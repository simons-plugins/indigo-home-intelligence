"""
Home Intelligence - Indigo plugin.

Produces a weekly digest from SQL Logger history via a Claude model, emails it
out via the domio-push-relay Cloudflare Worker, and enforces agent-proposed
rules the user has approved (Pattern 1: plugin-internal rule engine, Auto
Lights style - because indigo.trigger.create() does not exist).
"""

import json
import traceback
from datetime import datetime, timedelta, timezone

import indigo

from history_db import HistoryDB
from rule_store import RuleStore
from rule_evaluator import RuleEvaluator
from digest import DigestRunner
from delivery import DeliveryClient


DAYS_OF_WEEK = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.debug = pluginPrefs.get("showDebugInfo", False)
        self.history_db = None
        self.rule_store = None
        self.rule_evaluator = None
        self.digest = None
        self.delivery = None
        self._last_eval_at = 0.0
        self._last_digest_date = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def startup(self):
        self.logger.info("Home Intelligence starting up")
        try:
            self._init_history_db()
            self._init_rule_store()
            self.rule_evaluator = RuleEvaluator(self.rule_store, self.logger)
            self.delivery = DeliveryClient(
                worker_url=self.pluginPrefs.get("deliveryWorkerUrl", ""),
                hmac_secret=self.pluginPrefs.get("deliveryHmacSecret", ""),
                logger=self.logger,
            )
            self.digest = DigestRunner(
                history_db=self.history_db,
                rule_store=self.rule_store,
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
        self.logger.warning(f"Disabled {count} agent rule(s). Re-enable individually via the rule store variable.")

    def menuShowStatus(self):
        rules = self.rule_store.list_rules() if self.rule_store else []
        enabled = sum(1 for r in rules if r.get("enabled"))
        self.logger.info(
            "Home Intelligence status: "
            f"{len(rules)} rules ({enabled} enabled), "
            f"digest model={self.pluginPrefs.get('anthropicModel')}, "
            f"next digest day={self.pluginPrefs.get('digestDay')} at {self.pluginPrefs.get('digestTime')}"
        )

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
        """Receive signed feedback from the Worker (inbound email -> rule decision)."""
        try:
            body_props = action.props.get("body_params", indigo.Dict()) or indigo.Dict()
            raw_body = action.props.get("request_body", b"")
            if isinstance(raw_body, bytes):
                raw_body = raw_body.decode("utf-8", errors="replace")
            payload = json.loads(raw_body) if raw_body else dict(body_props)

            signature = (
                action.props.get("headers", indigo.Dict()).get("X-HI-Signature")
                or payload.get("signature")
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

            if intent == "yes" and payload.get("proposed_rule"):
                rule_id = self.rule_store.add_rule(payload["proposed_rule"])
                self.logger.info(f"Created agent rule {rule_id} from YES reply")
                return {"status": "ok", "rule_id": rule_id}
            if intent == "no":
                self.logger.info(f"User declined observation {observation_id}")
                return {"status": "ok"}

            # Free-text query: defer to a future digest-or-ask-Claude path.
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
