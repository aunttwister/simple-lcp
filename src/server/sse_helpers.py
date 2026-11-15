"""SSE (Server-Sent Events) helper utilities."""

import json
from datetime import datetime, timezone

from ..api.cost_plugins.base import select_rates
from ..api.runtime import resolve_service


def extract_last_sse_chunk(raw_bytes):
    """Parse the last data chunk from an SSE response buffer."""
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
        last_data = None
        for line in text.split("\n"):
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str and data_str != "[DONE]":
                    last_data = json.loads(data_str)
        return last_data
    except Exception:
        return None


def extract_sse_text(raw_bytes) -> str:
    """Join the assistant text out of an SSE response buffer.

    Only ``choices[].delta.content`` string fragments are concatenated. Reasoning
    (``delta.reasoning_content``) and tool-call argument deltas are excluded on
    purpose: they are not what the agent said, and the conversation store records
    tool calls structurally instead of as prose.

    Never raises — a stream that cannot be parsed yields "", because this feeds
    bookkeeping rather than the response itself.
    """
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    parts: list[str] = []
    for line in text.split("\n"):
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            payload = json.loads(data_str)
        except (ValueError, TypeError):
            continue
        for choice in (payload.get("choices") or []):
            piece = (choice.get("delta") or {}).get("content")
            if isinstance(piece, str):
                parts.append(piece)
    return "".join(parts)


def extract_message_text(response_body) -> str:
    """The assistant text of a non-streamed completion (empty when absent)."""
    try:
        message = (response_body.get("choices") or [{}])[0].get("message") or {}
        content = message.get("content")
    except (AttributeError, IndexError, TypeError):  # noqa: BLE001
        return ""
    return content if isinstance(content, str) else ""


def extract_sse_tool_calls(raw_bytes) -> list:
    """Reassemble the assistant turn's tool calls out of an SSE response buffer.

    OpenAI streams a tool call in fragments: the first delta for an ``index``
    carries ``id``/``type`` and ``function.name``, and every later delta for that
    index appends a slice of ``function.arguments``. Fragments are keyed by
    ``index`` so parallel calls do not interleave into each other.

    The conversation store needs the whole call, not the argument prose: a tool
    RESULT carries only ``tool_call_id``, so the name can only come from the
    assistant turn — and that turn is not in the request body, it exists only in
    this stream.

    Never raises — an unparseable stream yields ``[]``, because this feeds
    bookkeeping rather than the response itself.
    """
    calls: dict[str, dict] = {}   # insertion-ordered: fragments arrive in call order
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    for line in text.split("\n"):
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            payload = json.loads(data_str)
        except (ValueError, TypeError):
            continue
        for choice in (payload.get("choices") or []):
            for frag in ((choice.get("delta") or {}).get("tool_calls") or []):
                if not isinstance(frag, dict):
                    continue
                idx = frag.get("index")
                # Key by the stream's index; a fragment streamed WITHOUT one is
                # still merged by its id, and a bare fragment starts a new call.
                key = str(idx) if isinstance(idx, int) else str(frag.get("id") or len(calls))
                call = calls.setdefault(key, {"id": None, "type": "function",
                                              "function": {"name": None, "arguments": ""}})
                if frag.get("id"):
                    call["id"] = frag["id"]
                if frag.get("type"):
                    call["type"] = frag["type"]
                fn = frag.get("function") or {}
                if isinstance(fn, dict):
                    if fn.get("name"):
                        call["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        call["function"]["arguments"] += fn["arguments"]
    return [c for c in calls.values() if c.get("id") and c["function"].get("name")]


def extract_message_tool_calls(response_body) -> list:
    """The assistant message's ``tool_calls`` of a non-streamed completion."""
    try:
        message = (response_body.get("choices") or [{}])[0].get("message") or {}
        calls = message.get("tool_calls")
    except (AttributeError, IndexError, TypeError):  # noqa: BLE001
        return []
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def estimate_cost_from_tokens(provider, model, cost_info, config):
    """Calculate cost from token counts using configured pricing or plugins.

    Never raises: a missing price records $0 rather than failing the response
    it was asked to account for. Uses the same registry-aware lookup as
    :func:`src.api.request_pipeline.calculate_cost`, so one price row covers
    every provider spelling of a model.
    """
    from ..api.cost_plugins import get_registry
    from ..api.request_pipeline import _lookup_pricing

    # Billing timestamp — DeepSeek bills peak vs off-peak by request time.
    request_ts = datetime.now(timezone.utc)

    # Try plugin registry first
    usage_for_plugin = {
        "prompt_tokens": cost_info.get("prompt_tokens", 0)
                        + cost_info.get("cache_miss_tokens", 0),
        "completion_tokens": cost_info.get("completion_tokens", 0),
        "prompt_cache_hit_tokens": cost_info.get("cache_hit_tokens", 0),
        "prompt_cache_miss_tokens": cost_info.get("cache_miss_tokens", 0),
        "__ts": request_ts,
    }
    plugin_cost = resolve_service("pricing", fallback=get_registry).calculate_cost(provider, model, usage_for_plugin)
    if plugin_cost is not None:
        return round(plugin_cost, 8)

    # Fall back to config-based pricing (registry-aware, never fatal).
    pricing = _lookup_pricing(config, provider, model)
    if pricing is None:
        return 0.0

    rates = select_rates(pricing, request_ts)
    cache_hit = cost_info.get("cache_hit_tokens", 0)
    cache_miss = cost_info.get("cache_miss_tokens", 0)
    output = cost_info.get("completion_tokens", 0)
    return round(
        (cache_hit / 1_000_000) * rates["cache_hit"]
        + (cache_miss / 1_000_000) * rates["cache_miss"]
        + (output / 1_000_000) * rates["output"],
        8,
    )
