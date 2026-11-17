"""The schema and settings surface Arena routing needs — TDD contract.

Three small things that are easy to get wrong and expensive to notice late:

* ``routing_route_by`` accepts the three tier names, and the OLD value ``rules`` keeps
  working — an estate that has ``rules`` stored must not silently start routing by
  intent because the vocabulary was renamed;
* ``routing_decisions`` can carry ``base_intent`` and ``arena_board``, so a decision
  recorded today can be explained after the label space changes (alembic 041);
* ``get_model_score`` can answer for an arena board without inventing a number for a
  model the board never ranked (§16 E8).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from src.api import arena_routing as ar
from src.api.cost_cache import SettingsStore
from src.api.router import CapabilityRouter


# ── route_by vocabulary ───────────────────────────────────────────────────────

def _store(tmp_path):
    from src.api.models import Base, get_engine
    engine = get_engine(str(tmp_path / "settings.db"))
    Base.metadata.create_all(engine)
    return SettingsStore(engine)


def test_route_by_accepts_the_three_tiers(tmp_path):
    s = _store(tmp_path)
    for value in ("static", "intent", "arena"):
        s.set_route_by(value)
        assert s.get_route_by() == value, value


def test_route_by_legacy_rules_still_reads_as_static(tmp_path):
    """A stored `rules` is the static tier under its old name."""
    s = _store(tmp_path)
    s.set("routing_route_by", "rules")
    assert s.get_route_by() == "static"


def test_route_by_writing_rules_stores_static(tmp_path):
    s = _store(tmp_path)
    s.set_route_by("rules")
    assert s.get("routing_route_by") == "static"


def test_route_by_rejects_an_unknown_tier(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        s.set_route_by("dynamic-ish")


def test_route_by_per_profile_override_wins(tmp_path):
    s = _store(tmp_path)
    s.set_route_by("static")
    s.set_route_by("arena", profile="p1")
    assert s.get_route_by(profile="p1") == "arena"
    assert s.get_route_by(profile="p2") == "static"


def test_route_by_default_is_intent(tmp_path):
    assert _store(tmp_path).get_route_by() == "intent"


# ── the recording columns (alembic 041) ───────────────────────────────────────

SCHEMA_NO_ARENA_COLS = """
CREATE TABLE routing_decisions (
    id INTEGER PRIMARY KEY, ts VARCHAR, profile VARCHAR, task VARCHAR,
    policy VARCHAR, action VARCHAR, provider VARCHAR, model VARCHAR,
    score FLOAT, rules_json TEXT, from_provider VARCHAR, from_model VARCHAR,
    note TEXT, path VARCHAR, keyword VARCHAR, intent_text TEXT,
    semantic_json TEXT, min_score FLOAT, sem_available BOOLEAN,
    conversation_id TEXT, l4_json TEXT, from_deployment_id INTEGER,
    to_deployment_id INTEGER, registry_version INTEGER,
    taxonomy_id TEXT, taxonomy_version INTEGER);
"""


def _migration_041():
    """Load the migration file.

    `alembic` is a dependency of the IMAGE, not of this test host, so `alembic.op` is
    stubbed: every assertion below calls ``upgrade(conn)`` with an explicit connection,
    which is the branch that never touches ``op``.
    """
    import importlib.util
    import sys
    import types
    if "alembic" not in sys.modules:
        stub = types.ModuleType("alembic")
        stub.op = types.SimpleNamespace(get_bind=lambda: None)
        sys.modules["alembic"] = stub
    path = os.path.join(os.path.dirname(__file__), "..", "alembic", "versions",
                        "041_routing_decision_label_space.py")
    spec = importlib.util.spec_from_file_location("mig041", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_041_adds_both_columns():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_NO_ARENA_COLS)
    mod = _migration_041()
    assert mod.revision == "041" and mod.down_revision == "040"
    with con:
        mod.upgrade(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(routing_decisions)")}
    assert {"base_intent", "arena_board"} <= cols


def test_migration_041_is_idempotent():
    """Re-running on an upgraded DB must not raise — upgrades get retried."""
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA_NO_ARENA_COLS)
    mod = _migration_041()
    with con:
        mod.upgrade(con)
    with con:
        mod.upgrade(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(routing_decisions)")}
    assert {"base_intent", "arena_board"} <= cols


# ── the model keeps the columns, or the value never reaches the table ─────────

def test_routing_decision_model_has_the_new_columns():
    from src.api.models import RoutingDecision
    cols = set(RoutingDecision.__table__.columns.keys())
    assert {"base_intent", "arena_board"} <= cols


# ── get_model_score can answer for a board — without inventing a number ───────

def _arena_registry():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from src.api.models import get_engine, Base
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE arena_boards (
            board TEXT PRIMARY KEY, family TEXT NOT NULL, unit TEXT NOT NULL,
            rows INTEGER NOT NULL DEFAULT 0, models_matched INTEGER NOT NULL DEFAULT 0,
            published TEXT, first_seen TEXT NOT NULL, last_polled_at TEXT NOT NULL);
        CREATE TABLE arena_board_rows (
            id INTEGER PRIMARY KEY, board TEXT NOT NULL, model_slug TEXT NOT NULL,
            value REAL NOT NULL, unit TEXT NOT NULL, rank INTEGER, n INTEGER, lab TEXT,
            effort TEXT NOT NULL DEFAULT '', published TEXT, polled_at TEXT NOT NULL,
            UNIQUE (board, model_slug, effort));
    """)
    for b in ("coding", "math"):
        con.execute("INSERT INTO arena_boards (board, family, unit, rows,"
                    " models_matched, first_seen, last_polled_at)"
                    " VALUES (?,'text','elo',0,0,'x','x')", (b,))
    # rank 1 of 10 -> arena standing 1.0, deliberately NOT the 0.5 default, so a test
    # can tell "the board answered" from "nothing answered".
    for slug, value, rank in (("model-beta", 1509.4, 1), ("model-alpha", 1506.3, 3)):
        con.execute("INSERT INTO arena_board_rows (board, model_slug, value, unit, rank, n,"
                    " effort, polled_at) VALUES ('coding',?,?,'elo',?,10,'','x')",
                    (slug, value, rank))
    con.commit()
    con.close()
    return path


def test_get_model_score_answers_for_a_board_the_matrix_does_not_cover():
    path = _arena_registry()
    try:
        r = CapabilityRouter(enabled=True, db_path=path)
        got = r.get_model_score("model-beta", "coding")
        assert 0.0 < got <= 1.0
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass


def test_get_model_score_for_a_board_does_not_invent_a_value_for_an_unranked_model():
    """§16 E8 — an unmeasured model falls through to the ordinary default.

    The sharp version of this assertion matters: the board value here is 1.0 and the
    default is 0.5, so "got the default" and "got an arena number" are distinguishable
    numbers rather than a coincidence of both being 0.5.
    """
    path = _arena_registry()
    try:
        r = CapabilityRouter(enabled=True, db_path=path)
        con = sqlite3.connect(path)
        assert ar.score(con, "coding", "model-never-ranked") is None
        assert r.get_model_score("model-beta", "coding") == 1.0        # from the board
        assert r.get_model_score("model-never-ranked", "coding") == 0.5  # the default
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass


def test_get_model_score_is_unchanged_for_a_normal_task():
    path = _arena_registry()
    try:
        r = CapabilityRouter(enabled=True, db_path=path)
        # A task with no matrix rows and no board of that name keeps the old default.
        assert r.get_model_score("model-beta", "code_generation") == 0.5
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass
