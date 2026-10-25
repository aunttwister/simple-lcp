"""Unit tests for multiset dedup in the transcript capture path.

The regression these pin: a client that COMPACTS its history used to have the whole
post-compaction history appended again on every request.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, "/your/data/docker-apps/simple-lcp")

from src.api import conv_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    db = tmp_path / "costs.db"
    monkeypatch.setattr(conv_store, "db_path", lambda: str(db))
    conv_store.ensure_store()
    return str(db)


def _rows(db, cid):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(
            "SELECT seq, role, content, tool_call_id, msg_hash FROM conv_messages "
            "WHERE conversation_id=? ORDER BY seq", (cid,))]
    finally:
        con.close()


def _msg(role, content, **kw):
    d = {"role": role, "content": content}
    d.update(kw)
    return d


def test_reimport_of_identical_list_writes_nothing(store):
    cid = "c1"
    history = [_msg("user", "a"), _msg("assistant", "b")]
    assert conv_store.append_messages(cid, history, origin="capture") == 2
    assert conv_store.append_messages(cid, history, origin="capture") == 0
    assert len(_rows(store, cid)) == 2


def test_compacted_history_is_not_reappended(store):
    """The regression: the client drops its middle, then sends the shortened list."""
    cid = "c2"
    full = [_msg("user", "u1"), _msg("assistant", "a1"),
            _msg("user", "u2"), _msg("assistant", "a2"),
            _msg("user", "u3"), _msg("assistant", "a3")]
    assert conv_store.append_messages(cid, full, origin="capture") == 6

    # Compaction removes the first two turns; the remaining four are all stored.
    compacted = full[2:]
    assert conv_store.append_messages(cid, compacted, origin="capture") == 0
    assert len(_rows(store, cid)) == 6


def test_compacted_history_plus_one_new_turn_appends_exactly_one(store):
    cid = "c3"
    full = [_msg("user", "u1"), _msg("assistant", "a1"), _msg("user", "u2")]
    conv_store.append_messages(cid, full, origin="capture")

    # Compaction drops the first turn; one new turn is added at the end.
    compacted_plus = [_msg("assistant", "a1"), _msg("user", "u2"), _msg("user", "u3")]
    assert conv_store.append_messages(cid, compacted_plus, origin="capture") == 1
    rows = _rows(store, cid)
    assert len(rows) == 4
    assert rows[-1]["content"] == "u3"
    assert [r["seq"] for r in rows] == [0, 1, 2, 3]


def test_legitimate_repeat_is_stored_twice(store):
    """Positional dedup and set dedup both got this wrong; counting gets it right."""
    cid = "c4"
    assert conv_store.append_messages(cid, [_msg("user", "ok")], origin="capture") == 1
    assert conv_store.append_messages(cid, [_msg("user", "ok"), _msg("user", "ok")],
                                      origin="capture") == 1
    rows = _rows(store, cid)
    assert [r["content"] for r in rows] == ["ok", "ok"]


def test_three_identical_then_four_identical_appends_one(store):
    cid = "c5"
    conv_store.append_messages(cid, [_msg("assistant", "")] * 3, origin="capture")
    assert conv_store.append_messages(cid, [_msg("assistant", "")] * 4,
                                      origin="capture") == 1
    assert len(_rows(store, cid)) == 4


def test_empty_assistant_rows_keep_their_multiplicity(store):
    """Empty assistant turns share a hash; counting must still keep each one."""
    cid = "c6"
    batch = [_msg("assistant", "", tool_calls="[]")] * 5
    assert conv_store.append_messages(cid, batch, origin="capture") == 5
    assert conv_store.append_messages(cid, batch, origin="capture") == 0
    assert len(_rows(store, cid)) == 5


def test_capture_request_twice_is_idempotent(store):
    """End-to-end: the capture path is what re-appended history in prod."""
    cid = "c7"
    body = {"messages": [
        {"role": "system", "content": "you are a router"},
        {"role": "user", "content": "hello"},
    ]}
    assert conv_store.capture_request(cid, body, reply="hi") == 2
    assert conv_store.capture_request(cid, body, reply="hi") == 0
    rows = _rows(store, cid)
    assert [r["content"] for r in rows] == ["hello", "hi"]


def test_capture_request_after_compaction_adds_only_new(store):
    cid = "c8"
    b1 = {"messages": [{"role": "user", "content": "one"}]}
    conv_store.capture_request(cid, b1, reply="r1")
    # The client now sends only the tail plus one new turn.
    b2 = {"messages": [{"role": "assistant", "content": "r1"},
                       {"role": "user", "content": "two"}]}
    written = conv_store.capture_request(cid, b2, reply="r2")
    assert written == 2  # "two" and "r2"; "r1" was already stored
    assert [r["content"] for r in _rows(store, cid)] == ["one", "r1", "two", "r2"]
