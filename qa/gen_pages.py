# QA case generator 1: pages, nav groups, core API endpoints.
import json
import os

CASES = []

# ── Pages: every route answers 200 with expected markers ──
PAGES = [
    ("pg-001", "Dashboard", "/dashboard", ["Monitor", "Providers"]),
    ("pg-002", "Usage page", "/usage", ["Usage"]),
    ("pg-003", "Logs unified", "/logs", ["Conversations", "Requests", "Provider decisions", "Board decisions"]),
    ("pg-004", "Logs conversations tab", "/logs?view=conversations", ["tab-btn"]),
    ("pg-005", "Logs requests tab", "/logs?view=requests", ["tab-btn"]),
    ("pg-006", "Logs providers tab", "/logs?view=providers", ["tab-btn"]),
    ("pg-007", "Logs decisions tab", "/logs?view=decisions", ["tab-btn"]),
    ("pg-008", "Alerts page", "/alerts", ["Alerts"]),
    ("pg-009", "Providers page", "/providers", ["Providers"]),
    ("pg-010", "Models page", "/models", ["Models"]),
    ("pg-011", "Profiles page", "/profiles", ["Profiles"]),
    ("pg-012", "API Keys page", "/keys", ["API Keys"]),
    ("pg-013", "Tasks page", "/work/tasks", ["task-search", "task-rows"]),
    ("pg-014", "Fleet page", "/work/fleet", ["Fleet"]),
    ("pg-015", "Cron page", "/work/cron", ["cron-new-btn", "work-chip"]),
    ("pg-016", "Work config page", "/work/config", ["sources-form", "Save sources"]),
    ("pg-017", "Setup page", "/setup", ["Setup"]),
]
for cid, title, path, markers in PAGES:
    m = " && ".join("out=$(curl -s '{{BASE}}%s') && echo \"$out\" | grep -q '%s'" % (path, mk)
                    for mk in markers)
    CASES.append({
        "id": cid, "group": "pages", "title": title,
        "cmd": "[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s')\" = \"200\" ] && %s" % (path, m),
    })

# ── Nav group order: Work, Gateway, Monitor, Admin in sequence ──
CASES.append({
    "id": "pg-018", "group": "pages", "title": "Nav groups ordered Work/Gateway/Monitor/Admin",
    "cmd": ('curl -s "{{BASE}}/work/tasks" | python3 -c '
            '"import sys; h=sys.stdin.read(); '
            'w=h.find(\'nav-label\\\">Work</div>\'); g=h.find(\'nav-label\\\">Gateway</div>\'); '
            'm=h.find(\'nav-label\\\">Monitor</div>\'); a=h.find(\'nav-label\\\">Admin</div>\'); '
            'assert -1 < w < g < m < a, (w,g,m,a)"'),
})

# ── Core API endpoints 200 + sane JSON (real routes) ──
API = [
    ("api-001", "/api/setup", None),
    ("api-002", "/api/daily-costs", None),
    ("api-003", "/api/providers", None),
    ("api-004", "/api/models/registry", None),
    ("api-005", "/api/profiles", None),
    ("api-006", "/api/work/tasks?lite=1&per=5", "tasks"),
    ("api-007", "/api/work/tasks/detail?task=in_progress%2Flcp-pentest-fixes", None),
    ("api-008", "/api/work/tasks?states=all&per=5", "total"),
    ("api-009", "/api/work/conversations?per=5", "conversations"),
    ("api-010", "/api/work/cron", "counts"),
    ("api-011", "/api/work/cron/ops", "pending"),
    ("api-012", "/api/work/sources", "profiles"),
    ("api-013", "/api/work/fleet", "summary"),
    ("api-014", "/api/work/status", "available"),
]
for cid, path, key in API:
    suffix = ""
    if key:
        suffix = (" && curl -s '{{BASE}}%s' | python3 -c \"import sys,json; d=json.load(sys.stdin); "
                  "assert d.get('%s') is not None, 'missing key %s'\"" % (path, key, key))
    if path.startswith("/api/work/tasks/detail"):
        # a stale hardcoded key must NEVER 5xx — 200 (exists) or 404 (moved)
        suffix = (" ; code=$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s'); "
                  "[ \"$code\" = \"200\" ] || [ \"$code\" = \"404\" ]" % path)
    CASES.append({
        "id": cid, "group": "api", "title": path,
        "cmd": ("[ \"$(curl -s -o /dev/null -w '{{HTTP}}' '{{BASE}}%s')\" = \"200\" ]%s"
                % (path, suffix)),
    })

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.pages.json"), "w") as fh:
    json.dump(CASES, fh, indent=1)
print("cases.pages:", len(CASES))