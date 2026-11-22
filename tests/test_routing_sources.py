"""Routing sources: the two switches, and the tiers that depend on them.

Operator, 2026-11-21:

    "I say that we remove manage taxonomy as its unnecessary complicates the process. Just
     keep livebench and lmarena and have it configurable which one to turn on in global
     settings and then if both enabled have them chooseable in the profile"

Four claims, each its own test group:

1. **Two sources, one switch each.** `livebench` and `lmarena` are on/off in global settings,
   and the switch is the SAME `model_intel.sources.<name>.accepted` the poller reads — so a
   source cannot be off for polling and on for routing.
2. **A tier needs its source.** `static` needs none (it consults nothing, so it is always
   honest); `intent` needs LiveBench; `arena` needs LMArena.
3. **An unusable tier falls back to static and SAYS SO** — and the operator's choice is kept,
   so switching the source back on restores it. Nothing silently keeps routing on a source
   that was switched off.
4. **Manage taxonomy is gone** from the matrix tab (button, pane, binding), while the mapping
   DATA it edited survives — `model_ratings.mapped_in` still reads it.
"""
from __future__ import annotations

import os

import pytest
from unittest.mock import MagicMock

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "src", "ui", "templates", "jinja")


def _sec(name):
    return open(os.path.join(TEMPLATES, "sections", name)).read()


# ── 1/2/3. the rule itself ─────────────────────────────────────────────────

from src.api import routing_sources as rs  # noqa: E402


def test_only_two_sources_exist():
    assert (rs.SOURCE_LIVEBENCH, rs.SOURCE_LMARENA) == ("livebench", "lmarena")
    assert set(rs.TIER_REQUIRES) == {"static", "intent", "arena"}


def test_static_needs_no_source_and_is_always_available():
    """It consults neither source, so switching both off cannot make it wrong."""
    avail = rs.tier_availability({"livebench", "lmarena"})
    assert avail["static"]["available"] is True
    assert rs.effective_tier("static", {"livebench", "lmarena"})[0] == "static"


def test_intent_needs_livebench_and_arena_needs_lmarena():
    assert rs.TIER_REQUIRES["intent"] == "livebench"
    assert rs.TIER_REQUIRES["arena"] == "lmarena"
    lb_off = rs.tier_availability({"livebench"})
    assert not lb_off["intent"]["available"] and lb_off["arena"]["available"]
    ar_off = rs.tier_availability({"lmarena"})
    assert not ar_off["arena"]["available"] and ar_off["intent"]["available"]


def test_an_unavailable_tier_falls_back_to_static_and_explains():
    tier, why = rs.effective_tier("intent", {"livebench"})
    assert tier == "static"
    assert "Livebench is switched off" in why and "static" in why
    tier2, why2 = rs.effective_tier("arena", {"livebench", "lmarena"})
    assert tier2 == "static" and "Lmarena" in why2


def test_an_available_tier_is_returned_untouched_with_no_note():
    assert rs.effective_tier("intent", {"lmarena"}) == ("intent", "")
    assert rs.effective_tier("arena", {"livebench"}) == ("arena", "")
    assert rs.effective_tier("intent", set()) == ("intent", "")


def test_a_missing_or_unknown_choice_never_defaults_to_a_tier_that_needs_a_source():
    """Defaulting to `intent` is how switching a source off turns into unexpected routing."""
    assert rs.effective_tier("", set())[0] == "static"
    assert rs.effective_tier(None, set())[0] == "static"
    assert rs.effective_tier("nonsense", set())[0] == "static"


def test_the_old_name_for_static_still_works():
    """`rules` is the pre-rename spelling; an estate that has it saved must not silently
    start routing differently."""
    assert rs.effective_tier("rules", {"livebench", "lmarena"}) == ("static", "")


def test_a_config_object_and_a_loaded_section_agree():
    """The router has the section; the page has the config. One reader, two entry points."""
    section = {"sources": {"livebench": {"accepted": True}, "lmarena": {"accepted": False}}}
    assert rs.enabled_sources(cfg_section=section) == {"livebench"}


def test_enabled_sources_reads_the_config_it_is_GIVEN():
    """Regression: the section was the SECOND parameter, so `enabled_sources(config)`
    ignored the config and read the built-in defaults — which report every source off, i.e.
    a configured estate looked like an unconfigured one."""
    class C:
        model_intel = {"sources": {"lmarena": {"accepted": True}}}
    assert rs.enabled_sources(C()) == {"lmarena"}
    assert rs.enabled_sources({"sources": {"livebench": {"accepted": True}}}) == {"livebench"}
    assert rs.enabled_sources(C(), C.model_intel) == {"lmarena"}
    # and an OFF entry is what gates — a source merely ABSENT never does
    class D:
        model_intel = {"sources": {"lmarena": {"accepted": True},
                                   "livebench": {"accepted": False}}}
    assert rs.enabled_sources(D()) == {"lmarena"}
    assert rs.gating_sources(D()) == {"livebench"}
    assert rs.effective_tier("intent", rs.gating_sources(D()))[0] == "static"


def test_an_UNREADABLE_switch_is_not_the_same_as_all_off():
    """The distinction that keeps the gate honest. If an unreadable store read as "all off",
    a broken config reader would silently turn every profile into `static` — and a silent
    downgrade is indistinguishable from a working one."""
    assert rs.enabled_sources(None) is None            # nothing to read: unknown
    assert rs.enabled_sources({"sources": {}}) == set()  # read: every source off
    # and the gate does NOT fire on unknown
    assert rs.effective_tier("intent", None) == ("intent", "")
    assert rs.effective_tier("arena", None) == ("arena", "")
    assert rs.tier_availability(None)["intent"]["available"] is True
    # ...but it DOES fire on an explicit OFF
    assert rs.effective_tier("intent", {"livebench"})[0] == "static"


def test_source_names_match_the_pollers():
    """Two names for one thing is how a source ends up off for polling and on for routing."""
    from src.api import model_intel_poll as mip
    assert mip.SOURCE_LIVEBENCH == rs.SOURCE_LIVEBENCH
    assert mip.SOURCE_LMARENA == rs.SOURCE_LMARENA


def test_the_poller_reads_the_same_switch_the_page_writes():
    """The Settings page writes `model_intel.sources.<n>.accepted` through
    `/api/model-intel/settings`; this is the poller's reader for it."""
    from src.api.model_intel_poll import SOURCE_LIVEBENCH, source_accepted
    cfg = {"sources": {SOURCE_LIVEBENCH: {"accepted": True}}}
    assert source_accepted(cfg, SOURCE_LIVEBENCH) is True
    assert rs.enabled_sources(cfg_section=cfg) == {SOURCE_LIVEBENCH}


# ── the router enforces it, not just the UI ────────────────────────────────

def test_the_router_gate_is_wired_into_route_by():
    """A stored `intent` from before the switch was flipped must not keep routing on a
    disabled source. The UI is a courtesy; the router is the rule."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api", "router.py")).read()
    body = src[src.index("def _route_by(self, profile, config)"):]
    body = body[:body.index("@staticmethod")]
    assert "effective_tier" in body and "gating_sources" in body, (
        "the router must apply the source gate, not only the UI")


def test_the_status_reports_the_tier_IN_FORCE_not_just_the_pin():
    """A monitoring surface must not read `intent` for a profile that is routing `static`
    because its source is off — a tier that cannot fire reading as one that has not fired
    yet is the same lie as "Arena selected, static behaviour"."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api", "router.py")).read()
    assert src.count('"route_by_effective": eff') == 2, "both the per-profile and global blocks"
    assert src.count('"route_by_note": why') == 2
    # the helper's own `def` line also matches the naive substring, so count the calls
    assert src.count("= _tier_in_force(router, config") == 2


# ── 4. Manage taxonomy is gone; its data is not ────────────────────────────

@pytest.fixture
def engine(temp_db):
    return temp_db[1]


@pytest.fixture
def cfg():
    c = MagicMock()
    c.providers = {"deepseek": {"api_key_env": "K", "api_base": "u",
                                "models": ["deepseek-chat"]}}
    c.profiles = {"l2": {"forbidden_tools": [], "chain": []}}
    c.server = {"port": 8734}
    c.raw = {"model_intel": {"sources": {"livebench": {"accepted": True}}}}
    return c


def _matrix(cfg, engine):
    from src.ui.pages import render_routing_definitions_page
    return render_routing_definitions_page(cfg, engine, {"tab": "matrix"})


def test_manage_taxonomy_is_gone_from_the_matrix_tab(cfg, engine):
    html = _matrix(cfg, engine)
    assert 'id="taxOpenBtn"' not in html, "the button is still there"
    assert 'id="taxPane"' not in html, "the pane is still there"
    assert "Manage taxonomy" not in html
    for f in ("sec_routingdefs_matrix.html", "sec_routingdefs_matrix_script.html",
              "sec_taxonomy_pane_script.html"):
        assert "taxOpenBtn" not in _sec(f), f"{f} still binds the removed control"


def test_the_taxonomy_mapping_DATA_survives_the_editor_removal():
    """The mapping is still authoritative — `mapped_in` restricts arena rows by it, and the
    seeded rows are the vocabulary routing already speaks. Only the editor was removed."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api",
                            "model_ratings.py")).read()
    assert "taxonomy_map" in src and "def mapped_in" in src


def test_the_matrix_still_has_its_three_remaining_controls(cfg, engine):
    html = _matrix(cfg, engine)
    for want in ('id="pollOpenBtn"', 'id="importOpenBtn"', 'id="listOpenBtn"'):
        assert want in html, f"{want} must survive the taxonomy removal"


# ── the Settings page carries the switches ────────────────────────────────

def test_settings_shows_both_source_switches_and_the_tier_list(cfg, engine):
    from src.ui.pages import render_settings_page
    html = render_settings_page(cfg, engine, {})
    assert 'class="srcToggle" data-source="livebench"' in html
    assert 'class="srcToggle" data-source="lmarena"' in html
    assert 'id="sourcesSave"' in html
    # the tier consequences are stated, not implied
    for tier in ("static", "intent", "arena"):
        assert f">{tier}<" in html or f'value="{tier}"' in html or tier in html
    assert "needs livebench" in html and "needs lmarena" in html


def test_settings_save_uses_the_endpoint_that_owns_the_switch(cfg, engine):
    from src.ui.pages import render_settings_page
    html = render_settings_page(cfg, engine, {})
    body = html[html.index('id="sourcesSave"'):]
    assert "/api/model-intel/settings" in body, (
        "the save must go to the endpoint that owns sources.<name>.accepted, or polling "
        "and routing could disagree about whether a source is on")


def test_settings_says_what_turning_a_source_off_DOES(cfg, engine):
    """Turning it off stops polling AND makes its tier unavailable — not a no-op."""
    from src.ui.pages import render_settings_page
    html = render_settings_page(cfg, engine, {})
    assert "fall back to" in html and "static" in html
    assert "kept, not deleted" in html


def test_settings_marks_an_unavailable_tier(cfg, engine):
    cfg.raw = {"model_intel": {"sources": {"livebench": {"accepted": False},
                                          "lmarena": {"accepted": False}}}}
    from src.ui.pages import render_settings_page
    html = render_settings_page(cfg, engine, {})
    assert html.count("badge badge-error") >= 2, "both dependent tiers should read unavailable"


# ── the profile page makes the choice ─────────────────────────────────────

def test_the_profile_selector_disables_a_tier_with_no_source(cfg, engine):
    """'if both enabled have them chooseable in the profile' — so with one off, that tier is
    not choosable, and the reason is on screen."""
    # LiveBench explicitly OFF -> Intent is not choosable; LMArena on -> Arena is
    cfg.raw = {"model_intel": {"sources": {"livebench": {"accepted": False},
                                          "lmarena": {"accepted": True}}}}
    from src.ui.pages import _profile_routing_view
    view = _profile_routing_view(cfg, "l2", status={"per_profile": {"l2": {
        "enabled": True, "policy": "eager", "min_score": 0.0, "rules": [],
        "has_override": False}}})
    tiers = {t["name"]: t for t in view["tiers"]}
    assert tiers["static"]["available"] is True
    assert tiers["arena"]["available"] is True
    assert tiers["intent"]["available"] is False
    assert "Livebench" in tiers["intent"]["reason"]


def test_the_profile_view_reports_the_tier_in_force_and_why(cfg, engine):
    """The view must carry `route_by` — nothing set it before, so the selector showed no
    current value and the chip read 'ON · ' with nothing after it."""
    cfg.raw = {"model_intel": {"sources": {"lmarena": {"accepted": True}}}}
    from src.ui.pages import _profile_routing_view
    view = _profile_routing_view(cfg, "l2", status={"per_profile": {"l2": {
        "enabled": True, "policy": "eager", "min_score": 0.0, "rules": [],
        "has_override": False}}})
    assert view["route_by"] in ("static", "intent", "arena")
    assert view["route_by_effective"] in ("static", "intent", "arena")
