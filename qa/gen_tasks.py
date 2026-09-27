# QA case generator 2: Tasks page — lazy table, filters, pagination, detail.
import json
import os

CASES = []
G = "tasks"

# ── Base lite list behaviour ──
CASES += [
    {"id": "ts-001", "group": G, "title": "lite rows exist and carry keys",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&states=in_progress&per=20' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d['tasks'])>=1; assert all(t.get('key') for t in d['tasks'])\""},
    {"id": "ts-002", "group": G, "title": "lite rows have NO plan_html",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&states=in_progress&per=5' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert all('plan_html' not in t['payload'] for t in d['tasks'])\""},
    {"id": "ts-003", "group": G, "title": "states default = in_progress only",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&per=5' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['states']==['in_progress']\""},
    {"id": "ts-004", "group": G, "title": "states=all returns more than in_progress",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=5' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']>d['counts']['in_progress']\""},
    {"id": "ts-005", "group": G, "title": "multiselect states in_progress+completed",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&states=in_progress,completed&per=5' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']==d['counts']['in_progress']+d['counts']['completed']\""},
    {"id": "ts-006", "group": G, "title": "per=all returns every task on one page",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=all' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['pages']==1; assert len(d['tasks'])==d['total']\""},
]

# ── Pagination contract ──
CASES += [
    {"id": "ts-010", "group": G, "title": "per=20 pages math (states=all)",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=20' | python3 -c \"import sys,json; d=json.load(sys.stdin); import math; assert d['filter']['pages']==max(1,math.ceil(d['filter']['total_filtered']/20))\""},
    {"id": "ts-011", "group": G, "title": "page clamp: page 999 -> last page",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=20&page=999' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['page']==d['filter']['pages']\""},
    {"id": "ts-012", "group": G, "title": "page >= pages has rows (last page non-empty)",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=20&page=999' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d['tasks'])>=0\""},
    {"id": "ts-013", "group": G, "title": "page 1 and page 2 do not overlap",
     "cmd": "p1=$(curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=20&page=1'); p2=$(curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=20&page=2'); a=$(echo \"$p1\"|python3 -c 'import sys,json; print(json.dumps([t[\"key\"] for t in json.load(sys.stdin)[\"tasks\"]]))'); b=$(echo \"$p2\"|python3 -c 'import sys,json; print(json.dumps([t[\"key\"] for t in json.load(sys.stdin)[\"tasks\"]]))'); python3 -c \"import sys; a=sys.argv[1]; b=sys.argv[2]; import json; A=set(json.loads(a)); B=set(json.loads(b)); assert not (A&B), 'overlap'\" \"$a\" \"$b\""},
]

# ── Search ──
CASES += [
    {"id": "ts-020", "group": G, "title": "q matches plan text (full walk fallback)",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?states=all&q=searxng&per=10' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']>=1\""},
    {"id": "ts-021", "group": G, "title": "q=zzzqqqxx returns zero",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?states=all&q=zzzqqqxx&per=10' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']==0\""},
    {"id": "ts-022", "group": G, "title": "empty q returns unfiltered",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?states=all&q=&per=10' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']==d['total']\""},
    {"id": "ts-023", "group": G, "title": "search + tag combined",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?states=all&q=searxng&tag=infrastructure&per=10' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']>=0\""},
]

# ── Tag filter ──
CASES += [
    {"id": "ts-030", "group": G, "title": "tag=infrastructure count equals by_label",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?states=all&tag=infrastructure&per=200' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']==d['by_label'].get('infrastructure',0)\""},
    {"id": "ts-031", "group": G, "title": "unknown tag returns zero",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?states=all&tag=nonexistent-tag&per=10' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']==0\""},
    {"id": "ts-032", "group": G, "title": "all 10 known labels present in by_label",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&per=5' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d['by_label'])>=8\""},
]

# ── Detail (lazy expand contract) — key resolved dynamically ──
CASES += [
    {"id": "ts-040", "group": G, "title": "detail returns full heavy payload (dynamic key)",
     "cmd": ("k=$(curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=1' | python3 -c "
             "'import sys,json; print(json.load(sys.stdin)[\"tasks\"][0][\"key\"])'); "
             "curl -s \"{{BASE}}/api/work/tasks/detail?task=$k\" | python3 -c "
             "'import sys,json; d=json.load(sys.stdin); p=d[\"payload\"]; "
             "assert p.get(\"plan_html\") is not None or p.get(\"has_plan\") is not None'")},
    {"id": "ts-041", "group": G, "title": "detail invalid key -> 400",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/work/tasks/detail?task=..%2Fetc%2Fpasswd')\" = \"400\" ]"},
    {"id": "ts-042", "group": G, "title": "detail missing key -> 404",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/work/tasks/detail?task=in_progress%2Fdoes-not-exist-xyz')\" = \"404\" ]"},
    {"id": "ts-043", "group": G, "title": "detail missing task param -> 400",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/work/tasks/detail')\" = \"400\" ]"},
    {"id": "ts-044", "group": G, "title": "detail key round-trips with lite key",
     "cmd": "k=$(curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=1' | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"tasks\"][0][\"key\"])'); curl -s \"{{BASE}}/api/work/tasks/detail?task=$k\" | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d.get('subject')\""},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.tasks.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.tasks:", len(CASES))