"""
Observation store - JSON blob persisted in an Indigo variable.

Mirrors rule_store.py: Indigo-variable-backed so the user can inspect
and edit it, fixed schema so no free-form evaluation surface, created
automatically on first run.

Schema for an observation (fixed):

    {
        "id": "a7k3",                        # short nanoid; used as reply_id
        "digest_run_at": "2026-04-20T21:00:00Z",
        "headline": "Bedroom light usually still on past 23:30",
        "rationale": "Over the past 7 days, on 5 of 7 nights the bedroom light ...",
        "proposed_rule": { ... } | null,     # null means informational only
        "related_devices": [12345, 67890],   # for dedup
        "user_response": null | "yes" | "no" | "snooze" | "ignored",
        "responded_at": null | "2026-04-21T08:12:00Z",
        "response_body": null | "<first-line reply, capped at 500 chars>",
        "rule_id": null | "rule-xxxx",       # populated after YES creates the rule
        "expires_at": "2026-06-19T21:00:00Z" # drop from context after this
    }

Observations are never deleted automatically - only filtered out of the
prompt context once they expire. The user can delete individual entries
by editing the Indigo variable.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import indigo


DEFAULT_EXPIRY_DAYS = 60
DEDUP_WINDOW_DAYS = 30


class ObservationStore:
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
            self.logger.info(f"Created observation store variable '{self.variable_name}'")
        except Exception as exc:
            self.logger.exception(f"Failed to create observation store variable: {exc}")

    def _read(self) -> list:
        try:
            raw = indigo.variables[self.variable_name].value
        except KeyError:
            self.logger.warning(
                f"Observation store '{self.variable_name}' missing on read; "
                "ensuring and returning empty"
            )
            self.ensure_variable_exists()
            return []

        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._backup_corrupt(raw, str(exc))
            return []

        if not isinstance(data, list):
            self.logger.warning(
                f"Observation store '{self.variable_name}' is not a JSON array; "
                "backing up and resetting"
            )
            self._backup_corrupt(raw, "value is not a JSON array")
            return []
        return data

    def _backup_corrupt(self, raw_value: str, reason: str) -> None:
        """Preserve a corrupted JSON blob to a sibling variable before any
        subsequent _write overwrites it. Called from _read on parse failure."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_name = f"{self.variable_name}__corrupt_{stamp}"
        try:
            if backup_name not in indigo.variables:
                indigo.variable.create(backup_name, value=raw_value)
                self.logger.error(
                    f"Observation store JSON invalid ({reason}); raw value backed up "
                    f"to variable '{backup_name}'. Store continues with empty state; "
                    "next write will replace the corrupt blob. Inspect the backup "
                    "variable if you need to recover anything."
                )
            else:
                self.logger.error(
                    f"Observation store JSON invalid ({reason}); backup variable "
                    f"'{backup_name}' already exists, preserving existing backup"
                )
        except Exception as exc:
            self.logger.exception(
                f"Observation store corrupt AND backup also failed: {exc}; "
                f"raw value (first 500 chars): {raw_value[:500]!r}"
            )

    def _write(self, observations: list) -> None:
        if self.variable_name not in indigo.variables:
            self.ensure_variable_exists()
        indigo.variable.updateValue(
            self.variable_name, value=json.dumps(observations, indent=2)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_all(self) -> List[dict]:
        return self._read()

    def get(self, observation_id: str) -> Optional[dict]:
        for obs in self._read():
            if obs.get("id") == observation_id:
                return obs
        return None

    def add(
        self,
        headline: str,
        rationale: str,
        proposed_rule: Optional[dict],
        related_devices: Optional[List[int]] = None,
        expiry_days: int = DEFAULT_EXPIRY_DAYS,
    ) -> dict:
        """Write a new observation. Returns the full dict including generated id."""
        now = datetime.now(timezone.utc)
        observation = {
            "id": secrets.token_hex(3),
            "digest_run_at": now.isoformat(),
            "headline": headline,
            "rationale": rationale,
            "proposed_rule": proposed_rule,
            "related_devices": list(related_devices or []),
            "user_response": None,
            "responded_at": None,
            "response_body": None,
            "rule_id": None,
            "expires_at": (now + timedelta(days=expiry_days)).isoformat(),
        }
        existing = self._read()
        existing.append(observation)
        self._write(existing)
        return observation

    def delete(self, observation_id: str) -> bool:
        """Remove an observation by id. Returns True if removed, False if
        not found. Used to roll back persistence on permanent delivery
        failure so dedup doesn't block re-proposing next week."""
        observations = self._read()
        kept = [o for o in observations if o.get("id") != observation_id]
        if len(kept) == len(observations):
            return False
        self._write(kept)
        return True

    def record_response(
        self,
        observation_id: str,
        response: str,
        body: Optional[str] = None,
        rule_id: Optional[str] = None,
    ) -> bool:
        observations = self._read()
        for obs in observations:
            if obs.get("id") == observation_id:
                obs["user_response"] = response
                obs["responded_at"] = datetime.now(timezone.utc).isoformat()
                if body is not None:
                    obs["response_body"] = body[:500]
                if rule_id is not None:
                    obs["rule_id"] = rule_id
                self._write(observations)
                return True
        return False

    def recent_for_prompt(self, max_age_days: int = DEFAULT_EXPIRY_DAYS) -> List[dict]:
        """Return un-expired observations trimmed for prompt use.
        Excludes 'no' responses older than 7 days (we've remembered; stop nagging)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        no_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        out = []
        for obs in self._read():
            created = self._parse_dt(obs.get("digest_run_at"))
            if created and created < cutoff:
                continue
            response = obs.get("user_response")
            responded = self._parse_dt(obs.get("responded_at"))
            if response == "no" and responded and responded < no_cutoff:
                continue
            out.append(
                {
                    "id": obs.get("id"),
                    "digest_run_at": obs.get("digest_run_at"),
                    "headline": obs.get("headline"),
                    "user_response": response,
                    "related_devices": obs.get("related_devices", []),
                    "rule_id": obs.get("rule_id"),
                }
            )
        return out

    def already_suggested(self, related_devices: List[int]) -> bool:
        """True if an unresolved-or-recently-resolved observation touches any of
        these devices within DEDUP_WINDOW_DAYS. Dedup guard to avoid
        re-suggesting the same rule twice."""
        if not related_devices:
            return False
        target = set(related_devices)
        cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_WINDOW_DAYS)
        for obs in self._read():
            created = self._parse_dt(obs.get("digest_run_at"))
            if not created or created < cutoff:
                continue
            if target.intersection(obs.get("related_devices", [])):
                return True
        return False

    @staticmethod
    def _parse_dt(value: Optional[str]):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
