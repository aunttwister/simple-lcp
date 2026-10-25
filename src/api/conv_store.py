"""LCP's own conversation message store.

LCP keys a conversation by the client's ``x-opencode-session`` header, which the
``lcp`` model-provider plugin makes every Hermes agent send (its own session id).
This module owns the *messages* of those conversations, so LCP is self-sufficient:
it no longer has to reach into another application's session database to answer
"what was said in this conversation".

Two writers feed one table:

* the one-off migration (``scripts/migrate_conversations.py``) imports the corpus
  that predates this store, with ``origin='migration'``;
* the live capture appends the messages of each request as it is served, with
  ``origin='capture'``.

Both go through :func:`append_messages`, which is **positional and idempotent**:
it compares the incoming sequence against what is stored and appends only the
suffix that is not already there. Re-running the migration therefore adds nothing
the second time, and the next request of a live conversation adds only its new
turns — no duplicated transcripts.

Content is capped at :data:`MESSAGE_CAP` per message. ``content_len`` always
records the true length and ``truncated`` flags the row, so a capped row stays
self-describing and the audit view can report the real size instead of silently
showing a shortened body.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .work_conversations import db_path

#: Per-message storage cap. A row over this stores a prefix and records the real
#: length in ``content_len``; 578 of the 258 k rows in the current corpus exceed it.
MESSAGE_CAP = 64 * 1024

_CONV_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS conv_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT,
    content TEXT,
    content_len INTEGER,
    truncated INTEGER DEFAULT 0,
    tool_name TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    reasoning TEXT,
    ts TEXT,
    active INTEGER DEFAULT 1,
    compacted INTEGER DEFAULT 0,
    msg_hash TEXT,
    origin TEXT,
    UNIQUE (conversation_id, seq)
)
"""

#: Rollup columns capture writes into ``conversations``. Kept in step with alembic
#: 024 by hand — see the note in :func:`ensure_store` for why it is not imported.
_ROLLUP_COLUMNS = (
    ("source", "TEXT"),
    ("agent_session_id", "TEXT"),
    ("started_at", "TEXT"),
    ("ended_at", "TEXT"),
    ("message_count", "INTEGER"),
    ("active_message_count", "INTEGER"),
    ("tool_call_count", "INTEGER"),
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("imported_at", "TEXT"),
)


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def ensure_store() -> None:
    """Create the message store and the rollup columns capture writes. Idempotent.

    The schema normally arrives with alembic revision 024; this exists so the
    store also works on a database built without the migration line (tests, a
    fresh checkout) and so a running container that predates 024 cannot write
    into a missing table.

    ``conversations`` is created by ``work_conversations.ensure_schema()`` rather
    than by 024, so it is ensured here too — and its rollup columns are added if
    absent. Without that, capture would report messages written while silently
    dropping every rollup. The column list is deliberately duplicated from 024
    rather than imported: a migration is a frozen historical record, and having it
    read app code would let a later edit rewrite what an old revision did.
    """
    con = _connect()
    try:
        con.execute(_CONV_MESSAGES_DDL)
        con.commit()
    finally:
        con.close()
    try:
        from . import work_conversations
        work_conversations.ensure_schema()
    except Exception:  # noqa: BLE001 — rollup table is optional at write time
        import logging
        logging.getLogger(__name__).warning("conv_store_rollup_schema_failed",
                                            exc_info=True)
    _ensure_rollup_columns()


def _ensure_rollup_columns() -> None:
    """Add any rollup column of ``conversations`` that is missing. Idempotent."""
    con = _connect()
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name='conversations'").fetchone():
            return
        cols = {r[1] for r in con.execute("PRAGMA table_info(conversations)")}
        for column, ddl in _ROLLUP_COLUMNS:
            if column not in cols:
                con.execute("ALTER TABLE conversations ADD COLUMN %s %s" % (column, ddl))
        con.commit()
    finally:
        con.close()


def content_hash(role: Optional[str], content: Optional[str],
                 tool_name: Optional[str] = None,
                 tool_call_id: Optional[str] = None) -> str:
    """Identity of a message within its conversation: role + body + tool linkage.

    Deliberately over the *full* body, not a prefix: two tool results can share a
    long header and differ only at the end, and deduping those would corrupt the
    transcript.
    """
    h = hashlib.sha1()
    for part in (role or "", content or "", tool_name or "", tool_call_id or ""):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _prepare(message: dict[str, Any]) -> dict[str, Any]:
    """Normalise one incoming message into a storable row (content capped)."""
    raw = message.get("content")
    body = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    true_len = len(body)
    truncated = true_len > MESSAGE_CAP
    if truncated:
        body = body[:MESSAGE_CAP]
    role = message.get("role")
    tool_name = message.get("tool_name")
    tool_call_id = message.get("tool_call_id")
    return {
        "role": role,
        "content": body,
        "content_len": true_len,
        "truncated": 1 if truncated else 0,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "tool_calls": message.get("tool_calls"),
        "reasoning": message.get("reasoning"),
        "ts": message.get("ts"),
        "active": int(message.get("active", 1) or 0),
        "compacted": int(message.get("compacted", 0) or 0),
        "msg_hash": content_hash(role, body, tool_name, tool_call_id),
    }


def append_messages(conversation_id: str, messages: Iterable[dict[str, Any]],
                    *, origin: str,
                    con: Optional[sqlite3.Connection] = None) -> int:
    """Append *messages* to a conversation, skipping those already stored.

    Returns the number of rows actually written, so a caller can tell "imported"
    from "already present" without re-reading.

    Dedup is positional: the incoming hashes are compared against the stored
    sequence and only the unmatched suffix is appended, continuing the stored
    numbering. Positional (rather than set) comparison matters because a
    conversation legitimately repeats a message — an identical "ok" twice is two
    turns, not one.
    """
    prepared = [_prepare(m) for m in messages]
    if not prepared:
        return 0

    own = con is None
    if own:
        con = _connect()
    try:
        stored = con.execute(
            "SELECT msg_hash FROM conv_messages WHERE conversation_id=? ORDER BY seq",
            (conversation_id,),
        ).fetchall()
        stored_hashes = [r["msg_hash"] for r in stored]

        # Longest common prefix of stored vs incoming — the part already known.
        matched = 0
        limit = min(len(stored_hashes), len(prepared))
        while matched < limit and stored_hashes[matched] == prepared[matched]["msg_hash"]:
            matched += 1

        if matched == len(prepared):
            return 0  # nothing new (identical re-run)

        next_seq = len(stored_hashes)
        rows = [
            (conversation_id, next_seq + i, m["role"], m["content"], m["content_len"],
             m["truncated"], m["tool_name"], m["tool_call_id"], m["tool_calls"],
             m["reasoning"], m["ts"], m["active"], m["compacted"], m["msg_hash"], origin)
            for i, m in enumerate(prepared[matched:])
        ]
        con.executemany(
            "INSERT OR REPLACE INTO conv_messages "
            "(conversation_id, seq, role, content, content_len, truncated, tool_name, "
            " tool_call_id, tool_calls, reasoning, ts, active, compacted, msg_hash, origin) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        con.commit()
        return len(rows)
    finally:
        if own:
            con.close()


#: Sorts the Conversations history table offers. ``COALESCE`` because a migrated
#: row may carry only ``started_at``/``ended_at`` (from the Hermes session) while a
#: live row carries ``first_seen``/``last_seen`` (from the activity window).
HISTORY_SORTS = (
    ("newest", "Newest activity first",
     "COALESCE(last_seen, ended_at, first_seen, started_at, '') DESC, conversation_id DESC"),
    ("oldest", "Oldest activity first",
     "COALESCE(last_seen, ended_at, first_seen, started_at, '') ASC, conversation_id DESC"),
    ("messages", "Most messages first", "message_count DESC, conversation_id DESC"),
    ("calls", "Most tool calls first", "tool_call_count DESC, conversation_id DESC"),
)


def list_conversations(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """A page of the conversation history, newest activity first.

    Reads only LCP's own tables — this is the query behind the history table, and
    it must not depend on the Hermes schema it was migrated from.
    """
    from ..ui import tables

    params = params or {}
    profile_filter = str(params.get("profile") or "").strip()
    q = str(params.get("q") or "").strip()

    con = _connect()
    try:
        where = "WHERE 1=1"
        args: list[Any] = []
        if profile_filter:
            where += " AND profile = ?"
            args.append(profile_filter)
        if q:
            where += " AND (name LIKE ? OR conversation_id LIKE ? OR summary LIKE ?)"
            args += ["%%%s%%" % q] * 3
        total = con.execute("SELECT COUNT(*) FROM conversations " + where,
                            args).fetchone()[0]
        st = tables.state(params, HISTORY_SORTS, total)
        limit_sql, limit_args = tables.limit_clause(st)
        rows = con.execute(
            "SELECT conversation_id, name, summary, profile, model, source, "
            "message_count, active_message_count, tool_call_count, first_seen, "
            "last_seen, started_at, ended_at, calls, tokens, cost, errors "
            "FROM conversations " + where + " ORDER BY " + st["order_sql"] + limit_sql,
            args + limit_args).fetchall()
        conversations = [{
            "id": r["conversation_id"],
            "name": r["name"],
            "summary": r["summary"],
            "profile": r["profile"],
            "model": r["model"],
            "source": r["source"],
            # These three are NULL, not zero, on a rollup-only conversation — one
            # imported from the request log with no transcript stored (1,383 of
            # 5,051 here). `or 0` turned "we never counted" into "there were none",
            # so the table said 0 messages beside a summary describing 16 tool
            # calls. Unknown and zero are different facts; only the template may
            # collapse them, and it renders unknown as an em dash.
            "messages": r["message_count"],
            "active_messages": r["active_message_count"],
            "tool_calls": r["tool_call_count"],
            "started_at": r["first_seen"] or r["started_at"],
            "last_at": r["last_seen"] or r["ended_at"],
            "calls": r["calls"] or 0,
            "tokens": r["tokens"] or 0,
            "cost": round(r["cost"] or 0.0, 6),
            "errors": r["errors"] or 0,
        } for r in rows]
    finally:
        con.close()

    return {
        "available": True,
        "conversations": conversations,
        "total": total,
        "filter": tables.filter_payload(st, total, profile=profile_filter, q=q,
                                        profiles=_conversation_profiles()),
    }


def _conversation_profiles() -> list[str]:
    con = _connect()
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT profile FROM conversations WHERE profile IS NOT NULL "
            "AND profile != '' ORDER BY profile")]
    finally:
        con.close()


def conversation_audit(conversation_id: str) -> dict[str, Any]:
    """What was actually done in a conversation — the audit view.

    Beyond the counts, this reports the tools that ran (a tool name and how often)
    and the turns that framed the work. Tool *output* is not summarised here: it is
    large, and the point of the audit view is the shape of the work, with the
    bodies available on demand.
    """
    con = _connect()
    try:
        rollup = con.execute(
            "SELECT * FROM conversations WHERE conversation_id=?",
            (conversation_id,)).fetchone()
        if rollup is None:
            return {"available": False, "id": conversation_id}

        tools = [{"tool": r[0] or "(unnamed)", "count": r[1]} for r in con.execute(
            "SELECT tool_name, COUNT(*) FROM conv_messages WHERE conversation_id=? "
            "AND role='tool' GROUP BY tool_name ORDER BY COUNT(*) DESC, tool_name",
            (conversation_id,))]

        users = con.execute(
            "SELECT COUNT(*) FROM conv_messages WHERE conversation_id=? AND role='user'",
            (conversation_id,)).fetchone()[0]
        assistants = con.execute(
            "SELECT COUNT(*) FROM conv_messages WHERE conversation_id=? AND role='assistant'",
            (conversation_id,)).fetchone()[0]
        first_ask = con.execute(
            "SELECT content, ts FROM conv_messages WHERE conversation_id=? AND role='user' "
            "AND content IS NOT NULL AND content != '' ORDER BY seq LIMIT 1",
            (conversation_id,)).fetchone()

        keys = rollup.keys()
        result = {
            "available": True,
            "id": conversation_id,
            "name": rollup["name"] if "name" in keys else None,
            "summary": rollup["summary"] if "summary" in keys else None,
            "profile": rollup["profile"] if "profile" in keys else None,
            "model": rollup["model"] if "model" in keys else None,
            "source": rollup["source"] if "source" in keys else None,
            "messages": (rollup["message_count"]
                         if "message_count" in keys else None),
            "active_messages": (rollup["active_message_count"]
                                if "active_message_count" in keys else None),
            "started_at": (rollup["first_seen"] if "first_seen" in keys else None)
                          or (rollup["started_at"] if "started_at" in keys else None),
            "last_at": (rollup["last_seen"] if "last_seen" in keys else None)
                       or (rollup["ended_at"] if "ended_at" in keys else None),
            "tokens": rollup["tokens"] if "tokens" in keys else None,
            "cost": round((rollup["cost"] if "cost" in keys else 0) or 0.0, 6),
            "errors": rollup["errors"] if "errors" in keys else None,
            "user_turns": users,
            "assistant_turns": assistants,
            # The rollup's tool count is NULL when the conversation was never
            # recounted; summing an empty tool list would report 0 instead.
            "tool_calls": (rollup["tool_call_count"]
                           if "tool_call_count" in keys and rollup["tool_call_count"] is not None
                           else (sum(t["count"] for t in tools) if tools else None)),
            "tools": tools,
            "first_ask": (first_ask["content"] if first_ask else None),
        }
        return result
    finally:
        con.close()


def read_messages(conversation_id: str, limit: int = 2000,
                  include_inactive: bool = True, newest: bool = False) -> list[dict[str, Any]]:
    """The stored transcript of a conversation, oldest first.

    ``include_inactive=False`` hides compacted context — the rows the agent no
    longer sees. Default is to include them, because the audit view has to show
    what was actually done, not what survived compaction.

    ``newest=True`` returns the LAST ``limit`` messages in reading order rather
    than the first. A conversation here runs to 5874 messages and a transcript
    that only ever shows the opening is the least useful part of it: the live
    window — what the agent is doing now — sits at the end. The slice is taken
    from the tail and re-sorted so the reading order is still oldest first.
    """
    con = _connect()
    try:
        sql = ("SELECT seq, role, content, content_len, truncated, tool_name, "
               "tool_call_id, tool_calls, reasoning, ts, active, compacted, origin, msg_hash "
               "FROM conv_messages WHERE conversation_id=?")
        if not include_inactive:
            sql += " AND active=1"
        if newest:
            sql += " ORDER BY seq DESC LIMIT ?"
            return [dict(r) for r in reversed(list(con.execute(sql, (conversation_id, limit))))]
        sql += " ORDER BY seq LIMIT ?"
        return [dict(r) for r in con.execute(sql, (conversation_id, limit))]
    finally:
        con.close()


def messages_view(conversation_id: str, limit: int = 2000,
                  include_inactive: bool = True, newest: bool = True) -> dict[str, Any]:
    """A conversation's transcript plus whether the limit cut it short.

    ``read_messages`` applies a hard LIMIT with no way for a caller to tell a
    complete transcript from a clipped one, which would let the page present a
    truncated conversation as the whole thing.

    Defaults to the NEWEST ``limit`` messages: the live window is at the end of
    the transcript, and the opening is the part already summarised by the audit.
    The window is contiguous because the tail of the transcript is ordered, so a
    clipped slice is a real window rather than a sample.
    """
    messages = read_messages(conversation_id, limit=limit + 1,
                             include_inactive=include_inactive, newest=newest)
    truncated = len(messages) > limit
    # newest=True over-fetches the oldest end of the window, so dropping the FIRST
    # row keeps the tail intact; oldest-first drops the last.
    kept = messages[1:] if (truncated and newest) else messages[:limit]
    return {"id": conversation_id, "messages": kept,
            "truncated": truncated, "total": message_count(conversation_id),
            "newest": newest}


def message_count(conversation_id: str) -> int:
    con = _connect()
    try:
        return con.execute(
            "SELECT COUNT(*) FROM conv_messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
    finally:
        con.close()


def recount_conversation(conversation_id: str,
                         con: Optional[sqlite3.Connection] = None) -> tuple[int, int]:
    """Derive a conversation's rollup from its stored messages. Idempotent.

    The counts are derived rather than incremented: an increment drifts the moment
    anything else writes a row (a re-run of the migration, a repair), whereas a
    COUNT over the indexed ``(conversation_id, seq)`` is exact and cheap.

    ``tool_call_count`` and the activity window are derived here too. They used to
    be written only by the migration, so every conversation recorded live had a
    NULL tool count and NULL ``first_seen``/``last_seen`` — which left the history
    table unsortable by activity and its date column blank for exactly the rows
    that were newest.
    """
    own = con is None
    if own:
        con = _connect()
    try:
        row = con.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN active=1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN role='tool' THEN 1 ELSE 0 END), "
            "MIN(ts), MAX(ts) "
            "FROM conv_messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        total = row[0] or 0
        active = row[1] or 0
        tools = row[2] or 0
        first_ts, last_ts = row[3], row[4]

        cols = {r[1] for r in con.execute("PRAGMA table_info(conversations)")}
        sets, args = [], []
        for column, value in (("message_count", total),
                              ("active_message_count", active),
                              ("tool_call_count", tools)):
            if column in cols:
                sets.append("%s=?" % column)
                args.append(value)
        # Only widen: an explicit session window from the migration stays in place
        # when it is broader than the messages that survived into the store.
        if first_ts and "first_seen" in cols:
            sets.append("first_seen=CASE WHEN first_seen IS NULL OR ? < first_seen "
                        "THEN ? ELSE first_seen END")
            args += [first_ts, first_ts]
        if last_ts and "last_seen" in cols:
            sets.append("last_seen=CASE WHEN last_seen IS NULL OR ? > last_seen "
                        "THEN ? ELSE last_seen END")
            args += [last_ts, last_ts]
        if sets:
            con.execute("UPDATE conversations SET %s WHERE conversation_id=?"
                        % ", ".join(sets), args + [conversation_id])
            con.commit()
        return total, active
    finally:
        if own:
            con.close()


def _extract_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    """The messages of a chat-completions request, normalised for storage.

    ``system`` turns are dropped: every request carries the same standing prompt,
    it is not part of the conversation, and keeping it would make each
    conversation's first row a copy of the agent's whole instruction set.
    """
    out: list[dict[str, Any]] = []
    for m in (body.get("messages") or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            continue
        content = m.get("content")
        if isinstance(content, list):
            # Multimodal content parts — keep the text parts, note the rest.
            parts = [p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            extra = len(content) - len(parts)
            content = "".join(parts) + ("\n[%d non-text part(s)]" % extra if extra else "")
        out.append({
            "role": role,
            "content": content,
            "tool_name": m.get("name") or m.get("tool_name"),
            "tool_call_id": m.get("tool_call_id"),
            "tool_calls": (
                _dump(m.get("tool_calls")) if m.get("tool_calls") else None
            ),
        })
    return out


def _dump(value: Any) -> str:
    import json
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def capture_request(conversation_id: Optional[str], body: dict[str, Any],
                    reply: Optional[str] = None, *, ts: Optional[str] = None,
                    profile: Optional[str] = None,
                    model: Optional[str] = None) -> int:
    """Record a served request against its conversation. Fail-open by design.

    Appends the request's own messages (system excluded) and, when the reply is
    known, the assistant turn that answered it — so the live transcript is
    current rather than one request behind.

    Returns rows written; **0** when there is nothing to do, and it never raises:
    an unidentified or unstoreable request is a missing row, not a failed request.
    """
    if not conversation_id:
        return 0
    try:
        # Default the timestamp rather than leaving it None: the handler does not
        # pass one, and a None here left every live conversation with no activity
        # window at all (the rollup window is widened from the message timestamps).
        if ts is None:
            ts = datetime.now(timezone.utc).isoformat()
        ensure_store()
        messages = _extract_messages(body)
        if reply:
            messages.append({"role": "assistant", "content": reply, "ts": ts})
        for m in messages:
            m.setdefault("ts", ts)
        written = append_messages(conversation_id, messages, origin="capture")
        if written:
            upsert_conversation(conversation_id, profile=profile, model=model,
                                source="live", last_message_ts=ts)
            recount_conversation(conversation_id)
        return written
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger("lcp.conv_store").warning(
            "capture_failed", extra={"conversation_id": conversation_id}, exc_info=True
        )
        return 0


def upsert_conversation(cid: str, *, profile: Optional[str] = None,
                        model: Optional[str] = None, name: Optional[str] = None,
                        source: Optional[str] = None,
                        started_at: Optional[str] = None,
                        ended_at: Optional[str] = None,
                        message_count: Optional[int] = None,
                        active_message_count: Optional[int] = None,
                        tool_call_count: Optional[int] = None,
                        input_tokens: Optional[int] = None,
                        output_tokens: Optional[int] = None,
                        last_message_ts: Optional[str] = None,
                        con: Optional[sqlite3.Connection] = None) -> None:
    """Insert or update the conversation rollup, never overwriting a set value with NULL.

    ``conversations`` already exists (``work_conversations.ensure_schema``); the
    rollup columns this writes were added by alembic 024. Missing columns are
    tolerated so the store still works on a database that predates 024.
    """
    own = con is None
    if own:
        con = _connect()
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(conversations)")}
        wanted = [("profile", profile), ("model", model), ("name", name),
                  ("source", source), ("started_at", started_at),
                  ("ended_at", ended_at), ("message_count", message_count),
                  ("active_message_count", active_message_count),
                  ("tool_call_count", tool_call_count),
                  ("input_tokens", input_tokens), ("output_tokens", output_tokens)]
        present = [(c, v) for c, v in wanted if c in cols and v is not None]

        colnames = [c for c, _ in present]
        values = [v for _, v in present]

        if "conversation_id" not in cols:
            return
        con.execute(
            "INSERT INTO conversations (conversation_id%s) VALUES (?%s) "
            "ON CONFLICT(conversation_id) DO NOTHING"
            % ("".join(", " + c for c in colnames), ", ?" * len(colnames)),
            [cid] + values,
        )
        if colnames:
            # No COALESCE here: ``present`` already dropped every None, so a column
            # that arrived unset is simply absent from this SET list and its stored
            # value is left alone. Wrapping these in COALESCE(?, col) reads as if it
            # were the thing protecting stored values, when the protection is the
            # None filter above — verified by mutating that filter, not this line.
            con.execute(
                "UPDATE conversations SET %s WHERE conversation_id=?"
                % ", ".join("%s=?" % c for c in colnames),
                values + [cid],
            )

        # Widen the activity window from the newest message ts. `first_seen`/`last_seen`
        # are also maintained by sync_conversations() from `requests`; this only ever
        # widens them, so the two writers cannot fight over which is "later".
        if last_message_ts and "first_seen" in cols:
            con.execute(
                "UPDATE conversations SET "
                "first_seen=CASE WHEN first_seen IS NULL OR ? < first_seen THEN ? ELSE first_seen END "
                "WHERE conversation_id=?",
                [last_message_ts, last_message_ts, cid],
            )
        if last_message_ts and "last_seen" in cols:
            con.execute(
                "UPDATE conversations SET "
                "last_seen=CASE WHEN last_seen IS NULL OR ? > last_seen THEN ? ELSE last_seen END "
                "WHERE conversation_id=?",
                [last_message_ts, last_message_ts, cid],
            )
        con.commit()
    finally:
        if own:
            con.close()
