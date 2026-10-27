"""Admin authentication for the LCP control plane.

Design constraints, each of which came from a measurement rather than a guess:

1. **The guard cannot live only in ``router.dispatch()``.** The declarative route
   table covers GET and most POST routes, but ``do_PUT`` and ``do_DELETE`` are
   still hand-written if/elif chains (7 mutating branches each) that never reach
   ``dispatch()``. A guard inside ``dispatch()`` alone would leave
   ``DELETE /api/keys/{id}`` open. So the guard is called from *every* method
   entry point (GET/POST/PUT/DELETE) and the route table is classified, rather
   than relying on "routes are data" being universally true — it is only mostly
   true here.

2. **The proxy path must not be affected.** ``POST /{profile}/chat/completions``
   is authenticated by API key inside ``_serve_chat_completions``, never by a
   cookie, and it is *not* a route-table rule. ``ADMIN_PUBLIC_PREFIXES`` therefore
   lets any ``/{known-profile}/...`` path through to the existing key auth; the
   estate's 7 Hermes profiles depend on it.

3. **Fail closed by default.** If ``LCP_ADMIN_TOKEN`` is unset the process
   refuses to start, unless ``LCP_ALLOW_ANONYMOUS_ADMIN=1`` is set explicitly.
   A customer deployment cannot accidentally run open.

4. **The cookie signing key is derived, not borrowed.** ``LCP_SECRET_KEY`` already
   encrypts provider credentials (``crypto.py``). Reusing the raw key as an HMAC
   key would couple authentication to encryption. ``_cookie_key()`` derives a
   separate key with HKDF-SHA256 and a fixed info label.

5. **Constant-time comparison** for the token, and the token is never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from .crypto import get_secret_key
from .logging_config import get_logger

logger = get_logger("lcp.admin_auth")

# ── env names ────────────────────────────────────────────────────────────────
ADMIN_TOKEN_ENV = "LCP_ADMIN_TOKEN"
ALLOW_ANONYMOUS_ENV = "LCP_ALLOW_ANONYMOUS_ADMIN"

COOKIE_NAME = "lcp_admin"
COOKIE_TTL_SECONDS = 12 * 60 * 60  # 12 h
_CLOCK_SKEW = 60

# Paths that never require a session cookie.
#   /health, /metrics        — liveness + Prometheus scrape, no secrets
#   /admin/*                 — the login/logout routes themselves
#   /static/*                — css/js for the login page
#   /{profile}/*             — the proxy surface; it authenticates by API key
PUBLIC_EXACT = frozenset({"/health", "/metrics", "/welcome"})
# /welcome/ carries the landing page's own assets (css, js, fonts, favicon).
# It is a marketing surface, deliberately outside the admin session.
PUBLIC_PREFIXES = ("/admin/", "/static/", "/welcome/")

# Model-listing paths are configured (LCP_MODELS_PATHS) and are consumed by
# OpenAI-compatible clients that send a bearer key, not a cookie. They are
# treated as public here and keep whatever auth they already had.
_DEFAULT_MODELS_PATHS = ("/v1/models", "/models")

# The only suffixes under /{profile}/ that belong to the proxy surface. Everything
# else that happens to start with a profile name is an admin path and is guarded.
_PROXY_SUFFIXES = frozenset({
    "/chat/completions",
    "/v1/models",
    "/models",
})

# Model-listing forms that are public at the ROOT. Only /v1/models, because
# `/models` is also the admin Models page and the route table serves the page
# there (page.models is registered before models.list) — so treating it as
# public rendered admin HTML to anonymous callers. Verified live 2026-10-18.
_PUBLIC_MODEL_PATHS = frozenset({"/v1/models"})


class AdminAuthError(Exception):
    """Raised when admin auth is misconfigured. Never caught to continue."""


# ── configuration ────────────────────────────────────────────────────────────

def admin_token() -> str | None:
    """Return the configured admin token, or None.

    A blank/whitespace value is treated as unset so a stray ``LCP_ADMIN_TOKEN=``
    in a compose file fails closed rather than producing an empty token that
    ``compare_digest`` would happily match against an empty guess.
    """
    raw = os.environ.get(ADMIN_TOKEN_ENV)
    if raw is None:
        return None
    token = raw.strip()
    return token or None


def anonymous_admin_allowed() -> bool:
    return os.environ.get(ALLOW_ANONYMOUS_ENV, "").strip() == "1"


def assert_configured() -> None:
    """Fail closed at startup.

    Called from ``main()`` before the server binds. Refusing to start is the
    point: a deployment that forgot the token must not come up wide open.
    """
    if admin_token() is not None:
        return
    if anonymous_admin_allowed():
        # Loud, because this is the documented dev escape hatch and it must be
        # impossible to have enabled it by accident without seeing it.
        logger.warning(
            "admin_auth_disabled",
            reason=f"{ADMIN_TOKEN_ENV} unset and {ALLOW_ANONYMOUS_ENV}=1",
            consequence="the admin surface is open to anyone who can reach the port",
        )
        return
    raise AdminAuthError(
        f"{ADMIN_TOKEN_ENV} is not set. The LCP admin surface would be open to "
        f"anyone who can reach the port, so the process refuses to start.\n"
        f"  Set it:   export {ADMIN_TOKEN_ENV}=<long-random-value>\n"
        f"  Dev only: export {ALLOW_ANONYMOUS_ENV}=1"
    )


# ── cookie signing ───────────────────────────────────────────────────────────

def _cookie_key(data_dir: str = "data") -> bytes:
    """Derive the cookie-signing key from the master secret.

    HKDF-SHA256 with a distinct info label, so the cookie key is not the
    credential-encryption key. Compromise of one does not imply the other.
    """
    master = get_secret_key(data_dir)
    if isinstance(master, str):
        master = master.encode("utf-8")
    return hmac.new(master, b"lcp-admin-session-v1", hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_cookie(actor: str = "token", data_dir: str = "data",
                 now: float | None = None) -> str:
    """Mint a signed session cookie value: ``<payload>.<sig>``.

    The payload is deliberately tiny and self-describing so a future change
    (adding a role, an OIDC subject) is a visible schema bump rather than a
    silent reinterpretation of an old cookie.
    """
    issued = int(now if now is not None else time.time())
    payload = {"sub": actor, "iat": issued, "exp": issued + COOKIE_TTL_SECONDS,
               "v": 1}
    body = _b64e(json.dumps(payload, separators=(",", ":"),
                            sort_keys=True).encode("utf-8"))
    sig = hmac.new(_cookie_key(data_dir), body.encode("ascii"),
                   hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_cookie(value: str | None, data_dir: str = "data",
                  now: float | None = None) -> dict[str, Any] | None:
    """Return the decoded payload for a valid cookie, else None.

    Every failure mode returns None rather than an exception with detail, so a
    caller cannot accidentally leak *why* a cookie was rejected back to the
    client (which is a useful oracle when brute-forcing a signature).
    """
    if not value or "." not in value:
        return None
    body, _, sig_b64 = value.rpartition(".")
    if not body or not sig_b64:
        return None
    try:
        provided = _b64d(sig_b64)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None
    expected = hmac.new(_cookie_key(data_dir), body.encode("ascii"),
                        hashlib.sha256).digest()
    if not hmac.compare_digest(provided, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int):
        return None
    current = now if now is not None else time.time()
    if current > exp + _CLOCK_SKEW:
        return None
    return payload


# ── request classification ───────────────────────────────────────────────────

def _models_paths() -> set[str]:
    raw = os.environ.get("LCP_MODELS_PATHS")
    if not raw:
        return set(_DEFAULT_MODELS_PATHS)
    return {p.strip() for p in raw.split(",") if p.strip()}


def is_public_path(path_no_query: str, profiles: Any = None) -> bool:
    """True when ``path`` must be reachable without an admin session.

    Keep this a single, testable predicate: the route-coverage test asserts it
    classifies every live route, so an added route cannot silently ship open.
    """
    path = path_no_query or "/"
    if path in PUBLIC_EXACT:
        return True
    # NOTE: model paths are deliberately NOT checked here as a blanket rule.
    # /models is BOTH the OpenAI-style models endpoint and the admin "Models"
    # page (`page.models`), and the route table is ordered so the page wins
    # (handler.py: page.models is registered before models.list). Treating the
    # configured model paths as public therefore served the admin HTML page to
    # anyone unauthenticated — reproduced live 2026-10-18, `/models` returned
    # 200 with `<!DOCTYPE html>` while `/api/keys` correctly returned 401.
    # Only the unambiguous API forms are public.
    # /v1/models is unambiguously the API form. Bare /models is not: the route
    # table serves the admin page there first, so it stays guarded.
    if path in _PUBLIC_MODEL_PATHS:
        return True
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return True
    # Proxy surface: /{profile}/<exactly the proxy verbs>.
    #
    # This was originally "anything under a known profile name is public", which
    # was too broad: /{profile}/dashboard is an ADMIN page, not a proxy path, and
    # it only avoided leaking because its own handler happened to redirect to a
    # guarded page. A path should be public because it is a proxy endpoint, not
    # because its first segment looks like a profile.
    if profiles:
        segments = path.lstrip("/").split("/", 1)
        if segments and segments[0] in profiles and len(segments) == 2:
            rest = "/" + segments[1]
            if rest in _PROXY_SUFFIXES:
                return True
    return False


def token_matches(candidate: str | None) -> bool:
    """Constant-time compare of a presented token against the configured one."""
    expected = admin_token()
    if expected is None or candidate is None:
        return False
    return hmac.compare_digest(candidate, expected)


def is_authenticated(handler: Any, data_dir: str = "data") -> bool:
    """Decide whether this request carries a valid admin session.

    Bell: ``Authorization: Bearer <admin token>`` is accepted in addition to the
    cookie, so scripts and ``curl`` do not have to do a login round-trip first.
    """
    cookie_header = handler.headers.get("Cookie", "") or ""
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and value:
            if verify_cookie(value, data_dir) is not None:
                return True
    auth_header = handler.headers.get("Authorization", "") or ""
    if auth_header.lower().startswith("bearer "):
        if token_matches(auth_header[7:].strip()):
            return True
    return False


def wants_json(handler: Any, path_no_query: str) -> bool:
    """401+JSON for API callers, 302 for humans on a page."""
    if path_no_query.startswith("/api/"):
        return True
    accept = (handler.headers.get("Accept") or "").lower()
    return "application/json" in accept and "text/html" not in accept
