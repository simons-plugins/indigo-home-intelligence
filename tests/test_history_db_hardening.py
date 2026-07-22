"""Tests for the review-driven history_db hardening (aligned with lite)."""

from unittest.mock import MagicMock, patch

import pytest

from history_db import HistoryDB


def test_pg_env_sets_read_only_and_password():
    db = HistoryDB(db_type="postgresql", logger=MagicMock(), pg_password="pw")
    captured = {}

    def fake_run(cmd, capture_output, text, timeout, env):
        captured["env"] = env
        return type("R", (), {"returncode": 0, "stdout": "1\n", "stderr": ""})()

    with patch("history_db.subprocess.run", side_effect=fake_run):
        db._execute_pg("SELECT 1")
    assert captured["env"]["PGOPTIONS"] == "-c default_transaction_read_only=on"
    assert captured["env"]["PGPASSWORD"] == "pw"


def test_sqlite_wrong_path_fails_without_creating_file(tmp_path):
    path = tmp_path / "typo.sqlite"
    db = HistoryDB(db_type="sqlite", logger=MagicMock(), sqlite_path=str(path))
    assert db.test_connection() is False
    assert not path.exists()


def test_query_history_missing_table_raises(tmp_path):
    import sqlite3
    path = str(tmp_path / "h.sqlite")
    sqlite3.connect(path).close()
    db = HistoryDB(db_type="sqlite", logger=MagicMock(), sqlite_path=path)
    with pytest.raises(ValueError, match="no SQL Logger history for device 9"):
        db.query_history(9, "brightness", "1h")


def test_query_history_unknown_column_raises(tmp_path):
    import sqlite3
    path = str(tmp_path / "h.sqlite")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE device_history_9 (id INTEGER, ts TIMESTAMP, brightness INTEGER)")
    conn.commit(); conn.close()
    db = HistoryDB(db_type="sqlite", logger=MagicMock(), sqlite_path=path)
    with pytest.raises(ValueError, match="available: brightness"):
        db.query_history(9, "nope", "1h")
