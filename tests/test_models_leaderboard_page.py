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
    assert "STRIP_DESKTOP = 4" in script
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


# ── 7. the phone layout: six columns cannot share 390px ────────────────────

def test_every_board_cell_carries_its_label(cfg, engine):
    """Below 640px each row becomes labelled blocks. A cell without a data-label
    renders as an unlabelled number on a phone, so every one of them carries it —
    the header row is display:none there and cannot label anything."""
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    for label in ('data-label="#"', 'data-label="Model"', 'data-label="Lab"',
                  'data-label="Percentile"', 'data-label="Date"'):
        assert label in script, label
    # the value column's label is the UNIT (Elo / net improvement), not a word
    assert 'data-label="${esc(unitLabel(unit))}"' in script


def test_a_date_never_wraps(cfg, engine):
    """At 390px the date broke into "2026-" / "11-11" — corrupt-looking data."""
    css = open(os.path.join(TEMPLATES, "static", "dashboard.css")).read()
    assert ".lb-when { white-space: nowrap; }" in css
    assert '.lb-table td[data-label="Date"]' in css


def test_the_stacked_phone_rows_are_wired_to_the_breakpoint(cfg, engine):
    css = open(os.path.join(TEMPLATES, "static", "dashboard.css")).read()
    block = css.split("@media (max-width: 640px) {", 1)[1]
    assert ".lb-table thead { display: none; }" in block
    assert "content: attr(data-label)" in block


# ── 8. the polled set lives in the BROWSER, and only Poll again re-polls ────

def test_the_polled_set_is_kept_in_session_storage(cfg, engine):
    """Operator: "once we poll it we should keep it on frontend in memory in local
    or session storage ... only by user's Poll again the repolling will happen"."""
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    assert "sessionStorage.setItem(POLL_KEY" in script
    assert "sessionStorage.getItem(POLL_KEY)" in script
    assert "sessionStorage.removeItem(POLL_KEY)" in script


def test_loading_the_page_reads_the_stored_poll_and_never_re_polls(cfg, engine):
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    init = script.split("initLeaderboard", 1)[1]
    assert "loadPoll()" in init
    # the load path must not call the poll endpoint, and must not read the server's
    # staging table either (that would show a set the operator did not ask for)
    assert "API.boardPoll()" not in init
    assert "API.boardStaged()" not in init


def test_the_panel_stamps_when_the_data_was_polled(cfg, engine):
    """A polled number without its poll time is not reviewable."""
    html = page(cfg, engine)
    assert 'id="pollAt"' in html
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    assert "Data polled at" in script
    assert "polled_at" in script


def test_a_poll_response_carries_its_timestamp(cfg, engine):
    """Server side of the same fact: without `polled_at` the panel has no stamp."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api",
                            "model_ratings.py")).read()
    assert '"polled_at": now' in src


def test_accepting_or_clearing_empties_the_stored_set(cfg, engine):
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    for fn in ("async function updatePoll()", "async function clearPoll()"):
        body = script.split(fn, 1)[1].split("\n}\n", 1)[0]
        assert "savePoll([], null)" in body, fn


# ── 9. the poll panel shows ARENA boards only ──────────────────────────────

def test_the_poll_panel_drops_custom_rows(cfg, engine):
    """Operator: "why are we displaying custom taxonomy elo and rating in poll
    latest data? ... in poll latest data we should only poll the data for lmarena
    taxonomy that's present"."""
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    assert "function arenaRows(" in script
    assert "const rows = arenaRows(STATE.pollRows);" in script
    assert "savePoll(arenaRows(r.rows)" in script


def test_the_server_side_staged_list_is_arena_only_too(cfg, engine):
    """A render-side filter is not enough: the API must not offer a custom row at
    all, or accepting one would publish a rating the poll cannot have read."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api",
                            "model_ratings.py")).read()
    body = src.split("def staged_rows(", 1)[1].split("\n    out = []", 1)[0]
    assert "taxonomy NOT LIKE" in body and "CUSTOM_PREFIX" in body


def test_the_panel_says_a_poll_is_arena_only(cfg, engine):
    html = page(cfg, engine)
    assert "Arena boards only" in html


# ── 10. manage taxonomy is the default pane, and CSV is a FILE ─────────────

def test_manage_taxonomy_opens_by_default(cfg, engine):
    """Operator: "manage taxonomy should be the default opened in column 3"."""
    html = page(cfg, engine)
    assert 'id="mpTitle">Manage taxonomy<' in html
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    assert "mpShow('tax')" in script
    # the desktop default at init, and what "close" resets to
    init = script.split("initLeaderboard", 1)[1]
    assert "if (MP_DESKTOP.matches) mpShow('tax');" in init


def test_the_csv_control_is_a_file_input(cfg, engine):
    """Operator: "import csv should be a file import, the necessary columns should
    be shown below"."""
    html = page(cfg, engine)
    assert 'id="taxFile"' in html
    assert 'type="file"' in html
    assert 'id="taxCsv"' not in html          # the paste box is gone
    assert "FileReader" in open(os.path.join(TEMPLATES, "sections",
                                             "sec_models_script.html")).read()


def test_the_columns_table_lists_what_a_file_may_carry(cfg, engine):
    html = page(cfg, engine)
    block = html.split('class="mp-cols"', 1)[1]
    for col in ("model_slug", "value", "taxonomy", "lab"):
        assert col in block, col
    # required vs optional is stated, not implied
    assert "<td>yes</td>" in block and "<td>no</td>" in block


def test_the_import_can_use_the_selected_custom_board_as_its_fallback(cfg, engine):
    """A file with no `taxonomy` column lands on the board being looked at —
    but only when that board is one of the operator's own."""
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    assert "indexOf('custom:') === 0" in script
    assert "taxonomy: fallback" in script


def test_a_template_download_is_offered(cfg, engine):
    html = page(cfg, engine)
    assert 'id="taxTemplateBtn"' in html
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    assert "taxonomy-template.csv" in script


def test_a_tick_survives_a_reload(cfg, engine):
    """A stored set without the operator's ticks is a review he has to redo.

    Proven live first: the rows persisted but every row reset to its default, so a
    reload silently discarded which rows he had chosen. The tick map is stored with
    the rows and written on every change (not on unload), so a killed tab keeps the
    last deliberate click.
    """
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    assert "function saveChecks()" in script
    assert "STATE.pollChecks[raw] = box.checked" in script
    assert "checks: STATE.pollChecks" in script
    assert "STATE.pollChecks = d.checks || {};" in script
    # the row renders from the stored state, falling back to the poll's default
    assert "function checkStateFor(" in script
    assert "${checkStateFor(r) ? 'checked' : ''}" in script
    # a NEW poll starts from its own defaults
    assert "STATE.pollChecks = {};   // a NEW poll starts from its own defaults" in script


def test_the_row_identity_is_defined_once(cfg, engine):
    """The DOM key and the stored tick key must be the same function, or a tick
    lands on the wrong row."""
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    assert "function pollKeyOf(r)" in script
    assert "const key = esc(pollKeyOf(r));" in script


def test_the_polled_list_fills_the_lane(cfg, engine):
    """Operator: "poll latest data can extend the list from top to bottom".

    A fixed `max-height: 22rem` left half the lane empty under a short scrollbar,
    which reads as a cut-off list rather than as a full results pane.
    """
    css = open(os.path.join(TEMPLATES, "static", "dashboard.css")).read()
    block = css.split(".mp-rows {", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto" in block
    assert "max-height: none" in block
    assert "22rem" not in block
    # the chain from the lane body down to the list must be a flex column,
    # otherwise flex:1 has nothing to grow into
    assert "#modelsPanel .dp-body { display: flex; flex-direction: column; }" in css
    assert "#pollResults:not([hidden]) {" in css


def test_only_the_panes_own_button_polls(cfg, engine):
    """Operator: "only by user's 'Poll again' the repolling will happen".

    The page-header button SHOWS the pane. If it also polled, opening the panel
    would silently replace a stored review with a fresh fetch — the exact thing he
    ruled out.
    """
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    line = [l for l in script.splitlines()
            if "getElementById('pollRunBtn').addEventListener" in l][0]
    assert "mpShow('poll')" in line
    assert "runPoll" not in line, line
    # and runPoll must not disable it: a button that only shows a pane stays usable
    run = script.split("async function runPoll()", 1)[1].split("\n}\n", 1)[0]
    assert "pollRunBtn'" not in run
    # and exactly ONE control runs the poll: the pane's own button (+ Poll again)
    assert script.count("addEventListener('click', runPoll)") == 2


def test_the_two_panes_are_never_both_shown(cfg, engine):
    """mpShow hides the other pane; a screenshot of both at once is a harness
    artifact, but the code has to make it impossible."""
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    body = script.split("function mpShow(pane)", 1)[1].split("\n}", 1)[0]
    assert "pollPane').hidden = pane !== 'poll'" in body
    assert "taxPane').hidden = pane !== 'tax'" in body


# ── 11. the two defects the operator reported on the shipped panel ─────────

def test_a_styled_pane_still_honours_the_hidden_attribute(cfg, engine):
    """\"Manage taxonomy is overlaying the poll latest data\".

    `#pollPane { display: flex }` is author CSS on the element itself, and the UA's
    `[hidden] { display: none }` is only a user-agent rule — so the attribute was
    overridden and BOTH panes rendered stacked. Any element this stylesheet gives a
    display to must restate [hidden], or `element.hidden = true` silently stops
    working.
    """
    css = open(os.path.join(TEMPLATES, "static", "dashboard.css")).read()
    assert "#pollPane[hidden], #taxPane[hidden] { display: none !important; }" in css
    # and the rule that caused it is still there (the list needs the flex chain)
    assert "#pollPane { display: flex;" in css


def test_the_close_button_actually_closes(cfg, engine):
    """\"the X in the top right corner of 3rd column is not working".

    It called mpShow('tax'), a no-op whenever taxonomy was already the open pane —
    which is the state the page now loads in. A close button must hide the lane.
    """
    script = open(os.path.join(TEMPLATES, "sections", "sec_models_script.html")).read()
    body = script.split("function mpClose()", 1)[1].split("\n}", 1)[0]
    assert "mpHide()" in body
    # no mpShow( in the body itself (the comment above it may mention the old call)
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("//"))
    assert "mpShow(" not in code, code


def test_a_closed_lane_releases_its_reserved_width(cfg, engine):
    """Closing must not leave a dead gutter where the panel was."""
    css = open(os.path.join(TEMPLATES, "static", "dashboard.css")).read()
    assert "body:has(#modelsPanel:not(.open)) .main-content" in css
    block = css.split("body:has(#modelsPanel:not(.open)) .main-content", 1)[1].split("}", 1)[0]
    assert "margin-right: 0" in block


def test_the_board_name_may_wrap_so_the_panel_cannot_overflow():
    """`agent_bash_recovery_steps` is 24 chars of monospace, and the head also
    carries two chips and a count — measured live at 437px of content in a 364px
    body, which put a horizontal scrollbar on the drawer.

    The name wraps; the count keeps one line and does not shrink. Pinned because a
    long board name is normal data here, not an edge case: 7 of the 36 boards are
    `agent_*` and the longest is 26 characters.
    """
    css = open("src/ui/templates/jinja/static/dashboard.css").read()
    block = css.split(".mm-board {", 1)[1].split("}", 1)[0]
    assert "overflow-wrap" in block or "word-break" in block
    assert "min-width: 0" in block
    count = css.split(".mm-count {", 1)[1].split("}", 1)[0]
    assert "nowrap" in count
    # and the list itself must be allowed to shrink inside the flex column
    wrap = css.split(".mp-tax-map {", 1)[1].split("}", 1)[0]
    assert "min-width: 0" in wrap


# ── the mapping list is TAXONOMY-FIRST ─────────────────────────────────────
# Measured complaint (2026-11-16): "right now the process is really complex and
# repetitive". The old list rendered one row per BOARD — 36 rows, 36 dropdowns, 36
# buttons, 88 targets, 5 screen-heights of scroll — to answer a question that is
# always about ONE taxonomy. These pin the inversion.

def test_the_mapping_list_is_one_entry_per_taxonomy_not_per_board():
    html = open("src/ui/templates/jinja/sections/sec_models_script.html").read()
    # the row builder keys on taxonomy, and offers one add control per taxonomy
    assert "function taxGroups()" in html
    assert "byTax[r.taxonomy]" in html
    assert 'data-tax="${esc(g.taxonomy)}"' in html
    # the board-first builder must be gone
    assert "const rowFor = (b, mapped)" not in html
    assert "wrap.querySelectorAll('.mm-go').forEach(btn => {\n    btn.addEventListener('click', () => mapBoard(" not in html


def test_one_action_can_add_several_boards_to_a_taxonomy():
    """`reasoning_chain` is fed by math + expert + hard_prompts_english. Mapping
    those one dropdown at a time is exactly the repetitiveness he named."""
    html = open("src/ui/templates/jinja/sections/sec_models_script.html").read()
    assert "multiple size=" in html            # a real multi-select
    assert "selectedOptions" in html
    assert "async function mapBoards(tax, boards)" in html
    # partial failure must be reported per board, not swallowed
    assert "failed.push" in html
    assert "failed.join" in html


def test_the_22_unused_boards_are_collapsed_not_in_the_way():
    """10 language slices + 8 industry slices + 4 overall/meta cuts. They are not
    competing measurements, so they belong behind a disclosure."""
    html = open("src/ui/templates/jinja/sections/sec_models.html").read()
    assert '<details class="mm-unused-wrap"' in html
    assert 'id="unusedBoards"' in html
    # and it says WHY they are unused, rather than implying they are broken
    assert "language slices, industry" in html
    js = open("src/ui/templates/jinja/sections/sec_models_script.html").read()
    assert "function renderUnused()" in js
    assert "Unused LMArena boards (" in js


def test_a_taxonomy_with_only_languages_still_shows_its_chips():
    js = open("src/ui/templates/jinja/sections/sec_models_script.html").read()
    assert "nothing measures this yet" in js
    # the count is per taxonomy, and singular/plural is not faked
    assert "board${g.boards.length === 1 ? '' : 's'}" in js


def test_the_multi_add_message_agrees_with_the_count():
    """Caught live: adding ONE board said "1 board now measure reasoning_chain".
    The noun, the verb and the number come from the same variable."""
    js = open("src/ui/templates/jinja/sections/sec_models_script.html").read()
    assert "boards.length === 1 ? 'board now measures' : 'boards now measure'" in js
    # and the old malformed string must be gone
    assert "board${boards.length === 1 ? '' : 's'} now measure" not in js
