"""Dashboard renderer for the LCP gateway.

Generates the full server-rendered HTML dashboard.
"""
import json
from datetime import datetime, timezone
from typing import Any, cast
from ..api.models import Request as RequestModel, get_session
from ..api.prompt_cache import get_prompt_cache
from ..api.token_verifier import get_token_verifier
from ..api.router import get_dynamic_router
from ..api.logging_config import get_logger

logger = get_logger("lcp.dashboard")

from .render import render_page


def dashboard_context(config, engine, headers, profile_filter=None):
    """Build the overview view-context (what used to be the standalone dashboard).

    Split out of ``render_dashboard`` for M2: the Overview tab of the merged
    Activity page renders these same numbers, and the arithmetic must live in
    exactly one place. Returns the kwargs ``render_page`` expects.
    """

    from sqlalchemy import func, case

    # Map a raw stored model ID (e.g. "deepseek/deepseek-v4-pro" or
    # "deepseek-v4-pro") to its logical gateway name so the graphs group and
    # label models consistently (same as Models/Benchmarks pages).
    def _logical(model: str) -> str:
        if not model:
            return model or ""
        try:
            db_path = str(engine.url.database) if engine is not None else "data/costs.db"
            from ..api.router import logical_model_name
            return logical_model_name(model, db_path)
        except Exception:  # noqa: BLE001 — never break the dashboard on a name
            return model

    try:
        with get_session(engine) as session:
            # ── Summary query ──
            summary = session.query(
                func.coalesce(func.sum(RequestModel.cost), 0).label("total_cost"),
                func.count(RequestModel.id).label("total_requests"),
                func.coalesce(func.sum(RequestModel.cache_hit_tokens), 0).label("cache_hits"),
                func.coalesce(func.sum(RequestModel.cache_miss_tokens), 0).label("cache_misses"),
                func.coalesce(func.sum(RequestModel.prompt_tokens), 0).label(
                    "prompt_tokens"
                ),
                func.coalesce(func.sum(RequestModel.completion_tokens), 0).label(
                    "output_tokens"
                ),
            )
            if profile_filter:
                summary = summary.filter(
                    RequestModel.profile == profile_filter
                )
            summary = summary.first()

            # Unpack summary to plain Python types (avoids Column typing issues)
            _s = summary
            summary_total_cost: float = float(_s.total_cost) if _s else 0.0
            summary_total_requests: int = int(_s.total_requests) if _s else 0
            summary_prompt_tokens: int = int(_s.prompt_tokens) if _s else 0
            summary_output_tokens: int = int(_s.output_tokens) if _s else 0
            cache_hit_tokens: int = int(_s.cache_hits) if _s else 0
            cache_miss_tokens: int = int(_s.cache_misses) if _s else 0

            fallback_count = (
                session.query(func.count(RequestModel.id))
                .filter(RequestModel.success == 1)
            )
            if profile_filter:
                fallback_count = fallback_count.filter(
                    RequestModel.profile == profile_filter
                )
            fallback_count = (
                fallback_count.filter(RequestModel.provider != "error").scalar() or 0
            )
            total_success = (
                session.query(func.count(RequestModel.id))
                .filter(RequestModel.success == 1)
            )
            if profile_filter:
                total_success = total_success.filter(
                    RequestModel.profile == profile_filter
                )
            total_success = total_success.scalar() or 0

            cache_total = cache_hit_tokens + cache_miss_tokens
            cache_hit_rate = (
                (cache_hit_tokens / cache_total * 100) if cache_total > 0 else 0
            )

            # ── Active days ──
            active_days_q = session.query(
                func.count(func.distinct(func.substr(RequestModel.timestamp, 1, 10)))
            )
            if profile_filter:
                active_days_q = active_days_q.filter(
                    RequestModel.profile == profile_filter
                )
            active_days = active_days_q.scalar() or 0

            # ── Per-profile cost ──
            profile_rows_q = session.query(
                RequestModel.profile,
                func.sum(RequestModel.cost).label("total_cost"),
                func.count(RequestModel.id).label("count"),
            ).filter(RequestModel.success == 1)
            if profile_filter:
                profile_rows_q = profile_rows_q.filter(
                    RequestModel.profile == profile_filter
                )
            profile_rows = (
                profile_rows_q.group_by(RequestModel.profile).all()
            )

            # ── Daily Costs (all time, grouped by date+profile+model+provider) ──
            daily_rows = (
                session.query(
                    func.substr(RequestModel.timestamp, 1, 10).label("date"),
                    RequestModel.profile,
                    RequestModel.model,
                    RequestModel.provider,
                    func.count(RequestModel.id).label("reqs"),
                    func.sum(
                        case(
                            (RequestModel.provider != "error", 1), else_=0
                        )
                    ).label("fb_count"),
                    func.coalesce(func.sum(RequestModel.cache_hit_tokens), 0).label(
                        "cache_hit"
                    ),
                    func.coalesce(func.sum(RequestModel.cache_miss_tokens), 0).label(
                        "cache_miss"
                    ),
                    func.coalesce(func.sum(RequestModel.completion_tokens), 0).label(
                        "output"
                    ),
                    func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                )
            )
            if profile_filter:
                daily_rows = daily_rows.filter(
                    RequestModel.profile == profile_filter
                )
            daily_rows = (
                daily_rows.group_by(
                    func.substr(RequestModel.timestamp, 1, 10),
                    RequestModel.profile,
                    RequestModel.model,
                    RequestModel.provider,
                )
                .order_by(
                    func.substr(RequestModel.timestamp, 1, 10).desc(),
                    RequestModel.profile,
                )
                .limit(30)
                .all()
            )

            # ── Recent Requests ──
            recent_q = (
                session.query(RequestModel)
                .order_by(RequestModel.id.desc())
            )
            if profile_filter:
                recent_q = recent_q.filter(
                    RequestModel.profile == profile_filter
                )
            recent_rows = recent_q.limit(50).all()

            # ── Recent Errors ──
            error_q = (
                session.query(RequestModel)
                .filter(RequestModel.success == 0)
                .order_by(RequestModel.id.desc())
            )
            if profile_filter:
                error_q = error_q.filter(
                    RequestModel.profile == profile_filter
                )
            error_rows = error_q.limit(20).all()

            # ── Time-series data for Chart.js (last 14 days, daily cost) ──
            ts_rows = (
                session.query(
                    func.substr(RequestModel.timestamp, 1, 10).label("date"),
                    func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                    func.coalesce(func.avg(RequestModel.latency_ms), 0).label("avg_lat"),
                    func.count(RequestModel.id).label("count"),
                )
            )
            if profile_filter:
                ts_rows = ts_rows.filter(
                    RequestModel.profile == profile_filter
                )
            ts_rows = (
                ts_rows.filter(RequestModel.success == 1)
                .group_by(func.substr(RequestModel.timestamp, 1, 10))
                .order_by(func.substr(RequestModel.timestamp, 1, 10).asc())
                .limit(14)
                .all()
            )
            ts_dates = [r.date for r in ts_rows]
            ts_costs = [float(r.cost) for r in ts_rows]
            ts_lats = [float(r.avg_lat) for r in ts_rows]

            # Per-profile time series (grouped bar chart data)
            pp_rows_q = (
                session.query(
                    func.substr(RequestModel.timestamp, 1, 10).label("date"),
                    RequestModel.profile,
                    func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                    func.coalesce(func.avg(RequestModel.latency_ms), 0).label("avg_lat"),
                )
                .filter(RequestModel.success == 1)
            )
            if profile_filter:
                pp_rows_q = pp_rows_q.filter(
                    RequestModel.profile == profile_filter
                )
            pp_rows = (
                pp_rows_q.group_by(func.substr(RequestModel.timestamp, 1, 10), RequestModel.profile)
                .order_by(func.substr(RequestModel.timestamp, 1, 10).asc())
                .limit(14 * len(config.profiles))
                .all()
            )
            # Pivot into {dates: [...], profiles: {name: {costs:[], lats:[]}}}
            pp_data = {"dates": sorted(set(r.date for r in pp_rows)), "profiles": {}}
            for p in config.profiles.keys():
                pp_data["profiles"][p] = {"costs": [], "lats": []}
            date_to_idx = {d: i for i, d in enumerate(pp_data["dates"])}
            for p in config.profiles.keys():
                pp_data["profiles"][p]["costs"] = [0.0] * len(pp_data["dates"])
                pp_data["profiles"][p]["lats"] = [0.0] * len(pp_data["dates"])
            for r in pp_rows:
                idx = date_to_idx[r.date]
                pp_data["profiles"][r.profile]["costs"][idx] = float(r.cost)
                pp_data["profiles"][r.profile]["lats"][idx] = float(r.avg_lat)

            # Per-model time series (grouped by model)
            pm_rows_q = (
                session.query(
                    func.substr(RequestModel.timestamp, 1, 10).label("date"),
                    RequestModel.model,
                    func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                    func.coalesce(func.avg(RequestModel.latency_ms), 0).label("avg_lat"),
                )
                .filter(RequestModel.success == 1)
            )
            if profile_filter:
                pm_rows_q = pm_rows_q.filter(
                    RequestModel.profile == profile_filter
                )
            pm_rows = (
                pm_rows_q.group_by(func.substr(RequestModel.timestamp, 1, 10), RequestModel.model)
                .order_by(func.substr(RequestModel.timestamp, 1, 10).asc())
                .all()
            )
            pm_data = {"dates": sorted(set(r.date for r in pm_rows)), "models": {}}
            pm_date_to_idx = {d: i for i, d in enumerate(pm_data["dates"])}
            for r in pm_rows:
                logical = _logical(r.model)
                if logical not in pm_data["models"]:
                    pm_data["models"][logical] = {"costs": [0.0] * len(pm_data["dates"]), "lats": [0.0] * len(pm_data["dates"])}
                idx = pm_date_to_idx[r.date]
                pm_data["models"][logical]["costs"][idx] = float(r.cost)
                pm_data["models"][logical]["lats"][idx] = float(r.avg_lat)
    except Exception:
        logger.error("dashboard_query_failed", exc_info=True)
        summary = type("S", (), {"total_cost": 0, "total_requests": 0, "cache_hits": 0, "cache_misses": 0, "prompt_tokens": 0, "output_tokens": 0})()
        summary_total_cost = 0.0
        summary_total_requests = 0
        summary_output_tokens = 0
        summary_prompt_tokens = 0
        fallback_count = 0
        total_success = 0
        cache_hit_rate = 0
        cache_hit_tokens = 0
        cache_miss_tokens = 0
        active_days = 0
        profile_rows = []
        daily_rows = []
        recent_rows = []
        error_rows = []
        ts_dates = []
        ts_costs = []
        ts_lats = []
        pp_data = {"dates": [], "profiles": {}}
        pm_data = {"dates": [], "models": {}}

    cache = get_prompt_cache()
    cache_stats = cache.stats

    # ── Cache savings helper ──
    # Estimate dollars saved via provider prefix caching.
    # cache_hit tokens are billed at ~0.8% of miss rate.
    def _savings_for_model(model: str, hit_tokens: int) -> float:
        if hit_tokens <= 0:
            return 0.0
        for p_name, p_cfg in config.providers.items():
            if model in p_cfg.get("models", []):
                try:
                    pricing = config.get_pricing(p_name, model)
                    return (hit_tokens / 1_000_000) * (
                        pricing["cache_miss"] - pricing["cache_hit"]
                    )
                except Exception:
                    pass
        return 0.0

    total_cache_savings = sum(
        _savings_for_model(cast(Any, r).model, cast(Any, r).cache_hit)
        for r in daily_rows
    ) if daily_rows else 0.0

    # ── Helper: format large numbers ──
    def _fmt_num(n):
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n/1_000:.1f}K"
        return str(int(n))

    def _fmt_cost(c):
        return f"${float(c):.6f}" if c else "$0.000000"

    # ── Data for the Jinja2 dashboard template ──
    host = headers.get("Host", "localhost:8734")
    scheme = "https" if (
        headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"
        or headers.get("X-Forwarded-Scheme") == "https"
    ) else "http"
    host_url = f"{scheme}://{host}"

    # Sidebar profiles: name + chain summary (copy-button title)
    sidebar_profiles = []
    for p, pcfg in config.profiles.items():
        chain = pcfg.get("chain", [])
        providers_str = ", ".join(s["provider"] for s in chain[:2])
        if len(chain) > 2:
            providers_str += f" +{len(chain)-2}"
        sidebar_profiles.append({"name": p, "providers_str": providers_str})

    # Per-profile summary cards
    profile_cards = [
        {"profile": row.profile, "count": row.count,
         "total_cost": float(row.total_cost)}
        for row in profile_rows
    ]

    # Daily costs table rows (cache fields pre-formatted with _fmt_num)
    daily_rows_data = []
    for r in daily_rows:
        daily_saved = _savings_for_model(r.model, r.cache_hit)
        daily_rows_data.append({
            "date": r.date,
            "profile": r.profile,
            "model": _logical(r.model),
            "provider": r.provider,
            "reqs": r.reqs,
            "fb_count": r.fb_count,
            "cache_hit": _fmt_num(r.cache_hit),
            "cache_miss": _fmt_num(r.cache_miss),
            "output": _fmt_num(r.output),
            "cost": float(r.cost),
            "saved": daily_saved,
        })

    # Recent requests table rows (status badges pre-built as HTML)
    recent_rows_data = []
    for r in recent_rows:
        time_str = r.timestamp[11:19] if r.timestamp and "T" in r.timestamp else str(r.timestamp)[:19]
        status_badges = ""
        if r.success:
            status_badges += '<span class="badge badge-success">ok</span> '
        else:
            status_badges += f'<span class="badge badge-error">{r.error_type or "error"}</span> '
        if r.provider != "error" and r.success and r.provider:
            prof_cfg = config.profiles.get(r.profile, {})
            chain = prof_cfg.get("chain", [])
            if chain and chain[0]["provider"] != r.provider:
                status_badges += '<span class="badge badge-fallback">FB</span>'
        req_saved = _savings_for_model(r.model, r.cache_hit_tokens)
        recent_rows_data.append({
            "time": time_str,
            "profile": r.profile,
            "model": _logical(r.model),
            "provider": r.provider,
            "badges": status_badges,
            "latency_s": r.latency_ms / 1000,
            "cost": float(r.cost),
            "saved": req_saved,
        })

    # Recent errors table rows
    error_rows_data = [
        {
            "time": r.timestamp[:19] if r.timestamp else "",
            "profile": r.profile,
            "provider": r.provider,
            "error_type": r.error_type,
        }
        for r in error_rows
    ]

    # Fallback rate
    fb_pct = (fallback_count / total_success * 100) if total_success > 0 else 0

    # Phase 5/6 extra cards
    verifier = get_token_verifier()
    v_stats = verifier.stats if hasattr(verifier, "stats") else {}
    router = get_dynamic_router()
    r_conf = getattr(router, "config", {})
    token_mismatches = v_stats.get("mismatches", 0)
    routing_threshold = r_conf.get("flash_threshold_tokens", 4096)
    cache_entries = cache_stats["entries"]
    cache_max_entries = cache_stats.get("max_entries", "N/A")

    # ── Monthly usage per provider (for OpenCode display) ──
    from datetime import date as _date
    _first_of_month = _date.today().replace(day=1).isoformat()
    with get_session(engine) as _s:
        _monthly_rows = _s.query(
            RequestModel.provider,
            func.count(RequestModel.id).label("m_reqs"),
            func.coalesce(func.sum(RequestModel.completion_tokens + RequestModel.prompt_tokens), 0).label("m_tokens"),
            func.coalesce(func.sum(RequestModel.cost), 0).label("m_cost"),
        ).filter(
            RequestModel.timestamp >= _first_of_month,
            RequestModel.success == 1,
        )
        if profile_filter:
            _monthly_rows = _monthly_rows.filter(
                RequestModel.profile == profile_filter
            )
        _monthly_rows = _monthly_rows.group_by(RequestModel.provider).all()
    _monthly_data = {}
    for r in _monthly_rows:
        _monthly_data[r.provider] = {
            "reqs": int(r.m_reqs),
            "tokens": int(r.m_tokens),
            "cost": float(r.m_cost),
        }

    # Serialize for JS
    _monthly_data_json = json.dumps(_monthly_data)
    _configured_providers_json = json.dumps(sorted(config.providers.keys()))

    # ── Full HTML ──
    filter_title = f" — {profile_filter.upper()}" if profile_filter else ""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ts_dates_json = json.dumps(ts_dates)
    ts_costs_json = json.dumps(ts_costs)
    ts_lats_json = json.dumps(ts_lats)
    pp_data_json = json.dumps(pp_data)
    pm_data_json = json.dumps(pm_data)

    import src as _src
    version = getattr(_src, "__version__", "?")

    # Pre-formatted display strings (preserve old dashboard output exactly)
    total_cost_fmt = _fmt_cost(summary_total_cost)
    cache_savings_fmt = f"${total_cache_savings:.4f}"
    cache_hit_rate_fmt = f"{cache_hit_rate:.1f}%"
    fb_pct_fmt = f"{fb_pct:.1f}%"
    summary_total_requests_fmt = f"{summary_total_requests:,}"
    fallback_count_fmt = f"{fallback_count:,}"
    cache_hit_tokens_fmt = _fmt_num(cache_hit_tokens)
    cache_miss_tokens_fmt = _fmt_num(cache_miss_tokens)
    output_tokens_fmt = _fmt_num(summary_output_tokens)
    prompt_tokens_fmt = _fmt_num(summary_prompt_tokens)

    # ── Budget status cards ──
    budget_cards = []
    try:
        from ..api.models import Budget, get_session as _get_sess
        with _get_sess(engine) as b_sess:
            active_budgets = b_sess.query(Budget).filter(
                Budget.status.in_(["active", "exceeded"]),
            ).order_by(Budget.name).all()
            for b in active_budgets:
                pct = (b.current_spend / b.amount * 100) if b.amount > 0 else 0
                budget_cards.append({
                    "id": b.id,
                    "name": b.name,
                    "profile": b.profile or "global",
                    "amount": b.amount,
                    "current_spend": b.current_spend,
                    "spend_pct": round(pct, 1),
                    "period": b.period,
                    "action": b.action,
                    "status": b.status,
                    "exceeded": b.status == "exceeded" or (b.amount > 0 and b.current_spend >= b.amount),
                })
    except Exception:
        pass

    return {
        "active_page": "activity",
        "profile_filter": profile_filter,
        "filter_title": filter_title,
        "now_utc": now_utc,
        "version": version,
        "host_url": host_url,
        "total_cost_fmt": total_cost_fmt,
        "cache_savings_fmt": cache_savings_fmt,
        "cache_hit_rate_fmt": cache_hit_rate_fmt,
        "fb_pct_fmt": fb_pct_fmt,
        "summary_total_requests_fmt": summary_total_requests_fmt,
        "fallback_count_fmt": fallback_count_fmt,
        "cache_hit_tokens_fmt": cache_hit_tokens_fmt,
        "cache_miss_tokens_fmt": cache_miss_tokens_fmt,
        "output_tokens_fmt": output_tokens_fmt,
        "prompt_tokens_fmt": prompt_tokens_fmt,
        "active_days": active_days,
        "profile_cards": profile_cards,
        "daily_rows": daily_rows_data,
        "recent_rows": recent_rows_data,
        "error_rows": error_rows_data,
        "sidebar_profiles": sidebar_profiles,
        "budget_cards": budget_cards,
        "budget_cards_json": json.dumps(budget_cards),
        "token_mismatches": token_mismatches,
        "routing_threshold": routing_threshold,
        "cache_entries": cache_entries,
        "cache_max_entries": cache_max_entries,
        "ts_dates_json": ts_dates_json,
        "ts_costs_json": ts_costs_json,
        "ts_lats_json": ts_lats_json,
        "pp_data_json": pp_data_json,
        "pm_data_json": pm_data_json,
        "monthly_json": _monthly_data_json,
        "configured_providers_json": _configured_providers_json,
    }


def render_dashboard(config, engine, headers, profile_filter=None):
    """Render the standalone dashboard page (pre-M2 route, now a redirect target)."""
    return render_page("pages/dashboard.html", config, engine,
                       **dashboard_context(config, engine, headers, profile_filter))
