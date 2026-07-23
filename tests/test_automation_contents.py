"""Tests for the automation-contents surface (issue #27 / ADR-0010):

- ``HouseContextAccess.automation_contents_context`` — fired-set
  scoping, 40-entry cap, compaction (labels + resolved names, no raw
  class codes), per-automation isolation, reader-failure propagation.
- ``HouseContextAccess.automations_acting_on`` — reverse index for
  the rule-write gate (acts_on filter + role tagging).
- ``DigestRunner`` wiring — fired-name extraction from the event
  timeline and the degrade-to-absent-block path.

These touch ``indigo.server`` / entity collections, which the Tier-A
strict stub blocks; the fixtures monkeypatch the needed attributes
per-test (``raising=False`` opt-in pattern, same as
test_digest_health_energy.py). ``indidb_reader`` itself never does
``import indigo`` — the module object is injected — so patching the
attributes on ``data_access.indigo`` covers both construction and
name resolution.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import data_access
from data_access import HouseContextAccess
from digest import DigestRunner

# Reuse the schema-recon fixture XML from the adapted reader tests so
# the two files can't drift apart on database shape.
from test_indidb_reader import FIXTURE


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _named(name):
    return SimpleNamespace(name=name)


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    """HouseContextAccess wired to a temp .indiDb via the strict-stub
    opt-in pattern. Device 333 is deliberately absent from the live
    collections so its resolved name stays None (orphan signal)."""
    db = tmp_path / "Test House.indiDb"
    db.write_text(FIXTURE, encoding="utf-8")
    monkeypatch.setattr(
        data_access.indigo, "server",
        SimpleNamespace(getDbFilePath=lambda: str(db)),
        raising=False,
    )
    monkeypatch.setattr(
        data_access.indigo, "devices",
        {111: _named("Kitchen Light"), 222: _named("Meter"),
         444: _named("Sonos Speaker")},
        raising=False,
    )
    monkeypatch.setattr(
        data_access.indigo, "variables",
        {555: _named("presence")},
        raising=False,
    )
    monkeypatch.setattr(
        data_access.indigo, "actionGroups",
        {100: _named("Morning.Scene"), 200: _named("Chained")},
        raising=False,
    )
    return HouseContextAccess(history_db=None, logger=MagicMock())


# ---------------------------------------------------------------------
# automation_contents_context — scoping
# ---------------------------------------------------------------------


class TestAutomationContentsScoping:
    def test_empty_fired_set_returns_empty_without_touching_reader(self):
        # No indigo patching at all — an empty fired set must
        # short-circuit before any reader construction/parse.
        ctx = HouseContextAccess(history_db=None, logger=MagicMock())
        assert ctx.automation_contents_context([]) == {
            "automations": [], "matched_total": 0,
        }
        assert ctx.automation_contents_context(None) == {
            "automations": [], "matched_total": 0,
        }

    def test_scopes_to_fired_names_only(self, ctx):
        result = ctx.automation_contents_context({"Zone 2.5 Heating"})
        assert result["matched_total"] == 1
        assert [a["name"] for a in result["automations"]] == [
            "Zone 2.5 Heating"
        ]
        assert result["automations"][0]["automation_type"] == "schedule"

    def test_matches_by_id_as_well_as_name(self, ctx):
        result = ctx.automation_contents_context({400, "Morning.Scene"})
        got = {(a["automation_type"], a["id"]) for a in result["automations"]}
        assert got == {("trigger", 400), ("action_group", 100)}

    def test_unmatched_names_yield_empty_block(self, ctx):
        result = ctx.automation_contents_context({"No Such Automation"})
        assert result == {"automations": [], "matched_total": 0}

    def test_bools_never_match_ids(self, ctx):
        # True == 1 in Python; a stray boolean must not match id 1 or
        # anything else.
        result = ctx.automation_contents_context({True})
        assert result["matched_total"] == 0

    def test_cap_at_40_with_matched_total(self, ctx, monkeypatch, tmp_path):
        groups = "\n".join(
            f"""<ActionGroup type="dict">
              <ActionSteps type="vector" />
              <ID type="integer">{1000 + i}</ID>
              <Name type="string">AG {i}</Name>
            </ActionGroup>"""
            for i in range(45)
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Database type="dict">\n'
            f'<ActionGroupList type="vector">\n{groups}\n</ActionGroupList>\n'
            '</Database>\n'
        )
        db = tmp_path / "Big House.indiDb"
        db.write_text(xml, encoding="utf-8")
        monkeypatch.setattr(
            data_access.indigo, "server",
            SimpleNamespace(getDbFilePath=lambda: str(db)),
            raising=False,
        )
        ctx = HouseContextAccess(history_db=None, logger=MagicMock())
        fired = {f"AG {i}" for i in range(45)}
        result = ctx.automation_contents_context(fired)
        assert result["matched_total"] == 45
        assert len(result["automations"]) == 40
        # Cap loss is its own counter — distinct from compact_errors.
        assert result["truncated"] == 5
        assert "compact_errors" not in result

    def test_exact_name_match_only_no_substring_or_prefix(self, ctx):
        # Contract pin: fired-name matching is message == record name
        # (after strip) — a prefix or substring of a real automation
        # name must NOT match.
        assert ctx.automation_contents_context(
            {"Zone 2.5"}                       # prefix
        )["matched_total"] == 0
        assert ctx.automation_contents_context(
            {"one 2.5 Heating"}                # substring
        )["matched_total"] == 0
        assert ctx.automation_contents_context(
            {"zone 2.5 heating"}               # case differs
        )["matched_total"] == 0
        assert ctx.automation_contents_context(
            {"  Zone 2.5 Heating  "}           # stripped, then exact
        )["matched_total"] == 1


# ---------------------------------------------------------------------
# automation_contents_context — compaction
# ---------------------------------------------------------------------


class TestAutomationContentsCompaction:
    def test_steps_carry_labels_and_names_not_class_codes(self, ctx):
        sched = ctx.automation_contents_context({"Zone 2.5 Heating"})[
            "automations"
        ][0]
        turn_on, hvac = sched["steps"]
        assert turn_on == {
            "do": "turn_on",
            "device_id": 111,
            "device_name": "Kitchen Light",
            "value": 0,
        }
        assert hvac["do"] == "set_heat_setpoint"
        assert hvac["value"] == 13.0
        # No raw decoding noise anywhere in the compacted record.
        for step in sched["steps"]:
            assert "class" not in step
            assert "action_code" not in step
            assert "action_label" not in step

    def test_orphaned_device_reference_keeps_explicit_null_name(self, ctx):
        # Device 333 exists in the DB but not in the live collections
        # — the compacted step must say so explicitly, not omit it.
        sched = ctx.automation_contents_context({"Zone 2.5 Heating"})[
            "automations"
        ][0]
        hvac = sched["steps"][1]
        assert hvac["device_id"] == 333
        assert hvac["device_name"] is None

    def test_condition_tree_compacted_with_logic_and_names(self, ctx):
        cond = ctx.automation_contents_context({"Zone 2.5 Heating"})[
            "automations"
        ][0]["conditions"]
        assert cond["logic"] == "all"
        leaf, nested = cond["conditions"]
        assert leaf == {
            "device_id": 111,
            "device_name": "Kitchen Light",
            "state": "onOffState",
            "comparator": "is_false",
        }
        assert nested["logic"] == "any"
        var_leaf, time_leaf = nested["conditions"]
        assert var_leaf["variable_name"] == "presence"
        assert var_leaf["comparator"] == "is_true"
        assert time_leaf == {
            "time_window": {"start": "08:00", "end": "22:00"}
        }
        # Code fields dropped throughout.
        assert "comparator_code" not in leaf
        assert "logic_code" not in cond

    def test_trigger_gets_watch_and_disabled_flag(self, ctx):
        trig = ctx.automation_contents_context({"Door opened"})[
            "automations"
        ][0]
        assert trig["automation_type"] == "trigger"
        # Trigger 400 is Enabled=false in the fixture — surprising for
        # a "fired" automation, so the flag must survive compaction.
        assert trig["enabled"] is False
        assert trig["watch"]["device_id"] == 111
        assert trig["watch"]["device_name"] == "Kitchen Light"
        assert trig["watch"]["state_selector"] == "onOffState"
        # Steps: exec-group resolves the group's name; plugin action
        # keeps its label; unknown class stays numeric.
        exec_step, plugin_step, unknown_step = trig["steps"]
        assert exec_step == {
            "do": "execute_action_group",
            "action_group_id": 100,
            "action_group_name": "Morning.Scene",
        }
        assert plugin_step["do"] == "plugin_action"
        assert plugin_step["label"] == "Sonos: Stop"
        assert plugin_step["device_name"] == "Sonos Speaker"
        assert unknown_step == {"do": "unknown_action_class_42"}

    def test_type0_conditions_omitted_entirely(self, ctx):
        trig = ctx.automation_contents_context({"Door opened"})[
            "automations"
        ][0]
        assert "conditions" not in trig

    def test_variable_watch_compacted(self, ctx):
        trig = ctx.automation_contents_context({"Var watcher"})[
            "automations"
        ][0]
        assert trig["watch"] == {
            "variable_id": 555,
            "variable_name": "presence",
            "value": "true",
        }

    def test_script_step_excerpted_and_variable_step_named(self, ctx):
        group = ctx.automation_contents_context({"Morning.Scene"})[
            "automations"
        ][0]
        var_step = group["steps"][2]
        assert var_step == {
            "do": "set_value",
            "variable_id": 555,
            "variable_name": "presence",
            "value": "true",
        }
        script = group["steps"][3]
        assert script["do"] == "embedded_script"
        assert script["language"] == "python"
        assert len(script["source_excerpt"]) == 200
        assert script["truncated"] is True

    def test_short_script_not_truncated(self, ctx):
        group = ctx.automation_contents_context({"Chained"})[
            "automations"
        ][0]
        script = group["steps"][1]
        assert script["source_excerpt"] == 'print("hi")'
        assert script["truncated"] is False


# ---------------------------------------------------------------------
# automation_contents_context — isolation + failure propagation
# ---------------------------------------------------------------------


class _FakeReader:
    """Hand-built reader stand-in for isolation / failure tests."""

    def __init__(self, data=None, raises=None):
        self._data = data
        self._raises = raises

    def automations(self):
        if self._raises is not None:
            raise self._raises
        return self._data

    def resolve_name(self, collection, entity_id):
        return None


class TestAutomationContentsIsolation:
    def test_one_malformed_record_does_not_kill_the_block(self):
        logger = MagicMock()
        ctx = HouseContextAccess(history_db=None, logger=logger)
        ctx._indidb_reader = _FakeReader(data={
            "schedule": {},
            "trigger": {},
            "action_group": {
                # steps=42 → iteration raises TypeError mid-compaction.
                1: {"id": 1, "name": "Broken", "steps": 42,
                    "conditions": None},
                2: {"id": 2, "name": "Fine", "steps": [],
                    "conditions": None},
            },
            "skipped_automations": 0,
        })
        result = ctx.automation_contents_context({"Broken", "Fine"})
        assert [a["name"] for a in result["automations"]] == ["Fine"]
        assert result["matched_total"] == 2
        # Compaction loss is its own counter — distinct from the cap.
        assert result["compact_errors"] == 1
        assert "truncated" not in result
        assert logger.warning.called
        assert "Broken" in str(logger.warning.call_args_list)

    def test_skipped_automations_count_surfaces(self):
        ctx = HouseContextAccess(history_db=None, logger=MagicMock())
        ctx._indidb_reader = _FakeReader(data={
            "schedule": {},
            "trigger": {},
            "action_group": {
                2: {"id": 2, "name": "Fine", "steps": [],
                    "conditions": None},
            },
            "skipped_automations": 3,
        })
        result = ctx.automation_contents_context({"Fine"})
        assert result["skipped_automations"] == 3

    def test_reader_failure_propagates_to_caller(self):
        ctx = HouseContextAccess(history_db=None, logger=MagicMock())
        ctx._indidb_reader = _FakeReader(
            raises=ValueError("mid-write; retry")
        )
        with pytest.raises(ValueError, match="retry"):
            ctx.automation_contents_context({"Anything"})


# ---------------------------------------------------------------------
# automations_acting_on
# ---------------------------------------------------------------------


class TestAutomationsActingOn:
    def test_acts_on_filter_and_role_tagging(self, ctx):
        refs = ctx.automations_acting_on(111)
        by_id = {(r["automation_type"], r["id"]): r for r in refs}
        # Schedule 300 acts on 111 (turn_on step) AND has it in its
        # condition tree.
        assert by_id[("schedule", 300)]["roles"] == ["acts_on", "condition"]
        # Action group 100 acts on 111 (set_brightness step).
        assert by_id[("action_group", 100)]["roles"] == ["acts_on"]
        # Trigger 400 only WATCHES 111 — no action step targets it, so
        # it must not appear (the gate cares about actuation).
        assert ("trigger", 400) not in by_id
        assert len(refs) == 2

    def test_untouched_device_returns_empty(self, ctx):
        assert ctx.automations_acting_on(987654) == []

    def test_reader_failure_propagates(self):
        ctx = HouseContextAccess(history_db=None, logger=MagicMock())
        ctx._indidb_reader = _FakeReader(raises=ValueError("no path"))
        with pytest.raises(ValueError, match="no path"):
            ctx.automations_acting_on(111)


# ---------------------------------------------------------------------
# Digest wiring
# ---------------------------------------------------------------------


def _bare_runner(context=None):
    """DigestRunner without __init__ (which builds an Anthropic client
    and event-log reader) — the two helpers under test only need
    .context and .logger."""
    runner = DigestRunner.__new__(DigestRunner)
    runner.context = context
    runner.logger = MagicMock()
    return runner


class TestFiredAutomationNames:
    def test_extracts_from_trigger_schedule_and_action_group_lines(self):
        events = [
            {"source": "Trigger", "message": "Door opened"},
            {"source": "Schedule", "message": "Zone 2.5 Heating"},
            {"source": "Action Group", "message": "Morning.Scene"},
            {"source": "Z-Wave", "message": 'sent "Kitchen Light" on'},
            {"source": "Auto Lights", "message": "Zone Study occupied"},
            {"source": "Trigger", "message": "  Door opened  "},  # dedup
            {"source": "Trigger", "message": ""},                 # empty
        ]
        assert DigestRunner._fired_automation_names(events) == {
            "Door opened", "Zone 2.5 Heating", "Morning.Scene",
        }

    def test_empty_events_yield_empty_set(self):
        assert DigestRunner._fired_automation_names([]) == set()


class TestAttachAutomationContents:
    def test_attaches_block_when_contents_found(self):
        context = MagicMock()
        context.automation_contents_context.return_value = {
            "automations": [{"automation_type": "trigger", "id": 400,
                             "name": "Door opened", "steps": []}],
            "matched_total": 1,
        }
        runner = _bare_runner(context)
        summary = {}
        events = [{"source": "Trigger", "message": "Door opened"}]
        runner._attach_automation_contents(summary, events)
        assert summary["automation_contents"]["matched_total"] == 1
        context.automation_contents_context.assert_called_once_with(
            {"Door opened"}
        )

    def test_no_fired_automations_skips_context_call(self):
        context = MagicMock()
        runner = _bare_runner(context)
        summary = {}
        runner._attach_automation_contents(
            summary, [{"source": "Z-Wave", "message": "chatter"}]
        )
        assert "automation_contents" not in summary
        context.automation_contents_context.assert_not_called()

    def test_zero_match_leaves_block_absent_and_names_unmatched(self):
        context = MagicMock()
        context.automation_contents_context.return_value = {
            "automations": [], "matched_total": 0,
        }
        runner = _bare_runner(context)
        summary = {}
        runner._attach_automation_contents(
            summary, [{"source": "Trigger", "message": "Ghost"}]
        )
        assert "automation_contents" not in summary
        # The omission must be diagnosable: warning names the fired
        # automations that matched nothing in the database.
        assert runner.logger.warning.called
        assert "Ghost" in str(runner.logger.warning.call_args)

    def test_matched_but_undecoded_block_still_attached(self):
        # "40 matched, 0 decoded" must be visible to Claude, not an
        # invisible drop: matched_total > 0 attaches even with an
        # empty automations list.
        context = MagicMock()
        context.automation_contents_context.return_value = {
            "automations": [], "matched_total": 3, "compact_errors": 3,
        }
        runner = _bare_runner(context)
        summary = {}
        runner._attach_automation_contents(
            summary, [{"source": "Trigger", "message": "Door opened"}]
        )
        assert summary["automation_contents"]["matched_total"] == 3
        assert summary["automation_contents"]["automations"] == []
        runner.logger.warning.assert_not_called()

    def test_reader_failure_degrades_to_absent_block_with_warning(self):
        context = MagicMock()
        context.automation_contents_context.side_effect = ValueError(
            "the Indigo server may be mid-write; retry in a few seconds"
        )
        runner = _bare_runner(context)
        summary = {"health": {}}
        runner._attach_automation_contents(
            summary, [{"source": "Trigger", "message": "Door opened"}]
        )
        assert "automation_contents" not in summary
        assert summary["health"] == {}  # rest of the summary untouched
        assert runner.logger.warning.called


class TestRunPathAssembly:
    """End-to-end over the run() assembly seam: real HouseContextAccess
    + real .indiDb fixture feeding _attach_automation_contents, then
    _build_user_message — asserting the block lands INSIDE the emitted
    ``event_log_summary`` fenced JSON exactly as Claude will see it.
    (run() itself needs a live Anthropic client + SMTP; the two
    methods chained here are the complete summary-to-prompt path.)"""

    @staticmethod
    def _parse_fenced_block(message: str, fence_tag: str) -> str:
        start_marker = f"```{fence_tag}\n"
        start = message.index(start_marker) + len(start_marker)
        end = message.index("\n```", start)
        return message[start:end]

    def test_block_lands_in_emitted_event_log_summary_json(self, ctx):
        import json
        from datetime import datetime, timedelta, timezone

        runner = DigestRunner(
            context=ctx,
            rule_store=None,
            observation_store=None,
            delivery=None,
            api_key="",
            model="claude-sonnet-4-6",
            email_to="nobody@example.com",
            logger=MagicMock(),
        )
        events = [
            {"timestamp": "2026-07-20 09:00:00.000",
             "source": "Trigger", "message": "Door opened"},
            {"timestamp": "2026-07-20 21:00:00.000",
             "source": "Schedule", "message": "Zone 2.5 Heating"},
            {"timestamp": "2026-07-20 21:00:01.000",
             "source": "Z-Wave", "message": 'sent "Kitchen Light" on'},
        ]
        # Same assembly order as run(): summarise, enrich, attach,
        # then build the user message.
        summary = runner.event_log.summarise(events)
        summary["sql_logger_rollups"] = {}
        runner._attach_automation_contents(summary, events)

        now = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
        message = runner._build_user_message(
            now, now - timedelta(days=7), 7, events, summary
        )
        body = json.loads(
            self._parse_fenced_block(message, "event_log_summary")
        )
        block = body["automation_contents"]
        assert block["matched_total"] == 2
        assert {a["name"] for a in block["automations"]} == {
            "Door opened", "Zone 2.5 Heating",
        }
        # Decoded content (not just names) made it into the prompt.
        sched = next(a for a in block["automations"]
                     if a["name"] == "Zone 2.5 Heating")
        assert sched["steps"][0]["do"] == "turn_on"
        assert sched["steps"][0]["device_name"] == "Kitchen Light"
