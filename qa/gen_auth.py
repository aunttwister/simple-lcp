# QA case generator 1: the auth boundary.
#
# Two things must both be true, and they pull in opposite directions:
#   - every console page and every /api route is behind the admin gate;
#   - the proxy path POST /{profile}/chat/completions is NOT, because the agents
#     calling it hold no admin cookie.
#
# `curl -q` ignores ~/.curlrc, which is where the runner puts the admin cookie, so
# a `-q` case is genuinely anonymous.
import json
import os

CASES = []
G = "auth"


def code(path, extra=""):
    return ("[ \"$(curl -q -s -o /dev/null -w '{{HTTP}}' %s '{{BASE}}%s')\" = \"$EXPECT\" ]"
            % (extra, path))


CASES += [
    {"id": "au-001", "group": G, "title": "anonymous /profiles redirects to the login page",
     "cmd": "loc=$(curl -q -s -o /dev/null -w '%{redirect_url}' '{{BASE}}/profiles'); "
            "case \"$loc\" in */admin/login*) exit 0;; *) echo \"got $loc\"; exit 1;; esac"},
    {"id": "au-002", "group": G, "title": "anonymous /api/work/requests is refused, not served",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/work/requests'); "
            "[ \"$c\" = \"302\" ] || [ \"$c\" = \"401\" ] || [ \"$c\" = \"403\" ]"},
    {"id": "au-003", "group": G, "title": "anonymous /api/profiles is refused",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/profiles'); "
            "[ \"$c\" = \"302\" ] || [ \"$c\" = \"401\" ] || [ \"$c\" = \"403\" ]"},
    {"id": "au-004", "group": G, "title": "anonymous /api/evidence/export is refused",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/api/evidence/export'); "
            "[ \"$c\" = \"302\" ] || [ \"$c\" = \"401\" ] || [ \"$c\" = \"403\" ]"},
    {"id": "au-005", "group": G, "title": "proxy path stays public (no admin cookie)",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' "
            "-H 'Content-Type: application/json' "
            "-d '{\"model\":\"deepseek/deepseek-v4-flash\",\"messages\":[{\"role\":\"user\","
            "\"content\":\"say ok\"}],\"max_tokens\":4}'); "
            "[ \"$c\" = \"200\" ] || [ \"$c\" = \"201\" ]"},
    {"id": "au-006", "group": G,
     "title": "an unauthenticated unknown profile is gated, so profile names cannot be probed",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/nope/chat/completions' "
            "-H 'Content-Type: application/json' -d '{}'); [ \"$c\" = \"302\" ]"},
    {"id": "au-007", "group": G, "title": "a bad admin token does not mint a session",
     "cmd": "loc=$(curl -q -s -o /dev/null -w '%{redirect_url}' -d 'token=definitely-wrong&next=/' "
            "'{{BASE}}/admin/login'); "
            "case \"$loc\" in *admin/login*) exit 0;; *) echo \"got $loc\"; exit 1;; esac"},
    {"id": "au-008", "group": G, "title": "the admin cookie actually grants the console",
     "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/profiles')\" = \"200\" ]"},
    {"id": "au-009", "group": G, "title": "logout clears the session",
     "cmd": "curl -s -o /dev/null '{{BASE}}/admin/logout'; "
            "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' '{{BASE}}/profiles'); "
            "[ \"$c\" = \"302\" ]"},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.auth.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.auth:", len(CASES))
