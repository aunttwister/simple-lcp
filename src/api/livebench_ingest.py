"""LiveBench official leaderboard -> ``model_capabilities`` (the poll source that was missing).

livebench.ai publishes each release as **static files**, so there is nothing to scrape and
no API to call::

    https://livebench.ai/v1/table_<release>.csv       one row per model, one column per TASK
    https://livebench.ai/v1/categories_<release>.json suite -> the task columns in that suite

This reads those two artifacts and aggregates the task columns into LiveBench's own suites
(their grouping, taken from their own file, not re-derived here).

Mapping (LiveBench suite -> gateway task axis), the estate's convention:

    Coding -> code_generation          Mathematics -> reasoning_chain
    Agentic Coding -> agentic_multi_step
    Data Analysis -> research_deep     Language -> casual_chat
    IF -> planning

``Reasoning`` is deliberately **not** mapped. The estate takes *Mathematics* for
``reasoning_chain``: ``claude-fable-5`` stores 96.0, which is its Mathematics score, while its
Reasoning score (89.7) is stored nowhere. Mapping both would have the two suites fight over one
axis and silently pick whichever ran last, so the unused one is excluded by name.

``debugging`` and ``unit_tests`` are NOT written: ``seed_capabilities.DERIVED_TASKS`` already
derives them from ``code_generation`` on read, so writing them would duplicate a rule that
belongs in one place.

Why the 1.0 line and not "the newest release": LiveBench 2.0 (2026-11-06) is a different
benchmark — Long Agentic / Reasoning / Coding / Browser / Data Analysis / Research / Design,
with no Mathematics, Instruction Following or Agentic Coding — so it cannot fill
``reasoning_chain``, ``planning`` or ``agentic_multi_step``, and its Coding is bug-fix tasks from
real merged PRs, not the same measurement. The newest release that maps to this matrix is 1.0's
last, 2026-06-25. Polling that is still "the latest" for the 1.0 line, because LiveBench keeps
adding models to its current release: a September model legitimately appears in it.

Identity: a row is written ONLY for a name that resolves to a canonical model through the
registry's own routes (``resolve_canonical``) — slug, alias, served id or benchmark key.
Anything else is recorded in the identity queue instead, because a capability row keyed by a
spelling no read path searches is a dead row: it can never be joined and it makes the estate look
better covered than it is.

Provenance: ``source='livebench'``, ``release_label`` = the release the numbers were read from —
the *vintage of the scores*, which is what that column means (see ``models.ModelCapability``).
Upsert keyed on (model, task_type, source='livebench'), so re-runs refresh rather than duplicate.
Never touches ``lmarena`` / ``lcp_benchmark`` / ``manual`` rows, and therefore never overwrites a
value a human entered or another source measured.
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .model_identity import _rowify
from .model_identity_queue import (
    KIND_CAPABILITY_NAME,
    SOURCE_LIVEBENCH,
    observe,
    resolve_canonical,
)

#: The LiveBench 1.0 release to poll. 2.0 redefines the suites and cannot fill this estate's
#: axes (see the module docstring), so the poll pins the 1.0 line; bump it when 1.0 gains a
#: release. LiveBench keeps adding models to the current release, so this is not stale data.
RELEASE = "2026-06-25"

BASE = "https://livebench.ai/v1"
URL_TABLE = BASE + "/table_{slug}.csv"
URL_CATEGORIES = BASE + "/categories_{slug}.json"


def _slug(release: str) -> str:
    """The release's filename form. The release LABEL is hyphenated (``2026-06-25``, and that
    is what goes in ``release_label``) but the artifacts are underscored
    (``table_2026_06_25.csv``). Substituting the label straight into the path requests
    ``table_2026-06-25.csv``, which 404s — measured: that cost a real 404 that was initially
    misread as a flaky host, because a 404 was being retried."""
    return release.replace("-", "_")

#: LiveBench suite -> gateway task axis. ``Reasoning`` is intentionally absent.
SUITE_TO_TASK = {
    "Coding": "code_generation",
    "Mathematics": "reasoning_chain",
    "Agentic Coding": "agentic_multi_step",
    "Data Analysis": "research_deep",
    "Language": "casual_chat",
    "IF": "planning",
}

#: LiveBench suite -> the key the estate stores in ``benchmark_category``, matching the
#: bundled seed's own vocabulary (coding, math, agentic_coding, ...).
SUITE_KEY = {
    "Coding": "coding",
    "Mathematics": "math",
    "Agentic Coding": "agentic_coding",
    "Data Analysis": "data_analysis",
    "Language": "language",
    "IF": "instruction_following",
}

_UA = "lcp-livebench-ingest"

#: Statuses worth retrying. livebench.ai answers **503** under a burst of requests from one
#: host — measured: 12 sequential fetches 1s apart were 12/12 clean, while rapid repeats got
#: 503. A poll that dies on the first 503 records "LiveBench had no data" when the truth is
#: "we asked too fast".
#:
#: 404 is deliberately NOT retried. Retrying it cost real time here: a genuine 404 (a wrong
#: artifact path) was masked as a flaky host and re-attempted three times. A 404 means the
#: release does not exist, and re-asking cannot change that.
_RETRY_CODES = frozenset({429, 500, 502, 503, 504})


def _get(url: str, timeout: float = 45.0, attempts: int = 3, backoff: float = 2.0) -> str:
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — fixed https
                return r.read().decode("utf-8-sig")
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRY_CODES:
                raise
            last = exc
        except urllib.error.URLError as exc:  # DNS blip, reset connection
            last = exc
        if i + 1 < attempts:
            time.sleep(backoff * (i + 1))
    assert last is not None
    raise last


def fetch_release(release: str = RELEASE):
    """(categories, rows) for a release: the suite->tasks file and one dict per model."""
    slug = _slug(release)
    cats = json.loads(_get(URL_CATEGORIES.format(slug=slug)))
    rows = list(csv.DictReader(io.StringIO(_get(URL_TABLE.format(slug=slug)))))
    return cats, rows


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def suite_scores(cats: dict, rows: list) -> dict:
    """{model: {suite: mean}} — the aggregation the leaderboard itself documents: "each cell
    is the model's mean task score for the suite, 0-100, from a single run".

    Only blank / ``n/a`` cells are skipped. A task the run *could not* measure scores 0 in
    LiveBench's own table and is a real 0 here, not a gap — dropping it would inflate the mean.
    Verified: with this rule the means reproduce the figures livebench.ai displays for both
    models imported on 2026-11-19, exactly.
    """
    out: dict = {}
    for r in rows:
        name = (r.get("model") or "").strip()
        if not name:
            continue
        per: dict = {}
        for suite, tasks in cats.items():
            if not isinstance(tasks, list):
                continue  # e.g. 2.0's "_overall_weights"
            vals = [_num(r.get(t)) for t in tasks]
            vals = [v for v in vals if v is not None]
            if vals:
                per[suite] = round(sum(vals) / len(vals), 1)
        if per:
            out[name] = per
    return out


def ingest(con: sqlite3.Connection, *, release: str = RELEASE, dry_run: bool = False,
           fetched=None) -> dict:
    """Pull a LiveBench release and upsert capability rows for the models it resolves.

    ``fetched`` accepts a pre-fetched ``(categories, rows)`` pair so the network stays out of
    the tests; production passes nothing and the artifacts are downloaded.

    Returns {release, models_in_release, models_matched, rows, queued, axes, release_label}.
    """
    # The identity helpers read rows BY COLUMN NAME, so a bare sqlite3.connect(...) handed in
    # here raises "tuple indices must be integers" the moment resolve_canonical runs. _rowify is
    # idempotent, so normalising here protects every entry point (scheduler, HTTP route, tests).
    con = _rowify(con)
    cats, raw_rows = fetched if fetched is not None else fetch_release(release)
    scores = suite_scores(cats, raw_rows)
    now = datetime.now(timezone.utc).isoformat()

    matched = queued = written = 0
    axes: set = set()
    cur = None
    if not dry_run:
        cur = con.cursor()
        cur.execute("BEGIN")
    for name in sorted(scores):
        per = scores[name]
        canonical = resolve_canonical(con, name)
        if not canonical:
            queued += 1
            if not dry_run:
                observe(con, name, source=SOURCE_LIVEBENCH, kind=KIND_CAPABILITY_NAME,
                        evidence={"release": release, "suites": sorted(per)})
            continue
        matched += 1
        for suite, value in per.items():
            task = SUITE_TO_TASK.get(suite)
            if not task:
                continue  # `Reasoning` — excluded by name, see the module docstring
            # Recorded before the dry-run check: the dry run IS the operator's preview, and a
            # preview that cannot say which axes it would touch is not a preview.
            axes.add(task)
            if dry_run:
                continue
            # Upsert on (model, task_type, source='livebench') only: the DELETE is scoped to
            # this source so a re-run can never clobber another source's or a human's row.
            cur.execute(
                "DELETE FROM model_capabilities"
                " WHERE model=? AND task_type=? AND source='livebench'",
                (canonical, task))
            cur.execute(
                "INSERT INTO model_capabilities (model, task_type, score, source,"
                " benchmark_category, raw_score, updated_at, release_label)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (canonical, task, round(value / 100.0, 4), "livebench",
                 SUITE_KEY.get(suite, suite.lower()), value, now, release))
            written += 1
    if not dry_run:
        con.commit()
    return {"release": release, "models_in_release": len(scores), "models_matched": matched,
            "rows": written, "queued": queued, "axes": sorted(axes), "release_label": release}


if __name__ == "__main__":
    import sys

    db = sys.argv[1] if len(sys.argv) > 1 else "data/costs.db"
    rel = sys.argv[2] if len(sys.argv) > 2 else RELEASE
    c = sqlite3.connect(db)
    print(json.dumps(ingest(c, release=rel), indent=1))
