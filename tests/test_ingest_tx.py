"""Tests for the TX TWEDS-sourced ingestion adapter.

TX is source-driven: TWEDS v33 (TEDS) enumerates which (entity, element)
rows appear in the TX workbook; the Ed-Fi swagger spine enriches canonical
names, types, and extension attribution. These tests exercise that pipeline
with minimal synthetic TWEDS + spine fixtures so they run in milliseconds
without hitting the network.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone

import click
import pytest

from src.ingest import texas
from src.ingest.texas import build_element_records, run
from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
)
from src.models.element import StateElements
from src.models.spine import SpineSourceURLs, StateSpine
from src.spine.build import build_lookup_index


def _make_spine() -> StateSpine:
    """Minimal TX spine fixture with one core entity, one reference, and a
    TPDM extension — matches the production TX spine shape (Ed-Fi 3 vanilla
    + TPDM, no TEA state extensions)."""
    entities = {
        "Student": EntityEntry(
            description="Ed-Fi core Student entity",
            domains=["Student Identification and Demographics"],
            properties={
                "firstName": PropertyInfo(
                    description="The student's first name.",
                    type="string",
                ),
                "birthDate": PropertyInfo(
                    description="Student birth date.",
                    type="string",
                    format="date",
                ),
                "studentUniqueId": PropertyInfo(
                    description="Unique Ed-Fi student identifier.",
                    type="string",
                ),
            },
            references={},
            sub_collections={},
        ),
        "School": EntityEntry(
            description="Ed-Fi core School entity",
            domains=["Education Organization"],
            properties={
                "schoolId": PropertyInfo(
                    description="The identifier assigned to a school.",
                    type="integer",
                ),
            },
            references={},
            sub_collections={},
        ),
        "CourseTranscript": EntityEntry(
            description="Ed-Fi core CourseTranscript entity",
            domains=["Academic Record"],
            properties={
                "finalLetterGradeEarned": PropertyInfo(
                    description="Final letter grade.",
                    type="string",
                ),
            },
            references={},
            sub_collections={},
        ),
    }
    extensions = {
        "tpdm_studentExtension": ExtensionEntry(
            extends_entity="Student",
            source_prefix="tpdm",
            properties={
                "programGatewayIndicator": PropertyInfo(
                    description="TPDM program gateway flag.",
                    type="boolean",
                ),
            },
        ),
    }
    catalog = EdFiCatalog(
        version="3",
        entity_count=len(entities),
        extension_count=len(extensions),
        entities=entities,
        extensions=extensions,
        lookup_index=build_lookup_index(entities, extensions),
    )
    return StateSpine(
        state="TX",
        edfi_version="3",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(
            resources="http://localhost:8080/metadata/data/v3/resources/swagger.json",
        ),
        catalog=catalog,
    )


def _make_tweds_fixture() -> tuple[list[dict], list[dict]]:
    """Minimal TWEDS scrape fixture.

    Mirrors the real scrape shape: each entity carries a ``name`` +
    ``elements`` list (per-entity table rows), and the flat ``elements``
    catalog provides richer element-detail pages looked up by ``ecode``.
    """
    entities = [
        {
            "entity_name": "Student",
            "sidebar_name": "Student",
            "entity_description": "Student entity description.",
            "general_reporting_requirements": "## GR1\nReport all enrolled students.",
            "special_reporting_requirements": (
                "## SR1\nCurrently, there are no special reporting requirements."
            ),
            "data_element_reporting_requirements": (
                "## DR1\nFirstName (E0001) is required for all students."
            ),
            "elements": [
                {
                    "name": "FirstName",
                    "ecode": "E0001",
                    "data_type": "String",
                    "descriptor_table": None,
                    "is_sub_entity": False,
                },
                {
                    "name": "BirthDate",
                    "ecode": "E0002",
                    "data_type": "Date",
                    "descriptor_table": None,
                    "is_sub_entity": False,
                },
                {
                    "name": "ProgramGatewayIndicator",
                    "ecode": "E0003",
                    "data_type": "Boolean",
                    "descriptor_table": None,
                    "is_sub_entity": False,
                },
            ],
        },
        {
            "entity_name": "CourseTranscriptExt",  # TEA Ext-suffix variant
            "sidebar_name": "CourseTranscriptExt",
            "entity_description": "TEA-extended course transcript entity.",
            "general_reporting_requirements": "",
            "special_reporting_requirements": "",
            "data_element_reporting_requirements": "",
            "elements": [
                {
                    "name": "FinalLetterGradeEarned",
                    "ecode": "E0010",
                    "data_type": "String",
                    "descriptor_table": None,
                    "is_sub_entity": False,
                },
            ],
        },
        {
            # TEDS-only entity — no spine counterpart; should remain Unresolved
            "entity_name": "BasicReportingPeriodAttendance",
            "sidebar_name": "BasicReportingPeriodAttendance",
            "entity_description": "TEA attendance reporting aggregation.",
            "general_reporting_requirements": "",
            "special_reporting_requirements": "",
            "data_element_reporting_requirements": "",
            "elements": [
                {
                    "name": "NumberDaysTaught",
                    "ecode": "E0020",
                    "data_type": "Integer",
                    "descriptor_table": None,
                    "is_sub_entity": False,
                },
            ],
        },
    ]
    elements = [
        {
            "ecode": "E0001",
            "name": "FirstName",
            "definition": "The student's legal first name.",
            "data_type": "String",
            "special_instructions": (
                "Per 34 CFR 300.321(a), the FirstName field must be populated."
            ),
            "descriptor_table": None,
            "entities": ["Student"],
            "former_name": None,
            "collections_text": "PEIMS Fall",
        },
        {
            "ecode": "E0002",
            "name": "BirthDate",
            "definition": "Student's date of birth.",
            "data_type": "Date",
            "special_instructions": "",
            "descriptor_table": None,
            "entities": ["Student"],
            "former_name": None,
            "collections_text": "PEIMS Fall",
        },
        {
            "ecode": "E0003",
            "name": "ProgramGatewayIndicator",
            "definition": "TPDM program gateway indicator.",
            "data_type": "Boolean",
            "special_instructions": "",
            "descriptor_table": None,
            "entities": ["Student"],
            "former_name": None,
            "collections_text": "",
        },
        {
            "ecode": "E0010",
            "name": "FinalLetterGradeEarned",
            "definition": "Final letter grade for the course.",
            "data_type": "String",
            "special_instructions": "",
            "descriptor_table": None,
            "entities": ["CourseTranscriptExt"],
            "former_name": None,
            "collections_text": "PEIMS Summer",
        },
        {
            "ecode": "E0020",
            "name": "NumberDaysTaught",
            "definition": "Number of days taught in reporting period.",
            "data_type": "Integer",
            "special_instructions": "",
            "descriptor_table": None,
            "entities": ["BasicReportingPeriodAttendance"],
            "former_name": None,
            "collections_text": "",
        },
    ]
    return entities, elements


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self):
        """Regression guard — see `CLAUDE.md` "Two gotchas resolved"."""
        assert not isinstance(texas.run, click.Command)
        assert inspect.isfunction(texas.run)
        assert inspect.signature(texas.run).parameters == {}


class TestRecordGeneration:
    def test_emits_one_record_per_teds_pair(self):
        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        pairs = {(r.entity, r.element_name) for r in records}
        # Spine-resolved entity names (Ext suffix stripped)
        assert ("Student", "FirstName") in pairs
        assert ("Student", "BirthDate") in pairs
        assert ("Student", "ProgramGatewayIndicator") in pairs
        assert ("CourseTranscript", "FinalLetterGradeEarned") in pairs
        # TEDS-only entity stays in its raw form (no spine counterpart)
        assert ("BasicReportingPeriodAttendance", "NumberDaysTaught") in pairs

    def test_courseTranscriptExt_normalizes_to_courseTranscript(self):
        """Catalog-aware normalizer strips Ext suffix when base matches."""
        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        finalLetter = next(
            r for r in records if r.element_name == "FinalLetterGradeEarned"
        )
        assert finalLetter.entity == "CourseTranscript"
        # raw_entity preserves the source form for round-trip audit
        assert finalLetter.raw_entity == "CourseTranscriptExt"

    def test_basicReportingPeriodAttendance_stays_unresolvable_name(self):
        """TEDS-only entity has no spine counterpart; entity name preserved."""
        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        bpa = next(
            r for r in records
            if r.entity == "BasicReportingPeriodAttendance"
        )
        assert bpa.raw_entity == "BasicReportingPeriodAttendance"

    def test_records_tagged_as_tx_state_and_tweds_source(self):
        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        for r in records:
            assert r.state == "TX"
            assert r.source_document == "TWEDS v33 (TEDS)"
            assert r.source_page_or_section.startswith("Entity: ")

    def test_definition_lifted_from_element_detail(self):
        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        firstName = next(r for r in records if r.element_name == "FirstName")
        assert "legal first name" in firstName.definition_text.lower()

    def test_element_specific_rules_from_special_instructions(self):
        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        firstName = next(r for r in records if r.element_name == "FirstName")
        assert firstName.element_specific_rules
        assert "34 CFR 300.321" in firstName.element_specific_rules

    def test_regulatory_citations_extracted_from_special_instructions(self):
        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        firstName = next(r for r in records if r.element_name == "FirstName")
        assert firstName.regulatory_citations
        assert any("34 CFR" in c for c in firstName.regulatory_citations)

    def test_business_rules_text_entity_level_cached(self):
        """Entity-level GR+SR+DR populates `business_rules_text` for every
        row on that entity."""
        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        student_rows = [r for r in records if r.entity == "Student"]
        # All student rows share the same entity-level rules
        assert all(r.business_rules_text for r in student_rows)
        assert all(
            "Report all enrolled students" in (r.business_rules_text or "")
            for r in student_rows
        )

    def test_related_entities_extracted(self):
        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        firstName = next(r for r in records if r.element_name == "FirstName")
        assert firstName.related_entities == ["Student"]

    def test_domain_holds_raw_tweds_entity_name(self):
        """`domain` (Source Area) shows which TEDS entity page the row
        came from, parallel to AZ using sheet name."""
        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        finalLetter = next(
            r for r in records if r.element_name == "FinalLetterGradeEarned"
        )
        assert finalLetter.domain == "CourseTranscriptExt"


class TestAttribution:
    def test_core_match_gets_spine_type(self):
        """Canonical-type contract: matched rows overwrite TEDS type with
        spine-derived canonical type."""
        from src.ingest.shared import populate_data_types_from_spine
        from src.utils.matching import attribute_record_source

        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        attribute_record_source(records, spine)
        populate_data_types_from_spine(records, spine)

        birthDate = next(r for r in records if r.element_name == "BirthDate")
        assert birthDate.source == "core"
        # spine has format: date on birthDate → canonical "Date"
        assert birthDate.data_type == "Date"

    def test_extension_tagged_with_catalog_key(self):
        from src.utils.matching import attribute_record_source

        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        attribute_record_source(records, spine)

        gateway = next(
            r for r in records if r.element_name == "ProgramGatewayIndicator"
        )
        assert gateway.source == "extension"
        assert gateway.extension_name == "tpdm_studentExtension"

    def test_teds_only_entity_is_unresolved(self):
        from src.utils.matching import attribute_record_source

        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        attribute_record_source(records, spine)

        bpa = next(
            r for r in records if r.entity == "BasicReportingPeriodAttendance"
        )
        assert bpa.source == "unknown"
        assert bpa.extension_name is None

    def test_unresolved_keeps_teds_verbatim_data_type(self):
        """Canonical-type contract: unresolved rows preserve TEDS-provided
        type as an audit trail."""
        from src.ingest.shared import populate_data_types_from_spine
        from src.utils.matching import attribute_record_source

        spine = _make_spine()
        ents, elems = _make_tweds_fixture()
        records = build_element_records(spine, ents, elems)
        attribute_record_source(records, spine)
        populate_data_types_from_spine(records, spine)

        bpa = next(
            r for r in records if r.entity == "BasicReportingPeriodAttendance"
        )
        assert bpa.source == "unknown"
        assert bpa.data_type == "Integer"  # verbatim from TEDS


class TestEndToEndRun:
    def test_run_writes_elements_and_gap_log(self, tmp_path, monkeypatch):
        """Full run() pipeline with monkeypatched paths + TWEDS loader.

        Verifies:
        - writes tx_elements_source.json (valid StateElements)
        - writes tx_gap_log.json with canonical schema (source_coverage,
          spine_coverage, source_attribution, no teds_enrichment block)
        - counts and attribution match the fixture data
        """
        spine = _make_spine()
        spine_path = tmp_path / "data" / "spine" / "tx_spine.json"
        spine_path.parent.mkdir(parents=True, exist_ok=True)
        spine_path.write_text(spine.model_dump_json(indent=2), encoding="utf-8")

        out_dir = tmp_path / "data" / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(texas, "_TX_SPINE_PATH", spine_path)
        monkeypatch.setattr(
            texas, "_TX_ELEMENTS_OUT", out_dir / "tx_elements_source.json"
        )
        monkeypatch.setattr(
            texas, "_TX_ELEMENTS_SPINE_OUT", out_dir / "tx_elements_spine.json"
        )
        monkeypatch.setattr(texas, "_TX_GAP_OUT", out_dir / "tx_gap_log.json")
        monkeypatch.setattr(texas, "_MC_ROOT", tmp_path)
        monkeypatch.setattr(texas, "_TX_TWEDS_CACHE_DIR", tmp_path / "tweds")
        monkeypatch.setattr(
            texas.tx_tweds,
            "load_or_fetch_tweds",
            lambda *a, **kw: _make_tweds_fixture(),
        )

        run()

        elements_path = out_dir / "tx_elements_source.json"
        spine_elements_path = out_dir / "tx_elements_spine.json"
        gap_path = out_dir / "tx_gap_log.json"
        assert elements_path.exists()
        assert spine_elements_path.exists(), (
            "run() must write the spine-lens artifact alongside the source lens "
            "(Option 3b dual-lens contract)"
        )
        assert gap_path.exists()

        elements = StateElements.model_validate_json(
            elements_path.read_text(encoding="utf-8")
        )
        assert elements.state == "TX"
        assert elements.element_count == len(elements.elements)
        assert elements.element_count > 0

        # Spine lens parses as valid StateElements and carries the canonical
        # spine-emit positions (one record per logical slot) plus any
        # spine-missing source rows appended as the audit-trail hybrid tail.
        spine_elements = StateElements.model_validate_json(
            spine_elements_path.read_text(encoding="utf-8")
        )
        assert spine_elements.state == "TX"
        assert spine_elements.element_count == len(spine_elements.elements)
        assert spine_elements.element_count > 0
        # Hybrid append: the TEDS-only row (BasicReportingPeriodAttendance)
        # should land in the spine lens as source="unknown", documented=True.
        unknown_rows = [r for r in spine_elements.elements if r.source == "unknown"]
        assert any(
            r.element_name == "NumberDaysTaught" and r.documented
            for r in unknown_rows
        )
        # And core/extension spine slots carry `documented` reflecting whether
        # the source doc populated them.
        docd_pairs = {
            (r.entity, r.element_name)
            for r in spine_elements.elements
            if r.documented and r.source != "unknown"
        }
        assert ("Student", "firstName") in docd_pairs

        gap = json.loads(gap_path.read_text(encoding="utf-8"))
        # Canonical gap-log schema — no teds_enrichment sub-block
        assert "teds_enrichment" not in gap
        assert "source_coverage" in gap
        assert "spine_coverage" in gap
        assert "source_attribution" in gap

        # Fixture has 5 TEDS rows: 3 core + 1 extension + 1 unresolved
        #   core: Student.FirstName, Student.BirthDate, CourseTranscript.FinalLetterGradeEarned
        #   extension: Student.ProgramGatewayIndicator (tpdm_studentExtension)
        #   unresolved: BasicReportingPeriodAttendance.NumberDaysTaught (TEDS-only entity)
        attribution = gap["source_attribution"]
        assert attribution["core"] == 3
        assert attribution["extension"] == 1
        assert attribution["unknown"] == 1

        # Source coverage: matched = core + extension
        assert gap["source_coverage"]["matched"] == 4
        assert gap["source_coverage"]["total"] == 5

    def test_run_raises_when_spine_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            texas, "_TX_SPINE_PATH", tmp_path / "missing_spine.json"
        )
        with pytest.raises(FileNotFoundError, match="mc spine fetch"):
            run()

    def test_run_raises_when_tweds_unavailable(self, tmp_path, monkeypatch):
        """Previous behavior: silent spine-only fallback when TEDS was
        missing. New contract (Option B): TEDS is the source; if it's
        missing, ingest fails loud."""
        spine = _make_spine()
        spine_path = tmp_path / "data" / "spine" / "tx_spine.json"
        spine_path.parent.mkdir(parents=True, exist_ok=True)
        spine_path.write_text(spine.model_dump_json(indent=2), encoding="utf-8")

        monkeypatch.setattr(texas, "_TX_SPINE_PATH", spine_path)
        monkeypatch.setattr(texas, "_TX_TWEDS_CACHE_DIR", tmp_path / "no_tweds")
        monkeypatch.setattr(
            texas.tx_tweds, "load_or_fetch_tweds", lambda *a, **kw: None
        )
        with pytest.raises(RuntimeError, match="TWEDS cache"):
            run()
