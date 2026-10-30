"""Model identity: one canonical registry for every place a model is named (CUR-270).

Additive, nullable and reversible. Nothing in this migration changes behaviour:
no existing column is read differently, no row is rewritten, and every new column
on `requests` / `routing_decisions` is NULL until the write path stamps it.

Why the shape is what it is
---------------------------

Today a model is identified by a *display string*, and the lookup that turns a
provider's id into that string is keyed on the **provider name** — which itself
gets renamed. That is the measured root cause of the alias noise in prod: live
traffic carries providers `local-sparks` / `local-zgx` while the registry's
mapping keys are `zgx` / `llamacpp`, so the local lanes resolve to nothing.

So the schema separates five things that were conflated:

* ``endpoints``  — where it can be called.       Keyed on a stable surrogate id;
                   the slug (the LCP provider key) may change, and
                   ``endpoint_aliases`` records the old spellings.
* ``models``     — the app's stable identity.    ``slug`` is used in policy and
                   reporting; ``model_id`` is an immutable surrogate so a rename
                   is never a data migration.
* ``releases``   — which weights exist.          Per model.
* ``deployments``— a served id on an endpoint.   The unit that actually predicts
                   price and capability, because the same model is served from
                   several endpoints at different prices and contexts.
* ``aliases``    — every accepted spelling, **per endpoint and time-windowed**,
                   with provenance. A global string->model map is impossible:
                   `deepseek-v4-flash` was a genuine model until 2026-09-10 and is a
                   routing alias to V4.1-Flash after it. The same string is two
                   different models, and which one it is depends on *when* — which is
                   why the timestamp is part of the key. The reselling endpoints carry
                   the same legacy name, so the rule is applied there too.

``deployment_releases`` is the historical-normalisation mechanism: a name cannot
express that a served id silently changed model underneath it, so the release in
effect is a *timeline* with an explicit source and confidence rather than a
column on the model.

Revision ID: 026
Revises: 025
Create Date: 2026-10-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = (
    "mi_registry_versions",
    "mi_deployment_releases",
    "mi_aliases",
    "mi_deployments",
    "mi_releases",
    "mi_models",
    "mi_endpoint_aliases",
    "mi_endpoints",
)


def _has_table(name: str) -> bool:
    """True when the table already exists (``create_all`` may have beaten us)."""
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(table: str) -> set:
    if not _has_table(table):
        return set()
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_table("mi_endpoints"):
        op.create_table(
            "mi_endpoints",
            sa.Column("endpoint_id", sa.Integer(), primary_key=True),
            sa.Column("slug", sa.Text(), nullable=False, unique=True),
            sa.Column("display_name", sa.Text(), nullable=False),
            sa.Column("base_url", sa.Text(), nullable=True),
            sa.Column("tier", sa.Text(), nullable=True),          # local | cloud
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
        )

    if not _has_table("mi_endpoint_aliases"):
        op.create_table(
            "mi_endpoint_aliases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("endpoint_id", sa.Integer(),
                      sa.ForeignKey("mi_endpoints.endpoint_id"), nullable=False),
            sa.Column("alias", sa.Text(), nullable=False, unique=True),
            sa.Column("source", sa.Text(), nullable=False),
        )

    if not _has_table("mi_models"):
        op.create_table(
            "mi_models",
            sa.Column("model_id", sa.Integer(), primary_key=True),
            sa.Column("slug", sa.Text(), nullable=False, unique=True),
            sa.Column("display_name", sa.Text(), nullable=False),
            sa.Column("family", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("supersedes_model_id", sa.Integer(),
                      sa.ForeignKey("mi_models.model_id"), nullable=True),
            # carried over from model_registry so the old surface can be retired
            sa.Column("benchmark_key", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Text(), nullable=False),
        )

    if not _has_table("mi_releases"):
        op.create_table(
            "mi_releases",
            sa.Column("release_id", sa.Integer(), primary_key=True),
            sa.Column("model_id", sa.Integer(),
                      sa.ForeignKey("mi_models.model_id"), nullable=False),
            sa.Column("label", sa.Text(), nullable=False),
            sa.Column("released_on", sa.Text(), nullable=True),
            sa.Column("weights_ref", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.UniqueConstraint("model_id", "label", name="uq_mi_release"),
        )

    if not _has_table("mi_deployments"):
        op.create_table(
            "mi_deployments",
            sa.Column("deployment_id", sa.Integer(), primary_key=True),
            sa.Column("endpoint_id", sa.Integer(),
                      sa.ForeignKey("mi_endpoints.endpoint_id"), nullable=False),
            sa.Column("model_id", sa.Integer(),
                      sa.ForeignKey("mi_models.model_id"), nullable=False),
            sa.Column("served_id", sa.Text(), nullable=False),
            sa.Column("variant", sa.Text(), nullable=True),       # e.g. "fast"
            sa.Column("quantisation", sa.Text(), nullable=True),
            sa.Column("context_window", sa.Integer(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default="active"),
            sa.Column("source", sa.Text(), nullable=False),        # observed|registry|operator
            # NULL means "not yet confirmed against the endpoint's own /v1/models"
            sa.Column("verified_at", sa.Text(), nullable=True),
            sa.Column("first_seen", sa.Text(), nullable=True),
            sa.Column("last_seen", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.UniqueConstraint("endpoint_id", "served_id", name="uq_mi_deployment"),
        )

    if not _has_table("mi_deployment_releases"):
        op.create_table(
            "mi_deployment_releases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("deployment_id", sa.Integer(),
                      sa.ForeignKey("mi_deployments.deployment_id"), nullable=False),
            sa.Column("release_id", sa.Integer(),
                      sa.ForeignKey("mi_releases.release_id"), nullable=False),
            sa.Column("effective_from", sa.Text(), nullable=False),
            sa.Column("effective_to", sa.Text(), nullable=True),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("confidence", sa.Text(), nullable=False),     # high|medium|low
            sa.UniqueConstraint("deployment_id", "effective_from",
                                name="uq_mi_deployment_release"),
        )

    if not _has_table("mi_aliases"):
        op.create_table(
            "mi_aliases",
            sa.Column("id", sa.Integer(), primary_key=True),
            # NULL endpoint_id = "any endpoint" (a spelling that is unambiguous globally)
            sa.Column("endpoint_id", sa.Integer(),
                      sa.ForeignKey("mi_endpoints.endpoint_id"), nullable=True),
            sa.Column("alias", sa.Text(), nullable=False),
            sa.Column("model_id", sa.Integer(),
                      sa.ForeignKey("mi_models.model_id"), nullable=True),
            sa.Column("deployment_id", sa.Integer(),
                      sa.ForeignKey("mi_deployments.deployment_id"), nullable=True),
            sa.Column("source", sa.Text(), nullable=False),
            sa.Column("valid_from", sa.Text(), nullable=False),
            sa.Column("valid_to", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.UniqueConstraint("endpoint_id", "alias", "valid_from", name="uq_mi_alias"),
        )

    if not _has_table("mi_registry_versions"):
        op.create_table(
            "mi_registry_versions",
            sa.Column("version", sa.Integer(), primary_key=True),
            sa.Column("created_at", sa.Text(), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
        )

    for table, column in (("routing_decisions", "from_deployment_id"),
                          ("routing_decisions", "to_deployment_id"),
                          ("routing_decisions", "registry_version"),
                          ("requests", "deployment_id"),
                          ("requests", "release_id"),
                          ("requests", "registry_version")):
        if _has_table(table) and column not in _columns(table):
            op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))

    op.execute("CREATE INDEX IF NOT EXISTS ix_mi_aliases_alias ON mi_aliases (alias)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mi_deploy_served ON mi_deployments (served_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mi_dep_rel ON mi_deployment_releases (deployment_id)")


def downgrade() -> None:
    for table, column in (("routing_decisions", "registry_version"),
                          ("routing_decisions", "to_deployment_id"),
                          ("routing_decisions", "from_deployment_id"),
                          ("requests", "registry_version"),
                          ("requests", "release_id"),
                          ("requests", "deployment_id")):
        if _has_table(table) and column in _columns(table):
            op.drop_column(table, column)
    for table in TABLES:
        if _has_table(table):
            op.drop_table(table)
