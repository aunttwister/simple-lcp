"""Unit tests for the identity promotion queue (model-intel canonicalisation).

The rule under test: a capability row may only be keyed by a spelling the read path
searches (canonical slug, declared alias, served id, benchmark key). Anything else is a
dead row — it can never be joined — so it must be queued instead. These tests pin that,
plus the promotion path that makes queued rows reachable.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.api import model_identity_queue as q


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE mi_models (model_id INTEGER PRIMARY KEY, slug TEXT UNIQUE,
            benchmark_key TEXT);
        CREATE TABLE mi_endpoints (endpoint_id INTEGER PRIMARY KEY, slug TEXT);
        CREATE TABLE mi_deployments (deployment_id INTEGER PRIMARY KEY,
            endpoint_id INTEGER, model_id INTEGER, served_id TEXT);
        CREATE TABLE mi_sources (source_id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL, endpoint_id INTEGER, convention TEXT,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT);
        CREATE TABLE mi_model_names (id INTEGER PRIMARY KEY, model_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL, name TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0, confidence TEXT, evidence TEXT,
            valid_from TEXT, valid_to TEXT, first_seen TEXT, last_seen TEXT,
            verified_at TEXT, note TEXT, UNIQUE(source_id, name));
        CREATE TABLE model_capabilities (id INTEGER PRIMARY KEY, model TEXT, task_type TEXT,
            score REAL, source TEXT, raw_score REAL);
        CREATE TABLE mi_identity_queue (
            id INTEGER PRIMARY KEY, raw TEXT NOT NULL, norm TEXT NOT NULL,
            source TEXT NOT NULL, kind TEXT NOT NULL, evidence TEXT,
            suggested_slug TEXT, suggested_by TEXT, status TEXT NOT NULL DEFAULT 'pending',
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            occurrences INTEGER NOT NULL DEFAULT 1, decided_at TEXT, note TEXT,
            UNIQUE(norm, source));
    """)
    con.execute("INSERT INTO mi_models VALUES (1, 'claude-opus-5', 'claude-opus-5-bench')")
    con.execute("INSERT INTO mi_models VALUES (2, 'gpt-5.5', NULL)")
    con.execute("INSERT INTO mi_endpoints VALUES (1, 'commandcode')")
    con.execute("INSERT INTO mi_deployments VALUES (1, 1, 2, 'gpt-5.5')")
    for slug, kind in (("canonical", "canonical"), ("benchmark", "benchmark_arena"),
                       ("lmarena", "benchmark_arena"), ("operator", "operator")):
        con.execute("INSERT INTO mi_sources (slug, kind) VALUES (?,?)", (slug, kind))
    # an operator-declared name for claude-opus-5
    con.execute(
        "INSERT INTO mi_model_names (model_id, source_id, name, confidence, verified_at)"
        " VALUES (1, (SELECT source_id FROM mi_sources WHERE slug='operator'),"
        " 'claude-5-opus-thinking', 'verified', '2026-01-01')")
    con.commit()
    yield con
    con.close()


# ── resolution: the registry's own routes, nothing fuzzier ──────────────────

def test_slug_alias_served_id_and_benchmark_key_all_resolve(db):
    assert q.resolve_canonical(db, "claude-opus-5") == "claude-opus-5"
    assert q.resolve_canonical(db, "claude-5-opus-thinking") == "claude-opus-5"
    assert q.resolve_canonical(db, "gpt-5.5") == "gpt-5.5"
    assert q.resolve_canonical(db, "claude-opus-5-bench") == "claude-opus-5"


def test_provider_prefix_and_variant_are_tolerated_on_the_normalised_form(db):
    assert q.resolve_canonical(db, "xai/gpt-5.5") == "gpt-5.5"
    assert q.resolve_canonical(db, "Claude Opus 5 (High)") == "claude-opus-5"


def test_unrelated_name_does_not_resolve(db):
    """No fuzzy guessing: a wrong link fabricates capability data."""
    assert q.resolve_canonical(db, "Claude Opus 4.6") is None, (
        "a different generation must never resolve onto claude-opus-5")
    assert q.resolve_canonical(db, "Alpaca 13B") is None


def test_suggestion_is_labelled_unverified(db):
    sug = q.suggest(db, "Claude Opus 4.6")
    assert sug and sug["slug"] == "claude-opus-5"
    assert sug["method"] == "prefix2" and "unverified" in sug["warning"], (
        "a guess must be presented as a guess, never applied silently")


# ── observation: resolved joins, unresolved queues ─────────────────────────

def test_resolved_observation_is_not_queued(db):
    out = q.observe(db, "gpt-5.5", source=q.SOURCE_LMARENA, kind=q.KIND_CAPABILITY_NAME)
    assert out["status"] == "resolved" and out["canonical"] == "gpt-5.5"
    assert db.execute("select count(*) from mi_identity_queue").fetchone()[0] == 0


def test_unresolved_observation_is_queued_with_evidence(db):
    out = q.observe(db, "Some New Model", source=q.SOURCE_LMARENA,
                    kind=q.KIND_CAPABILITY_NAME, evidence={"categories": ["coding"]})
    assert out["status"] == "queued"
    row = db.execute("select * from mi_identity_queue where id=?", (out["queue_id"],)).fetchone()
    assert row["status"] == "pending" and row["source"] == "lmarena"
    assert row["kind"] == "capability_name" and "coding" in row["evidence"]


def test_repeats_bump_occurrences_instead_of_duplicating(db):
    q.observe(db, "Some New Model", source=q.SOURCE_LMARENA, kind=q.KIND_CAPABILITY_NAME)
    q.observe(db, "Some New Model", source=q.SOURCE_LMARENA, kind=q.KIND_CAPABILITY_NAME)
    rows = db.execute("select occurrences from mi_identity_queue").fetchall()
    assert len(rows) == 1 and rows[0]["occurrences"] == 2


def test_same_spelling_from_two_sources_stays_two_entries(db):
    q.observe(db, "Some New Model", source=q.SOURCE_LMARENA, kind=q.KIND_CAPABILITY_NAME)
    q.observe(db, "Some New Model", source=q.SOURCE_PROVIDER_CATALOG, kind=q.KIND_SERVED_ID)
    assert db.execute("select count(*) from mi_identity_queue").fetchone()[0] == 2


# ── promotion: the only path from observed to reachable ─────────────────────

def test_accept_writes_the_alias_that_makes_capabilities_reachable(db):
    ref = q.observe(db, "Claude Opus 4.6", source=q.SOURCE_LMARENA,
                    kind=q.KIND_CAPABILITY_NAME)
    q.accept(db, ref["queue_id"], slug="claude-opus-5", note="operator confirmed")
    names = [r["name"] for r in db.execute(
        "SELECT name FROM mi_model_names WHERE source_id ="
        " (SELECT source_id FROM mi_sources WHERE slug='operator')").fetchall()]
    assert "Claude Opus 4.6" in names, (
        "accept must record the name under the operator source — that is the bridge")
    row = db.execute("select status from mi_identity_queue where id=?",
                     (ref["queue_id"],)).fetchone()
    assert row["status"] == "accepted"


def test_accept_rekeys_capability_rows_already_stored_under_the_raw_name(db):
    db.execute("INSERT INTO model_capabilities VALUES (1, 'Claude Opus 4.6',"
               " 'code_generation', 90.0, 'lmarena', 1500.0)")
    db.commit()
    ref = q.observe(db, "Claude Opus 4.6", source=q.SOURCE_LMARENA,
                    kind=q.KIND_CAPABILITY_NAME)
    out = q.accept(db, ref["queue_id"], slug="claude-opus-5")
    assert out["capability_rows_rekeyed"] == 1
    assert db.execute("select model from model_capabilities").fetchone()["model"] == "claude-opus-5"


def test_accept_rejects_an_unknown_slug(db):
    ref = q.observe(db, "Some New Model", source=q.SOURCE_LMARENA,
                    kind=q.KIND_CAPABILITY_NAME)
    with pytest.raises(KeyError):
        q.accept(db, ref["queue_id"], slug="not-a-model")


def test_accept_is_idempotent(db):
    """A retry (double-click, network replay) must not stack duplicate names —
    duplicates fake extra attestation."""
    ref = q.observe(db, "Some New Model", source=q.SOURCE_LMARENA,
                    kind=q.KIND_CAPABILITY_NAME)
    first = q.accept(db, ref["queue_id"], slug="gpt-5.5")
    second = q.accept(db, ref["queue_id"], slug="gpt-5.5")
    assert first["name_recorded"] is True and second["name_recorded"] is False
    n = db.execute(
        "select count(*) from mi_model_names where name='Some New Model'").fetchone()[0]
    assert n == 1, "the same spelling must not be recorded twice"


def test_reject_records_the_decision_and_stops_re_offering(db):
    ref = q.observe(db, "Junk Name", source=q.SOURCE_LMARENA, kind=q.KIND_CAPABILITY_NAME)
    q.reject(db, ref["queue_id"], note="not a real model")
    row = db.execute("select status from mi_identity_queue where id=?",
                     (ref["queue_id"],)).fetchone()
    assert row["status"] == "rejected"
    assert q.list_pending(db) == [], "a rejected spelling must not reappear as pending"


# ── cleanup: dead rows out of the capability table ──────────────────────────

def test_purge_rekeys_what_resolves_and_removes_what_cannot(db):
    db.executescript("""
        INSERT INTO model_capabilities VALUES (1, 'claude-5-opus-thinking', 'x', 1, 'lmarena', 1);
        INSERT INTO model_capabilities VALUES (2, 'Alpaca 13B', 'x', 1, 'lmarena', 1);
        INSERT INTO model_capabilities VALUES (3, 'claude-opus-5', 'y', 1, 'livebench', 1);
    """)
    db.commit()
    out = q.purge_raw_capability_rows(db)
    assert out["rekeyed_rows"] == 1, "an alias-keyed row is re-keyed onto the canonical slug"
    assert out["removed_rows"] == 1 and out["removed_keys"] == ["Alpaca 13B"]
    models = {r["model"] for r in db.execute("select model from model_capabilities")}
    assert models == {"claude-opus-5"}, "only joinable keys may remain"
