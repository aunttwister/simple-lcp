"""LMArena official HF dataset -> ``model_capabilities`` (P3 ingestion, CUR model-intel).

The queryable arena: ``lmarena-ai/leaderboard-dataset`` text/latest parquet
(~420 models, 29 categories, refreshed every few days — free HTTPS + DuckDB).

Mapping (LMArena category -> gateway task axis):
    coding -> code_generation            math -> reasoning_chain
    instruction_following -> planning    multi_turn -> casual_chat
    creative_writing -> casual_chat      hard_prompts_english -> reasoning_chain
    expert -> reasoning_chain
Language and industry-vertical categories have no gateway axis and are skipped;
so are ``overall`` (a blend) and ``exclude_ties`` (a methodology, not a skill).

Identity: LMArena publishes marketing names ("Claude Opus 5.5 (High)"). The
ingester normalises them and auto-matches to ``mi_models`` slugs where
unambiguous; unmatched rows keep the arena name so the identity queue (P2) can
reconcile them — nothing is silently dropped, nothing canonical is overwritten.

Provenance: rows are ``source='lmarena'`` with ``release_label`` = the
leaderboard publish date. Upsert keyed on (model, task_type, source='lmarena'),
so re-runs refresh rather than duplicate. Never touches ``lcp_benchmark`` or
``livebench`` rows — and therefore never a value a user edited.
"""
from __future__ import annotations

import json
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .model_identity_queue import (
    KIND_CAPABILITY_NAME,
    SOURCE_LMARENA,
    observe,
    resolve_canonical,
)

PARQUET_URL = ("https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset/"
               "resolve/main/text/latest-00000-of-00001.parquet")

CATEGORY_TO_TASK = {
    "coding": "code_generation",
    "math": "reasoning_chain",
    "instruction_following": "planning",
    "multi_turn": "casual_chat",
    "creative_writing": "casual_chat",
    "hard_prompts_english": "reasoning_chain",
    "expert": "reasoning_chain",
}


def _norm(name: str) -> str:
    """'Claude Opus 5.5 (High)' -> 'claude opus 5.5' (variant stripped)."""
    s = re.sub(r"\([^)]*\)", "", name).lower()
    return re.sub(r"[^a-z0-9.]+", " ", s).strip()


def _fetch_rows(timeout: float = 60.0):
    """(category, model_name, rating, rank, votes, publish_date) — latest snapshot
    per (category, model_name): the leaderboard refreshes in place, so keep the
    newest publish date and drop superseded rows."""
    import duckdb

    con = duckdb.connect()
    con.execute("SET enable_http_metadata_cache=false")
    cur = con.execute(f"""
        SELECT category, model_name, rating, rank, vote_count,
               leaderboard_publish_date
        FROM read_parquet('{PARQUET_URL}')
        WHERE category IN ({",".join(f"'{c}'" for c in CATEGORY_TO_TASK)})
        QUALIFY row_number() OVER (
            PARTITION BY category, model_name
            ORDER BY leaderboard_publish_date DESC, vote_count DESC) = 1
    """)
    rows = cur.fetchall()
    con.close()
    return rows


def ingest(con: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Pull the LMArena text leaderboard and upsert capability rows.

    A row is written ONLY when its arena name resolves to a canonical model through
    the registry's own routes (slug / alias / served id / benchmark key). Anything
    else — the long tail of names the registry does not yet contain — is recorded in
    the identity queue instead of being written into ``model_capabilities``, because a
    capability row keyed by a spelling no read path searches is a dead row: it can
    never be joined, and it makes the estate look better covered than it is.

    Returns {categories, rows, matched, queued, release_label}.
    """
    rows = _fetch_rows()
    # Latest snapshot per (category, model_name): the leaderboard refreshes in
    # place, so a superseded row must never win. Deduped here (not in the
    # fetch) so the contract holds regardless of the data source.
    latest: dict = {}
    for category, model_name, rating, rank, votes, pub in rows:
        key = (category, model_name)
        prev = latest.get(key)
        if prev is None or (pub or "") > (prev[5] or "") or (
                (pub or "") == (prev[5] or "") and (votes or 0) > (prev[4] or 0)):
            latest[key] = (category, model_name, rating, rank, votes, pub)
    rows = sorted(latest.values(), key=lambda r: (r[0], r[1]))
    now = datetime.now(timezone.utc).isoformat()
    release_label = max(r[5] for r in rows) if rows else None

    per_category: dict = {}
    for category, *_ in rows:
        per_category[category] = per_category.get(category, 0) + 1
    matched = queued = written = 0
    cur = None
    if not dry_run:
        cur = con.cursor()
        cur.execute("BEGIN")
    for category, model_name, rating, rank, votes, _pub in rows:
        task = CATEGORY_TO_TASK.get(category)
        if not task or model_name is None:
            continue
        canonical = resolve_canonical(con, model_name)
        if not canonical:
            queued += 1
            if not dry_run:
                observe(con, model_name, source=SOURCE_LMARENA,
                        kind=KIND_CAPABILITY_NAME,
                        evidence={"categories": [category],
                                  "rows": 1,
                                  "best_rank": int(rank) if rank else None,
                                  "rating": round(float(rating), 2)})
            continue
        matched += 1
        if dry_run:
            continue
        n = max(1, per_category.get(category, 1))
        # score = percentile within the category (higher is better, 0-100),
        # raw_score = the arena Elo itself.
        percentile = round(100.0 * (n - float(rank) + 1.0) / n, 2)
        cur.execute(
            "DELETE FROM model_capabilities WHERE model=? AND task_type=? AND source='lmarena'",
            (canonical, task))
        cur.execute(
            "INSERT INTO model_capabilities (model, task_type, score, source,"
            " benchmark_category, raw_score, updated_at, release_label)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (canonical, task, percentile, "lmarena", category,
             round(float(rating), 2), now, release_label))
        written += 1
    if not dry_run:
        con.commit()
    return {"categories": sorted({r[0] for r in rows if r[0] in CATEGORY_TO_TASK}),
            "rows": written, "matched": matched, "queued": queued,
            "release_label": release_label}


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else "data/costs.db"
    c = sqlite3.connect(db)
    print(json.dumps(ingest(c), indent=1))
