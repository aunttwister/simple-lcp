"""Batch E coverage gaps — src/api/router.py branch coverage.

Closes helper + CapabilityRouter branches:
  - detect_quantization empty, _content_text list/tool_result/stray blocks,
    _message_has_tool_calls content-blocks, _has_tool_result_blocks,
    _matches_tool_result_patterns (empty/prefix/pattern/diff/JSON-array),
    _strip_bracket_segments unclosed, _context_tail None-tails,
    _strip_client_context_from_messages drop paths, _is_preamble_like
    short/continuation/task-signal exits, _preamble_tail exits, _is_tool_result
    layers, _extract_intent_text empty, classify_task_detail classifier crash
    + tool/token/casual structural paths
  - CapabilityRouter: load_matrix failure, _has_profile_override settings
    crash, _effective_policy TypeError+crash, _record_decision DB failure,
    recent_decisions DB failure, _health_bonus/_provider_available/
    _provider_health_rank CB crashes, prefer chain-as-source-of-truth
    serving, _credit_bonus error branches, _coerce_context, _context_window_for duck
    config, _fits_context, _candidate_models providers-crash, _choose_target
    _model empty/below-min, _provider_credit_rank balance branches, _rules
    crash/config, _rule_matches variants, _apply_blocks prefer-skip branches,
    _resolve_prefer gate/unserved/provider-only, _apply_rules provider-only,
    select_step disabled/empty-chain/all-blocked paths, get_dynamic_router
    resolve-exception, sync_router_enabled_from_settings crash,
    RouterComponent setup crash, routing_status selected-set crashes
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import src.api.router as R
from src.api.router import CapabilityRouter


@pytest.fixture
def db(tmp_path):
    from src.api.models import get_engine, Base
    path = str(tmp_path / "r.db")
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


def _rt(db):
    return CapabilityRouter(enabled=True, db_path=db)


# ── module-level helpers ─────────────────────────────────────────────────────

class TestHelperGaps:
    def test_detect_quantization_empty(self):
        assert R.detect_quantization("") is None  # 74

    def test_content_text_list_variants(self):
        msg = {"content": [
            "not-a-dict",                                # 240 continue
            {"text": "hello"},                           # 242
            {"type": "tool_result", "content": "plain"}, # 246
            {"type": "tool_result", "content": [{"text": "sub"}, {"x": 1}]},  # 248-250
            {"type": "other"},                           # skipped
        ]}
        assert R._content_text(msg) == "hello plain sub"
        assert R._content_text({"content": None}) == ""  # 252

    def test_message_has_tool_calls_blocks(self):
        assert R._message_has_tool_calls(
            {"content": [{"type": "tool_use"}, 5]}) is True   # 262-265
        assert R._message_has_tool_calls({"content": ["x"]}) is False
        assert R._message_has_tool_calls({"content": "hi"}) is False

    def test_has_tool_result_blocks(self):
        assert R._has_tool_result_blocks(
            {"content": [{"type": "tool_result"}]}) is True   # 273-276
        assert R._has_tool_result_blocks({"content": "hi"}) is False

    def test_matches_tool_result_patterns(self):
        assert R._matches_tool_result_patterns("") is False   # 284
        assert R._matches_tool_result_patterns("Tool result: ok") is True   # 287
        assert R._matches_tool_result_patterns(
            "Traceback (most recent call last): x = 1") is True  # 289
        assert R._matches_tool_result_patterns("diff --git a/x") is True  # 291-292
        assert R._matches_tool_result_patterns('["a", "b"]') is True     # 294-295
        assert R._matches_tool_result_patterns("please implement a feature") is False

    def test_strip_bracket_segments_unclosed(self):
        assert R._strip_bracket_segments("[unclosed text") == "[unclosed text"  # 369-370

    def test_context_tail_no_user_request(self):
        # 390: not a client-context wrapper at all
        assert R._context_tail("plain request") is None

    def test_context_tail_truncated_bracket(self):
        with patch.object(R, "_is_client_context", return_value=True):
            assert R._context_tail("[unclosed notice") is None  # 404

    def test_context_tail_continuation_dropped(self):
        with patch.object(R, "_is_client_context", return_value=True):
            assert R._context_tail("[notice] OK") is None  # 406 continuation

    def test_strip_client_context_drops_wrapper(self):
        msgs = [
            {"role": "user", "content": "real question"},
            {"role": "user", "content": "<system-reminder>stuff</system-reminder>"},
        ]
        with patch.object(R, "_is_client_context",
                          side_effect=lambda t: "reminder" in t):
            with patch.object(R, "_context_tail", return_value=""):  # 434-435 drop
                out = R._strip_client_context_from_messages(msgs)
        assert len(out) == 1

    def test_preamble_like_exits(self):
        assert R._is_preamble_like("") is False            # 506
        assert R._is_preamble_like("short") is False       # 509
        assert R._is_preamble_like("OK") is False          # 511 continuation
        assert R._is_preamble_like("please implement a login feature endpoint") is False  # 520

    def test_preamble_tail_exits(self):
        with patch.object(R, "_is_preamble_like", return_value=False):
            assert R._preamble_tail("x") is None           # 528
        with patch.object(R, "_is_preamble_like", return_value=True):
            assert R._preamble_tail("no blank line here") is None  # 532
            assert R._preamble_tail("head\n\ncontinue") is None    # 536

    def test_is_tool_result_layers(self):
        assert R._is_tool_result({"tool_call_id": "t"}, [{"tool_call_id": "t"}], 0) is True
        assert R._is_tool_result({"content": [{"type": "tool_result"}]}, [], 0) is True  # 551
        msgs = [{"role": "assistant", "tool_calls": [{"id": "1"}]},
                {"role": "user", "content": "output"}]
        assert R._is_tool_result(msgs[1], msgs, 1) is True  # 553-555

    def test_extract_intent_text_empty(self):
        text, meta = R._extract_intent_text([])            # 597
        assert text == "" and meta["source"] == "none"

    def test_classify_detail_semantic_crash(self):
        with patch("src.api.task_classifier.get_semantic_classifier",
                   side_effect=RuntimeError("classifier exploded")):  # 773-774
            res = R.classify_task_detail(
                [{"role": "user", "content": "write unit tests for module x"}])
        assert res.task  # classification still returned via keyword path

    def test_classify_detail_tool_count(self):
        tools = [{"function": {"name": f"t{i}"}} for i in range(7)]
        with patch("src.api.task_classifier.get_semantic_classifier",
                   return_value=None):
            res = R.classify_task_detail(
                [{"role": "user", "content": "zzz qqq wwww"}], tools=tools)  # 787-793
        assert res.path == "tool_count" and res.task == "agentic_multi_step"

    def test_classify_detail_token_count(self):
        with patch("src.api.task_classifier.get_semantic_classifier",
                   return_value=None), \
             patch("src.api.router.count_tokens", return_value=9000):  # 799-806
            res = R.classify_task_detail(
                [{"role": "user", "content": "zzz qqq wwww"}])
        assert res.path == "token_count" and res.task == "research_deep"

    def test_classify_detail_casual(self):
        res = R.classify_task_detail(
            [{"role": "user", "content": "hey there, how are you doing?"}])
        assert res.task == "casual_chat"                    # 810-813


# ── CapabilityRouter: matrix / policy / decisions ────────────────────────────

class TestRouterCoreGaps:
    def test_load_matrix_failure(self, db):
        r = _rt(db)
        with patch("src.api.seed_capabilities.load_capability_matrix",
                   side_effect=RuntimeError("db locked")):
            assert r.load_matrix() == {}                     # 1128-1130

    def test_has_profile_override_settings_crash(self, db):
        r = _rt(db)
        with patch("src.api.cost_cache.get_settings",
                   side_effect=RuntimeError("no settings")):
            assert r._has_profile_override("l2") is False    # 1157-1158

    def test_has_profile_override_true(self, db):
        r = _rt(db)
        st = MagicMock()
        st.get.return_value = "eager"
        with patch("src.api.cost_cache.get_settings", return_value=st):
            assert r._has_profile_override("l2") is True

    def test_effective_policy_typeerror(self, db):
        r = _rt(db)
        st = MagicMock()
        st.get_routing_policy.side_effect = lambda **k: "cost_first" if "profile" in k else TypeError
        st.get_routing_min_score.side_effect = lambda **k: 0.3 if "profile" in k else TypeError

        def pol(default=None, profile=None):
            if profile is not None:
                raise TypeError("no profile kwarg")
            return "cost_first"

        def ms(default=None, profile=None):
            if profile is not None:
                raise TypeError("no profile kwarg")
            return 0.3
        st.get_routing_policy.side_effect = pol
        st.get_routing_min_score.side_effect = ms
        with patch("src.api.cost_cache.get_settings", return_value=st):
            policy, min_score = r._effective_policy(None, "l2")  # 1186-1187
        assert policy == "cost_first" and min_score == 0.3

    def test_effective_policy_settings_crash(self, db):
        r = _rt(db)
        with patch("src.api.cost_cache.get_settings",
                   side_effect=RuntimeError("boom")):
            cfg = MagicMock()
            cfg.dynamic_routing = {"policy": "explore", "min_score": 0.5}
            policy, min_score = r._effective_policy(cfg)
        assert policy == "explore" and min_score == 0.5

    def test_effective_policy_config_crash(self, db):
        r = _rt(db)
        cfg = MagicMock()
        type(cfg).dynamic_routing = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("no dr")))
        with patch("src.api.cost_cache.get_settings", return_value=None):
            policy, min_score = r._effective_policy(cfg)     # 1195-1196
        assert policy == "eager"

    def test_record_decision_db_failure_swallowed(self, db):
        r = _rt(db)
        with patch("src.api.models.get_engine", side_effect=RuntimeError("locked")):
            r._record_decision({"action": "test"})           # 1280-1281
        assert r._decisions[-1]["action"] == "test"

    def test_recent_decisions_db_failure(self, db):
        r = _rt(db)
        r._decisions = [{"action": "mem"}]
        with patch("src.api.models.get_engine", side_effect=RuntimeError("gone")):
            out = r.recent_decisions(5)                      # 1316-1318
        assert out == [{"action": "mem"}]

    def test_get_model_score_resolved_debug(self, db):
        r = _rt(db)
        r._matrix = {"coding": {"logical-x": 0.9}}
        with patch.object(R, "logical_model_name", return_value="logical-x"), \
             patch.object(R, "benchmark_model_name", return_value="logical-x"):
            assert r.get_model_score("provider-side-id", "coding") == 0.9  # 1334-1340


# ── CapabilityRouter: provider gates & bonuses ───────────────────────────────

class TestRouterGatesGaps:
    def test_health_bonus_crash(self, db):
        r = _rt(db)
        with patch("src.api.circuit_breaker.get_circuit_breaker",
                   side_effect=RuntimeError("cb gone")):
            assert r._health_bonus({"provider": "p", "base_url": ""}) == 0.0  # 1362-1363

    def test_health_bonus_status(self, db):
        r = _rt(db)
        cb = MagicMock()
        cb.status_of.return_value = "healthy"
        cfg = MagicMock()
        cfg.providers = {"p": {"api_base": "http://x"}}
        with patch("src.api.circuit_breaker.get_circuit_breaker", return_value=cb):
            bonus = r._health_bonus({"provider": "p"}, "l2", cfg)
        assert bonus == R._HEALTH_BONUS["healthy"]
        assert cb.status_of.call_args[0][1] == "http://x"

    def test_provider_available_crash(self, db):
        r = _rt(db)
        with patch("src.api.circuit_breaker.get_circuit_breaker",
                   side_effect=RuntimeError("cb gone")):
            assert r._provider_available({"provider": "p"}) is True  # 1384-1385

    def test_provider_serves_model_chain_truth(self, db):
        # 325d050: chain-as-source-of-truth — a provider "serves" a model
        # when a chain step uses it; the provider's global models list is
        # never consulted (a provider with no list still serves via chain).
        r = _rt(db)
        chain = [{"provider": "p", "model": "mm"}]
        with patch.object(r, "_rules", return_value=[
                {"action": "prefer", "model": "mm"}]):
            tm, pp, fired = r._resolve_prefer(chain, "t", "l2", None)
        assert tm == "mm"
        assert fired[0]["action"] == "prefer"
        assert fired[0]["steps"] == 1

    def test_credit_bonus_branches(self, db):
        r = _rt(db)
        plugin = MagicMock()
        fake_reg = MagicMock()
        fake_reg.for_provider.return_value = plugin
        cache = MagicMock()
        # Drained via the provider's cost plugin → penalty (< 0).
        plugin.credit_status.return_value = "drained"
        with patch("src.api.runtime.resolve_service", return_value=fake_reg), \
             patch("src.api.cost_cache.get_cost_cache", return_value=cache):
            assert r._credit_bonus("p") < 0                  # 1419
        # Funded / unknown → no penalty.
        plugin.credit_status.return_value = "funded"
        with patch("src.api.runtime.resolve_service", return_value=fake_reg), \
             patch("src.api.cost_cache.get_cost_cache", return_value=cache):
            assert r._credit_bonus("p") == 0.0
        plugin.credit_status.return_value = "unknown"
        with patch("src.api.runtime.resolve_service", return_value=fake_reg), \
             patch("src.api.cost_cache.get_cost_cache", return_value=cache):
            assert r._credit_bonus("p") == 0.0
        # No plugin for the provider → unknown → no penalty.
        fake_reg.for_provider.return_value = None
        with patch("src.api.runtime.resolve_service", return_value=fake_reg), \
             patch("src.api.cost_cache.get_cost_cache", return_value=cache):
            assert r._credit_bonus("p") == 0.0
        # Cache gone → unknown → no penalty.
        with patch("src.api.cost_cache.get_cost_cache",
                   side_effect=RuntimeError("cache gone")):
            assert r._credit_bonus("p") == 0.0

    def test_coerce_context_variants(self, db):
        r = _rt(db)
        assert r._coerce_context(True) is None               # 1504-1505
        assert r._coerce_context(8000) == 8000
        assert r._coerce_context("128000") == 128000         # 1511-1513
        assert r._coerce_context("nope") is None
        assert r._coerce_context(object()) is None

    def test_context_window_duck_config(self, db):
        r = _rt(db)
        cfg = MagicMock()
        type(cfg).model_limits = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("duck")))
        assert r._context_window_for("m", cfg) == R._DEFAULT_CONTEXT_WINDOW  # 1532-1533

    def test_fits_context_zero(self, db):
        r = _rt(db)
        assert r._fits_context("m", 0) is True               # 1540

    def test_candidate_models_providers_crash(self, db):
        r = _rt(db)
        cfg = MagicMock()
        type(cfg).providers = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        chain = [{"provider": "p", "model": "m"}]
        with patch.object(R, "logical_model_name", return_value="m"):
            cand = r._candidate_models(chain, set(), cfg, 0)  # 1574-1575
        assert "m" in cand

    def test_provider_health_rank_crash(self, db):
        r = _rt(db)
        with patch("src.api.circuit_breaker.get_circuit_breaker",
                   side_effect=RuntimeError("gone")):
            assert r._provider_health_rank({"provider": "p"}) == 0  # 1634-1635

    def test_provider_credit_rank_branches(self, db):
        r = _rt(db)
        plugin = MagicMock()
        fake_reg = MagicMock()
        fake_reg.for_provider.return_value = plugin
        cache = MagicMock()
        # Drained → rank 1 (chain ordering prefers funded providers).
        plugin.credit_status.return_value = "drained"
        with patch("src.api.runtime.resolve_service", return_value=fake_reg), \
             patch("src.api.cost_cache.get_cost_cache", return_value=cache):
            assert r._provider_credit_rank("p") == 1         # 1622
        # Funded / unknown → rank 0.
        plugin.credit_status.return_value = "funded"
        with patch("src.api.runtime.resolve_service", return_value=fake_reg), \
             patch("src.api.cost_cache.get_cost_cache", return_value=cache):
            assert r._provider_credit_rank("p") == 0
        plugin.credit_status.return_value = "unknown"
        with patch("src.api.runtime.resolve_service", return_value=fake_reg), \
             patch("src.api.cost_cache.get_cost_cache", return_value=cache):
            assert r._provider_credit_rank("p") == 0
        # No plugin for the provider → unknown → rank 0.
        fake_reg.for_provider.return_value = None
        with patch("src.api.runtime.resolve_service", return_value=fake_reg), \
             patch("src.api.cost_cache.get_cost_cache", return_value=cache):
            assert r._provider_credit_rank("p") == 0
        # Cache gone → unknown → rank 0.
        with patch("src.api.cost_cache.get_cost_cache",
                   side_effect=RuntimeError("boom")):
            assert r._provider_credit_rank("p") == 0


# ── CapabilityRouter: rules ──────────────────────────────────────────────────

class TestRouterRulesGaps:
    def test_rules_settings_crash_config_seeds(self, db):
        r = _rt(db)
        cfg = MagicMock()
        cfg.dynamic_routing = {"rules": [{"action": "block", "provider": "p"}]}
        with patch("src.api.cost_cache.get_settings", return_value=None):
            assert r._rules(cfg) == cfg.dynamic_routing["rules"]

    def test_rules_settings_crash_config_crash(self, db):
        r = _rt(db)
        cfg = MagicMock()
        type(cfg).dynamic_routing = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("no dr")))
        with patch("src.api.cost_cache.get_settings", return_value=None):
            assert r._rules(cfg) == []                       # 1786-1788

    def test_rules_typeerror_legacy(self, db):
        r = _rt(db)
        st = MagicMock()

        def gr(profile=None):
            if profile is not None:
                raise TypeError("legacy store")
            return [{"action": "prefer", "provider": "q"}]
        st.get_routing_rules.side_effect = gr
        with patch("src.api.cost_cache.get_settings", return_value=st):
            assert r._rules(None, "l2")[0]["provider"] == "q"  # 1776-1777

    def test_rules_get_settings_crash(self, db):
        r = _rt(db)
        with patch("src.api.cost_cache.get_settings",
                   side_effect=RuntimeError("gone")):
            assert r._rules(None) == []                      # 1780-1781

    def test_rule_matches_variants(self, db):
        r = _rt(db)
        assert r._rule_matches({"enabled": False}, "t", "p") is False  # 1794
        assert r._rule_matches({"profile": "x"}, "t", "p") is False    # 1797
        assert r._rule_matches({"task": "*"}, "t", "p") is True        # 1800
        assert r._rule_matches({"task": ["t", "u"]}, "t", "p") is True  # 1802
        assert r._rule_matches({"task": "other"}, "t", "p") is False   # 1803

    def test_apply_blocks_skips_empty_rule(self, db):
        r = _rt(db)
        chain = [{"provider": "p", "model": "m"}]
        with patch.object(r, "_rules", return_value=[
            {"action": "block"},                              # 1852 no target
            {"action": "block", "provider": "p"},             # provider-wide
            {"action": "block", "model": "x"},                # other model
        ]):
            out, fired, bp, bm = r._apply_blocks(chain, "t", "l2", None)
        assert out == [] and bp == {"p"}                      # 1855-1857
        assert [f["action"] for f in fired] == ["block"]      # targetless skipped

    def test_resolve_prefer_low_score_gate(self, db):
        r = _rt(db)
        chain = [{"provider": "p", "model": "m"}]
        with patch.object(r, "_rules", return_value=[
            {"action": "prefer", "provider": "p", "model": "target",
             "min_score": 0.9},
        ]), patch.object(r, "get_model_score", return_value=0.1), \
             patch.object(R, "logical_model_name", return_value="target"):
            tm, pp, fired = r._resolve_prefer(chain, "t", "l2", None)
        assert tm is None
        assert fired[0]["action"] == "prefer_skipped_low_score"  # 1900-1905

    def test_resolve_prefer_unserved(self, db):
        r = _rt(db)
        chain = [{"provider": "p", "model": "m"}]
        with patch.object(r, "_rules", return_value=[
            {"action": "prefer", "model": "target"},
        ]):
            tm, pp, fired = r._resolve_prefer(chain, "t", "l2", None)
        assert tm is None
        assert fired[-1]["action"] == "prefer_unserved"       # 1855-1856

    def test_resolve_prefer_provider_only(self, db):
        r = _rt(db)
        chain = [{"provider": "p", "model": "m"}]
        with patch.object(r, "_rules", return_value=[
            {"action": "prefer", "provider": "zzz"},          # no chain target
            {"action": "prefer", "provider": "p"},            # 1922-1926
        ]):
            tm, pp, fired = r._resolve_prefer(chain, "t", "l2", None)
        assert tm is None and pp == "p"

    def test_resolve_prefer_skips_targetless(self, db):
        r = _rt(db)
        with patch.object(r, "_rules", return_value=[{"action": "prefer"}]):
            tm, pp, fired = r._resolve_prefer([], "t", "l2", None)  # 1889-1890
        assert tm is None and pp is None and fired == []

    def test_apply_rules_no_rules(self, db):
        r = _rt(db)
        with patch.object(r, "_rules", return_value=[]):
            out, fired = r._apply_rules([{"provider": "p", "model": "m"}], "t", "l2")
        assert fired == [] and out                            # 1939-1940

    def test_apply_rules_provider_only_prefer(self, db):
        r = _rt(db)
        chain = [{"provider": "a", "model": "m1"},
                 {"provider": "b", "model": "m2"}]
        with patch.object(r, "_rules", return_value=[
            {"action": "prefer", "provider": "b"},
        ]):
            out, fired = r._apply_rules(chain, "t", "l2")     # 2030-2045
        assert out[0]["provider"] == "b"
        assert fired[0]["steps"] == 1


# ── CapabilityRouter: select_step paths ──────────────────────────────────────

class TestSelectStepGaps:
    def test_disabled_returns_none(self, db):
        r = CapabilityRouter(enabled=False, db_path=db)
        assert r.select_step([{"role": "user", "content": "hi"}],
                             chain=[{"provider": "p", "model": "m"}]) is None

    def test_empty_chain(self, db):
        r = _rt(db)
        assert r.select_step([{"role": "user", "content": "hi"}], chain=[]) is None  # 2083

    def test_all_providers_down(self, db):
        r = _rt(db)
        with patch.object(r, "_provider_available", return_value=False):
            assert r.select_step([{"role": "user", "content": "hi"}],
                                 chain=[{"provider": "p", "model": "m"}]) is None  # 2103-2104

    def test_all_blocked(self, db):
        r = _rt(db)
        with patch.object(r, "_apply_blocks",
                          return_value=([], [], {"p"}, set())):
            assert r.select_step([{"role": "user", "content": "hi"}],
                                 chain=[{"provider": "p", "model": "m"}]) is None  # 2116-2117

    def test_below_min_score_keeps_chain(self, db):
        r = _rt(db)
        chain = [{"provider": "p", "model": "m"}]
        with patch.object(r, "_apply_blocks",
                          return_value=(list(chain), [], set(), set())), \
             patch.object(r, "_resolve_prefer", return_value=(None, None, [])), \
             patch.object(r, "_candidate_models", return_value={"m"}), \
             patch.object(r, "_choose_target_model", return_value=None), \
             patch.object(r, "_score_model", return_value=0.2):
            assert r.select_step([{"role": "user", "content": "hi"}],
                                 chain=list(chain)) is None   # 2167-2180
        assert r._decisions[-1]["action"] == "below_min_score"

    def test_explore_keep_default(self, db):
        r = _rt(db)
        chain = [{"provider": "p", "model": "m"}]
        with patch.object(r, "_apply_blocks",
                          return_value=(list(chain), [], set(), set())), \
             patch.object(r, "_resolve_prefer", return_value=(None, None, [])), \
             patch.object(r, "_candidate_models", return_value={"m"}), \
             patch.object(r, "_choose_target_model", return_value="m"), \
             patch.object(r, "_build_chain_for_model", return_value=list(chain)), \
             patch.object(r, "_score_model", return_value=0.8), \
             patch.object(r, "_effective_policy",
                          return_value=("explore", 0.0)):
            out = r.select_step([{"role": "user", "content": "hi"}],
                                chain=list(chain))
        assert r._decisions[-1]["action"] == "keep_default"   # 2195-2196, 2200

    def test_build_chain_empty(self, db):
        r = _rt(db)
        chain = [{"provider": "p", "model": "m"}]
        with patch.object(r, "_apply_blocks",
                          return_value=(list(chain), [], set(), set())), \
             patch.object(r, "_resolve_prefer", return_value=("mm", None, [{"action": "prefer"}])), \
             patch.object(r, "_build_chain_for_model", return_value=[]), \
             patch.object(r, "_score_model", return_value=0.8):
            assert r.select_step([{"role": "user", "content": "hi"}],
                                 chain=list(chain)) is None   # 2186-2187

    def test_prefer_context_excluded(self, db):
        r = _rt(db)
        chain = [{"provider": "p", "model": "m"}]
        detail_cls = R.ClassifyResult(task="coding", path="default",
                                      token_count=500000)
        with patch.object(r, "_apply_blocks",
                          return_value=(list(chain), [], set(), set())), \
             patch.object(r, "_resolve_prefer", return_value=("mm", "p", [])), \
             patch.object(r, "_fits_context", return_value=False), \
             patch.object(r, "_candidate_models", return_value={"m"}), \
             patch.object(r, "_choose_target_model", return_value="m"), \
             patch.object(r, "_build_chain_for_model", return_value=list(chain)), \
             patch.object(r, "_score_model", return_value=0.8), \
             patch("src.api.router.classify_task_detail", return_value=detail_cls):
            r.select_step([{"role": "user", "content": "hi"}], chain=list(chain))
        assert "prefer_context_excluded" in str(r._decisions[-1])  # 2127-2133

    def test_policy_rule_override(self, db):
        r = _rt(db)
        chain = [{"provider": "p", "model": "m"}]
        with patch.object(r, "_apply_blocks",
                          return_value=(list(chain), [], set(), set())), \
             patch.object(r, "_rules", return_value=[
                 {"action": "policy", "policy": "explore"}]), \
             patch.object(r, "_resolve_prefer", return_value=(None, None, [])), \
             patch.object(r, "_candidate_models", return_value={"m"}), \
             patch.object(r, "_choose_target_model", return_value="m"), \
             patch.object(r, "_build_chain_for_model", return_value=list(chain)), \
             patch.object(r, "_score_model", return_value=0.8):
            r.select_step([{"role": "user", "content": "hi"}], chain=list(chain))
        # 2107-2111: policy overridden to explore; head unchanged → keep_default
        assert r._decisions[-1]["policy"] == "explore"


# ── module-level router facade ───────────────────────────────────────────────

class TestRouterFacadeGaps:
    def test_get_dynamic_router_resolve_exception(self):
        from src.api import runtime as rt_mod
        prev = rt_mod._active_runtime
        fake = MagicMock()
        fake.resolve.side_effect = KeyError("inactive")
        rt_mod._active_runtime = fake
        try:
            assert R.get_dynamic_router() is R._dynamic_router  # 2247-2248
        finally:
            rt_mod._active_runtime = prev

    def test_sync_settings_crash(self):
        prev = R._dynamic_router.enabled
        with patch("src.api.cost_cache.get_settings",
                   side_effect=RuntimeError("gone")):
            R.sync_router_enabled_from_settings()             # 2270-2271
        R._dynamic_router.enabled = prev

    def test_sync_with_override(self):
        prev = R._dynamic_router.enabled
        st = MagicMock()
        st.get_routing_enabled.return_value = True
        with patch("src.api.cost_cache.get_settings", return_value=st):
            assert R.sync_router_enabled_from_settings() is True
        R._dynamic_router.enabled = prev

    def test_component_setup_settings_crash(self, db):
        comp = R.RouterComponent(db_path=db, enabled=False)
        rt = MagicMock()
        rt.resolve.side_effect = KeyError("settings absent")
        assert comp.setup(rt) is None                         # 2328-2329
        assert comp.service is comp.router

    def test_component_setup_enabled_warms(self, db):
        comp = R.RouterComponent(db_path=db, enabled=True)
        rt = MagicMock()
        rt.resolve.side_effect = KeyError("no settings")
        comp.setup(rt)
        assert comp.router.enabled is True
        assert comp.router._matrix is not None                # 2330-2331


# ── routing_status ───────────────────────────────────────────────────────────

class TestRoutingStatusGaps:
    def test_selected_set_crash(self, db):
        r = _rt(db)
        R._dynamic_router = r
        cfg = MagicMock()
        type(cfg).profiles = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("duck")))
        with patch.object(R, "get_dynamic_router", return_value=r):
            out = R.routing_status(cfg)
        assert "per_task" in out                              # 2376-2377, 2385

    def test_per_task_no_scores(self, db):
        r = _rt(db)
        r._matrix = {"coding": {}}                            # 2392 skip empty
        cfg = MagicMock()
        cfg.profiles = {"l2": {"chain": [{"provider": "p", "model": "m"}]}}
        cfg.providers = {"p": {}}
        with patch.object(R, "get_dynamic_router", return_value=r), \
             patch.object(R, "logical_model_name", return_value="m"), \
             patch.object(R, "normalize_model_id", return_value="m"):
            out = R.routing_status(cfg)
        assert out["per_task"] == {}

    def test_matrix_crash(self, db):
        r = _rt(db)
        with patch.object(r, "load_matrix", side_effect=RuntimeError("boom")):
            with patch.object(R, "get_dynamic_router", return_value=r):
                out = R.routing_status(None)                   # 2401-2402
        assert "per_task" in out
