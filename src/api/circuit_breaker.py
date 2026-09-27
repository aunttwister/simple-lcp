"""Circuit breaker — provider health tracking and failure management.

Providers are monitored per (provider_name, base_url, profile) tuple.
Success/failure is recorded, and providers are automatically marked
healthy → degraded → dead based on consecutive failure thresholds.
"""

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from .logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover — runtime import only for type hints
    from .component import Component
    from .runtime import Runtime
else:
    from .component import Component
    from .runtime import Runtime

logger = get_logger("lcp.circuit_breaker")

# Consecutive-failure weight per error type. Auth failures are permanent
# (a rejected key won't self-heal) so they should trip the breaker faster.
_ERROR_WEIGHTS = {
    "ProviderAuthError": 3,
    "ProviderCreditsError": 3,  # drained account won't self-heal — trip fast
    "ProviderTimeoutError": 1,
    "ProviderRateLimitError": 1,
    "ProviderInternalError": 1,
    "ProviderBadRequestError": 1,
}


class CircuitBreaker:
    """Tracks provider health with configurable thresholds."""

    def __init__(self, config):
        self._config = config
        self._health: dict = {}
        self._engine = None  # optional DB engine for failover event persistence

    def attach_engine(self, engine) -> None:
        """Attach a SQLAlchemy engine so failover events can be persisted.

        Also reloads persisted ``provider_health`` rows into memory so circuit
        breaker state survives a restart/redeploy. Because rows are only ever
        written by an actual request (or a manual override), the Health tab
        lists exactly the profiles that have been used — unused profiles stay
        hidden until their first request. Best-effort: a DB problem logs a
        warning and leaves the in-memory state untouched.
        """
        self._engine = engine
        self._load_health()

    def _load_health(self) -> None:
        """Load persisted circuit-breaker state into ``self._health``.

        ALL persisted rows are materialized (healthy included). Persisted rows
        correspond one-to-one with profiles that have actually served requests
        (or been manually overridden), so this both keeps state across restarts
        and keeps the listing request-driven — no configured-but-unused
        profiles appear.
        """
        if self._engine is None:
            return
        try:
            from .models import ProviderHealth, get_session
            with get_session(self._engine) as session:
                rows = session.query(ProviderHealth).all()
            loaded = 0
            for row in rows:
                key = (row.provider, row.base_url, row.profile)
                self._health[key] = {
                    "consecutive_failures": row.consecutive_failures or 0,
                    "last_failure": row.last_failure,
                    "last_failure_reason": row.last_failure_reason,
                    "last_success": row.last_success,
                    "status": row.status or "healthy",
                    "tripped_until": row.tripped_until,
                    "manual_override": row.manual_override,
                    "_key": key,
                }
                loaded += 1
            if loaded:
                logger.info("circuit_breaker_health_loaded", entries=loaded)
        except Exception as exc:
            logger.warning("circuit_breaker_health_load_failed", error=str(exc))

    def _persist(self, provider: str, base_url: str, profile: str) -> None:
        """Upsert a provider's in-memory health entry to the ``provider_health``
        table. Best-effort: a DB problem is logged, never raised, so the
        breaker keeps working (state just won't survive a restart).
        """
        if self._engine is None:
            return
        try:
            from .models import ProviderHealth, get_session
            h = self._health.get((provider, base_url, profile))
            if h is None:
                return
            now = datetime.now(timezone.utc).isoformat()
            with get_session(self._engine) as session:
                row = session.query(ProviderHealth).filter_by(
                    provider=provider, base_url=base_url, profile=profile,
                ).first()
                if row is None:
                    session.add(ProviderHealth(
                        provider=provider,
                        base_url=base_url,
                        profile=profile,
                        status=h["status"],
                        consecutive_failures=h["consecutive_failures"],
                        last_failure=h["last_failure"],
                        last_failure_reason=h["last_failure_reason"],
                        last_success=h["last_success"],
                        tripped_until=h["tripped_until"],
                        manual_override=h["manual_override"],
                        updated_at=now,
                    ))
                else:
                    row.status = h["status"]
                    row.consecutive_failures = h["consecutive_failures"]
                    row.last_failure = h["last_failure"]
                    row.last_failure_reason = h["last_failure_reason"]
                    row.last_success = h["last_success"]
                    row.tripped_until = h["tripped_until"]
                    row.manual_override = h["manual_override"]
                    row.updated_at = now
                session.commit()
        except Exception as exc:
            logger.warning("circuit_breaker_health_persist_failed",
                           provider=provider, profile=profile, error=str(exc))

    def _key(self, provider: str, base_url: str, profile: str) -> tuple:
        return (provider, base_url, profile)

    def _default_health(self, key: tuple) -> dict:
        """A fresh health view for a key that has never been tracked."""
        return {
            "consecutive_failures": 0,
            "last_failure": None,
            "last_failure_reason": None,
            "last_success": None,
            "status": "healthy",
            "tripped_until": None,
            "manual_override": None,  # None | 'degraded' | 'dead'
            "_key": key,  # (provider, base_url, profile) for persistence
        }

    def get_health(self, provider: str, base_url: str, profile: str) -> dict:
        """Get or create health entry for a provider+profile.

        This is the WRITE path. Anything that only reads status must use
        ``peek()`` instead: materializing on a read is how a routing
        evaluation with an empty provider invented the ``("/", url, "")``
        entry that showed up in ``/health`` as the phantom ``"/"`` key.
        """
        key = self._key(provider, base_url, profile)
        if key not in self._health:
            self._health[key] = self._default_health(key)
        return self._health[key]

    def peek(self, provider: str, base_url: str, profile: str) -> dict:
        """Read a provider's health WITHOUT creating an entry.

        Returns the live entry when the key is tracked, otherwise a detached
        default view, so callers can read fields unconditionally. The detached
        view is always ``healthy`` and is never stored, which is what keeps a
        read-only caller from adding to ``get_all_health()`` — and therefore
        from adding a key to ``/health``.
        """
        key = self._key(provider, base_url, profile)
        h = self._health.get(key)
        if h is None:
            return self._default_health(key)
        return h

    def status_of(self, provider: str, base_url: str, profile: str) -> str:
        """Return the current status string ('healthy'|'degraded'|'dead').

        Read-only: never materializes a health entry, and an empty provider is
        answered without a lookup at all (it is not an addressable provider, so
        it must never be tracked).
        """
        if not provider:
            return "healthy"
        return self.peek(provider, base_url, profile)["status"]

    def is_available(self, provider: str, base_url: str, profile: str) -> bool:
        """Check if provider is available (not tripped).

        Also implements the half-open ladder: when a cooldown expires the
        provider is promoted one level (dead → degraded → healthy) instead of
        jumping straight back to healthy. A single success during degraded
        promotes to healthy; a single failure re-trips to dead.

        Read-only for untracked keys: an unknown provider reports available and
        stays untracked, so a routing gate cannot create health state. Promotes
        only ever mutate an entry that already exists.
        """
        if not provider:
            return True
        h = self.peek(provider, base_url, profile)
        if h["status"] == "healthy":
            return True
        if h["tripped_until"] is not None and time.time() >= h["tripped_until"]:
            # Cooldown expired — promote one level and allow a probe request.
            if h["status"] == "dead":
                self._promote(h, "degraded")   # leaves tripped_until = None
            else:  # degraded → healthy after a quiet cooldown
                self._promote(h, "healthy")
            return True
        if h["status"] == "degraded" and h["tripped_until"] is None:
            # A promoted probe provider is available for its probe request.
            return True
        return False

    def _promote(self, h: dict, new_status: str) -> None:
        """Promote a provider one step up the health ladder (half-open probe)."""
        old_status = h["status"]
        h["status"] = new_status
        h["tripped_until"] = None
        # The caller only reaches here via is_available with a known key.
        self._persist(h["_key"][0], h["_key"][1], h["_key"][2])
        logger.info(
            "circuit_breaker_probe",
            old_status=old_status,
            new_status=new_status,
        )

    def record_success(self, provider: str, base_url: str, profile: str) -> None:
        """Record a successful request — resets failure count."""
        h = self.get_health(provider, base_url, profile)
        old_status = h["status"]
        h["status"] = "healthy"
        h["consecutive_failures"] = 0
        h["last_success"] = datetime.now(timezone.utc).isoformat()
        h["tripped_until"] = None
        h["last_failure_reason"] = None
        h["manual_override"] = None
        if old_status != "healthy":
            logger.info(
                "circuit_breaker_recovered",
                provider=provider,
                base_url=base_url,
                profile=profile,
                old_status=old_status,
                new_status="healthy",
            )
        self._persist(provider, base_url, profile)

    def record_failure(self, provider: str, base_url: str, profile: str,
                       error_type: str | None = None,
                       error_reason: str | None = None) -> None:
        """Record a failed request — may trip circuit breaker.

        ``error_type`` is the exception class name (e.g. 'ProviderAuthError').
        It maps to a failure weight: permanent failures (auth) trip the breaker
        faster than transient ones.

        ``error_reason`` is a human-readable description of the error
        (e.g. 'HTTP 503 Service Unavailable') stored for diagnostics.
        """
        cb_cfg = self._config.circuit_breaker
        h = self.get_health(provider, base_url, profile)
        old_status = h["status"]
        weight = _ERROR_WEIGHTS.get(error_type or "", 1)
        h["consecutive_failures"] += weight
        h["last_failure"] = datetime.now(timezone.utc).isoformat()
        if error_reason:
            h["last_failure_reason"] = error_reason
        n = h["consecutive_failures"]
        new_status = old_status
        if n >= cb_cfg["failures_dead"]:
            # A dead provider whose cooldown expired is promoted to 'degraded'
            # for a probe; its failure count is already ≥ threshold, so a single
            # probe failure naturally re-trips it to dead.
            h["status"] = "dead"
            h["tripped_until"] = time.time() + cb_cfg["dead_cooldown_seconds"]
            new_status = "dead"
        elif n >= cb_cfg["failures_degraded"]:
            h["status"] = "degraded"
            h["tripped_until"] = time.time() + cb_cfg["degraded_cooldown_seconds"]
            new_status = "degraded"
        if new_status != old_status:
            level = "error" if new_status == "dead" else "warning"
            logger_method = logger.error if level == "error" else logger.warning
            logger_method(
                "circuit_breaker_tripped",
                provider=provider,
                base_url=base_url,
                profile=profile,
                old_status=old_status,
                new_status=new_status,
                consecutive_failures=n,
                error_type=error_type,
                error_reason=error_reason,
            )
        self._persist(provider, base_url, profile)

    def get_all_health(self) -> dict:
        """Return all tracked provider health entries keyed by (provider, url, profile)."""
        return dict(self._health)

    def forget_provider(self, provider: str) -> int:
        """Drop every health entry for *provider* — in memory AND on disk.

        Called when a provider is deleted from the config. Without this the
        rows are effectively immortal: nothing else ever deletes them, and
        ``attach_engine()`` materializes ALL persisted rows at boot, so a
        deleted provider keeps being listed by ``/health`` and
        ``/api/providers/health`` forever (the "llamacpp ghost" — the provider
        was gone from the config while three rows, including a
        ``degraded/consecutive_failures=6`` one, kept feeding reports).

        Removes every ``(provider, *, profile)`` variant, since one provider can
        hold a row per base_url/profile combination. Returns the number of
        entries dropped (DB rows when the engine is attached, otherwise just the
        in-memory count). Best-effort on the DB side, matching ``_persist``: a
        DB problem is logged, never raised, so deleting a provider can't fail
        because of health bookkeeping.
        """
        in_memory = [k for k in self._health if k and k[0] == provider]
        for key in in_memory:
            self._health.pop(key, None)

        if self._engine is None:
            return len(in_memory)

        try:
            from .models import ProviderHealth, get_session
            with get_session(self._engine) as session:
                rows = session.query(ProviderHealth).filter_by(
                    provider=provider,
                ).delete(synchronize_session=False)
                session.commit()
        except Exception as exc:  # noqa: BLE001 — best-effort, see docstring
            logger.warning("circuit_breaker_health_forget_failed",
                           provider=provider, error=str(exc))
            return len(in_memory)

        if rows or in_memory:
            logger.info("circuit_breaker_health_forgotten",
                        provider=provider, rows=rows or 0,
                        in_memory=len(in_memory))
        return max(rows or 0, len(in_memory))

    def reset(self, provider: str, base_url: str, profile: str) -> None:
        """Force-reset a provider back to healthy, clearing failures and cooldown."""
        h = self.get_health(provider, base_url, profile)
        h["status"] = "healthy"
        h["consecutive_failures"] = 0
        h["last_failure"] = None
        h["last_failure_reason"] = None
        h["tripped_until"] = None
        h["manual_override"] = None
        self._persist(provider, base_url, profile)
        logger.info(
            "circuit_breaker_reset",
            provider=provider,
            base_url=base_url,
            profile=profile,
        )

    def force_status(self, provider: str, base_url: str, profile: str,
                     action: str) -> str:
        """Manually force a provider into a circuit-breaker state.

        ``action`` is one of:
          - 'degrade' — force degraded with the configured degraded cooldown
          - 'kill'    — force dead indefinitely (manual resume required)
          - 'resume'  — force back to healthy, clear failures + cooldown

        Returns the resulting status string.
        """
        h = self.get_health(provider, base_url, profile)
        cb_cfg = self._config.circuit_breaker
        if action == "degrade":
            h["status"] = "degraded"
            h["tripped_until"] = time.time() + cb_cfg["degraded_cooldown_seconds"]
            h["manual_override"] = "degraded"
            logger.info("circuit_breaker_manual_degrade",
                        provider=provider, base_url=base_url, profile=profile,
                        cooldown_seconds=cb_cfg["degraded_cooldown_seconds"])
        elif action == "kill":
            h["status"] = "dead"
            h["tripped_until"] = None  # indefinite — no auto-promotion
            h["manual_override"] = "dead"
            logger.info("circuit_breaker_manual_kill",
                        provider=provider, base_url=base_url, profile=profile)
        elif action == "resume":
            h["status"] = "healthy"
            h["consecutive_failures"] = 0
            h["last_failure"] = None
            h["last_failure_reason"] = None
            h["tripped_until"] = None
            h["manual_override"] = None
            logger.info("circuit_breaker_manual_resume",
                        provider=provider, base_url=base_url, profile=profile)
        else:
            raise ValueError(f"unknown circuit breaker action: {action}")
        self._persist(provider, base_url, profile)
        return h["status"]

    def record_failover(self, profile: str, from_provider: str, to_provider: str,
                        reason: str, error_message: str | None = None,
                        request_id: int | None = None) -> None:
        """Persist a failover event (chain fallback) to the database.

        Best-effort: failures to persist are logged, never raised, so a DB
        problem can't break request handling.
        """
        if self._engine is None:
            return
        try:
            from .models import FailoverEvent, get_session
            with get_session(self._engine) as session:
                session.add(FailoverEvent(
                    profile=profile,
                    from_provider=from_provider,
                    to_provider=to_provider,
                    reason=reason,
                    error_message=error_message,
                    request_id=request_id,
                ))
                session.commit()
        except Exception as exc:
            logger.warning("failover_persist_failed",
                           profile=profile, from_provider=from_provider,
                           to_provider=to_provider, error=str(exc))

    @property
    def stats(self) -> dict:
        """Return summary statistics across all tracked providers."""
        return {
            "total": len(self._health),
            "healthy": sum(1 for h in self._health.values() if h["status"] == "healthy"),
            "degraded": sum(1 for h in self._health.values() if h["status"] == "degraded"),
            "dead": sum(1 for h in self._health.values() if h["status"] == "dead"),
        }


# ── Module-level singleton ────────────────────────────────────────────────
_circuit_breaker: CircuitBreaker | None = None


def get_circuit_breaker(config=None) -> CircuitBreaker:
    """Get the active circuit breaker.

    When a Runtime is bound and its ``circuit_breaker`` component is active,
    returns the runtime's breaker (config + engine injected at construction —
    no attach_engine post-hoc step). Otherwise returns the legacy singleton
    (lazy-created with *config*), preserving the boot/tests path until main.py
    is rewired to the runtime.
    """
    from .runtime import get_runtime
    rt = get_runtime()
    if rt is not None:
        try:
            comp = rt.resolve("circuit_breaker")
        except Exception:  # noqa: BLE001 — inactive/unbound → legacy
            comp = None
        if comp is not None and getattr(comp, "breaker", None) is not None:
            return comp.breaker
    global _circuit_breaker
    if _circuit_breaker is None and config is not None:
        _circuit_breaker = CircuitBreaker(config)
    if _circuit_breaker is None:
        raise RuntimeError("CircuitBreaker not initialized — call with config first")
    return _circuit_breaker


# ── Component-runtime adapter (Phase C) ────────────────────────────────────
# When an active Runtime is bound, get_circuit_breaker() delegates to the
# runtime's CircuitBreakerComponent, which constructs the breaker with BOTH
# config and engine injected up front — eliminating the post-hoc
# attach_engine() call. The engine attach also reloads persisted health.


def bind_runtime(rt: "Runtime") -> None:
    """Bind an active Runtime so ``get_circuit_breaker()`` delegates to it."""
    from .runtime import bind_active_runtime
    bind_active_runtime(rt)


class CircuitBreakerComponent(Component):
    """The circuit breaker as a runtime component.

    ``requires=["config", "engine"]`` — both deps declared, injected at
    construction. ``setup`` creates the breaker, attaches the engine (which
    loads persisted provider health), and returns a no-op disposer (the breaker
    persists per-event, so there is nothing to undo on shutdown).
    """

    name = "circuit_breaker"
    requires = ["config", "engine"]
    provides = ["circuit_breaker"]

    def __init__(self) -> None:
        super().__init__()
        self.breaker: Optional[CircuitBreaker] = None

    @property
    def service(self) -> Optional[CircuitBreaker]:
        return self.breaker

    def setup(self, rt: "Runtime") -> Optional[Any]:
        breaker = CircuitBreaker(rt.resolve("config"))
        breaker.attach_engine(rt.resolve("engine"))
        self.breaker = breaker
        return None
