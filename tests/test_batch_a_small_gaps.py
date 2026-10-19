"""Batch A coverage gaps — small modules.

Closes remaining single/double-statement gaps in:
  - src/ui/pages.py (models + setup page renderers)
  - src/ui/render.py (credential-store lookup in render_page)
  - src/ui/dashboard.py (_logical exception fallback, budget-card except)
  - src/api/component.py (abstract setup body)
  - src/api/cost_estimator.py (_count_text encode fallback)
  - src/api/memory/embeddings.py (load fallbacks, ImportError surface, dim)
  - src/api/memory/__init__.py (baked-models branch, probe exceptions,
    init_memory defaults/embedder-fail, MemoryComponent service)
  - src/api/runtime.py (components ordering, reload inactive, dispose error)
  - src/api/reasoning_store.py (empty-id, empty-tc-id, component service)
  - src/api/prompt_cache.py / token_verifier.py (component service, stats log)
  - src/api/circuit_breaker.py (_persist no-entry, component service)
  - src/api/alert_manager.py (resolve DB-miss, component service)
  - src/api/credential_store.py (workspace-id helpers, component service)
  - src/api/key_manager.py (legacy-migration dup skip, expired key, component)
"""
import os
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import src.api.memory as mem_pkg


@pytest.fixture(autouse=True)
def _isolate_runtime():
    """Keep global runtime/memory state clean between tests."""
    from src.api import runtime as rt_mod
    prev = rt_mod._active_runtime
    rt_mod._active_runtime = None
    yield
    rt_mod._active_runtime = prev
    mem_pkg._backend = None


# ── ui.pages: models + setup renderers (pages.py:62-63) ─────────────────────

class TestPageRenderers:
    def test_render_models_page(self):
        from src.ui.pages import render_models_page
        html = render_models_page(None)
        assert isinstance(html, str) and html

# ── ui.render: credential-store branch (render.py:118-119) ───────────────────

class TestRenderPageCredentialLookup:
    def test_lookup_failure_swallowed(self):
        from src.ui.render import render_page
        engine = MagicMock()
        with patch("src.api.credential_store.get_credential_store",
                   side_effect=RuntimeError("boom")):
            html = render_page("pages/providers.html", None, engine)
        assert isinstance(html, str) and html


# ── ui.dashboard: _logical except + budget-cards except (29, 34-35, 496-497) ─

class TestDashboardSmallGaps:
    @pytest.fixture
    def dash_db(self, tmp_path):
        from src.api.models import get_engine, Base
        engine = get_engine(str(tmp_path / "db.sqlite"))
        Base.metadata.create_all(engine)
        return engine

    @pytest.fixture
    def dash_cfg(self):
        cfg = MagicMock()
        cfg.profiles = {"l2": {"chain": [{"provider": "p", "model": "m"}]}}
        cfg.providers = {"p": {"models": ["m"]}}
        return cfg

    def test_empty_model_short_circuit(self, dash_db, dash_cfg):
        from src.api.models import Request as RequestModel, get_session
        from src.ui.dashboard import render_dashboard
        with get_session(dash_db) as s:
            s.add(RequestModel(timestamp="2026-01-01T00:00:00+00:00", profile="l2",
                               model="", provider="p", prompt_tokens=0,
                               completion_tokens=0, cache_hit_tokens=0,
                               cache_miss_tokens=0, cost=0, latency_ms=0, success=1))
            s.commit()
        html = render_dashboard(dash_cfg, dash_db, {"Host": "localhost"})
        assert isinstance(html, str) and html

    def test_logical_falls_back_on_db_error(self, dash_db, dash_cfg):
        import src.api.router as router_mod
        from src.api.models import Request as RequestModel, get_session
        from src.ui.dashboard import render_dashboard
        with get_session(dash_db) as s:
            s.add(RequestModel(timestamp="2026-01-01T00:00:00+00:00", profile="l2",
                               model="x/y", provider="p", prompt_tokens=10,
                               completion_tokens=1, cache_hit_tokens=0,
                               cache_miss_tokens=10, cost=0.1, latency_ms=5, success=1))
            s.commit()
        with patch.object(router_mod, "logical_model_name",
                          side_effect=RuntimeError("db down")):
            html = render_dashboard(dash_cfg, dash_db, {"Host": "localhost"})
        assert isinstance(html, str) and html

    def test_budget_cards_query_failure(self, dash_db, dash_cfg):
        from src.ui.dashboard import render_dashboard
        import src.api.models as models_mod
        real = models_mod.get_session
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("locked")  # budget block fails -> except path
            return real(*a, **k)

        with patch.object(models_mod, "get_session", side_effect=flaky):
            html = render_dashboard(dash_cfg, dash_db, {"Host": "localhost"})
        assert isinstance(html, str) and html


# ── component.py: abstract setup body (56) ──────────────────────────────────

class TestComponentContract:
    def test_abstract_setup_body(self):
        from src.api.component import Component
        assert Component.setup is not None
        # calling the ABC body directly raises NotImplementedError
        with pytest.raises(NotImplementedError):
            Component.setup(object(), None)

    def test_default_on_dependency_change(self):
        from src.api.component import Component

        class C(Component):
            name = "c"

            def setup(self, rt):
                return None

        assert C().on_dependency_change("x") is None


# ── cost_estimator: _count_text encode fallback (43-45) ─────────────────────

class TestCountTextFallback:
    def test_encode_failure_uses_char_heuristic(self):
        import src.api.cost_estimator as ce
        with patch.object(ce._ENCODING, "encode", side_effect=RuntimeError("tokenizer unhappy")):
            assert ce._count_text("x" * 400) == 100
            assert ce._count_text("abc") == 1  # max(len//4, 1)
        assert ce._count_text("") == 0  # empty short-circuit


# ── memory.embeddings (45-48, 57-66, 74) ────────────────────────────────────

class TestEmbeddingsLoadPaths:
    def test_import_error_surfaces_underlying(self):
        from src.api.memory.embeddings import EmbeddingModel
        from src.api.memory.base import MemoryError as StMemoryError
        real = sys.modules.get("sentence_transformers")
        sys.modules["sentence_transformers"] = None  # import halted -> ImportError
        try:
            with pytest.raises(StMemoryError, match="import failed"):
                EmbeddingModel(backend="sentence-transformers")._load()
        finally:
            if real is not None:
                sys.modules["sentence_transformers"] = real
            else:
                del sys.modules["sentence_transformers"]

    def test_offline_retry_with_cache_dir(self, tmp_path):
        from src.api.memory.embeddings import EmbeddingModel
        calls = []

        class FakeST:
            def __init__(self, name, **kwargs):
                calls.append(kwargs)
                if "local_files_only" not in kwargs:
                    raise OSError("hub unreachable")
                self.dim = 384

        mod = types.ModuleType("sentence_transformers")
        mod.SentenceTransformer = FakeST
        m = EmbeddingModel(cache_dir=str(tmp_path), backend="sentence-transformers")
        with patch.dict(sys.modules, {"sentence_transformers": mod}):
            model = m._load()
        assert model is not None
        assert calls[-1]["local_files_only"] is True

    def test_no_cache_dir_reraises(self):
        from src.api.memory.embeddings import EmbeddingModel

        class FakeST:
            def __init__(self, name, **kwargs):
                raise OSError("hub unreachable")

        mod = types.ModuleType("sentence_transformers")
        mod.SentenceTransformer = FakeST
        m = EmbeddingModel(backend="sentence-transformers")
        with patch.dict(sys.modules, {"sentence_transformers": mod}):
            with pytest.raises(OSError):
                m._load()

    def test_dim_falls_back_on_load_failure(self):
        from src.api.memory import embeddings as emb
        m = emb.EmbeddingModel(backend="sentence-transformers")
        with patch.object(m, "_load", side_effect=RuntimeError("nope")):
            assert m.dim == emb.DEFAULT_DIM


# ── memory/__init__: model dirs, probes, init_memory branches ────────────────

class TestMemorySmallGaps:
    def test_memory_models_baked_branch(self, tmp_path, monkeypatch):
        baked = tmp_path / "baked"
        baked.mkdir()
        monkeypatch.setattr(mem_pkg, "_BAKED_MODELS_DIR", str(baked))
        assert mem_pkg.memory_models() == str(baked)
        assert mem_pkg.router_models() == str(baked)

    def test_memory_probe_exception(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run",
                            MagicMock(side_effect=subprocess.TimeoutExpired("x", 1)))
        assert mem_pkg.memory_available() is False

    def test_router_probe_exception(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run",
                            MagicMock(side_effect=OSError("no exec")))
        assert mem_pkg.router_available() is False

    def test_init_memory_default_storage_and_embedder_fail(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COST_DB", str(tmp_path / "sub" / "costs.db"))
        boom = MagicMock(side_effect=RuntimeError("no embedder"))
        cfg = {"memory": {"storage_path": "  "}}
        with patch.object(mem_pkg, "embedder_from_config", boom), \
             patch("src.api.memory.lancedb_backend.LanceDBMemoryBackend") as lb:
            mem_pkg.init_memory(cfg)
            lb.assert_called_once()
            storage = lb.call_args.kwargs.get("storage_path") or lb.call_args.args
        # storage derived from COST_DB dir + "memory"
        assert str(tmp_path / "sub") in str(lb.call_args)
        mem_pkg.shutdown_memory()
        assert mem_pkg.get_memory() is None

    def test_memory_component_service(self):
        comp = mem_pkg.MemoryComponent()
        mem_pkg._backend = None
        assert comp.service is None
        sentinel = object()
        mem_pkg._backend = sentinel
        assert comp.service is sentinel
        mem_pkg._backend = None


# ── runtime.py: components() ordering (108), reload inactive (165),
#    dispose error (201-202) ─────────────────────────────────────────────────

class TestRuntimeSmallGaps:
    def test_components_in_setup_order_plus_unordered(self):
        from src.api.runtime import Runtime
        from src.api.component import Component

        class A(Component):
            name = "a"
            provides = ["ka"]

            def setup(self, rt):
                return None

        class B(Component):
            name = "b"
            requires = ["ka"]

            def setup(self, rt):
                return None

        class Late(Component):
            name = "late"

            def setup(self, rt):
                return None

        rt = Runtime()
        a, b, late = A(), B(), Late()
        rt.register(a)
        rt.register(b)
        rt.start()
        rt.register(late)  # registered AFTER start → not in _order
        names = [c.name for c in rt.components()]
        assert names[:2] == ["a", "b"]  # setup order first
        assert names[-1] == "late"

    def test_reload_inactive_raises(self):
        from src.api.runtime import Runtime
        from src.api.component import Component

        class X(Component):
            name = "x"

            def setup(self, rt):
                return None

        rt = Runtime()
        rt.register(X())
        with pytest.raises(RuntimeError, match="not active"):
            rt.reload("x")

    def test_dispose_error_swallowed_on_shutdown(self):
        from src.api.runtime import Runtime
        from src.api.component import Component

        class X(Component):
            name = "x"

            def setup(self, rt):
                return lambda: (_ for _ in ()).throw(RuntimeError("dispose boom"))

        rt = Runtime()
        rt.register(X())
        rt.start()
        rt.shutdown()  # disposer raises → must be logged, not propagated
        assert rt.is_active("x") is False


# ── reasoning_store (53, 66, 90, 152) ───────────────────────────────────────

class TestReasoningStoreGaps:
    def test_empty_ids_noop(self):
        from src.api.reasoning_store import ReasoningStore
        s = ReasoningStore()
        s.capture("", "rc")
        s.capture(["", None], "rc")
        assert s.get_for_tool_call_id("") is None
        assert s._by_tool_call_id == {}

    def test_rehydrate_skips_idless_tool_calls(self):
        from src.api.reasoning_store import ReasoningStore
        s = ReasoningStore()
        s.capture("call-1", "because")
        msgs = [
            {"role": "assistant", "tool_calls": [{"function": {"name": "f"}}]},  # no id
            {"role": "assistant", "tool_calls": [{"id": "call-1"}], "content": "x"},
        ]
        s.rehydrate(msgs)
        assert "reasoning_content" not in msgs[0]
        assert msgs[1]["reasoning_content"] == "because"

    def test_component_service(self):
        from src.api import reasoning_store as rs
        rs._reasoning_store = None
        comp = rs.ReasoningStoreComponent()
        svc = comp.service
        assert svc is rs.get_reasoning_store()
        assert comp.setup(None) is None
        rs._reasoning_store = None


# ── prompt_cache (108) + token_verifier (75, 110) ───────────────────────────

class TestSingletonComponents:
    def test_prompt_cache_component_service(self):
        from src.api import prompt_cache as pc
        comp = pc.PromptCacheComponent()
        assert comp.service is pc.get_prompt_cache()

    def test_token_verifier_component_service(self):
        from src.api import token_verifier as tv
        comp = tv.TokenVerifierComponent()
        assert comp.service is tv.get_token_verifier()

    def test_periodic_stats_log_branch(self):
        from src.api.token_verifier import TokenVerifier
        v = TokenVerifier(threshold=0.0001)
        v._checks = 999
        # 1000th check -> % 1000 == 0 stats branch; huge provider tokens + tiny
        # content -> suspicious warning branch too
        v.verify([{"role": "user", "content": "hi"}],
                 {"prompt_tokens": 500000, "completion_tokens": 1})
        assert v._checks == 1000


# ── circuit_breaker (101, 381-382) ──────────────────────────────────────────

class TestCircuitBreakerGaps:
    def test_persist_missing_entry_returns(self, tmp_path):
        from src.api.circuit_breaker import CircuitBreaker
        from src.api.models import get_engine
        cfg = MagicMock()
        cfg.circuit_breaker = {"failures_dead": 5, "dead_cooldown_seconds": 300,
                               "failures_degraded": 3, "degraded_cooldown_seconds": 60}
        cb = CircuitBreaker(cfg)
        cb._engine = get_engine(str(tmp_path / "db.sqlite"))
        cb._persist("ghost", "http://x", "p")  # no health entry -> early return
        assert cb._health == {}

    def test_component_service(self):
        from src.api import circuit_breaker as cbm
        from src.api import runtime as rt_mod
        cbm._circuit_breaker = None
        comp = cbm.CircuitBreakerComponent()
        fake_rt = MagicMock()
        fake_rt.resolve.return_value = comp
        prev = rt_mod._active_runtime
        rt_mod._active_runtime = fake_rt
        try:
            cfg = MagicMock()
            cfg.circuit_breaker = {"failures_dead": 5, "dead_cooldown_seconds": 300,
                                   "failures_degraded": 3, "degraded_cooldown_seconds": 60}
            assert cbm.get_circuit_breaker(cfg) is not None  # comp None → legacy
            comp.breaker = object()
            assert cbm.get_circuit_breaker(cfg) is comp.breaker  # runtime delegation
            assert comp.service is comp.breaker
        finally:
            rt_mod._active_runtime = prev
        cbm._circuit_breaker = None


# ── alert_manager (201, 321-322) ────────────────────────────────────────────

class TestAlertManagerGaps:
    @pytest.fixture
    def am_db(self, tmp_path):
        from src.api.models import get_engine, Base
        engine = get_engine(str(tmp_path / "db.sqlite"))
        Base.metadata.create_all(engine)
        return engine

    def test_resolve_not_in_db_falls_back_to_memory(self, am_db):
        from src.api.alert_manager import AlertManager
        am = AlertManager(engine=am_db)
        am._active_alerts["k"] = {"dedup_key": "k", "status": "active"}
        assert am.resolve("k") is True  # found in memory, missing in DB
        assert "k" not in am._active_alerts
        assert am.resolve("nope") is False

    def test_component_service(self, am_db):
        from src.api import alert_manager as am_mod
        from src.api import runtime as rt_mod
        am_mod._alert_manager = None
        comp = am_mod.AlertManagerComponent()
        fake_rt = MagicMock()
        fake_rt.resolve.return_value = comp
        prev = rt_mod._active_runtime
        rt_mod._active_runtime = fake_rt
        try:
            assert am_mod.get_alert_manager() is not None  # comp None → legacy
            comp.manager = am_mod.AlertManager(am_db)
            assert am_mod.get_alert_manager() is comp.manager  # runtime delegation
            assert comp.service is comp.manager
        finally:
            rt_mod._active_runtime = prev
        am_mod._alert_manager = None


# ── credential_store (116, 133-134, 169) ────────────────────────────────────

class TestCredentialStoreGaps:
    def test_workspace_id_helpers(self, temp_db):
        from src.api.credential_store import CredentialStore
        _, engine = temp_db
        cs = CredentialStore(engine)
        assert cs.get_workspace_id("p") is None
        assert cs.has_workspace_id("p") is False
        cs.set(cs._ws_id_key("p"), "ws-1")
        assert cs.get_workspace_id("p") == "ws-1"
        assert cs.has_workspace_id("p") is True

    def test_component_service(self):
        from src.api import credential_store as cs_mod
        from src.api import runtime as rt_mod
        comp = cs_mod.CredentialStoreComponent()
        assert comp.store is None
        assert comp.service is None
        # runtime delegation branch: resolver raising → legacy, resolver
        # returning the comp with a bound store → runtime store
        prev = rt_mod._active_runtime
        fake_rt = MagicMock()
        fake_rt.resolve.side_effect = KeyError("inactive")
        rt_mod._active_runtime = fake_rt
        try:
            cs_mod._credential_store = None
            assert cs_mod.get_credential_store() is None  # legacy, no engine
            fake_rt.resolve.side_effect = None
            fake_rt.resolve.return_value = comp
            sentinel = object()
            comp.store = sentinel
            assert cs_mod.get_credential_store() is sentinel
        finally:
            rt_mod._active_runtime = prev
            comp.store = None


# ── key_manager (53, 232, 265-266) ──────────────────────────────────────────

class TestKeyManagerGaps:
    def test_legacy_migration_skips_duplicate_hash(self, tmp_path):
        from src.api.key_manager import KeyManager
        from src.api.models import get_engine, Base, ApiKey, get_session
        import json
        engine = get_engine(str(tmp_path / "db.sqlite"))
        Base.metadata.create_all(engine)
        with get_session(engine) as s:
            s.add(ApiKey(key_hash="HASH-EXIST", key_prefix="p", name="pre-existing"))
            s.commit()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "api_keys.json").write_text(json.dumps({"keys": [
            {"hash": "HASH-EXIST", "id": "abcdefgh1234", "label": "dup"},
            {"hash": "HASH-NEW", "id": "ijklmnop5678", "label": "fresh"},
        ]}))
        KeyManager(engine, str(data_dir))
        with get_session(engine) as s:
            names = sorted(x.name for x in s.query(ApiKey).all())
        assert names == ["dup", "fresh", "pre-existing"] or "fresh" in names
        # duplicate was skipped: exactly one row with HASH-EXIST
        with get_session(engine) as s:
            assert s.query(ApiKey).filter_by(key_hash="HASH-EXIST").count() == 1

    def test_expired_key_returns_none(self, temp_db):
        from src.api.key_manager import KeyManager
        _, engine = temp_db
        km = KeyManager(engine)
        created = km.create_key(name="exp")
        from src.api.models import ApiKey, get_session
        with get_session(engine) as s:
            row = s.query(ApiKey).filter_by(key_hash=created["hash"] if "hash" in created else None).first()
        # set expires_at in the past directly
        import hashlib
        h = hashlib.sha256(created["key"].encode()).hexdigest()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with get_session(engine) as s:
            row = s.query(ApiKey).filter_by(key_hash=h).first()
            row.expires_at = past
            s.commit()
        assert km.validate_key(created["key"]) is None

    def test_naive_expiry_gets_utc(self, temp_db):
        from src.api.key_manager import KeyManager
        from src.api.models import ApiKey, get_session
        import hashlib
        _, engine = temp_db
        km = KeyManager(engine)
        created = km.create_key(name="naive")
        h = hashlib.sha256(created["key"].encode()).hexdigest()
        future = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)).isoformat()
        with get_session(engine) as s:
            row = s.query(ApiKey).filter_by(key_hash=h).first()
            row.expires_at = future  # no tz suffix -> naive branch
            s.commit()
        assert km.validate_key(created["key"]) is not None

    def test_component_service(self, temp_db):
        from src.api import key_manager as km_mod
        from src.api import runtime as rt_mod
        km_mod._key_manager = None
        _, engine = temp_db
        comp = km_mod.KeyManagerComponent()
        fake_rt = MagicMock()
        fake_rt.resolve.return_value = comp
        prev = rt_mod._active_runtime
        rt_mod._active_runtime = fake_rt
        try:
            assert km_mod.get_key_manager(engine) is not None  # comp None → legacy
            comp.manager = km_mod.KeyManager(engine)
            assert km_mod.get_key_manager() is comp.manager  # runtime delegation
            assert comp.service is comp.manager
        finally:
            rt_mod._active_runtime = prev
        km_mod._key_manager = None
