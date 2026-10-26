# QA case generator 2: pages.
#
# The console is six pages plus a set of legacy URLs that must redirect rather than
# 404. Every retired surface must stay retired — those are asserted 404 on purpose,
# because a page quietly coming back is how an agent console grows back into a router.
import json
import os

CASES = []
G = "pages"

# ── live pages: 200 and a marker that proves the right template rendered ──
PAGES = [
    ("/profiles", "Profiles", "pg-profiles"),
    ("/models", "Models", "pg-models"),
    ("/models?tab=providers", "Providers", "pg-models-providers"),
    ("/usage", "Usage", "pg-usage"),
    ("/alerts", "Alerts", "pg-alerts"),
    ("/conversations", "Conversations", "pg-conversations"),
    ("/activity", "Activity", "pg-activity"),
    ("/activity?tab=logs", "lg-rows", "pg-activity-logs"),
]
for path, marker, cid in PAGES:
    CASES.append({
        "id": cid, "group": G, "title": "page %s renders %r" % (path, marker),
        "cmd": ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s')\" = \"200\" ] && "
                "curl -s '{{BASE}}%s' | grep -qF '%s'" % (path, path, marker)),
    })

# ── redirects: old URLs still land somewhere sensible ──
REDIRECTS = [
    ("/", "/activity", "pg-r-root"),
    ("/dashboard", "/activity", "pg-r-dashboard"),
    ("/keys", "/profiles?tab=keys", "pg-r-keys"),
    ("/providers", "/models?tab=providers", "pg-r-providers"),
    ("/logs", "/activity?tab=logs", "pg-r-logs"),
    ("/work/config", "/profiles", "pg-r-work-config"),
    ("/work/cron", "/profiles", "pg-r-work-cron"),
]
for path, want, cid in REDIRECTS:
    CASES.append({
        "id": cid, "group": G, "title": "%s redirects to %s" % (path, want),
        "cmd": ("loc=$(curl -s -o /dev/null -w '%%{redirect_url}' '{{BASE}}%s'); "
                "case \"$loc\" in *%s*) exit 0;; *) echo \"got $loc\"; exit 1;; esac"
                % (path, want)),
    })

# ── retired surfaces: gone, and staying gone ──
RETIRED = ["/setup", "/work/tasks", "/work/fleet", "/work/decisions",
           "/work/conversations", "/api/setup", "/api/work/tasks",
           "/api/work/fleet", "/api/work/status", "/api/work/cron",
           "/api/work/cron/ops", "/api/work/sources", "/api/work/decisions",
           "/api/work/conversations"]
for path in RETIRED:
    cid = "pg-404-" + path.strip("/").replace("/", "-")
    CASES.append({
        "id": cid, "group": G, "title": "retired %s is 404" % path,
        "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s')\" = \"404\" ]" % path,
    })

# ── the router's nav shape ──
CASES += [
    {"id": "pg-nav-1", "group": G, "title": "nav lists exactly six entries, in routing order",
     "cmd": ("curl -s '{{BASE}}/activity' | python3 -c \"import sys,re; h=sys.stdin.read(); "
             "nav=re.search(r'<nav class=\\\"sidebar-nav\\\"[^>]*>(.*?)</nav>', h, re.S).group(1); "
             "items=[re.sub(r'\\s+',' ',re.sub(r'<[^>]+>','',x)).strip().split(' ')[0] "
             "for x in re.findall(r'<a [^>]*>(.*?)</a>', nav, re.S)]; "
             "print(items); assert items==['Profiles','Models','Activity','Conversations',"
             "'Usage','Alerts'], items\"")},
    {"id": "pg-nav-2", "group": G, "title": "no agent-infrastructure nav entries survive",
     "cmd": ("curl -s '{{BASE}}/activity' | python3 -c \"import sys; h=sys.stdin.read(); "
             "bad=[n for n in ('>Skills<','>Memory<','>Cron<','>Tasks<','>Fleet<','>Setup<') "
             "if n in h]; assert not bad, bad\"")},
    {"id": "pg-nav-3", "group": G, "title": "sign out is a button, not a bare link",
     "cmd": ("curl -s '{{BASE}}/activity' | python3 -c \"import sys; h=sys.stdin.read(); "
             "assert 'sb-signout' in h, 'no .sb-signout'; assert 'Sign out' in h\"")},
    {"id": "pg-prof-1", "group": G, "title": "Profiles has no tab strip (Config left)",
     "cmd": ("curl -s '{{BASE}}/profiles' | python3 -c \"import sys; h=sys.stdin.read(); "
             "assert 'page-tabs' not in h, 'tab strip is back'; "
             "assert 'sources-form' not in h, 'config form is back'\"")},
    {"id": "pg-prof-2", "group": G, "title": "a retired ?tab= value lands on the directory",
     "cmd": ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/profiles?tab=config')\" = \"200\" ] && "
             "curl -s '{{BASE}}/profiles?tab=config' | grep -qF 'Profiles'")},
    {"id": "pg-model-1", "group": G, "title": "Models keeps its tab strip",
     "cmd": ("curl -s '{{BASE}}/models' | python3 -c \"import sys; h=sys.stdin.read(); "
             "assert 'page-tabs' in h, 'Models lost its tabs'\"")},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.pages.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.pages:", len(CASES))
