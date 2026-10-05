#!/usr/bin/env python3
"""Correct the deepseek-v4-pro row in an LCP `gateway_config:pricing` list.

    python3 lcp_v4pro_price_db.py <path-to-costs.db> [--dry-run]

The row previously held **MiMo V2.6 Pro's** catalogue rates
(0.003625 / 0.435 / 0.87) instead of DeepSeek-V4-Pro-0813's own rates. This
rewrites only rows whose model contains ``v4-pro``; every other row — including
every other DeepSeek row — is left byte-identical.

Correct values (api-docs.deepseek.com/quick_start/pricing, fetched 2026-10-05):
    off-peak  cache_hit 0.022  cache_miss 0.66  output 1.98
    peak      cache_hit 0.044  cache_miss 1.32  output 3.96     (peak = exactly 2x)

Pure and idempotent: a second run reports 0 changed rows.

Run against each instance's DB (staging and prod keep separate copies):
    staging: /your/data/app/lcp-staging/data/costs.db
    prod:    /your/data/app/lcp/data/costs.db
"""
import json
import sqlite3
import sys

SETTINGS_KEY = "gateway_config:pricing"
MATCH = "v4-pro"

CORRECT = {
    "cache_hit": 0.022,
    "cache_miss": 0.66,
    "output": 1.98,
    "peak_cache_hit": 0.044,
    "peak_cache_miss": 1.32,
    "peak_output": 3.96,
}

WRONG = {  # MiMo V2.6 Pro's table — what we expect to find before the fix
    "cache_hit": 0.003625,
    "cache_miss": 0.435,
    "output": 0.87,
    "peak_cache_hit": 0.00725,
    "peak_cache_miss": 0.87,
    "peak_output": 1.74,
}


def fix_row(row: dict) -> tuple[dict, bool]:
    out = dict(row)
    before = {k: out.get(k) for k in CORRECT}
    out.update(CORRECT)
    return out, before != CORRECT


def apply(db: str, dry_run: bool = False) -> tuple[int, int, list]:
    c = sqlite3.connect(db)
    got = c.execute("select value from settings where key=?", (SETTINGS_KEY,)).fetchone()
    if not got:
        raise SystemExit(f"{db}: no {SETTINGS_KEY!r} row")
    rows = json.loads(got[0])

    changed = 0
    report = []
    for i, r in enumerate(rows):
        if MATCH in str(r.get("model", "")).lower() or MATCH in str(r.get("model", "")):
            new, was_changed = fix_row(r)
            rows[i] = new
            if was_changed:
                changed += 1
            report.append((r.get("provider"), r.get("model"),
                           {k: r.get(k) for k in CORRECT},
                           {k: new[k] for k in CORRECT}))

    if not dry_run and changed:
        c.execute("update settings set value=? where key=?",
                  (json.dumps(rows, indent=2), SETTINGS_KEY))
        c.commit()
    c.close()
    return changed, len(rows), report


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    db = args[0]
    changed, total, report = apply(db, dry_run=dry)
    print(f"{'DRY-RUN ' if dry else ''}{db}")
    print(f"  rows={total}  v4-pro rows={len(report)}  changed={changed}")
    for prov, model, before, after in report:
        print(f"    {prov}/{model}")
        print(f"      was: {before}")
        print(f"      now: {after}")
    for prov, model, before, after in report:
        if before != WRONG and before != CORRECT:
            print(f"    !! {prov}/{model}: pre-existing value was neither the known-wrong "
                  f"nor the corrected set — review before trusting")
