"""Admin auth: the guard must actually gate, and must not break the proxy.

These tests are the acceptance proofs for the change. They deliberately do NOT
inherit the suite-wide ``LCP_ALLOW_ANONYMOUS_ADMIN=1`` opt-out from conftest —
the first fixture below removes it, because a test suite that runs with the door
held open cannot prove the door closes.

Written by L2 (not L1) on purpose: these back the security claim, so L2 must be
able to re-verify them personally. L1 owns the broader regression suite.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.api import admin_auth


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _real_auth(monkeypatch):
    """Undo the suite-wide opt-out and configure a known token."""
    monkeypatch.delenv("LCP_ALLOW_ANONYMOUS_ADMIN", raising=False)
    monkeypatch.setenv("LCP_ADMIN_TOKEN", "test-token-do-not-use-in-prod")


@pytest.fixture
def secret(monkeypatch):
    """A fixed master secret so cookie signatures are deterministic."""
    monkeypatch.setenv("LCP_SECRET_KEY", "unit-test-master-secret")
    return b"unit-test-master-secret"


def _handler(path="/api/keys", method="GET", headers=None):
    from tests.test_server import TestHandler
    return TestHandler(path=path, method=method, headers=headers or {})


# ── token / cookie primitives ───────────────────────────────────────────────

class TestToken:
    def test_matching_token_accepted(self):
        assert admin_auth.token_matches("test-token-do-not-use-in-prod") is True

    def test_wrong_token_rejected(self):
        assert admin_auth.token_matches("wrong") is False

    def test_none_rejected(self):
        assert admin_auth.token_matches(None) is False

    def test_empty_env_is_treated_as_unset(self, monkeypatch):
        """A stray `LCP_ADMIN_TOKEN=` must not produce an empty token that an
        empty guess would match."""
        monkeypatch.setenv("LCP_ADMIN_TOKEN", "   ")
        assert admin_auth.admin_token() is None
        assert admin_auth.token_matches("") is False
        assert admin_auth.token_matches("   ") is False


class TestCookie:
    def test_roundtrip(self, secret, temp_dir):
        value = admin_auth.issue_cookie(data_dir=str(temp_dir))
        payload = admin_auth.verify_cookie(value, data_dir=str(temp_dir))
        assert payload is not None
        assert payload["sub"] == "token"
        assert payload["v"] == 1

    def test_tampered_payload_rejected(self, secret, temp_dir):
        value = admin_auth.issue_cookie(data_dir=str(temp_dir))
        body, _, sig = value.rpartition(".")
        forged = admin_auth._b64e(json.dumps(
            {"sub": "attacker", "iat": 0, "exp": 9999999999, "v": 1}
        ).encode())
        assert admin_auth.verify_cookie(
            f"{forged}.{sig}", data_dir=str(temp_dir)) is None

    def test_tampered_signature_rejected(self, secret, temp_dir):
        value = admin_auth.issue_cookie(data_dir=str(temp_dir))
        body, _, _sig = value.rpartition(".")
        bogus = admin_auth._b64e(b"x" * 32)
        assert admin_auth.verify_cookie(f"{body}.{bogus}",
                                        data_dir=str(temp_dir)) is None

    def test_expired_rejected(self, secret, temp_dir):
        issued = int(time.time()) - admin_auth.COOKIE_TTL_SECONDS - 3600
        value = admin_auth.issue_cookie(data_dir=str(temp_dir), now=issued)
        assert admin_auth.verify_cookie(value, data_dir=str(temp_dir)) is None

    def test_unsigned_string_rejected(self, secret, temp_dir):
        for bad in (None, "", "garbage", "no-dot-here", ".", "a."):
            assert admin_auth.verify_cookie(bad, data_dir=str(temp_dir)) is None

    def test_cookie_key_is_derived_not_the_master_secret(self, secret, temp_dir):
        """The cookie key must not be the credential-encryption key."""
        import hashlib
        derived = admin_auth._cookie_key(str(temp_dir))
        raw = hashlib.sha256(b"unit-test-master-secret").digest()
        assert derived != raw
        assert derived != b"unit-test-master-secret"
        assert len(derived) == 32

    def test_different_master_secret_invalidates_cookie(self, secret, temp_dir,
                                                        monkeypatch):
        value = admin_auth.issue_cookie(data_dir=str(temp_dir))
        monkeypatch.setenv("LCP_SECRET_KEY", "a-completely-different-secret")
        assert admin_auth.verify_cookie(value, data_dir=str(temp_dir)) is None


# ── fail-closed startup ─────────────────────────────────────────────────────

class TestFailClosed:
    def test_refuses_to_start_without_token(self, monkeypatch):
        monkeypatch.delenv("LCP_ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("LCP_ALLOW_ANONYMOUS_ADMIN", raising=False)
        with pytest.raises(admin_auth.AdminAuthError) as exc:
            admin_auth.assert_configured()
        # The message must name the variable, or the operator cannot fix it.
        assert "LCP_ADMIN_TOKEN" in str(exc.value)

    def test_explicit_optout_starts(self, monkeypatch):
        monkeypatch.delenv("LCP_ADMIN_TOKEN", raising=False)
        monkeypatch.setenv("LCP_ALLOW_ANONYMOUS_ADMIN", "1")
        admin_auth.assert_configured()  # must not raise

    def test_optout_must_be_exactly_one(self, monkeypatch):
        """'true'/'yes' are not accepted — only the documented value."""
        monkeypatch.delenv("LCP_ADMIN_TOKEN", raising=False)
        for value in ("true", "yes", "0", "", "TRUE"):
            monkeypatch.setenv("LCP_ALLOW_ANONYMOUS_ADMIN", value)
            with pytest.raises(admin_auth.AdminAuthError):
                admin_auth.assert_configured()

    def test_token_present_starts(self, monkeypatch):
        monkeypatch.setenv("LCP_ADMIN_TOKEN", "x")
        monkeypatch.delenv("LCP_ALLOW_ANONYMOUS_ADMIN", raising=False)
        admin_auth.assert_configured()


# ── path classification ─────────────────────────────────────────────────────

class TestIsPublicPath:
    def test_health_and_metrics_public(self):
        assert admin_auth.is_public_path("/health")
        assert admin_auth.is_public_path("/metrics")

    def test_admin_routes_public(self):
        assert admin_auth.is_public_path("/admin/login")
        assert admin_auth.is_public_path("/admin/logout")

    def test_static_public(self):
        assert admin_auth.is_public_path("/static/js/api.js")

    def test_models_api_path_public(self):
        assert admin_auth.is_public_path("/v1/models")

    def test_bare_models_page_is_guarded(self):
        """`/models` is both a models-API path and the admin Models page, and the
        route table serves the PAGE. It must not be treated as public, or the
        admin HTML renders for anyone. Reproduced live 2026-10-18."""
        assert not admin_auth.is_public_path("/models")

    def test_proxy_profile_paths_public(self):
        profiles = {"l2": {}, "l1": {}}
        assert admin_auth.is_public_path("/l2/chat/completions", profiles)
        assert admin_auth.is_public_path("/l1/chat/completions", profiles)
        assert admin_auth.is_public_path("/l2/v1/models", profiles)
        assert admin_auth.is_public_path("/l2/models", profiles)

    def test_profile_prefixed_admin_pages_are_guarded(self):
        """A path must be public because it is a proxy endpoint, not because its
        first segment is a profile name. /{profile}/dashboard is an admin page."""
        profiles = {"l2": {}, "l1": {}}
        for path in ("/l2/dashboard", "/l2/keys", "/l2/profiles",
                     "/l2/logs", "/l2/setup", "/l2/work/tasks"):
            assert not admin_auth.is_public_path(path, profiles), path

    def test_unknown_profile_is_not_public(self):
        profiles = {"l2": {}}
        assert not admin_auth.is_public_path("/attacker/chat/completions",
                                             profiles)

    def test_admin_apis_are_not_public(self):
        for path in ("/api/keys", "/api/settings", "/api/profiles",
                     "/api/usage/totals", "/api/providers", "/api/alerts"):
            assert not admin_auth.is_public_path(path), path

    def test_pages_are_not_public(self):
        for path in ("/", "/dashboard", "/keys", "/providers", "/profiles"):
            assert not admin_auth.is_public_path(path), path


# ── the guard, end to end through the real handler ──────────────────────────

class TestGuardBlocks:
    """Every one of these returned 200 before the change. That is the finding."""

    @pytest.mark.parametrize("path,method", [
        ("/api/keys", "GET"),
        ("/api/settings", "GET"),
        ("/api/profiles", "GET"),
        ("/api/usage/totals", "GET"),
        ("/api/providers", "GET"),
        ("/api/conversations", "GET"),
        ("/api/keys", "POST"),
        ("/api/providers/p1", "PUT"),
        ("/api/keys/1", "DELETE"),
        ("/dashboard", "GET"),
        ("/profiles", "GET"),
    ])
    def test_unauthenticated_request_is_rejected(self, path, method, secret,
                                                 temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        h = _handler(path=path, method=method)
        assert h._admin_guard() is False, f"{method} {path} was allowed through"
        status = h.send_response.call_args[0][0]
        # /api/* must be 401 JSON; pages must be 302 to the login page.
        expected = 401 if path.startswith("/api/") else 302
        assert status == expected

    def test_401_body_is_json_not_html(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        h = _handler("/api/keys")
        h._admin_guard()
        payload = h.wfile.write.call_args[0][0]
        assert json.loads(payload)["error"]["code"] == "LCP-4011"

    def test_page_redirect_points_at_login(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        h = _handler("/profiles")
        h._admin_guard()
        headers = {c[0][0]: c[0][1] for c in h.send_header.call_args_list}
        assert headers["Location"].startswith("/admin/login")


class TestGuardAllows:
    def test_public_paths_pass(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        for path in ("/health", "/metrics", "/admin/login", "/static/x.js"):
            h = _handler(path)
            assert h._admin_guard() is True, path

    def test_valid_cookie_passes(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        cookie = admin_auth.issue_cookie(data_dir=str(temp_dir))
        h = _handler("/api/keys",
                     headers={"Cookie": f"{admin_auth.COOKIE_NAME}={cookie}"})
        assert h._admin_guard() is True

    def test_bearer_token_passes(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        h = _handler("/api/keys", headers={
            "Authorization": "Bearer test-token-do-not-use-in-prod"})
        assert h._admin_guard() is True

    def test_wrong_bearer_rejected(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        h = _handler("/api/keys", headers={"Authorization": "Bearer nope"})
        assert h._admin_guard() is False


class TestProxyNonRegression:
    """The estate's 7 profiles call this path continuously. It must not break."""

    def test_profile_chat_path_is_not_gated(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}, "l1": {}})
        h = _handler("/l2/chat/completions", method="POST")
        # Would be True even with no cookie — key auth happens downstream.
        assert h._admin_guard() is True
        assert h.send_response.call_args is None  # nothing was sent

    def test_models_endpoint_not_gated(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        h = _handler("/v1/models")
        assert h._admin_guard() is True


class TestLoginFlow:
    def test_login_page_renders(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        h = _handler("/admin/login")
        h._serve_admin_login_page()
        body = h.wfile.write.call_args[0][0].decode()
        assert "<form" in body and 'name="token"' in body
        status = h.send_response.call_args[0][0]
        assert status == 200

    def test_wrong_token_redirects_with_failure(self, secret, temp_dir,
                                                monkeypatch):
        monkeypatch.chdir(temp_dir)
        h = _handler("/admin/login", method="POST",
                     headers={"Content-Length": "21"})
        h.rfile.read = MagicMock(return_value=b"token=wrong&next=/keys")
        h._serve_admin_login_post()
        headers = {c[0][0]: c[0][1] for c in h.send_header.call_args_list}
        assert "failed=1" in headers["Location"]
        assert "Set-Cookie" not in headers

    def test_correct_token_sets_cookie(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        h = _handler("/admin/login", method="POST",
                     headers={"Content-Length": "49"})
        h.rfile.read = MagicMock(
            return_value=b"token=test-token-do-not-use-in-prod&next=/keys")
        h._serve_admin_login_post()
        headers = {c[0][0]: c[0][1] for c in h.send_header.call_args_list}
        assert "Set-Cookie" in headers
        cookie = headers["Set-Cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        assert "Path=/" in cookie
        # No TLS on this fake request, so Secure must NOT be set.
        assert "Secure" not in cookie
        assert headers["Location"] == "/keys"

    def test_cookie_is_secure_behind_tls(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        h = _handler("/admin/login", method="POST",
                     headers={"X-Forwarded-Proto": "https", "Content-Length": "44"})
        h.rfile.read = MagicMock(
            return_value=b"token=test-token-do-not-use-in-prod&next=/")
        h._serve_admin_login_post()
        cookie = {c[0][0]: c[0][1]
                  for c in h.send_header.call_args_list}["Set-Cookie"]
        assert "Secure" in cookie

    @pytest.mark.parametrize("evil", [
        "https://evil.example/x", "//evil.example/x", "javascript:alert(1)",
        "http://evil.example",
    ])
    def test_open_redirect_is_refused(self, evil, secret, temp_dir, monkeypatch):
        """The `next` parameter must never become an open redirect."""
        monkeypatch.chdir(temp_dir)
        body = f"token=test-token-do-not-use-in-prod&next={evil}".encode()
        h = _handler("/admin/login", method="POST",
                     headers={"Content-Length": str(len(body))})
        h.rfile.read = MagicMock(return_value=body)
        h._serve_admin_login_post()
        headers = {c[0][0]: c[0][1] for c in h.send_header.call_args_list}
        assert headers["Location"] == "/"

    def test_logout_clears_cookie(self, secret, temp_dir, monkeypatch):
        monkeypatch.chdir(temp_dir)
        h = _handler("/admin/logout")
        h._serve_admin_logout()
        cookie = {c[0][0]: c[0][1]
                  for c in h.send_header.call_args_list}["Set-Cookie"]
        assert "Max-Age=0" in cookie


# ── the route-coverage guard ────────────────────────────────────────────────

class TestRouteCoverage:
    """A route added later must not silently ship open.

    This is the test that keeps working as the surface grows. It walks the LIVE
    route table rather than a hardcoded list, so adding a route forces a
    public/guarded decision instead of defaulting to open.

    The hard part is getting a concrete path out of a matcher, because the table
    stores closures (``exact()``, ``prefix()``, ``regex()``), not strings. Rather
    than reverse-engineer every matcher type, this walks a set of *representative*
    paths derived from the rule names and asserts the guard's verdict on each —
    plus a direct check that the predicate is total for anything under /api/ or
    the page routes. A rule that is genuinely public must be in PUBLIC_EXACT,
    PUBLIC_PREFIXES or the models/profile lists; anything else must be gated.
    """

    def test_live_route_table_is_fully_loaded(self, secret, temp_dir, monkeypatch):
        """Sanity: if the table came back empty the coverage assertions below
        would pass vacuously, which is the classic way this kind of test rots.

        Deliberately does NOT assert exact counts. The slim-surface refactor
        (CUR-222) removes 16 rules, taking this table from 108 to 92 and POST
        from 30 to 29 — an earlier version of this test asserted ``POST >= 30``
        and would have failed the refactor for doing exactly what it was
        chartered to do. The guard's job is to protect the SECURITY property,
        not to pin the size of the surface. Keep this threshold low enough that
        deleting routes never trips it; if the table ever gets near it, the
        concern is a truncated table, not a smaller one."""
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler._routes = None
        table = LCPHandler._route_table()
        assert table.count() >= 20, f"route table looks truncated: {table.count()}"
        assert table.count("GET") >= 10

    def test_no_admin_rule_classifies_as_public_by_accident(
            self, secret, temp_dir, monkeypatch):
        """For every rule, build a plausible path and assert the classification
        matches the rule's own category. Public rules must be *deliberately*
        public; everything else must be gated."""
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler._routes = None
        table = LCPHandler._route_table()
        profiles = {"l2": {}, "l1": {}, "career": {}}

        public_names = {"health", "metrics", "static", "models.list",
                        "models.list.profile", "page.admin.login",
                        "page.admin.logout", "api.admin.login", "api.admin.logout"}

        offenders = []
        for rule in table.rules:
            path = "/" + rule.name.replace(".", "/")
            is_pub = admin_auth.is_public_path(path, profiles)
            if rule.name in public_names:
                continue  # deliberately public; asserted separately below
            # A rule that is neither in public_names nor public by predicate is
            # correctly gated. The failure mode we care about is the reverse:
            # a rule we did NOT list as public yet classifies as public.
            if is_pub:
                offenders.append((rule.name, path))
        assert not offenders, (
            "these rules classify as public without being declared public: "
            f"{offenders}")

    def test_declared_public_rules_are_actually_public(self, secret, temp_dir,
                                                       monkeypatch):
        monkeypatch.chdir(temp_dir)
        profiles = {"l2": {}, "l1": {}}
        for path in ("/health", "/metrics", "/static/js/app.js",
                     "/admin/login", "/admin/logout", "/v1/models"):
            assert admin_auth.is_public_path(path, profiles), path

    def test_a_new_admin_path_is_gated_without_being_listed(
            self, secret, temp_dir, monkeypatch):
        """The property that matters: an endpoint nobody classified is denied,
        not allowed. This is what makes the guard default-closed."""
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        cases = [("/api/brand-new-endpoint", 401), ("/api/v2/anything", 401),
                 ("/newpage", 302), ("/keys/new", 302)]
        for path, expected in cases:
            h = _handler(path, method="POST")
            assert h._admin_guard() is False, f"{path} was allowed through"
            assert h.send_response.call_args[0][0] == expected, path

    def test_put_and_delete_are_guarded_too(self, secret, temp_dir, monkeypatch):
        """The original plan claimed one guard in dispatch() covers everything.
        It does not: do_PUT/do_DELETE never reach dispatch() (7 branches each,
        hand-written if/elif). This pins the per-entry-point guards so a refactor
        cannot quietly drop them."""
        monkeypatch.chdir(temp_dir)
        import inspect
        from src.server.handler import LCPHandler
        for method in ("do_GET", "do_POST", "do_PUT", "do_DELETE"):
            source = inspect.getsource(getattr(LCPHandler, method))
            assert "_admin_guard" in source, f"{method} is not guarded"

    def test_route_table_covers_no_put_delete(self, secret, temp_dir, monkeypatch):
        """Documents WHY the per-entry-point guard exists. If this ever flips
        (all verbs move into the table), the guard-in-dispatch design becomes
        viable — but it must be a deliberate change, not an accident."""
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler._routes = None
        table = LCPHandler._route_table()
        # 0: the last table-level PUT (PUT /api/work/sources) went on 2026-10-25.
        # Every write verb is now dispatched through do_PUT's own guard chain, so
        # a PUT appearing here means someone bypassed it. Never more.
        assert table.count("PUT") == 0
        assert table.count("DELETE") == 0


class TestNoSecretLeakage:
    def test_token_never_logged(self, secret, temp_dir, monkeypatch, caplog):
        monkeypatch.chdir(temp_dir)
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        h = _handler("/api/keys")
        h._admin_guard()
        for record in caplog.records:
            assert "test-token-do-not-use-in-prod" not in record.getMessage()

    def test_cookie_value_never_logged(self, secret, temp_dir, monkeypatch,
                                       caplog):
        monkeypatch.chdir(temp_dir)
        cookie = admin_auth.issue_cookie(data_dir=str(temp_dir))
        from src.server.handler import LCPHandler
        LCPHandler.config = MagicMock(profiles={"l2": {}})
        h = _handler("/api/keys",
                     headers={"Cookie": f"{admin_auth.COOKIE_NAME}={cookie}"})
        h._admin_guard()
        for record in caplog.records:
            assert cookie not in record.getMessage()
