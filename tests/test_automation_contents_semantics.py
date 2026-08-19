"""Digest compaction of the facts lite's reader gained in its #53-#56
arc: sunrise/sunset conditions and a schedule's firing rule.

Both matter to the digest's "is this already automated?" reasoning,
and both would degrade quietly without a compaction branch of their
own: a sun condition would keep its type and lose its state, and a
schedule would arrive with no firing information at all.

Fixture XML comes from the adapted reader-semantics tests so this
file and the reader copy cannot drift apart on database shape.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import data_access
from data_access import HouseContextAccess

from test_indidb_reader_semantics import FIXTURE


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    """HouseContextAccess over the semantics fixture, via the same
    strict-stub opt-in pattern as test_automation_contents."""
    db = tmp_path / "Semantics.indiDb"
    db.write_text(FIXTURE, encoding="utf-8")
    monkeypatch.setattr(
        data_access.indigo, "server",
        SimpleNamespace(getDbFilePath=lambda: str(db)),
        raising=False,
    )
    for attr in ("devices", "variables", "actionGroups"):
        monkeypatch.setattr(data_access.indigo, attr, {}, raising=False)
    return HouseContextAccess(history_db=None, logger=MagicMock())


def _only(ctx, name):
    result = ctx.automation_contents_context({name})
    assert result["matched_total"] == 1, result
    return result["automations"][0]


# ---------------------------------------------------------------------
# Sun conditions
# ---------------------------------------------------------------------

def test_sun_condition_keeps_its_state_not_just_its_type(ctx):
    automation = _only(ctx, "Kitchen Lights On - Daytime")
    states = [c.get("sun") for c in automation["conditions"]["conditions"]]
    assert "daylight" in states
    assert "dark" in states


def test_unrecognised_condition_still_falls_through_to_type(ctx):
    # The generic branch must survive — an unknown leaf should say so
    # rather than being dropped or mislabelled as a sun condition.
    automation = _only(ctx, "Kitchen Lights On - Daytime")
    assert {"type": "unknown"} in automation["conditions"]["conditions"]


# ---------------------------------------------------------------------
# Schedule firing rule
# ---------------------------------------------------------------------

def test_absolute_schedule_reports_clock_time(ctx):
    assert _only(ctx, "schKitchen Lights 6am")["fires"] == {
        "when": "absolute", "at": "06:00:00",
    }


def test_sunset_schedule_reports_offset_and_randomisation(ctx):
    assert _only(ctx, "Evening indoor lights")["fires"] == {
        "when": "sunset", "offset_seconds": -1800, "randomize_seconds": 900,
    }


def test_countdown_schedule_reports_its_interval(ctx):
    assert _only(ctx, "Heating on kitchen")["fires"] == {
        "when": "countdown", "every_seconds": 60,
    }


def test_weekday_restricted_schedule_reports_days(ctx):
    fires = _only(ctx, "Workday hours on")["fires"]
    assert fires["days"] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert fires["when"] == "absolute"


def test_daily_schedule_omits_the_every_n_days_noise(ctx):
    # repeat_interval_days == 1 is "every day" — the default, and not
    # worth digest tokens.
    assert "every_n_days" not in _only(ctx, "schKitchen Lights 6am")["fires"]


def test_triggers_carry_no_fires_block(ctx):
    assert "fires" not in _only(ctx, "Kitchen Lights On - Daytime")
