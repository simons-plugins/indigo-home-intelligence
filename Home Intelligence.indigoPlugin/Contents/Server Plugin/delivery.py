"""
Delivery client - HMAC-signed POST to the domio-push-relay Cloudflare Worker.

Uses /email-out for outbound digest delivery and /email-in for inbound
parsed replies. HMAC shared secret matches the relay's
HOME_INTELLIGENCE_HMAC_SECRET env var.

Replay protection: payload includes a nonce + ISO timestamp; relay rejects
timestamps older than 5 minutes.
"""

import hashlib
import hmac
import json
import secrets
import urllib.request
from datetime import datetime, timezone
from typing import Optional


class DeliveryClient:
    def __init__(self, worker_url: str, hmac_secret: str, logger):
        self.worker_url = worker_url.rstrip("/") if worker_url else ""
        self.hmac_secret = hmac_secret
        self.logger = logger

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def send_email(
        self,
        subject: str,
        body_markdown: str,
        reply_id: Optional[str] = None,
        to: Optional[str] = None,
    ) -> bool:
        if not self.worker_url or not self.hmac_secret:
            self.logger.warning("Delivery not configured (worker URL or secret missing)")
            return False

        payload = {
            "subject": subject,
            "body_markdown": body_markdown,
            "reply_id": reply_id,
            "to": to,
            "nonce": secrets.token_hex(8),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = self._sign(raw)

        req = urllib.request.Request(
            f"{self.worker_url}/email-out",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-HI-Signature": signature,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                self.logger.info(
                    f"Email delivery: HTTP {resp.status} (reply_id={reply_id})"
                )
                return 200 <= resp.status < 300
        except Exception as exc:
            self.logger.exception(f"Email delivery failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Inbound verification
    # ------------------------------------------------------------------

    def verify_signature(self, payload: dict, signature: Optional[str]) -> bool:
        if not signature or not self.hmac_secret:
            return False
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        expected = self._sign(raw)
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sign(self, raw: bytes) -> str:
        return hmac.new(
            self.hmac_secret.encode("utf-8"), raw, hashlib.sha256
        ).hexdigest()
