"""Dynamic per-user intent taxonomies.

Additive, nullable and reversible. Nothing existing is read differently, and with no taxonomy
configured or assigned anywhere, resolution returns the built-in label space — so behaviour is
byte-identical until an operator deliberately activates a taxonomy for a profile.

Why
---
The classifier shipped with ONE hard-coded label set (``TASK_EXEMPLARS``). The profiles are not one
user: measured traffic shows ``coder``/``blog``/``career`` contain no operator speech at all while
the labelled corpus is 88% ``l2``, so a single estate-wide taxonomy describes almost none of the
other profiles' traffic. A taxonomy therefore becomes data — owned by a profile, versioned, and
gradeable.

Shape
-----
* ``intent_taxonomies`` — one row per taxonomy. ``spec`` holds the label space (labels plus verbatim
  exemplars) and the act/subject projections; ``grade`` holds the measurement report. Kept as JSON
  rather than columns because the label space is a nested structure whose shape will change, and a
  rigid schema would make every taxonomy edit a migration.
* ``routing_decisions.taxonomy_id`` / ``taxonomy_version`` — which label space produced a decision.
  Without these, a decision logged under one taxonomy is indistinguishable from the same decision
  under another, and the historical log cannot be re-interpreted honestly.

The ``active`` gate is the point of the whole feature: a taxonomy may only be resolved when it is
``active``, and ``TaxonomyStore.activate()`` refuses without a grade whose negative control passed.
An ungraded taxonomy is inert — the estate must never accumulate classifiers nobody has measured.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Short ids, matching 025/026/027: an id that does not match the chain makes every alembic command
# raise KeyError before it can do anything, including the test fixtures that stamp a revision.
revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None

_NEW_COLUMNS = (
    ("taxonomy_id", sa.Text()),
    ("taxonomy_version", sa.Integer()),
)

_TAXONOMY_DDL = """
CREATE TABLE IF NOT EXISTS intent_taxonomies (
  id              TEXT PRIMARY KEY,
  owner_profile   TEXT,
  version         INTEGER NOT NULL DEFAULT 1,
  status          TEXT NOT NULL DEFAULT 'draft',
  display_name    TEXT,
  spec            TEXT NOT NULL,
  grade           TEXT,
  embedding_model TEXT NOT NULL DEFAULT 'BAAI/bge-small-en-v1.5',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
)
"""


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table: str) -> bool:
    try:
        return table in _inspector().get_table_names()
    except Exception:  # noqa: BLE001 — a pre-existing database may not be inspectable
        return False


def _column_exists(table: str, column: str) -> bool:
    try:
        return column in {c["name"] for c in _inspector().get_columns(table)}
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists("intent_taxonomies"):
        bind.exec_driver_sql(_TAXONOMY_DDL)
        bind.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_intent_taxonomies_owner "
            "ON intent_taxonomies(owner_profile)"
        )

    # Guarded like 020/027: a database built without routing_decisions must still upgrade cleanly.
    if _table_exists("routing_decisions"):
        for name, type_ in _NEW_COLUMNS:
            if not _column_exists("routing_decisions", name):
                op.add_column("routing_decisions", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    if _table_exists("routing_decisions"):
        for name, _ in _NEW_COLUMNS:
            if _column_exists("routing_decisions", name):
                op.drop_column("routing_decisions", name)
    # The table is dropped last: it holds no foreign keys into the decision log, so dropping it
    # cannot orphan a decision row.
    if _table_exists("intent_taxonomies"):
        op.drop_table("intent_taxonomies")
