"""First-run setup wizard — installable module manifest + install coordinator.

LCP ships a small number of self-contained "plugins" that are each installed
(registered) once:

  - **Provider cost plugins** — ``deepseek``, ``opencode``, ``commandcode``,
    ``llamacpp``. "Installation" means adding the provider to the gateway
    config (the settings DB — ``config.save()``), reusing the same
    preset + provider-create machinery as the Providers
    page) and — for API-keyed providers — storing the key encrypted in the
    credential store. Local/credential-free steps (``llamacpp``) or steps that
    only need a cookie/workspace-id are config-apply only.
  - **Optional modules** (Memory, Semantic routing, Runboard) — runtime
    installs (pip install into the running container) that run in the
    background and report their progress in real time. There is no benchmark
    module: capability scores are declared data (see ``seed_capabilities``).

State is persisted in the ``setup_state`` table so the wizard knows which
steps are done, skipped, or failed. The Setup page reports the manifest
(kind, name, title, installed, required, etc.) and drives the
``POST /api/setup/install/{kind}/{name}`` + ``GET /api/setup/progress``
handshake.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from .logging_config import get_logger

logger = get_logger("lcp.setup")

# Root directory where runtime-installed modules live. Override with
# ``LCP_MODULES_DIR`` (default ``/opt/lcp-modules``). Each module clones into
# its own subdirectory, e.g. ``<modules_dir>/router``.
MODULES_DIR_ENV = "LCP_MODULES_DIR"
DEFAULT_MODULES_DIR = "/opt/lcp-modules"

# Cap on the retained install log for every runtime module install (memory,
# router, runboard). Shared by the per-module ``_*_update`` writers, which is
# why it lives here rather than inside any one module's section.
_LOG_MAX_LINES = 300


class SetupError(Exception):
    """Raised for synchronous, user-facing setup failures."""


def modules_dir() -> str:
    """Return the configured module install root (env or default)."""
    return os.environ.get(MODULES_DIR_ENV, "").strip() or DEFAULT_MODULES_DIR


def memory_site() -> str:
    """Return the persistent site-packages dir for memory module deps.

    ``<LCP_MODULES_DIR>/memory`` — pip installs ``--target`` here so lancedb +
    sentence-transformers survive container recreation, and ``remove_memory``
    can delete it without disturbing any other module's deps dir.
    """
    return os.path.join(modules_dir(), "memory")


def memory_models_dir() -> str:
    """Return the directory used to cache the embedding model weights."""
    return os.path.join(modules_dir(), "models", "memory")


def router_site() -> str:
    """Return the persistent site-packages dir for the SEMANTIC ROUTING module.

    ``<LCP_MODULES_DIR>/router`` — pip installs ``--target`` here (sentence-
    transformers + torch) so the embedding-based task classifier is independent
    of the memory plugin's install.
    """
    return os.path.join(modules_dir(), "router")


def router_models_dir() -> str:
    """Return the directory used to cache the router embedding model weights."""
    return os.path.join(modules_dir(), "models", "router")


def runboard_site() -> str:
    """Return the persistent deps dir for the RUNBOARD observability module.

    ``<LCP_MODULES_DIR>/runboard`` — pip installs ``--target`` here (the judge's
    sentence-transformers/torch stack) so it survives container recreation and
    can be removed independently of the router and memory modules.
    """
    return os.path.join(modules_dir(), "runboard")


def runboard_models_dir() -> str:
    """Return the directory used to cache the runboard judge model weights.

    ``<LCP_MODULES_DIR>/models/runboard`` — keeps the rlcd-modernbert weights
    out of the shared ``models`` root so removing the module removes them.
    """
    return os.path.join(modules_dir(), "models", "runboard")


def _db_path_from_engine(engine) -> Optional[str]:
    """Return the SQLite file path backing *engine* (or None)."""
    try:
        if getattr(engine, "url", None) is not None:
            path = str(engine.url.database)
            if path and path != ":memory:":
                return path
    except Exception:  # noqa: BLE001 — duck-typed engines in tests
        pass
    return None


# ── Manifest ────────────────────────────────────────────────────────────────

def provider_steps(config) -> list[dict]:
    """Build the provider manifest from configured providers + plugin presets.

    A provider is "installed" when it exists in ``config.providers`` AND (for
    providers with an API-keyed plugin preset) a credential or cookie/workspace
    id is present. ``llamacpp`` has no key/cookie — config presence is enough.
    """
    from .credential_store import get_credential_store

    configured = set(config.providers.keys()) if config and hasattr(config, "providers") else set()
    store = get_credential_store()

    steps: list[dict] = []
    for name in ("deepseek", "opencode", "commandcode", "llamacpp"):
        has_cred = bool(store and (store.has(name) or store.has_cookie(name) or store.has_workspace_id(name)))
        preset = _provider_preset(name)
        steps.append({
            "kind": "provider",
            "name": name,
            "title": _PROVIDER_TITLES.get(name, name),
            "description": _PROVIDER_DESCRIPTIONS.get(name, ""),
            "required": name in ("deepseek", "opencode", "commandcode"),
            "needs_key": name in ("deepseek", "opencode", "commandcode"),
            "configured": name in configured,
            "has_credential": has_cred,
            "installed": name in configured and (not _provider_needs_key(name) or has_cred),
            "api_base": preset.get("api_base", ""),
        })
    return steps


def memory_step() -> dict:
    """Build the memory module manifest entry."""
    from .memory import memory_status

    status = memory_status()
    installing = _mem_install
    if installing is None and _mem_last is not None and _mem_last.get("status") == "failed":
        installing = _mem_last
    return {
        "kind": "module",
        "name": "memory",
        "title": "Memory (LanceDB)",
        "description": (
            "Per-profile semantic memory bank — store facts, recall by "
            "embedding similarity. Install lancedb + sentence-transformers "
            "at runtime."
        ),
        "required": False,
        "installed": bool(status.get("available")),
        # True when the deps are baked into the image site-packages (not the
        # module --target dir), so the UI must NOT offer Remove (it would be a
        # no-op) — nothing to reinstall either.
        "baked": bool(status.get("available")) and not bool(status.get("removable")),
        "status": status,
        "install_path": memory_site(),
        "installing": installing,
    }


def router_install_blocked_reason(db_path: Optional[str] = None) -> Optional[str]:
    """Return why the Semantic routing module can't be installed (or None).

    Semantic routing classifies a prompt by MEANING into a task type, but the
    router then routes by that task's capability scores. So the real
    prerequisite is DECLARED CAPABILITY DATA in the matrix. The bundled
    declared matrix (``seed_capabilities``) is the producer, and individual
    scores can also be set by hand on the Models page.

    With *db_path* the gate keys on the matrix actually having rows, and the
    reason is tailored to the state:

    * matrix non-empty -> None (installable)
    * matrix empty     -> a reason naming the one thing that fills it: declare
                          the scores (Models page, manual) or re-apply the
                          bundled declared matrix via the seed action.

    Without *db_path* (e.g. no engine in tests) it cannot read the matrix, so it
    assumes empty and returns that reason.
    """
    if db_path:
        try:
            from .seed_capabilities import load_capability_matrix
            if load_capability_matrix(db_path):
                return None
        except Exception:  # noqa: BLE001 — DB may be unavailable; fall back below
            pass

    return (
        "No capability data to route by. Declare model scores on the Models "
        "page, or run the seed action to re-apply the bundled declared matrix, "
        "then install Semantic routing."
    )


def capability_matrix_stats(db_path: Optional[str] = None) -> dict:
    """Return a compact summary of the ACTIVE capability matrix for the UI.

    ``{"models": N, "tasks": M}`` — counts the distinct models and task types
    the router actually consumes. ``{"models": 0, "tasks": 0}`` when there are
    no graded rows yet (or the DB is unavailable).
    """
    if not db_path:
        return {"models": 0, "tasks": 0}
    try:
        from .seed_capabilities import load_capability_matrix
        matrix = load_capability_matrix(db_path)
    except Exception:  # noqa: BLE001 — DB may be unavailable
        return {"models": 0, "tasks": 0}
    models = {m for scores in matrix.values() for m in scores}
    return {"models": len(models), "tasks": len(matrix)}


def router_step(engine=None) -> dict:
    """Build the SEMANTIC ROUTING module manifest entry.

    The embedding-based task classifier powers dynamic routing: it picks the
    task type by MEANING (not keywords), which capability scores then route to
    the best-fit model. Install sentence-transformers into its own deps dir,
    independent of the memory plugin. Install is blocked (``blocked_reason``)
    until DECLARED CAPABILITY DATA exists (the bundled matrix or scores set
    by hand on the Models page).
    """
    from .memory import router_status

    status = router_status()
    installing = _router_install
    if installing is None and _router_last is not None and _router_last.get("status") == "failed":
        installing = _router_last
    db_path = _db_path_from_engine(engine)
    return {
        "kind": "module",
        "name": "router",
        "title": "Semantic routing",
        "description": (
            "Embedding-based task classification for the dynamic router — "
            "classifies prompts by meaning so they route to the best-fit "
            "model. Requires declared capability data (the bundled matrix or "
            "scores set by hand). Install sentence-transformers at runtime."
        ),
        "required": False,
        "installed": bool(status.get("available")),
        # Same baked vs runtime-installed distinction as the memory module:
        # baked-in deps can't be removed, so no Remove button in the UI.
        "baked": bool(status.get("available")) and not bool(status.get("removable")),
        # Install is gated on graded capability data (semantic routing's real
        # dependency). None when installable.
        "blocked_reason": None if bool(status.get("available")) else router_install_blocked_reason(db_path),
        # What the router routes by — surfaced on this card so a missing matrix
        # is visible next to the install button it gates.
        "capability": capability_matrix_stats(db_path),
        "status": status,
        "install_path": router_site(),
        "installing": installing,
    }


def runboard_step(engine=None) -> dict:
    """Build the RUNBOARD observability module manifest entry.

    runboard is the run/telemetry board (engine pressure, throughput, latency,
    hygiene) plus the notice board. It was previously a standalone Flask service
    on :8090; it now installs into LCP as a module so there is ONE surface.

    ``required`` is False: LCP is fully functional without it. Note the module
    is READ-ONLY against its data dir by design — host-side collectors write
    ``state.json``/``zgx.json``, and the judge's verdicts are merged into
    ``state.json`` rather than written from inside a read-only container.
    """
    return {
        "kind": "module",
        "name": "runboard",
        "title": "Runboard (run telemetry + notice board)",
        "description": (
            "Run telemetry board and notice board, merged in from the former "
            "standalone runboard service. Serves the run registry, live engine "
            "metrics, benchmarks and ZGX adapters, and hosts the judged notice "
            "board. Install the judge's embedding stack at runtime."
        ),
        "required": False,
        "installed": os.path.isdir(runboard_site()),
        "baked": False,
        "blocked_reason": None,
        "status": {"available": os.path.isdir(runboard_site())},
        "install_path": runboard_site(),
        "models_path": runboard_models_dir(),
        "installing": None,
    }


def manifest(config, engine=None) -> dict:
    """Return the full setup manifest (provider steps + benchmark + modules)."""
    return {
        "steps": provider_steps(config),
        "modules": [
            router_step(engine),
            memory_step(),
            runboard_step(engine),
            # The workspace module was retired in M2b: the work surfaces (Tasks,
            # Fleet) are always available, so there was nothing left for an
            # install/uninstall pair to gate. Its paths are configured per profile
            # on the Profiles > Config tab.
        ],
    }


# ── State helpers (kept in src.api.setup so handlers don't touch models) ────

def load_state(engine) -> dict[str, dict]:
    """Return {key: {status, updated_at, detail}} from the setup_state table."""
    from .models import SetupState, get_session

    try:
        with get_session(engine) as session:
            rows = session.query(SetupState).all()
            return {
                r.key: {
                    "status": r.status,
                    "updated_at": r.updated_at,
                    "detail": r.detail,
                }
                for r in rows
            }
    except Exception as exc:  # noqa: BLE001 — table may not exist yet
        logger.warning("setup_state_read_failed", error=str(exc))
        return {}


def set_state(engine, key: str, status: str, detail: Optional[str] = None) -> None:
    """Upsert one setup state record."""
    from .models import SetupState, get_session

    now = datetime.now(timezone.utc).isoformat()
    with get_session(engine) as session:
        row = session.query(SetupState).filter(SetupState.key == key).first()
        if row is None:
            session.add(SetupState(key=key, status=status, detail=detail, updated_at=now))
        else:
            row.status = status
            row.detail = detail
            row.updated_at = now
        session.commit()


def mark_skipped(engine) -> bool:
    """Persist wizard completion (skip) and return True when newly marked."""
    if load_state(engine).get("wizard", {}).get("status") == "skipped":
        return False
    # Record a marker for the gate's skip check.
    set_state(engine, "wizard", "skipped")
    return True


def is_complete(engine, config) -> bool:
    """True when the wizard may be bypassed (all required steps done or skipped)."""
    if load_state(engine).get("wizard", {}).get("status") == "skipped":
        return True
    required = [s for s in provider_steps(config) if s.get("required")]
    if not required:
        return True
    return all(s["installed"] for s in required)


# ── Provider install ────────────────────────────────────────────────────────

_PROVIDER_TITLES = {
    "deepseek": "DeepSeek",
    "opencode": "OpenCode",
    "commandcode": "Command Code",
    "llamacpp": "Local LLM",
}
_PROVIDER_DESCRIPTIONS = {
    "deepseek": "Official DeepSeek API — pricing, cost + balance tracking.",
    "opencode": "OpenCode gateway — cost history + subscription usage.",
    "commandcode": "Command Code — billing API + gateway cost tracking.",
    "llamacpp": "Self-hosted local inference — token-based usage tracking.",
}


def _provider_needs_key(name: str) -> bool:
    return name in ("deepseek", "opencode", "commandcode")


def _provider_preset(name: str) -> dict:
    """Return the quick-add preset for a provider (from the plugin registry)."""
    from .cost_plugins import get_registry

    return get_registry().presets.get(name, {}) or {}


def install_provider(engine, config, name: str, body: dict) -> dict:
    """Install one provider plugin into the running config.

    Mirrors ``_serve_provider_create`` (same write path + credential handling)
    but is idempotent and returns a structured result.
    """
    if name not in ("deepseek", "opencode", "commandcode", "llamacpp"):
        raise SetupError(f"unknown provider: {name}")

    preset = _provider_preset(name)
    api_base = (body.get("api_base") or preset.get("api_base") or "").strip()
    models = body.get("models") or preset.get("models") or []
    if not isinstance(models, list):
        raise SetupError("'models' must be a list")

    if not api_base:
        raise SetupError(f"missing api_base for {name} (no preset available)")

    # Write the provider into the gateway config (settings DB) — the same path
    # the Providers page uses. `config.save()` persists every section to
    # `gateway_config:<section>` rows; there is no YAML file involved.
    cfg_raw = config.raw
    existing = cfg_raw.get("providers", {}).get(name, {})
    merged = dict(existing)
    if api_base:
        merged["api_base"] = api_base
    if models:
        merged["models"] = models
    # Preserve config-only keys like api_key_env / cache settings.
    cfg_raw.setdefault("providers", {})[name] = merged
    config.save()

    # Store the API key encrypted (never in the config) when provided.
    from .credential_store import get_credential_store

    store = get_credential_store()
    if store is None:
        raise SetupError("credential store not initialized")

    api_key = (body.get("api_key") or "").strip()
    if _provider_needs_key(name):
        if not api_key and not store.has(name):
            raise SetupError(f"missing api_key for {name}")

    if api_key:
        store.set(name, api_key)
    if body.get("cookie"):
        store.set_cookie(name, body["cookie"])
    if body.get("workspace_id"):
        store.set_workspace_id(name, body["workspace_id"])

    set_state(engine, f"provider:{name}", "done")
    logger.info("setup_provider_installed", provider=name)
    return {"installed": True, "provider": name}


def remove_provider(engine, config, name: str) -> dict:
    """Remove a provider plugin from the running config + credential store.

    Mirrors ``_serve_provider_delete`` but also clears the setup state marker.
    Returns ``{"removed": True, "provider": name}`` or raises ``SetupError``.
    """
    if name not in ("deepseek", "opencode", "commandcode", "llamacpp"):
        raise SetupError(f"unknown provider: {name}")

    # Remove the provider entry from the gateway config.
    cfg_raw = config.raw
    cfg_raw.setdefault("providers", {}).pop(name, None)
    # Drop it from every profile chain (same as the Providers page).
    for pcfg in cfg_raw.get("profiles", {}).values():
        chain = pcfg.get("chain")
        if isinstance(chain, list):
            pcfg["chain"] = [c for c in chain if c.get("provider") != name]
    config.save()

    # Clear encrypted credentials / cookies / workspace IDs.
    from .credential_store import get_credential_store

    store = get_credential_store()
    if store is not None:
        store.set(name, "")
        store.set_cookie(name, "")
        store.set_workspace_id(name, "")

    set_state(engine, f"provider:{name}", "removed")
    # Cascade the circuit-breaker health, same as the Providers page delete:
    # provider_health rows are reloaded wholesale at boot, so leaving them
    # behind resurrects the provider in /health forever.
    try:
        from .circuit_breaker import get_circuit_breaker
        get_circuit_breaker(config).forget_provider(name)
    except Exception as exc:  # noqa: BLE001 — removal must not fail on health cleanup
        logger.warning("setup_provider_health_cascade_failed",
                       provider=name, error=str(exc))
    logger.info("setup_provider_removed", provider=name)
    return {"removed": True, "provider": name}


# ── Memory module install (background + progress) ───────────────────────────

_mem_lock = threading.Lock()
_mem_install: Optional[dict] = None  # in-flight {status, progress, detail, log, ...}
_mem_last: Optional[dict] = None     # terminal result (done/failed) for the UI

# Memory deps are installed into their own --target dir (memory_site) so
# remove_memory can delete them without disturbing the other module dirs.
# tokenizers is pinned for the SAME reason as ROUTER_PACKAGES: transformers
# (a sentence-transformers dep) rejects tokenizers>0.23.0, and an unpinned
# --target copy in /opt/lcp-modules/memory would shadow the baked global
# install (this dir is on PYTHONPATH) and break the semantic classifier too.
MEMORY_PACKAGES = ["lancedb>=0.15", "sentence-transformers>=3.0", "tokenizers==0.22.2"]
MEMORY_MODEL = "BAAI/bge-small-en-v1.5"


def _mem_update(msg: Optional[str], progress: Optional[float] = None,
                status: Optional[str] = None) -> None:
    """Mutate the shared memory-install state (no-op when nothing in flight)."""
    global _mem_install
    if _mem_install is None:
        return
    if status is not None:
        _mem_install["status"] = status
    if progress is not None:
        _mem_install["progress"] = round(min(max(progress, 0.0), 100.0), 1)
    if msg is not None:
        clean = (msg.rstrip("\n") if isinstance(msg, str) else str(msg)).strip("\r")
        if clean:
            _mem_install["detail"] = clean[-200:]
            log = _mem_install.setdefault("log", [])
            log.append(clean)
            if len(log) > _LOG_MAX_LINES:
                del log[: len(log) - _LOG_MAX_LINES]
    _mem_install["updated_at"] = datetime.now(timezone.utc).isoformat()


def _mem_finish(status: str, detail: str) -> None:
    """Mark the memory install terminal and move state into ``_mem_last``."""
    global _mem_install, _mem_last
    _mem_update(detail, status=status)
    if status == "done":
        _mem_update(None, progress=100.0)
    if _mem_install is not None:
        _mem_install["finished_at"] = datetime.now(timezone.utc).isoformat()
        _mem_last = dict(_mem_install)
    _mem_install = None


def mem_progress() -> Optional[dict]:
    """Return the in-flight memory install state (or None when idle)."""
    return _mem_install


def mem_last() -> Optional[dict]:
    """Return the most recent terminal memory install result (or None)."""
    return _mem_last


def start_memory_install(engine) -> dict:
    """Start (or join) the memory module install and return its state."""
    global _mem_install, _mem_last

    with _mem_lock:
        if _mem_install is not None and _mem_install.get("status") in ("queued", "running"):
            return _mem_install
        _mem_last = None
        _mem_install = {
            "status": "queued",
            "progress": 0.0,
            "detail": "Waiting to start…",
            "log": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        state = dict(_mem_install)
        thread = threading.Thread(target=_run_memory_install, args=(engine,), daemon=True)
        thread.start()
        return state


def _run_memory_install(engine) -> None:
    """Background install: pip lancedb + sentence-transformers into memory_site,
    probe importability, and pre-download the embedding model weights."""
    try:
        site = memory_site()
        models_dir = memory_models_dir()
        _mem_update(f"Target directory: {site}")
        os.makedirs(site, exist_ok=True)
        os.makedirs(models_dir, exist_ok=True)

        _stream_mem(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir",
             "--target", site] + MEMORY_PACKAGES,
            cwd=None, start=2.0, end=70.0,
            status_msg="Installing memory deps (lancedb + sentence-transformers)…",
        )

        # Verify the deps actually became importable with the target dir on
        # PYTHONPATH (fresh subprocess probe, as in the router install).
        from .memory import memory_available
        if not memory_available(site):
            raise SetupError(
                "Memory deps install did not take effect. The deps are at "
                f"{site} — check the install log."
            )

        # Pre-download the embedding model into the persistent cache dir so the
        # first retain/recall doesn't hit the Hub at runtime.
        try:
            _stream_mem(
                [sys.executable, "-c",
                 "import os,sys;"
                 "os.environ.setdefault('LCP_MODULES_DIR', "
                 f"{modules_dir()!r});"
                 "from src.api.memory.embeddings import EmbeddingModel;"
                 f"m=EmbeddingModel({MEMORY_MODEL!r}, cache_dir={models_dir!r});"
                 "m.embed(['warmup'])",
                 ],
                cwd=None, start=70.0, end=100.0,
                status_msg="Pre-downloading embedding model…",
            )
        except subprocess.CalledProcessError as exc:
            _mem_update(f"Model pre-download skipped ({exc}) — will download on first use.")
            _mem_update(None, progress=100.0)

        set_state(engine, "module:memory", "done")
        _mem_finish("done", "Memory module installed.")
    except subprocess.CalledProcessError as exc:
        _mem_finish("failed", _tail_mem_detail(f"Install failed: {exc}"))
    except FileNotFoundError as exc:
        _mem_finish("failed", f"Missing tool: {exc}")
    except Exception as exc:  # noqa: BLE001 — background thread must not die
        _mem_finish("failed", _tail_mem_detail(f"Install failed: {exc}"))


def _stream_mem(cmd: list[str], cwd: Optional[str], start: float, end: float,
                status_msg: str) -> None:
    """Like ``_stream`` but updates the memory-install log."""
    _mem_update(status_msg, progress=start, status="running")
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, errors="replace",
    )
    seen = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        seen += 1
        _mem_update(line)
        if seen % 3 == 0:
            frac = min(0.9, seen / 90.0)
            _mem_update(None, progress=start + (end - start) * frac)
    rc = proc.wait()
    _mem_update(None, progress=end)
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


def _tail_mem_detail(fallback: str, lines: int = 6) -> str:
    """Return *fallback* plus real error lines from the memory install log."""
    global _mem_install
    if _mem_install is None:
        return fallback
    log = _mem_install.get("log") or []
    if not log:
        return fallback
    error_markers = (
        "traceback", "error", "fatal", "failed", "exception", "conflict",
        "cannot", "could not", "no matching", "not importable", "missing",
    )
    error_lines = [
        ln for ln in log
        if any(m in ln.lower() for m in error_markers)
        and "pip.pypa.io" not in ln.lower()
    ]
    picked = error_lines[-lines:] if error_lines else log[-lines:]
    tail = "\n".join(picked).strip()
    if not tail:
        tail = "\n".join(log[-lines:]).strip()
    if not tail:
        return fallback
    return f"{fallback}\n{tail[-800:]}"


# ── Baked-module removal ─────────────────────────────────────────────────────
# When a module is BAKED into the image, its deps live in the container's
# site-packages (writable image layer), so deleting the --target dir is a
# no-op. "Remove" then uninstalls the baked deps from the RUNNING container.
# This is runtime-only: a rebuild with the matching WITH_*=1 bake arg restores
# them. The shared deps (sentence-transformers/transformers/tokenizers/torch)
# are used by BOTH the router and memory modules, so removal is refused while
# the sibling module is also baked.
BAKED_ROUTER_PACKAGES = [
    "sentence-transformers",
    "transformers",
    "tokenizers",
    "torch",
]
BAKED_MEMORY_PACKAGES = [
    "lancedb",
    "sentence-transformers",
    "transformers",
    "tokenizers",
    "torch",
]


def _uninstall_baked_packages(packages: list[str]) -> list[str]:
    """pip uninstall *packages* from the container's site-packages.

    Best-effort and never raises. Returns the package names pip reports as
    successfully uninstalled; packages that aren't installed are skipped.
    """
    uninstalled: list[str] = []
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y",
             "--disable-pip-version-check", *packages],
            capture_output=True, text=True, timeout=300,
        )
        for line in proc.stdout.splitlines():
            if line.startswith("Successfully uninstalled"):
                uninstalled.append(line.split("Successfully uninstalled ", 1)[1].strip())
    except Exception as exc:  # noqa: BLE001 — removal must never crash the request
        logger.warning("baked_packages_uninstall_failed", error=str(exc))
    return uninstalled


def remove_memory(engine) -> dict:
    """Remove the memory module and clear setup state.

    Does NOT delete stored memories in the LanceDB data dir (that lives under
    the app data dir, not LCP_MODULES_DIR).

    Lean image: deletes the --target deps dir + model cache. Baked image
    (deps in the image site-packages): uninstalls the baked deps from the
    running container so the module becomes genuinely unavailable — a rebuild
    with WITH_MEMORY=1 restores them. Blocked while SEMANTIC ROUTING is also
    baked, because both share sentence-transformers/torch.
    """
    from .memory import memory_available, router_available

    removed: list[str] = []
    for path in (memory_site(), memory_models_dir()):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)

    if memory_available(None):
        # Baked into the image site-packages — the --target removal above was
        # a no-op, so uninstall the baked deps from the running container.
        if router_available(None):
            raise SetupError(
                "Cannot remove the Memory module: Semantic routing is also "
                "baked into the image and shares sentence-transformers/torch. "
                "Rebuild the image with WITH_ROUTER=0 and WITH_MEMORY=0 so "
                "both modules are runtime-managed, then remove them from the "
                "Setup page."
            )
        removed += _uninstall_baked_packages(BAKED_MEMORY_PACKAGES)

    set_state(engine, "module:memory", "removed")
    logger.info("setup_memory_removed", removed=removed)
    return {"removed": True, "module": "memory", "paths": removed}


def remove_runboard(engine) -> dict:
    """Remove the runboard module's runtime deps and clear setup state.

    Does NOT delete the ledger or the board's data. Those live under the app
    data dir (``state.json``, ``decisions.db``, ``zgx.json``) and are written by
    host-side collectors, not by LCP -- removing the module uninstalls its
    embedding stack, it does not destroy history.
    """
    removed: list[str] = []
    for path in (runboard_site(), runboard_models_dir()):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)

    set_state(engine, "module:runboard", "removed")
    logger.info("setup_runboard_removed", removed=removed)
    return {"removed": True, "module": "runboard", "paths": removed}


# ── Semantic routing module install (background + progress) ─────────────────
# Mirrors the memory module install but with its OWN deps dir (router_site) so
# semantic task classification is independent of the memory plugin.

_router_lock = threading.Lock()
_router_install: Optional[dict] = None  # in-flight {status, progress, detail, log, ...}
_router_last: Optional[dict] = None     # terminal result (done/failed) for the UI

ROUTER_PACKAGES = [
    "sentence-transformers>=3.0",
    # transformers (a sentence-transformers dep) requires tokenizers in
    # [0.22.0, 0.23.0]. 0.23.0 does NOT exist on PyPI (only 0.23.0rc0 and
    # 0.23.1, which is out of range), so the newest satisfying release is
    # 0.22.2. Unpinned, pip resolves 0.23.1 and sentence-transformers refuses
    # to import ("tokenizers>=0.22.0,<=0.23.0 is required").
    "tokenizers==0.22.2",
]
ROUTER_MODEL = "BAAI/bge-small-en-v1.5"


def _router_update(msg: Optional[str], progress: Optional[float] = None,
                   status: Optional[str] = None) -> None:
    """Mutate the shared router-install state (no-op when nothing in flight)."""
    global _router_install
    if _router_install is None:
        return
    if status is not None:
        _router_install["status"] = status
    if progress is not None:
        _router_install["progress"] = round(min(max(progress, 0.0), 100.0), 1)
    if msg is not None:
        clean = (msg.rstrip("\n") if isinstance(msg, str) else str(msg)).strip("\r")
        if clean:
            _router_install["detail"] = clean[-200:]
            log = _router_install.setdefault("log", [])
            log.append(clean)
            if len(log) > _LOG_MAX_LINES:
                del log[: len(log) - _LOG_MAX_LINES]
    _router_install["updated_at"] = datetime.now(timezone.utc).isoformat()


def _router_finish(status: str, detail: str) -> None:
    """Mark the router install terminal and move state into ``_router_last``."""
    global _router_install, _router_last
    _router_update(detail, status=status)
    if status == "done":
        _router_update(None, progress=100.0)
    if _router_install is not None:
        _router_install["finished_at"] = datetime.now(timezone.utc).isoformat()
        _router_last = dict(_router_install)
    _router_install = None


def router_progress() -> Optional[dict]:
    """Return the in-flight router install state (or None when idle)."""
    return _router_install


def router_last() -> Optional[dict]:
    """Return the most recent terminal router install result (or None)."""
    return _router_last


def start_router_install(engine) -> dict:
    """Start (or join) the semantic routing module install and return its state."""
    global _router_install, _router_last

    with _router_lock:
        if _router_install is not None and _router_install.get("status") in ("queued", "running"):
            return _router_install
        _router_last = None
        _router_install = {
            "status": "queued",
            "progress": 0.0,
            "detail": "Waiting to start…",
            "log": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        state = dict(_router_install)
        thread = threading.Thread(target=_run_router_install, args=(engine,), daemon=True)
        thread.start()
        return state


def _run_router_install(engine) -> None:
    """Background install: pip sentence-transformers into router_site, probe
    importability, and pre-download the embedding model weights."""
    try:
        site = router_site()
        models_dir = router_models_dir()
        _router_update(f"Target directory: {site}")
        os.makedirs(site, exist_ok=True)
        os.makedirs(models_dir, exist_ok=True)

        _stream_router(
            [sys.executable, "-m", "pip", "install", "--no-cache-dir",
             "--target", site] + ROUTER_PACKAGES,
            cwd=None, start=2.0, end=70.0,
            status_msg="Installing semantic-routing deps (sentence-transformers)…",
        )

        # Verify the deps actually became importable with the target dir on
        # PYTHONPATH (fresh subprocess probe).
        from .memory import router_available
        if not router_available(site):
            raise SetupError(
                "Semantic-routing deps install did not take effect. The deps "
                f"are at {site} — check the install log."
            )

        # Pre-download the embedding model into the persistent cache dir so the
        # first classification doesn't hit the Hub at runtime.
        try:
            _stream_router(
                [sys.executable, "-c",
                 "import os,sys;"
                 "os.environ.setdefault('LCP_MODULES_DIR', "
                 f"{modules_dir()!r});"
                 "from src.api.memory.embeddings import EmbeddingModel;"
                 f"m=EmbeddingModel({ROUTER_MODEL!r}, cache_dir={models_dir!r});"
                 "m.embed(['warmup'])",
                 ],
                cwd=None, start=70.0, end=100.0,
                status_msg="Pre-downloading embedding model…",
            )
        except subprocess.CalledProcessError as exc:
            _router_update(f"Model pre-download skipped ({exc}) — will download on first use.")
            _router_update(None, progress=100.0)

        set_state(engine, "module:router", "done")
        _router_finish("done", "Semantic routing module installed.")
    except subprocess.CalledProcessError as exc:
        _router_finish("failed", _tail_router_detail(f"Install failed: {exc}"))
    except FileNotFoundError as exc:
        _router_finish("failed", f"Missing tool: {exc}")
    except Exception as exc:  # noqa: BLE001 — background thread must not die
        _router_finish("failed", _tail_router_detail(f"Install failed: {exc}"))


def _stream_router(cmd: list[str], cwd: Optional[str], start: float, end: float,
                   status_msg: str) -> None:
    """Like ``_stream`` but updates the router-install log."""
    _router_update(status_msg, progress=start, status="running")
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, errors="replace",
    )
    seen = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        seen += 1
        _router_update(line)
        if seen % 3 == 0:
            frac = min(0.9, seen / 90.0)
            _router_update(None, progress=start + (end - start) * frac)
    rc = proc.wait()
    _router_update(None, progress=end)
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


def _tail_router_detail(fallback: str, lines: int = 6) -> str:
    """Return *fallback* plus real error lines from the router install log."""
    global _router_install
    if _router_install is None:
        return fallback
    log = _router_install.get("log") or []
    if not log:
        return fallback
    error_markers = (
        "traceback", "error", "fatal", "failed", "exception", "conflict",
        "cannot", "could not", "no matching", "not importable", "missing",
    )
    error_lines = [
        ln for ln in log
        if any(m in ln.lower() for m in error_markers)
        and "pip.pypa.io" not in ln.lower()
    ]
    picked = error_lines[-lines:] if error_lines else log[-lines:]
    tail = "\n".join(picked).strip()
    if not tail:
        tail = "\n".join(log[-lines:]).strip()
    if not tail:
        return fallback
    return f"{fallback}\n{tail[-800:]}"


def remove_router(engine) -> dict:
    """Remove the semantic routing module and clear setup state.

    Lean image: deletes the --target deps dir + model cache. Baked image
    (deps in the image site-packages): uninstalls the baked deps from the
    running container so the module becomes genuinely unavailable — a rebuild
    with WITH_ROUTER=1 restores them. Blocked while MEMORY is also baked,
    because both share sentence-transformers/torch.
    """
    from .memory import memory_available, router_available

    removed: list[str] = []
    for path in (router_site(), router_models_dir()):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path)

    if router_available(None):
        # Baked into the image site-packages — the --target removal above was
        # a no-op, so uninstall the baked deps from the running container.
        if memory_available(None):
            raise SetupError(
                "Cannot remove Semantic routing: the Memory module is also "
                "baked into the image and shares sentence-transformers/torch. "
                "Rebuild the image with WITH_ROUTER=0 and WITH_MEMORY=0 so "
                "both modules are runtime-managed, then remove them from the "
                "Setup page."
            )
        removed += _uninstall_baked_packages(BAKED_ROUTER_PACKAGES)

    set_state(engine, "module:router", "removed")
    from .task_classifier import invalidate_semantic_classifier
    invalidate_semantic_classifier()
    logger.info("setup_router_removed", removed=removed)
    return {"removed": True, "module": "router", "paths": removed}



