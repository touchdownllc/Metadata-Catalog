"""Smoke tests for the MN Data Mapping Matrix adapter.

Live GitHub / XLSX download is never hit during tests — the real fetch is
exercised at runtime by `mc ingest mn` against the cached
`data/raw/mn/github/data_mapping_matrix_*.xlsx`. Tests exercise the
low-level parsers against synthetic MNMatrixRow instances.
"""

import inspect
import json
from datetime import datetime, timezone

import click

from src.ingest import minnesota
from src.ingest.minnesota import (
    MNMatrixRow,
    _expand_comma_list,
    _looks_like_rule,
    _split_root_entity,
    build_element_records,
    run,
)
from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
    ReferenceInfo,
)
from src.models.element import StateElements
from src.models.spine import SpineSourceURLs, StateSpine
from src.spine.build import build_lookup_index


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self):
        """POC-2 had `@click.command()` on module-level `run()` — when the
        POC-3 CLI called `run()` it invoked Click.Command.__call__, which
        re-parsed sys.argv and broke. Guard against a regression (gotcha #1
        in CLAUDE.md — bit us twice in AZ/WI already)."""
        assert not isinstance(minnesota.run, click.Command)
        assert inspect.isfunction(minnesota.run)
        assert inspect.signature(minnesota.run).parameters == {}


class TestSplitRootEntity:
    def test_plain_name(self):
        assert _split_root_entity("Calendar") == ("Calendar", None)

    def test_dot_reference(self):
        assert _split_root_entity("Course.EducationOrganizationReference") == (
            "Course",
            "EducationOrganizationReference",
        )

    def test_angle_bracket_concatenates_pascal_case(self):
        """`Course > AssessmentTool` flattens to `CourseAssessmentTool`
        so the spine's unflatten map can recover it."""
        concat, suffix = _split_root_entity("Course > AssessmentTool")
        assert concat == "CourseAssessmentTool"
        assert suffix == "AssessmentTool"

    def test_multi_level_angle_brackets(self):
        concat, _ = _split_root_entity(
            "Grade > StudentSectionAssociation > CollegeCourseReference > Course"
        )
        assert concat == "GradeStudentSectionAssociationCollegeCourseReferenceCourse"

    def test_internal_whitespace_segments_collapse(self):
        concat, _ = _split_root_entity("Course > Level Characteristics")
        assert concat == "CourseLevelCharacteristics"


class TestExpandCommaList:
    def test_pure_identifier_list_expands(self):
        tokens = _expand_comma_list("ProgramType, ProgramName, BeginDate")
        assert tokens == ["ProgramType", "ProgramName", "BeginDate"]

    def test_mixed_phrase_list_is_not_split(self):
        """Human-written cell with spaces inside tokens must stay whole."""
        value = "Course Reference (CourseIdentificationCode, CourseCode)"
        assert _expand_comma_list(value) == [value]

    def test_no_comma_passthrough(self):
        assert _expand_comma_list("schoolYear") == ["schoolYear"]


class TestLooksLikeRule:
    def test_newline_marks_rule(self):
        assert _looks_like_rule("line1\nline2")

    def test_equals_marks_rule(self):
        assert _looks_like_rule("Core.Calendar.CalendarCode = 'Kindergarten'")

    def test_plain_identifier_is_not_rule(self):
        assert not _looks_like_rule("calendarCode")


class TestBuildElementRecords:
    def _row(self, **overrides) -> MNMatrixRow:
        base = dict(
            sheet="Student Enrollment Elements",
            row_id="1",
            collection="StudentEnrollment",
            mde_entity=None,
            mde_group="School Calendar",
            mde_element="fiscalYear",
            edfi_entity_raw="Calendar",
            edfi_element_raw="SchoolYear",
            mapping_method="Core",
            enumeration=None,
            notes=None,
        )
        base.update(overrides)
        return MNMatrixRow(**base)

    def test_simple_core_row_produces_one_record(self):
        records = build_element_records(
            [self._row()],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert len(records) == 1
        r = records[0]
        assert r.state == "MN"
        assert r.edfi_version == "4.0"
        assert r.entity == "Calendar"
        assert r.element_name == "SchoolYear"
        assert "MDE mapping: School Calendar.fiscalYear" in r.definition_text
        assert r.business_rules_text is None
        assert r.source_page_or_section == "Student Enrollment Elements#row1"

    def test_rule_cell_falls_back_to_mde_name_and_captures_rule(self):
        rule = (
            "To indicate Kindergarten Schedule\n"
            'Core.Calendar.CalendarCode = "Kindergarten Schedule"'
        )
        records = build_element_records(
            [self._row(
                mde_element="kindergartenSchedule",
                edfi_element_raw=rule,
            )],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert len(records) == 1
        r = records[0]
        assert r.element_name == "kindergartenSchedule"  # MDE fallback
        assert r.business_rules_text is not None
        assert "Kindergarten Schedule" in r.business_rules_text

    def test_comma_list_expands_into_multiple_records(self):
        records = build_element_records(
            [self._row(
                edfi_entity_raw="StudentHomelessProgramAssociation",
                edfi_element_raw="ProgramType, ProgramName, BeginDate",
            )],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert {r.element_name for r in records} == {
            "ProgramType",
            "ProgramName",
            "BeginDate",
        }
        assert all(
            r.entity == "StudentHomelessProgramAssociation" for r in records
        )

    def test_angle_bracket_entity_flattens_for_unflatten(self):
        records = build_element_records(
            [self._row(
                edfi_entity_raw="Course > AssessmentTool",
                edfi_element_raw="assessmentToolDescriptor",
            )],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert len(records) == 1
        assert records[0].entity == "CourseAssessmentTool"
        assert records[0].raw_entity and "Course > AssessmentTool" in records[0].raw_entity

    def test_notes_flow_into_business_rules(self):
        records = build_element_records(
            [self._row(notes="MCCC-specific handling required")],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert records[0].business_rules_text is not None
        assert "MCCC-specific" in records[0].business_rules_text

    def test_dash_entity_row_is_skipped(self):
        """MN matrix uses literal `-` to mark explicit non-mappings; those
        rows must not enter the source-coverage denominator."""
        records = build_element_records(
            [self._row(edfi_entity_raw="-", edfi_element_raw="-")],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert records == []

    def test_dash_element_only_row_is_skipped(self):
        """Entity present, element `-` (no resolvable target) — also dropped."""
        records = build_element_records(
            [self._row(edfi_entity_raw="Course", edfi_element_raw="-")],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert records == []

    def test_title1_normalized_to_titleI_in_element_name(self):
        """`Title1PartAParticipant` element should be aliased to `TitleIPartAParticipant`
        so it matches Ed-Fi's Roman-numeral entity properties."""
        records = build_element_records(
            [self._row(
                edfi_entity_raw="StudentTitle1PartAProgramAssociation",
                edfi_element_raw="Title1PartAParticipant",
            )],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert len(records) == 1
        assert records[0].element_name == "TitleIPartAParticipant"

    def test_title1_substitution_only_at_word_boundary(self):
        """Don't accidentally chop `Title1` out of arbitrary words like a
        column called `dataTitle1Suffix` (none today, but guard the regex)."""
        from src.ingest.minnesota import _clean_element_name
        # Word-boundary-prefixed `Title1` followed by a letter rewrites.
        assert _clean_element_name("Title1PartAParticipant") == "TitleIPartAParticipant"
        # Embedded `Title1` (no leading word boundary) should not rewrite.
        assert _clean_element_name("metaTitle1PartAParticipant") == "metaTitle1PartAParticipant"
        # Bare `Title1` with no following letter shouldn't change.
        assert _clean_element_name("Title1") == "Title1"

    def test_trailing_dot_stripped(self):
        """MDE matrix occasionally leaves a trailing dot (`GenderIdentities.`)
        — authorial typo, not a path separator. Analyst feedback T2.4."""
        from src.ingest.minnesota import _clean_element_name
        assert _clean_element_name("GenderIdentities.") == "GenderIdentities"
        # Interior dots still preserved (they mean something for path-tail aliases).
        assert _clean_element_name("schoolReference.schoolId") == "schoolReference.schoolId"


class TestStripElementAnnotations:
    """MN matrix authors append role hints in parentheses; spine matching
    needs the bare identifier, analysts still see the hint in definition
    text. Confirmed by analyst feedback (rows on LanguageAcademicHonor)."""

    def test_strip_part_of_identity_suffix(self):
        from src.ingest.minnesota import _strip_element_annotations
        bare, note = _strip_element_annotations("AcademicHonorCategory (Part of Identity)")
        assert bare == "AcademicHonorCategory"
        assert note == "Part of Identity"

    def test_strip_optional_collection_suffix(self):
        from src.ingest.minnesota import _strip_element_annotations
        bare, note = _strip_element_annotations("Language (Optional Collection)")
        assert bare == "Language"
        assert note == "Optional Collection"

    def test_no_annotation_returns_none(self):
        from src.ingest.minnesota import _strip_element_annotations
        bare, note = _strip_element_annotations("SchoolYear")
        assert bare == "SchoolYear"
        assert note is None


class TestStripEntityPrefix:
    def test_strip_matching_prefix(self):
        from src.ingest.minnesota import _strip_entity_prefix
        assert _strip_entity_prefix("Student.StudentUniqueId", "Student") == "StudentUniqueId"

    def test_case_insensitive_match(self):
        from src.ingest.minnesota import _strip_entity_prefix
        # root_entity canonical form is PascalCase; source might lowercase.
        assert _strip_entity_prefix("student.StudentUniqueId", "Student") == "StudentUniqueId"

    def test_no_prefix_passthrough(self):
        from src.ingest.minnesota import _strip_entity_prefix
        assert _strip_entity_prefix("SchoolYear", "Calendar") == "SchoolYear"

    def test_partial_overlap_does_not_strip(self):
        """`StudentUniqueId` starts with `Student` but NOT with `Student.` —
        must not be stripped."""
        from src.ingest.minnesota import _strip_entity_prefix
        assert _strip_entity_prefix("StudentUniqueId", "Student") == "StudentUniqueId"


class TestBuildElementRecordsAnnotationHandling:
    def _row(self, **overrides):
        base = dict(
            sheet="Student Enrollment Elements",
            row_id="1",
            collection="StudentEnrollment",
            mde_entity=None,
            mde_group="Honors",
            mde_element="honorCategory",
            edfi_entity_raw="LanguageAcademicHonor",
            edfi_element_raw="AcademicHonorCategory (Part of Identity)",
            mapping_method="Core",
            enumeration=None,
            notes=None,
        )
        base.update(overrides)
        return MNMatrixRow(**base)

    def test_annotation_moves_to_definition_and_element_is_bare(self):
        records = build_element_records(
            [self._row()],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert len(records) == 1
        assert records[0].element_name == "AcademicHonorCategory"
        assert "role: Part of Identity" in records[0].definition_text

    def test_dotted_element_strips_entity_prefix(self):
        """Analyst feedback row 3: `Student` / `Student.StudentUniqueId` →
        `Student` / `StudentUniqueId`. Only that one row has the dotted form."""
        records = build_element_records(
            [self._row(
                edfi_entity_raw="Student",
                edfi_element_raw="Student.StudentUniqueId",
            )],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert len(records) == 1
        assert records[0].element_name == "StudentUniqueId"


class TestSpineTypePopulation:
    """Analyst P1: every MN row had `data_type=None`. After matching to the
    spine, primitive types / descriptor / reference / collection labels
    should flow into `data_type` for matched records."""

    def _spine(self):
        from datetime import datetime, timezone
        from src.models.edfi_catalog import (
            EdFiCatalog,
            EntityEntry,
            ExtensionEntry,
            PropertyInfo,
            ReferenceInfo,
            SubCollectionInfo,
        )
        from src.models.spine import SpineSourceURLs, StateSpine

        calendar = EntityEntry(
            properties={
                "calendarCode": PropertyInfo(type="string"),
                "schoolYear": PropertyInfo(type="integer"),
                "calendarTypeDescriptor": PropertyInfo(type="string"),
            },
            references={"schoolReference": ReferenceInfo(entity="School")},
            sub_collections={"gradeLevels": SubCollectionInfo(sub_entity="CalendarGradeLevel")},
        )
        student = EntityEntry(
            properties={
                "studentUniqueId": PropertyInfo(type="string"),
                "birthDate": PropertyInfo(type="string"),
            },
        )
        parent = EntityEntry(
            properties={"firstName": PropertyInfo(type="string")},
        )
        catalog = EdFiCatalog(
            version="4.0",
            entity_count=3,
            extension_count=1,
            entities={"Calendar": calendar, "Student": student, "Parent": parent},
            extensions={
                "mn_parentExtension": ExtensionEntry(
                    extends_entity="Parent",
                    source_prefix="mn",
                    properties={"householdIncome": PropertyInfo(type="number")},
                ),
            },
            lookup_index={"calendar": "Calendar", "student": "Student", "parent": "Parent"},
        )
        return StateSpine(
            state="MN",
            edfi_version="4.0",
            fetched_at=datetime.now(tz=timezone.utc),
            source_urls=SpineSourceURLs(resources="http://test/r.json"),
            catalog=catalog,
        )

    def _records(self, rows):
        from src.models.element import ElementRecord
        return [
            ElementRecord(
                state="MN",
                edfi_version="4.0",
                domain="X",
                entity=entity,
                element_name=element_name,
                definition_text="",
                source=source,
                documented=True,
            )
            for entity, element_name, source in rows
        ]

    def test_primitive_type_populated(self):
        from src.ingest.minnesota import _populate_data_types_from_spine
        spine = self._spine()
        records = self._records([("Calendar", "calendarCode", "core")])
        _populate_data_types_from_spine(records, spine)
        assert records[0].data_type == "String"

    def test_descriptor_populated_as_descriptor_regardless_of_raw_type(self):
        from src.ingest.minnesota import _populate_data_types_from_spine
        spine = self._spine()
        records = self._records([("Calendar", "calendarTypeDescriptor", "core")])
        _populate_data_types_from_spine(records, spine)
        assert records[0].data_type == "Descriptor"

    def test_reference_populated(self):
        from src.ingest.minnesota import _populate_data_types_from_spine
        spine = self._spine()
        records = self._records([("Calendar", "schoolReference", "core")])
        _populate_data_types_from_spine(records, spine)
        assert records[0].data_type == "Reference"

    def test_sub_collection_populated(self):
        from src.ingest.minnesota import _populate_data_types_from_spine
        spine = self._spine()
        records = self._records([("Calendar", "gradeLevels", "core")])
        _populate_data_types_from_spine(records, spine)
        assert records[0].data_type == "Collection"

    def test_extension_property_populated(self):
        from src.ingest.minnesota import _populate_data_types_from_spine
        spine = self._spine()
        records = self._records([("Parent", "householdIncome", "extension")])
        _populate_data_types_from_spine(records, spine)
        assert records[0].data_type == "Number"

    def test_unknown_record_left_untouched(self):
        from src.ingest.minnesota import _populate_data_types_from_spine
        spine = self._spine()
        records = self._records([("Calendar", "totallyMadeUp", "unknown")])
        _populate_data_types_from_spine(records, spine)
        assert records[0].data_type is None


class TestMultiTargetSplitters:
    """Helpers for recognizing and splitting multi-entity rows."""

    def test_split_multi_entity_comma(self):
        from src.ingest.minnesota import _split_multi_entity
        assert _split_multi_entity("Student, StudentEducationOrganizationAssociation") == [
            "Student", "StudentEducationOrganizationAssociation",
        ]

    def test_split_multi_entity_ampersand(self):
        from src.ingest.minnesota import _split_multi_entity
        assert _split_multi_entity("StudentEducationOrganizationAssociation & Student") == [
            "StudentEducationOrganizationAssociation", "Student",
        ]

    def test_split_multi_entity_single_returns_none(self):
        from src.ingest.minnesota import _split_multi_entity
        assert _split_multi_entity("Student") is None

    def test_split_multi_entity_rejects_non_pascal_tokens(self):
        """Entity cells with `>`-separated nav paths or dotted references
        shouldn't be mistaken for multi-target lists."""
        from src.ingest.minnesota import _split_multi_entity
        assert _split_multi_entity("Course > AssessmentTool") is None
        assert _split_multi_entity("Course.EducationOrganizationReference") is None

    def test_expand_multi_target_elements_dotted(self):
        from src.ingest.minnesota import _expand_multi_target_elements
        assert _expand_multi_target_elements("BirthData.BirthDate, SEOA.BirthDate") == [
            "BirthData.BirthDate", "SEOA.BirthDate",
        ]

    def test_expand_multi_target_elements_plain(self):
        from src.ingest.minnesota import _expand_multi_target_elements
        assert _expand_multi_target_elements("firstName, lastName") == [
            "firstName", "lastName",
        ]

    def test_expand_multi_target_elements_with_phrase_returns_none(self):
        from src.ingest.minnesota import _expand_multi_target_elements
        # Parenthetical / space-containing tokens can't be confidently split.
        assert _expand_multi_target_elements(
            "firstName, Student Name Part (Identity)"
        ) is None


class TestBuildElementRecordsMultiTargetRows:
    """End-to-end: analyst-flagged MN rows fan out into per-entity rows."""

    def _row(self, **overrides):
        base = dict(
            sheet="Student Enrollment Elements",
            row_id="196",
            collection="StudentEnrollment",
            mde_entity=None,
            mde_group="Demographics",
            mde_element="birthDate",
            edfi_entity_raw="Student, StudentEducationOrganizationAssociation",
            edfi_element_raw="BirthData.BirthDate, SEOA.BirthDate",
            mapping_method="Core",
            enumeration=None,
            notes=None,
        )
        base.update(overrides)
        return MNMatrixRow(**base)

    def test_parallel_split_produces_one_record_per_entity(self):
        """Row 196 case: 2 entities × 2 elements with matching counts →
        parallel 1:1 pairing."""
        records = build_element_records(
            [self._row()],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        entities = {r.entity for r in records}
        assert entities == {"Student", "StudentEducationOrganizationAssociation"}
        assert len(records) == 2

    def test_cross_product_for_asymmetric_counts(self):
        """Row 198 case: 2 entities × 6 elements → 2×6 = 12 cross-product rows."""
        records = build_element_records(
            [self._row(
                row_id="198",
                edfi_element_raw=(
                    "Name.FirstName, Name.LastSurname, Name.MiddleName, "
                    "SEOA.FirstName, SEOA.LastSurname, SEOA.MiddleName"
                ),
            )],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert len(records) == 12

    def test_ampersand_single_element(self):
        """Row 295: `SEOA & Student` × `gender` → 2 rows."""
        records = build_element_records(
            [self._row(
                row_id="295",
                edfi_entity_raw="StudentEducationOrganizationAssociation & Student",
                edfi_element_raw="gender",
            )],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        entities = {r.entity for r in records}
        assert entities == {"StudentEducationOrganizationAssociation", "Student"}
        assert len(records) == 2
        assert all(r.element_name == "gender" for r in records)

    def test_non_multi_entity_passthrough(self):
        """Ordinary single-entity row must still yield one record."""
        records = build_element_records(
            [self._row(
                edfi_entity_raw="Calendar",
                edfi_element_raw="schoolYear",
            )],
            edfi_version="4.0",
            source_document="test.xlsx",
            catalog=None,
        )
        assert len(records) == 1
        assert records[0].entity == "Calendar"
        assert records[0].element_name == "schoolYear"


def _make_mn_spine() -> StateSpine:
    """Minimal MN spine: Calendar + Parent + mn_parentExtension, matching
    the fixture rows in TestEndToEndRun."""
    entities = {
        "Calendar": EntityEntry(
            description="Ed-Fi Calendar.",
            domains=["Calendar"],
            properties={
                "calendarCode": PropertyInfo(
                    description="Calendar code.", type="string",
                ),
                "schoolYear": PropertyInfo(
                    description="School year.", type="integer",
                ),
            },
            references={"schoolReference": ReferenceInfo(entity="School")},
            sub_collections={},
        ),
        "Parent": EntityEntry(
            description="Parent.",
            domains=["Parent"],
            properties={
                "firstName": PropertyInfo(
                    description="First name.", type="string",
                ),
            },
            references={},
            sub_collections={},
        ),
    }
    extensions = {
        "mn_parentExtension": ExtensionEntry(
            extends_entity="Parent",
            source_prefix="mn",
            properties={
                "householdIncome": PropertyInfo(
                    description="Household income.", type="number",
                ),
            },
        ),
    }
    catalog = EdFiCatalog(
        version="4.0",
        entity_count=len(entities),
        extension_count=len(extensions),
        entities=entities,
        extensions=extensions,
        lookup_index=build_lookup_index(entities, extensions),
    )
    return StateSpine(
        state="MN",
        edfi_version="4.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(
            resources="http://example.test/metadata/data/v3/resources/swagger.json",
        ),
        catalog=catalog,
    )


def _make_mn_rows() -> list[MNMatrixRow]:
    """Tiny mapping-matrix fixture: 3 rows covering core + extension +
    unresolved, so the dual-lens E2E has something to exercise."""
    def _row(**overrides) -> MNMatrixRow:
        base = dict(
            sheet="Calendar Elements",
            row_id="1",
            collection="Calendar",
            mde_entity=None,
            mde_group="Calendar",
            mde_element=None,
            edfi_entity_raw=None,
            edfi_element_raw=None,
            mapping_method="Core",
            enumeration=None,
            notes=None,
        )
        base.update(overrides)
        return MNMatrixRow(**base)

    return [
        _row(row_id="1", edfi_entity_raw="Calendar", edfi_element_raw="calendarCode"),
        _row(row_id="2", edfi_entity_raw="Parent", edfi_element_raw="householdIncome"),
        _row(
            row_id="3",
            edfi_entity_raw="LegacyMdeOnlyEntity",
            edfi_element_raw="legacyField",
        ),
    ]


class TestEndToEndRun:
    """Regression guard: `run()` must write BOTH `_source.json` and
    `_spine.json` artifacts (Option 3b dual-lens contract)."""

    def test_run_writes_both_lens_artifacts(self, tmp_path, monkeypatch):
        spine = _make_mn_spine()
        spine_path = tmp_path / "data" / "spine" / "mn_spine.json"
        spine_path.parent.mkdir(parents=True, exist_ok=True)
        spine_path.write_text(spine.model_dump_json(indent=2), encoding="utf-8")

        out_dir = tmp_path / "data" / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        xlsx_path = tmp_path / "mapping_matrix.xlsx"
        xlsx_path.write_bytes(b"")  # exists but empty; parse is monkeypatched

        monkeypatch.setattr(minnesota, "_PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(minnesota, "_MN_SPINE_PATH", spine_path)
        monkeypatch.setattr(minnesota, "_MN_XLSX_PATH", xlsx_path)
        monkeypatch.setattr(
            minnesota, "_MN_ELEMENTS_OUT", out_dir / "mn_elements_source.json"
        )
        monkeypatch.setattr(
            minnesota,
            "_MN_ELEMENTS_SPINE_OUT",
            out_dir / "mn_elements_spine.json",
        )
        monkeypatch.setattr(
            minnesota, "_MN_GAP_OUT", out_dir / "mn_gap_log.json"
        )
        monkeypatch.setattr(minnesota, "_download_xlsx_if_missing", lambda: None)
        monkeypatch.setattr(
            minnesota, "parse_mn_matrix", lambda _path: _make_mn_rows()
        )

        run()

        source_path = out_dir / "mn_elements_source.json"
        spine_elements_path = out_dir / "mn_elements_spine.json"
        gap_path = out_dir / "mn_gap_log.json"
        assert source_path.exists()
        assert spine_elements_path.exists(), (
            "run() must write the spine-lens artifact (Option 3b contract)"
        )
        assert gap_path.exists()

        source_elements = StateElements.model_validate_json(
            source_path.read_text(encoding="utf-8")
        )
        assert source_elements.state == "MN"
        assert source_elements.element_count == len(source_elements.elements)
        assert source_elements.element_count > 0

        spine_elements = StateElements.model_validate_json(
            spine_elements_path.read_text(encoding="utf-8")
        )
        assert spine_elements.state == "MN"
        assert spine_elements.element_count == len(spine_elements.elements)
        spine_pairs = {
            (r.entity, r.element_name) for r in spine_elements.elements
        }
        # Canonical spine slots we expect walked from the catalog:
        assert ("Calendar", "calendarCode") in spine_pairs
        assert ("Parent", "householdIncome") in spine_pairs
        # Spine-missing source row appended as hybrid tail:
        assert any(
            r.entity == "LegacyMdeOnlyEntity" and r.source == "unknown"
            and r.documented
            for r in spine_elements.elements
        )

        gap = json.loads(gap_path.read_text(encoding="utf-8"))
        assert "source_coverage" in gap
        assert "spine_coverage" in gap
