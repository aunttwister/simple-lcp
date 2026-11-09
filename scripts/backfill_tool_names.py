#!/usr/bin/env python3
"""Backfill tool names on historical ``conv_messages`` rows.

Why: ``conv_store._prepare`` used to store only ``message["tool_name"]``. A
generic OpenAI-format ``tool`` result carries only a ``tool_call_id``, so those
rows landed with a NULL name and the UI fell back to ``(unnamed)``. New
captures now resolve the name from the assistant turn that called the tool
(``_tool_name_lookup`` in conv_store.py); this script repairs the rows already
on disk.

Two resolution passes, cheapest first:

1. **Caller map** — every ``tool_call_id`` named by an assistant turn's
   ``tool_calls[]``, built ONCE for the whole DB (not a query per row, which is
   what makes a multi-million-row table resolvable in one pass).
2. **Content prefix** — many captured tool rows were written as
   ``[tool_name] rest…``; where the id map has no entry, read the name back out
   of that prefix.

Fill-only and idempotent: it sets a name ONLY where the row is currently
unnamed AND one of the passes supplies one. It never overwrites an existing
name, and it is safe to re-run.

Usage:
    python3 scripts/backfill_tool_names.py [path/to/costs.db] [--apply]

Without ``--apply`` it reports what it WOULD change (dry run). Path defaults
to ``$LCP_DB_PATH`` or ``/app/data/costs.db``.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

# `[tool_name] …` — the name captured inline when the caller map has no entry.
_PREFIX_RE = re.compile(r"^\[([A-Za-z0-9_.\-]+)")


def _build_caller_map(con: sqlite3.Connection) -> dict[str, str]:
    """Map every tool_call_id referenced by an assistant turn to its name."""
    lookup: dict[str, str] = {}
    for (tc_json,) in con.execute(
        "SELECT tool_calls FROM conv_messages "
        "WHERE role = 'assistant' AND tool_calls IS NOT NULL"
    ):
        try:
            arr = json.loads(tc_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(arr, list):
            continue
        for tc in arr:
            if not isinstance(tc, dict):
                continue
            tc_id = tc.get("id")
            if not tc_id or tc_id in lookup:
                continue
            fn = tc.get("function") or {}
            name = (fn.get("name") if isinstance(fn, dict) else None) or tc.get("name")
            if name:
                lookup[tc_id] = str(name)
    return lookup


def backfill(db_path: str, apply: bool = False,
             batch: int = 50000,
             rollback_file: str | None = None) -> dict[str, int]:
    """Resolve unnamed tool rows in one batched pass; return a small report.

    The UPDATE runs in ``batch``-sized transactions so the write lock is held
    only briefly per chunk — on a live DB the gateway's own capture keeps
    writing, and one 1.9M-row transaction would stall it. In ``apply`` mode a
    rollback file listing the row ids that were unnamed (and so are the only
    rows the fill can touch) is written first, so the revert is exact.
    """
    con = sqlite3.connect(db_path)
    try:
        caller = _build_caller_map(con)
        # STREAM the unnamed rows — never ``fetchall()`` a multi-million-row
        # table. Loading 1.9M rows (with their content) into a list peaked at
        # ~4.2 GB and the OOM killer took the process inside a worker cgroup
        # (the loss was silent: Python flushes piped stdout only at exit). Iterate
        # the cursor and flush a batch at a time, so peak memory is the caller
        # map plus one batch.
        cur = con.execute(
            "SELECT id, tool_call_id, content FROM conv_messages "
            "WHERE role = 'tool' AND (tool_name IS NULL OR tool_name = '')"
        )
        scanned = resolvable = by_map = by_prefix = 0
        batch_buf: list[tuple[str, int]] = []
        rb_fh = open(rollback_file, "w") if (apply and rollback_file) else None
        try:
            for row_id, tool_call_id, content in cur:
                scanned += 1
                name = caller.get(tool_call_id) if tool_call_id else None
                if name:
                    by_map += 1
                elif content:
                    m = _PREFIX_RE.match(content)
                    if m:
                        name = m.group(1)
                        by_prefix += 1
                if not name:
                    continue
                resolvable += 1
                if rb_fh:
                    rb_fh.write(f"{row_id}\n")
                if apply:
                    batch_buf.append((name, row_id))
                    if len(batch_buf) >= batch:
                        con.executemany(
                            "UPDATE conv_messages SET tool_name = ? "
                            "WHERE id = ? AND (tool_name IS NULL OR tool_name = '')",
                            batch_buf,
                        )
                        con.commit()
                        batch_buf.clear()
            if apply and batch_buf:
                con.executemany(
                    "UPDATE conv_messages SET tool_name = ? "
                    "WHERE id = ? AND (tool_name IS NULL OR tool_name = '')",
                    batch_buf,
                )
                con.commit()
                batch_buf.clear()
        finally:
            if rb_fh:
                rb_fh.close()
        return {"scanned": scanned, "resolvable": resolvable,
                "by_map": by_map, "by_prefix": by_prefix,
                "applied": resolvable if apply else 0}
    finally:
        con.close()


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    db_path = args[0] if args else os.environ.get("LCP_DB_PATH", "/app/data/costs.db")
    rb = f"/tmp/backfill_tool_names_rollback_{os.getpid()}.txt" if apply else None
    report = backfill(db_path, apply=apply,
                      rollback_file=rb)
    mode = "APPLIED" if apply else "DRY RUN"
    print(f"[{mode}] {db_path}: scanned={report['scanned']} "
          f"resolvable={report['resolvable']} "
          f"(by_map={report['by_map']} by_prefix={report['by_prefix']}) "
          f"applied={report['applied']}")
    if rb and report["applied"]:
        print(f"rollback file: {rb} "
              f"({report['applied']} ids) — revert with "
              "'UPDATE conv_messages SET tool_name=NULL WHERE id IN (...)'")
    if not apply and report["resolvable"]:
        print("Re-run with --apply to write these names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
