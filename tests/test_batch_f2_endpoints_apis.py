"""Batch F2 — endpoints.py: settings, routing, memory, capability, registry,
benchmark and setup API branches.
"""
import contextlib
import io
import json
import os
import sqlite3
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
    get_session,
)
from tests import _model_names_fixtures as mn


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


def _mi_db(engine):
    """Open *engine*'s DB file with the mi_* model-name schema created.

    ``Base.metadata`` no longer defines the model-name tables (migration 035
    dropped ``model_registry``; ``mi_sources``/``mi_model_names``/``mi_models``
    belong to the model-identity layer), so the registry endpoints — which read
    the DB with raw sqlite3 through ``api.model_names`` — need them created
    explicitly. Returns the open connection so a test can seed it.
    """
    return mn.mi_schema(sqlite3.connect(engine.url.database))


def _model_row(engine, slug):
    """The ``mi_models`` row for *slug* as a dict, or None."""
    con = sqlite3.connect(engine.url.database)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT benchmark_key, active_release, benchmark_release, quantization"
            " FROM mi_models WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


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

    def test_capability_api_db_path_crash(self, temp_db, tmp_path, monkeypatch):
        # The endpoint swallows a crashing ``engine.url`` and falls back to the
        # relative path "data/costs.db"; run in a temp cwd whose data/costs.db
        # carries the mi_* schema so the real load_model_registry() succeeds
        # (the release-resolution path reads mi_models now, not model_registry).
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        mn.mi_schema(sqlite3.connect(str(tmp_path / "data" / "costs.db"))).close()

        class BoomEngine:
            def __getattr__(self, name):
                if name == "url":
                    raise RuntimeError("no url")
                return getattr(temp_db, name)

        h = TestHandler(path="/api/models/capability", engine=BoomEngine())
        h._serve_capability_api()                            # 2246-2247
        assert "tasks" in _json_body(h)

    def test_registry_api_empty_and_odd_provider_names(self, temp_db):
        """Rewrite of ``test_registry_api_with_broken_mappings``.

        The retired table kept provider spellings in a JSON blob, so the old test
        seeded a canonical entry with ``"{not json"`` and asserted the GET still
        returned a row (the blob degraded to ``{}``). There is no blob to malform
        now — a provider mapping is a typed ``mi_model_names`` row — so the two
        properties that mattered are asserted directly:

        * a canonical model with no provider names comes back with empty
          ``providers``/``provider_mappings`` (exactly what the malformed blob
          degraded to), and
        * an odd provider-side spelling is stored and echoed verbatim, never
          parsed.
        """
        con = _mi_db(temp_db)
        mn.add_model(con, "aaa", benchmark_key="aaa")     # no provider names
        bbb = mn.add_model(con, "bbb", benchmark_key="bbb")
        mn.add_endpoint(con, "p")
        odd = 'm/gguf{"v":2} (beta)'                      # was the JSON value "m"
        mn.add_name(con, bbb, "p", odd)
        con.close()

        h = TestHandler(path="/api/models/registry", engine=temp_db)
        h._serve_registry_api()                    # 2294-2319 (handler 348 too)
        body = _json_body(h)
        assert _status(h) == 200
        assert body["count"] == 2
        aaa = [e for e in body["registry"] if e["logical_name"] == "aaa"][0]
        assert aaa["providers"] == []
        assert aaa["provider_mappings"] == {}              # 2305-2306
        bbb = [e for e in body["registry"] if e["logical_name"] == "bbb"][0]
        assert bbb["providers"] == ["p"]
        assert bbb["provider_mappings"] == {"p": odd}      # verbatim, not parsed


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

    def test_upsert_tolerates_odd_neighbour_mapping(self, temp_db):
        """Rewrite of ``test_broken_other_mappings_tolerated``.

        The old test seeded a *neighbour* model with a malformed mappings blob
        (``"{broken"``) and asserted that upserting a different model still
        returned 200/created. With typed rows there is no blob to corrupt, so the
        neighbour instead carries an odd provider-side spelling. The property is
        unchanged — one weird mapping cannot break writing another model — and the
        odd spelling is proven to round-trip verbatim.
        """
        con = _mi_db(temp_db)
        other = mn.add_model(con, "other", benchmark_key="obk")
        mn.add_endpoint(con, "weird.provider")
        odd = "model{broken"
        mn.add_name(con, other, "weird.provider", odd)
        assert mn.provider_mappings(con, "other") == {"weird.provider": odd}
        con.close()
        h = self._h(temp_db, {"logical_name": "mine", "benchmark_key": "mbk",
                              "provider_mappings": {"p1": "model-a"}})
        h._serve_registry_upsert_api()                        # 2526-2527
        assert _status(h) == 200
        assert _json_body(h)["action"] == "created"
        con = sqlite3.connect(temp_db.url.database)
        try:
            assert mn.provider_mappings(con, "other") == {"weird.provider": odd}
        finally:
            con.close()

    def test_update_all_optional_fields(self, temp_db):
        con = _mi_db(temp_db)
        mn.add_model(con, "mine", benchmark_key="old")   # was: provider_mappings_json="{}"
        con.close()
        h = self._h(temp_db, {"logical_name": "mine", "benchmark_key": "new",
                              "provider_mappings": {"p": "m"},
                              "active_release": "2026-08", "benchmark_release": "2026-07",
                              "quantization": "q4_k_m"})
        h._serve_registry_upsert_api()              # 2543-2546 update fields
        assert _status(h) == 200
        assert _json_body(h)["action"] == "updated"
        row = _model_row(temp_db, "mine")
        assert row is not None
        assert row["benchmark_release"] == "2026-07"
        assert row["quantization"] == "q4_k_m"
        assert row["active_release"] == "2026-08"
        con = sqlite3.connect(temp_db.url.database)
        try:
            assert mn.provider_mappings(con, "mine") == {"p": "m"}
        finally:
            con.close()

    def test_router_matrix_invalidate_crash_swallowed(self, temp_db):
        _mi_db(temp_db).close()
        h = self._h(temp_db, {"logical_name": "fresh", "benchmark_key": "fbk",
                              "provider_mappings": {}})
        with patch("src.api.router.invalidate_router_matrix",
                   side_effect=RuntimeError("cache")):
            h._serve_registry_upsert_api()                    # 2567-2568
        assert _json_body(h)["action"] == "created"

    def test_upsert_db_crash_500(self, temp_db):
        _mi_db(temp_db).close()
        h = self._h(temp_db, {"logical_name": "x", "benchmark_key": "b",
                              "provider_mappings": {}})
        # The upsert path writes through api.model_names now, not get_session;
        # fail the actual write so the endpoint's DB-crash branch runs.
        with patch("src.api.model_names.upsert_entry",
                   side_effect=RuntimeError("db")):
            h._serve_registry_upsert_api()                    # 2570-2571
        assert _status(h) == 500

    def test_delete_db_crash_500(self, temp_db):
        h = TestHandler(path="/api/models/registry/x", method="DELETE", engine=temp_db)
        # Delete goes through api.model_names.delete_entry (raw sqlite3), so a
        # crash there is what the endpoint must surface as 500.
        with patch("src.api.model_names.delete_entry",
                   side_effect=RuntimeError("db")):
            h._serve_registry_delete_api("x")                 # 2590-2591
        assert _status(h) == 500


# ── Benchmark APIs ───────────────────────────────────────────────────────────


# ── Setup install / remove branches ─────────────────────────────────────────

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
