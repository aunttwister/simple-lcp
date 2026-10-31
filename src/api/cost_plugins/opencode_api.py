"""HTTP client for OpenCode's web API — subscription & usage tracking.

Fetches the OpenCode /go page (SolidJS SSR) and extracts subscription usage
(rolling 5-hour / weekly / monthly percentages with reset countdowns).
Authenticates via the ``auth`` browser cookie from a logged-in session.

Usage::

    from .opencode_api import fetch_subscription
    snapshot = fetch_subscription(cookie, workspace_id=workspace_id)
    # => {"rolling_pct": 6.0, "weekly_pct": 26.0, "monthly_pct": 13.0,
    #     "rolling_reset_sec": 14213, "weekly_reset_sec": 197701,
    #     "monthly_reset_sec": 2483586}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from ..logging_config import get_logger

logger = get_logger("lcp.cost.opencode_api")

_OPENCODE_BASE = "https://opencode.ai"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)


@dataclass
class SubscriptionSnapshot:
    """Parsed subscription usage from the OpenCode web API."""
    rolling_pct: float = 0.0        # 0-100
    weekly_pct: float = 0.0         # 0-100
    monthly_pct: float = 0.0        # 0-100
    rolling_reset_sec: int = 0      # seconds until 5-hour window resets
    weekly_reset_sec: int = 0       # seconds until weekly window resets
    monthly_reset_sec: int = 0      # seconds until monthly window resets
    workspace_id: Optional[str] = None


@dataclass
class BillingSnapshot:
    """Parsed credits / balance from the OpenCode billing page."""
    available_credits: Optional[float] = None   # USD available now
    currency: str = "USD"
    plan: Optional[str] = None                  # e.g. "pro" / "go"
    workspace_id: Optional[str] = None
    fetched_at: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────

def _base_headers(cookie: str) -> dict[str, str]:
    """Return common headers for page requests."""
    return {
        "Cookie": cookie.strip(),
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Origin": _OPENCODE_BASE,
        "Referer": _OPENCODE_BASE,
    }


def _http_get(url: str, headers: dict[str, str], timeout: int = 15) -> str:
    """Thin wrapper around urllib GET."""
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── SSR parsing ────────────────────────────────────────────────────────────

_WRK_RE = re.compile(r'id\s*:\s*"(wrk_[a-zA-Z0-9]+)"', re.IGNORECASE)

# Match the full lite.subscription.get data in the $R SSR block.
_SUBSCRIBE_BLOCK_START_RE = re.compile(
    r'lite\.subscription\.get\["wrk_[^"]+"\]',
    re.DOTALL,
)

# Step 1: find each (rollingUsage|weeklyUsage|monthlyUsage):$R[N]={...} block.
_BLOCK_RE = re.compile(
    r'(rollingUsage|weeklyUsage|monthlyUsage)'
    r':\$R\[\d+\]=\{(.+?)\}',
)

# Step 2: extract resetInSec / usagePercent from inside a block (any order).
_RESET_RE = re.compile(r'(?:resetInSec|resetIn)\s*:\s*(-?\d+)')
_USAGE_PCT_RE = re.compile(
    r'(?:usagePercent|usagePct|usage|pct)\s*:\s*(\d+(?:\.\d+)?)',
)


def _extract_workspace_ids(text: str) -> list[str]:
    """Extract all ``wrk_*`` workspace IDs from SSR data."""
    return list(set(_WRK_RE.findall(text)))


def _parse_ssr_subscription(text: str) -> Optional[dict]:
    """Parse the SolidJS SSR $R block for subscription usage data.

    Looks for ``lite.subscription.get`` query result containing
    rollingUsage, weeklyUsage, monthlyUsage objects.
    """
    # Find the subscription.get block to scope our search.
    sub_start = _SUBSCRIBE_BLOCK_START_RE.search(text)
    if sub_start:
        end_pos = min(len(text), sub_start.end() + 20000)
        block = text[sub_start.start():end_pos]
    else:
        block = text

    # Collect per-window matches — two-step: find blocks, then extract values.
    result: dict = {}
    for m in _BLOCK_RE.finditer(block):
        window = m.group(1)  # rollingUsage, weeklyUsage, monthlyUsage
        body = m.group(2)    # content inside { ... }

        reset_m = _RESET_RE.search(body)
        pct_m = _USAGE_PCT_RE.search(body)
        if not reset_m or not pct_m:
            continue

        reset_sec = int(reset_m.group(1))
        pct = float(pct_m.group(1))

        if "rolling" in window:
            result["rolling_reset_sec"] = reset_sec
            result["rolling_pct"] = pct
        elif "weekly" in window:
            result["weekly_reset_sec"] = reset_sec
            result["weekly_pct"] = pct
        elif "monthly" in window:
            result["monthly_reset_sec"] = reset_sec
            result["monthly_pct"] = pct

    if "rolling_pct" in result and "rolling_reset_sec" in result:
        return result

    return None


# ── Absence classification ─────────────────────────────────────────────────
# When the /go page loads but yields no usage SSR data, these helpers decide
# WHY, so the caller can log a concise reason (and surface a specific message)
# instead of dumping the raw page.

# A rendered (authenticated) workspace shell that has NO active subscription —
# the subscription object is explicitly null on the OpenCode/Anomaly page.
_SUBSCRIPTION_NULL_MARKERS = (
    "subscription:null",
    "subscriptionid:null",
    "subscriptionplan:null",
    '"subscription":null',
    "litesubscriptionid:null",
)
# SolidJS SSR session/user markers present on an authenticated app shell.
_SSR_SESSION_MARKERS = ("session.get", "useremail", "workspaces[", "_$HY")
# Markers typical of a signed-out marketing / login wall.
_LOGIN_PAGE_MARKERS = (
    "sign in", "log in", "log-in", "login", "anoma.ly", "social-share.png",
)


def _classify_subscription_absence(text: str) -> tuple[str, str]:
    """Classify why a /go page contained no subscription usage data.

    Returns ``(reason, detail)`` with ``reason`` one of:
      - ``no_subscription`` — authenticated shell, subscription object is null
        (expired / not renewed); renew to restore usage bars.
      - ``auth``            — page looks like a signed-out / login wall; the
        auth cookie is missing or no longer valid.
      - ``parse``           — layout drift; nothing we recognise (default).
    """
    low = text.lower()
    if any(m in low for m in _SUBSCRIPTION_NULL_MARKERS):
        return (
            "no_subscription",
            "No active OpenCode subscription (expired or not renewed). "
            "Renew at opencode.ai to restore usage tracking.",
        )
    has_ssr_session = any(m in low for m in _SSR_SESSION_MARKERS)
    looks_like_login = any(m in low for m in _LOGIN_PAGE_MARKERS)
    if not has_ssr_session and looks_like_login:
        return (
            "auth",
            "OpenCode session is invalid or expired — refresh the auth "
            "cookie in the Usage tab.",
        )
    return (
        "parse",
        "OpenCode changed its usage page layout — subscription usage could "
        "not be read.",
    )


class OpenCodeSubscriptionUnavailable(Exception):
    """Raised when the /go page loaded but subscription data is absent.

    Carries a machine-readable ``reason`` (``no_subscription`` / ``auth`` /
    ``parse``) and a user-facing ``detail`` string.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


# ── Billing SSR parsing ────────────────────────────────────────────────────

# Field names seen/most-likely on the billing page's $R blocks. The exact
# shape can vary; we scan broadly and normalise below.
_BILLING_BLOCK_START_RE = re.compile(
    r'lite\.(?:billing|credits|workspace|billing\.get)\.get\["wrk_[^"]+"\]',
    re.DOTALL,
)

# Match `$R[N]={ ... }` blocks (SolidJS SSR). Multi-line, non-greedy.
_R_BLOCK_RE = re.compile(r'\$R\[\d+\]=\{(.*?)\}\s*;', re.DOTALL)

# Credit/balance-ish keys → captured numeric value (any order). Captures the
# field NAME so we can prefer unambiguous credit fields over generic ones.
_CREDIT_VALUE_RE = re.compile(
    r'(availableCredits|available_credits|creditsAvailable|creditBalance'
    r'|balance|available|totalCredits|total_credits|remainingCredits'
    r'|remaining|monthlyCredits|monthly_credits|credits)\s*:\s*'
    r'([+-]?\d+(?:\.\d+)?)'
)

# Plan name (string or quoted).
_PLAN_RE = re.compile(
    r'(?:plan|planId|plan_name|tier|currentPlan)\s*:\s*"?([A-Za-z0-9_ .-]+)"?'
)

# Unambiguous "available credits" field names — these win over generic
# ``balance``/``available``/``credits`` which may hold token counts, ledger
# totals, or other large numbers on the billing page.
_CREDIT_SPECIFIC_KEYS = frozenset({
    "availablecredits", "available_credits", "creditsavailable", "creditbalance",
})

# The live OpenCode billing page stores the available balance in the
# ``billing.get`` object as an INTEGER in 8-decimal fixed-point:
#   balance: 949260397  ==  $9.49260397  (949260397 * 1e-8)
# ``balance`` is therefore the real credit figure — just scaled — NOT a
# token/ledger number, so we scale it by 1e-8 into USD.
_BALANCE_FIXED_POINT = 1e-8

# Plausible upper bound for USD credit balances. Generic keys (available,
# credits) on the OpenCode billing page can carry huge non-USD numbers (token
# ledgers etc.); anything above this is treated as not-credits. (``balance``
# is handled separately — it is scaled, not rejected.)
_MAX_PLAUSIBLE_CREDITS = 1_000_000.0


def _parse_ssr_billing(text: str) -> Optional[dict]:
    """Parse the SolidJS SSR $R blocks for billing/credit data.

    Returns ``{"available_credits": float, "currency": "USD", "plan": str}``
    or ``None`` when nothing credit-like is found.

    Candidate values are collected across all ``$R[N]`` blocks (scoped to the
    billing block when present), then:
      1. unambiguous credit fields (``availableCredits``/``creditBalance``/…)
         are preferred,
      2. ``balance`` (the billing page's 8-decimal fixed-point credit balance)
         is scaled by 1e-8 and treated as a strong candidate,
      3. generic keys (``available``/``credits``) are a final fallback, with
         implausibly-large values (token ledgers) skipped.
    """
    if not text:
        return None

    # Scope the search to the billing block when present, else whole page.
    block_start = _BILLING_BLOCK_START_RE.search(text)
    if block_start:
        end_pos = min(len(text), block_start.end() + 20000)
        block = text[block_start.start():end_pos]
    else:
        block = text

    specific: list[float] = []
    balance_scaled: list[float] = []
    generic: list[float] = []
    skipped: list[tuple[str, float]] = []
    plan: Optional[str] = None

    def _scan(segment: str) -> None:
        nonlocal plan
        for m in _CREDIT_VALUE_RE.finditer(segment):
            key = m.group(1).lower()
            val = float(m.group(2))
            if val <= 0:
                continue
            if key in _CREDIT_SPECIFIC_KEYS:
                specific.append(val)
            elif key == "balance":
                # billing.get: 8-decimal fixed point → USD.
                balance_scaled.append(val * _BALANCE_FIXED_POINT)
            elif val > _MAX_PLAUSIBLE_CREDITS:
                skipped.append((key, val))
                continue
            else:
                generic.append(val)
        if plan is None:
            pm = _PLAN_RE.search(segment)
            if pm:
                plan = pm.group(1).strip()

    for m in _R_BLOCK_RE.finditer(block):
        _scan(m.group(1))
    # Top-level (non-$R) keys — some SSR shapes inline them.
    _scan(block)

    if skipped:
        logger.warning(
            "opencode_billing_skipped_implausible",
            keys=[(k, v) for k, v in skipped],
        )

    available: Optional[float] = None
    if specific:
        available = specific[-1]
    elif balance_scaled:
        available = balance_scaled[-1]
    elif generic:
        available = generic[-1]

    if available is None:
        _debug_billing_failure(block)
        return None

    # Currency is always USD on opencode.ai.
    return {
        "available_credits": round(available, 2),
        "currency": "USD",
        "plan": plan,
    }


def _debug_billing_failure(text: str) -> None:
    """Log a short snippet of the billing SSR text for diagnosis (debug only)."""
    for kw in ("availableCredits", "creditBalance", "balance", "credits",
               "plan", "billing"):
        idx = text.find(kw)
        if idx >= 0:
            start = max(0, idx - 120)
            snippet = text[start:idx + 200].replace("\n", "\\n").replace("\r", "")
            logger.debug("opencode_billing_ssr_context", keyword=kw, snippet=snippet)
            return
    logger.debug("opencode_billing_ssr_no_credits_found")


# ── Public API ─────────────────────────────────────────────────────────────

def fetch_billing(cookie: Optional[str], workspace_id: Optional[str] = None) -> Optional[BillingSnapshot]:
    """Fetch OpenCode available credits from the /workspace/{id}/billing page.

    ``workspace_id``, if provided, skips the discover step (preferred when
    the ID is already known, e.g. from the dashboard or env var).

    Returns a ``BillingSnapshot`` or ``None`` when the cookie is missing,
    unreachable, or no credit data is found.
    """
    if not cookie or not cookie.strip():
        logger.debug("opencode_cookie_missing")
        return None

    cookie = cookie.strip()
    headers = _base_headers(cookie)

    # ── Step 1: discover workspace ID ──────────────────────────────────
    if not workspace_id:
        try:
            raw = _http_get(_OPENCODE_BASE, headers)
            ids = _extract_workspace_ids(raw)
            if ids:
                workspace_id = ids[0]
        except (URLError, OSError) as exc:
            logger.warning("opencode_dashboard_fetch_failed", error=str(exc))
            return None

    if not workspace_id:
        logger.warning("opencode_no_workspace_found")
        return None

    # ── Step 2: fetch /billing page and parse SSR credit data ──────────
    try:
        headers["Referer"] = f"{_OPENCODE_BASE}/workspace/{workspace_id}"
        url = f"{_OPENCODE_BASE}/workspace/{workspace_id}/billing"
        raw = _http_get(url, headers)
    except (URLError, OSError) as exc:
        logger.warning("opencode_billing_page_fetch_failed", error=str(exc))
        return None

    result = _parse_ssr_billing(raw)
    if result is None:
        logger.warning(
            "opencode_billing_parse_failed",
            page_len=len(raw),
        )
        return None

    return BillingSnapshot(
        available_credits=result.get("available_credits"),
        currency=result.get("currency", "USD"),
        plan=result.get("plan"),
        workspace_id=workspace_id,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def fetch_billing_dict(cookie: Optional[str], workspace_id: Optional[str] = None) -> Optional[dict]:
    """Same as ``fetch_billing`` but returns a plain dict (or None)."""
    snap = fetch_billing(cookie, workspace_id=workspace_id)
    if snap is None:
        return None
    return {
        "available_credits": snap.available_credits,
        "balance": snap.available_credits,  # generic contract: {balance, currency}
        "currency": snap.currency,
        "plan": snap.plan,
        "workspace_id": snap.workspace_id,
        "fetched_at": snap.fetched_at,
    }

def fetch_subscription(cookie: Optional[str], workspace_id: Optional[str] = None) -> Optional[SubscriptionSnapshot]:
    """Fetch OpenCode subscription usage from the /go page SSR data.

    ``workspace_id``, if provided, skips the discover step (preferred when
    the ID is already known, e.g. from the dashboard or env var).

    Returns a ``SubscriptionSnapshot`` or ``None`` when the cookie is
    missing, unreachable, or no subscription data is found in the page.
    """
    if not cookie or not cookie.strip():
        logger.debug("opencode_cookie_missing")
        return None

    cookie = cookie.strip()
    headers = _base_headers(cookie)

    # ── Step 1: discover workspace ID ──────────────────────────────────
    # Priority: explicit argument → scrape as a fallback
    if not workspace_id:
        try:
            raw = _http_get(_OPENCODE_BASE, headers)
            ids = _extract_workspace_ids(raw)
            if ids:
                workspace_id = ids[0]
        except (URLError, OSError) as exc:
            logger.warning("opencode_dashboard_fetch_failed", error=str(exc))
            return None

    if not workspace_id:
        logger.warning("opencode_no_workspace_found")
        return None

    # ── Step 2: fetch /go page and parse SSR subscription data ──────────
    try:
        headers["Referer"] = f"{_OPENCODE_BASE}/workspace/{workspace_id}"
        url = f"{_OPENCODE_BASE}/workspace/{workspace_id}/go"
        raw = _http_get(url, headers)
    except (URLError, OSError) as exc:
        logger.warning("opencode_go_page_fetch_failed", error=str(exc))
        return None

    result = _parse_ssr_subscription(raw)
    if result is None:
        reason, detail = _classify_subscription_absence(raw)
        logger.warning(
            "opencode_subscription_unavailable",
            reason=reason,
            page_len=len(raw),
        )
        if reason in ("no_subscription", "auth"):
            raise OpenCodeSubscriptionUnavailable(reason, detail)
        return None

    # Heuristic: if percent < 1, multiply by 100 (fraction → percentage).
    # OpenCode's ``usagePercent`` is already a percentage integer/float;
    # true fractions like 0.5 or 0.01 need scaling, but 1.0 is just 1%.
    for key in ("rolling_pct", "weekly_pct", "monthly_pct"):
        val = result.get(key, 0)
        if 0 < val < 1:
            result[key] = val * 100

    return SubscriptionSnapshot(
        rolling_pct=round(result.get("rolling_pct", 0), 1),
        weekly_pct=round(result.get("weekly_pct", 0), 1),
        monthly_pct=round(result.get("monthly_pct", 0), 1),
        rolling_reset_sec=result.get("rolling_reset_sec", 0),
        weekly_reset_sec=result.get("weekly_reset_sec", 0),
        monthly_reset_sec=result.get("monthly_reset_sec", 0),
        workspace_id=workspace_id,
    )


def fetch_subscription_dict(cookie: Optional[str], workspace_id: Optional[str] = None) -> Optional[dict]:
    """Same as ``fetch_subscription`` but returns a plain dict (or None)."""
    snap = fetch_subscription(cookie, workspace_id=workspace_id)
    if snap is None:
        return None
    now = datetime.now(timezone.utc)
    def _reset_at(sec: int) -> str:
        if sec:
            return (now + timedelta(seconds=sec)).strftime("%b %d, %H:%M")
        return ""
    return {
        "rolling_pct": snap.rolling_pct,
        "weekly_pct": snap.weekly_pct,
        "monthly_pct": snap.monthly_pct,
        "rolling_reset_sec": snap.rolling_reset_sec,
        "weekly_reset_sec": snap.weekly_reset_sec,
        "monthly_reset_sec": snap.monthly_reset_sec,
        "rolling_reset_at": _reset_at(snap.rolling_reset_sec),
        "weekly_reset_at": _reset_at(snap.weekly_reset_sec),
        "monthly_reset_at": _reset_at(snap.monthly_reset_sec),
        "workspace_id": snap.workspace_id,
    }


# ── Console JSON API (service key / console session token) ──────────────────
#
# Everything the console SPA renders comes from JSON routes under
# ``console.opencode.ai/api/*``.  The server-rendered ``/workspace/<wrk>/…``
# pages that the SSR parsers above target no longer exist — they now return
# the 1565-byte client shell — so nothing below scrapes HTML.
#
# Auth is ``Authorization: Bearer <token>`` where the token is either
#   * a service API key (``sk-…`` / ``oc_sk_…``) with permissions ``all``,
#     which reaches the usage routes, or
#   * a console session token from the OAuth authorization-code flow
#     (``console_oauth.py``), which additionally reaches the credits route.
#
# A 403 is "authenticated but not permitted" and is classified separately from
# a 401 so a permissions gap is never reported as a dead credential.

CONSOLE_API_BASE = "https://console.opencode.ai"
_CONSOLE_API = CONSOLE_API_BASE + "/api"
# The Go plan's own usage windows live on the inference host, NOT the console
# API, and take the same Bearer provider key as inference — no session.  The
# console's proxy table maps /zen/go/v1/usage to /go/v1/usage; both answer.
GO_USAGE_API = "https://opencode.ai/inference/go/v1/usage"
# Measured against the live console 2026-09-26: 1d/14d/60d/1m/mtd/month/365d
# all answer HTTP 400 — these four are the whole accepted set.
USAGE_RANGES = ("24h", "7d", "30d", "all")
MICRO_CENTS = 1e-8  # console money unit → USD (fixed-point, matches _BALANCE_FIXED_POINT)

# Org-scoped console routes (billing, usage) reject a session token on its own:
# they answer 400 {"_tag":"OrgRequired","message":"x-org-id is required"} until
# the caller names the org explicitly.  The org id is not a secret — it is the
# same ``wrk_...`` id that appears in console URLs — but it must be sent.
ORG_HEADER = "x-org-id"

# Member-budget routes.  v2 is the policy-backed line: identical request body
# (``{"budget_dollars": n}``), identical response fields and money units, plus
# ``additional_budget_rules``.  The vendor documents HTTP 409 for budgets not
# yet converted to policies ("unconverted legacy budgets"), and v1 keeps
# working through the transition — so v1 stays as the fallback rather than
# being deleted.  Verified live 2026-10-11: both paths answer 200 to the same
# ``all`` service key; v2 with a console *session* token is 403 (service-key
# surface, like v1), and /api/v2/budget-policies also exists on the key.
BUDGET_MEMBERS_PATH_V2 = "/v2/budgets/members"
BUDGET_MEMBERS_PATH_V1 = "/v1/budgets/members"


class ConsoleApiError(RuntimeError):
    """Console API returned a non-200 status."""

    def __init__(self, status: int, url: str, tag: str = "", detail: str = "") -> None:
        self.status = status
        self.url = url
        self.tag = tag
        self.detail = detail
        super().__init__(
            f"console API HTTP {status}"
            + (f" {tag}" if tag else "")
            + (f" — {detail}" if detail else "")
        )


def _console_headers(token: Optional[str],
                     org_id: Optional[str] = None) -> dict[str, str]:
    """Headers for console JSON routes (Bearer when a token is supplied).

    *org_id* adds ``x-org-id``, which every org-scoped route requires.
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Origin": _OPENCODE_BASE,
        "Referer": _OPENCODE_BASE + "/",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if org_id:
        headers[ORG_HEADER] = str(org_id)
    return headers


def _console_get(path: str, token: Optional[str], timeout: int = 15,
                 org_id: Optional[str] = None):
    """GET a console JSON route; raise ``ConsoleApiError`` on any non-200."""
    url = _CONSOLE_API + path
    req = Request(url, headers=_console_headers(token, org_id), method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raw = (exc.read() or b"").decode("utf-8", errors="replace")
        tag = detail = ""
        try:
            parsed = json.loads(raw or "{}")
            if isinstance(parsed, dict):
                tag = str(parsed.get("_tag") or parsed.get("error") or "")
                detail = str(
                    parsed.get("message") or parsed.get("error_description") or ""
                )
        except (ValueError, TypeError):
            pass
        raise ConsoleApiError(exc.code, url, tag, detail) from None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise ConsoleApiError(status, url, "", "non-JSON payload") from None


def _check_range(range_: str) -> str:
    """Validate a usage range against the console's accepted set."""
    value = (range_ or "7d").strip()
    if value not in USAGE_RANGES:
        raise ValueError(f"range must be one of {USAGE_RANGES}, got {value!r}")
    return value


def micro_cents_to_usd(value) -> Optional[float]:
    """Convert a console money field (fixed-point string or int) to USD."""
    try:
        return round(float(value) * MICRO_CENTS, 8)
    except (TypeError, ValueError):
        return None


def fetch_console_config(token: str) -> Optional[dict]:
    """Return the console client config (carries the model catalog)."""
    return _console_get("/config", token)


def console_model_ids(token: str, provider: str = "opencode") -> list[str]:
    """Model IDs the console advertises for *provider*.

    OpenCode serves **no** ``/models`` route on its inference base, so generic
    discovery (``{api_base}/models``) 404s.  The authoritative catalog is the
    console config's per-provider ``whitelist``.  Returns ``[]`` on an
    unfamiliar shape so discovery degrades instead of raising into CRUD.
    """
    config = fetch_console_config(token)
    if not isinstance(config, dict):
        return []
    providers = (config.get("config") or {}).get("provider") or config.get("provider") or {}
    if not isinstance(providers, dict):
        return []
    entry = providers.get(provider) or {}
    if not isinstance(entry, dict):
        return []
    models = entry.get("whitelist") or entry.get("models") or []
    if not isinstance(models, (list, tuple)):
        return []
    ids = [str(m).strip() for m in models if str(m).strip()]
    return sorted(set(ids))


def fetch_usage_summary(token: str, range_: str = "7d",
                        org_id: Optional[str] = None) -> Optional[dict]:
    """Vendor-side usage totals for *range_* (requests/tokens/cost)."""
    return _console_get(f"/usage/summary?range={_check_range(range_)}", token,
                        org_id=org_id)


def fetch_usage_cost_by_day(token: str, range_: str = "7d",
                            org_id: Optional[str] = None) -> list[dict]:
    """Vendor-side per-day usage rows for *range_* (date/cost/tokens/requests)."""
    data = _console_get(f"/usage/cost-by-day?range={_check_range(range_)}", token,
                        org_id=org_id)
    return data if isinstance(data, list) else []


def fetch_usage_models(token: str, range_: str = "7d",
                       org_id: Optional[str] = None) -> list[dict]:
    """Vendor-side per-model usage rows for *range_*."""
    data = _console_get(f"/usage/models?range={_check_range(range_)}", token,
                        org_id=org_id)
    if isinstance(data, dict):
        items = data.get("items")
        return items if isinstance(items, list) else []
    return data if isinstance(data, list) else []


def fetch_usage_users(token: str, range_: str = "7d",
                      org_id: Optional[str] = None) -> list[dict]:
    """Vendor-side per-principal usage rows for *range_*."""
    data = _console_get(f"/usage/users?range={_check_range(range_)}", token,
                        org_id=org_id)
    if isinstance(data, dict):
        items = data.get("items")
        return items if isinstance(items, list) else []
    return data if isinstance(data, list) else []


def fetch_go_usage(token: str, timeout: int = 15) -> Optional[dict]:
    """The Go plan's own usage windows: rolling-5h / weekly / monthly.

    This is the plan LCP's OpenCode plugin primarily tracks, and it needs no
    console session and no ``x-org-id`` — just the provider key::

        GET https://opencode.ai/inference/go/v1/usage
        Authorization: Bearer <oc_sk_…>
        {"usage": {"rolling": {"status","percent","resetsAt"}, "weekly": …, "monthly": …}}

    Returns the ``usage`` object, or ``None`` when the payload carries none.
    Raises :class:`ConsoleApiError` on a non-200 (e.g. a key without Go access).
    """
    if not token:
        return None
    request = Request(GO_USAGE_API, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        body = (exc.read() or b"").decode("utf-8", errors="replace")
        tag = detail = ""
        try:
            parsed = json.loads(body or "{}")
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict):
                tag = str(error.get("type") or "")
                detail = str(error.get("message") or "")
            elif isinstance(parsed, dict):
                tag = str(parsed.get("type") or "")
        except (ValueError, TypeError):
            pass
        raise ConsoleApiError(exc.code, GO_USAGE_API, tag, detail) from None
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ConsoleApiError(200, GO_USAGE_API, "", "non-JSON payload") from None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return usage if isinstance(usage, dict) else None


def fetch_orgs(token: str) -> list[dict]:
    """Organizations the session can see — ``GET /api/orgs``.

    The console returns a bare JSON array of ``{id, name}``; the first entry is
    the org every other org-scoped route must be told about via ``x-org-id``.
    """
    data = _console_get("/orgs", token)
    if isinstance(data, dict):
        data = data.get("data") or data.get("orgs") or []
    return data if isinstance(data, list) else []


def fetch_org_id(token: str) -> str:
    """The first org id for *token*, or ``""`` when none is visible."""
    for org in fetch_orgs(token):
        if isinstance(org, dict) and org.get("id"):
            return str(org["id"])
    return ""


def fetch_account_credits(token: str,
                          org_id: Optional[str] = None) -> Optional[dict]:
    """Console credits — ``GET /api/billing/status``.

    Two credentials are accepted, with different refusal modes:

    * a **user token** (console session) plus ``x-org-id`` — 400 ``OrgRequired``
      without the header;
    * an **API key** — but measured 2026-10-31 the billing/budgets class is
      **person-only**: the stored key answered 200 on ``/api/usage/*``,
      ``/api/v2/config`` and ``/api/service-accounts`` yet **403**
      ``{"_tag":"Forbidden"}`` here and on ``/api/v1|v2/budgets/members``.  The
      console documents that some actions "always require a person signed in to
      the Console, even with an **All** key", and key permissions are only
      ``Inference only`` | ``All``.  So expect 403 from a key and do not report it
      as a dead credential: it is a permissions verdict, not an auth failure.

    Returns ``{billingMode, mode, balanceMicroCents, availableMicroCents,
    creditLimitMicroCents, ...}``.
    """
    return _console_get("/billing/status", token, org_id=org_id)


def fetch_billing_account(token: str,
                          org_id: Optional[str] = None) -> Optional[dict]:
    """Legacy alias route — kept for comparison during shape migrations."""
    return _console_get("/billing/account", token, org_id=org_id)


def _seconds_until(iso_stamp) -> Optional[int]:
    """Seconds from now until an ISO-8601 timestamp, or None if unparsable."""
    if not iso_stamp:
        return None
    try:
        when = datetime.fromisoformat(str(iso_stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))


def fetch_budget_members(token: str, version: str = "v2") -> list[dict]:
    """Plan/budget rows — ``GET /api/v2/budgets/members`` (v1 fallback).

    Observed shape (2026-09-26 service key, 2026-10-11 v2) — v1::

        [{"user_id": "acc_…", "email": "…", "limit_micro_cents": null,
          "spent_micro_cents": "0", "exceeded": false,
          "resets_at": "2026-10-01T00:00:00.000Z", "source": null}]

    v2 returns the *same* keys and money units plus
    ``additional_budget_rules: []`` (shared / model-specific policy rules).

    Note the versioned prefix: these routes are not on the ts-rest ``/api``
    surface the other console reads use.  v2 answers HTTP 409 for a budget the
    vendor has not yet converted to a policy ("unconverted legacy budgets"), so
    a 409 falls back to v1 rather than blanking the card — v1 stays supported
    through the transition.  Any other status propagates.  Returns ``[]`` on an
    unfamiliar shape so the caller degrades instead of raising into the refresh
    loop.
    """
    path = BUDGET_MEMBERS_PATH_V2 if version == "v2" else BUDGET_MEMBERS_PATH_V1
    try:
        data = _console_get(path, token)
    except ConsoleApiError as exc:
        if version == "v2" and exc.status == 409:
            logger.info("opencode_budget_v2_unconverted_fallback_to_v1",
                        status=exc.status, tag=exc.tag)
            data = _console_get(BUDGET_MEMBERS_PATH_V1, token)
        else:
            raise
    if isinstance(data, dict):
        items = data.get("items")
        return items if isinstance(items, list) else []
    return data if isinstance(data, list) else []


def budget_rules(members: Optional[list[dict]]) -> list[dict]:
    """``additional_budget_rules`` from v2 rows — configuration, not spend.

    v2 lists shared and model-specific policy rules separately from the scalar
    per-member cap.  The vendor is explicit that they are configuration, not
    spend or admission status, so they are surfaced as descriptors only and
    never turned into a percentage bar (a percentage needs a measured spend
    against that rule, which this payload does not carry).
    """
    rules: list[dict] = []
    for row in members or []:
        if not isinstance(row, dict):
            continue
        for rule in row.get("additional_budget_rules") or []:
            if not isinstance(rule, dict):
                continue
            limit = micro_cents_to_usd(rule.get("limit_micro_cents"))
            rules.append({
                "policy_id": rule.get("policy_id"),
                "rule_id": rule.get("rule_id"),
                "limit_usd": limit,
                "audience": rule.get("audience"),
                "scope": rule.get("scope"),          # each | combined
                "per": rule.get("per"),              # set | model
                "selector": rule.get("selector"),
            })
    return rules


def plan_windows_from_go(usage: dict) -> dict:
    """Map ``GET /inference/go/v1/usage`` onto the Usage card's window payload.

    Live shape (measured 2026-09-26)::

        {"rolling": {"status": "ok",           "percent": 0,   "resetsAt": "..."},
         "weekly":  {"status": "ok",           "percent": 0,   "resetsAt": "..."},
         "monthly": {"status": "rate-limited", "percent": 100, "resetsAt": "..."}}

    ``status`` is preserved per window as ``<name>_status``: it is how the plan
    reports an exhausted quota (``rate-limited`` at ``percent`` 100), which the
    card shows because a red bar alone does not say the plan is refusing calls.
    """
    windows: dict = {}
    for key in ("rolling", "weekly", "monthly"):
        row = (usage or {}).get(key)
        if not isinstance(row, dict):
            continue
        try:
            windows[f"{key}_pct"] = round(float(row.get("percent")), 1)
        except (TypeError, ValueError):
            continue
        reset = _seconds_until(row.get("resetsAt"))
        if reset is not None:
            windows[f"{key}_reset_sec"] = reset
        status = row.get("status")
        if status:
            windows[f"{key}_status"] = str(status)
    return windows


def plan_windows(members: list[dict]) -> dict:
    """Map console budget rows onto the Usage card's window payload.

    Returns ``{}`` when no row carries a limit.  The console reports
    ``limit_micro_cents: null`` for a service-account-only org, and a percentage
    without a denominator must never be invented — an absent limit therefore
    yields no windows, which the caller turns into an explicit state rather
    than a fabricated bar.

    ``resets_at`` on a limited row is a calendar month boundary, so the window
    is reported as the monthly one.
    """
    for row in members or []:
        if not isinstance(row, dict):
            continue
        limit = micro_cents_to_usd(row.get("limit_micro_cents"))
        if not limit or limit <= 0:
            continue
        spent = micro_cents_to_usd(row.get("spent_micro_cents")) or 0.0
        window = {"monthly_pct": round(min(100.0, spent / limit * 100.0), 1)}
        reset = _seconds_until(row.get("resets_at"))
        if reset is not None:
            window["monthly_reset_sec"] = reset
        return window
    return {}
