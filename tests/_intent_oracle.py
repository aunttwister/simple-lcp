"""Test oracle: the OLD local classifier served behind the NEW lcp-v1 contract.

The local bge-small classifier is obsoleted from the decision path (CUR-327):
``router`` now classifies via a remote HTTP endpoint. The existing router tests
assert the semantic path's answers (planning vs debugging vs unit_tests, …) —
rather than rewriting 30 tests, this module stands the local classifier up
behind the wire contract as a mock remote endpoint, so the tests verify the
same outcomes through the path production now uses.

Not used in production: ``src/api`` never imports this.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ORACLE = {"srv": None, "base": None}


class _Oracle(BaseHTTPRequestHandler):
    """lcp-v1 endpoint backed by the real local SemanticClassifier."""

    def log_message(self, *a):  # silence the test log
        pass

    def do_POST(self):
        try:  # the suite imports src.api.*; fall back to the app's identity
            from src.api.task_classifier import get_semantic_classifier
        except ImportError:
            from api.task_classifier import get_semantic_classifier

        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        message = body.get("message", "")
        sent_labels = body.get("labels") or []
        clf = get_semantic_classifier(None)
        if clf is None or not message.strip():
            self._json({"label": None, "score": 0.0})
            return
        scores = clf.top_scores(message.strip(), 5)
        # Intersect with the label space the router sent, apply the classifier's
        # own gate so sub-gate messages fall through exactly as they used to.
        pairs = [(t, s) for t, s in scores if t in sent_labels and s >= clf.min_score]
        if not pairs:
            self._json({"label": None, "score": 0.0})
            return
        top, sc = pairs[0]
        self._json({"label": top, "score": round(sc, 4),
                    "scores": {t: round(s, 4) for t, s in pairs}})

    def _json(self, doc):
        out = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def oracle_base_url() -> str:
    """Start the oracle once per session; return its base URL."""
    if _ORACLE["srv"] is None:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _Oracle)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        _ORACLE["srv"] = srv
        _ORACLE["base"] = f"http://127.0.0.1:{srv.server_address[1]}/lcpv1"
    return _ORACLE["base"]


def oracle_min_score() -> float:
    """The classifier's own gate, so sub-gate messages fall through as before."""
    try:
        from src.api.task_classifier import SemanticClassifier
    except ImportError:
        from api.task_classifier import SemanticClassifier
    return SemanticClassifier.DEFAULT_MIN_SCORE


class OracleSettings:
    """Duck-typed settings store: intent model ON, pointing at the oracle."""

    INTENT_MODEL_URL_KEY = "intent_model_url"
    INTENT_MODEL_ENABLED_KEY = "intent_model_enabled"

    def get(self, key, default=None):
        return default

    def get_intent_model_enabled(self, profile=None, default=False):
        return True

    def get_intent_model_url(self, profile=None, default=None):
        return oracle_base_url()

    def get_intent_model_format(self, profile=None):
        return "lcp-v1"

    def get_intent_model_api_key(self, profile=None, default=None):
        return None

    def get_intent_model_timeout_ms(self, profile=None):
        return 10000

    def get_intent_model_min_score(self, profile=None):
        try:
            from src.api.task_classifier import SemanticClassifier
        except ImportError:
            from api.task_classifier import SemanticClassifier
        return SemanticClassifier.DEFAULT_MIN_SCORE
