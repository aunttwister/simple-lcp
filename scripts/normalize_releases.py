#!/usr/bin/env python3
"""Keep one model per line at its newest release (policy A) — plan, then apply.

    python3 scripts/normalize_releases.py --db data/costs.db            # plan only
    python3 scripts/normalize_releases.py --db data/costs.db --apply    # register + retire

The plan is read-only, and `--apply` writes the inverse of what it is about to do
to `--undo-out` FIRST, so the change can be reverted with sqlite3 even if this
process dies halfway.

Why it is needed: the arena board moves with the vendor, so a registry holding
`claude-opus-5` while the provider serves `claude-opus-5-5` shows the operator a
release that no longer exists on the board — and the released one scores nothing.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import arena_poll, release_normalize as rn  # noqa: E402


def arena_candidates(con: sqlite3.Connection, enabled: bool) -> list[dict]:
    """The arena names that do not resolve, from a fresh board fetch."""
    if not enabled:
        return []
    try:
        boards = arena_poll.default_fetch()
    except Exception as exc:                                # network, duckdb, HF
        print(f"warning: could not read the arena boards ({exc}); registering without "
              f"arena aliases — the poll will not score the new releases until the "
              f"names are captured", file=sys.stderr)
        return []
    return arena_poll.release_candidates(con, boards)["candidates"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/costs.db")
    ap.add_argument("--apply", action="store_true", help="write; without it this is read-only")
    ap.add_argument("--no-arena", action="store_true",
                    help="skip the arena fetch (offline; registers without aliases)")
    ap.add_argument("--undo-out", default=None,
                    help="where to write the inverse SQL before applying")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    cands = arena_candidates(con, not args.no_arena)
    p = rn.plan(con, cands)

    print(rn.describe(p))
    if not args.apply:
        print("\n(plan only — pass --apply to write)")
        return 0

    undo = args.undo_out or f"{args.db}.undo-release-normalize.sql"
    Path(undo).write_text(rn.undo_sql(p))
    print(f"\nundo written to {undo}")

    receipt = rn.apply(con, p)
    print(f"registered {len(receipt['registered'])}, aliased {receipt['aliased']}, "
          f"retired {len(receipt['retired'])}")
    for r in receipt["registered"]:
        print(f"  + {r['slug']} (model_id {r['model_id']}) aliases={r['aliases']}")
    if receipt["retired"]:
        print(f"  - retired: {', '.join(receipt['retired'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
