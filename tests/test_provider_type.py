"""Provider `type` — the vendor family a provider entry belongs to.

Background: every layer that used to key on the literal provider NAME now keys
on `providers.<name>.type` (`commandcode` / `opencode` / `deepseek` / `local`).
Two things made that necessary and both are pinned here:

  * a second account of a vendor (`commandcode2`) is the same *kind* of thing as
    `commandcode` — same panel, same credential card shape, its own cookie — so
    the UI cannot be a string comparison against `'commandcode'`;
  * a self-hosted engine has no billing API at all. `local-sparks` and
    `local-zgx` and `llamacpp` are all one family, and their panel is built from
    the box's own /health rather than from credits.

Three rules are tested at every layer because breaking any one of them is
silent:

  * an entry with **no** declared type keeps working (name inference) — the
    compatibility shim that lets this ship without a config migration;
  * a declared-but-unknown type is **reported** (validate) and **never stored**
    (the write API 400s) — the UI branches its whole panel on the value, so an
    unknown one would render an empty panel rather than fail loudly;
  * the engine payload contains **no invented numbers**: a metric with no source
    is None (rendered as an em-dash), never 0.0.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.api.config import (
    PROVIDER_TYPES,
    Config,
    infer_provider_type,
    provider_type_of,
    validate_gateway_config,
)


# ══════════════════════════════════════════════════════════════════════ inference

class TestInferFromName:
    """The shim for entries written before `type` existed."""

    @pytest.mark.parametrize("name,expected", [
        ("opencode", "opencode"),
        ("opencode2", "opencode"),
        ("deepseek", "deepseek"),
        ("commandcode", "commandcode"),
        ("commandcode2", "commandcode"),   # the case that motivated the field
        ("local-sparks", "local"),
        ("local-zgx", "local"),
        ("llamacpp", "local"),
        ("OpenCode", "opencode"),          # case-insensitive
        ("  deepseek  ", "deepseek"),      # surrounding whitespace
    ])
    def test_known_names(self, name, expected):
        assert infer_provider_type(name) == expected

    @pytest.mark.parametrize("name", ["", None, "openai", "anthropic", "mybox", "sparks"])
    def test_unknown_names_are_empty_not_guessed(self, name):
        """Inference is a shim, not a second source of truth: no match → ''."""
        assert infer_provider_type(name) == ""

    def test_every_inferred_value_is_a_known_type(self):
        for name in ["opencode", "deepseek", "commandcode2", "local-sparks", "llamacpp"]:
            assert infer_provider_type(name) in PROVIDER_TYPES


class TestDeclaredWins:
    def test_declared_type_beats_the_name(self):
        # A `local` engine whose name would infer `commandcode`.
        assert provider_type_of("commandcode3", {"type": "local"}) == "local"

    def test_declared_type_is_normalised(self):
        assert provider_type_of("x", {"type": "  LOCAL "}) == "local"

    def test_invalid_declared_type_falls_back_to_inference(self):
        """A typo degrades to the old behaviour instead of an empty panel."""
        assert provider_type_of("local-sparks", {"type": "locoal"}) == "local"

    def test_invalid_declared_type_on_an_unknown_name_is_empty(self):
        assert provider_type_of("mybox", {"type": "nonsense"}) == ""

    @pytest.mark.parametrize("entry", [None, {}, "not-a-dict", [], 7])
    def test_non_dict_entry_falls_back_to_inference(self, entry):
        assert provider_type_of("deepseek", entry) == "deepseek"


class TestConfigAccessor:
    def test_seed_providers_declare_their_type(self):
        cfg = Config(store=None)
        assert cfg.provider_type("opencode") == "opencode"
        assert cfg.provider_type("deepseek") == "deepseek"
        assert cfg.provider_type("commandcode") == "commandcode"

    def test_undeclared_entry_is_inferred(self):
        cfg = Config(store=None)
        cfg.raw["providers"]["commandcode2"] = {"api_base": "https://x/v1", "models": []}
        assert cfg.provider_type("commandcode2") == "commandcode"

    def test_local_entry_resolves_to_local(self):
        cfg = Config(store=None)
        cfg.raw["providers"]["local-sparks"] = {
            "api_base": "http://192.168.1.107:18300/v1", "models": ["GLM"]}
        assert cfg.provider_type("local-sparks") == "local"

    def test_unknown_provider_is_empty(self):
        cfg = Config(store=None)
        assert cfg.provider_type("not-a-provider") == ""

    def test_provider_types_maps_every_provider(self):
        cfg = Config(store=None)
        cfg.raw["providers"]["local-zgx"] = {"api_base": "http://h/v1", "models": []}
        types = cfg.provider_types()
        assert set(types) == set(cfg.providers)
        assert types["local-zgx"] == "local"


# ══════════════════════════════════════════════════════════════════════ validation

class TestValidation:
    def _base(self, providers):
        return {
            "providers": providers,
            "profiles": {"l2": {"chain": [{"provider": "deepseek", "model": "m"}]}},
        }

    def test_valid_types_produce_no_problem(self):
        problems = validate_gateway_config(self._base({
            "deepseek": {"type": "deepseek", "api_base": "https://x/v1", "models": []},
            "local-sparks": {"type": "local", "api_base": "http://h/v1", "models": []},
        }))
        assert problems == []

    def test_absent_type_produces_no_problem(self):
        """Every entry that predates the field must stay valid."""
        problems = validate_gateway_config(self._base({
            "deepseek": {"api_base": "https://x/v1", "models": []},
        }))
        assert problems == []

    def test_unknown_type_is_reported(self):
        problems = validate_gateway_config(self._base({
            "deepseek": {"type": "deepsek", "api_base": "https://x/v1", "models": []},
        }))
        assert len(problems) == 1
        assert "deepsek" in problems[0] and "deepseek" in problems[0]

    def test_non_string_type_is_reported(self):
        problems = validate_gateway_config(self._base({
            "deepseek": {"type": 42, "api_base": "https://x/v1", "models": []},
        }))
        assert len(problems) == 1 and "42" in problems[0]

    def test_empty_string_type_is_not_reported(self):
        problems = validate_gateway_config(self._base({
            "deepseek": {"type": "  ", "api_base": "https://x/v1", "models": []},
        }))
        assert problems == []

    def test_seed_config_is_valid(self):
        from src.api.config import SEED_CONFIG
        assert validate_gateway_config(SEED_CONFIG) == []


# ══════════════════════════════════════════════════════════════════════ the API

class ApiHandler:
    """Direct-call handler (skips BaseHTTPRequestHandler.__init__)."""

    def make(self, config, path="/", method="GET", body=None):
        from src.server import LCPHandler

        class H(LCPHandler):
            def __init__(self):
                self.path = path
                self.command = method
                self.headers = {}
                self.request_version = "HTTP/1.1"
                self.requestline = f"{method} {path} HTTP/1.1"
                self.raw_requestline = f"{method} {path} HTTP/1.1".encode()
                self.client_address = ("127.0.0.1", 0)
                self.send_response = MagicMock()
                self.send_header = MagicMock()
                self.end_headers = MagicMock()
                self.wfile = MagicMock()
                self.rfile = MagicMock()
                raw = json.dumps(body) if isinstance(body, dict) else (body or "{}")
                raw_bytes = raw.encode() if isinstance(raw, str) else raw
                self.rfile.read = MagicMock(return_value=raw_bytes)
                if body:
                    self.headers["Content-Length"] = str(len(raw_bytes))
                self.log_error = MagicMock()

        h = H()
        h.config = config
        return h

    @staticmethod
    def json_of(h):
        for call in h.wfile.write.call_args_list:
            try:
                return json.loads(call[0][0])
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                continue
        return {}

    @staticmethod
    def status_of(h):
        return h.send_response.call_args[0][0] if h.send_response.call_args else None


@pytest.fixture
def live_cfg():
    """A real Config with the prod-shaped provider list (names only, no types)."""
    cfg = Config(store=None)
    cfg.raw["providers"] = {
        "opencode": {"api_base": "https://opencode.ai/zen/v1", "models": ["m1"]},
        "deepseek": {"api_base": "https://api.deepseek.com/v1", "models": ["m2"]},
        "commandcode": {"api_base": "https://api.commandcode.ai/provider/v1", "models": ["m3"]},
        "commandcode2": {"api_base": "https://api.commandcode.ai/provider/v1", "models": ["m3"]},
        "local-sparks": {"api_base": "http://192.168.1.107:18300/v1", "models": ["GLM-5.3-Flash-EXL3"]},
    }
    cfg.raw["profiles"] = {"l2": {"chain": [{"provider": "local-sparks", "model": "GLM-5.3-Flash-EXL3"}]}}
    cfg.save = MagicMock()
    return cfg


class TestProvidersListApi:
    def test_every_provider_reports_a_type_and_whether_it_was_declared(self, live_cfg):
        h = ApiHandler().make(live_cfg, "/api/providers")
        h._serve_providers_list()
        body = ApiHandler.json_of(h)
        provs = body["providers"]
        assert provs["commandcode2"]["type"] == "commandcode"
        assert provs["local-sparks"]["type"] == "local"
        assert provs["deepseek"]["type"] == "deepseek"
        assert all(p["type_declared"] is False for p in provs.values())

    def test_a_declared_type_is_marked_declared(self, live_cfg):
        live_cfg.raw["providers"]["local-sparks"]["type"] = "local"
        h = ApiHandler().make(live_cfg, "/api/providers")
        h._serve_providers_list()
        body = ApiHandler.json_of(h)
        assert body["providers"]["local-sparks"]["type"] == "local"
        assert body["providers"]["local-sparks"]["type_declared"] is True

    def test_payload_is_json_serialisable_with_a_mock_config(self, mock_handler):
        """`_serve_providers_list` must not leak a non-string into JSON."""
        cfg = MagicMock()
        cfg.providers = {"deepseek": {"api_base": "https://x/v1", "models": []}}
        cfg.profiles = {}
        h = ApiHandler().make(cfg, "/api/providers")
        h._serve_providers_list()
        assert ApiHandler.json_of(h)["providers"]["deepseek"]["type"] == "deepseek"


class TestProviderWriteApi:
    def test_create_stores_a_valid_type(self, live_cfg):
        h = ApiHandler().make(live_cfg, "/api/providers", "POST", {
            "name": "local-zgx", "api_base": "http://192.168.1.108:18300/v1",
            "models": ["m"], "type": "local"})
        h._serve_provider_create()
        assert live_cfg.raw["providers"]["local-zgx"]["type"] == "local"

    def test_create_rejects_an_unknown_type_and_writes_nothing(self, live_cfg):
        h = ApiHandler().make(live_cfg, "/api/providers", "POST", {
            "name": "bogus", "api_base": "https://x/v1", "models": [], "type": "llm"})
        h._serve_provider_create()
        assert ApiHandler.status_of(h) == 400
        assert "local" in ApiHandler.json_of(h)["error"]
        assert "bogus" not in live_cfg.raw["providers"]

    def test_create_without_a_type_is_allowed(self, live_cfg):
        h = ApiHandler().make(live_cfg, "/api/providers", "POST", {
            "name": "plainbox", "api_base": "https://x/v1", "models": []})
        h._serve_provider_create()
        assert "type" not in live_cfg.raw["providers"]["plainbox"]

    def test_update_sets_a_type(self, live_cfg):
        h = ApiHandler().make(live_cfg, "/api/providers/local-sparks", "PUT",
                              {"type": "local", "api_base": "http://h/v1"})
        h._serve_provider_update("local-sparks")
        assert live_cfg.raw["providers"]["local-sparks"]["type"] == "local"

    def test_update_with_empty_type_clears_the_declaration(self, live_cfg):
        live_cfg.raw["providers"]["local-sparks"]["type"] = "local"
        h = ApiHandler().make(live_cfg, "/api/providers/local-sparks", "PUT",
                              {"type": "", "api_base": "http://h/v1"})
        h._serve_provider_update("local-sparks")
        assert "type" not in live_cfg.raw["providers"]["local-sparks"]

    def test_update_rejects_an_unknown_type(self, live_cfg):
        h = ApiHandler().make(live_cfg, "/api/providers/deepseek", "PUT",
                              {"type": "opencode-2", "api_base": "https://x/v1"})
        h._serve_provider_update("deepseek")
        assert ApiHandler.status_of(h) == 400
        assert "type" not in live_cfg.raw["providers"]["deepseek"]

    def test_update_leaves_the_type_alone_when_absent(self, live_cfg):
        live_cfg.raw["providers"]["deepseek"]["type"] = "deepseek"
        h = ApiHandler().make(live_cfg, "/api/providers/deepseek", "PUT",
                              {"api_base": "https://new/v1"})
        h._serve_provider_update("deepseek")
        assert live_cfg.raw["providers"]["deepseek"]["type"] == "deepseek"


# ══════════════════════════════════════════════════════════════ engine endpoint

HEALTH = {
    "ok": True, "busy": True, "backend": "tensorfold",
    "requests_total": 5000,
    "prompt_tokens_total": 2_000_000,
    "completion_tokens_total": 100_000,
    "cached_tokens_total": 1_500_000,
    "prefill_seconds_total": 500.0,
    "decode_seconds_total": 2000.0,
    "drafted_total": 40_000,
    "accepted_total": 30_000,
    "pool_tokens": 4096, "pool_free_tokens": 1024,
    "kept_bytes": 2 * 1024 ** 3, "kept_bytes_cap": 4 * 1024 ** 3,
    "context_length": 1_000_000,
    "streams": {"max": 8, "decoding": 2, "prefilling": 1},
}


class TestDeriveEngineMetrics:
    def _derive(self, health, counters=None):
        from src.server.endpoints import _derive_engine_metrics
        return _derive_engine_metrics(health, counters or {})

    def test_ratios_are_computed_from_the_engine_counters(self):
        d = self._derive(HEALTH)
        assert d["decode_tokens_per_s"] == 50.0          # 100k / 2000s
        assert d["prefill_tokens_per_s"] == 4000.0       # 2M / 500s
        assert d["prompt_cache_hit_pct"] == 75.0         # 1.5M / 2M
        assert d["mtp_accept_pct"] == 75.0               # 30k / 40k
        assert d["pool_used_pct"] == 75.0                # (4096-1024)/4096
        assert d["kept_cap_used_pct"] == 50.0

    def test_stream_counters_become_in_flight(self):
        d = self._derive(HEALTH)
        assert d["requests_running"] == 3                # decoding + prefilling
        assert d["lanes"] == 8
        assert d["context_length"] == 1_000_000

    def test_queue_depth_comes_from_the_metrics_exposition(self):
        d = self._derive(HEALTH, {"tensorfold:requests_waiting": 4.0})
        assert d["requests_waiting"] == 4.0

    def test_kv_ratio_is_scaled_to_a_percentage(self):
        d = self._derive(HEALTH, {"tensorfold:kv_cache_usage_ratio": 0.42})
        assert d["kv_cache_usage_pct"] == 42.0

    def test_a_metric_with_no_source_is_none_never_zero(self):
        """0.0 would read as 'measured, and it is empty' — a lie."""
        d = self._derive({"ok": True, "streams": {}})
        for key in ("decode_tokens_per_s", "prefill_tokens_per_s",
                    "prompt_cache_hit_pct", "mtp_accept_pct", "pool_used_pct",
                    "kept_cap_used_pct", "requests_waiting", "context_length"):
            assert d[key] is None, key

    def test_zero_denominators_do_not_raise(self):
        d = self._derive({"prompt_tokens_total": 0, "cached_tokens_total": 0,
                          "pool_tokens": 0, "kept_bytes_cap": 0,
                          "drafted_total": 0, "completion_tokens_total": 0})
        assert d["prompt_cache_hit_pct"] is None
        assert d["mtp_accept_pct"] is None
        assert d["pool_used_pct"] is None
        assert d["kept_cap_used_pct"] is None
        assert d["decode_tokens_per_s"] is None

    def test_bool_is_not_accepted_as_a_number(self):
        d = self._derive({"ok": True, "context_length": True})
        assert d["context_length"] is None


@pytest.fixture
def allow_lan_engine(monkeypatch):
    """The prod env allowlists the Spark box; the engine read requires it."""
    monkeypatch.setenv("LCP_SSRF_ALLOWLIST", "192.168.1.107/32")


class TestEngineEndpoint:
    def _call(self, cfg, name):
        h = ApiHandler().make(cfg, f"/api/providers/{name}/engine")
        h._serve_provider_engine(name)
        return ApiHandler.status_of(h), ApiHandler.json_of(h)

    def test_unknown_provider_is_404(self, live_cfg):
        status, _ = self._call(live_cfg, "nope")
        assert status == 404

    def test_non_http_api_base_is_reported_not_fetched(self, live_cfg):
        live_cfg.raw["providers"]["local-sparks"]["api_base"] = "not-a-url"
        _, body = self._call(live_cfg, "local-sparks")
        assert body["reachable"] is False
        assert body["error"]

    def test_unreachable_engine_is_200_with_a_reason(self, live_cfg, allow_lan_engine):
        """The page must still render its gateway-side numbers."""
        with patch("src.server.endpoints._engine_get_json",
                   return_value=(None, "URLError: connection refused")):
            status, body = self._call(live_cfg, "local-sparks")
        assert status == 200
        assert body["reachable"] is False
        assert "connection refused" in body["error"]

    def test_non_json_health_body_is_reported(self, live_cfg, allow_lan_engine):
        with patch("src.server.endpoints._engine_get_json", return_value=(None, None)):
            _, body = self._call(live_cfg, "local-sparks")
        assert body["reachable"] is False and body["error"]

    def test_happy_path_returns_health_and_derived(self, live_cfg, allow_lan_engine):
        with patch("src.server.endpoints._engine_get_json", return_value=(HEALTH, None)), \
             patch("src.server.endpoints._engine_get_counters",
                   return_value={"tensorfold:requests_waiting": 1.0}):
            status, body = self._call(live_cfg, "local-sparks")
        assert status == 200
        assert body["reachable"] is True
        assert body["type"] == "local"
        assert body["engine"] == "http://192.168.1.107:18300"   # /v1 stripped
        assert body["derived"]["decode_tokens_per_s"] == 50.0
        assert body["declared_models"] == ["GLM-5.3-Flash-EXL3"]
        assert body["fetched_at"].endswith("+00:00")

    def test_private_destination_is_refused_by_the_ssrf_guard(self, live_cfg, monkeypatch):
        monkeypatch.delenv("LCP_SSRF_ALLOWLIST", raising=False)
        with patch("src.server.endpoints._engine_get_json", return_value=(HEALTH, None)) as get:
            _, body = self._call(live_cfg, "local-sparks")
        assert body["reachable"] is False
        assert "non-public address" in body["error"]
        get.assert_not_called()

    def test_an_allowlisted_private_destination_is_read(self, live_cfg, monkeypatch):
        monkeypatch.setenv("LCP_SSRF_ALLOWLIST", "192.168.1.107/32")
        with patch("src.server.endpoints._engine_get_json", return_value=(HEALTH, None)), \
             patch("src.server.endpoints._engine_get_counters", return_value={}):
            _, body = self._call(live_cfg, "local-sparks")
        assert body["reachable"] is True

    def test_a_provider_with_no_models_declared_reports_an_empty_list(self, live_cfg, allow_lan_engine):
        live_cfg.raw["providers"]["local-sparks"]["models"] = None
        with patch("src.server.endpoints._engine_get_json", return_value=(HEALTH, None)), \
             patch("src.server.endpoints._engine_get_counters", return_value={}):
            _, body = self._call(live_cfg, "local-sparks")
        assert body["declared_models"] == []


# ══════════════════════════════════════════════════════════════ the route table

class TestRouteRegistration:
    def test_engine_route_is_reachable_through_the_route_table(self, live_cfg, allow_lan_engine):
        with patch("src.server.endpoints._engine_get_json", return_value=(HEALTH, None)), \
             patch("src.server.endpoints._engine_get_counters", return_value={}):
            h = ApiHandler().make(live_cfg, "/api/providers/local-sparks/engine")
            h.config = live_cfg
            assert h._route_table().dispatch(h, "GET") is True
            assert ApiHandler.json_of(h)["provider"] == "local-sparks"

    def test_the_bare_list_route_is_not_shadowed(self, live_cfg):
        h = ApiHandler().make(live_cfg, "/api/providers")
        h.config = live_cfg
        assert h._route_table().dispatch(h, "GET") is True
        assert "providers" in ApiHandler.json_of(h)
