"""Model-intel poll: fresh provider model lists, staged for careful identity.

Why
---
The model-intel schema (026) seeded ``mi_models``/``mi_deployments``/``mi_aliases`` from the
registry on 2026-10-30 and nothing has refreshed it since: ``last_seen`` is frozen while the
``requests`` log keeps drifting (four spellings of the same DeepSeek model, 2,394 literal
``unknown`` rows). The providers all answer a live catalog — bearer ``/v1/models`` for
local-sparks and deepseek, a public catalog GET for commandcode — so freshness is a poll away.

Shape
-----
* ``mi_polls`` — one row per (endpoint, poll attempt): status, model count, the raw id list,
  duration. The audit trail for every freshness claim the UI makes.
* ``mi_deployments`` gains ``status='new'`` / ``'stale_candidate'`` states written by the poller.
  A ``new`` row is NEVER read by ``model_identity.resolve`` (which only honours ``active``), so
  poll output cannot touch routing until a human confirms the identity. A vanished model becomes
  ``stale_candidate`` — never auto-retired, because an endpoint may hide a model temporarily.

Additive and reversible: new table plus status values no existing reader distinguishes.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mi_polls",
        sa.Column("poll_id", sa.Integer(), primary_key=True),
        sa.Column("endpoint_id", sa.Integer(), nullable=False),
        sa.Column("polled_at", sa.String(40), nullable=False),
        sa.Column("ok", sa.Integer(), nullable=False),  # 1 = HTTP success
        sa.Column("http_status", sa.Integer()),
        sa.Column("model_count", sa.Integer()),
        sa.Column("model_ids_json", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("duration_ms", sa.Integer()),
    )
    op.create_index("ix_mi_polls_endpoint_time", "mi_polls", ["endpoint_id", "polled_at"])
    # New deployments discovered by a poll are staged, not canonical.
    op.execute("UPDATE mi_deployments SET status='active' WHERE status IS NULL")


def downgrade() -> None:
    op.drop_index("ix_mi_polls_endpoint_time", table_name="mi_polls")
    op.drop_table("mi_polls")
    # 'new'/'stale_candidate' rows came only from polls; with the poll log gone they have no
    # provenance, so they are removed rather than left as unexplained staging rows.
    op.execute("DELETE FROM mi_deployments WHERE status IN ('new','stale_candidate')")
