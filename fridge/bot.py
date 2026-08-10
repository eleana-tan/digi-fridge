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
import random
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from datetime import time as dt_time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction
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
from .models import (
    ACTION_ADD,
    ACTION_QUERY,
    QUERY_BY_USER,
    QUERY_EXPIRING,
    QUERY_LIST_ALL,
    QUERY_RECIPES,
    QUERY_WHO_HAS,
    ItemSpec,
    ParsedAction,
)
from .group_gate import (
    should_handle_in_group,
    strip_bot_mention,
)
from .parser import build_parser
from .recipes import (
    build_recipe_suggester,
    format_recipe_reply,
)
from .saved_recipes import (
    PENDING_RECIPE_ADD_PROMPT,
    format_save_confirmation,
    format_saved_list,
    format_saved_matches_section,
    match_saved_recipes,
    parse_recipe_add_args,
    parse_recipe_add_message,
    title_from_recipe_url,
)
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
    '- "do I have milk?"\n'
    '- "what can I cook?" / "recipes with eggs and spinach"\n\n'
    "Slash commands (tap / in Telegram):\n"
    "/list — what's in the fridge\n"
    "/expiring — what's going bad soon\n"
    "/recipe — meal ideas from your fridge + saved reels\n"
    "/recipe korean — filter by keyword/style (matches your saves too)\n"
    "/recipe_add — save an Instagram reel (+ optional keywords)\n"
    "/reels — list saved recipe links\n"
    "/recipe_remove 12 — remove a saved recipe by id\n"
    "/who milk — who bought an item\n"
    "/by alice — what someone added\n"
    "/clear — empty the whole fridge (asks to confirm)\n"
    "/cancel — discard a pending photo draft or recipe-add\n"
    "/help — this message\n\n"
    "You can also:\n"
    "- send a voice message and I'll transcribe it\n"
    "- send a photo of a receipt or groceries — I'll propose items; confirm, "
    "cancel, or edit (e.g. remove 2 / change 1 to 3 eggs) before anything is saved\n"
    "- save Instagram recipe reels one-by-one with /recipe_add and tag them "
    "(e.g. /recipe_add <url> korean spicy) so /recipe korean can find them\n\n"
    "In a group chat I keep one shared fridge and remember who added what. "
    'Ask "who bought the milk?" or "what did alice buy?" for attribution '
    "(ordinary lists don't show buyers).\n"
    "In groups I ignore normal chat — @mention me, reply to me, use a /command, "
    'or say something fridge-related like "bought milk". '
    "A photo always means \"add these groceries\" (you'll confirm first).\n"
    "I'll also remind you when things are about to expire."
)

# Shown in Telegram's "/" menu (registered on startup via set_my_commands).
BOT_COMMANDS = [
    BotCommand("start", "Welcome & how to use"),
    BotCommand("help", "Help and tips"),
    BotCommand("list", "Show what's in the fridge"),
    BotCommand("expiring", "Show items expiring soon"),
    BotCommand("recipe", "Ideas from fridge/saves — /recipe korean"),
    BotCommand("recipe_add", "Save Instagram reel + keywords"),
    BotCommand("reels", "List saved recipe reels"),
    BotCommand("recipe_remove", "Remove saved recipe — /recipe_remove 12"),
    BotCommand("who", "Who bought an item — /who milk"),
    BotCommand("by", "What someone added — /by alice"),
    BotCommand("clear", "Empty the whole fridge (asks to confirm)"),
    BotCommand("cancel", "Cancel pending photo draft or recipe-add"),
]


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


def _is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))


def _should_process_group_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    text: str = "",
    allow_fridge_intent: bool = True,
) -> bool:
    """In groups, ignore chatter unless the bot is addressed or it's fridge-like."""
    return should_handle_in_group(
        is_group=_is_group_chat(update),
        message=update.message,
        bot_id=context.bot.id,
        bot_username=context.bot.username or "",
        text=text,
        allow_fridge_intent=allow_fridge_intent,
    )


# Telegram clears chat-action status after ~5s; refresh sooner so it never gaps.
_TYPING_INTERVAL_S = 2.5
# Shown briefly, then edited into the real reply.
_PROGRESS_PLACEHOLDERS = (
    "One sec — looking that up…",
    "On it — give me a moment…",
    "Hang tight, working on that…",
    "Okay, let me check…",
    "Just a moment…",
)


class ProgressReply:
    """Placeholder message that becomes the bot's final answer via ``send``."""

    def __init__(self, message: Message | None, status: Message | None) -> None:
        self._message = message
        self._status = status

    async def send(self, text: str, **kwargs) -> None:
        status = self._status
        self._status = None
        if status is not None:
            try:
                await status.edit_text(text, **kwargs)
                return
            except Exception:  # noqa: BLE001
                logger.debug("Could not edit progress placeholder", exc_info=True)
                with suppress(Exception):
                    await status.delete()
        if self._message is not None:
            await self._message.reply_text(text, **kwargs)

    async def discard(self) -> None:
        """Remove an unused placeholder (e.g. handler returned with no reply)."""
        status = self._status
        self._status = None
        if status is not None:
            with suppress(Exception):
                await status.delete()


def _chat_target(
    update: Update,
) -> tuple[int | None, Message | None, int | None]:
    chat = update.effective_chat
    message = update.effective_message
    thread_id = getattr(message, "message_thread_id", None) if message else None
    return (chat.id if chat else None, message, thread_id)


@asynccontextmanager
async def typing_while(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int | None,
    *,
    message: Message | None = None,
    message_thread_id: int | None = None,
):
    """Keep "typing…" alive and expose a placeholder the caller edits into a reply.

    Yields a :class:`ProgressReply`. Prefer ``await progress.send(text)`` for the
    final answer so something stays visible in-chat until the reply is ready.
    """
    if message_thread_id is None and message is not None:
        message_thread_id = getattr(message, "message_thread_id", None)

    status: Message | None = None
    if message is not None:
        try:
            status = await message.reply_text(random.choice(_PROGRESS_PLACEHOLDERS))
        except Exception:  # noqa: BLE001
            logger.debug("Could not send progress placeholder", exc_info=True)

    progress = ProgressReply(message, status)
    stop = asyncio.Event()

    async def _pulse() -> None:
        if chat_id is None:
            return
        while not stop.is_set():
            try:
                await context.bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.TYPING,
                    message_thread_id=message_thread_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep pulsing through transient errors
                logger.warning("typing chat action failed", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=_TYPING_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_pulse(), name="typing-pulse")
    try:
        yield progress
    finally:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await progress.discard()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME)


async def _run_query(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parsed: ParsedAction,
) -> None:
    """Run a structured query against the current chat's fridge scope."""
    scope_key, added_by, is_group = resolve_scope(update)
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    _remember_dm(conn, update, added_by)
    reply = actions.execute(
        conn, scope_key, parsed, added_by=added_by, is_group=is_group
    )
    await update.message.reply_text(reply)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_query(
        update,
        context,
        ParsedAction(action=ACTION_QUERY, query_type=QUERY_LIST_ALL),
    )


async def cmd_expiring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_query(
        update,
        context,
        ParsedAction(action=ACTION_QUERY, query_type=QUERY_EXPIRING),
    )


async def cmd_who(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/who milk — who bought that item."""
    target = " ".join(context.args).strip() if context.args else ""
    if not target:
        await update.message.reply_text(
            "Usage: /who <item>\nExample: /who milk"
        )
        return
    await _run_query(
        update,
        context,
        ParsedAction(
            action=ACTION_QUERY, query_type=QUERY_WHO_HAS, query_target=target
        ),
    )


async def cmd_by(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/by alice — what that person added."""
    target = " ".join(context.args).strip().lstrip("@") if context.args else ""
    if not target:
        await update.message.reply_text(
            "Usage: /by <username>\nExample: /by alice"
        )
        return
    await _run_query(
        update,
        context,
        ParsedAction(
            action=ACTION_QUERY, query_type=QUERY_BY_USER, query_target=target
        ),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Discard a pending photo draft or recipe-add wait, if any."""
    cleared = []
    if context.user_data.pop("pending_photo_items", None) is not None:
        cleared.append("photo draft")
    if context.user_data.pop("pending_recipe_add", None) is not None:
        cleared.append("recipe-add")
    if not cleared:
        await update.message.reply_text("Nothing pending to cancel.")
        return
    await update.message.reply_text(
        "Okay, I cancelled the pending " + " and ".join(cleared) + "."
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for confirmation, then wipe every item in this chat's fridge."""
    scope_key, added_by, is_group = resolve_scope(update)
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    _remember_dm(conn, update, added_by)

    count = len(db.get_items(conn, scope_key))
    if count == 0:
        await update.message.reply_text("The fridge is already empty.")
        return

    where = "this group's shared fridge" if is_group else "your fridge"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Yes, clear everything", callback_data="clear_confirm"
                ),
                InlineKeyboardButton("Cancel", callback_data="clear_cancel"),
            ]
        ]
    )
    await update.message.reply_text(
        f"Clear all {count} item(s) from {where}?\n"
        "This cannot be undone.",
        reply_markup=keyboard,
    )


async def on_clear_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Yes/Cancel under a /clear prompt."""
    query = update.callback_query
    await query.answer()

    if query.data == "clear_cancel":
        await query.edit_message_text("Okay, I left the fridge as-is.")
        return

    scope_key, added_by, is_group = resolve_scope(update)
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    removed = db.clear_scope(conn, scope_key)
    db.log_action(conn, added_by, "/clear", f"CLEARED:{removed}")
    where = "the group's fridge" if is_group else "your fridge"
    await query.edit_message_text(
        f"Cleared {removed} item(s) from {where}. It's empty now."
    )


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
    chat_id, message, thread_id = _chat_target(update)

    async with typing_while(
        context, chat_id, message=message, message_thread_id=thread_id
    ) as progress:
        try:
            parsed = await asyncio.to_thread(parser.parse, message_text)
        except Exception:  # noqa: BLE001 - never crash the handler on parse errors
            logger.exception("Parser failed for message: %r", message_text)
            db.log_action(conn, added_by, message_text, "PARSE_ERROR")
            await progress.send(
                "Sorry, I had trouble understanding that. Please try rephrasing."
            )
            return

        db.log_action(conn, added_by, message_text, json.dumps(asdict(parsed)))

        if parsed.action == ACTION_QUERY and parsed.query_type == QUERY_RECIPES:
            await _reply_recipes(
                update, context, scope_key, parsed, progress=progress
            )
            return

        reply = actions.execute(
            conn, scope_key, parsed, added_by=added_by, is_group=is_group
        )
        await progress.send(reply)


async def _reply_recipes(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    scope_key: str,
    parsed: ParsedAction,
    *,
    progress: ProgressReply,
) -> None:
    """Suggest recipes from fridge inventory, keywords, and saved Instagram reels.

    ``progress`` comes from the caller's ``typing_while`` so typing/placeholder
    stay active through the slow suggestion call.
    """
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    suggester = context.application.bot_data.get("recipe_suggester")

    request = (parsed.notes or "").strip()
    ingredients = [spec.item_name for spec in parsed.items if spec.item_name]
    if not ingredients:
        ingredients = [item.item_name for item in db.get_items(conn, scope_key)]

    # Dedupe while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for name in ingredients:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(name)

    saved_all = db.list_saved_recipes(conn, scope_key)
    matched = match_saved_recipes(
        saved_all, query=request, ingredients=unique, limit=3
    )
    saved_section = format_saved_matches_section(matched)

    recipes = []
    if suggester is not None and (unique or request):
        try:
            recipes = await asyncio.to_thread(
                suggester.suggest, unique, request=request
            )
        except Exception:  # noqa: BLE001
            logger.exception("Recipe suggestion failed")
            if not saved_section:
                await progress.send(
                    "Sorry, I couldn't fetch recipe ideas just now. "
                    "Try again in a moment."
                )
                return
    elif suggester is None and not saved_section:
        await progress.send(
            "Recipe ideas need an OpenAI API key (or save Instagram reels with "
            "/recipe_add). Set OPENAI_API_KEY in .env."
        )
        return

    await progress.send(
        format_recipe_reply(
            recipes, unique, request=request, saved_section=saved_section
        ),
        disable_web_page_preview=True,
    )


async def cmd_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recipe — fridge + saves; /recipe korean — keyword/style filter."""
    scope_key, added_by, _is_group = resolve_scope(update)
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    _remember_dm(conn, update, added_by)

    # Slash args are keywords/style (custom tags from /recipe_add), not grocery
    # names — ingredients always come from the fridge. Use NL for "recipes with X".
    request = " ".join(context.args or []).strip()
    parsed = ParsedAction(
        action=ACTION_QUERY,
        query_type=QUERY_RECIPES,
        items=[],
        notes=request or None,
    )
    chat_id, message, thread_id = _chat_target(update)
    async with typing_while(
        context, chat_id, message=message, message_thread_id=thread_id
    ) as progress:
        await _reply_recipes(update, context, scope_key, parsed, progress=progress)


async def _save_recipe_from_parts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    keywords: list[str],
) -> None:
    scope_key, added_by, _is_group = resolve_scope(update)
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    _remember_dm(conn, update, added_by)
    title = title_from_recipe_url(url)
    recipe, created = db.add_saved_recipe(
        conn, scope_key, added_by, url, title, keywords
    )
    db.log_action(
        conn,
        added_by,
        url,
        f"SAVED_RECIPE:{recipe.id}:{'new' if created else 'update'}",
    )
    await update.message.reply_text(
        format_save_confirmation(recipe, created=created),
        disable_web_page_preview=True,
    )


async def cmd_recipe_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recipe_add <url> [keywords…] — or /recipe_add then paste the link next."""
    args = list(context.args or [])
    if not args:
        context.user_data["pending_recipe_add"] = True
        await update.message.reply_text(PENDING_RECIPE_ADD_PROMPT)
        return

    url, keywords = parse_recipe_add_args(args)
    if not url:
        await update.message.reply_text(
            "I need a recipe link (Instagram reel or any website URL).\n"
            "Example: /recipe_add https://www.instagram.com/reel/XXXX/ korean spicy"
        )
        return

    context.user_data.pop("pending_recipe_add", None)
    await _save_recipe_from_parts(update, context, url, keywords)


async def cmd_reels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reels — list saved recipe links for this fridge scope."""
    scope_key, added_by, _is_group = resolve_scope(update)
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    _remember_dm(conn, update, added_by)
    recipes = db.list_saved_recipes(conn, scope_key)
    await update.message.reply_text(
        format_saved_list(recipes),
        disable_web_page_preview=True,
    )


async def cmd_recipe_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recipe_remove <id> — delete a saved recipe."""
    scope_key, added_by, _is_group = resolve_scope(update)
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    _remember_dm(conn, update, added_by)

    if not context.args:
        await update.message.reply_text(
            "Usage: /recipe_remove <id>\n"
            "See ids with /reels."
        )
        return
    raw = context.args[0].lstrip("#")
    try:
        recipe_id = int(raw)
    except ValueError:
        await update.message.reply_text(
            "That doesn't look like a recipe id. Try /reels, then "
            "/recipe_remove 12"
        )
        return

    existing = db.get_saved_recipe(conn, scope_key, recipe_id)
    if existing is None or not db.delete_saved_recipe(conn, scope_key, recipe_id):
        await update.message.reply_text(
            f"No saved recipe #{recipe_id} in this fridge. Check /reels."
        )
        return
    db.log_action(conn, added_by, f"/recipe_remove {recipe_id}", "REMOVED_RECIPE")
    await update.message.reply_text(
        f"Removed #{recipe_id} ({existing.title}).",
        disable_web_page_preview=True,
    )


async def _try_pending_recipe_add(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    """Handle the follow-up message after bare ``/recipe_add``."""
    if not context.user_data.get("pending_recipe_add"):
        return False
    url, keywords = parse_recipe_add_message(text)
    if not url:
        await update.message.reply_text(PENDING_RECIPE_ADD_PROMPT)
        return True
    context.user_data.pop("pending_recipe_add", None)
    await _save_recipe_from_parts(update, context, url, keywords)
    return True


def _remember_dm(conn, update: Update, added_by: str) -> None:
    """Record a user's DM chat id (private chats only) for personal reminders."""
    chat = update.effective_chat
    if chat and chat.type == "private":
        db.remember_user(conn, added_by, chat.id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a typed text message (or a pending photo-draft / recipe-add)."""
    text = update.message.text or ""
    # Pending flows first — even in groups, so a follow-up paste after /recipe_add
    # isn't dropped by the chatter gate.
    if context.user_data.get("pending_recipe_add"):
        handled = await _try_pending_recipe_add(update, context, text)
        if handled:
            return
    if context.user_data.get("pending_photo_items") is not None:
        handled = await _try_pending_photo_text(update, context, text)
        if handled:
            return
    if not _should_process_group_message(update, context, text=text):
        return  # group chatter — stay silent
    # "@FridgeBot bought milk" -> parse "bought milk"
    text = strip_bot_mention(text, context.bot.username or "")
    if not text:
        return
    await _process_text(update, context, text)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a voice note / audio: download -> transcribe -> text pipeline."""
    # In groups, only react to voice when @mentioned or replying to the bot.
    if not _should_process_group_message(
        update, context, text="", allow_fridge_intent=False
    ):
        return

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

    chat_id, message, thread_id = _chat_target(update)
    async with typing_while(
        context, chat_id, message=message, message_thread_id=thread_id
    ) as progress:
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
            await progress.send(
                "Sorry, I couldn't transcribe that audio. Please try again or type it."
            )
            return

        if not transcript:
            await progress.send(
                "I couldn't hear anything in that recording. Mind trying again?"
            )
            return

        # Echo what we heard so misheard audio is easy to spot, then process it.
        await progress.send(f'I heard: "{transcript}"')

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


def _image_mime_from_message(message) -> str:
    doc = getattr(message, "document", None)
    if doc and getattr(doc, "mime_type", None):
        return doc.mime_type
    return "image/jpeg"


async def _download_image_bytes(message) -> bytes | None:
    """Get image bytes from a compressed photo or an image document upload."""
    if message.photo:
        # Last PhotoSize is the highest resolution.
        tg_file = await message.photo[-1].get_file()
        return bytes(await tg_file.download_as_bytearray())
    doc = message.document
    if doc and (doc.mime_type or "").startswith("image/"):
        tg_file = await doc.get_file()
        return bytes(await tg_file.download_as_bytearray())
    return None


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """An image always means: propose inventory items from the photo (then confirm).

    No caption or @mention required — uploading a picture is the intent. Works in
    DMs and groups (compressed photos and image file uploads).
    """
    conn: "db.sqlite3.Connection" = context.application.bot_data["conn"]
    extractor = context.application.bot_data.get("image_extractor")
    added_by = resolve_user_id(update)

    if extractor is None:
        await update.message.reply_text(
            "Photo logging needs an OpenAI API key for image understanding. "
            "Set OPENAI_API_KEY in .env, or add items by text."
        )
        return

    chat_id, message, thread_id = _chat_target(update)
    async with typing_while(
        context, chat_id, message=message, message_thread_id=thread_id
    ) as progress:
        try:
            image_bytes = await _download_image_bytes(update.message)
            if not image_bytes:
                await progress.discard()
                return
            mime = _image_mime_from_message(update.message)
            items = await asyncio.to_thread(extractor.extract, image_bytes, mime)
        except Exception:  # noqa: BLE001 - never crash the handler
            logger.exception("Image extraction failed")
            db.log_action(conn, added_by, "<photo>", "VISION_ERROR")
            await progress.send(
                "Sorry, I couldn't read that image. Try a clearer photo or add by text."
            )
            return

        if not items:
            await progress.send(
                "I couldn't spot any grocery items in that photo. "
                "Try a clearer shot, or just type what you bought."
            )
            return

        # Stash the proposed items for this user until they confirm/cancel/edit.
        context.user_data["pending_photo_items"] = items
        db.log_action(conn, added_by, "<photo>", f"VISION_PROPOSED:{len(items)}")

        await progress.send(
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
async def _post_init(application: Application) -> None:
    """Publish slash commands so they appear in Telegram's / menu."""
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Registered bot commands: %s", ", ".join(c.command for c in BOT_COMMANDS))


def build_application(settings: Settings) -> Application:
    if not settings.telegram_bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_post_init)
        .build()
    )

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
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("expiring", cmd_expiring))
    application.add_handler(CommandHandler("recipe", cmd_recipe))
    application.add_handler(CommandHandler("recipe_add", cmd_recipe_add))
    application.add_handler(CommandHandler("reels", cmd_reels))
    application.add_handler(CommandHandler("recipe_remove", cmd_recipe_remove))
    application.add_handler(CommandHandler("who", cmd_who))
    application.add_handler(CommandHandler("by", cmd_by))
    application.add_handler(CommandHandler("clear", cmd_clear))
    application.add_handler(CommandHandler("cancel", cmd_cancel))

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
    recipe_suggester = build_recipe_suggester(
        openai_api_key=settings.openai_api_key,
        model=settings.openai_model,
    )
    if settings.has_openai:
        logger.info(
            "Using parser mode=%s (model=%s), voice (model=%s), vision (model=%s), "
            "recipes enabled.",
            settings.parser_mode,
            settings.openai_model,
            settings.openai_transcribe_model,
            settings.openai_vision_model,
        )
    else:
        logger.warning(
            "OPENAI_API_KEY not set - using the offline rule-based parser; "
            "voice, photo, and recipe ideas are disabled. Set OPENAI_API_KEY in "
            ".env for full natural-language understanding and those features."
        )

    application.bot_data["conn"] = conn
    application.bot_data["parser"] = parser
    application.bot_data["transcriber"] = transcriber
    application.bot_data["image_extractor"] = image_extractor
    application.bot_data["recipe_suggester"] = recipe_suggester
    application.bot_data["expiry_reminder_days"] = settings.expiry_reminder_days

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, handle_voice)
    )
    # Any image upload = inventory extract (photo bubble or image file).
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo)
    )
    application.add_handler(
        CallbackQueryHandler(on_photo_decision, pattern="^img_")
    )
    application.add_handler(
        CallbackQueryHandler(on_clear_decision, pattern="^clear_")
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
