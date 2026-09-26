"""M3 UI — the margin gate is set where the router reads it, and shown honestly.

M3 shipped the gate as a **profile-config** field, loadable and writable through
`/api/profiles`, but with no control on either surface a person actually uses: the
add/edit modal and a profile's Routing tab. This pins the three things that go wrong
when you add one.

1. **The control writes to the wrong store.** Everything else on the Routing tab is a
   *router* setting (`/api/routing/policy`); the gate is a *profile* setting that
   `Router._intent_margin_gate` reads out of the profile's config. A Save that posted
   the gate to the router would look like it worked and change nothing — the failure
   is silent, which is exactly why it gets a test.
2. **The page lies about what is in effect.** The value is read through
   `validate_profile_fields`, the same contract the router reads it through. A value
   the loader would reject must not be displayed as if it were live: the loader's
   response to an invalid profile is to fall back to the built-in seed, so the profile
   would be running something else entirely.
3. **The modal forgets the field.** The fields the page shows and the payload the save
   sends have to be the same set — on the create path *and* the edit path.
"""
import json
import re

import pytest
from unittest.mock import MagicMock, patch

from src.api.config import validate_profile_fields
from src.ui import pages


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def profiles_root(tmp_path, monkeypatch):
    """A profiles root with the one profile these tests care about."""
    root = tmp_path / "profiles"
    lane = root / "homelab-expert-l2"
    lane.mkdir(parents=True)
    (lane / "config.yaml").write_text("provider:\n  base_url: http://localhost:8734/l2\n")
    (lane / "SOUL.md").write_text("# SOUL\n\nYou are l2.\n")
    monkeypatch.setenv("LCP_PROFILES_DIR", str(root))
    monkeypatch.setenv("LCP_AVATAR_DIR", str(tmp_path / "avatars"))
    return root


def _cfg(profiles):
    c = MagicMock()
    c.providers = {"deepseek": {"api_key_env": "DEEPSEEK_API_KEY",
                                "api_base": "https://api.deepseek.com/v1",
                                "models": ["deepseek-chat"]}}
    c.profiles = profiles
    c.server = {"port": 8734}
    c.default_profile = "l2"
    return c


@pytest.fixture
def cfg():
    return _cfg({
        "l2": {"forbidden_tools": [], "auth_required": True,
               "chain": [{"provider": "deepseek", "model": "deepseek-chat"}]},
    })


@pytest.fixture
def engine(temp_db):
    return temp_db[1]


def _status(name, **over):
    """A router status that knows about exactly one profile."""
    per = {"enabled": True, "policy": "eager", "min_score": 0.35, "rules": [],
           "has_override": True}
    per.update(over)
    return {"per_profile": {name: per}, "per_task": {}, "providers": ["deepseek"],
            "recent_decisions": []}


def _routing_tab(cfg, engine, name):
    with patch("src.api.router.routing_status", lambda *a, **k: _status(name)):
        return pages.render_profile_detail_page(cfg, engine, name, {"tab": "routing"})


# ── 1. reading the gate: the same contract the router uses ───────────────────

def test_an_unset_gate_reads_as_off(cfg, profiles_root):
    assert pages._profile_margin_gate(cfg, "l2") == 0.0


@pytest.mark.parametrize("value", [0.0, 0.15, 1.0])
def test_a_set_gate_reads_back_verbatim(cfg, profiles_root, value):
    cfg.profiles["l2"]["intent_margin_gate"] = value
    assert pages._profile_margin_gate(cfg, "l2") == value


@pytest.mark.parametrize("junk", ["not a number", 2.5, -0.1, True, {}])
def test_a_value_the_loader_would_reject_is_never_shown_as_live(cfg, profiles_root, junk):
    """The negative control for (2): the page must report what is IN EFFECT.

    Each of these makes `validate_profile_fields` raise — verified here rather than
    assumed, so the test cannot pass because the junk happened to be accepted.
    """
    with pytest.raises(Exception):
        validate_profile_fields("l2", {"intent_margin_gate": junk})
    cfg.profiles["l2"]["intent_margin_gate"] = junk
    assert pages._profile_margin_gate(cfg, "l2") == 0.0


def test_the_reader_survives_a_config_it_cannot_interrogate():
    """Routing must never break on a config shape — the tab still has to render."""
    assert pages._profile_margin_gate(None, "l2") == 0.0
    assert pages._profile_margin_gate(MagicMock(), "l2") == 0.0
    assert pages._profile_margin_gate(_cfg({}), "nope") == 0.0


# ── 2. the Routing tab's view ────────────────────────────────────────────────

def test_the_routing_view_carries_the_gate(cfg, profiles_root):
    with patch("src.api.router.routing_status", lambda *a, **k: _status("l2")):
        assert pages._profile_routing_view(cfg, "l2")["intent_margin_gate"] == 0.0
    cfg.profiles["l2"]["intent_margin_gate"] = 0.25
    with patch("src.api.router.routing_status", lambda *a, **k: _status("l2")):
        assert pages._profile_routing_view(cfg, "l2")["intent_margin_gate"] == 0.25


def test_the_gate_is_on_the_view_even_when_the_router_says_nothing(cfg, profiles_root):
    """Set before the view's early returns, so a profile with a gate but no router
    entry shows the gate it actually has instead of a default."""
    cfg.profiles["l2"]["intent_margin_gate"] = 0.25
    with patch("src.api.router.routing_status", lambda *a, **k: {}):
        view = pages._profile_routing_view(cfg, "l2")
    assert view["available"] is False
    assert view["intent_margin_gate"] == 0.25


def test_the_view_is_still_json_serialisable(cfg, profiles_root):
    """The view is embedded in a <script type="application/json"> block."""
    cfg.profiles["l2"]["intent_margin_gate"] = 0.25
    with patch("src.api.router.routing_status", lambda *a, **k: _status("l2")):
        json.dumps(pages._profile_routing_view(cfg, "l2"))


# ── 3. the Routing tab's control ─────────────────────────────────────────────

def test_the_routing_tab_renders_the_gate_control(cfg, profiles_root, engine):
    cfg.profiles["l2"]["intent_margin_gate"] = 0.25
    html = _routing_tab(cfg, engine, "l2")
    assert 'id="prouMarginGate"' in html
    assert "prouSaveMarginGate()" in html
    assert 'value="0.25"' in html


def test_off_is_shown_blank_rather_than_as_zero(cfg, profiles_root, engine):
    """0 and unset mean the same thing, so showing an explicit 0 would suggest a
    setting that is not there."""
    html = _routing_tab(cfg, engine, "l2")
    m = re.search(r'id="prouMarginGate"[^>]*value="([^"]*)"', html)
    assert m, "the control disappeared"
    assert m.group(1) == ""


def test_the_gate_saves_to_the_profile_and_not_to_the_router(cfg, profiles_root, engine):
    """The crux: the router reads the gate from the profile's config, so a save that
    went to /api/routing/policy would report success and change nothing."""
    html = _routing_tab(cfg, engine, "l2")
    assert "'/api/profiles/' + PROFILE, { intent_margin_gate: value }" in html
    # and the tab says which store it is, so the two Save buttons are not a mystery
    assert "not a router setting" in html


def test_the_tab_rejects_a_gate_the_api_would_refuse(cfg, profiles_root, engine):
    """A 400 from the API must not be the first feedback a typo gets."""
    html = _routing_tab(cfg, engine, "l2")
    assert "Enter a number between 0 and 1" in html


# ── 4. the add/edit modal ────────────────────────────────────────────────────

def test_the_modal_offers_the_field(cfg, profiles_root):
    html = pages.render_profiles_page(cfg, engine=None, params={})
    assert 'id="pemMarginGate"' in html


def test_the_modal_sends_the_gate_on_both_save_paths(cfg, profiles_root):
    """Create and edit are two payloads; a field added to one and not the other is
    the classic half-done edit — it appears to save on the path it was tested on."""
    html = pages.render_profiles_page(cfg, engine=None, params={})
    assert html.count("intent_margin_gate: marginGateValue()") == 2
    assert "function marginGateValue()" in html


def test_the_modal_shows_off_as_blank_and_round_trips_it(cfg, profiles_root):
    html = pages.render_profiles_page(cfg, engine=None, params={})
    # off (null, absent, or 0) is displayed blank; anything else as itself
    assert "Number(gate) === 0) ? '' : gate" in html
    # blank is sent as null, which the loader treats as 0.0 == off
    assert "if (raw === '') return null;" in html
