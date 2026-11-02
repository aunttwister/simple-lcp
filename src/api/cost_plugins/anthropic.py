"""Cost tracking plugin for the Anthropic API (Claude).

Why this exists
---------------
LCP now speaks the Anthropic wire format natively (``src/api/wire_anthropic.py``:
``{api_base}/messages``, ``x-api-key`` + ``anthropic-version``, no
``temperature``). Wire selection is driven by the model id (``claude*`` /
``anthropic*``), so a provider created from this preset is routed over the
Messages API without any extra configuration.

Without a registered plugin a provider gets no quick-add preset, so "Anthropic"
never appears in the add-provider dropdown. That is the whole purpose of this
module; the pricing table below is a bonus that keeps cost accounting honest.

Pricing
-------
Per-1M-token rates, matching the Claude list rates the estate already declares
for the same models in ``cost_plugins/commandcode.py`` (verified 2026-10-05
there). Anthropic bills no peak/off-peak differential, so there are no
``peak_*`` keys and no time-based rate selection.

``claude-haiku-5-5`` is deliberately listed as SUPPORTED but UNPRICED. It is the
only Claude tier currently reachable on this estate's commandcode plan, but no
authoritative per-token rate has been sourced for it. Guessing a number would
silently corrupt every cost report that includes it, so ``get_pricing`` returns
``None`` for it and the pipeline records the call with tokens but no cost. Give
this module the real rate when it is known, and the number becomes trustworthy
everywhere at once.
"""

from typing import Optional

from .base import CostPlugin, get_registry

#: Claude models this plugin claims. Listed separately from ``_PRICING`` on
#: purpose: "we know this model exists" and "we know what it costs" are two
#: different claims, and conflating them is how invented prices get shipped.
_SUPPORTED_MODELS: list[str] = [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-5-5",   # in-plan, RATE UNKNOWN -> records as unpriced
    "claude-haiku-4-5",
]

#: Per-1M-token USD rates. Anthropic list rates, as already declared for these
#: same models in commandcode.py. Only models with a sourced rate appear here.
_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"cache_hit": 0.30, "cache_miss": 3.00, "output": 15.00},
    "claude-sonnet-5":   {"cache_hit": 0.30, "cache_miss": 3.00, "output": 15.00},
    "claude-opus-5":     {"cache_hit": 0.375, "cache_miss": 15.00, "output": 75.00},
    "claude-haiku-4-5":  {"cache_hit": 0.08, "cache_miss": 0.80, "output": 4.00},
}

#: Anthropic's public API version string base; model ids have no vendor prefix
#: here, unlike commandcode's ``vendor/model`` catalogue ids.
_API_BASE = "https://api.anthropic.com/v1"


class AnthropicCostPlugin(CostPlugin):
    """Cost tracking for the Anthropic (Claude) API."""

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def preset(self) -> Optional[dict]:
        return {
            "api_base": _API_BASE,
            "models": self.get_supported_models(),
        }

    def get_supported_models(self) -> list[str]:
        return list(_SUPPORTED_MODELS)

    def get_pricing(self, model: str) -> Optional[dict]:
        """Rates for *model*, or None when the rate is not sourced.

        None is the honest answer for ``claude-haiku-5-5``: it exists and is
        callable, but nobody has given this module a real rate for it.
        """
        return _PRICING.get(model)

    def calculate_cost(self, model: str, usage: dict) -> Optional[float]:
        pricing = _PRICING.get(model)
        if pricing is None:
            return None

        # wire_anthropic maps Anthropic's input_tokens -> prompt_tokens, so this
        # is the right source here. Anthropic's input_tokens excludes cache
        # read/creation tokens; when those are present they carry their native
        # names, which LCP does not set breakpoints for today.
        cache_hit = usage.get("prompt_cache_hit_tokens", 0)
        cache_miss = usage.get("prompt_cache_miss_tokens",
                               max(0, usage.get("prompt_tokens", 0) - cache_hit))
        output = usage.get("completion_tokens", 0)

        return (
            (cache_hit / 1_000_000) * pricing["cache_hit"]
            + (cache_miss / 1_000_000) * pricing["cache_miss"]
            + (output / 1_000_000) * pricing["output"]
        )


# ── Auto-register ──────────────────────────────────────────────────────────
_registry = get_registry()
_registry.register(AnthropicCostPlugin())
