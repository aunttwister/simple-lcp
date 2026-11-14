"""New poll-staged deployments have no canonical model yet: make model_id nullable.

Why
---
The poller stages a fresh served id as ``mi_deployments.status='new'`` with
``source='poll'`` — deliberately UNLINKED from ``mi_models`` until a human confirms
the identity (the whole point of the staging queue). The schema forced a
``model_id`` on every row, which would have demanded a fake canonical link at
stage time. A staged row is precisely a row whose canonical identity is unknown.

Shape
-----
Standard SQLite table rebuild preserving every constraint except the NOT NULL on
``model_id`` (PK, uq_mi_deployment, both FKs, defaults, ix_mi_deploy_served).
Reversible: the downgrade re-asserts NOT NULL and refuses (raises) if any NULL
rows would violate it.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None

_TABLE_NEW = """
CREATE TABLE mi_deployments_new (
    deployment_id INTEGER NOT NULL,
    endpoint_id INTEGER NOT NULL,
    model_id INTEGER,
    served_id TEXT NOT NULL,
    variant TEXT,
    quantisation TEXT,
    context_window INTEGER,
    status TEXT DEFAULT 'active' NOT NULL,
    source TEXT NOT NULL,
    verified_at TEXT,
    first_seen TEXT,
    last_seen TEXT,
    notes TEXT,
    PRIMARY KEY (deployment_id),
    CONSTRAINT uq_mi_deployment UNIQUE (endpoint_id, served_id),
    FOREIGN KEY(endpoint_id) REFERENCES mi_endpoints (endpoint_id),
    FOREIGN KEY(model_id) REFERENCES mi_models (model_id)
)
"""

_TABLE_STRICT = """
CREATE TABLE mi_deployments_new (
    deployment_id INTEGER NOT NULL,
    endpoint_id INTEGER NOT NULL,
    model_id INTEGER NOT NULL,
    served_id TEXT NOT NULL,
    variant TEXT,
    quantisation TEXT,
    context_window INTEGER,
    status TEXT DEFAULT 'active' NOT NULL,
    source TEXT NOT NULL,
    verified_at TEXT,
    first_seen TEXT,
    last_seen TEXT,
    notes TEXT,
    PRIMARY KEY (deployment_id),
    CONSTRAINT uq_mi_deployment UNIQUE (endpoint_id, served_id),
    FOREIGN KEY(endpoint_id) REFERENCES mi_endpoints (endpoint_id),
    FOREIGN KEY(model_id) REFERENCES mi_models (model_id)
)
"""

_COLS = ("deployment_id, endpoint_id, model_id, served_id, variant, quantisation,"
         " context_window, status, source, verified_at, first_seen, last_seen, notes")


def _finish_rebuild() -> None:
    op.execute("DROP TABLE mi_deployments")
    op.execute("ALTER TABLE mi_deployments_new RENAME TO mi_deployments")
    op.create_index("ix_mi_deploy_served", "mi_deployments", ["served_id"])


def upgrade() -> None:
    op.execute(_TABLE_NEW)
    op.execute(f"INSERT INTO mi_deployments_new ({_COLS}) SELECT {_COLS} FROM mi_deployments")
    _finish_rebuild()


def downgrade() -> None:
    # Refuse to re-assert NOT NULL while staged rows (model_id NULL) exist.
    nulls = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM mi_deployments WHERE model_id IS NULL")
    ).scalar()
    if nulls:
        raise RuntimeError(
            f"cannot downgrade: {nulls} staged deployments have no model_id; "
            "confirm or delete them first")
    op.execute(_TABLE_STRICT)
    op.execute(f"INSERT INTO mi_deployments_new ({_COLS}) SELECT {_COLS} FROM mi_deployments")
    _finish_rebuild()
