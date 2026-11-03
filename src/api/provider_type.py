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


def provider_type(name: str, providers: Optional[dict] = None) -> str:
    """Return the vendor type of configured provider *name*.

    *providers* is the ``config.providers`` mapping (``{name: entry}``). An entry with
    no usable ``type`` is its own type, so the answer for an untyped config is the
    provider name — unchanged behaviour, no migration. Anything that is not a mapping
    (a duck-typed or mocked config) also answers with the provider name, so asking the
    question can never be the thing that breaks a caller.
    """
    if not isinstance(providers, dict):
        return name
    entry = providers.get(name) or {}
    declared = entry.get("type") if isinstance(entry, dict) else None
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower()
    return name


def type_of(config: Any, name: str) -> str:
    """As :func:`provider_type`, reading the mapping off a ``Config`` (or any holder)."""
    return provider_type(name, getattr(config, "providers", None) or {})
