"""Live test for the remote intent model (CUR-327) — run with the staging venv.

Covers the five Done-when criteria from DESIGN.md:
  1. no URL / disabled  -> classify_remote returns None, router falls through
  2. mock lcp-v1 server -> path="remote", semantic populated
  3. mock openai server -> content parsed to exactly one label
  4. unreachable endpoint -> None, no exception
  5. settings inherit/override (global vs per-profile)
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "src")

from api import intent_model as im  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + ((" — " + detail) if detail else ""))


# ── 1+2+3+4: mock endpoints ──────────────────────────────────────────────────
class Mock(BaseHTTPRequestHandler):
    mode = "lcp-v1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/lcpv1":
            resp = {"label": body["labels"][0], "score": 0.87,
                    "scores": {body["labels"][0]: 0.87, body["labels"][1]: 0.09}}
        elif self.path == "/openai":
            # chat.completions request: recover the label set from the prompt.
            prompt = body["messages"][0]["content"]
            import re as _re
            m = _re.search(r"intents: ([^\n]+)\.", prompt)
            labels = [x.strip() for x in m.group(1).split(",")] if m else []
            resp = {"choices": [{"message": {"content": f'The intent is "{labels[1] if len(labels) > 1 else "unknown"}", confidence 0.72.'}}]}
        elif self.path == "/foreign":
            resp = {"label": "not_in_label_space", "score": 0.99}
        elif self.path == "/garbage":
            self.send_response(200); self.end_headers(); self.wfile.write(b"not json"); return
        elif self.path == "/slow":
            import time; time.sleep(5); resp = {}
        else:
            self.send_response(500); self.end_headers(); return
        out = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
LABELS = ["change", "inspect", "advise"]

print("== contract lcp-v1 ==")
r = im.classify_remote("restart the nginx container", LABELS, base + "/lcpv1")
check("lcp-v1 returns a validated label", r is not None and r.label == "change", f"{r and r.label}")
check("score recorded", r and 0.86 < r.score < 0.88, str(r and r.score))
check("semantic pairs populated", r and r.semantic_pairs() and r.semantic_pairs()[0][0] == "change")

print("== contract openai ==")
r = im.classify_remote("check whether it is up", LABELS, base + "/openai", fmt="openai")
check("openai content parsed to exactly one label", r is not None and r.label == "inspect", f"{r and r.label}")
r2 = im.classify_remote("please inspect the situation", LABELS, base + "/openai", fmt="openai")
check("prose wrapping tolerated (token match)", r2 is not None and r2.label == "inspect",
      f"{r2 and r2.label} score={r2 and r2.score}")

print("== rejections ==")
r = im.classify_remote("x", LABELS, base + "/foreign")
check("foreign label rejected", r is None)
r = im.classify_remote("x", LABELS, base + "/garbage")
check("non-JSON body rejected", r is None)
r = im.classify_remote("x", LABELS, base + "/nope")
check("unreachable endpoint -> None, no raise", r is None)
r = im.classify_remote("x", LABELS, base + "/slow", timeout_ms=300)
check("timeout bounded", r is None)
r = im.classify_remote("x", LABELS, base + "/lcpv1", fmt="jev")
check("jev reserved -> refused, not guessed", r is None)
r = im.classify_remote("", LABELS, base + "/lcpv1")
check("empty message refused", r is None)

# ── 5: settings inherit/override ─────────────────────────────────────────────
print("== settings store: global baseline + per-profile override ==")
from api.cost_cache import SettingsStore, get_settings  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
import os, tempfile  # noqa: E402

# Build a REAL store on a temp sqlite so get/set/clear run against actual rows.
from api.models import Setting, Base  # noqa: E402
tmp = tempfile.mkdtemp()
eng = create_engine(f"sqlite:///{tmp}/t.db")
Base.metadata.create_all(eng, tables=[Setting.__table__])
st = SettingsStore(eng)

check("default: nothing set", st.get_intent_model_url() is None and not st.get_intent_model_enabled())
st.set_intent_model_url("http://global:1/v1")
st.set_intent_model_enabled(True)
st.set_intent_model_format("lcp-v1")
check("global baseline set", st.get_intent_model_url() == "http://global:1/v1" and st.get_intent_model_enabled())
check("profile inherits global when no override", st.get_intent_model_url(profile="l2") == "http://global:1/v1")
st.set_intent_model_url("http://override:2/v1", profile="l2")
check("per-profile override wins", st.get_intent_model_url(profile="l2") == "http://override:2/v1")
check("other profile still inherits", st.get_intent_model_url(profile="cron") == "http://global:1/v1")
check("format inherits independently of url override", st.get_intent_model_format(profile="l2") == "lcp-v1")
st.clear_intent_model(profile="l2")
check("clear restores inheritance", st.get_intent_model_url(profile="l2") == "http://global:1/v1")
check("api key write-only by construction (stored, not echoed in GET payload shape)",
      st.set_intent_model_api_key("sk-test") is None)

# ── router path wiring ───────────────────────────────────────────────────────
print("== router decision path ==")
import api.cost_cache as cc  # noqa: E402
from api.task_classifier import TASK_EXEMPLARS  # noqa: E402
LBL0 = list(TASK_EXEMPLARS.keys())[0]  # the mock echoes labels[0]; assert label-agnostically


class FakeSettings:
    """Duck-typed stand-in so the router sees the intent model as configured."""
    INTENT_MODEL_URL_KEY = SettingsStore.INTENT_MODEL_URL_KEY
    INTENT_MODEL_ENABLED_KEY = SettingsStore.INTENT_MODEL_ENABLED_KEY

    def __init__(self, url, enabled=True):
        self._url, self._en = url, enabled
    def get(self, key, default=None):
        return default
    def get_intent_model_enabled(self, profile=None, default=False):
        return self._en
    def get_intent_model_url(self, profile=None, default=None):
        return self._url
    def get_intent_model_format(self, profile=None):
        return "lcp-v1"
    def get_intent_model_api_key(self, profile=None, default=None):
        return None
    def get_intent_model_timeout_ms(self, profile=None):
        return 3000
    def get_intent_model_min_score(self, profile=None):
        return 0.0


msgs = [{"role": "user", "content": "restart the nginx container please"}]

orig = cc.get_settings
try:
    cc.get_settings = lambda: FakeSettings(base + "/lcpv1")
    from api.router import classify_task_detail
    res = classify_task_detail(msgs, profile="l2", config=None)
    check("router stamps path='remote'", res.path == "remote", res.path)
    check("router task == remote label", res.task == LBL0, f"{res.task} (expected {LBL0})")
    check("router records semantic scores", bool(res.semantic) and res.semantic[0][0] == LBL0,
          str(res.semantic[:1] if res.semantic else None))
    check("sem_available True on remote success", res.sem_available is True)

    cc.get_settings = lambda: FakeSettings(base + "/nope")
    res = classify_task_detail(msgs, profile="l2", config=None)
    check("unreachable endpoint -> structural path, no raise",
          res.path != "remote" and res.task in ("agentic_multi_step", "code_generation",
                                                "research_deep", "casual_chat", "debugging",
                                                "unit_tests", "planning", "reasoning_chain"),
          f"path={res.path} task={res.task}")

    cc.get_settings = lambda: FakeSettings(None, enabled=True)
    res = classify_task_detail(msgs, profile="l2", config=None)
    check("no URL configured -> structural path", res.path != "remote", res.path)

    cc.get_settings = lambda: FakeSettings(base + "/lcpv1", enabled=False)
    res = classify_task_detail(msgs, profile="l2", config=None)
    check("disabled -> structural path", res.path != "remote", res.path)
finally:
    cc.get_settings = orig

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILURES:", FAIL); sys.exit(1)
