"""Arena routing inside the router — TDD contract for the integration.

Operator, 2026-11-17: "On every message. … Also dont forget about hard rules that can
be applied per profile. Same as it can be done now."

The engine is tested in ``test_arena_routing.py``. This file pins how the router uses
it, and the four things that must not regress:

1. **per message** — the board is classified from THIS request's text, so two messages
   in one conversation can take different models (§16 E6);
2. **hard rules still act, per profile** — `block` removes the model from the walk,
   `prefer` overrides the ranking, and both are read from the PROFILE's rule list;
3. **the static chain stands** whenever the board cannot be walked — no board, empty
   pool slice, no embedder. Returning None *is* the fallback: the caller keeps the
   chain it already has;
4. **the decision is recorded** with the board that produced it and the base intent,
   so a historical pick can be explained.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

import src.api.arena_routing as ar
from src.api.router import CapabilityRouter
from tests import _model_names_fixtures
from tests.test_arena_routing import ARENA_DDL, BOARDS, ROWS


@pytest.fixture
def arena_db():
    """A registry DB carrying arena tables AND a usable capability matrix."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from src.api.models import get_engine, Base
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    con = sqlite3.connect(path)
    _model_names_fixtures.mi_schema(con)
    con.executescript(ARENA_DDL)
    # Portfolios for the models the arena tables rank, so identity resolves.
    for i, slug in enumerate(("model-alpha", "model-beta", "model-far", "model-omega"),
                              start=1):
        con.execute("INSERT OR IGNORE INTO mi_models (model_id, slug, display_name,"
                    " status) VALUES (?,?,?,'active')", (i, slug, slug))
        con.execute("INSERT OR IGNORE INTO mi_deployments (deployment_id, endpoint_id,"
                    " model_id, served_id, status) VALUES (?,1,?,?,'active')",
                    (i, i, slug))
    for board, family, unit in BOARDS:
        con.execute("INSERT OR REPLACE INTO arena_boards (board, family, unit, rows,"
                    " models_matched, first_seen, last_polled_at)"
                    " VALUES (?,?,?,0,0,'2026-11-16','2026-11-16')", (board, family, unit))
    for board, slug, value, rank, effort in ROWS:
        con.execute("INSERT OR REPLACE INTO arena_board_rows (board, model_slug, value,"
                    " unit, rank, effort, polled_at) VALUES (?,?,?,'elo',?,?,'2026-11-16')",
                    (board, slug, value, rank, effort))
    con.commit()
    con.close()
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


CHAIN = [
    {"provider": "alpha-prov", "model": "model-beta"},
    {"provider": "alpha-prov", "model": "model-far"},
    {"provider": "beta-prov", "model": "model-alpha"},
]


class FakeSettings(dict):
    """The settings store, reduced to what routing reads."""

    def __init__(self, route_by="arena", rules=None, enabled=True, policy="eager",
                 min_score=0.0, gate=0.0, per_profile=None):
        super().__init__()
        self._route_by, self._rules = route_by, rules or []
        self._enabled, self._policy = enabled, policy
        self._min_score, self._gate = min_score, gate
        self._per_profile = per_profile or {}

    def get_route_by(self, default="intent", profile=None):
        if profile and profile in self._per_profile:
            return self._per_profile[profile].get("route_by", self._route_by)
        return self._route_by

    def get_routing_rules(self, default=None, profile=None):
        if profile and profile in self._per_profile:
            return self._per_profile[profile].get("rules", self._rules)
        return self._rules

    def get_routing_enabled(self, default=None, profile=None):
        return self._enabled

    def get_routing_policy(self, default="eager", profile=None):
        return self._policy

    def get_routing_min_score(self, default=0.0, profile=None):
        return self._min_score

    def get_intent_margin_gate(self, default=0.0, profile=None):
        return self._gate


def _wire(monkeypatch, settings, board, scores=None):
    """Point the router at *settings* and make the judge answer *board*."""
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: settings)
    monkeypatch.setattr(
        ar, "pick_board",
        lambda text, **kw: (board, scores if scores is not None
                            else ([(board, 0.9)] if board else [])))


def _messages(text="refactor this python module"):
    return [{"role": "user", "content": text}]


# ── 1. per message ────────────────────────────────────────────────────────────

def test_arena_routes_to_the_top_ranked_pool_model_on_the_classified_board(
        arena_db, monkeypatch):
    """coding ranks alpha=3, beta=2 (plain rows). The pool holds beta and alpha."""
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, FakeSettings(), "coding")
    out = r.select_step(_messages(), chain=CHAIN)
    assert out is not None
    assert out[0]["model"] == "model-beta"


def test_arena_decides_per_message_not_per_conversation(arena_db, monkeypatch):
    """Two messages, two boards, two models — his explicit answer to §16 E6."""
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    seen = []

    def pick(text, **kw):
        board = "coding" if "code" in text else "math"
        seen.append(board)
        return board, [(board, 0.9)]

    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: FakeSettings())
    monkeypatch.setattr(ar, "pick_board", pick)

    first = r.select_step(_messages("please write code"), chain=CHAIN)
    second = r.select_step(_messages("now solve this equation"), chain=CHAIN)
    assert seen == ["coding", "math"]
    assert first[0]["model"] == "model-beta"    # coding: beta ranks 2
    assert second[0]["model"] == "model-alpha"  # math:   alpha ranks 1


def test_arena_reports_the_board_it_used(arena_db, monkeypatch):
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, FakeSettings(), "coding")
    r.select_step(_messages(), chain=CHAIN)
    assert r.recent_decisions(1)[0]["arena_board"] == "coding"


# ── 2. hard rules, per profile ────────────────────────────────────────────────

def test_block_rule_removes_the_model_from_the_walk(arena_db, monkeypatch):
    """A per-profile block must make the walk skip to the next rank."""
    rules = [{"action": "block", "provider": "*", "model": "model-beta"}]
    settings = FakeSettings(rules=[], per_profile={"p1": {"rules": rules}})
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, settings, "coding")
    out = r.select_step(_messages(), chain=CHAIN, profile="p1")
    assert out[0]["model"] != "model-beta", "blocked model was still chosen"


def test_prefer_rule_overrides_the_board_ranking(arena_db, monkeypatch):
    """A win by rule is a win — the ranking does not get a second vote."""
    rules = [{"action": "prefer", "provider": "beta-prov", "model": "model-alpha"}]
    settings = FakeSettings(rules=[], per_profile={"p1": {"rules": rules}})
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, settings, "coding")
    out = r.select_step(_messages(), chain=CHAIN, profile="p1")
    assert out[0]["model"] == "model-alpha"


def test_rules_do_not_leak_across_profiles(arena_db, monkeypatch):
    """The block is p1's. p2 must still get the ranked pick."""
    rules = [{"action": "block", "provider": "*", "model": "model-beta"}]
    settings = FakeSettings(rules=[], per_profile={"p1": {"rules": rules}})
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, settings, "coding")
    out = r.select_step(_messages(), chain=CHAIN, profile="p2")
    assert out[0]["model"] == "model-beta"


# ── 3. the static chain stands ────────────────────────────────────────────────

def test_no_board_means_the_static_chain_stands(arena_db, monkeypatch):
    """Below the gate → no board → None, i.e. the caller keeps its chain."""
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, FakeSettings(), None, scores=[("coding", 0.11)])
    assert r.select_step(_messages(), chain=CHAIN) is None


def test_board_with_no_pool_model_means_the_static_chain_stands(arena_db, monkeypatch):
    """§16 E4 — nothing servable on the board is a fallback, not an exception."""
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, FakeSettings(), "coding")
    chain = [{"provider": "p", "model": "model-not-ranked"}]   # outside the board
    assert r.select_step(_messages(), chain=chain) is None


def test_missing_embedder_means_the_static_chain_stands_and_is_recorded(
        arena_db, monkeypatch):
    """§16 E7 — no embedder must never fall back to keyword→board guessing."""
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: FakeSettings())
    monkeypatch.setattr(ar, "pick_board", lambda text, **kw: (None, []))
    assert r.select_step(_messages(), chain=CHAIN) is None
    note = (r.recent_decisions(1)[0].get("note") or "")
    assert "arena" in note.lower()


def test_unknown_board_label_falls_back(arena_db, monkeypatch):
    """A board the store does not know cannot be walked."""
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, FakeSettings(), "board-that-does-not-exist")
    assert r.select_step(_messages(), chain=CHAIN) is None


# ── route_by wiring ───────────────────────────────────────────────────────────

def test_route_by_arena_is_honoured(arena_db, monkeypatch):
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, FakeSettings(route_by="arena"), "coding")
    assert r._route_by("p1", None) == "arena"


def test_route_by_static_replaces_rules(arena_db):
    """`rules` is the old name for the static tier; both must read as static."""
    assert CapabilityRouter._normalize_route_by("rules") == "static"
    assert CapabilityRouter._normalize_route_by("static") == "static"


def test_route_by_rejects_an_unknown_value(arena_db):
    assert CapabilityRouter._normalize_route_by("nonsense") is None


def test_route_by_defaults_to_intent_when_the_store_is_unavailable(
        arena_db, monkeypatch):
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: None)
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    assert r._route_by("p1", None) == "intent"


def test_route_by_unknown_stored_value_falls_back_to_intent(arena_db, monkeypatch):
    monkeypatch.setattr("src.api.cost_cache.get_settings",
                        lambda: FakeSettings(route_by="nonsense"))
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    assert r._route_by("p1", None) == "intent"


# ── 4. the recording ──────────────────────────────────────────────────────────

def test_decision_records_the_base_intent_as_well_as_the_board(arena_db, monkeypatch):
    """What the message was judged to be, and where that landed (§8)."""
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, FakeSettings(), "coding")
    r.select_step(_messages(), chain=CHAIN)
    d = r.recent_decisions(1)[0]
    assert d["arena_board"] == "coding"
    assert d["base_intent"] == "coding"


def test_decision_carries_the_score_vector_the_judge_produced(arena_db, monkeypatch):
    r = CapabilityRouter(enabled=True, db_path=arena_db)
    _wire(monkeypatch, FakeSettings(), "coding",
          scores=[("coding", 0.9), ("math", 0.4)])
    r.select_step(_messages(), chain=CHAIN)
    d = r.recent_decisions(1)[0]
    assert d["semantic_json"] and "coding" in d["semantic_json"]
    assert d["path"] == "arena"
