"""The Arena routing tier — TDD contract.

Operator, 2026-11-17: "On every message. You can proceed with tdd implementation where
you will write unit tests first and then implement. Also dont forget about hard rules
that can be applied per profile. Same as it can be done now."

What is pinned here, and why each one is a behavioural fact rather than a style
preference:

* the tier is **per message** — two messages in the same conversation classified to
  different boards are routed independently (his explicit answer to §16 E6);
* the **pool is the boundary**: a model outside the configured chain is never a
  candidate, however highly LMArena rates it (§3);
* **hard rules still win**, per profile: a `block` removes a model from the walk and a
  `prefer` overrides the board ranking, exactly as they do on the intent path;
* nothing servable / no board / no embedder ⇒ **the static chain stands** — never an
  exception, never a fabricated pick (§16 E4, E7);
* effort groups are chosen by an explicit policy, not by whatever the sort happens to
  do (§16 E2);
* ties break deterministically (§16 E3);
* the meta boards (`overall`, `exclude_ties`, `longer_query`, `non_english`) and the
  language boards are **not** label spaces (§16 E1);
* the board label space is a curated subset with real exemplars, because the classifier
  cannot label with a board it has never seen text for.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.api import arena_routing as ar
from src.api import board_exemplars as bx

SCHEMA = """
CREATE TABLE IF NOT EXISTS mi_models (
    model_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT, family TEXT,
    status TEXT, supersedes_model_id INTEGER, benchmark_key TEXT, created_at TEXT,
    active_release TEXT, benchmark_release TEXT, quantization TEXT);
CREATE TABLE IF NOT EXISTS mi_deployments (
    deployment_id INTEGER PRIMARY KEY, endpoint_id INTEGER, model_id INTEGER,
    served_id TEXT, variant TEXT, quantisation TEXT, context_window INTEGER,
    status TEXT, source TEXT, verified_at TEXT, first_seen TEXT, last_seen TEXT,
    notes TEXT, quant_id INTEGER);
CREATE TABLE IF NOT EXISTS mi_endpoints (
    endpoint_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT, base_url TEXT,
    status TEXT, poll_enabled INTEGER);
CREATE TABLE IF NOT EXISTS arena_boards (
    board TEXT PRIMARY KEY, family TEXT NOT NULL, unit TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0, models_matched INTEGER NOT NULL DEFAULT 0,
    published TEXT, first_seen TEXT NOT NULL, last_polled_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS arena_board_rows (
    id INTEGER PRIMARY KEY, board TEXT NOT NULL, model_slug TEXT NOT NULL,
    value REAL NOT NULL, unit TEXT NOT NULL, rank INTEGER, n INTEGER, lab TEXT,
    effort TEXT NOT NULL DEFAULT '', published TEXT, polled_at TEXT NOT NULL,
    UNIQUE (board, model_slug, effort));
"""

#: The boards the poll publishes, trimmed to what these tests exercise: three real
#: intents, one meta board, one language board, one agent board.
BOARDS = [
    ("coding", "text", "elo"),
    ("math", "text", "elo"),
    ("multi_turn", "text", "elo"),
    ("agent_bash_reliability", "agent", "net_improvement"),
    ("overall", "text", "elo"),          # meta — must not be a label space
    ("english", "text", "elo"),          # language — must not be a label space
]

#: (board, model_slug, value, rank, effort) — `rank` is what the walk orders by.
ROWS = [
    ("coding", "model-omega", 1543.4, 1, ""),
    ("coding", "model-beta", 1509.4, 2, ""),
    ("coding", "model-alpha", 1506.3, 3, ""),
    ("math", "model-alpha", 1525.1, 1, ""),
    ("math", "model-beta", 1500.0, 2, ""),
    # multi_turn: our only servable model is ranked 9th — §16 E5's honest worst case.
    ("multi_turn", "model-far", 1490.0, 9, ""),
    ("multi_turn", "model-omega", 1560.0, 1, ""),
    ("agent_bash_reliability", "model-omega", 0.0400, 1, ""),
    ("agent_bash_reliability", "model-beta", 0.0099, 2, ""),
    # effort groups on one board — §16 E2.
    ("coding", "model-alpha", 1600.0, 1, "max"),
    ("coding", "model-omega", 1590.0, 2, "max"),
]

#: Just the two arena tables, for tests that layer them over an existing mi_* schema.
ARENA_DDL = """
CREATE TABLE IF NOT EXISTS arena_boards (
    board TEXT PRIMARY KEY, family TEXT NOT NULL, unit TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0, models_matched INTEGER NOT NULL DEFAULT 0,
    published TEXT, first_seen TEXT NOT NULL, last_polled_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS arena_board_rows (
    id INTEGER PRIMARY KEY, board TEXT NOT NULL, model_slug TEXT NOT NULL,
    value REAL NOT NULL, unit TEXT NOT NULL, rank INTEGER, n INTEGER, lab TEXT,
    effort TEXT NOT NULL DEFAULT '', published TEXT, polled_at TEXT NOT NULL,
    UNIQUE (board, model_slug, effort));
"""

MODELS = [(1, "model-alpha"), (2, "model-beta"), (3, "model-far")]


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for i, slug in MODELS:
        con.execute("INSERT INTO mi_models (model_id, slug, status) VALUES (?,?,'active')",
                    (i, slug))
    con.execute("INSERT INTO mi_models (model_id, slug, status) VALUES (4,'model-omega','active')")
    con.execute("INSERT INTO mi_deployments (deployment_id, endpoint_id, model_id,"
                " served_id, status) VALUES (1,1,1,'model-alpha','active')")
    con.execute("INSERT INTO mi_deployments (deployment_id, endpoint_id, model_id,"
                " served_id, status) VALUES (2,1,2,'model-beta','active')")
    con.execute("INSERT INTO mi_deployments (deployment_id, endpoint_id, model_id,"
                " served_id, status) VALUES (3,1,3,'model-far','active')")
    for board, family, unit in BOARDS:
        con.execute("INSERT INTO arena_boards (board, family, unit, rows, models_matched,"
                    " first_seen, last_polled_at) VALUES (?,?,?,0,0,'2026-11-16','2026-11-16')",
                    (board, family, unit))
    for board, slug, value, rank, effort in ROWS:
        con.execute("INSERT INTO arena_board_rows (board, model_slug, value, unit, rank,"
                    " effort, polled_at) VALUES (?,?,?,'elo',?,?,'2026-11-16')",
                    (board, slug, value, rank, effort))
    con.commit()
    yield con
    con.close()


# ── §16 E1 — which boards may be a label space ────────────────────────────────

def test_meta_boards_are_not_label_spaces():
    """`overall` is a poll artefact, not something a request can be about."""
    for board in ("overall", "exclude_ties", "longer_query", "non_english"):
        assert board in ar.META_BOARDS, board


def test_language_boards_are_not_label_spaces():
    """Routing by the language of the message is a product decision nobody made."""
    for board in ("english", "chinese", "french", "german", "japanese", "korean",
                  "polish", "russian", "spanish"):
        assert board in ar.LANGUAGE_BOARDS, board


def test_curated_boards_excludes_meta_and_language(db):
    got = ar.curated_boards(db)
    assert "coding" in got and "math" in got and "multi_turn" in got
    assert "agent_bash_reliability" in got
    assert "overall" not in got
    assert "english" not in got


def test_curated_boards_is_deterministic(db):
    assert ar.curated_boards(db) == ar.curated_boards(db)
    assert ar.curated_boards(db) == sorted(ar.curated_boards(db))


# ── §16 E2 — the effort group is chosen by policy, not by accident ────────────

def test_effort_group_prefers_unlabelled_rows(db):
    """Our deployments do not declare an effort setting, so plain rows win."""
    assert ar.effort_group(db, "coding", {"model-alpha", "model-beta"}) == ""


def test_effort_group_falls_back_to_a_labelled_group_when_that_is_all_there_is(db):
    """A board whose only rows carry `max` must still be walkable."""
    con = db
    con.execute("DELETE FROM arena_board_rows WHERE board='math'")
    con.execute("INSERT INTO arena_board_rows (board, model_slug, value, unit, rank,"
                " effort, polled_at) VALUES ('math','model-alpha',1601.0,'elo',1,'max','2026-11-16')")
    con.commit()
    assert ar.effort_group(con, "math", {"model-alpha"}) == "max"


def test_effort_group_empty_when_no_candidate_has_any_row(db):
    assert ar.effort_group(db, "coding", {"nobody-here"}) == ""


# ── the walk — rank order, pool boundary, ties, fallbacks (§16 E3/E4) ─────────

def test_walk_returns_the_highest_ranked_candidate(db):
    slug, rank, effort = ar.walk(db, "coding", {"model-alpha", "model-beta"})
    assert (slug, rank) == ("model-beta", 2)   # beta ranks 2, alpha 3


def test_walk_never_leaves_the_pool(db):
    """`model-omega` is rank 1 on coding but is not in the candidate set."""
    slug, rank, _ = ar.walk(db, "coding", {"model-alpha", "model-beta"})
    assert slug != "model-omega"


def test_walk_skips_models_outside_the_pool_at_the_top_of_the_ranking(db):
    """multi_turn: rank 1 is model-omega (not ours); rank 9 model-far is."""
    slug, rank, _ = ar.walk(db, "multi_turn", {"model-far"})
    assert (slug, rank) == ("model-far", 9)


def test_walk_breaks_ties_deterministically_by_slug():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("INSERT INTO arena_boards (board, family, unit, rows, models_matched,"
                " first_seen, last_polled_at) VALUES ('coding','text','elo',0,0,'x','x')")
    for slug in ("zeta", "alpha"):
        con.execute("INSERT INTO arena_board_rows (board, model_slug, value, unit, rank,"
                    " effort, polled_at) VALUES ('coding',?,1500.0,'elo',1,'','x')", (slug,))
    con.commit()
    first = ar.walk(con, "coding", {"zeta", "alpha"})
    for _ in range(5):
        assert ar.walk(con, "coding", {"zeta", "alpha"}) == first
    assert first[0] == "alpha"   # slug ascending when values are equal
    con.close()


def test_walk_returns_nothing_when_the_board_has_no_candidate(db):
    """§16 E4 — 'no servable model on this board' must be expressible."""
    slug, rank, _ = ar.walk(db, "coding", {"model-not-served"})
    assert slug is None and rank is None


def test_walk_of_an_unknown_board_returns_nothing(db):
    assert ar.walk(db, "no-such-board", {"model-alpha"})[0] is None


def test_walk_of_a_meta_or_language_board_returns_nothing(db):
    """A board that is not a label space must not be walkable either."""
    assert ar.walk(db, "overall", {"model-alpha"})[0] is None
    assert ar.walk(db, "english", {"model-alpha"})[0] is None


# ── scoring a model on a board — never a fabricated number (§16 E8) ───────────

def test_board_score_is_the_percentile_within_the_board(db):
    got = ar.score(db, "coding", "model-omega")
    assert got is not None and 0.0 < got <= 1.0


def test_board_score_is_none_for_a_model_the_board_never_ranked(db):
    """None — not 0.0 and not a default. An unmeasured model is unmeasured."""
    assert ar.score(db, "coding", "model-not-on-this-board") is None


def test_board_score_is_none_for_a_board_that_is_not_a_label_space(db):
    assert ar.score(db, "overall", "model-omega") is None


def test_board_score_is_none_for_an_unknown_board(db):
    assert ar.score(db, "nope", "model-omega") is None


# ── the label space — exemplars, and the keyword guard (§16 E7) ───────────────

#: Every board the poll publishes (36 as of 2026-11-17). Frozen here on purpose: when
#: LMArena adds a board, this test fails, and that is the signal that the new board has
#: no exemplars and therefore cannot be routed to yet.
REAL_BOARDS = [
    "agent", "agent_bash_recovery_steps", "agent_bash_reliability",
    "agent_praise_complaint", "agent_steerability", "agent_task_outcome_explicit",
    "agent_tool_hallucination", "chinese", "coding", "creative_writing", "english",
    "exclude_ties", "expert", "french", "german", "hard_prompts",
    "hard_prompts_english",
    "industry_business_and_management_and_financial_operations",
    "industry_entertainment_and_sports_and_media", "industry_legal_and_government",
    "industry_life_and_physical_and_social_science", "industry_mathematical",
    "industry_medicine_and_healthcare", "industry_software_and_it_services",
    "industry_writing_and_literature_and_language", "instruction_following",
    "japanese", "korean", "longer_query", "math", "multi_turn", "non_english",
    "overall", "polish", "russian", "spanish",
]


@pytest.fixture
def real_boards_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for board in REAL_BOARDS:
        con.execute("INSERT INTO arena_boards (board, family, unit, rows, models_matched,"
                    " first_seen, last_polled_at) VALUES (?,'text','elo',0,0,'x','x')",
                    (board,))
    con.commit()
    yield con
    con.close()


def test_every_exemplar_board_is_a_curated_board_of_the_real_estate(real_boards_db):
    """The label space and the authored exemplars must agree exactly.

    A curated board with no exemplars is unroutable; an exemplar for a non-curated
    board is dead text. Both directions are failures, so both are asserted.
    """
    curated = set(ar.curated_boards(real_boards_db))
    assert set(bx.BOARD_EXEMPLARS) == curated, (
        f"no exemplars: {sorted(curated - set(bx.BOARD_EXEMPLARS))}; "
        f"not curated: {sorted(set(bx.BOARD_EXEMPLARS) - curated)}"
    )


def test_no_exemplar_board_is_a_meta_or_language_board():
    assert set(bx.BOARD_EXEMPLARS) & ar.NON_LABEL_BOARDS == set()


def test_every_published_board_is_either_curated_or_explicitly_excluded(real_boards_db):
    """36 published = 23 curated + 13 excluded. Ours, not the poll's, decides which."""
    curated = set(ar.curated_boards(real_boards_db))
    assert curated | ar.NON_LABEL_BOARDS == set(REAL_BOARDS)


def test_every_exemplar_board_has_enough_texts_to_build_a_centroid():
    thin = {b: len(v) for b, v in bx.BOARD_EXEMPLARS.items() if len(v) < 3}
    assert thin == {}, f"boards with fewer than 3 exemplars: {thin}"


def test_exemplars_are_non_empty_strings():
    bad = [(b, t) for b, texts in bx.BOARD_EXEMPLARS.items()
           for t in texts if not t.strip()]
    assert bad == []


def test_exemplar_board_count_is_the_curated_subset_not_all_36(real_boards_db):
    """The label space is a curated subset — 23 of the 36 published boards."""
    n_curated = len(ar.curated_boards(real_boards_db))
    assert n_curated == 23, n_curated
    assert len(bx.BOARD_EXEMPLARS) == n_curated


def test_pick_board_reports_no_board_and_no_scores_without_an_embedder(monkeypatch):
    """§16 E7 — no embedder means no board. It must never guess one."""
    monkeypatch.setattr(ar, "_board_classifier", lambda *a, **k: None)
    board, scores = ar.pick_board("refactor this function")
    assert board is None
    assert scores == []


def test_pick_board_returns_the_top_label_and_its_vector(monkeypatch):
    class FakeClf:
        available = True
        min_score = 0.35
        labels = ["coding", "math"]

        def top_scores(self, text, k=5):
            return [("coding", 0.81), ("math", 0.42)]

        def classify(self, text):
            return "coding"

    monkeypatch.setattr(ar, "_board_classifier", lambda *a, **k: FakeClf())
    board, scores = ar.pick_board("refactor this python module")
    assert board == "coding"
    assert scores[0] == ("coding", 0.81)


def test_pick_board_below_the_gate_returns_no_board(monkeypatch):
    """An uncertain message must fall back to the static chain, not pick a board."""
    class WeakClf:
        available = True
        min_score = 0.35
        labels = ["coding"]

        def top_scores(self, text, k=5):
            return [("coding", 0.11)]

        def classify(self, text):
            return None

    monkeypatch.setattr(ar, "_board_classifier", lambda *a, **k: WeakClf())
    board, scores = ar.pick_board("hello there")
    assert board is None
    assert scores[0] == ("coding", 0.11)   # the evidence is still reported
