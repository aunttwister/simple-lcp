"""Tests for circuit_breaker.py — health checks, state transitions, cooldowns."""

import time
from unittest.mock import MagicMock, patch

import pytest
from src.api.circuit_breaker import CircuitBreaker, get_circuit_breaker


@pytest.fixture
def cb():
    """Fresh CircuitBreaker with mock config."""
    cfg = MagicMock()
    cfg.circuit_breaker = {
        "failures_degraded": 3,
        "failures_dead": 6,
        "degraded_cooldown_seconds": 2,
        "dead_cooldown_seconds": 5,
    }
    cfg.profiles = {}
    cfg.providers = {}
    return CircuitBreaker(cfg)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the circuit breaker singleton between tests."""
    import src.api.circuit_breaker as cb_module
    cb_module._circuit_breaker = None


class TestHealthTracking:
    def test_initial_state_healthy(self, cb):
        h = cb.get_health("test_prov", "https://test/v1", "l2")
        assert h["status"] == "healthy"
        assert h["consecutive_failures"] == 0

    def test_success_resets_failures(self, cb):
        # Record some failures first
        cb.record_failure("test_prov", "https://test/v1", "l2")
        cb.record_failure("test_prov", "https://test/v1", "l2")
        # Then record success
        cb.record_success("test_prov", "https://test/v1", "l2")
        h = cb.get_health("test_prov", "https://test/v1", "l2")
        assert h["consecutive_failures"] == 0
        assert h["status"] == "healthy"

    def test_failure_increments_count(self, cb):
        cb.record_failure("a", "https://x", "p1")
        h = cb.get_health("a", "https://x", "p1")
        assert h["consecutive_failures"] == 1
        assert h["last_failure"] is not None

    def test_different_providers_independent(self, cb):
        cb.record_failure("a", "https://x", "p1")
        cb.record_failure("a", "https://x", "p1")
        h2 = cb.get_health("b", "https://y", "p2")
        assert h2["consecutive_failures"] == 0


class TestDegradedState:
    def test_degraded_after_threshold(self, cb):
        for _ in range(3):
            cb.record_failure("p", "https://x", "l2")
        h = cb.get_health("p", "https://x", "l2")
        assert h["status"] == "degraded"
        assert h["tripped_until"] is not None

    def test_degraded_resets_after_cooldown(self, cb):
        for _ in range(3):
            cb.record_failure("q", "https://x", "l2")
        # Fast-forward time
        with patch("time.time") as mock_time:
            mock_time.return_value = time.time() + 10  # past cooldown
            # Next success should reset
            cb.record_success("q", "https://x", "l2")
            h = cb.get_health("q", "https://x", "l2")
            assert h["status"] == "healthy"


class TestDeadState:
    def test_dead_after_dead_threshold(self, cb):
        for _ in range(6):
            cb.record_failure("d", "https://x", "l2")
        h = cb.get_health("d", "https://x", "l2")
        assert h["status"] == "dead"

    def test_dead_provider_not_available(self, cb):
        for _ in range(6):
            cb.record_failure("dead_prov", "https://x", "l2")
        assert cb.is_available("dead_prov", "https://x", "l2") is False

    def test_healthy_provider_is_available(self, cb):
        assert cb.is_available("healthy", "https://x", "l2") is True

    def test_available_after_cooldown_expired(self, cb):
        """is_available returns True when tripped_until is in the past (line 45)."""
        for _ in range(6):
            cb.record_failure("p", "https://x", "l2")
        assert cb.is_available("p", "https://x", "l2") is False  # tripped
        # Fast-forward past the cooldown — save real time before mocking
        real_now = time.time()
        with patch("time.time") as mock_time:
            mock_time.return_value = real_now + 60  # well past cooldown
            assert cb.is_available("p", "https://x", "l2") is True  # cooldown expired


class TestStats:
    def test_stats_counts(self, cb):
        cb.record_failure("h", "https://x", "l2")
        cb.record_failure("h", "https://x", "l2")
        cb.record_failure("h", "https://x", "l2")
        cb.record_failure("h", "https://x", "l2")  # still degraded
        s = cb.stats
        assert s["total"] == 1
        assert s["healthy"] == 0
        assert s["degraded"] == 1
        assert s["dead"] == 0

    def test_stats_all_states(self, cb):
        cb.record_failure("h1", "https://x", "l2")
        cb.record_failure("h1", "https://x", "l2")
        cb.record_failure("h1", "https://x", "l2")
        cb.record_failure("h1", "https://x", "l2")  # degraded
        for _ in range(6):
            cb.record_failure("dead-prov", "https://y", "l2")
        # Also have a healthy one. A READ must not create it (status_of /
        # is_available are non-materializing since the phantom-key fix), so the
        # entry is created the way production creates one: by a real request.
        assert cb.is_available("healthy-prov", "https://z", "l2")
        assert cb.stats["total"] == 2, "a read created a health entry"
        cb.record_success("healthy-prov", "https://z", "l2")
        s = cb.stats
        assert s["total"] == 3
        assert s["healthy"] == 1
        assert s["degraded"] == 1
        assert s["dead"] == 1


class TestGetAllHealth:
    def test_get_circuit_breaker_requires_config(self):
        import src.api.circuit_breaker as cb_module
        cb_module._circuit_breaker = None
        with pytest.raises(RuntimeError, match="not initialized"):
            get_circuit_breaker()

    def test_get_circuit_breaker_returns_same(self):
        cfg = MagicMock()
        cfg.circuit_breaker = {"failures_degraded": 2, "failures_dead": 4, "degraded_cooldown_seconds": 10, "dead_cooldown_seconds": 30}
        cb1 = get_circuit_breaker(cfg)
        cb2 = get_circuit_breaker()
        assert cb1 is cb2


class TestFailureWeighting:
    """Auth failures (weight 3) trip the breaker faster than transient errors."""

    def test_auth_failures_trip_dead_with_fewer_attempts(self, cb):
        # 2 auth failures × weight 3 = 6 >= failures_dead(6) → dead in 2 calls
        cb.record_failure("w", "https://x", "l2", error_type="ProviderAuthError")
        cb.record_failure("w", "https://x", "l2", error_type="ProviderAuthError")
        assert cb.status_of("w", "https://x", "l2") == "dead"

    def test_transient_failures_need_full_threshold(self, cb):
        # 2 transient failures × weight 1 = 2 < failures_degraded(3) → healthy
        cb.record_failure("w", "https://x", "l2", error_type="ProviderTimeoutError")
        cb.record_failure("w", "https://x", "l2", error_type="ProviderInternalError")
        assert cb.status_of("w", "https://x", "l2") == "healthy"

    def test_mixed_weights_accumulate(self, cb):
        # 1 auth (3) + 1 internal (1) = 4 >= failures_degraded(3) → degraded
        cb.record_failure("w", "https://x", "l2", error_type="ProviderAuthError")
        cb.record_failure("w", "https://x", "l2", error_type="ProviderInternalError")
        assert cb.status_of("w", "https://x", "l2") == "degraded"

    def test_unknown_error_type_uses_weight_one(self, cb):
        cb.record_failure("w", "https://x", "l2", error_type="SomeWeirdError")
        assert cb.status_of("w", "https://x", "l2") == "healthy"
        assert cb.get_health("w", "https://x", "l2")["consecutive_failures"] == 1

    def test_credits_failures_trip_dead_with_fewer_attempts(self, cb):
        # 2 credits failures × weight 3 = 6 >= failures_dead(6) → dead in 2 calls
        cb.record_failure("w", "https://x", "l2", error_type="ProviderCreditsError")
        cb.record_failure("w", "https://x", "l2", error_type="ProviderCreditsError")
        assert cb.status_of("w", "https://x", "l2") == "dead"


class TestHalfOpenLadder:
    """Dead → degraded (probe) → healthy on success, dead again on failure."""

    def _drive_to_dead(self, cb, provider="probe"):
        for _ in range(6):
            cb.record_failure(provider, "https://x", "l2")
        assert cb.status_of(provider, "https://x", "l2") == "dead"

    def test_dead_promotes_to_degraded_after_cooldown(self, cb):
        self._drive_to_dead(cb)
        real_now = time.time()
        with patch("time.time", return_value=real_now + 60):
            assert cb.is_available("probe", "https://x", "l2") is True
        assert cb.status_of("probe", "https://x", "l2") == "degraded"

    def test_degraded_probe_success_promotes_to_healthy(self, cb):
        self._drive_to_dead(cb)
        real_now = time.time()
        with patch("time.time", return_value=real_now + 60):
            cb.is_available("probe", "https://x", "l2")  # → degraded
        cb.record_success("probe", "https://x", "l2")
        assert cb.status_of("probe", "https://x", "l2") == "healthy"
        assert cb.get_health("probe", "https://x", "l2")["consecutive_failures"] == 0

    def test_degraded_probe_failure_retrips_to_dead(self, cb):
        self._drive_to_dead(cb)
        real_now = time.time()
        with patch("time.time", return_value=real_now + 60):
            cb.is_available("probe", "https://x", "l2")  # → degraded
        cb.record_failure("probe", "https://x", "l2", error_type="ProviderInternalError")
        assert cb.status_of("probe", "https://x", "l2") == "dead"

    def test_degraded_probe_remains_available_on_repeated_checks(self, cb):
        """A promoted probe provider stays available across is_available calls."""
        self._drive_to_dead(cb)
        real_now = time.time()
        with patch("time.time", return_value=real_now + 60):
            assert cb.is_available("probe", "https://x", "l2") is True
            assert cb.is_available("probe", "https://x", "l2") is True
        assert cb.status_of("probe", "https://x", "l2") == "degraded"

    def test_degraded_cooldown_expiry_promotes_to_healthy(self, cb):
        # 3 failures → degraded (cooldown 2s). Past cooldown → healthy.
        for _ in range(3):
            cb.record_failure("up", "https://x", "l2")
        assert cb.status_of("up", "https://x", "l2") == "degraded"
        real_now = time.time()
        with patch("time.time", return_value=real_now + 10):
            assert cb.is_available("up", "https://x", "l2") is True
        assert cb.status_of("up", "https://x", "l2") == "healthy"


class TestLastFailureReason:
    """Storing and clearing the last failure reason for diagnostics."""

    def test_initial_state_no_reason(self, cb):
        h = cb.get_health("test_prov", "https://test/v1", "l2")
        assert h["last_failure_reason"] is None

    def test_failure_stores_reason(self, cb):
        cb.record_failure("p", "https://x", "l2",
                          error_type="ProviderInternalError",
                          error_reason="HTTP 503 Service Unavailable")
        h = cb.get_health("p", "https://x", "l2")
        assert h["last_failure_reason"] == "HTTP 503 Service Unavailable"

    def test_success_clears_reason(self, cb):
        cb.record_failure("p", "https://x", "l2",
                          error_type="ProviderInternalError",
                          error_reason="HTTP 503 Service Unavailable")
        cb.record_success("p", "https://x", "l2")
        h = cb.get_health("p", "https://x", "l2")
        assert h["last_failure_reason"] is None

    def test_reason_not_required(self, cb):
        """error_reason is optional — omitting it keeps last_failure_reason as None."""
        cb.record_failure("p", "https://x", "l2", error_type="ProviderTimeoutError")
        h = cb.get_health("p", "https://x", "l2")
        assert h["last_failure_reason"] is None

    def test_last_reason_overwrites_previous(self, cb):
        cb.record_failure("p", "https://x", "l2",
                          error_type="ProviderTimeoutError",
                          error_reason="timeout")
        cb.record_failure("p", "https://x", "l2",
                          error_type="ProviderInternalError",
                          error_reason="HTTP 503 Service Unavailable")
        h = cb.get_health("p", "https://x", "l2")
        assert h["last_failure_reason"] == "HTTP 503 Service Unavailable"

    def test_get_all_health_includes_reason(self, cb):
        cb.record_failure("p", "https://x", "l2",
                          error_type="ProviderInternalError",
                          error_reason="HTTP 502 Bad Gateway")
        all_h = cb.get_all_health()
        key = ("p", "https://x", "l2")
        assert key in all_h
        assert all_h[key]["last_failure_reason"] == "HTTP 502 Bad Gateway"


class TestReset:
    """Force-resetting a provider back to healthy."""

    def test_reset_clears_failures_and_status(self, cb):
        for _ in range(6):
            cb.record_failure("p", "https://x", "l2",
                              error_type="ProviderInternalError",
                              error_reason="HTTP 503")
        assert cb.status_of("p", "https://x", "l2") == "dead"
        cb.reset("p", "https://x", "l2")
        h = cb.get_health("p", "https://x", "l2")
        assert h["status"] == "healthy"
        assert h["consecutive_failures"] == 0
        assert h["last_failure"] is None
        assert h["last_failure_reason"] is None
        assert h["tripped_until"] is None

    def test_reset_healthy_provider_stays_healthy(self, cb):
        h = cb.get_health("p", "https://x", "l2")
        assert h["status"] == "healthy"
        cb.reset("p", "https://x", "l2")
        assert cb.status_of("p", "https://x", "l2") == "healthy"


class TestForceStatus:
    """Manual circuit-breaker overrides: degrade / kill / resume."""

    def test_degrade_forces_degraded_with_cooldown(self, cb):
        status = cb.force_status("p", "https://x", "l2", "degrade")
        assert status == "degraded"
        h = cb.get_health("p", "https://x", "l2")
        assert h["manual_override"] == "degraded"
        assert h["tripped_until"] is not None

    def test_degrade_makes_provider_unavailable_until_cooldown(self, cb):
        cb.force_status("p", "https://x", "l2", "degrade")
        assert cb.is_available("p", "https://x", "l2") is False
        # Past cooldown → half-open promotes back to healthy
        real_now = time.time()
        with patch("time.time", return_value=real_now + 10):
            assert cb.is_available("p", "https://x", "l2") is True

    def test_kill_forces_dead_indefinitely(self, cb):
        status = cb.force_status("p", "https://x", "l2", "kill")
        assert status == "dead"
        h = cb.get_health("p", "https://x", "l2")
        assert h["manual_override"] == "dead"
        # Killed providers stay unavailable even after a long wait
        real_now = time.time()
        with patch("time.time", return_value=real_now + 100000):
            assert cb.is_available("p", "https://x", "l2") is False

    def test_resume_after_kill_recovers(self, cb):
        cb.force_status("p", "https://x", "l2", "kill")
        assert cb.status_of("p", "https://x", "l2") == "dead"
        status = cb.force_status("p", "https://x", "l2", "resume")
        assert status == "healthy"
        h = cb.get_health("p", "https://x", "l2")
        assert h["manual_override"] is None
        assert h["consecutive_failures"] == 0
        assert cb.is_available("p", "https://x", "l2") is True

    def test_resume_clears_manual_override(self, cb):
        cb.force_status("p", "https://x", "l2", "degrade")
        cb.force_status("p", "https://x", "l2", "resume")
        h = cb.get_health("p", "https://x", "l2")
        assert h["manual_override"] is None
        assert h["status"] == "healthy"

    def test_invalid_action_raises(self, cb):
        with pytest.raises(ValueError, match="unknown circuit breaker action"):
            cb.force_status("p", "https://x", "l2", "nonsense")

    def test_record_success_clears_manual_override(self, cb):
        cb.force_status("p", "https://x", "l2", "degrade")
        cb.record_success("p", "https://x", "l2")
        h = cb.get_health("p", "https://x", "l2")
        assert h["manual_override"] is None
        assert h["status"] == "healthy"

    def test_reset_clears_manual_override(self, cb):
        cb.force_status("p", "https://x", "l2", "kill")
        cb.reset("p", "https://x", "l2")
        h = cb.get_health("p", "https://x", "l2")
        assert h["manual_override"] is None


class TestRecordFailover:
    """Persisting failover events to the database."""

    def test_no_engine_is_noop(self, cb):
        # No engine attached — should not raise
        cb.record_failover("l2", "opencode", "deepseek", "ProviderTimeoutError")
        assert cb._engine is None

    def test_persists_failover_event(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        cb.record_failover("l2", "opencode", "deepseek", "ProviderTimeoutError",
                           error_message="HTTP 504 Gateway Timeout")
        from src.api.models import FailoverEvent, get_session
        with get_session(engine) as session:
            rows = session.query(FailoverEvent).all()
        assert len(rows) == 1
        assert rows[0].profile == "l2"
        assert rows[0].from_provider == "opencode"
        assert rows[0].to_provider == "deepseek"
        assert rows[0].reason == "ProviderTimeoutError"
        assert rows[0].error_message == "HTTP 504 Gateway Timeout"

    def test_db_failure_is_logged_not_raised(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        with patch("src.api.models.get_session", side_effect=RuntimeError("db down")):
            cb.record_failover("l2", "opencode", "deepseek", "ProviderInternalError")
        # No exception raised — nothing to assert beyond it not blowing up
        assert True

    def test_attach_engine_stores_reference(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        assert cb._engine is engine

    def test_reset_degraded_provider(self, cb):
        for _ in range(3):
            cb.record_failure("p", "https://x", "l2")
        assert cb.status_of("p", "https://x", "l2") == "degraded"
        cb.reset("p", "https://x", "l2")
        assert cb.status_of("p", "https://x", "l2") == "healthy"
        assert cb.is_available("p", "https://x", "l2") is True


class TestHealthPersistence:
    """Persisted circuit-breaker state survives a restart/redeploy."""

    def _restart(self, engine):
        """A fresh CircuitBreaker attached to the same DB (simulates a restart)."""
        cfg = MagicMock()
        cfg.circuit_breaker = {
            "failures_degraded": 3,
            "failures_dead": 6,
            "degraded_cooldown_seconds": 2,
            "dead_cooldown_seconds": 5,
        }
        cb2 = CircuitBreaker(cfg)
        cb2.attach_engine(engine)
        return cb2

    def test_failure_state_survives_restart(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        # Drive a provider to dead (6 weighted failures).
        for _ in range(6):
            cb.record_failure("persist_prov", "https://x", "l2")
        assert cb.status_of("persist_prov", "https://x", "l2") == "dead"

        cb2 = self._restart(engine)
        h = cb2.get_health("persist_prov", "https://x", "l2")
        assert h["status"] == "dead"
        assert h["consecutive_failures"] == 6
        assert cb2.is_available("persist_prov", "https://x", "l2") is False

    def test_partial_failure_count_survives_restart(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        # 2 of 3 failures → still healthy but the count must survive.
        for _ in range(2):
            cb.record_failure("partial", "https://x", "l2")

        cb2 = self._restart(engine)
        h = cb2.get_health("partial", "https://x", "l2")
        assert h["consecutive_failures"] == 2
        assert h["status"] == "healthy"

    def test_manual_kill_survives_restart(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        cb.force_status("kill_prov", "https://x", "l2", "kill")

        cb2 = self._restart(engine)
        h = cb2.get_health("kill_prov", "https://x", "l2")
        assert h["manual_override"] == "dead"
        assert h["status"] == "dead"
        # Indefinite kill (tripped_until None) must stay unavailable.
        assert cb2.is_available("kill_prov", "https://x", "l2") is False

    def test_manual_resume_persists_healthy(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        cb.force_status("r", "https://x", "l2", "kill")
        cb.force_status("r", "https://x", "l2", "resume")

        cb2 = self._restart(engine)
        h = cb2.get_health("r", "https://x", "l2")
        # Resumed to fully healthy → loaded back as a clean healthy entry.
        assert h["status"] == "healthy"
        assert h["consecutive_failures"] == 0
        assert h["manual_override"] is None

    def test_tripped_until_persisted(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        for _ in range(3):
            cb.record_failure("trip", "https://x", "l2")
        h1 = cb.get_health("trip", "https://x", "l2")
        assert h1["status"] == "degraded"
        assert h1["tripped_until"] is not None

        cb2 = self._restart(engine)
        h2 = cb2.get_health("trip", "https://x", "l2")
        assert h2["status"] == "degraded"
        assert h2["tripped_until"] == h1["tripped_until"]

    def test_success_clears_persisted_state(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        for _ in range(3):
            cb.record_failure("s", "https://x", "l2")
        assert cb.status_of("s", "https://x", "l2") == "degraded"
        cb.record_success("s", "https://x", "l2")

        cb2 = self._restart(engine)
        h = cb2.get_health("s", "https://x", "l2")
        assert h["status"] == "healthy"
        assert h["consecutive_failures"] == 0

    def test_health_persistence_best_effort_no_raise(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        with patch("src.api.models.get_session", side_effect=RuntimeError("db down")):
            cb.record_failure("p", "https://x", "l2")
            cb.record_success("p", "https://x", "l2")
            cb.force_status("p", "https://x", "l2", "kill")
        # In-memory state still updated despite the DB being down.
        assert cb.status_of("p", "https://x", "l2") == "dead"

    def test_db_rows_written(self, cb, temp_db):
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        for _ in range(3):
            cb.record_failure("dbrow", "https://x", "l2")
        from src.api.models import ProviderHealth, get_session
        with get_session(engine) as session:
            rows = session.query(ProviderHealth).filter_by(provider="dbrow").all()
        assert len(rows) == 1
        assert rows[0].status == "degraded"
        assert rows[0].consecutive_failures == 3

    def test_healthy_row_survives_restart(self, cb, temp_db):
        """A fully-healthy provider stays LISTED after a restart (no blink)."""
        _db_path, engine = temp_db
        cb.attach_engine(engine)
        cb.record_success("opencode", "https://oc/v1", "coder")

        cb2 = self._restart(engine)
        h = cb2.get_health("opencode", "https://oc/v1", "coder")
        assert h["status"] == "healthy"
        assert h["last_success"] is not None

