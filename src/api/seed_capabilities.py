"""Model capability storage and the model registry.

Capability scores are DECLARED data. The bundled ``data/declared_capabilities.json``
holds the matrix the router ranks on, ``seed_capabilities()`` writes it into a
fresh database, and the Models page / ``POST /api/models/capability/manual`` is
how it changes. The LiveBench import pipeline and the in-app benchmark executor
were removed (R13); ``source`` survives on each row as provenance, not as a live
input path.

The model registry maps each logical model name to its stable capability-matrix
key and provider-side model IDs; the provider keys double as the model's
provider list in the UI.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .logging_config import get_logger

logger = get_logger("lcp.seed_capabilities")

from typing import Optional

# Map LiveBench categories → LCP task types
LB_TO_LCP: dict[str, str] = {
    "reasoning": "reasoning_chain",
    "coding": "code_generation",
    "agentic_coding": "agentic_multi_step",
    "math": "reasoning_chain",
    "data_analysis": "research_deep",
    "language": "casual_chat",
    "instruction_following": "planning",
}

# Derived LCP task types — LiveBench has no dedicated category, so we mirror
# the closest related category. ``debugging`` and ``unit_tests`` are coding
# subskills, so both use the ``code_generation`` score as a proxy.
DERIVED_TASKS: dict[str, str] = {
    "debugging": "code_generation",
    "unit_tests": "code_generation",
}




# ── Declared capabilities (bundled seed) ─────────────────────────────────
# The matrix the router ranks on. It used to be imported from LiveBench
# leaderboard snapshots; that pipeline is gone (R13), so the matrix is DECLARED
# data: this bundled file is what a fresh database starts from, and the Models
# page / POST /api/models/capability/manual is how it changes. ``source`` on
# each row records where a score came from and is kept honest -- ``livebench``
# = imported from the 2026-06..09 snapshots, ``lcp_benchmark`` = produced by
# the retired benchmark executor, ``manual`` = hand-set.
CAPABILITIES_FILE = Path(__file__).resolve().parent / "data" / "declared_capabilities.json"


def load_declared_capabilities() -> list:
    """Read the bundled declared-capability rows (see the file's ``_why``)."""
    with open(CAPABILITIES_FILE, encoding="utf-8") as fh:
        return json.load(fh)["rows"]


def seed_capabilities(db_path: str) -> int:
    """Write the bundled declared capability rows into ``model_capabilities``.

    Idempotent and non-destructive: a row is matched on the same key the manual
    edit endpoint uses -- (model, task_type, source, release_label) -- so a
    re-run updates its own rows and never clobbers a hand-set ``manual`` score.
    Returns the number of rows written.
    """
    from src.api.models import ModelCapability

    rows = load_declared_capabilities()
    session = _get_session(db_path)
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    try:
        for row in rows:
            existing = session.query(ModelCapability).filter_by(
                model=row["model"],
                task_type=row["task_type"],
                source=row["source"],
                release_label=row.get("release_label"),
            ).first()
            if existing is not None:
                existing.score = row["score"]
                existing.raw_score = row.get("raw_score")
                existing.benchmark_category = row.get("benchmark_category")
                existing.updated_at = now
            else:
                session.add(ModelCapability(
                    model=row["model"],
                    task_type=row["task_type"],
                    score=row["score"],
                    source=row["source"],
                    benchmark_category=row.get("benchmark_category"),
                    raw_score=row.get("raw_score"),
                    release_label=row.get("release_label"),
                    updated_at=now,
                ))
            written += 1
        session.commit()
    finally:
        session.close()
    return written



def _get_session(db_path: str):
    from src.api.models import get_engine, get_session, Base

    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return get_session(engine)


def _default_db_path() -> str:
    """Resolve the DB path the same way the gateway does (COST_DB or data/costs.db)."""
    import os
    return os.environ.get("COST_DB", "data/costs.db")


def main() -> None:
    """CLI: seed the model registry + the declared capability matrix.

    Usage:
        python -m src.api.seed_capabilities                    # registry + capabilities
        python -m src.api.seed_capabilities --registry-only
        python -m src.api.seed_capabilities --capabilities-only --db /app/data/costs.db
        python -m src.api.seed_capabilities --sync             # re-apply curated registry defaults
    """
    import argparse

    parser = argparse.ArgumentParser(description="Seed LCP model data")
    parser.add_argument("--db", default=_default_db_path(), help="path to SQLite DB")
    parser.add_argument("--registry-only", action="store_true", help="only seed the model registry")
    parser.add_argument("--capabilities-only", action="store_true", help="only seed the declared capability matrix")
    parser.add_argument("--sync", action="store_true", help="re-apply curated registry defaults to existing rows")
    args = parser.parse_args()

    db_path = args.db

    if args.capabilities_only:
        print(f"Seeded {seed_capabilities(db_path)} declared capability rows")
        return

    if args.registry_only:
        print(f"Seeded {seed_model_registry(db_path, sync=args.sync)} registry entries")
        return

    r = seed_model_registry(db_path, sync=args.sync)
    c = seed_capabilities(db_path)
    print(f"Seeded {r} registry entries + {c} declared capability rows")


def resolve_active_rows(rows, registry: dict, release: Optional[str] = None):
    """Filter capability rows to each model's active release.

    Shared by ``load_capability_matrix`` (router) and the capability API
    (UI) so both always agree on which release's scores are live:

      * ``release`` given → exact ``release_label`` filter (+ legacy NULL rows);
      * ``active_release`` pinned on the model → that release (+ legacy);
      * otherwise → newest available ``release_label`` (+ legacy).

    Legacy rows (``release_label`` NULL) are always kept as a fallback so
    models with only unversioned scores still participate.
    """
    if release is not None:
        return [r for r in rows if r.release_label == release or r.release_label is None]

    # Reverse index: capability row model key → registry entry. Rows are keyed
    # by the model's benchmark_key (and sometimes by a provider-side model ID),
    # so map every reachable spelling back to its entry.
    by_key: dict[str, dict] = {}
    for entry in registry.values():
        keys = [entry.get("benchmark_key"), *((entry.get("provider_mappings") or {}).values())]
        for key in keys:
            if key:
                by_key.setdefault(key.lower(), entry)

    by_model: dict[str, dict[Optional[str], list]] = {}
    for row in rows:
        by_model.setdefault(row.model, {}).setdefault(row.release_label, []).append(row)

    out: list = []
    for model, buckets in by_model.items():
        entry = by_key.get(model.lower(), registry.get(model, {}))
        active = entry.get("active_release")
        dated = [rel for rel in buckets if rel is not None]
        if active in buckets:
            selected = active
        elif active in (None, "latest", "") or not dated:
            selected = max(dated) if dated else None
        else:
            # Pinned release has no rows yet — fall back to the newest anyway.
            selected = max(dated)
        out.extend(buckets.get(selected, []))
        out.extend(buckets.get(None, []))
    return out


def effective_releases(rows, registry: dict) -> dict[str, str]:
    """Return {model_key: release_label} for the release actually in effect.

    Mirrors ``resolve_active_rows``: the pinned ``active_release`` when rows
    exist for it, otherwise the newest available ``release_label``. Models
    with only legacy (NULL) rows are omitted.
    """
    by_key: dict[str, dict] = {}
    for entry in registry.values():
        keys = [entry.get("benchmark_key"), *((entry.get("provider_mappings") or {}).values())]
        for key in keys:
            if key:
                by_key.setdefault(key.lower(), entry)

    by_model: dict[str, set] = {}
    for row in rows:
        if row.release_label:
            by_model.setdefault(row.model, set()).add(row.release_label)

    out: dict[str, str] = {}
    for model, releases in by_model.items():
        if not releases:
            continue
        entry = by_key.get(model.lower(), registry.get(model, {}))
        active = entry.get("active_release")
        if active in releases:
            out[model] = active
        else:
            out[model] = max(releases)
    return out


def load_capability_matrix(db_path: str, release: Optional[str] = None) -> dict[str, dict[str, float]]:
    """Load the capability matrix from the DB for the ACTIVE release per model.

    ``release`` is treated as an exact ``release_label`` filter when given;
    otherwise each model resolves to its own release via
    ``resolve_active_rows`` (``active_release`` pin → newest release).

    Returns {task_type: {model_name: score}}.
    Sources priority: gateway_yaml > manual > lcp_benchmark > livebench > arena
    """
    from src.api.models import ModelCapability

    session = _get_session(db_path)
    SOURCE_PRIORITY = {"gateway_yaml": 0, "manual": 1, "lcp_benchmark": 2, "livebench": 3, "arena": 4}

    rows = session.query(ModelCapability).all()
    session.close()

    registry = load_model_registry(db_path) if release is None else {}
    candidate_rows = resolve_active_rows(rows, registry, release)

    # Pick the best source per (model, task).
    best: dict[tuple[str, str], dict] = {}
    for row in candidate_rows:
        key = (row.task_type, row.model)
        existing = best.get(key)
        new_prio = SOURCE_PRIORITY.get(row.source, 99)
        if existing is not None and SOURCE_PRIORITY.get(existing["source"], 99) <= new_prio:
            continue
        best[key] = {"score": row.score, "source": row.source}

    matrix: dict[str, dict[str, float]] = defaultdict(dict)
    for (task, model), data in best.items():
        matrix[task][model] = data["score"]

    # Safety net: derived task types (debugging ← code_generation) so the
    # router always has a debugging score even before a re-seed writes rows.
    for derived, source in DERIVED_TASKS.items():
        if source in matrix:
            matrix.setdefault(derived, {}).update(matrix[source])

    return dict(matrix)


# ── Model registry (logical → benchmark → providers) ────────────────────────

# Curated default registry. Seeded into the model_registry table on first run;
# subsequent changes are made via the DB (dashboard / API), NOT this dict.
DEFAULT_MODEL_REGISTRY: list[dict] = [
    {
        "logical_name": "deepseek-v4-pro",
        "benchmark_key": "deepseek-v4-pro",
        "provider_mappings": {
            "deepseek": "deepseek-v4-pro",
            "opencode": "deepseek-v4-pro",
            "commandcode": "deepseek/deepseek-v4-pro",
        },
        "active_release": "2026-08-13",
        "benchmark_release": "2026-06-25",
    },
    {
        "logical_name": "deepseek-v4-flash",
        "benchmark_key": "deepseek-v4-flash",
        "provider_mappings": {
            "deepseek": "deepseek-v4-flash",
            "opencode": "deepseek-v4-flash",
            "commandcode": "deepseek/deepseek-v4-flash",
        },
        "active_release": "2026-07-31",
        "benchmark_release": "2026-06-25",
    },
    {
        "logical_name": "claude-sonnet-5",
        "benchmark_key": "claude-sonnet-5",
        "provider_mappings": {"commandcode": "claude-sonnet-5"},
    },
    {
        "logical_name": "claude-fable-5",
        "benchmark_key": "claude-fable-5",
        "provider_mappings": {"commandcode": "claude-fable-5"},
    },
    {
        "logical_name": "claude-opus-5",
        "benchmark_key": "claude-opus-5",
        "provider_mappings": {"commandcode": "claude-opus-5"},
    },
    {
        "logical_name": "gpt-5.6-sol",
        "benchmark_key": "gpt-5.6-sol",
        "provider_mappings": {},
    },
    {
        "logical_name": "gpt-5.6-terra",
        "benchmark_key": "gpt-5.6-terra",
        "provider_mappings": {"commandcode": "gpt-5.6-terra"},
    },
    {
        "logical_name": "gpt-5.6-luna",
        "benchmark_key": "gpt-5.6-luna",
        "provider_mappings": {"commandcode": "gpt-5.6-luna"},
    },
    {
        "logical_name": "kimi-k3",
        "benchmark_key": "kimi-k3",
        "provider_mappings": {"commandcode": "moonshotai/Kimi-K3"},
    },
    {
        "logical_name": "minimax-m3",
        "benchmark_key": "minimax-m3",
        "provider_mappings": {"commandcode": "MiniMaxAI/MiniMax-M3"},
    },
    {
        "logical_name": "qwen3.8-max",
        "benchmark_key": "qwen-3.8-max",
        "provider_mappings": {"commandcode": "Qwen/Qwen3.8-Max"},
    },
    {
        "logical_name": "grok-4.5",
        "benchmark_key": "grok-4.5",
        "provider_mappings": {},
    },
    {
        "logical_name": "gemini-3.6-flash",
        "benchmark_key": "gemini-3.6-flash",
        "provider_mappings": {},
    },
    {
        "logical_name": "qwen3.8-flash-next",
        "benchmark_key": "qwen3.8-flash-next",
        "provider_mappings": {
            "llamacpp": "qwen3.8-flash-next",
        },
        "active_release": "2026-08-25",
        "benchmark_release": "2026-08-25",
    },
]


def seed_model_registry(db_path: str, sync: bool = False) -> int:
    """Seed the registry from DEFAULT_MODEL_REGISTRY.

    Writes into ``mi_models`` (the canonical model) and ``mi_model_names`` (each
    provider's spelling, under that provider's ``mi_sources`` row). Insert-only by
    default: existing rows are NOT overwritten — the DB is the source of truth once
    seeded. With ``sync=True``, curated defaults are re-applied (benchmark_key,
    release fields, provider names) so a code-level identity→release migration can be
    pushed out without wiping admin edits.

    The signature and return value are unchanged from the ``model_registry`` version,
    so ``router.get_model_registry`` and the seeding CLI keep working as before.
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    try:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                       " AND name='mi_models'").fetchone() is None:
            # The registry lives in tables the migrations own. A database without them
            # predates the identity registry, so there is nowhere to seed — that is a
            # no-op, not an error.
            logger.warning("seed_model_registry_unmigrated", db_path=db_path)
            return 0
        for entry in DEFAULT_MODEL_REGISTRY:
            logical = entry["logical_name"]
            row = con.execute(
                "SELECT model_id FROM mi_models WHERE slug = ?", (logical,)).fetchone()
            if row:
                model_id = int(row["model_id"])
                if sync:
                    con.execute(
                        "UPDATE mi_models SET benchmark_key = COALESCE(?, benchmark_key),"
                        " active_release = COALESCE(?, active_release),"
                        " benchmark_release = COALESCE(?, benchmark_release),"
                        " quantization = COALESCE(?, quantization) WHERE model_id = ?",
                        (entry.get("benchmark_key"), entry.get("active_release"),
                         entry.get("benchmark_release"), entry.get("quantization"),
                         model_id))
                    count += 1
            else:
                con.execute(
                    "INSERT INTO mi_models (slug, display_name, family, status,"
                    " benchmark_key, active_release, benchmark_release, quantization,"
                    " created_at) VALUES (?,?,?,'active',?,?,?,?,?)",
                    (logical, logical, None, entry.get("benchmark_key"),
                     entry.get("active_release"), entry.get("benchmark_release"),
                     entry.get("quantization"), now))
                model_id = int(con.execute(
                    "SELECT model_id FROM mi_models WHERE slug = ?", (logical,)
                ).fetchone()["model_id"])
                count += 1

            # the canonical slug and the benchmark key are names too
            _ensure_source(con, "canonical", "canonical", None, now)
            _ensure_name(con, model_id, "canonical", logical, 1, "high",
                         "canonical slug", now)
            if entry.get("benchmark_key"):
                _ensure_source(con, "benchmark", "benchmark_arena", None, now)
                _ensure_name(con, model_id, "benchmark", entry["benchmark_key"], 0,
                             "high", "benchmark snapshot key", now)

            for provider, name in (entry.get("provider_mappings") or {}).items():
                ep = con.execute(
                    "SELECT endpoint_id FROM mi_endpoints WHERE slug = ?",
                    (provider,)).fetchone()
                _ensure_source(con, provider, "provider_endpoint",
                               int(ep["endpoint_id"]) if ep else None, now)
                _ensure_name(con, model_id, provider, name, 0, "high",
                             "curated provider mapping", now, verified=True)
        con.commit()
    finally:
        con.close()
    print(f"Seeded {count} model registry entries")
    return count


def _ensure_source(con, slug: str, kind: str, endpoint_id, now: str) -> None:
    con.execute(
        "INSERT OR IGNORE INTO mi_sources (slug, kind, endpoint_id, created_at)"
        " VALUES (?,?,?,?)", (slug, kind, endpoint_id, now))


def _ensure_name(con, model_id: int, source_slug: str, name: str, is_primary: int,
                 confidence: str, evidence: str, now: str, *,
                 verified: bool = False) -> None:
    src = con.execute(
        "SELECT source_id FROM mi_sources WHERE slug = ?", (source_slug,)).fetchone()
    if src is None:
        return
    con.execute(
        "INSERT INTO mi_model_names (model_id, source_id, name, is_primary, confidence,"
        " evidence, first_seen, last_seen, verified_at) VALUES (?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(source_id, name) DO UPDATE SET"
        " model_id = excluded.model_id",
        (model_id, int(src["source_id"]), name, is_primary, confidence, evidence,
         now, now, now if verified else None))


def load_model_registry(db_path: str) -> dict[str, dict]:
    """Load the model registry as {logical_name: {...}}.

    Each entry: {benchmark_key, provider_mappings, active_release,
    benchmark_release, quantization}. Built from ``mi_models`` plus ``mi_model_names``
    (the provider-asserted spellings) instead of the retired ``model_registry`` table.

    The return shape is deliberately identical, so every caller — including the router's
    ``_alias_to_logical`` index and ``benchmark_model_name``/``provider_model_name`` —
    is unaffected. Unlike the old table this covers **every** canonical model, so a
    model that exists in the identity registry is no longer invisible to routing.
    """
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    registry: dict[str, dict] = {}
    try:
        try:
            rows = con.execute(
                "SELECT model_id, slug, benchmark_key, active_release, benchmark_release,"
                " quantization FROM mi_models").fetchall()
        except sqlite3.OperationalError:
            # No mi_models table: the database predates the identity registry. That is an
            # empty registry, not an error — the caller seeds one (get_model_registry does
            # exactly that) or falls back to normalising the string.
            logger.warning("load_model_registry_unmigrated", db_path=db_path)
            return {}
        for r in rows:
            entry = {
                "benchmark_key": r["benchmark_key"],
                "provider_mappings": {},
                "active_release": r["active_release"],
                "benchmark_release": r["benchmark_release"],
                "quantization": r["quantization"],
            }
            registry[r["slug"]] = entry
            try:
                names = con.execute(
                    "SELECT s.slug AS source, n.name FROM mi_model_names n"
                    " JOIN mi_sources s ON s.source_id = n.source_id"
                    " WHERE n.model_id = ? AND s.kind = 'provider_endpoint'"
                    " ORDER BY (n.verified_at IS NULL),"
                    " CASE n.confidence WHEN 'verified' THEN 0 WHEN 'high' THEN 1"
                    "   WHEN 'observed' THEN 2 ELSE 3 END, n.name",
                    (r["model_id"],)).fetchall()
            except sqlite3.OperationalError:
                names = []
            for n in names:
                # first (best-attested) spelling wins, matching model_names.provider_mappings_for
                entry["provider_mappings"].setdefault(n["source"], n["name"])
    finally:
        con.close()
    return registry


if __name__ == "__main__":
    main()

