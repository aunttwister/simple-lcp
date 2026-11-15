#!/usr/bin/env python3
"""Poll LMArena's boards into the ratings store (CLI).

    python3 scripts/poll_arena_into_store.py <costs.db>            # poll + show the diff
    python3 scripts/poll_arena_into_store.py <costs.db> --commit   # accept every row
    python3 scripts/poll_arena_into_store.py <costs.db> --release  # release-watch report only

The poll STAGES: nothing becomes the published baseline until it is accepted.
That is the same rule the UI follows (poll -> diff -> tick -> Update), so a
headless run and a click can never disagree about what is in force.

Needs DuckDB (declared in pyproject; present in the image). Run it inside the
container to be sure of the version:

    docker exec lcp-staging python3 /app/scripts/poll_arena_into_store.py \
        /app/data/costs.db --commit
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

sys.path.insert(0, ".")

from src.api import arena_poll, model_ratings  # noqa: E402


def _fmt_row(r) -> str:
    val = f"{r['value']:>10.4f}"
    old = "—" if r["old_value"] is None else f"{r['old_value']:>10.4f}"
    flag = " " if not r["changed"] else "*"
    return ("  %s %-22s %-9s %s -> %s  p%-5s %s"
            % (flag, r["model_slug"], r["unit"], old, val,
               r["percentile"], r.get("old_date") or "new"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db", help="path to costs.db")
    ap.add_argument("--commit", action="store_true",
                    help="accept every staged row as the new baseline")
    ap.add_argument("--release", action="store_true",
                    help="only run the release watcher (no poll, no writes)")
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    if args.release:
        boards = arena_poll.default_fetch()
        report = arena_poll.release_candidates(con, boards)
        print(json.dumps(report, indent=1) if args.json else _release_text(report))
        return 0

    result = model_ratings.stage_poll(con)
    print(f"polled {result['staged']} rows at {result['poll_id']}")
    for board, st in sorted(result["boards"].items()):
        print("  %-34s fetched=%-4s matched=%-3s rows=%-4s unmatched=%s"
              % (board, st["fetched"], st["matched"], st["rows"], st["unmatched"]))
    rows = result["rows"]
    changed = [r for r in rows if r["changed"]]
    print(f"\n{len(changed)} changed of {len(rows)} staged:")
    for r in changed[:60]:
        print(_fmt_row(r))
    rel = result.get("release") or {}
    if rel.get("candidates"):
        print(f"\nrelease candidates ({len(rel['candidates'])}; "
              f"{rel.get('newer_count', 0)} a newer release):")
        for c in rel["candidates"][:25]:
            print("  %-5s %-40s -> %s  (rank %s, %s)"
                  % ("NEWER" if c.get("newer") else "", c["arena_name"],
                     ", ".join(c["registered_same_line"]),
                     c["best_rank"], c["organization"]))

    if args.commit:
        committed = model_ratings.commit_staged(con)
        print("\ncommitted:", {k: v for k, v in committed.items()
                               if k in ("written", "skipped_unregistered",
                                        "rejected_namespace", "remaining")})
        for tax in ("code_generation", "debugging", "agentic_multi_step",
                    "research_deep"):
            lb = model_ratings.leaderboard(con, tax)
            print(f"\n{tax} ({len(lb)} rows):")
            for r in lb[:4]:
                print("   %-24s %10.4f %-16s p%-5s %s"
                      % (r["model_slug"], r["value"], r["unit"], r["percentile"],
                         r["lab"]))
    con.close()
    return 0


def _release_text(report: dict) -> str:
    out = [f"release watch — {report['registry_active']} active registered models, "
           f"{report['unresolved_names']} unresolved arena names"]
    out.append("\nadd candidates (unresolved name on a line we already carry):")
    for c in report["candidates"]:
        out.append("  %-40s -> %-28s rank %s  %s"
                   % (c["arena_name"], ", ".join(c["registered_same_line"]),
                      c["best_rank"], c["organization"]))
    if not report["candidates"]:
        out.append("  (none)")
    out.append("\nregistered models arena does not score at all:")
    for s in report["not_on_any_board"]:
        out.append(f"  {s}")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
