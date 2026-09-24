"""Jinja2 template renderer for the LCP gateway UI.

Replaces the f-string-based HTML generation in pages.py and dashboard.py
with proper Jinja2 template files — giving syntax highlighting, auto-escaping,
and shared partials for sidebar/JS without any build step.
"""

import json
from datetime import date as _date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import func

_templates_dir = Path(__file__).parent / "templates" / "jinja"
_env = Environment(loader=FileSystemLoader(str(_templates_dir)), autoescape=True, extensions=["jinja2.ext.do"])

# Cache-buster: max mtime across static assets (CSS + JS) so browsers
# re-fetch after every deploy. The CSS/JS links append ?v=<buster>.
_static_dir = _templates_dir / "static"
_cache_buster = "0"
if _static_dir.is_dir():
    _asset_paths = [_static_dir / "dashboard.css", *_static_dir.glob("js/*.js")]
    _mtimes = [p.stat().st_mtime for p in _asset_paths if p.is_file()]
    if _mtimes:
        _cache_buster = str(int(max(_mtimes)))


def _fmt_num(n) -> str:
    """Format a large number for display: 1.5M, 42.3K, 7."""
    if n is None:
        return "0"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(int(n))


def _fmt_cost(c) -> str:
    """Format a cost value as a dollar string."""
    if c is None:
        return "$0.000000"
    return f"${float(c):.6f}"


def _fmt_ts(t) -> str:
    """Format a POSIX timestamp as a compact UTC stamp.

    Used by the Work views, which must show SUBJECT time and COMPUTATION time as
    separate columns -- so this deliberately renders an explicit UTC suffix
    rather than a bare local-looking string that invites the two to be conflated.
    """
    if t is None:
        return "—"
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return str(t)


_env.filters["fmt_num"] = _fmt_num
_env.filters["fmt_cost"] = _fmt_cost
_env.filters["fmt_ts"] = _fmt_ts


def _compute_monthly(engine) -> dict:
    """Query gateway DB for current-month per-provider totals.

    Returns a dict like ``{"deepseek": {"reqs": 42, "tokens": 12345, "cost": 1.23}}``.
    When ``engine`` is None (e.g. tests), returns an empty dict.
    """
    from ..api.models import get_session, Request as RequestModel

    _first_of_month = _date.today().replace(day=1).isoformat()
    monthly: dict = {}
    if engine is None:
        return monthly
    try:
        with get_session(engine) as s:
            rows = (
                s.query(
                    RequestModel.provider,
                    func.count(RequestModel.id).label("m_reqs"),
                    func.coalesce(
                        func.sum(
                            RequestModel.completion_tokens + RequestModel.prompt_tokens
                        ),
                        0,
                    ).label("m_tokens"),
                    func.coalesce(func.sum(RequestModel.cost), 0).label("m_cost"),
                )
                .filter(
                    RequestModel.timestamp >= _first_of_month,
                    RequestModel.success == 1,
                )
                .group_by(RequestModel.provider)
                .all()
            )
        for r in rows:
            monthly[r.provider] = {
                "reqs": int(r.m_reqs),
                "tokens": int(r.m_tokens),
                "cost": float(r.m_cost),
            }
    except Exception:
        pass
    return monthly


# ── M2/M2b: legacy page names fold into the pages that absorbed them ─────────
# The pre-merge pages (dashboard / logs / providers / keys) were folded into the
# control-plane pages and their templates are gone. A caller that still names one
# — an older test, a stale render helper — resolves to the page with the tab that
# now owns that content, instead of TemplateNotFound.
#
# ``pages/usage.html`` is NOT in this table: M2b gave Usage its own page again, so
# the name maps to a real template and must not be rewritten (an alias here sends
# it to Activity, whose allowed tabs no longer include "usage" — the page then
# silently renders the overview instead of Usage).
# name -> (owning template, tab, any context the section needs to render)
_LEGACY_PAGE_TABS = {
    "pages/dashboard.html": ("pages/activity.html", "overview", {}),
    "pages/logs.html": ("pages/activity.html", "logs",
                        {"view": {"tab": "conversations"}, "params": {}}),
    "pages/providers.html": ("pages/models.html", "providers", {}),
    # M2c: keys are per profile, so the profile directory is what stands in for
    # the old global keys page — not a "keys" tab, which no longer exists at this
    # level and would render an empty page.
    "pages/keys.html": ("pages/profiles.html", "profiles", {}),
}


def render_page(template_name: str, config, engine=None, **kwargs) -> str:
    """Render a standalone page template with common context injected.

    All templates receive:
      * ``config`` — the gateway config object
      * ``monthly`` / ``monthly_json`` / ``configured_providers_json`` — sidebar data
      * ``profiles`` — list of profile names for sidebar nav

    Usage::

        html = render_page("pages/models.html", config=self.config, engine=self.engine)
    """
    merged = _LEGACY_PAGE_TABS.get(template_name)
    if merged is not None:
        template_name, tab, extra = merged
        kwargs.setdefault("tab", tab)
        for key, value in extra.items():
            kwargs.setdefault(key, value)
    monthly = _compute_monthly(engine)
    provider_names = sorted(config.providers.keys()) if config is not None and hasattr(config, 'providers') else []
    profiles_list = list(config.profiles.keys()) if config is not None and hasattr(config, 'profiles') else []
    # Which providers have an encrypted credential stored (UI-managed API key)?
    provider_has_keys = {}
    if engine is not None:
        try:
            from ..api.credential_store import get_credential_store
            store = get_credential_store(engine)
            if store is not None:
                provider_has_keys = {p: store.has(p) for p in provider_names}
        except Exception:
            pass
    ctx = {
        "config": config,
        "monthly": monthly,
        "monthly_json": json.dumps(monthly),
        "configured_providers_json": json.dumps(provider_names),
        "providers": config.providers if config is not None and hasattr(config, 'providers') else {},
        "profiles": profiles_list,
        "profiles_dict": config.profiles if config is not None and hasattr(config, 'profiles') else {},
        "provider_has_keys": provider_has_keys,
        "profile_budgets": {},
        "cache_buster": _cache_buster,
    }
    # Let caller-supplied kwargs override defaults (e.g. profile-filtered monthly)
    ctx.update(kwargs)
    return _env.get_template(template_name).render(**ctx)
