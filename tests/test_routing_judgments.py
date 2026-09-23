"""Tests for the routing-judgment feature: rationale persistence, the
``routing_judgments`` labeled dataset, and replay flagging."""

import sqlalchemy


def _columns(engine, table):
    return {c["name"] for c in sqlalchemy.inspect(engine).get_columns(table)}


def test_create_all_makes_judgment_table_and_columns(temp_db):
    """Fresh DB (create_all path) has the new columns + judgment table."""
    _db_path, engine = temp_db
    cols = _columns(engine, "routing_decisions")
    for c in ("path", "keyword", "intent_text", "semantic_json",
              "min_score", "sem_available"):
        assert c in cols, f"routing_decisions missing new column {c}"
    # M7 stripped the transcript blob: 101 MB of a 139 MB DB, duplicating the
    # harness's own session stores. It must not come back via create_all.
    assert "conversation_json" not in cols
    assert sqlalchemy.inspect(engine).has_table("routing_judgments")


def test_routing_judgment_round_trip(temp_db):
    db_path, engine = temp_db
    from src.api.models import RoutingJudgment, get_session
    with get_session(engine) as s:
        s.add(RoutingJudgment(
            decision_id=7, profile="l2", task="debugging", path="keyword:debugging",
            verdict="wrong", expected_task="planning", note="missed the plan intent",
        ))
        s.commit()
    with get_session(engine) as s:
        rows = s.query(RoutingJudgment).all()
        assert len(rows) == 1
        r = rows[0]
        assert r.decision_id == 7
        assert r.verdict == "wrong"
        assert r.expected_task == "planning"
        assert r.note == "missed the plan intent"
        assert r.judged_at  # default populated


def test_routing_judgment_default_profile_and_task(temp_db):
    db_path, engine = temp_db
    from src.api.models import RoutingJudgment, get_session
    with get_session(engine) as s:
        s.add(RoutingJudgment(decision_id=1, verdict="correct"))
        s.commit()
    with get_session(engine) as s:
        r = s.query(RoutingJudgment).first()
        assert r.profile == ""
        assert r.task == ""
        assert r.path is None


def test_replay_flags_drift_and_same(monkeypatch):
    """judge_routing replay logic: SAME when current == recorded, DRIFT when not."""
    import json
    from src.api.router import classify_task_detail

    # A conversation that currently classifies as debugging.
    conv = [{"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "debug this traceback please"}]
    detail = classify_task_detail(conv)
    assert detail.task == "debugging"

    def flag(recorded_task):
        re_detail = classify_task_detail(conv)
        return "SAME" if re_detail.task == recorded_task else "DRIFT"

    assert flag("debugging") == "SAME"
    assert flag("planning") == "DRIFT"
    assert json.dumps(conv)  # conversation_json is a JSON string



