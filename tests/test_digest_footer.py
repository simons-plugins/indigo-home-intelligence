"""Tests for digest.DigestRunner footer builders — reply prompt + cost."""

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


class TestAppendCostFooter:
    """Run cost is always shown on the email so the user doesn't have
    to check Indigo logs to see weekly spend."""

    _USAGE = {
        "input_tokens": 87961,
        "output_tokens": 402,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 47313,
    }

    def test_cost_line_formatted_in_gbp_with_thousands_separators(self):
        result = DigestRunner._append_cost_footer("Body.", self._USAGE, 0.3534)
        assert "£0.35" in result
        # Token counts use comma thousands separators for readability.
        assert "87,961" in result
        assert "47,313" in result
        assert "402" in result

    def test_inserts_horizontal_rule_when_body_has_none(self):
        """Quiet-week digests have no reply footer, so the cost line
        needs its own '---' separator before it."""
        result = DigestRunner._append_cost_footer("Quiet week.", self._USAGE, 0.35)
        assert "---" in result
        assert result.startswith("Quiet week.")

    def test_no_duplicate_horizontal_rule_when_reply_footer_present(self):
        """If the reply footer already ended with '---' the cost line
        should chain onto it rather than emit a second rule."""
        body_with_reply_footer = (
            "Digest body.\n\n---\n\n"
            "_Observation id: `abc123`_\n\n"
            "Reply **YES** ... SNOOZE ...\n\n---"
        )
        result = DigestRunner._append_cost_footer(
            body_with_reply_footer, self._USAGE, 0.35
        )
        # Only the existing '---' should appear, not a doubled rule.
        assert result.count("\n---\n") == 2  # the one after "Digest body." and the existing trailing one
        assert "\n---\n\n---\n" not in result

    def test_cost_line_runs_even_with_zero_cache(self):
        """First run after a prompt-shape change has cache_read=0 and
        cache_write=0 doesn't happen, but cache_read often does. Verify
        zero values render without crashing."""
        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        result = DigestRunner._append_cost_footer("Body.", usage, 0.01)
        assert "£0.01" in result
        assert "cache read 0" in result
        assert "cache write 0" in result
