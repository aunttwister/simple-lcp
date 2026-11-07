"""In-flight request registry.

The ``requests`` table records a request only when it FINISHES: ``record_cost()``
writes the row — and its ``latency_ms`` — after the reply has already left. A
call that is still running therefore has **no row anywhere**, which makes the DB
useless for the one question an operator watching a busy box actually asks:
*what is being worked on right now?*

This module keeps the live set in memory instead. The server is a single
``ThreadingHTTPServer`` process (see ``server.py``), so an in-process registry is
the whole state; ``_LOCK`` is what makes it safe across the handler threads.

Deliberately **not** persisted. An in-flight entry is only true while the process
that owns it is alive, and a restart ends every request it was tracking — a
surviving row would be a ghost claiming work that no longer exists. For the same
reason ``sweep()`` drops entries older than ``MAX_AGE_S``: a thread that dies
between ``begin()`` and ``end()`` must not leave a permanent phantom on the page.

Nothing here may raise into a request path. Every public function is total —
the registry is an observation aid, and an observation aid that can fail a
request is worse than no aid at all.
"""

from __future__ import annotations

import threading
import time
import uuid

#: Entries older than this are dropped by ``sweep()``. Chosen well above the
#: longest realistic call (LCP's own upstream read timeout ceiling is 900 s) so
#: it can never cut short a genuinely-running request, while still bounding a
#: leak to hours rather than forever.
MAX_AGE_S = 6 * 3600

_LOCK = threading.Lock()
_ENTRIES: dict[str, dict] = {}


def begin(profile: str, model: str, provider: str, *,
          prompt_tokens: int = 0,
          stream: bool = False,
          conversation_id: str | None = None,
          tools: int = 0) -> str:
    """Register a request as started. Returns a token for :func:`end`.

    ``provider`` is the provider the request is *first sent to* — for a chain,
    the primary step. That is the only fact the caller knows at ``begin()`` time;
    once the chain engages, :func:`update` moves the entry to the step that is
    actually on the wire (CUR-329), so the page shows what is being tried now
    rather than what was tried first. The finished row in ``requests`` remains
    the authoritative record of who finally answered.
    """
    token = uuid.uuid4().hex
    entry = {
        "id": token,
        "profile": profile or "unknown",
        "model": model or "unknown",
        "provider": provider or "unknown",
        "started_at": time.time(),
        "prompt_tokens": int(prompt_tokens or 0),
        "stream": bool(stream),
        "conversation_id": conversation_id,
        "tools": int(tools or 0),
    }
    with _LOCK:
        _ENTRIES[token] = entry
    return token


def end(token: str | None) -> None:
    """Deregister a request. Safe to call with ``None`` or an unknown token."""
    if not token:
        return
    with _LOCK:
        _ENTRIES.pop(token, None)


def update(token: str | None, *, provider: str | None = None,
           model: str | None = None) -> None:
    """Move a live entry to the chain step currently on the wire (CUR-329).

    A chain registers its PRIMARY step at ``begin()``; when fallback engages the
    entry would otherwise keep claiming the primary for the whole call — the page
    showed "local-sparks" while the bill was accruing on "deepseek". The handler
    passes an ``on_step`` callback into ``try_chain`` and this is what it calls
    before each wire attempt.

    Total, like every public function here: an unknown or ``None`` token, or
    fields that arrive empty, are a no-op — a registry that could raise into the
    chain would be fixing the label at the cost of the request it labels.
    """
    if not token:
        return
    with _LOCK:
        entry = _ENTRIES.get(token)
        if entry is None:
            return
        if provider:
            entry["provider"] = str(provider)
        if model:
            entry["model"] = str(model)


def sweep(max_age_s: int = MAX_AGE_S) -> int:
    """Drop entries older than ``max_age_s``. Returns how many were dropped."""
    cutoff = time.time() - max_age_s
    with _LOCK:
        stale = [k for k, v in _ENTRIES.items() if v["started_at"] < cutoff]
        for k in stale:
            del _ENTRIES[k]
    return len(stale)


def snapshot() -> list[dict]:
    """Every in-flight request, longest-running first.

    ``elapsed_s`` is computed here rather than stored, so the caller always gets
    a value consistent with the moment of the call.
    """
    now = time.time()
    with _LOCK:
        rows = []
        for v in _ENTRIES.values():
            row = dict(v)
            row["elapsed_s"] = round(now - v["started_at"], 1)
            rows.append(row)
    rows.sort(key=lambda r: r["elapsed_s"], reverse=True)
    return rows


def clear() -> None:
    """Empty the registry. For tests — never called from the request path."""
    with _LOCK:
        _ENTRIES.clear()


def count() -> int:
    """Number of in-flight requests, without building the snapshot."""
    with _LOCK:
        return len(_ENTRIES)
