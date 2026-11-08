"""CUR-329 — in-flight provider fix, background titler, settings page.

Three behaviours land together and each can rot without raising:

1. **the in-flight entry lying** — a chain that falls back used to keep the
   PRIMARY step's provider on the live table for the whole call, so the page
   showed one provider while another was being billed;
2. **the titler failing loudly or doubly** — it runs out of band against a
   live provider, so a raise, a non-200, or a repeated call for an already
   titled conversation are all silent-wrong states worth pinning;
3. **the settings surface** — the toggle/picker page, its API, and the
   conversations-page hint that explains the "Untitled" rows.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.api import conv_store
from src.api import conv_titler as ct
from src.api import inflight as fl


@pytest.fixture(autouse=True)
def _clean_registry():
    fl.clear()
    yield
    fl.clear()


@pytest.fixture(autouse=True)
def _reset_titler():
    """The titler's queue and worker are process-global; tests start clean.

    ``_run_worker`` is stubbed so an ``enqueue`` in one test cannot start a
    real loop that races the assertions with live upstream calls.
    """
    with patch.object(ct, "_run_worker", lambda: None):
        ct.reset_for_tests()
        yield
    ct.reset_for_tests()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("LCP_COSTS_DB", str(tmp_path / "costs.db"))
    conv_store.ensure_store()
    return tmp_path / "costs.db"


@pytest.fixture
def conv(store):
    """A conversation with a transcript long enough to be a titling candidate."""
    conv_store.capture_request(
        "sess-t", {"messages": [
            {"role": "user", "content": "audit the spark thermals"},
            {"role": "assistant", "content": "on it"},
            {"role": "user", "content": "also check the l2 budget"},
            {"role": "assistant", "content": "checked, 40% used"},
        ]},
        reply="done", profile="l2", model="m1",
        ts="2026-11-07T10:00:00+00:00")
    conv_store.upsert_conversation("sess-t", profile="l2", model="m1",
                                   source="live")
    conv_store.recount_conversation("sess-t")
    return "sess-t"


def _config(enabled=True, model=None, chain_head="head-model"):
    cfg = MagicMock()
    cfg.conversation_titling = {"enabled": enabled, "model": model}
    cfg.server = {"default_profile": "l2", "port": 8734}
    cfg.profiles = {"l2": {"chain": [
        {"provider": "prov", "model": chain_head, "base_url": "https://p/v1"}]}}
    cfg.providers = {"prov": {"api_base": "https://p/v1"}}
    return cfg


# ── 1. the in-flight registry: update() ──────────────────────────────────────

def test_update_moves_the_entry_to_the_step_on_the_wire():
    tok = fl.begin("l1", "head-model", "local-sparks")
    fl.update(tok, provider="deepseek", model="deepseek-v4-pro")
    row = fl.snapshot()[0]
    assert row["provider"] == "deepseek"
    assert row["model"] == "deepseek-v4-pro"


def test_update_keeps_fields_that_arrive_empty():
    """A partial announcement must not blank the other field."""
    tok = fl.begin("l1", "head-model", "local-sparks")
    fl.update(tok, provider="deepseek", model=None)
    row = fl.snapshot()[0]
    assert row["provider"] == "deepseek"
    assert row["model"] == "head-model"


def test_update_tolerates_none_and_unknown_tokens():
    """The chain calls this on every attempt; it must never be the thing that
    fails one."""
    fl.update(None, provider="p")
    fl.update("not-a-token", provider="p")
    assert fl.count() == 0


def test_update_cannot_resurrect_an_ended_entry():
    tok = fl.begin("l1", "m", "p")
    fl.end(tok)
    fl.update(tok, provider="other")
    assert fl.count() == 0


# ── 2. try_chain announces each attempted step ───────────────────────────────

@pytest.fixture
def chain_config():
    cfg = MagicMock()
    cfg.plugins = {}
    cfg.circuit_breaker = {
        "failures_dead": 5, "dead_cooldown_seconds": 300,
        "failures_degraded": 3, "degraded_cooldown_seconds": 60,
    }
    cfg.profiles = {"l2": {"chain": [
        {"provider": "first", "base_url": "https://first.com/v1", "model": "m1"},
        {"provider": "second", "base_url": "https://second.com/v1", "model": "m2"},
    ], "forbidden_tools": []}}
    cfg.providers = {
        "first": {"api_key_env": "KEY", "base_url": "https://first.com/v1"},
        "second": {"api_key_env": "KEY", "base_url": "https://second.com/v1"},
    }
    cfg.model_limits = {}
    cfg.get_provider_key.return_value = None
    cfg.retry = {}
    from src.api.circuit_breaker import get_circuit_breaker
    get_circuit_breaker(cfg)
    return cfg


def _body():
    return {"messages": [{"role": "user", "content": "hi"}]}


def test_on_step_fires_for_each_wire_attempt(chain_config):
    """First the primary, then — after it fails — the fallback step."""
    from src.api.exceptions import ProviderInternalError
    from src.api.request_pipeline import try_chain
    seen = []

    def fwd(step_cfg, body, config, session_id=None):
        if step_cfg["provider"] == "first":
            raise ProviderInternalError("boom")  # transient → chain falls through
        return {"ok": True}, 200

    with patch("src.api.request_pipeline.forward_request", side_effect=fwd):
        try_chain("l2", chain_config.profiles["l2"], _body(), chain_config,
                  on_step=lambda p, m: seen.append((p, m)))
    assert seen == [("first", "m1"), ("second", "m2")]


def test_on_step_does_not_fire_for_skipped_steps(chain_config):
    """A step the breaker refuses never reaches the wire, so announcing it
    would put a lie back on the live table."""
    from src.api.circuit_breaker import get_circuit_breaker
    from src.api.request_pipeline import try_chain
    cb = get_circuit_breaker(chain_config)
    cb.record_failure("first", "https://first.com/v1", "l2")
    cb.record_failure("first", "https://first.com/v1", "l2")
    cb.record_failure("first", "https://first.com/v1", "l2")
    cb.record_failure("first", "https://first.com/v1", "l2")
    cb.record_failure("first", "https://first.com/v1", "l2")
    seen = []
    with patch("src.api.request_pipeline.forward_request",
               return_value=({"ok": True}, 200)):
        try_chain("l2", chain_config.profiles["l2"], _body(), chain_config,
                  on_step=lambda p, m: seen.append((p, m)))
    assert seen == [("second", "m2")]


def test_a_raising_on_step_never_breaks_the_chain(chain_config):
    """The registry is an observation aid; a broken aid must not 502 the call
    it observes."""
    from src.api.request_pipeline import try_chain
    with patch("src.api.request_pipeline.forward_request",
               return_value=({"ok": True}, 200)):
        body, status, *_ = try_chain(
            "l2", chain_config.profiles["l2"], _body(), chain_config,
            on_step=lambda p, m: (_ for _ in ()).throw(RuntimeError("nope")))
    assert status == 200


def test_the_chat_handler_wires_on_step_and_enqueues_the_titler():
    """Structural, like the registry's cleanup guard: driving a real request
    would need a live provider, and the property is that the handler PASSES
    the pieces."""
    path = "src/server/handler.py"
    src = open(path, encoding="utf-8").read()
    assert "on_step=_note_inflight_step" in src, \
        "the chain is not moving the in-flight entry"
    assert src.count("conv_titler.enqueue(conversation_id") >= 2, \
        "the titler is not fed from both capture sites (stream + buffered)"


# ── 3. the titler: queue ─────────────────────────────────────────────────────

def test_enqueue_dedups_and_counts(conv):
    ct.enqueue("sess-t", config=_config())
    ct.enqueue("sess-t", config=_config())
    assert ct.pending_count() == 1


def test_enqueue_ignores_none_and_empty(conv):
    ct.enqueue(None, config=_config())
    ct.enqueue("", config=_config())
    assert ct.pending_count() == 0


def test_enqueue_drops_ids_when_disabled(conv):
    ct.enqueue("sess-t", config=_config(enabled=False))
    assert ct.pending_count() == 0


def test_enabled_flag_fails_open(conv):
    """An unreadable config must not silently switch titling off — the flag
    gates an improvement, and failing closed disables it by accident."""
    broken = MagicMock()
    broken.conversation_titling = property(lambda self: (_ for _ in ()).throw(
        RuntimeError("unreadable")))
    assert ct._enabled(broken) is True


def test_take_next_skips_already_titled_conversations(conv):
    conv_store.upsert_conversation("sess-t", name="Spark thermals")
    ct.enqueue("sess-t", config=_config())
    assert ct._take_next() is None, "an already-named conversation became an upstream call"


def test_take_next_skips_tiny_conversations(conv):
    conv_store.capture_request(
        "sess-small", {"messages": [{"role": "user", "content": "hi"}]},
        reply="hello", profile="l2", model="m1",
        ts="2026-11-07T10:01:00+00:00")
    conv_store.upsert_conversation("sess-small", profile="l2", model="m1")
    conv_store.recount_conversation("sess-small")
    ct.enqueue("sess-small", config=_config())
    assert ct._take_next() is None


def test_take_next_returns_a_real_candidate(conv):
    ct.enqueue("sess-t", config=_config())
    assert ct._take_next() == "sess-t"


# ── 4. the titler: the call and the write ────────────────────────────────────

def test_parse_title_reply_extracts_both_lines():
    name, summary = ct._parse_title_reply(
        "NAME: Spark thermal audit\nSUMMARY: Checked DGX Spark temps and the l2 budget.\n")
    assert name == "Spark thermal audit"
    assert summary == "Checked DGX Spark temps and the l2 budget."


def test_parse_title_reply_survives_junk():
    assert ct._parse_title_reply("I am a chatty model and will not comply") == (None, None)
    assert ct._parse_title_reply("") == (None, None)


def test_parse_title_reply_caps_lengths():
    name, summary = ct._parse_title_reply(
        "NAME: %s\nSUMMARY: %s" % ("x" * 500, "y" * 500))
    assert len(name) == ct._MAX_NAME
    assert len(summary) == ct._MAX_SUMMARY


def test_resolve_step_prefers_the_configured_model():
    cfg = _config(model="other-model")
    cfg.profiles["career"] = {"chain": [
        {"provider": "prov2", "model": "other-model", "base_url": "https://q/v1"}]}
    step = ct._resolve_step(cfg)
    assert step["model"] == "other-model"
    assert step["provider"] == "prov2"


def test_resolve_step_without_a_match_is_none():
    cfg = _config(model="not-chained-anywhere")
    assert ct._resolve_step(cfg) is None, \
        "an unresolvable model must stop the titler, not send it somewhere random"


def test_resolve_step_default_is_the_default_profiles_chain_head():
    step = ct._resolve_step(_config())
    assert step["model"] == "head-model"


def _title_one_with_reply(cid, reply, status=200, cfg=None):
    with patch("src.api.config.get_config", return_value=cfg or _config()), \
         patch("src.api.request_pipeline.forward_request",
               return_value=({"choices": [{"message": {"content": reply}}]}, status)):
        ct._title_one(cid)


def test_title_one_writes_name_and_summary(conv):
    _title_one_with_reply(
        conv, "NAME: Spark thermal audit\nSUMMARY: Audited the DGX Spark thermals.")
    info = conv_store.conversation_audit(conv)
    assert info["name"] == "Spark thermal audit"
    assert info["summary"] == "Audited the DGX Spark thermals."


def test_title_one_leaves_conversation_untitled_on_non_200(conv):
    _title_one_with_reply(conv, "NAME: never", status=502)
    assert conv_store.conversation_audit(conv)["name"] is None


def test_title_one_writes_nothing_for_a_junk_reply(conv):
    _title_one_with_reply(conv, "no structured content here")
    info = conv_store.conversation_audit(conv)
    assert info["name"] is None
    assert info["summary"] is None


def test_title_one_honours_the_disabled_flag(conv):
    _title_one_with_reply(conv, "NAME: never", cfg=_config(enabled=False))
    assert conv_store.conversation_audit(conv)["name"] is None


def test_title_one_swallows_an_upstream_exception(conv):
    with patch("src.api.config.get_config", return_value=_config()), \
         patch("src.api.request_pipeline.forward_request",
               side_effect=RuntimeError("connection refused")):
        ct._title_one(conv)  # must not raise
    assert conv_store.conversation_audit(conv)["name"] is None


# ── 5. the settings surface ──────────────────────────────────────────────────

def _make_handler(path="/", method="GET", body=None, config=None):
    """A handler that skips BaseHTTPRequestHandler.__init__ (see
    test_settings_endpoints for the origin of this pattern)."""
    from src.server.handler import LCPHandler

    class H(LCPHandler):
        def __init__(self):
            self.path = path
            self.command = method
            self.headers = {}
            self.request_version = "HTTP/1.1"
            self.requestline = "%s %s HTTP/1.1" % (method, path)
            self.raw_requestline = b""
            self.client_address = ("127.0.0.1", 0)
            self.send_response = MagicMock()
            self.send_header = MagicMock()
            self.end_headers = MagicMock()
            self.wfile = MagicMock()
            self.rfile = MagicMock()
            self._write_chunk = MagicMock()
            self.engine = None
            self.config = config
            self.log_error = MagicMock()
            if body is not None:
                raw = json.dumps(body).encode()
                self.rfile.read = MagicMock(return_value=raw)
                self.headers["Content-Length"] = str(len(raw))

    return H()


def _status(h):
    return h.send_response.call_args[0][0] if h.send_response.call_args else None


def _sent(h):
    return json.loads(h.wfile.write.call_args[0][0].decode("utf-8"))


class _Cfg:
    """A minimal config double: the properties the settings surface reads and
    writes, without a DB (``save`` is recorded, not executed)."""

    def __init__(self, enabled=True, model=None):
        self.raw = {"conversation_titling": {"enabled": enabled, "model": model}}
        self.server = {"default_profile": "l2"}
        self.profiles = {"l2": {"chain": [
            {"provider": "prov", "model": "m-a", "base_url": "https://p/v1"},
            {"provider": "prov", "model": "m-b", "base_url": "https://p/v1"},
        ]}}
        self.conversation_titling = self.raw["conversation_titling"]
        self.saved = 0

    def save(self):
        self.saved += 1


def test_settings_routes_are_registered():
    from src.server.handler import LCPHandler
    rt = LCPHandler._route_table()
    names = [r.name for r in rt.rules]
    assert "page.settings" in names
    assert "api.settings.conversations" in names


def test_settings_page_renders_toggle_picker_and_hint():
    from src.ui.pages import render_settings_page
    html = render_settings_page(_Cfg())
    assert "/api/settings/conversations" in html, "the save button has no endpoint"
    assert 'id="titlingEnabled"' in html
    assert 'id="titlingModel"' in html
    assert "Default profile chain head" in html
    assert "<option value=\"m-b\">" in html, "a chained model is missing from the picker"


def test_settings_page_marks_the_sidebar_entry_active():
    from src.ui.pages import render_settings_page
    html = render_settings_page(_Cfg())
    assert 'href="/settings"' in html
    assert 'aria-current="page"' in html


def test_settings_payload_lists_chained_models_only():
    cfg = _Cfg()
    cfg.profiles["l2"]["chain"][0]["model"] = "m-a"
    p = _payload(cfg)
    assert p["titling"] == {"enabled": True, "model": ""}
    assert p["models"] == ["m-a", "m-b"]


def _payload(cfg):
    from src.ui.pages import settings_payload
    return settings_payload(cfg)


def test_settings_get_api_returns_the_section():
    h = _make_handler(path="/api/settings/conversations", config=_Cfg())
    h._serve_conversations_settings_api()
    assert _status(h) == 200
    body = _sent(h)
    assert body["titling"]["enabled"] is True
    assert body["models"] == ["m-a", "m-b"]


def test_settings_post_persists_and_audits():
    cfg = _Cfg()
    h = _make_handler(path="/api/settings/conversations", method="POST",
                      body={"enabled": False, "model": "m-b"}, config=cfg)
    h._serve_conversations_settings_update_api()
    assert _status(h) == 200
    assert _sent(h)["ok"] is True
    assert cfg.raw["conversation_titling"] == {"enabled": False, "model": "m-b"}
    assert cfg.saved == 1


def test_settings_post_empty_model_means_chain_head():
    """``""``/null must be STORED as unset — the runtime meaning is "default
    profile's chain head", and storing "" would make _resolve_step search for
    a model literally named ""."""
    cfg = _Cfg()
    h = _make_handler(path="/api/settings/conversations", method="POST",
                      body={"enabled": True, "model": ""}, config=cfg)
    h._serve_conversations_settings_update_api()
    assert cfg.raw["conversation_titling"]["model"] is None


def test_settings_post_rejects_non_boolean_enabled():
    h = _make_handler(path="/api/settings/conversations", method="POST",
                      body={"enabled": "yes"}, config=_Cfg())
    h._serve_conversations_settings_update_api()
    assert _status(h) == 400


def test_settings_post_rejects_an_unchained_model():
    """The picker cannot offer it; a hand-built request must not store it —
    an unresolvable model means the titler silently titles nothing."""
    h = _make_handler(path="/api/settings/conversations", method="POST",
                      body={"enabled": True, "model": "not-in-any-chain"},
                      config=_Cfg())
    h._serve_conversations_settings_update_api()
    assert _status(h) == 400


def test_settings_post_rejects_invalid_json():
    h = _make_handler(path="/api/settings/conversations", method="POST",
                      config=_Cfg())
    h.rfile.read = MagicMock(return_value=b"{not json")
    h.headers["Content-Length"] = "9"
    h._serve_conversations_settings_update_api()
    assert _status(h) == 400


def test_conversations_page_hints_at_the_titler(store):
    """The "Untitled" rows now have a cause and a fix; both must be visible
    from the page that shows the rows."""
    from src.ui.pages import render_conversations_page
    html = render_conversations_page(_Cfg(), None, {})
    assert 'href="/settings"' in html
    assert "Untitled" in html


def test_conversations_page_marks_untitled_rows(store):
    conv_store.capture_request(
        "sess-hint", {"messages": [{"role": "user", "content": "check"}]},
        reply="ok", profile="l2", model="m1",
        ts="2026-11-07T11:00:00+00:00")
    conv_store.upsert_conversation("sess-hint", profile="l2", model="m1",
                                   source="live")
    conv_store.recount_conversation("sess-hint")
    from src.ui.pages import render_conversations_page
    html = render_conversations_page(_Cfg(), None, {})
    assert 'class="muted conv-untitled"' in html


def test_conversations_page_still_shows_real_names(store):
    """The class is additive; a titled row must not regress to the muted
    untitled treatment."""
    conv_store.capture_request(
        "sess-named", {"messages": [{"role": "user", "content": "check"}]},
        reply="ok", profile="l2", model="m1",
        ts="2026-11-07T11:00:00+00:00")
    conv_store.upsert_conversation("sess-named", name="Spark thermals",
                                   profile="l2", model="m1", source="live")
    conv_store.recount_conversation("sess-named")
    from src.ui.pages import render_conversations_page
    html = render_conversations_page(_Cfg(), None, {})
    assert "Spark thermals" in html


# ── 6. the persistent third column (panel, not a modal) ──────────────────────

def test_conversations_page_has_a_persistent_panel(store):
    """The lane is an <aside> in the markup — always rendered with the table,
    not opened on demand."""
    from src.ui.pages import render_conversations_page
    html = render_conversations_page(_Cfg(), None, {})
    assert '<aside class="conv-panel" id="convPanel"' in html
    assert 'aria-label="Conversation detail"' in html


def test_conversations_page_has_no_transcript_modal(store):
    """The modal era is over: a dialog that reflows nothing and persists is the
    shape the operator approved for drill-downs (CUR-330 detail-panel rules)."""
    from src.ui.pages import render_conversations_page
    html = render_conversations_page(_Cfg(), None, {})
    assert 'id="convModal"' not in html, "the transcript modal came back"


def test_conversation_rows_are_the_click_target(store):
    """The whole row opens the panel — keyboard reachable (role=button +
    tabindex) and named for screen readers — replacing the per-row View
    button that duplicated the affordance."""
    conv_store.capture_request(
        "sess-row", {"messages": [{"role": "user", "content": "check"}]},
        reply="ok", profile="l2", model="m1",
        ts="2026-11-07T11:00:00+00:00")
    conv_store.upsert_conversation("sess-row", name="Row target",
                                   profile="l2", model="m1", source="live")
    conv_store.recount_conversation("sess-row")
    from src.ui.pages import render_conversations_page
    import re
    html = render_conversations_page(_Cfg(), None, {})
    m = re.search(r'<tr class="conv-row[^"]*"[^>]*data-cid="sess-row".*?>', html, re.S)
    row = m.group(0)
    assert 'role="button"' in row
    assert 'tabindex="0"' in row
    assert 'data-name="Row target"' in row
    assert "conv-toggle" not in html, "the per-row View button is still rendered"


def test_conversations_page_loads_the_newest_conversation_by_default(store):
    """The panel is never an empty slab on arrival: the newest conversation in
    the current filter loads on first paint (the IIFE clicks the first row)."""
    conv_store.capture_request(
        "sess-first", {"messages": [{"role": "user", "content": "check"}]},
        reply="ok", profile="l2", model="m1",
        ts="2026-11-07T11:00:00+00:00")
    conv_store.upsert_conversation("sess-first", name="Newest one",
                                   profile="l2", model="m1", source="live")
    conv_store.recount_conversation("sess-first")
    from src.ui.pages import render_conversations_page
    html = render_conversations_page(_Cfg(), None, {})
    assert "first.click()" in html, "the panel does not auto-load the newest row"
    assert "CONV_EMPTY_HINT" in html, "the reset action has no empty-state hint"
    assert "keydown" in html, "rows are not keyboard operable"


def test_conversations_css_reserves_the_panel_lane():
    """The lane is a FIXED full-height panel: on desktop the body margin
    reserves its width (the panel is out of flow, like the overview's
    detail-panel), below 1100px it is a slide-over sheet with a backdrop."""
    path = "src/ui/templates/jinja/static/dashboard.css"
    css = open(path, encoding="utf-8").read()
    assert ".conv-panel {\n  position: fixed; top: 0; right: 0; bottom: 0;" in css, \
        "the panel is not a full-height fixed lane"
    assert "width: var(--conv-width, var(--detail-width))" in css
    assert "body:has(.conv-panel) .main-content" in css, "the lane is not reserved"
    assert "min-width: 1100px" in css, "the reservation has no desktop breakpoint"
    # The token is defined once (by the conversations block on this tree), and
    # the dragged width overrides it without redefining it.
    assert css.count("--detail-width: ") == 1, "--detail-width is defined more than once"
    assert "--conv-width" in css
    # The rejected sticky-in-grid shape must not come back.
    assert ".conv-panel { position: sticky" not in css
    # The rejected modal-era mechanics must not come back.
    assert ".detail-open" not in css


def test_conversations_panel_has_a_drag_handle():
    """File-explorer-style splitter: the resizer exists, is keyboard/pointer
    driven, clamps its width, persists it, and double-click resets."""
    path = "src/ui/templates/jinja/pages/conversations.html"
    html = open(path, encoding="utf-8").read()
    assert 'id="convResizer"' in html, "no drag handle in the template"
    assert "pointerdown" in html and "pointermove" in html and "pointerup" in html
    assert "CONV_MIN_W" in html and "CONV_MAX_W" in html, "the drag is unclamped"
    assert "conv-panel-width" in html, "the dragged width does not persist"
    assert "dblclick" in html, "double-click does not reset the width"
    css = open("src/ui/templates/jinja/static/dashboard.css", encoding="utf-8").read()
    assert ".conv-resizer" in css and "col-resize" in css


def test_conversations_panel_works_on_mobile():
    """Below 1100px the panel is a slide-over sheet: a backdrop exists, row
    tap opens the sheet, backdrop/Escape dismiss it, and the sheet does NOT
    auto-open on page load (covering the table would be a modal)."""
    path = "src/ui/templates/jinja/pages/conversations.html"
    html = open(path, encoding="utf-8").read()
    assert 'id="convBackdrop"' in html, "no backdrop element"
    assert "CONV_DESKTOP" in html and "matchMedia" in html, "no viewport split"
    assert "convPanelHide" in html, "the sheet cannot be dismissed"
    assert "Escape" in html, "the sheet is not Escape-dismissable on mobile"
    assert "if (!CONV_DESKTOP.matches) { convPanelSetEmpty(); return; }" in html, \
        "the sheet auto-opens on mobile page load"
    assert "convPanelShow();" in html, "a row tap does not open the sheet"
    assert "convSyncOpenToViewport" in html, "viewport flips (rotate/resize) do not resync the sheet"
    assert "addEventListener('change', convSyncOpenToViewport)" in html
