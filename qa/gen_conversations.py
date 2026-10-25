# QA case generator 4: conversations — list, transcript, audit.
#
# These are the cases that check the store tells the truth. A conversation row is a
# rollup of its messages, so the three counts on it must equal what the transcript
# actually contains; and the transcript's own `truncated`/`total` must not claim a
# window it did not deliver.
import json
import os

CASES = []
G = "conv"

CONV = "/api/conversations"
MSGS = "/api/conversations/messages"
AUDIT = "/api/conversations/audit"

CASES += [
    {"id": "cv-shape", "group": G, "title": "list carries both stores: live and history",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert {'live','history'} <= set(d); "
             "assert {'count','profiles','providers','items'} <= set(d['live']); "
             "assert d['history']['available'] is True\"" % CONV)},
    {"id": "cv-total", "group": G, "title": "history reports a real total",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "h=d['history']; assert h['total']>0; assert len(h['conversations'])<=int(h['filter']['per'])\""
             % CONV)},
    {"id": "cv-newest", "group": G, "title": "default order is newest activity first",
     "cmd": ("curl -s '{{BASE}}%s?per=20' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "ls=[c['last_at'] or '' for c in d['history']['conversations']]; "
             "assert ls==sorted(ls, reverse=True), ls[:4]\"" % CONV)},
    {"id": "cv-pages", "group": G, "title": "page count matches the total",
     "cmd": ("curl -s '{{BASE}}%s?per=20' | python3 -c \"import sys,json,math; "
             "d=json.load(sys.stdin); f=d['history']['filter']; "
             "assert f['pages']==max(1,math.ceil(f['total']/20)), (f['pages'],f['total'])\""
             % CONV)},
    {"id": "cv-nonoverlap", "group": G, "title": "consecutive pages do not overlap",
     "cmd": ("curl -s '{{BASE}}%s?per=5&page=1' > /tmp/cv1.json; "
             "curl -s '{{BASE}}%s?per=5&page=2' > /tmp/cv2.json; "
             "python3 -c \"import json; a=[c['id'] for c in json.load(open('/tmp/cv1.json'))['history']['conversations']]; "
             "b=[c['id'] for c in json.load(open('/tmp/cv2.json'))['history']['conversations']]; "
             "assert not (set(a)&set(b)), set(a)&set(b)\"" % (CONV, CONV))},
    {"id": "cv-sort-oldest", "group": G, "title": "sort=oldest reverses the order",
     "cmd": ("curl -s '{{BASE}}%s?per=10&sort=oldest' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); h=d['history']; ls=[c['last_at'] or '' for c in h['conversations']]; "
             "assert ls==sorted(ls), ls[:4]; assert h['filter']['sort']=='oldest'\"" % CONV)},
    {"id": "cv-sort-messages", "group": G, "title": "sort=messages is monotonic descending",
     "cmd": ("curl -s '{{BASE}}%s?per=10&sort=messages' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); m=[c['messages'] or 0 for c in d['history']['conversations']]; "
             "assert m==sorted(m, reverse=True), m\"" % CONV)},
    {"id": "cv-sort-calls", "group": G, "title": "sort=calls is monotonic descending",
     "cmd": ("curl -s '{{BASE}}%s?per=10&sort=calls' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); c=[x['calls'] or 0 for x in d['history']['conversations']]; "
             "assert c==sorted(c, reverse=True), c\"" % CONV)},
    {"id": "cv-sorts-advertised", "group": G, "title": "the four sorts are advertised to the client",
     "cmd": ("curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "keys=[s['key'] for s in d['history']['filter']['sorts']]; "
             "assert keys==['newest','oldest','messages','calls'], keys\"" % CONV)},
    {"id": "cv-filter-profile", "group": G, "title": "profile filter narrows and echoes back",
     "cmd": ("curl -s '{{BASE}}%s' > /tmp/cva.json; "
             "curl -s '{{BASE}}%s?profile=l2' > /tmp/cvb.json; "
             "python3 -c \"import json; a=json.load(open('/tmp/cva.json'))['history']; "
             "b=json.load(open('/tmp/cvb.json'))['history']; "
             "assert b['total']<a['total'], (b['total'],a['total']); "
             "assert b['filter']['profile']=='l2'; "
             "assert all(c['profile']=='l2' for c in b['conversations'])\"" % (CONV, CONV))},
    {"id": "cv-filter-q", "group": G, "title": "search by id narrows to one conversation",
     "cmd": ("cid=$(curl -s '{{BASE}}%s?per=1' | python3 -c 'import sys,json; "
             "print(json.load(sys.stdin)[\"history\"][\"conversations\"][0][\"id\"])'); "
             "curl -s \"{{BASE}}%s?q=$cid\" | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "h=d['history']; assert h['total']==1, h['total']; "
             "assert h['conversations'][0]['id']=='$cid'\"" % (CONV, CONV))},
    {"id": "cv-filter-q-none", "group": G, "title": "an impossible search returns zero, not an error",
     "cmd": ("curl -s '{{BASE}}%s?q=zzzz-no-such-conversation-zzzz' | python3 -c "
             "\"import sys,json; d=json.load(sys.stdin); h=d['history']; "
             "assert h['total']==0; assert h['conversations']==[]\"" % CONV)},
    {"id": "cv-clamp", "group": G, "title": "page past the end clamps instead of 500ing",
     "cmd": ("curl -s '{{BASE}}%s?per=5&page=99999' | python3 -c \"import sys,json; "
             "d=json.load(sys.stdin); f=d['history']['filter']; assert f['page']==f['pages'], f\""
             % CONV)},
    {"id": "cv-idem", "group": G, "title": "the list is idempotent",
     "cmd": ("a=$(curl -s '{{BASE}}%s?per=5' | md5sum); "
             "b=$(curl -s '{{BASE}}%s?per=5' | md5sum); [ \"$a\" = \"$b\" ]" % (CONV, CONV))},
]

# ── transcript ──
CASES += [
    {"id": "cv-msg-400", "group": G, "title": "transcript without cid is 400 JSON",
     "cmd": ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s')\" = \"400\" ] && "
             "curl -s '{{BASE}}%s' | python3 -c \"import sys,json; assert 'error' in json.load(sys.stdin)\""
             % (MSGS, MSGS))},
    {"id": "cv-msg-shape", "group": G, "title": "transcript reports its own window honestly",
     "cmd": ("cid=$(curl -s '{{BASE}}%s?per=1' | python3 -c 'import sys,json; "
             "print(json.load(sys.stdin)[\"history\"][\"conversations\"][0][\"id\"])'); "
             "curl -s \"{{BASE}}%s?cid=$cid\" | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert {'id','messages','truncated','total','newest'} <= set(d); "
             "assert d['id']=='$cid'; "
             "assert d['truncated'] == (d['total'] > len(d['messages'])), (d['truncated'], d['total'], len(d['messages']))\""
             % (CONV, MSGS))},
    {"id": "cv-msg-order", "group": G, "title": "transcript messages are in stored (seq) order",
     "cmd": ("cid=$(curl -s '{{BASE}}%s?per=1' | python3 -c 'import sys,json; "
             "print(json.load(sys.stdin)[\"history\"][\"conversations\"][0][\"id\"])'); "
             "curl -s \"{{BASE}}%s?cid=$cid\" | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "sq=[m['seq'] for m in d['messages']]; "
             "assert sq==sorted(sq), 'transcript is not in append order'; "
             "assert len(set(sq))==len(sq), 'duplicate seq'\""
             % (CONV, MSGS))},
    {"id": "cv-msg-unknown", "group": G, "title": "an unknown cid returns an empty transcript, not a 500",
     "cmd": ("c=$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s?cid=nope'); "
             "[ \"$c\" = \"200\" ] && curl -s '{{BASE}}%s?cid=nope' | python3 -c "
             "\"import sys,json; d=json.load(sys.stdin); assert d['messages']==[]; assert d['total']==0\""
             % (MSGS, MSGS))},
    {"id": "cv-msg-traversal", "group": G, "title": "a traversal-shaped cid does not read the filesystem",
     "cmd": ("c=$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s?cid=../../etc/passwd'); "
             "[ \"$c\" != \"500\" ] && curl -s '{{BASE}}%s?cid=../../etc/passwd' | grep -qv 'root:x:'"
             % (MSGS, MSGS))},
]

# ── audit ──
CASES += [
    {"id": "cv-audit-400", "group": G, "title": "audit without cid is 400 JSON",
     "cmd": ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s')\" = \"400\" ] && "
             "curl -s '{{BASE}}%s' | python3 -c \"import sys,json; assert 'error' in json.load(sys.stdin)\""
             % (AUDIT, AUDIT))},
    {"id": "cv-audit-shape", "group": G, "title": "audit reports availability and the rollup",
     "cmd": ("cid=$(curl -s '{{BASE}}%s?per=1' | python3 -c 'import sys,json; "
             "print(json.load(sys.stdin)[\"history\"][\"conversations\"][0][\"id\"])'); "
             "curl -s \"{{BASE}}%s?cid=$cid\" | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "assert d.get('available') is True; assert d['id']=='$cid'; "
             "assert 'tools' in d and 'first_ask' in d\""
             % (CONV, AUDIT))},
    {"id": "cv-audit-consistency", "group": G,
     "title": "audit message count equals the transcript total",
     "cmd": ("cid=$(curl -s '{{BASE}}%s?per=1' | python3 -c 'import sys,json; "
             "print(json.load(sys.stdin)[\"history\"][\"conversations\"][0][\"id\"])'); "
             "curl -s \"{{BASE}}%s?cid=$cid\" > /tmp/cvm.json; "
             "curl -s \"{{BASE}}%s?cid=$cid\" > /tmp/cva2.json; "
             "python3 -c \"import json; m=json.load(open('/tmp/cvm.json')); "
             "a=json.load(open('/tmp/cva2.json')); "
             "assert a['messages']==m['total'], (a['messages'], m['total'])\""
             % (CONV, MSGS, AUDIT))},
    {"id": "cv-audit-toolcount", "group": G,
     "title": "audit tool counts sum to the reported tool_calls",
     "cmd": ("cid=$(curl -s '{{BASE}}%s?per=1' | python3 -c 'import sys,json; "
             "print(json.load(sys.stdin)[\"history\"][\"conversations\"][0][\"id\"])'); "
             "curl -s \"{{BASE}}%s?cid=$cid\" | python3 -c \"import sys,json; d=json.load(sys.stdin); "
             "s=sum(t['count'] for t in d.get('tools') or []); "
             "assert s==(d.get('tool_calls') or 0), (s, d.get('tool_calls'))\""
             % (CONV, AUDIT))},
    {"id": "cv-audit-unknown", "group": G, "title": "an unknown cid reports unavailable, not a 500",
     "cmd": ("c=$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s?cid=nope'); "
             "[ \"$c\" = \"200\" ] && curl -s '{{BASE}}%s?cid=nope' | python3 -c "
             "\"import sys,json; d=json.load(sys.stdin); assert d.get('available') is False\""
             % (AUDIT, AUDIT))},
]

# ── the page itself ──
CASES += [
    {"id": "cv-page", "group": G, "title": "the page renders live + history with a caption",
     "cmd": ("curl -s '{{BASE}}/conversations' | python3 -c \"import sys,re; h=sys.stdin.read(); "
             "assert 'In flight' in h; assert 'History' in h; "
             "assert len(re.findall(r'<caption', h))>=2, 'a table is uncaptioned'\"" )},
    {"id": "cv-page-scope", "group": G, "title": "every conversation header carries scope=col",
     "cmd": ("curl -s '{{BASE}}/conversations' | python3 -c \"import sys,re; h=sys.stdin.read(); "
             "ths=re.findall(r'<th[\\s>](?![^>]*scope)', h); assert not ths, len(ths)\"")},
]

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.conv.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.conv:", len(CASES))
