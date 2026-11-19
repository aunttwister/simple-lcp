"""CSV import for the capability matrix — design build step 4.

One row per ``(model, task_type)``, tall rather than wide, so the file carries the axis
it means explicitly instead of depending on column order. Same shape as the ratings
importer (``model_ratings.import_csv``), deliberately: two importers that disagree about
headers is how a file that works in one pane silently fails in the other.

Two gates, both the operator's standing rules:

* **Only registered models are written.** Registration is the predicate every read path
  uses — ``mi_models.status='active' AND mi_deployments.status='active'``
  (``model_ratings.VISIBLE_PREDICATE``). A row for anything else is SKIPPED and counted.
  Writing a score for an unregistered model would put a number on a page that is never
  allowed to display it.
* **Only the eight routing axes are accepted as a task.** An unknown axis is an ERROR,
  not a skipped row: the router reads these keys and nothing else, so a typo would
  create a score nobody consults while the operator believes it landed.

Scores accept 0–100 or 0–1 and are normalised to 0–1, matching the manual editor.

Rows default to ``source='import'``, a source of their own: an import must not clobber
the benchmark rows (``livebench``/``lmarena``/``lcp_benchmark``) nor the hand-entered
ones (``manual``), and none of those may clobber it.

**An optional ``source`` column names the provenance instead, when the file IS that
source.** Found 2026-11-19: ``'import'`` is not in ``GRADING_SOURCES``, so an imported row
is dropped by the grading view — the operator imports a file, the matrix does not change,
and nothing says why. Published-leaderboard numbers ARE ``livebench``, and that is also
what makes them visible and gradeable, so the column allows an explicit, ALLOWLISTED
source. An unknown source is a whole-file ERROR for the same reason an unknown axis is:
a typo would make every row invisible while the operator believes it landed.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any, Optional

SOURCE = "import"

#: Column aliases, lowercased. Order in the file is free.
MODEL_COLS = ("model", "model_slug", "logical_name", "slug")
TASK_COLS = ("task_type", "task", "axis", "intent")
SCORE_COLS = ("score", "value", "capability")
RELEASE_COLS = ("release", "release_label")
#: Optional provenance. Absent → ``SOURCE`` (the safe default).
SOURCE_COLS = ("source", "provenance")


def routing_axes() -> tuple:
    """The task types the router actually reads — nothing else is a valid axis."""
    from .message_norm import TASK_SIGNALS
    return tuple(TASK_SIGNALS) + ("casual_chat",)


def _pick(fields: dict, names: tuple) -> Optional[str]:
    for n in names:
        if n in fields:
            return fields[n]
    return None


def allowed_sources() -> tuple:
    """The sources that may grade a model. Import must never invent one."""
    from .seed_capabilities import GRADING_SOURCES
    return tuple(GRADING_SOURCES)


def parse(text: str, registered: set, axes: tuple,
          sources: Optional[tuple] = None) -> dict:
    """Validate a capability CSV against the registry and the routing axes.

    Pure: no writes, no connection. Returns what WOULD be written plus every row that
    was not, with the reason — an import that silently drops rows is worse than one
    that refuses, because the operator reads the matrix and believes it.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    fields = {f.strip().lower(): f for f in reader.fieldnames if f and f.strip()}

    model_col = _pick(fields, MODEL_COLS)
    task_col = _pick(fields, TASK_COLS)
    score_col = _pick(fields, SCORE_COLS)
    release_col = _pick(fields, RELEASE_COLS)
    source_col = _pick(fields, SOURCE_COLS)
    # The default (`import`) is accepted even though it cannot GRADE: it is the
    # no-provenance default and an import must stay possible without naming a source.
    # Anything else must be a grading source, because a row a reader cannot see is a
    # row that did not land.
    allowed = tuple(sources) if sources is not None else tuple(allowed_sources()) + (SOURCE,)

    missing = [name for name, col in (("model", model_col), ("task_type", task_col),
                                      ("score", score_col)) if not col]
    if missing:
        raise ValueError(
            "CSV is missing required column(s): " + ", ".join(missing) +
            " — need model, task_type, score")

    rows, skipped, errors = [], [], []
    for i, raw in enumerate(reader, start=2):   # 2 = first data line, 1 is the header
        # The model cell is taken VERBATIM — never stripped, lowercased or otherwise
        # re-spelled. Stripping would make " glm-5.3-flash" match the registered
        # `glm-5.3-flash`, which is a spelling decision made outside the identity
        # authority (tests/test_model_identity_guard.py enforces exactly this). A cell
        # that does not match a registered slug exactly is SKIPPED and named in the
        # report, so the file gets fixed rather than silently reinterpreted.
        model = raw.get(model_col) or ""
        task = (raw.get(task_col) or "").strip()
        score = (raw.get(score_col) or "").strip()
        if not (model or task or score):
            continue                            # a blank line is not an error
        if not model or not task or not score:
            errors.append(f"line {i}: empty model/task_type/score")
            continue
        # A provenance the grading view would DROP is not a warning: it is a file that
        # writes rows nobody can see. Refuse it, and name the ones that work.
        row_source = ((raw.get(source_col) or "").strip() if source_col else "") or SOURCE
        if row_source not in allowed:
            errors.append(f"line {i}: source '{row_source}' is not a grading source "
                          f"(one of: {', '.join(allowed)}) — rows with it would be "
                          f"invisible on the matrix and unused by the router")
            continue
        if task not in axes:
            errors.append(f"line {i}: '{task}' is not a routing axis "
                          f"(one of: {', '.join(axes)})")
            continue
        try:
            raw_score = float(score)
        except ValueError:
            errors.append(f"line {i}: score '{score}' is not a number")
            continue
        if not registered or model not in registered:
            skipped.append(f"line {i}: {model} is not a registered model")
            continue
        normalized = raw_score / 100.0 if raw_score > 1.0 else raw_score
        rows.append({
            "model": model,
            "task_type": task,
            "score": round(max(0.0, min(1.0, normalized)), 4),
            "raw_score": raw_score,
            "release_label": (raw.get(release_col) or "").strip() if release_col else "",
            "source": row_source,
        })
    return {"rows": rows, "skipped": skipped, "errors": errors}


def import_capabilities(con: sqlite3.Connection, text: str, *,
                        ax_override: Optional[tuple] = None) -> dict:
    """Parse and upsert. Returns a report the UI shows verbatim.

    ``errors`` are fatal to the import (nothing is written): a file with a bad axis or a
    non-numeric score is a file the operator must fix, and writing the good half would
    make the matrix look complete when it is not. ``skipped`` rows are not fatal — they
    are unregistered models, which the caller is not permitted to write anyway.
    """
    from .model_ratings import visible_models
    from .model_names import _now

    axes = ax_override or routing_axes()
    registered = visible_models(con)
    out = parse(text, registered, axes)
    if out["errors"]:
        return {"written": 0, "models": [], "axes": [],
                "skipped": out["skipped"], "errors": out["errors"],
                "aborted": True}

    now = _now()
    written = 0
    for r in out["rows"]:
        label = r["release_label"] or now[:10]
        src = r.get("source") or SOURCE
        existing = con.execute(
            "SELECT id FROM model_capabilities"
            " WHERE model = ? AND task_type = ? AND source = ? AND release_label = ?",
            (r["model"], r["task_type"], src, label)).fetchone()
        if existing:
            con.execute(
                "UPDATE model_capabilities SET score = ?, raw_score = ?,"
                " updated_at = ? WHERE id = ?",
                (r["score"], r["raw_score"], now, existing[0]))
        else:
            con.execute(
                "INSERT INTO model_capabilities"
                " (model, task_type, score, source, raw_score, release_label, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r["model"], r["task_type"], r["score"], src, r["raw_score"],
                 label, now))
        written += 1
    con.commit()
    return {
        "written": written,
        "models": sorted({r["model"] for r in out["rows"]}),
        "axes": sorted({r["task_type"] for r in out["rows"]}),
        "sources": sorted({(r.get("source") or SOURCE) for r in out["rows"]}),
        "skipped": out["skipped"],
        "errors": [],
        "aborted": False,
    }


def template() -> str:
    """The downloaded template — the header, plus one line that shows the two shapes of
    ``score`` (0–1 and 0–100) so nobody has to guess which the file wants."""
    axes = routing_axes() or ("code_generation",)
    second = axes[1] if len(axes) > 1 else axes[0]
    return ("model,task_type,score,release\n"
            f"glm-5.3-flash,{axes[0]},0.87,\n"
            f"glm-5.3-flash,{second},87,2026-11-17\n")


__all__ = ["parse", "import_capabilities", "template", "routing_axes", "allowed_sources",
           "SOURCE", "MODEL_COLS", "TASK_COLS", "SCORE_COLS", "RELEASE_COLS", "SOURCE_COLS"]
