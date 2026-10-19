"""Batch H: request_pipeline.py residual gaps.

Targets term-missing lines: 134, 211-212, 225-226, 257-258, 266-267, 519,
616-625, 674, 748.
"""

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from src.api.request_pipeline import (
    capture_reasoning_from_sse,
    ensure_thinking_reasoning_content,
    normalize_messages_for_cache,
    try_chain,
)
from src.api.exceptions import RequestTooLargeError


@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.plugins = {}
    return cfg


@pytest.fixture
def chain_config(mock_config):
    """2-step chain config identical to test_request_pipeline_helpers."""
    from src.api.circuit_breaker import get_circuit_breaker
    mock_config.circuit_breaker = {
        "failures_dead": 5, "dead_cooldown_seconds": 300,
        "failures_degraded": 3, "degraded_cooldown_seconds": 60,
    }
    mock_config.profiles = {"l2": {"chain": [
        {"provider": "first", "base_url": "https://first.com/v1", "model": "m1"},
        {"provider": "second", "base_url": "https://second.com/v1", "model": "m2"},
    ], "forbidden_tools": []}}
    mock_config.providers = {
        "first": {"api_key_env": "KEY", "base_url": "https://first.com/v1"},
        "second": {"api_key_env": "KEY", "base_url": "https://second.com/v1"},
    }
    mock_config.model_limits = {}
    mock_config.get_provider_key.return_value = None
    get_circuit_breaker(mock_config)
    return mock_config


class TestNormalizeAssistantName:
    def test_assistant_name_kept(self):
        # 134: assistant tool_calls message with a name field
        out = normalize_messages_for_cache([{
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "f", "arguments": "{}"}}],
            "name": "tool-user",
        }])
        assert out[0]["name"] == "tool-user"


class TestCaptureSseGaps:
    def test_malformed_json_line_skipped(self):
        # 211-212: json.loads failure → continue
        raw = b"data: {not-json\n\ndata: [DONE]\n\n"
        capture_reasoning_from_sse(raw)  # best-effort, no raise

    def test_non_bytes_input_swallowed(self):
        # 225-226: outer try fails (None.decode) → except → pass
        capture_reasoning_from_sse(None)


class TestEnsureThinkingGaps:
    def test_limits_lookup_raises(self):
        # 257-258: get_model_limits raises → supports_thinking = False
        cfg = MagicMock()
        cfg.get_model_limits.side_effect = RuntimeError("nope")
        msgs = [{"role": "user", "content": "hi"}]
        assert ensure_thinking_reasoning_content(msgs, "m", cfg) == msgs

    def test_rehydrate_raises_swallowed(self):
        # 266-267: reasoning_store.rehydrate raises → except → pass,
        # Layer-2 presence fallback still runs
        cfg = MagicMock()
        cfg.get_model_limits.return_value = {"supports_thinking": True}
        fake_store = MagicMock()
        fake_store.rehydrate.side_effect = RuntimeError("db down")
        msgs = [{"role": "user", "content": "hi"}]
        with patch("src.api.request_pipeline.resolve_service", return_value=fake_store):
            out = ensure_thinking_reasoning_content(msgs, "m", cfg)
        assert out is not None


class TestForwardRequestStatusGaps:
    def _forward(self, code):
        from src.api.request_pipeline import forward_request
        cfg = {"provider": "test_prov", "base_url": "https://x/v1"}
        err = urllib.error.HTTPError(
            "https://x/v1/chat/completions", code, "redirected", {}, io.BytesIO(b"body"))
        with patch("src.api.credential_store.get_credential_store", return_value=None), \
             patch("src.api.request_pipeline._provider_needs_key", return_value=False), \
             patch("urllib.request.urlopen", side_effect=err):
            forward_request(cfg, {"messages": []}, MagicMock())

    def test_odd_status_falls_to_generic_auth_error(self):
        # 519: status < 400 and not credits → generic ProviderAuthError
        from src.api.exceptions import ProviderAuthError
        with pytest.raises(ProviderAuthError, match="HTTP 302"):
            self._forward(302)


class TestTryChainMemoryAndTools:
    def _body(self, **extra):
        b = {"messages": [{"role": "user", "content": "hi"}]}
        b.update(extra)
        return b

    def test_memory_auto_recall_injects(self, chain_config):
        # 616-623: plugins.memory.auto_recall → inject_memory_context applied
        chain_config.plugins = {"memory": {"auto_recall": True, "top_k": 5,
                                           "min_score": 0.1, "tag_filter": None}}
        seen = {}

        def fake_inject(messages, **kw):
            seen.update(kw)
            return [{"role": "system", "content": "mem"}] + messages

        fwd = MagicMock(return_value=({"ok": True}, 200))
        with patch("src.api.memory.harness.inject_memory_context", side_effect=fake_inject), \
             patch("src.api.request_pipeline.forward_request", fwd):
            body, status, *_ = try_chain("l2", chain_config.profiles["l2"],
                                         self._body(), chain_config)
        assert status == 200
        assert seen["top_k"] == 5
        assert seen["min_score"] == 0.1
        assert fwd.call_args[0][1]["messages"][0]["content"] == "mem"

    def test_memory_inject_failure_swallowed(self, chain_config):
        # 624-625: inject raises → except Exception → pass, request proceeds
        chain_config.plugins = {"memory": {"auto_recall": True}}
        fwd = MagicMock(return_value=({"ok": True}, 200))
        with patch("src.api.memory.harness.inject_memory_context",
                   side_effect=RuntimeError("memory corrupt")), \
             patch("src.api.request_pipeline.forward_request", fwd):
            body, status, provider, _ = try_chain(
                "l2", chain_config.profiles["l2"], self._body(), chain_config)
        assert status == 200

    def test_tools_normalized_in_chain(self, chain_config):
        # 748: body tools normalized via normalize_tools_for_cache
        fwd = MagicMock(return_value=({"ok": True}, 200))
        body = self._body(tools=[{"function": {"name": "zz"}},
                                 {"function": {"name": "aa"}}])
        with patch("src.api.request_pipeline.forward_request", fwd):
            try_chain("l2", chain_config.profiles["l2"], body, chain_config)
        sent = fwd.call_args[0][1]
        assert [t["function"]["name"] for t in sent["tools"]] == ["aa", "zz"]

    def test_request_too_large_propagates(self, chain_config):
        # 673-674: context preflight raises RequestTooLargeError → re-raised
        with patch("src.api.cost_estimator.count_tokens",
                   side_effect=RequestTooLargeError("too big")):
            with pytest.raises(RequestTooLargeError):
                try_chain("l2", chain_config.profiles["l2"], self._body(), chain_config)
