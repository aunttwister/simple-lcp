"""CLI + branch coverage for the seed_capabilities seeder."""

import os
import tempfile
from unittest.mock import patch

import pytest


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    from src.api.models import get_engine, Base
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    engine.dispose()
    yield path
    for ext in ("", "-wal", "-shm"):
        try:
            os.unlink(path + ext)
        except FileNotFoundError:
            pass


# ── seed_capabilities CLI ────────────────────────────────────────────────────

class TestSeedCli:
    def test_cli_registry_only(self, db_path, capsys):
        from src.api.seed_capabilities import main
        with patch("sys.argv", ["seed_capabilities", "--db", db_path, "--registry-only"]):
            main()
        out = capsys.readouterr().out
        assert "registry entries" in out

    def test_cli_capabilities_only(self, db_path, capsys):
        from src.api.seed_capabilities import main
        with patch("sys.argv", ["seed_capabilities", "--db", db_path, "--capabilities-only"]):
            main()
        out = capsys.readouterr().out
        assert "declared capability rows" in out

    def test_cli_default(self, db_path, capsys):
        from src.api.seed_capabilities import main
        with patch("sys.argv", ["seed_capabilities", "--db", db_path]):
            main()
        out = capsys.readouterr().out
        assert "registry entries" in out
        assert "declared capability rows" in out


# ── benchmark_import CLI ─────────────────────────────────────────────────────


