"""Batch D coverage gaps — setup.py install machinery + cost_cache.py.

Closes error/fallback branches in:
  - src/api/setup.py: _db_path_from_engine duck-engine, memory/router install
    threads (queued join, CalledProcessError/FileNotFoundError/generic excepts,
    pre-download skip, availability-probe failure, tail-detail fallbacks,
    log trimming, memory/router install failure paths,
    capability gate matrix-exception fallback
  - src/api/cost_cache.py: plugin_supports unknown kind, _ensure_loaded race,
    float coercion fallback, _clear_key DB failure, cache payload JSON failure,
    invalidate kind filter, entries/is_stale bad timestamps, refresher
    start-idempotence / registry exceptions / pass crash / scrape crash /
    quiet path, component services, get_settings/get_cost_cache resolve except
"""
import contextlib
import os
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.api import setup as setup_mod
from src.api import cost_cache as cc_mod


@pytest.fixture(autouse=True)
def _install_state_cleanup():
    for attr in ("_mem_install", "_mem_last", "_router_install",
                 "_router_last"):
        setattr(setup_mod, attr, None)
    yield
    for attr in ("_mem_install", "_mem_last", "_router_install",
                 "_router_last"):
        setattr(setup_mod, attr, None)


@pytest.fixture
def db_engine():
    """An engine (not the conftest tuple) for setup functions that call set_state."""
    from src.api.models import get_engine, Base
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _inflight():
    return {"status": "running", "progress": 0.0, "detail": "", "log": [],
            "started_at": "now", "updated_at": "now"}


# ── setup.py: helpers ────────────────────────────────────────────────────────

class TestSetupHelperGaps:
    def test_db_path_duck_engine_exception(self):
        class Explodes:
            @property
            def url(self):
                raise RuntimeError("no url")
        assert setup_mod._db_path_from_engine(Explodes()) is None  # 125-126

    def test_router_blocked_reason_matrix_exception(self, tmp_path):
        db = str(tmp_path / "x.db")
        with patch("src.api.seed_capabilities.load_capability_matrix",
                   side_effect=RuntimeError("db locked")):  # 248-249
            with contextlib.nullcontext():
                assert setup_mod.router_install_blocked_reason(db) is not None

    def test_router_blocked_reason_matrix_empty(self, tmp_path):
        db = str(tmp_path / "x.db")
        with patch("src.api.seed_capabilities.load_capability_matrix",
                   return_value={}), \
             contextlib.nullcontext():
            assert setup_mod.router_install_blocked_reason(db) is not None


# ── setup.py: memory install ─────────────────────────────────────────────────

class TestMemoryInstallGaps:
    def test_start_joins_inflight(self, db_engine):
        setup_mod._mem_install = _inflight()
        state = setup_mod.start_memory_install(db_engine)  # 597
        assert state["status"] == "running"

    def test_mem_update_noop_when_idle(self):
        setup_mod._mem_install = None
        setup_mod._mem_update("msg", progress=50.0)  # 553 return, no crash

    def test_mem_update_trims_log(self):
        setup_mod._mem_install = _inflight()
        setup_mod._mem_install["log"] = ["x"] * setup_mod._LOG_MAX_LINES
        setup_mod._mem_update("fresh line")  # 565
        assert len(setup_mod._mem_install["log"]) == setup_mod._LOG_MAX_LINES

    def test_tail_mem_detail_variants(self):
        setup_mod._mem_install = None
        assert setup_mod._tail_mem_detail("fb") == "fb"          # 694
        setup_mod._mem_install = {"log": []}
        assert setup_mod._tail_mem_detail("fb") == "fb"          # 697
        setup_mod._mem_install = {"log": ["   ", "\t"]}
        assert setup_mod._tail_mem_detail("fb") == "fb"          # 710→712

    def test_run_mem_install_probe_failure(self, db_engine, monkeypatch, tmp_path):
        monkeypatch.setattr(setup_mod, "memory_site", lambda: str(tmp_path / "s"))
        monkeypatch.setattr(setup_mod, "memory_models_dir", lambda: str(tmp_path / "m"))
        monkeypatch.setattr(setup_mod, "_stream_mem", lambda *a, **k: None)
        monkeypatch.setattr(setup_mod.os, "makedirs", lambda *a, **k: None)
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: False)
        setup_mod._mem_install = _inflight()
        setup_mod._run_memory_install(db_engine)  # SetupError → generic except
        assert setup_mod.mem_last()["status"] == "failed"

    def test_run_mem_install_predownload_skip(self, db_engine, monkeypatch, tmp_path):
        monkeypatch.setattr(setup_mod, "memory_site", lambda: str(tmp_path / "s"))
        monkeypatch.setattr(setup_mod, "memory_models_dir", lambda: str(tmp_path / "m"))
        monkeypatch.setattr(setup_mod.os, "makedirs", lambda *a, **k: None)
        monkeypatch.setattr("src.api.memory.memory_available", lambda site=None: True)

        def stream(cmd, **k):
            if "EmbeddingModel" in " ".join(cmd):
                raise subprocess.CalledProcessError(1, cmd)
        monkeypatch.setattr(setup_mod, "_stream_mem", stream)
        setup_mod._mem_install = _inflight()
        setup_mod._run_memory_install(db_engine)
        assert setup_mod.mem_last()["status"] == "done"  # 654-656 non-fatal

    def test_run_mem_install_filenotfound(self, db_engine, monkeypatch, tmp_path):
        monkeypatch.setattr(setup_mod, "memory_site", lambda: str(tmp_path / "s"))
        monkeypatch.setattr(setup_mod, "memory_models_dir", lambda: str(tmp_path / "m"))
        monkeypatch.setattr(setup_mod.os, "makedirs", lambda *a, **k: None)

        def stream(cmd, **k):
            raise FileNotFoundError("pip")
        monkeypatch.setattr(setup_mod, "_stream_mem", stream)
        setup_mod._mem_install = _inflight()
        setup_mod._run_memory_install(db_engine)  # 662-663
        assert setup_mod.mem_last()["status"] == "failed"

    def test_stream_mem_reads_lines(self, monkeypatch):
        setup_mod._mem_install = _inflight()

        class FakeProc:
            stdout = iter(["line1\n", "line2\n", "line3\n", "line4\n"])

            def wait(self):
                return 0

        monkeypatch.setattr(setup_mod.subprocess, "Popen",
                            lambda *a, **k: FakeProc())
        setup_mod._stream_mem(["true"], cwd=None, start=0.0, end=50.0,
                              status_msg="go")
        assert setup_mod._mem_install["progress"] == 50.0


# ── setup.py: router install ─────────────────────────────────────────────────

class TestRouterInstallGaps:
    def test_router_update_noop_idle(self):
        setup_mod._router_install = None
        setup_mod._router_update("msg", progress=50.0)  # 823

    def test_router_update_trims_log(self):
        setup_mod._router_install = _inflight()
        setup_mod._router_install["log"] = ["x"] * setup_mod._LOG_MAX_LINES
        setup_mod._router_update("fresh")  # 835
        assert len(setup_mod._router_install["log"]) == setup_mod._LOG_MAX_LINES

    def test_start_router_joins_inflight(self, db_engine):
        setup_mod._router_install = _inflight()
        assert setup_mod.start_router_install(db_engine)["status"] == "running"  # 867

    def test_tail_router_detail_variants(self):
        setup_mod._router_install = None
        assert setup_mod._tail_router_detail("fb") == "fb"       # 964
        setup_mod._router_install = {"log": []}
        assert setup_mod._tail_router_detail("fb") == "fb"       # 967
        setup_mod._router_install = {"log": [" ", "\t"]}
        assert setup_mod._tail_router_detail("fb") == "fb"       # 980→982

    def test_run_router_probe_failure(self, db_engine, monkeypatch, tmp_path):
        monkeypatch.setattr(setup_mod, "router_site", lambda: str(tmp_path / "s"))
        monkeypatch.setattr(setup_mod, "router_models_dir", lambda: str(tmp_path / "m"))
        monkeypatch.setattr(setup_mod, "_stream_router", lambda *a, **k: None)
        monkeypatch.setattr(setup_mod.os, "makedirs", lambda *a, **k: None)
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: False)
        setup_mod._router_install = _inflight()
        setup_mod._run_router_install(db_engine)  # SetupError → generic except
        assert setup_mod.router_last()["status"] == "failed"

    def test_run_router_predownload_skip(self, db_engine, monkeypatch, tmp_path):
        monkeypatch.setattr(setup_mod, "router_site", lambda: str(tmp_path / "s"))
        monkeypatch.setattr(setup_mod, "router_models_dir", lambda: str(tmp_path / "m"))
        monkeypatch.setattr(setup_mod.os, "makedirs", lambda *a, **k: None)
        monkeypatch.setattr("src.api.memory.router_available", lambda site=None: True)

        def stream(cmd, **k):
            if "EmbeddingModel" in " ".join(cmd):
                raise subprocess.CalledProcessError(1, cmd)
        monkeypatch.setattr(setup_mod, "_stream_router", stream)
        setup_mod._router_install = _inflight()
        setup_mod._run_router_install(db_engine)
        assert setup_mod.router_last()["status"] == "done"  # 924-926

    def test_run_router_filenotfound(self, db_engine, monkeypatch, tmp_path):
        monkeypatch.setattr(setup_mod, "router_site", lambda: str(tmp_path / "s"))
        monkeypatch.setattr(setup_mod, "router_models_dir", lambda: str(tmp_path / "m"))
        monkeypatch.setattr(setup_mod.os, "makedirs", lambda *a, **k: None)
        monkeypatch.setattr(
            setup_mod, "_stream_router",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("pip")))
        setup_mod._router_install = _inflight()
        setup_mod._run_router_install(db_engine)  # 932-933
        assert setup_mod.router_last()["status"] == "failed"

    def test_stream_router_reads_lines(self, monkeypatch):
        setup_mod._router_install = _inflight()

        class FakeProc:
            stdout = iter(["a\n", "b\n", "c\n", "d\n"])

            def wait(self):
                return 0

        monkeypatch.setattr(setup_mod.subprocess, "Popen",
                            lambda *a, **k: FakeProc())
        setup_mod._stream_router(["true"], cwd=None, start=0.0, end=40.0,
                                 status_msg="go")
        assert setup_mod._router_install["progress"] == 40.0

    def test_router_step_shows_last_failed(self):
        with patch("src.api.memory.router_status",
                   return_value={"available": False, "removable": True}), \
             patch.object(setup_mod, "router_install_blocked_reason",
                          return_value=None):
            setup_mod._router_last = {"status": "failed", "detail": "x"}
            step = setup_mod.router_step()  # 296: installing = _router_last
        assert step["installing"]["status"] == "failed"


# ── setup.py: install tail helpers ───────────────────────────────────────────


# ── cost_cache.py: SettingsStore + plugin_supports ───────────────────────────

class TestSettingsStoreGaps:
    def test_plugin_supports_unknown_kind(self):
        from src.api.cost_plugins.deepseek import DeepSeekCostPlugin
        assert cc_mod.plugin_supports(DeepSeekCostPlugin(), "no-such-kind") is False  # 75
        assert cc_mod.plugin_supports(None, "balance") is False

    def test_ensure_loaded_race(self, temp_db):
        _, engine = temp_db
        store = cc_mod.SettingsStore(engine)

        class RaceLock:
            def __enter__(self):
                store._loaded = True  # another thread won while we waited
                return self

            def __exit__(self, *a):
                return False

        store._loaded = False
        store._lock = RaceLock()
        store._ensure_loaded()  # 101-102: second check returns

    def test_get_routing_min_score_bad_value(self, temp_db):
        _, engine = temp_db
        store = cc_mod.SettingsStore(engine)
        store.set("routing_min_score", "not-a-float")
        assert store.get_routing_min_score(default=0.42) == 0.42  # 207-208

    def test_clear_key_db_failure(self, temp_db):
        _, engine = temp_db
        store = cc_mod.SettingsStore(engine)
        with patch("src.api.cost_cache.get_session",
                   side_effect=RuntimeError("locked")):
            store._clear_key("routing_min_score")  # 287-288 logged, not raised

    def test_get_settings_resolve_exception(self):
        from src.api import runtime as rt_mod
        prev = rt_mod._active_runtime
        rt_mod._active_runtime = None
        try:
            assert cc_mod.get_settings() is cc_mod._settings_store
        finally:
            rt_mod._active_runtime = prev

    def test_get_settings_resolve_exception_runtime(self):
        from src.api import runtime as rt_mod
        fake = MagicMock()
        fake.resolve.side_effect = KeyError("inactive")
        prev = rt_mod._active_runtime
        prev_store = cc_mod._settings_store
        cc_mod._settings_store = None
        rt_mod._active_runtime = fake
        try:
            assert cc_mod.get_settings() is None  # 369-370 → legacy None
        finally:
            rt_mod._active_runtime = prev
            cc_mod._settings_store = prev_store

    def test_get_cost_cache_resolve_exception(self, temp_db):
        from src.api import runtime as rt_mod
        _, engine = temp_db
        cc_mod._cost_cache = cc_mod.CostPluginCache(engine)
        fake = MagicMock()
        fake.resolve.side_effect = KeyError("inactive")
        prev = rt_mod._active_runtime
        rt_mod._active_runtime = fake
        try:
            assert cc_mod.get_cost_cache() is cc_mod._cost_cache  # 490-491
        finally:
            rt_mod._active_runtime = prev
            cc_mod._cost_cache = None


# ── cost_cache.py: CostPluginCache ───────────────────────────────────────────

class TestCostCacheGaps:
    @pytest.fixture
    def cache(self, temp_db):
        _, engine = temp_db
        return cc_mod.CostPluginCache(engine), engine

    def test_get_bad_json_payload(self, cache):
        from src.api.models import CostPluginCacheEntry as Row, get_session
        c, engine = cache
        with get_session(engine) as s:
            s.add(Row(provider="p", kind="balance", payload_json="{oops",
                      fetched_at=datetime.now(timezone.utc).isoformat()))
            s.commit()
        ent = c.get("p", "balance")
        assert ent["payload"] == {}  # 397-398

    def test_invalidate_kind_filter(self, cache):
        c, engine = cache
        c.set("p", "balance", {"x": 1})
        c.set("p", "subscription", {"y": 2})
        c.invalidate(provider="p", kind="balance")  # 441
        assert c.get("p", "balance") is None
        assert c.get("p", "subscription") is not None

    def test_entries_bad_timestamp(self, cache):
        from src.api.models import CostPluginCacheEntry as Row, get_session
        c, engine = cache
        with get_session(engine) as s:
            s.add(Row(provider="p", kind="balance", payload_json="{}",
                      fetched_at="garbage-ts"))
            s.commit()
        rows = c.entries()
        assert rows[0]["age_seconds"] == 0.0  # 459-460

    def test_is_stale_bad_timestamp(self, cache):
        from src.api.models import CostPluginCacheEntry as Row, get_session
        c, engine = cache
        with get_session(engine) as s:
            s.add(Row(provider="p", kind="balance", payload_json="{}",
                      fetched_at="garbage-ts"))
            s.commit()
        assert c.is_stale("p", "balance", 999999) is True  # 476-477


# ── cost_cache.py: CacheRefresher internals ──────────────────────────────────

class TestRefresherGaps:
    @pytest.fixture
    def refresher(self):
        cache = MagicMock()
        r = cc_mod.CacheRefresher(cache, MagicMock(), registry_getter=MagicMock())
        return r, cache

    def test_start_twice_noop(self, refresher):
        r, _ = refresher
        r._thread = MagicMock()
        r.start()  # 541: already running → return
        assert r._thread.start.called is False

    def test_request_refresh_registry_failure(self, refresher):
        r, _ = refresher
        r._registry_getter.side_effect = RuntimeError("registry gone")
        r.request_refresh()  # 565-566 registry=None, no crash

    def test_providers_for_registry_failure(self, refresher):
        r, _ = refresher
        r._registry_getter.side_effect = RuntimeError("gone")
        assert r._providers_for(None) == []  # 607-608

    def test_run_swallows_pass_crash(self, refresher):
        r, _ = refresher
        r._tick = 0
        calls = {"n": 0}

        def crash_pass():
            calls["n"] += 1
            r._stop.set()  # exit loop after the first pass
            raise RuntimeError("pass exploded")

        with patch.object(r, "_pass", side_effect=crash_pass):
            r._run()  # 614-615 logged, thread exits cleanly
        assert calls["n"] == 1

    def test_pass_scrape_crash_records_failure(self, refresher):
        r, cache = refresher
        plugin = MagicMock()
        reg = MagicMock()
        reg.providers = ["prov"]
        reg.for_provider.return_value = plugin
        r._registry_getter = lambda: reg
        r._settings.get_ttl_minutes.return_value = 60
        cache.is_stale.return_value = True
        with patch.object(cc_mod, "plugin_supports", return_value=True), \
             patch.object(r, "_scrape", side_effect=RuntimeError("crash")):
            r._pass()  # 658-660
        diag = r.diagnostics()
        assert any(v["consecutive_failures"] >= 1 for v in diag.values())

    def test_pass_scrape_none_prev_data_records_failure(self, refresher):
        r, cache = refresher
        plugin = MagicMock()
        plugin.fetch_subscription.side_effect = lambda: None
        reg = MagicMock()
        reg.providers = ["prov"]
        reg.for_provider.return_value = plugin
        r._registry_getter = lambda: reg
        r._settings.get_ttl_minutes.return_value = 60
        cache.is_stale.return_value = True
        cache.get.return_value = {"payload": {}, "fetched_at": "x"}  # had data
        with patch.object(cc_mod, "plugin_supports", return_value=True), \
             patch.object(cc_mod, "KINDS", ("subscription",)):
            r._pass()  # 675-676: prev data → stale failure record
        assert r.diagnostics()

    def test_pass_scrape_none_no_prev_data_quiet(self, refresher):
        r, cache = refresher
        plugin = MagicMock()
        plugin.fetch_subscription.side_effect = lambda: None
        reg = MagicMock()
        reg.providers = ["prov"]
        reg.for_provider.return_value = plugin
        r._registry_getter = lambda: reg
        r._settings.get_ttl_minutes.return_value = 60
        cache.is_stale.return_value = True
        cache.get.return_value = None  # never had data → quiet set
        with patch.object(cc_mod, "plugin_supports", return_value=True), \
             patch.object(cc_mod, "KINDS", ("subscription",)):
            r._pass()  # 677-679 quiet
        assert len(r._quiet) >= 1

    def test_clear_cache_clears_quiet(self, refresher):
        r, cache = refresher
        r._quiet.add(("p", "balance"))
        r.clear_cache()  # 582-586
        cache.clear.assert_called_once()
        assert not r._quiet and not r._pending

    def test_refresher_service_property(self):
        comp = cc_mod.RefresherComponent()
        assert comp.service is comp.refresher is None


# ── component services (cost_cache) ─────────────────────────────────────────

class TestCostCacheComponents:
    def test_settings_component_service(self, temp_db):
        _, engine = temp_db
        comp = cc_mod.SettingsComponent()
        rt = MagicMock()
        rt.resolve.return_value = engine
        assert comp.setup(rt) is None
        assert comp.service is comp.store

    def test_cost_cache_component_service(self, temp_db):
        _, engine = temp_db
        comp = cc_mod.CostCacheComponent()
        rt = MagicMock()
        rt.resolve.return_value = engine
        comp.setup(rt)
        assert comp.service is comp.cache
