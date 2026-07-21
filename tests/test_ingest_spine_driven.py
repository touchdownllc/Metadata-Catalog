"""Unit tests for the spine-driven assembler (Option 3b, Phase 3).

Tests the new shared-module helpers:

- `canonical_spine_emit_keys(spine)` — dedup alias-variant emits.
- `build_source_index(source_records)` — alias-expanded source-facts lookup.
- `assemble_spine_driven(...)` — spine-enumerated records, source-enriched,
  with spine-missing hybrid append.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.ingest.shared import (
    SourceFacts,
    assemble_spine_driven,
    build_source_index,
    canonical_spine_emit_keys,
)
from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
    ReferenceInfo,
    SubCollectionInfo,
)
from src.models.element import ElementRecord
from src.models.spine import SpineSourceURLs, StateSpine


def _make_spine_with_refs_and_subs() -> StateSpine:
    """Build a spine with a real mix of props / refs / sub-collections +
    one state extension, to exercise the full canonical-emit code path."""
    student = EntityEntry(
        description="Student",
        domains=["Student Enrollment"],
        properties={
            "studentUniqueId": PropertyInfo(
                description="A unique alphanumeric code assigned to a student.",
                type="string", is_identity=True,
            ),
            "firstName": PropertyInfo(
                description="A name given to an individual at birth, baptism, or during another naming ceremony.",
                type="string",
            ),
            "birthDate": PropertyInfo(
                description="The month, day, and year on which an individual was born.",
                type="string", format="date",
            ),
            "genderDescriptor": PropertyInfo(
                description="A person's gender.",
                type="string",
            ),
        },
        references={
            "schoolReference": ReferenceInfo(
                entity="School",
                key_properties={
                    "schoolId": PropertyInfo(
                        description="The identifier assigned to a school by the State Education Agency (SEA).",
                        type="integer",
                    ),
                },
            ),
        },
        sub_collections={
            "addresses": SubCollectionInfo(
                sub_entity="StudentAddress",
                properties={
                    "streetNumberName": PropertyInfo(
                        description="The street number and street name or post office box number of an address.",
                        type="string",
                    ),
                    "city": PropertyInfo(
                        description="The name of the city in which an address is located.",
                        type="string",
                    ),
                },
            ),
        },
    )
    school = EntityEntry(
        description="School",
        domains=["School"],
        properties={
            "schoolId": PropertyInfo(
                description="The identifier assigned to a school by the State Education Agency (SEA).",
                type="integer", is_identity=True,
            ),
            "nameOfInstitution": PropertyInfo(
                description="The full, legally accepted name of the institution.",
                type="string",
            ),
        },
    )
    student_ext = ExtensionEntry(
        extends_entity="Student",
        source_prefix="tx",
        properties={
            "txCustomField": PropertyInfo(
                description="A TEA-specific extension field on Student.",
                type="string",
            ),
        },
    )
    catalog = EdFiCatalog(
        version="4.0.0",
        entity_count=2,
        extension_count=1,
        entities={"Student": student, "School": school},
        extensions={"tx_studentExtension": student_ext},
    )
    return StateSpine(
        state="TX",
        edfi_version="4.0.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(resources="https://example/resources.json"),
        catalog=catalog,
    )


class TestCanonicalSpineEmitKeys:
    def test_emits_one_per_logical_slot(self):
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        keys = {(e.entity.lower(), e.element_name.lower()) for e in emits}
        # Canonical emits = one per logical slot. Alias forms
        # (`genderDescriptorId`, bare descriptor, reference bare-prefix,
        # sub-entity Pascal/camel) live in the per-emit alias sink, NOT
        # as separate emits — this keeps spine-lens total cardinality
        # anchored to the count of logical positions the stakeholder
        # expects to see.
        #
        # Reference / Collection wrapper slots are filtered from the
        # spine lens (see filter at tail of canonical_spine_emit_keys) —
        # their FK key props / sub-properties carry the reviewer content.
        # Student:
        #   studentUniqueId, firstName, birthDate,
        #   genderDescriptor, schoolId (FK flatten),
        #   streetNumberName, city (sub-props)     (7)
        # School: schoolId, nameOfInstitution      (2)
        # Extension: Student.txCustomField         (1)
        # Total: 10
        assert len(emits) == 10
        assert len(keys) == len(emits), "no duplicate canonical keys"
        # Sanity: no Reference / Collection wrappers slipped through.
        assert all(e.data_type not in ("Reference", "Collection") for e in emits)

    def test_reference_slot_excluded_from_spine_lens(self):
        # `schoolReference` (the wrapper slot) is filtered out. The
        # flattened FK key property `schoolId` is what reviewers score.
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        names = {(e.entity, e.element_name) for e in emits}
        assert ("Student", "schoolReference") not in names
        assert ("Student", "schoolId") in names

    def test_collection_slot_excluded_from_spine_lens(self):
        # `addresses` (the collection wrapper) is filtered out. The
        # sub-properties (`city`, `streetNumberName`) emit at parent level.
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        names = {(e.entity, e.element_name) for e in emits}
        assert ("Student", "addresses") not in names
        assert ("Student", "city") in names
        assert ("Student", "streetNumberName") in names

    def test_descriptor_type_inferred_from_suffix(self):
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        d = next(e for e in emits if e.element_name == "genderDescriptor")
        assert d.data_type == "Descriptor"

    def test_date_format_promoted_to_Date(self):
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        bd = next(e for e in emits if e.element_name == "birthDate")
        assert bd.data_type == "Date"

    def test_extension_emit_carries_extension_name(self):
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        ext = next(e for e in emits if e.element_name == "txCustomField")
        assert ext.source == "extension"
        assert ext.extension_name == "tx_studentExtension"
        assert ext.entity == "Student"  # parent entity

    def test_deterministic_order(self):
        spine = _make_spine_with_refs_and_subs()
        emits1 = canonical_spine_emit_keys(spine)
        emits2 = canonical_spine_emit_keys(spine)
        assert [
            (e.entity, e.element_name) for e in emits1
        ] == [
            (e.entity, e.element_name) for e in emits2
        ]

    def test_sub_collection_property_emitted_under_parent(self):
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        # Sub-collection properties are emitted under the PARENT entity
        # (matches spine.element_keys() precedent — source docs flatten).
        city = next(
            (e for e in emits if e.entity == "Student" and e.element_name == "city"),
            None,
        )
        assert city is not None
        assert city.data_type == "String"

    def test_education_organization_subtype_aliases_registered(self):
        # `educationOrganizationReference` targets the generalized
        # EducationOrganization; TEDS/TEA source docs name the concrete
        # subtypes (School, LEA, SEA...). Even with the Reference-slot
        # filter active, the alias-sink registrations still happen at
        # emit time; they're orphaned (no longer queried, since the ref
        # slot isn't in `emits`) but kept alive so future code that
        # wants to route subtype-named source rows to the flattened FK
        # key prop can read them.
        parent = EntityEntry(
            description="Parent",
            domains=["Education Organization"],
            properties={"id": PropertyInfo(type="integer", is_identity=True)},
            references={
                "educationOrganizationReference": ReferenceInfo(
                    entity="EducationOrganization",
                    key_properties={
                        "educationOrganizationId": PropertyInfo(type="integer"),
                    },
                ),
            },
        )
        catalog = EdFiCatalog(
            version="4.0.0",
            entity_count=1,
            extension_count=0,
            entities={"Parent": parent},
            extensions={},
        )
        spine = StateSpine(
            state="TX",
            edfi_version="4.0.0",
            fetched_at=datetime.now(timezone.utc),
            source_urls=SpineSourceURLs(resources="https://example/resources.json"),
            catalog=catalog,
        )
        alias_sink: dict[tuple[str, str], set[tuple[str, str]]] = {}
        emits = canonical_spine_emit_keys(spine, _alias_sink=alias_sink)
        # The ref wrapper slot itself is filtered out of the spine lens
        # — Parent only surfaces the flattened FK key prop.
        parent_emits = {e.element_name for e in emits if e.entity == "Parent"}
        assert "educationOrganizationReference" not in parent_emits
        assert "educationOrganizationId" in parent_emits
        # Subtype aliases still sit on the (now-virtual) ref slot in the
        # sink — belt-and-suspenders check that the registration logic
        # wasn't gutted along with the emit filter.
        ref_slot = ("parent", "educationorganizationreference")
        alias_elements = {alias[1] for alias in alias_sink.get(ref_slot, set())}
        assert "school" in alias_elements
        assert "localeducationagency" in alias_elements
        assert "stateeducationagencyid" in alias_elements
        assert "educationservicecenter" in alias_elements

    def test_student_program_association_template_propagated(self):
        # MN and other states register concrete `Student*ProgramAssociation`
        # entities only as extensions whose `extends_entity` is never in
        # `catalog.entities`. Template-propagate the base's element keys so
        # source rows on concrete program associations resolve.
        spa = EntityEntry(
            description="StudentProgramAssociation",
            domains=["Student Enrollment"],
            properties={
                "programType": PropertyInfo(type="string"),
                "programName": PropertyInfo(type="string"),
                "studentUniqueId": PropertyInfo(type="string", is_identity=True),
            },
        )
        concrete = ExtensionEntry(
            extends_entity="StudentHomelessProgramAssociation",
            source_prefix="mn",
            properties={"extraField": PropertyInfo(type="string")},
        )
        catalog = EdFiCatalog(
            version="4.0.0",
            entity_count=1,
            extension_count=1,
            entities={"StudentProgramAssociation": spa},
            extensions={"mn_studentHomelessProgramAssociationExtension": concrete},
        )
        spine = StateSpine(
            state="MN",
            edfi_version="4.0.0",
            fetched_at=datetime.now(timezone.utc),
            source_urls=SpineSourceURLs(resources="https://example/resources.json"),
            catalog=catalog,
        )
        emits = canonical_spine_emit_keys(spine)
        concrete_emits = {e.element_name for e in emits if e.entity == "StudentHomelessProgramAssociation"}
        # Template-propagated identity fields should surface on the concrete entity.
        assert "programType" in concrete_emits
        assert "programName" in concrete_emits
        assert "studentUniqueId" in concrete_emits
        # Extension's own props remain, too.
        assert "extraField" in concrete_emits

class TestSpineEmitCarriesDescription:
    """Descriptions from PropertyInfo / SubCollectionInfo flow onto each
    SpineEmit so the spine lens can surface the Ed-Fi spec text."""

    def test_property_description_flows_to_emit(self):
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        first = next(e for e in emits if e.entity == "Student" and e.element_name == "firstName")
        assert first.description.startswith("A name given to an individual at birth")
        bd = next(e for e in emits if e.entity == "Student" and e.element_name == "birthDate")
        assert bd.description == "The month, day, and year on which an individual was born."
        gd = next(e for e in emits if e.entity == "Student" and e.element_name == "genderDescriptor")
        assert gd.description == "A person's gender."

    def test_fk_key_property_description_flows_to_parent_emit(self):
        # When the reference's key property is flattened onto the parent
        # entity, the key-prop description comes along.
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        sid = next(e for e in emits if e.entity == "Student" and e.element_name == "schoolId")
        assert sid.description.startswith("The identifier assigned to a school")

    def test_sub_collection_property_description_flows(self):
        # `addresses.city` — emitted at parent Student level — carries the
        # sub-property's own description.
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        city = next(e for e in emits if e.entity == "Student" and e.element_name == "city")
        assert city.description.startswith("The name of the city")

    def test_extension_property_description_flows(self):
        spine = _make_spine_with_refs_and_subs()
        emits = canonical_spine_emit_keys(spine)
        ext = next(e for e in emits if e.element_name == "txCustomField")
        assert ext.description == "A TEA-specific extension field on Student."

    def test_template_propagation_carries_description(self):
        # StudentProgramAssociation base description propagates onto each
        # concrete `*ProgramAssociation` subclass during propagation.
        spa = EntityEntry(
            description="StudentProgramAssociation",
            domains=["Student Enrollment"],
            properties={
                "programType": PropertyInfo(
                    description="The type of program.",
                    type="string",
                ),
                "studentUniqueId": PropertyInfo(
                    description="A unique alphanumeric code assigned to a student.",
                    type="string", is_identity=True,
                ),
            },
        )
        concrete = ExtensionEntry(
            extends_entity="StudentHomelessProgramAssociation",
            source_prefix="mn",
            properties={
                "extraField": PropertyInfo(description="MN-only extra.", type="string"),
            },
        )
        catalog = EdFiCatalog(
            version="4.0.0",
            entity_count=1,
            extension_count=1,
            entities={"StudentProgramAssociation": spa},
            extensions={"mn_studentHomelessProgramAssociationExtension": concrete},
        )
        spine = StateSpine(
            state="MN",
            edfi_version="4.0.0",
            fetched_at=datetime.now(timezone.utc),
            source_urls=SpineSourceURLs(resources="https://example/resources.json"),
            catalog=catalog,
        )
        emits = canonical_spine_emit_keys(spine)
        concrete_emits = {
            e.element_name: e
            for e in emits
            if e.entity == "StudentHomelessProgramAssociation"
        }
        assert concrete_emits["programType"].description == "The type of program."


class TestBuildSourceIndex:
    def test_indexes_by_canonical_key(self):
        records = [
            ElementRecord(
                state="TX",
                edfi_version="4.0",
                domain="Student Enrollment",
                entity="Student",
                element_name="firstName",
                definition_text="Student's first name.",
                business_rules_text="Required.",
                documented=True,
            ),
        ]
        index = build_source_index(records)
        # key form mirrors record_match_keys — lowercased tuple
        assert (("student", "firstname")) in index
        facts = index[("student", "firstname")]
        assert facts.definition_text == "Student's first name."
        assert facts.business_rules_text == "Required."

    def test_first_record_wins_on_key_collision(self):
        records = [
            ElementRecord(
                state="TX",
                edfi_version="4.0",
                domain="Student Enrollment",
                entity="Student",
                element_name="firstName",
                definition_text="first",
                documented=True,
            ),
            ElementRecord(
                state="TX",
                edfi_version="4.0",
                domain="Student Enrollment",
                entity="Student",
                element_name="firstName",
                definition_text="second",
                documented=True,
            ),
        ]
        index = build_source_index(records)
        assert index[("student", "firstname")].definition_text == "first"


class TestAssembleSpineDriven:
    def _source_records(self) -> list[ElementRecord]:
        """Four source rows: 2 that land on the spine + 2 that don't."""
        return [
            ElementRecord(
                state="TX",
                edfi_version="4.0",
                domain="Student Enrollment",
                entity="Student",
                element_name="firstName",
                definition_text="Student's first name.",
                business_rules_text="Required.",
                source="core",
                documented=True,
            ),
            ElementRecord(
                state="TX",
                edfi_version="4.0",
                domain="Student Enrollment",
                entity="Student",
                element_name="txCustomField",
                definition_text="TX custom.",
                source="extension",
                extension_name="tx_studentExtension",
                documented=True,
            ),
            ElementRecord(
                state="TX",
                edfi_version="4.0",
                domain="Audit Trail",
                entity="LegacyTxEntity",
                element_name="someLegacyField",
                definition_text="Legacy row not in spine.",
                source="unknown",
                documented=True,
            ),
            ElementRecord(
                state="TX",
                edfi_version="4.0",
                domain="Audit Trail",
                entity="LegacyTxEntity",
                element_name="anotherLegacyField",
                definition_text="Another legacy row.",
                source="unknown",
                documented=True,
            ),
        ]

    def test_emits_one_record_per_canonical_spine_key(self):
        spine = _make_spine_with_refs_and_subs()
        source = self._source_records()
        source_index = build_source_index(source)
        unmatched = [r for r in source if r.source == "unknown"]

        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=source_index,
            spine_missing_source_rows=unmatched,
            state="TX",
            edfi_version="4.0.0",
            source_document="test.xlsx",
        )
        # 10 canonical spine emits (Ref/Collection wrappers filtered)
        # + 2 unknown appends = 12
        assert len(records) == 12

    def test_documented_flag_set_on_source_hits(self):
        spine = _make_spine_with_refs_and_subs()
        source = self._source_records()
        source_index = build_source_index(source)

        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=source_index,
            spine_missing_source_rows=[],
            state="TX",
            edfi_version="4.0.0",
            source_document="test.xlsx",
        )
        documented = [r for r in records if r.documented]
        # Student.firstName + Student.txCustomField both landed on spine
        assert len(documented) == 2
        docd_pairs = {(r.entity, r.element_name) for r in documented}
        assert ("Student", "firstName") in docd_pairs
        assert ("Student", "txCustomField") in docd_pairs

    def test_undocumented_records_have_empty_definition(self):
        spine = _make_spine_with_refs_and_subs()
        source_index = build_source_index([])

        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=source_index,
            spine_missing_source_rows=[],
            state="TX",
            edfi_version="4.0.0",
            source_document=None,
        )
        # No source ⇒ nothing documented
        assert all(not r.documented for r in records)
        assert all(r.definition_text == "" for r in records)

    def test_spine_missing_source_rows_are_appended_as_unknown(self):
        spine = _make_spine_with_refs_and_subs()
        source = self._source_records()
        source_index = build_source_index(source)
        unmatched = [r for r in source if r.source == "unknown"]

        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=source_index,
            spine_missing_source_rows=unmatched,
            state="TX",
            edfi_version="4.0.0",
            source_document="test.xlsx",
        )
        unknowns = [r for r in records if r.source == "unknown"]
        assert len(unknowns) == 2
        # Audit-trail rows are documented by construction (they came from source)
        assert all(r.documented for r in unknowns)
        names = {r.element_name for r in unknowns}
        assert names == {"someLegacyField", "anotherLegacyField"}

    def test_spine_derived_types_populated(self):
        spine = _make_spine_with_refs_and_subs()
        source_index = build_source_index([])

        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=source_index,
            spine_missing_source_rows=[],
            state="TX",
            edfi_version="4.0.0",
            source_document=None,
        )
        by_pair = {(r.entity, r.element_name): r for r in records}
        assert by_pair[("Student", "birthDate")].data_type == "Date"
        assert by_pair[("Student", "genderDescriptor")].data_type == "Descriptor"
        # Reference-typed wrapper slots (schoolReference) are filtered
        # out; the flattened FK key prop carries its spine-derived type.
        assert ("Student", "schoolReference") not in by_pair
        assert by_pair[("Student", "schoolId")].data_type == "Integer"

    def test_extension_records_carry_extension_name(self):
        spine = _make_spine_with_refs_and_subs()
        source_index = build_source_index([])

        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=source_index,
            spine_missing_source_rows=[],
            state="TX",
            edfi_version="4.0.0",
            source_document=None,
        )
        ext = next(r for r in records if r.element_name == "txCustomField")
        assert ext.source == "extension"
        assert ext.extension_name == "tx_studentExtension"
        assert ext.documented is False


class TestAssembleSpineDrivenPopulatesDefinition:
    """ElementRecord.edfi_standard_definition carries the catalog
    description regardless of documented flag — spine-lens reviewers see
    the Ed-Fi spec text on both documented and undocumented rows."""

    def test_definition_set_on_documented_rows(self):
        spine = _make_spine_with_refs_and_subs()
        source = [
            ElementRecord(
                state="TX",
                edfi_version="4.0",
                domain="Student Enrollment",
                entity="Student",
                element_name="firstName",
                definition_text="Student's first name (TEDS).",
                documented=True,
            ),
        ]
        source_index = build_source_index(source)
        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=source_index,
            spine_missing_source_rows=[],
            state="TX",
            edfi_version="4.0.0",
            source_document="test.xlsx",
        )
        first = next(
            r for r in records if r.entity == "Student" and r.element_name == "firstName"
        )
        assert first.documented is True
        assert first.edfi_standard_definition is not None
        assert first.edfi_standard_definition.startswith("A name given to an individual at birth")

    def test_definition_set_on_undocumented_rows(self):
        # Spine-lens primary signal: even when the state didn't document
        # this (entity, element) the spec definition is there.
        spine = _make_spine_with_refs_and_subs()
        records, _ = assemble_spine_driven(
            spine=spine,
            source_index=build_source_index([]),
            spine_missing_source_rows=[],
            state="TX",
            edfi_version="4.0.0",
            source_document=None,
        )
        bd = next(
            r for r in records if r.entity == "Student" and r.element_name == "birthDate"
        )
        assert bd.documented is False
        assert bd.edfi_standard_definition == (
            "The month, day, and year on which an individual was born."
        )

    def test_hybrid_append_rows_unaffected(self):
        # Spine-missing audit-trail rows come in verbatim from source —
        # their edfi_standard_definition was whatever the source row
        # carried (typically None), not touched by assemble_spine_driven.
        spine = _make_spine_with_refs_and_subs()
        audit_row = ElementRecord(
            state="TX",
            edfi_version="4.0",
            domain="Audit Trail",
            entity="LegacyTxEntity",
            element_name="legacyField",
            definition_text="Not in spine.",
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
        legacy = next(r for r in records if r.element_name == "legacyField")
        assert legacy.edfi_standard_definition is None


class TestSourceFactsShape:
    def test_default_is_empty(self):
        f = SourceFacts()
        assert f.definition_text == ""
        assert f.business_rules_text is None
        assert f.regulatory_citations == []
