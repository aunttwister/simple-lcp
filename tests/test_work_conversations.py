"""work_conversations tests — schema, backfill grouping, stamping, detail."""

import json
import os
import sqlite3

import pytest

from src.api import work_conversations as wc


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "costs.db"
    monkeypatch.setenv("LCP_COSTS_DB", str(path))
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            profile TEXT NOT NULL DEFAULT 'unknown', model TEXT NOT NULL,
            provider TEXT NOT NULL, prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0, cache_hit_tokens INTEGER DEFAULT 0,
            cache_miss_tokens INTEGER DEFAULT 0, cost REAL DEFAULT 0.0,
            latency_ms INTEGER DEFAULT 0, success INTEGER DEFAULT 1,
            error_type TEXT, tools_blocked TEXT, error_detail TEXT);
        CREATE TABLE routing_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts VARCHAR NOT NULL,
            profile VARCHAR NOT NULL, task VARCHAR NOT NULL, policy VARCHAR NOT NULL,
            action VARCHAR NOT NULL, provider VARCHAR, model VARCHAR, score FLOAT,
            rules_json TEXT, from_provider VARCHAR, from_model VARCHAR, note TEXT,
            path VARCHAR, keyword VARCHAR, intent_text TEXT, semantic_json TEXT,
            min_score FLOAT, sem_available BOOLEAN);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    con.commit()
    return con


def _req(con, ts, profile="l2", model="m", provider="p", **kw):
    con.execute(
        "INSERT INTO requests (timestamp, profile, model, provider, success, "
        "prompt_tokens, completion_tokens, cost, latency_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, profile, model, provider, kw.get("success", 1),
         kw.get("pt", 10), kw.get("ct", 5), kw.get("cost", 0.01),
         kw.get("lat", 100)))
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def _route(con, ts, profile="l2", task="research", action="keep_default", **kw):
    """Insert a routing decision.

    ``intent=`` is the classified user instruction. It replaced the old
    ``conversation_json=`` blob in simplify-lcp M7: that blob was 73% of the
    DB, and the instruction it carried is already stored here.
    """
    con.execute(
        "INSERT INTO routing_decisions (ts, profile, task, policy, action, "
        "provider, model, score, intent_text) VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, profile, task, "policy", action, kw.get("provider", "opencode"),
         kw.get("model", "m"), kw.get("score", 0.8),
         kw.get("intent", "")))


class TestStampNew:
    def test_stamp_request_and_routing_window(self, db):
        rid = _req(db, "2026-09-19T10:00:00+00:00", lat=100)
        _route(db, "2026-09-19T10:00:05+00:00")
        _route(db, "2026-09-19T10:30:00+00:00")  # outside window — untouched
        db.commit()
        wc.stamp_new(request_id=rid, conversation_id="sess-abc",
                     profile="l2", ts="2026-09-19T10:00:00+00:00")
        assert db.execute("SELECT conversation_id FROM requests WHERE id=?",
                          (rid,)).fetchone()[0] == "sess-abc"
        rt = [r[0] for r in db.execute(
            "SELECT conversation_id FROM routing_decisions ORDER BY id").fetchall()]
        assert rt[0] == "sess-abc"
        assert rt[1] is None  # outside the ±30s window

    def test_does_not_overwrite_existing(self, db):
        wc.ensure_schema()
        rid = _req(db, "2026-09-19T10:00:00+00:00")
        db.execute("UPDATE requests SET conversation_id=? WHERE id=?",
                   ("existing", rid))
        db.commit()
        wc.stamp_new(rid, "new", "l2", "2026-09-19T10:00:00+00:00")
        assert db.execute("SELECT conversation_id FROM requests WHERE id=?",
                          (rid,)).fetchone()[0] == "existing"


class TestLogTableModuleWiring:
    """Every log view pages and sorts through src/ui/tables.py.

    These pin the two things the shared module promises: newest-first by
    default, and a page window that does not overlap or drop rows.
    """

    @pytest.fixture
    def rows(self, db):
        wc.ensure_schema()   # adds conversation_id to both log tables
        for i in range(5):
            _req(db, "2026-09-19T10:0%d:00+00:00" % i, profile="l2", model="a")
        db.commit()
        return db

    def test_requests_default_is_newest_first(self, rows):
        v = wc.requests_view({"per": "2"})
        assert v["total"] == 5
        assert [r["timestamp"] for r in v["rows"]] == [
            "2026-09-19T10:04:00+00:00", "2026-09-19T10:03:00+00:00"]
        assert v["filter"]["sort"] == "newest"
        assert v["filter"]["sorts"][0]["key"] == "newest"

    def test_requests_pages_do_not_overlap(self, rows):
        p1 = wc.requests_view({"per": "2", "page": "1"})
        p2 = wc.requests_view({"per": "2", "page": "2"})
        p3 = wc.requests_view({"per": "2", "page": "3"})
        ids = [r["id"] for r in p1["rows"] + p2["rows"] + p3["rows"]]
        assert len(ids) == 5 and len(set(ids)) == 5   # no duplicates, no drops
        assert p3["filter"]["page"] == 3
        assert (p3["filter"]["first"], p3["filter"]["last"]) == (5, 5)

    def test_requests_page_clamps_past_the_end(self, rows):
        v = wc.requests_view({"per": "2", "page": "99"})
        assert v["filter"]["page"] == v["filter"]["pages"] == 3

    def test_requests_sort_oldest(self, rows):
        v = wc.requests_view({"per": "2", "sort": "oldest"})
        assert [r["timestamp"] for r in v["rows"]] == [
            "2026-09-19T10:00:00+00:00", "2026-09-19T10:01:00+00:00"]

    def test_requests_column_sorts_are_offered(self, rows):
        """The three numeric columns are sortable, and the client is told so.

        Without these keys a header could not become a sort button: the module
        only offers a sort the server advertises.
        """
        keys = [s["key"] for s in wc.requests_view({})["filter"]["sorts"]]
        assert keys == ["newest", "oldest", "cost", "tokens", "latency"]

    def test_requests_sort_cost_orders_by_cost_not_by_time(self, rows):
        rows.execute("UPDATE requests SET cost=0.50 WHERE id=1")   # oldest row
        rows.execute("UPDATE requests SET cost=0.90 WHERE id=3")   # middle row
        rows.commit()
        v = wc.requests_view({"per": "3", "sort": "cost"})
        assert [r["id"] for r in v["rows"]] == [3, 1, 5]
        assert v["filter"]["sort"] == "cost"
        assert v["filter"]["sort_label"] == "Costliest first"

    def test_requests_sort_tokens_sums_prompt_and_completion(self, rows):
        # id=2 is the biggest by the SUM of the two token columns, but the
        # smallest by prompt tokens alone — so this pins the sum, not one column.
        rows.execute("UPDATE requests SET prompt_tokens=1, completion_tokens=900 WHERE id=2")
        rows.execute("UPDATE requests SET prompt_tokens=800, completion_tokens=0 WHERE id=4")
        rows.commit()
        v = wc.requests_view({"per": "2", "sort": "tokens"})
        assert [r["id"] for r in v["rows"]] == [2, 4]

    def test_requests_sort_latency_is_slowest_first(self, rows):
        rows.execute("UPDATE requests SET latency_ms=9000 WHERE id=2")
        rows.commit()
        v = wc.requests_view({"per": "1", "sort": "latency"})
        assert v["rows"][0]["id"] == 2

    def test_requests_column_sort_survives_paging(self, rows):
        """Sorting must be the server's, not per-page."""
        rows.execute("UPDATE requests SET cost=0.90 WHERE id=1")   # oldest, costliest
        rows.commit()
        p1 = wc.requests_view({"per": "2", "page": "1", "sort": "cost"})
        p2 = wc.requests_view({"per": "2", "page": "2", "sort": "cost"})
        # The costliest row is the oldest one, so a client-side sort of page 1
        # would have left it on page 2 entirely.
        assert [r["id"] for r in p1["rows"]] == [1, 5]
        assert [r["id"] for r in p2["rows"]] == [4, 3]
        ids = [r["id"] for r in p1["rows"] + p2["rows"]]
        assert len(set(ids)) == 4          # no duplicates across the window

    def test_profile_filter_list_is_not_limited_to_conversations(self, rows):
        """A profile whose calls never joined a conversation must still be
        filterable — the filter narrows the log, it does not narrow history."""
        _req(rows, "2026-09-19T12:00:00+00:00", profile="noconv")
        rows.commit()
        assert "noconv" in wc.requests_view({})["filter"]["profiles"]

    def test_requests_unknown_sort_falls_back_to_newest(self, rows):
        v = wc.requests_view({"per": "1", "sort": "'; DROP TABLE requests--"})
        assert v["filter"]["sort"] == "newest"
        assert v["rows"][0]["timestamp"] == "2026-09-19T10:04:00+00:00"

    def test_requests_total_survives_paging(self, rows):
        assert wc.requests_view({"per": "2", "page": "3"})["total"] == 5

    def test_requests_profile_filter_with_pager(self, rows):
        _req(rows, "2026-09-19T11:00:00+00:00", profile="l1", model="b")
        rows.commit()
        v = wc.requests_view({"per": "2", "profile": "l2"})
        assert v["total"] == 5
        assert {r["profile"] for r in v["rows"]} == {"l2"}

    def test_provider_decisions_newest_first_and_paged(self, db):
        wc.ensure_schema()
        for i in range(5):
            _route(db, "2026-09-19T10:0%d:00+00:00" % i, action="a%d" % i)
        db.commit()
        v = wc.provider_decisions_view({"per": "2", "page": "2"})
        assert v["total"] == 5
        assert [r["action"] for r in v["rows"]] == ["a2", "a1"]
        assert v["filter"]["sort_label"] == "Newest first"

    def test_provider_decisions_sort_oldest(self, db):
        wc.ensure_schema()
        for i in range(3):
            _route(db, "2026-09-19T10:0%d:00+00:00" % i, action="a%d" % i)
        db.commit()
        v = wc.provider_decisions_view({"per": "2", "sort": "oldest"})
        assert [r["action"] for r in v["rows"]] == ["a0", "a1"]

