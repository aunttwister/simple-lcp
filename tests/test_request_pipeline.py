"""Tests for request_pipeline retry logic and circuit-breaker integration.

Covers:
  - call_with_retry: retryable vs non-retryable errors, backoff, jitter
  - try_chain retry integration: 5xx/429 retried within a provider, then fallback
  - try_chain degraded-provider gating
"""
import time
import pytest
from unittest.mock import MagicMock, patch

from src.api.request_pipeline import call_with_retry, try_chain
from src.api.exceptions import (
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderInternalError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)


@pytest.fixture(autouse=True)
def _fresh_circuit_breaker():
    """Reset the circuit-breaker singleton between tests (unique provider names
    already prevent most cross-test pollution, this is belt-and-suspenders)."""
    import src.api.circuit_breaker as cb_module
    cb_module._circuit_breaker = None
    yield
    cb_module._circuit_breaker = None


def _retry_cfg(**overrides):
    cfg = {
        "max_attempts": 3,
        "backoff_base": 0.5,
        "backoff_multiplier": 2,
        "max_backoff": 10,
        "jitter": False,
    }
    cfg.update(overrides)
    return cfg


class TestCallWithRetry:
    def test_success_first_attempt(self):
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        with patch("src.api.request_pipeline.time.sleep"):
            assert call_with_retry(fn, _retry_cfg()) == "ok"
        assert len(calls) == 1

    def test_retries_then_succeeds(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ProviderInternalError("503 from provider")
            return "ok"

        with patch("src.api.request_pipeline.time.sleep") as mock_sleep:
            assert call_with_retry(fn, _retry_cfg()) == "ok"
        assert len(calls) == 3
        # Exponential backoff: 0.5s then 1.0s (jitter off)
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays == [0.5, 1.0]

    def test_all_attempts_exhausted_raises_last_error(self):
        def fn():
            raise ProviderInternalError("503 from provider")

        with patch("src.api.request_pipeline.time.sleep"):
            with pytest.raises(ProviderInternalError, match="503"):
                call_with_retry(fn, _retry_cfg())

    def test_rate_limit_retried(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 2:
                raise ProviderRateLimitError("429")
            return "ok"

        with patch("src.api.request_pipeline.time.sleep"):
            assert call_with_retry(fn, _retry_cfg()) == "ok"
        assert len(calls) == 2

    def test_bad_request_not_retried(self):
        calls = []

        def fn():
            calls.append(1)
            raise ProviderBadRequestError("bad body")

        with patch("src.api.request_pipeline.time.sleep"):
            with pytest.raises(ProviderBadRequestError):
                call_with_retry(fn, _retry_cfg())
        assert len(calls) == 1

    def test_auth_error_not_retried(self):
        calls = []

        def fn():
            calls.append(1)
            raise ProviderAuthError("401")

        with patch("src.api.request_pipeline.time.sleep"):
            with pytest.raises(ProviderAuthError):
                call_with_retry(fn, _retry_cfg())
        assert len(calls) == 1

    def test_timeout_not_retried(self):
        calls = []

        def fn():
            calls.append(1)
            raise ProviderTimeoutError("connection dead")

        with patch("src.api.request_pipeline.time.sleep"):
            with pytest.raises(ProviderTimeoutError):
                call_with_retry(fn, _retry_cfg())
        assert len(calls) == 1

    def test_no_retry_config_single_attempt(self):
        """Without a retry config, only a single attempt is made."""
        calls = []

        def fn():
            calls.append(1)
            raise ProviderInternalError("503")

        with patch("src.api.request_pipeline.time.sleep"):
            with pytest.raises(ProviderInternalError):
                call_with_retry(fn, None)
        assert len(calls) == 1

    def test_jitter_randomizes_delay(self):
        def fn():
            raise ProviderInternalError("503")

        with patch("src.api.request_pipeline.time.sleep") as mock_sleep:
            with pytest.raises(ProviderInternalError):
                call_with_retry(fn, _retry_cfg(jitter=True))
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert len(delays) == 2
        # base 0.5 * uniform(0.5,1.5) → [0.25, 0.75]; second: 1.0 * uniform → [0.5, 1.5]
        assert 0.25 <= delays[0] <= 0.75
        assert 0.5 <= delays[1] <= 1.5


def _chain_config(**overrides):
    cfg = MagicMock()
    cfg.circuit_breaker = {
        "failures_degraded": 1,
        "failures_dead": 2,
        "degraded_cooldown_seconds": 10,
        "dead_cooldown_seconds": 20,
    }
    cfg.retry = {"max_attempts": 3, "backoff_base": 0.01,
                 "backoff_multiplier": 2, "max_backoff": 1, "jitter": False}
    cfg.model_limits = {}
    cfg.providers = {}
    cfg.profiles = {}
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestTryChainRetry:
    def test_5xx_retried_then_fallback_to_next_provider(self):
        """A 5xx provider is retried (max_attempts) before falling to the next."""
        from src.api.circuit_breaker import get_circuit_breaker
        cfg = _chain_config(providers={
            "bad": {"api_key_env": "KEY", "base_url": "https://bad/v1"},
            "good": {"api_key_env": "KEY", "base_url": "https://good/v1"},
        })
        get_circuit_breaker(cfg)
        profile_cfg = {
            "chain": [
                {"provider": "bad", "base_url": "https://bad/v1", "model": "m"},
                {"provider": "good", "base_url": "https://good/v1", "model": "m2"},
            ],
            "forbidden_tools": [],
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}

        def fake_forward(step, body, config, session_id=None):
            if step["provider"] == "bad":
                raise ProviderInternalError("503 from bad")
            return {"choices": []}, 200

        with patch("src.api.request_pipeline.forward_request",
                   side_effect=fake_forward) as m:
            with patch("src.api.request_pipeline.time.sleep"):
                resp, status, provider, model = try_chain("l2", profile_cfg, body, cfg)

        assert provider == "good"
        assert model == "m2"
        # bad attempted 3 times (retries exhausted) + good once = 4
        assert m.call_count == 4
        attempted = [c.args[0]["provider"] for c in m.call_args_list]
        assert attempted == ["bad", "bad", "bad", "good"]

    def test_all_retries_and_all_providers_fail(self):
        """All retries + all providers failing raises AllProvidersFailedError."""
        from src.api.circuit_breaker import get_circuit_breaker
        from src.api.exceptions import AllProvidersFailedError
        cfg = _chain_config(providers={
            "bad": {"api_key_env": "KEY", "base_url": "https://bad/v1"},
        })
        get_circuit_breaker(cfg)
        profile_cfg = {
            "chain": [{"provider": "bad", "base_url": "https://bad/v1", "model": "m"}],
            "forbidden_tools": [],
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}

        def fake_forward(step, body, config, session_id=None):
            raise ProviderInternalError("503 from bad")

        with patch("src.api.request_pipeline.forward_request", side_effect=fake_forward) as m:
            with patch("src.api.request_pipeline.time.sleep"):
                with pytest.raises(AllProvidersFailedError):
                    try_chain("l2", profile_cfg, body, cfg)
        assert m.call_count == 3


class TestTryChainDegradedGating:
    def _degrade_to_dead(self, cb):
        """Drive a provider to dead and then past cooldown → degraded (probe)."""
        cb.record_failure("a", "https://a/v1", "l2")
        cb.record_failure("a", "https://a/v1", "l2")  # dead (threshold 2)
        real_now = time.time()
        with patch("time.time", return_value=real_now + 100):
            assert cb.is_available("a", "https://a/v1", "l2") is True  # promoted
        assert cb.status_of("a", "https://a/v1", "l2") == "degraded"

    def test_degraded_probe_is_attempted_despite_healthy_alternative(self):
        """CONTRACT CHANGE 2026-10-27: a degraded step gets its half-open probe.

        The old expectation — a degraded provider is skipped whenever a healthy
        alternative exists in the chain — froze the provider out of rotation
        permanently: by the time the gate ran, ``degraded`` could only mean the
        probe slot ``is_available()`` had just granted, so the gate cancelled the
        probe, ``record_success()`` never ran, and the provider stayed degraded
        while every request billed a fallback (measured in prod 2026-10-27).
        """
        from src.api.circuit_breaker import get_circuit_breaker
        cfg = _chain_config(providers={
            "a": {"api_key_env": "KEY", "base_url": "https://a/v1"},
            "b": {"api_key_env": "KEY", "base_url": "https://b/v1"},
        })
        cb = get_circuit_breaker(cfg)
        self._degrade_to_dead(cb)
        # b is healthy (just tracked)
        cb.get_health("b", "https://b/v1", "l2")
        assert cb.status_of("b", "https://b/v1", "l2") == "healthy"

        profile_cfg = {
            "chain": [
                {"provider": "a", "base_url": "https://a/v1", "model": "m"},
                {"provider": "b", "base_url": "https://b/v1", "model": "m2"},
            ],
            "forbidden_tools": [],
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}

        with patch("src.api.request_pipeline.forward_request") as m:
            m.return_value = ({"choices": []}, 200)
            resp, status, provider, model = try_chain("l2", profile_cfg, body, cfg)

        # The probe goes to the degraded provider first, and its success promotes it.
        assert provider == "a"
        attempted = [c.args[0]["provider"] for c in m.call_args_list]
        assert attempted == ["a"]
        assert cb.status_of("a", "https://a/v1", "l2") == "healthy"

    def test_degraded_probe_failure_falls_through_to_healthy_alternative(self):
        """A failed probe re-trips the provider and the chain moves on."""
        from src.api.circuit_breaker import get_circuit_breaker
        cfg = _chain_config(providers={
            "a": {"api_key_env": "KEY", "base_url": "https://a/v1"},
            "b": {"api_key_env": "KEY", "base_url": "https://b/v1"},
        })
        cb = get_circuit_breaker(cfg)
        self._degrade_to_dead(cb)

        profile_cfg = {
            "chain": [
                {"provider": "a", "base_url": "https://a/v1", "model": "m"},
                {"provider": "b", "base_url": "https://b/v1", "model": "m2"},
            ],
            "forbidden_tools": [],
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}

        def fake_forward(step, body, config, session_id=None):
            if step["provider"] == "a":
                raise ProviderInternalError("503 from a")
            return {"choices": []}, 200

        with patch("src.api.request_pipeline.forward_request", side_effect=fake_forward) as m:
            with patch("src.api.request_pipeline.time.sleep"):
                resp, status, provider, model = try_chain("l2", profile_cfg, body, cfg)

        assert provider == "b"
        assert cb.status_of("a", "https://a/v1", "l2") == "dead"

    def test_degraded_used_when_no_healthy_alternative(self):
        """A degraded provider is still used when no healthy alternative exists."""
        from src.api.circuit_breaker import get_circuit_breaker
        cfg = _chain_config(providers={
            "a": {"api_key_env": "KEY", "base_url": "https://a/v1"},
        })
        cb = get_circuit_breaker(cfg)
        self._degrade_to_dead(cb)

        profile_cfg = {
            "chain": [{"provider": "a", "base_url": "https://a/v1", "model": "m"}],
            "forbidden_tools": [],
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}

        with patch("src.api.request_pipeline.forward_request") as m:
            m.return_value = ({"choices": []}, 200)
            resp, status, provider, model = try_chain("l2", profile_cfg, body, cfg)

        assert provider == "a"
        attempted = [c.args[0]["provider"] for c in m.call_args_list]
        assert attempted == ["a"]

    def test_dead_provider_skipped_even_without_alternative(self):
        """A dead provider (cooldown not elapsed) is skipped entirely.

        CONTRACT CHANGE (preflight gate, PLAN §8 / R11, CUR-103): with every
        chain member dead the gate's G1 refuses the request BEFORE the chain is
        walked, so the caller sees PreflightRefusalError(gate='G1', 502) instead
        of AllProvidersFailedError. Zero provider calls either way.
        """
        from src.api.circuit_breaker import get_circuit_breaker
        from src.api.exceptions import PreflightRefusalError
        cfg = _chain_config(providers={
            "a": {"api_key_env": "KEY", "base_url": "https://a/v1"},
        })
        cb = get_circuit_breaker(cfg)
        cb.record_failure("a", "https://a/v1", "l2")
        cb.record_failure("a", "https://a/v1", "l2")  # dead
        assert cb.status_of("a", "https://a/v1", "l2") == "dead"

        profile_cfg = {
            "chain": [{"provider": "a", "base_url": "https://a/v1", "model": "m"}],
            "forbidden_tools": [],
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}

        with patch("src.api.request_pipeline.forward_request") as m:
            with pytest.raises(PreflightRefusalError) as exc:
                try_chain("l2", profile_cfg, body, cfg)
        assert exc.value.gate == "G1"
        assert m.call_count == 0


class TestTryChainErrorReasonTracked:
    """Verify that circuit breaker record_failure receives error_reason."""

    def test_failure_passes_error_reason_to_circuit_breaker(self):
        """When a provider fails, record_failure is called with the error reason."""
        from unittest.mock import patch as mock_patch
        from src.api.circuit_breaker import get_circuit_breaker
        cfg = _chain_config(providers={
            "bad": {"api_key_env": "KEY", "base_url": "https://bad/v1"},
            "good": {"api_key_env": "KEY", "base_url": "https://good/v1"},
        })
        cb = get_circuit_breaker(cfg)
        profile_cfg = {
            "chain": [
                {"provider": "bad", "base_url": "https://bad/v1", "model": "m"},
                {"provider": "good", "base_url": "https://good/v1", "model": "m2"},
            ],
            "forbidden_tools": [],
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}

        def fake_forward(step, body, config, session_id=None):
            if step["provider"] == "bad":
                raise ProviderInternalError("HTTP 503 Service Unavailable")
            return {"choices": []}, 200

        with mock_patch("src.api.request_pipeline.forward_request",
                        side_effect=fake_forward):
            with mock_patch("src.api.request_pipeline.time.sleep"):
                resp, status, provider, model = try_chain("l2", profile_cfg, body, cfg)

        assert provider == "good"
        h = cb.get_health("bad", "https://bad/v1", "l2")
        assert h["consecutive_failures"] >= 1
        assert "HTTP 503 Service Unavailable" in h["last_failure_reason"]


class TestTryChainNoApiKeyEnv:
    """try_chain must not crash when provider configs lack api_key_env."""

    def test_try_chain_works_without_api_key_env(self):
        """Config providers with no api_key_env should not cause a KeyError."""
        from src.api.circuit_breaker import get_circuit_breaker
        cfg = _chain_config(providers={
            "main": {"base_url": "https://main.com/v1"},
        })
        get_circuit_breaker(cfg)
        profile_cfg = {
            "chain": [{"provider": "main", "base_url": "https://main.com/v1", "model": "m1"}],
            "forbidden_tools": [],
        }
        body = {"messages": [{"role": "user", "content": "hi"}]}

        with patch("src.api.request_pipeline.forward_request") as m:
            m.return_value = ({"choices": []}, 200)
            resp, status, provider, model = try_chain("l2", profile_cfg, body, cfg)

        assert provider == "main"
        assert status == 200
