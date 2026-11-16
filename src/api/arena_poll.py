"""Poll LMArena's published boards into the ratings store.

Two board families, deliberately kept apart (operator decision 2026-11-15):

  * **text boards** publish **Elo** (``rating``, ~1500, with ``vote_count``)
  * **agent boards** publish a **net improvement** (``score``, small signed)

Same rank/percentile machinery, different value column. The unit is carried on
every rating row, and nothing here ever ranks the two families together.

Source: the dataset's own parquet files, one per board
(``<config>/latest-00000-of-00001.parquet``), read with DuckDB. This replaced
the ``datasets-server`` ``/rows`` API, which returns HTTP 500/502 under any real
paging load (it throttled this poll to a standstill on 2026-11-15) and returns
100 rows at a time — the parquet carries every model in one request, so the poll
cannot be silently truncated by a paging failure.

Resolution (R1/R2/R3, operator-approved):
  * ``mi_model_names`` is consulted FIRST, so an operator-declared alias wins;
  * then the slug's own spelling (R1 normalisation, exact match only);
  * a trailing effort suffix ``(High)``/``(Max)``/``(xHigh)``… is stripped into
    the ``effort`` DIMENSION (R2=C) rather than creating a second model;
  * a name that resolves to nothing is IGNORED (R3) — it is never guessed at,
    because a wrong link fabricates leaderboard data.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

PARQUET_BASE = ("https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset/"
                "resolve/main")
PARQUET = PARQUET_BASE + "/{config}/latest-00000-of-00001.parquet"

# Board -> gateway axes. **This is now a SEED, not the source of truth.**
#
# The operator rejected the hardcoded shape (2026-11-15): "you need to enable user
# to map taxonomies". `arena_taxonomy.taxonomy_map` owns which board feeds which
# axis; the 14 pairs below are inserted as `is_default = 1` on the first poll, so
# the page looks identical the moment the mapping lands and the operator takes
# ownership by editing. Nothing reads these dicts to publish ratings any more —
# they only classify a fetched board (text = Elo, agent = net improvement) and
# supply the seed.
#
# The board FAMILY still has to be known here because it decides the value column
# and the unit: a text board publishes `rating` (Elo), an agent board `score`.
TEXT_BOARDS = {
    "coding": ["code_generation", "debugging", "unit_tests"],
    "math": ["reasoning_chain"],
    "hard_prompts_english": ["reasoning_chain"],
    "expert": ["reasoning_chain"],
    "instruction_following": ["planning"],
    "multi_turn": ["casual_chat"],
    "creative_writing": ["casual_chat"],
}
AGENT_BOARDS = {
    "agent": ["agentic_multi_step"],
    "agent_task_outcome_explicit": ["agentic_multi_step"],
    "agent_steerability": ["planning"],
    "agent_tool_hallucination": ["research_deep"],
    "agent_bash_reliability": ["debugging"],
    "agent_bash_recovery_steps": ["debugging"],
    "agent_praise_complaint": ["casual_chat"],
}

# Tab order for arena taxonomies: the gateway's own axes, in routing order, so
# the UI never reshuffles itself between polls.
AXIS_ORDER = ["code_generation", "reasoning_chain", "planning", "casual_chat",
              "agentic_multi_step", "debugging", "unit_tests", "research_deep"]

EFFORT = re.compile(r"\s*[\(\[]\s*(x?high|max|low|medium|minimal)\s*[\)\]]\s*$", re.I)


def norm(name: str) -> str:
    """'GLM 5.3 Flash' / 'glm_5.3-flash' -> 'glm-5.3-flash' (R1 spelling)."""
    s = re.sub(r"[\s_]+", "-", (name or "").strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def split_effort(name: str) -> tuple[str, str | None]:
    """R2=C: '(High)' is a dimension of the SAME model, not a second model."""
    m = EFFORT.search(name or "")
    return EFFORT.sub("", name or ""), (m.group(1).lower() if m else None)


def fetch_text_boards() -> dict[str, list[dict]]:
    """EVERY text category in one parquet read, keyed by LMArena category.

    All 29, not the 7 an axis happens to use. The file arrives in one read, so
    keeping the other 22 costs nothing — and discarding them here is what made the
    mapping impossible: the operator's board rows have to exist before he can map
    them (see `arena_taxonomy.store_boards`).
    """
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("SET enable_http_metadata_cache=false")
        cur = con.execute(
            "SELECT model_name, organization, rating, rank, vote_count, category,"
            " leaderboard_publish_date FROM read_parquet(?)",
            [PARQUET.format(config="text")])
        out: dict[str, list[dict]] = {}
        for model_name, org, rating, rank, votes, cat, published in cur.fetchall():
            out.setdefault(cat, []).append(
                {"model_name": model_name, "organization": org, "rating": rating,
                 "rank": rank, "vote_count": votes, "category": cat,
                 "published": published})
        return out
    finally:
        con.close()


def fetch_agent_board(config: str) -> list[dict]:
    """One agent board. Its rows all carry category='overall'; the CONFIG is the
    board name (each agent board is its own file and its own tournament)."""
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("SET enable_http_metadata_cache=false")
        cur = con.execute(
            "SELECT model_name, organization, score, rank, session_count,"
            " leaderboard_publish_date FROM read_parquet(?)",
            [PARQUET.format(config=config)])
        return [{"model_name": n, "organization": o, "score": s, "rank": r,
                 "session_count": c, "category": config, "published": p}
                for n, o, s, r, c, p in cur.fetchall()]
    finally:
        con.close()


def default_fetch() -> dict[str, list[dict]]:
    """Every board we poll, keyed by board name. One parquet read per family."""
    boards = fetch_text_boards()
    for cfg in AGENT_BOARDS:
        boards[cfg] = fetch_agent_board(cfg)
    return boards


def load_resolvers(con) -> tuple[dict, dict]:
    """(alias -> slug, slug-spelling -> slug). Aliases win; see module docstring."""
    alias: dict[str, str] = {}
    for r in con.execute(
            "SELECT n.name AS arena, m.slug AS slug FROM mi_model_names n"
            " JOIN mi_models m ON m.model_id = n.model_id"):
        alias[norm(r["arena"])] = r["slug"]
    known = {norm(r[0]): r[0] for r in con.execute("SELECT slug FROM mi_models")}
    return alias, known


def resolve(name: str, alias: dict, known: dict) -> tuple[str | None, str | None]:
    base, effort = split_effort(name)
    key = norm(base)
    return alias.get(key) or known.get(key), effort


def percentile(rank, n) -> float | None:
    """100 x (1 - (rank-1)/(n-1)) — the board's own standing, expressed as %."""
    if not rank or not n or n < 2:
        return None
    return round(100.0 * (1 - (rank - 1) / (n - 1)), 1)


def _axis_map(con) -> tuple[dict[str, list[str]], bool]:
    """(board -> [axis], whether any mapping exists at all).

    The map is the source of truth. When it is EMPTY (a fresh install, before the
    first poll seeds it) this falls back to the seed pairs so the very first poll
    still produces something — but once a single row exists, the operator's map
    alone decides, so deleting a default really removes it.
    """
    ax: dict[str, list[str]] = {}
    try:
        for board, tax in con.execute("SELECT board, taxonomy FROM taxonomy_map"):
            ax.setdefault(board, []).append(tax)
    except Exception:  # noqa: BLE001 — pre-040 database / unit fixture without the table
        ax = {}
    if ax:
        return ax, True
    seed: dict[str, list[str]] = {}
    for board, axes in TEXT_BOARDS.items():
        seed[board] = list(axes)
    for board, axes in AGENT_BOARDS.items():
        seed[board] = list(axes)
    return seed, False


def build_rows(con, boards: dict[str, list[dict]], *, visible=None) -> dict:
    """Turn fetched boards into rating rows.

    ``visible`` is the set of provider-registered, non-retired slugs; when given,
    anything outside it is IGNORED (R3) rather than stored.
    """
    from .model_ratings import visible_models

    if visible is None:
        visible = visible_models(con)
    alias, known = load_resolvers(con)
    # WHICH board feeds which axis is the operator's mapping, not a constant here.
    ax_by_board, mapped = _axis_map(con)
    rows: list[dict] = []
    stats: dict[str, dict] = {}
    unmatched: dict[str, set] = {}

    def _add(board: str, axes: list[str], name: str, value, rank, n, lab, unit,
             basis, published=None):
        slug, effort = resolve(name, alias, known)
        if not slug or slug not in visible:
            if not slug:
                unmatched.setdefault(board, set()).add(name)
                stats[board]["unmatched"] += 1
            else:
                stats[board]["not_registered"] += 1
            return
        for ax in axes:
            rows.append(dict(model_slug=slug, taxonomy=ax, value=value, unit=unit,
                             rank=rank, n=n, percentile=percentile(rank, n),
                             source="arena", board=board, lab=lab, effort=effort,
                             basis=basis, published=published))
            stats[board]["rows"] += 1
        # `matched` counts models we could PLACE on this board (registered and
        # resolvable), independent of whether an axis claims it. That is what makes
        # "this board scores 11 of yours, map it" visible in Manage taxonomy.
        stats[board]["matched"] += 1

    for board, members in boards.items():
        # EVERY board is classified and counted. Which of them reach a taxonomy is
        # the MAPPING's decision now, so nothing is skipped here on the strength of
        # a hardcoded axis table.
        stats[board] = {"fetched": len(members), "matched": 0, "unmatched": 0,
                        "not_registered": 0, "rows": 0}
        axes = ax_by_board.get(board, [])
        unit = "net_improvement" if board in AGENT_BOARDS else "elo"
        value_key = "score" if board in AGENT_BOARDS else "rating"
        basis = "arena-net-improvement" if board in AGENT_BOARDS else "arena-elo"
        n = len(members)
        for r in members:
            # `axes` may be EMPTY (one of the 22 boards nothing maps to yet) — the
            # name still has to be counted and matched, because the row is stored
            # for the catalogue and only reaches a board once it is mapped.
            _add(board, axes, r.get("model_name") or "", r.get(value_key),
                 r.get("rank"), n, r.get("organization"), unit, basis,
                 r.get("published"))

    # Keep the newest-effort row when a model appears at several efforts on ONE
    # board (R2=C): the highest effort is what the operator would route to.
    _effort_rank = {None: 99, "minimal": 0, "low": 1, "medium": 2, "high": 3,
                    "xhigh": 4, "max": 5}
    best: dict[tuple, dict] = {}
    for r in rows:
        key = (r["model_slug"], r["taxonomy"], r["unit"], r["board"])
        cur = best.get(key)
        if cur is None or _effort_rank[r["effort"]] > _effort_rank[cur["effort"]]:
            best[key] = r
    rows = list(best.values())

    return {"rows": rows, "stats": stats,
            "unmatched": {b: sorted(v) for b, v in unmatched.items()},
            "fetched_at": _now()}


# ── release watcher ─────────────────────────────────────────────────────────
# "we're behind, and that's the real answer" — the operator asked for this to be
# tracked. It is a REPORT, never an automatic registry write: registering a model
# needs its provider model id, which a leaderboard does not carry.

def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9.]+", (name or "").lower()) if t]


def line_key(name: str) -> str:
    """The part of a name that identifies its LINE, with the version dropped.

    'Claude Opus 5.5 (High)' -> 'claude opus' ; 'claude-opus-5' -> 'claude opus'.
    Marketing names and slugs meet here: the trailing numeric token is the
    release, everything before it is the family.
    """
    toks = [t for t in _tokens(split_effort(name)[0]) if not re.fullmatch(r"v?\d+(\.\d+)*", t)]
    return " ".join(toks)


def _version(name: str) -> tuple[int, ...]:
    """The numeric release of a name: 'Claude Opus 5.5 (High)' -> (5, 5).

    A four-digit-or-longer run is a DATE, not a release ('Grok 4 (0709)',
    'DeepSeek-V4.1-Flash-20261017'), so it is dropped — otherwise a dated
    snapshot reads as a later release than the model it snapshots.
    """
    out: list[int] = []
    for tok in _tokens(split_effort(name)[0]):
        m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", tok)
        if m:
            parts = m.group(1).split(".")
            out += [int(x) for x in parts if len(x) < 4]
    return tuple(out)


def is_newer(candidate: str, registered: str) -> bool | None:
    """Whether an arena name looks like a LATER release of a line we carry.

    ``None`` when neither side carries a version — the caller must not read that
    as "no".
    """
    a, b = _version(candidate), _version(registered)
    if not a or not b:
        return None
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def release_candidates(con, boards: dict[str, list[dict]]) -> dict:
    """Arena names that do NOT resolve, grouped against the registry's families.

    Two answers, both advisory:
      * ``candidates`` — an unresolved arena name whose line matches a registered
        model's line, flagged ``newer`` when its release number is higher. Newer
        ones are listed first; that is the operator's "let's have the latest
        releases" question.
      * ``not_on_any_board`` — a registered model arena does not score at all.
    """
    alias, known = load_resolvers(con)
    reg = [dict(r) for r in con.execute(
        "SELECT slug, family, status FROM mi_models WHERE status='active'")]
    by_line: dict[str, list[str]] = {}
    for m in reg:
        by_line.setdefault(line_key(m["slug"]), []).append(m["slug"])

    seen: dict[str, dict] = {}
    for board, members in boards.items():
        if board not in TEXT_BOARDS and board not in AGENT_BOARDS:
            continue
        n = len(members)
        for r in members:
            name = r.get("model_name") or ""
            slug, effort = resolve(name, alias, known)
            if slug:
                continue
            key = norm(name)
            entry = seen.setdefault(key, {"arena_name": name, "boards": [],
                                          "organization": r.get("organization"),
                                          "best_rank": None, "n": n})
            entry["boards"].append(board)
            rank = r.get("rank")
            if rank and (entry["best_rank"] is None or rank < entry["best_rank"]):
                entry["best_rank"] = rank

    candidates = []
    for e in seen.values():
        line = line_key(e["arena_name"])
        same_line = sorted({s for s in by_line.get(line, [])})
        if not same_line:
            continue
        e["registered_same_line"] = same_line
        e["boards"] = sorted(set(e["boards"]))
        e["newer"] = any(is_newer(e["arena_name"], s) for s in same_line)
        candidates.append(e)
    candidates.sort(key=lambda e: (not e["newer"], e["best_rank"] or 10 ** 6))

    on_board = set(alias.get(norm(r.get("model_name") or ""), None)
                   for members in boards.values() for r in members)
    on_board = {s for s in on_board if s}
    absent = sorted(m["slug"] for m in reg if m["slug"] not in on_board)
    return {"candidates": candidates, "not_on_any_board": absent,
            "newer_count": sum(1 for c in candidates if c["newer"]),
            "registry_active": len(reg), "unresolved_names": len(seen)}
