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
