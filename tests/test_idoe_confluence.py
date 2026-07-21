"""Hermetic tests for the IDOE Confluence harvester (issue #156).

Network is never hit during tests — the parser is exercised against committed
real Confluence storage XML at ``tests/fixtures/idoe_confluence_pages.json``,
captured 2026-05-04 from ``Attendance: Descriptors`` (id 379355147) and
``Enrollment: General Reporting Info`` (id 380797111).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingest.idoe_confluence import (
    _DOMAIN_RESOURCE_KEYMAP,
    _NO_NARRATIVE_RESOURCES,
    _clean_storage_xml,
    canonical_descriptor_key,
    descriptor_definition,
    domain_business_rules,
    extract_descriptor_tables,
    extract_narrative,
    is_descriptor_page,
    is_narrative_page,
    page_domain,
)


@pytest.fixture(scope="module")
def fixture_pages() -> dict:
    path = Path(__file__).parent / "fixtures" / "idoe_confluence_pages.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TestCanonicalDescriptorKey:
    """Confluence labels descriptors three ways (PascalCase singular, spaced
    plural, mixed-case). All three must collide on a single canonical key so
    XLSX ``Enumerations`` lookups don't silently miss."""

    def test_pascal_singular(self):
        assert canonical_descriptor_key("CalendarEventDescriptor") == "calendareventdescriptor"

    def test_spaced_plural(self):
        assert canonical_descriptor_key("Calendar Event Descriptors") == "calendareventdescriptor"

    def test_pascal_singular_and_plural_collide(self):
        assert (
            canonical_descriptor_key("AttendanceEventCategoryDescriptor")
            == canonical_descriptor_key("Attendance Event Category Descriptors")
        )

    def test_lowercase_with_punctuation(self):
        # Strip parens / hyphens / spaces that appear in some Confluence headings.
        # Note: the trailing-``s`` strip is conditioned on ending with the literal
        # word ``descriptors`` — when there's trailing junk like ``(PK-12)`` the
        # singular/plural collision doesn't fire, but we still get a stable key.
        assert canonical_descriptor_key("Grade Level Descriptors (PK-12)") == "gradeleveldescriptorspk12"
        # And without trailing junk the plural strip fires:
        assert canonical_descriptor_key("Grade Level Descriptors") == "gradeleveldescriptor"


class TestPageTitleFilters:
    @pytest.mark.parametrize("title,expected", [
        ("Attendance: Descriptors", True),
        ("Calendar: Types", True),
        ("Calendar: Terminology", False),
        ("Reporting Guide: Calendar", False),
        ("Enrollment: General Reporting Info", False),
    ])
    def test_descriptor_filter(self, title, expected):
        assert is_descriptor_page(title) is expected

    @pytest.mark.parametrize("title,expected", [
        ("Reporting Guide: Calendar", True),
        ("Enrollment: General Reporting Info", True),
        ("Membership: General Reporting Information", True),
        ("Calendar: Descriptors", False),
        ("Calendar: Types", False),
        ("FAQ: Terminology", False),
    ])
    def test_narrative_filter(self, title, expected):
        assert is_narrative_page(title) is expected

    def test_page_domain_extraction(self):
        assert page_domain("Reporting Guide: Calendar") == "Calendar"
        assert page_domain("Enrollment: General Reporting Info") == "Enrollment"
        assert page_domain("Calendar: Descriptors") == "Calendar"
        assert page_domain("Welcome to the IDOE Knowledge Hub") is None


class TestStorageXmlCleaning:
    def test_drops_karma_macro(self, fixture_pages):
        body = fixture_pages["attendance_descriptors"]["body_storage"]
        soup = _clean_storage_xml(body)
        # The cleaned soup should NOT contain any appanvil-karma-designer macros
        for macro in soup.find_all("ac:structured-macro"):
            assert macro.get("ac:name", "") != "appanvil-karma-designer"


class TestDescriptorExtraction:
    def test_attendance_descriptors_extracted(self, fixture_pages):
        body = fixture_pages["attendance_descriptors"]["body_storage"]
        soup = _clean_storage_xml(body)
        tables = extract_descriptor_tables(soup)
        assert tables, "expected at least one descriptor table on Attendance: Descriptors"
        # AttendanceEventCategoryDescriptor should be present with code values like "Excused Medical"
        canonical_keys = {canonical_descriptor_key(t.descriptor_name) for t in tables}
        assert "attendanceeventcategorydescriptor" in canonical_keys, (
            f"missing AttendanceEventCategoryDescriptor; got {canonical_keys}"
        )

    def test_attendance_excused_medical_prose_extracted(self, fixture_pages):
        body = fixture_pages["attendance_descriptors"]["body_storage"]
        soup = _clean_storage_xml(body)
        tables = extract_descriptor_tables(soup)
        # Find AttendanceEventCategory rows
        rows: list[dict] = []
        for t in tables:
            if canonical_descriptor_key(t.descriptor_name) == "attendanceeventcategorydescriptor":
                rows = t.rows
                break
        codes = {r["code_value"] for r in rows}
        assert "Excused Medical" in codes
        excused_medical = next(r for r in rows if r["code_value"] == "Excused Medical")
        assert "Student is excused absent" in excused_medical["description"]


class TestNarrativeExtraction:
    def test_enrollment_narrative_extracted(self, fixture_pages):
        body = fixture_pages["enrollment_narrative"]["body_storage"]
        soup = _clean_storage_xml(body)
        text = extract_narrative(soup)
        assert text, "expected non-empty narrative for Enrollment: General Reporting Info"
        # Should pick up at least one substantive paragraph
        assert "Public schools" in text or "enrollment" in text.lower()

    def test_narrative_drops_residual_karma_json(self, fixture_pages):
        body = fixture_pages["enrollment_narrative"]["body_storage"]
        soup = _clean_storage_xml(body)
        text = extract_narrative(soup)
        # Karma config strings like ``"templateId":"page"`` must not leak through
        assert '"templateId"' not in text
        assert '"borderRadius"' not in text


class TestDescriptorDefinitionLookup:
    def test_lookup_by_descriptor_name_only_returns_aggregated_prose(self):
        digest = {
            "descriptors": {
                "calendareventdescriptor": {
                    "Student Calendar": "Day on which students are present.",
                    "Holiday": "Day off; not a counting day.",
                },
            },
        }
        text = descriptor_definition(digest, "CalendarEventDescriptor")
        assert "Student Calendar" in text
        assert "Holiday" in text
        assert "Day on which students are present" in text

    def test_lookup_with_specific_code_value(self):
        digest = {
            "descriptors": {
                "attendanceeventcategorydescriptor": {
                    "Excused Medical": "Student is excused absent.",
                },
            },
        }
        text = descriptor_definition(
            digest, "AttendanceEventCategoryDescriptor", code_value="Excused Medical"
        )
        assert text == "Student is excused absent."

    def test_lookup_truncates_long_buckets(self):
        digest = {
            "descriptors": {
                "stateabbreviationdescriptor": {
                    f"S{i:02d}": f"State {i}" for i in range(60)
                },
            },
        }
        text = descriptor_definition(digest, "StateAbbreviationDescriptor")
        assert "(+48 additional values)" in text  # 60 - 12 = 48

    def test_unknown_descriptor_returns_empty(self):
        assert descriptor_definition({"descriptors": {}}, "NoSuchDescriptor") == ""

    def test_none_descriptor_returns_empty(self):
        assert descriptor_definition({"descriptors": {}}, None) == ""


class TestDomainBusinessRulesLookup:
    def test_attendance_resource_returns_attendance_narrative(self):
        digest = {
            "domain_resource_keymap": {"Attendance": ("studentSchoolAttendanceEvents",)},
            "domain_narratives": {"Attendance": "Attendance is reported per IC 20-33-2-3.2..."},
        }
        text = domain_business_rules(digest, "studentSchoolAttendanceEvents")
        assert "IC 20-33-2-3.2" in text

    def test_unknown_resource_returns_none(self):
        digest = {
            "domain_resource_keymap": _DOMAIN_RESOURCE_KEYMAP,
            "domain_narratives": {},
        }
        assert domain_business_rules(digest, "noSuchResource") is None

    def test_resource_intentionally_excluded(self):
        # `schools` is on the deliberate-omission list — domain_business_rules
        # returns None rather than fabricating a narrative.
        digest = {
            "domain_resource_keymap": _DOMAIN_RESOURCE_KEYMAP,
            "domain_narratives": {"Attendance": "..."},
        }
        assert domain_business_rules(digest, "schools") is None
        assert "schools" in _NO_NARRATIVE_RESOURCES
