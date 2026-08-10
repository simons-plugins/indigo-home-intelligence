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


# ----- naive-local ts: AT TIME ZONE + local window (issue #48, backport
# of indigo-mcp-lite #48/#50) --------------------------------------------


def _capture_pg_sql(*responses):
    """Run a query against canned psql stdout, returning the SQL strings
    handed to psql (the value after each -c)."""
    seen = []
    canned = list(responses)

    def fake_run(cmd, capture_output, text, timeout, env):
        seen.append(cmd[cmd.index("-c") + 1])
        return type("R", (), {"returncode": 0,
                              "stdout": canned.pop(0), "stderr": ""})()

    return seen, patch("history_db.subprocess.run", side_effect=fake_run)


def test_pg_epoch_extraction_interprets_ts_as_local():
    db = HistoryDB(db_type="postgresql", logger=MagicMock())
    seen, patcher = _capture_pg_sql(
        "onOffState\tboolean\n",
        "1753100000\tt\n",
    )
    with patcher:
        db.query_history(42, "onoffstate", "1h")
    assert "EXTRACT(EPOCH FROM (ts AT TIME ZONE 'Europe/London'))" in seen[-1]
    assert "EXTRACT(EPOCH FROM ts)" not in seen[-1]


def test_pg_bucketed_epoch_extraction_interprets_ts_as_local():
    db = HistoryDB(db_type="postgresql", logger=MagicMock())
    seen, patcher = _capture_pg_sql(
        "sensorValue\tdouble precision\n",
        "1753100000\t20.5\n",
    )
    with patcher:
        db.query_history(42, "sensorvalue", "24h")
    assert "EXTRACT(EPOCH FROM (ts AT TIME ZONE 'Europe/London'))" in seen[-1]


def test_pg_custom_timezone_reaches_sql_and_window():
    # Asia/Tokyo: UTC+9 year-round, no DST -- unlike Europe/London this
    # assertion can never go season-blind (London == UTC all winter, so
    # a regression to UTC would pass a London-based test half the year).
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    db = HistoryDB(db_type="postgresql", logger=MagicMock(),
                   pg_timezone="Asia/Tokyo")
    seen, patcher = _capture_pg_sql(
        "onOffState\tboolean\n",
        "1753100000\tt\n",
    )
    with patcher:
        db.query_history(42, "onoffstate", "1h")
    assert "AT TIME ZONE 'Asia/Tokyo'" in seen[-1]
    start_str = seen[-1].split("ts >= '")[1].split("'")[0]
    start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
    expected = datetime.now(
        ZoneInfo("Asia/Tokyo")).replace(tzinfo=None) - timedelta(hours=1)
    assert abs((start - expected).total_seconds()) < 30


def test_pg_timezone_invalid_and_blank_fall_back_to_london():
    logger = MagicMock()
    db = HistoryDB(db_type="postgresql", logger=logger,
                   pg_timezone="Narnia/Lantern")
    assert db.pg_timezone == "Europe/London"
    warnings = " ".join(str(c) for c in logger.warning.call_args_list)
    assert "Narnia/Lantern" in warnings

    db = HistoryDB(db_type="postgresql", logger=MagicMock())
    assert db.pg_timezone == "Europe/London"

    db = HistoryDB(db_type="postgresql", logger=MagicMock(), pg_timezone="   ")
    assert db.pg_timezone == "Europe/London"


def test_pg_rollup_7d_cutoff_is_local_wall_time():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    db = HistoryDB(db_type="postgresql", logger=MagicMock(),
                   pg_timezone="Asia/Tokyo")
    seen, patcher = _capture_pg_sql("5\n")
    with patcher:
        db.rollup_7d([100])
    start_str = seen[-1].split("ts >= '")[1].split("'")[0]
    start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
    expected = datetime.now(
        ZoneInfo("Asia/Tokyo")).replace(tzinfo=None) - timedelta(days=7)
    assert abs((start - expected).total_seconds()) < 30


def test_pg_energy_rollup_14d_cutoffs_are_local_wall_time():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    db = HistoryDB(db_type="postgresql", logger=MagicMock(),
                   pg_timezone="Asia/Tokyo")
    seen, patcher = _capture_pg_sql("100\t155.0\t130.0\t100.0\n")
    with patcher:
        db.energy_rollup_14d([100])
    query_sql = seen[-1]
    week_ago_str = query_sql.split("ts <= '")[1].split("'")[0]
    two_weeks_ago_str = query_sql.split("ts <= '")[2].split("'")[0]
    week_ago = datetime.strptime(week_ago_str, "%Y-%m-%d %H:%M:%S")
    two_weeks_ago = datetime.strptime(two_weeks_ago_str, "%Y-%m-%d %H:%M:%S")
    now_tokyo = datetime.now(ZoneInfo("Asia/Tokyo")).replace(tzinfo=None)
    assert abs((week_ago - (now_tokyo - timedelta(days=7))).total_seconds()) < 30
    assert abs((two_weeks_ago - (now_tokyo - timedelta(days=14))).total_seconds()) < 30


# ----- text columns: latest-per-bucket, never AVG (issue #49, backport
# of indigo-mcp-lite #49/#50) ---------------------------------------------


def test_pg_text_column_uses_distinct_on_not_avg():
    db = HistoryDB(db_type="postgresql", logger=MagicMock())
    seen, patcher = _capture_pg_sql(
        "operationState\ttext\n",
        "1753100000\tRun\n1753100300\tReady\n",
    )
    with patcher:
        result = db.query_history(42, "operationstate", "24h")
    query_sql = seen[-1]
    assert "AVG(" not in query_sql
    assert "DISTINCT ON (bucket)" in query_sql
    assert "ORDER BY bucket, ts DESC" in query_sql
    assert [p["v"] for p in result["points"]] == ["Run", "Ready"]
    assert result["min"] is None and result["max"] is None
    assert result["current"] == "Ready"


def test_pg_text_raw_path_keeps_strings():
    # 1h + text: the only _pg_epoch() call site the bucketed test misses.
    db = HistoryDB(db_type="postgresql", logger=MagicMock())
    seen, patcher = _capture_pg_sql(
        "operationState\ttext\n",
        "1753100000\tRun\n1753100600\tReady\n",
    )
    with patcher:
        result = db.query_history(42, "operationstate", "1h")
    assert "AVG(" not in seen[-1]
    assert "AT TIME ZONE 'Europe/London'" in seen[-1]
    assert [p["v"] for p in result["points"]] == ["Run", "Ready"]
    assert result["current"] == "Ready"


def _ts(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def sqlite_text_db(tmp_path):
    """SQL-Logger-shaped SQLite DB with a text history column.

    Anchored mid-bucket for the largest bucket size (30d -> 10800s) so
    all five rows always share one bucket -- wall-clock `now` straddles
    a bucket boundary ~22% of the time, which makes the latest-per-bucket
    assertion flaky without anchoring (same technique as indigo-mcp-lite's
    ``sqlite_db`` fixture in tests/test_history_tools.py)."""
    import sqlite3
    import time

    path = str(tmp_path / "text_history.sqlite")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE device_history_77 ("
        "id INTEGER PRIMARY KEY, ts TIMESTAMP, operationState TEXT)"
    )
    now = (int(time.time()) // 10800) * 10800 + 5400
    if now < time.time() - 1200:
        now += 10800
    states = ["Ready", "Run", "Run", "Finished", "Idle"]
    rows = [(i, _ts(now - 600 * i), states[i]) for i in range(5)]
    conn.executemany(
        "INSERT INTO device_history_77 VALUES (?, ?, ?)", rows
    )
    conn.commit()
    conn.close()
    return path


def test_sqlite_text_raw_returns_strings(sqlite_text_db):
    db = HistoryDB(db_type="sqlite", logger=MagicMock(), sqlite_path=sqlite_text_db)
    result = db.query_history(77, "operationState", "1h")
    assert result["type"] == "text"
    assert [p["v"] for p in result["points"]] == [
        "Idle", "Finished", "Run", "Run", "Ready"]
    assert result["min"] is None and result["max"] is None
    assert result["current"] == "Ready"


def test_sqlite_text_bucketed_latest_per_bucket_wins(sqlite_text_db):
    # 30d -> 3h buckets: the fixture anchors all five rows into ONE
    # bucket, and the latest row ("Ready") must win over the earliest
    # ("Idle") -- a MIN-vs-MAX regression flips this to "Idle".
    db = HistoryDB(db_type="sqlite", logger=MagicMock(), sqlite_path=sqlite_text_db)
    result = db.query_history(77, "operationState", "30d")
    assert result["type"] == "text"
    assert len(result["points"]) == 1
    assert result["points"][0]["v"] == "Ready"
    assert result["current"] == "Ready"
