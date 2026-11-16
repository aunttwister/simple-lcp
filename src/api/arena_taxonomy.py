"""LMArena board catalogue + the operator's taxonomy mapping.

The mapping used to be 14 entries hardcoded in `arena_poll.py`
(`TEXT_BOARDS` / `AGENT_BOARDS`). The operator rejected that shape: "you need to
enable user to map taxonomies". The AXES are his, the BOARDS are LMArena's, and
which board measures which axis is his judgement. Worse, the poll reads the whole
text parquet (29 categories in ONE read) and then discarded 22 of them, because
there was nowhere to put them.

What this module does, in the order the operator specified: **persist ALL of
LMArena's boards and their rows first, then let the mapping decide what reaches a
leaderboard.**

    arena_boards       the catalogue — every board LMArena publishes, its family
                       (text = Elo / agent = net improvement), row count, publish
                       date, and when we last read it.
    arena_board_rows   every published rating for the models a provider here serves,
                       stored BEFORE any mapping is consulted. Adding a mapping
                       therefore shows a table immediately, with no re-poll.
    taxonomy_map       the operator's mapping, many-to-many in BOTH directions.
                       One board can feed several axes (`coding` is the same
                       measurement for code_generation, debugging and unit_tests)
                       and one axis can be fed by several boards (`reasoning_chain`
                       from math + hard_prompts_english + expert).

`custom:` taxonomies can never be a mapping target: they are the operator's own
ratings with no upstream, and a CHECK constraint enforces it rather than a
convention.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

# One definition of the percentile formula, in arena_poll. It was duplicated here as
# `_percentile` — same three lines, two names, and a change to one would have
# silently disagreed with the other.
from .arena_poll import percentile as _percentile

CUSTOM_PREFIX = "custom:"

#: The 14 mappings that were hardcoded, seeded as `is_default = 1` on the first
#: poll so the page looks identical the moment this lands. `is_default` is only a
#: provenance marker — every row is editable and deletable.
DEFAULT_MAP: list[tuple[str, str]] = [
    ("code_generation", "coding"),
    ("debugging", "coding"),
    ("unit_tests", "coding"),
    ("reasoning_chain", "math"),
    ("reasoning_chain", "hard_prompts_english"),
    ("reasoning_chain", "expert"),
    ("planning", "instruction_following"),
    ("casual_chat", "multi_turn"),
    ("casual_chat", "creative_writing"),
    ("agentic_multi_step", "agent"),
    ("agentic_multi_step", "agent_task_outcome_explicit"),
    ("planning", "agent_steerability"),
    ("research_deep", "agent_tool_hallucination"),
    ("debugging", "agent_bash_reliability"),
    ("debugging", "agent_bash_recovery_steps"),
    ("casual_chat", "agent_praise_complaint"),
]

#: Tab order. The mapping table is the source of truth for WHICH boards feed an
#: axis; this is only the order the axes are shown in, so the strip never
#: reshuffles itself between polls.
AXIS_ORDER = ["code_generation", "reasoning_chain", "planning", "casual_chat",
              "agentic_multi_step", "debugging", "unit_tests", "research_deep"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(con: sqlite3.Connection) -> sqlite3.Connection:
    if con.row_factory is None:
        con.row_factory = sqlite3.Row
    return con


# ── the catalogue ───────────────────────────────────────────────────────────

def store_boards(con: sqlite3.Connection, boards: dict[str, list[dict]],
                 *, polled_at: Optional[str] = None) -> dict:
    """Persist EVERY board and every row we can place — before any mapping.

    ``boards`` is `{board_name: [row dicts]}` as `arena_poll.default_fetch`
    returns it, including the 22 text categories nothing maps to yet: dropping
    them here is what made the mapping impossible in the first place.

    Only rows that resolve to a model a provider here CONFIRMS it serves are
    stored; a name we cannot place is not inventable. That keeps the table the
    size of our own fleet (~400 rows) rather than the arena's (~11k).
    """
    from .arena_poll import AGENT_BOARDS, TEXT_BOARDS, load_resolvers, resolve
    from .model_ratings import visible_models

    con = _row(con)
    now = polled_at or _now()
    visible = visible_models(con)
    alias, known = load_resolvers(con)

    text_boards, agent_boards = set(TEXT_BOARDS), set(AGENT_BOARDS)
    n_boards = n_rows = n_unresolved = 0
    per_board: dict[str, dict] = {}

    for board, members in boards.items():
        family = "agent" if board in agent_boards else "text"
        unit = "net_improvement" if family == "agent" else "elo"
        value_key = "score" if family == "agent" else "rating"
        published = None
        n_matched = 0
        for r in members:
            published = published or r.get("published")
            slug, effort = resolve(r.get("model_name") or "", alias, known)
            if not slug or slug not in visible:
                n_unresolved += not slug
                continue
            n_matched += 1
            con.execute(
                "INSERT INTO arena_board_rows (board, model_slug, value, unit, rank, n,"
                " lab, effort, published, polled_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (board, model_slug, effort) DO UPDATE SET"
                " value=excluded.value, unit=excluded.unit, rank=excluded.rank,"
                " n=excluded.n, lab=excluded.lab, published=excluded.published,"
                " polled_at=excluded.polled_at",
                (board, slug, float(r.get(value_key) or 0.0), unit, r.get("rank"),
                 len(members), r.get("organization"), effort or "",
                 r.get("published"), now))
            n_rows += 1
        con.execute(
            "INSERT INTO arena_boards (board, family, unit, rows, models_matched,"
            " published, first_seen, last_polled_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT (board) DO UPDATE SET rows=excluded.rows,"
            " models_matched=excluded.models_matched, published=excluded.published,"
            " last_polled_at=excluded.last_polled_at",
            (board, family, unit, len(members), n_matched, published, now, now))
        per_board[board] = {"family": family, "unit": unit, "rows": len(members),
                            "matched": n_matched, "published": published}
        n_boards += 1

    con.commit()
    return {"boards": n_boards, "rows": n_rows, "unresolved": n_unresolved,
            "per_board": per_board, "polled_at": now}


def seed_default_map(con: sqlite3.Connection) -> dict:
    """Insert the 14 former-hardcoded mappings, once.

    Called after the first `store_boards` so the boards a mapping names actually
    exist (the FK would refuse otherwise, and a placeholder board would describe a
    board whose rows we do not have — invented data).

    Idempotent: a `taxonomy_map` that already has ANY row is left alone, so an
    operator who deleted a default does not get it back on the next poll.
    """
    con = _row(con)
    have = con.execute("SELECT COUNT(*) FROM taxonomy_map").fetchone()[0]
    if have:
        return {"seeded": 0, "reason": "the mapping already has rows"}
    boards = {r[0] for r in con.execute("SELECT board FROM arena_boards")}
    now = _now()
    seeded = 0
    for tax, board in DEFAULT_MAP:
        if board not in boards:
            continue
        con.execute(
            "INSERT INTO taxonomy_map (taxonomy, board, note, is_default, created_at,"
            " updated_at) VALUES (?,?,?,1,?,?) ON CONFLICT (taxonomy, board) DO NOTHING",
            (tax, board, "seeded from the former built-in mapping", now, now))
        seeded += 1
    con.commit()
    return {"seeded": seeded}


# ── the mapping ─────────────────────────────────────────────────────────────

def ensure_taxonomy(con: sqlite3.Connection, taxonomy: str) -> None:
    """A mapped axis exists as a tab before its first row (an empty board is data)."""
    con = _row(con)
    con.execute(
        "INSERT INTO rating_taxonomies (taxonomy, kind, label, unit, note, created_at,"
        " updated_at) VALUES (?, 'arena', ?, NULL, NULL, ?, ?)"
        " ON CONFLICT (taxonomy) DO NOTHING",
        (taxonomy, taxonomy, _now(), _now()))
    con.commit()


def mapping(con: sqlite3.Connection) -> list[dict]:
    """The whole map, with what each board would contribute."""
    con = _row(con)
    return [dict(r) for r in con.execute(
        "SELECT m.taxonomy, m.board, m.note, m.is_default, b.family, b.unit, b.rows,"
        " b.models_matched, b.published,"
        " (SELECT COUNT(*) FROM arena_board_rows r WHERE r.board = m.board) AS stored_rows"
        " FROM taxonomy_map m JOIN arena_boards b ON b.board = m.board"
        " ORDER BY m.taxonomy, b.family, m.board")]


def map_board(con: sqlite3.Connection, taxonomy: str, board: str,
              note: Optional[str] = None) -> dict:
    """Map ONE board onto ONE taxonomy, and publish it immediately."""
    con = _row(con)
    tax = (taxonomy or "").strip()
    if not tax:
        raise ValueError("taxonomy is required")
    if tax.startswith(CUSTOM_PREFIX):
        raise ValueError("a custom board is your own rating and has no upstream — "
                         "it cannot be fed by an LMArena board")
    if not con.execute("SELECT 1 FROM arena_boards WHERE board=?", (board,)).fetchone():
        raise ValueError(f"unknown board {board!r} — poll once so the catalogue exists")
    now = _now()
    cur = con.execute(
        "INSERT INTO taxonomy_map (taxonomy, board, note, is_default, created_at,"
        " updated_at) VALUES (?,?,?,0,?,?) ON CONFLICT (taxonomy, board) DO UPDATE"
        " SET note=excluded.note, updated_at=excluded.updated_at",
        (tax, board, note, now, now))
    ensure_taxonomy(con, tax)
    con.commit()
    published = publish_taxonomy(con, tax)
    return {"taxonomy": tax, "board": board, "created": cur.rowcount > 0,
            "published": published}


def unmap_board(con: sqlite3.Connection, taxonomy: str, board: str) -> dict:
    """Remove ONE mapping and the rows it produced."""
    con = _row(con)
    cur = con.execute("DELETE FROM taxonomy_map WHERE taxonomy=? AND board=?",
                      (taxonomy, board))
    con.commit()
    removed = republish_taxonomy(con, taxonomy) if cur.rowcount else {"removed": 0}
    return {"taxonomy": taxonomy, "board": board, "unmapped": cur.rowcount,
            "removed": removed.get("removed", 0)}


def boards_for(con: sqlite3.Connection, taxonomy: str) -> list[str]:
    con = _row(con)
    return [r[0] for r in con.execute(
        "SELECT board FROM taxonomy_map WHERE taxonomy=? ORDER BY board", (taxonomy,))]


def taxonomies_for(con: sqlite3.Connection, board: str) -> list[str]:
    con = _row(con)
    return [r[0] for r in con.execute(
        "SELECT taxonomy FROM taxonomy_map WHERE board=? ORDER BY taxonomy", (board,))]


# ── publishing: rows carry the mapping, not a hardcoded axis table ──────────

def publish_taxonomy(con: sqlite3.Connection, taxonomy: str) -> dict:
    """Write every stored arena row that this taxonomy's mapped boards contribute.

    Replaces the taxonomy's arena rows wholesale, inside its own source: the
    mapping is the truth, so a board that was unmapped must stop contributing and
    a row that no longer exists upstream must disappear. `custom:` rows are never
    touched — a different source, a different owner.
    """
    con = _row(con)
    boards = boards_for(con, taxonomy)
    if not boards:
        cur = con.execute("DELETE FROM model_ratings WHERE taxonomy=? AND source='arena'",
                          (taxonomy,))
        con.commit()
        return {"taxonomy": taxonomy, "written": 0, "removed": cur.rowcount,
                "boards": []}
    placeholders = ",".join("?" * len(boards))
    rows = con.execute(
        f"SELECT board, model_slug, value, unit, rank, n, lab, effort, published"
        f" FROM arena_board_rows WHERE board IN ({placeholders})",
        tuple(boards)).fetchall()
    con.execute("DELETE FROM model_ratings WHERE taxonomy=? AND source='arena'",
                (taxonomy,))
    now = _now()
    written = 0
    for r in rows:
        con.execute(
            "INSERT INTO model_ratings (model_slug, taxonomy, value, unit, percentile,"
            " rank, n, source, board, lab, effort, basis, published, first_seen,"
            " updated_at) VALUES (?,?,?,?,?,?,?, 'arena', ?,?,?,?,?,?,?)"
            " ON CONFLICT (model_slug, taxonomy, source, board, effort) DO UPDATE SET"
            " value=excluded.value, unit=excluded.unit, rank=excluded.rank,"
            " n=excluded.n, lab=excluded.lab, published=excluded.published,"
            " updated_at=excluded.updated_at",
            (r["model_slug"], taxonomy, float(r["value"]), r["unit"],
             _percentile(r["rank"], r["n"]), r["rank"], r["n"], r["board"],
             r["lab"], r["effort"], "arena-elo" if r["unit"] == "elo"
             else "arena-net-improvement", r["published"], now, now))
        written += 1
    con.commit()
    return {"taxonomy": taxonomy, "written": written, "boards": boards}


def republish_taxonomy(con: sqlite3.Connection, taxonomy: str) -> dict:
    """After an unmap: rewrite what remains and report what went away."""
    before = con.execute(
        "SELECT COUNT(*) FROM model_ratings WHERE taxonomy=? AND source='arena'",
        (taxonomy,)).fetchone()[0]
    out = publish_taxonomy(con, taxonomy)
    out["removed"] = max(0, before - out["written"])
    return out


def publish_all(con: sqlite3.Connection) -> dict:
    """Publish every mapped taxonomy. Run after a poll."""
    con = _row(con)
    taxes = sorted({r[0] for r in con.execute("SELECT DISTINCT taxonomy FROM taxonomy_map")})
    return {tax: publish_taxonomy(con, tax) for tax in taxes}


# ── what Manage taxonomy shows ──────────────────────────────────────────────

def catalogue(con: sqlite3.Connection) -> dict:
    """Boards grouped as the operator asked: those feeding a taxonomy, and the
    rest — visible but unmapped, with row counts, so "this board scores 11 of
    yours" is information rather than a silence."""
    con = _row(con)
    rows = [dict(r) for r in con.execute(
        "SELECT b.board, b.family, b.unit, b.rows, b.models_matched, b.published,"
        " b.last_polled_at,"
        " (SELECT COUNT(*) FROM arena_board_rows r WHERE r.board=b.board) AS stored_rows,"
        " (SELECT GROUP_CONCAT(taxonomy) FROM taxonomy_map m WHERE m.board=b.board)"
        "   AS mapped_to"
        " FROM arena_boards b ORDER BY b.family, b.board")]
    used, unused = [], []
    for r in rows:
        r["taxonomies"] = (r.pop("mapped_to") or "").split(",") if r.get("mapped_to") else []
        r["taxonomies"] = [t for t in r["taxonomies"] if t]
        (used if r["taxonomies"] else unused).append(r)
    return {"mapped": used, "unmapped": unused,
            "boards": len(rows), "mapped_count": len(used), "unmapped_count": len(unused)}
