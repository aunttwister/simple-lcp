"""`Routing definitions` — the page the operator expected in the navbar, and the
home the capability-matrix editor needs before prod can take the cut.

Why each assertion is a rendered-markup fact rather than a template grep: on
2026-11-17 the prod-cut assessment found that the staging line had **removed** the
capability-matrix editor from the UI (`Add score` / `Edit` / `Delete` → 0 hits) with
no replacement, because the `Routing definitions` page was designed (design doc §13
step 3) and never built. These tests pin the SURFACE, against the real renderers, so
"the editor is reachable" is checkable rather than assumed.

The move is deliberate and verbatim: prod's matrix partial is lifted unchanged into
tab 1, so the editor the operator already uses keeps working exactly as it does.
"""
from __future__ import annotations

import os

import pytest
from unittest.mock import MagicMock

REPO = os.path.join(os.path.dirname(__file__), "..")


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


def _render(cfg, engine, tab=None):
    from src.ui.pages import render_routing_definitions_page
    params = {"tab": tab} if tab else {}
    return render_routing_definitions_page(cfg, engine, params)


# ── the page exists, and it is in the navbar ──────────────────────────────────

def test_the_page_renders(cfg, engine):
    html = _render(cfg, engine)
    assert html and len(html) > 2000, "page did not render"
    assert "Routing definitions" in html


def test_the_navbar_links_to_it(cfg, engine):
    """The operator looked for this in the navbar and found nothing."""
    html = _render(cfg, engine)
    assert 'href="/routing-definitions"' in html, "no navbar entry for the new page"


def test_the_route_is_registered():
    """A page that renders but has no route is a 404 with extra steps."""
    import re
    src = open(os.path.join(REPO, "src", "server", "handler.py")).read()
    assert 'exact("/routing-definitions")' in src, "route not registered in the table"
    src_ep = open(os.path.join(REPO, "src", "server", "endpoints.py")).read()
    assert "_serve_routing_definitions_page" in src_ep, "no serving method"


# ── the three tabs the design specifies ───────────────────────────────────────

def test_the_three_tabs_are_present(cfg, engine):
    """Three tabs, one per vocabulary. The DEFAULT tab links to the bare URL, so it
    carries no query param — assert the link targets, not a literal string."""
    html = _render(cfg, engine)
    for label in ("Capability matrix", "Arena boards", "Unmapped"):
        assert label in html, f"tab strip is missing {label}"
    assert 'href="/routing-definitions"' in html, "matrix (the default) has no link"
    for tab in ("boards", "unmapped"):
        assert f"tab={tab}" in html, f"tab strip is missing the {tab} link"


def test_tab_defaults_to_the_matrix(cfg, engine):
    """The editor is the reason this page exists; it must be what you land on."""
    html = _render(cfg, engine, "matrix")
    assert "modelList" in html and "modelDetail" in html


def test_an_unknown_tab_falls_back_rather_than_erroring(cfg, engine):
    html = _render(cfg, engine, "nonsense")
    assert "modelList" in html


# ── tab 1: the capability editor, moved from prod ─────────────────────────────

def test_matrix_tab_carries_the_capability_editor(cfg, engine):
    """Prod's editor controls, verbatim — this is what the cut would have removed."""
    html = _render(cfg, engine, "matrix")
    for control in ("modelFilter", "addRegistryBtn", "configuredOnlyBtn",
                    "modelList", "modelDetail", "editModelModal",
                    "registerModelModal", "saveModelBtn", "saveRegisterBtn"):
        assert control in html, f"capability editor is missing {control}"


def test_matrix_tab_is_the_capability_matrix_not_the_leaderboard(cfg, engine):
    html = _render(cfg, engine, "matrix")
    assert "Capability scores drive dynamic routing" in html
    assert "lbTable" not in html, "the leaderboard leaked into the matrix tab"


# ── tab 2: the Arena board catalogue ─────────────────────────────────────────

def test_boards_tab_lists_every_stored_board(cfg, engine):
    """In use / not used, ours-of-stored, publish date — the catalogue the design
    specifies. Read-only: polling stays where it works (see the deferral test)."""
    html = _render(cfg, engine, "boards")
    for control in ("boardCatList", "boardCatNote"):
        assert control in html, f"boards tab is missing {control}"
    assert "/api/model-ratings/catalogue" in html, "catalogue is not what it reads"


def test_the_boards_tab_says_where_the_poll_is(cfg, engine):
    """A catalogue with no pointer to the poll is a dead end for the operator."""
    html = _render(cfg, engine, "boards")
    assert "Models" in html and "Poll" in html


def test_the_page_has_a_breadcrumb(cfg, engine):
    """`_crumbs()` builds {label, href} dicts; passing raw tuples renders nothing —
    which is a silent blank in the header, not an error, so it needs a test."""
    for tab in ("matrix", "boards", "unmapped"):
        html = _render(cfg, engine, tab)
        assert "Routing definitions" in html
        # the terminus crumb differs per tab, so the trail proves the dicts arrived
        label = {"matrix": "Capability matrix", "boards": "Arena boards",
                 "unmapped": "Unmapped"}[tab]
        assert f">{label}<" in html, f"breadcrumb terminus missing on {tab}"


# ── tab 3: the unmapped resolution queue ──────────────────────────────────────

def test_unmapped_tab_renders_its_own_surface(cfg, engine):
    html = _render(cfg, engine, "unmapped")
    assert "unmappedList" in html or "unmappedPane" in html


# ── Models is now the leaderboard ONLY (the design's division) ────────────────

def test_models_page_keeps_the_leaderboard(cfg, engine):
    from src.ui.pages import render_models_page
    html = render_models_page(cfg, engine, {})
    assert "lbTable" in html or "leaderboardPane" in html


def test_the_poll_is_still_reachable_while_its_move_is_deferred(cfg, engine):
    """DEFERRED, deliberately, and this test is what stops "deferred" becoming "lost".

    Design §10 wants the poll on Routing definitions → Arena boards. It has NOT moved
    yet, because that page's leaderboard, poll and taxonomy map share ONE <script>
    block whose first-paint calls back into the leaderboard's tab strip and board
    loader (#mlTabs / #lbTable). Splitting those helpers is its own task; doing it as a
    side effect of building this page would have risked the poll the operator uses.

    So: the editor moved (that was the blocker), the catalogue is new, and the poll
    must remain reachable on Models until its move is done properly.
    """
    from src.ui.pages import render_models_page
    html = render_models_page(cfg, engine, {})
    assert "pollRunBtn" in html and "pollPane" in html, (
        "the poll vanished from Models AND has not moved to Routing definitions — "
        "it is now unreachable")


# ── the editor's own API must still exist (it is what makes tab 1 work) ───────

def test_the_capability_api_is_still_registered():
    src = open(os.path.join(REPO, "src", "server", "handler.py")).read()
    for route in ("api.models.capability", "api.models.registry"):
        assert route in src, f"{route} disappeared — tab 1 would render but do nothing"
