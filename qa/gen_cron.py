# QA case generator 4: Cron CRUD/ops + Work sources.
import json

CASES = []
G = "cron-sources"

# The profile every cron-ops case targets. It is NOT a real agent profile: it has no
# config.yaml, so profile_data.agent_profiles() skips it and it never appears on the
# Profiles page, and it is pinned hidden:true in the ops work-sources.json so the Cron
# listing skips it too. It exists so these cases can exercise a real, accepted write
# path without writing probe jobs into a live agent's schedule.
# (It used to target `backups`, which was an accident — a stray cron/jobs.json made a
# directory of profile archives look like a profile. See FIXTURE below.)
FIXTURE = "qa-fixture"

# ── Cron API surface ──
CASES += [
    {"id": "cr-001", "group": G, "title": "cron view counts sane",
     "cmd": "curl -s '{{BASE}}/api/work/cron' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert d['available'] is True; assert d['counts']['total']>0\""},
    {"id": "cr-002", "group": G, "title": "cron ops list empty-pending",
     "cmd": "curl -s '{{BASE}}/api/work/cron/ops' | python3 -c \"import sys,json; d=json.load(sys.stdin); assert 'pending' in d and 'done' in d\""},
    {"id": "cr-003", "group": G, "title": "ops spool dir writable (POST surface)",
     "cmd": "[ -d /your/data/app/lcp/cron-ops/ops ] && [ -d /your/data/app/lcp/cron-ops/done ]"},
    {"id": "cr-004", "group": G, "title": "fixture is hidden from the cron listing",
     "cmd": "curl -s '{{BASE}}/api/work/cron' | python3 -c \"import sys,json; d=json.load(sys.stdin); f=[p for p in d['profiles'] if p['profile']=='%s']; assert len(f)==1, 'fixture missing from snapshot'; assert f[0]['hidden'] is True, 'fixture is not hidden'\"" % FIXTURE},
    {"id": "cr-005", "group": G, "title": "fixture is not an agent profile (no config.yaml)",
     "cmd": "[ ! -f /root/.hermes/profiles/%s/config.yaml ]" % FIXTURE},
]

# ── Fixture reset. Runs first so a previous run's probe can never accumulate. ──
# The fixture is disposable, but the cron scheduler owns jobs.json (there is a lock
# file), so cleanup goes through the CLI rather than writing the file.
CASES += [
    {"id": "cr-009", "group": G, "title": "fixture reset: remove stale probes",
     "cmd": ("python3 -c \"import json,os,subprocess;"
             "p='/root/.hermes/profiles/%s/cron/jobs.json';"
             "j=json.load(open(p)).get('jobs',[]) if os.path.exists(p) else [];"
             "ids=[x['id'] for x in j if x.get('name')=='qa-suite-probe'];"
             "[subprocess.run(['/root/.hermes/hermes-agent/venv/bin/python','-m','hermes_cli.main',"
             "'--profile','%s','cron','remove',i],capture_output=True) for i in ids];"
             "print('removed',len(ids))\"" % (FIXTURE, FIXTURE))},
]

# ── Ops validation (reject malformed without side effects) ──
# Every payload here is malformed in exactly ONE way, and targets a profile that IS
# valid — otherwise a 400 would prove nothing about the malformed field.
BAD_OPS = [
    ("cr-010", "unknown action", {"action": "explode", "profile": FIXTURE}),
    ("cr-011", "unknown profile", {"action": "remove", "profile": "nope-profile", "job_id": "a1b2c3d4e5f6"}),
    ("cr-012", "create without prompt/script", {"action": "create", "profile": FIXTURE, "name": "x", "schedule": "30m"}),
    ("cr-013", "script with slash", {"action": "create", "profile": FIXTURE, "name": "x", "schedule": "30m", "script": "../evil.sh"}),
    ("cr-014", "edit empty fields", {"action": "edit", "profile": FIXTURE, "job_id": "a1b2c3d4e5f6", "fields": {}}),
    ("cr-015", "bad deliver", {"action": "create", "profile": FIXTURE, "name": "x", "schedule": "30m", "prompt": "p", "deliver": "nowhere"}),
    ("cr-016", "workdir outside /your/data", {"action": "create", "profile": FIXTURE, "name": "x", "schedule": "30m", "prompt": "p", "workdir": "/etc"}),
]
import json as _json
for cid, title, payload in BAD_OPS:
    CASES.append({
        "id": cid, "group": G, "title": "reject: %s" % title,
        "cmd": ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/api/work/cron/ops' "
                "-H 'Content-Type: application/json' -d '%s')\" = \"400\" ]"
                % _json.dumps(payload).replace('"', '\\"')),
    })

# ── Ops lifecycle on the fixture. Accepted create → executor applies it → cr-009 of
#    the NEXT run removes it. The fixture is the only place a probe may land. ──
CASES += [
    {"id": "cr-020", "group": G, "title": "create op accepted (%s)" % FIXTURE,
     "cmd": ("op=$(curl -s -X POST '{{BASE}}/api/work/cron/ops' -H 'Content-Type: application/json' "
             "-d '{\"action\":\"create\",\"profile\":\"%s\",\"name\":\"qa-suite-probe\","
             "\"schedule\":\"2033-01-01T00:00:00\",\"prompt\":\"qa probe - safe to delete\","
             "\"deliver\":\"local\"}'); echo \"$op\" | python3 -c "
             "'import sys,json; d=json.load(sys.stdin); assert d.get(\"status\")==\"pending\", d'" % FIXTURE)},
    {"id": "cr-021", "group": G, "title": "accepted create leaves no real profile touched",
     "cmd": ("python3 -c \"import json;"
             "bad=[n for n in ('homelab-expert-l1','homelab-expert-l2','blog-writer') "
             "if any(x.get('name')=='qa-suite-probe' for x in json.load(open('/root/.hermes/profiles/'+n+'/cron/jobs.json')).get('jobs',[]))];"
             "assert not bad, 'probe leaked into a live profile: %s' % bad\"")},
]

import os as _os

with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "cases.cron.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.cron:", len(CASES))
