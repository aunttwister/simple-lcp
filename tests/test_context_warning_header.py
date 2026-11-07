"""Regression: near-limit context warning surfaces as X-LCP-Context-Warning header.

The pre-flight no longer hard-413s near-limit requests; try_chain appends a
warning to the optional warning_sink and the handler emits it as a response
header so the coding agent gets a signal before history is silently truncated.
"""
import json
import os
import tempfile

import pytest
from unittest.mock import patch, MagicMock

from src.server import LCPHandler


class _TestHandler(LCPHandler):
    def __init__(self, path="/", method="GET", engine=None):
        self.path = path
        self.command = method
        self.headers = {}
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
        self.rfile.read = MagicMock(return_value=b"{}")
        self._write_chunk = MagicMock()
        self.engine = engine
        self.log_error = MagicMock()


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


def _chat_handler(temp_db):
    h = _TestHandler(path="/l2/chat/completions", method="POST", engine=temp_db)
    h.handler_config = MagicMock()
    LCPHandler.config = MagicMock()
    # _resolve_profile() looks up the path segment in config.profiles;
    # get_profile() then returns the chain config for that profile.
    LCPHandler.config.profiles = {"l2": {
        "chain": [{"provider": "test", "model": "test-model", "base_url": "http://t"}],
        "forbidden_tools": [],
        "auth_required": False,
    }}
    LCPHandler.config.get_profile = lambda name: LCPHandler.config.profiles.get(name)
    LCPHandler.config.providers = {"test": {"base_url": "http://t"}}
    LCPHandler.config.get_pricing = MagicMock(return_value={
        "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0})
    LCPHandler.config.model_limits = {}
    return h


def _set_body(h, body: str):
    """Simulate Content-Length-aware body read the way the real handler does."""
    h.headers = {"Content-Length": str(len(body.encode()))}
    h.rfile.read = MagicMock(return_value=body.encode())


class TestContextWarningHeader:
    def test_near_limit_warning_header_emitted_non_streaming(self, temp_db):
        h = _chat_handler(temp_db)
        h.wfile = MagicMock()
        h.wfile.write = MagicMock()
        body = json.dumps({
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        })
        _set_body(h, body)

        def fake_try_chain(profile, cfg, body, config, warning_sink=None,
                           session_id=None, on_step=None):
            if warning_sink is not None:
                warning_sink.append("[lcp] request 100 tokens is at/near the 128000-token context limit for profile 'l2'; history may be truncated")
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}, 200, "test", "test-model"

        with patch("src.server.handler.try_chain", side_effect=fake_try_chain):
            h.do_POST()

        headers = {c[0][0]: c[0][1] for c in h.send_header.call_args_list}
        assert "X-LCP-Context-Warning" in headers
        assert "context limit" in headers["X-LCP-Context-Warning"]

    def test_near_limit_warning_header_emitted_streaming(self, temp_db):
        h = _chat_handler(temp_db)
        h.wfile = MagicMock()
        h.wfile.write = MagicMock()
        body = json.dumps({
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        })
        _set_body(h, body)

        def fake_try_chain(profile, cfg, body, config, warning_sink=None,
                           session_id=None, on_step=None):
            if warning_sink is not None:
                warning_sink.append("[lcp] request 100 tokens is at/near the 128000-token context limit for profile 'l2'")
            return iter([b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', b"data: [DONE]\n\n"]), 200, "test", "test-model"

        with patch("src.server.handler.try_chain", side_effect=fake_try_chain):
            h.do_POST()

        headers = {c[0][0]: c[0][1] for c in h.send_header.call_args_list}
        assert "X-LCP-Context-Warning" in headers

    def test_no_warning_no_header(self, temp_db):
        h = _chat_handler(temp_db)
        h.wfile = MagicMock()
        h.wfile.write = MagicMock()
        body = json.dumps({
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        })
        _set_body(h, body)

        def fake_try_chain(profile, cfg, body, config, warning_sink=None,
                           session_id=None, on_step=None):
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}, 200, "test", "test-model"

        with patch("src.server.handler.try_chain", side_effect=fake_try_chain):
            h.do_POST()

        headers = {c[0][0]: c[0][1] for c in h.send_header.call_args_list}
        assert "X-LCP-Context-Warning" not in headers