#!/usr/bin/env python3
"""Does the bge-small classifier discriminate on REAL session text?

This is the check Laya failed: on its own clean preset it scored 6/6, but on
raw real sessions it returned 'domain: code' 16 of 16 times -- a constant. A
classifier that returns one label for everything is not a classifier, and a
Categories view built on it would show a single bar.

So: sample real first-user-messages from real profiles, classify them, and look
at the DISTRIBUTION. If one label dominates ~everything, the view is a lie.
"""
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "/app")
PROFILES = {
    "l2": "/root/.hermes/profiles/homelab-expert-l2/state.db",
    "l1": "/root/.hermes/profiles/homelab-expert-l1/state.db",
    "blog-writer": "/root/.hermes/profiles/blog-writer/state.db",
    "career": "/root/.hermes/profiles/hermes-career-advisor/state.db",
    "naptune-admin": "/root/.hermes/profiles/naptune-admin/state.db",
}

from src.api.task_classifier import get_semantic_classifier

clf = get_semantic_classifier()
print("classifier ready:", clf is not None, "min_score:", getattr(clf, "min_score", None))
print()

samples = []
for prof, db in PROFILES.items():
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        con.row_factory = sqlite3.Row
        has = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
        if not {"content", "role"} <= has:
            print("  %-14s no messages(content,role)" % prof)
            continue
        # First user message of each session = the session's stated intent.
        q = ("SELECT m.content FROM messages m WHERE m.role='user' "
             "AND m.content IS NOT NULL AND length(m.content) BETWEEN 40 AND 700 "
             "GROUP BY m.session_id ORDER BY m.id DESC LIMIT 40")
        rows = [r[0] for r in con.execute(q)]
        for t in rows:
            samples.append((prof, t.replace("\n", " ").strip()))
        print("  %-14s %d samples" % (prof, len(rows)))
        con.close()
    except Exception as e:
        print("  %-14s ERR %s: %s" % (prof, type(e).__name__, e))

print("\ntotal samples: %d" % len(samples))
if not samples:
    raise SystemExit("no samples")

print("\n" + "=" * 72)
print("label distribution on REAL session text")
print("=" * 72)
dist = Counter()
examples = {}
for prof, t in samples:
    lab = clf.classify(t)
    dist[lab] += 1
    examples.setdefault(lab, (prof, t[:96]))
    examples.setdefault(lab, (prof, t[:96]))

total = sum(dist.values())
for lab, n in dist.most_common():
    pct = 100.0 * n / total
    prof, ex = examples[lab]
    print("  %-22s %4d  %5.1f%%   [%s] %s" % (lab, n, pct, prof, ex))

top = dist.most_common(1)[0]
print("\n  dominant label: %s at %.1f%%" % (top[0], 100.0 * top[1] / total))
print("  distinct labels used: %d of 8" % len(dist))
verdict = "DISCRIMINATES" if top[1] / total < 0.6 else "COLLAPSED (constant-like)"
print("  verdict: %s" % verdict)
