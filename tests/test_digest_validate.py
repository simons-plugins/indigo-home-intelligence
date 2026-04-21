"""Tests for digest.DigestRunner._validate_parsed — schema guard against
Claude returning malformed output."""

from digest import DigestRunner


class TestValidateParsed:
    def _valid_with_rule(self):
        """Helper: a fully-valid shape including a proposed_rule."""
        return {
            "subject": "Weekly digest 2026-04-20",
            "narrative_markdown": "# Digest\n\nAll quiet this week.",
            "observation": {
                "headline": "Consider auto-off for study lamp",
                "rationale": "Left on overnight 3 out of 7 nights.",
                "related_devices": [12345],
                "proposed_rule": {
                    "description": "Turn off study lamp after 30 min of no motion post-23:00",
                    "when": {
                        "device_id": 12345,
                        "state": "onState",
                        "equals": True,
                        "after_local_time": "23:00",
                        "for_minutes": 30,
                    },
                    "then": {"device_id": 12345, "op": "off"},
                },
            },
        }

    def test_valid_full_shape(self):
        assert DigestRunner._validate_parsed(self._valid_with_rule()) is None

    def test_observation_null_is_valid(self):
        payload = self._valid_with_rule()
        payload["observation"] = None
        assert DigestRunner._validate_parsed(payload) is None

    def test_observation_with_no_proposed_rule_is_valid(self):
        payload = self._valid_with_rule()
        payload["observation"]["proposed_rule"] = None
        assert DigestRunner._validate_parsed(payload) is None

    def test_missing_subject(self):
        payload = self._valid_with_rule()
        del payload["subject"]
        error = DigestRunner._validate_parsed(payload)
        assert error is not None
        assert "subject" in error

    def test_empty_subject(self):
        payload = self._valid_with_rule()
        payload["subject"] = "   "
        error = DigestRunner._validate_parsed(payload)
        assert error is not None
        assert "subject" in error

    def test_missing_narrative(self):
        payload = self._valid_with_rule()
        del payload["narrative_markdown"]
        error = DigestRunner._validate_parsed(payload)
        assert error is not None
        assert "narrative_markdown" in error

    def test_observation_not_dict(self):
        payload = self._valid_with_rule()
        payload["observation"] = "not an object"
        error = DigestRunner._validate_parsed(payload)
        assert error is not None
        assert "observation" in error

    def test_proposed_rule_missing_device_id_in_when(self):
        payload = self._valid_with_rule()
        del payload["observation"]["proposed_rule"]["when"]["device_id"]
        error = DigestRunner._validate_parsed(payload)
        assert error is not None
        assert "device_id" in error

    def test_proposed_rule_invalid_op(self):
        payload = self._valid_with_rule()
        payload["observation"]["proposed_rule"]["then"]["op"] = "sabotage"
        error = DigestRunner._validate_parsed(payload)
        assert error is not None
        assert "op" in error

    def test_related_devices_must_be_list_of_ints(self):
        payload = self._valid_with_rule()
        payload["observation"]["related_devices"] = ["not-an-int"]
        error = DigestRunner._validate_parsed(payload)
        assert error is not None
        assert "related_devices" in error

    def test_proposed_rule_missing_when_equals(self):
        payload = self._valid_with_rule()
        del payload["observation"]["proposed_rule"]["when"]["equals"]
        error = DigestRunner._validate_parsed(payload)
        assert error is not None
        assert "equals" in error

    def test_top_level_not_object(self):
        assert DigestRunner._validate_parsed([1, 2, 3]) is not None
        assert DigestRunner._validate_parsed("not an object") is not None
        assert DigestRunner._validate_parsed(None) is not None

    def test_all_rule_ops_accepted(self):
        for op in ("on", "off", "toggle", "set_brightness"):
            payload = self._valid_with_rule()
            payload["observation"]["proposed_rule"]["then"]["op"] = op
            assert DigestRunner._validate_parsed(payload) is None, f"op={op} rejected"
