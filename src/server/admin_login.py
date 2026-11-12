"""Admin login / logout endpoints.

Deliberately minimal and dependency-free: a plain HTML form, no JavaScript, so
the one page that gates everything else cannot fail because an asset 404'd.

The login form posts the admin token. A token in a form body is not a
password-manager-friendly design, but it is the honest shape of a
single-operator break-glass credential — Phase 2 adds Pocket ID OIDC, after
which the token is only used to bootstrap and to recover if the IdP is down.
"""

from __future__ import annotations

import html
from urllib.parse import parse_qs, quote, unquote, urlparse

from ..api import admin_auth
from ..api import admin_oidc
from ..api.logging_config import get_logger

logger = get_logger("lcp.admin_login")


_LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LCP — sign in</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         margin: 0; min-height: 100vh; display: grid; place-items: center;
         background: #0f1115; color: #e7e9ee; }}
  main {{ width: min(92vw, 22rem); box-sizing: border-box; background: #171a21;
          border: 1px solid #262b36;
          border-radius: 10px; padding: 1.75rem; }}
  h1 {{ font-size: 1.05rem; margin: 0 0 .25rem; font-weight: 600; }}
  p.sub {{ margin: 0 0 1.25rem; color: #98a1b3; font-size: .85rem; }}
  label {{ display: block; font-size: .8rem; color: #98a1b3; margin-bottom: .35rem; }}
  input[type=password] {{ width: 100%; box-sizing: border-box; padding: .6rem .7rem;
          background: #0f1115; color: inherit; border: 1px solid #2c3240;
          border-radius: 6px; font: inherit; }}
  input[type=password]:focus {{ outline: 2px solid #4c7dff; outline-offset: 1px; }}
  button {{ margin-top: 1rem; width: 100%; padding: .65rem; font: inherit;
          font-weight: 600; color: #fff; background: #4c7dff; border: 0;
          border-radius: 6px; cursor: pointer; }}
  button:hover {{ background: #3f6ceb; }}
  .err {{ margin: 0 0 1rem; padding: .6rem .7rem; font-size: .85rem;
          background: #3a1d22; border: 1px solid #5d2b33; border-radius: 6px;
          color: #ffb4be; }}
</style>
</head>
<body>
<main>
  <h1>LCP control plane</h1>
  <p class="sub">Sign in to manage profiles, keys and providers.</p>
  {error}
  <form method="post" action="/admin/login">
    <input type="hidden" name="next" value="{next}">
    <label for="token">Admin token</label>
    <input id="token" name="token" type="password" autocomplete="current-password"
           required autofocus>
    <button type="submit">Sign in</button>
  </form>
  {sso}
</main>
</body>
</html>
"""

_ERROR_HTML = '<p class="err">{}</p>'

_SSO_HTML = ('<p style="margin:1rem 0 0;text-align:center;font-size:.85rem;">'
             '<a href="/admin/oidc/start?next={next}" '
             'style="color:#4c7dff;">Sign in with Pocket ID</a></p>')


def _sso_block(nxt: str) -> str:
    """SSO link only when OIDC is fully configured (and not half-set)."""
    try:
        configured = admin_oidc.is_oidc_configured()
    except admin_oidc.OIDCConfigError:
        return ""
    if not configured:
        return ""
    return _SSO_HTML.format(next=quote(nxt, safe="/"))


def _safe_next(raw: str | None) -> str:
    """Only allow same-origin relative redirects.

    Without this, ``/admin/login?next=https://evil.example`` would turn the login
    page into an open redirect — a classic way to phish a freshly authenticated
    operator.
    """
    if not raw:
        return "/"
    candidate = unquote(raw).strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return "/"
    return candidate


class AdminLoginEndpoints:
    """Mixin: the login page, the token POST, and logout."""

    # ── GET /admin/login ────────────────────────────────────────────────────
    def _serve_admin_login_page(self) -> None:
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = parse_qs(query)
        nxt = _safe_next((params.get("next") or ["/"])[0])
        # Already signed in? Send them on rather than showing the form again.
        if admin_auth.is_authenticated(self):
            return self._admin_redirect(nxt)
        error = ""
        if (params.get("failed") or [""])[0]:
            error = _ERROR_HTML.format(
                "That token was not accepted. Check LCP_ADMIN_TOKEN and try again.")
        body = _LOGIN_HTML.format(error=error, next=html.escape(nxt, quote=True),
                                  sso=_sso_block(nxt))
        self._admin_send_html(body, 200)

    # ── POST /admin/login ───────────────────────────────────────────────────
    def _serve_admin_login_post(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = parse_qs(raw)
        candidate = (form.get("token") or [""])[0]
        nxt = _safe_next((form.get("next") or ["/"])[0])
        client_ip = self.client_address[0] if self.client_address else "-"

        if not admin_auth.token_matches(candidate):
            # One generic message, no distinction between "wrong token" and
            # "no token configured" — the second would tell an attacker that
            # the gate is misconfigured rather than merely strict.
            logger.warning("admin_login_failed", client_ip=client_ip)
            return self._admin_redirect(
                "/admin/login?failed=1&next=" + quote(nxt, safe="/"))

        cookie = admin_auth.issue_cookie()
        logger.info("admin_login_ok", actor="token", client_ip=client_ip)
        self._admin_redirect(nxt, set_cookie=cookie)

    # ── GET/POST /admin/logout ──────────────────────────────────────────────
    def _serve_admin_logout(self) -> None:
        logger.info("admin_logout",
                    client_ip=self.client_address[0] if self.client_address else "-")
        self._admin_redirect("/admin/login", clear_cookie=True)

    # ── helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def _is_tls(handler) -> bool:
        return (handler.headers.get("X-Forwarded-Proto", "") or "").lower() == "https"

    def _admin_send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(payload)

    def _admin_redirect(self, location: str, set_cookie: str | None = None,
                        clear_cookie: bool = False) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        if set_cookie:
            self._admin_send_cookie(set_cookie)
        if clear_cookie:
            self._admin_send_cookie("", expire=True)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _admin_send_cookie(self, value: str, expire: bool = False) -> None:
        attrs = [
            f"{admin_auth.COOKIE_NAME}={value}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if self._is_tls(self):
            attrs.append("Secure")
        if expire:
            attrs += ["Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"]
        else:
            attrs.append(f"Max-Age={admin_auth.COOKIE_TTL_SECONDS}")
        self.send_header("Set-Cookie", "; ".join(attrs))
