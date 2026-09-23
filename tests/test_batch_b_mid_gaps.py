"""Batch B coverage gaps — mid-size modules.

Closes error/edge branches in:
  - src/api/memory/lancedb_backend.py (connect/table/forget/count/index errors,
    table-name normalisation, metadata decoding, embed passthroughs)
  - src/api/config.py (_validate raises, _hydrate store-fail/seed-missing,
    env-override excepts, save skip/fail)
  - src/api/task_classifier.py (_normalize, centroid-build branches,
    top_scores/classify fallbacks, _probe_embed, classifier except)
  - src/main.py (build_runtime dynamic_routing fallbacks, main() seed/
    refresher/recovery excepts, __main__ guard)
  - src/api/memory/harness.py (get_memory import fail, empty block,
    config_for attr fail)
  - src/api/memory/__init__.py (COST_DB read failure)
  - src/api/memory/embeddings.py (dim happy path)
  - src/api/{circuit_breaker,alert_manager,key_manager}.py (runtime resolve
    exception → legacy fallback)
  - src/api/seed_capabilities.py (effective_releases, matrix source priority,
    __main__ guard)

The __main__ guards are exercised by exec()-ing the file up to (but not
including) the guard, patching the copy's main(), then exec-ing the guard line
with its true line number — the guard statement really executes; only the
entry function is mocked (same semantics as the TestMain pattern).
"""
import io
import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _exec_module_guards(mod_name):
    """Run a module's __main__ guard with its main() mocked. Returns the mock."""
    import importlib
    mod = importlib.import_module(mod_name)
    src_path = mod.__file__
    with open(src_path, encoding="utf-8") as f:
        lines = f.readlines()
    guard_idx = next(i for i, ln in enumerate(lines)
                     if ln.startswith("if __name__ == "))
    body = "".join(lines[:guard_idx])
    guard = "".join(lines[guard_idx:])
    # Exec the defs under the module's real name so relative imports resolve,
    # then flip __name__ to "__main__" for the guard line (main patched out).
    g = {"__name__": mod_name, "__file__": src_path}
    exec(compile(body, src_path, "exec"), g)
    called = MagicMock()
    g["main"] = called
    g["__name__"] = "__main__"
    exec(compile(guard, src_path, "exec"), g)
    called.assert_called_once()


class _Row:
    """Lightweight stand-in for a ModelCapability ORM row."""

    def __init__(self, model, task_type, score, source, release_label=None):
        self.model = model
        self.task_type = task_type
        self.score = score
        self.source = source
        self.release_label = release_label


# ── lancedb_backend: error/edge branches ────────────────────────────────────

class TestLanceDBErrorBranches:
    @pytest.fixture
    def be(self, tmp_path):
        from src.api.memory.lancedb_backend import LanceDBMemoryBackend

        def fake_embed(texts):
            return [[0.1] * 32 for _ in texts]
        b = LanceDBMemoryBackend(str(tmp_path / "mem"), fake_embed, dim=32)
        return b

    def test_makedirs_oserror(self, monkeypatch):
        from src.api.memory.lancedb_backend import LanceDBMemoryBackend
        from src.api.memory.base import MemoryError as MemErr
        monkeypatch.setattr(os, "makedirs",
                            MagicMock(side_effect=OSError("read-only fs")))
        with pytest.raises(MemErr, match="cannot create memory storage"):
            LanceDBMemoryBackend("/proc/nope/mem", lambda t: [[0.0]], dim=4)

    def test_connect_failure(self, be):
        from src.api.memory.base import MemoryError as MemErr
        with patch("lancedb.connect", side_effect=RuntimeError("corrupt")):
            with pytest.raises(MemErr, match="failed to open LanceDB"):
                be._connect()

    def test_table_access_failure(self, be):
        from src.api.memory.base import MemoryError as MemErr
        be._db = MagicMock()
        be._db.open_table.side_effect = RuntimeError("io")
        with pytest.raises(MemErr, match="failed to access memory table"):
            be._table("p", create=False)

    def test_table_names_legacy_objects(self, be):
        be._db = MagicMock()
        t = MagicMock()
        t.name = "prof1"
        be._db.table_names.return_value = [t]
        assert be._table_names() == ["prof1"]

    def test_table_names_exception(self, be):
        be._db = MagicMock()
        be._db.table_names.side_effect = RuntimeError("boom")
        assert be._table_names() == []

    def test_table_names_none(self, be):
        be._db = MagicMock()
        be._db.table_names.return_value = None
        assert be._table_names() == []

    def test_retain_table_add_failure(self, be):
        from src.api.memory.base import MemoryError as MemErr
        with patch.object(be, "_table", side_effect=MemErr("no table")):
            with pytest.raises(MemErr, match="failed to retain"):
                be.retain("some content", profile="p")

    def test_retain_memoryerror_passthrough(self, be):
        from src.api.memory.base import MemoryError as MemErr

        def boom(texts):
            raise MemErr("embed says no")
        be._embed = boom
        with pytest.raises(MemErr, match="embed says no"):
            be.retain("content", profile="p")

    def test_recall_memoryerror_passthrough(self, be):
        from src.api.memory.base import MemoryError as MemErr
        be.retain("gpu hardware note", profile="p")

        def boom(texts):
            raise MemErr("embed down")
        be._embed = boom
        with pytest.raises(MemErr, match="embed down"):
            be.recall("gpu", profile="p")

    def test_recall_tag_filter_empty_after_strip(self, be):
        # tags present but all blank → literal-less search path
        be.retain("gpu hardware note", tags=["gpu"], profile="p")
        out = be.recall("gpu", profile="p", tag_filter=["  ", ""])
        assert isinstance(out, list)

    def test_recall_where_failure_retries_unfiltered(self, be):
        be.retain("gpu hardware note", tags=["gpu"], profile="p")
        real = be._table("p", create=False)

        class FakeBuilder:
            called = {"n": 0}

            def __init__(self, vector):
                self.vector = vector

            def where(self, q):
                raise RuntimeError("unsupported filter")

            def limit(self, k):
                # only ever reached on the unfiltered retry (WHERE breaks the
                # chain before limit() on the first attempt)
                FakeBuilder.called["n"] += 1
                return real.search(self.vector).limit(k)

        with patch.object(be, "_table") as tbl:
            t = MagicMock()
            t.count_rows.return_value = 5
            t.search.side_effect = lambda v: FakeBuilder(v)
            tbl.return_value = t
            out = be.recall("gpu", profile="p", tag_filter=["gpu"])
        assert out and "id" in out[0]
        assert FakeBuilder.called["n"] == 1  # only the unfiltered retry

    def test_recall_retry_also_fails(self, be):
        from src.api.memory.base import MemoryError as MemErr
        be.retain("gpu note", profile="p")
        with patch.object(be, "_table") as tbl:
            t = MagicMock()
            t.count_rows.return_value = 1
            s = MagicMock()
            s.where.side_effect = RuntimeError("nope")
            s.limit.side_effect = RuntimeError("also nope")
            t.search.return_value = s
            tbl.return_value = t
            with pytest.raises(MemErr, match="memory recall failed"):
                be.recall("gpu", profile="p", tag_filter=["gpu"])

    def test_forget_error_returns_false(self, be):
        be.retain("gpu note", profile="p")
        with patch.object(be, "_table") as tbl:
            t = MagicMock()
            t.count_rows.side_effect = RuntimeError("io")
            tbl.return_value = t
            assert be.forget("abc", profile="p") is False

    def test_count_error_returns_zero(self, be):
        be.retain("gpu note", profile="p")
        with patch.object(be, "_table") as tbl:
            tbl.side_effect = RuntimeError("io")
            assert be.count("p") == 0

    def test_decode_meta_variants(self, be):
        assert be._decode_meta({"a": 1}) == {"a": 1}
        assert be._decode_meta('{"b": 2}') == {"b": 2}
        assert be._decode_meta("not json{{") == {}
        assert be._decode_meta(12345) == {}

    def test_ensure_index_disabled(self, be):
        be._index_threshold = 0
        t = MagicMock()
        be._ensure_index(t)  # 270-271: early return
        t.create_index.assert_not_called()

    def test_ensure_index_builds_above_threshold(self, be):
        be._index_threshold = 2
        t = MagicMock()
        t.count_rows.return_value = 5
        be._ensure_index(t)
        t.create_index.assert_called_once()

    def test_ensure_index_failure_swallowed(self, be):
        be._index_threshold = 2
        t = MagicMock()
        t.count_rows.return_value = 5
        t.create_index.side_effect = RuntimeError("no IVF_PQ")
        be._ensure_index(t)  # logs warning, never raises


# ── config.py: _validate raises + hydrate/save branches ─────────────────────

class TestConfigGaps:
    def test_validate_missing_default_profile(self):
        from src.api.config import _validate, ConfigError
        with pytest.raises(ConfigError, match="default_profile"):
            _validate("server", {"port": 8734})

    def test_validate_profiles_not_dict(self):
        from src.api.config import _validate, ConfigError
        with pytest.raises(ConfigError, match="must be a dict"):
            _validate("profiles", ["x"])

    def test_validate_empty_chain(self):
        from src.api.config import _validate, ConfigError
        with pytest.raises(ConfigError, match="empty 'chain'"):
            _validate("profiles", {"p": {"chain": []}})

    def test_validate_pricing_not_list(self):
        from src.api.config import _validate, ConfigError
        with pytest.raises(ConfigError, match="'pricing' must be a list"):
            _validate("pricing", {})

    def test_validate_providers_not_dict(self):
        from src.api.config import _validate, ConfigError
        with pytest.raises(ConfigError, match="must be a dict"):
            _validate("providers", "nope")

    def test_hydrate_store_read_failure(self, tmp_path):
        from src.api.config import Config
        from src.api.models import get_engine, Base
        engine = get_engine(str(tmp_path / "db.sqlite"))
        Base.metadata.create_all(engine)
        store = MagicMock()
        store.get_config_section.side_effect = RuntimeError("db locked")
        cfg = Config(store=store)  # every read fails → seed values (253-254)
        assert cfg.server.get("port") is not None

    def test_hydrate_section_missing_from_seed(self, tmp_path):
        from src.api.config import Config, SEED_CONFIG
        seed = {k: v for k, v in SEED_CONFIG.items() if v is not None}
        seed.pop("plugins", None)  # plugins: no DB row AND not in seed → {}
        store = MagicMock()
        store.get_config_section.return_value = None
        cfg = Config(store=store, seed=seed)
        assert cfg._data["plugins"] == {}

    def test_env_port_override_failure(self, monkeypatch):
        import src.api.config as cfg_mod
        monkeypatch.setenv("LISTEN_PORT", "not-a-port")
        cfg = cfg_mod.Config()  # int() raises inside try → except (277-278)
        assert isinstance(cfg.server["port"], int)  # seed value retained

    def test_env_db_path_override_failure(self, monkeypatch):
        import src.api.config as cfg_mod
        monkeypatch.setenv("COST_DB", "/x/costs.db")
        with patch.object(cfg_mod, "_env_db_path", side_effect=RuntimeError):
            cfg = cfg_mod.Config()  # except (282-283)
        assert "path" in cfg._data["database"]

    def test_save_skips_none_section_and_logs_failures(self, tmp_path):
        from src.api.config import Config
        cfg = Config()
        store = MagicMock()
        cfg._store = store
        cfg._data["retry"] = None  # None section → continue (384)
        store.set_config_section.side_effect = [None] + [RuntimeError("ro")] * 20
        cfg.save()  # failures logged per section, never raised

    def test_save_no_store(self):
        from src.api.config import Config
        Config().save()  # store None → early return


# ── task_classifier: centroid/probe/classify branches ────────────────────────

class TestTaskClassifierGaps:
    def test_normalize(self):
        from src.api.task_classifier import _normalize
        assert _normalize("  MiXeD ") == "mixed"
        assert _normalize(None) == ""

    def test_centroids_empty_exemplars_skipped(self):
        import src.api.task_classifier as tc
        calls = []

        def embed(texts):
            calls.append(texts)
            return [[1.0, 0.0] for _ in texts]
        with patch.dict(tc.TASK_EXEMPLARS, {"empty_task": []}):
            c = tc.SemanticClassifier(embed=embed)
            c._build_centroids()
        assert "empty_task" not in c._centroids

    def test_centroids_embed_exception_skips_task(self):
        import src.api.task_classifier as tc
        seen = []

        def embed(texts):
            seen.append(texts[0])
            if len(seen) == 1:
                raise RuntimeError("hub down")
            return [[1.0, 0.0] for _ in texts]
        c = tc.SemanticClassifier(embed=embed)
        c._build_centroids()  # first task raises → continue (169-170)
        assert c._centroids

    def test_centroids_empty_vectors_skipped(self):
        import src.api.task_classifier as tc
        c = tc.SemanticClassifier(embed=lambda texts: [])
        c._build_centroids()  # not vectors → continue (172)
        assert c._centroids == {}
        assert c.top_scores("anything", 3) == []  # 197
        assert c.classify("anything") is None     # 220

    def test_centroids_zero_dim_skipped(self):
        import src.api.task_classifier as tc
        c = tc.SemanticClassifier(embed=lambda texts: [[]])
        c._build_centroids()  # dim == 0 → continue (175)
        assert c._centroids == {}

    def test_top_scores_embed_failure(self):
        import src.api.task_classifier as tc

        def embed(texts):
            if len(texts) == 1 and texts[0] == "QUERY":
                raise RuntimeError("embed exploded")
            return [[1.0, 0.0]]
        c = tc.SemanticClassifier(embed=embed)
        c._build_centroids()
        assert c.top_scores("QUERY", 3) == []  # 200-201

    def test_classify_empty_scores(self):
        import src.api.task_classifier as tc
        c = tc.SemanticClassifier(embed=lambda texts: [[1.0, 0.0]])
        c._build_centroids()
        c.top_scores = lambda *a, **k: []  # scores empty → None (223)
        assert c.classify("anything") is None

    def test_probe_embed_variants(self):
        from src.api.task_classifier import _probe_embed
        assert _probe_embed(None) is False            # 242
        assert _probe_embed(lambda t: (_ for _ in ()).throw(RuntimeError("x"))) is False
        assert _probe_embed(lambda t: [[]]) is False  # 249 — empty vector

    def test_classifier_build_failure(self):
        import src.api.task_classifier as tc
        tc.invalidate_semantic_classifier()
        with patch("src.api.config.get_config", side_effect=RuntimeError("cfg gone")):
            assert tc.get_semantic_classifier() is None  # except → None (315-317)
        tc.invalidate_semantic_classifier()

    def test_classifier_probe_no_signal(self):
        import src.api.task_classifier as tc
        tc.invalidate_semantic_classifier()
        cfg = MagicMock()
        cfg.plugins = {"router": {"enabled": True}}
        with patch("src.api.config.get_config", return_value=cfg), \
             patch.object(tc, "_probe_embed", return_value=False):
            assert tc.get_semantic_classifier() is None
        tc.invalidate_semantic_classifier()


# ── main.py: boot fallbacks + __main__ guard ─────────────────────────────────

class TestMainGaps:
    @pytest.fixture(autouse=True)
    def _no_runtime_leak(self):
        yield
        import src.api.runtime as runtime
        runtime._active_runtime = None

    def test_dynamic_routing_non_dict(self):
        import src.main as m
        cfg = MagicMock()
        cfg.dynamic_routing = "not-a-dict"
        with patch("src.api.runtime.Runtime") as RT:
            rt = m.build_runtime(MagicMock(), cfg, "/tmp/x.db", "/tmp")
        assert RT.return_value is rt

    def test_dynamic_routing_attr_failure(self):
        import src.main as m
        cfg = object()  # no .dynamic_routing → AttributeError → dr={} (67-68)
        with patch("src.api.runtime.Runtime"):
            m.build_runtime(MagicMock(), cfg, "/tmp/x.db", "/tmp")

    def test_main_config_seed_failure_swallowed(self, tmp_path):
        import src.main
        db = str(tmp_path / "c.db")
        server = MagicMock()
        settings = MagicMock()
        settings.config_sections.side_effect = RuntimeError("db locked")
        cfg = MagicMock()
        cfg.database = {"path": db}
        with patch.dict(os.environ, {"COST_DB": db}), \
             patch.object(src.main, "create_server", return_value=server), \
             patch("src.api.cost_cache.init_settings", return_value=settings), \
             patch("src.api.config.init_config", return_value=cfg):
            src.main.main()

    def test_main_seeds_db_on_first_boot(self, tmp_path):
        import src.main
        db = str(tmp_path / "c.db")
        server = MagicMock()
        settings = MagicMock()
        settings.config_sections.return_value = []  # first boot → config.save()
        cfg = MagicMock()
        cfg.database = {"path": db}
        with patch.dict(os.environ, {"COST_DB": db}), \
             patch.object(src.main, "create_server", return_value=server), \
             patch("src.api.cost_cache.init_settings", return_value=settings), \
             patch("src.api.config.init_config", return_value=cfg):
            src.main.main()
        cfg.save.assert_called_once()

    def test_main_refresher_start_failure(self, tmp_path):
        import src.main
        db = str(tmp_path / "c.db")
        rt = MagicMock()
        rt.resolve.side_effect = RuntimeError("component missing")
        settings = MagicMock()
        settings.config_sections.return_value = ["server"]
        cfg = MagicMock()
        with patch.dict(os.environ, {"COST_DB": db}), \
             patch.object(src.main, "create_server", return_value=MagicMock()), \
             patch.object(src.main, "build_runtime", return_value=rt), \
             patch("src.api.cost_cache.init_settings", return_value=settings), \
             patch("src.api.config.init_config", return_value=cfg):
            src.main.main()  # refresher except (148-149) must not block boot

    def test_main_benchmark_recovery_with_and_without(self, tmp_path):
        import src.main
        db = str(tmp_path / "c.db")
        settings = MagicMock()
        settings.config_sections.return_value = ["server"]
        rt = MagicMock()
        cfg = MagicMock()
        with patch.dict(os.environ, {"COST_DB": db}), \
             patch.object(src.main, "create_server", return_value=MagicMock()), \
             patch.object(src.main, "build_runtime", return_value=rt), \
             patch("src.api.cost_cache.init_settings", return_value=settings), \
             patch("src.api.config.init_config", return_value=cfg), \
             patch("src.api.benchmark.recover_stale_runs", return_value=3):
            src.main.main()  # recovered>0 → info path (156)
        with patch.dict(os.environ, {"COST_DB": db}), \
             patch.object(src.main, "create_server", return_value=MagicMock()), \
             patch.object(src.main, "build_runtime", return_value=rt), \
             patch("src.api.cost_cache.init_settings", return_value=settings), \
             patch("src.api.config.init_config", return_value=cfg), \
             patch("src.api.benchmark.recover_stale_runs",
                   side_effect=RuntimeError("no table")):
            src.main.main()  # recovery except (157-158)

    def test_main_entry_guard(self):
        _exec_module_guards("src.main")


# ── memory/harness.py ────────────────────────────────────────────────────────

class TestMemoryHarnessGaps:
    def test_get_memory_import_failure(self):
        import src.api.memory.harness as h
        with patch.dict(sys.modules, {"src.api.memory": None}):
            # `from . import get_memory` with the package halted → ImportError
            assert h.recall_for_request(
                [{"role": "user", "content": "hi"}]) == []

    def test_inject_empty_context_block(self):
        import src.api.memory.harness as h
        backend = MagicMock()
        backend.recall.return_value = [{"content": "   ", "score": 0.9}]
        msgs = [{"role": "user", "content": "hi"}]
        with patch("src.api.memory.get_memory", return_value=backend):
            assert h.inject_memory_context(msgs) is msgs  # block empty → unchanged

    def test_config_for_attr_failure(self):
        from src.api.memory.harness import config_for
        cfg = MagicMock()
        type(cfg).plugins = property(lambda self: (_ for _ in ()).throw(
            RuntimeError("no plugins")))
        out = config_for(cfg)  # except → plugins={} (137-138)
        assert out["enabled"] is False


# ── memory/__init__.py + embeddings.py ───────────────────────────────────────

class TestMemoryInitGaps:
    def test_cost_db_read_failure(self, monkeypatch, tmp_path):
        import src.api.memory as mem
        mem._backend = None

        def fake_get(key, default=""):
            if key == "COST_DB":
                raise RuntimeError("environ read frozen")
            return default

        fake_os = types.SimpleNamespace(
            environ=MagicMock(get=fake_get),
            path=os.path,
            isdir=os.path.isdir,
        )
        # embedder_from_config is imported into the package namespace (line 22)
        with patch.object(mem, "os", fake_os), \
             patch.object(mem, "embedder_from_config",
                          MagicMock(side_effect=RuntimeError("no embedder"))), \
             patch("src.api.memory.lancedb_backend.LanceDBMemoryBackend") as lb:
            assert mem.init_memory(None) is True
            lb.assert_called_once()
            assert str(lb.call_args.args[0]).startswith("data")  # storage default
        mem.shutdown_memory()

    def test_dim_happy_path(self):
        from src.api.memory.embeddings import EmbeddingModel
        m = EmbeddingModel()
        fake = MagicMock()
        fake.get_sentence_embedding_dimension.return_value = 384
        m._model = fake
        assert m.dim == 384


# ── singleton facades: runtime resolve exception → legacy fallback ───────────

class TestFacadeResolveExceptions:
    @pytest.fixture
    def raising_rt(self):
        from src.api import runtime as rt_mod
        fake = MagicMock()
        fake.resolve.side_effect = KeyError("component inactive")
        prev = rt_mod._active_runtime
        rt_mod._active_runtime = fake
        yield fake
        rt_mod._active_runtime = prev

    def test_circuit_breaker_resolve_exception(self, raising_rt):
        from src.api import circuit_breaker as cbm
        cbm._circuit_breaker = None
        cfg = MagicMock()
        cfg.circuit_breaker = {"failures_dead": 5, "dead_cooldown_seconds": 300,
                               "failures_degraded": 3, "degraded_cooldown_seconds": 60}
        assert cbm.get_circuit_breaker(cfg) is cbm._circuit_breaker
        cbm._circuit_breaker = None

    def test_alert_manager_resolve_exception(self, raising_rt):
        from src.api import alert_manager as am
        am._alert_manager = None
        assert am.get_alert_manager() is not None
        am._alert_manager = None

    def test_key_manager_resolve_exception(self, raising_rt):
        from src.api import key_manager as km
        km._key_manager = None
        assert km.get_key_manager() is None  # legacy, no engine → None
        assert km.get_key_manager(MagicMock()) is km._key_manager
        km._key_manager = None




# ── seed_capabilities: releases + priority + __main__ ────────────────────────

class TestSeedCapabilitiesGaps:
    def test_effective_releases_pinned_and_max(self):
        from src.api.seed_capabilities import effective_releases
        rows = [
            _Row("model-a", "coding", 0.5, "livebench", "2026-06-25"),
            _Row("model-a", "coding", 0.6, "livebench", "2026-08-13"),
        ]
        registry = {"ModelA": {"benchmark_key": "model-a",
                               "active_release": "2026-06-25"}}
        out = effective_releases(rows, registry)
        assert out["model-a"] == "2026-06-25"  # pinned
        out2 = effective_releases(rows, {})     # no pin → max
        assert out2["model-a"] == "2026-08-13"

    def test_matrix_source_priority_skip(self, tmp_path):
        from src.api.seed_capabilities import load_capability_matrix
        from src.api.models import get_engine, Base, ModelCapability
        db = str(tmp_path / "caps.db")
        engine = get_engine(db)
        Base.metadata.create_all(engine)
        from src.api.models import get_session
        with get_session(engine) as s:
            s.add(ModelCapability(model="m1", task_type="coding", score=0.9,
                                 source="gateway_yaml"))
            s.add(ModelCapability(model="m1", task_type="coding", score=0.4,
                                 source="livebench"))
            s.commit()
        engine.dispose()
        matrix = load_capability_matrix(db)
        assert matrix["coding"]["m1"] == 0.9  # higher-priority source wins (418)

    def test_entry_guard(self):
        _exec_module_guards("src.api.seed_capabilities")
