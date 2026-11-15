"""Quantisation as an independent entity (``mi_quants``).

A quant is a property of the SERVED ARTIFACT, not of the model's identity. So:

* ``mi_models`` names the model, and carries no quantisation;
* ``mi_quants`` names the quantisation (``Q4_K_M``, ``EXL3``, ...);
* ``mi_deployments.quant_id`` says which quantisation a served artifact is, and is
  NULL for a deployment serving full precision.

Two reasons this is the right shape rather than a column on the model:

1. **Capability is measured per model, not per quant.** If a quant were part of the
   model's identity, one evaluation could not cover two quants of the same model and
   every quant would need its own duplicated capability rows for the same model.
2. **Routing selects on capability.** The quant is how the model happens to be served
   — a VRAM/speed choice — not a quality tier.

``mi_deployments.quantisation`` deliberately still holds the *observed* string (what
the engine reported, e.g. ``exl3-eaqv1-2.9bpw``) while ``quant_id`` holds the
*resolved* entity. Raw observation plus resolved reference, the same pattern as
``mi_model_names``: the raw value is evidence and is never overwritten by a lookup.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from .model_identity import connect as _mi_connect
from .model_names import _path

#: Format families, for grouping and for future bits-per-weight comparison.
FAMILY_GGUF = "gguf"
FAMILY_EXLLAMA = "exllama"
FAMILY_AWQ = "awq"
FAMILY_FP8 = "fp8"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(target: Any = None) -> sqlite3.Connection:
    con = _mi_connect(_path(target), ro=False)
    con.row_factory = sqlite3.Row
    return con


def _open(target: Any = None) -> tuple[sqlite3.Connection, bool]:
    """Open a connection, or adopt one the caller owns.

    Returns ``(connection, owned)`` so a read helper closes only what it opened — the
    same discipline the model-name accessors use, and the reason a test can pass an
    in-memory connection without having it closed underneath it.
    """
    if isinstance(target, sqlite3.Connection):
        target.row_factory = sqlite3.Row
        return target, False
    return _connect(target), True


def _bits_from(slug: str) -> Optional[float]:
    """``exl3-eaqv1-2.9bpw`` -> 2.9. None when the string states no bits.

    Never inferred: a quant with no stated bits per weight records none, so the
    absence stays visible instead of being guessed.
    """
    if "bpw" not in slug:
        return None
    import re
    m = re.search(r"(\d+(?:\.\d+)?)\s*bpw", slug)
    return float(m.group(1)) if m else None


def _table_exists(con: sqlite3.Connection) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mi_quants'"
    ).fetchone() is not None


def ensure_quant(con: sqlite3.Connection, slug: str, *, label: Optional[str] = None,
                 family: Optional[str] = None,
                 bits: Optional[float] = None) -> int:
    """The quant_id for a quantisation, creating the row when absent. Idempotent.

    ``slug`` is the normalised key (lower-cased). A re-run must not create a second
    row for the same quant — the same discipline as ``ensure_source``.
    """
    slug = (slug or "").strip().lower()
    if not slug:
        raise ValueError("a quant needs a slug")
    row = con.execute("SELECT quant_id FROM mi_quants WHERE slug = ?", (slug,)).fetchone()
    if row is not None:
        existing = row["quant_id"]
        if existing is not None:
            return int(existing)
    cur = con.execute(
        "INSERT INTO mi_quants (slug, label, family, bits, status, created_at)"
        " VALUES (?,?,?,?,'active',?)",
        (slug, label or slug.upper(), family, bits if bits is not None else _bits_from(slug),
         _now()))
    return int(cur.lastrowid)


def quants(target: Any = None) -> list[dict]:
    """Every quantisation, with how many deployments serve it.

    Returns [] on a database that predates the quant entity.
    """
    con, owned = _open(target)
    try:
        if not _table_exists(con):
            return []
        return [dict(r) for r in con.execute(
            "SELECT q.quant_id, q.slug, q.label, q.family, q.bits, q.status,"
            " (SELECT COUNT(*) FROM mi_deployments d WHERE d.quant_id = q.quant_id)"
            "   AS deployments"
            " FROM mi_quants q ORDER BY q.family, q.slug")]
    finally:
        if owned:
            con.close()


def quant_for_deployment(con: sqlite3.Connection, deployment_id: int) -> Optional[dict]:
    """The quantisation a served artifact is, or None (full precision)."""
    if not _table_exists(con):
        return None
    row = con.execute(
        "SELECT q.* FROM mi_deployments d JOIN mi_quants q ON q.quant_id = d.quant_id"
        " WHERE d.deployment_id = ?", (deployment_id,)).fetchone()
    return dict(row) if row else None


def deployments_for_quant(target: Any = None, slug: str = "") -> list[dict]:
    """Every served artifact using one quantisation."""
    con, owned = _open(target)
    try:
        if not _table_exists(con):
            return []
        return [dict(r) for r in con.execute(
            "SELECT d.deployment_id, e.slug AS endpoint, d.served_id, d.model_id,"
            " d.status, d.quantisation AS observed"
            " FROM mi_deployments d"
            " JOIN mi_quants q ON q.quant_id = d.quant_id"
            " LEFT JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
            " WHERE q.slug = ? ORDER BY e.slug, d.served_id", (slug,))]
    finally:
        if owned:
            con.close()


def set_deployment_quant(con: sqlite3.Connection, deployment_id: int,
                         slug: Optional[str]) -> bool:
    """Record which quantisation a served artifact is (None clears it).

    The observed string in ``mi_deployments.quantisation`` is NOT touched — it is the
    engine's own report and stays as evidence.
    """
    if not _table_exists(con):
        return False
    qid = None
    if slug:
        qid = ensure_quant(con, slug)
    cur = con.execute("UPDATE mi_deployments SET quant_id = ? WHERE deployment_id = ?",
                      (qid, deployment_id))
    return cur.rowcount > 0


def quant_identity_is_clean(target: Any = None) -> list[str]:
    """Models whose NAME still embeds a quantisation — the drift this design forbids.

    A model name is an identity; a quant belongs to the served artifact. Any hit here
    is a name to rename, not a quant to record.
    """
    con, owned = _open(target)
    try:
        markers = ("q4_k_m", "q5_k_m", "q8_0", "gguf", "awq", "gptq", "exl3", "fp8")
        hits = []
        for r in con.execute("SELECT slug FROM mi_models"):
            slug = (r["slug"] or "").lower()
            if any(m in slug for m in markers):
                hits.append(r["slug"])
        return hits
    finally:
        if owned:
            con.close()
