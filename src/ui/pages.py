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


def _crumbs(*parts) -> list:
    """Breadcrumb entries from (label, href) pairs.

    The last entry is the current location and carries no href, so the template
    styles the terminus without needing a second flag. Pages that are only one
    level deep pass a single pair and get a single crumb — a location is still
    worth naming even when there is no trail behind it.
    """
    out = []
    for i, (label, href) in enumerate(parts):
        out.append({"label": label, "href": None if i == len(parts) - 1 else href})
    return out


def _profile_budgets(engine) -> dict:
    """Per-profile budget rows, keyed by profile name. {} when there is no engine."""
    budgets = {}
    if engine is None:
        return budgets
    try:
        from ..api.models import Budget, get_session as _gs
        with _gs(engine) as s:
            for b in s.query(Budget).filter(Budget.key_id.is_(None), Budget.profile.isnot(None)).all():
                budgets[b.profile] = {
                    "id": b.id, "name": b.name, "amount": b.amount,
                    "current_spend": b.current_spend, "period": b.period,
                    "threshold_pct": b.threshold_pct, "action": b.action, "status": b.status,
                    "spend_pct": round((b.current_spend / b.amount * 100) if b.amount > 0 else 0, 1),
                }
    except Exception:
        pass
    return budgets


def _profile_card(config, name, pcfg, budgets, agent="", kind="profile",
                  mapping_source="none", routed_by=None, lane="",
                  routing_on=None) -> dict:
    """One card for the profiles grid (M2d).

    One kind of card, one builder, since M2d the grid is grouped by *what a profile
    is* rather than by which side of the lane/agent link a name comes from:

    * a **profile** declares the agent profile behind it (`agent_profile`) — the
      gateway lanes that have an agent, whose skills, memory and task tree the card
      leads to;
    * an **lcp_only** profile is a gateway profile with no agent behind it. That is
      a kind of profile, not an incomplete one: it routes, it holds API keys, it has
      a chain. Nothing about it is missing.

    The description is clamped to ten words *here* rather than in CSS: a clamp that
    only exists in a stylesheet still ships the whole paragraph in the page source.
    """
    from ..api import profile_data
    try:
        pcfg = pcfg or {}
        desc = str(pcfg.get("description") or "").strip()
        if not desc and agent:
            # No description written yet: the agent's own SOUL.md says what it is
            # for, which is exactly what the card is asking.
            desc = profile_data.agent_summary(agent)
        words = desc.split()
        short = " ".join(words[:10]) + ("\u2026" if len(words) > 10 else "")
        chain = pcfg.get("chain", []) or []
        steps = []
        for st in chain:
            if isinstance(st, dict):
                steps.append("%s/%s" % (st.get("provider", ""), st.get("model", "")))
            else:
                steps.append("%s/%s" % (getattr(st, "provider", ""), getattr(st, "model", "")))
        auth_required = pcfg.get("auth_required", True)
        pb = budgets.get(name)
        budget_label = ""
        if pb:
            budget_label = "$%.2f/$%.0f (%s%%)" % (pb["current_spend"], pb["amount"], pb["spend_pct"])
        routing = str(pcfg.get("routing") or "")
        intents = list(pcfg.get("intents") or [])
        # A routing mode is only a *choice* when there is more than one model to
        # choose between; one step means the dynamic router has nothing to decide.
        # `routing_on` is the router's own answer for this profile (None when the
        # router could not be asked) and beats the declared field, because the badge
        # says what the profile *does*, not what it was asked to do.
        if len(chain) <= 1:
            routing_effective = "static"
        elif routing_on is not None:
            routing_effective = "dynamic" if routing_on else "static"
        elif routing:
            routing_effective = routing
        else:
            # The router did not say and nothing is declared: no badge. A label here
            # would be a claim about behaviour that nothing supports.
            routing_effective = ""
        if kind == "lcp_only":
            kind_label = "profile without an agent"
        else:
            kind_label = "profile"
            if mapping_source == "suggested":
                kind_label += " \u00b7 mapping suggested"
        others = [n for n in (routed_by or []) if n != agent and n != name]
        # The photo belongs to a name; a profile whose artefacts live under an agent
        # profile reads (and writes) that profile's picture rather than showing a
        # broken image at its own URL.
        avatar_name = name
        if profile_data.avatar_for(name) is None and agent and profile_data.avatar_for(agent) is not None:
            avatar_name = agent
        return {
            "name": name,
            "initials": (name[:2] or "?").upper(),
            "href": "/profiles/" + name,
            "kind": kind,
            "kind_label": kind_label,
            # `lane` is the gateway path a request arrives on. An LCP profile IS one
            # (its name is the path segment); an agent profile that owns none has the
            # lane it borrows; an LCP-only profile is a profile in its own right.
            "lane": (lane if kind == "agent" else (name if kind == "profile" else "")),
            "agent": agent,
            "mapping_source": mapping_source,
            "routed_by": others,
            "description": desc,
            "short_description": short,
            "words": len(words),
            "avatar": avatar_name != name or profile_data.avatar_for(name) is not None,
            "avatar_name": avatar_name,
            "auth_label": "key required" if auth_required else "public",
            "budget_label": budget_label,
            "chain_label": " \u2192 ".join(steps),
            "chain_steps": len(steps),
            "routing": routing,
            "routing_effective": routing_effective,
            "routing_label": routing_effective,
            "intents": intents,
            "intents_label": ", ".join(intents) if intents else "",
        }
    except profile_data.BadProfileName:
        return None


def _profile_groups(config, budgets, routing_on=None) -> dict:
    """The profiles grid, grouped by what a profile is (M2d).

    Three groups, and only the first two are LCP profiles:

    ``declared``    — gateway profiles, each with the agent behind it. The mapping
                      is the declared ``agent_profile`` when there is one; when there
                      is not, the proposal derived from the Hermes side is shown and
                      flagged, so a suggestion is never mistaken for a decision.
    ``lcp_only``    — gateway profiles with no agent at all (they were created in
                      LCP and route for other things). First-class, not incomplete.
    ``uncovered``   — Hermes profiles no profile claims: the candidates the create
                      flow offers. `routes_through` says where that profile's traffic
                      goes today, which is the interesting part — a profile routing
                      through someone else's profile is the one worth its own.

    `effective` (declared, falling back to the proposal) is what decides coverage:
    if a profile's card already presents an agent, that agent is not also "uncovered".
    """
    from ..api import profile_data
    from ..api.config import validate_profile_fields
    profiles = config.profiles if (config is not None and hasattr(config, "profiles")) else {}
    lanes = list(profiles.keys())
    agents = profile_data.agent_profiles(lanes)
    proposals = profile_data.mapping_suggestions(lanes, agents)

    declared, lcp_only, effective = [], [], {}
    for lane in lanes:
        pcfg = profiles.get(lane) or {}
        try:
            fields = validate_profile_fields(lane, pcfg)
        except Exception:  # never blank the grid on one bad profile
            fields = {"agent_profile": "", "routing": "", "intents": [],
                      "description": str(pcfg.get("description") or "")}
        agent = fields["agent_profile"]
        source = "declared"
        if not agent:
            agent = proposals.get(lane, "")
            source = "suggested" if agent else "none"
        effective[lane] = agent
        card = _profile_card(config, lane, pcfg, budgets, agent=agent,
                             kind="profile" if agent else "lcp_only",
                             mapping_source=source,
                             routed_by=profile_data.agents_for_lane(lane, agents),
                             routing_on=(routing_on or {}).get(lane))
        if card:
            (declared if agent else lcp_only).append(card)
    uncovered = profile_data.uncovered_agents(lanes, mapping=effective, agents=agents)
    return {"declared": declared, "lcp_only": lcp_only, "uncovered": uncovered}


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
    ctx = {"active_page": "activity", "tab": tab, "params": params,
           "crumbs": _crumbs(("Activity", "/activity"),
                             ("Logs" if tab == "logs" else "Overview", None))}
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
    """Render the Profiles page — one card per profile (M2c, level 2).

    This page is the directory. In M2b it also carried API Keys, Cron and Config
    as tabs; in M2c every artefact that belongs to a *profile* moved under the
    profile itself (``/profiles/<name>``), because that is where a reader looks
    for it and because keys are per-profile by design (R9). Config stays: it is the
    one genuinely cross-profile view, the defaults plus every profile's overrides
    in a single table.
    """
    from .render import render_page
    params = dict(params or {})
    tab = _tab(params, ("profiles", "config"), "profiles")
    budgets = _profile_budgets(engine)
    # One router call for the whole grid: the badge on each card says what the profile
    # does, so it has to come from the router rather than the profile's declared field.
    _status = _routing_status_safe(config)
    names_for_status = list(config.profiles.keys()) if (
        config is not None and hasattr(config, "profiles")) else []
    groups = _profile_groups(config, budgets,
                             routing_on={n: _effective_routing_on(_status, n)
                                         for n in names_for_status})
    # Every Hermes profile directory, for the Edit modal's Agent Profile selector: a
    # profile may legitimately be mapped to any of them, including the legacy copies.
    from ..api import profile_data
    names = list(config.profiles.keys()) if (config is not None and hasattr(config, "profiles")) else []
    agent_choices = sorted(profile_data.agent_profiles(names))
    crumbs = (_crumbs(("Profiles", "/profiles"), ("Config", None)) if tab == "config"
              else _crumbs(("Profiles", None)))
    ctx = {"active_page": "profiles", "tab": tab, "params": params,
           "profile_budgets": budgets,
           "profile_cards": groups["declared"] + groups["lcp_only"],
           "profile_groups": groups,
           "agent_choices": agent_choices,
           "crumbs": crumbs}
    if tab == "config":
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
                       crumbs=_crumbs(("Usage", None)),
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
    return render_page("pages/alerts.html", config, engine, active_page="alerts",
                       crumbs=_crumbs(("Alerts", None)))


def render_models_page(config, engine=None, params=None) -> str:
    """Render the Models page — the capability matrix, with providers as a tab (M2).

    A provider exists here only as *where a model comes from*, so Providers is a
    tab of this page rather than a nav entry of its own.
    """
    from .render import render_page
    tab = _tab(params, ("matrix", "providers"), "matrix")
    return render_page("pages/models.html", config, engine,
                       active_page="models",
                       tab=tab,
                       crumbs=_crumbs(("Models", "/models"),
                                      ("Providers", None) if tab == "providers" else ("Capability matrix", None)),
                       params=dict(params or {}))


def render_setup_page(config, engine=None) -> str:
    """Render the first-run setup wizard page (Jinja2)."""
    from .render import render_page
    return render_page("pages/setup.html", config, engine, active_page="setup",
                       crumbs=_crumbs(("Setup", None)))


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
        # The fallback must satisfy the same shape the template reads, or the guard
        # converts a handled read error into a 500 — which it did, whenever the task
        # tree was present but empty. One shape, defined in one place.
        view = work_tasks.empty_tasks_view(
            "could not read the task tree", "%s: %s" % (type(e).__name__, e))
    view["tab"] = tab
    return render_page("pages/work_tasks.html", config, engine,
                       active_page="work_tasks", view=view,
                       crumbs=_crumbs(("Tasks", "/work/tasks"),
                                      ("Assessments", None) if tab == "assessments" else ("All tasks", None)))


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
                       active_page="work_fleet", view=view,
                       crumbs=_crumbs(("Fleet", None)))


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


def _profile_config_payload(config, name, pcfg) -> dict:
    """The profile's declared configuration, as the page init payload carries it.

    Normalised through the same contract the config loader and the write API use, so
    the payload cannot describe a profile differently from the way it is stored.
    """
    from ..api.config import validate_profile_fields
    try:
        fields = validate_profile_fields(name, pcfg or {})
    except Exception:
        fields = {"agent_profile": "", "routing": "static", "intents": [],
                  "description": ""}
    chain = (pcfg or {}).get("chain", []) or []
    steps = []
    for st in chain:
        if isinstance(st, dict):
            steps.append({"provider": st.get("provider", ""), "model": st.get("model", ""),
                          "base_url": st.get("base_url", "")})
    return {
        "name": name,
        "agent_profile": fields["agent_profile"],
        "routing": fields["routing"],
        "intents": fields["intents"],
        "description": fields["description"],
        "auth_required": bool((pcfg or {}).get("auth_required", True)),
        "forbidden_tools": list((pcfg or {}).get("forbidden_tools", []) or []),
        "chain": steps,
        "chain_steps": len(steps),
    }


def _routing_status_safe(config) -> dict:
    """The router's status, or {} when it cannot be asked.

    A control plane that 500s because the router is unhappy is worse than one that
    says it does not know — every caller here already has a "not available" state.
    """
    try:
        from ..api.router import routing_status
        return routing_status(config) or {}
    except Exception:  # noqa: BLE001
        return {}


def _effective_routing_on(status, name):
    """Is the router on for this profile? None when the router did not say.

    `None` is not `False`: a badge reading "static" because the router could not be
    reached would be a claim about behaviour, where the truth is that we did not ask.
    """
    entry = ((status or {}).get("per_profile") or {}).get(name)
    if isinstance(entry, dict) and "enabled" in entry:
        return bool(entry.get("enabled"))
    return None


def _profile_routing_view(config, name, status=None) -> dict:
    """The Routing tab's view: this profile's effective dynamic-routing settings.

    The router already computes them and already scopes them per profile
    (`routing_enabled:<profile>`, `routing_policy:<profile>`,
    `routing_min_score:<profile>`, `routing_rules:<profile>`), so this reads that
    rather than re-deriving anything. `has_override` is the honest part: it says
    whether what you are looking at is a decision made for this profile or the
    global default inherited by it.
    """
    out = {"available": False, "profile": name, "enabled": None, "policy": "",
           "min_score": None, "rules": [], "has_override": False,
           "tasks": [], "decisions": [], "reason": ""}
    if status is None:
        try:
            status = _routing_status_safe(config)
        except Exception as e:  # noqa: BLE001 — the helper already guards; belt and braces
            status = {}
        if not status:
            out["reason"] = "the router did not return a status"
    if status:
        pass
    else:
        return out
        return out
    per_profile = (status.get("per_profile") or {}).get(name)
    if per_profile is None:
        out["reason"] = "the router has no entry for this profile"
        return out

    # Every value is coerced to a JSON primitive here because the view is embedded in
    # a <script type="application/json"> block: one non-serialisable value (a router
    # that returned a Decimal, a settings object, anything) would break the page
    # rather than the value. The coercion is cheap; the failure it prevents is not.
    def _str(v):
        return "" if v is None else str(v)

    def _num(v):
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return None

    # `rules` is the list that is *in effect* for this profile — and which list that
    # is depends on the router's precedence: a per-profile list replaces the shared
    # one; otherwise the shared list applies. Both are shown, and `rules_scope` says
    # which it is, because writing back to the wrong scope would either shadow the
    # shared rules or overwrite rules belonging to other profiles.
    rules = []
    rules_other_scoped = 0
    for r in (per_profile.get("rules") or []):
        if not isinstance(r, dict):
            continue
        scope_of_rule = _str(r.get("profile"))
        if scope_of_rule and scope_of_rule not in ("*", name):
            rules_other_scoped += 1
        rules.append({k: (_str(v) if not isinstance(v, (int, float, bool)) else v)
                      for k, v in r.items()})

    out.update({
        "available": True,
        "enabled": bool(per_profile.get("enabled")),
        "policy": _str(per_profile.get("policy")),
        "min_score": _num(per_profile.get("min_score")),
        "rules": rules,
        "rules_scope": "profile" if per_profile.get("has_override") else "shared",
        "rules_other_scoped": rules_other_scoped,
        "has_override": bool(per_profile.get("has_override")),
        "tasks": [str(t) for t in sorted(
            set((status.get("per_task") or {}).keys())
            | {str(r.get("task")) for r in rules if r.get("task") and r.get("task") != "None"})],
    })
    decisions = []
    for d in (status.get("recent_decisions") or []):
        if not isinstance(d, dict) or d.get("profile") != name:
            continue
        decisions.append({k: (_str(v) if not isinstance(v, (int, float, bool)) else v)
                          for k, v in d.items()})
    out["decisions"] = decisions[:12]
    out["providers"] = [str(p) for p in sorted(status.get("providers") or [])]
    return out


def _profile_pool_view(config, name, routing_on=None) -> dict:
    """The Models tab's view: the chain, and everything it could be picked from.

    The pool *is* the chain — the router scores exactly these `(provider, model)`
    steps — so there is no second list to drift out of sync. What is new is that the
    chain is presented as a selection from the registry: every provider, every model
    it offers, and whether that provider's key is present (a model you cannot call is
    worth seeing, marked, rather than hidden).
    """
    profiles = config.profiles if (config is not None and hasattr(config, "profiles")) else {}
    pcfg = profiles.get(name) or {}
    catalogue = []
    providers = config.providers if (config is not None and hasattr(config, "providers")) else {}
    import os
    for pname in sorted(providers or {}):
        pdata = providers[pname] or {}
        env_var = pdata.get("api_key_env") or ""
        models = []
        for m in (pdata.get("models") or []):
            models.append(str(m))
        catalogue.append({
            "provider": pname,
            "api_base": pdata.get("api_base", ""),
            "models": models,
            "has_key": bool(os.environ.get(env_var)) if env_var else True,
            "key_env": env_var,
        })
    chain = []
    for st in (pcfg.get("chain") or []):
        if isinstance(st, dict):
            chain.append({"provider": st.get("provider", ""), "model": st.get("model", "")})
    return {"chain": chain, "catalogue": catalogue,
            "providers": [c["provider"] for c in catalogue],
            # The effective dynamic-routing switch, from the router — not the profile's
            # declared field. A page that showed the declaration here contradicted the
            # Routing tab next to it, which reads the router.
            "routing_on": routing_on,
            "model_count": sum(len(c["models"]) for c in catalogue)}


def render_profile_detail_page(config, engine=None, name=None, params=None) -> str:
    """Render one profile — level 3 of the profile surface (M2c).

    Five tabs, each a different artefact the profile owns: the skills it can load,
    the memory it carries, the tasks in its tree, the cron jobs it runs and the API
    keys that call it. All five are *read* — from the profile's own directory, or
    from the host snapshot the cron tab already depends on. Nothing here writes to
    a profile.

    The name arrives from the URL, so it is validated before use: an unknown name
    renders a "no such profile" page rather than a traceback, and a name carrying a
    path separator never reaches the filesystem at all.
    """
    from .render import render_page
    from ..api import profile_data

    params = dict(params or {})
    tab = _tab(params, ("skills", "memory", "tasks", "cron", "keys",
                        "routing", "models"), "skills")
    wanted = (name or "").strip()
    profiles = config.profiles if (config is not None and hasattr(config, "profiles")) else {}
    try:
        wanted = profile_data.validate_name(wanted)
    except profile_data.BadProfileName:
        wanted = ""

    # Two names can land here, and they mean different things: a gateway lane
    # ("l2"), whose artefacts live under whichever agent profile routes through it,
    # or an agent profile ("homelab-expert-l2"), which owns the artefacts itself.
    agents = profile_data.agent_profiles(list(profiles.keys())) if wanted else {}
    if wanted in profiles:
        lane, agent, kind = wanted, profile_data.primary_agent_for_lane(wanted, agents), "profile"
    elif wanted in agents:
        lane, agent, kind = agents[wanted].get("lane", ""), wanted, "agent"
    else:
        lane = agent = kind = ""

    # Ask the router once, here, and hand the answer to every surface on this page
    # that shows it: the Routing tab, the Models tab's sentence and the hero badge.
    # Each of them asking separately is how the page came to contradict itself.
    routing_status_data = _routing_status_safe(config)
    routing_on = _effective_routing_on(routing_status_data, wanted)

    # Which directory the skills, memory and task tree are read from: the agent
    # profile's when the name was a lane, the name itself when it was a profile.
    artefacts = agent or wanted

    budgets = _profile_budgets(engine)
    if not kind:
        card = None
    elif kind == "profile":
        # An LCP profile: what the grid shows for it, including whether the mapping
        # to an agent is declared or only derived from the Hermes side.
        pcfg = profiles.get(wanted) or {}
        from ..api.config import validate_profile_fields
        try:
            declared = validate_profile_fields(wanted, pcfg)["agent_profile"]
        except Exception:
            declared = ""
        shown = declared or agent
        card = _profile_card(config, wanted, pcfg, budgets, agent=shown,
                             kind="profile" if shown else "lcp_only",
                             mapping_source="declared" if declared else ("suggested" if shown else "none"),
                             routed_by=profile_data.agents_for_lane(wanted, agents),
                             routing_on=routing_on)
    else:
        # An agent profile directory that is not itself a gateway profile. It owns a
        # skills tree, memory and tasks — but no chain, so the Routing and Models tabs
        # describe that rather than offering controls that would write a chain onto a
        # name that is not a profile.
        card = _profile_card(config, wanted, {}, budgets, agent=wanted, kind="agent",
                             lane=lane)
    exists = card is not None
    if card is None:
        profile = {"name": wanted, "exists": False, "configured": False, "initials": "?",
                   "avatar": False, "avatar_name": wanted, "words": 0, "description": "",
                   "short_description": "", "kind": "", "kind_label": "", "lane": "", "agent": "",
                   "auth_label": "", "budget_label": "", "chain_label": "",
                   "routing_label": "", "intents": [], "chain_steps": 0, "mapping_source": ""}
    else:
        profile = dict(card)
        profile["exists"] = True
        # `configured` means "this is a gateway profile" — not which kind of gateway
        # profile it is. An agent-only directory is the one that is not.
        profile["configured"] = kind in ("profile", "lcp_only")

    labels = (("skills", "Skills"), ("memory", "Memory"), ("tasks", "Tasks"),
              ("cron", "Cron"), ("keys", "API Keys"),
              # M2d: how this profile routes (the toggle, policy, min-score and the
              # rules that act on its chain) and what it may choose from (the chain,
              # picked from every registered provider and model).
              ("routing", "Routing"), ("models", "Models"))
    tabs = [{"id": tid, "label": lbl, "count": None, "active": tid == tab,
             "href": "/profiles/%s?tab=%s" % (wanted, tid)} for tid, lbl in labels]
    tab_label = dict(labels).get(tab, "Profile")

    ctx = {"active_page": "profiles", "tab": tab if exists else "missing",
           "params": params, "profile": profile, "tabs": tabs, "tab_label": tab_label,
           "key_scope": lane or wanted,
           # M2d: the profile's own configuration travels with the page. Every tab
           # gets it in the initial payload, so nothing has to re-fetch what the
           # server already knows at render time — and a tab that edits it (Routing,
           # Models) starts from the declared values rather than from a guess.
           # Only a gateway profile has this: an agent profile directory is not a
           # routing entry, so its payload is empty by design.
           "profile_config": _profile_config_payload(config, wanted, profiles.get(wanted))
           if wanted in profiles else {},
           "crumbs": (_crumbs(("Profiles", "/profiles"),
                              (wanted, "/profiles/" + wanted), (tab_label, None))
                      if exists else _crumbs(("Profiles", "/profiles"), ("Not found", None)))}
    if not exists:
        return render_page("pages/profile_detail.html", config, engine, **ctx)

    if tab == "skills":
        try:
            ctx["skills"] = profile_data.skills_view(artefacts)
        except Exception as e:  # a viewer must not 500 on a filesystem surprise
            ctx["skills"] = {"available": False, "count": 0, "categories": [], "skills": [],
                             "truncated": False, "reason": "%s: %s" % (type(e).__name__, e)}
    elif tab == "memory":
        try:
            ctx["memory"] = profile_data.memory_view(artefacts)
        except Exception as e:
            ctx["memory"] = {"available": False, "files": [], "total_chars": 0,
                             "reason": "%s: %s" % (type(e).__name__, e)}
    elif tab == "tasks":
        ctx["view"] = _profile_tasks_view(artefacts, params)
    elif tab == "cron":
        ctx["view"] = _work_cron_view({"profile": artefacts or wanted})
    elif tab == "routing":
        ctx["routing"] = (_profile_routing_view(config, wanted, status=routing_status_data)
                          if wanted in profiles
                          else {"available": False, "profile": wanted, "rules": [],
                                "reason": "this is an agent profile, not a gateway "
                                          "profile, so it has no routing settings of "
                                          "its own"})
    elif tab == "models":
        ctx["pool"] = (_profile_pool_view(config, wanted, routing_on=routing_on)
                       if wanted in profiles
                       else {"chain": [], "catalogue": [], "providers": [],
                             "model_count": 0})
    # The API Keys tab fetches /api/keys itself and filters rows against
    # `key_scope` — the section is the same interactive one the global keys page
    # used, so create/show/revoke keep working per profile.
    return render_page("pages/profile_detail.html", config, engine, **ctx)


def _profile_tasks_view(name, params) -> dict:
    """One profile's task tree, rendered through the same view the Tasks page uses.

    The tree is whatever ``work-sources.json`` resolves for that profile, so the
    per-profile view cannot drift from the tree the host actually writes. A
    ContextVar rather than a parameter because the task helpers call ``tasks_dir()``
    from a dozen places, and because the server is threaded — one profile's root
    must not leak into another request.
    """
    from ..api import work_tasks, work_sources
    params = dict(params or {})
    root = work_sources.tasks_root_for(name)
    try:
        if root:
            with work_tasks.use_root(root):
                view = work_tasks.tasks_view(params)
        else:
            view = work_tasks.tasks_view(params)
    except Exception as e:  # never blank the tab on a data error
        view = {"available": False,
                "empty": {"reason": "could not read this profile's task tree",
                          "hint": "%s: %s" % (type(e).__name__, e)},
                "counts": {}, "total": 0, "tasks": [], "todos": None, "conflicts": [],
                "filter": {"states": [], "tag": "", "per": 20, "q": ""}}
    view["tab"] = "tasks"
    view["profile"] = name
    view["tasks_root"] = root or ""
    return view

