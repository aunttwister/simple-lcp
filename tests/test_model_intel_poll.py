"""Unit tests for the model-intel freshness poller (CUR model-intel redesign).

The operator's contract, pinned here so a regression cannot ship:
* polling is OFF until the master switch is on AND the source is accepted AND the
  endpoint is opted in — three independent layers;
* a poll writes only ``mi_polls`` audit rows and ``mi_deployments`` status /
  first_seen / last_seen / source — ``mi_models`` (canonical labels, user-edited
  display values) is never touched;
* unknown served ids are staged ``status='new'``, which ``model_identity.resolve``
  never reads, so poll output cannot affect routing until a human confirms;
* a served id that resolves is staged LINKED, so its capabilities are joinable;
* a vanished id becomes ``stale_candidate`` — kept, never auto-retired;
* an empty or sharply shrunken listing is a broken read, not a mass retirement;
* a user-verified deployment is never auto-staled by a poll.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.api import model_intel_poll as mip


class _Cfg:
    """A minimal stand-in for the gateway config object."""

    def __init__(self, section=None):
        self.raw = {"model_intel": section} if section else {}
        self.saved = False

    def save(self):
        self.saved = True


def _enabled_cfg(**over):
    section = {
        "poller_enabled": True,
        "sources": {"provider_catalog": {"accepted": True}},
    }
    section.update(over)
    return _Cfg(section)


SCHEMA = """
CREATE TABLE mi_endpoints (
    endpoint_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT, base_url TEXT,
    status TEXT, poll_enabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE mi_deployments (
    deployment_id INTEGER PRIMARY KEY, endpoint_id INTEGER, model_id INTEGER,
    served_id TEXT, variant TEXT, quantisation TEXT, context_window INTEGER,
    status TEXT, source TEXT, verified_at TEXT, first_seen TEXT, last_seen TEXT,
    notes TEXT);
CREATE TABLE mi_models (
    model_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT, family TEXT,
    status TEXT, supersedes_model_id INTEGER, benchmark_key TEXT, created_at TEXT);
CREATE TABLE mi_sources (
    source_id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
    endpoint_id INTEGER, convention TEXT, status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT);
CREATE TABLE mi_model_names (
    id INTEGER PRIMARY KEY, model_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
    name TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0, confidence TEXT,
    evidence TEXT, valid_from TEXT, valid_to TEXT, first_seen TEXT, last_seen TEXT,
    verified_at TEXT, note TEXT, UNIQUE(source_id, name));
CREATE TABLE mi_polls (
    poll_id INTEGER PRIMARY KEY, endpoint_id INTEGER NOT NULL, polled_at TEXT NOT NULL,
    ok INTEGER NOT NULL, http_status INTEGER, model_count INTEGER,
    model_ids_json TEXT, error TEXT, duration_ms INTEGER);
CREATE TABLE mi_identity_queue (
    id INTEGER PRIMARY KEY, raw TEXT NOT NULL, norm TEXT NOT NULL, source TEXT NOT NULL,
    kind TEXT NOT NULL, evidence TEXT, suggested_slug TEXT, suggested_by TEXT,
    status TEXT NOT NULL DEFAULT 'pending', first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
    decided_at TEXT, note TEXT, UNIQUE(norm, source));
"""


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute(
        "INSERT INTO mi_endpoints (endpoint_id, slug, display_name, base_url, status,"
        " poll_enabled) VALUES (1, 'prov-a', 'Prov A', 'https://a/v1', 'active', 1)")
    con.execute(
        "INSERT INTO mi_deployments (endpoint_id, served_id, status, source,"
        " first_seen, last_seen) VALUES (1, 'known-model', 'active', 'seed',"
        " '2026-10-30T00:00:00', '2026-10-30T00:00:00')")
    con.execute(
        "INSERT INTO mi_models (model_id, slug, display_name, family, status, created_at)"
        " VALUES (1, 'known-model', 'USER EDITED NAME', 'fam', 'active',"
        " '2026-10-30T00:00:00')")
    for slug, kind in (("canonical", "canonical"), ("benchmark", "benchmark_arena"),
                       ("lmarena", "benchmark_arena"), ("operator", "operator"),
                       ("prov-a", "provider_endpoint")):
        con.execute("INSERT INTO mi_sources (slug, kind) VALUES (?,?)", (slug, kind))
    con.commit()
    yield con
    con.close()


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


def _poll(db, ids, *, ok=True, status=200, cfg=None):
    with patch_fetch(ids, ok=ok, status=status):
        return mip.poll_all(con=db, config=cfg or _enabled_cfg(), force=True)


# ── 1. the three switches: master, source, endpoint ─────────────────────────

def test_poll_does_nothing_while_the_master_switch_is_off(db):
    out = mip.poll_all(con=db, config=_Cfg())  # no section → defaults, all off
    assert out["enabled"] is False and out["endpoints"] == []
    assert "off" in out["skipped"]
    assert db.execute("select count(*) from mi_polls").fetchone()[0] == 0


def test_poll_names_the_sources_it_skipped(db):
    """Superseded 2026-11-19. It used to assert the whole poll switched itself off when
    provider_catalog was unaccepted — which meant a config that wanted LiveBench but not
    provider discovery could never run, and reported a reason that pointed at the wrong
    switch. The master switch governs `enabled`; each source governs its own work, and a run
    that did nothing must SAY which switches were off."""
    cfg = _Cfg({"poller_enabled": True})
    out = mip.poll_all(con=db, config=cfg)
    assert out["enabled"] is True, "the master switch is on, so polling is on"
    assert out["endpoints"] == [] and out["capabilities"] == {}
    assert out["sources_accepted"] == []
    assert "livebench" in out["sources_skipped"]
    assert "nothing to poll" in out["skipped"]


def test_livebench_runs_without_provider_catalog_being_accepted(db, monkeypatch):
    """The defect this pins: the provider_catalog gate returned early for the WHOLE poll."""
    from src.api import livebench_ingest as li
    monkeypatch.setattr(li, "ingest", lambda con, **kw: {"rows": 6, "models_matched": 1})
    cfg = _Cfg({"poller_enabled": True, "sources": {"livebench": {"accepted": True}}})
    out = mip.poll_all(con=db, config=cfg, force=True)
    assert out["enabled"] is True
    assert out["capabilities"]["livebench"]["rows"] == 6
    assert out["endpoints"] == [], "provider discovery was not asked for"
    assert "provider_catalog" in out["sources_skipped"]


def test_a_failing_livebench_source_does_not_sink_the_poll(db, monkeypatch):
    """One HTTPS source failing must not take down discovery, and must surface as an error
    rather than as silence."""
    from src.api import livebench_ingest as li

    def boom(con, **kw):
        raise RuntimeError("503 from livebench")

    monkeypatch.setattr(li, "ingest", boom)
    cfg = _Cfg({"poller_enabled": True, "sources": {"livebench": {"accepted": True}}})
    out = mip.poll_all(con=db, config=cfg, force=True)
    assert out["enabled"] is True
    assert "503" in out["capabilities"]["livebench"]["error"]


def test_poll_skips_an_endpoint_that_is_not_opted_in(db):
    db.execute("update mi_endpoints set poll_enabled=0")
    db.commit()
    out = _poll(db, ["x"])
    assert out["endpoints"] == []


def test_toggled_endpoint_polls_and_writes_an_audit_row(db):
    out = _poll(db, ["known-model", "brand-new-model"])
    ep = out["endpoints"][0]
    assert ep["ok"] is True and ep["model_count"] == 2
    assert "brand-new-model" in ep["new"]
    polls = db.execute("select * from mi_polls").fetchall()
    assert len(polls) == 1 and polls[0]["ok"] == 1 and polls[0]["model_count"] == 2


def test_set_poll_enabled_round_trips_and_rejects_unknown_slug(db):
    assert mip.set_poll_enabled(con=db, slug="prov-a", enabled=False)["poll_enabled"] is False
    with pytest.raises(KeyError):
        mip.set_poll_enabled(con=db, slug="nope", enabled=True)


def test_second_run_inside_the_floor_is_skipped(db):
    _poll(db, ["known-model"])
    out = mip.poll_all(con=db, config=_enabled_cfg())  # not forced
    assert out["endpoints"][0].get("skipped"), (
        "two runs in quick succession must not interleave")


# ── 2. staging: new ids never touch routing ──────────────────────────────────

def test_unknown_id_is_staged_new_with_poll_provenance(db):
    _poll(db, ["known-model", "brand-new-model"])
    row = db.execute(
        "select status, source from mi_deployments where served_id='brand-new-model'"
    ).fetchone()
    assert row["status"] == "new" and row["source"] == "poll"


def test_resolve_only_honours_active_so_new_staging_cannot_route(db):
    _poll(db, ["known-model", "brand-new-model"])
    routed = [r["served_id"] for r in db.execute(
        "select served_id from mi_deployments where status = 'active'")]
    assert routed == ["known-model"]


def test_a_served_id_that_matches_a_known_alias_is_staged_linked(db):
    """The canonicalisation win: a staged model that has an identity also has
    capabilities, because capability rows join on that identity."""
    db.execute(
        "INSERT INTO mi_model_names (model_id, source_id, name) VALUES (1,"
        " (SELECT source_id FROM mi_sources WHERE slug='operator'), 'served/known-model')")
    db.commit()
    out = _poll(db, ["served/known-model"])
    row = db.execute(
        "select model_id from mi_deployments where served_id='served/known-model'"
    ).fetchone()
    assert row["model_id"] == 1, "a resolvable served id must be staged linked"
    assert out["endpoints"][0].get("linked") == ["served/known-model"]


def test_an_unresolvable_served_id_is_queued_not_left_anonymous(db):
    _poll(db, ["totally-unknown/some-model"])
    q = db.execute("select raw, source, kind from mi_identity_queue").fetchall()
    assert len(q) == 1 and q[0]["source"] == "provider_catalog"
    assert q[0]["kind"] == "served_id"


# ── 3. known ids, stale candidates, user-verified immunity ──────────────────

def test_known_id_gets_last_seen_bumped_and_status_untouched(db):
    _poll(db, ["known-model"])
    row = db.execute(
        "select status, source, last_seen from mi_deployments where served_id='known-model'"
    ).fetchone()
    assert row["status"] == "active" and row["source"] == "seed"
    assert row["last_seen"] > "2026-10-30"


def test_vanished_id_becomes_stale_candidate_and_is_never_deleted(db):
    _poll(db, ["something-else"])
    row = db.execute(
        "select status from mi_deployments where served_id='known-model'").fetchone()
    assert row["status"] == "stale_candidate", (
        "a vanished model is a candidate for review, not a deletion")
    assert db.execute(
        "select count(*) from mi_deployments where served_id='known-model'"
    ).fetchone()[0] == 1


def test_republished_stale_candidate_is_revived(db):
    _poll(db, ["something-else"])
    _poll(db, ["known-model"])
    row = db.execute(
        "select status from mi_deployments where served_id='known-model'").fetchone()
    assert row["status"] == "active"


def test_a_user_verified_deployment_is_never_auto_staled(db):
    """A human confirmed it; a poll must not overrule that."""
    db.execute("update mi_deployments set verified_at='2026-11-01T00:00:00'"
               " where served_id='known-model'")
    db.commit()
    out = _poll(db, ["something-else"])
    row = db.execute(
        "select status from mi_deployments where served_id='known-model'").fetchone()
    assert row["status"] == "active", "a verified deployment must survive a poll"
    assert "known-model" in out["endpoints"][0].get("protected", [])


# ── 4. edge cases: a broken read is not a mass retirement ───────────────────

def test_empty_catalog_stages_nothing_and_stales_nothing(db):
    out = _poll(db, [])
    ep = out["endpoints"][0]
    assert ep["suspect"] is True and "empty" in ep["suspect_reason"]
    assert db.execute(
        "select status from mi_deployments where served_id='known-model'").fetchone()[
            "status"] == "active", "an empty response must not retire the estate"
    assert db.execute("select count(*) from mi_deployments").fetchone()[0] == 1


def test_sharply_shrunken_listing_is_suspicious(db):
    for i in range(10):
        db.execute("INSERT INTO mi_deployments (endpoint_id, served_id, status, source,"
                   " first_seen, last_seen) VALUES (1, ?, 'active', 'seed', 'x', 'x')",
                   (f"m{i}",))
    db.commit()
    out = _poll(db, ["m0"])  # 1 of 11 known active
    assert out["endpoints"][0]["suspect"] is True
    still_active = db.execute(
        "select count(*) from mi_deployments where status='active'").fetchone()[0]
    assert still_active == 11, "a partial listing must not mass-retire"


def test_a_small_but_complete_listing_is_not_suspicious(db):
    out = _poll(db, ["known-model", "n1", "n2", "n3"])
    assert out["endpoints"][0]["suspect"] is False


# ── 5. provenance: canonical labels are immune, failures are recorded ───────

def test_poll_never_touches_mi_models_user_edits(db):
    _poll(db, ["known-model", "brand-new-model"])
    row = db.execute("select display_name from mi_models where model_id=1").fetchone()
    assert row["display_name"] == "USER EDITED NAME", (
        "canonical/user-edited model fields are immune to polls")
    assert db.execute("select count(*) from mi_models").fetchone()[0] == 1


def test_failed_poll_is_audited_with_the_error_and_changes_nothing(db):
    out = _poll(db, [], ok=False, status=500)
    assert out["endpoints"][0]["ok"] is False
    row = db.execute("select ok, http_status, error from mi_polls").fetchone()
    assert row["ok"] == 0 and row["http_status"] == 500 and row["error"]
    assert db.execute(
        "select count(*) from mi_deployments where status='new'").fetchone()[0] == 0


# ── 6. health: a dead poller must not look switched off ─────────────────────

def test_health_reports_off_never_polled_ok_and_failing(db):
    assert mip.endpoint_health(db)[0]["state"] == "never_polled"

    _poll(db, ["known-model"])
    assert mip.endpoint_health(db)[0]["state"] == "ok"

    _poll(db, [], ok=False, status=503)
    h = mip.endpoint_health(db)[0]
    assert h["state"] == "failing" and h["consecutive_failures"] == 1
    assert h["last_success_at"], "the last good run must stay visible"

    db.execute("update mi_endpoints set poll_enabled=0")
    db.commit()
    assert mip.endpoint_health(db)[0]["state"] == "off"


def test_run_guarded_reports_a_concurrent_run_instead_of_interleaving(db):
    mip._run_lock.acquire()
    try:
        out = mip.run_guarded(con=db, config=_enabled_cfg())
        assert "already running" in out["skipped"]
    finally:
        mip._run_lock.release()


# ── 7. config: defaults are all-off, and a write is surgical ────────────────

def test_defaults_are_off_and_a_missing_config_cannot_start_polling():
    cfg = mip.load_config(_Cfg())
    assert cfg["poller_enabled"] is False
    assert mip.source_accepted(cfg, mip.SOURCE_PROVIDER_CATALOG) is False
    assert mip.source_accepted(cfg, mip.SOURCE_LMARENA) is False


def test_save_config_merges_without_clearing_siblings():
    cfg = _Cfg()
    mip.save_config(cfg, {"poller_enabled": True})
    mip.save_config(cfg, {"sources": {"lmarena": {"accepted": True}}})
    eff = mip.load_config(cfg)
    assert eff["poller_enabled"] is True
    assert mip.source_accepted(eff, mip.SOURCE_LMARENA) is True
    assert eff["guard"]["max_shrink_pct"] == 50, "untouched keys must survive"
