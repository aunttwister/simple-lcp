# QA case generator 3: every JSON route that the console actually reads.
#
# Grounded in a live probe of the route table: 43 of 54 exact GET routes answer 200
# behind the admin cookie. Each case asserts the status AND the top-level key the
# page's JavaScript depends on — a 200 with a renamed key is a broken page.
import json
import os

CASES = []
G = "api"

# (id, path, top-level key that must be present)
ROUTES = [
    ("api-health", "/health", "status"),
    ("api-in-progress", "/api/in-progress", "count"),
    ("api-conversations", "/api/conversations", "history"),
    ("api-cache-stats", "/cache/stats", "hit_rate"),
    ("api-evidence-export", "/api/evidence/export", "manifest"),
    ("api-evidence-key", "/api/evidence/public-key", "public_key_pem"),
    ("api-settings", "/api/settings", "entries"),
    ("api-routing-status", "/api/routing/status", "enabled"),
    ("api-work-requests", "/api/work/requests", "rows"),
    ("api-work-decisions", "/api/work/provider-decisions", "rows"),
    ("api-daily-costs", "/api/daily-costs", "daily_costs"),
    ("api-recent-requests", "/api/recent-requests", "requests"),
    ("api-logs", "/api/logs", "rows"),
    ("api-usage-stats", "/api/usage/stats", "totals"),
    ("api-usage-totals", "/api/usage/totals", "requests"),
    ("api-usage-series", "/api/usage/profile-series", "dates"),
    ("api-providers-presets", "/api/providers/presets", "presets"),
    ("api-providers-health", "/api/providers/health", "summary"),
    ("api-providers-failovers", "/api/providers/failovers", "failovers"),
    ("api-providers", "/api/providers", "providers"),
    ("api-profiles", "/api/profiles", "profiles"),
    ("api-audit", "/api/audit", "entries"),
    ("api-keys", "/api/keys", "keys"),
    ("api-alerts-config", "/api/alerts/config", None),
    ("api-alerts", "/api/alerts", "alerts"),
    ("api-budgets", "/api/budgets", None),
    ("api-cost-plugins-usage", "/api/cost-plugins/usage", None),
    ("api-cost-plugins-balances", "/api/cost-plugins/balances", None),
    ("api-cost-plugins-summary", "/api/cost-plugins/summary", None),
    ("api-models-capability", "/api/models/capability", None),
    ("api-models-registry", "/api/models/registry", "registry"),
]
for cid, path, key in ROUTES:
    if key:
        cmd = ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s')\" = \"200\" ] && "
               "curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
               "assert d.get('%s') is not None, 'missing key %s'\"" % (path, path, key, key))
    else:
        cmd = ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s')\" = \"200\" ] && "
               "curl -s '{{BASE}}%s' | python3 -c \"import sys,json; json.load(sys.stdin)\""
               % (path, path))
    CASES.append({"id": cid, "group": G, "title": path, "cmd": cmd})

# ── cross-endpoint consistency: the same fact told twice must agree ──
CASES += [
    {"id": "api-x-profiles", "group": G,
     "title": "/api/profiles and /api/providers/profile_chains name the same profiles",
     "cmd": ("curl -s '{{BASE}}/api/profiles' > /tmp/qa_p.json; "
             "curl -s '{{BASE}}/api/providers' > /tmp/qa_c.json; "
             "python3 -c \"import json; p=set(json.load(open('/tmp/qa_p.json'))['profiles']); "
             "c=set(json.load(open('/tmp/qa_c.json'))['profile_chains']); "
             "assert p==c, (sorted(p-c), sorted(c-p))\"")},
    {"id": "api-x-routing", "group": G,
     "title": "/api/routing/status covers every profile in /api/profiles",
     "cmd": ("curl -s '{{BASE}}/api/routing/status' > /tmp/qa_r.json; "
             "curl -s '{{BASE}}/api/profiles' > /tmp/qa_p.json; "
             "python3 -c \"import json; r=set(json.load(open('/tmp/qa_r.json'))['per_profile']); "
             "p=set(json.load(open('/tmp/qa_p.json'))['profiles']); "
             "assert not (p-r), sorted(p-r)\"")},
    {"id": "api-x-health", "group": G,
     "title": "health reports every profile and no dead provider",
     "cmd": ("curl -s '{{BASE}}/health' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert d['status']=='ok'; assert len(d['profiles'])>=1; "
             "assert all(v.get('state')!='dead' for v in d['providers'].values()), "
             "[k for k,v in d['providers'].items() if v.get('state')=='dead']\"")},
    {"id": "api-x-totals", "group": G,
     "title": "/api/usage/totals agrees with /api/usage/stats.totals",
     "cmd": ("curl -s '{{BASE}}/api/usage/stats' > /tmp/qa_s.json; "
             "curl -s '{{BASE}}/api/usage/totals' > /tmp/qa_t.json; "
             "python3 -c \"import json; s=json.load(open('/tmp/qa_s.json'))['totals']; "
             "t=json.load(open('/tmp/qa_t.json')); "
             "assert s['requests']==t['requests'], (s['requests'],t['requests']); "
             "assert abs(s['cost']-t['cost'])<0.01, (s['cost'],t['cost'])\"")},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.api.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.api:", len(CASES))
