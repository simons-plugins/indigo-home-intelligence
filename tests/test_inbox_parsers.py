"""Tests for inbox.InboxPoller's pure header/body parsers."""

import email

from inbox import InboxPoller


def _msg(**headers) -> email.message.Message:
    """Build a minimal email.Message from header keyword args."""
    msg = email.message.Message()
    for k, v in headers.items():
        # Swap underscores for dashes so keyword args map to valid header
        # names (e.g. in_reply_to → In-Reply-To).
        msg[k.replace("_", "-")] = v
    return msg


class TestExtractReplyId:
    def test_in_reply_to_with_hi_prefix(self):
        msg = _msg(in_reply_to="<hi-abc123-177671@starmerclark.com>")
        assert InboxPoller._extract_reply_id(msg) == "abc123"

    def test_references_with_hi_prefix(self):
        msg = _msg(
            in_reply_to="<unrelated@example.com>",
            references="<unrelated@example.com> <hi-def456-abc@foo.com>",
        )
        assert InboxPoller._extract_reply_id(msg) == "def456"

    def test_x_hi_reply_id_header(self):
        msg = _msg(**{"X-HI-Reply-Id": "xyz789"})
        assert InboxPoller._extract_reply_id(msg) == "xyz789"

    def test_subject_tag(self):
        msg = _msg(subject="Re: Weekly digest [obs-ghi012]")
        assert InboxPoller._extract_reply_id(msg) == "ghi012"

    def test_header_takes_priority_over_subject(self):
        msg = _msg(
            in_reply_to="<hi-fromheader-x@d.com>",
            subject="Re: Weekly digest [obs-fromsubject]",
        )
        assert InboxPoller._extract_reply_id(msg) == "fromheader"

    def test_no_identifiable_fields(self):
        msg = _msg(
            subject="random spam",
            from_="noreply@example.com",
            in_reply_to="<unrelated-thread@example.com>",
        )
        assert InboxPoller._extract_reply_id(msg) is None

    def test_empty_message(self):
        assert InboxPoller._extract_reply_id(_msg()) is None

    def test_rfc2047_encoded_subject(self):
        # =?utf-8?Q?Re:_Weekly_digest_=5Bobs-enc123=5D?=
        # decodes to "Re: Weekly digest [obs-enc123]"
        msg = _msg(
            subject="=?utf-8?Q?Re:_Weekly_digest_=5Bobs-enc123=5D?="
        )
        assert InboxPoller._extract_reply_id(msg) == "enc123"


class TestClassifyIntent:
    def test_yes_lowercase(self):
        assert InboxPoller._classify_intent("yes") == "yes"

    def test_yes_uppercase(self):
        assert InboxPoller._classify_intent("YES") == "yes"

    def test_yes_with_trailing_text(self):
        assert InboxPoller._classify_intent("Yes, please do that") == "yes"

    def test_no(self):
        assert InboxPoller._classify_intent("no thanks") == "no"

    def test_stop_is_no(self):
        # stop is treated as a deny intent per _INTENT_PATTERNS.
        assert InboxPoller._classify_intent("stop this, please") == "no"

    def test_snooze(self):
        assert InboxPoller._classify_intent("snooze until next week") == "snooze"

    def test_free_text_returns_query(self):
        assert (
            InboxPoller._classify_intent("thanks, the digest looks great")
            == "query"
        )

    def test_empty_string(self):
        assert InboxPoller._classify_intent("") == "query"

    def test_whitespace_only(self):
        assert InboxPoller._classify_intent("   \n  ") == "query"

    def test_only_first_line_matters(self):
        # If the first line doesn't match, the rest is ignored — prevents
        # matching YES/NO tokens buried inside a longer thought.
        multiline = "Thanks for the suggestion.\n\nYes, let's do it."
        assert InboxPoller._classify_intent(multiline) == "query"


class TestStripQuotedFallback:
    def test_gmail_style_quote(self):
        body = "Yes please.\n\nOn Mon, 21 Apr 2026, Simon wrote:\n> here was my original"
        assert InboxPoller._strip_quoted_fallback(body).strip() == "Yes please."

    def test_chevron_quote(self):
        body = "Yes.\n> previous message\n> more quoted text"
        assert InboxPoller._strip_quoted_fallback(body).strip() == "Yes."

    def test_outlook_style_quote(self):
        body = "No, cancel it.\n\n-----Original Message-----\nFrom: noreply@..."
        assert (
            InboxPoller._strip_quoted_fallback(body).strip() == "No, cancel it."
        )

    def test_no_quoted_content_returned_unchanged(self):
        body = "Standalone reply with no quoted section."
        assert InboxPoller._strip_quoted_fallback(body) == body

    def test_from_header_style_quote(self):
        body = "snooze\n\nFrom: digest@starmerclark.com"
        assert InboxPoller._strip_quoted_fallback(body).strip() == "snooze"
