#!/usr/bin/env python3
"""Poll Arena's AGENT boards and map them onto the gateway's axes.

Why this exists: the poll only read the *text* board family, so every agentic
axis (agentic_multi_step, debugging, research_deep, and part of planning) looked
like it had no Arena signal. It does — on the `agent*` board configs, which the
operator proved with a screenshot of Agent Arena | Capabilities (2026-11-15).
See references/lmarena-source-map.md.

Units: agent boards publish a NET IMPROVEMENT (small signed value) plus a rank
and a session_count — not Elo. Value and unit are labelled so the two families
are never ranked against each other in one list.

Name resolution is an EXPLICIT alias table. Variant suffixes ("(Max)", dated
suffixes) are NOT resolved here: a variant is a different row, and guessing is
how capability data gets fabricated. Unresolved names are printed for the
operator to confirm.
"""
import csv
import json
import urllib.request
from datetime import datetime, timezone

API = "https://datasets-server.huggingface.co/rows"
DS = "lmarena-ai%2Fleaderboard-dataset"
OUT = "/tmp/poll/arena_agent_leaderboard.csv"

# agent board -> our taxonomy axis. Boards are dimensioned by what they measure.
BOARD_TO_AXIS = {
    "agent": "agentic_multi_step",
    "agent_task_outcome_explicit": "agentic_multi_step",
    "agent_steerability": "planning",
    "agent_tool_hallucination": "research_deep",
    "agent_bash_reliability": "debugging",
    "agent_bash_recovery_steps": "debugging",
    "agent_praise_complaint": "casual_chat",
}

# EXACT alias table: arena agent-board spelling -> our registry slug.
# Base spellings only. "(Max)"/dated variants are deliberately absent.
ALIASES = {
    "qwen3.8 flash next": "qwen3.8-flash-next",
    "qwen3.8-flash-next": "qwen3.8-flash-next",
    "qwen3.8 max": "qwen3.8-max",
    "qwen3.8-max": "qwen3.8-max",
    "qwen3.8-27b": "qwen3.8-27b-heretic",          # operator-declared 2026-11-15
    "glm-5.3-flash": "glm-5.3-flash",
    "minimax m3": "minimax-m3",
    "minimax-m3": "minimax-m3",
    "kimi k3": "kimi-k3",
    "gemini 3.6 flash": "gemini-3.6-flash",
    "grok 4.5": "grok-4.5",
    "gpt-5.6 sol": "gpt-5.6-sol",
    "claude opus 5": "claude-opus-5",
    "claude sonnet 5": "claude-sonnet-5",
    "claude fable 5": "claude-fable-5",
    "deepseek-v4-pro": "deepseek-v4-pro",
}


def rows_for(board):
    url = f"{API}?dataset={DS}&config={board}&split=latest&offset=0&length=100"
    with urllib.request.urlopen(url, timeout=60) as r:
        d = json.load(r)
    return [x["row"] for x in d.get("rows", [])]


def norm(s):
    return (s or "").strip().lower()


found, unresolved = [], {}
for board, axis in BOARD_TO_AXIS.items():
    try:
        rs = rows_for(board)
    except Exception as e:                                   # noqa: BLE001
        print(f"  ! {board}: fetch failed ({str(e)[:50]})")
        continue
    print(f"  {board:32s} rows={len(rs):3d} -> {axis}")
    for r in rs:
        name = r.get("model_name") or ""
        slug = ALIASES.get(norm(name))
        if not slug:
            if any(k in name.lower() for k in ("glm", "qwen", "minimax", "kimi", "deepseek", "grok", "gemini", "claude", "gpt-")):
                unresolved.setdefault(name, board)
            continue
        found.append(dict(board=board, taxonomy=axis, model_slug=slug,
                          arena_name=name, lab=r.get("organization"),
                          value=r.get("score"), rank=r.get("rank"),
                          sessions=r.get("session_count"),
                          n=len(rs), unit="net_improvement",
                          published=r.get("leaderboard_publish_date")))

# percentile from rank within the board (higher rank number = lower standing)
for r in found:
    r["percentile"] = round(100.0 * (1 - (r["rank"] - 1) / max(r["n"] - 1, 1)), 1) if r["rank"] else None

with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["taxonomy", "model_slug", "arena_name", "lab", "value",
                                       "unit", "percentile", "rank", "sessions", "board", "published"])
    w.writeheader()
    for r in sorted(found, key=lambda r: (r["taxonomy"], r["rank"] or 99)):
        w.writerow({k: r[k] for k in w.fieldnames})

print(f"\nresolved rows: {len(found)}  -> {OUT}")
print("\n=== our models on the AGENT boards (value = net improvement, NOT Elo) ===")
axis = None
for r in sorted(found, key=lambda r: (r["taxonomy"], r["rank"] or 99)):
    if r["taxonomy"] != axis:
        axis = r["taxonomy"]; print(f"\n  {axis}")
    print(f"     {r['model_slug']:22s} {r['value']:+.4f}  rank {r['rank']:>3}/{r['n']}  p{r['percentile']:>5}  {r['lab']:9s} [{r['board']}] sessions={r['sessions']}")

print("\n=== NOT resolved — variant/suffixed names awaiting your confirmation ===")
for n, b in sorted(unresolved.items()):
    print(f"     {n:38s} [{b}]")
