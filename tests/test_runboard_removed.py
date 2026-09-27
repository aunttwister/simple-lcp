"""Runboard is not part of LCP — guard the removal.

Runboard stays a standalone service. It is not a module of LCP any more, so the
app must not name it anywhere: not in the setup manifest, not in the uninstall
API, not in a default path, not in a template or a doc. These tests fail if a
reference comes back, which is the only thing that keeps a removal removed.

See work/tasks/cancelled/runboard-lcp-merge/ for why the merge was dropped.
"""

import os
import re

import pytest

from src.api import setup as setup_mod

SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_DIRS = ("src", "docs")
TEXT_FILES = ("README.md",)
PATTERN = re.compile(r"runboard", re.IGNORECASE)


def _iter_text_files():
    """Every tracked text file whose content is part of the shipped app."""
    for rel in TEXT_DIRS:
        root = os.path.join(SRC_ROOT, rel)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in {".git", "__pycache__"}]
            for name in filenames:
                if os.path.splitext(name)[1] in {".py", ".html", ".js", ".css", ".md"}:
                    yield os.path.join(dirpath, name)
    for rel in TEXT_FILES:
        path = os.path.join(SRC_ROOT, rel)
        if os.path.exists(path):
            yield path


def test_no_runboard_string_anywhere_in_the_app():
    """The requirement is literally 'no references', so check the text."""
    offenders = []
    for path in _iter_text_files():
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                if PATTERN.search(line):
                    offenders.append("%s:%d: %s" % (
                        os.path.relpath(path, SRC_ROOT), lineno, line.strip()))
    assert offenders == [], "runboard is referenced again:\n" + "\n".join(offenders)


def test_manifest_does_not_offer_runboard(mock_config):
    """The Setup page must not offer a module that no longer exists."""
    names = {mod["name"] for mod in setup_mod.manifest(mock_config)["modules"]}
    assert names == {"router", "memory"}


@pytest.mark.parametrize("symbol", [
    "runboard_step",
    "runboard_site",
    "runboard_models_dir",
    "remove_runboard",
])
def test_retired_setup_symbols_are_gone(symbol):
    """The manifest entry, its deps dirs and its uninstaller are all removed."""
    assert not hasattr(setup_mod, symbol)
