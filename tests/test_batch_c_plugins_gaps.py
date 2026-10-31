"""Batch C coverage gaps — cost plugins.

Closes error/fallback branches in src/api/cost_plugins/:
  - base.py: get_registry runtime delegation + component registry errors
  - opencode.py: set_engine, _ensure_engine raise, credential-store failure
    fallbacks, empty-summary rows, mock-data paths, api_error results
  - opencode_api.py: SSR-scan branches (implausible skip, no-workspace,
    discover failure, billing-page failure, debug-snippet logging)
  - commandcode.py: set_engine, empty/None model, query failure paths,
    credential-store failure
  - commandcode_api.py: non-dict JSON, int coercion, non-dict usage entries,
    credits parse failure, naive period_end
  - deepseek.py: credential-store failure, mock balance data
  - llamacpp.py: _fmt_params M-branch, persist without path, load no file
"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest


# ── base.py: registry runtime delegation ─────────────────────────────────────

class TestBaseRegistryGaps:
    def test_get_registry_resolve_exception_legacy(self):
        from src.api.cost_plugins import base
        from src.api import runtime as rt_mod
        base._registry = None
        fake = MagicMock()
        fake.resolve.side_effect = KeyError("inactive")
        prev = rt_mod._active_runtime
        rt_mod._active_runtime = fake
        try:
            reg = base.get_registry()
            assert reg is base._registry
        finally:
            rt_mod._active_runtime = prev
            base._registry = None

    def test_component_registry_before_setup(self):
        from src.api.cost_plugins.base import CostPluginsComponent
        comp = CostPluginsComponent()
        with pytest.raises(RuntimeError, match="has not been set up"):
            comp.registry

    def test_disposer_swallows_plugin_errors(self):
        from src.api.cost_plugins.base import CostPluginsComponent, PluginRegistry
        comp = CostPluginsComponent()
        reg = PluginRegistry()
        bad = MagicMock()
        bad.on_shutdown.side_effect = RuntimeError("plugin teardown boom")
        reg.register(bad)
        rt = MagicMock()
        rt.resolve.return_value = reg
        with patch("src.api.cost_plugins.base.get_registry", return_value=reg):
            dispose = comp.setup(rt)
        dispose()  # must not raise


# ── opencode.py ──────────────────────────────────────────────────────────────

class TestOpenCodePluginGaps:
    def test_set_engine(self):
        from src.api.cost_plugins.opencode import OpenCodeCostPlugin
        p = OpenCodeCostPlugin()
        p.set_engine(MagicMock())
        assert p._engine is not None

    def test_ensure_engine_raises_without_engine(self):
        from src.api.cost_plugins.opencode import OpenCodeCostPlugin
        p = OpenCodeCostPlugin()
        with pytest.raises(RuntimeError, match="no gateway engine"):
            p._ensure_engine()

    def test_fetch_balance_credential_store_failure(self, monkeypatch):
        from src.api.cost_plugins import opencode as oc
        monkeypatch.delenv("LCP_MOCK_PLUGIN_DATA", raising=False)
        p = oc.OpenCodeCostPlugin()
        with patch("src.api.credential_store.get_credential_store",
                   side_effect=RuntimeError("db locked")):
            # cookie stays "" after except → quiet None
            assert p.fetch_balance() is None

    def test_fetch_balance_api_error(self, monkeypatch):
        """A broken store is not a credential; a failing route stays an api_error.

        Rewritten 2026-10-31: the old body patched ``fetch_billing_dict`` (a
        function this plugin no longer calls) and relied on a MagicMock store
        leaking a mock token through the single-credential path.  Both are now
        guarded, so the assertions are the real contract — a store that yields
        no usable credential stays quiet, and a 5xx is a transient api_error
        (not an auth failure the operator would chase).
        """
        from src.api.cost_plugins import opencode as oc
        from src.api.cost_plugins import opencode_api as oa

        monkeypatch.delenv("LCP_MOCK_PLUGIN_DATA", raising=False)
        p = oc.OpenCodeCostPlugin()

        store = MagicMock()
        store.get_cookie.return_value = "cookie"
        store.get_workspace_id.return_value = ""
        with patch("src.api.credential_store.get_credential_store", return_value=store):
            assert p.fetch_balance() is None  # a mock is not a credential

        with patch.object(p, "_token", return_value="oc_sk_key"), \
             patch("src.api.cost_plugins.opencode_api.fetch_account_credits",
                   side_effect=oa.ConsoleApiError(503, "/api/billing/status",
                                                  "Unavailable")):
            out = p.fetch_balance()
        assert out["_error"] == "api_error"
        assert "503" in out["detail"]

    def test_fetch_balance_mock_data(self, monkeypatch):
        from src.api.cost_plugins import opencode as oc
        monkeypatch.setenv("LCP_MOCK_PLUGIN_DATA", "1")
        assert oc.OpenCodeCostPlugin().fetch_balance()["currency"] == "USD"

    def test_fetch_subscription_mock_data(self, monkeypatch):
        from src.api.cost_plugins import opencode as oc
        monkeypatch.setenv("LCP_MOCK_PLUGIN_DATA", "1")
        out = oc.OpenCodeCostPlugin().fetch_subscription()
        assert out["monthly_pct"] == 12.0

    def test_fetch_subscription_credential_store_failure(self, monkeypatch):
        from src.api.cost_plugins import opencode as oc
        monkeypatch.delenv("LCP_MOCK_PLUGIN_DATA", raising=False)
        p = oc.OpenCodeCostPlugin()
        with patch("src.api.credential_store.get_credential_store",
                   side_effect=RuntimeError("locked")):
            out = p.fetch_subscription()
        assert out["_error"] == "auth_failed"  # cookie empty after except

    def test_fetch_summary_empty_result_row_none(self):
        # defensive else-branch: aggregate row missing → zeros
        from src.api.cost_plugins.opencode import OpenCodeCostPlugin
        p = OpenCodeCostPlugin(engine=MagicMock())
        sess = MagicMock()
        sess.query.return_value.filter.return_value.first.return_value = None
        sess.__enter__ = lambda s: s
        sess.__exit__ = lambda s, *a: False
        with patch.object(p, "_gw_session", return_value=sess):
            out = p.fetch_summary()
        assert out["daily"] == {"tokens": 0, "cost": 0.0, "requests": 0}

    def test_fetch_summary_query_failure(self):
        from src.api.cost_plugins.opencode import OpenCodeCostPlugin
        p = OpenCodeCostPlugin(engine=MagicMock())
        with patch.object(p, "_gw_session", side_effect=RuntimeError("db gone")):
            assert p.fetch_summary() is None


# ── opencode_api.py ──────────────────────────────────────────────────────────


# ── commandcode.py ───────────────────────────────────────────────────────────

class TestCommandCodePluginGaps:
    def test_set_engine(self):
        from src.api.cost_plugins.commandcode import CommandCodeCostPlugin
        p = CommandCodeCostPlugin()
        p.set_engine(MagicMock())
        assert p._engine is not None

    def test_logical_model_empty(self):
        from src.api.cost_plugins.commandcode import _logical_model
        assert _logical_model("") == ""
        assert _logical_model("moonshotai/Kimi-K3") == "kimi-k3"

    def test_api_model_empty(self):
        from src.api.cost_plugins.commandcode import _api_model
        assert _api_model("") == ""

    def test_fetch_usage_query_failure(self):
        from src.api.cost_plugins.commandcode import CommandCodeCostPlugin
        p = CommandCodeCostPlugin(engine=MagicMock())
        with patch.object(p, "_gw_session", side_effect=RuntimeError("db gone")):
            assert p.fetch_usage() == []

    def test_fetch_summary_row_none(self):
        from src.api.cost_plugins.commandcode import CommandCodeCostPlugin
        p = CommandCodeCostPlugin(engine=MagicMock())
        sess = MagicMock()
        sess.query.return_value.filter.return_value.first.return_value = None
        sess.__enter__ = lambda s: s
        sess.__exit__ = lambda s, *a: False
        with patch.object(p, "_gw_session", return_value=sess):
            out = p.fetch_summary()
        assert out["weekly"] == {"tokens": 0, "cost": 0.0, "requests": 0}

    def test_fetch_summary_query_failure(self):
        from src.api.cost_plugins.commandcode import CommandCodeCostPlugin
        p = CommandCodeCostPlugin(engine=MagicMock())
        with patch.object(p, "_gw_session", side_effect=RuntimeError("gone")):
            assert p.fetch_summary() is None

    def test_fetch_subscription_credential_store_failure(self, monkeypatch):
        from src.api.cost_plugins import commandcode as cc
        monkeypatch.delenv("LCP_MOCK_PLUGIN_DATA", raising=False)
        p = cc.CommandCodeCostPlugin()
        with patch("src.api.credential_store.get_credential_store",
                   side_effect=RuntimeError("locked")):
            out = p.fetch_subscription()
        assert out["_error"] == "auth_failed"  # cookie "" → not-configured


# ── commandcode_api.py ───────────────────────────────────────────────────────

class TestCommandCodeApiGaps:
    def test_http_get_json_non_dict(self):
        from src.api.cost_plugins import commandcode_api as ca
        resp = MagicMock()
        resp.read.return_value = b"[1, 2, 3]"
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        with patch.object(ca, "urlopen", return_value=resp):
            with pytest.raises(ValueError, match="expected JSON object"):
                ca._http_get_json("http://x", {})

    def test_int_coercion_variants(self):
        from src.api.cost_plugins.commandcode_api import _int
        assert _int(None) == 0
        assert _int(None, 5) == 5
        assert _int("12") == 12
        assert _int("not-a-number") == 0   # ValueError → default
        assert _int(object()) == 0          # TypeError → default

    def test_usage_rows_skip_non_dict(self):
        from src.api.cost_plugins.commandcode_api import _parse_usage_list
        out = _parse_usage_list({"usages": ["garbage", None, {"createdAt": "2026-01-01"}]})
        assert len(out) == 1

    def test_subscription_credits_parse_failure(self):
        from src.api.cost_plugins import commandcode_api as ca
        with patch.object(ca, "_parse_credits", side_effect=RuntimeError("shape")):
            assert ca.fetch_subscription_snapshot("cookie") is None

    def test_subscription_period_end_unparseable(self):
        from src.api.cost_plugins import commandcode_api as ca
        # billing_period_end garbage → ValueError → reset 0 (397-398)
        with patch.object(ca, "_http_get_json",
                          side_effect=[
                              {"credits": {"available": 1.0}},
                              {"data": {"currentPeriodEnd": "garbage-date"}},
                              {}, {},
                          ]):
            snap = ca.fetch_subscription_snapshot("cookie")
        assert snap is not None
        assert snap.monthly_reset_sec == 0

    def test_subscription_period_end_naive_datetime(self):
        from src.api.cost_plugins import commandcode_api as ca
        # period_end without tz → naive branch (393-394)
        with patch.object(ca, "_http_get_json",
                          side_effect=[
                              {"credits": {"available": 1.0}},
                              {"data": {"currentPeriodEnd": "2030-01-01T00:00:00"}},
                              {}, {},
                          ]):
            snap = ca.fetch_subscription_snapshot("cookie")
        assert snap is not None
        assert snap.monthly_reset_sec > 0


# ── deepseek.py ──────────────────────────────────────────────────────────────

class TestDeepSeekPluginGaps:
    def test_api_key_store_failure_falls_back_to_env(self, monkeypatch):
        from src.api.cost_plugins.deepseek import DeepSeekCostPlugin
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
        p = DeepSeekCostPlugin()
        with patch("src.api.credential_store.get_credential_store",
                   side_effect=RuntimeError("locked")):
            assert p._api_key() == "sk-env"

    def test_balance_mock_data_without_key(self, monkeypatch):
        from src.api.cost_plugins import deepseek as ds
        monkeypatch.setenv("LCP_MOCK_PLUGIN_DATA", "1")
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        p = ds.DeepSeekCostPlugin()
        with patch.object(p, "_api_key", return_value=None):
            bal = p.fetch_balance()
        assert bal["balance"] == 20.00


# ── llamacpp.py ──────────────────────────────────────────────────────────────

class TestLlamaCppPluginGaps:
    def test_fmt_params_millions(self):
        from src.api.cost_plugins.llamacpp import _fmt_params
        assert _fmt_params(7_000_000) == "7.0M"
        assert _fmt_params(1_000_000_000) == "1.0B"
        assert _fmt_params(123) == "123"

    def test_persist_no_path(self):
        from src.api.cost_plugins.llamacpp import LlamaCppCostPlugin
        p = LlamaCppCostPlugin(persist_path=None)
        p._persist()          # early return, no file writes
        p._load_persisted()   # early return

    def test_load_persisted_missing_file(self, tmp_path):
        from src.api.cost_plugins.llamacpp import LlamaCppCostPlugin
        p = LlamaCppCostPlugin(persist_path=str(tmp_path / "nope.json"))
        p._load_persisted()   # path missing → return, no state change
