"""Active health assessment for (provider, base_url, profile) pairs.

WHY THIS EXISTS
---------------
Before this module, provider health was written by **nothing but real traffic**:
``request_pipeline`` calls ``circuit_breaker.record_success/record_failure`` as a
side effect of serving a request. Nothing ever probed a provider, so a pair that
is configured in a chain but happens to be idle is never assessed at all — it sits
at ``healthy`` with ``uptime_24h: 100%`` because it has never carried a request to
fail. Measured on prod 2026-11-01: 16 of 20 health rows had a ``last_success``
older than an hour, three of them older than 58 days, and every single row read
``uptime_24h: 100%``. A provider that is down and a provider nobody calls were
indistinguishable.

That is a monitoring surface reporting "fine" about something it never checked —
the same class of defect as showing a five-day-old balance as a current ``$0.00``.

WHAT IT DOES
------------
A daemon thread walks every pair registered in a configured profile's chain and,
when a pair has not been assessed within the interval, sends one minimal
completion to that provider and records the outcome through the SAME circuit
breaker the request path uses. One source of truth: the existing breaker floors,
cooldowns, transition alerts and the health matrix all apply unchanged.

HAZARDS IT MUST RESPECT (each one verified, not assumed)
--------------------------------------------------------
1. ``record_success`` sets ``manual_override = None``. Probing a pair an operator
   has manually killed or degraded would silently undo their override, so pairs
   carrying a ``manual_override`` are SKIPPED. Silent override reversal is worse
   than a stale reading.
2. A provider that requires a key and has none stored would fail every probe and
   be marked dead by the prober itself — a self-inflicted outage report. Skipped
   unless the provider is known to be keyless (e.g. a local engine).
3. A 4xx with a body is the provider ANSWERING, not failing. Only transport
   errors and 5xx count as unreachable; auth/credit responses are recorded as
   failures because they mean this pair cannot serve, which is a real fault.
4. Probing costs a real API call on paid providers. The prompt is one token and
   the interval is bounded from below, and the whole thing is switchable off.
5. A failure recorded here must be attributed to the pair, never the provider
   globally: health is keyed (provider, base_url, profile).
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

SETTINGS_KEY = "gateway_config:health_probe"

# Defaults. Health used to be written ONLY by real traffic, so an idle provider
# was never assessed at all. 5 minutes is the default: measured on prod
# 2026-11-01, only 5 of 20 tracked pairs carried traffic in 24 h, so probing all
# of them every 60 s would add ~7x the estate's real request volume in paid
# calls. 15 s is the floor — each probe is a billed request on a paid provider.
DEFAULT_INTERVAL_SECONDS = 300
MIN_INTERVAL_SECONDS = 15
DEFAULT_TIMEOUT_SECONDS = 10

# Providers that legitimately need no credential (local engines). Anything not
# listed here and lacking a stored key is skipped rather than probed.
KEYLESS_ALLOWED = {"llamacpp", "local-sparks", "local-llm", "ollama"}

_PROBE_PROMPT = "hi"
_PROBE_MAX_TOKENS = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_probe_config(settings: Any = None) -> dict:
    """Probe settings from the gateway config, falling back to defaults.

    ``settings`` may be a store OR a callable returning one. A callable is the
    safe form: this module is constructed at boot, before the runtime binds its
    components, and ``SettingsStore`` caches per INSTANCE — so holding a store
    built at boot means an operator's change saved through the app's store is
    never seen here. Measured 2026-11-01: the panel saved interval 300 (in the
    DB) while the running prober kept reading 60 forever.

    Shape of ``gateway_config:health_probe``::

        {"enabled": true, "interval_seconds": 60, "timeout_seconds": 10}

    An explicit ``enabled: false`` disables probing; a missing row means the
    feature is ON at the default interval, because the whole point is that
    health should be assessed without a human enabling it.
    """
    if callable(settings):
        try:
            settings = settings()
        except Exception:  # noqa: BLE001 - fall back to defaults
            settings = None
    cfg = {"enabled": True,
           "interval_seconds": DEFAULT_INTERVAL_SECONDS,
           "timeout_seconds": DEFAULT_TIMEOUT_SECONDS}
    raw = None
    if settings is not None:
        try:
            raw = settings.get(SETTINGS_KEY)
        except Exception:  # noqa: BLE001 - a broken store must not stop the app
            raw = None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            raw = None
    if isinstance(raw, dict):
        for k in ("enabled", "interval_seconds", "timeout_seconds"):
            if k in raw:
                cfg[k] = raw[k]
    try:
        cfg["interval_seconds"] = max(MIN_INTERVAL_SECONDS, int(cfg["interval_seconds"]))
    except (TypeError, ValueError):
        cfg["interval_seconds"] = DEFAULT_INTERVAL_SECONDS
    try:
        cfg["timeout_seconds"] = max(1, int(cfg["timeout_seconds"]))
    except (TypeError, ValueError):
        cfg["timeout_seconds"] = DEFAULT_TIMEOUT_SECONDS
    cfg["enabled"] = bool(cfg["enabled"])
    return cfg


def _targets_from_config(config: Any) -> list[dict]:
    """Every (provider, base_url, profile) a configured chain can call.

    Read from the profile chains, not from the provider list: a provider is only
    reachable for a profile if a step names it, and only that pair can be
    assessed meaningfully. ``base_url`` follows the same resolution the request
    path uses — the step's own override first, then the provider default.
    """
    out: dict[tuple[str, str, str], dict] = {}
    profiles = getattr(config, "profiles", {}) or {}
    providers = getattr(config, "providers", {}) or {}
    for profile, pcfg in profiles.items():
        if not isinstance(pcfg, dict):
            continue
        for step in (pcfg.get("chain") or []):
            if not isinstance(step, dict):
                continue
            prov = step.get("provider")
            if not prov:
                continue
            base = (step.get("base_url")
                    or (providers.get(prov) or {}).get("api_base", "") or "").rstrip("/")
            if not base:
                continue
            out[(prov, base, profile)] = {"provider": prov, "base_url": base, "profile": profile,
                                          "model": step.get("model") or ""}
    return list(out.values())


def probe_once_targets(config: Any) -> list[dict]:
    """Public helper so the API can report exactly what would be probed."""
    return _targets_from_config(config)

# A 4xx whose body states a provider-SIDE condition (no funds, model gone, key
# rejected) means this pair cannot serve, even though the endpoint answered.
# Without this, opencode answering "Model is unavailable" for a chain step that
# still names that model reads as "reachable" — a monitor reporting health about
# a broken configuration. These are BODY STRINGS, i.e. a heuristic: a vendor that
# rephrases its errors will read as reachable again, so the raw body is always
# kept in the detail for a human to judge.
_PROVIDER_FAULT_HINTS = (
    "insufficient account funds", "insufficient funds", "insufficient credit",
    "out of credits", "no credit", "billing", "quota",
    "model is unavailable", "model not found", "model_not_found",
    "does not exist", "no such model", "unsupported model",
    "invalid api key", "incorrect api key", "unauthorized", "authentication",
    "invalid_api_key", "account is not active", "entitlement",
)


def _body_is_provider_fault(body: str) -> bool:
    low = (body or "").lower()
    return any(h in low for h in _PROVIDER_FAULT_HINTS)


class HealthProber:
    """Probe configured (provider, base_url, profile) pairs on an interval.

    Records through the shared circuit breaker, so the breaker's floors,
    cooldowns and transition alerts all apply to a probe exactly as they do to a
    real request. Nothing here restates a threshold.
    """

    def __init__(self, config: Any, settings: Any = None, breaker: Any = None,
                 credential_store: Any = None):
        self._config = config
        self._settings = settings
        self._breaker = breaker
        self._creds = credential_store

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_checked: dict[tuple[str, str, str], float] = {}
        self._last_result: dict[tuple[str, str, str], dict] = {}
        self._skipped: dict[tuple[str, str, str], str] = {}
        self._covered: dict[tuple[str, str, str], bool] = {}
        self._cycles = 0
        self._last_cycle_at: Optional[str] = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        if not load_probe_config(self._settings)["enabled"]:
            logger.info("health_prober_disabled")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="health-prober", daemon=True)
        self._thread.start()
        logger.info("health_prober_started",
                    interval=load_probe_config(self._settings)["interval_seconds"])

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def probe_now(self) -> list:
        """Run one full cycle synchronously (used by the API's \"Check now\")."""
        return self._cycle(force=True)

    def status(self) -> dict:
        cfg = load_probe_config(self._settings)
        # Report WHERE the cadence came from. An operator whose saved value is
        # not taking effect needs to see "defaults (no settings store)" rather
        # than a number with no provenance — this exact gap hid a stale store
        # reference while the DB already held the new value.
        try:
            store = self._settings() if callable(self._settings) else self._settings
        except Exception:  # noqa: BLE001
            store = None
        raw = None
        if store is not None:
            try:
                raw = store.get(SETTINGS_KEY)
            except Exception:  # noqa: BLE001
                raw = None
        with self._lock:
            return {
                "enabled": cfg["enabled"],
                "interval_seconds": cfg["interval_seconds"],
                "timeout_seconds": cfg["timeout_seconds"],
                "settings_source": ("store" if store is not None else "defaults (no settings store)"),
                "saved_row": raw,
                "running": self._thread is not None and self._thread.is_alive(),
                "cycles": self._cycles,
                "last_cycle_at": self._last_cycle_at,
                "tracked": len(self._targets()),
                "results": {"/".join(k): v for k, v in self._last_result.items()},
                "skipped": {"/".join(k): v for k, v in self._skipped.items()},
                "covered_by_traffic": len(self._covered),
            }

    # ── the loop ───────────────────────────────────────────────────────────

    def _run(self) -> None:
        # Probe shortly after boot rather than after a full interval: the first
        # minutes are exactly when a silently-dead provider should surface.
        self._wake.wait(5.0)
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception:  # noqa: BLE001 - the prober must never die
                logger.exception("health_prober_cycle_failed")
            interval = load_probe_config(self._settings)["interval_seconds"]
            self._wake.wait(interval)
            self._wake.clear()

    def _targets(self) -> list:
        return _targets_from_config(self._config)

    def _cycle(self, force: bool = False) -> list:
        cfg = load_probe_config(self._settings)
        if not cfg["enabled"] and not force:
            return []
        now = time.monotonic()
        due = []
        for t in self._targets():
            key = (t["provider"], t["base_url"], t["profile"])
            last = self._last_checked.get(key)
            if force or last is None or (now - last) >= cfg["interval_seconds"]:
                due.append(t)

        results = []
        for t in due:
            key = (t["provider"], t["base_url"], t["profile"])
            skip = self._skip_reason(t)
            if skip:
                with self._lock:
                    self._skipped[key] = skip
                results.append({"provider": t["provider"], "profile": t["profile"],
                                "base_url": t["base_url"], "skipped": skip})
                continue
            # Not a "skip" (nothing is wrong) and not a probe: traffic already
            # measured this pair within the interval. Reported separately so the
            # UI can say "covered by real traffic" instead of listing healthy
            # pairs as if they needed attention.
            if not force and self._covered_by_traffic(t, cfg):
                with self._lock:
                    self._covered[key] = True
                results.append({"provider": t["provider"], "profile": t["profile"],
                                "base_url": t["base_url"],
                                "covered_by_traffic": True})
                continue
            res = self._probe(t, cfg["timeout_seconds"])
            with self._lock:
                self._last_checked[key] = time.monotonic()
                self._last_result[key] = res
                self._skipped.pop(key, None)
                self._covered.pop(key, None)
            results.append(res)

        with self._lock:
            self._cycles += 1
            self._last_cycle_at = _now()
        logger.info("health_probe_cycle", probed=len(due), results=len(results))
        return results

    # ── skip rules (hazards 1 and 2) ───────────────────────────────────────

    def _skip_reason(self, t: dict) -> str:
        """Return why this pair must NOT be probed, or '' to proceed.

        HAZARD 7 (below) is the expensive one: a probe is a BILLED request, and
        the pairs that most need probing are exactly the ones nothing calls.
        """
        if self._breaker is None:
            return "no circuit breaker"
        # peek(), never get_health(): get_health() CREATES a health entry, and a
        # read-only assessor materializing rows is how this codebase once grew a
        # phantom ("/", url, "") entry in /health. Reading must not write.
        reader = getattr(self._breaker, "peek", None)
        try:
            h = reader(t["provider"], t["base_url"], t["profile"]) if reader \
                else self._breaker.get_health(t["provider"], t["base_url"], t["profile"])
        except Exception as exc:  # noqa: BLE001
            return f"health unreadable: {exc}"
        # HAZARD 1: record_success clears manual_override. A probe must never
        # silently undo an operator's kill/degrade.
        if h.get("manual_override"):
            return f"manual override ({h['manual_override']}) — not probed"
        # HAZARD 7: a pair already tripped into a cooldown is in a KNOWN state
        # with a deadline. The breaker half-opens it on its own ladder; probing
        # it meanwhile only spends money to re-confirm what is recorded, and
        # fights the cooldown by resetting it.
        tripped_until = h.get("tripped_until")
        if tripped_until and time.time() < tripped_until:
            return "in cooldown — breaker will half-open on its own"
        # HAZARD 2: no credential where one is required would make the prober
        # itself declare a healthy provider dead.
        if t["provider"] not in KEYLESS_ALLOWED:
            if self._creds is None:
                return "no credential store"
            try:
                if not (self._creds.get(t["provider"]) or ""):
                    return "no stored credential — not probed"
            except Exception:  # noqa: BLE001
                return "credential lookup failed"
        return ""

    def _covered_by_traffic(self, t: dict, cfg: dict) -> bool:
        """True when real traffic assessed this pair within one interval.

        HAZARD 7. The prober exists to cover pairs that traffic does NOT reach;
        for a pair that is being called anyway (l2/commandcode ran 3,148 requests
        in 24 h), the request path already writes the health row for free, so
        probing it adds cost and zero information.

        max(last_success, last_failure) is the last time anything touched the
        pair. This includes the prober's own probe, which is harmless: the
        threshold IS the interval, so the cadence is preserved either way.
        """
        if self._breaker is None:
            return False
        reader = getattr(self._breaker, "peek", None)
        try:
            h = reader(t["provider"], t["base_url"], t["profile"]) if reader \
                else self._breaker.get_health(t["provider"], t["base_url"], t["profile"])
        except Exception:  # noqa: BLE001
            return False
        stamps = []
        for field in ("last_success", "last_failure"):
            raw = h.get(field)
            if not raw:
                continue
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                stamps.append(dt.timestamp())
            except Exception:  # noqa: BLE001
                continue
        if not stamps:
            return False
        return (time.time() - max(stamps)) < cfg["interval_seconds"]

    # ── the probe itself (hazard 3) ────────────────────────────────────────

    def _probe(self, t: dict, timeout: int) -> dict:
        from ..server.endpoints import (  # local import: avoid a cycle at import time
            _browser_headers,
            _validate_api_base_destination,
        )

        base, prov, prof = t["base_url"], t["provider"], t["profile"]
        out = {"provider": prov, "profile": prof, "base_url": base,
               "at": _now(), "ok": False, "detail": ""}

        reason = _validate_api_base_destination(base)
        if reason:
            # HAZARD 6: the SSRF guard blocks RFC1918 unless allowlisted. A
            # blocked destination is NOT a provider failure — the prober simply
            # cannot assess it. Recording a failure here would mark the estate's
            # own local engine dead the moment the allowlist changed. Skipped and
            # said out loud, like any other n/a.
            out["detail"] = f"not probed: destination blocked by SSRF guard ({reason})"
            out["skipped"] = "blocked by SSRF guard"
            return out

        key = ""
        if self._creds is not None:
            try:
                key = self._creds.get(prov) or ""
            except Exception:  # noqa: BLE001
                key = ""

        model = t.get("model") or ""
        # Claude models answer 403 on /chat/completions and only speak the
        # Anthropic Messages API, so a Claude step would otherwise be reported
        # dead by this prober even when it works. Select the wire the same way
        # the proxy does.
        from ..api.wire_anthropic import (
            ANTHROPIC_API_VERSION,
            ANTHROPIC_MESSAGES_PATH,
            is_anthropic_model,
            to_anthropic,
        )
        anthropic_wire = is_anthropic_model(model)
        if anthropic_wire:
            body = json.dumps(to_anthropic({
                "model": model,
                "messages": [{"role": "user", "content": _PROBE_PROMPT}],
                # _PROBE_MAX_TOKENS is 1: enough to prove reachability on the
                # OpenAI wire, but Claude emits a `thinking` block first and can
                # return 200 with no text at all. The probe only reads the status,
                # so a slightly larger budget is still cheap and less surprising.
                "max_tokens": 64,
            }, model)).encode()
            headers = _browser_headers(base)
            headers["anthropic-version"] = ANTHROPIC_API_VERSION
            if key:
                headers["x-api-key"] = key
            url = f"{base}{ANTHROPIC_MESSAGES_PATH}"
        else:
            body = json.dumps({"model": model,
                               "messages": [{"role": "user", "content": _PROBE_PROMPT}],
                               "max_tokens": _PROBE_MAX_TOKENS}).encode()
            headers = _browser_headers(base)
            if key:
                headers["Authorization"] = f"Bearer {key}"
            url = f"{base}/chat/completions"
        req = urllib.request.Request(url, data=body, headers=headers)

        start = time.monotonic()
        status_code = None
        try:
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                status_code = getattr(resp, "status", 200)
                resp.read()
            out["ok"] = True
            out["detail"] = f"HTTP {status_code}"
        except urllib.error.HTTPError as e:
            status_code = e.code
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            # HAZARD 3: a 4xx body is the provider answering. 5xx is the
            # provider failing. Both are recorded distinctly.
            if 500 <= e.code:
                out["ok"] = False
                out["detail"] = f"HTTP {e.code} {detail}".strip()
            elif e.code in (401, 402, 403, 429) or _body_is_provider_fault(detail):
                # Cannot serve this pair: bad key, no funds, rate limited, or a
                # model the chain still names but the vendor no longer serves.
                out["ok"] = False
                out["detail"] = f"HTTP {e.code} {detail}".strip()
            else:
                # 400/404/405: reachable, our probe shape is simply not what it
                # wants. Counts as reachable, never as a provider failure.
                out["ok"] = True
                out["detail"] = f"HTTP {e.code} (reachable) {detail}".strip()
        except Exception as exc:  # noqa: BLE001 - transport/SSL/DNS
            out["ok"] = False
            out["detail"] = f"{type(exc).__name__}: {exc}"[:200]

        out["latency_ms"] = round((time.monotonic() - start) * 1000)
        self._record(t, out)
        return out

    def _record(self, t: dict, out: dict) -> None:
        """Feed the probe into the shared breaker, attributed to this pair.

        A skipped probe (blocked destination) records NOTHING: the breaker must
        not learn a fault from an assessment that never happened.
        """
        if out.get("skipped"):
            return
        if self._breaker is None:
            return
        try:
            if out["ok"]:
                self._breaker.record_success(t["provider"], t["base_url"], t["profile"])
            else:
                self._breaker.record_failure(
                    t["provider"], t["base_url"], t["profile"],
                    error_type="ProviderConnectionError",
                    error_reason=f"health probe: {out.get('detail', '')}",
                )
        except Exception:  # noqa: BLE001
            logger.exception("health_prober_record_failed",
                             provider=t["provider"], profile=t["profile"])


# ── singleton ──────────────────────────────────────────────────────────────
# Same shape as get_circuit_breaker(): one prober per process, built on first
# use. main.py starts it; endpoints read its status.

_PROBER: Optional[HealthProber] = None
_PROBER_LOCK = threading.Lock()


def get_health_prober(config: Any = None, settings: Any = None, breaker: Any = None,
                     credential_store: Any = None) -> HealthProber:
    global _PROBER
    with _PROBER_LOCK:
        if _PROBER is None:
            _PROBER = HealthProber(config, settings, breaker, credential_store)
        return _PROBER
