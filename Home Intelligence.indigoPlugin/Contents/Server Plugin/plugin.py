"""
Home Intelligence - Indigo plugin.

Produces a weekly digest from SQL Logger history via a Claude model, sends
it out over the user's SMTP server, polls their IMAP inbox for YES/NO
replies, and enforces agent-proposed rules the user has approved
(Pattern 1: plugin-internal rule engine, Auto Lights style - because
indigo.trigger.create() does not exist).

See ADR-0002 for the email-in/out decision.
"""

import json
import secrets
import traceback
from datetime import datetime, timezone

import indigo

from history_db import HistoryDB
from rule_store import RuleStore
from rule_evaluator import RuleEvaluator
from observation_store import ObservationStore
from digest import DigestRunner
from delivery import DeliveryClient
from inbox import InboxPoller


DAYS_OF_WEEK = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

DEFAULT_FEEDBACK_URL = (
    "http://127.0.0.1:8176/message/com.simons-plugins.home-intelligence/feedback"
)


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = pluginPrefs.get("showDebugInfo", False)
        self.history_db = None
        self.rule_store = None
        self.rule_evaluator = None
        self.observation_store = None
        self.digest = None
        self.delivery = None
        self.inbox = None
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

            hmac_secret = self._ensure_internal_hmac_secret()
            self.delivery = DeliveryClient(
                smtp_host=self.pluginPrefs.get("smtpHost", ""),
                smtp_port=self.pluginPrefs.get("smtpPort", "465"),
                smtp_user=self.pluginPrefs.get("smtpUser", ""),
                smtp_password=self.pluginPrefs.get("smtpPassword", ""),
                from_address=self.pluginPrefs.get("smtpFromAddress", ""),
                default_to=self.pluginPrefs.get("digestEmailTo", ""),
                hmac_secret=hmac_secret,
                smtp_use_ssl=bool(self.pluginPrefs.get("smtpUseSsl", True)),
                logger=self.logger,
            )
            self.inbox = InboxPoller(
                imap_host=self.pluginPrefs.get("imapHost", ""),
                imap_port=self.pluginPrefs.get("imapPort", "993"),
                imap_user=self.pluginPrefs.get("imapUser", ""),
                imap_password=self.pluginPrefs.get("imapPassword", ""),
                imap_folder=self.pluginPrefs.get("imapFolder", "INBOX"),
                feedback_url=self.pluginPrefs.get("feedbackUrl", DEFAULT_FEEDBACK_URL),
                delivery_client=self.delivery,
                imap_use_ssl=bool(self.pluginPrefs.get("imapUseSsl", True)),
                logger=self.logger,
            )
            self.digest = DigestRunner(
                history_db=self.history_db,
                rule_store=self.rule_store,
                observation_store=self.observation_store,
                delivery=self.delivery,
                api_key=self.pluginPrefs.get("anthropicApiKey", ""),
                model=self.pluginPrefs.get("anthropicModel", "claude-sonnet-4-6"),
                email_to=self.pluginPrefs.get("digestEmailTo", ""),
                logger=self.logger,
            )
            self.logger.info("Home Intelligence startup complete")
        except Exception as exc:
            self.logger.exception(f"Startup failed: {exc}")

    def shutdown(self):
        self.logger.info("Home Intelligence shutting down")

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
        if not self.pluginPrefs.get("rulesEnabled", True):
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
        except Exception as exc:
            self.logger.exception(f"Inbox poll failed: {exc}")

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
        if (
            now_local.weekday() == target_day
            and now_local.hour == hh
            and now_local.minute == mm
            and self._last_digest_date != now_local.date()
        ):
            self._last_digest_date = now_local.date()
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
            status = "enabled" if rule.get("enabled") else "DISABLED"
            self.logger.info(
                f"  [{rule.get('id')}] {status}: {rule.get('description', '(no description)')}"
            )

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
            self.logger.info(f"Poll complete: {count} reply/replies processed")
        except Exception as exc:
            self.logger.exception(f"Manual inbox poll failed: {exc}")

    def menuToggleDebug(self):
        self.debug = not self.debug
        self.pluginPrefs["showDebugInfo"] = self.debug
        indigo.server.savePluginPrefs()
        self.logger.info(f"Debug logging {'ON' if self.debug else 'OFF'}")

    # ------------------------------------------------------------------
    # HTTP action handlers (IWS)
    # ------------------------------------------------------------------

    def handle_status(self, action, dev=None, callerWaitingForResult=None):
        return {"status": "ok", "plugin": "home-intelligence"}

    def handle_feedback(self, action, dev=None, callerWaitingForResult=None):
        """Receive signed feedback (from inbox.py, or any other channel posting
        to this endpoint with a matching HMAC signature)."""
        try:
            body_props = action.props.get("body_params", indigo.Dict()) or indigo.Dict()
            raw_body = action.props.get("request_body", b"")
            if isinstance(raw_body, bytes):
                raw_body = raw_body.decode("utf-8", errors="replace")
            payload = json.loads(raw_body) if raw_body else dict(body_props)

            signature = (
                action.props.get("headers", indigo.Dict()).get("X-HI-Signature")
                or payload.pop("signature", None)
            )

            if not self.delivery.verify_signature(payload, signature):
                self.logger.warning("Feedback webhook: invalid signature")
                return {"status": "unauthorized"}

            observation_id = payload.get("observation_id")
            intent = payload.get("intent")  # "yes" | "no" | "query" | "snooze"
            body_text = payload.get("body", "")

            self.logger.info(
                f"Feedback received: obs={observation_id} intent={intent} body_len={len(body_text)}"
            )

            observation = (
                self.observation_store.get(observation_id)
                if observation_id
                else None
            )
            if observation_id and not observation:
                self.logger.warning(
                    f"Feedback references unknown observation {observation_id}"
                )

            if intent == "yes":
                proposed_rule = (observation or {}).get("proposed_rule")
                if not proposed_rule:
                    self.logger.info(
                        f"YES reply for {observation_id} but no proposed_rule "
                        "attached to the observation; recording response only"
                    )
                    self.observation_store.record_response(
                        observation_id, "yes", body=body_text
                    )
                    return {"status": "ok", "rule_id": None}
                rule_id = self.rule_store.add_rule(proposed_rule)
                self.observation_store.record_response(
                    observation_id, "yes", body=body_text, rule_id=rule_id
                )
                self.logger.info(
                    f"Created agent rule {rule_id} from YES on observation {observation_id}"
                )
                return {"status": "ok", "rule_id": rule_id}

            if intent == "no":
                self.observation_store.record_response(
                    observation_id, "no", body=body_text
                )
                self.logger.info(f"User declined observation {observation_id}")
                return {"status": "ok"}

            if intent == "snooze":
                self.observation_store.record_response(
                    observation_id, "snooze", body=body_text
                )
                return {"status": "ok"}

            # Free-text query: defer to a future digest-or-ask-Claude path.
            if observation_id:
                self.observation_store.record_response(
                    observation_id, "ignored", body=body_text
                )
            self.logger.info(
                "Free-text query received (not yet implemented): "
                f"{body_text[:120]!r}"
            )
            return {"status": "accepted", "note": "query path not yet wired"}
        except Exception as exc:
            self.logger.exception(f"handle_feedback failed: {exc}")
            return {"status": "error", "detail": traceback.format_exc(limit=2)}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _init_history_db(self):
        db_type = self.pluginPrefs.get("dbType", "sqlite")
        if db_type == "postgresql":
            self.history_db = HistoryDB(
                db_type="postgresql",
                logger=self.logger,
                pg_host=self.pluginPrefs.get("pgHost", "127.0.0.1"),
                pg_port=self.pluginPrefs.get("pgPort", "5432"),
                pg_user=self.pluginPrefs.get("pgUser", "postgres"),
                pg_password=self.pluginPrefs.get("pgPassword", ""),
                pg_database=self.pluginPrefs.get("pgDatabase", "indigo_history"),
            )
        else:
            self.history_db = HistoryDB(
                db_type="sqlite",
                logger=self.logger,
                sqlite_path=self.pluginPrefs.get("sqlitePath", "") or None,
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

    def _ensure_internal_hmac_secret(self) -> str:
        """Generate the HMAC secret used to authenticate inbox→/feedback POSTs.
        Not user-configurable — it's purely internal. Stored in pluginPrefs
        on first startup so it survives restarts."""
        secret = self.pluginPrefs.get("internalHmacSecret", "")
        if secret:
            return secret
        secret = secrets.token_hex(32)
        self.pluginPrefs["internalHmacSecret"] = secret
        try:
            indigo.server.savePluginPrefs()
        except Exception as exc:
            self.logger.warning(f"Could not persist internal HMAC secret: {exc}")
        self.logger.info("Generated internal HMAC secret for /feedback endpoint")
        return secret
