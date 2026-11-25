"""The provider-health matrix must derive its columns from the config, not a list.

The matrix is the page an operator reads to answer "is provider X usable
anywhere". A hardcoded column set makes it lie in the two directions that matter:
a provider registered a minute ago never appears, and a provider deleted minutes
ago keeps a column. Both were observed on 2026-11-22 (a provider was removed and
the operator read the missing column as a rendering bug), so the behaviour is
pinned here rather than left to a comment.

These assert the SOURCE, deliberately: the column set is produced in the browser
from `/api/providers`, so a Python test cannot execute it. What it can do is
refuse the regression — a literal provider list reappearing in the template — and
pin the two data flows the header depends on.
"""
import re
from pathlib import Path

TEMPLATE = Path("src/ui/templates/jinja/sections/sec_providers_script.html")
MARKUP = Path("src/ui/templates/jinja/sections/sec_providers.html")
#: CUR-358 — the page-local CSS moved out of the section markup into a head partial
#: (included by the page's `head_extra`, like every other page here). These tests
#: used to grep MARKUP for the style; they grep the file that now owns it.
STYLE = Path("src/ui/templates/jinja/sections/sec_providers_head.html")

# Provider names that have existed in this estate. A JS array literal holding two
# or more of them is the hardcoded column list this test exists to prevent.
KNOWN = ["opencode", "commandcode", "deepseek", "local-sparks", "llamacpp"]


def _src() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_columns_come_from_the_providers_api():
    src = _src()
    assert re.search(r"PH\.provs\s*=\s*Object\.keys\(\(p && p\.providers\)", src), (
        "the column set must be Object.keys(<providers API payload>)")


def test_no_literal_provider_column_list():
    src = _src()
    for arr in re.findall(r"\[[^\[\]\n]*\]", src):
        hits = [n for n in KNOWN if f"'{n}'" in arr or f'"{n}"' in arr]
        assert len(hits) < 2, f"looks like a hardcoded provider list: {arr[:120]}"


def test_header_reads_provider_metadata():
    src = _src()
    assert "PH.pmeta" in src, "the header must be built from provider metadata"
    assert re.search(r"PH\.pmeta\s*=\s*\(p && p\.providers\)", src), (
        "pmeta must be the providers API payload, not a second source")
    for field in ("api_base", "has_api_key", "models", "type"):
        assert field in src, f"header tooltip should use provider field {field!r}"


def test_a_provider_with_no_config_is_labelled():
    """A column that exists only in health data is a delete/rename remnant."""
    src = _src()
    assert "unconfigured" in src
    assert "unused" in src, "a registered provider in no chain must be labelled"
    assert "ph-tag-warn" in STYLE.read_text(encoding="utf-8"), (
        "the warning tag needs a style, or it reads as ordinary metadata")


def test_the_page_states_where_the_columns_come_from():
    """A column that disappears because its provider was DELETED is correct and
    indistinguishable from a hardcoded list unless the page says so."""
    src = _src()
    assert "phCaption" in src, "the caption must be populated by phRender"
    assert "every provider registered in this gateway" in src
    assert "phCaption" in MARKUP.read_text(encoding="utf-8"), (
        "the caption element must exist in the markup")


def test_matrix_cell_states_are_unchanged():
    """The four cell states are the page's contract; a column change must not
    quietly drop one of them."""
    src = _src()
    for phrase in ("no data", "not registered for", "retired"):
        assert phrase in src, f"cell state {phrase!r} disappeared"
