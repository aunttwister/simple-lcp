"""Unit tests for the LMArena capability ingester (CUR model-intel).

No network: ``_fetch_rows`` is monkeypatched to a fixed leaderboard snapshot.

The contract under test is the canonicalisation rule: a capability row is written ONLY
for an arena name that resolves to a canonical model through the registry's own routes.
Everything else goes to the identity queue, because a capability row keyed by a spelling
no read path searches can never be joined — it inflates apparent coverage and is exactly
the defect this replaced (2,743 dead rows of 2,869).
"""
from __future__ import annotations

import sqlite3

import pytest

from src.api import lmarena_ingest as li


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE mi_models (model_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT,
          benchmark_key TEXT);
        CREATE TABLE mi_sources (source_id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL,
          kind TEXT NOT NULL, endpoint_id INTEGER, convention TEXT,
          status TEXT NOT NULL DEFAULT 'active', created_at TEXT);
        CREATE TABLE mi_model_names (id INTEGER PRIMARY KEY, model_id INTEGER NOT NULL,
          source_id INTEGER NOT NULL, name TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0,
          confidence TEXT, evidence TEXT, valid_from TEXT, valid_to TEXT, first_seen TEXT,
          last_seen TEXT, verified_at TEXT, note TEXT, UNIQUE(source_id, name));
        CREATE TABLE mi_deployments (deployment_id INTEGER PRIMARY KEY, endpoint_id INTEGER,
          model_id INTEGER, served_id TEXT);
        CREATE TABLE model_capabilities (id INTEGER PRIMARY KEY, model TEXT, task_type TEXT,
          score REAL, source TEXT, benchmark_category TEXT, raw_score REAL,
          updated_at TEXT, release_label TEXT);
        CREATE TABLE mi_identity_queue (
          id INTEGER PRIMARY KEY, raw TEXT NOT NULL, norm TEXT NOT NULL,
          source TEXT NOT NULL, kind TEXT NOT NULL, evidence TEXT, suggested_slug TEXT,
          suggested_by TEXT, status TEXT NOT NULL DEFAULT 'pending',
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          occurrences INTEGER NOT NULL DEFAULT 1, decided_at TEXT, note TEXT,
          UNIQUE(norm, source));
    """)
    con.execute("INSERT INTO mi_models VALUES (1, 'claude-opus-5', 'Claude Opus 5', NULL)")
    con.execute("INSERT INTO mi_models VALUES (2, 'gpt-5.5', 'GPT 5.5', NULL)")
    for slug, kind in (("canonical", "canonical"), ("benchmark", "benchmark_arena"),
                       ("lmarena", "benchmark_arena"), ("operator", "operator")):
        con.execute("INSERT INTO mi_sources (slug, kind) VALUES (?,?)", (slug, kind))
    con.execute("INSERT INTO model_capabilities VALUES (1, 'claude-opus-5',"
                " 'code_generation', 88.0, 'livebench', 'coding', 0.77,"
                " '2026-10-30', '2026-08-13')")
    con.commit()
    yield con
    con.close()


ARENA_ROWS = [
    # (category, model_name, rating, rank, votes, publish_date)
    ("coding", "Claude Opus 5", 1530.0, 1, 20000, "2026-11-11"),
    ("coding", "Mystery Model X", 1400.0, 2, 5000, "2026-11-11"),
    ("coding", "Claude Opus 5", 1500.0, 1, 10000, "2026-11-04"),  # superseded
    ("math", "gpt-5.5", 1510.0, 1, 15000, "2026-11-11"),
    ("chinese", "Claude Opus 5", 1500.0, 1, 9000, "2026-11-11"),  # no axis: skipped
    ("overall", "Claude Opus 5", 1520.0, 1, 99000, "2026-11-11"),  # blend: skipped
]


@pytest.fixture(autouse=True)
def arena(monkeypatch):
    monkeypatch.setattr(li, "_fetch_rows", lambda *a, **k: list(ARENA_ROWS))


def _keys(con):
    return {r["model"] for r in con.execute(
        "select model from model_capabilities where source='lmarena'")}


# ── the canonicalisation rule ──────────────────────────────────────────────

def test_only_resolvable_names_become_capability_rows(db):
    out = li.ingest(db)
    assert out["rows"] == 2 and out["matched"] == 2, (
        "one row for Claude Opus 5 (coding) and one for gpt-5.5 (math)")
    assert _keys(db) == {"claude-opus-5", "gpt-5.5"}, (
        "an unmatched arena name must never become a capability key")


def test_unmatched_name_goes_to_the_identity_queue(db):
    out = li.ingest(db)
    assert out["queued"] == 1
    q = db.execute("select raw, source, kind from mi_identity_queue").fetchall()
    assert len(q) == 1 and q[0]["raw"] == "Mystery Model X"
    assert q[0]["source"] == "lmarena" and q[0]["kind"] == "capability_name"


def test_every_written_key_is_joinable_by_the_reader(db):
    """The property that was violated before: no dead rows. A row the read path
    cannot find is worse than no row, because it looks like coverage."""
    li.ingest(db)
    from src.api.model_identity_queue import resolve_canonical
    for r in db.execute("select distinct model from model_capabilities"):
        assert resolve_canonical(db, r["model"]), (
            f"{r['model']!r} is stored but unreachable")


def test_categories_map_onto_gateway_axes(db):
    out = li.ingest(db)
    assert out["categories"] == ["coding", "math"], (
        "language/industry/overall categories must be skipped")


def test_an_alias_or_served_id_also_brings_an_arena_name_in(db):
    """Resolution is not slug-only: an operator-declared name is a route too."""
    db.execute(
        "INSERT INTO mi_model_names (model_id, source_id, name) VALUES (2,"
        " (SELECT source_id FROM mi_sources WHERE slug='operator'), 'Mystery Model X')")
    db.commit()
    out = li.ingest(db)
    assert out["rows"] == 3 and out["queued"] == 0
    assert _keys(db) == {"claude-opus-5", "gpt-5.5"}


def test_latest_snapshot_beats_superseded_rows(db):
    li.ingest(db)
    rows = db.execute(
        "select raw_score, release_label from model_capabilities"
        " where source='lmarena' and model='claude-opus-5' and benchmark_category='coding'"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["raw_score"] == 1530.0
    assert rows[0]["release_label"] == "2026-11-11"


def test_score_is_percentile_and_raw_score_is_elo(db):
    li.ingest(db)
    row = db.execute(
        "select score, raw_score from model_capabilities"
        " where source='lmarena' and model='gpt-5.5'").fetchone()
    assert row["raw_score"] == 1510.0
    assert row["score"] == 100.0  # rank 1 of 1 in math


def test_livebench_rows_are_never_touched(db):
    """The provenance rule: a source never overwrites another source's rows."""
    li.ingest(db)
    row = db.execute(
        "select score, source, updated_at from model_capabilities where source='livebench'"
    ).fetchone()
    assert row["score"] == 88.0 and row["updated_at"] == "2026-10-30"


def test_reruns_refresh_not_duplicate(db):
    li.ingest(db)
    n1 = db.execute(
        "select count(*) from model_capabilities where source='lmarena'").fetchone()[0]
    li.ingest(db)
    n2 = db.execute(
        "select count(*) from model_capabilities where source='lmarena'").fetchone()[0]
    assert n1 == n2 == 2, "a re-run refreshes its own rows and adds none"


def test_queued_repeats_do_not_duplicate(db):
    li.ingest(db)
    li.ingest(db)
    assert db.execute("select count(*) from mi_identity_queue").fetchone()[0] == 1
    assert db.execute("select occurrences from mi_identity_queue").fetchone()[0] == 2


def test_dry_run_writes_nothing(db):
    out = li.ingest(db, dry_run=True)
    assert out["matched"] == 2 and out["rows"] == 0 and out["queued"] == 1
    assert db.execute("select count(*) from mi_identity_queue").fetchone()[0] == 0
    assert db.execute(
        "select count(*) from model_capabilities where source='lmarena'").fetchone()[0] == 0


def test_accepting_a_queued_name_makes_later_rows_land_on_the_canonical_model(db):
    """End to end: the observation queues, the operator links it, and the next ingest
    writes the score against the canonical model instead of a dead name."""
    li.ingest(db)
    from src.api.model_identity_queue import accept, list_pending
    item = list_pending(db)[0]
    accept(db, item["id"], slug="gpt-5.5", note="operator linked")
    li.ingest(db)
    assert _keys(db) == {"claude-opus-5", "gpt-5.5"}
    assert db.execute(
        "select count(*) from model_capabilities where model='gpt-5.5'"
    ).fetchone()[0] == 2, "the newly linked name now lands on gpt-5.5"
    assert db.execute(
        "select count(*) from mi_identity_queue where status='pending'").fetchone()[0] == 0
