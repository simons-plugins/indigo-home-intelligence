"""MCP tool registrations for the Home Intelligence plugin.

Four read-only tools in Phase 1 of PRD-0002. Each is a thin
adapter around the plugin's existing stores / history DB, exposed
via JSON-RPC so Claude Desktop / Claude Code can call them. Tool
output shapes live here (in the ``return`` of each handler); tool
*input* shapes live in the JSON schemas passed to ``register_tool``.

Design note: validation is defensive because tool arguments come
from an LLM. Any ``ValueError`` / ``TypeError`` raised from a
handler is mapped by ``MCPHandler`` into an ``isError: true`` tool
result, which is the MCP 2025-11-25 pattern for "your arguments
were wrong, self-correct and retry".
"""

from typing import Callable, List, Optional

from data_access import HouseContextAccess
from event_log_reader import EventLogReader


DIGEST_INSTRUCTIONS_URI = "home-intelligence:digest_instructions"

# Valid actions for the update_rule tool — matches Manage Rule...
# menu item's action list. Keeping them in one place means both
# surfaces agree on the vocabulary.
_UPDATE_RULE_ACTIONS = ("disable", "enable", "delete")


# Allowed values for query_sql_logger's time_range argument. Matches
# ``history_db.RANGE_BUCKETS`` keys. Keeping the list here (rather
# than importing from history_db) makes it a frozen public contract:
# clients can rely on these values, and adding a new range requires
# conscious tool-schema review.
_ALLOWED_TIME_RANGES = ("1h", "6h", "24h", "7d", "30d")

# Observation status filter vocabulary. Matches the user_response
# values persisted by observation_store, plus "all" and "pending".
_OBSERVATION_STATUS_FILTERS = (
    "all",
    "pending",           # no user_response yet
    "yes",               # user accepted — rule added
    "no",                # user declined
    "snooze",            # user asked to be reminded later
    "ignore",            # plugin auto-dismissed
    "rejected_unsafe_target",
)

# Cap on house_context_snapshot window. Plugin window is weekly (7
# days); allowing up to 30 days supports monthly look-back queries
# without letting Claude run away.
_CONTEXT_MAX_DAYS = 30


def register_all(
    handler,
    *,
    context: HouseContextAccess,
    rule_store,
    observation_store,
    history_db,
    logger,
    safety_check: Optional[Callable[[int], bool]] = None,
    send_confirmation: Optional[Callable[[dict, str, dict], None]] = None,
    send_rejection: Optional[Callable[[dict, int], None]] = None,
    after_write: Optional[Callable[[], None]] = None,
) -> None:
    """Register all read + write tools + resources on the given
    MCPHandler.

    The four optional callables are what the write tools need
    (propose_rule / add_rule / update_rule):

    - ``safety_check(device_id) -> bool`` — mirrors plugin's
      ``_is_safe_rule_target``; returns True only for rule targets
      on the safety allowlist (dimmer / relay / smart plug).
      If omitted, write tools are NOT registered.
    - ``send_confirmation(observation, rule_id, rule)`` — the same
      confirmation-email template used by the email-YES flow.
    - ``send_rejection(observation, target_device_id)`` — rejection
      email used when the safety gate refuses a write.
    - ``after_write()`` — fired after every successful rule mutation
      (add_rule, update_rule). Plugin wires this to
      ``_refresh_state_variables`` so the Indigo state vars
      (``hi_active_rules`` etc.) stay in sync with chat-initiated
      changes the same way they do with menu-initiated ones.
    """
    _register_get_rules(handler, rule_store=rule_store)
    _register_get_observations(handler, observation_store=observation_store)
    _register_query_sql_logger(handler, history_db=history_db, logger=logger)
    _register_house_context_snapshot(
        handler,
        context=context,
        rule_store=rule_store,
        observation_store=observation_store,
        logger=logger,
    )
    _register_digest_instructions_resource(handler)

    if safety_check is not None:
        _register_propose_rule(handler, safety_check=safety_check)
        _register_add_rule(
            handler,
            rule_store=rule_store,
            observation_store=observation_store,
            safety_check=safety_check,
            send_confirmation=send_confirmation,
            send_rejection=send_rejection,
            after_write=after_write,
            logger=logger,
        )
        _register_update_rule(
            handler, rule_store=rule_store,
            after_write=after_write, logger=logger,
        )


def _register_digest_instructions_resource(handler) -> None:
    """Publish the plugin's INSTRUCTIONS text as an MCP resource so
    interactive Claude can opt into the same reasoning guide the
    weekly email uses. Importing here (function scope) avoids pulling
    the digest module into plugin startup if this function is never
    called."""
    from digest import INSTRUCTIONS

    handler.register_resource(
        uri=DIGEST_INSTRUCTIONS_URI,
        name="Home Intelligence digest instructions",
        description=(
            "The reasoning guide the weekly digest runs Claude under — "
            "filtering rules, health/energy signals, event log schema, "
            "auto-disabled rule handling, rule-target safety allowlist, "
            "output format. Fetch this when asked for digest-style "
            "output; otherwise answer directly from the tools."
        ),
        handler=lambda: INSTRUCTIONS,
        mime_type="text/markdown",
    )


# ---------------------------------------------------------------------
# get_rules
# ---------------------------------------------------------------------


def _register_get_rules(handler, *, rule_store) -> None:
    def get_rules(include_disabled: bool = False) -> dict:
        rules: List[dict] = list(rule_store.list_rules())
        if not include_disabled:
            rules = [
                r for r in rules
                if r.get("enabled") and not r.get("auto_disabled")
            ]
        return {
            "total": len(rules),
            "rules": rules,
        }

    handler.register_tool(
        name="get_rules",
        description=(
            "List agent-owned automation rules managed by the Home "
            "Intelligence plugin (separate from Indigo's built-in "
            "triggers). Each rule is a fixed-schema JSON object with "
            "a `when` and `then` clause plus activity metadata "
            "(fires_count, last_fired_at, auto_disabled reason). "
            "Use this instead of trying to compose Indigo triggers "
            "— these rules are private to the plugin."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_disabled": {
                    "type": "boolean",
                    "description": (
                        "Include disabled and auto-disabled rules in "
                        "the result. Default false."
                    ),
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
        handler=get_rules,
    )


# ---------------------------------------------------------------------
# get_observations
# ---------------------------------------------------------------------


def _register_get_observations(handler, *, observation_store) -> None:
    def get_observations(
        status_filter: str = "all",
        days_back: int = 60,
    ) -> dict:
        if status_filter not in _OBSERVATION_STATUS_FILTERS:
            raise ValueError(
                f"status_filter must be one of "
                f"{list(_OBSERVATION_STATUS_FILTERS)}, got {status_filter!r}"
            )
        if not isinstance(days_back, int) or days_back < 1 or days_back > 365:
            raise ValueError(
                f"days_back must be an int in [1, 365], got {days_back!r}"
            )

        all_obs: List[dict] = list(observation_store.list_all())

        # Filter by recency. Observations carry a `digest_run_at` ISO
        # timestamp; anything missing it is treated as "current".
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        recent: List[dict] = []
        for obs in all_obs:
            ts_raw = obs.get("digest_run_at") or ""
            if not ts_raw:
                recent.append(obs)
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    recent.append(obs)
            except ValueError:
                # Malformed timestamps keep the observation in — losing
                # one because we can't parse its date would be worse
                # than including it.
                recent.append(obs)

        if status_filter == "all":
            filtered = recent
        elif status_filter == "pending":
            filtered = [o for o in recent if not o.get("user_response")]
        else:
            filtered = [o for o in recent if o.get("user_response") == status_filter]

        return {
            "total": len(filtered),
            "status_filter": status_filter,
            "days_back": days_back,
            "observations": filtered,
        }

    handler.register_tool(
        name="get_observations",
        description=(
            "List observations the plugin has flagged in prior weekly "
            "digests, with the user's response to each (accepted / "
            "declined / snoozed / pending / rejected-as-unsafe-target). "
            "Use this to answer 'have I already flagged this before?' "
            "and to avoid re-proposing something the user has "
            "declined. Filter by status and recency."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "enum": list(_OBSERVATION_STATUS_FILTERS),
                    "description": (
                        "Filter by user response. `all` returns "
                        "everything in the window; `pending` returns "
                        "observations not yet responded to."
                    ),
                    "default": "all",
                },
                "days_back": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 365,
                    "description": (
                        "How far back in days to include observations. "
                        "Default 60 covers ~8 weekly digests."
                    ),
                    "default": 60,
                },
            },
            "additionalProperties": False,
        },
        handler=get_observations,
    )


# ---------------------------------------------------------------------
# query_sql_logger
# ---------------------------------------------------------------------


def _register_query_sql_logger(handler, *, history_db, logger) -> None:
    def query_sql_logger(
        device_id: int,
        column: str,
        time_range: str = "24h",
    ) -> dict:
        if not isinstance(device_id, int):
            raise ValueError(
                f"device_id must be an integer Indigo ID, got {type(device_id).__name__}"
            )
        if not isinstance(column, str) or not column.strip():
            raise ValueError("column must be a non-empty string (SQL Logger column name)")
        if time_range not in _ALLOWED_TIME_RANGES:
            raise ValueError(
                f"time_range must be one of {list(_ALLOWED_TIME_RANGES)}, "
                f"got {time_range!r}"
            )
        if history_db is None:
            raise ValueError(
                "SQL Logger is not configured. Set up SQL Logger access in the "
                "Home Intelligence plugin config before calling this tool."
            )
        try:
            return history_db.query_history(
                device_id=device_id,
                column=column,
                time_range=time_range,
            )
        except Exception as exc:
            # query_history raises on DB errors (missing table / column).
            # Downgrade to ValueError so the MCP handler emits it as a
            # tool-result error with isError: true rather than a
            # protocol-level -32603 (which would be "internal error,
            # back off"). The failure mode here is "bad input" —
            # device_id not in SQL Logger, or column name doesn't
            # exist for that device — and a self-correcting agent
            # should try a different device/column, not give up.
            logger.info(
                f"query_sql_logger failed: device_id={device_id} "
                f"column={column} range={time_range}: {exc}"
            )
            raise ValueError(f"SQL Logger query failed: {exc}")

    handler.register_tool(
        name="query_sql_logger",
        description=(
            "Query Indigo SQL Logger history for one device + column "
            "over a time range. Returns time-bucketed points plus "
            "min/max/current. Differs from Indigo's built-in history "
            "(which is the event log); SQL Logger is a richer "
            "per-device-column time series. Use list_devices / "
            "search_entities from the general Indigo MCP first to "
            "discover device IDs and column names."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "integer",
                    "description": "Indigo device ID (integer). See list_devices.",
                },
                "column": {
                    "type": "string",
                    "description": (
                        "SQL Logger column name — e.g. `onOffState`, "
                        "`brightness`, `sensorValue`, `accumEnergyTotal`. "
                        "Case-folded internally; exact casing returned "
                        "from DB metadata."
                    ),
                },
                "time_range": {
                    "type": "string",
                    "enum": list(_ALLOWED_TIME_RANGES),
                    "description": (
                        "Pre-defined window + bucket size. `1h` is raw, "
                        "others bucket for readability. Default `24h`."
                    ),
                    "default": "24h",
                },
            },
            "required": ["device_id", "column"],
            "additionalProperties": False,
        },
        handler=query_sql_logger,
    )


# ---------------------------------------------------------------------
# house_context_snapshot
# ---------------------------------------------------------------------


def _register_house_context_snapshot(
    handler,
    *,
    context: HouseContextAccess,
    rule_store,
    observation_store,
    logger,
) -> None:
    def house_context_snapshot(days: int = 7) -> dict:
        if not isinstance(days, int) or days < 1 or days > _CONTEXT_MAX_DAYS:
            raise ValueError(
                f"days must be an int in [1, {_CONTEXT_MAX_DAYS}], got {days!r}"
            )

        # Build event log locally — the context access doesn't wrap
        # EventLogReader (intentionally; it's a small, stateless class
        # and both digest and MCP construct their own).
        event_log = EventLogReader(logger=logger)
        events = event_log.read_window(days_back=days)
        event_summary = event_log.summarise(events)
        event_summary["sql_logger_rollups"] = context.sql_rollups()
        event_summary["health"] = context.fleet_health()
        event_summary["energy"] = context.energy_context()

        house_model = context.build_house_model()
        rules = list(rule_store.list_rules()) if rule_store else []
        observations = (
            list(observation_store.list_all()) if observation_store else []
        )

        return {
            "window_days": days,
            "devices": house_model["devices"],
            "device_folders": house_model["device_folders"],
            "indigo_triggers": house_model["indigo_triggers"],
            "indigo_schedules": house_model["indigo_schedules"],
            "action_groups": house_model["action_groups"],
            "event_log_summary": event_summary,
            "event_log_timeline": [
                {"timestamp": e["timestamp"], "source": e["source"], "message": e["message"]}
                for e in events
            ],
            "rules": rules,
            "observations": observations,
        }

    handler.register_tool(
        name="house_context_snapshot",
        description=(
            "Return the curated whole-house context the weekly digest "
            "reasons over: filtered device inventory, Indigo triggers "
            "/ schedules / action groups, event-log summary + timeline, "
            "SQL Logger rollups, fleet health (low batteries, offline "
            "devices), energy context (whole-house kWh + top consumers), "
            "existing agent rules, and recent observations. EXPENSIVE "
            "(~20k tokens on a 1000-device house) — use sparingly. Prefer "
            "calling individual tools (get_rules, get_observations, "
            "query_sql_logger) when you only need one block."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _CONTEXT_MAX_DAYS,
                    "description": (
                        "Look-back window in days. Default 7 matches "
                        "the weekly digest."
                    ),
                    "default": 7,
                },
            },
            "additionalProperties": False,
        },
        handler=house_context_snapshot,
    )


# ---------------------------------------------------------------------
# propose_rule, add_rule, update_rule (write surface)
# ---------------------------------------------------------------------
#
# All three write tools share the validation path from
# digest.DigestRunner._validate_rule. Importing inside the register
# functions (rather than at module top) keeps mcp_tools loadable in
# tests that don't touch the digest module.


# Rule schema fragment — reused across tool input schemas so the
# write tools present the same shape to Claude that the email flow
# persists. Keeping this as a dict (not a full JSON Schema $ref) is
# deliberate: Claude Desktop's tool-schema validation is permissive,
# and adding a top-level $defs block would be more ceremony than
# value at four tools.
_RULE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "Human summary of what the rule does.",
        },
        "when": {
            "type": "object",
            "properties": {
                "device_id": {"type": "integer"},
                "state": {"type": "string", "description": "e.g. onState, brightness"},
                "equals": {"description": "Match value — true/false/int/string"},
                "after_local_time": {"type": "string", "description": "HH:MM"},
                "before_local_time": {"type": "string", "description": "HH:MM"},
                "for_minutes": {"type": "integer", "minimum": 1},
            },
            "required": ["device_id", "state", "equals"],
        },
        "then": {
            "type": "object",
            "properties": {
                "device_id": {"type": "integer"},
                "op": {
                    "type": "string",
                    "enum": ["on", "off", "toggle", "set_brightness"],
                },
                "value": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["device_id", "op"],
        },
    },
    "required": ["description", "when", "then"],
}


def _register_propose_rule(handler, *, safety_check) -> None:
    def propose_rule(rule: dict) -> dict:
        from digest import DigestRunner

        err = DigestRunner._validate_rule(rule)
        if err:
            return {"ok": False, "stage": "schema", "error": err}

        target_id = (rule.get("then") or {}).get("device_id")
        if not safety_check(target_id):
            return {
                "ok": False,
                "stage": "safety",
                "error": "target device is refused by the safety allowlist",
                "target_device_id": target_id,
                "allowlist_rationale": (
                    "Safe rule targets are dimmers, relays, or smart plugs. "
                    "Thermostats, security systems, locks, cameras, and "
                    "sensors without a power surface are refused — propose "
                    "the user create those as standard Indigo triggers "
                    "themselves, not via the plugin rule engine."
                ),
            }

        # Pull in the human renderer only when useful — avoids an
        # import of plugin.py for callers that only validate.
        from plugin import _render_rule_human

        return {
            "ok": True,
            "stage": "validated",
            "preview": _render_rule_human(rule),
        }

    handler.register_tool(
        name="propose_rule",
        description=(
            "Validate a proposed rule against the plugin's fixed schema "
            "AND the safety allowlist, without writing it. Use this to "
            "show the user what a rule would look like before committing. "
            "Returns `ok: true` with a human-readable preview on success, "
            "or `ok: false` with the specific failure and stage "
            "(schema / safety). Call `add_rule` afterwards to persist "
            "if the user agrees."
        ),
        input_schema={
            "type": "object",
            "properties": {"rule": _RULE_INPUT_SCHEMA},
            "required": ["rule"],
            "additionalProperties": False,
        },
        handler=propose_rule,
    )


def _register_add_rule(
    handler,
    *,
    rule_store,
    observation_store,
    safety_check,
    send_confirmation,
    send_rejection,
    after_write,
    logger,
) -> None:
    def add_rule(rule: dict, from_observation_id: Optional[str] = None) -> dict:
        from digest import DigestRunner

        err = DigestRunner._validate_rule(rule)
        if err:
            raise ValueError(err)

        target_id = (rule.get("then") or {}).get("device_id")

        # Observation linkage is optional. If present, look it up now
        # so both the rejection and confirmation paths can use it.
        observation = None
        if from_observation_id:
            observation = observation_store.get(from_observation_id)
            if observation is None:
                raise ValueError(
                    f"from_observation_id {from_observation_id!r} not found"
                )

        if not safety_check(target_id):
            logger.warning(
                f"add_rule via MCP rejected: target device {target_id} "
                f"fails safety allowlist"
            )
            if observation is not None:
                observation_store.record_response(
                    from_observation_id, "rejected_unsafe_target", body=""
                )
                if send_rejection is not None:
                    try:
                        send_rejection(observation, target_id)
                    except Exception as exc:
                        logger.exception(
                            f"send_rejection email failed: {exc}"
                        )
            return {
                "ok": False,
                "stage": "safety",
                "error": "target device is refused by the safety allowlist",
                "target_device_id": target_id,
            }

        rule_id = rule_store.add_rule(rule)
        logger.info(f"MCP add_rule created agent rule {rule_id}")

        if observation is not None:
            observation_store.record_response(
                from_observation_id, "yes", body="", rule_id=rule_id
            )

        # Confirmation email: always attempted (best-effort). Gives
        # the user an audit trail for chat-initiated rules the same
        # way email-YES does. Synthesise a minimal observation shell
        # when we have no linked observation so the same template
        # works for both paths.
        if send_confirmation is not None:
            shell = observation or {
                "id": f"mcp-{rule_id}",
                "headline": rule.get("description", "Rule added via MCP"),
            }
            try:
                send_confirmation(shell, rule_id, rule)
            except Exception as exc:
                logger.exception(f"send_confirmation email failed: {exc}")

        # Keep the plugin's Indigo state variables in sync with the
        # rule store — menu path calls the same refresh hook after
        # mutations. Best-effort; a failure here doesn't invalidate
        # the rule itself.
        if after_write is not None:
            try:
                after_write()
            except Exception as exc:
                logger.exception(f"after_write hook failed: {exc}")

        return {"ok": True, "rule_id": rule_id}

    handler.register_tool(
        name="add_rule",
        description=(
            "Persist a rule to the plugin's rule store. ALWAYS call "
            "`propose_rule` first and show the preview to the user; only "
            "call this after they confirm. Enforces the same safety "
            "allowlist as the email-YES path — thermostats, security "
            "systems, locks, cameras, and no-power-surface sensors are "
            "refused server-side. On success sends the same "
            "confirmation email as the Sunday-digest flow. If "
            "`from_observation_id` is supplied the observation's "
            "user_response is set to `yes` with the new rule_id."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "rule": _RULE_INPUT_SCHEMA,
                "from_observation_id": {
                    "type": "string",
                    "description": (
                        "Optional observation id this rule is "
                        "accepting — updates the observation's "
                        "user_response. Leave unset for chat-initiated "
                        "rules."
                    ),
                },
            },
            "required": ["rule"],
            "additionalProperties": False,
        },
        handler=add_rule,
    )


def _register_update_rule(handler, *, rule_store, after_write, logger) -> None:
    def update_rule(rule_id: str, action: str) -> dict:
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError("rule_id must be a non-empty string")
        if action not in _UPDATE_RULE_ACTIONS:
            raise ValueError(
                f"action must be one of {list(_UPDATE_RULE_ACTIONS)}, got {action!r}"
            )

        existing = rule_store.get_rule(rule_id)
        if existing is None:
            raise ValueError(f"rule {rule_id!r} not found")

        if action == "disable":
            rule_store.update_rule(rule_id, enabled=False)
        elif action == "enable":
            # Mirrors Manage Rule... menu: enabling clears
            # auto_disabled metadata so the evaluator starts fresh.
            rule_store.update_rule(
                rule_id,
                enabled=True,
                auto_disabled=False,
                auto_disabled_reason=None,
                auto_disabled_at=None,
            )
        elif action == "delete":
            rule_store.delete_rule(rule_id)

        logger.info(f"MCP update_rule: {action} on {rule_id}")
        if after_write is not None:
            try:
                after_write()
            except Exception as exc:
                logger.exception(f"after_write hook failed: {exc}")
        return {"ok": True, "rule_id": rule_id, "action": action}

    handler.register_tool(
        name="update_rule",
        description=(
            "Disable, enable, or delete an existing rule. Use this to "
            "give the user control when they ask to turn off a noisy "
            "rule mid-week without waiting for the next digest. "
            "`enable` also clears auto-disabled metadata so the "
            "evaluator's failure counter resets. `delete` is "
            "permanent."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "rule_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": list(_UPDATE_RULE_ACTIONS),
                },
            },
            "required": ["rule_id", "action"],
            "additionalProperties": False,
        },
        handler=update_rule,
    )
