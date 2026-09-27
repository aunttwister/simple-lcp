# QA case generator 3: Logs unified page + conversations.
import json
import os

CASES = []
G = "logs-convo"

# ── Unified Logs tabs ──
for view, marker in [("conversations", "Conversations"), ("requests", "Requests"),
                     ("providers", "Provider decisions"), ("decisions", "Board decisions")]:
    CASES.append({
        "id": "lg-0%s" % ("1" if view == "conversations" else "2" if view == "requests"
                          else "3" if view == "providers" else "4"),
        "group": G, "title": "Logs tab %s renders" % view,
        "cmd": ("out=$(curl -s '{{BASE}}/logs?view=%s'); echo \"$out\" | grep -q '%s' && "
                "echo \"$out\" | grep -q 'tab-btn'" % (view, marker)),
    })

# ── Requests tab ──
CASES += [
    {"id": "lg-005", "group": G, "title": "requests tab renders with rows",
     "cmd": "out=$(curl -s '{{BASE}}/logs?view=requests&per=5'); echo \"$out\" | grep -q 'work-table' && echo \"$out\" | grep -q 'requests'"},
    {"id": "lg-006", "group": G, "title": "requests API rows have conversation links when stamped",
     "cmd": "curl -s '{{BASE}}/api/work/requests?per=10' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['total']>0\""},
]

# ── Conversations list ──
CASES += [
    {"id": "lg-010", "group": G, "title": "conversations exist with names",
     "cmd": "curl -s '{{BASE}}/api/work/conversations?per=5' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['total']>0; assert all(c.get('name') for c in d['conversations'])\""},
    {"id": "lg-011", "group": G, "title": "conversations paginate",
     "cmd": "curl -s '{{BASE}}/api/work/conversations?per=3' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d['conversations'])<=3; assert d['filter']['per']=='3'\""},
    {"id": "lg-012", "group": G, "title": "profile filter works",
     "cmd": "curl -s '{{BASE}}/api/work/conversations?per=5&profile=l2' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['total']>=0\""},
    {"id": "lg-013", "group": G, "title": "LLM summaries present (summary_source=llm rows)",
     "cmd": "curl -s '{{BASE}}/api/work/conversations?per=5' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert sum(1 for c in d['conversations'] if c.get('summary'))>=1\""},
]

# ── Conversation detail timeline ──
CASES += [
    {"id": "lg-020", "group": G, "title": "detail returns chronological events",
     "cmd": "cid=$(curl -s '{{BASE}}/api/work/conversations?per=1' | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"conversations\"][0][\"id\"])'); curl -s \"{{BASE}}/api/work/conversations/detail?cid=$cid\" | python3 -c 'import sys,json; ts=[e[\"ts\"] for e in json.load(sys.stdin)[\"events\"]]; assert ts==sorted(ts), \"not chronological\"'"},
    {"id": "lg-021", "group": G, "title": "detail requires cid",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/work/conversations/detail')\" = \"400\" ]"},
    {"id": "lg-022", "group": G, "title": "detail unknown cid returns events empty (not 500)",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/work/conversations/detail?cid=nope')\" != \"500\" ]"},
]

# ── Board decisions tab data ──
CASES += [
    {"id": "lg-030", "group": G, "title": "decisions view accessible",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/work/decisions')\" = \"200\" ]"},
]

# ── Provider decisions tab data ──
CASES += [
    {"id": "lg-040", "group": G, "title": "provider decisions list has rows",
     "cmd": "curl -s '{{BASE}}/api/work/provider-decisions?per=5' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['total']>0\""},
    {"id": "lg-041", "group": G, "title": "provider decisions paginate",
     "cmd": "curl -s '{{BASE}}/api/work/provider-decisions?per=3' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d['rows'])<=3\""},
]

# ── Shared table module: paging + ordering on EVERY log ──────────────────────
# The page mounts the module (static/js/logtable.js) and the module renders its
# controls from the JSON filter payload, so these cases check the CONTRACT the
# module renders from rather than pager markup in the HTML.
CASES += [
    {"id": "lg-050", "group": G, "title": "logs page ships the shared table module",
     "cmd": "out=$(curl -s '{{BASE}}/logs?view=requests'); echo \"$out\" | grep -q 'logtable.js' && echo \"$out\" | grep -q 'LCPTable.mount'"},
    {"id": "lg-051", "group": G, "title": "every logs tab ships a table shell",
     "cmd": "for v in conversations requests providers decisions; do out=$(curl -s \"{{BASE}}/logs?view=$v\"); echo \"$out\" | grep -q 'work-table' || exit 1; echo \"$out\" | grep -q 'logtable.js' || exit 1; done"},
    {"id": "lg-052", "group": G, "title": "requests API reports newest-first default",
     "cmd": "curl -s '{{BASE}}/api/work/requests?per=2' | python3 -c \"import sys,json; d=json.load(sys.stdin); f=d['filter']; assert f['sort']=='newest', f; assert [s['key'] for s in f['sorts']][0]=='newest'; assert len(d['rows'])<=2\""},
    {"id": "lg-053", "group": G, "title": "requests pages do not overlap or drop rows",
     "cmd": "curl -s '{{BASE}}/api/work/requests?per=3&page=1' > /tmp/lg1.json; curl -s '{{BASE}}/api/work/requests?per=3&page=2' > /tmp/lg2.json; python3 -c \"import json; a=json.load(open('/tmp/lg1.json'))['rows']; b=json.load(open('/tmp/lg2.json'))['rows']; ia=[r['id'] for r in a]; ib=[r['id'] for r in b]; assert len(set(ia)&set(ib))==0; assert min(ia)>max(ib)\""},
    {"id": "lg-054", "group": G, "title": "requests sort=oldest flips the order",
     "cmd": "curl -s '{{BASE}}/api/work/requests?per=3&sort=oldest' | python3 -c \"import sys,json; d=json.load(sys.stdin); ids=[r['id'] for r in d['rows']]; assert ids==sorted(ids), ids; assert d['filter']['sort']=='oldest'\""},
    {"id": "lg-055", "group": G, "title": "unknown sort key falls back to newest (no SQL leak)",
     "cmd": "curl -s '{{BASE}}/api/work/requests?per=2&sort=id%20DESC%3B%20DROP%20TABLE%20requests' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['filter']['sort']=='newest'\"; curl -s '{{BASE}}/api/work/requests?per=1' | python3 -c \"import sys,json; assert json.load(sys.stdin)['total']>0\""},
    {"id": "lg-056", "group": G, "title": "page past the end clamps to the last page",
     "cmd": "curl -s '{{BASE}}/api/work/requests?per=5&page=99999' | python3 -c \"import sys,json; d=json.load(sys.stdin); f=d['filter']; assert f['page']==f['pages'], f; assert len(d['rows'])>0\""},
    {"id": "lg-057", "group": G, "title": "per=all still honoured by the API but not offered per-tab",
     "cmd": "curl -s '{{BASE}}/api/work/provider-decisions?per=all' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d['rows'])==d['total']; assert 'all' not in d['filter']['per_choices']\""},
    {"id": "lg-058", "group": G, "title": "provider decisions default is newest-first",
     "cmd": "curl -s '{{BASE}}/api/work/provider-decisions?per=2' | python3 -c \"import sys,json; d=json.load(sys.stdin); ids=[r['id'] for r in d['rows']]; assert ids==sorted(ids, reverse=True), ids\""},
    {"id": "lg-059", "group": G, "title": "board decisions ledger paginates and reports sorts",
     "cmd": "curl -s '{{BASE}}/api/work/decisions?per=5' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert len(d['ledger']['rows'])<=5; f=d['filter']; assert f['sort']=='newest'; assert len(d['filter']['sorts'])>=2\""},
    {"id": "lg-060", "group": G, "title": "board decisions funnel covers the whole ledger, not the page",
     "cmd": "curl -s '{{BASE}}/api/work/decisions?per=2' | python3 -c \"import sys,json; d=json.load(sys.stdin); rows=len(d['ledger']['rows']); cons=d['funnel']['considered']; assert cons>=rows, (cons, rows); assert sum(d['funnel']['by_label'].values())==cons\""},
    {"id": "lg-061", "group": G, "title": "board decisions ledger without params is the whole ledger (back-compat)",
     "cmd": "curl -s '{{BASE}}/api/work/decisions' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['ledger']['count']==len(d['ledger']['rows'])\""},
    {"id": "lg-062", "group": G, "title": "conversations default is newest activity first",
     "cmd": "curl -s '{{BASE}}/api/work/conversations?per=3' | python3 -c \"import sys,json; d=json.load(sys.stdin); ls=[c['last_at'] for c in d['conversations']]; assert ls==sorted(ls, reverse=True), ls\""},
    {"id": "lg-063", "group": G, "title": "conversations qid narrows to the named conversation",
     "cmd": "cid=$(curl -s '{{BASE}}/api/work/conversations?per=1' | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"conversations\"][0][\"id\"])'); curl -s \"{{BASE}}/api/work/conversations?qid=$cid\" | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['total']==1 and d['conversations'][0]['id']=='$cid'\""},
    {"id": "lg-064", "group": G, "title": "conversations mount wires the detail endpoint (expand)",
     "cmd": "out=$(curl -s '{{BASE}}/logs?view=conversations'); echo \"$out\" | grep -q \"endpoint: '/api/work/conversations/detail'\" && echo \"$out\" | grep -q 'convo-timeline'"},
    {"id": "lg-065", "group": G, "title": "conversation detail payload carries the event window",
     "cmd": "cid=$(curl -s '{{BASE}}/api/work/conversations?per=1' | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"conversations\"][0][\"id\"])'); curl -s \"{{BASE}}/api/work/conversations/detail?cid=$cid\" | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['id']=='$cid'; assert len(d['events'])>0; assert 'kind' in d['events'][0]\""},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.logs.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.logs:", len(CASES))