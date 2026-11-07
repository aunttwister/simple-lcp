"""Tests for the admin Settings API + cache-read cost-plugin endpoints."""

import json
from unittest.mock import MagicMock

import pytest

import src.api.cost_cache as cc
from src.api.cost_cache import (
    CacheRefresher,
    CostPluginCache,
    _reset_singletons,
    get_cost_cache,
    get_settings,
    init_settings,
)
import src.api.router as router_mod
from src.api.models import Base, get_engine
from src.api.router import CapabilityRouter
from src.server import LCPHandler


def _seed_router(db_path="data/costs.db", enabled=False, cost_bias=0.15):
    """Seed the module-level dynamic router (legacy init_router)."""
    r = CapabilityRouter(enabled=enabled, db_path=db_path, cost_bias=cost_bias)
    if enabled:
        r.load_matrix()
    router_mod._dynamic_router = r
    return r


def _seed_cost_cache(engine):
    """Seed the module-level cost-cache singleton (legacy init_cost_cache)."""
    cc._cost_cache = CostPluginCache(engine)
    return cc._cost_cache


def _seed_refresher(cache, settings=None):
    """Seed the module-level refresher singleton (legacy init_refresher)."""
    cc._refresher = CacheRefresher(cache, settings)
    return cc._refresher


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
    return json.loads(handler.wfile.write.call_args[0][0].decode("utf-8"))


@pytest.fixture
def engine(tmp_path):
    db_path = str(tmp_path / "test.db")
    e = get_engine(db_path)
    Base.metadata.create_all(e)
    return e


@pytest.fixture(autouse=True)
def _cleanup_singletons():
    yield
    _reset_singletons()


class TestSettingsApi:
    def test_usage_page_has_cache_section(self, mock_config):
        # The settings cache now lives on the Usage page (moved from the
        # Providers page's Cache tab).
        from src.ui.pages import render_usage_page
        html = render_usage_page(mock_config)
        assert "Cost data refresh" in html
        assert 'id="cacheRows"' in html
        assert 'id="ttlRows"' in html
        assert "/api/settings" in html

    def test_providers_page_no_cache_tab(self, mock_config):
        # The Cache tab has been removed from the Providers page.
        from src.ui.pages import render_providers_page
        html = render_providers_page(mock_config)
        assert 'data-tab="cache"' not in html
        assert 'id="cacheRows"' not in html

    def test_settings_page_route_restored(self, engine, mock_config):
        # CUR-329: /settings exists again — the estate-level switches page
        # (conversation titling first). The old assertion here pinned its
        # removal; that decision is reversed, and the page must render rather
        # than 404.
        h = TestHandler(path="/settings", engine=engine)
        h.config = mock_config
        h.do_GET()
        assert _status(h) == 200
        body = h.wfile.write.call_args[0][0].decode("utf-8")
        assert "/api/settings/conversations" in body

    def test_settings_api_default(self, engine):
        init_settings(engine)
        _seed_cost_cache(engine)
        h = TestHandler(path="/api/settings", engine=engine)
        h.do_GET()
        body = _json_body(h)
        assert body["ttl_minutes"] == 30
        assert body["per_provider_ttl"] == {}
        assert body["entries"] == []

    def test_settings_update_per_provider(self, engine, mock_config):
        init_settings(engine)
        h = TestHandler(path="/api/settings", method="POST", engine=engine,
                        body=json.dumps({"provider": "deepseek", "ttl_minutes": 5}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["provider"] == "deepseek"
        assert body["ttl_minutes"] == 5
        assert body["per_provider_ttl"] == {"deepseek": 5}
        # Other providers still use the default.
        assert get_settings().get_ttl_minutes(provider="deepseek") == 5
        assert get_settings().get_ttl_minutes(provider="opencode") == 30

    def test_settings_update_reset_provider(self, engine, mock_config):
        init_settings(engine)
        get_settings().set_ttl_minutes(9, provider="deepseek")
        h = TestHandler(path="/api/settings", method="POST", engine=engine,
                        body=json.dumps({"provider": "deepseek"}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["provider"] == "deepseek"
        assert body["ttl_minutes"] == 30  # back to default
        assert body["per_provider_ttl"] == {}
        assert get_settings().get_ttl_minutes(provider="deepseek") == 30

    def test_settings_update_persists(self, engine, mock_config):
        init_settings(engine)
        h = TestHandler(path="/api/settings", method="POST", engine=engine,
                        body=json.dumps({"ttl_minutes": 7}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        assert _json_body(h)["ttl_minutes"] == 7
        # Persisted + readable via a fresh store.
        assert get_settings().get_ttl_minutes() == 7

    def test_settings_update_rejects_bad_ttl(self, engine, mock_config):
        init_settings(engine)
        h = TestHandler(path="/api/settings", method="POST", engine=engine,
                        body=json.dumps({"ttl_minutes": "nope"}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 400

    def test_settings_refresh_and_clear(self, engine, mock_config):
        init_settings(engine)
        _seed_cost_cache(engine)
        _seed_refresher(get_cost_cache(), get_settings())
        h = TestHandler(path="/api/settings/cache/refresh", method="POST", engine=engine, body="{}")
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        assert _json_body(h)["refreshing"] is True

        get_cost_cache().set("opencode", "subscription", {"a": 1})
        h = TestHandler(path="/api/settings/cache/clear", method="POST", engine=engine, body="{}")
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        assert get_cost_cache().entries() == []

    def test_routing_status_api(self, engine, mock_config):
        init_settings(engine)
        _seed_router(enabled=True)
        try:
            h = TestHandler(path="/api/routing/status", engine=engine)
            h.config = mock_config
            h.do_GET()
            body = _json_body(h)
            assert body["enabled"] is True
            assert body["policy"] == "eager"
            assert "per_task" in body
            assert "recent_decisions" in body
        finally:
            _seed_router(enabled=False)

    def test_routing_policy_update(self, engine, mock_config):
        init_settings(engine)
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"policy": "cost_first", "min_score": 0.4}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["policy"] == "cost_first"
        assert body["min_score"] == 0.4
        # Persisted via settings store.
        assert get_settings().get_routing_policy() == "cost_first"
        assert get_settings().get_routing_min_score() == 0.4

    def test_routing_policy_rejects_invalid(self, engine, mock_config):
        init_settings(engine)
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"policy": "bogus"}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 400

    def test_routing_enabled_update(self, engine, mock_config):
        init_settings(engine)
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"enabled": True}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["enabled"] is True
        assert get_settings().get_routing_enabled() is True

    def test_routing_enabled_disable(self, engine, mock_config):
        init_settings(engine)
        get_settings().set_routing_enabled(True)
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"enabled": False}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        assert get_settings().get_routing_enabled() is False

    def test_routing_enabled_syncs_db_config_section(self, engine, mock_config):
        """The global toggle also updates gateway_config:dynamic_routing."""
        init_settings(engine)
        # Seed a section like boot does.
        get_settings().set_config_section("dynamic_routing", {"enabled": False, "cost_bias": 0.15})
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"enabled": True}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        section = get_settings().get_config_section("dynamic_routing")
        assert section["enabled"] is True
        assert section["cost_bias"] == 0.15  # other keys preserved

    def test_routing_enabled_sync_ignores_per_profile(self, engine, mock_config):
        """A per-profile toggle must NOT clobber the global config section."""
        init_settings(engine)
        get_settings().set_config_section("dynamic_routing", {"enabled": False, "cost_bias": 0.15})
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"enabled": True, "profile": "l2"}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        section = get_settings().get_config_section("dynamic_routing")
        assert section["enabled"] is False  # global unchanged

    def test_routing_policy_disable_all_syncs(self, engine, mock_config):
        """Disable-all (global off + clear every profile) persists in the DB section."""
        init_settings(engine)
        get_settings().set_config_section("dynamic_routing", {"enabled": True, "cost_bias": 0.15})
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"enabled": False}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        section = get_settings().get_config_section("dynamic_routing")
        assert section["enabled"] is False

    def test_routing_enabled_rejects_non_bool(self, engine, mock_config):
        init_settings(engine)
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"enabled": "yes"}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 400

    def test_routing_rules_save(self, engine, mock_config):
        init_settings(engine)
        rules = [{"task": "debugging", "action": "prefer",
                  "provider": "deepseek", "model": "deepseek-v4-pro"}]
        h = TestHandler(path="/api/routing/rules", method="POST", engine=engine,
                        body=json.dumps({"rules": rules}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["rules"] == rules
        assert get_settings().get_routing_rules() == rules

    def test_routing_rules_rejects_bad_action(self, engine, mock_config):
        init_settings(engine)
        h = TestHandler(path="/api/routing/rules", method="POST", engine=engine,
                        body=json.dumps({"rules": [{"task": "x", "action": "bogus"}]}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 400

    def test_routing_rules_rejects_empty_prefer(self, engine, mock_config):
        init_settings(engine)
        # prefer/block need provider and/or model.
        h = TestHandler(path="/api/routing/rules", method="POST", engine=engine,
                        body=json.dumps({"rules": [{"task": "x", "action": "prefer"}]}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 400

    # ── Per-profile routing overrides ─────────────────────────────────────

    def test_routing_policy_per_profile(self, engine, mock_config):
        init_settings(engine)
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"policy": "explore", "profile": "l2"}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        assert get_settings().get_routing_policy(profile="l2") == "explore"
        assert get_settings().get_routing_policy() == "eager"  # global untouched

    def test_routing_enabled_per_profile(self, engine, mock_config):
        init_settings(engine)
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"enabled": False, "profile": "career"}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        assert get_settings().get_routing_enabled(profile="career") is False
        assert get_settings().get_routing_enabled() is None  # global untouched

    def test_routing_rules_per_profile(self, engine, mock_config):
        init_settings(engine)
        rules = [{"task": "planning", "action": "prefer", "model": "deepseek-v4-pro"}]
        h = TestHandler(path="/api/routing/rules", method="POST", engine=engine,
                        body=json.dumps({"rules": rules, "profile": "coder"}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        assert get_settings().get_routing_rules(profile="coder") == rules
        assert get_settings().get_routing_rules() == []  # global untouched

    def test_routing_status_per_profile(self, engine, mock_config):
        init_settings(engine)
        get_settings().set_routing_enabled(True, profile="l2")
        _seed_router(enabled=True)
        try:
            h = TestHandler(path="/api/routing/status?profile=l2", engine=engine)
            h.config = mock_config
            h.do_GET()
            body = _json_body(h)
            assert body["profile"] == "l2"
            assert body["enabled"] is True
            assert "rules" in body
        finally:
            _seed_router(enabled=False)

    def test_routing_status_has_per_profile_map(self, engine, mock_config):
        init_settings(engine)
        _seed_router(enabled=True)
        try:
            h = TestHandler(path="/api/routing/status", engine=engine)
            h.config = mock_config
            h.do_GET()
            body = _json_body(h)
            assert "per_profile" in body
        finally:
            _seed_router(enabled=False)

    def test_routing_clear_profile_override(self, engine, mock_config):
        init_settings(engine)
        get_settings().set_routing_enabled(True, profile="l2")
        get_settings().set_routing_policy("explore", profile="l2")
        h = TestHandler(path="/api/routing/policy", method="POST", engine=engine,
                        body=json.dumps({"profile": "l2", "clear_profile": True}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        assert get_settings().get_routing_enabled(profile="l2") is None
        assert get_settings().get_routing_policy(profile="l2") == "eager"

    def test_provider_create_requests_refresh(self, engine, mock_config, monkeypatch):
        init_settings(engine)
        cache = _seed_cost_cache(engine)
        refresher = _seed_refresher(cache, get_settings())
        # The refresher must receive a refresh request for the provider.
        requested = []
        monkeypatch.setattr(refresher, "request_refresh",
                            lambda provider=None, kind=None: requested.append(provider))
        h = TestHandler(path="/api/providers", method="POST", engine=engine,
                        body=json.dumps({"name": "newco", "api_base": "https://x", "models": []}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        assert "newco" in requested

    def test_provider_update_requests_refresh(self, engine, mock_config, monkeypatch):
        init_settings(engine)
        cache = _seed_cost_cache(engine)
        refresher = _seed_refresher(cache, get_settings())
        requested = []
        monkeypatch.setattr(refresher, "request_refresh",
                            lambda provider=None, kind=None: requested.append(provider))
        # newco must already exist in config.raw
        mock_config.raw.setdefault("providers", {})["newco"] = {"api_base": "https://x", "models": []}
        h = TestHandler(path="/api/providers/newco", method="PUT", engine=engine,
                        body=json.dumps({"api_base": "https://y"}))
        h.config = mock_config
        h.do_PUT()
        assert _status(h) == 200
        assert "newco" in requested


class TestPluginEndpointsCacheRead:
    def test_subscriptions_served_from_cache_without_fetch(self, engine, monkeypatch):
        init_settings(engine)
        cache = _seed_cost_cache(engine)
        cache.set("opencode", "subscription", {"monthly_pct": 42.0})

        from src.api.cost_plugins import get_registry
        # If the endpoint tried to scrape, this would blow up.
        monkeypatch.setattr(
            get_registry(), "fetch_all_subscriptions",
            lambda: (_ for _ in ()).throw(AssertionError("live fetch called!")),
        )

        h = TestHandler(path="/api/cost-plugins/subscriptions", engine=engine)
        h.do_GET()
        body = _json_body(h)
        assert body["plugin_subscriptions"]["opencode"] == {"monthly_pct": 42.0, "fetched_at": cache.get("opencode", "subscription")["fetched_at"]}

    def test_subscriptions_stale_flag(self, engine):
        init_settings(engine)
        cache = _seed_cost_cache(engine)
        cache.set("opencode", "subscription", {"monthly_pct": 42.0}, stale_error="boom")

        h = TestHandler(path="/api/cost-plugins/subscriptions", engine=engine)
        h.do_GET()
        body = _json_body(h)
        sub = body["plugin_subscriptions"]["opencode"]
        assert sub["monthly_pct"] == 42.0
        assert sub["_stale"] is True
        assert sub["_stale_error"] == "boom"

    def test_subscriptions_unsupported_provider_is_none(self, engine):
        init_settings(engine)
        _seed_cost_cache(engine)
        h = TestHandler(path="/api/cost-plugins/subscriptions", engine=engine)
        h.do_GET()
        body = _json_body(h)
        # llamacpp does not support subscriptions → None (matches old shape).
        assert body["plugin_subscriptions"]["llamacpp"] is None

    def test_balances_served_from_cache(self, engine):
        init_settings(engine)
        cache = _seed_cost_cache(engine)
        cache.set("deepseek", "balance", {"balance": 11.5, "currency": "USD"})
        h = TestHandler(path="/api/cost-plugins/balances", engine=engine)
        h.do_GET()
        body = _json_body(h)
        assert body["plugin_balances"]["deepseek"]["balance"] == 11.5

    def test_cookie_set_invalidates_cache_and_requests_refresh(self, engine, monkeypatch, mock_config):
        init_settings(engine)
        cache = _seed_cost_cache(engine)
        _seed_refresher(cache, get_settings())
        cache.set("opencode", "subscription", {"monthly_pct": 42.0})

        # Stub the credential store so the handler has one to write to.
        from src.api import credential_store as cred_mod
        fake_store = MagicMock()
        fake_store.has_cookie.return_value = True
        monkeypatch.setattr(cred_mod, "get_credential_store", lambda *a, **k: fake_store)

        h = TestHandler(path="/api/cost-plugins/cookie/opencode", method="POST",
                        engine=engine, body=json.dumps({"cookie": "auth=xyz"}))
        h.config = mock_config
        h.do_POST()
        assert _status(h) == 200
        # Cache for opencode was invalidated; the refresh was requested.
        assert cache.get("opencode", "subscription") is None
