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

from anthropic_client import AnthropicClient, AnthropicError
from data_access import HouseContextAccess
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

HEALTH & ENERGY SIGNALS
The ``event_log_summary`` block carries two additional sub-sections
that summarise the state of the fleet beyond what narrated events
show:

- ``health.low_batteries``: list of devices whose battery is at or
  below the configured threshold (default 20%). Each entry has
  ``{id, name, battery_pct}``.
- ``health.offline_devices``: devices with ``error_state`` set OR
  whose ``hours_offline`` exceeds the configured threshold (default
  24h). Each entry has ``{id, name, error_state, hours_offline}``.
- ``health.low_batteries_total`` / ``health.offline_devices_total``:
  full counts when the per-entry lists are capped at 30.
- ``energy.whole_house``: 7-day vs previous-7-day kWh on the
  configured whole-house meter. Fields:
  ``{device_id, this_week_kwh, last_week_kwh, delta_kwh, delta_pct}``.
  ``delta_pct`` is null when last_week_kwh is zero.
- ``energy.top_consumers``: top 10 individual devices by this-week
  consumption, each with the same WoW fields. Excludes the whole-
  house meter (not double-counting).

How to use them in the narrative:
- Mention any at-threshold items in the opening weekly-status
  paragraph. Batteries <20% and offline >24h are concrete, specific
  items the owner will want to hear about.
- Frame energy changes: "Heating ran a bit lighter this week (-12%)"
  or "Your tumble dryer did 18 kWh this week, up 45% on last week".
- If a single health signal is the most concerning thing this week
  (e.g. a critical sensor is at 5% battery), flag IT as the
  observation rather than a speculative event-log pattern.

EVENT LOG FORMAT
The event log is delivered in two fenced code blocks in the user message:

- ``event_log_summary`` — one compact JSON object with aggregate counts
  (``total_events``, ``top_sources``, ``events_by_hour``,
  ``sql_logger_rollups``, plus the ``health`` and ``energy`` blocks
  described above).
- ``event_log_timeline`` — JSON-lines, chronological, one **positional
  array** per line with the shape
  ``["YYYY-MM-DD HH:MM:SS", source, message]``. Milliseconds are
  omitted. Multi-line tracebacks inside ``message`` appear as escaped
  ``\\n`` inside the string.

AUTO-DISABLED RULES
The plugin's rule evaluator auto-disables a rule when it can't
evaluate it (target device deleted, state key renamed by a plugin
upgrade) or when its action keeps failing. Each such rule carries
``enabled: false`` plus ``auto_disabled: true``,
``auto_disabled_reason``, and ``auto_disabled_at`` in the EXISTING
AGENT RULES block.

When you find auto-disabled rules, mention them in the opening
weekly-status paragraph with the reason — the user accepted them
once and needs to know they've stopped working. Suggested phrasing:
"One rule (`a524f4a5` — coffee machine auto-off) was auto-disabled
on 2026-04-21 because the target device has been removed. Worth
re-creating or deleting if you restructured your plugs." Don't
propose a new rule to replace them in the same digest — raise the
awareness first, let the user decide.

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

RULE TARGETING ALLOWLIST
The `then.device_id` MUST be a "controllable power" device — a dimmer,
relay, or smart plug. Do NOT propose rules that target:
- Thermostats (setpoint changes are high-stakes; user changes manually)
- Security systems, alarm panels, door locks (safety-critical)
- Sensors without a power surface (nothing to switch on/off)
- Cameras, AV receivers, or irrigation controllers (non-standard ops)

Concretely: the target device must expose `brightness` (dimmer) or
`onState` (relay/plug). It must NOT have `temperatureInputs`
(thermostat). Look at the device `type` field in the HOUSE MODEL
block: only `dimmer` and `relay` types are valid targets.

If the pattern you spotted really needs a non-controllable target
(e.g. "when the front door is left open for 20 minutes → set the
thermostat down"), describe the insight in the observation rationale
but set `proposed_rule: null`. The user can then act manually.

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
        context: HouseContextAccess,
        rule_store,
        observation_store,
        delivery,
        api_key: str,
        model: str,
        email_to: str,
        logger,
    ):
        self.context = context
        self.rule_store = rule_store
        self.observation_store = observation_store
        self.delivery = delivery
        self.model = model
        self.email_to = email_to
        self.logger = logger
        self.client = AnthropicClient(api_key=api_key, model=model, logger=logger)
        self.event_log = EventLogReader(logger=logger)
        # Populated by run() after each Claude call; None before the
        # first run. Read by plugin.py to maintain the
        # hi_last_digest_cost_gbp state variable.
        self.last_cost_gbp: Optional[float] = None
        self.last_usage: Optional[dict] = None

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
            house_model = self.context.build_house_model()
            rules = self.rule_store.list_rules()
            prior_observations = self.observation_store.recent_for_prompt()
            events = self.event_log.read_window(days_back=window_days)
            event_summary = self.event_log.summarise(events)
            event_summary["sql_logger_rollups"] = self.context.sql_rollups()
            event_summary["health"] = self.context.fleet_health()
            event_summary["energy"] = self.context.energy_context()
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
        # Expose for the plugin's state-variable refresh after run.
        self.last_cost_gbp = cost_gbp
        self.last_usage = usage
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

        # Soft-warn on narrative-shape drift. A slightly-off-shape digest
        # is still worth delivering — the schema is already enforced
        # above; the shape checks are about prompt adherence, not
        # correctness. Logging at warning keeps drift visible without
        # dropping the week's digest.
        for shape_warning in self._shape_warnings(parsed):
            self.logger.warning(f"Digest shape: {shape_warning}")

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
        # Timestamp slicing: "2026-04-22 07:37:42.510"[:19] -> "2026-04-22 07:37:42"
        # Drops milliseconds (not load-bearing at weekly resolution) but
        # keeps the year — digest windows can cross New Year (e.g. a run
        # on 4 Jan covers 28 Dec - 4 Jan) and a year-less "MM-DD" timestamp
        # would sort wrongly across the boundary.
        timeline_lines = [
            json.dumps(
                [e["timestamp"][:19], e["source"], e["message"]],
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
            "chronological — schema: "
            "[\"YYYY-MM-DD HH:MM:SS\", source, message]; "
            "multi-line tracebacks in the message field survive as "
            "escaped \\n)\n"
            "```event_log_timeline\n"
            f"{timeline_block}\n"
            "```\n\n"
            "Produce this week's digest as a single JSON object matching the schema. "
            "Return JSON only."
        )

    # ------------------------------------------------------------------
    # Output handling
    # ------------------------------------------------------------------

    _ALLOWED_RULE_OPS = {"on", "off", "toggle", "set_brightness"}

    @classmethod
    def _shape_warnings(cls, parsed: dict) -> List[str]:
        """Check the parsed output against the pinned narrative shape in
        INSTRUCTIONS: ``Week of ...`` subject prefix, ``## ``
        weekly-status heading, and (when an observation is flagged)
        both ``### What caught my eye`` and ``### The inference``
        sections.

        Returns a list of warning strings, empty if the shape is fine.
        Non-blocking — the caller logs these but still delivers the
        digest. Blocking validation lives in ``_validate_parsed``."""
        warnings: List[str] = []
        subject = parsed.get("subject", "") or ""
        if not subject.startswith("Week of "):
            warnings.append(
                f"subject missing 'Week of' prefix: {subject[:80]!r}"
            )

        narrative = parsed.get("narrative_markdown", "") or ""
        if "## " not in narrative:
            warnings.append("narrative_markdown missing any '## ' heading")

        observation = parsed.get("observation")
        if isinstance(observation, dict):
            if "### What caught my eye" not in narrative:
                warnings.append(
                    "narrative_markdown missing '### What caught my eye' section"
                )
            if "### The inference" not in narrative:
                warnings.append(
                    "narrative_markdown missing '### The inference' section"
                )
        return warnings

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

        return cls._validate_rule(rule, path="proposed_rule")

    @classmethod
    def _validate_rule(cls, rule: object, path: str = "rule") -> Optional[str]:
        """Validate a rule dict against the fixed schema.

        Pulled out of ``_validate_parsed`` so the MCP write tools
        (propose_rule, add_rule) can apply the same schema check as the
        email-YES flow — keeps validation in one place per ADR-0006.
        Returns None on success, a short error string on failure.

        ``path`` is the key-path prefix used in error messages. Defaults
        to ``"rule"`` for standalone use; ``_validate_parsed`` passes
        ``"proposed_rule"`` to match its existing error strings."""
        if not isinstance(rule, dict):
            return f"{path} is {type(rule).__name__}, expected object"
        if not isinstance(rule.get("description"), str) or not rule["description"].strip():
            return f"{path}.description missing or empty"

        when = rule.get("when")
        if not isinstance(when, dict):
            return f"{path}.when missing or not an object"
        # Bool is a subclass of int in Python — `isinstance(True, int)` is
        # True — so we must explicitly reject bools to prevent a JSON
        # `"device_id": true` sneaking past the gate. `indigo.devices[True]`
        # would blow up far downstream; catch it here.
        when_device = when.get("device_id")
        if not isinstance(when_device, int) or isinstance(when_device, bool):
            return f"{path}.when.device_id must be an int"
        if not isinstance(when.get("state"), str) or not when["state"]:
            return f"{path}.when.state missing or empty"
        if "equals" not in when:
            return f"{path}.when.equals is required"

        then = rule.get("then")
        if not isinstance(then, dict):
            return f"{path}.then missing or not an object"
        then_device = then.get("device_id")
        if not isinstance(then_device, int) or isinstance(then_device, bool):
            return f"{path}.then.device_id must be an int"
        op = then.get("op")
        # op membership test: a list / dict / any unhashable would raise
        # TypeError against a frozenset. Type-check first so the caller
        # gets a clean schema error instead of a 500.
        if not isinstance(op, str) or op not in cls._ALLOWED_RULE_OPS:
            return f"{path}.then.op must be one of {sorted(cls._ALLOWED_RULE_OPS)}, got {op!r}"

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
