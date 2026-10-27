"""Model capability probing — evidence-based flags for the vision gate.

``supports_vision`` in ``gateway_config:model_limits`` decides whether a chain
step may serve an image request. It used to be hand-set, and CUR-144 was the
result: a multimodal model (GLM-5.3-Flash-EXL3) sat flagged text-only, so every
lane refused images while text flowed fine.

This module makes the flag *provable*: it sends a small fixture image with known
content to a model and reports whether the model read it back. The Capabilities
tab calls this; a scheduled audit would call exactly the same function, which is
what stops a model swap from silently blinding a lane again.
"""

from __future__ import annotations

import json
import ssl
import time
from typing import Optional
from urllib import error
from urllib import request as urlrequest

# 320x170 PNG reading "ZEBRA" / "7291". Arbitrary text: legible to OCR, and
# impossible to answer correctly without actually seeing the image, so a model
# that returns the tokens proves it read the pixels rather than guessed.
FIXTURE_PNG_B64 = (
    "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"
)

# Both tokens must appear for the probe to count as a pass.
FIXTURE_TOKENS = ("zebra", "7291")

PROMPT = "Transcribe ALL text in this image exactly."


def fixture_data_url() -> str:
    """Return the fixture as a data: URL for an image_url content part."""
    return "data:image/png;base64," + FIXTURE_PNG_B64


def _extract_text(payload: dict) -> str:
    """Pull readable text out of a chat-completion response.

    Thinking models often put the answer in ``reasoning_content`` and leave
    ``content`` empty, so both are consulted.
    """
    try:
        msg = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    parts = []
    for key in ("content", "reasoning_content", "reasoning"):
        val = msg.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    return "\n".join(parts)


def probe_vision(api_base: str, api_key: Optional[str], model: str, *,
                 timeout: int = 45, browser_headers: Optional[dict] = None) -> dict:
    """Ask ``model`` at ``api_base`` to read the fixture image.

    Returns a dict:
      ``status``   — "vision" (read it), "no_vision" (answered without reading
                     it), or "error" (could not reach / not a chat shape).
      ``snippet``  — the first 200 chars of whatever came back.
      ``error``    — populated only for status == "error".

    Never raises: a probe must not break the caller.
    """
    base = (api_base or "").rstrip("/")
    if not base:
        return {"status": "error", "error": "no api_base for provider"}

    body = json.dumps({
        "model": model,
        "max_tokens": 120,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url",
                 "image_url": {"url": fixture_data_url()}},
            ],
        }],
    }).encode()

    headers = dict(browser_headers or {})
    headers.setdefault("Content-Type", "application/json")
    if api_key:
        headers["Authorization"] = "Bearer " + api_key

    req = urlrequest.Request(base + "/chat/completions", data=body, headers=headers)
    start = time.monotonic()
    try:
        ctx = ssl.create_default_context()
        with urlrequest.urlopen(req, timeout=timeout, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "error": "HTTP {} {}".format(exc.code, detail)}
    except Exception as exc:  # noqa: BLE001 — timeout, DNS, TLS, bad JSON
        return {"status": "error", "error": "{}: {}".format(type(exc).__name__, exc)}

    text = _extract_text(payload)
    duration_ms = round((time.monotonic() - start) * 1000)
    low = text.lower()
    if all(tok in low for tok in FIXTURE_TOKENS):
        return {"status": "vision", "snippet": text[:200], "duration_ms": duration_ms}
    return {"status": "no_vision", "snippet": text[:200], "duration_ms": duration_ms}
