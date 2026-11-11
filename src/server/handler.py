"""LCPHandler — HTTP request handler with routing and chat completion logic."""

import json
import os
import re
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import quote, unquote

from ..api.logging_config import get_logger
from ..api.request_pipeline import (
    strip_forbidden_tools,
    calculate_cost,
    try_chain,
    record_cost,
    capture_reasoning_from_response,
    capture_reasoning_from_sse,
)
from ..api.cost_estimator import estimate_from_request
from ..api.prompt_cache import get_prompt_cache
from ..api.token_verifier import get_token_verifier
from ..api.key_manager import get_key_manager
from ..api.alert_manager import get_alert_manager
from ..api import inflight as in_flight
from ..api import admin_auth
from .admin_login import AdminLoginEndpoints
from .admin_oidc_endpoints import AdminOIDCEndpoints
from ..api import audit as audit_log
from ..api import conv_store
from ..api import conv_titler
from ..api.runtime import resolve_service
from ..api.models import Budget, ApiKey, get_session
from sqlalchemy import or_
from ..api.exceptions import (
    AllProvidersFailedError,
    AuthError,
    CreditExhaustedError,
    ForbiddenError,
    LCPError,
    ProviderBadRequestError,
    ProviderError,
    ToolBlockedError,
)
from .sse_helpers import extract_last_sse_chunk, extract_sse_text, extract_message_text
from .endpoints import (
    HealthEndpoints,
    ProviderEndpoints,
    ProfileEndpoints,
    KeyEndpoints,
    AlertEndpoints,
    BudgetEndpoints,
    PluginEndpoints,
    UsageEndpoints,
    DashboardEndpoints,
    SettingsEndpoints,
    MemoryEndpoints,
    WorkEndpoints,
    EvidenceEndpoints,
)

from .router import (
    RouteTable,
    any_of,
    exact,
    prefix,
    prefix_suffix,
    regex,
    suffix,
)

logger = get_logger("lcp.server")

# Redact things that look like API keys / bearer tokens before surfacing any
# provider error text to a client.
_SENSITIVE_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_-]{6,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
_MAX_CLIENT_ERROR_LEN = 300


def _sanitize_message(msg: str) -> str:
    """Redact secrets and truncate a message for client-facing responses."""
    if not isinstance(msg, str):
        msg = str(msg)
    msg = _SENSITIVE_PATTERN.sub("[REDACTED]", msg)
    if len(msg) > _MAX_CLIENT_ERROR_LEN:
        msg = msg[:_MAX_CLIENT_ERROR_LEN] + "..."
    return msg


def _resolve_pricing(config, provider: str, model: str) -> dict | None:
    """Resolve pricing for cost estimation without hard-failing.

    Order: config (the gateway config's ``pricing:`` section) → cost-plugin
    registry (e.g. Command Code's built-in pricing) → None
    (``estimate_from_request`` then uses default rates).

    Returns ``None`` instead of raising so a missing pricing entry (e.g.
    commandcode absent from the config's ``pricing:`` section) does NOT turn a
    request into a 500 before it ever reaches the provider chain.
    """
    try:
        return config.get_pricing(provider, model)
    except Exception:
        pass
    try:
        from ..api.cost_plugins import get_registry
        p = resolve_service("pricing", fallback=get_registry).get_pricing(provider, model)
        if p is not None:
            return p
    except Exception:
        pass
    logger.warning(
        "pricing_resolved_fallback",
        provider=provider,
        model=model,
        reason="no config or plugin pricing — using default estimate rates",
    )
    return None


#: Headers a client uses to declare that it is an autonomous agent rather than a person.
#: Sent by the Hermes fleet (see `model.default_headers` in each profile's config.yaml).
AGENT_GENERATED_HEADER = "X-Agent-Generated"
AGENT_IDENTITY_HEADER = "X-Agent-Identity"

#: Explicit "not an agent" spellings. Only these produce False; anything else unknown to us
#: is NOT treated as a denial, because a caller that sets the header to something we do not
#: recognise has told us nothing we can act on.
_NOT_AGENT_VALUES = frozenset({"0", "false", "no", "off", "human", "user"})


def _caller_provenance(headers) -> tuple[bool | None, str | None]:
    """Read the caller's own declaration of who drove this request.

    Returns ``(agent_generated, agent_identity)`` where ``agent_generated`` is:

    * ``True``  — the caller declared itself an agent (header present, truthy value);
    * ``False`` — the caller explicitly declared it is not one (``0``/``false``/``human``…);
    * ``None``  — no header, or a value we do not recognise.

    The tri-state is the point. ``/l1``, ``/l2`` and the other profile routes are shared
    between the operator's own clients and autonomous agents, so a two-state flag would
    force every unlabelled request into "human" — inventing a fact about a request that
    never made a claim. Unknown stays unknown, and a report can say so.

    Never raises: this is bookkeeping beside a request that has already been served, and a
    malformed header must not turn a successful call into a failure.
    """
    try:
        raw = headers.get(AGENT_GENERATED_HEADER)
    except Exception:  # noqa: BLE001
        return None, None
    identity = None
    try:
        identity = headers.get(AGENT_IDENTITY_HEADER) or None
    except Exception:  # noqa: BLE001
        identity = None
    if raw is None:
        # No claim made. An identity header on its own still claims an agent, because the
        # only senders of it are agents.
        return (True if identity else None), identity
    value = str(raw).strip().lower()
    if value in _NOT_AGENT_VALUES:
        return False, identity
    return (True if value else None), identity


class LCPHandler(
    HealthEndpoints,
    ProviderEndpoints,
    ProfileEndpoints,
    KeyEndpoints,
    AlertEndpoints,
    BudgetEndpoints,
    PluginEndpoints,
    UsageEndpoints,
    DashboardEndpoints,
    SettingsEndpoints,
    MemoryEndpoints,
    WorkEndpoints,
    EvidenceEndpoints,
    AdminLoginEndpoints,
    AdminOIDCEndpoints,
    audit_log.AuditEndpoints,
    BaseHTTPRequestHandler,
):
    """HTTP request handler for LCP gateway."""

    # Class-level references set after server init
    config: Any = None
    engine: Any = None

    # Route tables are built once per process and reused across requests.
    _routes: "RouteTable | None" = None

    @classmethod
    def _route_table(cls) -> "RouteTable":
        """Return the (lazily built) declarative route table."""
        if cls._routes is None:
            cls._routes = _build_routes()
        return cls._routes


    def log_message(self, format, *args):
        """Suppress default http.server logging — we use structlog."""
        pass

    def _send_json(self, data: dict, status: int = 200):
        """Send a JSON response. Logs but does not crash on client disconnect."""
        body = json.dumps(data).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            logger.debug("client_disconnected", path=self.path, status=status)

    def _send_error(self, exc: Exception, status: int | None = None,
                    message: str | None = None) -> None:
        """Send a sanitized, structured error response.

        Format: ``{"error": {"code": "LCP-XXXX", "message": "..."}}``.

        The client-facing message never includes sensitive provider internals:
        exception ``client_message`` overrides win, provider-key patterns are
        redacted, and long messages are truncated. Full details are logged
        server-side by the caller.
        """
        if isinstance(exc, LCPError):
            payload = exc.to_dict()
            if message is not None:
                payload["message"] = _sanitize_message(message)
            else:
                payload["message"] = _sanitize_message(payload["message"])
            status = status if status is not None else exc.status_code
        else:
            payload = {"code": "LCP-5001", "message": "internal error"}
            status = status if status is not None else 500
        self._send_json({"error": payload}, status)

    def _resolve_profile(self) -> str | None:
        """Extract profile name from URL path. Returns None for non-profile routes."""
        path = self.path.rstrip("/")
        parts = path.split("/")
        if len(parts) >= 2:
            candidate = parts[1]
            if candidate in self.config.profiles:
                return candidate
        return None

    def _read_body(self) -> dict:
        """Read and parse JSON request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        raw = self.rfile.read(content_length)
        return json.loads(raw)

    def _serve_static(self):
        """Serve static files (JS, CSS, etc.) from the Jinja2 templates/static dir."""
        from pathlib import Path

        relative = self.path[len("/static/"):].split("?")[0]
        if ".." in relative or relative.startswith("/"):
            self._send_json({"error": "forbidden"}, 403)
            return

        static_dir = Path(__file__).resolve().parent.parent / "ui" / "templates" / "jinja" / "static"
        file_path = (static_dir / relative).resolve()
        if not str(file_path).startswith(str(static_dir.resolve())):
            self._send_json({"error": "forbidden"}, 403)
            return

        content_type = {
            ".js": "application/javascript",
            ".css": "text/css",
            ".html": "text/html",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(file_path.suffix, "application/octet-stream")

        if not file_path.is_file():
            self._send_json({"error": "not found"}, 404)
            return

        data = file_path.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache, max-age=0")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            logger.debug("client_disconnected", path=self.path, static_file=relative)

    # ── Routes ────────────────────────────────────────────────────────────

    def _serve_landing(self, index: bool = False):
        """Serve the public marketing landing page from ``<repo>/landing``.

        Public by design (``admin_auth.PUBLIC_EXACT`` / ``PUBLIC_PREFIXES``):
        it is the only surface a visitor without an admin session should see.
        Assets are referenced RELATIVE in index.html, so the page must be
        reached at ``/welcome/`` — ``/welcome`` redirects rather than serving
        HTML whose ``styles.css`` would resolve to the site root.
        """
        from pathlib import Path

        landing_dir = (Path(__file__).resolve().parent.parent.parent / "landing").resolve()

        if index:
            relative = "index.html"
        else:
            from urllib.parse import unquote

            # Decode BEFORE the traversal check: a percent-encoded `..%2f` would
            # otherwise be treated as a literal (harmless but answered 404
            # instead of 403, which reads as "no such file" rather than "refused").
            relative = unquote(self.path.split("?", 1)[0][len("/welcome/"):])
            if not relative:
                relative = "index.html"

        if ".." in relative or relative.startswith("/"):
            self._send_json({"error": "forbidden"}, 403)
            return

        file_path = (landing_dir / relative).resolve()
        if not str(file_path).startswith(str(landing_dir)):
            self._send_json({"error": "forbidden"}, 403)
            return

        if not file_path.is_file():
            # A branded 404, not a JSON blob: this is a public page.
            fallback = landing_dir / "404.html"
            if fallback.is_file():
                data = fallback.read_bytes()
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send_json({"error": "not found"}, 404)
            return

        content_type = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".html": "text/html; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".woff2": "font/woff2",
            ".ico": "image/x-icon",
        }.get(file_path.suffix, "application/octet-stream")

        data = file_path.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            # The landing is versioned by the container build, so a short cache
            # is correct here; the hashed-font/asset names are not content-hashed,
            # so a long max-age would pin a stale stylesheet across deploys.
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            logger.debug("client_disconnected", path=self.path, landing_file=relative)

    def _redirect_welcome(self):
        """``/welcome`` -> ``/welcome/`` so relative asset URLs resolve."""
        self.send_response(301)
        self.send_header("Location", "/welcome/")
        self.send_header("Content-Length", "0")
        self.end_headers()


    # Configurable models endpoint paths (env: LCP_MODELS_PATHS, default: /v1/models,/models)
    _models_paths = set(
        p.strip() for p in os.environ.get("LCP_MODELS_PATHS", "/v1/models,/models").split(",") if p.strip()
    )

    @staticmethod
    def _path_part(path: str, index: int) -> str:
        """Return URL-DECODED path segment at *index* (0-based on split('/')).

        Browser clients percent-encode names in URLs (e.g. a provider named
        "local llm zgx" becomes ``local%20llm%20zgx``). Without decoding, the
        config lookup compared the encoded form against the real (decoded)
        config keys and 404'd. Also normalizes NBSP → space (rich-text editors
        and UI forms can insert U+00A0 which is not the same key).
        """
        return unquote(path.split("?")[0].split("/")[index]).replace("\u00a0", " ")

    # ── admin authentication ────────────────────────────────────────────
    # ── Phase 5: change-history trail (guarded by the admin guard) ──────────
    def _serve_audit_trail(self) -> None:
        """GET /api/audit — newest-first change history (paged)."""
        limit, offset, total = 200, 0, None
        try:
            from urllib.parse import parse_qs
            qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            limit = max(1, min(1000, int((qs.get("limit") or ["200"])[0])))
            offset = max(0, int((qs.get("offset") or ["0"])[0]))
        except (ValueError, IndexError):
            pass
        rows = audit_log.recent(limit=limit, offset=offset)
        try:
            total = audit_log.count()
        except Exception:  # noqa: BLE001 — total is a nicety, never fail the page
            total = None
        self._send_json({"ok": True, "count": len(rows), "total": total,
                         "entries": rows})


    def _admin_guard(self) -> bool:
        """Return True when the request may proceed; otherwise respond and False.

        Called from **every** method entry point. The route table alone is not
        enough: ``do_PUT`` and ``do_DELETE`` are hand-written if/elif chains that
        never reach ``dispatch()``, so a guard placed only in the table would
        leave ``DELETE /api/keys/{id}`` open. Measured 2026-10-18.

        The proxy surface (``/{profile}/...``), ``/health``, ``/metrics``, the
        static assets and the login routes are public — see
        ``admin_auth.is_public_path``. The proxy keeps its own API-key auth.
        """
        if not hasattr(admin_auth, "is_authenticated"):
            # Defensive: a partial import must not turn into an open surface.
            return True
        if admin_auth.anonymous_admin_allowed():
            # The documented dev escape hatch. Coherent with the startup gate:
            # if the operator explicitly opted out of admin auth, the gate is
            # open by design and the startup log says so loudly.
            return True
        path_no_query = (self.path or "/").split("?", 1)[0]
        profiles = getattr(self.config, "profiles", {}) if self.config else {}
        if admin_auth.is_public_path(path_no_query, profiles):
            return True
        if admin_auth.is_authenticated(self):
            return True

        client_ip = self.client_address[0] if self.client_address else "-"
        logger.warning("admin_auth_rejected", path=path_no_query,
                       method=getattr(self, "command", "?"), client_ip=client_ip)
        if admin_auth.wants_json(self, path_no_query):
            self._send_json({"error": {"code": "LCP-4011",
                                       "message": "admin authentication required"}},
                            401)
        else:
            self.send_response(302)
            self.send_header("Location", "/admin/login?next=" + quote(path_no_query, safe="/"))
            self.send_header("Content-Length", "0")
            self.end_headers()
        return False

    def do_GET(self):
        """Dispatch a GET through the declarative route table.

        Replaces a 143-line if/elif chain of 60 string comparisons. Rules are
        matched in registration order; a miss is a 404, which is exactly the
        semantics the chain ended with.
        """
        logger.debug("request_start", method="GET", path=self.path,
                     client_ip=self.client_address[0])
        if not self._admin_guard():
            return
        if not self._route_table().dispatch(self, "GET"):
            self._send_json({"error": "not found"}, 404)
    def do_POST(self):
        profile = self._resolve_profile()
        logger.debug("request_start", method="POST", path=self.path,
                     client_ip=self.client_address[0], profile=profile or "none")

        # ── Admin / UI routes: guarded, then the declarative table ─────────
        # The guard runs here rather than after dispatch() because a POST that
        # matches no admin rule still falls through to the proxy path below,
        # and that path must keep its API-key auth and stay reachable.
        path_no_query = (self.path or "/").split("?", 1)[0]
        if not admin_auth.is_public_path(
                path_no_query, getattr(self.config, "profiles", {}) or {}):
            if not self._admin_guard():
                return
        if self._route_table().dispatch(self, "POST"):
            return

        # ── Proxy path: POST /{profile}/chat/completions ─────────────────────
        # Everything below the admin routes is the proxy itself. It used to be
        # inlined right here, which is what made do_POST 438 lines long.
        self._serve_chat_completions()

    def _serve_chat_completions(self):
        """Handle a POST /{profile}/chat/completions proxy request.

        Extracted verbatim from ``do_POST`` — resolves the profile (falling back
        to the body's ``model`` field), authenticates the key, enforces budgets,
        then drives the provider chain with prompt caching and cost recording.
        """
        # Only handle chat completions
        if "/chat/completions" not in self.path:
            logger.warning("invalid_route", method="POST", path=self.path,
                           client_ip=self.client_address[0])
            self._send_json({"error": "not found"}, 404)
            return

        # Read body early — needed for model→profile fallback and validation
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return

        profile = self._resolve_profile()
        if profile is None:
            # Fallback: resolve profile from "model" field in request body
            model_name = (body.get("model") or "").strip()
            if model_name in self.config.profiles:
                profile = model_name
                logger.info("profile_from_model", model=model_name, path=self.path)
            else:
                logger.warning("unknown_profile", path=self.path,
                               client_ip=self.client_address[0], model=model_name)
                self._send_json({"error": f"unknown profile in path: {self.path}. Use /PROFILE/chat/completions or set model to a profile name."}, 400)
                return

        profile_cfg = self.config.get_profile(profile)
        if profile_cfg is None:
            self._send_json({"error": f"profile not found: {profile}"}, 400)
            return

        # A profile whose chain was emptied by a provider removal is INVALID, not
        # broken. Say so explicitly instead of letting the request fail inside the
        # chain walk, where it would look like a provider outage.
        _invalid = getattr(self.config, "invalid_profiles", None)
        invalid_reason = _invalid.get(profile) if isinstance(_invalid, dict) else None
        if invalid_reason:
            logger.warning("profile_invalid", profile=profile, reason=invalid_reason,
                           client_ip=self.client_address[0])
            self._send_json({
                "error": f"profile '{profile}' is invalid: {invalid_reason}",
                "code": "profile_invalid",
                "profile": profile,
            }, 503)
            return

        # Auth check — if profile requires API key, validate Authorization header
        try:
            if profile_cfg.get("auth_required", True):
                auth_header = self.headers.get("Authorization", "")
                if not auth_header.startswith("Bearer "):
                    logger.warning("auth_failed", reason="missing_bearer_token",
                                   profile=profile, client_ip=self.client_address[0])
                    raise AuthError("API key required for this profile. Use Authorization: Bearer <key>")
                raw_key = auth_header[7:]
                km = resolve_service("key_manager", fallback=get_key_manager)
                if km:
                    key_info = km.validate_key(raw_key)
                    if key_info is None:
                        logger.warning("auth_failed", reason="invalid_or_revoked_key",
                                       profile=profile, client_ip=self.client_address[0])
                        raise AuthError("invalid or revoked API key")
                    # Check profile access
                    allowed = key_info.get("allowed_profiles")
                    if allowed:
                        allowed_list = [p.strip() for p in allowed.split(",") if p.strip()]
                        if profile not in allowed_list:
                            logger.warning("auth_failed", reason="profile_access_denied",
                                           profile=profile, client_ip=self.client_address[0],
                                           key_id=key_info.get("id"))
                            raise ForbiddenError(f"key does not have access to profile '{profile}'")
                    # Check budget-enforced spend limit (budgets are single source of truth)
                    if key_info.get("id"):
                        blocked = self._check_budget_block(profile, key_info.get("id"))
                        if blocked:
                            logger.warning("auth_failed", reason="budget_exceeded",
                                           profile=profile, client_ip=self.client_address[0],
                                           key_id=key_info.get("id"), budget=blocked)
                            raise CreditExhaustedError(
                                f"Budget '{blocked}' has been exceeded for this key/profile"
                            )
                    self._current_key_id = key_info.get("id")
                else:
                    # Fail CLOSED when the key-manager service cannot be
                    # resolved: an unavailable verifier must not behave as
                    # "no key needed" (CWE-287). Same 401 an invalid or
                    # revoked key produces.
                    logger.error(
                        "auth_failed",
                        reason="key_manager_unavailable",
                        profile=profile,
                        client_ip=self.client_address[0],
                    )
                    raise AuthError("Key manager unavailable — authentication cannot be verified. Try again later.")
        except (AuthError, CreditExhaustedError, ForbiddenError) as e:
            self._send_error(e)
            return

        # ── Budget enforcement (before LLM call) ──
        blocked_budget = self._check_budget_block(
            profile, getattr(self, '_current_key_id', None)
        )
        if blocked_budget:
            self._send_json({
                "error": {
                    "code": "LCP-4290",
                    "message": f"Budget '{blocked_budget}' has been exceeded. Further requests are blocked.",
                }
            }, 429)
            return

        # Set before the try: `finally` runs even on a path that never reached
        # the registry, and referencing an unassigned name there would raise
        # NameError, masking the real error.
        _inflight_token = None

        # Bound before the try so an exception raised BEFORE the header read still has a
        # name to report. (None, not False: the request never got far enough to be
        # classified, and a failure row claiming "not an agent" would be an invented fact.)
        agent_generated: bool | None = None
        agent_identity: str | None = None

        try:
            # Validate body has messages (after auth so we return 401 first if unauthenticated)
            if not isinstance(body.get("messages"), list) or len(body.get("messages", [])) == 0:
                self._send_json({"error": "missing required field: messages"}, 400)
                return

            # Tool stripping
            body, blocked_tools = strip_forbidden_tools(body, profile_cfg.get("forbidden_tools"))

            # Pre-request cost estimation (pricing falls back to the plugin
            # registry or default rates instead of hard-failing → 500)
            primary_step = profile_cfg["chain"][0]
            pricing = _resolve_pricing(
                self.config, primary_step["provider"], primary_step["model"]
            )
            try:
                estimation = estimate_from_request(
                    primary_step["model"],
                    body.get("messages", []),
                    body.get("tools"),
                    body.get("max_tokens", 1024),
                    pricing,
                )
            except Exception:  # noqa: BLE001 — estimation must never break a request
                estimation = {
                    "input_tokens": 0,
                    "estimated_output_tokens": 0,
                    "estimated_input_cost": 0.0,
                    "estimated_output_cost": 0.0,
                    "estimated_total_cost": 0.0,
                    "currency": "USD",
                }

            # Prompt cache check (skip cache for streaming requests - cached JSON cannot satisfy SSE)
            cache = resolve_service("prompt_cache", fallback=get_prompt_cache)
            primary_model = profile_cfg["chain"][0]["model"]
            cached = cache.get(profile, primary_model, body) if not body.get("stream", False) else None
            if cached is not None:
                latency_ms = 1  # near-instant
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("X-LCP-Cache", "HIT")
                    self.send_header("X-Estimated-Cost", str(estimation["estimated_total_cost"]))
                    self.end_headers()
                    self.wfile.write(json.dumps(cached).encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    logger.debug("client_disconnected", path=self.path, cache="HIT")
                logger.info("cache_hit_served", profile=profile, model=primary_model)
                return

            # Set estimated cost header
            self._pending_headers = {
                "X-LCP-Cache": "MISS",
                "X-Estimated-Cost": str(estimation["estimated_total_cost"]),
            }

            t0 = time.time()

            # Register the request as in-flight. The `requests` table gains
            # its row only in record_cost(), after the reply has left, so a
            # call that is still running is visible nowhere else. Cleared in
            # the `finally` below, which covers the error paths too.
            _inflight_token = in_flight.begin(
                profile,
                primary_step["model"],
                primary_step["provider"],
                prompt_tokens=estimation.get("input_tokens", 0),
                stream=bool(body.get("stream", False)),
                conversation_id=self.headers.get("x-opencode-session"),
                tools=len(body.get("tools") or []),
            )

            # CUR-329: the entry above names the PRIMARY step, but a chain that
            # falls back bills a different provider. try_chain announces each
            # step as it goes to the wire, so the live table shows what is
            # being tried NOW rather than what was tried first. Guarded inside
            # try_chain; a failed label update must never fail the request.
            def _note_inflight_step(step_provider, step_model):
                in_flight.update(_inflight_token, provider=step_provider,
                                 model=step_model)

            streaming = body.get("stream", False)

            # The client's per-conversation id, when it sends one. Hermes attaches
            # its agent session id to every LCP request (see the `lcp` user
            # provider plugin), so agent traffic is identified; other clients may
            # still omit it.
            #
            # This MUST be bound before the accounting calls below: they read
            # `conversation_id`, and the ternary they used only evaluated that name
            # when the header was present — so the undefined name stayed latent
            # while no client sent the header, then raised NameError for every
            # identified request and silently dropped its cost row.
            conversation_id = self.headers.get("x-opencode-session")

            # Who drove this request, as the caller declares it (alembic 027). Three
            # states, deliberately: a declared agent (1), a caller that says it is not
            # (0), and a caller that said nothing (None) — an unlabelled request must not
            # read as a person's. The identity is the caller's own name for itself and is
            # stored verbatim; it arrives from the wire, so it can never fail the request.
            agent_generated, agent_identity = _caller_provenance(self.headers)

            # Outbound requests must always carry a stable per-conversation header
            # (`x-opencode-session`); fall back to a per-profile id.
            session_id = conversation_id or f"lcp-{profile}"

            # Try provider chain
            _warning_sink: list[str] = []
            response_body, status, provider, model = try_chain(
                profile, profile_cfg, body, self.config, warning_sink=_warning_sink,
                session_id=session_id, on_step=_note_inflight_step,
            )
            if _warning_sink:
                self._pending_headers["X-LCP-Context-Warning"] = " | ".join(_warning_sink)

            latency_ms = int((time.time() - t0) * 1000)

            # ── Streaming response ──
            if streaming:
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-LCP-Cache", "MISS")
                self.send_header("X-Estimated-Cost", str(estimation["estimated_total_cost"]))
                if _warning_sink:
                    self.send_header("X-LCP-Context-Warning", " | ".join(_warning_sink))
                self.end_headers()

                sse_parts = []
                try:
                    for chunk in response_body:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        sse_parts.append(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    logger.debug("client_disconnected", path=self.path, stream="SSE")

                full_sse = b"".join(sse_parts)
                # Capture reasoning_content from the stream so it can be
                # re-attached to later requests (DeepSeek thinking mode).
                try:
                    capture_reasoning_from_sse(full_sse)
                except Exception:
                    pass
                last_chunk = extract_last_sse_chunk(full_sse)
                # Cost accounting is bookkeeping that runs AFTER the stream has
                # gone out. A failure in it — an unpriceable model, a DB hiccup —
                # must never surface as a 500 on a response the client already
                # received in full, so the whole block is guarded.
                cost_info = {"prompt_tokens": 0, "completion_tokens": 0,
                             "cache_hit_tokens": 0, "cache_miss_tokens": 0,
                             "cost": 0.0, "latency_ms": latency_ms}
                try:
                    if last_chunk and last_chunk.get("usage"):
                        # Price a streamed response through exactly the same path as
                        # a non-streamed one. This branch used to build the usage
                        # dict inline with DeepSeek's flat cache field names
                        # (`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`),
                        # which a provider reporting the OpenAI-compatible nested
                        # block (`usage.prompt_tokens_details.cached_tokens`) never
                        # fills in — OpenCode and CommandCode both report it there.
                        # Every prompt token was therefore recorded as a cache miss
                        # and priced at the miss rate, overstating cost ~10x on all
                        # streamed traffic (which is most of it).
                        cost_info = calculate_cost(provider, model, body, last_chunk,
                                                   self.config)
                        cost_info["latency_ms"] = latency_ms
                    else:
                        # SSE stream without usage — fall back to pre-flight estimation
                        cost_info = {
                            "prompt_tokens": estimation["input_tokens"],
                            "completion_tokens": 0,
                            "cache_hit_tokens": 0,
                            "cache_miss_tokens": estimation["input_tokens"],
                            "cost": estimation["estimated_total_cost"],
                            "latency_ms": latency_ms,
                        }
                    record_cost(self.engine, profile, model, provider, cost_info, True, None, blocked_tools,
                                conversation_id=conversation_id,
                                agent_generated=agent_generated,
                                agent_identity=agent_identity)

                    # Increment budget spend and fire alerts
                    self._track_budget_spend(
                        profile, cost_info["cost"],
                        getattr(self, '_current_key_id', None)
                    )
                except Exception as e:  # noqa: BLE001 — bookkeeping, not the response
                    logger.error("cost_accounting_failed", profile=profile,
                                 provider=provider, model=model, stream=True,
                                 error=str(e), traceback=traceback.format_exc()[-500:])

                # Conversation capture. The assistant's words are not in the
                # request body — they only exist in the stream this handler just
                # wrote out — so the reply text is recovered from the SSE buffer.
                # It stays bookkeeping: a failure stores nothing rather than
                # corrupting a response the client already received.
                conv_store.capture_request(
                    conversation_id, body,
                    reply=extract_sse_text(full_sse),
                    profile=profile, model=model,
                    agent_generated=agent_generated,
                    agent_identity=agent_identity,
                )
                # CUR-329: the transcript just grew — maybe past the titler's
                # minimum — so the conversation is now a titling candidate.
                # Queue-only on the request path; the worker does the work.
                conv_titler.enqueue(conversation_id, config=self.config)

                total_wall_ms = int((time.time() - t0) * 1000)
                logger.info(
                    "request_complete",
                    profile=profile,
                    provider=provider,
                    model=model,
                    latency_ms=latency_ms,
                    total_wall_ms=total_wall_ms,
                    tools_blocked=len(blocked_tools),
                    cache="HIT" if cost_info.get("cache_hit_tokens") else "MISS",
                    stream=True,
                )
                return

            # ── Non-streaming ──
            # Capture reasoning_content so it can be re-attached to later
            # requests (DeepSeek thinking mode requires it on tool-call turns).
            try:
                capture_reasoning_from_response(response_body)
            except Exception:
                pass
            cache.set(profile, model, body, response_body)

            # Token verification
            verifier = resolve_service("token_verifier", fallback=get_token_verifier)
            verification = verifier.verify(body.get("messages", []), response_body.get("usage", {}))
            if verification["suspicious"]:
                self._pending_headers["X-LCP-Token-Warning"] = (
                    f"suspicious: provider={verification['provider_prompt_tokens']} "
                    f"estimated={verification['estimated_prompt_tokens']} "
                    f"pct={verification['prompt_discrepancy_pct']}"
                )

            # Cost accounting — bookkeeping, so it must never be able to turn a
            # completion the upstream already produced (and charged for) into a
            # failure response. Guarded end to end: an unpriceable model or a
            # DB error degrades to a $0 line on a request that still succeeds.
            cost_info = {"prompt_tokens": 0, "completion_tokens": 0,
                         "cache_hit_tokens": 0, "cache_miss_tokens": 0,
                         "cost": 0.0, "latency_ms": latency_ms}
            try:
                # Calculate cost
                cost_info = calculate_cost(provider, model, body, response_body, self.config)
                cost_info["latency_ms"] = latency_ms

                # Record cost
                record_cost(self.engine, profile, model, provider, cost_info, True, None, blocked_tools,
                                conversation_id=conversation_id,
                                agent_generated=agent_generated,
                                agent_identity=agent_identity)

                # Budget spend tracking — unified: increments profile + key budgets
                # and syncs ApiKey.total_spend with key-scoped budgets.
                key_id = getattr(self, '_current_key_id', None)
                self._track_budget_spend(profile, cost_info["cost"], key_id)
            except Exception as e:  # noqa: BLE001 — bookkeeping, not the response
                logger.error("cost_accounting_failed", profile=profile,
                             provider=provider, model=model, stream=False,
                             error=str(e), traceback=traceback.format_exc()[-500:])

            # Send response with custom headers
            body_bytes = json.dumps(response_body).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body_bytes)))
                for hdr_name, hdr_val in self._pending_headers.items():
                    self.send_header(hdr_name, hdr_val)
                self.end_headers()
                self.wfile.write(body_bytes)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                logger.debug("client_disconnected", path=self.path, stream=False)

            # Conversation capture — see the streaming branch. Bookkeeping, so it
            # is independent of the response that has already been sent.
            conv_store.capture_request(
                conversation_id, body,
                reply=extract_message_text(response_body),
                profile=profile, model=model,
                agent_generated=agent_generated,
                agent_identity=agent_identity,
            )
            # CUR-329: titling candidate — see the streaming branch's enqueue.
            conv_titler.enqueue(conversation_id, config=self.config)

            total_wall_ms = int((time.time() - t0) * 1000)
            logger.info(
                "request_complete",
                profile=profile,
                provider=provider,
                model=model,
                cost=round(cost_info["cost"], 6),
                latency_ms=latency_ms,
                total_wall_ms=total_wall_ms,
                tools_blocked=len(blocked_tools),
                cache="MISS",
            )

        except ToolBlockedError as e:
            logger.warning("tool_blocked", profile=profile, error=str(e))
            self._send_error(e)
        except ProviderBadRequestError as e:
            # The provider rejected the request body as invalid (HTTP 400).
            # Report the exact error to the client so they can fix their request.
            logger.error("provider_bad_request", profile=profile, error=str(e))
            cost_info = {"prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0,
                         "cache_miss_tokens": 0, "cost": 0, "latency_ms": 0}
            record_cost(self.engine, profile, "unknown", "unknown", cost_info, False,
                       "provider_bad_request", [], error_detail=str(e),
                       agent_generated=agent_generated, agent_identity=agent_identity)
            self._send_error(e)
        except AllProvidersFailedError as e:
            logger.error("all_providers_failed", profile=profile, error=str(e))
            cost_info = {"prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0,
                         "cache_miss_tokens": 0, "cost": 0, "latency_ms": 0}
            record_cost(self.engine, profile, "unknown", "unknown", cost_info, False,
                       "all_providers_failed", [], error_detail=str(e),
                       agent_generated=agent_generated, agent_identity=agent_identity)
            self._send_error(e)
        except ProviderError as e:
            # Any other typed provider error (timeout, rate limit, upstream
            # auth, 5xx) that escaped the chain fallback — surface it cleanly.
            logger.error("provider_error", profile=profile, error=str(e))
            cost_info = {"prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0,
                         "cache_miss_tokens": 0, "cost": 0, "latency_ms": 0}
            record_cost(self.engine, profile, "unknown", "unknown", cost_info, False,
                       e.code, [], error_detail=str(e),
                       agent_generated=agent_generated, agent_identity=agent_identity)
            self._send_error(e)
        except LCPError as e:
            logger.error("lcp_error", profile=profile, error=str(e))
            self._send_error(e)
        except Exception as e:
            logger.error("unhandled_error", error=str(e), traceback=traceback.format_exc()[-500:])
            self._send_error(e)
        finally:
            # Every exit path -- success, typed provider error, unhandled
            # exception -- must deregister, or the In-progress page shows a
            # request that has already finished or failed.
            in_flight.end(_inflight_token)

    def do_PUT(self):
        logger.debug("request_start", method="PUT", path=self.path,
                     client_ip=self.client_address[0])
        if not self._admin_guard():
            return
        if self.path.startswith("/api/providers/") and len(self.path.split("?")[0].split("/")) == 4:
            provider_name = self._path_part(self.path, 3)
            self._serve_provider_update(provider_name)
        elif self.path.startswith("/api/chains/") and len(self.path.split("/")) == 4:
            profile = self.path.split("/")[3]
            self._serve_chain_reorder(profile)
        elif self.path.startswith("/api/profiles/") and self.path.endswith("/budget"):
            # PUT /api/profiles/{name}/budget
            parts = self.path.split("/")
            self._serve_profile_budget_update(parts[3])
        elif self.path.startswith("/api/profiles/") and len(self.path.split("/")) == 4:
            profile = self.path.split("/")[3]
            self._serve_profile_update(profile)
        elif self.path == "/api/alerts/config":
            self._serve_alerts_config_update()
        elif self.path == "/api/alerts/thresholds":
            self._serve_alert_thresholds_update()
        elif self.path.startswith("/api/budgets/") and len(self.path.split("/")) == 4:
            budget_id = self.path.split("/")[3]
            self._serve_budget_update(budget_id)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        logger.debug("request_start", method="DELETE", path=self.path,
                     client_ip=self.client_address[0])
        if not self._admin_guard():
            return
        if self.path.startswith("/api/providers/") and len(self.path.split("?")[0].split("/")) == 4:
            provider_name = self._path_part(self.path, 3)
            self._serve_provider_delete(provider_name)
        elif self.path.startswith("/api/keys/") and len(self.path.split("/")) == 4:
            key_id = self.path.split("/")[3]
            self._serve_key_delete(key_id)
        elif (self.path.split("?")[0].startswith("/api/profiles/")
              and self.path.split("?")[0].endswith("/avatar")
              and len(self.path.split("?")[0].split("/")) == 5):
            # DELETE /api/profiles/{name}/avatar
            self._serve_profile_avatar_delete(self._path_part(self.path, 3))
        elif self.path.startswith("/api/profiles/") and len(self.path.split("/")) == 4:
            profile = self.path.split("/")[3]
            self._serve_profile_delete(profile)
        elif self.path.startswith("/api/budgets/") and len(self.path.split("/")) == 4:
            budget_id = self.path.split("/")[3]
            self._serve_budget_delete(budget_id)
        elif self.path.startswith("/api/models/registry/") and len(self.path.split("/")) == 5:
            logical = self.path.split("/")[4]
            self._serve_registry_delete_api(logical)
        else:
            self._send_json({"error": "not found"}, 404)

    # ── Budget Enforcement ────────────────────────────────────────────────

    def _check_budget_block(self, profile: str, key_id: int | None = None) -> str | None:
        """Check if any active budget with action=block is exceeded.

        Returns the budget name if blocked, or None if allowed.
        """
        try:
            with get_session(self.engine) as session:
                query = session.query(Budget).filter(
                    Budget.action == "block",
                    Budget.status.in_(["active", "exceeded"]),
                )
                # Match budgets for this profile, or global (null profile)
                query = query.filter(
                    or_(Budget.profile == profile, Budget.profile.is_(None))
                )
                if key_id is not None:
                    query = query.filter(
                        or_(Budget.key_id == key_id, Budget.key_id.is_(None))
                    )
                else:
                    query = query.filter(Budget.key_id.is_(None))

                for budget in query.all():
                    if budget.amount > 0 and budget.current_spend >= budget.amount:
                        logger.warning(
                            "budget_blocked",
                            budget_name=budget.name,
                            profile=profile,
                            spend=budget.current_spend,
                            limit=budget.amount,
                        )
                        return budget.name
        except Exception as e:
            logger.error("budget_check_failed", error=str(e))
        return None

    def _increment_budget_spend(self, profile: str, cost: float, key_id: int | None = None) -> list[dict]:
        """Increment spend on matching budgets and return any threshold breaches."""
        breaches = []
        try:
            with get_session(self.engine) as session:
                query = session.query(Budget).filter(
                    Budget.status == "active",
                )
                query = query.filter(
                    or_(Budget.profile == profile, Budget.profile.is_(None))
                )
                if key_id is not None:
                    query = query.filter(
                        or_(Budget.key_id == key_id, Budget.key_id.is_(None))
                    )
                else:
                    query = query.filter(Budget.key_id.is_(None))

                for budget in query.all():
                    old_spend = budget.current_spend
                    budget.current_spend = round(old_spend + cost, 6)

                    # Check thresholds
                    if budget.amount > 0:
                        old_pct = (old_spend / budget.amount) * 100
                        new_pct = (budget.current_spend / budget.amount) * 100
                        for t in [int(t) for t in budget.threshold_pct.split(",") if t]:
                            if old_pct < t <= new_pct:
                                breaches.append({
                                    "budget_id": budget.id,
                                    "budget_name": budget.name,
                                    "threshold": t,
                                    "spend_pct": round(new_pct, 1),
                                    "limit": budget.amount,
                                    "current_spend": budget.current_spend,
                                })
                    if budget.current_spend >= budget.amount and budget.amount > 0:
                        budget.status = "exceeded"
                        budget.last_alert_at = datetime.now(timezone.utc).isoformat()

                    # Sync ApiKey.total_spend with key-scoped budget
                    if budget.key_id is not None:
                        key = session.query(ApiKey).filter(ApiKey.id == budget.key_id).first()
                        if key:
                            key.total_spend = budget.current_spend

                session.commit()
                return breaches
        except Exception as e:
            logger.error("budget_increment_failed", error=str(e))
        return []

    def _track_budget_spend(self, profile: str, cost: float, key_id: int | None = None) -> None:
        """Increment budgets and fire alerts for any threshold breaches."""
        breaches = self._increment_budget_spend(profile, cost, key_id)
        am = resolve_service("alert_manager", fallback=get_alert_manager)
        for breach in breaches:
            severity = "critical" if breach["spend_pct"] >= 100 else "warning" if breach["spend_pct"] >= 80 else "info"
            am.fire(
                rule="budget_breach",
                severity=severity,
                title=f"Budget '{breach['budget_name']}' at {breach['spend_pct']}%",
                message=f"Budget '{breach['budget_name']}' has reached {breach['spend_pct']}% of its ${breach['limit']:.2f} limit (${breach['current_spend']:.4f} spent).",
                dedup_key=f"budget:{breach['budget_id']}:t{breach['threshold']}",
                metadata=breach,
            )


def _build_routes() -> RouteTable:
    """Build the declarative route table.

    ORDER IS SIGNIFICANT — the first matching rule wins, which is the contract
    the previous if/elif chain had. Specific paths must be registered before
    generic ones that would also match them (e.g. ``/api/providers/presets``
    before ``/api/providers``).

    Rule names are used by ``RouteTable.describe()`` for the routing inventory
    and are asserted against in tests.
    """
    t = RouteTable()
    _mp = LCPHandler._models_paths

    # ── pages ──
    # M2: five control-plane entries. The old page routes stay and REDIRECT to
    # the page that now owns their content, so existing links and bookmarks keep
    # working. Work is a module with its own four entries.
    t.get("page.activity", exact("/activity"),
          lambda h, p: h._serve_activity_page())
    # What the gateway is working on NOW, and everything it has done. Exact match,
    # so it cannot shadow the per-profile `/{profile}/dashboard` suffix rule below.
    t.get("page.conversations", exact("/conversations"),
          lambda h, p: h._serve_conversations_page())
    # Legacy URL: the live table now heads /conversations, so this redirects there.
    t.get("page.in_progress", exact("/in-progress"),
          lambda h, p: h._serve_in_progress_page())
    t.get("page.dashboard", exact("/", "/dashboard"),
          lambda h, p: h._serve_dashboard())
    t.get("page.keys", exact("/keys", "/keys/dashboard"),
          lambda h, p: h._serve_keys_dashboard())
    t.get("page.providers", exact("/providers"),
          lambda h, p: h._serve_providers_page())
    t.get("page.profiles", exact("/profiles"),
          lambda h, p: h._serve_profiles_page())
    t.get("page.profile.detail", regex(r"^/profiles/(?P<name>[^/]+)$"),
          lambda h, p: h._serve_profile_detail_page(h._path_part(h.path, 2)))
    t.get("api.profile.avatar", regex(r"^/api/profiles/(?P<name>[^/]+)/avatar$"),
          lambda h, p: h._serve_profile_avatar(h._path_part(h.path, 3)))
    t.get("page.models", exact("/models"),
          lambda h, p: h._serve_models_page())
    # ── Work section (work layer merged into LCP as a module) ──
    t.get("page.work.cron", exact("/work/cron"),
          lambda h, p: h._serve_work_cron_page())
    t.get("page.work.config", exact("/work/config"),
          lambda h, p: h._serve_work_config_page())
    t.get("page.usage", exact("/usage"),
          lambda h, p: h._serve_usage_page())
    t.get("page.logs", exact("/logs"),
          lambda h, p: h._serve_logs_page())
    t.get("page.alerts", exact("/alerts"),
          lambda h, p: h._serve_alerts_page())
    # CUR-329: estate-level switches (titling today). Registered with the other
    # pages, before the per-profile suffix rule below.
    t.get("page.settings", exact("/settings"),
          lambda h, p: h._serve_settings_page())

    # ── public marketing landing (no admin session required) ──
    # /welcome is PUBLIC_EXACT in admin_auth; /welcome/ serves its assets.
    # The bare path redirects so the page's relative URLs resolve.
    t.get("page.welcome.redirect", exact("/welcome"),
          lambda h, p: h._redirect_welcome())
    t.get("page.welcome", prefix("/welcome/"),
          lambda h, p: h._serve_landing())

    # ── per-profile dashboard: /{profile}/dashboard ──
    def _dashboard(h, p):
        profile = h._resolve_profile()
        if profile:
            h._serve_dashboard(profile_filter=profile)
        else:
            h._serve_dashboard()

    t.get("page.dashboard.profile", suffix("/dashboard"), _dashboard)

    # ── admin login / logout (public: these ARE the auth surface) ──
    t.get("page.admin.login", exact("/admin/login"),
          lambda h, p: h._serve_admin_login_page())
    t.post("api.admin.login", exact("/admin/login"),
           lambda h, p: h._serve_admin_login_post())
    t.get("page.admin.logout", exact("/admin/logout"),
          lambda h, p: h._serve_admin_logout())
    t.post("api.admin.logout", exact("/admin/logout"),
           lambda h, p: h._serve_admin_logout())
    t.get("page.admin.oidc.start", exact("/admin/oidc/start"),
          lambda h, p: h._serve_admin_oidc_start())
    t.get("page.admin.oidc.callback", exact("/admin/callback"),
          lambda h, p: h._serve_admin_oidc_callback())

    # ── in-progress (JSON for the live section's own refresh loop) ──
    t.get("api.in_progress", exact("/api/in-progress"),
          lambda h, p: h._serve_in_progress_json())

    # ── conversations (history table + per-conversation detail) ──
    t.get("api.conversations", exact("/api/conversations"),
          lambda h, p: h._serve_conversations_json())
    t.get("api.conversation.messages", exact("/api/conversations/messages"),
          lambda h, p: h._serve_conversation_messages_json())
    t.get("api.conversation.audit", exact("/api/conversations/audit"),
          lambda h, p: h._serve_conversation_audit_json())

    # ── health / models ──
    t.get("health", exact("/health"), lambda h, p: h._serve_health())
    t.get("models.list", exact(*sorted(_mp)), lambda h, p: h._serve_models())

    def _models_for_profile(h, p):
        profile = h._resolve_profile()
        if profile and profile in h.config.profiles:
            h._serve_models(profile=profile)
        else:
            h._send_json({"error": "not found"}, 404)

    t.get("models.list.profile",
          any_of(*[suffix("/" + q.lstrip("/")) for q in sorted(_mp)]),
          _models_for_profile,
          note="per-profile: /coder/v1/models, /coder/models")

    # ── static / diagnostics ──
    t.get("static", prefix("/static/"), lambda h, p: h._serve_static())
    t.get("errors", prefix("/errors"), lambda h, p: h._serve_errors())
    t.get("cache.stats", exact("/cache/stats"), lambda h, p: h._serve_cache_stats())
    t.get("metrics", exact("/metrics"), lambda h, p: h._serve_metrics())
    t.get("export", exact("/export"), lambda h, p: h._serve_export())

    # ── evidence export (Phase 4 — admin-guarded via the global guard) ──
    t.get("api.evidence.export", exact("/api/evidence/export"),
          lambda h, p: h._serve_evidence_export())
    t.post("api.evidence.export", exact("/api/evidence/export"),
           lambda h, p: h._serve_evidence_export())
    t.get("api.evidence.public_key", exact("/api/evidence/public-key"),
          lambda h, p: h._serve_evidence_public_key())
    t.get("api.evidence.download", exact("/api/evidence/download"),
          lambda h, p: h._serve_evidence_download())

    # ── settings / routing ──
    t.get("api.settings", exact("/api/settings"),
          lambda h, p: h._serve_settings_api())
    # CUR-329: the conversations-titling section. Registered BEFORE
    # api.settings.cache.* would matter only if the matchers overlapped —
    # they do not — but it sits with its siblings for readability.
    t.get("api.settings.conversations", exact("/api/settings/conversations"),
          lambda h, p: h._serve_conversations_settings_api())
    t.post("api.settings.conversations.title",
          exact("/api/settings/conversations/title"),
          lambda h, p: h._serve_conversations_title_api())
    t.get("api.routing.status", exact("/api/routing/status"),
          lambda h, p: h._serve_routing_status_api())
    # ── Work layer ──
    # The cron and sources APIs are gone (2026-10-25): their only caller was the
    # Cron and Config tabs, both decommissioned. The host-side collector reads and
    # writes work-sources.json directly from the shared ops mount, so nothing
    # outside this process depended on them.
    t.get("api.work.requests", exact("/api/work/requests"),
          lambda h, p: h._serve_work_requests_api())
    t.get("api.work.provider-decisions", exact("/api/work/provider-decisions"),
          lambda h, p: h._serve_work_provider_decisions_api())

    # ── cost / usage / logs ──
    t.get("api.daily-costs", exact("/api/daily-costs"),
          lambda h, p: h._serve_daily_costs_api())
    t.get("api.recent-requests", exact("/api/recent-requests"),
          lambda h, p: h._serve_recent_requests_api())
    t.get("api.logs", exact("/api/logs"), lambda h, p: h._serve_logs_api())
    t.get("api.usage.stats", exact("/api/usage/stats"),
          lambda h, p: h._serve_usage_stats_api())
    t.get("api.usage.totals", exact("/api/usage/totals"),
          lambda h, p: h._serve_usage_totals_api())
    t.get("api.usage.profile-series", exact("/api/usage/profile-series"),
          lambda h, p: h._serve_usage_profile_series_api())

    # ── providers (presets before the bare list) ──
    t.get("api.providers.presets", exact("/api/providers/presets"),
          lambda h, p: h._serve_provider_presets())
    t.get("api.providers.health", exact("/api/providers/health"),
          lambda h, p: h._serve_providers_health_api())
    # Active probing. Exact paths, declared next to the other /api/providers/*
    # GETs so a provider literally named "probe" cannot shadow them.
    t.get("api.providers.probe", exact("/api/providers/probe"),
          lambda h, p: h._serve_health_probe_status())
    t.get("api.providers.failovers", exact("/api/providers/failovers"),
          lambda h, p: h._serve_providers_failovers_api())
    t.get("api.providers.failures",
          regex(r"^/api/providers/(?P<name>[^/]+)/failures$"),
          lambda h, p: h._serve_provider_failures_api(
              h._path_part(h.path, 3)))
    # Self-hosted engines have no billing API: this reads the box's own /health
    # + /metrics so the Usage page can show what a local provider is actually
    # doing. Sits with the other /api/providers/* GETs, before the bare list.
    t.get("api.providers.engine",
          regex(r"^/api/providers/(?P<name>[^/]+)/engine$"),
          lambda h, p: h._serve_provider_engine(h._path_part(h.path, 3)))
    t.get("api.providers", exact("/api/providers"),
          lambda h, p: h._serve_providers_list())

    # ── profiles ──
    t.get("api.profiles.budget",
          regex(r"^/api/profiles/(?P<name>[^/]+)/budget$"),
          lambda h, p: h._serve_profile_budget(h._path_part(h.path, 3)))
    t.get("api.profiles", exact("/api/profiles"),
          lambda h, p: h._serve_profiles_list())

    # ── keys ──
    t.get("api.keys.detail", regex(r"^/api/keys/(?P<id>[^/]+)$"),
          lambda h, p: h._serve_key_detail(h._path_part(h.path, 3)))
    t.get("api.audit", exact("/api/audit"),
          lambda h, p: h._serve_audit_trail())

    t.get("api.keys", exact("/api/keys"), lambda h, p: h._serve_keys_list())

    # ── alerts / budgets ──
    t.get("api.alerts.config", exact("/api/alerts/config"),
          lambda h, p: h._serve_alerts_config())
    # Per-provider, per-window credit floors — the Alerts page's Thresholds panel and
    # the L2 provider watch read this same payload.
    t.get("api.alerts.thresholds", exact("/api/alerts/thresholds"),
          lambda h, p: h._serve_alert_thresholds())
    t.get("api.alerts.active", exact("/api/alerts/active"),
          lambda h, p: h._serve_alerts_active())
    t.get("api.alerts", exact("/api/alerts"), lambda h, p: h._serve_alerts_list())
    t.get("api.budgets.status", exact("/api/budgets/status"),
          lambda h, p: h._serve_budgets_status())
    t.get("api.budgets", exact("/api/budgets"), lambda h, p: h._serve_budgets_list())

    # ── cost plugins ──
    t.get("api.cost-plugins.usage", exact("/api/cost-plugins/usage"),
          lambda h, p: h._serve_plugin_usage())
    t.get("api.cost-plugins.balances", exact("/api/cost-plugins/balances"),
          lambda h, p: h._serve_plugin_balances())
    t.get("api.cost-plugins.summary", exact("/api/cost-plugins/summary"),
          lambda h, p: h._serve_plugin_summary())
    t.get("api.cost-plugins.subscriptions", exact("/api/cost-plugins/subscriptions"),
          lambda h, p: h._serve_plugin_subscriptions())
    t.get("api.cost-plugins.cookie",
          regex(r"^/api/cost-plugins/cookie/(?P<provider>[^/]+)$"),
          lambda h, p: h._serve_plugin_cookie_get(h._path_part(h.path, 4)))
    t.get("api.cost-plugins.workspace-id",
          regex(r"^/api/cost-plugins/workspace-id/(?P<provider>[^/]+)$"),
          lambda h, p: h._serve_plugin_workspace_id_get(h._path_part(h.path, 4)))

    # ── models ──
    t.get("api.models.capability", exact("/api/models/capability"),
          lambda h, p: h._serve_capability_api())
    t.get("api.models.registry", exact("/api/models/registry"),
          lambda h, p: h._serve_registry_api())
    # The capability flags the vision gate reads (supports_vision / thinking /
    # context window) — one row per model_limits entry. Replaces the invisible
    # DB-only surface that let CUR-144 hide.
    t.get("api.models.limits", exact("/api/models/limits"),
          lambda h, p: h._serve_model_limits_api())

    # ── memory: GET /{profile}/memory/count (last resort, as before) ──
    def _memory_count(h, p):
        profile = h._memory_profile()
        if profile:
            h._serve_memory_api(profile, "count")
        else:
            h._send_json({"error": "not found"}, 404)

    t.get("memory.count", regex(r"^/(?P<profile>[^/]+)/memory/count$"), _memory_count)


    # ── POST: admin / UI routes ──────────────────────────────────────────
    t.post("api.providers.test", exact("/api/providers/test"),
           lambda h, p: h._serve_provider_test())
    t.post("api.providers.probe", exact("/api/providers/probe"),
           lambda h, p: h._serve_health_probe_action())
    t.post("api.providers.discover", exact("/api/providers/discover"),
           lambda h, p: h._serve_provider_discover())
    t.post("api.providers.toggle",
           prefix_suffix("/api/providers/", "/toggle"),
           lambda h, p: h._serve_provider_toggle(h._path_part(h.path, 3)))
    t.post("api.providers.rename",
           prefix_suffix("/api/providers/", "/rename"),
           lambda h, p: h._serve_provider_rename(h._path_part(h.path, 3)))
    t.post("api.providers", exact("/api/providers"),
           lambda h, p: h._serve_provider_create())

    t.post("api.profile.avatar", regex(r"^/api/profiles/(?P<name>[^/]+)/avatar$"),
          lambda h, p: h._serve_profile_avatar_put(h._path_part(h.path, 3)))
    # M2d: seed the lane -> agent-profile mapping. Registered here, ahead of the
    # exact "/api/profiles" rule, because the first matching rule wins.
    t.post("api.profile.mapping.seed", exact("/api/profiles/mapping/seed"),
           lambda h, p: h._serve_profile_mapping_seed())
    t.post("api.profiles", exact("/api/profiles"),
           lambda h, p: h._serve_profile_create())

    t.post("api.keys.rotate", prefix_suffix("/api/keys/", "/rotate"),
           lambda h, p: h._serve_key_rotate(h._path_part(h.path, 3)))
    t.post("api.keys", exact("/api/keys"), lambda h, p: h._serve_key_create())

    t.post("api.alerts.test-webhook", exact("/api/alerts/webhook/test"),
           lambda h, p: h._serve_alerts_test_webhook())
    t.post("api.alerts.acknowledge",
           prefix_suffix("/api/alerts/", "/acknowledge"),
           lambda h, p: h._serve_alert_acknowledge(h._path_part(h.path, 3)))

    t.post("api.budgets", exact("/api/budgets"),
           lambda h, p: h._serve_budget_create())

    t.post("api.settings.cache.refresh", exact("/api/settings/cache/refresh"),
           lambda h, p: h._serve_settings_refresh_api())
    t.post("api.settings.cache.clear", exact("/api/settings/cache/clear"),
           lambda h, p: h._serve_settings_cache_clear())
    t.post("api.settings", exact("/api/settings"),
           lambda h, p: h._serve_settings_update_api())
    t.post("api.settings.conversations", exact("/api/settings/conversations"),
           lambda h, p: h._serve_conversations_settings_update_api())

    t.post("api.routing.policy", exact("/api/routing/policy"),
           lambda h, p: h._serve_routing_policy_api())
    t.post("api.routing.rules", exact("/api/routing/rules"),
           lambda h, p: h._serve_routing_rules_api())
    t.get("api.intent_model.config", prefix("/api/intent_model"),
          lambda h, p: h._serve_intent_model_api())
    t.post("api.intent_model.config", exact("/api/intent_model"),
           lambda h, p: h._serve_intent_model_api())

    t.post("api.circuit-breaker.reset", prefix("/api/circuit-breaker/reset"),
           lambda h, p: h._serve_circuit_breaker_reset())

    t.post("api.models.registry", exact("/api/models/registry"),
           lambda h, p: h._serve_registry_upsert_api())
    t.post("api.models.capability.manual", exact("/api/models/capability/manual"),
           lambda h, p: h._serve_capability_manual_api())
    t.post("api.models.capability.seed", exact("/api/models/capability/seed"),
           lambda h, p: h._serve_capability_seed_api())
    # Idempotent write of one model's capability flags (no restart needed — the
    # change lands in the live Config the gate reads).
    t.post("api.models.limits", exact("/api/models/limits"),
           lambda h, p: h._serve_model_limits_update_api())
    # Probe a model with a real image and (optionally) write the answer back.
    t.post("api.models.limits.probe", exact("/api/models/limits/probe"),
           lambda h, p: h._serve_model_limits_probe_api())

    t.post("api.cost-plugins.cookie",
           regex(r"^/api/cost-plugins/cookie/(?P<provider>[^/]+)$"),
           lambda h, p: h._serve_plugin_cookie_set(h._path_part(h.path, 4)))
    t.post("api.cost-plugins.workspace-id",
           regex(r"^/api/cost-plugins/workspace-id/(?P<provider>[^/]+)$"),
           lambda h, p: h._serve_plugin_workspace_id_set(h._path_part(h.path, 4)))

    # memory: POST /{profile}/memory/{retain|recall|forget}
    def _memory_write(h, p):
        profile = h._memory_profile()
        if profile:
            h._serve_memory_api(profile, p["action"])
        else:
            h._send_json({"error": "not found"}, 404)

    t.post("memory.write",
           regex(r"^/(?P<profile>[^/]+)/memory/(?P<action>retain|recall|forget)$"),
           _memory_write)


    return t
