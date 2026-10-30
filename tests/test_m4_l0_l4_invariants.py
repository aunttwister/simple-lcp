"""The five §9 invariants as property tests — M4 (CUR-101).

    1. permuting the input pool never changes the winner unless chain_index
       decides it;
    2. replaying an L4 record reproduces the same winner;
    3. margin < gate ⇒ the winner's MODEL is the static chain head's, always
       (re-aimed per §26.4 — the gate never blocks a provider swap);
    4. no random, no wall-clock, no dict-iteration-order dependence in L0–L3
       (explore is the single documented exception, and its POOL is pinned);
    5. with all balances equal, the result equals the pre-balance-aware
       result (R12/M5's balance term is additive).

These fail on regression by construction: each one encodes an invariant the
operator stated in the plan, not an implementation detail.
"""
import inspect
import os
import random
import re
import tempfile

import pytest

import src.api.router as router_mod
from src.api.models import Base, get_engine
from src.api.router import CapabilityRouter, ClassifyResult, logical_model_name


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
    def __init__(self, profiles=None, policy="eager", min_score=0.0):
        self.profiles = profiles or {}
        self.dynamic_routing = {"policy": policy, "min_score": min_score}


def detail_with(semantic, task=None):
    return ClassifyResult(
        task=task or (semantic[0][0] if semantic else "fallback"),
        path="semantic",
        semantic=list(semantic),
        min_score=0.0,
        sem_available=True,
    )


def mk_router(db, monkeypatch, *, policy="eager", min_score=0.0,
              drained=(), qualities=None):
    """A router with the environment pinned deterministic: breaker healthy,
    balances per `drained`, declared qualities per `qualities`."""
    r = CapabilityRouter(enabled=True, db_path=db)
    monkeypatch.setattr(r, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(r, "_effective_policy", lambda *a, **k: (policy, min_score))
    monkeypatch.setattr(r, "_provider_available", lambda *a, **k: True)
    monkeypatch.setattr(r, "_provider_health_rank", lambda *a, **k: 0)
    monkeypatch.setattr(r, "_provider_credit_status",
                        lambda p: "drained" if p in drained else "funded")
    if qualities is not None:
        # The double must track the real signature: `get_model_score` gained an optional
        # `provider` in phase 4 so the registry can price/capability the deployment the
        # step actually names.
        monkeypatch.setattr(r, "get_model_score",
                            lambda model, task, provider=None: qualities.get(model, 0.5))
    return r


def routed(monkeypatch, r, semantic):
    """Pin the classifier so the task (and margin) is known exactly."""
    monkeypatch.setattr(router_mod, "classify_task_detail",
                        lambda *a, **k: detail_with(semantic))


# Two chains used throughout: one logical model on two providers (the §26
# provider-swap shape), and two models (a real model-change shape). Models use
# the registry's provider-side IDs (commandcode spells deepseek-v4-flash as
# deepseek/deepseek-v4-flash) so target/fallback dedup sees them.
CHAIN_SWAP = [{"provider": "commandcode", "model": "deepseek/deepseek-v4-flash"},
              {"provider": "opencode", "model": "deepseek-v4-flash"}]
CHAIN_MODELS = [{"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"},
                {"provider": "opencode", "model": "deepseek-v4-flash"}]
WIDE = [("unit_tests", 0.95), ("planning", 0.20)]   # margin 0.75 — nothing gates this
THIN = [("unit_tests", 0.62), ("planning", 0.60)]   # margin 0.02


# ── invariant 1: permutation invariance ───────────────────────────────────────

def _permute_pool(db, monkeypatch, steps, target, cidx, qualities, bias=0.0):
    r = mk_router(db, monkeypatch, qualities=qualities)
    return r._build_chain_for_model(steps, target, preferred_provider=None,
                                    profile=None, config=None, request_tokens=0,
                                    task="coding", bias=bias, chain_index=cidx)


def test_invariant_1_permuting_the_pool_never_changes_the_order(registry_db,
                                                                monkeypatch):
    """The winner is a function of (declared qualities, balances, chain_index)
    — never of the physical order the pool arrived in."""
    rng = random.Random(20261013)
    for _ in range(60):
        n = rng.randint(2, 6)
        steps = [{"provider": f"prov{i}", "model": f"m{i}"} for i in range(n)]
        # distinct declared qualities, so no chain_index rescue is needed
        qs = rng.sample(range(1, 100), n)
        qualities = {f"m{i}": qs[i] / 100 for i in range(n)}
        cidx = {(f"prov{i}", f"m{i}"): i for i in range(n)}
        target = f"m{rng.randrange(n)}"
        baseline = _permute_pool(registry_db, monkeypatch, steps, target,
                                 cidx, qualities)
        for _attempt in range(3):
            perm = steps[:]
            rng.shuffle(perm)
            again = _permute_pool(registry_db, monkeypatch, perm, target,
                                  cidx, qualities)
            assert [(s["provider"], s["model"]) for s in again] == \
                   [(s["provider"], s["model"]) for s in baseline], \
                   f"permutation changed the order for target {target}"


def test_invariant_1_when_all_tie_chain_index_decides(registry_db, monkeypatch):
    """The 'unless chain_index decides it' clause: with every other component
    tied, the winner is the pool's chain-index order — not the input order."""
    rng = random.Random(99)
    n = 5
    steps = [{"provider": f"prov{i}", "model": f"m{i}"} for i in range(n)]
    qualities = {f"m{i}": 0.5 for i in range(n)}        # all tied
    cidx = {(f"prov{i}", f"m{i}"): i for i in range(n)}
    for _ in range(10):
        perm = steps[:]
        rng.shuffle(perm)
        out = _permute_pool(registry_db, monkeypatch, perm, "m0", cidx, qualities)
        # m0's sole provider is the target head; the fallbacks must come back
        # in chain_index order regardless of the shuffle.
        assert [s["model"] for s in out] == [f"m{i}" for i in range(n)]


# ── invariant 2: the L4 record replays to the same winner ─────────────────────

def _l4_of(r):
    return r._decisions[-1]["l4"]


def test_invariant_2_same_inputs_replay_the_same_row(registry_db, monkeypatch):
    """Running the decision again from the recorded inputs reproduces the row —
    including `to`, byte for byte."""
    rows = []
    for _ in range(2):
        r = mk_router(registry_db, monkeypatch,
                      qualities={"deepseek-v4-pro": 0.5, "deepseek-v4-flash": 0.9})
        routed(monkeypatch, r, WIDE)
        out = r.select_step([{"role": "user", "content": "hi"}],
                            chain=list(CHAIN_MODELS), profile="p",
                            config=Cfg({"p": {}}))
        assert out is not None
        rows.append(_l4_of(r))
    assert rows[0] == rows[1]
    assert rows[0]["to"] == {"provider": "opencode", "model": "deepseek-v4-flash"}


def test_invariant_2_scores_replay_the_model_choice(registry_db, monkeypatch):
    """A recorded MODEL CHANGE replays from the row alone: argmax(scores) with
    a lexicographic tiebreak is the recorded `to.model` (hysteresis only ever
    demotes the best model TOWARD the default, so a recorded change IS the
    argmax)."""
    r = mk_router(registry_db, monkeypatch,
                  qualities={"deepseek-v4-pro": 0.5, "deepseek-v4-flash": 0.9})
    routed(monkeypatch, r, WIDE)
    out = r.select_step([{"role": "user", "content": "hi"}],
                        chain=list(CHAIN_MODELS), profile="p", config=Cfg({"p": {}}))
    l4 = _l4_of(r)
    assert logical_model_name(out[0]["model"]) != \
        logical_model_name(CHAIN_MODELS[0]["model"])          # a real change happened
    best = sorted(l4["scores"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    assert best == logical_model_name(l4["to"]["model"])


def test_invariant_2_balances_and_chain_replay_the_provider(registry_db,
                                                            monkeypatch):
    """The provider choice replays from the row's balances + the static chain:
    rank the target's steps by balance (desc) then chain_index — the recorded
    `to.provider` is the winner."""
    r = mk_router(registry_db, monkeypatch, drained={"commandcode"})
    routed(monkeypatch, r, WIDE)
    out = r.select_step([{"role": "user", "content": "hi"}],
                        chain=list(CHAIN_SWAP), profile="p", config=Cfg({"p": {}}))
    l4 = _l4_of(r)
    target = logical_model_name(l4["to"]["model"])
    serving = [s for s in CHAIN_SWAP
               if logical_model_name(s["model"]) == target]
    serving.sort(key=lambda s: (-l4["balances"][s["provider"]],
                                CHAIN_SWAP.index(s)))
    assert serving[0]["provider"] == l4["to"]["provider"]
    assert out[0]["provider"] == "opencode"   # the funded one, not the head
    # And the replayed provider choice matches a direct L3 ranking built only
    # from the row's balances + the static chain's indices.
    r2 = mk_router(registry_db, monkeypatch, drained={"commandcode"})
    cidx = {(s["provider"], s["model"]): i for i, s in enumerate(CHAIN_SWAP)}
    rebuilt = r2._build_chain_for_model(list(CHAIN_SWAP), target, task="coding",
                                        bias=0.0, chain_index=cidx)
    assert (rebuilt[0]["provider"], logical_model_name(rebuilt[0]["model"])) == \
        (l4["to"]["provider"], target)


# ── invariant 3: the gate vetoes a MODEL change, never a provider swap ────────

def test_invariant_3_gate_never_blocks_a_provider_swap(registry_db, monkeypatch):
    """THE §26 regression: a fired gate on a two-provider pool must still let
    the provider ranking swap commandcode→opencode. The old M3 gate returned
    None here and priced out ~$390/month + 10.3 h on l2."""
    r = mk_router(registry_db, monkeypatch, drained={"commandcode"})
    routed(monkeypatch, r, THIN)
    out = r.select_step([{"role": "user", "content": "hi"}], chain=list(CHAIN_SWAP),
                        profile="p",
                        config=Cfg({"p": {"intent_margin_gate": 0.1}}))
    assert out is not None, "gate discarded the provider ranking (the §26 bug)"
    assert logical_model_name(out[0]["model"]) == "deepseek-v4-flash"
    assert out[0]["provider"] == "opencode"
    l4 = _l4_of(r)
    assert l4["layer"] == "L2" and l4["rule_id"] == "intent_margin_gate"
    assert l4["from"]["provider"] == "commandcode" and l4["to"]["provider"] == "opencode"


def test_invariant_3_gate_always_pins_the_head_model(registry_db, monkeypatch):
    """Swept: below the gate the winner's MODEL is the static chain head's,
    whatever the scorer says. The negative control is gate=0 — the same
    requests DO change model."""
    for margin, top2 in ((0.02, 0.60), (0.01, 0.61), (0.05, 0.57)):
        sem = [("unit_tests", 0.62), ("planning", top2)]
        for gate in (0.06, 0.1, 0.5, 1.0):
            r = mk_router(registry_db, monkeypatch,
                          qualities={"deepseek-v4-pro": 0.5,
                                     "deepseek-v4-flash": 0.95})
            routed(monkeypatch, r, sem)
            out = r.select_step([{"role": "user", "content": "hi"}],
                                chain=list(CHAIN_MODELS), profile="p",
                                config=Cfg({"p": {"intent_margin_gate": gate}}))
            winner = CHAIN_MODELS[0]["model"] if out is None else out[0]["model"]
            assert logical_model_name(winner) == "deepseek-v4-pro", \
                f"model changed at margin {margin} < gate {gate}"
        # negative control: same inputs, gate off
        r = mk_router(registry_db, monkeypatch,
                      qualities={"deepseek-v4-pro": 0.5, "deepseek-v4-flash": 0.95})
        routed(monkeypatch, r, sem)
        out = r.select_step([{"role": "user", "content": "hi"}],
                            chain=list(CHAIN_MODELS), profile="p",
                            config=Cfg({"p": {}}))
        assert out is not None and logical_model_name(out[0]["model"]) == \
            "deepseek-v4-flash", "negative control failed: gate is vacuous"


# ── invariant 4: no random / wall-clock / dict-order in L0–L3 ─────────────────

_L0_L3 = ("_candidate_models", "_apply_blocks", "_resolve_prefer",
          "_l3_sort_key", "_build_chain_for_model", "_balance_headroom",
          "_choose_target_model", "_intent_margin")


@pytest.mark.parametrize("fn", _L0_L3)
def test_invariant_4_no_random_no_clock_in_l0_l3(fn):
    src = inspect.getsource(getattr(CapabilityRouter, fn))
    # word-boundary match: prose about "randomness" is fine, `random` use is not
    assert not re.search(r"\brandom\b", src) or fn == "_choose_target_model", \
        f"{fn} reaches for randomness"
    assert "now(" not in src and "time()" not in src, f"{fn} reads the clock"


def test_invariant_4_explore_is_the_single_documented_exception():
    """explore may draw randomly — but only from the within-hysteresis pool,
    whose membership is deterministic. One `random` call in one function."""
    src = inspect.getsource(CapabilityRouter._choose_target_model)
    assert src.count("import random") == 1
    assert src.count("random.") == 1          # the single random.choices call
    for other in _L0_L3:
        if other != "_choose_target_model":
            assert not re.search(r"\brandom\b", inspect.getsource(
                getattr(CapabilityRouter, other)))


def test_invariant_4_score_ties_break_lexicographically(registry_db, monkeypatch):
    """Two candidates with the SAME score (default not among them, so no
    hysteresis rescue): the pick is the lexicographically smaller model —
    never the set's iteration order."""
    r = mk_router(registry_db, monkeypatch, qualities={"zz-model": 0.7,
                                                       "aa-model": 0.7})
    assert r._choose_target_model({"zz-model", "aa-model"}, "coding",
                                  "eager", 0.0, 0.0, "other-model") == "aa-model"
    # and with the default IN the pool, the tie keeps the default (hysteresis)
    assert r._choose_target_model({"zz-model", "aa-model"}, "coding",
                                  "eager", 0.0, 0.0, "zz-model") == "zz-model"


def test_invariant_4_same_request_same_decision(registry_db, monkeypatch):
    """Whole-pipeline determinism: two fresh routers, identical inputs —
    identical L4 rows and identical chains."""
    outs = []
    for _ in range(2):
        r = mk_router(registry_db, monkeypatch,
                      qualities={"deepseek-v4-pro": 0.5, "deepseek-v4-flash": 0.9})
        routed(monkeypatch, r, WIDE)
        outs.append((r.select_step([{"role": "user", "content": "hi"}],
                                   chain=list(CHAIN_MODELS), profile="p",
                                   config=Cfg({"p": {}})), _l4_of(r)))
    (out1, l41), (out2, l42) = outs
    assert [(s["provider"], s["model"]) for s in out1] == \
           [(s["provider"], s["model"]) for s in out2]
    assert l41 == l42


def test_invariant_4_explore_pick_stays_inside_the_pinned_pool(registry_db,
                                                               monkeypatch):
    """explore's randomness is bounded: every pick is a within-hysteresis
    model — the pool is deterministic even though the draw is not."""
    r = mk_router(registry_db, monkeypatch, policy="explore",
                  qualities={"deepseek-v4-pro": 0.90, "deepseek-v4-flash": 0.88})
    routed(monkeypatch, r, WIDE)
    pool = {"deepseek-v4-pro", "deepseek-v4-flash"}   # both within 0.05 hysteresis
    for _ in range(40):
        r._decisions = []
        out = r.select_step([{"role": "user", "content": "hi"}],
                            chain=list(CHAIN_MODELS), profile="p",
                            config=Cfg({"p": {}}, policy="explore"))
        if out is None:        # keep_default is always legal
            continue
        assert logical_model_name(out[0]["model"]) in pool


# ── invariant 5: equal balances ⇒ the pre-balance-aware result ────────────────

def test_invariant_5_equal_balances_equal_no_balance_term(registry_db,
                                                          monkeypatch):
    """With every balance equal, the order is exactly what a router with NO
    balance term would produce — over a swept family of pools."""
    rng = random.Random(7)
    for _ in range(30):
        n = rng.randint(2, 5)
        steps = [{"provider": f"prov{i}", "model": f"m{i}"} for i in range(n)]
        qs = rng.sample(range(1, 100), n)
        qualities = {f"m{i}": qs[i] / 100 for i in range(n)}
        cidx = {(f"prov{i}", f"m{i}"): i for i in range(n)}
        target = f"m{rng.randrange(n)}"
        r = mk_router(registry_db, monkeypatch, qualities=qualities)
        with_bal = r._build_chain_for_model(steps, target, task="coding",
                                            bias=0.15, chain_index=cidx)
        # neutralize the balance term entirely (the pre-balance-aware router)
        monkeypatch.setattr(r, "_balance_headroom", lambda p: 1)
        without = r._build_chain_for_model(steps, target, task="coding",
                                           bias=0.15, chain_index=cidx)
        assert [(s["provider"], s["model"]) for s in with_bal] == \
               [(s["provider"], s["model"]) for s in without]


def test_invariant_5_balance_term_is_not_vacuous(registry_db, monkeypatch):
    """Negative control: an UNEQUAL balance must be able to change the
    outcome — otherwise the term is decorative and M5 builds on sand."""
    r = mk_router(registry_db, monkeypatch, drained={"commandcode"})
    cidx = {(s["provider"], s["model"]): i for i, s in enumerate(CHAIN_SWAP)}
    out = r._build_chain_for_model(list(CHAIN_SWAP), "deepseek-v4-flash",
                                   task="coding", bias=0.0, chain_index=cidx)
    assert out[0]["provider"] == "opencode"   # funded outranks drained
    monkeypatch.setattr(r, "_balance_headroom", lambda p: 1)   # pre-balance router
    out_flat = r._build_chain_for_model(list(CHAIN_SWAP), "deepseek-v4-flash",
                                        task="coding", bias=0.0, chain_index=cidx)
    assert out_flat[0]["provider"] == "commandcode"   # chain order alone
    assert out[0]["provider"] != out_flat[0]["provider"]


# ── the L4 row is the §9 shape, and it persists ────────────────────────────────

def test_l4_row_has_the_spec_shape(registry_db, monkeypatch):
    r = mk_router(registry_db, monkeypatch)
    routed(monkeypatch, r, WIDE)
    r.select_step([{"role": "user", "content": "hi"}], chain=list(CHAIN_MODELS),
                  profile="p", config=Cfg({"p": {}}))
    l4 = _l4_of(r)
    assert set(l4) == {"layer", "rule_id", "intent", "margin", "scores",
                       "balances", "from", "to", "reason"}
    assert l4["intent"] == "unit_tests"
    assert set(l4["scores"]) == {"deepseek-v4-pro", "deepseek-v4-flash"}
    assert set(l4["balances"]) == {"commandcode", "opencode"}


def test_l4_row_persists_and_reads_back(registry_db, monkeypatch):
    r = mk_router(registry_db, monkeypatch)
    routed(monkeypatch, r, WIDE)
    r.select_step([{"role": "user", "content": "hi"}], chain=list(CHAIN_MODELS),
                  profile="p", config=Cfg({"p": {}}))
    rows = r.recent_decisions(5)
    assert rows and rows[0]["l4"] == _l4_of(r)
