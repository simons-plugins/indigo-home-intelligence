"""Tests for digest.DigestRunner's schedule/trigger/action_group snapshot
helpers (issue #2). Tests the pure logic: jsonable coercion, key
filtering, and safe dict() wrapping. The snapshot methods themselves
are exercised indirectly by passing fake Indigo-shaped objects."""

from types import SimpleNamespace

from digest import DigestRunner


class TestJsonable:
    def test_primitives_unchanged(self):
        assert DigestRunner._jsonable(None) is None
        assert DigestRunner._jsonable(True) is True
        assert DigestRunner._jsonable(42) == 42
        assert DigestRunner._jsonable(3.14) == 3.14
        assert DigestRunner._jsonable("hello") == "hello"

    def test_list_coerces_elements(self):
        assert DigestRunner._jsonable([1, "a", None]) == [1, "a", None]

    def test_tuple_becomes_list(self):
        assert DigestRunner._jsonable((1, 2, 3)) == [1, 2, 3]

    def test_nested_dict(self):
        result = DigestRunner._jsonable({"a": 1, "b": {"c": 2}})
        assert result == {"a": 1, "b": {"c": 2}}

    def test_dict_keys_stringified(self):
        # JSON can't represent non-string keys; helper must cast.
        result = DigestRunner._jsonable({1: "one", 2: "two"})
        assert result == {"1": "one", "2": "two"}

    def test_exotic_type_falls_back_to_str(self):
        class Exotic:
            def __str__(self):
                return "exotic-repr"
        assert DigestRunner._jsonable(Exotic()) == "exotic-repr"

    def test_datetime_becomes_string(self):
        from datetime import datetime
        result = DigestRunner._jsonable(datetime(2026, 4, 21, 10, 30))
        assert isinstance(result, str)
        assert "2026-04-21" in result


class TestFilterKeys:
    def test_drop_list_removes_keys(self):
        d = {"keep": 1, "xmlElement": "<foo/>", "also_keep": 2}
        result = DigestRunner._filter_keys(d, frozenset({"xmlElement"}))
        assert result == {"keep": 1, "also_keep": 2}

    def test_none_values_removed(self):
        d = {"name": "x", "missing": None, "val": 0}
        result = DigestRunner._filter_keys(d, frozenset())
        # 0 is a legitimate value, not "empty" — must be preserved.
        assert result == {"name": "x", "val": 0}

    def test_empty_collections_removed(self):
        d = {"name": "x", "empty_list": [], "empty_dict": {}, "empty_str": ""}
        result = DigestRunner._filter_keys(d, frozenset())
        assert result == {"name": "x"}

    def test_zero_and_false_preserved(self):
        # Edge case: 0 and False should NOT be treated as empty.
        d = {"count": 0, "flag": False, "enabled": True}
        result = DigestRunner._filter_keys(d, frozenset())
        assert result == {"count": 0, "flag": False, "enabled": True}


class TestSafeIndigoDict:
    def test_dict_coercible_object_round_trips(self):
        class DictLike:
            def keys(self):
                return ["a", "b"]
            def __getitem__(self, key):
                return {"a": 1, "b": "two"}[key]
        result = DigestRunner._safe_indigo_dict(DictLike())
        assert result == {"a": 1, "b": "two"}

    def test_dict_failure_returns_empty(self):
        class Unfriendly:
            def keys(self):
                raise RuntimeError("cannot iterate")
        result = DigestRunner._safe_indigo_dict(Unfriendly())
        assert result == {}

    def test_dict_coercion_applies_jsonable(self):
        class WithExoticValue:
            def keys(self):
                return ["special"]
            def __getitem__(self, key):
                class Exotic:
                    def __str__(self): return "coerced"
                return Exotic()
        result = DigestRunner._safe_indigo_dict(WithExoticValue())
        assert result == {"special": "coerced"}


class TestScheduleSnapshot:
    def _fake_schedule(self, **overrides):
        # Indigo's Schedule has .id/.name/.enabled + type(obj).__name__.
        # dict() coercion yields configuration fields.
        defaults = {
            "id": 100,
            "name": "Coffee Off 4pm",
            "enabled": True,
            "description": "Turn off coffee machine at 4pm weekdays",
            "folderId": 0,
            "nextExecution": None,
            "scheduleTime": "16:00:00",
            # Keys we should drop:
            "xmlElement": "<foo/>",
            "remoteDisplay": True,
            "class": "Schedule",
        }
        defaults.update(overrides)
        class FakeSched(SimpleNamespace):
            # Support dict() coercion
            def keys(self):
                return [k for k in self.__dict__.keys()]
            def __getitem__(self, key):
                return self.__dict__[key]
        return FakeSched(**defaults)

    def test_standard_schedule(self):
        sched = self._fake_schedule()
        snap = DigestRunner._schedule_snapshot(sched)
        assert snap["id"] == 100
        assert snap["name"] == "Coffee Off 4pm"
        assert snap["enabled"] is True
        assert snap["type"] == "FakeSched"
        assert snap["description"] == "Turn off coffee machine at 4pm weekdays"
        assert snap.get("scheduleTime") == "16:00:00" or snap.get("schedule_time") == "16:00:00"
        # Noise keys must be dropped
        assert "xmlElement" not in snap
        assert "class" not in snap

    def test_schedule_without_description(self):
        sched = self._fake_schedule(description="")
        snap = DigestRunner._schedule_snapshot(sched)
        # Empty strings are stripped by filter; no bare "description" key
        assert snap.get("description") is None or snap.get("description") == ""

    def test_schedule_dict_coercion_failure_is_non_fatal(self):
        class BrokenSched:
            id = 999
            name = "Broken"
            enabled = True
            def keys(self):
                raise RuntimeError("breakage")
        snap = DigestRunner._schedule_snapshot(BrokenSched())
        assert snap["id"] == 999
        assert snap["name"] == "Broken"
        assert snap["enabled"] is True
        assert snap["type"] == "BrokenSched"


class TestTriggerSnapshot:
    def _fake_trigger(self, **overrides):
        defaults = {
            "id": 200,
            "name": "Kitchen motion → lights",
            "enabled": True,
            "description": "",
            "folderId": 0,
            "deviceId": 12345,
            "stateSelector": "onOffState",
            "stateValue": "on",
            "xmlElement": "<foo/>",
        }
        defaults.update(overrides)
        class FakeTrigger(SimpleNamespace):
            def keys(self):
                return [k for k in self.__dict__.keys()]
            def __getitem__(self, key):
                return self.__dict__[key]
        return FakeTrigger(**defaults)

    def test_device_state_change_trigger(self):
        t = self._fake_trigger()
        snap = DigestRunner._trigger_snapshot(t)
        assert snap["id"] == 200
        assert snap["name"] == "Kitchen motion → lights"
        # Subclass-specific fields captured either from dict() or from fallback getattr
        assert snap.get("deviceId") == 12345 or snap.get("device_id") == 12345
        assert snap.get("stateSelector") == "onOffState" or snap.get("state_selector") == "onOffState"
        assert "xmlElement" not in snap


class TestActionGroupSnapshot:
    def _fake_action_group(self, **overrides):
        defaults = {
            "id": 300,
            "name": "Kitchen Lights On Auto Dim",
            "description": "Turn on kitchen lights to 50%",
            "folderId": 0,
            "xmlElement": "<foo/>",
        }
        defaults.update(overrides)
        class FakeActionGroup(SimpleNamespace):
            def keys(self):
                return [k for k in self.__dict__.keys()]
            def __getitem__(self, key):
                return self.__dict__[key]
        return FakeActionGroup(**defaults)

    def test_standard_action_group(self):
        ag = self._fake_action_group()
        snap = DigestRunner._action_group_snapshot(ag)
        assert snap["id"] == 300
        assert snap["name"] == "Kitchen Lights On Auto Dim"
        assert snap["description"] == "Turn on kitchen lights to 50%"
        assert "xmlElement" not in snap
