"""Every page's script blocks must PARSE.

Why this exists: on 2026-11-19 a merge (`0c8735a`) resolved a conflict hunk by keeping
BOTH sides of a chart config — a per-chart IIFE copy of `darkOpts` and prod's `costOpts`.
Two `options:` keys with no comma between them is a SyntaxError, and because a browser
parses a script block as a unit, the error killed the ENTIRE block: the Overview home page
had been inert for a day and nothing noticed.

Nothing caught it. Every existing test reads one partial at a time and asserts that
particular strings are present — all of which were present. `node --check` on the SERVED
page is what found it, so that check now runs here. `node` parses the block as the browser
would, which is the only way to test "does this script run" without a browser.

Skipped when `node` is unavailable, and it says so rather than passing silently.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest
from unittest.mock import MagicMock

REPO = os.path.join(os.path.dirname(__file__), "..")

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")


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


def _pages():
    from src.ui import pages
    return pages


#: (label, renderer) — the same surfaces test_m2_page_merge renders, plus every
#: routing-definitions tab.
PAGES = [
    ("/", lambda c, e: _pages().render_home_page(c, e, {"Host": "test:8734"})),
    ("/models", lambda c, e: _pages().render_models_page(c, e, {})),
    ("/profiles", lambda c, e: _pages().render_profiles_page(c, e, {})),
    ("/activity", lambda c, e: _pages().render_activity_page(c, e, {}, {"Host": "test:8734"})),
    ("/usage", lambda c, e: _pages().render_usage_page(c, e, {})),
    ("/settings", lambda c, e: _pages().render_settings_page(c, e, {})),
    ("/routing-definitions?tab=matrix",
     lambda c, e: _pages().render_routing_definitions_page(c, e, {"tab": "matrix"})),
    ("/routing-definitions?tab=boards",
     lambda c, e: _pages().render_routing_definitions_page(c, e, {"tab": "boards"})),
    ("/routing-definitions?tab=unmapped",
     lambda c, e: _pages().render_routing_definitions_page(c, e, {"tab": "unmapped"})),
    ("/routing-definitions?tab=log",
     lambda c, e: _pages().render_routing_definitions_page(c, e, {"tab": "log"})),
]


def _script_blocks(html):
    return re.findall(r"<script(?:[^>]*)>(.*?)</script>", html, re.S)


@pytest.mark.parametrize("label,render", PAGES, ids=[p[0] for p in PAGES])
def test_every_script_block_on_every_page_parses(cfg, engine, label, render, tmp_path):
    html = render(cfg, engine)
    blocks = _script_blocks(html)
    assert blocks, "%s rendered no script at all" % label
    broken = []
    for i, b in enumerate(blocks):
        if not b.strip():
            continue
        f = tmp_path / ("block%d.js" % i)
        f.write_text(b)
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        if r.returncode:
            first = (r.stderr.strip().split("\n") or [""])[0]
            broken.append("block %d: %s" % (i, first))
    assert not broken, (
        "%s serves %d script block(s) a browser cannot parse — the whole block is dead, "
        "not just the broken line: %s" % (label, len(broken), "; ".join(broken))
    )
