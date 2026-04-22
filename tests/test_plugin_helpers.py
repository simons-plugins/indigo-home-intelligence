"""Tests for plugin.py module-level pure helpers."""

from plugin import _as_bool, _as_int, _as_optional_int


class TestAsBool:
    def test_true_bool(self):
        assert _as_bool(True) is True

    def test_false_bool(self):
        assert _as_bool(False) is False

    def test_string_true(self):
        assert _as_bool("true") is True
        assert _as_bool("True") is True
        assert _as_bool("TRUE") is True

    def test_string_false(self):
        assert _as_bool("false") is False
        assert _as_bool("False") is False

    def test_common_synonyms_truthy(self):
        assert _as_bool("yes") is True
        assert _as_bool("1") is True
        assert _as_bool("on") is True

    def test_common_synonyms_falsy(self):
        assert _as_bool("no") is False
        assert _as_bool("0") is False
        assert _as_bool("off") is False

    def test_none_returns_default(self):
        assert _as_bool(None) is False
        assert _as_bool(None, default=True) is True

    def test_int_truthy(self):
        assert _as_bool(1) is True
        assert _as_bool(42) is True
        assert _as_bool(-1) is True

    def test_int_falsy(self):
        assert _as_bool(0) is False

    def test_float(self):
        assert _as_bool(1.5) is True
        assert _as_bool(0.0) is False

    def test_unknown_string_falls_through_to_false(self):
        # Catches accidental "maybe"-style inputs from buggy config UIs.
        assert _as_bool("maybe") is False
        assert _as_bool("") is False

    def test_whitespace_is_stripped(self):
        assert _as_bool(" true ") is True
        assert _as_bool("  false ") is False


class TestAsInt:
    def test_valid_int(self):
        assert _as_int("42", 0) == 42

    def test_empty_returns_default(self):
        assert _as_int("", 20) == 20
        assert _as_int(None, 20) == 20

    def test_invalid_returns_default(self):
        assert _as_int("not-a-number", 20) == 20
        assert _as_int("12.5", 20) == 20  # int() rejects floats as strings

    def test_below_min_returns_default(self):
        # Pref typo — a battery threshold of -5 makes no sense; fall
        # back rather than silently accept.
        assert _as_int("-5", 20, min_value=0, max_value=100) == 20

    def test_above_max_returns_default(self):
        # Battery threshold of 200% is unreachable — fall back.
        assert _as_int("200", 20, min_value=0, max_value=100) == 20

    def test_at_boundary_accepted(self):
        assert _as_int("0", 20, min_value=0, max_value=100) == 0
        assert _as_int("100", 20, min_value=0, max_value=100) == 100

    def test_no_bounds_accepts_anything_in_int_range(self):
        assert _as_int("999999", 0) == 999999
        assert _as_int("-999999", 0) == -999999


class TestAsOptionalInt:
    def test_valid_int(self):
        assert _as_optional_int("452894065") == 452894065

    def test_empty_returns_none(self):
        assert _as_optional_int("") is None
        assert _as_optional_int(None) is None

    def test_invalid_returns_none(self):
        assert _as_optional_int("not-a-number") is None

    def test_negative_accepted(self):
        # Device IDs are positive but validation lives at the call site
        # (whether the ID is in discovery) — the helper itself just
        # parses.
        assert _as_optional_int("-1") == -1
