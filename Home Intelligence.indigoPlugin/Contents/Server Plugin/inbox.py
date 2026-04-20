"""
Inbox poller - IMAP loop that ingests digest replies.

Per ADR-0002, the Home Intelligence feedback loop polls the user's IMAP
folder every N minutes, parses YES/NO/SNOOZE replies, matches them to
observations by In-Reply-To / References (with an X-HI-Reply-Id header
and a [obs-<id>] subject tag as fallbacks), and POSTs a signed payload
to the plugin's own /feedback endpoint over localhost.

email-reply-parser is used when available to strip quoted content from
threaded replies; falls back to a simple marker-based stripper if the
package isn't installed.
"""

import email
import imaplib
import json
import re
import ssl
import urllib.request
from email.header import decode_header, make_header
from typing import Optional

try:
    from email_reply_parser import EmailReplyParser
    _HAVE_EMAIL_REPLY_PARSER = True
except ImportError:
    EmailReplyParser = None
    _HAVE_EMAIL_REPLY_PARSER = False


_INTENT_PATTERNS = [
    (re.compile(r"^\s*yes\b", re.IGNORECASE), "yes"),
    (re.compile(r"^\s*no\b", re.IGNORECASE), "no"),
    (re.compile(r"^\s*stop\b", re.IGNORECASE), "no"),
    (re.compile(r"^\s*snooze\b", re.IGNORECASE), "snooze"),
]

_REPLY_ID_IN_MESSAGE_ID = re.compile(r"<hi-([A-Za-z0-9_-]+)-")
_REPLY_ID_IN_SUBJECT = re.compile(r"\[obs-([A-Za-z0-9_-]+)\]")

_QUOTED_START_PATTERNS = (
    re.compile(r"^\s*On .+ wrote:\s*$"),
    re.compile(r"^\s*-----\s*Original Message\s*-----"),
    re.compile(r"^\s*From: .+$"),
)


class InboxPoller:
    def __init__(
        self,
        imap_host: str,
        imap_port: int,
        imap_user: str,
        imap_password: str,
        imap_folder: str,
        feedback_url: str,
        delivery_client,
        logger,
        imap_use_ssl: bool = True,
    ):
        self.imap_host = imap_host
        self.imap_port = int(imap_port or (993 if imap_use_ssl else 143))
        self.imap_user = imap_user
        self.imap_password = imap_password
        self.imap_folder = imap_folder or "INBOX"
        self.feedback_url = feedback_url
        self.delivery = delivery_client
        self.logger = logger
        self.imap_use_ssl = imap_use_ssl

    def configured(self) -> bool:
        return bool(
            self.imap_host and self.imap_user and self.imap_password and self.feedback_url
        )

    # ------------------------------------------------------------------
    # Poll entry point
    # ------------------------------------------------------------------

    def poll(self) -> int:
        """Process UNSEEN messages in the configured folder. Returns count processed."""
        if not self.configured():
            return 0
        try:
            conn = self._connect()
        except Exception as exc:
            self.logger.exception(f"IMAP connect failed: {exc}")
            return 0

        count = 0
        try:
            typ, _ = conn.select(self.imap_folder)
            if typ != "OK":
                self.logger.warning(f"IMAP: cannot select folder '{self.imap_folder}'")
                return 0
            typ, data = conn.search(None, "UNSEEN")
            if typ != "OK" or not data or not data[0]:
                return 0
            for uid in data[0].split():
                try:
                    if self._process_message(conn, uid):
                        count += 1
                except Exception as exc:
                    self.logger.exception(f"Processing IMAP message {uid!r} failed: {exc}")
        finally:
            try:
                conn.logout()
            except Exception:
                pass

        if count:
            self.logger.info(f"Inbox poll: processed {count} reply/replies")
        return count

    # ------------------------------------------------------------------
    # Per-message handling
    # ------------------------------------------------------------------

    def _connect(self):
        if self.imap_use_ssl:
            ctx = ssl.create_default_context()
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port, ssl_context=ctx)
        else:
            conn = imaplib.IMAP4(self.imap_host, self.imap_port)
        conn.login(self.imap_user, self.imap_password)
        return conn

    def _process_message(self, conn, uid: bytes) -> bool:
        typ, msg_data = conn.fetch(uid, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            return False
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        reply_id = self._extract_reply_id(msg)
        if not reply_id:
            # Not a digest reply; leave untouched so the user still sees it.
            return False

        body = self._extract_body(msg)
        visible = (
            EmailReplyParser.parse_reply(body)
            if _HAVE_EMAIL_REPLY_PARSER
            else self._strip_quoted_fallback(body)
        )
        visible = (visible or "").strip()
        intent = self._classify_intent(visible)

        subject = self._decode_header_value(msg.get("Subject", ""))
        from_addr = self._decode_header_value(msg.get("From", ""))

        payload = {
            "observation_id": reply_id,
            "intent": intent,
            "body": visible[:2000],
            "subject": subject,
            "from": from_addr,
        }
        ok = self._post_feedback(payload)
        if ok:
            conn.store(uid, "+FLAGS", "\\Seen")
        return ok

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @classmethod
    def _extract_reply_id(cls, msg) -> Optional[str]:
        for header in ("In-Reply-To", "References"):
            value = msg.get(header, "")
            if value:
                match = _REPLY_ID_IN_MESSAGE_ID.search(value)
                if match:
                    return match.group(1)
        x_hdr = msg.get("X-HI-Reply-Id")
        if x_hdr:
            return x_hdr.strip()
        subject = cls._decode_header_value(msg.get("Subject", ""))
        match = _REPLY_ID_IN_SUBJECT.search(subject)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _decode_header_value(value: str) -> str:
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @staticmethod
    def _extract_body(msg) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    return _decode_part(part)
            # No text/plain? fall back to first text/html.
            for part in msg.walk():
                if part.get_content_type() == "text/html" and not part.get_filename():
                    return _decode_part(part)
            return ""
        return _decode_part(msg)

    @staticmethod
    def _strip_quoted_fallback(body: str) -> str:
        lines = []
        for line in body.splitlines():
            if line.startswith(">"):
                break
            if any(pat.match(line) for pat in _QUOTED_START_PATTERNS):
                break
            lines.append(line)
        return "\n".join(lines).strip()

    @staticmethod
    def _classify_intent(text: str) -> str:
        first_line = text.strip().split("\n", 1)[0] if text else ""
        for pat, intent in _INTENT_PATTERNS:
            if pat.match(first_line):
                return intent
        return "query"

    # ------------------------------------------------------------------
    # /feedback POST
    # ------------------------------------------------------------------

    def _post_feedback(self, payload: dict) -> bool:
        signature = self.delivery.sign(payload)
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        req = urllib.request.Request(
            self.feedback_url,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-HI-Signature": signature,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            self.logger.exception(f"Feedback POST failed: {exc}")
            return False


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")
