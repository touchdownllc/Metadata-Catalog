"""Tests for per-record extension attribution (Phase 5a + Option B).

`StateSpine.extension_element_keys()` mirrors `element_keys()` but tags only
extension-contributed keys with their catalog key (e.g., `mn_calendarExtension`).
`attribute_record_source` uses that map plus core spine keys to set
`ElementRecord.source` and `extension_name` per element. Replaces the
entity-level `_is_extension_entity` heuristic that flagged all elements on an
entity as `Yes` whenever the spine declared any extension on that entity.
"""

from datetime import datetime, timezone

from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
)
from src.models.element import ElementRecord
from src.models.spine import SpineSourceURLs, StateSpine
from src.utils.matching import attribute_record_source
from tests.factories import make_record


def _spine(entities: dict, extensions: dict | None = None) -> StateSpine:
    catalog = EdFiCatalog(
        version="4.0",
        entity_count=len(entities),
        extension_count=len(extensions or {}),
        entities=entities,
        extensions=extensions or {},
    )
    return StateSpine(
        state="XX",
        edfi_version="4.0",
        fetched_at=datetime.now(tz=timezone.utc),
        source_urls=SpineSourceURLs(resources="http://test/resources.json"),
        catalog=catalog,
    )


def _record(entity: str, element_name: str) -> ElementRecord:
    return make_record(entity, element_name, state="WI")


class TestExtensionElementKeys:
    def test_returns_extension_props_with_catalog_key(self):
        spine = _spine(
            entities={"Calendar": EntityEntry(properties={"calendarCode": PropertyInfo()})},
            extensions={
                "wi_calendarExtension": ExtensionEntry(
                    extends_entity="Calendar",
                    source_prefix="wi",
                    properties={"wiSpecialFlag": PropertyInfo()},
                ),
            },
        )
        ext_pairs = spine.extension_element_keys()
        # Value is the catalog dict key, not the source_prefix.
        assert ext_pairs[("Calendar", "wiSpecialFlag")] == "wi_calendarExtension"
        # Core property is NOT in the extension map.
        assert ("Calendar", "calendarCode") not in ext_pairs

    def test_descriptor_variants_carry_catalog_key(self):
        spine = _spine(
            entities={"Student": EntityEntry()},
            extensions={
                "mn_studentExtension": ExtensionEntry(
                    extends_entity="Student",
                    source_prefix="mn",
                    properties={"mnFlavorDescriptor": PropertyInfo()},
                ),
            },
        )
        ext_pairs = spine.extension_element_keys()
        assert ext_pairs[("Student", "mnFlavorDescriptor")] == "mn_studentExtension"
        assert ext_pairs[("Student", "mnFlavorDescriptorId")] == "mn_studentExtension"
        assert ext_pairs[("Student", "mnFlavor")] == "mn_studentExtension"

    def test_sub_entity_extension_propagates_to_parent(self):
        """When an extension extends a concatenated sub-entity name, the
        extension's keys also attribute to the catalog parent (mirrors the
        same loop in `element_keys()`)."""
        spine = _spine(
            entities={
                "StudentEducationOrganizationAssociation": EntityEntry(),
            },
            extensions={
                "mn_seoaLanguageAcademicHonor": ExtensionEntry(
                    extends_entity="StudentEducationOrganizationAssociationLanguageAcademicHonor",
                    source_prefix="mn",
                    properties={"recognitionTitle": PropertyInfo()},
                ),
            },
        )
        ext_pairs = spine.extension_element_keys()
        assert ext_pairs[(
            "StudentEducationOrganizationAssociationLanguageAcademicHonor",
            "recognitionTitle",
        )] == "mn_seoaLanguageAcademicHonor"
        # Parent-propagated key carries the same catalog key as the original.
        assert ext_pairs[(
            "StudentEducationOrganizationAssociation",
            "recognitionTitle",
        )] == "mn_seoaLanguageAcademicHonor"

    def test_sub_entity_name_emitted_as_pseudo_element(self):
        """Analyst feedback T2.4: MN row names the sub-entity itself as the
        element (e.g., `StudentEducationOrganizationAssociation.GenderIdentities`)
        rather than a specific property under it. The extension map should
        attribute this to the correct extension so `Is an extension = Yes`."""
        spine = _spine(
            entities={
                "StudentEducationOrganizationAssociation": EntityEntry(),
            },
            extensions={
                "mn_studentEducationOrganizationAssociationGenderIdentity": ExtensionEntry(
                    extends_entity="StudentEducationOrganizationAssociationGenderIdentity",
                    source_prefix="mn",
                    properties={"genderIdentityDescriptor": PropertyInfo()},
                ),
            },
        )
        ext_pairs = spine.extension_element_keys()
        ext_key = "mn_studentEducationOrganizationAssociationGenderIdentity"
        # Singular sub-entity tail (both PascalCase and camelCase).
        assert ext_pairs[("StudentEducationOrganizationAssociation", "GenderIdentity")] == ext_key
        assert ext_pairs[("StudentEducationOrganizationAssociation", "genderIdentity")] == ext_key
        # Plural-overloaded aliases so MDE's `GenderIdentities.` (after
        # trailing-dot cleanup) resolves.
        assert ext_pairs[("StudentEducationOrganizationAssociation", "GenderIdentities")] == ext_key
        assert ext_pairs[("StudentEducationOrganizationAssociation", "genderIdentities")] == ext_key
        # The actual property is also attributed (as before).
        assert ext_pairs[("StudentEducationOrganizationAssociation", "genderIdentityDescriptor")] == ext_key


class TestAttributeRecordSource:
    def test_core_property_marked_core(self):
        spine = _spine(
            entities={"Calendar": EntityEntry(properties={"calendarCode": PropertyInfo()})},
        )
        records = [_record("Calendar", "calendarCode")]
        attribute_record_source(records, spine)
        assert records[0].source == "core"
        assert records[0].extension_name is None

    def test_extension_property_marked_extension_with_catalog_key(self):
        spine = _spine(
            entities={"Calendar": EntityEntry(properties={"calendarCode": PropertyInfo()})},
            extensions={
                "wi_calendarExtension": ExtensionEntry(
                    extends_entity="Calendar",
                    source_prefix="wi",
                    properties={"wiSpecialFlag": PropertyInfo()},
                ),
            },
        )
        records = [
            _record("Calendar", "calendarCode"),
            _record("Calendar", "wiSpecialFlag"),
        ]
        attribute_record_source(records, spine)
        assert records[0].source == "core"
        assert records[1].source == "extension"
        assert records[1].extension_name == "wi_calendarExtension"

    def test_unmatched_record_marked_unknown(self):
        spine = _spine(
            entities={"Calendar": EntityEntry(properties={"calendarCode": PropertyInfo()})},
        )
        records = [_record("Calendar", "totallyMadeUpField")]
        attribute_record_source(records, spine)
        assert records[0].source == "unknown"
        assert records[0].extension_name is None

    def test_descriptor_alias_resolves_to_extension(self):
        """MDE drops the `Descriptor` suffix — the bare alias should still
        be attributed to the contributing extension."""
        spine = _spine(
            entities={"Student": EntityEntry()},
            extensions={
                "mn_studentExtension": ExtensionEntry(
                    extends_entity="Student",
                    source_prefix="mn",
                    properties={"mnFlavorDescriptor": PropertyInfo()},
                ),
            },
        )
        records = [_record("Student", "mnFlavor")]  # MDE-style bare form
        attribute_record_source(records, spine)
        assert records[0].source == "extension"
        assert records[0].extension_name == "mn_studentExtension"


class TestArizonaSourceFromTable:
    """AZ sets `source` at record-creation time from `table.is_extension`
    (the `edfi.*` / `az.*` namespace), not via spine lookup. Verify the
    direct path produces the expected attribution."""

    def test_az_extension_table_yields_extension_records(self):
        from src.ingest.arizona import AZElementRow, AZEntityTable, build_element_records

        table = AZEntityTable(
            sheet_name="SchoolCalendar",
            entity_name="az.CalendarExtension",
            is_extension=True,
            requirement_level="Required",
            elements=[
                AZElementRow(
                    raw_name="extraField",
                    clean_name="extraField",
                    optionality=None,
                    data_type="string",
                    codes_ref=None,
                    description="",
                ),
            ],
        )
        records = build_element_records([table], edfi_version="4.0")
        assert records[0].source == "extension"
        assert records[0].extension_name == "az.CalendarExtension"

    def test_az_core_table_yields_core_records(self):
        from src.ingest.arizona import AZElementRow, AZEntityTable, build_element_records

        table = AZEntityTable(
            sheet_name="SchoolCalendar",
            entity_name="edfi.Calendar",
            is_extension=False,
            requirement_level="Required",
            elements=[
                AZElementRow(
                    raw_name="calendarCode",
                    clean_name="calendarCode",
                    optionality=None,
                    data_type="string",
                    codes_ref=None,
                    description="",
                ),
            ],
        )
        records = build_element_records([table], edfi_version="4.0")
        assert records[0].source == "core"
        assert records[0].extension_name is None


class TestAzExtensionNameCanonicalization:
    """Analyst review flagged three source-side quirks leaking into the
    workbook's `Contributing extension` column. Verify
    `_canonicalize_az_extension_name` fixes each; `raw_entity` preserves the
    verbatim source so audit trails stay intact."""

    def test_uppercase_prefix_lowered(self):
        from src.ingest.arizona import _canonicalize_az_extension_name
        assert (
            _canonicalize_az_extension_name("AZ.DisciplineActionExtension")
            == "az.DisciplineActionExtension"
        )

    def test_extention_typo_corrected(self):
        from src.ingest.arizona import _canonicalize_az_extension_name
        assert (
            _canonicalize_az_extension_name("az.CourseTranscriptExtention")
            == "az.CourseTranscriptExtension"
        )

    def test_internal_whitespace_collapsed(self):
        from src.ingest.arizona import _canonicalize_az_extension_name
        assert (
            _canonicalize_az_extension_name(
                "az.StudentDropOut RecoveryProgramMonthlyUpdates"
            )
            == "az.StudentDropOutRecoveryProgramMonthlyUpdates"
        )

    def test_already_canonical_passthrough(self):
        from src.ingest.arizona import _canonicalize_az_extension_name
        assert (
            _canonicalize_az_extension_name("az.CalendarExtension")
            == "az.CalendarExtension"
        )

    def test_build_records_canonicalizes_extension_name_but_preserves_raw_entity(self):
        """End-to-end: extension_name is canonicalized, raw_entity stays verbatim."""
        from src.ingest.arizona import AZElementRow, AZEntityTable, build_element_records

        table = AZEntityTable(
            sheet_name="Discipline",
            entity_name="AZ.DisciplineActionExtension",
            is_extension=True,
            requirement_level="Required",
            elements=[
                AZElementRow(
                    raw_name="x",
                    clean_name="x",
                    optionality=None,
                    data_type="string",
                    codes_ref=None,
                    description="",
                ),
            ],
        )
        records = build_element_records([table], edfi_version="4.0")
        assert records[0].extension_name == "az.DisciplineActionExtension"
        assert records[0].raw_entity == "AZ.DisciplineActionExtension"


class TestAzDescriptorTypeNormalization:
    """Analyst P1 (feedback2 #5): row 107 PartCTransition.DelayDescriptorId
    source-typed as `Date` despite definition describing a descriptor.
    Normalize descriptor-suffixed elements to `Descriptor` regardless of the
    AZ XLSX source type."""

    def _record(self, element_name: str, data_type: str) -> "ElementRecord":
        from src.models.element import ElementRecord
        return ElementRecord(
            state="AZ",
            edfi_version="4.0",
            domain="X",
            entity="PartCTransition",
            element_name=element_name,
            data_type=data_type,
            definition_text="",
            source="extension",
            documented=True,
        )

    def test_descriptor_id_suffix_overrides_to_descriptor(self):
        from src.ingest.arizona import _normalize_descriptor_type_in_place
        records = [self._record("DelayDescriptorId", "Date")]
        _normalize_descriptor_type_in_place(records)
        assert records[0].data_type == "Descriptor"

    def test_descriptor_suffix_overrides_to_descriptor(self):
        from src.ingest.arizona import _normalize_descriptor_type_in_place
        records = [self._record("courseLevelDescriptor", "Integer")]
        _normalize_descriptor_type_in_place(records)
        assert records[0].data_type == "Descriptor"

    def test_non_descriptor_field_type_preserved(self):
        """Primitive fields keep their source-provided type — AZ's XLSX is
        the authoritative type source for non-descriptor fields."""
        from src.ingest.arizona import _normalize_descriptor_type_in_place
        records = [self._record("BirthDate", "Date")]
        _normalize_descriptor_type_in_place(records)
        assert records[0].data_type == "Date"
