"""Keep one model per line at its NEWEST release — the operator's policy A.

Why this exists
---------------
The arena board moves with the vendor. The day Opus 5.5 ships, LMArena scores
"Claude Opus 5.5" and stops scoring "Claude Opus 5" — so a registry still
carrying `claude-opus-5` shows a stale number under a name the operator no longer
recognises, or falls off every board entirely. The operator sees "opus 5" on a
leaderboard and says so, correctly. The fix is not a display filter: it is to
register the release the provider actually serves.

Two read-only inputs
--------------------
* **the provider catalog.** The model-intel poll stages a `mi_deployments` row
  with `status='new'` for every served id it does not recognise. That staging is
  the catalogue: `claude-opus-5-5` sitting at the same endpoint as our registered
  `claude-opus-5` is the successor, and its served id is exactly what a
  registration needs.
* **the arena names that do not resolve** (`arena_poll.release_candidates`) — the
  display spelling the poll must be able to MAP onto the model we register. A
  registration without an alias is a model the poll can never find.

What a successor IS
-------------------
A served id is split into (family tokens, release, variant tokens):

    claude-opus-5      -> ('claude','opus')  (5,)    ()
    claude-opus-5-5    -> ('claude','opus')  (5,5)   ()
    gemini-3.8-flash   -> ('gemini',)        (3,8)   ('flash',)
    minimax-m3.1-flash -> ('minimax',)       (3,1)   ('flash',)

A candidate is a successor only when family **and** variant are identical and the
release is HIGHER. Requiring the variant to match is what keeps `gpt-6.1-sol`
from being read as a successor of `gpt-5.6-luna`, and `MiniMax-M3.1-Flash` from
being read as a plain M3.1. When the highest version of a line is picked, the
lower ones are reported as skipped — 4.6 does not get registered when the
provider already serves 4.7.

Nothing here writes until `apply()`, and `apply()` returns a receipt whose inverse
is `undo_sql(plan)`, written to disk BEFORE the write.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .arena_poll import _version, line_key, norm, split_effort

ACTIVE = "active"
STAGED = "new"
RETIRED = "retired"
LMARENA_SOURCE = "lmarena"

#: A release token: `5`, `5.5`, `v4.1`, and the vendor-lettered `m3.1` / `k3.1`
#: / `qwen3.8`. Only ONE release token is taken per name; anything numeric after
#: it is a continuation only when it looks like a version component (see below).
_VERSION_TOKEN = re.compile(r"^[a-z]*(\d+(?:\.\d+)*)$")
_NUMERIC = re.compile(r"^\d+(?:\.\d+)*$")
#: A 4+-digit run is a DATE or a build stamp, not a release part: `Grok 4 (0709)`,
#: `Qwen3.8-Max-0902`, `DeepSeek-V4.1-Flash-20261017`. Merging one into the
#: release would make a dated snapshot read as a LATER release and register it —
#: so it stays a variant token, which by definition cannot match the plain name.
_DATEISH = re.compile(r"^\d{4,}$")
_SEP = re.compile(r"[^a-z0-9.]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def last_segment(served_id: str) -> str:
    """`moonshotai/Kimi-K3` -> `Kimi-K3` (the vendor prefix is not the model)."""
    return (served_id or "").strip().rsplit("/", 1)[-1]


def slug_for_served_id(served_id: str) -> str:
    """The registry slug a served id should get: dots kept, everything else `-`."""
    s = _SEP.sub("-", last_segment(served_id).lower())
    return re.sub(r"-{2,}", "-", s).strip("-")


def identity(served_id: str) -> tuple[tuple[str, ...], tuple[int, ...], tuple[str, ...]]:
    """(family tokens, release, variant tokens) for a served id.

    `claude-opus-5-5` -> (('claude','opus'), (5,5), ()) — a trailing bare number
    continues the release rather than starting the variant, which is what makes
    `-5-5` read as 5.5 and not as "release 5, variant 5".

    A vendor-lettered token splits at its digits when the letters are a word
    (`qwen3.8` -> family 'qwen', release 3.8) but stays a pure release when the
    letter is a version marker (`m3.1`, `k3.1`, `v4.1`). Without that split every
    vendor-lettered name would have an EMPTY family, and `mistral3.9-max` would
    read as a successor of `qwen3.8-max`.
    """
    toks = [t for t in _SEP.split(last_segment(served_id).lower()) if t]
    family: list[str] = []
    release: tuple[int, ...] = ()
    variant: list[str] = []
    seen_release = False
    for t in toks:
        m = _VERSION_TOKEN.match(t)
        if m and not seen_release:
            letters, digits = t[:len(t) - len(m.group(1))], m.group(1)
            if len(letters) >= 2:                   # 'qwen3.8' -> family 'qwen'
                family.append(letters)
            release = tuple(int(x) for x in digits.split("."))
            seen_release = True
        elif seen_release and _NUMERIC.match(t) and not _DATEISH.match(t):
            release = release + tuple(int(x) for x in t.split("."))
        elif not seen_release:
            family.append(t)
        else:
            variant.append(t)
    return tuple(family), release, tuple(variant)


def _gt(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    """Tuple compare padded to equal width: (5,5) > (5,), (6,) > (5,6)."""
    if not a or not b:
        return False
    w = max(len(a), len(b))
    return a + (0,) * (w - len(a)) > b + (0,) * (w - len(b))


def display_name_for(served_id: str) -> str:
    """`claude-opus-5-5` -> `Claude Opus 5 5`, `google/gemini-3.8-flash` -> `Gemini 3.8 Flash`."""
    toks = [t for t in _SEP.split(last_segment(served_id)) if t]
    out: list[str] = []
    for t in toks:
        if t[:1].isalpha() and not _NUMERIC.match(t):
            out.append(t[:1].upper() + t[1:])
        else:
            out.append(t)
    return " ".join(out)


# ── reading the catalog ─────────────────────────────────────────────────────

def _row(con: sqlite3.Connection) -> sqlite3.Connection:
    if con.row_factory is None:
        con.row_factory = sqlite3.Row
    return con


def catalog_successors(con: sqlite3.Connection) -> dict[str, Any]:
    """Staged served ids that are a NEWER release of a line we already serve.

    Compared per endpoint (a provider's catalog is the thing being read) and per
    line. The winner per line is the highest release; lower releases of the same
    line are reported under ``skipped`` rather than registered.
    """
    _row(con)
    # Anchors: any line a provider we poll puts on its catalog, whatever the
    # status of THAT row. A model with no confirmed deployment still names a line
    # we carry — CommandCode serving `claude-opus-5-5` beside a stale
    # `claude-opus-5` is exactly the case this exists for.
    current = [dict(r) for r in con.execute(
        "SELECT d.endpoint_id, d.served_id, d.status AS deployment_status,"
        " m.model_id, m.slug, m.family"
        " FROM mi_deployments d"
        " JOIN mi_models m ON m.model_id = d.model_id"
        " JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
        " WHERE m.status = ? AND e.status = ?",
        (ACTIVE, ACTIVE))]
    staged = [dict(r) for r in con.execute(
        "SELECT d.deployment_id, d.endpoint_id, d.served_id, e.slug AS endpoint_slug"
        " FROM mi_deployments d"
        " JOIN mi_endpoints e ON e.endpoint_id = d.endpoint_id"
        " WHERE d.status = ? AND d.model_id IS NULL AND e.status = ?",
        (STAGED, ACTIVE))]

    by_endpoint: dict[int, list[dict]] = {}
    for c in current:
        by_endpoint.setdefault(c["endpoint_id"], []).append(c)

    candidates: list[dict] = []
    skipped: list[dict] = []
    for s in staged:
        fam, ver, var = identity(s["served_id"])
        for c in by_endpoint.get(s["endpoint_id"], []):
            cfam, cver, cvar = identity(c["served_id"])
            if fam != cfam or var != cvar:
                continue
            if not _gt(ver, cver):
                continue
            candidates.append({
                "served_id": s["served_id"], "endpoint_id": s["endpoint_id"],
                "endpoint_slug": s["endpoint_slug"], "deployment_id": s["deployment_id"],
                "slug": slug_for_served_id(s["served_id"]),
                "display_name": display_name_for(s["served_id"]),
                "family": c["family"], "release": ".".join(str(x) for x in ver),
                "version": ver, "variant": var,
                "supersedes_slug": c["slug"], "supersedes_model_id": c["model_id"],
                "anchor_confirmed": c["deployment_status"] == ACTIVE,
                "supersedes_version": cver})

    best: dict[tuple, dict] = {}
    for cand in sorted(candidates, key=lambda c: c["version"]):
        key = (cand["endpoint_id"], cand["family"], cand["variant"], cand["supersedes_slug"])
        prev = best.get(key)
        if prev is None or _gt(cand["version"], prev["version"]):
            if prev is not None:
                skipped.append({**prev, "reason": f"a newer release of the same line "
                                                   f"({cand['slug']}) is served"})
            best[key] = cand
        else:
            skipped.append({**cand, "reason": f"a newer release of the same line "
                                              f"({prev['slug']}) is served"})
    return {"register": list(best.values()), "skipped": skipped}


# ── pairing the arena's spelling ────────────────────────────────────────────

def arena_aliases(register: Iterable[dict], arena_candidates: Iterable[dict]) -> dict[str, list[str]]:
    """slug -> the arena display names that mean it (effort suffix stripped).

    Pairing is on the arena's own line+release against the new slug's, so
    'Claude Opus 5.5 (High)' and '... (Max)' both land on `claude-opus-5-5` as
    the single base name 'Claude Opus 5.5' — the effort is a dimension, not a
    second name.
    """
    out: dict[str, list[str]] = {}
    arena = [dict(a) for a in arena_candidates]
    for r in register:
        want_line, want_ver = line_key(r["slug"]), _version(r["slug"])
        names: list[str] = []
        for a in arena:
            name = a.get("arena_name") or ""
            if not name:
                continue
            if norm(name) == norm(r["slug"]) or norm(name) == norm(last_segment(r["served_id"])):
                names.append(split_effort(name)[0])
                continue
            if line_key(name) == want_line and _version(name) == want_ver:
                names.append(split_effort(name)[0])
        out[r["slug"]] = sorted(set(names))
    return out


# ── the plan ────────────────────────────────────────────────────────────────

def plan(con: sqlite3.Connection, arena_candidates: Optional[Iterable[dict]] = None,
         *, register: Optional[Iterable[dict]] = None) -> dict[str, Any]:
    """{'register': [{... 'aliases': [...]}], 'retire': [...], 'skipped': [...]}.

    ``arena_candidates`` accepts `arena_poll.release_candidates(...)['candidates']`
    so a caller that already fetched the boards does not fetch them twice, and so
    a test needs no network at all.
    """
    found = catalog_successors(con)
    reg = list(register) if register is not None else found["register"]
    aliases = arena_aliases(reg, arena_candidates or [])
    for r in reg:
        r["aliases"] = aliases.get(r["slug"], [])

    _row(con)
    existing = {r[0] for r in con.execute("SELECT slug FROM mi_models")}
    retire: list[dict] = []
    for r in reg:
        if r["slug"] in existing:
            r["already_registered"] = True
            continue
        r["already_registered"] = False
        retire.append({"slug": r["supersedes_slug"], "model_id": r["supersedes_model_id"],
                       "replaced_by": r["slug"]})

    no_alias = [r["slug"] for r in reg if not r["aliases"]]
    return {"register": reg, "retire": retire, "skipped": found["skipped"],
            "unaliased": no_alias}


def describe(p: dict[str, Any]) -> str:
    """The plan as the operator reads it."""
    lines: list[str] = []
    reg = [r for r in p["register"] if not r.get("already_registered")]
    lines.append(f"register {len(reg)} newer release(s):")
    for r in sorted(reg, key=lambda r: r["slug"]):
        al = ", ".join(repr(a) for a in r["aliases"]) or "NO ARENA NAME MATCHED"
        note = "" if r.get("anchor_confirmed", True) else \
            "  [the line had NO confirmed deployment — this registers the release\n" \
            "       the provider actually serves for it]"
        lines.append(f"  {r['supersedes_slug']} -> {r['slug']}  (release {r['release']}, "
                     f"{r['endpoint_slug']} served id {r['served_id']!r})")
        lines.append(f"      arena names: {al}{note}")
    lines.append(f"retire {len(p['retire'])} superseded release(s): "
                 + ", ".join(f"{r['slug']} (-> {r['replaced_by']})" for r in p["retire"]))
    if p["skipped"]:
        lines.append(f"skipped {len(p['skipped'])} lower release(s) of the same line:")
        for s in sorted(p["skipped"], key=lambda s: s["slug"])[:12]:
            lines.append(f"  {s['slug']} — {s['reason']}")
    already = [r["slug"] for r in p["register"] if r.get("already_registered")]
    if already:
        lines.append(f"already registered: {', '.join(sorted(already))}")
    if p["unaliased"]:
        lines.append(f"warning: no arena name matched for {', '.join(sorted(p['unaliased']))} "
                     f"— these register but the poll cannot score them")
    return "\n".join(lines)


# ── the write ───────────────────────────────────────────────────────────────

def undo_sql(p: dict[str, Any]) -> str:
    """The inverse of `apply`, as SQL to run BEFORE trusting the new state."""
    reg = [r for r in p["register"] if not r.get("already_registered")]
    stmts: list[str] = [f"-- undo of the release normalisation planned {_now()}"]
    for r in p["retire"]:
        stmts.append(f"UPDATE mi_models SET status='active' WHERE slug={r['slug']!r};")
        stmts.append("UPDATE mi_deployments SET status='active' WHERE model_id="
                     f"{r['model_id']} AND status='retired';")
    for r in reg:
        stmts.append("UPDATE mi_deployments SET model_id=NULL, status='new', verified_at=NULL"
                     f" WHERE endpoint_id={r['endpoint_id']} AND served_id={r['served_id']!r};")
        for name in r["aliases"]:
            stmts.append(f"DELETE FROM mi_model_names WHERE name={name!r} AND source_id=(SELECT"
                         " source_id FROM mi_sources WHERE slug='lmarena');")
        stmts.append(f"DELETE FROM mi_deployments WHERE model_id=(SELECT model_id FROM mi_models"
                     f" WHERE slug={r['slug']!r});")
        stmts.append(f"DELETE FROM mi_models WHERE slug={r['slug']!r};")
    return "\n".join(stmts) + "\n"


def apply(con: sqlite3.Connection, p: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Register the successors, alias them to the arena's spelling, retire the old.

    One transaction. Returns {'registered': [...], 'aliased': n, 'retired': [...],
    'model_ids': {slug: id}}. `dry_run` computes everything and writes nothing.
    """
    _row(con)
    src = con.execute("SELECT source_id FROM mi_sources WHERE slug=?",
                      (LMARENA_SOURCE,)).fetchone()
    if src is None:
        raise RuntimeError(f"no mi_sources row for {LMARENA_SOURCE!r}")
    lmarena = src["source_id"] if isinstance(src, sqlite3.Row) else src[0]

    receipt: dict[str, Any] = {"registered": [], "aliased": 0, "retired": [],
                               "model_ids": {}, "dry_run": dry_run}
    reg = [r for r in p["register"] if not r.get("already_registered")]
    savepoint = "release_normalize"
    con.execute(f"SAVEPOINT {savepoint}")
    try:
        for r in reg:
            model_id = con.execute(
                "SELECT model_id FROM mi_models WHERE slug=?", (r["slug"],)).fetchone()
            if model_id is None:
                cur = con.execute(
                    "INSERT INTO mi_models (slug, display_name, family, status,"
                    " supersedes_model_id, benchmark_key, created_at, active_release)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (r["slug"], r["display_name"], r["family"], ACTIVE,
                     r["supersedes_model_id"], r["slug"], _now(), r["release"]))
                model_id = cur.lastrowid
            else:
                model_id = model_id[0]
            receipt["model_ids"][r["slug"]] = model_id

            # The staged row IS the registration: claim it instead of inserting a
            # second row for the same (endpoint, served_id).
            con.execute(
                "UPDATE mi_deployments SET model_id=?, status=?, verified_at=?,"
                " notes='confirmed successor from the provider catalog'"
                " WHERE endpoint_id=? AND served_id=? AND model_id IS NULL",
                (model_id, ACTIVE, _now(), r["endpoint_id"], r["served_id"]))

            for name in r["aliases"]:
                exists = con.execute(
                    "SELECT id FROM mi_model_names WHERE source_id=? AND name=?",
                    (lmarena, name)).fetchone()
                if exists:
                    con.execute("UPDATE mi_model_names SET model_id=? WHERE id=?",
                                (model_id, exists[0]))
                else:
                    con.execute(
                        "INSERT INTO mi_model_names (model_id, source_id, name, is_primary,"
                        " confidence, evidence, first_seen) VALUES (?,?,?,0,?,?,?)",
                        (model_id, lmarena, name, "high",
                         "release normalisation: newest release of the line", _now()))
                    receipt["aliased"] += 1
            receipt["registered"].append({"slug": r["slug"], "model_id": model_id,
                                          "aliases": r["aliases"]})

        for r in p["retire"]:
            con.execute("UPDATE mi_models SET status=? WHERE model_id=? AND slug=?",
                        (RETIRED, r["model_id"], r["slug"]))
            con.execute("UPDATE mi_deployments SET status=? WHERE model_id=? AND status=?",
                        (RETIRED, r["model_id"], ACTIVE))
            receipt["retired"].append(r["slug"])

        if dry_run:
            con.execute(f"ROLLBACK TO {savepoint}")
            con.execute(f"RELEASE {savepoint}")
        else:
            con.execute(f"RELEASE {savepoint}")
            con.commit()
    except Exception:
        con.execute(f"ROLLBACK TO {savepoint}")
        con.execute(f"RELEASE {savepoint}")
        raise
    return receipt
