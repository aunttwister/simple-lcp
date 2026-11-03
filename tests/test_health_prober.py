"""Active health probing — the skip rules and the recording contract.

Each test here pins a hazard that would otherwise produce a FALSE health signal,
which is the worst outcome for this feature: a monitor that invents a fault or
invents health is worse than no monitor.
"""

import json
import time
from unittest.mock import patch

import pytest

from src.api.health_prober import (
    DEFAULT_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    HealthProber,
    get_health_prober,
    load_probe_config,
    probe_once_targets,
)


class FakeSettings:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def get(self, key):
        return self.rows.get(key)


class FakeBreaker:
    """Records what the prober told it, and can claim a manual override."""

    def __init__(self, overrides=None):
        self.calls = []
        self.overrides = overrides or {}

    def get_health(self, provider, base_url, profile):
        return {"manual_override": self.overrides.get((provider, base_url, profile))}

    def record_success(self, provider, base_url, profile):
        self.calls.append(("success", provider, base_url, profile))

    def record_failure(self, provider, base_url, profile, error_type=None, error_reason=None):
        self.calls.append(("failure", provider, base_url, profile, error_reason))


class FakeCreds:
    def __init__(self, keys=None):
        self.keys = keys or {}

    def get(self, provider):
        return self.keys.get(provider)


class FakeConfig:
    def __init__(self, chains, providers=None):
        self.profiles = {p: {"chain": c} for p, c in chains.items()}
        self.providers = providers or {}


# ── config ────────────────────────────────────────────────────────────────

def test_probe_is_on_by_default_because_health_should_not_need_enabling():
    cfg = load_probe_config(FakeSettings())
    assert cfg["enabled"] is True
    assert cfg["interval_seconds"] == DEFAULT_INTERVAL_SECONDS


def test_interval_is_configurable_and_floored():
    cfg = load_probe_config(FakeSettings({"gateway_config:health_probe":
                                          json.dumps({"interval_seconds": 5})}))
    assert cfg["interval_seconds"] == MIN_INTERVAL_SECONDS, "a 5s probe would bill every 5s"


def test_interval_reads_from_a_json_string_or_a_dict():
    as_str = load_probe_config(FakeSettings({"gateway_config:health_probe":
                                             '{"interval_seconds": 120}'}))
    as_dict = load_probe_config(FakeSettings({"gateway_config:health_probe":
                                              {"interval_seconds": 120}}))
    assert as_str["interval_seconds"] == as_dict["interval_seconds"] == 120


def test_can_be_switched_off():
    cfg = load_probe_config(FakeSettings({"gateway_config:health_probe":
                                          json.dumps({"enabled": False})}))
    assert cfg["enabled"] is False


def test_a_broken_settings_store_leaves_probing_ON():
    """Falling back to 'off' would turn a store fault into silent blindness."""
    class Boom:
        def get(self, key):
            raise RuntimeError("db down")

    assert load_probe_config(Boom())["enabled"] is True


# ── targets ───────────────────────────────────────────────────────────────

def test_targets_are_chain_steps_not_every_provider():
    cfg = FakeConfig({"l2": [{"provider": "commandcode"},
                             {"provider": "local-sparks", "base_url": "http://192.168.1.107:18300/v1"}]},
                     providers={"commandcode": {"api_base": "https://api.commandcode.ai/provider/v1"},
                                "unused": {"api_base": "https://unused.example/v1"}})
    targets = probe_once_targets(cfg)
    got = {(t["provider"], t["base_url"], t["profile"]) for t in targets}
    assert got == {("commandcode", "https://api.commandcode.ai/provider/v1", "l2"),
                   ("local-sparks", "http://192.168.1.107:18300/v1", "l2")}
    assert all(t["provider"] != "unused" for t in targets), \
        "a provider no chain names is not assessable for that profile"


def test_step_base_url_wins_over_the_provider_default():
    cfg = FakeConfig({"coder": [{"provider": "opencode",
                                 "base_url": "https://opencode.ai/inference/openai/v1"}]},
                     providers={"opencode": {"api_base": "https://opencode.ai/zen/go/v1"}})
    assert probe_once_targets(cfg)[0]["base_url"] == "https://opencode.ai/inference/openai/v1"


def test_a_step_with_no_resolvable_base_url_is_dropped_not_guessed():
    cfg = FakeConfig({"l2": [{"provider": "mystery"}], "other": []}, providers={})
    assert probe_once_targets(cfg) == []


# ── hazard 1: a probe must never undo a manual override ───────────────────

def test_a_manual_override_is_skipped_not_probed():
    t = {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "profile": "l2"}
    b = FakeBreaker(overrides={("deepseek", "https://api.deepseek.com/v1", "l2"): "dead"})
    p = HealthProber(FakeConfig({"l2": [{"provider": "deepseek"}]},
                                {"deepseek": {"api_base": "https://api.deepseek.com/v1"}}),
                     settings=FakeSettings(), breaker=b, credential_store=FakeCreds({"deepseek": "k"}))
    assert "manual override" in p._skip_reason(t)
    assert b.calls == [], "record_success would have cleared the operator's override"


def test_the_cycle_records_a_skip_rather_than_telling_the_breaker_anything():
    url = "https://api.deepseek.com/v1"
    b = FakeBreaker(overrides={("deepseek", url, "l2"): "degraded"})
    p = HealthProber(FakeConfig({"l2": [{"provider": "deepseek"}]},
                                {"deepseek": {"api_base": url}}),
                     settings=FakeSettings(), breaker=b, credential_store=FakeCreds({"deepseek": "k"}))
    res = p._cycle(force=True)
    assert res and res[0].get("skipped")
    assert b.calls == []
    assert p.status()["skipped"], "a skip must be visible in the status"


# ── hazard 2: no credential must not become a fault ───────────────────────

def test_provider_with_no_stored_key_is_skipped():
    t = {"provider": "opencode", "base_url": "https://opencode.ai/zen/go/v1", "profile": "l2"}
    p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=FakeBreaker(),
                     credential_store=FakeCreds({}))
    assert "no stored credential" in p._skip_reason(t)


def test_a_keyless_local_provider_is_probed_without_a_key():
    t = {"provider": "local-sparks", "base_url": "http://192.168.1.107:18300/v1", "profile": "l2"}
    p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=FakeBreaker(),
                     credential_store=FakeCreds({}))
    assert p._skip_reason(t) == ""


# ── hazard 6: a blocked destination is not a fault ────────────────────────

def test_a_ssrf_blocked_destination_is_skipped_and_records_nothing():
    url = "http://10.0.0.9:18300/v1"
    b = FakeBreaker()
    p = HealthProber(FakeConfig({"l2": [{"provider": "local-sparks"}]},
                                {"local-sparks": {"api_base": url}}),
                     settings=FakeSettings(), breaker=b, credential_store=FakeCreds({}))
    res = p._cycle(force=True)
    assert res and res[0].get("skipped"), "blocked must be a skip"
    assert "blocked" in res[0]["detail"]
    assert b.calls == [], "the breaker must not learn a fault from an unperformed probe"


# ── hazard 3/4/5: what counts as reachable, and attribution ───────────────

def test_http_400_is_reachable_not_a_failure():
    """The provider answering is the opposite of the provider being down."""
    import urllib.error
    t = {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "profile": "l2"}
    b = FakeBreaker()
    p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=b,
                     credential_store=FakeCreds({"deepseek": "k"}))
    err = urllib.error.HTTPError("u", 400, "bad", {}, None)
    err.read = lambda: b'{"error":"bad request"}'
    with patch("urllib.request.urlopen", side_effect=err):
        out = p._probe(t, 5)
    assert out["ok"] is True and "reachable" in out["detail"]
    assert b.calls[0][0] == "success"


def test_http_500_and_transport_errors_are_failures():
    import urllib.error
    t = {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1", "profile": "l2"}
    for exc, expect in ((urllib.error.HTTPError("u", 503, "down", {}, None), "HTTP 503"),
                        (urllib.error.URLError("name resolution"), "name resolution")):
        b = FakeBreaker()
        p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=b,
                         credential_store=FakeCreds({"deepseek": "k"}))
        if isinstance(exc, urllib.error.HTTPError):
            exc.read = lambda: b'oops'
        with patch("urllib.request.urlopen", side_effect=exc):
            out = p._probe(t, 5)
        assert out["ok"] is False, f"{expect} must not count as reachable"
        assert b.calls[0][0] == "failure"
        assert b.calls[0][1:4] == ("deepseek", "https://api.deepseek.com/v1", "l2"), \
            "a probe failure is attributed to the PAIR, never the provider globally"


def test_401_and_429_are_failures_because_the_pair_cannot_serve():
    import urllib.error
    t = {"provider": "opencode", "base_url": "https://opencode.ai/zen/go/v1", "profile": "l2"}
    for code in (401, 402, 403, 429):
        b = FakeBreaker()
        p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=b,
                         credential_store=FakeCreds({"opencode": "k"}))
        err = urllib.error.HTTPError("u", code, "no", {}, None)
        err.read = lambda: b'{"error":"no"}'
        with patch("urllib.request.urlopen", side_effect=err):
            out = p._probe(t, 5)
        assert out["ok"] is False, f"HTTP {code} means this pair cannot serve"


# ── the loop must not re-probe early ──────────────────────────────────────

def test_a_pair_is_not_probed_twice_within_the_interval():
    url = "https://api.deepseek.com/v1"
    b = FakeBreaker()
    p = HealthProber(FakeConfig({"l2": [{"provider": "deepseek"}]},
                                {"deepseek": {"api_base": url}}),
                     settings=FakeSettings(), breaker=b, credential_store=FakeCreds({"deepseek": "k"}))
    t = {"provider": "deepseek", "base_url": url, "profile": "l2"}
    p._last_checked[(t["provider"], t["base_url"], t["profile"])] = time.monotonic()
    assert p._cycle() == [], "probing every tick would bill the provider every tick"
    assert len(p._cycle(force=True)) == 1, "an explicit check runs regardless"


def test_the_prober_never_dies_on_a_broken_cycle():
    p = HealthProber(FakeConfig({"l2": [{"provider": "deepseek"}]},
                                {"deepseek": {"api_base": "https://api.deepseek.com/v1"}}),
                     settings=FakeSettings(), breaker=None, credential_store=None)
    res = p._cycle(force=True)
    assert res and res[0].get("skipped") == "no circuit breaker"


def test_singleton_is_one_instance():
    assert get_health_prober(FakeConfig({})) is get_health_prober(FakeConfig({}))


def test_the_probe_uses_the_chain_step_model_not_an_empty_string():
    """A probe that sends no model is answered 400 by every real provider and
    then tricked into recording "reachable" for a provider that never served
    anything. The step's own model is what the chain would really call."""
    cfg = FakeConfig({"l2": [{"provider": "commandcode", "model": "deepseek/deepseek-v4-flash"}]},
                     providers={"commandcode": {"api_base": "https://api.commandcode.ai/provider/v1"}})
    t = probe_once_targets(cfg)[0]
    assert t["model"] == "deepseek/deepseek-v4-flash"

    seen = {}

    class Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["body"] = json.loads(req.data.decode())
        seen["url"] = req.full_url
        return Resp()

    b = FakeBreaker()
    p = HealthProber(cfg, settings=FakeSettings(), breaker=b,
                     credential_store=FakeCreds({"commandcode": "k"}))
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = p._probe(t, 5)

    assert seen["body"]["model"] == "deepseek/deepseek-v4-flash"
    assert seen["url"].endswith("/chat/completions")
    assert out["ok"] is True and out["latency_ms"] >= 0


@pytest.mark.parametrize("code,body,expect_ok", [
    (400, '{"error":{"message":"Upstream request failed: Model is unavailable."}}', False),
    (400, '{"error":{"message":"Upstream request failed: Insufficient account funds"}}', False),
    (400, '{"error":{"message":"Invalid API key provided"}}', False),
    (400, '{"error":{"type":"invalid_request_error","message":"Too small: expected string to have >=1 characters","param":"model"}}', True),
    (400, '{"error":{"message":"unknown field foo"}}', True),
    (404, "{}", True),
])
def test_a_4xx_body_naming_a_provider_fault_is_recorded_as_a_failure(code, body, expect_ok):
    import urllib.error
    from src.api.health_prober import _body_is_provider_fault
    t = {"provider": "opencode", "base_url": "https://opencode.ai/inference/openai/v1", "profile": "coder",
         "model": "deepseek-v4-flash"}
    b = FakeBreaker()
    p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=b,
                     credential_store=FakeCreds({"opencode": "k"}))
    err = urllib.error.HTTPError("u", code, "x", {}, None)
    err.read = lambda: body.encode()
    with patch("urllib.request.urlopen", side_effect=err):
        out = p._probe(t, 5)
    assert out["ok"] is expect_ok, f"{body[:50]} => expected ok={expect_ok}"
    assert _body_is_provider_fault(body) is (not expect_ok)
    if not expect_ok:
        assert b.calls[0][0] == "failure", "a broken pair must be recorded as a failure"


def test_the_detail_always_keeps_the_raw_body_for_a_human_to_judge():
    """The hints are a heuristic; the body is the evidence."""
    import urllib.error
    body = '{"error":{"message":"some brand new vendor phrasing"}}'
    t = {"provider": "opencode", "base_url": "https://opencode.ai/inference/openai/v1",
         "profile": "coder", "model": "deepseek-v4-flash"}
    p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=FakeBreaker(),
                     credential_store=FakeCreds({"opencode": "k"}))
    err = urllib.error.HTTPError("u", 400, "x", {}, None)
    err.read = lambda: body.encode()
    with patch("urllib.request.urlopen", side_effect=err):
        out = p._probe(t, 5)
    assert "brand new vendor phrasing" in out["detail"]


def test_settings_may_be_a_callable_and_is_resolved_on_every_read():
    """SettingsStore caches per INSTANCE. A store captured at boot never sees a
    write made through the app's own store — the panel saved interval 300 while
    the prober kept reading 60, with the DB holding 300."""
    live = {"row": None}

    def store():
        class S:
            def get(self, key):
                return live["row"]
        return S()

    assert load_probe_config(store)["interval_seconds"] == DEFAULT_INTERVAL_SECONDS
    live["row"] = json.dumps({"interval_seconds": 300})
    assert load_probe_config(store)["interval_seconds"] == 300, \
        "a callable must be re-resolved every read, not cached at first call"
    live["row"] = json.dumps({"interval_seconds": 900})
    assert load_probe_config(store)["interval_seconds"] == 900


def test_a_callable_that_raises_falls_back_to_defaults_not_to_off():
    def boom():
        raise RuntimeError("runtime not bound")

    cfg = load_probe_config(boom)
    assert cfg["enabled"] is True and cfg["interval_seconds"] == DEFAULT_INTERVAL_SECONDS


def test_the_running_prober_picks_up_a_cadence_change_without_a_restart():
    live = {"row": None}

    def store():
        class S:
            def get(self, key):
                return live["row"]
        return S()

    b = FakeBreaker()
    p = HealthProber(FakeConfig({"l2": [{"provider": "deepseek"}]},
                                {"deepseek": {"api_base": "https://api.deepseek.com/v1"}}),
                     settings=store, breaker=b, credential_store=FakeCreds({"deepseek": "k"}))
    assert p.status()["interval_seconds"] == DEFAULT_INTERVAL_SECONDS
    live["row"] = json.dumps({"interval_seconds": 120, "enabled": True})
    assert p.status()["interval_seconds"] == 120


def _iso(offset_seconds):
    from datetime import datetime, timedelta, timezone as tz
    return (datetime.now(tz.utc) - timedelta(seconds=offset_seconds)).isoformat()


class PeekBreaker(FakeBreaker):
    """Exposes peek() and arbitrary health fields, like the real breaker."""

    def __init__(self, rows=None, overrides=None):
        super().__init__(overrides=overrides)
        self.rows = rows or {}
        self.peeks = 0

    def peek(self, provider, base_url, profile):
        self.peeks += 1
        return self.rows.get((provider, base_url, profile), {"manual_override": None})


def test_reading_health_must_not_create_entries():
    """get_health() materializes; peek() does not. An assessor that reads with
    get_health() grows phantom health rows — this codebase already produced a
    ('/', url, '') entry in /health exactly that way."""
    url = "https://api.deepseek.com/v1"
    b = PeekBreaker()
    p = HealthProber(FakeConfig({"l2": [{"provider": "deepseek"}]},
                                {"deepseek": {"api_base": url}}),
                     settings=FakeSettings(), breaker=b, credential_store=FakeCreds({"deepseek": "k"}))
    p._skip_reason({"provider": "deepseek", "base_url": url, "profile": "l2"})
    assert b.peeks >= 1, "the read must go through peek()"
    assert b.calls == []


def test_a_pair_in_cooldown_is_not_probed():
    """It is in a known state with a deadline; the breaker half-opens it itself.
    Probing meanwhile spends money to re-confirm what is already recorded."""
    import time as _t
    url = "https://api.deepseek.com/v1"
    b = PeekBreaker(rows={(("deepseek", url, "l2")): {
        "manual_override": None, "tripped_until": _t.time() + 120, "last_success": None}})
    p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=b,
                     credential_store=FakeCreds({"deepseek": "k"}))
    assert "cooldown" in p._skip_reason({"provider": "deepseek", "base_url": url, "profile": "l2"})


def test_a_pair_whose_cooldown_expired_is_probed_again():
    import time as _t
    url = "https://api.deepseek.com/v1"
    b = PeekBreaker(rows={(("deepseek", url, "l2")): {
        "manual_override": None, "tripped_until": _t.time() - 5, "last_success": None}})
    p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=b,
                     credential_store=FakeCreds({"deepseek": "k"}))
    assert p._skip_reason({"provider": "deepseek", "base_url": url, "profile": "l2"}) == ""


def test_a_pair_real_traffic_just_served_is_not_probed():
    """The prober covers what traffic does NOT reach. l2/commandcode served 3,148
    requests in 24h; probing it too is cost with no information."""
    url = "https://api.commandcode.ai/provider/v1"
    b = PeekBreaker(rows={(("commandcode", url, "l2")): {
        "manual_override": None, "tripped_until": None, "last_success": _iso(10)}})
    p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=b, credential_store=FakeCreds({}))
    cfg = {"interval_seconds": 300}
    assert p._covered_by_traffic({"provider": "commandcode", "base_url": url, "profile": "l2"}, cfg)


def test_an_idle_pair_is_probed_even_if_it_succeeded_long_ago():
    """A 63-day-old success is exactly the silence this feature exists to break."""
    url = "https://api.deepseek.com/v1"
    b = PeekBreaker(rows={(("deepseek", url, "career")): {
        "manual_override": None, "tripped_until": None, "last_success": _iso(63*86400)}})
    p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=b, credential_store=FakeCreds({}))
    cfg = {"interval_seconds": 300}
    assert not p._covered_by_traffic({"provider": "deepseek", "base_url": url, "profile": "career"}, cfg)


def test_recent_failure_also_counts_as_recently_assessed():
    url = "https://api.deepseek.com/v1"
    b = PeekBreaker(rows={(("deepseek", url, "l2")): {
        "manual_override": None, "tripped_until": None,
        "last_success": _iso(9999), "last_failure": _iso(5)}})
    p = HealthProber(FakeConfig({}), settings=FakeSettings(), breaker=b, credential_store=FakeCreds({}))
    assert p._covered_by_traffic({"provider": "deepseek", "base_url": url, "profile": "l2"},
                                 {"interval_seconds": 300})


def test_an_explicit_check_ignores_the_traffic_cover():
    """Check now is the operator overriding the cost optimisation: \"assess this
    even though traffic is flowing\"."""
    url = "https://api.commandcode.ai/provider/v1"
    b = PeekBreaker(rows={(("commandcode", url, "l2")): {
        "manual_override": None, "tripped_until": None, "last_success": _iso(2)}})
    p = HealthProber(FakeConfig({"l2": [{"provider": "commandcode", "model": "m"}]},
                                {"commandcode": {"api_base": url}}),
                     settings=FakeSettings(), breaker=b, credential_store=FakeCreds({"commandcode": "k"}))
    res = p._cycle(force=True)
    assert res and not res[0].get("covered_by_traffic"), \
        "force must not be silenced by the traffic optimisation"


def test_the_status_separates_covered_from_skipped():
    url = "https://api.commandcode.ai/provider/v1"
    b = PeekBreaker(rows={(("commandcode", url, "l2")): {
        "manual_override": None, "tripped_until": None, "last_success": _iso(2)}})
    p = HealthProber(FakeConfig({"l2": [{"provider": "commandcode", "model": "m"}]},
                                {"commandcode": {"api_base": url}}),
                     settings=FakeSettings(), breaker=b, credential_store=FakeCreds({"commandcode": "k"}))
    p._cycle()
    st = p.status()
    assert st["covered_by_traffic"] == 1
    assert st["skipped"] == {}, "a covered pair is not a problem and must not be listed as one"


def test_the_default_cadence_is_five_minutes():
    """Measured 2026-11-01: only 5 of 20 tracked pairs carried traffic in 24h, so
    probing all of them every 60s would add ~7x the estate's real request volume."""
    assert DEFAULT_INTERVAL_SECONDS == 300
