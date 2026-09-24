"""Request pipeline — chat completion lifecycle.

Handles the full request flow:
  auth → strip tools → cache check → try provider chain → calculate cost → record
"""

import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, TypeVar

from .circuit_breaker import get_circuit_breaker
from .cost_plugins import get_registry
from .runtime import resolve_service
from .exceptions import (
    AllProvidersFailedError,
    ConfigError,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderCreditsError,
    ProviderInternalError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    RequestTooLargeError,
)
from .logging_config import get_logger
from .models import get_session, Request as RequestModel
from .router import get_dynamic_router

logger = get_logger("lcp.pipeline")

# Generic return type for call_with_retry — it returns whatever request_fn returns.
T = TypeVar("T")

# Upstream socket read timeout for a single provider relay call.
# Hardcoded 180s used to kill legitimate long cache-MISS requests (Hermes
# context-compression summaries ~200k tokens can take 8-9+ min through the
# opencode relay before the first byte). Make it configurable so operators
# can tune per deployment; 900s default covers observed compression runs.
# NOTE: read timeout is per socket read — streaming requests (SSE) are
# unaffected because each 8KB chunk read resets the window.
_UPSTREAM_READ_TIMEOUT_DEFAULT = 900.0
_UPSTREAM_READ_TIMEOUT_ENV = "LCP_UPSTREAM_READ_TIMEOUT"


def _upstream_read_timeout() -> float:
    """Return the upstream relay read timeout (seconds).

    Reads ``LCP_UPSTREAM_READ_TIMEOUT`` from the environment; falls back to
    900.0 when unset or invalid. Clamped to a sane lower bound.
    """
    raw = os.environ.get(_UPSTREAM_READ_TIMEOUT_ENV, "")
    try:
        value = float(raw) if raw else _UPSTREAM_READ_TIMEOUT_DEFAULT
    except (TypeError, ValueError):
        value = _UPSTREAM_READ_TIMEOUT_DEFAULT
    return max(value, 30.0)

# Substrings that identify an insufficient-balance / out-of-credits response
# from a provider. Detected in the HTTP error body regardless of status code.
_CREDITS_MARKERS = (
    "creditserror",
    "insufficient balance",
    "insufficient funds",
    "insufficient credits",
    "out of credits",
    "no credits",
    "billing",
    "payment required",
)


def _is_credits_error(error_body: str, status: int) -> bool:
    """Return True when a provider error body indicates an out-of-credits state.

    The opencode API returns a ``CreditsError`` (often with an HTTP 401/403 or
    402) when the workspace balance is exhausted. These should trip the circuit
    breaker rather than be treated as a plain auth failure or a bad request.
    """
    body = (error_body or "").lower()
    if any(marker in body for marker in _CREDITS_MARKERS):
        return True
    # HTTP 402 Payment Required is the canonical "insufficient balance" status.
    return status == 402


def _is_bare_4xx(error_body: str) -> bool:
    """True when a 4xx error body carries no descriptive error message.

    A bare 4xx (empty body, or JSON with no ``error``/``message`` field) is
    ambiguous — we cannot conclude the request body is the problem. Providers
    sometimes return a minimal envelope (e.g. opencode's
    ``{"object":"error","model":"deepseek-v4-flash"}``) for transient or
    content-specific rejections. Treating these as fatal bad requests aborts
    the whole chain even when healthy fallbacks exist; instead they should
    fall through to the next provider.

    A body that DOES carry an ``error`` or ``message`` field (e.g.
    ``{"error":"bad"}`` or ``{"error":{"message":"..."}})`` is NOT bare — it
    describes the problem, so it stays a fatal bad request.
    """
    body = (error_body or "").strip()
    if not body:
        return True
    try:
        data = json.loads(body)
    except Exception:
        # Non-JSON body: bare only when trivially short (no real message);
        # otherwise it likely carries a human-readable reason.
        return len(body) < 20
    if isinstance(data, dict):
        if "error" in data or "message" in data:
            return False
        return True
    return False


# ── Tool Stripping ───────────────────────────────────────────────────────────

def strip_forbidden_tools(body: dict, forbidden: list[str] | None) -> tuple[dict, list[str]]:
    """Remove forbidden tools from the request body. Returns (modified_body, blocked_tools)."""
    if forbidden is None:
        # ALL tools forbidden — strip everything
        blocked = []
        if "tools" in body and body["tools"]:
            blocked = [t.get("function", {}).get("name", "unknown") for t in body["tools"]]
            body["tools"] = []
        return body, blocked

    if not forbidden or "tools" not in body or not body["tools"]:
        return body, []

    blocked = []
    kept = []
    for tool in body["tools"]:
        name = tool.get("function", {}).get("name", "")
        if name in forbidden:
            blocked.append(name)
        else:
            kept.append(tool)

    body["tools"] = kept
    return body, blocked


# ── Prefix Cache Normalization ───────────────────────────────────────────────

def normalize_messages_for_cache(messages: list[dict]) -> list[dict]:
    """Make the cacheable prefix deterministic across requests.

    DeepSeek/OpenAI cache from token 0. Any difference in the prefix
    (trailing whitespace, different tool ordering) breaks the cache.
    This normalizes so identical logical prompts produce identical
    token sequences — maximizing cache-hit rate.

    IMPORTANT: Preserves tool_call_id on tool messages — providers require it.
    IMPORTANT: Preserves reasoning_content on assistant messages — DeepSeek
    thinking mode requires this field to be passed back in conversation history.
    """
    normalized = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system" and isinstance(content, str):
            content = content.rstrip()
        if role == "tool":
            # Preserve tool_call_id — providers require it for validation
            normalized.append({
                "role": role,
                "content": content,
                "tool_call_id": msg.get("tool_call_id", ""),
            })
        elif role == "assistant" and msg.get("tool_calls"):
            # Preserve tool_calls on assistant messages so sanitize_messages()
            # can match them to tool responses. Without this, normalize strips
            # tool_calls → sanitize orphans all tool messages → conversation
            # collapses to flat user/assistant history → provider sees no tool
            # context and stops responding mid-stream.
            entry = {
                "role": role,
                "content": content,
                "tool_calls": msg["tool_calls"],
            }
            if "reasoning_content" in msg:
                entry["reasoning_content"] = msg["reasoning_content"]
            if msg.get("name"):
                entry["name"] = msg["name"]
            normalized.append(entry)
        else:
            entry = {"role": role, "content": content}
            # DeepSeek thinking mode requires reasoning_content to be passed
            # back in subsequent requests — omit it and get HTTP 400.
            # Must include the field even when empty (falsy), because providers
            # (especially OpenCode's proxy) validate field presence, not the value.
            if role == "assistant" and "reasoning_content" in msg:
                entry["reasoning_content"] = msg["reasoning_content"]
            if msg.get("name"):
                entry["name"] = msg["name"]
            normalized.append(entry)
    return normalized


def normalize_tools_for_cache(tools: list[dict]) -> list[dict]:
    """Sort tool definitions by function name for deterministic ordering."""
    if not tools:
        return tools
    return sorted(tools, key=lambda t: t.get("function", {}).get("name", ""))


def has_image_content(messages: list[dict]) -> bool:
    """Return True if any message contains an image_url content block."""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "image_url":
                    return True
    return False


# ── Message Sanitization ─────────────────────────────────────────────────────

def capture_reasoning_from_response(response_body: dict) -> None:
    """Capture reasoning_content from a non-streaming provider response.

    DeepSeek returns ``choices[0].message.reasoning_content`` alongside
    ``tool_calls``. We store it keyed by tool_call_id so a later request whose
    client stripped reasoning_content can be rehydrated with the real content.
    Best-effort; never raises.
    """
    try:
        message = (response_body or {}).get("choices", [{}])[0].get("message", {})
        reasoning = message.get("reasoning_content")
        tool_calls = message.get("tool_calls") or []
        ids = [tc.get("id", tc.get("tool_call_id")) for tc in tool_calls
               if tc.get("id", tc.get("tool_call_id"))]
        if ids and reasoning:
            from .reasoning_store import get_reasoning_store
            resolve_service("reasoning_store", fallback=get_reasoning_store).capture(ids, reasoning)
    except Exception:
        pass


def capture_reasoning_from_sse(raw_bytes: bytes) -> None:
    """Capture reasoning_content from a streaming (SSE) provider response.

    SSE deltas carry ``reasoning_content`` and ``tool_calls`` across chunks.
    We accumulate reasoning deltas and pair them with any tool_call ids seen
    in the same response, storing the real content keyed by tool_call_id.
    Best-effort; never raises.
    """
    try:
        text = raw_bytes.decode("utf-8", errors="replace")
        reasoning_buf: list[str] = []
        tool_ids: list[str] = []
        for line in text.split("\n"):
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                obj = json.loads(data_str)
            except Exception:
                continue
            for choice in obj.get("choices", []):
                delta = choice.get("delta") or {}
                rc = delta.get("reasoning_content")
                if rc:
                    reasoning_buf.append(rc)
                for tc in delta.get("tool_calls") or []:
                    tc_id = tc.get("id", tc.get("tool_call_id"))
                    if tc_id:
                        tool_ids.append(tc_id)
        if tool_ids and reasoning_buf:
            from .reasoning_store import get_reasoning_store
            resolve_service("reasoning_store", fallback=get_reasoning_store).capture(tool_ids, "".join(reasoning_buf))
    except Exception:
        pass


def ensure_thinking_reasoning_content(messages: list[dict], model: str,
                                      config) -> list[dict]:
    """Ensure tool-calling assistant turns carry a ``reasoning_content`` key.

    Per the official DeepSeek thinking-mode docs:
      - For requests carrying the ``tools`` parameter, ``reasoning_content``
        must be passed back on assistant messages that performed a tool call.
        Omitting it returns HTTP 400: "The `reasoning_content` in the thinking
        mode must be passed back to the API."
      - For assistant turns WITHOUT a tool call, ``reasoning_content`` is
        ignored by the API — no injection needed.

    The gateway preserves ``reasoning_content`` when the client sends it, but
    clients (agents / Copilot) often strip it when rebuilding multi-turn
    history. Two recovery layers:
      1. Rehydrate from the reasoning store — LCP remembers the real content
         keyed by tool_call_id (captured when the provider returned it) and
         re-attaches it, so the genuine chain-of-thought is passed back.
      2. Fallback: inject an empty string as a presence placeholder when no
         stored content exists, since DeepSeek's validation is on field
         presence for this error path.

    Only applied when the target model declares ``supports_thinking: true`` in
    ``model_limits`` (e.g. deepseek-v4-pro / deepseek-v4-flash).
    """
    try:
        limits = config.get_model_limits(model) if hasattr(config, "get_model_limits") else None
        supports_thinking = bool((limits or {}).get("supports_thinking", False))
    except Exception:
        supports_thinking = False
    if not supports_thinking:
        return messages

    # Layer 1: reattach genuinely captured reasoning content (by tool_call_id)
    try:
        from .reasoning_store import get_reasoning_store
        messages = resolve_service("reasoning_store", fallback=get_reasoning_store).rehydrate(messages)
    except Exception:
        pass

    # Layer 2: presence fallback for any tool-call turn still missing it
    for msg in messages:
        # Docs: only tool-calling assistant turns require reasoning_content.
        if msg.get("role") == "assistant" and msg.get("tool_calls") \
                and "reasoning_content" not in msg:
            msg["reasoning_content"] = ""
    return messages


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Fix malformed conversation histories before forwarding to providers.

    Two cases handled:
      1. Dangling assistant tool_calls (no matching tool response) ->
         remove the tool_calls AND the orphaned tool responses.
      2. Orphaned tool messages (tool_call_id not declared by any assistant) ->
         remove the orphaned tool messages.

    This prevents deepseek 400 errors:
      - 'missing field tool_call_id' (tool msg without id)
      - 'tool must be a response to preceding tool_calls' (tool msg with no
        assistant that declared the call)
    """
    if not messages:
        return messages

    # Build set of all tool_call_ids declared by assistant messages
    declared_ids = set()
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", tc.get("tool_call_id"))
                if tc_id:
                    declared_ids.add(tc_id)

    # Build set of tool_call_ids referenced by tool messages
    referenced_ids = set()
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            referenced_ids.add(msg["tool_call_id"])

    # Dangling = declared by assistant but never answered by tool msg
    dangling_declared = declared_ids - referenced_ids
    # Orphaned = referenced by tool msg but never declared by assistant
    orphaned_referenced = referenced_ids - declared_ids

    if not dangling_declared and not orphaned_referenced:
        return messages

    ids_to_remove = dangling_declared | orphaned_referenced

    sanitized = []
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id") in ids_to_remove:
            # Convert orphaned tool response to user message (preserve context)
            sanitized.append({"role": "user", "content": msg.get("content", "")})
            continue
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            kept_calls = [
                tc for tc in msg["tool_calls"]
                if tc.get("id", tc.get("tool_call_id")) not in ids_to_remove
            ]
            if kept_calls:
                sanitized.append({**msg, "tool_calls": kept_calls})
            elif msg.get("content"):
                sanitized.append({**msg, "tool_calls": None})
            # else: drop assistant msg that only had dangling tool_calls
        else:
            sanitized.append(msg)

    logger.warning(
        "messages_sanitized",
        dangling_declared=len(dangling_declared),
        orphaned_referenced=len(orphaned_referenced),
        converted_to_user=len(orphaned_referenced),
        removed_ids=len(ids_to_remove),
        original_len=len(messages),
        sanitized_len=len(sanitized),
    )
    return sanitized


# ── Cost Calculation ─────────────────────────────────────────────────────────

def read_cache_hit_tokens(provider_name: str, response_body: dict | None,
                          config) -> int:
    """Read cache-hit tokens from provider response, using configured field name.

    Falls back to the standard 'prompt_cache_hit_tokens' field when the config
    doesn't declare a provider-specific field (or when called from tests with
    a minimal mock config).
    """
    if response_body is None:
        return 0
    usage = response_body.get("usage", {})
    field = "prompt_cache_hit_tokens"  # default for DeepSeek/OpenAI
    if hasattr(config, "get_provider_cache_config"):
        cc = config.get_provider_cache_config(provider_name)
        if isinstance(cc, dict) and cc.get("hit_field"):
            field = cc["hit_field"]
    return usage.get(field, 0)


def calculate_cost(provider: str, model: str, body: dict, response_body: dict | None,
                   config) -> dict:
    """Calculate token usage and cost from request+response.

    Tries the plugin registry first (for plugin-provided cost tracking),
    then falls back to the generic config-based pricing.
    """
    usage = response_body.get("usage", {}) if response_body else {}
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cache_hit = read_cache_hit_tokens(provider, response_body, config)
    cache_miss = usage.get("prompt_cache_miss_tokens",
                           prompt_tokens - cache_hit if prompt_tokens > cache_hit else 0)

    usage_for_plugin = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "prompt_cache_hit_tokens": cache_hit,
        "prompt_cache_miss_tokens": cache_miss,
    }

    # Try plugin registry first
    plugin_cost = resolve_service("pricing", fallback=get_registry).calculate_cost(provider, model, usage_for_plugin)
    if plugin_cost is not None:
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "cost": round(plugin_cost, 8),
        }

    # Fall back to config-based pricing
    pricing = config.get_pricing(provider, model)

    cache_hit_cost = (cache_hit / 1_000_000) * pricing["cache_hit"]
    cache_miss_cost = (cache_miss / 1_000_000) * pricing["cache_miss"]
    output_cost = (completion_tokens / 1_000_000) * pricing["output"]

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "cost": round(cache_hit_cost + cache_miss_cost + output_cost, 8),
    }


# ── Request Forwarding ───────────────────────────────────────────────────────

def forward_request(provider_cfg: dict, body: dict, config, session_id: str | None = None):
    """Forward a request to a provider.

    If body['stream'] is True, returns (chunk_iterable, status_code) where
    chunk_iterable yields raw bytes as they arrive from the upstream.
    Otherwise returns (response_body_dict, status_code).

    ``session_id`` (optional): a stable per-conversation identifier forwarded as
    the ``x-opencode-session`` header. OpenCode requires this header to optimize
    its service; requests missing it may error (from 2026-09-06). The caller
    (handler) passes the client's header when present, else a stable per-profile
    ID so every outbound request carries it.
    """
    api_key = ""
    provider_name = provider_cfg["provider"]
    # 1. Encrypted credential stored via the UI (sole source for provider keys)
    from .credential_store import get_credential_store
    store = resolve_service("credential_store", fallback=get_credential_store)
    api_key = ""
    if store is not None:
        api_key = store.get(provider_name) or ""
    # Local / self-hosted providers (e.g. llama.cpp, or any user-added endpoint)
    # run KEYLESS — the request is sent without an Authorization header. Only
    # the known API-keyed providers hard-require a stored key; a genuinely
    # keyed upstream without one then surfaces as a real ProviderAuthError
    # (a 401 from the upstream) instead of a synthetic ConfigError.
    from .setup import _provider_needs_key
    if not api_key and _provider_needs_key(provider_name):
        raise ConfigError(
            f"No API key found for provider '{provider_name}'. "
            f"Add it in Models → Providers → Configuration."
        )

    streaming = body.get("stream", False)

    url = f"{provider_cfg['base_url']}/chat/completions"
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "LLMControlPlane/1.0",
    }
    if session_id:
        headers["x-opencode-session"] = session_id
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    _read_timeout = _upstream_read_timeout()
    try:
        resp = urllib.request.urlopen(req, timeout=_read_timeout)
        if streaming:
            # Return a closure that yields chunks and closes the response when done.
            # This avoids buffering the entire SSE stream in memory.
            def chunk_reader():
                try:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        yield chunk
                except TimeoutError as e:
                    # Mid-stream socket read timeout (socket.timeout == TimeoutError
                    # on py3.10+). Classify as ProviderTimeoutError so the chain
                    # fallback can engage instead of escaping as an untyped error.
                    raise ProviderTimeoutError(
                        f"Provider {provider_cfg['provider']} stream read timed out after "
                        f"{_read_timeout:.0f}s"
                    ) from e
                finally:
                    resp.close()
            return chunk_reader(), resp.status
        else:
            raw = resp.read()
            resp.close()
            response_body = json.loads(raw.decode("utf-8"))
            return response_body, resp.status
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")[:500]
        status = e.code
        # Insufficient balance / credits error — provider-side funding issue.
        # Must be detected BEFORE the generic 4xx / auth branches so it gets a
        # dedicated exception that trips the circuit breaker (a drained account
        # won't self-heal, so fall back + break rather than retry).
        if _is_credits_error(error_body, status):
            raise ProviderCreditsError(
                f"Provider {provider_cfg['provider']} out of credits: {error_body}"
            )
        if status == 401 or status == 403:
            reason = error_body.strip() if error_body.strip() else f"HTTP {status}"
            raise ProviderAuthError(
                f"Provider {provider_cfg['provider']} rejected auth: {reason}"
            )
        elif status == 429:
            raise ProviderRateLimitError(f"Provider {provider_cfg['provider']} rate limited")
        elif 400 <= status < 500:
            # 4xx (non-auth, non-rate-limit, non-credits). A body with a real
            # message is a bad request — the body is the problem, don't fall
            # back (the same body would fail again). But a BARE 4xx (empty
            # body or no error/message field) is ambiguous: it could be a
            # transient provider rejection. With healthy fallbacks in the
            # chain, fall through (ProviderInternalError) rather than abort
            # everything.
            if _is_bare_4xx(error_body):
                raise ProviderInternalError(
                    f"Provider {provider_cfg['provider']} HTTP {status}: {error_body}"
                )
            raise ProviderBadRequestError(
                f"Provider {provider_cfg['provider']} HTTP {status}: {error_body}"
            )
        elif status >= 500:
            # 5xx — provider-side transient failure. Distinct from auth so the
            # circuit breaker and retry logic can treat it appropriately.
            raise ProviderInternalError(
                f"Provider {provider_cfg['provider']} HTTP {status}: {error_body}"
            )
        raise ProviderAuthError(f"Provider {provider_cfg['provider']} HTTP {status}: {error_body}")
    except urllib.error.URLError as e:
        raise ProviderTimeoutError(f"Provider {provider_cfg['provider']} unreachable: {e.reason}")
    except TimeoutError as e:
        # Raw socket read timeout on the full-body (non-streaming) path.
        # ``urlopen`` wraps connect-level timeouts in URLError, but a timeout
        # mid-``resp.read()`` raises bare TimeoutError (socket.timeout) that
        # escapes the URLError handler above. Without this, try_chain's typed
        # fallback never engages and the client sees a generic LCP-5001
        # instead of a ProviderTimeoutError (verified 2026-09-06 — long
        # cache-miss compression summaries died with exactly this symptom).
        raise ProviderTimeoutError(
            f"Provider {provider_cfg['provider']} read timed out after "
            f"{_read_timeout:.0f}s"
        ) from e


def call_with_retry(request_fn: Callable[[], T], retry_cfg=None) -> T:
    """Call request_fn, retrying transient provider errors (5xx / 429) with backoff.

    ``retry_cfg`` is a dict with keys: max_attempts, backoff_base,
    backoff_multiplier, max_backoff, jitter. When missing or empty, a single
    attempt is made (no retry) — preserving prior behavior for callers that
    don't opt in.

    Retryable errors: ``ProviderInternalError`` (5xx), ``ProviderRateLimitError``
    (429) — transient, worth re-attempting.

    Non-retryable errors propagate immediately: ``ProviderBadRequestError``
    (the body is wrong — retrying can't help), ``ProviderAuthError`` (the key is
    rejected — retrying won't fix it), ``ProviderTimeoutError`` (provider may be
    down — the chain fallback handles it instead).

    Returns whatever ``request_fn`` returns on success; re-raises the last
    retryable error once attempts are exhausted.
    """
    retry_cfg = retry_cfg or {}
    max_attempts = max(1, int(retry_cfg.get("max_attempts", 1)))
    backoff_base = float(retry_cfg.get("backoff_base", 0.5))
    backoff_multiplier = float(retry_cfg.get("backoff_multiplier", 2))
    max_backoff = float(retry_cfg.get("max_backoff", 10))
    jitter = bool(retry_cfg.get("jitter", True))

    for attempt in range(1, max_attempts + 1):
        try:
            return request_fn()
        except (ProviderInternalError, ProviderRateLimitError) as e:
            if attempt >= max_attempts:
                # All retryable attempts exhausted — surface the last error so
                # the chain fallback (or caller) can decide what to do.
                raise
            delay = min(backoff_base * (backoff_multiplier ** (attempt - 1)), max_backoff)
            if jitter:
                delay *= random.uniform(0.5, 1.5)
            logger.info(
                "provider_retry",
                attempt=attempt,
                max_attempts=max_attempts,
                delay_ms=int(delay * 1000),
                error=str(e),
            )
            time.sleep(delay)
    # Unreachable — the loop body always returns (success) or re-raises, because
    # max_attempts is clamped to >= 1. Kept for the type checker's benefit.
    raise RuntimeError("retry loop exited unexpectedly")  # pragma: no cover


def _has_healthy_alternative(cb, chain: list[dict], profile_name: str,
                             start_idx: int, config) -> bool:
    """Return True if any chain step after start_idx has a healthy provider.

    Used for degraded-provider gating: if a degraded provider is next in line
    but a healthy provider is coming up, skip the degraded one.
    """
    for step in chain[start_idx + 1:]:
        p = step["provider"]
        url = step.get("base_url") or config.providers.get(p, {}).get("api_base", "")
        if cb.status_of(p, url, profile_name) == "healthy":
            return True
    return False


def try_chain(profile_name: str, profile_cfg: dict, body: dict, config,
               warning_sink: list | None = None,
               session_id: str | None = None) -> tuple[dict, int, str, str]:
    """Try each provider in the chain. Returns (response, status, provider, model).

    ``warning_sink`` (optional): a list that receives human-readable warnings
    (e.g. contextual near-limit notices) so the caller can surface them to the
    client without failing the request. Near-limit requests are NOT hard-413'd
    — the agent gets a signal instead of a silently truncated history.

    ``session_id`` (optional): stable per-conversation identifier forwarded
    upstream as ``x-opencode-session`` (see forward_request).
    """
    cb = resolve_service("circuit_breaker", fallback=get_circuit_breaker)
    # Work on a COPY of the chain — a dynamic-router override must never mutate
    # the profile config in place, or every later request would use the rerouted
    # model permanently.
    chain = [dict(step) for step in profile_cfg["chain"]]
    chain_len = len(chain)
    errors = []

    # ── Memory harness: auto-recall + inject relevant facts into the request ─
    # Opt-in via plugins.memory.auto_recall. No-op when memory is inactive.
    try:
        from .memory.harness import config_for, inject_memory_context
        _mc = config_for(config)
        if _mc["enabled"]:
            body["messages"] = inject_memory_context(
                body.get("messages", []),
                profile=profile_name,
                enabled=True,
                top_k=_mc["top_k"],
                min_score=_mc["min_score"],
                tag_filter=_mc["tag_filter"],
            )
    except Exception:  # noqa: BLE001 — never let memory break the request
        pass

    # ── Dynamic router: reorder chain so best (provider, model) step is first ─
    # Per-profile gating: a profile override (routing_enabled:<profile>) wins,
    # then the global toggle. select_step re-reads policy/rules per profile.
    router = resolve_service("dynamic_router", fallback=get_dynamic_router)
    if router.is_enabled(config, profile_name) and chain_len > 0:
        reordered = router.select_step(
            messages=body.get("messages", []),
            tools=body.get("tools"),
            max_tokens=body.get("max_tokens", 1024),
            chain=chain,
            profile=profile_name,
            config=config,
        )
        if reordered:
            chain = reordered  # apply to the local copy only

    # ── Context pre-flight: never send a request bigger than ANY model in the
    #    chain can hold. The router already excludes too-small models per
    #    request; this is the final guarantee that a request which exceeds the
    #    profile's largest routable context fails cleanly (413) instead of
    #    hitting a provider 400 "exceed_context_size".
    try:
        from .cost_estimator import count_tokens as _count_tokens
        from .router import (
            _CONTEXT_OUTPUT_RESERVE as _CTX_OUTPUT_RESERVE,
            _strip_client_context_from_messages,
        )
        request_tokens = _count_tokens(
            _strip_client_context_from_messages(body.get("messages", [])),
            body.get("tools"),
        )
        if request_tokens > 0:
            max_ctx = router._max_routable_context(chain, config)
            if request_tokens + _CTX_OUTPUT_RESERVE > max_ctx:
                logger.warning(
                    "context_near_limit",
                    profile=profile_name,
                    request_tokens=request_tokens,
                    max_ctx=max_ctx,
                )
                if warning_sink is not None:
                    warning_sink.append(
                        f"[lcp] request {request_tokens} tokens is at/near the "
                        f"{max_ctx}-token context limit for profile '{profile_name}'; "
                        f"history may be truncated and the model may forget earlier turns"
                    )
    except RequestTooLargeError:
        raise
    except Exception:  # noqa: BLE001 — never let the guard break routing
        pass

    for i, step in enumerate(chain):
        provider_name = step["provider"]
        base_url = step.get("base_url") or config.providers.get(provider_name, {}).get("api_base", "")
        model = step["model"]

        logger.info(
            "chain_attempt",
            profile=profile_name,
            provider=provider_name,
            model=model,
            attempt=i + 1,
            chain_len=chain_len,
        )

        # Check vision support — fail gracefully so user knows why
        if has_image_content(body.get("messages", [])):
            model_limits = config.model_limits.get(model, {})
            if not model_limits.get("supports_vision", False):
                logger.warning(
                    "vision_not_supported",
                    profile=profile_name,
                    provider=provider_name,
                    model=model,
                )
                errors.append(f"{provider_name}/{model}: model does not support vision/image input")
                continue

        # Check circuit breaker
        if not cb.is_available(provider_name, base_url, profile_name):
            logger.warning(
                "provider_skipped_circuit_breaker",
                provider=provider_name,
                profile=profile_name,
            )
            errors.append(f"{provider_name}: circuit breaker open")
            continue

        # Degraded gating — prefer healthy alternatives over a degraded provider.
        # Only route to degraded when no healthy option remains in the chain.
        if cb.status_of(provider_name, base_url, profile_name) == "degraded" \
                and _has_healthy_alternative(cb, chain, profile_name, i, config):
            logger.warning(
                "provider_degraded_skipped",
                provider=provider_name,
                profile=profile_name,
            )
            errors.append(f"{provider_name}: degraded (healthy alternative available)")
            continue

        # Set model in body — translate the gateway model name to the
        # provider's API model ID when the provider's plugin overrides
        # get_api_model() (e.g. Command Code's prefixed catalog IDs).
        body["model"] = model
        plugin = resolve_service("pricing", fallback=get_registry).for_provider(provider_name)
        if plugin is not None:
            api_model = plugin.get_api_model(model)
            if api_model != model:
                logger.info(
                    "model_translated_for_provider",
                    provider=provider_name,
                    logical_model=model,
                    api_model=api_model,
                )
                body["model"] = api_model

        # Normalize for prefix caching — deterministic message/tool ordering
        # so repeated logical prompts hit the provider's KV-cache.
        if "messages" in body:
            body["messages"] = normalize_messages_for_cache(body["messages"])
        if "tools" in body and body["tools"]:
            body["tools"] = normalize_tools_for_cache(body["tools"])

        # Sanitize AFTER normalization — normalization strips tool_calls from
        # assistant messages, so any tool messages left behind become orphaned.
        # Running here ensures we see the final message shape before forwarding.
        if "messages" in body:
            body["messages"] = sanitize_messages(body["messages"])

        # Inject empty reasoning_content for thinking-mode models (DeepSeek).
        # Clients often strip it from history; DeepSeek requires presence.
        if "messages" in body:
            body["messages"] = ensure_thinking_reasoning_content(
                body["messages"], model, config
            )

        # Add base_url to step config (API keys now come from credential store)
        step_with_key = {
            **step,
            "base_url": base_url,
        }

        # Per-provider retry config (5xx/429 retried with backoff). Real Config
        # objects expose .retry; mocks without it fall back to a single attempt.
        retry_cfg = getattr(config, "retry", None)
        if not isinstance(retry_cfg, dict):
            retry_cfg = {}

        try:
            t0 = time.time()
            resp, status = call_with_retry(
                lambda: forward_request(step_with_key, body, config, session_id=session_id),
                retry_cfg,
            )
            hop_ms = int((time.time() - t0) * 1000)
            cb.record_success(provider_name, base_url, profile_name)
            logger.info(
                "chain_success",
                profile=profile_name,
                provider=provider_name,
                model=model,
                attempt=i + 1,
                chain_len=chain_len,
                latency_ms=hop_ms,
            )
            return resp, status, provider_name, model
        except ProviderBadRequestError as e:
            # Bad request — the problem is in the body, not the provider.
            # Falling back to another provider just wastes attempts.
            # Re-raise immediately so the client sees the real error.
            # NOTE: deliberately NOT calling cb.record_failure() — a 400 caused
            # by a bad client body is not a provider outage, and counting it
            # against provider health would falsely degrade healthy providers.
            logger.error(
                "chain_bad_request",
                profile=profile_name,
                provider=provider_name,
                model=model,
                attempt=i + 1,
                chain_len=chain_len,
                error=str(e),
            )
            raise AllProvidersFailedError(
                f"Provider {provider_name} rejected the request as invalid: {e}"
            ) from e
        except (ProviderTimeoutError, ProviderAuthError, ProviderCreditsError,
                ProviderRateLimitError, ProviderInternalError) as e:
            cb.record_failure(provider_name, base_url, profile_name,
                              error_type=type(e).__name__,
                              error_reason=str(e))
            errors.append(f"{provider_name}: {e}")
            # Log a failover event when a next provider exists in the chain
            if i + 1 < chain_len:
                next_provider = chain[i + 1]["provider"]
                cb.record_failover(
                    profile_name, provider_name, next_provider,
                    reason=type(e).__name__,
                    error_message=str(e),
                )
            logger.warning(
                "chain_fallback",
                profile=profile_name,
                provider=provider_name,
                model=model,
                attempt=i + 1,
                chain_len=chain_len,
                error=str(e),
                next=chain[i + 1]["provider"] if i + 1 < chain_len else "none",
            )
        except ConfigError as e:
            # Provider config problem (e.g. missing API key) — this provider
            # can't work, but it is NOT a provider outage, so it must NOT trip
            # the circuit breaker: a misconfigured local/keyless provider would
            # otherwise be marked degraded/dead for a config issue. Log the
            # failover (observability) and fall through to the next provider.
            errors.append(f"{provider_name}: {e}")
            if i + 1 < chain_len:
                next_provider = chain[i + 1]["provider"]
                cb.record_failover(
                    profile_name, provider_name, next_provider,
                    reason="ConfigError",
                    error_message=str(e),
                )
            logger.error(
                "chain_config_error",
                profile=profile_name,
                provider=provider_name,
                model=model,
                attempt=i + 1,
                chain_len=chain_len,
                error=str(e),
            )

    raise AllProvidersFailedError(f"All providers failed for {profile_name}: {'; '.join(errors)}")


# ── Cost Recording ───────────────────────────────────────────────────────────

def record_cost(engine, profile: str, model: str, provider: str, cost_info: dict,
                success: bool, error_type: str | None, tools_blocked: list[str],
                error_detail: str | None = None,
                conversation_id: str | None = None) -> None:
    """Record cost data to SQLite and track against budgets.

    ``conversation_id`` (optional): the client's real per-conversation id
    (``x-opencode-session``). Stamped onto the new row AND the routing
    decisions written during this call, so the unified Conversations view can
    group request + provider logs by conversation. NULL (no header present /
    legacy) is later covered by the deterministic burst backfill.
    """
    cost = cost_info.get("cost", 0)

    rid = None
    with get_session(engine) as session:
        req = RequestModel(
            timestamp=datetime.now(timezone.utc).isoformat(),
            profile=profile,
            model=model,
            provider=provider,
            prompt_tokens=cost_info.get("prompt_tokens", 0),
            completion_tokens=cost_info.get("completion_tokens", 0),
            cache_hit_tokens=cost_info.get("cache_hit_tokens", 0),
            cache_miss_tokens=cost_info.get("cache_miss_tokens", 0),
            cost=cost,
            latency_ms=cost_info.get("latency_ms", 0),
            success=1 if success else 0,
            error_type=error_type,
            error_detail=error_detail,
            tools_blocked=",".join(tools_blocked) if tools_blocked else None,
        )
        session.add(req)
        session.commit()
        rid = req.id

    # Stamp the conversation correlation id (client's real session header) onto
    # this request row + the routing rows written during the call. Best-effort:
    # a stamping failure must never fail the request that was already recorded.
    if conversation_id and rid is not None:
        try:
            from .work_conversations import stamp_new
            ts = req.timestamp
            stamp_new(request_id=rid, conversation_id=conversation_id,
                      profile=profile, ts=ts)
        except Exception:  # noqa: BLE001
            logger.warning("conversation_stamp_failed", profile=profile,
                           error="")

    # Track spend against key (when key auth is wired in)

    # ── Plugin hooks ──────────────────────────────────────────────────────
    # If the provider has a plugin, let it record the tokens (e.g. llama.cpp
    # local tracking, or future plugins that need request-level callbacks).
    if success:
        plugin = resolve_service("pricing", fallback=get_registry).for_provider(provider)
        if plugin is not None and hasattr(plugin, "record_tokens"):
            try:
                plugin.record_tokens(
                    model,
                    prompt_tokens=cost_info.get("prompt_tokens", 0)
                                 + cost_info.get("cache_miss_tokens", 0),
                    completion_tokens=cost_info.get("completion_tokens", 0),
                    cache_hit_tokens=cost_info.get("cache_hit_tokens", 0),
                    latency_ms=cost_info.get("latency_ms", 0),
                )
            except Exception as exc:
                logger.warning("plugin_record_tokens_failed",
                               provider=provider, error=str(exc))
