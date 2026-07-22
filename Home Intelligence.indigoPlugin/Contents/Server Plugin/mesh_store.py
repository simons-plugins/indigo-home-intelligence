"""Z-Wave mesh snapshot store — JSON blob in an Indigo variable.

Persists the previous digest's ``{node_address: neighbour_count}``
map so the next weekly run can report week-over-week mesh changes.
Mirrors the rule_store / observation_store Indigo-variable pattern so
the user can inspect the snapshot in the Indigo UI. Unlike those
stores the data here is fully derivable from the live network, so
corrupt or missing JSON just resets to empty — no corrupt-backup
dance."""

import json

import indigo


class MeshSnapshotStore:
    def __init__(self, variable_name: str, logger):
        self.variable_name = variable_name
        self.logger = logger

    def ensure_variable_exists(self) -> None:
        if self.variable_name in indigo.variables:
            return
        try:
            indigo.variable.create(self.variable_name, value="{}")
            self.logger.info(
                f"Created Z-Wave mesh snapshot variable '{self.variable_name}'"
            )
        except Exception as exc:
            self.logger.exception(
                f"Failed to create mesh snapshot variable: {exc}"
            )

    def read(self) -> dict:
        try:
            raw = indigo.variables[self.variable_name].value
        except KeyError:
            self.ensure_variable_exists()
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.logger.warning(
                f"Mesh snapshot variable '{self.variable_name}' held invalid "
                f"JSON ({exc}); resetting to empty"
            )
            return {}
        if not isinstance(data, dict):
            self.logger.warning(
                f"Mesh snapshot variable '{self.variable_name}' is not a JSON "
                "object; resetting to empty"
            )
            return {}
        return data

    def write(self, snapshot: dict) -> None:
        try:
            if self.variable_name not in indigo.variables:
                self.ensure_variable_exists()
            indigo.variable.updateValue(
                self.variable_name,
                value=json.dumps(snapshot, separators=(",", ":")),
            )
        except Exception as exc:
            self.logger.exception(f"Failed to write mesh snapshot: {exc}")
