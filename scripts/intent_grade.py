#!/usr/bin/env python3
"""Intent grading mechanism — score ANY decision layer against a reference label set.

Design: this is the SCORER only. Predictions are an input, so the same mechanism grades the
shipped centroid classifier, a trained head, or a future per-user taxonomy without knowing
anything about them. Stdlib only — no numpy, no sklearn — so it runs anywhere the repo runs.

Reference rows (JSONL), one per line:
    {"msg": "...", "gold": "<label>", "pred": "<label>", "conf": 0.83,
     "group": "<optional: reference-unanimity>", "provenance": "<optional>"}

`pred` may be absent/null to express ABSTAIN (the layer declined to decide) — coverage and
end-to-end accuracy then differ, which is the whole point of the CPU-only gate analysis.

Outputs a versioned JSON report and a markdown rendering, plus two self-checks that must hold
or the run is worthless:
  * NEGATIVE CONTROL — with the reference shuffled, accuracy must collapse to the majority-class
    rate. A grader that scores well on a shuffled reference is a rubber stamp.
  * MAJORITY BASELINE — the score every layer must beat to be worth anything.

Usage:
  python3 intent_grade.py --rows graded.jsonl --label act --name shipped-centroid \
      --out reports/act-shipped
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict


# ----------------------------------------------------------------- helpers
def load_rows(path):
    rows = []
    for i, line in enumerate(open(path, encoding="utf-8"), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception as exc:
            sys.exit("bad JSON on line %d of %s: %s" % (i, path, exc))
    return rows


def prf(gold, pred, labels):
    """Per-class precision/recall/F1/support + accuracy + macro-F1."""
    tp = Counter(); fp = Counter(); fn = Counter(); sup = Counter()
    for g, p in zip(gold, pred):
        sup[g] += 1
        if g == p:
            tp[g] += 1
        else:
            fp[p] += 1
            fn[g] += 1
    per = {}
    for lab in labels:
        t, P, f, s = tp[lab], tp[lab] + fp[lab], tp[lab] + fn[lab], sup[lab]
        prec = t / P if P else 0.0
        rec = t / f if f else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        per[lab] = {"precision": prec, "recall": rec, "f1": f1, "support": s}
    correct = sum(tp.values())
    n = len(gold)
    macro = sum(v["f1"] for v in per.values()) / len(labels) if labels else 0.0
    return {"accuracy": correct / n if n else 0.0, "correct": correct, "n": n,
            "macro_f1": macro, "per_class": per}


def confusion(gold, pred, labels):
    m = {g: Counter() for g in labels}
    for g, p in zip(gold, pred):
        m[g][p] += 1
    return {g: {p: m[g].get(p, 0) for p in labels} for g in labels}


def ece(pairs, bins=10):
    """Expected calibration error over (confidence, correct) pairs."""
    if not pairs:
        return None, []
    buckets = defaultdict(list)
    for conf, ok in pairs:
        buckets[min(int(conf * bins), bins - 1)].append((conf, ok))
    total = len(pairs)
    e = 0.0
    table = []
    for b in range(bins):
        items = buckets.get(b) or []
        if not items:
            continue
        acc = sum(1 for _, ok in items if ok) / len(items)
        conf = sum(c for c, _ in items) / len(items)
        e += (len(items) / total) * abs(acc - conf)
        table.append({"bin": "%d%%-%d%%" % (b * 10, (b + 1) * 10), "n": len(items),
                      "avg_conf": round(conf, 3), "accuracy": round(acc, 3),
                      "gap": round(acc - conf, 3)})
    return e, table


# ----------------------------------------------------------------- grading
def grade(rows, label, name, abstain_as_wrong=True):
    labels = sorted({r["gold"] for r in rows if r.get("gold")})
    graded = [r for r in rows if r.get("gold")]
    gold = [r["gold"] for r in graded]
    pred = [r.get("pred") if r.get("pred") in labels else None for r in graded]

    answered = [(g, p) for g, p in zip(gold, pred) if p is not None]
    cov = len(answered) / len(graded) if graded else 0.0
    # end-to-end: an abstain is a miss (this is the number the operator experiences)
    e2e = prf(gold, [p if p is not None else "__abstain__" for p in pred], labels)
    # on-answered: how good is the layer when it does decide
    onans = prf([g for g, _ in answered], [p for _, p in answered], labels) if answered else None

    # majority baseline
    maj = Counter(gold).most_common(1)[0] if gold else (None, 0)
    maj_acc = maj[1] / len(gold) if gold else 0.0

    # negative control: shuffle the reference, score again
    rng = random.Random(0)
    shuf = list(gold)
    rng.shuffle(shuf)
    ctrl = prf(shuf, [p if p is not None else "__abstain__" for p in pred], labels)

    conf_pairs = [(float(r["conf"]), r.get("pred") == r["gold"])
                  for r in graded if r.get("conf") is not None and r.get("pred") is not None]
    e, ece_table = ece(conf_pairs)

    # optional splits
    splits = {}
    for key in ("group", "provenance"):
        vals = sorted({r[key] for r in graded if r.get(key)})
        if not vals:
            continue
        splits[key] = {}
        for v in vals:
            sub = [r for r in graded if r.get(key) == v]
            s_pred = [r.get("pred") if r.get("pred") in labels else "__abstain__" for r in sub]
            ss = prf([r["gold"] for r in sub], s_pred, labels)
            splits[key][v] = {"n": ss["n"], "accuracy": ss["accuracy"], "macro_f1": ss["macro_f1"]}

    return {
        "name": name, "label_axis": label,
        "labels": labels, "n_rows": len(graded),
        "coverage": cov, "abstained": len(graded) - len(answered),
        "end_to_end": {"accuracy": e2e["accuracy"], "macro_f1": e2e["macro_f1"],
                       "correct": e2e["correct"]},
        "on_answered": ({"accuracy": onans["accuracy"], "macro_f1": onans["macro_f1"],
                         "n": onans["n"]} if onans else None),
        "majority_baseline": {"label": maj[0], "accuracy": maj_acc,
                              "n": maj[1], "share": maj_acc},
        "negative_control": {"accuracy": ctrl["accuracy"], "macro_f1": ctrl["macro_f1"],
                             "verdict": "PASS" if ctrl["accuracy"] <= maj_acc + 0.05 else "FAIL"},
        "per_class": e2e["per_class"],
        "confusion": confusion(gold, [p if p is not None else "__abstain__" for p in pred], labels),
        "calibration": {"ece": e, "bands": ece_table},
        "splits": splits,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def render_md(rep):
    L = []
    L.append("# Intent grade — %s (%s)\n" % (rep["name"], rep["label_axis"]))
    L.append("Rows **%d** · coverage **%.1f%%** (%d abstained) · generated %s\n"
             % (rep["n_rows"], 100 * rep["coverage"], rep["abstained"], rep["generated_at"]))
    m = rep["majority_baseline"]
    L.append("**End-to-end accuracy %.1f%% / macro-F1 %.1f** — majority-class baseline "
             "**%.1f%%** (`%s`).\n"
             % (100 * rep["end_to_end"]["accuracy"], 100 * rep["end_to_end"]["macro_f1"],
                100 * m["accuracy"], m["label"]))
    if rep["on_answered"]:
        L.append("On answered rows only: **%.1f%%** / macro-F1 %.1f over %d rows.\n"
                 % (100 * rep["on_answered"]["accuracy"],
                    100 * rep["on_answered"]["macro_f1"], rep["on_answered"]["n"]))
    nc = rep["negative_control"]
    L.append("## Self-checks\n")
    L.append("- **Negative control (shuffled reference): %.1f%% accuracy → %s.** "
             "Must sit at the majority baseline; a grader that scores on shuffled labels is "
             "broken, not lenient.\n"
             % (100 * nc["accuracy"], nc["verdict"]))
    if rep["calibration"]["ece"] is not None:
        L.append("- **Calibration ECE %.3f** (0 = perfectly honest confidence).\n"
                 % rep["calibration"]["ece"])
    L.append("\n## Per class\n")
    L.append("| label | precision | recall | F1 | support |\n|---|---|---|---|---|\n")
    for lab, v in rep["per_class"].items():
        L.append("| `%s` | %.3f | %.3f | %.3f | %d |\n"
                 % (lab, v["precision"], v["recall"], v["f1"], v["support"]))
    L.append("\n## Confusion (row = truth, col = predicted)\n\n")
    labs = rep["labels"]
    L.append("| truth \\ pred | " + " | ".join("`%s`" % x for x in labs) + " | abstain |\n")
    L.append("|" + "---|" * (len(labs) + 2) + "\n")
    for g in labs:
        cells = " | ".join(str(rep["confusion"][g].get(p, 0)) for p in labs)
        L.append("| **%s** | %s | %d |\n" % (g, cells, rep["confusion"][g].get("__abstain__", 0)))
    if rep["calibration"]["bands"]:
        L.append("\n## Confidence bands\n\n| band | n | avg conf | accuracy | gap |\n|---|---|---|---|---|\n")
        for b in rep["calibration"]["bands"]:
            L.append("| %s | %d | %.3f | %.3f | %+.3f |\n"
                     % (b["bin"], b["n"], b["avg_conf"], b["accuracy"], b["gap"]))
    for key, vals in rep["splits"].items():
        L.append("\n## Split by %s\n\n| %s | n | accuracy | macro-F1 |\n|---|---|---|---|\n"
                 % (key, key))
        for v, s in vals.items():
            L.append("| `%s` | %d | %.1f%% | %.1f |\n"
                     % (v, s["n"], 100 * s["accuracy"], 100 * s["macro_f1"]))
    return "".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--label", default="act")
    ap.add_argument("--name", default="candidate")
    ap.add_argument("--out", required=True, help="path prefix; .json and .md are written")
    args = ap.parse_args()

    rows = load_rows(args.rows)
    rep = grade(rows, args.label, args.name)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out + ".json", "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2, sort_keys=True)
    with open(args.out + ".md", "w", encoding="utf-8") as fh:
        fh.write(render_md(rep))

    print("== %s (%s)" % (rep["name"], rep["label_axis"]))
    print("   rows %d  coverage %.1f%%  abstained %d"
          % (rep["n_rows"], 100 * rep["coverage"], rep["abstained"]))
    print("   END-TO-END accuracy %.1f%%  macro-F1 %.1f"
          % (100 * rep["end_to_end"]["accuracy"], 100 * rep["end_to_end"]["macro_f1"]))
    if rep["on_answered"]:
        print("   on answered      accuracy %.1f%%  (n=%d)"
              % (100 * rep["on_answered"]["accuracy"], rep["on_answered"]["n"]))
    print("   majority baseline %.1f%% (%s)" % (100 * rep["majority_baseline"]["accuracy"],
                                                rep["majority_baseline"]["label"]))
    print("   NEGATIVE CONTROL  %.1f%%  -> %s"
          % (100 * rep["negative_control"]["accuracy"], rep["negative_control"]["verdict"]))
    if rep["calibration"]["ece"] is not None:
        print("   calibration ECE   %.3f" % rep["calibration"]["ece"])
    print("   wrote %s.json / .md" % args.out)
    if rep["negative_control"]["verdict"] == "FAIL":
        print("\n!! the grader did not collapse on a shuffled reference - treat this run as "
              "INVALID and fix the grader before trusting any number above")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
