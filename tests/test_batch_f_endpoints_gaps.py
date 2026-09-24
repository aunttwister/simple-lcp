"""Batch F — endpoints.py final coverage gaps.

Closes the remaining src/server/endpoints.py branches: invalid-JSON-body
guards, 500 error paths, multipart edge cases, CF-ray detection, discover
fallbacks, settings/routing validation branches, memory API edges, setup
install/remove branches, registry/capability/benchmark API branches.
"""
import io
import json
import os
import tempfile
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.server import LCPHandler
from src.api.models import (
    get_engine,
    Base,
    Request as RequestModel,
    Budget,
    get_session,
)


class TestHandler(LCPHandler):
    """Subclass that skips BaseHTTPRequestHandler.__init__ for direct testing."""

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


@pytest.fixture(autouse=True)
def _setup_handler_config(temp_db):
    """Ensure LCPHandler.config is set for all tests."""
    from src.api.key_manager import KeyManager
    import src.api.key_manager as key_manager_mod
    key_manager_mod._key_manager = KeyManager(temp_db, "data")

    cfg = MagicMock()
    cfg.server = {"port": 8734, "default_profile": "l2"}
    cfg.profiles = {
        "l2": {
            "forbidden_tools": [],
            "chain": [{"provider": "deepseek", "model": "deepseek-v4-pro", "base_url": "https://t/v1"}],
            "auth_required": False,
        },
    }
    cfg.providers = {
        "deepseek": {"api_key_env": "DK", "api_base": "https://t/v1", "models": ["deepseek-v4-pro"]},
    }
    cfg.pricing = [{"provider": "deepseek", "model": "deepseek-v4-pro", "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0}]
    cfg.circuit_breaker = {"failures_dead": 5, "dead_cooldown_seconds": 300, "failures_degraded": 3, "degraded_cooldown_seconds": 60}
    cfg.database = {"path": "/tmp/test.db", "wal_mode": True}
    cfg.model_limits = {}
    cfg.get_profile = lambda name: cfg.profiles.get(name)
    cfg.get_pricing = lambda provider, model: cfg.pricing[0]
    cfg.get_provider_key = lambda name: "test-key"
    cfg.check_reload = MagicMock()
    cfg.raw = {"providers": dict(cfg.providers), "profiles": dict(cfg.profiles)}
    cfg.save = MagicMock()
    LCPHandler.config = cfg


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


# ── module-level helpers ─────────────────────────────────────────────────────

class TestModuleHelpers:
    def test_engine_db_path_exception_fallback(self):
        from src.server.endpoints import _engine_db_path

        class ExplodingEngine:
            @property
            def url(self):
                raise RuntimeError("no url")

        # 44-46: exception path falls back to the default path
        assert _engine_db_path(ExplodingEngine()) == "data/costs.db"

    def test_auto_learn_model_contexts_learn_and_skip(self):
        from src.server.endpoints import _auto_learn_model_contexts

        cfg = MagicMock()
        cfg.raw = {}
        plugin = MagicMock()
        plugin.discover_models.return_value = [
            {"id": "m1", "context_length": 32000},   # learned
            "not-a-dict",                              # 79-80 continue
            {"id": "", "n_ctx": 10},                   # 83-84 no id
            {"id": "m2", "n_ctx": "abc"},              # 85-88 int fails
            {"id": "m3", "n_ctx": 8192},               # learned
        ]
        registry = MagicMock()
        registry.for_provider.return_value = plugin
        with patch("src.server.endpoints.resolve_service", return_value=registry):
            learned = _auto_learn_model_contexts(cfg, "prov", "http://x/v1")
        assert learned == 2
        assert cfg.raw["model_limits"]["m1"]["context_window"] == 32000
        cfg.save.assert_called_once()

    def test_auto_learn_model_contexts_crash_returns_zero(self):
        from src.server.endpoints import _auto_learn_model_contexts

        plugin = MagicMock()
        plugin.discover_models.side_effect = RuntimeError("boom")
        registry = MagicMock()
        registry.for_provider.return_value = plugin
        cfg = MagicMock()
        cfg.raw = {}
        with patch("src.server.endpoints.resolve_service", return_value=registry):
            # 95-96: discovery crash must never propagate
            assert _auto_learn_model_contexts(cfg, "prov", "http://x/v1") == 0

    def test_sync_dynamic_routing_enabled_crash_swallowed(self):
        from src.server.endpoints import _sync_dynamic_routing_enabled

        settings = MagicMock()
        settings.get_config_section.side_effect = RuntimeError("db gone")
        # 112-113: best-effort sync never raises
        _sync_dynamic_routing_enabled(settings, True)

    def test_parse_multipart_part_without_name(self):
        from src.server.endpoints import _parse_multipart_upload

        body = (
            b"------XBOUND\r\n"
            b"Content-Disposition: form-data; junk-no-name-header\r\n\r\n"
            b"ignored-value\r\n"
            b"------XBOUND\r\n"
            b'Content-Disposition: form-data; name="file"; filename="a.csv"\r\n'
            b"Content-Type: text/csv\r\n\r\n"
            b"model,score\nm1,5\n"
            b"------XBOUND--\r\n"
        )
        # 260: part without name= header is skipped, file part still parsed
        file_bytes, release, filename = _parse_multipart_upload(
            body, 'multipart/form-data; boundary="----XBOUND"')
        assert b"model,score" in file_bytes
        assert filename == "a.csv"


# ── invalid JSON body guards (parametrized) ─────────────────────────────────

class TestInvalidJsonBody:
    """Every POST handler wraps _read_body() in a try; each guard must 400."""

    def _h(self, temp_db, path="/x", method="POST"):
        h = TestHandler(path=path, method=method, engine=temp_db)
        h._read_body = MagicMock(side_effect=ValueError("bad json"))
        return h

    @pytest.mark.parametrize("method_name,args", [
        ("_serve_circuit_breaker_reset", ()),
        ("_serve_provider_toggle", ("deepseek",)),
        ("_serve_provider_create", ()),
        ("_serve_provider_update", ("deepseek",)),
        ("_serve_provider_test", ()),
        ("_serve_provider_discover", ()),
        ("_serve_profile_update", ("l2",)),
        ("_serve_key_create", ()),
        ("_serve_budget_update", ("1",)),
        ("_serve_plugin_cookie_set", ("opencode",)),
        ("_serve_plugin_workspace_id_set", ("opencode",)),
        ("_serve_settings_update_api", ()),
        ("_serve_routing_policy_api", ()),
        ("_serve_routing_rules_api", ()),
        ("_serve_capability_manual_api", ()),
        ("_serve_registry_upsert_api", ()),
    ])
    def test_invalid_json_returns_400(self, temp_db, method_name, args):
        h = self._h(temp_db)
        getattr(h, method_name)(*args)
        assert _status(h) == 400
        assert _json_body(h)["error"] == "invalid JSON body"

    def test_setup_install_invalid_json(self, temp_db):
        h = self._h(temp_db)
        h._serve_setup_install_api("provider", "x")
        assert _status(h) == 400

    def test_memory_retain_invalid_json(self, temp_db, monkeypatch):
        backend = MagicMock()
        monkeypatch.setattr("src.api.memory.get_memory", lambda: backend)
        monkeypatch.setattr("src.api.key_manager.get_key_manager", lambda: None)
        h = TestHandler(path="/l2/memory/retain", method="POST", engine=temp_db)
        h._read_body = MagicMock(side_effect=ValueError("bad"))
        h.do_POST()
        assert _status(h) == 400


# ── provider failures / failovers branches ──────────────────────────────────

class TestProviderFailures:
    def test_failures_api_db_crash_500(self, temp_db):
        h = TestHandler(path="/api/providers/deepseek/failures", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            h._serve_provider_failures_api("deepseek")
        assert _status(h) == 500

    def test_failures_bucket_credits_and_other(self, temp_db):
        now = datetime.now(timezone.utc).isoformat()
        with get_session(temp_db) as s:
            s.add_all([
                RequestModel(timestamp=now, profile="l2", model="m",
                             provider="deepseek", cost=0, latency_ms=1, success=0,
                             error_type="insufficient balance"),
                RequestModel(timestamp=now, profile="l2", model="m",
                             provider="deepseek", cost=0, latency_ms=1, success=0,
                             error_type="zzz weird failure"),
                RequestModel(timestamp=now, profile="l2", model="m",
                             provider="deepseek", cost=0, latency_ms=1, success=0,
                             error_type=None),
            ])
            s.commit()
        h = TestHandler(path="/api/providers/deepseek/failures?profile=l2", engine=temp_db)
        h._serve_provider_failures_api("deepseek")
        body = _json_body(h)
        assert body["buckets"]["credits"] == 1        # 455
        assert body["buckets"]["other"] == 2          # 464 (incl. None error_type)
        assert body["total"] == 3

    def test_failovers_bad_limit_query(self, temp_db):
        h = TestHandler(path="/api/providers/failovers?limit=abc", engine=temp_db)
        h._serve_providers_failovers_api()
        assert _status(h) == 200                      # 486-487: limit defaults to 20
        assert "failovers" in _json_body(h)

    def test_failovers_db_crash_500(self, temp_db):
        h = TestHandler(path="/api/providers/failovers", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            h._serve_providers_failovers_api()
        assert _status(h) == 500                      # 512-514


# ── provider CRUD branches ───────────────────────────────────────────────────

class TestProviderCrudBranches:
    def test_provider_update_api_key_and_models(self, temp_db):
        store = MagicMock()
        h = TestHandler(path="/api/providers/deepseek", method="PUT", engine=temp_db,
                        body={"api_key": "k1", "models": ["a", "b"]})
        with patch("src.server.endpoints._credential_store_for", return_value=store), \
             patch("src.server.endpoints._auto_learn_model_contexts", return_value=0):
            h._serve_provider_update("deepseek")
        assert _status(h) == 200                      # 817, 824
        store.set.assert_called_once_with("deepseek", "k1")
        assert LCPHandler.config.providers["deepseek"]["models"] == ["a", "b"]

    def test_provider_update_api_key_blank_defaults_empty(self, temp_db):
        store = MagicMock()
        h = TestHandler(path="/api/providers/deepseek", method="PUT", engine=temp_db,
                        body={"api_key": None})
        with patch("src.server.endpoints._credential_store_for", return_value=store), \
             patch("src.server.endpoints._auto_learn_model_contexts", return_value=0):
            h._serve_provider_update("deepseek")
        store.set.assert_called_once_with("deepseek", "")


class TestProviderTestErrors:
    def _body(self, **kw):
        b = {"api_base": "https://x/v1", "api_key": "k", "model": "m"}
        b.update(kw)
        return b

    def test_http_error_body_unreadable(self, temp_db):
        h = TestHandler(path="/api/providers/test", method="POST", engine=temp_db,
                        body=self._body())

        class BadFP:
            def read(self, *a, **k):
                raise OSError("stream closed")

            def close(self):
                # HTTPError's base chain ends at tempfile._TemporaryFileCloser, whose
                # __del__ calls .close() on the fp once the error object is collected.
                # Without this the AttributeError is raised inside a GC finaliser, so
                # pytest reports it as an unraisable exception against whichever test
                # happens to be running - it passed alone and failed in the full suite.
                pass

        err = urllib.error.HTTPError("https://x/v1", 503, "down", {}, BadFP())
        with patch("urllib.request.urlopen", side_effect=err):
            h._serve_provider_test()
        data = _json_body(h)
        assert data["ok"] is False                    # 920-921: err_body = ""

    def test_http_error_cf_ray_in_body(self, temp_db):
        h = TestHandler(path="/api/providers/test", method="POST", engine=temp_db,
                        body=self._body())
        fp = io.BytesIO(b"Cloudflare error code: 1010 cf-ray: abc123-def")
        err = urllib.error.HTTPError("https://x/v1", 403, "blocked", {}, fp)
        with patch("urllib.request.urlopen", side_effect=err):
            h._serve_provider_test()
        data = _json_body(h)
        assert data["ok"] is False
        assert "Cloudflare" in data["error"]          # 938-941: cf-ray captured
        assert data.get("cf_ray") == "abc123-def"


class TestProviderDiscover:
    def test_generic_fallback_result_without_data_key(self, temp_db):
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db,
                        body={"api_base": "https://x/v1"})
        resp = MagicMock()
        resp.read.return_value = json.dumps({"weird": True}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("src.server.endpoints._credential_store_for", return_value=None):
            h._serve_provider_discover()
        data = _json_body(h)
        assert data["ok"] is True and data["models"] == []   # 1070: None -> []

    def test_generic_fallback_result_models_not_a_list(self, temp_db):
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db,
                        body={"api_base": "https://x/v1"})
        resp = MagicMock()
        resp.read.return_value = json.dumps({"data": {"x": 1}}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        with patch("urllib.request.urlopen", return_value=resp), \
             patch("src.server.endpoints._credential_store_for", return_value=None):
            h._serve_provider_discover()
        data = _json_body(h)
        assert data["models"] == []                   # 1072: non-list -> []

    def test_commandcode_enrichment_crash_swallowed(self, temp_db):
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db,
                        body={"api_base": "https://x/v1", "provider": "commandcode"})
        plugin = MagicMock()
        plugin.discover_models.return_value = [{"id": "m1"}]
        plugin.fetch_subscription.side_effect = RuntimeError("billing down")
        registry = MagicMock()
        registry.for_provider.return_value = plugin
        with patch("src.server.endpoints.resolve_service", return_value=registry):
            h._serve_provider_discover()
        data = _json_body(h)
        assert data["ok"] is True                     # 1124-1125: enrich crash ignored


# ── profile / budget branches ────────────────────────────────────────────────

class TestProfileBudgetBranches:
    def test_profile_update_sets_chain(self, temp_db):
        cfg = LCPHandler.config
        cfg.profiles["l2"]["chain"] = []
        h = TestHandler(path="/api/profiles/l2", method="PUT", engine=temp_db,
                        body={"chain": [{"provider": "deepseek", "model": "m"}]})
        h._serve_profile_update("l2")
        assert _status(h) == 200                      # 1225
        assert cfg.profiles["l2"]["chain"] == [{"provider": "deepseek", "model": "m"}]
        cfg.save.assert_called()

    def test_profile_budget_update_all_fields(self, temp_db):
        with get_session(temp_db) as s:
            b = Budget(name="pb", profile="l2", amount=5.0, period="monthly",
                       threshold_pct="80", action="notify", status="active")
            s.add(b)
            s.commit()
            bid = b.id
        h = TestHandler(path="/api/profiles/l2/budget", method="PUT", engine=temp_db,
                        body={"amount": 9.0, "period": "weekly",
                              "threshold_pct": "50", "action": "block",
                              "status": "paused"})
        h._serve_profile_budget_update("l2")
        assert _status(h) == 200                      # 1286: period updated
        with get_session(temp_db) as s:
            b = s.query(Budget).filter(Budget.id == bid).first()
            assert b.period == "weekly"
            assert b.amount == 9.0

    def test_key_detail_not_found_404(self, temp_db):
        h = TestHandler(path="/api/keys/999", engine=temp_db)
        h._serve_key_detail("999")
        assert _status(h) == 404                      # 1343

    def test_budgets_list_db_crash_500(self, temp_db):
        h = TestHandler(path="/api/budgets", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("x")):
            h._serve_budgets_list()
        assert _status(h) == 500                      # 1488-1490

    def test_budget_create_bad_amount_500(self, temp_db):
        h = TestHandler(path="/api/budgets", method="POST", engine=temp_db,
                        body={"name": "b", "amount": "not-a-number"})
        h._serve_budget_create()
        assert _status(h) == 500                      # 1513-1515

    def test_budget_update_all_fields(self, temp_db):
        with get_session(temp_db) as s:
            b = Budget(name="b1", amount=1.0, period="monthly",
                       threshold_pct="80", action="notify", status="active")
            s.add(b)
            s.commit()
            bid = b.id
        h = TestHandler(path=f"/api/budgets/{bid}", method="PUT", engine=temp_db,
                        body={"name": "b2", "key_id": 7, "profile": "l2",
                              "amount": 12.5, "period": "daily",
                              "threshold_pct": "50,90", "action": "block",
                              "status": "paused"})
        h._serve_budget_update(str(bid))
        assert _status(h) == 200                      # 1536, 1538, 1544
        with get_session(temp_db) as s:
            b = s.query(Budget).filter(Budget.id == bid).first()
            assert b.name == "b2" and b.period == "daily" and b.key_id == 7

    def test_budget_update_bad_amount_500(self, temp_db):
        with get_session(temp_db) as s:
            b = Budget(name="b1", amount=1.0, status="active")
            s.add(b)
            s.commit()
            bid = b.id
        h = TestHandler(path=f"/api/budgets/{bid}", method="PUT", engine=temp_db,
                        body={"amount": "oops"})
        h._serve_budget_update(str(bid))
        assert _status(h) == 500                      # 1553-1555

    def test_budget_delete_invalid_id(self, temp_db):
        h = TestHandler(path="/api/budgets/abc", method="DELETE", engine=temp_db)
        h._serve_budget_delete("abc")
        assert _status(h) == 400                      # 1561-1563

    def test_budget_delete_db_crash_500(self, temp_db):
        h = TestHandler(path="/api/budgets/5", method="DELETE", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("x")):
            h._serve_budget_delete("5")
        assert _status(h) == 500                      # 1573-1575

    def test_budgets_status_db_crash_500(self, temp_db):
        h = TestHandler(path="/api/budgets/status", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("x")):
            h._serve_budgets_status()
        assert _status(h) == 500                      # 1597-1599
