"""Work-layer: Tasks and Fleet views.

Same moment model as the Decisions view -- but the sources are the filesystem
(task directories) and the gateway health endpoint, not the board ledger.

A task directory is a small structured record whose *state is its location*
(new -> in_progress -> completed / cancelled). That makes ``moved`` a first-class
moment: the directory's mtime IS the transition time, and the destination IS the
new state. No extra bookkeeping, and it cannot drift from reality because the
location is the state.
"""

import html
import json
import os
import contextlib
import contextvars
import re
import sys
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional

from .logging_config import get_logger

logger = get_logger("lcp.work.tasks")

# Where the task trees live. Overridable so LCP can point at any profile.
DEFAULT_TASKS_DIR = "/root/.hermes/profiles/homelab-expert-l2/work/tasks"

STATES = ("new", "in_progress", "completed", "cancelled")

# How much of a PLAN.md / RESULTS.md to carry into the view. The plan is the
# task's document of record; the panel shows its head as a summary and the
# full (capped) text inside the expander. Truncation is flagged, not silent.
_PLAN_CAP = 8000
_RESULTS_CAP = 2000
# Assessments tab: how many ledger records to carry, and how much of each
# finding (the rest is explicitly flagged, never silently cut).
_ASSESS_CAP = 200
_ASSESS_SUMMARY_CAP = 2000


def _module_site_dirs() -> List[str]:
    """Candidate dirs for the RUNTIME-installed modules under the bind mount.

    The image is built lean (``WITH_ROUTER=0``), so markdown_it is NOT baked
    into it -- ``pip install --target <LCP_MODULES_DIR>/{site,router}`` puts it
    in the bind-mounted modules dir instead (it survives container recreation;
    the image does not carry it).
    """
    root = (os.environ.get("LCP_MODULES_DIR") or "").strip() or "/opt/lcp-modules"
    return [os.path.join(root, "site"), os.path.join(root, "router")]


@lru_cache(maxsize=1)
def _markdown_it():
    """The shared MarkdownIt instance, or ``None`` if markdown_it is missing.

    markdown_it has no build-time home, and the ONLY thing that used to put the
    modules dir on ``sys.path`` was the router classifier's lazy init
    (``task_classifier`` appends its site dir as a side effect of an unrelated
    job). Markdown rendering therefore depended on another component having run
    first: a freshly recreated container answered
    ``500 {"error": "No module named 'markdown_it'"}`` on ``/api/work/tasks``
    until the classifier happened to warm up (observed on lcp-staging
    2026-09-20). Resolve the path here so rendering stands on its own.
    """
    try:
        from markdown_it import MarkdownIt
    except ModuleNotFoundError:
        for cand in _module_site_dirs():
            if cand not in sys.path and os.path.isdir(cand):
                sys.path.append(cand)
        try:
            from markdown_it import MarkdownIt
        except ModuleNotFoundError:
            logger.warning(
                "markdown_it_unavailable",
                searched=_module_site_dirs(),
                detail="rendering plain text; the Tasks view must not 500 on it",
            )
            return None
    return MarkdownIt("commonmark", {"html": False, "linkify": False})


def _md_to_html(text: str) -> str:
    """CommonMark -> HTML with raw HTML escaped (html=False).

    Task files are agent-written, but they are still untrusted input as far
    as the browser is concerned; markdown-it with html=False renders any raw
    HTML inside the document as escaped text instead of pasting it through.

    Degrades to escaped plain text rather than raising: an optional runtime
    module must never take the whole page down (same rule as the classifier
    chips, which fail to "no chips" instead of a 500).
    """
    md = _markdown_it()
    if md is None:
        escaped = html.escape(text or "")
        return "<p>" + escaped.replace("\n", "<br>") + "</p>"
    return md.render(text)


def _plan_summary(plan_text: str) -> Optional[str]:
    """The first substantive line of a PLAN.md, minus plumbing.

    Skips headings and the front-matter meta lines (**Created:**, **Status:**,
    **Branch:**, **Owner:**) so the summary is what the task is actually about
    -- for most plans that is the first user-ask or problem sentence.
    """
    skip_meta = ("created:", "status:", "branch:", "owner:", "user ask")
    for raw in plan_text.splitlines():
        line = raw.strip().lstrip("*_`~ ").strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        low = line.lower()
        if low.startswith(skip_meta):
            continue
        return line[:320]
    return None


# The Tasks tab is per profile (M2c): the same views run against whichever tree
# the profile resolves to. A ContextVar rather than a module global because the
# server is threaded — one request's profile must not leak into another's — and
# rather than a parameter because a dozen helpers reach `tasks_dir()` on their own.
_ACTIVE_ROOT: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "lcp_tasks_root", default=None)


@contextlib.contextmanager
def use_root(root: str):
    """Point the whole task tree at ``root`` for the duration of the block."""
    token = _ACTIVE_ROOT.set(root)
    try:
        yield root
    finally:
        _ACTIVE_ROOT.reset(token)


def tasks_dir() -> str:
    """Resolve the task tree root: explicit override, env, then configured sources."""
    override = _ACTIVE_ROOT.get()
    if override:
        return override
    env = os.environ.get("LCP_WORK_TASKS_DIR")
    if env:
        return env
    try:
        from . import work_sources
        src = work_sources.load_sources()
        if src and src.get("tasks_root"):
            return src["tasks_root"]
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_TASKS_DIR


def todos_path() -> str:
    """The sibling todo.md for the same profile as the task tree.

    When a per-profile root is in force the ledger is derived from it. The
    ``LCP_WORK_TODO`` override names the *default* profile's ledger, so honouring
    it here would caption one profile's tasks with another profile's todo.md —
    which is exactly the kind of mismatch this view exists to prevent.
    """
    root = tasks_dir()
    override = os.environ.get("LCP_WORK_TODO")
    if override and _ACTIVE_ROOT.get() is None:
        return override
    return os.path.join(os.path.dirname(root), "todo.md")


def _classification_index() -> Optional[Dict[str, Any]]:
    """The work-layers classification index, if the batch has run.

    ``<tasks_root>/.work-layers/classifications.json`` is written by the
    deterministic classifier (bge-small centroids over a hand-authored
    taxonomy). Defensive: a missing or corrupt index simply means the panel
    shows no chips.
    """
    root = tasks_dir()
    p = os.path.join(root, ".work-layers", "classifications.json")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _state_summary(tdir: str) -> Dict[str, Any]:
    """STATE-SUMMARY.md (PLAN-vs-REAL, written by the summarizer batch)."""
    p = os.path.join(tdir, "STATE-SUMMARY.md")
    if not os.path.isfile(p):
        return {"text": None, "html": None, "present": False}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(4000)
    except OSError:
        return {"text": None, "html": None, "present": False}
    return {"text": text, "html": _md_to_html(text), "present": True}


_FACTS_CAP = 4000


def _session_facts(tdir: str) -> Dict[str, Any]:
    """SESSION-FACTS.md (mined from sessions by the 4h assessment round)."""
    p = os.path.join(tdir, "SESSION-FACTS.md")
    if not os.path.isfile(p):
        return {"text": None, "html": None, "present": False, "truncated": False}
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(_FACTS_CAP)
        truncated = os.path.getsize(p) > _FACTS_CAP
    except OSError:
        return {"text": None, "html": None, "present": False, "truncated": False}
    return {"text": text, "html": _md_to_html(text), "present": True, "truncated": truncated}


def _assessment_feed(limit: int = _ASSESS_CAP) -> List[Dict[str, Any]]:
    """Newest decisions from .work-layers/assessments.jsonl (append-only).

    The ledger is written by the 4h session-assessment round; a corrupt or
    absent file must never crash the view, so record-level tolerance is
    mandatory (one bad line is skipped, not fatal).

    The record is carried WHOLE (summary markdown-rendered, evidence session
    ids, the assessor actor, the skip reason) because the Assessments tab's job
    is to let the operator SEE what the round decided and why — the old 200-char
    one-liner hid exactly the part that explains a skipped decision.
    """
    root = tasks_dir()
    p = os.path.join(root, ".work-layers", "assessments.jsonl")
    if not os.path.isfile(p):
        return []
    records = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    records.sort(key=lambda r: r.get("ts", 0) or 0, reverse=True)
    out = []
    for r in records[:limit]:
        raw_summary = r.get("summary") or ""
        summary = raw_summary[:_ASSESS_SUMMARY_CAP]
        evidence = r.get("evidence")
        if evidence is None:
            evidence = r.get("evidence_sessions") or []
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        evidence = [str(e) for e in evidence if str(e).strip()]
        out.append({
            "ts": r.get("ts"),
            "ts_iso": time.strftime("%Y-%m-%d %H:%M",
                                    time.gmtime(r.get("ts", 0) or 0)),
            "action": r.get("action") or "?",
            "slug": r.get("slug"),
            "title": (r.get("title") or "").strip(),
            "summary": summary,
            "summary_html": _md_to_html(summary) if summary else None,
            "summary_truncated": len(raw_summary) > _ASSESS_SUMMARY_CAP,
            "n_evidence": len(evidence),
            "evidence": evidence[:8],
            "round_actor": r.get("round_actor"),
            "applied": bool(r.get("applied")),
            "reason": r.get("reason"),
        })
    return out


def assessments_view(records: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The Work › Tasks › Assessments tab: the session-assessment ledger.

    Returns the feed plus the rollup the tab's stat cards need (how many
    decisions the round reached, how many it actually applied, and the
    per-action split) — an assessment round that applied nothing is a signal
    worth seeing at a glance, not something to infer from a table.
    """
    if records is None:
        records = _assessment_feed()
    by_action: Dict[str, int] = {}
    for r in records:
        by_action[r["action"]] = by_action.get(r["action"], 0) + 1
    applied = sum(1 for r in records if r["applied"])
    return {
        "available": os.path.isdir(tasks_dir()),
        "records": records,
        "total": len(records),
        "applied": applied,
        "skipped": len(records) - applied,
        "by_action": dict(sorted(by_action.items(), key=lambda kv: -kv[1])),
    }


def _read_status_line(plan_path: str) -> Optional[str]:
    """Pull the bolded **Status:** line out of a PLAN.md, if present.

    PLAN.md files are prose, not a schema -- but every one written to the task
    standard carries a ``**Status:**`` line. Reading that one line is enough to
    show what a task believes about itself, and when it disagrees with the
    directory it lives in, that disagreement is worth showing rather than hiding.
    """
    try:
        with open(plan_path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4000)
    except OSError:
        return None
    m = re.search(r"\*\*Status:\*\*\s*(.+)", head)
    if not m:
        return None
    return m.group(1).strip().rstrip("*").strip()


def _moment_for(state: str, name: str, tdir: str,
                cls_tasks: Dict[str, Any], lite: bool = False) -> Dict[str, Any]:
    """Build one task moment for a directory.

    ``lite`` skips every heavy read (PLAN.md body + markdown, RESULTS.md,
    STATE-SUMMARY.md, SESSION-FACTS.md) — used by the lazy table so a page of
    rows costs ~1 small head-read per directory, not 5 full-file reads each.
    The detail endpoint uses the same builder for ONE directory.
    """
    try:
        st = os.stat(tdir)
    except OSError:
        raise FileNotFoundError(tdir)

    files: List[str] = []
    try:
        files = sorted(os.listdir(tdir))
    except OSError:
        pass

    plan = os.path.join(tdir, "PLAN.md")
    claimed = _read_status_line(plan) if os.path.isfile(plan) else None

    classification = None
    cls = cls_tasks.get(name)
    if cls and cls.get("label"):
        classification = {"label": cls["label"], "score": cls.get("score")}

    payload: Dict[str, Any] = {
        "state": state,
        "files": files,
        "n_files": len(files),
        "has_plan": os.path.isfile(plan),
        "claimed_status": claimed,
        "classification": classification,
        "status_conflict": bool(claimed) and _conflicts(claimed, state),
    }
    if lite:
        return {
            "id": "task:%s" % name,
            "key": "%s/%s" % (state, name),
            "t": st.st_mtime,
            "computed_at": st.st_mtime,
            "subject": name,
            "actor": "filesystem",
            "kind": "task.%s" % state,
            "payload": payload,
            "provenance": {"path": tdir},
        }

    # ── heavy reads (full payload for the detail endpoint) ──
    plan_text = None
    plan_truncated = False
    plan_summary = None
    plan_html = None
    if os.path.isfile(plan):
        try:
            with open(plan, "r", encoding="utf-8", errors="replace") as fh:
                plan_text = fh.read(_PLAN_CAP)
            plan_truncated = os.path.getsize(plan) > _PLAN_CAP
        except OSError:
            plan_text = None
        plan_summary = _plan_summary(plan_text) if plan_text else None
        plan_html = _md_to_html(plan_text) if plan_text else None

    results_path = os.path.join(tdir, "RESULTS.md")
    results_text = None
    results_truncated = False
    results_html = None
    if os.path.isfile(results_path):
        try:
            with open(results_path, "r", encoding="utf-8", errors="replace") as fh:
                results_text = fh.read(_RESULTS_CAP)
            results_truncated = os.path.getsize(results_path) > _RESULTS_CAP
        except OSError:
            results_text = None
        results_html = _md_to_html(results_text) if results_text else None

    payload.update({
        "state_summary": _state_summary(tdir),
        "session_facts": _session_facts(tdir),
        "plan_summary": plan_summary,
        "plan_text": plan_text,
        "plan_html": plan_html,
        "plan_truncated": plan_truncated,
        "results_text": results_text,
        "results_html": results_html,
        "results_truncated": results_truncated,
        "artifacts": [f for f in files
                      if f not in ("PLAN.md", "STATE-SUMMARY.md", "SESSION-FACTS.md")],
    })
    return {
        "id": "task:%s" % name,
        "t": st.st_mtime,
        "computed_at": st.st_mtime,
        "subject": name,
        "actor": "filesystem",
        "kind": "task.%s" % state,
        "payload": payload,
        "provenance": {"path": tdir},
    }


def task_moments(lite: bool = False) -> List[Dict[str, Any]]:
    """Every task directory as a moment, ordered newest transition first.

    ``lite`` skips the heavy per-task reads (see ``_moment_for``) — the lazy
    table path. Lite results are cached for a few seconds so rapid filter /
    pagination clicks don't re-walk the tree.
    """
    root = tasks_dir()
    if not os.path.isdir(root):
        return []

    if lite:
        now = time.time()
        if _LITE_CACHE["ts"] and now - _LITE_CACHE["ts"] < _LITE_CACHE_TTL:
            return _LITE_CACHE["moments"]

    cls_idx = _classification_index()
    cls_tasks = (cls_idx or {}).get("tasks") or {}

    moments: List[Dict[str, Any]] = []
    for state in STATES:
        sdir = os.path.join(root, state)
        if not os.path.isdir(sdir):
            continue
        for name in sorted(os.listdir(sdir)):
            tdir = os.path.join(sdir, name)
            if not os.path.isdir(tdir):
                continue
            try:
                moments.append(_moment_for(state, name, tdir, cls_tasks, lite=lite))
            except (OSError, FileNotFoundError):
                continue

    moments.sort(key=lambda m: m["t"] or 0, reverse=True)
    if lite:
        _LITE_CACHE["ts"] = time.time()
        _LITE_CACHE["moments"] = moments
    return moments


_LITE_CACHE: Dict[str, Any] = {"ts": 0, "moments": []}
_LITE_CACHE_TTL = 8


def _conflicts(claimed: str, state: str) -> bool:
    """Does a PLAN's own status line contradict the directory it sits in?

    Deliberately conservative -- only flags a clear contradiction, and only when
    the claim sits at the START of the status line. Substring matching anywhere
    in the line produces false positives: "in_progress — Phases 1-3 done" is a
    consistent status that happens to contain the word "done", and a detector
    that cries wolf on ten tasks is one nobody reads.
    """
    c = claimed.strip().lower()
    # Strip leading emphasis/markers so the comparison sees the actual claim.
    c = c.lstrip("*_`~ ").strip()

    if state == "completed":
        return c.startswith("in progress") or c.startswith("in_progress") or c.startswith("pending")
    if state == "in_progress":
        return c.startswith("completed") or c.startswith("done") or c.startswith("cancelled")
    if state == "cancelled":
        return c.startswith("completed") or c.startswith("done")
    if state == "new":
        return c.startswith("completed") or c.startswith("cancelled")
    return False


def _strip_html(text: str) -> str:
    """Rough HTML -> plain text for the search haystack (entity decode only
    for the common escapes; substring matching is tolerant)."""
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&#39;", "'")):
        t = t.replace(ent, ch)
    return t


def _haystack(m: Dict[str, Any]) -> str:
    """Plain-text corpus a search term is matched against."""
    p = m["payload"]
    return " ".join([
        m.get("subject") or "",
        p.get("state") or "",
        p.get("claimed_status") or "",
        (p.get("classification") or {}).get("label") or "",
        _strip_html(p.get("plan_html") or ""),
    ]).lower()


def tasks_view(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Assemble the Tasks view: counts per state plus the moment list.

    ``params`` drives the server-side filters/pagination (so search covers ALL
    tasks and every response only renders the current page's rows):
      states  comma list of STATES, or "all"        (default: in_progress)
      q       substring over subject/plan/state/tag/haystack
      tag     exact classifier label
      per     rows per page: int or "all"           (default: 20)
      page    1-based                                (default: 1)
    """
    root = tasks_dir()
    params = params or {}
    q = str(params.get("q") or "").lower().strip()
    # Search must see PLAN text (part of the haystack), which the lite fast
    # path does not read — queries fall back to the full walk.
    lite = (str(params.get("lite") or "").lower() in ("1", "true", "yes")
            and not q)
    moments = task_moments(lite=lite)
    cls_idx = _classification_index()

    if not moments:
        # A present-but-empty tree is a normal state, not an error, and it must still
        # carry the shape the template reads. This branch used to omit `filter`, so
        # the Tasks page 500'd on `view.filter.states_options` whenever the tree
        # existed but held no tasks (found 2026-09-25 reproducing a red CI run).
        view = empty_tasks_view(
            "no task directories found at %s" % root,
            "Set LCP_WORK_TASKS_DIR to the profile's work/tasks tree.")
        view["available"] = os.path.isdir(root)
        return view

    # ── filter + pagination params ──
    params = params or {}
    per_raw = str(params.get("per") or "20")
    if per_raw == "all":
        per = "all"
    else:
        try:
            per = max(1, min(500, int(per_raw)))
        except (TypeError, ValueError):
            per = 20
    states_raw = str(params.get("states") or "in_progress")
    if states_raw in ("", "all"):
        states = list(STATES)
    else:
        states = [s for s in states_raw.split(",") if s in STATES] or list(STATES)
    q = str(params.get("q") or "").lower().strip()
    tag = str(params.get("tag") or "").strip().lower()
    try:
        page = max(1, int(params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1

    # ── counts (unfiltered, drive the status cards) ──
    counts = {s: 0 for s in STATES}
    for m in moments:
        counts[m["payload"]["state"]] = counts.get(m["payload"]["state"], 0) + 1

    conflicts = [m for m in moments if m["payload"]["status_conflict"]]

    # ── apply filters ──
    filtered = []
    for m in moments:
        st = m["payload"]["state"]
        if st not in states:
            continue
        if tag:
            label = ((m["payload"].get("classification") or {}).get("label") or "").lower()
            if label != tag:
                continue
        if q and q not in _haystack(m):
            continue
        filtered.append(m)

    total_filtered = len(filtered)
    if per == "all":
        pages = 1
        page = 1
        tasks = filtered
    else:
        pages = max(1, -(-total_filtered // per))
        page = min(page, pages)
        start = (page - 1) * per
        tasks = filtered[start:start + per]

    # ── lite mode: row-only payload for the lazy-loaded table ──
    # The Tasks page no longer ships PLAN.md/RESULTS.md for every row; the
    # table fetches lite rows and pulls the heavy detail per-task on expand.
    if str(params.get("lite") or "").lower() in ("1", "true", "yes"):
        tasks = [_lite_moment(m) for m in tasks]

    # ── classification rollup ──
    by_label = (cls_idx or {}).get("by_label") or {}
    labels_meta = (cls_idx or {}).get("labels") or {}
    classified = sum(1 for p in (cls_idx or {}).get("tasks", {}).values() if p.get("label"))
    total_tasks = len(moments)

    return {
        "available": True,
        "empty": None,
        "counts": counts,
        "total": total_tasks,
        "classified": classified,
        "by_label": by_label,
        "labels_meta": labels_meta,
        "conflicts": conflicts,
        "tasks": tasks,
        "todos": _todo_view(),
        "filter": {
            "states": states,
            "states_options": list(STATES),
            "q": str(params.get("q") or ""),
            "tag": str(params.get("tag") or ""),
            "per": str(per),
            "page": page,
            "pages": pages,
            "total_filtered": total_filtered,
        },
    }


def empty_tasks_view(reason: str, hint: str = "") -> Dict[str, Any]:
    """The shape ``tasks_view`` promises, with nothing in it.

    The pages layer renders this when a read fails, so it has to satisfy the whole
    contract the template expects. A *leaf* key the template never dereferences can
    be left out; a missing *parent* cannot — ``sec_ptasks.html`` reads
    ``view.filter.states_options``, and an attribute lookup on an undefined value is
    the one thing Jinja raises on. That is how the "never blank the page on a data
    error" guard turned a handled read error into a 500 whenever the task tree was
    present but empty (found 2026-09-25 while reproducing a red CI run: the guard
    did not cover the shape it was guarding).

    One shape, two callers — the failure path here and the pages-layer fallback —
    so the two cannot drift apart again.
    """
    return {
        "available": False,
        "empty": {"reason": reason or "could not read the task tree", "hint": hint},
        # tasks tab
        "counts": {s: 0 for s in STATES},
        "total": 0,
        "classified": 0,
        "by_label": {},
        "labels_meta": {},
        "conflicts": [],
        "tasks": [],
        "todos": None,
        "filter": {
            "states": list(STATES),
            "states_options": list(STATES),
            "q": "",
            "tag": "",
            "per": 20,
            "page": 1,
            "pages": 1,
            "total_filtered": 0,
        },
        # assessments tab
        "records": [],
        "applied": 0,
        "skipped": 0,
        "by_action": {},
    }


def _todo_view() -> Optional[Dict[str, Any]]:
    """The profile's todo.md, parsed into a renderable overview.

    The ledger file is never rewritten (task-management standard), so this is
    purely a presentation parse: the ``> Updated:`` blockquote run becomes a
    timeline, ``##`` headings become sections, and each ``###`` task line
    becomes a bulleted item. Tables are skipped for the overview — the full
    content stays in the file. Truncation is flagged, never silent.
    """
    p = todos_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r"Updated:\s*([^\n]+)", text)
    out = {
        "path": p,
        "lines": text.count("\n") + 1,
        "updated": m.group(1).strip() if m else None,
        "open_boxes": text.count("- [ ]"),
        "done_boxes": text.count("- [x]"),
        "timeline": [],
        "sections": [],
    }
    section: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("> "):
            if len(out["timeline"]) >= _TODO_TIMELINE_CAP:
                continue
            body = s[2:].strip(" \t")
            # separator: em/en dash (or hyphen) followed by whitespace —
            # the date itself contains hyphens, so `—` without the \s+ would
            # swallow "2026-09-18" down to "2026".
            pm = re.match(r"^Updated[:：]?[\s　]*(.*?)[\s　]*[—–-][\s　]+(.*)$", body)
            if pm and pm.group(1):
                date = pm.group(1).strip()
                piece = pm.group(2).strip()
            else:
                date, piece = None, body
            out["timeline"].append({
                "date": date or "update",
                "text": piece,
                "html": _md_to_html(piece[:_TODO_ENTRY_CAP]),
                "truncated": len(piece) > _TODO_ENTRY_CAP,
            })
        elif s.startswith("## "):
            # NOTE: key is `entries`, not `items` — Jinja's attribute access
            # would resolve `sec.items` to the dict METHOD first.
            raw_title = s[3:].strip()
            # The section markers (🆕🔴🟡📋✅ …) are decorative; the overview
            # shows clean titles. `expanded` remembers the 🔴 marker because
            # "In Progress" is the one group that should open by default.
            expanded = raw_title.startswith("🔴")
            title = raw_title.lstrip("🆕🔴🟡📋✅⚪🟢").strip()
            # The All Tasks table below already covers these states with
            # filters/search — the todo.md groups are redundant noise.
            if title.lower().startswith(_TODO_SKIP_SECTIONS):
                section = None
                continue
            section = {"title": title, "expanded": expanded, "entries": []}
            out["sections"].append(section)
        elif s.startswith("### ") and section is not None:
            t = _strip_emoji(s[4:].strip())
            section["entries"].append({
                "text": t,
                "html": _md_to_html(t[:_TODO_ITEM_CAP]),
                "truncated": len(t) > _TODO_ITEM_CAP,
            })
        elif s.startswith("|") and section is not None:
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not cells or all(re.fullmatch(r":?-+:?", c) for c in cells):
                # |---|---| separator: the row just before it was the table
                # header — drop it so it never becomes an overview bullet.
                if section["entries"] and section["entries"][-1].get("table_row"):
                    section["entries"].pop()
                continue
            t = _strip_emoji(" · ".join(c for c in cells if c))
            if not t:
                continue
            section["entries"].append({
                "text": t,
                "html": _md_to_html(t[:_TODO_ITEM_CAP]),
                "truncated": len(t) > _TODO_ITEM_CAP,
                "table_row": True,
            })
    return out


def _lite_moment(m: Dict[str, Any]) -> Dict[str, Any]:
    """Row-only view of a task moment — NO PLAN/RESULTS/STATE payloads.

    Everything the lazy table needs to render a row (and the key to fetch the
    full detail on expand).
    """
    p = m["payload"]
    return {
        "id": "task:%s/%s" % (p["state"], m["subject"]),
        "key": "%s/%s" % (p["state"], m["subject"]),
        "t": m["t"],
        "computed_at": m["computed_at"],
        "subject": m["subject"],
        "kind": m["kind"],
        "payload": {
            "state": p["state"],
            "n_files": p["n_files"],
            "claimed_status": p["claimed_status"],
            "status_conflict": p["status_conflict"],
            "classification": p["classification"],
        },
    }


def task_detail(key: str) -> Dict[str, Any]:
    """The heavy payload for ONE task (fetched on row expand).

    ``key`` is ``<state>/<slug>`` — the lite row's ``key`` field. Strictly
    validated: state must be a known state and the slug must resolve inside
    the task tree (no traversal). Reads ONLY that directory — no full walk.
    """
    if "/" in key:
        state, slug = key.split("/", 1)
    else:
        state, slug = "", key
    if state not in STATES or not slug or not re.fullmatch(r"[A-Za-z0-9.-]+", slug):
        raise ValueError("invalid task key %r" % key)
    tdir = os.path.join(tasks_dir(), state, slug)
    if not os.path.isdir(tdir):
        raise FileNotFoundError("no task at %s" % key)

    cls_idx = _classification_index()
    cls_tasks = (cls_idx or {}).get("tasks") or {}
    try:
        return _moment_for(state, slug, tdir, cls_tasks)
    except FileNotFoundError:
        raise FileNotFoundError("task vanished: %s" % key)


_TODO_TIMELINE_CAP = 50
_TODO_ENTRY_CAP = 500
_TODO_ITEM_CAP = 180
# todo.md groups that duplicate the All Tasks state filters below — dropped.
# "queued" goes too: the Queued group is a dispatch buffer, not task state, and
# the operator asked for it off this page (2026-09-20).
_TODO_SKIP_SECTIONS = ("in progress", "new / pending", "completed", "queued")

# Decorated status markers used INSIDE task descriptions (✅ ⏸ 🆕 ⚠ …).
# Arrows (→) and other prose symbols are deliberately NOT in the set.
_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\U00002300-\U000023FF\U00002600-\U000027BF"
    r"\U00002B00-\U00002BFF\uFE0F\u200D\u20E3]+")


def _strip_emoji(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", text)).strip()
