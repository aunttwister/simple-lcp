"""Model ratings: a leaderboard store that is NOT capability accuracy.

Operator decision 2026-11-15: give up on accuracy as the leaderboard's basis.
Leaderboards show POSITION - Elo (text boards) or net improvement (agent boards)
- plus a percentile derived from rank. This table is the home for that, so
`model_capabilities` keeps its meaning (measured accuracy, the grading input) and
nothing here can leak into grading or routing.

Revision ID: 038
Revises: 037
"""
from alembic import op
import sqlalchemy as sa

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_ratings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("model_slug", sa.String, nullable=False),
        sa.Column("taxonomy", sa.String, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        # unit is explicit because the two board families are NOT comparable:
        # text boards publish Elo, agent boards publish a net improvement.
        sa.Column("unit", sa.String, nullable=False),
        sa.Column("percentile", sa.Float),
        sa.Column("rank", sa.Integer),
        sa.Column("n", sa.Integer),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("board", sa.String),
        sa.Column("lab", sa.String),
        # R2=C: reasoning effort is a DIMENSION of a rating row, not a new model.
        sa.Column("effort", sa.String),
        sa.Column("basis", sa.String),
        sa.Column("published", sa.String),
        sa.Column("first_seen", sa.String),
        sa.Column("updated_at", sa.String, nullable=False),
        sa.UniqueConstraint("model_slug", "taxonomy", "source", "board", "effort",
                            name="uq_model_rating_identity"),
    )
    op.create_index("ix_model_ratings_taxonomy", "model_ratings", ["taxonomy"])
    op.create_index("ix_model_ratings_slug", "model_ratings", ["model_slug"])


def downgrade() -> None:
    op.drop_index("ix_model_ratings_slug", table_name="model_ratings")
    op.drop_index("ix_model_ratings_taxonomy", table_name="model_ratings")
    op.drop_table("model_ratings")
