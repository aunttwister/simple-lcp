#!/usr/bin/env python3
"""Poll BOTH Arena board families into the ratings store.

Resolution order (fixes the gap found 2026-11-15): the alias table
`mi_model_names` is consulted FIRST, so operator-declared aliases win (e.g.
Qwen3.8-27B -> qwen3.8-27b-heretic); slug spelling is only the fallback. Effort
suffixes are stripped into a dimension (R2=C). Only provider-registered,
non-retired models are written; everything else is ignored.
"""
import json, re, sqlite3, sys, urllib.request
sys.path.insert(0, ".")
from src.api.model_ratings import upsert_ratings, leaderboard

API = "https://datasets-server.huggingface.co/rows"
DS = "lmarena-ai%2Fleaderboard-dataset"
TEXT = {"coding": ["code_generation", "debugging", "unit_tests"],
        "math": ["reasoning_chain"], "hard_prompts_english": ["reasoning_chain"],
        "expert": ["reasoning_chain"], "instruction_following": ["planning"],
        "multi_turn": ["casual_chat"], "creative_writing": ["casual_chat"]}
AGENT = {"agent": "agentic_multi_step", "agent_task_outcome_explicit": "agentic_multi_step",
         "agent_steerability": "planning", "agent_tool_hallucination": "research_deep",
         "agent_bash_reliability": "debugging", "agent_bash_recovery_steps": "debugging",
         "agent_praise_complaint": "casual_chat"}
EFFORT = re.compile(r"\s*[\(\[]\s*(x?high|max|low|medium|minimal)\s*[\)\]]\s*$", re.I)

def norm(s):
    s = re.sub(r"[\s_]+", "-", (s or "").strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-")

def rows(cfg, attempts=4):
    """The datasets-server 503s when hammered; back off rather than fail the poll."""
    import time
    u = f"{API}?dataset={DS}&config={cfg}&split=latest&offset=0&length=100"
    last = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(u, timeout=60) as r:
                return [x["row"] for x in json.load(r).get("rows", [])]
        except Exception as e:
            last = e
            time.sleep(2 ** i)
    print(f"  ! {cfg}: {last}")
    return []

db = sys.argv[1]
con = sqlite3.connect(db); con.row_factory = sqlite3.Row
# 1. alias table first (operator aliases win), then slug spelling
alias = {}
for r in con.execute("""SELECT n.name AS arena, m.slug AS slug FROM mi_model_names n
                        JOIN mi_models m ON m.model_id = n.model_id"""):
    alias[norm(r["arena"])] = r["slug"]
known = {norm(r[0]): r[0] for r in con.execute("SELECT slug FROM mi_models")}

def resolve(name):
    base = EFFORT.sub("", name); eff = None
    m = EFFORT.search(name)
    if m: eff = m.group(1).lower()
    k = norm(base)
    return alias.get(k) or known.get(k), eff

out = []
# ONE fetch per board, expanded across every axis that board informs. (Fetching
# inside the axis loop silently wrote only the first axis of a shared board.)
for cfg, axes in TEXT.items():
    rs = rows(cfg)
    for r in rs:
        slug, eff = resolve(r["model_name"] or "")
        if not slug: continue
        n = len(rs)
        pct = round(100*(1-(r["rank"]-1)/max(n-1,1)),1) if r.get("rank") else None
        for ax in axes:
            out.append(dict(model_slug=slug, taxonomy=ax, value=r["rating"], unit="elo",
                            rank=r["rank"], n=n, percentile=pct, source="arena", board=cfg,
                            lab=r.get("organization"), effort=eff, basis="arena-measured",
                            published=r.get("leaderboard_publish_date")))
for cfg, ax in AGENT.items():
    rs = rows(cfg)
    for r in rs:
        slug, eff = resolve(r["model_name"] or "")
        if not slug: continue
        n = len(rs)
        out.append(dict(model_slug=slug, taxonomy=ax, value=r["score"], unit="net_improvement",
                        rank=r["rank"], n=n,
                        percentile=round(100*(1-(r["rank"]-1)/max(n-1,1)),1) if r.get("rank") else None,
                        source="arena", board=cfg, lab=r.get("organization"), effort=eff,
                        basis="arena-measured", published=r.get("leaderboard_publish_date")))
print("polled rows:", len(out))
print("upsert:", upsert_ratings(con, out))
for t in ("code_generation", "debugging", "agentic_multi_step", "research_deep"):
    lb = leaderboard(con, t)
    print(f"\n{t} ({len(lb)} rows):")
    for r in lb[:4]:
        print("   %-24s %10.4f %-16s p%-5s %s" % (r["model_slug"], r["value"], r["unit"], r["percentile"], r["lab"]))
