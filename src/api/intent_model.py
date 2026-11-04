"""Remote intent decision model — THE classifier (CUR-327).

The local bge-small semantic classifier is OBSOLETED from the decision path.
Intent classification is a call to a configurable HTTP endpoint; the contract
(what LCP sends, what it expects back) is declared here and surfaced in the
config UI.

Contract ``lcp-v1``
-------------------
Request  — ``POST <url>``, ``Content-Type: application/json``, and
``Authorization: Bearer <api_key>`` when a key is configured::

    {
      "message": "<the newest genuine user instruction>",
      "labels": ["change", "inspect", "advise"],
      "profile": "l2",
      "taxonomy_id": "builtin-v0",
      "taxonomy_version": 1
    }

Response::

    { "label": "change", "score": 0.87,
      "scores": { "change": 0.87, "inspect": 0.09, "advise": 0.04 } }

``label`` MUST be one of the sent ``labels`` (a foreign label is rejected and
the caller falls through to the structural path); ``score`` is 0–1; ``scores``
is optional but recorded when present.

Contract ``openai``
-------------------
Any OpenAI-compatible ``chat.completions`` endpoint: LCP takes
``choices[0].message.content`` and requires it to name exactly one of the
labels (surrounding prose / JSON wrapping tolerated); score is 1.0 unless a
0–1 number is extractable from the content.

Contract ``jev``
----------------
Jev / SystemOne shape — reserved, NOT implemented: selecting it returns an
unsupported result until a real jev endpoint exists to measure the field
names against. Guessing the wire format would be worse than refusing.

Every call is timeout-bounded and exception-guarded: a failure returns None
and the caller falls to the structural degraded path. This module sits in the
request path and must never raise.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .logging_config import get_logger

logger = get_logger("lcp.router.intent_model")

# The formats LCP speaks. ``jev`` is a deliberate stub — see the module docstring.
FORMATS = ("lcp-v1", "openai", "jev")
DEFAULT_FORMAT = "lcp-v1"
DEFAULT_TIMEOUT_MS = 3000


@dataclass
class IntentModelResult:
    """A successful remote classification, already validated against the label space."""

    label: str
    score: float
    scores: Optional[dict] = None       # label -> score, when the endpoint sent them
    endpoint: str = ""                  # the URL that answered (for the decision record)
    format: str = DEFAULT_FORMAT
    raw: Optional[str] = None           # the unparsed content, for evidence

    def semantic_pairs(self) -> list:
        """The ``semantic`` shape ``ClassifyResult`` records: top-N (label, score)."""
        if self.scores:
            return [(k, round(float(v), 4)) for k, v in
                    sorted(self.scores.items(), key=lambda kv: -kv[1])]
        return [(self.label, round(self.score, 4))]


def _reject(reason: str, **kw) -> None:
    """Log a rejection and return None — the caller degrades; nothing raises."""
    logger.info("intent_model_rejected", reason=reason, **kw)


def _post_json(url: str, payload: dict, api_key: Optional[str],
               timeout_s: float) -> tuple:
    """POST *payload* and return ``(status, body_text)``. Raises on transport errors."""
    import urllib.request
    import urllib.error

    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _parse_lcp_v1(body: str, labels: list) -> Optional[IntentModelResult]:
    """Parse the native ``lcp-v1`` response."""
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        _reject("lcp_v1_body_not_json", body_head=body[:200])
        return None
    label = doc.get("label") or doc.get("task") or doc.get("intent")
    if not isinstance(label, str) or label not in labels:
        _reject("lcp_v1_label_not_in_label_space", label=label if isinstance(label, str) else None)
        return None
    try:
        score = float(doc.get("score", 1.0))
    except (TypeError, ValueError):
        score = 1.0
    score = min(1.0, max(0.0, score))
    scores = doc.get("scores") if isinstance(doc.get("scores"), dict) else None
    return IntentModelResult(label=label, score=score, scores=scores,
                             format="lcp-v1", raw=body[:2000])


_LABEL_TOKEN = re.compile(r"[A-Za-z0-9_\-]+")


def _parse_openai(body: str, labels: list) -> Optional[IntentModelResult]:
    """Parse an OpenAI-compatible chat.completions body."""
    try:
        doc = json.loads(body)
        content = doc["choices"][0]["message"]["content"]
    except (ValueError, TypeError, KeyError, IndexError):
        _reject("openai_body_missing_choices_content", body_head=body[:200])
        return None
    if not isinstance(content, str) or not content.strip():
        _reject("openai_content_empty")
        return None
    # The label must APPEAR as a standalone token; prose that merely contains a
    # label substring ("unchanged" contains "change") must not match.
    tokens = set(_LABEL_TOKEN.findall(content))
    hits = [lab for lab in labels if lab in tokens]
    if len(hits) != 1:
        _reject("openai_content_not_exactly_one_label", hits=hits[:5])
        return None
    m = re.search(r"\b(0\.\d+|1\.0|0)\b", content)
    score = min(1.0, max(0.0, float(m.group(1)))) if m else 1.0
    return IntentModelResult(label=hits[0], score=score, format="openai",
                             raw=content[:2000])


def _build_openai_payload(message: str, labels: list, model: Optional[str]) -> dict:
    """A chat.completions request that asks for exactly one label back."""
    prompt = (
        "Classify the following message into exactly one of these intents: "
        + ", ".join(labels)
        + ". Reply with the single intent name and nothing else.\n\nMessage: "
        + message.strip()
    )
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 16,
    }
    if model:
        payload["model"] = model
    return payload


def classify_remote(message: str, labels: list, url: str,
                    fmt: str = DEFAULT_FORMAT,
                    api_key: Optional[str] = None,
                    timeout_ms: int = DEFAULT_TIMEOUT_MS,
                    profile: Optional[str] = None,
                    taxonomy_id: Optional[str] = None,
                    taxonomy_version: Optional[int] = None,
                    min_score: float = 0.0,
                    model: Optional[str] = None) -> Optional[IntentModelResult]:
    """Classify *message* against the remote intent model. None on ANY failure.

    This is the request-path entry point. It validates the answer against
    *labels*, applies *min_score*, and never raises. For ``openai`` the URL is
    the full chat-completions URL and *model* names the served model; for
    ``lcp-v1`` the URL is the intent endpoint and the body is the contract
    documented in the module docstring.
    """
    if not (url and message and message.strip() and labels):
        return None
    if fmt == "jev":
        _reject("jev_format_unsupported_until_measured")
        return None
    if fmt not in FORMATS:
        _reject("unknown_format", fmt=fmt)
        return None

    if fmt == "openai":
        payload = _build_openai_payload(message, labels, model)
    else:
        payload = {
            "message": message.strip(),
            "labels": list(labels),
            "profile": profile,
            "taxonomy_id": taxonomy_id,
            "taxonomy_version": taxonomy_version,
        }
    try:
        status, body = _post_json(url, payload, api_key, timeout_ms / 1000.0)
    except Exception as exc:  # noqa: BLE001 — the request path must not break
        logger.warning("intent_model_call_failed", url=url, error=str(exc)[:300])
        return None
    if status != 200:
        _reject("non_200", status=status)
        return None

    res = _parse_lcp_v1(body, labels) if fmt == "lcp-v1" else _parse_openai(body, labels)
    if res is None:
        return None
    res.endpoint = url
    if res.score < min_score:
        _reject("below_min_score", score=res.score, min_score=min_score)
        return None
    logger.info("intent_model_classified", label=res.label, score=round(res.score, 4),
                endpoint=url, fmt=fmt, profile=profile)
    return res
