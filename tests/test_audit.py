"""Phase 5 acceptance proofs: the change-history trail.

Same doctrine as the other auth tests — these back a security claim, so they
run against a real temp SQLite database with the production schema, never
against mocks of the audit layer itself.
"""

import json
import sqlite3

import pytest

from src.api import audit

SCHEMA = """
CREATE TABLE audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),
    team_id INTEGER REFERENCES teams(id),
    action TEXT NOT NULL,
    detail TEXT,
    ip_address TEXT
);
"""


@pytest.fixture
def db_dir(temp_dir):
    conn = sqlite3.connect(str(temp_dir / "costs.db"))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return str(temp_dir)


class TestRecord:
    def test_roundtrip(self, db_dir):
        assert audit.record("key.create", actor="token", detail={"name": "ci"},
                            ip="192.168.1.50", data_dir=db_dir) is True
        rows = audit.recent(data_dir=db_dir)
        assert rows[0]["action"] == "key.create"
        assert rows[0]["detail"]["actor"] == "token"
        assert rows[0]["detail"]["detail"] == {"name": "ci"}
        assert rows[0]["ip_address"] == "192.168.1.50"

    def test_actor_rides_in_detail_not_user_id(self, db_dir):
        """user_id is an INTEGER FK to the empty users table — the actor string
        must never be written into it."""
        audit.record("settings.update", actor="oidc:aunttwister",
                     data_dir=db_dir)
        conn = sqlite3.connect(str(db_dir + "/costs.db"))
        uid = conn.execute("select user_id from audit_logs").fetchone()[0]
        conn.close()
        assert uid is None

    def test_newest_first(self, db_dir):
        audit.record("a.one", data_dir=db_dir)
        audit.record("a.two", data_dir=db_dir)
        rows = audit.recent(data_dir=db_dir)
        assert [r["action"] for r in rows] == ["a.two", "a.one"]

    def test_secret_values_redacted(self, db_dir):
        audit.record("provider.create", actor="token", data_dir=db_dir,
                     detail={"name": "deepseek", "api_key": "sk-live-123",
                             "config": {"password": "hunter2",
                                        "base_url": "https://x"}})
        detail = audit.recent(data_dir=db_dir)[0]["detail"]["detail"]
        assert detail["api_key"] == "[REDACTED]"
        assert detail["config"]["password"] == "[REDACTED]"
        assert detail["config"]["base_url"] == "https://x"
        assert "sk-live" not in json.dumps(detail)

    def test_failure_never_raises(self, temp_dir):
        """An empty dir has no costs.db — record must return False, not raise.
        A failed audit write must never 500 a mutation that already succeeded."""
        assert audit.record("x.y", data_dir=str(temp_dir)) is False

    def test_limit_bounds(self, db_dir):
        for i in range(5):
            audit.record(f"a.{i}", data_dir=db_dir)
        assert len(audit.recent(limit=2, data_dir=db_dir)) == 2


class TestActorExtraction:
    def _handler(self, headers):
        from tests.test_server import TestHandler
        return TestHandler(path="/api/keys", method="POST", headers=headers)

    def test_cookie_actor(self, temp_dir, monkeypatch):
        monkeypatch.setenv("LCP_SECRET_KEY", "unit-test-master-secret")
        cookie = __import__("src.api.admin_auth", fromlist=["issue_cookie"]) \
            .issue_cookie(actor="oidc:aunttwister", data_dir=str(temp_dir))
        h = self._handler({"Cookie": f"lcp_admin={cookie}"})
        assert audit.actor_from_handler(h, data_dir=str(temp_dir)) == "oidc:aunttwister"

    def test_bearer_actor(self, temp_dir, monkeypatch):
        monkeypatch.setenv("LCP_SECRET_KEY", "unit-test-master-secret")
        monkeypatch.setenv("LCP_ADMIN_TOKEN", "test-token-do-not-use-in-prod")
        h = self._handler({"Authorization": "Bearer test-token-do-not-use-in-prod"})
        assert audit.actor_from_handler(h, data_dir=str(temp_dir)) == "bearer"

    def test_undecodable_is_authenticated_not_anonymous(self, temp_dir):
        """The guard has already admitted the request; the audit layer only
        attributes. It must never downgrade to a name that reads like 'nobody'."""
        h = self._handler({})
        assert audit.actor_from_handler(h, data_dir=str(temp_dir)) == "authenticated"


class TestRouteSurface:
    def test_audit_endpoint_is_guarded(self):
        from src.api.admin_auth import is_public_path
        assert not is_public_path("/api/audit")
