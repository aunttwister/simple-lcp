"""Anthropic ``/messages`` wire format ↔ OpenAI chat-completions wire format.

Pure translation helpers — **no network, no DB, no filesystem**. The request
pipeline uses these to let an Anthropic-speaking model (``claude-*``) travel
through the same proxy path as every OpenAI-speaking one, so cost accounting,
the conversation store and every client keep seeing the OpenAI shape they
already handle.

The request pipeline is the only caller. Everything here is deterministic and
side-effect free so it can be unit-tested directly.

Measured contract (probed live against commandcode, 2026-11-02 — the vendor docs
are wrong about several of these):

* path      ``POST {base}/messages`` (``/chat/completions`` answers 403 for
  ``claude-*``)
* auth      ``x-api-key`` + ``anthropic-version: 2023-06-01`` (Bearer is
  rejected)
* ``temperature`` **must be omitted** — newer Claude rejects it with HTTP 400
  ``temperature is deprecated for this model``
* ``max_tokens`` is **required** (OpenAI treats it as optional)
* ``User-Agent`` — the same product UA the OpenAI path sends works. (An
  earlier probe with an invented UA returned a Cloudflare ``error code: 1010``
  that *reads* like a bad key, which is why this was investigated rather than
  assumed; verified 2026-11-02 that ``LLMControlPlane/1.0`` returns 200 on
  ``/messages``. See :data:`ANTHROPIC_USER_AGENT`.)
* response  ``{content:[{type:"text",text}], stop_reason,
  usage:{input_tokens,output_tokens}}``
"""

from __future__ import annotations

import codecs
import json
import time
from typing import Any, Iterable, Iterator

from .model_identity import normalise_model_string

__all__ = [
    "ANTHROPIC_API_VERSION",
    "ANTHROPIC_USER_AGENT",
    "ANTHROPIC_MESSAGES_PATH",
    "DEFAULT_MAX_TOKENS",
    "is_anthropic_model",
    "to_anthropic",
    "from_anthropic",
    "usage_to_openai",
    "sse_to_openai",
    "anthropic_error",
    "anthropic_finish_reason",
]

#: ``anthropic-version`` header value; required by the Messages API.
ANTHROPIC_API_VERSION = "2023-06-01"

#: User-Agent for the Anthropic branch. Defaults to the same product identity
#: the OpenAI path sends — verified by live probe (2026-11-02) against
#: commandcode's ``/messages``: ``LLMControlPlane/1.0`` returns HTTP 200, so no
#: special bench identity is needed. Operators can override per provider config
#: with ``user_agent``.
ANTHROPIC_USER_AGENT = "LLMControlPlane/1.0"

#: Path appended to the provider's base_url on the Anthropic wire.
ANTHROPIC_MESSAGES_PATH = "/messages"

#: ``max_tokens`` is mandatory on the Messages API but optional on the OpenAI
#: chat API. Used when the inbound body carries neither ``max_tokens`` nor
#: ``max_completion_tokens``.
DEFAULT_MAX_TOKENS = 4096

#: Model-id prefixes that speak the Anthropic wire format.
_ANTHROPIC_PREFIXES = ("claude", "anthropic")

#: Anthropic ``stop_reason`` -> OpenAI ``finish_reason``.
_FINISH_REASONS: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    "pause_turn": "stop",
}

#: Error codes callers/vendors use on the Anthropic wire.
_OVERLOADED = "overloaded_error"
_PERMISSION = "permission_error"
_AUTH = "authentication_error"
_RATE_LIMIT = "rate_limit_error"
_INVALID = "invalid_request_error"


# ── Model detection ─────────────────────────────────────────────────────────


def is_anthropic_model(model: Any) -> bool:
    """True when *model* is served over the Anthropic Messages wire format.

    Matches bare ids (``claude-sonnet-5``), vendor-prefixed ids
    (``anthropic/claude-sonnet-5``) and already-catalogued ids
    (``commandcode/claude-haiku-5-5``). Case-insensitive; anything that is not a
    non-empty string is not an Anthropic model.

    The string re-spelling (case-fold + vendor-prefix strip) is delegated to
    ``model_identity.normalise_model_string`` — the estate's single authority for
    reading a model string — rather than done here, which is both the
    architectural rule and what keeps the model canary green. What remains is a
    property of the id (its family prefix), not an identity decision: it never
    decides *which* model a string is, only which wire format can address it.
    """
    if not isinstance(model, str):
        return False
    normalised = normalise_model_string(model)
    if not normalised:
        return False
    candidates = [normalised]
    tail = normalised.rsplit("/", 1)[-1]
    if tail and tail != normalised:
        candidates.append(tail)
    return any(c.startswith(_ANTHROPIC_PREFIXES) for c in candidates)


# ── Request: OpenAI chat body -> Anthropic messages body ────────────────────


def to_anthropic(body: dict, model: str | None = None) -> dict:
    """Translate an OpenAI chat-completions body into an Anthropic messages body.

    * ``system``/``developer`` messages are hoisted into the top-level
      ``system`` string (OpenAI carries them in ``messages``; Anthropic rejects
      them there).
    * assistant ``tool_calls`` become ``tool_use`` content blocks and
      ``role="tool"`` messages become ``tool_result`` blocks (consecutive tool
      results are merged into ONE user turn, which is what Anthropic expects).
    * ``temperature`` is **dropped** — newer Claude answers 400
      ``temperature is deprecated for this model``.
    * ``max_tokens`` is guaranteed present, defaulting to
      :data:`DEFAULT_MAX_TOKENS`.
    * ``stream`` is preserved verbatim.

    The input dict is never mutated. Unknown/OpenAI-only parameters are dropped
    rather than forwarded, so the optional-parameter surface cannot 400 the
    request.
    """
    if not isinstance(body, dict):
        raise TypeError("to_anthropic() expects a mapping body")

    resolved_model = model or body.get("model") or ""

    out: dict[str, Any] = {"model": resolved_model}
    out["max_tokens"] = _resolve_max_tokens(body)

    system = _collect_system(body)
    if system:
        out["system"] = system

    out["messages"] = _to_anthropic_messages(body.get("messages"))

    tools = _to_anthropic_tools(body.get("tools"))
    if tools:
        out["tools"] = tools

    tool_choice = _to_anthropic_tool_choice(body.get("tool_choice"))
    if tool_choice is not None:
        out["tool_choice"] = tool_choice

    if "stream" in body:
        out["stream"] = bool(body.get("stream"))

    # Sampling knobs Anthropic actually understands.
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    if body.get("top_k") is not None:
        out["top_k"] = body["top_k"]

    stop_sequences = _to_stop_sequences(body.get("stop"))
    if stop_sequences:
        out["stop_sequences"] = stop_sequences

    metadata = body.get("metadata")
    if isinstance(metadata, dict) and metadata:
        out["metadata"] = metadata

    # NOTE: ``temperature`` (and the other OpenAI-only knobs: n, logprobs,
    # frequency_penalty, presence_penalty, response_format, stream_options,
    # parallel_tool_calls, reasoning_effort) are deliberately NOT copied. They
    # are either rejected outright or silently change nothing.
    return out


def _resolve_max_tokens(body: dict) -> int:
    """Return the output-token cap, guaranteeing a positive integer."""
    for key in ("max_tokens", "max_completion_tokens"):
        value = body.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return DEFAULT_MAX_TOKENS


def _collect_system(body: dict) -> str:
    """Join every system-level instruction into one Anthropic ``system`` string."""
    parts: list[str] = []

    top = body.get("system")
    if isinstance(top, str) and top:
        parts.append(top)
    elif isinstance(top, list):
        parts.extend(_text_from_parts(top))

    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") not in ("system", "developer"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content:
                parts.append(content)
        elif isinstance(content, list):
            parts.extend(_text_from_parts(content))

    return "\n\n".join(p for p in parts if p)


def _text_from_parts(parts: Iterable[Any]) -> list[str]:
    """Pull plain text out of a mixed OpenAI content-part list."""
    out: list[str] = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                out.append(text)
    return out


def _to_anthropic_messages(messages: Any) -> list[dict]:
    """Convert an OpenAI message list into Anthropic message turns."""
    out: list[dict] = []
    if not isinstance(messages, list):
        return out

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")

        if role in ("system", "developer"):
            continue  # hoisted into the top-level ``system`` field

        if role in ("tool", "function"):
            block = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or message.get("name") or ""),
                "content": _tool_result_content(message.get("content")),
            }
            if _is_tool_result_turn(out):
                # Consecutive tool results belong to ONE user turn.
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        target_role = "assistant" if role == "assistant" else "user"
        blocks: list[dict] = []

        content = message.get("content")
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            blocks.extend(_to_anthropic_content_parts(content))

        if target_role == "assistant":
            blocks.extend(_tool_calls_to_tool_use(message.get("tool_calls")))

        if not blocks:
            # Anthropic rejects empty content arrays.
            blocks = [{"type": "text", "text": ""}]

        out.append({"role": target_role, "content": blocks})

    return out


def _is_tool_result_turn(turns: list[dict]) -> bool:
    if not turns:
        return False
    last = turns[-1]
    if last.get("role") != "user":
        return False
    content = last.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _to_anthropic_content_parts(parts: list) -> list[dict]:
    """Translate OpenAI content parts (text / image_url) into Anthropic blocks."""
    out: list[dict] = []
    for part in parts:
        if isinstance(part, str):
            if part:
                out.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text" and isinstance(part.get("text"), str):
            out.append({"type": "text", "text": part["text"]})
        elif ptype == "image_url":
            image = _image_block(part)
            if image is not None:
                out.append(image)
        elif ptype in ("image", "tool_use", "tool_result", "document"):
            out.append(part)  # already Anthropic-shaped
        elif ptype in ("input_audio", "audio"):
            continue  # no Anthropic equivalent
        else:
            out.append(part)
    return out


def _image_block(part: dict) -> dict | None:
    """OpenAI ``image_url`` part -> Anthropic ``image`` block (url or base64)."""
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header[len("data:"):].split(";")[0] or "image/png"
        if not data:
            return None
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _tool_calls_to_tool_use(tool_calls: Any) -> list[dict]:
    """OpenAI assistant ``tool_calls`` -> Anthropic ``tool_use`` blocks."""
    out: list[dict] = []
    if not isinstance(tool_calls, list):
        return out
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict):
            function = {}
        name = function.get("name") or call.get("name") or ""
        if not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments) if arguments.strip() else {}
            except (ValueError, TypeError):
                parsed = {}
        elif isinstance(arguments, dict):
            parsed = arguments
        else:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        out.append({
            "type": "tool_use",
            "id": str(call.get("id") or call.get("tool_call_id") or ""),
            "name": str(name),
            "input": parsed,
        })
    return out


def _tool_result_content(content: Any) -> Any:
    """Anthropic tool_result content: a string, or a list of text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = _to_anthropic_content_parts(content)
        return blocks or [{"type": "text", "text": ""}]
    if content is None:
        return ""
    if isinstance(content, (dict, int, float, bool)):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _to_anthropic_tools(tools: Any) -> list[dict]:
    """OpenAI tool envelopes -> Anthropic tool definitions."""
    out: list[dict] = []
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            name = function.get("name")
            if not name:
                continue
            definition: dict[str, Any] = {"name": str(name)}
            if function.get("description"):
                definition["description"] = function["description"]
            parameters = function.get("parameters")
            definition["input_schema"] = (
                parameters if isinstance(parameters, dict)
                else {"type": "object", "properties": {}}
            )
            out.append(definition)
        elif tool.get("name"):
            out.append(tool)  # already Anthropic-shaped
    return out


def _to_anthropic_tool_choice(choice: Any) -> dict | None:
    """OpenAI ``tool_choice`` -> Anthropic ``tool_choice`` (None = omit)."""
    if choice is None:
        return None
    if isinstance(choice, str):
        lowered = choice.strip().lower()
        if lowered == "auto":
            return {"type": "auto"}
        if lowered in ("required", "any"):
            return {"type": "any"}
        if lowered == "none":
            return {"type": "none"}
        return None
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            function = choice.get("function") or {}
            name = function.get("name") if isinstance(function, dict) else None
            if name:
                return {"type": "tool", "name": str(name)}
            return None
        if choice.get("type") in ("auto", "any", "none", "tool"):
            return choice
    return None


def _to_stop_sequences(stop: Any) -> list[str]:
    """OpenAI ``stop`` (str | list[str]) -> Anthropic ``stop_sequences``."""
    if isinstance(stop, str) and stop:
        return [stop]
    if isinstance(stop, list):
        return [s for s in stop if isinstance(s, str) and s]
    return []


# ── Response: Anthropic message -> OpenAI chat completion ───────────────────


def anthropic_finish_reason(stop_reason: Any) -> str:
    """Anthropic ``stop_reason`` -> OpenAI ``finish_reason``.

    ``end_turn``/``stop_sequence`` -> ``stop``, ``max_tokens`` -> ``length``,
    ``tool_use`` -> ``tool_calls``. Unknown/absent values fall back to ``stop``.
    """
    if isinstance(stop_reason, str):
        return _FINISH_REASONS.get(stop_reason, "stop")
    return "stop"


def from_anthropic(resp: dict, model: str | None = None) -> dict:
    """Translate an Anthropic message response into an OpenAI-shaped response.

    Every existing consumer of ``forward_request`` (cost accounting, the
    conversation store, streaming clients) reads the OpenAI shape, so this is
    the compatibility boundary for the non-streamed path.
    """
    if not isinstance(resp, dict):
        raise TypeError("from_anthropic() expects a mapping response")

    texts: list[str] = []
    tool_calls: list[dict] = []

    content = resp.get("content")
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                texts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif btype == "tool_use":
                payload = block.get("input")
                if payload is None:
                    payload = {}
                tool_calls.append({
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(payload, ensure_ascii=False),
                    },
                })
            # thinking / redacted_thinking blocks carry no user-visible content.

    message: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": resp.get("id") or "",
        "object": "chat.completion",
        "created": int(resp.get("created") or time.time()),
        "model": model or resp.get("model") or "",
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": anthropic_finish_reason(resp.get("stop_reason")),
        }],
        "usage": usage_to_openai(resp.get("usage")),
    }


def usage_to_openai(usage: Any) -> dict:
    """Anthropic token usage -> OpenAI ``usage`` block.

    ``input_tokens``/``output_tokens`` -> ``prompt_tokens``/``completion_tokens``
    (+ ``total_tokens``). Anthropic's prompt-cache counters are passed through
    under their native names because ``read_cache_hit_tokens`` already knows
    ``cache_read_input_tokens`` — dropping them would score every cached token
    as a miss (the ~10x cost overstatement LCP fixed for the other providers).
    """
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    prompt = _as_int(usage.get("input_tokens"))
    completion = _as_int(usage.get("output_tokens"))
    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        value = _as_int(usage.get(key))
        if value:
            out[key] = value
    return out


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── Errors ─────────────────────────────────────────────────────────────────


def anthropic_error(status: int, body: str) -> tuple[str, str, str]:
    """Map an Anthropic error response to ``(friendly, code, error_type)``.

    Codes line up with what callers already handle:
    ``authentication_error`` (401), ``MODEL_NOT_IN_PLAN``/``permission_error``
    (403 plan-gated model), ``rate_limit_error`` (429), ``overloaded_error``
    (529, retryable) and ``invalid_request_error`` for everything else 4xx.

    Falls back to the raw body when the response is not an Anthropic envelope.
    """
    message = (body or "")[:300]
    code = ""
    etype = ""
    try:
        data = json.loads(body)
    except Exception:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            message = err.get("message") or message
            etype = str(err.get("type") or "")
            code = str(err.get("code") or "")
        elif isinstance(err, str) and err:
            message = err
    if not isinstance(message, str) or not message.strip():
        message = f"HTTP {status}"

    low = message.lower()

    if etype == _OVERLOADED or code == _OVERLOADED or status == 529 \
            or "overloaded" in low:
        return (
            "Provider is overloaded (Anthropic 529) — retry in a moment.",
            code or _OVERLOADED, _OVERLOADED,
        )
    if status == 401 or etype == _AUTH or code == _AUTH or "invalid api key" in low \
            or "x-api-key" in low:
        return (
            "Authentication failed — check your API key.",
            code or _AUTH, etype or _AUTH,
        )
    if code == "upgrade_required" or "upgrade_required" in low or "provider plan" in low:
        return (
            "API access requires the Command Code Provider plan or higher.",
            "upgrade_required", etype or _PERMISSION,
        )
    if code == "MODEL_NOT_IN_PLAN" or "not in plan" in low \
            or etype == _PERMISSION or code == _PERMISSION \
            or "upgrade" in low or "plan" in low:
        return (
            f"Model not available on your current plan: {message}. "
            f"Pick another model, upgrade your plan, or use extra on-demand usage.",
            code or "MODEL_NOT_IN_PLAN", etype or _PERMISSION,
        )
    if status == 429 or etype == _RATE_LIMIT or code == _RATE_LIMIT \
            or "rate limit" in low:
        return (
            "Rate limited by the provider — retry in a moment.",
            code or _RATE_LIMIT, etype or _RATE_LIMIT,
        )
    if status == 403 or etype == _PERMISSION or code == _PERMISSION:
        return (
            f"Model not available on your current plan: {message}. "
            f"Pick another model, upgrade your plan, or use extra on-demand usage.",
            code or "MODEL_NOT_IN_PLAN", etype or _PERMISSION,
        )
    return (message, code or _INVALID, etype or _INVALID)


# ── Streaming: Anthropic SSE -> OpenAI SSE ─────────────────────────────────


def sse_to_openai(chunks: Iterable[Any]) -> Iterator[bytes]:
    """Translate an Anthropic SSE byte stream into an OpenAI SSE byte stream.

    Streams incrementally: input is decoded with an incremental UTF-8 decoder
    and parsed line-by-line as it arrives, so a split multi-byte character or
    an event straddling a chunk boundary is handled without ever buffering the
    whole response (that matters — LCP relays long completions).

    * ``content_block_delta`` text deltas -> ``choices[0].delta.content``
    * ``thinking_delta`` -> ``choices[0].delta.reasoning_content`` (same shape
      LCP already stores for DeepSeek thinking mode)
    * ``message_delta`` usage -> a final chunk carrying ``usage`` (this is what
      ``extract_last_sse_chunk`` reads for streaming cost accounting)
    * ``message_stop`` -> ``data: [DONE]``
    """
    state: dict[str, Any] = {
        "id": "",
        "model": "",
        "created": int(time.time()),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "stop_reason": None,
        "tool_seq": 0,
        "tool_seq_by_block": {},
        "done": False,
    }
    buffer = ""

    for text in _iter_text(chunks):
        buffer += text
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line.startswith("data:"):
                # ``event:`` / comment / blank separator lines carry no payload.
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                state["done"] = True
                yield b"data: [DONE]\n\n"
                continue
            try:
                event = json.loads(payload)
            except (ValueError, TypeError):
                continue  # partial/garbled data line: skip, never fail the relay
            if not isinstance(event, dict):
                continue
            yield from _translate_event(event, state)

    if not state["done"]:
        # Upstream closed without ``message_stop`` — still terminate the client
        # stream cleanly rather than leaving it hanging.
        yield b"data: [DONE]\n\n"


def _iter_text(chunks: Iterable[Any]) -> Iterator[str]:
    """Yield decoded text incrementally, surviving chunk-boundary splits."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    for chunk in chunks:
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            text = decoder.decode(bytes(chunk))
        elif isinstance(chunk, str):
            text = chunk
        else:
            continue
        if text:
            yield text
    tail = decoder.decode(b"", True)
    if tail:
        yield tail


def _sse_line(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")


def _chunk(state: dict, delta: dict, finish_reason: Any = None,
           usage: dict | None = None) -> bytes:
    payload: dict[str, Any] = {
        "id": state["id"] or "chatcmpl-anthropic",
        "object": "chat.completion.chunk",
        "created": state["created"],
        "model": state["model"] or "",
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    if usage is not None:
        payload["usage"] = usage
    return _sse_line(payload)


def _state_usage(state: dict) -> dict:
    usage = {
        "prompt_tokens": state["input_tokens"],
        "completion_tokens": state["output_tokens"],
        "total_tokens": state["input_tokens"] + state["output_tokens"],
    }
    if state["cache_read"]:
        usage["cache_read_input_tokens"] = state["cache_read"]
    if state["cache_creation"]:
        usage["cache_creation_input_tokens"] = state["cache_creation"]
    return usage


def _absorb_usage(state: dict, usage: Any, *, cumulative_output: bool) -> None:
    if not isinstance(usage, dict):
        return
    if "input_tokens" in usage:
        state["input_tokens"] = _as_int(usage.get("input_tokens"))
    if "output_tokens" in usage:
        # message_delta reports cumulative output tokens; message_start reports
        # the (usually tiny) count already emitted. Take the max so a partial
        # message_start value can never walk the total backwards.
        value = _as_int(usage.get("output_tokens"))
        state["output_tokens"] = value if cumulative_output else max(state["output_tokens"], value)
    if "cache_read_input_tokens" in usage:
        state["cache_read"] = _as_int(usage.get("cache_read_input_tokens"))
    if "cache_creation_input_tokens" in usage:
        state["cache_creation"] = _as_int(usage.get("cache_creation_input_tokens"))


def _translate_event(event: dict, state: dict) -> Iterator[bytes]:
    """Translate one Anthropic SSE event into zero or more OpenAI SSE records."""
    etype = event.get("type")

    if etype == "message_start":
        message = event.get("message") or {}
        if isinstance(message, dict):
            state["id"] = message.get("id") or state["id"]
            state["model"] = message.get("model") or state["model"]
            _absorb_usage(state, message.get("usage"), cumulative_output=False)
        yield _chunk(state, {"role": "assistant", "content": ""})
        return

    if etype == "content_block_start":
        block = event.get("content_block") or {}
        if not isinstance(block, dict):
            return
        if block.get("type") == "tool_use":
            seq = state["tool_seq"]
            state["tool_seq"] = seq + 1
            state["tool_seq_by_block"][event.get("index", seq)] = seq
            yield _chunk(state, {"tool_calls": [{
                "index": seq,
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {"name": str(block.get("name") or ""), "arguments": ""},
            }]})
        elif isinstance(block.get("text"), str) and block["text"]:
            yield _chunk(state, {"content": block["text"]})
        return

    if etype == "content_block_delta":
        delta = event.get("delta") or {}
        if not isinstance(delta, dict):
            return
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                yield _chunk(state, {"content": text})
        elif dtype == "thinking_delta":
            text = delta.get("thinking")
            if isinstance(text, str) and text:
                yield _chunk(state, {"reasoning_content": text})
        elif dtype == "input_json_delta":
            partial = delta.get("partial_json")
            if isinstance(partial, str) and partial:
                seq = state["tool_seq_by_block"].get(event.get("index", 0), 0)
                yield _chunk(state, {"tool_calls": [{
                    "index": seq,
                    "function": {"arguments": partial},
                }]})
        return

    if etype == "message_delta":
        delta = event.get("delta") or {}
        if isinstance(delta, dict):
            if delta.get("stop_reason"):
                state["stop_reason"] = delta["stop_reason"]
        _absorb_usage(state, event.get("usage"), cumulative_output=True)
        usage = _state_usage(state)
        if usage["total_tokens"] == 0 and "usage" not in event:
            usage = None  # nothing to report — don't overwrite estimate-based cost
        yield _chunk(state, {}, anthropic_finish_reason(state["stop_reason"]), usage)
        return

    if etype == "message_stop":
        state["done"] = True
        yield b"data: [DONE]\n\n"
        return

    if etype == "error":
        error = event.get("error")
        if not isinstance(error, dict):
            error = {"type": _INVALID, "message": str(error or "provider error")}
        yield _sse_line({"error": error})
        state["done"] = True
        yield b"data: [DONE]\n\n"
        return

    # ping / content_block_stop / anything new: nothing to relay.
