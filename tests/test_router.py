"""Tests for router.py"""
import os
import tempfile

import pytest
import src.api.router as router_mod
from src.api.router import (
    CapabilityRouter, classify_task, get_dynamic_router,
    logical_model_name, benchmark_model_name,
    normalize_model_id, detect_quantization,
)
from src.api.seed_capabilities import DEFAULT_MODEL_REGISTRY


def _seed_router(db_path="data/costs.db", enabled=False, cost_bias=0.15):
    """Seed the module-level dynamic router (legacy init_router)."""
    r = CapabilityRouter(enabled=enabled, db_path=db_path, cost_bias=cost_bias)
    if enabled:
        r.load_matrix()
    router_mod._dynamic_router = r
    return r

# A realistic VS Code Copilot system prompt (the exact text the client sends as
# role="system" — some of which is echoed as role="user").
SYSTEM_PROMPT = (
    "You are an expert AI programming assistant, working in a VS Code workspace. "
    "Follow the user's requirements carefully & to the letter. When asked for "
    "your name, you must respond with \"GitHub Copilot\". When asked about the "
    "model you are using, you must state that you are using coder."
)


@pytest.fixture
def registry_db():
    """A fresh DB seeded with the default model registry."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from src.api.models import get_engine, Base
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    from src.api.seed_capabilities import seed_model_registry
    seed_model_registry(path)
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


# ── CapabilityRouter tests ────────────────────────────────────────────────

def test_classify_code_generation():
    msgs = [{"role": "user", "content": "write a function in python to sort a list"}]
    assert classify_task(msgs) == "code_generation"

def test_classify_debugging():
    msgs = [{"role": "user", "content": "why does this fail with a TypeError?"}]
    assert classify_task(msgs) == "debugging"

def test_classify_agentic():
    msgs = [{"role": "system", "content": "You are an AI agent with tools: read_file, write_file"}]
    assert classify_task(msgs) == "agentic_multi_step"

def test_classify_agentic_system_prompt_does_not_mask_planning():
    """An agentic system prompt must not mask a concrete planning user msg."""
    msgs = [
        {"role": "system", "content": "You are an AI agent with tools: read_file, write_file, terminal"},
        {"role": "user", "content": "design the architecture for a microservice"},
    ]
    assert classify_task(msgs) == "planning"

def test_classify_agentic_system_prompt_does_not_mask_debugging():
    msgs = [
        {"role": "system", "content": "You are a coding agent. tools: terminal, patch"},
        {"role": "user", "content": "debug why this returns a TypeError"},
    ]
    assert classify_task(msgs) == "debugging"

def test_classify_agentic_system_prompt_does_not_mask_unit_tests():
    msgs = [
        {"role": "system", "content": "You are a coding agent with tools: write_file"},
        {"role": "user", "content": "write unit tests for the payment module"},
    ]
    assert classify_task(msgs) == "unit_tests"

def test_classify_user_intent_beats_assistant_test_chatter():
    """Regression: assistant/tool messages that mention 'test' must NOT hijack
    the classification. The USER message is the actual intent signal."""
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "can we plan the next feature in features folder?"},
        {"role": "assistant", "content": "Sure, let me look at the existing test suite first."},
        {"role": "tool", "content": "Ran 12 tests, 3 failed. The test file is features/test_plan.py"},
    ]
    assert classify_task(msgs) == "planning"

def test_classify_user_intent_beats_tool_test_output():
    """A user debugging request must stay debugging even when tool output
    mentions running tests."""
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "why does this endpoint return a 500? debug the traceback"},
        {"role": "assistant", "content": "I ran the test suite to reproduce"},
        {"role": "tool", "content": "test_plan.py: 3 tests passed, 1 failed"},
    ]
    assert classify_task(msgs) == "debugging"

def test_classify_user_mentioning_tests_still_unit_tests():
    """If the USER message itself asks for tests, it's still unit_tests despite
    any assistant/tool chatter."""
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "write pytest unit tests for the auth module with mocking"},
        {"role": "assistant", "content": "Let me check the current test coverage"},
    ]
    assert classify_task(msgs) == "unit_tests"

def test_classify_first_user_msg_beats_tool_result_as_user():
    """Regression: some clients send tool results as role='user'. The classifier
    must use the FIRST user message (the original instruction), not the
    accumulated user text or the latest tool echo."""
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "can we plan the next feature in features folder?"},
        {"role": "assistant", "content": "Let me look at the current state."},
        {"role": "user", "content": "[tool result] Ran 12 tests, 3 failed. test_plan.py: FAILED"},
    ]
    assert classify_task(msgs) == "planning"

def test_classify_first_user_msg_beats_later_test_echoes():
    """A multi-turn session where the first user turn was planning and later
    tool-result user turns mention tests must stay planning."""
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "design the architecture for the new feature"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "[tool] running tests..."},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "[tool] 10 tests passed"},
    ]
    assert classify_task(msgs) == "planning"

# ── Newest genuine user instruction (intent extraction) ───────────────────

def test_classify_mid_session_instruction_reclassifies():
    """A session that started with debugging must RE-CLASSIFY when the user
    later issues a new planning instruction (regression for sticky-debugging:
    the old first-user-message behavior froze intent for the whole session)."""
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "fix this error in the code"},
        {"role": "assistant", "content": "Let me reproduce."},
        {"role": "user", "content": "[tool result] Ran 12 tests, 3 failed. test_plan.py: FAILED"},
        {"role": "assistant", "content": "I see the bug."},
        {"role": "user", "content": "can we plan the next feature in features folder?"},
    ]
    assert classify_task(msgs) == "planning"

def test_classify_continuation_keeps_earlier_intent():
    """A trailing 'continue' carries no new intent — it must NOT reset a
    debugging session; the intent stays with the last real instruction."""
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "why does this endpoint return a 500? debug the traceback"},
        {"role": "assistant", "content": "Running the endpoint..."},
        {"role": "user", "content": "[tool] Exit code: 1, traceback below"},
        {"role": "assistant", "content": "Found the failing line."},
        {"role": "user", "content": "continue"},
    ]
    assert classify_task(msgs) == "debugging"

def test_classify_short_instruction_not_treated_as_continuation():
    """Short genuine instructions ('fix it', 'make it work') are intent, not
    continuations. (Semantic-only: 'fix this' lands on code_generation; the
    point is that it is NOT skipped as a continuation.)"""
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "fix this"},
    ]
    assert classify_task(msgs) == "code_generation"

def test_classify_structural_tool_result_after_assistant_tool_calls():
    """OpenAI-style: assistant.tool_calls followed by a bare user message is a
    tool result and must be skipped in favour of the real instruction."""
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "add unit tests for the auth module with mocking"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "run_tests", "arguments": "{}"}}]},
        {"role": "user", "content": '{"output": "3 tests passed, 1 failed"}'},
    ]
    assert classify_task(msgs) == "unit_tests"

def test_extract_intent_text_meta():
    """_extract_intent_text reports which source won and how many messages were
    skipped (feeds the future observability work)."""
    from src.api.router import _extract_intent_text
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "why does this test fail"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "[tool result] Ran 12 tests, 3 failed"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "continue"},
    ]
    text, meta = _extract_intent_text(msgs)
    assert text == "why does this test fail"
    assert meta["source"] == "last_instruction"
    assert meta["skipped_tool"] == 1
    assert meta["skipped_cont"] == 1

def test_extract_intent_text_pure_tool_conversation_falls_back():
    """Edge: a conversation with no genuine instruction falls back to the first
    user message instead of erroring."""
    from src.api.router import _extract_intent_text
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "[tool result] Ran 12 tests, 3 failed"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "[tool] command finished with exit code 1"},
    ]
    text, meta = _extract_intent_text(msgs)
    assert meta["source"] == "first_user_fallback"
    assert "Ran 12 tests" in text

def test_extract_intent_text_skips_attachment_context():
    """A trailing client-injected <attachments> wrapper must not become the
    intent — the real user instruction behind it wins."""
    from src.api.router import _extract_intent_text
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "write pytest tests for the router"},
        {"role": "assistant", "content": "on it"},
        {"role": "user", "content":
         "<attachments>\n<attachment id=\"Browser Pages\">\nNo browser pages "
         "are currently shared with you.\n</attachment>"},
    ]
    text, meta = _extract_intent_text(msgs)
    assert text == "write pytest tests for the router"
    assert meta["source"] == "last_instruction"
    assert meta["skipped_context"] == 1

def test_extract_intent_text_skips_context_and_tool_echoes():
    """Client-context wrappers and tool echoes both skipped; the walk finds
    the last genuine instruction."""
    from src.api.router import _extract_intent_text
    msgs = [
        {"role": "system", "content": "You are a coding agent. tools: terminal"},
        {"role": "user", "content": "debug this traceback"},
        {"role": "assistant", "content": "running",
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "run", "arguments": "{}"}}]},
        {"role": "user", "content": "[tool result] Ran 12 tests, 3 failed", "tool_call_id": "c1"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "<attachments> <attachment id='X'> no pages"},
    ]
    text, meta = _extract_intent_text(msgs)
    assert text == "debug this traceback"
    assert meta["source"] == "last_instruction"
    assert meta["skipped_tool"] == 1
    assert meta["skipped_context"] == 1

def test_summarize_preserves_attachment_skip():
    """End-trimmed summary keeps the <attachments> prefix so replay skips it
    and the round-trip intent is unchanged."""
    from src.api.router import _summarize_conversation, _extract_intent_text
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "debug this traceback"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "<attachments> <attachment id='X'> " + "z" * 300},
    ]
    s = _summarize_conversation(msgs)
    assert _extract_intent_text(s)[0] == "debug this traceback"



def test_extract_intent_text_preamble_not_mid_message():
    """A real instruction that merely MENTIONS the system prompt (not at the
    start) must NOT be skipped."""
    from src.api.router import _extract_intent_text
    msgs = [{"role": "user", "content":
             "can you check whether 'You are an expert AI programming assistant' "
             "appears in the system prompt?"}]
    text, meta = _extract_intent_text(msgs)
    assert meta["source"] == "last_instruction"
    assert text.startswith("can you check")

def test_extract_intent_text_skips_copilot_system_echo_first_user():
    """Copilot's system prompt (echoed as the FIRST role=user message, with
    the model name substituted) is skipped — the real user message wins."""
    from src.api.router import _extract_intent_text
    echo = (
        "You are an expert AI programming assistant, working with a user in the "
        "VS Code editor.\nWhen asked for your name, you must respond with \"GitHub "
        "Copilot\". When asked about the model you are using, you must state that "
        "you are using coder.\nFollow the user's requirements carefully & to the "
        "letter."
    )
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": echo},
        {"role": "assistant", "content": "on it"},
        {"role": "user", "content": "debug this traceback"},
    ]
    text, meta = _extract_intent_text(msgs)
    assert text == "debug this traceback"
    assert meta["source"] == "last_instruction"
    # The echo precedes the real message; the walk finds the real one first.
    assert meta["skipped_preamble"] in (0, 1)

def test_extract_intent_text_preamble_only_neutralizes():
    """When the newest user message is a long GENERIC preamble (any harness —
    here structural, no Copilot markers), it is flagged preamble so the
    classifier can neutralize it instead of keyword-matching."""
    from src.api.router import _extract_intent_text, _is_preamble_like
    preamble = (
        "You are an autonomous software engineering agent operating in a remote "
        "development environment. Your responsibilities include analyzing the "
        "workspace, making precise modifications, running verification commands, "
        "and providing clear summaries of the work performed. You should prioritize "
        "clarity, correctness, and incremental progress throughout the session."
    )
    assert _is_preamble_like(preamble) is True
    conv = [
        {"role": "system", "content": "[older messages omitted]"},
        {"role": "tool", "content": "some output", "tool_call_id": "c1"},
        {"role": "assistant", "content": "processing"},
        {"role": "user", "content": preamble},
    ]
    text, meta = _extract_intent_text(conv)
    assert meta["preamble"] is True
    assert meta["skipped_preamble"] >= 1
    assert meta["source"] == "preamble"
    assert text == preamble  # returned flagged (not dropped) so classify can neutralize

def test_classify_preamble_routes_to_agentic():
    """A long generic preamble as the only intent must NOT be keyword-matched
    (e.g. misroute to debugging) — it routes to the neutral agentic default."""
    from src.api.router import classify_task_detail
    preamble = (
        "You are an autonomous software engineering agent operating in a remote "
        "development environment. Your responsibilities include analyzing the "
        "workspace, making precise modifications, running verification commands, "
        "and providing clear summaries of the work performed. You should prioritize "
        "clarity, correctness, and incremental progress throughout the session."
    )
    conv = [
        {"role": "system", "content": "[older messages omitted]"},
        {"role": "user", "content": preamble},
    ]
    detail = classify_task_detail(conv)
    assert detail.task == "agentic_multi_step"
    assert detail.path == "preamble"

def test_extract_intent_text_short_system_match_not_echo():
    """A SHORT user message that happens to match a system phrase must NOT be
    skipped (length guard) — e.g. 'follow the user's requirements' is a real
    short request, not an echo."""
    from src.api.router import _extract_intent_text
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "debug this traceback"}]
    text, meta = _extract_intent_text(msgs)
    assert meta["source"] == "last_instruction"
    assert text == "debug this traceback"

def test_extract_intent_text_combined_preamble_keeps_tail():
    """When a long generic preamble appends a REAL instruction after a blank
    line, the tail (the request) is kept as intent."""
    from src.api.router import _extract_intent_text
    preamble = (
        "You are an autonomous software engineering agent operating in a remote "
        "development environment. Your responsibilities include analyzing the "
        "workspace, making precise modifications, and running verification "
        "commands.\n\n\ncan we plan the next feature in the features folder?"
    )
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": preamble}]
    text, meta = _extract_intent_text(msgs)
    assert meta["source"] == "last_instruction"
    assert "plan the next feature" in text
    assert not text.startswith("You are an")

def test_extract_intent_text_strips_mention_prefix():
    """Copilot prefixes replies with [username] — the real request follows and
    must be kept (with the prefix stripped)."""
    from src.api.router import _extract_intent_text
    msgs = [{"role": "user", "content":
             "[aunttwister] can you set the SEO job that scans the blog for issues?"}]
    text, meta = _extract_intent_text(msgs)
    assert meta["source"] == "last_instruction"
    assert text.startswith("can you set the SEO job")
    assert not text.startswith("[")

def test_extract_intent_text_skips_reply_quote_wrapper():
    """Copilot's [Replying to: \"...\"] wrapper (sent as role=user) is not an
    instruction — the walk skips it and finds the real user message."""
    from src.api.router import _extract_intent_text
    msgs = [
        {"role": "user", "content": "can you help me bring up a qwen3.8 model locally?"},
        {"role": "assistant", "content": "sure, here's how..."},
        {"role": "user", "content": "[Replying to: \"can you help me bring up a qwen3.8 model locally?\"]"},
    ]
    text, meta = _extract_intent_text(msgs)
    # The wrapper carries no new instruction → walk finds the real user msg.
    assert meta["source"] == "last_instruction"
    assert text == "can you help me bring up a qwen3.8 model locally?"

def test_extract_intent_text_reply_quote_keeps_tail_instruction():
    """A [Replying to: ...] wrapper that appends a NEW instruction on the next
    line keeps that instruction as the intent."""
    from src.api.router import _extract_intent_text
    msgs = [
        {"role": "user", "content": "can you help me bring up a qwen3.8 model locally?"},
        {"role": "assistant", "content": "sure, here's how..."},
        {"role": "user", "content": "[Replying to: \"can you help me bring up a qwen3.8 model locally?\"]\n\nand also configure the port"},
    ]
    text, meta = _extract_intent_text(msgs)
    assert meta["source"] == "last_instruction"
    assert "and also configure the port" in text

def test_extract_intent_text_skips_copilot_agent_instructions():
    """The agent-customization block (When asked for your name...) is detected
    as a system-prompt substring echo — no phrase list needed."""
    from src.api.router import _extract_intent_text
    mid_block = (
        "When asked for your name, you must respond with \"GitHub Copilot\". "
        "When asked about the model you are using, you must state that you are "
        "using coder."
    )
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "debug this traceback"},
        {"role": "assistant", "content": "on it"},
        {"role": "user", "content": mid_block},
    ]
    text, meta = _extract_intent_text(msgs)
    assert text == "debug this traceback"
    assert meta["source"] == "last_instruction"
    assert meta["skipped_preamble"] == 1

def test_extract_intent_text_skips_empty_tool_response_feedback():
    """Copilot's 'You just executed tool calls but returned an empty response.'
    model-feedback message is not user intent."""
    from src.api.router import _extract_intent_text
    msgs = [
        {"role": "user", "content": "write pytest tests for the router"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "run", "arguments": "{}"}}]},
        {"role": "user", "content": "You just executed tool calls but returned an empty response."},
    ]
    text, meta = _extract_intent_text(msgs)
    assert text == "write pytest tests for the router"
    assert meta["source"] == "last_instruction"

def test_classify_agentic_system_prompt_only_still_agentic():
    """With no concrete user task, the agentic system prompt still wins."""
    msgs = [{"role": "system", "content": "You are an AI agent with tools: read_file, write_file"}]
    assert classify_task(msgs) == "agentic_multi_step"

def test_classify_system_prompt_planning_keywords_do_not_force_planning():
    """Regression: a system prompt full of incidental planning keywords (plan,
    strategy, recommend, suggest, best practice) must NOT classify a code
    request as planning. Classification must use conversation content, not the
    fixed agent preamble."""
    msgs = [
        {"role": "system", "content": (
            "You are a coding assistant. Plan your approach, recommend best "
            "practices, suggest a strategy, and design clean architecture.")},
        {"role": "user", "content": "implement a sorting algorithm in python"},
    ]
    assert classify_task(msgs) == "code_generation"

def test_classify_planning_keywords_in_user_message_win():
    """Planning keywords in the USER message (actual intent) still classify as
    planning even when the system prompt is generic."""
    msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "design the architecture for a microservice"},
    ]
    assert classify_task(msgs) == "planning"

def test_classify_planning():
    msgs = [{"role": "user", "content": "design the architecture for a microservice"}]
    assert classify_task(msgs) == "planning"

def test_classify_recommend_not_planning():
    """Bare 'recommend'/'suggest' must NOT force planning (too generic)."""
    msgs = [{"role": "user", "content": "recommend a good book to read"}]
    assert classify_task(msgs) != "planning"

def test_classify_implement_not_planning_even_with_long_max_tokens():
    """A code request with a large output budget must NOT be classified planning."""
    msgs = [{"role": "user", "content": "implement a sorting algorithm in python"}]
    assert classify_task(msgs, max_tokens=16384) == "code_generation"

def test_classify_long_max_tokens_no_longer_forces_planning():
    """Long max_tokens alone is not a planning signal."""
    msgs = [{"role": "user", "content": "write me a report about whales"}]
    assert classify_task(msgs, max_tokens=16384) != "planning"

def test_classify_casual():
    msgs = [{"role": "user", "content": "hello, how are you?"}]
    assert classify_task(msgs) == "casual_chat"

def test_classify_defaults_to_code():
    """A generic factual question is NOT routed to code once semantics are active.

    The semantic classifier (when sentence-transformers is installed) classifies
    by meaning — a factual query lands on reasoning_chain rather than the
    keyword default of code_generation.
    """
    msgs = [{"role": "user", "content": "what is the speed of light?"}]
    # Semantics active: meaning beats the code_generation keyword default.
    assert classify_task(msgs) == "reasoning_chain"

def test_classify_many_tools():
    """Tool-count still signals agentic, but semantic classification runs first.

    With the semantic layer active, a vague 'do something' is classified by
    MEANING even when many tools are present — never by the tool-count
    heuristic. The exact semantic label is geometry-dependent (casual vs code),
    but the invariant is that it must NOT fall through to the agentic
    tool-count path.
    """
    tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(6)]
    msgs = [{"role": "user", "content": "do something"}]
    # Semantics active: meaning beats the tool-count agentic heuristic.
    assert classify_task(msgs, tools=tools) != "agentic_multi_step"

def test_classify_unit_tests():
    msgs = [{"role": "user", "content": "write unit tests for the payment module"}]
    assert classify_task(msgs) == "unit_tests"

def test_classify_pytest_suite():
    msgs = [{"role": "user", "content": "add a pytest test suite with mocks for the auth service"}]
    assert classify_task(msgs) == "unit_tests"

def test_classify_unit_tests_beats_code_gen_class_kw():
    # "class " is a code_generation signal, but unit tests must win first-match.
    msgs = [{"role": "user", "content": "add unit tests to this class"}]
    assert classify_task(msgs) == "unit_tests"

def test_classify_code_gen_not_unit_tests():
    msgs = [{"role": "user", "content": "implement a sorting algorithm in python"}]
    assert classify_task(msgs) == "code_generation"

def test_singleton():
    assert get_dynamic_router() is get_dynamic_router()

def test_init_router_passes_cost_bias(registry_db):
    _seed_router(registry_db, enabled=True, cost_bias=0.4)
    try:
        r = get_dynamic_router()
        assert r.enabled is True
        assert r.cost_bias == 0.4
    finally:
        # restore the default so other tests aren't affected
        _seed_router(enabled=False)

def test_sync_router_enabled_from_settings(registry_db, monkeypatch):
    """Boot seeds enabled=False from the yaml baseline; the persisted UI toggle
    must re-sync the global router to enabled=True."""
    _seed_router(registry_db, enabled=False)
    try:
        class FakeSettings:
            def get_routing_enabled(self, default=None):
                return True
        monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: FakeSettings())
        from src.api.router import sync_router_enabled_from_settings
        assert sync_router_enabled_from_settings() is True
        assert get_dynamic_router().enabled is True
    finally:
        _seed_router(enabled=False)

def test_get_model_score_resolves_debugging_via_matrix(registry_db):
    """debugging is derived from code_generation, so scores must resolve."""
    from src.api.seed_capabilities import seed_livebench, load_capability_matrix
    seed_livebench(registry_db)
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    matrix = load_capability_matrix(registry_db)
    assert "debugging" in matrix
    # A debugging prompt should resolve to a real score (not the 0.5 default).
    score = router.get_model_score("deepseek-v4-pro", "debugging")
    assert score > 0.5

def test_get_model_score_falls_back_for_unknown_model(registry_db):
    """A model with no debugging row falls back to the 0.5 default."""
    from src.api.seed_capabilities import seed_livebench
    seed_livebench(registry_db)
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    assert router.get_model_score("some-unknown-model", "debugging") == 0.5

# ── Provider-aware selection (Phase 2) ───────────────────────────────────

def test_score_step_uses_capability_and_cost(registry_db):
    from src.api.seed_capabilities import seed_livebench
    seed_livebench(registry_db)
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    # No breaker/cost-cache configured → pure capability + cost-bias boost.
    s = router.score_step({"provider": "opencode", "model": "deepseek-v4-pro",
                           "base_url": "https://opencode.ai"}, "reasoning_chain")
    assert s > 0.5
    # Same model on the same step scores identically without health input.
    s2 = router.score_step({"provider": "deepseek", "model": "deepseek-v4-pro",
                            "base_url": "https://deepseek.com"}, "reasoning_chain")
    assert abs(s - s2) < 1e-9  # health/credit tiebreakers both absent

def test_health_bonus_penalizes_degraded(registry_db):
    from src.api.seed_capabilities import seed_livebench
    from src.api.circuit_breaker import get_circuit_breaker
    seed_livebench(registry_db)
    router = CapabilityRouter(enabled=True, db_path=registry_db)

    class _Cfg:
        providers = {"deepseek": {"api_base": "https://deepseek.com"},
                     "opencode": {"api_base": "https://opencode.ai"}}
    cfg = _Cfg()
    breaker_cfg = {"failures_dead": 5, "dead_cooldown_seconds": 60,
                   "failures_degraded": 3, "degraded_cooldown_seconds": 60}
    get_circuit_breaker(breaker_cfg)

    healthy_step = {"provider": "deepseek", "model": "deepseek-v4-pro"}
    degraded_step = {"provider": "opencode", "model": "deepseek-v4-pro"}
    # Mark opencode degraded for this profile/base_url.
    get_circuit_breaker().get_health("opencode", "https://opencode.ai", "l2")["status"] = "degraded"

    sh = router.score_step(healthy_step, "reasoning_chain", profile="l2", config=cfg)
    sd = router.score_step(degraded_step, "reasoning_chain", profile="l2", config=cfg)
    assert sh > sd  # healthy provider step scores higher

def test_credit_bonus_penalizes_low_credits(registry_db, monkeypatch):
    from src.api.seed_capabilities import seed_livebench
    seed_livebench(registry_db)
    router = CapabilityRouter(enabled=True, db_path=registry_db)

    class FakeCache:
        def get(self, provider, kind):
            if provider == "commandcode" and kind == "subscription":
                return {"payload": {"monthly_credits_remaining": 1.0}}
            return None
    monkeypatch.setattr("src.api.cost_cache.get_cost_cache", lambda: FakeCache())

    step = {"provider": "commandcode", "model": "deepseek-v4-pro", "base_url": "x"}
    base = router.score_step(step, "reasoning_chain")
    # Same provider with plenty of credits scores higher (no penalty).
    class RichCache:
        def get(self, provider, kind):
            if provider == "commandcode" and kind == "subscription":
                return {"payload": {"monthly_credits_remaining": 50.0}}
            return None
    monkeypatch.setattr("src.api.cost_cache.get_cost_cache", lambda: RichCache())
    rich = router.score_step(step, "reasoning_chain")
    assert rich > base

def test_select_step_reorders_best_first(registry_db, monkeypatch):
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    # Force known scores: second step clearly better.
    monkeypatch.setattr(router, "score_step",
                        lambda step, task, profile=None, config=None, bias=None:
                        0.9 if step["model"] == "deepseek-v4-pro" else 0.6)
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    out = router.select_step([{"role": "user", "content": "hi"}], chain=chain)
    assert out is not None
    assert out[0]["model"] == "deepseek-v4-pro"  # best first

def test_select_step_keeps_order_within_hysteresis(registry_db, monkeypatch):
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    # Both steps ~equal → no reorder (avoids flapping).
    monkeypatch.setattr(router, "score_step",
                        lambda step, task, profile=None, config=None, bias=None:
                        0.81 if step["model"] == "deepseek-v4-pro" else 0.80)
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    assert router.select_step([{"role": "user", "content": "hi"}], chain=chain) is None

def test_select_step_disabled_or_empty_returns_none(registry_db):
    router = CapabilityRouter(enabled=False, db_path=registry_db)
    assert router.select_step([{"role": "user", "content": "hi"}], chain=[{"provider": "a", "model": "m"}]) is None
    router.enabled = True
    assert router.select_step([{"role": "user", "content": "hi"}], chain=[]) is None


# ── Policy + decisions + matrix invalidation (Phase 3) ───────────────────

def test_effective_policy_from_settings(registry_db, monkeypatch):
    router = CapabilityRouter(enabled=True, db_path=registry_db)

    class FakeSettings:
        def get_routing_policy(self, default="eager"):
            return "cost_first"
        def get_routing_min_score(self, default=0.0):
            return 0.5
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: FakeSettings())
    policy, min_score = router._effective_policy(None)
    assert policy == "cost_first"
    assert min_score == 0.5

def test_effective_policy_config_fallback(registry_db, monkeypatch):
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    # No runtime settings → config.dynamic_routing used.
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: None)

    class _Cfg:
        dynamic_routing = {"policy": "explore", "min_score": 0.4}
    policy, min_score = router._effective_policy(_Cfg())
    assert policy == "explore"
    assert min_score == 0.4

def test_is_enabled_runtime_setting_wins(registry_db, monkeypatch):
    router = CapabilityRouter(enabled=False, db_path=registry_db)

    class FakeSettings:
        def get_routing_enabled(self, default=None):
            return True
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: FakeSettings())
    assert router.is_enabled() is True

def test_is_enabled_falls_back_to_boot_value(registry_db, monkeypatch):
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: None)
    assert CapabilityRouter(enabled=True, db_path=registry_db).is_enabled() is True
    assert CapabilityRouter(enabled=False, db_path=registry_db).is_enabled() is False

def test_select_step_disabled_via_runtime_toggle(registry_db, monkeypatch):
    """select_step honors the runtime disable even when the router boots enabled."""
    router = CapabilityRouter(enabled=True, db_path=registry_db)

    class FakeSettings:
        def get_routing_enabled(self, default=None):
            return False
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: FakeSettings())
    chain = [{"provider": "opencode", "model": "deepseek-v4-pro"},
             {"provider": "deepseek", "model": "deepseek-v4-flash"}]
    assert router.select_step([{"role": "user", "content": "hi"}], chain=chain) is None

def test_select_step_min_score_floor(registry_db, monkeypatch):
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    monkeypatch.setattr(router, "_effective_policy", lambda config: ("eager", 0.99))
    # Best score is ~0.9 → below the 0.99 floor → no reorder + decision recorded.
    monkeypatch.setattr(router, "score_step",
                        lambda step, task, profile=None, config=None, bias=None:
                        0.9 if step["model"] == "deepseek-v4-pro" else 0.6)
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    assert router.select_step([{"role": "user", "content": "hi"}], chain=chain) is None
    assert router.recent_decisions()[-1]["action"] == "below_min_score"

def test_select_step_cost_first_policy(registry_db, monkeypatch):
    """cost_first boosts cheap models so a cheaper-but-close step can win."""
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    monkeypatch.setattr(router, "_effective_policy", lambda config: ("cost_first", 0.0))
    monkeypatch.setattr(router, "score_step", router.score_step)  # real scoring
    chain = [{"provider": "opencode", "model": "deepseek-v4-pro"},
             {"provider": "deepseek", "model": "deepseek-v4-flash"}]
    # Flash is much cheaper; under cost_first it should rank first for a
    # task where the models are close (e.g. casual_chat).
    out = router.select_step(
        [{"role": "user", "content": "hello, how are you?"}], chain=chain)
    assert out is None or out[0]["model"] == "deepseek-v4-flash"

def test_select_step_explore_records_decision(registry_db, monkeypatch):
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    monkeypatch.setattr(router, "_effective_policy", lambda config: ("explore", 0.0))
    monkeypatch.setattr(router, "score_step",
                        lambda step, task, profile=None, config=None, bias=None:
                        0.95 if step["model"] == "deepseek-v4-pro" else 0.93)
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    out = router.select_step([{"role": "user", "content": "hi"}], chain=chain)
    # Either a reorder (explore) or keep_default — both record a decision.
    assert router.recent_decisions()
    if out is not None:
        assert out[0]["model"] == "deepseek-v4-pro"

def test_decision_buffer_bounded(registry_db, monkeypatch):
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    monkeypatch.setattr(router, "_effective_policy", lambda config: ("eager", 0.0))
    monkeypatch.setattr(router, "score_step",
                        lambda step, task, profile=None, config=None, bias=None:
                        0.9 if step["model"] == "deepseek-v4-pro" else 0.6)
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    for _ in range(60):
        router.select_step([{"role": "user", "content": "hi"}], chain=chain)
    assert len(router._decisions) <= 50

def test_invalidate_matrix(registry_db):
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    router.load_matrix()
    assert router._matrix is not None
    router.invalidate_matrix()
    assert router._matrix is None
    # reloads on next access
    assert router.load_matrix() is not None

def test_routing_status(registry_db, monkeypatch):
    from src.api.router import routing_status
    _seed_router(registry_db, enabled=True)
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: None)
    router = get_dynamic_router()
    router._record_decision({"ts": "t", "profile": "l2", "task": "debugging",
                             "policy": "eager", "action": "reorder",
                             "model": "m", "provider": "p", "score": 0.9})
    st = routing_status(None)
    assert st["enabled"] is True
    assert st["policy"] == "eager"
    assert "recent_decisions" in st
    assert st["recent_decisions"][0]["action"] == "reorder"
    _seed_router(enabled=False)  # restore


def test_decisions_persist_across_restart(registry_db):
    """Routing decisions survive a rebuild: a fresh router on the same DB reads
    back the persisted routing_decisions rows."""
    from src.api.models import Base, get_engine, RoutingDecision, get_session

    # Ensure the routing_decisions table exists on the shared registry DB.
    engine = get_engine(registry_db)
    Base.metadata.create_all(engine)

    r1 = CapabilityRouter(enabled=True, db_path=registry_db)
    r1._record_decision({
        "ts": "2026-08-25T20:59:15Z", "profile": "coder", "task": "planning",
        "policy": "eager", "action": "prefer", "provider": "commandcode",
        "model": "deepseek/deepseek-v4-flash", "score": 0.778,
        "rules": ["prefer"], "note": "fired: prefer",
    })

    # Simulate a rebuild: a FRESH router instance on the SAME db_path.
    r2 = CapabilityRouter(enabled=True, db_path=registry_db)
    decs = r2.recent_decisions(25)
    assert any(d["task"] == "planning" and d["action"] == "prefer" for d in decs)
    # The row is actually in the DB table.
    with get_session(engine) as s:
        assert s.query(RoutingDecision).filter(RoutingDecision.task == "planning").count() >= 1


# ── Routing rules (Phase: UI-defined overrides) ─────────────────────────

def _rule_router(registry_db, monkeypatch, rules, config=None):
    router = CapabilityRouter(enabled=True, db_path=registry_db)

    class FakeSettings:
        def get_routing_rules(self):
            return rules
        def get_routing_policy(self, default="eager"):
            return "eager"
        def get_routing_min_score(self, default=0.0):
            return 0.0
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: FakeSettings())
    return router


def _prov_config(providers):
    """A minimal config stub with provider model lists (for prefer expansion)."""
    class _Cfg:
        pass
    cfg = _Cfg()
    cfg.providers = providers
    return cfg


REAL_PROVIDERS = {
    "commandcode": {"models": ["deepseek-v4-pro", "deepseek-v4-flash"]},
    "deepseek": {"models": ["deepseek-v4-pro", "deepseek-v4-flash"]},
    "opencode": {"models": ["deepseek-v4-pro", "deepseek-v4-flash", "ox-alpha-free"]},
}

def test_apply_rules_block_removes_provider(registry_db, monkeypatch):
    router = _rule_router(registry_db, monkeypatch,
                          [{"task": "*", "action": "block", "provider": "opencode"}])
    chain = [{"provider": "opencode", "model": "m1"},
             {"provider": "deepseek", "model": "m2"}]
    candidates, fired = router._apply_rules(chain, "code_generation", "l2")
    assert [s["provider"] for s in candidates] == ["deepseek"]
    assert fired and fired[0]["action"] == "block"

def test_apply_rules_prefer_moves_to_front(registry_db, monkeypatch):
    router = _rule_router(registry_db, monkeypatch,
                          [{"task": "debugging", "action": "prefer",
                            "provider": "deepseek", "model": "deepseek-v4-pro"}])
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    candidates, fired = router._apply_rules(chain, "debugging", "l2")
    assert candidates[0]["provider"] == "deepseek"
    assert fired[0]["action"] == "prefer"
    # Non-matching task → no change.
    candidates2, _ = router._apply_rules(chain, "casual_chat", "l2")
    assert candidates2[0]["provider"] == "opencode"

def test_apply_rules_prefer_min_score_gate(registry_db, monkeypatch):
    router = _rule_router(registry_db, monkeypatch,
                          [{"task": "debugging", "action": "prefer",
                            "provider": "deepseek", "model": "deepseek-v4-pro",
                            "min_score": 0.99}])
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    # deepseek-v4-pro's debugging score is ~0.77 < 0.99 → prefer skipped.
    candidates, fired = router._apply_rules(chain, "debugging", "l2")
    assert candidates[0]["provider"] == "opencode"
    assert fired and fired[0]["action"] == "prefer_skipped_low_score"

def test_apply_rules_model_only_prefer(registry_db, monkeypatch):
    """A rule with only a model (provider '*' wildcard) expands to every chain
    step that uses the model, in chain order. The chain is the source of truth:
    a provider whose chain step uses a different model is NOT expanded."""
    router = _rule_router(registry_db, monkeypatch,
                          [{"task": "*", "action": "prefer",
                            "provider": "*", "model": "deepseek-v4-pro"}])
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    candidates, fired = router._apply_rules(chain, "code_generation", "l2",
                                            _prov_config(REAL_PROVIDERS))
    # Only deepseek's chain step is pro → deepseek leads; opencode (flash) stays.
    assert candidates[0]["model"] == "deepseek-v4-pro"
    assert candidates[0]["provider"] == "deepseek"
    assert fired and fired[0]["action"] == "prefer"

def test_apply_rules_model_only_block(registry_db, monkeypatch):
    """A block rule with only a model removes it from every provider."""
    router = _rule_router(registry_db, monkeypatch,
                          [{"task": "*", "action": "block",
                            "provider": "*", "model": "deepseek-v4-flash"}])
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    candidates, fired = router._apply_rules(chain, "code_generation", "l2")
    assert [s["model"] for s in candidates] == ["deepseek-v4-pro"]
    assert fired and fired[0]["action"] == "block"

def test_apply_rules_both_wildcards_matches_nothing(registry_db, monkeypatch):
    """provider='*' AND model='*' has no concrete target → no-op."""
    router = _rule_router(registry_db, monkeypatch,
                          [{"task": "*", "action": "prefer",
                            "provider": "*", "model": "*"}])
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    candidates, fired = router._apply_rules(chain, "code_generation", "l2")
    assert candidates[0]["provider"] == "opencode"
    assert not fired

def test_rule_target_normalizes_provider_side_model_id(registry_db, monkeypatch):
    """A rule written with the logical model name matches a chain step whose
    model is a provider-side ID (commandcode: deepseek/deepseek-v4-pro)."""
    router = _rule_router(registry_db, monkeypatch, [])
    rule = {"provider": "*", "model": "deepseek-v4-pro"}
    step = {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"}
    assert router._rule_target(rule, step) is True

def test_apply_rules_prefer_first_match_wins(registry_db, monkeypatch):
    """The first matching prefer (in rule order) wins; later prefers don't
    override it."""
    router = _rule_router(registry_db, monkeypatch, [
        {"task": "*", "action": "prefer", "provider": "*", "model": "deepseek-v4-pro"},
        {"task": "*", "action": "prefer", "provider": "*", "model": "deepseek-v4-flash"},
    ])
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    candidates, fired = router._apply_rules(chain, "code_generation", "l2")
    assert candidates[0]["model"] == "deepseek-v4-pro"
    assert [f["action"] for f in fired] == ["prefer"]

def test_apply_rules_prefer_expands_across_providers(registry_db, monkeypatch):
    """A prefer rule expands the preferred model to EVERY chain step that uses
    it, in chain order, before the chain's other models. So a degraded provider
    falls to the NEXT provider of the SAME model — not to a cheaper one. The
    chain is the source of truth: a provider whose chain step uses a different
    model (deepseek/flash here) is NOT expanded."""
    router = _rule_router(registry_db, monkeypatch,
                          [{"task": "*", "action": "prefer",
                            "provider": "*", "model": "deepseek-v4-pro"}])
    chain = [
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"},
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "opencode", "model": "deepseek-v4-pro"},
        {"provider": "opencode", "model": "ox-alpha-free"},
    ]
    candidates, fired = router._apply_rules(chain, "planning", "coder",
                                            _prov_config(REAL_PROVIDERS))
    models = [c["model"] for c in candidates]
    # commandcode-pro, opencode-pro (both pro chain steps), then fallbacks.
    assert models == ["deepseek/deepseek-v4-pro", "deepseek-v4-pro",
                      "deepseek-v4-flash", "ox-alpha-free"]
    assert [c["provider"] for c in candidates] == [
        "commandcode", "opencode", "deepseek", "opencode",
    ]
    assert fired[0]["action"] == "prefer"
    assert fired[0].get("steps") == 2

def test_select_step_prefer_is_mandatory(registry_db, monkeypatch):
    """A fired prefer expands the preferred model across chain steps that use
    it: even if scoring favors flash, the router returns the preferred model
    first, tried on every chain step that uses it, before any other model."""
    rules = [{"task": "planning", "profile": "*", "action": "prefer",
              "provider": "*", "model": "deepseek-v4-pro"}]
    router = _rule_router(registry_db, monkeypatch, rules)
    monkeypatch.setattr(router, "_effective_policy", lambda config: ("eager", 0.0))
    # Flash would outscore pro on planning — prefer must still win.
    monkeypatch.setattr(router, "score_step",
                        lambda step, task, profile=None, config=None, bias=None:
                        0.9 if step["model"] == "deepseek-v4-flash" else 0.7)
    chain = [{"provider": "deepseek", "model": "deepseek-v4-flash"},
             {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"}]
    cfg = _prov_config(REAL_PROVIDERS)
    out = router.select_step([{"role": "user", "content": "design the architecture"}],
                             chain=chain, profile="coder", config=cfg)
    assert out is not None
    # pro on commandcode (the only pro chain step), then flash fallback.
    assert out[0]["provider"] == "commandcode"
    assert out[0]["model"] == "deepseek/deepseek-v4-pro"
    assert out[1]["provider"] == "deepseek"
    assert out[1]["model"] == "deepseek-v4-flash"
    dec = router.recent_decisions()[-1]
    assert dec["action"] == "prefer"
    assert dec["rules"] == ["prefer"]
    assert dec["task"] == "planning"

def test_select_step_prefer_gate_skip_still_scores(registry_db, monkeypatch):
    """When a prefer is skipped by its min_score gate (no prefer fires), the
    router falls through to normal scoring."""
    rules = [{"task": "planning", "profile": "*", "action": "prefer",
              "provider": "*", "model": "deepseek-v4-pro", "min_score": 0.99}]
    router = _rule_router(registry_db, monkeypatch, rules)
    monkeypatch.setattr(router, "_effective_policy", lambda config: ("eager", 0.0))
    monkeypatch.setattr(router, "score_step",
                        lambda step, task, profile=None, config=None, bias=None:
                        0.9 if step["model"] == "deepseek-v4-flash" else 0.7)
    chain = [{"provider": "deepseek", "model": "deepseek-v4-flash"},
             {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"}]
    out = router.select_step([{"role": "user", "content": "design the architecture"}],
                             chain=chain, profile="coder")
    # No prefer fired → eager keeps the default (flash is already first/highest).
    # The router returns None to keep the chain order.
    assert out is None
    dec = router.recent_decisions()[-1]
    assert dec["rules"] == ["prefer_skipped_low_score"]
    assert dec["action"] == "keep_default"

def test_select_step_agentic_system_prompt_still_fires_planning_prefer(registry_db, monkeypatch):
    """Regression: an agentic system prompt must not mask a planning request —
    the planning prefer rule must fire and pin pro even though the system
    prompt is agentic and flash scores higher on planning."""
    rules = [{"task": "planning", "profile": "*", "action": "prefer",
              "provider": "*", "model": "deepseek-v4-pro"}]
    router = _rule_router(registry_db, monkeypatch, rules)
    monkeypatch.setattr(router, "_effective_policy", lambda config: ("eager", 0.0))
    # Flash out-scores pro on planning — prefer must still win.
    monkeypatch.setattr(router, "score_step",
                        lambda step, task, profile=None, config=None, bias=None:
                        0.9 if step["model"] == "deepseek-v4-flash" else 0.7)
    chain = [{"provider": "deepseek", "model": "deepseek-v4-flash"},
             {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"}]
    msgs = [
        {"role": "system", "content": "You are an AI agent with tools: read_file, write_file, terminal"},
        {"role": "user", "content": "design the architecture for a microservice"},
    ]
    out = router.select_step(msgs, chain=chain, profile="coder",
                             config=_prov_config(REAL_PROVIDERS))
    assert out is not None
    # Preferred model pinned: commandcode's pro chain step leads (not flash).
    from src.api.router import logical_model_name
    assert logical_model_name(out[0]["model"], registry_db) == "deepseek-v4-pro"
    dec = router.recent_decisions()[-1]
    assert dec["task"] == "planning"
    assert dec["action"] == "prefer"
    assert dec["rules"] == ["prefer"]

def test_select_step_filters_unavailable_provider(registry_db, monkeypatch):
    """The circuit breaker only gates PROVIDERS: when a provider is unavailable,
    the router drops its steps entirely, so the ordering never proposes it — and
    a prefer rule still expands the preferred model across the remaining chain
    steps that use it."""
    rules = [{"task": "planning", "profile": "*", "action": "prefer",
              "provider": "*", "model": "deepseek-v4-pro"}]
    router = _rule_router(registry_db, monkeypatch, rules)
    monkeypatch.setattr(router, "_effective_policy", lambda config: ("eager", 0.0))
    # commandcode unavailable → its pro step is dropped; opencode pro survives.
    monkeypatch.setattr(router, "_provider_available",
                        lambda step, profile=None, config=None:
                        step["provider"] != "commandcode")
    chain = [
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"},
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "opencode", "model": "deepseek-v4-pro"},
    ]
    msgs = [{"role": "user", "content": "design the architecture"}]
    out = router.select_step(msgs, chain=chain, profile="coder",
                             config=_prov_config(REAL_PROVIDERS))
    assert out is not None
    # commandcode dropped; pro expanded to opencode (the only pro chain step),
    # then flash fallback.
    assert [s["provider"] for s in out] == ["opencode", "deepseek"]
    assert [s["model"] for s in out] == ["deepseek-v4-pro", "deepseek-v4-flash"]
    assert router.recent_decisions()[-1]["action"] == "prefer"

def test_select_step_audit_note_when_rule_matches_scope_but_not_fired(registry_db, monkeypatch):
    """When a rule matches the task scope but no action fires (e.g. the only
    matching rule is a policy override), the decision carries an audit note."""
    rules = [{"profile": "cron", "action": "policy", "policy": "cost_first"}]
    router = _rule_router(registry_db, monkeypatch, rules)
    monkeypatch.setattr(router, "_effective_policy", lambda config: ("eager", 0.0))
    monkeypatch.setattr(router, "score_step",
                        lambda step, task, profile=None, config=None, bias=None:
                        0.9 if step["model"] == "deepseek-v4-pro" else 0.6)
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    # cron profile → policy rule flips policy but doesn't "fire" a prefer/block.
    router.select_step([{"role": "user", "content": "hi"}], chain=chain, profile="cron")
    dec = router.recent_decisions()[-1]
    assert dec["note"] is not None
    assert "matched scope" in dec["note"]

def test_select_step_policy_rule_override(registry_db, monkeypatch):
    rules = [{"profile": "cron", "action": "policy", "policy": "cost_first"}]
    router = _rule_router(registry_db, monkeypatch, rules)
    monkeypatch.setattr(router, "_effective_policy", lambda config: ("eager", 0.0))
    monkeypatch.setattr(router, "score_step",
                        lambda step, task, profile=None, config=None, bias=None:
                        0.9 if step["model"] == "deepseek-v4-pro" else 0.6)
    chain = [{"provider": "opencode", "model": "deepseek-v4-flash"},
             {"provider": "deepseek", "model": "deepseek-v4-pro"}]
    # cron profile → policy rule flips to cost_first (bias > 0) but the outcome
    # is a reorder regardless; assert the decision recorded the policy.
    router.select_step([{"role": "user", "content": "hi"}], chain=chain, profile="cron")
    assert router.recent_decisions()[-1]["policy"] == "cost_first"

def test_routing_status_includes_rules(registry_db, monkeypatch):
    from src.api.router import routing_status
    rules = [{"task": "debugging", "action": "prefer", "provider": "deepseek"}]
    _seed_router(registry_db, enabled=True)
    try:
        _rule_router(registry_db, monkeypatch, rules)
        st = routing_status(None)
        assert st["rules"] == rules
    finally:
        _seed_router(enabled=False)

# ── Per-profile routing (enable/policy/min_score/rules overrides) ─────────

class _PerProfileSettings:
    """A duck-typed settings store that supports per-profile routing keys."""

    def __init__(self, store):
        self._store = store

    def get_routing_enabled(self, default=None, profile=None):
        return self._store.get_routing_enabled(default=default, profile=profile)

    def get_routing_policy(self, default="eager", profile=None):
        return self._store.get_routing_policy(default=default, profile=profile)

    def get_routing_min_score(self, default=0.0, profile=None):
        return self._store.get_routing_min_score(default=default, profile=profile)

    def get_routing_rules(self, default=None, profile=None):
        return self._store.get_routing_rules(default=default, profile=profile)


def test_per_profile_enabled_wins_over_global(registry_db, monkeypatch):
    import tempfile, os
    from src.api.cost_cache import SettingsStore
    from src.api.models import get_engine, Base
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    try:
        store = SettingsStore(engine)
        store.set_routing_enabled(False)            # global off
        store.set_routing_enabled(True, profile="l2")  # l2 on
        monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: store)

        router = CapabilityRouter(enabled=True, db_path=registry_db)
        assert router.is_enabled(None) is False          # global off
        assert router.is_enabled(None, "l2") is True     # l2 override on
        assert router.is_enabled(None, "career") is False  # falls back to global
    finally:
        engine.dispose()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass


def test_per_profile_policy_and_rules(registry_db, monkeypatch):
    import tempfile, os
    from src.api.cost_cache import SettingsStore
    from src.api.models import get_engine, Base
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    try:
        store = SettingsStore(engine)
        store.set_routing_policy("cost_first")                     # global
        store.set_routing_policy("explore", profile="l2")          # l2 override
        store.set_routing_rules([{"task": "planning", "action": "prefer",
                                  "model": "deepseek-v4-pro"}], profile="l2")
        monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: store)

        router = CapabilityRouter(enabled=True, db_path=registry_db)
        pol, ms = router._effective_policy(None, "l2")
        assert pol == "explore"
        pol_g, _ = router._effective_policy(None)
        assert pol_g == "cost_first"
        assert len(router._rules(None, "l2")) == 1
        assert router._rules(None) == []  # global rules untouched
    finally:
        engine.dispose()
        for ext in ("", "-wal", "-shm"):
            try:
                os.unlink(path + ext)
            except FileNotFoundError:
                pass


def test_routing_status_includes_per_profile(registry_db, monkeypatch):
    from src.api.router import routing_status, _status_for_profile
    _seed_router(registry_db, enabled=True)
    monkeypatch.setattr("src.api.cost_cache.get_settings", lambda: None)
    try:
        class _Cfg:
            profiles = {"l2": {"chain": [{"provider": "deepseek", "model": "deepseek-v4-pro"}]},
                        "career": {"chain": [{"provider": "deepseek", "model": "deepseek-v4-flash"}]}}
            dynamic_routing = {}
        st = routing_status(_Cfg())
        assert "per_profile" in st
        assert set(st["per_profile"].keys()) == {"l2", "career"}
        assert st["per_profile"]["l2"]["enabled"] is True
        # _status_for_profile returns the same shape for a direct profile query.
        router = get_dynamic_router()
        block = _status_for_profile(router, _Cfg(), "l2")
        assert block["enabled"] is True
        assert "rules" in block
    finally:
        _seed_router(enabled=False)


def test_routing_status_restricts_to_selected_models(registry_db, monkeypatch):
    """Per-task recommendations only include models referenced by a chain."""
    from src.api.seed_capabilities import seed_livebench
    from src.api.router import routing_status
    seed_livebench(registry_db)
    _seed_router(registry_db, enabled=True)
    try:
        # Chain selects ONLY deepseek-v4-flash — recommendations must not
        # mention models like gpt-5.6-sol / claude-fable-5 that aren't selected.
        class _Cfg:
            profiles = {"l2": {"chain": [
                {"provider": "deepseek", "model": "deepseek-v4-flash"},
            ]}}
            dynamic_routing = {}
        st = routing_status(_Cfg())
        for task, rec in st["per_task"].items():
            assert rec["model"] == "deepseek-v4-flash", f"{task} -> {rec['model']} (not selected)"
    finally:
        _seed_router(enabled=False)

def test_routing_status_falls_back_without_config(registry_db, monkeypatch):
    """Without config (tests), the top model per task is still shown."""
    from src.api.seed_capabilities import seed_livebench
    from src.api.router import routing_status
    seed_livebench(registry_db)
    _seed_router(registry_db, enabled=True)
    try:
        st = routing_status(None)
        assert st["per_task"], "expected per_task populated without config"
        # The top overall model appears for at least one task.
        assert any(rec["model"] for rec in st["per_task"].values())
    finally:
        _seed_router(enabled=False)


# ── Model registry tests (explicit alias → logical → benchmark) ───────────

def test_logical_model_name_maps_provider_alias(registry_db):
    assert logical_model_name("deepseek/deepseek-v4-pro", registry_db) == "deepseek-v4-pro"
    assert logical_model_name("moonshotai/Kimi-K3", registry_db) == "kimi-k3"
    assert logical_model_name("Qwen/Qwen3.8-Max", registry_db) == "qwen3.8-max"

def test_logical_model_name_passthrough_unknown(registry_db):
    assert logical_model_name("some-brand-new-model", registry_db) == "some-brand-new-model"

def test_logical_model_name_case_insensitive(registry_db):
    assert logical_model_name("DeepSeek-V4-Pro", registry_db) == "deepseek-v4-pro"

def test_benchmark_model_name_resolves_rolling_alias(registry_db):
    # The benchmark key is the stable logical name; the dated 0731 snapshot
    # is a RELEASE of it, not a separate identity.
    assert benchmark_model_name("deepseek-v4-flash", registry_db) == "deepseek-v4-flash"

def test_benchmark_model_name_passthrough_without_pin(registry_db):
    # deepseek-v4-pro has no dated snapshot in the registry
    assert benchmark_model_name("deepseek-v4-pro", registry_db) == "deepseek-v4-pro"

def test_registry_logical_names_unique():
    names = [entry["logical_name"].lower() for entry in DEFAULT_MODEL_REGISTRY]
    assert len(names) == len(set(names)), "logical names duplicated in registry"


# ── Model-ID normalization + quantization detection ────────────────────────

def test_normalize_model_id_strips_models_prefix_and_gguf():
    assert normalize_model_id("/models/qwen3.6-27b-q4_k_m.gguf") == "qwen3.6-27b-q4_k_m"
    assert normalize_model_id("/models/deepseek-v4-flash.gguf") == "deepseek-v4-flash"
    assert normalize_model_id("deepseek-v4-pro") == "deepseek-v4-pro"

def test_normalize_model_id_strips_leading_slash_and_path():
    assert normalize_model_id("/qwen3.6-27b-q4_k_m.gguf") == "qwen3.6-27b-q4_k_m"
    assert normalize_model_id("moonshotai/Kimi-K3") == "kimi-k3"

def test_normalize_model_id_lowercases_and_trims():
    assert normalize_model_id("  DeepSeek-V4-Pro ") == "deepseek-v4-pro"
    assert normalize_model_id("Qwen/Qwen3.8-Max") == "qwen3.8-max"

def test_normalize_model_id_empty():
    assert normalize_model_id("") == ""
    assert normalize_model_id(None) is None

def test_detect_quantization_gguf_tags():
    assert detect_quantization("/models/qwen3.6-27b-q4_k_m.gguf") == "Q4_K_M"
    assert detect_quantization("llama-3.1-8b-instruct-q8_0") == "Q8_0"
    assert detect_quantization("qwen3.6-27b-q4_0") == "Q4_0"
    assert detect_quantization("model-f16") == "F16"

def test_detect_quantization_none_for_regular_models():
    assert detect_quantization("deepseek-v4-pro") is None
    assert detect_quantization("qwen3.8-max") is None
    assert detect_quantization("gpt-5.6-sol") is None
    assert detect_quantization("kimi-k3") is None

def test_logical_model_name_normalizes_llamacpp_path(registry_db):
    # An unregistered llama.cpp path is normalized even without a registry entry.
    assert logical_model_name("/models/qwen3.6-27b-q4_k_m.gguf", registry_db) == "qwen3.6-27b-q4_k_m"


def test_load_model_registry_includes_quantization(registry_db):
    from src.api.seed_capabilities import load_model_registry
    # Insert a quantized entry and re-read.
    from src.api.models import ModelRegistryEntry, get_engine, get_session
    import json
    engine = get_engine(registry_db)
    with get_session(engine) as session:
        session.add(ModelRegistryEntry(
            logical_name="qwen3.6-27b-q4_k_m",
            benchmark_key="qwen3.6-27b-q4_k_m",
            provider_mappings_json=json.dumps({"llamacpp": "/models/qwen3.6-27b-q4_k_m.gguf"}),
            quantization="Q4_K_M",
        ))
        session.commit()
    registry = load_model_registry(registry_db)
    entry = registry["qwen3.6-27b-q4_k_m"]
    assert entry["quantization"] == "Q4_K_M"
    assert entry["provider_mappings"]["llamacpp"] == "/models/qwen3.6-27b-q4_k_m.gguf"


# ── Provider → model mappings ─────────────────────────────────────────────

def test_provider_model_name_explicit_mapping(registry_db):
    from src.api.router import provider_model_name
    # Command Code uses catalog IDs; registry pins the exact mapping.
    assert provider_model_name("deepseek-v4-pro", "commandcode", registry_db) == "deepseek/deepseek-v4-pro"
    # OpenCode/DeepSeek use bare names.
    assert provider_model_name("deepseek-v4-pro", "opencode", registry_db) == "deepseek-v4-pro"

def test_provider_model_name_falls_back_to_logical(registry_db):
    from src.api.router import provider_model_name
    # Kimi: explicit mapping supplies the prefixed catalog ID.
    assert provider_model_name("kimi-k3", "commandcode", registry_db) == "moonshotai/Kimi-K3"

def test_provider_model_name_unknown_provider_passthrough(registry_db):
    from src.api.router import provider_model_name
    assert provider_model_name("deepseek-v4-pro", "someprovider", registry_db) == "deepseek-v4-pro"


# ── CapabilityRouter selection edge paths ────────────────────────────────────

def test_router_load_matrix_error_returns_empty(tmp_path):
    from src.api.router import CapabilityRouter
    router = CapabilityRouter(enabled=True, db_path=str(tmp_path / "missing.db"))
    # Missing DB → load fails gracefully to {}.
    matrix = router.load_matrix()
    assert matrix == {}

def test_init_router_warm_cache(registry_db):
    _seed_router(db_path=registry_db, enabled=True)
    assert router_mod.get_dynamic_router().enabled is True
    # Restore for other tests.
    _seed_router(db_path=registry_db, enabled=False)


# ── Observability + judgment: classify_task_detail, summarize, rationale ────

def test_classify_task_detail_semantic_debugging():
    from src.api.router import classify_task_detail
    msgs = [{"role": "user", "content": "debug why this returns a TypeError"}]
    detail = classify_task_detail(msgs)
    assert detail.task == "debugging"
    assert detail.path == "semantic"
    assert detail.keyword is None
    assert "TypeError" in detail.intent_text
    assert detail.intent_meta is not None


def test_classify_task_detail_default_path(monkeypatch):
    # Deterministic: disable the semantic path so we exercise the fallback
    # chain (with the real embedder installed, semantics usually win).
    from src.api.router import classify_task_detail
    from src.api import task_classifier as tc
    monkeypatch.setattr(tc, "get_semantic_classifier", lambda: None)
    msgs = [{"role": "user", "content": "review the current state of things in this workspace"}]
    detail = classify_task_detail(msgs)
    assert detail.task == "code_generation"
    assert detail.path == "default"
    assert detail.semantic is None
    assert detail.sem_available is False


def test_classify_task_detail_agentic_prompt_path():
    from src.api.router import classify_task_detail
    msgs = [{"role": "system",
             "content": "You are an AI agent with tools: read_file, write_file, terminal, patch, bash, curl"}]
    detail = classify_task_detail(msgs)
    assert detail.task == "agentic_multi_step"
    assert detail.path == "agentic_prompt"


def test_classify_task_detail_semantic_path(monkeypatch):
    from src.api.router import classify_task_detail
    from src.api import task_classifier as tc

    class FakeClf:
        min_score = 0.3
        def top_scores(self, text, k=5):
            return [("unit_tests", 0.9), ("code_generation", 0.4)]

    monkeypatch.setattr(tc, "get_semantic_classifier", lambda: FakeClf())
    detail = classify_task_detail([{"role": "user", "content": "zzz qqq some tests"}])
    assert detail.task == "unit_tests"
    assert detail.path == "semantic"
    assert detail.sem_available is True
    assert detail.min_score == 0.3
    assert detail.semantic == [("unit_tests", 0.9), ("code_generation", 0.4)]


def test_classify_task_is_thin_wrapper():
    from src.api.router import classify_task_detail
    msgs = [{"role": "user", "content": "debug this traceback please"}]
    assert classify_task(msgs) == classify_task_detail(msgs).task


# ── Attachment / client-context must not hijack routing ─────────────────────
# Regression (L2): a message wrapped in <attachments>/<context>/<userRequest>
# (VS Code sends these as role=user) used to be discarded as "client context",
# then the keyword stage fell back to the ENTIRE raw user text — so incidental
# words inside the attached document ("error", "pytest") hijacked routing.

def _attachment_doc():
    """A plausible attached doc containing the debugging/unit_tests trigger
    words that previously hijacked classification via the user_text fallback."""
    return ("# Component Runtime\n\nThis proposal covers the gateway lifecycle. "
            "A collision on name or provides-key is an error. We run pytest "
            "for the suite.\n" * 5)


def test_classify_attachment_hijack_uses_real_instruction():
    """A planning request with an attached document must be routed by the REAL
    instruction, not by incidental words in the attachment."""
    from src.api.router import classify_task_detail
    msgs = [
        {"role": "system", "content": "You are an expert AI programming assistant."},
        {"role": "user", "content": (
            "<attachments>\n<attachment id=\"component-runtime.md\" "
            "filePath=\"/x/component-runtime.md\">\n" + _attachment_doc() +
            "\n</attachment>\n</attachments>\n"
            "<context>The current date is 2026-08-27.</context>\n"
            "<editorContext>the user's current file is ...</editorContext>\n"
            "<reminderInstructions>...</reminderInstructions>\n"
            "<userRequest>let's plan to implement component runtime feature</userRequest>"
        )},
    ]
    detail = classify_task_detail(msgs)
    # Not hijacked by the attachment's "error"/"pytest": the genuine instruction
    # ("let's plan to implement...") drives the SEMANTIC path to planning.
    assert detail.task == "planning"
    assert detail.path == "semantic"
    assert "implement" in detail.intent_text
    assert "error" not in detail.intent_text.lower()


def test_classify_attachment_only_does_not_hijack():
    """An attachment-only user message (no instruction) must not leak its content
    into routing; a separate real instruction is what gets classified."""
    from src.api.router import classify_task_detail
    msgs = [
        {"role": "system", "content": "You are an expert AI programming assistant."},
        {"role": "user", "content": (
            "<attachments><attachment id=\"x.md\" filePath=\"/x/x.md\">"
            + _attachment_doc() + "</attachment></attachments>"
        )},
        {"role": "user", "content": "write a function to parse csv"},
    ]
    detail = classify_task_detail(msgs)
    assert detail.task == "code_generation"
    assert detail.intent_text == "write a function to parse csv"


def test_classify_attachment_casual_words_do_not_route_casual(monkeypatch):
    """Casual words inside an attached document must not trigger casual_chat;
    only genuine user text is scanned. Semantic disabled for determinism."""
    from src.api.router import classify_task_detail
    from src.api import task_classifier as tc
    monkeypatch.setattr(tc, "get_semantic_classifier", lambda: None)
    msgs = [
        {"role": "system", "content": "You are an expert AI programming assistant."},
        {"role": "user", "content": (
            "<attachments><attachment id=\"x.md\" filePath=\"/x/x.md\">"
            "hey hello how are you thanks for the great doc\n" * 5 +
            "</attachment></attachments>"
            "<userRequest>update the dashboard</userRequest>"
        )},
    ]
    detail = classify_task_detail(msgs)
    assert detail.task != "casual_chat"
    assert detail.path != "casual"
    assert detail.intent_text == "update the dashboard"


def test_context_tail_extracts_real_instruction_from_wrapper():
    """_context_tail unwraps the genuine instruction out of an attachments/
    userRequest wrapper (and returns None for wrapper-only messages)."""
    from src.api.router import _context_tail
    assert _context_tail(
        "<attachments><attachment id=\"x\" filePath=\"/x\">body</attachment>"
        "</attachments><userRequest>debug the failing test</userRequest>"
    ) == "debug the failing test"
    # Attachment-only (no instruction) → None, so the walk keeps going back.
    assert _context_tail(
        "<attachments><attachment id=\"x\" filePath=\"/x\">body</attachment></attachments>"
    ) is None
    # Reply-quote wrapper keeps working.
    assert _context_tail(
        '[Replying to: "quoted text"]\nnow do the real thing'
    ) == "now do the real thing"


def test_extract_intent_text_attachment_with_real_instruction():
    """The intent walk returns the real instruction from a client-context
    wrapper instead of discarding the whole message."""
    from src.api.router import _extract_intent_text
    msgs = [
        {"role": "system", "content": "You are an expert AI programming assistant."},
        {"role": "user", "content": (
            "<attachments><attachment id=\"x.md\" filePath=\"/x/x.md\">"
            + _attachment_doc() + "</attachment></attachments>"
            "<userRequest>implement the sorting module</userRequest>"
        )},
    ]
    intent, meta = _extract_intent_text(msgs)
    assert intent == "implement the sorting module"
    assert meta["source"] == "last_instruction"


def test_classify_strips_chat_mention_file_token():
    """A VS Code @file: mention token (client artifact) must not skew semantic
    classification. 'let's now plan for the @file:component-runtime.md feature'
    was tipping to code_generation because of the 'file'/'component' tokens;
    with the mention stripped it must classify as planning."""
    from src.api.router import classify_task_detail, _extract_intent_text, _strip_chat_mentions
    msgs = [
        {"role": "system", "content": "You are an expert AI programming assistant."},
        {"role": "user", "content": (
            "<attachments><attachment id=\"component-runtime.md\" filePath=\"/x/component-runtime.md\">"
            + _attachment_doc() + "</attachment></attachments>"
            "<context>today</context>"
            "<userRequest>let's now plan for the @file:component-runtime.md feature</userRequest>"
        )},
    ]
    intent, _ = _extract_intent_text(msgs)
    assert "@file:component-runtime.md" not in intent
    detail = classify_task_detail(msgs)
    assert detail.task == "planning"
    assert detail.path == "semantic"
    # Emails are not @mention tokens (no colon after the local part) — untouched.
    assert _strip_chat_mentions("mail foo@bar.com please") == "mail foo@bar.com please"


def test_classify_skips_terminal_output_echo():
    """VS Code 'Terminal output: ...' echoes (sent as role=user) are command
    output, not user intent — the walk skips them and finds the real
    instruction, so error-ish terminal text can't classify as debugging."""
    from src.api.router import _extract_intent_text, _is_tool_result
    term = "Terminal output: bash: warning: setlocale: LC_ALL: cannot change locale (en_US.UTF-8)"
    assert _is_tool_result({"role": "user", "content": term}, [], 0)
    msgs = [
        {"role": "user", "content": "please plan the component runtime feature"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": term},
    ]
    intent, meta = _extract_intent_text(msgs)
    assert intent == "please plan the component runtime feature"
    assert meta["skipped_tool"] == 1


def test_classify_strips_attachment_paste_ref():
    """VS Code '#attachment:Pasted text #1' paste references are client
    artifacts — stripping them keeps the real instruction (and its semantic
    classification) intact."""
    from src.api.router import _strip_chat_mentions
    assert (_strip_chat_mentions("it seems its still the same #attachment:Pasted text #1")
            == "it seems its still the same")
    # A bare '#attachment' with no colon is NOT a reference — left alone.
    assert _strip_chat_mentions("the #attachment file") == "the #attachment file"


def test_context_tail_unknown_client_tag_still_stripped():
    """Wrapper detection is STRUCTURAL — a brand-new client tag that is NOT in
    any name list must still be recognized and stripped (no hardcoding)."""
    from src.api.router import _context_tail, _is_client_context
    # A hypothetical new client wraps metadata in <fooBar> and the real request
    # in <userRequest>. Neither name is enumerated anywhere in the code.
    wrapper = (
        "<fooBar>some injected metadata we've never seen before</fooBar>"
        "<userRequest>fix the login bug</userRequest>"
    )
    assert _is_client_context(wrapper)
    assert _context_tail(wrapper) == "fix the login bug"
    # Unknown tag WITHOUT a userRequest container, followed by free text.
    tail = "<fooBar>metadata</fooBar> please refactor the router"
    assert _context_tail(tail) == "please refactor the router"


def test_context_tail_truncated_wrapper_body_is_not_intent():
    """A truncated wrapper (unclosed tag) must not leak its body as the
    instruction — the walk continues back to a real user message."""
    from src.api.router import _context_tail
    assert _context_tail("<attachments> <attachment id='X'> zzzzzzzzzzz") is None


def test_summarize_conversation_preserves_tool_shape():
    from src.api.router import _summarize_conversation
    msgs = [
        {"role": "system", "content": "You are a coding agent. tools: terminal, patch"},
        {"role": "user", "content": "debug this failing test"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "run_tests", "arguments": '{"cmd": "pytest"}'}}]},
        {"role": "user", "content": "[tool result] Ran 12 tests, 3 failed. " + "x" * 500,
         "tool_call_id": "call_1"},
    ]
    s = _summarize_conversation(msgs)
    assert [m["role"] for m in s] == ["system", "user", "assistant", "user"]
    assert s[2]["tool_calls"][0]["id"] == "call_1"
    assert s[2]["tool_calls"][0]["function"]["name"] == "run_tests"
    assert s[3]["tool_call_id"] == "call_1"
    # Trimmed from the END keeps the leading tool-result marker.
    assert s[3]["content"].startswith("[tool result]")
    assert "… [" in s[3]["content"] and "chars omitted]" in s[3]["content"]


def test_summarize_conversation_roundtrip_intent():
    from src.api.router import _summarize_conversation, _extract_intent_text
    msgs = [
        {"role": "system", "content": "You are a coding agent. tools: terminal"},
        {"role": "user", "content": "debug this error in the code"},
        {"role": "assistant", "content": "Let me reproduce.",
         "tool_calls": [{"id": "call_9", "type": "function", "function": {"name": "run", "arguments": "{}"}}]},
        {"role": "user", "content": "[tool result] Ran 12 tests, 3 failed", "tool_call_id": "call_9"},
    ]
    summarized = _summarize_conversation(msgs)
    assert _extract_intent_text(msgs) == _extract_intent_text(summarized)


def test_summarize_conversation_drops_oldest_when_huge():
    import json
    from src.api.router import _summarize_conversation
    msgs = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "y" * 2000},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "write pytest tests now"},
    ]
    s = _summarize_conversation(msgs, max_content=50, max_total=800)
    blob = json.dumps(s)
    assert "pytest tests" in blob  # newest intent survives
    assert any(isinstance(m.get("content"), str) and "omitted" in m["content"] for m in s)


def test_summarize_preserves_trailing_user_request_instruction():
    """Regression (judge replay NOINTENT): a client-context wrapper carries the
    real instruction at the END (<userRequest>...</userRequest>). Trimming from
    the START (default) would cut it off and replay would find no intent; the
    wrapper message must keep its tail so the stored summary still replays to
    the genuine instruction (planning)."""
    from src.api.router import _summarize_conversation, _extract_intent_text, classify_task_detail
    doc = "# Component Runtime\n\n" + "x" * 400  # long attachment body
    user_msg = (
        "<attachments>\n<attachment id=\"component-runtime.md\" filePath=\"/x/component-runtime.md\">\n"
        + doc + "\n</attachment>\n</attachments>\n"
        "<context>The current date is 2026-08-28.</context>\n"
        "<userRequest>let's now plan for the @file:component-runtime.md feature</userRequest>"
    )
    msgs = [
        {"role": "system", "content": "You are an expert AI programming assistant."},
        {"role": "user", "content": user_msg},
    ]
    summarized = _summarize_conversation(msgs, max_content=200, max_total=4000)
    # The trailing instruction survives the trim.
    assert "<userRequest>let's now plan" in summarized[1]["content"]
    # Replaying the summary recovers the same genuine instruction.
    assert _extract_intent_text(summarized)[0] == _extract_intent_text(msgs)[0]
    # And (with the embedder active) it classifies as planning, not NOINTENT.
    detail = classify_task_detail(summarized)
    assert detail.task == "planning"
    assert detail.path == "semantic"
    assert detail.intent_text == "let's now plan for the  feature"


def test_summarize_preserves_leading_user_request_instruction():
    """Regression (judge replay NOINTENT storm): the real VS Code wrapper has
    <userRequest> at the START, followed by long trailing <context>/
    <editorContext>/<reminderInstructions> blocks. Trimming to keep a physical
    END preserved the trailing context and dropped the instruction, so replay
    found no intent. The summary must canonicalize to JUST the instruction."""
    from src.api.router import _summarize_conversation, _extract_intent_text, classify_task_detail
    trailing = (
        "<context>The current date is 2026-08-29.\nTerminals: bash\n</context>\n"
        "<editorContext>The user's current file is /x/y.py.\n</editorContext>\n"
        "<reminderInstructions>When using replace_string_in_file...\n</reminderInstructions>"
    )
    user_msg = "<userRequest>Start implementation</userRequest>\n\n" + trailing * 20
    msgs = [
        {"role": "system", "content": "You are an expert AI programming assistant."},
        {"role": "user", "content": user_msg},
    ]
    summarized = _summarize_conversation(msgs, max_content=200, max_total=4000)
    # Only the instruction survives — the trailing context is dropped entirely.
    assert summarized[1]["content"] == "<userRequest>Start implementation</userRequest>"
    # Replaying recovers the instruction and classifies it as CODE GENERATION
    # (the plan→implement handoff fix), not planning and not NOINTENT.
    assert _extract_intent_text(summarized)[0] == "Start implementation"
    detail = classify_task_detail(summarized)
    assert detail.task == "code_generation"
    assert detail.path == "semantic"
    assert detail.intent_text == "Start implementation"


def test_record_decision_persists_rationale_without_the_transcript(registry_db):
    """M7: the rationale survives; the transcript capture does not.

    `conversation_json` held a trimmed copy of the request messages and was
    101 MB of a 139 MB DB (73%), duplicating the harness's own session stores.
    `intent_text` is what carries the driver, and it is what the check asserts.
    """
    from src.api.router import CapabilityRouter, classify_task_detail
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    msgs = [{"role": "user", "content": "debug this traceback"}]
    detail = classify_task_detail(msgs)
    router._record_decision({
        **router._decision_base(detail, msgs, "l2", "eager", "note here", ["prefer"]),
        "action": "prefer", "model": "deepseek-v4-pro", "provider": "commandcode",
        "score": 0.85,
    })
    decs = router.recent_decisions(5)
    assert decs and decs[0]["task"] == "debugging"
    assert decs[0]["path"] == "semantic"
    assert decs[0]["note"] == "note here"
    assert decs[0]["profile"] == "l2"
    assert "debug this traceback" in decs[0]["intent_text"]
    # the 101 MB field is gone from the record, not merely empty
    assert "conversation_json" not in decs[0]


# ── Deterministic routing: rule path == scoring path (unified resolver) ─────

def test_flash_prefer_and_scoring_agree(registry_db, monkeypatch):
    """HEADLINE: the prefer (rule) path and the scoring (reorder) path must
    resolve the SAME provider for the same target model. Regression for the
    live divergence: prefer picked commandcode/…flash while reorder picked
    deepseek/…flash. Under chain-as-source-of-truth, only deepseek's chain step
    is flash, so BOTH paths resolve flash on deepseek."""
    chain = [
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"},
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "opencode", "model": "deepseek-v4-pro"},
        {"provider": "opencode", "model": "ox-alpha-free"},
    ]
    cfg = _prov_config(REAL_PROVIDERS)

    # Rule path: a prefer-flash rule fires for code_generation.
    rule_router = _rule_router(
        registry_db, monkeypatch,
        [{"task": "code_generation", "profile": "*", "action": "prefer",
          "provider": "*", "model": "deepseek-v4-flash"}])
    rule_router._effective_policy = lambda config: ("eager", 0.0)
    rule_out = rule_router.select_step(
        [{"role": "user", "content": "write a helper script in python"}],
        chain=chain, profile="coder", config=cfg)
    assert rule_out is not None

    # Scoring path: no prefer rule; flash out-scores pro so scoring picks flash.
    score_router = _rule_router(registry_db, monkeypatch, [])
    score_router._effective_policy = lambda config: ("eager", 0.0)
    score_router.score_step = (
        lambda step, task, profile=None, config=None, bias=None:
        0.9 if step["model"] == "deepseek-v4-flash" else
        0.7 if step["model"] == "deepseek-v4-pro" else 0.4)
    score_out = score_router.select_step(
        [{"role": "user", "content": "this endpoint returns a 500"}],
        chain=chain, profile="coder", config=cfg)
    assert score_out is not None

    # BOTH must resolve the target model on the SAME first provider (chain order).
    assert (rule_out[0]["provider"], rule_out[0]["model"]) == \
           (score_out[0]["provider"], score_out[0]["model"])
    assert rule_out[0]["provider"] == "deepseek"
    assert rule_out[0]["model"] == "deepseek-v4-flash"


def test_degraded_target_provider_falls_to_healthy_same_model(registry_db, monkeypatch):
    """The flash target resolves to deepseek (the only flash chain step); the
    degraded commandcode (pro) is a FALLBACK and is ordered after the healthy
    opencode fallback — the model stays flash (no cheaper-model fallback)."""
    from src.api.circuit_breaker import get_circuit_breaker
    get_circuit_breaker({"failures_dead": 5, "dead_cooldown_seconds": 60,
                         "failures_degraded": 3, "degraded_cooldown_seconds": 60})
    get_circuit_breaker().get_health("commandcode", "https://cmd.example/v1", "l2")["status"] = "degraded"
    rules = [{"task": "*", "action": "prefer", "provider": "*", "model": "deepseek-v4-flash"}]
    router = _rule_router(registry_db, monkeypatch, rules)
    router._effective_policy = lambda config: ("eager", 0.0)
    chain = [
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro",
         "base_url": "https://cmd.example/v1"},
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "opencode", "model": "deepseek-v4-pro"},
    ]
    out = router.select_step([{"role": "user", "content": "design the architecture"}],
                             chain=chain, profile="l2", config=_prov_config(REAL_PROVIDERS))
    assert out is not None
    # Flash head on deepseek; healthy opencode fallback leads degraded commandcode.
    assert [s["provider"] for s in out[:3]] == ["deepseek", "opencode", "commandcode"]
    assert out[0]["model"] == "deepseek-v4-flash"


def test_provider_only_prefer_is_tiebreak_not_mandate(registry_db, monkeypatch):
    """A provider-only prefer does NOT force a model — it is a provider
    tiebreak: the model is still chosen by scoring, and the provider leads only
    because its chain step uses the chosen model."""
    rules = [{"task": "*", "action": "prefer", "provider": "opencode"}]
    router = _rule_router(registry_db, monkeypatch, rules)
    router._effective_policy = lambda config: ("eager", 0.0)
    router.score_step = (
        lambda step, task, profile=None, config=None, bias=None:
        0.9 if step["model"] == "deepseek-v4-flash" else 0.7)
    chain = [
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"},
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "opencode", "model": "deepseek-v4-flash"},
    ]
    out = router.select_step([{"role": "user", "content": "hi"}], chain=chain,
                             config=_prov_config(REAL_PROVIDERS))
    assert out is not None
    # Model still chosen by scoring = flash; opencode's chain step is flash so
    # the provider tiebreak leads it.
    assert out[0]["model"] == "deepseek-v4-flash"
    assert out[0]["provider"] == "opencode"
    assert router.recent_decisions()[-1]["action"] == "prefer"


def test_prefer_model_served_by_no_provider_falls_through(registry_db, monkeypatch):
    """A prefer whose model no provider serves must fall through to scoring
    (no provider is force-picked), with an audit trail."""
    rules = [{"task": "*", "action": "prefer", "provider": "*",
              "model": "does-not-exist-9000"}]
    router = _rule_router(registry_db, monkeypatch, rules)
    router._effective_policy = lambda config: ("eager", 0.0)
    router.score_step = (
        lambda step, task, profile=None, config=None, bias=None:
        0.9 if step["model"] == "deepseek-v4-flash" else 0.7)
    chain = [
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"},
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "opencode", "model": "deepseek-v4-pro"},
    ]
    out = router.select_step([{"role": "user", "content": "hi"}], chain=chain,
                             config=_prov_config(REAL_PROVIDERS))
    assert out is not None
    # Scoring picks flash (no prefer mandate) on the first serving provider.
    from src.api.router import logical_model_name
    assert logical_model_name(out[0]["model"], registry_db) == "deepseek-v4-flash"
    dec = router.recent_decisions()[-1]
    assert dec["action"] == "reorder"
    assert "prefer_unserved" in dec["rules"]


def test_build_chain_for_model_deterministic_order(registry_db):
    """The shared chain-builder emits one target-model step per chain step that
    uses the target (chain order), then non-target steps as fallbacks. The
    chain is the source of truth: commandcode/opencode (pro steps) are NOT
    flash targets even though their global models list includes flash."""
    from src.api.router import CapabilityRouter
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    chain = [
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro"},
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "opencode", "model": "deepseek-v4-pro"},
        {"provider": "opencode", "model": "ox-alpha-free"},
    ]
    out = router._build_chain_for_model(chain, "deepseek-v4-flash",
                                        config=_prov_config(REAL_PROVIDERS))
    # Only deepseek's chain step is flash → it is the sole target head.
    assert [(s["provider"], s["model"]) for s in out[:1]] == [
        ("deepseek", "deepseek-v4-flash"),
    ]
    # Non-target original steps remain as fallbacks.
    assert any(s["model"] == "ox-alpha-free" for s in out[1:])


def test_build_chain_for_model_chain_step_is_source_of_truth(registry_db):
    """Regression (live L2): commandcode's chain step is glm-5.3-flash, NOT
    deepseek-v4-flash. Even though commandcode's GLOBAL models list includes
    deepseek-v4-flash, the router must NOT build a commandcode/deepseek-v4-flash
    target step — commandcode only appears as a fallback for its own model."""
    from src.api.router import CapabilityRouter
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    chain = [
        {"provider": "opencode", "model": "deepseek-v4-pro"},
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "commandcode", "model": "z-ai/glm-5.3-flash"},
        {"provider": "llamacpp", "model": "qwen3.8-flash-next"},
    ]
    # commandcode's global models list DOES include deepseek-v4-flash — but the
    # chain step is glm, so commandcode must not be a flash target.
    cfg = _prov_config({
        "opencode": {"models": ["deepseek-v4-pro", "deepseek-v4-flash"]},
        "deepseek": {"models": ["deepseek-v4-pro", "deepseek-v4-flash"]},
        "commandcode": {"models": ["deepseek-v4-pro", "deepseek-v4-flash", "z-ai/glm-5.3-flash"]},
        "llamacpp": {"models": ["qwen3.8-flash-next"]},
    })
    out = router._build_chain_for_model(chain, "deepseek-v4-flash", config=cfg)
    # Only deepseek's chain step is flash → sole target head.
    assert [(s["provider"], s["model"]) for s in out[:1]] == [
        ("deepseek", "deepseek-v4-flash"),
    ]
    # commandcode (glm) and llamacpp (qwen) are fallbacks, never flash targets.
    assert "commandcode" not in [s["provider"] for s in out[:1]]
    assert any(s["provider"] == "commandcode" and "glm" in s["model"] for s in out[1:])


def test_build_chain_for_model_orders_funded_provider_first(registry_db, monkeypatch):
    """Regression (L2): commandcode drained ($0.12) must NOT be ordered ahead
    of opencode for the same target model. This models the REAL L2 state where
    opencode is at 100% monthly BUT holds $7.81 available credits — the
    balance-first credit rank must treat it as funded (not the monthly_pct
    heuristic), so the request goes to opencode first instead of 400ing on
    'insufficient credits'."""
    from src.api.router import CapabilityRouter, provider_model_name
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    chain = [
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-flash"},
        {"provider": "opencode", "model": "deepseek-v4-flash"},
    ]
    cfg = _prov_config({"commandcode": {"models": ["deepseek-v4-flash"]},
                        "opencode": {"models": ["deepseek-v4-flash"]}})

    class FakeCache:
        def get(self, provider, kind):
            if kind == "subscription" and provider == "commandcode":
                return {"payload": {"monthly_credits_remaining": 0.12}}
            if kind == "subscription" and provider == "opencode":
                # 100% monthly would trip the _credit_bonus heuristic, BUT the
                # explicit balance below says opencode still has real credits.
                return {"payload": {"monthly_pct": 100}}
            if kind == "balance" and provider == "opencode":
                return {"payload": {"available_credits": 7.81, "balance": 7.81}}
            return None
    monkeypatch.setattr("src.api.cost_cache.get_cost_cache", lambda: FakeCache())

    out = router._build_chain_for_model(chain, "deepseek-v4-flash", config=cfg)
    # Same health band (both healthy) → credits decide: funded opencode first.
    assert out[0]["provider"] == "opencode"
    assert out[1]["provider"] == "commandcode"
    # Both still serve the target model.
    assert provider_model_name("deepseek-v4-flash", "opencode", registry_db) in out[0]["model"]
    assert provider_model_name("deepseek-v4-flash", "commandcode", registry_db) in out[1]["model"]


def test_build_chain_for_model_all_drained_keeps_chain_order(registry_db, monkeypatch):
    """When every serving provider is drained, credit rank ties (all 1) and the
    chain order is preserved — never drops providers or breaks determinism."""
    from src.api.router import CapabilityRouter
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    chain = [
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-flash"},
        {"provider": "opencode", "model": "deepseek-v4-flash"},
    ]
    cfg = _prov_config({"commandcode": {"models": ["deepseek-v4-flash"]},
                        "opencode": {"models": ["deepseek-v4-flash"]}})

    class DrainedCache:
        def get(self, provider, kind):
            if kind == "subscription":
                return {"payload": {"monthly_credits_remaining": 0.12}}
            if kind == "balance" and provider == "opencode":
                return {"payload": {"available_credits": 0.30, "balance": 0.30}}
            return None
    monkeypatch.setattr("src.api.cost_cache.get_cost_cache", lambda: DrainedCache())

    out = router._build_chain_for_model(chain, "deepseek-v4-flash", config=cfg)
    # Ties preserve original chain order (deterministic, no providers dropped).
    assert [s["provider"] for s in out[:2]] == ["commandcode", "opencode"]


def test_build_chain_for_model_sorts_fallbacks_by_credit(registry_db, monkeypatch):
    """Regression (live): when the router's chosen head (e.g. llama.cpp/Qwen)
    FAILS, the FALLBACK steps must also be credit/health-ordered — a funded
    fallback (deepseek-v4-flash via opencode) is tried BEFORE a drained one
    (commandcode), instead of blindly walking the raw chain into a provider
    that 400s on 'insufficient credits'."""
    from src.api.router import CapabilityRouter
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    chain = [
        {"provider": "llamacpp", "model": "qwen3.8-27b", "base_url": "http://localhost:8080/v1"},
        {"provider": "commandcode", "model": "deepseek-v4-flash", "base_url": "https://cc/v1"},
        {"provider": "opencode", "model": "deepseek-v4-flash", "base_url": "https://oc/v1"},
    ]
    cfg = _prov_config({
        "llamacpp": {"models": ["qwen3.8-27b"]},
        "commandcode": {"models": ["deepseek-v4-flash"]},
        "opencode": {"models": ["deepseek-v4-flash"]},
    })

    class FakeCache:
        def get(self, provider, kind):
            if kind == "subscription" and provider == "commandcode":
                return {"payload": {"monthly_credits_remaining": 0.12}}
            if kind == "subscription" and provider == "opencode":
                return {"payload": {"monthly_pct": 100}}
            if kind == "balance" and provider == "opencode":
                return {"payload": {"available_credits": 7.81, "balance": 7.81}}
            return None
    monkeypatch.setattr("src.api.cost_cache.get_cost_cache", lambda: FakeCache())

    out = router._build_chain_for_model(chain, "qwen3.8-27b", config=cfg)
    providers = [s["provider"] for s in out]
    # Target head first; the FUNDED opencode fallback before drained commandcode.
    assert providers[0] == "llamacpp"
    assert providers.index("opencode") < providers.index("commandcode")


# ── Context-aware routing ──────────────────────────────────────────────────

class _CtxCfg:
    """Duck-typed config with providers + model_limits for context tests."""

    def __init__(self, providers, model_limits):
        self.providers = providers
        self.model_limits = model_limits
        self.dynamic_routing = {}


def test_context_window_for_known_and_unknown(registry_db):
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    cfg = _CtxCfg({}, {
        "deepseek-v4-pro": {"context_window": 1000000},
        "deepseek-v4-flash": {"context_window": 1000000},
    })
    assert router._context_window_for("deepseek-v4-pro", cfg) == 1000000
    # Unknown model → conservative default (never oversized into a local model).
    assert router._context_window_for("qwen3.8-27b", cfg) == 128000


def test_fits_context_and_output_reserve(registry_db):
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    cfg = _CtxCfg({}, {"deepseek-v4-pro": {"context_window": 1000000}})
    assert router._fits_context("deepseek-v4-pro", 200000, cfg) is True
    # 200k request on a 200k-context model with 8k reserve does NOT fit.
    cfg2 = _CtxCfg({}, {"qwen3.8-27b": {"context_window": 200192}})
    assert router._fits_context("qwen3.8-27b", 205120, cfg2) is False


def test_candidate_models_excludes_too_small_context(registry_db):
    """A model whose context can't hold the request is dropped from candidates,
    so scoring picks the next-best model that FITS instead of 400ing."""
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    cfg = _CtxCfg(
        {"llamacpp": {"models": ["qwen3.8-27b"]},
         "deepseek": {"models": ["deepseek-v4-flash"]}},
        {"qwen3.8-27b": {"context_window": 200192},
         "deepseek-v4-flash": {"context_window": 1000000}},
    )
    chain = [{"provider": "llamacpp", "model": "qwen3.8-27b"},
             {"provider": "deepseek", "model": "deepseek-v4-flash"}]
    # 205k request: the qwen model (200k) is excluded; flash (1M) remains.
    cand = router._candidate_models(chain, set(), cfg, request_tokens=205120)
    assert "qwen3.8-27b" not in cand
    assert "deepseek-v4-flash" in cand


def test_build_chain_drops_too_small_fallbacks(registry_db):
    """Fallback steps whose model can't hold the request are dropped, so a dead
    head never falls through to a too-small-context model that 400s."""
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    cfg = _CtxCfg(
        {"llamacpp": {"models": ["qwen3.8-27b"]},
         "deepseek": {"models": ["deepseek-v4-flash"]}},
        {"qwen3.8-27b": {"context_window": 200192},
         "deepseek-v4-flash": {"context_window": 1000000}},
    )
    chain = [{"provider": "deepseek", "model": "deepseek-v4-flash"},
             {"provider": "llamacpp", "model": "qwen3.8-27b"}]
    # Target flash (1M) fits; the qwen FALLBACK (200k) can't hold 205k → dropped.
    out = router._build_chain_for_model(chain, "deepseek-v4-flash", config=cfg,
                                        request_tokens=205120)
    models = [s["model"] for s in out]
    assert "deepseek-v4-flash" in models
    assert "qwen3.8-27b" not in models


def test_max_routable_context(registry_db):
    router = CapabilityRouter(enabled=True, db_path=registry_db)
    cfg = _CtxCfg({}, {
        "deepseek-v4-pro": {"context_window": 1000000},
        "deepseek-v4-flash": {"context_window": 1000000},
    })
    chain = [{"provider": "deepseek", "model": "deepseek-v4-pro"},
             {"provider": "deepseek", "model": "deepseek-v4-flash"}]
    assert router._max_routable_context(chain, cfg) == 1000000
