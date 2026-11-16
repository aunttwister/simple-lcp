"""The operator's TAXONOMY MAPPING — persist every board, then let him map.

Operator, 2026-11-15: "you might have mapped my taxonomies to lmarena taxonomies,
however thats not gonna work in real user experience. You need to enable user to
map taxonomies. What we should: on the first poll latest data persist all lmarena
taxonomies in sqlite. Then enable the user to map their taxonomy to lm arenas in
manage taxonomies."

What is pinned here, and why each one is a behavioural fact rather than a style
preference:

* **every** board the poll fetched is stored, including the 22 that no axis uses —
  the mapping is impossible otherwise (there would be nothing to map);
* a new mapping publishes immediately from the STORED rows, with no re-poll;
* unmapping deletes exactly the rows that board produced and leaves the rest;
* `custom:` can never be a mapping target (his own rating, no upstream);
* the 14 former-hardcoded pairs are seeded once and are editable — deleting one
  must not bring it back on the next poll;
* a mapping the operator deleted changes the leaderboard on the next read, because
  the map — not a constant in Python — decides what a tab contains.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.api import arena_poll, arena_taxonomy as at, model_ratings as mr

SCHEMA = """
CREATE TABLE mi_models (
    model_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT, family TEXT,
    status TEXT, supersedes_model_id INTEGER, benchmark_key TEXT, created_at TEXT,
    active_release TEXT, benchmark_release TEXT, quantization TEXT);
CREATE TABLE mi_deployments (
    deployment_id INTEGER PRIMARY KEY, endpoint_id INTEGER, model_id INTEGER,
    served_id TEXT, variant TEXT, quantisation TEXT, context_window INTEGER,
    status TEXT, source TEXT, verified_at TEXT, first_seen TEXT, last_seen TEXT,
    notes TEXT, quant_id INTEGER);
CREATE TABLE mi_model_names (
    id INTEGER PRIMARY KEY, model_id INTEGER, source_id INTEGER, name TEXT,
    is_primary INTEGER, confidence TEXT, evidence TEXT, valid_from TEXT,
    valid_to TEXT, first_seen TEXT, last_seen TEXT, verified_at TEXT, note TEXT);
CREATE TABLE mi_endpoints (
    endpoint_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT, base_url TEXT,
    status TEXT, poll_enabled INTEGER);
CREATE TABLE mi_sources (
    source_id INTEGER PRIMARY KEY, slug TEXT, kind TEXT, convention TEXT);
CREATE TABLE arena_boards (
    board TEXT PRIMARY KEY, family TEXT NOT NULL, unit TEXT NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0, models_matched INTEGER NOT NULL DEFAULT 0,
    published TEXT, first_seen TEXT NOT NULL, last_polled_at TEXT NOT NULL);
CREATE TABLE arena_board_rows (
    id INTEGER PRIMARY KEY, board TEXT NOT NULL, model_slug TEXT NOT NULL,
    value REAL NOT NULL, unit TEXT NOT NULL, rank INTEGER, n INTEGER, lab TEXT,
    effort TEXT NOT NULL DEFAULT '', published TEXT, polled_at TEXT NOT NULL,
    UNIQUE (board, model_slug, effort));
CREATE TABLE taxonomy_map (
    id INTEGER PRIMARY KEY, taxonomy TEXT NOT NULL, board TEXT NOT NULL,
    note TEXT, is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (taxonomy, board));
CREATE TABLE model_ratings (
    id INTEGER PRIMARY KEY, model_slug TEXT NOT NULL, taxonomy TEXT NOT NULL,
    value REAL NOT NULL, unit TEXT NOT NULL, percentile REAL, rank INTEGER,
    n INTEGER, source TEXT NOT NULL, board TEXT, lab TEXT, effort TEXT,
    basis TEXT, published TEXT, first_seen TEXT, updated_at TEXT NOT NULL,
    UNIQUE (model_slug, taxonomy, source, board, effort));
CREATE TABLE rating_taxonomies (
    taxonomy TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT, unit TEXT,
    note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE rating_poll_staging (
    id INTEGER PRIMARY KEY, poll_id TEXT NOT NULL, model_slug TEXT NOT NULL,
    taxonomy TEXT NOT NULL, unit TEXT NOT NULL, value REAL NOT NULL,
    old_value REAL, percentile REAL, rank INTEGER, n INTEGER,
    source TEXT NOT NULL, board TEXT NOT NULL DEFAULT '',
    lab TEXT, effort TEXT NOT NULL DEFAULT '', basis TEXT, published TEXT,
    old_published TEXT, old_updated_at TEXT, polled_at TEXT NOT NULL,
    UNIQUE (model_slug, taxonomy, unit, board, effort));
"""

MODELS = [
    (1, "glm-5.3-flash", "zai"),
    (2, "claude-opus-5", "anthropic"),
    (3, "kimi-k3", "moonshot"),
]


def _row(name, rating, rank, org, published="2026-11-11"):
    return {"model_name": name, "organization": org, "rating": rating, "score": rating,
            "rank": rank, "vote_count": 10, "published": published}


#: Two text categories, one of which (`english`) is one of the 22 nothing mapped
#: before — the case the operator called out. One agent board, for the family split.
BOARDS = {
    "coding": [_row("glm-5.3-flash", 1509.4, 16, "zai"),
               _row("claude-opus-5", 1543.4, 2, "anthropic"),
               _row("kimi-k3", 1506.3, 18, "moonshot")],
    "english": [_row("glm-5.3-flash", 1500.0, 40, "zai"),
                _row("claude-opus-5", 1560.0, 1, "anthropic")],
    "agent_bash_reliability": [_row("glm-5.3-flash", 0.0099, 2, "zai"),
                               _row("claude-opus-5", 0.0400, 1, "anthropic")],
}


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("INSERT INTO mi_endpoints (endpoint_id, slug, status, poll_enabled)"
                " VALUES (1,'commandcode','active',1)")
    for mid, slug, lab in MODELS:
        con.execute("INSERT INTO mi_models (model_id, slug, family, status, created_at)"
                    " VALUES (?,?,?,'active','2026-10-30T00:00:00Z')", (mid, slug, lab))
        con.execute("INSERT INTO mi_deployments (deployment_id, endpoint_id, model_id,"
                    " served_id, status, source) VALUES (?,1,?,?,'active','registry')",
                    (mid, mid, slug))
    con.execute("INSERT INTO mi_sources (source_id, slug, kind) VALUES (3,'lmarena','benchmark_arena')")
    con.commit()
    return con


# ── 1. persist EVERYTHING first ─────────────────────────────────────────────

def test_every_fetched_board_is_stored_including_the_unmapped_ones(db):
    """The 22 boards nothing used to claim must exist before they can be mapped."""
    out = at.store_boards(db, BOARDS)
    assert out["boards"] == 3
    boards = {r[0] for r in db.execute("SELECT board FROM arena_boards")}
    assert boards == {"coding", "english", "agent_bash_reliability"}
    # `english` is one of the 22 the old hardcoded table discarded
    assert "english" in boards


def test_every_published_row_for_a_served_model_is_stored(db):
    at.store_boards(db, BOARDS)
    rows = db.execute("SELECT board, model_slug, value FROM arena_board_rows"
                      " ORDER BY board, model_slug").fetchall()
    # 3 + 2 + 2 served models across the three boards
    assert len(rows) == 7
    assert ("english", "claude-opus-5", 1560.0) in [tuple(r) for r in rows]


def test_a_model_we_cannot_place_is_not_stored(db):
    boards = {"coding": [_row("Some Model We Do Not Serve", 1400.0, 300, "nobody")]}
    out = at.store_boards(db, boards)
    assert db.execute("SELECT COUNT(*) FROM arena_board_rows").fetchone()[0] == 0
    assert out["unresolved"] == 1


def test_the_board_catalogue_carries_family_unit_and_counts(db):
    at.store_boards(db, BOARDS)
    b = {r["board"]: r for r in db.execute("SELECT * FROM arena_boards")}
    assert (b["coding"]["family"], b["coding"]["unit"]) == ("text", "elo")
    assert (b["agent_bash_reliability"]["family"],
            b["agent_bash_reliability"]["unit"]) == ("agent", "net_improvement")
    assert b["coding"]["rows"] == 3 and b["coding"]["models_matched"] == 3
    assert b["coding"]["published"] == "2026-11-11"


def test_a_second_poll_refreshes_instead_of_duplicating(db):
    at.store_boards(db, BOARDS)
    at.store_boards(db, BOARDS)
    assert db.execute("SELECT COUNT(*) FROM arena_board_rows").fetchone()[0] == 7


# ── 2. the mapping is the operator's, and it publishes immediately ──────────

def test_mapping_a_board_publishes_its_rows_without_a_re_poll(db):
    at.store_boards(db, BOARDS)
    out = at.map_board(db, "casual_chat", "english")
    assert out["published"]["written"] == 2      # two served models on `english`
    rows = mr.leaderboard(db, "casual_chat")
    assert {r["model_slug"] for r in rows} == {"glm-5.3-flash", "claude-opus-5"}


def test_one_board_can_feed_several_taxonomies(db):
    at.store_boards(db, BOARDS)
    for tax in ("code_generation", "debugging", "unit_tests"):
        at.map_board(db, tax, "coding")
    for tax in ("code_generation", "debugging", "unit_tests"):
        assert {r["model_slug"] for r in mr.leaderboard(db, tax)} == {
            "glm-5.3-flash", "claude-opus-5", "kimi-k3"}


def test_one_taxonomy_can_be_fed_by_boards_of_both_kinds(db):
    """The operator kept this: a tab may show an Elo table AND a net-improvement
    table, labelled separately and never ranked together."""
    at.store_boards(db, BOARDS)
    at.map_board(db, "debugging", "coding")
    at.map_board(db, "debugging", "agent_bash_reliability")
    units = {r["unit"] for r in mr.leaderboard(db, "debugging")}
    assert units == {"elo", "net_improvement"}


def test_unmapping_removes_exactly_that_board_s_rows(db):
    at.store_boards(db, BOARDS)
    at.map_board(db, "debugging", "coding")
    at.map_board(db, "debugging", "agent_bash_reliability")
    out = at.unmap_board(db, "debugging", "agent_bash_reliability")
    assert out["removed"] == 2
    units = {r["unit"] for r in mr.leaderboard(db, "debugging")}
    assert units == {"elo"}          # the coding rows survive


def test_unmapping_the_last_board_empties_the_taxonomy(db):
    at.store_boards(db, BOARDS)
    at.map_board(db, "planning", "coding")
    at.unmap_board(db, "planning", "coding")
    assert mr.leaderboard(db, "planning") == []


def test_custom_can_never_be_a_mapping_target(db):
    at.store_boards(db, BOARDS)
    with pytest.raises(ValueError, match="no upstream"):
        at.map_board(db, "custom:my-board", "coding")


def test_an_unknown_board_is_refused(db):
    with pytest.raises(ValueError, match="unknown board"):
        at.map_board(db, "planning", "not-a-real-board")


def test_mapping_creates_the_tab_before_its_first_row(db):
    at.store_boards(db, BOARDS)
    at.map_board(db, "a_brand_new_axis", "coding")
    assert "a_brand_new_axis" in {t["taxonomy"] for t in mr.taxonomies(db)}


# ── 3. the seed ─────────────────────────────────────────────────────────────

def test_the_former_hardcoded_pairs_are_seeded_once(db):
    at.store_boards(db, BOARDS)
    assert at.seed_default_map(db)["seeded"] >= 1
    # only the seed pairs whose board exists in this fixture
    seeded = {r[0] for r in db.execute(
        "SELECT board FROM taxonomy_map WHERE is_default=1")}
    assert seeded == {"coding", "agent_bash_reliability"}
    # a second call is a no-op
    assert at.seed_default_map(db)["seeded"] == 0


def test_a_default_the_operator_deleted_does_not_come_back(db):
    """Ownership means deletion sticks: the next poll must not resurrect it."""
    at.store_boards(db, BOARDS)
    at.seed_default_map(db)
    at.unmap_board(db, "code_generation", "coding")
    at.store_boards(db, BOARDS)
    at.seed_default_map(db)
    assert "coding" not in at.boards_for(db, "code_generation")


def test_the_seed_never_creates_a_mapping_for_a_board_it_has_not_stored(db):
    """A mapping must not describe a board whose rows we do not have."""
    at.store_boards(db, {"coding": BOARDS["coding"]})
    at.seed_default_map(db)
    boards = {r[0] for r in db.execute("SELECT board FROM taxonomy_map")}
    assert boards == {"coding"}


# ── 4. the leaderboard is a join, not a constant ────────────────────────────

def test_the_leaderboard_reads_the_map_not_the_python_constants(db):
    """Deleting every mapping must empty the board even though TEXT_BOARDS still
    names the pair — the constant is a seed now, not the source of truth."""
    at.store_boards(db, BOARDS)
    at.map_board(db, "code_generation", "coding")
    assert mr.leaderboard(db, "code_generation")
    db.execute("DELETE FROM taxonomy_map")
    db.commit()
    assert mr.leaderboard(db, "code_generation") == []


def test_poll_publishes_through_the_map(db):
    """A full poll: store everything, seed, then build the staged diff via the map."""
    staged = mr.stage_poll(db, fetch=lambda: BOARDS)
    assert staged["staged"] > 0
    assert staged["catalogue"]["boards"] == 3
    # the seeded pairs are what the diff carries
    taxes = {r["taxonomy"] for r in mr.staged_rows(db)}
    assert "code_generation" in taxes


def test_a_poll_never_stages_a_custom_row(db):
    at.store_boards(db, BOARDS)
    at.map_board(db, "casual_chat", "english")
    mr.stage_poll(db, fetch=lambda: BOARDS)
    db.execute("INSERT INTO rating_poll_staging (poll_id, model_slug, taxonomy, unit,"
               " value, source, board, effort, polled_at) VALUES ('p','glm-5.3-flash',"
               "'custom:mine','elo',1500.0,'arena','coding','','2026-11-15T00:00:00Z')")
    assert all(not r["taxonomy"].startswith("custom:") for r in mr.staged_rows(db))


def test_a_mapped_board_with_no_stored_rows_warns_instead_of_inventing(db):
    """Operator's decision: "nothing, saying board has no stored rows yet"."""
    at.store_boards(db, {"coding": BOARDS["coding"]})
    # `english` was never stored, so its rows cannot be published
    db.execute("INSERT INTO arena_boards (board, family, unit, rows, models_matched,"
               " first_seen, last_polled_at) VALUES ('english','text','elo',421,11,"
               "'2026-11-15T00:00:00Z','2026-11-15T00:00:00Z')")
    db.commit()
    out = at.map_board(db, "casual_chat", "english")
    assert out["published"]["written"] == 0
    assert mr.leaderboard(db, "casual_chat") == []
    row = [b for b in at.catalogue(db)["mapped"] if b["board"] == "english"][0]
    assert row["stored_rows"] == 0


# ── 5. what Manage taxonomy shows ───────────────────────────────────────────

def test_the_catalogue_separates_mapped_from_unmapped(db):
    at.store_boards(db, BOARDS)
    at.map_board(db, "code_generation", "coding")
    cat = at.catalogue(db)
    assert [b["board"] for b in cat["mapped"]] == ["coding"]
    assert {b["board"] for b in cat["unmapped"]} == {"english", "agent_bash_reliability"}
    assert cat["boards"] == 3 and cat["unmapped_count"] == 2


def test_an_unmapped_board_still_reports_how_many_of_ours_it_scores(db):
    """This is the information that makes the mapping decidable — hiding it was
    the operator's complaint about the hardcoded table."""
    at.store_boards(db, BOARDS)
    cat = at.catalogue(db)
    english = [b for b in cat["unmapped"] if b["board"] == "english"][0]
    assert english["models_matched"] == 2
    assert english["stored_rows"] == 2


def test_a_board_can_report_every_taxonomy_it_feeds(db):
    at.store_boards(db, BOARDS)
    for tax in ("code_generation", "debugging"):
        at.map_board(db, tax, "coding")
    cat = at.catalogue(db)
    coding = [b for b in cat["mapped"] if b["board"] == "coding"][0]
    assert sorted(coding["taxonomies"]) == ["code_generation", "debugging"]


# ── 6. the poll's axis table is a seed, documented as such ─────────────────

def test_the_constants_still_classify_but_no_longer_publish(db):
    """TEXT_BOARDS/AGENT_BOARDS answer "which family is this board", which decides
    the value column and the unit. They must not decide an axis."""
    src = open("src/api/arena_poll.py").read()
    block = src.split("TEXT_BOARDS = {", 1)[1].split("}", 1)[0]
    assert "coding" in block            # still the classification seed
    assert "SEED, not the source of truth" in src
    # and build_rows reads the mapping
    body = src.split("def build_rows(", 1)[1].split("\n    return {", 1)[0]
    assert "_axis_map(con)" in body
    assert "TEXT_BOARDS[board]" not in body
    assert "TEXT_BOARDS.get(board)" not in body
