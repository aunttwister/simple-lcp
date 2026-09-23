"""Remaining branch coverage: setup._stream, commandcode subscription error,
the commandcode subscription error branch."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest


# ── setup._stream ────────────────────────────────────────────────────────────


# ── commandcode subscription api_error path ──────────────────────────────────

class TestCommandCodeApiError:
    def test_subscription_exception_returns_api_error(self):
        from src.api.cost_plugins.commandcode import CommandCodeCostPlugin
        plugin = CommandCodeCostPlugin(engine=None)
        fake_store = MagicMock()
        fake_store.get_cookie.return_value = "session=valid"
        with patch.dict(os.environ, {}, clear=False):
            with patch("src.api.credential_store.get_credential_store", return_value=fake_store):
                with patch(
                    "src.api.cost_plugins.commandcode_api.fetch_subscription_snapshot_dict",
                    side_effect=RuntimeError("boom"),
                ):
                    result = plugin.fetch_subscription()
        assert result["_error"] == "api_error"
        assert "boom" in result["detail"]


# ── benchmark: _execute_run non-provider target + no-CSV ─────────────────────

def _engine(tmp_path):
    from src.api.models import Base, get_engine
    engine = get_engine(str(tmp_path / "b.db"))
    Base.metadata.create_all(engine)
    return engine


# ── commandcode subscription error ───────────────────────────────────────────


# ── benchmark: list_runs clamping + run_to_dict JSON fallback ───────────────
