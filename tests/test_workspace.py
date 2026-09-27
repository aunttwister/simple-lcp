"""Tests for src/api/workspace.py -- the workspace module status/probe layer.

The module under test is pure data probing: it reads directories and one
SQLite file and reports their state. Nothing here touches the network/HTTP
layer, and every source path is pointed at temp dirs via env vars, never at
real host paths.
"""

import os
import sqlite3

import pytest

import src.api.workspace as ws

# The four env vars the module reads (see the decisions_db/tasks_dir/todo_path/
# sessions_dir helpers).
ENV_VARS = (
    "LCP_WORK_DECISIONS_DB",
    "LCP_WORK_TASKS_DIR",
    "LCP_WORK_TODO",
    "LCP_WORK_PROFILES_DIR",
)

# The exact schema of the live decisions ledger: the verdict column is 'label'
# and the actor column is 'engine'. A fixture with a made-up 'choice' column
# would silently exercise nothing.
LIVE_LEDGER_SCHEMA = """
CREATE TABLE decisions (
    id INTEGER, event_id TEXT, run TEXT, event_t REAL, event_kind TEXT,
    event_json TEXT, asked_at REAL, engine TEXT, model TEXT, question_hash TEXT,
    label TEXT, criteria TEXT, confidence REAL, latency_ms REAL, raw TEXT)
"""


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Point every workspace source at fresh temp paths (dirs created)."""
    tasks = tmp_path / "tasks"
    sessions = tmp_path / "profiles"
    todo = tmp_path / "todo.md"
    ledger = tmp_path / "decisions.db"
    tasks.mkdir()
    sessions.mkdir()
    monkeypatch.setenv("LCP_WORK_TASKS_DIR", str(tasks))
    monkeypatch.setenv("LCP_WORK_PROFILES_DIR", str(sessions))
    monkeypatch.setenv("LCP_WORK_TODO", str(todo))
    monkeypatch.setenv("LCP_WORK_DECISIONS_DB", str(ledger))
    return {"tasks": tasks, "sessions": sessions, "todo": todo, "ledger": ledger}


def _task(tree, state, name):
    d = tree / state / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_tasks(tree, per_state):
    for state, names in per_state.items():
        for name in names:
            _task(tree, state, name)


def _seed_ledger(path, rows, schema=LIVE_LEDGER_SCHEMA):
    con = sqlite3.connect(str(path))
    con.execute(schema)
    for r in rows:
        con.execute(
            "INSERT INTO decisions (engine, label, confidence, event_kind) "
            "VALUES (?, ?, ?, ?)", r)
    con.commit()
    con.close()
    return path


# ── tasks kind: counts TASK directories, never the state dirs ──────────────

def test_tasks_counts_task_dirs_not_state_dirs(env):
    """3 tasks in new + 2 in in_progress must read '5 tasks', never '4'.

    This pins the exact regression that once shipped: the probe counted the
    four top-level state directories and reported "4 entries" for a tree that
    actually held 173 tasks.
    """
    _seed_tasks(env["tasks"], {
        "new": ["t%d" % i for i in range(3)],
        "in_progress": ["t%d" % i for i in range(2)],
        "completed": [],
        "cancelled": [],
    })
    info = ws._probe(str(env["tasks"]), "tasks")
    assert info["present"] is True
    assert info["entries"] == 5                       # task-level, not '4 entries'
    assert info["per_state"] == {
        "new": 3, "in_progress": 2, "completed": 0, "cancelled": 0}
    assert "5 tasks" in info["detail"]
    assert "new 3" in info["detail"] and "in_progress 2" in info["detail"]


def test_tasks_counts_task_dirs_not_files_or_deeper_dirs(env):
    """A task dir is one entry even if it contains subdirs; files do not count."""
    nested = _task(env["tasks"], "completed", "nested")
    (nested / "context").mkdir()                      # inside the task, still 1
    _task(env["tasks"], "new", "plain")
    (env["tasks"] / "completed" / "notes.txt").write_text("x")  # file != task
    info = ws._probe(str(env["tasks"]), "tasks")
    assert info["entries"] == 2
    assert info["per_state"]["completed"] == 1
    assert info["per_state"]["new"] == 1


def test_tasks_empty_tree_reports_zero(env):
    info = ws._probe(str(env["tasks"]), "tasks")
    assert info["present"] is True
    assert info["entries"] == 0
    assert info["per_state"] == {
        "new": 0, "in_progress": 0, "completed": 0, "cancelled": 0}


# ── sessions kind: counts profiles with a real state.db file ───────────────

def test_sessions_counts_profiles_with_state_db(env):
    for name in ("p1", "p2", "p3"):
        (env["sessions"] / name).mkdir()
    (env["sessions"] / "p1" / "state.db").write_text("x")
    (env["sessions"] / "p2" / "state.db").write_text("x")
    # p3 has no db; a state.db that is a DIRECTORY must not count
    (env["sessions"] / "p3" / "state.db").mkdir()
    # a stray top-level file is not a profile
    (env["sessions"] / "README.md").write_text("hi")
    info = ws._probe(str(env["sessions"]), "sessions")
    assert info["present"] is True
    assert info["entries"] == 2                       # p1, p2 -- not p3, not README.md
    assert info["detail"] == "2 profiles with a session DB"


def test_sessions_probe_never_raises_on_file_path(env):
    f = env["sessions"] / "not-a-dir.txt"
    f.write_text("x")
    info = ws._probe(str(f), "sessions")              # os.listdir -> OSError
    assert info["present"] is True
    assert info["entries"] == 0


# ── sqlite kind: read-only, WAL-safe, honest about garbage ─────────────────

def test_sqlite_probe_reads_wal_db_with_sidecars(env):
    """A WAL ledger with -wal/-shm sidecars present must still probe readable."""
    db = env["ledger"]
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE decisions (id INTEGER, label TEXT)")
    con.execute("INSERT INTO decisions (label) VALUES ('publish')")
    con.commit()
    try:
        assert os.path.exists(str(db) + "-wal")       # sidecar really present
        assert os.path.exists(str(db) + "-shm")
        info = ws._probe(str(db), "sqlite")
        assert info["present"] is True
        assert info["detail"] == "readable read-only"
    finally:
        con.close()


def test_sqlite_probe_reports_corrupt_db_unusable(env):
    """A corrupt ledger must read as NOT present, not 'readable read-only'.

    Guards the fix: the probe used to run SELECT 1, a constant expression that
    never touches the file, so garbage input passed as readable.
    """
    db = env["ledger"]
    db.write_bytes(b"this is not a sqlite database at all" * 20)
    info = ws._probe(str(db), "sqlite")
    assert info["present"] is False
    assert "DatabaseError" in info["detail"]


def test_sqlite_probe_directory_is_not_readable(env):
    d = env["ledger"]
    d.mkdir()
    info = ws._probe(str(d), "sqlite")
    assert info["present"] is False


# ── file / dir kinds ───────────────────────────────────────────────────────

def test_file_kind_present_for_real_file(env):
    env["todo"].write_text("# todo\n")
    info = ws._probe(str(env["todo"]), "file")
    assert info["present"] is True
    assert info["detail"] == "present"


def test_dir_kind_counts_entries(env):
    (env["sessions"] / "a").mkdir()
    (env["sessions"] / "b").mkdir()
    env["sessions"].joinpath("c.txt").write_text("x")
    info = ws._probe(str(env["sessions"]), "dir")
    assert info["present"] is True
    assert info["entries"] == 3


def test_dir_kind_never_raises_on_file_path(env):
    f = env["sessions"] / "plain.txt"
    f.write_text("x")
    info = ws._probe(str(f), "dir")
    assert info["present"] is False


# ── absent sources ─────────────────────────────────────────────────────────

def test_absent_source_reports_false_with_mount_hint(monkeypatch, tmp_path):
    for var in ENV_VARS:
        monkeypatch.setenv(var, str(tmp_path / "missing" / var))
    srcs = ws.workspace_sources()
    assert set(srcs) == {"tasks", "todo", "decisions_ledger", "sessions"}
    for key, var in (("tasks", "LCP_WORK_TASKS_DIR"),
                     ("todo", "LCP_WORK_TODO"),
                     ("decisions_ledger", "LCP_WORK_DECISIONS_DB"),
                     ("sessions", "LCP_WORK_PROFILES_DIR")):
        info = srcs[key]
        assert info["present"] is False, key
        assert "mount" in info["detail"], key          # 'is the read-only mount declared...'
        assert info["path"] == str(tmp_path / "missing" / var), key


def test_env_defaults_used_when_unset(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    srcs = ws.workspace_sources()
    assert srcs["tasks"]["path"] == "/app/work-tree/tasks"
    assert srcs["todo"]["path"] == "/app/work-tree/todo.md"
    assert srcs["decisions_ledger"]["path"] == "/app/work/decisions.db"
    assert srcs["sessions"]["path"] == "/app/profiles"


# ── workspace_status() shape and failure modes ─────────────────────────────

STATUS_KEYS = ("available", "views_live", "views_degraded", "missing", "sources")


def _full_env(env):
    """Make every source real: a task tree, a ledger, a todo file, sessions."""
    _seed_tasks(env["tasks"], {
        "new": ["t1"], "in_progress": ["t2"], "completed": [], "cancelled": []})
    _seed_ledger(env["ledger"], [
        ("rlcd", "publish", 0.9, "restart"),
        ("rlcd", "suppress", 0.7, "restart"),
        ("policy", "suppress", None, "churn")])
    env["todo"].write_text("# todo\n")
    (env["sessions"] / "p1").mkdir()
    (env["sessions"] / "p1" / "state.db").write_text("x")


def test_status_all_sources_present(env):
    _full_env(env)
    status = ws.workspace_status()
    for k in STATUS_KEYS:
        assert k in status, k
    assert status["available"] is True
    assert status["views_live"] == ["categories", "decisions", "fleet", "tasks"]
    assert status["views_degraded"] == []
    assert status["missing"] == []
    assert status["sources"]["tasks"]["path"] == str(env["tasks"])
    assert status["sources"]["tasks"]["entries"] == 2


def test_status_missing_optional_sources_degrades_views_only(env, monkeypatch):
    _seed_tasks(env["tasks"], {"new": ["t1"], "in_progress": [],
                               "completed": [], "cancelled": []})
    env["todo"].write_text("# todo\n")
    # ledger + sessions left truly absent (sessions path does not exist):
    # decisions and categories degrade, the module stays available.
    monkeypatch.setenv("LCP_WORK_PROFILES_DIR",
                       str(env["sessions"] / "does-not-exist"))
    status = ws.workspace_status()
    assert status["available"] is True               # task tree is the core
    assert status["views_live"] == ["fleet", "tasks"]
    assert sorted(v["view"] for v in status["views_degraded"]) == [
        "categories", "decisions"]
    assert status["missing"] == ["decisions_ledger", "sessions"]


def test_status_all_absent_never_raises(monkeypatch, tmp_path):
    for var in ENV_VARS:
        monkeypatch.setenv(var, str(tmp_path / "gone" / var))
    status = ws.workspace_status()                    # must not raise
    assert status["available"] is False
    assert sorted(status["missing"]) == sorted(
        ("tasks", "todo", "decisions_ledger", "sessions"))
    assert status["views_live"] == ["fleet"]
    assert status["sources"]["tasks"]["present"] is False


def test_status_garbage_paths_never_raises(monkeypatch, tmp_path):
    """Every source is garbage: file where a dir is expected, corrupt sqlite,
    dir where a file is expected, file where profiles are expected."""
    tree_file = tmp_path / "tasks_as_file"
    tree_file.write_text("i am a file, not a task tree")
    ledger_bad = tmp_path / "decisions_bad.db"
    ledger_bad.write_bytes(b"garbage bytes, not sqlite" * 20)
    sessions_file = tmp_path / "profiles_as_file"
    sessions_file.write_text("i am a file, not profiles")
    todo_dir = tmp_path / "todo_as_dir"
    todo_dir.mkdir()
    monkeypatch.setenv("LCP_WORK_TASKS_DIR", str(tree_file))
    monkeypatch.setenv("LCP_WORK_DECISIONS_DB", str(ledger_bad))
    monkeypatch.setenv("LCP_WORK_PROFILES_DIR", str(sessions_file))
    monkeypatch.setenv("LCP_WORK_TODO", str(todo_dir))
    status = ws.workspace_status()                    # must not raise
    assert set(STATUS_KEYS).issubset(status)
    assert "sources" in status
    assert status["sources"]["decisions_ledger"]["present"] is False
    assert status["sources"]["tasks"]["entries"] == 0
    assert status["sources"]["tasks"]["present"] is True   # exists, holds 0 tasks


def test_status_sqlite_absent_degrades_only_decisions(env):
    _seed_tasks(env["tasks"], {"new": ["t1"], "in_progress": [],
                               "completed": [], "cancelled": []})
    (env["sessions"] / "p1").mkdir()
    (env["sessions"] / "p1" / "state.db").write_text("x")
    env["todo"].write_text("# todo\n")
    status = ws.workspace_status()
    assert status["available"] is True
    assert status["missing"] == ["decisions_ledger"]
    degraded = {d["view"]: d for d in status["views_degraded"]}
    assert degraded["decisions"]["needs"] == "decisions_ledger"
    assert degraded["decisions"]["path"] == str(env["ledger"])