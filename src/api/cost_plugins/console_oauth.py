"""OpenCode console session tokens - the door to credits.

Why this module exists
----------------------
The console's credits routes accept only a console **session token**.  A
service API key is refused even with permissions ``all`` - verified live
2026-09-26: ``/api/billing/status`` -> 403 ``{"_tag":"Forbidden"}`` while
``/api/usage/summary`` -> 200 with the same key.  So credits need a real
session, and this module mints one.

Two flows exist.  Only one of them works.

**Device code - primary, verified working 2026-09-26.**  The console runs its
own first-party device endpoint, separate from the OAuth AS advertised in
``/.well-known/oauth-authorization-server``::

    POST /console/auth/device/code   {"client_id": "opencode-cli"}
      -> {device_code, user_code, verification_uri_complete, expires_in, interval}
    POST /console/auth/device/token  {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                      "device_code": ..., "client_id": "opencode-cli"}
      -> {access_token, refresh_token, expires_in}  |  {"error": "authorization_pending"}

``verification_uri_complete`` is returned as a **relative** path
(``/console/device?user_code=...``) and must be prefixed with the host.  The
tokens are Bearer credentials for the console API - the flow opencode's own
CLI uses (``packages/core/src/plugin/provider/opencode.ts``).

**Authorization code + PKCE - fallback, blocked upstream.**  The AS accepts a
dynamically registered client, but its consent page requires four query
parameters - ``client_id``, ``redirect_uri``, ``code_challenge`` and
``resource`` - and omitting any of them yields "This authorization link is
incomplete. Start the sign-in again from the application."  ``resource`` is
required yet advertised nowhere in the discovery document.  Prefer the device
flow; this path survives only in case the device endpoint is retired.

CLI (inside an LCP container)::

    python -m api.cost_plugins.console_oauth device    # primary: prints a URL, then waits
    python -m api.cost_plugins.console_oauth start     # PKCE fallback
    python -m api.cost_plugins.console_oauth complete '<pasted callback url>'
    python -m api.cost_plugins.console_oauth status
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..logging_config import get_logger

logger = get_logger("lcp.cost.opencode_oauth")

CONSOLE_WEB = "https://opencode.ai/console"
REGISTER_URL = CONSOLE_WEB + "/auth/oauth/register"
TOKEN_URL = CONSOLE_WEB + "/auth/oauth/token"
AUTHORIZE_URL = CONSOLE_WEB + "/oauth/authorize"
LOOPBACK_REDIRECT = "http://127.0.0.1:8791/callback"
SCOPE = "usage:read org:manage"
CREDENTIAL_KEY = "opencode_console"

# First-party device endpoint (not the OAuth AS).  client_id "opencode-cli" is
# the CLI's own registered identifier; the endpoint accepts it without any
# dynamic registration step.
DEVICE_CODE_URL = CONSOLE_WEB + "/auth/device/code"
DEVICE_TOKEN_URL = CONSOLE_WEB + "/auth/device/token"
DEVICE_CLIENT_ID = "opencode-cli"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_POLL_INTERVAL = 5.0
DEVICE_TIMEOUT_SEC = 660.0

# The consent page requires a ``resource`` (RFC 8707) that the discovery
# document never advertises.  The console's own bundle uses this value as the
# sample for the field's URL schema.
RESOURCE_DEFAULT = "https://opencode.ai/inference"

#: Refresh this many seconds before expiry so a request never races the clock.
REFRESH_SKEW_SEC = 120

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)


class OAuthFlowError(RuntimeError):
    """The console OAuth server refused a request."""

    def __init__(self, status: int, error: str = "", description: str = "",
                 url: str = "") -> None:
        self.status = status
        self.error = error
        self.description = description
        self.url = url
        super().__init__(
            f"oauth HTTP {status}"
            + (f" {error}" if error else "")
            + (f" — {description}" if description else "")
        )


# ── HTTP ───────────────────────────────────────────────────────────────────

def _request(url: str, *, data: Optional[bytes] = None,
             content_type: Optional[str] = None) -> dict:
    """POST (or GET when *data* is None) and return the parsed JSON body."""
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": "https://opencode.ai",
        "Referer": "https://opencode.ai/console",
    }
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(url, data=data, headers=headers,
                      method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except HTTPError as exc:
        raw = (exc.read() or b"").decode("utf-8", errors="replace")
        error = description = ""
        try:
            parsed = json.loads(raw or "{}")
            if isinstance(parsed, dict):
                error = str(parsed.get("error") or parsed.get("_tag") or "")
                description = str(parsed.get("error_description")
                                  or parsed.get("message") or "")
        except (ValueError, TypeError):
            pass
        raise OAuthFlowError(exc.code, error, description, url) from None
    except URLError as exc:
        raise OAuthFlowError(0, "network_error", str(exc.reason), url) from None

    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise OAuthFlowError(status, "bad_response", "non-JSON payload", url) from None
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def _post_json(url: str, payload: dict) -> dict:
    return _request(url, data=json.dumps(payload).encode(),
                    content_type="application/json")


def _post_form(url: str, fields: dict[str, str]) -> dict:
    return _request(url, data=urllib.parse.urlencode(fields).encode(),
                    content_type="application/x-www-form-urlencoded")


# ── Flow primitives ────────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_pkce() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for PKCE S256 (the only advertised method)."""
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def register_client(client_name: str = "lcp-credit-reader",
                    redirect: str = LOOPBACK_REDIRECT) -> str:
    """Register a public PKCE client; returns its ``client_id``."""
    payload = {
        "client_name": client_name,
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    response = _post_json(REGISTER_URL, payload)
    client_id = response.get("client_id") or ""
    if not client_id:
        raise OAuthFlowError(0, "no_client_id",
                             json.dumps(response)[:200], REGISTER_URL)
    return str(client_id)


def build_authorize_url(client_id: str, verifier: str,
                        redirect: str = LOOPBACK_REDIRECT,
                        state: Optional[str] = None,
                        scope: str = SCOPE,
                        resource: str = RESOURCE_DEFAULT) -> tuple[str, str]:
    """Return ``(url, state)`` - the URL the operator opens once.

    ``resource`` is mandatory on the consent page even though the discovery
    document never advertises it; without it the page renders "This
    authorization link is incomplete." and never reaches the API.
    """
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = state or _b64url(secrets.token_bytes(16))
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": scope,
        "resource": resource,
    })
    return f"{AUTHORIZE_URL}?{query}", state


def parse_callback(value: str) -> str:
    """Extract the authorization code from a pasted callback URL (or a bare code).

    The operator pastes the address-bar URL after the loopback redirect fails to
    load, so accept a full URL, a query string fragment, or the raw code.
    """
    text = (value or "").strip().strip('"').strip("'")
    if not text:
        raise ValueError("empty callback value")
    if "code=" in text:
        query = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
        params = urllib.parse.parse_qs(query)
        refused = params.get("error", [""])[0]
        if refused:
            raise ValueError(f"authorization was refused: {refused}")
        code = params.get("code", [""])[0]
        if not code:
            raise ValueError("no code parameter in the pasted URL")
        return code
    if "://" in text or "?" in text or text.startswith("/"):
        # A callback URL with no code in it: a denial, or the authorize page was
        # copied instead of the redirect. Either way it is not a usable code, and
        # sending it to the token endpoint would only produce a confusing
        # invalid_grant.
        raise ValueError("no code parameter in the pasted URL")
    return text


def exchange_code(code: str, verifier: str, client_id: str,
                  redirect: str = LOOPBACK_REDIRECT) -> dict:
    """Swap an authorization code for tokens."""
    return _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "client_id": client_id,
        "redirect_uri": redirect,
    })


def refresh(client_id: str, refresh_token: str, flow: str = "pkce") -> dict:
    """Exchange a refresh token for a fresh access token.

    The first-party device endpoint and the OAuth AS are different servers with
    different refresh routes, so the flow that minted the token decides where
    it gets refreshed.
    """
    if flow == "device":
        return _post_json(DEVICE_TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id or DEVICE_CLIENT_ID,
        })
    return _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    })


# ── Device code flow (primary) ─────────────────────────────────────────────

def start_device_flow(client_id: str = DEVICE_CLIENT_ID) -> dict:
    """Begin a device authorization.

    Returns the provider payload plus ``verification_url`` - the *absolute*
    URL to open.  The provider returns ``verification_uri_complete`` as a
    relative path, so it is joined onto the console host here rather than
    leaving every caller to remember.
    """
    data = _post_json(DEVICE_CODE_URL, {"client_id": client_id})
    if not isinstance(data, dict) or not data.get("device_code"):
        raise OAuthFlowError(0, "invalid_response",
                             "device/code returned no device_code", DEVICE_CODE_URL)
    path = data.get("verification_uri_complete") or data.get("verification_uri") or ""
    data["verification_url"] = urllib.parse.urljoin(CONSOLE_WEB + "/", str(path))
    return data


def poll_device_token(device_code: str,
                      interval: float = DEVICE_POLL_INTERVAL,
                      timeout: float = DEVICE_TIMEOUT_SEC,
                      client_id: str = DEVICE_CLIENT_ID) -> dict:
    """Poll until the operator approves, the code expires, or a real error lands.

    Mirrors opencode's own client: ``authorization_pending`` keeps the current
    interval, ``slow_down`` adds 5s, anything else is terminal.  Pending states
    may arrive either as HTTP 400 with an ``error`` body or as a 200 carrying
    ``error``, so both are handled.
    """
    deadline = time.time() + max(1.0, float(timeout or DEVICE_TIMEOUT_SEC))
    wait = max(1.0, float(interval or DEVICE_POLL_INTERVAL))
    while time.time() < deadline:
        time.sleep(wait)
        payload = {
            "grant_type": DEVICE_GRANT,
            "device_code": device_code,
            "client_id": client_id,
        }
        try:
            data = _post_json(DEVICE_TOKEN_URL, payload)
        except OAuthFlowError as exc:
            if exc.error == "authorization_pending":
                continue
            if exc.error == "slow_down":
                wait += 5.0
                continue
            raise
        if isinstance(data, dict) and data.get("access_token"):
            return data
        error = data.get("error") if isinstance(data, dict) else ""
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            wait += 5.0
            continue
        raise OAuthFlowError(0, error or "device_flow_failed",
                             "device authorization did not complete",
                             DEVICE_TOKEN_URL)
    raise OAuthFlowError(0, "expired_token",
                         "device code expired before it was approved",
                         DEVICE_TOKEN_URL)


def fetch_account(token: str) -> dict:
    """``GET /api/user`` + ``GET /api/orgs`` - proves the session token works.

    The console API wants the token as a Bearer credential; this is the same
    pair of calls opencode's CLI makes immediately after a device login, so it
    doubles as the post-approval verification step.
    """
    headers_token = token or ""
    user = _request_with_token(CONSOLE_WEB + "/api/user", headers_token)
    orgs = _request_with_token(CONSOLE_WEB + "/api/orgs", headers_token)
    if isinstance(orgs, dict):
        orgs = orgs.get("data") or []
    return {"user": user, "orgs": orgs if isinstance(orgs, list) else []}


def stored_org_id() -> str:
    """The org id saved alongside the session, or ``""`` when unknown.

    Org-scoped console routes (billing, usage) require ``x-org-id``; the id is
    captured once at login so the hourly refresh needs no extra round-trip.
    """
    state = load_tokens() or {}
    org_id = state.get("org_id")
    return str(org_id) if org_id else ""


def remember_org_id(org_id: str) -> None:
    """Persist the org id for org-scoped console calls."""
    if not org_id:
        return
    state = load_tokens() or {}
    if state.get("org_id") != str(org_id):
        state["org_id"] = str(org_id)
        save_tokens(state)


def resolve_org_id(token: str = "") -> str:
    """Stored org id, else ask the console ``/api/orgs`` and remember it."""
    org_id = stored_org_id()
    if org_id:
        return org_id
    token = token or current_access_token()
    if not token:
        return ""
    try:
        from .opencode_api import fetch_org_id
        org_id = fetch_org_id(token)
    except Exception:  # noqa: BLE001 - never fail a fetch over the org lookup
        org_id = ""
    if org_id:
        remember_org_id(org_id)
    return org_id


def _request_with_token(url: str, token: str) -> dict:
    """GET *url* with a Bearer token (the console API's expected auth shape)."""
    request = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
    }, method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raw = (exc.read() or b"").decode("utf-8", errors="replace")
        raise OAuthFlowError(exc.code, "api_error", raw[:200], url) from None
    except URLError as exc:
        raise OAuthFlowError(0, "network_error", str(exc.reason), url) from None
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise OAuthFlowError(0, "bad_response", "non-JSON payload", url) from None


# ── Storage ────────────────────────────────────────────────────────────────

def _resolve_store():
    """Return a CredentialStore even from a bare CLI process (no runtime bound)."""
    from ..credential_store import CredentialStore, get_credential_store

    store = get_credential_store()
    if store is not None:
        return store
    data_dir = os.environ.get("LCP_DATA_DIR") or "/app/data"
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{data_dir}/costs.db")
    return CredentialStore(engine, data_dir=data_dir)


def _expiry(tokens: dict) -> float:
    try:
        expires_in = float(tokens.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0.0
    return time.time() + expires_in if expires_in else 0.0


def save_tokens(state: dict) -> None:
    """Persist the OAuth state as one encrypted credential blob."""
    _resolve_store().set(CREDENTIAL_KEY, json.dumps(state))


def load_tokens() -> Optional[dict]:
    """Load the stored OAuth state, or None when nothing is stored."""
    try:
        raw = _resolve_store().get(CREDENTIAL_KEY) or ""
    except Exception:  # noqa: BLE001 — an unreadable store means "not authorized"
        return None
    if not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("opencode_console_state_corrupt")
        return None
    return parsed if isinstance(parsed, dict) else None


def merge_tokens(state: dict, tokens: dict) -> dict:
    """Fold a token response into stored state.

    The refresh response may omit ``refresh_token`` (meaning "keep the current
    one"), so the existing value wins unless a new one is supplied.
    """
    merged = dict(state)
    merged["access_token"] = tokens.get("access_token") or state.get("access_token") or ""
    merged["refresh_token"] = tokens.get("refresh_token") or state.get("refresh_token") or ""
    merged["token_type"] = tokens.get("token_type") or state.get("token_type") or "Bearer"
    merged["scope"] = tokens.get("scope") or state.get("scope") or SCOPE
    merged["expires_at"] = _expiry(tokens)
    merged["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return merged


def current_access_token(force_refresh: bool = False) -> str:
    """Return a usable console access token, refreshing it when it is near expiry.

    Returns ``""`` when the operator has not completed the flow yet — callers
    treat that as "credits unavailable", never as an error worth alerting on.
    """
    state = load_tokens()
    if not state:
        return ""
    access = state.get("access_token") or ""
    try:
        expires_at = float(state.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    fresh = bool(access) and (not expires_at or expires_at - time.time() > REFRESH_SKEW_SEC)
    if fresh and not force_refresh:
        return access

    refresh_token = state.get("refresh_token") or ""
    client_id = state.get("client_id") or ""
    if not refresh_token or not client_id:
        return access
    try:
        tokens = refresh(client_id, refresh_token,
                         flow=state.get("flow") or "pkce")
    except OAuthFlowError as exc:
        logger.warning("opencode_console_refresh_failed", status=exc.status,
                       error=exc.error)
        return access
    except Exception as exc:  # noqa: BLE001 — never break a caller over a refresh
        logger.warning("opencode_console_refresh_error", error=str(exc))
        return access
    merged = merge_tokens(state, tokens)
    try:
        save_tokens(merged)
    except Exception as exc:  # noqa: BLE001
        logger.warning("opencode_console_save_failed", error=str(exc))
    logger.info("opencode_console_token_refreshed")
    return merged.get("access_token") or access


# ── CLI ────────────────────────────────────────────────────────────────────

def _cli(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="console_oauth",
                                     description="OpenCode console OAuth helper")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start", help="register a client and print the approval URL")
    done = sub.add_parser("complete", help="exchange the pasted callback URL")
    done.add_argument("callback", help="the URL from the browser address bar")
    done.add_argument("--client-id"),
    done.add_argument("--verifier"),
    device = sub.add_parser(
        "device", help="device-code login (primary): prints a URL and waits")
    device.add_argument("--timeout", type=float, default=DEVICE_TIMEOUT_SEC,
                        help="seconds to wait for approval")
    sub.add_parser("status", help="show whether a session is stored and valid")
    args = parser.parse_args(argv)

    # A pending flow is kept in a world-readable-free file so `complete` can
    # find the PKCE verifier that `start` generated.
    pending_path = os.environ.get("OC_OAUTH_PENDING", "/tmp/oc_oauth_pending.json")

    if args.cmd == "start":
        client_id = register_client()
        verifier, _ = new_pkce()
        url, state = build_authorize_url(client_id, verifier)
        with open(pending_path, "w") as handle:
            json.dump({"client_id": client_id, "verifier": verifier,
                       "state": state}, handle)
        os.chmod(pending_path, 0o600)
        print("Open this URL, approve, then paste the address-bar URL back:\n")
        print(url)
        return 0

    if args.cmd == "complete":
        client_id = args.client_id
        verifier = args.verifier
        if (not client_id or not verifier) and os.path.exists(pending_path):
            with open(pending_path) as handle:
                pending = json.load(handle)
            client_id = client_id or pending.get("client_id")
            verifier = verifier or pending.get("verifier")
        if not client_id or not verifier:
            print("no pending flow — run `start` first (or pass --client-id/--verifier)")
            return 2
        tokens = exchange_code(parse_callback(args.callback), verifier, client_id)
        state = merge_tokens({"client_id": client_id}, tokens)
        save_tokens(state)
        os.unlink(pending_path) if os.path.exists(pending_path) else None
        print(f"stored: access_token len={len(state['access_token'])} "
              f"refresh={'yes' if state['refresh_token'] else 'no'} "
              f"expires_in={int(max(0, state['expires_at'] - time.time()))}s")
        return 0

    if args.cmd == "device":
        flow = start_device_flow()
        print("Open this URL and approve (the code is pre-filled):\n")
        print(f"  {flow['verification_url']}\n")
        print(f"  user code: {flow['user_code']}  ·  expires in "
              f"{int(flow.get('expires_in') or 0)}s")
        print("\nwaiting for approval ...", flush=True)
        tokens = poll_device_token(
            flow["device_code"],
            interval=flow.get("interval") or DEVICE_POLL_INTERVAL,
            timeout=args.timeout,
        )
        state = merge_tokens({"client_id": DEVICE_CLIENT_ID, "flow": "device"},
                             tokens)
        save_tokens(state)
        try:
            account = fetch_account(state["access_token"])
            user = account.get("user") or {}
            orgs = account.get("orgs") or []
            if orgs:
                remember_org_id((orgs[0] or {}).get("id") or "")
            who = user.get("email") or user.get("id") or "?"
            org = f" · {orgs[0].get('name')}" if orgs else ""
            print(f"approved as {who}{org} ({len(orgs)} org(s))")
        except OAuthFlowError as exc:
            print(f"approved, but verification failed: "
                  f"{exc.status} {exc.error} {exc.description}")
        print(f"stored: access_token len={len(state['access_token'])} "
              f"refresh={'yes' if state['refresh_token'] else 'no'} "
              f"expires_in={int(max(0, state['expires_at'] - time.time()))}s")
        return 0

    state = load_tokens()
    if not state:
        print("status: no console session stored")
        return 1
    remaining = int(float(state.get("expires_at") or 0) - time.time())
    print(f"status: session stored · refresh_token="
          f"{'yes' if state.get('refresh_token') else 'no'} · "
          f"access_expires_in={remaining}s")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual operator entry point
    import sys

    sys.exit(_cli(sys.argv[1:]))
