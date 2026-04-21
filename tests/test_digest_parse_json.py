"""Tests for digest.DigestRunner._parse_json — robustness against various
shapes of Claude output."""

from digest import DigestRunner


class TestParseJson:
    def test_clean_json_object(self):
        result = DigestRunner._parse_json('{"a": 1, "b": "two"}')
        assert result == {"a": 1, "b": "two"}

    def test_wrapped_in_json_fence(self):
        result = DigestRunner._parse_json(
            '```json\n{"subject": "Weekly digest", "value": 42}\n```'
        )
        assert result == {"subject": "Weekly digest", "value": 42}

    def test_wrapped_in_bare_fence(self):
        result = DigestRunner._parse_json('```\n{"x": 1}\n```')
        assert result == {"x": 1}

    def test_preamble_before_object(self):
        result = DigestRunner._parse_json(
            "Here is the JSON you asked for:\n\n" '{"ok": true, "count": 3}'
        )
        assert result == {"ok": True, "count": 3}

    def test_trailing_text_after_object(self):
        result = DigestRunner._parse_json(
            '{"observation": {"headline": "ping"}}\n\nLet me know if you need more.'
        )
        assert result == {"observation": {"headline": "ping"}}

    def test_nested_objects_depth_scan_doesnt_close_early(self):
        # Trailing text forces the depth-scan fallback: the initial
        # json.loads fails, find-first-'{' returns the whole payload,
        # then the depth counter must navigate the nested structure
        # without closing early on any inner '}'.
        payload = """{
            "subject": "Weekly",
            "observation": {
                "headline": "a",
                "proposed_rule": {
                    "description": "x",
                    "when": {"device_id": 1, "state": "onState", "equals": true}
                }
            }
        }

        Let me know if you need anything else."""
        result = DigestRunner._parse_json(payload)
        assert result["observation"]["proposed_rule"]["when"]["device_id"] == 1

    def test_json_with_escaped_braces_in_strings_via_fallback(self):
        # Same as test_json_with_escaped_braces_in_strings but with
        # trailing junk so we actually exercise the depth-scan path
        # (not just json.loads on already-valid input).
        payload = '{"x": "contains } brace"}\n\nSome trailing commentary.'
        result = DigestRunner._parse_json(payload)
        assert result == {"x": "contains } brace"}

    def test_malformed_json_returns_none(self):
        # Unclosed string inside an otherwise-braced block
        result = DigestRunner._parse_json('{"unclosed": "string')
        assert result is None

    def test_empty_string_returns_none(self):
        assert DigestRunner._parse_json("") is None

    def test_no_brace_returns_none(self):
        assert DigestRunner._parse_json("just some text without any JSON") is None

    def test_json_with_escaped_braces_in_strings(self):
        # A common depth-scan trap: braces inside string literals must not
        # affect the depth counter. json.loads handles this correctly before
        # the scan fallback fires.
        result = DigestRunner._parse_json('{"x": "contains } brace"}')
        assert result == {"x": "contains } brace"}
