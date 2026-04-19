"""
Rule evaluator - evaluates agent rules against live Indigo device state.

Called once per evaluator-interval from runConcurrentThread. Fixed-schema
evaluation only (no DSL, no eval). See rule_store.py for the rule schema.

State tracking for the optional `for_minutes` field uses an in-memory
dict keyed by rule id. Lost on plugin restart, which is acceptable:
the rule simply needs another `for_minutes` window to re-fire.
"""

from datetime import datetime, time, timezone
from typing import Optional

import indigo


class RuleEvaluator:
    def __init__(self, rule_store, logger):
        self.rule_store = rule_store
        self.logger = logger
        # rule_id -> datetime the condition first became true
        self._hold_since: dict = {}

    def tick(self) -> None:
        for rule in self.rule_store.list_rules():
            if not rule.get("enabled"):
                self._hold_since.pop(rule.get("id"), None)
                continue
            try:
                self._evaluate_rule(rule)
            except Exception as exc:
                self.logger.exception(
                    f"Rule {rule.get('id')} evaluation failed: {exc}"
                )

    def _evaluate_rule(self, rule: dict) -> None:
        when = rule.get("when") or {}
        device_id = when.get("device_id")
        state_key = when.get("state", "onState")
        expected = when.get("equals")

        if device_id is None or device_id not in indigo.devices:
            return

        dev = indigo.devices[device_id]
        actual = dev.states.get(state_key) if state_key in dev.states else getattr(dev, state_key, None)
        if actual != expected:
            self._hold_since.pop(rule.get("id"), None)
            return

        if not self._time_window_matches(when):
            self._hold_since.pop(rule.get("id"), None)
            return

        for_minutes = when.get("for_minutes")
        rule_id = rule["id"]
        now = datetime.now(timezone.utc)

        if for_minutes:
            started = self._hold_since.get(rule_id)
            if started is None:
                self._hold_since[rule_id] = now
                return
            if (now - started).total_seconds() < for_minutes * 60:
                return

        self._fire(rule)
        self._hold_since.pop(rule_id, None)

    def _time_window_matches(self, when: dict) -> bool:
        after = self._parse_hhmm(when.get("after_local_time"))
        before = self._parse_hhmm(when.get("before_local_time"))
        if after is None and before is None:
            return True
        now_t = datetime.now().astimezone().time()
        if after is not None and before is not None:
            if after <= before:
                return after <= now_t <= before
            # Window crosses midnight, e.g. after 23:00 before 06:00
            return now_t >= after or now_t <= before
        if after is not None:
            return now_t >= after
        return now_t <= before

    @staticmethod
    def _parse_hhmm(value) -> Optional[time]:
        if not value:
            return None
        try:
            hh, mm = (int(p) for p in value.split(":"))
            return time(hh, mm)
        except (ValueError, AttributeError):
            return None

    def _fire(self, rule: dict) -> None:
        then = rule.get("then") or {}
        device_id = then.get("device_id")
        op = then.get("op")
        if device_id is None or device_id not in indigo.devices:
            self.logger.warning(f"Rule {rule['id']}: action device {device_id} missing")
            return
        dev = indigo.devices[device_id]
        description = rule.get("description", "(no description)")

        if op == "on":
            if hasattr(dev, "brightness"):
                indigo.dimmer.turnOn(dev.id)
            else:
                indigo.relay.turnOn(dev.id)
        elif op == "off":
            if hasattr(dev, "brightness"):
                indigo.dimmer.turnOff(dev.id)
            else:
                indigo.relay.turnOff(dev.id)
        elif op == "toggle":
            if hasattr(dev, "brightness"):
                indigo.dimmer.toggle(dev.id)
            else:
                indigo.relay.toggle(dev.id)
        elif op == "set_brightness":
            value = int(then.get("value", 50))
            indigo.dimmer.setBrightness(dev.id, value=value)
        else:
            self.logger.warning(f"Rule {rule['id']}: unknown op '{op}'")
            return

        self.logger.info(f"Agent rule fired: [{rule['id']}] {description}")
        self.rule_store.record_fire(rule["id"])
