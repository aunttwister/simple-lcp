"""Work-layer source configuration.

Every path the work layer reads (task trees, cron stores, the op spool) is
data, not code. ``work-sources.json`` lives inside the ops mount so BOTH the
LCP container (via /app/cron-ops) and the host-side executor/collector (via
/your/data/app/lcp/cron-ops) can read and write it without another volume.

Schema (v1)::

    {
      "version": 1,
      "hermes_profiles_dir": "/root/.hermes/profiles",   # cron stores live here
      "tasks_root": "/root/.hermes/profiles/<p>/work/tasks",  # default Tasks panel root
      "ops_dir": "/your/data/app/lcp/cron-ops",          # op spool (host view)
      "profiles": {
        "<profile>": {
          "tasks_root": "…",     # optional per-profile override
          "cron_store": "…",     # optional; default <hermes_profiles_dir>/<p>/cron
          "legacy": false,
          "hidden": false        # hide from the fleet view
        }
      }
    }

The design goal is that nothing about Hermes is required: these values are
just source locations. The collector seeds the file on first run with
whatever it can discover; a user can repoint every value through the panel
(Work > Cron → Work sources).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Optional

DEFAULT_OPS_DIR = "/app/cron-ops"           # container view
DEFAULT_PROFILES_DIR = "/root/.hermes/profiles"
DEFAULT_TASKS_ROOT = "/root/.hermes/profiles/homelab-expert-l2/work/tasks"

_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9_.\-/]{1,300}$")
_RESERVED = {"version", "hermes_profiles_dir", "tasks_root", "ops_dir",
             "profiles", "updated_at", "notes"}


def ops_dir() -> str:
    return os.environ.get("LCP_CRON_OPS_DIR", DEFAULT_OPS_DIR)


def sources_path() -> str:
    return os.environ.get("LCP_WORK_SOURCES",
                          os.path.join(ops_dir(), "work-sources.json"))


def _checked(v: Any, what: str) -> str:
    if not isinstance(v, str) or not _ABS_PATH_RE.fullmatch(v):
        raise ValueError("%s must be an absolute bare path (got %r)" % (what, v))
    return v


def validate(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + normalize a sources payload. Raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("sources must be an object")
    version = data.get("version")
    if version != 1:
        raise ValueError("unsupported sources version %r" % version)

    out = {"version": 1}
    out["hermes_profiles_dir"] = _checked(
        data.get("hermes_profiles_dir") or DEFAULT_PROFILES_DIR,
        "hermes_profiles_dir")
    tasks_root = data.get("tasks_root")
    if tasks_root:
        out["tasks_root"] = _checked(tasks_root, "tasks_root")
    ops = data.get("ops_dir")
    if ops:
        out["ops_dir"] = _checked(ops, "ops_dir")

    profiles_in = data.get("profiles") or {}
    if not isinstance(profiles_in, dict):
        raise ValueError("profiles must be an object keyed by profile name")
    profiles: Dict[str, Any] = {}
    for name, cfg in profiles_in.items():
        if not _PROFILE_RE.fullmatch(str(name)):
            raise ValueError("profile name rejected: %r" % name)
        if not isinstance(cfg, dict):
            raise ValueError("profile %s config must be an object" % name)
        known = {"tasks_root", "cron_store", "legacy", "hidden"}
        extra = set(cfg) - known
        if extra:
            raise ValueError("profile %s has unknown keys: %s"
                             % (name, ", ".join(sorted(extra))))
        p = {}
        if cfg.get("tasks_root"):
            p["tasks_root"] = _checked(cfg["tasks_root"], "%s.tasks_root" % name)
        if cfg.get("cron_store"):
            p["cron_store"] = _checked(cfg["cron_store"], "%s.cron_store" % name)
        p["legacy"] = bool(cfg.get("legacy"))
        p["hidden"] = bool(cfg.get("hidden"))
        profiles[str(name)] = p
    out["profiles"] = profiles

    extras = set(data) - _RESERVED
    if extras:
        notes = out.get("notes") or {}
        notes["ignored_keys"] = sorted(extras)
        out["notes"] = notes
    return out


def load_sources() -> Optional[Dict[str, Any]]:
    """Load + validate the sources file; None when absent or corrupt."""
    p = sources_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return validate(raw)
    except (OSError, ValueError):
        return None


def tasks_root_for(profile: str) -> str:
    """The task tree one profile resolves to: its override, else the default.

    Shared by the Tasks tab of a profile page and the tasks JSON API, so the page
    and the table it loads can never disagree about which tree is being shown.
    Returns "" when the profile has no entry at all, which callers read as "use
    the default root" rather than "no tasks".
    """
    if not profile:
        return ""
    try:
        entry = (resolved_view().get("profiles") or {}).get(profile) or {}
    except Exception:  # noqa: BLE001 — a bad sources file must not break a page
        return ""
    return entry.get("tasks_root") or ""


def resolved_view() -> Dict[str, Any]:
    """The panel's form payload: file values merged over defaults."""
    file_vals = load_sources() or {}
    profiles_dir = file_vals.get("hermes_profiles_dir", DEFAULT_PROFILES_DIR)
    tasks_root = file_vals.get("tasks_root", DEFAULT_TASKS_ROOT)
    ops = file_vals.get("ops_dir", DEFAULT_OPS_DIR)

    from . import work_cron  # local import: snapshot drives profile names

    snap_profiles = []
    try:
        v = work_cron.cron_view()
        for p in v.get("profiles", []):
            snap_profiles.append((p["profile"], bool(p.get("legacy"))))
    except Exception:  # noqa: BLE001
        snap_profiles = [(k, False) for k in file_vals.get("profiles", {})]

    configured = set(file_vals.get("profiles", {}))
    profiles = {}
    for name, legacy in snap_profiles:
        cfg = file_vals.get("profiles", {}).get(name, {})
        profiles[name] = {
            "tasks_root": cfg.get("tasks_root",
                                  os.path.join(profiles_dir, name, "work", "tasks")),
            "cron_store": cfg.get("cron_store",
                                  os.path.join(profiles_dir, name, "cron")),
            "legacy": bool(cfg.get("legacy", legacy)),
            "hidden": bool(cfg.get("hidden", False)),
            "configured": name in configured,
        }
    return {
        "configured": bool(file_vals),
        "hermes_profiles_dir": profiles_dir,
        "tasks_root": tasks_root,
        "ops_dir": ops,
        "profiles": profiles,
    }


def save_sources(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + atomically persist the sources file."""
    clean = validate(data)
    clean["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p = sources_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=2, sort_keys=True)
    os.replace(tmp, p)
    return clean