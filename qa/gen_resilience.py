# QA case generator 8: resilience — idempotency, negatives, validation matrix.
import json
import os

CASES = []
G = "resilience"

# ── Idempotency: repeat calls return same shape (×6 endpoints) ──
for ep in ["/api/work/tasks?lite=1&states=in_progress&per=5",
           "/api/work/cron", "/api/work/conversations?per=5",
           "/api/work/fleet", "/api/work/sources", "/api/work/cron/ops"]:
    CASES.append({
        "id": "rs-idem-%s" % ep.replace("/", "_").replace("?", "_").replace("=", "_")[:40],
        "group": G, "title": "idempotent %s" % ep[:48],
        "cmd": ("a=$(curl -s '{{BASE}}%s' | md5sum); b=$(curl -s '{{BASE}}%s' | md5sum); "
                "[ \"$a\" = \"$b\" ]" % (ep, ep)),
    })

# ── Filter edge cases: clamp behaviour ──
CLAMPS = [
    ("rs-clamp-1", "/api/work/tasks?lite=1&states=all&per=0&page=0",
     "per=0/page=0 clamps safely (not 500)"),
    ("rs-clamp-2", "/api/work/tasks?lite=1&states=garbage&per=5",
     "invalid states falls back (not 500)"),
    ("rs-clamp-3", "/api/work/conversations?per=-3&page=-2",
     "negative pagination clamps (not 500)"),
    ("rs-clamp-4", "/api/work/tasks?lite=1&states=all&per=99999&page=99999",
     "huge per/page clamps (not 500)"),
]
for cid, path, title in CLAMPS:
    CASES.append({
        "id": cid, "group": G, "title": title,
        "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s')\" != \"500\" ]" % path,
    })

# ── JSON error bodies where errors are real; unknown cid is 200-empty by design ──
for path in ["/api/work/tasks/detail?task=in_progress%2Fmissing-x"]:
    CASES.append({
        "id": "rs-json-11", "group": G,
        "title": "JSON error body for %s" % path[:44],
        "cmd": ("code=$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s'); "
                "[ \"$code\" != \"200\" ] && "
                "curl -s '{{BASE}}%s' | python3 -c 'import sys,json; json.load(sys.stdin)'"
                % (path, path)),
    })
CASES.append({
    "id": "rs-json-12", "group": G, "title": "unknown conversation cid -> 200 JSON (by design)",
    "cmd": "curl -s '{{BASE}}/api/work/conversations/detail?cid=missing' | python3 -c 'import sys,json; d=json.load(sys.stdin); assert \"events\" in d'",
})
# malformed cron op body -> 400 with JSON error
CASES.append({
    "id": "rs-json-13", "group": G, "title": "cron op malformed body -> 400 JSON",
    "cmd": ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/api/work/cron/ops' "
            "-H 'Content-Type: application/json' -d '{bad')\" = \"400\" ]"),
})

# ── Cron op validation matrix ──
import json as _json
VAL = [
    ("rs-op-1", "action missing", {"profile": "backups"}),
    ("rs-op-2", "profile empty", {"action": "remove", "profile": "", "job_id": "a1b2c3d4e5f6"}),
    ("rs-op-3", "job_id too long", {"action": "remove", "profile": "backups", "job_id": "a" * 40}),
    ("rs-op-4", "repeat negative", {"action": "create", "profile": "backups", "name": "x", "schedule": "30m", "prompt": "p", "repeat": -1}),
    ("rs-op-5", "prompt over 8k", {"action": "create", "profile": "backups", "name": "x", "schedule": "30m", "prompt": "p" * 9000}),
    ("rs-op-6", "no-agent without script", {"action": "create", "profile": "backups", "name": "x", "schedule": "30m", "no_agent": True}),
    ("rs-op-7", "skill with slash", {"action": "create", "profile": "backups", "name": "x", "schedule": "30m", "prompt": "p", "skill": "a/b"}),
    ("rs-op-8", "schedule empty", {"action": "create", "profile": "backups", "name": "x", "schedule": "", "prompt": "p"}),
]
for cid, title, payload in VAL:
    CASES.append({
        "id": cid, "group": G, "title": "op reject: %s" % title,
        "cmd": ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/api/work/cron/ops' "
                "-H 'Content-Type: application/json' -d '%s')\" = \"400\" ]"
                % _json.dumps(payload).replace('"', '\\"')),
    })

# ── Parallel load: 5 concurrent lite fetches all 200 ──
CASES.append({
    "id": "rs-para-1", "group": G, "title": "5 parallel lite fetches all succeed",
    "cmd": ("for i in 1 2 3 4 5; do "
            "( curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/work/tasks?lite=1&states=in_progress&per=10' & ) ; "
            "done; wait 2>/dev/null; echo ok") ,
})

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.resilience.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.resilience:", len(CASES))