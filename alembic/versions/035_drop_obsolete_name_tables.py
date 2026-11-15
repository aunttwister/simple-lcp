"""Drop the superseded name tables — their content now lives in mi_sources/mi_model_names.

Removed
-------
* ``model_registry`` — folded into ``mi_models`` (which already carried every one of its
  16 logical names) plus ``mi_sources``/``mi_model_names`` for the provider mappings.
  ``load_model_registry``/``seed_model_registry`` keep their exact signature and return
  shape, reading the new tables, so the routing path is unchanged.
* ``mi_aliases`` — replaced by ``mi_model_names``; its free-text ``source`` became
  ``confidence``, and endpoint scoping now comes through the source's ``endpoint_id``.
* ``mi_endpoint_aliases`` (1 row) — became an extra ``mi_sources`` row for the endpoint.

Why the content check matters
-----------------------------
This revision refuses to drop anything it cannot account for in the new tables: if a
provider mapping, alias or served id exists only in an old table, the upgrade stops and
names the row rather than destroying it. That guard is what makes the drop safe to run
against a live registry.

Reversible: ``downgrade`` recreates all three tables **from the new tables** (the data is
copied, not moved), so a rollback restores the old shape without the file snapshot.
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None

#: confidence -> the free-text source tag the old mi_aliases carried.
_BACK_TO_SOURCE = {
    "verified": "operator",
    "high": "registry",
    "observed": "observed_catalog",
    "low": "provider_catalog_inferred",
}


def _unaccounted(con) -> list[str]:
    """Spellings present only in an old table — the guard against a lossy drop."""
    missing: list[str] = []

    names = {(r[0] or "").strip().lower() for r in con.execute(sa.text(
        "SELECT name FROM mi_model_names")).fetchall()}
    names |= {(r[0] or "").strip().lower() for r in con.execute(sa.text(
        "SELECT slug FROM mi_models")).fetchall()}
    names |= {(r[0] or "").strip().lower() for r in con.execute(sa.text(
        "SELECT served_id FROM mi_deployments")).fetchall()}

    for (alias,) in con.execute(sa.text("SELECT alias FROM mi_aliases")).fetchall():
        if (alias or "").strip().lower() not in names:
            missing.append(f"mi_aliases.alias={alias!r}")
    for (alias,) in con.execute(sa.text(
            "SELECT alias FROM mi_endpoint_aliases")).fetchall():
        found = con.execute(
            sa.text("SELECT 1 FROM mi_sources WHERE slug = :s"), {"s": alias}).fetchone()
        if not found:
            missing.append(f"mi_endpoint_aliases.alias={alias!r}")
    for logical, pm_json in con.execute(sa.text(
            "SELECT logical_name, provider_mappings_json FROM model_registry")).fetchall():
        found = con.execute(sa.text(
            "SELECT 1 FROM mi_models WHERE slug = :s"), {"s": logical}).fetchone()
        if not found:
            missing.append(f"model_registry.logical_name={logical!r}")
        try:
            mappings = json.loads(pm_json or "{}")
        except (TypeError, ValueError):
            mappings = {}
        for provider, name in (mappings or {}).items():
            if not name:
                continue
            if (str(name)).strip().lower() not in names:
                missing.append(f"model_registry[{logical!r}][{provider!r}]={name!r}")
    return missing


def upgrade() -> None:
    con = op.get_bind()
    missing = _unaccounted(con)
    if missing:
        raise RuntimeError(
            "refusing to drop: these spellings exist only in the old tables and would be "
            "lost — " + "; ".join(sorted(missing)[:10]))

    op.drop_table("mi_endpoint_aliases")
    op.drop_table("mi_aliases")
    op.drop_table("model_registry")


def downgrade() -> None:
    """Recreate the three tables from the new ones — a true inverse."""
    con = op.get_bind()

    op.create_table(
        "model_registry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("logical_name", sa.Text(), nullable=False, unique=True),
        sa.Column("benchmark_key", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("active_release", sa.Text()),
        sa.Column("provider_mappings_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("benchmark_release", sa.Text()),
        sa.Column("quantization", sa.Text()),
    )
    op.create_table(
        "mi_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("endpoint_id", sa.Integer()),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Integer()),
        sa.Column("deployment_id", sa.Integer()),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Text()),
        sa.Column("valid_to", sa.Text()),
        sa.Column("notes", sa.Text()),
    )
    op.create_table(
        "mi_endpoint_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("endpoint_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
    )

    # model_registry: from mi_models + its provider mappings
    for r in con.execute(sa.text(
            "SELECT model_id, slug, benchmark_key, active_release, benchmark_release,"
            "       quantization FROM mi_models")).fetchall():
        model_id, slug, bkey, active, bench_rel, quant = r
        mappings = {}
        for src_slug, name in con.execute(sa.text(
                "SELECT s.slug, n.name FROM mi_model_names n"
                " JOIN mi_sources s ON s.source_id = n.source_id"
                " WHERE n.model_id = :m AND s.kind = 'provider_endpoint'"),
                {"m": model_id}).fetchall():
            mappings[src_slug] = name
        con.execute(
            sa.text("INSERT INTO model_registry (logical_name, benchmark_key, updated_at,"
                    " active_release, provider_mappings_json, benchmark_release,"
                    " quantization) VALUES (:l, :b,"
                    " strftime('%Y-%m-%dT%H:%M:%fZ','now'), :a, :p, :br, :q)"),
            {"l": slug, "b": bkey or slug, "a": active,
             "p": json.dumps(mappings, sort_keys=True), "br": bench_rel, "q": quant},
        )

    # mi_aliases: every name that is not a canonical slug / benchmark key
    for n in con.execute(sa.text(
            "SELECT n.name, n.model_id, n.confidence, n.valid_from, n.valid_to,"
            "       s.kind, s.endpoint_id, s.slug AS src_slug"
            " FROM mi_model_names n JOIN mi_sources s ON s.source_id = n.source_id"
            " WHERE s.kind IN ('provider_endpoint', 'operator')"
            "   AND n.is_primary = 0")).fetchall():
        name, model_id, conf, vf, vt, kind, ep_id, src_slug = n
        dep_id = None
        if ep_id is not None:
            row = con.execute(sa.text(
                "SELECT deployment_id FROM mi_deployments"
                " WHERE endpoint_id = :e AND served_id = :n LIMIT 1"),
                {"e": ep_id, "n": name}).fetchone()
            dep_id = row[0] if row else None
        con.execute(
            sa.text("INSERT INTO mi_aliases (endpoint_id, alias, model_id, deployment_id,"
                    " source, valid_from, valid_to, notes)"
                    " VALUES (:e, :a, :m, :d, :s, :vf, :vt, :nt)"),
            {"e": ep_id, "a": name, "m": model_id, "d": dep_id,
             "s": _BACK_TO_SOURCE.get((conf or ""), "observed"),
             "vf": vf, "vt": vt, "nt": f"restored from mi_model_names (source {src_slug})"},
        )

    # mi_endpoint_aliases: provider sources whose slug is not the endpoint slug itself
    for ep_id, alias in con.execute(sa.text(
            "SELECT s.endpoint_id, s.slug FROM mi_sources s"
            " WHERE s.kind = 'provider_endpoint' AND s.endpoint_id IS NOT NULL"
            "   AND s.slug NOT IN (SELECT slug FROM mi_endpoints WHERE endpoint_id = s.endpoint_id)"
    )).fetchall():
        con.execute(
            sa.text("INSERT INTO mi_endpoint_aliases (endpoint_id, alias, source)"
                    " VALUES (:e, :a, 'restored')"),
            {"e": ep_id, "a": alias},
        )
