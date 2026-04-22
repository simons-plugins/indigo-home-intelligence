"""Tests for RuleEvaluator's auto-disable behaviour — the counter
pattern that turns silent rule-death and retry-loop spam into a
visible auto-disable surfaced in the next digest.

Exercises the three failure modes that increment the counter:
1. Target device missing (deleted or renamed)
2. State key missing on device (plugin upgrade renamed state)
3. Action (turnOn/turnOff etc.) raises

Plus the three cases that should NOT accumulate toward disable:
- Condition value doesn't match (legitimate non-match, off-state)
- Outside the time window (legitimate, rule just isn't active now)
- Disabled rules (skipped entirely)"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import rule_evaluator


class _FakeRuleStore:
    def __init__(self, rules):
        self.rules = rules
        self.auto_disable_calls = []
        self.fire_calls = []

    def list_rules(self):
        return self.rules

    def auto_disable(self, rule_id, reason):
        self.auto_disable_calls.append((rule_id, reason))
        for r in self.rules:
            if r.get("id") == rule_id:
                r["enabled"] = False
                r["auto_disabled"] = True
                r["auto_disabled_reason"] = reason
                return True
        return False

    def record_fire(self, rule_id):
        self.fire_calls.append(rule_id)


def _make_evaluator(rules, monkeypatch, devices=None):
    """Build a RuleEvaluator with a fake rule_store and a patched
    indigo.devices that behaves like a dict keyed by device id."""
    store = _FakeRuleStore(rules)
    # `devices or {}` would silently replace a passed-in empty dict
    # with a fresh one, breaking tests that expect to mutate the dict
    # after construction. Use an explicit None check.
    if devices is None:
        devices = {}

    class _DevicesDict:
        def __init__(self, d):
            self._d = d

        def __contains__(self, key):
            return key in self._d

        def __getitem__(self, key):
            return self._d[key]

    monkeypatch.setattr(
        rule_evaluator.indigo, "devices",
        _DevicesDict(devices),
        raising=False,
    )
    # Also patch dimmer/relay so _fire can call turnOff/turnOn without
    # raising AttributeError on the strict stub.
    monkeypatch.setattr(
        rule_evaluator.indigo, "dimmer",
        SimpleNamespace(turnOn=lambda *a, **kw: None,
                        turnOff=lambda *a, **kw: None,
                        toggle=lambda *a, **kw: None,
                        setBrightness=lambda *a, **kw: None),
        raising=False,
    )
    monkeypatch.setattr(
        rule_evaluator.indigo, "relay",
        SimpleNamespace(turnOn=lambda *a, **kw: None,
                        turnOff=lambda *a, **kw: None,
                        toggle=lambda *a, **kw: None),
        raising=False,
    )
    ev = rule_evaluator.RuleEvaluator(store, MagicMock())
    return ev, store


def _rule(device_id=100, state="onState", equals=True,
          action_device_id=100, op="off", enabled=True):
    return {
        "id": "r1",
        "enabled": enabled,
        "description": "test rule",
        "when": {"device_id": device_id, "state": state, "equals": equals},
        "then": {"device_id": action_device_id, "op": op},
    }


def _device(id, name="Dev", states=None, onState=True, brightness=None):
    d = SimpleNamespace(id=id, name=name, states=states or {"onState": onState})
    if brightness is not None:
        d.brightness = brightness
    else:
        # Default: has onState (relay)
        d.onState = onState
    return d


class TestAutoDisableOnTargetMissing:
    def test_single_miss_doesnt_disable(self, monkeypatch):
        # Rule points at a device that doesn't exist → counter
        # increments but stays under the threshold.
        rule = _rule(device_id=999)
        ev, store = _make_evaluator([rule], monkeypatch, devices={})
        ev.tick()
        assert store.auto_disable_calls == []
        assert ev._failures[rule["id"]] == 1

    def test_n_consecutive_misses_triggers_autodisable(self, monkeypatch):
        rule = _rule(device_id=999)
        ev, store = _make_evaluator([rule], monkeypatch, devices={})
        for _ in range(rule_evaluator.AUTO_DISABLE_AFTER_FAILURES):
            ev.tick()
        assert len(store.auto_disable_calls) == 1
        disabled_id, reason = store.auto_disable_calls[0]
        assert disabled_id == "r1"
        assert reason == "target_device_missing"

    def test_recovery_resets_counter(self, monkeypatch):
        # A few failures, then the target device reappears and the
        # condition clears → counter resets, no auto-disable.
        rule = _rule(device_id=100)
        devices = {}
        ev, store = _make_evaluator([rule], monkeypatch, devices=devices)
        # First 3 ticks: no device, counter grows.
        for _ in range(3):
            ev.tick()
        assert ev._failures[rule["id"]] == 3
        # Device appears, condition doesn't match (onState=False) —
        # legitimate non-match, counter resets.
        devices[100] = _device(id=100, onState=False)
        ev.tick()
        assert rule["id"] not in ev._failures or ev._failures[rule["id"]] == 0
        assert store.auto_disable_calls == []


class TestAutoDisableOnStateKeyMissing:
    def test_state_key_renamed_counts_as_failure(self, monkeypatch):
        # Device exists but the state key the rule asks for is absent
        # (states dict doesn't contain it AND hasattr returns False).
        rule = _rule(state="renamedState")
        dev = SimpleNamespace(id=100, name="X", states={"onState": True})
        ev, store = _make_evaluator(
            [rule], monkeypatch, devices={100: dev}
        )
        for _ in range(rule_evaluator.AUTO_DISABLE_AFTER_FAILURES):
            ev.tick()
        assert len(store.auto_disable_calls) == 1
        assert store.auto_disable_calls[0][1] == "state_key_missing"


class TestAutoDisableOnActionFailure:
    def test_turn_off_raises_counts_as_failure(self, monkeypatch):
        rule = _rule(device_id=100, action_device_id=100, op="off")
        devices = {100: _device(id=100, onState=True)}
        ev, store = _make_evaluator([rule], monkeypatch, devices=devices)
        # Replace relay.turnOff with a raiser.
        raising = MagicMock(side_effect=RuntimeError("device unreachable"))
        monkeypatch.setattr(
            rule_evaluator.indigo, "relay",
            SimpleNamespace(turnOn=lambda *a, **kw: None,
                            turnOff=raising,
                            toggle=lambda *a, **kw: None),
            raising=False,
        )
        for _ in range(rule_evaluator.AUTO_DISABLE_AFTER_FAILURES):
            ev.tick()
        assert len(store.auto_disable_calls) == 1
        assert store.auto_disable_calls[0][1] == "action_failed"

    def test_successful_fire_clears_counter(self, monkeypatch):
        rule = _rule(device_id=100, action_device_id=100, op="off")
        devices = {100: _device(id=100, onState=True)}
        ev, store = _make_evaluator([rule], monkeypatch, devices=devices)
        ev.tick()
        # Counter should be absent (cleared) after successful fire.
        assert rule["id"] not in ev._failures
        assert store.auto_disable_calls == []
        assert store.fire_calls == ["r1"]


class TestNonFailurePathsDoNotAccumulate:
    def test_disabled_rule_skipped(self, monkeypatch):
        rule = _rule(enabled=False)
        ev, store = _make_evaluator([rule], monkeypatch, devices={})
        for _ in range(rule_evaluator.AUTO_DISABLE_AFTER_FAILURES):
            ev.tick()
        # Disabled rule never evaluated; no counter, no auto-disable.
        assert rule["id"] not in ev._failures
        assert store.auto_disable_calls == []

    def test_condition_not_met_clears_counter(self, monkeypatch):
        # State exists but value doesn't match — condition just isn't
        # true right now. Legitimate, not a failure.
        rule = _rule(equals=True)
        devices = {100: _device(id=100, onState=False)}
        ev, store = _make_evaluator([rule], monkeypatch, devices=devices)
        # Precondition — ensure no prior failures.
        ev._failures[rule["id"]] = 0
        for _ in range(rule_evaluator.AUTO_DISABLE_AFTER_FAILURES + 5):
            ev.tick()
        assert store.auto_disable_calls == []
