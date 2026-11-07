"""Agent provenance: the caller says who it is, LCP records it — and never invents it.

The behaviour under test is deliberately tri-state. ``/l1`` and ``/l2`` are shared routes:
the operator's own client and an autonomous agent hit the same URL, so "no header" has to
mean *unknown*, not *human*. These tests exist because the tempting shortcut — a boolean
defaulting to False — silently reclassifies every unlabelled request as a person.
"""

import sqlite3

import pytest

from src.api import conv_store
from src.server.handler import (
    AGENT_GENERATED_HEADER,
    AGENT_IDENTITY_HEADER,
    _caller_provenance,
)


class FakeHeaders(dict):
    """Case-insensitive like http.client.HTTPMessage, so the tests match the wire."""

    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == str(key).lower():
                return v
        return default


# ── the header parser ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True), ("TRUE", True), (" True ", True),
])
def test_truthy_values_declare_an_agent(raw, expected):
    generated, _ = _caller_provenance(FakeHeaders({AGENT_GENERATED_HEADER: raw}))
    assert generated is expected


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", "human", "user"])
def test_explicit_denials_are_false_not_unknown(raw):
    generated, _ = _caller_provenance(FakeHeaders({AGENT_GENERATED_HEADER: raw}))
    assert generated is False


def test_no_header_at_all_is_unknown():
    generated, identity = _caller_provenance(FakeHeaders())
    assert generated is None, "an unlabelled request must not be classified as a person"
    assert identity is None


def test_unrecognised_value_is_unknown_not_a_denial():
    generated, _ = _caller_provenance(FakeHeaders({AGENT_GENERATED_HEADER: "maybe"}))
    assert generated is True, "a non-denial value is a claim, not silence"

    generated, _ = _caller_provenance(FakeHeaders({AGENT_GENERATED_HEADER: ""}))
    assert generated is None, "an empty header made no claim"


def test_identity_header_alone_claims_an_agent():
    generated, identity = _caller_provenance(
        FakeHeaders({AGENT_IDENTITY_HEADER: "hermes/homelab-expert-l1"}))
    assert generated is True
    assert identity == "hermes/homelab-expert-l1"


def test_identity_is_carried_verbatim_including_with_a_denial():
    generated, identity = _caller_provenance(FakeHeaders({
        AGENT_GENERATED_HEADER: "false",
        AGENT_IDENTITY_HEADER: "some-client/9",
    }))
    assert generated is False
    assert identity == "some-client/9"


def test_header_lookup_is_case_insensitive():
    generated, identity = _caller_provenance(FakeHeaders({
        "x-agent-generated": "true", "x-agent-identity": "hermes/x",
    }))
    assert (generated, identity) == (True, "hermes/x")


def test_a_broken_headers_object_never_raises():
    class Exploding:
        def get(self, *a, **k):
            raise RuntimeError("boom")

    assert _caller_provenance(Exploding()) == (None, None)


# ── persistence: the rollup and the request row ───────────────────────────────

@pytest.fixture()
def store(tmp_path, monkeypatch):
    db = tmp_path / "costs.db"
    monkeypatch.setattr(conv_store, "db_path", lambda: str(db))
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, source TEXT)")
    con.execute(
        "CREATE TABLE conv_messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " conversation_id TEXT, seq INTEGER, role TEXT, content TEXT, content_len INTEGER,"
        " truncated INTEGER DEFAULT 0, tool_name TEXT, tool_call_id TEXT, tool_calls TEXT,"
        " reasoning TEXT, ts TEXT, active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0,"
        " msg_hash TEXT, origin TEXT, UNIQUE (conversation_id, seq))")
    con.commit()
    con.close()
    return db


def _row(db, cid):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT agent_generated, agent_identity FROM conversations WHERE conversation_id=?",
            (cid,)).fetchone()
    finally:
        con.close()


def test_columns_are_added_to_an_old_database(store):
    conv_store.ensure_store()
    cols = {r[1] for r in sqlite3.connect(store).execute("PRAGMA table_info(conversations)")}
    assert {"agent_generated", "agent_identity"} <= cols


def test_agent_declaration_is_stored_as_one(store):
    conv_store.ensure_store()
    conv_store.upsert_conversation("c1", source="live",
                                   agent_generated=True,
                                   agent_identity="hermes/homelab-expert-l2")
    row = _row(store, "c1")
    assert row["agent_generated"] == 1
    assert row["agent_identity"] == "hermes/homelab-expert-l2"


def test_a_denial_is_stored_as_zero_not_null(store):
    conv_store.ensure_store()
    conv_store.upsert_conversation("c2", source="live", agent_generated=False)
    assert _row(store, "c2")["agent_generated"] == 0


def test_unknown_is_stored_as_null(store):
    conv_store.ensure_store()
    conv_store.upsert_conversation("c3", source="live")
    assert _row(store, "c3")["agent_generated"] is None


def test_a_later_silent_request_does_not_erase_a_known_declaration(store):
    conv_store.ensure_store()
    conv_store.upsert_conversation("c4", source="live", agent_generated=True,
                                   agent_identity="hermes/x")
    # A client that sends no header must not overwrite what an earlier request declared.
    conv_store.upsert_conversation("c4", source="live")
    row = _row(store, "c4")
    assert row["agent_generated"] == 1
    assert row["agent_identity"] == "hermes/x"


def test_capture_request_passes_provenance_through(store):
    conv_store.ensure_store()
    written = conv_store.capture_request(
        "c5", {"messages": [{"role": "user", "content": "hi"}]},
        reply="hello", profile="l2", model="m",
        agent_generated=True, agent_identity="hermes/homelab-expert-l2")
    assert written == 2
    row = _row(store, "c5")
    assert row["agent_generated"] == 1
    assert row["agent_identity"] == "hermes/homelab-expert-l2"


# ── the read paths must survive a database that has not migrated yet ──────────
# The code ships before the migration runs (and the container migrates at start), so a
# rolled-back or not-yet-upgraded database is a real state: the history page has to render
# with the columns absent instead of raising where it used to work.

@pytest.fixture()
def old_store(tmp_path, monkeypatch):
    """A pre-027 schema: conversations with no provenance columns, no ensure_store()."""
    db = tmp_path / "old.db"
    monkeypatch.setattr(conv_store, "db_path", lambda: str(db))
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE conversations (
        conversation_id TEXT PRIMARY KEY, name TEXT, summary TEXT, profile TEXT, model TEXT,
        source TEXT, agent_session_id TEXT, started_at TEXT, ended_at TEXT,
        message_count INTEGER, active_message_count INTEGER, tool_call_count INTEGER,
        input_tokens INTEGER, output_tokens INTEGER, imported_at TEXT,
        first_seen TEXT, last_seen TEXT, calls INTEGER, tokens INTEGER,
        cost REAL, errors INTEGER)""")
    # conv_messages arrived in revision 024, so a pre-027 database has it; only the 027
    # columns are missing. Without it the audit path fails for the wrong reason.
    con.execute("""CREATE TABLE conv_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, seq INTEGER,
        role TEXT, content TEXT, content_len INTEGER, truncated INTEGER DEFAULT 0,
        tool_name TEXT, tool_call_id TEXT, tool_calls TEXT, reasoning TEXT, ts TEXT,
        active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0, msg_hash TEXT, origin TEXT,
        UNIQUE (conversation_id, seq))""")
    con.execute("INSERT INTO conversations (conversation_id, profile, source, first_seen)"
                " VALUES ('old-1', 'l2', 'live', '2026-10-01T00:00:00+00:00')")
    con.commit()
    con.close()
    return db


def test_history_page_reads_an_unmigrated_database(old_store):
    page = conv_store.list_conversations({})
    assert page["available"] is True
    assert len(page["conversations"]) == 1
    row = page["conversations"][0]
    assert row["id"] == "old-1"
    assert row["agent_generated"] is None, "no column and no claim is unknown, not human"


def test_audit_reads_an_unmigrated_database(old_store):
    audit = conv_store.conversation_audit("old-1")
    assert audit["available"] is True
    assert audit["agent_generated"] is None
    assert audit["agent_identity"] is None


# ── the page actually shows the actor ─────────────────────────────────────────
# The payload carrying the field is not the same claim as the page rendering it; a Jinja
# comparison against a missing/renamed key fails silently (the `else` branch), so the real
# page is rendered and its markup read.

@pytest.fixture()
def page_store(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    monkeypatch.setenv("LCP_COSTS_DB", str(tmp_path / "costs.db"))
    conv_store.ensure_store()
    conv_store.capture_request(
        "pc-agent", {"messages": [{"role": "user", "content": "a"}]},
        reply="r", profile="l2", model="m1",
        agent_generated=True, agent_identity="hermes/homelab-expert-l2")
    conv_store.capture_request(
        "pc-human", {"messages": [{"role": "user", "content": "b"}]},
        reply="r", profile="l2", model="m1", agent_generated=False)
    conv_store.capture_request(
        "pc-unknown", {"messages": [{"role": "user", "content": "c"}]},
        reply="r", profile="l2", model="m1")
    cfg = MagicMock()
    cfg.providers, cfg.profiles = {}, {}
    return cfg


def test_page_marks_an_agent_row_and_an_unknown_row_differently(page_store):
    from src.ui.pages import render_conversations_page
    html = render_conversations_page(page_store, None, {})
    # Provenance is a chip on the name cell since the Source column folded into
    # the audit panel — the three-way distinction is the property, not the dot.
    assert '<span class="chip conv-prov" title="hermes/homelab-expert-l2">agent</span>' in html, \
        "a declared agent row must be visibly marked"
    assert '<span class="chip conv-prov">not agent</span>' in html
    assert 'title="Caller never declared"' in html, \
        "an undeclared row must render as unknown, not as a person"
    assert "hermes/homelab-expert-l2" in html
