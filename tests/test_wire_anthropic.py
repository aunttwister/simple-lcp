"""Tests for the Anthropic ``/messages`` wire-format support.

Two halves:

* ``src/api/wire_anthropic.py`` — pure translation functions (request body,
  response body, SSE stream, error envelopes), tested directly.
* ``forward_request()`` — the wire-format branch, including the critical
  assertion that the OpenAI path stayed byte-identical (same URL, same headers
  in the same order, same payload) so production risk is confined to the new
  branch.

Contract facts asserted here were established by live probing of the
commandcode gateway (2026-11-02), not from vendor documentation.
"""

import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

from src.api.request_pipeline import (
    ANTHROPIC_WIRE,
    OPENAI_WIRE,
    _select_wire_format,
    forward_request,
)
from src.api.wire_anthropic import (
    ANTHROPIC_API_VERSION,
    ANTHROPIC_USER_AGENT,
    DEFAULT_MAX_TOKENS,
    anthropic_error,
    anthropic_finish_reason,
    from_anthropic,
    is_anthropic_model,
    sse_to_openai,
    to_anthropic,
)

CLAUDE = "claude-haiku-5-5"
BASE = "https://api.commandcode.ai/provider/v1"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _cred_patch(fallback="test-key", **keys):
    """Patch the credential store: keys[provider] or fallback."""
    store = MagicMock()
    store.get.side_effect = lambda name: keys.get(name, fallback)
    return patch("src.api.credential_store.get_credential_store", return_value=store)


def _capture_request(provider_cfg, body, *, session_id=None, response=None,
                     side_effect=None, fallback_key="sk-test-key"):
    """Run forward_request against a fake urlopen and capture the outgoing request."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = list(req.headers.items())
        captured["method"] = req.get_method()
        if side_effect is not None:
            raise side_effect
        resp = MagicMock()
        resp.status = 200
        resp.read.side_effect = response if response is not None else [
            b'{"choices":[{"message":{"content":"hi"}}]}'
        ]
        return resp

    mock_config = MagicMock()
    mock_config.get_provider_key.return_value = None
    with _cred_patch(**{provider_cfg["provider"]: fallback_key}), \
            patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = forward_request(provider_cfg, body, mock_config, session_id=session_id)
    return captured, result


def _anthropic_response(**overrides):
    payload = {
        "id": "msg_01ABC",
        "type": "message",
        "role": "assistant",
        "model": CLAUDE,
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }
    payload.update(overrides)
    return payload


def _sse(*events) -> bytes:
    """Build an SSE byte stream from (event_name, payload_dict) tuples."""
    out = []
    for name, payload in events:
        # Real streams carry raw UTF-8, not \uXXXX escapes — the incremental
        # decoder path is what we want to exercise.
        out.append(f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n")
    return "".join(out).encode("utf-8")


def _records(chunks):
    """Parse translated OpenAI SSE bytes into a list of data payloads."""
    text = b"".join(chunks).decode("utf-8")
    payloads = []
    for line in text.split("\n"):
        if line.startswith("data:"):
            raw = line[5:].strip()
            payloads.append(raw if raw == "[DONE]" else json.loads(raw))
    return payloads


TEXT_STREAM = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_01ABC", "type": "message", "role": "assistant",
        "model": CLAUDE, "content": [], "stop_reason": None,
        "usage": {"input_tokens": 12, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                            "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "Hello"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": " world"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 7}}),
    ("message_stop", {"type": "message_stop"}),
]


# ── Model detection ─────────────────────────────────────────────────────────


class TestIsAnthropicModel:
    @pytest.mark.parametrize("model", [
        "claude-sonnet-5",
        "claude-haiku-5-5",
        "Claude-Opus-4",
        "anthropic/claude-3-5-sonnet",
        "commandcode/claude-haiku-5-5",
        "  claude-sonnet-5  ",
        "anthropic-claude-x",
    ])
    def test_anthropic_ids(self, model):
        assert is_anthropic_model(model) is True

    @pytest.mark.parametrize("model", [
        "deepseek-v4-pro", "gpt-5.6-luna", "kimi-k3", "minimax-m3",
        "qwen3.8-max", "", "   ", "notclaude", None, 123, ["claude"],
    ])
    def test_non_anthropic_ids(self, model):
        assert is_anthropic_model(model) is False


# ── Request translation ─────────────────────────────────────────────────────


class TestSystemHoist:
    def test_system_message_hoisted_out_of_messages(self):
        out = to_anthropic({
            "model": CLAUDE,
            "messages": [
                {"role": "system", "content": "you are terse"},
                {"role": "user", "content": "hi"},
            ],
        })
        assert out["system"] == "you are terse"
        assert [m["role"] for m in out["messages"]] == ["user"]

    def test_multiple_system_messages_joined(self):
        out = to_anthropic({
            "model": CLAUDE,
            "messages": [
                {"role": "system", "content": "first"},
                {"role": "developer", "content": "second"},
                {"role": "user", "content": "hi"},
            ],
        })
        assert out["system"] == "first\n\nsecond"

    def test_no_system_key_when_absent(self):
        out = to_anthropic({"model": CLAUDE,
                            "messages": [{"role": "user", "content": "hi"}]})
        assert "system" not in out

    def test_top_level_system_merged_with_message_system(self):
        out = to_anthropic({
            "system": "top",
            "messages": [
                {"role": "system", "content": "inline"},
                {"role": "user", "content": "hi"},
            ],
        })
        assert out["system"] == "top\n\ninline"


class TestToolRoundTrip:
    def test_assistant_tool_calls_become_tool_use_blocks(self):
        out = to_anthropic({
            "model": CLAUDE,
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": "checking",
                 "tool_calls": [{"id": "call_1", "type": "function", "function": {
                     "name": "get_weather", "arguments": '{"city": "Berlin"}'}}]},
            ],
        })
        assistant = out["messages"][1]
        assert assistant["role"] == "assistant"
        assert assistant["content"][0] == {"type": "text", "text": "checking"}
        assert assistant["content"][1] == {
            "type": "tool_use", "id": "call_1", "name": "get_weather",
            "input": {"city": "Berlin"},
        }

    def test_tool_result_becomes_tool_result_block(self):
        out = to_anthropic({
            "model": CLAUDE,
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "call_1", "type": "function", "function": {
                     "name": "get_weather", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "18C sunny"},
            ],
        })
        result_turn = out["messages"][2]
        assert result_turn["role"] == "user"
        assert result_turn["content"] == [{
            "type": "tool_result", "tool_use_id": "call_1", "content": "18C sunny",
        }]

    def test_consecutive_tool_results_merge_into_one_turn(self):
        out = to_anthropic({
            "model": CLAUDE,
            "messages": [
                {"role": "user", "content": "both"},
                {"role": "tool", "tool_call_id": "c1", "content": "one"},
                {"role": "tool", "tool_call_id": "c2", "content": "two"},
            ],
        })
        assert len(out["messages"]) == 2
        blocks = out["messages"][1]["content"]
        assert [b["type"] for b in blocks] == ["tool_result", "tool_result"]
        assert [b["tool_use_id"] for b in blocks] == ["c1", "c2"]

    def test_tool_result_after_user_text_is_not_merged(self):
        out = to_anthropic({
            "model": CLAUDE,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "tool", "tool_call_id": "c1", "content": "one"},
            ],
        })
        assert len(out["messages"]) == 2
        assert out["messages"][1]["content"][0]["type"] == "tool_result"

    def test_malformed_tool_arguments_degrade_to_empty_object(self):
        out = to_anthropic({
            "model": CLAUDE,
            "messages": [{"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": "{not json"}}]}],
        })
        assert out["messages"][0]["content"][0]["input"] == {}

    def test_assistant_with_only_tool_calls_gets_no_text_block(self):
        out = to_anthropic({
            "model": CLAUDE,
            "messages": [{"role": "assistant", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}]}],
        })
        blocks = out["messages"][0]["content"]
        assert [b["type"] for b in blocks] == ["tool_use"]

    def test_empty_content_becomes_empty_text_block(self):
        out = to_anthropic({"model": CLAUDE, "messages": [
            {"role": "assistant", "content": ""}]})
        assert out["messages"][0]["content"] == [{"type": "text", "text": ""}]

    def test_openai_tools_become_input_schema(self):
        out = to_anthropic({"model": CLAUDE, "messages": [], "tools": [{
            "type": "function", "function": {
                "name": "get_weather", "description": "get it",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            }}]})
        assert out["tools"] == [{
            "name": "get_weather", "description": "get it",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }]

    def test_tools_without_parameters_get_empty_schema(self):
        out = to_anthropic({"model": CLAUDE, "messages": [], "tools": [
            {"type": "function", "function": {"name": "f"}}]})
        assert out["tools"][0]["input_schema"] == {"type": "object", "properties": {}}

    @pytest.mark.parametrize("choice,expected", [
        ("auto", {"type": "auto"}),
        ("required", {"type": "any"}),
        ("any", {"type": "any"}),
        ("none", {"type": "none"}),
        ({"type": "function", "function": {"name": "f"}}, {"type": "tool", "name": "f"}),
        ({"type": "auto"}, {"type": "auto"}),
        ("bogus", None),
        (None, None),
    ])
    def test_tool_choice_mapping(self, choice, expected):
        body = {"model": CLAUDE, "messages": []}
        if choice is not None:
            body["tool_choice"] = choice
        out = to_anthropic(body)
        assert out.get("tool_choice") == expected

    def test_image_url_parts_translated(self):
        out = to_anthropic({"model": CLAUDE, "messages": [{
            "role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ]}]})
        blocks = out["messages"][0]["content"]
        assert blocks[0] == {"type": "text", "text": "look"}
        assert blocks[1] == {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}
        assert blocks[2] == {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "AAA"}}


class TestParameterTranslation:
    def test_temperature_is_dropped_entirely(self):
        out = to_anthropic({"model": CLAUDE, "temperature": 0.7,
                            "messages": [{"role": "user", "content": "hi"}]})
        assert "temperature" not in out

    def test_max_tokens_defaulted_when_absent(self):
        out = to_anthropic({"model": CLAUDE,
                            "messages": [{"role": "user", "content": "hi"}]})
        assert out["max_tokens"] == DEFAULT_MAX_TOKENS

    def test_max_tokens_preserved(self):
        out = to_anthropic({"model": CLAUDE, "max_tokens": 16, "messages": []})
        assert out["max_tokens"] == 16

    def test_max_completion_tokens_used_when_max_tokens_absent(self):
        out = to_anthropic({"model": CLAUDE, "max_completion_tokens": 32, "messages": []})
        assert out["max_tokens"] == 32

    @pytest.mark.parametrize("value", [None, 0, -5, True, "many"])
    def test_invalid_max_tokens_falls_back_to_default(self, value):
        out = to_anthropic({"model": CLAUDE, "max_tokens": value, "messages": []})
        assert out["max_tokens"] == DEFAULT_MAX_TOKENS

    def test_stream_preserved_true(self):
        out = to_anthropic({"model": CLAUDE, "stream": True, "messages": []})
        assert out["stream"] is True

    def test_stream_preserved_false(self):
        out = to_anthropic({"model": CLAUDE, "stream": False, "messages": []})
        assert out["stream"] is False

    def test_stream_absent_when_not_supplied(self):
        out = to_anthropic({"model": CLAUDE, "messages": []})
        assert "stream" not in out

    def test_stop_string_becomes_stop_sequences(self):
        out = to_anthropic({"model": CLAUDE, "stop": "END", "messages": []})
        assert out["stop_sequences"] == ["END"]

    def test_stop_list_becomes_stop_sequences(self):
        out = to_anthropic({"model": CLAUDE, "stop": ["a", "b"], "messages": []})
        assert out["stop_sequences"] == ["a", "b"]

    def test_openai_only_parameters_dropped(self):
        out = to_anthropic({
            "model": CLAUDE, "messages": [],
            "n": 2, "logprobs": True, "top_logprobs": 3,
            "frequency_penalty": 0.5, "presence_penalty": 0.2,
            "response_format": {"type": "json_object"},
            "stream_options": {"include_usage": True},
            "parallel_tool_calls": False,
            "reasoning_effort": "high",
            "user": "pavle",
        })
        for key in ("n", "logprobs", "top_logprobs", "frequency_penalty",
                    "presence_penalty", "response_format", "stream_options",
                    "parallel_tool_calls", "reasoning_effort", "user"):
            assert key not in out

    def test_top_p_forwarded(self):
        out = to_anthropic({"model": CLAUDE, "top_p": 0.9, "messages": []})
        assert out["top_p"] == 0.9

    def test_model_argument_wins_over_body(self):
        out = to_anthropic({"model": "logical-name", "messages": []}, CLAUDE)
        assert out["model"] == CLAUDE

    def test_model_defaults_to_body(self):
        out = to_anthropic({"model": CLAUDE, "messages": []})
        assert out["model"] == CLAUDE

    def test_input_body_is_not_mutated(self):
        body = {
            "model": CLAUDE, "temperature": 0.3, "max_tokens": 8,
            "messages": [{"role": "system", "content": "s"},
                         {"role": "user", "content": "u"}],
        }
        snapshot = json.loads(json.dumps(body))
        to_anthropic(body)
        assert body == snapshot

    def test_non_mapping_body_raises(self):
        with pytest.raises(TypeError):
            to_anthropic(["not", "a", "dict"])  # type: ignore[arg-type]


# ── Response translation ────────────────────────────────────────────────────


class TestFromAnthropic:
    def test_text_content_mapped_to_message_content(self):
        out = from_anthropic(_anthropic_response())
        assert out["choices"][0]["message"]["content"] == "hello"
        assert out["choices"][0]["message"]["role"] == "assistant"
        assert out["object"] == "chat.completion"
        assert out["id"] == "msg_01ABC"

    def test_multiple_text_blocks_concatenated(self):
        out = from_anthropic(_anthropic_response(content=[
            {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]))
        assert out["choices"][0]["message"]["content"] == "ab"

    def test_thinking_blocks_excluded_from_content(self):
        out = from_anthropic(_anthropic_response(content=[
            {"type": "thinking", "thinking": "secret"},
            {"type": "text", "text": "answer"},
            {"type": "redacted_thinking", "data": "x"}]))
        assert out["choices"][0]["message"]["content"] == "answer"

    def test_tool_use_blocks_mapped_to_tool_calls(self):
        out = from_anthropic(_anthropic_response(
            content=[{"type": "text", "text": "using a tool"},
                     {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                      "input": {"city": "Berlin"}}],
            stop_reason="tool_use"))
        message = out["choices"][0]["message"]
        assert message["content"] == "using a tool"
        assert message["tool_calls"] == [{
            "id": "toolu_1", "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Berlin"}'},
        }]
        assert out["choices"][0]["finish_reason"] == "tool_calls"

    def test_tool_use_only_yields_empty_content(self):
        out = from_anthropic(_anthropic_response(
            content=[{"type": "tool_use", "id": "t", "name": "f", "input": {}}],
            stop_reason="tool_use"))
        assert out["choices"][0]["message"]["content"] == ""
        assert "tool_calls" in out["choices"][0]["message"]

    @pytest.mark.parametrize("stop_reason,finish_reason", [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("refusal", "content_filter"),
        ("pause_turn", "stop"),
        ("something_new", "stop"),
        (None, "stop"),
    ])
    def test_stop_reason_mapping(self, stop_reason, finish_reason):
        out = from_anthropic(_anthropic_response(stop_reason=stop_reason))
        assert out["choices"][0]["finish_reason"] == finish_reason
        assert anthropic_finish_reason(stop_reason) == finish_reason

    def test_usage_mapped(self):
        out = from_anthropic(_anthropic_response(
            usage={"input_tokens": 100, "output_tokens": 25}))
        assert out["usage"] == {
            "prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125}

    def test_usage_cache_fields_passed_through(self):
        out = from_anthropic(_anthropic_response(usage={
            "input_tokens": 10, "output_tokens": 5,
            "cache_read_input_tokens": 90, "cache_creation_input_tokens": 4}))
        assert out["usage"]["cache_read_input_tokens"] == 90
        assert out["usage"]["cache_creation_input_tokens"] == 4

    def test_usage_absent_yields_zeros(self):
        resp = _anthropic_response()
        resp.pop("usage")
        out = from_anthropic(resp)
        assert out["usage"] == {"prompt_tokens": 0, "completion_tokens": 0,
                                "total_tokens": 0}

    def test_model_override_and_fallback(self):
        assert from_anthropic(_anthropic_response(), "claude-x")["model"] == "claude-x"
        resp = _anthropic_response()
        resp["model"] = ""
        assert from_anthropic(resp)["model"] == ""

    def test_string_content_tolerated(self):
        out = from_anthropic(_anthropic_response(content="plain"))
        assert out["choices"][0]["message"]["content"] == "plain"

    def test_non_mapping_response_raises(self):
        with pytest.raises(TypeError):
            from_anthropic("nope")  # type: ignore[arg-type]

    def test_result_is_json_serialisable(self):
        out = from_anthropic(_anthropic_response(
            content=[{"type": "tool_use", "id": "t", "name": "f", "input": {"a": 1}}]))
        assert json.loads(json.dumps(out))["choices"][0]["message"]["tool_calls"]


# ── SSE translation ─────────────────────────────────────────────────────────


class TestSseTranslation:
    def test_full_text_stream(self):
        records = _records(sse_to_openai([_sse(*TEXT_STREAM)]))
        assert records[0]["choices"][0]["delta"]["role"] == "assistant"
        deltas = [r["choices"][0]["delta"].get("content") for r in records
                  if isinstance(r, dict)]
        assert [d for d in deltas if d] == ["Hello", " world"]
        assert records[-1] == "[DONE]"

    def test_message_stop_emits_done_terminator(self):
        out = b"".join(sse_to_openai([_sse(*TEXT_STREAM)]))
        assert out.endswith(b"data: [DONE]\n\n")

    def test_done_still_emitted_when_stream_truncated(self):
        truncated = TEXT_STREAM[:-1]  # no message_stop
        out = b"".join(sse_to_openai([_sse(*truncated)]))
        assert out.count(b"[DONE]") == 1
        assert out.endswith(b"data: [DONE]\n\n")

    def test_usage_becomes_final_usage_record(self):
        records = _records(sse_to_openai([_sse(*TEXT_STREAM)]))
        final = [r for r in records if isinstance(r, dict) and "usage" in r][-1]
        assert final["usage"] == {"prompt_tokens": 12, "completion_tokens": 7,
                                  "total_tokens": 19}
        assert final["choices"][0]["finish_reason"] == "stop"

    def test_last_data_line_is_the_usage_record(self):
        """The streaming cost accountant reads the LAST data chunk."""
        from src.server.sse_helpers import extract_last_sse_chunk
        translated = b"".join(sse_to_openai([_sse(*TEXT_STREAM)]))
        last = extract_last_sse_chunk(translated)
        assert last is not None
        assert last["usage"]["completion_tokens"] == 7

    def test_text_helpers_recover_the_assistant_text(self):
        from src.server.sse_helpers import extract_sse_text
        translated = b"".join(sse_to_openai([_sse(*TEXT_STREAM)]))
        assert extract_sse_text(translated) == "Hello world"

    def test_events_split_across_chunk_boundaries(self):
        raw = _sse(*TEXT_STREAM)
        pieces = [raw[i:i + 7] for i in range(0, len(raw), 7)]
        records = _records(sse_to_openai(pieces))
        assert records[-1] == "[DONE]"
        text = "".join(r["choices"][0]["delta"].get("content") or ""
                       for r in records if isinstance(r, dict))
        assert text == "Hello world"

    def test_multibyte_character_split_across_chunks(self):
        raw = _sse(
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "café ünïcode"}}),
            ("message_stop", {"type": "message_stop"}),
        )
        needle = "café ünïcode".encode("utf-8")
        split = raw.index(needle) + 2  # inside the multibyte sequence
        records = _records(sse_to_openai([raw[:split], raw[split:]]))
        text = "".join(r["choices"][0]["delta"].get("content") or ""
                       for r in records if isinstance(r, dict))
        assert text == "café ünïcode"

    def test_tool_use_streaming(self):
        records = _records(sse_to_openai([_sse(
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "tool_use",
                                                       "id": "toolu_1", "name": "f"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "input_json_delta",
                                               "partial_json": '{"a": 1}'}}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "tool_use"},
                               "usage": {"output_tokens": 3}}),
            ("message_stop", {"type": "message_stop"}),
        )]))
        start = records[0]["choices"][0]["delta"]["tool_calls"][0]
        assert start["id"] == "toolu_1"
        assert start["function"]["name"] == "f"
        args = records[1]["choices"][0]["delta"]["tool_calls"][0]
        assert args["function"]["arguments"] == '{"a": 1}'
        final = [r for r in records if isinstance(r, dict) and "usage" in r][-1]
        assert final["choices"][0]["finish_reason"] == "tool_calls"

    def test_thinking_delta_maps_to_reasoning_content(self):
        records = _records(sse_to_openai([_sse(
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "thinking_delta",
                                               "thinking": "hmm"}}),
            ("message_stop", {"type": "message_stop"}),
        )]))
        assert records[0]["choices"][0]["delta"]["reasoning_content"] == "hmm"

    def test_error_event_relayed_then_done(self):
        records = _records(sse_to_openai([_sse(
            ("error", {"type": "error", "error": {"type": "overloaded_error",
                                                  "message": "Overloaded"}}))]))
        assert records[0]["error"]["type"] == "overloaded_error"
        assert records[-1] == "[DONE]"

    def test_unknown_and_ping_events_ignored(self):
        records = _records(sse_to_openai([_sse(
            ("ping", {"type": "ping"}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_stop", {"type": "message_stop"}),
        )]))
        assert records == ["[DONE]"]

    def test_garbage_data_line_is_skipped_not_fatal(self):
        raw = (b"event: message_start\ndata: {not json}\n\n"
               + _sse(("content_block_delta",
                       {"type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": "ok"}}),
                      ("message_stop", {"type": "message_stop"})))
        records = _records(sse_to_openai([raw]))
        assert records[0]["choices"][0]["delta"]["content"] == "ok"
        assert records[-1] == "[DONE]"

    def test_stream_is_lazy_not_buffered(self):
        """Consuming one event must not pull the rest of the upstream stream."""
        reads = []

        def source():
            reads.append(1)
            yield b'event: message_start\ndata: {"type":"message_start","message":{"id":"m","model":"c"}}\n\n'
            raise AssertionError("sse_to_openai read ahead of the consumer")

        gen = sse_to_openai(source())
        first = next(gen)
        assert json.loads(bytes(first).decode().split("data:", 1)[1])["choices"][0]["delta"]["role"] == "assistant"
        assert reads == [1]

    def test_output_is_bytes(self):
        out = list(sse_to_openai([_sse(*TEXT_STREAM)]))
        assert all(isinstance(chunk, bytes) for chunk in out)


# ── Error mapping ───────────────────────────────────────────────────────────


class TestAnthropicError:
    def _envelope(self, etype, message, code=None):
        err = {"type": etype, "message": message}
        if code:
            err["code"] = code
        return json.dumps({"type": "error", "error": err})

    def test_400_invalid_request(self):
        message, code, etype = anthropic_error(
            400, self._envelope("invalid_request_error",
                                "temperature is deprecated for this model"))
        assert "temperature is deprecated" in message
        assert code == "invalid_request_error"
        assert etype == "invalid_request_error"

    def test_401_authentication(self):
        message, code, etype = anthropic_error(
            401, self._envelope("authentication_error", "invalid x-api-key"))
        assert "Authentication failed" in message
        assert code == "authentication_error"
        assert etype == "authentication_error"

    def test_401_with_empty_body(self):
        message, code, etype = anthropic_error(401, "")
        assert code == "authentication_error"
        assert message

    def test_403_model_not_in_plan(self):
        message, code, etype = anthropic_error(
            403, self._envelope("permission_error", "MODEL_NOT_IN_PLAN: sonnet", code="MODEL_NOT_IN_PLAN"))
        assert "Model not available on your current plan" in message
        assert code == "MODEL_NOT_IN_PLAN"
        assert etype == "permission_error"

    def test_403_plain_permission_error(self):
        message, code, etype = anthropic_error(
            403, self._envelope("permission_error", "no access to this model"))
        assert code in ("MODEL_NOT_IN_PLAN", "permission_error")
        assert etype == "permission_error"

    def test_403_upgrade_required(self):
        message, code, etype = anthropic_error(
            403, self._envelope("permission_error", "upgrade_required"))
        assert code == "upgrade_required"
        assert etype == "permission_error"
        assert "Provider plan" in message

    def test_429_rate_limit(self):
        message, code, etype = anthropic_error(
            429, self._envelope("rate_limit_error", "slow down"))
        assert "Rate limited" in message
        assert code == "rate_limit_error"
        assert etype == "rate_limit_error"

    def test_429_bare_status_still_rate_limit(self):
        message, code, etype = anthropic_error(429, "")
        assert code == "rate_limit_error"

    def test_529_overloaded(self):
        message, code, etype = anthropic_error(
            529, self._envelope("overloaded_error", "Overloaded"))
        assert "overloaded" in message.lower()
        assert code == "overloaded_error"
        assert etype == "overloaded_error"

    def test_529_without_envelope(self):
        message, code, etype = anthropic_error(529, "")
        assert etype == "overloaded_error"

    def test_non_json_body_falls_back_to_raw_text(self):
        message, code, etype = anthropic_error(400, "<html>bad gateway</html>")
        assert message == "<html>bad gateway</html>"
        assert code == "invalid_request_error"

    def test_always_returns_three_strings(self):
        for status in (400, 401, 403, 429, 500, 529):
            message, code, etype = anthropic_error(status, '{"error":{"message":"x"}}')
            assert isinstance(message, str) and message
            assert isinstance(code, str)
            assert isinstance(etype, str)


# ── The OpenAI path must not have changed ───────────────────────────────────


def _legacy_openai_request(provider_cfg, body, api_key, session_id):
    """The pre-Anthropic implementation of forward_request's request build.

    Kept verbatim so the test can assert the OpenAI branch is byte-identical:
    same URL, same payload bytes, same header names in the same order.
    """
    url = f"{provider_cfg['base_url']}/chat/completions"
    data = json.dumps(body).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "LLMControlPlane/1.0",
    }
    if session_id:
        headers["x-opencode-session"] = session_id
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return urllib.request.Request(url, data=data, headers=headers, method="POST")


class TestOpenAIPathUnchanged:
    BODY = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
        "temperature": 0.5,
        "max_tokens": 64,
        "tools": [{"type": "function", "function": {"name": "f",
                                                    "parameters": {"type": "object"}}}],
        "stream": False,
    }
    CFG = {"provider": "testco", "base_url": "https://api.example.com/v1"}

    def test_request_is_byte_identical_to_legacy(self):
        captured, _ = _capture_request(self.CFG, dict(self.BODY),
                                       fallback_key="sk-test123")
        legacy = _legacy_openai_request(self.CFG, self.BODY, "sk-test123", None)
        assert captured["url"] == legacy.full_url
        assert captured["data"] == legacy.data
        assert captured["data"] == json.dumps(self.BODY).encode("utf-8")
        assert captured["headers"] == list(legacy.headers.items())
        assert captured["method"] == "POST"

    def test_header_order_preserved(self):
        captured, _ = _capture_request(self.CFG, dict(self.BODY))
        names = [name for name, _ in captured["headers"]]
        assert names == ["Content-type", "User-agent", "Authorization"]

    def test_session_header_position_preserved(self):
        captured, _ = _capture_request(self.CFG, dict(self.BODY), session_id="sess-1",
                                       fallback_key="test-key")
        legacy = _legacy_openai_request(self.CFG, self.BODY, "test-key", "sess-1")
        assert captured["headers"] == list(legacy.headers.items())
        assert [n for n, _ in captured["headers"]] == [
            "Content-type", "User-agent", "X-opencode-session", "Authorization"]

    def test_keyless_provider_sends_no_authorization(self):
        cfg = {"provider": "llamacpp", "base_url": "http://localhost:8080/v1"}
        captured, _ = _capture_request(cfg, dict(self.BODY), fallback_key="")
        assert [n for n, _ in captured["headers"]] == ["Content-type", "User-agent"]

    def test_body_is_not_mutated(self):
        body = json.loads(json.dumps(self.BODY))
        snapshot = json.loads(json.dumps(self.BODY))
        _capture_request(self.CFG, body)
        assert body == snapshot

    def test_response_body_passed_through_untouched(self):
        payload = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
        _, (body_out, status) = _capture_request(
            self.CFG, dict(self.BODY), response=[json.dumps(payload).encode()])
        assert body_out == payload
        assert status == 200

    def test_explicit_openai_wire_override_for_claude_model(self):
        cfg = {"provider": "testco", "base_url": "https://api.example.com/v1",
               "wire": "openai"}
        captured, _ = _capture_request(cfg, {"model": CLAUDE, "messages": []},
                                       fallback_key="test-key")
        assert captured["url"].endswith("/chat/completions")
        assert ("Authorization", "Bearer test-key") in captured["headers"]

    def test_no_wire_key_means_openai_for_unknown_models(self):
        assert _select_wire_format({"provider": "p"}, {"model": "gpt-5.6-luna"}) == OPENAI_WIRE
        assert _select_wire_format({"provider": "p"}, {}) == OPENAI_WIRE


# ── The Anthropic branch end to end through forward_request ────────────────


class TestAnthropicBranch:
    CFG = {"provider": "commandcode", "base_url": BASE}

    def test_routes_to_messages_with_anthropic_auth(self):
        body = {"model": CLAUDE, "temperature": 0.2, "max_tokens": 16,
                "messages": [{"role": "system", "content": "s"},
                             {"role": "user", "content": "reply only: ok"}]}
        captured, _ = _capture_request(
            self.CFG, body, response=[json.dumps(_anthropic_response()).encode()])
        assert captured["url"] == f"{BASE}/messages"
        headers = dict(captured["headers"])
        assert headers["X-api-key"] == "sk-test-key"
        assert headers["Anthropic-version"] == ANTHROPIC_API_VERSION
        assert "Authorization" not in headers
        payload = json.loads(captured["data"])
        assert payload["model"] == CLAUDE
        assert "temperature" not in payload
        assert payload["max_tokens"] == 16
        assert payload["system"] == "s"
        assert [m["role"] for m in payload["messages"]] == ["user"]

    def test_default_user_agent_is_the_measured_value(self):
        captured, _ = _capture_request(
            self.CFG, {"model": CLAUDE, "messages": []},
            response=[json.dumps(_anthropic_response()).encode()])
        assert dict(captured["headers"])["User-agent"] == ANTHROPIC_USER_AGENT

    def test_user_agent_override_from_provider_cfg(self):
        cfg = dict(self.CFG, user_agent="custom-agent/2.0")
        captured, _ = _capture_request(
            cfg, {"model": CLAUDE, "messages": []},
            response=[json.dumps(_anthropic_response()).encode()])
        assert dict(captured["headers"])["User-agent"] == "custom-agent/2.0"

    def test_session_header_still_sent_on_anthropic_wire(self):
        captured, _ = _capture_request(
            self.CFG, {"model": CLAUDE, "messages": []}, session_id="sess-9",
            response=[json.dumps(_anthropic_response()).encode()])
        assert dict(captured["headers"])["X-opencode-session"] == "sess-9"

    def test_non_streamed_response_translated(self):
        _, (body_out, status) = _capture_request(
            self.CFG, {"model": CLAUDE, "messages": []},
            response=[json.dumps(_anthropic_response()).encode()])
        assert status == 200
        assert body_out["choices"][0]["message"]["content"] == "hello"
        assert body_out["usage"] == {"prompt_tokens": 12, "completion_tokens": 7,
                                     "total_tokens": 19}

    def test_streamed_response_translated(self):
        payload = _sse(*TEXT_STREAM)
        _, (reader, status) = _capture_request(
            self.CFG, {"model": CLAUDE, "messages": [], "stream": True},
            response=[payload, b""])
        assert status == 200
        records = _records(list(reader))
        assert records[-1] == "[DONE]"
        text = "".join(r["choices"][0]["delta"].get("content") or ""
                       for r in records if isinstance(r, dict))
        assert text == "Hello world"

    def test_openai_stream_path_unwrapped(self):
        """An OpenAI-wire stream must still be passed through verbatim."""
        payload = b'data: {"choices":[{"delta":{"content":"raw"}}]}\n\ndata: [DONE]\n\n'
        _, (reader, _) = _capture_request(
            {"provider": "testco", "base_url": "https://api.example.com/v1"},
            {"model": "deepseek-v4-pro", "stream": True, "messages": []},
            response=[payload, b""])
        assert b"".join(reader) == payload

    def test_wire_override_forces_anthropic_on_other_models(self):
        cfg = {"provider": "commandcode", "base_url": BASE, "wire": "messages"}
        captured, _ = _capture_request(
            cfg, {"model": "some-proxy-name", "messages": []},
            response=[json.dumps(_anthropic_response()).encode()])
        assert captured["url"] == f"{BASE}/messages"

    def test_http_403_raises_auth_error_with_friendly_text(self):
        envelope = json.dumps({"type": "error", "error": {
            "type": "permission_error", "message": "MODEL_NOT_IN_PLAN: claude-sonnet-5",
            "code": "MODEL_NOT_IN_PLAN"}}).encode()
        err = urllib.error.HTTPError("url", 403, "Forbidden", {}, None)
        err.read = MagicMock(return_value=envelope)
        from src.api.exceptions import ProviderAuthError
        with pytest.raises(ProviderAuthError) as excinfo:
            _capture_request(self.CFG, {"model": CLAUDE, "messages": []}, side_effect=err)
        message = str(excinfo.value)
        assert "rejected auth" in message
        assert "Model not available on your current plan" in message

    def test_http_429_raises_rate_limit(self):
        err = urllib.error.HTTPError("url", 429, "Too Many", {}, None)
        err.read = MagicMock(return_value=json.dumps(
            {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}).encode())
        from src.api.exceptions import ProviderRateLimitError
        with pytest.raises(ProviderRateLimitError):
            _capture_request(self.CFG, {"model": CLAUDE, "messages": []}, side_effect=err)

    def test_http_529_raises_retryable_internal_error(self):
        err = urllib.error.HTTPError("url", 529, "Overloaded", {}, None)
        err.read = MagicMock(return_value=json.dumps(
            {"type": "error", "error": {"type": "overloaded_error",
                                        "message": "Overloaded"}}).encode())
        from src.api.exceptions import ProviderInternalError
        with pytest.raises(ProviderInternalError) as excinfo:
            _capture_request(self.CFG, {"model": CLAUDE, "messages": []}, side_effect=err)
        assert "overloaded" in str(excinfo.value).lower()

    def test_http_400_raises_bad_request(self):
        err = urllib.error.HTTPError("url", 400, "Bad Request", {}, None)
        err.read = MagicMock(return_value=json.dumps({"type": "error", "error": {
            "type": "invalid_request_error",
            "message": "temperature is deprecated for this model"}}).encode())
        from src.api.exceptions import ProviderBadRequestError
        with pytest.raises(ProviderBadRequestError) as excinfo:
            _capture_request(self.CFG, {"model": CLAUDE, "messages": []}, side_effect=err)
        assert "temperature is deprecated" in str(excinfo.value)

    def test_openai_error_message_still_raw_body(self):
        """The OpenAI wire must keep quoting the raw provider body verbatim."""
        err = urllib.error.HTTPError("url", 400, "Bad Request", {}, None)
        err.read = MagicMock(return_value=b'{"error":"bad request"}')
        from src.api.exceptions import ProviderBadRequestError
        with pytest.raises(ProviderBadRequestError) as excinfo:
            _capture_request({"provider": "testco", "base_url": "https://api.example.com/v1"},
                             {"model": "deepseek-v4-pro", "messages": []}, side_effect=err)
        assert str(excinfo.value).endswith('{"error":"bad request"}')

    def test_wire_constants_exported(self):
        assert ANTHROPIC_WIRE == "anthropic"
        assert OPENAI_WIRE == "openai"
