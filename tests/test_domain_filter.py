"""Unit tests for the SIS-never-populated domain filter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.ingest import domain_scope
from src.ingest.domain_filter import (
    FILTERED_DOMAINS,
    PLACEHOLDER_LABELS,
    entity_filter_domain,
    placeholder_note,
)
from src.ingest.domain_scope import DomainSource
from src.ingest.shared import assemble_spine_driven, build_source_index
from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
)
from src.models.element import ElementRecord
from src.models.spine import SpineSourceURLs, StateSpine


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch):
    """Isolate raw-filter-logic tests from the live ``domain_scope`` seed.

    These tests build ``state='TX'`` spines and assert the unconditional
    collapse behavior; the shipped seed enables TX/IN Assessment, which would
    otherwise un-filter those rows. Per-state-enablement tests
    (``TestPerStateUnfilter``) re-set ``DOMAIN_SOURCES`` themselves and that
    later monkeypatch wins.
    """
    monkeypatch.setattr(domain_scope, "DOMAIN_SOURCES", ())


def _spine_with(entities: dict, extensions: dict | None = None) -> StateSpine:
    catalog = EdFiCatalog(
        version="4.0.0",
        entity_count=len(entities),
        extension_count=len(extensions or {}),
        entities=entities,
        extensions=extensions or {},
    )
    return StateSpine(
        state="TX",
        edfi_version="4.0.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(resources="https://example/resources.json"),
        catalog=catalog,
    )


class TestEntityFilterDomain:
    def test_assessment_entity_with_all_filtered_domains_is_filtered(self):
        spine = _spine_with({
            "Assessment": EntityEntry(
                description="Assessment",
                domains=["Assessment", "AssessmentMetadata"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("Assessment", spine) == "Assessment"

    def test_survey_entity_filtered(self):
        spine = _spine_with({
            "SurveyResponse": EntityEntry(
                description="Survey response",
                domains=["Survey"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("SurveyResponse", spine) == "Survey"

    def test_standards_entity_filtered(self):
        spine = _spine_with({
            "AssessmentScoreRangeLearningStandard": EntityEntry(
                description="Scope",
                domains=["Assessment"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        # Only `Assessment` domain → collapses to Assessment label.
        assert entity_filter_domain("AssessmentScoreRangeLearningStandard", spine) == "Assessment"

    def test_gradebook_entity_filtered(self):
        spine = _spine_with({
            "GradebookEntryOnly": EntityEntry(
                description="GradebookEntry",
                domains=["Gradebook"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("GradebookEntryOnly", spine) == "Gradebook"

    def test_intervention_entity_filtered(self):
        spine = _spine_with({
            "InterventionStudy": EntityEntry(
                description="Intervention study",
                domains=["Intervention"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("InterventionStudy", spine) == "Intervention"

    def test_mixed_domain_entity_is_not_filtered(self):
        # Student participates in Assessment AND Enrollment — Ed-Fi core entity,
        # must stay in the spine lens regardless of its Assessment membership.
        spine = _spine_with({
            "Student": EntityEntry(
                description="Student",
                domains=["StudentIdentificationAndDemographics", "Assessment", "Survey"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("Student", spine) is None

    def test_explicit_concept_anchor_entities_filtered_despite_mixed_domains(self):
        # LearningStandard ships with 7 Ed-Fi domain tags (Assessment,
        # StudentAcademicRecord, TeachingAndLearning, CourseCatalog,
        # Gradebook, ReportCard, Standards). The domain-subset rule would
        # keep it; the explicit concept-anchor list filters it.
        spine = _spine_with({
            "LearningStandard": EntityEntry(
                description="Learning standard",
                domains=[
                    "Assessment", "StudentAcademicRecord", "TeachingAndLearning",
                    "CourseCatalog", "Gradebook", "ReportCard", "Standards",
                ],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
            "GradebookEntry": EntityEntry(
                description="Gradebook entry",
                domains=["StudentAcademicRecord", "Gradebook"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
            "Cohort": EntityEntry(
                description="Cohort",
                domains=["Intervention", "StudentCohort"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("LearningStandard", spine) == "LearningStandard"
        assert entity_filter_domain("GradebookEntry", spine) == "Gradebook"
        assert entity_filter_domain("Cohort", spine) == "Intervention"

    def test_entity_without_domains_falls_through_to_prefix_fallback(self):
        # Extension entity not in the committed domain map — trigger the
        # fallback path.
        spine = _spine_with({
            "tx_AssessmentExt": EntityEntry(
                description="TX Assessment extension",
                domains=[],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("tx_AssessmentExt", spine) is None  # 'tx_' prefix, not filtered
        # Entity name starting with filtered prefix:
        spine2 = _spine_with({
            "AssessmentShell": EntityEntry(
                description="Assessment shell",
                domains=[],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("AssessmentShell", spine2) == "Assessment"

    def test_non_filtered_entity_with_empty_domains_is_not_filtered(self):
        spine = _spine_with({
            "Calendar": EntityEntry(
                description="Calendar",
                domains=[],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("Calendar", spine) is None

    def test_unknown_entity_is_not_filtered(self):
        spine = _spine_with({})
        assert entity_filter_domain("SomeRandomEntity", spine) is None

    def test_fallback_prefix_is_disjoint_label_set(self):
        # Sanity: every prefix fallback maps to one of the placeholder labels.
        for label in PLACEHOLDER_LABELS:
            assert isinstance(label, str)
        # FILTERED_DOMAINS must be a subset of Ed-Fi's published domain labels.
        expected = {
            "Assessment", "AssessmentMetadata", "AssessmentRegistration",
            "StudentAssessment", "Survey", "Standards", "Gradebook", "Intervention",
        }
        assert FILTERED_DOMAINS == frozenset(expected)


class TestPlaceholderNote:
    def test_mentions_domain_label(self):
        note = placeholder_note("Assessment")
        assert "Assessment" in note
        assert "SIS vendors" in note


class TestPerStateUnfilter:
    """The `domain_scope` registry lifts the spine-lens collapse per (state,
    domain). The `_spine_with` helper builds state='TX' spines."""

    def _assess_spine(self) -> StateSpine:
        return _spine_with({
            "Assessment": EntityEntry(
                description="Assessment",
                domains=["Assessment", "AssessmentMetadata"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
            "AssessmentAdministration": EntityEntry(
                description="Assessment administration",
                domains=["AssessmentRegistration"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })

    def test_collapses_when_state_not_registered(self, monkeypatch):
        # Empty-registry invariant: default behavior unchanged.
        monkeypatch.setattr(domain_scope, "DOMAIN_SOURCES", ())
        spine = self._assess_spine()
        assert entity_filter_domain("Assessment", spine) == "Assessment"
        assert entity_filter_domain("AssessmentAdministration", spine) == "Assessment"

    def test_assessment_enabled_survives_but_ar_still_collapses(self, monkeypatch):
        monkeypatch.setattr(
            domain_scope, "DOMAIN_SOURCES",
            (DomainSource(state="TX", domain="Assessment"),),
        )
        spine = self._assess_spine()
        assert entity_filter_domain("Assessment", spine) is None
        # AssessmentRegistration is a distinct key — not enabled here.
        assert entity_filter_domain("AssessmentAdministration", spine) == "Assessment"

    def test_assessment_registration_enabled_independently(self, monkeypatch):
        monkeypatch.setattr(
            domain_scope, "DOMAIN_SOURCES",
            (DomainSource(state="TX", domain="AssessmentRegistration"),),
        )
        spine = self._assess_spine()
        # Plain Assessment is not enabled → still collapses.
        assert entity_filter_domain("Assessment", spine) == "Assessment"
        # AR entity un-filtered.
        assert entity_filter_domain("AssessmentAdministration", spine) is None

    def test_enablement_is_scoped_to_the_named_state(self, monkeypatch):
        # WI enabled, but the spine is TX → no effect.
        monkeypatch.setattr(
            domain_scope, "DOMAIN_SOURCES",
            (DomainSource(state="WI", domain="Assessment"),),
        )
        spine = self._assess_spine()
        assert entity_filter_domain("Assessment", spine) == "Assessment"

    def test_assessment_enable_does_not_leak_explicit_anchor_entities(self, monkeypatch):
        # LearningStandard carries an `Assessment` domain tag among several
        # non-filtered domains, but is filtered via the explicit concept-anchor
        # list. Enabling Assessment must NOT un-filter it (regression: the
        # naive domain-membership check leaked it back into the spine lens).
        monkeypatch.setattr(
            domain_scope, "DOMAIN_SOURCES",
            (DomainSource(state="TX", domain="Assessment"),),
        )
        spine = _spine_with({
            "LearningStandard": EntityEntry(
                description="Learning standard",
                domains=[
                    "Assessment", "StudentAcademicRecord", "TeachingAndLearning",
                    "CourseCatalog", "Gradebook", "ReportCard", "Standards",
                ],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
            "GradebookEntry": EntityEntry(
                description="Gradebook entry",
                domains=["StudentAcademicRecord", "Gradebook"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("LearningStandard", spine) == "LearningStandard"
        assert entity_filter_domain("GradebookEntry", spine) == "Gradebook"

    def test_staff_and_finance_never_filtered_regardless_of_registry(self, monkeypatch):
        monkeypatch.setattr(domain_scope, "DOMAIN_SOURCES", ())
        spine = _spine_with({
            "StaffSectionAssociation": EntityEntry(
                description="Staff section",
                domains=["Staff"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
            "ChartOfAccount": EntityEntry(
                description="Chart of account",
                domains=["Finance"],
                properties={"id": PropertyInfo(type="string", is_identity=True)},
            ),
        })
        assert entity_filter_domain("StaffSectionAssociation", spine) is None
        assert entity_filter_domain("ChartOfAccount", spine) is None


def _build_mixed_spine() -> StateSpine:
    student = EntityEntry(
        description="Student",
        domains=["StudentIdentificationAndDemographics"],
        properties={
            "studentUniqueId": PropertyInfo(type="string", is_identity=True),
            "firstName": PropertyInfo(type="string"),
        },
    )
    assessment = EntityEntry(
        description="Assessment",
        domains=["Assessment", "AssessmentMetadata"],
        properties={
            "assessmentIdentifier": PropertyInfo(type="string", is_identity=True),
            "title": PropertyInfo(type="string"),
            "maxRawScore": PropertyInfo(type="integer"),
        },
    )
    survey_response = EntityEntry(
        description="Survey response",
        domains=["Survey"],
        properties={
            "surveyResponseIdentifier": PropertyInfo(type="string", is_identity=True),
            "namespace": PropertyInfo(type="string"),
        },
    )
    intervention = EntityEntry(
        description="Intervention",
        domains=["Intervention"],
        properties={
            "interventionIdentificationCode": PropertyInfo(type="string", is_identity=True),
        },
    )
    return _spine_with({
        "Student": student,
        "Assessment": assessment,
        "SurveyResponse": survey_response,
        "InterventionStudy": intervention,
    })


class TestAssembleSpineDrivenAppliesFilter:
    def test_undocumented_filtered_entity_rows_absent_from_spine_lens(self):
        # Soft-filter contract: when no source rows document a filtered
        # entity, the entire entity collapses into a placeholder.
        spine = _build_mixed_spine()
        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=build_source_index([]),
            spine_missing_source_rows=[],
            state="TX",
            edfi_version="4.0.0",
            source_document=None,
        )
        entities = {r.entity for r in records if r.source != "filtered"}
        assert "Student" in entities  # kept
        assert "Assessment" not in entities
        assert "SurveyResponse" not in entities
        assert "InterventionStudy" not in entities

    def test_documented_filtered_entity_rows_retained(self):
        # A source row documenting Assessment.title survives the filter as
        # a real signal of state intent. The placeholder still emits for
        # the rest of the Assessment slots.
        spine = _build_mixed_spine()
        source_rows = [
            ElementRecord(
                state="TX",
                edfi_version="4.0",
                domain="Assessment",
                entity="Assessment",
                element_name="title",
                definition_text="The TEDS-defined title of the assessment.",
                source="core",
                documented=True,
            ),
        ]
        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=build_source_index(source_rows),
            spine_missing_source_rows=[],
            state="TX",
            edfi_version="4.0.0",
            source_document="TWEDS",
        )
        # The documented Assessment.title row is present and retains its
        # core source attribution.
        survivor = next(
            (r for r in records if r.entity == "Assessment" and r.element_name == "title"),
            None,
        )
        assert survivor is not None
        assert survivor.source == "core"
        assert survivor.documented is True
        # Non-documented Assessment slots (assessmentIdentifier,
        # maxRawScore) collapsed.
        assessment_rows = {r.element_name for r in records if r.entity == "Assessment"}
        assert "assessmentIdentifier" not in assessment_rows
        assert "maxRawScore" not in assessment_rows
        # Placeholder still emits because slots were dropped.
        placeholder = next(
            (r for r in records if r.source == "filtered" and r.domain == "Assessment"),
            None,
        )
        assert placeholder is not None

    def test_single_placeholder_per_filtered_domain(self):
        spine = _build_mixed_spine()
        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=build_source_index([]),
            spine_missing_source_rows=[],
            state="TX",
            edfi_version="4.0.0",
            source_document=None,
        )
        placeholders = [r for r in records if r.source == "filtered"]
        labels = [r.domain for r in placeholders]
        # Three filtered domains represented; each appears exactly once.
        assert sorted(labels) == ["Assessment", "Intervention", "Survey"]
        # Entity naming convention sanity.
        assert {r.entity for r in placeholders} == {
            "Assessment (filtered)",
            "Intervention (filtered)",
            "Survey (filtered)",
        }
        # element_name uniform, data_type cleared, documented=False, note set.
        for r in placeholders:
            assert r.element_name == "(filtered)"
            assert r.data_type is None
            assert r.documented is False
            assert "SIS vendors" in r.definition_text

    def test_hybrid_append_on_filtered_entity_is_retained(self):
        # Soft-filter contract: hybrid-append rows are by-construction
        # documented (they came from the state's source doc). Even on a
        # filtered-domain entity, that's real state intent and survives.
        spine = _build_mixed_spine()
        audit_row = ElementRecord(
            state="TX",
            edfi_version="4.0",
            domain="Audit",
            entity="ObjectiveAssessmentExtra",
            element_name="customField",
            definition_text="TEDS-only.",
            source="unknown",
            documented=True,
        )
        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=build_source_index([]),
            spine_missing_source_rows=[audit_row],
            state="TX",
            edfi_version="4.0.0",
            source_document=None,
        )
        assert any(
            r.entity == "ObjectiveAssessmentExtra" for r in records
        )
        # Placeholder still emits — Assessment slots in spine were undocumented.
        assert any(
            r.source == "filtered" and r.domain == "Assessment" for r in records
        )

    def test_hybrid_append_on_non_filtered_entity_survives(self):
        spine = _build_mixed_spine()
        audit_row = ElementRecord(
            state="TX",
            edfi_version="4.0",
            domain="Audit",
            entity="LegacyTxEntity",
            element_name="legacyField",
            definition_text="Legacy.",
            source="unknown",
            documented=True,
        )
        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=build_source_index([]),
            spine_missing_source_rows=[audit_row],
            state="TX",
            edfi_version="4.0.0",
            source_document=None,
        )
        assert any(r.entity == "LegacyTxEntity" for r in records)

    def test_undocumented_extension_on_filtered_parent_is_filtered(self):
        spine = _spine_with(
            {
                "Assessment": EntityEntry(
                    description="Assessment",
                    domains=["Assessment", "AssessmentMetadata"],
                    properties={
                        "assessmentIdentifier": PropertyInfo(type="string", is_identity=True),
                    },
                ),
            },
            extensions={
                "tx_assessmentExtension": ExtensionEntry(
                    extends_entity="Assessment",
                    source_prefix="tx",
                    properties={
                        "customField": PropertyInfo(type="string"),
                    },
                ),
            },
        )
        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=build_source_index([]),
            spine_missing_source_rows=[],
            state="TX",
            edfi_version="4.0.0",
            source_document=None,
        )
        # With no source documenting any Assessment slot, no core or
        # extension row for Assessment survives.
        non_placeholder = [r for r in records if r.source != "filtered"]
        assert not any(r.entity == "Assessment" for r in non_placeholder)
        # One Assessment placeholder.
        placeholders = [r for r in records if r.source == "filtered"]
        assert [r.domain for r in placeholders] == ["Assessment"]
