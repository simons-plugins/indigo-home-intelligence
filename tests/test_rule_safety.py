"""Tests for the rule-safety helpers in plugin.py:
- _is_safe_rule_target: server-side allowlist that rejects rules
  targeting thermostats / security systems / sensors before write
- _render_rule_human: templated confirmation-email rule description"""

from types import SimpleNamespace

import pytest

import plugin


class _DevicesDict:
    """Stand-in for indigo.devices usable as `id in indigo.devices`
    and `indigo.devices[id]`. Backed by a real dict."""

    def __init__(self, d):
        self._d = d

    def __contains__(self, key):
        return key in self._d

    def __getitem__(self, key):
        return self._d[key]


@pytest.fixture
def patched_devices(monkeypatch):
    def _set(devices):
        monkeypatch.setattr(
            plugin.indigo, "devices", _DevicesDict(devices), raising=False
        )
    return _set


class TestIsSafeRuleTarget:
    def test_dimmer_is_safe(self, patched_devices):
        dev = SimpleNamespace(id=1, name="Study Lamp", pluginId="com.shelly",
                              brightness=80)
        patched_devices({1: dev})
        assert plugin._is_safe_rule_target(1) is True

    def test_relay_is_safe(self, patched_devices):
        dev = SimpleNamespace(id=2, name="Kettle", pluginId="com.shelly",
                              onState=True)
        patched_devices({2: dev})
        assert plugin._is_safe_rule_target(2) is True

    def test_thermostat_rejected(self, patched_devices):
        # Thermostats have setpoints that are high-stakes — explicit reject
        # even though they may have onState via a fan. temperatureInputs
        # is the defining marker.
        dev = SimpleNamespace(id=3, name="Heatmiser",
                              pluginId="com.simons-plugins.heatmiser",
                              temperatureInputs=[20.0],
                              onState=False)
        patched_devices({3: dev})
        assert plugin._is_safe_rule_target(3) is False

    def test_sensor_without_power_rejected(self, patched_devices):
        # A pure sensor (sensorValue only) has no switchable surface.
        dev = SimpleNamespace(id=4, name="Motion Sensor",
                              pluginId="com.zwave", sensorValue=1.0)
        patched_devices({4: dev})
        assert plugin._is_safe_rule_target(4) is False

    def test_alarm_plugin_rejected(self, patched_devices):
        dev = SimpleNamespace(
            id=5, name="Alarm Panel",
            pluginId="com.example.texecom-alarm",
            onState=True,  # alarm panels have onState but shouldn't be toggled
        )
        patched_devices({5: dev})
        assert plugin._is_safe_rule_target(5) is False

    def test_security_camera_plugin_rejected(self, patched_devices):
        dev = SimpleNamespace(
            id=6, name="Front Camera",
            pluginId="com.cynical.securityspy-motion",
            onState=True,
        )
        patched_devices({6: dev})
        assert plugin._is_safe_rule_target(6) is False

    def test_lock_plugin_rejected(self, patched_devices):
        dev = SimpleNamespace(
            id=7, name="Front Door Lock",
            pluginId="com.example.yale-lock-plugin",
            onState=False,
        )
        patched_devices({7: dev})
        assert plugin._is_safe_rule_target(7) is False

    def test_unknown_device_rejected(self, patched_devices):
        patched_devices({})
        assert plugin._is_safe_rule_target(12345) is False

    def test_non_int_id_rejected(self, patched_devices):
        patched_devices({})
        assert plugin._is_safe_rule_target("not-an-id") is False
        assert plugin._is_safe_rule_target(None) is False


class TestRenderRuleHuman:
    """The templated rule description used in the confirmation email.
    Must resolve device IDs to names and render when/then clauses in
    plain English."""

    @pytest.fixture(autouse=True)
    def _patch_devices(self, monkeypatch):
        dev = SimpleNamespace(id=100, name="Coffee Machine",
                              pluginId="com.shelly", onState=True)
        monkeypatch.setattr(
            plugin.indigo, "devices",
            _DevicesDict({100: dev}),
            raising=False,
        )

    def test_simple_off_rule_rendered(self):
        rule = {
            "when": {"device_id": 100, "state": "onState", "equals": True},
            "then": {"device_id": 100, "op": "off"},
        }
        out = plugin._render_rule_human(rule)
        assert "Coffee Machine" in out
        assert "onState" in out
        assert "True" in out
        assert "turn off" in out
        assert "100" in out  # device id shown in backticks

    def test_time_window_rendered(self):
        rule = {
            "when": {"device_id": 100, "state": "onState", "equals": True,
                     "after_local_time": "21:00", "before_local_time": "23:00"},
            "then": {"device_id": 100, "op": "off"},
        }
        out = plugin._render_rule_human(rule)
        assert "between" in out
        assert "21:00" in out
        assert "23:00" in out

    def test_single_after_window(self):
        rule = {
            "when": {"device_id": 100, "state": "onState", "equals": True,
                     "after_local_time": "21:00"},
            "then": {"device_id": 100, "op": "off"},
        }
        out = plugin._render_rule_human(rule)
        assert "after" in out
        assert "21:00" in out

    def test_for_minutes_rendered(self):
        rule = {
            "when": {"device_id": 100, "state": "onState", "equals": True,
                     "for_minutes": 30},
            "then": {"device_id": 100, "op": "off"},
        }
        out = plugin._render_rule_human(rule)
        assert "30 minutes" in out

    def test_set_brightness_with_value(self):
        rule = {
            "when": {"device_id": 100, "state": "onState", "equals": True},
            "then": {"device_id": 100, "op": "set_brightness", "value": 40},
        }
        out = plugin._render_rule_human(rule)
        assert "set brightness to 40" in out

    def test_missing_device_falls_back_to_id(self):
        rule = {
            "when": {"device_id": 999, "state": "onState", "equals": True},
            "then": {"device_id": 999, "op": "off"},
        }
        out = plugin._render_rule_human(rule)
        # Device 999 is not in our fixture — falls back to "device 999"
        assert "device 999" in out
