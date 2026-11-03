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

``claude-haiku-5-5`` is priced from Command Code's own catalogue at
https://commandcode.ai/docs/resources/pricing-limits (read 2026-11-02): input
$0.10/M, output $0.50/M, cache read $0.01/M, cache write $0.125/M. That is the
same page the estate's other rates were verified against and the same provider
this estate actually routes Claude through, so the number is sourced rather
than inferred.

``claude-haiku-4-5`` is priced from the same page (re-read 2026-11-03): 200K
context, input $1.00/M, output $5.00/M, cache read $0.10/M, cache write $1.25/M.
It previously carried $0.80 / $4.00 / $0.08 — 80% of the published rate, an
inference rather than a source. Corrected once the catalogue was re-read.

Correcting a rate table does NOT rewrite history: rows already recorded keep the
figures they were costed at. Only new traffic is affected, so a sourced figure
beats a round-number guess here.
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
    "claude-haiku-5-5",   # in-plan, priced below from the Command Code catalogue
    "claude-haiku-4-5",
]

#: Per-1M-token USD rates. Anthropic list rates, as already declared for these
#: same models in commandcode.py. Only models with a sourced rate appear here.
_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"cache_hit": 0.30, "cache_miss": 3.00, "output": 15.00},
    "claude-sonnet-5":   {"cache_hit": 0.30, "cache_miss": 3.00, "output": 15.00},
    "claude-opus-5":     {"cache_hit": 0.375, "cache_miss": 15.00, "output": 75.00},
    # 2026-11-03, same catalogue page: 1.00 / 5.00 / cache read 0.10 / cache write 1.25
    "claude-haiku-4-5":  {"cache_hit": 0.10, "cache_miss": 1.00, "output": 5.00},
    # 2026-11-02, commandcode.ai/docs/resources/pricing-limits: in 0.10 / out 0.50
    # / cache read 0.01 (cache write 0.125 is not a column this schema tracks).
    "claude-haiku-5-5":  {"cache_hit": 0.01, "cache_miss": 0.10, "output": 0.50},
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

        None means the rate is not sourced for that model; every Claude tier this
        module claims currently has one, so None here is a signal that a new model
        appeared without a rate rather than an expected outcome.
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
