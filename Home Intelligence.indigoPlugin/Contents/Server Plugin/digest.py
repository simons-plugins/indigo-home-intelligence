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
    "narrative_markdown": "<digest body as Markdown, 200-600 words, inverted pyramid: headline paragraph, a timeline or observation, inference, closing line>",
    "observation": null | {
      "headline": "<one-line summary of the observation>",
      "rationale": "<1-3 sentences: WHY you're flagging this>",
      "related_devices": [<device_id>, ...],
      "proposed_rule": null | { ...schema above... }
    }
  }

If nothing is worth the owner's attention this week, set `observation`
to null. It's better to say "quiet week, everything looks healthy" than
to invent a concern.

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
        except Exception as exc:
            self.logger.exception(f"Digest context gathering failed: {exc}")
            return None

        system_blocks = self._build_system_blocks(house_model, rules, prior_observations)
        user_message = self._build_user_message(now, since, window_days)

        self.logger.info(
            f"Digest: calling {self.model} "
            f"(devices={len(house_model['devices'])}, rules={len(rules)}, "
            f"prior_obs={len(prior_observations)})"
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

        return self._deliver(parsed)

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_system_blocks(
        self, house_model: dict, rules: List[dict], prior_observations: List[dict]
    ) -> List[dict]:
        context = (
            "HOUSE MODEL\n"
            f"{json.dumps(house_model, indent=2)}\n\n"
            "EXISTING AGENT RULES (already enforced by the plugin)\n"
            f"{json.dumps(rules, indent=2) if rules else '(none yet)'}\n\n"
            "RECENT OBSERVATIONS (past suggestions — avoid repeating)\n"
            f"{json.dumps(prior_observations, indent=2) if prior_observations else '(none yet)'}"
        )
        return [
            {"type": "text", "text": INSTRUCTIONS},
            {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
        ]

    def _build_user_message(self, now: datetime, since: datetime, window_days: int) -> str:
        local_now = now.astimezone()
        return (
            f"Current local time: {local_now.isoformat(timespec='minutes')}\n"
            f"Digest window: last {window_days} days "
            f"({since.date().isoformat()} to {now.date().isoformat()})\n\n"
            "Produce this week's digest as a single JSON object matching the schema. "
            "Return JSON only."
        )

    def _build_house_model(self) -> dict:
        devices = []
        for dev in indigo.devices:
            room = getattr(dev, "folderId", None)
            devices.append(
                {
                    "id": dev.id,
                    "name": dev.name,
                    "type": self._device_type_label(dev),
                    "model": getattr(dev, "model", "") or "",
                    "folder_id": room,
                    "enabled": bool(getattr(dev, "enabled", True)),
                }
            )

        triggers = [
            {"id": t.id, "name": t.name, "enabled": bool(t.enabled)}
            for t in indigo.triggers
        ]
        schedules = [
            {"id": s.id, "name": s.name, "enabled": bool(s.enabled)}
            for s in indigo.schedules
        ]
        action_groups = [
            {"id": a.id, "name": a.name} for a in indigo.actionGroups
        ]
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

    def _deliver(self, parsed: dict) -> Optional[str]:
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
