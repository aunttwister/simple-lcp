"""Unit tests for the LCP preflight gate (PLAN §8 / R11 — task CUR-103).

The gate is ONE function, ``src.api.preflight.preflight_gate``, that runs
immediately before ``try_chain``'s provider loop and answers "can this request
succeed, and on which lane?" — G0 capability, G1 breaker, G2 budget,
G3 balance, G4 intent gate.

What is proven here:

* one test per gate (G0–G4) for its decisive behaviour;
* a PASS-THROUGH case — a valid request changes nothing and reaches
  ``try_chain``'s provider call exactly as before;
* a REFUSED request produces NO provider call: the provider client is stubbed
  and asserted never invoked (this is the "a doomed request costs nothing"
  property);
* the refusal is recorded on the request's ``routing_decisions`` row, carrying
  the failed gate id.

Tests are hermetic: balance/intent behaviour is driven through a duck-typed
router stub, budget checks through a temp SQLite DB, and no test container,
network or live DB is touched.
"""
import os
import tempfile

import pytest
from unittest.mock import patch

from src.api.exceptions import PreflightRefusalError
from src.api.preflight import (
    GATE_BALANCE,
    GATE_BREAKER,
    GATE_BUDGET,
    GATE_CAPABILITY,
    GATE_INTENT,
    DENY,
    DOWNGRADE,
    PASS,
    preflight_gate,
)


# ── Fixtures / helpers ──────────────────────────────────────────────────────

class FakeRouter:
    """Duck-typed CapabilityRouter exposing only what the gate calls.

    Keeping the stub here (instead of a MagicMock) means every value the gate
    reads is explicit — a change in what the gate consults shows up as a test
    failure rather than a silently-true Mock.
    """

    def __init__(self, headroom=None, contexts=None,
                 intent_gate=0.0, margin=None, db_path="data/costs.db"):
        self.headroom = headroom or {}
        self.contexts = contexts or {}
        self._intent_gate = intent_gate
        self._margin = margin
        self.db_path = db_path

    def _balance_headroom(self, provider):
        return self.headroom.get(provider, 1)

    def _context_window_for(self, logical, config=None):
        return self.contexts.get(logical, 128000)

    def _max_routable_context(self, chain, config=None):
        return max((self._context_window_for(s.get("model"), config)
                    for s in chain), default=128000)

    def _intent_margin_gate(self, config, profile):
        return self._intent_gate

    def _intent_margin(self, detail):
        return self._margin, "task_a", "task_b"


def make_config(**overrides):
    """A minimal duck-typed config (MagicMock) with sane defaults."""
    from unittest.mock import MagicMock
    cfg = MagicMock()
    cfg.circuit_breaker = {
        "failures_degraded": 3, "failures_dead": 5,
        "degraded_cooldown_seconds": 60, "dead_cooldown_seconds": 300,
    }
    cfg.retry = {"max_attempts": 1, "backoff_base": 0.01,
                 "backoff_multiplier": 2, "max_backoff": 1, "jitter": False}
    cfg.model_limits = {}
    cfg.providers = {}
    cfg.profiles = {}
    cfg._data = {}
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def chain_of(*pairs, model="m"):
    return [{"provider": p, "model": model, "base_url": f"http://{p}"} for p in pairs]


BODY = {"messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def gate_db():
    """A private SQLite DB (schema created) the gate can record decisions into."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from src.api.models import Base, get_engine
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    yield path, engine
    engine.dispose()
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


def decision_rows(engine):
    from src.api.models import RoutingDecision, get_session
    with get_session(engine) as session:
        rows = session.query(RoutingDecision).order_by(RoutingDecision.id).all()
        return [{
            "action": r.action, "path": r.path, "note": r.note,
            "profile": r.profile, "task": r.task, "policy": r.policy,
            "provider": r.provider, "model": r.model,
            "rules_json": r.rules_json, "l4_json": r.l4_json,
        } for r in rows]


# ── G0 — capability ─────────────────────────────────────────────────────────

class TestG0Capability:
    def test_image_request_refused_when_no_model_declares_vision(self):
        cfg = make_config(model_limits={})  # nothing declares supports_vision
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]}]}
        result = preflight_gate("l2", {"chain": chain_of("p")}, body, cfg,
                                router=FakeRouter(), record=False)

        assert result.verdict == DENY
        assert result.gate == GATE_CAPABILITY
        assert result.refusal is not None
        assert result.refusal.details["reason"] == "vision_unsupported"
        assert result.refusal.code == "LCP-4004"
        assert result.refusal.status_code == 400

    def test_image_request_passes_when_a_model_declares_vision(self):
        cfg = make_config(model_limits={"m": {"supports_vision": True,
                                              "context_window": 128000}})
        body = {"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        ]}]}
        result = preflight_gate("l2", {"chain": chain_of("p")}, body, cfg,
                                router=FakeRouter(), record=False)
        assert result.verdict == PASS
        assert result.refusal is None

    def test_oversized_request_refused_on_context(self):
        cfg = make_config(model_limits={"m": {"context_window": 1000}})
        router = FakeRouter(contexts={"m": 1000})
        result = preflight_gate("l2", {"chain": chain_of("p")}, BODY, cfg,
                                router=router, request_tokens=50_000,
                                record=False)
        assert result.verdict == DENY
        assert result.gate == GATE_CAPABILITY
        assert result.refusal.details["reason"] == "context_exceeded"
        assert result.refusal.status_code == 413

    def test_request_inside_every_context_is_not_refused(self):
        cfg = make_config(model_limits={"m": {"context_window": 262144}})
        router = FakeRouter(contexts={"m": 262144})
        result = preflight_gate("l2", {"chain": chain_of("p")}, BODY, cfg,
                                router=router, request_tokens=100_000,
                                record=False)
        assert result.refusal is None

    def test_response_format_contract_refused_when_every_provider_rejects(self):
        # `deepseek` is declared (capabilities.py) to accept text/json_object only.
        cfg = make_config(providers={"deepseek": {"api_base": "http://d"}})
        body = {"messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_schema", "json_schema": {}}}
        result = preflight_gate("l2", {"chain": chain_of("deepseek")}, body, cfg,
                                router=FakeRouter(), record=False)
        assert result.verdict == DENY
        assert result.gate == GATE_CAPABILITY
        assert result.refusal.details["reason"] == "response_format_unsupported"

    def test_vision_refusal_blocks_the_provider_call(self):
        """A G0-denied request never reaches a provider."""
        from src.api.request_pipeline import try_chain
        cfg = make_config(model_limits={})
        body = {"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "x"}},
        ]}]}
        profile_cfg = {"chain": chain_of("g0vision"), "forbidden_tools": []}

        with patch("src.api.request_pipeline.forward_request") as provider:
            with pytest.raises(PreflightRefusalError) as exc:
                try_chain("l2", profile_cfg, body, cfg)

        assert exc.value.gate == GATE_CAPABILITY
        assert exc.value.code == "LCP-4004"
        provider.assert_not_called()


# ── G1 — breaker ────────────────────────────────────────────────────────────

class TestG1Breaker:
    def test_all_providers_unavailable_is_refused(self):
        from src.api.circuit_breaker import get_circuit_breaker
        cfg = make_config(providers={"g1a": {"api_base": "http://g1a"}})
        cb = get_circuit_breaker(cfg)
        cb.record_failure("g1a", "http://g1a", "l2")  # failures_dead=5 → degrade
        for _ in range(4):
            cb.record_failure("g1a", "http://g1a", "l2")  # → dead
        assert cb.status_of("g1a", "http://g1a", "l2") == "dead"

        result = preflight_gate("l2", {"chain": chain_of("g1a")}, BODY, cfg,
                                router=FakeRouter(), record=False)
        assert result.verdict == DENY
        assert result.gate == GATE_BREAKER
        assert result.refusal.status_code == 502
        assert result.refusal.details["reason"] == "all_providers_unavailable"

    def test_a_healthy_member_keeps_the_chain_usable(self):
        from src.api.circuit_breaker import get_circuit_breaker
        cfg = make_config(providers={"g1b": {"api_base": "http://g1b"},
                                     "g1c": {"api_base": "http://g1c"}})
        cb = get_circuit_breaker(cfg)
        for _ in range(5):
            cb.record_failure("g1b", "http://g1b", "l2")
        assert cb.status_of("g1b", "http://g1b", "l2") == "dead"

        result = preflight_gate("l2", {"chain": chain_of("g1b", "g1c")}, BODY, cfg,
                                router=FakeRouter(), record=False)
        assert result.refusal is None
        assert result.checks[GATE_BREAKER] == "pass"

    def test_dead_chain_is_refused_before_any_provider_call(self):
        """A fully dead chain is refused here instead of walking the chain to a 502."""
        from src.api.circuit_breaker import get_circuit_breaker
        from src.api.request_pipeline import try_chain
        cfg = make_config(providers={"g1d": {"api_base": "http://g1d"}})
        cb = get_circuit_breaker(cfg)
        for _ in range(5):
            cb.record_failure("g1d", "http://g1d", "l2")

        profile_cfg = {"chain": chain_of("g1d"), "forbidden_tools": []}
        with patch("src.api.request_pipeline.forward_request") as provider:
            with pytest.raises(PreflightRefusalError) as exc:
                try_chain("l2", profile_cfg, BODY, cfg)

        assert exc.value.gate == GATE_BREAKER
        provider.assert_not_called()


# ── G2 — budget ─────────────────────────────────────────────────────────────

class TestG2Budget:
    def _seed_budget(self, engine, *, amount=10.0, spend=10.0, name="cap",
                     action="block", status="active", profile="l2"):
        from src.api.models import Budget, get_session
        with get_session(engine) as session:
            session.add(Budget(name=name, profile=profile, amount=amount,
                               current_spend=spend, action=action, status=status))
            session.commit()

    def test_exhausted_blocking_budget_refuses(self, gate_db):
        path, engine = gate_db
        self._seed_budget(engine)
        result = preflight_gate("l2", {"chain": chain_of("p")}, BODY,
                                make_config(), engine=engine,
                                router=FakeRouter(), record=False)
        assert result.verdict == DENY
        assert result.gate == GATE_BUDGET
        assert result.refusal.status_code == 429
        assert result.refusal.details["budget"] == "cap"

    def test_budget_with_headroom_passes(self, gate_db):
        path, engine = gate_db
        self._seed_budget(engine, spend=1.0)
        result = preflight_gate("l2", {"chain": chain_of("p")}, BODY,
                                make_config(), engine=engine,
                                router=FakeRouter(), record=False)
        assert result.refusal is None
        assert result.checks[GATE_BUDGET] == "pass"

    def test_a_log_only_budget_does_not_block(self, gate_db):
        path, engine = gate_db
        self._seed_budget(engine, action="log")
        result = preflight_gate("l2", {"chain": chain_of("p")}, BODY,
                                make_config(), engine=engine,
                                router=FakeRouter(), record=False)
        assert result.refusal is None

    def test_blocked_budget_blocks_the_provider_call(self, gate_db):
        """The gate resolves the DB from the config and refuses before spending."""
        from src.api.request_pipeline import try_chain
        path, engine = gate_db
        self._seed_budget(engine)
        cfg = make_config(providers={"p": {"api_base": "http://p"}})
        cfg._data = {"database": {"path": path}}
        profile_cfg = {"chain": chain_of("p"), "forbidden_tools": []}

        with patch("src.api.request_pipeline.forward_request") as provider:
            with pytest.raises(PreflightRefusalError) as exc:
                try_chain("l2", profile_cfg, BODY, cfg)

        assert exc.value.gate == GATE_BUDGET
        provider.assert_not_called()


# ── G3 — balance ────────────────────────────────────────────────────────────

class TestG3Balance:
    def test_drained_head_is_reranked_not_refused(self):
        """§8: G3 're-ranks, does not fail' — the request still goes out, funded first."""
        router = FakeRouter(headroom={"a": 0, "b": 1})
        result = preflight_gate("l2", {"chain": chain_of("a", "b")}, BODY,
                                make_config(), router=router, record=False)
        assert result.verdict == DOWNGRADE
        assert result.gate == GATE_BALANCE
        assert result.refusal is None
        assert [s["provider"] for s in result.chain] == ["b", "a"]
        assert result.checks[GATE_BALANCE] == "downgrade"

    def test_funded_head_leaves_the_chain_untouched(self):
        router = FakeRouter(headroom={"a": 1, "b": 1})
        result = preflight_gate("l2", {"chain": chain_of("a", "b")}, BODY,
                                make_config(), router=router, record=False)
        assert result.verdict == PASS
        assert [s["provider"] for s in result.chain] == ["a", "b"]
        assert result.checks[GATE_BALANCE] == "pass"

    def test_every_provider_drained_is_refused(self):
        router = FakeRouter(headroom={"a": 0, "b": 0})
        result = preflight_gate("l2", {"chain": chain_of("a", "b")}, BODY,
                                make_config(), router=router, record=False)
        assert result.verdict == DENY
        assert result.gate == GATE_BALANCE
        assert result.refusal.status_code == 402
        assert result.refusal.details["reason"] == "all_providers_drained"

    def test_rerank_is_applied_by_try_chain(self, monkeypatch):
        """try_chain calls the re-ranked lane first, and its provider is funded."""
        from src.api import router as router_mod
        from src.api.request_pipeline import try_chain
        monkeypatch.setattr(router_mod.CapabilityRouter, "_balance_headroom",
                            lambda self, provider: 0 if provider == "g3drained" else 1)
        cfg = make_config(providers={"g3drained": {"api_base": "http://g3drained"},
                                     "g3funded": {"api_base": "http://g3funded"}})
        profile_cfg = {"chain": chain_of("g3drained", "g3funded"),
                       "forbidden_tools": []}

        with patch("src.api.request_pipeline.forward_request") as provider:
            provider.return_value = ({"choices": []}, 200)
            _resp, _status, used, _model = try_chain("l2", profile_cfg, BODY, cfg)

        assert used == "g3funded"
        attempted = [c.args[0]["provider"] for c in provider.call_args_list]
        assert attempted[0] == "g3funded"


# ── G4 — intent gate ────────────────────────────────────────────────────────

class TestG4Intent:
    STATIC = chain_of("g4static1", "g4static2")
    PROPOSED = chain_of("g4proposed", "g4static2")

    def test_low_margin_keeps_the_static_order(self):
        router = FakeRouter(intent_gate=0.35, margin=0.02)
        result = preflight_gate("l2", {"chain": self.PROPOSED}, BODY,
                                make_config(), chain=self.PROPOSED,
                                static_chain=self.STATIC, router=router,
                                record=False)
        assert result.verdict == PASS
        assert result.gate == GATE_INTENT
        assert result.refusal is None
        assert result.checks[GATE_INTENT] == "static"
        # the router's proposed reorder is vetoed; the static order stands
        assert [s["provider"] for s in result.chain] == ["g4static1", "g4static2"]
        assert any("static chain order kept" in n for n in result.notes)

    def test_high_margin_keeps_the_proposed_order(self):
        router = FakeRouter(intent_gate=0.35, margin=0.90)
        result = preflight_gate("l2", {"chain": self.PROPOSED}, BODY,
                                make_config(), chain=self.PROPOSED,
                                static_chain=self.STATIC, router=router,
                                record=False)
        assert result.verdict == PASS
        assert result.checks[GATE_INTENT] == "pass"
        assert [s["provider"] for s in result.chain] == ["g4proposed", "g4static2"]

    def test_no_declared_gate_skips_the_check(self):
        router = FakeRouter(intent_gate=0.0, margin=0.01)
        result = preflight_gate("l2", {"chain": self.PROPOSED}, BODY,
                                make_config(), chain=self.PROPOSED,
                                static_chain=self.STATIC, router=router,
                                record=False)
        assert result.chain == self.PROPOSED
        assert result.checks[GATE_INTENT] == "skip"


# ── Pass-through + decision row ─────────────────────────────────────────────

class TestPassThrough:
    def test_valid_request_passes_every_gate(self):
        cfg = make_config(providers={"ok": {"api_base": "http://ok"}})
        result = preflight_gate("l2", {"chain": chain_of("ok")}, BODY, cfg,
                                router=FakeRouter(), record=False)
        assert result.verdict == PASS
        assert result.gate is None
        assert result.refusal is None
        assert result.chain == chain_of("ok")

    def test_valid_request_reaches_try_chain_unchanged(self):
        """The gate is transparent: same chain, same body, one provider call."""
        from src.api.circuit_breaker import get_circuit_breaker
        from src.api.request_pipeline import try_chain
        cfg = make_config(providers={"ok": {"api_base": "http://ok"}})
        get_circuit_breaker(cfg)
        profile_cfg = {"chain": chain_of("ok"), "forbidden_tools": []}
        sent = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        with patch("src.api.request_pipeline.forward_request") as provider:
            provider.return_value = (sent, 200)
            resp, status, used, model = try_chain("l2", profile_cfg, dict(BODY), cfg)

        assert (resp, status, used, model) == (sent, 200, "ok", "m")
        assert provider.call_count == 1
        assert provider.call_args.args[0]["provider"] == "ok"
        # the request body was not mutated by the gate
        assert provider.call_args.args[1]["messages"] == BODY["messages"]

    def test_a_clean_pass_writes_no_decision_row_by_default(self, gate_db):
        path, engine = gate_db
        cfg = make_config()
        cfg._data = {"database": {"path": path}}
        preflight_gate("l2", {"chain": chain_of("ok")}, BODY, cfg,
                       router=FakeRouter(), record=True)
        assert decision_rows(engine) == []

    def test_a_pass_can_be_recorded_when_asked(self, gate_db):
        path, engine = gate_db
        cfg = make_config()
        cfg._data = {"database": {"path": path}}
        result = preflight_gate("l2", {"chain": chain_of("ok")}, BODY, cfg,
                                router=FakeRouter(), record=True, record_pass=True)
        assert result.recorded is True
        rows = decision_rows(engine)
        assert len(rows) == 1
        assert rows[0]["action"] == "gate_pass"
        assert rows[0]["policy"] == "preflight"

    def test_refusal_is_recorded_on_the_decision_row(self, gate_db):
        """The failed gate id lands on the decision row LCP already writes."""
        path, engine = gate_db
        cfg = make_config(model_limits={})
        cfg._data = {"database": {"path": path}}
        body = {"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "x"}},
        ]}]}

        result = preflight_gate("l2", {"chain": chain_of("p")}, body, cfg,
                                router=FakeRouter(), record=True, task="code")

        assert result.recorded is True
        rows = decision_rows(engine)
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "refuse"
        assert row["path"] == GATE_CAPABILITY           # the failed gate id
        assert row["profile"] == "l2"
        assert row["task"] == "code"
        assert row["policy"] == "preflight"
        assert row["provider"] == "p"
        assert row["model"] == "m"
        assert "image content" in row["note"]
        assert GATE_CAPABILITY in (row["l4_json"] or "")

    def test_recorder_can_be_injected(self):
        seen = []
        result = preflight_gate(
            "l2", {"chain": chain_of("p")},
            {"messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "x"}}]}]},
            make_config(model_limits={}), router=FakeRouter(),
            recorder=lambda d: seen.append(d) or True)
        assert result.is_refusal
        assert len(seen) == 1
        assert seen[0]["action"] == "refuse"
        assert seen[0]["path"] == GATE_CAPABILITY

    def test_refused_request_never_calls_the_provider_client(self):
        """Belt-and-braces: stub the provider client, assert it is never invoked."""
        from src.api.request_pipeline import try_chain
        cfg = make_config(model_limits={})
        body = {"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "x"}}]}]}
        profile_cfg = {"chain": chain_of("p", "p2"), "forbidden_tools": []}

        with patch("src.api.request_pipeline.forward_request") as provider:
            with pytest.raises(PreflightRefusalError):
                try_chain("l2", profile_cfg, body, cfg)

        assert provider.call_count == 0
        provider.assert_not_called()
