"""In-process tests for LCPHandler — covers do_GET, do_POST, _serve_*.

Uses a handle-safe subclass that skips the BaseHTTPRequestHandler auto-handle
so we can call do_GET/do_POST directly with pytest-cov measuring coverage.
"""
import json
import os
import tempfile
import pytest

from unittest.mock import patch, MagicMock

from src.server import LCPHandler
from src.api.models import get_engine, Base


class _TestHandler(LCPHandler):
    """Subclass that skips auto-handle on init so we control invocation."""

    def __init__(self, path="/", method="GET", engine=None):
        # Skip socketserver.BaseRequestHandler.__init__ completely
        self.path = path
        self.command = method
        self.headers = {}
        self.request_version = "HTTP/1.1"
        self.requestline = f"{method} {path} HTTP/1.1"
        self.raw_requestline = f"{method} {path} HTTP/1.1".encode()
        self.client_address = ("127.0.0.1", 0)

        # Mock response methods
        self.send_response = MagicMock()
        self.send_header = MagicMock()
        self.end_headers = MagicMock()
        self.wfile = MagicMock()
        self.wfile.write = MagicMock()
        self.rfile = MagicMock()
        self.rfile.read = MagicMock(return_value=b"{}")

        # Set up handlers for stream chunks
        self._write_chunk = MagicMock()

        # Set up internal refs that methods expect
        self.engine = engine

        # Log error helper
        self.log_error = MagicMock()


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


def _get_written_bytes(handler):
    """Extract all bytes written to wfile."""
    written = []
    for call in handler.wfile.write.call_args_list:
        arg = call[0][0]
        if isinstance(arg, bytes):
            written.append(arg)
        elif isinstance(arg, str):
            written.append(arg.encode())
        elif isinstance(arg, (bytearray, memoryview)):
            written.append(bytes(arg))
    return b"".join(written)


def _json_body(handler):
    """Parse the last JSON body written to wfile."""
    raw = _get_written_bytes(handler)
    return json.loads(raw.decode("utf-8"))


# ═══════════════════════════════════════════════════════════════════════
# do_GET tests
# ═══════════════════════════════════════════════════════════════════════

class TestDoGet:
    def test_root_serves_dashboard(self, temp_db):
        LCPHandler.config = MagicMock()
        LCPHandler.config.pricing = []
        LCPHandler.config.providers = {}
        LCPHandler.config.profiles = {"l2": {"chain": [], "forbidden_tools": []}, "l1": {"chain": [], "forbidden_tools": []}}
        LCPHandler.config.circuit_breaker = {
            "failures_dead": 5, "dead_cooldown_seconds": 300,
            "failures_degraded": 3, "degraded_cooldown_seconds": 60,
        }
        LCPHandler.config.get_pricing = MagicMock(return_value=None)
        LCPHandler.config.get_profile = MagicMock(return_value={"chain": [], "forbidden_tools": []})
        LCPHandler.engine = temp_db

        h = _TestHandler("/", engine=temp_db)
        h.do_GET()
        # M2: / is the legacy dashboard URL and now redirects to Activity
        assert h.send_response.called
        assert h.send_response.call_args[0][0] == 302
        loc = [c[0][1] for c in h.send_header.call_args_list
               if c[0] and c[0][0] == "Location"]
        assert loc == ["/activity"]

    def test_health(self, temp_db):
        h = _TestHandler("/health", engine=temp_db)
        h.do_GET()
        assert h.send_response.called
        combined = _get_written_bytes(h)
        assert b"providers" in combined
        # CWE-200 redaction: provider health must NOT expose base_url or
        # last_failure_reason (private provider endpoint map) on this
        # unauthenticated route; status counters remain.
        data = json.loads(combined)
        for key, info in data.get("providers", {}).items():
            assert "base_url" not in info, f"provider {key} leaks base_url"
            assert "last_failure_reason" not in info, f"provider {key} leaks last_failure_reason"
            assert "status" in info

    def test_models(self, temp_db):
        LCPHandler.config.profiles = {"l2": {"chain": []}, "l1": {"chain": []}}
        h = _TestHandler("/v1/models", engine=temp_db)
        h.do_GET()
        assert h.send_response.called
        combined = _get_written_bytes(h)
        assert b'"data"' in combined

    def test_cache_stats(self, temp_db):
        h = _TestHandler("/cache/stats", engine=temp_db)
        h.do_GET()
        assert h.send_response.called

    def test_metrics(self, temp_db):
        h = _TestHandler("/metrics", engine=temp_db)
        h.do_GET()
        assert h.send_response.called

    def test_export(self, temp_db):
        h = _TestHandler("/export", engine=temp_db)
        h.do_GET()
        assert h.send_response.called

    def test_export_with_limit(self, temp_db):
        h = _TestHandler("/export?limit=3", engine=temp_db)
        h.do_GET()
        assert h.send_response.called

    def test_errors(self, temp_db):
        h = _TestHandler("/errors", engine=temp_db)
        h.do_GET()
        assert h.send_response.called

    def test_404(self, temp_db):
        h = _TestHandler("/nonexistent", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 404


# ═══════════════════════════════════════════════════════════════════════
# do_POST tests
# ═══════════════════════════════════════════════════════════════════════

class TestDoPost:
    def test_non_chat_path(self, temp_db):
        h = _TestHandler("/health", method="POST", engine=temp_db)
        h.do_POST()
        assert h.send_response.called
        assert h.send_response.call_args[0][0] == 404

    def test_unknown_profile(self, temp_db):
        LCPHandler.config.get_profile = MagicMock(return_value=None)
        h = _TestHandler("/no-such/chat/completions", method="POST", engine=temp_db)
        h.do_POST()
        assert h.send_response.called

    def test_read_body_error(self, temp_db):
        h = _TestHandler("/l2/chat/completions", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(side_effect=Exception("IO error"))
        h.headers = {"Content-Length": "10"}
        h.do_POST()
        assert h.send_response.called

    def test_chat_flow(self, temp_db):
        """Test through do_POST with a minimal valid body."""
        body = {"messages": [{"role": "user", "content": "test"}]}
        body_bytes = json.dumps(body).encode()

        h = _TestHandler("/l2/chat/completions", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(return_value=body_bytes)
        h.headers = {"Content-Length": str(len(body_bytes))}

        LCPHandler.config.get_profile = MagicMock(return_value={
            "chain": [{"provider": "test", "model": "test-model", "base_url": "http://t"}],
            "forbidden_tools": [],
        })
        LCPHandler.config.providers = {"test": {"base_url": "http://t"}}
        LCPHandler.config.get_pricing = MagicMock(return_value=None)

        h.do_POST()
        assert h.send_response.called

    def test_circuit_breaker_reset(self, temp_db):
        """POST /api/circuit-breaker/reset should reset a provider to healthy."""
        body = {"provider": "test", "base_url": "https://t/v1", "profile": "l2"}
        body_bytes = json.dumps(body).encode()
        h = _TestHandler("/api/circuit-breaker/reset", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(return_value=body_bytes)
        h.headers = {"Content-Length": str(len(body_bytes))}
        h.do_POST()
        assert h.send_response.called
        combined = _get_written_bytes(h)
        data = json.loads(combined)
        assert data.get("ok") is True

    def test_circuit_breaker_reset_missing_fields(self, temp_db):
        """POST /api/circuit-breaker/reset with missing fields should 400."""
        body = {"provider": "test"}
        body_bytes = json.dumps(body).encode()
        h = _TestHandler("/api/circuit-breaker/reset", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(return_value=body_bytes)
        h.headers = {"Content-Length": str(len(body_bytes))}
        h.do_POST()
        assert h.send_response.called
        assert h.send_response.call_args[0][0] == 400


# ═══════════════════════════════════════════════════════════════════════
# Structured error response tests
# ═══════════════════════════════════════════════════════════════════════

class TestErrorResponses:
    """do_POST error paths return structured {code, message} payloads."""

    def _chat_handler(self, temp_db):
        body = {"messages": [{"role": "user", "content": "test"}]}
        body_bytes = json.dumps(body).encode()
        h = _TestHandler("/l2/chat/completions", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(return_value=body_bytes)
        h.headers = {"Content-Length": str(len(body_bytes))}
        LCPHandler.config.get_profile = MagicMock(return_value={
            "chain": [{"provider": "test", "model": "test-model", "base_url": "http://t"}],
            "forbidden_tools": [],
            "auth_required": False,
        })
        LCPHandler.config.providers = {"test": {"base_url": "http://t"}}
        LCPHandler.config.get_pricing = MagicMock(return_value=None)
        return h

    def test_provider_bad_request_returns_structured_400(self, temp_db):
        from src.api.exceptions import ProviderBadRequestError
        h = self._chat_handler(temp_db)
        with patch("src.server.handler.try_chain",
                   side_effect=ProviderBadRequestError("model 'x' not found")):
            h.do_POST()
        assert h.send_response.call_args[0][0] == 400
        err = _json_body(h)["error"]
        assert err["code"] == "LCP-2004"
        assert "model 'x' not found" in err["message"]

    def test_bad_request_message_redacts_api_keys(self, temp_db):
        from src.api.exceptions import ProviderBadRequestError
        h = self._chat_handler(temp_db)
        with patch("src.server.handler.try_chain",
                   side_effect=ProviderBadRequestError(
                       "HTTP 400: sk-abcdef1234567890 is invalid")):
            h.do_POST()
        err = _json_body(h)["error"]
        assert err["code"] == "LCP-2004"
        assert "sk-abcdef1234567890" not in err["message"]
        assert "[REDACTED]" in err["message"]

    def test_all_providers_failed_does_not_leak_internals(self, temp_db):
        from src.api.exceptions import AllProvidersFailedError
        h = self._chat_handler(temp_db)
        with patch("src.server.handler.try_chain",
                   side_effect=AllProvidersFailedError(
                       "All providers failed for l2: secretco: HTTP 500: sk-abc123secret")):
            h.do_POST()
        assert h.send_response.call_args[0][0] == 502
        err = _json_body(h)["error"]
        assert err["code"] == "LCP-3001"
        assert "secretco" not in err["message"]
        assert "sk-abc123secret" not in err["message"]
        assert "providers failed" in err["message"]

    def test_provider_timeout_returns_504(self, temp_db):
        from src.api.exceptions import ProviderTimeoutError
        h = self._chat_handler(temp_db)
        with patch("src.server.handler.try_chain",
                   side_effect=ProviderTimeoutError("upstream unreachable")):
            h.do_POST()
        assert h.send_response.call_args[0][0] == 504
        err = _json_body(h)["error"]
        assert err["code"] == "LCP-2001"

    def test_unhandled_exception_returns_500_structured(self, temp_db):
        h = self._chat_handler(temp_db)
        with patch("src.server.handler.try_chain", side_effect=RuntimeError("boom")):
            h.do_POST()
        assert h.send_response.call_args[0][0] == 500
        err = _json_body(h)["error"]
        assert err["code"] == "LCP-5001"
        assert "internal error" in err["message"]

    def test_missing_messages_returns_400(self, temp_db):
        h = self._chat_handler(temp_db)
        h.rfile.read = MagicMock(return_value=b"{}")
        h.headers = {"Content-Length": "2"}
        h.do_POST()
        assert h.send_response.call_args[0][0] == 400
        # Client-input validation errors keep the flat format (not structured).
        assert "missing required field" in _json_body(h)["error"]

    def test_spend_limit_exceeded_returns_429(self, temp_db):
        from src.api.models import Budget, get_session
        from sqlalchemy import Engine
        # temp_db may be a tuple (db_path, engine) or just engine
        if isinstance(temp_db, Engine):
            engine = temp_db
        else:
            engine = temp_db[1]
        with get_session(engine) as session:
            session.add(Budget(
                name="Key Cap", key_id=1, profile=None,
                amount=10.0, current_spend=12.0, period="total",
                threshold_pct="80", action="block", status="exceeded",
            ))
            session.commit()
        km = MagicMock()
        km.validate_key.return_value = {
            "id": 1,
            "allowed_profiles": None,
            "spend_limit": 0,
            "total_spend": 0,
        }
        with patch("src.server.handler.get_key_manager", return_value=km):
            h = self._chat_handler(temp_db)
            LCPHandler.config.get_profile.return_value["auth_required"] = True
            h.headers["Authorization"] = "Bearer somekey123"
            h.do_POST()
        assert h.send_response.call_args[0][0] == 429
        err = _json_body(h)["error"]
        assert err["code"] == "LCP-1002"


# ═══════════════════════════════════════════════════════════════════════
# Profile budget routing tests (GET/PUT /api/profiles/{name}/budget)
# ═══════════════════════════════════════════════════════════════════════

class TestProfileBudgetRouting:
    def test_get_profile_budget(self, temp_db):
        from src.api.models import Budget, get_session
        with get_session(temp_db) as session:
            session.add(Budget(
                name="L2 Cap", key_id=None, profile="l2",
                amount=200.0, current_spend=50.0, period="monthly",
                threshold_pct="50,80", action="block", status="active",
            ))
            session.commit()
        LCPHandler.engine = temp_db
        h = _TestHandler("/api/profiles/l2/budget", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 200
        body = _json_body(h)
        assert body["budget"]["name"] == "L2 Cap"
        assert body["budget"]["spend_pct"] == 25.0

    def test_get_profile_budget_when_none(self, temp_db):
        LCPHandler.engine = temp_db
        h = _TestHandler("/api/profiles/l2/budget", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 200
        assert _json_body(h)["budget"] is None

    def test_put_profile_budget_creates(self, temp_db):
        LCPHandler.config = MagicMock()
        LCPHandler.engine = temp_db
        body = json.dumps({"amount": 150.0, "action": "block", "threshold_pct": "80,90"}).encode()
        h = _TestHandler("/api/profiles/l1/budget", method="PUT", engine=temp_db)
        h.rfile.read = MagicMock(return_value=body)
        h.headers = {"Content-Length": str(len(body))}
        h.do_PUT()
        assert h.send_response.call_args[0][0] == 200
        assert _json_body(h)["ok"] is True
        from src.api.models import Budget, get_session
        with get_session(temp_db) as session:
            b = session.query(Budget).filter(Budget.profile == "l1").first()
            assert b is not None
            assert b.amount == 150.0
            assert b.action == "block"

    def test_put_profile_budget_updates_existing(self, temp_db):
        from src.api.models import Budget, get_session
        with get_session(temp_db) as session:
            b = Budget(
                name="L2 Cap", key_id=None, profile="l2",
                amount=200.0, current_spend=50.0, period="monthly",
                threshold_pct="50,80", action="block", status="active",
            )
            session.add(b)
            session.commit()
            budget_id = b.id
        LCPHandler.config = MagicMock()
        LCPHandler.engine = temp_db
        body = json.dumps({"amount": 300.0}).encode()
        h = _TestHandler("/api/profiles/l2/budget", method="PUT", engine=temp_db)
        h.rfile.read = MagicMock(return_value=body)
        h.headers = {"Content-Length": str(len(body))}
        h.do_PUT()
        assert h.send_response.call_args[0][0] == 200
        with get_session(temp_db) as session:
            assert session.get(Budget, budget_id).amount == 300.0

    def test_put_profile_budget_invalid_json(self, temp_db):
        LCPHandler.config = MagicMock()
        LCPHandler.engine = temp_db
        h = _TestHandler("/api/profiles/l1/budget", method="PUT", engine=temp_db)
        h.rfile.read = MagicMock(side_effect=Exception("bad json"))
        h.headers = {"Content-Length": "10"}
        h.do_PUT()
        assert h.send_response.call_args[0][0] == 400

    def test_put_unknown_profile_budget_route_404(self, temp_db):
        # Budget-less profile paths (4 segments) still route to profile update
        LCPHandler.config = MagicMock()
        LCPHandler.engine = temp_db
        h = _TestHandler("/api/profiles/l2/budgets", method="PUT", engine=temp_db)
        h.do_PUT()
        assert h.send_response.call_args[0][0] == 404


# ═══════════════════════════════════════════════════════════════════════
# Streaming chat (SSE) path tests
# ═══════════════════════════════════════════════════════════════════════

class TestStreamingChat:
    """Covers the SSE streaming block in do_POST (lines ~461-573)."""

    def _streaming_handler(self, temp_db):
        """Set up a handler ready to process a streaming chat request."""
        body = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
        body_bytes = json.dumps(body).encode()
        h = _TestHandler("/l2/chat/completions", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(return_value=body_bytes)
        h.headers = {"Content-Length": str(len(body_bytes))}

        LCPHandler.config = MagicMock()
        LCPHandler.config.profiles = {"l2": {"chain": [], "forbidden_tools": []}}
        LCPHandler.config.get_profile = MagicMock(return_value={
            "chain": [{"provider": "test", "model": "test-model", "base_url": "http://t"}],
            "forbidden_tools": [],
            "auth_required": False,
        })
        LCPHandler.config.get_pricing = MagicMock(return_value={
            "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0,
        })
        LCPHandler.config.providers = {"test": {"base_url": "http://t"}}
        return h

    def _sse_chunks_with_usage(self):
        return [
            b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":10}}\n\n',
            b'data: [DONE]\n\n',
        ]

    def test_streaming_writes_sse_and_records_cost(self, temp_db):
        h = self._streaming_handler(temp_db)
        chunks = self._sse_chunks_with_usage()
        with patch("src.server.handler.try_chain",
                   return_value=(iter(chunks), 200, "test", "test-model")):
            with patch("src.server.handler.get_prompt_cache") as mock_cache:
                mock_cache.return_value.get.return_value = None
                with patch("src.server.handler.record_cost") as mock_record:
                    with patch("src.server.handler.get_alert_manager"):
                        h.do_POST()

        assert h.send_response.call_args[0][0] == 200
        # SSE content type header
        content_types = [c[0][1] for c in h.send_header.call_args_list if c[0][0] == "Content-Type"]
        assert "text/event-stream" in content_types

        # All SSE chunks written to the client
        written = _get_written_bytes(h)
        assert b'data: {"choices":[{"delta":{"content":"Hel"}}]}' in written
        assert b'data: [DONE]' in written

        # Cost recorded for the streaming request
        mock_record.assert_called_once()
        args, kwargs = mock_record.call_args
        assert args[1] == "l2"
        assert args[3] == "test"  # provider
        assert args[4]["prompt_tokens"] == 10
        assert args[4]["completion_tokens"] == 5

    def test_streaming_without_usage_falls_back_to_estimation(self, temp_db):
        # Chunks with no usage block -> falls back to pre-flight estimation
        chunks = [
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        h = self._streaming_handler(temp_db)
        with patch("src.server.handler.try_chain",
                   return_value=(iter(chunks), 200, "test", "test-model")):
            with patch("src.server.handler.get_prompt_cache") as mock_cache:
                mock_cache.return_value.get.return_value = None
                with patch("src.server.handler.record_cost") as mock_record:
                    with patch("src.server.handler.get_alert_manager"):
                        h.do_POST()
        assert h.send_response.call_args[0][0] == 200
        mock_record.assert_called_once()
        args, kwargs = mock_record.call_args
        # Falls back to estimation-derived cost info
        assert args[4]["completion_tokens"] == 0

    def test_streaming_client_disconnect_swallowed(self, temp_db):
        h = self._streaming_handler(temp_db)
        chunks = self._sse_chunks_with_usage()
        h.wfile.write = MagicMock(side_effect=BrokenPipeError("client gone"))
        with patch("src.server.handler.try_chain",
                   return_value=(iter(chunks), 200, "test", "test-model")):
            with patch("src.server.handler.get_prompt_cache") as mock_cache:
                mock_cache.return_value.get.return_value = None
                with patch("src.server.handler.record_cost"):
                    with patch("src.server.handler.get_alert_manager"):
                        h.do_POST()  # should not raise

        assert h.send_response.call_args[0][0] == 200


# ═══════════════════════════════════════════════════════════════════════
# Static file serving
# ═══════════════════════════════════════════════════════════════════════

class TestStaticServing:
    def test_serves_css_file(self, temp_db):
        h = _TestHandler("/static/dashboard.css", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 200
        cts = [c[0][1] for c in h.send_header.call_args_list if c[0][0] == "Content-Type"]
        assert "text/css" in cts

    def test_path_traversal_forbidden(self, temp_db):
        h = _TestHandler("/static/../../etc/passwd", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 403
        assert _json_body(h)["error"] == "forbidden"

    def test_missing_file_404(self, temp_db):
        h = _TestHandler("/static/nonexistent-xyz.js", engine=temp_db)
        h.do_GET()
        assert h.send_response.call_args[0][0] == 404


# ═══════════════════════════════════════════════════════════════════════
# Auth failure paths in the chat POST flow
# ═══════════════════════════════════════════════════════════════════════

class TestAuthFailures:
    def _auth_handler(self, temp_db, headers=None):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        body_bytes = json.dumps(body).encode()
        h = _TestHandler("/l2/chat/completions", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(return_value=body_bytes)
        h.headers = {"Content-Length": str(len(body_bytes))}
        if headers:
            h.headers.update(headers)
        LCPHandler.config = MagicMock()
        LCPHandler.config.profiles = {"l2": {"chain": [], "forbidden_tools": []}}
        LCPHandler.config.get_profile = MagicMock(return_value={
            "chain": [{"provider": "test", "model": "test-model", "base_url": "http://t"}],
            "forbidden_tools": [],
            "auth_required": True,
        })
        LCPHandler.config.get_pricing = MagicMock(return_value={
            "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0,
        })
        LCPHandler.config.providers = {"test": {"base_url": "http://t"}}
        return h

    def test_missing_bearer_401(self, temp_db):
        h = self._auth_handler(temp_db, headers={"Authorization": ""})
        with patch("src.server.handler.get_key_manager"):
            h.do_POST()
        assert h.send_response.call_args[0][0] == 401
        assert _json_body(h)["error"]["code"] == "LCP-1001"

    def test_invalid_key_401(self, temp_db):
        h = self._auth_handler(temp_db, headers={"Authorization": "Bearer badkey"})
        km = MagicMock()
        km.validate_key.return_value = None
        with patch("src.server.handler.get_key_manager", return_value=km):
            h.do_POST()
        assert h.send_response.call_args[0][0] == 401
        assert _json_body(h)["error"]["code"] == "LCP-1001"

    def test_profile_access_denied_403(self, temp_db):
        h = self._auth_handler(temp_db, headers={"Authorization": "Bearer goodkey"})
        km = MagicMock()
        km.validate_key.return_value = {"id": 1, "allowed_profiles": "l1"}
        with patch("src.server.handler.get_key_manager", return_value=km):
            h.do_POST()
        assert h.send_response.call_args[0][0] == 403
        assert _json_body(h)["error"]["code"] == "LCP-1003"

    def test_budget_exceeded_during_auth_429(self, temp_db):
        from src.api.models import Budget, get_session
        with get_session(temp_db) as session:
            session.add(Budget(
                name="Key Cap", key_id=1, profile=None,
                amount=10.0, current_spend=12.0, period="total",
                threshold_pct="80", action="block", status="exceeded",
            ))
            session.commit()
        h = self._auth_handler(temp_db, headers={"Authorization": "Bearer goodkey"})
        km = MagicMock()
        km.validate_key.return_value = {"id": 1, "allowed_profiles": None}
        with patch("src.server.handler.get_key_manager", return_value=km):
            h.do_POST()
        assert h.send_response.call_args[0][0] == 429
        assert _json_body(h)["error"]["code"] == "LCP-1002"

    def test_valid_key_passes_auth_and_stores_key_id(self, temp_db):
        h = self._auth_handler(temp_db, headers={"Authorization": "Bearer goodkey"})
        km = MagicMock()
        km.validate_key.return_value = {"id": 1, "allowed_profiles": None}
        resp = {"choices": [{"message": {"content": "ok"}}], "usage": {
            "prompt_tokens": 10, "completion_tokens": 5,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10,
        }}
        with patch("src.server.handler.get_key_manager", return_value=km):
            with patch("src.server.handler.get_prompt_cache") as mock_cache:
                mock_cache.return_value.get.return_value = None
                with patch("src.server.handler.try_chain",
                           return_value=(resp, 200, "test", "test-model")):
                    with patch("src.server.handler.get_token_verifier") as mock_tv:
                        mock_tv.return_value.verify.return_value = {"suspicious": False}
                        with patch("src.server.handler.record_cost"):
                            with patch("src.server.handler.get_alert_manager"):
                                h.do_POST()
        assert h.send_response.call_args[0][0] == 200
        assert h._current_key_id == 1


# ═══════════════════════════════════════════════════════════════════════
# Missing pricing fallback (e.g. commandcode not in gateway.yaml pricing)
# ═══════════════════════════════════════════════════════════════════════

class TestPricingFallback:
    """A missing config pricing entry must NOT 500; it falls back to the
    plugin registry (or default rates) so the request proceeds to the chain."""

    def _handler_with_missing_pricing(self, temp_db):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        body_bytes = json.dumps(body).encode()
        h = _TestHandler("/coder/chat/completions", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(return_value=body_bytes)
        h.headers = {"Content-Length": str(len(body_bytes))}

        LCPHandler.config = MagicMock()
        LCPHandler.config.profiles = {"coder": {"chain": [], "forbidden_tools": []}}
        LCPHandler.config.get_profile = MagicMock(return_value={
            "chain": [{"provider": "commandcode", "model": "deepseek-v4-pro",
                       "base_url": "https://api.commandcode.ai/provider/v1"}],
            "forbidden_tools": [],
            "auth_required": False,
        })
        # Config has NO commandcode pricing -> get_pricing raises ConfigError,
        # exactly the "No pricing found for commandcode/deepseek-v4-pro" case.
        from src.api.exceptions import ConfigError
        LCPHandler.config.get_pricing = MagicMock(side_effect=ConfigError(
            "No pricing found for commandcode/deepseek-v4-pro"))
        LCPHandler.config.providers = {
            "commandcode": {"api_base": "https://api.commandcode.ai/provider/v1"}}
        return h

    def test_missing_pricing_does_not_500_and_reaches_chain(self, temp_db):
        """When config pricing is missing, the request proceeds (no 500)."""
        h = self._handler_with_missing_pricing(temp_db)
        resp = {"choices": [{"message": {"content": "ok"}}], "usage": {
            "prompt_tokens": 10, "completion_tokens": 5,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10,
        }}
        # Patch BOTH registry import sites so the plugin consistently supplies
        # pricing/cost (otherwise calculate_cost falls back to config.get_pricing
        # and re-raises the ConfigError → 500 in full-suite runs).
        reg = MagicMock()
        reg.get_pricing.return_value = {"cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0}
        reg.calculate_cost.return_value = 0.001
        with patch("src.api.cost_plugins.get_registry", return_value=reg), \
             patch("src.api.request_pipeline.get_registry", return_value=reg):
            with patch("src.server.handler.get_prompt_cache") as mock_cache:
                mock_cache.return_value.get.return_value = None
                with patch("src.server.handler.try_chain",
                           return_value=(resp, 200, "commandcode", "deepseek-v4-pro")) as mock_chain:
                    with patch("src.server.handler.record_cost"):
                        with patch("src.server.handler.get_alert_manager"):
                            with patch("src.server.handler.get_token_verifier") as mock_tv:
                                mock_tv.return_value.verify.return_value = {"suspicious": False}
                                h.do_POST()

        assert h.send_response.call_args[0][0] == 200, "must not 500 on missing pricing"
        mock_chain.assert_called_once()

    def test_resolve_pricing_plugin_fallback(self):
        """_resolve_pricing falls back to the plugin registry when config pricing is missing."""
        from src.server.handler import _resolve_pricing
        from src.api.exceptions import ConfigError
        cfg = MagicMock()
        cfg.get_pricing = MagicMock(side_effect=ConfigError("No pricing found"))
        reg = MagicMock()
        reg.get_pricing.return_value = {"cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0}
        with patch("src.api.cost_plugins.get_registry", return_value=reg):
            result = _resolve_pricing(cfg, "commandcode", "deepseek-v4-pro")
        assert result == {"cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0}
        reg.get_pricing.assert_called_once_with("commandcode", "deepseek-v4-pro")

    def test_resolve_pricing_default_when_nothing_found(self):
        """_resolve_pricing returns None (default rates) when neither config nor plugin has pricing."""
        from src.server.handler import _resolve_pricing
        from src.api.exceptions import ConfigError
        cfg = MagicMock()
        cfg.get_pricing = MagicMock(side_effect=ConfigError("No pricing found"))
        reg = MagicMock()
        reg.get_pricing.return_value = None
        with patch("src.api.cost_plugins.get_registry", return_value=reg):
            result = _resolve_pricing(cfg, "unknown", "unknown-model")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
# Non-streaming chat completion path
# ═══════════════════════════════════════════════════════════════════════

class TestNonStreamingChat:
    def _handler(self, temp_db, body):
        body_bytes = json.dumps(body).encode()
        h = _TestHandler("/l2/chat/completions", method="POST", engine=temp_db)
        h.rfile.read = MagicMock(return_value=body_bytes)
        h.headers = {"Content-Length": str(len(body_bytes))}
        LCPHandler.config = MagicMock()
        LCPHandler.config.profiles = {"l2": {"chain": [], "forbidden_tools": []}}
        LCPHandler.config.get_profile = MagicMock(return_value={
            "chain": [{"provider": "test", "model": "test-model", "base_url": "http://t"}],
            "forbidden_tools": [],
            "auth_required": False,
        })
        LCPHandler.config.get_pricing = MagicMock(return_value={
            "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0,
        })
        LCPHandler.config.providers = {"test": {"base_url": "http://t"}}
        return h

    def test_non_streaming_cache_hit_served(self, temp_db):
        body = {"messages": [{"role": "user", "content": "hi"}], "stream": False}
        h = self._handler(temp_db, body)
        cached = {"choices": [{"message": {"content": "cached reply"}}]}
        with patch("src.server.handler.get_prompt_cache") as mock_cache:
            mock_cache.return_value.get.return_value = cached
            h.do_POST()
        assert h.send_response.call_args[0][0] == 200
        written = _get_written_bytes(h)
        assert b"cached reply" in written

    def test_non_streaming_full_flow(self, temp_db):
        body = {"messages": [{"role": "user", "content": "hi"}], "stream": False}
        h = self._handler(temp_db, body)
        resp = {"choices": [{"message": {"content": "reply"}}], "usage": {
            "prompt_tokens": 10, "completion_tokens": 5,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10,
        }}
        with patch("src.server.handler.get_prompt_cache") as mock_cache:
            mock_cache.return_value.get.return_value = None
            with patch("src.server.handler.try_chain",
                       return_value=(resp, 200, "test", "test-model")):
                with patch("src.server.handler.get_token_verifier") as mock_tv:
                    mock_tv.return_value.verify.return_value = {"suspicious": False}
                    with patch("src.server.handler.record_cost") as mock_record:
                        with patch("src.server.handler.get_alert_manager"):
                            h.do_POST()
        assert h.send_response.call_args[0][0] == 200
        written = _get_written_bytes(h)
        assert b"reply" in written
        mock_record.assert_called_once()

    def test_suspicious_token_sets_warning_header(self, temp_db):
        body = {"messages": [{"role": "user", "content": "hi"}], "stream": False}
        h = self._handler(temp_db, body)
        resp = {"choices": [{"message": {"content": "reply"}}], "usage": {
            "prompt_tokens": 10, "completion_tokens": 5,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 10,
        }}
        with patch("src.server.handler.get_prompt_cache") as mock_cache:
            mock_cache.return_value.get.return_value = None
            with patch("src.server.handler.try_chain",
                       return_value=(resp, 200, "test", "test-model")):
                with patch("src.server.handler.get_token_verifier") as mock_tv:
                    mock_tv.return_value.verify.return_value = {
                        "suspicious": True,
                        "provider_prompt_tokens": 100,
                        "estimated_prompt_tokens": 50,
                        "prompt_discrepancy_pct": 100.0,
                    }
                    with patch("src.server.handler.record_cost"):
                        with patch("src.server.handler.get_alert_manager"):
                            h.do_POST()
        assert h.send_response.call_args[0][0] == 200
        # X-LCP-Token-Warning header was set
        header_vals = [c[0][1] for c in h.send_header.call_args_list if c[0][0] == "X-LCP-Token-Warning"]
        assert len(header_vals) == 1
        assert "suspicious" in header_vals[0]

    def test_budget_block_before_llm_returns_429(self, temp_db):
        """A blocking profile budget exceeded returns 429 LCP-4290 before the LLM call."""
        from src.api.models import Budget, get_session
        with get_session(temp_db) as session:
            session.add(Budget(
                name="L2 Hard Cap", key_id=None, profile="l2",
                amount=10.0, current_spend=12.0, period="monthly",
                threshold_pct="80", action="block", status="exceeded",
            ))
            session.commit()
        body = {"messages": [{"role": "user", "content": "hi"}], "stream": False}
        h = self._handler(temp_db, body)
        with patch("src.server.handler.get_prompt_cache"):
            with patch("src.server.handler.try_chain") as mock_try:
                h.do_POST()
        assert h.send_response.call_args[0][0] == 429
        err = _json_body(h)["error"]
        assert err["code"] == "LCP-4290"
        mock_try.assert_not_called()

    def test_all_providers_failed_returns_502(self, temp_db):
        from src.api.exceptions import AllProvidersFailedError
        body = {"messages": [{"role": "user", "content": "hi"}], "stream": False}
        h = self._handler(temp_db, body)
        with patch("src.server.handler.get_prompt_cache") as mock_cache:
            mock_cache.return_value.get.return_value = None
            with patch("src.server.handler.try_chain",
                       side_effect=AllProvidersFailedError("all down")):
                with patch("src.server.handler.record_cost"):
                    h.do_POST()
        assert h.send_response.call_args[0][0] == 502
