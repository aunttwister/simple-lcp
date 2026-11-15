#!/usr/bin/env python3
"""Resolve Arena names to our registry under the operator's rules (2026-11-15).

  R1  spelling only            -> normalise (case, punctuation) and match exactly.
  R2=C effort is a DIMENSION   -> "(High)"/"(Max)"/"(xHigh)"/"(Low)"/"(Medium)" is
                                  parsed OFF the name into an `effort` attribute,
                                  and the row attaches to the base model. One
                                  registry entry per model, effort belongs to the
                                  rating row (the ratings store carries the column;
                                  until it exists the value rides in the audit file).
  R3  distinct model           -> gets its OWN registry entry. GLM-5.3 (Max) is NOT
                                  glm-5.3-flash; Qwen3.8-Max is not Flash-Next.

Writes an audit file (every decision, so it can be reviewed and unit-tested) and,
with --apply, creates the missing registry entries + aliases on the target DB.
"""
import argparse
import json
import re
import shutil
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone

API = "https://datasets-server.huggingface.co/rows"
DS = "lmarena-ai%2Fleaderboard-dataset"
BOARDS = ["text_style_control", "agent", "agent_task_outcome_explicit",
          "agent_steerability", "agent_tool_hallucination",
          "agent_bash_reliability", "agent_bash_recovery_steps",
          "agent_praise_complaint"]

# effort suffix -> kept as a dimension, stripped from the model name (R2=C)
EFFORT = re.compile(r"\s*[\(\[]\s*(x?high|max|low|medium|minimal|non-?thinking|thinking)\s*[\)\]]\s*$", re.I)
# generation/date suffixes that are part of the model identity, NOT effort
IDENT = re.compile(r"\s*[\(\[]\s*(20\d{6}|\d{4})\s*[\)\]]\s*$")

ORG_TO_FAMILY = {
    "zai": "glm", "alibaba": "qwen", "moonshot": "moonshot", "minimax": "minimax",
    "xai": "xai", "google": "google", "deepseek": "deepseek", "openai": "openai",
    "anthropic": "anthropic", "xiaomi": "xiaomi", "meituan": "meituan",
    "abacus": "abacus", "stepfun": "stepfun", "ant-group": "ant-group",
    "meta": "meta", "mistral": "mistral", "nvidia": "nvidia",
}


def norm(s: str) -> str:
    """R1: lowercase, collapse spaces/separators. 'Qwen 3.8 27B' -> 'qwen3.8-27b'."""
    s = (s or "").strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[()\[\]]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9.\-]", "", s)
    return re.sub(r"-{2,}", "-", s).strip("-")


def fetch(board):
    url = f"{API}?dataset={DS}&config={board}&split=latest&offset=0&length=100"
    with urllib.request.urlopen(url, timeout=60) as r:
        return [(x["row"], board) for x in json.load(r).get("rows", [])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default="data/arena_name_resolution.json")
    a = ap.parse_args()

    seen = {}
    for b in BOARDS:
        try:
            for row, board in fetch(b):
                n = row.get("model_name") or ""
                if not n:
                    continue
                seen.setdefault(n, {"org": row.get("organization"), "boards": set()})
                seen[n]["boards"].add(board)
        except Exception as e:                                  # noqa: BLE001
            print(f"  ! {b}: {str(e)[:40]}")

    known = {}
    if a.db:
        c = sqlite3.connect(a.db)
        for slug, in c.execute("SELECT slug FROM mi_models"):
            known[norm(slug)] = slug

    audit, new_entries = [], {}
    for name, meta in sorted(seen.items()):
        base = name
        eff = None
        m = EFFORT.search(base)
        if m:
            eff = m.group(1).lower()
            base = EFFORT.sub("", base)
        if IDENT.search(base) and not eff:          # a dated revision is its own model
            pass
        key = norm(base)
        slug = known.get(key)
        if slug:
            decision = "matched-existing"
        else:
            # R3: its own entry (variant or brand-new model)
            slug = slugify(base)
            decision = "new-registry-entry"
            new_entries[slug] = {
                "display_name": base.strip(),
                "family": ORG_TO_FAMILY.get((meta["org"] or "").lower(), (meta["org"] or "").lower() or None),
                "arena_names": [],
            }
        if eff:
            new_entries.setdefault(slug, {}).setdefault("arena_names", [])
        if slug in new_entries:
            new_entries[slug]["arena_names"].append(name)
        audit.append(dict(arena_name=name, base=base, effort=eff, model_slug=slug,
                          decision=decision, lab=meta["org"], boards=sorted(meta["boards"])))

    json.dump(audit, open(a.out, "w"), indent=1)
    matched = sum(1 for r in audit if r["decision"] == "matched-existing")
    print(f"arena names analysed: {len(audit)}  matched-exact: {matched}  "
          f"new entries: {len(new_entries)}")
    print(f"audit -> {a.out}")

    if not a.apply or not a.db:
        print("(dry run — pass --apply --db <path> to write)")
        return

    bk = f"/your/data/app/lcp-staging/db-backups/costs.db.pre-arena-registration-{time.strftime('%Y%m%dT%H%M%SZ')}"
    shutil.copy(a.db, bk)
    print("backup:", bk)
    now = datetime.now(timezone.utc).isoformat()
    c = sqlite3.connect(a.db)
    ins = 0
    for slug, meta in sorted(new_entries.items()):
        if c.execute("SELECT 1 FROM mi_models WHERE slug=?", (slug,)).fetchone():
            continue
        c.execute("""INSERT INTO mi_models (slug, display_name, family, status, created_at)
                     VALUES (?,?,?,'active',?)""", (slug, meta["display_name"], meta["family"], now))
        mid = c.execute("SELECT model_id FROM mi_models WHERE slug=?", (slug,)).fetchone()[0]
        for nm in meta["arena_names"]:
            c.execute("""INSERT OR IGNORE INTO mi_model_names
                         (model_id, source_id, name, is_primary, confidence, evidence,
                          valid_from, first_seen, last_seen, verified_at, note)
                         VALUES (?,3,?,0,'arena-exact','Arena name -> own registry entry per R3 '
                                 'and effort stripped per R2=C (2026-11-15)',?,?,?,?,?)""",
                      (mid, nm, now, now, now, now, "operator rule R3"))
        ins += 1
    c.commit()
    print(f"registered {ins} new model entries (+ their arena-name aliases)")


if __name__ == "__main__":
    main()
