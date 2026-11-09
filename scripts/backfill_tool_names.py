#!/usr/bin/env python3
"""Backfill tool names on historical ``conv_messages`` rows.

Why: ``conv_store._prepare`` used to store only ``message["tool_name"]``. A
generic OpenAI-format ``tool`` result carries only a ``tool_call_id``, so those
rows landed with a NULL name and the UI fell back to ``(unnamed)``. New
captures now resolve the name from the assistant turn that called the tool
(``_tool_name_lookup`` in conv_store.py); this script repairs the rows already
on disk.

Fill-only and idempotent: it sets a name ONLY where the row is currently
unnamed AND a matching ``tool_calls`` entry in the same conversation supplies
one. It never overwrites an existing name, and it is safe to re-run.

Usage:
    python3 scripts/backfill_tool_names.py [path/to/costs.db] [--apply]

Without ``--apply`` it reports what it WOULD change (dry run). Path defaults
to ``$LCP_DB_PATH`` or ``/app/data/costs.db``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys


def _name_for(tool_calls_json: str | None, tool_call_id: str | None) -> str | None:
    """Return the tool name a given tool_call_id maps to, or None."""
    if not tool_calls_json or not tool_call_id:
        return None
    try:
        arr = json.loads(tool_calls_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(arr, list):
        return None
    for tc in arr:
        if not isinstance(tc, dict) or tc.get("id") != tool_call_id:
            continue
        fn = tc.get("function") or {}
        name = (fn.get("name") if isinstance(fn, dict) else None) or tc.get("name")
        if name:
            return str(name)
    return None


def backfill(db_path: str, apply: bool = False) -> dict[str, int]:
    """Resolve unnamed tool rows from their caller; return a small report."""
    con = sqlite3.connect(db_path)
    try:
        unnamed = con.execute(
            "SELECT id, conversation_id, tool_call_id FROM conv_messages "
            "WHERE role = 'tool' AND (tool_name IS NULL OR tool_name = '')"
        ).fetchall()
        scanned = len(unnamed)
        updates: list[tuple[str, int]] = []
        for row_id, conv_id, tool_call_id in unnamed:
            calls = con.execute(
                "SELECT tool_calls FROM conv_messages "
                "WHERE conversation_id = ? AND role = 'assistant' "
                "AND tool_calls IS NOT NULL",
                (conv_id,),
            ).fetchall()
            name = None
            for (tc_json,) in calls:
                name = _name_for(tc_json, tool_call_id)
                if name:
                    break
            if name:
                updates.append((name, row_id))
        if apply and updates:
            con.executemany(
                "UPDATE conv_messages SET tool_name = ? "
                "WHERE id = ? AND (tool_name IS NULL OR tool_name = '')",
                updates,
            )
            con.commit()
        return {"scanned": scanned, "resolvable": len(updates),
                "applied": len(updates) if apply else 0}
    finally:
        con.close()


def main(argv: list[str]) -> int:
    apply = "--apply" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    db_path = args[0] if args else os.environ.get("LCP_DB_PATH", "/app/data/costs.db")
    report = backfill(db_path, apply=apply)
    mode = "APPLIED" if apply else "DRY RUN"
    print(f"[{mode}] {db_path}: scanned={report['scanned']} "
          f"resolvable={report['resolvable']} applied={report['applied']}")
    if not apply and report["resolvable"]:
        print("Re-run with --apply to write these names.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
