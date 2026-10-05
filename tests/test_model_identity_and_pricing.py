"""Model identity: one canonical model, several provider spellings.

Covers the defect reported on 2026-10-05 — a model offered as
``deepseek-flash`` (deepseek), ``deepseek-v4.1-flash`` (opencode) and
``deepseek/deepseek-v4.1-flash`` (commandcode) was treated as three unrelated
models, so pricing missed, ``logical_model_name`` failed to group them, and a
pricing miss on the response path became HTTP 500 LCP-4001.

Four behaviours are pinned here:

1. ``logical_model_name`` resolves every spelling of one model to one logical
   name, including a vendor-prefixed ID the registry maps only under a sibling
   spelling (the alias index must be consulted BEFORE path stripping).
2. ``ModelConfig.resolve_pricing`` prices every spelling from one row, prefers
   a provider-specific row over a shared one, and NEVER raises.
3. ``calculate_cost`` records a $0 row instead of throwing when a model is
   unpriceable, so cost accounting cannot turn a good completion into a 500.
4. The provider cost plugins price every spelling of DeepSeek-V4.1-Flash.
"""

import json
import sqlite3

import pytest

from src.api import config as config_module
from src.api import router as router_module
from src.api.config import Config, SEED_CONFIG
from src.api.cost_plugins.commandcode import CommandCodeCostPlugin
from src.api.cost_plugins.deepseek import DeepSeekCostPlugin
from src.api.cost_plugins.opencode import OpenCodeCostPlugin
from src.api.request_pipeline import calculate_cost, _lookup_pricing
from src.api.router import (
    _alias_to_logical,
    get_model_registry,
    invalidate_registry_cache,
    logical_model_name,
)

# The registry row the operator half-built at 13:23, completed: one logical
# model, benchmark key `deepseek-v4.1-flash`, three provider spellings.
V41_MAPPINGS = {
    "deepseek": "deepseek-flash",
    "opencode": "deepseek-v4.1-flash",
    "commandcode": "deepseek/deepseek-v4.1-flash",
}


@pytest.fixture
def registry_db(tmp_path, monkeypatch):
    """A real SQLite registry DB holding only the V4.1-Flash entry."""
    db = tmp_path / "costs.db"
    con = sqlite3.connect(db)
    con.execute(
        """CREATE TABLE model_registry (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               logical_name TEXT NOT NULL UNIQUE,
               benchmark_key TEXT,
               active_release TEXT,
               provider_mappings_json TEXT,
               benchmark_release TEXT,
               quantization TEXT,
               updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    con.execute(
        "INSERT INTO model_registry (logical_name, benchmark_key, provider_mappings_json)"
        " VALUES (?,?,?)",
        ("deepseek-flash", "deepseek-v4.1-flash", json.dumps(V41_MAPPINGS)),
    )
    con.execute(
        "INSERT INTO model_registry (logical_name, benchmark_key, provider_mappings_json)"
        " VALUES (?,?,?)",
        ("deepseek-v4-pro", "deepseek-v4-pro",
         json.dumps({"deepseek": "deepseek-v4-pro",
                     "opencode": "deepseek-v4-pro",
                     "commandcode": "deepseek/deepseek-v4-pro"})),
    )
    con.commit()
    con.close()
    invalidate_registry_cache()
    yield str(db)
    invalidate_registry_cache()


def _cfg(pricing, limits=None, db_path=None):
    """A Config with an explicit pricing section and no DB store.

    ``db_path`` points the registry lookups at the test's registry DB, which is
    where ``_canonical_names`` reads from (``database.path``).
    """
    import copy
    seed = copy.deepcopy(SEED_CONFIG)
    seed["pricing"] = list(pricing)
    if limits is not None:
        seed["model_limits"] = dict(limits)
    cfg = Config(store=None, seed=seed)
    if db_path:
        cfg._data["database"]["path"] = db_path
    return cfg


# ── 1. logical_model_name groups every spelling ───────────────────────────


class TestLogicalModelName:
    def test_every_spelling_of_v41_flash_groups_to_one_logical_name(self, registry_db):
        """The operator's report, at the identity layer.

        One model, four spellings — the deepseek API name, the benchmark key
        that opencode serves it under, commandcode's prefixed catalogue ID, and
        the prefixed form of the deepseek name itself. All must resolve to the
        SAME logical name, or a rule written in one spelling cannot match a
        chain step carrying another.
        """
        names = {
            logical_model_name(m, registry_db)
            for m in ("deepseek-flash",                # deepseek (current API name)
                      "deepseek-v4.1-flash",           # benchmark key / opencode
                      "deepseek/deepseek-v4.1-flash",  # commandcode catalogue ID
                      "deepseek/deepseek-flash")       # prefixed deepseek name
        }
        assert names == {"deepseek-flash"}, f"spellings did not group: {names}"

    def test_prefixed_id_the_registry_maps_only_under_a_sibling_still_groups(self, registry_db):
        """A vendor prefix must not manufacture a second logical name.

        ``deepseek/deepseek-v4.1-flash`` is declared verbatim, but a prefixed
        spelling that the registry declares only as a BARE value under another
        provider still has to group. Normalizing before consulting the alias
        index would leave the prefixed form unmapped, so the index has to see
        the last path segment too.
        """
        assert logical_model_name("deepseek/deepseek-flash", registry_db) == "deepseek-flash"
        assert logical_model_name("commandcode/deepseek-v4.1-flash", registry_db) == "deepseek-flash"

    def test_an_undeclared_spelling_keeps_its_own_identity(self, registry_db):
        """Grouping is driven by the registry, not by name similarity.

        ``deepseek-v4-flash`` is NOT declared on the V4.1-Flash row, so it does
        not get folded into it. On the deepseek provider the two are the same
        upstream model (DeepSeek serves the retired ``deepseek-v4-flash`` name
        with DeepSeek-V4.1-Flash), but whether the gateway should treat the
        older ``deepseek-v4-flash`` REGISTRY ROW as the same model is a data
        decision, not something name-matching may decide silently. Declaring
        it as a spelling on one row is what merges them.
        """
        assert logical_model_name("deepseek-v4-flash", registry_db) == "deepseek-v4-flash"

    def test_pro_and_flash_are_not_confused(self, registry_db):
        assert logical_model_name("deepseek/deepseek-v4-pro", registry_db) == "deepseek-v4-pro"
        assert logical_model_name("deepseek-flash", registry_db) == "deepseek-flash"

    def test_unknown_name_falls_back_to_normalization(self, registry_db):
        """A llama.cpp path keeps working — the registry is not a hard gate."""
        assert (logical_model_name("/models/qwen3.6-27b-q4_k_m.gguf", registry_db)
                == "qwen3.6-27b-q4_k_m")
        assert logical_model_name("totally-unknown-model", registry_db) == "totally-unknown-model"

    def test_empty_name_is_returned_unchanged(self, registry_db):
        assert logical_model_name("", registry_db) == ""

    def test_alias_index_is_cached_but_rebuilt_when_the_registry_changes(self, registry_db):
        """Hot path: not rebuilt per call; correctness: rebuilt on effect."""
        first = _alias_to_logical(get_model_registry(registry_db))
        second = _alias_to_logical(get_model_registry(registry_db))
        assert first is second

        invalidate_registry_cache()
        assert _alias_to_logical(get_model_registry(registry_db)) is not first

    def test_missing_registry_still_normalizes(self, tmp_path, monkeypatch):
        """An unreadable/seeding-failed registry must not break routing."""
        monkeypatch.setattr(router_module, "get_model_registry", lambda db=None: {})
        invalidate_registry_cache()
        # Falls back to last-segment normalization — the pre-registry behaviour.
        assert logical_model_name("deepseek/deepseek-v4.1-flash", "nope.db") == \
            "deepseek-v4.1-flash"


# ── 2. resolve_pricing ────────────────────────────────────────────────────

_FLASH = {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6}


class TestResolvePricing:
    def test_exact_pair_wins(self):
        cfg = _cfg([{"provider": "deepseek", "model": "deepseek-flash", **_FLASH}])
        assert cfg.resolve_pricing("deepseek", "deepseek-flash")["cache_miss"] == 0.15

    def test_canonical_name_prices_a_provider_side_spelling(self, registry_db):
        """opencode's ``deepseek-v4.1-flash`` is priced from the deepseek row."""
        cfg = _cfg([{"provider": "deepseek", "model": "deepseek-flash", **_FLASH}],
                   db_path=registry_db)
        row = cfg.resolve_pricing("opencode", "deepseek-v4.1-flash")
        assert row is not None and row["model"] == "deepseek-flash"

    def test_cross_provider_grouping_uses_any_providers_row(self, registry_db):
        """One price declared once serves every provider — the operator's ask."""
        cfg = _cfg([{"provider": "deepseek", "model": "deepseek-flash", **_FLASH}],
                   db_path=registry_db)
        for provider, model in (("opencode", "deepseek-v4.1-flash"),
                                ("commandcode", "deepseek/deepseek-v4.1-flash"),
                                ("deepseek", "deepseek/deepseek-flash")):
            row = cfg.resolve_pricing(provider, model)
            assert row is not None, f"{provider}/{model} unresolved"
            assert row["cache_miss"] == 0.15

    def test_provider_specific_row_beats_the_shared_one(self, registry_db):
        cfg = _cfg([
            {"provider": "deepseek", "model": "deepseek-flash", **_FLASH},
            {"provider": "opencode", "model": "deepseek-flash",
             "cache_hit": 0.01, "cache_miss": 0.5, "output": 2.0},
        ], db_path=registry_db)
        assert cfg.resolve_pricing("opencode", "deepseek-flash")["cache_miss"] == 0.5
        assert cfg.resolve_pricing("deepseek", "deepseek-flash")["cache_miss"] == 0.15

    def test_wildcard_provider_row_matches_any_provider(self):
        cfg = _cfg([{"provider": "*", "model": "some-model", **_FLASH}])
        assert cfg.resolve_pricing("anything", "some-model") is not None
        assert cfg.resolve_pricing("other", "some-model") is not None

    def test_miss_returns_none_and_does_not_raise(self, registry_db):
        cfg = _cfg([{"provider": "deepseek", "model": "deepseek-flash", **_FLASH}],
                   db_path=registry_db)
        assert cfg.resolve_pricing("deepseek", "no-such-model") is None

    def test_get_pricing_still_raises_for_callers_that_want_strictness(self):
        """The strict contract is preserved, only rewired through the registry."""
        from src.api.exceptions import ConfigError

        cfg = _cfg([{"provider": "deepseek", "model": "deepseek-flash", **_FLASH}])
        assert cfg.get_pricing("deepseek", "deepseek-flash")["cache_miss"] == 0.15
        with pytest.raises(ConfigError):
            cfg.get_pricing("deepseek", "no-such-model")

    def test_empty_model_is_a_miss_not_a_crash(self):
        cfg = _cfg([{"provider": "deepseek", "model": "deepseek-flash", **_FLASH}])
        assert cfg.resolve_pricing("deepseek", "") is None


# ── 3. cost accounting never fails the response ────────────────────────────


class _StubConfig:
    """A duck-typed config with no registry awareness at all."""

    def __init__(self, pricing=None, raises=False):
        self._pricing = pricing
        self._raises = raises
        self.model_limits = {}

    def get_pricing(self, provider, model):
        if self._raises:
            raise RuntimeError("No pricing found")
        if self._pricing is None:
            raise RuntimeError("No pricing found")
        return self._pricing

    def get_provider_cache_config(self, provider):
        return {"hit_field": "prompt_cache_hit_tokens"}


class TestCostAccountingIsNotFatal:
    def test_unpriced_model_records_zero_instead_of_raising(self, monkeypatch):
        resp = {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}}
        monkeypatch.setattr("src.api.request_pipeline.get_registry",
                            lambda: type("R", (), {"calculate_cost": staticmethod(lambda *a: None)})())
        out = calculate_cost("deepseek", "mystery-model", {}, resp, _StubConfig(raises=True))
        assert out["cost"] == 0.0
        assert out["priced"] is False
        assert out["prompt_tokens"] == 1000        # tokens are still recorded
        assert out["completion_tokens"] == 500

    def test_priced_model_is_marked_priced(self, monkeypatch):
        resp = {"usage": {"prompt_tokens": 1000, "completion_tokens": 1000}}
        monkeypatch.setattr("src.api.request_pipeline.get_registry",
                            lambda: type("R", (), {"calculate_cost": staticmethod(lambda *a: None)})())
        out = calculate_cost("deepseek", "m", {}, resp,
                             _StubConfig(pricing={"cache_hit": 0.01, "cache_miss": 0.5,
                                                  "output": 1.0}))
        assert out["priced"] is True and out["cost"] > 0

    def test_lookup_prefers_resolve_pricing_and_swallows_errors(self):
        class Cfg:
            model_limits = {}

            def resolve_pricing(self, provider, model):
                raise RuntimeError("boom")

            def get_pricing(self, provider, model):
                return {"cache_hit": 0.1, "cache_miss": 0.2, "output": 0.3}

        assert _lookup_pricing(Cfg(), "p", "m")["cache_miss"] == 0.2

    def test_lookup_treats_non_dict_returns_as_no_answer(self):
        """A mock/stub config returning a Mock must not reach the arithmetic."""
        class Cfg:
            model_limits = {}

            def resolve_pricing(self, provider, model):
                return object()          # not a dict

        assert _lookup_pricing(Cfg(), "p", "m") is None


# ── 4. plugins price every spelling ───────────────────────────────────────

_SPELLINGS = [
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4.1-flash",
    "deepseek-v4-flash-vision-exp",
]


@pytest.mark.parametrize("plugin_cls", [DeepSeekCostPlugin, OpenCodeCostPlugin,
                                        CommandCodeCostPlugin])
def test_plugins_price_every_v41_flash_spelling(plugin_cls):
    plugin = plugin_cls()
    for name in _SPELLINGS:
        pricing = plugin.get_pricing(name)
        assert pricing is not None, f"{plugin_cls.__name__} cannot price {name}"
        assert pricing["cache_miss"] == 0.15
        assert plugin.calculate_cost(name, {"prompt_tokens": 1_000_000,
                                            "completion_tokens": 0}) == pytest.approx(0.15)


def test_commandcode_prices_the_prefixed_catalogue_id():
    """The ID the Provider API actually expects resolves to a price."""
    plugin = CommandCodeCostPlugin()
    assert plugin.get_pricing("deepseek/deepseek-v4.1-flash")["cache_miss"] == 0.15
    assert plugin.get_pricing("deepseek/deepseek-v4-flash")["cache_miss"] == 0.15


def test_flash_entries_share_one_price_object_per_table():
    """One model = one price. A change must not land on only some spellings."""
    from src.api.cost_plugins import commandcode as cc
    from src.api.cost_plugins import deepseek as ds
    from src.api.cost_plugins import opencode as oc

    for table in (ds._PRICING, oc._OPENCODE_PRICING, cc._COMMANDCODE_PRICING):
        flash = [table[n]["cache_miss"] for n in _SPELLINGS]
        assert len(set(flash)) == 1, f"flash spellings disagree in {table}"
        assert table["deepseek-flash"] == ds._FLASH_PRICING


# ── 5. seed config is self-consistent ─────────────────────────────────────


def test_seed_config_prices_every_spelling_used_by_the_chain():
    from src.api.config import SEED_CONFIG

    pairs = {(r["provider"], r["model"]) for r in SEED_CONFIG["pricing"]}
    for model in ("deepseek-flash", "deepseek-v4-flash", "deepseek-v4.1-flash"):
        for provider in ("deepseek", "opencode"):
            assert (provider, model) in pairs, f"seed pricing missing {provider}/{model}"
    for model in ("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash",
                  "deepseek/deepseek-v4-pro"):
        assert ("commandcode", model) in pairs, f"seed pricing missing commandcode/{model}"
