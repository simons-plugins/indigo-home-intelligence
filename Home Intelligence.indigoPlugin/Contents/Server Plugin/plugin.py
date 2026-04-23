"""
Home Intelligence - Indigo plugin.

Produces a weekly digest from SQL Logger history via a Claude model,
sends it over the user's SMTP server, polls their IMAP inbox for
YES/NO replies, and enforces agent-proposed rules the user has
approved (Auto Lights-style plugin-internal rule engine because
indigo.trigger.create() is not in the Indigo Object Model).

Architectural decisions:
  - Workspace ADR-0002 (~/vsCodeProjects/Indigo/docs/adr/0002-*)
    for the SMTP/IMAP email-in/out choice.
  - Repo-local docs/adr/0001-plugin-internal-rule-engine.md for the
    rule-engine-vs-native-triggers choice.
"""

import json
from datetime import datetime, timezone

import indigo

from data_access import HouseContextAccess
from history_db import HistoryDB
from rule_store import RuleStore
from rule_evaluator import RuleEvaluator
from observation_store import ObservationStore
from digest import DigestRunner
from delivery import DeliveryClient
from inbox import InboxPoller, InboxPollError
from mcp_handler import MCPHandler
import mcp_tools


DAYS_OF_WEEK = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Plugin-ID substrings that mark a device as unsafe for agent rule
# targeting — security systems, locks, alarms. A YES reply to an
# observation whose proposed_rule targets one of these is rejected
# with a mailed notice, preventing a rushed approval from disarming
# the house. Matches lowercase pluginId prefix/substring.
UNSAFE_RULE_TARGET_PLUGIN_SUBSTRINGS = (
    "texecom",       # alarm panel
    "securityspy",   # camera server
    "lockitron",     # smart lock
    "schlage",       # smart lock
    "yale",          # smart lock
    "alarm",         # generic
)


def _is_safe_rule_target(device_id) -> bool:
    """Return True if `device_id` is a valid target for an agent rule.

    Safe = controllable power surface (dimmer / relay / smart plug).
    Unsafe = thermostats (setpoints are high-stakes, user changes
    manually), security systems / cameras / locks (safety-critical),
    and devices with no power surface (sensors, irrigation).

    Conservative by design — a rule that fails this check is rejected
    rather than silently accepted. The LLM is instructed to avoid
    proposing such rules; this is the server-side gate."""
    if not isinstance(device_id, int):
        return False
    if device_id not in indigo.devices:
        return False
    dev = indigo.devices[device_id]
    # Thermostats: explicit reject.
    if hasattr(dev, "temperatureInputs"):
        return False
    # Must have a power-switchable surface.
    if not (hasattr(dev, "brightness") or hasattr(dev, "onState")):
        return False
    # Plugin ID denylist for safety-critical categories.
    plugin_id = (getattr(dev, "pluginId", "") or "").lower()
    for needle in UNSAFE_RULE_TARGET_PLUGIN_SUBSTRINGS:
        if needle in plugin_id:
            return False
    return True


def _render_rule_human(rule: dict) -> str:
    """Return a plain-language summary of a rule for confirmation email.

    Resolves device IDs to names via indigo.devices so the user sees
    'Coffee Machine' not just a bare integer. Formats the when/then
    clauses as natural English."""
    when = rule.get("when", {}) or {}
    then = rule.get("then", {}) or {}
    when_dev_id = when.get("device_id")
    then_dev_id = then.get("device_id")

    def _name(did):
        if did in indigo.devices:
            return indigo.devices[did].name
        return f"device {did}"

    parts = [f"When **{_name(when_dev_id)}** (id `{when_dev_id}`) has"]
    state = when.get("state", "onState")
    equals = when.get("equals")
    parts.append(f"`{state}` = `{equals}`")
    after = when.get("after_local_time")
    before = when.get("before_local_time")
    if after and before:
        parts.append(f"between **{after}** and **{before}**")
    elif after:
        parts.append(f"after **{after}**")
    elif before:
        parts.append(f"before **{before}**")
    for_minutes = when.get("for_minutes")
    if for_minutes:
        parts.append(f"for at least **{for_minutes} minutes**")
    parts.append("→")
    op = then.get("op", "?")
    value = then.get("value")
    op_desc = {
        "on": "turn on",
        "off": "turn off",
        "toggle": "toggle",
        "set_brightness": f"set brightness to {value}",
    }.get(op, op)
    parts.append(f"{op_desc} **{_name(then_dev_id)}** (id `{then_dev_id}`).")
    return " ".join(parts)


def _as_bool(value, default: bool = False) -> bool:
    """Coerce an Indigo checkbox pref to a real bool.

    Indigo stores checkbox values as the strings "true" / "false" in
    pluginPrefs, so bool() against them is always True. This helper
    accepts real bools (from code), Indigo strings, common synonyms
    ("yes", "no", "1", "0"), and None.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _as_int(value, default: int, min_value=None, max_value=None) -> int:
    """Coerce an Indigo textfield pref (stored as string) to int.
    Falls back to ``default`` on None / empty / invalid / out-of-range.

    Range bounds are optional but recommended for user-facing prefs —
    a typo like "200" for a battery percentage would otherwise silently
    set an unreachable threshold."""
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if min_value is not None and parsed < min_value:
        return default
    if max_value is not None and parsed > max_value:
        return default
    return parsed


def _as_optional_int(value):
    """Coerce an Indigo textfield pref to an optional int — returns
    None when the pref is blank, rather than a fallback. Used for
    truly-optional settings like the whole-house energy device ID
    where absence is meaningful."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = _as_bool(pluginPrefs.get("showDebugInfo"), False)
        self.history_db = None
        self.rule_store = None
        self.rule_evaluator = None
        self.observation_store = None
        self.digest = None
        self.delivery = None
        self.inbox = None
        self.mcp = None
        self.context = None
        self._last_eval_at = 0.0
        self._last_inbox_poll_at = 0.0
        self._last_digest_date = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def startup(self):
        self.logger.info("Home Intelligence starting up")
        try:
            self._init_history_db()
            self._init_rule_store()
            self._init_observation_store()
            self.rule_evaluator = RuleEvaluator(self.rule_store, self.logger)
            self._rebuild_clients()
            self._refresh_state_variables()
            self.logger.info("Home Intelligence startup complete")
        except Exception as exc:
            self.logger.exception(f"Startup failed: {exc}")

    def shutdown(self):
        self.logger.info("Home Intelligence shutting down")

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        """Rebuild the SMTP/IMAP/Claude clients when prefs change so edits
        take effect without a plugin restart."""
        if userCancelled:
            return
        self.debug = _as_bool(valuesDict.get("showDebugInfo"), False)
        try:
            self._init_history_db()
            self._rebuild_clients()
            self.logger.info("Configuration updated; clients rebuilt")
        except Exception as exc:
            self.logger.exception(f"Rebuild after config change failed: {exc}")

    def _rebuild_clients(self):
        """Construct DeliveryClient, InboxPoller, and DigestRunner from current
        pluginPrefs. Called from startup() and closedPrefsConfigUi()."""
        self.delivery = DeliveryClient(
            smtp_host=self.pluginPrefs.get("smtpHost", ""),
            smtp_port=self.pluginPrefs.get("smtpPort", "465"),
            smtp_user=self.pluginPrefs.get("smtpUser", ""),
            smtp_password=self.pluginPrefs.get("smtpPassword", ""),
            from_address=self.pluginPrefs.get("smtpFromAddress", ""),
            default_to=self.pluginPrefs.get("digestEmailTo", ""),
            smtp_use_ssl=_as_bool(self.pluginPrefs.get("smtpUseSsl"), True),
            logger=self.logger,
        )
        self.inbox = InboxPoller(
            imap_host=self.pluginPrefs.get("imapHost", ""),
            imap_port=self.pluginPrefs.get("imapPort", "993"),
            imap_user=self.pluginPrefs.get("imapUser", ""),
            imap_password=self.pluginPrefs.get("imapPassword", ""),
            imap_folder=self.pluginPrefs.get("imapFolder", "INBOX"),
            feedback_callback=self._dispatch_feedback,
            imap_use_ssl=_as_bool(self.pluginPrefs.get("imapUseSsl"), True),
            logger=self.logger,
        )
        self.context = HouseContextAccess(
            history_db=self.history_db,
            logger=self.logger,
            whole_house_energy_device_id=_as_optional_int(
                self.pluginPrefs.get("wholeHouseEnergyDeviceId")
            ),
            battery_low_threshold=_as_int(
                self.pluginPrefs.get("batteryLowThreshold"),
                20, min_value=0, max_value=100,
            ),
            offline_hours_threshold=_as_int(
                self.pluginPrefs.get("offlineHoursThreshold"),
                24, min_value=0, max_value=168,
            ),
        )
        self.digest = DigestRunner(
            context=self.context,
            rule_store=self.rule_store,
            observation_store=self.observation_store,
            delivery=self.delivery,
            api_key=self.pluginPrefs.get("anthropicApiKey", ""),
            model=self.pluginPrefs.get("anthropicModel", "claude-sonnet-4-6"),
            email_to=self.pluginPrefs.get("digestEmailTo", ""),
            logger=self.logger,
        )
        self.mcp = MCPHandler(
            logger=self.logger,
            server_name="home-intelligence",
            server_version=self.pluginVersion,
        )
        mcp_tools.register_all(
            self.mcp,
            context=self.context,
            rule_store=self.rule_store,
            observation_store=self.observation_store,
            history_db=self.history_db,
            logger=self.logger,
            # Write-tool dependencies. _is_safe_rule_target enforces the
            # ADR-0006 allowlist on every rule-write path; the email
            # helpers reuse the email-YES templates so chat-initiated
            # and email-initiated rules leave an identical audit trail.
            safety_check=_is_safe_rule_target,
            send_confirmation=self._send_rule_confirmation,
            send_rejection=self._send_rule_rejection,
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def runConcurrentThread(self):
        try:
            while True:
                self._tick_rule_evaluator()
                self._tick_inbox_poller()
                self._tick_digest_clock()
                self.sleep(10)
        except self.StopThread:
            pass

    def _tick_rule_evaluator(self):
        if not _as_bool(self.pluginPrefs.get("rulesEnabled"), True):
            return
        interval = int(self.pluginPrefs.get("ruleEvaluatorIntervalSec", 60) or 60)
        now = datetime.now(timezone.utc).timestamp()
        if now - self._last_eval_at < interval:
            return
        self._last_eval_at = now
        try:
            self.rule_evaluator.tick()
        except Exception as exc:
            self.logger.exception(f"Rule evaluator tick failed: {exc}")

    def _tick_inbox_poller(self):
        if not self.inbox or not self.inbox.configured():
            return
        try:
            interval_min = float(self.pluginPrefs.get("inboxPollIntervalMin", 5) or 5)
        except (TypeError, ValueError):
            interval_min = 5.0
        interval_sec = max(30.0, interval_min * 60.0)
        now = datetime.now(timezone.utc).timestamp()
        if now - self._last_inbox_poll_at < interval_sec:
            return
        self._last_inbox_poll_at = now
        try:
            self.inbox.poll()
        except InboxPollError as exc:
            # Infrastructure error (connect/login/select). One log line,
            # no traceback — the message is self-explanatory and keeps
            # the log readable across many repeated polls.
            self.logger.error(f"Inbox poll failed: {exc}")
        except Exception as exc:
            self.logger.exception(f"Inbox poll failed unexpectedly: {exc}")

    def _tick_digest_clock(self):
        target_day = DAYS_OF_WEEK.get(
            self.pluginPrefs.get("digestDay", "sunday"), 6
        )
        target_time = self.pluginPrefs.get("digestTime", "18:00")
        try:
            hh, mm = (int(p) for p in target_time.split(":"))
        except (ValueError, AttributeError):
            self.logger.warning(f"Invalid digestTime '{target_time}', using 18:00")
            hh, mm = 18, 0

        now_local = datetime.now().astimezone()
        if now_local.weekday() != target_day:
            return
        if self._last_digest_date == now_local.date():
            return

        # Fire at target_time OR any later time on the target day: the plugin
        # may have missed the exact minute (restart mid-minute, long
        # runConcurrentThread tick, laptop asleep). We still run, and we log
        # a warning if the catch-up is more than a few minutes late so the
        # operator can see it.
        target_today = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now_local < target_today:
            return

        late_by = (now_local - target_today).total_seconds()
        self._last_digest_date = now_local.date()
        if late_by > 300:
            self.logger.warning(
                f"Firing weekly digest for {now_local.date().isoformat()} "
                f"late by {int(late_by)}s (scheduled {hh:02d}:{mm:02d}, "
                f"ran {now_local.strftime('%H:%M')})"
            )
        else:
            self.logger.info(
                f"Firing weekly digest for {now_local.date().isoformat()}"
            )
        try:
            self.digest.run(window_days=7)
        except Exception as exc:
            self.logger.exception(f"Digest run failed: {exc}")

    # ------------------------------------------------------------------
    # Menu callbacks
    # ------------------------------------------------------------------

    def menuRunDigestNow(self):
        self.logger.info("Menu: Run Digest Now")
        try:
            self.digest.run(window_days=7)
        except Exception as exc:
            self.logger.exception(f"Manual digest failed: {exc}")

    def menuShowAgentRules(self):
        rules = self.rule_store.list_rules()
        if not rules:
            self.logger.info("No agent rules stored.")
            return
        self.logger.info(f"Agent rules ({len(rules)}):")
        for rule in rules:
            if rule.get("auto_disabled"):
                status = (
                    f"AUTO-DISABLED ({rule.get('auto_disabled_reason', '?')}"
                    f" @ {rule.get('auto_disabled_at', '?')})"
                )
            elif rule.get("enabled"):
                status = "enabled"
            else:
                status = "DISABLED"
            fires = rule.get("fires_count", 0)
            last = rule.get("last_fired_at") or "never"
            self.logger.info(
                f"  [{rule.get('id')}] {status} "
                f"(fires={fires}, last={last}): "
                f"{rule.get('description', '(no description)')}"
            )

    def menuManageRuleRuleList(self, filter="", valuesDict=None, typeId="", targetId=0):
        """Populate the 'Rule:' dropdown in the Manage Rule... dialog
        with the current set of agent rules. Indigo invokes this on
        dialog open. Each option is (rule_id, human label)."""
        rules = self.rule_store.list_rules() if self.rule_store else []
        out = []
        for rule in rules:
            rule_id = rule.get("id", "?")
            desc = rule.get("description", "(no description)")
            if rule.get("auto_disabled"):
                marker = " [auto-disabled]"
            elif rule.get("enabled"):
                marker = ""
            else:
                marker = " [disabled]"
            # Truncate long descriptions so the dropdown stays usable.
            short = desc if len(desc) < 60 else desc[:57] + "..."
            out.append((rule_id, f"{rule_id}: {short}{marker}"))
        if not out:
            out.append(("", "(no rules stored)"))
        return out

    def menuManageRule(self, valuesDict, typeId):
        """Apply the user's chosen enable/disable/delete action to a
        specific rule. Invoked by the Manage Rule... menu item's OK
        button."""
        rule_id = valuesDict.get("rule_id", "").strip()
        action = valuesDict.get("action", "").strip()
        if not rule_id:
            errors = indigo.Dict()
            errors["rule_id"] = "Select a rule"
            return False, valuesDict, errors
        rule = self.rule_store.get_rule(rule_id)
        if rule is None:
            errors = indigo.Dict()
            errors["rule_id"] = f"Rule {rule_id!r} no longer exists"
            return False, valuesDict, errors
        desc = rule.get("description", "(no description)")
        if action == "disable":
            self.rule_store.update_rule(rule_id, enabled=False)
            self.logger.info(f"Rule {rule_id} disabled via menu: {desc}")
        elif action == "enable":
            # Enabling also clears auto_disabled metadata so the
            # evaluator starts fresh with a zero failure counter.
            self.rule_store.update_rule(
                rule_id,
                enabled=True,
                auto_disabled=False,
                auto_disabled_reason=None,
                auto_disabled_at=None,
            )
            self.logger.info(f"Rule {rule_id} enabled via menu: {desc}")
        elif action == "delete":
            self.rule_store.delete_rule(rule_id)
            self.logger.warning(f"Rule {rule_id} deleted via menu: {desc}")
        else:
            errors = indigo.Dict()
            errors["action"] = f"Unknown action {action!r}"
            return False, valuesDict, errors
        self._refresh_state_variables()
        return True

    def menuDisableAllRules(self):
        count = self.rule_store.disable_all()
        self.logger.warning(
            f"Disabled {count} agent rule(s). Re-enable individually via the rule store variable."
        )

    def menuShowObservations(self):
        observations = self.observation_store.list_all() if self.observation_store else []
        if not observations:
            self.logger.info("No observations stored.")
            return
        self.logger.info(f"Observations ({len(observations)}):")
        for obs in observations[-10:]:
            response = obs.get("user_response") or "pending"
            has_rule = "+rule" if obs.get("proposed_rule") else ""
            self.logger.info(
                f"  [{obs.get('id')}] ({response}){has_rule} "
                f"{obs.get('headline', '(no headline)')}"
            )

    def menuShowStatus(self):
        rules = self.rule_store.list_rules() if self.rule_store else []
        enabled = sum(1 for r in rules if r.get("enabled"))
        self.logger.info(
            "Home Intelligence status: "
            f"{len(rules)} rules ({enabled} enabled), "
            f"digest model={self.pluginPrefs.get('anthropicModel')}, "
            f"next digest day={self.pluginPrefs.get('digestDay')} at {self.pluginPrefs.get('digestTime')}, "
            f"SMTP={'configured' if self.delivery and self.delivery._configured() else 'NOT configured'}, "
            f"IMAP={'configured' if self.inbox and self.inbox.configured() else 'NOT configured'}"
        )

    def menuPollInboxNow(self):
        self.logger.info("Menu: Poll Inbox Now")
        if not self.inbox or not self.inbox.configured():
            self.logger.warning("IMAP not configured; poll skipped")
            return
        try:
            count = self.inbox.poll()
        except InboxPollError as exc:
            self.logger.error(f"Poll failed: {exc}")
            return
        except Exception as exc:
            self.logger.exception(f"Manual inbox poll failed unexpectedly: {exc}")
            return
        self.logger.info(f"Poll complete: {count} reply/replies processed")

    def menuToggleDebug(self):
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        indigo.server.savePluginPrefs()
        self.logger.info(f"Debug logging {'ON' if self.debug else 'OFF'}")

    # ------------------------------------------------------------------
    # HTTP action handlers (IWS)
    # ------------------------------------------------------------------

    def handle_status(self, action, dev=None, callerWaitingForResult=None):
        """Structured health snapshot of the plugin.

        Returns enough operational detail for:
          - A control-page dashboard to show "next digest at X, N
            pending observations, M rules"
          - MCP tools or scripts to check plugin health
          - The user to visually confirm the plugin is doing something
        Does NOT expose credentials or secret material. Unauthenticated
        endpoint, so keep sensitive fields out of the response."""
        snapshot = {
            "status": "ok",
            "plugin": "home-intelligence",
            "plugin_version": self.pluginVersion,
            "last_digest_date": self._last_digest_date.isoformat()
                if self._last_digest_date else None,
            "scheduled_digest_day": self.pluginPrefs.get("digestDay", "sunday"),
            "scheduled_digest_time": self.pluginPrefs.get("digestTime", "18:00"),
            "last_inbox_poll_at": (
                datetime.fromtimestamp(self._last_inbox_poll_at, tz=timezone.utc).isoformat()
                if self._last_inbox_poll_at else None
            ),
        }
        # Rule and observation counts — source of truth lives in the
        # Indigo variable stores, not cached locally.
        if self.rule_store is not None:
            try:
                rules = self.rule_store.list_rules()
                snapshot["rules_active"] = sum(
                    1 for r in rules if r.get("enabled")
                )
                snapshot["rules_auto_disabled"] = sum(
                    1 for r in rules if r.get("auto_disabled")
                )
                snapshot["rules_total"] = len(rules)
            except Exception as exc:
                snapshot["rules_error"] = str(exc)
        if self.observation_store is not None:
            try:
                obs = self.observation_store.list_all()
                snapshot["observations_total"] = len(obs)
                snapshot["observations_pending_reply"] = sum(
                    1 for o in obs if not o.get("user_response")
                )
            except Exception as exc:
                snapshot["observations_error"] = str(exc)
        return snapshot

    def handle_mcp(self, action, dev=None, callerWaitingForResult=None):
        """IWS entry point for the MCP endpoint at
        ``POST /message/<plugin-id>/mcp``. Extracts HTTP method, headers,
        and body from the incoming action and delegates to MCPHandler,
        which returns the IWS-shaped response dict."""
        if self.mcp is None:
            self.logger.error("MCP endpoint hit before handler ready")
            return {
                "status": 503,
                "headers": {"Content-Type": "application/json"},
                "content": json.dumps({"error": "mcp_not_ready"}),
            }
        http_method = (action.props.get("incoming_request_method") or "POST").upper()
        headers = dict(action.props.get("headers", indigo.Dict()))
        body_raw = action.props.get("request_body", "")
        # IWS hands us bytes for some requests, str for others. Normalise.
        if isinstance(body_raw, (bytes, bytearray)):
            try:
                body = body_raw.decode("utf-8", errors="replace")
            except Exception:
                body = ""
        else:
            body = body_raw or ""
        try:
            return self.mcp.handle_request(http_method, headers, body)
        except Exception as exc:
            self.logger.exception(f"MCP handler raised: {exc}")
            return {
                "status": 500,
                "headers": {"Content-Type": "application/json"},
                "content": json.dumps({"error": str(exc)}),
            }

    def handle_run_digest(self, action, dev=None, callerWaitingForResult=None):
        """Programmatic entrypoint for a digest run. Wraps DigestRunner.run
        so callers outside the plugin (Action Groups, HTTP, scripts) can
        trigger a digest without going through the menu. Returns a minimal
        status dict rather than the full digest output — the real output
        goes out by email."""
        self.logger.info("Action: runDigest")
        if not self.digest:
            self.logger.warning("runDigest called before startup completed")
            return {"status": "error", "detail": "digest_not_ready"}
        try:
            result = self.digest.run(window_days=7)
        except Exception as exc:
            self.logger.exception(f"runDigest failed: {exc}")
            return {"status": "error", "detail": str(exc)}
        # Digest may have added / rolled back observations; refresh
        # the state variables so /status and Indigo variables match.
        self._set_state_var(
            "hi_last_digest_at",
            datetime.now(timezone.utc).isoformat(),
        )
        last_cost = getattr(self.digest, "last_cost_gbp", None)
        if last_cost is not None:
            self._set_state_var("hi_last_digest_cost_gbp", f"{last_cost:.4f}")
        self._refresh_state_variables()
        return {"status": "ok", "reply_id": result}

    def _dispatch_feedback(self, payload: dict) -> dict:
        """Process a decoded feedback payload from the inbox poller.

        Contract: returns a dict with a `status` key. Any status other
        than 'ok' or 'accepted' causes the inbox poller to leave the
        source message UNSEEN for retry — so unknown observations and
        failures should report an error status rather than silently
        succeed.
        """
        observation_id = payload.get("observation_id")
        intent = payload.get("intent")
        body_text = payload.get("body", "")

        self.logger.info(
            f"Feedback received: obs={observation_id} intent={intent} "
            f"body_len={len(body_text)}"
        )

        if not observation_id:
            self.logger.warning("Feedback received with no observation_id; refusing")
            return {"status": "error", "detail": "missing_observation_id"}

        observation = self.observation_store.get(observation_id)
        if observation is None:
            # Leave UNSEEN — either the store is out of sync or the reply
            # matched some unrelated threading ID. The user can re-trigger
            # by marking read/unread if genuinely spurious.
            self.logger.warning(
                f"Feedback references unknown observation {observation_id}; "
                "returning error so inbox poller leaves message UNSEEN"
            )
            return {"status": "error", "detail": "unknown_observation"}

        if intent == "yes":
            # Idempotency guard: if we've already created a rule for this
            # observation (e.g. previous poll succeeded but the IMAP \Seen
            # flag set failed, causing the message to be re-delivered),
            # short-circuit and report the existing rule. No duplicate
            # rules, no second record_response overwrite.
            existing_rule_id = observation.get("rule_id")
            if existing_rule_id:
                self.logger.info(
                    f"Observation {observation_id} already has rule "
                    f"{existing_rule_id}; treating YES as duplicate and "
                    "returning existing rule_id"
                )
                return {"status": "ok", "rule_id": existing_rule_id, "duplicate": True}

            proposed_rule = observation.get("proposed_rule")
            if not proposed_rule:
                self.logger.info(
                    f"YES reply for {observation_id} but no proposed_rule "
                    "attached; recording response only"
                )
                if not self.observation_store.record_response(
                    observation_id, "yes", body=body_text
                ):
                    self.logger.warning(
                        f"record_response failed for {observation_id} (YES, no rule)"
                    )
                return {"status": "ok", "rule_id": None}

            # Safety gate: reject rules targeting thermostats / security
            # / locks / sensors before write. Even if the LLM proposed
            # one (ignored its prompt) we refuse here.
            target_id = (proposed_rule.get("then") or {}).get("device_id")
            if not _is_safe_rule_target(target_id):
                self.logger.warning(
                    f"YES on {observation_id} rejected: target device "
                    f"{target_id} is not a safe rule target "
                    f"(thermostat / security / sensor / missing)"
                )
                self.observation_store.record_response(
                    observation_id, "rejected_unsafe_target", body=body_text
                )
                self._send_rule_rejection(observation, target_id)
                return {
                    "status": "error",
                    "detail": "unsafe_target",
                    "target_device_id": target_id,
                }

            rule_id = self.rule_store.add_rule(proposed_rule)
            if not self.observation_store.record_response(
                observation_id, "yes", body=body_text, rule_id=rule_id
            ):
                self.logger.warning(
                    f"record_response failed for {observation_id} after adding "
                    f"rule {rule_id}; rule is created but observation not updated"
                )
            self.logger.info(
                f"Created agent rule {rule_id} from YES on observation {observation_id}"
            )
            # Confirmation email with the rule's human-readable form.
            # Pure template — no LLM call, pennies of SMTP cost.
            self._send_rule_confirmation(observation, rule_id, proposed_rule)
            self._refresh_state_variables()
            return {"status": "ok", "rule_id": rule_id}

        if intent == "no":
            if not self.observation_store.record_response(
                observation_id, "no", body=body_text
            ):
                self.logger.warning(f"record_response failed for {observation_id} (NO)")
            self.logger.info(f"User declined observation {observation_id}")
            self._refresh_state_variables()
            return {"status": "ok"}

        if intent == "snooze":
            if not self.observation_store.record_response(
                observation_id, "snooze", body=body_text
            ):
                self.logger.warning(
                    f"record_response failed for {observation_id} (SNOOZE)"
                )
            self._refresh_state_variables()
            return {"status": "ok"}

        # Free-text query: defer to a future digest-or-ask-Claude path.
        if not self.observation_store.record_response(
            observation_id, "ignored", body=body_text
        ):
            self.logger.warning(
                f"record_response failed for {observation_id} (free-text query)"
            )
        self.logger.info(
            "Free-text query received (not yet implemented): "
            f"{body_text[:120]!r}"
        )
        return {"status": "accepted", "note": "query path not yet wired"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _send_rule_confirmation(
        self, observation: dict, rule_id: str, rule: dict
    ) -> None:
        """Send a plain-template confirmation email after a YES reply
        turns into a live rule. Pure template, no LLM call. Gives the
        user a human-readable recap of what was written, with enough
        context to disable it (via DISABLE reply or plugin menu)."""
        if self.delivery is None:
            return
        obs_id = observation.get("id", "?")
        headline = observation.get("headline", "Rule created")
        description = rule.get("description", "(no description)")
        body = (
            f"# Rule `{rule_id}` is now active\n\n"
            f"You accepted the observation for:\n\n"
            f"> {headline}\n\n"
            f"## What this rule does\n\n"
            f"**Description:** {description}\n\n"
            f"{_render_rule_human(rule)}\n\n"
            f"## How to remove it\n\n"
            f"- Reply **DISABLE** to this email, or\n"
            f"- Use the Indigo plugin menu: Plugins > Home Intelligence "
            f"> Disable Rule...\n\n"
            f"---\n\n"
            f"_Rule id: `{rule_id}` · Observation id: `{obs_id}`_\n"
            f"_This is a templated confirmation — no LLM was called, "
            f"no API cost._\n"
        )
        try:
            self.delivery.send_email(
                subject=f"Rule `{rule_id}` created from your YES reply",
                body_markdown=body,
                reply_id=obs_id,
            )
        except Exception as exc:
            self.logger.exception(
                f"Rule confirmation email failed for rule {rule_id}: {exc}"
            )

    def _send_rule_rejection(
        self, observation: dict, target_id
    ) -> None:
        """Notify user that their YES reply was refused because the
        proposed rule targets an unsafe device type. Pure template,
        no LLM call."""
        if self.delivery is None:
            return
        obs_id = observation.get("id", "?")
        headline = observation.get("headline", "Proposed rule")
        target_name = "unknown device"
        if isinstance(target_id, int) and target_id in indigo.devices:
            target_name = indigo.devices[target_id].name
        body = (
            f"# Rule rejected — unsafe target\n\n"
            f"You replied YES to the observation:\n\n"
            f"> {headline}\n\n"
            f"The proposed rule targeted **{target_name}** (id "
            f"`{target_id}`), which is on the plugin's allowlist of "
            f"devices the automatic rule engine must not control "
            f"(thermostats, security systems, locks, cameras, "
            f"sensors without a power surface).\n\n"
            f"**No rule has been created.** The observation is marked "
            f"rejected; nothing has changed in your house.\n\n"
            f"If you genuinely want this automation, create a "
            f"standard Indigo trigger or schedule manually — those "
            f"aren't gated.\n\n"
            f"---\n\n"
            f"_Observation id: `{obs_id}`_\n"
        )
        try:
            self.delivery.send_email(
                subject=f"Rule rejected (unsafe target) — obs {obs_id}",
                body_markdown=body,
                reply_id=obs_id,
            )
        except Exception as exc:
            self.logger.exception(
                f"Rule rejection email failed for obs {obs_id}: {exc}"
            )

    # State variables exposed to Indigo so operators can see
    # plugin state without reading logs or calling /status. Maintained
    # idempotently — creation is one-time, updates on every relevant
    # event. Variable names are prefixed ``hi_`` to group them in the
    # Indigo variable list.
    _STATE_VARIABLES = (
        ("hi_active_rules", "0", "Count of rules with enabled=true"),
        ("hi_auto_disabled_rules", "0",
         "Count of rules auto-disabled by the evaluator (target missing/failing)"),
        ("hi_pending_observations", "0",
         "Count of observations awaiting a user reply"),
        ("hi_last_digest_cost_gbp", "0",
         "Claude API cost of the most recent digest run, in GBP"),
        ("hi_last_digest_at", "",
         "ISO timestamp of the most recent digest run"),
    )

    def _ensure_state_variables(self) -> None:
        """Idempotently create the hi_* variables if missing. Called
        on startup; survives plugin restarts without duplicate writes."""
        for name, initial, _description in self._STATE_VARIABLES:
            if name not in indigo.variables:
                try:
                    indigo.variable.create(name, value=initial)
                except Exception as exc:
                    self.logger.warning(
                        f"Failed to create state variable {name!r}: {exc}"
                    )

    def _refresh_state_variables(self) -> None:
        """Update the hi_* variables from current plugin state. Safe
        to call frequently — writes are no-ops when value unchanged.
        Called from startup, after YES/NO feedback, and after each
        digest run."""
        self._ensure_state_variables()
        try:
            if self.rule_store is not None:
                rules = self.rule_store.list_rules()
                self._set_state_var(
                    "hi_active_rules",
                    str(sum(1 for r in rules if r.get("enabled"))),
                )
                self._set_state_var(
                    "hi_auto_disabled_rules",
                    str(sum(1 for r in rules if r.get("auto_disabled"))),
                )
            if self.observation_store is not None:
                obs = self.observation_store.list_all()
                self._set_state_var(
                    "hi_pending_observations",
                    str(sum(1 for o in obs if not o.get("user_response"))),
                )
        except Exception as exc:
            self.logger.warning(f"State variable refresh failed: {exc}")

    @staticmethod
    def _set_state_var(name: str, value: str) -> None:
        """Write only if value differs — avoids redundant change events
        on Indigo's variable-change pipeline."""
        if name not in indigo.variables:
            return
        current = indigo.variables[name].value
        if current != value:
            indigo.variable.updateValue(name, value=value)

    def _init_history_db(self):
        """Initialise the SQL Logger connection and log its status so the
        user can see immediately whether per-device rollups will be
        available in the digest. A failed connection is NOT fatal — the
        digest degrades to event-log-only reasoning — but it should be
        visible at startup, not buried inside a weekly digest run."""
        db_type = self.pluginPrefs.get("dbType", "sqlite")
        if db_type == "postgresql":
            pg_host = self.pluginPrefs.get("pgHost", "127.0.0.1")
            pg_user = self.pluginPrefs.get("pgUser", "postgres")
            pg_database = self.pluginPrefs.get("pgDatabase", "indigo_history")
            self.history_db = HistoryDB(
                db_type="postgresql",
                logger=self.logger,
                pg_host=pg_host,
                pg_port=self.pluginPrefs.get("pgPort", "5432"),
                pg_user=pg_user,
                pg_password=self.pluginPrefs.get("pgPassword", ""),
                pg_database=pg_database,
            )
            target = f"postgresql @ {pg_host}/{pg_database} (user: {pg_user!r})"
        else:
            sqlite_path = self.pluginPrefs.get("sqlitePath", "") or None
            self.history_db = HistoryDB(
                db_type="sqlite",
                logger=self.logger,
                sqlite_path=sqlite_path,
            )
            target = f"sqlite @ {sqlite_path or '(default)'}"

        if self.history_db.test_connection():
            self.logger.info(f"SQL Logger ready: {target}")
            # One-shot startup rollup dry-run — lets us validate the
            # energy rollup query returns sensible counts without
            # firing a digest (each digest costs real money through
            # Claude). Safe to leave in; it's one DB query.
            try:
                ids = self.history_db.discover_energy_tables()
                if ids:
                    rollup = self.history_db.energy_rollup_14d(ids)
                    name_lookup = {dev.id: dev.name for dev in indigo.devices}
                    this_week_only = sorted(
                        did for did, d in rollup.items()
                        if d.get("last_week_kwh") is None
                    )
                    if this_week_only:
                        labelled = [
                            f"{did}={name_lookup.get(did, '?')!r}"
                            for did in this_week_only
                        ]
                        self.logger.info(
                            f"Rollup this-week-only (no 14d baseline, "
                            f"{len(this_week_only)}): {labelled}"
                        )
            except Exception as exc:
                self.logger.warning(f"Rollup dry-run failed: {exc}")
        else:
            self.logger.warning(
                f"SQL Logger unavailable: {target}. Digest will run "
                f"without per-device activity rollups. Fix in Plugin "
                f"Configure... if you want rollups."
            )

    def _init_rule_store(self):
        var_name = self.pluginPrefs.get("ruleStoreVariable", "home_intelligence_rules")
        self.rule_store = RuleStore(variable_name=var_name, logger=self.logger)
        self.rule_store.ensure_variable_exists()

    def _init_observation_store(self):
        var_name = self.pluginPrefs.get(
            "observationStoreVariable", "home_intelligence_observations"
        )
        self.observation_store = ObservationStore(
            variable_name=var_name, logger=self.logger
        )
        self.observation_store.ensure_variable_exists()

