"""Unit tests for the LiveBench capability poller.

No network: the release artifacts are passed in as ``fetched=(categories, rows)``.

The two model rows here are the REAL LiveBench 2026-06-25 rows for ``glm-5.3-flash`` and
``deepseek-v4.1-flash-max``, so the expected values are the figures livebench.ai itself
displays — the aggregation is asserted against the official numbers, not against numbers this
code produced.

The contracts under test:

1. The suite means are LiveBench's own definition ("the model's mean task score for the suite"),
   so the poll reproduces the published table rather than inventing a scale.
2. ``reasoning_chain`` comes from **Mathematics**, not from the Reasoning suite — mapping both
   would let two suites fight over one axis.
3. A row is written only for a name the registry can resolve; everything else is queued.
4. No source ever touches another's rows.
"""
from __future__ import annotations

import csv
import io
import sqlite3
import urllib.error

import pytest

from src.api import livebench_ingest as lb

# The real column order of livebench.ai/v1/table_2026_06_25.csv.
TASKS = ["AMPS_Hard", "code_completion", "code_generation", "connections", "consecutive_events",
         "integrals_with_game", "javascript", "logic_with_navigation", "math_comp", "olympiad",
         "paraphrase", "plot_unscrambling", "python", "simplify", "spatial", "story_generation",
         "summarize", "tablejoin", "tablereformat", "theory_of_mind", "typescript", "typos",
         "zebra_puzzle"]

# livebench.ai/v1/table_2026_06_25.csv, verbatim.
GLM = [97.0, 80.435, 77.465, 93.667, 84.298, 45.0, 63.636, 62.0, 93.137, 89.841, 47.983,
       60.255, 60.0, 46.95, 96.0, 59.883, 56.467, 48.827, 96.078, 73.077, 46.667, 78.0, 79.5]
DS = [98.0, 82.609, 77.465, 100.0, 88.722, 90.0, 81.818, 68.0, 96.078, 89.081, 70.733,
      61.569, 90.0, 67.067, 98.0, 72.683, 69.65, 49.038, 100.0, 80.769, 60.0, 82.0, 100.0]

# livebench.ai/v1/categories_2026_06_25.json, verbatim.
CATS = {
    "Reasoning": ["theory_of_mind", "zebra_puzzle", "spatial", "logic_with_navigation"],
    "Coding": ["code_generation", "code_completion"],
    "Agentic Coding": ["javascript", "typescript", "python"],
    "Mathematics": ["AMPS_Hard", "integrals_with_game", "math_comp", "olympiad"],
    "Data Analysis": ["consecutive_events", "tablejoin", "tablereformat"],
    "Language": ["connections", "plot_unscrambling", "typos"],
    "IF": ["paraphrase", "simplify", "story_generation", "summarize"],
}

#: The figures livebench.ai publishes for these two rows, in the estate's axes.
OFFICIAL = {
    "glm-5.3-flash": {"code_generation": 79.0, "reasoning_chain": 81.2,
                      "agentic_multi_step": 56.8, "research_deep": 76.4,
                      "casual_chat": 77.3, "planning": 52.8},
    "deepseek-flash": {"code_generation": 80.0, "reasoning_chain": 93.3,
                       "agentic_multi_step": 77.3, "research_deep": 79.3,
                       "casual_chat": 81.2, "planning": 70.0},
}


def _rows():
    text = "model," + ",".join(TASKS) + "\n"
    for name, vals in (("glm-5.3-flash", GLM),
                       ("deepseek-v4.1-flash-max", DS),
                       ("Mystery Model X", [50.0] * 23)):
        text += name + "," + ",".join(str(v) for v in vals) + "\n"
    return list(csv.DictReader(io.StringIO(text)))


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE mi_models (model_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT,
          benchmark_key TEXT);
        CREATE TABLE mi_sources (source_id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL,
          kind TEXT NOT NULL, endpoint_id INTEGER, convention TEXT,
          status TEXT NOT NULL DEFAULT 'active', created_at TEXT);
        CREATE TABLE mi_model_names (id INTEGER PRIMARY KEY, model_id INTEGER NOT NULL,
          source_id INTEGER NOT NULL, name TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0,
          confidence TEXT, evidence TEXT, valid_from TEXT, valid_to TEXT, first_seen TEXT,
          last_seen TEXT, verified_at TEXT, note TEXT, UNIQUE(source_id, name));
        CREATE TABLE mi_deployments (deployment_id INTEGER PRIMARY KEY, endpoint_id INTEGER,
          model_id INTEGER, served_id TEXT);
        CREATE TABLE model_capabilities (id INTEGER PRIMARY KEY, model TEXT, task_type TEXT,
          score REAL, source TEXT, benchmark_category TEXT, raw_score REAL,
          updated_at TEXT, release_label TEXT);
        CREATE TABLE mi_identity_queue (
          id INTEGER PRIMARY KEY, raw TEXT NOT NULL, norm TEXT NOT NULL,
          source TEXT NOT NULL, kind TEXT NOT NULL, evidence TEXT, suggested_slug TEXT,
          suggested_by TEXT, status TEXT NOT NULL DEFAULT 'pending',
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          occurrences INTEGER NOT NULL DEFAULT 1, decided_at TEXT, note TEXT,
          UNIQUE(norm, source));
    """)
    con.execute("INSERT INTO mi_models VALUES (1, 'glm-5.3-flash', 'GLM-5.3 Flash', NULL)")
    # The registry's own route for the LiveBench name: the benchmark key.
    con.execute("INSERT INTO mi_models VALUES (2, 'deepseek-flash', 'DeepSeek Flash',"
                " 'deepseek-v4.1-flash-max')")
    # Rows belonging to OTHER writers, which this poll must never disturb.
    con.execute("INSERT INTO model_capabilities VALUES (1, 'glm-5.3-flash', 'code_generation',"
                " 0.91, 'lmarena', 'coding', 1479.12, '2026-11-15', '2026-11-11')")
    con.execute("INSERT INTO model_capabilities VALUES (2, 'glm-5.3-flash', 'research_deep',"
                " 0.55, 'manual', 'hand', 55.0, '2026-11-17', '2026-11-17')")
    con.commit()
    yield con
    con.close()


def _run(con, **kw):
    kw.setdefault("fetched", (CATS, _rows()))
    return lb.ingest(con, **kw)


def _raw(con, model, axis):
    r = con.execute("select raw_score from model_capabilities where source='livebench'"
                    " and model=? and task_type=?", (model, axis)).fetchone()
    return None if r is None else r["raw_score"]


# ── the aggregation reproduces the published figures ───────────────────────

@pytest.mark.parametrize("model", ["glm-5.3-flash", "deepseek-flash"])
def test_the_poll_reproduces_the_official_livebench_values(db, model):
    """If this drifts, the matrix silently disagrees with the source it cites."""
    _run(db)
    for axis, expected in OFFICIAL[model].items():
        got = _raw(db, model, axis)
        assert got is not None, f"{model}/{axis} was not written"
        assert abs(got - expected) <= 0.05, (
            f"{model}/{axis}: polled {got}, livebench.ai publishes {expected}")


def test_reasoning_chain_comes_from_MATHEMATICS_not_from_the_Reasoning_suite(db):
    """Both suites map to reasoning_chain in the estate's vocabulary, so only one may be used.

    For glm-5.3-flash the two differ sharply: Mathematics 81.2 vs Reasoning 77.6. Storing the
    wrong one would be a silent 3.6-point error on every reasoning decision.
    """
    _run(db)
    assert abs(_raw(db, "glm-5.3-flash", "reasoning_chain") - 81.2) <= 0.05
    assert abs(_raw(db, "glm-5.3-flash", "reasoning_chain") - 77.6) > 1.0, (
        "the Reasoning suite is being used — it is deliberately unmapped")


def test_only_the_six_mapped_axes_are_written(db):
    """`Reasoning` has no axis, and debugging/unit_tests are DERIVED on read by
    seed_capabilities.DERIVED_TASKS — writing them here would duplicate that rule."""
    out = _run(db)
    assert out["axes"] == sorted(OFFICIAL["glm-5.3-flash"])
    stored = {r["task_type"] for r in db.execute(
        "select distinct task_type from model_capabilities where source='livebench'")}
    assert stored == set(OFFICIAL["glm-5.3-flash"])
    assert "debugging" not in stored and "unit_tests" not in stored


# ── the identity rule ─────────────────────────────────────────────────────

def test_only_resolvable_names_become_rows(db):
    out = _run(db)
    assert out["models_matched"] == 2 and out["models_in_release"] == 3
    keys = {r["model"] for r in db.execute(
        "select distinct model from model_capabilities where source='livebench'")}
    assert keys == {"glm-5.3-flash", "deepseek-flash"}


def test_a_benchmark_key_resolves_the_livebench_name(db):
    """The LiveBench name is not the slug: it reaches the canonical model through the
    registry's benchmark-key route, which is the whole reason this is safe to automate."""
    out = _run(db)
    assert out["rows"] == 12
    assert _raw(db, "deepseek-flash", "code_generation") is not None


def test_unmatched_name_goes_to_the_identity_queue(db):
    out = _run(db)
    assert out["queued"] == 1
    q = db.execute("select raw, source, kind from mi_identity_queue").fetchall()
    assert len(q) == 1 and q[0]["raw"] == "Mystery Model X"
    assert q[0]["source"] == "livebench" and q[0]["kind"] == "capability_name"


def test_no_dead_rows_keyed_by_an_unjoinable_name(db):
    from src.api.model_identity_queue import resolve_canonical
    _run(db)
    for r in db.execute("select distinct model from model_capabilities"):
        assert resolve_canonical(db, r["model"]), f"{r['model']!r} is stored but unreachable"


# ── provenance and the column contracts ───────────────────────────────────

def test_other_sources_rows_are_never_touched(db):
    """A poll must not overwrite a human's value or another benchmark's."""
    _run(db)
    lm = db.execute("select score, raw_score, updated_at, release_label from model_capabilities"
                    " where source='lmarena'").fetchone()
    assert (lm["score"], lm["raw_score"], lm["updated_at"], lm["release_label"]) == \
           (0.91, 1479.12, "2026-11-15", "2026-11-11")
    man = db.execute("select score, updated_at from model_capabilities"
                     " where source='manual'").fetchone()
    assert (man["score"], man["updated_at"]) == (0.55, "2026-11-17")


def test_score_is_a_0_1_fraction_and_raw_score_is_the_published_value(db):
    """Every writer stores 0-1 because the UI renders `score * 100`."""
    _run(db)
    for r in db.execute("select score, raw_score from model_capabilities"
                        " where source='livebench'"):
        assert 0.0 <= r["score"] <= 1.0, f"{r['score']} would render as {r['score']*100}%"
        assert 0.0 <= r["raw_score"] <= 100.0
    assert abs(_raw(db, "glm-5.3-flash", "code_generation") - 79.0) <= 0.05
    s = db.execute("select score from model_capabilities where source='livebench'"
                   " and model='glm-5.3-flash' and task_type='code_generation'").fetchone()[0]
    assert abs(s - 0.79) <= 0.0005


def test_release_label_is_the_release_the_numbers_were_read_from(db):
    out = _run(db)
    assert out["release_label"] == lb.RELEASE == "2026-06-25"
    labels = {r["release_label"] for r in db.execute(
        "select distinct release_label from model_capabilities where source='livebench'")}
    assert labels == {"2026-06-25"}
    # The schema contract this satisfies: a vintage, not a model release date.
    assert "coding" in {
        r["benchmark_category"] for r in db.execute(
            "select distinct benchmark_category from model_capabilities where source='livebench'")}


def test_benchmark_category_uses_the_estates_own_vocabulary(db):
    """The seed stores `math`/`agentic_coding`/`instruction_following`, not the display names."""
    _run(db)
    cats = {r["benchmark_category"] for r in db.execute(
        "select distinct benchmark_category from model_capabilities where source='livebench'")}
    assert cats == {"coding", "math", "agentic_coding", "data_analysis", "language",
                    "instruction_following"}


# ── idempotence ───────────────────────────────────────────────────────────

def test_reruns_refresh_rather_than_duplicate(db):
    _run(db)
    n1 = db.execute("select count(*) from model_capabilities where source='livebench'"
                    ).fetchone()[0]
    _run(db)
    n2 = db.execute("select count(*) from model_capabilities where source='livebench'"
                    ).fetchone()[0]
    assert n1 == n2 == 12
    assert db.execute("select count(*) from mi_identity_queue").fetchone()[0] == 1


def test_dry_run_writes_nothing(db):
    out = _run(db, dry_run=True)
    assert out["models_matched"] == 2 and out["rows"] == 0 and out["queued"] == 1
    assert out["axes"] == sorted(OFFICIAL["glm-5.3-flash"]), (
        "the dry run is the operator's preview, so it must still report the axes")
    assert db.execute("select count(*) from model_capabilities where source='livebench'"
                      ).fetchone()[0] == 0
    assert db.execute("select count(*) from mi_identity_queue").fetchone()[0] == 0


def test_it_works_on_a_bare_connection_without_row_factory(db):
    """The identity helpers read by column name; a caller passing a plain
    sqlite3.connect(...) must not blow up inside resolve_canonical."""
    plain = sqlite3.connect(":memory:")
    plain.executescript("""
        CREATE TABLE mi_models (model_id INTEGER PRIMARY KEY, slug TEXT, display_name TEXT,
          benchmark_key TEXT);
        CREATE TABLE mi_sources (source_id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL,
          kind TEXT NOT NULL, endpoint_id INTEGER, convention TEXT,
          status TEXT NOT NULL DEFAULT 'active', created_at TEXT);
        CREATE TABLE mi_model_names (id INTEGER PRIMARY KEY, model_id INTEGER NOT NULL,
          source_id INTEGER NOT NULL, name TEXT NOT NULL, is_primary INTEGER NOT NULL DEFAULT 0,
          confidence TEXT, evidence TEXT, valid_from TEXT, valid_to TEXT, first_seen TEXT,
          last_seen TEXT, verified_at TEXT, note TEXT, UNIQUE(source_id, name));
        CREATE TABLE mi_deployments (deployment_id INTEGER PRIMARY KEY, endpoint_id INTEGER,
          model_id INTEGER, served_id TEXT);
        CREATE TABLE model_capabilities (id INTEGER PRIMARY KEY, model TEXT, task_type TEXT,
          score REAL, source TEXT, benchmark_category TEXT, raw_score REAL,
          updated_at TEXT, release_label TEXT);
        CREATE TABLE mi_identity_queue (
          id INTEGER PRIMARY KEY, raw TEXT NOT NULL, norm TEXT NOT NULL,
          source TEXT NOT NULL, kind TEXT NOT NULL, evidence TEXT, suggested_slug TEXT,
          suggested_by TEXT, status TEXT NOT NULL DEFAULT 'pending',
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
          occurrences INTEGER NOT NULL DEFAULT 1, decided_at TEXT, note TEXT,
          UNIQUE(norm, source));
    """)
    plain.execute("INSERT INTO mi_models VALUES (1, 'glm-5.3-flash', 'GLM-5.3 Flash', NULL)")
    plain.commit()
    out = lb.ingest(plain, fetched=(CATS, _rows()))
    assert out["models_matched"] >= 1
    plain.close()


# ── the aggregation itself ────────────────────────────────────────────────

def test_blank_and_na_cells_are_skipped_but_zero_is_a_real_score():
    rows = [{"model": "m", "code_generation": "80", "code_completion": ""}]
    assert lb.suite_scores({"Coding": ["code_generation", "code_completion"]}, rows) == \
        {"m": {"Coding": 80.0}}, "a blank cell is a gap, not a zero"
    rows = [{"model": "m", "code_generation": "80", "code_completion": "0"}]
    assert lb.suite_scores({"Coding": ["code_generation", "code_completion"]}, rows) == \
        {"m": {"Coding": 40.0}}, "an unmeasured task scores 0 and must count in the mean"


def test_a_non_list_suite_key_is_ignored():
    """2.0's categories file carries `_overall_weights` beside the suites."""
    cats = {"Coding": ["code_generation"], "_overall_weights": {"Coding": 1.5}}
    got = lb.suite_scores(cats, [{"model": "m", "code_generation": "90"}])
    assert got == {"m": {"Coding": 90.0}}


# ── the fetch must survive a blip (measured: the first real call got a 503) ──────

class _Resp:
    def __init__(self, body):
        self._b = body.encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_transient_503_is_retried(monkeypatch):
    """livebench.ai rate-limits bursts. A poll that gives up on the first 503 records
    nothing while looking like a source outage — so the retry is the difference between
    "LiveBench had no data" and "we asked too fast"."""
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)
        return _Resp("ok")

    monkeypatch.setattr(lb.urllib.request, "urlopen", fake)
    assert lb._get("https://example.invalid/x", backoff=0) == "ok"
    assert calls["n"] == 2


def test_a_404_is_not_retried(monkeypatch):
    """A 404 means the release does not exist. Retrying it once cost real debugging time
    here — a wrong artifact path was misread as a flaky host for three attempts."""
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(lb.urllib.request, "urlopen", fake)
    with pytest.raises(urllib.error.HTTPError):
        lb._get("https://example.invalid/x", attempts=3, backoff=0)
    assert calls["n"] == 1, "a 404 is a real answer, not a blip"


def test_the_artifact_path_uses_underscores_but_the_label_keeps_hyphens():
    """The release LABEL is `2026-06-25`; the FILES are `table_2026_06_25.csv`. Mixing them
    requests a path that does not exist — the bug this pins."""
    assert lb.RELEASE == "2026-06-25"
    assert lb._slug(lb.RELEASE) == "2026_06_25"
    assert lb.URL_TABLE.format(slug=lb._slug(lb.RELEASE)).endswith("/table_2026_06_25.csv")
    assert lb.URL_CATEGORIES.format(slug=lb._slug(lb.RELEASE)).endswith(
        "/categories_2026_06_25.json")


def test_a_503_is_retried(monkeypatch):
    """livebench.ai 503s under a burst; the poll must not record that as "no data"."""
    calls = {"n": 0}

    def fake(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)
        return _Resp("ok")

    monkeypatch.setattr(lb.urllib.request, "urlopen", fake)
    assert lb._get("https://example.invalid/x", backoff=0) == "ok"
    assert calls["n"] == 2
