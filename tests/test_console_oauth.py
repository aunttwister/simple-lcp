"""Console OAuth (PKCE) flow + credits mapping.

The credits route (``/api/billing/account``) only accepts a console session, and
their OAuth server supports exactly ``authorization_code`` + ``refresh_token``
(no device grant).  These tests pin the flow mechanics, the storage/refresh
behaviour, and the mapping of the console's micro-cent money format.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time

import pytest
from urllib.error import HTTPError

from src.api.cost_plugins import console_oauth as co
from src.api.cost_plugins.opencode import OpenCodeCostPlugin, _map_account_credits


def _expected_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()


# ── PKCE ───────────────────────────────────────────────────────────────────

def test_new_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = co.new_pkce()
    assert challenge == _expected_challenge(verifier)
    assert 43 <= len(verifier) <= 128
    assert "=" not in verifier and "=" not in challenge


def test_new_pkce_is_random_per_call():
    assert co.new_pkce()[0] != co.new_pkce()[0]


def test_authorize_url_carries_every_required_parameter():
    url, state = co.build_authorize_url("oac_test", "verifier-abc")
    assert url.startswith(co.AUTHORIZE_URL + "?")
    for fragment in (
        "client_id=oac_test",
        "response_type=code",
        "code_challenge_method=S256",
        f"code_challenge={_expected_challenge('verifier-abc')}",
        "redirect_uri=http%3A%2F%2F127.0.0.1%3A8791%2Fcallback",
        # The consent page refuses to render without this (RFC 8707), and the
        # discovery document never advertises it.
        "resource=https%3A%2F%2Fopencode.ai%2Finference",
    ):
        assert fragment in url
    assert f"state={state}" in url
    assert state


# ── callback parsing ───────────────────────────────────────────────────────

def test_parse_callback_accepts_a_full_redirect_url():
    assert co.parse_callback(
        "http://127.0.0.1:8791/callback?code=abc123&state=xyz"
    ) == "abc123"


def test_parse_callback_accepts_extra_params_and_quoting():
    assert co.parse_callback(
        '"http://127.0.0.1:8791/callback?state=xyz&code=abc123"'
    ) == "abc123"


def test_parse_callback_accepts_a_bare_code():
    assert co.parse_callback("  abc123  ") == "abc123"


@pytest.mark.parametrize("value", ["", "   ", "http://127.0.0.1:8791/callback",
                                   "http://127.0.0.1:8791/callback?state=xyz"])
def test_parse_callback_rejects_non_codes(value):
    with pytest.raises(ValueError):
        co.parse_callback(value)


# ── error classification ───────────────────────────────────────────────────

def test_token_error_is_classified_not_swallowed(monkeypatch):
    body = json.dumps({"_tag": "OAuthTokenError", "error": "invalid_grant",
                       "error_description": "Unknown or already used code"}).encode()

    def fake_urlopen(req, timeout=30):
        raise HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(co, "urlopen", fake_urlopen)
    with pytest.raises(co.OAuthFlowError) as exc:
        co.exchange_code("code", "verifier", "client")
    assert exc.value.status == 400
    assert exc.value.error == "invalid_grant"
    assert "Unknown or already used" in exc.value.description


def test_registration_requires_a_client_id(monkeypatch):
    monkeypatch.setattr(co, "_post_json", lambda url, payload: {"error": "nope"})
    with pytest.raises(co.OAuthFlowError):
        co.register_client()


def test_register_client_uses_loopback_redirect_and_no_secret(monkeypatch):
    seen = {}

    def fake_post(url, payload):
        seen["url"] = url
        seen["payload"] = payload
        return {"client_id": "oac_x"}

    monkeypatch.setattr(co, "_post_json", fake_post)
    assert co.register_client() == "oac_x"
    assert seen["url"] == co.REGISTER_URL
    # http redirects are rejected off-loopback, and there is no device grant
    assert seen["payload"]["redirect_uris"] == ["http://127.0.0.1:8791/callback"]
    assert seen["payload"]["grant_types"] == ["authorization_code", "refresh_token"]
    assert seen["payload"]["token_endpoint_auth_method"] == "none"


# ── token storage / refresh ────────────────────────────────────────────────

def test_merge_keeps_the_existing_refresh_token_when_omitted():
    state = {"client_id": "c", "refresh_token": "r-old", "access_token": "a-old"}
    merged = co.merge_tokens(state, {"access_token": "a-new", "expires_in": 3600})
    assert merged["refresh_token"] == "r-old"
    assert merged["access_token"] == "a-new"
    assert merged["expires_at"] > time.time()


def test_merge_prefers_a_rotated_refresh_token():
    merged = co.merge_tokens({"refresh_token": "r-old"}, {"refresh_token": "r-new"})
    assert merged["refresh_token"] == "r-new"


def test_current_access_token_is_empty_when_nothing_is_stored(monkeypatch):
    monkeypatch.setattr(co, "load_tokens", lambda: None)
    assert co.current_access_token() == ""


def test_current_access_token_returns_a_fresh_token_without_refreshing(monkeypatch):
    monkeypatch.setattr(co, "load_tokens", lambda: {
        "client_id": "c", "access_token": "a", "refresh_token": "r",
        "expires_at": time.time() + 3600,
    })

    def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("should not refresh a fresh token")

    monkeypatch.setattr(co, "refresh", boom)
    assert co.current_access_token() == "a"


def test_current_access_token_refreshes_a_near_expiry_token(monkeypatch):
    monkeypatch.setattr(co, "load_tokens", lambda: {
        "client_id": "c", "access_token": "a-old", "refresh_token": "r",
        "expires_at": time.time() + 5,  # inside the skew window
    })
    saved = {}
    monkeypatch.setattr(co, "save_tokens", lambda s: saved.update(s))
    monkeypatch.setattr(co, "refresh",
                        lambda cid, rt, flow="pkce": {"access_token": "a-new",
                                                      "expires_in": 3600})
    assert co.current_access_token() == "a-new"
    assert saved["access_token"] == "a-new"
    assert saved["refresh_token"] == "r"


def test_current_access_token_survives_a_failed_refresh(monkeypatch):
    monkeypatch.setattr(co, "load_tokens", lambda: {
        "client_id": "c", "access_token": "a-old", "refresh_token": "r",
        "expires_at": time.time() - 10,
    })

    def boom(cid, rt):
        raise co.OAuthFlowError(400, "invalid_grant", "The refresh token is invalid")

    monkeypatch.setattr(co, "refresh", boom)
    saved = []
    monkeypatch.setattr(co, "save_tokens", lambda s: saved.append(s))
    # the stale token is still returned: the caller's 403 handling is explicit
    assert co.current_access_token() == "a-old"
    assert saved == []


def test_current_access_token_without_refresh_token_returns_what_it_has(monkeypatch):
    monkeypatch.setattr(co, "load_tokens", lambda: {
        "access_token": "a", "expires_at": time.time() - 10,
    })
    assert co.current_access_token() == "a"


# ── credits mapping ────────────────────────────────────────────────────────

def test_map_account_credits_reads_micro_cents():
    mapped = _map_account_credits({
        "availableMicroCents": "1234000000",  # $12.34
        "currency": "USD", "plan": "go", "workspaceId": "wrk_1",
    })
    assert mapped["available_credits"] == 12.34
    assert mapped["balance"] == 12.34
    assert mapped["plan"] == "go"
    assert mapped["workspace_id"] == "wrk_1"


def test_map_account_credits_unwraps_a_nested_account():
    mapped = _map_account_credits({"account": {"availableMicroCents": 500000000}})
    assert mapped["available_credits"] == 5.0


def test_map_account_credits_falls_back_to_balance():
    mapped = _map_account_credits({"balanceMicroCents": 250000000})
    assert mapped["available_credits"] == 2.5


def test_map_account_credits_derives_plan_from_subscription():
    mapped = _map_account_credits({
        "availableMicroCents": 100000000, "subscription": {"plan": "pro"},
    })
    assert mapped["plan"] == "pro"


@pytest.mark.parametrize("payload", [{}, {"foo": "bar"}, None, [], {"account": {}}])
def test_map_account_credits_returns_none_when_unmappable(payload):
    """A shape change must report 'unrecognised', never a fabricated balance."""
    assert _map_account_credits(payload) is None


# ── plugin wiring ──────────────────────────────────────────────────────────

def test_fetch_balance_is_quiet_without_any_credential(monkeypatch):
    plugin = OpenCodeCostPlugin()
    monkeypatch.setattr(plugin, "_token", lambda **kw: "", raising=False)
    monkeypatch.setattr(co, "current_access_token", lambda force_refresh=False: "")
    assert plugin.fetch_balance() is None


def test_fetch_balance_states_that_a_session_is_needed(monkeypatch):
    plugin = OpenCodeCostPlugin()
    monkeypatch.setattr(plugin, "_token", lambda **kw: "oc_sk_all", raising=False)
    monkeypatch.setattr(co, "current_access_token", lambda force_refresh=False: "")
    result = plugin.fetch_balance()
    assert result["_error"] == "auth_failed"
    assert "console_oauth start" in result["detail"]


def test_fetch_balance_uses_the_console_session(monkeypatch):
    plugin = OpenCodeCostPlugin()
    monkeypatch.setattr(plugin, "_token", lambda **kw: "oc_sk_all", raising=False)
    monkeypatch.setattr(co, "current_access_token", lambda force_refresh=False: "sess")
    from src.api.cost_plugins import opencode_api as oa

    seen = {}

    def fake_credits(token, org_id=None):
        seen["token"], seen["org_id"] = token, org_id
        return {"availableMicroCents": 300000000, "mode": "pay-as-you-go",
                "creditLimitMicroCents": None}

    monkeypatch.setattr(oa, "fetch_account_credits", fake_credits)
    monkeypatch.setattr(co, "resolve_org_id", lambda token="": "wrk_test")
    result = plugin.fetch_balance()
    assert seen["token"] == "sess"
    assert seen["org_id"] == "wrk_test"
    assert result["available_credits"] == 3.0
    assert result["plan"] == "pay-as-you-go"
    assert result["credit_limit_usd"] is None


def test_fetch_balance_reports_a_403_distinctly(monkeypatch):
    plugin = OpenCodeCostPlugin()
    monkeypatch.setattr(plugin, "_token", lambda **kw: "oc_sk_all", raising=False)
    monkeypatch.setattr(co, "current_access_token", lambda force_refresh=False: "sess")
    from src.api.cost_plugins import opencode_api as oa

    def forbidden(token, org_id=None):
        raise oa.ConsoleApiError(403, "/api/billing/status", "Forbidden")

    monkeypatch.setattr(oa, "fetch_account_credits", forbidden)
    monkeypatch.setattr(co, "resolve_org_id", lambda token="": "wrk_test")
    result = plugin.fetch_balance()
    assert result["_error"] == "auth_failed"
    assert "403" in result["detail"]


def test_fetch_balance_flags_an_unknown_payload_shape(monkeypatch):
    plugin = OpenCodeCostPlugin()
    monkeypatch.setattr(plugin, "_token", lambda **kw: "oc_sk_all", raising=False)
    monkeypatch.setattr(co, "current_access_token", lambda force_refresh=False: "sess")
    from src.api.cost_plugins import opencode_api as oa

    monkeypatch.setattr(oa, "fetch_account_credits",
                        lambda token, org_id=None: {"surprise": 1})
    monkeypatch.setattr(co, "resolve_org_id", lambda token="": "wrk_test")
    result = plugin.fetch_balance()
    assert result["_error"] == "api_error"
    assert "unrecognised" in result["detail"]


# ── Device code flow (the primary path) ────────────────────────────────────

DEVICE_CODE_RESPONSE = {
    "device_code": "dev-abc123",
    "user_code": "RGJS-HVSZ",
    "verification_uri": "/console/device",
    "verification_uri_complete":
        "/console/device?user_code=RGJS-HVSZ&client_id=opencode-cli",
    "expires_in": 600,
    "interval": 5,
}


def test_start_device_flow_absolutises_the_relative_verification_path(monkeypatch):
    """The provider returns a *relative* verification path; callers need a URL."""
    seen = {}

    def fake_post(url, payload):
        seen["url"], seen["payload"] = url, payload
        return dict(DEVICE_CODE_RESPONSE)

    monkeypatch.setattr(co, "_post_json", fake_post)
    flow = co.start_device_flow()
    assert seen["url"] == co.CONSOLE_WEB + "/auth/device/code"
    assert seen["payload"] == {"client_id": "opencode-cli"}
    assert flow["verification_url"] == (
        "https://opencode.ai/console/device"
        "?user_code=RGJS-HVSZ&client_id=opencode-cli"
    )
    assert flow["user_code"] == "RGJS-HVSZ"


def test_start_device_flow_rejects_a_response_without_a_device_code(monkeypatch):
    monkeypatch.setattr(co, "_post_json", lambda url, payload: {"error": "boom"})
    with pytest.raises(co.OAuthFlowError):
        co.start_device_flow()


def test_poll_device_token_keeps_waiting_while_pending(monkeypatch):
    """``authorization_pending`` is not a failure, in either shape it arrives.

    The console returns it as HTTP 400 with an ``error`` body; tolerate a 200
    body carrying the same field so a server change cannot break the login.
    """
    monkeypatch.setattr(co.time, "sleep", lambda _seconds: None)
    calls = {"n": 0}

    def fake_post(url, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise co.OAuthFlowError(400, "authorization_pending", "", url)
        if calls["n"] == 2:
            return {"error": "authorization_pending"}
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    monkeypatch.setattr(co, "_post_json", fake_post)
    tokens = co.poll_device_token("dev-abc123", interval=1, timeout=30)
    assert tokens["access_token"] == "at"
    assert calls["n"] == 3


def test_poll_device_token_slows_down_when_told_to(monkeypatch):
    """``slow_down`` adds 5s to the interval, as opencode's own client does."""
    waits = []
    monkeypatch.setattr(co.time, "sleep", lambda seconds: waits.append(seconds))
    calls = {"n": 0}

    def fake_post(url, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise co.OAuthFlowError(400, "slow_down", "", url)
        return {"access_token": "at"}

    monkeypatch.setattr(co, "_post_json", fake_post)
    co.poll_device_token("dev-abc123", interval=5, timeout=30)
    assert waits == [5.0, 10.0]


def test_poll_device_token_raises_on_a_terminal_error(monkeypatch):
    monkeypatch.setattr(co.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(co, "_post_json",
                        lambda url, payload: {"error": "access_denied"})
    with pytest.raises(co.OAuthFlowError) as exc:
        co.poll_device_token("dev-abc123", interval=1, timeout=10)
    assert exc.value.error == "access_denied"


def test_poll_device_token_times_out_on_an_unapproved_code(monkeypatch):
    """The deadline is enforced, so a never-approved code cannot hang a worker."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(co.time, "sleep",
                        lambda seconds: clock.update(t=clock["t"] + seconds))
    monkeypatch.setattr(co.time, "time", lambda: clock["t"])
    monkeypatch.setattr(co, "_post_json",
                        lambda url, payload: {"error": "authorization_pending"})
    with pytest.raises(co.OAuthFlowError) as exc:
        co.poll_device_token("dev-abc123", interval=1, timeout=5)
    assert exc.value.error == "expired_token"


def test_refresh_uses_the_device_endpoint_for_device_sessions(monkeypatch):
    """Device tokens refresh at /auth/device/token, not at the OAuth AS."""
    seen = {}

    def fake_post(url, payload):
        seen["url"], seen["payload"] = url, payload
        return {"access_token": "at2", "expires_in": 60}

    monkeypatch.setattr(co, "_post_json", fake_post)
    co.refresh("", "rt", flow="device")
    assert seen["url"] == co.DEVICE_TOKEN_URL
    assert seen["payload"]["grant_type"] == "refresh_token"
    assert seen["payload"]["client_id"] == "opencode-cli"


def test_current_access_token_refreshes_with_the_stored_flow(monkeypatch):
    """A device session must not be refreshed against the PKCE token route."""
    state = {"client_id": "opencode-cli", "flow": "device",
             "access_token": "old", "refresh_token": "rt", "expires_at": 1.0}
    saved = {}
    monkeypatch.setattr(co, "load_tokens", lambda: dict(state))
    monkeypatch.setattr(co, "save_tokens", lambda blob: saved.update(blob))

    def fake_refresh(client_id, refresh_token, flow="pkce"):
        saved["flow_seen"] = flow
        return {"access_token": "new", "expires_in": 3600}

    monkeypatch.setattr(co, "refresh", fake_refresh)
    assert co.current_access_token() == "new"
    assert saved["flow_seen"] == "device"


# ── org id (org-scoped console routes require x-org-id) ───────────────────

def test_stored_org_id_round_trips(monkeypatch):
    """The id captured at login is what later calls send as x-org-id."""
    state = {"access_token": "a", "refresh_token": "r", "expires_at": 1.0}
    monkeypatch.setattr(co, "load_tokens", lambda: dict(state))
    monkeypatch.setattr(co, "save_tokens", lambda blob: state.update(blob))

    assert co.stored_org_id() == ""
    co.remember_org_id("wrk_abc")
    assert co.stored_org_id() == "wrk_abc"
    assert state["org_id"] == "wrk_abc"

    # Remembering the same id again must not churn the store.
    writes = []
    monkeypatch.setattr(co, "save_tokens", lambda blob: writes.append(blob))
    co.remember_org_id("wrk_abc")
    assert writes == []


def test_remember_org_id_ignores_an_empty_id(monkeypatch):
    monkeypatch.setattr(co, "load_tokens", lambda: {"access_token": "a"})
    writes = []
    monkeypatch.setattr(co, "save_tokens", lambda blob: writes.append(blob))
    co.remember_org_id("")
    assert writes == []


def test_resolve_org_id_prefers_the_stored_id_over_the_network(monkeypatch):
    monkeypatch.setattr(co, "load_tokens", lambda: {"org_id": "wrk_stored"})
    monkeypatch.setattr(co, "current_access_token", lambda force_refresh=False: "tok")
    from src.api.cost_plugins import opencode_api as oa
    monkeypatch.setattr(oa, "fetch_org_id",
                        lambda token: pytest.fail("must not call the console"))
    assert co.resolve_org_id() == "wrk_stored"


def test_resolve_org_id_fetches_and_remembers_when_absent(monkeypatch):
    state = {"access_token": "a", "expires_at": 1.0}
    monkeypatch.setattr(co, "load_tokens", lambda: dict(state))
    monkeypatch.setattr(co, "save_tokens", lambda blob: state.update(blob))
    monkeypatch.setattr(co, "current_access_token", lambda force_refresh=False: "tok")
    from src.api.cost_plugins import opencode_api as oa
    monkeypatch.setattr(oa, "fetch_org_id", lambda token: "wrk_fetched")

    assert co.resolve_org_id() == "wrk_fetched"
    assert state["org_id"] == "wrk_fetched"


def test_resolve_org_id_survives_a_console_failure(monkeypatch):
    monkeypatch.setattr(co, "load_tokens", lambda: {"access_token": "a"})
    monkeypatch.setattr(co, "save_tokens", lambda blob: None)
    monkeypatch.setattr(co, "current_access_token", lambda force_refresh=False: "tok")
    from src.api.cost_plugins import opencode_api as oa

    def boom(token):
        raise RuntimeError("console down")

    monkeypatch.setattr(oa, "fetch_org_id", boom)
    assert co.resolve_org_id() == ""  # never raises into a cost fetch
