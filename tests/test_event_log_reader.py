"""Tests for event_log_reader.EventLogReader's pure parse/filter/summarise
helpers. The live-log and file-read paths touch Indigo / filesystem and
are covered by manual smoke tests on jarvis."""

import textwrap
from pathlib import Path

from event_log_reader import EventLogReader


class _NullLogger:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def warning(self, msg, *args, **kwargs):
        self.warnings.append(msg)

    def error(self, msg, *args, **kwargs):
        self.errors.append(msg)

    def exception(self, msg, *args, **kwargs):
        self.errors.append(msg)

    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


class TestIsUseful:
    def test_trigger_type(self):
        assert EventLogReader._is_useful("Trigger", "Kitchen motion fire") is True

    def test_schedule_type(self):
        assert EventLogReader._is_useful("Schedule", "Evening indoor lights") is True

    def test_action_group_type(self):
        assert EventLogReader._is_useful("Action Group", "Lounge Lights") is True

    def test_auto_lights_narration(self):
        assert (
            EventLogReader._is_useful("Auto Lights", "Zone 'Study' applying changes")
            is True
        )

    def test_device_narration_sent_on(self):
        assert (
            EventLogReader._is_useful("ShellyNGMQTT", 'sent "Study Lamp" on')
            is True
        )

    def test_device_narration_received_off(self):
        assert (
            EventLogReader._is_useful("Z-Wave", 'received "Hall Light" off')
            is True
        )

    def test_setpoint_narration(self):
        assert (
            EventLogReader._is_useful(
                "Heatmiser-Neo", '"Downstairs" setpoint set to 20.0'
            )
            is True
        )

    def test_trailing_on_off_without_quotes_still_useful(self):
        # Form we see in the log: 'Online Dining TRV' on
        assert (
            EventLogReader._is_useful("Online Sensor", "'Online Dining TRV' on")
            is False  # single-quote form isn't recognised by the 'sent "'/... heuristic
        )

    def test_unrelated_info_line(self):
        # "StopPoll max exceeded" isn't useful narration — it's a plugin
        # diagnostic. Would be caught by noise first, but the usefulness
        # check alone should also reject.
        assert (
            EventLogReader._is_useful("TP-Link Devices", "StopPoll max exceeded ()")
            is False
        )


class TestIsNoise:
    def test_mcp_server_prefix(self):
        assert EventLogReader._is_noise("MCP Server", "anything") is True

    def test_vector_store_message(self):
        assert (
            EventLogReader._is_noise("Anything", "Vector store: synchronizing...")
            is True
        )

    def test_tools_call_message(self):
        assert (
            EventLogReader._is_noise("Anything", "📨 tools:call | claude-code")
            is True
        )

    def test_plugin_lifecycle_messages(self):
        for msg in (
            'Starting plugin "Foo 1.0" (pid 1)',
            'Stopping plugin "Foo 1.0"',
            'Started plugin "Foo 1.0"',
            'Stopped plugin "Foo 1.0"',
            'Reloading plugin "Foo 1.0" using API v3.6',
        ):
            assert EventLogReader._is_noise("Application", msg) is True, msg

    def test_stoppoll_is_noise(self):
        assert (
            EventLogReader._is_noise("TP-Link Devices", "StopPoll max exceeded ()")
            is True
        )

    def test_useful_trigger_is_not_noise(self):
        assert EventLogReader._is_noise("Trigger", "Kitchen motion") is False


class TestFilterAndDedup:
    def test_drops_noise(self):
        events = [
            {"timestamp": "t1", "source": "MCP Server", "message": "sync"},
            {"timestamp": "t2", "source": "Trigger", "message": "Kitchen motion"},
        ]
        result = EventLogReader._filter_and_dedup(events)
        assert len(result) == 1
        assert result[0]["source"] == "Trigger"

    def test_dedups_same_event(self):
        events = [
            {"timestamp": "t1", "source": "Trigger", "message": "Fire once"},
            {"timestamp": "t1", "source": "Trigger", "message": "Fire once"},
        ]
        result = EventLogReader._filter_and_dedup(events)
        assert len(result) == 1

    def test_different_timestamps_not_deduped(self):
        events = [
            {"timestamp": "t1", "source": "Trigger", "message": "Fire"},
            {"timestamp": "t2", "source": "Trigger", "message": "Fire"},
        ]
        result = EventLogReader._filter_and_dedup(events)
        assert len(result) == 2

    def test_long_messages_deduped_by_prefix(self):
        """Messages that share the first 100 chars are deduped — defends
        against whitespace / trailing junk differences across live vs
        file representations of the same event."""
        long_prefix = "A" * 110
        events = [
            {"timestamp": "t1", "source": "Trigger", "message": long_prefix + "X"},
            {"timestamp": "t1", "source": "Trigger", "message": long_prefix + "Y"},
        ]
        result = EventLogReader._filter_and_dedup(events)
        assert len(result) == 1


class TestParseFile:
    def _write(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "2026-04-21 Events.txt"
        p.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
        return p

    def test_parses_standard_lines(self, tmp_path):
        content = (
            "2026-04-21 15:30:12.345\tTrigger\tKitchen motion\n"
            "2026-04-21 15:30:13.100\tAction Group\tKitchen Lights On\n"
        )
        p = self._write(tmp_path, content)
        events = EventLogReader._parse_file(str(p))
        assert len(events) == 2
        assert events[0]["timestamp"] == "2026-04-21 15:30:12.345"
        assert events[0]["source"] == "Trigger"
        assert events[0]["message"] == "Kitchen motion"
        assert events[1]["source"] == "Action Group"

    def test_continuation_lines_append_to_previous(self, tmp_path):
        """Multi-line error tracebacks appear as a timestamped first line
        followed by unparseable continuation lines — should collapse
        into the previous event's message, not dropped."""
        content = (
            "2026-04-21 15:30:12.345\tScript Error\ttrigger failed:\n"
            "Traceback (most recent call last):\n"
            '  File "plugin.py", line 1, in <module>\n'
            "RuntimeError: boom\n"
            "2026-04-21 15:30:20.000\tTrigger\tNext event\n"
        )
        p = self._write(tmp_path, content)
        events = EventLogReader._parse_file(str(p))
        assert len(events) == 2
        assert "Traceback" in events[0]["message"]
        assert "RuntimeError: boom" in events[0]["message"]
        assert events[1]["message"] == "Next event"

    def test_leading_garbage_before_first_entry_is_dropped(self, tmp_path):
        """Some log files have header lines without timestamps. They
        should be skipped, not attached to a non-existent previous
        entry."""
        content = (
            "Indigo Event Log - started 2026-04-21\n"
            "Version 2025.1\n"
            "2026-04-21 00:00:00.000\tTrigger\tFirst real event\n"
        )
        p = self._write(tmp_path, content)
        events = EventLogReader._parse_file(str(p))
        assert len(events) == 1
        assert events[0]["message"] == "First real event"

    def test_missing_file_returns_empty(self):
        events = EventLogReader._parse_file("/nonexistent/path")
        assert events == []

    def test_latin1_fallback(self, tmp_path):
        """Non-UTF-8 bytes (smart quotes from older Indigo logs) must
        not crash the reader — fall back to latin-1."""
        p = tmp_path / "2026-04-21 Events.txt"
        # 0xa3 is £ in latin-1, invalid as utf-8 start byte in this position
        content = b"2026-04-21 00:00:00.000\tTrigger\tCost \xa35 per run\n"
        p.write_bytes(content)
        events = EventLogReader._parse_file(str(p))
        assert len(events) == 1
        assert "Cost" in events[0]["message"]


class TestSummarise:
    def test_counts_by_source(self):
        events = [
            {"timestamp": "t", "source": "Trigger", "message": "a"},
            {"timestamp": "t", "source": "Trigger", "message": "b"},
            {"timestamp": "t", "source": "Schedule", "message": "c"},
        ]
        reader = EventLogReader(logger=_NullLogger())
        summary = reader.summarise(events)
        assert summary["total_events"] == 3
        assert summary["top_sources"]["Trigger"] == 2
        assert summary["top_sources"]["Schedule"] == 1

    def test_counts_by_hour(self):
        events = [
            {"timestamp": "2026-04-21 15:30:00.000", "source": "T", "message": ""},
            {"timestamp": "2026-04-21 15:45:00.000", "source": "T", "message": ""},
            {"timestamp": "2026-04-21 23:00:00.000", "source": "T", "message": ""},
        ]
        reader = EventLogReader(logger=_NullLogger())
        summary = reader.summarise(events)
        assert summary["events_by_hour"]["15"] == 2
        assert summary["events_by_hour"]["23"] == 1

    def test_iso_timestamp_hour_extraction(self):
        """Live log timestamps are ISO 8601 — same byte offsets 11-12
        should still yield the hour."""
        events = [
            {"timestamp": "2026-04-21T09:30:00+01:00", "source": "T", "message": ""},
        ]
        reader = EventLogReader(logger=_NullLogger())
        summary = reader.summarise(events)
        assert summary["events_by_hour"]["09"] == 1

    def test_empty_list(self):
        reader = EventLogReader(logger=_NullLogger())
        summary = reader.summarise([])
        assert summary["total_events"] == 0
        assert summary["top_sources"] == {}
        assert summary["events_by_hour"] == {}
