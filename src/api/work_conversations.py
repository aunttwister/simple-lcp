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
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

# The shared log-table module — owns ordering (newest first by default) and the
# pagination window for every log view, so no view invents its own ORDER BY.
from ..ui import tables

# conversation_id values are opaque strings: either the client's session
# header or a deterministic backfill id like "c<10 hex>".
_BURST_GAP_SECONDS = 300
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


def _burstable(row: sqlite3.Row, prev: Optional[sqlite3.Row],
               gap: float) -> bool:
    if prev is None:
        return True
    if row["profile"] != prev["profile"] or row["model"] != prev["model"]:
        return True
    return gap > _BURST_GAP_SECONDS


def _backfill_id(profile: str, model: str, first_ts: str) -> str:
    import hashlib
    return "c" + hashlib.sha1(
        ("%s|%s|%s" % (profile, model, first_ts)).encode()).hexdigest()[:10]


def ensure_schema() -> None:
    """Idempotent ALTER: add conversation_id to both tables + the persisted
    conversations table (name/summary generated once and stored)."""
    con = _connect()
    try:
        for table in ("requests", "routing_decisions"):
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


def _generate_name(head: Optional[str], cid: str,
                   task: Optional[str] = None, last_seen: Optional[str] = None) -> str:
    """Deterministic generated name: leading words of the first user turn (or
    assistant text when the window has no user turn), else the routing task
    label plus the activity date, else a bare fallback. Markdown decoration
    is stripped so names never carry `**`/backtick artifacts."""
    if head:
        clean = re.sub(r"[*_`~#>]", "", head)
        words = re.sub(r"\s{2,}", " ", clean).strip().split()[:7]
        if words:
            return " ".join(words)[:48]
    if task:
        date = (last_seen or "")[:10]
        return "%s %s" % (task, date) if date else task
    return "Conversation %s" % cid


def sync_conversations(force: bool = False) -> Dict[str, int]:
    """Upsert persisted conversations from requests; generate name/summary once.

    ``force`` regenerates name + summary for EVERY conversation (used when the
    generation logic improves — e.g. the assistant-text fallback — and the
    operator explicitly wants existing rows upgraded). By default, name +
    summary are written only on first creation so operator-made names survive
    syncs.
    """
    ensure_schema()
    con = _connect()
    created = updated = 0
    try:
        rows = con.execute(
            """
            SELECT conversation_id, profile, model,
                   COUNT(*) AS calls,
                   SUM(prompt_tokens + completion_tokens) AS tokens,
                   SUM(cost) AS cost,
                   SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS errors,
                   MIN(timestamp) AS first_seen,
                   MAX(timestamp) AS last_seen
            FROM requests WHERE conversation_id IS NOT NULL
            GROUP BY conversation_id, profile, model
            """
        ).fetchall()
        for r in rows:
            cid = r["conversation_id"]
            existing = con.execute(
                "SELECT name, summary FROM conversations WHERE conversation_id=?",
                (cid,)).fetchone()

            # Optional: refresh the generated fields (explicit upgrade).
            refresh_gen = force

            if existing is not None and not refresh_gen:
                con.execute(
                    """
                    UPDATE conversations SET profile=?, model=?, calls=?, tokens=?,
                        cost=?, errors=?, first_seen=?, last_seen=?
                    WHERE conversation_id=?
                    """, (r["profile"], r["model"], r["calls"], r["tokens"] or 0,
                          r["cost"] or 0.0, r["errors"] or 0,
                          r["first_seen"], r["last_seen"], cid))
                updated += 1
                continue

            head = None
            task = None
            route = con.execute(
                "SELECT task FROM routing_decisions "
                "WHERE conversation_id=? ORDER BY ts DESC LIMIT 1", (cid,)).fetchone()
            if route:
                task = route["task"]
            # `head` stays None: the transcript that used to supply it was the
            # `conversation_json` blob (73% of the DB, dropped in M7). Measured
            # against all 1,383 conversations, `intent_text` is NOT a usable
            # substitute — it is a mid-thread instruction fragment, not the
            # opening ask: 55% of conversations have no text at all, 29% carry a
            # template tail, 16% a usable fragment ("xapp-1-A0BUF…", "what is
            # the"). So a new conversation is named from its task and date, and
            # the descriptive name/summary stays the job of the LLM pass over the
            # harness's own session stores (`summary_source='llm'`), which is
            # where every stored name came from. sync_conversations() without
            # force never rewrites a stored name.
            name = _generate_name(head, cid, task, r["last_seen"])
            summary = "%s · %d calls · $%.4f" % (
                (head or "no user or assistant text captured")[:_CONV_SUMMARY_CAP],
                r["calls"], r["cost"] or 0.0)
            if existing is not None:
                con.execute(
                    """
                    UPDATE conversations SET name=?, summary=?, profile=?, model=?,
                        calls=?, tokens=?, cost=?, errors=?, first_seen=?,
                        last_seen=?, generated_at=?
                    WHERE conversation_id=?
                    """, (name, summary, r["profile"], r["model"], r["calls"],
                          r["tokens"] or 0, r["cost"] or 0.0, r["errors"] or 0,
                          r["first_seen"], r["last_seen"],
                          time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), cid))
                updated += 1
                continue
            con.execute(
                """
                INSERT INTO conversations
                    (conversation_id, name, summary, profile, model,
                     first_seen, last_seen, calls, tokens, cost, errors,
                     generated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (cid, name, summary, r["profile"], r["model"],
                      r["first_seen"], r["last_seen"], r["calls"],
                      r["tokens"] or 0, r["cost"] or 0.0, r["errors"] or 0,
                      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            created += 1
        con.commit()
    finally:
        con.close()
    return {"created": created, "updated": updated}


def _last_backfill_ts() -> Optional[str]:
    try:
        con = _connect()
        row = con.execute(
            "SELECT value FROM settings WHERE key='convo_backfill_ts'").fetchone()
        con.close()
        return row["value"] if row else None
    except sqlite3.Error:
        return None


def _set_last_backfill_ts(ts: str) -> None:
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        try:
            # Some installs have settings.updated_at NOT NULL — satisfy it,
            # else fall back to the minimal row shape.
            con.execute(
                "INSERT OR REPLACE INTO settings(key, value, updated_at) "
                "VALUES(?,?,?)", ("convo_backfill_ts", ts, ts))
        except sqlite3.Error:
            con.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)",
                ("convo_backfill_ts", ts))
        con.commit()
    finally:
        con.close()


def backfill_conversations(vacuum: bool = False) -> Dict[str, int]:
    """Assign conversation_id to legacy rows (NULL only). Returns counts.

    Requests: burst grouping over (profile, model) ordered by timestamp.
    Routing decisions: joined to whichever conversation is active for their
    profile within the burst window; orphaned rows get their own ids.
    """
    ensure_schema()
    stamp_requests = 0
    stamp_routing = 0

    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, timestamp, profile, model FROM requests "
            "WHERE conversation_id IS NULL ORDER BY profile, model, timestamp"
        ).fetchall()
        prev: Optional[sqlite3.Row] = None
        cur_id: Optional[str] = None
        for r in rows:
            gap = 0.0
            if prev is not None:
                try:
                    gap = (__import__("datetime").datetime.fromisoformat(
                        r["timestamp"].replace("Z", "+00:00")) -
                        __import__("datetime").datetime.fromisoformat(
                            prev["timestamp"].replace("Z", "+00:00"))).total_seconds()
                except ValueError:
                    gap = _BURST_GAP_SECONDS + 1
            if _burstable(r, prev, gap):
                cur_id = _backfill_id(r["profile"], r["model"], r["timestamp"])
            con.execute("UPDATE requests SET conversation_id=? WHERE id=?",
                        (cur_id, r["id"]))
            stamp_requests += 1
            prev = r

        con.commit()

        # routing decisions: attach to the conversation whose request window
        # covers them (same profile). Requests are backfilled now, so look up
        # by nearest request conversation within a sliding join.
        rt = con.execute(
            "SELECT id, ts, profile FROM routing_decisions "
            "WHERE conversation_id IS NULL ORDER BY profile, ts"
        ).fetchall()
        for r in rt:
            match = con.execute(
                """
                SELECT conversation_id FROM requests
                WHERE profile = ? AND conversation_id IS NOT NULL
                  AND datetime(timestamp) BETWEEN
                      datetime(?, '-10 minutes') AND datetime(?, '+10 minutes')
                ORDER BY abs(julianday(timestamp) - julianday(?)) LIMIT 1
                """, (r["profile"], r["ts"], r["ts"], r["ts"])).fetchone()
            conv = match["conversation_id"] if match else None
            if conv is None:
                # synthetic conversation for the orphan
                first_ts = r["ts"]
                conv = _backfill_id(r["profile"], "__routing__", first_ts)
            con.execute("UPDATE routing_decisions SET conversation_id=? WHERE id=?",
                        (conv, r["id"]))
            stamp_routing += 1
        con.commit()
    finally:
        con.close()

    _set_last_backfill_ts(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return {"requests": stamp_requests, "routing": stamp_routing}


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


_CONV_SUMMARY_CAP = 240


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


_SYNC_STALE_SECONDS = 300


def _set_sync_ts(ts: Optional[str] = None) -> None:
    con = _connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        ts = ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            con.execute("INSERT OR REPLACE INTO settings(key, value, updated_at) "
                        "VALUES(?,?,?)", ("convo_sync_ts", ts, ts))
        except sqlite3.Error:
            con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)",
                        ("convo_sync_ts", ts))
        con.commit()
    finally:
        con.close()


def _maybe_sync() -> None:
    """Lazy sync: refresh the persisted conversations table when stale."""
    from datetime import datetime
    try:
        con = _connect()
        row = con.execute("SELECT value FROM settings "
                          "WHERE key='convo_sync_ts'").fetchone()
        con.close()
        if row:
            try:
                last = datetime.fromisoformat(row["value"])
                if time.time() - last.timestamp() < _SYNC_STALE_SECONDS:
                    return  # fresh enough
            except (ValueError, AttributeError, OSError):
                pass
        sync_conversations()
        _set_sync_ts()
    except sqlite3.Error:
        pass


def conversations_view(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Paginated conversation list, read from the PERSISTED conversations
    table (name + summary generated once by sync_conversations).

    Newest activity first by default. ``?qid=<conversation_id>`` narrows the
    list to one conversation — that is what the Requests tab's conversation
    link targets, so the link lands on the conversation it names.
    """
    try:
        _maybe_sync()
    except Exception:  # noqa: BLE001 — view must not die on sync failure
        pass

    params = params or {}
    profile_filter = str(params.get("profile") or "").strip()
    qid = str(params.get("qid") or "").strip()

    con = _connect()
    try:
        where = "WHERE 1=1"
        args: List[Any] = []
        if profile_filter:
            where += " AND profile = ?"
            args.append(profile_filter)
        if qid:
            where += " AND conversation_id = ?"
            args.append(qid)

        total = con.execute("SELECT COUNT(*) FROM conversations " + where,
                            args).fetchone()[0]
        st = tables.state(params, CONVERSATION_SORTS, total)
        limit_sql, limit_args = tables.limit_clause(st)
        rows = con.execute(
            "SELECT conversation_id, name, summary, profile, model, calls, "
            "tokens, cost, errors, first_seen, last_seen "
            "FROM conversations " + where +
            " ORDER BY " + st["order_sql"] + limit_sql,
            args + limit_args).fetchall()

        conversations = []
        for r in rows:
            cid = r["conversation_id"]
            route = con.execute(
                "SELECT task FROM routing_decisions WHERE conversation_id=? "
                "ORDER BY ts DESC LIMIT 1", (cid,)).fetchone()
            conversations.append({
                "id": cid,
                "name": r["name"],
                "summary": r["summary"],
                "profile": r["profile"],
                "model": r["model"],
                "calls": r["calls"],
                "tokens": r["tokens"] or 0,
                "cost": round(r["cost"] or 0.0, 6),
                "errors": r["errors"] or 0,
                "started_at": r["first_seen"],
                "last_at": r["last_seen"],
                "task": route["task"] if route else None,
            })
        con.close()
    except sqlite3.Error:
        con.close()
        raise

    return {
        "available": True,
        "conversations": conversations,
        "total": total,
        "filter": tables.filter_payload(st, total, profile=profile_filter,
                                        qid=qid, profiles=_profiles()),
    }


def _profiles() -> List[str]:
    con = _connect()
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT profile FROM requests WHERE conversation_id IS NOT NULL "
            "ORDER BY profile")]
    finally:
        con.close()


def conversation_detail(cid: str, limit: int = 500) -> Dict[str, Any]:
    """The chronological sequence of a conversation: requests + routing."""
    ensure_schema()
    con = _connect()
    try:
        events: List[Dict[str, Any]] = []
        for r in con.execute(
                "SELECT * FROM requests WHERE conversation_id=? ORDER BY timestamp",
                (cid,)):
            events.append({
                "kind": "request", "ts": r["timestamp"],
                "provider": r["provider"], "model": r["model"],
                "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"],
                "cost": r["cost"], "latency_ms": r["latency_ms"],
                "success": r["success"], "error_type": r["error_type"],
                "error_detail": r["error_detail"],
            })
        for r in con.execute(
                "SELECT * FROM routing_decisions WHERE conversation_id=? "
                "ORDER BY ts", (cid,)):
            events.append({
                "kind": "routing", "ts": r["ts"],
                "task": r["task"], "policy": r["policy"], "action": r["action"],
                "provider": r["provider"], "model": r["model"], "score": r["score"],
                "note": r["note"],
            })
        events.sort(key=lambda e: e["ts"])
        totals = con.execute(
            "SELECT COUNT(*) AS calls, SUM(cost) AS cost, "
            "SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS errors "
            "FROM requests WHERE conversation_id=?", (cid,)).fetchone()
        con.close()
    finally:
        pass
    return {
        "id": cid,
        "events": events[:limit],
        "truncated": len(events) > limit,
        "calls": totals["calls"], "cost": round(totals["cost"] or 0.0, 6),
        "errors": totals["errors"] or 0,
    }