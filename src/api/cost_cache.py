"""DB-backed cache for cost-plugin scrape results + admin settings store.

Architecture
------------
* ``SettingsStore`` — key/value admin settings (e.g. the cost-cache TTL) in the
  ``settings`` table. Typed accessors for the TTL.
* ``CostPluginCache`` — stores scraped ``subscription``/``balance`` payloads in
  ``cost_plugin_cache`` with a ``fetched_at`` timestamp. This is pure storage;
  it has no TTL logic of its own.
* ``CacheRefresher`` — a background daemon thread that OWNS all live scraping.
  It periodically checks each entry and, when an entry is missing, older than
  the configured TTL, or explicitly requested, re-scrapes via the plugin
  registry and writes the result. HTTP endpoints only ever read the cache, so a
  frontend request never triggers (or blocks on) a live scrape.

Retry / rate-limit resilience
-----------------------------
Per (provider, kind) the refresher tracks ``consecutive_failures``,
``next_attempt_at`` and ``last_error``. Transient failures (network errors,
``api_error`` payloads, ``None`` results) back off exponentially with jitter
(``base * 2**failures`` capped) to stay Cloudflare-friendly. Auth failures
(``_error: auth_failed``) do NOT hammer — they wait until the entry is stale
again (or a credential change triggers ``request_refresh``). On failure the
previous payload is kept and marked stale (``stale_error``) so the UI never
goes blank.
"""

from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from .logging_config import get_logger
from .models import CostPluginCacheEntry, Setting, get_session
from .cost_plugins.base import CostPlugin

if TYPE_CHECKING:  # pragma: no cover — runtime import only for type hints
    from .component import Component
    from .runtime import Runtime
else:
    from .component import Component
    from .runtime import Runtime

logger = get_logger("lcp.cost_cache")

# kind → plugin method name
KIND_METHODS = {
    "subscription": "fetch_subscription",
    "balance": "fetch_balance",
}
KINDS = tuple(KIND_METHODS.keys())

# Defaults for the refresher (all overridable via constructor for tests)
_DEFAULT_TTL_MINUTES = 30
_DEFAULT_TICK_SECONDS = 30
_DEFAULT_THROTTLE_SECONDS = 10
_DEFAULT_BACKOFF_BASE = 60
_DEFAULT_BACKOFF_CAP = 1800


def plugin_supports(plugin: Optional[CostPlugin], kind: str) -> bool:
    """True if *plugin* overrides the fetch method for *kind* (static check).

    Uses method identity against the base class so we never have to call the
    live fetcher just to know whether a provider supports a kind.
    """
    if plugin is None:
        return False
    method = KIND_METHODS.get(kind)
    if not method:
        return False
    return getattr(type(plugin), method) is not getattr(CostPlugin, method)


# ═══════════════════════════════════════════════════════════════════════════
# SettingsStore
# ═══════════════════════════════════════════════════════════════════════════


class SettingsStore:
    """Admin key/value settings backed by the ``settings`` table.

    The full row set is cached in memory and invalidated on write, so changes
    made via the Settings page apply immediately without a restart.
    """

    def __init__(self, engine: Any = None):
        self._engine = engine
        self._cache: dict[str, str] = {}
        self._loaded = False
        self._lock = threading.Lock()

    def _ensure_loaded(self) -> None:
        if self._loaded or self._engine is None:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                with get_session(self._engine) as session:
                    for row in session.query(Setting).all():
                        self._cache[row.key] = row.value
                self._loaded = True
            except Exception:  # noqa: BLE001 — defer until DB is ready
                logger.warning("settings_load_failed", error=True)

    def get(self, key: str, default: Any = None) -> Any:
        self._ensure_loaded()
        return self._cache.get(key, default)

    def set(self, key: str, value: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        text = str(value)
        with get_session(self._engine) as session:
            row = session.query(Setting).filter(Setting.key == key).first()
            if row is None:
                session.add(Setting(key=key, value=text, updated_at=now))
            else:
                row.value = text
                row.updated_at = now
            session.commit()
        with self._lock:
            self._cache[key] = text
            self._loaded = True

    TTL_KEY = "cost_cache_ttl_minutes"

    def _ttl_from_raw(self, raw, default: int) -> int:
        try:
            return max(1, int(float(raw)))
        except (TypeError, ValueError):
            return default

    def get_ttl_minutes(self, provider: Optional[str] = None,
                        default: int = _DEFAULT_TTL_MINUTES) -> int:
        """Return the refresh TTL in minutes.

        A per-provider override (``cost_cache_ttl_minutes:<provider>``) wins
        when set; otherwise the global default applies.
        """
        if provider:
            raw = self.get(f"{self.TTL_KEY}:{provider}", None)
            if raw is not None:
                return self._ttl_from_raw(raw, default)
        raw = self.get(self.TTL_KEY, default)
        return self._ttl_from_raw(raw, default)

    def set_ttl_minutes(self, minutes: int, provider: Optional[str] = None) -> None:
        """Set the refresh TTL.

        With *provider*, set a per-provider override; otherwise set the global
        default that new/other providers fall back to.
        """
        key = f"{self.TTL_KEY}:{provider}" if provider else self.TTL_KEY
        self.set(key, max(1, int(minutes)))

    # ── Dynamic-routing policy (runtime-editable, like the TTL) ───────────

    ROUTING_POLICY_KEY = "routing_policy"
    ROUTING_MIN_SCORE_KEY = "routing_min_score"
    VALID_POLICIES = ("eager", "cost_first", "explore")

    @staticmethod
    def _profile_key(base: str, profile: Optional[str]) -> str:
        """Return the per-profile settings key (or the global base key)."""
        return f"{base}:{profile}" if profile else base

    def get_routing_policy(self, default: str = "eager",
                           profile: Optional[str] = None) -> str:
        """Return the effective routing policy for a scope.

        A per-profile override (``routing_policy:<profile>``) wins when set;
        otherwise the global value (``routing_policy``). ``profile=None`` reads
        the global value directly.
        """
        raw = self.get(self._profile_key(self.ROUTING_POLICY_KEY, profile), None)
        if raw is None and profile:
            raw = self.get(self.ROUTING_POLICY_KEY, None)
        if raw is None:
            raw = default
        return raw if raw in self.VALID_POLICIES else default

    def set_routing_policy(self, policy: str,
                           profile: Optional[str] = None) -> None:
        if policy not in self.VALID_POLICIES:
            raise ValueError(f"invalid routing policy {policy!r}; expected {self.VALID_POLICIES}")
        self.set(self._profile_key(self.ROUTING_POLICY_KEY, profile), policy)

    def clear_routing_policy(self, profile: Optional[str] = None) -> None:
        """Remove a routing-policy override (per-profile or global)."""
        self._clear_key(self._profile_key(self.ROUTING_POLICY_KEY, profile))

    def get_routing_min_score(self, default: float = 0.0,
                              profile: Optional[str] = None) -> float:
        """Effective min-score for a scope (per-profile override wins)."""
        raw = self.get(self._profile_key(self.ROUTING_MIN_SCORE_KEY, profile), None)
        if raw is None and profile:
            raw = self.get(self.ROUTING_MIN_SCORE_KEY, None)
        if raw is None:
            raw = default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def set_routing_min_score(self, value: float,
                              profile: Optional[str] = None) -> None:
        self.set(self._profile_key(self.ROUTING_MIN_SCORE_KEY, profile), float(value))

    def clear_routing_min_score(self, profile: Optional[str] = None) -> None:
        self._clear_key(self._profile_key(self.ROUTING_MIN_SCORE_KEY, profile))

    # ── Route-by key (CUR-328): what drives the dynamic redirect ──────────

    ROUTE_BY_KEY = "routing_route_by"
    ROUTE_BY_OPTIONS = ("intent", "rules")

    def get_route_by(self, default: str = "intent",
                     profile: Optional[str] = None) -> str:
        """What the dynamic router keys on for this scope.

        ``intent`` (default) classifies each request and reorders by the
        capability matrix; ``rules`` keeps the static chain order and lets
        only explicit rules act (no intent-model call, no scoring). A
        per-profile override wins; otherwise the global value.
        """
        raw = self.get(self._profile_key(self.ROUTE_BY_KEY, profile), None)
        if raw is None and profile:
            raw = self.get(self.ROUTE_BY_KEY, None)
        return raw if raw in self.ROUTE_BY_OPTIONS else default

    def set_route_by(self, value: str, profile: Optional[str] = None) -> None:
        if value not in self.ROUTE_BY_OPTIONS:
            raise ValueError(f"invalid route_by {value!r}; expected {self.ROUTE_BY_OPTIONS}")
        self.set(self._profile_key(self.ROUTE_BY_KEY, profile), value)

    def clear_route_by(self, profile: Optional[str] = None) -> None:
        """Remove a route-by override (per-profile or global)."""
        self._clear_key(self._profile_key(self.ROUTE_BY_KEY, profile))

    # ── Dynamic-routing enable/disable (runtime toggle) ───────────────────

    ROUTING_ENABLED_KEY = "routing_enabled"

    def get_routing_enabled(self, default: Optional[bool] = None,
                            profile: Optional[str] = None) -> Optional[bool]:
        """Return the runtime enable override, or *default* when unset.

        A per-profile override (``routing_enabled:<profile>``) wins when set;
        otherwise the global value. ``None`` (unset) means "no override" — the
        caller falls back to the boot-time config value. Stored as ``"1"``/
        ``"0"``.
        """
        raw = self.get(self._profile_key(self.ROUTING_ENABLED_KEY, profile), None)
        if raw is None and profile:
            raw = self.get(self.ROUTING_ENABLED_KEY, None)
        if raw is None:
            return default
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def set_routing_enabled(self, enabled: bool,
                            profile: Optional[str] = None) -> None:
        self.set(self._profile_key(self.ROUTING_ENABLED_KEY, profile), "1" if enabled else "0")

    def clear_routing_enabled(self, profile: Optional[str] = None) -> None:
        self._clear_key(self._profile_key(self.ROUTING_ENABLED_KEY, profile))

    # ── Routing rules (UI-defined, JSON list in the settings table) ───────

    ROUTING_RULES_KEY = "routing_rules"

    def get_routing_rules(self, default: Optional[list] = None,
                          profile: Optional[str] = None) -> list:
        """Return the effective routing-rules list for a scope.

        A per-profile rules list (``routing_rules:<profile>``) replaces the
        global list when set (the profile owns its routing); otherwise the
        global list applies. Each rule: {task, profile, action, provider?,
        model?, min_score?, policy?, enabled?}.
        """
        raw = self.get(self._profile_key(self.ROUTING_RULES_KEY, profile), None)
        if raw is None and profile:
            raw = self.get(self.ROUTING_RULES_KEY, None)
        if raw is None:
            return list(default) if default else []
        try:
            import json as _json
            parsed = _json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return list(default) if default else []

    def set_routing_rules(self, rules: list,
                          profile: Optional[str] = None) -> None:
        import json as _json
        self.set(self._profile_key(self.ROUTING_RULES_KEY, profile), _json.dumps(rules or []))

    def clear_routing_rules(self, profile: Optional[str] = None) -> None:
        self._clear_key(self._profile_key(self.ROUTING_RULES_KEY, profile))

    # ── Intent decision model (CUR-327: remote classifier, global + per-profile) ──

    INTENT_MODEL_URL_KEY = "intent_model_url"
    INTENT_MODEL_FORMAT_KEY = "intent_model_format"
    INTENT_MODEL_API_KEY_KEY = "intent_model_api_key"
    INTENT_MODEL_MODEL_KEY = "intent_model_model"
    INTENT_MODEL_TIMEOUT_KEY = "intent_model_timeout_ms"
    INTENT_MODEL_MIN_SCORE_KEY = "intent_model_min_score"
    INTENT_MODEL_ENABLED_KEY = "intent_model_enabled"
    INTENT_MODEL_FORMATS = ("lcp-v1", "openai", "jev")

    def _intent_str(self, base: str, default: Optional[str],
                    profile: Optional[str]) -> Optional[str]:
        """Effective string setting: per-profile override, else global, else default."""
        raw = self.get(self._profile_key(base, profile), None)
        if raw is None and profile:
            raw = self.get(base, None)
        if raw is None or not str(raw).strip():
            return default
        return str(raw).strip()

    def get_intent_model_url(self, profile: Optional[str] = None,
                             default: Optional[str] = None) -> Optional[str]:
        return self._intent_str(self.INTENT_MODEL_URL_KEY, default, profile)

    def set_intent_model_url(self, url: str, profile: Optional[str] = None) -> None:
        url = (url or "").strip()
        if not url:
            raise ValueError("intent model URL must not be empty")
        self.set(self._profile_key(self.INTENT_MODEL_URL_KEY, profile), url)

    def get_intent_model_format(self, profile: Optional[str] = None) -> str:
        raw = self._intent_str(self.INTENT_MODEL_FORMAT_KEY, None, profile)
        return raw if raw in self.INTENT_MODEL_FORMATS else "lcp-v1"

    def set_intent_model_format(self, fmt: str, profile: Optional[str] = None) -> None:
        if fmt not in self.INTENT_MODEL_FORMATS:
            raise ValueError(f"invalid intent-model format {fmt!r}; expected {self.INTENT_MODEL_FORMATS}")
        self.set(self._profile_key(self.INTENT_MODEL_FORMAT_KEY, profile), fmt)

    def get_intent_model_api_key(self, profile: Optional[str] = None,
                                 default: Optional[str] = None) -> Optional[str]:
        return self._intent_str(self.INTENT_MODEL_API_KEY_KEY, default, profile)

    def set_intent_model_api_key(self, key: str, profile: Optional[str] = None) -> None:
        self.set(self._profile_key(self.INTENT_MODEL_API_KEY_KEY, profile), (key or "").strip())

    def get_intent_model_model(self, profile: Optional[str] = None,
                               default: Optional[str] = None) -> Optional[str]:
        """Model name for openai-format endpoints (chat.completions)."""
        return self._intent_str(self.INTENT_MODEL_MODEL_KEY, default, profile)

    def set_intent_model_model(self, model: str, profile: Optional[str] = None) -> None:
        # No munging here on purpose (model-identity guard): the value is stored
        # verbatim and `_intent_str` strips on read.
        self.set(self._profile_key(self.INTENT_MODEL_MODEL_KEY, profile), (model or ""))

    def get_intent_model_timeout_ms(self, profile: Optional[str] = None) -> int:
        raw = self._intent_str(self.INTENT_MODEL_TIMEOUT_KEY, None, profile)
        try:
            v = int(float(raw)) if raw is not None else 3000
        except (TypeError, ValueError):
            v = 3000
        return min(60000, max(100, v))

    def set_intent_model_timeout_ms(self, value: int, profile: Optional[str] = None) -> None:
        self.set(self._profile_key(self.INTENT_MODEL_TIMEOUT_KEY, profile),
                 str(min(60000, max(100, int(value)))))

    def get_intent_model_min_score(self, profile: Optional[str] = None) -> float:
        raw = self._intent_str(self.INTENT_MODEL_MIN_SCORE_KEY, None, profile)
        try:
            return min(1.0, max(0.0, float(raw))) if raw is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def set_intent_model_min_score(self, value: float, profile: Optional[str] = None) -> None:
        self.set(self._profile_key(self.INTENT_MODEL_MIN_SCORE_KEY, profile),
                 str(min(1.0, max(0.0, float(value)))))

    def get_intent_model_enabled(self, profile: Optional[str] = None,
                                 default: bool = False) -> bool:
        raw = self._intent_str(self.INTENT_MODEL_ENABLED_KEY, None, profile)
        if raw is None:
            return default
        return raw.lower() in ("1", "true", "yes", "on")

    def set_intent_model_enabled(self, enabled: bool, profile: Optional[str] = None) -> None:
        self.set(self._profile_key(self.INTENT_MODEL_ENABLED_KEY, profile),
                 "1" if enabled else "0")

    def clear_intent_model(self, profile: Optional[str] = None) -> None:
        """Drop every per-profile intent-model override (the profile inherits)."""
        for base in (self.INTENT_MODEL_URL_KEY, self.INTENT_MODEL_FORMAT_KEY,
                     self.INTENT_MODEL_API_KEY_KEY, self.INTENT_MODEL_MODEL_KEY,
                     self.INTENT_MODEL_TIMEOUT_KEY,
                     self.INTENT_MODEL_MIN_SCORE_KEY, self.INTENT_MODEL_ENABLED_KEY):
            self._clear_key(self._profile_key(base, profile))

    def _clear_key(self, key: str) -> None:
        """Delete a settings row if it exists (ignore when absent)."""
        try:
            with get_session(self._engine) as session:
                row = session.query(Setting).filter(Setting.key == key).first()
                if row is not None:
                    session.delete(row)
                    session.commit()
            with self._lock:
                self._cache.pop(key, None)
        except Exception:  # noqa: BLE001 — best-effort clear
            logger.warning("settings_clear_failed", key=key)

    def clear_ttl_minutes(self, provider: str) -> None:
        """Remove a per-provider override so it falls back to the default."""
        key = f"{self.TTL_KEY}:{provider}"
        with get_session(self._engine) as session:
            row = session.query(Setting).filter(Setting.key == key).first()
            if row is not None:
                session.delete(row)
                session.commit()
        with self._lock:
            self._cache.pop(key, None)

    def ttl_overrides(self) -> dict[str, int]:
        """Return {provider: minutes} for every per-provider TTL override."""
        self._ensure_loaded()
        prefix = f"{self.TTL_KEY}:"
        out: dict[str, int] = {}
        for key, raw in self._cache.items():
            if key.startswith(prefix):
                prov = key[len(prefix):]
                if prov:
                    out[prov] = self._ttl_from_raw(raw, _DEFAULT_TTL_MINUTES)
        return out

    # ── DB-backed gateway config sections (gateway_config:<section>) ─────
    # The whole gateway config lives here (JSON blobs per top-level section)
    # so nothing is stored in gateway.yaml anymore. The Config adapter reads
    # these rows and falls back to the Python seed when a row is absent.

    GATEWAY_CONFIG_PREFIX = "gateway_config:"

    @staticmethod
    def _config_section_key(section: str) -> str:
        return f"{SettingsStore.GATEWAY_CONFIG_PREFIX}{section}"

    def get_config_section(self, section: str, default: Any = None) -> Any:
        """Return a gateway-config section (dict OR list), or *default* if absent.

        Stored as a JSON blob under ``gateway_config:<section>``. ``pricing``
        is a list; the rest are dicts.
        """
        raw = self.get(self._config_section_key(section), None)
        if raw is None:
            return default
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, (dict, list)):
                return parsed
            return default
        except (TypeError, ValueError):
            return default

    def set_config_section(self, section: str, value: Any) -> None:
        """Upsert a gateway-config section (JSON blob) into the settings table."""
        self.set(self._config_section_key(section), json.dumps(value or {}))

    def config_sections(self) -> list[str]:
        """Return the names of all DB-backed gateway-config sections present."""
        self._ensure_loaded()
        prefix = self.GATEWAY_CONFIG_PREFIX
        return [key[len(prefix):] for key in self._cache
                if key.startswith(prefix) and len(key) > len(prefix)]


_settings_store: Optional[SettingsStore] = None


def init_settings(engine: Any) -> SettingsStore:
    """Create (or recreate) the global settings store."""
    global _settings_store
    _settings_store = SettingsStore(engine)
    return _settings_store


def get_settings() -> Optional[SettingsStore]:
    from .runtime import get_runtime
    rt = get_runtime()
    if rt is not None:
        try:
            comp = rt.resolve("settings")
        except Exception:  # noqa: BLE001 — inactive/unbound → legacy
            comp = None
        if comp is not None and getattr(comp, "store", None) is not None:
            return comp.store
    return _settings_store


# ═══════════════════════════════════════════════════════════════════════════
# CostPluginCache
# ═══════════════════════════════════════════════════════════════════════════


class CostPluginCache:
    """DB storage for scraped cost-plugin payloads (no TTL logic)."""

    def __init__(self, engine: Any):
        self._engine = engine

    def get(self, provider: str, kind: str) -> Optional[dict]:
        """Return {payload, fetched_at, stale_error} or None if no row."""
        with get_session(self._engine) as session:
            row = session.query(CostPluginCacheEntry).filter_by(
                provider=provider, kind=kind
            ).first()
            if row is None:
                return None
            try:
                payload = json.loads(row.payload_json)
            except json.JSONDecodeError:
                payload = {}
            return {
                "payload": payload,
                "fetched_at": row.fetched_at,
                "stale_error": row.stale_error,
            }

    def set(self, provider: str, kind: str, payload: dict, stale_error: Optional[str] = None) -> None:
        """Upsert a payload. ``stale_error`` is normally None on success."""
        now = datetime.now(timezone.utc).isoformat()
        with get_session(self._engine) as session:
            row = session.query(CostPluginCacheEntry).filter_by(
                provider=provider, kind=kind
            ).first()
            if row is None:
                session.add(CostPluginCacheEntry(
                    provider=provider, kind=kind,
                    payload_json=json.dumps(payload),
                    fetched_at=now, stale_error=stale_error,
                ))
            else:
                row.payload_json = json.dumps(payload)
                row.fetched_at = now
                row.stale_error = stale_error
            session.commit()

    def mark_stale(self, provider: str, kind: str, error: str) -> None:
        """Set stale_error on an existing row without touching payload/time."""
        with get_session(self._engine) as session:
            row = session.query(CostPluginCacheEntry).filter_by(
                provider=provider, kind=kind
            ).first()
            if row is not None:
                row.stale_error = error
                session.commit()

    def invalidate(self, provider: Optional[str] = None, kind: Optional[str] = None) -> None:
        """Delete matching rows (all when provider/kind are None)."""
        with get_session(self._engine) as session:
            q = session.query(CostPluginCacheEntry)
            if provider:
                q = q.filter(CostPluginCacheEntry.provider == provider)
            if kind:
                q = q.filter(CostPluginCacheEntry.kind == kind)
            q.delete(synchronize_session=False)
            session.commit()

    def clear(self) -> None:
        self.invalidate()

    def entries(self) -> list[dict]:
        """Return all rows with a computed age in seconds."""
        now = datetime.now(timezone.utc)
        rows: list[dict] = []
        with get_session(self._engine) as session:
            for r in session.query(CostPluginCacheEntry).order_by(
                CostPluginCacheEntry.provider, CostPluginCacheEntry.kind
            ).all():
                try:
                    fetched = datetime.fromisoformat(r.fetched_at)
                    age = max(0.0, (now - fetched).total_seconds())
                except (ValueError, TypeError):
                    age = 0.0
                rows.append({
                    "provider": r.provider,
                    "kind": r.kind,
                    "fetched_at": r.fetched_at,
                    "age_seconds": round(age, 1),
                    "stale_error": r.stale_error,
                })
        return rows

    def is_stale(self, provider: str, kind: str, ttl_seconds: float) -> bool:
        ent = self.get(provider, kind)
        if ent is None:
            return True
        try:
            fetched = datetime.fromisoformat(ent["fetched_at"])
        except (ValueError, TypeError):
            return True
        return (datetime.now(timezone.utc) - fetched).total_seconds() >= ttl_seconds


_cost_cache: Optional[CostPluginCache] = None


def get_cost_cache() -> Optional[CostPluginCache]:
    from .runtime import get_runtime
    rt = get_runtime()
    if rt is not None:
        try:
            comp = rt.resolve("cost_cache")
        except Exception:  # noqa: BLE001 — inactive/unbound → legacy
            comp = None
        if comp is not None and getattr(comp, "cache", None) is not None:
            return comp.cache
    return _cost_cache


# ═══════════════════════════════════════════════════════════════════════════
# CacheRefresher (background scraper)
# ═══════════════════════════════════════════════════════════════════════════


class CacheRefresher:
    """Background daemon thread that owns all live cost-plugin scraping.

    The HTTP endpoints read the cache only; this thread decides *when* to
    scrape: an entry is refreshed when it is missing, older than the TTL, or
    explicitly requested (``request_refresh``). Failures back off exponentially
    (transient) or wait for TTL expiry (auth) and keep the previous payload
    marked stale.
    """

    def __init__(self, cache: CostPluginCache, settings: Optional[SettingsStore],
                 registry_getter=None, tick_seconds: int = _DEFAULT_TICK_SECONDS,
                 throttle_seconds: float = _DEFAULT_THROTTLE_SECONDS,
                 backoff_base: float = _DEFAULT_BACKOFF_BASE,
                 backoff_cap: float = _DEFAULT_BACKOFF_CAP):
        self._cache = cache
        self._settings = settings or SettingsStore()
        self._registry_getter = registry_getter or _default_registry
        self._tick = tick_seconds
        self._throttle = throttle_seconds
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._pending: set[tuple[str, str]] = set()
        self._refresh_all = False
        self._quiet: set[tuple[str, str]] = set()  # known "no data" — skip until asked
        self._last_attempt: dict[tuple[str, str], float] = {}
        self._failures: dict[tuple[str, str], int] = {}
        self._next_attempt: dict[tuple[str, str], float] = {}
        self._last_error: dict[tuple[str, str], str] = {}

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cost-cache-refresher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ── public control ─────────────────────────────────────────────────────

    def request_refresh(self, provider: Optional[str] = None, kind: Optional[str] = None) -> None:
        """Enqueue an immediate background re-scrape (never blocks the caller).

        Clears backoff for the affected providers so a credential change or a
        manual "Refresh now" is honored on the next pass. Only kinds a plugin
        actually supports are enqueued.
        """
        registry = None
        try:
            registry = self._registry_getter()
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            for prov in self._providers_for(provider):
                plugin = registry.for_provider(prov) if registry is not None else None
                for k in ([kind] if kind else KINDS):
                    if plugin is not None and not plugin_supports(plugin, k):
                        continue
                    key = (prov, k)
                    self._pending.add(key)
                    self._next_attempt.pop(key, None)
                    self._failures.pop(key, None)
                    self._last_error.pop(key, None)
                    self._quiet.discard(key)
        self._wake.set()

    def clear_cache(self) -> None:
        self._cache.clear()
        with self._lock:
            self._pending.clear()
            self._refresh_all = False
            self._quiet.clear()

    def diagnostics(self) -> dict:
        """Per-key retry state for the Settings page."""
        with self._lock:
            out = {}
            for key, fails in self._failures.items():
                out["%s/%s" % key] = {
                    "consecutive_failures": fails,
                    "next_attempt_at": self._next_attempt.get(key),
                    "last_error": self._last_error.get(key),
                }
            return out

    # ── internals ──────────────────────────────────────────────────────────

    def _providers_for(self, provider: Optional[str]) -> list[str]:
        if provider:
            return [provider]
        try:
            return list(self._registry_getter().providers)
        except Exception:  # noqa: BLE001
            return []

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._pass()
            except Exception:  # noqa: BLE001 — never let one pass kill the thread
                logger.exception("cost_cache_refresh_pass_failed")
            self._wake.wait(self._tick)
            self._wake.clear()

    def _pass(self) -> None:
        registry = self._registry_getter()
        now = time.time()

        with self._lock:
            pending = set(self._pending)
            self._pending.clear()
            refresh_all = self._refresh_all
            self._refresh_all = False

        for prov in registry.providers:
            plugin = registry.for_provider(prov)
            if plugin is None:
                continue
            # Per-provider TTL: each provider has its own refresh interval,
            # falling back to the global default when no override is set.
            ttl = self._settings.get_ttl_minutes(provider=prov) * 60
            for kind in KINDS:
                if not plugin_supports(plugin, kind):
                    continue
                key = (prov, kind)
                forced = refresh_all or key in pending
                if not forced and key in self._quiet:
                    continue  # known "no data" — skip until explicitly asked
                if not forced and not self._cache.is_stale(prov, kind, ttl):
                    continue
                # throttle: never scrape the same provider more often than this
                if now - self._last_attempt.get(key, 0.0) < self._throttle:
                    with self._lock:
                        self._pending.add(key)  # retry on a later tick
                    continue
                # backoff gate
                with self._lock:
                    gate = self._next_attempt.get(key, 0.0)
                if now < gate:
                    continue
                self._last_attempt[key] = now
                try:
                    self._scrape(key, prov, kind, plugin)
                except Exception:  # noqa: BLE001 — isolate per provider
                    logger.exception("cost_cache_scrape_crashed", provider=prov, kind=kind)
                    self._record_failure(key, "api_error", "internal error", transient=True)

    def _scrape(self, key, prov: str, kind: str, plugin) -> None:
        method = getattr(plugin, KIND_METHODS[kind])
        try:
            payload = method()
        except Exception as exc:  # noqa: BLE001 — classify any scrape failure
            self._record_failure(key, "api_error", str(exc) or "scrape failed", transient=True)
            return
        if payload is None:
            # No data at all. If we previously had data, treat it as a stale
            # signal (keep serving the old payload). If we never had data, the
            # provider just doesn't expose it (e.g. balance for opencode/
            # commandcode) — stay quiet and only re-check when explicitly asked
            # (credential change / Refresh now), so we don't churn on retries.
            if self._cache.get(prov, kind) is not None:
                self._record_failure(key, "api_error", f"{prov} returned no {kind} data", transient=True)
            else:
                with self._lock:
                    self._quiet.add(key)
            return
        if isinstance(payload, dict) and payload.get("_error"):
            error_kind = payload.get("_error")
            detail = payload.get("detail") or error_kind
            # Stable conditions (bad cookie, missing permission, no subscription)
            # do NOT self-heal — wait until the entry is stale again instead of
            # backing off/hammering.  ``permission_denied`` is a key scope the
            # operator must grant in the vendor console; retrying cannot fix it.
            transient = error_kind not in ("auth_failed", "no_subscription",
                                           "permission_denied")
            self._record_failure(key, error_kind, detail, transient=transient)
            return
        # Success — persist and reset retry state.
        self._cache.set(prov, kind, payload)
        with self._lock:
            self._failures.pop(key, None)
            self._next_attempt.pop(key, None)
            self._last_error.pop(key, None)
        logger.info("cost_cache_refreshed", provider=prov, kind=kind)

    def _record_failure(self, key, error_kind: str, detail: str, transient: bool) -> None:
        prov, kind = key
        with self._lock:
            fails = self._failures.get(key, 0) + 1
            self._failures[key] = fails
            self._last_error[key] = detail
            if transient:
                delay = min(self._backoff_base * (2 ** (fails - 1)), self._backoff_cap)
                delay += random.uniform(0, min(delay * 0.2, 30))  # jitter
            else:
                # Auth failure → retry only when this provider's entry is stale again.
                delay = self._settings.get_ttl_minutes(provider=prov) * 60
            self._next_attempt[key] = time.time() + delay
        logger.warning("cost_cache_refresh_failed", provider=prov, kind=kind,
                       error_kind=error_kind, detail=detail, consecutive=fails)

        # Keep serving the last good payload, marked stale; or record the error.
        ent = self._cache.get(prov, kind)
        if ent is None:
            self._cache.set(prov, kind, {"_error": error_kind, "detail": detail}, stale_error=detail)
        else:
            self._cache.mark_stale(prov, kind, detail)


_refresher: Optional[CacheRefresher] = None


def _default_registry():
    from .cost_plugins import get_registry
    return get_registry()


def get_refresher() -> Optional[CacheRefresher]:
    from .runtime import get_runtime
    rt = get_runtime()
    if rt is not None:
        try:
            comp = rt.resolve("refresher")
        except Exception:  # noqa: BLE001 — inactive/unbound → legacy
            comp = None
        if comp is not None and getattr(comp, "refresher", None) is not None:
            return comp.refresher
    return _refresher


def _reset_singletons() -> None:
    """Test helper — clear all module-level singletons."""
    global _settings_store, _cost_cache, _refresher
    if _refresher is not None:
        _refresher.stop()
    _refresher = None
    _settings_store = None
    _cost_cache = None


# ═══════════════════════════════════════════════════════════════════════════
# Component-runtime adapters (Phase C)
# ═══════════════════════════════════════════════════════════════════════════
# When a Runtime is bound, the get_settings()/get_cost_cache()/get_refresher()
# facades delegate to the runtime's components. The refresher's disposer stops
# its background thread on shutdown — the only real init→shutdown pair here.


def bind_runtime(rt: "Runtime") -> None:
    """Bind an active Runtime so the cost-cache facades delegate to it."""
    from .runtime import bind_active_runtime
    bind_active_runtime(rt)


class SettingsComponent(Component):
    """The admin settings store as a runtime component."""

    name = "settings"
    requires = ["engine"]
    provides = ["settings"]

    def __init__(self) -> None:
        super().__init__()
        self.store: Optional[SettingsStore] = None

    @property
    def service(self) -> Optional[SettingsStore]:
        return self.store

    def setup(self, rt: "Runtime") -> Optional[Any]:
        self.store = SettingsStore(rt.resolve("engine"))
        return None


class CostCacheComponent(Component):
    """The cost-plugin payload cache as a runtime component."""

    name = "cost_cache"
    requires = ["engine"]
    provides = ["cost_cache"]

    def __init__(self) -> None:
        super().__init__()
        self.cache: Optional[CostPluginCache] = None

    @property
    def service(self) -> Optional[CostPluginCache]:
        return self.cache

    def setup(self, rt: "Runtime") -> Optional[Any]:
        self.cache = CostPluginCache(rt.resolve("engine"))
        return None


class RefresherComponent(Component):
    """The background scraper as a runtime component.

    ``requires=["cost_cache", "settings"]``. ``setup`` builds the refresher
    (caller still calls ``start()``); the disposer stops the background thread
    on shutdown.
    """

    name = "refresher"
    requires = ["cost_cache", "settings"]
    provides = ["refresher"]

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self._kwargs = kwargs
        self.refresher: Optional[CacheRefresher] = None

    @property
    def service(self) -> Optional[CacheRefresher]:
        return self.refresher

    def setup(self, rt: "Runtime") -> Optional[Any]:
        from .cost_plugins import get_registry
        # Resolve the PROVIDED objects (the cache + store), not the component
        # instances — the refresher calls .is_stale()/.get()/.set() and
        # .get_ttl_minutes() on them at scrape time.
        self.refresher = CacheRefresher(
            rt.resolve("cost_cache").cache,
            rt.resolve("settings").store,
            registry_getter=get_registry,
            **self._kwargs,
        )
        return self.refresher.stop


# ═══════════════════════════════════════════════════════════════════════════
# Endpoint helper — serve from cache only
# ═══════════════════════════════════════════════════════════════════════════


def cached_plugin_payloads(cache: CostPluginCache, kind: str, registry) -> dict:
    """Build the {provider: payload} map for an endpoint from the cache only.

    Unsupported providers (plugin does not override the method) map to None;
    supported-but-not-yet-scraped providers map to ``{"_error":
    "cache_pending"}``. Payloads that are being served while stale get
    ``_stale``/``_stale_error`` injected (the frontend already tolerates
    unknown keys).
    """
    result: dict = {}
    for prov in registry.providers:
        plugin = registry.for_provider(prov)
        ent = cache.get(prov, kind)
        if ent is None:
            result[prov] = None if not plugin_supports(plugin, kind) else {"_error": "cache_pending"}
            continue
        payload = dict(ent["payload"])
        if ent.get("stale_error"):
            payload["_stale"] = True
            payload["_stale_error"] = ent["stale_error"]
        if "fetched_at" not in payload:
            payload["fetched_at"] = ent["fetched_at"]
        result[prov] = payload
    return result
