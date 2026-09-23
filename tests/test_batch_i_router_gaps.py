"""Batch I: router.py residual gap sweep (last ~24 stmts).

Targets term-missing lines: 520, 812, 877, 883, 1067, 1509-1510, 1514-1515,
1604, 1858-1859, 1950, 1975, 2007, 2011, 2016, 2081-2082, 2327, 2368,
2374-2375, 2398.
"""
import sys
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


def _cfg(profiles=None):
    cfg = MagicMock()
    cfg.profiles = profiles or {}
    cfg.providers = {}
    cfg.dynamic_routing = {}
    cfg.model_limits = {}
    return cfg


# ── helpers / classification ─────────────────────────────────────────────────

class TestPreamble520:
    def test_specific_keyword_blocks_preamble(self):
        # 519-520: strong multi-word task keyword → not preamble-like
        text = "step by step " * 20
        assert R._is_preamble_like(text) is False


class TestCasual812:
    def test_casual_via_combined_conversation(self):
        # 810-816: casual keyword lives only in the assistant turn
        with patch("src.api.task_classifier.get_semantic_classifier",
                   return_value=None), \
             patch("src.api.router.count_tokens", return_value=5):
            res = R.classify_task_detail([
                {"role": "assistant", "content": "hello there"},
                {"role": "user", "content": "sup"},
            ])
        assert res.task == "casual_chat"
        assert res.path == "casual"


class TestLogicalModelEmpty:
    def test_empty_model_shortcut(self, db):
        # 1067: falsy model returned as-is
        assert R.logical_model_name("", db) == ""


class TestCoerceContext:
    def test_nan_fails_int_conversion(self, db):
        # 1509-1510: int(nan) raises ValueError → None
        assert _rt(db)._coerce_context(float("nan")) is None

    def test_unicode_digit_string_fails_int(self, db):
        # 1514-1515: isdigit True but int() raises ValueError → None
        assert _rt(db)._coerce_context("\u00b2") is None


class TestChooseTargetEmpty:
    def test_no_candidates_returns_none(self, db):
        # 1604: empty candidate set → None
        assert _rt(db)._choose_target_model(
            set(), "coding", "eager", 0.0, 0.0, "m-default") is None


# ── rules: block/prefer branches ─────────────────────────────────────────────

class TestApplyBlocksModelOnly:
    def test_model_only_block_records_logical(self, db):
        # 1858-1859: model-only block that removes steps
        r = _rt(db)
        chain = [{"provider": "p", "model": "m-blocked"},
                 {"provider": "p", "model": "m-keep"}]
        with patch.object(r, "_rules", return_value=[
                {"action": "block", "model": "m-blocked"}]):
            cands, fired, bprov, bmodels = r._apply_blocks(chain, "coding", "l2")
        assert len(cands) == 1
        assert "m-blocked" in bmodels
        assert fired[0]["action"] == "block"


class TestApplyRulesNoTarget:
    def test_rules_without_target_skipped(self, db):
        # 1950 + 1975: block/prefer rules with neither provider nor model
        r = _rt(db)
        chain = [{"provider": "p", "model": "m"}]
        with patch.object(r, "_rules", return_value=[
                {"action": "block"}, {"action": "prefer"}]):
            cands, fired = r._apply_rules(chain, "coding", "l2")
        assert cands == chain and fired == []

    def test_prefer_provider_unserved_skipped(self, db):
        # 2007: provider mismatch inside model-prefer expansion → no steps
        r = _rt(db)
        chain = [{"provider": "p1", "model": "m1"}]
        with patch.object(r, "_rules", return_value=[
                {"action": "prefer", "provider": "p2", "model": "mm"}]):
            cands, fired = r._apply_rules(chain, "coding", "l2")
        assert cands == chain and fired == []            # preferred empty → 2016

    def test_prefer_base_url_carried(self, db):
        # 1953-1954: expanded step copies base_url from the chain step
        # (chain-as-source-of-truth — the chain's model must be the
        # preferred logical model for the expansion to happen at all).
        r = _rt(db)
        chain = [{"provider": "p", "model": "mm", "base_url": "https://b/v1"}]
        with patch.object(r, "_rules", return_value=[
                {"action": "prefer", "model": "mm"}]), \
             patch("src.api.router.provider_model_name", return_value="api-mm"):
            cands, fired = r._apply_rules(chain, "coding", "l2")
        assert cands[0]["base_url"] == "https://b/v1"
        assert cands[0]["model"] == "api-mm"
        assert fired[0]["action"] == "prefer"


# ── select_step: duck-typed is_enabled TypeError fallback ───────────────────

class TestSelectStepTypeErrorFallback:
    def test_is_enabled_single_arg(self, db):
        # 2081-2082: is_enabled(config, profile) raises TypeError → retry 1-arg
        r = _rt(db)

        def is_enabled(config):
            return False

        with patch.object(r, "is_enabled", side_effect=is_enabled):
            assert r.select_step([{"role": "user", "content": "hi"}],
                                 chain=[{"provider": "p", "model": "m"}]) is None


# ── RouterComponent: settings toggle override ────────────────────────────────

class TestRouterComponentOverride:
    def test_persisted_toggle_applied(self, db):
        # 2327: settings.get_routing_enabled returns False → router.enabled False
        comp = R.RouterComponent(db_path=db, enabled=True)
        rt = MagicMock()
        rt.resolve.return_value.store.get_routing_enabled.return_value = False
        assert comp.setup(rt) is None
        assert comp.router.enabled is False


# ── routing_status gaps ──────────────────────────────────────────────────────

class TestRoutingStatusGaps:
    def _status(self, db, r, cfg):
        with patch.object(R, "get_dynamic_router", return_value=r):
            return R.routing_status(cfg)

    def test_step_without_model_skipped(self, db):
        # 2368: chain step lacking a model key → selected stays empty,
        # and with no selection info routing_status falls back to overall top
        r = _rt(db)
        with patch.object(r, "load_matrix", return_value={"coding": {"m": 0.9}}):
            out = self._status(db, r, _cfg(
                {"l2": {"chain": [{"provider": "p"}]}}))
        assert out["per_task"]["coding"]["model"] == "m"

    def test_benchmark_name_crash_swallowed(self, db):
        # 2374-2375: benchmark_model_name raises → per-model continue
        r = _rt(db)
        with patch("src.api.router.benchmark_model_name",
                   side_effect=RuntimeError("no registry")), \
             patch.object(r, "load_matrix",
                          return_value={"coding": {"m-a": 0.7}}):
            out = self._status(db, r, _cfg(
                {"l2": {"chain": [{"provider": "p", "model": "m-a"}]}}))
        assert out["per_task"]["coding"]["model"] == "m-a"

    def test_selected_excludes_all_candidates(self, db):
        # 2398: matrix models outside the profile selection → task skipped
        r = _rt(db)
        with patch("src.api.router.logical_model_name", side_effect=lambda m, p: m), \
             patch("src.api.router.normalize_model_id", side_effect=lambda m: m), \
             patch("src.api.router.benchmark_model_name", side_effect=lambda m, p: m), \
             patch.object(r, "load_matrix",
                          return_value={"coding": {"zzz-other": 0.9}}):
            out = self._status(db, r, _cfg(
                {"l2": {"chain": [{"provider": "p", "model": "m-a"}]}}))
        assert "coding" not in out["per_task"]
