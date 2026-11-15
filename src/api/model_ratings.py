"""The leaderboard store: ratings/position per model per taxonomy.

Distinct from `model_capabilities` on purpose (see the 038 migration). Rules this
module enforces, all from the operator's decisions of 2026-11-15:

  * A model is VISIBLE only if a provider registers it (a served_id in
    mi_deployments). Everything else is ignored rather than invented.
  * `unit` is stored per row: text boards publish Elo, agent boards publish a net
    improvement. They are never ranked against each other, and percentiles are
    computed WITHIN one (taxonomy, unit) group.
  * `effort` is a dimension (R2=C), so "(Max)"/"(High)" variants of one model are
    rows, not separate models.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

VISIBLE_SQL = """
    SELECT DISTINCT m.slug FROM mi_deployments d
    JOIN mi_models m ON m.model_id = d.model_id
    WHERE m.status = 'active'
"""


def visible_models(con: sqlite3.Connection) -> set[str]:
    """Slugs a provider registers AND that are not retired."""
    return {r[0] for r in con.execute(VISIBLE_SQL)}


def _rowify(con: sqlite3.Connection) -> sqlite3.Connection:
    if con.row_factory is None:
        con.row_factory = sqlite3.Row
    return con


def upsert_ratings(con: sqlite3.Connection, rows: Iterable[dict]) -> dict:
    """Write rating rows. Only registered, non-retired models are accepted."""
    con = _rowify(con)
    visible = visible_models(con)
    now = datetime.now(timezone.utc).isoformat()
    written = skipped = 0
    cur = con.cursor()
    for r in rows:
        if r["model_slug"] not in visible:
            skipped += 1
            continue
        cur.execute("""
            INSERT INTO model_ratings
                (model_slug, taxonomy, value, unit, percentile, rank, n, source,
                 board, lab, effort, basis, published, first_seen, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (model_slug, taxonomy, source, board, effort) DO UPDATE SET
                value=excluded.value, unit=excluded.unit,
                percentile=excluded.percentile, rank=excluded.rank, n=excluded.n,
                lab=excluded.lab, basis=excluded.basis,
                published=excluded.published, updated_at=excluded.updated_at
        """, (r["model_slug"], r["taxonomy"], float(r["value"]), r["unit"],
              r.get("percentile"), r.get("rank"), r.get("n"), r.get("source", "arena"),
              r.get("board"), r.get("lab"), r.get("effort"), r.get("basis"),
              r.get("published"), r.get("first_seen", now), now))
        written += 1
    con.commit()
    return {"written": written, "skipped_unregistered": skipped}


def leaderboard(con: sqlite3.Connection, taxonomy: str) -> list[dict]:
    """One taxonomy's board, ranked within its unit. Tabs are namespaced by the
    caller (`arena:*` / `custom:*`) so the two can never collide."""
    con = _rowify(con)
    out = []
    for unit in {r[0] for r in con.execute(
            "SELECT DISTINCT unit FROM model_ratings WHERE taxonomy=?", (taxonomy,))}:
        rows = con.execute("""
            SELECT model_slug, value, unit, percentile, rank, lab, effort, board,
                   published, basis
            FROM model_ratings WHERE taxonomy=? AND unit=?
            ORDER BY value DESC
        """, (taxonomy, unit)).fetchall()
        n = len(rows)
        for i, r in enumerate(rows):
            d = dict(r)
            if d["percentile"] is None:
                d["percentile"] = round(100.0 * (1 - i / max(n - 1, 1)), 1) if n > 1 else 100.0
            out.append(d)
    return out


def diff(con: sqlite3.Connection, taxonomy: str, incoming: list[dict]) -> list[dict]:
    """Old -> new for the poll panel. Shows BOTH dates and both values."""
    con = _rowify(con)
    current = {(r["model_slug"], r["unit"]): r for r in con.execute(
        "SELECT model_slug, unit, value, published, updated_at FROM model_ratings WHERE taxonomy=?",
        (taxonomy,))}
    changes = []
    for r in incoming:
        cur = current.get((r["model_slug"], r["unit"]))
        old = cur["value"] if cur else None
        new = float(r["value"])
        changes.append(dict(
            model_slug=r["model_slug"], unit=r["unit"], old=old, new=new,
            changed=old is None or abs(old - new) > 1e-9,
            old_date=(cur["published"] or cur["updated_at"]) if cur else None,
            new_date=r.get("published"), effort=r.get("effort"),
            lab=r.get("lab"), board=r.get("board")))
    return changes
