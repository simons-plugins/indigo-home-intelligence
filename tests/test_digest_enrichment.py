"""Tests for digest.DigestRunner's schedule/trigger/action_group snapshot
helpers. Tests the pure logic: jsonable coercion, key filtering, safe
dict() wrapping, and the three snapshot builders. Snapshot methods are
exercised via fake Indigo-shaped objects (dict-coercible classes built
by _dict_coercible below)."""

from types import SimpleNamespace

import pytest

from digest import DigestRunner


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

# Keys we expect _filter_keys to strip — lets tests assert disjointness
# from the final snapshot in one line.
NOISE_KEYS = {"xmlElement", "class", "remoteDisplay", "configured"}


def _dict_coercible(cls_name: str, attrs: dict):
    """Build a SimpleNamespace that also supports dict() coercion.
    Python's dict() uses the mapping protocol (.keys() + __getitem__),
    so providing those makes the resulting instance behave like a real
    Indigo Schedule/Trigger/ActionGroup for our purposes."""
    def keys(self):
        return list(self.__dict__.keys())

    def getitem(self, key):
        return self.__dict__[key]

    Cls = type(cls_name, (SimpleNamespace,), {"keys": keys, "__getitem__": getitem})
    return Cls(**attrs)


class CaptureLogger:
    """Records .debug / .warning calls for assertion in tests."""
    def __init__(self):
        self.debug_calls = []
        self.warning_calls = []
        self.info_calls = []
        self.error_calls = []

    def debug(self, msg, *args, **kwargs):
        self.debug_calls.append(msg)

    def warning(self, msg, *args, **kwargs):
        self.warning_calls.append(msg)

    def info(self, msg, *args, **kwargs):
        self.info_calls.append(msg)

    def error(self, msg, *args, **kwargs):
        self.error_calls.append(msg)

    def exception(self, msg, *args, **kwargs):
        self.error_calls.append(msg)


# ---------------------------------------------------------------------
# _jsonable
# ---------------------------------------------------------------------


class TestJsonable:
    def test_primitives_unchanged(self):
        assert DigestRunner._jsonable(None) is None
        assert DigestRunner._jsonable(True) is True
        assert DigestRunner._jsonable(42) == 42
        assert DigestRunner._jsonable(3.14) == 3.14
        assert DigestRunner._jsonable("hello") == "hello"

    def test_false_and_zero_primitives(self):
        # Critical: False/0 pass through, not treated as empty signals.
        assert DigestRunner._jsonable(False) is False
        assert DigestRunner._jsonable(0) == 0

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

    def test_items_fallback_on_non_dict_mapping(self):
        """An object that has .items() but doesn't subclass dict — this
        is the real indigo.Dict path. Must recurse, not stringify."""
        class MappingLike:
            def __init__(self, data):
                self._data = data
            def items(self):
                return self._data.items()
        result = DigestRunner._jsonable(
            MappingLike({"a": 1, "b": MappingLike({"c": 2})})
        )
        assert result == {"a": 1, "b": {"c": 2}}

    def test_iter_fallback_on_non_list_iterable(self):
        """An object with __iter__ but not list/tuple/dict — the
        indigo.List path."""
        class IterableLike:
            def __init__(self, items):
                self._items = items
            def __iter__(self):
                return iter(self._items)
        result = DigestRunner._jsonable(
            IterableLike([1, "two", IterableLike([3, 4])])
        )
        assert result == [1, "two", [3, 4]]

    def test_items_that_raises_falls_through_to_str(self):
        """A lazy proxy whose items() raises on iteration must not
        kill the snapshot — it should degrade to str()."""
        class BrokenLazy:
            def items(self):
                raise RuntimeError("backend gone")
            def __str__(self):
                return "broken-lazy"
        assert DigestRunner._jsonable(BrokenLazy()) == "broken-lazy"

    def test_misbehaving_dict_subclass_does_not_propagate(self):
        """The isinstance(dict) branch used to be unguarded. If an
        indigo.Dict subclasses dict but misbehaves on iteration, it
        must fall through rather than propagate."""
        class BadDictSubclass(dict):
            def items(self):
                raise RuntimeError("lazy backend")
        # Should not raise — degrades to str().
        result = DigestRunner._jsonable(BadDictSubclass())
        assert isinstance(result, str)

    def test_misbehaving_list_subclass_does_not_propagate(self):
        """Symmetric guard for isinstance(list/tuple) branch."""
        class BadList(list):
            def __iter__(self):
                raise RuntimeError("lazy backend")
        result = DigestRunner._jsonable(BadList([1, 2]))
        assert isinstance(result, str)


# ---------------------------------------------------------------------
# _filter_keys
# ---------------------------------------------------------------------


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

    def test_recurses_into_nested_dicts(self):
        """Nested dicts must have drop-list keys stripped too —
        Indigo pluginProps can contain their own xmlElement noise."""
        d = {
            "outer": 1,
            "pluginProps": {
                "useful": "val",
                "xmlElement": "<nested/>",
                "more_nested": {"xmlElement": "<even deeper/>", "keep": "me"},
            },
        }
        result = DigestRunner._filter_keys(d, frozenset({"xmlElement"}))
        assert result == {
            "outer": 1,
            "pluginProps": {
                "useful": "val",
                "more_nested": {"keep": "me"},
            },
        }

    def test_recurses_through_list_of_dicts(self):
        """Dict elements inside a list must also get filtered."""
        d = {
            "actions": [
                {"op": "on", "xmlElement": "<a/>"},
                {"op": "off", "xmlElement": "<b/>"},
            ],
        }
        result = DigestRunner._filter_keys(d, frozenset({"xmlElement"}))
        assert result == {"actions": [{"op": "on"}, {"op": "off"}]}

    def test_nested_becomes_empty_is_dropped(self):
        """If recursion empties a nested dict entirely, the now-empty
        parent key is dropped by the outer filter."""
        d = {"shell": {"xmlElement": "<only-this/>"}}
        result = DigestRunner._filter_keys(d, frozenset({"xmlElement"}))
        assert result == {}

    def test_drops_none_from_jsonable_regression_guard(self):
        """Contract pin: None values are dropped. If _jsonable ever
        regresses to returning None for a preserved value, the filter
        will silently swallow the key. Documents the contract so an
        accidental change gets reviewed."""
        d = {"important_field": None}
        assert DigestRunner._filter_keys(d, frozenset()) == {}


# ---------------------------------------------------------------------
# _safe_indigo_dict
# ---------------------------------------------------------------------


class TestSafeIndigoDict:
    def test_dict_coercible_object_round_trips(self):
        class DictLike:
            def keys(self):
                return ["a", "b"]
            def __getitem__(self, key):
                return {"a": 1, "b": "two"}[key]
        result = DigestRunner._safe_indigo_dict(DictLike())
        assert result == {"a": 1, "b": "two"}

    def test_keys_failure_returns_empty(self):
        class Unfriendly:
            def keys(self):
                raise RuntimeError("cannot iterate")
        result = DigestRunner._safe_indigo_dict(Unfriendly())
        assert result == {}

    def test_getitem_partial_failure_returns_empty(self):
        """Python's dict() constructor aborts on the FIRST __getitem__
        exception — it does not produce a partial dict. Documenting
        this all-or-nothing behaviour."""
        class PartialFail:
            def keys(self):
                return ["a", "b", "c"]
            def __getitem__(self, key):
                if key == "b":
                    raise RuntimeError("broken state ref")
                return {"a": 1, "c": 3}[key]
        assert DigestRunner._safe_indigo_dict(PartialFail()) == {}

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

    def test_memory_error_propagates(self):
        """MemoryError indicates resource exhaustion — silent
        continuation would make the next object hit the same wall."""
        class OutOfMemory:
            def keys(self):
                raise MemoryError("exhausted")
        with pytest.raises(MemoryError):
            DigestRunner._safe_indigo_dict(OutOfMemory())

    def test_debug_logged_when_logger_provided(self):
        """Passing a logger surfaces silent degradation — without it
        an Indigo API change would silently kill enrichment quality."""
        class Unfriendly:
            def keys(self):
                raise RuntimeError("broken")
        logger = CaptureLogger()
        DigestRunner._safe_indigo_dict(Unfriendly(), logger=logger)
        assert len(logger.debug_calls) == 1
        assert "dict() coercion failed" in logger.debug_calls[0]

    def test_no_log_when_logger_is_none(self):
        """Default None = silent, so pure-function tests don't crash."""
        class Unfriendly:
            def keys(self):
                raise RuntimeError("broken")
        # Must not raise.
        DigestRunner._safe_indigo_dict(Unfriendly())


# ---------------------------------------------------------------------
# _extras
# ---------------------------------------------------------------------


class TestExtras:
    def test_strips_reserved_keys(self):
        """Hand-set id/name/enabled/type must not be clobbered by the
        dict()-coerced body."""
        base = {"id": 999, "name": "wrong", "enabled": "string-not-bool", "type": "X"}
        result = DigestRunner._extras(base, frozenset())
        assert result == {}

    def test_strips_drop_list(self):
        base = {"keep": "me", "xmlElement": "<noise/>"}
        result = DigestRunner._extras(base, frozenset({"xmlElement"}))
        assert result == {"keep": "me"}

    def test_preserves_unrelated_fields(self):
        base = {"scheduleTime": "16:00:00", "folderId": 3}
        result = DigestRunner._extras(base, frozenset())
        assert result == {"scheduleTime": "16:00:00", "folderId": 3}


# ---------------------------------------------------------------------
# _schedule_snapshot / _trigger_snapshot / _action_group_snapshot
# ---------------------------------------------------------------------


class TestScheduleSnapshot:
    def _fake_schedule(self, **overrides):
        defaults = {
            "id": 100,
            "name": "Coffee Off 4pm",
            "enabled": True,
            "description": "Turn off coffee machine at 4pm weekdays",
            "folderId": 0,
            "nextExecution": None,
            "scheduleTime": "16:00:00",
            # Noise that must be stripped.
            "xmlElement": "<foo/>",
            "remoteDisplay": True,
            "class": "Schedule",
        }
        defaults.update(overrides)
        return _dict_coercible("FakeSched", defaults)

    def test_standard_schedule_populates_from_dict(self):
        sched = self._fake_schedule()
        snap = DigestRunner._schedule_snapshot(sched)
        assert snap["id"] == 100
        assert snap["name"] == "Coffee Off 4pm"
        assert snap["enabled"] is True
        assert snap["type"] == "FakeSched"
        assert snap["description"] == "Turn off coffee machine at 4pm weekdays"
        assert snap["scheduleTime"] == "16:00:00"
        assert NOISE_KEYS.isdisjoint(snap.keys())

    def test_schedule_without_description(self):
        sched = self._fake_schedule(description="")
        snap = DigestRunner._schedule_snapshot(sched)
        assert "description" not in snap

    def test_dict_coercion_failure_falls_back_to_getattr(self):
        """Dict() raises; hand-set fields still populate; fallback
        getattrs recover description / folderId / scheduleTime under
        Indigo's native camelCase keys."""
        class BrokenSched:
            id = 999
            name = "Broken"
            enabled = True
            description = "fallback desc"
            folderId = 7
            scheduleTime = "09:00:00"
            def keys(self):
                raise RuntimeError("breakage")
        snap = DigestRunner._schedule_snapshot(BrokenSched())
        assert snap["id"] == 999
        assert snap["name"] == "Broken"
        assert snap["enabled"] is True
        assert snap["type"] == "BrokenSched"
        assert snap["description"] == "fallback desc"
        assert snap["folderId"] == 7
        assert snap["scheduleTime"] == "09:00:00"

    def test_schedule_time_candidate_attributes(self):
        """If `scheduleTime` doesn't exist but `nextExecution` does,
        the fallback emits it under its real attribute name — the
        snapshot shape stays aligned with Indigo's camelCase."""
        class OddSchedule:
            id = 1
            name = "Odd"
            enabled = True
            description = ""
            nextExecution = "22:30:00"
            def keys(self):
                return []
        snap = DigestRunner._schedule_snapshot(OddSchedule())
        assert snap["nextExecution"] == "22:30:00"
        assert "scheduleTime" not in snap

    def test_hand_set_keys_not_clobbered_by_dict(self):
        """If dict() exposes `type` or `enabled`, hand-set values win —
        otherwise Claude would see a wire value that disagrees with
        the Python class name or a non-bool enabled."""
        sched = self._fake_schedule(
            type="ShouldNotOverrideClassName",
            enabled="definitely-not-a-bool",
        )
        snap = DigestRunner._schedule_snapshot(sched)
        assert snap["type"] == "FakeSched"
        assert snap["enabled"] is True


class TestTriggerSnapshot:
    def _fake_trigger(self, cls_name="FakeTrigger", **attrs):
        defaults = {
            "id": 200,
            "name": "trigger name",
            "enabled": True,
            "description": "",
            "folderId": 0,
            "xmlElement": "<foo/>",
        }
        defaults.update(attrs)
        return _dict_coercible(cls_name, defaults)

    def test_device_state_change_trigger(self):
        t = self._fake_trigger(
            cls_name="DeviceStateChangeTrigger",
            name="Kitchen motion → lights",
            deviceId=12345,
            stateSelector="onOffState",
            stateValue="on",
        )
        snap = DigestRunner._trigger_snapshot(t)
        assert snap["type"] == "DeviceStateChangeTrigger"
        assert snap["deviceId"] == 12345
        assert snap["stateSelector"] == "onOffState"
        assert snap["stateValue"] == "on"
        assert NOISE_KEYS.isdisjoint(snap.keys())

    def test_variable_value_change_trigger(self):
        t = self._fake_trigger(
            cls_name="VariableValueChangeTrigger",
            name="Doorbell count changed",
            variableId=55555,
            variableValue="1",
        )
        snap = DigestRunner._trigger_snapshot(t)
        assert snap["type"] == "VariableValueChangeTrigger"
        assert snap["variableId"] == 55555
        assert snap["variableValue"] == "1"

    def test_plugin_event_trigger(self):
        t = self._fake_trigger(
            cls_name="PluginEventTrigger",
            name="Custom plugin event",
            pluginId="com.example.someplugin",
            pluginTypeId="motionDetected",
        )
        snap = DigestRunner._trigger_snapshot(t)
        assert snap["type"] == "PluginEventTrigger"
        assert snap["pluginId"] == "com.example.someplugin"
        assert snap["pluginTypeId"] == "motionDetected"

    def test_fallback_fires_when_dict_omits_field(self):
        """If dict() doesn't include `deviceId` but the attribute
        exists on the trigger, the named fallback emits it under the
        canonical camelCase key — no snake_case duplicate."""
        class PartialDict:
            id = 300
            name = "partial"
            enabled = True
            description = ""
            folderId = 0
            deviceId = 99999
            stateSelector = "onOffState"
            stateValue = True
            def keys(self):
                # Expose only the basics; deviceId etc. are attributes
                # but not in dict() output.
                return ["id", "name", "enabled", "folderId"]
            def __getitem__(self, key):
                return getattr(self, key)
        snap = DigestRunner._trigger_snapshot(PartialDict())
        assert snap["deviceId"] == 99999
        assert snap["stateSelector"] == "onOffState"
        assert snap["stateValue"] is True
        # No snake_case duplicates in the snapshot.
        assert "device_id" not in snap
        assert "state_selector" not in snap


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
        return _dict_coercible("FakeActionGroup", defaults)

    def test_standard_action_group(self):
        ag = self._fake_action_group()
        snap = DigestRunner._action_group_snapshot(ag)
        assert snap["id"] == 300
        assert snap["name"] == "Kitchen Lights On Auto Dim"
        assert snap["description"] == "Turn on kitchen lights to 50%"
        assert NOISE_KEYS.isdisjoint(snap.keys())

    def test_dict_failure_is_non_fatal(self):
        """Hand-set fields + fallback getattrs still populate snapshot."""
        class Broken:
            id = 301
            name = "Broken"
            description = "fallback desc"
            folderId = 3
            def keys(self):
                raise RuntimeError("bad")
        snap = DigestRunner._action_group_snapshot(Broken())
        assert snap["id"] == 301
        assert snap["name"] == "Broken"
        assert snap["description"] == "fallback desc"
        assert snap["folderId"] == 3


# ---------------------------------------------------------------------
# _snapshot_all — per-object isolation
# ---------------------------------------------------------------------


class TestSnapshotAll:
    """The per-object try/except in _build_house_model: one broken
    object degrades to a stub + warning; the rest keep full fidelity."""

    def _fake_runner(self):
        """Build a minimal DigestRunner-like object exposing just what
        _snapshot_all needs: self.logger. Avoids constructing the full
        DigestRunner (which needs a Claude client etc.)."""
        class Mini:
            _snapshot_all = DigestRunner._snapshot_all
            logger = CaptureLogger()
        return Mini()

    def test_healthy_objects_pass_through(self):
        runner = self._fake_runner()

        def snap(obj, logger=None):
            return {"id": obj.id, "name": obj.name}

        result = runner._snapshot_all(
            [SimpleNamespace(id=1, name="a"), SimpleNamespace(id=2, name="b")],
            snap,
            "thing",
        )
        assert result == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        assert runner.logger.warning_calls == []

    def test_one_broken_does_not_poison_the_rest(self):
        runner = self._fake_runner()

        def snap(obj, logger=None):
            if obj.name == "broken":
                raise RuntimeError("snapshot internals failed")
            return {"id": obj.id, "name": obj.name}

        result = runner._snapshot_all(
            [
                SimpleNamespace(id=1, name="good1"),
                SimpleNamespace(id=2, name="broken"),
                SimpleNamespace(id=3, name="good2"),
            ],
            snap,
            "trigger",
        )
        assert len(result) == 3
        # Good snapshots unchanged.
        assert result[0] == {"id": 1, "name": "good1"}
        assert result[2] == {"id": 3, "name": "good2"}
        # Broken one is a stub with diagnostic info.
        assert result[1]["id"] == 2
        assert result[1]["name"] == "broken"
        assert "_snapshot_error" in result[1]
        assert "snapshot internals failed" in result[1]["_snapshot_error"]
        # Operator-visible warning logged.
        assert len(runner.logger.warning_calls) == 1
        assert "broken" in runner.logger.warning_calls[0]

    def test_memory_error_propagates(self):
        runner = self._fake_runner()

        def snap(obj, logger=None):
            raise MemoryError("out of memory")

        with pytest.raises(MemoryError):
            runner._snapshot_all([SimpleNamespace(id=1, name="x")], snap, "thing")
