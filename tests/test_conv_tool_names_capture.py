"""Tool names in the conversation transcript must survive the LIVE capture path.

Two defects kept ``(unnamed)`` in the conversations view after a fix that only
the module-level test could see:

1. ``_tool_name_lookup`` read ``tool_calls`` only as a LIST. The live path hands
   it the output of ``_extract_messages``, where the field is a JSON STRING, so
   the map was empty on every real request. The test that "proved" the fix fed
   raw client dicts — a shape production never passes.
2. The assistant turn's ``tool_calls`` were only ever stored when the client
   re-sent the turn on its NEXT request. A turn carrying prose as well as calls
   was by then already stored by the reply path with the same hash (which does
   not include ``tool_calls``), so the calling turn was deduplicated away and
   its tool results were left with no caller in the store at all.

These tests therefore go through ``capture_request`` — the function the request
handler actually calls — never through ``_prepare`` directly.
"""

import json
import sqlite3

import pytest

from src.api import conv_store
from src.server.sse_helpers import (extract_message_tool_calls,
                                    extract_sse_tool_calls)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("LCP_COSTS_DB", str(tmp_path / "costs.db"))
    conv_store.ensure_store()
    return tmp_path / "costs.db"


def _rows(db, cid):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(
        "SELECT seq, role, tool_name, tool_call_id, tool_calls FROM conv_messages "
        "WHERE conversation_id=? ORDER BY seq", (cid,))]
    con.close()
    return out


def _call(cid, name="terminal"):
    return [{"id": "call_x1", "type": "function",
             "function": {"name": name, "arguments": "{\"command\":\"ls\"}"}}]


def test_capture_names_a_tool_result_from_the_calling_turn_in_the_same_batch(db):
    """The client re-sends [user, assistant(tool_calls), tool(result)] in one
    body; the generic OpenAI tool result carries only its tool_call_id."""
    conv_store.capture_request("c1", {"messages": [
        {"role": "user", "content": "run ls"},
        {"role": "assistant", "content": "", "tool_calls": _call("c1")},
        {"role": "tool", "tool_call_id": "call_x1", "content": "{\"output\":\"ok\"}"},
    ]})
    rows = _rows(db, "c1")
    tool = [r for r in rows if r["role"] == "tool"]
    assert tool and tool[0]["tool_name"] == "terminal", \
        "the live capture path stored the tool result unnamed"


def test_reply_tool_calls_are_stored_on_the_reply_turn(db):
    """The calling turn's prose lands from the reply, its calls from the stream —
    one row, carrying both; the next request must not duplicate it."""
    conv_store.capture_request(
        "c2", {"messages": [{"role": "user", "content": "run ls"}]},
        reply="I will run the command now.", reply_tool_calls=_call("c2"))
    rows = _rows(db, "c2")
    assistant = [r for r in rows if r["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["tool_calls"], "the reply turn lost its tool calls"
    assert json.loads(assistant[0]["tool_calls"])[0]["function"]["name"] == "terminal"

    # the client now re-sends the same turn plus its result
    conv_store.capture_request("c2", {"messages": [
        {"role": "user", "content": "run ls"},
        {"role": "assistant", "content": "I will run the command now.",
         "tool_calls": _call("c2")},
        {"role": "tool", "tool_call_id": "call_x1", "content": "{\"output\":\"ok\"}"},
    ]})
    rows = _rows(db, "c2")
    assert len([r for r in rows if r["role"] == "assistant"]) == 1, \
        "the re-sent calling turn was stored a second time"
    tool = [r for r in rows if r["role"] == "tool"]
    assert tool[0]["tool_name"] == "terminal"


def test_reply_with_only_tool_calls_is_still_stored(db):
    """A pure tool-calling turn has empty prose; the row must exist anyway, or
    the result that follows has no caller."""
    conv_store.capture_request(
        "c3", {"messages": [{"role": "user", "content": "run ls"}]},
        reply="", reply_tool_calls=_call("c3"))
    rows = _rows(db, "c3")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[1]["tool_calls"]


def test_tool_result_without_a_caller_stays_unnamed(db):
    """Unchanged contract: nothing to resolve from, so no name is invented."""
    conv_store.capture_request("c4", {"messages": [
        {"role": "tool", "tool_call_id": "ghost", "content": "orphan"}]})
    rows = _rows(db, "c4")
    assert rows[0]["tool_name"] is None


def test_lookup_reads_both_the_list_and_the_serialised_shape():
    """The root cause of the first defect, pinned: whichever shape a batch is in,
    the lookup must resolve the name — reading lists only left the map empty."""
    raw = [{"role": "assistant", "content": "", "tool_calls": _call("c1")}]
    assert conv_store._tool_name_lookup(raw) == {"call_x1": "terminal"}
    serialised = [{"role": "assistant", "content": "",
                   "tool_calls": json.dumps(_call("c1"))}]
    assert conv_store._tool_name_lookup(serialised) == {"call_x1": "terminal"}
    assert conv_store._tool_name_lookup(
        [{"role": "assistant", "content": "", "tool_calls": "{not json"}]) == {}


def test_extract_messages_hands_the_lookup_a_usable_batch():
    """End of the chain that broke: whatever ``_extract_messages`` emits must be
    enough for the lookup, since that is what ``append_messages`` is given."""
    batch = conv_store._extract_messages({"messages": [
        {"role": "assistant", "content": "", "tool_calls": _call("c1")},
        {"role": "tool", "tool_call_id": "call_x1", "content": "out"},
    ]})
    assert conv_store._tool_name_lookup(batch) == {"call_x1": "terminal"}


def test_sse_tool_calls_are_reassembled_from_fragments():
    """OpenAI streams the call in pieces: id and name first, arguments sliced."""
    stream = b"".join(
        b"data: " + json.dumps({"choices": [{"delta": d}]}).encode() + b"\n\n"
        for d in (
            {"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                             "function": {"name": "read_file", "arguments": ""}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": "{\"path\":\""}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": "/etc/os-release\"}"}}]},
            {"tool_calls": [{"index": 1, "id": "call_b", "type": "function",
                             "function": {"name": "terminal", "arguments": "{}"}}]},
        )) + b"data: [DONE]\n\n"
    calls = extract_sse_tool_calls(stream)
    assert [c["id"] for c in calls] == ["call_a", "call_b"]
    assert calls[0]["function"]["name"] == "read_file"
    assert calls[0]["function"]["arguments"] == '{"path":"/etc/os-release"}'
    assert calls[1]["function"]["name"] == "terminal"


def test_sse_tool_calls_absent_or_garbage_yields_empty():
    assert extract_sse_tool_calls(b"data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n") == []
    assert extract_sse_tool_calls(b"not a stream at all") == []
    # a fragment that never names a function cannot be stored as a call
    frag = b"data: " + json.dumps(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a"}]}}]}).encode()
    assert extract_sse_tool_calls(frag + b"\n\n") == []


def test_non_streamed_message_tool_calls_are_extracted():
    body = {"choices": [{"message": {"content": None, "tool_calls": _call("c1")}}]}
    assert extract_message_tool_calls(body)[0]["function"]["name"] == "terminal"
    assert extract_message_tool_calls({"choices": [{"message": {"content": "hi"}}]}) == []
    assert extract_message_tool_calls({}) == []
