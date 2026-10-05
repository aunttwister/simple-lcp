#!/usr/bin/env python3
"""Add DeepSeek peak rates to an LCP `gateway_config:pricing` row.

    python3 lcp_peak_db.py <path-to-costs.db> [--dry-run]

Pure and idempotent: for every DeepSeek-family row it sets peak_cache_hit /
peak_cache_miss / peak_output = exactly 2x that row's base rate. Non-DeepSeek
rows (Claude / GPT / Kimi / MiniMax) are left completely alone — they have no
peak window. Base rates are NEVER modified: re-pricing a model is a business
input, not a billing-schema change.

Run this against each instance's DB (staging and prod keep separate copies):
    staging: /your/data/app/lcp-staging/data/costs.db
    prod:    /your/data/app/lcp/data/costs.db
"""
import json
import sqlite3
import sys

SETTINGS_KEY = "gateway_config:pricing"
IS_PEAK = "deepseek"          # only DeepSeek bills peak / off-peak


def peak_row(row: dict) -> dict:
    out = dict(row)
    for k in ("cache_hit", "cache_miss", "output"):
        if k in out and isinstance(out[k], (int, float)):
            out["peak_" + k] = round(out[k] * 2, 10)
    return out


def apply(db: str, dry_run: bool = False) -> tuple[int, int]:
    c = sqlite3.connect(db)
    got = c.execute("select value from settings where key=?", (SETTINGS_KEY,)).fetchone()
    if not got:
        raise SystemExit(f"{db}: no {SETTINGS_KEY!r} row")
    rows = json.loads(got[0])

    touched = total = 0
    for i, r in enumerate(rows):
        total += 1
        if IS_PEAK in str(r.get("model", "")).lower():
            new = peak_row(r)
            if new != r:
                touched += 1
            rows[i] = new

    if not dry_run:
        c.execute("update settings set value=? where key=?",
                  (json.dumps(rows, indent=2), SETTINGS_KEY))
        c.commit()
    c.close()
    return touched, total


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    db = args[0]
    n, total = apply(db, dry_run=dry)
    print(f"{'DRY-RUN ' if dry else ''}{db}")
    print(f"  rows={total}  peaked={n}")

    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = json.loads(c.execute("select value from settings where key=?",
                                (SETTINGS_KEY,)).fetchone()[0])
    c.close()
    for r in rows:
        if IS_PEAK in str(r.get("model", "")).lower():
            print(f"    {r['provider']:12s} {r['model']:32s} "
                  f"{r['cache_hit']}/{r['cache_miss']}/{r['output']}"
                  f"  peak {r.get('peak_cache_hit')}/{r.get('peak_cache_miss')}"
                  f"/{r.get('peak_output')}")
