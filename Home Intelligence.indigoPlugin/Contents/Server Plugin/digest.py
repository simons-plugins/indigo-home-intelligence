"""
Weekly digest runner.

v1: reasons over the home's structure (device inventory, triggers,
schedules, existing agent rules) + memory of prior observations, and
asks Claude to surface ONE thing worth the user's attention with an
optional proposed automation rule. No SQL Logger history rollup yet —
that's tracked in issue #3 on the repo.

Prompt layout (matters for caching):
    system_blocks[0] = INSTRUCTIONS  (small, stable — NOT tagged for
                      cache because it's well under the 1024-token
                      minimum-cacheable-prefix threshold)
    system_blocks[1] = HOUSE MODEL + existing rules + recent
                      observations, tagged with cache_control. This
                      is the large block (~100k tokens for a
                      thousand-device house); worth caching even
                      though weekly cadence rarely benefits from the
                      5-minute TTL. The real benefit is manual
                      re-runs ("Run Digest Now" back-to-back) and
                      future integrations that call digest/summarise
                      paths repeatedly.
    user_message    = Current date + digest task (volatile — always
                      changes, explicitly outside the cache prefix).

Output is JSON instructed-in-the-prompt. We validate shape via
_validate_parsed after parsing and skip rather than email a
malformed digest. See _validate_parsed for the allowed shape.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import indigo

from anthropic_client import AnthropicClient, AnthropicError
from event_log_reader import EventLogReader


# ---------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------

INSTRUCTIONS = """\
You are the Home Intelligence agent for an Indigo home-automation server.
Once a week you produce a short written digest for the owner of the home.

GOALS
- Filter, don't dump. The owner wants the ONE thing worth their attention
  this week — not a list of everything that happened.
- When you suggest an automation, propose a concrete rule the plugin can
  enforce directly. The rule schema is fixed (see below) — no free-form
  code, no DSL.
- Never re-suggest something the owner has already declined or that is
  already automated by an existing trigger, schedule, or agent rule.

EVENT LOG FORMAT
The event log is delivered in two fenced code blocks in the user message:

- ``event_log_summary`` — one compact JSON object with aggregate counts
  (``total_events``, ``top_sources``, ``events_by_hour``,
  ``sql_logger_rollups``).
- ``event_log_timeline`` — JSON-lines, chronological, one **positional
  array** per line with the shape ``["MM-DD HH:MM:SS", source, message]``.
  The year is always the current year (shown in the digest window
  header). Milliseconds are omitted. Multi-line tracebacks inside
  ``message`` appear as escaped ``\\n`` inside the string.

REASONING OVER THE EVENT LOG
You are given the last 7 days of Indigo event log narrations alongside
the static house model. Use them to understand what ACTUALLY happens,
not just what's configured:

- Action group names like "Simon Light Off" or "Study Lights Evening"
  don't reveal what devices they control. The log does: look for the
  device state-change narrations ("sent 'Study Lamp' off") that appear
  immediately after an action group fires to infer its effect.
- "Auto Lights" narrations show the plugin's presence-based zone logic
  — treat this as active automation even though it's not a native
  Indigo trigger / schedule.
- A device changing state without any nearby (within ~5 seconds)
  trigger, schedule, or action-group narration is EITHER a manual
  action OR a trigger with "Write to Event Log" disabled. Hedge
  accordingly — don't confidently call it manual.
- Prefer observations grounded in actual behaviour ("Study Lamp was
  manually left on past 23:00 on 4 of 7 nights") over structural
  ones ("you have a study lamp with no auto-off"). The former is
  actionable, the latter is speculation.

RULE SCHEMA (the shape `observation.proposed_rule` must match, when non-null)
  {
    "description": "<one sentence in plain English>",
    "when": {
      "device_id": <int>,
      "state": "<state key, e.g. onState, brightness>",
      "equals": <bool | number | string>,
      "after_local_time": "HH:MM" | null,   // optional
      "before_local_time": "HH:MM" | null,  // optional
      "for_minutes": <int> | null           // optional: hold duration
    },
    "then": {
      "device_id": <int>,
      "op": "on" | "off" | "toggle" | "set_brightness",
      "value": <int> | null                 // only for set_brightness
    }
  }

OUTPUT FORMAT
Return ONLY a single JSON object. No markdown code fences. No preamble.
The JSON object must have this shape:

  {
    "subject": "<email subject line, 40-80 chars>",
    "narrative_markdown": "<digest body as Markdown, see NARRATIVE STRUCTURE>",
    "observation": null | {
      "headline": "<one-line summary of the observation>",
      "rationale": "<1-3 sentences: WHY you're flagging this>",
      "related_devices": [<device_id>, ...],
      "proposed_rule": null | { ...schema above... }
    }
  }

SUBJECT FORMAT
Start the subject with the week range in the form "Week of D-D Mon:"
followed by a short headline summarising this week. 40-80 chars total.
The plugin appends an "[obs-XXXXXX]" reply token; you don't include it.
Examples:
  "Week of 14-21 Apr: Dining TRV keeps dropping off"
  "Week of 14-21 Apr: quiet week, everything healthy"

NARRATIVE STRUCTURE
Always follow this order. Don't deviate even when the week is quiet —
the consistent shape is what makes the weekly email feel like a
newsletter rather than an alert.

  1. Opening section with "## <warm weekly-status heading>"
     — one paragraph (3-5 sentences) giving the big picture of the
     week: what's running smoothly, what agent rules are quietly
     holding, notable calm. Addresses the owner directly as "you".
     Even when flagging an observation, lead with the roundup FIRST
     so the observation lands in context rather than cold.

  2. "### What caught my eye"
     — 2-4 paragraphs describing the observation in narrative prose.
     Use **bold** sparingly to highlight device names or key facts.
     Omit this section entirely (and the next) on a quiet week with
     observation=null.

  3. "### The inference"
     — 1-2 paragraphs explaining WHY this pattern matters and what a
     proposed rule would do differently. Omit on a quiet week.

  4. Closing line
     — warm single sentence inviting a reply, or (on a quiet week)
     a reassuring one-liner confirming nothing requires attention.

For quiet weeks (observation=null): expand the opening roundup to
2-3 paragraphs covering each category (heating, lighting, security,
cost / automations) rather than collapsing it. It's better to say
"quiet week, everything looks healthy" at length than to invent a
concern.

Keep the narrative warm and direct, not corporate. Refer to the owner
as "you".
"""


# ---------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------


class DigestRunner:
    def __init__(
        self,
        history_db,
        rule_store,
        observation_store,
        delivery,
        api_key: str,
        model: str,
        email_to: str,
        logger,
    ):
        self.history_db = history_db
        self.rule_store = rule_store
        self.observation_store = observation_store
        self.delivery = delivery
        self.model = model
        self.email_to = email_to
        self.logger = logger
        self.client = AnthropicClient(api_key=api_key, model=model, logger=logger)
        self.event_log = EventLogReader(logger=logger)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, window_days: int = 7) -> Optional[str]:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=window_days)

        if not self.client.configured():
            self.logger.warning("No Anthropic API key configured; digest skipped")
            return None
        if not self.email_to:
            self.logger.warning("No digest recipient configured; digest skipped")
            return None

        try:
            house_model = self._build_house_model()
            rules = self.rule_store.list_rules()
            prior_observations = self.observation_store.recent_for_prompt()
            events = self.event_log.read_window(days_back=window_days)
            event_summary = self.event_log.summarise(events)
            event_summary["sql_logger_rollups"] = self._sql_rollups()
        except Exception as exc:
            self.logger.exception(f"Digest context gathering failed: {exc}")
            return None

        system_blocks = self._build_system_blocks(house_model, rules, prior_observations)
        user_message = self._build_user_message(
            now, since, window_days, events, event_summary
        )

        self.logger.info(
            f"Digest: calling {self.model} "
            f"(devices={len(house_model['devices'])}, rules={len(rules)}, "
            f"prior_obs={len(prior_observations)}, events={len(events)})"
        )
        try:
            response = self.client.create_message(
                system_blocks=system_blocks,
                user_message=user_message,
                max_tokens=3000,
            )
        except AnthropicError as exc:
            self.logger.error(
                f"Digest Claude call failed: {exc} (status={exc.status}) "
                f"body={(exc.body or '')[:300]}"
            )
            return None

        usage = self.client.extract_usage(response)
        cost_gbp = self.client.estimate_cost_gbp(usage, self.model)
        self.logger.info(
            f"Digest usage: in={usage['input_tokens']} out={usage['output_tokens']} "
            f"cache_read={usage['cache_read_input_tokens']} "
            f"cache_write={usage['cache_creation_input_tokens']} "
            f"~GBP{cost_gbp}"
        )

        raw_text = self.client.extract_text(response)
        parsed = self._parse_json(raw_text)
        if parsed is None:
            self.logger.error(
                f"Digest: could not parse Claude JSON output; first 300 chars: {raw_text[:300]!r}"
            )
            return None

        validation_error = self._validate_parsed(parsed)
        if validation_error is not None:
            self.logger.error(
                f"Digest: Claude output failed schema validation: {validation_error}. "
                f"First 500 chars of raw: {raw_text[:500]!r}"
            )
            return None

        return self._deliver(parsed, usage, cost_gbp)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_system_blocks(
        self, house_model: dict, rules: List[dict], prior_observations: List[dict]
    ) -> List[dict]:
        # Compact JSON — the cached block pays 1.25x the base input rate,
        # so every byte of pretty-print whitespace costs real money for
        # zero reasoning benefit. Claude doesn't read indentation.
        context = (
            "HOUSE MODEL\n"
            f"{json.dumps(house_model, separators=(',', ':'))}\n\n"
            "EXISTING AGENT RULES (already enforced by the plugin)\n"
            f"{json.dumps(rules, separators=(',', ':')) if rules else '(none yet)'}\n\n"
            "RECENT OBSERVATIONS (past suggestions — avoid repeating)\n"
            f"{json.dumps(prior_observations, separators=(',', ':')) if prior_observations else '(none yet)'}"
        )
        return [
            {"type": "text", "text": INSTRUCTIONS},
            {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
        ]

    def _build_user_message(
        self,
        now: datetime,
        since: datetime,
        window_days: int,
        events: List[dict],
        event_summary: dict,
    ) -> str:
        """Build the volatile user-message block for the digest call.

        Compact JSON throughout — Claude doesn't read indentation and the
        event log dominates the uncached input cost, so every byte of
        structural overhead matters:

        - ``event_log_summary`` — compact JSON object (no indent).
        - ``event_log_timeline`` — JSON-lines of **positional arrays**
          ``["MM-DD HH:MM:SS", source, message]`` rather than keyed
          objects. Drops ~15 tokens/event of repeated JSON keys and
          the ``20YY-`` / ``.mmm`` timestamp prefix/suffix. Schema is
          documented in INSTRUCTIONS so Claude knows the positions.

        Multi-line tracebacks in ``message`` still survive because
        embedded newlines are escaped as ``\\n`` inside the JSON string."""
        local_now = now.astimezone()
        summary_block = json.dumps(event_summary, separators=(',', ':'))
        # Timestamp slicing: "2026-04-22 07:37:42.510"[5:19] -> "04-22 07:37:42"
        # drops year (implicit from the digest window) and milliseconds
        # (not load-bearing at weekly resolution).
        timeline_lines = [
            json.dumps(
                [e["timestamp"][5:19], e["source"], e["message"]],
                separators=(',', ':'),
            )
            for e in events
        ]
        timeline_block = "\n".join(timeline_lines)

        return (
            f"Current local time: {local_now.isoformat(timespec='minutes')}\n"
            f"Digest window: last {window_days} days "
            f"({since.date().isoformat()} to {now.date().isoformat()})\n\n"
            "EVENT LOG SUMMARY (compact JSON — totals, top sources, hourly "
            "distribution, and per-device 7-day SQL Logger rollups)\n"
            "```event_log_summary\n"
            f"{summary_block}\n"
            "```\n\n"
            "EVENT LOG TIMELINE (JSON-lines positional arrays, "
            "chronological — schema: [\"MM-DD HH:MM:SS\", source, message]; "
            "year is the current year; multi-line tracebacks in the "
            "message field survive as escaped \\n)\n"
            "```event_log_timeline\n"
            f"{timeline_block}\n"
            "```\n\n"
            "Produce this week's digest as a single JSON object matching the schema. "
            "Return JSON only."
        )

    # Hard cap on devices queried for SQL rollup. Saves a minute-long run
    # on huge houses where PG psql startup dominates. 300 covers every
    # device I've seen in practice.
    _SQL_ROLLUP_DEVICE_CAP = 300

    def _sql_rollups(self) -> dict:
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

    @classmethod
    def _is_real_device(cls, dev) -> bool:
        """Return True for devices that represent a real, user-recognisable
        thing in the house: lights, switches, TRVs, thermostats, sensors,
        power meters, contact sensors. Drop mirrors (Alexa / HomeKit),
        virtual device collections, and sub-widget components."""
        plugin_id = getattr(dev, "pluginId", "") or ""
        if plugin_id in cls._EXCLUDE_PLUGIN_IDS:
            return False
        type_id = getattr(dev, "deviceTypeId", "") or ""
        if type_id in cls._EXCLUDE_DEVICE_TYPE_IDS:
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

    def _build_house_model(self) -> dict:
        """Build the static house-shape block of the digest prompt.

        Filters applied:

        - Devices: only "real" devices (see ``_is_real_device``) that are
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

    # Noise keys from dict(obj): XML-serialisation internals plus
    # boolean aliases (`configured`, `remoteDisplay`) that duplicate
    # .enabled semantics. One shared set across schedule / trigger /
    # action_group — keys that don't exist on a given object are
    # harmlessly no-op.
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
        snapshot.update(cls._extras(base, cls._DROP_NOISE_KEYS))

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
        if not any(k in snapshot for k in cls._SCHEDULE_TIME_CANDIDATES):
            for attr in cls._SCHEDULE_TIME_CANDIDATES:
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
        snapshot.update(cls._extras(base, cls._DROP_NOISE_KEYS))

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
        snapshot.update(cls._extras(base, cls._DROP_NOISE_KEYS))

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
        return {k: v for k, v in filtered.items() if k not in cls._RESERVED_SNAPSHOT_KEYS}

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

    @staticmethod
    def _filter_keys(d: dict, drop: frozenset) -> dict:
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
                v = DigestRunner._filter_keys(v, drop)
            elif isinstance(v, list):
                v = [
                    DigestRunner._filter_keys(item, drop)
                    if isinstance(item, dict) else item
                    for item in v
                ]
            if v in (None, "", [], {}):
                continue
            out[k] = v
        return out

    @staticmethod
    def _jsonable(value):
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
                return [DigestRunner._jsonable(v) for v in value]
            if isinstance(value, dict):
                return {str(k): DigestRunner._jsonable(v) for k, v in value.items()}
            if hasattr(value, "items"):
                return {str(k): DigestRunner._jsonable(v) for k, v in value.items()}
            if hasattr(value, "__iter__"):
                return [DigestRunner._jsonable(v) for v in value]
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            pass
        return str(value)

    # ------------------------------------------------------------------
    # Output handling
    # ------------------------------------------------------------------

    _ALLOWED_RULE_OPS = {"on", "off", "toggle", "set_brightness"}

    @classmethod
    def _validate_parsed(cls, parsed: object) -> Optional[str]:
        """Check that a parsed JSON response matches the documented digest
        schema. Returns None on success, a short error string on failure.

        Strict enough to catch "model returned garbage" / "model returned
        the example block instead of the answer" / "proposed_rule missing
        required fields". Permissive about optional fields (related_devices
        defaulting to empty, etc.)."""
        if not isinstance(parsed, dict):
            return f"top-level is {type(parsed).__name__}, expected object"

        subject = parsed.get("subject")
        if not isinstance(subject, str) or not subject.strip():
            return "subject missing or empty"

        narrative = parsed.get("narrative_markdown")
        if not isinstance(narrative, str) or not narrative.strip():
            return "narrative_markdown missing or empty"

        observation = parsed.get("observation")
        if observation is None:
            return None  # informational-only digest, valid

        if not isinstance(observation, dict):
            return f"observation is {type(observation).__name__}, expected object or null"

        if not isinstance(observation.get("headline"), str) or not observation["headline"].strip():
            return "observation.headline missing or empty"
        if not isinstance(observation.get("rationale"), str):
            return "observation.rationale missing or not a string"
        related = observation.get("related_devices")
        if related is not None and not (
            isinstance(related, list) and all(isinstance(x, int) for x in related)
        ):
            return "observation.related_devices must be a list of ints"

        rule = observation.get("proposed_rule")
        if rule is None:
            return None  # observation with no actionable rule, valid

        if not isinstance(rule, dict):
            return f"proposed_rule is {type(rule).__name__}, expected object or null"
        if not isinstance(rule.get("description"), str) or not rule["description"].strip():
            return "proposed_rule.description missing or empty"

        when = rule.get("when")
        if not isinstance(when, dict):
            return "proposed_rule.when missing or not an object"
        if not isinstance(when.get("device_id"), int):
            return "proposed_rule.when.device_id must be an int"
        if not isinstance(when.get("state"), str) or not when["state"]:
            return "proposed_rule.when.state missing or empty"
        if "equals" not in when:
            return "proposed_rule.when.equals is required"

        then = rule.get("then")
        if not isinstance(then, dict):
            return "proposed_rule.then missing or not an object"
        if not isinstance(then.get("device_id"), int):
            return "proposed_rule.then.device_id must be an int"
        op = then.get("op")
        if op not in cls._ALLOWED_RULE_OPS:
            return f"proposed_rule.then.op must be one of {sorted(cls._ALLOWED_RULE_OPS)}, got {op!r}"

        return None

    _JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.DOTALL)

    @classmethod
    def _parse_json(cls, text: str) -> Optional[dict]:
        cleaned = text.strip()
        cleaned = cls._JSON_FENCE.sub("", cleaned).strip()
        # If Claude prefixed text before the JSON, find the first '{' and
        # the matching last '}' by depth scan.
        if not cleaned.startswith("{"):
            start = cleaned.find("{")
            if start < 0:
                return None
            cleaned = cleaned[start:]
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Trim trailing junk after the last balanced '}'. The depth
            # counter must ignore braces that appear inside string
            # literals — otherwise a `}` inside a rule description
            # closes the scan early and we strip valid content.
            depth = 0
            end = -1
            in_string = False
            escape = False
            for i, ch in enumerate(cleaned):
                if escape:
                    escape = False
                    continue
                if in_string:
                    if ch == "\\":
                        escape = True
                    elif ch == "\"":
                        in_string = False
                    continue
                if ch == "\"":
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if end > 0:
                try:
                    return json.loads(cleaned[: end + 1])
                except json.JSONDecodeError:
                    return None
            return None

    def _deliver(
        self, parsed: dict, usage: dict, cost_gbp: float
    ) -> Optional[str]:
        subject = (parsed.get("subject") or "Home Intelligence — weekly digest").strip()
        body_markdown = parsed.get("narrative_markdown") or "(empty digest)"
        observation = parsed.get("observation")

        reply_id: Optional[str] = None
        stored_obs: Optional[dict] = None
        if isinstance(observation, dict) and observation.get("headline"):
            try:
                stored_obs = self.observation_store.add(
                    headline=observation.get("headline", ""),
                    rationale=observation.get("rationale", ""),
                    proposed_rule=observation.get("proposed_rule"),
                    related_devices=observation.get("related_devices", []) or [],
                )
                reply_id = stored_obs["id"]
                body_markdown = self._append_reply_footer(body_markdown, stored_obs)
            except Exception as exc:
                self.logger.exception(f"Failed to persist observation: {exc}")

        # Always append cost to the email — makes the weekly run
        # self-observable without having to check the Indigo log.
        body_markdown = self._append_cost_footer(body_markdown, usage, cost_gbp)

        # Classified send so we can distinguish permanent from transient
        # SMTP failure. On a permanent failure we roll back the
        # observation: next week's digest would otherwise dedup against
        # an observation the user never saw.
        msg_id, error = self.delivery.send_email_with_result(
            subject=subject,
            body_markdown=body_markdown,
            reply_id=reply_id,
        )
        if msg_id is not None:
            return reply_id or "(sent, no observation)"

        if error == "permanent" and stored_obs is not None:
            if self.observation_store.delete(stored_obs["id"]):
                self.logger.warning(
                    f"Rolled back observation {stored_obs['id']} after permanent "
                    "SMTP failure so next digest can re-propose if the pattern "
                    "holds"
                )
            else:
                self.logger.warning(
                    f"Permanent SMTP failure but could not delete observation "
                    f"{stored_obs['id']}; dedup may block re-suggestion"
                )
        elif error == "transient" and stored_obs is not None:
            self.logger.warning(
                f"Transient SMTP failure; observation {stored_obs['id']} "
                "retained (weekly dedup will skip re-proposing it). User won't "
                "see this week's digest unless we retry."
            )
        self.logger.warning(f"Digest email not delivered: error={error}")
        return None

    @staticmethod
    def _append_reply_footer(body: str, observation: dict) -> str:
        obs_id = observation.get("id", "")
        footer_lines = [
            "",
            "---",
            "",
            f"_Observation id: `{obs_id}`_",
            "",
        ]
        if observation.get("proposed_rule"):
            footer_lines.extend(
                [
                    "Reply **YES** to this email if you'd like me to enforce the rule above.",
                    "Reply **NO** to dismiss it. Reply **SNOOZE** to remind me next week.",
                    "",
                ]
            )
        else:
            footer_lines.append(
                "Reply **NO** if this observation isn't useful and I'll stop flagging it."
            )
        return body.rstrip() + "\n" + "\n".join(footer_lines)

    @staticmethod
    def _append_cost_footer(body: str, usage: dict, cost_gbp: float) -> str:
        """Append a single-line cost/usage summary to the digest body.

        Always runs regardless of whether an observation was flagged, so
        "quiet week" digests (observation=null, no reply footer) still
        show the run cost. Helps the user sanity-check monthly spend
        against the configured cap without needing to scan Indigo logs.
        Token figures use thousands separators so they're readable in a
        Markdown body."""
        in_tokens = usage.get("input_tokens", 0)
        out_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        cost_line = (
            f"_Run cost: ~£{cost_gbp:.2f} — "
            f"in {in_tokens:,}, out {out_tokens:,}, "
            f"cache read {cache_read:,}, cache write {cache_write:,}._"
        )
        # Insert a horizontal rule only if the body doesn't already end
        # with one (i.e. there was no reply footer before us).
        stripped = body.rstrip()
        if stripped.endswith("---"):
            return stripped + "\n\n" + cost_line + "\n"
        return stripped + "\n\n---\n\n" + cost_line + "\n"
