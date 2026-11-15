#!/usr/bin/env python3
"""Proposal: Elo for our taxonomy, derived from what we actually have.

Operator direction (2026-11-15): "give up on accuracy ... make a sensible
approximation of what elo each model deserves (based on livebench rating and
lmarena rating on sensible categories (e.g. coding for debugging,
code_generation, etc.)) and then show me the proposal of elos for our custom
taxonomies; we can save that csv and use it as unit tests."

Method, in order of authority per (model, axis) cell:

  1. ARENA-MEASURED  — LMArena Elo for the axis, where an arena category maps to
     it (code_generation, reasoning_chain, planning, casual_chat). Real
     preference data, so it wins where it exists.
  2. DERIVED-FROM-BENCHMARK — for axes with no arena category, map the model's
     benchmark score onto the Elo scale using a calibration fitted on the
     OVERLAP set (models that have BOTH a livebench accuracy and an arena Elo),
     so the mapped number is anchored to arena's own scale rather than invented.
  3. OPERATOR-INPUT — axes where neither exists. Reported as such, never faked.

Arena category -> axis mapping follows the operator's example ("coding for
debugging, code_generation"):
    coding                -> code_generation, debugging, unit_tests
    math, hard_prompts    -> reasoning_chain
    instruction_following -> planning
    multi_turn, creative_writing -> casual_chat
    (agentic_multi_step, research_deep have no arena text category at all)
"""
import csv
import sqlite3
import statistics as st

DB = "/tmp/poll/costs.db"
OUT = "/tmp/poll/proposed_elo.csv"

ARENA_TO_AXES = {
    "coding": ["code_generation", "debugging", "unit_tests"],
    "math": ["reasoning_chain"],
    "hard_prompts_english": ["reasoning_chain"],
    "expert": ["reasoning_chain"],
    "instruction_following": ["planning"],
    "multi_turn": ["casual_chat"],
    "creative_writing": ["casual_chat"],
}

AXES = ["code_generation", "debugging", "unit_tests", "reasoning_chain",
        "planning", "casual_chat", "agentic_multi_step", "research_deep"]

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

models = {r["slug"]: r for r in c.execute(
    "SELECT slug, display_name, family, status FROM mi_models WHERE status='active'")}

# --- gather every signal per (model, axis) -------------------------------------
arena = {}      # (model, axis) -> elo from the arena board, via task_type
bench = {}      # (model, axis) -> benchmark score 0..1 (livebench / manual)
for r in c.execute("""SELECT model, task_type, score, raw_score, source, release_label,
                             benchmark_category
                      FROM model_capabilities"""):
    key = (r["model"], r["task_type"])
    if r["source"] == "lmarena":
        # One arena category can inform SEVERAL of our axes (operator's rule:
        # "coding for debugging, code_generation"). The ingester writes only the
        # single primary axis (coding -> code_generation), so expand through the
        # board the row actually came from, otherwise debugging/unit_tests end up
        # with almost no coverage while the data to fill them is already here.
        cat = r["benchmark_category"] or ""
        for axis in ARENA_TO_AXES.get(cat, [r["task_type"]]):
            arena[(r["model"], axis)] = dict(elo=r["raw_score"], cat=cat,
                                             date=r["release_label"])
    else:
        bench[key] = dict(score=r["score"], source=r["source"],
                          date=r["release_label"])

print("signals available")
print("  arena cells :", len(arena), "over axes", sorted({a for _, a in arena}))
print("  bench cells :", len(bench), "over axes", sorted({a for _, a in bench}))

# --- calibrate benchmark -> Elo on the overlap ---------------------------------
# Only pairs where the SAME model+axis has both a benchmark score and an arena
# Elo are usable; anything else would be fitting on one side only.
pairs = [(bench[k]["score"] * 100.0, arena[k]["elo"])
         for k in arena.keys() & bench.keys()]
xs = [p[0] for p in pairs]
ys = [p[1] for p in pairs]
print("\ncalibration set (model+axis with both):", len(pairs))
for k in sorted(arena.keys() & bench.keys()):
    print(f"   {k[0][:22]:22s} {k[1]:18s} bench {bench[k]['score']*100:6.1f} -> arena {arena[k]['elo']:.1f}")
if len(pairs) >= 3:
    mx, my = st.mean(xs), st.mean(ys)
    b = sum((x - mx) * (y - my) for x, y in pairs) / sum((x - mx) ** 2 for x in xs)
    a = my - b * mx
    resid = [abs(y - (a + b * x)) for x, y in pairs]
    print(f"   fit: elo = {a:.1f} + {b:.2f} x bench100   (r-ish residual median {st.median(resid):.1f})")
else:
    b, a = 6.0, 1000.0
    print("   too few pairs — falling back to a declared linear map elo = 1000 + 6 x bench%")

# --- emit the proposal --------------------------------------------------------
rows = []
for slug in sorted(models):
    for axis in AXES:
        k = (slug, axis)
        if k in arena:
            elo, basis = arena[k]["elo"], "arena-measured"
            note = f"arena {arena[k]['cat']} {arena[k]['date']}"
        elif k in bench:
            elo = a + b * (bench[k]["score"] * 100.0)
            basis = "derived"
            note = f"from {bench[k]['source']} {bench[k]['score']:.3f} ({bench[k]['date']})"
        else:
            elo, basis, note = None, "operator-input", "no source for this axis"
        rows.append(dict(model_slug=slug, display_name=models[slug]["display_name"],
                         lab=models[slug]["family"], taxonomy=axis,
                         elo=None if elo is None else round(elo, 1), basis=basis, note=note))

# percentile inside each taxonomy, over the models that have a value there
for axis in AXES:
    vals = sorted([r for r in rows if r["taxonomy"] == axis and r["elo"] is not None],
                  key=lambda r: -r["elo"])
    n = len(vals)
    for i, r in enumerate(vals):
        r["percentile"] = round(100.0 * (1 - i / max(n - 1, 1)), 1) if n > 1 else 100.0
for r in rows:
    r.setdefault("percentile", None)

with open(OUT, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["taxonomy", "model_slug", "display_name", "lab",
                                       "elo", "percentile", "basis", "note"])
    w.writeheader()
    for r in sorted(rows, key=lambda r: (r["taxonomy"], -(r["elo"] or 0))):
        w.writerow(r)

print(f"\nwrote {OUT} ({len(rows)} rows)")
print("\n=== PROPOSAL (top 4 per taxonomy) ===")
for axis in AXES:
    rs = [r for r in rows if r["taxonomy"] == axis]
    have = [r for r in rs if r["elo"] is not None]
    print(f"\n{axis}  ({len(have)}/{len(rs)} models have a value; basis: "
          f"{sorted({r['basis'] for r in have}) or 'none'})")
    for r in sorted(have, key=lambda r: -r["elo"])[:4]:
        print(f"   {r['display_name'][:26]:26s} {r['elo']:7.1f}  p{r['percentile']:5.1f}  {r['lab']:9s} [{r['basis']}]")
    if not have:
        print("   — no arena category and no benchmark signal: needs your input")
