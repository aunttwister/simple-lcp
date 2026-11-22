"""The Capability matrix's "Poll latest data" pane (column 3).

Operator, 2026-11-21:

    "So we have capability matrix tab that is polled by live bench but user is able to
     import and edit those ratings. We should have a poll latest data in the third column."

Three things are asserted, and they are separate claims:

1. **The control exists** on the matrix tab, and it lives in the THIRD column — the lane's
   panes — not as a toolbar-only action.
2. **It polls, then SHOWS WHAT MOVED.** Before this change a run reported nothing: a
   capability poll writes no audit row (`mi_polls` gets a row per *endpoint* and a capability
   source polls none), logs nothing, and its provenance was fetched but never drawn. So the
   result table is not decoration — it is the only visible record of the run.
3. **The operator's precedence is stated where he acts on it.** He can import and hand-edit
   these ratings, so the pane must say which source survives a poll. `manual` (1) outranks
   `livebench` (3) in `GRADING_SOURCES`, so a poll cannot overwrite a typed value.

The pane is also a place a mistake is easy: `.score-row` is a 4-column grid, so a fifth child
would shift every column and misalign the "Other subtasks" rows that carry only four. The
provenance badge therefore goes INSIDE the first cell, and that is pinned below.
"""
from __future__ import annotations

import os

import pytest
from html.parser import HTMLParser
from unittest.mock import MagicMock

TEMPLATES = os.path.join(os.path.dirname(__file__), "..", "src", "ui", "templates", "jinja")
CSS = os.path.join(TEMPLATES, "static", "dashboard.css")


def _sec(name):
    return open(os.path.join(TEMPLATES, "sections", name)).read()


MATRIX_HTML = _sec("sec_routingdefs_matrix.html")
MATRIX_JS = _sec("sec_routingdefs_matrix_script.html")
LANE_JS = _sec("sec_lane_js.html")
DASH_CSS = open(CSS).read()


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


def matrix_page(cfg, engine):
    from src.ui.pages import render_routing_definitions_page
    return render_routing_definitions_page(cfg, engine, {"tab": "matrix"})


# ── 1. the control is on the matrix tab, in the third column ───────────────

def test_the_matrix_tab_offers_poll_latest_data(cfg, engine):
    html = matrix_page(cfg, engine)
    assert 'id="pollOpenBtn"' in html, "the toolbar control is missing"
    assert 'id="pollPane"' in html, "the pane is missing"


def test_the_poll_lives_in_the_third_column_lane(cfg, engine):
    """The operator asked for it in the third column, so it must be a lane pane — a
    toolbar-only action would not survive ✕/reopen the way the other panes do."""
    html = matrix_page(cfg, engine)
    lane = html.index('id="modelsPanel"')          # the column-3 aside
    pane = html.index('id="pollPane"')
    assert pane > lane, "the poll pane is outside the lane"
    assert 'data-pane="poll"' in html, "the lane cannot address it by name"
    # the lane reveals panes by data-pane, so the name must be the one it is opened with
    assert "PANE_TITLES.poll" in MATRIX_JS
    assert "on('pollOpenBtn', 'click'" in MATRIX_JS


def test_the_pane_has_a_title_and_an_empty_state_does_not_regress(cfg, engine):
    """✕ must still leave column 3 saying nothing — the operator's rule from 2026-11-19."""
    html = matrix_page(cfg, engine)
    assert 'id="mpEmpty"' in html
    assert "PANE_TITLES.poll = 'Poll latest data'" in MATRIX_JS


def test_no_duplicate_element_ids_are_introduced(cfg, engine):
    html = matrix_page(cfg, engine)
    import re
    ids = re.findall(r'\bid="([^"]+)"', html)
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate ids: {sorted(dupes)}"


# ── 2. it polls and reports what moved ─────────────────────────────────────

def test_it_calls_the_model_intel_poll_endpoint():
    """One route exists for a poll; using another would be a second code path to keep."""
    assert "/api/model-intel/poll" in MATRIX_JS
    body = MATRIX_JS[MATRIX_JS.index('async function pollLatest'):]
    assert "method: 'POST'" in body


def test_it_reads_the_livebench_result_specifically():
    """A poll can be accepted for one source and skipped for another. Reading
    `capabilities.livebench` (not the whole summary) is what makes the pane honest when
    another source is the one that ran."""
    body = MATRIX_JS[MATRIX_JS.index('async function pollLatest'):]
    assert "(s.capabilities || {}).livebench" in body


def test_it_reports_the_run_because_nothing_else_does():
    """No audit row, no log: the response and the row timestamps are the only trace."""
    assert "function renderPollResults" in MATRIX_JS
    for stat in ("models_in_release", "models_matched", "rows", "queued"):
        assert stat in MATRIX_JS, f"the result must report {stat}"
    assert "values moved" in MATRIX_JS or "value${moved.length === 1 ? '' : 's'} moved" in MATRIX_JS
    assert "Nothing moved" in MATRIX_JS, "an unchanged poll must read as valid, not as failure"


def test_it_diffs_against_the_values_shown_before_the_poll():
    body = MATRIX_JS[MATRIX_JS.index('async function pollLatest'):]
    assert "const before = snapshotScores()" in body, (
        "without a before-snapshot the pane cannot say what the poll changed")
    assert "function snapshotScores" in MATRIX_JS


def test_it_re_reads_the_matrix_so_the_table_and_pane_agree():
    """A poll that updates the DB but not the view leaves the two disagreeing on screen."""
    body = MATRIX_JS[MATRIX_JS.index('async function pollLatest'):]
    assert "STATE.matrix = await API.matrix()" in body
    assert "renderMatrix()" in body and "renderModelList()" in body


def test_a_poll_that_did_nothing_names_the_reason():
    """The common failure is an unaccepted source, and the older poll blamed
    provider_catalog for it — pointing the operator at the wrong switch."""
    body = MATRIX_JS[MATRIX_JS.index('async function pollLatest'):]
    assert "not an accepted poll source" in body
    assert "the poll did nothing" in body


def test_a_failed_poll_surfaces_instead_of_reading_as_success():
    body = MATRIX_JS[MATRIX_JS.index('async function pollLatest'):]
    assert "catch (e)" in body and "Poll failed" in body
    assert "btn.disabled = false" in body, "the button must not stay stuck"


# ── the operator's rule: no polling on load, no timer ──────────────────────

def test_it_never_polls_on_page_load():
    """His standing rule: 'Only by users "Poll again" the repolling will happen.'"""
    assert "sessionStorage" in MATRIX_JS
    assert "POLL_STAMP_KEY" in MATRIX_JS
    # the stamp is read when the pane is opened, not at load time
    assert "setPollStamp()" in MATRIX_JS
    for timer in ("setInterval", "setTimeout"):
        assert timer not in MATRIX_JS, f"{timer} would make the poll unattended"


def test_the_stamp_reports_when_it_last_polled():
    assert "lcpCapabilityPolledAt" in MATRIX_JS
    assert "Not polled in this page yet" in MATRIX_JS


# ── 3. the precedence, stated where he acts on it ─────────────────────────

def test_the_pane_states_that_a_poll_cannot_overwrite_a_hand_entered_score(cfg, engine):
    html = matrix_page(cfg, engine)
    pane = html[html.index('id="pollPane"'):]
    pane = pane[:pane.index('id="mpEmpty"')] if 'id="mpEmpty"' in pane else pane
    for source in ("manual", "import", "livebench", "lmarena"):
        assert f">{source}<" in pane, f"the precedence table omits {source}"
    assert "outranks a polled one" in pane


def test_the_manual_precedence_claim_matches_the_server():
    """If GRADING_SOURCES ever reorders so a poll outranks a hand edit, the pane would be
    lying to the operator — so the claim is asserted against the real table."""
    from src.api.seed_capabilities import GRADING_SOURCES
    assert GRADING_SOURCES["manual"] < GRADING_SOURCES["livebench"], (
        "manual must outrank livebench or a poll can overwrite a typed score")
    assert "lmarena" not in GRADING_SOURCES, (
        "the pane says an arena score is never served; if it graded, that text is wrong")
    assert "import" not in GRADING_SOURCES, (
        "the pane says an import that names no source does not grade")


# ── provenance: fetched, and now actually drawn ───────────────────────────

def test_the_source_badge_is_rendered():
    """The API has returned a per-axis source all along and nothing drew it, so a polled
    number and a typed one looked identical on screen."""
    assert "badge badge-src" in MATRIX_JS
    assert "${s.source}" in MATRIX_JS


def test_the_badge_goes_inside_the_first_cell_so_the_grid_is_untouched():
    """.score-row is `grid-template-columns: 170px 1fr 52px 16px`. A fifth child shifts
    every column and misaligns the 'Other subtasks' rows, which carry four."""
    assert "score-task-cell" in MATRIX_JS
    row = MATRIX_JS[MATRIX_JS.index('<div class="score-row${clickable}"'):]
    row = row[:row.index('</div>')]
    # the row's direct children: task-cell, track, pct, chev
    assert row.count('<span class="score-task-cell">') == 1
    assert row.count('class="badge badge-src"') == 1, "exactly one badge per row"
    assert "grid-template-columns: 170px 1fr 52px 16px" in DASH_CSS, (
        "the 4-column grid must stay as it was")


def test_every_class_the_new_markup_uses_has_a_css_rule():
    """Classes shipped with zero rules before (`.matrix-cell`, `.best`), which made the
    feature invisible while the DOM looked right."""
    for cls in ("badge-src", "score-task-cell", "poll-summary", "poll-stat",
                "poll-moved", "poll-new"):
        assert f".{cls}" in DASH_CSS, f".{cls} has no CSS rule"


# ── the pane's copy is honest about what LiveBench can and cannot fill ────

def test_the_pane_says_livebench_is_the_only_benchmark_this_matrix_polls(cfg, engine):
    html = matrix_page(cfg, engine)
    assert "LiveBench is the only benchmark this matrix is polled from" in html


def test_the_pane_explains_why_a_model_is_queued_rather_than_written(cfg, engine):
    """The effort-suffix trap: LiveBench names models `…-max-effort` while the registry
    holds the base slug, so an unresolved name is queued, never guessed."""
    html = matrix_page(cfg, engine)
    assert "max-effort" in html
    assert "queued" in html


# ── the pane must be a SIBLING, not nested inside another pane ─────────────

class _LaneTree(HTMLParser):
    """Track each div's parent so a pane's NESTING can be asserted, not just its presence."""

    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "area", "base", "col"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.pane_parent = {}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        ident = d.get("id") or ""
        cls = d.get("class") or ""
        if tag == "div" and "dp-pane" in cls and ident:
            self.pane_parent[ident] = self.stack[-1][1] if self.stack else None
        if tag in self.VOID:
            return
        self.stack.append((tag, ident or (("." + cls.split()[0]) if cls else tag)))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return


def test_every_pane_is_a_SIBLING_inside_the_lane(cfg, engine):
    """The defect this exists for (found 2026-11-21 by the operator: "Poll latest data column is
    empty on prod").

    `#detailPane` was never closed, so import / poll / tax were its CHILDREN. The lane toggles
    panes with `el.hidden`, so clicking their buttons set `hidden = false` — every DOM check
    passed — while the pane measured 0x0 on screen and inherited the parent's `display:none`.
    A text search for `id="pollPane"` cannot see this: the markup IS present. Only the NESTING is
    wrong, so the assertion has to be structural.

    This also explains the operator's 2026-11-19 report that "Manage Taxonomy and Import scores
    doesnt work, nothing opens in column 3": the recursion fix was real but not the whole cause.
    """
    html = matrix_page(cfg, engine)
    t = _LaneTree()
    t.feed(html)
    panes = t.pane_parent
    assert panes, "no dp-pane found at all"
    for pane, parent in panes.items():
        assert parent == "mpBody", (
            f"#{pane} is nested inside {parent!r} instead of being a sibling in #mpBody — "
            f"hiding that ancestor makes this pane invisible while its own `hidden` reads false")


def test_the_lane_markup_is_div_balanced(cfg, engine):
    """A cheaper guard on the same failure: an unbalanced div is what nested the panes."""
    import re
    html = matrix_page(cfg, engine)
    lane = html[html.index('id="modelsPanel"'):html.index("</aside>")]
    opens = len(re.findall(r"<div\b", lane))
    closes = len(re.findall(r"</div>", lane))
    assert opens == closes, f"the lane has {opens} <div> and {closes} </div>"


def test_the_poll_pane_is_not_inside_the_detail_pane(cfg, engine):
    """Named explicitly, because this is the exact shape that shipped broken."""
    html = matrix_page(cfg, engine)
    t = _LaneTree()
    t.feed(html)
    assert t.pane_parent.get("pollPane") != "detailPane"
    assert t.pane_parent.get("importPane") != "detailPane"
    assert t.pane_parent.get("taxPane") != "detailPane"
