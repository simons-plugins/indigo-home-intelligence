"""Tests for DigestRunner._fleet_health and ._energy_context — the two
new blocks added for PR #13.

These touch ``indigo.devices`` (for battery/offline scans and name
lookups on top consumers) which the Tier-A strict stub blocks. We
replace the module-level ``indigo.devices`` temporarily per-test so
the rest of the strict stub protection remains in force for every
other test file."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import digest


class _FakeDevicesIterable(list):
    """Stand-in for ``indigo.devices`` — iterable list of SimpleNamespace
    devices. Works with ``for dev in indigo.devices`` plus the
    ``{dev.id: dev.name for dev in indigo.devices}`` comprehension the
    energy context uses for name lookups."""


@pytest.fixture
def patched_devices(monkeypatch):
    """Factory that replaces ``digest.indigo.devices`` for one test.

    ``raising=False`` bypasses the strict-stub's AttributeError on
    attribute check — the Tier-A stub deliberately blocks normal
    attribute access to ``indigo.devices`` so accidental prod-path
    reads fail loudly; this fixture is the opt-in exception."""
    def _set(devices):
        monkeypatch.setattr(
            digest.indigo, "devices",
            _FakeDevicesIterable(devices),
            raising=False,
        )
    return _set


def _make_runner(
    history_db=None,
    whole_house_energy_device_id=None,
    battery_low_threshold=20,
    offline_hours_threshold=24,
):
    """Stubbed DigestRunner for testing the health/energy helpers in
    isolation. Collaborators we don't touch are MagicMocks."""
    return digest.DigestRunner(
        history_db=history_db,
        rule_store=MagicMock(),
        observation_store=MagicMock(),
        delivery=MagicMock(),
        api_key="",
        model="claude-sonnet-4-6",
        email_to="test@example.com",
        logger=MagicMock(),
        whole_house_energy_device_id=whole_house_energy_device_id,
        battery_low_threshold=battery_low_threshold,
        offline_hours_threshold=offline_hours_threshold,
    )


class TestFleetHealth:
    def test_empty_device_list_returns_empty_counts(self, patched_devices):
        patched_devices([])
        runner = _make_runner()
        health = runner._fleet_health()
        assert health == {
            "low_batteries": [],
            "low_batteries_total": 0,
            "offline_devices": [],
            "offline_devices_total": 0,
        }

    def test_low_battery_detected_at_or_below_threshold(self, patched_devices):
        now = datetime.now()
        patched_devices([
            SimpleNamespace(id=1, name="At threshold", enabled=True,
                            batteryLevel=20, errorState="", lastSuccessfulComm=now),
            SimpleNamespace(id=2, name="Below", enabled=True,
                            batteryLevel=8, errorState="", lastSuccessfulComm=now),
            SimpleNamespace(id=3, name="Healthy", enabled=True,
                            batteryLevel=80, errorState="", lastSuccessfulComm=now),
            SimpleNamespace(id=4, name="Mains-powered", enabled=True,
                            batteryLevel=None, errorState="", lastSuccessfulComm=now),
        ])
        runner = _make_runner(battery_low_threshold=20)
        health = runner._fleet_health()
        names = [d["name"] for d in health["low_batteries"]]
        assert "At threshold" in names
        assert "Below" in names
        assert "Healthy" not in names
        assert "Mains-powered" not in names
        # Sorted ascending by battery_pct (worst first).
        pct_order = [d["battery_pct"] for d in health["low_batteries"]]
        assert pct_order == sorted(pct_order)

    def test_offline_by_error_state(self, patched_devices):
        now = datetime.now()
        patched_devices([
            SimpleNamespace(id=10, name="Has error", enabled=True,
                            batteryLevel=None, errorState="timeout",
                            lastSuccessfulComm=now),
            SimpleNamespace(id=11, name="Healthy", enabled=True,
                            batteryLevel=None, errorState="",
                            lastSuccessfulComm=now),
        ])
        runner = _make_runner()
        health = runner._fleet_health()
        offline = [d["name"] for d in health["offline_devices"]]
        assert offline == ["Has error"]
        assert health["offline_devices"][0]["error_state"] == "timeout"

    def test_offline_by_stale_last_comm(self, patched_devices):
        now = datetime.now()
        patched_devices([
            SimpleNamespace(id=20, name="Stale", enabled=True,
                            batteryLevel=None, errorState="",
                            lastSuccessfulComm=now - timedelta(hours=48)),
            SimpleNamespace(id=21, name="Recent", enabled=True,
                            batteryLevel=None, errorState="",
                            lastSuccessfulComm=now - timedelta(minutes=30)),
        ])
        runner = _make_runner(offline_hours_threshold=24)
        health = runner._fleet_health()
        names = [d["name"] for d in health["offline_devices"]]
        assert "Stale" in names
        assert "Recent" not in names

    def test_disabled_devices_skipped(self, patched_devices):
        now = datetime.now()
        patched_devices([
            SimpleNamespace(id=30, name="Disabled low", enabled=False,
                            batteryLevel=5, errorState="",
                            lastSuccessfulComm=now),
        ])
        runner = _make_runner()
        health = runner._fleet_health()
        assert health["low_batteries"] == []

    def test_missing_last_comm_and_no_error_is_not_offline(self, patched_devices):
        # Device with no evidence either way — don't invent an offline
        # claim.
        patched_devices([
            SimpleNamespace(id=40, name="No comm field", enabled=True,
                            batteryLevel=None, errorState="",
                            lastSuccessfulComm=None),
        ])
        runner = _make_runner()
        health = runner._fleet_health()
        assert health["offline_devices"] == []

    def test_caps_lists_at_30_but_reports_total(self, patched_devices):
        now = datetime.now()
        # 35 low-battery devices, 2 offline — lists cap at 30, totals
        # still accurate.
        devs = []
        for i in range(35):
            devs.append(SimpleNamespace(
                id=100 + i, name=f"Batt{i}", enabled=True,
                batteryLevel=5, errorState="",
                lastSuccessfulComm=now,
            ))
        patched_devices(devs)
        runner = _make_runner()
        health = runner._fleet_health()
        assert len(health["low_batteries"]) == 30
        assert health["low_batteries_total"] == 35


class TestEnergyContext:
    def test_no_history_db_returns_empty(self, patched_devices):
        patched_devices([])
        runner = _make_runner(history_db=None)
        assert runner._energy_context() == {}

    def test_empty_discovery_returns_empty(self, patched_devices):
        patched_devices([])
        history_db = MagicMock()
        history_db.discover_energy_tables.return_value = []
        runner = _make_runner(history_db=history_db)
        assert runner._energy_context() == {}

    def test_whole_house_and_top_consumers_assembled(self, patched_devices):
        # Three energy devices: the whole-house meter (high totals
        # because it aggregates everything) and two individual devices.
        patched_devices([
            SimpleNamespace(id=452894065, name="Power Meter"),
            SimpleNamespace(id=100, name="Tumble Dryer"),
            SimpleNamespace(id=200, name="Kettle"),
        ])
        history_db = MagicMock()
        history_db.discover_energy_tables.return_value = [452894065, 100, 200]
        history_db.energy_rollup_14d.return_value = {
            452894065: {
                "this_week_kwh": 127.3,
                "last_week_kwh": 142.1,
                "delta_kwh": -14.8,
                "delta_pct": -10.4,
            },
            100: {
                "this_week_kwh": 18.4,
                "last_week_kwh": 12.7,
                "delta_kwh": 5.7,
                "delta_pct": 44.9,
            },
            200: {
                "this_week_kwh": 4.2,
                "last_week_kwh": 4.3,
                "delta_kwh": -0.1,
                "delta_pct": -2.3,
            },
        }
        runner = _make_runner(
            history_db=history_db,
            whole_house_energy_device_id=452894065,
        )
        ctx = runner._energy_context()
        assert ctx["whole_house"]["device_id"] == 452894065
        assert ctx["whole_house"]["this_week_kwh"] == 127.3
        # Top consumers excludes the whole-house meter, sorted desc by
        # this_week_kwh.
        names = [c["name"] for c in ctx["top_consumers"]]
        assert "Power Meter" not in names
        assert names == ["Tumble Dryer", "Kettle"]
        assert ctx["top_consumers"][0]["delta_pct"] == 44.9

    def test_whole_house_missing_from_rollup_omits_whole_house_block(self, patched_devices):
        # Configured device has no 14-day history (not enough data) →
        # rollup drops it. Top-consumers should still populate.
        patched_devices([
            SimpleNamespace(id=999, name="Brand New Meter"),
            SimpleNamespace(id=100, name="Tumble Dryer"),
        ])
        history_db = MagicMock()
        history_db.discover_energy_tables.return_value = [999, 100]
        history_db.energy_rollup_14d.return_value = {
            # 999 is missing → no whole_house emitted.
            100: {
                "this_week_kwh": 18.4, "last_week_kwh": 12.7,
                "delta_kwh": 5.7, "delta_pct": 44.9,
            },
        }
        runner = _make_runner(
            history_db=history_db,
            whole_house_energy_device_id=999,
        )
        ctx = runner._energy_context()
        assert "whole_house" not in ctx
        assert ctx["top_consumers"][0]["name"] == "Tumble Dryer"

    def test_no_whole_house_device_configured_still_emits_top_consumers(self, patched_devices):
        patched_devices([
            SimpleNamespace(id=100, name="Tumble Dryer"),
        ])
        history_db = MagicMock()
        history_db.discover_energy_tables.return_value = [100]
        history_db.energy_rollup_14d.return_value = {
            100: {
                "this_week_kwh": 18.4, "last_week_kwh": 12.7,
                "delta_kwh": 5.7, "delta_pct": 44.9,
            },
        }
        runner = _make_runner(
            history_db=history_db,
            whole_house_energy_device_id=None,
        )
        ctx = runner._energy_context()
        assert "whole_house" not in ctx
        assert len(ctx["top_consumers"]) == 1

    def test_whole_house_id_not_in_discovery_omits_whole_house(self, patched_devices):
        """If the user configures a whole-house device ID that isn't
        in SQL Logger discovery (never set up for energy logging, or
        table dropped), the whole_house block is omitted — we don't
        forcibly add the ID to the bulk query, because that would make
        the UNION ALL fail on the missing table."""
        patched_devices([SimpleNamespace(id=100, name="Tumble Dryer")])
        history_db = MagicMock()
        # Discovery returns ONLY device 100; the configured whole-house
        # ID 999 is NOT in the list.
        history_db.discover_energy_tables.return_value = [100]
        history_db.energy_rollup_14d.return_value = {
            100: {
                "this_week_kwh": 18.4, "last_week_kwh": 12.7,
                "delta_kwh": 5.7, "delta_pct": 44.9,
            },
        }
        runner = _make_runner(
            history_db=history_db,
            whole_house_energy_device_id=999,
        )
        ctx = runner._energy_context()
        assert "whole_house" not in ctx
        assert ctx["top_consumers"][0]["name"] == "Tumble Dryer"
        # Verify rollup was called with ONLY the discovered IDs — not
        # with the configured-but-undiscovered 999 appended defensively.
        history_db.energy_rollup_14d.assert_called_once_with([100])

    def test_top_consumers_capped_at_10(self, patched_devices):
        # 15 devices — result list should be 10.
        devs = [SimpleNamespace(id=i, name=f"Dev{i}") for i in range(15)]
        patched_devices(devs)
        history_db = MagicMock()
        history_db.discover_energy_tables.return_value = list(range(15))
        history_db.energy_rollup_14d.return_value = {
            i: {
                "this_week_kwh": float(15 - i),
                "last_week_kwh": float(10),
                "delta_kwh": float(5 - i),
                "delta_pct": 0.0,
            }
            for i in range(15)
        }
        runner = _make_runner(history_db=history_db)
        ctx = runner._energy_context()
        assert len(ctx["top_consumers"]) == 10
        # Sorted desc — device 0 (15 kWh) should be first.
        assert ctx["top_consumers"][0]["id"] == 0
