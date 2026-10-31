"""HTTP client for Command Code's billing/usage API — credits + subscription.

Two surfaces exist; this module speaks to both and prefers the durable one:

1. **Key-authenticated ``/alpha/*``** (the Command Code CLI's own routes) —
   ``Authorization: Bearer <CMD_API_KEY>``. The credential is the provider API
   key, the same one ``/provider/v1`` uses, so it does **not** expire on its own:

       GET /alpha/billing/credits        -> credits + rolling window limits
       GET /alpha/billing/subscriptions  -> active subscription (plan/status)
       GET /alpha/usage/summary          -> billing-period totals (tokens/cost)
       GET /alpha/whoami?limits=1        -> user/org + model limits (unused here)

2. **Cookie-authenticated ``/internal/*``** (the Studio usage page) — a browser
   session cookie from a logged-in commandcode.ai session. Kept only as a
   fallback: this cookie is a session token that expires, and when it does the
   vendor card goes stale (2026-10-31: last good fetch 2026-10-28, then 401
   every 30 min for 3.5 days). ``/internal/*`` rejects the API key.

       GET /internal/billing/credits        -> credits summary + rolling limits
       GET /internal/billing/subscriptions  -> active subscription (plan/status)
       GET /internal/usage/summary          -> billing-period totals (tokens/cost)
       GET /internal/usage?limit=N          -> recent usage entries

The two surfaces return the same field names for credits/subscription/usage; the
differences are ``freeCredits`` in the alpha payload (the internal one splits the
grant into premium/open-source pools) and the absence of a per-run list on alpha
(``/alpha/usage`` is not a route), so ``recent_runs`` is only populated by the
cookie path.

Usage::

    from .commandcode_api import fetch_subscription_snapshot
    snap = fetch_subscription_snapshot(cookie, api_key=key)
    # => {"monthly_credits_remaining": 14.86, "purchased_credits": 0.0,
    #     "free_credits": 0.0,
    #     "five_hour_pct": 4.0, "weekly_pct": 64.1, "monthly_pct": 78.8,
    #     "five_hour_reset_sec": 14568, "weekly_reset_sec": 48131,
    #     "plan_id": "individual-goat", "plan_status": "active",
    #     "usage_summary": {"total_tokens": ..., "total_runs": ...},
    #     "credits_source": "alpha", "recent_runs": [...], ...}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from ..logging_config import get_logger

logger = get_logger("lcp.cost.commandcode_api")

_COMMANDCODE_BASE = "https://api.commandcode.ai"
_WEB_ORIGIN = "https://commandcode.ai"

# Key-authenticated surface (the Command Code CLI's own routes) — preferred.
_ALPHA_CREDITS_PATH = "/alpha/billing/credits"
_ALPHA_SUBSCRIPTIONS_PATH = "/alpha/billing/subscriptions"
_ALPHA_USAGE_SUMMARY_PATH = "/alpha/usage/summary"

# Cookie-authenticated surface (Studio usage page) — fallback only.
_CREDITS_PATH = "/internal/billing/credits"
_SUBSCRIPTIONS_PATH = "/internal/billing/subscriptions"
_USAGE_SUMMARY_PATH = "/internal/usage/summary"
_USAGE_PATH = "/internal/usage"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)

_TIMEOUT_SECONDS = 15

# ── Plan catalog ────────────────────────────────────────────────────────────
# Monthly usage-value (USD) per plan, used to derive monthly usage percentage
# from ``credits.monthlyCredits`` (remaining) vs the plan's total. Mirrors
# CodexBar's CommandCodePlanCatalog plan IDs.
_PLAN_MONTHLY_CAP: dict[str, float] = {
    "individual-go": 10.0,
    "individual-pro": 30.0,
    "individual-goat": 70.0,
    "individual-max": 150.0,
    "individual-ultra": 300.0,
}


@dataclass
class CommandCodeSubscriptionSnapshot:
    """Parsed billing/usage state from the Command Code web API."""
    monthly_credits_remaining: float = 0.0   # USD left in the monthly grant
    purchased_credits: float = 0.0           # top-up / pay-as-you-go balance
    free_credits: float = 0.0                # USD left in the free grant (/alpha only)
    premium_monthly_credits: float = 0.0     # USD left in premium grant
    opensource_monthly_credits: float = 0.0  # USD left in open-source grant
    # Rolling 5-hour window
    five_hour_pct: float = 0.0               # 0-100
    five_hour_used: float = 0.0
    five_hour_cap: float = 0.0
    five_hour_reset_sec: int = 0             # seconds until reset (countdown)
    # Rolling weekly window
    weekly_pct: float = 0.0                  # 0-100
    weekly_used: float = 0.0
    weekly_cap: float = 0.0
    weekly_reset_sec: int = 0                # seconds until reset (countdown)
    # Monthly window (derived from plan cap + remaining credits)
    monthly_pct: float = 0.0                 # 0-100
    monthly_used: float = 0.0
    monthly_cap: float = 0.0
    monthly_reset_sec: int = 0
    # Subscription
    plan_id: Optional[str] = None
    plan_status: Optional[str] = None
    billing_period_end: Optional[str] = None
    # Usage totals + recent runs (from the usage endpoints)
    usage_summary: dict = field(default_factory=dict)
    recent_runs: list = field(default_factory=list)
    # Which surface produced this snapshot: "alpha" (API key) or "internal" (cookie)
    source: str = ""


# ── Helpers ────────────────────────────────────────────────────────────────

def _base_headers(cookie: str) -> dict[str, str]:
    """Return common headers for billing API requests."""
    return {
        "Cookie": cookie.strip(),
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": _WEB_ORIGIN,
        "Referer": _WEB_ORIGIN + "/",
    }


def _api_key_headers(api_key: str) -> dict[str, str]:
    """Return common headers for the key-authenticated ``/alpha/*`` routes."""
    return {
        "Authorization": f"Bearer {api_key.strip()}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": _WEB_ORIGIN,
        "Referer": _WEB_ORIGIN + "/",
    }


def _http_get_json(url: str, headers: dict[str, str],
                   timeout: int = _TIMEOUT_SECONDS) -> dict:
    """Thin wrapper around urllib GET that parses a JSON object."""
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object, got {type(data).__name__}")
    return data


def _num(value, default: float = 0.0) -> float:
    """Coerce a JSON value (number or numeric string) to float."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int = 0) -> int:
    """Coerce a JSON value (number or numeric string) to int."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Credits parsing ────────────────────────────────────────────────────────

def _window_limits(payload: dict) -> dict:
    """Locate the ``windowLimits`` object — at the response root or nested
    inside ``credits`` (both shapes are observed in the wild)."""
    wl = payload.get("windowLimits")
    if isinstance(wl, dict) and wl:
        return wl
    credits = payload.get("credits")
    if isinstance(credits, dict):
        wl = credits.get("windowLimits")
        if isinstance(wl, dict) and wl:
            return wl
    return {}


def _window_pct(window: dict, now_ts: float) -> tuple[float, float, float, int]:
    """Parse one rolling window ``{used, cap, resetAt}`` into
    ``(pct, used, cap, reset_sec)``.

    ``resetAt`` is an absolute Unix epoch (seconds or milliseconds, number or
    string) — converted to a countdown of seconds until reset.
    """
    used = _num(window.get("used"))
    cap = _num(window.get("cap"))
    pct = round((used / cap * 100.0) if cap > 0 else 0.0, 1)
    pct = max(0.0, min(100.0, pct))

    reset_at = _num(window.get("resetAt"))
    reset_sec = 0
    if reset_at > 0:
        # Normalize ms → seconds (epochs beyond ~10B are milliseconds).
        epoch_sec = reset_at / 1000.0 if reset_at > 10_000_000_000 else reset_at
        reset_sec = int(max(0.0, epoch_sec - now_ts))
    return pct, used, cap, reset_sec


def _parse_credits(payload: dict) -> dict:
    """Extract credits + rolling-window fields from /internal/billing/credits.

    Expected shape (real response, 2026-08)::

        {"credits": {
            "monthlyCredits": 40.19,
            "purchasedCredits": 0,
            "premiumMonthlyCredits": 0,
            "opensourceMonthlyCredits": 40.19,
         },
         "windowLimits": {
            "limited": true, "exceeded": null,
            "fiveHour": {"used": 1.47, "cap": 14, "resetAt": 1787084876441},
            "weekly":   {"used": 21.57, "cap": 35, "resetAt": 1787424086179},
         }}

    ``windowLimits`` may alternatively be nested inside ``credits``.
    """
    credits = payload.get("credits") or {}
    now_ts = datetime.now(timezone.utc).timestamp()
    windows = _window_limits(payload)

    five_hour_pct, five_used, five_cap, five_reset = _window_pct(
        windows.get("fiveHour") or {}, now_ts)
    weekly_pct, weekly_used, weekly_cap, weekly_reset = _window_pct(
        windows.get("weekly") or {}, now_ts)

    return {
        "monthly_credits_remaining": _num(credits.get("monthlyCredits")),
        "purchased_credits": _num(credits.get("purchasedCredits")),
        # ``freeCredits`` only appears on the /alpha surface; the /internal one
        # splits the grant into premium/open-source pools instead.
        "free_credits": _num(credits.get("freeCredits")),
        "premium_monthly_credits": _num(credits.get("premiumMonthlyCredits")),
        "opensource_monthly_credits": _num(credits.get("opensourceMonthlyCredits")),
        "five_hour_pct": five_hour_pct,
        "five_hour_used": five_used,
        "five_hour_cap": five_cap,
        "five_hour_reset_sec": five_reset,
        "weekly_pct": weekly_pct,
        "weekly_used": weekly_used,
        "weekly_cap": weekly_cap,
        "weekly_reset_sec": weekly_reset,
    }


# ── Subscription parsing ───────────────────────────────────────────────────

def _parse_subscription(payload: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (plan_id, status, current_period_end) from /subscriptions.

    Expected shape::

        {"success": true, "data": {
            "id": "sub_...", "status": "active",
            "planId": "individual-goat", "currentPeriodEnd": "2026-09-08T...",
        }}

    ``data`` may be null when the user has no active subscription (free tier) —
    in that case all three fields are None and no error is raised.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, None, None
    plan_id = data.get("planId") or data.get("planID") or data.get("plan")
    status = data.get("status")
    period_end = data.get("currentPeriodEnd") or data.get("current_period_end")
    return plan_id, status, period_end


# ── Usage parsing ──────────────────────────────────────────────────────────

def _parse_usage_summary(payload: dict) -> dict:
    """Extract billing-period totals from /internal/usage/summary.

    Real shape (numeric tokens fields may be strings)::

        {"totalCount": 2204, "totalCost": 29.81, "averageCost": 0.0135,
         "successRate": 100, "completedCount": 2204, "failedCount": 0,
         "totalTokensIn": 622439445, "totalTokensOut": 1588190,
         "totalTokens": 624027635, "totalCredits": 29.81,
         "totalFreeCredits": 0, "totalMonthlyCredits": 29.81,
         "totalPurchasedCredits": 0, "periodBasis": "billing-period"}
    """
    return {
        "total_runs": _int(payload.get("totalCount")),
        "completed_runs": _int(payload.get("completedCount")),
        "failed_runs": _int(payload.get("failedCount")),
        "success_rate": _num(payload.get("successRate")),
        "total_cost": _num(payload.get("totalCost")),
        "average_cost": _num(payload.get("averageCost")),
        "total_tokens_in": _int(payload.get("totalTokensIn")),
        "total_tokens_out": _int(payload.get("totalTokensOut")),
        "total_tokens": _int(payload.get("totalTokens")),
        "total_credits": _num(payload.get("totalCredits")),
        "total_free_credits": _num(payload.get("totalFreeCredits")),
        "total_monthly_credits": _num(payload.get("totalMonthlyCredits")),
        "total_purchased_credits": _num(payload.get("totalPurchasedCredits")),
        "period_basis": payload.get("periodBasis") or "",
    }


def _parse_usage_list(payload: dict) -> list[dict]:
    """Extract recent usage entries from /internal/usage.

    Real shape::

        {"usages": [ { "createdAt": "...", "tokensIn": "137815",
            "tokensOut": "4716", "tokensTotal": "142531",
            "creditsTotal": "0.0125", "durationTotal": "82422",
            "status": "completed",
            "meta": {"model": "deepseek/deepseek-v4-pro", ...},
            "type": "api", "mode": "api", ... } ],
         "nextCursor": "...", "limit": 10, ...}
    """
    rows = []
    for u in payload.get("usages") or []:
        if not isinstance(u, dict):
            continue
        meta = u.get("meta") or {}
        rows.append({
            "created_at": u.get("createdAt") or "",
            "model": meta.get("model") or "",
            "tokens_in": _int(u.get("tokensIn")),
            "tokens_out": _int(u.get("tokensOut")),
            "tokens_total": _int(u.get("tokensTotal")),
            "credits_total": _num(u.get("creditsTotal")),
            "duration_total": _num(u.get("durationTotal")),
            "status": u.get("status") or "",
        })
    return rows


def _derive_monthly(plan_id: Optional[str], monthly_remaining: float) -> tuple[float, float, float]:
    """Derive (monthly_pct, monthly_used, monthly_cap) from plan + remaining.

    Monthly usage value = plan's total monthly cap minus remaining credits,
    clamped to [0, cap]. When the plan is unknown, ``cap`` is 0 (UI hides it).
    """
    cap = _PLAN_MONTHLY_CAP.get(plan_id or "", 0.0)
    if cap <= 0:
        return 0.0, 0.0, 0.0
    used = max(0.0, min(cap, cap - monthly_remaining))
    pct = round(used / cap * 100.0, 1) if cap > 0 else 0.0
    return max(0.0, min(100.0, pct)), used, cap


# ── Public API ─────────────────────────────────────────────────────────────

def fetch_subscription_snapshot(
    cookie: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[CommandCodeSubscriptionSnapshot]:
    """Fetch Command Code billing/usage data, preferring the durable surface.

    The API key against ``/alpha/*`` is tried first — that credential is the
    provider API key and does not expire on its own, so the vendor card cannot
    go stale behind it. The browser cookie against ``/internal/*`` is used only
    when the key is absent or rejected. Returns ``None`` when neither credential
    is usable (missing, rejected, or the API is unreachable).
    """
    key = (api_key or "").strip()
    if key:
        snap = _fetch_alpha_snapshot(key)
        if snap is not None:
            return snap
        logger.warning("commandcode_alpha_unavailable_falling_back_to_cookie")

    cookie = (cookie or "").strip()
    if cookie:
        return _fetch_internal_snapshot(cookie)

    logger.debug("commandcode_credentials_missing")
    return None


def _fetch_internal_snapshot(cookie: str) -> Optional[CommandCodeSubscriptionSnapshot]:
    """Cookie-authenticated ``/internal/*`` fetch — the legacy fallback path.

    Returns ``None`` when the credential is rejected, unreachable, or the
    response cannot be parsed.
    """
    headers = _base_headers(cookie)

    # ── Fetch credits (primary) ────────────────────────────────────────
    try:
        credits_url = _COMMANDCODE_BASE + _CREDITS_PATH
        credits_payload = _http_get_json(credits_url, headers)
    except HTTPError as exc:
        if exc.code in (401, 403):
            logger.warning("commandcode_auth_failed", status=exc.code)
            return None
        logger.warning("commandcode_credits_http_error", status=exc.code)
        return None
    except (URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("commandcode_credits_fetch_failed", error=str(exc))
        return None

    try:
        credits = _parse_credits(credits_payload)
    except Exception as exc:
        logger.warning("commandcode_credits_parse_failed", error=str(exc))
        return None

    # ── Fetch subscription (best-effort enrichment) ────────────────────
    plan_id = plan_status = period_end = None
    try:
        sub_url = _COMMANDCODE_BASE + _SUBSCRIPTIONS_PATH
        sub_payload = _http_get_json(sub_url, headers)
        plan_id, plan_status, period_end = _parse_subscription(sub_payload)
    except Exception as exc:
        # Subscription enrichment is optional — credits still usable.
        logger.warning("commandcode_subscription_fetch_failed", error=str(exc))

    # ── Fetch usage totals (best-effort) ───────────────────────────────
    usage_summary: dict = {}
    recent_runs: list = []
    try:
        summary_url = _COMMANDCODE_BASE + _USAGE_SUMMARY_PATH
        usage_summary = _parse_usage_summary(_http_get_json(summary_url, headers))
    except Exception as exc:
        logger.warning("commandcode_usage_summary_fetch_failed", error=str(exc))
    try:
        usage_url = _COMMANDCODE_BASE + _USAGE_PATH + "?limit=10"
        recent_runs = _parse_usage_list(_http_get_json(usage_url, headers))
    except Exception as exc:
        logger.warning("commandcode_usage_list_fetch_failed", error=str(exc))

    # ── Derive monthly usage % ─────────────────────────────────────────
    monthly_pct, monthly_used, monthly_cap = _derive_monthly(
        plan_id, credits["monthly_credits_remaining"])
    monthly_reset_sec = _period_end_countdown(period_end)

    return _build_snapshot(credits, monthly_pct, monthly_used, monthly_cap,
                           monthly_reset_sec, plan_id, plan_status, period_end,
                           usage_summary, recent_runs, "internal")


def _fetch_alpha_snapshot(api_key: str) -> Optional[CommandCodeSubscriptionSnapshot]:
    """Key-authenticated ``/alpha/*`` fetch — the preferred, non-expiring path.

    Authenticates with the Command Code provider API key (``Authorization:
    Bearer``), the same credential ``/provider/v1`` uses. The ``/alpha/usage``
    per-run list is not a route on this surface, so ``recent_runs`` stays empty.
    Returns ``None`` when the key is rejected or the API is unreachable, which
    lets the caller fall back to the cookie path.
    """
    headers = _api_key_headers(api_key)

    # ── Fetch credits (primary) ────────────────────────────────────────
    try:
        credits_url = _COMMANDCODE_BASE + _ALPHA_CREDITS_PATH
        credits_payload = _http_get_json(credits_url, headers)
    except HTTPError as exc:
        if exc.code in (401, 403):
            logger.warning("commandcode_alpha_auth_failed", status=exc.code)
            return None
        logger.warning("commandcode_alpha_credits_http_error", status=exc.code)
        return None
    except (URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("commandcode_alpha_credits_fetch_failed", error=str(exc))
        return None

    try:
        credits = _parse_credits(credits_payload)
    except Exception as exc:
        logger.warning("commandcode_alpha_credits_parse_failed", error=str(exc))
        return None

    # ── Fetch subscription (best-effort enrichment) ────────────────────
    plan_id = plan_status = period_end = None
    try:
        sub_url = _COMMANDCODE_BASE + _ALPHA_SUBSCRIPTIONS_PATH
        sub_payload = _http_get_json(sub_url, headers)
        plan_id, plan_status, period_end = _parse_subscription(sub_payload)
    except Exception as exc:
        logger.warning("commandcode_alpha_subscription_fetch_failed", error=str(exc))

    # ── Fetch usage totals (best-effort) ───────────────────────────────
    usage_summary: dict = {}
    try:
        summary_url = _COMMANDCODE_BASE + _ALPHA_USAGE_SUMMARY_PATH
        usage_summary = _parse_usage_summary(_http_get_json(summary_url, headers))
    except Exception as exc:
        logger.warning("commandcode_alpha_usage_summary_fetch_failed", error=str(exc))

    # ── Derive monthly usage % ─────────────────────────────────────────
    monthly_pct, monthly_used, monthly_cap = _derive_monthly(
        plan_id, credits["monthly_credits_remaining"])
    monthly_reset_sec = _period_end_countdown(period_end)

    return _build_snapshot(credits, monthly_pct, monthly_used, monthly_cap,
                           monthly_reset_sec, plan_id, plan_status, period_end,
                           usage_summary, [], "alpha")


def _period_end_countdown(period_end: Optional[str]) -> int:
    """Seconds until the billing period ends (0 when unknown/unparseable)."""
    if not period_end:
        return 0
    try:
        end = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return int(max(0.0, (end - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, AttributeError):
        return 0


def _build_snapshot(credits: dict, monthly_pct: float, monthly_used: float,
                    monthly_cap: float, monthly_reset_sec: int,
                    plan_id: Optional[str], plan_status: Optional[str],
                    period_end: Optional[str], usage_summary: dict,
                    recent_runs: list,
                    source: str) -> CommandCodeSubscriptionSnapshot:
    """Assemble a snapshot from parsed credits + optional enrichment."""
    return CommandCodeSubscriptionSnapshot(
        monthly_credits_remaining=credits["monthly_credits_remaining"],
        purchased_credits=credits["purchased_credits"],
        free_credits=credits.get("free_credits", 0.0),
        premium_monthly_credits=credits["premium_monthly_credits"],
        opensource_monthly_credits=credits["opensource_monthly_credits"],
        five_hour_pct=credits["five_hour_pct"],
        five_hour_used=credits["five_hour_used"],
        five_hour_cap=credits["five_hour_cap"],
        five_hour_reset_sec=credits["five_hour_reset_sec"],
        weekly_pct=credits["weekly_pct"],
        weekly_used=credits["weekly_used"],
        weekly_cap=credits["weekly_cap"],
        weekly_reset_sec=credits["weekly_reset_sec"],
        monthly_pct=monthly_pct,
        monthly_used=monthly_used,
        monthly_cap=monthly_cap,
        monthly_reset_sec=monthly_reset_sec,
        plan_id=plan_id,
        plan_status=plan_status,
        billing_period_end=period_end,
        usage_summary=usage_summary,
        recent_runs=recent_runs,
        source=source,
    )


def fetch_subscription_snapshot_dict(cookie: Optional[str] = None,
                                    api_key: Optional[str] = None) -> Optional[dict]:
    """Same as ``fetch_subscription_snapshot`` but returns a plain dict (or None)."""
    snap = fetch_subscription_snapshot(cookie, api_key=api_key)
    if snap is None:
        return None
    now = datetime.now(timezone.utc)

    def _reset_at(sec: int) -> str:
        if sec:
            return (now + timedelta(seconds=sec)).strftime("%b %d, %H:%M")
        return ""

    return {
        "monthly_credits_remaining": snap.monthly_credits_remaining,
        "purchased_credits": snap.purchased_credits,
        "free_credits": snap.free_credits,
        "premium_monthly_credits": snap.premium_monthly_credits,
        "opensource_monthly_credits": snap.opensource_monthly_credits,
        "five_hour_pct": snap.five_hour_pct,
        "five_hour_used": snap.five_hour_used,
        "five_hour_cap": snap.five_hour_cap,
        "five_hour_reset_sec": snap.five_hour_reset_sec,
        "five_hour_reset_at": _reset_at(snap.five_hour_reset_sec),
        "weekly_pct": snap.weekly_pct,
        "weekly_used": snap.weekly_used,
        "weekly_cap": snap.weekly_cap,
        "weekly_reset_sec": snap.weekly_reset_sec,
        "weekly_reset_at": _reset_at(snap.weekly_reset_sec),
        "monthly_pct": snap.monthly_pct,
        "monthly_used": snap.monthly_used,
        "monthly_cap": snap.monthly_cap,
        "monthly_reset_sec": snap.monthly_reset_sec,
        "monthly_reset_at": _reset_at(snap.monthly_reset_sec),
        "plan_id": snap.plan_id,
        "plan_status": snap.plan_status,
        "billing_period_end": snap.billing_period_end,
        "usage_summary": snap.usage_summary,
        "recent_runs": snap.recent_runs,
        # Which surface answered: "alpha" (API key, durable) or "internal"
        # (browser cookie, expires). Lets a deploy check prove the fix is live.
        "credits_source": snap.source,
    }
