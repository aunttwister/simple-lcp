"""Batch F2 — endpoints.py: settings, routing, memory, capability, registry,
benchmark and setup API branches.
"""
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.server import LCPHandler
from src.api.models import (
    get_engine,
    Base,
    Budget,
    ModelCapability,
    ModelRegistryEntry,
    get_session,
)


class TestHandler(LCPHandler):
    def __init__(self, path="/", method="GET", engine=None, headers=None, body=None):
        self.path = path
        self.command = method
        self.headers = headers or {}
        self.request_version = "HTTP/1.1"
        self.requestline = f"{method} {path} HTTP/1.1"
        self.raw_requestline = f"{method} {path} HTTP/1.1".encode()
        self.client_address = ("127.0.0.1", 0)
        self.send_response = MagicMock()
        self.send_header = MagicMock()
        self.end_headers = MagicMock()
        self.wfile = MagicMock()
        self.wfile.write = MagicMock()
        self.rfile = MagicMock()
        if isinstance(body, dict):
            body = json.dumps(body)
        body_bytes = (body or b"{}") if isinstance(body or b"{}", bytes) else (body or "{}").encode()
        self.rfile.read = MagicMock(return_value=body_bytes)
        if body:
            self.headers["Content-Length"] = str(len(body_bytes))
        self._write_chunk = MagicMock()
        self.engine = engine
        self.log_error = MagicMock()


def _status(handler):
    return handler.send_response.call_args[0][0] if handler.send_response.call_args else None


def _json_body(handler):
    for call in handler.wfile.write.call_args_list:
        try:
            return json.loads(call[0][0])
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return {}


@pytest.fixture
def temp_db():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    for ext in ["", "-wal", "-shm"]:
        try:
            os.unlink(db_path + ext)
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def _cfg():
    cfg = MagicMock()
    cfg.profiles = {"l2": {"chain": [], "forbidden_tools": [], "auth_required": False}}
    cfg.providers = {"deepseek": {"api_base": "https://t/v1", "models": ["m"]}}
    cfg.get_profile = lambda name: cfg.profiles.get(name)
    cfg.save = MagicMock()
    cfg.raw = {"providers": dict(cfg.providers), "profiles": dict(cfg.profiles)}
    LCPHandler.config = cfg
    return cfg


def _services(**by_key):
    """resolve_service side_effect returning per-key services; None → None."""
    def _se(key, fallback=None):
        return by_key.get(key, None)
    return _se


# ── Settings API ─────────────────────────────────────────────────────────────

class TestSettingsApi:
    def test_settings_update_settings_none_500(self, temp_db):
        h = TestHandler(path="/api/settings", method="POST", engine=temp_db,
                        body={"ttl_minutes": 5})
        with patch("src.server.endpoints.resolve_service", side_effect=_services()):
            h._serve_settings_update_api()
        assert _status(h) == 500                      # 1755-1756

    def test_settings_provider_reset_branch(self, temp_db):
        settings = MagicMock()
        settings.get_ttl_minutes.return_value = 30
        settings.ttl_overrides.return_value = {}
        refresher = MagicMock()
        h = TestHandler(path="/api/settings", method="POST", engine=temp_db,
                        body={"provider": "opencode"})
        with patch("src.server.endpoints.resolve_service",
                   side_effect=_services(settings=settings, refresher=refresher)):
            h._serve_settings_update_api()
        assert _status(h) == 200                      # 1763
        settings.clear_ttl_minutes.assert_called_once_with("opencode")
        refresher.request_refresh.assert_called_once_with(provider="opencode")

    def test_settings_ttl_set_with_refresher(self, temp_db):
        settings = MagicMock()
        settings.ttl_overrides.return_value = {}
        refresher = MagicMock()
        h = TestHandler(path="/api/settings", method="POST", engine=temp_db,
                        body={"ttl_minutes": 7})
        with patch("src.server.endpoints.resolve_service",
                   side_effect=_services(settings=settings, refresher=refresher)):
            h._serve_settings_update_api()
        assert _status(h) == 200                      # 1779
        settings.set_ttl_minutes.assert_called_once_with(7, provider=None)
        refresher.request_refresh.assert_called_once_with(provider=None)

    def test_settings_refresh_no_refresher_500(self, temp_db):
        h = TestHandler(path="/api/settings/cache/refresh", method="POST", engine=temp_db)
        with patch("src.server.endpoints.resolve_service", side_effect=_services()):
            h._serve_settings_refresh_api()
        assert _status(h) == 500                      # 1793

    def test_cookie_set_invalidates_and_refreshes(self, temp_db):
        store = MagicMock()
        cache = MagicMock()
        refresher = MagicMock()
        h = TestHandler(path="/api/cost-plugins/cookie/opencode", method="POST",
                        engine=temp_db, body={"cookie": "c"})

        def cred_fallback():
            return store

        def se(key, fallback=None):
            if key == "credential_store":
                return cred_fallback()
            return by_key.get(key)
        by_key = {"cost_cache": cache, "refresher": refresher}
        with patch("src.server.endpoints.resolve_service", side_effect=se):
            h._serve_plugin_cookie_set("opencode")
        assert _status(h) == 200                      # 1704, 1707
        cache.invalidate.assert_called_once_with(provider="opencode")
        refresher.request_refresh.assert_called_once_with(provider="opencode")

    def test_workspace_id_set_invalidates_and_refreshes(self, temp_db):
        store = MagicMock()
        cache = MagicMock()
        refresher = MagicMock()
        h = TestHandler(path="/api/cost-plugins/workspace-id/opencode", method="POST",
                        engine=temp_db, body={"workspace_id": "wrk_1"})

        def se(key, fallback=None):
            if key == "credential_store":
                return store
            return by_key.get(key)
        by_key = {"cost_cache": cache, "refresher": refresher}
        with patch("src.server.endpoints.resolve_service", side_effect=se):
            h._serve_plugin_workspace_id_set("opencode")
        assert _status(h) == 200                      # 1704, 1707
        cache.invalidate.assert_called_once_with(provider="opencode")


# ── Routing policy / rules API ───────────────────────────────────────────────

class TestRoutingApi:
    def test_policy_bad_min_score_400(self, temp_db):
        settings = MagicMock()
        h = TestHandler(path="/api/routing/policy", method="POST", engine=temp_db,
                        body={"min_score": "abc"})
        with patch("src.server.endpoints.resolve_service",
                   side_effect=_services(settings=settings)), \
             patch("src.api.router.routing_status", return_value={}):
            h._serve_routing_policy_api()
        assert _status(h) == 400                      # 1866-1868

    def test_policy_settings_none_500(self, temp_db):
        h = TestHandler(path="/api/routing/policy", method="POST", engine=temp_db,
                        body={"enabled": True})
        with patch("src.server.endpoints.resolve_service", side_effect=_services()):
            h._serve_routing_policy_api()
        assert _status(h) == 500                      # 1846-1847

    def test_rules_not_a_list_400(self, temp_db):
        h = TestHandler(path="/api/routing/rules", method="POST", engine=temp_db,
                        body={"rules": "nope"})
        h._serve_routing_rules_api()
        assert _status(h) == 400                      # 1898-1899

    def test_rule_not_object_400(self, temp_db):
        h = TestHandler(path="/api/routing/rules", method="POST", engine=temp_db,
                        body={"rules": ["x"]})
        h._serve_routing_rules_api()
        assert _status(h) == 400                      # 1902-1903

    def test_rule_bad_policy_400(self, temp_db):
        h = TestHandler(path="/api/routing/rules", method="POST", engine=temp_db,
                        body={"rules": [{"action": "policy", "policy": "warp"}]})
        h._serve_routing_rules_api()
        assert _status(h) == 400                      # 1913-1914

    def test_rule_bad_min_score_400(self, temp_db):
        h = TestHandler(path="/api/routing/rules", method="POST", engine=temp_db,
                        body={"rules": [{"action": "block", "provider": "p",
                                         "min_score": "high"}]})
        h._serve_routing_rules_api()
        assert _status(h) == 400                      # 1916-1920

    def test_rules_settings_none_500(self, temp_db):
        h = TestHandler(path="/api/routing/rules", method="POST", engine=temp_db,
                        body={"rules": [{"action": "block", "provider": "p"}]})
        with patch("src.server.endpoints.resolve_service", side_effect=_services()):
            h._serve_routing_rules_api()
        assert _status(h) == 500                      # 1924-1925


# ── Usage stats branches ─────────────────────────────────────────────────────

class TestUsageBranches:
    def test_usage_stats_savings_crash_swallowed(self, temp_db):
        now = datetime.now(timezone.utc).isoformat()
        from src.api.models import Request as RM
        with get_session(temp_db) as s:
            s.add(RM(timestamp=now, profile="l2", model="deepseek-v4-pro",
                     provider="deepseek", prompt_tokens=10, completion_tokens=5,
                     cache_hit_tokens=5000, cache_miss_tokens=0, cost=0.01,
                     latency_ms=1, success=1))
            s.commit()

        def boom(p, m):
            raise RuntimeError("no pricing")

        LCPHandler.config.get_pricing = boom
        h = TestHandler(path="/api/usage/stats", engine=temp_db)
        h._serve_usage_stats_api()
        assert _status(h) == 200                      # 2041-2042
        assert _json_body(h)["cache"]["savings"] == 0.0

    def test_usage_totals_provider_filter(self, temp_db):
        h = TestHandler(path="/api/usage/totals?provider=deepseek", engine=temp_db)
        h._serve_usage_totals_api()
        assert _status(h) == 200                      # 2124


# ── Models page + capability + registry GET APIs ─────────────────────────────

class TestPageAndCapabilityApis:
    def test_models_page(self, temp_db):
        h = TestHandler(path="/models", engine=temp_db)
        with patch("src.ui.pages.render_models_page", return_value="<html>x</html>"):
            h.do_GET()
        assert _status(h) == 200                      # 2208-2213 (handler 250 too)

    def test_capability_api_broken_subtasks_and_benchmarks(self, temp_db):
        now = datetime.now(timezone.utc).isoformat()
        with get_session(temp_db) as s:
            s.add(ModelCapability(model="m1", task_type="code_generation",
                                  score=0.5, source="livebench",
                                  benchmark_category="coding",
                                  release_label="2026-01-01", updated_at=now))
            s.commit()

        h = TestHandler(path="/api/models/capability", engine=temp_db)

        class BrokenSubtask:
            pass

        with patch("src.api.models.ModelCapabilitySubtask", BrokenSubtask):
            h._serve_capability_api()                        # 2276-2278
        body = _json_body(h)
        assert body["subtasks"] == {}
        assert body["benchmark_categories"]["code_generation"]["m1"] == "coding"  # 2264
        assert body["releases"]["code_generation"]["m1"] == "2026-01-01"

    def test_capability_api_db_path_crash(self, temp_db):
        class BoomEngine:
            def __getattr__(self, name):
                if name == "url":
                    raise RuntimeError("no url")
                return getattr(temp_db, name)

        h = TestHandler(path="/api/models/capability", engine=BoomEngine())
        h._serve_capability_api()                            # 2246-2247
        assert "tasks" in _json_body(h)

    def test_registry_api_with_broken_mappings(self, temp_db):
        now = datetime.now(timezone.utc).isoformat()
        with get_session(temp_db) as s:
            s.add(ModelRegistryEntry(logical_name="aaa", benchmark_key="aaa",
                                     provider_mappings_json="{not json",
                                     updated_at=now))
            s.add(ModelRegistryEntry(logical_name="bbb", benchmark_key="bbb",
                                     provider_mappings_json='{"p": "m"}',
                                     updated_at=now))
            s.commit()
        h = TestHandler(path="/api/models/registry", engine=temp_db)
        h._serve_registry_api()                    # 2294-2319 (handler 348 too)
        body = _json_body(h)
        assert body["count"] == 2
        aaa = [e for e in body["registry"] if e["logical_name"] == "aaa"][0]
        assert aaa["provider_mappings"] == {}              # 2305-2306
        bbb = [e for e in body["registry"] if e["logical_name"] == "bbb"][0]
        assert bbb["providers"] == ["p"]


# ── Capability import / seed / manual APIs ───────────────────────────────────

class TestCapabilityWriteApis:

    def test_seed_crash_500(self, temp_db):
        h = TestHandler(path="/api/models/capability/seed", method="POST",
                        engine=temp_db)
        with patch("src.api.seed_capabilities.seed_capabilities",
                   side_effect=RuntimeError("seed boom")):
            h._serve_capability_seed_api()
        assert _status(h) == 500                              # 2395-2396

    def test_manual_api_db_crash_500(self, temp_db):
        h = TestHandler(path="/api/models/capability/manual", method="POST",
                        engine=temp_db,
                        body={"model": "m1", "scores": {"planning": 80}})
        with patch("src.api.models.get_session", side_effect=RuntimeError("db")):
            h._serve_capability_manual_api()
        assert _status(h) == 500                              # 2461-2462


# ── Registry upsert / delete branches ────────────────────────────────────────

class TestRegistryUpsert:
    def _h(self, temp_db, body):
        return TestHandler(path="/api/models/registry", method="POST",
                           engine=temp_db, body=body)

    def test_missing_benchmark_key_400(self, temp_db):
        h = self._h(temp_db, {"logical_name": "x"})
        h._serve_registry_upsert_api()
        assert _status(h) == 400                              # 2491-2492

    def test_broken_other_mappings_tolerated(self, temp_db):
        now = datetime.now(timezone.utc).isoformat()
        with get_session(temp_db) as s:
            s.add(ModelRegistryEntry(logical_name="other", benchmark_key="obk",
                                     provider_mappings_json="{broken",
                                     updated_at=now))
            s.commit()
        h = self._h(temp_db, {"logical_name": "mine", "benchmark_key": "mbk",
                              "provider_mappings": {"p1": "model-a"}})
        h._serve_registry_upsert_api()                        # 2526-2527
        assert _status(h) == 200
        assert _json_body(h)["action"] == "created"

    def test_update_all_optional_fields(self, temp_db):
        now = datetime.now(timezone.utc).isoformat()
        with get_session(temp_db) as s:
            s.add(ModelRegistryEntry(logical_name="mine", benchmark_key="old",
                                     provider_mappings_json="{}",
                                     updated_at=now))
            s.commit()
        h = self._h(temp_db, {"logical_name": "mine", "benchmark_key": "new",
                              "provider_mappings": {"p": "m"},
                              "active_release": "2026-08", "benchmark_release": "2026-07",
                              "quantization": "q4_k_m"})
        h._serve_registry_upsert_api()              # 2543-2546 update fields
        assert _json_body(h)["action"] == "updated"
        with get_session(temp_db) as s:
            e = s.query(ModelRegistryEntry).filter_by(logical_name="mine").first()
            assert e.benchmark_release == "2026-07"
            assert e.quantization == "q4_k_m"

    def test_router_matrix_invalidate_crash_swallowed(self, temp_db):
        h = self._h(temp_db, {"logical_name": "fresh", "benchmark_key": "fbk",
                              "provider_mappings": {}})
        with patch("src.api.router.invalidate_router_matrix",
                   side_effect=RuntimeError("cache")):
            h._serve_registry_upsert_api()                    # 2567-2568
        assert _json_body(h)["action"] == "created"

    def test_upsert_db_crash_500(self, temp_db):
        h = self._h(temp_db, {"logical_name": "x", "benchmark_key": "b",
                              "provider_mappings": {}})
        with patch("src.api.models.get_session", side_effect=RuntimeError("db")):
            h._serve_registry_upsert_api()                    # 2570-2571
        assert _status(h) == 500

    def test_delete_db_crash_500(self, temp_db):
        h = TestHandler(path="/api/models/registry/x", method="DELETE", engine=temp_db)
        with patch("src.api.models.get_session", side_effect=RuntimeError("db")):
            h._serve_registry_delete_api("x")                 # 2590-2591
        assert _status(h) == 500


# ── Benchmark APIs ───────────────────────────────────────────────────────────

class TestBenchmarkApis:
    def test_list_bad_limit_and_offset(self, temp_db):
        h = TestHandler(path="/api/models/benchmark?limit=x&offset=y", engine=temp_db)
        h._serve_benchmark_list_api()
        assert _status(h) == 200                              # 2604-2609

    def test_list_crash_500(self, temp_db):
        h = TestHandler(path="/api/models/benchmark", engine=temp_db)
        with patch("src.api.benchmark.list_runs", side_effect=RuntimeError("db")):
            h._serve_benchmark_list_api()                     # 2613-2614
        assert _status(h) == 500

    def test_status_crash_500(self, temp_db):
        h = TestHandler(path="/api/models/benchmark/status", engine=temp_db)
        with patch("src.api.benchmark.benchmark_status", side_effect=RuntimeError("x")):
            h._serve_benchmark_status_api()                   # 2621-2622
        assert _status(h) == 500

    def test_detail_found(self, temp_db):
        h = TestHandler(path="/api/models/benchmark/9", engine=temp_db)
        with patch("src.api.benchmark.get_run", return_value={"id": 9}):
            h._serve_benchmark_detail_api("9")                # 2634
        assert _json_body(h)["run"]["id"] == 9

    def test_log_invalid_id_400(self, temp_db):
        h = TestHandler(path="/api/models/benchmark/x/log", engine=temp_db)
        h._serve_benchmark_log_api("x")                       # 2643-2645
        assert _status(h) == 400

    def test_create_with_release_and_value_error(self, temp_db):
        h = TestHandler(path="/api/models/benchmark", method="POST", engine=temp_db,
                        body={"provider": "p", "model": "m", "release": "2026-08"})

        def boom(*a, **k):
            raise ValueError("bad target")
        with patch("src.api.benchmark.queue_benchmark", side_effect=boom):
            h._serve_benchmark_create_api()                   # 2679, 2690-2691
        assert _status(h) == 400

    def test_create_generic_crash_500(self, temp_db):
        h = TestHandler(path="/api/models/benchmark", method="POST", engine=temp_db,
                        body={"provider": "p", "model": "m"})
        with patch("src.api.benchmark.queue_benchmark",
                   side_effect=RuntimeError("queue")):
            h._serve_benchmark_create_api()                   # 2692-2693
        assert _status(h) == 500


# ── Setup install / remove branches ─────────────────────────────────────────

class TestSetupApiBranches:
    def test_install_memory_module(self, temp_db):
        h = TestHandler(path="/api/setup/install/module/memory", method="POST",
                        engine=temp_db, body={})
        with patch("src.api.setup.start_memory_install",
                   return_value={"status": "queued"}):
            h._serve_setup_install_api("module", "memory")    # 3056
        assert _json_body(h)["status"] == "queued"

    def test_install_generic_crash_500(self, temp_db):
        h = TestHandler(path="/api/setup/install/provider/x", method="POST",
                        engine=temp_db, body={})
        with patch("src.api.setup.install_provider",
                   side_effect=RuntimeError("boom")):
            h._serve_setup_install_api("provider", "x")       # 3063-3065
        assert _status(h) == 500

    def test_remove_router_module(self, temp_db):
        h = TestHandler(path="/api/setup/module/router", method="DELETE", engine=temp_db)
        with patch("src.api.setup.remove_router", return_value={"ok": True}):
            h._serve_setup_remove_api("module", "router")     # 3123-3124
        assert _status(h) == 200

    def test_remove_memory_module(self, temp_db):
        h = TestHandler(path="/api/setup/module/memory", method="DELETE", engine=temp_db)
        with patch("src.api.setup.remove_memory", return_value={"ok": True}):
            h._serve_setup_remove_api("module", "memory")     # 3125-3126
        assert _status(h) == 200

    def test_remove_unknown_target_404(self, temp_db):
        h = TestHandler(path="/api/setup/module/nope", method="DELETE", engine=temp_db)
        h._serve_setup_remove_api("module", "nope")           # 3128-3129
        assert _status(h) == 404

    def test_remove_setup_error_400(self, temp_db):
        from src.api import setup as setup_mod
        h = TestHandler(path="/api/setup/module/livebench", method="DELETE", engine=temp_db)
        with patch("src.api.setup.remove_livebench",
                   side_effect=setup_mod.SetupError("nope")):
            h._serve_setup_remove_api("module", "livebench")  # 3131-3132
        assert _status(h) == 400

    def test_remove_generic_crash_500(self, temp_db):
        h = TestHandler(path="/api/setup/module/livebench", method="DELETE", engine=temp_db)
        with patch("src.api.setup.remove_livebench",
                   side_effect=RuntimeError("boom")):
            h._serve_setup_remove_api("module", "livebench")  # 3133-3135
        assert _status(h) == 500


# ── Memory API auth + branches ───────────────────────────────────────────────

class TestMemoryApiGaps:
    def _authed_cfg(self, auth_required=True):
        cfg = LCPHandler.config
        cfg.profiles["l2"]["auth_required"] = auth_required
        return cfg

    def test_auth_km_none_500(self, temp_db, monkeypatch):
        self._authed_cfg()
        monkeypatch.setattr("src.api.key_manager.get_key_manager", lambda: None)
        h = TestHandler(path="/l2/memory/count", engine=temp_db,
                        headers={"Authorization": "Bearer k"})
        assert h._memory_auth("l2") is False              # 2897-2898
        assert _status(h) == 500

    def test_auth_invalid_key_401(self, temp_db, monkeypatch):
        self._authed_cfg()
        km = MagicMock()
        km.validate_key.return_value = None
        monkeypatch.setattr("src.api.key_manager.get_key_manager", lambda: km)
        h = TestHandler(path="/l2/memory/count", engine=temp_db,
                        headers={"Authorization": "Bearer bad"})
        assert h._memory_auth("l2") is False              # 2901, 2903
        assert _status(h) == 401

    def test_auth_validate_crash_500(self, temp_db, monkeypatch):
        self._authed_cfg()
        km = MagicMock()
        km.validate_key.side_effect = RuntimeError("db")
        monkeypatch.setattr("src.api.key_manager.get_key_manager", lambda: km)
        h = TestHandler(path="/l2/memory/count", engine=temp_db,
                        headers={"Authorization": "Bearer x"})
        assert h._memory_auth("l2") is False              # 2912-2915
        assert _status(h) == 500

    def test_backend_import_crash_501(self, temp_db, monkeypatch):
        monkeypatch.setattr("src.api.key_manager.get_key_manager", lambda: None)
        monkeypatch.delattr("src.api.memory.get_memory")
        h = TestHandler(path="/l2/memory/count", engine=temp_db)
        assert h._memory_backend_or_501() is None         # 2922-2923
        assert _status(h) == 501

    def test_unknown_action_404(self, temp_db, monkeypatch):
        monkeypatch.setattr("src.api.key_manager.get_key_manager", lambda: None)
        h = TestHandler(path="/l2/memory/bogus", method="POST", engine=temp_db)
        h._serve_memory_api("l2", "bogus")                # 2938-2939
        assert _status(h) == 404

    def test_recall_missing_query_400(self, temp_db, monkeypatch):
        monkeypatch.setattr("src.api.key_manager.get_key_manager", lambda: None)
        backend = MagicMock()
        monkeypatch.setattr("src.api.memory.get_memory", lambda: backend)
        h = TestHandler(path="/l2/memory/recall", method="POST", engine=temp_db,
                        body={})
        h.do_POST()
        assert _status(h) == 400                          # 2972-2973

    def test_forget_missing_id_400(self, temp_db, monkeypatch):
        monkeypatch.setattr("src.api.key_manager.get_key_manager", lambda: None)
        backend = MagicMock()
        monkeypatch.setattr("src.api.memory.get_memory", lambda: backend)
        h = TestHandler(path="/l2/memory/forget", method="POST", engine=temp_db,
                        body={})
        h.do_POST()
        assert _status(h) == 400                          # 2984-2985

    def test_generic_crash_500(self, temp_db, monkeypatch):
        monkeypatch.setattr("src.api.key_manager.get_key_manager", lambda: None)
        backend = MagicMock()
        backend.retain.side_effect = RuntimeError("db corrupt")
        monkeypatch.setattr("src.api.memory.get_memory", lambda: backend)
        h = TestHandler(path="/l2/memory/retain", method="POST", engine=temp_db,
                        body={"content": "fact"})
        h.do_POST()
        assert _status(h) == 500                          # 2993-2994
