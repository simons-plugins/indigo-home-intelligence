"""
Inbox poller - IMAP loop that ingests digest replies.

Per workspace ADR-0002 (~/vsCodeProjects/Indigo/docs/adr/0002-*), the
feedback loop polls the user's IMAP folder every N minutes, parses
YES/NO/SNOOZE replies, matches them to observations by In-Reply-To /
References (with an X-HI-Reply-Id header and a [obs-<id>] subject tag
as fallbacks), and calls a feedback dispatcher callback in the plugin
process. Inbox and handler run in the same Python process, so an HTTP
hop through Indigo's web server would add auth and latency without
any isolation benefit — the callback is direct.

Gmail tokenises SUBJECT at indexing time and won't substring-match
"[obs-", so the primary search is on the In-Reply-To header (which
preserves the "<hi-<replyid>-...@domain>" Message-ID we stamp in
delivery.py). SUBJECT is kept as a fallback for non-Gmail servers
where substring match on the header index works per RFC 3501.

email-reply-parser is used when available to strip quoted content
from threaded replies; falls back to a simple marker-based stripper
if the package isn't installed.

IMAP FETCH uses BODY.PEEK[] rather than RFC822 - a raw RFC822 fetch
implicitly sets the \\Seen flag on the server, which we don't want
until we've actually processed the reply successfully. The \\Seen
flag only flips after feedback_callback returns status in
{ok, accepted}; any earlier failure leaves the message UNSEEN for
the next poll.

poll() raises on connect/login/folder-select failure so the caller
can distinguish "IMAP unreachable" from "no messages to process".
Previously both signalled zero processed, which made the "Poll Inbox
Now" menu indistinguishable from a healthy empty inbox.
"""

import email
import imaplib
import re
import ssl
from email.header import decode_header, make_header
from typing import Callable, Optional

try:
    from email_reply_parser import EmailReplyParser
    _HAVE_EMAIL_REPLY_PARSER = True
except ImportError:
    EmailReplyParser = None
    _HAVE_EMAIL_REPLY_PARSER = False


class InboxPollError(Exception):
    """Raised by InboxPoller.poll on connect/login/folder-select failure.
    Per-message processing failures are logged but not raised."""


_INTENT_PATTERNS = [
    (re.compile(r"^\s*yes\b", re.IGNORECASE), "yes"),
    (re.compile(r"^\s*no\b", re.IGNORECASE), "no"),
    (re.compile(r"^\s*stop\b", re.IGNORECASE), "no"),
    (re.compile(r"^\s*snooze\b", re.IGNORECASE), "snooze"),
]

_REPLY_ID_IN_MESSAGE_ID = re.compile(r"<hi-([A-Za-z0-9_-]+)-")
_REPLY_ID_IN_SUBJECT = re.compile(r"\[obs-([A-Za-z0-9_-]+)\]")

# Hard cap per poll. Narrowed search normally returns <10 results, but a
# misconfigured folder or a buggy server could swamp us otherwise.
MAX_MESSAGES_PER_POLL = 100

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
        feedback_callback: Callable[[dict], dict],
        logger,
        imap_use_ssl: bool = True,
        timeout_sec: int = 30,
    ):
        self.imap_host = imap_host
        self.imap_port = int(imap_port or (993 if imap_use_ssl else 143))
        self.imap_user = imap_user
        self.imap_password = imap_password
        self.imap_folder = imap_folder or "INBOX"
        self.feedback_callback = feedback_callback
        self.logger = logger
        self.imap_use_ssl = imap_use_ssl
        self.timeout_sec = timeout_sec

    def configured(self) -> bool:
        return bool(
            self.imap_host
            and self.imap_user
            and self.imap_password
            and self.feedback_callback is not None
        )

    # ------------------------------------------------------------------
    # Poll entry point
    # ------------------------------------------------------------------

    def poll(self) -> int:
        """Process UNSEEN digest replies in the configured folder.
        Returns count processed.

        Raises InboxPollError on connect/login/folder-select failure so
        the caller can distinguish infrastructure problems from a
        genuinely-empty inbox. Per-message processing errors are caught
        and logged but do not raise — one bad message shouldn't poison
        the rest of the batch.
        """
        if not self.configured():
            return 0
        self.logger.debug(
            f"IMAP: connecting to {self.imap_host}:{self.imap_port} "
            f"(ssl={self.imap_use_ssl}, timeout={self.timeout_sec}s)"
        )
        try:
            conn = self._connect()
        except Exception as exc:
            raise InboxPollError(f"IMAP connect/login failed: {exc}") from exc

        count = 0
        try:
            self.logger.debug(f"IMAP: selecting folder '{self.imap_folder}'")
            typ, _ = conn.select(self.imap_folder)
            if typ != "OK":
                raise InboxPollError(
                    f"IMAP cannot select folder '{self.imap_folder}' (response={typ})"
                )
            # Target replies to our digest by the In-Reply-To header. The
            # digest Message-ID is stamped as "<hi-<replyid>-...@domain>"
            # so any reply in the same thread carries it in In-Reply-To.
            # (Subject search was tried first but Gmail tokenises SUBJECT
            # at indexing time and won't substring-match "[obs-".)
            self.logger.debug("IMAP: searching UNSEEN HEADER In-Reply-To hi-")
            typ, data = conn.search(
                None, "UNSEEN", "HEADER", "In-Reply-To", '"hi-"'
            )
            if typ != "OK" or not data or not data[0]:
                # Fallback: subject tag match for non-Gmail servers or
                # clients that strip threading headers.
                self.logger.debug(
                    'IMAP: no In-Reply-To match; trying SUBJECT "[obs-"'
                )
                typ, data = conn.search(None, "UNSEEN", "SUBJECT", '"[obs-"')
            if typ != "OK" or not data or not data[0]:
                self.logger.debug("IMAP: no matching unseen messages")
                return 0
            uids = data[0].split()
            if len(uids) > MAX_MESSAGES_PER_POLL:
                self.logger.warning(
                    f"IMAP: {len(uids)} matching messages exceeds cap "
                    f"{MAX_MESSAGES_PER_POLL}; processing the newest {MAX_MESSAGES_PER_POLL}"
                )
                uids = uids[-MAX_MESSAGES_PER_POLL:]
            self.logger.info(f"IMAP: {len(uids)} digest reply candidate(s) to examine")
            for uid in uids:
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

        self.logger.info(f"Inbox poll: processed {count} reply/replies")
        return count

    # ------------------------------------------------------------------
    # Per-message handling
    # ------------------------------------------------------------------

    def _connect(self):
        # timeout= on the constructor caps the TCP connect handshake
        # (Python 3.9+). After login, set the socket timeout so every
        # subsequent read (search, fetch, logout) also respects it —
        # imaplib otherwise blocks indefinitely if the server stalls.
        if self.imap_use_ssl:
            ctx = ssl.create_default_context()
            conn = imaplib.IMAP4_SSL(
                self.imap_host,
                self.imap_port,
                ssl_context=ctx,
                timeout=self.timeout_sec,
            )
        else:
            conn = imaplib.IMAP4(
                self.imap_host, self.imap_port, timeout=self.timeout_sec
            )
        conn.login(self.imap_user, self.imap_password)
        try:
            conn.sock.settimeout(self.timeout_sec)
        except Exception:
            pass
        return conn

    def _process_message(self, conn, uid: bytes) -> bool:
        # BODY.PEEK[] fetches the full RFC822 content WITHOUT setting
        # the \Seen flag — critical because we only want to mark the
        # message seen after successful processing.
        typ, msg_data = conn.fetch(uid, "(BODY.PEEK[])")
        if typ != "OK" or not msg_data or not msg_data[0]:
            self.logger.warning(
                f"IMAP FETCH returned typ={typ!r} for uid {uid!r}; skipping"
            )
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

        try:
            result = self.feedback_callback(payload) or {}
        except Exception as exc:
            self.logger.exception(f"Feedback callback raised: {exc}")
            return False

        status = str(result.get("status", "")).lower()
        if status in ("ok", "accepted"):
            conn.store(uid, "+FLAGS", "\\Seen")
            return True
        self.logger.warning(
            f"Feedback dispatcher returned status={status!r} for obs={reply_id}; "
            "leaving message UNSEEN for retry"
        )
        return False

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
