"""Work-layer data access: turn raw sources into *moments*.

A **moment** is an atomic, timestamped, attributable thing that happened to an
item. Every view in the Work section renders moments, so the envelope is fixed:

    id          stable string, so re-runs are comparable and nothing double-renders
    t           when the SUBJECT happened (``event_t``) -- not when we computed it
    computed_at our time (``asked_at``)
    subject     what it happened to -- a session, task, provider, event
    actor       who decided: an engine name, ``human``, or a rule table
    kind        ``<family>.<subtype>``
    payload     family-specific dict
    provenance  input hash / source path / row id, so any number is traceable

The split between ``t`` and ``computed_at`` is load-bearing: conflating them makes
"this decision is 3 months old" impossible to express, which is the bug that made
three board windows disagree.

Design rules enforced here (see the task's COMPONENTS.md):
  * Every categorization names its **actor**. ``by-construction`` is NOT the same
    as a classifier is NOT the same as an LLM, and the view must not blur them.
  * Every group prints its **rule and window**, and a rule that was invented
    rather than measured is labelled ``invented``.
  * The funnel is computed, never asserted: ``considered`` and the per-label
    counts come from the same scan, so ``len(notices) == published`` holds by
    construction instead of by hope.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

# The shared log-table module: this view pages and orders like every other log.
from ..ui import tables

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

# The decisions ledger, as mounted read-only into the container. Overridable so
# a host-side run (tests, CLI) can point at the real file.
DEFAULT_DECISIONS_DB = "/app/work/decisions.db"

# ── Sort allow-list ─────────────────────────────────────────────────────────
# (key, label, ORDER BY fragment). First entry is the default and it is the
# newest-to-oldest ordering. The ledger keeps SUBJECT time (event_t) and
# COMPUTATION time (asked_at) apart, so both are offerable as sorts.
DECISION_SORTS: Tuple[tables.SortSpec, ...] = (
    ("newest", "Newest event first", "COALESCE(event_t, asked_at) DESC, id DESC"),
    ("oldest", "Oldest event first", "COALESCE(event_t, asked_at) ASC, id DESC"),
    ("asked", "Newest asked first", "asked_at DESC, id DESC"),
)
_ALLOWED_ORDERS = frozenset(frag for _, _, frag in DECISION_SORTS)

# Labels that mean "this reached the board".
PUBLISHED_LABELS = ("publish",)


def decisions_db_path() -> str:
    """Resolve the decisions ledger path, allowing an env override."""
    return os.environ.get("LCP_WORK_DECISIONS_DB", DEFAULT_DECISIONS_DB)


def _connect(db_path: str) -> sqlite3.Connection:
    """Open the ledger read-only so a Work view can never mutate the board."""
    uri = "file:%s?mode=ro" % db_path
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Moment builders
# ---------------------------------------------------------------------------


def decisions_available(db_path: Optional[str] = None) -> bool:
    """Whether the decisions ledger is present and queryable."""
    path = db_path or decisions_db_path()
    if not os.path.isfile(path):
        return False
    try:
        with _connect(path) as conn:
            conn.execute("SELECT 1 FROM decisions LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def decision_moments(db_path: Optional[str] = None,
                     order_sql: Optional[str] = None,
                     per: Any = "all",
                     offset: int = 0) -> List[Dict[str, Any]]:
    """Return recorded decisions as moments, newest subject-time first.

    Uses ``latest_verdict`` so a re-asked question yields ONE moment per
    (event, engine) rather than one per attempt. That is what makes the funnel
    comparable across engines -- the earlier ledger bugs came from counting
    attempts where the board counted verdicts.

    ``order_sql`` / ``per`` / ``offset`` come from the shared table module
    (:mod:`src.ui.tables`) so the ledger pages and sorts exactly like every
    other log. Omitting them returns the whole ledger, newest first — the
    funnel is computed over THAT, never over one page.
    """
    path = db_path or decisions_db_path()
    if not os.path.isfile(path):
        return []

    order = order_sql if order_sql in _ALLOWED_ORDERS else DECISION_SORTS[0][2]
    sql = ("""
        SELECT id, event_id, run, event_t, event_kind, asked_at, engine, model,
               question_hash, label, criteria, confidence, latency_ms, raw
          FROM latest_verdict
         ORDER BY %s
    """ % order)
    args: List[Any] = []
    if per != "all":
        sql += " LIMIT ? OFFSET ?"
        args = [per, offset]
    out: List[Dict[str, Any]] = []
    try:
        with _connect(path) as conn:
            for r in conn.execute(sql, args):
                out.append({
                    "id": "decision:%s" % r["id"],
                    "t": r["event_t"],
                    "computed_at": r["asked_at"],
                    # An event with no run label still has an identity.
                    "subject": r["run"] or r["event_id"],
                    "actor": r["engine"],
                    "kind": "decision.%s" % (r["label"] or "unknown"),
                    "payload": {
                        "label": r["label"],
                        "event_id": r["event_id"],
                        "event_kind": r["event_kind"],
                        "criteria": r["criteria"],
                        "confidence": r["confidence"],
                        "latency_ms": r["latency_ms"],
                        "model": r["model"],
                    },
                    "provenance": {
                        "row_id": r["id"],
                        "question_hash": r["question_hash"],
                        "source": "decisions.db",
                    },
                })
    except sqlite3.Error:
        return []
    return out


def decisions_funnel(moments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute the funnel from the SAME moments the view renders.

    ``considered`` is the moment count and ``by_label`` partitions it, so the
    invariant ``sum(by_label.values()) == considered`` holds by construction.
    ``published`` is derived, never asserted independently.
    """
    by_label: Dict[str, int] = {}
    by_engine: Dict[str, Dict[str, int]] = {}

    for m in moments:
        label = (m["payload"].get("label") or "unknown")
        engine = m.get("actor") or "unknown"
        by_label[label] = by_label.get(label, 0) + 1
        eng = by_engine.setdefault(engine, {})
        eng[label] = eng.get(label, 0) + 1

    published = sum(n for lbl, n in by_label.items() if lbl in PUBLISHED_LABELS)
    return {
        "considered": len(moments),
        "published": published,
        "suppressed": len(moments) - published,
        "by_label": by_label,
        # Per-engine breakdown exists so the invariant can be checked per engine,
        # which is the check that would have caught the original ledger bugs.
        "by_engine": by_engine,
        "published_labels": list(PUBLISHED_LABELS),
        # Declared so the view can print the rule and window, per the design.
        "rule": "count(latest_verdict rows) partitioned by label",
        "window": "all recorded decisions",
        "rule_origin": "measured",
    }


def decisions_ledger(moments: List[Dict[str, Any]],
                    total: Optional[int] = None) -> Dict[str, Any]:
    """The raw ledger view: every moment plus its provenance.

    ``count`` is the number of rows IN THIS PAYLOAD; ``total`` is the size of the
    ledger they were drawn from. When a caller pages the ledger the two differ,
    so anything that wants "how big is the ledger" must ask ``total`` — the
    server-rendered heading used to print the page size as the ledger size.
    """
    return {
        "rows": moments,
        "count": len(moments),
        "total": total if total is not None else len(moments),
        "columns": [
            ("t", "event time"),
            ("computed_at", "asked at"),
            ("subject", "subject"),
            ("actor", "engine"),
            ("kind", "kind"),
            ("payload.label", "label"),
            ("payload.confidence", "conf"),
            ("provenance.row_id", "row"),
        ],
    }


def decisions_view(db_path: Optional[str] = None,
                   params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Assemble the full Decisions view payload.

    Always returns a renderable shape -- an absent ledger yields a populated
    ``empty`` reason rather than a blank page, because an empty state is content.

    ``params`` pages the LEDGER through the shared table module (newest event
    first by default). The funnel is always computed over the whole ledger, never
    over the current page — a funnel that changed as you paged would be a lie.
    Callers that pass nothing get the whole ledger, as before.
    """
    path = db_path or decisions_db_path()
    if not decisions_available(path):
        return {
            "available": False,
            "empty": {
                "reason": "decisions ledger not found at %s" % path,
                "hint": ("Set LCP_WORK_DECISIONS_DB to the decisions ledger path, or "
                         "mount the ledger at /app/work/decisions.db."),
            },
            "funnel": None,
            "ledger": None,
            "filter": None,
        }

    all_moments = decision_moments(path)
    st = tables.state(params or {}, DECISION_SORTS, len(all_moments))
    if params:
        moments = decision_moments(path, order_sql=st["order_sql"],
                                   per=st["per"], offset=st["offset"])
    else:
        moments = all_moments
    ledger = decisions_ledger(moments, total=len(all_moments))
    return {
        "available": True,
        "empty": None if all_moments else {
            "reason": "the ledger exists but holds no decision rows",
            "hint": "the board has not judged any events yet",
        },
        "funnel": decisions_funnel(all_moments),
        "ledger": ledger,
        "filter": tables.filter_payload(st, len(all_moments)),
    }
