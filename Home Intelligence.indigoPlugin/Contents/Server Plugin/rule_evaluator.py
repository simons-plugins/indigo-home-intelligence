"""
Rule evaluator - evaluates agent rules against live Indigo device state.

Called once per evaluator-interval from runConcurrentThread. Fixed-schema
evaluation only (no DSL, no eval). See rule_store.py for the rule schema.

State tracking for the optional `for_minutes` field uses an in-memory
dict keyed by rule id. Lost on plugin restart, which is acceptable:
the rule simply needs another `for_minutes` window to re-fire.

Failure tracking: rules that can't be evaluated (target device or
state_key missing) or whose action fails (indigo.*.turnOff raises)
accumulate consecutive failures in an in-memory counter. After
``AUTO_DISABLE_AFTER_FAILURES`` consecutive failures the rule is
auto-disabled via rule_store.auto_disable(reason=...). Prevents
silent-death (a rule targeting a deleted device) and log-spam (a
rule whose turnOff is failing every 10s).
"""

from datetime import datetime, time, timezone
from typing import Optional

import indigo


# Number of consecutive evaluation / action failures a rule is allowed
# before the evaluator auto-disables it. At the default 10s tick this
# means ~100 seconds of transient breakage before we disable — enough
# to ride out a momentary plugin reload, short enough to not spam the
# event log for hours.
AUTO_DISABLE_AFTER_FAILURES = 10


class RuleEvaluator:
    def __init__(self, rule_store, logger):
        self.rule_store = rule_store
        self.logger = logger
        # rule_id -> datetime the condition first became true
        self._hold_since: dict = {}
        # rule_id -> int consecutive failures (eval miss + action error)
        self._failures: dict = {}

    def tick(self) -> None:
        for rule in self.rule_store.list_rules():
            if not rule.get("enabled"):
                self._hold_since.pop(rule.get("id"), None)
                self._failures.pop(rule.get("id"), None)
                continue
            try:
                self._evaluate_rule(rule)
            except Exception as exc:
                self._record_failure(
                    rule, "action_failed",
                    f"rule {rule.get('id')} raised during evaluation: {exc}"
                )

    def _record_failure(self, rule: dict, reason: str, log_msg: str) -> None:
        """Increment the per-rule failure counter and auto-disable if
        we cross the threshold. Logs once on increment and once on
        disable so the event log isn't silent but also isn't spammed."""
        rule_id = rule.get("id")
        if not rule_id:
            return
        count = self._failures.get(rule_id, 0) + 1
        self._failures[rule_id] = count
        # First-failure log gives immediate visibility; subsequent
        # counter increments stay at debug level to avoid log-spam.
        if count == 1:
            self.logger.warning(log_msg)
        else:
            self.logger.debug(f"{log_msg} (consecutive={count})")
        if count >= AUTO_DISABLE_AFTER_FAILURES:
            if self.rule_store.auto_disable(rule_id, reason):
                self.logger.warning(
                    f"Rule {rule_id} auto-disabled after {count} consecutive "
                    f"failures (reason={reason}). Will surface in next digest."
                )
            self._failures.pop(rule_id, None)
            self._hold_since.pop(rule_id, None)

    def _clear_failure(self, rule_id) -> None:
        """Called when a rule evaluates cleanly. Resets the failure
        counter so transient issues don't accumulate toward auto-disable
        across long quiet periods."""
        if rule_id is not None:
            self._failures.pop(rule_id, None)

    def _evaluate_rule(self, rule: dict) -> None:
        when = rule.get("when") or {}
        device_id = when.get("device_id")
        state_key = when.get("state", "onState")
        expected = when.get("equals")
        rule_id = rule.get("id")

        if device_id is None or device_id not in indigo.devices:
            # Target device missing — count as failure so a renamed or
            # deleted device auto-disables the rule rather than silently
            # becoming inert forever.
            self._record_failure(
                rule, "target_device_missing",
                f"Rule {rule_id}: target device {device_id} not in indigo.devices"
            )
            return

        dev = indigo.devices[device_id]
        # Distinguish "state key exists but wrong value" (normal
        # non-match, clear counter) from "state key doesn't exist at
        # all" (plugin upgrade renamed it, count as failure).
        if state_key in dev.states:
            actual = dev.states[state_key]
        elif hasattr(dev, state_key):
            actual = getattr(dev, state_key)
        else:
            self._record_failure(
                rule, "state_key_missing",
                f"Rule {rule_id}: state '{state_key}' not found on "
                f"device {device_id} ({dev.name}). Plugin upgrade may "
                f"have renamed it."
            )
            return
        if actual != expected:
            # Legitimate non-match — condition just isn't true right
            # now. NOT a failure; clear counter so transient misses
            # during long off-periods don't accumulate.
            self._clear_failure(rule_id)
            self._hold_since.pop(rule_id, None)
            return

        if not self._time_window_matches(when):
            # Outside the time window — legitimate non-match, clear
            # the counter (we don't want transient misses piling up
            # during a rule's off-hours).
            self._clear_failure(rule_id)
            self._hold_since.pop(rule_id, None)
            return

        for_minutes = when.get("for_minutes")
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
        rule_id = rule.get("id")
        if device_id is None or device_id not in indigo.devices:
            self._record_failure(
                rule, "action_device_missing",
                f"Rule {rule_id}: action device {device_id} missing"
            )
            return
        dev = indigo.devices[device_id]
        description = rule.get("description", "(no description)")

        # Wrap the device-action call so a failing turnOff (device
        # unreachable, plugin stopped) counts toward auto-disable.
        # Without this, the outer tick() catch would log every 10s
        # with no recovery mechanism.
        try:
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
                self.logger.warning(f"Rule {rule_id}: unknown op '{op}'")
                return
        except (MemoryError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._record_failure(
                rule, "action_failed",
                f"Rule {rule_id}: {op} on device {device_id} "
                f"({dev.name}) raised: {exc}"
            )
            return

        self._clear_failure(rule_id)
        self.logger.info(f"Agent rule fired: [{rule_id}] {description}")
        self.rule_store.record_fire(rule_id)
