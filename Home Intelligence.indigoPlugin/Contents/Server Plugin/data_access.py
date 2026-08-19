"""Shared data-access layer for curated house context.

Both the weekly digest (`DigestRunner`) and the interactive MCP
surface (`MCPHandler`, from Phase 1 of PRD-0002) read their house
snapshot from here. Keeping it in one module means device filtering,
trigger/schedule/action-group snapshotting, fleet-health scanning,
and SQL-Logger rollups all have a single implementation.

Note on side effects: everything in this module reads indigo state
(`indigo.devices`, `.triggers`, `.schedules`, `.actionGroups`), the
configured `history_db`, and (via `indidb_reader`) the Indigo
database file; nothing here writes. Rule writes, observation
persistence, and email delivery stay in their own modules.
"""

from datetime import datetime
from typing import List, Optional, Set

import indigo

from indidb_reader import IndiDbReader


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

    # Hard cap on decoded automations in the digest's automation-
    # contents block. Scoping to fired-this-week already trims hard;
    # the cap bounds prompt tokens on weeks where hundreds of
    # automations fire.
    _AUTOMATION_CONTENTS_CAP = 40

    # Embedded-script sources are already truncated to 2000 chars by
    # the reader; for the digest block a short excerpt is enough to
    # identify what the script touches.
    _SCRIPT_EXCERPT_AT = 200

    def __init__(
        self,
        history_db,
        logger,
        whole_house_energy_device_id: Optional[int] = None,
        battery_low_threshold: int = 20,
        offline_hours_threshold: int = 24,
        zwave_weak_neighbour_threshold: int = 2,
    ):
        self.history_db = history_db
        self.logger = logger
        self.whole_house_energy_device_id = whole_house_energy_device_id
        self.battery_low_threshold = battery_low_threshold
        self.offline_hours_threshold = offline_hours_threshold
        self.zwave_weak_neighbour_threshold = zwave_weak_neighbour_threshold
        # Lazily-constructed .indiDb reader (ADR-0010). Construction is
        # cheap and does no I/O, but deferring it keeps this class fully
        # constructible in tests that never touch automation contents.
        self._indidb_reader: Optional[IndiDbReader] = None

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
    # Z-Wave mesh health
    # ------------------------------------------------------------------

    def zwave_mesh_health(self, previous_snapshot: Optional[dict] = None) -> dict:
        """Scan Z-Wave devices' neighbour tables for mesh-health signals.

        Pure in-memory over ``indigo.devices``; the neighbour data comes
        from the Z-Wave interface's per-device ``globalProps``
        (``zwNodeNeighbors``). One entry per physical node — multi-
        endpoint devices share an address and the first one seen wins.

        - ``weak_nodes``: routing (mains-powered) nodes whose neighbour
          count is at or below the configured threshold (default 2).
          Battery/non-routing nodes are excluded — sparse neighbour
          tables are normal for them.
        - ``neighbour_changes`` / ``new_nodes`` / ``vanished_nodes``:
          only present when ``previous_snapshot`` is supplied; diffs
          this scan against it, biggest neighbour-count drops first.
        - ``snapshot``: the ``{address: neighbour_count}`` map for the
          caller to persist for next week's diff. Callers should strip
          it before putting the rest in a prompt.

        Caveat: Indigo refreshes neighbour tables only on node sync /
        network optimise, so an unchanged week may just mean no sync
        ran — the digest instructions say as much to Claude."""
        zwave_protocol = indigo.kProtocol.ZWave
        nodes: dict = {}
        for dev in indigo.devices:
            # Per-device isolation, same contract as fleet_health: one
            # malformed device must not kill the block or the digest.
            try:
                if not bool(getattr(dev, "enabled", True)):
                    continue
                if getattr(dev, "protocol", None) != zwave_protocol:
                    continue
                address = str(getattr(dev, "address", "") or "")
                if not address or address in nodes:
                    continue
                zw_props = self._zwave_interface_props(dev)
                neighbours = zw_props.get("zwNodeNeighbors")
                if not isinstance(neighbours, list):
                    # No neighbour table (node never synced) — nothing
                    # meaningful to report for this node.
                    continue
                features = str(zw_props.get("zwFeatureListStr", ""))
                # Battery nodes report "routing" too (routing *slave*),
                # so "routing" alone over-flags: a sleeping leak sensor
                # legitimately shows 0 neighbours. A node only counts as
                # a router when nothing marks it battery-powered.
                is_battery = (
                    "battery" in features
                    or getattr(dev, "batteryLevel", None) is not None
                )
                nodes[address] = {
                    "name": dev.name,
                    "neighbour_count": len(neighbours),
                    "is_router": "routing" in features and not is_battery,
                }
            except (MemoryError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                obj_id = getattr(dev, "id", "?")
                obj_name = getattr(dev, "name", "?")
                self.logger.warning(
                    f"Z-Wave mesh health: skipping device id={obj_id} "
                    f"name={obj_name!r}: {exc}"
                )
                continue

        snapshot = {a: info["neighbour_count"] for a, info in nodes.items()}
        weak = sorted(
            (
                {"address": a, **info}
                for a, info in nodes.items()
                if info["is_router"]
                and info["neighbour_count"] <= self.zwave_weak_neighbour_threshold
            ),
            key=lambda x: x["neighbour_count"],
        )
        result = {
            "nodes_total": len(nodes),
            "weak_threshold": self.zwave_weak_neighbour_threshold,
            "weak_nodes": weak[:30],
            "weak_nodes_total": len(weak),
            "snapshot": snapshot,
        }
        if previous_snapshot:
            prev = {
                str(k): v
                for k, v in previous_snapshot.items()
                if isinstance(v, int) and not isinstance(v, bool)
            }
            changes = [
                {
                    "address": a,
                    "name": nodes[a]["name"],
                    "was": prev[a],
                    "now": count,
                }
                for a, count in snapshot.items()
                if a in prev and prev[a] != count
            ]
            changes.sort(key=lambda c: c["now"] - c["was"])
            result["neighbour_changes"] = changes[:30]
            result["neighbour_changes_total"] = len(changes)
            result["new_nodes"] = sorted(set(snapshot) - set(prev))[:30]
            result["vanished_nodes"] = sorted(set(prev) - set(snapshot))[:30]
        return result

    @classmethod
    def _zwave_interface_props(cls, dev) -> dict:
        """Return the Z-Wave interface's sub-dict from ``globalProps``.

        Matched by plugin-id substring rather than a hardcoded id so an
        interface-id rename between Indigo releases degrades to an
        empty dict, not a wrong-key miss."""
        global_props = getattr(dev, "globalProps", None)
        if global_props is None or not hasattr(global_props, "items"):
            return {}
        for key, sub in global_props.items():
            if "zwave" in str(key).lower():
                coerced = cls._jsonable(sub)
                return coerced if isinstance(coerced, dict) else {}
        return {}

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
    # Automation contents (.indiDb reader — ADR-0010)
    # ------------------------------------------------------------------

    def _indidb(self) -> IndiDbReader:
        if self._indidb_reader is None:
            self._indidb_reader = IndiDbReader(
                indigo_module=indigo, logger=self.logger
            )
        return self._indidb_reader

    def automation_contents_context(self, fired_names_or_ids) -> dict:
        """Compact decoded contents for automations that fired.

        ``fired_names_or_ids`` is an iterable of automation names (str)
        and/or ids (int) — the digest passes the names it extracted
        from Trigger / Schedule / Action Group event-log lines, which
        scopes the block to the digest window (issue #27's token
        scoping). Matching automations (across schedules, triggers,
        and action groups) are compacted: raw class/code noise dropped,
        action labels + resolved device/variable/action-group names
        kept, condition trees summarised. Capped at
        ``_AUTOMATION_CONTENTS_CAP`` entries; ``matched_total`` carries
        the pre-cap count, with ``truncated`` (matches dropped by the
        cap) and ``compact_errors`` (records skipped by compaction
        failures) present when nonzero so the two loss modes are
        separately visible rather than hiding in
        ``matched_total - len(automations)``.

        Unlike ``energy_context`` this RAISES on reader failure (DB
        path unavailable, mid-write parse error — a friendly
        ``ValueError``): callers choose their own degradation (the
        digest logs a warning and omits the block). Per-automation
        compaction failures are isolated like ``fleet_health`` — one
        malformed record degrades to a warning, not a lost block."""
        fired_names: set = set()
        fired_ids: set = set()
        for item in fired_names_or_ids or ():
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                fired_ids.add(item)
            elif isinstance(item, str) and item.strip():
                fired_names.add(item.strip())
        if not fired_names and not fired_ids:
            return {"automations": [], "matched_total": 0}

        reader = self._indidb()
        data = reader.automations()

        matched: List[tuple] = []
        for kind in ("schedule", "trigger", "action_group"):
            for record in data[kind].values():
                if record["id"] in fired_ids or record["name"] in fired_names:
                    matched.append((kind, record))

        automations: List[dict] = []
        compact_errors = 0
        for kind, record in matched[: self._AUTOMATION_CONTENTS_CAP]:
            try:
                automations.append(
                    self._compact_automation(kind, record, reader)
                )
            except (MemoryError, KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                compact_errors += 1
                self.logger.warning(
                    f"Automation contents: skipping {kind} "
                    f"id={record.get('id')} name={record.get('name')!r}: {exc}"
                )
                continue

        result = {"automations": automations, "matched_total": len(matched)}
        cap_truncated = len(matched) - min(
            len(matched), self._AUTOMATION_CONTENTS_CAP
        )
        if cap_truncated:
            result["truncated"] = cap_truncated
        if compact_errors:
            result["compact_errors"] = compact_errors
        skipped = data.get("skipped_automations", 0)
        if skipped:
            result["skipped_automations"] = skipped
        return result

    def automations_acting_on(self, device_id: int) -> List[dict]:
        """Reverse index for the rule-write gate: native automations
        with an action step targeting ``device_id``.

        Returns ``[{automation_type, id, name, roles}, ...]`` where
        ``roles`` uses lite's vocabulary (``acts_on`` /
        ``acts_on_via_props`` / ``condition`` / ``watches``). Only
        automations that ACT ON the device are returned (one of the
        two acts_on roles always present); the others ride along as
        context.

        ``acts_on_via_props`` covers a plugin action step naming the
        device only inside its own parameters — the majority of the
        plugin action surface, and invisible to Indigo's own
        dependency check. Entries carrying it also carry
        ``matched_props`` naming the parameters that matched, because
        it is an INFERRED reference, not a declared one.

        Raises the reader's friendly ``ValueError`` on DB failure, and
        RuntimeError when the live-device lookup fails — callers
        (mcp_tools) degrade to ``_LOOKUP_UNAVAILABLE``, never blocking
        the write path. Failing loudly there is deliberate: this gate
        exists so a rule-write is warned about conflicting native
        automations, and a partial list would read as a complete
        one."""
        reader = self._indidb()
        data = reader.automations()
        presence = self._entity_presence("devices", device_id)
        if presence == "unavailable":
            raise RuntimeError(
                f"live device lookup failed for {device_id}; "
                "plugin action parameters were not searched"
            )
        match_props = presence == "present"
        out: List[dict] = []
        for kind in ("schedule", "trigger", "action_group"):
            for record in data[kind].values():
                try:
                    roles: List[str] = []
                    if any(
                        step.get("device_id") == device_id
                        for step in record.get("steps") or ()
                    ):
                        roles.append("acts_on")
                    matched_props: Set[str] = set()
                    if match_props:
                        for step in record.get("steps") or ():
                            matched_props.update(
                                self._step_prop_references(step, device_id)
                            )
                    if matched_props:
                        roles.append("acts_on_via_props")
                    if self._condition_references(
                        record.get("conditions"), device_id
                    ):
                        roles.append("condition")
                    if kind == "trigger" and (
                        (record.get("watch") or {}).get("device_id")
                        == device_id
                    ):
                        roles.append("watches")
                    if not ({"acts_on", "acts_on_via_props"} & set(roles)):
                        continue
                    entry = {
                        "automation_type": kind,
                        "id": record["id"],
                        "name": record["name"],
                        "roles": roles,
                    }
                    if matched_props:
                        entry["matched_props"] = sorted(matched_props)
                    out.append(entry)
                except (MemoryError, KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    self.logger.warning(
                        f"Automation reverse-index: skipping {kind} "
                        f"id={record.get('id')}: {exc}"
                    )
                    continue
        return out

    @staticmethod
    def _entity_presence(collection_attr: str, entity_id) -> str:
        """Whether ``entity_id`` names a live object: present /
        absent / unavailable.

        ``IndiDbReader.resolve_name`` collapses "no such object" and
        "the lookup itself failed" into the same ``None`` — right for
        a display label, wrong for gating a detection pass. Props
        matching is INFERRED from raw values, so it only runs when the
        id is known to name a live device: an id that names nothing
        could collide with an ordinary numeric parameter (a level, a
        delay). Kept in step with lite's ``_entity_presence``."""
        collection = getattr(indigo, collection_attr, None)
        if collection is None:
            return "unavailable"
        try:
            collection[entity_id]
        except (KeyError, IndexError, ValueError, TypeError):
            return "absent"
        except Exception:
            return "unavailable"
        return "present"

    @staticmethod
    def _prop_value_matches(value, entity_id) -> bool:
        """True if one decoded prop value names ``entity_id``.

        Props are plugin-defined, so an id arrives as an integer, a
        bare string, or one member of a comma-separated list. Booleans
        are excluded even though ``isinstance(True, int)`` is True,
        and floats are never ids — a ``real`` prop is a level or a
        setpoint. Kept in step with lite's copy."""
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return value == entity_id
        if isinstance(value, str):
            target = str(entity_id)
            return any(part.strip() == target for part in value.split(","))
        return False

    @classmethod
    def _iter_prop_matches(cls, value, entity_id, path: str = ""):
        """Yield the prop paths under ``value`` naming ``entity_id``,
        dotted for nested dicts and ``[i]`` for vector members."""
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                yield from cls._iter_prop_matches(
                    child, entity_id, child_path
                )
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from cls._iter_prop_matches(
                    child, entity_id, f"{path}[{index}]"
                )
        elif path and cls._prop_value_matches(value, entity_id):
            yield path

    @classmethod
    def _step_prop_references(cls, step, entity_id) -> List[str]:
        """Prop paths in a step naming ``entity_id``."""
        return list(cls._iter_prop_matches(step.get("props"), entity_id))

    @classmethod
    def _condition_references(cls, node, device_id) -> bool:
        """True if a decoded condition tree references the device."""
        if not isinstance(node, dict):
            return False
        children = node.get("conditions")
        if children is not None:
            return any(
                cls._condition_references(child, device_id)
                for child in children
            )
        return node.get("device_id") == device_id

    # -- compaction helpers -------------------------------------------

    @staticmethod
    def _ref(reader, collection: str, prefix: str, entity_id) -> dict:
        """``{prefix}_id`` + best-effort ``{prefix}_name`` for a
        referenced entity. Name stays an explicit ``None`` when the
        live IOM can't resolve it — that's the orphaned-reference
        signal the digest INSTRUCTIONS ask Claude to flag."""
        if entity_id is None:
            return {}
        return {
            f"{prefix}_id": entity_id,
            f"{prefix}_name": reader.resolve_name(collection, entity_id),
        }

    def _compact_automation(self, kind: str, record: dict, reader) -> dict:
        out = {
            "automation_type": kind,
            "id": record["id"],
            "name": record["name"],
            "steps": [
                self._compact_step(step, reader)
                for step in record.get("steps") or ()
            ],
        }
        # enabled is only worth tokens when it's surprising (a disabled
        # automation whose name still appeared in the fired window —
        # e.g. disabled mid-week).
        if record.get("enabled") is False:
            out["enabled"] = False
        conditions = self._compact_conditions(record.get("conditions"), reader)
        if conditions is not None:
            out["conditions"] = conditions
        watch = record.get("watch")
        if kind == "trigger" and watch:
            out["watch"] = self._compact_watch(watch, reader)
        schedule = record.get("schedule")
        if kind == "schedule" and schedule:
            out["fires"] = self._compact_schedule(schedule)
        return out

    @staticmethod
    def _compact_schedule(schedule: dict) -> dict:
        """A schedule's firing rule → compact digest form.

        The digest's "is this already automated?" reasoning needs the
        rule, not the next timestamp: an absolute 06:00 schedule and
        one tracking sunrise are the same single datetime to
        next_execution. Raw codes are dropped (the labels cover every
        value seen live) and zero-valued offsets stay out.
        """
        out = {"when": schedule.get("time_type")
               or f"time_type_code_{schedule.get('time_type_code')}"}
        for key, out_key in (
            ("time", "at"),
            ("sun_offset_seconds", "offset_seconds"),
            ("interval_seconds", "every_seconds"),
            ("randomize_seconds", "randomize_seconds"),
            ("days_of_week", "days"),
        ):
            value = schedule.get(key)
            if value:
                out[out_key] = value
        days_interval = schedule.get("repeat_interval_days")
        if days_interval and days_interval > 1:
            out["every_n_days"] = days_interval
        return out

    def _compact_step(self, step: dict, reader) -> dict:
        """One decoded step → compact digest form: ``do`` carries the
        action label (numeric code fallback, never guessed), raw
        ``class``/code fields are dropped, referenced ids gain
        resolved names."""
        stype = step.get("type")
        if stype in ("device_action", "hvac_action"):
            out = {
                "do": step.get("action_label")
                or f"action_code_{step.get('action_code')}",
                **self._ref(reader, "devices", "device", step.get("device_id")),
            }
            if step.get("action_value") is not None:
                out["value"] = step["action_value"]
            return out
        if stype == "execute_action_group":
            return {
                "do": "execute_action_group",
                **self._ref(
                    reader, "actionGroups", "action_group",
                    step.get("action_group_id"),
                ),
            }
        if stype == "variable_action":
            out = {
                "do": step.get("action_label")
                or f"variable_action_code_{step.get('action_code')}",
                **self._ref(
                    reader, "variables", "variable", step.get("variable_id")
                ),
            }
            if step.get("value") is not None:
                out["value"] = step["value"]
            return out
        if stype == "plugin_action":
            out = {
                "do": "plugin_action",
                "label": step.get("label"),
                "plugin_id": step.get("plugin_id"),
            }
            out.update(
                self._ref(reader, "devices", "device", step.get("device_id"))
            )
            return out
        if stype == "embedded_script":
            source = step.get("source") or ""
            return {
                "do": "embedded_script",
                "language": step.get("script_type_label")
                or f"script_type_{step.get('script_type')}",
                "source_excerpt": source[: self._SCRIPT_EXCERPT_AT],
                "truncated": bool(step.get("truncated"))
                or len(source) > self._SCRIPT_EXCERPT_AT,
            }
        return {"do": f"unknown_action_class_{step.get('class')}"}

    def _compact_conditions(self, node, reader) -> Optional[dict]:
        if not isinstance(node, dict):
            return None
        children = node.get("conditions")
        if children is not None:
            return {
                "logic": node.get("logic")
                or f"logic_code_{node.get('logic_code')}",
                "conditions": [
                    compacted
                    for compacted in (
                        self._compact_conditions(child, reader)
                        for child in children
                    )
                    if compacted is not None
                ],
            }
        ntype = node.get("type")
        if ntype == "device_state":
            out = {
                **self._ref(reader, "devices", "device", node.get("device_id")),
                "state": node.get("state"),
                "comparator": node.get("comparator")
                or f"comparator_code_{node.get('comparator_code')}",
            }
        elif ntype == "variable":
            out = {
                **self._ref(
                    reader, "variables", "variable", node.get("variable_id")
                ),
                "comparator": node.get("comparator")
                or f"comparator_code_{node.get('comparator_code')}",
            }
        elif ntype == "time_date":
            return {
                "time_window": {
                    "start": node.get("start"),
                    "end": node.get("end"),
                }
            }
        elif ntype == "sun":
            # "is it dark/light outside" — the generic fallthrough
            # below would keep the type and drop the state, which is
            # the only part that means anything.
            return {"sun": node.get("state") or f"type_code_{node.get('type_code')}"}
        else:
            return {"type": ntype}
        if node.get("value") not in (None, ""):
            out["value"] = node["value"]
        if node.get("value2"):
            out["value2"] = node["value2"]
        return out

    def _compact_watch(self, watch: dict, reader) -> dict:
        out: dict = {}
        out.update(self._ref(reader, "devices", "device", watch.get("device_id")))
        for key in ("state_selector", "state_value"):
            if watch.get(key) not in (None, ""):
                out[key] = watch[key]
        out.update(
            self._ref(reader, "variables", "variable", watch.get("variable_id"))
        )
        if watch.get("value") not in (None, ""):
            out["value"] = watch["value"]
        for key in ("plugin_id", "event_label"):
            if watch.get(key):
                out[key] = watch[key]
        return out

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
