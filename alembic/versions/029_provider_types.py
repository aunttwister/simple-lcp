"""Hard provider types — a CHECK-constrained table for the provider-type axis.

The provider `type` was a free-text string inside the ``gateway_config:providers``
JSON blob. That is not a type: the UI branches its whole per-provider panel on it,
and the vendor plugin registry keys on it, so an unknown/typo value silently falls
back to name inference instead of failing loudly. This table makes the type a *type*:

* the DB itself refuses a value outside ``('commandcode','opencode','deepseek','local')``;
* the write path can only store one of the four;
* every layer (UI, registry, alert policy) reads the same row.

``local`` is a first-class type: a self-hosted engine has no billing API and renders
its own panel, so it must be a real type, not an inference afterthought.

Revision ID: 029
Revises: 028
Create Date: 2026-11-04

The store + resolver land with the code; this migration only shapes the table.
Backfill of existing providers is a runtime concern (the store writes inferred
rows on read); the migration stays a pure, idempotent DDL change.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "029"
down_revision: str = "028"
branch_labels = None
depends_on = None

_TYPES = ("commandcode", "opencode", "deepseek", "local")


def _table_exists(table: str) -> bool:
    try:
        return table in sa.inspect(op.get_bind()).get_table_names()
    except Exception:  # noqa: BLE001 — a pre-existing DB may not be inspectable
        return False


def upgrade() -> None:
    if _table_exists("provider_types"):
        return
    op.create_table(
        "provider_types",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(), nullable=False, unique=True, index=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            f"type IN {tuple(_TYPES)}",
            name="ck_provider_types_type",
        ),
    )


def downgrade() -> None:
    if _table_exists("provider_types"):
        op.drop_table("provider_types")