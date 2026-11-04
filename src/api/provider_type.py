"""Provider types — one place that answers "which vendor is this provider?".

A configured provider may declare ``type``. It names the vendor whose plugin, pricing
and alert windows serve it, and it defaults to the provider's own name, so a config
written before typed providers resolves exactly as it did before.

Why it exists: nothing about the name ``commandcode2`` says it is a second Command
Code account. Without the type, every layer keyed by the provider name — the cost
plugin registry, the credit/usage cache, the alert-window policy and the Usage page's
panel layout — treats it as a vendor it has never heard of, so the account gets no
credits card and cannot be watched.

Deliberately import-free: ``config`` and the cost plugins both need this, and a
dependency in either direction would be a cycle.
"""

from __future__ import annotations

from typing import Any, Optional


def _db_type_of(name: str, providers: Optional[dict] = None) -> Optional[str]:
    """The DB row's type for *name* when the provider-type store is reachable.

    Lazy + guarded: this file is import-free by design (config and the cost
    plugins both depend on it), so the store is imported only when called and
    any failure degrades to the config-blob / name answers below. The DB row is
    the hard source of truth; a provider with a row is that type.
    """
    try:
        from .provider_type_store import get_provider_type_store
        store = get_provider_type_store(None)
        if store is not None:
            got = store.get(name)
            if got:
                return got
    except Exception:  # noqa: BLE001 — never let type resolution be the breaker
        pass
    return None


def provider_type(name: str, providers: Optional[dict] = None) -> str:
    """Return the vendor type of configured provider *name*.

    Precedence: the hard ``provider_types`` DB row when present, else a
    ``type`` declared in the config entry, else the provider's own name.
    Anything that is not resolvable answers the provider's own name, so asking
    the question can never break a caller and an untyped config behaves exactly
    as before (its own name — unchanged behaviour, no migration).
    """
    declared = _db_type_of(name, providers)
    if declared:
        return declared
    if not isinstance(providers, dict):
        return name
    entry = providers.get(name) or {}
    cfg_type = entry.get("type") if isinstance(entry, dict) else None
    if isinstance(cfg_type, str) and cfg_type.strip():
        return cfg_type.strip().lower()
    return name


def type_of(config: Any, name: str) -> str:
    """As :func:`provider_type`, reading the mapping off a ``Config`` (or any holder)."""
    providers = getattr(config, "providers", None) or {}
    return provider_type(name, providers)
