"""Tests for server.py — auth enforcement, API endpoints, page routes."""

import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from src.server import LCPHandler


# Set up class-level config before any handler tests
@pytest.fixture(autouse=True)
def _setup_handler_config(temp_db):
    """Ensure LCPHandler.config and key manager are set for all tests."""
    from unittest.mock import MagicMock
    from src.api.key_manager import KeyManager
    import src.api.key_manager as key_manager_mod

    engine = temp_db
    key_manager_mod._key_manager = KeyManager(engine, "data")

    cfg = MagicMock()
    cfg.server = {"port": 8734, "default_profile": "l2"}
    cfg.profiles = {
        "l2": {
            "forbidden_tools": ["write_file"],
            "chain": [
                {"provider": "opencode", "model": "deepseek-v4-pro", "base_url": "https://test/v1"},
                {"provider": "deepseek", "model": "deepseek-v4-pro", "base_url": "https://test/v1"},
            ],
            "auth_required": True,
        },
        "l1": {
            "forbidden_tools": [],
            "chain": [{"provider": "deepseek", "model": "deepseek-v4-flash", "base_url": "https://test/v1"}],
            "auth_required": True,
        },
        "career": {
            "forbidden_tools": ["terminal"],
            "chain": [{"provider": "deepseek", "model": "deepseek-v4-flash", "base_url": "https://test/v1"}],
            "auth_required": True,
        },
        "cron": {
            "forbidden_tools": None,
            "chain": [{"provider": "deepseek", "model": "deepseek-v4-flash", "base_url": "https://test/v1"}],
            "auth_required": True,
        },
    }
    cfg.providers = {
        "opencode": {"api_key_env": "OK", "api_base": "https://test/v1", "models": ["deepseek-v4-pro", "deepseek-v4-flash"]},
        "deepseek": {"api_key_env": "DK", "api_base": "https://test/v1", "models": ["deepseek-v4-pro", "deepseek-v4-flash"]},
    }
    cfg.pricing = [
        {"provider": "opencode", "model": "deepseek-v4-pro", "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0},
        {"provider": "deepseek", "model": "deepseek-v4-flash", "cache_hit": 0.005, "cache_miss": 0.1, "output": 0.2},
    ]
    cfg.circuit_breaker = {"failures_dead": 5, "dead_cooldown_seconds": 300, "failures_degraded": 3, "degraded_cooldown_seconds": 60}
    cfg.database = {"path": "/tmp/test.db", "wal_mode": True}
    cfg.model_limits = {
        "deepseek-v4-pro": {"context_window": 1000000, "max_output_tokens": 384000, "description": "pro model"},
        "deepseek-v4-flash": {"context_window": 1000000, "max_output_tokens": 384000, "description": "flash model"},
    }
    cfg.get_profile = lambda name: cfg.profiles.get(name)
    cfg.get_pricing = lambda provider, model: next((p for p in cfg.pricing if p["provider"] == provider and p["model"] == model), cfg.pricing[0])
    cfg.get_provider_key = lambda name: "test-key"
    cfg.check_reload = MagicMock()

    # Make raw and save work together for CRUD tests
    cfg.raw = {
        "server": cfg.server,
        "profiles": {k: dict(v) for k, v in cfg.profiles.items()},
        "providers": {k: dict(v) for k, v in cfg.providers.items()},
        "pricing": list(cfg.pricing),
        "circuit_breaker": dict(cfg.circuit_breaker),
        "database": dict(cfg.database),
    }
    # Sync profiles/providers back from raw when save is called
    def _save():
        cfg.profiles = cfg.raw.get("profiles", {})
        cfg.providers = cfg.raw.get("providers", {})
    cfg.save = MagicMock(side_effect=_save)

    LCPHandler.config = cfg


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
        body_bytes = (body or b"{}") if isinstance(body or b"{}", bytes) else (body or "{}").encode()
        self.rfile.read = MagicMock(return_value=body_bytes)
        if body:
            self.headers["Content-Length"] = str(len(body_bytes))
        self._write_chunk = MagicMock()
        self.engine = engine
        self.log_error = MagicMock()


def _status(handler):
    """Get the response status code sent."""
    return handler.send_response.call_args[0][0] if handler.send_response.call_args else None


def _json_body(handler):
    """Parse the JSON body written to wfile."""
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
    from src.api.models import get_engine, Base
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    for ext in ["", "-wal", "-shm"]:
        try:
            os.unlink(db_path + ext)
        except FileNotFoundError:
            pass


# ═══════════════════════════════════════════════════════════════════════
# Auth enforcement tests
# ═══════════════════════════════════════════════════════════════════════

class TestAuthEnforcement:
    """Tests for API key auth on chat completions."""

    def test_requires_auth_for_protected_profile(self, temp_db):
        """A profile with auth_required=true should reject unauthenticated requests."""
        h = TestHandler(path="/l2/chat/completions", method="POST", engine=temp_db)
        h.do_POST()
        assert _status(h) == 401
        body = _json_body(h)
        err = body.get("error", {})
        assert isinstance(err, dict)
        assert err.get("code") == "LCP-1001"
        assert "API key required" in err.get("message", "")

    def test_rejects_invalid_key(self, temp_db):
        """A bad API key should get 401."""
        h = TestHandler(path="/l2/chat/completions", method="POST", engine=temp_db,
                        headers={"Authorization": "Bearer badkey123"})
        h.do_POST()
        assert _status(h) == 401

    def test_accepts_valid_key(self, temp_db):
        """A valid key should pass auth and proceed to the pipeline."""
        from src.api.key_manager import KeyManager
        import src.api.key_manager as key_manager_mod
        km = key_manager_mod._key_manager = KeyManager(temp_db, "data")
        result = km.create_key(name="Test", allowed_profiles="l2")
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 5, "stream": False})
        h = TestHandler(path="/l2/chat/completions", method="POST", engine=temp_db,
                        headers={"Authorization": f"Bearer {result['key']}"}, body=body)
        h.do_POST()
        status = _status(h)
        # Auth passes; pipeline may fail due to real provider call
        assert status in (200, 502, 500)

    def test_rejects_key_without_profile_access(self, temp_db):
        """A key limited to l1 should not access l2."""
        from src.api.key_manager import KeyManager
        import src.api.key_manager as key_manager_mod
        km = key_manager_mod._key_manager = KeyManager(temp_db, "data")
        result = km.create_key(name="L1 Only", allowed_profiles="l1")
        h = TestHandler(path="/l2/chat/completions", method="POST", engine=temp_db,
                        headers={"Authorization": f"Bearer {result['key']}"})
        h.do_POST()
        assert _status(h) == 403
        body = _json_body(h)
        err = body.get("error", {})
        assert isinstance(err, dict)
        assert err.get("code") == "LCP-1003"
        assert "access to profile" in err.get("message", "")

    def test_allows_public_profile(self, temp_db):
        """A profile with auth_required=false should allow access without key."""
        # Make l1 profile public via the raw config (handler reads from config.profiles)
        LCPHandler.config.profiles["l1"]["auth_required"] = False
        h = TestHandler(path="/l1/chat/completions", method="POST", engine=temp_db)
        h.rfile.read.return_value = json.dumps({
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
            "stream": False,
        }).encode()
        h.do_POST()
        # No 401 — either 200/502 depending on provider availability
        assert _status(h) != 401


# ═══════════════════════════════════════════════════════════════════════
# API Key endpoint tests
# ═══════════════════════════════════════════════════════════════════════


class TestKeyEndpoints:
    def test_list_keys(self, temp_db):
        from src.api.key_manager import KeyManager
        import src.api.key_manager as key_manager_mod
        km = key_manager_mod._key_manager = KeyManager(temp_db, "data")
        km.create_key(name="K1")
        h = TestHandler(path="/api/keys", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert len(body.get("keys", [])) >= 1

    def test_create_key(self, temp_db):
        from src.api.key_manager import KeyManager
        import src.api.key_manager as key_manager_mod
        key_manager_mod._key_manager = KeyManager(temp_db, "data")
        body = json.dumps({"name": "New Key", "allowed_profiles": "l2", "spend_limit": 100})
        h = TestHandler(path="/api/keys", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 200
        assert _json_body(h).get("key", "").startswith("lcp_")

    def test_key_detail(self, temp_db):
        from src.api.key_manager import KeyManager
        import src.api.key_manager as key_manager_mod
        km = key_manager_mod._key_manager = KeyManager(temp_db, "data")
        created = km.create_key(name="Detail")
        h = TestHandler(path=f"/api/keys/{created['id']}", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["key"]["name"] == "Detail"

    def test_rotate_key(self, temp_db):
        from src.api.key_manager import KeyManager
        import src.api.key_manager as key_manager_mod
        km = key_manager_mod._key_manager = KeyManager(temp_db, "data")
        created = km.create_key(name="Rotate")
        h = TestHandler(path=f"/api/keys/{created['id']}/rotate", method="POST", engine=temp_db)
        h.do_POST()
        assert _status(h) == 200
        body = _json_body(h)
        assert body.get("ok") is True
        assert body.get("old_id") == created["id"]

    def test_delete_key(self, temp_db):
        from src.api.key_manager import KeyManager
        import src.api.key_manager as key_manager_mod
        km = key_manager_mod._key_manager = KeyManager(temp_db, "data")
        created = km.create_key(name="Delete Me")
        h = TestHandler(path=f"/api/keys/{created['id']}", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 200


# ═══════════════════════════════════════════════════════════════════════
# Provider endpoint tests
# ═══════════════════════════════════════════════════════════════════════

class TestProviderEndpoints:
    def test_list_providers(self, temp_db):
        h = TestHandler(path="/api/providers", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert "providers" in body

    def test_presets(self, temp_db):
        # Seed a populated plugin registry (the runtime component builds one
        # with the built-ins; tests construct it directly).
        import src.api.cost_plugins.base as cpb
        from src.api.cost_plugins.commandcode import CommandCodeCostPlugin
        from src.api.cost_plugins.deepseek import DeepSeekCostPlugin
        from src.api.cost_plugins.llamacpp import LlamaCppCostPlugin
        from src.api.cost_plugins.opencode import OpenCodeCostPlugin
        reg = cpb.PluginRegistry()
        reg.register(DeepSeekCostPlugin())
        reg.register(OpenCodeCostPlugin())
        reg.register(LlamaCppCostPlugin())
        reg.register(CommandCodeCostPlugin())
        cpb._registry = reg
        h = TestHandler(path="/api/providers/presets", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert "presets" in body
        assert "deepseek" in body["presets"]
        assert "opencode" in body["presets"]
        assert "llamacpp" in body["presets"]
        assert body["presets"]["llamacpp"]["models"] == []

    def test_create_provider(self, temp_db):
        body = json.dumps({"name": "newco", "api_base": "https://new.api/v1", "api_key_env": "NEWCO_KEY", "models": ["m1"]})
        h = TestHandler(path="/api/providers", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 200

    def test_create_provider_with_api_key_stored_encrypted(self, temp_db, tmp_path):
        """api_key sent to POST /api/providers is stored in credential store, not YAML."""
        from src.api.credential_store import CredentialStore
        from src.api.models import ProviderCredential, get_session
        import src.api.credential_store as cs_module
        import os as _os
        from unittest.mock import patch as _patch

        store = CredentialStore(temp_db, data_dir=str(tmp_path))
        cs_module._credential_store = store

        body = json.dumps({"name": "secco", "api_base": "https://sec.api/v1", "api_key": "sk-secret"})
        with _patch.dict(_os.environ, {"LCP_SECRET_KEY": "test-master"}, clear=False):
            h = TestHandler(path="/api/providers", method="POST", engine=temp_db, body=body)
            h.do_POST()
            assert _status(h) == 200

            # Key is retrievable via store (same master key active)
            assert store.get("secco") == "sk-secret"
        # And NOT stored in the YAML config
        assert "sk-secret" not in json.dumps(LCPHandler.config.providers.get("secco", {}))
        # And stored encrypted in DB (ciphertext != plaintext)
        with _patch.dict(_os.environ, {"LCP_SECRET_KEY": "test-master"}, clear=False):
            with get_session(temp_db) as session:
                row = session.query(ProviderCredential).filter(
                    ProviderCredential.provider == "secco"
                ).first()
        assert row is not None
        assert "sk-secret" not in row.encrypted_key

    def test_update_provider(self, temp_db):
        body = json.dumps({"api_base": "https://updated.api/v1"})
        h = TestHandler(path="/api/providers/opencode", method="PUT", engine=temp_db, body=body)
        h.do_PUT()
        assert _status(h) == 200

    def test_delete_provider(self, temp_db):
        # First create one
        body = json.dumps({"name": "tempco", "api_base": "https://temp.api/v1"})
        h = TestHandler(path="/api/providers", method="POST", engine=temp_db, body=body)
        h.do_POST()
        # Then delete
        h = TestHandler(path="/api/providers/tempco", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 200

    @patch("urllib.request.urlopen")
    def test_discover_models_with_metadata(self, mock_urlopen, temp_db):
        """Discover endpoint returns models with metadata when available."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": [
                {"id": "m1", "owned_by": "org1", "context_length": 128000, "created": 1700000000},
                {"id": "m2", "owned_by": "org2", "context_length": 64000},
            ]
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        body = json.dumps({"api_base": "https://test.api/v1", "api_key": "sk-test"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is True
        assert result["has_metadata"] is True
        assert result["count"] == 2
        assert len(result["models"]) == 2
        assert result["models"][0]["id"] == "m1"
        assert result["models"][0]["owned_by"] == "org1"
        assert result["models"][0]["context_length"] == 128000

    @patch("urllib.request.urlopen")
    def test_discover_models_without_metadata(self, mock_urlopen, temp_db):
        """Discover returns has_metadata=False when only IDs present."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        body = json.dumps({"api_base": "https://test.api/v1", "api_key": "sk-test"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is True
        assert result["has_metadata"] is False
        assert result["count"] == 3

    @patch("urllib.request.urlopen")
    def test_discover_models_http_error(self, mock_urlopen, temp_db):
        """Discover handles HTTP errors gracefully."""
        mock_urlopen.side_effect = Exception("Connection refused")

        body = json.dumps({"api_base": "https://dead.api/v1", "api_key": "sk-test"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is False
        assert "error" in result

    def test_discover_delegates_to_plugin(self, temp_db):
        """When a plugin has discover_models, endpoint returns response (may fail if unreachable)."""
        body = json.dumps({"api_base": "http://llama.cpp/v1", "provider": "llamacpp"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert "ok" in result  # True if reaches server, False with error otherwise

    def test_discover_missing_api_base(self, temp_db):
        body = json.dumps({"provider": "deepseek"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 400

    @patch("urllib.request.urlopen")
    def test_discover_resolves_key_from_credential_store(self, mock_urlopen, temp_db):
        """Discover endpoint uses credential store when no api_key in body."""
        from unittest.mock import patch as _patch
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": [{"id": "m1", "owned_by": "org1"}]
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        store = MagicMock()
        store.get.return_value = "sk-stored"
        body = json.dumps({"api_base": "https://test.api/v1", "provider": "deepseek"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        with _patch("src.server.endpoints.get_credential_store", return_value=store):
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is True


# ═══════════════════════════════════════════════════════════════════════
# Plugin cookie endpoint tests
# ═══════════════════════════════════════════════════════════════════════

class TestPluginCookieEndpoints:
    def test_cookie_get_not_set(self, temp_db):
        from src.api.credential_store import CredentialStore
        import src.api.credential_store as cs_module
        cs_module._credential_store = CredentialStore(temp_db, data_dir="tests-data")
        h = TestHandler(path="/api/cost-plugins/cookie/opencode", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["provider"] == "opencode"
        assert body["has_cookie"] is False

    def test_cookie_set_then_get(self, temp_db):
        from src.api.credential_store import CredentialStore
        import src.api.credential_store as cs_module
        import os as _os
        from unittest.mock import patch as _patch
        cs_module._credential_store = CredentialStore(temp_db, data_dir="tests-data")
        body = json.dumps({"cookie": "auth=abc123"})
        with _patch.dict(_os.environ, {"LCP_SECRET_KEY": "test-master"}, clear=False):
            h = TestHandler(path="/api/cost-plugins/cookie/opencode", method="POST", engine=temp_db, body=body)
            h.do_POST()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["ok"] is True
        assert body["has_cookie"] is True
        # GET reflects stored cookie (value never returned)
        h = TestHandler(path="/api/cost-plugins/cookie/opencode", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["has_cookie"] is True

    def test_cookie_clear(self, temp_db):
        from src.api.credential_store import CredentialStore
        import src.api.credential_store as cs_module
        import os as _os
        from unittest.mock import patch as _patch
        cs_module._credential_store = CredentialStore(temp_db, data_dir="tests-data")
        with _patch.dict(_os.environ, {"LCP_SECRET_KEY": "test-master"}, clear=False):
            h = TestHandler(path="/api/cost-plugins/cookie/opencode", method="POST", engine=temp_db,
                            body=json.dumps({"cookie": "auth=abc123"}))
            h.do_POST()
            h = TestHandler(path="/api/cost-plugins/cookie/opencode", method="POST", engine=temp_db,
                            body=json.dumps({"cookie": ""}))
            h.do_POST()
        h = TestHandler(path="/api/cost-plugins/cookie/opencode", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["has_cookie"] is False


# ═══════════════════════════════════════════════════════════════════════
# Profile endpoint tests
# ═══════════════════════════════════════════════════════════════════════

class TestProfileEndpoints:
    def test_list_profiles(self, temp_db):
        h = TestHandler(path="/api/profiles", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert "profiles" in body

    def test_create_profile(self, temp_db):
        body = json.dumps({"name": "testprof"})
        h = TestHandler(path="/api/profiles", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 200

    def test_update_profile(self, temp_db):
        body = json.dumps({"forbidden_tools": ["test_tool"]})
        h = TestHandler(path="/api/profiles/l2", method="PUT", engine=temp_db, body=body)
        h.do_PUT()
        assert _status(h) == 200

    def test_delete_profile(self, temp_db):
        body = json.dumps({"name": "tempprof"})
        h = TestHandler(path="/api/profiles", method="POST", engine=temp_db, body=body)
        h.do_POST()
        h = TestHandler(path="/api/profiles/tempprof", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 200

    def test_chain_reorder(self, temp_db):
        body = json.dumps({"chain": [{"provider": "deepseek", "model": "deepseek-v4-flash"}, {"provider": "opencode", "model": "deepseek-v4-pro"}]})
        h = TestHandler(path="/api/chains/l2", method="PUT", engine=temp_db, body=body)
        h.do_PUT()
        assert _status(h) == 200

    def test_create_profile_missing_name(self, temp_db):
        body = json.dumps({"notname": ""})
        h = TestHandler(path="/api/profiles", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 400

    def test_create_profile_duplicate(self, temp_db):
        body = json.dumps({"name": "l2"})
        h = TestHandler(path="/api/profiles", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 409


# ═══════════════════════════════════════════════════════════════════════
# Static endpoint tests
# ═══════════════════════════════════════════════════════════════════════

class TestStaticEndpoints:
    def test_health(self, temp_db):
        h = TestHandler(path="/health", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_models(self, temp_db):
        h = TestHandler(path="/v1/models", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert "data" in body
        assert isinstance(body["data"], list)
        # Only profiles are now exposed
        assert len(body["data"]) == 4

        # ── Verify profile entries ──

        # l2 profile should appear as a virtual model
        l2 = next((m for m in body["data"] if m["id"] == "l2"), None)
        assert l2 is not None
        assert l2["object"] == "model"
        assert l2["owned_by"] == "lcp"
        assert l2["kind"] == "profile"
        assert l2["context_window"] == 1000000
        assert len(l2["providers"]) == 2  # opencode + deepseek in l2 chain

        # career profile — single chain step
        career = next((m for m in body["data"] if m["id"] == "career"), None)
        assert career is not None
        assert career["kind"] == "profile"
        assert len(career["providers"]) == 1
        assert career["providers"][0]["model"] == "deepseek-v4-flash"

    def test_models_no_limits_falls_back_to_default_context_length(self, temp_db):
        """When model_limits is empty/missing, providers use 128000 default."""
        original_limits = LCPHandler.config.model_limits
        try:
            LCPHandler.config.model_limits = {}
            h = TestHandler(path="/v1/models", engine=temp_db)
            h.do_GET()
            body = _json_body(h)
            for m in body["data"]:
                for p in m["providers"]:
                    assert p["context_length"] == 128000
                    assert p["supports_tools"] is True
        finally:
            LCPHandler.config.model_limits = original_limits

    def test_models_per_profile_v1(self, temp_db):
        """GET /l2/v1/models returns l2 profile's models + the profile itself."""
        h = TestHandler(path="/l2/v1/models", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        # 1 profile only (raw models hidden)
        assert len(body["data"]) == 1
        model_ids = {m["id"] for m in body["data"]}
        assert model_ids == {"l2"}
        # Profile entry
        prof = next(m for m in body["data"] if m["id"] == "l2")
        assert prof["kind"] == "profile"
        assert prof["owned_by"] == "lcp"

    def test_models_per_profile_short(self, temp_db):
        """GET /l2/models (no /v1) also works for oaicopilot compat."""
        h = TestHandler(path="/l2/models", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        # 1 profile only (raw models hidden)
        assert len(body["data"]) == 1
        model_ids = {m["id"] for m in body["data"]}
        assert model_ids == {"l2"}

    def test_models_per_profile_l1(self, temp_db):
        """GET /l1/models returns l1 profile entry only."""
        h = TestHandler(path="/l1/models", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        # 1 profile only (raw models hidden)
        assert len(body["data"]) == 1
        model_ids = {m["id"] for m in body["data"]}
        assert model_ids == {"l1"}

    def test_models_per_profile_unknown_404(self, temp_db):
        """Unknown profile in path returns 404."""
        h = TestHandler(path="/nonexistent/models", engine=temp_db)
        h.do_GET()
        assert _status(h) == 404

    def test_errors(self, temp_db):
        h = TestHandler(path="/errors", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_cache_stats(self, temp_db):
        h = TestHandler(path="/cache/stats", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_metrics(self, temp_db):
        h = TestHandler(path="/metrics", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_export(self, temp_db):
        h = TestHandler(path="/export", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_daily_costs_api(self, temp_db):
        h = TestHandler(path="/api/daily-costs", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_recent_requests_api(self, temp_db):
        h = TestHandler(path="/api/recent-requests", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_page_dashboard(self, temp_db):
        h = TestHandler(path="/dashboard", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_page_keys(self, temp_db):
        from src.api.key_manager import KeyManager
        import src.api.key_manager as key_manager_mod
        key_manager_mod._key_manager = KeyManager(temp_db, "data")
        h = TestHandler(path="/keys", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_page_providers(self, temp_db):
        h = TestHandler(path="/providers", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_page_profiles(self, temp_db):
        h = TestHandler(path="/profiles", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_404(self, temp_db):
        h = TestHandler(path="/nonexistent", engine=temp_db)
        h.do_GET()
        assert _status(h) == 404

    def test_dashboard_with_profile_filter(self, temp_db):
        h = TestHandler(path="/l2/dashboard", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_per_profile_dashboard(self, temp_db):
        h = TestHandler(path="/l1/dashboard", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200


# ═══════════════════════════════════════════════════════════════════════
# Alert endpoint tests
# ═══════════════════════════════════════════════════════════════════════

class TestAlertEndpoints:
    def test_alerts_config_get(self, temp_db):
        h = TestHandler(path="/api/alerts/config", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_alerts_config_update(self, temp_db):
        h = TestHandler(path="/api/alerts/config", method="PUT", engine=temp_db)
        h.rfile.read.return_value = json.dumps({"webhook_url": "https://hook.test"}).encode()
        h.do_PUT()
        assert _status(h) == 200

    def test_alerts_list(self, temp_db):
        h = TestHandler(path="/api/alerts", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_alerts_active(self, temp_db):
        h = TestHandler(path="/api/alerts/active", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_alerts_test_webhook(self, temp_db):
        h = TestHandler(path="/api/alerts/webhook/test", method="POST", engine=temp_db)
        h.do_POST()
        assert _status(h) == 200
