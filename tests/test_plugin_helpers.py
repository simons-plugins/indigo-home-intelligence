"""Tests for plugin.py module-level pure helpers."""

from plugin import _as_bool


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
