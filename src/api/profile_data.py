"""Per-profile introspection for the profile detail page (M2c).

Everything here is read-only except the three avatar helpers. The LCP container
mounts the Hermes profiles directory at ``/app/profiles``, so a profile's skills,
its memory files and its task tree are visible without the gateway owning them —
the same relationship the work layer already has with the host.

Two rules this module exists to enforce:

* **The profile name is validated before it is ever joined onto a path.** A
  name reaches this module from a URL, so it is checked against a strict pattern
  and the resolved path is asserted to stay inside the profiles root. Without
  both checks ``/profiles/..%2f..%2fetc`` is a file-read primitive.
* **Only known artefacts are read** — ``skills/**/SKILL.md`` and the two memory
  files. A profile directory also holds ``.env`` (provider keys), ``auth.json``
  and a session DB; those must never be reachable through a viewer that exists
  to show a skill list. Nothing here walks arbitrary files.

Read failures degrade to ``available: False`` with a reason rather than raising:
a viewer that 500s on a missing directory is worse than one that says why it is
empty.
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

# A profile name is a directory name: lowercase-with-dashes in every real
# deployment, but underscores and dots appear in older ones. Anything else is
# rejected outright rather than escaped.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Display caps, so one enormous profile cannot produce a 40 MB page. Both are
# reported in the view so the page can say what it is not showing.
SKILL_CAP = 500
MEMORY_CHAR_CAP = 40_000

MEMORY_FILES = ("MEMORY.md", "USER.md")

AVATAR_MAX_BYTES = 256 * 1024
# content-type -> (extension, magic-byte check)
AVATAR_TYPES = {
    "image/png": ("png", lambda b: b.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": ("jpg", lambda b: b.startswith(b"\xff\xd8\xff")),
    "image/webp": ("webp", lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP"),
}


class BadProfileName(ValueError):
    """Raised when a profile name is not a safe single path segment."""


def profiles_root() -> str:
    """Where the per-profile directories live inside this container."""
    return os.environ.get("LCP_PROFILES_DIR", "/app/profiles")


def validate_name(name: str) -> str:
    """Return ``name`` if it is a safe single path segment, else raise."""
    name = (name or "").strip()
    if not _NAME_RE.match(name) or name in (".", ".."):
        raise BadProfileName("invalid profile name")
    return name


def profile_dir(name: str) -> Optional[str]:
    """Absolute path to a profile directory, or None when it does not exist.

    The resolved real path must still sit directly under the profiles root, so a
    symlinked profile directory pointing elsewhere is not followed.
    """
    name = validate_name(name)
    root = os.path.realpath(profiles_root())
    path = os.path.realpath(os.path.join(root, name))
    if os.path.dirname(path) != root:
        return None
    return path if os.path.isdir(path) else None


# ── skills ───────────────────────────────────────────────────────────────────

def _frontmatter(path: str, limit: int = 4000) -> Dict[str, str]:
    """Read the leading ``---`` block as flat ``key: value`` pairs.

    Deliberately not a YAML parse: skill frontmatter is a handful of scalar keys,
    and the values we want (name, description) are single-line. A malformed
    block yields {} rather than an exception, because one bad skill must not
    blank the list.
    """
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(limit)
    except OSError:
        return out
    if not head.startswith("---"):
        return out
    end = head.find("\n---", 3)
    if end == -1:
        return out
    for line in head[3:end].splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip("'\"")
        if key and val and key not in out:
            out[key] = val

    # Folded/literal block scalars (`description: >` followed by an indented
    # block) are in use, and a naive read returns the marker character as the
    # description. Join the indented lines instead.
    for key in list(out):
        if out[key] not in (">", "|", ">-", "|-", ""):
            continue
        body: List[str] = []
        seen_key = False
        for line in head[3:end].splitlines():
            if not seen_key:
                if line.strip().startswith(key + ":"):
                    seen_key = True
                continue
            if not line.strip():
                body.append("")
                continue
            if not line.startswith((" ", "\t")):
                break
            body.append(line.strip())
        joined = " ".join(p for p in body if p).strip()
        if joined:
            out[key] = joined
    return out


def skills_view(name: str) -> Dict[str, Any]:
    """Every skill this profile can load, grouped by category directory.

    Layout on disk is ``skills/<category>/<skill>/SKILL.md``, with the flat
    ``skills/<skill>/SKILL.md`` form also in use, so the category is derived from
    the relative depth rather than assumed.
    """
    base = profile_dir(name)
    if not base:
        return {"available": False, "reason": "profile directory not found",
                "count": 0, "categories": [], "skills": [], "truncated": False}

    root = os.path.join(base, "skills")
    if not os.path.isdir(root):
        return {"available": False, "reason": "no skills directory",
                "count": 0, "categories": [], "skills": [], "truncated": False}

    skills: List[Dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if "SKILL.md" not in filenames:
            continue
        path = os.path.join(dirpath, "SKILL.md")
        rel = os.path.relpath(path, root)
        parts = rel.split(os.sep)
        # Layouts in use: skills/<skill>/SKILL.md (flat) and the far more common
        # skills/<category>/<skill>/SKILL.md. Anything above the skill directory
        # is the category, so the first segment is it whenever there is one.
        category = parts[0] if len(parts) >= 3 else ""
        fm = _frontmatter(path)
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0
        skills.append({
            "name": fm.get("name") or parts[-2],
            "description": fm.get("description", ""),
            "category": category,
            "path": rel,
            "bytes": size,
            "mtime": mtime,
            "mtime_rel": _rel_time(mtime),
        })

    skills.sort(key=lambda s: (s["category"], s["name"].lower()))
    categories = sorted({s["category"] for s in skills if s["category"]})
    return {
        "available": True,
        "reason": "",
        "count": len(skills),
        "categories": categories,
        "skills": skills[:SKILL_CAP],
        "truncated": len(skills) > SKILL_CAP,
    }


# ── memory ───────────────────────────────────────────────────────────────────

def memory_view(name: str) -> Dict[str, Any]:
    """The profile's long-term memory files, verbatim.

    Memory is the one artefact where the character count IS the interesting
    number — both files carry a budget and get rejected when they overflow — so
    each file reports its length alongside its text.
    """
    base = profile_dir(name)
    if not base:
        return {"available": False, "reason": "profile directory not found", "files": []}

    root = os.path.join(base, "memories")
    if not os.path.isdir(root):
        return {"available": False, "reason": "no memories directory", "files": []}

    files: List[Dict[str, Any]] = []
    for fname in MEMORY_FILES:
        path = os.path.join(root, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        files.append({
            "name": fname,
            "chars": len(text),
            "lines": text.count("\n") + (1 if text else 0),
            "text": text[:MEMORY_CHAR_CAP],
            "truncated": len(text) > MEMORY_CHAR_CAP,
            "mtime": mtime,
            "mtime_rel": _rel_time(mtime),
        })

    return {
        "available": bool(files),
        "reason": "" if files else "no memory files yet",
        "files": files,
        "total_chars": sum(f["chars"] for f in files),
    }


# ── avatars ──────────────────────────────────────────────────────────────────

def avatar_dir() -> str:
    """Where uploaded profile pictures live (a mounted volume, not the image)."""
    return os.environ.get("LCP_AVATAR_DIR", "/app/data/avatars")


def avatar_for(name: str) -> Optional[Tuple[str, str]]:
    """Return ``(path, content_type)`` for a profile's avatar, or None."""
    name = validate_name(name)
    for ctype, (ext, _check) in AVATAR_TYPES.items():
        path = os.path.join(avatar_dir(), "%s.%s" % (name, ext))
        if os.path.isfile(path):
            return path, ctype
    return None


def save_avatar(name: str, content_type: str, data: bytes) -> Dict[str, Any]:
    """Validate and store a profile picture, replacing any existing one.

    The content type is *checked against the bytes*, not trusted: an upload is
    served back from this directory, so a mislabelled file would be a way to
    place arbitrary content on a page.
    """
    name = validate_name(name)
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype not in AVATAR_TYPES:
        raise ValueError("unsupported image type: %s" % (ctype or "none"))
    ext, matches = AVATAR_TYPES[ctype]
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("empty upload")
    if len(data) > AVATAR_MAX_BYTES:
        raise ValueError("image larger than %d KB" % (AVATAR_MAX_BYTES // 1024))
    if not matches(bytes(data)):
        raise ValueError("file content is not a valid %s" % ctype)

    os.makedirs(avatar_dir(), exist_ok=True)
    dest = os.path.join(avatar_dir(), "%s.%s" % (name, ext))
    tmp = dest + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(bytes(data))
    os.replace(tmp, dest)
    # One avatar per profile: clear the other extensions.
    for other_ctype, (other_ext, _m) in AVATAR_TYPES.items():
        if other_ext != ext:
            try:
                os.unlink(os.path.join(avatar_dir(), "%s.%s" % (name, other_ext)))
            except OSError:
                pass
    return {"ok": True, "profile": name, "content_type": ctype, "bytes": len(data)}


def delete_avatar(name: str) -> Dict[str, Any]:
    """Remove a profile picture. Reports whether anything was there."""
    name = validate_name(name)
    removed = []
    for _ctype, (ext, _m) in AVATAR_TYPES.items():
        path = os.path.join(avatar_dir(), "%s.%s" % (name, ext))
        try:
            os.unlink(path)
            removed.append(ext)
        except OSError:
            pass
    return {"ok": True, "profile": name, "removed": removed}


# ── helpers ──────────────────────────────────────────────────────────────────

# ── lane <-> agent profile ───────────────────────────────────────────────────
#
# A gateway lane and an agent profile are two different names for related things:
# "l2" is a routing decision, while "homelab-expert-l2" is a directory that owns
# skills, memory and a task tree. More than one agent profile can route through one
# lane (blog-writer also routes through l2), and the only place the link is written
# down is each profile's own config, where the gateway base URL ends in the lane.

_LANE_URL_RE = re.compile(
    r"(?:localhost|127\.0\.0\.1):\d+/([A-Za-z0-9._-]+)"   # http://127.0.0.1:8734/l2
    r"|lcp\.[A-Za-z0-9.-]+/([A-Za-z0-9._-]+)"             # https://lcp.local.curci.cc/l2
)
_CONFIG_READ_LIMIT = 64 * 1024


def lane_in_config_text(text: str, lanes) -> str:
    """The first URL segment in a profile's config that names a known lane.

    Reads every match rather than the first, because a config also points at the
    providers themselves (``…:8734/v1``), and those are not lanes.
    """
    order = [str(l) for l in (lanes or ()) if l]
    wanted = {l.lower(): l for l in order}
    if not wanted:
        return ""
    for m in _LANE_URL_RE.finditer(text or ""):
        seg = (m.group(1) or m.group(2) or "").lower()
        if seg in wanted:
            return wanted[seg]
    return ""


def agent_profiles(lanes=None) -> Dict[str, Dict[str, Any]]:
    """Every Hermes agent profile directory, with the lane it routes through.

    Only directories with a config.yaml count: this view is about profiles that own
    skills, memory and tasks, not about every directory under the profiles root
    (there is a ``backups/`` there that owns none of those).
    """
    root = profiles_root()
    out: Dict[str, Dict[str, Any]] = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(root, name)
        cfg = os.path.join(path, "config.yaml")
        if not os.path.isdir(path) or not os.path.isfile(cfg):
            continue
        lane = ""
        try:
            with open(cfg, encoding="utf-8", errors="replace") as fh:
                lane = lane_in_config_text(fh.read(_CONFIG_READ_LIMIT), lanes)
        except OSError:
            lane = ""
        out[name] = {"name": name, "lane": lane}
    return out


def primary_agent_for_lane(lane: str, agents) -> str:
    """Which agent profile's artefacts a lane shows when several share the lane.

    The lane's card is a summary, not the whole truth: every agent profile that
    shares the lane keeps a card of its own, so nothing is hidden by this choice.
    Preference is the house convention — the profile named after the lane
    (``homelab-expert-l2`` for ``l2``) — and lowercase wins over the legacy
    capitalised copies.
    """
    if not lane:
        return ""
    sharing = [n for n, info in (agents or {}).items() if (info or {}).get("lane") == lane]
    if not sharing:
        return ""
    exact = [n for n in sharing if n.lower().endswith("-" + lane.lower()) or n.lower() == lane.lower()]
    return sorted(exact or sharing, key=lambda n: (n != n.lower(), n))[0]


def agent_summary(name: str, words: int = 10) -> str:
    """A one-line summary of an agent profile, taken from its own SOUL.md.

    A profile's SOUL.md opens by saying what the agent is for, which is exactly the
    card's question. The leading heading and Markdown emphasis come off, and the
    second person goes with them — "Level 2 Support Agent for homelab-expert" reads
    as a description where "You are the Level 2 Support Agent" reads as an
    instruction. Nothing usable means an empty string: the card then says it has no
    description rather than inventing one.
    """
    path = profile_dir(name)
    if not path:
        return ""
    text = ""
    for candidate in ("SOUL.md", "AGENTS.md"):
        full = os.path.join(path, candidate)
        if os.path.isfile(full):
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read(8000)
            except OSError:
                text = ""
            if text:
                break
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "-", "*", "|", "```", "<!--")):
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line[:400])
        line = re.sub(r"[`\[\]]", "", line).strip()
        line = re.sub(r"^(?:You are|You're)\s+(?:the|an?)\s+", "", line, flags=re.IGNORECASE)
        parts = line.split()
        if len(parts) < 3:
            continue
        return " ".join(parts[:words]) + ("…" if len(parts) > words else "")
    return ""


def _rel_time(ts: float) -> str:
    """A short relative age for a timestamp ("3d", "2h", "just now")."""
    if not ts:
        return ""
    delta = max(0, int(time.time() - ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return "%dm" % (delta // 60)
    if delta < 86400:
        return "%dh" % (delta // 3600)
    return "%dd" % (delta // 86400)


def profile_paths(name: str) -> Dict[str, Any]:
    """The work-layer paths for one profile (its overrides over the defaults)."""
    from . import work_sources
    try:
        view = work_sources.resolved_view()
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": "%s: %s" % (type(e).__name__, e)}
    entry = (view.get("profiles") or {}).get(name)
    if not entry:
        return {"available": False, "reason": "profile not in the work sources view"}
    return {"available": True, "paths": entry,
            "profiles_dir": view.get("hermes_profiles_dir", ""),
            "ops_dir": view.get("ops_dir", "")}
