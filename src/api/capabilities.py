"""Per-provider request-parameter capabilities.

Why this exists — measured in prod, 2026-10-24 (`docker logs lcp --since 72h`), profile `l2`:

```
chain_attempt 1015 · chain_success 895 · chain_bad_request 96 · all_providers_failed 24
```

96 rejections came from 24 requests (4 each — every request that reached the tail of the 5-step
chain), and those 24 were answered 502 before `RequestUnsupportedError` existed:

- 48× `commandcode` — `reasoning_effort` "Invalid option: expected one of off|low|medium|high|xhigh|max"
- 24× `deepseek`    — `response_format` "This response_format type is unavailable now"
- 24× `local-sparks` — `response_format`

Every successful request also paid those rejected round-trips first. The gateway was forwarding
the client's body verbatim to providers that cannot accept parts of it.

The two parameters need different treatment, and that distinction is the whole design:

- **`reasoning_effort` is a hint.** Dropping an unsupported value costs the caller nothing but a
  little steering, so the right move is to strip it and forward.
- **`response_format` is a contract.** Silently dropping it changes what comes back — a caller that
  asked for `json_schema` gets prose. So a provider that cannot honour it is **skipped**, not
  downgraded, and if every provider is skipped the caller gets `RequestUnsupportedError` (400)
  instead of a wrong-shaped 200.

Capabilities are declared per provider, and only for providers whose limits were actually measured.
A provider with no declaration gets the body unchanged — the gateway does not guess.
"""
from __future__ import annotations

from typing import Any

# Only measured limits are declared here. Anything absent means "no known restriction", so the
# body passes through untouched; inventing a restriction would silently degrade providers we have
# not tested.
DEFAULT_CAPABILITIES: dict[str, dict[str, Any]] = {
    # Rejects reasoning_effort outside this enum with HTTP 400 (measured 2026-10-24, 48 events).
    "commandcode": {"reasoning_effort": {"off", "low", "medium", "high", "xhigh", "max"}},
    # Rejects json_schema ("This response_format type is unavailable now"), but ACCEPTS
    # json_object — measured 2026-10-25 by sending the identical body to api.deepseek.com
    # (200) and through the gateway (LCP-3002). The earlier {text} declaration was a false
    # negative: the 2026-10-24 events were json_object failures caused by the *prompt* not
    # containing the word "json" (a deepseek precondition), not by the type being unsupported.
    "deepseek": {"response_format": {"text", "json_object"}},
    "local-sparks": {"response_format": {"text"}},
}

# The body keys a provider's `response_format` support is judged by. `type` is what the OpenAI
# wire format actually carries ("text" | "json_object" | "json_schema").
_RESPONSE_FORMAT_KEY = "response_format"


def get_capabilities(provider: str, config: Any = None) -> dict[str, Any]:
    """Return the capability map for *provider*, config override winning over the defaults.

    A provider config may carry ``capabilities`` (e.g.
    ``providers.commandcode.capabilities.reasoning_effort: [low, high]``); that replaces the
    default for that parameter. Declaring an empty list means "accepts nothing" — i.e. always strip.
    """
    caps: dict[str, Any] = {}
    default = DEFAULT_CAPABILITIES.get(provider)
    if default:
        caps.update({k: set(v) if isinstance(v, (set, list, tuple)) else v
                     for k, v in default.items()})

    providers = getattr(config, "providers", None)
    if isinstance(providers, dict):
        override = (providers.get(provider) or {}).get("capabilities")
        if isinstance(override, dict):
            for param, allowed in override.items():
                if isinstance(allowed, (list, tuple, set)):
                    caps[param] = set(allowed)
                else:
                    caps[param] = allowed
    return caps


def prepare_body(provider: str, body: dict, config: Any = None) -> tuple[dict, list[str], str | None]:
    """Make *body* safe to send to *provider*.

    Returns ``(body_for_this_provider, notes, skip_reason)``:

    - ``notes`` names every parameter that was stripped (for the log, never the client).
    - ``skip_reason`` is set when the provider cannot honour a parameter whose semantics matter;
      the caller must skip this provider rather than send a body that will come back wrong.

    The input dict is never mutated — a stripped parameter must still reach the next provider,
    which may well accept it.
    """
    caps = get_capabilities(provider, config)
    if not caps:
        return body, [], None

    out = dict(body)
    notes: list[str] = []

    allowed_effort = caps.get("reasoning_effort")
    if isinstance(allowed_effort, (set, frozenset)) and "reasoning_effort" in out:
        value = out["reasoning_effort"]
        if value not in allowed_effort:
            out.pop("reasoning_effort", None)
            notes.append(f"stripped reasoning_effort={value!r} (allowed: {sorted(allowed_effort)})")

    allowed_format = caps.get(_RESPONSE_FORMAT_KEY)
    if isinstance(allowed_format, (set, frozenset)) and _RESPONSE_FORMAT_KEY in out:
        requested = out[_RESPONSE_FORMAT_KEY]
        kind = requested.get("type") if isinstance(requested, dict) else requested
        if kind not in allowed_format:
            return body, notes, (
                f"response_format {kind!r} is not supported (supports: {sorted(allowed_format)})"
            )

    return out, notes, None
