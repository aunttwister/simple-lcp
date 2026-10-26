# QA case generator 7: resilience — idempotency, clamps, negatives, concurrency.
#
# A router's failure modes are worse than its slow paths: a 500 on a log endpoint
# takes the console down, and a 500 on the proxy takes an agent down. These cases
# assert "never 5xx" on garbage input rather than "returns exactly X", because the
# contract is that bad input degrades, it does not crash.
import json
import os

CASES = []
G = "resilience"

IDEMPOTENT = ["/api/profiles", "/api/models/registry", "/api/providers",
              "/api/work/requests?per=5", "/api/work/provider-decisions?per=5",
              "/api/conversations?per=5", "/api/routing/status"]
for ep in IDEMPOTENT:
    CASES.append({
        "id": "rs-idem-" + ep.strip("/").replace("/", "-").replace("?", "-").replace("=", "-")[:40],
        "group": G, "title": "idempotent %s" % ep[:48],
        "cmd": ("a=$(curl -s '{{BASE}}%s' | md5sum); b=$(curl -s '{{BASE}}%s' | md5sum); "
                "[ \"$a\" = \"$b\" ]" % (ep, ep)),
    })

CLAMPS = [
    ("rs-clamp-per0", "/api/work/requests?per=0&page=0", "per=0/page=0 clamps (not 500)"),
    ("rs-clamp-negative", "/api/work/requests?per=-3&page=-2", "negative pagination clamps (not 500)"),
    ("rs-clamp-huge", "/api/work/requests?per=99999&page=99999", "huge per/page clamps (not 500)"),
    ("rs-clamp-alpha", "/api/work/requests?per=abc&page=xyz", "non-numeric pagination degrades (not 500)"),
    ("rs-clamp-conv-per0", "/api/conversations?per=0&page=0", "conversations per=0 clamps (not 500)"),
    ("rs-clamp-conv-neg", "/api/conversations?per=-3&page=-2", "conversations negative clamps (not 500)"),
    ("rs-clamp-dec", "/api/work/provider-decisions?per=-1&page=-9", "decisions negative clamps (not 500)"),
]
for cid, path, title in CLAMPS:
    CASES.append({
        "id": cid, "group": G, "title": title,
        "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s')\" != \"500\" ]" % path,
    })

# ── routing / traversal: never a 200, never a stack ──
CASES += [
    {"id": "rs-404", "group": G, "title": "an unknown route is 404",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/definitely-not-a-route')\" = \"404\" ]"},
    {"id": "rs-traversal", "group": G, "title": "a traversal path is not served",
     "cmd": "[ \"$(curl -q -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/..%%2fetc%%2fpasswd')\" != \"200\" ]"},
    {"id": "rs-static-traversal", "group": G, "title": "/static does not escape its root",
     "cmd": ("curl -q -s --path-as-is '{{BASE}}/static/../../etc/passwd' "
             "| grep -q 'root:x:' && exit 1; exit 0")},
    {"id": "rs-api-json-errors", "group": G,
     "title": "API errors are JSON, not HTML",
     "cmd": ("curl -s '{{BASE}}/api/conversations/messages' | python3 -c "
             "\"import sys,json; d=json.load(sys.stdin); assert 'error' in d\"")},
    {"id": "rs-evidence-key", "group": G, "title": "the evidence public key is a real PEM",
     "cmd": ("curl -s '{{BASE}}/api/evidence/public-key' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); assert d['public_key_pem'].startswith('-----BEGIN PUBLIC KEY-----'); "
             "assert d['fingerprint'].startswith('sha256:')\"")},
]

# ── concurrency: five simultaneous reads all succeed ──
# NB: `grep -L` exits 0 when every file MATCHES, so `grep -L ... && exit 1` fails a
# passing run. Check each file explicitly instead.
CASES.append({
    "id": "rs-parallel", "group": G, "title": "five parallel requests all succeed",
    "cmd": ("rm -f /tmp/rs_p_*; "
            "for i in 1 2 3 4 5; do "
            "( curl -s -o /dev/null -w '%{http_code}' '{{BASE}}/api/work/requests?per=10' "
            "> /tmp/rs_p_$i ) & done; wait; "
            "for f in /tmp/rs_p_*; do "
            "[ \"$(cat \"$f\")\" = \"200\" ] || { echo \"bad $f=$(cat \"$f\")\"; exit 1; }; "
            "done; echo ok"),
})

# ── the console survives a slow-ish burst without 5xx ──
CASES.append({
    "id": "rs-parallel-mixed", "group": G, "title": "mixed parallel reads never 5xx",
    "cmd": ("rm -f /tmp/rs_m_*; "
            "for i in 1 2 3; do "
            "( curl -s -o /dev/null -w '%{http_code}' '{{BASE}}/api/conversations?per=20' "
            "> /tmp/rs_m_c$i ) & "
            "( curl -s -o /dev/null -w '%{http_code}' '{{BASE}}/api/usage/stats' "
            "> /tmp/rs_m_u$i ) & "
            "( curl -s -o /dev/null -w '%{http_code}' '{{BASE}}/api/routing/status' "
            "> /tmp/rs_m_r$i ) & done; wait; "
            "for f in /tmp/rs_m_*; do "
            "case \"$(cat \"$f\")\" in 5*) echo \"5xx on $f\"; exit 1;; esac; "
            "done; echo ok"),
})

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.resilience.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.resilience:", len(CASES))
