"""Image understanding: a photo (receipt or groceries) -> list of items.

Decoupled from Telegram (takes raw image bytes) and uses an injectable client,
so it can be unit-tested with a stub that returns canned JSON. Reuses the
parser's JSON-to-items transform so the extracted structure matches everything
else in the app.

The extracted items are always *proposed* — the bot shows them to the user for
confirmation before anything is written to the database.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional, Protocol

from .models import ItemSpec
from .parser import parsed_action_from_dict

_SYSTEM_PROMPT = """\
You extract grocery/pantry items from an image. The image is either a shopping
receipt or a photo of groceries laid out. Respond with ONLY a JSON object:

{
  "items": [
    {
      "item_name": string,          // singular, lowercase, no brand noise
      "item_qty": number | null,
      "unit": string | null,        // e.g. "carton", "count", "g", "loaf"
      "expires_on": string | null,  // ISO date if clearly printed, else null
      "category": string | null     // dairy | produce | meat | pantry | frozen | other
    }
  ]
}

Rules:
- Include only food/grocery/household-pantry items. Ignore totals, taxes,
  store names, phone numbers, loyalty text, and non-grocery lines.
- If a quantity is printed, use it; otherwise null.
- Merge obvious duplicates. If you can't read it confidently, leave it out.
- If there are no grocery items, return {"items": []}.
"""


class ImageExtractor(Protocol):
    """Anything that turns image bytes into a list of proposed items."""

    def extract(self, image: bytes, mime: str = "image/jpeg") -> list[ItemSpec]:
        ...  # pragma: no cover - protocol


class OpenAIImageExtractor:
    """Extract items from an image with an OpenAI vision-capable model."""

    def __init__(self, client: Any, model: str = "gpt-4o-mini") -> None:
        self._client = client
        self._model = model

    def extract(self, image: bytes, mime: str = "image/jpeg") -> list[ItemSpec]:
        b64 = base64.b64encode(image).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        response = self._client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract the grocery items."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        )
        content = response.choices[0].message.content or "{}"
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []
        # Reuse the parser's validation/normalisation (dates, categories, etc.).
        data.setdefault("action", "add")
        return parsed_action_from_dict(data).items


def build_image_extractor(
    openai_api_key: str = "",
    model: str = "gpt-4o-mini",
) -> Optional[ImageExtractor]:
    """Return an :class:`OpenAIImageExtractor`, or None if no API key is set."""
    if not openai_api_key:
        return None
    try:
        from openai import OpenAI  # imported lazily so it stays optional
    except ImportError as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            "OPENAI_API_KEY is set but the 'openai' package is not installed. "
            "Run: pip install -r requirements.txt"
        ) from exc
    return OpenAIImageExtractor(client=OpenAI(api_key=openai_api_key), model=model)
