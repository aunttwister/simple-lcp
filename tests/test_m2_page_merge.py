"""M2 / M2b — the page merge and the M2b reshuffle.

The control plane went from nine pages to five (M2), then the five were reshaped
(M2b): Profiles moved to the top and absorbed Cron and Config, Usage left Activity
and became a page again, the nav lost its group labels and its usage widgets, and
the workspace module stopped being a Setup module.

    Profiles   profiles + the API keys scoped to them + their cron jobs + their paths
    Models     the capability matrix + where those models come from
    Activity   overview + every log realm
    Usage      spend and balances (its own page: a billing view, not a log)
    Alerts     unchanged, and deliberately not part of Activity (R10)
    Setup      unchanged, minus the retired workspace module

Each merged page is server-side tabbed: the sections live in
``templates/jinja/sections/`` and are guarded by the ``tab`` variable, so only the
active tab's markup and script reach the browser.

Three things could rot silently, so they are pinned here:

1. **the nav shape** — retired entries creeping back, group labels returning, or
   the sidebar usage widgets (a second copy of the Usage page) reappearing;
2. **section isolation** — a tab rendering another tab's markup. A guard that
   stopped matching would not raise; the page would just quietly get busier and
   pay for queries it does not show, which is exactly the bug this catches;
3. **the legacy page names** — old links and older call sites still resolving,
   with the tab that now owns that content.
"""
import os
import re

import pytest
from unittest.mock import MagicMock

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "src", "ui", "templates", "jinja")
SECTIONS = os.path.abspath(os.path.join(TEMPLATES, "sections"))

# section partial -> the tab that renders it
SECTION_TAB = {
    "sec_dashboard": "overview",
    "sec_usage": "usage",
    "sec_logs": "logs",
    "sec_models": "matrix",
    "sec_providers": "providers",
    "sec_profiles": "profiles",
    "sec_keys": "keys",
    "sec_cron": "cron",
    "sec_config": "config",
}

NAV = ["/profiles", "/models", "/activity", "/usage", "/alerts", "/setup",
       "/work/tasks", "/work/fleet"]


@pytest.fixture
def engine(temp_db):
    """conftest's ``temp_db`` is a (path, engine) pair."""
    return temp_db[1]


@pytest.fixture
def cfg():
    """Minimal config: two providers, two profiles."""
    c = MagicMock()
    c.providers = {
        "deepseek": {"api_key_env": "DEEPSEEK_API_KEY",
                     "api_base": "https://api.deepseek.com/v1",
                     "models": ["deepseek-chat"]},
    }
    c.profiles = {
        "l2": {"forbidden_tools": ["write_file"], "chain": []},
        "l1": {"forbidden_tools": [], "chain": []},
    }
    c.server = {"port": 8734}
    return c


def _markers():
    """Element ids each section owns, derived from the partials themselves.

    An id owned by more than one section is ambiguous, so it is skipped: this
    measures the real thing rather than a hand-written list that drifts.
    """
    owner = {}
    for fn in sorted(os.listdir(SECTIONS)):
        if fn.endswith("_script.html") or fn.endswith("_head.html"):
            continue
        tab = SECTION_TAB[fn[:-5]]
        with open(os.path.join(SECTIONS, fn)) as fh:
            for i in set(re.findall(r'\bid="([A-Za-z][\w-]*)"', fh.read())):
                owner.setdefault(i, set()).add(tab)
    marks = {}
    for i, tabs in owner.items():
        if len(tabs) == 1:
            marks.setdefault(next(iter(tabs)), []).append(i)
    return {k: sorted(v)[:5] for k, v in marks.items()}


MARKERS = _markers()


def _nav(html):
    return html.split('<nav class="sidebar-nav">')[1].split("</nav>")[0]


# ── the nav ──────────────────────────────────────────────────────────────────

def test_nav_is_flat_and_ordered_with_profiles_first(cfg):
    """Eight entries, no group labels: the label only made the reader classify."""
    from src.ui.render import render_page
    html = render_page("pages/models.html", cfg)
    nav = _nav(html)
    assert re.findall(r'href="(/[^"]*)"', nav) == NAV
    assert "nav-label" not in nav


def test_nav_does_not_link_the_retired_pages(cfg):
    """The pages that were folded away are gone from the nav entirely."""
    from src.ui.render import render_page
    nav = _nav(render_page("pages/models.html", cfg))
    for gone in ('href="/keys"', 'href="/providers"', 'href="/logs"',
                 'href="/dashboard"', 'href="/work/cron"', 'href="/work/config"'):
        assert gone not in nav, gone


def test_nav_has_no_usage_widgets(cfg):
    """The provider-credit submenu and the ↻ refresh button are gone from the nav.

    Both duplicated the Usage page, which owns its own refresh controls, so no
    capability was lost — but the markup, its ids and the JS that filled it must all
    stay gone, or the widget comes back half-alive.
    """
    from src.ui.render import render_page
    html = render_page("pages/models.html", cfg)
    for dead in ("usageSubmenu", "usageChevron", "sbUsageRefreshBtn",
                 "sb-usage-nav", "sb-usage-detail", "plugin-status.js"):
        assert dead not in html, dead


def test_sidebar_js_no_longer_carries_the_usage_widget(cfg):
    """The JS half of the same deletion: functions that can no longer be reached."""
    js = open(os.path.join(TEMPLATES, "static", "js", "sidebar.js")).read()
    for dead in ("sbUsageRefresh", "toggleUsageSubmenu", "initUsageSubmenu",
                 "animateUsageSubmenu", "setSbRefreshIcon"):
        assert dead not in js, dead
    # what the sidebar still does must survive
    assert "toggleSidebar" in js and "sidebarAlertBadge" in js


# ── tabs ─────────────────────────────────────────────────────────────────────

def _pages():
    import src.ui.pages as p
    return p


MERGED = [
    ("/profiles", "profiles", lambda c, e: _pages().render_profiles_page(c, e, {})),
    ("/profiles?tab=keys", "keys", lambda c, e: _pages().render_profiles_page(c, e, {"tab": "keys"})),
    ("/profiles?tab=cron", "cron", lambda c, e: _pages().render_profiles_page(c, e, {"tab": "cron"})),
    ("/profiles?tab=config", "config", lambda c, e: _pages().render_profiles_page(c, e, {"tab": "config"})),
    ("/models", "matrix", lambda c, e: _pages().render_models_page(c, e, {})),
    ("/models?tab=providers", "providers", lambda c, e: _pages().render_models_page(c, e, {"tab": "providers"})),
    ("/activity", "overview", lambda c, e: _pages().render_activity_page(c, e, {}, {"Host": "test:8734"})),
    ("/activity?tab=logs", "logs", lambda c, e: _pages().render_activity_page(c, e, {"tab": "logs"})),
    ("/usage", "usage", lambda c, e: _pages().render_usage_page(c, e, {})),
]


@pytest.mark.parametrize("label,tab,render", MERGED)
def test_tab_renders_its_own_section(cfg, engine, label, tab, render):
    html = render(cfg, engine)
    assert "<!--" not in html[:6]  # rendered a page, not a template error string
    for marker in MARKERS[tab]:
        assert 'id="%s"' % marker in html, "%s missing %s" % (label, marker)


@pytest.mark.parametrize("label,tab,render", MERGED)
def test_tab_does_not_render_other_sections(cfg, engine, label, tab, render):
    """The whole point of the server-side guard: no foreign section markup."""
    html = render(cfg, engine)
    leaked = [("%s#%s" % (other, m)) for other, marks in MARKERS.items() if other != tab
              for m in marks if 'id="%s"' % m in html]
    assert not leaked, "leaked %s" % leaked


def test_every_tab_serves_a_tab_strip(cfg, engine):
    """Each merged page shows its tabs, with the active one marked."""
    from src.ui.pages import render_models_page, render_profiles_page
    for render, params, active_href in (
            (render_profiles_page, {}, "/profiles"),
            (render_profiles_page, {"tab": "keys"}, "/profiles?tab=keys"),
            (render_profiles_page, {"tab": "cron"}, "/profiles?tab=cron"),
            (render_profiles_page, {"tab": "config"}, "/profiles?tab=config"),
            (render_models_page, {}, "/models"),
            (render_models_page, {"tab": "providers"}, "/models?tab=providers"),
    ):
        html = render(cfg, engine, params)
        assert 'href="%s"' % active_href in html
        assert 'class="tab-btn active"' in html


def test_activity_has_exactly_overview_and_logs(cfg, engine):
    """Usage left Activity in M2b — a dead tab must not linger in the strip."""
    from src.ui.pages import render_activity_page
    html = render_activity_page(cfg, engine, {}, {"Host": "test:8734"})
    strip = html.split('<div class="page-tabs">')[1].split("</div>")[0]
    assert re.findall(r'href="([^"]+)"', strip) == ["/activity", "/activity?tab=logs"]


def test_usage_is_a_page_of_its_own(cfg, engine):
    """Outside Activity means: its own nav entry, no Activity tab strip."""
    from src.ui.pages import render_usage_page
    html = render_usage_page(cfg, engine, {})
    assert "<h1>Usage</h1>" in html
    assert "page-tabs" not in html
    assert 'href="/activity"' in html  # ...but still reachable from the nav


def test_profiles_cron_tab_filters_to_one_profile(cfg, engine):
    """``?profile=`` narrows the cross-profile cron view to that profile's jobs."""
    from src.ui.pages import render_profiles_page
    view = _pages_module()._work_cron_view({"profile": "l2"})
    assert view.get("profile_filter") == "l2"
    assert all(str(p.get("profile", "")).lower() == "l2" for p in view.get("profiles") or [])
    # and the tab still renders
    html = render_profiles_page(cfg, engine, {"tab": "cron", "profile": "l2"})
    assert 'id="cron-modal"' in html


def _pages_module():
    import src.ui.pages as p
    return p


# ── titles follow the tab ────────────────────────────────────────────────────

@pytest.mark.parametrize("params,title", [
    ({}, "Profiles — LCP"),
    ({"tab": "keys"}, "API Keys — LCP"),
    ({"tab": "cron"}, "Cron — LCP"),
    ({"tab": "config"}, "Config — LCP"),
])
def test_profiles_title_follows_the_tab(cfg, engine, params, title):
    from src.ui.pages import render_profiles_page
    html = render_profiles_page(cfg, engine, params)
    assert "<title>%s</title>" % title in html


@pytest.mark.parametrize("params,title", [
    ({}, "Models — LCP"),
    ({"tab": "providers"}, "Providers — LCP"),
])
def test_models_title_follows_the_tab(cfg, engine, params, title):
    from src.ui.pages import render_models_page
    html = render_models_page(cfg, engine, params)
    assert "<title>%s</title>" % title in html


@pytest.mark.parametrize("params,title", [
    ({}, "Activity — LCP"),
    ({"tab": "logs"}, "Logs — LCP"),
])
def test_activity_title_follows_the_tab(cfg, engine, params, title):
    from src.ui.pages import render_activity_page
    html = render_activity_page(cfg, engine, params, {"Host": "test:8734"})
    assert "<title>%s</title>" % title in html


def test_usage_page_title(cfg, engine):
    from src.ui.pages import render_usage_page
    assert "<title>Usage — LCP</title>" in render_usage_page(cfg, engine, {})


# ── unknown tabs, and the legacy page names ──────────────────────────────────

@pytest.mark.parametrize("page,params,expected", [
    ("profiles", {"tab": "nope"}, "Profiles — LCP"),
    ("models", {"tab": "nope"}, "Models — LCP"),
    ("activity", {"tab": "usage"}, "Activity — LCP"),  # the dead tab falls back
])
def test_unknown_tab_falls_back_to_the_default(cfg, engine, page, params, expected):
    """A stale bookmark lands somewhere sensible instead of a 404 or a blank page."""
    from src.ui import pages
    render = getattr(pages, "render_%s_page" % page)
    html = render(cfg, engine, params, {"Host": "test:8734"}) if page == "activity" \
        else render(cfg, engine, params)
    assert "<title>%s</title>" % expected in html


@pytest.mark.parametrize("legacy,tab", [
    ("pages/keys.html", "keys"),
    ("pages/providers.html", "providers"),
    ("pages/logs.html", "logs"),
])
def test_legacy_page_names_still_resolve(cfg, engine, legacy, tab):
    """The pre-merge page names render the owning page's tab, not a 404.

    Kept as an alias table in src.ui.render rather than fixed up at ~40 call
    sites; this pins that the alias keeps pointing at the right section.
    """
    from src.ui.render import render_page
    html = render_page(legacy, cfg, engine)
    for marker in MARKERS[tab]:
        assert 'id="%s"' % marker in html


def test_the_dashboard_alias_renders_with_its_own_context(cfg, engine):
    """pages/dashboard.html is aliased to Activity's overview, and the overview
    needs the dashboard context — so the alias is only valid for a caller that
    supplies it (render_dashboard does). This pins that real path rather than a
    bare-name call no caller makes.
    """
    from src.ui.dashboard import render_dashboard
    html = render_dashboard(cfg, engine, {}, None)
    for marker in MARKERS["overview"]:
        assert 'id="%s"' % marker in html


def test_usage_is_not_aliased_away(cfg, engine):
    """Regression: pages/usage.html is a REAL template again (M2b).

    While the alias table still listed it, render_page rewrote the name to
    Activity — whose allowed tabs no longer include "usage" — so /usage silently
    served the overview with a 200. Asserting the content, not the status.
    """
    from src.ui.render import render_page
    html = render_page("pages/usage.html", cfg, engine, tab="usage")
    assert "<h1>Usage</h1>" in html
    for marker in MARKERS["usage"]:
        assert 'id="%s"' % marker in html


def test_the_retired_templates_are_really_gone():
    """The merge deleted the old templates — the aliases point at the survivors."""
    for gone in ("dashboard.html", "logs.html", "providers.html",
                 "keys.html", "work_cron.html", "work_config.html"):
        assert not os.path.exists(os.path.join(TEMPLATES, "pages", gone)), gone


def test_the_legacy_work_routes_render_the_profiles_tab(cfg, engine):
    """render_work_cron_page/config_page are aliases onto the Profiles page now."""
    from src.ui.pages import render_work_config_page, render_work_cron_page
    for render, marker, title in ((render_work_cron_page, "cron-modal", "Cron — LCP"),
                                  (render_work_config_page, "sources-form", "Config — LCP")):
        html = render(cfg, engine, {})
        assert "<title>%s</title>" % title in html
        assert 'id="%s"' % marker in html
