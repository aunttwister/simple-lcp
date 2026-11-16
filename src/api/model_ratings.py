"""The leaderboard store: ratings/position per model per taxonomy.

Distinct from `model_capabilities` on purpose (see the 038 migration). Rules this
module enforces, all from the operator's decisions of 2026-11-15:

  * A model is VISIBLE only if a provider registers it (a served_id in
    mi_deployments) AND it is not retired. Everything else is ignored rather
    than invented.
  * `unit` is stored per row: text boards publish Elo, agent boards publish a net
    improvement. They are never ranked against each other, and percentiles are
    computed WITHIN one (taxonomy, unit) group.
  * `effort` is a dimension (R2=C), so "(Max)"/"(High)" variants of one model are
    rows, not separate models.
  * Operator-defined taxonomies are namespaced `custom:<slug>` (039). Arena rows
    only ever carry an axis name as their taxonomy, so a poll can never overwrite
    a custom board and a custom import can never pollute an arena one — the
    separation is structural, not a promise.

Nothing in this module writes `model_capabilities`: grading and routing read that
table and are unchanged by anything here.
"""
from __future__ import annotations

import csv
import io
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

CUSTOM_PREFIX = "custom:"
INF = math.inf
NEG_INF = -math.inf

VISIBLE_PREDICATE = "m.status = 'active' AND d.status = 'active'"
VISIBLE_FROM = "mi_deployments d JOIN mi_models m ON m.model_id = d.model_id"
VISIBLE_SQL = f"SELECT DISTINCT m.slug FROM {VISIBLE_FROM} WHERE {VISIBLE_PREDICATE}"
#: The same set as a subquery, so every READ path enforces registration too. A
#: write gate alone is not enough: a model that was registered when its row was
#: stored and is retired later must stop being DISPLAYED, and a poll row already
#: staged for it must not be offered for acceptance.
VISIBLE_IN = f"(SELECT DISTINCT m.slug FROM {VISIBLE_FROM} WHERE {VISIBLE_PREDICATE})"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def visible_models(con: sqlite3.Connection) -> set[str]:
    """Slugs a provider CONFIRMS it serves, and that are not retired.

    Both sides of the registration have to be live, and the deployment side is
    the one that bites. `mi_deployments.status` has three values in this estate:

      * ``active``  — a provider serves this. The identity contract's own notion
        of registered (`model_identity` reads only ``active``).
      * ``retired`` — it served it and stopped.
      * ``new``     — the model-intel poll STAGED an unknown served id for
        identity review. Nothing reads it, precisely because the registry
        refuses to treat a poll's own output as a fact.

    Reading every deployment row as "registered" therefore let the poll widen
    its own eligibility window: it stages a ``new`` row for a name it does not
    recognise, and the next poll then resolves that name as a registered model
    and publishes its numbers. Three models reached the leaderboard that way
    (gemini-3.6-flash, gpt-5.6-sol, grok-4.5) — the operator caught it on the
    rendered page, which is what this filter is for.
    """
    return {r[0] for r in con.execute(VISIBLE_SQL)}


def _rowify(con: sqlite3.Connection) -> sqlite3.Connection:
    if con.row_factory is None:
        con.row_factory = sqlite3.Row
    return con


# ── taxonomy namespace ──────────────────────────────────────────────────────

def _tax_slug(name: str) -> str:
    s = re.sub(r"['\u2019]", "", (name or "").strip().lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unnamed"


def custom_taxonomy(name: str) -> str:
    """Coerce anything the operator types into the `custom:` namespace.

    'Coding' -> 'custom:coding'. Typing an arena axis is allowed and stays a
    SEPARATE tab, because a self-authored number must never be readable as an
    arena measurement.
    """
    n = (name or "").strip()
    if n.startswith(CUSTOM_PREFIX):
        n = n[len(CUSTOM_PREFIX):]
    return CUSTOM_PREFIX + _tax_slug(n)


def is_custom(taxonomy: str) -> bool:
    return str(taxonomy or "").startswith(CUSTOM_PREFIX)


def register_taxonomy(con: sqlite3.Connection, taxonomy: str, *, kind: str,
                      label: Optional[str] = None, unit: Optional[str] = None,
                      note: Optional[str] = None) -> dict:
    """Record a tab even before it has rows (a defined-but-empty custom board)."""
    now = _now()
    con.execute("""
        INSERT INTO rating_taxonomies (taxonomy, kind, label, unit, note,
                                       created_at, updated_at)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT (taxonomy) DO UPDATE SET
            label=COALESCE(excluded.label, rating_taxonomies.label),
            unit=COALESCE(excluded.unit, rating_taxonomies.unit),
            note=COALESCE(excluded.note, rating_taxonomies.note),
            updated_at=excluded.updated_at
    """, (taxonomy, kind, label, unit, note, now, now))
    con.commit()
    return {"taxonomy": taxonomy, "kind": kind}


def register_arena_taxonomies(con: sqlite3.Connection, taxonomies: Iterable[str]) -> int:
    n = 0
    for t in taxonomies:
        if is_custom(t):
            continue
        register_taxonomy(con, t, kind="arena")
        n += 1
    return n


def taxonomies(con: sqlite3.Connection) -> list[dict]:
    """Every tab: registered ones plus any taxonomy that already carries rows."""
    from .arena_poll import AXIS_ORDER

    con = _rowify(con)
    counts: dict[str, dict] = {}
    for r in con.execute(f"""
            SELECT taxonomy, unit, count(*) AS rows, max(updated_at) AS updated_at
            FROM model_ratings WHERE model_slug IN {VISIBLE_IN}
            GROUP BY taxonomy, unit"""):
        c = counts.setdefault(r["taxonomy"], {"rows": 0, "units": [], "updated_at": None})
        c["rows"] += r["rows"]
        c["units"].append(r["unit"])
        c["updated_at"] = max(c["updated_at"] or "", r["updated_at"] or "")
    registered = {r["taxonomy"]: dict(r) for r in con.execute(
        "SELECT taxonomy, kind, label, unit, note, updated_at FROM rating_taxonomies")}

    out = []
    for tax in sorted(set(registered) | set(counts)):
        reg = registered.get(tax, {})
        cnt = counts.get(tax, {})
        kind = reg.get("kind") or ("custom" if is_custom(tax) else "arena")
        out.append({
            "taxonomy": tax, "kind": kind,
            "label": reg.get("label") or (tax.split(":", 1)[1] if is_custom(tax) else tax),
            "units": sorted(set(cnt.get("units") or ([reg["unit"]] if reg.get("unit") else []))),
            "rows": cnt.get("rows", 0),
            "note": reg.get("note"),
            "updated_at": max(reg.get("updated_at") or "", cnt.get("updated_at") or "") or None,
        })
    order = {t: i for i, t in enumerate(AXIS_ORDER)}
    out.sort(key=lambda t: (0 if t["kind"] == "arena" else 1,
                            order.get(t["taxonomy"], 50), t["label"]))
    return out


# ── ratings ─────────────────────────────────────────────────────────────────

def upsert_ratings(con: sqlite3.Connection, rows: Iterable[dict]) -> dict:
    """Write rating rows. Only registered, non-retired models are accepted."""
    con = _rowify(con)
    visible = visible_models(con)
    now = _now()
    written = skipped = rejected = 0
    cur = con.cursor()
    for r in rows:
        # An arena poll may not write a custom board, and a custom row may not
        # claim to be an arena measurement. One rule, checked in one place.
        source = r.get("source", "arena")
        if source == "arena" and is_custom(r["taxonomy"]):
            rejected += 1
            continue
        if source == "custom" and not is_custom(r["taxonomy"]):
            rejected += 1
            continue
        if r["model_slug"] not in visible:
            skipped += 1
            continue
        # '' not NULL: SQLite treats NULLs as DISTINCT in a unique index, so a
        # NULL effort/board never matches its own ON CONFLICT and every re-poll
        # would INSERT a duplicate of the same model instead of refreshing it.
        effort = r.get("effort") or ""
        board = r.get("board") or ""
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
              r.get("percentile"), r.get("rank"), r.get("n"), source,
              board, r.get("lab"), effort, r.get("basis"),
              r.get("published"), r.get("first_seen", now), now))
        written += 1
    con.commit()
    return {"written": written, "skipped_unregistered": skipped,
            "rejected_namespace": rejected}


def leaderboard(con: sqlite3.Connection, taxonomy: str) -> list[dict]:
    """One taxonomy's board, ranked within its unit.

    One row per MODEL per unit: several boards can inform one axis (the agent
    boards `agent` and `agent_task_outcome_explicit` both feed
    `agentic_multi_step`), and a board per row would list the same model twice.
    The winning row keeps the board it came from, and every board that informed
    the model is listed in ``boards`` so the number stays traceable.

    Tabs are namespaced by the caller (`arena:*` / `custom:*`) so the two can
    never collide.
    """
    con = _rowify(con)
    out = []
    # Deterministic unit order: iterating a set made the two groups swap places
    # between two identical requests.
    units = sorted({r[0] for r in con.execute(
        f"SELECT DISTINCT unit FROM model_ratings WHERE taxonomy=? AND model_slug IN {VISIBLE_IN}",
        (taxonomy,))},
        key=lambda u: ({"elo": 0, "net_improvement": 1}.get(u, 9), u))
    for unit in units:
        rows = con.execute(f"""
            SELECT model_slug, value, unit, percentile, rank, lab, effort, board,
                   published, basis, source, updated_at
            FROM model_ratings WHERE taxonomy=? AND unit=? AND model_slug IN {VISIBLE_IN}
            ORDER BY value DESC
        """, (taxonomy, unit)).fetchall()
        best: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            cur = best.get(d["model_slug"])
            if cur is None:
                d["boards"] = [d["board"]] if d["board"] else []
                best[d["model_slug"]] = d
            elif d["board"] and d["board"] not in cur["boards"]:
                cur["boards"].append(d["board"])
        ranked = sorted(best.values(), key=lambda d: d["value"], reverse=True)
        n = len(ranked)
        for i, d in enumerate(ranked):
            if d["percentile"] is None:
                d["percentile"] = round(100.0 * (1 - i / max(n - 1, 1)), 1) if n > 1 else 100.0
            out.append(d)
    return out


def diff(con: sqlite3.Connection, taxonomy: str, incoming: list[dict]) -> list[dict]:
    """Old -> new for the poll panel. Shows BOTH dates and both values."""
    con = _rowify(con)
    current = {(r["model_slug"], r["unit"]): r for r in con.execute(
        f"SELECT model_slug, unit, value, published, updated_at FROM model_ratings"
        f" WHERE taxonomy=? AND model_slug IN {VISIBLE_IN}",
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


# ── custom boards: define + CSV import ──────────────────────────────────────

def define_taxonomy(con: sqlite3.Connection, name: str, *, label: Optional[str] = None,
                    note: Optional[str] = None) -> dict:
    tax = custom_taxonomy(name)
    register_taxonomy(con, tax, kind="custom", label=label or name, unit="elo", note=note)
    return {"taxonomy": tax, "label": label or name}


def import_csv(con: sqlite3.Connection, text: str, *,
               taxonomy: Optional[str] = None) -> dict:
    """CSV -> custom ratings. Columns (header required, order free):

        model_slug   (alias: model)     required
        value        (alias: elo)       required, numeric
        taxonomy                        optional — falls back to ``taxonomy``
        lab, effort, note/published     optional

    Every taxonomy the CSV names is coerced into ``custom:`` — an import can
    never write an arena board.
    """
    con = _rowify(con)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    fields = {f.strip().lower(): f for f in reader.fieldnames if f}

    def pick(row, *names):
        for n in names:
            if n in fields:
                v = (row.get(fields[n]) or "").strip()
                if v:
                    return v
        return None

    if "model_slug" not in fields and "model" not in fields:
        raise ValueError("CSV needs a 'model_slug' (or 'model') column")
    if "value" not in fields and "elo" not in fields:
        raise ValueError("CSV needs a 'value' (or 'elo') column")

    rows, errors = [], []
    for i, raw in enumerate(reader, start=2):
        slug = pick(raw, "model_slug", "model")
        val = pick(raw, "value", "elo")
        if not slug and not val:
            continue
        tax = pick(raw, "taxonomy") or taxonomy
        if not slug:
            errors.append({"line": i, "error": "missing model_slug"})
            continue
        if not tax:
            errors.append({"line": i, "error": "missing taxonomy (no default given)"})
            continue
        try:
            value = float(val)
        except (TypeError, ValueError):
            errors.append({"line": i, "error": f"value not numeric: {val!r}"})
            continue
        if value != value or value in (INF, NEG_INF):
            errors.append({"line": i, "error": "value not finite"})
            continue
        rows.append(dict(model_slug=slug, taxonomy=custom_taxonomy(tax), value=value,
                         unit="elo", source="custom", board="operator-csv",
                         lab=pick(raw, "lab", "organization"),
                         effort=pick(raw, "effort"),
                         basis="operator-input",
                         published=pick(raw, "published", "date")))

    for tax in sorted({r["taxonomy"] for r in rows}):
        register_taxonomy(con, tax, kind="custom", unit="elo")
    result = upsert_ratings(con, rows)
    result["errors"] = errors
    result["parsed"] = len(rows)
    return result


# ── poll staging: poll -> diff -> accept ────────────────────────────────────
# The poll is staged, not applied: the operator reviews a diff and ticks what
# becomes the new baseline. Same shape as the model-intel poll's `status='new'`
# deployments — a machine reading is staged, never written straight over the
# human-owned value.

def stage_poll(con: sqlite3.Connection, *, fetch=None) -> dict:
    """Fetch the boards, snapshot the baseline, stage the incoming rows."""
    from . import arena_poll

    con = _rowify(con)
    boards = (fetch or arena_poll.default_fetch)()
    built = arena_poll.build_rows(con, boards)
    poll_id = uuid.uuid4().hex[:12]
    now = _now()
    baseline = {}
    for r in con.execute("SELECT model_slug, taxonomy, unit, board, effort, value,"
                         " published, updated_at FROM model_ratings"):
        baseline[(r["model_slug"], r["taxonomy"], r["unit"], r["board"],
                  r["effort"])] = r
    cur = con.cursor()
    cur.execute("DELETE FROM rating_poll_staging")
    staged = 0
    for r in built["rows"]:
        key = (r["model_slug"], r["taxonomy"], r["unit"], r.get("board") or "",
               r.get("effort") or "")
        old = baseline.get(key)
        cur.execute("""
            INSERT INTO rating_poll_staging
                (poll_id, model_slug, taxonomy, unit, value, old_value, percentile,
                 rank, n, source, board, lab, effort, basis, published,
                 old_published, old_updated_at, polled_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (poll_id, r["model_slug"], r["taxonomy"], r["unit"], float(r["value"]),
              old["value"] if old else None, r.get("percentile"), r.get("rank"),
              r.get("n"), r.get("source", "arena"), r.get("board") or "",
              r.get("lab"), r.get("effort") or "", r.get("basis"),
              r.get("published"),
              old["published"] if old else None, old["updated_at"] if old else None, now))
        staged += 1
    # A board with no rows yet must still appear as a tab (e.g. the first poll
    # into an axis arena does not score at all).
    register_arena_taxonomies(con, sorted({r["taxonomy"] for r in built["rows"]}))
    con.commit()
    out = {"poll_id": poll_id, "staged": staged, "polled_at": now,
           "boards": built["stats"], "unmatched": built["unmatched"],
           "taxonomies": taxonomies(con), "rows": staged_rows(con)}
    try:
        out["release"] = arena_poll.release_candidates(con, boards)
    except Exception as exc:  # noqa: BLE001 — the diff is the deliverable; this is a bonus
        out["release"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def staged_rows(con: sqlite3.Connection, taxonomy: Optional[str] = None) -> list[dict]:
    """The poll panel's rows: old -> new, with BOTH dates.

    Two filters beyond "what the poll wrote", both load-bearing:

    * only models that are STILL registered — accepting a row for a model that has
      since been retired would write a rating onto a board for something no
      provider serves;
    * **arena boards only.** A `custom:` board is a rating the operator entered
      and has no upstream, so a poll has nothing to fetch for it. A custom row
      reaching this list would be a poll claiming to have re-read something it
      cannot read (operator, 2026-11-15: "why are we displaying custom taxonomy
      elo and rating in poll latest data?").
    """
    con = _rowify(con)
    sql = ("SELECT model_slug, taxonomy, unit, value, old_value, percentile, rank, n,"
           " source, board, lab, effort, basis, published, old_published,"
           " old_updated_at, polled_at FROM rating_poll_staging"
           f" WHERE model_slug IN {VISIBLE_IN}"
           f" AND taxonomy NOT LIKE '{CUSTOM_PREFIX}%'")
    params: tuple = ()
    if taxonomy:
        sql += " AND taxonomy=?"
        params = (taxonomy,)
    sql += " ORDER BY taxonomy, unit, rank IS NULL, rank, value DESC"
    out = []
    for r in con.execute(sql, params):
        d = dict(r)
        d["changed"] = d["old_value"] is None or abs(d["old_value"] - d["value"]) > 1e-9
        d["old_date"] = d["old_published"] or d["old_updated_at"]
        d["new_date"] = d["published"]
        out.append(d)
    return out


def commit_staged(con: sqlite3.Connection, keys: Optional[list[dict]] = None) -> dict:
    """Accept ticked staging rows as the new baseline. ``keys=None`` accepts all."""
    con = _rowify(con)
    rows = staged_rows(con)
    if keys is not None:
        wanted = {(k["model_slug"], k["taxonomy"], k["unit"], k.get("board"),
                   k.get("effort")) for k in keys}
        rows = [r for r in rows
                if (r["model_slug"], r["taxonomy"], r["unit"], r["board"],
                    r["effort"]) in wanted]
    result = upsert_ratings(con, rows)
    cur = con.cursor()
    for r in rows:
        cur.execute("""DELETE FROM rating_poll_staging WHERE model_slug=? AND taxonomy=?
                       AND unit=? AND board IS ? AND effort IS ?""",
                    (r["model_slug"], r["taxonomy"], r["unit"], r["board"], r["effort"]))
    con.commit()
    result["remaining"] = len(staged_rows(con))
    result["taxonomies"] = taxonomies(con)
    return result


def clear_staged(con: sqlite3.Connection) -> dict:
    """Discard the polled results. The baseline is untouched."""
    con = _rowify(con)
    n = con.execute("SELECT count(*) FROM rating_poll_staging").fetchone()[0]
    con.execute("DELETE FROM rating_poll_staging")
    con.commit()
    return {"cleared": n}


def set_custom_ratings(con: sqlite3.Connection, taxonomy: str,
                       rows: Iterable[dict]) -> dict:
    """The operator typing ratings by hand into a custom board."""
    tax = custom_taxonomy(taxonomy)
    register_taxonomy(con, tax, kind="custom", unit="elo")
    payload = []
    for r in rows:
        try:
            value = float(r["value"])
        except (KeyError, TypeError, ValueError):
            continue
        payload.append(dict(model_slug=r["model_slug"], taxonomy=tax, value=value,
                            unit="elo", source="custom", board="operator",
                            lab=r.get("lab"), effort=None, basis="operator-input"))
    result = upsert_ratings(con, payload)
    result["taxonomy"] = tax
    return result
