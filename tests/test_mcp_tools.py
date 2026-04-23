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
        self._observations = list(observations)
        self.responses = []   # log of record_response calls

    def list_all(self):
        return list(self._observations)

    def get(self, observation_id):
        for obs in self._observations:
            if obs.get("id") == observation_id:
                return dict(obs)
        return None

    def record_response(self, observation_id, response, body="", rule_id=None):
        self.responses.append(
            {"id": observation_id, "response": response,
             "body": body, "rule_id": rule_id}
        )
        for obs in self._observations:
            if obs.get("id") == observation_id:
                obs["user_response"] = response
                if rule_id is not None:
                    obs["rule_id"] = rule_id
                return True
        return False


class _CapturingRuleStore:
    """RuleStore fake that records every mutation for assertion."""
    def __init__(self, rules=None):
        self._rules = {r["id"]: dict(r) for r in (rules or [])}
        self._counter = 0
        self.added = []
        self.updated = []
        self.deleted = []

    def list_rules(self):
        return list(self._rules.values())

    def get_rule(self, rule_id):
        r = self._rules.get(rule_id)
        return dict(r) if r else None

    def add_rule(self, rule):
        self._counter += 1
        new_id = f"rule-{self._counter}"
        record = dict(rule)
        record["id"] = new_id
        record.setdefault("enabled", True)
        record.setdefault("fires_count", 0)
        self._rules[new_id] = record
        self.added.append(record)
        return new_id

    def update_rule(self, rule_id, **changes):
        self.updated.append({"id": rule_id, "changes": changes})
        if rule_id not in self._rules:
            return False
        self._rules[rule_id].update(changes)
        return True

    def delete_rule(self, rule_id):
        self.deleted.append(rule_id)
        return self._rules.pop(rule_id, None) is not None


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

    def test_registers_digest_instructions_resource(self, handler, logger):
        mcp_tools.register_all(
            handler,
            context=_FakeContext(),
            rule_store=_FakeRuleStore([]),
            observation_store=_FakeObservationStore([]),
            history_db=_FakeHistoryDB(result={"points": []}),
            logger=logger,
        )
        # resources/list surfaces it.
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "resources/list"}
        resp = handler.handle_request("POST", {"Accept": "application/json"}, json.dumps(rpc))
        resources = json.loads(resp["content"])["result"]["resources"]
        uris = [r["uri"] for r in resources]
        assert mcp_tools.DIGEST_INSTRUCTIONS_URI in uris

        # resources/read returns the real INSTRUCTIONS content.
        from digest import INSTRUCTIONS
        rpc = {
            "jsonrpc": "2.0", "id": 2, "method": "resources/read",
            "params": {"uri": mcp_tools.DIGEST_INSTRUCTIONS_URI},
        }
        resp = handler.handle_request("POST", {"Accept": "application/json"}, json.dumps(rpc))
        body = json.loads(resp["content"])
        assert body["result"]["contents"][0]["text"] == INSTRUCTIONS
        assert body["result"]["contents"][0]["mimeType"] == "text/markdown"


# ---------------------------------------------------------------------
# propose_rule / add_rule / update_rule (write tools)
# ---------------------------------------------------------------------


def _valid_rule(description="nightly off"):
    """Shared fixture: a schema-valid rule the safety check will
    accept as long as the test's safety_check fake returns True."""
    return {
        "description": description,
        "when": {"device_id": 100, "state": "onState", "equals": True, "for_minutes": 30},
        "then": {"device_id": 200, "op": "off"},
    }


class TestProposeRule:
    def _register(self, handler, safety_check=lambda _id: True):
        import mcp_tools
        mcp_tools._register_propose_rule(handler, safety_check=safety_check)

    def test_valid_rule_returns_preview(self, handler):
        # Need to monkey-patch _render_rule_human because its
        # implementation lives on plugin.py and imports indigo.
        import mcp_tools, sys, types
        fake_plugin = types.ModuleType("plugin")
        fake_plugin._render_rule_human = lambda rule: f"preview of {rule['description']}"
        sys.modules["plugin"] = fake_plugin
        self._register(handler, safety_check=lambda _id: True)
        body = _call_tool(handler, "propose_rule", {"rule": _valid_rule()})
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["stage"] == "validated"
        assert "nightly off" in payload["preview"]

    def test_schema_error_short_circuits_safety(self, handler):
        # safety_check that would raise if called — asserts we never
        # reach it on a schema failure.
        def boom(_id):
            raise AssertionError("safety_check called despite schema error")
        self._register(handler, safety_check=boom)
        bad_rule = {"description": "missing when/then"}
        body = _call_tool(handler, "propose_rule", {"rule": bad_rule})
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["ok"] is False
        assert payload["stage"] == "schema"

    def test_unsafe_target_returns_safety_stage(self, handler):
        self._register(handler, safety_check=lambda _id: False)
        body = _call_tool(handler, "propose_rule", {"rule": _valid_rule()})
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["ok"] is False
        assert payload["stage"] == "safety"
        assert payload["target_device_id"] == 200
        # Rationale surfaces the device classes that trigger rejection
        # so Claude can narrate it back to the user — assert the
        # specifics we rely on in the field description.
        rationale = payload["allowlist_rationale"].lower()
        assert "thermostat" in rationale
        assert "dimmer" in rationale or "relay" in rationale


class TestAddRule:
    def _register(self, handler, *, safety_check=lambda _id: True,
                  rule_store=None, observation_store=None,
                  send_confirmation=None, send_rejection=None,
                  logger=None):
        import mcp_tools, sys, types
        # Stub plugin._render_rule_human to keep import cheap.
        if "plugin" not in sys.modules:
            sys.modules["plugin"] = types.ModuleType("plugin")
        sys.modules["plugin"]._render_rule_human = lambda rule: "preview"

        mcp_tools._register_add_rule(
            handler,
            rule_store=rule_store or _CapturingRuleStore(),
            observation_store=observation_store or _FakeObservationStore([]),
            safety_check=safety_check,
            send_confirmation=send_confirmation,
            send_rejection=send_rejection,
            logger=logger or logging.getLogger("test-add-rule"),
        )

    def test_writes_rule_and_sends_confirmation_when_safe(self, handler):
        rs = _CapturingRuleStore()
        sent = []
        self._register(
            handler,
            rule_store=rs,
            send_confirmation=lambda obs, rid, rule: sent.append((obs, rid, rule)),
        )
        body = _call_tool(handler, "add_rule", {"rule": _valid_rule()})
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["rule_id"] == "rule-1"
        assert len(rs.added) == 1
        assert rs.added[0]["description"] == "nightly off"
        # Chat-initiated (no from_observation_id) — confirmation email
        # still fires with a synthetic observation shell.
        assert len(sent) == 1
        shell, rule_id, _rule = sent[0]
        assert rule_id == "rule-1"
        assert shell["id"].startswith("mcp-")

    def test_schema_error_raises_value_error(self, handler):
        self._register(handler)
        body = _call_tool(handler, "add_rule", {"rule": {"description": "no when"}})
        # ValueError → isError: true tool result.
        assert body["result"]["isError"] is True

    def test_unsafe_target_refused_and_no_write(self, handler):
        rs = _CapturingRuleStore()
        sent_conf = []
        sent_rej = []
        self._register(
            handler,
            safety_check=lambda _id: False,
            rule_store=rs,
            send_confirmation=lambda *a: sent_conf.append(a),
            send_rejection=lambda *a: sent_rej.append(a),
        )
        body = _call_tool(handler, "add_rule", {"rule": _valid_rule()})
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["ok"] is False
        assert payload["stage"] == "safety"
        assert rs.added == []
        # No confirmation, and no rejection email either — rejection
        # emails only fire when we have an observation to link against.
        assert sent_conf == []
        assert sent_rej == []

    def test_unsafe_target_with_observation_sends_rejection(self, handler):
        rs = _CapturingRuleStore()
        obs_store = _FakeObservationStore([
            {"id": "obs-1", "headline": "flag X"},
        ])
        sent_rej = []
        self._register(
            handler,
            safety_check=lambda _id: False,
            rule_store=rs,
            observation_store=obs_store,
            send_rejection=lambda obs, target: sent_rej.append((obs, target)),
        )
        body = _call_tool(handler, "add_rule", {
            "rule": _valid_rule(),
            "from_observation_id": "obs-1",
        })
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["ok"] is False
        assert rs.added == []
        # Observation marked rejected; rejection email sent once.
        assert any(r["response"] == "rejected_unsafe_target"
                   for r in obs_store.responses)
        assert len(sent_rej) == 1

    def test_from_observation_id_updates_observation_on_success(self, handler):
        rs = _CapturingRuleStore()
        obs_store = _FakeObservationStore([
            {"id": "obs-42", "headline": "Something noisy", "user_response": None},
        ])
        self._register(
            handler,
            rule_store=rs,
            observation_store=obs_store,
            send_confirmation=lambda *a: None,
        )
        body = _call_tool(handler, "add_rule", {
            "rule": _valid_rule(),
            "from_observation_id": "obs-42",
        })
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["ok"] is True
        recorded = [r for r in obs_store.responses if r["id"] == "obs-42"]
        assert len(recorded) == 1
        assert recorded[0]["response"] == "yes"
        assert recorded[0]["rule_id"] == "rule-1"

    def test_from_observation_id_not_found_raises(self, handler):
        self._register(handler)
        body = _call_tool(handler, "add_rule", {
            "rule": _valid_rule(),
            "from_observation_id": "nope",
        })
        assert body["result"]["isError"] is True


class TestUpdateRule:
    def _register(self, handler, rules=None):
        import mcp_tools
        store = _CapturingRuleStore(rules or [
            {"id": "r1", "enabled": True, "description": "noon off"},
            {"id": "r2", "enabled": True, "auto_disabled": True,
             "auto_disabled_reason": "10 failures",
             "auto_disabled_at": "2026-04-22T10:00:00", "description": "x"},
        ])
        mcp_tools._register_update_rule(
            handler, rule_store=store, logger=logging.getLogger("test-update"),
        )
        return store

    def test_disable_marks_rule_disabled(self, handler):
        store = self._register(handler)
        body = _call_tool(handler, "update_rule", {"rule_id": "r1", "action": "disable"})
        payload = json.loads(body["result"]["content"][0]["text"])
        assert payload["ok"] is True
        assert payload["action"] == "disable"
        assert store.updated[-1] == {"id": "r1", "changes": {"enabled": False}}

    def test_enable_clears_auto_disabled_metadata(self, handler):
        store = self._register(handler)
        body = _call_tool(handler, "update_rule", {"rule_id": "r2", "action": "enable"})
        assert json.loads(body["result"]["content"][0]["text"])["ok"] is True
        change = store.updated[-1]["changes"]
        assert change["enabled"] is True
        assert change["auto_disabled"] is False
        assert change["auto_disabled_reason"] is None
        assert change["auto_disabled_at"] is None

    def test_delete_removes_rule(self, handler):
        store = self._register(handler)
        body = _call_tool(handler, "update_rule", {"rule_id": "r1", "action": "delete"})
        assert json.loads(body["result"]["content"][0]["text"])["ok"] is True
        assert "r1" in store.deleted

    def test_unknown_rule_errors(self, handler):
        self._register(handler)
        body = _call_tool(handler, "update_rule", {"rule_id": "ghost", "action": "disable"})
        assert body["result"]["isError"] is True

    def test_unknown_action_errors(self, handler):
        self._register(handler)
        body = _call_tool(handler, "update_rule", {"rule_id": "r1", "action": "frobnicate"})
        assert body["result"]["isError"] is True
