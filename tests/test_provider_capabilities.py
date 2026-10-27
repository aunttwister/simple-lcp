"""Per-provider capability handling — strip a hint, skip a contract.

The `l2` chain in prod, measured 2026-10-24 (`docker logs lcp --since 72h`):

    chain_attempt 1015 · chain_success 895 · chain_bad_request 96 · all_providers_failed 24

96 rejections over 24 requests — four per request, i.e. every request that walked the whole
5-step chain paid a rejection at every step before the tail served it:

  * 48x commandcode  — `reasoning_effort` outside off|low|medium|high|xhigh|max
  * 24x deepseek     — `response_format` type unavailable
  * 24x local-sparks — `response_format`

Two treatments, and the difference is the point of this module:

  * `reasoning_effort` is a **hint** → strip the unsupported value, forward anyway. The caller
    loses a little steering, not correctness.
  * `response_format` is a **contract** → do not strip. A caller that asked for `json_schema`
    and silently got prose has been lied to. Skip the provider instead, and if every provider
    is skipped the request ends as RequestUnsupportedError (400).

Both halves are pinned here, plus the regression that matters most: a parameter stripped for
provider A must still reach provider B, which may accept it.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.api.capabilities import DEFAULT_CAPABILITIES, get_capabilities, prepare_body
from src.api.exceptions import RequestUnsupportedError
from src.api.request_pipeline import try_chain


BODY = {"messages": [{"role": "user", "content": "hi"}]}


def _cfg(providers=None, profiles=None):
    cfg = MagicMock()
    cfg.circuit_breaker = {
        "failures_degraded": 1, "failures_dead": 2,
        "degraded_cooldown_seconds": 10, "dead_cooldown_seconds": 20,
    }
    cfg.retry = {"max_attempts": 1, "backoff_base": 0.01,
                 "backoff_multiplier": 2, "max_backoff": 1, "jitter": False}
    cfg.model_limits = {}
    cfg.providers = providers or {}
    cfg.profiles = profiles or {}
    from src.api.circuit_breaker import get_circuit_breaker
    get_circuit_breaker(cfg)
    return cfg


@pytest.fixture(autouse=True)
def _fresh_circuit_breaker():
    import src.api.circuit_breaker as cb_module
    cb_module._circuit_breaker = None
    yield
    cb_module._circuit_breaker = None


# --------------------------------------------------------------------------- unit: prepare_body

class TestReasoningEffortIsAHint:
    def test_unsupported_value_is_stripped_not_fatal(self):
        body = {**BODY, "reasoning_effort": "minimal"}
        out, notes, skip = prepare_body("commandcode", body, _cfg())
        assert skip is None
        assert "reasoning_effort" not in out
        assert notes and "reasoning_effort" in notes[0]

    def test_supported_value_passes_through_untouched(self):
        body = {**BODY, "reasoning_effort": "high"}
        out, notes, skip = prepare_body("commandcode", body, _cfg())
        assert skip is None and notes == []
        assert out["reasoning_effort"] == "high"

    def test_input_dict_is_not_mutated(self):
        body = {**BODY, "reasoning_effort": "minimal"}
        prepare_body("commandcode", body, _cfg())
        assert body["reasoning_effort"] == "minimal", "caller's body must survive a strip"

    def test_provider_without_declaration_gets_body_unchanged(self):
        body = {**BODY, "reasoning_effort": "minimal"}
        out, notes, skip = prepare_body("opencode", body, _cfg())
        assert skip is None and notes == []
        assert out["reasoning_effort"] == "minimal", "no declaration means no guessing"

    def test_other_keys_survive_a_strip(self):
        body = {**BODY, "reasoning_effort": "minimal", "temperature": 0.2, "max_tokens": 64}
        out, _, _ = prepare_body("commandcode", body, _cfg())
        assert out["temperature"] == 0.2 and out["max_tokens"] == 64


class TestResponseFormatIsAContract:
    def test_json_schema_is_a_skip_for_deepseek(self):
        body = {**BODY, "response_format": {"type": "json_schema"}}
        out, notes, skip = prepare_body("deepseek", body, _cfg())
        assert skip and "json_schema" in skip
        assert out is body, "a contract violation must not be silently downgraded"

    def test_json_object_is_a_skip_for_local_sparks(self):
        body = {**BODY, "response_format": {"type": "json_object"}}
        _, _, skip = prepare_body("local-sparks", body, _cfg())
        assert skip and "json_object" in skip

    def test_json_object_is_accepted_by_deepseek(self):
        """Regression (2026-10-25): a {text}-only deepseek declaration killed every Hindsight
        retain. deepseek accepts json_object; only json_schema is unavailable."""
        body = {**BODY, "response_format": {"type": "json_object"}}
        out, notes, skip = prepare_body("deepseek", body, _cfg())
        assert skip is None, "json_object is supported by deepseek — skipping it is a false negative"
        assert notes == []
        assert out["response_format"] == {"type": "json_object"}

    def test_plain_text_is_accepted(self):
        body = {**BODY, "response_format": {"type": "text"}}
        out, notes, skip = prepare_body("deepseek", body, _cfg())
        assert skip is None and notes == []
        assert out["response_format"] == {"type": "text"}

    def test_absent_response_format_is_fine(self):
        out, notes, skip = prepare_body("deepseek", BODY, _cfg())
        assert skip is None and notes == [] and out["messages"] == BODY["messages"]

    def test_provider_without_declaration_is_not_skipped(self):
        body = {**BODY, "response_format": {"type": "json_schema"}}
        _, _, skip = prepare_body("opencode", body, _cfg())
        assert skip is None


class TestConfigOverride:
    def test_override_replaces_the_default_enum(self):
        cfg = _cfg(providers={"commandcode": {"capabilities": {"reasoning_effort": ["low", "high"]}}})
        out, notes, _ = prepare_body("commandcode", {**BODY, "reasoning_effort": "medium"}, cfg)
        assert "reasoning_effort" not in out and notes

    def test_override_can_widen_the_enum(self):
        cfg = _cfg(providers={"commandcode": {"capabilities": {"reasoning_effort": ["minimal", "high"]}}})
        out, notes, _ = prepare_body("commandcode", {**BODY, "reasoning_effort": "minimal"}, cfg)
        assert notes == [] and out["reasoning_effort"] == "minimal"

    def test_override_can_declare_a_contract_the_defaults_do_not(self):
        cfg = _cfg(providers={"opencode": {"capabilities": {"response_format": ["text"]}}})
        _, _, skip = prepare_body("opencode", {**BODY, "response_format": {"type": "json_schema"}}, cfg)
        assert skip

    def test_get_capabilities_returns_a_copy(self):
        caps = get_capabilities("commandcode", _cfg())
        caps["reasoning_effort"].add("bogus")
        assert "bogus" not in DEFAULT_CAPABILITIES["commandcode"]["reasoning_effort"], \
            "mutating a returned capability set must not poison the module default"


# ------------------------------------------------------------------ integration: try_chain

def _chain_providers(names):
    return {n: {"api_key_env": "KEY", "base_url": f"https://{n}/v1"} for n in names}


def _profile(names):
    return {
        "chain": [{"provider": n, "base_url": f"https://{n}/v1", "model": "m"} for n in names],
        "forbidden_tools": [],
    }


class TestChainIntegration:
    def test_reasoning_effort_stripped_then_request_succeeds(self):
        cfg = _cfg(providers=_chain_providers(["commandcode"]), profiles={"p": _profile(["commandcode"])})
        sent = {}

        def _forward(step, body, config, session_id=None):
            sent.update(body)
            return {"choices": [{"message": {"content": "ok"}}]}, 200

        with patch("src.api.request_pipeline.forward_request", side_effect=_forward):
            resp, status, provider, model = try_chain("p", _profile(["commandcode"]),
                      {"messages": BODY["messages"], "reasoning_effort": "minimal"}, cfg)

        assert status == 200 and provider == "commandcode"
        assert "reasoning_effort" not in sent, "the unsupported hint must not reach the wire"

    def test_strip_for_one_provider_does_not_starve_the_next(self):
        """The regression that matters: step_body must be per-step, not shared."""
        cfg = _cfg(providers=_chain_providers(["commandcode", "deepseek"]),
                   profiles={"p": _profile(["commandcode", "deepseek"])})
        seen = []

        def _forward(step, body, config, session_id=None):
            seen.append((step["provider"], "reasoning_effort" in body))
            return {"choices": [{"message": {"content": "ok"}}]}, 200

        with patch("src.api.request_pipeline.forward_request", side_effect=_forward):
            try_chain("p", _profile(["commandcode", "deepseek"]),
                      {"messages": BODY["messages"], "reasoning_effort": "minimal"}, cfg)

        assert seen == [("commandcode", False)], "first step served; only one hop expected"

    def test_contract_violation_skips_to_the_next_provider(self):
        cfg = _cfg(providers=_chain_providers(["deepseek", "opencode"]),
                   profiles={"p": _profile(["deepseek", "opencode"])})
        seen = []

        def _forward(step, body, config, session_id=None):
            seen.append(step["provider"])
            return {"choices": [{"message": {"content": "{}"}}]}, 200

        with patch("src.api.request_pipeline.forward_request", side_effect=_forward):
            _, status, provider, _ = try_chain("p", _profile(["deepseek", "opencode"]),
                          {"messages": BODY["messages"], "response_format": {"type": "json_schema"}}, cfg)

        assert status == 200 and provider == "opencode"
        assert seen == ["opencode"], "deepseek must not be sent a contract it cannot honour"

    def test_every_provider_unable_is_a_400_not_a_502(self):
        cfg = _cfg(providers=_chain_providers(["deepseek", "local-sparks"]),
                   profiles={"p": _profile(["deepseek", "local-sparks"])})
        with patch("src.api.request_pipeline.forward_request") as fwd:
            with pytest.raises(RequestUnsupportedError) as ei:
                try_chain("p", _profile(["deepseek", "local-sparks"]),
                           {"messages": BODY["messages"], "response_format": {"type": "json_schema"}}, cfg)
        assert fwd.call_count == 0, "nothing should have gone over the wire"
        assert ei.value.status_code == 400 and ei.value.code == "LCP-3002"

    def test_capability_skip_does_not_trip_the_circuit_breaker(self):
        """A provider that cannot serve this *shape* is not an outage — its health must not move."""
        cfg = _cfg(providers=_chain_providers(["deepseek", "opencode"]),
                   profiles={"p": _profile(["deepseek", "opencode"])})
        from src.api.circuit_breaker import get_circuit_breaker
        cb = get_circuit_breaker(cfg)
        before = cb.status_of("deepseek", "https://deepseek/v1", "p")

        with patch("src.api.request_pipeline.forward_request",
                   return_value=({"choices": []}, 200)):
            try_chain("p", _profile(["deepseek", "opencode"]),
                       {"messages": BODY["messages"], "response_format": {"type": "json_schema"}}, cfg)

        assert cb.status_of("deepseek", "https://deepseek/v1", "p") == before

    def test_body_without_known_params_is_unchanged(self):
        cfg = _cfg(providers=_chain_providers(["deepseek"]), profiles={"p": _profile(["deepseek"])})
        sent = {}

        def _forward(step, body, config, session_id=None):
            sent.update(body)
            return {"choices": []}, 200

        with patch("src.api.request_pipeline.forward_request", side_effect=_forward):
            try_chain("p", _profile(["deepseek"]),
                      {"messages": BODY["messages"], "temperature": 0.3}, cfg)

        assert sent["temperature"] == 0.3 and "reasoning_effort" not in sent
