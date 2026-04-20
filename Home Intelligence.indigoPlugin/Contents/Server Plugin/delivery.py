"""
Delivery client - SMTP digest sender + HMAC helpers for the IWS
/feedback endpoint.

Per workspace ADR-0002 (~/vsCodeProjects/Indigo/docs/adr/0002-*), the
Home Intelligence feedback loop uses the user's own SMTP and IMAP
credentials directly: outbound digest via smtplib here, reply
ingestion via imaplib in inbox.py.

The Message-ID is stamped with the observation's reply_id so replies'
In-Reply-To header threads them back to the correct observation. An
X-HI-Reply-Id header and a [obs-<id>] subject tag act as fallbacks
when a mail client mangles the Message-ID.

The HMAC helpers sign/verify payloads for plugin.py::handle_feedback,
the HTTP entrypoint on Indigo's IWS. The in-process inbox poller
bypasses that endpoint and calls _dispatch_feedback directly, so the
helpers exist purely for external callers (an iMessage plugin, a
webhook, etc.). The secret is auto-generated on first startup and not
user-configurable.

SMTP failure handling distinguishes permanent (auth, bad recipient —
5xx) from transient (network, server disconnect — retriable) so the
caller can decide whether to roll back the observation that was
persisted pre-send.
"""

import email.utils
import hashlib
import hmac
import json
import smtplib
import socket
import ssl
from email.message import EmailMessage
from typing import Optional, Tuple


class DeliveryClient:
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        from_address: str,
        default_to: str,
        hmac_secret: str,
        logger,
        smtp_use_ssl: bool = True,
    ):
        self.smtp_host = smtp_host
        self.smtp_port = int(smtp_port or (465 if smtp_use_ssl else 587))
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_address = from_address or smtp_user
        self.default_to = default_to
        self.hmac_secret = hmac_secret
        self.logger = logger
        self.smtp_use_ssl = smtp_use_ssl

    # ------------------------------------------------------------------
    # Outbound digest email
    # ------------------------------------------------------------------

    def send_email(
        self,
        subject: str,
        body_markdown: str,
        reply_id: Optional[str] = None,
        to: Optional[str] = None,
    ) -> Optional[str]:
        """Send the digest. Returns the Message-ID on success, None on
        any failure (logged, classified by _send_smtp)."""
        msg_id, error = self.send_email_with_result(subject, body_markdown, reply_id, to)
        return msg_id

    def send_email_with_result(
        self,
        subject: str,
        body_markdown: str,
        reply_id: Optional[str] = None,
        to: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Like send_email but returns (message_id_or_None, error_classification).

        error_classification is one of:
            None          success
            'unconfigured'  SMTP creds or recipient missing
            'permanent'   5xx response, SMTPAuthenticationError,
                          SMTPRecipientsRefused, SMTPSenderRefused
                          (retrying won't help)
            'transient'   network blip, timeout, 4xx, SMTPServerDisconnected
                          (a single retry is worth trying)

        Callers who persist state before send (e.g. observation creation)
        can use the error code to decide whether to roll back.
        """
        recipient = to or self.default_to
        if not self._configured():
            self.logger.warning("SMTP not configured; digest email skipped")
            return None, "unconfigured"
        if not recipient:
            self.logger.warning("No digest recipient configured; digest email skipped")
            return None, "unconfigured"

        domain = self._sender_domain()
        msg = EmailMessage()
        msg["Subject"] = self._subject_with_tag(subject, reply_id)
        msg["From"] = self.from_address
        msg["To"] = recipient
        msg["Date"] = email.utils.formatdate(localtime=True)
        if reply_id:
            local = f"hi-{reply_id}-{email.utils.make_msgid().split('<', 1)[1].split('@', 1)[0]}"
            msg["Message-ID"] = f"<{local}@{domain}>"
            msg["X-HI-Reply-Id"] = reply_id
        else:
            msg["Message-ID"] = email.utils.make_msgid(domain=domain)
        msg.set_content(body_markdown)

        error = self._send_smtp(msg, recipient)
        if error is not None:
            return None, error

        message_id = msg["Message-ID"]
        self.logger.info(
            f"Digest sent: to={recipient} reply_id={reply_id} msgid={message_id}"
        )
        return message_id, None

    def _send_smtp(self, msg: EmailMessage, recipient: str) -> Optional[str]:
        """Send `msg`. Returns error classification string on failure, None on success."""
        try:
            if self.smtp_use_ssl:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(
                    self.smtp_host, self.smtp_port, context=ctx, timeout=30
                ) as s:
                    s.login(self.smtp_user, self.smtp_password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(self.smtp_user, self.smtp_password)
                    s.send_message(msg)
            return None
        except smtplib.SMTPAuthenticationError as exc:
            self.logger.error(
                f"SMTP permanent failure (auth) to {recipient}: "
                f"{exc.smtp_code} {exc.smtp_error!r}"
            )
            return "permanent"
        except smtplib.SMTPRecipientsRefused as exc:
            self.logger.error(
                f"SMTP permanent failure (recipient refused) to {recipient}: {exc.recipients!r}"
            )
            return "permanent"
        except smtplib.SMTPSenderRefused as exc:
            self.logger.error(
                f"SMTP permanent failure (sender refused): "
                f"{exc.smtp_code} {exc.smtp_error!r}"
            )
            return "permanent"
        except smtplib.SMTPResponseException as exc:
            # Everything else with a structured SMTP response: 5xx is
            # permanent; anything 4xx is transient per RFC 5321.
            code = exc.smtp_code
            classification = "permanent" if 500 <= code < 600 else "transient"
            self.logger.error(
                f"SMTP {classification} failure to {recipient}: "
                f"{code} {exc.smtp_error!r}"
            )
            return classification
        except (smtplib.SMTPServerDisconnected, socket.timeout, ssl.SSLError, OSError) as exc:
            self.logger.error(f"SMTP transient failure to {recipient}: {type(exc).__name__}: {exc}")
            return "transient"
        except Exception as exc:
            self.logger.exception(f"SMTP send failed (unclassified) to {recipient}: {exc}")
            return "transient"

    # ------------------------------------------------------------------
    # HMAC helpers for /feedback
    # ------------------------------------------------------------------

    def sign(self, payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hmac.new(
            self.hmac_secret.encode("utf-8"), raw, hashlib.sha256
        ).hexdigest()

    def verify_signature(self, payload: dict, signature: Optional[str]) -> bool:
        if not signature or not self.hmac_secret:
            return False
        expected = self.sign(payload)
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    def _sender_domain(self) -> str:
        addr = self.from_address or ""
        return addr.split("@", 1)[1] if "@" in addr else "localhost"

    @staticmethod
    def _subject_with_tag(subject: str, reply_id: Optional[str]) -> str:
        if not reply_id or f"[obs-{reply_id}]" in (subject or ""):
            return subject
        return f"{subject} [obs-{reply_id}]"
