"""Smoke tests for src/ingest/normalize.py and src/utils/matching.py."""

from src.ingest.normalize import normalize_data_type, normalize_entity
from src.utils import matching


class TestNormalizeEntityNoCatalog:
    def test_strips_namespace(self):
        assert normalize_entity("edfi.Course") == "Course"
        assert normalize_entity("az.Section") == "Section"
        assert normalize_entity("wi_student") == "Student"

    def test_fixes_extention_typo(self):
        assert normalize_entity("SectionExtention") == "SectionExtension"

    def test_pascal_case_first_char(self):
        assert normalize_entity("student") == "Student"

    def test_preserves_unknown(self):
        assert normalize_entity("StudentNeed") == "StudentNeed"


class TestEntityNameFixes:
    def test_section504_placement_to_plan(self):
        """MN matrix uses `Section504Placement…`; canonical Ed-Fi entity is
        `Section504Plan…`."""
        assert (
            normalize_entity("StudentSection504PlacementProgramAssociation")
            == "StudentSection504PlanProgramAssociation"
        )

    def test_section504_placement_extension_variant(self):
        assert (
            normalize_entity("StudentSection504PlacementProgramAssociationExtension")
            == "StudentSection504PlanProgramAssociationExtension"
        )

    def test_title_arabic_to_roman(self):
        """MN uses Arabic `1`; Ed-Fi entity uses Roman `I`."""
        assert (
            normalize_entity("StudentTitle1PartAProgramAssociation")
            == "StudentTitleIPartAProgramAssociation"
        )


class TestNormalizeDataType:
    def test_string_family(self):
        assert normalize_data_type("varchar(255)") == "String"
        assert normalize_data_type("nvarchar(50)") == "String"
        assert normalize_data_type("text") == "String"
        assert normalize_data_type("string") == "String"

    def test_integer_family(self):
        assert normalize_data_type("bigint") == "Integer"
        assert normalize_data_type("int") == "Integer"
        assert normalize_data_type("smallint") == "Integer"

    def test_wi_big_integer_variants(self):
        """WI Confluence authors write space-separated `big integer` and a
        known typo `big nteger` — both must normalize to Integer."""
        assert normalize_data_type("big integer") == "Integer"
        assert normalize_data_type("Big integer") == "Integer"
        assert normalize_data_type("big nteger") == "Integer"

    def test_array_maps_to_collection(self):
        """WI uses `array` for repeated/collection-typed fields."""
        assert normalize_data_type("array") == "Collection"
        assert normalize_data_type("Array") == "Collection"

    def test_decimal(self):
        assert normalize_data_type("decimal(5,2)") == "Decimal"
        assert normalize_data_type("dec(7,2)") == "Decimal"

    def test_boolean(self):
        assert normalize_data_type("bit") == "Boolean"
        assert normalize_data_type("boolean") == "Boolean"

    def test_date_time(self):
        assert normalize_data_type("date") == "Date"
        assert normalize_data_type("datetime") == "Date"
        assert normalize_data_type("time") == "Time"

    def test_none_passes_through(self):
        assert normalize_data_type(None) is None
        assert normalize_data_type("") is None

    def test_unknown_returns_cleaned(self):
        assert normalize_data_type("exotic_type") == "exotic_type"


class TestMatchingEntity:
    def test_plural_singular(self):
        assert matching.entity_match_form("Calendars") == matching.entity_match_form("Calendar")

    def test_preserves_ss_us_is(self):
        assert matching.entity_match_form("Address") == "address"
        assert matching.entity_match_form("Census") == "census"

    def test_course_transcript_ext_rename(self):
        assert matching.entity_match_form("CourseTranscriptExt") == matching.entity_match_form(
            "CourseTranscript"
        )


class TestMatchingElementAliases:
    def test_dotted_path_aliases(self):
        aliases = matching.element_aliases("school.schoolId")
        assert "schoolId" in aliases
        assert "school.schoolId" in aliases

    def test_parenthesized_note_stripped(self):
        aliases = matching.element_aliases("alternativeCourseCode (For 2025-26 onward)")
        assert "alternativeCourseCode" in aliases


class TestMatchKey:
    def test_combines_entity_and_element(self):
        assert matching.match_key("Calendars", "calendarCode") == (
            "calendar",
            "calendarCode",
        )


class TestDerivedNsPrefixRegex:
    """Issue #213 item 2: `_NS_PREFIX_RE`'s long agency prefixes (IN's
    `idoe`) derive from `src.states.STATE_INFO`; two-letter prefixes ride
    the generic `[A-Za-z]{2}` class. The derived pattern must be
    byte-identical to the pre-derivation literal."""

    def test_pattern_string_pinned_to_legacy_literal(self):
        from src.ingest.normalize import _NS_PREFIX_RE

        assert _NS_PREFIX_RE.pattern == r"^(?:edfi|idoe|[A-Za-z]{2})[._/]"

    def test_every_state_prefix_strips(self):
        from src.states import STATE_INFO

        for state, info in STATE_INFO.items():
            for prefix in info.ext_prefixes:
                assert normalize_entity(f"{prefix}.course") == "Course", (
                    f"{state}'s ext prefix {prefix!r} not stripped by "
                    "_NS_PREFIX_RE"
                )
