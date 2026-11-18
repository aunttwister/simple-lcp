"""Accessors for the model-name normalisation tables (``mi_sources`` / ``mi_model_names``).

This is the single API for "which name does source X use for model Y". It exists so the
admin endpoints, the seeding path and the evidence export all read and write the same
shape, and so the JSON contract each of them exposes to the UI stays unchanged after the
retirement of ``model_registry`` / ``mi_aliases``.

Terminology
-----------
* **source** — a naming authority: a provider endpoint, a benchmark arena, the canonical
  registry, or the operator. ``mi_sources``.
* **name** — a spelling that source uses for a model. ``mi_model_names``.

The old ``provider_mappings`` JSON object ``{provider: provider_side_name}`` is exactly
the set of names asserted by sources of kind ``provider_endpoint``, so
``provider_mappings_for`` reconstructs it with one query.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from .model_identity import connect as _mi_connect

#: Kind of a naming authority.
KIND_CANONICAL = "canonical"
KIND_BENCHMARK = "benchmark_arena"
KIND_PROVIDER = "provider_endpoint"
KIND_OPERATOR = "operator"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(target: Any) -> Optional[str]:
    """Accept a db path, a SQLAlchemy engine, or None (the app default)."""
    if target is None:
        return None
    if isinstance(target, str):
        return target
    url = getattr(target, "url", None)
    if url is not None:
        return getattr(url, "database", None)
    return None


def _connect(target: Any = None) -> sqlite3.Connection:
    return _mi_connect(_path(target), ro=False)


def ensure_source(con: sqlite3.Connection, slug: str, kind: str = KIND_PROVIDER,
                  *, convention: Optional[str] = None) -> int:
    """The source_id for a naming authority, creating the row when absent.

    A provider source is linked to the endpoint it names things for, when that endpoint
    exists; that linkage is what scopes its names to one endpoint during resolution.
    """
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("source slug required")
    row = con.execute(
        "SELECT source_id FROM mi_sources WHERE slug = ?", (slug,)).fetchone()
    if row:
        return int(row["source_id"])
    ep = con.execute(
        "SELECT endpoint_id FROM mi_endpoints WHERE slug = ?", (slug,)).fetchone()
    con.execute(
        "INSERT INTO mi_sources (slug, kind, endpoint_id, convention, created_at)"
        " VALUES (?,?,?,?,?)",
        (slug, kind, int(ep["endpoint_id"]) if ep else None, convention, _now()))
    return int(con.execute(
        "SELECT source_id FROM mi_sources WHERE slug = ?", (slug,)).fetchone()["source_id"])


def provider_mappings_for(con: sqlite3.Connection, model_id: int) -> dict:
    """Reconstruct the old ``{provider: name}`` object for one model.

    A provider can have asserted more than one spelling over time (a renamed id, a
    documented alias). The old JSON object could hold only one, so the pick must be
    deterministic and defensible: a human-verified name first, then the higher
    confidence, then alphabetical. Without an explicit order the winner would depend on
    row order, i.e. on nothing.
    """
    _rank = ("ORDER BY (n.verified_at IS NULL),"
             " CASE n.confidence WHEN 'verified' THEN 0 WHEN 'high' THEN 1"
             "   WHEN 'observed' THEN 2 ELSE 3 END, n.name")
    out: dict = {}
    for r in con.execute(
            "SELECT s.slug AS source, n.name FROM mi_model_names n"
            " JOIN mi_sources s ON s.source_id = n.source_id"
            " WHERE n.model_id = ? AND s.kind = ? " + _rank,
            (model_id, KIND_PROVIDER)):
        out.setdefault(r["source"], r["name"])
    return out


def _migrated(con: sqlite3.Connection) -> bool:
    """True when the normalisation tables exist — an unmigrated DB has no registry."""
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mi_model_names'"
    ).fetchone() is not None


def unbound_deployments(target: Any = None) -> dict:
    """Registered deployments that have no portfolio — the only rows to resolve.

    Registration is the operator's rule, and it is a predicate, not a flag: a model
    counts as registered only when ``mi_models.status='active'`` AND
    ``mi_deployments.status='active'`` — the same pair every read path already uses
    (see ``model_ratings.VISIBLE_PREDICATE``).

    The poll's discovery queue is NOT registration. ``mi_deployments.status='new'``
    holds every model ID an endpoint serves, vetted or not (77 of them on staging),
    and showing those as though they were part of the estate is exactly what the
    operator objected to on 2026-11-17: "Can we only have the models that are
    registered". So this function never returns them.
    """
    con = _connect(target)
    con.row_factory = sqlite3.Row
    try:
        if not _migrated(con):
            return {"registered": 0, "unbound": [], "count": 0}
        registered = con.execute(
            "SELECT COUNT(*) FROM mi_deployments WHERE status = 'active'").fetchone()[0]
        rows = [dict(r) for r in con.execute(
            "SELECT d.deployment_id, d.endpoint_id, d.served_id, d.source,"
            "       e.slug AS endpoint_slug"
            "  FROM mi_deployments d"
            "  LEFT JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
            " WHERE d.status = 'active' AND d.model_id IS NULL"
            " ORDER BY d.deployment_id DESC")]
        return {"registered": registered, "unbound": rows, "count": len(rows)}
    finally:
        con.close()


def registry_entries(target: Any = None) -> list[dict]:
    """Every canonical model with its names, in the shape the registry API exposes.

    ``providers``/``provider_mappings`` are the provider-asserted names; the release
    fields come from ``mi_models`` (where the retired ``model_registry`` put them).
    Returns [] on a database that predates the identity registry.
    """
    con = _connect(target)
    con.row_factory = sqlite3.Row
    try:
        if not _migrated(con):
            return []
        out = []
        for r in con.execute(
                "SELECT model_id, slug, benchmark_key, active_release, benchmark_release,"
                " quantization, created_at FROM mi_models ORDER BY slug"):
            mappings = provider_mappings_for(con, int(r["model_id"]))
            out.append({
                "logical_name": r["slug"],
                "benchmark_key": r["benchmark_key"],
                "providers": sorted(mappings.keys()),
                "provider_mappings": mappings,
                "active_release": r["active_release"],
                "benchmark_release": r["benchmark_release"],
                "quantization": r["quantization"],
                "updated_at": r["created_at"],
            })
        return out
    finally:
        con.close()


def conflicts(target: Any, *, logical_name: str, benchmark_key: str,
              provider_mappings: dict) -> list[str]:
    """Uniqueness violations a registry write would introduce.

    Two rules, both carried over from the retired table: a benchmark key belongs to one
    logical model, and a (provider, provider-side id) pair maps to one logical model.
    Returns human-readable reasons; empty means the write is allowed.
    """
    con = _connect(target)
    con.row_factory = sqlite3.Row
    out: list[str] = []
    try:
        for r in con.execute(
                "SELECT model_id, slug, benchmark_key FROM mi_models WHERE slug != ?",
                (logical_name,)):
            if benchmark_key and r["benchmark_key"] == benchmark_key:
                out.append(f"benchmark key '{benchmark_key}' is already registered to "
                           f"'{r['slug']}'")
            existing = provider_mappings_for(con, int(r["model_id"]))
            for prov, mid in (provider_mappings or {}).items():
                other = existing.get(prov)
                if other and other.strip().lower() == (mid or "").strip().lower():
                    out.append(f"provider '{prov}' already maps '{other}' to "
                               f"'{r['slug']}'")
        return out
    finally:
        con.close()


def upsert_entry(target: Any, *, logical_name: str, benchmark_key: str,
                 provider_mappings: dict, active_release: Optional[str] = None,
                 benchmark_release: Optional[str] = None,
                 quantization: Optional[str] = None) -> str:
    """Create or update a canonical model and its names. Returns 'created'|'updated'.

    The canonical slug and the benchmark key are recorded as names too, so a single
    lookup finds every spelling of the model. Provider names are written under their own
    provider sources, replacing that provider's previous name for this model (a rename
    must not leave the old spelling resolvable).
    """
    con = _connect(target)
    con.row_factory = sqlite3.Row
    now = _now()
    try:
        con.execute("BEGIN")
        row = con.execute(
            "SELECT model_id FROM mi_models WHERE slug = ?", (logical_name,)).fetchone()
        if row:
            model_id = int(row["model_id"])
            con.execute(
                "UPDATE mi_models SET benchmark_key = ?,"
                " active_release = COALESCE(?, active_release),"
                " benchmark_release = COALESCE(?, benchmark_release),"
                " quantization = COALESCE(?, quantization) WHERE model_id = ?",
                (benchmark_key, active_release, benchmark_release, quantization, model_id))
            action = "updated"
        else:
            con.execute(
                "INSERT INTO mi_models (slug, display_name, status, benchmark_key,"
                " active_release, benchmark_release, quantization, created_at)"
                " VALUES (?,?,'active',?,?,?,?,?)",
                (logical_name, logical_name, benchmark_key, active_release,
                 benchmark_release, quantization, now))
            model_id = int(con.execute(
                "SELECT model_id FROM mi_models WHERE slug = ?", (logical_name,)
            ).fetchone()["model_id"])
            action = "created"

        can = ensure_source(con, "canonical", KIND_CANONICAL)
        con.execute(
            "INSERT INTO mi_model_names (model_id, source_id, name, is_primary,"
            " confidence, first_seen, last_seen, verified_at) VALUES (?,?,?,1,'verified',?,?,?)"
            " ON CONFLICT(source_id, name) DO UPDATE SET model_id = excluded.model_id,"
            " is_primary = 1",
            (model_id, can, logical_name, now, now, now))
        if benchmark_key:
            ben = ensure_source(con, "benchmark", KIND_BENCHMARK)
            con.execute(
                "INSERT INTO mi_model_names (model_id, source_id, name, is_primary,"
                " confidence, first_seen, last_seen) VALUES (?,?,?,0,'high',?,?)"
                " ON CONFLICT(source_id, name) DO UPDATE SET model_id = excluded.model_id",
                (model_id, ben, benchmark_key, now, now))

        existing = provider_mappings_for(con, model_id)
        for prov in set(existing) | set(provider_mappings or {}):
            s = ensure_source(con, prov)
            new_name = (provider_mappings or {}).get(prov)
            con.execute(
                "DELETE FROM mi_model_names WHERE source_id = ? AND model_id = ?", (s, model_id))
            if new_name:
                con.execute(
                    "INSERT INTO mi_model_names (model_id, source_id, name, is_primary,"
                    " confidence, evidence, first_seen, last_seen, verified_at)"
                    " VALUES (?,?,?,0,'verified','admin registry edit',?,?,?)"
                    " ON CONFLICT(source_id, name) DO UPDATE SET model_id = excluded.model_id",
                    (model_id, s, new_name, now, now, now))
        con.commit()
        return action
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def remove_provider(target: Any, provider: str) -> tuple[int, int]:
    """Drop a provider's names everywhere. Returns (rows_changed, models_left_unserved).

    A model whose last provider name goes away is KEPT: its benchmark identity and
    capability scores still mean something, it simply is not served right now.
    """
    con = _connect(target)
    try:
        s = con.execute("SELECT source_id FROM mi_sources WHERE slug = ?", (provider,)).fetchone()
        if s is None:
            return 0, 0
        src_id = int(s["source_id"])
        models = [int(r["model_id"]) for r in con.execute(
            "SELECT DISTINCT model_id FROM mi_model_names WHERE source_id = ?", (src_id,))]
        cur = con.execute("DELETE FROM mi_model_names WHERE source_id = ?", (src_id,))
        changed = cur.rowcount or 0
        unserved = 0
        for mid in models:
            left = con.execute(
                "SELECT COUNT(*) FROM mi_model_names n JOIN mi_sources s2"
                " ON s2.source_id = n.source_id WHERE n.model_id = ? AND s2.kind = ?",
                (mid, KIND_PROVIDER)).fetchone()[0]
            if not left:
                unserved += 1
        con.commit()
        return changed, unserved
    finally:
        con.close()


def rename_provider(target: Any, old: str, new: str) -> int:
    """Re-point every name from *old* to *new*. Returns the rows moved.

    A rename is a graph rewrite: the old spelling must stop resolving, or traffic keeps
    landing on a provider that no longer exists under that name.
    """
    con = _connect(target)
    con.row_factory = sqlite3.Row
    try:
        s = con.execute("SELECT * FROM mi_sources WHERE slug = ?", (old,)).fetchone()
        if s is None:
            return 0
        ep = con.execute(
            "SELECT endpoint_id FROM mi_endpoints WHERE slug = ?", (new,)).fetchone()
        target_ep = int(ep["endpoint_id"]) if ep else s["endpoint_id"]
        con.execute("BEGIN")
        new_id = ensure_source(con, new, KIND_PROVIDER)
        con.execute(
            "UPDATE mi_sources SET endpoint_id = COALESCE(?, endpoint_id) WHERE source_id = ?",
            (target_ep, new_id))
        names = con.execute(
            "SELECT id, model_id, name, is_primary, confidence, evidence, first_seen,"
            " last_seen, verified_at, note FROM mi_model_names WHERE source_id = ?",
            (int(s["source_id"]),)).fetchall()
        moved = 0
        for n in names:
            try:
                con.execute(
                    "INSERT INTO mi_model_names (model_id, source_id, name, is_primary,"
                    " confidence, evidence, first_seen, last_seen, verified_at, note)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (n["model_id"], new_id, n["name"], n["is_primary"], n["confidence"],
                     n["evidence"], n["first_seen"], n["last_seen"], n["verified_at"],
                     n["note"]))
                moved += 1
            except sqlite3.IntegrityError:
                # the new source already asserts this name for a model — keep that row
                pass
        con.execute("DELETE FROM mi_model_names WHERE source_id = ?", (int(s["source_id"]),))
        con.execute("UPDATE mi_sources SET status = 'retired' WHERE source_id = ?",
                    (int(s["source_id"]),))
        con.commit()
        return moved
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def delete_entry(target: Any, logical_name: str) -> bool:
    """Retire a canonical model so it stops resolving. Returns True when it existed.

    The names are removed (so no spelling resolves to it any more) and the model is
    marked ``retired`` rather than deleted: its capability history stays, and the
    identity layer's ``resolve()`` honours only ``active`` models — so the routing effect
    is the same as the old row deletion, without discarding the scores.
    """
    con = _connect(target)
    try:
        row = con.execute(
            "SELECT model_id FROM mi_models WHERE slug = ?", (logical_name,)).fetchone()
        if row is None:
            return False
        model_id = int(row["model_id"])
        con.execute("BEGIN")
        con.execute("DELETE FROM mi_model_names WHERE model_id = ?", (model_id,))
        con.execute("UPDATE mi_models SET status = 'retired' WHERE model_id = ?", (model_id,))
        con.commit()
        return True
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def sources(target: Any = None) -> list[dict]:
    """Every naming authority with how many names it asserts."""
    con = _connect(target)
    con.row_factory = sqlite3.Row
    try:
        if not _migrated(con):
            return []
        return [dict(r) for r in con.execute(
            "SELECT s.source_id, s.slug, s.kind, s.endpoint_id, s.status, s.convention,"
            " (SELECT COUNT(*) FROM mi_model_names n WHERE n.source_id = s.source_id) AS names"
            " FROM mi_sources s ORDER BY s.kind, s.slug")]
    finally:
        con.close()
