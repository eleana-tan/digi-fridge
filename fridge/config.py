"""Application settings.

Loads configuration from environment variables, optionally seeded from a
``.env`` file in the project root. We parse ``.env`` ourselves with a tiny
loader so we don't need an extra dependency (e.g. python-dotenv).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Populate ``os.environ`` from a ``.env`` file if present.

    Existing environment variables always win over the file, matching the
    behaviour of most dotenv loaders. Lines that are blank or start with ``#``
    are ignored. Values may optionally be wrapped in single or double quotes.
    """
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_optional_float(name: str) -> "float | None":
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _get_optional_int(name: str) -> "int | None":
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class Settings:
    """Resolved runtime settings."""

    telegram_bot_token: str
    openai_api_key: str
    openai_model: str
    openai_temperature: "float | None"
    openai_reasoning_effort: str
    openai_max_tokens: "int | None"
    parser_mode: str
    openai_transcribe_model: str
    openai_transcribe_language: str
    db_path: str
    expiry_reminder_days: int
    reminder_hour: int
    reminder_minute: int

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key)


def get_settings() -> Settings:
    """Build a :class:`Settings` object from the current environment."""
    load_dotenv()
    reminder_time = os.environ.get("REMINDER_TIME", "09:00")
    try:
        hour_str, minute_str = reminder_time.split(":", 1)
        reminder_hour, reminder_minute = int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        reminder_hour, reminder_minute = 9, 0

    return Settings(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        openai_temperature=_get_optional_float("OPENAI_TEMPERATURE"),
        openai_reasoning_effort=os.environ.get("OPENAI_REASONING_EFFORT", ""),
        openai_max_tokens=_get_optional_int("OPENAI_MAX_TOKENS"),
        parser_mode=os.environ.get("PARSER_MODE", "llm").strip().lower() or "llm",
        openai_transcribe_model=os.environ.get(
            "OPENAI_TRANSCRIBE_MODEL", "whisper-1"
        ),
        # Default to English to avoid Whisper misdetecting language on short
        # clips. Set to "" for auto-detect, or another ISO-639-1 code.
        openai_transcribe_language=os.environ.get(
            "OPENAI_TRANSCRIBE_LANGUAGE", "en"
        ),
        db_path=os.environ.get("DB_PATH", str(PROJECT_ROOT / "fridge.db")),
        expiry_reminder_days=_get_int("EXPIRY_REMINDER_DAYS", 2),
        reminder_hour=reminder_hour,
        reminder_minute=reminder_minute,
    )
