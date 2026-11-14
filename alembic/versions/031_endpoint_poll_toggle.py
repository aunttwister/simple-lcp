"""Per-endpoint poll toggle: polling latest info is OFF until the user opts in.

Why
---
The operator's rule (CUR model-intel redesign): "if user edited values it will not be
polled unless user checks the toggle that enables polling latest info". ``mi_polls``
(030) gave the poller an audit trail; this gives the user the switch. Every endpoint
defaults to ``poll_enabled=0`` — a fresh deployment polls nothing until someone
deliberately turns the endpoint on.

Additive and reversible: one nullable-default column no existing reader distinguishes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mi_endpoints",
        sa.Column("poll_enabled", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("mi_endpoints", "poll_enabled")
