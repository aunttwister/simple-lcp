"""Batch L: final residual gaps — lancedb, refresher, commandcode,
endpoints, handler routes.

Targets: lancedb_backend.py 201; cost_cache.py 632, 647-649;
commandcode_api.py 356-358; endpoints.py 1124-1125, 2318-2319, 3111-3112;
handler.py 196-197, 318, 348.
"""
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from src.server import LCPHandler
from src.api.models import Base, get_engine


@pytest.fixture
def temp_db():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + ext)
        except FileNotFoundError:
            pass


def _json(handler):
    for call in handler.wfile.write.call_args_list:
        try:
            return json.loads(call[0][0])
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return {}


class _TH(LCPHandler):
    """Minimal in-process handler (same shape as test_batch_f3_handler_gaps)."""

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


# ── lancedb_backend.py: MemoryError re-raise inside recall search ────────────

class TestLanceRecallMemoryError:
    def test_memory_error_propagates(self, tmp_path):
        # 201: MemoryError from the search block propagates, NOT wrapped
        from src.api.memory.lancedb_backend import LanceDBMemoryBackend
        from src.api.memory.base import MemoryError as MemErr

        def fake_embed(texts):
            return [[0.1] * 16 for _ in texts]

        b = LanceDBMemoryBackend(str(tmp_path / "mem"), fake_embed, dim=16)
        b.retain("keep me", profile="p1")

        class BoomTable:
            def search(self, v):
                raise MemErr("lance oom")

        with patch.object(b, "_table", return_value=BoomTable()):
            with pytest.raises(MemErr):
                b.recall("keep", top_k=3, profile="p1")


# ── cost_cache.py CacheRefresher: no-plugin + throttle branches ──────────────

class TestRefresherGaps:
    def test_provider_without_plugin_skipped(self, tmp_path):
        # 632: registry provider with no plugin object
        from src.api.cost_cache import CacheRefresher, CostPluginCache, SettingsStore
        from src.api.models import Base, get_engine

        engine = get_engine(str(tmp_path / "cc.db"))
        Base.metadata.create_all(engine)
        cache = CostPluginCache(engine)
        settings = SettingsStore(engine)
        settings.set_ttl_minutes(30)

        class EmptyReg:
            providers = ["ghostprov"]

            def for_provider(self, name):
                return None

        r = CacheRefresher(cache, settings, registry_getter=lambda: EmptyReg(),
                           tick_seconds=1000, throttle_seconds=0,
                           backoff_base=60, backoff_cap=1800)
        r._pass()  # ghostprov has no plugin → continue, no crash
        assert cache.get("ghostprov", "subscription") is None

    def test_throttle_defers_to_pending(self, tmp_path):
        # 647-649: stale + throttle window active → key re-queued as pending
        from src.api.cost_cache import CacheRefresher, CostPluginCache, SettingsStore
        from src.api.models import Base, get_engine
        from tests.test_cost_cache_refresher import (
            FakeSubPlugin, FakeRegistry)

        engine = get_engine(str(tmp_path / "cc2.db"))
        Base.metadata.create_all(engine)
        cache = CostPluginCache(engine)
        settings = SettingsStore(engine)
        settings.set_ttl_minutes(0)          # everything is always stale
        plugin = FakeSubPlugin(sub={"monthly_pct": 1.0})
        reg = FakeRegistry({"opencode": plugin})
        r = CacheRefresher(cache, settings, registry_getter=lambda: reg,
                           tick_seconds=1000, throttle_seconds=3600,
                           backoff_base=60, backoff_cap=1800)
        r._last_attempt[("opencode", "subscription")] = __import__("time").time()
        r._pass()
        assert ("opencode", "subscription") in r._pending
        assert plugin.calls["sub"] == 0     # never scraped — throttled


# ── commandcode_api: credits parse failure ──────────────────────────────────

class TestCommandCodeParseFail:
    def test_parse_credits_raises_returns_none(self):
        # 356-358: _parse_credits raises → warning + None
        import src.api.cost_plugins.commandcode_api as cc
        with patch.object(cc, "_http_get_json", return_value={"odd": 1}), \
             patch.object(cc, "_parse_credits",
                          side_effect=ValueError("bad payload")):
            assert cc.fetch_subscription_snapshot("cookie-x") is None


# ── endpoints: discover enrich error, registry 500, setup skip 500 ──────────

class TestEndpointErrorBranches:
    def test_discover_commandcode_subscription_error(self):
        # endpoints.py 1124-1125: commandcode plan enrichment raises → pass
        from src.server.endpoints import ProviderEndpoints
        ep = ProviderEndpoints()
        ep._read_body = MagicMock(
            return_value={"provider": "commandcode",
                          "api_base": "https://cc.example/v1"})
        ep._send_json = MagicMock()

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"models": ["a"]}).encode()

        plugin = MagicMock()
        plugin.discover_models.return_value = None   # → generic HTTP fallback
        plugin.fetch_subscription.side_effect = RuntimeError("billing down")
        reg = MagicMock()
        reg.for_provider.return_value = plugin
        with patch("urllib.request.urlopen", return_value=Resp()), \
             patch("src.server.endpoints.resolve_service", return_value=reg):
            ep._serve_provider_discover()
        payload = ep._send_json.call_args[0][0]
        assert payload["ok"] is True and "plan_id" not in payload

    def test_registry_api_500(self, temp_db):
        # endpoints.py 2318-2319: query blows up → 500 error dict
        h = _TH("/api/models/registry", engine=temp_db)
        with patch("src.api.models.get_session",
                   side_effect=RuntimeError("registry unreadable")):
            h._serve_registry_api()
        assert h._status() == 500
        assert "registry unreadable" in _json(h)["error"]

# ── handler.py: static traversal via do_GET + branch routes ──────────────────

class TestHandlerRoutes:
    def test_static_escape_after_resolve(self, temp_db):
        # handler.py 196-197: no '..' in the name, but resolve() lands outside
        # static_dir (symlink-shaped escape) → second guard returns 403
        import os as _os
        from pathlib import Path
        import src.server.handler as H
        real_orig = _os.path.realpath
        static_dir = (Path(H.__file__).resolve().parent.parent
                      / "ui" / "templates" / "jinja" / "static")

        def fake_realpath(p, *a, **k):
            if "dashboard.css" in str(p):
                return "/tmp/outside-tree/dashboard.css"
            return real_orig(p)

        h = _TH("/static/dashboard.css", engine=temp_db)
        with patch("os.path.realpath", side_effect=fake_realpath):
            h._serve_static()
        assert h._status() == 403

    def test_plugin_balances_route(self, temp_db):
        # handler.py 318 → dispatch to _serve_plugin_balances
        h = _TH("/api/cost-plugins/balances", engine=temp_db)
        with patch.object(LCPHandler, "_serve_plugin_balances") as m:
            h.do_GET()
        m.assert_called_once()

    def test_capability_route(self, temp_db):
        # handler.py 348 → dispatch to _serve_capability_api
        h = _TH("/api/models/capability", engine=temp_db)
        with patch.object(LCPHandler, "_serve_capability_api") as m:
            h.do_GET()
        m.assert_called_once()
