"""Boot-path smoke tests for the entry point (src/main.py) and the server
factory (src/server/server.py).

These were the two least-covered modules (0% and 43%): they are bootstrap
code that only runs when the process starts, so unit tests mock the heavy
dependencies and verify the wiring. Since Phase D, ``main()`` boots the
component runtime (src/api/runtime.py); ``build_runtime()`` is tested both
with mocked components (wiring) and with a real temp DB (integration).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def boot_config():
    """A minimal config object that main() can consume."""
    cfg = MagicMock()
    cfg.server = {"port": 8734}
    cfg.database = {"path": "/tmp/test.db"}
    cfg.profiles = {"l2": {"chain": []}, "l1": {"chain": []}}
    cfg.providers = {}
    cfg.dynamic_routing = {"enabled": False, "cost_bias": 0.15}
    return cfg


@pytest.fixture(autouse=True)
def _runtime_cleanup():
    """Reset every facade's bound runtime after each test so a runtime bound
    here never leaks into other test files (which fall back to the legacy
    module singletons)."""
    yield
    import src.api.runtime as runtime
    runtime._active_runtime = None


# ═══════════════════════════════════════════════════════════════════════
# src.main.main()
# ═══════════════════════════════════════════════════════════════════════

class TestMain:
    def test_main_starts_server_via_runtime(self, boot_config):
        import src.main
        server = MagicMock()
        engine = MagicMock()
        settings = MagicMock()
        rt = MagicMock()

        with patch.object(src.main, "get_engine", return_value=engine) as mock_engine, \
             patch.object(src.main, "setup_logging") as mock_log, \
             patch.object(src.main, "build_runtime", return_value=rt) as mock_build, \
             patch.object(src.main, "create_server", return_value=server) as mock_server, \
             patch("src.api.cost_cache.init_settings", return_value=settings) as mock_settings, \
             patch("src.api.config.init_config", return_value=boot_config) as mock_init_cfg:

            src.main.main()

        mock_log.assert_called_once()
        mock_engine.assert_called_once_with("/app/data/costs.db")
        mock_settings.assert_called_once_with(engine)
        mock_init_cfg.assert_called_once_with(store=settings)
        # Runtime built with the DB-backed config + resolved data dir.
        mock_build.assert_called_once_with(engine, boot_config, "/app/data/costs.db", "/app/data")
        # Background refresher started once the runtime is up.
        rt.resolve("refresher").refresher.start.assert_called_once()
        mock_server.assert_called_once_with(boot_config, engine, 8734)
        server.serve_forever.assert_called_once()

    def test_main_uses_env_overrides(self, boot_config):
        import src.main
        server = MagicMock()
        engine = MagicMock()

        with patch.object(src.main, "get_engine", return_value=engine) as mock_engine, \
             patch.object(src.main, "setup_logging"), \
             patch.object(src.main, "build_runtime", return_value=MagicMock()), \
             patch.object(src.main, "create_server", return_value=server), \
             patch("src.api.cost_cache.init_settings", return_value=MagicMock()), \
             patch("src.api.config.init_config", return_value=boot_config), \
             patch.dict("os.environ", {"COST_DB": "/env/costs.db", "LISTEN_PORT": "9000"}, clear=False):

            src.main.main()

        mock_engine.assert_called_once_with("/env/costs.db")
        assert server.serve_forever is not None

    def test_main_shuts_down_runtime_on_keyboard_interrupt(self, boot_config):
        import src.main
        server = MagicMock()
        server.serve_forever.side_effect = KeyboardInterrupt()
        engine = MagicMock()
        rt = MagicMock()

        with patch.object(src.main, "get_engine", return_value=engine), \
             patch.object(src.main, "setup_logging"), \
             patch.object(src.main, "build_runtime", return_value=rt), \
             patch.object(src.main, "create_server", return_value=server), \
             patch("src.api.cost_cache.init_settings", return_value=MagicMock()), \
             patch("src.api.config.init_config", return_value=boot_config):

            src.main.main()  # should not raise

        server.shutdown.assert_called_once()
        # Runtime.shutdown replays component disposers in LIFO order.
        rt.shutdown.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════
# src.main.build_runtime() — wiring (mocked runtime/components)
# ═══════════════════════════════════════════════════════════════════════

class TestBuildRuntime:
    def test_registers_all_components_and_starts(self, boot_config):
        import src.main
        rt = MagicMock()
        engine = MagicMock()

        with patch("src.api.runtime.Runtime", return_value=rt) as runtime_cls, \
             patch("src.api.circuit_breaker.bind_runtime") as cb_bind, \
             patch("src.api.cost_cache.bind_runtime") as cc_bind, \
             patch("src.api.cost_plugins.bind_runtime") as cp_bind, \
             patch("src.api.key_manager.bind_runtime") as km_bind, \
             patch("src.api.credential_store.bind_runtime") as cs_bind, \
             patch("src.api.alert_manager.bind_runtime") as am_bind, \
             patch("src.api.router.bind_runtime") as router_bind, \
             patch("src.api.memory.bind_runtime") as mem_bind:

            result = src.main.build_runtime(engine, boot_config, "/tmp/test.db", "/tmp")

        assert result is rt
        runtime_cls.assert_called_once_with(config=boot_config, engine=engine, data_dir="/tmp")
        # Every module singleton from the legacy bootstrap is registered.
        names = [call.args[0].name for call in rt.register.call_args_list]
        assert names == [
            "settings", "cost_cache", "refresher", "circuit_breaker",
            "key_manager", "credential_store", "provider_type_store",
            "alert_manager", "dynamic_router", "memory", "cost_plugins",
            "prompt_cache", "token_verifier", "reasoning_store",
        ]
        rt.start.assert_called_once()
        # Every facade is bound to the runtime.
        for bind in (cb_bind, cc_bind, cp_bind, km_bind, cs_bind, am_bind, router_bind, mem_bind):
            bind.assert_called_once_with(rt)

    def test_router_component_wired_from_config(self, boot_config):
        """build_runtime folds config.dynamic_routing into RouterComponent."""
        import src.main
        boot_config.dynamic_routing = {"enabled": True, "cost_bias": 0.3}
        rt = MagicMock()

        with patch("src.api.runtime.Runtime", return_value=rt), \
             patch("src.api.circuit_breaker.bind_runtime"), \
             patch("src.api.cost_cache.bind_runtime"), \
             patch("src.api.cost_plugins.bind_runtime"), \
             patch("src.api.key_manager.bind_runtime"), \
             patch("src.api.credential_store.bind_runtime"), \
             patch("src.api.alert_manager.bind_runtime"), \
             patch("src.api.router.bind_runtime"), \
             patch("src.api.memory.bind_runtime"):

            src.main.build_runtime(MagicMock(), boot_config, "/data/costs.db", "/data")

        router_comp = next(call.args[0] for call in rt.register.call_args_list
                           if call.args[0].name == "dynamic_router")
        assert router_comp._db_path == "/data/costs.db"
        assert router_comp._enabled is True
        assert router_comp._cost_bias == 0.3

    def test_router_component_defaults_disabled(self, boot_config):
        """A config without dynamic_routing leaves the router disabled."""
        import src.main
        boot_config.dynamic_routing = None
        rt = MagicMock()

        with patch("src.api.runtime.Runtime", return_value=rt), \
             patch("src.api.circuit_breaker.bind_runtime"), \
             patch("src.api.cost_cache.bind_runtime"), \
             patch("src.api.cost_plugins.bind_runtime"), \
             patch("src.api.key_manager.bind_runtime"), \
             patch("src.api.credential_store.bind_runtime"), \
             patch("src.api.alert_manager.bind_runtime"), \
             patch("src.api.router.bind_runtime"), \
             patch("src.api.memory.bind_runtime"):

            src.main.build_runtime(MagicMock(), boot_config, "/data/costs.db", "/data")

        router_comp = next(call.args[0] for call in rt.register.call_args_list
                           if call.args[0].name == "dynamic_router")
        assert router_comp._enabled is False
        assert router_comp._cost_bias == 0.15


# ═══════════════════════════════════════════════════════════════════════
# src.main.build_runtime() — real boot (integration)
# ═══════════════════════════════════════════════════════════════════════

class TestBuildRuntimeRealBoot:
    def test_real_boot_activates_all_components_and_delegates(self, temp_db, mock_config, monkeypatch):
        """A full runtime boot against a real temp DB: every component starts
        and every facade delegates to its runtime-owned instance."""
        import src.main
        db_path, engine = temp_db
        data_dir = str(Path(db_path).parent)
        # Avoid building a real LanceDB backend in tests.
        monkeypatch.setattr("src.api.memory.init_memory", lambda cfg: False)

        rt = src.main.build_runtime(engine, mock_config, db_path, data_dir)

        try:
            for name in ("settings", "cost_cache", "refresher", "circuit_breaker",
                         "key_manager", "credential_store", "provider_type_store",
                         "alert_manager", "dynamic_router", "memory", "cost_plugins",
                         "prompt_cache", "token_verifier", "reasoning_store"):
                assert rt.is_active(name) is True, f"{name} inactive"

            from src.api.alert_manager import get_alert_manager
            from src.api.circuit_breaker import get_circuit_breaker
            from src.api.cost_cache import get_cost_cache, get_refresher, get_settings
            from src.api.cost_plugins import get_registry
            from src.api.credential_store import get_credential_store
            from src.api.key_manager import get_key_manager
            from src.api.router import get_dynamic_router
            from src.api.runtime import get_runtime, resolve_service

            assert get_settings() is rt.resolve("settings").store
            assert get_cost_cache() is rt.resolve("cost_cache").cache
            assert get_refresher() is rt.resolve("refresher").refresher
            assert get_circuit_breaker() is rt.resolve("circuit_breaker").breaker
            assert get_key_manager() is rt.resolve("key_manager").manager
            assert get_credential_store() is rt.resolve("credential_store").store
            assert get_alert_manager() is rt.resolve("alert_manager").manager
            assert get_dynamic_router() is rt.resolve("dynamic_router").router
            assert get_registry() is rt.resolve("cost_plugins").registry
            # Phase F: the request path resolves the same runtime-owned
            # instances through the central accessor.
            assert get_runtime() is rt
            assert resolve_service("settings") is rt.resolve("settings").store
            assert resolve_service("cost_cache") is rt.resolve("cost_cache").cache
            assert resolve_service("circuit_breaker") is rt.resolve("circuit_breaker").breaker
            assert resolve_service("key_manager") is rt.resolve("key_manager").manager
            assert resolve_service("pricing") is rt.resolve("cost_plugins").registry
            assert resolve_service("alert_manager") is rt.resolve("alert_manager").manager
            # Router reflects the DB-backed baseline (no UI override set).
            assert rt.resolve("dynamic_router").router.enabled is False
            # The refresher received the PROVIDED cache + store (not the
            # component instances) — the fix for the scrape-time AttributeError.
            refresher = rt.resolve("refresher").refresher
            assert refresher._cache is rt.resolve("cost_cache").cache
            assert refresher._settings is rt.resolve("settings").store
        finally:
            rt.shutdown()
            # Shutdown marks every component inactive (disposers replayed LIFO).
            assert rt.is_active("settings") is False
            assert rt.is_active("cost_plugins") is False


# ═══════════════════════════════════════════════════════════════════════
# src.server.server.create_server()
# ═══════════════════════════════════════════════════════════════════════

class TestCreateServer:
    def test_creates_http_server(self, boot_config):
        from src.server.server import create_server
        engine = MagicMock()
        http_server_cls = MagicMock()

        with patch("src.server.server.ThreadingHTTPServer", http_server_cls):
            result = create_server(boot_config, engine, 8734)

        # ThreadingHTTPServer constructed with address + handler class
        addr, handler_cls = http_server_cls.call_args[0]
        assert addr == ("0.0.0.0", 8734)
        # The configured handler inherits LCPHandler and carries config/engine
        assert handler_cls.config is boot_config
        assert handler_cls.engine is engine
        assert result == http_server_cls.return_value

    def test_never_force_inits_legacy_singletons(self, boot_config):
        """The factory never force-inits the key manager / credential store —
        the component runtime owns them (Phase D)."""
        from src.server.server import create_server
        engine = MagicMock()
        http_server_cls = MagicMock()

        with patch("src.server.server.ThreadingHTTPServer", http_server_cls), \
             patch("src.api.key_manager.KeyManager") as mock_km_cls, \
             patch("src.api.credential_store.CredentialStore") as mock_cs_cls:

            create_server(boot_config, engine, 8734)

        mock_km_cls.assert_not_called()
        mock_cs_cls.assert_not_called()
