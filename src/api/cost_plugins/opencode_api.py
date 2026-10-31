"""HTTP client for OpenCode's console JSON API — usage, credits and budgets.

Every read goes to a JSON route under ``console.opencode.ai/api/*`` with
``Authorization: Bearer <token>``.  The server-rendered ``/workspace/<wrk>/…``
pages that the old scrapers targeted no longer exist (they return a client
shell), so nothing here parses HTML.

Two credential classes, measured live 2026-10-31:

* an API key (``oc_sk_…``) reads the usage routes, ``/v2/config`` and the
  admin-class ``/service-accounts``;
* the credits/budgets routes (``/billing/*``, ``/budgets/members``) are
  **person-only** and refuse every API key with 403 — only a console session
  token (``console_oauth.py``) reads them.

``GET /usage/*`` and ``GET /go/v1/usage`` therefore work with a key; callers
that need credits must supply a session token.  A 403 is "authenticated but not
permitted" and is classified apart from a 401 so a permissions verdict is never
reported as a dead credential.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from ..logging_config import get_logger

logger = get_logger("lcp.cost.opencode_api")

_OPENCODE_BASE = "https://opencode.ai"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)






# ── Helpers ────────────────────────────────────────────────────────────────





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
MICRO_CENTS = 1e-8  # console money unit → USD (the vendor's fixed-point scale)

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
