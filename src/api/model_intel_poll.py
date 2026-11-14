"""Model-intel poller: fetch fresh provider model lists, stage them for identity.

The poll layer of the model-intel redesign (CUR: "poll fresh data for fresh models and
integrate them carefully with our canonical labels").

Contract
--------
``poll_all`` is the only entry point the scheduler and the admin endpoint call. For each
``active`` endpoint in ``mi_endpoints`` it fetches the live catalog, appends one ``mi_polls``
row (the audit trail), and diffs against ``mi_deployments``:

* known served id              → ``last_seen`` bumped, status untouched
* unknown served id            → new deployment row ``status='new', source='poll'`` —
                                 staged for identity review, never read by resolve()
* known id absent from poll    → ``status='stale_candidate'`` (kept, never auto-retired)

Every endpoint strategy returns ``(ok, http_status, [ids], error)``. Failures are recorded,
never raised — a poll is an observation, and the caller decides what to report.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .model_identity import connect as mi_connect

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------- fetchers


def _fetch_bearer(base_url: str, key: Optional[str], timeout: float = 12.0):
    """Standard OpenAI-style ``GET {base}/models`` with an optional bearer token."""
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        ids = sorted({m.get("id") for m in (data.get("data") or []) if isinstance(m, dict) and isinstance(m.get("id"), str)})
        return True, resp.status if hasattr(resp, "status") else 200, ids, None
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return False, int(e.code), [], f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 — recorded, not raised
        return False, None, [], f"{type(e).__name__}: {e}"[:200]
    finally:
        pass


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
        ids = sorted(ids)
        return True, 200, [i for i in ids if isinstance(i, str)], None
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
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


def _diff_and_stage(con: sqlite3.Connection, endpoint_id: int, slug: str,
                    ok: bool, http_status, ids: list, error: Optional[str],
                    duration_ms: int) -> dict:
    """Write the poll row, then diff ids against mi_deployments (one transaction)."""
    now = _now()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO mi_polls (endpoint_id, polled_at, ok, http_status, model_count,"
        " model_ids_json, error, duration_ms) VALUES (?,?,?,?,?,?,?,?)",
        (endpoint_id, now, 1 if ok else 0, http_status, len(ids) if ok else None,
         json.dumps(ids) if ok else None, error, duration_ms),
    )
    summary = {"endpoint": slug, "ok": ok, "model_count": len(ids) if ok else 0,
               "new": [], "stale": [], "error": error}
    if not ok:
        con.commit()
        return summary

    known = {
        r["served_id"]: r
        for r in cur.execute(
            "SELECT deployment_id, served_id, status FROM mi_deployments WHERE endpoint_id=?",
            (endpoint_id,),
        ).fetchall()
    }
    seen = set()
    for mid in ids:
        seen.add(mid)
        row = known.get(mid)
        if row is None:
            cur.execute(
                "INSERT INTO mi_deployments (endpoint_id, served_id, status, source,"
                " first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                (endpoint_id, mid, "new", "poll", now, now),
            )
            summary["new"].append(mid)
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
        if mid not in seen and row["status"] == "active":
            cur.execute(
                "UPDATE mi_deployments SET status='stale_candidate' WHERE deployment_id=?",
                (row["deployment_id"],),
            )
            summary["stale"].append(mid)
    con.commit()
    return summary


def poll_all(db_path: Optional[str] = None, con: Optional[sqlite3.Connection] = None) -> dict:
    """Poll every active endpoint the user opted in (``poll_enabled=1``).

    Polling is off by default per endpoint (migration 031): a poll run touches
    only endpoints the user explicitly toggled on. Never raises for endpoint
    failures — those land in the per-endpoint ``error`` field and the
    ``mi_polls`` audit row. Only a missing/unmigrated DB raises.
    """
    own = con is None
    c = mi_connect(db_path) if own else con  # type: ignore[assignment]
    try:
        eps = c.execute(
            "SELECT endpoint_id, slug, base_url FROM mi_endpoints"
            " WHERE status='active' AND poll_enabled=1"
            " ORDER BY endpoint_id"
        ).fetchall()
        results = []
        for r in eps:
            slug, base = r["slug"], r["base_url"]
            if not base:
                results.append({"endpoint": slug, "ok": False, "model_count": 0,
                                "new": [], "stale": [], "error": "no base_url"})
                continue
            t0 = time.monotonic()
            ok, status, ids, err = _fetch_endpoint(c, slug, base)
            duration = int((time.monotonic() - t0) * 1000)
            results.append(_diff_and_stage(c, r["endpoint_id"], slug, ok, status,
                                           ids, err, duration))
        return {"polled_at": _now(), "endpoints": results}
    finally:
        if own:
            c.close()


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
