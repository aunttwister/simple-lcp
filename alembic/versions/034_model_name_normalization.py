"""Model-name normalization: one source registry, one name table.

Why
---
Before this, "which name does source X use for model Y" was recorded in **four**
places with no link between them:

* ``model_registry.provider_mappings_json`` — ``{provider: provider_side_name}`` as a
  JSON blob (right idea, unusable shape). This is what ``router.py`` reads in the
  ROUTING path.
* ``mi_aliases`` — ``(alias, model_id, deployment_id, endpoint_id, source)`` with
  ``source`` as free text carrying six different conventions (``registry``,
  ``operator``, ``provider_doc``, ``benchmark_import``, ``observed_catalog``,
  ``provider_catalog_inferred``).
* ``mi_deployments.served_id`` — the provider's spelling, doing double duty as the
  serving record *and* the name mapping.
* ``model_capabilities.model`` — a free-text join key resolved against all of the above.

and two canonical registries that did not reference each other: ``model_registry``
(16 rows, read by routing) and ``mi_models`` (21 rows, read by identity/capabilities).
Five models in ``mi_models`` were invisible to routing.

Shape
-----
* ``mi_sources`` — the naming authorities. One row per provider endpoint, the
  benchmark sources, the canonical registry and the operator. ``kind`` distinguishes a
  live provider from a benchmark arena from a human, and ``endpoint_id`` links a
  provider source to the endpoint it names things for.
* ``mi_model_names`` — ``(model_id, source_id) -> name``. One row per spelling a source
  uses, with confidence, evidence, validity window and a ``verified_at`` stamp for
  names a human confirmed. ``UNIQUE(source_id, name)`` — the same source cannot use one
  name for two models.

The old ``source`` free text becomes ``confidence`` (how much to trust the assertion),
which is the distinction it was actually carrying.

``mi_models`` gains the three release fields ``model_registry`` held, so the two
registries can be merged into one in the next revision.

Additive and reversible: two new tables plus three nullable columns. Nothing is dropped
here, and the backfill copies rather than moves, so every pre-existing reader still
sees its data.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None

#: The old free-text ``mi_aliases.source`` / evidence tag -> the confidence to record.
_CONFIDENCE = {
    "operator": "verified",
    "registry": "high",
    "provider_doc": "high",
    "benchmark_import": "high",
    "observed_catalog": "observed",
    "provider_catalog_inferred": "low",
}

_SOURCES = [
    # (slug, kind, endpoint_slug_or_None, convention)
    ("canonical", "canonical", None, "The gateway's own slug for a logical model"),
    ("benchmark", "benchmark_arena", None, "LiveBench / capability-snapshot key"),
    ("lmarena", "benchmark_arena", None, "LMArena leaderboard display name"),
    ("operator", "operator", None, "Declared by hand from the admin UI"),
]


def upgrade() -> None:
    op.create_table(
        "mi_sources",
        sa.Column("source_id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("kind", sa.Text(), nullable=False),   # canonical|benchmark_arena|provider_endpoint|operator
        sa.Column("endpoint_id", sa.Integer(), sa.ForeignKey("mi_endpoints.endpoint_id")),
        sa.Column("convention", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.Text()),
    )
    op.create_index("ix_mi_sources_kind", "mi_sources", ["kind", "status"])

    op.create_table(
        "mi_model_names",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("mi_models.model_id"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("mi_sources.source_id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Text()),
        sa.Column("evidence", sa.Text()),
        sa.Column("valid_from", sa.Text()),
        sa.Column("valid_to", sa.Text()),
        sa.Column("first_seen", sa.Text()),
        sa.Column("last_seen", sa.Text()),
        sa.Column("verified_at", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.UniqueConstraint("source_id", "name", name="uq_mi_model_name_per_source"),
    )
    op.create_index("ix_mi_model_names_model", "mi_model_names", ["model_id"])
    op.create_index("ix_mi_model_names_name", "mi_model_names", ["name"])

    # The three fields model_registry held that mi_models lacked — needed to merge the
    # two registries into one.
    op.add_column("mi_models", sa.Column("active_release", sa.Text()))
    op.add_column("mi_models", sa.Column("benchmark_release", sa.Text()))
    op.add_column("mi_models", sa.Column("quantization", sa.Text()))

    _backfill()


def _backfill() -> None:
    """Copy every existing spelling into mi_model_names with its authority attached."""
    con = op.get_bind()
    # A literal timestamp for the values that must be parameters. (strftime(...) is used
    # directly inside the SQL text elsewhere; it cannot be a bind value.)
    ts = datetime.now(timezone.utc).isoformat()

    def src_id(slug: str) -> int:
        return con.execute(
            sa.text("SELECT source_id FROM mi_sources WHERE slug = :s"), {"s": slug}
        ).scalar()

    # ---- sources: the fixed authorities, one per endpoint, plus legacy endpoint names
    known_endpoints = con.execute(
        sa.text("SELECT endpoint_id, slug FROM mi_endpoints ORDER BY endpoint_id")
    ).fetchall()
    con.execute(
        sa.text("INSERT INTO mi_sources (slug, kind, endpoint_id, convention, created_at)"
                " VALUES (:slug, :kind, :ep, :conv, strftime('%Y-%m-%dT%H:%M:%fZ','now'))"),
        [
            {"slug": s, "kind": k,
             "ep": None if ep is None else next((e[0] for e in known_endpoints if e[1] == ep), None),
             "conv": conv}
            for s, k, ep, conv in _SOURCES
        ],
    )
    for ep_id, ep_slug in known_endpoints:
        con.execute(
            sa.text("INSERT INTO mi_sources (slug, kind, endpoint_id, convention, created_at)"
                    " VALUES (:slug, 'provider_endpoint', :ep, :conv,"
                    " strftime('%Y-%m-%dT%H:%M:%fZ','now'))"),
            {"slug": ep_slug, "ep": ep_id, "conv": f"Endpoint {ep_slug} model ids"},
        )
    # mi_endpoint_aliases: an alternate spelling for an endpoint (e.g. 'zgx' -> local-zgx)
    for row in con.execute(sa.text(
            "SELECT a.alias, a.endpoint_id FROM mi_endpoint_aliases a")).fetchall():
        con.execute(
            sa.text("INSERT OR IGNORE INTO mi_sources"
                    " (slug, kind, endpoint_id, convention, created_at)"
                    " VALUES (:slug, 'provider_endpoint', :ep, :conv,"
                    " strftime('%Y-%m-%dT%H:%M:%fZ','now'))"),
            {"slug": row[0], "ep": row[1],
             "conv": "Legacy provider spelling for the same endpoint"},
        )

    # ---- names: the canonical slug and benchmark key of every model
    for model_id, slug, bkey in con.execute(sa.text(
            "SELECT model_id, slug, benchmark_key FROM mi_models")).fetchall():
        con.execute(
            sa.text("INSERT OR IGNORE INTO mi_model_names"
                    " (model_id, source_id, name, is_primary, confidence, first_seen,"
                    "  last_seen) VALUES (:m, :s, :n, 1, 'high',"
                    "  strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                    "  strftime('%Y-%m-%dT%H:%M:%fZ','now'))"),
            {"m": model_id, "s": src_id("canonical"), "n": slug},
        )
        if bkey:
            con.execute(
                sa.text("INSERT OR IGNORE INTO mi_model_names"
                        " (model_id, source_id, name, is_primary, confidence, first_seen,"
                        "  last_seen) VALUES (:m, :s, :n, 0, 'high',"
                        "  strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                        "  strftime('%Y-%m-%dT%H:%M:%fZ','now'))"),
                {"m": model_id, "s": src_id("benchmark"), "n": bkey},
            )

    # ---- names: the provider's served id for every linked deployment
    for d in con.execute(sa.text(
            "SELECT d.model_id, d.served_id, e.slug AS endpoint, d.source,"
            "       d.first_seen, d.last_seen"
            " FROM mi_deployments d JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
            " WHERE d.model_id IS NOT NULL")).fetchall():
        s = src_id(d[2])
        if s is None:
            continue
        if d[1] == d[2]:          # served id equals the endpoint slug: no extra info
            continue
        conf = {"registry": "high", "traffic": "high", "observed": "observed",
                "poll": "observed"}.get(d[3] or "", "high")
        con.execute(
            sa.text("INSERT OR IGNORE INTO mi_model_names"
                    " (model_id, source_id, name, is_primary, confidence, evidence,"
                    "  first_seen, last_seen) VALUES (:m, :s, :n, 0, :c, :e, :fs, :ls)"),
            {"m": d[0], "s": s, "n": d[1], "c": conf,
             "e": f"served id ({d[3] or 'unknown'})", "fs": d[4], "ls": d[5]},
        )

    # ---- names: the legacy alias table (its free-text source becomes confidence)
    for a in con.execute(sa.text(
            "SELECT a.alias, a.model_id, a.deployment_id, a.endpoint_id, a.source,"
            "       a.valid_from, a.valid_to,"
            "  (SELECT d.model_id FROM mi_deployments d WHERE d.deployment_id = a.deployment_id),"
            "  (SELECT e.slug FROM mi_endpoints e WHERE e.endpoint_id = a.endpoint_id)"
            " FROM mi_aliases a")).fetchall():
        alias, model_id, dep_id, ep_id, old_src, vf, vt, dep_model, ep_slug = a
        target = model_id if model_id is not None else dep_model
        if target is None:
            continue
        # An alias declared for an endpoint belongs to that provider source; a global
        # alias belongs to the operator.
        source_slug = ep_slug if ep_slug else "operator"
        s = src_id(source_slug)
        if s is None:
            continue
        conf = _CONFIDENCE.get((old_src or "").strip(), "observed")
        con.execute(
            sa.text("INSERT OR IGNORE INTO mi_model_names"
                    " (model_id, source_id, name, is_primary, confidence, evidence,"
                    "  valid_from, valid_to, first_seen, last_seen, verified_at)"
                    " VALUES (:m, :s, :n, 0, :c, :e, :vf, :vt,"
                    "  strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                    "  strftime('%Y-%m-%dT%H:%M:%fZ','now'), :va)"),
            {"m": target, "s": s, "n": alias, "c": conf,
             "e": f"legacy alias ({old_src})", "vf": vf, "vt": vt,
             "va": ts if conf == "verified" else None},
        )

    # ---- names: the legacy registry's provider_mappings JSON, plus its release fields
    rows = con.execute(sa.text(
        "SELECT logical_name, benchmark_key, provider_mappings_json, active_release,"
        "       benchmark_release, quantization FROM model_registry")).fetchall()
    for logical, bkey, pm_json, active, bench_rel, quant in rows:
        mid = con.execute(
            sa.text("SELECT model_id FROM mi_models WHERE slug = :s"), {"s": logical}
        ).scalar()
        if mid is None:
            # The merge direction is model_registry -> mi_models only in this revision
            # (mi_models is a superset), so a missing row means the data is malformed;
            # create it rather than silently drop a routing-visible model.
            con.execute(
                sa.text("INSERT INTO mi_models (slug, display_name, status, benchmark_key,"
                        " created_at) VALUES (:s, :s, 'active', :b,"
                        " strftime('%Y-%m-%dT%H:%M:%fZ','now'))"),
                {"s": logical, "b": bkey},
            )
            mid = con.execute(
                sa.text("SELECT model_id FROM mi_models WHERE slug = :s"), {"s": logical}
            ).scalar()
        # the release fields the old registry held
        con.execute(
            sa.text("UPDATE mi_models SET active_release = COALESCE(active_release, :a),"
                    " benchmark_release = COALESCE(benchmark_release, :b),"
                    " quantization = COALESCE(quantization, :q),"
                    " benchmark_key = COALESCE(NULLIF(benchmark_key, ''), :k)"
                    " WHERE model_id = :m"),
            {"a": active, "b": bench_rel, "q": quant, "k": bkey, "m": mid},
        )
        try:
            mappings = json.loads(pm_json or "{}")
        except (TypeError, ValueError):
            mappings = {}
        for provider, name in (mappings or {}).items():
            if not name:
                continue
            s = src_id(provider)
            if s is None:
                # A provider that is not a live endpoint (retired lane). Register it so
                # the mapping is not lost, with no endpoint attached.
                con.execute(
                    sa.text("INSERT INTO mi_sources (slug, kind, convention, created_at)"
                            " VALUES (:s, 'provider_endpoint', :c,"
                            " strftime('%Y-%m-%dT%H:%M:%fZ','now'))"),
                    {"s": provider, "c": "Retired provider spelling from the legacy registry"},
                )
                s = src_id(provider)
            con.execute(
                sa.text("INSERT OR IGNORE INTO mi_model_names"
                        " (model_id, source_id, name, is_primary, confidence, evidence,"
                        "  first_seen, last_seen, verified_at)"
                        " VALUES (:m, :s, :n, 0, 'high', :e,"
                        "  strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                        "  strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                        "  strftime('%Y-%m-%dT%H:%M:%fZ','now'))"),
                {"m": mid, "s": s, "n": name,
                 "e": "legacy registry provider mapping (hand-curated)"},
            )


def downgrade() -> None:
    op.drop_column("mi_models", "quantization")
    op.drop_column("mi_models", "benchmark_release")
    op.drop_column("mi_models", "active_release")
    op.drop_index("ix_mi_model_names_name", table_name="mi_model_names")
    op.drop_index("ix_mi_model_names_model", table_name="mi_model_names")
    op.drop_table("mi_model_names")
    op.drop_index("ix_mi_sources_kind", table_name="mi_sources")
    op.drop_table("mi_sources")
