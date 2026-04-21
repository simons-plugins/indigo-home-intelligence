"""Tests for delivery.DeliveryClient's pure helpers."""

from delivery import DeliveryClient


class TestSubjectWithTag:
    def test_no_reply_id_unchanged(self):
        assert DeliveryClient._subject_with_tag("Weekly digest", None) == "Weekly digest"

    def test_reply_id_appends_tag(self):
        assert (
            DeliveryClient._subject_with_tag("Weekly digest", "abc123")
            == "Weekly digest [obs-abc123]"
        )

    def test_tag_already_present_is_idempotent(self):
        already_tagged = "Weekly digest [obs-abc123]"
        assert (
            DeliveryClient._subject_with_tag(already_tagged, "abc123")
            == already_tagged
        )

    def test_tag_for_different_reply_id_still_appends(self):
        # Edge case: if the subject has a DIFFERENT tag, we still append
        # because the helper only checks the specific reply_id. Harmless
        # duplicate-looking tags that a human reader could distinguish.
        subject_with_old_tag = "Weekly digest [obs-old123]"
        result = DeliveryClient._subject_with_tag(subject_with_old_tag, "new456")
        assert "[obs-new456]" in result
