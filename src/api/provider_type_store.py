"""Provider-type store — the hard provider-type axis, DB-backed.

One row per configured provider in ``provider_types`` (alembic 029) carries its
**type** as a CHECK-constrained column. The DB itself refuses a value outside
``('commandcode','opencode','deepseek','local')``, so a provider's type is a real
type, not a free-text string in the gateway config blob.

Precedence (the single rule every layer reads):

* a row exists (and is a valid type)  → that is the answer, hard/declared;
* otherwise                            → inferred from the provider's name (the
                                        compat shim for entries written before
                                        the table existed).

The store is a runtime component (``provider_type_store``) so it holds the engine
and the DB-backed Config without a module-level singleton, exactly like
``credential_store``. It publishes the DB row to the Config's ``provider_type``
accessor; the write path (provider create/update/delete) calls
``set_type`` / ``drop``.

Backfill: on first use a configured provider with no row is written from its
inferred type, so a provider that was ``commandcode`` by name becomes a hard
``commandcode`` row without a migration. Writing the inferred type on read means
an operator who never edits still gets the hard-axis behaviour (the registry, the
UI and the alert policy all key on the row), and an explicit edit after that only
rewrites the row.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .component import Component
from .models import PROVIDER_TYPES, ProviderType, get_session
from .config import infer_provider_type


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProviderTypeStore:
    """DB-backed provider-type resolution (one row per configured provider)."""

    def __init__(self, engine):
        self._engine = engine

    # ── Resolution ───────────────────────────────────────────────────────

    def get(self, provider: str) -> Optional[str]:
        """Return the DB type for a provider, or None (no row).

        The answer is the row's ``type`` **when it is one of the known types** —
        the CHECK constraint already guarantees this, but an old/corrupt row must
        degrade to inference rather than feed an unknown string into the UI.
        """
        with get_session(self._engine) as session:
            row = session.query(ProviderType).filter(
                ProviderType.provider == provider
            ).first()
        if row is None:
            return None
        return row.type if row.type in PROVIDER_TYPES else None

    def type_for(self, provider: str) -> str:
        """Resolve the type: DB row, else name inference (never raises)."""
        declared = self.get(provider)
        if declared:
            return declared
        return infer_provider_type(provider)

    def backfill(self, provider: str) -> str:
        """Ensure a row exists and return the resolved type.

        Given the seed of inference, write the inferred type as a hard row so the
        provider is on the hard axis from then on. Do not clobber a row that
        already exists.
        """
        existing = self.get(provider)
        if existing:
            return existing
        inferred = infer_provider_type(provider)
        self.set(provider, inferred)
        return inferred

    # ── Writes ───────────────────────────────────────────────────────────

    def set(self, provider: str, ptype: Optional[str]) -> None:
        """Upsert the type for a provider. ``ptype`` None/empty deletes the row.

        Only a known type may be stored — this is where the hard constraint is
        enforced on the write path (the DB CHECK is the second line of defence).
        An empty value is the explicit way to clear a declaration and return the
        provider to name inference.
        """
        ptype = (ptype or "").strip().lower() or None
        if ptype is not None and ptype not in PROVIDER_TYPES:
            raise ValueError(
                f"'type' must be one of {', '.join(PROVIDER_TYPES)}, got {ptype!r}"
            )
        now = now_iso()
        with get_session(self._engine) as session:
            row = session.query(ProviderType).filter(
                ProviderType.provider == provider
            ).first()
            if ptype is None:
                if row is not None:
                    session.delete(row)
                    session.commit()
                return
            if row is None:
                session.add(ProviderType(
                    provider=provider,
                    type=ptype,
                    created_at=now,
                    updated_at=now,
                ))
            else:
                row.type = ptype
                row.updated_at = now
            session.commit()

    def drop(self, provider: str) -> None:
        """Remove a provider's row (provider deleted / renamed)."""
        self.set(provider, None)

    def all(self) -> dict[str, str]:
        """``{provider: type}`` for every row."""
        with get_session(self._engine) as session:
            rows = session.query(ProviderType).all()
        return {r.provider: r.type for r in rows if r.type in PROVIDER_TYPES}

    def backfill_config(self, config) -> int:
        """Ensure a row exists for every configured provider.

        Given a provider with no row, seed it from the config entry's declared
        ``type`` (when it is a known type) else from name inference — the compat
        shim for entries written before the table existed. This is what turns an
        *inferred* answer into a *declared* hard row without a migration: a
        provider that was ``commandcode`` by name becomes a hard ``commandcode``.

        Returning the count written makes it testable. Idempotent: never
        clobbers a row that already exists.
        """
        written = 0
        try:
            providers = (config.providers or {}) if hasattr(config, "providers") else {}
        except Exception:
            return 0
        for name, entry in (providers or {}).items():
            if self.get(name):
                continue
            declared = ""
            if isinstance(entry, dict):
                raw = entry.get("type")
                if isinstance(raw, str) and raw.strip().lower() in PROVIDER_TYPES:
                    declared = raw.strip().lower()
            ptype = declared or infer_provider_type(name)
            if not ptype:
                continue
            try:
                self.set(name, ptype)
                written += 1
            except Exception:  # noqa: BLE001
                continue
        return written


# ── Component-runtime adapter ─────────────────────────────────────────────


class ProviderTypeStoreComponent(Component):
    """The provider-type store as a runtime component (server can resolve it)."""

    name = "provider_type_store"
    requires = ["engine", "config"]
    provides = ["provider_type_store"]

    def __init__(self) -> None:
        super().__init__()
        self.store: Optional[ProviderTypeStore] = None

    @property
    def service(self) -> Optional[ProviderTypeStore]:
        return self.store

    def setup(self, rt) -> Optional[object]:  # type: ignore[override]
        self.store = ProviderTypeStore(rt.resolve("engine"))
        # Backfill every configured provider from name inference so the whole
        # estate is on the hard type axis from boot (see backfill_config).
        try:
            cfg = rt.resolve("config")
            self.store.backfill_config(cfg)
        except Exception:  # noqa: BLE001 — never break boot on a backfill
            pass
        return None


def get_provider_type_store(engine=None) -> Optional[ProviderTypeStore]:
    """Resolve from the active runtime, else a legacy lazy store on *engine*."""
    from .runtime import resolve_service
    store = resolve_service("provider_type_store", fallback=None)
    if store is not None:
        return store
    if engine is not None:
        return ProviderTypeStore(engine)
    return None