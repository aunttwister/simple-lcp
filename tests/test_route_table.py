"""Tests for the declarative route table (src/server/router.py).

The table replaced a 147-branch if/elif chain in LCPHandler. These tests pin
the properties that made the chain hard to maintain, so the replacement cannot
regress into the same shape: matchers must ignore query strings, ordering must
be honoured, and the live table must stay well-formed.

NOTE: this is separate from ``tests/test_router.py``, which covers
``src/api/router.py`` (the CapabilityRouter). Different module, similar name —
keep them distinct.
"""

import pytest

from src.server.router import (
    RouteTable,
    any_of,
    exact,
    prefix,
    prefix_suffix,
    regex,
    suffix,
)


class _H:
    """Minimal stand-in for the request handler."""

    def __init__(self, path):
        self.path = path
        self.called = None


class TestMatchers:
    def test_exact_matches_and_ignores_query(self):
        m = exact("/health")
        assert m("/health", "/health") == {}
        assert m("/health?x=1", "/health") == {}
        assert m("/healthz", "/healthz") is None
        assert m("/", "/") is None

    def test_exact_multiple_paths(self):
        m = exact("/", "/dashboard")
        assert m("/", "/") == {}
        assert m("/dashboard", "/dashboard") == {}

    def test_prefix(self):
        m = prefix("/static/")
        assert m("/static/js/api.js", "/static/js/api.js") == {}
        assert m("/static", "/static") is None

    def test_suffix(self):
        m = suffix("/dashboard")
        assert m("/l2/dashboard", "/l2/dashboard") == {}
        assert m("/dashboard", "/dashboard") == {}

    def test_prefix_suffix(self):
        m = prefix_suffix("/api/providers/", "/toggle")
        assert m("/api/providers/x/toggle", "/api/providers/x/toggle") == {}
        assert m("/api/providers/x/rename", "/api/providers/x/rename") is None
        assert m("/api/keys/x/toggle", "/api/keys/x/toggle") is None

    def test_regex_returns_named_groups(self):
        m = regex(r"^/api/providers/(?P<name>[^/]+)/failures$")
        assert m("/api/providers/zgx/failures", "/api/providers/zgx/failures") == {
            "name": "zgx"}
        assert m("/api/providers/zgx/failures/extra",
                 "/api/providers/zgx/failures/extra") is None

    def test_regex_segment_does_not_swallow_slashes(self):
        """`[^/]+` must not match across a slash, or deeper paths alias."""
        m = regex(r"^/api/keys/(?P<id>[^/]+)$")
        assert m("/api/keys/1/rotate", "/api/keys/1/rotate") is None

    def test_any_of_first_match_wins(self):
        m = any_of(suffix("/v1/models"), suffix("/models"))
        assert m("/coder/v1/models", "/coder/v1/models") == {}
        assert m("/coder/models", "/coder/models") == {}
        assert m("/coder/modelsX", "/coder/modelsX") is None


class TestTable:
    def test_first_match_wins(self):
        t = RouteTable()
        t.get("first", prefix("/a"), lambda h, p: setattr(h, "called", "first"))
        t.get("second", prefix("/a/b"), lambda h, p: setattr(h, "called", "second"))
        h = _H("/a/b/c")
        assert t.dispatch(h, "GET") is True
        assert h.called == "first"  # registration order, not specificity

    def test_miss_returns_false(self):
        t = RouteTable()
        t.get("health", exact("/health"), lambda h, p: None)
        assert t.dispatch(_H("/nope"), "GET") is False

    def test_method_is_part_of_the_match(self):
        t = RouteTable()
        t.get("health", exact("/health"), lambda h, p: setattr(h, "called", "get"))
        h = _H("/health")
        assert t.dispatch(h, "POST") is False
        assert t.dispatch(h, "GET") is True

    def test_query_string_reaches_matcher_stripped(self):
        """A path with a query must still match an exact rule."""
        t = RouteTable()
        t.get("logs", exact("/api/logs"),
              lambda h, p: setattr(h, "called", "logs"))
        h = _H("/api/logs?limit=50")
        assert t.dispatch(h, "GET") is True
        assert h.called == "logs"

    def test_params_are_passed_to_the_action(self):
        t = RouteTable()
        seen = {}
        t.get("detail", regex(r"^/api/keys/(?P<id>[^/]+)$"),
              lambda h, p: seen.update(p))
        t.dispatch(_H("/api/keys/42"), "GET")
        assert seen == {"id": "42"}

    def test_describe_and_count(self):
        t = RouteTable()
        t.get("a", exact("/a"), lambda h, p: None)
        t.post("b", exact("/b"), lambda h, p: None)
        assert t.describe() == ["GET    a", "POST   b"]
        assert t.count("GET") == 1
        assert t.count("POST") == 1
        assert t.count() == 2


@pytest.fixture(scope="module")
def live_table():
    """The table handler.py actually dispatches through."""
    from src.server.handler import _build_routes
    return _build_routes()


class TestLiveRouteTable:
    """Invariants on the live table.

    Rule names are unique per (method, name) — the same resource legitimately
    appears under both GET (read) and POST (create/write), e.g. `api.budgets`.
    """

    def test_rule_names_unique_per_method(self, live_table):
        pairs = [(r.method, r.name) for r in live_table.rules]
        dupes = {p for p in pairs if pairs.count(p) > 1}
        assert not dupes, "duplicate (method, name): %s" % sorted(dupes)

    def test_names_are_namespaced_sharing_is_deliberate(self, live_table):
        """A name may appear at most twice, and only across different methods."""
        names = [r.name for r in live_table.rules]
        for n in set(names):
            assert names.count(n) <= 2, "name %s used %d times" % (n, names.count(n))
            methods = {r.method for r in live_table.rules if r.name == n}
            assert len(methods) == names.count(n), (
                "name %s reused within one method" % n)

    def test_every_rule_has_a_name(self, live_table):
        assert all(r.name.strip() for r in live_table.rules)

    def test_provider_presets_before_bare_providers(self, live_table):
        """/api/providers/presets must be reachable before /api/providers."""
        get = [r.name for r in live_table.rules if r.method == "GET"]
        assert get.index("api.providers.presets") < get.index("api.providers")
        assert get.index("api.providers.health") < get.index("api.providers")

    def test_required_routes_present(self, live_table):
        names = {r.name for r in live_table.rules}
        for expected in ("health", "api.settings", "static", "memory.count",
                         "memory.write", "api.providers.toggle",
                         "api.settings.cache.refresh", "api.setup.install"):
            assert expected in names, "missing route: %s" % expected

    def test_settings_cache_before_settings(self, live_table):
        """Both are exact matches; specific-first documents the intent."""
        post = [r.name for r in live_table.rules if r.method == "POST"]
        assert post.index("api.settings.cache.refresh") < post.index("api.settings")

    def test_both_methods_have_rules(self, live_table):
        assert live_table.count("GET") > 40
        assert live_table.count("POST") > 20
