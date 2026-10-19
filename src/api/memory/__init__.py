"""Memory plugin — module-level API.

``init_memory(config)`` builds a :class:`LanceDBMemoryBackend` when the memory
module is installed (lancedb importable) AND ``plugins.memory.enabled`` is not
False. It never fails boot: a missing module / disabled config degrades to
``get_memory() -> None`` and the HTTP layer returns 501 with a Setup hint.

The embedder is resolved lazily on first use: fastembed (ONNX) by default,
with sentence-transformers as a fallback for model names fastembed does not
ship. The backend for this image is chosen at build time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any, Optional

from ..logging_config import get_logger
from .base import MemoryBackend
from .embeddings import EmbeddingModel, embedder_from_config

if TYPE_CHECKING:  # pragma: no cover — runtime import only for type hints
    from ..component import Component
    from ..runtime import Runtime
else:
    from ..component import Component
    from ..runtime import Runtime

logger = get_logger("lcp.memory")

_backend: Optional[MemoryBackend] = None


# ── Install-time paths (mirror src.api.setup naming) ────────────────────────

def memory_site() -> str:
    """Return the persistent site-packages dir for memory deps.

    ``<LCP_MODULES_DIR>/memory`` — pip installs ``--target`` here so lancedb +
    sentence-transformers survive container recreation, and ``remove_memory``
    can delete it without touching LiveBench's shared ``site``.
    """
    root = os.environ.get("LCP_MODULES_DIR", "").strip() or "/opt/lcp-modules"
    return os.path.join(root, "memory")


# Shared, baked image path for the embedding model weights. Both the semantic
# router and the memory plugin embed with the SAME model (BAAI/bge-small-en-v1.5),
# so ONE baked cache serves both. It must live under /app (not bind-mounted) —
# /opt/lcp-modules and /app/data are bind-mounted at runtime and would shadow
# any image content baked there.
_BAKED_MODELS_DIR = "/app/models/embedding"


def memory_models() -> str:
    """Return the directory used to cache the memory embedding model weights."""
    if os.path.isdir(_BAKED_MODELS_DIR):
        return _BAKED_MODELS_DIR
    root = os.environ.get("LCP_MODULES_DIR", "").strip() or "/opt/lcp-modules"
    return os.path.join(root, "models", "memory")


def router_site() -> str:
    """Return the site-packages dir for the SEMANTIC ROUTING module deps.

    Only used by the LEAN-image runtime installer (WITH_ROUTER=0):
    ``pip install --target <LCP_MODULES_DIR>/router``. In the default baked
    image the deps live in the image site-packages and this dir is unused.
    """
    root = os.environ.get("LCP_MODULES_DIR", "").strip() or "/opt/lcp-modules"
    return os.path.join(root, "router")


def router_models() -> str:
    """Return the directory used to cache the router embedding model weights."""
    if os.path.isdir(_BAKED_MODELS_DIR):
        return _BAKED_MODELS_DIR
    root = os.environ.get("LCP_MODULES_DIR", "").strip() or "/opt/lcp-modules"
    return os.path.join(root, "models", "router")


def memory_available(site: Optional[str] = None) -> bool:
    """Return True when the memory module's Python deps are importable.

    Probes with a FRESH subprocess (like ``core_deps_available``) so an
    editable/``--target`` install that only registers a finder at interpreter
    startup is detected correctly. When *site* is given it is prepended to
    ``PYTHONPATH`` for the probe.
    """
    env = dict(os.environ)
    if site:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = site if not existing else f"{site}{os.pathsep}{existing}"
    probe = (
        "import importlib.util, sys;"
        "sys.exit(0 if importlib.util.find_spec('lancedb') and "
        "importlib.util.find_spec('sentence_transformers') else 1)"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=60, env=env,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 — treat as unavailable
        return False


# ── Runtime state ───────────────────────────────────────────────────────────

def init_memory(config=None) -> bool:
    """Initialize the memory backend from config. Returns True when active.

    Never raises: any failure logs and leaves ``get_memory()`` returning None
    (endpoints report 501 with a Setup hint).
    """
    global _backend
    try:
        plugins = (getattr(config, "plugins", None) or {}) if config is not None else {}
        mem_cfg = plugins.get("memory") or {}
        if not mem_cfg.get("enabled", True):
            logger.info("memory_disabled_by_config")
            _backend = None
            return False

        # A non-string storage_path (e.g. a MagicMock from a test or a dubious
        # config) must NEVER reach a path API — os.makedirs would silently
        # create a directory tree named after the mock (MagicMock/<chain>/<id>)
        # instead of raising. Treat "configured but not a string" as a
        # misconfiguration: disable memory rather than guess; the default path
        # is only used when storage_path is absent/empty (init_memory(None)).
        raw_storage = mem_cfg.get("storage_path")
        if raw_storage is not None and not isinstance(raw_storage, str):
            logger.warning(
                "memory_storage_path_invalid",
                detail=f"storage_path must be a string, got {type(raw_storage).__name__}",
            )
            _backend = None
            return False
        storage = raw_storage.strip() if isinstance(raw_storage, str) else ""
        if not storage:
            data_dir = None
            try:
                data_dir = os.environ.get("COST_DB", "")
            except Exception:
                data_dir = ""
            base = os.path.dirname(data_dir) if data_dir else "data"
            storage = os.path.join(base, "memory")

        embedder: Optional[EmbeddingModel] = None
        try:
            embedder = embedder_from_config(mem_cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_embedder_init_failed", error=str(exc))

        # Lean images (WITH_MEMORY=0) install the memory module at runtime into
        # memory_site(). Make that dir importable IN-PROCESS (PYTHONPATH only
        # affects fresh interpreters) so lancedb resolves without a restart.
        site = memory_site()
        if site and site not in sys.path:
            sys.path.append(site)

        from .lancedb_backend import LanceDBMemoryBackend

        dim = int((mem_cfg.get("embedding") or {}).get("dim", 384) or 384)
        _backend = LanceDBMemoryBackend(
            storage,
            embed=lambda texts: embedder.embed(texts) if embedder else _noop_embed(texts),
            dim=dim,
        )
        logger.info("memory_initialized", storage=storage, dim=dim)
        return True
    except Exception as exc:  # noqa: BLE001 — never block boot
        logger.warning("memory_init_failed", error=str(exc))
        _backend = None
        return False


def _noop_embed(texts: list[str]) -> list[list[float]]:
    """Fallback embedder used only when the model can't be built.

    Produces a deterministic zero-ish vector per text so retain/recall never
    hard-fail before the module is installed; recall will simply be low-signal.
    """
    vec = [0.0] * 384
    return [list(vec) for _ in texts]


def get_memory() -> Optional[MemoryBackend]:
    """Return the active memory backend (or None when unavailable).

    The MemoryComponent's setup() delegates to init_memory(), which sets the
    module-level ``_backend`` — so this accessor is unchanged and correct for
    both the legacy boot path and the runtime path.
    """
    return _backend


def memory_status() -> dict:
    """Return a status dict for the UI / manifest.

    ``available`` reflects the installed module (importable deps);
    ``removable`` is True ONLY when the deps live exclusively in the module's
    own ``--target`` dir (a runtime install on a lean image). When the deps
    are also/only in the interpreter site-packages (baked image), ``available``
    is True but ``removable`` is False — deleting the module dir would be a
    no-op, so the Setup UI must not offer Remove/Reinstall.
    ``active`` reflects the runtime backend (config-enabled + initialized).
    """
    available = memory_available(memory_site())
    return {
        "available": available,
        "removable": available and not memory_available(None),
        "active": _backend is not None,
        "site": memory_site(),
        "models_dir": memory_models(),
    }


# ── Semantic routing module status ─────────────────────────────────────────
# The embedding-based task classifier is its OWN installable module ("router"),
# independent of the memory plugin. It reuses the same availability probe
# (sentence-transformers importable) but against the router deps dir.

def router_available(site: Optional[str] = None) -> bool:
    """Return True when the router module's Python deps are importable.

    Probes with a FRESH subprocess (like ``memory_available``) so a --target
    install is detected correctly. When *site* is given it is prepended to
    ``PYTHONPATH`` for the probe.
    """
    env = dict(os.environ)
    if site:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = site if not existing else f"{site}{os.pathsep}{existing}"
    probe = (
        "import importlib.util, sys;"
        "sys.exit(0 if importlib.util.find_spec('sentence_transformers') else 1)"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=60, env=env,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001 — treat as unavailable
        return False


def router_status() -> dict:
    """Return a status dict for the semantic routing module (UI / manifest).

    ``removable`` follows the same rule as ``memory_status``: True only when
    sentence-transformers is importable EXCLUSIVELY via the router ``--target``
    dir (runtime install on a lean image); False when baked into the image
    site-packages.
    """
    site = router_site()
    available = router_available(site)
    return {
        "available": available,
        "removable": available and not router_available(None),
        "active": available,
        "site": site,
        "models_dir": router_models(),
    }


def shutdown_memory() -> None:
    """Release the backend and its embedder (best-effort)."""
    global _backend
    _backend = None
    logger.info("memory_shutdown")


# ── Component-runtime adapter (Phase C) ────────────────────────────────────


def bind_runtime(rt: "Runtime") -> None:
    """Bind an active Runtime so ``get_memory()`` delegates to it."""
    from ..runtime import bind_active_runtime
    bind_active_runtime(rt)


class MemoryComponent(Component):
    """The memory backend as a runtime component.

    ``requires=["config"]``. ``setup`` reuses the existing ``init_memory``
    (which preserves the baked-cache + lean-build sys.path fallback); the
    disposer is ``shutdown_memory``. Best-effort: a missing/disabled module
    leaves the component active but with ``get_memory() -> None`` (endpoints
    report 501), matching boot behavior.
    """

    name = "memory"
    requires = ["config"]
    provides = ["memory"]

    @property
    def service(self):
        return get_memory()

    def setup(self, rt: "Runtime") -> Optional[Any]:
        init_memory(rt.resolve("config"))
        return shutdown_memory
