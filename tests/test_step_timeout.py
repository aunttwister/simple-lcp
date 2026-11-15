"""Per-step FIRST-BYTE budget — "try for N seconds, then next in line".

The mechanism behind it: a request sent to a local engine whose admission queue is
already deep sits in that queue producing nothing, and LCP's relay ceiling is 900 s
because a legitimate long-context cloud compression can take 8-9 minutes before its
first byte. One shared ceiling cannot serve both, so the budget is per STEP:
``profiles.<name>.step_timeout_s`` (the caller owns the latency budget) with an
optional ``timeout_s`` on a single chain step.

Two properties are pinned hardest:

* **A budget that expires with NO first byte is not a provider fault.** It must fail
  over WITHOUT counting against provider health — otherwise a busy-but-serving lane
  latches degraded/dead, which is the very thing the budget exists to avoid.
* **A provider that DID start is never killed by the budget.** The socket is relaxed
  to the relay ceiling once the first byte arrives, so long generations still finish.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.api.exceptions import ProviderFirstByteTimeoutError, ProviderTimeoutError
from src.api.request_pipeline import first_byte_timeout, forward_request, try_chain


BODY = {"messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture(autouse=True)
def _fresh_breaker():
    import src.api.circuit_breaker as cb_module
    cb_module._circuit_breaker = None
    yield
    cb_module._circuit_breaker = None


def _cfg(profiles=None):
    cfg = MagicMock()
    cfg.circuit_breaker = {"failures_degraded": 1, "failures_dead": 2,
                           "degraded_cooldown_seconds": 10, "dead_cooldown_seconds": 20}
    cfg.retry = {"max_attempts": 1, "backoff_base": 0.01,
                 "backoff_multiplier": 2, "max_backoff": 1, "jitter": False}
    cfg.model_limits = {}
    cfg.providers = {}
    cfg.profiles = profiles or {}
    from src.api.circuit_breaker import get_circuit_breaker
    get_circuit_breaker(cfg)
    return cfg


def _profile(names, **extra):
    prof = {
        "chain": [{"provider": n, "base_url": f"https://{n}/v1", "model": "m"} for n in names],
        "forbidden_tools": [],
    }
    prof.update(extra)
    return prof


# --------------------------------------------------------------- unit: precedence

class TestFirstByteTimeoutResolution:
    def test_no_config_means_no_budget(self):
        """Absent everywhere -> None -> the historical relay ceiling is untouched."""
        assert first_byte_timeout({}, {}, None) is None

    def test_profile_budget_is_the_default_home(self):
        assert first_byte_timeout({"step_timeout_s": 90}, {}, None) == 90

    def test_step_budget_overrides_the_profile(self):
        assert first_byte_timeout({"step_timeout_s": 90}, {"timeout_s": 15}, None) == 15

    def test_step_budget_alone_works(self):
        assert first_byte_timeout({}, {"timeout_s": 45}, None) == 45

    def test_string_values_are_coerced(self):
        assert first_byte_timeout({"step_timeout_s": "120"}, {}, None) == 120

    def test_nonsense_values_fall_through_to_the_profile(self):
        """A typo must not disable the budget — fall through to the next source."""
        assert first_byte_timeout({"step_timeout_s": 60}, {"timeout_s": "soon"}, None) == 60

    def test_nonsense_values_are_ignored_entirely(self):
        assert first_byte_timeout({"step_timeout_s": "nope"}, {}, None) is None

    def test_zero_and_negative_are_not_budgets(self):
        """0 would mean 'never' — treat it as unset, never as an instant timeout."""
        assert first_byte_timeout({"step_timeout_s": 0}, {}, None) is None
        assert first_byte_timeout({"step_timeout_s": -5}, {}, None) is None

    def test_non_dict_inputs_do_not_raise(self):
        assert first_byte_timeout(None, None, None) is None


# --------------------------------------------------- unit: the streaming mechanism

class _FakeResp:
    """Minimal urllib response: raises TimeoutError after `ok_chunks` good reads."""

    def __init__(self, ok_chunks=0, then_timeout=True, status=200):
        self.status = status
        self._left = ok_chunks
        self._then = then_timeout
        self.closed = False

    def read(self, n):
        if self._left > 0:
            self._left -= 1
            return b"data: {}\n\n"
        if self._then:
            raise TimeoutError("timed out")
        return b""

    def close(self):
        self.closed = True


def _stream_body():
    return {"messages": BODY["messages"], "stream": True, "model": "glm-5.3-flash"}


class TestStreamingBudget:
    def test_no_first_byte_within_budget_is_a_first_byte_timeout(self):
        resp = _FakeResp(ok_chunks=0)
        with patch("src.api.request_pipeline.urllib.request.urlopen", return_value=resp):
            reader, status = forward_request(
                {"provider": "local-sparks", "base_url": "http://spark:18300/v1",
                 "model": "glm-5.3-flash", "first_byte_timeout": 30},
                _stream_body(), _cfg())
            with pytest.raises(ProviderFirstByteTimeoutError) as ei:
                list(reader)
        assert "no first byte" in str(ei.value)
        assert "30s" in str(ei.value)

    def test_budget_expiry_is_a_subclass_of_the_existing_timeout(self):
        """Every existing handler/status consumer keeps working unchanged."""
        assert issubclass(ProviderFirstByteTimeoutError, ProviderTimeoutError)

    def test_stall_AFTER_the_first_byte_stays_a_plain_timeout(self):
        """A provider that started and then stalled IS a fault — different class."""
        resp = _FakeResp(ok_chunks=1, then_timeout=True)
        with patch("src.api.request_pipeline.urllib.request.urlopen", return_value=resp), \
             patch("src.api.request_pipeline._relax_socket_timeout"):
            reader, _ = forward_request(
                {"provider": "local-sparks", "base_url": "http://spark:18300/v1",
                 "model": "glm-5.3-flash", "first_byte_timeout": 30},
                _stream_body(), _cfg())
            with pytest.raises(ProviderTimeoutError) as ei:
                list(reader)
        assert not isinstance(ei.value, ProviderFirstByteTimeoutError)

    def test_first_byte_relaxes_the_socket_to_the_relay_ceiling(self):
        """The budget must never truncate a long generation that already started."""
        resp = _FakeResp(ok_chunks=2, then_timeout=False)
        with patch("src.api.request_pipeline.urllib.request.urlopen", return_value=resp), \
             patch("src.api.request_pipeline._relax_socket_timeout") as relax:
            reader, _ = forward_request(
                {"provider": "local-sparks", "base_url": "http://spark:18300/v1",
                 "model": "glm-5.3-flash", "first_byte_timeout": 30},
                _stream_body(), _cfg())
            list(reader)
        assert relax.call_count == 1, "relax exactly once, on the first chunk"

    def test_no_budget_means_no_relaxation(self):
        resp = _FakeResp(ok_chunks=1, then_timeout=False)
        with patch("src.api.request_pipeline.urllib.request.urlopen", return_value=resp), \
             patch("src.api.request_pipeline._relax_socket_timeout") as relax:
            reader, _ = forward_request(
                {"provider": "local-sparks", "base_url": "http://spark:18300/v1",
                 "model": "glm-5.3-flash"},
                _stream_body(), _cfg())
            list(reader)
        assert relax.call_count == 0

    def test_non_streaming_ignores_the_budget(self):
        """On a non-streaming call the first byte IS the whole answer: applying the
        budget there would become a total-request timeout and kill long generations."""
        seen = {}

        def _open(req, timeout=None):
            seen["timeout"] = timeout
            return _FakeResp(ok_chunks=1, then_timeout=False)

        with patch("src.api.request_pipeline.urllib.request.urlopen", side_effect=_open):
            body = {"messages": BODY["messages"], "stream": False, "model": "glm-5.3-flash"}
            with pytest.raises(Exception):
                # The fake body is not valid JSON, so the parse fails — the point
                # asserted below is only the socket timeout that was used.
                forward_request({"provider": "local-sparks",
                                 "base_url": "http://spark:18300/v1",
                                 "model": "glm-5.3-flash",
                                 "first_byte_timeout": 30}, body, _cfg())
        assert seen["timeout"] != 30, "a non-streaming call must keep the relay ceiling"


# ------------------------------------------------------------ integration: try_chain

class TestChainStepTimeout:
    def test_budget_reaches_the_step_from_the_profile(self):
        names = ["local-sparks", "deepseek"]
        prof = _profile(names, step_timeout_s=42)
        cfg = _cfg(profiles={"p": prof})
        seen = {}

        def _forward(step, body, config, session_id=None):
            seen["budget"] = step.get("first_byte_timeout")
            return {"choices": [{"message": {"content": "ok"}}]}, 200

        with patch("src.api.request_pipeline.forward_request", side_effect=_forward):
            try_chain("p", prof, dict(BODY), cfg)
        assert seen["budget"] == 42

    def test_step_budget_overrides_the_profile_in_the_chain(self):
        names = ["local-sparks", "deepseek"]
        prof = {"chain": [
            {"provider": "local-sparks", "base_url": "https://local-sparks/v1",
             "model": "m", "timeout_s": 10},
            {"provider": "deepseek", "base_url": "https://deepseek/v1", "model": "m"},
        ], "forbidden_tools": [], "step_timeout_s": 90}
        cfg = _cfg(profiles={"p": prof})
        seen = {}

        def _forward(step, body, config, session_id=None):
            seen[step["provider"]] = step.get("first_byte_timeout")
            return {"choices": [{"message": {"content": "ok"}}]}, 200

        with patch("src.api.request_pipeline.forward_request", side_effect=_forward):
            try_chain("p", prof, dict(BODY), cfg)
        assert seen["local-sparks"] == 10, "the hop's own budget wins"

    def test_expired_budget_fails_over_without_marking_the_provider_down(self):
        """THE property: a busy lane must not be latched degraded for being busy."""
        from src.api.circuit_breaker import get_circuit_breaker
        names = ["local-sparks", "deepseek"]
        prof = _profile(names, step_timeout_s=30)
        cfg = _cfg(profiles={"p": prof})
        wired = []

        def _forward(step, body, config, session_id=None):
            if step["provider"] == "local-sparks":
                raise ProviderFirstByteTimeoutError(
                    "Provider local-sparks produced no first byte within its 30s step budget")
            wired.append(step["provider"])
            return {"choices": [{"message": {"content": "ok"}}]}, 200

        with patch("src.api.request_pipeline.forward_request", side_effect=_forward):
            _, status, provider, _ = try_chain("p", prof, dict(BODY), cfg)

        assert status == 200 and provider == "deepseek"
        assert wired == ["deepseek"], "the chain moved to the next step in line"
        cb = get_circuit_breaker(cfg)
        assert cb.status_of("local-sparks", "https://local-sparks/v1", "p") == "healthy", \
            "a first-byte timeout must not be counted against provider health"

    def test_no_budget_anywhere_leaves_the_chain_unchanged(self):
        """Regression guard: shipping this must change nothing by default."""
        names = ["local-sparks", "deepseek"]
        prof = _profile(names)
        cfg = _cfg(profiles={"p": prof})
        seen = {}

        def _forward(step, body, config, session_id=None):
            seen[step["provider"]] = step.get("first_byte_timeout")
            return {"choices": [{"message": {"content": "ok"}}]}, 200

        with patch("src.api.request_pipeline.forward_request", side_effect=_forward):
            _, status, provider, _ = try_chain("p", prof, dict(BODY), cfg)
        assert provider == "local-sparks" and seen["local-sparks"] is None


class TestStallBeforeTheResponseHead:
    """The failure the live proof on staging exposed (2026-11-15).

    The armed step ACCEPTED the TCP connection and then sent nothing at all, so
    ``urlopen`` itself blocked on the response HEAD and raised TimeoutError —
    ``chunk_reader`` was never reached. Every earlier test here fed in a response
    object and made the READ time out, so this path was never exercised: the
    TimeoutError escaped to the generic handler, came back as a plain
    ProviderTimeoutError quoting the 900s relay ceiling, and the chain recorded a
    FAILURE for a provider that was merely busy — the exact latch the per-step
    budget exists to prevent. Symptom on staging: 502 at 5.2s, provider marked
    failed, no chain_skip_step_timeout.
    """

    def test_urlopen_timeout_with_a_budget_is_a_first_byte_timeout(self):
        with patch("src.api.request_pipeline.urllib.request.urlopen",
                   side_effect=TimeoutError("timed out")):
            with pytest.raises(ProviderFirstByteTimeoutError) as ei:
                forward_request(
                    {"provider": "blackhole", "base_url": "http://bh:9999/v1",
                     "model": "stall", "first_byte_timeout": 5},
                    _stream_body(), _cfg())
        msg = str(ei.value)
        assert "no first byte" in msg
        assert "5s" in msg, msg
        # must not carry the relay ceiling it used to be mislabelled with
        assert "900" not in msg, msg

    def test_urlopen_timeout_without_a_budget_keeps_the_old_behaviour(self):
        """No budget configured anywhere: the historical path is untouched, so
        the budget is inert until someone sets it (the fail-open requirement)."""
        with patch("src.api.request_pipeline.urllib.request.urlopen",
                   side_effect=TimeoutError("timed out")):
            with pytest.raises(ProviderTimeoutError) as ei:
                forward_request(
                    {"provider": "local-sparks", "base_url": "http://spark:18300/v1",
                     "model": "glm-5.3-flash"},
                    _stream_body(), _cfg())
        assert not isinstance(ei.value, ProviderFirstByteTimeoutError)

    def test_non_streaming_request_never_gets_a_first_byte_budget(self):
        """A non-streaming call has no separable first byte, so the budget must
        not apply — otherwise it silently becomes a total-request timeout and
        kills legitimately long generations."""
        with patch("src.api.request_pipeline.urllib.request.urlopen",
                   side_effect=TimeoutError("timed out")) as uo:
            with pytest.raises(ProviderTimeoutError) as ei:
                forward_request(
                    {"provider": "local-sparks", "base_url": "http://spark:18300/v1",
                     "model": "glm-5.3-flash", "first_byte_timeout": 5},
                    {"messages": BODY["messages"], "stream": False,
                     "model": "glm-5.3-flash"}, _cfg())
        assert not isinstance(ei.value, ProviderFirstByteTimeoutError)
        # and it waited on the ceiling, not the step budget
        assert uo.call_args.kwargs.get("timeout") != 5
