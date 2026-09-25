"""L2's intent margin gate — R6, the layer the plan calls the highest-value change.

The plan's spec (README, "The deterministic router (R6)"):

    L2  PER-PROFILE INTENT   score ONLY against this profile's declared intents (R5)
                             gate: (top1 - top2) < intent_margin_gate -> DO NOT REORDER

and invariant 3: *margin < gate ⇒ output == static chain head, always*.

`select_step` returns None to mean "leave the static chain alone", so the invariant
is "the gate fired ⇒ None". These tests pin the gate itself, the two off switches
(absent field, gate 0.0), and the layer order (an explicit rule outranks the gate).
"""
import os
import tempfile

import pytest

import src.api.router as router_mod
from src.api.config import ConfigError, validate_profile_fields
from src.api.models import Base
from src.api.models import get_engine
from src.api.router import CapabilityRouter, ClassifyResult


@pytest.fixture
def registry_db():
    """A fresh DB seeded with the default model registry."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    from src.api.seed_capabilities import seed_model_registry
    seed_model_registry(path)
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


class Cfg:
    """A duck-typed config: whatever a profile declares, nothing else."""

    def __init__(self, profiles=None):
        self.profiles = profiles or {}
        self.dynamic_routing = {"policy": "explore", "min_score": 0.0}


def detail_with(semantic, task=None):
    """A ClassifyResult whose semantic top-N is known exactly."""
    return ClassifyResult(
        task=task or (semantic[0][0] if semantic else "fallback"),
        path="semantic",
        semantic=list(semantic),
        min_score=0.0,
        sem_available=True,
    )


@pytest.fixture
def routed(monkeypatch):
    """Drive the gate without the rest of the router's plumbing in the way."""
    def _setup(semantic, profiles, *, prefer=None):
        r = CapabilityRouter(enabled=True)
        monkeypatch.setattr(router_mod, "classify_task_detail",
                            lambda *a, **k: detail_with(semantic))
        monkeypatch.setattr(r, "is_enabled", lambda *a, **k: True)
        monkeypatch.setattr(r, "_effective_policy", lambda *a, **k: ("explore", 0.0))
        monkeypatch.setattr(r, "_provider_available", lambda *a, **k: True)
        if prefer is not None:
            monkeypatch.setattr(r, "_resolve_prefer", lambda *a, **k: prefer)
        r._decisions = []
        return r
    return _setup


CHAIN = [{"provider": "deepseek", "model": "deepseek-v4-pro"},
         {"provider": "opencode", "model": "deepseek-v4-flash"}]


# ── the field ─────────────────────────────────────────────────────────────────

def test_absent_means_off():
    """Off is 0.0. A profile that never heard of the gate is routed as before."""
    assert validate_profile_fields("p", {})["intent_margin_gate"] == 0.0


def test_none_means_off():
    assert validate_profile_fields("p", {"intent_margin_gate": None})["intent_margin_gate"] == 0.0


def test_zero_means_off():
    assert validate_profile_fields("p", {"intent_margin_gate": 0})["intent_margin_gate"] == 0.0


def test_a_fraction_is_kept():
    assert validate_profile_fields("p", {"intent_margin_gate": 0.05})["intent_margin_gate"] == 0.05


def test_the_whole_range_is_legal():
    for v in (0.0, 0.25, 0.5, 1.0, 1):
        assert validate_profile_fields("p", {"intent_margin_gate": v})["intent_margin_gate"] == float(v)


@pytest.mark.parametrize("bad", ["0.5", "half", -0.1, 1.5, 2, [0.5], {"v": 0.5}, float("nan")])
def test_junk_is_refused(bad):
    """A gate that cannot mean anything must not be stored as if it did."""
    with pytest.raises(ConfigError):
        validate_profile_fields("p", {"intent_margin_gate": bad})


def test_true_is_refused_not_read_as_one():
    """bool is an int subclass, so True would silently mean gate=1.0 (never reorder)."""
    with pytest.raises(ConfigError):
        validate_profile_fields("p", {"intent_margin_gate": True})


# ── the margin ────────────────────────────────────────────────────────────────

def test_margin_is_top1_minus_top2():
    m, t1, t2 = CapabilityRouter._intent_margin(detail_with([("unit_tests", 0.9), ("planning", 0.4)]))
    assert m == pytest.approx(0.5)
    assert (t1, t2) == ("unit_tests", "planning")


def test_no_semantic_means_no_margin():
    """A keyword/token classified request has no score to weigh — never gated."""
    assert CapabilityRouter._intent_margin(ClassifyResult(task="x", path="casual")) == (None, None, None)


def test_one_candidate_is_not_a_coin_flip():
    assert CapabilityRouter._intent_margin(detail_with([("planning", 0.8)])) == (None, None, None)


def test_a_lone_unscored_candidate_is_not_a_margin():
    assert CapabilityRouter._intent_margin(detail_with([("planning", 0.8), ("x", None)])) == (None, None, None)


# ── the gate ──────────────────────────────────────────────────────────────────

def test_a_thin_margin_does_not_reorder(routed):
    r = routed([("unit_tests", 0.62), ("planning", 0.60)],
               {"p": {"intent_margin_gate": 0.1}})
    assert r.select_step([{"role": "user", "content": "hi"}], chain=CHAIN,
                         profile="p", config=Cfg({"p": {"intent_margin_gate": 0.1}})) is None


def test_a_thin_margin_is_recorded_so_it_can_be_replayed(routed):
    cfg = Cfg({"p": {"intent_margin_gate": 0.1}})
    r = routed([("unit_tests", 0.62), ("planning", 0.60)], {"p": {"intent_margin_gate": 0.1}})
    r.select_step([{"role": "user", "content": "hi"}], chain=CHAIN, profile="p", config=cfg)
    d = r._decisions[-1]
    assert d["action"] == "margin_gate"
    assert d["score"] == pytest.approx(0.02, abs=1e-3)   # score carries the margin
    assert d["model"] == CHAIN[0]["model"]               # the head that stood
    assert "margin 0.020 < gate 0.1" in d["note"]        # the gate, which has no column
    assert "unit_tests vs planning" in d["note"]


def test_the_rules_note_survives_the_gate_note(routed, monkeypatch):
    """The gate appends to the rules audit rather than replacing it."""
    cfg = Cfg({"p": {"intent_margin_gate": 0.1}})
    r = routed([("unit_tests", 0.62), ("planning", 0.60)], {"p": {"intent_margin_gate": 0.1}})
    monkeypatch.setattr(r, "_rules", lambda *a, **k: [{"action": "policy", "policy": "explore"}])
    r.select_step([{"role": "user", "content": "hi"}], chain=CHAIN, profile="p", config=cfg)
    assert "matched scope" in r._decisions[-1]["note"]


def test_a_wide_margin_still_reorders(routed, registry_db):
    """The negative control: above the gate, the router behaves exactly as before."""
    cfg = Cfg({"p": {"intent_margin_gate": 0.1}})
    r = routed([("unit_tests", 0.95), ("planning", 0.20)], {"p": {"intent_margin_gate": 0.1}})
    r.db_path = registry_db
    r.select_step([{"role": "user", "content": "hi"}], chain=CHAIN, profile="p", config=cfg)
    assert all(d["action"] != "margin_gate" for d in r._decisions)


def test_gate_off_records_no_gate_decision(routed):
    """A profile that declares nothing is untouched — the field is the off switch."""
    r = routed([("unit_tests", 0.62), ("planning", 0.60)], {})
    r.select_step([{"role": "user", "content": "hi"}], chain=CHAIN,
                  profile="p", config=Cfg({}))
    assert all(d["action"] != "margin_gate" for d in r._decisions)


def test_a_lone_candidate_is_never_gated(routed):
    """Only one intent scored, so there is nothing to be ambiguous between."""
    r = routed([("unit_tests", 0.62)], {"p": {"intent_margin_gate": 0.9}})
    r.select_step([{"role": "user", "content": "hi"}], chain=CHAIN,
                  profile="p", config=Cfg({"p": {"intent_margin_gate": 0.9}}))
    assert all(d["action"] != "margin_gate" for d in r._decisions)


def test_an_explicit_rule_outranks_the_gate(routed, registry_db):
    """L1 sits above L2: a prefer is a deliberate pin, not a coin flip."""
    cfg = Cfg({"p": {"intent_margin_gate": 1.0}})   # gate that would always fire
    r = routed([("unit_tests", 0.62), ("planning", 0.60)], {"p": {"intent_margin_gate": 1.0}},
               prefer=("deepseek-v4-flash", "opencode", [{"action": "prefer"}]))
    r.db_path = registry_db
    r.select_step([{"role": "user", "content": "hi"}], chain=CHAIN, profile="p", config=cfg)
    assert all(d["action"] != "margin_gate" for d in r._decisions)


# ── invariant 3, as a property ────────────────────────────────────────────────

def test_invariant_3_margin_below_gate_always_keeps_the_static_chain(routed):
    """*margin < gate ⇒ output == static chain head, always* — swept, not sampled.

    Chain order is the observable: every "do not reorder" must leave the head the
    caller passed in, since the caller applies whatever `select_step` returns.
    """
    thin = []
    for i in range(1, 20):
        for g in (0.05, 0.1, 0.3, 0.6, 0.9, 1.0):
            top1 = 0.5 + i / 100
            margin = i / 100
            if margin < g:
                thin.append((top1, top1 - margin, g))
    assert thin, "the sweep produced nothing to test"
    for top1, top2, gate in thin:
        cfg = Cfg({"p": {"intent_margin_gate": gate}})
        r = routed([("unit_tests", top1), ("planning", top2)],
                   {"p": {"intent_margin_gate": gate}})
        head_before = (CHAIN[0]["provider"], CHAIN[0]["model"])
        out = r.select_step([{"role": "user", "content": "hi"}], chain=CHAIN,
                            profile="p", config=cfg)
        assert out is None, f"reordered at margin {top1 - top2} < gate {gate}"
        assert (CHAIN[0]["provider"], CHAIN[0]["model"]) == head_before
        assert r._decisions[-1]["action"] == "margin_gate"


def test_invariant_3_is_not_vacuous(routed):
    """Negative control: the same sweep above the gate must NOT always keep the chain."""
    fired = 0
    for i in range(1, 20):
        margin = i / 100
        gate = 0.05
        if margin > gate:
            cfg = Cfg({"p": {"intent_margin_gate": gate}})
            r = routed([("unit_tests", 0.5 + margin), ("planning", 0.5)],
                       {"p": {"intent_margin_gate": gate}})
            r.select_step([{"role": "user", "content": "hi"}], chain=CHAIN,
                          profile="p", config=cfg)
            if all(d["action"] != "margin_gate" for d in r._decisions):
                fired += 1
    assert fired == 14, f"expected the 14 wide-margin cases to pass the gate, got {fired}"


# ── the API the modal writes through ──────────────────────────────────────────
#
# These exist because a live probe on staging found the field missing from the write
# path: `PUT /api/profiles/<n>` answered `{"ok": true}` and stored nothing. A field
# the reader never surfaces and the writer silently drops is worse than no field.

from tests.test_m2d_profile_model import _handler, _real_config, _sent, _validate  # noqa: E402
from tests.test_m2d_profile_model import profiles_root  # noqa: E402,F401


def _cfg_with_gate(gate=None):
    prof = {"l2": {"chain": [{"provider": "deepseek", "model": "deepseek-chat"}]}}
    if gate is not None:
        prof["l2"]["intent_margin_gate"] = gate
    return _real_config(prof)


def test_the_gate_is_stored_by_the_write_api():
    from src.server.handler import LCPHandler
    cfg, sections = _cfg_with_gate()
    LCPHandler.config = cfg
    h = _handler("/api/profiles/l2", "PUT", {"intent_margin_gate": 0.15}, cfg)
    h._serve_profile_update("l2")
    status, body = _sent(h)
    assert status == 200, body
    assert sections["profiles"]["l2"]["intent_margin_gate"] == 0.15


def test_a_stored_gate_is_always_a_loadable_one():
    """A value the loader rejects makes the whole profiles section revert to the seed."""
    from src.server.handler import LCPHandler
    cfg, sections = _cfg_with_gate()
    LCPHandler.config = cfg
    h = _handler("/api/profiles/l2", "PUT", {"intent_margin_gate": 0.15}, cfg)
    h._serve_profile_update("l2")
    _validate("profiles", sections["profiles"])   # the failure this prevents


@pytest.mark.parametrize("bad", ["0.5", True, 1.5, -1, [0.1]])
def test_junk_from_the_api_is_refused(bad):
    from src.server.handler import LCPHandler
    cfg, sections = _cfg_with_gate()
    LCPHandler.config = cfg
    h = _handler("/api/profiles/l2", "PUT", {"intent_margin_gate": bad}, cfg)
    h._serve_profile_update("l2")
    status, body = _sent(h)
    assert status == 400, body
    assert "intent_margin_gate" in body["error"]
    assert "intent_margin_gate" not in sections["profiles"]["l2"]


def test_a_refused_gate_does_not_half_apply_its_neighbours():
    """A refused write must not leave the profile half-edited."""
    from src.server.handler import LCPHandler
    cfg, sections = _cfg_with_gate()
    LCPHandler.config = cfg
    h = _handler("/api/profiles/l2", "PUT",
                 {"agent_profile": "homelab-expert-l2", "intent_margin_gate": "half"}, cfg)
    h._serve_profile_update("l2")
    status, _ = _sent(h)
    assert status == 400
    assert "agent_profile" not in sections["profiles"]["l2"]


def test_the_list_surfaces_the_gate(profiles_root):
    from src.server.handler import LCPHandler
    cfg, _ = _cfg_with_gate(0.2)
    LCPHandler.config = cfg
    h = _handler("/api/profiles", "GET", None, cfg)
    h._serve_profiles_list()
    status, body = _sent(h)
    assert status == 200
    assert body["profiles"]["l2"]["intent_margin_gate"] == 0.2


def test_the_list_reports_off_when_the_field_is_absent(profiles_root):
    """Absent reads as 0.0 — the modal shows what it would store, which is 'off'."""
    from src.server.handler import LCPHandler
    cfg, _ = _cfg_with_gate()
    LCPHandler.config = cfg
    h = _handler("/api/profiles", "GET", None, cfg)
    h._serve_profiles_list()
    _, body = _sent(h)
    assert body["profiles"]["l2"]["intent_margin_gate"] == 0.0
