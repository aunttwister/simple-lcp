"""Arena routing — the tier that needs nothing configured but the pool.

Operator decision, 2026-11-17: "Board names for Arena routing" (design doc §15 Q1), and
"On every message" (§16 E6). So this module answers, for ONE request's text:

    which board judges this?  →  walk that board's ranking, filtered to the pool
                              →  the first survivor wins

Four rules that exist because the alternative is a lie:

* **The pool is the boundary.** The walk is filtered to the profile's configured
  `model:provider` pairs. A model outside the pool is not a candidate however highly
  LMArena rates it (design §3).
* **Never a fabricated number.** `score()` returns ``None`` for a model the board never
  ranked — not 0.0, not a default. An unmeasured model is unmeasured (§16 E8).
* **No embedder means no board.** ``pick_board`` returns ``(None, [])`` rather than
  falling back to keyword matching, because a keyword rule cannot name a board without
  inventing a mapping (§16 E7). The caller's fallback is the static chain.
* **Effort groups are chosen by policy, not by the sort.** LMArena ranks models within an
  effort setting; our deployments do not declare one, so the unlabelled rows are the
  honest default and a labelled group is used only when it is all a board has (§16 E2).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import structlog

from .board_exemplars import BOARD_EXEMPLARS, BOARD_LABEL_SPACE_ID
from .board_labels import LANGUAGE_BOARDS, META_BOARDS, NON_LABEL_BOARDS

logger = structlog.get_logger(__name__)

#: Effort groups, most-preferred first. The empty string is an unlabelled row — the
#: ones that describe a model as served rather than a particular reasoning setting.
EFFORT_ORDER: tuple[str, ...] = ("", "max", "xhigh", "high", "medium", "low")

#: Cached board-space classifier. The label space is a module constant here (unlike the
#: task taxonomy, which is per profile), so one cache entry is enough.
_board_clf = None


# ── the label space ───────────────────────────────────────────────────────────

def curated_boards(con: sqlite3.Connection) -> list[str]:
    """The boards that can be a routing label, from the boards the poll stored.

    Sorted, so the label space's order never depends on insertion order. Boards the
    poll has not stored yet are absent by construction — a label with no ranking to
    walk would be a pick with nothing behind it.
    """
    rows = con.execute("SELECT board FROM arena_boards").fetchall()
    boards = [(r["board"] if isinstance(r, sqlite3.Row) else r[0]) for r in rows]
    return sorted(b for b in boards if b not in NON_LABEL_BOARDS)


def is_label_board(board: str) -> bool:
    """True when *board* may be used as a routing label at all."""
    return bool(board) and board not in NON_LABEL_BOARDS


# ── the judge ─────────────────────────────────────────────────────────────────

def _board_classifier(taxonomy_id: str = BOARD_LABEL_SPACE_ID):
    """The classifier that decides in the board label space, or None.

    Reuses the router's embedder — the same model, the same install path — so Arena
    routing adds a label space, not a second embedding stack. Returns None when the
    embedder is unavailable, which is the caller's signal to fall back to the static
    chain rather than guess.
    """
    global _board_clf
    if _board_clf is not None and getattr(_board_clf, "available", False):
        return _board_clf
    try:
        from .task_classifier import SemanticClassifier, get_semantic_classifier
        # Ensure the embedder is built (and its site dir appended) exactly as the
        # task classifier does, then borrow it.
        base = get_semantic_classifier()
        if base is None:
            return None
        embed = getattr(base, "_embed", None)
        if embed is None:
            return None
        _board_clf = SemanticClassifier(
            embed=embed,
            min_score=getattr(base, "min_score", SemanticClassifier.DEFAULT_MIN_SCORE),
            exemplars=BOARD_EXEMPLARS,
            taxonomy_id=taxonomy_id,
        )
        logger.info("board_classifier_ready", labels=len(_board_clf.labels),
                    taxonomy_id=taxonomy_id)
        return _board_clf
    except Exception as exc:  # noqa: BLE001 — routing must never break on this
        logger.warning("board_classifier_unavailable", error=str(exc)[:200])
        return None


def label_space(con: sqlite3.Connection) -> dict:
    """What the Arena tier would judge against, and whether it can judge at all.

    The tier needs two things to fire: boards the poll has STORED (a label with no
    ranking behind it is a pick with nothing behind it) and an exemplar set for each
    (a label the classifier was never taught is a label it cannot assign). Missing the
    first is the common case on a fresh install, and it fails SILENTLY — every message
    falls back to the static chain, so the operator sees Arena selected and no arena
    behaviour. That is why this is exposed to the selector rather than left implicit.
    """
    from .board_exemplars import BOARD_EXEMPLARS

    stored = curated_boards(con)
    ready = [b for b in stored if b in BOARD_EXEMPLARS]
    return {
        "boards": ready,
        "count": len(ready),
        "ready": bool(ready),
        "stored_count": len(stored),
        # Stored but unteachable — a board the poll wrote that has no exemplars yet.
        # Reported rather than hidden: it is the difference between "not polled" and
        # "polled but not usable", and only the first is fixed by polling.
        "without_exemplars": sorted(b for b in stored if b not in BOARD_EXEMPLARS),
    }


def pick_board(text: str) -> tuple[Optional[str], list]:
    """Which board judges *text* — ``(board, [(label, score), …])``.

    ``(None, [])`` when the embedder is unavailable, and ``(None, scored)`` when the
    best score is below the gate. The vector is returned either way: a decision that
    fell back is worth explaining, and the scores are the evidence.
    """
    clf = _board_classifier()
    if clf is None or not text:
        return None, []
    try:
        scored = clf.top_scores(text) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("board_pick_failed", error=str(exc)[:200])
        return None, []
    if not scored:
        return None, []
    board = clf.classify(text)
    return (board if is_label_board(board) else None), list(scored)


# ── the ranking ───────────────────────────────────────────────────────────────

def effort_group(con: sqlite3.Connection, board: str,
                 candidates: set[str]) -> str:
    """The effort group to walk for *board*, given the models we can serve.

    Unlabelled rows first: our deployments do not declare a reasoning-effort setting,
    so the rows without one describe the model as we actually serve it. A board whose
    only rows carry an effort label still gets walked, using that group — the
    alternative is a board that silently cannot route anything.

    Returns ``""`` when no group contains a candidate (the caller falls back).
    """
    if not is_label_board(board) or not candidates:
        return ""
    present = {
        (r["effort"] if isinstance(r, sqlite3.Row) else r[0])
        for r in con.execute(
            "SELECT DISTINCT effort FROM arena_board_rows"
            " WHERE board=? AND model_slug IN (%s)"
            % ",".join("?" * len(candidates)),
            (board, *sorted(candidates)))
    }
    for effort in EFFORT_ORDER:
        if effort in present:
            return effort
    return ""


def walk(con: sqlite3.Connection, board: str,
         candidates: set[str]) -> tuple[Optional[str], Optional[int], str]:
    """Walk *board*'s ranking, filtered to *candidates*.

    Returns ``(model_slug, rank, effort_used)`` for the best-placed servable model, or
    ``(None, None, "")`` when the board has no candidate — the caller keeps the static
    chain, which is what "nothing survives" must mean (design §4).

    Ordering is ``value DESC`` then ``model_slug ASC``: LMArena's own rank where it
    agrees, and a deterministic tiebreak where two models share a value, so routing
    never flips between equal-ranked models (§16 E3).
    """
    if not is_label_board(board) or not candidates:
        return None, None, ""
    effort = effort_group(con, board, candidates)
    rows = con.execute(
        "SELECT model_slug, rank, value FROM arena_board_rows"
        " WHERE board=? AND effort=? AND model_slug IN (%s)"
        " ORDER BY value DESC, model_slug ASC"
        % ",".join("?" * len(candidates)),
        (board, effort, *sorted(candidates))).fetchall()
    if not rows:
        return None, None, ""
    top = rows[0]
    slug = top["model_slug"] if isinstance(top, sqlite3.Row) else top[0]
    rank = top["rank"] if isinstance(top, sqlite3.Row) else top[1]
    return slug, rank, effort


def score(con: sqlite3.Connection, board: str,
          model_slug: str) -> Optional[float]:
    """The model's standing on *board*, as a 0–1 fraction of the field.

    Percentile-like and deliberately NOT Elo: Elo is only comparable within one board
    (design §5), and the caller may be comparing across boards. Returns ``None`` for a
    model the board never ranked — an unmeasured model must not read as zero.
    """
    if not is_label_board(board) or not model_slug:
        return None
    effort = effort_group(con, board, {model_slug})
    if effort == "" and not con.execute(
            "SELECT 1 FROM arena_board_rows WHERE board=? AND effort='' AND model_slug=?"
            " LIMIT 1", (board, model_slug)).fetchone():
        return None
    row = con.execute(
        "SELECT value, rank, n FROM arena_board_rows WHERE board=? AND effort=?"
        " AND model_slug=? LIMIT 1", (board, effort, model_slug)).fetchone()
    if row is None:
        return None
    rank, n = row["rank"], row["n"]
    if not rank:
        return None
    if not n:
        # The poll usually records the field size, but a stored row without one still
        # has a rank — count the field it was ranked in rather than give up on it.
        n = con.execute(
            "SELECT COUNT(*) FROM arena_board_rows WHERE board=? AND effort=?",
            (board, effort)).fetchone()[0]
    if not n or rank > n:
        return None
    return max(0.0, min(1.0, 1.0 - (rank - 1) / float(n)))
