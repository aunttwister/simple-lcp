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
    """2026-11-18: the tab's column 2 IS the comparison ("go for B"): rows are the axes,
    columns the models. Not the leaderboard — that lives on Arena boards and Models."""
    html = _render(cfg, engine, "matrix")
    assert 'id="matrixCompare"' in html, "the comparison matrix is not on the matrix tab"
    assert "One row per axis, one column per model" in html
    assert 'id="matrixHead"' in html and 'id="matrixBody"' in html
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


def test_the_model_list_shows_ONLY_registered_models(cfg, engine):
    """Operator, 2026-11-18: "this list is broken".

    It showed 19 models of which 10 were not registered. The list unions three sources
    and NONE of them is a registration: `mi_models` carries retired rows, the provider
    config carries configured-but-unserved names, and the matrix carries scores for
    models retired since — and the detail pane opened on `claude-5-opus-thinking`,
    which is not registered. Prod's editor never had the rule because it was moved
    byte-for-byte; this pins it on the move.
    """
    html = _render(cfg, engine, "matrix")
    assert "STATE.registered" in html, "the list does not consult the registered set"
    assert "registered" in html and "canonicalName" in html
    # and the rule is stated where a reader will find it
    assert "only models a provider CONFIRMS" in html


def test_the_registered_set_arrives_with_the_scores(cfg, engine):
    """One payload carries both, so the page cannot disagree with the read paths: the
    scores (what the router reads) and the displayable set (the operator's rule).
    Reporting it as a SEPARATE list, not as a filter, keeps a still-in-force score
    visible to the router while the page refuses to display its model."""
    src = open(os.path.join(REPO, "src", "server", "endpoints.py")).read()
    block = src.split("def _serve_capability_api", 1)[1].split("def _serve_registry_api", 1)[0]
    assert "visible_models" in block, "the registered set is not the shared predicate"
    assert '"registered": registered' in block, "the payload does not carry it"


def test_the_list_does_not_hide_everything_when_there_is_no_registry(cfg, engine):
    """An unmigrated DB returns no registered set; the list must fall back rather than
    render empty, which would read as 'all my models disappeared'."""
    html = _render(cfg, engine, "matrix")
    assert "if (!reg || !reg.size) return sorted;" in html


def test_a_model_appears_as_ONE_column_however_many_spellings_it_has(cfg, engine):
    """Found on prod 2026-11-19 by reading the RENDERED table: `qwen-3.8-max` and
    `qwen3.8-max` both rendered as columns, the same six numbers twice.

    They are one model — the hyphenated name is a provider spelling that resolves to the
    registered slug through the alias map. Both legitimately PASS the registration filter
    (an alias of a registered model is registered), so filtering cannot be the fix: the
    columns have to be keyed on the model's IDENTITY, not on the name that reported it.

    No unit test caught it because every one of them inspects a single partial and none of
    them ever saw two spellings of one model side by side.
    """
    script = open(os.path.join(REPO, "src", "ui", "templates", "jinja", "sections",
                              "sec_routingdefs_matrix_script.html")).read()
    assert "function columnGroups(" in script, "the columns are not grouped by identity"
    assert "compareModels(" not in script, "the un-deduped grouping is still in use"
    # the identity is the canonical name, and it is applied everywhere a model is named
    assert "function compareId(" in script
    assert "const id = compareId(m);" in script, "grouping does not use the canonical id"
    assert "const cols = groups.filter(g => STATE.compare.has(g.id));" in script
    assert "STATE.compare.has(compareId(m))" in script, \
        "the list's tick is not keyed on the same identity as the column"
    assert "const id = compareId(model);" in script, \
        "ticking a name does not record the canonical id"
    # one column per identity: the group takes the REGISTERED spelling's own number
    assert "if (isReg(m) && !g.regName) g.regName = m;" in script
    assert "a[0] - b[0] || a[1].localeCompare(b[1])" in script, \
        "which spelling wins is left to payload order"
    # and the column says which spellings it stands for
    assert "g.names.join(' / ')" in script


# ── the lane's mechanics: the four defects reported 2026-11-19 ─────────────────

def _js_section(name):
    return open(os.path.join(REPO, "src", "ui", "templates", "jinja", "sections", name)).read()


def test_the_lane_does_not_call_itself_back(cfg, engine):
    """Operator, 2026-11-19: "Manage Taxonomy and Import scores doesn't work, nothing opens
    in column 3".

    The pair recursed: mpShow() called mpSyncViewport(), which calls mpShow() on desktop, so
    every pane button died inside `RangeError: Maximum call stack size exceeded`. The symptom
    was silent — no console error reached the page's own handler, the pane simply never
    changed, and each nested call re-applied the OLD pane on its way back out, which is why
    ✕ also appeared to do nothing but close the width.

    This asserts the DIRECTION: mpShow must not call mpSyncViewport. The pre-rework code
    never did; the call was added when the function was generalised.
    """
    js = _js_section("sec_lane_js.html")
    show = js.split("function mpSetPane(pane) {", 1)[1].split("\nfunction mpShow(", 1)[0]
    assert "mpSyncViewport(" not in show, \
        "mpShow calls mpSyncViewport, which calls mpShow: unbounded recursion"
    # and the reverse direction is the one that is allowed
    sync = js.split("function mpSyncViewport(mq) {", 1)[1]
    assert "mpSetPane(" in sync or "mpShow(" in sync


def test_closing_the_lane_clears_what_was_open(cfg, engine):
    """Operator: "the models which was open before i clicked X remains (it should disappear
    and default column 3 no info should be shown)".

    ✕ must EMPTY the lane, not just release its width — and it must not remove the reserved
    column on desktop, where the operator's own rule is that column 2 never reflows.
    """
    js = _js_section("sec_lane_js.html")
    close = js.split("function mpClose() {", 1)[1].split("\n}", 1)[0]
    assert "mpClear()" in close, "✕ does not clear the lane"
    assert "mpHide()" in close and "MP_DESKTOP.matches" in close, \
        "✕ must close the slide-over only BELOW the desktop breakpoint"
    # the empty state exists in both lanes, and mpSetPane is what drives it
    assert "mpEmpty" in js and "empty.hidden = !!pane" in js
    assert 'id="mpEmpty"' in _js_section("sec_routingdefs_matrix.html")
    assert 'id="mpEmpty"' in _js_section("sec_routingdefs_boards.html")


def test_the_desktop_lane_has_no_full_screen_backdrop(cfg, engine):
    """The 2026-11-07 bug, which the generalisation reintroduced: a backdrop over the reserved
    desktop lane swallows clicks aimed at column 2, so the panel looked like a modal."""
    js = _js_section("sec_lane_js.html")
    body = js.split("function mpSetPane(pane) {", 1)[1].split("\nfunction mpShow(", 1)[0]
    assert "classList.toggle('open', !MP_DESKTOP.matches)" in body, \
        "the backdrop is not scoped to the sub-1100px overlay"


def test_the_highest_score_on_a_row_is_actually_styled(cfg, engine):
    """Operator: "we can highlight the highest number in capability matrix just to catch the
    attention easier".

    The markup marked it (`.matrix-cell.best`) from the start and the stylesheet had NO rule
    for it — or for any other matrix class — so the marking was invisible on screen and the
    table did not read as a table at all. A class in the DOM is not a highlight.
    """
    css = open(os.path.join(REPO, "src", "ui", "templates", "jinja", "static",
                            "dashboard.css")).read()
    for rule in ("#matrixCompare td.matrix-cell.best", "#matrixCompare th.matrix-col",
                 "#matrixCompare td.matrix-cell", "#matrixCompare th.matrix-axis"):
        assert rule in css, "no stylesheet rule for %s" % rule
    best = css.split("#matrixCompare td.matrix-cell.best", 1)[1].split("}", 1)[0]
    # weight + wash, never colour alone: the house rule for a colour-blind reader
    assert "font-weight" in best and "background" in best


def test_the_configured_filter_also_filters_the_matrix(cfg, engine):
    """Operator: "'Configured' filter should also filter only configured models in the
    matrix". It narrowed the LIST only, so the table beside it kept showing models the filter
    had just excluded — the two surfaces disagreed on screen."""
    script = _js_section("sec_routingdefs_matrix_script.html")
    assert "function passesConfigured(" in script
    assert "function matrixVisible(" in script
    assert "if (!matrixVisible(m)) continue;" in script, \
        "the matrix does not honour the Configured filter"
    # and the toggle re-renders the table, not just the list
    toggle = script.split("STATE.configuredOnly = !STATE.configuredOnly;", 1)[1].split("\n}", 1)[0]
    assert "renderMatrix()" in toggle, "the toggle does not re-render the matrix"
    assert "renderModelList()" in toggle
    # the count says which set it counted
    assert "configured' : ' registered') + ' model(s) compared" in script


# ── tab 2: the Arena boards — the poll, MOVED here 2026-11-17 ────────────────

def test_boards_tab_carries_the_poll(cfg, engine):
    """Design §10: the poll is a routing input, so it lives where routing is defined.
    Moved from the Models page — this is the move that was deferred in the morning and
    done properly once the shared JS was extracted."""
    html = _render(cfg, engine, "boards")
    for control in ("pollPane", "pollRunBtn2", "pollRows"):
        assert control in html, f"the poll lane is missing {control}"
    # Operator, 2026-11-18: "arena boards should have poll data in 3rd column, NOT manage
    # taxonomy" — the taxonomy pane and its map left this tab.
    for gone in ("taxPane", "taxMap", "taxFile"):
        assert gone not in html, f"taxonomy control {gone} is still on the boards tab"


def test_the_import_lives_on_the_matrix_tab_not_the_boards_tab(cfg, engine):
    """Operator, 2026-11-18: "import should be a button on 2nd column and when clicked it
    should open up the details in 3rd column" — so it is the matrix's, and it is gone here."""
    boards = _render(cfg, engine, "boards")
    for gone in ("taxFile", "taxImportBtn", "matrixFile", "importOpenBtn"):
        assert gone not in boards, f"the import is still on the boards tab: {gone}"
    matrix = _render(cfg, engine, "matrix")
    for control in ("importOpenBtn", "matrixFile", "taxImportBtn", "taxFile",
                    "taxTemplateBtn"):
        assert control in matrix, f"CSV import is missing from the matrix tab: {control}"


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

def test_the_board_lives_on_the_boards_tab_not_a_models_page(cfg, engine):
    """CUR-358: the Models page is gone; the board is THIS tab's own surface."""
    from src.ui.pages import render_routing_definitions_page
    html = render_routing_definitions_page(cfg, engine, {"tab": "boards"})
    assert "lbTable" in html or "leaderboardPane" in html
    assert "providerHealthBody" not in html, "the board is not the Providers page"


def test_the_boards_tab_owns_the_poll_and_its_lane(cfg, engine):
    """The move is DONE and the old page is gone (CUR-358).

    The leaderboard and the poll that fills it are one tab: the board is column 2 and
    the lane is the poll it opens. What must stay true is that the board never appears
    without its lane, and that no second page renders either.
    """
    from src.ui.pages import render_routing_definitions_page
    html = render_routing_definitions_page(cfg, engine, {"tab": "boards"})
    assert 'id="lbTable"' in html
    assert 'id="pollPane"' in html and 'id="pollOpenBtn"' in html


def test_the_shared_js_is_one_copy(cfg, engine):
    """One copy of the fetch wrappers, the poll cache and the formatters.

    A second copy would drift, and the Formatters are what make the two surfaces
    agree about what a value means. Since CUR-358 there is one page left that
    composes them (the boards tab, which includes the leaderboard script), so the
    assertion is that the include is singular and nothing inlines a copy.
    """
    import os
    d = os.path.join(REPO, "src", "ui", "templates", "jinja", "sections")
    shared = open(os.path.join(d, "sec_lcp_shared_js.html")).read()
    assert "const API = {" in shared and "function esc(s)" in shared
    assert "POLL_KEY" in shared, "the poll cache must be shared, not duplicated"
    body = open(os.path.join(d, "sec_routingdefs_boards_script.html")).read()
    assert body.count('sec_lcp_shared_js.html') == 1, "the shared JS is included once"
    # (The matrix tab has its own API object on purpose: it talks to a different
    # endpoint family — capability/registry — and never composes the board.)
    for f in ("sec_leaderboard_script.html", "sec_routingdefs_boards_script.html"):
        assert "const API = {" not in open(os.path.join(d, f)).read(), (
            f"{f} carries a second copy of API")


def test_the_unbound_route_is_registered():
    """A read the template calls but the server does not serve is a 404 in the tab."""
    src = open(os.path.join(REPO, "src", "server", "handler.py")).read()
    assert "api.models.unbound" in src, "route not registered"


# ── the editor's own API must still exist (it is what makes tab 1 work) ───────

def test_the_capability_api_is_still_registered():
    src = open(os.path.join(REPO, "src", "server", "handler.py")).read()
    for route in ("api.models.capability", "api.models.registry"):
        assert route in src, f"{route} disappeared — tab 1 would render but do nothing"
