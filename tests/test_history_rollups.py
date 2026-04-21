"""Tests for HistoryDB.rollup_7d — the 7-day per-device activity
counter that feeds the digest's ``sql_logger_rollups`` block.

Uses a temp SQLite database rather than mocking. The DB logic is
thin enough that mocks don't add much safety, and a real SQLite fixture
catches schema or query-placeholder bugs the mocks wouldn't."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from history_db import HistoryDB


class _NullLogger:
    def __init__(self):
        self.debug_msgs = []
        self.warn_msgs = []
        self.error_msgs = []

    def debug(self, msg, *args, **kwargs):
        self.debug_msgs.append(msg)

    def warning(self, msg, *args, **kwargs):
        self.warn_msgs.append(msg)

    def error(self, msg, *args, **kwargs):
        self.error_msgs.append(msg)


def _build_db(tmp_path: Path, data: dict) -> Path:
    """Build a temp SQLite DB with device_history_{id} tables.

    ``data`` is ``{device_id: [(hours_ago, value), ...]}``. ``ts`` is
    written in UTC to match real SQL Logger storage."""
    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc)
    for device_id, rows in data.items():
        conn.execute(
            f'CREATE TABLE "device_history_{device_id}" '
            f"(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, value REAL)"
        )
        for hours_ago, value in rows:
            ts = (now - timedelta(hours=hours_ago)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conn.execute(
                f'INSERT INTO "device_history_{device_id}" (ts, value) '
                f"VALUES (?, ?)",
                (ts, value),
            )
    conn.commit()
    conn.close()
    return db_path


class TestRollup7d:
    def test_counts_within_window(self, tmp_path):
        db_path = _build_db(
            tmp_path,
            {
                # 7 days = 168h; 200h is outside, the other three are in.
                100: [(1, 1.0), (12, 2.0), (48, 3.0), (200, 4.0)],
                # device 200: all rows older than 7 days — should be 0
                # and thus omitted from the output.
                200: [(200, 1.0), (300, 2.0)],
                # device 300: all rows inside window.
                300: [(1, 1.0), (2, 2.0), (3, 3.0)],
            },
        )
        db = HistoryDB(
            db_type="sqlite",
            logger=_NullLogger(),
            sqlite_path=str(db_path),
        )
        rollups = db.rollup_7d([100, 200, 300])

        assert 100 in rollups
        assert rollups[100]["changes_7d"] == 3
        assert 200 not in rollups  # zero rows inside window
        assert rollups[300]["changes_7d"] == 3

    def test_empty_device_list_returns_empty(self, tmp_path):
        db_path = _build_db(tmp_path, {})
        db = HistoryDB(
            db_type="sqlite",
            logger=_NullLogger(),
            sqlite_path=str(db_path),
        )
        assert db.rollup_7d([]) == {}

    def test_missing_table_is_skipped_not_raised(self, tmp_path):
        db_path = _build_db(tmp_path, {100: [(1, 1.0)]})
        logger = _NullLogger()
        db = HistoryDB(
            db_type="sqlite",
            logger=logger,
            sqlite_path=str(db_path),
        )
        # 999 has no device_history_999 table — should be skipped, not
        # crash the whole rollup.
        rollups = db.rollup_7d([100, 999])
        assert 100 in rollups
        assert 999 not in rollups
        # And the failure should have been logged at debug.
        assert any("999" in m for m in logger.debug_msgs)

    def test_non_int_device_id_skipped(self, tmp_path):
        db_path = _build_db(tmp_path, {100: [(1, 1.0)]})
        db = HistoryDB(
            db_type="sqlite",
            logger=_NullLogger(),
            sqlite_path=str(db_path),
        )
        # A stringified ID (the Indigo mapping protocol sometimes returns
        # these) should be silently skipped rather than letting the f-string
        # build a table name like device_history_abc.
        rollups = db.rollup_7d([100, "malformed", 999.5])
        assert 100 in rollups
        assert "malformed" not in rollups
        assert 999.5 not in rollups
