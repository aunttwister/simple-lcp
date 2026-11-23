"""Tests for usage/stats/logs/plugin/provider-test endpoints.

Covers the big uncovered blocks in src/server/endpoints.py:
  - _serve_usage_stats_api (daily/by_model/by_profile/date-range)
  - _serve_usage_totals_api
  - _serve_daily_costs_api / _serve_recent_requests_api
  - _serve_logs_api (filters, pagination, sort)
  - _serve_usage_page / _serve_logs_page / _serve_alerts_page
  - _serve_provider_test (mocked urllib)
  - _serve_plugin_usage / balances / summary / subscriptions
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.server import LCPHandler
from src.server.endpoints import (
    _derive_engine_metrics,
    _ENGINE_COUNTER_NAMES,
    _ENGINE_METRIC_FALLBACKS,
)
from src.api.models import get_engine, Base, Request as RequestModel, get_session


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
    """Ensure LCPHandler.config is set for all tests (mirrors test_server.py)."""
    from unittest.mock import MagicMock
    from src.api.key_manager import KeyManager
    import src.api.key_manager as key_manager_mod

    key_manager_mod._key_manager = KeyManager(temp_db, "data")

    cfg = MagicMock()
    cfg.server = {"port": 8734, "default_profile": "l2"}
    cfg.profiles = {
        "l2": {"forbidden_tools": [], "chain": [{"provider": "opencode", "model": "deepseek-v4-pro", "base_url": "https://t/v1"}]},
        "l1": {"forbidden_tools": [], "chain": [{"provider": "deepseek", "model": "deepseek-v4-flash", "base_url": "https://t/v1"}]},
    }
    cfg.providers = {
        "opencode": {"api_key_env": "OK", "api_base": "https://t/v1", "models": ["deepseek-v4-pro"]},
        "deepseek": {"api_key_env": "DK", "api_base": "https://t/v1", "models": ["deepseek-v4-flash"]},
    }
    cfg.pricing = [
        {"provider": "opencode", "model": "deepseek-v4-pro", "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0},
    ]
    cfg.circuit_breaker = {"failures_dead": 5, "dead_cooldown_seconds": 300, "failures_degraded": 3, "degraded_cooldown_seconds": 60}
    cfg.database = {"path": "/tmp/test.db", "wal_mode": True}
    cfg.model_limits = {}
    cfg.get_profile = lambda name: cfg.profiles.get(name)
    cfg.get_pricing = lambda provider, model: next((p for p in cfg.pricing if p["provider"] == provider), cfg.pricing[0])
    cfg.get_provider_key = lambda name: "test-key"
    cfg.check_reload = MagicMock()
    cfg.raw = {}
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


def _seed(engine):
    now = datetime.now(timezone.utc).isoformat()
    with get_session(engine) as s:
        s.add_all([
            RequestModel(
                timestamp=now, profile="l2", model="deepseek-v4-pro", provider="opencode",
                prompt_tokens=1000, completion_tokens=500, cache_hit_tokens=400,
                cache_miss_tokens=600, cost=0.5, latency_ms=100, success=1,
            ),
            RequestModel(
                timestamp=now, profile="l1", model="deepseek-v4-flash", provider="deepseek",
                prompt_tokens=200, completion_tokens=50, cache_hit_tokens=0,
                cache_miss_tokens=200, cost=0.02, latency_ms=50, success=1,
            ),
            RequestModel(
                timestamp=now, profile="l2", model="deepseek-v4-pro", provider="opencode",
                prompt_tokens=100, completion_tokens=50, cache_hit_tokens=0,
                cache_miss_tokens=100, cost=0.01, latency_ms=10, success=0, error_type="timeout",
                error_detail="Provider opencode timed out after 10s",
            ),
        ])
        s.commit()


# ── Usage stats API ───────────────────────────────────────────────────────

class TestUsageStatsApi:
    def test_empty(self, temp_db):
        h = TestHandler(path="/api/usage/stats", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        body = _json_body(h)
        assert body["totals"]["cost"] == 0
        assert body["totals"]["requests"] == 0
        assert body["totals"]["tokens"] == 0
        # The date-range fill generates 30 zero-days even when empty
        assert len(body["daily"]) == 30
        assert all(d["cost"] == 0 for d in body["daily"])
        assert all(d["tokens"] == 0 for d in body["daily"])

    def test_with_seeded_data(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/usage/stats", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["totals"]["requests"] == 2  # success only
        assert body["totals"]["cost"] > 0
        assert "deepseek-v4-pro" in body["by_model"]  # keyed by model name
        assert "l2" in body["by_profile"]
        assert body["cache"]["miss_tokens"] > 0

    def test_provider_filter(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/usage/stats?provider=opencode", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["provider"] == "opencode"
        assert body["totals"]["requests"] == 1

    def test_date_range(self, temp_db):
        _seed(temp_db)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        h = TestHandler(path=f"/api/usage/stats?start={today}&end={today}", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["totals"]["requests"] == 2
        assert len(body["daily"]) >= 1

    def test_days_limit(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/usage/stats?days=7", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["totals"]["requests"] == 2

    def test_error_returns_500(self, temp_db):
        h = TestHandler(path="/api/usage/stats", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            h.do_GET()
        assert _status(h) == 500


# ── Usage totals API ──────────────────────────────────────────────────────

class TestUsageTotalsApi:
    def test_empty(self, temp_db):
        h = TestHandler(path="/api/usage/totals", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["requests"] == 0
        assert body["tokens"] == 0

    def test_with_seeded_data(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/usage/totals", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["requests"] == 2
        assert body["tokens"] > 0

    def test_date_range_filter(self, temp_db):
        _seed(temp_db)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        h = TestHandler(path=f"/api/usage/totals?start={today}&end={today}", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["start"] == today
        assert body["requests"] == 2


# ── Daily costs API ───────────────────────────────────────────────────────

class TestDailyCostsApi:
    def test_with_seeded_data(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/daily-costs", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert len(body["daily_costs"]) >= 1
        assert body["daily_costs"][0]["cost"] > 0

    def test_error_returns_500(self, temp_db):
        h = TestHandler(path="/api/daily-costs", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            h.do_GET()
        assert _status(h) == 500


# ── Recent requests API ───────────────────────────────────────────────────

class TestRecentRequestsApi:
    def test_with_seeded_data(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/recent-requests", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert len(body["requests"]) == 3
        r = body["requests"][0]
        assert r["success"] in (True, False)
        assert "saved" in r
        assert r["latency_ms"] >= 0

    def test_error_returns_500(self, temp_db):
        h = TestHandler(path="/api/recent-requests", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            h.do_GET()
        assert _status(h) == 500


# ── Logs API ──────────────────────────────────────────────────────────────

class TestLogsApi:
    def test_all_rows(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/logs", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["total"] == 3
        assert len(body["rows"]) == 3
        assert len(body["profiles"]) == 2
        assert len(body["providers"]) == 2

    def test_filter_by_profile(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/logs?profile=l2", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["total"] == 2
        assert all(r["profile"] == "l2" for r in body["rows"])

    def test_filter_by_status_error(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/logs?status=error", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["total"] == 1
        assert body["rows"][0]["success"] is False
        # Error reason must be surfaced to the Logs page.
        assert body["rows"][0]["error_detail"] == "Provider opencode timed out after 10s"

    def test_filter_by_status_success(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/logs?status=success", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["total"] == 2

    def test_pagination_and_sort(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/logs?limit=1&offset=0&sort=asc", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["total"] == 3
        assert len(body["rows"]) == 1

    def test_provider_filter(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/api/logs?provider=deepseek", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["total"] == 1
        assert body["rows"][0]["provider"] == "deepseek"

    def test_error_returns_500(self, temp_db):
        h = TestHandler(path="/api/logs", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            h.do_GET()
        assert _status(h) == 500


# ── Page routes ───────────────────────────────────────────────────────────

class TestPageRoutes:
    def test_usage_page_serves_its_own_page(self, temp_db):
        """M2b: Usage left Activity and is a page again — 200, not a redirect."""
        _seed(temp_db)
        h = TestHandler(path="/usage", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200

    def test_logs_page_redirects_to_activity(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/logs", engine=temp_db)
        h.do_GET()
        assert _status(h) == 302

    def test_activity_page_serves(self, temp_db):
        _seed(temp_db)
        # CUR-330: a bare /activity renders the Activity page now; only the
        # explicit legacy overview URL redirects to the homepage (`/`), which
        # itself serves 200.
        h = TestHandler(path="/activity", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        h = TestHandler(path="/activity?tab=overview", engine=temp_db)
        h.do_GET()
        assert _status(h) == 302
        loc = [call[0][1] for call in h.send_header.call_args_list
               if call[0] and call[0][0] == "Location"]
        assert loc == ["/"]
        home = TestHandler(path="/", engine=temp_db)
        home.do_GET()
        assert _status(home) == 200
        logs = TestHandler(path="/activity?tab=logs", engine=temp_db)
        logs.do_GET()
        assert _status(logs) == 200

    def test_alerts_page(self, temp_db):
        h = TestHandler(path="/alerts", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200


# ── Provider test endpoint ─────────────────────────────────────────────────

class TestProviderTest:
    def _body_handler(self, temp_db, body):
        return TestHandler(path="/api/providers/test", method="POST", engine=temp_db,
                           body=body)

    def test_success(self, temp_db):
        body = {"api_base": "https://api.example.com/v1", "api_key": "sk-test", "model": "gpt-3.5-turbo"}
        h = self._body_handler(temp_db, body)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"model": "gpt-3.5-turbo", "id": "chatcmpl-1"}'
        mock_resp.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_resp):
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is True
        assert result["model"] == "gpt-3.5-turbo"

    def test_http_error(self, temp_db):
        import urllib.error
        body = {"api_base": "https://api.example.com/v1", "api_key": "sk-test", "model": "gpt-3.5-turbo"}
        h = self._body_handler(temp_db, body)
        err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        err.read = MagicMock(return_value=b'{"error":"bad key"}')
        with patch("urllib.request.urlopen", side_effect=err):
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is False
        assert result["status"] == 401

    def test_generic_error(self, temp_db):
        body = {"api_base": "https://api.example.com/v1", "api_key": "sk-test", "model": "gpt-3.5-turbo"}
        h = self._body_handler(temp_db, body)
        with patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
            h.do_POST()
        assert _status(h) == 200
        assert _json_body(h)["ok"] is False

    def test_missing_api_base(self, temp_db):
        h = self._body_handler(temp_db, {"api_base": "", "api_key": ""})
        h.do_POST()
        assert _status(h) == 400

    def test_works_without_api_key(self, temp_db, monkeypatch):
        """llama.cpp (local) needs no API key — test connection must proceed without one.
        Loopback here is explicitly allowlisted (the SSRF guard's default blocks it)."""
        monkeypatch.setenv("LCP_SSRF_ALLOWLIST", "127.0.0.0/8")
        body = {"api_base": "http://localhost:8080/v1", "api_key": "", "provider": "llamacpp", "model": "m"}
        h = self._body_handler(temp_db, body)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"model": "m"}'
        mock_resp.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is True
        req = mock_open.call_args[0][0]
        hdrs = {k.lower(): v for k, v in req.headers.items()}
        assert "authorization" not in hdrs

    def test_resolves_key_from_credential_store(self, temp_db):
        """Provider test resolves key from credential store when not in body."""
        from unittest.mock import patch as _patch
        store = MagicMock()
        store.get.return_value = "cred-key"
        body = {"api_base": "https://api.example.com/v1", "api_key": "", "provider": "opencode", "model": "m"}
        h = self._body_handler(temp_db, body)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"model": "m"}'
        mock_resp.__enter__.return_value = mock_resp
        with _patch("src.server.endpoints.get_credential_store", return_value=store):
            with _patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
                h.do_POST()
        req = mock_open.call_args[0][0]
        assert req.headers["Authorization"] == "Bearer cred-key"

    def test_sends_browser_headers(self, temp_db):
        """Provider test sends browser-like headers to avoid Cloudflare 1010."""
        body = {"api_base": "https://api.commandcode.ai/v1", "api_key": "sk-test", "model": "m", "provider": "commandcode"}
        h = self._body_handler(temp_db, body)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"model": "m"}'
        mock_resp.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            h.do_POST()
        req = mock_open.call_args[0][0]
        # urllib normalises header casing (User-Agent -> User-agent); compare case-insensitively
        hdrs = {k.lower(): v for k, v in req.headers.items()}
        ua = hdrs.get("user-agent", "")
        assert "Chrome" in ua
        assert "Python-urllib" not in ua
        assert hdrs.get("origin") == "https://api.commandcode.ai"
        assert hdrs.get("referer") == "https://api.commandcode.ai/"
        assert hdrs.get("authorization") == "Bearer sk-test"

    def test_cloudflare_block_1010(self, temp_db):
        """Cloudflare 1010 error body is detected and surfaced with a clear message."""
        import urllib.error
        body = {"api_base": "https://api.commandcode.ai/v1", "api_key": "sk-test", "model": "gpt-3.5-turbo", "provider": "commandcode"}
        h = self._body_handler(temp_db, body)
        cloudflare_body = (
            b'<html><head><title>commandcode.ai</title></head><body>'
            b'<span data-translate="error">1010</span>'
            b'<span>Ray ID: abc123def456</span>'
            b'</body></html>'
        )
        err = urllib.error.HTTPError("url", 403, "Forbidden", {"cf-ray": "abc123def456"}, None)
        err.read = MagicMock(return_value=cloudflare_body)
        with patch("urllib.request.urlopen", side_effect=err):
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is False
        assert result["status"] == 403
        assert "Cloudflare" in result["error"]
        assert "1010" in result["error"]
        assert "TLS" in result["error"]
        assert result["cf_ray"] == "abc123def456"

    def test_cloudflare_block_no_ray(self, temp_db):
        """Cloudflare block without a ray ID still detected."""
        import urllib.error
        body = {"api_base": "https://api.commandcode.ai/v1", "api_key": "sk-test", "model": "m", "provider": "commandcode"}
        h = self._body_handler(temp_db, body)
        err = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        err.read = MagicMock(return_value=b'Error 1010: cloudflare block')
        with patch("urllib.request.urlopen", side_effect=err):
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is False
        assert result["status"] == 403
        assert "Cloudflare" in result["error"]
        assert "1010" in result["error"]
        assert "cf_ray" not in result

    def test_cloudflare_block_minimal_body(self, temp_db):
        """Minimal 'error code: 1010' body (observed from api.commandcode.ai) detected."""
        import urllib.error
        body = {"api_base": "https://api.commandcode.ai/v1", "api_key": "sk-test", "model": "deepseek-v4-pro", "provider": "commandcode"}
        h = self._body_handler(temp_db, body)
        err = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        err.read = MagicMock(return_value=b'error code: 1010\n')
        with patch("urllib.request.urlopen", side_effect=err):
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is False
        assert result["status"] == 403
        assert "Cloudflare" in result["error"]
        assert "1010" in result["error"]
        assert "TLS" in result["error"]

    def test_non_cloudflare_http_error(self, temp_db):
        """Non-Cloudflare HTTP errors still return raw error body."""
        import urllib.error
        body = {"api_base": "https://api.example.com/v1", "api_key": "sk-test", "model": "gpt-3.5-turbo"}
        h = self._body_handler(temp_db, body)
        err = urllib.error.HTTPError("url", 500, "Server Error", {}, None)
        err.read = MagicMock(return_value=b'{"error": "internal explosion"}')
        with patch("urllib.request.urlopen", side_effect=err):
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is False
        assert result["status"] == 500
        assert "internal explosion" in result["error"]
        assert "Cloudflare" not in result["error"]

    def test_commandcode_model_not_in_plan(self, temp_db):
        """Command Code MODEL_NOT_IN_PLAN error surfaces with a plan hint."""
        import urllib.error
        body = {"api_base": "https://api.commandcode.ai/provider/v1", "api_key": "sk-test", "model": "gpt-5.6-sol", "provider": "commandcode"}
        h = self._body_handler(temp_db, body)
        err = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        err.read = MagicMock(return_value=b'{"error":{"message":"MODEL_NOT_IN_PLAN: GPT-5.6 Sol available in Pro and above plans or extra on demand usage","type":"permission_error","code":"FORBIDDEN"}}')
        with patch("urllib.request.urlopen", side_effect=err):
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is False
        assert result["status"] == 403
        assert "current plan" in result["error"].lower()
        assert result.get("code") in ("MODEL_NOT_IN_PLAN", "FORBIDDEN")
        assert result.get("type") == "permission_error"

    def test_commandcode_unsupported_model_anthropic(self, temp_db):
        """Command Code unsupported_model (Anthropic shape) surfaces a clear hint."""
        import urllib.error
        body = {"api_base": "https://api.commandcode.ai/provider/v1", "api_key": "sk-test", "model": "claude-sonnet-4-6", "provider": "commandcode"}
        h = self._body_handler(temp_db, body)
        err = urllib.error.HTTPError("url", 400, "Bad Request", {}, None)
        err.read = MagicMock(return_value=b'{"error":{"message":"Model \\"claude-sonnet-4-6\\" must be called via /provider/v1/messages (Anthropic Messages shape).","type":"invalid_request_error","param":"model","code":"unsupported_model"}}')
        with patch("urllib.request.urlopen", side_effect=err):
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is False
        assert result["status"] == 400
        assert "Anthropic" in result["error"]
        assert "/provider/v1/messages" in result["error"]
        assert result.get("code") == "unsupported_model"

    def test_commandcode_auth_error(self, temp_db):
        """Command Code 401 surfaces an authentication message."""
        import urllib.error
        body = {"api_base": "https://api.commandcode.ai/provider/v1", "api_key": "bad-key", "model": "deepseek-v4-flash", "provider": "commandcode"}
        h = self._body_handler(temp_db, body)
        err = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        err.read = MagicMock(return_value=b'{"error":{"message":"Invalid API key","type":"authentication_error","code":"authentication_error"}}')
        with patch("urllib.request.urlopen", side_effect=err):
            h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is False
        assert result["status"] == 401
        assert "API key" in result["error"]
        assert result.get("code") == "authentication_error"


# ── Plugin endpoints ──────────────────────────────────────────────────────

class TestPluginEndpoints:
    def test_plugin_usage(self, temp_db):
        # Note: route matches exact path only; start/end parsed from query on the
        # matched path. Call the endpoint method directly to cover the query handling.
        h = TestHandler(path="/api/cost-plugins/usage?start=2026-01-01&end=2026-01-31", engine=temp_db)
        with patch("src.server.endpoints.get_registry") as mock_reg:
            mock_reg.return_value.fetch_all_usage.return_value = {"opencode": []}
            h._serve_plugin_usage()
        assert _status(h) == 200
        assert _json_body(h) == {"plugin_usage": {"opencode": []}}
        # The query params should be forwarded to the plugin
        _, kwargs = mock_reg.return_value.fetch_all_usage.call_args
        assert kwargs["start_date"] == "2026-01-01"
        assert kwargs["end_date"] == "2026-01-31"

    def test_plugin_balances(self, temp_db):
        h = TestHandler(path="/api/cost-plugins/balances", engine=temp_db)
        with patch("src.server.endpoints.get_registry") as mock_reg:
            mock_reg.return_value.fetch_all_balances.return_value = {}
            h.do_GET()
        assert _status(h) == 200
        assert _json_body(h) == {"plugin_balances": {}}

    def test_plugin_summary(self, temp_db):
        h = TestHandler(path="/api/cost-plugins/summary", engine=temp_db)
        with patch("src.server.endpoints.get_registry") as mock_reg:
            mock_reg.return_value.fetch_all_summaries.return_value = {}
            h.do_GET()
        assert _status(h) == 200
        assert _json_body(h) == {"plugin_summaries": {}}

    def test_plugin_subscriptions(self, temp_db):
        h = TestHandler(path="/api/cost-plugins/subscriptions", engine=temp_db)
        with patch("src.server.endpoints.get_registry") as mock_reg:
            mock_reg.return_value.fetch_all_subscriptions.return_value = {}
            h.do_GET()
        assert _status(h) == 200
        assert _json_body(h) == {"plugin_subscriptions": {}}


# ── Errors / metrics / export ──────────────────────────────────────────────

class TestErrorsMetricsExport:
    def test_errors_with_data(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/errors", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert len(body["errors"]) >= 1
        assert body["errors"][0]["error_type"] == "timeout"

    def test_errors_empty(self, temp_db):
        h = TestHandler(path="/errors", engine=temp_db)
        h.do_GET()
        assert _json_body(h) == {"errors": []}

    def test_errors_db_failure(self, temp_db):
        h = TestHandler(path="/errors", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            h.do_GET()
        assert _status(h) == 500

    def test_metrics_with_data(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/metrics", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        combined = b"".join(c[0][0] for c in h.wfile.write.call_args_list if isinstance(c[0][0], bytes))
        assert b"lcp_requests_total" in combined
        assert b"lcp_cost_total" in combined

    def test_export_with_data(self, temp_db):
        _seed(temp_db)
        h = TestHandler(path="/export", engine=temp_db)
        h.do_GET()
        assert _status(h) == 200
        combined = b"".join(c[0][0] for c in h.wfile.write.call_args_list if isinstance(c[0][0], bytes))
        assert b"timestamp,profile,model,provider" in combined
        assert b"deepseek-v4-pro" in combined

    def test_export_db_failure(self, temp_db):
        h = TestHandler(path="/export", engine=temp_db)
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            h.do_GET()
        assert _status(h) == 500


# ── Provider discover: rich metadata details ───────────────────────────────

class TestProviderDiscoverDetails:
    @patch("urllib.request.urlopen")
    def test_rich_metadata_details(self, mock_urlopen, temp_db):
        """Discover parses meta n_params (B/M), n_ctx, ftype, size, details, pricing."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "data": [
                # meta.n_params (no details) -> _fmt_params B branch
                {
                    "id": "big-model",
                    "meta": {
                        "n_ctx": 131072,
                        "n_ctx_train": 200000,
                        "n_params": 7_300_000_000,  # -> 7.3B via _fmt_params
                        "ftype": "Q4_K_M",
                        "size": 4_000_000_000,
                    },
                    "pricing": {"prompt": 0.5, "completion": 1.5},
                },
                # details.parameter_size overwrites -> M branch + details fields
                {
                    "id": "mid-model",
                    "meta": {"n_params": 3_500_000},  # -> 3.5M via _fmt_params
                    "details": {
                        "parameter_size": "3.5B",
                        "quantization_level": "Q4_0",
                    },
                },
                "simple-model-id",
            ]
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        body = json.dumps({"api_base": "https://test.api/v1", "api_key": "sk"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["ok"] is True
        m0 = result["models"][0]
        assert m0["context_length"] == 131072
        assert m0["context_train"] == 200000
        assert m0["parameters"] == "7.3B"  # _fmt_params B branch
        assert m0["quantization"] == "Q4_K_M"
        assert m0["size_bytes"] == 4_000_000_000
        assert m0["pricing"] == {"prompt": 0.5, "completion": 1.5}
        # details overwrites meta-derived parameters; quantization from details
        m1 = result["models"][1]
        assert m1["parameters"] == "3.5B"
        assert m1["quantization"] == "Q4_0"
        # String-only model entry
        assert result["models"][2]["id"] == "simple-model-id"

    @patch("urllib.request.urlopen")
    def test_list_response_format(self, mock_urlopen, temp_db):
        """Discover handles a top-level list response."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([
            {"id": "m-a"},
            {"id": "m-b"},
        ]).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        body = json.dumps({"api_base": "https://test.api/v1", "api_key": "sk"})
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        result = _json_body(h)
        assert result["count"] == 2
        assert result["models"][0]["id"] == "m-a"

    @patch("urllib.request.urlopen")
    def test_http_error_then_success_fallback(self, mock_urlopen, temp_db):
        """First URL fails with HTTPError, second (/v1/models) succeeds."""
        import urllib.error
        err = urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": [{"id": "m1"}]}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [err, mock_resp]

        body = json.dumps({"api_base": "https://test.api", "api_key": "sk"})  # no /v1
        h = TestHandler(path="/api/providers/discover", method="POST", engine=temp_db, body=body)
        h.do_POST()
        result = _json_body(h)
        assert result["ok"] is True
        assert result["models"][0]["id"] == "m1"

    @patch("urllib.request.urlopen")
    def test_commandcode_plan_enrichment(self, mock_urlopen, temp_db):
        """Discover for commandcode includes plan info from the billing API."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": [{"id": "deepseek-v4-flash"}]}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        plugin = MagicMock()
        plugin.discover_models.return_value = None  # fall through to generic HTTP
        plugin.fetch_subscription.return_value = {
            "plan_id": "goat", "plan_status": "active",
        }
        with patch("src.server.endpoints.get_registry") as mock_reg:
            mock_reg.return_value.for_provider.return_value = plugin
            body = json.dumps({"api_base": "https://api.commandcode.ai/provider/v1",
                               "provider": "commandcode", "api_key": "sk"})
            h = TestHandler(path="/api/providers/discover", method="POST",
                            engine=temp_db, body=body)
            h.do_POST()

        result = _json_body(h)
        assert result["ok"] is True
        assert result["plan_id"] == "goat"
        assert result["plan_status"] == "active"
        assert result["models"][0]["id"] == "deepseek-v4-flash"


# ── Chain reorder branches ─────────────────────────────────────────────────

class TestChainReorder:
    def _chain_body(self, temp_db, body, path="/api/chains/l2"):
        return TestHandler(path=path, method="PUT", engine=temp_db, body=body)

    def test_invalid_json(self, temp_db):
        h = TestHandler(path="/api/chains/l2", method="PUT", engine=temp_db)
        h.rfile.read = MagicMock(side_effect=Exception("bad json"))
        h.headers = {"Content-Length": "10"}
        h.do_PUT()
        assert _status(h) == 400

    def test_profile_not_found(self, temp_db):
        body = json.dumps({"chain": []})
        h = self._chain_body(temp_db, body, path="/api/chains/ghost")
        h.do_PUT()
        assert _status(h) == 404

    def test_missing_chain_list(self, temp_db):
        LCPHandler.config.raw = {"profiles": {"l2": {"chain": []}}}
        h = self._chain_body(temp_db, json.dumps({"chain": "notalist"}))
        h.do_PUT()
        assert _status(h) == 400

    def test_reorder_preserves_base_url(self, temp_db):
        LCPHandler.config.raw = {
            "profiles": {
                "l2": {
                    "chain": [
                        {"provider": "opencode", "model": "deepseek-v4-pro", "base_url": "https://old/v1"},
                    ],
                },
            },
        }
        saved = {}
        LCPHandler.config.save = MagicMock(side_effect=lambda: saved.update({"chain": LCPHandler.config.raw["profiles"]["l2"]["chain"]}))
        body = json.dumps({"chain": [
            {"provider": "deepseek", "model": "deepseek-v4-flash", "base_url": "https://new/v1"},
            {"provider": "opencode", "model": "deepseek-v4-pro"},
        ]})
        h = self._chain_body(temp_db, body)
        h.do_PUT()
        assert _status(h) == 200
        result = _json_body(h)
        assert result["chain"][0]["base_url"] == "https://new/v1"
        # New entry for opencode inherits the preserved base_url from old chain
        assert result["chain"][1]["base_url"] == "https://old/v1"


# ── Engine-metric derivation: two sources (health, then /metrics) ──────────

class TestEngineMetricFallbacks:
    """TensorFold 1.3.0 answers /health with {"status": "ok"} alone and publishes
    every counter on /metrics (tensorfold:*), which left the Usage panel's engine
    cards as five em-dashes on a healthy box. Both sources are read now, and a
    figure absent from BOTH must stay None — the panel renders that as an em-dash,
    and 0.0 would claim a measurement nobody made.
    """

    METRICS = {
        "tensorfold:prompt_tokens_total": 9_064_391.0,
        "tensorfold:prompt_tokens_cached_total": 7_712_768.0,
        "tensorfold:generation_tokens_total": 183_077.0,
        "tensorfold:request_prefill_seconds_sum": 1_306.268816,
        "tensorfold:request_decode_seconds_sum": 3_893.65238,
        "tensorfold:mtp_drafted_total": 191_016.0,
        "tensorfold:mtp_accepted_total": 127_022.0,
        "tensorfold:kv_pool_capacity_tokens": 2_240_000.0,
        "tensorfold:kv_pool_available_tokens": 2_238_976.0,
        "tensorfold:prompt_cache_bytes": 5_368_709_120.0,
        "tensorfold:prompt_cache_budget_bytes": 5_368_709_120.0,
        "tensorfold:requests_started_total": 1_434.0,
        "tensorfold:requests_running": 2.0,
        "tensorfold:requests_waiting": 0.0,
        "tensorfold:lanes": 8.0,
    }

    def test_health_only_is_unchanged(self):
        """An engine that still puts the counters in /health reads as before."""
        health = {
            "status": "ok",
            "prompt_tokens_total": 1000, "completion_tokens_total": 500,
            "prefill_seconds_total": 2, "decode_seconds_total": 10,
            "drafted_total": 100, "accepted_total": 60,
            "pool_tokens": 1000, "pool_free_tokens": 750,
            "kept_bytes": 2048, "kept_bytes_cap": 4096,
            "requests_total": 7, "streams": {"max": 8},
        }
        d = _derive_engine_metrics(health, {})
        assert d["ok"] is True
        assert d["decode_tokens_per_s"] == 50.0    # 500 / 10
        assert d["prefill_tokens_per_s"] == 500.0  # 1000 / 2
        assert d["mtp_accept_pct"] == 60.0
        assert d["pool_used_pct"] == 25.0
        assert d["kept_cap_used_pct"] == 50.0
        assert d["requests_total"] == 7.0
        assert d["lanes"] == 8

    def test_metrics_only_fills_every_card(self):
        """/health carries nothing but status; the five dashes get real numbers."""
        d = _derive_engine_metrics({"status": "ok"}, dict(self.METRICS))
        assert d["ok"] is True
        assert d["decode_tokens_per_s"] == pytest.approx(183_077 / 3_893.65238, abs=0.05)
        assert d["prefill_tokens_per_s"] == pytest.approx(9_064_391 / 1_306.268816, abs=0.05)
        assert d["mtp_accept_pct"] == pytest.approx(127_022 / 191_016 * 100, abs=0.05)
        assert d["pool_used_pct"] == pytest.approx(1_024 / 2_240_000 * 100, abs=0.05)
        assert d["kept_cap_used_pct"] == 100.0
        assert d["requests_total"] == 1_434.0
        assert d["requests_running"] == 2.0
        assert d["requests_waiting"] == 0.0
        assert d["lanes"] == 8.0
        # the three token counters the panel also prints
        assert d["prompt_tokens_total"] == 9_064_391.0
        assert d["completion_tokens_total"] == 183_077.0
        assert d["cached_tokens_total"] == 7_712_768.0
        assert d["prompt_cache_hit_pct"] == pytest.approx(7_712_768 / 9_064_391 * 100, abs=0.05)

    def test_health_wins_where_both_have_the_key(self):
        d = _derive_engine_metrics(
            {"status": "ok", "prompt_tokens_total": 111}, dict(self.METRICS))
        assert d["prompt_tokens_total"] == 111.0

    def test_absent_from_both_is_none_never_zero(self):
        d = _derive_engine_metrics({"status": "ok"}, {})
        for key in ("decode_tokens_per_s", "prefill_tokens_per_s", "mtp_accept_pct",
                    "pool_used_pct", "kept_cap_used_pct", "requests_total", "lanes",
                    "requests_running", "requests_waiting", "kv_cache_usage_pct"):
            assert d[key] is None, key

    def test_the_fallback_map_is_what_supplies_the_numbers(self):
        """Control: with the map empty (exactly the pre-fix behaviour) every card
        goes back to a dash. Without this, the suite would still pass if the map
        were dropped — the tests would then only be proving the constants exist.
        """
        with patch.dict("src.server.endpoints._ENGINE_METRIC_FALLBACKS", {}, clear=True):
            d = _derive_engine_metrics({"status": "ok"}, dict(self.METRICS))
        assert d["decode_tokens_per_s"] is None
        assert d["prefill_tokens_per_s"] is None
        assert d["mtp_accept_pct"] is None
        assert d["pool_used_pct"] is None
        assert d["kept_cap_used_pct"] is None
        assert d["requests_total"] is None

    def test_requested_counter_names_cover_every_fallback(self):
        """The endpoint asks for exactly the names the map can read — a name the
        map knows but the request omits would read as absent on a live engine."""
        assert set(_ENGINE_COUNTER_NAMES) >= {
            f"tensorfold:{name}" for name in _ENGINE_METRIC_FALLBACKS.values()}
        for extra in ("tensorfold:requests_waiting", "tensorfold:kv_cache_usage_ratio",
                      "tensorfold:requests_running", "tensorfold:lanes"):
            assert extra in _ENGINE_COUNTER_NAMES


# ── The cards and the chart above them must describe the SAME period ───────

_FROZEN_NOW = datetime(2026, 11, 23, 11, 0, 0)  # naive UTC, mid-day on purpose


class _FrozenDateTime(datetime):
    """Pin the handler's clock so a boundary row's side is deterministic."""

    @classmethod
    def utcnow(cls):
        return _FROZEN_NOW


class TestUsageWindowAlignment:
    """`totals`/`by_model`/`by_profile` are scoped by the same first CALENDAR day
    as the `daily` series. Before this, they used a rolling utcnow-days window
    while the chart filled calendar days, so at days=7 the cards counted rows the
    chart could not show (8.8M tokens live; 94.5M at 30d).
    """

    # Frozen clock 2026-11-23T11:00Z: the rolling window would start 11-16T11:00,
    # the calendar window starts 11-17T00:00 — the gap between them is the bug.
    GAP_ROW = "2026-11-16T20:00:00+00:00"     # rolling-only: must be excluded
    INSIDE_7D = "2026-11-17T00:30:00+00:00"   # in both: counted
    TODAY = "2026-11-23T06:00:00+00:00"       # in both: counted

    def _seed(self, engine):
        with get_session(engine) as s:
            s.add_all([
                RequestModel(timestamp=self.GAP_ROW, profile="l1", model="glm-5.3-flash-exl3",
                             provider="local-sparks", prompt_tokens=10_000_000,
                             completion_tokens=0, cache_hit_tokens=0,
                             cache_miss_tokens=10_000_000, cost=0.0, latency_ms=1, success=1),
                RequestModel(timestamp=self.INSIDE_7D, profile="l1", model="glm-5.3-flash-exl3",
                             provider="local-sparks", prompt_tokens=1000, completion_tokens=500,
                             cache_hit_tokens=400, cache_miss_tokens=600, cost=0.0,
                             latency_ms=1, success=1),
                RequestModel(timestamp=self.TODAY, profile="l2", model="glm-5.3-flash-exl3",
                             provider="local-sparks", prompt_tokens=200, completion_tokens=100,
                             cache_hit_tokens=0, cache_miss_tokens=200, cost=0.0,
                             latency_ms=1, success=1),
            ])
            s.commit()

    def _get(self, temp_db, query):
        h = TestHandler(path=f"/api/usage/stats?{query}", engine=temp_db)
        with patch("src.server.endpoints.datetime", _FrozenDateTime):
            h.do_GET()
        assert _status(h) == 200
        return _json_body(h)

    def test_7_days_cards_equal_the_chart(self, temp_db):
        self._seed(temp_db)
        body = self._get(temp_db, "provider=local-sparks&days=7")
        assert len(body["daily"]) == 7
        assert body["totals"]["tokens"] == sum(d["tokens"] for d in body["daily"]) == 1800
        assert body["totals"]["requests"] == sum(d["requests"] for d in body["daily"]) == 2
        assert body["totals"]["cost"] == sum(d["cost"] for d in body["daily"])
        # Negative control for this test: the gap row IS inside the legacy rolling
        # window (utcnow-7d == 11-16T11:00Z), which is why the old filter counted
        # it while the chart could not show it.
        assert self.GAP_ROW >= (_FROZEN_NOW - timedelta(days=7)).isoformat()
        assert self.GAP_ROW < "2026-11-17"
        # the rolling-only row is in NEITHER, not just the chart
        assert body["totals"]["tokens"] != 10_001_800

    def test_model_and_profile_breakdowns_use_the_same_window(self, temp_db):
        self._seed(temp_db)
        body = self._get(temp_db, "provider=local-sparks&days=7")
        assert sum(m["prompt_tokens"] + m["completion_tokens"]
                   for m in body["by_model"].values()) == body["totals"]["tokens"]
        assert sum(p["prompt_tokens"] + p["completion_tokens"]
                   for p in body["by_profile"].values()) == body["totals"]["tokens"]

    def test_30_days_includes_the_gap_row_in_both(self, temp_db):
        self._seed(temp_db)
        body = self._get(temp_db, "provider=local-sparks&days=30")
        assert len(body["daily"]) == 30
        assert body["totals"]["tokens"] == sum(d["tokens"] for d in body["daily"]) == 10_001_800

    def test_explicit_range_still_wins(self, temp_db):
        """start/end is unchanged: it scopes both the chart and the totals."""
        self._seed(temp_db)
        body = self._get(temp_db, "provider=local-sparks&start=2026-11-23&end=2026-11-23")
        assert body["totals"]["tokens"] == sum(d["tokens"] for d in body["daily"]) == 300
