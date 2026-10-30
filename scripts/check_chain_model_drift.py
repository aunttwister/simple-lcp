#!/usr/bin/env python3
"""CUR-272 — does every model a chain references still get served by that provider?

Read-only. Compares the CONFIG against the truth the registry already holds, and
against the provider's own live catalogue where one can be fetched.

Why this exists
---------------
A chain step can reference a model its provider no longer serves and *the request still
succeeds*: the local engine answers for any name, and the only symptom is that the answer
came from a different model than the chain asked for.

What was missing was not a check — the 30-minute monitor (`lcp-alert-watch.py`) already
flags chain steps whose model is not in the provider's declared list, and it caught this
one. What was missing was **ranking**: measured on this estate on 2026-10-30 it reported
11 identical `chain_model_undeclared` warnings, of which exactly one is a model the
provider does not serve. The other ten are steps the provider serves perfectly well
(active deployment, thousands of requests) where the provider block merely omits a legacy
spelling. One actionable item among ten cosmetic ones is a warning nobody reads. This
script supplies the missing evidence — the registry's per-endpoint deployment status, and
the provider's own live catalogue — so the one that matters can be ranked above the rest.

The dead end it was built from, measured on this estate on 2026-10-30:

    coder -> local-sparks -> qwen3.8-flash-next

`local-sparks` declares and advertises exactly one model, ``DeepSeek-V4.1-Flash-1017``,
and the registry already records ``qwen3.8-flash-next`` on that endpoint as **retired**
("Recorded historically; not advertised by the live engine"). The engine answers for
*any* requested name with whatever it has loaded, so the step never errors — it
quietly serves DeepSeek V4.1 Flash to a chain that asked for Qwen 3.8, and everything
downstream (cost attribution, capability expectations, routing decisions) inherits the
wrong assumption.

Evidence, in order of authority
-------------------------------
1. ``mi_*`` registry — per-endpoint deployment status ``active`` / ``retired``.
   This is the decisive one, and it is time-aware.
2. the provider block's DECLARED ``models`` list (``gateway_config:providers``).
   A declaration is what the operator wrote down; it can be stale in BOTH
   directions, so it never decides an error on its own.
3. the provider's live ``/v1/models`` (``--live``). Ground truth, but only where a
   catalogue can actually be fetched — and it is useless for a gateway that answers
   the same way whatever name you ask for.

Severity
--------
error   the chain points at a model that endpoint does NOT serve (retired, or
        unresolvable and undeclared). This is the drift CUR-272 is about.
warn    the provider does serve it (active deployment, real traffic) but the
        provider block's declared list omits it — a config/documentation drift.
info    an active deployment no chain references (reported, never an error).

Exit codes: 0 clean, 1 drift found, 2 could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("COST_DB", "/app/data/costs.db")
USER_AGENT = "lcp-chain-drift/1.0 (+curl-compatible)"
CATALOGUE_PATHS = ("/v1/models", "/models")
TRAFFIC_DAYS = 30

OK, WARN, ERROR, INFO = "ok", "warn", "error", "info"


def load_identity():
    """The one resolver. Never a second implementation of identity."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from src.api import model_identity as mi
    return mi


def _json_setting(con: sqlite3.Connection, key: str, default):
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def chain_steps(profiles: dict) -> list[dict]:
    """Every (profile, provider, model) the config asks for, in a stable order."""
    out: list[dict] = []
    for profile in sorted(profiles):
        cfg = profiles[profile] or {}
        for i, step in enumerate(cfg.get("chain") or []):
            if not isinstance(step, dict):
                continue
            model = (step.get("model") or "").strip()
            if not model:
                continue
            out.append({
                "profile": profile,
                "index": i,
                "provider": (step.get("provider") or "").strip(),
                "model": model,
                "role": step.get("role"),
            })
    return out


def declared_models(providers: dict, provider: str):
    """The model ids a provider block declares, or None when it declares nothing."""
    block = providers.get(provider)
    if not isinstance(block, dict):
        return None
    models = block.get("models")
    if models is None:
        return None
    if isinstance(models, str):
        models = [models]
    elif isinstance(models, dict):
        # A bare dict is one entry, not a sequence of its keys — iterating it directly
        # yielded ["id"] for {"models": {"id": "c"}}.
        models = [models]
    out: list[str] = []
    for m in models:
        if isinstance(m, str) and m.strip():
            out.append(m.strip())
        elif isinstance(m, dict):
            for k in ("id", "model", "name", "served_id"):
                if m.get(k):
                    out.append(str(m[k]).strip())
                    break
    return out


def _tokens_for(slug: str):
    """Credentials for a catalogue fetch: env first, then the app's credential store.

    The store is only reachable inside the running app, which is why `--live` is
    most useful under `docker exec lcp ...`. A public catalogue needs no token, so
    ``None`` is always tried last.
    """
    out = []
    for key in (f"LCP_{slug.upper().replace('-', '_')}_KEY", f"{slug.upper()}_API_KEY"):
        v = os.environ.get(key)
        if v:
            out.append(v)
    try:
        from src.api.credential_store import get_credential_store
        store = get_credential_store()
        if store is not None and store.get(slug):
            out.append(store.get(slug))
    except Exception:  # noqa: BLE001 — not inside the app; fine
        pass
    out.append(None)
    return out


def fetch_catalogue(base_url: str, slug: str):
    """The provider's advertised model ids, or (None, why-not)."""
    base = (base_url or "").rstrip("/")
    if not base:
        return None, "no base_url recorded"
    errors = []
    for path in CATALOGUE_PATHS:
        url = base.rsplit("/v1", 1)[0] + path if "/v1" in base else base + path
        for token in _tokens_for(slug):
            req = urllib.request.Request(url)
            req.add_header("User-Agent", USER_AGENT)
            req.add_header("Accept", "application/json")
            if token:
                req.add_header("Authorization", "Bearer " + token)
            try:
                with urllib.request.urlopen(req, timeout=12) as f:
                    body = json.loads(f.read().decode())
                ids = [str(m.get("id")) for m in (body.get("data") or []) if m.get("id")]
                if ids:
                    return ids, None
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{'+auth' if token else 'no-auth'}: {str(ex)[:50]}")
    return None, "; ".join(errors[:3]) or "no model ids"


def _recent_traffic(con: sqlite3.Connection, provider: str, model: str) -> int:
    row = con.execute(
        "SELECT COUNT(*) n FROM requests WHERE provider = ? AND model = ?"
        "  AND timestamp >= datetime('now', ?)", (provider, model, f"-{TRAFFIC_DAYS} days")
    ).fetchone()
    return int(row["n"] if row else 0)


def _deployment_status(con: sqlite3.Connection, deployment_id):
    row = con.execute("SELECT status FROM mi_deployments WHERE deployment_id = ?",
                      (deployment_id,)).fetchone()
    return row["status"] if row else None


def assess(con, mi, step: dict, providers: dict, live: dict) -> dict:
    """One verdict per chain step, with the evidence that produced it."""
    provider, model = step["provider"], step["model"]
    res = mi.resolve(provider or None, model, con=con)

    slug = None
    if res.model_id:
        row = con.execute("SELECT slug FROM mi_models WHERE model_id = ?",
                          (res.model_id,)).fetchone()
        slug = row["slug"] if row else None

    dep_status = _deployment_status(con, res.deployment_id)
    # An in-force resolution is only "served" if the deployment is still live.
    active_dep = dep_status == "active"
    declared = declared_models(providers, provider)
    catalogue = live.get(provider, (None, "not checked"))
    advertised, why_not = catalogue

    traffic = _recent_traffic(con, provider, model) if provider else 0

    verdict = OK
    detail = []
    dep_desc = "no deployment" if not dep_status else dep_status
    if res.model_id is None:
        verdict = ERROR
        detail.append(f"the registry cannot resolve it ({res.unresolved_reason})")
    elif not active_dep:
        verdict = ERROR
        detail.append(f"the only deployment is not serving ({dep_desc})")
    detail.append(f"registry={slug or 'unresolved'}, serving={active_dep}"
                  if res.model_id else f"registry=unresolved ({res.unresolved_reason})")

    if declared is not None:
        in_declared = any(m == model for m in declared)
        detail.append(f"declared={'yes' if in_declared else 'NO'} "
                      f"({len(declared)} model(s) listed)")
        if not in_declared and verdict == OK:
            # The provider still serves it (active deployment and/or real traffic), so
            # the config is lagging the provider — worth saying once, not an error.
            verdict = INFO
            detail.append("provider block does not list it")
    else:
        detail.append("declared=n/a")

    if advertised is not None:
        in_cat = any(m == model for m in advertised)
        detail.append(f"advertised={'yes' if in_cat else 'NO'} "
                      f"({len(advertised)} id(s) live)")
        if not in_cat and verdict in (OK, INFO):
            # A live catalogue is the strongest statement that this endpoint does not
            # serve the name; a retired registry row agrees with it.
            verdict = ERROR if not active_dep else INFO
    else:
        detail.append(f"live=n/a ({why_not})")

    return {**step, "verdict": verdict, "slug": slug,
            "deployment_id": res.deployment_id,
            "deployment_status": dep_status,
            "matched_by": res.matched_by,
            "unresolved_reason": res.unresolved_reason,
            "traffic_30d": traffic, "evidence": detail}


def unreferenced(con, mi, steps: list[dict]) -> list[dict]:
    """Active deployments no chain references — informational, never an error."""
    referenced = {(s["provider"], s["model"]) for s in steps}
    out = []
    for d in con.execute(
        "SELECT d.deployment_id, e.slug endpoint, d.served_id FROM mi_deployments d"
        " JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
        " WHERE d.status = 'active' ORDER BY e.slug, d.served_id"
    ):
        if (d["endpoint"], d["served_id"]) in referenced:
            continue
        if any(p == d["endpoint"] for p, _ in referenced):
            out.append({"endpoint": d["endpoint"], "served_id": d["served_id"]})
    return out


_RANK = {INFO: 0, OK: 1, WARN: 2, ERROR: 3}


def group_findings(findings: list[dict]) -> list[dict]:
    """One row per (provider, model); the worst verdict wins.

    The same drift shows up in every profile that uses the step — `deepseek ->
    deepseek-v4-flash` appears in five — and repeating it five times is how a report
    stops being read. The profiles are listed on the row instead.
    """
    groups: dict = {}
    for f in findings:
        key = (f["provider"], f["model"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"provider": f["provider"], "model": f["model"],
                               "verdict": f["verdict"], "evidence": f["evidence"],
                               "slug": f["slug"],
                               "deployment_status": f["deployment_status"],
                               "traffic_30d": f["traffic_30d"], "profiles": []}
        g["profiles"].append(f["profile"])
        g["traffic_30d"] = max(g["traffic_30d"], f["traffic_30d"])
        if _RANK[f["verdict"]] > _RANK[g["verdict"]]:
            g["verdict"] = f["verdict"]
            g["evidence"] = f["evidence"]
            g["slug"] = f["slug"]
            g["deployment_status"] = f["deployment_status"]
    return sorted(groups.values(),
                  key=lambda g: (-_RANK[g["verdict"]], g["provider"] or "", g["model"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--live", action="store_true",
                    help="also fetch each provider's /v1/models (best inside the container)")
    ap.add_argument("--quiet", action="store_true", help="only errors and the summary")
    args = ap.parse_args()

    try:
        mi = load_identity()
        con = mi.connect(args.db, ro=True)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2

    try:
        profiles = _json_setting(con, "gateway_config:profiles", {})
        providers = _json_setting(con, "gateway_config:providers", {})
        steps = chain_steps(profiles)

        live: dict = {}
        if args.live:
            for slug, block in sorted(providers.items()):
                base = (block.get("api_base") or "") if isinstance(block, dict) else ""
                live[slug] = fetch_catalogue(base, slug)

        findings = [assess(con, mi, s, providers, live) for s in steps]
        extra = unreferenced(con, mi, steps)
    finally:
        con.close()

    groups = group_findings(findings)
    errors = [g for g in groups if g["verdict"] == ERROR]
    warns = [g for g in groups if g["verdict"] == WARN]
    notes = [g for g in groups if g["verdict"] == INFO]

    if args.json:
        print(json.dumps({"groups": groups, "steps": findings, "unreferenced": extra,
                          "errors": len(errors), "warnings": len(warns),
                          "notes": len(notes)}, indent=2, sort_keys=True))
        return 1 if errors else 0

    print(f"chain model drift — {len(steps)} step(s) over {len(groups)} distinct model(s),"
          f" {len(profiles)} profile(s)"
          f"{', live catalogues checked' if args.live else ''}")
    print()
    for g in groups:
        if args.quiet and g["verdict"] != ERROR:
            continue
        mark = {ERROR: "DRIFT", WARN: "warn ", OK: "ok   ", INFO: "note "}[g["verdict"]]
        prov = g["provider"] or "(any)"
        print(f"  {mark}  {prov} -> {g['model']}")
        print(f"          {'; '.join(g['evidence'])}")
        print(f"          used by: {', '.join(sorted(set(g['profiles'])))}")
        if g["traffic_30d"]:
            print(f"          {g['traffic_30d']} request(s) in {TRAFFIC_DAYS}d")
    if extra and not args.quiet:
        print()
        print("  active deployments no chain references (informational):")
        for e in extra:
            print(f"    {e['endpoint']} -> {e['served_id']}")
    print()
    if errors:
        print(f"  {len(errors)} DRIFT, {len(warns)} warning(s), {len(notes)} note(s) — "
              f"a chain references a model its provider does not serve")
        return 1
    print(f"  clean: every chain model is served by its provider"
          f"{f' ({len(notes)} config note(s))' if notes else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
