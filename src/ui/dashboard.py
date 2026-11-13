"""Dashboard renderer for the LCP gateway.

Builds the view-context for the Overview homepage (CUR-330): the page the LCP
brand link opens. The arithmetic lives here and nowhere else — the Activity
page no longer renders an overview tab, it links here.

Design decisions recorded for the next reader:

* **Period is a server-side filter.** The old "14-day" trend labelled an
  all-time series because ``.limit(14)`` ran against a query that grouped all
  time; the label lied. Now every card, breakdown and series is computed over
  the same date cutoff (?period=7|14|30|90|all, default 14), so the number and
  the chart can never disagree.
* **Cost is split by billing model**, not shown as one "total": DeepSeek is
  genuinely metered (what the DB says is what the invoice says), Command Code /
  OpenCode are subscriptions whose rows are usage *valued* at list price, and
  the local engines carry synthetic near-zero costs. One grand total mixed
  three different kinds of dollars.
* **The old requests card counted every successful request as a "fallback"**
  (success==1 AND provider != 'error' — 139,206 of 139,206 = 99.9%). A real
  fallback is a success served by a provider that is NOT the profile's primary
  chain step; errors are the success==0 rows.
* **Everything is click-through.** Each card and each trend bar opens the
  right-hand detail panel (same page, server-computed ``breakdown`` payload) —
  no profile dropdown, no per-profile mini cards.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from ..api.models import Request as RequestModel, get_session
from ..api.prompt_cache import get_prompt_cache
from ..api.token_verifier import get_token_verifier
from ..api.router import get_dynamic_router
from ..api.logging_config import get_logger

logger = get_logger("lcp.dashboard")

from .render import render_page

# CUR-330: billing-model families. A provider's family decides which bucket of
# the cost card its spend lands in. Declared `type` (the provider_types store)
# wins; the name heuristics only catch what was never classified.
_METERED_TYPES = {"deepseek"}
_SUB_TYPES = {"opencode", "commandcode"}
_LOCAL_TYPES = {"local"}
# Providers that never name a real route: error rows and unrouted failures.
_NON_ROUTE_PROVIDERS = {"error", "unknown", ""}

_PERIOD_DAYS = {"7": 7, "14": 14, "30": 30, "90": 90}


def _period_cutoff(period: str) -> str | None:
    """ISO prefix a `timestamp` string must be >= to fall inside the period.

    The stored timestamps carry a 'T' separator, so the cutoff must too — a
    space-separated cutoff (datetime('now', …) style) compares as SMALLER than
    every same-day row and the filter silently matches nothing (see the
    costs.db timestamp-comparison pitfall in the repo skill notes).
    """
    days = _PERIOD_DAYS.get(str(period))
    if days is None:
        return None
    start = (datetime.now(timezone.utc).date() - timedelta(days=days - 1))
    return f"{start.isoformat()}T00:00:00"


def dashboard_context(config, engine, headers, profile_filter=None, period="14"):
    """Build the Overview homepage view-context.

    Returns the kwargs ``render_page`` expects. ``period`` selects the date
    window every card, series and breakdown is computed over.
    """

    from sqlalchemy import func, case

    period = str(period) if str(period) in ("7", "14", "30", "90", "all") else "14"
    cutoff = _period_cutoff(period)
    profile_filter = profile_filter or None

    # Map a raw stored model ID (e.g. "deepseek/deepseek-v4-pro" or
    # "deepseek-v4-pro") to its logical gateway name so the graphs group and
    # label models consistently (same as Models/Benchmarks pages).
    def _logical(model: str) -> str:
        if not model:
            return model or ""
        try:
            db_path = str(engine.url.database) if engine is not None else "data/costs.db"
            from ..api.router import logical_model_name
            resolved = logical_model_name(model, db_path)
            # Phase 4: fall through to the identity registry only when the legacy
            # index could not name the model (it keys on the provider NAME, which
            # is renamed in this estate, so newer spellings resolve to nothing).
            if resolved and resolved != model:
                return resolved
            try:
                from ..api import model_identity as _mi
                con = _mi.connect(db_path, ro=True)
                try:
                    names = _mi.canonical_names(None, model, con=con)
                finally:
                    con.close()
                if names:
                    return names[0]
            except Exception:  # noqa: BLE001
                pass
            return resolved
        except Exception:  # noqa: BLE001 — never break the dashboard on a name
            return model

    def _family_of(provider_name: str) -> str:
        """Billing-model family of a provider: metered | subs | local | unknown."""
        name = (provider_name or "").strip().lower()
        if name in _NON_ROUTE_PROVIDERS:
            return "nonroute"
        declared = None
        try:
            declared = config.provider_type(name)
        except Exception:  # noqa: BLE001 — a mock/incomplete config degrades
            declared = None
        if not isinstance(declared, str):
            declared = None
        declared = (declared or "").strip().lower()
        if declared in _METERED_TYPES:
            return "metered"
        if declared in _SUB_TYPES:
            return "subs"
        if declared in _LOCAL_TYPES:
            return "local"
        # Name heuristics — only for providers the type store never classified.
        if name.startswith("local") or name == "llamacpp":
            return "local"
        if name.startswith("commandcode") or name.startswith("opencode"):
            return "subs"
        if "deepseek" in name:
            return "metered"
        return "unknown"

    # Primary chain provider per profile — the step a request is SUPPOSED to hit.
    # A success served by anything else is a fallback (CUR-330 item 1).
    def _primary_of(profile: str) -> str:
        pcfg = (getattr(config, "profiles", {}) or {}).get(profile) or {}
        chain = pcfg.get("chain") or []
        if chain and isinstance(chain[0], dict):
            return (chain[0].get("provider") or "").strip()
        if chain and not isinstance(chain[0], dict):
            return (getattr(chain[0], "provider", "") or "").strip()
        return ""

    try:
        with get_session(engine) as session:
            base = session.query(RequestModel)
            if cutoff:
                base = base.filter(RequestModel.timestamp >= cutoff)
            if profile_filter:
                base = base.filter(RequestModel.profile == profile_filter)

            # ── One aggregate row over the period ──
            agg = base.with_entities(
                func.count(RequestModel.id).label("n"),
                func.coalesce(func.sum(case((RequestModel.success == 1, 1), else_=0)), 0).label("ok"),
                func.coalesce(func.sum(case((RequestModel.success == 0, 1), else_=0)), 0).label("err"),
                func.coalesce(func.sum(RequestModel.cost), 0.0).label("cost"),
                func.coalesce(func.sum(RequestModel.prompt_tokens), 0).label("ptok"),
                func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("ctok"),
                func.coalesce(func.sum(RequestModel.cache_hit_tokens), 0).label("chit"),
                func.coalesce(func.sum(RequestModel.cache_miss_tokens), 0).label("cmiss"),
                func.count(func.distinct(func.substr(RequestModel.timestamp, 1, 10))).label("days"),
            ).first()

            ok_requests = int(agg.ok) if agg else 0
            error_count = int(agg.err) if agg else 0
            total_cost = float(agg.cost) if agg else 0.0
            prompt_tokens = int(agg.ptok) if agg else 0
            completion_tokens = int(agg.ctok) if agg else 0
            cache_hit_tokens = int(agg.chit) if agg else 0
            cache_miss_tokens = int(agg.cmiss) if agg else 0
            active_days = int(agg.days) if agg else 0

            # ── Per-provider period aggregates (cost split + breakdowns) ──
            prov_q = base.with_entities(
                RequestModel.provider.label("provider"),
                func.count(RequestModel.id).label("reqs"),
                func.coalesce(func.sum(case((RequestModel.success == 1, 1), else_=0)), 0).label("ok"),
                func.coalesce(func.sum(case((RequestModel.success == 0, 1), else_=0)), 0).label("err"),
                func.coalesce(func.sum(RequestModel.cost), 0.0).label("cost"),
                func.coalesce(func.sum(RequestModel.prompt_tokens), 0).label("ptok"),
                func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("ctok"),
                func.coalesce(func.sum(RequestModel.cache_hit_tokens), 0).label("chit"),
                func.coalesce(func.sum(RequestModel.cache_miss_tokens), 0).label("cmiss"),
            ).group_by(RequestModel.provider).all()

            provider_rows = []
            metered_cost = subs_cost = local_cost = unknown_cost = 0.0
            for r in prov_q:
                family = _family_of(r.provider)
                row = {
                    "provider": r.provider,
                    "family": family,
                    "reqs": int(r.reqs), "ok": int(r.ok), "err": int(r.err),
                    "cost": float(r.cost), "prompt": int(r.ptok),
                    "completion": int(r.ctok), "hit": int(r.chit),
                    "miss": int(r.cmiss),
                    "cache_ratio": (int(r.chit) / (int(r.chit) + int(r.cmiss)) * 100)
                    if (int(r.chit) + int(r.cmiss)) > 0 else None,
                }
                provider_rows.append(row)
                if family == "metered":
                    metered_cost += row["cost"]
                elif family == "subs":
                    subs_cost += row["cost"]
                elif family == "local":
                    local_cost += row["cost"]
                elif family != "nonroute":
                    unknown_cost += row["cost"]

            # ── Per-profile period aggregates + real fallback counts ──
            prof_q = base.with_entities(
                RequestModel.profile.label("profile"),
                func.count(RequestModel.id).label("reqs"),
                func.coalesce(func.sum(case((RequestModel.success == 1, 1), else_=0)), 0).label("ok"),
                func.coalesce(func.sum(case((RequestModel.success == 0, 1), else_=0)), 0).label("err"),
                func.coalesce(func.sum(RequestModel.cost), 0.0).label("cost"),
                func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("ctok"),
            ).group_by(RequestModel.profile).all()

            # (profile, provider) -> ok-request count, for fallback detection.
            pp_q = base.with_entities(
                RequestModel.profile.label("profile"),
                RequestModel.provider.label("provider"),
                func.count(RequestModel.id).label("n"),
            ).filter(RequestModel.success == 1).group_by(
                RequestModel.profile, RequestModel.provider).all()
            fallback_by_profile: dict[str, int] = {}
            for r in pp_q:
                primary = _primary_of(r.profile)
                if r.provider not in _NON_ROUTE_PROVIDERS and r.provider != primary:
                    fallback_by_profile[r.profile] = (
                        fallback_by_profile.get(r.profile, 0) + int(r.n))

            profile_rows = []
            fallback_count = 0
            for r in prof_q:
                fb = fallback_by_profile.get(r.profile, 0)
                fallback_count += fb
                profile_rows.append({
                    "profile": r.profile,
                    "reqs": int(r.reqs), "ok": int(r.ok), "err": int(r.err),
                    "fallbacks": fb,
                    "cost": float(r.cost), "completion": int(r.ctok),
                })

            # ── Per-model period aggregates (tokens breakdown) ──
            model_q = base.with_entities(
                RequestModel.model.label("model"),
                func.count(RequestModel.id).label("reqs"),
                func.coalesce(func.sum(RequestModel.prompt_tokens), 0).label("ptok"),
                func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("ctok"),
                func.coalesce(func.sum(RequestModel.cache_hit_tokens), 0).label("chit"),
                func.coalesce(func.sum(RequestModel.cost), 0.0).label("cost"),
            ).group_by(RequestModel.model).all()
            model_rows_map: dict[str, dict] = {}
            for r in model_q:
                logical = _logical(r.model)
                row = model_rows_map.setdefault(logical, {
                    "model": logical, "reqs": 0, "prompt": 0,
                    "completion": 0, "hit": 0, "cost": 0.0,
                })
                row["reqs"] += int(r.reqs)
                row["prompt"] += int(r.ptok)
                row["completion"] += int(r.ctok)
                row["hit"] += int(r.chit)
                row["cost"] += float(r.cost)
            model_rows = sorted(model_rows_map.values(),
                                key=lambda m: m["completion"], reverse=True)

            # ── Daily series over the period (cost + tokens + reqs) ──
            day_q = base.with_entities(
                func.substr(RequestModel.timestamp, 1, 10).label("date"),
                func.count(RequestModel.id).label("reqs"),
                func.coalesce(func.sum(case((RequestModel.success == 1, 1), else_=0)), 0).label("ok"),
                func.coalesce(func.sum(RequestModel.cost), 0.0).label("cost"),
                func.coalesce(func.sum(RequestModel.prompt_tokens), 0).label("ptok"),
                func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("ctok"),
                func.coalesce(func.avg(case((RequestModel.success == 1, RequestModel.latency_ms), else_=None)), 0.0).label("avg_lat"),
            ).group_by(func.substr(RequestModel.timestamp, 1, 10)).all()
            day_rows = [{
                "date": r.date, "reqs": int(r.reqs), "ok": int(r.ok),
                "cost": float(r.cost), "prompt": int(r.ptok),
                "completion": int(r.ctok), "avg_lat": float(r.avg_lat),
                "providers": [], "profiles": [],
            } for r in sorted(day_q, key=lambda x: x.date)]
            day_index = {d["date"]: d for d in day_rows}

            # Per (day, provider) drill-down for the trend-click detail panel.
            dp_q = base.with_entities(
                func.substr(RequestModel.timestamp, 1, 10).label("date"),
                RequestModel.provider.label("provider"),
                func.count(RequestModel.id).label("reqs"),
                func.coalesce(func.sum(RequestModel.cost), 0.0).label("cost"),
                func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("ctok"),
            ).group_by(
                func.substr(RequestModel.timestamp, 1, 10),
                RequestModel.provider).all()
            for r in dp_q:
                d = day_index.get(r.date)
                if d is None:
                    continue
                d["providers"].append({
                    "provider": r.provider, "family": _family_of(r.provider),
                    "reqs": int(r.reqs), "cost": float(r.cost),
                    "completion": int(r.ctok),
                })
            # Per (day, profile) drill-down too — the day panel shows both cuts.
            dpr_q = base.with_entities(
                func.substr(RequestModel.timestamp, 1, 10).label("date"),
                RequestModel.profile.label("profile"),
                func.count(RequestModel.id).label("reqs"),
                func.coalesce(func.sum(RequestModel.cost), 0.0).label("cost"),
                func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("ctok"),
            ).group_by(
                func.substr(RequestModel.timestamp, 1, 10),
                RequestModel.profile).all()
            for r in dpr_q:
                d = day_index.get(r.date)
                if d is None:
                    continue
                d["profiles"].append({
                    "profile": r.profile, "reqs": int(r.reqs),
                    "cost": float(r.cost), "completion": int(r.ctok),
                })

            # ── Trend series for Chart.js (per-day aggregates) ──
            ts_dates = [d["date"] for d in day_rows]
            ts_costs = [round(d["cost"], 4) for d in day_rows]
            ts_ctok = [d["completion"] for d in day_rows]
            ts_ptok = [d["prompt"] for d in day_rows]
            ts_reqs = [d["ok"] for d in day_rows]

            # ── Per-profile trend series (costs + completion tokens) ──
            ppr_q = base.with_entities(
                func.substr(RequestModel.timestamp, 1, 10).label("date"),
                RequestModel.profile.label("profile"),
                func.coalesce(func.sum(RequestModel.cost), 0.0).label("cost"),
                func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("ctok"),
            ).filter(RequestModel.success == 1).group_by(
                func.substr(RequestModel.timestamp, 1, 10),
                RequestModel.profile).all()
            pp_data = {"dates": ts_dates, "profiles": {}}
            date_to_idx = {d: i for i, d in enumerate(ts_dates)}
            for r in ppr_q:
                p = pp_data["profiles"].setdefault(
                    r.profile, {"costs": [0.0] * len(ts_dates),
                                "toks": [0] * len(ts_dates)})
                idx = date_to_idx[r.date]
                p["costs"][idx] = round(float(r.cost), 4)
                p["toks"][idx] = int(r.ctok)

            # ── Per-model trend series (costs + completion tokens) ──
            pmm_q = base.with_entities(
                func.substr(RequestModel.timestamp, 1, 10).label("date"),
                RequestModel.model.label("model"),
                func.coalesce(func.sum(RequestModel.cost), 0.0).label("cost"),
                func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("ctok"),
            ).filter(RequestModel.success == 1).group_by(
                func.substr(RequestModel.timestamp, 1, 10),
                RequestModel.model).all()
            pm_data = {"dates": ts_dates, "models": {}}
            for r in pmm_q:
                logical = _logical(r.model)
                m = pm_data["models"].setdefault(
                    logical, {"costs": [0.0] * len(ts_dates),
                              "toks": [0] * len(ts_dates)})
                idx = date_to_idx[r.date]
                m["costs"][idx] = round(float(r.cost), 4)
                m["toks"][idx] = int(r.ctok)
    except Exception:
        logger.error("dashboard_query_failed", exc_info=True)
        ok_requests = fallback_count = error_count = 0
        total_cost = metered_cost = subs_cost = local_cost = unknown_cost = 0.0
        prompt_tokens = completion_tokens = 0
        cache_hit_tokens = cache_miss_tokens = 0
        cache_hit_rate = 0.0
        cache_savings = 0.0
        active_days = 0
        provider_rows, profile_rows, model_rows, day_rows = [], [], [], []
        ts_dates, ts_costs, ts_ctok, ts_ptok, ts_reqs = [], [], [], [], []
        pp_data = {"dates": [], "profiles": {}}
        pm_data = {"dates": [], "models": {}}
    else:
        cache_total = cache_hit_tokens + cache_miss_tokens
        cache_hit_rate = (cache_hit_tokens / cache_total * 100) if cache_total > 0 else 0.0

        # ── Cache savings estimate (period) ──
        # cache_hit tokens bill at the provider's cache_hit rate instead of the
        # cache_miss rate; the difference, valued per model, is what prefix
        # caching saved. Same arithmetic the old dashboard used, now scoped to
        # the selected period instead of the top-30 daily table.
        def _savings_for_model(model: str, hit_tokens: int) -> float:
            if hit_tokens <= 0:
                return 0.0
            for p_name, p_cfg in (getattr(config, "providers", {}) or {}).items():
                if model in (p_cfg.get("models") or []):
                    try:
                        pricing = config.get_pricing(p_name, model)
                        return (hit_tokens / 1_000_000) * (
                            pricing["cache_miss"] - pricing["cache_hit"]
                        )
                    except Exception:  # noqa: BLE001
                        pass
            return 0.0

        cache_savings = sum(
            _savings_for_model(cast(Any, m)["model"], cast(Any, m)["hit"])
            for m in model_rows)

        # Provider-level savings rollup for the cache detail panel.
        model_to_providers: dict[str, list[str]] = {}
        for p_name, p_cfg in (getattr(config, "providers", {}) or {}).items():
            for m in (p_cfg.get("models") or []):
                model_to_providers.setdefault(m, []).append(p_name)
        prov_savings: dict[str, float] = {}
        for m in model_rows:
            saved = _savings_for_model(m["model"], m["hit"])
            if not saved:
                continue
            owners = model_to_providers.get(m["model"]) or []
            for owner in owners or ["unknown"]:
                prov_savings[owner] = prov_savings.get(owner, 0.0) + saved
        for row in provider_rows:
            row["saved"] = round(prov_savings.get(row["provider"], 0.0), 4)

    # ── Helper: format large numbers ──
    def _fmt_num(n):
        if n is None:
            return "0"
        # A B tier was missing, so billions rendered as "6217.8M"; counts under
        # 10K keep full precision with a comma instead of fake "1.5K".
        if n >= 1_000_000_000:
            return f"{n/1_000_000_000:.1f}B"
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        if n >= 10_000:
            return f"{n/1_000:.1f}K"
        return f"{int(n):,}"

    def _fmt_cost2(c):
        return f"${float(c):,.2f}"

    # ── Detail-panel payload (CUR-330 item 10): everything a card or a trend
    #    bar can open, precomputed server-side and embedded as JSON. ──
    breakdown = {
        "period": period,
        "cost": {
            "totals": {"metered": round(metered_cost, 4),
                       "subs": round(subs_cost, 4),
                       "local": round(local_cost, 4),
                       "unknown": round(unknown_cost, 4),
                       "total": round(total_cost, 4)},
            "providers": provider_rows,
            "profiles": sorted(profile_rows, key=lambda p: -p["cost"]),
            "active_days": active_days,
        },
        "tokens": {
            "totals": {"completion": completion_tokens, "prompt": prompt_tokens},
            "providers": sorted(provider_rows, key=lambda p: -p["completion"]),
            "models": model_rows[:25],
        },
        "requests": {
            "totals": {"ok": ok_requests, "fallbacks": fallback_count,
                       "errors": error_count,
                       "fb_pct": round(fallback_count / ok_requests * 100, 1)
                       if ok_requests else 0.0},
            "profiles": sorted(profile_rows, key=lambda p: -p["reqs"]),
        },
        "cache": {
            "totals": {"hit": cache_hit_tokens, "miss": cache_miss_tokens,
                       "ratio": round(cache_hit_rate, 1),
                       "saved": round(cache_savings, 4)},
            "providers": sorted(
                [p for p in provider_rows if (p["hit"] + p["miss"]) > 0],
                key=lambda p: -(p["hit"] + p["miss"])),
        },
        "days": day_rows,
    }

    # ── Data for the Jinja2 dashboard template ──
    host = headers.get("Host", "localhost:8734")
    scheme = "https" if (
        headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"
        or headers.get("X-Forwarded-Scheme") == "https"
    ) else "http"
    host_url = f"{scheme}://{host}"

    import src as _src
    version = getattr(_src, "__version__", "?")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    period_options = [
        {"value": "7", "label": "7 days"},
        {"value": "14", "label": "14 days"},
        {"value": "30", "label": "30 days"},
        {"value": "90", "label": "90 days"},
        {"value": "all", "label": "All time"},
    ]
    for opt in period_options:
        opt["active"] = opt["value"] == period

    filter_title = f" — {profile_filter.upper()}" if profile_filter else ""

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

    cache = get_prompt_cache()
    cache_stats = cache.stats

    return {
        "active_page": "home",
        "tab": "overview",
        "profile_filter": profile_filter,
        "filter_title": filter_title,
        "period": period,
        "period_options": period_options,
        "now_utc": now_utc,
        "version": version,
        "host_url": host_url,
        # Cards (period-scoped)
        "ok_requests_fmt": f"{ok_requests:,}",
        "fallback_count_fmt": f"{fallback_count:,}",
        "error_count_fmt": f"{error_count:,}",
        "fb_pct_fmt": f"{fallback_count / ok_requests * 100:.1f}%" if ok_requests else "0.0%",
        "usage_value_fmt": _fmt_cost2(total_cost),
        "metered_cost_fmt": _fmt_cost2(metered_cost),
        "subs_cost_fmt": _fmt_cost2(subs_cost),
        "local_cost_fmt": _fmt_cost2(local_cost),
        "cache_hit_rate_fmt": f"{cache_hit_rate:.1f}%",
        "cache_hit_tokens_fmt": _fmt_num(cache_hit_tokens),
        "cache_miss_tokens_fmt": _fmt_num(cache_miss_tokens),
        "cache_savings_fmt": _fmt_cost2(cache_savings),
        "output_tokens_fmt": _fmt_num(completion_tokens),
        "prompt_tokens_fmt": _fmt_num(prompt_tokens),
        "active_days": active_days,
        # Trend series
        "ts_dates_json": json.dumps(ts_dates),
        "ts_costs_json": json.dumps(ts_costs),
        "ts_ctok_json": json.dumps(ts_ctok),
        "ts_ptok_json": json.dumps(ts_ptok),
        "ts_reqs_json": json.dumps(ts_reqs),
        "pp_data_json": json.dumps(pp_data),
        "pm_data_json": json.dumps(pm_data),
        # Detail-panel payload
        "breakdown_json": json.dumps(breakdown),
        # Retained for the global header widget + sidebar profiles
        "sidebar_profiles": [],
        "budget_cards": budget_cards,
        "budget_cards_json": json.dumps(budget_cards),
        "cache_entries": cache_stats["entries"],
        "cache_max_entries": cache_stats.get("max_entries", "N/A"),
        "token_mismatches": (get_token_verifier().stats or {}).get("mismatches", 0)
        if hasattr(get_token_verifier(), "stats") else 0,
        "routing_threshold": getattr(get_dynamic_router(), "config", {}).get(
            "flash_threshold_tokens", 4096),
        "monthly_json": json.dumps({}),
        "configured_providers_json": json.dumps(
            sorted((getattr(config, "providers", {}) or {}).keys())),
    }


def render_dashboard(config, engine, headers, profile_filter=None, period="14"):
    """Legacy entry point — now renders the Overview homepage (CUR-330)."""
    return render_page("pages/home.html", config, engine,
                       **dashboard_context(config, engine, headers,
                                           profile_filter=profile_filter,
                                           period=period))
