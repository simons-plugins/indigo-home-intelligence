"""Shared data-access layer for curated house context.

Both the weekly digest (`DigestRunner`) and the interactive MCP
surface (`MCPHandler`, from Phase 1 of PRD-0002) read their house
snapshot from here. Keeping it in one module means device filtering,
trigger/schedule/action-group snapshotting, fleet-health scanning,
and SQL-Logger rollups all have a single implementation.

Note on side effects: everything in this module reads indigo state
(`indigo.devices`, `.triggers`, `.schedules`, `.actionGroups`) and the
configured `history_db`; nothing here writes. Rule writes,
observation persistence, and email delivery stay in their own
modules.
"""

from datetime import datetime
from typing import List, Optional

import indigo


# Plugins whose "devices" are mirrors/virtual/UI-only — exclude
# wholesale so Claude doesn't see every real light twice (once as
# the Shelly, once as the HomeKit mirror). These are Simon's house
# specifically; if this plugin gains other users we'd want this
# configurable.
_EXCLUDE_PLUGIN_IDS = frozenset(
    {
        "com.indigodomo.opensource.alexa-hue-bridge",
        "com.GlennNZ.indigoplugin.HomeKitLink-Siri",
        "com.perceptiveautomation.indigoplugin.devicecollection",
    }
)

# deviceTypeIds that are sub-widgets of a primary device (Shelly
# button children, input children, on-board CPU-temperature sensor
# that ships with every relay). We keep the primary switch/relay and
# drop the kids.
_EXCLUDE_DEVICE_TYPE_IDS = frozenset(
    {
        "component-button",
        "component-input",
        "component-temperature-onboard",
    }
)

# Noise keys from dict(obj): XML-serialisation internals plus boolean
# aliases (`configured`, `remoteDisplay`) that duplicate .enabled
# semantics. One shared set across schedule / trigger / action_group
# — keys that don't exist on a given object are harmlessly no-op.
_DROP_NOISE_KEYS = frozenset(
    {"configured", "remoteDisplay", "xmlElement", "xml", "class"}
)

# Fields we hand-compute on the snapshot before merging dict() output.
# We strip these from the merge so Indigo's raw bytes can't clobber our
# canonical values (most importantly `type`, which we set to the Python
# class name so Claude can distinguish DeviceStateChangeTrigger from
# PluginEventTrigger).
_RESERVED_SNAPSHOT_KEYS = frozenset({"id", "name", "enabled", "type"})

# Candidate attribute names for schedule fire-time. Indigo docs don't
# nail down the exact spelling and it may vary by schedule subtype;
# we probe each in order and keep the first non-empty value.
_SCHEDULE_TIME_CANDIDATES = (
    "scheduleTime",
    "time",
    "nextExecution",
    "nextDate",
    "nextScheduled",
)


class HouseContextAccess:
    """Curated house-state reader shared by the digest and MCP surfaces.

    Construction: one instance per plugin lifecycle, injected into
    both `DigestRunner` and the future `MCPHandler`. All deps
    (history_db, logger, thresholds) are injected via __init__ so
    tests can substitute fakes without touching indigo.
    """

    # Hard cap on devices queried for SQL rollup. Saves a minute-long
    # run on huge houses where PG psql startup dominates. 300 covers
    # every device seen in practice.
    _SQL_ROLLUP_DEVICE_CAP = 300

    # Top N energy consumers to show in the digest. Keeps prompt tokens
    # bounded on houses with 50+ energy-logged devices. The full per-
    # device map stays internal; only the top slice hits Claude.
    _TOP_ENERGY_N = 10

    def __init__(
        self,
        history_db,
        logger,
        whole_house_energy_device_id: Optional[int] = None,
        battery_low_threshold: int = 20,
        offline_hours_threshold: int = 24,
    ):
        self.history_db = history_db
        self.logger = logger
        self.whole_house_energy_device_id = whole_house_energy_device_id
        self.battery_low_threshold = battery_low_threshold
        self.offline_hours_threshold = offline_hours_threshold

    # ------------------------------------------------------------------
    # Fleet health
    # ------------------------------------------------------------------

    def fleet_health(self) -> dict:
        """Scan ``indigo.devices`` for low batteries and offline
        devices. Pure in-memory, no SQL — runs in milliseconds regardless
        of history DB state.

        - ``low_batteries``: any device with ``batteryLevel`` at or
          below the configured threshold (default 20%).
        - ``offline_devices``: ``errorState`` set OR
          ``lastSuccessfulComm`` older than the configured threshold
          (default 24h). Devices with no ``lastSuccessfulComm`` and no
          error are skipped — we have no evidence they're offline.

        Disabled devices are always skipped (the house-model filter
        already drops them; including here would create cross-references
        to devices Claude doesn't see)."""
        now = datetime.now().astimezone()
        low_batteries: List[dict] = []
        offline_devices: List[dict] = []
        for dev in indigo.devices:
            # Per-device isolation: one malformed device (attribute
            # raise, plugin stale state) must NOT kill the whole
            # fleet-health block and therefore skip the weekly digest.
            try:
                if not bool(getattr(dev, "enabled", True)):
                    continue
                battery = getattr(dev, "batteryLevel", None)
                if battery is not None and battery <= self.battery_low_threshold:
                    low_batteries.append(
                        {"id": dev.id, "name": dev.name, "battery_pct": battery}
                    )
                error_state = getattr(dev, "errorState", "") or ""
                last_comm = getattr(dev, "lastSuccessfulComm", None)
                hours_offline: Optional[float] = None
                if last_comm is not None:
                    try:
                        # lastSuccessfulComm is a tz-naive datetime in
                        # Indigo's local timezone. Coerce comparison via a
                        # naive now-local.
                        now_naive = now.replace(tzinfo=None)
                        hours_offline = round(
                            (now_naive - last_comm).total_seconds() / 3600, 1
                        )
                    except Exception as exc:
                        self.logger.debug(
                            f"Fleet health: lastSuccessfulComm delta failed "
                            f"for {dev.id}: {exc}"
                        )
                        hours_offline = None
                is_offline = bool(error_state) or (
                    hours_offline is not None
                    and hours_offline > self.offline_hours_threshold
                )
                if is_offline:
                    offline_devices.append(
                        {
                            "id": dev.id,
                            "name": dev.name,
                            "error_state": error_state or None,
                            "hours_offline": hours_offline,
                        }
                    )
            except (MemoryError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                obj_id = getattr(dev, "id", "?")
                obj_name = getattr(dev, "name", "?")
                self.logger.warning(
                    f"Fleet health: skipping device id={obj_id} "
                    f"name={obj_name!r}: {exc}"
                )
                continue
        # Cap each list at 30 entries; more than that and the digest
        # prompt ballooning matters more than naming every single one.
        # Claude can still say "and 14 others" from the total count.
        return {
            "low_batteries": sorted(low_batteries, key=lambda x: x["battery_pct"])[:30],
            "low_batteries_total": len(low_batteries),
            "offline_devices": offline_devices[:30],
            "offline_devices_total": len(offline_devices),
        }

    # ------------------------------------------------------------------
    # Energy context
    # ------------------------------------------------------------------

    def energy_context(self) -> dict:
        """Return whole-house week-over-week kWh plus the top N
        per-device consumers with their own WoW deltas.

        Returns an empty dict if the history DB isn't configured, the
        whole-house device isn't set, or the queries fail. Energy is
        nice-to-have — the digest still runs without it."""
        if self.history_db is None:
            return {}
        try:
            energy_device_ids = self.history_db.discover_energy_tables()
        except Exception as exc:
            self.logger.warning(f"Energy-table discovery failed: {exc}")
            return {}
        if not energy_device_ids:
            return {}

        # Only query devices that discovery found — discovery filters to
        # tables that actually have the ``accumEnergyTotal`` column, so
        # adding IDs outside that list would cause the UNION ALL to fail
        # on a missing / mis-typed table. If the configured whole-house
        # ID isn't in discovery, the whole_house block will legitimately
        # be omitted below.
        try:
            rollups = self.history_db.energy_rollup_14d(energy_device_ids)
        except Exception as exc:
            self.logger.warning(f"Energy rollup_14d failed: {exc}")
            return {}

        out: dict = {}

        # Whole-house: pluck by configured device ID if set.
        if self.whole_house_energy_device_id is not None:
            wh = rollups.get(self.whole_house_energy_device_id)
            if wh is not None:
                out["whole_house"] = {
                    "device_id": self.whole_house_energy_device_id,
                    **wh,
                }

        # Top consumers: per-device list, excluding the whole-house meter
        # (since it's the sum of everything downstream, counting it in
        # the "top consumers" list would always put it #1 and be
        # double-counting relative to itself).
        individual = {
            did: data
            for did, data in rollups.items()
            if did != self.whole_house_energy_device_id
        }
        # Sort by this-week consumption desc. Name resolution happens
        # inline via indigo.devices; missing names fall back to the id.
        name_lookup = {dev.id: dev.name for dev in indigo.devices}
        top = sorted(
            individual.items(),
            key=lambda kv: kv[1].get("this_week_kwh", 0),
            reverse=True,
        )[: self._TOP_ENERGY_N]
        out["top_consumers"] = [
            {
                "id": did,
                "name": name_lookup.get(did, str(did)),
                **data,
            }
            for did, data in top
        ]
        return out

    # ------------------------------------------------------------------
    # SQL rollups
    # ------------------------------------------------------------------

    def sql_rollups(self) -> dict:
        """Return per-device 7-day activity counts from SQL Logger, keyed
        by device_id as a string (JSON keys are always strings — avoids
        int-vs-string drift when this rides through the prompt).

        Returns an empty dict if the history DB isn't configured or if
        the query fails — rollups are a nice-to-have, not load-bearing
        for the digest."""
        if self.history_db is None:
            return {}
        try:
            device_ids = self.history_db.get_device_tables()
        except Exception as exc:
            self.logger.warning(f"SQL Logger device-table lookup failed: {exc}")
            return {}
        if not device_ids:
            return {}
        try:
            rollups = self.history_db.rollup_7d(
                device_ids[: self._SQL_ROLLUP_DEVICE_CAP]
            )
        except Exception as exc:
            self.logger.warning(f"SQL Logger rollup failed: {exc}")
            return {}
        return {str(did): body for did, body in rollups.items()}

    # ------------------------------------------------------------------
    # House model (devices, triggers, schedules, action groups)
    # ------------------------------------------------------------------

    @classmethod
    def _is_real_device(cls, dev) -> bool:
        """Return True for devices that represent a real, user-recognisable
        thing in the house: lights, switches, TRVs, thermostats, sensors,
        power meters, contact sensors. Drop mirrors (Alexa / HomeKit),
        virtual device collections, and sub-widget components."""
        plugin_id = getattr(dev, "pluginId", "") or ""
        if plugin_id in _EXCLUDE_PLUGIN_IDS:
            return False
        type_id = getattr(dev, "deviceTypeId", "") or ""
        if type_id in _EXCLUDE_DEVICE_TYPE_IDS:
            return False
        # Capability gate: must expose at least one of the real device
        # surfaces. Drops pure-virtual plugin devices that slipped past
        # the plugin-ID list above.
        return (
            hasattr(dev, "brightness")            # dimmers (lights)
            or hasattr(dev, "onState")            # relays, TRV switches, outlets
            or hasattr(dev, "temperatureInputs")  # thermostats
            or hasattr(dev, "sensorValue")        # temp/humidity/motion
        )

    @staticmethod
    def _device_type_label(dev) -> str:
        for attr, label in (
            ("brightness", "dimmer"),
            ("onState", "relay"),
            ("temperatureInputs", "thermostat"),
            ("sensorValue", "sensor"),
        ):
            if hasattr(dev, attr):
                return label
        return dev.__class__.__name__

    def build_house_model(self) -> dict:
        """Build the static house-shape block of the digest / MCP
        context.

        Filters applied:

        - Devices: only "real" devices (see ``is_real_device``) that are
          enabled. Dropping sub-components and mirrors is the biggest
          single cache-write saving on Simon's 1113-device house (~70%
          of the raw count is noise).
        - Triggers / schedules: only those with ``enabled=True``. The
          ``enabled`` key is stripped from the emitted snapshot (always
          true after filtering, so redundant).
        - Action groups: no enabled attribute in Indigo, pass through.
        - ``folderId`` is stripped from triggers/schedules/action-groups
          since the folder is a UI convenience; Claude reasons from names
          and descriptions. Devices keep ``folder_id`` so per-room
          grouping survives via ``device_folders``."""
        devices = []
        for dev in indigo.devices:
            if not self._is_real_device(dev):
                continue
            if not bool(getattr(dev, "enabled", True)):
                continue
            devices.append(
                {
                    "id": dev.id,
                    "name": dev.name,
                    "type": self._device_type_label(dev),
                    "model": getattr(dev, "model", "") or "",
                    "folder_id": getattr(dev, "folderId", None),
                }
            )

        triggers = self._snapshot_all(
            indigo.triggers, self._trigger_snapshot, "trigger"
        )
        schedules = self._snapshot_all(
            indigo.schedules, self._schedule_snapshot, "schedule"
        )
        action_groups = self._snapshot_all(
            indigo.actionGroups, self._action_group_snapshot, "action_group"
        )
        folders = [
            {"id": f.id, "name": f.name} for f in indigo.devices.folders
        ]

        return {
            "devices": devices,
            "device_folders": folders,
            "indigo_triggers": triggers,
            "indigo_schedules": schedules,
            "action_groups": action_groups,
        }

    # ------------------------------------------------------------------
    # Automation snapshots
    #
    # Indigo schedules, triggers, and action groups support dict()
    # coercion (same pattern as indigomcp's data adapter). We use that
    # to expose the configuration body — name + id + enabled alone
    # doesn't tell a reasoning model what a schedule or trigger
    # actually does.
    #
    # dict() coercion can be partial (a class may not expose every
    # field through the mapping protocol), so each snapshot also has a
    # named-attribute fallback for the fields we care about.
    # ------------------------------------------------------------------

    def _snapshot_all(self, iterable, snapshot_fn, label: str) -> List[dict]:
        """Iterate an `indigo.*` collection and build snapshots with
        per-object isolation: one broken object degrades to a stub and
        a warning, the rest keep full fidelity.

        Two post-filters applied to reduce cached-block size:

        - Disabled objects (``enabled=False``) are skipped. Action groups
          have no ``enabled`` attribute, so the ``getattr(..., True)``
          default passes them through unchanged.
        - ``enabled`` and ``folderId`` keys are stripped from the emitted
          snapshot. After the disabled filter ``enabled`` is always True
          (so redundant); ``folderId`` is UI organisation not semantics.

        Returns the list in original order, minus filtered-out objects."""
        out = []
        for obj in iterable:
            if not bool(getattr(obj, "enabled", True)):
                continue
            try:
                snapshot = snapshot_fn(obj, logger=self.logger)
            except (MemoryError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                obj_id = getattr(obj, "id", "?")
                obj_name = getattr(obj, "name", "?")
                self.logger.warning(
                    f"Skipping {label} id={obj_id} name={obj_name!r} "
                    f"in house model snapshot: {exc}"
                )
                out.append(
                    {"id": obj_id, "name": obj_name, "_snapshot_error": str(exc)}
                )
                continue
            snapshot.pop("enabled", None)
            snapshot.pop("folderId", None)
            out.append(snapshot)
        return out

    @classmethod
    def _schedule_snapshot(cls, schedule, logger=None) -> dict:
        """Serialise an Indigo schedule so Claude can see when it fires
        and what it does. Prefers dict() coercion; falls back to named
        attributes when the mapping protocol returns a partial result."""
        base = cls._safe_indigo_dict(schedule, logger=logger)
        snapshot = {
            "id": schedule.id,
            "name": schedule.name,
            "enabled": bool(schedule.enabled),
            "type": type(schedule).__name__,
        }
        snapshot.update(cls._extras(base, _DROP_NOISE_KEYS))

        # Fill headline fields that dict() missed. Key names mirror
        # Indigo's native camelCase so dict-path and fallback-path
        # produce identical shapes.
        if "description" not in snapshot:
            value = getattr(schedule, "description", None)
            if value:
                snapshot["description"] = cls._jsonable(value)
        if "folderId" not in snapshot:
            value = getattr(schedule, "folderId", None)
            if value is not None:
                snapshot["folderId"] = cls._jsonable(value)

        # Schedule fire-time: probe candidate attribute names and
        # expose the first populated one under its real attribute name
        # (scheduleTime / nextExecution / etc.), not a renamed slot.
        if not any(k in snapshot for k in _SCHEDULE_TIME_CANDIDATES):
            for attr in _SCHEDULE_TIME_CANDIDATES:
                value = getattr(schedule, attr, None)
                if value not in (None, ""):
                    snapshot[attr] = cls._jsonable(value)
                    break
        return snapshot

    @classmethod
    def _trigger_snapshot(cls, trigger, logger=None) -> dict:
        """Serialise an Indigo trigger so Claude can see the event
        condition and what fires as a result. Captures subclass-specific
        fields via dict() coercion plus named fallbacks for each
        documented subclass (DeviceStateChangeTrigger,
        VariableValueChangeTrigger, PluginEventTrigger)."""
        base = cls._safe_indigo_dict(trigger, logger=logger)
        snapshot = {
            "id": trigger.id,
            "name": trigger.name,
            "enabled": bool(trigger.enabled),
            "type": type(trigger).__name__,
        }
        snapshot.update(cls._extras(base, _DROP_NOISE_KEYS))

        # Subclass-specific fallbacks. Key names match Indigo's native
        # camelCase (which is what dict() emits), so dict-path and
        # fallback-path produce identical snapshot shapes.
        for attr in (
            "description",
            "folderId",
            "deviceId",
            "stateSelector",
            "stateValue",
            "variableId",
            "variableValue",
            "pluginId",
            "pluginTypeId",
        ):
            if attr in snapshot:
                continue
            value = getattr(trigger, attr, None)
            if value not in (None, ""):
                snapshot[attr] = cls._jsonable(value)
        return snapshot

    @classmethod
    def _action_group_snapshot(cls, action_group, logger=None) -> dict:
        """Serialise an Indigo action group. Note: Indigo's Object Model
        doesn't expose the per-action list of target devices via the
        Python mapping protocol, so Claude sees name + description +
        folder and has to rely on names for cross-referencing."""
        base = cls._safe_indigo_dict(action_group, logger=logger)
        snapshot = {
            "id": action_group.id,
            "name": action_group.name,
            "type": type(action_group).__name__,
        }
        snapshot.update(cls._extras(base, _DROP_NOISE_KEYS))

        # camelCase for consistency with dict() output.
        if "description" not in snapshot:
            value = getattr(action_group, "description", None)
            if value:
                snapshot["description"] = cls._jsonable(value)
        if "folderId" not in snapshot:
            value = getattr(action_group, "folderId", None)
            if value is not None:
                snapshot["folderId"] = cls._jsonable(value)
        return snapshot

    @classmethod
    def _extras(cls, base: dict, drop: frozenset) -> dict:
        """Filter a dict-coerced snapshot body down to keys worth merging:
        drop noise keys + any empty-value keys, and strip reserved keys
        that the caller set authoritatively (so dict() can't clobber
        id/name/enabled/type with wire values)."""
        filtered = cls._filter_keys(base, drop)
        return {k: v for k, v in filtered.items() if k not in _RESERVED_SNAPSHOT_KEYS}

    @classmethod
    def _safe_indigo_dict(cls, obj, logger=None) -> dict:
        """Coerce an Indigo object to a dict via the mapping protocol.
        On any Exception (e.g. a property that raises during enumeration),
        log at debug and return {} — the caller will still emit the
        hand-set id/name/enabled fields so the object doesn't disappear
        from the manifest."""
        try:
            raw = dict(obj)
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if logger is not None:
                obj_id = getattr(obj, "id", "?")
                logger.debug(
                    f"dict() coercion failed on {type(obj).__name__} "
                    f"id={obj_id}: {exc}; snapshot will use hand-set fields only"
                )
            return {}
        try:
            return {k: cls._jsonable(v) for k, v in raw.items()}
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if logger is not None:
                logger.debug(
                    f"_jsonable failed mid-dict on {type(obj).__name__}: {exc}; "
                    "snapshot will use hand-set fields only"
                )
            return {}

    @classmethod
    def _filter_keys(cls, d: dict, drop: frozenset) -> dict:
        """Strip drop-listed keys plus any None / empty-string /
        empty-list / empty-dict values. Preserves 0 and False — a
        disabled schedule legitimately has enabled=False and we still
        want to see it. Recurses into nested dicts and into dicts
        appearing as list elements (Indigo pluginProps can nest
        indigo.Dict / indigo.List arbitrarily)."""
        out = {}
        for k, v in d.items():
            if k in drop:
                continue
            if isinstance(v, dict):
                v = cls._filter_keys(v, drop)
            elif isinstance(v, list):
                v = [
                    cls._filter_keys(item, drop)
                    if isinstance(item, dict) else item
                    for item in v
                ]
            if v in (None, "", [], {}):
                continue
            out[k] = v
        return out

    @classmethod
    def _jsonable(cls, value):
        """Best-effort coerce an Indigo return value into something
        json.dumps can serialise. Primitives pass through; lists / tuples
        / dicts recurse; datetime, indigo.Dict, indigo.List, enum values
        and anything else fall through to str(). Dict keys are stringified
        (json.dumps can't serialise non-string keys and Indigo IDs are
        integers)."""
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        # Recursion is wrapped so one misbehaving proxy (e.g. a lazy
        # indigo.Dict whose items() raises on iteration) falls through
        # to str() instead of propagating out and killing the snapshot.
        try:
            if isinstance(value, (list, tuple)):
                return [cls._jsonable(v) for v in value]
            if isinstance(value, dict):
                return {str(k): cls._jsonable(v) for k, v in value.items()}
            if hasattr(value, "items"):
                return {str(k): cls._jsonable(v) for k, v in value.items()}
            if hasattr(value, "__iter__"):
                return [cls._jsonable(v) for v in value]
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            pass
        return str(value)
