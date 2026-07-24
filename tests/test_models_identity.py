"""Frozen byte-for-byte contract for the record-key format (plan A1).

The format keys committed curation sidecars and every cross-artifact
join — these assertions pin the EXISTING format exactly; a failure
here means orphaned human decisions, not a style regression.
"""

from __future__ import annotations

from src.models.identity import record_key


class TestRecordKeyFormat:
    def test_exact_format_byte_for_byte(self) -> None:
        assert record_key("AZ", "Student", "beginDate") == "AZ|Student|beginDate"

    def test_state_is_uppercased(self) -> None:
        assert record_key("az", "Student", "beginDate") == "AZ|Student|beginDate"

    def test_entity_and_element_casing_preserved_verbatim(self) -> None:
        # Source-lens keys carry the source document's casing (AZ XLSX
        # PascalCase); spine-lens keys carry swagger camelCase. The key
        # NEVER normalizes them — cross-lens joins lower the whole key
        # at the join site (issue #94).
        assert (
            record_key("TX", "CourseTranscript", "CourseAttemptResult")
            == "TX|CourseTranscript|CourseAttemptResult"
        )
        assert (
            record_key("TX", "CourseTranscript", "courseAttemptResult")
            == "TX|CourseTranscript|courseAttemptResult"
        )

    def test_pipe_separator_and_three_parts(self) -> None:
        key = record_key("WI", "DisciplineIncident", "schoolId")
        assert key.count("|") == 2
        assert key.split("|") == ["WI", "DisciplineIncident", "schoolId"]

    def test_delegating_helpers_agree(self) -> None:
        """The pre-A1 constructions now delegate here — spot-check the
        two public delegates keep byte-identical output."""
        from src.report.loaders import record_key as loaders_record_key
        from tests.factories import make_record

        rec = make_record(entity="Student", element_name="beginDate", state="AZ")
        assert loaders_record_key("az", rec) == "AZ|Student|beginDate"
