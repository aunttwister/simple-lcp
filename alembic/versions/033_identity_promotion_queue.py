"""Identity promotion queue table (model-intel canonicalisation).

Why
---
``model_capabilities`` can only be joined by a spelling that ``capabilities_for()``
searches — a canonical slug, declared alias, served id, or benchmark key. Observations
that reach none of those (the long tail of arena names and new served ids) have nowhere
legitimate to live until a human links them. This table is that holding area, so the
capability table stops accumulating unreachable rows.

Additive and reversible: one new table, no existing reader or writer touched.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mi_identity_queue",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("raw", sa.Text(), nullable=False),          # spelling as observed
        sa.Column("norm", sa.Text(), nullable=False),         # normalised dedupe key
        sa.Column("source", sa.Text(), nullable=False),       # lmarena | provider_catalog
        sa.Column("kind", sa.Text(), nullable=False),         # capability_name | served_id
        sa.Column("evidence", sa.Text()),                     # JSON: counts, categories, …
        sa.Column("suggested_slug", sa.Text()),               # a GUESS — never auto-applied
        sa.Column("suggested_by", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("first_seen", sa.Text(), nullable=False),
        sa.Column("last_seen", sa.Text(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("decided_at", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.UniqueConstraint("norm", "source", name="uq_mi_identity_norm_source"),
    )
    op.create_index("ix_mi_identity_status", "mi_identity_queue", ["status", "occurrences"])


def downgrade() -> None:
    op.drop_index("ix_mi_identity_status", table_name="mi_identity_queue")
    op.drop_table("mi_identity_queue")
