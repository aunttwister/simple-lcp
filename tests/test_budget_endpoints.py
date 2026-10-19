"""Tests for budget CRUD endpoints and budget enforcement."""

from unittest.mock import MagicMock, patch

import pytest

from src.api.models import Budget, ApiKey, get_session
from src.server.endpoints import BudgetEndpoints
from src.server.handler import LCPHandler


@pytest.fixture
def budget_ep(temp_db):
    """BudgetEndpoints instance with a temp DB engine."""
    db_path, engine = temp_db
    ep = BudgetEndpoints()
    ep._audit = MagicMock()  # standalone mixin: no AuditEndpoints in its MRO
    ep.engine = engine
    ep.path = "/api/budgets"
    ep._read_body = lambda: {}
    ep._send_json = MagicMock()
    return ep


@pytest.fixture
def seeded_budget(budget_ep):
    """Create a budget in the SAME temp DB as budget_ep and return (engine, budget_id)."""
    engine = budget_ep.engine
    with get_session(engine) as session:
        budget = Budget(
            name="Test Budget",
            key_id=None,
            profile="l2",
            amount=100.0,
            current_spend=50.0,
            period="monthly",
            threshold_pct="50,80",
            action="log",
            status="active",
        )
        session.add(budget)
        session.commit()
        budget_id = budget.id
    return engine, budget_id


# ── Budget CRUD ────────────────────────────────────────────────────────────

class TestBudgetList:
    def test_empty_list(self, budget_ep):
        budget_ep._serve_budgets_list()
        result = budget_ep._send_json.call_args[0][0]
        assert result == {"budgets": []}

    def test_lists_seeded_budgets(self, budget_ep, seeded_budget):
        budget_ep._serve_budgets_list()
        result = budget_ep._send_json.call_args[0][0]
        assert len(result["budgets"]) == 1
        b = result["budgets"][0]
        assert b["name"] == "Test Budget"
        assert b["amount"] == 100.0
        assert b["current_spend"] == 50.0
        assert b["threshold_pct"] == "50,80"
        assert b["profile"] == "l2"

    def test_list_includes_key_name(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            key = ApiKey(
                key_hash="hash123", key_prefix="sk-test", name="My Key",
                allowed_profiles="l2", spend_limit=0, total_spend=0,
                status="active",
            )
            session.add(key)
            session.commit()
            key_id = key.id
            budget = Budget(
                name="Key Budget", key_id=key_id, profile=None,
                amount=50.0, current_spend=10.0, period="total",
                threshold_pct="80", action="log", status="active",
            )
            session.add(budget)
            session.commit()
        budget_ep._serve_budgets_list()
        result = budget_ep._send_json.call_args[0][0]
        assert result["budgets"][0]["key_name"] == "My Key"
        assert result["budgets"][0]["key_id"] == key_id


class TestBudgetCreate:
    def test_creates_budget(self, budget_ep):
        budget_ep._read_body = lambda: {
            "name": "New Budget",
            "key_id": None,
            "profile": "l1",
            "amount": 250.0,
            "period": "monthly",
            "threshold_pct": "80,90",
            "action": "block",
        }
        budget_ep._serve_budget_create()
        result = budget_ep._send_json.call_args[0][0]
        assert result["ok"] is True
        assert result["budget"]["name"] == "New Budget"

        # Verify persisted
        with get_session(budget_ep.engine) as session:
            budgets = session.query(Budget).all()
            assert len(budgets) == 1
            assert budgets[0].profile == "l1"
            assert budgets[0].action == "block"
            assert budgets[0].amount == 250.0

    def test_invalid_json(self, budget_ep):
        budget_ep._read_body = MagicMock(side_effect=Exception("bad json"))
        budget_ep._serve_budget_create()
        assert budget_ep._send_json.call_args[0][1] == 400

    def test_creates_with_defaults(self, budget_ep):
        budget_ep._read_body = lambda: {"name": "Default Budget", "amount": 100}
        budget_ep._serve_budget_create()
        result = budget_ep._send_json.call_args[0][0]
        assert result["ok"] is True
        with get_session(budget_ep.engine) as session:
            b = session.query(Budget).first()
            assert b.period == "monthly"
            assert b.threshold_pct == "80"
            assert b.action == "log"
            assert b.status == "active"


class TestBudgetUpdate:
    def test_updates_budget(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget
        budget_ep._read_body = lambda: {"amount": 200.0, "action": "block", "profile": "career"}
        budget_ep._serve_budget_update(str(budget_id))
        result = budget_ep._send_json.call_args[0][0]
        assert result["ok"] is True
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            assert b.amount == 200.0
            assert b.action == "block"
            assert b.profile == "career"

    def test_update_not_found(self, budget_ep):
        budget_ep._read_body = lambda: {"amount": 5.0}
        budget_ep._serve_budget_update("9999")
        assert budget_ep._send_json.call_args[0][1] == 404

    def test_update_invalid_id(self, budget_ep):
        budget_ep._read_body = lambda: {"amount": 5.0}
        budget_ep._serve_budget_update("abc")
        assert budget_ep._send_json.call_args[0][1] == 400

    def test_partial_update(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget
        budget_ep._read_body = lambda: {"threshold_pct": "90,95"}
        budget_ep._serve_budget_update(str(budget_id))
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            assert b.threshold_pct == "90,95"
            # Untouched fields preserved
            assert b.amount == 100.0
            assert b.name == "Test Budget"


class TestBudgetDelete:
    def test_deletes_budget(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget
        budget_ep._serve_budget_delete(str(budget_id))
        result = budget_ep._send_json.call_args[0][0]
        assert result["ok"] is True
        with get_session(engine) as session:
            assert session.query(Budget).count() == 0

    def test_delete_not_found(self, budget_ep):
        budget_ep._serve_budget_delete("9999")
        assert budget_ep._send_json.call_args[0][1] == 404


class TestBudgetStatus:
    def test_status_computes_pct(self, budget_ep, seeded_budget):
        budget_ep._serve_budgets_status()
        result = budget_ep._send_json.call_args[0][0]
        assert len(result["budgets"]) == 1
        b = result["budgets"][0]
        assert b["spend_pct"] == 50.0  # 50/100
        assert b["thresholds"] == [50, 80]

    def test_status_excludes_non_active(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            b.status = "paused"
            session.commit()
        budget_ep._serve_budgets_status()
        result = budget_ep._send_json.call_args[0][0]
        assert result["budgets"] == []

    def test_status_zero_amount_no_error(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            b.amount = 0
            session.commit()
        budget_ep._serve_budgets_status()
        result = budget_ep._send_json.call_args[0][0]
        assert result["budgets"][0]["spend_pct"] == 0.0


# ── Budget Enforcement (handler) ────────────────────────────────────────────

class TestCheckBudgetBlock:
    def _make_handler(self, engine):
        h = LCPHandler.__new__(LCPHandler)
        h.engine = engine
        return h

    def test_no_block_when_budget_not_exceeded(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget  # 50/100, not exceeded
        h = self._make_handler(engine)
        assert h._check_budget_block("l2") is None

    def test_blocks_when_exceeded(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            b.current_spend = 100.0
            b.action = "block"
            session.commit()
        h = self._make_handler(engine)
        assert h._check_budget_block("l2") == "Test Budget"

    def test_no_block_when_action_log(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            b.current_spend = 100.0
            b.action = "log"  # log-and-allow
            session.commit()
        h = self._make_handler(engine)
        assert h._check_budget_block("l2") is None

    def test_block_respects_profile(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget  # profile=l2
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            b.current_spend = 100.0
            b.action = "block"
            session.commit()
        h = self._make_handler(engine)
        # Different profile should not block
        assert h._check_budget_block("career") is None

    def test_global_budget_blocks_any_profile(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            session.add(Budget(
                name="Global", key_id=None, profile=None,
                amount=10.0, current_spend=10.0, period="monthly",
                threshold_pct="80", action="block", status="active",
            ))
            session.commit()
        h = self._make_handler(engine)
        assert h._check_budget_block("l2") == "Global"
        assert h._check_budget_block("career") == "Global"

    def test_exceeded_status_still_blocks(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            b.current_spend = 100.0
            b.action = "block"
            b.status = "exceeded"
            session.commit()
        h = self._make_handler(engine)
        assert h._check_budget_block("l2") == "Test Budget"


class TestIncrementBudgetSpend:
    def _make_handler(self, engine):
        h = LCPHandler.__new__(LCPHandler)
        h.engine = engine
        return h

    def test_increments_spend(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget
        h = self._make_handler(engine)
        h._increment_budget_spend("l2", 12.5)
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            assert b.current_spend == 62.5  # 50 + 12.5

    def test_no_breach_below_threshold(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget  # 50/100, threshold 50 already passed
        h = self._make_handler(engine)
        breaches = h._increment_budget_spend("l2", 5.0)
        assert breaches == []

    def test_breach_on_threshold_crossing(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget  # 50/100, thresholds [50, 80]
        h = self._make_handler(engine)
        # 50 -> 85 crosses 80 threshold
        breaches = h._increment_budget_spend("l2", 35.0)
        assert len(breaches) == 1
        assert breaches[0]["threshold"] == 80
        assert breaches[0]["spend_pct"] == 85.0
        assert breaches[0]["budget_name"] == "Test Budget"

    def test_multiple_thresholds_crossed(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget  # 50/100, thresholds [50, 80]
        h = self._make_handler(engine)
        # 50 -> 95 crosses 80 only (50 was already crossed before)
        breaches = h._increment_budget_spend("l2", 45.0)
        thresholds = [b["threshold"] for b in breaches]
        assert 80 in thresholds

    def test_marks_exceeded_status(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget  # 50/100
        h = self._make_handler(engine)
        h._increment_budget_spend("l2", 50.0)  # now exactly 100
        with get_session(engine) as session:
            b = session.get(Budget, budget_id)
            assert b.status == "exceeded"
            assert b.last_alert_at is not None

    def test_key_budget_increments(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            key = ApiKey(
                key_hash="h1", key_prefix="sk-1", name="K",
                allowed_profiles="l2", spend_limit=10, total_spend=0, status="active",
            )
            session.add(key)
            session.commit()
            key_id = key.id
            session.add(Budget(
                name="Key Budget", key_id=key_id, profile=None,
                amount=10.0, current_spend=0, period="total",
                threshold_pct="50", action="log", status="active",
            ))
            session.commit()
        h = self._make_handler(engine)
        breaches = h._increment_budget_spend("l2", 6.0, key_id=key_id)
        assert len(breaches) == 1  # crosses 50%
        assert breaches[0]["budget_name"] == "Key Budget"


class TestTrackBudgetSpend:
    def test_fires_alert_on_breach(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget  # 50/100, threshold 80
        h = LCPHandler.__new__(LCPHandler)
        h.engine = engine
        with patch("src.server.handler.get_alert_manager") as mock_get_am:
            mock_am = MagicMock()
            mock_get_am.return_value = mock_am
            h._track_budget_spend("l2", 35.0)  # 50 -> 85, crosses 80
            mock_am.fire.assert_called_once()
            call_kwargs = mock_am.fire.call_args[1]
            assert call_kwargs["rule"] == "budget_breach"
            assert call_kwargs["severity"] == "warning"
            assert "85.0%" in call_kwargs["title"]

    def test_fires_critical_at_100(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget  # 50/100, threshold 80
        h = LCPHandler.__new__(LCPHandler)
        h.engine = engine
        with patch("src.server.handler.get_alert_manager") as mock_get_am:
            mock_am = MagicMock()
            mock_get_am.return_value = mock_am
            h._track_budget_spend("l2", 50.0)  # 50 -> 100, crosses 80
            mock_am.fire.assert_called_once()
            assert mock_am.fire.call_args[1]["severity"] == "critical"

    def test_no_fire_without_breach(self, budget_ep, seeded_budget):
        engine, budget_id = seeded_budget  # 50/100
        h = LCPHandler.__new__(LCPHandler)
        h.engine = engine
        with patch("src.server.handler.get_alert_manager") as mock_get_am:
            mock_am = MagicMock()
            mock_get_am.return_value = mock_am
            h._track_budget_spend("l2", 5.0)  # 50 -> 55, no threshold crossed
            mock_am.fire.assert_not_called()

    def test_fires_info_severity_below_80(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            session.add(Budget(
                name="Info Cap", key_id=None, profile="l2",
                amount=100.0, current_spend=40.0, period="monthly",
                threshold_pct="50,80", action="log", status="active",
            ))
            session.commit()
        h = LCPHandler.__new__(LCPHandler)
        h.engine = engine
        with patch("src.server.handler.get_alert_manager") as mock_get_am:
            mock_am = MagicMock()
            mock_get_am.return_value = mock_am
            # 40 -> 60 crosses 50 but stays under 80 -> info severity
            h._track_budget_spend("l2", 20.0)
            mock_am.fire.assert_called_once()
            assert mock_am.fire.call_args[1]["severity"] == "info"
            assert "60.0%" in mock_am.fire.call_args[1]["title"]


# ── Key-scoped budget block matching ─────────────────────────────────────

class TestCheckBudgetBlockKeyScoped:
    def _make_handler(self, engine):
        h = LCPHandler.__new__(LCPHandler)
        h.engine = engine
        return h

    def _add_key_budget(self, engine, key_id, name="Key Block", spend=100.0, amount=10.0):
        with get_session(engine) as session:
            session.add(Budget(
                name=name, key_id=key_id, profile=None,
                amount=amount, current_spend=spend, period="total",
                threshold_pct="80", action="block", status="exceeded",
            ))
            session.commit()

    def test_blocks_when_key_matches(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            key = ApiKey(
                key_hash="h-kb1", key_prefix="sk-kb1", name="KB1",
                allowed_profiles="l2", spend_limit=0, total_spend=0, status="active",
            )
            session.add(key)
            session.commit()
            key_id = key.id
        self._add_key_budget(engine, key_id)
        h = self._make_handler(engine)
        assert h._check_budget_block("l2", key_id=key_id) == "Key Block"

    def test_key_budget_not_blocked_for_other_key(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            key_a = ApiKey(
                key_hash="h-a", key_prefix="sk-a", name="A",
                allowed_profiles="l2", spend_limit=0, total_spend=0, status="active",
            )
            key_b = ApiKey(
                key_hash="h-b", key_prefix="sk-b", name="B",
                allowed_profiles="l2", spend_limit=0, total_spend=0, status="active",
            )
            session.add(key_a)
            session.add(key_b)
            session.commit()
            key_a_id, key_b_id = key_a.id, key_b.id
        self._add_key_budget(engine, key_a_id)
        h = self._make_handler(engine)
        # Budget scoped to key A should not block key B
        assert h._check_budget_block("l2", key_id=key_b_id) is None

    def test_key_budget_not_blocked_without_key(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            key = ApiKey(
                key_hash="h-kb2", key_prefix="sk-kb2", name="KB2",
                allowed_profiles="l2", spend_limit=0, total_spend=0, status="active",
            )
            session.add(key)
            session.commit()
            key_id = key.id  # capture while session is still open
        self._add_key_budget(engine, key_id)
        h = self._make_handler(engine)
        # No key supplied -> only global/profile budgets considered
        assert h._check_budget_block("l2") is None


# ── Budget block edge cases ──────────────────────────────────────────────

class TestCheckBudgetBlockEdge:
    def _make_handler(self, engine):
        h = LCPHandler.__new__(LCPHandler)
        h.engine = engine
        return h

    def test_first_blocking_budget_wins(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            session.add(Budget(
                name="Block A", key_id=None, profile="l2",
                amount=10.0, current_spend=10.0, period="monthly",
                threshold_pct="80", action="block", status="exceeded",
            ))
            session.add(Budget(
                name="Block B", key_id=None, profile="l2",
                amount=5.0, current_spend=6.0, period="monthly",
                threshold_pct="80", action="block", status="exceeded",
            ))
            session.commit()
        h = self._make_handler(engine)
        blocked = h._check_budget_block("l2")
        assert blocked in ("Block A", "Block B")

    def test_paused_budget_does_not_block(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            session.add(Budget(
                name="Paused Cap", key_id=None, profile="l2",
                amount=10.0, current_spend=50.0, period="monthly",
                threshold_pct="80", action="block", status="paused",
            ))
            session.commit()
        h = self._make_handler(engine)
        assert h._check_budget_block("l2") is None

    def test_exception_returns_none(self, budget_ep):
        engine = budget_ep.engine
        h = self._make_handler(engine)
        with patch("src.server.handler.get_session", side_effect=RuntimeError("db down")):
            assert h._check_budget_block("l2") is None


# ── Budget spend increment edge cases ────────────────────────────────────

class TestIncrementBudgetSpendEdge:
    def _make_handler(self, engine):
        h = LCPHandler.__new__(LCPHandler)
        h.engine = engine
        return h

    def test_syncs_api_key_total_spend(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            key = ApiKey(
                key_hash="h-sync", key_prefix="sk-sync", name="Sync",
                allowed_profiles="l2", spend_limit=0, total_spend=0, status="active",
            )
            session.add(key)
            session.commit()
            key_id = key.id
            session.add(Budget(
                name="Sync Budget", key_id=key_id, profile=None,
                amount=100.0, current_spend=20.0, period="total",
                threshold_pct="80", action="log", status="active",
            ))
            session.commit()
        h = self._make_handler(engine)
        h._increment_budget_spend("l2", 15.0, key_id=key_id)
        with get_session(engine) as session:
            b = session.query(Budget).filter(Budget.key_id == key_id).first()
            key = session.query(ApiKey).filter(ApiKey.id == key_id).first()
            assert b.current_spend == 35.0
            assert key.total_spend == 35.0  # synced from key budget

    def test_key_budget_not_incremented_without_key_id(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            key = ApiKey(
                key_hash="h-nk", key_prefix="sk-nk", name="NK",
                allowed_profiles="l2", spend_limit=0, total_spend=0, status="active",
            )
            session.add(key)
            session.commit()
            key_id = key.id
            session.add(Budget(
                name="NoKey Budget", key_id=key_id, profile=None,
                amount=100.0, current_spend=0.0, period="total",
                threshold_pct="80", action="log", status="active",
            ))
            session.commit()
        h = self._make_handler(engine)
        breaches = h._increment_budget_spend("l2", 50.0)  # no key_id
        assert breaches == []
        with get_session(engine) as session:
            b = session.query(Budget).filter(Budget.key_id == key_id).first()
            assert b.current_spend == 0.0  # untouched

    def test_crossing_100_threshold_fires_breach(self, budget_ep):
        engine = budget_ep.engine
        with get_session(engine) as session:
            session.add(Budget(
                name="Hard Cap", key_id=None, profile="l2",
                amount=100.0, current_spend=90.0, period="monthly",
                threshold_pct="100", action="block", status="active",
            ))
            session.commit()
        h = self._make_handler(engine)
        breaches = h._increment_budget_spend("l2", 10.0)  # 90 -> 100
        assert len(breaches) == 1
        assert breaches[0]["threshold"] == 100
        assert breaches[0]["spend_pct"] == 100.0
        with get_session(engine) as session:
            b = session.query(Budget).filter(Budget.name == "Hard Cap").first()
            assert b.status == "exceeded"

    def test_exception_returns_empty_list(self, budget_ep):
        engine = budget_ep.engine
        h = self._make_handler(engine)
        with patch("src.server.handler.get_session", side_effect=RuntimeError("db down")):
            assert h._increment_budget_spend("l2", 5.0) == []
