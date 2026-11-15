"""Test fixtures for the model-name normalisation tables.

The registry moved from ``model_registry`` (one row per model, provider spellings in a
JSON blob) and ``mi_aliases`` to ``mi_sources`` (naming authorities) plus
``mi_model_names`` (one row per spelling). These helpers build that schema and seed it,
so a test can state a registry in one line per fact instead of hand-writing SQL.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

#: The exact DDL from migration 034, so a test schema matches production.
_DDL = """
CREATE TABLE IF NOT EXISTS mi_endpoints (
    endpoint_id INTEGER PRIMARY KEY, slug TEXT UNIQUE, display_name TEXT, base_url TEXT,
    tier TEXT, status TEXT DEFAULT 'active', notes TEXT, created_at TEXT,
    poll_enabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS mi_models (
    model_id INTEGER PRIMARY KEY, slug TEXT UNIQUE, display_name TEXT, family TEXT,
    status TEXT DEFAULT 'active', supersedes_model_id INTEGER, benchmark_key TEXT,
    created_at TEXT, active_release TEXT, benchmark_release TEXT, quantization TEXT);
CREATE TABLE IF NOT EXISTS mi_sources (
    source_id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
    endpoint_id INTEGER, convention TEXT, status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT);
CREATE TABLE IF NOT EXISTS mi_model_names (
    id INTEGER PRIMARY KEY, model_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
    name TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0, confidence TEXT,
    evidence TEXT, valid_from TEXT, valid_to TEXT, first_seen TEXT, last_seen TEXT,
    verified_at TEXT, note TEXT, UNIQUE(source_id, name));
CREATE INDEX IF NOT EXISTS ix_mi_model_names_model ON mi_model_names (model_id);
CREATE INDEX IF NOT EXISTS ix_mi_model_names_name ON mi_model_names (name);
CREATE TABLE IF NOT EXISTS mi_registry_versions (
    version INTEGER PRIMARY KEY, created_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS mi_deployments (
    deployment_id INTEGER PRIMARY KEY, endpoint_id INTEGER, model_id INTEGER,
    served_id TEXT, variant TEXT, quantisation TEXT, context_window INTEGER,
    status TEXT DEFAULT 'active', source TEXT, verified_at TEXT, first_seen TEXT,
    last_seen TEXT, notes TEXT, quant_id INTEGER REFERENCES mi_quants (quant_id));
CREATE TABLE IF NOT EXISTS mi_quants (
    quant_id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, label TEXT NOT NULL,
    family TEXT, bits REAL, status TEXT NOT NULL DEFAULT 'active', created_at TEXT);
CREATE TABLE IF NOT EXISTS mi_deployment_releases (
    id INTEGER PRIMARY KEY, deployment_id INTEGER, release_id INTEGER,
    effective_from TEXT, effective_to TEXT, source TEXT, confidence TEXT);
CREATE TABLE IF NOT EXISTS mi_releases (
    release_id INTEGER PRIMARY KEY, model_id INTEGER, label TEXT, released_on TEXT,
    weights_ref TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS model_capabilities (
    id INTEGER PRIMARY KEY, model TEXT, task_type TEXT, score REAL, source TEXT,
    benchmark_category TEXT, raw_score REAL, updated_at TEXT, release_label TEXT);
"""

#: The fixed authorities migration 034 always creates.
_BASE_SOURCES = [
    ("canonical", "canonical"),
    ("benchmark", "benchmark_arena"),
    ("lmarena", "benchmark_arena"),
    ("operator", "operator"),
]


def mi_schema(con: sqlite3.Connection) -> sqlite3.Connection:
    """Create the model-intelligence tables on an open connection."""
    con.executescript(_DDL)
    con.row_factory = sqlite3.Row
    for slug, kind in _BASE_SOURCES:
        con.execute("INSERT OR IGNORE INTO mi_sources (slug, kind) VALUES (?,?)",
                    (slug, kind))
    con.commit()
    return con


def new_db() -> sqlite3.Connection:
    """An in-memory DB with the model-intelligence schema ready."""
    return mi_schema(sqlite3.connect(":memory:"))


def add_endpoint(con, slug: str, *, status: str = "active") -> int:
    con.execute("INSERT INTO mi_endpoints (slug, display_name, status) VALUES (?,?,?)",
                (slug, slug, status))
    con.execute(
        "INSERT OR IGNORE INTO mi_sources (slug, kind, endpoint_id) VALUES (?, 'provider_endpoint',"
        " (SELECT endpoint_id FROM mi_endpoints WHERE slug = ?))", (slug, slug))
    con.commit()
    return int(con.execute(
        "SELECT endpoint_id FROM mi_endpoints WHERE slug = ?", (slug,)).fetchone()[0])


def add_source(con, slug: str, kind: str = "provider_endpoint",
               endpoint_id=None) -> int:
    con.execute("INSERT OR IGNORE INTO mi_sources (slug, kind, endpoint_id) VALUES (?,?,?)",
                (slug, kind, endpoint_id))
    con.commit()
    return int(con.execute(
        "SELECT source_id FROM mi_sources WHERE slug = ?", (slug,)).fetchone()[0])


def add_model(con, slug: str, *, benchmark_key=None, status: str = "active",
              active_release=None, benchmark_release=None,
              quantization=None) -> int:
    """A canonical model plus its slug name row."""
    con.execute(
        "INSERT INTO mi_models (slug, display_name, status, benchmark_key, active_release,"
        " benchmark_release, quantization) VALUES (?,?,?,?,?,?,?)",
        (slug, slug, status, benchmark_key, active_release, benchmark_release, quantization))
    model_id = int(con.execute(
        "SELECT model_id FROM mi_models WHERE slug = ?", (slug,)).fetchone()[0])
    add_name(con, model_id, "canonical", slug, is_primary=1, verified=True)
    if benchmark_key:
        add_name(con, model_id, "benchmark", benchmark_key)
    con.commit()
    return model_id


def add_name(con, model_id: int, source_slug: str, name: str, *, is_primary=0,
             confidence="high", verified=False, evidence=None,
             valid_from=None, valid_to=None) -> int:
    """One spelling that one source uses for one model."""
    src = con.execute("SELECT source_id FROM mi_sources WHERE slug = ?",
                      (source_slug,)).fetchone()
    if src is None:
        raise ValueError(f"unknown source {source_slug!r}; add it with add_source()")
    cur = con.execute(
        "INSERT INTO mi_model_names (model_id, source_id, name, is_primary, confidence,"
        " evidence, valid_from, valid_to, first_seen, last_seen, verified_at)"
        " VALUES (?,?,?,?,?,?,?,?,'2026-01-01','2026-01-01',?)",
        (model_id, int(src["source_id"]), name, is_primary, confidence, evidence,
         valid_from, valid_to, "2026-01-01" if verified else None))
    con.commit()
    return int(cur.lastrowid)


def add_deployment(con, endpoint_slug: str, served_id: str, model_slug=None, *,
                   status: str = "active", verified: bool = False,
                   quant: Optional[str] = None,
                   observed_quant: Optional[str] = None) -> int:
    """A (endpoint, served id) pair, optionally linked to a canonical model.

    ``observed_quant`` is the engine's own report (free text, kept as evidence);
    ``quant`` is the resolved quantisation entity it is linked to.
    """
    model_id = None
    if model_slug:
        row = con.execute("SELECT model_id FROM mi_models WHERE slug = ?",
                          (model_slug,)).fetchone()
        model_id = int(row[0]) if row else None
    quant_id = None
    if quant:
        add_quant(con, quant)
        quant_id = int(con.execute("SELECT quant_id FROM mi_quants WHERE slug = ?",
                                   (quant,)).fetchone()[0])
    con.execute(
        "INSERT INTO mi_deployments (endpoint_id, model_id, served_id, status, source,"
        " first_seen, last_seen, verified_at, quantisation, quant_id) VALUES"
        " ((SELECT endpoint_id FROM mi_endpoints WHERE slug = ?),?,?,?,'fixture',"
        " '2026-01-01','2026-01-01',?,?,?)",
        (endpoint_slug, model_id, served_id, status,
         "2026-01-01" if verified else None, observed_quant, quant_id))
    con.commit()
    return int(con.execute(
        "SELECT MAX(deployment_id) FROM mi_deployments").fetchone()[0])


def add_quant(con, slug: str, *, label: Optional[str] = None,
              family: Optional[str] = None, bits: Optional[float] = None) -> int:
    """One quantisation entity. Idempotent, like the real ``ensure_quant``."""
    con.execute(
        "INSERT OR IGNORE INTO mi_quants (slug, label, family, bits, created_at)"
        " VALUES (?,?,?,?,'2026-01-01')",
        (slug.strip().lower(), label or slug.upper(), family, bits))
    con.commit()
    return int(con.execute("SELECT quant_id FROM mi_quants WHERE slug = ?",
                           (slug.strip().lower(),)).fetchone()[0])


def provider_mappings(con, model_slug: str) -> dict:
    """The ``{provider: name}`` object for a model, from the new tables."""
    rows = con.execute(
        "SELECT s.slug, n.name FROM mi_model_names n"
        " JOIN mi_sources s ON s.source_id = n.source_id"
        " JOIN mi_models m ON m.model_id = n.model_id"
        " WHERE m.slug = ? AND s.kind = 'provider_endpoint'", (model_slug,)).fetchall()
    return {r[0]: r[1] for r in rows}
