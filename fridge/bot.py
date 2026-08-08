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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import actions, db
from .config import Settings, get_settings
from .models import ACTION_ADD, ItemSpec, ParsedAction
from .parser import build_parser
from .pending import (
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_ERROR,
    STATUS_NOT_AN_EDIT,
    STATUS_UPDATED,
    apply_pending_edit,
    edit_help_text,
    format_pending,
)
from .reminders import send_daily_reminders
from .transcribe import build_transcriber
from .vision import build_image_extractor

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
    "You can also:\n"
    "- send a voice message and I'll transcribe it\n"
    "- send a photo of a receipt or groceries — I'll propose items; confirm, "
    "cancel, or edit (e.g. remove 2 / change 1 to 3 eggs) before anything is saved\n\n"
    "In a group chat I keep one shared fridge and remember who added what. "
    'Ask "who bought the milk?" or "what did alice buy?" for attribution '
    "(ordinary lists don't show buyers).\n"
    "I'll also remind you when things are about to expire."
)


def resolve_user_id(update: Update) -> str:
    """A stable handle for the user who sent the update (attribution)."""
    user = update.effective_user
    if user and user.username:
        return user.username
    if user and user.first_name:
        return user.first_name
    if user:
        return f"user{user.id}"
    chat = update.effective_chat
    return f"chat:{chat.id}" if chat else "unknown"


def resolve_scope(update: Update) -> tuple[str, str, bool]:
    """Return ``(scope_key, added_by, is_group)`` for this update.

    - In a group/supergroup the scope is the shared ``chat:<id>`` fridge.
    - In a private chat the scope is the user's personal ``user:<handle>`` fridge.
    ``added_by`` is always the individual user, so group items stay attributed.
    """
    added_by = resolve_user_id(update)
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        return f"chat:{chat.id}", added_by, True
    return f"user:{added_by}", added_by, False


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
    scope_key, added_by, is_group = resolve_scope(update)
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    parser = context.application.bot_data["parser"]

    _remember_dm(conn, update, added_by)
    if update.effective_chat:
        # Immediate feedback while the (possibly slow) parse runs.
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="typing"
        )

    try:
        parsed = await asyncio.to_thread(parser.parse, message_text)
    except Exception:  # noqa: BLE001 - never crash the handler on parse errors
        logger.exception("Parser failed for message: %r", message_text)
        db.log_action(conn, added_by, message_text, "PARSE_ERROR")
        await update.message.reply_text(
            "Sorry, I had trouble understanding that. Please try rephrasing."
        )
        return

    db.log_action(conn, added_by, message_text, json.dumps(asdict(parsed)))
    reply = actions.execute(
        conn, scope_key, parsed, added_by=added_by, is_group=is_group
    )
    await update.message.reply_text(reply)


def _remember_dm(conn, update: Update, added_by: str) -> None:
    """Record a user's DM chat id (private chats only) for personal reminders."""
    chat = update.effective_chat
    if chat and chat.type == "private":
        db.remember_user(conn, added_by, chat.id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a typed text message (or a pending photo-draft edit)."""
    text = update.message.text or ""
    # If a photo draft is waiting, try to treat the text as an edit/confirm first.
    if context.user_data.get("pending_photo_items") is not None:
        handled = await _try_pending_photo_text(update, context, text)
        if handled:
            return
    await _process_text(update, context, text)


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
# Photo logging (extract -> verify/edit -> confirm)
# ---------------------------------------------------------------------------
def _photo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("\u2705 Add all", callback_data="img_confirm"),
                InlineKeyboardButton("\u274c Cancel", callback_data="img_cancel"),
            ]
        ]
    )


def _photo_proposal_text(items: list[ItemSpec], *, preface: str = "") -> str:
    body = (
        f"{preface}\n\n" if preface else ""
    ) + (
        "I found these items:\n"
        f"{format_pending(items)}\n\n"
        f"{edit_help_text()}"
    )
    return body


async def _commit_pending_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    items: list[ItemSpec],
    *,
    via_callback: bool,
) -> None:
    """Write pending items to the DB and clear the draft."""
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    scope_key, added_by, is_group = resolve_scope(update)
    action = ParsedAction(action=ACTION_ADD, items=items)
    reply = actions.execute(
        conn, scope_key, action, added_by=added_by, is_group=is_group
    )
    context.user_data.pop("pending_photo_items", None)
    db.log_action(conn, added_by, "<photo confirm>", f"VISION_ADDED:{len(items)}")
    text = f"Done!\n{reply}"
    if via_callback:
        await update.callback_query.edit_message_text(text)
    else:
        await update.message.reply_text(text)


async def _try_pending_photo_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    """Handle confirm/cancel/edit text against a pending photo draft.

    Returns True if the message was consumed (caller should not run the
    normal inventory pipeline).
    """
    items: list[ItemSpec] = list(context.user_data.get("pending_photo_items") or [])
    status, new_items, fragment = apply_pending_edit(items, text)

    if status == STATUS_NOT_AN_EDIT:
        return False

    if status == STATUS_CANCELLED:
        context.user_data.pop("pending_photo_items", None)
        await update.message.reply_text("Okay, I didn't add anything.")
        return True

    if status == STATUS_CONFIRMED:
        await _commit_pending_photo(update, context, new_items, via_callback=False)
        return True

    if status == STATUS_ERROR:
        await update.message.reply_text(
            f"{fragment}\n\nCurrent draft:\n{format_pending(items)}\n\n"
            f"{edit_help_text()}",
            reply_markup=_photo_keyboard(),
        )
        return True

    # STATUS_UPDATED
    context.user_data["pending_photo_items"] = new_items
    if not new_items:
        context.user_data.pop("pending_photo_items", None)
        await update.message.reply_text(
            f"{fragment}\nDraft is empty — nothing to add. Send another photo if you like."
        )
        return True
    await update.message.reply_text(
        f"{fragment}\n\nUpdated draft:\n{format_pending(new_items)}\n\n"
        f"{edit_help_text()}",
        reply_markup=_photo_keyboard(),
    )
    return True


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photo (receipt / groceries): extract items and ask to confirm/edit."""
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    extractor = context.application.bot_data.get("image_extractor")
    added_by = resolve_user_id(update)

    if extractor is None:
        await update.message.reply_text(
            "Photo logging needs an OpenAI API key for image understanding. "
            "Set OPENAI_API_KEY in .env, or add items by text."
        )
        return

    if not update.message.photo:
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )
    try:
        # The last PhotoSize is the highest resolution.
        tg_file = await update.message.photo[-1].get_file()
        image_bytes = bytes(await tg_file.download_as_bytearray())
        items = await asyncio.to_thread(extractor.extract, image_bytes, "image/jpeg")
    except Exception:  # noqa: BLE001 - never crash the handler
        logger.exception("Image extraction failed")
        db.log_action(conn, added_by, "<photo>", "VISION_ERROR")
        await update.message.reply_text(
            "Sorry, I couldn't read that image. Try a clearer photo or add by text."
        )
        return

    if not items:
        await update.message.reply_text(
            "I couldn't spot any grocery items in that photo. "
            "Try a clearer shot, or just type what you bought."
        )
        return

    # Stash the proposed items for this user until they confirm/cancel/edit.
    context.user_data["pending_photo_items"] = items
    db.log_action(conn, added_by, "<photo>", f"VISION_PROPOSED:{len(items)}")

    await update.message.reply_text(
        _photo_proposal_text(items),
        reply_markup=_photo_keyboard(),
    )


async def on_photo_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the Confirm/Cancel buttons under a proposed photo extraction."""
    query = update.callback_query
    await query.answer()

    items: list[ItemSpec] = context.user_data.get("pending_photo_items") or []

    if query.data == "img_cancel" or not items:
        context.user_data.pop("pending_photo_items", None)
        await query.edit_message_text("Okay, I didn't add anything.")
        return

    await _commit_pending_photo(update, context, items, via_callback=True)


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
    image_extractor = build_image_extractor(
        openai_api_key=settings.openai_api_key,
        model=settings.openai_vision_model,
    )
    if settings.has_openai:
        logger.info(
            "Using parser mode=%s (model=%s), voice (model=%s), vision (model=%s).",
            settings.parser_mode,
            settings.openai_model,
            settings.openai_transcribe_model,
            settings.openai_vision_model,
        )
    else:
        logger.warning(
            "OPENAI_API_KEY not set - using the offline rule-based parser; "
            "voice and photo logging are disabled. Set OPENAI_API_KEY in .env "
            "for full natural-language understanding, voice, and image support."
        )

    application.bot_data["conn"] = conn
    application.bot_data["parser"] = parser
    application.bot_data["transcriber"] = transcriber
    application.bot_data["image_extractor"] = image_extractor
    application.bot_data["expiry_reminder_days"] = settings.expiry_reminder_days

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, handle_voice)
    )
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(
        CallbackQueryHandler(on_photo_decision, pattern="^img_")
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
