# QA case generator 9: misc — retired pages, services, deeper pagination.
import json
import os

CASES = []
G = "misc"

# ── Retired pages must be gone / 404 ──
CASES += [
    {"id": "ms-001", "group": G, "title": "retired /work/decisions page 404",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/work/decisions')\" = \"404\" ]"},
    {"id": "ms-002", "group": G, "title": "retired /work/conversations page 404",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/work/conversations')\" = \"404\" ]"},
]

# ── Requests + provider-decisions pagination details ──
for per in ["3", "7"]:
    CASES.append({
        "id": "ms-req-%s" % per, "group": G, "title": "requests per=%s" % per,
        "cmd": ("curl -s '{{BASE}}/api/work/requests?per=%s' | python3 -c "
                "\"import sys,json; d=json.load(sys.stdin); assert len(d['rows'])<=%s; assert d['total']>0\""
                % (per, per)),
    })
    CASES.append({
        "id": "ms-pro-%s" % per, "group": G, "title": "provider-decisions per=%s" % per,
        "cmd": ("curl -s '{{BASE}}/api/work/provider-decisions?per=%s' | python3 -c "
                "\"import sys,json; d=json.load(sys.stdin); assert len(d['rows'])<=%s; assert d['total']>0\""
                % (per, per)),
    })

# ── Conversations quality ──
CASES += [
    {"id": "ms-conv-1", "group": G, "title": "newest page names are not bare fallbacks",
     "cmd": ("curl -s '{{BASE}}/api/work/conversations?per=10' | python3 -c "
             "\"import sys,json; d=json.load(sys.stdin); "
             "assert not any(c['name'].startswith('Conversation c') for c in d['conversations'])\"")},
    {"id": "ms-conv-2", "group": G, "title": "LLM summary coverage > 500 conversations",
     "cmd": ("python3 -c \"import sqlite3; "
             "con=sqlite3.connect('/your/data/app/lcp/data/costs.db'); "
             "n=con.execute(\\\"SELECT count(*) FROM conversations WHERE summary_source='llm'\\\").fetchone()[0]; "
             "assert n>500, n\"")},
    {"id": "ms-conv-3", "group": G, "title": "conversations page shows names in table",
     "cmd": "out=$(curl -s '{{BASE}}/logs?view=conversations&per=5'); echo \"$out\" | grep -c 'work-job-name' | python3 -c 'import sys; assert int(sys.stdin.read()) >= 5'"},
]

# ── Host services backing the features ──
CASES += [
    {"id": "ms-svc-1", "group": G, "title": "convo-summarize timer active",
     "cmd": "systemctl is-active work-layers-convo-summarize.timer | grep -q active"},
    {"id": "ms-svc-2", "group": G, "title": "convo-sync timer active",
     "cmd": "systemctl is-active work-layers-convo-sync.timer | grep -q active"},
    {"id": "ms-svc-3", "group": G, "title": "classify timer active",
     "cmd": "systemctl is-active work-layers-classify.timer | grep -q active"},
    {"id": "ms-svc-4", "group": G, "title": "cron snapshot service ran recently",
     "cmd": "python3 -c \"import json,time; d=json.load(open('/root/.hermes/profiles/homelab-expert-l2/work/tasks/.work-layers/cron-jobs.json')); assert time.time()-d['generated_at_ts']<3600\""},
    {"id": "ms-svc-5", "group": G, "title": "lcp-staging container running",
     "cmd": "docker inspect -f '{{.State.Running}}' lcp-staging | grep -q true"},
    {"id": "ms-svc-6", "group": G, "title": "convo summarizer lock released (no stuck run)",
     "cmd": "pgrep -f summarize_convs.py | wc -l | python3 -c 'import sys; assert int(sys.stdin.read()) <= 1'"},
]

# ── Streaming chat path ──
CASES += [
    {"id": "ms-fx-1", "group": G, "title": "streaming chat returns SSE",
     "cmd": ("out=$(curl -s -N -X POST '{{BASE}}/l2/chat/completions' -H 'Content-Type: application/json' "
             "-d '{\"model\":\"deepseek/deepseek-v4-flash\",\"stream\":true,"
             "\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":6}'); "
             "echo \"$out\" | grep -q 'data:'")},
    {"id": "ms-fx-2", "group": G, "title": "chat with unknown model never 5xx (chain fallback or 4xx)",
     "cmd": ("code=$(curl -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' "
             "-H 'Content-Type: application/json' -d '{\"model\":\"nope/nope\","
             "\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":6}'); "
             "[ \"$code\" != \"500\" ] && [ \"$code\" != \"000\" ]")},
]

# ── Nav structure ──
CASES += [
    {"id": "ms-nav-1", "group": G, "title": "exactly 4 nav groups",
     "cmd": "out=$(curl -s '{{BASE}}/work/tasks'); echo \"$out\" | grep -c 'nav-label' | python3 -c 'import sys; assert int(sys.stdin.read())==4'"},
    {"id": "ms-nav-2", "group": G, "title": "Tasks link present once in nav",
     "cmd": "out=$(curl -s '{{BASE}}/work/tasks'); echo \"$out\" | grep -c 'href=\"/work/tasks\"' | python3 -c 'import sys; assert int(sys.stdin.read())==1'"},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.misc.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.misc:", len(CASES))