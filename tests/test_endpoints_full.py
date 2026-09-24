"""Comprehensive endpoint tests for src/server/endpoints.py.

Covers the routes/blocks that were previously uncovered: circuit-breaker
reset, provider health/failures/failovers, provider CRUD edge cases, provider
test (Cloudflare + HTTP errors), discover (error + commandcode enrichment),
profile budget get/update, key detail/rotate/delete edge cases, alerts list
filters, budget update/delete/status, plugin usage/balances/summary/subscriptions,
and the metrics/export endpoints.
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.server import LCPHandler
from src.api.models import (
    get_engine,
    Base,
    Request as RequestModel,
    Budget,
    FailoverEvent,
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


import os
import tempfile


def _seed_requests(engine):
    now = datetime.now(timezone.utc).isoformat()
    with get_session(engine) as s:
        s.add_all([
            RequestModel(
                timestamp=now, profile="l2", model="deepseek-v4-pro", provider="deepseek",
                prompt_tokens=1000, completion_tokens=500, cache_hit_tokens=400,
                cache_miss_tokens=600, cost=0.5, latency_ms=100, success=1,
            ),
            RequestModel(
                timestamp=now, profile="l2", model="deepseek-v4-pro", provider="deepseek",
                prompt_tokens=100, completion_tokens=50, cache_hit_tokens=0,
                cache_miss_tokens=100, cost=0.01, latency_ms=10, success=0,
                error_type="timeout",
            ),
        ])
        s.commit()


# ── Health / metrics / export ───────────────────────────────────────────────

class TestHealthMetrics:
    def test_health(self, temp_db):
        h = TestHandler(path="/health", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["status"] == "ok"
        assert "profiles" in body

    def test_circuit_breaker_reset_missing_fields(self, temp_db):
        h = TestHandler(path="/api/circuit-breaker/reset", method="POST", engine=temp_db, body="{}")
        h.do_POST()
        assert _status(h) == 400

    def test_circuit_breaker_reset_ok(self, temp_db):
        body = json.dumps({"provider": "deepseek", "base_url": "https://t/v1", "profile": "l2"})
        h = TestHandler(path="/api/circuit-breaker/reset", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 200
        assert _json_body(h)["ok"] is True

    def test_provider_health_api(self, temp_db):
        _seed_requests(temp_db)
        h = TestHandler(path="/api/providers/health", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert "summary" in body and "providers" in body

    def test_provider_failures_api(self, temp_db):
        _seed_requests(temp_db)
        h = TestHandler(path="/api/providers/deepseek/failures?window=24h", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["provider"] == "deepseek"
        assert body["buckets"]["timeout"] == 1

    def test_provider_failures_api_bad_window(self, temp_db):
        _seed_requests(temp_db)
        h = TestHandler(path="/api/providers/deepseek/failures?window=999", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        assert _json_body(h)["window"] == "999"

    def test_failovers_empty(self, temp_db):
        h = TestHandler(path="/api/providers/failovers", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        assert _json_body(h)["failovers"] == []

    def test_failovers_with_filters(self, temp_db):
        with get_session(temp_db) as s:
            s.add(FailoverEvent(profile="l2", from_provider="deepseek", to_provider="opencode", reason="timeout"))
            s.commit()
        h = TestHandler(path="/api/providers/failovers?profile=l2&from=deepseek&to=opencode&limit=5", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert len(body["failovers"]) == 1
        assert body["failovers"][0]["reason"] == "timeout"

    def test_provider_toggle_bad_action(self, temp_db):
        body = json.dumps({"profile": "l2", "action": "banana"})
        h = TestHandler(path="/api/providers/deepseek/toggle", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 400

    def test_provider_toggle_missing_fields(self, temp_db):
        h = TestHandler(path="/api/providers/deepseek/toggle", method="POST", engine=temp_db, body="{}")
        h.do_POST()
        assert _status(h) == 400

    def test_provider_toggle_not_in_profile(self, temp_db):
        body = json.dumps({"profile": "l2", "action": "degrade"})
        h = TestHandler(path="/api/providers/nonexistent/toggle", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 404

    def test_provider_toggle_ok(self, temp_db):
        body = json.dumps({"profile": "l2", "action": "degrade"})
        h = TestHandler(path="/api/providers/deepseek/toggle", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 200

    def test_metrics(self, temp_db):
        _seed_requests(temp_db)
        h = TestHandler(path="/metrics", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        # Prometheus text output written to wfile.
        written = b"".join(c[0][0] if isinstance(c[0][0], bytes) else str(c[0][0]).encode() for c in h.wfile.write.call_args_list)
        assert b"lcp_requests_total" in written

    def test_export_csv(self, temp_db):
        _seed_requests(temp_db)
        h = TestHandler(path="/export", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        written = b"".join(c[0][0] if isinstance(c[0][0], bytes) else str(c[0][0]).encode() for c in h.wfile.write.call_args_list)
        assert b"timestamp,profile,model" in written

    def test_errors(self, temp_db):
        _seed_requests(temp_db)
        h = TestHandler(path="/errors", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert len(body["errors"]) == 1  # only the failed request


# ── Provider CRUD edge cases ────────────────────────────────────────────────

class TestProviderCrudEdges:
    def test_create_missing_name(self, temp_db):
        h = TestHandler(path="/api/providers", method="POST", engine=temp_db, body="{}")
        h.do_POST()
        assert _status(h) == 400

    def test_create_api_key_store_none(self, temp_db):
        body = json.dumps({"name": "x", "api_base": "https://x/v1", "api_key": "k"})
        with patch("src.server.endpoints.get_credential_store", return_value=None):
            h = TestHandler(path="/api/providers", method="POST", engine=temp_db, body=body)
            h.do_POST()
        assert _status(h) == 500

    def test_update_missing_provider(self, temp_db):
        h = TestHandler(path="/api/providers/nope", method="PUT", engine=temp_db,
                        body=json.dumps({"api_base": "https://x/v1"}))
        h.do_PUT()
        assert _status(h) == 404

    def test_update_store_none(self, temp_db):
        LCPHandler.config.providers["deepseek"] = {"api_base": "https://t/v1", "models": []}
        LCPHandler.config.raw["providers"]["deepseek"] = {"api_base": "https://t/v1", "models": []}
        body = json.dumps({"api_key": "new-key"})
        with patch("src.server.endpoints.get_credential_store", return_value=None):
            h = TestHandler(path="/api/providers/deepseek", method="PUT", engine=temp_db, body=body)
            h.do_PUT()
        assert _status(h) == 500

    def test_delete_missing_provider(self, temp_db):
        h = TestHandler(path="/api/providers/nope", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 404

    def test_delete_ok(self, temp_db):
        LCPHandler.config.providers["delco"] = {"api_base": "https://d/v1", "models": []}
        LCPHandler.config.raw["providers"]["delco"] = {"api_base": "https://d/v1", "models": []}
        h = TestHandler(path="/api/providers/delco", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 200

    def test_delete_provider_with_url_encoded_spaces(self, temp_db):
        """Regression: a provider named with spaces arrives percent-encoded in
        the URL (local%20llm%20zgx). The handler must decode it before the
        config lookup, else the delete 404s even though the provider exists."""
        name = "local llm zgx"
        cfg = LCPHandler.config
        # Mirror the real Config: .providers and .raw["providers"] share state.
        cfg.providers[name] = {"api_base": "https://d/v1", "models": []}
        cfg.raw["providers"] = cfg.providers
        h = TestHandler(path="/api/providers/local%20llm%20zgx", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 200
        assert name not in cfg.providers
        assert name not in cfg.raw["providers"]

    def test_rename_provider(self, temp_db):
        """POST /api/providers/{name}/rename moves the config entry, re-points
        every profile chain step and moves stored credentials."""
        old, new = "oldco", "newco"
        cfg = LCPHandler.config
        cfg.providers[old] = {"api_base": "https://o/v1", "models": ["m1"]}
        cfg.raw["providers"] = cfg.providers
        cfg.raw["profiles"]["l2"]["chain"] = [
            {"provider": old, "model": "m1", "base_url": "https://o/v1"},
            {"provider": "deepseek", "model": "deepseek-v4-pro", "base_url": "https://t/v1"},
        ]

        store = MagicMock()
        store.has.return_value = True
        store.get.return_value = "sk-old"
        store.has_cookie.return_value = False
        store.has_workspace_id.return_value = False
        with patch("src.server.endpoints._credential_store_for", return_value=store):
            h = TestHandler(path=f"/api/providers/{old}/rename", method="POST", engine=temp_db,
                            body=json.dumps({"new_name": new}))
            h.do_POST()

        assert _status(h) == 200
        assert old not in cfg.providers
        assert new in cfg.providers
        assert cfg.providers[new]["api_base"] == "https://o/v1"
        # Chain step re-pointed to the new provider name.
        assert cfg.raw["profiles"]["l2"]["chain"][0]["provider"] == new
        # Credential moved to the new name.
        store.set.assert_any_call(new, "sk-old")
        cfg.save.assert_called()

    def test_rename_missing_new_name(self, temp_db):
        h = TestHandler(path="/api/providers/deepseek/rename", method="POST", engine=temp_db,
                        body=json.dumps({}))
        h.do_POST()
        assert _status(h) == 400

    def test_rename_missing_provider(self, temp_db):
        h = TestHandler(path="/api/providers/nope/rename", method="POST", engine=temp_db,
                        body=json.dumps({"new_name": "x"}))
        h.do_POST()
        assert _status(h) == 404

    def test_rename_conflict_existing_target(self, temp_db):
        h = TestHandler(path="/api/providers/deepseek/rename", method="POST", engine=temp_db,
                        body=json.dumps({"new_name": "deepseek"}))
        h.do_POST()
        assert _status(h) == 200  # no-op rename → ok, renamed False
        body = _json_body(h)
        assert body["renamed"] is False

    def test_rename_to_existing_other_provider_conflicts(self, temp_db):
        cfg = LCPHandler.config
        cfg.providers["other"] = {"api_base": "https://o/v1", "models": []}
        cfg.raw["providers"] = dict(cfg.providers)
        h = TestHandler(path="/api/providers/deepseek/rename", method="POST", engine=temp_db,
                        body=json.dumps({"new_name": "other"}))
        h.do_POST()
        assert _status(h) == 409

    def test_provider_test_cloudflare_block(self, temp_db):
        import urllib.error
        err = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        err.read = MagicMock(return_value=b"error code: 1010")
        body = json.dumps({"api_base": "https://api.commandcode.ai/provider/v1", "api_key": "k", "model": "m"})
        h = TestHandler(path="/api/providers/test", method="POST", engine=temp_db, body=body)
        with patch("urllib.request.urlopen", side_effect=err):
            h.do_POST()
        result = _json_body(h)
        assert result["ok"] is False
        assert "Cloudflare" in result["error"]

    def test_provider_test_invalid_body(self, temp_db):
        h = TestHandler(path="/api/providers/test", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(side_effect=json.JSONDecodeError("bad", "doc", 0))
        h.do_POST()
        assert _status(h) == 400

    def test_chain_reorder_preserves_url(self, temp_db):
        LCPHandler.config.raw["profiles"]["l2"]["chain"] = [
            {"provider": "deepseek", "model": "deepseek-v4-pro", "base_url": "https://old/v1"}
        ]
        body = json.dumps({"chain": [{"provider": "deepseek", "model": "deepseek-v4-pro"}]})
        h = TestHandler(path="/api/chains/l2", method="PUT", engine=temp_db, body=body)
        h.do_PUT()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["chain"][0]["base_url"] == "https://old/v1"

    def test_chain_reorder_missing_profile(self, temp_db):
        h = TestHandler(path="/api/chains/nope", method="PUT", engine=temp_db, body="{}")
        h.do_PUT()
        assert _status(h) == 404

    def test_chain_reorder_missing_chain(self, temp_db):
        h = TestHandler(path="/api/chains/l2", method="PUT", engine=temp_db, body="{}")
        h.do_PUT()
        assert _status(h) == 400


# ── Profile + budget endpoints ──────────────────────────────────────────────

class TestProfileBudget:
    def test_profile_budget_get_none(self, temp_db):
        h = TestHandler(path="/api/profiles/l2/budget", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        assert _json_body(h)["budget"] is None

    def test_profile_budget_create(self, temp_db):
        body = json.dumps({"name": "L2 Cap", "amount": 100.0, "action": "block"})
        h = TestHandler(path="/api/profiles/l2/budget", method="PUT", engine=temp_db, body=body)
        h.do_PUT()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["ok"] is True
        assert "created" in body

    def test_profile_budget_update_existing(self, temp_db):
        with get_session(temp_db) as s:
            b = Budget(name="L2", key_id=None, profile="l2", amount=10.0, period="monthly",
                       threshold_pct="80", action="log", status="active")
            s.add(b)
            s.commit()
            bid = b.id
        body = json.dumps({"amount": 200.0})
        h = TestHandler(path="/api/profiles/l2/budget", method="PUT", engine=temp_db, body=body)
        h.do_PUT()
        assert _status(h) == 200
        assert _json_body(h)["updated"] == bid

    def test_profile_budget_update_invalid_body(self, temp_db):
        h = TestHandler(path="/api/profiles/l2/budget", method="PUT", engine=temp_db, body=b"{")
        h.rfile.read = MagicMock(side_effect=json.JSONDecodeError("bad", "doc", 0))
        h.headers["Content-Length"] = "5"
        h.do_PUT()
        assert _status(h) == 400

    def test_budgets_list_and_status(self, temp_db):
        with get_session(temp_db) as s:
            s.add(Budget(name="Cap", key_id=None, profile="l2", amount=100.0, period="monthly",
                         threshold_pct="50,80", action="block", status="active"))
            s.commit()
        h = TestHandler(path="/api/budgets", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        assert len(_json_body(h)["budgets"]) == 1

        h = TestHandler(path="/api/budgets/status", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert len(body["budgets"]) == 1
        assert body["budgets"][0]["thresholds"] == [50, 80]

    def test_budget_update_and_delete(self, temp_db):
        with get_session(temp_db) as s:
            b = Budget(name="Cap", key_id=None, profile="l2", amount=100.0, period="monthly",
                       threshold_pct="80", action="log", status="active")
            s.add(b)
            s.commit()
            bid = b.id
        h = TestHandler(path=f"/api/budgets/{bid}", method="PUT", engine=temp_db,
                        body=json.dumps({"amount": 500.0, "status": "paused"}))
        h.do_PUT()
        assert _status(h) == 200

        h = TestHandler(path=f"/api/budgets/{bid}", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 200
        assert _json_body(h)["deleted"] == bid

    def test_budget_update_invalid_id(self, temp_db):
        h = TestHandler(path="/api/budgets/abc", method="PUT", engine=temp_db, body="{}")
        h.do_PUT()
        assert _status(h) == 400

    def test_budget_delete_missing(self, temp_db):
        h = TestHandler(path="/api/budgets/999999", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 404


# ── Key endpoint edge cases ─────────────────────────────────────────────────

class TestKeyEdges:
    def test_rotate_invalid_id(self, temp_db):
        h = TestHandler(path="/api/keys/abc/rotate", method="POST", engine=temp_db)
        h.do_POST()
        assert _status(h) == 400

    def test_rotate_missing(self, temp_db):
        h = TestHandler(path="/api/keys/999999/rotate", method="POST", engine=temp_db)
        h.do_POST()
        assert _status(h) == 404

    def test_delete_invalid_id(self, temp_db):
        h = TestHandler(path="/api/keys/abc", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 400

    def test_delete_missing(self, temp_db):
        h = TestHandler(path="/api/keys/999999", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 404

    def test_key_detail_invalid_id(self, temp_db):
        h = TestHandler(path="/api/keys/abc", engine=temp_db)
        h.do_GET()
        assert _status(h) == 400


# ── Alerts endpoints ────────────────────────────────────────────────────────

class TestAlertsExtra:
    def test_alerts_list_with_params(self, temp_db):
        am = MagicMock()
        am.list_alerts.return_value = [{"id": 1}]
        with patch("src.server.endpoints.get_alert_manager", return_value=am):
            h = TestHandler(path="/api/alerts?limit=5&status=active", engine=temp_db)
            # Query-string routes aren't matched by the exact-path router; call directly.
            h._serve_alerts_list()
        am.list_alerts.assert_called_with(limit=5, status="active")
        assert _json_body(h) == {"alerts": [{"id": 1}]}

    def test_alerts_active(self, temp_db):
        am = MagicMock()
        am.get_active_alerts.return_value = []
        with patch("src.server.endpoints.get_alert_manager", return_value=am):
            h = TestHandler(path="/api/alerts/active", engine=temp_db)
            h.do_GET()
        assert _status(h) == 200

    def test_alerts_config_update_invalid_body(self, temp_db):
        am = MagicMock()
        with patch("src.server.endpoints.get_alert_manager", return_value=am):
            h = TestHandler(path="/api/alerts/config", method="PUT", engine=temp_db, body=b"{\"")
            h.rfile.read = MagicMock(side_effect=json.JSONDecodeError("bad", "doc", 0))
            h.headers["Content-Length"] = "5"
            h.do_PUT()
        assert _status(h) == 400


# ── Plugin endpoints ─────────────────────────────────────────────────────────

class TestPluginEndpoints:
    def test_plugin_usage(self, temp_db):
        reg = MagicMock()
        reg.fetch_all_usage.return_value = {"deepseek": []}
        with patch("src.server.endpoints.get_registry", return_value=reg):
            h = TestHandler(path="/api/cost-plugins/usage?start=2026-08-01&end=2026-08-08", engine=temp_db)
            # Query-string route: call the endpoint directly.
            h._serve_plugin_usage()
        assert _json_body(h) == {"plugin_usage": {"deepseek": []}}

    def test_plugin_balances_summary_subscriptions(self, temp_db):
        reg = MagicMock()
        reg.fetch_all_balances.return_value = {}
        reg.fetch_all_summaries.return_value = {}
        reg.fetch_all_subscriptions.return_value = {}
        with patch("src.server.endpoints.get_registry", return_value=reg):
            for path in ("/api/cost-plugins/balances", "/api/cost-plugins/summary", "/api/cost-plugins/subscriptions"):
                h = TestHandler(path=path, engine=temp_db)
                h.do_GET()
                assert _status(h) == 200

    def test_plugin_cookie_get_has(self, temp_db):
        store = MagicMock()
        store.has_cookie.return_value = True
        with patch("src.server.endpoints.get_credential_store", return_value=store):
            h = TestHandler(path="/api/cost-plugins/cookie/opencode", engine=temp_db)
            h.do_GET()
        assert _json_body(h)["has_cookie"] is True

    def test_plugin_workspace_id_get(self, temp_db):
        store = MagicMock()
        store.has_workspace_id.return_value = True
        with patch("src.server.endpoints.get_credential_store", return_value=store):
            h = TestHandler(path="/api/cost-plugins/workspace-id/opencode", engine=temp_db)
            h.do_GET()
        assert _json_body(h)["has_workspace_id"] is True

    def test_plugin_workspace_id_set_store_none(self, temp_db):
        body = json.dumps({"workspace_id": "wrk_1"})
        with patch("src.server.endpoints.get_credential_store", return_value=None):
            h = TestHandler(path="/api/cost-plugins/workspace-id/opencode", method="POST", engine=temp_db, body=body)
            h.do_POST()
        assert _status(h) == 500


# ── Discover endpoint edge cases ────────────────────────────────────────────

class TestDiscoverEdges:
    @patch("urllib.request.urlopen")
    def test_discover_http_error(self, mock_urlopen, temp_db):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError("url", 500, "err", {}, None)
        body = json.dumps({"api_base": "https://x/v1", "api_key": "k"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        result = _json_body(h)
        assert result["ok"] is False
        assert "error" in result

    @patch("urllib.request.urlopen")
    def test_discover_flat_list(self, mock_urlopen, temp_db):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(["model-a", "model-b"]).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        body = json.dumps({"api_base": "https://x/v1"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        result = _json_body(h)
        assert result["ok"] is True
        assert result["models"] == [{"id": "model-a"}, {"id": "model-b"}]

    @patch("urllib.request.urlopen")
    def test_discover_llamacpp_meta(self, mock_urlopen, temp_db, monkeypatch):
        # llama.cpp on loopback is blocked by the SSRF guard by default; the
        # operator escape hatch (LCP_SSRF_ALLOWLIST) restores local discovery.
        monkeypatch.setenv("LCP_SSRF_ALLOWLIST", "127.0.0.0/8")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": [{
                "id": "m1",
                "meta": {"n_ctx": 8192, "n_ctx_train": 16384, "n_params": 8000000000, "ftype": "Q4_K_M", "size": 4000000000},
            }]
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        body = json.dumps({"api_base": "http://localhost:8080/v1", "provider": "llamacpp"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        result = _json_body(h)
        m = result["models"][0]
        assert m["context_length"] == 8192
        assert m["parameters"] == "8.0B"
        assert m["quantization"] == "Q4_K_M"

    def test_discover_missing_api_base(self, temp_db):
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body="{}")
        h.do_POST()
        assert _status(h) == 400


# ── Setup endpoints ─────────────────────────────────────────────────────────

class TestSetupEndpointsExtra:
    def test_setup_api(self, temp_db):
        h = TestHandler(path="/api/setup", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert "manifest" in body and "state" in body

    def test_setup_install_unknown(self, temp_db):
        h = TestHandler(path="/api/setup/install/bogus/nope", method="POST", engine=temp_db, body="{}")
        h.do_POST()
        assert _status(h) == 404

    def test_setup_remove_unknown(self, temp_db):
        h = TestHandler(path="/api/setup/remove/bogus/nope", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 404

    def test_setup_remove_retired_workspace_module(self, temp_db):
        """M2b retired the workspace module, so its uninstall target is gone too.

        The work surfaces (Tasks, Fleet) are always available, so there is nothing
        left for an install/uninstall pair to gate — and a route that still answered
        would imply otherwise.
        """
        h = TestHandler(path="/api/setup/remove/module/workspace", method="DELETE", engine=temp_db)
        h.do_DELETE()
        assert _status(h) == 404

    def test_setup_install_provider(self, temp_db):
        from unittest.mock import patch as _patch
        store = MagicMock()
        store.has.return_value = False
        body = json.dumps({"api_base": "https://api.deepseek.com/v1", "models": ["deepseek-v4-pro"], "api_key": "sk-x"})
        with _patch("src.api.setup._provider_preset", return_value={"api_base": "https://api.deepseek.com/v1", "models": ["deepseek-v4-pro"]}):
            with _patch("src.api.credential_store.get_credential_store", return_value=store):
                h = TestHandler(path="/api/setup/install/provider/deepseek", method="POST", engine=temp_db, body=body)
                h.do_POST()
        assert _status(h) == 200
        assert _json_body(h)["installed"] is True

    def test_setup_install_provider_error(self, temp_db):
        body = json.dumps({"api_base": "https://x/v1"})
        h = TestHandler(path="/api/setup/install/provider/openai", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 400

    def test_setup_skip(self, temp_db):
        h = TestHandler(path="/api/setup/skip", method="POST", engine=temp_db, body="{}")
        h.do_POST()
        assert _status(h) == 200
        assert _json_body(h)["ok"] is True

    def test_setup_progress(self, temp_db):
        h = TestHandler(path="/api/setup/progress", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        assert _json_body(h)["progress"]["status"] == "idle"
