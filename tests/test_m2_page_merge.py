"""M2 — the five-page merge.

The control plane went from nine pages to five:

    Profiles   profiles + the API keys scoped to them
    Models     the capability matrix + where those models come from
    Activity   overview + usage + every log realm
    Alerts     unchanged, and deliberately not part of Activity (R10)
    Setup      unchanged

Each merged page is server-side tabbed: the sections live in
``templates/jinja/sections/`` and are guarded by the ``tab`` variable, so only the
active tab's markup and script reach the browser.

Three things could rot silently, so they are pinned here:

1. **the nav shape** — nine control-plane entries creeping back;
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
}


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


# ── the nav ──────────────────────────────────────────────────────────────────

def test_nav_has_five_control_plane_entries(cfg):
    """Five entries, not nine: profiles, models, activity, alerts, setup."""
    from src.ui.render import render_page
    html = render_page("pages/models.html", cfg)
    nav = html.split('<nav class="sidebar-nav">')[1].split("</nav>")[0]
    gateway = nav.split('<div class="nav-label">Gateway</div>')[1]
    hrefs = re.findall(r'href="(/[^"]*)"', gateway)
    assert hrefs == ["/profiles", "/models", "/activity", "/alerts", "/setup"]


def test_nav_does_not_link_the_retired_pages(cfg):
    """The pages that were folded away are gone from the nav entirely."""
    from src.ui.render import render_page
    html = render_page("pages/models.html", cfg)
    nav = html.split('<nav class="sidebar-nav">')[1].split("</nav>")[0]
    for gone in ('href="/keys"', 'href="/providers"', 'href="/usage"',
                 'href="/logs"', 'href="/dashboard"'):
        assert gone not in nav, gone


def test_work_module_keeps_its_own_entries(cfg):
    from src.ui.render import render_page
    html = render_page("pages/models.html", cfg)
    nav = html.split('<nav class="sidebar-nav">')[1].split("</nav>")[0]
    work = nav.split('<div class="nav-label">Work</div>')[1].split('<div class="nav-label">Gateway</div>')[0]
    assert re.findall(r'href="(/work/[^"]*)"', work) == [
        "/work/tasks", "/work/fleet", "/work/cron", "/work/config"]


# ── tabs ─────────────────────────────────────────────────────────────────────

MERGED = [
    ("/profiles", "profiles", lambda c, e: __import__("src.ui.pages", fromlist=["x"]).render_profiles_page(c, e, {})),
    ("/profiles?tab=keys", "keys", lambda c, e: __import__("src.ui.pages", fromlist=["x"]).render_profiles_page(c, e, {"tab": "keys"})),
    ("/models", "matrix", lambda c, e: __import__("src.ui.pages", fromlist=["x"]).render_models_page(c, e, {})),
    ("/models?tab=providers", "providers", lambda c, e: __import__("src.ui.pages", fromlist=["x"]).render_models_page(c, e, {"tab": "providers"})),
    ("/activity", "overview", lambda c, e: __import__("src.ui.pages", fromlist=["x"]).render_activity_page(c, e, {}, {"Host": "test:8734"})),
    ("/activity?tab=usage", "usage", lambda c, e: __import__("src.ui.pages", fromlist=["x"]).render_activity_page(c, e, {"tab": "usage"})),
    ("/activity?tab=logs", "logs", lambda c, e: __import__("src.ui.pages", fromlist=["x"]).render_activity_page(c, e, {"tab": "logs"})),
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
    from src.ui.pages import render_activity_page, render_models_page, render_profiles_page
    for render, params, active_href in (
            (render_profiles_page, {}, "/profiles"),
            (render_profiles_page, {"tab": "keys"}, "/profiles?tab=keys"),
            (render_models_page, {}, "/models"),
            (render_models_page, {"tab": "providers"}, "/models?tab=providers"),
    ):
        html = render(cfg, engine, params)
        assert 'href="%s"' % active_href in html
        assert 'class="tab-btn active"' in html


# ── titles follow the tab ────────────────────────────────────────────────────

@pytest.mark.parametrize("params,title", [
    ({}, "Profiles — LCP"),
    ({"tab": "keys"}, "API Keys — LCP"),
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
    ({"tab": "usage"}, "Usage — LCP"),
    ({"tab": "logs"}, "Logs — LCP"),
])
def test_activity_title_follows_the_tab(cfg, engine, params, title):
    from src.ui.pages import render_activity_page
    html = render_activity_page(cfg, engine, params, {"Host": "test:8734"})
    assert "<title>%s</title>" % title in html


# ── unknown tabs, and the legacy page names ──────────────────────────────────

@pytest.mark.parametrize("page,params,expected", [
    ("profiles", {"tab": "nope"}, "Profiles — LCP"),
    ("models", {"tab": "nope"}, "Models — LCP"),
])
def test_unknown_tab_falls_back_to_the_default(cfg, engine, page, params, expected):
    """A stale bookmark lands somewhere sensible instead of a 404 or a blank page."""
    from src.ui import pages
    render = getattr(pages, "render_%s_page" % page)
    html = render(cfg, engine, params)
    assert "<title>%s</title>" % expected in html


@pytest.mark.parametrize("legacy,tab,href", [
    ("pages/keys.html", "keys", "/profiles?tab=keys"),
    ("pages/providers.html", "providers", "/models?tab=providers"),
    ("pages/usage.html", "usage", None),
    ("pages/logs.html", "logs", None),
])
def test_legacy_page_names_still_resolve(cfg, engine, legacy, tab, href):
    """The pre-merge page names render the merged page's tab, not a 404.

    Kept as an alias table in src.ui.render rather than fixed up at ~40 call
    sites; this pins that the alias keeps pointing at the right section.
    """
    from src.ui.render import render_page
    html = render_page(legacy, cfg, engine)
    for marker in MARKERS[tab]:
        assert 'id="%s"' % marker in html


def test_the_retired_templates_are_really_gone():
    """The merge deleted the old templates — the aliases point at the merged ones."""
    for gone in ("dashboard.html", "usage.html", "logs.html", "providers.html", "keys.html"):
        assert not os.path.exists(os.path.join(TEMPLATES, "pages", gone)), gone
