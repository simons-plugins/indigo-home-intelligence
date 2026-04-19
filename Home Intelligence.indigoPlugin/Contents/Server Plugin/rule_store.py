"""
Rule store - JSON blob persisted in an Indigo variable.

Schema for a rule (fixed, no DSL, no eval):

    {
        "id": "abc123",                     # short nanoid
        "description": "Turn bedroom light off 30 min after it's on past 23:00",
        "enabled": true,
        "when": {
            "device_id": 12345,
            "state": "onState",
            "equals": true,
            "after_local_time": "23:00",    # optional; 24h HH:MM
            "before_local_time": "06:00",   # optional
            "for_minutes": 30               # optional; hold duration
        },
        "then": {
            "device_id": 12345,
            "op": "off"                     # "on" | "off" | "toggle" | "set_brightness"
            # optional: "value": 50 for set_brightness
        },
        "created_at": "2026-04-19T23:15:00Z",
        "created_by": "agent",              # or "user" for hand-edits
        "fires_count": 0,
        "last_fired_at": null
    }

The fixed schema is deliberate - see CLAUDE.md. No arbitrary expression
evaluation, no eval(), no imported code paths.
"""

import json
import secrets
from datetime import datetime, timezone
from typing import List, Optional

import indigo


class RuleStore:
    def __init__(self, variable_name: str, logger):
        self.variable_name = variable_name
        self.logger = logger

    # ------------------------------------------------------------------
    # Indigo variable plumbing
    # ------------------------------------------------------------------

    def ensure_variable_exists(self) -> None:
        if self.variable_name in indigo.variables:
            return
        try:
            indigo.variable.create(self.variable_name, value="[]")
            self.logger.info(f"Created rule store variable '{self.variable_name}'")
        except Exception as exc:
            self.logger.exception(f"Failed to create rule store variable: {exc}")

    def _read(self) -> list:
        try:
            raw = indigo.variables[self.variable_name].value
            data = json.loads(raw) if raw else []
            if not isinstance(data, list):
                self.logger.warning(
                    f"Rule store variable '{self.variable_name}' is not a JSON array; resetting"
                )
                return []
            return data
        except KeyError:
            self.logger.warning(f"Rule store variable '{self.variable_name}' missing on read")
            return []
        except json.JSONDecodeError as exc:
            self.logger.error(f"Rule store JSON parse failed: {exc}; treating as empty")
            return []

    def _write(self, rules: list) -> None:
        indigo.variable.updateValue(self.variable_name, value=json.dumps(rules, indent=2))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_rules(self) -> List[dict]:
        return self._read()

    def get_rule(self, rule_id: str) -> Optional[dict]:
        for rule in self._read():
            if rule.get("id") == rule_id:
                return rule
        return None

    def add_rule(self, rule: dict) -> str:
        rules = self._read()
        rule = dict(rule)
        rule.setdefault("id", secrets.token_hex(4))
        rule.setdefault("enabled", True)
        rule.setdefault("created_by", "agent")
        rule.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        rule.setdefault("fires_count", 0)
        rule.setdefault("last_fired_at", None)
        rules.append(rule)
        self._write(rules)
        return rule["id"]

    def update_rule(self, rule_id: str, **changes) -> bool:
        rules = self._read()
        for rule in rules:
            if rule.get("id") == rule_id:
                rule.update(changes)
                self._write(rules)
                return True
        return False

    def delete_rule(self, rule_id: str) -> bool:
        rules = self._read()
        kept = [r for r in rules if r.get("id") != rule_id]
        if len(kept) == len(rules):
            return False
        self._write(kept)
        return True

    def disable_all(self) -> int:
        rules = self._read()
        count = 0
        for rule in rules:
            if rule.get("enabled"):
                rule["enabled"] = False
                count += 1
        if count:
            self._write(rules)
        return count

    def record_fire(self, rule_id: str) -> None:
        self.update_rule(
            rule_id,
            fires_count=(self.get_rule(rule_id) or {}).get("fires_count", 0) + 1,
            last_fired_at=datetime.now(timezone.utc).isoformat(),
        )
