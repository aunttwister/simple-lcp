"""Rating taxonomies + poll staging for the leaderboard store.

Three things, all in service of the 2026-11-15 operator decisions:

1. **A NULL-variant defect in 038, fixed.** ``model_ratings`` is unique on
   ``(model_slug, taxonomy, source, board, effort)`` — but SQLite treats NULLs as
   DISTINCT in a unique index, so every row whose effort or board was NULL never
   matched its own ON CONFLICT clause and a re-poll INSERTED a duplicate instead
   of refreshing. Effort is NULL for every base model (only the ``(High)``/
   ``(Max)`` variants carry one), so this would have doubled the board on the
   second poll. The columns are normalised to ``''`` here and the writer coerces,
   so the uniqueness the schema declares is the uniqueness that actually holds.

2. **``rating_taxonomies``** — the tab registry. Arena axes are registered on
   every poll; an operator-defined taxonomy is registered when it is DEFINED, so
   a board can exist (and be visible as a tab) before it has a single row.

3. **``rating_poll_staging``** — poll -> diff -> accept. A poll stages, it does
   not apply: the operator reviews old -> new (with BOTH dates) and ticks what
   becomes the new baseline. Same shape as the model-intel poll's
   ``status='new'`` deployments — a machine reading is staged, never written
   straight over the human-owned value.

Operator-defined taxonomies are namespaced ``custom:<slug>``. Arena rows only
ever carry an axis name, so a poll cannot overwrite a custom board and an import
cannot pollute an arena one.

Revision ID: 039
Revises: 038
"""
from alembic import op
import sqlalchemy as sa

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. make the declared uniqueness real (see module docstring, point 1)
    op.execute("UPDATE model_ratings SET effort = '' WHERE effort IS NULL")
    op.execute("UPDATE model_ratings SET board = '' WHERE board IS NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_model_ratings_unit"
               " ON model_ratings (taxonomy, unit)")

    # 2. the tab registry
    op.create_table(
        "rating_taxonomies",
        sa.Column("taxonomy", sa.String, primary_key=True),
        sa.Column("kind", sa.String, nullable=False),          # 'arena' | 'custom'
        sa.Column("label", sa.String),
        sa.Column("unit", sa.String),
        sa.Column("note", sa.String),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("updated_at", sa.String, nullable=False),
    )

    # 3. the staged poll
    op.create_table(
        "rating_poll_staging",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("poll_id", sa.String, nullable=False),
        sa.Column("model_slug", sa.String, nullable=False),
        sa.Column("taxonomy", sa.String, nullable=False),
        sa.Column("unit", sa.String, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("old_value", sa.Float),
        sa.Column("percentile", sa.Float),
        sa.Column("rank", sa.Integer),
        sa.Column("n", sa.Integer),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("board", sa.String, nullable=False, server_default=""),
        sa.Column("lab", sa.String),
        sa.Column("effort", sa.String, nullable=False, server_default=""),
        sa.Column("basis", sa.String),
        sa.Column("published", sa.String),
        sa.Column("old_published", sa.String),
        sa.Column("old_updated_at", sa.String),
        sa.Column("polled_at", sa.String, nullable=False),
        sa.UniqueConstraint("model_slug", "taxonomy", "unit", "board", "effort",
                            name="uq_rating_poll_identity"),
    )
    op.create_index("ix_rating_poll_poll_id", "rating_poll_staging", ["poll_id"])
    op.create_index("ix_rating_poll_taxonomy", "rating_poll_staging", ["taxonomy"])

    # seed the tab registry from whatever the store already holds
    op.execute("""
        INSERT OR IGNORE INTO rating_taxonomies
            (taxonomy, kind, label, unit, created_at, updated_at)
        SELECT DISTINCT taxonomy,
               CASE WHEN taxonomy LIKE 'custom:%' THEN 'custom' ELSE 'arena' END,
               taxonomy, unit,
               COALESCE(MIN(first_seen), ''), COALESCE(MAX(updated_at), '')
        FROM model_ratings GROUP BY taxonomy
    """)


def downgrade() -> None:
    op.drop_index("ix_rating_poll_taxonomy", table_name="rating_poll_staging")
    op.drop_index("ix_rating_poll_poll_id", table_name="rating_poll_staging")
    op.drop_table("rating_poll_staging")
    op.drop_table("rating_taxonomies")
    op.drop_index("ix_model_ratings_unit", table_name="model_ratings")
