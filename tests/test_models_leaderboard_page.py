"""The Models page is the LEADERBOARD — the surface the operator asked for.

Operator, 2026-11-15:

  "the 3rd column is permanent with it taking height from top to bottom ... use
   same 3rd column component as on overview and conversations"
  "in 2nd column we will have 2 buttons present, poll latest data and manage
   taxonomy"
  "we should remove the usage widget from models now"
  "we will should also remove this filter and register models, and Model tab with
   them"
  "we will have only leaderboard"
  "instead of these tabs we will have 3 tabs + 1 tab with '…' on which click we
   will have a dropdown select list for the remaining tabs"
  "on mobile view this will be 1 tab + 1 tab with '…'"

Each of those is a rendered-markup fact, so it is pinned against the REAL
renderer (``render_models_page``), not the template source. A regex over the
partial would pass while the page still shipped the old surface.
"""
from __future__ import annotations

import os
import re

import pytest
from unittest.mock import MagicMock

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "src", "ui", "templates", "jinja")


@pytest.fixture
def engine(temp_db):
    return temp_db[1]


@pytest.fixture
def cfg():
    c = MagicMock()
    c.providers = {"deepseek": {"api_key_env": "DEEPSEEK_API_KEY",
                                "api_base": "https://api.deepseek.com/v1",
                                "models": ["deepseek-chat"]}}
    c.profiles = {"l2": {"forbidden_tools": [], "chain": []}}
    c.server = {"port": 8734}
    return c


def page(cfg, engine, params=None):
    from src.ui.pages import render_models_page
    return render_models_page(cfg, engine, params or {})


# ── 1. leaderboard only ─────────────────────────────────────────────────────

@pytest.mark.parametrize("dead", [
    'id="modelList"',            # the left model list
    'id="modelFilter"',          # its filter
    'id="addRegistryBtn"',       # + Register model
    'id="configuredOnlyBtn"',    # the Configured toggle
    'id="modelDetail"',          # the Model detail tab body
    'id="editModelModal"',       # the modals that belonged to it
    'id="registerModelModal"',
    'id="scoreModal"',
    'id="deleteModelModal"',
    'data-tax="model"',          # the Model tab itself
    'Capability scores',         # the matrix surface
    'the dynamic router sorts by',
])
def test_the_matrix_surface_is_gone(cfg, engine, dead):
    assert dead not in page(cfg, engine)


def test_the_page_still_names_itself_models(cfg, engine):
    html = page(cfg, engine)
    assert "<h1>Models</h1>" in html
    # the tab strip that still exists: the leaderboard and the providers
    strip = html.split('<div class="page-tabs">')[1].split("</div>")[0]
    assert re.findall(r'href="([^"]+)"', strip) == ["/models", "/models?tab=providers"]
    assert ">Leaderboard<" in strip


def test_a_matrix_bookmark_lands_on_the_leaderboard(cfg, engine):
    """`?tab=matrix` was the tab id until the matrix left; it must not 404."""
    html = page(cfg, engine, {"tab": "matrix"})
    assert 'id="lbTable"' in html
    assert 'id="modelList"' not in html


# ── 2. column 3 IS the shared detail panel ──────────────────────────────────

def test_column_three_is_the_shared_detail_panel(cfg, engine):
    """Not a bespoke in-grid card: the SAME component as overview/conversations."""
    html = page(cfg, engine)
    panel = html.split('id="modelsPanel"')[0].rsplit("<aside", 1)[1]
    assert 'class="detail-panel"' in panel, panel
    for part in ('id="mpResizer"', 'class="dp-header"', 'class="dp-body"'):
        assert part in html, part
    # the drag handle and the backdrop come WITH the component
    assert 'id="mpBackdrop"' in html and 'dp-backdrop' in html


def test_the_panel_no_longer_looks_like_a_sticky_card(cfg, engine):
    """The old panel was an in-grid <section> stack; the lane replaced it."""
    html = page(cfg, engine)
    assert 'class="models-panel"' not in html
    assert 'class="mp-section"' not in html


def test_the_detail_width_token_is_defined_once(cfg, engine):
    """--detail-width is the shared default; a page override rides --dp-width.

    Two `:root` definitions is not a harmless override — test_cur329_convo_titling
    asserts the token is defined exactly once, and a second one silently wins
    depending on which stylesheet block loaded last.
    """
    css = open(os.path.join(TEMPLATES, "static", "dashboard.css")).read()
    assert css.count("--detail-width:") == 1
    # the lane follows a per-page dragged width, with the shared default behind it
    assert "var(--dp-width, var(--detail-width))" in css


def test_the_lane_is_full_height_not_a_card_beside_the_table(cfg, engine):
    css = open(os.path.join(TEMPLATES, "static", "dashboard.css")).read()
    lane = css.split(".detail-panel {", 1)[1].split("}", 1)[0]
    assert "position: fixed" in lane
    assert "top: 0" in lane and "bottom: 0" in lane
    # and the body reserves the width on desktop, from the same token
    assert "body:has(.detail-panel) .main-content" in css
    assert "margin-right: var(--dp-width, var(--detail-width))" in css


# ── 3. exactly two buttons in column 2 ─────────────────────────────────────

def test_column_two_carries_the_two_controls(cfg, engine):
    html = page(cfg, engine)
    # the whole strip row: from .lb-head up to the board it introduces
    head = html.split('class="lb-head"', 1)[1].split('id="leaderboardPane"', 1)[0]
    assert 'id="pollRunBtn"' in head
    assert 'id="taxOpenBtn"' in head
    assert ">Poll latest data<" in head
    assert ">Manage taxonomy<" in head
    # nothing else pretends to be a control up there
    assert head.count("<button") == 2


def test_the_two_buttons_open_the_two_panes(cfg, engine):
    html = page(cfg, engine)
    assert 'id="pollPane"' in html and 'id="taxPane"' in html
    # both panes live INSIDE the shared panel, which is what the buttons drive
    body = html.split('id="mpBody"')[1]
    assert 'id="pollPane"' in body and 'id="taxPane"' in body


# ── 4. the usage widget is gone from this page ──────────────────────────────

def test_the_usage_widget_is_not_rendered_on_models(cfg, engine):
    """The header pill carries a provider/token readout AND a credits bar; it is
    a usage surface, and this page is a leaderboard."""
    html = page(cfg, engine)
    for dead in ('id="pluginHeaderBadge"', 'id="hpUsage"', 'hp-usage-fill'):
        assert dead not in html, dead


def test_the_usage_widget_is_still_on_the_pages_that_want_it(cfg, engine):
    """The gate is per-page, not a deletion: dropping it everywhere would take a
    live surface off Usage/Profiles/Activity."""
    from src.ui.pages import render_models_page, render_usage_page  # noqa: F401
    from src.ui.render import render_page
    other = render_page("pages/alerts.html", cfg, engine, active_page="alerts")
    assert 'id="pluginHeaderBadge"' in other


# ── 5. the tab strip: 3 + "…" desktop, 1 + "…" mobile ──────────────────────

def test_the_strip_renders_a_more_affordance(cfg, engine):
    html = page(cfg, engine)
    assert 'id="mlTabs"' in html
    # the "…" button is created by the strip renderer, and its dropdown carries
    # the overflowed tabs; both must exist in the shipped script.
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    assert "STRIP_DESKTOP = 3" in script
    assert "STRIP_MOBILE = 1" in script
    assert "ml-more-menu" in script
    assert "ml-more-item" in script


def test_the_strip_breakpoint_is_768(cfg, engine):
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    assert "matchMedia('(min-width: 768px)')" in script


def test_the_more_menu_is_keyboard_reachable(cfg, engine):
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    for token in ("aria-haspopup", "aria-expanded", "ArrowDown", "ArrowUp", "Escape"):
        assert token in script, token
    # the menu carries the ARIA menu roles: the container is set programmatically
    # (it is created per open), the items are in the markup.
    assert "setAttribute('role', 'menu')" in script
    assert 'role="menuitem"' in script


# ── 6. the panel's poll copy answers the operator's own question ────────────

def test_the_panel_says_a_custom_board_has_no_upstream(cfg, engine):
    """His words: "poll latest data will not poll data for custom taxonomy?? how
    does this even work? it will poll the real data, what does lmarena have?"

    The answer belongs on the surface, not in a reply he has to remember.
    """
    html = page(cfg, engine)
    assert "cannot poll your own taxonomies" in html
    assert "Elo" in html and "net improvement" in html
    # and what it will never do to his numbers
    assert "stages" in html.lower() and "Update" in html
