"""Model-intel poller: fetch fresh provider model lists, stage them for identity.

The poll layer of the model-intel redesign (CUR: "poll fresh data for fresh models and
integrate them carefully with our canonical labels").

Three independent opt-ins, all OFF by default
---------------------------------------------
Freshness data is never fetched until the operator turns it on, and each layer is
separate so one source can be rejected without disabling the rest:

1. ``model_intel.poller_enabled`` — the master switch. Nothing polls anywhere until this
   is true. Flipping it back off is the kill switch.
2. ``model_intel.sources.<name>.accepted`` — per SOURCE acceptance. ``provider_catalog``
   covers discovery from a provider's own ``/models``; ``lmarena`` covers the capability
   scores imported from the public LMArena leaderboard. A source the operator does not
   trust is simply never read.
3. ``mi_endpoints.poll_enabled`` — per ENDPOINT (migration 031). One flaky or expensive
   endpoint can stay out while the rest poll.

Contract
--------
``poll_all`` is the only entry point the scheduler and the admin endpoint call. For each
``active``, opted-in endpoint it fetches the live catalog, appends one ``mi_polls`` row
(the audit trail), and diffs against ``mi_deployments``:

* known served id              → ``last_seen`` bumped, status untouched
* unknown served id            → new deployment row ``status='new', source='poll'`` —
                                 staged for identity review, never read by resolve()
* known id absent from poll    → ``status='stale_candidate'`` (kept, never auto-retired)
* user-verified deployment     → never auto-staled (``verified_at`` set means a human
                                 confirmed it; a poll cannot overrule that)

Every endpoint strategy returns ``(ok, http_status, [ids], error)``. Failures are recorded,
never raised — a poll is an observation, and the caller decides what to report.

Failure and edge handling
-------------------------
* **Transport failure** (timeout, DNS, TLS, 401/403/500): the poll row records ``ok=0``
  plus the error, the registry is untouched, and failures are counted so a permanently
  dead poller is visible instead of looking like "switched off".
* **Empty catalog**: a provider that answers 200 with an empty list is treated as
  suspicious, not as "every model vanished". Nothing is staged and nothing goes stale;
  only ``last_seen`` moves for ids that are still listed.
* **Partial catalog**: if the listing is shorter than ``guard.max_shrink_pct`` of the
  known active set, the same suspicious path applies. A truncated response must not
  look like a mass retirement.
* **Concurrency**: ``run_guarded`` holds a process lock, and a per-endpoint floor
  (``min_interval_s``, 5 s when ``force``) stops two overlapping runs interleaving.
* **Unavailable canonical identity**: a staged row carries ``model_id = NULL`` on purpose.
  ``model_identity.resolve()`` only reads ``status='active'``, so nothing a poll discovers
  can affect routing before a human links it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .model_identity import connect as mi_connect
from .logging_config import get_logger

logger = get_logger("lcp.model_intel_poll")

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)

#: Source names used in ``model_intel.sources``. ``provider_catalog`` is discovery from
#: a provider's own ``/models``; ``lmarena`` is the public capability leaderboard;
#: ``livebench`` is the public LiveBench leaderboard (static release artifacts).
SOURCE_PROVIDER_CATALOG = "provider_catalog"
SOURCE_LMARENA = "lmarena"
SOURCE_LIVEBENCH = "livebench"

DEFAULTS: dict = {
    "poller_enabled": False,          # master switch — nothing polls until this is true
    "sources": {
        SOURCE_PROVIDER_CATALOG: {"accepted": False},
        SOURCE_LMARENA: {"accepted": False},
        SOURCE_LIVEBENCH: {"accepted": False},
    },
    "guard": {"max_shrink_pct": 50},  # a listing this much shorter looks truncated
    "min_interval_s": 300,            # floor between two non-forced runs
}

_config_lock = threading.Lock()
_run_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deep_merge(base: dict, over: Optional[dict]) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(config=None) -> dict:
    """The effective model-intel switches, defaults merged underneath.

    Reads the gateway config section ``model_intel`` (the same store the Settings page
    writes). An absent section, an absent config object, or an unreadable store all
    yield the defaults — which are all-off, so a failure here can never start polling.
    """
    try:
        if config is None:
            from .config import get_config
            config = get_config()
        raw = getattr(config, "raw", None) or {}
        section = raw.get("model_intel") if isinstance(raw, dict) else None
        return _deep_merge(DEFAULTS, section if isinstance(section, dict) else None)
    except Exception:  # noqa: BLE001 — the defaults are the safe answer
        return json.loads(json.dumps(DEFAULTS))


def source_accepted(cfg: dict, source: str) -> bool:
    return bool(((cfg.get("sources") or {}).get(source) or {}).get("accepted"))


def save_config(config, updates: dict) -> dict:
    """Persist switch changes into the ``model_intel`` config section.

    Only the keys present in ``updates`` are merged; everything else is preserved, so
    the Settings page can flip one switch without resending the rest. Returns the
    effective config after the write.
    """
    with _config_lock:
        raw = getattr(config, "raw", None)
        if not isinstance(raw, dict):
            raise RuntimeError("config object has no writable .raw mapping")
        current = raw.get("model_intel")
        merged = _deep_merge(
            current if isinstance(current, dict) else {},
            {k: v for k, v in updates.items() if v is not None},
        )
        raw["model_intel"] = merged
        save = getattr(config, "save", None)
        if callable(save):
            save()
        return _deep_merge(DEFAULTS, merged)


# ------------------------------------------------------------------- fetchers


def _fetch_bearer(base_url: str, key: Optional[str], timeout: float = 12.0):
    """Standard OpenAI-style ``GET {base}/models`` with an optional bearer token."""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        ids = sorted({m.get("id") for m in (data.get("data") or [])
                      if isinstance(m, dict) and isinstance(m.get("id"), str)})
        return True, getattr(resp, "status", 200), ids, None
    except urllib.error.HTTPError as e:
        return False, int(e.code), [], f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 — recorded, not raised
        return False, None, [], f"{type(e).__name__}: {e}"[:200]


def _fetch_commandcode_catalog(timeout: float = 8.0):
    """Command Code exposes a PUBLIC catalog (bearer gets 403) — browser-UA GET.

    Mirrors ``cost_plugins.commandcode._load_catalog`` but keeps full ids, not the
    last-segment index, because identity wants the provider's real spelling.
    """
    base = "https://api.commandcode.ai/provider/v1"
    req = urllib.request.Request(
        base.rstrip("/") + "/models",
        headers={"Accept": "application/json", "User-Agent": _BROWSER_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        raw_models = data.get("data") if isinstance(data, dict) else data
        ids = set()
        for m in raw_models or []:
            mid = m.get("id") if isinstance(m, dict) else m
            if isinstance(mid, str) and mid:
                ids.add(mid)
        return True, 200, sorted(ids), None
    except urllib.error.HTTPError as e:
        return False, int(e.code), [], f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, None, [], f"{type(e).__name__}: {e}"[:200]


def _endpoint_key(con: sqlite3.Connection, slug: str) -> Optional[str]:
    """Bearer credential for an endpoint slug, via the app's credential store."""
    try:
        from .credential_store import get_credential_store

        store = get_credential_store()
        if store is None:
            from .models import get_engine
            from .credential_store import CredentialStore

            store = CredentialStore(get_engine("data/costs.db"), data_dir="data")
        return store.get(slug)
    except Exception:  # noqa: BLE001 — a missing credential is a poll with no auth
        return None


def _fetch_endpoint(con: sqlite3.Connection, slug: str, base_url: str):
    """Strategy dispatch: the one place that knows each endpoint's catalog shape."""
    if slug == "commandcode":
        return _fetch_commandcode_catalog()
    return _fetch_bearer(base_url, _endpoint_key(con, slug))


# ------------------------------------------------------------------- diff + record


def _last_poll_age(con: sqlite3.Connection, endpoint_id: int) -> Optional[float]:
    row = con.execute(
        "SELECT polled_at FROM mi_polls WHERE endpoint_id=? ORDER BY poll_id DESC LIMIT 1",
        (endpoint_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        at = datetime.fromisoformat(row["polled_at"])
    except Exception:  # noqa: BLE001
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - at).total_seconds()


def _diff_and_stage(con: sqlite3.Connection, endpoint_id: int, slug: str,
                    ok: bool, http_status, ids: list, error: Optional[str],
                    duration_ms: int, guard: Optional[dict] = None) -> dict:
    """Write the poll row, then diff ids against mi_deployments (one transaction)."""
    now = _now()
    guard = guard or {}
    max_shrink = float(guard.get("max_shrink_pct", 50)) / 100.0
    cur = con.cursor()
    cur.execute(
        "INSERT INTO mi_polls (endpoint_id, polled_at, ok, http_status, model_count,"
        " model_ids_json, error, duration_ms) VALUES (?,?,?,?,?,?,?,?)",
        (endpoint_id, now, 1 if ok else 0, http_status, len(ids) if ok else None,
         json.dumps(ids) if ok else None, error, duration_ms),
    )
    summary = {"endpoint": slug, "ok": ok, "model_count": len(ids) if ok else 0,
               "new": [], "stale": [], "suspect": False, "error": error}
    if not ok:
        con.commit()
        return summary

    known = {
        r["served_id"]: r
        for r in cur.execute(
            "SELECT deployment_id, served_id, status, verified_at FROM mi_deployments"
            " WHERE endpoint_id=?",
            (endpoint_id,),
        ).fetchall()
    }
    active_known = {k: v for k, v in known.items() if v["status"] == "active"}

    # Guard: an empty or sharply shrunken listing is a broken read, not a mass
    # retirement. Bump last_seen for what we did see, stage nothing, stale nothing.
    suspicious, reason = False, None
    if not ids:
        suspicious, reason = True, "empty catalog"
    elif active_known and len(ids) < len(active_known) * max_shrink:
        suspicious, reason = True, (
            f"listing {len(ids)} is under {int(max_shrink * 100)}% of"
            f" {len(active_known)} known active")

    if suspicious:
        for mid in ids:
            row = known.get(mid)
            if row is not None:
                cur.execute("UPDATE mi_deployments SET last_seen=? WHERE deployment_id=?",
                            (now, row["deployment_id"]))
        cur.execute("UPDATE mi_polls SET error=? WHERE poll_id=last_insert_rowid()",
                    (f"suspicious: {reason}",))
        summary.update(suspect=True, suspect_reason=reason)
        con.commit()
        return summary

    seen = set()
    for mid in ids:
        seen.add(mid)
        row = known.get(mid)
        if row is None:
            # Resolve the served id through the registry's own routes before staging:
            # a polled id that reaches a canonical model is staged LINKED (so the
            # capability rows for that model become joinable), and one that reaches
            # nothing is queued for promotion rather than stored with no identity.
            linked = None
            try:
                from .model_identity_queue import (
                    KIND_SERVED_ID,
                    SOURCE_PROVIDER_CATALOG,
                    observe,
                    resolve_canonical,
                )
                linked = resolve_canonical(con, mid)
                if not linked:
                    observe(con, mid, source=SOURCE_PROVIDER_CATALOG, kind=KIND_SERVED_ID,
                            evidence={"endpoints": [slug]})
            except Exception as exc:  # noqa: BLE001 — staging must not fail on a lookup
                logger.warning("model_intel_resolve_failed", served_id=mid,
                               error=str(exc)[:160])
            model_id = None
            if linked:
                r = con.execute("SELECT model_id FROM mi_models WHERE slug=?", (linked,)).fetchone()
                model_id = r["model_id"] if r else None
            cur.execute(
                "INSERT INTO mi_deployments (endpoint_id, model_id, served_id, status, source,"
                " first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
                (endpoint_id, model_id, mid, "new", "poll", now, now),
            )
            summary["new"].append(mid)
            if model_id:
                summary.setdefault("linked", []).append(mid)
        else:
            cur.execute(
                "UPDATE mi_deployments SET last_seen=? WHERE deployment_id=?",
                (now, row["deployment_id"]),
            )
            # a poll re-observing a stale candidate revives it — the endpoint re-listed it
            if row["status"] == "stale_candidate":
                cur.execute(
                    "UPDATE mi_deployments SET status='active' WHERE deployment_id=?",
                    (row["deployment_id"],),
                )
                summary.setdefault("revived", []).append(mid)
    for mid, row in known.items():
        if mid in seen or row["status"] != "active":
            continue
        if row["verified_at"]:
            # A human confirmed this deployment. A poll must not retire it.
            summary.setdefault("protected", []).append(mid)
            continue
        cur.execute(
            "UPDATE mi_deployments SET status='stale_candidate' WHERE deployment_id=?",
            (row["deployment_id"],),
        )
        summary["stale"].append(mid)
    con.commit()
    return summary


def endpoint_health(con: sqlite3.Connection) -> list:
    """Per-endpoint poll health, derived from the ``mi_polls`` audit trail.

    Answers the operational question a failure log alone cannot: *has this endpoint
    ever been polled, and do its polls still succeed?* A poller that has been failing
    for a week must not look the same as one that is switched off.
    """
    rows = con.execute("""
        SELECT e.endpoint_id, e.slug, e.status, e.poll_enabled,
               (SELECT p.polled_at FROM mi_polls p WHERE p.endpoint_id = e.endpoint_id
                 ORDER BY p.poll_id DESC LIMIT 1)             AS last_at,
               (SELECT p.ok FROM mi_polls p WHERE p.endpoint_id = e.endpoint_id
                 ORDER BY p.poll_id DESC LIMIT 1)             AS last_ok,
               (SELECT p.error FROM mi_polls p WHERE p.endpoint_id = e.endpoint_id
                 ORDER BY p.poll_id DESC LIMIT 1)             AS last_error,
               (SELECT p.model_count FROM mi_polls p WHERE p.endpoint_id = e.endpoint_id
                 ORDER BY p.poll_id DESC LIMIT 1)             AS last_count,
               (SELECT p.polled_at FROM mi_polls p WHERE p.endpoint_id = e.endpoint_id
                 AND p.ok = 1 ORDER BY p.poll_id DESC LIMIT 1) AS last_success_at,
               (SELECT COUNT(*) FROM mi_polls p WHERE p.endpoint_id = e.endpoint_id)    AS total_runs,
               (SELECT COUNT(*) FROM mi_polls p WHERE p.endpoint_id = e.endpoint_id
                 AND p.ok = 0)                                AS failed_runs
        FROM mi_endpoints e ORDER BY e.endpoint_id
    """).fetchall()
    out = []
    for r in rows:
        consec = con.execute("""
            SELECT COUNT(*) FROM mi_polls
            WHERE endpoint_id = ? AND poll_id > COALESCE(
                (SELECT MAX(poll_id) FROM mi_polls WHERE endpoint_id = ? AND ok = 1), 0)
        """, (r["endpoint_id"], r["endpoint_id"])).fetchone()[0]
        if not r["poll_enabled"]:
            state = "off"
        elif r["last_at"] is None:
            state = "never_polled"
        elif r["last_ok"]:
            state = "ok"
        else:
            state = "failing"
        out.append({
            "endpoint_id": r["endpoint_id"], "slug": r["slug"],
            "endpoint_status": r["status"], "poll_enabled": bool(r["poll_enabled"]),
            "state": state, "last_at": r["last_at"],
            "last_ok": None if r["last_ok"] is None else bool(r["last_ok"]),
            "last_error": r["last_error"], "last_count": r["last_count"],
            "last_success_at": r["last_success_at"],
            "consecutive_failures": consec, "total_runs": r["total_runs"],
            "failed_runs": r["failed_runs"],
        })
    return out


def poll_all(db_path: Optional[str] = None, con: Optional[sqlite3.Connection] = None,
             *, config=None, force: bool = False) -> dict:
    """Poll every active endpoint the operator opted in, at both levels.

    Returns a machine-readable summary with, per endpoint, what changed and why a run
    was skipped. Never raises for endpoint failures — those land in the per-endpoint
    ``error`` field and the ``mi_polls`` audit row. A missing or unmigrated DB is the
    one thing that raises.
    """
    cfg = load_config(config)
    if not cfg.get("poller_enabled"):
        return {"polled_at": _now(), "enabled": False, "endpoints": [],
                "skipped": "polling is switched off in settings"}
    if not source_accepted(cfg, SOURCE_PROVIDER_CATALOG):
        return {"polled_at": _now(), "enabled": False, "endpoints": [],
                "skipped": "the provider_catalog source is not accepted"}

    own = con is None
    c = mi_connect(db_path) if own else con  # type: ignore[assignment]
    try:
        # Capability sources run first: one HTTPS fetch each, touching no endpoint table, so
        # a failure here cannot affect discovery below. Each is gated by its own accepted
        # switch and the default is False — a fresh install polls nothing.
        capabilities: dict = {}
        if source_accepted(cfg, SOURCE_LIVEBENCH):
            try:
                from .livebench_ingest import ingest as _livebench_ingest
                capabilities[SOURCE_LIVEBENCH] = _livebench_ingest(c)
            except Exception as exc:  # noqa: BLE001 — one source must never sink the poll
                logger.warning("livebench ingest failed: %s", exc)
                capabilities[SOURCE_LIVEBENCH] = {"error": str(exc)}
        eps = c.execute(
            "SELECT endpoint_id, slug, base_url FROM mi_endpoints"
            " WHERE status='active' AND poll_enabled=1 ORDER BY endpoint_id"
        ).fetchall()
        results = []
        # An explicit run bypasses the interval: the operator asked for it, the process
        # lock guards against overlap, and the UI disables the button. The interval only
        # paces the unattended scheduler.
        floor = 0.0 if force else float(cfg.get("min_interval_s", 300))
        for r in eps:
            slug, base = r["slug"], r["base_url"]
            if not base:
                results.append({"endpoint": slug, "ok": False, "model_count": 0,
                                "new": [], "stale": [], "suspect": False,
                                "error": "no base_url"})
                continue
            age = _last_poll_age(c, r["endpoint_id"])
            if age is not None and age < floor:
                results.append({"endpoint": slug, "ok": True, "model_count": 0,
                                "new": [], "stale": [], "suspect": False,
                                "skipped": f"last polled {int(age)}s ago"})
                continue
            t0 = time.monotonic()
            ok, status, ids, err = _fetch_endpoint(c, slug, base)
            duration = int((time.monotonic() - t0) * 1000)
            results.append(_diff_and_stage(c, r["endpoint_id"], slug, ok, status,
                                           ids, err, duration, cfg.get("guard")))
        return {"polled_at": _now(), "enabled": True, "endpoints": results,
                "capabilities": capabilities}
    finally:
        if own:
            c.close()


def run_guarded(**kwargs) -> dict:
    """``poll_all`` behind a process lock, so two overlapping runs cannot interleave."""
    if not _run_lock.acquire(blocking=False):
        return {"polled_at": _now(), "enabled": True, "endpoints": [],
                "skipped": "a poll is already running"}
    try:
        return poll_all(**kwargs)
    finally:
        _run_lock.release()


def set_poll_enabled(db_path: Optional[str] = None, con: Optional[sqlite3.Connection] = None,
                     *, slug: str, enabled: bool) -> dict:
    """The user's opt-in switch for one endpoint. Returns the new state."""
    own = con is None
    c = mi_connect(db_path) if own else con  # type: ignore[assignment]
    try:
        cur = c.cursor()
        cur.execute("UPDATE mi_endpoints SET poll_enabled=? WHERE slug=?",
                    (1 if enabled else 0, slug))
        c.commit()
        row = cur.execute(
            "SELECT endpoint_id, slug, poll_enabled FROM mi_endpoints WHERE slug=?",
            (slug,)).fetchone()
        if row is None:
            raise KeyError(f"unknown endpoint slug: {slug}")
        return {"endpoint": row["slug"], "endpoint_id": row["endpoint_id"],
                "poll_enabled": bool(row["poll_enabled"])}
    finally:
        if own:
            c.close()
