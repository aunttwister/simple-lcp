"""Final gap batch: key_manager legacy migration + spend thresholds,
reasoning_store prune/clear/singleton, and remaining endpoint blocks
(manual score edges, registry upsert quantization, usage totals, setup page)."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def temp_db():
    import tempfile as _t
    from src.api.models import get_engine, Base
    fd, path = _t.mkstemp(suffix=".db")
    os.close(fd)
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()
    for ext in ["", "-wal", "-shm"]:
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


@pytest.fixture(autouse=True)
def _setup_handler_config(temp_db):
    """Ensure LCPHandler.config is set for TestHandler-based endpoint tests."""
    from src.server import LCPHandler
    from src.api.key_manager import KeyManager
    import src.api.key_manager as key_manager_mod
    key_manager_mod._key_manager = KeyManager(temp_db, "data")
    cfg = MagicMock()
    cfg.server = {"port": 8734, "default_profile": "l2"}
    cfg.profiles = {
        "l2": {
            "forbidden_tools": [],
            "chain": [{"provider": "deepseek", "model": "deepseek-v4-pro", "base_url": "https://t/v1"}],
            "auth_required": False,
        },
    }
    cfg.providers = {"deepseek": {"api_base": "https://t/v1", "models": ["deepseek-v4-pro"]}}
    cfg.pricing = [{"provider": "deepseek", "model": "deepseek-v4-pro", "cache_hit": 0.01, "cache_miss": 0.5, "output": 1.0}]
    cfg.circuit_breaker = {"failures_dead": 5, "dead_cooldown_seconds": 300, "failures_degraded": 3, "degraded_cooldown_seconds": 60}
    cfg.database = {"path": "/tmp/test.db", "wal_mode": True}
    cfg.model_limits = {}
    cfg.get_profile = lambda name: cfg.profiles.get(name)
    cfg.get_pricing = lambda provider, model: cfg.pricing[0]
    cfg.get_provider_key = lambda name: "test-key"
    cfg.check_reload = MagicMock()
    cfg.raw = {"providers": dict(cfg.providers), "profiles": dict(cfg.profiles)}
    cfg.save = MagicMock()
    LCPHandler.config = cfg
    LCPHandler.engine = temp_db


# ── key_manager: legacy JSON migration ───────────────────────────────────────

class TestKeyMigration:
    def test_migrates_legacy_json(self, temp_db, tmp_path):
        from src.api.key_manager import KeyManager
        import json as _json
        legacy = {
            "keys": [
                {"hash": "abc123", "id": "key1", "label": "Legacy", "profile": "l2", "created": "2026-01-01T00:00:00"},
                {"hash": "def456", "id": "key2", "label": "", "profile": "l1", "created": "2026-01-01T00:00:00"},
            ]
        }
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        (data_dir / "api_keys.json").write_text(_json.dumps(legacy))
        km = KeyManager(temp_db, str(data_dir))
        keys = km.list_keys()
        assert len(keys) == 2
        names = {k["name"] for k in keys}
        assert "Legacy" in names
        # File renamed to .bak.
        assert not (data_dir / "api_keys.json").exists()
        assert (data_dir / "api_keys.json.bak").exists()

    def test_migrate_skips_existing_hash(self, temp_db, tmp_path):
        from src.api.key_manager import KeyManager
        import json as _json
        legacy = {"keys": [{"hash": "dup", "id": "k", "label": "K", "profile": "l2"}]}
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        (data_dir / "api_keys.json").write_text(_json.dumps(legacy))
        KeyManager(temp_db, str(data_dir))
        km2 = KeyManager(temp_db, str(data_dir))  # second run: hash already exists
        keys = km2.list_keys()
        assert len(keys) == 1  # no duplicate

    def test_migrate_no_file_is_noop(self, temp_db, tmp_path):
        from src.api.key_manager import KeyManager
        km = KeyManager(temp_db, str(tmp_path / "nodata"))
        assert km.list_keys() == []


# ── reasoning_store: prune/clear ─────────────────────────────────────────────

class TestReasoningStorePrune:
    def test_clear_empties_store(self):
        from src.api.reasoning_store import ReasoningStore
        store = ReasoningStore()
        store.capture("a", "x")
        store.clear()
        assert len(store) == 0

    def test_prune_removes_expired(self):
        import time as _t
        from unittest.mock import patch as _patch
        from src.api.reasoning_store import ReasoningStore
        store = ReasoningStore(ttl_seconds=1, max_entries=10)
        store.capture("a", "x")
        with _patch("time.time", return_value=_t.time() + 60):
            store._prune()
        assert len(store) == 0

    def test_singleton_roundtrip(self):
        from src.api import reasoning_store
        reasoning_store._reasoning_store = None
        s = reasoning_store.get_reasoning_store()
        s.capture("c", "v")
        assert reasoning_store.get_reasoning_store().get_for_tool_call_id("c") == "v"
        reasoning_store._reasoning_store = None


# ── Endpoint: manual score edge cases ────────────────────────────────────────

class TestManualScoreEdges:
    def _handler(self, temp_db, body):
        from tests.test_server import TestHandler
        return TestHandler(path="/api/models/capability/manual", method="POST",
                           engine=temp_db, body=body)

    def test_missing_model(self, temp_db):
        from tests.test_server import TestHandler
        h = TestHandler(path="/api/models/capability/manual", method="POST",
                        engine=temp_db, body=json.dumps({"scores": {"t": 1.0}}))
        h.do_POST()
        assert h.send_response.call_args[0][0] == 400

    def test_empty_scores(self, temp_db):
        from tests.test_server import TestHandler
        h = TestHandler(path="/api/models/capability/manual", method="POST",
                        engine=temp_db, body=json.dumps({"model": "m", "scores": {}}))
        h.do_POST()
        assert h.send_response.call_args[0][0] == 400

    def test_non_numeric_score(self, temp_db):
        from tests.test_server import TestHandler
        h = TestHandler(path="/api/models/capability/manual", method="POST",
                        engine=temp_db, body=json.dumps({"model": "m", "scores": {"t": "abc"}}))
        h.do_POST()
        assert h.send_response.call_args[0][0] == 400

    def test_normalizes_0_100_and_updates(self, temp_db):
        from tests.test_server import TestHandler
        from src.api.models import get_session, ModelCapability
        body = json.dumps({"model": "m", "release": "2026-01-01", "scores": {"code_generation": 85.0}})
        h = TestHandler(path="/api/models/capability/manual", method="POST",
                        engine=temp_db, body=body)
        h.do_POST()
        assert h.send_response.call_args[0][0] == 200
        # Update again → upsert (still one row).
        h2 = TestHandler(path="/api/models/capability/manual", method="POST",
                         engine=temp_db, body=body)
        h2.do_POST()
        with get_session(temp_db) as s:
            rows = s.query(ModelCapability).filter_by(model="m", source="manual").all()
            assert len(rows) == 1
            assert rows[0].score == 0.85


# ── Endpoint: bundled leaderboard seed (semantic-routing gate exit) ─────────

class TestCapabilitySeedEndpoint:
    def test_seed_bundled_leaderboard(self, temp_db):
        """POST /api/models/capability/seed imports the bundled snapshot."""
        from tests.test_server import TestHandler
        from src.api.models import get_session, ModelCapability
        h = TestHandler(path="/api/models/capability/seed", method="POST",
                        engine=temp_db, body="{}")
        h.do_POST()
        assert h.send_response.call_args[0][0] == 200
        with get_session(temp_db) as s:
            rows = s.query(ModelCapability).filter(ModelCapability.source == "livebench").all()
            assert len(rows) > 0


# ── Endpoint: registry upsert quantization + update ─────────────────────────

class TestRegistryUpsertEdges:
    def test_upsert_detects_quantization(self, temp_db):
        from tests.test_server import TestHandler
        from src.api.models import ModelRegistryEntry, get_session
        body = json.dumps({
            "logical_name": "qwen3.6-27b-q4_k_m",
            "benchmark_key": "qwen3.6-27b-q4_k_m",
            "provider_mappings": {},
        })
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert h.send_response.call_args[0][0] == 200
        with get_session(temp_db) as s:
            entry = s.query(ModelRegistryEntry).filter_by(logical_name="qwen3.6-27b-q4_k_m").first()
            assert entry.quantization == "Q4_K_M"

    def test_upsert_updates_existing(self, temp_db):
        from tests.test_server import TestHandler, _json_body
        body = json.dumps({
            "logical_name": "deepseek-v4-pro",
            "benchmark_key": "deepseek-v4-pro",
            "provider_mappings": {"deepseek": "deepseek-v4-pro"},
            "active_release": "2099-01-01",
        })
        h = TestHandler(path="/api/models/registry", method="POST", engine=temp_db, body=body)
        h.do_POST()
        assert _json_body(h)["action"] == "created"
        # Second upsert → updated.
        h2 = TestHandler(path="/api/models/registry", method="POST", engine=temp_db, body=body)
        h2.do_POST()
        assert _json_body(h2)["action"] == "updated"


# ── Endpoint: usage totals + setup page ─────────────────────────────────────

class TestUsageTotalsEdges:
    def test_usage_totals_empty(self, temp_db):
        from tests.test_server import TestHandler, _json_body
        h = TestHandler(path="/api/usage/totals", engine=temp_db)
        h.do_GET()
        body = _json_body(h)
        assert body["requests"] == 0
        assert body["tokens"] == 0

    def test_usage_totals_error_500(self, temp_db):
        from tests.test_server import TestHandler
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            h = TestHandler(path="/api/usage/totals", engine=temp_db)
            h.do_GET()
        assert h.send_response.call_args[0][0] == 500


