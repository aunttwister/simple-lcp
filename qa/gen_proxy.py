# QA case generator 6: the proxy path — POST /{profile}/chat/completions.
#
# This is the one surface the agents hit, and it is deliberately outside the admin
# gate. Its failure modes matter more than any page: a 500 here is a broken agent,
# and a silently-served unknown profile would route traffic nowhere.
import json
import os

CASES = []
G = "proxy"

BODY = ('{"model":"deepseek/deepseek-v4-flash","messages":[{"role":"user",'
        '"content":"say ok"}],"max_tokens":8}')


def post(path, body, extra=""):
    return ("c=$(curl -q -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}%s' %s "
            "-H 'Content-Type: application/json' -d '%s'); echo \"$c\"" % (path, extra, body))


CASES += [
    {"id": "px-200", "group": G, "title": "a known profile answers 200",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' "
            "-H 'Content-Type: application/json' -d '%s'); "
            "[ \"$c\" = \"200\" ] || [ \"$c\" = \"201\" ]" % BODY},
    {"id": "px-json", "group": G, "title": "the reply is JSON with an OpenAI-shaped body",
     "cmd": "curl -q -s -X POST '{{BASE}}/l2/chat/completions' "
            "-H 'Content-Type: application/json' -d '%s' | python3 -c "
            "\"import sys,json; d=json.load(sys.stdin); "
            "assert 'choices' in d or 'error' in d, list(d)\"" % BODY},
    {"id": "px-unknown-profile", "group": G,
     "title": "an authenticated unknown profile is 400, not a silent default",
     "cmd": "c=$(curl -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/nope/chat/completions' "
            "-H 'Content-Type: application/json' -d '%s'); [ \"$c\" = \"400\" ]" % BODY},
    {"id": "px-unknown-profile-msg", "group": G, "title": "the 400 names the bad profile",
     "cmd": "curl -s -X POST '{{BASE}}/nope/chat/completions' "
            "-H 'Content-Type: application/json' -d '%s' | python3 -c "
            "\"import sys,json; d=json.load(sys.stdin); "
            "assert 'error' in d; assert 'nope' in d['error'], d['error']\"" % BODY},
    {"id": "px-unknown-anon", "group": G,
     "title": "anonymous unknown profile is 302, not a profile-enumeration oracle",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/nope/chat/completions' "
            "-H 'Content-Type: application/json' -d '%s'); [ \"$c\" = \"302\" ]" % BODY},
    {"id": "px-malformed", "group": G, "title": "a malformed body is 400, not 500",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' "
            "-H 'Content-Type: application/json' -d '{bad'); [ \"$c\" = \"400\" ]"},
    {"id": "px-unknown-model", "group": G, "title": "an unknown model never 5xxes (chain fallback)",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' "
            "-H 'Content-Type: application/json' -d "
            "'{\"model\":\"nope/nope\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],"
            "\"max_tokens\":4}'); "
            "[ \"$c\" != \"500\" ] && [ \"$c\" != \"000\" ]"},
    {"id": "px-content-type", "group": G, "title": "a text/plain body is not accepted as JSON",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' "
            "-H 'Content-Type: text/plain' -d '{}'); [ \"$c\" != \"200\" ]"},
    {"id": "px-stream", "group": G, "title": "stream=true returns SSE data frames",
     "cmd": "out=$(curl -q -s -N -X POST '{{BASE}}/l2/chat/completions' "
            "-H 'Content-Type: application/json' -d "
            "'{\"model\":\"deepseek/deepseek-v4-flash\",\"stream\":true,"
            "\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":6}'); "
            "echo \"$out\" | grep -q 'data:'"},
    {"id": "px-tools", "group": G, "title": "a tools payload is accepted",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l2/chat/completions' "
            "-H 'Content-Type: application/json' -d "
            "'{\"model\":\"deepseek/deepseek-v4-flash\",\"messages\":[{\"role\":\"user\","
            "\"content\":\"what is 2+2\"}],\"max_tokens\":8,\"tools\":[{\"type\":\"function\","
            "\"function\":{\"name\":\"add\",\"parameters\":{\"type\":\"object\",\"properties\":"
            "{\"a\":{\"type\":\"number\"},\"b\":{\"type\":\"number\"}}}}}]}'); "
            "[ \"$c\" = \"200\" ] || [ \"$c\" = \"201\" ]"},
    {"id": "px-other-profile", "group": G, "title": "a second profile routes too (l1)",
     "cmd": "c=$(curl -q -s -o /dev/null -w '{{HTTP}}' -X POST '{{BASE}}/l1/chat/completions' "
            "-H 'Content-Type: application/json' -d '%s'); "
            "[ \"$c\" = \"200\" ] || [ \"$c\" = \"201\" ]" % BODY},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.proxy.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.proxy:", len(CASES))
