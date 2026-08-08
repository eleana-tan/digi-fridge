"""Telegram wiring and application entrypoint.

This is the ONLY module that imports python-telegram-bot. Everything it needs
from the rest of the app comes through the decoupled ``parser``, ``actions``,
``db`` and ``reminders`` modules.

Run it with::

    python -m fridge.bot          # or: python run.py

Set ``ECHO_MODE=1`` to run the step-1 plumbing check (echoes messages, no LLM,
no DB).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from datetime import time as dt_time

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import actions, db
from .config import Settings, get_settings
from .parser import build_parser
from .reminders import send_daily_reminders
from .transcribe import build_transcriber

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WELCOME = (
    "Hi! I'm your fridge/pantry inventory bot.\n\n"
    "Just talk to me in plain language, e.g.:\n"
    '- "bought 2 cartons of milk and a dozen eggs"\n'
    '- "used the last of the cheese"\n'
    '- "what do I have?"\n'
    '- "what\'s expiring soon?"\n'
    '- "do I have milk?"\n\n'
    "You can also send me a voice message and I'll transcribe it.\n"
    "I'll also remind you when things are about to expire."
)


def resolve_user_id(update: Update) -> str:
    """Prefer the Telegram username; fall back to a stable chat-id handle."""
    user = update.effective_user
    if user and user.username:
        return user.username
    chat = update.effective_chat
    return f"chat:{chat.id}" if chat else "unknown"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Step-1 plumbing check: echo whatever the user sends."""
    await update.message.reply_text(f"You said: {update.message.text}")


async def _process_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str
) -> None:
    """Shared pipeline for text (typed or transcribed): parse -> DB -> reply.

    The blocking LLM call runs in a worker thread (``asyncio.to_thread``) so a
    slow request from one user never stalls the event loop for other users.
    Database access stays on the event-loop thread, keeping SQLite access
    single-threaded and safe.
    """
    user_id = resolve_user_id(update)
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    parser = context.application.bot_data["parser"]

    # Remember how to reach this user for background reminders.
    if update.effective_chat:
        db.remember_user(conn, user_id, update.effective_chat.id)
        # Immediate feedback while the (possibly slow) parse runs.
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="typing"
        )

    try:
        parsed = await asyncio.to_thread(parser.parse, message_text)
    except Exception:  # noqa: BLE001 - never crash the handler on parse errors
        logger.exception("Parser failed for message: %r", message_text)
        db.log_action(conn, user_id, message_text, "PARSE_ERROR")
        await update.message.reply_text(
            "Sorry, I had trouble understanding that. Please try rephrasing."
        )
        return

    db.log_action(conn, user_id, message_text, json.dumps(asdict(parsed)))
    reply = actions.execute(conn, user_id, parsed)
    await update.message.reply_text(reply)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a typed text message."""
    await _process_text(update, context, update.message.text or "")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a voice note / audio: download -> transcribe -> text pipeline."""
    user_id = resolve_user_id(update)
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    transcriber = context.application.bot_data.get("transcriber")

    if transcriber is None:
        await update.message.reply_text(
            "Voice messages need an OpenAI API key for transcription. "
            "Set OPENAI_API_KEY in .env, or just send me a text message."
        )
        return

    voice = update.message.voice or update.message.audio
    if voice is None:
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    try:
        tg_file = await voice.get_file()
        audio_bytes = bytes(await tg_file.download_as_bytearray())
        # Telegram voice notes are Opus/OGG; keep a sensible extension for the API.
        filename = "audio.mp3" if update.message.audio else "voice.oga"
        transcript = await asyncio.to_thread(
            transcriber.transcribe, audio_bytes, filename
        )
    except Exception:  # noqa: BLE001 - never crash the handler
        logger.exception("Transcription failed")
        db.log_action(conn, user_id, "<voice message>", "TRANSCRIBE_ERROR")
        await update.message.reply_text(
            "Sorry, I couldn't transcribe that audio. Please try again or type it."
        )
        return

    if not transcript:
        await update.message.reply_text(
            "I couldn't hear anything in that recording. Mind trying again?"
        )
        return

    # Echo what we heard so misheard audio is easy to spot, then process it.
    await update.message.reply_text(f'I heard: "{transcript}"')
    await _process_text(update, context, transcript)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
def build_application(settings: Settings) -> Application:
    if not settings.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    application = Application.builder().token(settings.telegram_bot_token).build()

    echo_mode = os.environ.get("ECHO_MODE", "").strip() in {"1", "true", "yes"}

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))

    if echo_mode:
        logger.info("Starting in ECHO_MODE (step 1 plumbing check).")
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, echo)
        )
        return application

    # Full mode: open the DB, build the parser, wire the reminder job.
    conn = db.connect(settings.db_path)
    parser = build_parser(
        openai_api_key=settings.openai_api_key,
        model=settings.openai_model,
        temperature=settings.openai_temperature,
        mode=settings.parser_mode,
        reasoning_effort=settings.openai_reasoning_effort,
        max_completion_tokens=settings.openai_max_tokens,
    )
    transcriber = build_transcriber(
        openai_api_key=settings.openai_api_key,
        model=settings.openai_transcribe_model,
        language=settings.openai_transcribe_language,
    )
    if settings.has_openai:
        logger.info(
            "Using parser mode=%s (model=%s) and voice transcription (model=%s).",
            settings.parser_mode,
            settings.openai_model,
            settings.openai_transcribe_model,
        )
    else:
        logger.warning(
            "OPENAI_API_KEY not set - using the offline rule-based parser and "
            "voice input is disabled. Set OPENAI_API_KEY in .env for full "
            "natural-language understanding and voice support."
        )

    application.bot_data["conn"] = conn
    application.bot_data["parser"] = parser
    application.bot_data["transcriber"] = transcriber
    application.bot_data["expiry_reminder_days"] = settings.expiry_reminder_days

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, handle_voice)
    )

    # Daily expiry reminder job.
    if application.job_queue is not None:
        application.job_queue.run_daily(
            send_daily_reminders,
            time=dt_time(
                hour=settings.reminder_hour,
                minute=settings.reminder_minute,
            ),
            name="daily_expiry_reminders",
        )
        logger.info(
            "Scheduled daily expiry reminders at %02d:%02d (server time), "
            "window=%d days.",
            settings.reminder_hour,
            settings.reminder_minute,
            settings.expiry_reminder_days,
        )
    else:
        logger.warning(
            "JobQueue unavailable - install python-telegram-bot[job-queue] "
            "to enable expiry reminders."
        )

    return application


def main() -> None:
    settings = get_settings()
    application = build_application(settings)
    logger.info("Bot starting. Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
