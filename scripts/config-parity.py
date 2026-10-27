#!/usr/bin/env python3
"""Validate an LCP gateway config against the same rule CI uses.

CI builds its config from `SEED_CONFIG`; prod runs whatever is in the `settings` table after
UI edits. Before this, nothing checked the two against one rule, so a config that breaks prod
could pass CI green.

Usage:
    # what CI does: validate the seed
    python scripts/config-parity.py --seed

    # what prod actually runs: read the live config straight from the DB
    python scripts/config-parity.py --db /app/data/costs.db

    # machine-readable
    python scripts/config-parity.py --db /app/data/costs.db --json

Exit codes: 0 = valid, 1 = problems found, 2 = could not read the config.
Never prints credentials: only section names, provider names, profile names and models.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.config import SEED_CONFIG, validate_gateway_config  # noqa: E402


def _from_db(path: str) -> dict:
    """Read every `gateway_config:<section>` row from the settings table."""
    import sqlite3

    if not os.path.exists(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM settings WHERE key LIKE 'gateway_config:%'")
        raw = {}
        for key, value in cur.fetchall():
            section = key.split(":", 1)[1]
            try:
                raw[section] = json.loads(value)
            except (TypeError, ValueError):
                raw[section] = value
        return raw
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", action="store_true", help="validate SEED_CONFIG (what CI does)")
    ap.add_argument("--db", metavar="PATH", help="validate the live config in this costs.db")
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args()

    if args.seed:
        raw, source = SEED_CONFIG, "SEED_CONFIG"
    elif args.db:
        try:
            raw, source = _from_db(args.db), args.db
        except Exception as exc:  # noqa: BLE001
            print(f"could not read config: {exc}", file=sys.stderr)
            return 2
    else:
        ap.error("pass --seed or --db PATH")

    problems = validate_gateway_config(raw)

    if args.json:
        print(json.dumps({
            "source": source,
            "valid": not problems,
            "problems": problems,
            "providers": sorted((raw.get("providers") or {}).keys()),
            "profiles": sorted((raw.get("profiles") or {}).keys()),
        }, indent=2))
    else:
        print(f"source: {source}")
        print(f"providers: {len(raw.get('providers') or {})}  "
              f"profiles: {len(raw.get('profiles') or {})}")
        if problems:
            print(f"INVALID — {len(problems)} problem(s):")
            for p in problems:
                print(f"  - {p}")
        else:
            print("VALID — every chain step names a declared provider with a model and a base_url")

    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
