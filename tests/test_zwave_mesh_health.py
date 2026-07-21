"""Tests for HouseContextAccess.zwave_mesh_health and MeshSnapshotStore.

The collector touches ``indigo.devices`` and ``indigo.kProtocol``
which the Tier-A strict stub blocks; both are patched per-test via
the same opt-in monkeypatch pattern as test_digest_health_energy.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import data_access
import mesh_store as mesh_store_module
from mesh_store import MeshSnapshotStore


_ZWAVE = object()  # sentinel standing in for indigo.kProtocol.ZWave
_PLUGIN = object()

_ZW_KEY = "com.perceptiveautomation.indigoplugin.zwave"


class _FakeDevicesIterable(list):
    """Iterable stand-in for ``indigo.devices``."""


@pytest.fixture
def patched_zwave(monkeypatch):
    """Patch ``data_access.indigo`` devices + kProtocol for one test."""
    def _set(devices):
        monkeypatch.setattr(
            data_access.indigo, "devices",
            _FakeDevicesIterable(devices),
            raising=False,
        )
        monkeypatch.setattr(
            data_access.indigo, "kProtocol",
            SimpleNamespace(ZWave=_ZWAVE, Plugin=_PLUGIN),
            raising=False,
        )
    return _set


def _make_context(zwave_weak_neighbour_threshold=2):
    return data_access.HouseContextAccess(
        history_db=None,
        logger=MagicMock(),
        zwave_weak_neighbour_threshold=zwave_weak_neighbour_threshold,
    )


def _zw_device(
    id_,
    name,
    address,
    neighbours,
    features="routing, beaming",
    enabled=True,
    protocol=_ZWAVE,
    battery_level=None,
):
    props = {"zwNodeNeighbors": neighbours, "zwFeatureListStr": features}
    return SimpleNamespace(
        id=id_,
        name=name,
        address=address,
        enabled=enabled,
        protocol=protocol,
        batteryLevel=battery_level,
        globalProps={_ZW_KEY: props},
    )


# ----- collector ---------------------------------------------------------


def test_mesh_flags_weak_routers_only(patched_zwave):
    patched_zwave([
        _zw_device(1, "Weak Relay", "10", [3]),
        _zw_device(2, "Healthy Relay", "11", [1, 2, 3, 4, 5]),
        _zw_device(3, "Battery Sensor", "12", [4], features="beaming"),
    ])
    result = _make_context().zwave_mesh_health()
    assert result["nodes_total"] == 3
    assert result["weak_nodes_total"] == 1
    assert result["weak_nodes"][0]["name"] == "Weak Relay"
    assert result["weak_nodes"][0]["neighbour_count"] == 1


def test_mesh_routing_slave_battery_nodes_not_flagged(patched_zwave):
    # Real jarvis data: battery sensors report "routing, battery,
    # beaming, waking" (routing *slave*) and legitimately show 0
    # neighbours while asleep — they must not be flagged as weak.
    patched_zwave([
        _zw_device(1, "Leak Sensor", "31", [],
                   features="routing, battery, beaming, waking"),
        _zw_device(2, "No-feature Battery", "32", [],
                   features="routing", battery_level=77),
    ])
    result = _make_context().zwave_mesh_health()
    assert result["weak_nodes_total"] == 0
    assert result["nodes_total"] == 2


def test_mesh_skips_non_zwave_disabled_and_unsynced(patched_zwave):
    unsynced = _zw_device(4, "Never Synced", "20", None)
    unsynced.globalProps = {_ZW_KEY: {"zwFeatureListStr": "routing"}}
    patched_zwave([
        _zw_device(1, "Plugin Dev", "10", [1], protocol=_PLUGIN),
        _zw_device(2, "Disabled", "11", [1], enabled=False),
        _zw_device(3, "Real", "12", [1, 2, 3]),
        unsynced,
    ])
    result = _make_context().zwave_mesh_health()
    assert result["nodes_total"] == 1
    assert result["snapshot"] == {"12": 3}


def test_mesh_dedupes_multi_endpoint_nodes(patched_zwave):
    patched_zwave([
        _zw_device(1, "Clamp 1", "29", [1, 2]),
        _zw_device(2, "Clamp 2", "29", [1, 2]),
    ])
    result = _make_context().zwave_mesh_health()
    assert result["nodes_total"] == 1
    assert result["snapshot"] == {"29": 2}


def test_mesh_snapshot_and_no_diff_without_previous(patched_zwave):
    patched_zwave([_zw_device(1, "Relay", "10", [1, 2, 3])])
    result = _make_context().zwave_mesh_health()
    assert result["snapshot"] == {"10": 3}
    assert "neighbour_changes" not in result


def test_mesh_diffs_against_previous_snapshot(patched_zwave):
    patched_zwave([
        _zw_device(1, "Dropped", "10", [1]),
        _zw_device(2, "Grew", "11", [1, 2, 3, 4]),
        _zw_device(3, "Same", "12", [1, 2]),
        _zw_device(4, "Brand New", "13", [1, 2, 3]),
    ])
    previous = {"10": 12, "11": 2, "12": 2, "99": 5}
    result = _make_context().zwave_mesh_health(previous)
    # Biggest drop first.
    assert result["neighbour_changes"] == [
        {"address": "10", "name": "Dropped", "was": 12, "now": 1},
        {"address": "11", "name": "Grew", "was": 2, "now": 4},
    ]
    assert result["neighbour_changes_total"] == 2
    assert result["new_nodes"] == ["13"]
    assert result["vanished_nodes"] == ["99"]


def test_mesh_previous_snapshot_ignores_corrupt_values(patched_zwave):
    patched_zwave([_zw_device(1, "Relay", "10", [1])])
    previous = {"10": "twelve", "11": True}
    result = _make_context().zwave_mesh_health(previous)
    assert result["neighbour_changes"] == []
    assert result["vanished_nodes"] == []


def test_mesh_threshold_configurable(patched_zwave):
    patched_zwave([_zw_device(1, "Relay", "10", [1, 2, 3, 4])])
    result = _make_context(zwave_weak_neighbour_threshold=4).zwave_mesh_health()
    assert result["weak_nodes_total"] == 1
    assert result["weak_threshold"] == 4


def test_mesh_isolates_per_device_failures(patched_zwave):
    class _Bomb:
        id = 99
        name = "Bomb"
        enabled = True

        @property
        def protocol(self):
            raise RuntimeError("boom")

    patched_zwave([_Bomb(), _zw_device(1, "Relay", "10", [1, 2, 3])])
    ctx = _make_context()
    result = ctx.zwave_mesh_health()
    assert result["nodes_total"] == 1
    assert ctx.logger.warning.called


def test_zwave_interface_props_matches_by_substring():
    dev = SimpleNamespace(
        globalProps={
            "com.other.plugin": {"ignored": 1},
            _ZW_KEY: {"zwNodeNeighbors": [1]},
        }
    )
    props = data_access.HouseContextAccess._zwave_interface_props(dev)
    assert props == {"zwNodeNeighbors": [1]}


def test_zwave_interface_props_missing_globalprops():
    dev = SimpleNamespace(globalProps=None)
    assert data_access.HouseContextAccess._zwave_interface_props(dev) == {}


# ----- MeshSnapshotStore -------------------------------------------------


class _FakeVariables(dict):
    def __getitem__(self, name):
        return SimpleNamespace(value=dict.__getitem__(self, name))


@pytest.fixture
def patched_variables(monkeypatch):
    """Patch ``mesh_store.indigo`` variables + variable command space."""
    variables = _FakeVariables()
    writes = {}

    def _create(name, value=""):
        variables[name] = value

    def _update(name, value=""):
        variables[name] = value
        writes[name] = value

    monkeypatch.setattr(
        mesh_store_module.indigo, "variables", variables, raising=False
    )
    monkeypatch.setattr(
        mesh_store_module.indigo, "variable",
        SimpleNamespace(create=_create, updateValue=_update),
        raising=False,
    )
    return variables, writes


def test_store_read_missing_creates_and_returns_empty(patched_variables):
    variables, _ = patched_variables
    store = MeshSnapshotStore("hi_mesh", MagicMock())
    assert store.read() == {}
    assert variables["hi_mesh"].value == "{}"


def test_store_roundtrip(patched_variables):
    variables, writes = patched_variables
    store = MeshSnapshotStore("hi_mesh", MagicMock())
    store.write({"10": 3, "11": 5})
    assert json.loads(writes["hi_mesh"]) == {"10": 3, "11": 5}
    assert store.read() == {"10": 3, "11": 5}


def test_store_read_corrupt_resets_empty(patched_variables):
    variables, _ = patched_variables
    variables["hi_mesh"] = "not json"
    logger = MagicMock()
    store = MeshSnapshotStore("hi_mesh", logger)
    assert store.read() == {}
    assert logger.warning.called


def test_store_read_non_object_resets_empty(patched_variables):
    variables, _ = patched_variables
    variables["hi_mesh"] = "[1,2]"
    store = MeshSnapshotStore("hi_mesh", MagicMock())
    assert store.read() == {}
