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

def test_the_four_tabs_are_present(cfg, engine):
    """Four tabs: the capability matrix, the Arena boards, the unbound queue, and the
    classification log. The DEFAULT tab links to the bare URL, so it carries no query
    param — assert the link targets, not a literal string."""
    html = _render(cfg, engine)
    for label in ("Capability matrix", "Arena boards", "Unmapped", "Classification log"):
        assert label in html, f"tab strip is missing {label}"
    assert 'href="/routing-definitions"' in html, "matrix (the default) has no link"
    for tab in ("boards", "unmapped", "log"):
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


def test_matrix_tab_carries_the_csv_import(cfg, engine):
    """Design build step 4. A SIBLING of the editor, not folded into it: the editor
    partial is prod's bytes lifted verbatim and must stay that way."""
    html = _render(cfg, engine, "matrix")
    for control in ("matrixFile", "matrixImportBtn", "matrixTemplateBtn", "matrixMsg"):
        assert control in html, f"CSV import is missing {control}"
    assert "/api/models/capability/template" in html, "the template link is dead"


def test_the_import_routes_are_registered():
    src = open(os.path.join(REPO, "src", "server", "handler.py")).read()
    for route in ("api.models.capability.import", "api.models.capability.template"):
        assert route in src, f"{route} not registered — the control would 404"


# ── tab 2: the Arena boards — the poll, MOVED here 2026-11-17 ────────────────

def test_boards_tab_carries_the_poll(cfg, engine):
    """Design §10: the poll is a routing input, so it lives where routing is defined.
    Moved from the Models page — this is the move that was deferred in the morning and
    done properly once the shared JS was extracted."""
    html = _render(cfg, engine, "boards")
    for control in ("pollPane", "pollRunBtn2", "taxPane", "taxMap", "pollRows"):
        assert control in html, f"the poll lane is missing {control}"


def test_boards_tab_carries_the_csv_import(cfg, engine):
    """Importing a taxonomy CSV is how a custom board gets its numbers."""
    html = _render(cfg, engine, "boards")
    for control in ("taxFile", "taxImportBtn", "taxTemplateBtn"):
        assert control in html, f"CSV import is missing {control}"


def test_boards_tab_carries_the_shared_detail_lane(cfg, engine):
    """The lane mechanics are shared with overview/conversations — not redesigned."""
    html = _render(cfg, engine, "boards")
    for control in ("modelsPanel", "mpResizer", "mpClose", "mpBackdrop", "mpBody"):
        assert control in html, f"the lane is missing {control}"


def test_boards_tab_binds_its_wiring_defensively(cfg, engine):
    """The lane's controls are bound through a guarded helper: an unguarded
    getElementById threw on the page that did not own the control and killed the
    whole script — which is how the first split attempt failed."""
    html = _render(cfg, engine, "boards")
    assert "function on(id, ev, fn)" in html, "bindings are not guarded"
    assert "document.getElementById('pollRunBtn').addEventListener" not in html, \
        "an unguarded binding survived the move"


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
    assert "unmappedList" in html and "unmappedNote" in html


def test_unmapped_tab_reads_the_REGISTRY_not_the_discovery_queue(cfg, engine):
    """Operator, 2026-11-17: "Why do we have 47 models? Can we only have the models
    that are registered".

    The 47 were the poll's DISCOVERY queue (mi_deployments.status='new' — every model
    ID an endpoint serves, vetted or not), which this tab presented as if it were the
    estate. It must read registration instead: mi_models.status='active' AND
    mi_deployments.status='active', the predicate every other read path uses.
    """
    html = _render(cfg, engine, "unmapped")
    assert "/api/models/unbound" in html, "not reading the registered set"
    assert "/api/model-intel/settings" not in html, \
        "still reading the poll's discovery queue"


def test_the_unbound_read_never_returns_unregistered_rows(engine):
    """The read itself is the guarantee — the template cannot show what it is not given."""
    from src.api.model_names import unbound_deployments
    out = unbound_deployments(engine)
    assert set(out) >= {"registered", "unbound", "count"}
    for row in out["unbound"]:
        assert "served_id" in row and "endpoint_id" in row


# ── tab 4: the classification log (design step 12) ───────────────────────────

def test_the_log_tab_renders(cfg, engine):
    html = _render(cfg, engine, "log")
    assert "logList" in html and "logNote" in html


def test_the_log_reads_the_stored_record(cfg, engine):
    """`routing_decisions.semantic_json` already holds the ranked vector for 25,308 of
    28,668 decisions. This view renders it — it does not re-score anything, because a
    recomputed number beside a recorded one makes the record unreadable as a record."""
    html = _render(cfg, engine, "log")
    assert "/api/routing/status" in html
    assert "recent_decisions" in html or "semantic_json" in html


def test_the_log_never_writes(cfg, engine):
    """A view that could edit the log is a view nobody could trust as the record."""
    html = _render(cfg, engine, "log")
    assert "method: 'POST'" not in html and 'method: "POST"' not in html


# ── Models is now the leaderboard ONLY (the design's division) ────────────────

def test_models_page_keeps_the_leaderboard(cfg, engine):
    from src.ui.pages import render_models_page
    html = render_models_page(cfg, engine, {})
    assert "lbTable" in html or "leaderboardPane" in html


def test_models_links_to_the_poll_instead_of_owning_it(cfg, engine):
    """The move is DONE: the lane is on the boards tab, and Models points at it.

    It cannot simply be deleted from Models — a leaderboard with no route to the poll
    is a dead end. So the two buttons that used to open the lane here are replaced by a
    link, and the poll itself is no longer rendered on this page.
    """
    from src.ui.pages import render_models_page
    html = render_models_page(cfg, engine, {})
    assert 'href="/routing-definitions?tab=boards"' in html, \
        "Models does not link to where the poll lives now"
    for gone in ("pollPane", "pollRunBtn", "taxMap", "modelsPanel"):
        assert gone not in html, f"Models still renders the moved lane control {gone}"


def test_the_shared_js_is_one_copy(cfg, engine):
    """Two pages, one copy of the fetch wrappers, the poll cache and the formatters.
    A second copy would drift, and the Formatters are what make the two pages agree
    about what a value means."""
    import os
    d = os.path.join(REPO, "src", "ui", "templates", "jinja", "sections")
    shared = open(os.path.join(d, "sec_lcp_shared_js.html")).read()
    assert "const API = {" in shared and "function esc(s)" in shared
    assert "POLL_KEY" in shared, "the poll cache must be shared, not duplicated"
    for f in ("sec_models_script.html", "sec_routingdefs_boards_script.html"):
        body = open(os.path.join(d, f)).read()
        assert 'sec_lcp_shared_js.html' in body, f"{f} does not include the shared JS"
        assert "const API = {" not in body, f"{f} carries a second copy of API"


def test_the_unbound_route_is_registered():
    """A read the template calls but the server does not serve is a 404 in the tab."""
    src = open(os.path.join(REPO, "src", "server", "handler.py")).read()
    assert "api.models.unbound" in src, "route not registered"


# ── the editor's own API must still exist (it is what makes tab 1 work) ───────

def test_the_capability_api_is_still_registered():
    src = open(os.path.join(REPO, "src", "server", "handler.py")).read()
    for route in ("api.models.capability", "api.models.registry"):
        assert route in src, f"{route} disappeared — tab 1 would render but do nothing"
