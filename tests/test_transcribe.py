"""Transcriber tests - offline, using a stub audio client (no network)."""

from __future__ import annotations

import unittest

from fridge.transcribe import OpenAITranscriber, build_transcriber


class _StubResult:
    def __init__(self, text):
        self.text = text


class _StubTranscriptions:
    def __init__(self, text):
        self._text = text
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _StubResult(self._text)


class _StubAudio:
    def __init__(self, text):
        self.transcriptions = _StubTranscriptions(text)


class _StubClient:
    """Mimics the slice of the OpenAI client OpenAITranscriber uses."""

    def __init__(self, text):
        self.audio = _StubAudio(text)


class TranscriberTests(unittest.TestCase):
    def test_returns_transcript_text(self):
        client = _StubClient("bought two cartons of milk")
        t = OpenAITranscriber(client=client, model="whisper-1")
        text = t.transcribe(b"\x00\x01\x02", "voice.oga")
        self.assertEqual(text, "bought two cartons of milk")
        # It forwarded the model and a (filename, bytes) file tuple.
        kwargs = client.audio.transcriptions.last_kwargs
        self.assertEqual(kwargs["model"], "whisper-1")
        self.assertEqual(kwargs["file"][0], "voice.oga")
        self.assertEqual(kwargs["file"][1], b"\x00\x01\x02")

    def test_language_forwarded_when_set(self):
        client = _StubClient("hello")
        OpenAITranscriber(client=client, language="en").transcribe(b"x", "a.oga")
        self.assertEqual(client.audio.transcriptions.last_kwargs["language"], "en")

    def test_language_omitted_when_unset(self):
        client = _StubClient("hello")
        OpenAITranscriber(client=client).transcribe(b"x", "a.oga")
        self.assertNotIn("language", client.audio.transcriptions.last_kwargs)

    def test_strips_whitespace(self):
        t = OpenAITranscriber(client=_StubClient("  hello  \n"))
        self.assertEqual(t.transcribe(b"x", "a.oga"), "hello")

    def test_handles_empty(self):
        t = OpenAITranscriber(client=_StubClient(None))
        self.assertEqual(t.transcribe(b"x", "a.oga"), "")

    def test_build_transcriber_none_without_key(self):
        self.assertIsNone(build_transcriber(openai_api_key=""))


if __name__ == "__main__":
    unittest.main()
