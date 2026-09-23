"""Final endpoint gap coverage: capability API release filter, provider
discover cookie/workspace enrichment, profile update/delete, provider toggle
degrade, and the remaining setup remove branches."""

import contextlib
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


# ── Capability API: release filter + source filter ──────────────────────────

class TestCapabilityApiFilter:
    def _seed_capability(self, temp_db, model="m", task="code_generation", source="livebench", release="2026-06-25"):
        from src.api.models import ModelCapability, get_session
        with get_session(temp_db) as s:
            s.add(ModelCapability(model=model, task_type=task, score=0.8, source=source,
                                  release_label=release, updated_at="x"))
            s.commit()

    def test_release_filter(self, temp_db):
        from tests.test_server import TestHandler, _json_body
        self._seed_capability(temp_db)
        h = TestHandler(path="/api/models/capability?release=2026-06-25", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 200
        body = _json_body(h)
        assert "m" in body["tasks"]["code_generation"]
        assert body["count"] == 1

    def test_source_filter(self, temp_db):
        from tests.test_server import TestHandler, _json_body
        self._seed_capability(temp_db, source="manual", release=None)
        h = TestHandler(path="/api/models/capability?source=manual", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 200
        body = _json_body(h)
        assert "m" in body["tasks"]["code_generation"]

    def test_capability_api_error_500(self, temp_db):
        from tests.test_server import TestHandler
        with patch("src.api.models.get_session", side_effect=RuntimeError("db down")):
            h = TestHandler(path="/api/models/capability", engine=temp_db)
            h.do_GET()
        assert h.send_response.call_args[0][0] == 500


# ── Provider discover: cookie + workspace-id enrichment ─────────────────────

class TestDiscoverEnrichment:
    @patch("urllib.request.urlopen")
    def test_discover_with_cookie_and_workspace(self, mock_urlopen, temp_db):
        from tests.test_server import TestHandler
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": [{"id": "m1"}]}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        store = MagicMock()
        store.get.return_value = "sk-key"
        store.get_cookie.return_value = "session=c"
        store.get_workspace_id.return_value = "wrk_1"
        body = json.dumps({"api_base": "https://api.opencode.ai", "provider": "opencode"})
        with patch("src.server.endpoints.get_credential_store", return_value=store):
            h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
            h.do_POST()
        assert h.send_response.call_args[0][0] == 200
        # The request should carry Cookie + X-Workspace-Id headers. urllib
        # title-cases header keys, so lookup is case-insensitive here.
        req = mock_urlopen.call_args[0][0]
        headers = {k.lower(): v for k, v in req.headers.items()}
        assert headers.get("cookie") == "session=c"
        assert headers.get("x-workspace-id") == "wrk_1"
        assert headers.get("authorization") == "Bearer sk-key"


# ── Profile update/delete ────────────────────────────────────────────────────

class TestProfileEdges:
    def test_profile_update_forbidden_tools(self, temp_db):
        from tests.test_server import TestHandler
        from src.server import LCPHandler
        body = json.dumps({"forbidden_tools": ["terminal"], "auth_required": True})
        h = TestHandler(path="/api/profiles/l2", method="PUT", engine=temp_db, body=body)
        h.do_PUT()
        assert h.send_response.call_args[0][0] == 200
        assert LCPHandler.config.raw["profiles"]["l2"]["forbidden_tools"] == ["terminal"]

    def test_profile_delete(self, temp_db):
        from tests.test_server import TestHandler
        from src.server import LCPHandler
        LCPHandler.config.profiles["tempprof"] = {"chain": []}
        LCPHandler.config.raw["profiles"]["tempprof"] = {"chain": []}
        h = TestHandler(path="/api/profiles/tempprof", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert h.send_response.call_args[0][0] == 200
        assert "tempprof" not in LCPHandler.config.raw["profiles"]


# ── Provider toggle: valid degrade through the real route ───────────────────

class TestProviderToggleRoute:
    def test_toggle_degrade(self, temp_db):
        from tests.test_server import TestHandler
        body = json.dumps({"profile": "l2", "action": "degrade"})
        h = TestHandler(path="/api/providers/deepseek/toggle", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert h.send_response.call_args[0][0] == 200


# ── Setup: module install + remove through the route ────────────────────────
