"""DB-backed gateway configuration.

The whole gateway config lives in the ``settings`` table as JSON blobs under
``gateway_config:<section>`` (server, profiles, providers, pricing,
circuit_breaker, retry, database, dynamic_routing, model_limits, plugins).
``gateway.yaml`` and the YAML hot-reload are obsolete: a Python ``SEED_CONFIG``
dict seeds a fresh DB on first boot, and edits via the UI (which mutate
``config.raw`` then call ``config.save()``) are written straight to the DB, so
they persist across restarts.
"""

import os
import re
from typing import Any, Optional

from .exceptions import ConfigError
from .logging_config import get_logger

logger = get_logger("lcp.config")


# ─────────────────────────────────────────────────────────────────────────────
# Default seed — used ONLY to initialise a fresh DB (first boot) or when a
# section is missing. Once a section row exists in the DB it is the source of
# truth; edits to this dict do not affect a running gateway.
# ─────────────────────────────────────────────────────────────────────────────
SEED_CONFIG: dict[str, Any] = {
    "server": {
        "port": 8734,
        "default_profile": "l2",
    },
    "dynamic_routing": {
        "enabled": False,
        "cost_bias": 0.15,
    },
    "profiles": {
        "l2": {
            "forbidden_tools": ["write_file", "patch", "cronjob"],
            "chain": [
                {"provider": "opencode", "model": "deepseek-v4-pro",
                 "base_url": "https://opencode.ai/inference/openai/v1"},
                {"provider": "deepseek", "model": "deepseek-v4-pro",
                 "base_url": "https://api.deepseek.com/v1"},
            ],
            "auth_required": False,
        },
        "l1": {
            "forbidden_tools": ["write_file", "patch", "terminal", "execute_code",
                                "cronjob", "process", "delegate_task", "memory",
                                "send_message", "vision_analyze"],
            "chain": [
                {"provider": "opencode", "model": "deepseek-v4-flash",
                 "base_url": "https://opencode.ai/inference/openai/v1"},
                {"provider": "deepseek", "model": "deepseek-v4-flash",
                 "base_url": "https://api.deepseek.com/v1"},
            ],
            "auth_required": False,
        },
        "career": {
            "forbidden_tools": ["write_file", "patch", "terminal", "execute_code",
                                "cronjob", "process", "delegate_task", "memory",
                                "vision_analyze", "read_file", "search_files",
                                "skill_manage", "todo"],
            "chain": [
                {"provider": "deepseek", "model": "deepseek-v4-flash",
                 "base_url": "https://api.deepseek.com/v1"},
            ],
            "auth_required": False,
        },
        "cron": {
            "chain": [
                {"provider": "deepseek", "model": "deepseek-v4-flash",
                 "base_url": "https://api.deepseek.com/v1"},
            ],
            "forbidden_tools": None,
            "auth_required": False,
        },
        "coder": {
            "chain": [
                {"provider": "opencode", "model": "deepseek-v4-pro",
                 "base_url": "https://opencode.ai/inference/openai/v1"},
                {"provider": "deepseek", "model": "deepseek-v4-flash",
                 "base_url": "https://api.deepseek.com/v1"},
            ],
            "forbidden_tools": [],
            "auth_required": True,
        },
    },
    "providers": {
        "opencode": {
            "api_key_env": "OPENCODE_API_KEY",
            "api_base": "https://opencode.ai/inference/openai/v1",
            "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
            # OpenCode reports cache hits in the OpenAI-compatible nested block
            # (usage.prompt_tokens_details.cached_tokens). Without this the
            # hit count read as 0 and every prompt token was priced at the
            # cache-miss rate (~10x overstatement).
            "cache": {
                "strategy": "prefix",
                "savings": "cost",
                "hit_field": "prompt_tokens_details.cached_tokens",
            },
        },
        "deepseek": {
            "api_key_env": "DEEPSEEK_API_KEY",
            "api_base": "https://api.deepseek.com/v1",
            "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
            "cache": {
                "strategy": "prefix",
                "savings": "cost",
                "hit_field": "prompt_cache_hit_tokens",
            },
        },
        "commandcode": {
            "api_base": "https://api.commandcode.ai/provider/v1",
            "models": ["deepseek-v4-pro", "deepseek-v4-flash", "claude-sonnet-5",
                       "gpt-5.6-luna", "kimi-k3", "minimax-m3", "qwen3.8-max"],
            # Same nested field as OpenCode: a repeated ~2k-token prefix reports
            # cached_tokens, so the hit/miss split is available after all.
            "cache": {
                "strategy": "prefix",
                "savings": "cost",
                "hit_field": "prompt_tokens_details.cached_tokens",
            },
        },
    },
    "pricing": [
        {"provider": "deepseek", "model": "deepseek-v4-pro",
         "cache_hit": 0.003625, "cache_miss": 0.435, "output": 0.87},
        # DeepSeek-V4.1-Flash. `deepseek-flash` is the current API name;
        # `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` are legacy names
        # DeepSeek still accepts and serves with the same model at the same
        # price; `deepseek-v4.1-flash` is this gateway's benchmark key. One
        # model, four spellings, one set of rates — see
        # https://api-docs.deepseek.com/quick_start/pricing (off-peak,
        # verified 2026-10-05; peak is 2x).
        {"provider": "deepseek", "model": "deepseek-flash",
         "cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
        {"provider": "deepseek", "model": "deepseek-v4-flash",
         "cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
        {"provider": "deepseek", "model": "deepseek-v4.1-flash",
         "cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
        {"provider": "opencode", "model": "deepseek-v4-pro",
         "cache_hit": 0.003625, "cache_miss": 0.435, "output": 0.87},
        {"provider": "opencode", "model": "deepseek-flash",
         "cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
        {"provider": "opencode", "model": "deepseek-v4-flash",
         "cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
        {"provider": "opencode", "model": "deepseek-v4.1-flash",
         "cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
        # Command Code bills the catalogue ID (with the vendor prefix).
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-pro",
         "cache_hit": 0.003625, "cache_miss": 0.435, "output": 0.87},
        {"provider": "commandcode", "model": "deepseek/deepseek-v4.1-flash",
         "cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
        {"provider": "commandcode", "model": "deepseek/deepseek-v4-flash",
         "cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
        # Self-hosted DGX Spark (local-zgx) — $0 marginal cost. Without an entry
        # here, a *successful* upstream response was discarded with
        # ConfigError("No pricing found") -> HTTP 500 LCP-4001 whenever the
        # provider had no pricing plugin (l1 / coder). See fix-lcp-vision-support.
        {"provider": "local-zgx", "model": "qwen3.8-flash-next",
         "cache_hit": 0.0, "cache_miss": 0.0, "output": 0.0},
    ],
    "circuit_breaker": {
        "failures_degraded": 3,
        "failures_dead": 6,
        "degraded_cooldown_seconds": 30,
        "dead_cooldown_seconds": 120,
    },
    "retry": {
        "max_attempts": 3,
        "backoff_base": 0.5,
        "backoff_multiplier": 2,
        "max_backoff": 10,
        "jitter": True,
    },
    "model_limits": {
        "deepseek-v4-pro": {
            "context_window": 1000000,
            "max_output_tokens": 384000,
            "supports_vision": False,
            "supports_thinking": True,
            "description": "DeepSeek V4 Pro — flagship MoE for coding, reasoning, and agentic work",
        },
        "deepseek-v4-flash": {
            "context_window": 1000000,
            "max_output_tokens": 384000,
            "supports_vision": False,
            "supports_thinking": True,
            "description": "DeepSeek V4 Flash — fast lane for economical reasoning and long-context work",
        },
        # opencode zen/go catalog IDs (text-only lanes — images must route past them)
        "glm-5.3-flash": {
            "context_window": 128000,
            "max_output_tokens": 16384,
            "supports_vision": False,
            "supports_thinking": False,
            "description": "GLM 5.3 Flash — opencode zen/go fast lane",
        },
        # Command Code catalog IDs. Both verified image-capable 2026-09-20 with a
        # real content image (shapes + text "ZEBRA 7291" transcribed correctly).
        "z-ai/glm-5.3-flash": {
            "context_window": 128000,
            "max_output_tokens": 16384,
            "supports_vision": True,
            "supports_thinking": False,
            "description": "GLM 5.3 Flash (Command Code) — vision-capable fast lane",
        },
        "deepseek/deepseek-v4-flash": {
            "context_window": 128000,
            "max_output_tokens": 16384,
            "supports_vision": True,
            "supports_thinking": False,
            "description": "DeepSeek V4 Flash (Command Code) — vision-capable",
        },
        "ox-alpha-free": {
            "context_window": 262144,
            "max_output_tokens": 16384,
            "supports_vision": False,
            "supports_thinking": False,
            "description": "Qwen3.8-27b Q4_K_M — local llama.cpp (RVN multilingual MTP, n_ctx 262144)",
        },
        "qwen3.8-27b-q4_k_m-heretic": {
            "context_window": 262144,
            "max_output_tokens": 16384,
            "supports_vision": False,
            "supports_thinking": False,
            "description": "Qwen3.8-27b Q4_K_M — local llama.cpp (RVN multilingual MTP, n_ctx 262144)",
        },
        "qwen3.8-flash-next": {
            "context_window": 262144,
            "max_output_tokens": 16384,
            "supports_vision": True,
            "supports_thinking": False,
            "description": "Qwen3.8-Flash-Next — local vLLM (DGX Spark 128GB, SEQS=16), vision-capable",
        },
    },
    "database": {
        "path": "/app/data/costs.db",
        "wal_mode": True,
    },
    "plugins": {
        "memory": {
            "enabled": True,
            "auto_recall": False,   # opt-in: auto-inject recalled facts into requests
            "top_k": 3,
            "min_score": 0.0,
            "embedding": {"model": "BAAI/bge-small-en-v1.5", "dim": 384, "device": "cpu"},
        },
        # SEMANTIC ROUTING module — the sole task classifier (no keyword
        # fallback). Deps + model weights are baked into the image by the
        # Docker build (WITH_ROUTER=1); this block just pins the model and
        # the confidence gate.
        "router": {
            "enabled": True,
            "min_score": 0.35,
            "embedding": {"model": "BAAI/bge-small-en-v1.5", "dim": 384, "device": "cpu"},
        },
    },
}

# Sections stored in the DB. ``plugins`` is optional (memory module tolerates
# its absence), the rest are required for a valid config.
REQUIRED_SECTIONS = ("server", "profiles", "providers", "pricing",
                     "circuit_breaker", "database")
ALL_SECTIONS = ("server", "profiles", "providers", "pricing",
                "circuit_breaker", "retry", "database",
                "dynamic_routing", "model_limits", "plugins")


def _env_db_path() -> str:
    """Resolve the DB path from env (COST_DB) or the seed default."""
    return os.environ.get("COST_DB", SEED_CONFIG["database"]["path"])


def _env_port() -> int:
    """Resolve the listen port from env (LISTEN_PORT) or the seed default."""
    return int(os.environ.get("LISTEN_PORT", str(SEED_CONFIG["server"]["port"])))


# `intents` are short human labels ("code planning", "architecture planning").
INTENT_MAX_LEN = 64
INTENTS_MAX = 32

# `routing` is the per-profile choice between walking the chain in order and
# letting the dynamic router score it. Static is the pre-existing behaviour.
ROUTING_MODES = ("", "static", "dynamic")

# Matches the API's cap on the card description.
PROFILE_DESC_MAX = 120


def validate_profile_fields(name: str, prof: dict) -> dict:
    """Validate a profile's optional fields; return the normalised copy.

    One contract, used by both the config loader and the profile write API, so a
    value that can be stored is always a value that can be loaded. Every field is
    optional — absent means "not declared", which is different from empty.
    """
    out = dict(prof)

    agent = prof.get("agent_profile", "")
    # Whitespace is noise, not a value: `"   "` means "not declared", the same as ""
    # or absent. It is stripped before the emptiness test so the three agree.
    if isinstance(agent, str):
        agent = agent.strip()
    if agent is not None and agent != "":
        if not isinstance(agent, str):
            raise ConfigError(
                f"Profile '{name}': 'agent_profile' must be a Hermes profile name"
            )
        # Delegated deliberately: this field names a directory under the profiles
        # root, so it is held to exactly the shape a profile name is held to
        # (profile_data.validate_name — no separators, no "." or "..", ≤64 chars).
        # A second regex here would be a second contract, free to drift from that one.
        from .profile_data import BadProfileName, validate_name
        try:
            out["agent_profile"] = validate_name(agent)
        except BadProfileName as e:
            raise ConfigError(
                f"Profile '{name}': 'agent_profile' must be a Hermes profile name "
                f"({e})"
            ) from e
    else:
        out["agent_profile"] = ""

    # Absent is its own state, and it is not "static": a profile that declares nothing
    # is routed by the gateway's own switch, which is what happens today. Making the
    # default "static" would have the field claim a decision nobody made — and put a
    # "static routing" badge on a profile the router is in fact scoring.
    routing = prof.get("routing", "") or ""
    if isinstance(routing, str):
        routing = routing.strip().lower()
    if routing not in ROUTING_MODES:
        raise ConfigError(
            f"Profile '{name}': 'routing' must be one of "
            f"{[m for m in ROUTING_MODES if m] or ['static', 'dynamic']}"
            " (or absent to inherit the gateway setting)"
        )
    out["routing"] = routing

    intents = prof.get("intents", [])
    if intents is None:
        intents = []
    if not isinstance(intents, list) or len(intents) > INTENTS_MAX:
        raise ConfigError(
            f"Profile '{name}': 'intents' must be a list of at most {INTENTS_MAX} labels"
        )
    cleaned = []
    for it in intents:
        if not isinstance(it, str) or not it.strip():
            raise ConfigError(f"Profile '{name}': every intent must be a non-empty string")
        it = it.strip()
        if len(it) > INTENT_MAX_LEN:
            raise ConfigError(
                f"Profile '{name}': intent '{it[:20]}…' exceeds {INTENT_MAX_LEN} chars"
            )
        if it not in cleaned:
            cleaned.append(it)
    out["intents"] = cleaned

    # L2's margin gate. When the classifier's top two intents sit closer together
    # than this, the decision is a coin flip, so the router does not reorder at all
    # and the static chain head stands — the plan's "delete the decisions that are
    # coin flips". 0.0 is what an absent field means and it keeps today's behaviour
    # exactly: every winner reorders, byte for byte.
    gate = prof.get("intent_margin_gate", 0.0)
    if gate is None:
        gate = 0.0
    # bool is an int subclass, so `isinstance(True, int)` is True — refuse it
    # explicitly, or `True` would silently mean a gate of 1.0 (never reorder).
    if isinstance(gate, bool) or not isinstance(gate, (int, float)):
        raise ConfigError(
            f"Profile '{name}': 'intent_margin_gate' must be a number between 0 and 1"
        )
    gate = float(gate)
    if gate != gate or not 0.0 <= gate <= 1.0:  # gate != gate catches NaN
        raise ConfigError(
            f"Profile '{name}': 'intent_margin_gate' must be between 0 and 1 "
            f"(0 disables the gate)"
        )
    out["intent_margin_gate"] = gate

    desc = prof.get("description", "")
    if desc is None:
        desc = ""
    if not isinstance(desc, str):
        raise ConfigError(f"Profile '{name}': 'description' must be a string")
    out["description"] = desc.strip()[:PROFILE_DESC_MAX]

    return out


def _validate(section: str, data: Any) -> None:
    """Validate a loaded section; raise ConfigError on structural problems."""
    if section == "server":
        if not isinstance(data, dict) or "port" not in data:
            raise ConfigError("Missing 'server.port'")
        if "default_profile" not in data:
            raise ConfigError("Missing 'server.default_profile'")
    elif section == "profiles":
        if not isinstance(data, dict):
            raise ConfigError("'profiles' must be a dict")
        for name, prof in data.items():
            if not isinstance(prof, dict) or "chain" not in prof:
                raise ConfigError(f"Profile '{name}' missing 'chain'")
            if not prof["chain"]:
                raise ConfigError(f"Profile '{name}' has empty 'chain'")
            # agent_profile / routing / intents / description — one contract,
            # shared with the profile write API.
            validate_profile_fields(name, prof)
    elif section == "pricing":
        if not isinstance(data, list):
            raise ConfigError("'pricing' must be a list")
    elif section in ("providers", "circuit_breaker", "database"):
        if not isinstance(data, dict):
            raise ConfigError(f"'{section}' must be a dict")
    # dynamic_routing / retry / model_limits / plugins are optional; handled by
    # the accessors.


class Config:
    """DB-backed gateway configuration.

    Hydrated from the ``settings`` table (``gateway_config:<section>`` rows)
    at init, seeding any missing section from ``SEED_CONFIG``. ``raw`` returns
    the live in-memory dict (mutated by the CRUD endpoints); ``save()`` writes
    every section back to the DB. There is no YAML file and no hot-reload.
    """

    def __init__(self, store: Optional[Any] = None, seed: Optional[dict] = None):
        self._store = store
        self._seed = seed or SEED_CONFIG
        self._data: dict[str, Any] = {}
        self._hydrate()

    def _hydrate(self) -> None:
        """Load every section into ``_data``: DB row if present, else seed."""
        for section in ALL_SECTIONS:
            stored = None
            if self._store is not None:
                try:
                    stored = self._store.get_config_section(section, None)
                except Exception:  # noqa: BLE001 — never break config reads
                    stored = None
            if isinstance(stored, (dict, list)) and stored:
                self._data[section] = stored
            elif section in self._seed and self._seed[section] is not None:
                import copy
                self._data[section] = copy.deepcopy(self._seed[section])
            else:
                self._data[section] = {}
        # Validate required sections; fall back to seed on failure so boot and
        # read paths never hard-fail from a corrupt DB section.
        for section in REQUIRED_SECTIONS:
            try:
                _validate(section, self._data.get(section))
            except ConfigError as exc:
                logger.warning("config_section_invalid", section=section, error=str(exc))
                if section in self._seed:
                    import copy
                    self._data[section] = copy.deepcopy(self._seed[section])
        # Env overrides for the two bootstrap-critical values — only when the
        # env var is actually set (so a DB value is preserved otherwise).
        try:
            if os.environ.get("LISTEN_PORT"):
                self._data["server"]["port"] = _env_port()
        except Exception:  # noqa: BLE001
            pass
        try:
            if os.environ.get("COST_DB"):
                self._data["database"]["path"] = _env_db_path()
        except Exception:  # noqa: BLE001
            pass
        logger.info("config_loaded", sections=sorted(self._data.keys()))

    # ── Accessors ──────────────────────────────────────────────────────────

    @property
    def server(self) -> dict:
        return self._data["server"]

    @property
    def profiles(self) -> dict:
        return self._data["profiles"]

    @property
    def providers(self) -> dict:
        return self._data["providers"]

    @property
    def pricing(self) -> list:
        return self._data["pricing"]

    @property
    def circuit_breaker(self) -> dict:
        return self._data.get("circuit_breaker", {})

    @property
    def retry(self) -> dict:
        return self._data.get("retry", {
            "max_attempts": 3,
            "backoff_base": 0.5,
            "backoff_multiplier": 2,
            "max_backoff": 10,
            "jitter": True,
        })

    @property
    def database(self) -> dict:
        return self._data["database"]

    @property
    def dynamic_routing(self) -> dict:
        return self._data.get("dynamic_routing", {
            "enabled": False,
            "cost_bias": 0.15,
        })

    @property
    def model_limits(self) -> dict:
        return self._data.get("model_limits", {})

    @property
    def plugins(self) -> dict:
        """Plugin config blocks, e.g. ``plugins.memory``.

        Absent/partial config falls back to the memory plugin's defaults.
        """
        return self._data.get("plugins", {})

    # ── Model identity (registry-aware lookups) ───────────────────────────
    #
    # One model reaches the gateway under several spellings: the provider's
    # own ID (``deepseek/deepseek-v4.1-flash``), the bare name another
    # provider uses for the same model (``deepseek-v4.1-flash``), the
    # gateway's logical name (``deepseek-flash``) and the benchmark key
    # (``deepseek-v4.1-flash``). The ``model_registry`` table is what ties
    # those together, so EVERY per-model lookup — pricing, limits — resolves
    # through it rather than matching the raw provider-side string. One row
    # then describes one model, however many names it has.

    def _registry_db_path(self) -> str:
        """The DB the model registry lives in (same DB as everything else)."""
        try:
            return (self._data.get("database") or {}).get("path") or "data/costs.db"
        except Exception:  # noqa: BLE001 — duck-typed configs
            return "data/costs.db"

    def _canonical_names(self, model: str) -> list:
        """Registry-resolved names for *model*, most specific first.

        Returns ``[]`` when the registry is unavailable or the name is
        unknown, so callers degrade to a plain exact-match lookup.
        """
        names: list = []
        try:
            from .router import logical_model_name, benchmark_model_name
        except Exception:  # noqa: BLE001 — registry is optional
            return names
        db_path = self._registry_db_path()
        try:
            logical = logical_model_name(model, db_path)
        except Exception:  # noqa: BLE001
            return names
        if not logical:
            return names
        names.append(logical)
        try:
            benchmark = benchmark_model_name(logical, db_path)
        except Exception:  # noqa: BLE001
            benchmark = None
        if benchmark and benchmark not in names:
            names.append(benchmark)
        return names

    @staticmethod
    def _provider_matches(row_provider: Any, provider: str) -> bool:
        """True when a pricing row's provider applies to *provider*.

        ``"*"``/missing/empty is a WILDCARD, so a single row can price a
        canonical model however many providers serve it.
        """
        if row_provider in (None, "", "*"):
            return True
        return str(row_provider) == str(provider)

    def _find_pricing_row(self, provider: str, model: str,
                          provider_matcher=None) -> Optional[dict]:
        """Return the first pricing row for *model*, or ``None``."""
        target = (model or "").strip()
        if not target:
            return None
        target_lower = target.lower()
        matcher = provider_matcher or (lambda rp: self._provider_matches(rp, provider))
        for row in self.pricing:
            row_model = str(row.get("model") or "")
            if row_model != target and row_model.lower() != target_lower:
                continue
            if matcher(row.get("provider")):
                return row
        return None

    def resolve_pricing(self, provider: str, model: str) -> Optional[dict]:
        """Pricing for a ``(provider, model)`` pair, or ``None``. Never raises.

        Resolution order — the first hit wins:

        1. ``(provider, model)`` — the exact pair, plus any wildcard-provider
           row for the same model string;
        2. ``(provider, <canonical>)`` — the model's logical and benchmark
           names, again with wildcard-provider rows;
        3. ``(<any provider>, <canonical>)`` — cross-provider grouping: a
           price declared once for the canonical model serves every provider
           that serves it. This is the case the operator asked for — one
           model, different provider names, one price.

        Step 3 only runs when steps 1-2 miss, so a provider-specific rate
        always wins over the shared one.

        **A miss is not an error.** A model the gateway cannot price is a
        reporting gap, not a request failure: callers record the request at
        $0 and log ``pricing_unresolved`` instead of discarding a completion
        the upstream already produced. Use :meth:`get_pricing` where an
        exception is genuinely wanted.
        """
        exact = self._find_pricing_row(provider, model)
        if exact is not None:
            return exact

        canonical = self._canonical_names(model)
        for probe in canonical:
            hit = self._find_pricing_row(provider, probe)
            if hit is not None:
                return hit

        # Cross-provider: the canonical name priced under ANY provider.
        for probe in canonical:
            hit = self._find_pricing_row(
                provider, probe, provider_matcher=lambda rp: True)
            if hit is not None:
                return hit

        logger.warning("pricing_unresolved", provider=provider, model=model,
                       canonical=canonical)
        return None

    def get_model_limits(self, model_id: str) -> dict | None:
        """Return context_window, max_output_tokens, description for a model, or None.

        Registry-aware, like :meth:`resolve_pricing`: an entry keyed by the
        model's logical or benchmark name serves every provider-side spelling
        of the same model, so limits are declared once rather than once per
        provider alias.
        """
        for probe in (model_id, *self._canonical_names(model_id)):
            entry = self.model_limits.get(probe)
            if entry:
                return entry
        return None

    def get_profile(self, name: str) -> dict | None:
        return self.profiles.get(name)

    def get_provider_key(self, provider_name: str) -> str:
        """Resolve API key from environment variable."""
        env_var = self.providers[provider_name]["api_key_env"]
        key = os.environ.get(env_var)
        if not key:
            raise ConfigError(f"Environment variable {env_var} not set for provider {provider_name}")
        return key

    def get_pricing(self, provider: str, model: str) -> dict:
        """Registry-aware pricing; raises :class:`ConfigError` when unknown.

        Thin wrapper over :meth:`resolve_pricing` that preserves the strict
        contract for callers that genuinely want to fail (tests, admin
        tooling). Request-path cost accounting uses ``resolve_pricing``.
        """
        pricing = self.resolve_pricing(provider, model)
        if pricing is None:
            raise ConfigError(f"No pricing found for {provider}/{model}")
        return pricing

    def get_provider_cache_config(self, provider_name: str) -> dict:
        """Return cache config for a provider, or empty dict if not configured."""
        p = self.providers.get(provider_name, {})
        return p.get("cache", {"strategy": "none", "savings": "none", "hit_field": None})

    @property
    def raw(self) -> dict:
        return self._data

    def save(self) -> None:
        """Persist every section to the settings DB (source of truth).

        This is the write path for all UI/config edits (the CRUD endpoints
        mutate ``config.raw`` then call ``save()``). Missing sections are
        seeded; present sections are fully replaced by the current ``_data``.
        """
        if self._store is None:
            logger.warning("config_save_no_store", error=True)
            return
        for section in ALL_SECTIONS:
            value = self._data.get(section)
            if value is None:
                continue
            try:
                self._store.set_config_section(section, value)
            except Exception:  # noqa: BLE001 — best-effort per section
                logger.warning("config_section_save_failed", section=section)
        logger.info("config_saved")


# Global config instance — loaded at startup
_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        # Back-compat fallback: build from a store if one is already bound.
        from .cost_cache import get_settings
        _config = Config(store=get_settings())
    return _config


def init_config(store: Optional[Any] = None) -> Config:
    """Create the global Config bound to the settings store (DB-backed)."""
    global _config
    _config = Config(store=store)
    return _config
