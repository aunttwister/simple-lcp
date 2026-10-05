"""Cost tracking plugin for OpenCode.

Uses the gateway's own ``requests`` table for cost history (every routed
request is logged there with ``provider='opencode'``) and optionally polls
the OpenCode web API for subscription usage (5-hour / weekly percentages
and reset countdowns) using the UI-managed cookie + workspace ID from the
encrypted credential store.

Pricing is the same as DeepSeek (OpenCode uses deepseek models under the
hood).  The plugin also reports the free OpenCode-hosted models as zero-cost.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func

from ..logging_config import get_logger
from .base import CostPlugin, get_registry, select_rates

logger = get_logger("lcp.cost.opencode")

# ── Pricing ─────────────────────────────────────────────────────────────────
# OpenCode resells the same DeepSeek models, so these rates mirror
# src/api/cost_plugins/deepseek.py. Every spelling of DeepSeek-V4.1-Flash —
# `deepseek-flash` (current API name), `deepseek-v4-flash` and
# `deepseek-v4-flash-vision-exp` (legacy, served by the same model), and
# `deepseek-v4.1-flash` (the gateway's benchmark key) — shares ONE entry, so
# the model cannot be priced differently depending on which of its names a
# provider happened to use. Verified against
# https://api-docs.deepseek.com/quick_start/pricing on 2026-10-05 (off-peak).
_FLASH_PRICING: dict[str, float] = {
    "cache_hit": 0.003,
    "cache_miss": 0.15,
    "output": 0.6,
    "peak_cache_hit": 0.006,
    "peak_cache_miss": 0.30,
    "peak_output": 1.20,
}

_OPENCODE_PRICING: dict[str, dict[str, float]] = {
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
    "deepseek-flash": dict(_FLASH_PRICING),
    "deepseek-v4-flash": dict(_FLASH_PRICING),
    "deepseek-v4.1-flash": dict(_FLASH_PRICING),
    "deepseek-v4-flash-vision-exp": dict(_FLASH_PRICING),
}

_FREE_MODELS = frozenset({
    "qwen3-coder",
    "glm-4.7-free",
    "minimax-m2.1-free",
})


def _map_account_credits(payload: Any) -> Optional[dict]:
    """Map ``GET /api/billing/account`` onto the balance payload shape.

    The console quotes money in micro-cents (fixed point, 1e-8 USD) — the same
    unit the retired billing page used.  Returns ``None`` when no credits field
    is present, so an upstream shape change reports "unrecognised" rather than a
    fabricated number.
    """
    from .opencode_api import micro_cents_to_usd

    if not isinstance(payload, dict):
        return None
    source = payload.get("account") if isinstance(payload.get("account"), dict) else payload

    def _first_usd(*keys: str) -> Optional[float]:
        for key in keys:
            if key in source:
                value = micro_cents_to_usd(source.get(key))
                if value is not None:
                    return value
        return None

    available = _first_usd("availableMicroCents", "available_micro_cents",
                          "availableCreditMicroCents")
    balance = _first_usd("balanceMicroCents", "balance_micro_cents")
    if available is None and balance is None:
        return None
    credits = available if available is not None else balance

    plan = (source.get("plan") or source.get("planName")
            or source.get("mode") or source.get("billingMode"))
    if plan is None and isinstance(source.get("subscription"), dict):
        plan = source["subscription"].get("plan") or source["subscription"].get("name")

    return {
        "available_credits": credits,
        "balance": credits,
        "currency": source.get("currency") or "USD",
        "plan": plan,
        "billing_mode": source.get("billingMode"),
        # Null on a pay-as-you-go account: there is no plan ceiling to show.
        "credit_limit_usd": _first_usd("creditLimitMicroCents",
                                       "credit_limit_micro_cents"),
        "workspace_id": source.get("workspaceId") or source.get("workspace_id"),
    }


class OpenCodeCostPlugin(CostPlugin):
    """Cost tracking for OpenCode.

    Cost history comes from the gateway ``requests`` table (single source
    of truth for every routed request).  Subscription usage (usage % and
    reset countdowns) comes from the OpenCode web API using the UI-managed
    cookie + workspace ID from the encrypted credential store.

    Requires a SQLAlchemy *engine* for gateway DB queries — pass it to the
    constructor or call ``set_engine()`` before any query methods are used.
    """

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    def set_engine(self, engine: Any) -> None:
        """Bind the gateway SQLAlchemy engine for DB queries."""
        self._engine = engine

    # ── Identity ───────────────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "opencode"

    @property
    def preset(self) -> Optional[dict]:
        return {
            # The inference base moved /zen/go/v1 -> /inference/openai/v1; the
            # old value 429s ("Go usage limit, monthly") and 400s without an
            # x-opencode-session header.
            "api_base": "https://opencode.ai/inference/openai/v1",
            "models": self.get_supported_models(),
        }

    def get_supported_models(self) -> list[str]:
        return list(_OPENCODE_PRICING.keys()) + sorted(_FREE_MODELS)

    # ── Credentials ────────────────────────────────────────────────────────

    def _token(self, *, session: bool = False) -> str:
        """Return a console bearer token from the encrypted credential store.

        ``session=False`` → the OpenCode service API key (inference + usage
        routes when its permission is ``all``).
        ``session=True``  → a console session token minted by the OAuth
        authorization-code flow, which is what the credits route needs.
        Never raises: an unreadable store degrades to "no token".
        """
        name = "opencode_console" if session else "opencode"
        try:
            from ..credential_store import get_credential_store
            store = get_credential_store()
            if store is None:
                return ""
            return store.get(name) or ""
        except Exception:  # noqa: BLE001 — credential reads must never raise
            return ""

    # ── Model discovery ────────────────────────────────────────────────────

    def discover_models(self, api_base: str) -> Optional[list[dict]]:
        """Return this provider's model catalog.

        OpenCode serves **no** ``/models`` route on its inference base, so the
        generic discovery path (``{api_base}/models``) always 404s — that is
        why "Discover Models" fails for this provider.  The authoritative
        catalog is the console config's per-provider ``whitelist``.

        ``api_base`` is accepted for interface parity with the other plugins
        and intentionally unused: the catalog lives on the console host, not
        on the inference base.

        Returns ``None`` (→ caller falls back to generic discovery) when no key
        is configured or the catalog is empty.
        """
        token = self._token()
        if not token:
            logger.debug("opencode_discover_no_key")
            return None
        try:
            from .opencode_api import ConsoleApiError, console_model_ids
            ids = console_model_ids(token)
        except ConsoleApiError as exc:
            logger.warning(
                "opencode_discover_api_error",
                status=exc.status,
                detail=exc.detail or exc.tag,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — discovery must not break CRUD
            logger.warning("opencode_discover_failed", error=str(exc))
            return None
        if not ids:
            logger.warning("opencode_discover_empty_catalog")
            return None
        logger.info("opencode_models_discovered", count=len(ids))
        return [{"id": model_id} for model_id in ids]

    # ── Pricing ────────────────────────────────────────────────────────────

    def get_pricing(self, model: str) -> Optional[dict]:
        if model in _FREE_MODELS:
            return {"cache_hit": 0.0, "cache_miss": 0.0, "output": 0.0}
        return _OPENCODE_PRICING.get(model)

    def calculate_cost(self, model: str, usage: dict) -> Optional[float]:
        """Calculate cost using OpenCode/DeepSeek pricing."""
        if model in _FREE_MODELS:
            return 0.0

        pricing = select_rates(_OPENCODE_PRICING.get(model), usage.get("__ts"))
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
        """Return the engine or raise a clear error."""
        if self._engine is None:
            raise RuntimeError(
                "OpenCode plugin has no gateway engine — call set_engine() "
                "or pass engine= to the constructor before querying."
            )
        return self._engine

    def _gw_session(self):
        """Context manager returning a session bound to the gateway engine."""
        from ..models import get_session
        return get_session(self._ensure_engine())

    # ── Usage history (from gateway requests table) ────────────────────────

    def fetch_usage(self,
                    start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> list[dict]:
        """Return daily aggregates for opencode from the gateway DB."""
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
                    RequestModel.provider == "opencode",
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
                    "provider": r.provider or "opencode",
                    "prompt_tokens": int(r.prompt_tokens),
                    "completion_tokens": int(r.completion_tokens),
                    "cache_hit_tokens": int(r.cache_hit_tokens),
                    "cache_miss_tokens": int(r.cache_miss_tokens),
                    "cost": round(float(r.cost), 8),
                    "request_count": int(r.request_count),
                })

            logger.debug("usage_fetched", days=len(result))
            return result
        except Exception as exc:
            logger.warning("usage_query_failed", error=str(exc))
            return []

    # ── Balance / available credits (from OpenCode billing page) ─────────

    def fetch_balance(self) -> Optional[dict]:
        """Fetch available credits from the OpenCode console.

        Credits live behind ``GET /api/billing/account``, which accepts only a
        console **session** token — a service API key is rejected with 403 even
        with permissions ``all`` (verified 2026-09-26).  The session comes from
        the one-time authorization-code flow in :mod:`.console_oauth`.

        The previous implementation scraped the billing page's SSR payload with
        the ``auth`` cookie.  That page no longer exists — it returns the same
        1565-byte client shell as every other console route — so the scrape
        could only ever fail; it is gone rather than left to report a misleading
        dead-cookie error.

        Returns::

            {"available_credits": 12.34, "balance": 12.34,
             "currency": "USD", "plan": "go", "workspace_id": "wrk_..."}

        ``None`` when the provider holds no credential at all (stays quiet), or
        an error dict (``{"_error": ..., "detail": ...}``) when the fetch fails.
        """
        try:
            if os.environ.get("LCP_MOCK_PLUGIN_DATA"):
                return {
                    "available_credits": 12.34, "balance": 12.34,
                    "currency": "USD", "plan": "pro",
                    "workspace_id": "wrk_mock", "fetched_at": None,
                }
            from .opencode_api import ConsoleApiError, fetch_account_credits
            from .console_oauth import current_access_token, resolve_org_id

            token = current_access_token()
            if not token:
                if not self._token():
                    logger.debug("opencode_not_configured")
                    return None  # plugin "doesn't support balance" → stays quiet
                logger.info("opencode_console_session_missing")
                return {
                    "_error": "auth_failed",
                    "detail": (
                        "OpenCode credits need a console session (one-time "
                        "browser approval): run "
                        "`python -m api.cost_plugins.console_oauth start`"
                    ),
                }
            try:
                payload = fetch_account_credits(token, org_id=resolve_org_id(token))
            except ConsoleApiError as exc:
                logger.warning("opencode_credits_api_error", status=exc.status,
                               detail=exc.detail or exc.tag)
                return {
                    "_error": "auth_failed",
                    "detail": f"console API HTTP {exc.status} on /api/billing/status",
                }
            mapped = _map_account_credits(payload)
            if mapped is None:
                logger.warning("opencode_credits_unmapped",
                               keys=sorted(payload)[:12] if isinstance(payload, dict) else None)
                return {"_error": "api_error",
                        "detail": "unrecognised /api/billing/status payload shape"}
            return mapped
        except Exception as exc:
            logger.warning("billing_fetch_failed", error=str(exc))
            return {"_error": "api_error", "detail": str(exc)}

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
                        RequestModel.provider == "opencode",
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
            logger.warning("summary_query_failed", error=str(exc))
            return None

    # ── Subscription (from OpenCode web API) ───────────────────────────────

    def fetch_subscription(self) -> Optional[dict]:
        """Plan-window usage for the OpenCode vendor card.

        Primary source is the **Go plan's own endpoint**, which lives on the
        inference host and takes the same Bearer provider key as inference —
        no console session, no ``x-org-id``::

            GET https://opencode.ai/inference/go/v1/usage
            {"usage": {"rolling": {"status","percent","resetsAt"},
                       "weekly":  {...}, "monthly": {...}}}

        That is the plan this plugin primarily tracks, and it is the only place
        the rolling-5h/weekly/monthly windows exist: the console exposes no
        quota route at all (``/api/usage/{limits,quota,windows,current}} → 404)
        and ``creditLimitMicroCents`` is null on this prepaid org.

        Falls back to the console budget rows, then to the console's real usage
        totals, so a key without Go access still shows something honest.
        """
        if os.environ.get("LCP_MOCK_PLUGIN_DATA"):
            return {
                "monthly_pct": 12.0, "monthly_reset_sec": 1209600,
                "rolling_pct": 17.0, "rolling_reset_sec": 5944,
                "weekly_pct": 75.0, "weekly_reset_sec": 278201,
            }
        from .opencode_api import (
            ConsoleApiError,
            fetch_budget_members,
            fetch_go_usage,
            plan_windows,
            plan_windows_from_go,
        )

        # ── 1. Go plan windows (provider key; survives console-token expiry) ──
        key = self._token()
        if key:
            try:
                windows = plan_windows_from_go(fetch_go_usage(key) or {})
            except ConsoleApiError as exc:
                logger.info("opencode_go_usage_unavailable", status=exc.status)
                windows = {}
            except Exception as exc:  # noqa: BLE001
                logger.info("opencode_go_usage_error", error=str(exc))
                windows = {}
            if windows:
                windows["source"] = "go-usage"
                # Enrich with the console's real totals when a session exists:
                # the month-to-date figures live only on the console API, while
                # the plan windows live only on the Go endpoint.  Without a
                # session the bars still render — the numbers are just absent.
                try:
                    from .console_oauth import current_access_token

                    session = current_access_token()
                    numbers = self._console_usage_numbers(session) if session else {}
                except Exception as exc:  # noqa: BLE001
                    logger.info("opencode_usage_enrich_failed", error=str(exc))
                    numbers = {}
                for poison in ("_error", "detail", "limit_available", "source"):
                    numbers.pop(poison, None)
                windows.update(numbers)
                # Provenance: name both sources when the console contributed.
                windows["source"] = "go-usage+console" if numbers else "go-usage"
                # The bars exist, so this is a measured plan, not a missing one.
                windows["limit_available"] = True
                return windows
        else:
            logger.info("opencode_go_usage_no_key")

        from .console_oauth import current_access_token

        token = current_access_token()
        # ── 2. Console budget rows (``/api/v1/*`` is service-key only) ───────
        if token:
            windows = {}
            try:
                windows = plan_windows(fetch_budget_members(token))
            except ConsoleApiError as exc:
                logger.info("opencode_budget_route_unavailable", status=exc.status)
            except Exception as exc:  # noqa: BLE001
                logger.info("opencode_budget_route_error", error=str(exc))
            if windows:
                return windows
        else:
            logger.info("opencode_console_session_missing")

        # ── 3. No session at all → actionable reason ─────────────────────────
        if not token:
            return {
                "_error": "auth_failed",
                "detail": (
                    "OpenCode credits need a console session (one-time "
                    "browser approval): run "
                    "`python -m api.cost_plugins.console_oauth start`"
                ),
            }

        # No plan ceiling exists on this account (``creditLimitMicroCents`` is
        # null), so report the vendor's real usage numbers instead of inventing
        # a percentage.  The UI only draws bars for pct fields, so nothing is
        # fabricated downstream either.
        return self._console_usage_numbers(token)

    def _console_usage_numbers(self, token: str) -> Optional[dict]:
        """Vendor usage totals for the vendor card — ``GET /api/usage/summary``.

        Used when the account exposes no plan limit.  Returns real numbers
        (requests, tokens, cost) plus ``limit_available: False`` so the reader
        can tell "no bars" from "not measured".
        """
        from .console_oauth import resolve_org_id
        from .opencode_api import (
            ConsoleApiError,
            fetch_usage_summary,
            micro_cents_to_usd,
        )

        org_id = resolve_org_id(token)
        try:
            summary = fetch_usage_summary(token, "7d", org_id=org_id) or {}
        except ConsoleApiError as exc:
            logger.warning("opencode_usage_api_error", status=exc.status,
                           detail=exc.detail or exc.tag)
            return {
                "_error": "auth_failed",
                "detail": f"console API HTTP {exc.status} on /api/usage/summary",
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("opencode_usage_failed", error=str(exc))
            return {"_error": "api_error", "detail": str(exc)}

        def _count(key: str) -> int:
            try:
                return int(summary.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        payload = {
            "source": "console-usage",
            "range": "7d",
            "total_requests": _count("totalRequests"),
            "total_input_tokens": _count("totalInputTokens"),
            "total_output_tokens": _count("totalOutputTokens"),
            "total_cache_read_tokens": _count("totalCacheReadTokens"),
            "total_cost_usd": micro_cents_to_usd(summary.get("totalCostMicroCents")),
            "limit_available": False,
            "note": (
                "OpenCode exposes no plan limit (creditLimitMicroCents is null "
                "on a prepaid, pay-as-you-go org), so there is no monthly "
                "allowance and no percentage bars - spend stops only when the "
                "credit balance runs out."
            ),
        }
        payload.update(self._month_to_date(token, org_id))
        return payload

    def _month_to_date(self, token: str, org_id: str = "") -> dict:
        """Sum the current UTC calendar month from ``/api/usage/cost-by-day``.

        This is the only monthly dimension OpenCode offers: with no plan quota
        there is no "left this month" to report, but "used this month" is real.

        The console's range enum is {24h,7d,30d,all} with no month-aligned
        window (1m/mtd/month all 400), so the month is summed from daily rows.
        ``all`` and ``30d`` are unioned by date because on the last day of a
        31-day month neither window reaches back to the 1st on its own.
        """
        from .opencode_api import (
            ConsoleApiError,
            fetch_usage_cost_by_day,
            micro_cents_to_usd,
        )

        now = datetime.now(timezone.utc)
        month = now.strftime("%Y-%m")
        rows: dict[str, dict] = {}
        for range_ in ("all", "30d"):
            try:
                for row in fetch_usage_cost_by_day(token, range_, org_id=org_id) or []:
                    date = str((row or {}).get("date") or "")
                    if date.startswith(month):
                        rows[date] = row
            except ConsoleApiError as exc:
                logger.info("opencode_cost_by_day_unavailable", range=range_,
                            status=exc.status)
            except Exception as exc:  # noqa: BLE001
                logger.info("opencode_cost_by_day_error", range=range_,
                            error=str(exc))
        if not rows:
            return {}

        def _num(row: dict, key: str) -> int:
            try:
                return int(row.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return {
            "month_label": now.strftime("%b %Y"),
            "month_days": len(rows),
            "month_to_date_usd": micro_cents_to_usd(
                sum(_num(r, "totalCostMicroCents") for r in rows.values())),
            "month_requests": sum(_num(r, "totalRequests") for r in rows.values()),
            "month_tokens": sum(_num(r, "totalTokens") for r in rows.values()),
        }

    def credit_status(self, subscription: Optional[dict] = None,
                      balance: Optional[dict] = None) -> str:
        """Interpret cached OpenCode payloads into a credit status.

        Balance-first: an explicit available balance means FUNDED even when the
        monthly% heuristic would say drained — opencode can sit at 100% monthly
        yet still hold real dollars (e.g. $7.81 available), and must rank as
        funded. Without a balance, monthly_pct >= 95 is treated as drained.
        """
        if balance:
            avail = balance.get("available_credits")
            if avail is not None and avail > 1.0:
                return "funded"
            b = balance.get("balance")
            if isinstance(b, dict):
                a = b.get("available")
                if a is not None and a > 1.0:
                    return "funded"
        if subscription:
            if subscription.get("_error"):
                return "drained"
            mpct = subscription.get("monthly_pct")
            if mpct is not None and mpct >= 95:
                return "drained"
        if balance:
            b = balance.get("balance")
            if isinstance(b, dict):
                a = b.get("available")
                if a is not None and a <= 1.0:
                    return "drained"
            avail = balance.get("available_credits")
            if avail is not None and avail <= 1.0:
                return "drained"
        return "unknown"



# ── Auto-register ──────────────────────────────────────────────────────────
_registry = get_registry()
_registry.register(OpenCodeCostPlugin())
