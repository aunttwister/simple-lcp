"""Cost tracking plugin for Command Code (commandcode.ai).

Command Code is a coding-agent platform that provides access to numerous
LLM providers through a single API endpoint
(``https://api.commandcode.ai/provider/v1``).

Usage tracking has two sources:
  - ``fetch_subscription`` — Command Code's internal billing API
    (``/internal/billing/credits`` + ``/internal/billing/subscriptions``),
    authenticated with a browser session cookie from the encrypted credential
    store (same pattern as OpenCode). Returns rolling 5-hour / weekly usage
    percentages with reset countdowns plus remaining monthly credits.
  - ``fetch_summary`` / ``fetch_usage`` — cost history from the gateway's own
    ``requests`` table (every routed request is logged with the provider name).

Pricing is at the provider's list price (Command Code passes through at
cost). Common models are pre-priced; unknown models fall back to config-based
pricing from the gateway config.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func

from ..logging_config import get_logger
from .base import CostPlugin, get_registry, select_rates

logger = get_logger("lcp.cost.commandcode")

# ── API endpoint ────────────────────────────────────────────────────────────
# Command Code Provider API (Provider plan or higher):
#   https://commandcode.ai/docs/provider
# Chat Completions: POST /provider/v1/chat/completions (Bearer <CMD_API_KEY>)
_COMMANDCODE_BASE = "https://api.commandcode.ai/provider/v1"

# ── Pre-known model pricing (per 1M tokens, USD) ────────────────────────────
# Sourced from commandcode.ai/docs/resources/pricing-limits (August 2026), with
# DeepSeek rates re-verified against https://api-docs.deepseek.com/quick_start/pricing
# (off-peak) on 2026-10-05. Only models commonly routed through commandcode are
# listed. Unlisted models are billed through config-based pricing from the
# gateway config.
_FLASH_PRICING: dict[str, float] = {
    "cache_hit": 0.003,
    "cache_miss": 0.15,
    "output": 0.6,
    "peak_cache_hit": 0.006,
    "peak_cache_miss": 0.30,
    "peak_output": 1.20,
}

_COMMANDCODE_PRICING: dict[str, dict[str, float]] = {
    # DeepSeek (primary models — 75% off deal)
    # DeepSeek-V4-Pro-0813. Corrected 2026-10-05 to DeepSeek's own OFF-PEAK
    # rates per https://api-docs.deepseek.com/quick_start/pricing — this entry
    # previously held MiMo V2.6 Pro's catalogue table by mistake. Peak = exactly
    # 2x each base rate (same rule as deepseek.py).
    "deepseek-v4-pro": {
        "cache_hit": 0.022,
        "cache_miss": 0.66,
        "output": 1.98,
        "peak_cache_hit": 0.044,
        "peak_cache_miss": 1.32,
        "peak_output": 3.96,
    },
    # Every spelling of DeepSeek-V4.1-Flash shares ONE entry — the Provider API
    # catalog ID `deepseek/deepseek-v4.1-flash`, the bare benchmark name
    # `deepseek-v4.1-flash`, the current DeepSeek API name `deepseek-flash`,
    # and the legacy `deepseek-v4-flash` / `...-vision-exp` names that DeepSeek
    # still accepts and serves with the same model. `_logical_model()` strips
    # the catalog prefix, so keying on the bare names covers the ID form too.
    # Verified against https://api-docs.deepseek.com/quick_start/pricing
    # (off-peak) on 2026-10-05.
    "deepseek-flash": dict(_FLASH_PRICING),
    "deepseek-v4-flash": dict(_FLASH_PRICING),
    "deepseek-v4.1-flash": dict(_FLASH_PRICING),
    "deepseek-v4-flash-vision-exp": dict(_FLASH_PRICING),
    # `deepseek-v4.1-flash-fast` is NOT another spelling — Command Code's
    # catalogue lists it as a distinct model, "DeepSeek V4.1 Flash Fast", with
    # its own rates (verified in the catalogue JSON embedded in
    # https://commandcode.ai/docs/resources/pricing-limits, 2026-10-05).
    # Without this row it fell through to a $0 record while sitting in the live
    # `l2` chain as step 4, silently under-counting real spend.
    # Command Code lists this model with its own peak rate ($0.32 / $1.16) —
    # i.e. 2x off-peak, same rule as every other DeepSeek row.
    "deepseek-v4.1-flash-fast": {
        "cache_hit": 0.016,
        "cache_miss": 0.16,
        "output": 0.58,
        "peak_cache_hit": 0.032,
        "peak_cache_miss": 0.32,
        "peak_output": 1.16,
    },
    # Anthropic Claude
    "claude-sonnet-4-6": {
        "cache_hit": 0.30,
        "cache_miss": 3.00,
        "output": 15.00,
    },
    "claude-sonnet-5": {
        "cache_hit": 0.30,
        "cache_miss": 3.00,
        "output": 15.00,
    },
    "claude-opus-5": {
        "cache_hit": 0.375,
        "cache_miss": 15.00,
        "output": 75.00,
    },
    "claude-haiku-4-5": {
        "cache_hit": 0.08,
        "cache_miss": 0.80,
        "output": 4.00,
    },
    # OpenAI GPT
    "gpt-5.6-luna": {
        "cache_hit": 0.01,
        "cache_miss": 0.10,
        "output": 0.60,
    },
    "gpt-5.6-terra": {
        "cache_hit": 0.10,
        "cache_miss": 1.00,
        "output": 6.00,
    },
    # Kimi
    "kimi-k3": {
        "cache_hit": 0.30,
        "cache_miss": 3.00,
        "output": 15.00,
    },
    "kimi-k2.7-code": {
        "cache_hit": 0.19,
        "cache_miss": 0.95,
        "output": 4.00,
    },
    # MiniMax
    "minimax-m3": {
        "cache_hit": 0.06,
        "cache_miss": 0.30,
        "output": 1.20,
    },
    # Qwen
    "qwen3.8-max": {
        "cache_hit": 0.10,
        "cache_miss": 1.00,
        "output": 4.00,
    },
}

# ── Model-ID resolution (live catalog, no hardcoding) ───────────────────────
# Command Code's Provider API expects full catalog IDs (e.g.
# ``deepseek/deepseek-v4-pro``, ``moonshotai/Kimi-K3``), NOT the bare names
# used elsewhere in the gateway for pricing/aggregation. Instead of a
# hardcoded 50+ entry table (Command Code adds models often), we derive the
# mapping from the live public GET /provider/v1/models catalog, cached with a
# short TTL. The generic rule "last path segment, lowercased" maps both ways:
#
#   deepseek/deepseek-v4-pro -> deepseek-v4-pro
#   moonshotai/Kimi-K3       -> kimi-k3
#   MiniMaxAI/MiniMax-M3     -> minimax-m3
#   claude-sonnet-5          -> claude-sonnet-5
#
# Unknown models pass through unchanged (the API returns a clear error).
_CATALOG_TTL_SECONDS = 600        # how long a successful catalog fetch is cached
_CATALOG_FAIL_COOLDOWN = 60       # don't retry a failed fetch for this long
_catalog_cache: dict = {
    "by_last_seg": {},            # {lowercased last segment: catalog id}
    "loaded_ts": 0.0,
    "failed_ts": 0.0,
}


def _load_catalog() -> dict[str, str]:
    """Fetch and cache the live Command Code model catalog.

    Returns ``{lowercased last segment: full catalog id}`` (e.g.
    ``{"deepseek-v4-flash": "deepseek/deepseek-v4-flash"}``). On failure or
    timeout, returns the last cached index (possibly empty) and backs off —
    request forwarding never blocks or breaks because of this.
    """
    import time as _time
    now = _time.time()
    if now - _catalog_cache["loaded_ts"] < _CATALOG_TTL_SECONDS:
        return _catalog_cache["by_last_seg"]
    if now - _catalog_cache["failed_ts"] < _CATALOG_FAIL_COOLDOWN:
        return _catalog_cache["by_last_seg"]

    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            _COMMANDCODE_BASE.rstrip("/") + "/models",
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/143.0.0.0 Safari/537.36"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        raw_models = data.get("data") if isinstance(data, dict) else data
        idx: dict[str, str] = {}
        for m in raw_models or []:
            mid = m.get("id") if isinstance(m, dict) else m
            if isinstance(mid, str) and mid:
                key = mid.split("/")[-1].strip().lower()
                idx.setdefault(key, mid)  # first id wins on last-segment collisions
        _catalog_cache["by_last_seg"] = idx
        _catalog_cache["loaded_ts"] = now
        return idx
    except Exception:
        _catalog_cache["failed_ts"] = now
        return _catalog_cache["by_last_seg"]


def _logical_model(model: str) -> str:
    """Derive the bare logical pricing name from any model ID.

    - Bare name → lowercased as-is.
    - Catalog ID (contains ``/``) → last path segment, lowercased.

    e.g. ``moonshotai/Kimi-K3`` → ``kimi-k3`` (matches _COMMANDCODE_PRICING).
    """
    if not model:
        return model
    return model.split("/")[-1].strip().lower()


def _api_model(model: str) -> str:
    """Resolve a bare logical name to its live Command Code catalog ID.

    Catalog IDs (containing ``/``) pass through unchanged. Bare names are
    looked up in the cached live catalog; unknown ones pass through unchanged.
    """
    if not model or "/" in model:
        return model
    return _load_catalog().get(model.strip().lower(), model)


class CommandCodeCostPlugin(CostPlugin):
    """Cost tracking for Command Code.

    Command Code does not expose a public balance or subscription API, so
    ``fetch_balance`` and ``fetch_subscription`` always return ``None``.
    Cost history comes from the gateway's ``requests`` table.
    """

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    def set_engine(self, engine: Any) -> None:
        """Bind the gateway SQLAlchemy engine for DB queries."""
        self._engine = engine

    # ── Identity ───────────────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "commandcode"

    @property
    def preset(self) -> Optional[dict]:
        return {
            "api_base": _COMMANDCODE_BASE,
            "models": self.get_supported_models(),
        }

    def get_supported_models(self) -> list[str]:
        return list(_COMMANDCODE_PRICING.keys())

    def get_api_model(self, model: str) -> str:
        """Translate a bare logical model name to the Provider API catalog ID.

        This is what gets sent upstream as ``body['model']``. Bare names like
        ``deepseek-v4-pro`` become ``deepseek/deepseek-v4-pro``; already-prefixed
        IDs pass through unchanged.
        """
        return _api_model(model)

    # ── Pricing ────────────────────────────────────────────────────────────

    def get_pricing(self, model: str) -> Optional[dict]:
        # Accept both bare logical names and API catalog IDs.
        return _COMMANDCODE_PRICING.get(_logical_model(model))

    def calculate_cost(self, model: str, usage: dict) -> Optional[float]:
        pricing = select_rates(_COMMANDCODE_PRICING.get(_logical_model(model)),
                               usage.get("__ts"))
        if pricing is None:
            return None

        cache_hit = usage.get("prompt_cache_hit_tokens", 0)
        cache_miss = usage.get(
            "prompt_cache_miss_tokens",
            usage.get("prompt_tokens", 0) - cache_hit,
        )
        output = usage.get("completion_tokens", 0)

        if cache_hit == 0 and cache_miss == 0:
            cache_miss = usage.get("prompt_tokens", 0)

        cost = (
            (cache_hit / 1_000_000) * pricing["cache_hit"]
            + (cache_miss / 1_000_000) * pricing["cache_miss"]
            + (output / 1_000_000) * pricing["output"]
        )
        return round(cost, 8)

    # ── Database helpers ───────────────────────────────────────────────────

    def _ensure_engine(self) -> Any:
        if self._engine is None:
            raise RuntimeError(
                "CommandCode plugin has no gateway engine — call set_engine() "
                "or pass engine= to the constructor before querying."
            )
        return self._engine

    def _gw_session(self):
        from ..models import get_session
        return get_session(self._ensure_engine())

    # ── Usage history (from gateway requests table) ────────────────────────

    def fetch_usage(self,
                    start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> list[dict]:
        """Return daily aggregates for commandcode from the gateway DB."""
        if self._engine is None:
            return []

        from ..models import Request as RequestModel

        try:
            with self._gw_session() as session:
                q = session.query(
                    func.substr(RequestModel.timestamp, 1, 10).label("day"),
                    RequestModel.model,
                    RequestModel.provider,
                    func.coalesce(func.sum(RequestModel.prompt_tokens), 0).label("prompt_tokens"),
                    func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("completion_tokens"),
                    func.coalesce(func.sum(RequestModel.cache_hit_tokens), 0).label("cache_hit_tokens"),
                    func.coalesce(func.sum(RequestModel.cache_miss_tokens), 0).label("cache_miss_tokens"),
                    func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                    func.count(RequestModel.id).label("request_count"),
                ).filter(
                    RequestModel.provider == "commandcode",
                    RequestModel.success == 1,
                )

                if start_date:
                    q = q.filter(RequestModel.timestamp >= start_date)
                if end_date:
                    q = q.filter(RequestModel.timestamp <= (end_date + "T23:59:59"))

                q = q.group_by("day").order_by("day")
                rows = q.all()

            result: list[dict] = []
            for r in rows:
                result.append({
                    "date": r.day,
                    "model": r.model or "unknown",
                    "provider": r.provider or "commandcode",
                    "prompt_tokens": int(r.prompt_tokens),
                    "completion_tokens": int(r.completion_tokens),
                    "cache_hit_tokens": int(r.cache_hit_tokens),
                    "cache_miss_tokens": int(r.cache_miss_tokens),
                    "cost": round(float(r.cost), 8),
                    "request_count": int(r.request_count),
                })

            logger.debug("usage_fetched", provider="commandcode", days=len(result))
            return result
        except Exception as exc:
            logger.warning("usage_query_failed", provider="commandcode", error=str(exc))
            return []

    # ── Balance (not available) ────────────────────────────────────────────

    def fetch_balance(self) -> Optional[dict]:
        return None

    # ── Rich summary — daily / weekly / monthly from gateway DB ────────────

    def fetch_summary(self) -> Optional[dict]:
        """Return daily, weekly, and monthly cost aggregates from the gateway DB."""
        if self._engine is None:
            return None

        from ..models import Request as RequestModel

        try:
            with self._gw_session() as session:
                now = datetime.now(timezone.utc)
                thresholds: dict[str, str] = {
                    "daily": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
                    "weekly": (now - timedelta(days=7)).strftime("%Y-%m-%d"),
                    "monthly": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S"),
                }

                result: dict[str, dict] = {}
                for period_label, cutoff in thresholds.items():
                    row = session.query(
                        func.coalesce(
                            func.sum(RequestModel.prompt_tokens + RequestModel.completion_tokens),
                            0,
                        ).label("tokens"),
                        func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                        func.count(RequestModel.id).label("requests"),
                    ).filter(
                        RequestModel.provider == "commandcode",
                        RequestModel.success == 1,
                        RequestModel.timestamp >= cutoff,
                    ).first()

                    if row:
                        result[period_label] = {
                            "tokens": int(row.tokens),
                            "cost": round(float(row.cost), 8),
                            "requests": int(row.requests),
                        }
                    else:
                        result[period_label] = {"tokens": 0, "cost": 0.0, "requests": 0}

            return result
        except Exception as exc:
            logger.warning("summary_query_failed", provider="commandcode", error=str(exc))
            return None

    # ── Subscription (from Command Code billing API) ──────────────────────

    def fetch_subscription(self) -> Optional[dict]:
        """Fetch subscription/usage snapshot from the Command Code billing API.

        Requires a Command Code browser session cookie — read from the
        encrypted credential store (UI-managed).  Returns::

            {"monthly_credits_remaining": 40.19, "purchased_credits": 0.0,
             "five_hour_pct": 10.5, "weekly_pct": 61.6, "monthly_pct": 42.6,
             "five_hour_reset_sec": 11520, "weekly_reset_sec": 172800,
             "plan_id": "individual-goat", "plan_status": "active",
             "usage_summary": {"total_tokens": ..., "total_runs": ...},
             "recent_runs": [...], ...}

        Returns ``{"_error": "auth_failed", "detail": "..."}`` when the cookie
        is missing/invalid, or ``{"_error": "api_error", "detail": "..."}`` on
        an API failure.
        """
        try:
            if os.environ.get("LCP_MOCK_PLUGIN_DATA"):
                return {
                    "monthly_credits_remaining": 40.19,
                    "purchased_credits": 0.0,
                    "premium_monthly_credits": 0.0,
                    "opensource_monthly_credits": 40.19,
                    "five_hour_pct": 10.5,
                    "five_hour_used": 1.47,
                    "five_hour_cap": 14.0,
                    "five_hour_reset_sec": 11520,
                    "five_hour_reset_at": "",
                    "weekly_pct": 61.6,
                    "weekly_used": 21.57,
                    "weekly_cap": 35.0,
                    "weekly_reset_sec": 172800,
                    "weekly_reset_at": "",
                    "monthly_pct": 42.6,
                    "monthly_used": 29.81,
                    "monthly_cap": 70.0,
                    "monthly_reset_sec": 0,
                    "monthly_reset_at": "",
                    "plan_id": "individual-goat",
                    "plan_status": "active",
                    "billing_period_end": None,
                    "usage_summary": {
                        "total_runs": 2204,
                        "completed_runs": 2204,
                        "failed_runs": 0,
                        "success_rate": 100.0,
                        "total_cost": 29.81,
                        "total_tokens": 624027635,
                        "total_tokens_in": 622439445,
                        "total_tokens_out": 1588190,
                        "total_monthly_credits": 29.81,
                        "period_basis": "billing-period",
                    },
                    "recent_runs": [],
                }
            from .commandcode_api import fetch_subscription_snapshot_dict
            cookie = ""
            # Cookie from the encrypted credential store (UI-managed)
            try:
                from ..credential_store import get_credential_store
                store = get_credential_store()
                if store is not None:
                    cookie = store.get_cookie("commandcode") or ""
            except Exception:
                cookie = ""
            if not cookie:
                logger.debug("commandcode_cookie_not_configured")
                return {"_error": "auth_failed",
                        "detail": "Command Code cookie not set — add it in the Usage tab"}
            data = fetch_subscription_snapshot_dict(cookie)
            if data is None:
                logger.warning("commandcode_subscription_fetch_returned_none")
                return {"_error": "auth_failed",
                        "detail": "Invalid or expired Command Code cookie, or API unreachable"}
            return data
        except Exception as exc:
            logger.warning("commandcode_subscription_fetch_failed", error=str(exc))
            return {"_error": "api_error", "detail": str(exc)}

    def credit_status(self, subscription: Optional[dict] = None,
                      balance: Optional[dict] = None) -> str:
        """Interpret cached Command Code payloads into a credit status.

        Command Code's "insufficient credits" can mean the MONTHLY plan credits
        are exhausted, the PURCHASED (top-up) credits are exhausted, or both.
        The account is drained when monthly + purchased remaining is <= $5.
        Unknown (no credit fields) is NOT drained.
        """
        if subscription:
            if subscription.get("_error"):
                return "drained"
            rem = subscription.get("monthly_credits_remaining")
            purchased = subscription.get("purchased_credits")
            if rem is None and purchased is None:
                return "unknown"
            total = 0.0
            if isinstance(rem, (int, float)):
                total += rem
            if isinstance(purchased, (int, float)):
                total += purchased
            return "drained" if total <= 5.0 else "funded"
        return "unknown"


# ── Auto-register ──────────────────────────────────────────────────────────
_registry = get_registry()
_registry.register(CommandCodeCostPlugin())
