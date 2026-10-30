"""Every provider rejected the body → 400, not 502.

Measured in prod on 2026-10-24 (`docker logs lcp --since 72h`): 96 `chain_bad_request` events
across 24 requests, all on the `l2` profile. Each of those requests walked all five chain steps
and was answered **502** (`all_providers_failed`) — but every step had in fact answered 400:

  * 48x commandcode: `reasoning_effort` "Invalid option: expected one of off|low|medium|high|xhigh|max"
  * 24x deepseek:     `response_format` "This response_format type is unavailable now"
  * 24x local-sparks: `response_format`

So 24 of ~737 `l2` requests (3.3%) in 72h were told "the gateway is broken, try again shortly"
for a request that no provider in the chain could serve and that would fail identically on retry.
A 4xx is the honest answer: the caller has to change the request.

The distinction the chain now makes:
  * every *attempted* step rejected the body  → RequestUnsupportedError (LCP-3002, 400)
  * anything else (timeout, 5xx, auth, credits, breaker skips) → AllProvidersFailedError (502)

This module pins both branches, plus the fall-through behaviour that must survive.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.api.exceptions import (
    AllProvidersFailedError,
    PreflightRefusalError,
    ProviderBadRequestError,
    ProviderInternalError,
    RequestUnsupportedError,
)
from src.api.request_pipeline import try_chain


@pytest.fixture(autouse=True)
def _fresh_circuit_breaker():
    import src.api.circuit_breaker as cb_module
    cb_module._circuit_breaker = None
    yield
    cb_module._circuit_breaker = None


def _cfg(providers):
    cfg = MagicMock()
    cfg.circuit_breaker = {
        "failures_degraded": 1, "failures_dead": 2,
        "degraded_cooldown_seconds": 10, "dead_cooldown_seconds": 20,
    }
    cfg.retry = {"max_attempts": 1, "backoff_base": 0.01,
                 "backoff_multiplier": 2, "max_backoff": 1, "jitter": False}
    cfg.model_limits = {}
    cfg.providers = providers
    cfg.profiles = {}
    # try_chain resolves the breaker singleton; it must be initialized with this config first.
    from src.api.circuit_breaker import get_circuit_breaker
    get_circuit_breaker(cfg)
    return cfg


def _profile(n, prefix="p"):
    return {
        "chain": [
            {"provider": f"{prefix}{i}", "base_url": f"https://{prefix}{i}/v1", "model": f"m{i}"}
            for i in range(n)
        ],
        "forbidden_tools": [],
    }


def _providers(n, prefix="p"):
    return {f"{prefix}{i}": {"api_key_env": "KEY", "base_url": f"https://{prefix}{i}/v1"}
            for i in range(n)}


BODY = {"messages": [{"role": "user", "content": "hi"}]}


class TestAllRejectedIsAClientError:
    def test_every_step_400_raises_request_unsupported(self):
        cfg = _cfg(_providers(3))
        with patch("src.api.request_pipeline.forward_request",
                   side_effect=ProviderBadRequestError("bad body")):
            with pytest.raises(RequestUnsupportedError) as ei:
                try_chain("l2", _profile(3), BODY, cfg)
        assert ei.value.status_code == 400
        assert ei.value.code == "LCP-3002"

    def test_it_really_walks_the_whole_chain_first(self):
        """Falling through is the point — one 400 must not abort the chain."""
        cfg = _cfg(_providers(3))
        with patch("src.api.request_pipeline.forward_request",
                   side_effect=ProviderBadRequestError("bad body")) as m:
            with pytest.raises(RequestUnsupportedError):
                try_chain("l2", _profile(3), BODY, cfg)
        assert m.call_count == 3
        assert [c.args[0]["provider"] for c in m.call_args_list] == ["p0", "p1", "p2"]

    def test_client_payload_hides_provider_names(self):
        cfg = _cfg(_providers(2))
        with patch("src.api.request_pipeline.forward_request",
                   side_effect=ProviderBadRequestError("secret-upstream-detail")):
            with pytest.raises(RequestUnsupportedError) as ei:
                try_chain("l2", _profile(2), BODY, cfg)
        payload = ei.value.to_dict()
        blob = str(payload)
        assert "secret-upstream-detail" not in blob
        assert "p0" not in payload["message"]
        assert payload["code"] == "LCP-3002"
        assert "invalid" in payload["message"]

    def test_server_message_keeps_the_reasons(self):
        """The detail must survive for the logs — that is how the parameter bug was found."""
        cfg = _cfg(_providers(2))
        with patch("src.api.request_pipeline.forward_request",
                   side_effect=ProviderBadRequestError("reasoning_effort invalid")):
            with pytest.raises(RequestUnsupportedError) as ei:
                try_chain("l2", _profile(2), BODY, cfg)
        assert "reasoning_effort invalid" in str(ei.value)
        assert "2 provider(s)" in str(ei.value)


class TestMixedFailuresStayAnOutage:
    def test_one_400_and_one_5xx_is_still_502(self):
        cfg = _cfg(_providers(2))

        def fake(step, body, config, session_id=None):
            if step["provider"] == "p0":
                raise ProviderBadRequestError("bad body")
            raise ProviderInternalError("503")

        with patch("src.api.request_pipeline.forward_request", side_effect=fake):
            with pytest.raises(AllProvidersFailedError) as ei:
                try_chain("l2", _profile(2), BODY, cfg)
        assert ei.value.status_code == 502
        assert ei.value.code == "LCP-3001"

    def test_a_400_then_a_success_is_a_success(self):
        """The measured prod shape: head rejects, a later provider serves it."""
        cfg = _cfg(_providers(3))
        seen = []

        def fake(step, body, config, session_id=None):
            seen.append(step["provider"])
            if step["provider"] == "p0":
                raise ProviderBadRequestError("reasoning_effort invalid")
            return {"choices": []}, 200

        with patch("src.api.request_pipeline.forward_request", side_effect=fake):
            resp, status, provider, model = try_chain("l2", _profile(3), BODY, cfg)
        assert provider == "p1"
        assert status == 200
        assert seen == ["p0", "p1"]

    def test_breaker_skips_are_not_rejections(self):
        """A chain that never reached a provider must not claim the body was rejected."""
        cfg = _cfg(_providers(2))
        from src.api.circuit_breaker import get_circuit_breaker
        cb = get_circuit_breaker(cfg)
        for _ in range(3):
            cb.record_failure("p0", "https://p0/v1", "l2",
                              error_type="ProviderInternalError", error_reason="503")
        with patch("src.api.request_pipeline.forward_request",
                   side_effect=ProviderBadRequestError("bad body")):
            # p0 is dead/skipped, p1 is reached and rejects → 1 attempt, 1 rejection
            with pytest.raises(RequestUnsupportedError):
                try_chain("l2", _profile(2), BODY, cfg)

    def test_nothing_attempted_is_an_outage_not_a_rejection(self):
        """An all-dead chain stays an OUTAGE (5xx), never a client rejection (4xx).

        CONTRACT CHANGE (preflight gate, PLAN §8 / R11, CUR-103): this used to
        walk the chain, skip every step and end as AllProvidersFailedError. The
        gate's G1 answers "is any chain member usable?" before the walk, so the
        refusal is now PreflightRefusalError(gate='G1') — still a 502-shaped
        outage, still zero provider calls.
        """
        cfg = _cfg(_providers(2))
        from src.api.circuit_breaker import get_circuit_breaker
        cb = get_circuit_breaker(cfg)
        for p in ("p0", "p1"):
            for _ in range(3):
                cb.record_failure(p, f"https://{p}/v1", "l2",
                                  error_type="ProviderInternalError", error_reason="503")
        with patch("src.api.request_pipeline.forward_request") as m:
            with pytest.raises(PreflightRefusalError) as ei:
                try_chain("l2", _profile(2), BODY, cfg)
        assert m.call_count == 0
        assert ei.value.gate == "G1"
        assert ei.value.status_code >= 500, "an all-dead chain is an outage, not a bad request"
        assert not isinstance(ei.value, RequestUnsupportedError)


class TestErrorTaxonomy:
    def test_status_and_code(self):
        e = RequestUnsupportedError("x")
        assert (e.code, e.status_code) == ("LCP-3002", 400)

    def test_it_is_not_a_provider_error(self):
        """502-shaped handling (retry, failover) must not swallow it."""
        from src.api.exceptions import ProviderError
        assert not issubclass(RequestUnsupportedError, ProviderError)

    def test_distinct_from_all_providers_failed(self):
        assert not issubclass(RequestUnsupportedError, AllProvidersFailedError)
        assert RequestUnsupportedError.status_code != AllProvidersFailedError.status_code
