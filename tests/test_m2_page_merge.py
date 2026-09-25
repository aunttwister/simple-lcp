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
    # M2c: these three only ever render on /profiles/<name>, never on a level-2 page.
    "sec_skills": "skills",
    "sec_memory": "memory",
    "sec_ptasks": "tasks",
    # M2d: likewise level-3 only — the routing controls and the model pool act on one
    # profile, which is what the level-3 page is.
    "sec_prouting": "routing",
    "sec_pmodels": "models",
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
    # M2c: keys and cron moved under the profile. The old tab params are not a
    # 404 — they fall back to the profile directory, which is where those keys and
    # jobs are now reached from.
    ("/profiles?tab=keys", "profiles", lambda c, e: _pages().render_profiles_page(c, e, {"tab": "keys"})),
    ("/profiles?tab=cron", "profiles", lambda c, e: _pages().render_profiles_page(c, e, {"tab": "cron"})),
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


def test_profile_cron_tab_is_scoped_to_its_profile(cfg, engine, cron_snapshot):
    """The cron tab of a profile shows that profile's jobs, not the cross-profile view.

    M2c moved cron under the profile, so the view is always narrowed now — the
    ``?profile=`` narrowing this test used to pin is built in rather than optional.
    """
    from src.ui.pages import render_profile_detail_page
    view = _pages_module()._work_cron_view({"profile": "l2"})
    assert view.get("profile_filter") == "l2"
    assert all(str(p.get("profile", "")).lower() == "l2" for p in view.get("profiles") or [])
    html = render_profile_detail_page(cfg, engine, "l2", {"tab": "cron"})
    assert 'id="cron-modal"' in html
    assert "Cron jobs for <b>l2</b>" in html


def test_the_cron_tab_says_so_when_the_snapshot_is_missing(cfg, engine, monkeypatch):
    """The other half of the contract: no snapshot means the tab says so.

    This is the state a fresh install is in before the host-side collector has run,
    and it is what a CI runner always sees. Pinned deliberately, because the test
    above spent eleven pushes asserting the opposite without saying so.

    The missing path is forced rather than assumed: asserting "there is no snapshot"
    by relying on the dev box not having one would be the same environment coupling
    that caused the red CI in the first place.
    """
    from src.api import work_cron
    from src.ui.pages import render_profile_detail_page
    monkeypatch.setattr(work_cron, "_snapshot_path", lambda: "/nonexistent/cron-jobs.json")
    html = render_profile_detail_page(cfg, engine, "l2", {"tab": "cron"})
    assert "snapshot missing" in html
    # and it does not pretend to be showing jobs it never read
    assert 'id="cron-modal"' not in html


def _pages_module():
    import src.ui.pages as p
    return p


# ── the Tasks page must survive having nothing to show ───────────────────────

def test_an_empty_task_tree_renders_the_page_instead_of_500ing(tmp_path, monkeypatch, cfg, engine):
    """A present-but-empty tree is a normal state, and the page must render it.

    ``tasks_view`` returned a shape without ``filter`` on this path, while
    ``sec_ptasks.html`` reads ``view.filter.states_options`` — an attribute lookup on
    an undefined value, which is the one thing Jinja raises on. So the Tasks page
    500'd on an empty tree, and the "never blank the page on a data error" guard
    could not catch it, because nothing had raised where the guard was looking.
    Found 2026-09-25 while reproducing a red CI run.
    """
    from src.api import work_tasks
    from src.ui.pages import render_work_tasks_page
    monkeypatch.setenv("LCP_WORK_TASKS_DIR", str(tmp_path))
    view = work_tasks.tasks_view({})
    assert view["total"] == 0
    assert view["filter"]["states_options"]     # exactly what the template dereferences
    html = render_work_tasks_page(cfg, engine, {})
    # The tree exists but holds nothing, so `available` is true and the page renders
    # its normal chrome with zero rows. The point of the assertion is that the filter
    # was iterated at all: `view.filter.states_options` is the lookup that used to
    # raise, and it only exists on this branch.
    assert 'data-state="new"' in html
    assert 'id="task-count-line"' in html


def test_the_failed_read_fallback_renders_the_same_shape(cfg, engine, monkeypatch):
    """The other half: when the read *raises*, the fallback must still render.

    The guard that catches the read error used to build its own view by hand, with a
    different shape from the real one — so it turned a handled error into a 500. Both
    callers now build the view in one place.
    """
    from src.api import work_tasks
    from src.ui.pages import render_work_tasks_page

    def _boom(_params=None):
        raise OSError("task tree unreadable")

    monkeypatch.setattr(work_tasks, "tasks_view", _boom)
    html = render_work_tasks_page(cfg, engine, {})
    assert "could not read the task tree" in html
    assert "OSError" in html


# ── titles follow the tab ────────────────────────────────────────────────────

@pytest.mark.parametrize("params,title", [
    ({}, "Profiles — LCP"),
    ({"tab": "config"}, "Config — LCP"),
    ({"tab": "keys"}, "Profiles — LCP"),   # retired tab -> the directory
    ({"tab": "cron"}, "Profiles — LCP"),
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
    ("pages/keys.html", "profiles"),   # M2c: keys are per profile now
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


def test_the_legacy_work_routes_render_the_profiles_page(cfg, engine):
    """render_work_cron_page/config_page are aliases onto the Profiles page.

    M2c: cron is a per-profile tab, so the cron alias lands on the profile
    directory (the HTTP route 302s there). Config is still a level-2 tab.
    """
    from src.ui.pages import render_work_config_page, render_work_cron_page
    for render, marker, title in ((render_work_cron_page, "profileEditModal", "Profiles — LCP"),
                                  (render_work_config_page, "sources-form", "Config — LCP")):
        html = render(cfg, engine, {})
        assert "<title>%s</title>" % title in html
        assert 'id="%s"' % marker in html
