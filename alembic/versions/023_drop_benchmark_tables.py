"""drop the benchmark measurement tables

Revision ID: 023
Revises: 022

R13: benchmarking a model inside LCP is the wrong tool, so the in-app runner and
its storage go. Three tables were written only by that runner:

* ``capability_metrics`` — per-run metric rows (496 live rows)
* ``model_capability_subtasks`` — the per-subtask breakdown parsed from
  LiveBench's ``all_tasks.csv`` (434 live rows)
* ``benchmark_runs`` — the run queue/ledger (30 live rows)

``model_capabilities`` is deliberately NOT dropped: CapabilityRouter reads it —
it is the routing matrix. Its rows now come from the bundled declared matrix
(``src/api/data/declared_capabilities.json``) plus the manual endpoint, not from
a benchmark run.

Each drop is guarded so the migration is safe on a database that never had one
of the tables (a fresh install, or a DB that ran an older revision set).
"""

from alembic import op
import sqlalchemy as sa


revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


_TABLES = ("capability_metrics", "model_capability_subtasks", "benchmark_runs")


def _has_table(name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in insp.get_table_names()


def upgrade() -> None:
    for table in _TABLES:
        if _has_table(table):
            op.drop_table(table)


def downgrade() -> None:
    """Recreate the three tables in their pre-M8 shape.

    The columns mirror the models as they existed before the drop, so a
    downgrade restores a schema the old code can write to. Row data is gone —
    this restores structure, not content.
    """
    if not _has_table("capability_metrics"):
        op.create_table(
            "capability_metrics",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.Integer(), nullable=True),
            sa.Column("model", sa.String(), nullable=True),
            sa.Column("metric", sa.String(), nullable=True),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
    if not _has_table("model_capability_subtasks"):
        op.create_table(
            "model_capability_subtasks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("model", sa.String(), nullable=True),
            sa.Column("category", sa.String(), nullable=True),
            sa.Column("task", sa.String(), nullable=True),
            sa.Column("score", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
    if not _has_table("benchmark_runs"):
        op.create_table(
            "benchmark_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("target_kind", sa.String(), nullable=True),
            sa.Column("target_json", sa.Text(), nullable=True),
            sa.Column("status", sa.String(), nullable=True),
            sa.Column("progress", sa.Float(), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("log_path", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
        )
