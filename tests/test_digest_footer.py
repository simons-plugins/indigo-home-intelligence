"""Tests for digest.DigestRunner._append_reply_footer — markdown footer builder."""

from digest import DigestRunner


class TestAppendReplyFooter:
    def test_observation_with_rule_gets_yes_no_snooze_prompt(self):
        observation = {
            "id": "abc123",
            "proposed_rule": {
                "description": "auto-off study lamp after 30 min",
                "when": {"device_id": 1, "state": "onState", "equals": True},
                "then": {"device_id": 1, "op": "off"},
            },
        }
        result = DigestRunner._append_reply_footer("Body text.", observation)
        assert "Body text." in result
        assert "abc123" in result
        assert "YES" in result
        assert "NO" in result
        assert "SNOOZE" in result

    def test_observation_without_rule_gets_no_only(self):
        observation = {"id": "xyz789", "proposed_rule": None}
        result = DigestRunner._append_reply_footer("Body.", observation)
        assert "xyz789" in result
        assert "NO" in result
        # No rule = no YES prompt (nothing to accept).
        assert "YES" not in result

    def test_trailing_whitespace_normalised(self):
        observation = {"id": "a", "proposed_rule": None}
        result = DigestRunner._append_reply_footer("Body.\n\n\n  ", observation)
        # The body is rstripped before the footer is appended, so no
        # runaway whitespace leaks into the final email.
        lines = result.split("\n")
        assert lines[0] == "Body."
