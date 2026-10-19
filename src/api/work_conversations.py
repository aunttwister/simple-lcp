"""Unified conversation-centered log view.

Three log realms currently sit in separate tables under the same costs.db:

* ``requests``           — every chat-completions call (model, provider, tokens,
                           cost, latency, success/error)
* ``routing_decisions``  — the router's provider decision per request (task
                           label, policy/action, and a summary of the
                           conversation content that drove it)
* (external) the board decisions ledger, joined by profile+time later

Until now nothing tied them together. This module introduces the missing
correlation key — ``conversation_id`` — on both tables:

* NEW calls are stamped at write time with the client's real per-conversation
  header (``x-opencode-session``, sent by Hermes >= 0.21). Rows written by
  clients WITHOUT that header (or pre-date this change) get NULL and are
  covered by a one-time BACKFILL: same (profile, model), gaps <= 5 minutes
  form one conversation.
* The backfill is deterministic and idempotent (fills NULLs only).

The view then renders conversations newest-first: a short summary (first user
message + counts) per conversation, expanding into the chronological sequence
of request + routing events. Determinstic summaries in v1; an LLM summary
pass is a later, optional refinement.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

# The shared log-table module — owns ordering (newest first by default) and the
# pagination window for every log view, so no view invents its own ORDER BY.
from ..ui import tables

# conversation_id values are opaque strings: either the client's session
# header or a deterministic backfill id like "c<10 hex>".
_CONV_SQLITE = "data/costs.db"

# ── Sort allow-lists ────────────────────────────────────────────────────────
# (key, label, ORDER BY fragment). The first entry is the default, and it is
# always newest-to-oldest. Fragments are static strings authored here — the
# client only ever sends a key, never SQL.
CONVERSATION_SORTS: Tuple[tables.SortSpec, ...] = (
    ("newest", "Newest activity first", "last_seen DESC, conversation_id DESC"),
    ("oldest", "Oldest activity first", "last_seen ASC, conversation_id DESC"),
)
REQUEST_SORTS: Tuple[tables.SortSpec, ...] = (
    ("newest", "Newest first", "id DESC"),
    ("oldest", "Oldest first", "id ASC"),
)
ROUTING_SORTS: Tuple[tables.SortSpec, ...] = (
    ("newest", "Newest first", "id DESC"),
    ("oldest", "Oldest first", "id ASC"),
)


def db_path() -> str:
    return os.environ.get("LCP_COSTS_DB", _CONV_SQLITE)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path(), timeout=10)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema() -> None:
    """Idempotent ALTER: add conversation_id to both tables + the persisted
    conversations table (name/summary generated once and stored)."""
    con = _connect()
    try:
        for table in ("requests", "routing_decisions"):
            # Guard on existence: ``ALTER TABLE`` raises on a table that is not
            # there, which aborted the rest of this function — including the
            # CREATE below — on any database that had never been served. That made
            # the conversations table impossible to create from a clean DB.
            present = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            if not present:
                continue
            cols = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
            if "conversation_id" not in cols:
                con.execute("ALTER TABLE %s ADD COLUMN conversation_id TEXT" % table)
        con.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                conversation_id TEXT PRIMARY KEY,
                name TEXT,
                summary TEXT,
                profile TEXT,
                model TEXT,
                first_seen TEXT,
                last_seen TEXT,
                calls INTEGER,
                tokens INTEGER,
                cost REAL,
                errors INTEGER,
                generated_at TEXT
            )
        """)
        con.commit()
    finally:
        con.close()


def stamp_new(request_id: int, conversation_id: str, profile: str,
              ts: str) -> None:
    """Stamp a JUST-inserted request + the routing decisions of the same call.

    Called by the proxy at the end of a request when the client sent a real
    per-conversation header. The request row is matched by its primary key;
    the routing rows by the request's timestamp window (±30s), which keeps
    the stamp from touching unrelated traffic.
    """
    ensure_schema()
    con = _connect()
    try:
        con.execute(
            "UPDATE requests SET conversation_id = ? WHERE id = ? "
            "AND conversation_id IS NULL",
            (conversation_id, request_id))
        delta = 30  # seconds of slack for the routing rows of this call
        con.execute(
            """
            UPDATE routing_decisions SET conversation_id = ?
            WHERE conversation_id IS NULL
              AND profile = ?
              AND datetime(ts) BETWEEN datetime(?, '-%d seconds')
                                     AND datetime(?, '+%d seconds')
            """ % (delta, delta),
            (conversation_id, profile, ts, ts))
        con.commit()
    finally:
        con.close()



def _profiles() -> List[str]:
    """The profiles that appear in the log's profile filter."""
    con = _connect()
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT profile FROM requests WHERE conversation_id IS NOT NULL "
            "ORDER BY profile")]
    finally:
        con.close()


def requests_view(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Paginated raw request log (Requests tab). Newest first by default."""
    params = params or {}
    profile_filter = str(params.get("profile") or "").strip()
    where = ""
    args: List[Any] = []
    if profile_filter:
        where = " WHERE profile = ?"
        args = [profile_filter]
    con = _connect()
    try:
        total = con.execute("SELECT COUNT(*) FROM requests" + where, args).fetchone()[0]
        st = tables.state(params, REQUEST_SORTS, total)
        limit_sql, limit_args = tables.limit_clause(st)
        rows = con.execute(
            "SELECT id, timestamp, profile, model, provider, prompt_tokens, "
            "completion_tokens, cost, latency_ms, success, error_type, "
            "conversation_id FROM requests" + where +
            " ORDER BY " + st["order_sql"] + limit_sql,
            args + limit_args).fetchall()
        con.close()
    except sqlite3.Error:
        con.close()
        raise
    return {
        "rows": [dict(r) for r in rows],
        "total": total,
        "filter": tables.filter_payload(st, total, profile=profile_filter,
                                        profiles=_profiles()),
    }


def provider_decisions_view(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Paginated provider-routing decisions log (Provider decisions tab)."""
    params = params or {}
    con = _connect()
    try:
        total = con.execute(
            "SELECT COUNT(*) FROM routing_decisions").fetchone()[0]
        st = tables.state(params, ROUTING_SORTS, total)
        limit_sql, limit_args = tables.limit_clause(st)
        rows = con.execute(
            "SELECT id, ts, profile, task, policy, action, provider, model, "
            "score, note, conversation_id FROM routing_decisions "
            "ORDER BY " + st["order_sql"] + limit_sql, limit_args).fetchall()
        con.close()
    except sqlite3.Error:
        con.close()
        raise
    return {
        "rows": [dict(r) for r in rows],
        "total": total,
        "filter": tables.filter_payload(st, total),
    }
