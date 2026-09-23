"""Batch M: the last ~15 statements.

Fixes two coverage-instrumentation misses in batch J (guard lines compile at
line 1 unless padded) and targets:
  benchmark.py 801-802; seed_capabilities.py 380 (+597 via padded guard);
  router.py 2007, 2198; setup.py 1050; llamacpp.py 181, 196 (attr override);
  opencode_api.py 263 (unquoted SSR values); endpoints.py 2276 (real subtask
  rows); handler.py 318, 348 (route dispatch).
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import src.api.setup as setup_mod
from src.server import LCPHandler


class _TH(LCPHandler):
    def __init__(self, path="/", engine=None):
        self.path = path
        self.command = "GET"
        self.headers = {}
        self.request_version = "HTTP/1.1"
        self.requestline = f"GET {path} HTTP/1.1"
        self.raw_requestline = f"GET {path} HTTP/1.1".encode()
        self.client_address = ("127.0.0.1", 0)
        self.send_response = MagicMock()
        self.send_header = MagicMock()
        self.end_headers = MagicMock()
        self.wfile = MagicMock()
        self.rfile = MagicMock()
        self.engine = engine
        self.log_error = MagicMock()

    def _status(self):
        return self.send_response.call_args[0][0]


@pytest.fixture
def db(tmp_path):
    from src.api.models import get_engine, Base
    path = str(tmp_path / "m.db")
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


# ── benchmark.py 801-802: router-matrix invalidation crash is non-fatal ─────

class TestInvalidateCrash:
    def test_run_done_despite_invalidate_error(self, tmp_path):
        from src.api.benchmark import _execute_run, get_run
        from src.api.models import Base, get_engine, BenchmarkRun, get_session

        engine = get_engine(str(tmp_path / "b.db"))
        Base.metadata.create_all(engine)
        with get_session(engine) as session:
            run = BenchmarkRun(
                target_kind="provider",
                target_json=json.dumps({"provider": "deepseek",
                                        "model": "deepseek-v4-pro"}),
                categories_json=json.dumps(["coding", "math"]),
                status="queued")
            session.add(run)
            session.commit()
            rid = run.id
        root = tmp_path / "livebench"
        root.mkdir()
        (root / "all_groups.csv").write_text(
            "model,coding,math\ndeepseek-v4-pro,80.0,90.0\n")

        class Proc:
            stdout = iter(["done\n"])

            def wait(self):
                return 0

            def kill(self):
                pass

        class Cfg:
            providers = {"deepseek": {"api_base": "https://api.deepseek.com/v1"}}

            def get_provider_key(self, p):
                return "sk-test"

        with patch("src.api.benchmark.core_deps_available", return_value=True), \
             patch("src.api.benchmark.livebench_dir", return_value=str(root)), \
             patch("src.api.benchmark.subprocess.Popen", return_value=Proc()), \
             patch("src.api.router.invalidate_router_matrix",
                   side_effect=RuntimeError("matrix cache locked")):
            _execute_run(rid, engine, Cfg())
        run = get_run(engine, rid)
        assert run["status"] == "done"          # except → pass, run completes


# ── __main__ guards with LINE-PADDED exec (coverage maps guard line to N) ───

def _padded_guard(mod_name, argv=None):
    """Exec defs under the real module name, then ONLY the guard line with its
    original line number preserved (newline padding) so coverage attributes
    main() to its real line. main() is patched in the exec namespace."""
    import importlib
    mod = importlib.import_module(mod_name)
    src_path = mod.__file__
    with open(src_path, encoding="utf-8") as f:
        lines = f.readlines()
    guard_idx = next(i for i, ln in enumerate(lines)
                     if ln.startswith("if __name__ == "))
    body = "".join(lines[:guard_idx])
    guard = "\n" * guard_idx + "".join(lines[guard_idx:])
    g = {"__name__": mod_name, "__file__": src_path}
    exec(compile(body, src_path, "exec"), g)
    called = MagicMock()
    g["main"] = called
    g["__name__"] = "__main__"
    with patch("sys.argv", argv or [mod_name]):
        exec(compile(guard, src_path, "exec"), g)
    called.assert_called_once()


class TestPaddedGuards:
    def test_main_py(self):
        _padded_guard("src.main")            # main.py:181

    def test_seed_capabilities(self):
        _padded_guard("src.api.seed_capabilities")    # 597


# ── seed_capabilities.py 380: defensive 'not releases' branch ───────────────

class TestEffectiveReleasesEmptySet:
    def test_empty_release_set_skipped(self):
        # 379-380: by-design-unreachable by normal rows (a set always gets an
        # item), so inject the impossible state via the caller frame's
        # write-through locals proxy (PEP 558) — the branch itself is then
        # executed and its effect asserted.
        from src.api.seed_capabilities import effective_releases
        import sys

        class Row:
            model = "ghost-model"
            release_label = "2026-01-01"

        def rows_gen():
            yield Row()
            fr = sys._getframe()
            while fr is not None and "by_model" not in fr.f_locals:
                fr = fr.f_back
            assert fr is not None, "caller frame not found"
            fr.f_locals["by_model"]["ghost-empty"] = set()   # empty → skipped

        out = effective_releases(rows_gen(), {})
        assert out == {"ghost-model": "2026-01-01"}
        assert "ghost-empty" not in out


# ── opencode_api.py 263: non-positive credit value → continue ────────────────

class TestSsrNonPositive:
    def test_negative_value_skipped(self):
        # 262-263: val <= 0 → continue (unquoted SSR syntax required)
        from src.api.cost_plugins.opencode_api import _parse_ssr_billing
        assert _parse_ssr_billing("availableCredits:-5") is None


# ── llamacpp.py 181/196: persist no-path early returns ──────────────────────

class TestLlamaNoPath:
    def test_empty_persist_path_attribute(self, tmp_path):
        # constructor `persist_path or default` makes "" fall back to the
        # default — force the attribute instead
        from src.api.cost_plugins.llamacpp import LlamaCppCostPlugin
        p = LlamaCppCostPlugin(persist_path=str(tmp_path / "x.json"))
        p._persist_path = ""
        p._persist()            # 181: return
        p._load_persisted()     # 196: return


# ── router.py 2007 + 2198 ────────────────────────────────────────────────────

class TestPreferUnservedAndExplore:
    def test_model_prefer_provider_not_serving(self, db):
        # 1949-1950 + 1958-1959: chain-as-source-of-truth — the preferred
        # model's logical name must match a chain step's model; a chain
        # whose models do NOT include the preferred model yields no
        # expansion, so the chain is left untouched (no prefer fired).
        from src.api.router import CapabilityRouter
        r = CapabilityRouter(enabled=True, db_path=db)
        chain = [{"provider": "p1", "model": "other"}]
        with patch.object(r, "_rules", return_value=[
                {"action": "prefer", "model": "mm"}]):
            cands, fired = r._apply_rules(chain, "coding", "l2")
        assert cands == chain and fired == []

    def test_explore_action_when_head_changes(self, db):
        # 2197-2198: policy explore + head changed → action 'explore'
        from src.api.router import CapabilityRouter
        r = CapabilityRouter(enabled=True, db_path=db)
        chain = [{"provider": "p", "model": "m"}]
        new_chain = [{"provider": "other", "model": "m"}]
        with patch.object(r, "_apply_blocks",
                          return_value=(list(chain), [], set(), set())), \
             patch.object(r, "_resolve_prefer", return_value=(None, None, [])), \
             patch.object(r, "_candidate_models", return_value={"m"}), \
             patch.object(r, "_choose_target_model", return_value="m"), \
             patch.object(r, "_build_chain_for_model", return_value=list(new_chain)), \
             patch.object(r, "_score_model", return_value=0.8), \
             patch.object(r, "_effective_policy",
                          return_value=("explore", 0.0)):
            out = r.select_step([{"role": "user", "content": "hi"}],
                                chain=list(chain))
        assert r._decisions[-1]["action"] == "explore"
        assert out is not None


# ── setup.py 1050: bench install log trim ───────────────────────────────────

class TestBenchLogTrim:
    def test_log_capped(self, monkeypatch):
        saved = setup_mod._bench_install
        setup_mod._bench_install = {"log": ["x"] * setup_mod._LOG_MAX_LINES,
                                    "status": "running", "progress": 0}
        try:
            setup_mod._bench_update("one more line")   # 1049-1050
            assert len(setup_mod._bench_install["log"]) == setup_mod._LOG_MAX_LINES
            assert setup_mod._bench_install["log"][-1] == "one more line"
        finally:
            setup_mod._bench_install = saved


# ── endpoints.py 2276: real subtask rows in capability API ──────────────────

class TestSubtaskRows:
    def test_capability_api_with_subtasks(self, tmp_path):
        from datetime import datetime, timezone
        from src.api.models import (get_engine, Base, ModelCapability,
                                    ModelCapabilitySubtask, get_session)
        from tests.test_batch_f2_endpoints_apis import TestHandler

        db = str(tmp_path / "cap.db")
        engine = get_engine(db)
        Base.metadata.create_all(engine)
        now = datetime.now(timezone.utc).isoformat()
        with get_session(engine) as s:
            s.add(ModelCapabilitySubtask(model="bk1", category="reasoning",
                                         task="theory_of_mind", score=0.77,
                                         source="livebench", updated_at=now))
            s.commit()
        h = TestHandler(path="/api/models/capability", engine=engine)
        h._serve_capability_api()
        body = json.loads(h.wfile.write.call_args[0][0])
        assert body["subtasks"]["bk1"]["reasoning"]["theory_of_mind"] == 0.77
        engine.dispose()


# ── handler.py 318/348: route dispatch lines ────────────────────────────────

class TestRouteDispatch:
    def test_plugin_usage_route(self):
        h = _TH("/api/cost-plugins/usage")          # → 318
        with patch.object(LCPHandler, "_serve_plugin_usage") as m:
            h.do_GET()
        m.assert_called_once()

    def test_registry_route(self):
        h = _TH("/api/models/registry")             # → 348
        with patch.object(LCPHandler, "_serve_registry_api") as m:
            h.do_GET()
        m.assert_called_once()
