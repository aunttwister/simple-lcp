"""The Providers page — its own page since 2026-11-25 (CUR-358).

Operator, 2026-11-25:

  "in LCP we can remove the leaderboard from models .. actually the entire page, as we
   have that in routing definition, we should however leave providers and modernize it a
   bit with UI to fit the other pages we have"

Three claims are pinned here against the REAL renderer, because each one can rot
silently while /providers still answers 200:

1. **Models is gone and the board did not move.** The leaderboard was already one
   partial (`sec_leaderboard.html`) rendered by Routing definitions → Arena boards, so
   deleting the page must not delete a table — and `/models` must land on that table.
2. **Each tab ships ONE panel and ONE script.** Before this, all four panels sat in the
   DOM behind CSS and every tab began every poll: the Health tab paid for the registry
   table, the routing editor and the capability probe.
3. **The UI is the house UI.** Server-side `.page-tabs` links (a tab is a URL), `.card`
   surfaces, breadcrumbs — not the ad-hoc inline-styled boxes the merged page carried.
"""
from __future__ import annotations

import os
import re

import pytest
from unittest.mock import MagicMock

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "src", "ui", "templates", "jinja")
SECTIONS = os.path.join(TEMPLATES, "sections")


@pytest.fixture
def engine(temp_db):
    return temp_db[1]


@pytest.fixture
def cfg():
    c = MagicMock()
    c.providers = {
        "opencode": {"api_key_env": "OC", "api_base": "https://opencode.ai/inference/go/v1",
                     "models": ["glm-5.3-flash"], "type": "opencode"},
        "local-sparks": {"api_base": "http://192.168.1.107:18300/v1",
                         "models": ["GLM-5.3-Flash-EXL3"], "type": "local"},
    }
    c.profiles = {"l2": {"chain": [{"provider": "opencode", "model": "glm-5.3-flash"}]}}
    c.server = {"port": 8734}
    return c


def page(cfg, engine, params=None):
    from src.ui.pages import render_providers_page
    return render_providers_page(cfg, engine, params or {})


def _sec(name):
    return open(os.path.join(SECTIONS, name)).read()


# ── 1. one page, four real URLs ─────────────────────────────────────────────

def test_the_tab_strip_is_server_rendered_links(cfg, engine):
    """A tab is a URL: bookmarkable, shareable, reloadable without a `#hash` shim."""
    html = page(cfg, engine)
    strip = html.split('<div class="page-tabs">')[1].split("</div>")[0]
    assert re.findall(r'href="([^"]+)"', strip) == [
        "/providers", "/providers?tab=config",
        "/providers?tab=routing", "/providers?tab=capabilities"]
    assert [m for m in re.findall(r'>([^<]+)</a>', strip)] == [
        "Health", "Configuration", "Routing", "Capabilities"]


def test_the_client_side_tab_switcher_is_gone(cfg, engine):
    """`switchProvTab` + the `#hash` deep link existed to fake a URL client-side.

    Asserted on the SHIPPED page, not the source: the file names the retired helper
    in a comment explaining why it went, and a grep over the source cannot tell a
    comment from code. The served HTML can.
    """
    for pane in ("health", "config", "routing", "capabilities"):
        html = page(cfg, engine, {"tab": pane})
        for dead in ("switchProvTab", "applyHashTab", "window.location.hash"):
            assert dead not in html, (pane, dead)


def test_each_tab_marks_itself_active_and_the_request_decides_which(cfg, engine):
    for params, href in (({}, "/providers"),
                         ({"tab": "config"}, "/providers?tab=config"),
                         ({"tab": "routing"}, "/providers?tab=routing"),
                         ({"tab": "capabilities"}, "/providers?tab=capabilities")):
        html = page(cfg, engine, params)
        active = re.findall(r'class="tab-btn active" href="([^"]+)"', html)
        assert active == [href], params


def test_an_unknown_tab_falls_back_to_health(cfg, engine):
    """"?tab=licenses" has never existed; it lands on the default, never a blank page."""
    html = page(cfg, engine, {"tab": "licenses"})
    assert 'id="providerHealthBody"' in html


def test_the_page_carries_a_breadcrumb(cfg, engine):
    html = page(cfg, engine, {"tab": "config"})
    assert '<nav class="breadcrumbs"' in html
    assert ">Configuration<" in html


# ── 2. one panel, one script per tab ────────────────────────────────────────

PANES = {
    "health": ("providerHealthBody", "phProbeNow", "failoverBody"),
    "config": ("providersBody", "provForm", "provPreset"),
    "routing": ("routingRulesRows", "routingProfile", "routingEnabled"),
    "capabilities": ("capsRows", "capsError"),
}


@pytest.mark.parametrize("pane", sorted(PANES))
def test_a_tab_renders_only_its_own_panel(cfg, engine, pane):
    html = page(cfg, engine, {"tab": pane})
    for other, markers in PANES.items():
        for marker in markers:
            if other == pane:
                assert 'id="%s"' % marker in html, (pane, marker)
            else:
                assert 'id="%s"' % marker not in html, (pane, "leaked", marker)


@pytest.mark.parametrize("pane", sorted(PANES))
def test_a_tab_ships_only_its_own_script(cfg, engine, pane):
    """A symbol is the honest test: it is the code that runs, not the markup."""
    html = page(cfg, engine, {"tab": pane})
    own = {"health": "function phRender(",
           "config": "function saveProvider(",
           "routing": "function renderRoutingRules(",
           "capabilities": "function loadCaps("}[pane]
    assert own in html, (pane, own)
    for other, symbol in (("health", "function phRender("),
                          ("config", "function saveProvider("),
                          ("routing", "function renderRoutingRules("),
                          ("capabilities", "function loadCaps(")):
        if other != pane:
            assert symbol not in html, (pane, "ships", symbol)


def test_the_health_poll_only_starts_where_the_matrix_is(cfg, engine):
    """`loadProviderHealth()` at parse time on another tab polls a missing element."""
    html = page(cfg, engine, {"tab": "config"})
    assert "loadProviderHealth();" not in html


# ── 3. the house UI, not the merged page's inline styling ───────────────────

def test_the_panels_are_cards_and_the_styles_came_out_of_the_markup(cfg, engine):
    """Page-local CSS belongs in a head partial, like every other page here."""
    markup = _sec("sec_providers.html")
    assert "<style>" not in markup, "the section markup carries CSS again"
    head = _sec("sec_providers_head.html")
    assert "<style>" in head
    for rule in (".ph-matrix", ".ph-sum-row", ".ph-tag-warn", ".ph-cellpanel"):
        assert rule in head, rule
    html = page(cfg, engine, {"tab": "routing"})
    assert html.count('class="card"') >= 4, "routing panels must be cards"


def test_the_summary_is_one_card_not_five_boxes(cfg, engine):
    """Five same-weight metric boxes for five integers made nothing important."""
    html = page(cfg, engine)
    assert 'class="card ph-sum"' in html
    assert html.count('class="metric-card') == 0


def test_the_header_widget_is_not_suppressed_on_providers(cfg, engine):
    """It was suppressed on Models because that page was a board. Providers is not."""
    html = page(cfg, engine)
    assert 'id="pluginHeaderBadge"' in html


# ── 4. the nav and the removed page ────────────────────────────────────────

def test_providers_replaced_models_in_the_nav(cfg, engine):
    """The nav entry is Providers, and it is the active one on its own page."""
    nav = page(cfg, engine).split('class="sidebar-nav"')[1].split("</nav>")[0]
    assert 'href="/providers"' in nav
    assert 'href="/models"' not in nav
    assert 'href="/providers" class="active" aria-current="page">Providers<' in nav


def test_the_models_page_template_is_gone_and_the_board_is_not(cfg, engine):
    assert not os.path.exists(os.path.join(TEMPLATES, "pages", "models.html"))
    assert not os.path.exists(os.path.join(SECTIONS, "sec_models.html"))
    assert not os.path.exists(os.path.join(SECTIONS, "sec_models_script.html"))
    # the board itself survives on the tab that owns it
    from src.ui.pages import render_routing_definitions_page
    boards = render_routing_definitions_page(cfg, engine, {"tab": "boards"})
    assert 'id="ltTable"' in boards or 'id="lbTable"' in boards
