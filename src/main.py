"""LCP gateway — entry point. Loads env, boots the component runtime, serves."""

import os
import time

from .api.logging_config import setup_logging, get_logger
from .api.models import get_engine, Base
from .api.runtime import Runtime
from .server import create_server

logger = get_logger("lcp.main")


def _startup_step(name: str, t0: float) -> float:
    """Log a startup step and its duration. Returns the new timestamp."""
    t1 = time.monotonic()
    logger.info("startup_step", step=name, elapsed_ms=round((t1 - t0) * 1000, 1))
    return t1


def build_runtime(engine, config, db_path: str, data_dir: str) -> Runtime:
    """Construct, register, bind, and start the component runtime.

    The runtime owns every module singleton the legacy 16-step bootstrap used
    to hand-sequence:

    * ``settings`` / ``cost_cache`` / ``refresher`` (cost_cache.py)
    * ``circuit_breaker`` (circuit_breaker.py)
    * ``key_manager`` / ``credential_store``
    * ``alert_manager``
    * ``dynamic_router`` (router.py — settings-toggle folded into setup)
    * ``memory`` (memory/__init__.py — best-effort, never blocks boot)
    * ``cost_plugins`` (cost_plugins/base.py — engine at construction)
    * dep-free leaves ``prompt_cache`` / ``token_verifier`` / ``reasoning_store``

    Components are topologically ordered by ``requires``/``provides`` at
    ``start()``; each module facade is bound so the existing ``get_*()``
    accessors delegate to the runtime instead of the legacy singletons.
    """
    from .api import (
        alert_manager as alert_manager_mod,
        circuit_breaker as circuit_breaker_mod,
        cost_cache as cost_cache_mod,
        cost_plugins as cost_plugins_mod,
        credential_store as credential_store_mod,
        key_manager as key_manager_mod,
        memory as memory_mod,
        prompt_cache as prompt_cache_mod,
        reasoning_store as reasoning_store_mod,
        router as router_mod,
        runtime as runtime_mod,
        token_verifier as token_verifier_mod,
    )

    # Referenced through the module (not the module-level import) so tests can
    # patch ``src.api.runtime.Runtime`` and observe the wiring.
    rt = runtime_mod.Runtime(config=config, engine=engine, data_dir=data_dir)

    # Baseline routing flags from config.dynamic_routing (mirrors the legacy
    # init_router call). The RouterComponent folds in the persisted UI toggle
    # during setup, so the post-init sync is no longer needed at boot.
    dr = {}
    try:
        dr = config.dynamic_routing or {}
        if not isinstance(dr, dict):
            dr = {}
    except Exception:  # noqa: BLE001 — mocked/partial configs fall back to disabled
        dr = {}

    rt.register(cost_cache_mod.SettingsComponent())
    rt.register(cost_cache_mod.CostCacheComponent())
    rt.register(cost_cache_mod.RefresherComponent())
    rt.register(circuit_breaker_mod.CircuitBreakerComponent())
    rt.register(key_manager_mod.KeyManagerComponent())
    rt.register(credential_store_mod.CredentialStoreComponent())
    rt.register(alert_manager_mod.AlertManagerComponent())
    rt.register(router_mod.RouterComponent(
        db_path=db_path,
        enabled=bool(dr.get("enabled", False)),
        cost_bias=float(dr.get("cost_bias", 0.15)),
    ))
    rt.register(memory_mod.MemoryComponent())
    rt.register(cost_plugins_mod.CostPluginsComponent())
    rt.register(prompt_cache_mod.PromptCacheComponent())
    rt.register(token_verifier_mod.TokenVerifierComponent())
    rt.register(reasoning_store_mod.ReasoningStoreComponent())

    # Bind the runtime to each module facade so the get_*() accessors delegate
    # to the runtime's components instead of the legacy module singletons.
    for _mod in (circuit_breaker_mod, cost_cache_mod, cost_plugins_mod,
                 key_manager_mod, credential_store_mod, alert_manager_mod,
                 router_mod, memory_mod):
        _mod.bind_runtime(rt)

    rt.start()
    return rt


def main():
    """Entry point — start the HTTP server via the component runtime."""
    _boot = time.monotonic()
    t0 = _boot

    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))

    # ── Bootstrap: the DB path + listen port come from env (with seed
    #    defaults) so we can open the DB BEFORE reading the DB-backed config.
    from .api.config import _env_db_path, _env_port, init_config
    db_path = _env_db_path()
    port = _env_port()
    logger.info("startup_begin", version=__import__("src").__version__,
                port=port, log_level=os.environ.get("LOG_LEVEL", "INFO"))

    # ── Database (must exist before the DB-backed config can be read) ────
    assert db_path is not None
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    t0 = _startup_step("db_init", t0)
    logger.info("db_initialized", path=db_path)

    # ── DB-backed config: settings store → Config (seeds missing sections
    #    from the Python seed on first boot).
    from .api.cost_cache import init_settings
    settings = init_settings(engine)
    config = init_config(store=settings)
    # Persist the seed to the DB on first boot so the DB is the materialised
    # source of truth (idempotent — later boots load from the DB and skip).
    try:
        if settings.config_sections() == []:
            config.save()
            logger.info("config_seeded_db")
    except Exception:  # noqa: BLE001 — never block boot
        logger.warning("config_seed_failed", error=True)
    t0 = _startup_step("config_load", t0)
    logger.info("config_loaded_db")

    # ── Component runtime: config/engine/data_dir roots, topo-ordered setup,
    #    LIFO teardown. Every module singleton from the legacy bootstrap is
    #    owned here.
    data_dir = os.path.dirname(db_path) if db_path else "data"
    rt = build_runtime(engine, config, db_path, data_dir)
    t0 = _startup_step("runtime_start", t0)

    # The refresher owns ALL live cost scraping; start its background thread
    # (the component's disposer stops it on shutdown).
    try:
        rt.resolve("refresher").refresher.start()
    except Exception:  # noqa: BLE001 — never block boot
        logger.warning("refresher_start_failed", error=True)

    server = create_server(config, engine, port)
    t0 = _startup_step("server_create", t0)

    profiles = list(config.profiles.keys()) if hasattr(config, "profiles") else []
    providers = list(config.providers.keys()) if hasattr(config, "providers") else []
    logger.info("startup_complete", port=port, profiles=profiles, providers=providers,
                total_boot_ms=round((time.monotonic() - _boot) * 1000, 1))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
        server.shutdown()
    finally:
        # Runtime.shutdown replays component disposers in LIFO order (reverse
        # of setup) — this stops the refresher, releases memory, runs plugin
        # on_shutdown (llamacpp persist), etc.
        rt.shutdown()


if __name__ == "__main__":
    main()
