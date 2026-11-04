"""Abstract base class and registry for cost tracking plugins."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, Any

from ..provider_type import type_of

if TYPE_CHECKING:  # pragma: no cover — runtime import only for type hints
    from ..component import Component
    from ..runtime import Runtime
else:
    # Imported at module load — no cycle: component/runtime import only
    # logging_config, not cost_plugins.
    from ..component import Component
    from ..runtime import Runtime


# ── DeepSeek peak / off-peak billing windows ────────────────────────────────
# Source: https://api-docs.deepseek.com/quick_start/pricing (verified 2026-10-05)
#   "Off-peak rates are half of the peak rates. Peak hours are 01:00-04:00 and
#    06:00-10:00 UTC, Monday through Friday, excluding Chinese public holidays.
#    All other hours are off-peak, including weekends and Chinese public
#    holidays in full."
#
# Every DeepSeek-backed row (deepseek, opencode, commandcode) stores the
# OFF-PEAK triple as its base rates plus a ``peak_*`` triple. Cost is selected
# at billing time from the request's UTC timestamp. Storing the off-peak triple
# alone was the earlier defect: it understated real spend by 2x for every
# peak-window request (measured at ~14% of 70k rows, 2026-10-05).
DEEPSEEK_PEAK_BANDS: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))


def is_deepseek_peak(ts: Optional[datetime] = None) -> bool:
    """True when *ts* falls inside a DeepSeek peak billing window.

    Peak = 01:00-04:00 and 06:00-10:00 UTC, Monday-Friday. Weekends are
    off-peak in full. Upstream also treats Chinese public holidays as off-peak;
    modelling that would need a holiday calendar, so on those days this
    overstates rather than understates — a handful of days a year.

    *ts* defaults to now. Naive timestamps are read as UTC.
    """
    ts = ts if ts is not None else datetime.now(timezone.utc)
    if not isinstance(ts, datetime):        # ignore junk from a bad caller
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    if ts.weekday() >= 5:                   # Sat/Sun — off-peak in full
        return False
    return any(lo <= ts.hour < hi for lo, hi in DEEPSEEK_PEAK_BANDS)


def select_rates(pricing: Optional[dict],
                 ts: Optional[datetime] = None) -> Optional[dict]:
    """Return the effective ``{cache_hit, cache_miss, output}`` rates for *ts*.

    A row without ``peak_*`` fields is peak-agnostic — the local Spark tiers and
    every non-DeepSeek provider — and is returned unchanged, so this is a no-op
    for them. For a peak-capable row the peak triple is used inside a peak
    window and the base (off-peak) triple outside it, so ONE row prices both.
    """
    if not pricing:
        return pricing
    if "peak_cache_miss" not in pricing:
        return pricing
    if not is_deepseek_peak(ts):
        return pricing
    return {
        "cache_hit": pricing.get("peak_cache_hit", pricing.get("cache_hit", 0.0)),
        "cache_miss": pricing.get("peak_cache_miss", pricing.get("cache_miss", 0.0)),
        "output": pricing.get("peak_output", pricing.get("output", 0.0)),
    }


class CostPlugin(ABC):
    """Abstract base for provider-specific cost tracking plugins.

    Each plugin handles one provider (e.g. deepseek, opencode, llamacpp).
    Default implementations return None/[] for operations the plugin
    does not support — override only the methods that apply.
    """

    # ── Identity ───────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Lowercase provider identifier, e.g. 'deepseek', 'opencode'."""
        ...

    # ── Provider binding ───────────────────────────────────────────────────
    # One vendor can be configured more than once — two Command Code accounts are
    # 'commandcode' and 'commandcode2'. ``provider_name`` is the VENDOR identity: it
    # selects the plugin class, the pricing table and the alert-window policy.
    # ``provider`` is the CONFIGURED provider name this instance speaks for: the
    # credential slot it reads and the ``requests.provider`` value it aggregates.
    # The two are equal unless the instance was bound with :meth:`bind_provider`.

    @property
    def provider(self) -> str:
        """Configured provider this instance serves (defaults to the vendor name)."""
        return getattr(self, "_provider", None) or self.provider_name

    #: Settable so a plugin may declare the provider it serves as a plain attribute
    #: (``self.provider = "..."``) as well as through :meth:`bind_provider`.
    @provider.setter
    def provider(self, value: str) -> None:
        self._provider = value

    def bind_provider(self, provider: str) -> "CostPlugin":
        """Serve configured provider *provider* instead of the vendor name."""
        self._provider = provider
        return self

    # ── Model support ──────────────────────────────────────────────────────

    def get_supported_models(self) -> list[str]:
        """Return the list of model names this plugin handles.

        An empty list means 'all models' — the plugin provides global pricing
        or cost calculation covering every model the provider might serve.
        """
        return []

    def discover_models(self, api_base: str) -> list[dict] | None:
        """Query the provider's /models endpoint and return normalized model metadata.

        Args:
            api_base: The provider's API base URL (e.g. https://api.openai.com/v1).

        Returns:
            List of model dicts each with at minimum ``id``, plus optional
            metadata like ``context_length``, ``parameters``, ``quantization``.
            Return ``None`` to use the default generic HTTP-based discovery.
        """
        return None

    def get_api_model(self, model: str) -> str:
        """Translate a gateway model name to the provider's API model ID.

        Most providers accept the same name the gateway uses, so the default is
        a passthrough. Providers whose API uses a different model-ID scheme
        (e.g. Command Code's prefixed catalog IDs) override this.
        """
        return model

    # ── Pricing ────────────────────────────────────────────────────────────

    def get_pricing(self, model: str) -> Optional[dict]:
        """Return per-1M-token pricing for *model*.

        Return format::

            {"cache_hit": float, "cache_miss": float, "output": float}

        Return *None* when the plugin does not have pricing for *model*.
        """
        return None

    # ── Cost calculation ───────────────────────────────────────────────────

    def calculate_cost(self, model: str, usage: dict) -> Optional[float]:
        """Calculate total cost (USD) from a usage dict.

        *usage* typically contains ``prompt_tokens``, ``completion_tokens``,
        ``prompt_cache_hit_tokens``, ``prompt_cache_miss_tokens``.

        Return *None* to let the default pipeline apply generic pricing.
        """
        return None

    # ── Usage history (per-provider API or local storage) ──────────────────

    def fetch_usage(self,
                    start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> list[dict]:
        """Fetch historical usage data from the provider's API or local store.

        Returns a list of dicts, each with::

            date             str   YYYY-MM-DD
            model            str
            provider         str
            prompt_tokens    int
            completion_tokens int
            cache_hit_tokens int
            cache_miss_tokens int
            cost             float
            request_count    int

        Return an empty list when the source is unavailable or unsupported.
        """
        return []

    # ── Balance / credit check ─────────────────────────────────────────────

    def fetch_balance(self) -> Optional[dict]:
        """Fetch current account balance / remaining credits.

        Return format::

            {"balance": float, "currency": str, "total_granted": Optional[float]}

        Return *None* when balance info is unavailable.
        """
        return None

    # ── Rich summary (daily/weekly/monthly usage, balance, limits) ─────────

    def fetch_summary(self) -> Optional[dict]:
        """Return a rich provider summary for the dashboard.

        Each plugin returns a provider-specific shape:

        OpenCode (local DB)::

            {
                "daily":   {"tokens": int, "cost": float, "requests": int},
                "weekly":  {"tokens": int, "cost": float, "requests": int},
                "monthly": {"tokens": int, "cost": float, "requests": int},
            }

        DeepSeek (balance API)::

            {
                "balance": {
                    "available": float, "spent": float,
                    "total_granted": float, "currency": str,
                },
            }

        Return *None* when the summary is unavailable.
        """
        return None

    # ── Quick-add preset (provider config template) ────────────────────────

    @property
    def preset(self) -> Optional[dict]:
        """Recommended quick-add preset for this provider.

        Return format::

            {"api_base": str, "models": list[str]}

        Return *None* when there is no recommended preset.
        """
        return None

    # ── Subscription usage (provider web API, optional) ────────────────────

    def fetch_subscription(self) -> Optional[dict]:
        """Fetch subscription usage snapshot from the provider web API.

        Each plugin returns a provider-specific shape.  OpenCode example::

            {"rolling_pct": 17.0, "weekly_pct": 75.0,
             "rolling_reset_sec": 5944, "weekly_reset_sec": 278201}

        Return *None* when subscription data is unavailable.
        """
        return None

    # ── Credit status (cached payload interpretation) ──────────────────────

    def credit_status(self, subscription: Optional[dict] = None,
                      balance: Optional[dict] = None) -> str:
        """Interpret cached subscription/balance payloads into a credit status.

        The router asks this instead of hardcoding per-provider heuristics —
        each plugin knows its own payload shape. Returns one of:

          - ``"funded"``  — the account has usable credits
          - ``"drained"`` — the account is out of usable credits (a request
            would 400 with "insufficient credits")
          - ``"unknown"`` — no payload data to judge (treated as funded)

        The base returns ``"unknown"``; providers with a credit signal
        override it.
        """
        return "unknown"

    # ── Lifecycle hooks (optional) ─────────────────────────────────────────

    def on_startup(self) -> None:
        """Called once when the plugin is first loaded."""

    def on_shutdown(self) -> None:
        """Called during graceful shutdown (not guaranteed)."""


# ═══════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════

class PluginRegistry:
    """Holds all registered CostPlugin instances and provides lookup helpers.

    Usage::

        registry = PluginRegistry()
        registry.register(DeepSeekPlugin())
        p = registry.for_provider("deepseek")   # -> DeepSeekPlugin instance
    """

    def __init__(self):
        self._plugins: dict[str, CostPlugin] = {}

    # ── Registration ──────────────────────────────────────────────────────

    def register(self, plugin: CostPlugin) -> None:
        """Register a plugin instance, keyed by the CONFIGURED provider it serves."""
        name = plugin.provider
        if name in self._plugins:
            raise ValueError(f"Plugin for provider '{name}' is already registered")
        self._plugins[name] = plugin
        plugin.on_startup()

    def add_bound(self, provider: str, plugin: CostPlugin) -> bool:
        """Register *plugin* re-pointed at configured provider *provider*.

        Returns False (registering nothing) when that name is already taken, so
        wiring is idempotent. Used to give a second account of one vendor its own
        instance — same vendor logic, its own credential slot and its own rows.
        """
        if provider in self._plugins:
            return False
        self.register(plugin.bind_provider(provider))
        return True

    def forget(self, provider: str) -> bool:
        """Drop the instance registered for *provider*; True when one was removed.

        The cost endpoints iterate the registry, so a provider deleted from the
        config would otherwise keep reporting credits (and keep its tab in the
        Usage payload) until the next restart.
        """
        plugin = self._plugins.pop(provider, None)
        if plugin is None:
            return False
        try:
            plugin.on_shutdown()
        except Exception:  # noqa: BLE001 — teardown must never break the caller
            pass
        return True

    # ── Lookup ────────────────────────────────────────────────────────────

    def for_provider(self, provider: str) -> Optional[CostPlugin]:
        """Get the plugin for *provider*, or None."""
        return self._plugins.get(provider)

    @property
    def all(self) -> list[CostPlugin]:
        """Return all registered plugins."""
        return list(self._plugins.values())

    @property
    def providers(self) -> list[str]:
        """Return provider names of all registered plugins."""
        return list(self._plugins.keys())

    @property
    def types(self) -> dict[str, str]:
        """Return ``{registered provider name: vendor type}``.

        The mapping exists for callers that must apply a per-vendor policy to a
        provider whose name is not the vendor's (e.g. alert windows).
        """
        return {name: plugin.provider_name for name, plugin in self._plugins.items()}


    @property
    def presets(self) -> dict[str, dict]:
        """Collect quick-add presets from all plugins that define one.

        Returns {provider_name: {api_base, models}}.
        """
        result: dict[str, dict] = {}
        for name, plugin in self._plugins.items():
            p = plugin.preset
            if p is not None:
                result[name] = p
        return result

    # ── Delegation helpers ────────────────────────────────────────────────

    def get_pricing(self, provider: str, model: str) -> Optional[dict]:
        """Ask the matching plugin for pricing. Falls back to None."""
        p = self.for_provider(provider)
        return p.get_pricing(model) if p else None

    def calculate_cost(self, provider: str, model: str,
                       usage: dict) -> Optional[float]:
        """Ask the matching plugin to calculate cost. Falls back to None."""
        p = self.for_provider(provider)
        return p.calculate_cost(model, usage) if p else None

    def fetch_all_usage(self,
                        start_date: Optional[str] = None,
                        end_date: Optional[str] = None) -> dict[str, list[dict]]:
        """Fetch usage from every plugin that has data.

        Returns {provider_name: [usage_dict, ...]}.
        """
        result: dict[str, list[dict]] = {}
        for name, plugin in self._plugins.items():
            data = plugin.fetch_usage(start_date, end_date)
            if data:
                result[name] = data
        return result

    def fetch_all_balances(self) -> dict[str, Optional[dict]]:
        """Fetch balances from every plugin that supports it.

        Returns {provider_name: balance_dict_or_None}.
        """
        result: dict[str, Optional[dict]] = {}
        for name, plugin in self._plugins.items():
            result[name] = plugin.fetch_balance()
        return result

    def fetch_all_summaries(self) -> dict[str, Optional[dict]]:
        """Fetch rich provider summaries from every plugin.

        Returns {provider_name: summary_dict_or_None}.
        """
        result: dict[str, Optional[dict]] = {}
        for name, plugin in self._plugins.items():
            result[name] = plugin.fetch_summary()
        return result

    def fetch_all_subscriptions(self) -> dict[str, Optional[dict]]:
        """Fetch subscription snapshots from every plugin.

        Returns {provider_name: subscription_dict_or_None}.
        """
        result: dict[str, Optional[dict]] = {}
        for name, plugin in self._plugins.items():
            result[name] = plugin.fetch_subscription()
        return result

# ═══════════════════════════════════════════════════════════════════════════
# Module-level singleton
# ═══════════════════════════════════════════════════════════════════════════

_registry: Optional[PluginRegistry] = None


def get_registry() -> PluginRegistry:
    """Return the active plugin registry.

    When a Runtime is bound AND its ``cost_plugins`` component is active, this
    returns the runtime's registry (engine injected via the component's
    constructor). Otherwise it returns the legacy module-level singleton —
    preserving the boot/tests path until main.py is rewired to the runtime.
    """
    from ..runtime import get_runtime
    rt = get_runtime()
    if rt is not None:
        try:
            comp = rt.resolve("cost_plugins")
        except Exception:  # noqa: BLE001 — inactive/unbound → legacy
            comp = None
        if comp is not None and getattr(comp, "registry", None) is not None:
            return comp.registry
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
    return _registry


# ═══════════════════════════════════════════════════════════════════════════
# Component-runtime adapter (Phase B pilot)
# ═══════════════════════════════════════════════════════════════════════════

# When an active Runtime is bound, the get_registry() facade delegates to the
# runtime's CostPluginsComponent instead of the legacy module singleton. The
# component builds a FRESH registry with engine injected at construction
# (constructor injection — no set_engine probing), and returns a disposer that
# runs each plugin's on_shutdown() (e.g. llamacpp persists its usage cache).


def bind_runtime(rt: "Runtime") -> None:
    """Bind an active Runtime so ``get_registry()`` delegates to it."""
    from ..runtime import bind_active_runtime
    bind_active_runtime(rt)


class CostPluginsComponent(Component):
    """The cost-plugin registry as a runtime component.

    ``requires=["engine"]`` — engine is declared, not probed via
    ``hasattr(plugin, "set_engine")``. ``setup`` constructs the built-in
    plugins with engine injected at construction and returns a disposer that
    calls each plugin's ``on_shutdown`` (in LIFO order via the runtime).
    """

    name = "cost_plugins"
    requires = ["engine"]
    provides = ["cost_plugins", "pricing"]

    def __init__(self, extra_plugins: Optional[list[CostPlugin]] = None):
        super().__init__()
        self._extra_plugins = extra_plugins or []
        self._registry: Optional[PluginRegistry] = None
        self._config: Any = None
        self._factories: dict = {}

    @property
    def registry(self) -> PluginRegistry:
        if self._registry is None:
            raise RuntimeError("cost_plugins component has not been set up")
        return self._registry

    @property
    def service(self) -> PluginRegistry:
        return self.registry

    def _build_factories(self, engine: Any) -> dict:
        """One builder per vendor, for the plugins whose data source is an account.

        A provider entry's ``type`` selects one of these by name, which is what lets a
        second account of the same vendor ('commandcode2', type 'commandcode') get its
        own bound instance — its own credential slot, its own rows — instead of being
        treated as a vendor the cost layer has never heard of.
        """
        # Imported here, not at module level: every one of these imports ``.base``.
        from .deepseek import DeepSeekCostPlugin
        from .opencode import OpenCodeCostPlugin
        from .llamacpp import LlamaCppCostPlugin
        from .commandcode import CommandCodeCostPlugin
        from .anthropic import AnthropicCostPlugin

        return {
            "deepseek": lambda: DeepSeekCostPlugin(),
            "opencode": lambda: OpenCodeCostPlugin(engine=engine),
            "llamacpp": lambda: LlamaCppCostPlugin(),
            "commandcode": lambda: CommandCodeCostPlugin(engine=engine),
            "anthropic": lambda: AnthropicCostPlugin(),
        }

    def _wire_typed_providers(self, registry: PluginRegistry) -> list[str]:
        """Bind an instance for every configured provider of a known vendor type.

        Providers whose type names no known vendor stay unregistered, exactly as
        before this existed. Returns the provider names added.
        """
        added: list[str] = []
        for pname in sorted((getattr(self._config, "providers", None) or {})):
            _t = type_of(self._config, pname)
            # `local` (the hard self-hosted type) and `llamacpp` (the legacy
            # name) share one plugin; `local` maps to the llamacpp key so the
            # two never double-register the same plugin.
            key = "llamacpp" if _t == "local" else _t
            builder = self._factories.get(key)
            if builder is not None and registry.add_bound(pname, builder()):
                added.append(pname)
        return added

    def ensure_provider(self, provider: str) -> bool:
        """Wire a provider created or edited AFTER boot into the live registry.

        The registry is built once at boot, so without this a provider added later —
        or one whose ``type`` was just set — has no plugin at all: it reads no
        credits and "Refresh now" for it does nothing until the process restarts.
        Returns True when an instance was registered.
        """
        if self._registry is None:
            return False
        _t = type_of(self._config, provider)
        key = "llamacpp" if _t == "local" else _t
        builder = self._factories.get(key)
        if builder is None:
            return False
        return self._registry.add_bound(provider, builder())

    def drop_provider(self, provider: str) -> bool:
        """Remove a deleted provider's instance from the live registry."""
        if self._registry is None:
            return False
        return self._registry.forget(provider)

    def setup(self, rt: "Runtime") -> Optional[Any]:
        # NOTE: this list — not the plugins' own module-level
        # `get_registry().register(...)` calls — is what the running server
        # actually uses. `get_registry()` returns the module singleton, which a
        # bound Runtime never consults (`resolve_service("pricing")` returns
        # THIS registry). A plugin registered only at import time therefore
        # works in ad-hoc scripts and is invisible in the UI. Keep both in sync.

        engine = rt.resolve("engine")
        self._factories = self._build_factories(engine)
        registry = PluginRegistry()
        for builder in self._factories.values():
            registry.register(builder())
        for p in self._extra_plugins:
            registry.register(p)

        # Every configured provider whose type is a vendor served by one of the
        # factories gets its OWN instance (its own credential slot, its own
        # ``requests.provider`` rows).
        try:
            self._config = rt.resolve("config")
        except Exception:  # noqa: BLE001 — nothing published → vendor set only
            self._config = None
        self._wire_typed_providers(registry)
        self._registry = registry

        def _dispose() -> None:
            for plugin in registry.all:
                try:
                    plugin.on_shutdown()
                except Exception:  # noqa: BLE001 — teardown must never break
                    pass

        return _dispose
