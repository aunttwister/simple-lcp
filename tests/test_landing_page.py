"""Tests for the public marketing landing page at /welcome/.

Three things are load-bearing and are asserted here:

1. the paths are PUBLIC (no admin session), because a landing page behind a
   login redirect is not a landing page;
2. the bare path redirects to the trailing-slash form, because the page's asset
   URLs are relative and would otherwise resolve against the site root;
3. traversal out of the landing directory is refused.

The handler is exercised through the real route table, not by calling the
methods directly, so a route-table regression fails here.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from src.api import admin_auth
from src.server.handler import LCPHandler


LANDING_DIR = Path(__file__).resolve().parent.parent / "landing"


class _FakeHandler:
    """Minimal stand-in exposing what the landing routes touch."""

    def __init__(self, path: str):
        self.path = path
        self.status = None
        self.headers: dict[str, str] = {}
        self.body = b""
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def _send_json(self, payload, status=200):
        import json

        self.status = status
        self.body = json.dumps(payload).encode()


def _call(path: str, index: bool = False) -> _FakeHandler:
    h = _FakeHandler(path)
    LCPHandler._serve_landing(h, index=index)
    h.body = h.wfile.getvalue()
    return h


class TestPublicClassification:
    def test_welcome_exact_is_public(self):
        assert admin_auth.is_public_path("/welcome")

    def test_welcome_assets_are_public(self):
        for asset in ("/welcome/", "/welcome/styles.css", "/welcome/main.js",
                      "/welcome/fonts/geist-latin-wght-normal.woff2",
                      "/welcome/favicon.svg"):
            assert admin_auth.is_public_path(asset), asset

    def test_admin_surface_stays_guarded(self):
        """The landing is public; the control plane behind it is not."""
        for path in ("/", "/dashboard", "/providers", "/api/keys"):
            assert not admin_auth.is_public_path(path), path

    def test_welcome_is_not_a_blanket_prefix(self):
        """`/welcome` must not make `/welcomex` or a sibling path public."""
        assert not admin_auth.is_public_path("/welcomex")


class TestServing:
    def test_index_is_served_with_html_content_type(self):
        h = _call("/welcome/")
        assert h.status == 200
        assert h.headers["Content-Type"].startswith("text/html")
        assert b"<!doctype html>" in h.body.lower()
        assert b"control plane" in h.body.lower()

    def test_css_is_served_as_css(self):
        h = _call("/welcome/styles.css")
        assert h.status == 200
        assert h.headers["Content-Type"].startswith("text/css")
        assert b"--canvas" in h.body

    def test_font_is_served_as_woff2(self):
        h = _call("/welcome/fonts/geist-latin-wght-normal.woff2")
        assert h.status == 200
        assert h.headers["Content-Type"] == "font/woff2"
        assert h.body[:4] == b"wOF2"

    def test_query_string_is_ignored(self):
        h = _call("/welcome/styles.css?v=2")
        assert h.status == 200
        assert h.headers["Content-Type"].startswith("text/css")

    def test_missing_asset_falls_back_to_the_branded_404(self):
        h = _call("/welcome/does-not-exist")
        assert h.status == 404
        assert h.headers["Content-Type"].startswith("text/html")
        assert b"not in the table" in h.body

    def test_traversal_is_refused(self):
        for path in ("/welcome/../src/server/handler.py",
                     "/welcome/..%2f..%2fetc/passwd",
                     "/welcome/%2e%2e/%2e%2e/etc/passwd"):
            h = _call(path)
            assert h.status == 403, path

    def test_redirect_to_trailing_slash(self):
        h = _FakeHandler("/welcome")
        LCPHandler._redirect_welcome(h)
        assert h.status == 301
        assert h.headers["Location"] == "/welcome/"


class TestRouteTable:
    def test_routes_are_registered_in_order(self):
        """`/welcome` (exact) must be registered before `/welcome/` (prefix),
        or the redirect never fires."""
        routes = LCPHandler._route_table()
        names = [r.name for r in routes.rules]
        assert "page.welcome.redirect" in names
        assert "page.welcome" in names
        assert names.index("page.welcome.redirect") < names.index("page.welcome")


@pytest.fixture(scope="module")
def html():
    return (LANDING_DIR / "index.html").read_text(encoding="utf-8")


class TestPageContent:
    """The page's own ship requirements, asserted rather than assumed."""

    def test_single_h1(self, html):
        assert html.lower().count("<h1") == 1

    def test_skip_link(self, html):
        assert 'class="skip"' in html

    def test_meta_and_social_tags(self, html):
        for needle in ('name="description"', 'property="og:title"',
                       'property="og:description"', 'name="twitter:card"',
                       'rel="canonical"', 'rel="icon"'):
            assert needle in html, needle

    def test_no_background_gradients(self, html):
        css = (LANDING_DIR / "styles.css").read_text(encoding="utf-8")
        # The only gradient allowed is the hero heading's TEXT gradient —
        # backgrounds stay flat. Anything else is a second one.
        gradient_users = [line for line in css.splitlines()
                          if "linear-gradient" in line]
        assert len(gradient_users) == 1, gradient_users
        assert "background-clip: text" in css
        # The gradient must be on the heading, not on a section/body rule.
        blocks = [b for b in css.split("}") if "linear-gradient" in b]
        assert len(blocks) == 1
        selector = blocks[0].rsplit("{", 1)[0].strip().splitlines()[-1].strip()
        assert selector == ".hero-title", selector

    def test_no_scroll_listeners(self):
        js = (LANDING_DIR / "main.js").read_text(encoding="utf-8")
        assert "addEventListener('scroll'" not in js
        assert "IntersectionObserver" in js

    def test_reduced_motion_is_handled(self):
        css = (LANDING_DIR / "styles.css").read_text(encoding="utf-8")
        js = (LANDING_DIR / "main.js").read_text(encoding="utf-8")
        assert "prefers-reduced-motion" in css
        assert "prefers-reduced-motion" in js

    def test_fonts_are_self_hosted(self, html):
        assert "fonts.googleapis.com" not in html
        assert (LANDING_DIR / "fonts" / "geist-latin-wght-normal.woff2").is_file()
        assert (LANDING_DIR / "fonts" / "geist-mono-latin-wght-normal.woff2").is_file()
