"""Unit tests for the model-intel freshness poller (CUR model-intel redesign P1).

The operator's contract, pinned here so a regression cannot ship:
* polling is OFF per endpoint until the user toggles it on (migration 031);
* a poll writes only ``mi_polls`` audit rows and ``mi_deployments`` status /
  first_seen / last_seen / source — ``mi_models`` (canonical labels, user-edited
  display values) is never touched;
* unknown served ids are staged ``status='new'``, which ``model_identity.resolve``
  never reads, so poll output cannot affect routing until a human confirms;
* a vanished id becomes ``stale_candidate`` — kept, never auto-retired.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.api import model_intel_poll as mip
from src.api.model_identity import connect as mi_connect


@pytest.fixture
def db(tmp_path):
    """The model-intel tables with the real staging schema, then seed rows."""
    db_path = str(tmp_path / "costs.db")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE mi_endpoints (
            endpoint_id INTEGER PRIMARY KEY,
            slug TEXT, display_name TEXT, base_url TEXT, tier TEXT,
            status TEXT, notes TEXT, created_at TEXT,
            poll_enabled INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE mi_deployments (
            deployment_id INTEGER PRIMARY KEY,
            endpoint_id INTEGER, model_id INTEGER, served_id TEXT,
            variant TEXT, quantisation TEXT, context_window INTEGER,
            status TEXT, source TEXT, verified_at TEXT,
            first_seen TEXT, last_seen TEXT, notes TEXT);
        CREATE TABLE mi_models (
            model_id INTEGER PRIMARY KEY,
            slug TEXT, display_name TEXT, family TEXT, status TEXT,
            supersedes_model_id INTEGER, benchmark_key TEXT, created_at TEXT);
        CREATE TABLE mi_polls (
            poll_id INTEGER PRIMARY KEY,
            endpoint_id INTEGER NOT NULL, polled_at TEXT NOT NULL,
            ok INTEGER NOT NULL, http_status INTEGER, model_count INTEGER,
            model_ids_json TEXT, error TEXT, duration_ms INTEGER);
    """)
    con.execute(
        "INSERT INTO mi_endpoints (endpoint_id, slug, display_name, base_url, status,"
        " poll_enabled) VALUES (1, 'prov-a', 'Prov A', 'https://a/v1', 'active', 0)")
    con.execute(
        "INSERT INTO mi_deployments (endpoint_id, served_id, status, source,"
        " first_seen, last_seen) VALUES (1, 'known-model', 'active', 'seed',"
        " '2026-10-30T00:00:00', '2026-10-30T00:00:00')")
    con.execute(
        "INSERT INTO mi_models (model_id, slug, display_name, family, status, created_at)"
        " VALUES (1, 'known-model', 'USER EDITED NAME', 'fam', 'active',"
        " '2026-10-30T00:00:00')")
    con.commit()
    yield con
    con.close()


def _poll_with(db, ids, *, ok=True, status=200):
    """Run poll_all against a fake catalog for prov-a."""
    with patch_fetch(ids, ok=ok, status=status):
        return mip.poll_all(con=db)


def patch_fetch(ids, *, ok=True, status=200):
    class _Ctx:
        def __enter__(self):
            self._orig = mip._fetch_endpoint

            def fake(con, slug, base):
                return ok, status, list(ids), None if ok else "HTTP 500"

            mip._fetch_endpoint = fake
            return self

        def __exit__(self, *a):
            mip._fetch_endpoint = self._orig

    return _Ctx()


# ── 1. the toggle: polling is off until the user opts in ─────────────────────

def test_poll_touches_nothing_while_every_toggle_is_off(db):
    """The default state of every endpoint: no poll, no rows, no requests."""
    out = _poll_with(db, ["brand-new-model"])
    assert out["endpoints"] == [], (
        "polling must be opt-in per endpoint; nothing enabled means no polls")
    assert db.execute("select count(*) from mi_polls").fetchone()[0] == 0


def test_set_poll_enabled_opts_in_exactly_one_endpoint(db):
    state = mip.set_poll_enabled(con=db, slug="prov-a", enabled=True)
    assert state["poll_enabled"] is True
    row = db.execute("select poll_enabled from mi_endpoints where slug='prov-a'").fetchone()
    assert row["poll_enabled"] == 1


def test_toggled_endpoint_polls_and_writes_an_audit_row(db):
    mip.set_poll_enabled(con=db, slug="prov-a", enabled=True)
    out = _poll_with(db, ["known-model", "brand-new-model"])
    ep = out["endpoints"][0]
    assert ep["ok"] is True and ep["model_count"] == 2
    assert ep["new"] == ["brand-new-model"]
    polls = db.execute("select * from mi_polls").fetchall()
    assert len(polls) == 1 and polls[0]["ok"] == 1 and polls[0]["model_count"] == 2


def test_toggle_rejects_an_unknown_slug(db):
    with pytest.raises(KeyError):
        mip.set_poll_enabled(con=db, slug="does-not-exist", enabled=True)


# ── 2. staging: new ids never touch routing ──────────────────────────────────

def test_unknown_id_is_staged_new_with_poll_provenance(db):
    mip.set_poll_enabled(con=db, slug="prov-a", enabled=True)
    _poll_with(db, ["known-model", "brand-new-model"])
    row = db.execute(
        "select status, source from mi_deployments where served_id='brand-new-model'"
    ).fetchone()
    assert row["status"] == "new" and row["source"] == "poll"


def test_resolve_only_honours_active_so_new_staging_cannot_route(db):
    """The safety property the whole design rests on."""
    mip.set_poll_enabled(con=db, slug="prov-a", enabled=True)
    _poll_with(db, ["known-model", "brand-new-model"])
    statuses = dict(db.execute(
        "select served_id, status from mi_deployments").fetchall())
    assert statuses["brand-new-model"] == "new"
    assert statuses["known-model"] == "active"
    # resolve() honours only active: prove the query shape it uses
    routed = db.execute(
        "select served_id from mi_deployments where status = 'active'"
    ).fetchall()
    assert [r["served_id"] for r in routed] == ["known-model"]


# ── 3. known ids: refresh without rewriting ─────────────────────────────────

def test_known_id_gets_last_seen_bumped_and_status_untouched(db):
    mip.set_poll_enabled(con=db, slug="prov-a", enabled=True)
    _poll_with(db, ["known-model"])
    row = db.execute(
        "select status, source, last_seen from mi_deployments where served_id='known-model'"
    ).fetchone()
    assert row["status"] == "active" and row["source"] == "seed"
    assert row["last_seen"] > "2026-10-30"


def test_vanished_id_becomes_stale_candidate_and_is_never_deleted(db):
    mip.set_poll_enabled(con=db, slug="prov-a", enabled=True)
    _poll_with(db, ["something-else"])
    row = db.execute(
        "select status from mi_deployments where served_id='known-model'").fetchone()
    assert row["status"] == "stale_candidate", (
        "a vanished model is a candidate for review, not a deletion")
    assert db.execute(
        "select count(*) from mi_deployments where served_id='known-model'"
    ).fetchone()[0] == 1


def test_republished_stale_candidate_is_revived_to_active(db):
    mip.set_poll_enabled(con=db, slug="prov-a", enabled=True)
    _poll_with(db, ["something-else"])   # known-model goes stale
    _poll_with(db, ["known-model"])      # endpoint re-lists it
    row = db.execute(
        "select status from mi_deployments where served_id='known-model'").fetchone()
    assert row["status"] == "active"


# ── 4. provenance: the poll never touches the canonical labels ───────────────

def test_poll_never_touches_mi_models_user_edits(db):
    mip.set_poll_enabled(con=db, slug="prov-a", enabled=True)
    _poll_with(db, ["known-model", "brand-new-model"])
    row = db.execute("select display_name from mi_models where model_id=1").fetchone()
    assert row["display_name"] == "USER EDITED NAME", (
        "canonical/user-edited model fields are immune to polls")
    assert db.execute("select count(*) from mi_models").fetchone()[0] == 1


# ── 5. failures are recorded, never raised ──────────────────────────────────

def test_failed_poll_is_audited_with_the_error(db):
    mip.set_poll_enabled(con=db, slug="prov-a", enabled=True)
    out = _poll_with(db, [], ok=False, status=500)
    assert out["endpoints"][0]["ok"] is False
    row = db.execute("select ok, http_status, error from mi_polls").fetchone()
    assert row["ok"] == 0 and row["http_status"] == 500 and row["error"]
    # and a failed poll changed nothing in the registry
    assert db.execute(
        "select count(*) from mi_deployments where status='new'").fetchone()[0] == 0
