"""Phase 3 of CUR-270: backfill the identity columns on historical rows.

Fills ONLY NULLs with what resolve() says served each row, using each row's own
timestamp. Idempotent: a second run finds nothing to do. Nothing else is touched
- the raw provider/model strings stay exactly as recorded.

Reads through the same model_identity.stamp() the live write path uses, so a
backfilled row and a freshly written one are resolved by identical rules.

  --dry-run   (default) plans, reports, writes nothing
  --apply     writes, in batches, committing each batch
  --limit N   only consider the N oldest unstamped rows (a safe live trial)
  --undo      set every identity column back to NULL (restores the pre-phase-2
              state; nothing reads these columns yet, so no behaviour changes)

Run from the repo root:

    .venv/bin/python scripts/backfill_model_identity.py --db <path>            # plan
    .venv/bin/python scripts/backfill_model_identity.py --db <path> --apply    # write

Measured on prod 2026-10-30: 126,317 request rows + 65,225 routing-decision rows
in 6.5 s, batched at 2,000 with a 50 ms pause. Safe on a live DB (WAL): the app
keeps serving and writing throughout. A re-run writes 0 rows.

Two shapes of half-known row, both handled by writing only the side that is
known and leaving the other NULL rather than skipping the row:
  * a first-choice routing decision has no from-side at all (22,496 sides here)
  * 2,233 request rows are the unknown/unknown writer defect
"""

import argparse
import collections
import os
import sqlite3
import sys
import time

# Import the resolver from THIS checkout, wherever it is — staging and prod are
# separate repositories, so an absolute path would silently resolve one repo's
# registry against the other's data.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from src.api import model_identity as mi  # noqa: E402


# table -> (timestamp column, [(columns this side writes, provider col, model col)])
SPEC = {
    "requests": ("timestamp", [
        (("deployment_id", "release_id"), "provider", "model"),
    ]),
    "routing_decisions": ("ts", [
        (("from_deployment_id",), "from_provider", "from_model"),
        (("to_deployment_id",), "provider", "model"),
    ]),
}


def _value(col: str, ident: dict):
    """The value a resolved identity puts in *col*."""
    if col == "release_id":
        return ident["release_id"]
    return ident["deployment_id"]           # deployment_id / from_ / to_ all hold the deployment


def _plan(con, table, ts_col, sides, limit=None):
    """Decide, per row, which columns to write and to what. Writes nothing."""
    where = " AND ".join(f"{c[0]} IS NULL" for c, _, _ in sides)
    q = f"SELECT id, {ts_col} AS ts, " + ", ".join(
        f"{p} AS p{i}, {m} AS m{i}" for i, (_, p, m) in enumerate(sides)) + \
        f" FROM {table} WHERE {where} ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"

    cache: dict = {}
    updates = []
    stats = collections.Counter()
    reasons = collections.Counter()
    for r in con.execute(q):
        pending: dict = {}
        got = 0
        for i, (cols, _p, _m) in enumerate(sides):
            key = (r[f"p{i}"], r[f"m{i}"], r["ts"][:10])
            if key not in cache:
                cache[key] = mi.stamp(key[0], key[1], at=key[2] + "T00:00:00+00:00", con=con)
            ident = cache[key]
            # These columns only exist for a DEPLOYMENT. A string that resolves to a
            # logical model but no deployment (a slug match, e.g. "deepseek-flash" on an
            # endpoint that never served it) has nothing to record here - writing
            # registry_version anyway would claim a resolution we do not have, which is
            # exactly what "NULL means could not tell" forbids.
            if ident.get("deployment_id") is None:
                if ident.get("model_id") is not None:
                    stats["model_only_side"] += 1
                else:
                    stats["unresolved_side"] += 1
                    reasons[(r[f"p{i}"], r[f"m{i}"], ident["unresolved_reason"])] += 1
                continue
            got += 1
            stats["resolved_side"] += 1
            stats["by_" + (ident["matched_by"] or "?")] += 1
            for c in cols:
                v = _value(c, ident)
                if v is not None:
                    pending[c] = v
        if not pending:
            stats["rows_none"] += 1
        elif got == len(sides):
            stats["rows_all_sides"] += 1
        else:
            stats["rows_partial"] += 1
        updates.append((pending, r["id"]))
    stats["groups"] = len(cache)
    return updates, stats, reasons


def _apply(con, table, updates, batch, pause):
    """Write each row's pending columns, grouped by signature so one statement per shape."""
    rv = mi.registry_version(con)
    by_sig: dict = collections.defaultdict(list)
    for pending, rid in updates:
        if not pending:
            continue
        sig = tuple(sorted(pending))
        by_sig[sig].append(tuple(pending[c] for c in sig) + (rv, rid))

    n = 0
    for sig, rows in sorted(by_sig.items()):
        sets = ", ".join(f"{c}=?" for c in sig) + ", registry_version=?"
        sql = f"UPDATE {table} SET {sets} WHERE id=?"
        for i in range(0, len(rows), batch):
            con.executemany(sql, rows[i:i + batch])
            con.commit()
            if pause:
                time.sleep(pause / 1000.0)
        print(f"  {' + '.join(sig):42} {len(rows):>7} rows")
        n += len(rows)
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--pause", type=int, default=50, help="ms between batches")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    con = sqlite3.connect(a.db, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")

    if a.undo:
        print("undoing: setting every identity column back to NULL")
        for t, cols in (("requests", ("deployment_id", "release_id", "registry_version")),
                        ("routing_decisions", ("from_deployment_id", "to_deployment_id",
                                               "registry_version"))):
            cur = con.execute(f"UPDATE {t} SET " + ", ".join(f"{c}=NULL" for c in cols))
            con.commit()
            print(f"  {t}: {cur.rowcount} rows cleared")
        con.close()
        return

    print(f"=== {'APPLY' if a.apply else 'DRY RUN'} on {a.db} ===")
    print(f"registry_version: {mi.registry_version(con)}"
          + (f"   (limited to {a.limit} rows)" if a.limit else ""))
    print()

    t0 = time.time()
    for table, (ts_col, sides) in SPEC.items():
        updates, stats, reasons = _plan(con, table, ts_col, sides, a.limit)
        total_sides = stats["resolved_side"] + stats["unresolved_side"]
        print(f"--- {table} ---")
        print(f"  rows considered      : {len(updates)}")
        print(f"  sides resolved       : {stats['resolved_side']}/{total_sides}"
              + (f" ({100.0*stats['resolved_side']/total_sides:.1f}%)" if total_sides else ""))
        print(f"  identity groups      : {stats['groups']} distinct (endpoint, model, day)")
        print(f"  rows every side known : {stats['rows_all_sides']}")
        print(f"  rows only some known  : {stats['rows_partial']}")
        print(f"  rows nothing known    : {stats['rows_none']}  (left NULL on purpose)")
        if stats["model_only_side"]:
            print(f"  model-but-no-deployment sides (nothing to record): {stats['model_only_side']}")
        for k in sorted(stats):
            if k.startswith("by_"):
                print(f"     {k[3:]:16} {stats[k]}")
        for (p, m, why), n in reasons.most_common(6):
            print(f"     UNRESOLVED {n:6d}  {p} / {m}  ({why})")
        if a.apply:
            print(f"  WRITTEN              : {_apply(con, table, updates, a.batch, a.pause)} rows")
        print()

    if a.apply:
        print("=== verification ===")
        for t, cols in (("requests", ("deployment_id", "release_id")),
                        ("routing_decisions", ("from_deployment_id", "to_deployment_id"))):
            tot = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for c in cols:
                st = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {c} IS NOT NULL").fetchone()[0]
                print(f"  {t:18} {c:20} {st:>7}/{tot} ({100.0*st/tot:.1f}%)")
            rvn = con.execute(
                f"SELECT COUNT(*) FROM {t} WHERE registry_version IS NOT NULL").fetchone()[0]
            print(f"  {t:18} {'registry_version':20} {rvn:>7}/{tot} ({100.0*rvn/tot:.1f}%)")
        bad = 0
        checked = 0
        for r in con.execute("SELECT provider, model, timestamp, deployment_id FROM requests"
                             " WHERE deployment_id IS NOT NULL LIMIT 5000"):
            checked += 1
            x = mi.resolve(r["provider"], r["model"], at=r["timestamp"], con=con)
            if x.deployment_id != r["deployment_id"]:
                bad += 1
        print(f"  sample re-resolve disagreements : {bad} of {checked}")
    print(f"\nelapsed {time.time()-t0:.1f}s")
    con.close()


if __name__ == "__main__":
    main()
