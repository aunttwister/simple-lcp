"""Workspace module -- the human work surfaces.

This is the WORK BENCH, deliberately separate from the instrument panel (run
telemetry, graphs, board). The distinction, because it is the whole reason for
the split:

    telemetry  answers "how is it going?"   -- run/usage metrics, graphs, board
    workspace  answers "what needs doing?"  -- Decisions, Tasks, Categories, Fleet

They are separate concerns because they have opposite failure modes. An
instrument can be removed and nothing is lost but visibility. workspace is a
*view of work*: it must survive its telemetry sources being absent, because the
tasks and the decisions still exist whether or not anything is graphing them.

That is enforced structurally, not by convention. Every source workspace reads is
mounted READ-ONLY, and each one is probed independently. When a source is
missing, its view reports `available: False` and names the path it looked at --
it does NOT render an empty table, because an empty table is indistinguishable
from "there is no data" and that is the failure mode that cost two debugging
cycles already (see docs/semantic-routing.md section 8).
"""

import os
from typing import Any, Dict, List, Optional

# ── Sources ─────────────────────────────────────────────────────────────────
# Each is a read-only mount. The default paths are the in-container mount points;
# the env overrides let a host-side run (tests, CLI) point at the real files.

def decisions_db() -> str:
    """The decisions ledger. Optional -- Decisions degrades to empty."""
    return os.environ.get("LCP_WORK_DECISIONS_DB", "/app/work/decisions.db")


def tasks_dir() -> str:
    """The task tree. This one is NOT optional: it is workspace's own data."""
    return os.environ.get("LCP_WORK_TASKS_DIR", "/app/work-tree/tasks")


def todo_path() -> str:
    return os.environ.get("LCP_WORK_TODO", "/app/work-tree/todo.md")


def sessions_dir() -> str:
    """Hermes profile dirs, for session-backed views (Categories)."""
    return os.environ.get("LCP_WORK_PROFILES_DIR", "/app/profiles")


# ── Probes ──────────────────────────────────────────────────────────────────

def _probe(path: str, kind: str) -> Dict[str, Any]:
    """Describe one source: does it exist, and is it usable?"""
    exists = os.path.exists(path)
    info: Dict[str, Any] = {"path": path, "kind": kind, "present": exists}
    if not exists:
        info["detail"] = "absent -- is the read-only mount declared in compose?"
        return info
    try:
        if kind == "sqlite":
            import sqlite3
            con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
            # SELECT 1 is a constant expression that never touches the file,
            # so a corrupt ledger sailed through the probe as "readable
            # read-only". Querying sqlite_master forces a real read and
            # surfaces "file is not a database" for garbage input.
            con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            info["detail"] = "readable read-only"
            con.close()
        elif kind == "tasks":
            # Count TASK directories, not the four state directories. Reporting
            # the raw top-level entry count here reads as "4 tasks" when the
            # tree actually holds 173 -- a status line that under-reports by 40x
            # is worse than no status line.
            from ..api.work_tasks import STATES
            per_state, total = {}, 0
            for state in STATES:
                sdir = os.path.join(path, state)
                n = 0
                if os.path.isdir(sdir):
                    n = sum(1 for e in os.listdir(sdir)
                            if os.path.isdir(os.path.join(sdir, e)))
                per_state[state] = n
                total += n
            info["entries"] = total
            info["per_state"] = per_state
            info["detail"] = "%d tasks (%s)" % (
                total, ", ".join("%s %d" % (k, v) for k, v in per_state.items() if v))
        elif kind == "sessions":
            # Count profiles that actually have a session DB, not directories.
            n = 0
            try:
                for e in os.listdir(path):
                    if os.path.isfile(os.path.join(path, e, "state.db")):
                        n += 1
            except OSError:
                pass
            info["entries"] = n
            info["detail"] = "%d profiles with a session DB" % n
        elif kind == "dir":
            info["entries"] = len(os.listdir(path))
            info["detail"] = "%d entries" % info["entries"]
        else:
            info["detail"] = "present"
    except Exception as exc:  # noqa: BLE001 -- a probe must never raise
        info["present"] = False
        info["detail"] = "%s: %s" % (type(exc).__name__, exc)
    return info


def workspace_sources() -> Dict[str, Dict[str, Any]]:
    """Probe every source this module reads."""
    return {
        "tasks": _probe(tasks_dir(), "tasks"),
        "todo": _probe(todo_path(), "file"),
        "decisions_ledger": _probe(decisions_db(), "sqlite"),
        "sessions": _probe(sessions_dir(), "sessions"),
    }


# ── Status / identity ───────────────────────────────────────────────────────

# Views, and which source each one needs. Used by both the status probe and the
# UI, so a view can never silently ship without its source.
VIEWS = {
    "decisions": ("decisions_ledger", "required"),
    "tasks": ("tasks", "required"),
    "fleet": (None, "self"),          # reads LCP's own APIs
    "categories": ("sessions", "required"),
}


def workspace_status() -> Dict[str, Any]:
    """Module status: available, what it can serve, and what is missing.

    ``available`` is False only when the module cannot do ANYTHING. A single
    missing optional source degrades that one view rather than disabling the
    module -- the task tree is the irreducible core.
    """
    srcs = workspace_sources()

    # Views that can actually render right now.
    live, degraded = [], []
    for view, (source, req) in VIEWS.items():
        if source is None:
            live.append(view)
            continue
        if srcs.get(source, {}).get("present"):
            live.append(view)
        else:
            degraded.append({"view": view, "needs": source,
                             "path": srcs.get(source, {}).get("path")})

    core_present = srcs["tasks"]["present"]
    missing = [k for k, v in srcs.items() if not v["present"]]

    return {
        "available": core_present,
        "removable": not bool(os.environ.get("LCP_HOME")),
        "views_live": sorted(live),
        "views_degraded": degraded,
        "sources": srcs,
        "missing": missing,
    }
