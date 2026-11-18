"""The Arena tier's label space, and the selector refusing a tier that cannot fire.

Design build step 9. Why this needs its own tests: Arena with no label space is
INDISTINGUISHABLE from Arena working. Every message falls back to the static chain, so a
tier that can never fire looks exactly like a tier that has not fired yet — the operator
selects it, sees no arena behaviour, and has nothing to go on. So the readiness is
measured and surfaced, and selecting the tier without it is refused with the reason.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from src.api import arena_routing as arena
from src.api.board_exemplars import BOARD_EXEMPLARS
from src.api.board_labels import NON_LABEL_BOARDS

REPO = os.path.join(os.path.dirname(__file__), "..")

SCHEMA = """
CREATE TABLE arena_boards (
    board TEXT PRIMARY KEY, family TEXT NOT NULL, unit TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0, models_matched INTEGER NOT NULL DEFAULT 0,
    published TEXT, first_seen TEXT NOT NULL, last_polled_at TEXT NOT NULL);
"""


def _con(boards=()):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for b in boards:
        con.execute("INSERT INTO arena_boards (board, family, unit, first_seen,"
                    " last_polled_at) VALUES (?,?,?,?,?)",
                    (b, "text", "elo", "2026-11-17", "2026-11-17"))
    con.commit()
    return con


# ── the two things the tier needs ────────────────────────────────────────────

def test_no_boards_means_not_ready():
    """A fresh install: the poll has never run, so there is nothing to judge against."""
    ls = arena.label_space(_con())
    assert ls["ready"] is False
    assert ls["count"] == 0 and ls["boards"] == []


def test_a_stored_curated_board_makes_it_ready():
    ls = arena.label_space(_con(["coding"]))
    assert ls["ready"] is True
    assert ls["boards"] == ["coding"]
    assert ls["without_exemplars"] == []


def test_a_board_without_exemplars_is_reported_not_silently_dropped():
    """The difference between "not polled" and "polled but unusable".

    Only the first is fixed by polling, so the two must not look the same. A stored board
    the classifier was never taught cannot be assigned, so it is excluded from the label
    space AND named — otherwise the operator polls again and nothing changes.
    """
    ls = arena.label_space(_con(["coding", "some_uncurated_board"]))
    assert ls["boards"] == ["coding"]
    assert ls["stored_count"] == 2
    assert ls["without_exemplars"] == ["some_uncurated_board"]


def test_the_meta_and_language_boards_are_not_part_of_the_label_space():
    """They are slices of a measurement, not measurements — see board_labels.

    Polled into arena_boards (the poll stores everything it reads), but never a routing
    label: a message is not a language or an industry.
    """
    meta = ["overall", "exclude_ties", "longer_query", "non_english"]
    con = _con(["coding"] + meta)
    ls = arena.label_space(con)
    assert ls["boards"] == ["coding"]
    # They are not candidates AT ALL (curated_boards drops them before readiness is
    # measured), so they are correctly NOT reported as "polled but unusable" — that
    # report is for boards that could have been labels and are not usable yet.
    assert ls["without_exemplars"] == []
    assert arena.curated_boards(con) == ["coding"]
    for b in meta:
        assert arena.is_label_board(b) is False


def test_the_authored_exemplars_ARE_the_label_space():
    """There is no separate curated list to drift out of sync.

    `curated_boards` derives the label space from what the poll stored, and a stored board
    is usable only if it is in BOARD_EXEMPLARS. So the exemplar set IS the definition of
    what the classifier can be asked to assign — which is why the two can never disagree.
    """
    assert len(BOARD_EXEMPLARS) == 23, f"expected 23 board exemplar sets, got {len(BOARD_EXEMPLARS)}"
    for board, texts in BOARD_EXEMPLARS.items():
        assert len(texts) >= 4, f"{board} has only {len(texts)} exemplar(s)"


def test_no_exemplars_exist_for_a_board_that_is_not_a_label():
    """Teaching a label the router never offers is dead weight at best."""
    stray = sorted(set(BOARD_EXEMPLARS) & set(NON_LABEL_BOARDS))
    assert not stray, f"exemplars for non-label boards: {stray}"


# ── the selector ─────────────────────────────────────────────────────────────

def test_the_status_payload_carries_the_label_space():
    """The selector reads /api/routing/status; a readiness it cannot see cannot be shown."""
    src = open(os.path.join(REPO, "src", "api", "router.py")).read()
    assert '_arena_label_space(router.db_path)' in src
    assert '"arena": ' in src


def test_saving_route_by_arena_without_a_label_space_is_refused():
    """400 with the reason, not accepted-and-inert.

    Accepting it would leave the operator with a tier that never fires and no signal —
    the exact failure mode this step exists to remove.
    """
    src = open(os.path.join(REPO, "src", "server", "endpoints.py")).read()
    block = src.split('if route_by == "arena":', 1)[1].split("settings.set_route_by", 1)[0]
    assert "label space" in block
    assert "400" in block, "the refusal must not answer 200"


def test_the_readiness_report_never_raises():
    """It rides in the status payload the UI renders; an unreadable DB must degrade to
    'not ready' rather than take the page down."""
    from src.api.router import _arena_label_space
    for bad in (None, "/nonexistent/definitely-not-a-db.sqlite"):
        out = _arena_label_space(bad)
        assert out["ready"] is False and out["boards"] == []
