"""Tests for profile-level budget endpoints (GET/PUT /api/profiles/{name}/budget).

These cover the profile-budget management UI endpoints added alongside the
unified budget system: reading a profile budget and creating-or-updating it.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.api.models import Budget, get_session
from src.server.endpoints import ProfileEndpoints


@pytest.fixture
def profile_ep(temp_db):
    """ProfileEndpoints instance with a temp DB engine."""
    db_path, engine = temp_db
    ep = ProfileEndpoints()
    ep._audit = MagicMock()  # standalone mixin: no AuditEndpoints in its MRO
    ep.engine = engine
    ep._read_body = lambda: {}
    ep._send_json = MagicMock()
    return ep


@pytest.fixture
def seeded_profile_budget(profile_ep):
    """Create a profile budget in the same temp DB and return (engine, budget_id)."""
    engine = profile_ep.engine
    with get_session(engine) as session:
        budget = Budget(
            name="L2 Cap",
            key_id=None,
            profile="l2",
            amount=200.0,
            current_spend=50.0,
            period="monthly",
            threshold_pct="50,80,90",
            action="block",
            status="active",
        )
        session.add(budget)
        session.commit()
        budget_id = budget.id
    return engine, budget_id


# ── GET /api/profiles/{name}/budget ──────────────────────────────────────

class TestGetProfileBudget:
    def test_returns_profile_budget(self, profile_ep, seeded_profile_budget):
        profile_ep._serve_profile_budget("l2")
        result = profile_ep._send_json.call_args[0][0]
        assert result["budget"] is not None
        b = result["budget"]
        assert b["name"] == "L2 Cap"
        assert b["amount"] == 200.0
        assert b["current_spend"] == 50.0
        assert b["period"] == "monthly"
        assert b["threshold_pct"] == "50,80,90"
        assert b["action"] == "block"
        assert b["status"] == "active"
        assert b["spend_pct"] == 25.0  # 50/200

    def test_returns_null_when_no_budget(self, profile_ep):
        profile_ep._serve_profile_budget("l2")
        result = profile_ep._send_json.call_args[0][0]
        assert result == {"budget": None}

    def test_returns_null_for_other_profile(self, profile_ep, seeded_profile_budget):
        # Budget exists for l2, but not for career
        profile_ep._serve_profile_budget("career")
        result = profile_ep._send_json.call_args[0][0]
        assert result == {"budget": None}

    def test_zero_amount_spend_pct_is_zero(self, profile_ep, seeded_profile_budget):
        engine, budget_id = seeded_profile_budget
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            b.amount = 0
            session.commit()
        profile_ep._serve_profile_budget("l2")
        result = profile_ep._send_json.call_args[0][0]
        assert result["budget"]["spend_pct"] == 0.0

    def test_exception_returns_500(self, profile_ep):
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            profile_ep._serve_profile_budget("l2")
        assert profile_ep._send_json.call_args[0][1] == 500
        assert "db down" in profile_ep._send_json.call_args[0][0]["error"]


# ── PUT /api/profiles/{name}/budget ──────────────────────────────────────

class TestPutProfileBudget:
    def test_creates_new_budget_with_defaults(self, profile_ep):
        profile_ep._read_body = lambda: {"amount": 150.0}
        profile_ep._serve_profile_budget_update("l1")
        result = profile_ep._send_json.call_args[0][0]
        assert result["ok"] is True
        assert "created" in result
        with get_session(profile_ep.engine) as session:
            b = session.query(Budget).filter(Budget.profile == "l1").first()
            assert b is not None
            assert b.name == "L1 Budget"  # default derived name
            assert b.key_id is None
            assert b.amount == 150.0
            assert b.period == "monthly"
            assert b.threshold_pct == "80"
            assert b.action == "log"
            assert b.status == "active"

    def test_creates_new_budget_with_custom_values(self, profile_ep):
        profile_ep._read_body = lambda: {
            "name": "Career Cap",
            "amount": 25.0,
            "action": "block",
            "threshold_pct": "50,80,90",
            "period": "monthly",
        }
        profile_ep._serve_profile_budget_update("career")
        result = profile_ep._send_json.call_args[0][0]
        assert result["ok"] is True
        with get_session(profile_ep.engine) as session:
            b = session.query(Budget).filter(Budget.profile == "career").first()
            assert b.name == "Career Cap"
            assert b.action == "block"
            assert b.threshold_pct == "50,80,90"

    def test_updates_existing_budget(self, profile_ep, seeded_profile_budget):
        engine, budget_id = seeded_profile_budget
        profile_ep._read_body = lambda: {"amount": 300.0, "action": "log", "threshold_pct": "60,80"}
        profile_ep._serve_profile_budget_update("l2")
        result = profile_ep._send_json.call_args[0][0]
        assert result["ok"] is True
        assert result["updated"] == budget_id
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            assert b.amount == 300.0
            assert b.action == "log"
            assert b.threshold_pct == "60,80"
            assert b.profile == "l2"  # unchanged

    def test_partial_update_preserves_unset_fields(self, profile_ep, seeded_profile_budget):
        engine, budget_id = seeded_profile_budget
        profile_ep._read_body = lambda: {"name": "Renamed Cap"}
        profile_ep._serve_profile_budget_update("l2")
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            assert b.name == "Renamed Cap"
            assert b.amount == 200.0
            assert b.current_spend == 50.0
            assert b.action == "block"
            assert b.period == "monthly"

    def test_updates_status_field(self, profile_ep, seeded_profile_budget):
        engine, budget_id = seeded_profile_budget
        profile_ep._read_body = lambda: {"status": "paused"}
        profile_ep._serve_profile_budget_update("l2")
        with get_session(engine) as session:
            assert session.get(Budget, budget_id).status == "paused"

    def test_invalid_json_returns_400(self, profile_ep):
        profile_ep._read_body = MagicMock(side_effect=Exception("bad json"))
        profile_ep._serve_profile_budget_update("l2")
        assert profile_ep._send_json.call_args[0][1] == 400

    def test_exception_returns_500(self, profile_ep):
        profile_ep._read_body = lambda: {"amount": 10.0}
        with patch("src.server.endpoints.get_session", side_effect=RuntimeError("db down")):
            profile_ep._serve_profile_budget_update("l2")
        assert profile_ep._send_json.call_args[0][1] == 500
        assert "db down" in profile_ep._send_json.call_args[0][0]["error"]
