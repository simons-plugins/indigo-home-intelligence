"""
Weekly digest runner.

Pulls the last N days of SQL Logger rows + Indigo event log, joins them
with the device/trigger/schedule inventory and prior observations, sends
one prompt to Claude, and POSTs the rendered output to the relay for
email delivery.

Stub implementation: the Claude call and prompt assembly are not yet
wired. This stub logs what it *would* do so the rest of the plumbing
(cron tick, HTTP endpoints, rule store) can be exercised end-to-end.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional


class DigestRunner:
    def __init__(
        self,
        history_db,
        rule_store,
        delivery,
        api_key: str,
        model: str,
        email_to: str,
        logger,
    ):
        self.history_db = history_db
        self.rule_store = rule_store
        self.delivery = delivery
        self.api_key = api_key
        self.model = model
        self.email_to = email_to
        self.logger = logger

    def run(self, window_days: int = 7) -> Optional[str]:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=window_days)
        self.logger.info(
            f"Digest window: {since.isoformat()} -> {now.isoformat()} ({window_days}d)"
        )

        if not self.api_key:
            self.logger.warning("No Anthropic API key configured; digest skipped")
            return None
        if not self.email_to:
            self.logger.warning("No digest recipient email configured; digest skipped")
            return None

        # TODO: build the prompt from:
        #   - self.history_db (SQL Logger rows in window)
        #   - Indigo event log lines in window
        #   - device/trigger/schedule inventory
        #   - self.rule_store.list_rules()
        #   - prior digest output (persisted somewhere - plugin data dir)
        # TODO: call Claude with prompt caching on the stable prefix
        # TODO: parse structured output into { headline, narrative, suggestions[] }

        placeholder = (
            f"[Digest stub] Window {window_days}d. "
            f"History backend={getattr(self.history_db, 'db_type', 'unknown')}, "
            f"model={self.model}, rules={len(self.rule_store.list_rules())}"
        )
        self.logger.info(placeholder)

        # TODO: self.delivery.send_email(subject=..., body=..., reply_id=...)
        return placeholder
