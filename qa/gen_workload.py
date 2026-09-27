# QA case generator 7: workload — per-task detail sweeps + pagination matrices.
import json
import os
import urllib.request

CASES = []
G = "workload"
BASE = "http://192.168.1.198:8735"


def fetch(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read())

# ── Dynamic: detail every task on page 1 of states=all (real lazy-expand load) ──
try:
    first = fetch("/api/work/tasks?lite=1&states=all&per=15")
    for i, t in enumerate(first.get("tasks", [])[:15]):
        CASES.append({
            "id": "wl-detail-%02d" % (i + 1), "group": G,
            "title": "detail %s" % t["key"],
            "cmd": ("curl -s '{{BASE}}/api/work/tasks/detail?task=%s' | python3 -c "
                    "\"import sys,json; d=json.load(sys.stdin); assert d.get('subject')\""
                    % urllib.parse.quote(t["key"], safe="")),
        })
except Exception:
    pass

# ── Pinned: detail closed tasks (paths that never move again) ────────────────
# The dynamic block above snapshots page 1 of the OPEN tree, so every case in it
# dies the moment a task is moved between states -- which is exactly what
# happens every time the backlog is pruned (four cases went stale on
# 2026-09-26 alone). These pin the closed buckets, whose paths are immutable
# once a task lands there, so the suite keeps a detail-endpoint baseline no
# matter how much the open tree churns.
PINNED_CLOSED_TASKS = (
    "completed/daily-journal",
    "completed/google-tasks-automation",
    "completed/reddit-daily-digest",
    "completed/ezb-weekly-expense-report",
    "cancelled/pve03-transplant-x99",
    "cancelled/pi-agent-setup",
)

for i, key in enumerate(PINNED_CLOSED_TASKS):
    CASES.append({
        "id": "wl-detail-closed-%02d" % (i + 1), "group": G,
        "title": "detail %s" % key,
        "cmd": ("curl -s '{{BASE}}/api/work/tasks/detail?task=%s' | python3 -c "
                "\"import sys,json; d=json.load(sys.stdin); assert d.get('subject')\""
                % urllib.parse.quote(key, safe="")),
    })

# ── Dynamic: detail first 3 conversations ──
try:
    convs = fetch("/api/work/conversations?per=3")
    for i, c in enumerate(convs.get("conversations", [])[:3]):
        CASES.append({
            "id": "wl-convo-%02d" % (i + 1), "group": G,
            "title": "conversation detail %s" % c["id"][:12],
            "cmd": ("curl -s '{{BASE}}/api/work/conversations/detail?cid=%s' | python3 -c "
                    "\"import sys,json; d=json.load(sys.stdin); assert 'events' in d\""
                    % c["id"]),
        })
except Exception:
    pass

# ── Pagination matrix over conversations ──
for per in ["5", "10", "25"]:
    for page in ["1", "2"]:
        CASES.append({
            "id": "wl-cov-pg-%s-%s" % (per, page), "group": G,
            "title": "conversations per=%s page=%s" % (per, page),
            "cmd": ("curl -s '{{BASE}}/api/work/conversations?per=%s&page=%s' | python3 -c "
                    "\"import sys,json; d=json.load(sys.stdin); "
                    "assert len(d['conversations'])<=int(d['filter']['per'])\""
                    % (per, page)),
        })

# ── Per-page matrix over tasks (states=all) ──
for per in ["10", "20", "50"]:
    for page in ["1", "2", "3"]:
        CASES.append({
            "id": "wl-task-pg-%s-%s" % (per, page), "group": G,
            "title": "tasks litep=1 per=%s page=%s" % (per, page),
            "cmd": ("curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=%s&page=%s' | python3 -c "
                    "\"import sys,json; d=json.load(sys.stdin); "
                    "assert len(d['tasks'])<=int(d['filter']['per'])\""
                    % (per, page)),
        })

# ── State-combination matrix ──
for combo in ["new", "in_progress,new", "new,completed,cancelled",
              "in_progress,new,completed", "cancelled"]:
    CASES.append({
        "id": "wl-state-%s" % combo.replace(",", "-"), "group": G,
        "title": "states=%s" % combo,
        "cmd": ("curl -s '{{BASE}}/api/work/tasks?lite=1&states=%s&per=5' | python3 -c "
                "\"import sys,json; d=json.load(sys.stdin); "
                "expected=sum(d['counts'].get(s,0) for s in d['filter']['states']); "
                "assert d['filter']['total_filtered']==expected\"" % combo),
    })

# ── Tag x state combos ──
for tag in ["infrastructure", "automation", "research", "dashboard-ui"]:
    for st in ["in_progress", "completed"]:
        CASES.append({
            "id": "wl-tag-%s-%s" % (tag, st), "group": G,
            "title": "tag=%s states=%s" % (tag, st),
            "cmd": ("curl -s '{{BASE}}/api/work/tasks?lite=1&states=%s&tag=%s&per=200' | python3 -c "
                    "\"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']>=0\""
                    % (st, tag)),
        })

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.workload.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.workload:", len(CASES))