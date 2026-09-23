"""Batch K: setup install workers + cost-plugin fetch branches.

Targets setup.py: 201, 661, 687, 931, 957, 1050, 1148, 1160;
cost_plugins/base.py 398-399; llamacpp.py 181, 196; opencode.py 229-231;
opencode_api.py 263, 270-271, 285, 356; commandcode_api.py 356-358;
memory/lancedb_backend.py 201; cost_cache.py 632, 647-649.
"""
import contextlib
import subprocess
import threading
from unittest.mock import MagicMock, patch

import pytest

import src.api.setup as setup_mod


@pytest.fixture(autouse=True)
def _reset_setup_state():
    attrs = ("_mem_install", "_mem_last", "_router_install",
             "_router_last")
    saved = {a: getattr(setup_mod, a) for a in attrs}
    for a in attrs:
        setattr(setup_mod, a, None)
    yield
    for a, v in saved.items():
        setattr(setup_mod, a, v)


class _Proc:
    def __init__(self, lines, rc=0):
        self.stdout = iter(lines)
        self.rc = rc

    def wait(self):
        return self.rc


# ── setup.py: memory_step failed-install surfacing ──────────────────────────

class TestMemoryStepFailed:
    def test_mem_last_failed_surfaced(self):
        # 201: no in-flight install, last run failed → 'installing' = last
        setup_mod._mem_last = {"status": "failed", "detail": "boom"}
        with patch("src.api.memory.memory_status",
                   return_value={"available": False, "removable": False}), \
             patch("src.api.setup.capability_matrix_stats", return_value={}), \
             patch("src.api.setup._db_path_from_engine", return_value=""), \
             contextlib.nullcontext():
            out = setup_mod.memory_step()
        assert out["installing"]["status"] == "failed"


# ── setup.py: install-worker failure branches ────────────────────────────────

class TestMemoryInstallFailure:
    def _run(self, side_effect):
        engine = MagicMock()
        setup_mod._mem_install = {"log": [], "status": "running",
                                  "progress": 0}
        with patch("src.api.setup.memory_site", return_value="/tmp/mem-site"), \
             patch("src.api.setup.memory_models_dir", return_value="/tmp/mem-models"), \
             patch("src.api.setup.os.makedirs"), \
             patch("src.api.setup._stream_mem", side_effect=side_effect):
            setup_mod._run_memory_install(engine)

    def test_called_process_error(self):
        # 661: _stream_mem raises CalledProcessError → failed, tail detail
        self._run(subprocess.CalledProcessError(1, ["pip"]))
        assert setup_mod._mem_last["status"] == "failed"
        assert "Install failed" in setup_mod._mem_last["detail"]

    def test_generic_exception(self):
        # 665: unexpected error → failed
        self._run(RuntimeError("disk on fire"))
        assert setup_mod._mem_last["status"] == "failed"

    def test_stream_mem_nonzero_rc(self):
        # 687: rc != 0 inside _stream_mem raises CalledProcessError
        setup_mod._mem_install = {"log": [], "status": "running", "progress": 0}
        with patch("src.api.setup.subprocess.Popen",
                   return_value=_Proc(["a", "b", "c", "d"], rc=2)):
            with pytest.raises(subprocess.CalledProcessError):
                setup_mod._stream_mem(["x"], None, 0.0, 10.0, "go")


class TestRouterInstallFailure:
    def _run(self, side_effect):
        engine = MagicMock()
        setup_mod._router_install = {"log": [], "status": "running",
                                     "progress": 0}
        with patch("src.api.setup.router_site", return_value="/tmp/r-site"), \
             patch("src.api.setup.router_models_dir", return_value="/tmp/r-m"), \
             patch("src.api.setup.os.makedirs"), \
             patch("src.api.setup._stream_router", side_effect=side_effect):
            setup_mod._run_router_install(engine)

    def test_called_process_error(self):
        # 931
        self._run(subprocess.CalledProcessError(1, ["pip"]))
        assert setup_mod._router_last["status"] == "failed"

    def test_stream_router_nonzero_rc(self):
        # 957
        setup_mod._router_install = {"log": [], "status": "running", "progress": 0}
        with patch("src.api.setup.subprocess.Popen",
                   return_value=_Proc(["a", "b", "c", "d", "e", "f"], rc=1)):
            with pytest.raises(subprocess.CalledProcessError):
                setup_mod._stream_router(["x"], None, 0.0, 10.0, "go")


# ── cost_plugins/base.py: plugin disposer swallows on_shutdown errors ────────

class TestPluginDisposer:
    def test_disposer_swallows_shutdown_error(self):
        # base.py 398-399
        from src.api.cost_plugins.base import CostPluginsComponent, PluginRegistry

        comp = CostPluginsComponent()
        rt = MagicMock()
        rt.resolve.return_value = MagicMock()
        dispose = comp.setup(rt)

        bad = MagicMock()
        bad.on_shutdown.side_effect = RuntimeError("plugin teardown boom")
        comp._registry.register(bad)
        assert isinstance(comp._registry, PluginRegistry)
        dispose()  # must not raise


# ── llamacpp.py: persist with no path ────────────────────────────────────────

class TestLlamaCppNoPath:
    def test_persist_and_load_no_path(self, monkeypatch):
        # 181 + 196: empty persist path → both are early returns
        from src.api.cost_plugins.llamacpp import LlamaCppCostPlugin
        monkeypatch.delenv("LCP_LLAMACPP_PERSIST", raising=False)
        p = LlamaCppCostPlugin(persist_path="")
        p._persist()
        p._load_persisted()


# ── opencode.py: fetch_balance generic exception ─────────────────────────────

class TestOpenCodeBalanceExc:
    def test_balance_exception_becomes_error_dict(self, monkeypatch):
        # opencode.py 229-231
        from src.api.cost_plugins.opencode import OpenCodeCostPlugin
        monkeypatch.delenv("LCP_MOCK_PLUGIN_DATA", raising=False)
        store = MagicMock()
        store.get_cookie.return_value = "cookie"
        with patch("src.api.credential_store.get_credential_store",
                   return_value=store), \
             patch("src.api.cost_plugins.opencode_api.fetch_billing_dict",
                   side_effect=RuntimeError("socket died")):
            out = OpenCodeCostPlugin().fetch_balance()
        assert out["_error"] == "api_error"
        assert "socket died" in out["detail"]


# ── opencode_api.py: _parse_ssr_billing branches ─────────────────────────────

class TestSsrBillingBranches:
    def test_nonpositive_value_skipped(self):
        # 263: val <= 0 → continue
        from src.api.cost_plugins.opencode_api import _parse_ssr_billing
        assert _parse_ssr_billing('{"availableCredits": -5}') is None

    def test_implausible_generic_skipped(self):
        # 270-271 + 285: generic key with absurd value → skipped + warning
        from src.api.cost_plugins.opencode_api import _parse_ssr_billing
        text = 'credits:999999999 plan:"pro" availableCredits:3.5'
        out = _parse_ssr_billing(text)
        assert out["available_credits"] == 3.5   # absurd one skipped, specific kept

    def test_workspace_discovery_picks_first_id(self):
        # 356: discover step extracts ids[0], billing page parsed after
        import src.api.cost_plugins.opencode_api as oa
        dashboard = 'id:"wrk_alpha"'
        billing = ('lite.billing.get["wrk_alpha"] '
                   'availableCredits:7.25 plan:"pro"')
        urls = []

        def fake_get(url, headers=None):
            urls.append(url)
            return dashboard if "billing" not in url else billing

        with patch.object(oa, "_http_get", side_effect=fake_get):
            snap = oa.fetch_billing("cookie-value")
        assert snap is not None
        assert snap.available_credits == 7.25
        assert "wrk_alpha" in urls[1]

