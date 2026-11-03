#!/usr/bin/env python3
"""ASSERT THE CLASS SPACE of a corpus against the pinned taxonomy.

Why this exists
---------------
The 2026-11-02/03 corpus lineage drifted silently:

  reference_final2  ->  3 act classes   (build_corpus2.py applied the ACT3 map)
  reference_final4  ->  4 act classes   (+'personal', unmapped in ACT3 -> survived)
  reference_final6  -> 15 act classes   (build_additive6.py filled raw FINE labels)

train_mt5.py reads `consensus_act` RAW and derives its class list from whatever is in
the file, so an unbucketed corpus silently becomes a different classification problem.
Every accuracy figure in between is therefore denominated in a different label space
and is NOT comparable. Class count is itself an accuracy lever, so this is not a
cosmetic difference.

This script makes the class space explicit and refuses when it is not the pinned one.
Exit code 0 = the space matches. Exit 1 = mismatch (do not quote numbers from it).
"""
import collections
import json
import os
import sys

TAX = os.environ.get("TAXONOMY_BUCKETS", "/app/data/intent/taxonomy-buckets.json")


def load_pinned(path=TAX):
    with open(path, encoding="utf-8") as fh:
        t = json.load(fh)
    return (set(t["axes"]["act_bucket"]["labels"]),
            set(t["axes"]["subject_group"]["labels"]))


def class_space(corpus):
    acts, subs = collections.Counter(), collections.Counter()
    rows = 0
    for line in open(corpus, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        rows += 1
        if r.get("consensus_act"):
            acts[r["consensus_act"]] += 1
        if r.get("consensus_subject"):
            subs[r["consensus_subject"]] += 1
    return rows, acts, subs


def main():
    corpus = sys.argv[1]
    expect_acts, expect_subs = load_pinned()
    rows, acts, subs = class_space(corpus)
    got_acts, got_subs = set(acts), set(subs)

    print("corpus      : %s" % corpus)
    print("rows        : %d" % rows)
    print("act classes : %d %s" % (len(got_acts), dict(acts.most_common(6))))
    print("sub classes : %d %s" % (len(got_subs), dict(subs.most_common(6))))

    ok = True
    extra_a, missing_a = got_acts - expect_acts, expect_acts - got_acts
    extra_s, missing_s = got_subs - expect_subs, expect_subs - got_subs
    if extra_a:
        print("FAIL act: %d label(s) OUTSIDE the pinned bucket space: %s"
              % (len(extra_a), sorted(extra_a)))
        ok = False
    if missing_a:
        print("warn act: pinned label(s) absent from this corpus: %s" % sorted(missing_a))
    if extra_s:
        print("FAIL subject: %d label(s) OUTSIDE the pinned group space: %s"
              % (len(extra_s), sorted(extra_s)))
        ok = False
    if missing_s:
        print("warn subject: pinned label(s) absent: %s" % sorted(missing_s))

    if ok:
        print("CLASS SPACE OK - %d act / %d subject classes, matches the pinned taxonomy"
              % (len(got_acts), len(got_subs)))
        return 0
    print("CLASS SPACE MISMATCH - any accuracy measured on this corpus is NOT comparable "
          "to a run on a different class space")
    return 1


if __name__ == "__main__":
    sys.exit(main())
