# QA case generator 3: the log tables — requests, provider decisions, /api/logs.
#
# The page mounts static/js/logtable.js and the module renders its controls from the
# JSON filter payload, so these cases check the CONTRACT the module renders from
# rather than pager markup in the HTML. Sorting is server-side by design: a client
# sort of a paged table reorders only the page you can see.
import json
import os

CASES = []
G = "logs"

REQ = "/api/work/requests"
DEC = "/api/work/provider-decisions"

CASES += [
    {"id": "lg-req-shape", "group": G, "title": "requests default: newest first, 20 rows",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "f=d['filter']; assert f['sort']=='newest', f; assert f['per']=='20'; "
             "assert len(d['rows'])<=20; assert d['total']>0\"" % REQ)},
    {"id": "lg-req-pages", "group": G, "title": "requests page count matches the total",
     "cmd": ("curl -s '{{BASE}}%s?per=20' | python3 -c \"import sys,json,math; "
             "d=json.load(sys.stdin); f=d['filter']; "
             "assert f['pages']==max(1,math.ceil(f['total']/20)), (f['pages'],f['total'])\""
             % REQ)},
    {"id": "lg-req-nonoverlap", "group": G, "title": "requests pages do not overlap or drop rows",
     "cmd": ("curl -s '{{BASE}}%s?per=3&page=1' > /tmp/lg1.json; "
             "curl -s '{{BASE}}%s?per=3&page=2' > /tmp/lg2.json; "
             "python3 -c \"import json; a=[r['id'] for r in json.load(open('/tmp/lg1.json'))['rows']]; "
             "b=[r['id'] for r in json.load(open('/tmp/lg2.json'))['rows']]; "
             "assert not (set(a)&set(b)); assert min(a)>max(b)\"" % (REQ, REQ))},
    {"id": "lg-req-oldest", "group": G, "title": "requests sort=oldest flips the order",
     "cmd": ("curl -s '{{BASE}}%s?per=3&sort=oldest' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); ids=[r['id'] for r in d['rows']]; "
             "assert ids==sorted(ids), ids; assert d['filter']['sort']=='oldest'\"" % REQ)},
    {"id": "lg-req-cost", "group": G, "title": "requests sort=cost is monotonic descending",
     "cmd": ("curl -s '{{BASE}}%s?per=5&sort=cost' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); c=[r['cost'] for r in d['rows']]; "
             "assert c==sorted(c, reverse=True), c; assert d['filter']['sort']=='cost'\"" % REQ)},
    {"id": "lg-req-tokens", "group": G, "title": "requests sort=tokens is monotonic descending",
     "cmd": ("curl -s '{{BASE}}%s?per=5&sort=tokens' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); t=[(r['prompt_tokens'] or 0)+(r['completion_tokens'] or 0) "
             "for r in d['rows']]; assert t==sorted(t, reverse=True), t\"" % REQ)},
    {"id": "lg-req-latency", "group": G, "title": "requests sort=latency is monotonic descending",
     "cmd": ("curl -s '{{BASE}}%s?per=5&sort=latency' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); l=[r['latency_ms'] or 0 for r in d['rows']]; "
             "assert l==sorted(l, reverse=True), l\"" % REQ)},
    {"id": "lg-req-inject", "group": G,
     "title": "a sort key that is SQL is rejected, and the table survives",
     "cmd": ("curl -s '{{BASE}}%s?per=2&sort=id%%20DESC%%3B%%20DROP%%20TABLE%%20requests' "
             "| python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert d['filter']['sort']=='newest', d['filter']['sort']\"; "
             "curl -s '{{BASE}}%s?per=1' | python3 -c \"import sys,json; "
             "assert json.load(sys.stdin)['total']>0\"" % (REQ, REQ))},
    {"id": "lg-req-clamp-page", "group": G, "title": "page past the end clamps to the last page",
     "cmd": ("curl -s '{{BASE}}%s?per=5&page=99999' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); f=d['filter']; assert f['page']==f['pages'], f; "
             "assert len(d['rows'])>0\"" % REQ)},
    {"id": "lg-req-clamp-per0", "group": G, "title": "per=0 clamps to 1 rather than dividing by zero",
     "cmd": ("curl -s '{{BASE}}%s?per=0' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "f=d['filter']; assert f['per']=='1', f; assert len(d['rows'])==1\"" % REQ)},
    {"id": "lg-req-clamp-permax", "group": G, "title": "an absurd per is capped",
     "cmd": ("curl -s '{{BASE}}%s?per=100000' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert int(d['filter']['per'])<=500, d['filter']['per']; "
             "assert len(d['rows'])<=500\"" % REQ)},
    {"id": "lg-req-profile", "group": G, "title": "profile filter narrows and echoes back",
     "cmd": ("curl -s '{{BASE}}%s' > /tmp/lga.json; "
             "curl -s '{{BASE}}%s?profile=l2' > /tmp/lgb.json; "
             "python3 -c \"import json; a=json.load(open('/tmp/lga.json')); "
             "b=json.load(open('/tmp/lgb.json')); "
             "assert b['total']<a['total'], (b['total'],a['total']); "
             "assert b['filter']['profile']=='l2'; "
             "assert all(r['profile']=='l2' for r in b['rows'])\"" % (REQ, REQ))},
    {"id": "lg-req-profile-none", "group": G, "title": "an unknown profile returns zero, not an error",
     "cmd": ("curl -s '{{BASE}}%s?profile=definitely-not-a-profile' | python3 -c "
             "\"import sys,json; d=json.load(sys.stdin); assert d['total']==0; "
             "assert d['rows']==[]\"" % REQ)},
    {"id": "lg-req-perall", "group": G, "title": "per=all returns every row on one page",
     "cmd": ("curl -s '{{BASE}}%s?per=all' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert len(d['rows'])==d['total']; assert d['filter']['pages']==1\"" % REQ)},
    {"id": "lg-req-idem", "group": G, "title": "two identical requests return identical bytes",
     "cmd": ("a=$(curl -s '{{BASE}}%s?per=5' | md5sum); "
             "b=$(curl -s '{{BASE}}%s?per=5' | md5sum); [ \"$a\" = \"$b\" ]" % (REQ, REQ))},
]

CASES += [
    {"id": "lg-dec-shape", "group": G, "title": "decisions default: newest first",
     "cmd": ("curl -s '{{BASE}}%s?per=2' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "ids=[r['id'] for r in d['rows']]; assert ids==sorted(ids, reverse=True), ids; "
             "assert d['total']>0\"" % DEC)},
    {"id": "lg-dec-oldest", "group": G, "title": "decisions sort=oldest flips the order",
     "cmd": ("curl -s '{{BASE}}%s?per=3&sort=oldest' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); ids=[r['id'] for r in d['rows']]; "
             "assert ids==sorted(ids), ids\"" % DEC)},
    {"id": "lg-dec-pages", "group": G, "title": "decisions page count matches the total",
     "cmd": ("curl -s '{{BASE}}%s?per=20' | python3 -c \"import sys,json,math; "
             "d=json.load(sys.stdin); f=d['filter']; "
             "assert f['pages']==max(1,math.ceil(f['total']/20)), (f['pages'],f['total'])\""
             % DEC)},
    {"id": "lg-dec-perall", "group": G, "title": "decisions per=all returns every row",
     "cmd": ("curl -s '{{BASE}}%s?per=all' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert len(d['rows'])==d['total']\"" % DEC)},
    {"id": "lg-dec-fields", "group": G, "title": "every decision row carries the routing verdict",
     "cmd": ("curl -s '{{BASE}}%s?per=25' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "miss=[r['id'] for r in d['rows'] if not (r.get('profile') and r.get('provider') "
             "and r.get('model') and r.get('action'))]; assert not miss, miss\""
             % DEC)},
    {"id": "lg-dec-idem", "group": G, "title": "decisions are idempotent",
     "cmd": ("a=$(curl -s '{{BASE}}%s?per=5' | md5sum); "
             "b=$(curl -s '{{BASE}}%s?per=5' | md5sum); [ \"$a\" = \"$b\" ]" % (DEC, DEC))},
]

CASES += [
    {"id": "lg-api-logs", "group": G, "title": "/api/logs reports total, rows and facets",
     "cmd": ("curl -s '{{BASE}}/api/logs' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert d['total']>0; assert d['rows']; "
             "assert {'total','limit','offset','rows','profiles','providers'} <= set(d)\"")},
    {"id": "lg-api-logs-limit", "group": G, "title": "/api/logs honours its limit",
     "cmd": ("curl -s '{{BASE}}/api/logs?limit=5' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); assert len(d['rows'])<=5, len(d['rows'])\"")},
    {"id": "lg-page-shell", "group": G, "title": "the logs tab ships the shared table module",
     "cmd": ("out=$(curl -s '{{BASE}}/activity?tab=logs'); echo \"$out\" | grep -q 'logtable.js' && "
             "echo \"$out\" | grep -q 'lg-rows' && echo \"$out\" | grep -q 'lg-controls'")},
    {"id": "lg-page-captions", "group": G, "title": "every log table carries a non-empty caption",
     "cmd": ("for t in logs 'logs&view=providers'; do curl -s \"{{BASE}}/activity?tab=$t\" | python3 -c "
             "\"import sys,re; h=sys.stdin.read(); "
             "caps=re.findall(r'<caption[^>]*>(.*?)</caption>', h, re.S); "
             "assert len(caps)==1, (len(caps), caps); "
             "assert caps[0].strip(), 'empty caption'\" || exit 1; done")},
    {"id": "lg-page-scope", "group": G, "title": "every log header carries scope=col",
     "cmd": ("for t in logs 'logs&view=providers'; do curl -s \"{{BASE}}/activity?tab=$t\" | python3 -c "
             "\"import sys,re; h=sys.stdin.read(); "
             "ths=re.findall(r'<th[\\s>](?![^>]*scope)', h); assert not ths, len(ths)\" || exit 1; done")},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.logs.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.logs:", len(CASES))
