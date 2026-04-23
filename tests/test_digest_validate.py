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

    def test_bool_device_id_rejected(self):
        # isinstance(True, int) is True in Python, so a naive int
        # check would let {"device_id": true} sneak past the gate and
        # blow up at indigo.devices[True]. Validator must explicitly
        # exclude bools.
        for field, offender in (("when", {"device_id": True, "state": "onState", "equals": True}),
                                 ("then", {"device_id": False, "op": "off"})):
            payload = self._valid_with_rule()
            payload["observation"]["proposed_rule"][field] = offender
            err = DigestRunner._validate_parsed(payload)
            assert err is not None and "device_id must be an int" in err, (field, err)

    def test_unhashable_op_rejected_cleanly(self):
        # A list-typed op would raise `TypeError: unhashable type: 'list'`
        # against the frozenset membership check; validator must catch
        # the type before the comparison.
        payload = self._valid_with_rule()
        payload["observation"]["proposed_rule"]["then"]["op"] = ["off"]
        err = DigestRunner._validate_parsed(payload)
        assert err is not None and "then.op" in err


class TestShapeWarnings:
    """The soft shape-check that runs after _validate_parsed. Detects
    narrative drift from the pinned template in INSTRUCTIONS but never
    blocks delivery — a slightly-off digest is better than no digest."""

    _WELL_FORMED_OBSERVATION = {
        "subject": "Week of 15-22 Apr: Dining TRV issues",
        "narrative_markdown": (
            "## This week in the house\n\n"
            "Everything ran smoothly except one thing.\n\n"
            "### What caught my eye\n\n"
            "The Dining TRV kept dropping off the network.\n\n"
            "### The inference\n\n"
            "This could affect Cat's WFH heating schedule."
        ),
        "observation": {
            "headline": "Dining TRV unreliable",
            "rationale": "Offline repeatedly across the week.",
            "related_devices": [123],
            "proposed_rule": None,
        },
    }

    def test_well_formed_observation_no_warnings(self):
        assert DigestRunner._shape_warnings(self._WELL_FORMED_OBSERVATION) == []

    def test_well_formed_quiet_week_no_warnings(self):
        payload = {
            "subject": "Week of 15-22 Apr: quiet week, everything healthy",
            "narrative_markdown": (
                "## Quiet week in the house\n\n"
                "No leaks, no alarms, no outages.\n\n"
                "Heating and lighting all behaved as configured."
            ),
            "observation": None,
        }
        assert DigestRunner._shape_warnings(payload) == []

    def test_subject_without_week_of_prefix_warns(self):
        payload = dict(self._WELL_FORMED_OBSERVATION)
        payload["subject"] = "Weekly digest for April"
        warnings = DigestRunner._shape_warnings(payload)
        assert any("Week of" in w for w in warnings)

    def test_narrative_missing_any_heading_warns(self):
        payload = dict(self._WELL_FORMED_OBSERVATION)
        payload["narrative_markdown"] = "Just a flat paragraph, no headings."
        warnings = DigestRunner._shape_warnings(payload)
        assert any("'## '" in w for w in warnings)

    def test_observation_missing_what_caught_my_eye_warns(self):
        payload = dict(self._WELL_FORMED_OBSERVATION)
        payload["narrative_markdown"] = (
            "## This week\n\nPrelude.\n\n"
            "### The inference\n\nAnalysis."
        )
        warnings = DigestRunner._shape_warnings(payload)
        assert any("What caught my eye" in w for w in warnings)

    def test_observation_missing_the_inference_warns(self):
        payload = dict(self._WELL_FORMED_OBSERVATION)
        payload["narrative_markdown"] = (
            "## This week\n\nPrelude.\n\n"
            "### What caught my eye\n\nDescription."
        )
        warnings = DigestRunner._shape_warnings(payload)
        assert any("The inference" in w for w in warnings)

    def test_quiet_week_narrative_doesnt_need_observation_sections(self):
        # observation=None means no '### What caught my eye' /
        # '### The inference' required; the opening '## ' heading is
        # still required though.
        payload = {
            "subject": "Week of 1-8 Feb: quiet",
            "narrative_markdown": "## Calm week\n\nAll good.",
            "observation": None,
        }
        assert DigestRunner._shape_warnings(payload) == []

    def test_empty_subject_warns(self):
        payload = dict(self._WELL_FORMED_OBSERVATION)
        payload["subject"] = ""
        warnings = DigestRunner._shape_warnings(payload)
        assert any("Week of" in w for w in warnings)
