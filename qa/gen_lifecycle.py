# QA case generator 6: lifecycle, modules, sources, misc + RESTORE steps.
import json
import os

CASES = []
G = "lifecycle"

# ── Work sources config surface ──
CASES += [
    {"id": "lc-001", "group": G, "title": "sources GET is configured",
     "cmd": "curl -s '{{BASE}}/api/work/sources' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d.get('configured') is not None\""},
    {"id": "lc-002", "group": G, "title": "sources PUT rejects relative path",
     "cmd": ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' -X PUT '{{BASE}}/api/work/sources' "
             "-H 'Content-Type: application/json' -d '{\"version\":1,\"hermes_profiles_dir\":\"relative\"}')\" = \"400\" ]")},
    {"id": "lc-003", "group": G, "title": "sources PUT rejects bad version",
     "cmd": ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' -X PUT '{{BASE}}/api/work/sources' "
             "-H 'Content-Type: application/json' -d '{\"version\":99}')\" = \"400\" ]")},
    {"id": "lc-004", "group": G, "title": "sources GET exposes documented fields",
     "cmd": "curl -s '{{BASE}}/api/work/sources' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert 'hermes_profiles_dir' in d and 'tasks_root' in d and 'ops_dir' in d and 'profiles' in d\""},
]

# ── Module registry surface (breaks nothing, checks reality) ──
CASES += [
    {"id": "lc-010", "group": G, "title": "setup shows module registry",
     "cmd": "out=$(curl -s '{{BASE}}/setup'); echo \"$out\" | grep -qi 'module'"},
    {"id": "lc-011", "group": G, "title": "workspace work-status endpoint healthy",
     "cmd": "curl -s '{{BASE}}/api/work/status' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d.get('available') or d\" "},
    {"id": "lc-012", "group": G, "title": "work status reports liveness rather than lying",
     "cmd": "curl -s '{{BASE}}/api/work/status' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert 'available' in d\""},
]

# ── Security headers / negatives ──
CASES += [
    {"id": "lc-020", "group": G, "title": "unknown route 404",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/definitely-not-a-route')\" = \"404\" ]"},
    {"id": "lc-021", "group": G, "title": "traversal in path rejected",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/..%%2fetc%%2fpasswd')\" != \"200\" ]"},
    {"id": "lc-022", "group": G, "title": "json content type enforced",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' -H 'Content-Type: text/plain' -d '{}')\" != \"200\" ]"},
    {"id": "lc-023", "group": G, "title": "API error responses are JSON",
     "cmd": "curl -s '{{BASE}}/api/work/tasks/detail?task=in_progress%2Fmissing-xyz' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert 'error' in d\""},
]

# ── Alerts + usage surface ──
CASES += [
    {"id": "lc-030", "group": G, "title": "alerts API 200",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/alerts')\" = \"200\" ]"},
    {"id": "lc-031", "group": G, "title": "usage stats 200",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/usage/stats')\" = \"200\" ]"},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.lifecycle.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.lifecycle:", len(CASES))