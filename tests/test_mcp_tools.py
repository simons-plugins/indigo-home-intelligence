"""Tests for MCP tool registration and behaviour.

Each tool is exercised end-to-end: register it on a fresh MCPHandler,
fire a ``tools/call`` request, and assert the returned structure /
error envelope. Stores and history_db are replaced by lightweight
fakes so no indigo runtime is required.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import mcp_tools
from mcp_handler import MCPHandler


# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------


class _FakeRuleStore:
    def __init__(self, rules):
        self._rules = rules

    def list_rules(self):
        return list(self._rules)


class _FakeObservationStore:
    def __init__(self, observations):
        self._observations = observations

    def list_all(self):
        return list(self._observations)


class _FakeHistoryDB:
    """Stand-in with the subset of history_db used by mcp_tools."""
    def __init__(self, result=None, raises=None):
        self._result = result or {}
        self._raises = raises
        self.calls = []

    def query_history(self, device_id, column, time_range="24h"):
        self.calls.append(
            {"device_id": device_id, "column": column, "time_range": time_range}
        )
        if self._raises:
            raise self._raises
        return dict(self._result)


class _FakeContext:
    """Drop-in for HouseContextAccess with pre-canned responses."""
    def __init__(self, house_model=None, sql=None, health=None, energy=None):
        self._house_model = house_model or {
            "devices": [],
            "device_folders": [],
            "indigo_triggers": [],
            "indigo_schedules": [],
            "action_groups": [],
        }
        self._sql = sql or {}
        self._health = health or {
            "low_batteries": [], "low_batteries_total": 0,
            "offline_devices": [], "offline_devices_total": 0,
        }
        self._energy = energy or {}

    def build_house_model(self):
        return dict(self._house_model)

    def sql_rollups(self):
        return dict(self._sql)

    def fleet_health(self):
        return dict(self._health)

    def energy_context(self):
        return dict(self._energy)


@pytest.fixture
def logger():
    return logging.getLogger("test-mcp-tools")


@pytest.fixture
def handler(logger):
    return MCPHandler(logger=logger, server_name="test", server_version="0.0.0")


def _call_tool(handler, tool_name, arguments=None):
    """Invoke a registered tool via JSON-RPC and return the parsed
    response body."""
    rpc = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments or {}},
    }
    resp = handler.handle_request(
        "POST",
        {"Accept": "application/json"},
        json.dumps(rpc),
    )
    return json.loads(resp["content"])


# ---------------------------------------------------------------------
# get_rules
# ---------------------------------------------------------------------


class TestGetRules:
    def _register(self, handler, rules):
        mcp_tools._register_get_rules(handler, rule_store=_FakeRuleStore(rules))

    def test_active_rules_by_default(self, handler):
        self._register(handler, [
            {"id": "a", "enabled": True, "auto_disabled": False},
            {"id": "b", "enabled": False, "auto_disabled": False},
            {"id": "c", "enabled": True, "auto_disabled": True},
        ])
        body = _call_tool(handler, "get_rules")
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["total"] == 1
        assert payload["rules"][0]["id"] == "a"

    def test_include_disabled_returns_everything(self, handler):
        self._register(handler, [
            {"id": "a", "enabled": True, "auto_disabled": False},
            {"id": "b", "enabled": False, "auto_disabled": False},
            {"id": "c", "enabled": True, "auto_disabled": True},
        ])
        body = _call_tool(handler, "get_rules", {"include_disabled": True})
        payload = json.loads(body["result"]["content"][0]["text"])
        ids = sorted(r["id"] for r in payload["rules"])
        assert ids == ["a", "b", "c"]

    def test_tool_registered_with_schema(self, handler):
        self._register(handler, [])
        # tools/list surfaces the schema we registered.
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        resp = handler.handle_request("POST", {"Accept": "application/json"}, json.dumps(rpc))
        tools = json.loads(resp["content"])["result"]["tools"]
        tool = next(t for t in tools if t["name"] == "get_rules")
        assert tool["inputSchema"]["properties"]["include_disabled"]["type"] == "boolean"


# ---------------------------------------------------------------------
# get_observations
# ---------------------------------------------------------------------


class TestGetObservations:
    def _register(self, handler, obs):
        mcp_tools._register_get_observations(
            handler, observation_store=_FakeObservationStore(obs),
        )

    def _iso(self, days_ago):
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()

    def test_all_filter_returns_recent_observations(self, handler):
        self._register(handler, [
            {"id": "1", "user_response": "yes", "digest_run_at": self._iso(3)},
            {"id": "2", "user_response": None, "digest_run_at": self._iso(10)},
            {"id": "3", "user_response": "no", "digest_run_at": self._iso(100)},
        ])
        body = _call_tool(handler, "get_observations", {"status_filter": "all", "days_back": 60})
        payload = json.loads(body["result"]["content"][0]["text"])
        ids = sorted(o["id"] for o in payload["observations"])
        assert ids == ["1", "2"]  # id 3 is 100 days ago — outside window

    def test_pending_filter_excludes_responded(self, handler):
        self._register(handler, [
            {"id": "1", "user_response": "yes", "digest_run_at": self._iso(3)},
            {"id": "2", "user_response": None, "digest_run_at": self._iso(3)},
        ])
        body = _call_tool(handler, "get_observations", {"status_filter": "pending"})
        payload = json.loads(body["result"]["content"][0]["text"])
        assert len(payload["observations"]) == 1
        assert payload["observations"][0]["id"] == "2"

    def test_specific_status_filter(self, handler):
        self._register(handler, [
            {"id": "1", "user_response": "yes", "digest_run_at": self._iso(3)},
            {"id": "2", "user_response": "no", "digest_run_at": self._iso(3)},
            {"id": "3", "user_response": "snooze", "digest_run_at": self._iso(3)},
        ])
        body = _call_tool(handler, "get_observations", {"status_filter": "yes"})
        payload = json.loads(body["result"]["content"][0]["text"])
        assert [o["id"] for o in payload["observations"]] == ["1"]

    def test_invalid_status_returns_tool_error(self, handler):
        self._register(handler, [])
        body = _call_tool(handler, "get_observations", {"status_filter": "nonsense"})
        assert body["result"]["isError"] is True

    def test_invalid_days_returns_tool_error(self, handler):
        self._register(handler, [])
        body = _call_tool(handler, "get_observations", {"days_back": 0})
        assert body["result"]["isError"] is True


# ---------------------------------------------------------------------
# query_sql_logger
# ---------------------------------------------------------------------


class TestQuerySqlLogger:
    def _register(self, handler, history_db, logger):
        mcp_tools._register_query_sql_logger(
            handler, history_db=history_db, logger=logger,
        )

    def test_happy_path(self, handler, logger):
        db = _FakeHistoryDB(result={"points": [[1, 2]], "min": 1, "max": 2, "current": 2, "type": "float"})
        self._register(handler, db, logger)
        body = _call_tool(handler, "query_sql_logger", {
            "device_id": 123, "column": "sensorValue", "time_range": "7d",
        })
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["current"] == 2
        assert db.calls == [{"device_id": 123, "column": "sensorValue", "time_range": "7d"}]

    def test_default_time_range_is_24h(self, handler, logger):
        db = _FakeHistoryDB(result={"points": []})
        self._register(handler, db, logger)
        _call_tool(handler, "query_sql_logger", {"device_id": 1, "column": "x"})
        assert db.calls[0]["time_range"] == "24h"

    def test_bad_device_id_type(self, handler, logger):
        self._register(handler, _FakeHistoryDB(), logger)
        body = _call_tool(handler, "query_sql_logger", {
            "device_id": "not-an-int", "column": "x",
        })
        assert body["result"]["isError"] is True

    def test_bad_time_range(self, handler, logger):
        self._register(handler, _FakeHistoryDB(), logger)
        body = _call_tool(handler, "query_sql_logger", {
            "device_id": 1, "column": "x", "time_range": "99d",
        })
        assert body["result"]["isError"] is True

    def test_empty_column_rejected(self, handler, logger):
        self._register(handler, _FakeHistoryDB(), logger)
        body = _call_tool(handler, "query_sql_logger", {
            "device_id": 1, "column": "  ",
        })
        assert body["result"]["isError"] is True

    def test_null_history_db_rejected(self, handler, logger):
        self._register(handler, None, logger)
        body = _call_tool(handler, "query_sql_logger", {
            "device_id": 1, "column": "x",
        })
        assert body["result"]["isError"] is True

    def test_db_exception_downgrades_to_tool_error(self, handler, logger):
        # DB failures for bad device/column are classified as input
        # errors so Claude can self-correct to a different device/col.
        db = _FakeHistoryDB(raises=Exception("table missing"))
        self._register(handler, db, logger)
        body = _call_tool(handler, "query_sql_logger", {
            "device_id": 1, "column": "x",
        })
        assert body["result"]["isError"] is True


# ---------------------------------------------------------------------
# house_context_snapshot
# ---------------------------------------------------------------------


class TestHouseContextSnapshot:
    def _register(self, handler, *, context=None, rule_store=None,
                  observation_store=None, logger=None):
        mcp_tools._register_house_context_snapshot(
            handler,
            context=context or _FakeContext(),
            rule_store=rule_store or _FakeRuleStore([]),
            observation_store=observation_store or _FakeObservationStore([]),
            logger=logger or logging.getLogger("test-hcs"),
        )

    def test_assembles_full_snapshot(self, handler, logger, monkeypatch):
        # Patch EventLogReader to avoid touching the filesystem.
        fake_events = [
            {"timestamp": "2026-04-22 10:00:00", "source": "Action", "message": "Door opened"},
        ]
        fake_summary = {"total_events": 1, "top_sources": {"Action": 1}}

        class _FakeEventLogReader:
            def __init__(self, logger=None):
                pass
            def read_window(self, days_back):
                return fake_events
            def summarise(self, events):
                return dict(fake_summary)

        monkeypatch.setattr(mcp_tools, "EventLogReader", _FakeEventLogReader)

        ctx = _FakeContext(
            house_model={
                "devices": [{"id": 1, "name": "Hall Light"}],
                "device_folders": [{"id": 10, "name": "Hallway"}],
                "indigo_triggers": [],
                "indigo_schedules": [],
                "action_groups": [],
            },
            health={"low_batteries": [], "low_batteries_total": 0,
                    "offline_devices": [], "offline_devices_total": 0},
            energy={"whole_house": {"device_id": 999}},
            sql={"1": {"total": 5}},
        )
        rs = _FakeRuleStore([{"id": "r1", "enabled": True}])
        obs = _FakeObservationStore([{"id": "o1", "user_response": "yes"}])
        self._register(handler, context=ctx, rule_store=rs, observation_store=obs, logger=logger)

        body = _call_tool(handler, "house_context_snapshot", {"days": 7})
        payload = json.loads(body["result"]["content"][0]["text"])

        assert payload["window_days"] == 7
        assert payload["devices"][0]["name"] == "Hall Light"
        assert payload["event_log_summary"]["total_events"] == 1
        assert payload["event_log_summary"]["sql_logger_rollups"] == {"1": {"total": 5}}
        assert payload["event_log_summary"]["health"]["low_batteries_total"] == 0
        assert payload["event_log_summary"]["energy"] == {"whole_house": {"device_id": 999}}
        assert len(payload["event_log_timeline"]) == 1
        assert payload["rules"][0]["id"] == "r1"
        assert payload["observations"][0]["id"] == "o1"

    def test_rejects_invalid_days(self, handler, logger):
        self._register(handler, logger=logger)
        body = _call_tool(handler, "house_context_snapshot", {"days": 0})
        assert body["result"]["isError"] is True

    def test_rejects_days_over_cap(self, handler, logger):
        self._register(handler, logger=logger)
        body = _call_tool(handler, "house_context_snapshot", {"days": 365})
        assert body["result"]["isError"] is True


# ---------------------------------------------------------------------
# register_all
# ---------------------------------------------------------------------


class TestRegisterAll:
    def test_registers_all_four_tools(self, handler, logger):
        mcp_tools.register_all(
            handler,
            context=_FakeContext(),
            rule_store=_FakeRuleStore([]),
            observation_store=_FakeObservationStore([]),
            history_db=_FakeHistoryDB(result={"points": []}),
            logger=logger,
        )
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        resp = handler.handle_request("POST", {"Accept": "application/json"}, json.dumps(rpc))
        names = sorted(t["name"] for t in json.loads(resp["content"])["result"]["tools"])
        assert names == [
            "get_observations",
            "get_rules",
            "house_context_snapshot",
            "query_sql_logger",
        ]
