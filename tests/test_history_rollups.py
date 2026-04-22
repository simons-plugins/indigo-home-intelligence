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
        self.info_msgs = []
        self.warn_msgs = []
        self.error_msgs = []

    def debug(self, msg, *args, **kwargs):
        self.debug_msgs.append(msg)

    def info(self, msg, *args, **kwargs):
        self.info_msgs.append(msg)

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

class TestDiagnosePgError:
    """The classifier turns raw psql stderr into a one-line actionable
    hint so users see *what* to fix instead of having to parse Postgres
    error text themselves."""

    def _db(self):
        return HistoryDB(
            db_type="postgresql",
            logger=_NullLogger(),
            pg_host="127.0.0.1",
            pg_user="x",
            pg_password="y",
            pg_database="z",
        )

    def test_role_does_not_exist(self):
        db = self._db()
        stderr = 'psql error: FATAL:  role "Simon" does not exist'
        hint = db._diagnose_pg_error(stderr)
        assert "Plugin Configure" in hint
        assert "case-sensitive" in hint

    def test_password_auth_failed(self):
        db = self._db()
        stderr = 'psql error: FATAL:  password authentication failed for user "simon"'
        hint = db._diagnose_pg_error(stderr)
        assert "password" in hint.lower()

    def test_connection_refused(self):
        db = self._db()
        stderr = 'psql: could not connect to server: Connection refused'
        hint = db._diagnose_pg_error(stderr)
        assert "Postgres.app" in hint

    def test_unknown_host(self):
        db = self._db()
        stderr = 'psql: could not translate host name "nonsense" to address'
        hint = db._diagnose_pg_error(stderr)
        assert "hostname" in hint.lower()

    def test_missing_database(self):
        db = self._db()
        stderr = 'FATAL:  database "nosuchdb" does not exist'
        hint = db._diagnose_pg_error(stderr)
        # Specific 'database "' match fires before the generic
        # "does not exist" rule, so database-not-found classifies as a
        # database error rather than being misreported as a role error.
        assert "database doesn't exist" in hint
        assert "Postgres database" in hint

    def test_missing_role_still_classifies_as_role(self):
        db = self._db()
        stderr = 'psql error: FATAL:  role "Simon" does not exist'
        hint = db._diagnose_pg_error(stderr)
        # No 'database "' in this stderr, so the generic 'does not exist'
        # rule fires and correctly reports a role error.
        assert "role (user) not found" in hint
        assert "Postgres user" in hint

    def test_unrecognised_error_falls_through(self):
        db = self._db()
        hint = db._diagnose_pg_error("something unexpected happened")
        assert "unrecognised" in hint.lower()


class TestRollup7dExtras:
    def test_non_int_device_id_skipped_rollup_7d(self, tmp_path):
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


def _build_energy_db(tmp_path: Path, data: dict) -> Path:
    """Build a SQLite DB with device_history_{id} tables carrying an
    ``accumEnergyTotal`` column. ``data`` is
    ``{device_id: [(hours_ago, accum_kwh), ...]}`` — each tuple is one
    row with a cumulative kWh value. ``ts`` is UTC-stamped to match
    real SQL Logger storage."""
    db_path = tmp_path / "energy.db"
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc)
    for device_id, rows in data.items():
        conn.execute(
            f'CREATE TABLE "device_history_{device_id}" '
            f"(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, accumEnergyTotal REAL)"
        )
        for hours_ago, kwh in rows:
            ts = (now - timedelta(hours=hours_ago)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            conn.execute(
                f'INSERT INTO "device_history_{device_id}" (ts, accumEnergyTotal) '
                f"VALUES (?, ?)",
                (ts, kwh),
            )
    conn.commit()
    conn.close()
    return db_path


class TestDiscoverEnergyTables:
    def test_finds_tables_with_accumEnergyTotal_column(self, tmp_path):
        db_path = tmp_path / "mixed.db"
        conn = sqlite3.connect(str(db_path))
        # 100: energy device (has accumEnergyTotal)
        conn.execute(
            'CREATE TABLE "device_history_100" '
            "(id INTEGER PRIMARY KEY, ts TEXT, accumEnergyTotal REAL)"
        )
        # 200: non-energy device (temperature sensor)
        conn.execute(
            'CREATE TABLE "device_history_200" '
            "(id INTEGER PRIMARY KEY, ts TEXT, sensorValue REAL)"
        )
        # 300: also energy
        conn.execute(
            'CREATE TABLE "device_history_300" '
            "(id INTEGER PRIMARY KEY, ts TEXT, accumEnergyTotal REAL, curEnergyLevel REAL)"
        )
        conn.commit()
        conn.close()
        db = HistoryDB(
            db_type="sqlite",
            logger=_NullLogger(),
            sqlite_path=str(db_path),
        )
        ids = db.discover_energy_tables()
        assert set(ids) == {100, 300}


class TestEnergyRollup14d:
    def test_computes_this_week_last_week_and_delta(self, tmp_path):
        # Device 100: started the fortnight at 100 kWh cumulative, was at
        # 130 kWh one week ago, now at 155 kWh. So last_week = 30 kWh,
        # this_week = 25 kWh, delta = -5, delta_pct ≈ -16.7.
        db_path = _build_energy_db(
            tmp_path,
            {
                100: [
                    (0.1, 155.0),   # ~ now
                    (24 * 7, 130.0),  # 7 days ago
                    (24 * 14, 100.0),  # 14 days ago
                ],
            },
        )
        db = HistoryDB(
            db_type="sqlite",
            logger=_NullLogger(),
            sqlite_path=str(db_path),
        )
        rollup = db.energy_rollup_14d([100])
        assert 100 in rollup
        assert rollup[100]["this_week_kwh"] == 25.0
        assert rollup[100]["last_week_kwh"] == 30.0
        assert rollup[100]["delta_kwh"] == -5.0
        assert rollup[100]["delta_pct"] == -16.7

    def test_missing_now_or_7d_omits_device(self, tmp_path):
        # Device 100 only has one row (now) — no 7d snapshot. Without
        # both now + 7d-ago we can't compute this-week consumption, so
        # drop.
        db_path = _build_energy_db(
            tmp_path,
            {100: [(0.1, 5.0)]},
        )
        db = HistoryDB(
            db_type="sqlite",
            logger=_NullLogger(),
            sqlite_path=str(db_path),
        )
        assert db.energy_rollup_14d([100]) == {}

    def test_missing_14d_emits_partial(self, tmp_path):
        # Device has now + 7d-ago but no 14d-ago snapshot (common on
        # the Aeon HEM: the column was added after the oldest rows
        # were written). Emit this_week_kwh; mark last_week and
        # delta fields as None.
        db_path = _build_energy_db(
            tmp_path,
            {
                100: [
                    (0.1, 155.0),    # now
                    (24 * 7, 130.0),  # 7 days ago
                    # No 14d row.
                ],
            },
        )
        db = HistoryDB(
            db_type="sqlite",
            logger=_NullLogger(),
            sqlite_path=str(db_path),
        )
        rollup = db.energy_rollup_14d([100])
        assert 100 in rollup
        assert rollup[100]["this_week_kwh"] == 25.0
        assert rollup[100]["last_week_kwh"] is None
        assert rollup[100]["delta_kwh"] is None
        assert rollup[100]["delta_pct"] is None

    def test_zero_baseline_reports_delta_pct_none(self, tmp_path):
        # If last week was 0 kWh (device was idle), delta_pct would be
        # divide-by-zero. Emit None so the caller treats it as
        # 'no baseline to compare'.
        db_path = _build_energy_db(
            tmp_path,
            {
                100: [
                    (0.1, 5.0),
                    (24 * 7, 5.0),   # same as now → this_week = 0
                    (24 * 14, 5.0),  # same → last_week = 0
                ],
            },
        )
        db = HistoryDB(
            db_type="sqlite",
            logger=_NullLogger(),
            sqlite_path=str(db_path),
        )
        rollup = db.energy_rollup_14d([100])
        assert 100 in rollup
        assert rollup[100]["last_week_kwh"] == 0.0
        assert rollup[100]["delta_pct"] is None

    def test_empty_device_list_returns_empty(self, tmp_path):
        db_path = _build_energy_db(tmp_path, {})
        db = HistoryDB(
            db_type="sqlite",
            logger=_NullLogger(),
            sqlite_path=str(db_path),
        )
        assert db.energy_rollup_14d([]) == {}

    def test_non_int_device_id_filtered(self, tmp_path):
        db_path = _build_energy_db(
            tmp_path,
            {100: [(0.1, 5.0), (24 * 7, 3.0), (24 * 14, 1.0)]},
        )
        db = HistoryDB(
            db_type="sqlite",
            logger=_NullLogger(),
            sqlite_path=str(db_path),
        )
        rollup = db.energy_rollup_14d([100, "garbage", None, 999.5])
        # Only 100 is a valid int; nothing else should leak into the
        # dynamic SQL.
        assert list(rollup.keys()) == [100]
