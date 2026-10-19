"""Final targeted gaps: reasoning_store rehydrate fallback, handler
chat-completion error branches (profile-from-model, unknown profile, missing
messages, blocked budget), static file absolute-path forbidden, and small
remaining endpoint branches."""

import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def temp_db():
    import os
    import tempfile as _t
    from src.api.models import get_engine, Base
    fd, path = _t.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    for ext in ["", "-wal", "-shm"]:
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def _setup_handler_config(temp_db):
    from src.server import LCPHandler
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
    cfg.providers = {"deepseek": {"api_base": "https://t/v1", "models": ["deepseek-v4-pro"]}}
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
    LCPHandler.engine = temp_db


class _H:
    """Minimal handler stub for direct endpoint-method calls."""

    def __init__(self, engine):
        self.engine = engine
        self.path = "/"
        self.headers = {}
        self.send_response = MagicMock()
        self.send_header = MagicMock()
        self.end_headers = MagicMock()
        self.wfile = MagicMock()
        self.wfile.write = MagicMock()
        self._send_json = MagicMock()
        self._read_body = MagicMock(return_value={})


# ── reasoning_store: rehydrate fallback ─────────────────────────────────────

class TestReasoningRehydrate:
    def test_rehydrate_uses_tool_call_id_fallback(self):
        from src.api.reasoning_store import ReasoningStore
        store = ReasoningStore()
        store.capture("fallback_id", "thinking")
        messages = [
            {"role": "assistant", "content": "x",
             "tool_calls": [{"tool_call_id": "fallback_id"}]},
        ]
        store.rehydrate(messages)
        assert messages[0]["reasoning_content"] == "thinking"

    def test_rehydrate_missing_lookup_untouched(self):
        from src.api.reasoning_store import ReasoningStore
        store = ReasoningStore()
        messages = [
            {"role": "assistant", "content": "x",
             "tool_calls": [{"id": "nope"}]},
        ]
        store.rehydrate(messages)
        assert "reasoning_content" not in messages[0]


# ── handler: static file absolute path forbidden ────────────────────────────

class TestStaticForbidden:
    def test_static_absolute_path_403(self, temp_db):
        from tests.test_server import TestHandler
        h = TestHandler(path="/static//etc/passwd", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 403


# ── handler: chat-completion error branches (direct method calls) ───────────

class TestChatCompletionBranches:
    def test_unknown_profile_in_path(self, temp_db):
        from tests.test_server import TestHandler
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}], "model": "nope"})
        h = TestHandler(path="/nope/chat/completions", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert h.send_response.call_args[0][0] == 400

    def test_profile_from_model_fallback(self, temp_db):
        """model=l2 in the body resolves the profile (no path profile)."""
        from tests.test_server import TestHandler
        # l2 profile has auth_required=False; pipeline may proceed to provider.
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}], "model": "l2", "stream": False})
        h = TestHandler(path="/chat/completions", method="POST", engine=temp_db, body=body)
        h.do_POST()
        # Should not 400 (profile resolved) — either proceeds or fails on provider.
        assert h.send_response.call_args[0][0] != 400

    def test_profile_cfg_none(self, temp_db):
        from tests.test_server import TestHandler
        from src.server import LCPHandler
        orig = LCPHandler.config.get_profile
        LCPHandler.config.get_profile = lambda name: None
        try:
            body = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
            h = TestHandler(path="/l2/chat/completions", method="POST", engine=temp_db, body=body)
            h.do_POST()
            assert h.send_response.call_args[0][0] == 400
        finally:
            LCPHandler.config.get_profile = orig

    def test_missing_messages_field(self, temp_db):
        from tests.test_server import TestHandler
        body = json.dumps({"model": "deepseek-v4-pro"})
        h = TestHandler(path="/l2/chat/completions", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert h.send_response.call_args[0][0] == 400

    def test_invalid_json_body(self, temp_db):
        from tests.test_server import TestHandler
        h = TestHandler(path="/l2/chat/completions", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(side_effect=json.JSONDecodeError("bad", "doc", 0))
        h.headers["Content-Length"] = "5"
        h.do_POST()
        assert h.send_response.call_args[0][0] == 400


# ── handler: _check_budget_block + budget enforcement ───────────────────────

class TestBudgetBlock:
    def test_budget_block_returns_429(self, temp_db):
        from tests.test_server import TestHandler
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
        h = TestHandler(path="/l2/chat/completions", method="POST", engine=temp_db, body=body)
        # Force a budget block for the profile (no key).
        with patch.object(h, "_check_budget_block", return_value="OverBudget"):
            h.do_POST()
        assert h.send_response.call_args[0][0] == 429


# ── endpoint: remaining branch coverage ─────────────────────────────────────

class TestEndpointRemaining:
    def test_models_with_vision_limit(self, temp_db):
        from tests.test_server import TestHandler, _json_body
        from src.server import LCPHandler
        LCPHandler.config.model_limits = {
            "deepseek-v4-pro": {"context_window": 999999, "supports_vision": True, "description": "Vis model"},
        }
        h = TestHandler(path="/v1/models", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 200
        body = _json_body(h)
        l2 = next(m for m in body["data"] if m["id"] == "l2")
        assert l2["context_window"] == 999999
        assert l2["supports_vision"] is True
        assert l2["description"] == "Vis model"

    def test_provider_health_with_tripped(self, temp_db):
        from tests.test_server import TestHandler, _json_body
        from src.api.circuit_breaker import get_circuit_breaker
        cb = get_circuit_breaker()
        # Register a dead provider entry so the health endpoint has data.
        cb.force_status("deepseek", "https://t/v1", "l2", "kill")
        h = TestHandler(path="/api/providers/health", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 200
        body = _json_body(h)
        assert body["summary"]["total"] >= 1

