"""Round 2.2 regression guards.

Each test here locks in an analyst-reviewer-flagged behavior fixed in the
Round 2.2 polish pass (see `docs/archive/round-2.2-plan.md`). Grouped by fix
category rather than by source module so the next reviewer can read the
file as an executable changelog.
"""

from __future__ import annotations

from src.ingest import arizona, minnesota, wisconsin
from src.ingest.shared import (
    build_spine_type_index,
    canonical_type,
    populate_data_types_from_spine,
)


# ---------------------------------------------------------------------------
# Tier A.1 — AZ singular extension name
# ---------------------------------------------------------------------------


class TestAZExtensionNameSingularization:
    """Reviewer B: `az.StudentDropOutRecoveryProgramMonthlyUpdates` (plural)
    should display as the singular spine form
    `az.StudentDropOutRecoveryProgramMonthlyUpdate`."""

    KNOWN_EXT_KEYS = {
        "az_studentDropOutRecoveryProgramMonthlyUpdate",
        "az_calendarExtension",
        "az_sectionExtension",
    }

    def test_strips_trailing_s_when_singular_matches_spine(self):
        out = arizona._canonicalize_az_extension_name(
            "az.StudentDropOutRecoveryProgramMonthlyUpdates",
            self.KNOWN_EXT_KEYS,
        )
        assert out == "az.StudentDropOutRecoveryProgramMonthlyUpdate"

    def test_does_not_strip_when_singular_not_in_spine(self):
        """Defensive: names that legitimately end in `s` (hypothetical
        `az.Others`) shouldn't be mangled if the singular isn't a known
        extension."""
        out = arizona._canonicalize_az_extension_name(
            "az.FictionalEntitys",
            self.KNOWN_EXT_KEYS,
        )
        assert out == "az.FictionalEntitys"

    def test_works_without_known_keys_preserves_legacy_behavior(self):
        """Called with no spine context: no singularization — must not
        regress the pre-Round-2.2 behavior."""
        out = arizona._canonicalize_az_extension_name(
            "az.StudentDropOutRecoveryProgramMonthlyUpdates"
        )
        assert out == "az.StudentDropOutRecoveryProgramMonthlyUpdates"

    def test_whitespace_collapse_still_fires(self):
        out = arizona._canonicalize_az_extension_name(
            "az.StudentDropOut RecoveryProgramMonthlyUpdates",
            self.KNOWN_EXT_KEYS,
        )
        assert out == "az.StudentDropOutRecoveryProgramMonthlyUpdate"

    def test_uppercase_prefix_still_normalizes(self):
        out = arizona._canonicalize_az_extension_name("AZ.CalendarExtension")
        assert out == "az.CalendarExtension"

    def test_extention_typo_still_fires(self):
        out = arizona._canonicalize_az_extension_name(
            "az.CourseTranscriptExtention"
        )
        assert out == "az.CourseTranscriptExtension"


# ---------------------------------------------------------------------------
# Tier A.3 — MN nav-path + dotted-prefix cleanup
# ---------------------------------------------------------------------------


class TestMNDottedPrefixAliases:
    """Reviewer B + Moffatt: bare `Student.StudentUniqueId` should strip to
    `StudentUniqueId`. Also `School.SchoolId`, `EducationOrganization.*`,
    `addresses.*` — FK-reference paths previously preserved as path labels."""

    def test_bare_student_prefix_strips(self):
        assert minnesota._strip_known_source_aliases(
            "Student.StudentUniqueId"
        ) == "StudentUniqueId"

    def test_school_prefix_strips(self):
        assert minnesota._strip_known_source_aliases(
            "School.SchoolId"
        ) == "SchoolId"

    def test_education_organization_prefix_strips(self):
        assert minnesota._strip_known_source_aliases(
            "EducationOrganization.EducationOrganizationId"
        ) == "EducationOrganizationId"

    def test_transporting_lea_ref_prefix_strips(self):
        assert minnesota._strip_known_source_aliases(
            "TransportingLocalEducationAgencyReference.LocalEducationAgencyId"
        ) == "LocalEducationAgencyId"

    def test_addresses_prefix_strips(self):
        assert minnesota._strip_known_source_aliases(
            "addresses.stateAbbreviationDescriptor"
        ) == "stateAbbreviationDescriptor"

    def test_core_student_still_strips(self):
        """Round-2 pre-existing: make sure `Core.Student.` aliases still strip."""
        assert minnesota._strip_known_source_aliases(
            "Core.Student.StudentUniqueId"
        ) == "StudentUniqueId"

    def test_chained_aliases_strip_to_fixed_point(self):
        """Reviewer B flagged `Transportation.TransportingLocalEducationAgency
        Reference.LocalEducationAgencyId` — source chains two aliased prefixes.
        Round 2.2 iterates strip to fixed point so both prefixes are removed."""
        assert minnesota._strip_known_source_aliases(
            "Transportation.TransportingLocalEducationAgencyReference.LocalEducationAgencyId"
        ) == "LocalEducationAgencyId"

    def test_unaliased_prefix_preserved(self):
        """Only listed aliases strip; unknown prefixes are preserved so the
        analyst sees source-verbatim structure when the strip can't safely
        apply."""
        assert minnesota._strip_known_source_aliases(
            "unstrippedPrefix.someField"
        ) == "unstrippedPrefix.someField"


class TestMNNavPathCleanup:
    """Reviewer B: `StudentReference>StudentUniqueId > LastSurname` should
    display as bare `lastSurname`, not the full nav path."""

    def test_single_arrow_strip_to_tail(self):
        assert minnesota._strip_mn_nav_path(
            "StudentReference>StudentUniqueId"
        ) == "studentUniqueId"

    def test_two_arrow_strip_to_last_segment(self):
        assert minnesota._strip_mn_nav_path(
            "StudentReference>StudentUniqueId > LastSurname"
        ) == "lastSurname"

    def test_humanized_label_collapses_to_camel(self):
        assert minnesota._strip_mn_nav_path(
            "StudentReference>StudentUniqueId > First Name"
        ) == "firstName"

    def test_humanized_middle_name(self):
        assert minnesota._strip_mn_nav_path(
            "StudentReference>StudentUniqueId > Middle Name"
        ) == "middleName"

    def test_no_arrow_passthrough(self):
        assert minnesota._strip_mn_nav_path("plainElementName") == "plainElementName"

    def test_clean_element_name_integration(self):
        """End-to-end path: _clean_element_name runs the nav-path stripper."""
        assert minnesota._clean_element_name(
            "StudentReference>StudentUniqueId > GenerationCodeSuffix"
        ) == "generationCodeSuffix"


# ---------------------------------------------------------------------------
# Tier B.2 — WI descriptor suffix normalization
# ---------------------------------------------------------------------------


class TestWIDescriptorTypeNormalization:
    """Reviewer B: 129 WI matched-descriptor rows typed as `String`
    should be `Descriptor`. Mirrors AZ behavior."""

    def test_descriptor_suffix_overwritten(self):
        from src.models.element import ElementRecord

        rec = ElementRecord(
            state="WI", edfi_version="5.2", domain="Test",
            entity="LocalAccount", element_name="reportingTagDescriptor",
            data_type="String", definition_text="", source="core",
            documented=True,
        )
        recs = [rec]
        wisconsin._normalize_descriptor_type_in_place(recs)
        assert recs[0].data_type == "Descriptor"

    def test_descriptor_id_suffix_case_insensitive(self):
        from src.models.element import ElementRecord

        rec = ElementRecord(
            state="WI", edfi_version="5.2", domain="Test",
            entity="Foo", element_name="fooDescriptorID",
            data_type="Integer", definition_text="", source="core",
            documented=True,
        )
        recs = [rec]
        wisconsin._normalize_descriptor_type_in_place(recs)
        assert recs[0].data_type == "Descriptor"

    def test_non_descriptor_names_untouched(self):
        from src.models.element import ElementRecord

        rec = ElementRecord(
            state="WI", edfi_version="5.2", domain="Test",
            entity="Student", element_name="birthDate",
            data_type="Date", definition_text="", source="core",
            documented=True,
        )
        recs = [rec]
        wisconsin._normalize_descriptor_type_in_place(recs)
        assert recs[0].data_type == "Date"


# ---------------------------------------------------------------------------
# Tier B.3 + B.4 — Spine-type override (canonical type contract)
# ---------------------------------------------------------------------------


def _make_synthetic_spine():
    """Tiny spine fixture exercising descriptor, reference, and primitive
    property shapes. Shared across canonical-type tests."""
    from src.models.edfi_catalog import (
        EdFiCatalog,
        EntityEntry,
        PropertyInfo,
        ReferenceInfo,
    )
    from src.models.spine import SpineSourceURLs, StateSpine
    from datetime import datetime, timezone

    catalog = EdFiCatalog(
        version="5.2",
        entities={
            "Student": EntityEntry(
                properties={
                    "birthDate": PropertyInfo(type="string", format="date"),
                    "lastSurname": PropertyInfo(type="string"),
                    "generationCodeSuffix": PropertyInfo(type="string"),
                },
                references={
                    "schoolReference": ReferenceInfo(
                        entity="School",
                        key_properties={
                            "schoolId": PropertyInfo(type="integer"),
                        },
                    ),
                },
                domains=["Enrollment"],
            ),
            "School": EntityEntry(
                properties={
                    "nameOfInstitution": PropertyInfo(type="string"),
                },
                domains=["School Organization"],
            ),
            "Section": EntityEntry(
                properties={
                    "sequenceOfCourse": PropertyInfo(type="integer"),
                },
                domains=["Teaching and Learning"],
            ),
            "LocalAccount": EntityEntry(
                properties={
                    "reportingTagDescriptor": PropertyInfo(type="string"),
                },
                domains=["Finance"],
            ),
            "StudentMigrantEducationProgramAssociation": EntityEntry(
                properties={
                    "lastQualifyingMove": PropertyInfo(type="string", format="date"),
                    "priorityForServices": PropertyInfo(type="boolean"),
                },
                domains=["MigrantEducation"],
            ),
        },
        extensions={},
    )
    return StateSpine(
        state="WI",
        edfi_version="5.2",
        fetched_at=datetime.now(timezone.utc),
        school_year=2026,
        source_urls=SpineSourceURLs(resources="https://test.example/resources.json"),
        catalog=catalog,
    )


class TestCanonicalTypeContract:
    """Round 2.2: matched records take their type from the spine, overriding
    any source-inferred value (descriptors, dates, booleans — including
    source typos like `Bloolean`)."""

    def test_canonical_type_descriptor_wins_over_raw(self):
        assert canonical_type("reportingTagDescriptor", "string") == "Descriptor"

    def test_canonical_type_descriptor_id_variant(self):
        assert canonical_type("reportingTagDescriptorId", "integer") == "Descriptor"

    def test_canonical_type_date_format_promotes(self):
        """Swagger models dates as `string` with `format: date`; we promote
        to canonical `Date` rather than rendering as `String`."""
        assert canonical_type("birthDate", "string", "date") == "Date"

    def test_canonical_type_date_time_format_promotes(self):
        assert canonical_type("lastModifiedDate", "string", "date-time") == "DateTime"

    def test_canonical_type_string_without_format(self):
        assert canonical_type("lastSurname", "string") == "String"

    def test_build_spine_type_index_includes_references(self):
        spine = _make_synthetic_spine()
        idx = build_spine_type_index(spine)
        assert idx.get(("student", "schoolreference")) == "Reference"
        # Bare-prefix alias so MDE cells naming the target entity resolve.
        assert idx.get(("student", "school")) == "Reference"

    def test_build_spine_type_index_includes_fk_aliases(self):
        spine = _make_synthetic_spine()
        idx = build_spine_type_index(spine)
        # `schoolId` key property emits an entry on the parent entity.
        assert idx.get(("student", "schoolid")) == "Integer"

    def test_populate_data_types_overrides_descriptor_string(self):
        """The core canonical-type contract: matched core record typed as
        `String` is overwritten with `Descriptor`."""
        from src.models.element import ElementRecord

        spine = _make_synthetic_spine()
        rec = ElementRecord(
            state="WI", edfi_version="5.2", domain="Test",
            entity="LocalAccount", element_name="reportingTagDescriptor",
            data_type="String", definition_text="", source="core",
            documented=True,
        )
        recs = [rec]
        populate_data_types_from_spine(recs, spine)
        assert recs[0].data_type == "Descriptor"

    def test_populate_data_types_overrides_bloolean_typo(self):
        """Reviewer B: WI row 88 `lastQualifyingMove: Bloolean` should become
        `Date` via spine override."""
        from src.models.element import ElementRecord

        spine = _make_synthetic_spine()
        rec = ElementRecord(
            state="WI", edfi_version="5.2", domain="Test",
            entity="StudentMigrantEducationProgramAssociation",
            element_name="lastQualifyingMove",
            data_type="Bloolean", definition_text="", source="core",
            documented=True,
        )
        recs = [rec]
        populate_data_types_from_spine(recs, spine)
        assert recs[0].data_type == "Date"

    def test_populate_data_types_fills_reference_blank(self):
        """Reviewer B: `schoolReference` on matched-core row was blank;
        Round 2.2 types it `Reference`."""
        from src.models.element import ElementRecord

        spine = _make_synthetic_spine()
        rec = ElementRecord(
            state="WI", edfi_version="5.2", domain="Test",
            entity="Student", element_name="schoolReference",
            data_type=None, definition_text="", source="core",
            documented=True,
        )
        recs = [rec]
        populate_data_types_from_spine(recs, spine)
        assert recs[0].data_type == "Reference"

    def test_populate_data_types_leaves_unknown_untouched(self):
        """Unresolved rows keep source-verbatim types as an audit trail."""
        from src.models.element import ElementRecord

        spine = _make_synthetic_spine()
        rec = ElementRecord(
            state="WI", edfi_version="5.2", domain="Test",
            entity="GhostEntity", element_name="ghostField",
            data_type="SourceTypo", definition_text="", source="unknown",
            documented=True,
        )
        recs = [rec]
        populate_data_types_from_spine(recs, spine)
        assert recs[0].data_type == "SourceTypo"


# ---------------------------------------------------------------------------
# Tier A.5/A.6 — Known Limitations state scoping
# ---------------------------------------------------------------------------


class TestKnownLimitationsStateScoping:
    """Reviewer Moffatt #13: state-specific KL rows leak across workbooks.
    Round 2.2 adds `applies_to` filtering."""

    def test_az_workbook_excludes_mn_specific(self):
        from src.report.analyst import _filter_known_limitations

        rows = _filter_known_limitations("AZ")
        titles = [r[0] for r in rows]
        assert any("AZ-specific" in t for t in titles)
        assert not any("MN-specific" in t for t in titles)

    def test_mn_workbook_excludes_az_specific(self):
        from src.report.analyst import _filter_known_limitations

        rows = _filter_known_limitations("MN")
        titles = [r[0] for r in rows]
        assert any("MN-specific" in t for t in titles)
        assert not any("AZ-specific" in t for t in titles)

    def test_wi_workbook_has_no_state_specific(self):
        from src.report.analyst import _filter_known_limitations

        rows = _filter_known_limitations("WI")
        titles = [r[0] for r in rows]
        assert not any("AZ-specific" in t for t in titles)
        assert not any("MN-specific" in t for t in titles)

    def test_combined_workbook_shows_all(self):
        """`state=None` = combined `coverage_analyst.xlsx` context; shows all
        rows so cross-state readers see every limitation."""
        from src.report.analyst import _KNOWN_LIMITATIONS, _filter_known_limitations

        rows = _filter_known_limitations(None)
        assert len(rows) == len(_KNOWN_LIMITATIONS)

    def test_shared_rows_present_on_every_state(self):
        from src.report.analyst import _filter_known_limitations

        for state in ("AZ", "WI", "MN"):
            rows = _filter_known_limitations(state)
            titles = [r[0] for r in rows]
            # Phase F renamed this limitation now that the NACHOS
            # methodology columns fill on in-scope rows.
            assert (
                "In-scope NACHOS rows carry methodology scores; "
                "out-of-scope blank by design"
            ) in titles
            assert "Source-scope limitations" in titles


# ---------------------------------------------------------------------------
# Tier A.2 — AZ cross-attribution narrow fix
# ---------------------------------------------------------------------------


class TestAZCrossAttributionHint:
    """Reviewer B row 42: the AZ `StudentDropOutRecoveryProgramMonthlyUpdate`
    extension declares its own props; inherited identity (BeginDate) gets
    falsely matched on `Student` via spine parent-propagation. Round 2.2
    adds a targeted post-unflatten fix that restores the original entity
    and marks such rows unresolved.

    The actual fix lives in `arizona.py::run`; this test asserts the
    detection substring lives in the module so future maintainers don't
    rename the constant silently."""

    def test_az_cross_attribution_hints_constant_exists(self):
        import inspect

        src = inspect.getsource(arizona.run)
        assert "_AZ_CROSS_ATTRIBUTION_HINTS" in src
        assert "StudentDropOutRecoveryProgramMonthlyUpdate" in src

    def test_spine_entity_keys_includes_extension_targets(self):
        """`entity_keys()` must include extension targets — sanity check
        for downstream ingest logic that filters on entity membership."""
        from src.models.edfi_catalog import (
            EdFiCatalog,
            EntityEntry,
            ExtensionEntry,
        )
        from src.models.spine import SpineSourceURLs, StateSpine
        from datetime import datetime, timezone

        catalog = EdFiCatalog(
            version="5.2",
            entities={
                "Student": EntityEntry(properties={}, domains=[]),
            },
            extensions={
                "az_studentDropOutRecoveryProgramMonthlyUpdate": ExtensionEntry(
                    extends_entity="StudentDropOutRecoveryProgramMonthlyUpdate",
                    source_prefix="az",
                    properties={},
                ),
            },
        )
        spine = StateSpine(
            state="AZ",
            edfi_version="5.2",
            fetched_at=datetime.now(timezone.utc),
            source_urls=SpineSourceURLs(
                resources="https://test.example/resources.json"
            ),
            catalog=catalog,
        )
        entity_names = spine.entity_keys()
        assert "Student" in entity_names
        assert "StudentDropOutRecoveryProgramMonthlyUpdate" in entity_names
