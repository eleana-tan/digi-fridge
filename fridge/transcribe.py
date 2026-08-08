"""Speech-to-text layer: audio bytes -> transcript text.

Kept decoupled from Telegram (takes raw bytes, not Telegram objects) and, like
the parser, uses an injectable client so it can be unit-tested with a stub that
returns canned text instead of hitting the network.

Telegram voice notes are Opus audio in an OGG container (``.oga``), which the
OpenAI transcription API accepts directly, so no ffmpeg/transcoding is needed.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol


class Transcriber(Protocol):
    """Anything that turns audio bytes into text."""

    def transcribe(self, audio: bytes, filename: str) -> str:  # pragma: no cover
        ...


class OpenAITranscriber:
    """Transcribe audio with an OpenAI-compatible audio client."""

    def __init__(
        self,
        client: Any,
        model: str = "whisper-1",
        language: Optional[str] = None,
    ) -> None:
        self._client = client
        self._model = model
        # Pinning the language (ISO-639-1, e.g. "en") avoids Whisper's frequent
        # language misdetection on short/accented clips (it otherwise sometimes
        # transcribes English speech as Malay/Indonesian, etc.). Leave as None
        # to auto-detect.
        self._language = language or None

    def transcribe(self, audio: bytes, filename: str = "voice.oga") -> str:
        # The OpenAI SDK accepts ``file`` as a (filename, bytes) tuple.
        create_kwargs: dict[str, Any] = dict(
            model=self._model,
            file=(filename, audio),
        )
        if self._language:
            create_kwargs["language"] = self._language
        result = self._client.audio.transcriptions.create(**create_kwargs)
        # SDK returns an object with ``.text``; be tolerant of dict-like too.
        text = getattr(result, "text", None)
        if text is None and isinstance(result, dict):
            text = result.get("text")
        return (text or "").strip()


def build_transcriber(
    openai_api_key: str = "",
    model: str = "whisper-1",
    language: Optional[str] = None,
) -> Optional[Transcriber]:
    """Return an :class:`OpenAITranscriber`, or None if no API key is set.

    Transcription requires the OpenAI API (there's no offline fallback), so the
    caller should treat ``None`` as "voice input unavailable".
    """
    if not openai_api_key:
        return None
    try:
        from openai import OpenAI  # imported lazily so it stays optional
    except ImportError as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            "OPENAI_API_KEY is set but the 'openai' package is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc
    return OpenAITranscriber(
        client=OpenAI(api_key=openai_api_key), model=model, language=language
    )
