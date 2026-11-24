"""The Usage page's local panel is two cards, not a wall of boxes.

A self-hosted engine's panel used to render eighteen bordered boxes — an engine
strip carrying six inner cells, a trend card, five traffic cards and five
throughput cards — which is a correct set of numbers presented as a page nobody
can read. These tests pin the shape the rebuild chose, and the two defects the
rebuild fixed on the way:

  * one window control, above everything it governs (it used to sit below the
    cards it scoped, and a second copy of the same strip was appended for the
    chart);
  * the windowed figures re-render on a range change, because leaving 7-day
    cards above a 30-day chart is the same defect as the old rolling-window bug
    that made the cards describe tokens the chart could not show.

Markup assertions only: this is a Jinja partial and the panel builder is inline
JS, so there is no Python function to call. The live page is verified separately
by driving the rendered DOM.
"""

import re

import pytest


@pytest.fixture
def usage_html(mock_config):
    from src.ui.pages import render_usage_page

    return render_usage_page(mock_config)


class TestLocalPanelShape:
    def test_the_panel_is_built_by_the_grouped_builder(self, usage_html):
        assert "function localPanelHtml(" in usage_html
        assert "localCards-" in usage_html

    def test_the_eighteen_box_shape_is_gone(self, usage_html):
        # The old engine strip and its six inner cells are both retired; a
        # leftover reference is a dead code path that renders nothing.
        assert "engineStrip" not in usage_html
        assert "engineCell" not in usage_html

    def test_the_grouped_primitives_carry_their_css(self, usage_html):
        # A class referenced by the template with no rule renders as browser
        # defaults, which is how a "tidy" panel quietly becomes an unreadable
        # one. Both halves must arrive together.
        for cls in (".lp-card", ".lp-row", ".lp-item", ".lp-caption", ".lp-note"):
            assert cls in usage_html, cls
        for cls in ("lp-card", "lp-row", "lp-item", "lp-caption", "lp-note"):
            assert '"' + cls + '"' in usage_html or "'" + cls + "'" in usage_html, cls


class TestOneWindowControl:
    def test_exactly_one_range_strip_per_panel(self, usage_html):
        # The strip is emitted once and scopes the cards, the chart and both
        # breakdowns. Two strips meant two elements with one id.
        marks = re.findall(r"dateFilt-' \+ prov", usage_html)
        assert len(marks) == 1
        assert usage_html.count('data-range="7"') == 1
        assert usage_html.count('data-range="30"') == 1

    def test_the_strip_precedes_the_numbers_it_governs(self, usage_html):
        # Ordering is the whole point of moving it: it used to sit under the
        # last card, so the reader met the filter after the figures. Compare
        # inside the panel builder — the CSS block earlier in the document also
        # mentions .lp-big and would win a naive text search.
        body = usage_html[usage_html.index("panelsHtml += '<div class=\"tab-panel'"):]
        strip = body.index("dateFilt-' + prov")
        cards = body.index("localCards-' + prov")
        assert strip < cards


class TestRangeChangeRefreshesTheWindowedFigures:
    def test_the_range_handler_rebuilds_the_local_panel(self, usage_html):
        # applyDateRange must feed the new stats back into the panel, not only
        # into the chart and the sparkline.
        handler = usage_html[usage_html.index("window.applyDateRange = function"):
                             usage_html.index("// Deep-link support")]
        assert "_localStats[provider] = stats" in handler
        assert "localCards-' + provider" in handler
        assert "localPanelHtml(provider, stats" in handler

    def test_the_local_panel_has_one_trend_surface_not_two(self, usage_html):
        # The panel carries a dated bar chart of the same daily series, so the
        # text sparkline that used to sit beside the headline was a second
        # picture of one series — the redundancy the rebuild removed.
        body = usage_html[usage_html.index("function localPanelHtml("):
                          usage_html.index("window.refreshEngine = function")]
        assert "pre class=\"spark\"" not in body
        assert "spark-trend-" not in body

    def test_the_engine_refresh_rebuilds_the_whole_panel(self, usage_html):
        # The card carries both the engine's live state and the windowed
        # figures, so a refresh has to re-render from the CACHED stats rather
        # than replace the card with an engine-only fragment.
        refresh = usage_html[usage_html.index("window.refreshEngine = function"):
                             usage_html.index("function loadEngine(")]
        assert "_engineCache[prov] = d" in refresh
        assert "localPanelHtml(prov, _localStats[prov], d, _localLabel[prov])" in refresh
