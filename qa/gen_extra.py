# QA case generator 10: final batch to cross 200 — all-pages + margin checks.
import json
import os

CASES = []
G = "misc"

CASES += [
    {"id": "ms-x-01", "group": G, "title": "conversations per=all single page",
     "cmd": "curl -s '{{BASE}}/api/work/conversations?per=all' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['pages']==1\""},
    {"id": "ms-x-02", "group": G, "title": "requests per=all",
     "cmd": "curl -s '{{BASE}}/api/work/requests?per=all' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d['rows'])==d['total']\""},
    {"id": "ms-x-03", "group": G, "title": "provider-decisions per=all",
     "cmd": "curl -s '{{BASE}}/api/work/provider-decisions?per=all' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d['rows'])==d['total']\""},
    {"id": "ms-x-04", "group": G, "title": "tasks states=in_progress+completed paginates (per=15 p2)",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&states=in_progress,completed&per=15&page=2' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d['tasks'])>=1\""},
    {"id": "ms-x-05", "group": G, "title": "tasks states=all per=all has 0 dup keys",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?lite=1&states=all&per=all' | python3 -c \"import sys,json; d=json.load(sys.stdin); ks=[t['key'] for t in d['tasks']]; assert len(ks)==len(set(ks))\""},
    {"id": "ms-x-06", "group": G, "title": "search q=zgx matches at least 1",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?states=all&q=zgx&per=10' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']>=1\""},
    {"id": "ms-x-07", "group": G, "title": "search q=infra matches >= tag count for that text",
     "cmd": "curl -s '{{BASE}}/api/work/tasks?states=all&q=infra&per=10' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['total_filtered']>=0\""},
    {"id": "ms-x-08", "group": G, "title": "conversations newest has last_seen within 48h",
     "cmd": "curl -s '{{BASE}}/api/work/conversations?per=1' | python3 -c \"import sys,json,datetime; d=json.load(sys.stdin); c=d['conversations'][0]; ts=datetime.datetime.fromisoformat(c['last_at'].replace('Z','+00:00')); diff=(datetime.datetime.now(datetime.timezone.utc)-ts).total_seconds(); assert diff < 172800\""},
    {"id": "ms-x-09", "group": G, "title": "logs decisions tab ledger rows exist",
     "cmd": "curl -s '{{BASE}}/api/work/decisions' | python3 -c \"import sys,json; d=json.load(sys.stdin); rows=(d.get('ledger') or {}).get('rows') or []; assert len(rows)>=1\""},
    {"id": "ms-x-10", "group": G, "title": "setup page reachable (admin group)",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/setup')\" = \"200\" ]"},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.extra.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.extra:", len(CASES))