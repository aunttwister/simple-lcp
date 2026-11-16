"""arena board catalogue + the operator's taxonomy map

The operator's correction (2026-11-15), verbatim:

  "you might have mapped my taxonomies to lmarena taxonomies, however thats not
   gonna work in real user experience. You need to enable user to map taxonomies.
   What we should: on the first poll latest data persist all lmarena taxonomies in
   sqlite. Then enable the user to map their taxonomy to lm arenas in manage
   taxonomies."

The mapping had been 14 entries hardcoded in `src/api/arena_poll.py`
(`TEXT_BOARDS` / `AGENT_BOARDS`). That is the wrong shape: the AXES are the
operator's, the BOARDS are LMArena's, and which board measures which axis is a
judgement only he should make. Worse, the poll reads the whole text file (29
categories in one read) and then threw 22 of them away — there was no table to
hold them.

So this migration adds the data model the product needs:

  arena_boards       the catalogue of every board LMArena publishes (36 today:
                     29 text categories + 7 agent tournaments), with its family,
                     unit, row count and publish date.
  arena_board_rows   every rating LMArena published, for the models we serve —
                     persisted BEFORE any mapping is consulted, so adding a
                     mapping shows a table immediately with no re-poll.
  taxonomy_map       the operator's mapping, many-to-many in BOTH directions:
                     one board can feed several axes (coding is the same
                     measurement for code_generation, debugging, unit_tests) and
                     one axis can be fed by several boards (reasoning_chain from
                     math + hard_prompts_english + expert).

The 14 mappings that were hardcoded are seeded with `is_default = 1`, so the page
looks identical the moment this lands and the operator takes ownership by editing
rather than by rebuilding from blank. `is_default` is only a provenance marker —
every row is editable and deletable.

Revision ID: 040
Revises: 039
"""
from alembic import op
import sqlalchemy as sa

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None

#: (taxonomy, board, note) — the 14 that were hardcoded, in AXIS_ORDER.
DEFAULT_MAP = [
    ("code_generation", "coding", "coding tournament"),
    ("debugging", "coding", "coding tournament"),
    ("unit_tests", "coding", "coding tournament"),
    ("reasoning_chain", "math", "math tournament"),
    ("reasoning_chain", "hard_prompts_english", "hard English prompts"),
    ("reasoning_chain", "expert", "expert-level prompts"),
    ("planning", "instruction_following", "instruction following"),
    ("casual_chat", "multi_turn", "multi-turn conversation"),
    ("casual_chat", "creative_writing", "creative writing"),
    ("agentic_multi_step", "agent", "agent arena"),
    ("agentic_multi_step", "agent_task_outcome_explicit", "agent task outcome"),
    ("planning", "agent_steerability", "agent steerability"),
    ("research_deep", "agent_tool_hallucination", "tool hallucination (lower is better)"),
    ("debugging", "agent_bash_reliability", "bash reliability"),
    ("debugging", "agent_bash_recovery_steps", "bash recovery"),
    ("casual_chat", "agent_praise_complaint", "praise vs complaint"),
]


def upgrade() -> None:
    op.create_table(
        "arena_boards",
        sa.Column("board", sa.String(), primary_key=True),
        sa.Column("family", sa.String(), nullable=False),      # text | agent
        sa.Column("unit", sa.String(), nullable=False),        # elo | net_improvement
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("models_matched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published", sa.String(), nullable=True),
        sa.Column("first_seen", sa.String(), nullable=False),
        sa.Column("last_polled_at", sa.String(), nullable=False),
        sa.CheckConstraint("family IN ('text','agent')", name="ck_arena_board_family"),
        sa.CheckConstraint("unit IN ('elo','net_improvement')", name="ck_arena_board_unit"),
    )

    op.create_table(
        "arena_board_rows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("board", sa.String(), nullable=False),
        sa.Column("model_slug", sa.String(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("n", sa.Integer(), nullable=True),
        sa.Column("lab", sa.String(), nullable=True),
        sa.Column("effort", sa.String(), nullable=False, server_default=""),
        sa.Column("published", sa.String(), nullable=True),
        sa.Column("polled_at", sa.String(), nullable=False),
        # '': SQLite treats NULLs as DISTINCT in a unique index, so a NULL effort
        # would never match its own ON CONFLICT and every re-poll would INSERT a
        # duplicate — the same defect 039 fixed for model_ratings.
        sa.ForeignKeyConstraint(["board"], ["arena_boards.board"], ondelete="CASCADE"),
        sa.UniqueConstraint("board", "model_slug", "effort", name="uq_arena_board_row"),
    )
    op.create_index("ix_arena_board_rows_slug", "arena_board_rows", ["model_slug"])
    op.create_index("ix_arena_board_rows_board", "arena_board_rows", ["board"])

    op.create_table(
        "taxonomy_map",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("taxonomy", sa.String(), nullable=False),
        sa.Column("board", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("is_default", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["board"], ["arena_boards.board"], ondelete="CASCADE"),
        sa.UniqueConstraint("taxonomy", "board", name="uq_taxonomy_map"),
        # A `custom:` board is the operator's own rating with no upstream, so a
        # poll can never write one and a mapping can never target one. Enforced in
        # the schema, not only in the writer.
        sa.CheckConstraint("taxonomy NOT LIKE 'custom:%'", name="ck_taxonomy_map_not_custom"),
    )
    op.create_index("ix_taxonomy_map_taxonomy", "taxonomy_map", ["taxonomy"])
    op.create_index("ix_taxonomy_map_board", "taxonomy_map", ["board"])

    # Seed the 14 that were hardcoded, so day one is a no-op for the operator.
    # `arena_boards` rows are NOT seeded: a mapping needs a board that exists, and
    # the boards are created by the first poll that reads them. Inserting a
    # placeholder board here would describe a board whose rows we do not have —
    # exactly the "invented data" this design avoids. Instead the seed is applied by
    # the poll on its first run (see `model_ratings.seed_default_map`), which is
    # also when `arena_boards` gains its 36 rows.
    #
    # What THIS migration does seed is the axis ordering, so the tab strip keeps
    # the routing order it has today instead of falling back to alphabetical.
    conn = op.get_bind()
    rows = [
        ("code_generation", 0), ("reasoning_chain", 10), ("planning", 20),
        ("casual_chat", 30), ("agentic_multi_step", 40), ("debugging", 50),
        ("unit_tests", 60), ("research_deep", 70),
    ]
    for tax, order in rows:
        conn.execute(sa.text(
            "INSERT INTO rating_taxonomies (taxonomy, kind, label, unit, note,"
            " created_at, updated_at) VALUES (:t, 'arena', :l, NULL, :n,"
            " :now, :now) ON CONFLICT (taxonomy) DO UPDATE SET"
            " note = COALESCE(rating_taxonomies.note, :n)"),
            {"t": tax, "l": tax, "n": f"axis-order:{order}",
             "now": "2026-11-15T00:00:00+00:00"})


def downgrade() -> None:
    op.drop_index("ix_taxonomy_map_board", table_name="taxonomy_map")
    op.drop_index("ix_taxonomy_map_taxonomy", table_name="taxonomy_map")
    op.drop_table("taxonomy_map")
    op.drop_index("ix_arena_board_rows_board", table_name="arena_board_rows")
    op.drop_index("ix_arena_board_rows_slug", table_name="arena_board_rows")
    op.drop_table("arena_board_rows")
    op.drop_table("arena_boards")
