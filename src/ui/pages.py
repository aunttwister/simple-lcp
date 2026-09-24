"""Standalone page-rendering functions (HTML templates).

Each function returns a complete HTML page as a string.
Called from endpoint mixins in src.server.endpoints.
"""


def _tab(params, allowed, default) -> str:
    """Normalise ``?tab=`` (or the legacy ``?view=``) against a merged page's tabs.

    An unknown tab falls back to the page's default rather than 404-ing or
    rendering nothing: a stale bookmark should land somewhere sensible.
    """
    params = params or {}
    tab = str(params.get("tab") or params.get("view") or default).strip().lower()
    return tab if tab in allowed else default


def _logs_view(params) -> dict:
    """The logs tab's view dict: which realm, plus its server-side funnel data.

    Only the decisions realm reads data here — its funnel describes the whole
    ledger, not one page of it; every other realm is a mount of the shared table
    module (static/js/logtable.js), which fetches its own rows.
    """
    from ..api import work as work_api
    params = params or {}
    tab = str(params.get("view") or "conversations")
    view = {"tab": tab}
    if tab == "decisions":
        try:
            view.update({"decisions": work_api.decisions_view(params=params)})
        except Exception as e:  # never blank the page on a data error
            view["error"] = "%s: %s" % (type(e).__name__, e)
    elif tab not in ("conversations", "requests", "providers"):
        view["tab_error"] = "unknown view %r" % tab
    return view


def render_activity_page(config, engine=None, params=None, headers=None) -> str:
    """Render the Activity page — overview | logs (M2, two tabs since M2b).

    The observability surface in one nav entry. Server-side tabs, the shape the
    logs page already used: only the active tab's section renders, so a tab pays
    for its own queries and ships only its own script. ``view`` selects the log
    realm inside the logs tab. Alerts are deliberately not here (R10), and Usage
    left in M2b — spend and balances are a billing view, not a log.
    """
    from .render import render_page
    params = dict(params or {})
    tab = _tab(params, ("overview", "logs"), "overview")
    ctx = {"active_page": "activity", "tab": tab, "params": params}
    if tab == "logs":
        ctx["view"] = _logs_view(params)
    elif tab == "overview":
        from .dashboard import dashboard_context
        ctx.update(dashboard_context(config, engine, headers or {},
                                     params.get("profile") or None))
    return render_page("pages/activity.html", config, engine, **ctx)


def render_providers_page(config, engine=None, params=None) -> str:
    """Legacy /providers — now the Providers tab of Models (M2)."""
    return render_models_page(config, engine, {"tab": "providers"})


def render_profiles_page(config, engine=None, params=None) -> str:
    """Render the Profiles page — the profile surface (M2, four tabs since M2b).

    One nav entry for everything profile-scoped: the profiles themselves, the API
    keys that call them (R9), the cron jobs they run, and the work-layer paths they
    read. Cron and Config were nav entries of their own; both describe a profile, so
    both moved in here.

    ``?profile=`` filters the Cron tab to one profile — the jobs already belong to a
    profile, so the tab is a cross-profile view by default and a single profile's
    jobs when asked.
    """
    from .render import render_page
    # Include profile budget data
    profile_budgets = {}
    if engine is not None:
        try:
            from ..api.models import Budget, get_session as _gs
            with _gs(engine) as s:
                for b in s.query(Budget).filter(Budget.key_id.is_(None), Budget.profile.isnot(None)).all():
                    profile_budgets[b.profile] = {
                        "id": b.id, "name": b.name, "amount": b.amount,
                        "current_spend": b.current_spend, "period": b.period,
                        "threshold_pct": b.threshold_pct, "action": b.action, "status": b.status,
                        "spend_pct": round((b.current_spend / b.amount * 100) if b.amount > 0 else 0, 1),
                    }
        except Exception:
            pass
    params = dict(params or {})
    tab = _tab(params, ("profiles", "keys", "cron", "config"), "profiles")
    ctx = {"active_page": "profiles", "tab": tab, "params": params,
           "profile_budgets": profile_budgets}
    if tab == "cron":
        ctx["view"] = _work_cron_view(params)
    elif tab == "config":
        ctx["view"] = _work_config_view()
    return render_page("pages/profiles.html", config, engine, **ctx)


def render_keys_page(config, engine, params=None) -> str:
    """Legacy /keys — now the API Keys tab of Profiles (M2)."""
    return render_profiles_page(config, engine, {"tab": "keys"})


def render_usage_page(config, engine=None, params=None) -> str:
    """Render the Usage page (M2b) — spend and balances, outside Activity.

    A billing view rather than a log: what each provider cost, what is left on the
    balances, and the state of the scrape cache behind those numbers. The section is
    the same partial Activity used as its Usage tab, so ``tab="usage"`` is what the
    guard inside it matches.
    """
    from .render import render_page
    return render_page("pages/usage.html", config, engine,
                       active_page="usage", tab="usage",
                       params=dict(params or {}))


def render_logs_page(config, engine=None, params=None) -> str:
    """Legacy /logs — now the Logs tab of Activity (M2).

    ``?view=`` still selects the realm (conversations | requests | providers |
    decisions), so existing links and bookmarks keep working.
    """
    p = dict(params or {})
    p["tab"] = "logs"
    return render_activity_page(config, engine, p)


def render_alerts_page(config, engine=None) -> str:
    """Render the Alerts page (Jinja2)."""
    from .render import render_page
    return render_page("pages/alerts.html", config, engine, active_page="alerts")


def render_models_page(config, engine=None, params=None) -> str:
    """Render the Models page — the capability matrix, with providers as a tab (M2).

    A provider exists here only as *where a model comes from*, so Providers is a
    tab of this page rather than a nav entry of its own.
    """
    from .render import render_page
    return render_page("pages/models.html", config, engine,
                       active_page="models",
                       tab=_tab(params, ("matrix", "providers"), "matrix"),
                       params=dict(params or {}))


def render_setup_page(config, engine=None) -> str:
    """Render the first-run setup wizard page (Jinja2)."""
    from .render import render_page
    return render_page("pages/setup.html", config, engine, active_page="setup")


def render_work_decisions_page(config, engine=None) -> str:
    """Render the Work > Decisions page (Jinja2).

    The Work section is the work layer merged into LCP as a module: the moments
    the board recorded, attributed to the actor that decided each one.
    """
    from .render import render_page
    from ..api import work as work_api
    try:
        view = work_api.decisions_view()
    except Exception as e:  # never blank the page on a data error
        view = {
            "available": False,
            "empty": {"reason": "could not read the decisions ledger",
                      "hint": "%s: %s" % (type(e).__name__, e)},
            "funnel": None,
            "ledger": None,
        }
    return render_page("pages/work_decisions.html", config, engine,
                       active_page="work_decisions", view=view)


def render_work_tasks_page(config, engine=None, params=None) -> str:
    """Render the Work > Tasks page (Jinja2).

    A task's state is its directory, so this view reads the tree rather than a
    status field -- the state cannot drift from where the item actually lives.
    ``params`` carries the server-side filter/pagination query string.

    Two tabs, one page: ``?view=tasks`` (default) is the task tree itself;
    ``?view=assessments`` is the session-assessment ledger the 4h L1 round
    writes. They share a sidebar entry because they describe the same objects —
    a task's directory state, and what the round decided about it.
    """
    from .render import render_page
    from ..api import work_tasks

    params = params or {}
    tab = str(params.get("view") or "tasks").strip().lower()
    if tab not in ("tasks", "assessments"):
        tab = "tasks"
    try:
        if tab == "assessments":
            view = work_tasks.assessments_view()
        else:
            view = work_tasks.tasks_view(params)
    except Exception as e:  # never blank the page on a data error
        view = {
            "available": False,
            "empty": {"reason": "could not read the task tree",
                      "hint": "%s: %s" % (type(e).__name__, e)},
            "counts": {}, "total": 0, "tasks": [], "todos": None, "conflicts": [],
        }
    view["tab"] = tab
    return render_page("pages/work_tasks.html", config, engine,
                       active_page="work_tasks", view=view)


def render_work_fleet_page(config, engine=None) -> str:
    """Render the Work > Fleet page (Jinja2).

    Reads LCP's own provider/profile APIs, so the chain shown is the chain LCP
    will actually use -- not a second copy that can drift from it.
    """
    from .render import render_page
    from ..api import work_fleet
    try:
        view = work_fleet.fleet_view()
    except Exception as e:  # never blank the page on a data error
        view = {
            "available": False,
            "empty": {"reason": "could not read the fleet",
                      "hint": "%s: %s" % (type(e).__name__, e)},
            "summary": None, "flaky": [], "profiles": [],
            "failover_moments": [], "failover_stats": None, "routing": None,
        }
    return render_page("pages/work_fleet.html", config, engine,
                       active_page="work_fleet", view=view)


def _work_cron_view(params=None) -> dict:
    """The Cron tab's data: the host snapshot, recent ops, and the resolved sources.

    The LCP container cannot read ``/root/.hermes/profiles``, so everything here
    comes from the snapshot and op spool the host-side timer writes. ``?profile=``
    narrows the profile list to one — the jobs already belong to a profile, so the
    tab is a cross-profile view by default and a single profile's jobs when asked.
    """
    from ..api import work_cron
    try:
        view = work_cron.cron_view()
    except Exception as e:  # never blank the tab on a data error
        view = {
            "available": False,
            "hint": "cron view failed: %s: %s" % (type(e).__name__, e),
            "error": str(e), "generated_at": None, "generated_rel": None,
            "counts": {"total": 0, "active": 0, "paused": 0, "disabled": 0, "error": 0},
            "profiles": [],
        }
    try:
        view["ops"] = work_cron.cron_ops_view(limit=10).get("pending", []) + \
            work_cron.cron_ops_view(limit=10).get("done", [])[:10]
        # newest first
        view["ops"].sort(key=lambda o: (o.get("result") or {}).get("executed_at")
                         or o.get("created_at") or "", reverse=True)
    except Exception:
        view["ops"] = []
    try:
        from ..api import work_sources
        view["sources"] = work_sources.resolved_view()
    except Exception:
        view["sources"] = None
    wanted = str((params or {}).get("profile") or "").strip().lower()
    if wanted and isinstance(view.get("profiles"), list):
        kept = [p for p in view["profiles"]
                if str(p.get("profile", "")).lower() == wanted]
        view["profiles"] = kept
        view["profile_filter"] = wanted
    return view


def _work_config_view() -> dict:
    """The Config tab's data: every path the work layer reads, per profile.

    ``work-sources.json`` already carries a per-profile section (``tasks_root`` and
    ``cron_store`` overrides on top of the host defaults), so this view is the
    per-profile config — the page edits it, the host-side collector picks it up.
    """
    from ..api import work_sources
    try:
        return {"sources": work_sources.resolved_view()}
    except Exception:
        return {"sources": None}


def render_work_cron_page(config, engine=None, params=None) -> str:
    """Legacy /work/cron — now the Cron tab of Profiles (M2b)."""
    p = dict(params or {})
    p["tab"] = "cron"
    return render_profiles_page(config, engine, p)


def render_work_config_page(config, engine=None, params=None) -> str:
    """Legacy /work/config — now the Config tab of Profiles (M2b)."""
    p = dict(params or {})
    p["tab"] = "config"
    return render_profiles_page(config, engine, p)


def render_work_conversations_page(config, engine=None, params=None) -> str:
    """Render the Work > Conversations page (Jinja2).

    The unified conversation log: every conversation groups its request rows
    and the router's provider-decision rows by conversation_id, newest first,
    with a deterministic summary (first user turn + counts). Expanding a
    conversation shows the chronological sequence of events.
    """
    from .render import render_page
    from ..api import work_conversations
    try:
        view = work_conversations.conversations_view(params or {})
        for c in view.get("conversations", []):
            det = work_conversations.conversation_detail(c["id"], limit=50)
            c["detail"] = det.get("events", [])
            c["detail_truncated"] = det.get("truncated", False)
    except Exception as e:
        view = {
            "available": False,
            "error": "%s: %s" % (type(e).__name__, e),
            "conversations": [], "total": 0,
            "filter": {"per": "20", "page": 1, "pages": 1,
                       "profile": "", "profiles": []},
        }
    return render_page("pages/work_conversations.html", config, engine,
                       active_page="work_conversations", view=view)



