"""Tests for DigestRunner._build_user_message — the volatile block
sent below the cache breakpoint. Locks in the fenced-JSON format so
Claude can deterministically parse ``event_log_summary`` and
``event_log_timeline`` without having to guess at pipe delimiters."""

import json
from datetime import datetime, timedelta, timezone

from digest import DigestRunner


class _NullLogger:
    def debug(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def exception(self, *a, **kw): pass


def _runner():
    """DigestRunner with every collaborator stubbed — we only call
    _build_user_message which touches none of them."""
    return DigestRunner(
        context=None,
        rule_store=None,
        observation_store=None,
        delivery=None,
        api_key="",
        model="claude-sonnet-4-5",
        email_to="nobody@example.com",
        logger=_NullLogger(),
    )


def _parse_fenced_block(message: str, fence_tag: str) -> str:
    """Extract the body of a ```fence_tag ... ``` block from the message."""
    start_marker = f"```{fence_tag}\n"
    start = message.index(start_marker) + len(start_marker)
    end = message.index("\n```", start)
    return message[start:end]


class TestBuildUserMessage:
    def test_emits_fenced_summary_and_timeline_blocks(self):
        runner = _runner()
        now = datetime(2026, 4, 21, 14, 30, tzinfo=timezone.utc)
        since = now - timedelta(days=7)
        events = [
            {"timestamp": "2026-04-21 09:00:00.000", "source": "Trigger", "message": "Kitchen motion"},
            {"timestamp": "2026-04-21 09:05:00.000", "source": "Action Group", "message": "Lights on"},
        ]
        summary = {
            "total_events": 2,
            "top_sources": {"Trigger": 1, "Action Group": 1},
            "events_by_hour": {"09": 2},
            "sql_logger_rollups": {"123": {"changes_7d": 47}},
        }
        message = runner._build_user_message(now, since, 7, events, summary)

        # Both fenced blocks present.
        assert "```event_log_summary" in message
        assert "```event_log_timeline" in message

        # Summary body is valid JSON and round-trips.
        summary_body = _parse_fenced_block(message, "event_log_summary")
        assert json.loads(summary_body) == summary

        # Timeline is one JSON *array* per line with positional fields
        # ["YYYY-MM-DD HH:MM:SS", source, message]. Milliseconds are
        # sliced off for token efficiency; year is kept so windows that
        # span New Year (e.g. late-December → early-January run) sort
        # correctly.
        timeline_body = _parse_fenced_block(message, "event_log_timeline")
        lines = timeline_body.split("\n")
        assert len(lines) == len(events)
        for line, original in zip(lines, events):
            arr = json.loads(line)
            assert isinstance(arr, list) and len(arr) == 3
            assert arr[0] == original["timestamp"][:19]
            assert arr[1] == original["source"]
            assert arr[2] == original["message"]

    def test_timeline_preserves_year_across_new_year(self):
        """Digest window can span 31 Dec / 1 Jan. Year-less timestamps
        would sort wrongly (01-02 before 12-31). Full-year timestamps
        are chronologically comparable."""
        runner = _runner()
        now = datetime(2026, 1, 4, 10, 0, tzinfo=timezone.utc)
        since = now - timedelta(days=7)
        events = [
            {"timestamp": "2025-12-29 10:00:00.000", "source": "Trigger", "message": "old year"},
            {"timestamp": "2026-01-02 10:00:00.000", "source": "Trigger", "message": "new year"},
        ]
        summary = {"total_events": 2, "top_sources": {}, "events_by_hour": {}, "sql_logger_rollups": {}}
        message = runner._build_user_message(now, since, 7, events, summary)
        timeline_body = _parse_fenced_block(message, "event_log_timeline")
        lines = timeline_body.split("\n")
        assert json.loads(lines[0])[0] == "2025-12-29 10:00:00"
        assert json.loads(lines[1])[0] == "2026-01-02 10:00:00"

    def test_timeline_is_compact_no_indent(self):
        """Compact JSON (no whitespace between tokens) — avoids paying
        for indentation in the uncached user-message block."""
        runner = _runner()
        now = datetime(2026, 4, 21, 14, 30, tzinfo=timezone.utc)
        since = now - timedelta(days=7)
        events = [
            {"timestamp": "2026-04-21 09:00:00.000", "source": "Trigger", "message": "X"}
        ]
        summary = {"total_events": 1, "top_sources": {}, "events_by_hour": {}, "sql_logger_rollups": {}}
        message = runner._build_user_message(now, since, 7, events, summary)
        # No ", " (with space) in a valid compact-JSON array — separators
        # should be "," only.
        timeline_body = _parse_fenced_block(message, "event_log_timeline")
        assert ", " not in timeline_body
        # Summary body is also compact.
        summary_body = _parse_fenced_block(message, "event_log_summary")
        assert ", " not in summary_body

    def test_multiline_message_preserved_in_timeline(self):
        """A trigger that failed with a traceback logs a multi-line
        message. Pipe-delimited rendering (the old format) would corrupt
        it by interpreting embedded newlines as row boundaries. JSON-lines
        preserves the newlines inside the string."""
        runner = _runner()
        now = datetime(2026, 4, 21, 14, 30, tzinfo=timezone.utc)
        since = now - timedelta(days=7)
        traceback_msg = (
            "trigger failed:\n"
            "Traceback (most recent call last):\n"
            '  File "plugin.py", line 1, in <module>\n'
            "RuntimeError: boom"
        )
        events = [
            {"timestamp": "2026-04-21 09:00:00.000", "source": "Script Error", "message": traceback_msg}
        ]
        summary = {"total_events": 1, "top_sources": {}, "events_by_hour": {}, "sql_logger_rollups": {}}
        message = runner._build_user_message(now, since, 7, events, summary)

        timeline_body = _parse_fenced_block(message, "event_log_timeline")
        # Should be exactly one JSON line — the embedded newlines live
        # inside the JSON string at position [2] of the array, not as
        # timeline row separators.
        assert len(timeline_body.split("\n")) == 1
        arr = json.loads(timeline_body)
        assert "Traceback" in arr[2]
        assert "RuntimeError: boom" in arr[2]

    def test_empty_events_still_emits_fenced_blocks(self):
        runner = _runner()
        now = datetime(2026, 4, 21, 14, 30, tzinfo=timezone.utc)
        since = now - timedelta(days=7)
        summary = {"total_events": 0, "top_sources": {}, "events_by_hour": {}, "sql_logger_rollups": {}}
        message = runner._build_user_message(now, since, 7, [], summary)

        # Fences still present so the schema stays consistent.
        assert "```event_log_summary" in message
        assert "```event_log_timeline" in message
        # Timeline body is empty.
        timeline_body = _parse_fenced_block(message, "event_log_timeline")
        assert timeline_body == ""

    def test_local_time_header_present(self):
        runner = _runner()
        now = datetime(2026, 4, 21, 14, 30, tzinfo=timezone.utc)
        since = now - timedelta(days=7)
        summary = {"total_events": 0, "top_sources": {}, "events_by_hour": {}, "sql_logger_rollups": {}}
        message = runner._build_user_message(now, since, 7, [], summary)

        assert "Current local time:" in message
        assert "Digest window: last 7 days" in message
        assert "2026-04-14 to 2026-04-21" in message
