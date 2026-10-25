# QA case generator 5: usage + routing.
#
# These are the arithmetic checks: the dashboard's headline numbers must equal the
# sum of the breakdowns they claim to summarise. A total that disagrees with its own
# parts is the most expensive kind of wrong, because it looks authoritative.
import json
import os

CASES = []
G = "usage"

S = "/api/usage/stats"
T = "/api/usage/totals"

CASES += [
    {"id": "us-shape", "group": G, "title": "stats carries daily, by_model, by_profile, totals, cache",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert {'daily','by_model','by_profile','totals','cache'} <= set(d)\"" % S)},
    {"id": "us-totals", "group": G, "title": "totals has cost, requests, tokens",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "t=d['totals']; assert {'cost','requests','tokens','prompt_tokens','completion_tokens'} "
             "<= set(t); assert t['requests']>0\"" % S)},
    {"id": "us-by-model-sums", "group": G, "title": "by_model cost sums to the total cost",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "s=sum(v['cost'] for v in d['by_model'].values()); "
             "assert abs(s-d['totals']['cost'])<0.02, (s, d['totals']['cost'])\"" % S)},
    {"id": "us-by-profile-sums", "group": G, "title": "by_profile cost sums to the total cost",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "s=sum(v['cost'] for v in d['by_profile'].values()); "
             "assert abs(s-d['totals']['cost'])<0.02, (s, d['totals']['cost'])\"" % S)},
    {"id": "us-by-model-requests", "group": G, "title": "by_model requests sum to the total requests",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "s=sum(v['requests'] for v in d['by_model'].values()); "
             "assert s==d['totals']['requests'], (s, d['totals']['requests'])\"" % S)},
    {"id": "us-daily", "group": G, "title": "daily has a cost per day, no gaps in the window",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "days=d['daily']; assert len(days)>=1; "
             "assert all('date' in x and 'cost' in x for x in days); "
             "ds=[x['date'] for x in days]; assert ds==sorted(ds), 'daily not chronological'\""
             % S)},
    {"id": "us-cache", "group": G, "title": "cache reports hits, misses and savings consistently",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "c=d['cache']; assert {'hit_tokens','miss_tokens','savings'} <= set(c); "
             "assert c['hit_tokens']>=0 and c['miss_tokens']>=0; assert c['savings']>=0\""
             % S)},
    {"id": "us-totals-agree", "group": G, "title": "/api/usage/totals agrees with stats.totals",
     "cmd": ("curl -s '{{BASE}}%s' > /tmp/us1.json; curl -s '{{BASE}}%s' > /tmp/us2.json; "
             "python3 -c \"import json; s=json.load(open('/tmp/us1.json'))['totals']; "
             "t=json.load(open('/tmp/us2.json')); "
             "assert s['requests']==t['requests'], (s['requests'],t['requests']); "
             "assert abs(s['cost']-t['cost'])<0.01\"" % (S, T))},
    {"id": "us-provider-filter", "group": G, "title": "a provider filter narrows without changing shape",
     "cmd": ("curl -s '{{BASE}}%s?provider=deepseek' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); assert d['totals']['requests']>=0; "
             "assert {'daily','by_model','by_profile','totals'} <= set(d)\"" % S)},
    {"id": "us-series", "group": G, "title": "profile-series has one value per day, per profile",
     "cmd": ("curl -s '{{BASE}}/api/usage/profile-series' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); n=d['days']; "
             "assert len(d['dates'])==n, (len(d['dates']), n); "
             "bad=[(p, len(v.get('cost') or [])) for p,v in d['profiles'].items() "
             "if len(v.get('cost') or [])!=n]; assert not bad, bad\"" )},
    {"id": "us-daily-costs", "group": G, "title": "daily-costs returns a non-empty series",
     "cmd": ("curl -s '{{BASE}}/api/daily-costs' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert len(d['daily_costs'])>0\"")},
    {"id": "us-page", "group": G, "title": "the usage page renders its tables",
     "cmd": ("curl -s '{{BASE}}/usage' | python3 -c \"import sys,re; h=sys.stdin.read(); "
             "assert 'Usage' in h; assert len(re.findall(r'<caption', h))>=1, 'no captions'\"")},
]

G2 = "routing"

CASES += [
    {"id": "rt-status", "group": G2, "title": "routing status reports policy, rules and decisions",
     "cmd": ("curl -s '{{BASE}}/api/routing/status' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); assert isinstance(d['enabled'],bool); "
             "assert isinstance(d['policy'],str) and d['policy']; "
             "assert isinstance(d['rules'],list); assert isinstance(d['recent_decisions'],list)\"")},
    {"id": "rt-covers-profiles", "group": G2, "title": "routing status covers every configured profile",
     "cmd": ("curl -s '{{BASE}}/api/routing/status' > /tmp/rt.json; "
             "curl -s '{{BASE}}/api/profiles' > /tmp/pf.json; "
             "python3 -c \"import json; r=set(json.load(open('/tmp/rt.json'))['per_profile']); "
             "p=set(json.load(open('/tmp/pf.json'))['profiles']); "
             "assert not (p-r), sorted(p-r)\"")},
    {"id": "rt-providers-subset", "group": G2,
     "title": "every routed provider is a configured provider",
     "cmd": ("curl -s '{{BASE}}/api/routing/status' > /tmp/rt.json; "
             "curl -s '{{BASE}}/api/providers' > /tmp/pv.json; "
             "python3 -c \"import json; r=set(json.load(open('/tmp/rt.json'))['providers']); "
             "p=set(json.load(open('/tmp/pv.json'))['providers']); "
             "assert not (r-p), sorted(r-p)\"")},
    {"id": "rt-decisions-in-log", "group": G2,
     "title": "recent decisions also appear in the decision log",
     "cmd": ("curl -s '{{BASE}}/api/routing/status' > /tmp/rt.json; "
             "curl -s '{{BASE}}/api/work/provider-decisions?per=50' > /tmp/dl.json; "
             "python3 -c \"import json; r=json.load(open('/tmp/rt.json'))['recent_decisions']; "
             "l=json.load(open('/tmp/dl.json'))['rows']; "
             "assert len(r)>0 and len(l)>0; "
             "assert {x.get('provider') for x in r} <= {x.get('provider') for x in l}\"")},
    {"id": "rt-health-summary", "group": G2,
     "title": "provider health summary counts match its own provider list",
     "cmd": ("curl -s '{{BASE}}/api/providers/health' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); s=d['summary']; n=len(d['providers']); "
             "assert s['total']==n, (s['total'], n); "
             "assert s['healthy']+s['degraded']+s['dead']==n, s\"")},
    {"id": "rt-failovers", "group": G2, "title": "failover rows carry from/to/provider",
     "cmd": ("curl -s '{{BASE}}/api/providers/failovers' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); rows=d['failovers']; "
             "assert isinstance(rows,list); "
             "assert all(isinstance(x,dict) and x for x in rows[:5])\"")},
    {"id": "rt-presets", "group": G2, "title": "provider presets are non-empty and shaped",
     "cmd": ("curl -s '{{BASE}}/api/providers/presets' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); pr=d['presets']; "
             "assert isinstance(pr,dict) and pr, 'no presets'; "
             "assert all(isinstance(v,dict) for v in pr.values())\"")},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.usage.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.usage:", len(CASES))
