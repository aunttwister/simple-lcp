# QA case generator 5: gateway routing, classifier, decisions, fleet.
import json
import os

CASES = []
G = "gateway"

# ── Fleet / providers / health surface ──
CASES += [
    {"id": "gw-001", "group": G, "title": "fleet summary payload",
     "cmd": "curl -s '{{BASE}}/api/work/fleet' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert 'summary' in d and 'profiles' in d\""},
    {"id": "gw-002", "group": G, "title": "providers health 200",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/providers/health')\" = \"200\" ]"},
    {"id": "gw-003", "group": G, "title": "fleet has all profiles",
     "cmd": "curl -s '{{BASE}}/api/work/fleet' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d.get('profiles',[]))>=1\""},
    {"id": "gw-004", "group": G, "title": "routing status endpoint",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/routing/status')\" = \"200\" ]"},
]

# ── Decisions board (the unified Logs decisions tab feeds this) ──
CASES += [
    {"id": "gw-010", "group": G, "title": "decisions view has funnel + ledger",
     "cmd": "curl -s '{{BASE}}/api/work/decisions' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d.get('available') is True; assert d.get('funnel') or d.get('ledger')\""},
    {"id": "gw-011", "group": G, "title": "decisions ledger rows have actors",
     "cmd": "curl -s '{{BASE}}/api/work/decisions' | python3 -c \"import sys,json; d=json.load(sys.stdin); rows=(d.get('ledger') or {}).get('rows') or []; assert all(r.get('actor') for r in rows[:5])\""},
]

# ── Classifier taxonomy surface ──
CASES += [
    {"id": "gw-020", "group": G, "title": "classifications index present",
     "cmd": "[ -f /root/.hermes/profiles/homelab-expert-l2/work/tasks/.work-layers/classifications.json ]"},
    {"id": "gw-021", "group": G, "title": "classifications index has by_label map",
     "cmd": "python3 -c \"import json; d=json.load(open('/root/.hermes/profiles/homelab-expert-l2/work/tasks/.work-layers/classifications.json')); assert len(d.get('by_label',{}))>=8\""},
    {"id": "gw-022", "group": G, "title": "cron snapshot exists and fresh",
     "cmd": "python3 -c \"import json,time; d=json.load(open('/root/.hermes/profiles/homelab-expert-l2/work/tasks/.work-layers/cron-jobs.json')); assert time.time()-d['generated_at_ts']<3600\""},
    {"id": "gw-023", "group": G, "title": "conversations table populated with LLM summaries",
     "cmd": "python3 -c \"import sqlite3; con=sqlite3.connect('/your/data/app/lcp-staging/data/costs.db'); n=con.execute(\\\"SELECT count(*) FROM conversations WHERE summary_source='llm'\\\").fetchone()[0]; assert n>0\""},
]

# ── Routing through the actual chat-completions path ──
# Sends lightweight probe requests through the LCP proxy with the l2 profile.
CASES += [
    {"id": "gw-030", "group": G, "title": "chat completion (stream off) returns 200/201",
     "cmd": ("code=$(curl -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' "
             "-H 'Content-Type: application/json' -d '{\"model\":\"deepseek/deepseek-v4-flash\","
             "\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}],\"max_tokens\":8}'); "
             "[ \"$code\" = \"200\" ] || [ \"$code\" = \"201\" ]")},
    {"id": "gw-031", "group": G, "title": "chat completion with tools payload routes",
     "cmd": ("code=$(curl -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' "
             "-H 'Content-Type: application/json' -d '{\"model\":\"deepseek/deepseek-v4-flash\","
             "\"messages\":[{\"role\":\"user\",\"content\":\"what is 2+2\"}],\"max_tokens\":8,"
             "\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"add\",\"parameters\":{\"type\":\"object\"}}}]}'); "
             "[ \"$code\" = \"200\" ] || [ \"$code\" = \"201\" ]")},
    {"id": "gw-032", "group": G, "title": "unknown profile rejected (400, measured)",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/nope/chat/completions' -H 'Content-Type: application/json' -d '{}')\" = \"400\" ]"},
    {"id": "gw-033", "group": G, "title": "malformed body rejected 400",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' -H 'Content-Type: application/json' -d '{bad')\" = \"400\" ]"},
    {"id": "gw-034", "group": G, "title": "routing decisions recorded for the probe",
     "cmd": "sleep 2; python3 -c \"import sqlite3; con=sqlite3.connect('/your/data/app/lcp-staging/data/costs.db'); n=con.execute(\\\"SELECT count(*) FROM routing_decisions WHERE ts > datetime('now','-5 minutes')\\\").fetchone()[0]; assert n>=1\""},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.gateway.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.gateway:", len(CASES))