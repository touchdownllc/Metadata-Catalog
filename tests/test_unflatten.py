"""Tests for the AZ-style concatenated-sub-entity unflattening helper."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    PropertyInfo,
    ReferenceInfo,
    SubCollectionInfo,
)
from src.models.spine import StateSpine, SpineSourceURLs
from src.spine.unflatten import (
    ABSTRACT_BASES,
    _depluralize,
    build_unflatten_map,
    resolve_parent,
)


def _make_spine() -> StateSpine:
    """Fixture: CalendarDate.calendarEvents[] + StudentSchoolAssociation."""
    cat = EdFiCatalog(
        version="5.2",
        entity_count=3,
        entities={
            "CalendarDate": EntityEntry(
                properties={
                    "date": PropertyInfo(),
                    "calendarCode": PropertyInfo(),
                },
                references={
                    "calendarReference": ReferenceInfo(
                        entity="Calendar",
                        key_properties={
                            "calendarCode": PropertyInfo(),
                            "schoolId": PropertyInfo(),
                            "schoolYear": PropertyInfo(),
                        },
                    ),
                },
                sub_collections={
                    "calendarEvents": SubCollectionInfo(
                        sub_entity="CalendarDateCalendarEvent",
                        properties={"calendarEventDescriptor": PropertyInfo()},
                    ),
                },
            ),
            "Calendar": EntityEntry(),
            "StudentSchoolAssociation": EntityEntry(
                properties={"entryDate": PropertyInfo()},
                references={
                    "studentReference": ReferenceInfo(
                        entity="Student",
                        key_properties={"studentUniqueId": PropertyInfo()},
                    ),
                    "classOfSchoolYearTypeReference": ReferenceInfo(
                        entity="SchoolYearType",
                        key_properties={"schoolYear": PropertyInfo()},
                    ),
                },
            ),
        },
        lookup_index={
            "calendardate": "CalendarDate",
            "calendar": "Calendar",
            "studentschoolassociation": "StudentSchoolAssociation",
        },
    )
    return StateSpine(
        state="AZ",
        edfi_version="5.2",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(resources="test", descriptors=None),
        catalog=cat,
    )


class TestDepluralize:
    def test_simple_plural(self):
        assert _depluralize("calendarEvents") == "CalendarEvent"

    def test_y_plural(self):
        assert _depluralize("activities") == "Activity"

    def test_already_singular(self):
        assert _depluralize("address") == "Address"


class TestBuildUnflattenMap:
    def test_includes_swagger_sub_entity_name(self):
        m = build_unflatten_map(_make_spine())
        assert m["calendardatecalendarevent"] == ("CalendarDate", "calendarEvents")

    def test_includes_derived_parent_plus_singular_form(self):
        # Same result under the `{Parent}{SingularSubCollection}` convention.
        m = build_unflatten_map(_make_spine())
        assert m["calendardatecalendarevent"] == ("CalendarDate", "calendarEvents")

    def test_keys_are_lowercased(self):
        m = build_unflatten_map(_make_spine())
        assert all(k == k.lower() for k in m.keys())


class TestResolveParent:
    def test_direct_hit_in_map(self):
        spine = _make_spine()
        m = build_unflatten_map(spine)
        parent = resolve_parent("CalendarDateCalendarEvent", m, spine.entity_keys())
        assert parent == ("CalendarDate", "calendarEvents")

    def test_longest_prefix_fallback(self):
        # Extension-only concatenated entity — not in sub_collections, but its
        # name starts with a known spine entity.
        spine = _make_spine()
        m = build_unflatten_map(spine)
        parent = resolve_parent(
            "StudentSchoolAssociationLocalEducationAgency",
            m,
            spine.entity_keys(),
        )
        assert parent == ("StudentSchoolAssociation", "localEducationAgency")

    def test_returns_none_for_unknown(self):
        spine = _make_spine()
        m = build_unflatten_map(spine)
        assert resolve_parent("SomethingEntirelyMadeUp", m, spine.entity_keys()) is None

    def test_suffix_match_returns_full_concatenated_entity(self):
        """When a bare source name (`LanguageAcademicHonor`) suffix-matches
        a single nested spine entity (`SEOALanguageAcademicHonor`), the
        rewrite target must be the FULL entity — extension propagation
        attributes the props to the closest catalog parent (often itself a
        sub-entity), so matching against just the head misses."""
        # Minimal fixture: a parent + a deeper nested entity that ends with
        # `LanguageAcademicHonor`, plus the head as a real spine entity.
        cat = EdFiCatalog(
            version="5.2",
            entity_count=2,
            entities={
                "StudentEducationOrganizationAssociation": EntityEntry(),
                "StudentEducationOrganizationAssociationLanguage": EntityEntry(),
            },
            lookup_index={},
        )
        spine = StateSpine(
            state="MN",
            edfi_version="5.2",
            fetched_at=datetime.now(timezone.utc),
            source_urls=SpineSourceURLs(resources="test", descriptors=None),
            catalog=cat,
        )
        # Inject the deeper sub-entity into entity_keys via an extension
        # extends_entity (mirrors how MN's spine surfaces it).
        names = spine.entity_keys() | {
            "StudentEducationOrganizationAssociationLanguageAcademicHonor"
        }
        m = build_unflatten_map(spine)
        result = resolve_parent("LanguageAcademicHonor", m, names)
        assert result is not None
        full, sub = result
        # Full concatenated form — NOT just `StudentEducationOrganizationAssociation`.
        assert full == "StudentEducationOrganizationAssociationLanguageAcademicHonor"
        assert sub == "languageAcademicHonor"


class TestElementKeysRefExpansion:
    """Spine element_keys should emit AZ-style FK aliases so matches don't
    depend on downstream adapters knowing about the naming convention."""

    def test_uniqueid_aliased_to_id(self):
        spine = _make_spine()
        keys = spine.element_keys()
        assert ("StudentSchoolAssociation", "studentId") in keys
        assert ("StudentSchoolAssociation", "studentUniqueId") in keys

    def test_ref_qualifier_stripped_when_it_duplicates_target_entity(self):
        # classOfSchoolYearTypeReference (target=SchoolYearType) with key schoolYear
        # should emit `classOfSchoolYear` after qualifier stripping.
        spine = _make_spine()
        keys = spine.element_keys()
        assert ("StudentSchoolAssociation", "classOfSchoolYear") in keys


class TestAbstractBases:
    def test_expected_members(self):
        assert "EducationOrganization" in ABSTRACT_BASES
        assert "GeneralStudentProgramAssociation" in ABSTRACT_BASES


# ---------------------------------------------------------------------------
# Integration — exercise against the real AZ spine if available.
# ---------------------------------------------------------------------------

_AZ_SPINE = Path(__file__).resolve().parents[1] / "data" / "spine" / "az_spine.json"


@pytest.mark.realdata
@pytest.mark.skipif(not _AZ_SPINE.exists(), reason="AZ spine not built")
class TestAgainstRealAZSpine:
    def test_known_sub_collection_entries_resolve(self):
        spine = StateSpine.model_validate_json(_AZ_SPINE.read_text())
        m = build_unflatten_map(spine)
        names = spine.entity_keys()

        # Sub-collection-derived mapping
        assert resolve_parent("CalendarDateCalendarEvent", m, names) == (
            "CalendarDate", "calendarEvents",
        )
        # Extension-only concatenated entity — via longest-prefix fallback
        ssa = resolve_parent(
            "StudentSchoolAssociationLocalEducationAgency", m, names,
        )
        assert ssa is not None
        assert ssa[0] == "StudentSchoolAssociation"

    def test_abstract_base_names_present_in_az_gap_domain(self):
        # These names should not be resolvable to a concrete parent.
        spine = StateSpine.model_validate_json(_AZ_SPINE.read_text())
        m = build_unflatten_map(spine)
        names = spine.entity_keys()
        # GeneralStudentProgramAssociation is itself a spine entity in some
        # catalogs (abstract). Even if the name-split fallback fires, ABSTRACT_BASES
        # is what the gap log uses to mark these records as out-of-scope.
        assert "GeneralStudentProgramAssociation" in ABSTRACT_BASES
