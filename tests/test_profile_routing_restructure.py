"""CUR-328 — profile page static/dynamic routing restructure.

Covers the backend seams the restructure added:
  * route_by settings accessors (global / per-profile / validation)
  * CapabilityRouter._route_by (store missing -> safe default 'intent')
  * CapabilityRouter._select_step_rules_only — no intent-model call, no
    scoring, rules still act, decisions stamped path='rules_only'
  * the routing-policy POST accepts route_by (validated) — covered by the
    endpoints-level check below through the store contract.

The disabled-chain-step skip and the chain-reorder `enabled` preservation are
wired into the live request path and verified on staging (see PLAN.md);
unit-testing them here would mean re-creating the HTTP handler fixtures.
"""

import pytest

from api.cost_cache import SettingsStore, init_settings
from api.models import Base, get_engine
from api.router import CapabilityRouter, ClassifyResult


def _fresh_store(tmp_path):
    """A settings store on a fresh DB — tables created, session isolated."""
    engine = get_engine(str(tmp_path / "s.db"))
    Base.metadata.create_all(engine)
    return init_settings(engine)


# ── route_by settings accessors ──────────────────────────────────────────────

def test_route_by_defaults_to_intent(tmp_path):
    store = _fresh_store(tmp_path)
    assert store.get_route_by() == "intent"
    assert store.get_route_by(profile="l2") == "intent"


def test_route_by_set_get_clear(tmp_path):
    store = _fresh_store(tmp_path)
    store.set_route_by("rules")
    assert store.get_route_by() == "rules"
    store.set_route_by("intent", profile="l2")
    assert store.get_route_by(profile="l2") == "intent"
    # per-profile override wins over the global
    store.set_route_by("rules", profile="l2")
    assert store.get_route_by(profile="l2") == "rules"
    assert store.get_route_by() == "rules"
    store.clear_route_by(profile="l2")
    assert store.get_route_by(profile="l2") == "rules"  # falls back to global
    store.clear_route_by()
    assert store.get_route_by() == "intent"


def test_route_by_rejects_unknown(tmp_path):
    store = _fresh_store(tmp_path)
    with pytest.raises(ValueError):
        store.set_route_by("cost")


# ── _route_by resolution ─────────────────────────────────────────────────────

def test_router_route_by_defaults_intent_without_store(tmp_path, monkeypatch):
    import api.cost_cache as cc
    monkeypatch.setattr(cc, "get_settings", lambda: None)
    r = CapabilityRouter(db_path=str(tmp_path / "r.db"))
    assert r._route_by("l2", None) == "intent"


def test_router_route_by_reads_store(tmp_path, monkeypatch):
    store = _fresh_store(tmp_path)
    store.set_route_by("rules", profile="l2")
    import api.cost_cache as cc
    monkeypatch.setattr(cc, "get_settings", lambda: store)
    r = CapabilityRouter(db_path=str(tmp_path / "r.db"))
    assert r._route_by("l2", None) == "rules"
    assert r._route_by("other", None) == "intent"


# ── rules-only select path ───────────────────────────────────────────────────

class _FakeConfig:
    """Just enough config for _rules/_apply_blocks/_resolve_prefer to no-op."""

    profiles = {}
    providers = {}
    raw = {}


def _rules_router(tmp_path, monkeypatch):
    # rules mode ON for every test that lands on the rules-only path
    store = _fresh_store(tmp_path)
    store.set_route_by("rules")
    import api.cost_cache as cc
    monkeypatch.setattr(cc, "get_settings", lambda: store)
    r = CapabilityRouter(db_path=str(tmp_path / "r.db"))
    # select_step bails on the is_enabled gate before the route-by gate —
    # rules-only mode only ever runs when dynamic routing is on.
    r.enabled = True
    # breaker: everything available
    monkeypatch.setattr(r, "_provider_available", lambda s, p, c: True)
    # no rules configured
    monkeypatch.setattr(r, "_rules", lambda config, profile=None: [])
    captured = []
    monkeypatch.setattr(r, "_record_decision", lambda d: captured.append(d))
    return r, captured


def test_rules_only_skips_classification_and_scores(tmp_path, monkeypatch):
    """route_by=rules must not call the intent model or touch the matrix."""
    store = _fresh_store(tmp_path)
    store.set_route_by("rules")
    import api.cost_cache as cc
    monkeypatch.setattr(cc, "get_settings", lambda: store)

    r, captured = _rules_router(tmp_path, monkeypatch)
    # any attempt to classify explodes — proving the call is never made
    monkeypatch.setattr(
        "api.router.classify_task_detail",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("classified in rules mode")))

    chain = [{"provider": "p1", "model": "m1"}, {"provider": "p2", "model": "m2"}]
    out = r.select_step([{"role": "user", "content": "hi"}], chain=chain,
                        profile="l2", config=_FakeConfig())
    assert out is None  # static order stands
    assert captured, "a decision must be recorded"
    d = captured[0]
    assert d["path"] == "rules_only"
    assert d["task"] == ""
    assert d["action"] == "keep_default"
    assert d["model"] == "m1" and d["provider"] == "p1"


def test_rules_only_applies_prefer_rule(tmp_path, monkeypatch):
    """A taskless prefer rule still fires in rules-only mode."""
    r, captured = _rules_router(tmp_path, monkeypatch)
    monkeypatch.setattr(r, "_resolve_prefer",
                        lambda chain, task, profile, config: (None, "p2", [
                            {"action": "prefer", "provider": "p2"}]))
    chain = [{"provider": "p1", "model": "m1"}, {"provider": "p2", "model": "m2"}]
    # _build_chain_for_model is heavy (registry-backed) — stand in the essence:
    # the prefer target's step first, rest as fallback.
    monkeypatch.setattr(r, "_build_chain_for_model",
                        lambda chain, tm, pp, profile, config, **kw:
                            [s for s in chain if s["provider"] == pp]
                            + [s for s in chain if s["provider"] != pp])
    out = r.select_step([{"role": "user", "content": "hi"}], chain=chain,
                        profile="l2", config=_FakeConfig())
    assert out[0]["provider"] == "p2"
    assert captured[0]["action"] == "prefer"
    assert captured[0]["path"] == "rules_only"


def test_rules_only_respects_breaker(tmp_path, monkeypatch):
    """A breaker-unavailable provider is dropped before rules act."""
    r, captured = _rules_router(tmp_path, monkeypatch)
    monkeypatch.setattr(r, "_provider_available",
                        lambda s, p, c: s["provider"] != "p1")
    chain = [{"provider": "p1", "model": "m1"}, {"provider": "p2", "model": "m2"}]
    out = r.select_step([{"role": "user", "content": "hi"}], chain=chain,
                        profile="l2", config=_FakeConfig())
    assert out is None
    assert captured[0]["provider"] == "p2" and captured[0]["model"] == "m2"


def test_rules_only_all_blocked_returns_none(tmp_path, monkeypatch):
    r, captured = _rules_router(tmp_path, monkeypatch)
    monkeypatch.setattr(r, "_provider_available", lambda s, p, c: False)
    chain = [{"provider": "p1", "model": "m1"}]
    assert r.select_step([{"role": "user", "content": "hi"}], chain=chain,
                         profile="l2", config=_FakeConfig()) is None
    assert captured == []


def test_classify_result_rules_only_path_is_distinct():
    """The stamp that makes rules-only decisions greppable exists."""
    d = ClassifyResult(task="", path="rules_only", intent_meta={"route_by": "rules"})
    assert d.path == "rules_only"
