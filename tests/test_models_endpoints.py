"""Tests for the models-related endpoints (capability, registry, benchmark)
that were previously uncovered in src/server/endpoints.py."""

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest

from src.server import LCPHandler


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
    key_manager_mod._key_manager = KeyManager(temp_db[1], "data")

    cfg = MagicMock()
    cfg.server = {"port": 8734, "default_profile": "l2"}
    cfg.profiles = {
        "l2": {"forbidden_tools": [], "chain": [{"provider": "deepseek", "model": "deepseek-v4-pro", "base_url": "https://t/v1"}]},
    }
    cfg.providers = {"deepseek": {"api_key_env": "DK", "api_base": "https://t/v1", "models": ["deepseek-v4-pro"]}}
    cfg.pricing = [{"provider": "deepseek", "model": "deepseek-v4-pro", "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0}]
    cfg.circuit_breaker = {"failures_dead": 5, "dead_cooldown_seconds": 300, "failures_degraded": 3, "degraded_cooldown_seconds": 60}
    cfg.database = {"path": "/tmp/test.db", "wal_mode": True}
    cfg.model_limits = {}
    cfg.get_profile = lambda name: cfg.profiles.get(name)
    cfg.get_pricing = lambda provider, model: cfg.pricing[0]
    cfg.get_provider_key = lambda name: "test-key"
    cfg.check_reload = MagicMock()
    cfg.raw = {}
    cfg.save = MagicMock()
    LCPHandler.config = cfg


# ── Capability API ──────────────────────────────────────────────────────────

class TestCapabilityApi:
    def test_get_capability(self, temp_db):
        h = TestHandler(path="/api/models/capability", engine=temp_db[1])
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert "tasks" in body
        assert "subtasks" in body
        assert "active_releases" in body

    def test_get_capability_source_filter(self, temp_db):
        h = TestHandler(path="/api/models/capability?source=manual", engine=temp_db[1])
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert "tasks" in body


# ── Registry API ────────────────────────────────────────────────────────────

class TestRegistryApi:
    def test_upsert_creates(self, temp_db):
        body = json.dumps({
            "logical_name": "new-model",
            "benchmark_key": "new-model",
            "provider_mappings": {"deepseek": "new-model"},
        })
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db[1], body=body)
        h.do_POST()
        assert _status(h) == 200
        assert _json_body(h)["action"] == "created"

    def test_upsert_missing_logical(self, temp_db):
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db[1],
                        body=json.dumps({"benchmark_key": "m"}))
        h.do_POST()
        assert _status(h) == 400

    def test_upsert_bad_mappings(self, temp_db):
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db[1],
                        body=json.dumps({"logical_name": "m", "benchmark_key": "m", "provider_mappings": {"a": 1}}))
        h.do_POST()
        assert _status(h) == 400

    def test_delete_registry(self, temp_db):
        # Create then delete.
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db[1],
                        body=json.dumps({"logical_name": "delme", "benchmark_key": "delme", "provider_mappings": {}}))
        h.do_POST()
        assert _status(h) == 200
        h = TestHandler(path="/api/models/registry/delme", method="DELETE", engine=temp_db[1])
        h.do_DELETE()
        assert _status(h) == 200
        assert _json_body(h)["deleted"] == "delme"

    def test_delete_missing(self, temp_db):
        h = TestHandler(path="/api/models/registry/nope", method="DELETE", engine=temp_db[1])
        h.do_DELETE()
        assert _status(h) == 404

    def test_upsert_rejects_reused_benchmark_key(self, temp_db):
        # model-a claims benchmark key "shared-key".
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db[1],
                        body=json.dumps({"logical_name": "model-a", "benchmark_key": "shared-key",
                                         "provider_mappings": {}}))
        h.do_POST()
        assert _status(h) == 200
        # A different logical model may not reuse that benchmark key.
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db[1],
                        body=json.dumps({"logical_name": "model-b", "benchmark_key": "shared-key",
                                         "provider_mappings": {}}))
        h.do_POST()
        assert _status(h) == 400
        assert "already registered" in _json_body(h)["error"]

    def test_upsert_rejects_duplicate_provider_mapping(self, temp_db):
        # model-a claims deepseek → deepseek-v4-pro.
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db[1],
                        body=json.dumps({"logical_name": "model-a", "benchmark_key": "model-a",
                                         "provider_mappings": {"deepseek": "deepseek-v4-pro"}}))
        h.do_POST()
        assert _status(h) == 200
        # model-b may not claim the same (provider, model) pair, even via a
        # differently-cased / path-qualified spelling of the model ID.
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db[1],
                        body=json.dumps({"logical_name": "model-b", "benchmark_key": "model-b",
                                         "provider_mappings": {"deepseek": "/models/DeepSeek-V4-Pro.gguf"}}))
        h.do_POST()
        assert _status(h) == 400
        assert "already maps" in _json_body(h)["error"]

    def test_upsert_allows_updating_same_model(self, temp_db):
        # Re-registering the SAME logical model with its own mappings is an
        # update, not a duplicate → allowed.
        body = json.dumps({"logical_name": "model-a", "benchmark_key": "model-a",
                           "provider_mappings": {"deepseek": "deepseek-v4-pro"}})
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db[1], body=body)
        h.do_POST()
        assert _status(h) == 200
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db[1], body=body)
        h.do_POST()
        assert _status(h) == 200
        assert _json_body(h)["action"] == "updated"


# ── Benchmark endpoints ──────────────────────────────────────────────────────
