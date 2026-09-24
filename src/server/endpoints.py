"""Endpoint mixin classes for LCPHandler.

Each class groups related ``_serve_*`` methods by domain. ``LCPHandler``
inherits from all of them via multiple inheritance.

    HealthEndpoints      (~456 lines)  health, models, errors, metrics, export
    ProviderEndpoints    (~500)        provider CRUD, test, discover, toggle, rename
    ProfileEndpoints     (~150)        profile CRUD, per-profile budget
    KeyEndpoints         (~77)         API key CRUD, rotate
    AlertEndpoints       (~50)         alert list/config/active, webhook, acknowledge
    BudgetEndpoints      (~147)        budget CRUD + status
    PluginEndpoints      (~105)        cost-plugin usage/balances/summary/subscriptions
    SettingsEndpoints    (~217)        settings read/update, cache refresh/clear
    UsageEndpoints       (~258)        daily costs, recent requests, logs, usage stats
    DashboardEndpoints   (~702)        dashboard page + its aggregated views
    MemoryEndpoints      (~137)        per-profile memory count/retain/recall/forget
    SetupEndpoints       (~137)        first-run setup wizard + install progress

Routing lives in ``src/server/router.py`` — this module only implements the
handlers. Module-level ``_helpers`` below are shared by several classes.

WHY THIS IS ONE MODULE (measured, 2026-09-18) — do not split it naively
-----------------------------------------------------------------------
This file is ~3,250 lines and is the largest single module in the repo, so the
obvious move is one file per class. That was assessed and rejected, because it
is NOT a mechanical change and it would make the test suite worse:

* ``tests/`` patches module-global dependencies through THIS module's
  namespace in 69 places — ``src.server.endpoints.get_session`` (17),
  ``resolve_service`` (13), ``get_credential_store`` (12), ``get_registry``
  (9), ``get_alert_manager`` (7), ``_credential_store_for`` (5),
  ``get_key_manager`` (4), ``_auto_learn_model_contexts`` (2).
* Python binds a patched name to the module that *resolves* it. Moving a class
  into a submodule means its dependencies resolve in that submodule, so every
  patch site must be retargeted.
* Worse, the shared dependencies are used broadly — ``resolve_service`` appears
  54 times and ``get_session`` 25 times across many classes — so a single
  ``patch("...resolve_service")`` covering a test that exercises several
  endpoints would have to become up to twelve patches.

The classes are already cleanly separated by concern; the file is only a
container. Splitting it is therefore a test-refactor project (rewrite 69 patch
targets, accepting worse ergonomics), not a code cleanup. Do that deliberately
if ever, not as a drive-by.
"""

import base64
import ipaddress
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

from ..api.logging_config import get_logger
from ..api.circuit_breaker import get_circuit_breaker
from ..api.key_manager import get_key_manager
from ..api.credential_store import get_credential_store
from ..api.alert_manager import get_alert_manager
from ..api.cost_plugins import get_registry
from ..api.models import get_session, Request as RequestModel, Budget, ApiKey, FailoverEvent
from ..api.prompt_cache import get_prompt_cache
from ..api.token_verifier import get_token_verifier
from ..api.runtime import resolve_service
from ..ui.dashboard import render_dashboard

logger = get_logger("lcp.server")


def _credential_store_for(handler):
    """Resolve the credential store from the active runtime, falling back to
    the legacy lazy-init against the handler's engine when no runtime is bound
    (standalone/tests)."""
    return resolve_service(
        "credential_store",
        fallback=lambda: get_credential_store(handler.engine),
    )


# Networks a provider test/discover fetch must never reach unless explicitly
# allowlisted (SSRF guard, CWE-918). Covers RFC1918, loopback, link-local
# (includes the cloud metadata address 169.254.169.254), CGNAT, multicast,
# reserved, IPv6 loopback/ULA/link-local/multicast and the documentation range.
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("2001:db8::/32"),
)


def _ssrf_allowlist() -> list:
    """Parse LCP_SSRF_ALLOWLIST (comma-separated CIDRs) into ip_network objects.

    Invalid entries are logged and skipped; the default (unset) policy is
    public-only, i.e. provider test/discover may only fetch public addresses.
    Set it (e.g. ``LCP_SSRF_ALLOWLIST=192.168.1.0/24`` in docker-compose) for
    operators who deliberately test providers that live on private networks.
    """
    networks: list = []
    for token in os.environ.get("LCP_SSRF_ALLOWLIST", "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("ssrf_allowlist_invalid_cidr", cidr=token)
    # Loopback has two representations (127.0.0.0/8 and ::1). Allowlisting
    # either one must permit both, otherwise dual-stack localhost resolution
    # (127.0.0.1 + ::1) still gets rejected despite an explicit 127/8 allow.
    if any(ipaddress.ip_network("127.0.0.0/8").subnet_of(net) for net in networks):
        networks.append(ipaddress.ip_network("::1/128"))
    return networks


def _validate_api_base_destination(api_base: str) -> str | None:
    """Return None when *api_base* may be fetched by provider test/discover,
    else a human-readable rejection reason.

    Rules:
      * scheme must be http or https (no file://, gopher://, ...)
      * host must be present and the port valid
      * every resolved address must be PUBLIC unless it matches an entry in
        LCP_SSRF_ALLOWLIST (comma-separated IPv4/IPv6 CIDRs)
      * a host that fails to resolve is allowed through — the subsequent
        urlopen fails naturally and no internal address is reachable.

    The all-resolved-addresses check runs against the post-resolution IPs so
    literal loopback/link-local forms are handled explicitly, and mixed
    public+private result sets are rejected.
    """
    parsed = urlparse(api_base)
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme '{parsed.scheme or 'none'}' — only http/https are allowed"
    host = parsed.hostname
    if not host:
        return "missing host"
    try:
        port = parsed.port
    except ValueError:
        return "invalid port"
    if port is not None and not (1 <= port <= 65535):
        return "invalid port"
    try:
        infos = socket.getaddrinfo(
            host, port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror:
        return None  # unresolvable → urlopen fails naturally; nothing reachable
    allow = _ssrf_allowlist()
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in net for net in _PRIVATE_NETWORKS) and not any(ip in net for net in allow):
            return f"destination '{host}' resolves to non-public address {ip}"
    return None


def _engine_db_path(engine) -> str:
    """Return the SQLite file path backing *engine* (fallback ``data/costs.db``)."""
    try:
        if getattr(engine, "url", None) is not None:
            path = str(engine.url.database)
            if path and path != ":memory:":
                return path
    except Exception:  # noqa: BLE001 — duck-typed engines in tests
        pass
    return "data/costs.db"


def _fmt_params(n: int) -> str:
    """Format parameter count: 27320697856 → '27.3B'"""
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    return str(n)


def _auto_learn_model_contexts(config, provider: str, api_base: str) -> int:
    """Best-effort: record each discovered model's served context window.

    Calls the provider's ``discover_models`` (llama.cpp returns ``meta.n_ctx``,
    the SERVED context, e.g. 200192) and upserts ``context_window`` into
    ``config.model_limits`` so BOTH the context-aware router and ``/v1/models``
    advertise the model's true capacity instead of the 128000 default.

    Returns the number of models whose context was learned. Never raises.
    """
    try:
        registry = resolve_service("pricing", fallback=get_registry)
        plugin = registry.for_provider(provider)
        if plugin is None or not hasattr(plugin, "discover_models"):
            return 0
        models = plugin.discover_models(api_base)
        if not models:
            return 0
        learned = 0
        limits = config.raw.setdefault("model_limits", {})
        for m in models:
            if not isinstance(m, dict):
                continue
            mid = (m.get("id") or "").strip()
            ctx = m.get("context_length") or m.get("n_ctx")
            if not mid or not ctx:
                continue
            try:
                ctx_int = int(ctx)
            except (TypeError, ValueError):
                continue
            entry = limits.setdefault(mid, {})
            entry["context_window"] = ctx_int
            learned += 1
        if learned:
            config.save()
        return learned
    except Exception:  # noqa: BLE001 — discovery must never break provider CRUD
        return 0


def _sync_dynamic_routing_enabled(settings, enabled: bool) -> None:
    """Update the DB-backed ``dynamic_routing`` config section's ``enabled``.

    Keeps ``config.dynamic_routing`` (which reads the settings DB first) in
    sync with the global routing toggle, so the section reflects the current
    runtime state. Best-effort: never breaks the request on failure.
    """
    try:
        section = settings.get_config_section("dynamic_routing", None)
        if not isinstance(section, dict):
            section = {}
        section["enabled"] = bool(enabled)
        settings.set_config_section("dynamic_routing", section)
    except Exception:  # noqa: BLE001 — best-effort sync
        logger.warning("dynamic_routing_config_sync_failed", error=True)


def _savings_for_model(config, model: str, hit_tokens: int) -> float:
    """Estimate dollars saved via provider prefix caching for a model."""
    if hit_tokens <= 0:
        return 0.0
    for p_name, p_cfg in config.providers.items():
        if model in p_cfg.get("models", []):
            try:
                pricing = config.get_pricing(p_name, model)
                return (hit_tokens / 1_000_000) * (
                    pricing["cache_miss"] - pricing["cache_hit"]
                )
            except Exception:
                pass
    return 0.0


# ── Shared outbound request helpers ─────────────────────────────────────────
def _browser_headers(api_base: str, extra: dict | None = None) -> dict:
    """Build request headers that look like a real browser session.

    Cloudflare flags Python's default ``Python-urllib/x.y`` User-Agent as a bot
    and answers with HTTP 403 / "error code: 1010" (verified against
    api.commandcode.ai). Sending a Chrome User-Agent plus Origin/Referer derived
    from the target host avoids the block — same urllib transport, no TLS change.
    """
    from urllib.parse import urlparse
    parsed = urlparse(api_base)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/143.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }
    if origin:
        headers["Origin"] = origin
        headers["Referer"] = origin + "/"
    if extra:
        headers.update(extra)
    return headers


def _friendly_provider_error(provider: str, status: int, body: str) -> tuple[str, str, str]:
    """Map a provider's raw error response to a human-friendly message.

    Command Code returns an OpenAI-style envelope::

        {"error": {"message": ..., "type": ..., "code": ..., "param": ...}}

    Common Command Code cases:
      - ``unsupported_model`` + "Anthropic" -> must go via /provider/v1/messages
      - ``MODEL_NOT_IN_PLAN`` / ``permission_error`` -> plan-gated model
      - ``authentication_error`` (401)        -> bad key
      - ``upgrade_required`` (403)            -> needs Provider plan or higher
      - ``rate_limit_error`` (429)            -> rate limited

    Returns ``(friendly_message, error_code, error_type)``. Falls back to the
    raw body when the response isn't a recognized envelope.
    """
    code = ""
    etype = ""
    message = body[:300]
    try:
        data = json.loads(body)
        err = data.get("error") or {}
        if isinstance(err, dict):
            message = err.get("message") or message
            code = str(err.get("code") or "")
            etype = str(err.get("type") or "")
    except Exception:
        pass

    if not code and status == 401:
        code, etype = "authentication_error", "authentication_error"
    if not code and status == 403:
        code, etype = "upgrade_required", "permission_error"

    low = message.lower()
    if (code == "unsupported_model"
            or "must be called via /provider/v1/messages" in low
            or ("anthropic" in low and "messages" in low)):
        return (
            f"{message} — this model uses the Anthropic /provider/v1/messages "
            f"shape, not /chat/completions.",
            code or "unsupported_model", etype or "invalid_request_error",
        )
    if code in ("MODEL_NOT_IN_PLAN", "permission_error") \
            or "model_not_in_plan" in low or "not in plan" in low:
        return (
            f"Model not available on your current plan: {message}. "
            f"Pick another model, upgrade your plan, or use extra on-demand usage.",
            code or "MODEL_NOT_IN_PLAN", etype or "permission_error",
        )
    if code == "authentication_error" or "authentication_error" in low \
            or "invalid api key" in low:
        return ("Authentication failed — check your API key.",
                "authentication_error", etype or "authentication_error")
    if code == "upgrade_required" or "upgrade_required" in low \
            or "provider plan" in low:
        return ("API access requires the Command Code Provider plan or higher.",
                "upgrade_required", etype or "permission_error")
    if code == "rate_limit_error" or "rate_limit_error" in low or status == 429:
        return ("Rate limited by the provider — retry in a moment.",
                "rate_limit_error", etype or "rate_limit_error")
    return (message, code, etype)


def _parse_multipart_upload(body: bytes, content_type: str) -> tuple[bytes, str, str]:
    """Manually parse a ``multipart/form-data`` upload body for the ``file`` field.

    Returns ``(file_bytes, release_value, filename)``. Handles quoted
    boundaries and the standard ``name="file"`` / ``name="release"`` fields.
    Raises ``ValueError`` on malformed input or a missing file field.
    """
    import re

    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise ValueError("multipart boundary not found in Content-Type")
    boundary = m.group(1).encode("utf-8")

    # Split into parts by the boundary.
    parts = body.split(b"--" + boundary)
    file_bytes: bytes = b""
    release: str = ""
    filename: str = ""
    found_file = False

    for part in parts:
        if not part or part in (b"\r\n", b"\n"):
            continue
        # A part starts with headers then a blank line then the body.
        try:
            header_blob, payload = part.split(b"\r\n\r\n", 1)
        except ValueError:
            continue
        headers_text = header_blob.decode("utf-8", errors="replace")
        payload = payload.rstrip(b"\r\n")

        name_match = re.search(r'name="([^"]+)"', headers_text)
        if not name_match:
            continue
        field_name = name_match.group(1)

        if field_name == "file":
            found_file = True
            file_bytes = payload
            file_match = re.search(r'filename="([^"]+)"', headers_text)
            if file_match:
                filename = file_match.group(1)
        elif field_name == "release":
            release = payload.decode("utf-8", errors="replace").strip()

    if not found_file or not file_bytes:
        raise ValueError("missing 'file' field in upload")
    return file_bytes, release, filename


# ── Health / Monitoring Endpoints ────────────────────────────────────────────

class HealthEndpoints:
    """Health, models, errors, cache stats, metrics, export."""

    config: Any
    engine: Any
    _send_json: Any
    send_response: Any
    send_header: Any
    end_headers: Any
    wfile: Any

    def _serve_health(self):
        provider_status = {}
        cb = resolve_service("circuit_breaker", fallback=get_circuit_breaker)
        for key, h in cb.get_all_health().items():
            provider, url, profile = key
            tripped_until = None
            if h.get("tripped_until"):
                tripped_until = datetime.fromtimestamp(h["tripped_until"], tz=timezone.utc).isoformat()
            provider_status[f"{provider}/{profile}"] = {
                "status": h["status"],
                "failures": h["consecutive_failures"],
                "last_success": h["last_success"],
                "last_failure": h["last_failure"],
                "tripped_until": tripped_until,
                # base_url / last_failure_reason are intentionally REDACTED:
                # /health is served unauthenticated (uptime checks, link
                # checkers). The private provider endpoint map was disclosed
                # through this route (CWE-200, Strix 09-10); status/failure
                # counters are all the health consumers need.
            }
        self._send_json({
            "status": "ok",
            "profiles": list(self.config.profiles.keys()),
            "providers": provider_status,
        })

    def _serve_circuit_breaker_reset(self):
        """POST /api/circuit-breaker/reset — force-reset a provider to healthy."""
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        provider = body.get("provider")
        base_url = body.get("base_url")
        profile = body.get("profile")
        if not provider or not base_url or not profile:
            self._send_json({"error": "missing provider, base_url, or profile"}, 400)
            return
        cb = resolve_service("circuit_breaker", fallback=get_circuit_breaker)
        cb.reset(provider, base_url, profile)
        self._send_json({"ok": True, "provider": provider, "profile": profile})

    # ── Provider health / failover / failure endpoints ────────────────────

    def _uptime_for(self, provider: str, profile: str, window_hours: int) -> float:
        """Return uptime % for a provider+profile over a rolling window.

        Computed from the persisted ``requests`` table: successes / total.
        Returns 100.0 when there are no requests in the window.
        """
        from sqlalchemy import func
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        try:
            with get_session(self.engine) as session:
                total = session.query(func.count(RequestModel.id)).filter(
                    RequestModel.provider == provider,
                    RequestModel.profile == profile,
                    RequestModel.timestamp >= cutoff,
                ).scalar() or 0
                ok = session.query(func.count(RequestModel.id)).filter(
                    RequestModel.provider == provider,
                    RequestModel.profile == profile,
                    RequestModel.timestamp >= cutoff,
                    RequestModel.success == 1,
                ).scalar() or 0
        except Exception:
            return 100.0
        if total == 0:
            return 100.0
        return round(ok / total * 100, 2)

    def _failure_breakdown(self, provider: str, profile: str,
                           window_hours: int) -> dict:
        """Return failure counts grouped by error_type over a rolling window."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        from sqlalchemy import func
        try:
            with get_session(self.engine) as session:
                rows = session.query(
                    RequestModel.error_type,
                    func.count(RequestModel.id),
                ).filter(
                    RequestModel.provider == provider,
                    RequestModel.profile == profile,
                    RequestModel.timestamp >= cutoff,
                    RequestModel.success == 0,
                ).group_by(RequestModel.error_type).all()
        except Exception:
            return {}
        return {r[0] or "unknown": int(r[1]) for r in rows}

    def _serve_providers_health_api(self):
        """GET /api/providers/health — all provider health with uptime + failures."""
        cb = resolve_service("circuit_breaker", fallback=get_circuit_breaker)
        summary = {"total": 0, "healthy": 0, "degraded": 0, "dead": 0}
        providers_out: dict[str, dict] = {}

        for key, h in cb.get_all_health().items():
            provider, url, profile = key
            status = h["status"]
            summary["total"] += 1
            if status == "healthy":
                summary["healthy"] += 1
            elif status == "degraded":
                summary["degraded"] += 1
            elif status == "dead":
                summary["dead"] += 1
            tripped_until = None
            if h.get("tripped_until"):
                tripped_until = datetime.fromtimestamp(
                    h["tripped_until"], tz=timezone.utc
                ).isoformat()
            providers_out[f"{provider}/{profile}"] = {
                "provider": provider,
                "profile": profile,
                "base_url": url,
                "status": status,
                "manual_override": h.get("manual_override"),
                "failures": h["consecutive_failures"],
                "last_success": h["last_success"],
                "last_failure": h["last_failure"],
                "last_failure_reason": h.get("last_failure_reason"),
                "tripped_until": tripped_until,
                "uptime_24h": self._uptime_for(provider, profile, 24),
                "uptime_7d": self._uptime_for(provider, profile, 24 * 7),
                "uptime_30d": self._uptime_for(provider, profile, 24 * 30),
                "failures_24h": self._failure_breakdown(provider, profile, 24),
            }

        self._send_json({"summary": summary, "providers": providers_out})

    def _serve_provider_failures_api(self, name: str):
        """GET /api/providers/{name}/failures?window=24h|7d|30d — breakdown by error_type."""
        qs = parse_qs(urlparse(self.path).query)
        window = qs.get("window", ["24h"])[0]
        window_hours = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}.get(window, 24)
        profile = qs.get("profile", [None])[0]

        from sqlalchemy import func
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        try:
            with get_session(self.engine) as session:
                q = session.query(
                    RequestModel.error_type,
                    func.count(RequestModel.id),
                ).filter(
                    RequestModel.provider == name,
                    RequestModel.timestamp >= cutoff,
                    RequestModel.success == 0,
                )
                if profile:
                    q = q.filter(RequestModel.profile == profile)
                rows = q.group_by(RequestModel.error_type).all()
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)
            return

        breakdown = {r[0] or "unknown": int(r[1]) for r in rows}
        total = sum(breakdown.values())
        # Normalized buckets for the chart (matches spec: timeout/5xx/rate_limit/auth).
        # Accept both exception-class names ("ProviderTimeoutError") and short
        # legacy names ("timeout") so seeded/older data also groups correctly.
        def _bucket(et: str) -> str:
            e = (et or "").lower()
            if "timeout" in e:
                return "timeout"
            if "credit" in e or "balance" in e or "insufficient" in e or "funds" in e:
                return "credits"
            if "rate" in e or "429" in e:
                return "rate_limit"
            if "auth" in e or "401" in e or "403" in e:
                return "auth"
            if "bad" in e or "400" in e or "invalid" in e:
                return "bad_request"
            if "internal" in e or "5" in e or "server" in e:
                return "internal_error"
            return "other"

        buckets = {"timeout": 0, "internal_error": 0, "rate_limit": 0,
                   "auth": 0, "bad_request": 0, "credits": 0, "other": 0}
        for et, n in breakdown.items():
            buckets[_bucket(et)] += n
        self._send_json({
            "provider": name,
            "window": window,
            "breakdown": breakdown,
            "buckets": buckets,
            "total": total,
        })

    def _serve_providers_failovers_api(self):
        """GET /api/providers/failovers?profile=&from=&to=&limit= — failover event log."""
        qs = parse_qs(urlparse(self.path).query)
        profile = qs.get("profile", [None])[0]
        from_provider = qs.get("from", [None])[0]
        to_provider = qs.get("to", [None])[0]
        try:
            limit = int(qs.get("limit", ["20"])[0])
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 200))

        try:
            with get_session(self.engine) as session:
                q = session.query(FailoverEvent)
                if profile:
                    q = q.filter(FailoverEvent.profile == profile)
                if from_provider:
                    q = q.filter(FailoverEvent.from_provider == from_provider)
                if to_provider:
                    q = q.filter(FailoverEvent.to_provider == to_provider)
                rows = q.order_by(FailoverEvent.id.desc()).limit(limit).all()
                failovers = [
                    {
                        "id": r.id,
                        "timestamp": r.timestamp,
                        "profile": r.profile,
                        "from_provider": r.from_provider,
                        "to_provider": r.to_provider,
                        "reason": r.reason,
                        "error_message": r.error_message,
                    }
                    for r in rows
                ]
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)
            return
        self._send_json({"failovers": failovers})

    def _serve_provider_toggle(self, name: str):
        """POST /api/providers/{name}/toggle — body {profile, action: degrade|resume|kill}."""
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        profile = body.get("profile")
        action = body.get("action")
        if not profile or not action:
            self._send_json({"error": "missing 'profile' or 'action' field"}, 400)
            return
        if action not in ("degrade", "resume", "kill"):
            self._send_json({"error": "action must be 'degrade', 'resume', or 'kill'"}, 400)
            return

        # Resolve the provider's base_url for this profile
        base_url = None
        pcfg = self.config.profiles.get(profile, {})
        for step in pcfg.get("chain", []):
            if step.get("provider") == name:
                base_url = step.get("base_url") or \
                    self.config.providers.get(name, {}).get("api_base", "")
                break

        cb = resolve_service("circuit_breaker", fallback=get_circuit_breaker)
        if base_url:
            status = cb.force_status(name, base_url, profile, action)
            self._send_json({"ok": True, "provider": name, "profile": profile,
                             "action": action, "status": status})
        else:
            self._send_json({"error": f"provider '{name}' not found in profile '{profile}'"}, 404)

    def _serve_models(self, profile: str | None = None):
        # Collect all providers per model: {model_id: [provider_names]}
        model_providers = {}
        model_provider_seen = {}  # {model_id: set(provider_names)} for dedup
        model_owned_by = {}

        model_limits = self.config.model_limits

        profiles_iter = (
            [(profile, self.config.profiles[profile])]
            if profile
            else self.config.profiles.items()
        )

        for _prof_name, prof_cfg in profiles_iter:
            for step in prof_cfg["chain"]:
                mid = step["model"]
                provider = step["provider"]

                if mid not in model_provider_seen:
                    model_provider_seen[mid] = set()
                    model_providers[mid] = []

                if provider not in model_provider_seen[mid]:
                    model_provider_seen[mid].add(provider)
                    model_providers[mid].append(provider)

                # First-seen provider becomes owned_by
                if mid not in model_owned_by:
                    model_owned_by[mid] = provider

        models = []
        # ── Emit profile entries as virtual models ──
        for prof_name, prof_cfg in profiles_iter:
            chain = prof_cfg.get("chain", [])
            if not chain:
                continue

            profile_providers = []
            max_context = 0
            profile_description = ""
            profile_supports_vision = False

            for step in chain:
                mid = step["model"]
                provider = step["provider"]
                limits = model_limits.get(mid, {})
                ctx = limits.get("context_window", 128000)
                profile_providers.append({
                    "provider": provider,
                    "model": mid,
                    "context_length": ctx,
                    "supports_tools": True,
                })
                if ctx > max_context:
                    max_context = ctx
                if limits.get("supports_vision", False):
                    profile_supports_vision = True
                # Use first model's description as fallback
                if not profile_description and limits.get("description"):
                    profile_description = limits["description"]

            models.append({
                "id": prof_name,
                "object": "model",
                "owned_by": "lcp",
                "kind": "profile",
                "context_window": max_context,
                "max_model_len": max_context,
                "context_length": max_context,
                "supports_vision": profile_supports_vision,
                "description": profile_description or f"Profile with {len(chain)} provider(s) in chain",
                "providers": profile_providers,
            })

        self._send_json({"object": "list", "data": models})

    def _serve_errors(self):
        """Show recent errors from the database."""
        try:
            with get_session(self.engine) as session:
                recents = (
                    session.query(RequestModel)
                    .filter(RequestModel.success == 0)
                    .order_by(RequestModel.id.desc())
                    .limit(50)
                    .all()
                )
                errors_list = [
                    {
                        "timestamp": r.timestamp,
                        "profile": r.profile,
                        "error_type": r.error_type,
                    }
                    for r in recents
                ]
            self._send_json({"errors": errors_list})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_cache_stats(self):
        """Prompt cache hit/miss statistics."""
        cache = resolve_service("prompt_cache", fallback=get_prompt_cache)
        self._send_json(cache.stats)

    def _serve_metrics(self):
        """Prometheus-compatible metrics endpoint."""
        from sqlalchemy import func

        try:
            with get_session(self.engine) as session:
                total_req = session.query(func.count(RequestModel.id)).scalar() or 0
                total_cost = session.query(func.sum(RequestModel.cost)).scalar() or 0
                failed = (
                    session.query(func.count(RequestModel.id))
                    .filter(RequestModel.success == 0)
                    .scalar() or 0
                )
        except Exception:
            total_req = 0
            total_cost = 0
            failed = 0

        cache = resolve_service("prompt_cache", fallback=get_prompt_cache)
        verifier = resolve_service("token_verifier", fallback=get_token_verifier)

        lines = [
            "# HELP lcp_requests_total Total requests processed",
            "# TYPE lcp_requests_total counter",
            f"lcp_requests_total {total_req}",
            "# HELP lcp_cost_total Total cost in USD",
            "# TYPE lcp_cost_total gauge",
            f"lcp_cost_total {total_cost}",
            "# HELP lcp_errors_total Total failed requests",
            "# TYPE lcp_errors_total counter",
            f"lcp_errors_total {failed}",
            "# HELP lcp_cache_hits_total Cache hits",
            "# TYPE lcp_cache_hits_total counter",
            f"lcp_cache_hits_total {cache.stats['hits']}",
            "# HELP lcp_cache_misses_total Cache misses",
            "# TYPE lcp_cache_misses_total counter",
            f"lcp_cache_misses_total {cache.stats['misses']}",
            "# HELP lcp_cache_entries Current cache entries",
            "# TYPE lcp_cache_entries gauge",
            f"lcp_cache_entries {cache.stats['entries']}",
            "# HELP lcp_token_warnings_total Token discrepancy warnings",
            "# TYPE lcp_token_warnings_total counter",
            f"lcp_token_warnings_total {verifier.stats['warnings']}",
        ]

        body = "\n".join(lines) + "\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _serve_export(self):
        """CSV export of cost data."""
        try:
            with get_session(self.engine) as session:
                q = session.query(RequestModel).order_by(RequestModel.id.desc())
                rows = q.limit(min(1000, 10000)).all()

            csv_lines = [
                "timestamp,profile,model,provider,prompt_tokens,completion_tokens,"
                "cache_hit_tokens,cache_miss_tokens,cost,latency_ms,success,error_type"
            ]
            for r in rows:
                csv_lines.append(
                    f"{r.timestamp},{r.profile},{r.model},{r.provider},"
                    f"{r.prompt_tokens},{r.completion_tokens},{r.cache_hit_tokens},"
                    f"{r.cache_miss_tokens},{r.cost},{r.latency_ms},{r.success},"
                    f"{r.error_type or ''}"
                )

            body = "\n".join(csv_lines)
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", "attachment; filename=lcp-export.csv")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        except Exception as e:
            self._send_json({"error": str(e)}, 500)


# ── Provider Configuration Endpoints ─────────────────────────────────────────

class ProviderEndpoints:
    """Provider CRUD, testing, chain reorder."""

    config: Any
    _send_json: Any
    _read_body: Any

    def _serve_providers_list(self):
        cfg = self.config
        providers = {}
        store = resolve_service("credential_store", fallback=get_credential_store)
        for name, pdata in cfg.providers.items():
            providers[name] = {
                "api_base": pdata.get("api_base", ""),
                "models": pdata.get("models", []),
                "has_api_key": bool(store and store.has(name)),
            }
        profile_chains = {}
        for pname, pcfg in cfg.profiles.items():
            profile_chains[pname] = pcfg.get("chain", [])
        self._send_json({"providers": providers, "profile_chains": profile_chains})

    def _serve_provider_presets(self):
        presets = resolve_service("pricing", fallback=get_registry).presets
        self._send_json({"presets": presets})

    def _serve_provider_create(self):
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        name = body.get("name")
        if not name:
            self._send_json({"error": "missing 'name' field"}, 400)
            return
        cfg = self.config
        provider_data = {
            "api_base": body.get("api_base", ""),
            "models": body.get("models", []),
        }
        cfg.raw.setdefault("providers", {})[name] = provider_data
        cfg.save()
        # Best-effort: learn each model's real served context window so the
        # context-aware router + /v1/models know the true capacity.
        _auto_learn_model_contexts(cfg, name, provider_data.get("api_base", ""))
        # Store the API key (if provided) encrypted in the credential store —
        # never in the git-tracked config.
        if body.get("api_key"):
            store = _credential_store_for(self)
            if store is not None:
                store.set(name, body["api_key"])
            else:
                self._send_json({"error": "credential store not initialized"}, 500)
                return
        # A newly added/updated provider should get its cache entry (if its
        # cost plugin exposes usage/balance/credits) populated on the next
        # background pass rather than waiting for the TTL.
        from ..api.cost_cache import get_refresher
        refresher = resolve_service("refresher", fallback=get_refresher)
        if refresher is not None:
            refresher.request_refresh(provider=name)
        self._send_json({"ok": True, "provider": name})

    def _serve_provider_update(self, name: str):
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        cfg = self.config
        if name not in cfg.providers:
            self._send_json({"error": f"provider '{name}' not found"}, 404)
            return
        pdata = cfg.raw["providers"][name]
        if "api_key" in body:
            store = _credential_store_for(self)
            if store is not None:
                store.set(name, body.get("api_key") or "")
            else:
                self._send_json({"error": "credential store not initialized"}, 500)
                return
        if "api_base" in body:
            pdata["api_base"] = body["api_base"]
        if "models" in body:
            pdata["models"] = body["models"]
        cfg.save()
        # Best-effort: refresh the models' served context windows.
        _auto_learn_model_contexts(cfg, name, pdata.get("api_base", ""))
        # See create: request a background cache refresh for this provider.
        from ..api.cost_cache import get_refresher
        refresher = resolve_service("refresher", fallback=get_refresher)
        if refresher is not None:
            refresher.request_refresh(provider=name)
        self._send_json({"ok": True, "provider": name})

    def _serve_provider_delete(self, name: str):
        cfg = self.config
        if name not in cfg.providers:
            self._send_json({"error": f"provider '{name}' not found"}, 404)
            return
        del cfg.raw["providers"][name]
        for pname, pcfg in cfg.raw["profiles"].items():
            pcfg["chain"] = [c for c in pcfg.get("chain", []) if c.get("provider") != name]
        cfg.save()
        # Clear any stored credential for the deleted provider
        store = _credential_store_for(self)
        if store is not None:
            store.set(name, "")
        # Cascade: a provider's persisted circuit-breaker health is otherwise
        # immortal — attach_engine() materializes EVERY provider_health row at
        # boot, so /health and /api/providers/health kept listing the deleted
        # provider across restarts. Drop its rows here so the delete is complete.
        health_rows_removed = 0
        try:
            cb = resolve_service("circuit_breaker", fallback=get_circuit_breaker)
            health_rows_removed = cb.forget_provider(name)
        except Exception as exc:  # noqa: BLE001 — never fail a delete on health bookkeeping
            logger.warning("provider_delete_health_cascade_failed",
                           provider=name, error=str(exc))
        self._send_json({"ok": True, "deleted": name,
                         "health_rows_removed": health_rows_removed})

    def _serve_provider_rename(self, name: str):
        """POST /api/providers/{name}/rename  {new_name: "..."} — rename a provider.

        Renaming is NOT create-then-delete: the provider's config entry, every
        profile chain step that references it, and its stored credential are
        moved to the new name atomically. Rejects missing/empty/duplicate names
        and no-op renames.
        """
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        new_name = (body.get("new_name") or "").strip()
        if not new_name:
            self._send_json({"error": "missing 'new_name' field"}, 400)
            return
        cfg = self.config
        if name not in cfg.providers:
            self._send_json({"error": f"provider '{name}' not found"}, 404)
            return
        if new_name == name:
            self._send_json({"ok": True, "provider": name, "renamed": False})
            return
        if new_name in cfg.providers:
            self._send_json({"error": f"provider '{new_name}' already exists"}, 409)
            return

        # Move the config entry.
        cfg.raw["providers"][new_name] = cfg.raw["providers"].pop(name)
        # Re-point every chain step that referenced the old provider.
        steps_updated = 0
        for pname, pcfg in cfg.raw.get("profiles", {}).items():
            for step in pcfg.get("chain", []):
                if step.get("provider") == name:
                    step["provider"] = new_name
                    steps_updated += 1
        cfg.save()

        # Move stored credentials (api key / cookie / workspace id).
        store = _credential_store_for(self)
        if store is not None:
            try:
                if store.has(name):
                    store.set(new_name, store.get(name) or "")
                    store.set(name, "")
                if store.has_cookie(name):
                    store.set_cookie(new_name, store.get_cookie(name) or "")
                    store.set_cookie(name, "")
                if store.has_workspace_id(name):
                    store.set_workspace_id(new_name, store.get_workspace_id(name) or "")
                    store.set_workspace_id(name, "")
            except Exception as exc:  # noqa: BLE001 — rename must not fail wholesale
                logger.warning("provider_rename_credential_move_failed",
                               provider=name, new_name=new_name, error=str(exc))

        # Fresh cache entry for the new name on the next background pass.
        from ..api.cost_cache import get_refresher
        refresher = resolve_service("refresher", fallback=get_refresher)
        if refresher is not None:
            refresher.request_refresh(provider=new_name)

        logger.info("provider_renamed", provider=name, new_name=new_name,
                    chain_steps_updated=steps_updated)
        self._send_json({"ok": True, "provider": new_name, "renamed": True,
                         "chain_steps_updated": steps_updated})

    def _serve_provider_test(self):
        import urllib.request
        import urllib.error
        import ssl
        import time

        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        api_base = body.get("api_base", "").rstrip("/")
        api_key = body.get("api_key", "")
        model = body.get("model", "")
        provider = body.get("provider", "")
        # Resolve API key from credential store if not provided inline
        key_source = "inline"
        if not api_key and provider:
            store = _credential_store_for(self)
            if store is not None:
                api_key = store.get(provider) or ""
                if api_key:
                    key_source = "credential_store"
        # API key is optional — providers like llama.cpp (local) need none.
        if not api_base:
            self._send_json({"error": "missing 'api_base'"}, 400)
            return

        ssrf_reason = _validate_api_base_destination(api_base)
        if ssrf_reason:
            logger.warning("provider_test_blocked_ssrf", api_base=api_base, reason=ssrf_reason)
            self._send_json({"error": f"api_base not allowed: {ssrf_reason}"}, 400)
            return

        url = f"{api_base}/chat/completions"
        test_model = model or "gpt-3.5-turbo"
        test_body = json.dumps({
            "model": test_model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
        }).encode()

        logger.info(
            "provider_test_start",
            provider=provider or "",
            api_base=api_base,
            model=test_model,
            key_source=key_source,
        )

        start_ts = time.monotonic()
        headers = _browser_headers(api_base)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(url, data=test_body, headers=headers)
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                result = json.loads(resp.read().decode())
                model_used = result.get("model", "unknown")
                duration_ms = round((time.monotonic() - start_ts) * 1000)
                logger.info(
                    "provider_test_ok",
                    provider=provider or "",
                    api_base=api_base,
                    model_returned=model_used,
                    status=resp.status,
                    duration_ms=duration_ms,
                )
                self._send_json({"ok": True, "model": model_used, "status": resp.status})
        except urllib.error.HTTPError as e:
            duration_ms = round((time.monotonic() - start_ts) * 1000)
            err_body = ""
            if e.fp:
                try:
                    err_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""

            # Detect Cloudflare-specific signatures. Real-world bodies vary:
            #   - "error code: 1010\n"           (minimal — what commandcode returns)
            #   - full HTML challenge page with "Ray ID: ..." and "cloudflare"
            #   - JSON-ish or plain "error 1010"
            body_lower = err_body.lower()
            is_cloudflare = (
                "cloudflare" in body_lower
                or "error 1010" in body_lower
                or "error code: 1010" in body_lower
                or "error code 1010" in body_lower
                or "error code=1010" in body_lower
                or "ray id:" in body_lower
            )
            ray_id = ""
            if "cf-ray" in body_lower:
                import re
                m = re.search(r"cf-ray[:\s]+([a-z0-9-]+)", body_lower)
                if m:
                    ray_id = m.group(1)
            if not ray_id and "ray id:" in body_lower:
                import re
                m = re.search(r"ray\s*id[:\s]+([a-z0-9-]+)", body_lower)
                if m:
                    ray_id = m.group(1)
            # Also check response headers for cf-ray
            cf_ray_header = ""
            if hasattr(e, "headers"):
                hdrs = dict(e.headers) if isinstance(e.headers, dict) else {}
                cf_ray_header = hdrs.get("cf-ray") or hdrs.get("Cf-Ray") or ""
                if cf_ray_header and not ray_id:
                    ray_id = cf_ray_header
                if cf_ray_header:
                    is_cloudflare = True

            logger.warning(
                "provider_test_http_error",
                provider=provider or "",
                api_base=api_base,
                status=e.code,
                duration_ms=duration_ms,
                cloudflare_block=is_cloudflare,
                cf_ray=ray_id,
                error_preview=err_body[:500],
            )

            response = {"ok": False, "status": e.code}
            if is_cloudflare:
                response["error"] = (
                    "Cloudflare block (error 1010) — the request was rejected at the "
                    "TLS layer because it does not appear to come from a real browser."
                )
                if ray_id:
                    response["cf_ray"] = ray_id
            else:
                msg, code, etype = _friendly_provider_error(provider, e.code, err_body)
                response["error"] = msg
                if code:
                    response["code"] = code
                if etype:
                    response["type"] = etype
            self._send_json(response)
        except Exception as e:
            duration_ms = round((time.monotonic() - start_ts) * 1000)
            logger.warning(
                "provider_test_error",
                provider=provider or "",
                api_base=api_base,
                error_type=type(e).__name__,
                error_message=str(e)[:500],
                duration_ms=duration_ms,
            )
            self._send_json({"ok": False, "error": str(e)})

    def _serve_provider_discover(self):
        """Proxy a /models call via the provider's plugin, or generic HTTP fallback."""
        import urllib.request, urllib.error, ssl

        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        api_base = body.get("api_base", "").rstrip("/")
        provider = body.get("provider", "")
        if not api_base:
            self._send_json({"error": "missing 'api_base'"}, 400)
            return

        ssrf_reason = _validate_api_base_destination(api_base)
        if ssrf_reason:
            logger.warning("provider_discover_blocked_ssrf", api_base=api_base, reason=ssrf_reason)
            self._send_json({"error": f"api_base not allowed: {ssrf_reason}"}, 400)
            return

        # Try plugin first for provider-specific parser
        registry = resolve_service("pricing", fallback=get_registry)
        if provider:
            plugin = registry.for_provider(provider)
            if plugin and hasattr(plugin, 'discover_models'):
                models = plugin.discover_models(api_base)
                if models is not None:
                    has_meta = any(len(m) > 1 for m in models)
                    self._send_json({"ok": True, "models": models, "has_metadata": has_meta, "count": len(models)})
                    return

        # Generic HTTP fallback
        api_key = body.get("api_key", "")
        headers = _browser_headers(api_base)
        if provider and not api_key:
            store = _credential_store_for(self)
            if store is not None:
                api_key = store.get(provider) or ""
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # UI-managed cookie from the credential store
        store = _credential_store_for(self)
        cookie = ""
        if store is not None:
            cookie = store.get_cookie("opencode") or ""
        if cookie:
            headers["Cookie"] = cookie
            headers["Origin"] = "https://opencode.ai"
            headers["Referer"] = "https://opencode.ai/"
        ws = ""
        if store is not None:
            ws = store.get_workspace_id("opencode") or ""
        if ws:
            headers["X-Workspace-Id"] = ws

        urls_to_try = [f"{api_base}/models"]
        if "/v1" not in api_base.lower():
            urls_to_try.append(f"{api_base}/v1/models")
        result, last_error = None, None
        for url in urls_to_try:
            try:
                req = urllib.request.Request(url, headers=headers)
                ctx = ssl.create_default_context()
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    result = json.loads(resp.read().decode())
                    break
            except urllib.error.HTTPError as e:
                last_error = f"HTTP {e.code}"
            except Exception as e:
                last_error = str(e)
        if result is None:
            self._send_json({"ok": False, "error": last_error or "no models endpoint found"})
            return

        if isinstance(result, list):
            models_raw = result
        else:
            models_raw = result.get("data") or result.get("models")
            if models_raw is None:
                models_raw = []
            elif not isinstance(models_raw, list):
                models_raw = []

        models = []
        for m in models_raw:
            if not isinstance(m, dict):
                models.append({"id": str(m)})
                continue
            entry = {"id": m.get("id") or m.get("name") or str(m)}
            for field in ("created", "owned_by", "object"):
                if m.get(field) is not None:
                    entry[field] = m[field]
            for field in ("context_length", "max_model_len"):
                if m.get(field):
                    entry[field] = m[field]
            meta = m.get("meta", {})
            if isinstance(meta, dict):
                if meta.get("n_ctx"):
                    entry["context_length"] = meta["n_ctx"]
                if meta.get("n_ctx_train"):
                    entry["context_train"] = meta["n_ctx_train"]
                if meta.get("n_params"):
                    entry["parameters"] = _fmt_params(meta["n_params"])
                if meta.get("ftype"):
                    entry["quantization"] = meta["ftype"]
                if meta.get("size"):
                    entry["size_bytes"] = meta["size"]
            details = m.get("details", {})
            if isinstance(details, dict):
                if details.get("parameter_size"):
                    entry["parameters"] = details["parameter_size"]
                if details.get("quantization_level"):
                    entry["quantization"] = details["quantization_level"]
            if "pricing" in m and isinstance(m["pricing"], dict):
                entry["pricing"] = m["pricing"]
            models.append(entry)
        has_meta = any(len(m) > 1 for m in models)

        payload = {"ok": True, "models": models, "has_metadata": has_meta, "count": len(models)}
        # Enrich Command Code discover with the current plan from the billing API
        # (best-effort — Command Code does not expose per-plan model availability).
        if provider == "commandcode":
            try:
                plugin = resolve_service("pricing", fallback=get_registry).for_provider("commandcode")
                if plugin is not None:
                    sub = plugin.fetch_subscription() or {}
                    if sub.get("plan_id"):
                        payload["plan_id"] = sub["plan_id"]
                        payload["plan_status"] = sub.get("plan_status") or ""
                    if sub.get("_error"):
                        payload["plan_error"] = (
                            sub.get("detail") or sub.get("_error")
                        )
            except Exception:
                pass
        self._send_json(payload)

    def _serve_chain_reorder(self, profile: str):
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        cfg = self.config
        if profile not in cfg.profiles:
            self._send_json({"error": f"profile '{profile}' not found"}, 404)
            return
        new_chain = body.get("chain")
        if not isinstance(new_chain, list):
            self._send_json({"error": "missing 'chain' list"}, 400)
            return
        # Preserve base_url from existing chain entries
        old_chain = cfg.raw["profiles"][profile].get("chain", [])
        old_by_prov = {}
        for s in old_chain:
            old_by_prov[(s.get("provider"), s.get("model"))] = s.get("base_url", "")
        merged = []
        for s in new_chain:
            entry = {"provider": s.get("provider"), "model": s.get("model")}
            if s.get("base_url"):
                entry["base_url"] = s["base_url"]
            else:
                key = (entry["provider"], entry["model"])
                if key in old_by_prov and old_by_prov[key]:
                    entry["base_url"] = old_by_prov[key]
            merged.append(entry)
        cfg.raw["profiles"][profile]["chain"] = merged
        cfg.save()
        self._send_json({"ok": True, "profile": profile, "chain": merged})


# ── Profile Management Endpoints ─────────────────────────────────────────────

class ProfileEndpoints:
    """Profile CRUD."""

    config: Any
    headers: Any
    _send_json: Any
    _read_body: Any

    def _serve_profiles_list(self):
        """Return all profiles with their gateway URLs."""
        profiles = {}
        host = self.headers.get("Host", "localhost:8735")
        scheme = "https" if (
            self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"
            or self.headers.get("X-Forwarded-Scheme") == "https"
        ) else "http"
        base = f"{scheme}://{host}"
        from ..api import profile_data
        for pname, pcfg in self.config.profiles.items():
            profiles[pname] = {
                "chain": pcfg.get("chain", []),
                "url": f"{base}/{pname}/chat/completions",
                "forbidden": pcfg.get("forbidden_tools", []),
                "auth_required": pcfg.get("auth_required", True),
                # M2c: the card's description and its picture slot. The list is
                # what the Edit modal reads, so the field has to be here for the
                # modal to round-trip it.
                "description": pcfg.get("description", ""),
                "avatar": bool(profile_data.avatar_for(pname)),
            }
        self._send_json({"profiles": profiles})

    def _serve_profile_create(self):
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        name = body.get("name", "").strip().lower()
        if not name:
            self._send_json({"error": "missing 'name' field"}, 400)
            return
        cfg = self.config
        if name in cfg.profiles:
            self._send_json({"error": f"profile '{name}' already exists"}, 409)
            return
        cfg.raw["profiles"][name] = {
            "chain": [],
            "forbidden_tools": [],
        }
        cfg.save()
        self._send_json({"ok": True, "profile": name})

    def _serve_profile_update(self, name: str):
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        cfg = self.config
        if name not in cfg.profiles:
            self._send_json({"error": f"profile '{name}' not found"}, 404)
            return
        pcfg = cfg.raw["profiles"][name]
        if "forbidden_tools" in body:
            pcfg["forbidden_tools"] = body["forbidden_tools"]
        if "chain" in body:
            pcfg["chain"] = body["chain"]
        if "auth_required" in body:
            pcfg["auth_required"] = body["auth_required"]
        if "description" in body:
            # Stored as given but bounded: the card clamps to ten words, and the
            # field exists to be read at a glance. 120 chars is the modal's cap.
            desc = body["description"]
            if desc is None:
                desc = ""
            if not isinstance(desc, str):
                self._send_json({"error": "description must be a string"}, 400)
                return
            pcfg["description"] = desc.strip()[:120]
        cfg.save()
        self._send_json({"ok": True, "profile": name})

    def _serve_profile_delete(self, name: str):
        cfg = self.config
        if name not in cfg.profiles:
            self._send_json({"error": f"profile '{name}' not found"}, 404)
            return
        del cfg.raw["profiles"][name]
        cfg.save()
        self._send_json({"ok": True, "deleted": name})

    def _serve_profile_budget(self, name: str):
        """GET /api/profiles/{name}/budget — return the profile-level budget."""
        try:
            with get_session(self.engine) as session:
                budget = session.query(Budget).filter(
                    Budget.profile == name,
                    Budget.key_id.is_(None),
                ).first()
                if budget:
                    self._send_json({
                        "budget": {
                            "id": budget.id,
                            "name": budget.name,
                            "amount": budget.amount,
                            "current_spend": budget.current_spend,
                            "period": budget.period,
                            "threshold_pct": budget.threshold_pct,
                            "action": budget.action,
                            "status": budget.status,
                            "spend_pct": round((budget.current_spend / budget.amount * 100) if budget.amount > 0 else 0, 1),
                        }
                    })
                else:
                    self._send_json({"budget": None})
        except Exception as e:
            logger.error("profile_budget_get_failed", error=str(e))
            self._send_json({"error": str(e)}, 500)

    def _serve_profile_budget_update(self, name: str):
        """PUT /api/profiles/{name}/budget — create or update profile budget."""
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        try:
            with get_session(self.engine) as session:
                budget = session.query(Budget).filter(
                    Budget.profile == name,
                    Budget.key_id.is_(None),
                ).first()
                if budget:
                    # Update existing
                    if "amount" in body:
                        budget.amount = float(body["amount"])
                    if "period" in body:
                        budget.period = body["period"]
                    if "threshold_pct" in body:
                        budget.threshold_pct = body["threshold_pct"]
                    if "action" in body:
                        budget.action = body["action"]
                    if "status" in body:
                        budget.status = body["status"]
                    if "name" in body:
                        budget.name = body["name"]
                    session.commit()
                    self._send_json({"ok": True, "updated": budget.id})
                else:
                    # Create new
                    budget = Budget(
                        name=body.get("name", f"{name.upper()} Budget"),
                        key_id=None,
                        profile=name,
                        amount=float(body.get("amount", 0)),
                        period=body.get("period", "monthly"),
                        threshold_pct=body.get("threshold_pct", "80"),
                        action=body.get("action", "log"),
                    )
                    session.add(budget)
                    session.commit()
                    self._send_json({"ok": True, "created": budget.id})
        except Exception as e:
            logger.error("profile_budget_update_failed", error=str(e))
            self._send_json({"error": str(e)}, 500)


# ── API Key Management ───────────────────────────────────────────────────────

class KeyEndpoints:
    """API key CRUD."""

    _send_json: Any
    _read_body: Any

    def _serve_keys_list(self):
        km = resolve_service("key_manager", fallback=get_key_manager)
        keys = km.list_keys() if km else []
        self._send_json({"keys": keys})

    def _serve_key_detail(self, key_id: str):
        km = resolve_service("key_manager", fallback=get_key_manager)
        if not km:
            self._send_json({"error": "key manager not initialized"}, 500)
            return
        try:
            kid = int(key_id)
        except ValueError:
            self._send_json({"error": "invalid key id"}, 400)
            return
        key = km.get_key(kid)
        if key:
            self._send_json({"key": key})
        else:
            self._send_json({"error": "key not found"}, 404)

    def _serve_key_create(self):
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        km = resolve_service("key_manager", fallback=get_key_manager)
        if not km:
            self._send_json({"error": "key manager not initialized"}, 500)
            return
        result = km.create_key(
            name=body.get("name", ""),
            allowed_profiles=body.get("allowed_profiles", ""),
            spend_limit=float(body.get("spend_limit", 0) or 0),
            expires_at=body.get("expires_at", ""),
            metadata_tags=body.get("metadata_tags", ""),
        )
        self._send_json(result)

    def _serve_key_rotate(self, key_id: str):
        km = resolve_service("key_manager", fallback=get_key_manager)
        if not km:
            self._send_json({"error": "key manager not initialized"}, 500)
            return
        try:
            kid = int(key_id)
        except ValueError:
            self._send_json({"error": "invalid key id"}, 400)
            return
        result = km.rotate_key(kid)
        if result:
            self._send_json(result)
        else:
            self._send_json({"error": "key not found"}, 404)

    def _serve_key_delete(self, key_id: str):
        km = resolve_service("key_manager", fallback=get_key_manager)
        if not km:
            self._send_json({"error": "key manager not initialized"}, 500)
            return
        try:
            kid = int(key_id)
        except ValueError:
            self._send_json({"error": "invalid key id"}, 400)
            return
        ok = km.revoke_key(kid)
        if ok:
            self._send_json({"ok": True, "deleted": kid})
        else:
            self._send_json({"error": "key not found"}, 404)


# ── Alert Management ─────────────────────────────────────────────────────────

class AlertEndpoints:
    """Alert listing, config, webhook testing."""

    path: Any
    _send_json: Any
    _read_body: Any

    def _serve_alerts_list(self):
        am = resolve_service("alert_manager", fallback=get_alert_manager)
        limit = 100
        status = None
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "limit" in params:
            limit = int(params["limit"][0])
        if "status" in params:
            status = params["status"][0]
        alerts = am.list_alerts(limit=limit, status=status)
        self._send_json({"alerts": alerts})

    def _serve_alerts_active(self):
        am = resolve_service("alert_manager", fallback=get_alert_manager)
        self._send_json({"alerts": am.get_active_alerts()})

    def _serve_alerts_config(self):
        am = resolve_service("alert_manager", fallback=get_alert_manager)
        self._send_json({"config": am.config})

    def _serve_alerts_config_update(self):
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        am = resolve_service("alert_manager", fallback=get_alert_manager)
        config = am.update_config(body)
        self._send_json({"ok": True, "config": config})

    def _serve_alert_acknowledge(self, alert_id: str):
        am = resolve_service("alert_manager", fallback=get_alert_manager)
        ok = am.acknowledge(alert_id)
        if ok:
            self._send_json({"ok": True, "acknowledged": alert_id})
        else:
            self._send_json({"error": "alert not found"}, 404)

    def _serve_alerts_test_webhook(self):
        am = resolve_service("alert_manager", fallback=get_alert_manager)
        result = am.test_webhook()
        self._send_json(result)


# ── Budget Management ────────────────────────────────────────────────────────

class BudgetEndpoints:
    """Budget CRUD and status."""

    path: Any
    engine: Any
    _send_json: Any
    _read_body: Any

    def _serve_budgets_list(self):
        """GET /api/budgets — list all budgets."""
        try:
            with get_session(self.engine) as session:
                budgets = session.query(Budget).order_by(Budget.created_at.desc()).all()
                result = []
                for b in budgets:
                    key_name = None
                    if b.key_id:
                        key = session.query(ApiKey).filter(ApiKey.id == b.key_id).first()
                        key_name = key.name if key else None
                    result.append({
                        "id": b.id,
                        "name": b.name,
                        "key_id": b.key_id,
                        "key_name": key_name,
                        "profile": b.profile,
                        "amount": b.amount,
                        "current_spend": b.current_spend,
                        "period": b.period,
                        "threshold_pct": b.threshold_pct,
                        "action": b.action,
                        "status": b.status,
                        "created_at": b.created_at,
                        "last_alert_at": b.last_alert_at,
                    })
                self._send_json({"budgets": result})
        except Exception as e:
            logger.error("budgets_list_failed", error=str(e))
            self._send_json({"error": str(e)}, 500)

    def _serve_budget_create(self):
        """POST /api/budgets — create a budget."""
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        try:
            with get_session(self.engine) as session:
                budget = Budget(
                    name=body.get("name", ""),
                    key_id=body.get("key_id") or None,
                    profile=body.get("profile") or None,
                    amount=float(body.get("amount", 0)),
                    period=body.get("period", "monthly"),
                    threshold_pct=body.get("threshold_pct", "80"),
                    action=body.get("action", "log"),
                )
                session.add(budget)
                session.commit()
                self._send_json({"ok": True, "budget": {"id": budget.id, "name": budget.name}})
        except Exception as e:
            logger.error("budget_create_failed", error=str(e))
            self._send_json({"error": str(e)}, 500)

    def _serve_budget_update(self, budget_id: str):
        """PUT /api/budgets/{id} — update a budget."""
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        try:
            bid = int(budget_id)
        except ValueError:
            self._send_json({"error": "invalid budget id"}, 400)
            return
        try:
            with get_session(self.engine) as session:
                budget = session.query(Budget).filter(Budget.id == bid).first()
                if not budget:
                    self._send_json({"error": "budget not found"}, 404)
                    return
                if "name" in body:
                    budget.name = body["name"]
                if "key_id" in body:
                    budget.key_id = body["key_id"] or None
                if "profile" in body:
                    budget.profile = body["profile"] or None
                if "amount" in body:
                    budget.amount = float(body["amount"])
                if "period" in body:
                    budget.period = body["period"]
                if "threshold_pct" in body:
                    budget.threshold_pct = body["threshold_pct"]
                if "action" in body:
                    budget.action = body["action"]
                if "status" in body:
                    budget.status = body["status"]
                session.commit()
                self._send_json({"ok": True, "updated": budget_id})
        except Exception as e:
            logger.error("budget_update_failed", error=str(e))
            self._send_json({"error": str(e)}, 500)

    def _serve_budget_delete(self, budget_id: str):
        """DELETE /api/budgets/{id} — delete a budget."""
        try:
            bid = int(budget_id)
        except ValueError:
            self._send_json({"error": "invalid budget id"}, 400)
            return
        try:
            with get_session(self.engine) as session:
                budget = session.query(Budget).filter(Budget.id == bid).first()
                if not budget:
                    self._send_json({"error": "budget not found"}, 404)
                    return
                session.delete(budget)
                session.commit()
                self._send_json({"ok": True, "deleted": bid})
        except Exception as e:
            logger.error("budget_delete_failed", error=str(e))
            self._send_json({"error": str(e)}, 500)

    def _serve_budgets_status(self):
        """GET /api/budgets/status — current spend vs budget for all active budgets."""
        try:
            with get_session(self.engine) as session:
                budgets = session.query(Budget).filter(Budget.status == "active").all()
                result = []
                for b in budgets:
                    pct = (b.current_spend / b.amount * 100) if b.amount > 0 else 0
                    result.append({
                        "id": b.id,
                        "name": b.name,
                        "profile": b.profile,
                        "amount": b.amount,
                        "current_spend": b.current_spend,
                        "spend_pct": round(pct, 1),
                        "period": b.period,
                        "action": b.action,
                        "thresholds": [int(t) for t in b.threshold_pct.split(",") if t],
                    })
                self._send_json({"budgets": result})
        except Exception as e:
            logger.error("budgets_status_failed", error=str(e))
            self._send_json({"error": str(e)}, 500)


# ── Cost Plugin API ──────────────────────────────────────────────────────────

class PluginEndpoints:
    """Cost tracking plugin endpoints."""

    path: Any
    _send_json: Any

    def _serve_plugin_usage(self):
        """Return aggregated usage from all cost tracking plugins."""
        qs = parse_qs(urlparse(self.path).query)
        start = qs.get("start", [None])[0]
        end = qs.get("end", [None])[0]
        data = resolve_service("pricing", fallback=get_registry).fetch_all_usage(start_date=start, end_date=end)
        self._send_json({"plugin_usage": data})

    def _serve_plugin_balances(self):
        """Return account balances from all cost tracking plugins.

        Served from the DB cache (refreshed by the background scraper) when
        the cache is initialized; otherwise falls back to a live fetch.
        """
        from ..api.cost_cache import cached_plugin_payloads, get_cost_cache
        cache = resolve_service("cost_cache", fallback=get_cost_cache)
        if cache is None:
            data = resolve_service("pricing", fallback=get_registry).fetch_all_balances()
        else:
            data = cached_plugin_payloads(cache, "balance", resolve_service("pricing", fallback=get_registry))
        self._send_json({"plugin_balances": data})

    def _serve_plugin_summary(self):
        """Return rich provider summaries (usage limits, balance, etc.)."""
        data = resolve_service("pricing", fallback=get_registry).fetch_all_summaries()
        self._send_json({"plugin_summaries": data})

    def _serve_plugin_subscriptions(self):
        """Return subscription usage snapshots from all cost plugins.

        Served from the DB cache (refreshed by the background scraper) when
        the cache is initialized; otherwise falls back to a live fetch.
        """
        from ..api.cost_cache import cached_plugin_payloads, get_cost_cache
        cache = resolve_service("cost_cache", fallback=get_cost_cache)
        if cache is None:
            data = resolve_service("pricing", fallback=get_registry).fetch_all_subscriptions()
        else:
            data = cached_plugin_payloads(cache, "subscription", resolve_service("pricing", fallback=get_registry))
        self._send_json({"plugin_subscriptions": data})

    def _serve_plugin_cookie_get(self, provider: str):
        """Return whether a UI-managed cookie exists for a provider (never the value)."""
        store = _credential_store_for(self)
        has = bool(store and store.has_cookie(provider))
        self._send_json({"provider": provider, "has_cookie": has})

    def _serve_plugin_cookie_set(self, provider: str):
        """POST body {cookie: '...'} to upsert (or clear) a UI-managed cookie."""
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        cookie = (body.get("cookie") or "").strip()
        store = _credential_store_for(self)
        if store is None:
            self._send_json({"error": "credential store not initialized"}, 500)
            return
        store.set_cookie(provider, cookie)
        # New/cleared credentials invalidate the cached scrape for this provider
        # and re-scrape it in the background (never blocking this request).
        from ..api.cost_cache import get_cost_cache, get_refresher
        cache = resolve_service("cost_cache", fallback=get_cost_cache)
        if cache is not None:
            cache.invalidate(provider=provider)
        refresher = resolve_service("refresher", fallback=get_refresher)
        if refresher is not None:
            refresher.request_refresh(provider=provider)
        self._send_json({"ok": True, "provider": provider, "has_cookie": bool(cookie)})

    def _serve_plugin_workspace_id_get(self, provider: str):
        """Return whether a UI-managed workspace ID exists for a provider."""
        store = _credential_store_for(self)
        has = bool(store and store.has_workspace_id(provider))
        self._send_json({"provider": provider, "has_workspace_id": has})

    def _serve_plugin_workspace_id_set(self, provider: str):
        """POST body {workspace_id: 'wrk_...'} to upsert (or clear) a UI-managed workspace ID."""
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        ws_id = (body.get("workspace_id") or "").strip()
        store = _credential_store_for(self)
        if store is None:
            self._send_json({"error": "credential store not initialized"}, 500)
            return
        store.set_workspace_id(provider, ws_id)
        # Invalidate + background re-scrape (see cookie-set handler).
        from ..api.cost_cache import get_cost_cache, get_refresher
        cache = resolve_service("cost_cache", fallback=get_cost_cache)
        if cache is not None:
            cache.invalidate(provider=provider)
        refresher = resolve_service("refresher", fallback=get_refresher)
        if refresher is not None:
            refresher.request_refresh(provider=provider)
        self._send_json({"ok": True, "provider": provider, "has_workspace_id": bool(ws_id)})


# ── Admin Settings API ───────────────────────────────────────────────────────

class SettingsEndpoints:
    """Admin settings API: cost-cache TTL, routing policy, cache management.

    The UI for these lives in the Providers page's Cache / Routing tabs; these
    endpoints back them. There is no standalone /settings page anymore.
    """

    config: Any
    _send_json: Any
    _read_body: Any

    def _serve_settings_api(self):
        """GET /api/settings — default TTL, per-provider TTLs, cache entries."""
        from ..api.cost_cache import get_cost_cache, get_refresher, get_settings
        settings = resolve_service("settings", fallback=get_settings)
        ttl = settings.get_ttl_minutes() if settings is not None else 30
        cache = resolve_service("cost_cache", fallback=get_cost_cache)
        refresher = resolve_service("refresher", fallback=get_refresher)
        self._send_json({
            "ttl_minutes": ttl,
            "per_provider_ttl": settings.ttl_overrides() if settings is not None else {},
            "entries": cache.entries() if cache is not None else [],
            "refresh": refresher.diagnostics() if refresher is not None else {},
        })

    def _serve_settings_update_api(self):
        """POST /api/settings — persist the cache TTL.

        Body ``{ttl_minutes: N}`` sets the global default.
        Body ``{provider: p, ttl_minutes: N}`` sets a per-provider override.
        Body ``{provider: p}`` (no ttl_minutes) resets that provider to the
        default.
        """
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        provider = (body.get("provider") or "").strip().lower() or None
        from ..api.cost_cache import get_refresher, get_settings
        settings = resolve_service("settings", fallback=get_settings)
        if settings is None:
            self._send_json({"error": "settings store not initialized"}, 500)
            return
        refresher = resolve_service("refresher", fallback=get_refresher)

        if provider and "ttl_minutes" not in body:
            # Reset this provider to the global default.
            settings.clear_ttl_minutes(provider)
            if refresher is not None:
                refresher.request_refresh(provider=provider)
            self._send_json({
                "ok": True, "provider": provider,
                "ttl_minutes": settings.get_ttl_minutes(provider=provider),
                "per_provider_ttl": settings.ttl_overrides(),
            })
            return

        try:
            ttl = max(1, int(body.get("ttl_minutes")))
        except (TypeError, ValueError):
            self._send_json({"error": "ttl_minutes must be an integer >= 1"}, 400)
            return
        settings.set_ttl_minutes(ttl, provider=provider)
        # TTL changed → affected entries may now be stale; re-scrape in background.
        if refresher is not None:
            refresher.request_refresh(provider=provider)
        self._send_json({
            "ok": True, "provider": provider, "ttl_minutes": ttl,
            "per_provider_ttl": settings.ttl_overrides(),
        })

    def _serve_settings_refresh_api(self):
        """POST /api/settings/cache/refresh — enqueue a background re-scrape."""
        from ..api.cost_cache import get_refresher
        refresher = resolve_service("refresher", fallback=get_refresher)
        if refresher is not None:
            refresher.request_refresh()
            self._send_json({"ok": True, "refreshing": True})
        else:
            self._send_json({"error": "refresher not initialized"}, 500)

    def _serve_settings_cache_clear(self):
        """POST /api/settings/cache/clear — wipe the cache (refreshes after)."""
        from ..api.cost_cache import get_cost_cache, get_refresher
        cache = resolve_service("cost_cache", fallback=get_cost_cache)
        if cache is not None:
            cache.clear()
        refresher = resolve_service("refresher", fallback=get_refresher)
        if refresher is not None:
            refresher.request_refresh()
        self._send_json({"ok": True})

    def _serve_routing_status_api(self):
        """GET /api/routing/status — dynamic-routing snapshot for the UI.

        Accepts ``?profile=<name>`` to return just that profile's effective
        block; without it, returns the global snapshot + a ``per_profile`` map.
        """
        from urllib.parse import parse_qs, urlparse
        from ..api.router import routing_status, _status_for_profile, get_dynamic_router

        qs = parse_qs(urlparse(self.path).query)
        profile = (qs.get("profile") or [None])[0]
        if profile:
            router = resolve_service("dynamic_router", fallback=get_dynamic_router)
            block = _status_for_profile(router, self.config, profile)
            block["per_task"] = routing_status(self.config)["per_task"]
            block["recent_decisions"] = routing_status(self.config)["recent_decisions"]
            block["profiles"] = routing_status(self.config)["profiles"]
            block["providers"] = routing_status(self.config)["providers"]
            block["profile"] = profile
            self._send_json(block)
            return
        self._send_json(routing_status(self.config))

    def _serve_routing_policy_api(self):
        """POST /api/routing/policy {policy?, min_score?, enabled?, profile?}
        — persist routing policy for a scope.

        ``profile`` (optional) scopes the change to that profile's override
        (``routing_*:<profile>`` keys); without it the global value is set.
        ``policy`` ∈ eager | cost_first | explore; ``min_score`` is a 0–1 floor.
        Applies immediately, no restart.
        """
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        from ..api.cost_cache import get_settings
        settings = resolve_service("settings", fallback=get_settings)
        if settings is None:
            self._send_json({"error": "settings store not initialized"}, 500)
            return
        profile = (body.get("profile") or "").strip() or None
        if "clear_profile" in body and body.get("clear_profile") is True and profile:
            settings.clear_routing_enabled(profile)
            settings.clear_routing_policy(profile)
            settings.clear_routing_min_score(profile)
            settings.clear_routing_rules(profile)
            from ..api.router import routing_status
            self._send_json(routing_status(self.config))
            return
        if "policy" in body:
            policy = (body.get("policy") or "").strip().lower()
            if policy not in ("eager", "cost_first", "explore"):
                self._send_json({"error": "policy must be eager | cost_first | explore"}, 400)
                return
            settings.set_routing_policy(policy, profile=profile)
        if "min_score" in body:
            try:
                min_score = max(0.0, min(1.0, float(body["min_score"])))
            except (TypeError, ValueError):
                self._send_json({"error": "min_score must be a number 0–1"}, 400)
                return
            settings.set_routing_min_score(min_score, profile=profile)
        if "enabled" in body:
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                self._send_json({"error": "enabled must be a boolean"}, 400)
                return
            settings.set_routing_enabled(enabled, profile=profile)
            # Keep the DB-backed dynamic_routing config section in sync so
            # ``config.dynamic_routing`` reflects the current global toggle.
            if not profile:
                _sync_dynamic_routing_enabled(settings, enabled)
        from ..api.router import routing_status
        self._send_json(routing_status(self.config))

    def _serve_routing_rules_api(self):
        """POST /api/routing/rules {rules: [...], profile?} — validate + persist.

        Each rule: {task, profile, action (prefer|block|policy), provider?,
        model?, min_score?, policy?, enabled?}. ``profile`` (optional) scopes
        the rules to that profile's override (``routing_rules:<profile>``).
        Replaces the whole list for that scope.
        """
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        rules = body.get("rules")
        if not isinstance(rules, list):
            self._send_json({"error": "'rules' must be a list"}, 400)
            return
        for i, rule in enumerate(rules):
            if not isinstance(rule, dict):
                self._send_json({"error": f"rule {i} must be an object"}, 400)
                return
            action = rule.get("action")
            if action not in ("prefer", "block", "policy"):
                self._send_json({"error": f"rule {i}: action must be prefer | block | policy"}, 400)
                return
            if action != "policy":
                if not rule.get("provider") and not rule.get("model"):
                    self._send_json({"error": f"rule {i}: prefer/block need provider and/or model"}, 400)
                    return
            if action == "policy" and rule.get("policy") not in ("eager", "cost_first", "explore"):
                self._send_json({"error": f"rule {i}: policy rule needs a valid policy"}, 400)
                return
            if rule.get("min_score") is not None:
                try:
                    float(rule["min_score"])
                except (TypeError, ValueError):
                    self._send_json({"error": f"rule {i}: min_score must be a number"}, 400)
                    return
        from ..api.cost_cache import get_settings
        settings = resolve_service("settings", fallback=get_settings)
        if settings is None:
            self._send_json({"error": "settings store not initialized"}, 500)
            return
        profile = (body.get("profile") or "").strip() or None
        settings.set_routing_rules(rules, profile=profile)
        from ..api.router import routing_status
        self._send_json(routing_status(self.config))


# ── Usage Stats API ──────────────────────────────────────────────────────────

class UsageEndpoints:
    """Usage statistics and usage page."""

    config: Any
    engine: Any
    path: Any
    _send_json: Any
    send_response: Any
    send_header: Any
    end_headers: Any
    wfile: Any

    def _serve_usage_stats_api(self):
        """Return per-provider aggregates: daily spending, by-model, by-profile.

        Accepts ?provider=X&days=N or ?provider=X&start=YYYY-MM-DD&end=YYYY-MM-DD.
        """
        from sqlalchemy import func, true

        qs = parse_qs(urlparse(self.path).query)
        provider = qs.get("provider", [None])[0]
        days = int(qs.get("days", ["30"])[0])
        start_str = qs.get("start", [None])[0]
        end_str = qs.get("end", [None])[0]

        try:
            with get_session(self.engine) as session:
                base_filter = [RequestModel.success == 1]
                if provider:
                    base_filter.append(RequestModel.provider == provider)

                # Date range: use start/end if provided, else use days limit
                if start_str and end_str:
                    base_filter.append(RequestModel.timestamp >= start_str)
                    base_filter.append(RequestModel.timestamp <= end_str + "T23:59:59")
                    daily_date_filter = RequestModel.timestamp.between(start_str, end_str + "T23:59:59")
                else:
                    daily_date_filter = true()

                # Daily spending trend
                daily_q = (
                    session.query(
                        func.substr(RequestModel.timestamp, 1, 10).label("date"),
                        func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                        func.count(RequestModel.id).label("requests"),
                        func.coalesce(func.sum(RequestModel.prompt_tokens), 0).label("prompt_tokens"),
                        func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("completion_tokens"),
                        func.coalesce(func.sum(RequestModel.cache_hit_tokens), 0).label("cache_hit_tokens"),
                        func.coalesce(func.sum(RequestModel.cache_miss_tokens), 0).label("cache_miss_tokens"),
                        func.coalesce(func.sum(RequestModel.latency_ms), 0).label("latency_ms"),
                    )
                )
                for f in base_filter:
                    daily_q = daily_q.filter(f)
                if not (start_str and end_str):
                    daily_q = daily_q.filter(daily_date_filter)
                daily_rows = (
                    daily_q
                    .group_by(func.substr(RequestModel.timestamp, 1, 10))
                    .order_by(func.substr(RequestModel.timestamp, 1, 10).desc())
                    .limit(days)
                    .all()
                )

                # Build lookup of existing data keyed by date
                daily_map = {}
                for r in daily_rows:
                    total_tok = r.prompt_tokens + r.completion_tokens
                    daily_map[r.date] = {
                        "cost": float(r.cost),
                        "requests": r.requests,
                        "prompt_tokens": r.prompt_tokens,
                        "completion_tokens": r.completion_tokens,
                        "cache_hit_tokens": r.cache_hit_tokens,
                        "cache_miss_tokens": r.cache_miss_tokens,
                        "tokens": total_tok,
                        "latency_ms": r.latency_ms,
                    }

                # Determine full date range to fill gaps with zero-usage days
                if start_str and end_str:
                    date_end = datetime.strptime(end_str, "%Y-%m-%d").date()
                    date_start = datetime.strptime(start_str, "%Y-%m-%d").date()
                else:
                    date_end = datetime.utcnow().date()
                    date_start = date_end - timedelta(days=days - 1)

                # Generate every date in the range, inserting zeros for missing days
                daily = []
                d = date_start
                while d <= date_end:
                    ds = d.strftime("%Y-%m-%d")
                    if ds in daily_map:
                        daily.append({"date": ds, **daily_map[ds]})
                    else:
                        daily.append({
                            "date": ds, "cost": 0, "requests": 0,
                            "prompt_tokens": 0, "completion_tokens": 0,
                            "cache_hit_tokens": 0, "cache_miss_tokens": 0,
                            "tokens": 0, "latency_ms": 0,
                        })
                    d += timedelta(days=1)

                # Per-model aggregates (with cache tokens)
                model_q = (
                    session.query(
                        RequestModel.model,
                        func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                        func.count(RequestModel.id).label("requests"),
                        func.coalesce(func.sum(RequestModel.prompt_tokens), 0).label("prompt_tokens"),
                        func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("completion_tokens"),
                        func.coalesce(func.sum(RequestModel.cache_hit_tokens), 0).label("cache_hit_tokens"),
                        func.coalesce(func.sum(RequestModel.cache_miss_tokens), 0).label("cache_miss_tokens"),
                    )
                )
                for f in base_filter:
                    model_q = model_q.filter(f)
                model_rows = model_q.group_by(RequestModel.model).order_by(func.sum(RequestModel.cost).desc()).all()

                # Compute cache savings from gateway config pricing
                total_cache_savings = 0.0
                for r in model_rows:
                    try:
                        pricing = self.config.get_pricing(provider or "deepseek", r.model)
                        if pricing and r.cache_hit_tokens > 0:
                            savings = (r.cache_hit_tokens / 1_000_000) * (
                                pricing["cache_miss"] - pricing["cache_hit"]
                            )
                            total_cache_savings += savings
                    except Exception:
                        pass

                by_model = {
                    r.model: {
                        "cost": float(r.cost),
                        "requests": r.requests,
                        "prompt_tokens": r.prompt_tokens,
                        "completion_tokens": r.completion_tokens,
                        "cache_hit_tokens": r.cache_hit_tokens,
                        "cache_miss_tokens": r.cache_miss_tokens,
                    }
                    for r in model_rows
                }

                # Per-profile aggregates
                profile_q = (
                    session.query(
                        RequestModel.profile,
                        func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                        func.count(RequestModel.id).label("requests"),
                        func.coalesce(func.sum(RequestModel.prompt_tokens), 0).label("prompt_tokens"),
                        func.coalesce(func.sum(RequestModel.completion_tokens), 0).label("completion_tokens"),
                    )
                )
                for f in base_filter:
                    profile_q = profile_q.filter(f)
                profile_rows = profile_q.group_by(RequestModel.profile).order_by(func.sum(RequestModel.cost).desc()).all()
                by_profile = {
                    r.profile: {
                        "cost": float(r.cost),
                        "requests": r.requests,
                        "prompt_tokens": r.prompt_tokens,
                        "completion_tokens": r.completion_tokens,
                    }
                    for r in profile_rows
                }

                # Totals (respect date range)
                total_q = (
                    session.query(
                        func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                        func.count(RequestModel.id).label("requests"),
                        func.coalesce(func.sum(RequestModel.prompt_tokens + RequestModel.completion_tokens), 0).label("tokens"),
                        func.coalesce(func.sum(RequestModel.latency_ms), 0).label("latency_ms"),
                    )
                )
                for f in base_filter:
                    total_q = total_q.filter(f)
                total_row = total_q.first()
                total_cost = float(total_row.cost) if total_row else 0
                total_requests = total_row.requests if total_row else 0
                total_tokens = int(total_row.tokens) if total_row and total_row.tokens else 0
                total_latency_ms = int(total_row.latency_ms) if total_row else 0

                # Aggregate cache stats
                cache_hit = sum(r.cache_hit_tokens for r in model_rows)
                cache_miss = sum(r.cache_miss_tokens for r in model_rows)

            self._send_json({
                "provider": provider,
                "daily": daily,
                "by_model": by_model,
                "by_profile": by_profile,
                "totals": {
                    "cost": total_cost,
                    "requests": total_requests,
                    "tokens": total_tokens,
                    "prompt_tokens": sum(r.prompt_tokens for r in model_rows),
                    "completion_tokens": sum(r.completion_tokens for r in model_rows),
                    "latency_ms": total_latency_ms,
                },
                "cache": {
                    "hit_tokens": cache_hit,
                    "miss_tokens": cache_miss,
                    "savings": round(total_cache_savings, 6),
                },
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_usage_totals_api(self):
        """Return just the totals for a date range — lightweight endpoint for date-filter pills."""
        from sqlalchemy import func

        qs = parse_qs(urlparse(self.path).query)
        provider = qs.get("provider", [None])[0]
        start_str = qs.get("start", [None])[0]
        end_str = qs.get("end", [None])[0]

        try:
            with get_session(self.engine) as session:
                filters = [RequestModel.success == 1]
                if provider:
                    filters.append(RequestModel.provider == provider)
                if start_str and end_str:
                    filters.append(RequestModel.timestamp >= start_str)
                    filters.append(RequestModel.timestamp <= end_str + "T23:59:59")

                total_q = (
                    session.query(
                        func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                        func.count(RequestModel.id).label("requests"),
                        func.coalesce(func.sum(RequestModel.prompt_tokens + RequestModel.completion_tokens), 0).label("tokens"),
                    )
                )
                for f in filters:
                    total_q = total_q.filter(f)
                row = total_q.first()
                result = {
                    "provider": provider,
                    "start": start_str,
                    "end": end_str,
                    "cost": float(row.cost) if row else 0,
                    "requests": row.requests if row else 0,
                    "tokens": int(row.tokens) if row and row.tokens else 0,
                }
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_usage_page(self):
        """Server-rendered Usage page (M2b) — spend and balances, outside Activity."""
        from ..ui.pages import render_usage_page
        self._send_html(render_usage_page(self.config, self.engine, self._qs()))


# ── Dashboard / Page Endpoints ───────────────────────────────────────────────

class DashboardEndpoints:
    """The merged pages (M2): Activity, Models, Profiles, Alerts, Setup."""

    config: Any
    engine: Any
    headers: Any
    _send_json: Any
    send_response: Any
    send_header: Any
    end_headers: Any
    wfile: Any

    # ── M2 shared page plumbing ──────────────────────────────────────────────
    def _qs(self) -> dict:
        """Query-string params as a flat dict (first value wins)."""
        from urllib.parse import parse_qs, urlsplit
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def _send_html(self, html: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _redirect(self, location: str, code: int = 302):
        """Send the visitor to the page that now owns this content (M2).

        A 302 rather than a 301: these are UI re-organisations, and a permanent
        redirect would be cached by browsers long after the layout settles.
        """
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _host_headers(self) -> dict:
        """The Host / scheme pair the overview needs to build share links."""
        host = self.headers.get("Host", "localhost:8734")
        scheme = "https" if (
            self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"
            or self.headers.get("X-Forwarded-Scheme") == "https"
        ) else "http"
        return {"Host": host, "X-Forwarded-Proto": scheme}

    def _serve_activity_page(self):
        """Server-rendered Activity page (?tab=overview|logs[&view=...])."""
        from ..ui.pages import render_activity_page
        self._send_html(render_activity_page(self.config, self.engine,
                                             self._qs(), self._host_headers()))

    def _serve_dashboard(self, profile_filter: str | None = None):
        """Legacy / and /dashboard — now the Overview tab of Activity (M2)."""
        target = "/activity"
        if profile_filter:
            target += "?profile=" + profile_filter
        self._redirect(target)

    def _serve_logs_page(self):
        """Legacy /logs — now the Logs tab of Activity (M2).

        The realm (``view``) and the table params (``per`` / ``sort`` /
        ``profile``) ride along in the query string, so existing links land on
        the same rows they always did.
        """
        from urllib.parse import urlencode
        q = self._qs()
        q["tab"] = "logs"
        self._redirect("/activity?" + urlencode(q))

    def _serve_alerts_page(self):
        """Server-rendered Alerts page."""
        from ..ui.pages import render_alerts_page
        html = render_alerts_page(self.config, self.engine)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_models_page(self):
        """Server-rendered Models page (?tab=matrix|providers) — M2."""
        from ..ui.pages import render_models_page
        self._send_html(render_models_page(self.config, self.engine, self._qs()))

    def _serve_capability_api(self):
        """GET /api/models/capability — return the capability matrix as JSON.

        Optional ``?release=<label>`` filters to one release; without it each
        model's ACTIVE release is returned (``active_release`` pin → newest
        release), matching the router's ``load_capability_matrix`` exactly.
        """
        from urllib.parse import parse_qs, urlparse
        from ..api.models import ModelCapability, get_session as _gs
        from ..api.seed_capabilities import resolve_active_rows

        qs = parse_qs(urlparse(self.path).query)
        source_filter = qs.get("source", [None])[0]
        release_filter = qs.get("release", [None])[0]

        try:
            with _gs(self.engine) as session:
                q = session.query(ModelCapability)
                if source_filter:
                    q = q.filter(ModelCapability.source == source_filter)
                rows = q.all()

            active_releases: dict[str, str] = {}
            if release_filter:
                rows = [r for r in rows if r.release_label == release_filter or r.release_label is None]
            else:
                from ..api.seed_capabilities import load_model_registry, effective_releases
                db_path = "data/costs.db"
                try:
                    if getattr(self.engine, "url", None) is not None:
                        db_path = str(self.engine.url.database) or db_path
                except Exception:
                    pass
                registry = load_model_registry(db_path)
                all_rows = rows
                rows = resolve_active_rows(all_rows, registry)
                # Report the release actually in effect per model (pinned or
                # newest), computed deterministically over ALL rows.
                active_releases = effective_releases(all_rows, registry)

            tasks: dict[str, dict[str, float]] = {}
            sources: dict[str, dict[str, str]] = {}
            benchmarks: dict[str, dict[str, str]] = {}
            releases: dict[str, dict[str, str]] = {}

            for r in rows:
                tasks.setdefault(r.task_type, {})[r.model] = r.score
                sources.setdefault(r.task_type, {})[r.model] = r.source
                if r.benchmark_category:
                    benchmarks.setdefault(r.task_type, {})[r.model] = r.benchmark_category
                if r.release_label:
                    releases.setdefault(r.task_type, {})[r.model] = r.release_label

            # The per-subtask breakdown used to come from the LiveBench
            # ``all_tasks.csv`` import (``model_capability_subtasks``, dropped in
            # migration 023). The key stays as an empty map so the response shape
            # does not change under the current UI; the panels that rendered it
            # are removed with the rest of the benchmark UI.
            subtasks: dict[str, dict[str, dict[str, float]]] = {}

            self._send_json({
                "tasks": tasks,
                "sources": sources,
                "benchmark_categories": benchmarks,
                "releases": releases,
                "active_releases": active_releases,
                "subtasks": subtasks,
                "count": len(rows),
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_registry_api(self):
        """GET /api/models/registry — return the model registry as JSON."""
        import json
        from ..api.models import ModelRegistryEntry, get_session as _gs
        try:
            with _gs(self.engine) as session:
                rows = session.query(ModelRegistryEntry).order_by(
                    ModelRegistryEntry.logical_name
                ).all()
            entries = []
            for r in rows:
                try:
                    provider_mappings = json.loads(r.provider_mappings_json or "{}")
                except json.JSONDecodeError:
                    provider_mappings = {}
                entries.append({
                    "logical_name": r.logical_name,
                    "benchmark_key": r.benchmark_key,
                    "providers": list(provider_mappings.keys()),
                    "provider_mappings": provider_mappings,
                    "active_release": r.active_release,
                    "benchmark_release": r.benchmark_release,
                    "quantization": r.quantization,
                    "updated_at": r.updated_at,
                })
            self._send_json({"registry": entries, "count": len(entries)})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)


    def _serve_capability_seed_api(self):
        """POST /api/models/capability/seed — (re)apply the bundled declared matrix.

        Writes the shipped ``data/declared_capabilities.json`` into
        ``model_capabilities`` — the instant "give the router capability data to
        route by" action surfaced by the Setup page when no scores exist yet.
        Idempotent; never touches hand-set (``source="manual"``) rows.
        """
        from ..api.seed_capabilities import seed_capabilities

        db_path = _engine_db_path(self.engine)
        try:
            count = seed_capabilities(db_path)
            self._send_json({"ok": True, "materialized": count})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_capability_manual_api(self):
        """POST /api/models/capability/manual — upsert user-entered scores.

        Body: {model, release (optional), scores: {task_type: 0..1 or 0..100}}.
        Scores are stored source="manual" with the given release label (default
        today's date), so manual edits never clobber benchmark results.
        """
        from datetime import datetime, timezone
        from ..api.models import ModelCapability, get_session as _gs
        from ..api.router import invalidate_registry_cache

        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return

        model = (body.get("model") or "").strip().lower()
        scores = body.get("scores") or {}
        release = (body.get("release") or "").strip() or None
        if not model:
            self._send_json({"error": "missing 'model'"}, 400)
            return
        if not isinstance(scores, dict) or not scores:
            self._send_json({"error": "'scores' must be a non-empty object"}, 400)
            return

        label = release or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = datetime.now(timezone.utc).isoformat()

        try:
            with _gs(self.engine) as session:
                for task, value in scores.items():
                    try:
                        raw = float(value)
                    except (TypeError, ValueError):
                        self._send_json({"error": f"score for {task} is not a number"}, 400)
                        return
                    # Accept 0-100 or 0-1; normalize to 0-1.
                    normalized = raw / 100.0 if raw > 1.0 else raw
                    normalized = round(max(0.0, min(1.0, normalized)), 4)

                    existing = session.query(ModelCapability).filter_by(
                        model=model, task_type=task, source="manual",
                        release_label=label,
                    ).first()
                    if existing is not None:
                        existing.score = normalized
                        existing.raw_score = raw
                        existing.updated_at = now
                    else:
                        session.add(ModelCapability(
                            model=model,
                            task_type=task,
                            score=normalized,
                            source="manual",
                            raw_score=raw,
                            release_label=label,
                            updated_at=now,
                        ))
                session.commit()
            invalidate_registry_cache()
            self._send_json({"ok": True, "model": model, "release": label})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_registry_upsert_api(self):
        """POST /api/models/registry — create or update a registry entry.

        Body: {logical_name, benchmark_key, provider_mappings: {..},
               active_release?, benchmark_release?, quantization?}
        """
        import json
        from datetime import datetime, timezone
        from ..api.models import ModelRegistryEntry, get_session as _gs
        from ..api.router import invalidate_registry_cache, normalize_model_id, detect_quantization

        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return

        logical = (body.get("logical_name") or "").strip().lower()
        benchmark = (body.get("benchmark_key") or "").strip().lower()
        active_release = (body.get("active_release") or "").strip() or None
        benchmark_release = (body.get("benchmark_release") or "").strip() or None
        quantization = (body.get("quantization") or "").strip() or None
        provider_mappings = body.get("provider_mappings") or {}
        if not logical:
            self._send_json({"error": "missing 'logical_name'"}, 400)
            return
        if not benchmark:
            self._send_json({"error": "missing 'benchmark_key'"}, 400)
            return
        if not isinstance(provider_mappings, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in provider_mappings.items()
        ):
            self._send_json({"error": "'provider_mappings' must be a {provider: model} object"}, 400)
            return

        # Auto-detect quantization from the benchmark key when not provided.
        if quantization is None:
            quantization = detect_quantization(benchmark) or detect_quantization(logical)

        # Normalize llama.cpp-style paths (/models/x.gguf → x) unless the
        # admin explicitly opted out via an explicit quantization-less name.
        logical = normalize_model_id(logical)
        benchmark = normalize_model_id(benchmark)

        now = datetime.now(timezone.utc).isoformat()
        try:
            with _gs(self.engine) as session:
                # Uniqueness guards: a benchmark key may belong to only one
                # logical model, and a (provider, provider-side model ID) pair
                # may be mapped by only one logical model. Updating the SAME
                # logical model (below) is always allowed.
                others = session.query(ModelRegistryEntry).filter(
                    ModelRegistryEntry.logical_name != logical
                ).all()
                for other in others:
                    if benchmark and other.benchmark_key and other.benchmark_key == benchmark:
                        self._send_json({
                            "error": f"benchmark key '{benchmark}' is already registered to '{other.logical_name}'"
                        }, 400)
                        return
                    try:
                        other_mappings = json.loads(other.provider_mappings_json or "{}")
                    except json.JSONDecodeError:
                        other_mappings = {}
                    for prov, mid in provider_mappings.items():
                        other_mid = other_mappings.get(prov)
                        if other_mid and normalize_model_id(other_mid) == normalize_model_id(mid):
                            self._send_json({
                                "error": f"provider '{prov}' already maps '{other_mid}' to '{other.logical_name}'"
                            }, 400)
                            return
                entry = session.query(ModelRegistryEntry).filter_by(
                    logical_name=logical
                ).first()
                if entry:
                    entry.benchmark_key = benchmark
                    entry.provider_mappings_json = json.dumps(provider_mappings)
                    if active_release is not None:
                        entry.active_release = active_release
                    if benchmark_release is not None:
                        entry.benchmark_release = benchmark_release
                    if quantization is not None:
                        entry.quantization = quantization
                    entry.updated_at = now
                    action = "updated"
                else:
                    session.add(ModelRegistryEntry(
                        logical_name=logical,
                        benchmark_key=benchmark,
                        provider_mappings_json=json.dumps(provider_mappings),
                        active_release=active_release,
                        benchmark_release=benchmark_release,
                        quantization=quantization,
                        updated_at=now,
                    ))
                    action = "created"
                session.commit()
            invalidate_registry_cache()
            # Registry identity/aliases changed → also drop the router's cached
            # capability matrix so lookups use the new mapping.
            try:
                from ..api.router import invalidate_router_matrix
                invalidate_router_matrix()
            except Exception:  # noqa: BLE001
                pass
            self._send_json({"ok": True, "action": action, "logical_name": logical})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_registry_delete_api(self, logical: str):
        """DELETE /api/models/registry/{logical} — remove a registry entry."""
        from ..api.models import ModelRegistryEntry, get_session as _gs
        from ..api.router import invalidate_registry_cache

        try:
            with _gs(self.engine) as session:
                entry = session.query(ModelRegistryEntry).filter_by(
                    logical_name=logical
                ).first()
                if not entry:
                    self._send_json({"error": "entry not found"}, 404)
                    return
                session.delete(entry)
                session.commit()
            invalidate_registry_cache()
            self._send_json({"ok": True, "deleted": logical})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_daily_costs_api(self):
        """API endpoint: daily costs as JSON."""
        from sqlalchemy import func

        try:
            with get_session(self.engine) as session:
                rows = (
                    session.query(
                        func.substr(RequestModel.timestamp, 1, 10).label("date"),
                        func.coalesce(func.sum(RequestModel.cost), 0).label("cost"),
                        func.count(RequestModel.id).label("requests"),
                    )
                    .group_by(func.substr(RequestModel.timestamp, 1, 10))
                    .order_by(func.substr(RequestModel.timestamp, 1, 10).desc())
                    .limit(90)
                    .all()
                )
            data = [{"date": r.date, "cost": float(r.cost), "requests": r.requests} for r in rows]
            self._send_json({"daily_costs": data})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_recent_requests_api(self):
        """API endpoint: last 100 requests as JSON (with tokens + cache savings)."""
        try:
            with get_session(self.engine) as session:
                rows = (
                    session.query(RequestModel)
                    .order_by(RequestModel.id.desc())
                    .limit(100)
                    .all()
                )
            data = [
                {
                    "timestamp": r.timestamp,
                    "profile": r.profile,
                    "model": r.model,
                    "provider": r.provider,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "cache_hit_tokens": r.cache_hit_tokens,
                    "cache_miss_tokens": r.cache_miss_tokens,
                    "cost": r.cost,
                    "saved": _savings_for_model(self.config, str(r.model), int(r.cache_hit_tokens)),
                    "latency_ms": r.latency_ms,
                    "success": bool(r.success),
                    "error_type": r.error_type,
                    "error_detail": r.error_detail,
                }
                for r in rows
            ]
            self._send_json({"requests": data})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_logs_api(self):
        """API endpoint: filterable, paginated request logs.

        Query params: profile, provider, status (all/success/error),
                      limit (default 100), offset (default 0), sort (asc/desc).
        """
        from sqlalchemy import func

        qs = parse_qs(urlparse(self.path).query)
        profile = qs.get("profile", [None])[0]
        provider = qs.get("provider", [None])[0]
        status = qs.get("status", ["all"])[0]
        limit = min(int(qs.get("limit", ["100"])[0]), 1000)
        offset = int(qs.get("offset", ["0"])[0])
        sort_order = (qs.get("sort", ["desc"])[0] or "desc").lower()
        order_col = RequestModel.id.desc() if sort_order == "desc" else RequestModel.id.asc()

        try:
            with get_session(self.engine) as session:
                filters = []
                if profile:
                    filters.append(RequestModel.profile == profile)
                if provider:
                    filters.append(RequestModel.provider == provider)
                if status == "success":
                    filters.append(RequestModel.success == 1)
                elif status == "error":
                    filters.append(RequestModel.success == 0)

                total_q = session.query(func.count(RequestModel.id))
                rows_q = session.query(RequestModel)

                for f in filters:
                    total_q = total_q.filter(f)
                    rows_q = rows_q.filter(f)

                total = total_q.scalar() or 0
                rows = rows_q.order_by(order_col).offset(offset).limit(limit).all()

                # Distinct profiles and providers for filter dropdowns
                profiles_q = session.query(
                    RequestModel.profile, func.count(RequestModel.id).label("n")
                ).group_by(RequestModel.profile).order_by(func.count(RequestModel.id).desc())
                providers_q = session.query(
                    RequestModel.provider, func.count(RequestModel.id).label("n")
                ).group_by(RequestModel.provider).order_by(func.count(RequestModel.id).desc())

                log_rows = []
                for r in rows:
                    log_rows.append({
                        "id": r.id,
                        "timestamp": r.timestamp,
                        "profile": r.profile,
                        "model": r.model,
                        "provider": r.provider,
                        "prompt_tokens": r.prompt_tokens,
                        "completion_tokens": r.completion_tokens,
                        "cache_hit_tokens": r.cache_hit_tokens,
                        "cache_miss_tokens": r.cache_miss_tokens,
                        "cost": r.cost,
                        "saved": _savings_for_model(self.config, r.model, r.cache_hit_tokens),
                        "latency_ms": r.latency_ms,
                        "success": bool(r.success),
                        "error_type": r.error_type,
                        "error_detail": r.error_detail,
                    })

            self._send_json({
                "total": total,
                "limit": limit,
                "offset": offset,
                "rows": log_rows,
                "profiles": [{"name": p.profile, "count": p.n} for p in profiles_q.all()],
                "providers": [{"name": p.provider, "count": p.n} for p in providers_q.all()],
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_keys_dashboard(self):
        """Legacy /keys — now the API Keys tab of Profiles (M2)."""
        self._redirect("/profiles?tab=keys")

    def _serve_providers_page(self):
        """Legacy /providers — now the Providers tab of Models (M2)."""
        self._redirect("/models?tab=providers")

    def _serve_profiles_page(self):
        """Server-rendered Profiles page — the profile cards, and Config (M2c).

        M2b carried API Keys and Cron as tabs here. M2c moved both under the
        profile they describe (``/profiles/<name>``), so a request for the old tab
        lands on the directory rather than rendering a tab that no longer exists.
        """
        from ..ui.pages import render_profiles_page
        tab = str((self._qs() or {}).get("tab") or "").strip().lower()
        if tab in ("keys", "cron"):
            self._redirect("/profiles")
            return
        self._send_html(render_profiles_page(self.config, self.engine, self._qs()))

    def _serve_profile_detail_page(self, name: str):
        """Server-rendered level-3 page: one profile, five tabs (M2c).

        The name is handed to the renderer, which validates it before using it as
        a path or a config key — a bad name produces a page, not a traceback.
        """
        from ..ui.pages import render_profile_detail_page
        self._send_html(render_profile_detail_page(self.config, self.engine, name, self._qs()))

    def _serve_profile_avatar(self, name: str):
        """GET /api/profiles/{name}/avatar — the stored picture, or 404.

        A 404 rather than a placeholder image: the browser falls back to the
        initials the page already rendered, so "no picture" needs no bytes.
        """
        from ..api import profile_data
        try:
            found = profile_data.avatar_for(name)
        except profile_data.BadProfileName:
            found = None
        if not found:
            self._send_json({"error": "no avatar"}, 404)
            return
        path, ctype = found
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            self._send_json({"error": "avatar unreadable"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Replaceable by design, so a stale face must not survive in a cache.
        self.send_header("Cache-Control", "no-cache, max-age=0, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _serve_profile_avatar_put(self, name: str):
        """POST /api/profiles/{name}/avatar — store a picture sent as base64 JSON.

        Base64 in a JSON body rather than multipart: the HTTP layer here is a
        hand-rolled stdlib handler with no multipart parser, and writing one to
        accept a 256 KB profile picture would be the wrong trade. The content type
        is validated against the decoded bytes, never trusted.
        """
        from ..api import profile_data
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        raw = str(body.get("data_base64") or "")
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[-1]
        try:
            data = base64.b64decode(raw, validate=True)
        except Exception:
            self._send_json({"ok": False, "error": "data_base64 is not valid base64"}, 400)
            return
        try:
            result = profile_data.save_avatar(name, str(body.get("content_type") or ""), data)
        except profile_data.BadProfileName:
            self._send_json({"ok": False, "error": "invalid profile name"}, 400)
            return
        except ValueError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
            return
        self._send_json(result)

    def _serve_profile_avatar_delete(self, name: str):
        """DELETE /api/profiles/{name}/avatar — forget the picture."""
        from ..api import profile_data
        try:
            self._send_json(profile_data.delete_avatar(name))
        except profile_data.BadProfileName:
            self._send_json({"ok": False, "error": "invalid profile name"}, 400)


# ── Memory Plugin Endpoints ─────────────────────────────────────────────────

class MemoryEndpoints:
    """Per-profile semantic memory API: retain / recall / forget / count.

    Routes: ``/{profile}/memory/{action}``. Uses the same auth model as chat
    completions (profile ``auth_required``). Returns 501 with a Setup hint
    when the memory module is not installed/active.
    """

    config: Any
    engine: Any
    headers: Any
    _send_json: Any
    _read_body: Any

    _MEMORY_ACTIONS = ("retain", "recall", "forget", "count")

    def _memory_profile(self) -> str | None:
        """Return the profile from a ``/{profile}/memory/...`` path, or None."""
        path = self.path.rstrip("/")
        parts = path.split("/")
        if len(parts) >= 4 and parts[1] in self.config.profiles and parts[2] == "memory":
            return parts[1]
        return None

    def _memory_auth(self, profile: str) -> bool:
        """Validate the profile key like chat completions. Returns True when ok."""
        profile_cfg = self.config.get_profile(profile) or {}
        if not profile_cfg.get("auth_required", True):
            return True
        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            self._send_json({"error": {"code": "LCP-4010",
                                       "message": "API key required for this profile."}}, 401)
            return False
        raw_key = auth_header[7:]
        try:
            from ..api.key_manager import get_key_manager
            km = resolve_service("key_manager", fallback=get_key_manager)
            if km is None:
                self._send_json({"error": {"code": "LCP-5001", "message": "internal error"}}, 500)
                return False
            key_info = km.validate_key(raw_key)
            if key_info is None:
                self._send_json({"error": {"code": "LCP-4011",
                                           "message": "invalid or revoked API key"}}, 401)
                return False
            allowed = key_info.get("allowed_profiles")
            if allowed:
                allowed_list = [p.strip() for p in allowed.split(",") if p.strip()]
                if profile not in allowed_list:
                    self._send_json({"error": {"code": "LCP-4030",
                                               "message": f"key does not have access to profile '{profile}'"}}, 403)
                    return False
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("memory_auth_failed", error=str(exc), profile=profile)
            self._send_json({"error": {"code": "LCP-5001", "message": "internal error"}}, 500)
            return False

    def _memory_backend_or_501(self):
        """Return the active backend or send a 501 + None."""
        try:
            from ..api.memory import get_memory
            backend = resolve_service("memory", fallback=get_memory)
        except Exception:
            backend = None
        if backend is None:
            self._send_json({
                "error": {
                    "code": "LCP-5010",
                    "message": "Memory plugin is not installed or disabled — "
                               "install the Memory (LanceDB) module from the Setup page.",
                }
            }, 501)
            return None
        return backend

    def _serve_memory_api(self, profile: str, action: str):
        """Dispatch one memory action for a profile."""
        if action not in self._MEMORY_ACTIONS:
            self._send_json({"error": f"unknown memory action: {action}"}, 404)
            return
        if not self._memory_auth(profile):
            return
        backend = self._memory_backend_or_501()
        if backend is None:
            return

        try:
            if action == "count":
                self._send_json({"count": backend.count(profile)})
                return

            try:
                body = self._read_body()
            except Exception:
                self._send_json({"error": "invalid JSON body"}, 400)
                return

            if action == "retain":
                content = (body.get("content") or "").strip()
                if not content:
                    self._send_json({"error": "missing 'content' field"}, 400)
                    return
                memory_id = backend.retain(
                    content,
                    metadata=body.get("metadata"),
                    tags=body.get("tags"),
                    profile=profile,
                )
                self._send_json({"memory_id": memory_id})
            elif action == "recall":
                query = (body.get("query") or body.get("content") or "").strip()
                if not query:
                    self._send_json({"error": "missing 'query' field"}, 400)
                    return
                top_k = int(body.get("top_k", 10) or 10)
                results = backend.recall(
                    query, top_k=top_k,
                    tag_filter=body.get("tag_filter"),
                    profile=profile,
                )
                self._send_json({"results": results})
            elif action == "forget":
                memory_id = (body.get("memory_id") or "").strip()
                if not memory_id:
                    self._send_json({"error": "missing 'memory_id' field"}, 400)
                    return
                deleted = backend.forget(memory_id, profile=profile)
                self._send_json({"deleted": deleted})
        except Exception as exc:  # noqa: BLE001
            from ..api.memory.base import MemoryError as MemErr
            if isinstance(exc, MemErr):
                self._send_json({"error": str(exc)}, 400)
            else:
                logger.error("memory_api_failed", action=action, profile=profile, error=str(exc))
                self._send_json({"error": {"code": "LCP-5001", "message": "internal error"}}, 500)


# ── First-run Setup Wizard Endpoints ─────────────────────────────────────────

class SetupEndpoints:
    """First-run setup wizard: manifest, install, progress, skip."""

    config: Any
    engine: Any
    _send_json: Any
    _read_body: Any
    send_response: Any
    send_header: Any
    end_headers: Any
    wfile: Any

    def _serve_setup_page(self):
        """Server-rendered setup wizard page."""
        from ..ui.pages import render_setup_page
        html = render_setup_page(self.config, self.engine)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_setup_api(self):
        """GET /api/setup — full manifest (steps + modules) and completion state."""
        from ..api import setup as setup_mod

        try:
            self._send_json({
                "manifest": setup_mod.manifest(self.config, self.engine),
                "state": setup_mod.load_state(self.engine),
                "complete": setup_mod.is_complete(self.engine, self.config),
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_setup_install_api(self, kind: str, name: str):
        """POST /api/setup/install/{kind}/{name} — install one plugin/module."""
        from ..api import setup as setup_mod

        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return

        try:
            if kind == "provider":
                result = setup_mod.install_provider(self.engine, self.config, name, body)
            elif kind == "module" and name == "router":
                # Semantic routing depends on capability data the router can
                # rank by (the declared matrix). Enforce server-side too.
                reason = setup_mod.router_install_blocked_reason(_engine_db_path(self.engine))
                if reason:
                    raise setup_mod.SetupError(reason)
                result = setup_mod.start_router_install(self.engine)
            elif kind == "module" and name == "memory":
                result = setup_mod.start_memory_install(self.engine)
            else:
                self._send_json({"error": f"unknown install target: {kind}/{name}"}, 404)
                return
            self._send_json({"ok": True, **result})
        except setup_mod.SetupError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            logger.error("setup_install_failed", kind=kind, name=name, error=str(e))
            self._send_json({"error": str(e)}, 500)

    def _serve_setup_progress_api(self):
        """GET /api/setup/progress — live progress of module installs.

        Returns per-module progress (``modules``) plus a back-compat top-level
        ``progress``/``installed`` that reflect any single in-flight module
        (router first, then memory) so older UIs keep working.
        """
        from ..api import setup as setup_mod

        entries = {}
        for name, prog, last, step in (
            ("router", setup_mod.router_progress(), setup_mod.router_last(),
             setup_mod.router_step),
            ("memory", setup_mod.mem_progress(), setup_mod.mem_last(),
             setup_mod.memory_step),
        ):
            entries[name] = prog or last or {"status": "idle", "progress": 0.0}
            entries[name]["installed"] = bool(step()["installed"])

        # Back-compat: the single in-flight/last module (router preferred).
        active = None
        for name in ("router", "memory"):
            state = entries[name]
            if state.get("status") not in (None, "idle"):
                active = state
                break
        if active is None:
            active = entries.get("router") or {"status": "idle", "progress": 0.0}

        self._send_json({
            "progress": active,
            "installed": bool(active.get("installed")),
            "modules": entries,
        })

    def _serve_setup_skip_api(self):
        """POST /api/setup/skip — mark the wizard as skipped."""
        from ..api import setup as setup_mod

        try:
            newly_skipped = setup_mod.mark_skipped(self.engine)
            self._send_json({"ok": True, "skipped": newly_skipped})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_setup_remove_api(self, kind: str, name: str):
        """DELETE /api/setup/{kind}/{name} — uninstall one plugin/module."""
        from ..api import setup as setup_mod

        try:
            if kind == "provider":
                result = setup_mod.remove_provider(self.engine, self.config, name)
            elif kind == "module" and name == "router":
                result = setup_mod.remove_router(self.engine)
            elif kind == "module" and name == "memory":
                result = setup_mod.remove_memory(self.engine)
            elif kind == "module" and name == "runboard":
                result = setup_mod.remove_runboard(self.engine)
            else:
                self._send_json({"error": f"unknown remove target: {kind}/{name}"}, 404)
                return
            self._send_json({"ok": True, **result})
        except setup_mod.SetupError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            logger.error("setup_remove_failed", kind=kind, name=name, error=str(e))
            self._send_json({"error": str(e)}, 500)


class WorkEndpoints:
    """Work layer: the moments LCP records about sessions, tasks, providers.

    This is the work-surface half of LCP, merged in as a module by admin
    direction. It reads the runboard decisions ledger read-only and renders
    moments -- it never writes to the board, so a Work view cannot change what
    was published.
    """

    config: Any
    engine: Any
    _send_json: Any
    send_response: Any
    send_header: Any
    end_headers: Any
    wfile: Any

    def _serve_work_decisions_page(self):
        """Server-rendered Work > Decisions page."""
        from ..ui.pages import render_work_decisions_page
        html = render_work_decisions_page(self.config, self.engine)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_work_decisions_api(self):
        """GET /api/work/decisions — the Decisions view as JSON.

        ``?per=``/``?page=``/``?sort=`` page the ledger (shared table module);
        without them the whole ledger is returned, as before.
        """
        from ..api import work as work_api
        from urllib.parse import parse_qs, urlsplit

        try:
            qs = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            self._send_json(work_api.decisions_view(params=qs))
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_tasks_page(self):
        """Server-rendered Work > Tasks page (supports ?states/q/tag/per/page)."""
        from ..ui.pages import render_work_tasks_page
        from urllib.parse import parse_qs, urlsplit
        qs = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        html = render_work_tasks_page(self.config, self.engine, qs)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _tasks_root_for(self, profile: Any) -> str:
        """Resolved task tree for ?profile=, or "" to mean "use the default"."""
        from ..api import work_sources
        try:
            return work_sources.tasks_root_for(str(profile or "").strip().lower())
        except Exception:  # noqa: BLE001
            return ""

    def _serve_work_tasks_api(self):
        """GET /api/work/tasks — the Tasks view as JSON (filters supported,
        faint lite=1 strips the heavy per-row PLAN/RESULTS payloads)."""
        from ..api import work_tasks
        from urllib.parse import parse_qs, urlsplit

        try:
            qs = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            # ?profile= scopes the tree (M2c). Without it the per-profile Tasks tab
            # would render its own heading over the default profile's rows.
            root = self._tasks_root_for(qs.get("profile"))
            if root:
                with work_tasks.use_root(root):
                    self._send_json(work_tasks.tasks_view(qs))
            else:
                self._send_json(work_tasks.tasks_view(qs))
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_tasks_detail_api(self):
        """GET /api/work/tasks/detail?task=<state>/<slug> — one task's heavy
        payload (PLAN.md, RESULTS.md, STATE-SUMMARY, SESSION-FACTS, artifacts)
        fetched lazily when its row is expanded."""
        from ..api import work_tasks
        from urllib.parse import parse_qs, urlsplit

        try:
            qs = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            key = qs.get("task") or ""
            if not key:
                self._send_json({"error": "task key required"}, 400)
                return
            # A task's detail must be read from the same tree its row came from.
            root = self._tasks_root_for(qs.get("profile"))
            if root:
                with work_tasks.use_root(root):
                    self._send_json(work_tasks.task_detail(key))
            else:
                self._send_json(work_tasks.task_detail(key))
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except FileNotFoundError as e:
            self._send_json({"error": str(e)}, 404)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_fleet_page(self):
        """Server-rendered Work > Fleet page."""
        from ..ui.pages import render_work_fleet_page
        html = render_work_fleet_page(self.config, self.engine)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_work_fleet_api(self):
        """GET /api/work/fleet — the Fleet view as JSON."""
        from ..api import work_fleet

        try:
            self._send_json(work_fleet.fleet_view())
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_cron_page(self):
        """Legacy /work/cron — redirects to the Cron tab of Profiles (M2b)."""
        target = "/profiles?tab=cron"
        profile = (self._qs() or {}).get("profile")
        if profile:
            target += "&profile=" + str(profile)
        self._redirect(target)

    def _serve_work_config_page(self):
        """Legacy /work/config — redirects to the Config tab of Profiles (M2b)."""
        self._redirect("/profiles?tab=config")

    def _serve_work_cron_api(self):
        """GET /api/work/cron — the Cron view as JSON."""
        from ..api import work_cron

        try:
            self._send_json(work_cron.cron_view())
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_cron_ops_get(self):
        """GET /api/work/cron/ops — pending + recent executed cron ops."""
        from ..api import work_cron

        try:
            self._send_json(work_cron.cron_ops_view())
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_cron_ops_post(self):
        """POST /api/work/cron/ops — validate + spool a cron mutation intent."""
        from ..api import work_cron

        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        try:
            op = work_cron.submit_cron_op(body)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return
        except Exception as e:
            self._send_json({"error": "internal: %s" % e}, 500)
            return
        self._send_json(op, 201)

    def _serve_work_sources_get(self):
        """GET /api/work/sources — resolved work-layer source configuration."""
        from ..api import work_sources

        try:
            self._send_json(work_sources.resolved_view())
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_sources_put(self):
        """PUT /api/work/sources — validate + persist source configuration."""
        from ..api import work_sources

        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return
        try:
            saved = work_sources.save_sources(body)
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
            return
        except Exception as e:
            self._send_json({"error": "internal: %s" % e}, 500)
            return
        self._send_json({"saved": True, "sources": saved})

    def _serve_work_conversations_page(self):
        """Server-rendered Work > Conversations page."""
        from ..ui.pages import render_work_conversations_page
        from urllib.parse import parse_qs, urlsplit
        qs = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
        html = render_work_conversations_page(self.config, self.engine, qs)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_work_conversations_api(self):
        """GET /api/work/conversations — grouped conversation list."""
        from ..api import work_conversations
        from urllib.parse import parse_qs, urlsplit

        try:
            qs = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            self._send_json(work_conversations.conversations_view(qs))
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_conversations_detail_api(self):
        """GET /api/work/conversations/detail?cid=… — chronological events."""
        from ..api import work_conversations
        from urllib.parse import parse_qs, urlsplit

        try:
            qs = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            cid = qs.get("cid") or ""
            if not cid:
                self._send_json({"error": "cid required"}, 400)
                return
            self._send_json(work_conversations.conversation_detail(cid))
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_requests_api(self):
        """GET /api/work/requests — raw request log (paginated)."""
        from ..api import work_conversations
        from urllib.parse import parse_qs, urlsplit

        try:
            qs = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            self._send_json(work_conversations.requests_view(qs))
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_provider_decisions_api(self):
        """GET /api/work/provider-decisions — routing decisions log."""
        from ..api import work_conversations
        from urllib.parse import parse_qs, urlsplit

        try:
            qs = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            self._send_json(work_conversations.provider_decisions_view(qs))
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _serve_work_status_api(self):
        """GET /api/work/status — the work layer's own health.

        Distinct from a view's payload: this reports which read-only sources are
        reachable and therefore which views can render. It exists so a missing
        bind mount is diagnosable from the UI instead of looking like an empty
        table.
        """
        from ..api import workspace

        try:
            self._send_json(workspace.workspace_status())
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
