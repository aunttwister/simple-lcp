"""Unit tests for the LMArena capability ingester (CUR model-intel P3).

No network: ``_fetch_rows`` is monkeypatched to a fixed leaderboard snapshot.
Pinned contract: category mapping, canonical-slug matching, livebench rows
untouched (user-edited values immune), idempotent re-runs, provenance fields.
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
        CREATE TABLE mi_models (model_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT);
        CREATE TABLE model_capabilities (id INTEGER PRIMARY KEY, model TEXT, task_type TEXT,
          score REAL, source TEXT, benchmark_category TEXT, raw_score REAL,
          updated_at TEXT, release_label TEXT);
    """)
    con.execute("INSERT INTO mi_models VALUES (1, 'claude-opus-5', 'Claude Opus 5')")
    con.execute("INSERT INTO mi_models VALUES (2, 'gemini-4-carbon', 'Gemini 4 Carbon')")
    con.execute("INSERT INTO mi_models VALUES (3, 'claude-opus-5-thinking',"
                " 'Claude Opus 5 Thinking')")  # norm differs: unambiguous
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
    ("math", "Claude Opus 5", 1510.0, 1, 15000, "2026-11-11"),
    ("chinese", "Claude Opus 5", 1500.0, 1, 9000, "2026-11-11"),  # no axis: skipped
    ("overall", "Claude Opus 5", 1520.0, 1, 99000, "2026-11-11"),  # blend: skipped
]


@pytest.fixture(autouse=True)
def arena(monkeypatch):
    monkeypatch.setattr(li, "_fetch_rows", lambda *a, **k: list(ARENA_ROWS))


def test_categories_map_onto_gateway_axes(db):
    out = li.ingest(db)
    assert out["categories"] == ["coding", "math"], (
        "language/industry/overall categories must be skipped")


def test_canonical_slug_match_wins_and_dedupes(db):
    """'Claude Opus 5' matches slug 'claude-opus-5' ('...-thinking' normalises
    differently, so no collision). Keys are slugs, never ids."""
    out = li.ingest(db)
    assert out["matched"] == 2  # coding + math rows for Claude Opus 5
    keys = {r[0] for r in db.execute(
        "select model from model_capabilities where source='lmarena'")}
    assert keys == {"claude-opus-5", "Mystery Model X"}


def test_latest_snapshot_beats_superseded_rows(db):
    """The 2026-11-04 coding row is superseded by the 2026-11-11 one."""
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
        " where source='lmarena' and model='Mystery Model X'").fetchone()
    assert row["raw_score"] == 1400.0
    assert row["score"] == 50.0  # rank 2 of 2 in coding


def test_livebench_rows_are_never_touched(db):
    """The operator's provenance rule: a poll never overwrites another source."""
    li.ingest(db)
    row = db.execute(
        "select score, source, updated_at from model_capabilities where source='livebench'"
    ).fetchone()
    assert row["score"] == 88.0 and row["updated_at"] == "2026-10-30"


def test_reruns_refresh_not_duplicate(db):
    """3 mapped rows (coding x2 models + math) — a re-run refreshes, never grows."""
    li.ingest(db)
    n1 = db.execute(
        "select count(*) from model_capabilities where source='lmarena'").fetchone()[0]
    li.ingest(db)
    n2 = db.execute(
        "select count(*) from model_capabilities where source='lmarena'").fetchone()[0]
    assert n1 == n2 == 3


def test_dry_run_writes_nothing(db):
    out = li.ingest(db, dry_run=True)
    assert out["matched"] == 2 and out["rows"] == 0
    assert db.execute(
        "select count(*) from model_capabilities where source='lmarena'"
    ).fetchone()[0] == 0
