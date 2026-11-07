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
      gateway lanes that have an agent, whose name the card carries
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
    """The logs tab's view dict: which realm.

    Every realm is a mount of the shared table module (static/js/logtable.js),
    which fetches its own rows.
    """
    params = params or {}
    # Default is 'requests': the conversations realm is retired (it became the
    # /conversations page), and the board-decisions realm was cut with the work
    # surface — leaving the default pointed at a realm that no longer renders
    # would have opened the Logs tab on an error.
    tab = str(params.get("view") or "requests")
    view = {"tab": tab}
    if tab == "conversations":
        # Retired: the Conversations page now owns both the live set and the
        # history, so this realm has no data of its own to mount.
        view["tab_error"] = ("Conversations moved to the Conversations page.")
    elif tab not in ("requests", "providers"):
        view["tab_error"] = "unknown view %r" % tab
    return view


def render_home_page(config, engine=None, headers=None, profile_filter=None,
                     period="14") -> str:
    """Render the Overview homepage (CUR-330).

    The page the LCP brand link opens: the old Activity > Overview tab, promoted
    to ``/``. Every card, the trend and the detail panel are computed over the
    ``?period=`` window (7|14|30|90|all, default 14) by ``dashboard_context``.
    ``?profile=`` still scopes the whole page — the old dropdown is gone, but the
    URL keeps working.
    """
    from .dashboard import dashboard_context
    from .render import render_page
    ctx = dashboard_context(config, engine, headers or {},
                            profile_filter=profile_filter, period=period)
    ctx["crumbs"] = _crumbs(("Overview", None))
    return render_page("pages/home.html", config, engine, **ctx)


def render_activity_page(config, engine=None, params=None, headers=None) -> str:
    """Render the Activity page — logs | evidence (CUR-330).

    The overview left this page in CUR-330: it is the homepage now (``/``, see
    ``render_home_page``). ``?tab=overview`` and the bare page land there via a
    redirect in the endpoint, and this renderer only ever produces the two log
    realms. Alerts are deliberately not here (R10).
    """
    from .render import render_page
    params = dict(params or {})
    tab = _tab(params, ("logs", "evidence"), "logs")
    ctx = {"active_page": "activity", "tab": tab, "params": params,
           "crumbs": _crumbs(("Activity", "/activity"),
                             ({"evidence": "Evidence"}.get(tab, "Logs"), None))}
    if tab == "logs":
        ctx["view"] = _logs_view(params)
    return render_page("pages/activity.html", config, engine, **ctx)


def render_providers_page(config, engine=None, params=None) -> str:
    """Legacy /providers — now the Providers tab of Models (M2)."""
    return render_models_page(config, engine, {"tab": "providers"})


def render_profiles_page(config, engine=None, params=None) -> str:
    """Render the Profiles page — one card per profile (M2c, level 2).

    This page is the directory. In M2b it also carried API Keys, Cron and Config
    as tabs; in M2c every artefact that belongs to a *profile* moved under the
    profile itself (``/profiles/<name>``), because that is where a reader looks
    for it and because keys are per-profile by design (R9).

    Config left on 2026-10-25. It edited the task-tree root, the cron store and the
    op spool — Hermes agent infrastructure, not routing — so it went with the
    skills/memory/cron tabs. The page now has no tabs at all: a strip with one
    entry is not a strip. ``?tab=config`` and every other retired value fall back
    to the directory.
    """
    from .render import render_page
    params = dict(params or {})
    tab = _tab(params, ("profiles",), "profiles")
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
    crumbs = _crumbs(("Profiles", None))
    ctx = {"active_page": "profiles", "tab": tab, "params": params,
           "profile_budgets": budgets,
           "profile_cards": groups["declared"] + groups["lcp_only"],
           "profile_groups": groups,
           "agent_choices": agent_choices,
           "crumbs": crumbs}
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
                          "base_url": st.get("base_url", ""),
                          # CUR-328: the in-rotation flag must survive the view
                          # round-trip, or the toggle reads disabled links as on.
                          "enabled": st.get("enabled", True)})
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


def _profile_margin_gate(config, name) -> float:
    """This profile's L2 margin gate, read the way the router reads it.

    The gate is a *profile-config* field, unlike everything else the Routing tab
    shows — the router reads it from the profile's config (`_intent_margin_gate`),
    not from its own settings store, so the tab reads it from the same place and the
    Edit modal writes to the same place. 0.0 is both the off switch and the
    absent-field default.

    It goes through `validate_profile_fields` rather than reading the raw dict
    directly: a page that displayed a value the loader would reject (and therefore
    silently fall back to the seed over) would be lying about what is in effect.
    Tolerant of a duck-typed config, like the router's own reader.
    """
    try:
        from ..api.config import validate_profile_fields
        pcfg = (getattr(config, "profiles", None) or {}).get(name) or {}
        return float(validate_profile_fields(name, pcfg).get("intent_margin_gate") or 0.0)
    except Exception:  # noqa: BLE001 — a bad gate must not break the tab
        return 0.0


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
    # M3: set before the early returns below, so a profile with a gate but no router
    # entry still shows the gate it actually has rather than a default.
    out["intent_margin_gate"] = _profile_margin_gate(config, name)
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
            chain.append({"provider": st.get("provider", ""), "model": st.get("model", ""),
                          # CUR-328: keep the in-rotation flag in the view, or
                          # the toggle renders disabled links as enabled.
                          "enabled": st.get("enabled", True)})
    return {"chain": chain, "catalogue": catalogue,
            "providers": [c["provider"] for c in catalogue],
            # The effective dynamic-routing switch, from the router — not the profile's
            # declared field. A page that showed the declaration here contradicted the
            # Routing tab next to it, which reads the router.
            "routing_on": routing_on,
            "model_count": sum(len(c["models"]) for c in catalogue)}


def render_profile_detail_page(config, engine=None, name=None, params=None) -> str:
    """Render one profile — level 3 of the profile surface (M2c).

    Three tabs, each a *routing* artefact the profile owns: how it routes (the
    toggle, policy, min-score and the rules that act on its chain), the models it
    may choose from, and the API keys that call it.

    Skills, memory and cron are deliberately absent. They describe a Hermes agent,
    not a route: an agent's skills tree, its MEMORY.md and its cron jobs are
    managed in the Hermes dashboard, which owns them. A read-only copy here made
    this page look like an agent console and was the only part of the UI that read
    another process's filesystem.

    The name arrives from the URL, so it is validated before use: an unknown name
    renders a "no such profile" page rather than a traceback, and a name carrying a
    path separator never reaches the filesystem at all.
    """
    from .render import render_page
    from ..api import profile_data

    params = dict(params or {})
    tab = _tab(params, ("routing", "models", "keys"), "routing")
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
        # An agent profile directory that is not itself a gateway profile. It has no
        # chain, so the Routing and Models tabs describe that rather than offering
        # controls that would write a chain onto a name that is not a profile.
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

    # Routing first: the router is what this app is, and keys/models are its
    # inputs, so they follow the decision they feed.
    labels = (("routing", "Dynamic routing"), ("models", "Static routing"), ("keys", "API Keys"))
    # Counts come from what the page already carries: the intents that route this
    # profile and the steps in its chain. A pill saying how much is configured
    # turns the strip into an overview rather than three words. Keys are fetched
    # by the tab itself, so that one carries no count.
    _cfg = (_profile_config_payload(config, wanted, profiles.get(wanted))
            if wanted in profiles else {})
    _counts = {"routing": len(_cfg.get("intents") or []) or None,
               "models": _cfg.get("chain_steps") or None,
               "keys": None}
    tabs = [{"id": tid, "label": lbl, "count": _counts[tid], "active": tid == tab,
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
           "profile_config": _cfg,
           "crumbs": (_crumbs(("Profiles", "/profiles"),
                              (wanted, "/profiles/" + wanted), (tab_label, None))
                      if exists else _crumbs(("Profiles", "/profiles"), ("Not found", None)))}
    if not exists:
        return render_page("pages/profile_detail.html", config, engine, **ctx)

    if tab == "routing":
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


def _inflight_conversation_names(ids) -> dict:
    """``conversation_id -> name`` — the conversation's own task label.

    Best-effort: an unreadable conversations table must not blank the page, so
    a failure yields no names rather than an error.
    """
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    try:
        from ..api.work_conversations import _connect
        con = _connect()
        try:
            marks = ",".join("?" * len(ids))
            rows = con.execute(
                "SELECT conversation_id, name FROM conversations "
                "WHERE conversation_id IN (%s)" % marks, ids).fetchall()
            return {r["conversation_id"]: r["name"] for r in rows}
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — a label is not worth failing the view for
        return {}


def in_progress_payload(engine=None) -> dict:
    """What LCP is working on right now, as JSON-serialisable data.

    ``task`` is the joined conversation name — the closest thing the estate has
    to a human-readable answer for *what* a call is for. It is ``None`` when the
    client sent no ``x-opencode-session`` header, and the page says "untagged"
    rather than inventing a label.
    """
    from ..api import inflight as in_flight
    try:
        in_flight.sweep()
    except Exception:  # noqa: BLE001
        pass
    items = in_flight.snapshot()
    names = _inflight_conversation_names([i.get("conversation_id") for i in items])
    for it in items:
        it["task"] = names.get(it.get("conversation_id") or "")
    return {
        "count": len(items),
        "profiles": sorted({i["profile"] for i in items}),
        "providers": sorted({i["provider"] for i in items}),
        "items": items,
    }


def render_in_progress_page(config, engine=None, params=None) -> str:
    """The In-progress page — started-but-unfinished requests, live.

    Superseded by /conversations, which keeps this live table at the top and adds
    the history below it. The route stays and redirects, so a bookmark still lands.
    """
    import json as _json
    from .render import render_page
    payload = in_progress_payload(engine)
    return render_page("pages/in_progress.html", config, engine,
                       active_page="in_progress",
                       tab="in_progress",
                       params=params or {},
                       payload=payload,
                       payload_json=_json.dumps(payload))


# ── Conversations — what LCP is doing now, and what it has done ───────────────
# Two stores, one page. The live half is the in-process registry (a running call
# has no `requests` row yet); the history half is LCP's own conversation store,
# which it owns outright — migrated from the Hermes session DB and appended to on
# every served request.

def conversations_payload(config=None, engine=None, params=None) -> dict:
    """The Conversations page's data: the live set plus a page of history."""
    from ..api import conv_store

    live = in_progress_payload(engine)
    try:
        history = conv_store.list_conversations(params or {})
    except Exception as exc:  # noqa: BLE001 — the live half must still render
        import logging
        logging.getLogger("lcp.pages").warning("conversations_history_failed",
                                               exc_info=True)
        history = {"available": False, "error": str(exc), "conversations": [],
                   "total": 0,
                   "filter": {"per": "20", "page": 1, "pages": 1, "total": 0,
                              "profile": "", "q": "", "profiles": []}}
    return {"live": live, "history": history}


def render_conversations_page(config, engine=None, params=None) -> str:
    """Server-rendered Conversations page: live at the top, history below."""
    import json as _json
    from .render import render_page
    payload = conversations_payload(config, engine, params)
    return render_page("pages/conversations.html", config, engine,
                       active_page="conversations",
                       tab="conversations",
                       params=params or {},
                       payload=payload,
                       payload_json=_json.dumps(payload))


# ── Settings — the gateway switches that are not per-provider ────────────────
# The Providers page owns the per-provider panels (cache TTLs, routing tab);
# what lands here is estate-level behaviour with no provider home. First tenant:
# CUR-329 conversation titling.

def settings_payload(config=None) -> dict:
    """The Settings page's data: the titling switch and its model options.

    ``models`` is the set of models the gateway's own profiles actually chain
    to — the only models a titler can meaningly pick, because the titler sends
    through a chain step (it needs a provider + base_url, not just a name). The
    empty option is "the default profile's chain head", which is what an unset
    ``model`` means at runtime, so the picker never shows a value the titler
    cannot resolve.
    """
    import json as _json
    section = {"enabled": True, "model": None}
    try:
        raw = getattr(config, "conversation_titling", None)
        if isinstance(raw, dict):
            section = dict(raw)
    except Exception:  # noqa: BLE001 — the page must render, not 500
        pass
    models: list[str] = []
    try:
        profiles = getattr(config, "profiles", None) or {}
        for prof in profiles.values():
            for step in (prof.get("chain") or []):
                m = step.get("model")
                if m and m not in models:
                    models.append(m)
        models.sort()
    except Exception:  # noqa: BLE001
        models = []
    return {
        "titling": {
            "enabled": bool(section.get("enabled", True)),
            "model": section.get("model") or "",
        },
        "models": models,
        "models_json": _json.dumps(models),
    }


def render_settings_page(config, engine=None, params=None) -> str:
    """Server-rendered Settings page (CUR-329): titling toggle + model picker."""
    import json as _json
    from .render import render_page
    payload = settings_payload(config)
    return render_page("pages/settings.html", config, engine,
                       active_page="settings",
                       tab="settings",
                       params=params or {},
                       payload=payload,
                       payload_json=_json.dumps(payload))
