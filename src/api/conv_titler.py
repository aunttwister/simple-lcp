"""Background conversation titler/summarizer (CUR-329).

The conversation history is captured verbatim, but `conversations.name` stays
NULL for every live conversation: naming a conversation means reading it, and
nothing on the request path can afford to read one. So the history table fills
with "Untitled" rows, and search — which matches on name, id and summary —
cannot find anything by what the conversation was about.

This module closes that gap the same way the capture path does its work: out of
band. After a chat request is served the handler enqueues the conversation id;
a daemon thread (started lazily on the first enqueue) dequeues ids and asks the
gateway's own chain head to produce a short title and summary. The worker then
writes them through `conv_store.upsert_conversation(name=..., summary=...)`,
which only ever fills or explicitly replaces — the None-safety in that writer
means a title-less upsert cannot erase a name someone set earlier.

Design constraints, all deliberate:

* **Fail-open.** Titling is bookkeeping. Every failure degrades to an untitled
  conversation, exactly what existed before this module; nothing may log so
  loudly it becomes an operational burden.
* **Never on the request path.** `enqueue()` is a put on a bounded deque with
  dedup and drop-oldest overflow — microseconds, and immune to the worker's
  health. If the queue is full the request loses nothing: the conversation
  stays untitled, as it already was.
* **At most one title per conversation per window.** The skip check reads the
  stored name and drops already-titled ids, so a long-running agent session
  (one conversation, hundreds of requests) costs one call, not hundreds.
* **Stdlib only.** No new dependency; the call goes out through the same
  `forward_request` plumbing the proxy itself uses.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from .logging_config import get_logger

logger = get_logger("lcp.conv_titler")

#: Bounded queue: a burst of requests cannot grow the process without bound,
#: and on overflow the OLDEST pending id is dropped — the newest conversations
#: are the ones still changing, so they are worth keeping.
QUEUE_MAX = 128

#: Minimum transcript length before titling is worth an upstream call. A single
#: "hi" produces a worse title than no title.
MIN_MESSAGES = 3

#: Cap on how much transcript is sent: the title is derived from the SHAPE of
#: the conversation, and the opening turns carry it. Capped in characters, not
#: messages, because one tool result can outweigh fifty turns.
TRANSCRIPT_CHAR_CAP = 8000

_MAX_NAME = 80
_MAX_SUMMARY = 240

_pending: deque = deque(maxlen=QUEUE_MAX)
_pending_set: set = set()
_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_started = False

# Scheduler (interval_hours) + manual "run now" pass. Started lazily via
# ensure_scheduler(); lives alongside the per-request enqueue worker.
_sched: Optional[threading.Thread] = None
_sched_stop = threading.Event()
_sched_lock = threading.Lock()
_LAST_RUN_AT: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue(conversation_id: Optional[str], *, config=None) -> None:
    """Queue a conversation id for background titling. Never raises, never blocks.

    Safe to call from the request path with anything, including None: a
    request without a conversation id has nothing to title, and an enqueue
    failure costs only the title. When the feature is switched off in config
    the id is dropped here — the worker never becomes a hidden cost of a
    feature the operator disabled (the worker re-checks the flag too, so a
    runtime off-flip still stops new upstream calls).
    """
    global _worker, _started
    if not conversation_id:
        return
    try:
        if config is not None and _enabled(config) is False:
            return
        with _lock:
            if conversation_id in _pending_set:
                return
            _pending_set.add(conversation_id)
            _pending.append(conversation_id)
            already = _started
            _started = True
        if not already:
            _worker = threading.Thread(
                target=_run_worker, name="lcp-conv-titler", daemon=True)
            _worker.start()
    except Exception:  # noqa: BLE001 — bookkeeping must not fail a request
        logger.debug("conv_titler_enqueue_failed")


def _config_section(config) -> dict:
    """The conversation_titling config section, tolerant of duck-typed configs."""
    try:
        section = config.conversation_titling
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def interval_hours(config) -> int:
    """How often the scheduled titling pass runs. Config, default 24, min 1."""
    try:
        v = int(_config_section(config).get("interval_hours", 24))
        return v if v >= 1 else 24
    except Exception:
        return 24


def scope(config) -> str:
    """Which conversations a pass titles. Config, default 'untitled'."""
    try:
        s = str(_config_section(config).get("scope", "untitled")).strip().lower()
        return s if s in ("untitled", "all") else "untitled"
    except Exception:
        return "untitled"


def _enabled(config) -> bool:
    """The feature flag, tolerant of duck-typed configs (tests, mocks).

    An unreadable flag means ENABLED, not disabled: the flag gates an
    improvement over the pre-CUR-329 status quo, and failing closed would
    silently disable titling because of a config object that merely does not
    carry the section yet.
    """
    try:
        section = config.conversation_titling
        return bool(section.get("enabled", True)) if section else True
    except Exception:  # noqa: BLE001 — see above: fail open
        return True


def pending_count() -> int:
    """How many ids are waiting. Test/diagnostic surface only."""
    with _lock:
        return len(_pending)


def _run_worker() -> None:
    """Drain the queue forever. One conversation at a time: titling an agent
    session is inherently serial work (each call is an upstream round trip),
    and a pool would only add concurrency the chain head did not ask for."""
    while True:
        try:
            cid = _take_next()
            if cid is None:
                time.sleep(2.0)
                continue
            _title_one(cid)
        except Exception:  # noqa: BLE001 — the worker must outlive any one failure
            logger.warning("conv_titler_worker_error")
            time.sleep(2.0)


def _take_next() -> Optional[str]:
    with _lock:
        while _pending:
            cid = _pending.popleft()
            _pending_set.discard(cid)
            if _should_title(cid):
                return cid
            # else: already titled (or too small) — drop silently
    return None


def _should_title(cid: str) -> bool:
    """Skip conversations that already carry a recent name.

    Checked twice (also in `_title_one`'s write guard) because the queue can
    hold an id for seconds while the worker finishes the previous one; the
    cheap DB read here keeps most of those from ever becoming an upstream
    call.
    """
    try:
        from . import conv_store
        info = conv_store.conversation_audit(cid)
        if not info.get("available"):
            return False
        if info.get("name"):
            # A name exists; treat it as fresh enough and skip.
            return False
        return (info.get("messages") or 0) >= MIN_MESSAGES
    except Exception:  # noqa: BLE001 — a read failure means try, not skip
        return True


def _build_transcript(cid: str) -> str:
    """A character-capped, oldest-first transcript excerpt for the title call."""
    from . import conv_store
    msgs = conv_store.read_messages(cid, limit=40, include_inactive=False)
    out: list[str] = []
    total = 0
    for m in msgs:
        role = m.get("role") or "?"
        body = (m.get("content") or "").strip()
        if not body:
            continue
        piece = "%s: %s" % (role, body[:800])
        if total + len(piece) > TRANSCRIPT_CHAR_CAP:
            break
        out.append(piece)
        total += len(piece)
    return "\n".join(out)


#: The one prompt the titler sends. Deliberately short and format-strict: the
#: output is parsed into exactly two lines and anything else is discarded, so a
#: chatty model produces a discarded answer, not a corrupted row.
_TITLE_PROMPT = (
    "You name conversations for an operations console. Read the transcript "
    "excerpt and reply with EXACTLY two lines and nothing else:\n"
    "NAME: <a concrete title, max 80 chars, no quotes, no period>\n"
    "SUMMARY: <one sentence, max 240 chars, what was done or asked>\n\n"
    "Transcript:\n%s"
)


def _parse_title_reply(text: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (name, summary) from the model's reply. Either may be missing."""
    name = summary = None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.upper().startswith("NAME:"):
            name = line[5:].strip().strip('"').strip()
        elif line.upper().startswith("SUMMARY:"):
            summary = line[8:].strip().strip('"').strip()
    return (
        name[:_MAX_NAME] if name else None,
        summary[:_MAX_SUMMARY] if summary else None,
    )


def _resolve_step(config) -> Optional[dict]:
    """The chain step the titler calls: the configured model's step, or the
    default profile's chain head.

    A configured ``model`` names a gateway logical model; the step is found by
    scanning the profiles' chains for it, because the chain step carries the
    provider + base_url that model is reachable at. No match (or no default
    profile) means no titler — returned as None rather than guessed.
    """
    section = config.conversation_titling or {}
    wanted_model = (section.get("model") or "").strip() or None
    profiles = config.profiles or {}
    if wanted_model:
        for prof in profiles.values():
            for step in (prof.get("chain") or []):
                if step.get("model") == wanted_model:
                    return dict(step)
        logger.info("conv_titler_model_not_in_any_chain", model=wanted_model)
        return None
    default_profile = (config.server or {}).get("default_profile") or ""
    prof = profiles.get(default_profile) or {}
    chain = prof.get("chain") or []
    return dict(chain[0]) if chain else None


def _title_one(cid: str, *, force: bool = False) -> None:
    """Ask the chain head for a title and store it. Fail-open end to end.

    ``force=True`` backs the ``scope=all`` re-title: it bypasses the
    already-titled skip so a fresh pass can regenerate every conversation.
    """
    try:
        from . import conv_store
        from .config import get_config
        from .request_pipeline import forward_request

        config = get_config()
        if not _enabled(config):
            return

        # Re-check under the same condition the dequeue used: the audit read is
        # the last chance to drop an id whose conversation got titled meanwhile.
        # force=True (scope=all re-title) deliberately skips this.
        if not force and not _should_title(cid):
            return
        transcript = _build_transcript(cid)
        if not transcript:
            return

        step = _resolve_step(config)
        if not step or not step.get("model"):
            logger.info("conv_titler_no_step")
            return
        step.setdefault("base_url",
                        (config.providers.get(step.get("provider"), {})
                         .get("api_base") or ""))
        body = {
            "model": step["model"],
            "messages": [{"role": "user", "content": _TITLE_PROMPT % transcript}],
            "max_tokens": 200,
            "temperature": 0.2,
        }
        resp, status = forward_request(step, body, config)
        if status != 200:
            logger.info("conv_titler_upstream_status", status=status)
            return
        text = _reply_text(resp)
        name, summary = _parse_title_reply(text)
        if not name and not summary:
            return
        conv_store.upsert_conversation(cid, name=name, summary=summary,
                                       last_message_ts=_now_iso())
        logger.info("conv_titler_titled", conversation_id=cid,
                    named=bool(name), summarised=bool(summary))
    except Exception:  # noqa: BLE001 — titling is bookkeeping, never a failure
        logger.debug("conv_titler_failed", conversation_id=cid)


def _reply_text(resp: dict) -> str:
    """The assistant text of a chat-completions response body, best-effort."""
    try:
        choices = resp.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            return "".join(p.get("text", "") for p in content
                           if isinstance(p, dict))
        return content or ""
    except Exception:  # noqa: BLE001
        return ""


# ── Scheduled / manual titling pass ─────────────────────────────────────────
def _candidate_ids(scope_value: str, limit: int = 500) -> list[str]:
    """Conversation ids to title in one pass.

    scope='untitled' -> only rows missing a name OR a summary.
    scope='all'      -> every conversation (fresh re-title). Bounded by `limit`
    so a single scheduled pass cannot run away.
    """
    from . import conv_store
    rows = conv_store.list_conversations({"limit": limit})
    items = rows.get("conversations") or []
    out = []
    for c in items:
        cid = c.get("id")
        if not cid:
            continue
        if scope_value == "untitled":
            if c.get("name") and c.get("summary"):
                continue
        out.append(cid)
    return out


def run_titling_pass(scope_value: Optional[str] = None, limit: int = 500) -> dict:
    """Run one titling pass. Returns {titled, scanned, scope, at}.

    scope_value None -> config.scope(). Uses the fail-open _title_one per id
    (out-of-band; never raises; a failure just leaves that conversation
    untitled). ``scope='all'`` forces a re-title via _title_one(force=True).
    """
    global _LAST_RUN_AT
    try:
        from .config import get_config
        cfg = get_config()
        eff_scope = scope_value or scope(cfg) or "untitled"
        ids = _candidate_ids(eff_scope, limit)
        titled = 0
        for cid in ids:
            try:
                _title_one(cid, force=(eff_scope == "all"))
                titled += 1
            except Exception:  # noqa: BLE001 — per-id, out-of-band
                continue
        _LAST_RUN_AT = _now_iso()
        return {"titled": titled, "scanned": len(ids),
                "scope": eff_scope, "at": _LAST_RUN_AT}
    except Exception as exc:  # noqa: BLE001 — never fail the caller (a request)
        return {"error": str(exc), "titled": 0, "scanned": 0}


def _scheduler_loop() -> None:
    """Every interval_hours, run a titling pass. Polls the stop flag so a
    shutdown or interval change is prompt even on long intervals."""
    while not _sched_stop.is_set():
        try:
            from .config import get_config
            cfg = get_config()
            interval = max(1, interval_hours(cfg))
        except Exception:
            interval = 24
        waited = 0
        step = min(30, max(1, interval * 3600))
        while waited < interval * 3600 and not _sched_stop.is_set():
            _sched_stop.wait(step)
            waited += step
        if _sched_stop.is_set():
            break
        try:
            run_titling_pass()
        except Exception:  # noqa: BLE001 — the loop must outlive one bad pass
            pass


def ensure_scheduler() -> None:
    """Start the scheduler thread if not already running. Safe at startup and
    from the settings POST (after an interval change) — it self-governs."""
    global _sched
    with _sched_lock:
        if _sched and _sched.is_alive():
            return
        _sched_stop.clear()
        _sched = threading.Thread(target=_scheduler_loop,
                                  name="lcp-conv-titler-scheduler",
                                  daemon=True)
        _sched.start()


def last_run_at() -> Optional[str]:
    """ISO string of the last completed pass (for the /settings status)."""
    return _LAST_RUN_AT


def reset_for_tests() -> None:
    """Empty the queue and forget the worker + scheduler. Tests only."""
    global _worker, _started, _sched, _LAST_RUN_AT
    with _lock:
        _pending.clear()
        _pending_set.clear()
        _started = False
    _worker = None
    _sched_stop.set()
    _sched = None
    _LAST_RUN_AT = None
