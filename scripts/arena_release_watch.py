#!/usr/bin/env python3
"""Release watcher: is our registry behind the leaderboard?

The operator's question, verbatim: *"I also dont want us to keep sonnet 5, claude 5
and etc, let's have the latest releases of those models — can we track that
somehow?"* This is the tracker. It is a REPORT and nothing else: registering a
model needs its provider model id, which a leaderboard does not carry, so a
candidate is surfaced for review and never auto-registered or auto-retired.

What it answers, per run:

  * **add candidates** — an arena name that resolves to nothing, whose LINE
    matches a registered model's line. 'Claude Opus 5.5 (High)' next to our
    'claude-opus-5' is the newer release of a line we carry.
  * **not on any board** — a registered model no board scores at all (our own
    local builds live here; their number can only come from a custom board).
  * **unmatched volume** — names arena carries that we ignore, so a real gap can
    never hide behind a quiet report.

    docker exec lcp-staging python3 /app/scripts/arena_release_watch.py \
        --db /app/data/costs.db --out /app/data/reports/arena-release-watch.md
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from src.api import arena_poll  # noqa: E402


def build_report(db: str) -> tuple[str, dict]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        boards = arena_poll.default_fetch()
        report = arena_poll.release_candidates(con, boards)
        per_board = {b: len(rows) for b, rows in sorted(boards.items())}
        registry = [dict(r) for r in con.execute(
            "SELECT slug, family, status FROM mi_models ORDER BY slug")]
        stored = [dict(r) for r in con.execute(
            "SELECT taxonomy, unit, count(*) AS rows FROM model_ratings"
            " GROUP BY taxonomy, unit ORDER BY taxonomy, unit")]
    finally:
        con.close()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = [f"# Arena release watch — {now}", ""]
    L.append(f"- boards read: {len(boards)} ({', '.join('%s=%d' % (b, n) for b, n in per_board.items())})")
    L.append(f"- registry: {report['registry_active']} active models")
    L.append(f"- arena names we do not resolve: {report['unresolved_names']}")
    L.append("")

    L.append(f"## Add candidates ({len(report['candidates'])}, "
             f"{report.get('newer_count', 0)} a newer release)")
    L.append("")
    if report["candidates"]:
        L.append("| arena name | line already carried | newer? | arena rank | lab | boards |")
        L.append("|---|---|---|---|---|---|")
        for c in report["candidates"]:
            newer = "**yes**" if c.get("newer") else ("—" if c.get("newer") is None else "no")
            L.append("| %s | %s | %s | %s | %s | %s |"
                     % (c["arena_name"], ", ".join(c["registered_same_line"]), newer,
                        c["best_rank"] or "—", c["organization"] or "—",
                        ", ".join(c["boards"])))
    else:
        L.append("_none — every name arena carries either resolves or belongs to a "
                 "line we do not carry._")
    L.append("")

    L.append("## Registered models no board scores ({})".format(
        len(report["not_on_any_board"])))
    L.append("")
    if report["not_on_any_board"]:
        L.append(", ".join(report["not_on_any_board"]))
    else:
        L.append("_none_")
    L.append("")

    L.append("## Registry")
    L.append("")
    L.append("| slug | family | status |")
    L.append("|---|---|---|")
    for m in registry:
        L.append("| %s | %s | %s |" % (m["slug"], m["family"] or "—", m["status"]))
    L.append("")

    L.append("## Stored ratings")
    L.append("")
    if stored:
        L.append("| taxonomy | unit | rows |")
        L.append("|---|---|---|")
        for s in stored:
            L.append("| %s | %s | %s |" % (s["taxonomy"], s["unit"], s["rows"]))
    else:
        L.append("_no ratings stored yet_")
    L.append("")
    L.append("---")
    L.append("")
    L.append("A candidate is a name similarity on the LINE, not a measurement: "
             "registering one needs its provider model id, and retiring the old "
             "release needs the new one registered first. Nothing here writes the "
             "registry.")

    return "\n".join(L) + "\n", report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/costs.db")
    ap.add_argument("--out", help="write the markdown report here")
    ap.add_argument("--summary", action="store_true",
                    help="print only the headline counts (for a cron log)")
    args = ap.parse_args()

    text, report = build_report(args.db)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    if args.summary:
        print(f"arena release watch: {report['unresolved_names']} unresolved names, "
              f"{len(report['candidates'])} add candidates, "
              f"{len(report['not_on_any_board'])} models on no board"
              + (f" -> {args.out}" if args.out else ""))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
