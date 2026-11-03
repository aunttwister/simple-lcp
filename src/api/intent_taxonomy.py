"""Dynamic, per-user intent taxonomies.

WHY THIS EXISTS
---------------
The classifier shipped with ONE hard-coded label set: ``TASK_EXEMPLARS``, a module constant. That is
fine while a single operator owns every profile, but the measured traffic says otherwise — the
profiles are different users with different traffic, and three of them contain NO operator speech at
all (``coder``/``blog``/``career``), while the labelled corpus is 88% ``l2``. A single estate-wide
taxonomy fitted on one operator's messages describes none of those profiles' traffic.

So a taxonomy becomes DATA: versioned, owned by a profile, gradeable, and resolvable at request time.

THE ONE RULE THAT KEEPS THIS HONEST
-----------------------------------
A taxonomy may only be ``active`` if it carries a **grade** whose negative control passed. An
ungraded taxonomy is inert. Without that rule the estate accumulates N unmeasured classifiers and
nobody can say what any of them is worth.

SAFETY: DEFAULT BEHAVIOUR IS BYTE-IDENTICAL
-------------------------------------------
With nothing configured anywhere, resolution returns ``builtin-v0`` — whose exemplars ARE
``TASK_EXEMPLARS`` — so the classifier returns exactly what it returns today. A taxonomy that is not
``active`` is never resolvable, so merely inserting a row changes nothing.

RULES SCOPING (the known correctness risk, deliberately NOT guessed)
-------------------------------------------------------------------
``router.py`` emits ``task=<label>`` and the routing rules are keyed on that vocabulary. A taxonomy
whose labels differ from the shipped set would silently stop matching those rules. That decision is
an operator call, so this module refuses to make it quietly: an ``active`` taxonomy whose labels are
not a superset of the shipped vocabulary is still resolvable, but ``logical_labels_differ()`` reports
it and the operator-facing docs say so. Nothing else is assumed.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .logging_config import get_logger

logger = get_logger("lcp.router.taxonomy")

BUILTIN_ID = "builtin-v0"

# A centroid needs at least one vector, but a label with one or two exemplars is a coin flip. Below
# this an ``active`` taxonomy is refused — the point of the grade gate is that "enabled" means
# "measured", and a 1-exemplar label cannot be measured.
MIN_EXEMPLARS_PER_LABEL = 3

AXES = ("act", "subject")
STATUSES = ("draft", "active", "retired")


class TaxonomyError(ValueError):
    """A taxonomy that is structurally unusable. Never a warning: a malformed taxonomy must not
    silently degrade into 'classify everything as the majority label'."""


@dataclass
class IntentTaxonomy:
    """One labelled intent space, owned (optionally) by a profile."""

    id: str
    acts: dict[str, list[str]] = field(default_factory=dict)
    subjects: dict[str, list[str]] = field(default_factory=dict)
    buckets: dict[str, list[str]] = field(default_factory=dict)   # DECIDING projection: act -> [act]
    groups: dict[str, list[str]] = field(default_factory=dict)    # DESCRIBING projection
    display_name: str = ""
    owner: Optional[str] = None
    version: int = 1
    status: str = "draft"
    min_score: float = 0.35
    grade: Optional[dict] = None
    # Empty means "derive from what is defined": a taxonomy with only an act axis is legitimate, and
    # a hard default of ("act", "subject") made validate() reject every act-only taxonomy.
    axes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.axes:
            self.axes = [ax for ax, present in
                         (("act", bool(self.acts)), ("subject", bool(self.subjects))) if present]

    # ---------------- validation ----------------

    def validate(self) -> None:
        """Raise ``TaxonomyError`` on anything structurally unusable."""
        if not self.id or not str(self.id).strip():
            raise TaxonomyError("taxonomy id is empty")
        if self.status not in STATUSES:
            raise TaxonomyError(f"status {self.status!r} not in {STATUSES}")
        if not self.axes:
            raise TaxonomyError("axes is empty — a taxonomy with no axis cannot classify anything")
        for ax in self.axes:
            if ax not in AXES:
                raise TaxonomyError(f"unknown axis {ax!r} (known: {AXES})")
            labels = self.labels(ax)
            if not labels:
                raise TaxonomyError(f"axis {ax!r} has no labels")
            for label, ex in labels.items():
                if not str(label).strip():
                    raise TaxonomyError(f"axis {ax!r} has an empty label")
                if not isinstance(ex, list) or not any(str(e).strip() for e in ex):
                    raise TaxonomyError(f"{ax}.{label} has no usable exemplars")
        # A projection that names a label the axis does not define is a silent typo that would
        # produce an empty class at decode time.
        for bucket, members in (self.buckets or {}).items():
            for m in members:
                if m not in self.acts:
                    raise TaxonomyError(f"bucket {bucket!r} names unknown act {m!r}")
        for group, members in (self.groups or {}).items():
            for m in members:
                if m not in self.subjects:
                    raise TaxonomyError(f"group {group!r} names unknown subject {m!r}")

    def activation_problems(self) -> list[str]:
        """Reasons this taxonomy may NOT go ``active``. Empty list = allowed."""
        problems: list[str] = []
        if not self.grade:
            problems.append(
                "no grade attached — a taxonomy may only activate with a grade whose negative "
                "control passed (an ungraded taxonomy is an unmeasured classifier)"
            )
        elif not (self.grade.get("negative_control") or {}).get("passed"):
            problems.append("grade present but its negative control did not pass")
        for ax in self.axes:
            for label, ex in self.labels(ax).items():
                if len([e for e in ex if str(e).strip()]) < MIN_EXEMPLARS_PER_LABEL:
                    problems.append(
                        f"{ax}.{label} has < {MIN_EXEMPLARS_PER_LABEL} exemplars — too thin to centroid"
                    )
        return problems

    def can_activate(self) -> bool:
        return not self.activation_problems()

    # ---------------- accessors ----------------

    def labels(self, axis: str) -> dict[str, list[str]]:
        return self.acts if axis == "act" else self.subjects

    def exemplars(self, axis: str) -> dict[str, list[str]]:
        """The ``{label: [text, ...]}`` mapping the classifier builds centroids from."""
        return {k: [e for e in v if str(e).strip()] for k, v in self.labels(axis).items()}

    def logical_labels(self) -> set[str]:
        return set(self.acts) | set(self.subjects)

    def logical_labels_differ(self, other: "IntentTaxonomy") -> bool:
        """True when this taxonomy's label vocabulary is NOT covered by *other*'s.

        This is the 'rules scoping' warning: routing rules are keyed on the shipped ``task=``
        vocabulary, so a taxonomy bringing new labels needs its own rule scope or those rules stop
        matching. Reported, never silently fixed.
        """
        return not self.logical_labels().issubset(other.logical_labels())

    # ---------------- (de)serialisation ----------------

    def to_dict(self) -> dict:
        return {
            "id": self.id, "display_name": self.display_name, "owner": self.owner,
            "version": self.version, "status": self.status, "axes": list(self.axes),
            "acts": self.acts, "subjects": self.subjects,
            "buckets": self.buckets, "groups": self.groups,
            "min_score": self.min_score, "grade": self.grade,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "IntentTaxonomy":
        known = {f for f in cls.__dataclass_fields__}          # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------------------
# the built-in taxonomy: today's hard-coded exemplars, expressed as data
# --------------------------------------------------------------------------------------

def builtin_v0() -> IntentTaxonomy:
    """``TASK_EXEMPLARS`` as a taxonomy, so the default path is one mechanism, not two.

    Imported lazily: ``task_classifier`` must not import this module at import time or the two
    become circular.
    """
    from .task_classifier import TASK_EXEMPLARS  # local import on purpose (see docstring)
    return IntentTaxonomy(
        id=BUILTIN_ID,
        display_name="Built-in task types",
        owner=None,
        version=1,
        status="active",
        axes=["act"],
        acts={k: list(v) for k, v in TASK_EXEMPLARS.items()},
        subjects={},
        buckets={},
        groups={},
        min_score=0.35,
        # The built-in label set is the shipped routing vocabulary and its behaviour is the status
        # quo, so it does not need the new-taxonomy grade gate. Marked so that is explicit.
        grade={"kind": "builtin", "note": "shipped vocabulary; behaviour is the status quo",
               "negative_control": {"passed": True}},
    )


# --------------------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intent_taxonomies (
  id              TEXT PRIMARY KEY,
  owner_profile   TEXT,
  version         INTEGER NOT NULL DEFAULT 1,
  status          TEXT NOT NULL DEFAULT 'draft',
  display_name    TEXT,
  spec            TEXT NOT NULL,
  grade           TEXT,
  embedding_model TEXT NOT NULL DEFAULT 'BAAI/bge-small-en-v1.5',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_intent_taxonomies_owner ON intent_taxonomies(owner_profile);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaxonomyStore:
    """SQLite-backed store for taxonomies.

    Deliberately tolerant: a missing table (pre-migration image) raises ``TaxonomyError`` up to the
    resolver, which falls back to the built-in taxonomy — so an unmigrated database degrades to
    today's behaviour instead of breaking routing.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def ensure_schema(self) -> None:
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def get(self, taxonomy_id: str) -> Optional[IntentTaxonomy]:
        try:
            with self._connect() as con:
                row = con.execute(
                    "SELECT spec, grade, version, status FROM intent_taxonomies WHERE id = ?",
                    (taxonomy_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise TaxonomyError(f"taxonomy store unreadable: {exc}") from exc
        if not row:
            return None
        spec = json.loads(row["spec"])
        tax = IntentTaxonomy.from_dict(spec)
        tax.version = int(row["version"] or tax.version)
        tax.status = row["status"] or tax.status
        tax.grade = json.loads(row["grade"]) if row["grade"] else tax.grade
        return tax

    def list_all(self, status: Optional[str] = None) -> list[IntentTaxonomy]:
        try:
            with self._connect() as con:
                if status:
                    rows = con.execute(
                        "SELECT spec, grade, version, status FROM intent_taxonomies WHERE status = ?",
                        (status,)).fetchall()
                else:
                    rows = con.execute(
                        "SELECT spec, grade, version, status FROM intent_taxonomies").fetchall()
        except sqlite3.Error as exc:
            raise TaxonomyError(f"taxonomy store unreadable: {exc}") from exc
        out = []
        for row in rows:
            t = IntentTaxonomy.from_dict(json.loads(row["spec"]))
            t.version = int(row["version"] or t.version)
            t.status = row["status"] or t.status
            t.grade = json.loads(row["grade"]) if row["grade"] else t.grade
            out.append(t)
        return out

    def save(self, tax: IntentTaxonomy) -> None:
        tax.validate()
        self.ensure_schema()
        with self._connect() as con:
            existed = con.execute(
                "SELECT version FROM intent_taxonomies WHERE id = ?", (tax.id,)).fetchone()
            if existed:
                # Any spec change is a version bump, so a decision can name the exact taxonomy
                # revision that produced it.
                tax.version = max(tax.version, int(existed["version"]) + 1)
            with con:
                con.execute(
                    """INSERT INTO intent_taxonomies
                       (id, owner_profile, version, status, display_name, spec, grade, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         owner_profile=excluded.owner_profile, version=excluded.version,
                         status=excluded.status, display_name=excluded.display_name,
                         spec=excluded.spec, grade=excluded.grade, updated_at=excluded.updated_at""",
                    (tax.id, tax.owner, tax.version, tax.status, tax.display_name,
                     json.dumps(tax.to_dict()), json.dumps(tax.grade) if tax.grade else None,
                     _now(), _now()),
                )

    def activate(self, taxonomy_id: str) -> IntentTaxonomy:
        """Move ``draft -> active``. REFUSES without a passing grade."""
        tax = self.get(taxonomy_id)
        if tax is None:
            raise TaxonomyError(f"no taxonomy {taxonomy_id!r}")
        problems = tax.activation_problems()
        if problems:
            raise TaxonomyError(
                f"taxonomy {taxonomy_id!r} cannot activate: " + "; ".join(problems))
        tax.status = "active"
        self.save(tax)
        return tax


# --------------------------------------------------------------------------------------
# resolution — the per-user part
# --------------------------------------------------------------------------------------

def _db_path() -> Optional[str]:
    try:
        from .config import get_config
        cfg = get_config()
        return ((getattr(cfg, "database", None) or {}).get("path")
                or os.environ.get("LCP_DB_PATH") or None)
    except Exception:  # noqa: BLE001 — resolution must never break a request
        return None


def _profile_config(config: Any, profile: Optional[str]) -> dict:
    if not profile or config is None:
        return {}
    try:
        return (config.profiles.get(profile) or {})
    except Exception:  # noqa: BLE001
        return {}


def resolve_for_profile(
    profile: Optional[str] = None,
    config: Any = None,
    store: Optional[TaxonomyStore] = None,
) -> IntentTaxonomy:
    """Resolve the taxonomy that governs *profile*.

    Order: ``profiles.<p>.intent_taxonomy`` -> ``router.default_taxonomy`` -> built-in.

    Every failure mode falls back to the built-in taxonomy rather than raising, because this sits in
    the request path: a missing table, an unknown id, or a malformed spec must degrade to today's
    behaviour, not break routing. Failures are logged with the reason.
    """
    if config is None:
        try:
            from .config import get_config
            config = get_config()
        except Exception:  # noqa: BLE001
            config = None

    assigned = (_profile_config(config, profile) or {}).get("intent_taxonomy")
    if not assigned:
        try:
            router_cfg = (getattr(config, "plugins", None) or {}).get("router") or {}
        except Exception:  # noqa: BLE001
            router_cfg = {}
        assigned = router_cfg.get("default_taxonomy")
    if not assigned:
        return builtin_v0()

    if store is None:
        path = _db_path()
        if not path:
            logger.warning("intent_taxonomy_store_unavailable", assigned=assigned)
            return builtin_v0()
        store = TaxonomyStore(path)

    try:
        tax = store.get(str(assigned))
    except TaxonomyError as exc:
        logger.warning("intent_taxonomy_load_failed", assigned=assigned, error=str(exc)[:200])
        return builtin_v0()

    if tax is None:
        logger.warning("intent_taxonomy_unknown", assigned=assigned)
        return builtin_v0()
    if tax.status != "active":
        # An inert taxonomy is the whole point of the draft state: resolvable, but not in force.
        logger.info("intent_taxonomy_not_active", assigned=assigned, status=tax.status)
        return builtin_v0()
    try:
        tax.validate()
    except TaxonomyError as exc:
        logger.warning("intent_taxonomy_invalid", assigned=assigned, error=str(exc)[:200])
        return builtin_v0()
    return tax
