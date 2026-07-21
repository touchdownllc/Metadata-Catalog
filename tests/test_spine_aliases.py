"""Tests for StateSpine.element_keys() alias expansions.

The alias set drives how permissive state-doc matching is against the
authoritative Swagger spine. Changes here directly affect source
coverage pct. See [touchdownllc/nachos-ai-poc-3#2] for context.
"""

from datetime import datetime, timezone

from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
    ReferenceInfo,
)
from src.models.spine import SpineSourceURLs, StateSpine


def _spine(entities: dict, extensions: dict | None = None) -> StateSpine:
    """Build a minimal StateSpine for alias-emission tests."""
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


class TestDescriptorStripping:
    def test_descriptor_suffix_emits_bare_alias(self):
        """MDE mapping-matrix drops the `Descriptor` suffix (`ProgramType`),
        spine has `programTypeDescriptor`. Both forms must coexist."""
        spine = _spine({
            "Calendar": EntityEntry(properties={
                "calendarTypeDescriptor": PropertyInfo(),
            }),
        })
        keys = spine.element_keys()
        assert ("Calendar", "calendarTypeDescriptor") in keys
        assert ("Calendar", "calendarTypeDescriptorId") in keys
        assert ("Calendar", "calendarType") in keys


class TestFkQualifierIdStripping:
    def test_fk_qualifier_emits_bare_and_id_variants(self):
        """`programReference` with key `educationOrganizationId` emits
        `programEducationOrganizationId` AND `programEducationOrganization`
        (the MDE form)."""
        spine = _spine({
            "StudentProgramAssociation": EntityEntry(
                properties={},
                references={
                    "programReference": ReferenceInfo(
                        entity="Program",
                        key_properties={
                            "educationOrganizationId": PropertyInfo(),
                        },
                    ),
                },
            ),
        })
        keys = spine.element_keys()
        assert ("StudentProgramAssociation", "programEducationOrganizationId") in keys
        assert ("StudentProgramAssociation", "programEducationOrganization") in keys


class TestReferenceBareAlias:
    def test_reference_emits_bare_prefix_alias(self):
        """MDE cell `Course` should resolve to spine's `courseReference`."""
        spine = _spine({
            "CourseOffering": EntityEntry(
                properties={},
                references={
                    "courseReference": ReferenceInfo(
                        entity="Course",
                        key_properties={"courseCode": PropertyInfo()},
                    ),
                },
            ),
        })
        keys = spine.element_keys()
        assert ("CourseOffering", "course") in keys
        assert ("CourseOffering", "courseReference") in keys  # original still present


class TestInheritedIdentitySynthesis:
    def test_concrete_program_assoc_inherits_base_keys(self):
        """Every concrete `Student*ProgramAssociation` should inherit
        element keys from the `StudentProgramAssociation` template so
        state docs listing `programType`/`programName` on the concrete
        subclass resolve even when Swagger hides the inheritance."""
        template = EntityEntry(
            properties={"beginDate": PropertyInfo()},
            references={
                "programReference": ReferenceInfo(
                    entity="Program",
                    key_properties={
                        "programName": PropertyInfo(),
                        "programTypeDescriptor": PropertyInfo(),
                    },
                ),
            },
        )
        spine = _spine(
            entities={
                "StudentProgramAssociation": template,
                "StudentHomelessProgramAssociation": EntityEntry(properties={
                    "homelessCode": PropertyInfo(),  # concrete-only prop
                }),
            },
            extensions={
                # Extension-only concrete (common in MN for state-specific
                # associations that don't exist as core entities).
                "mn_studentADSISProgramAssociation": ExtensionEntry(
                    extends_entity="StudentADSISProgramAssociation",
                    source_prefix="mn",
                    properties={"adsisOnlyField": PropertyInfo()},
                ),
            },
        )
        keys = spine.element_keys()

        # Concrete-specific property still present.
        assert ("StudentHomelessProgramAssociation", "homelessCode") in keys
        # Inherited identity fields synthesized onto the concrete.
        assert ("StudentHomelessProgramAssociation", "beginDate") in keys
        assert ("StudentHomelessProgramAssociation", "programName") in keys
        assert ("StudentHomelessProgramAssociation", "programTypeDescriptor") in keys
        # Extension-only concrete also inherits.
        assert ("StudentADSISProgramAssociation", "beginDate") in keys
        assert ("StudentADSISProgramAssociation", "programName") in keys
        # Abstract base itself is NOT a copy target.
        assert ("StudentProgramAssociation", "beginDate") in keys
        # Unrelated entity should NOT inherit the template.
        # (Guard against the synthesis matching too broadly.)
        spine_with_unrelated = _spine({
            "StudentProgramAssociation": template,
            "School": EntityEntry(properties={"schoolId": PropertyInfo()}),
        })
        unrelated_keys = spine_with_unrelated.element_keys()
        assert ("School", "programName") not in unrelated_keys


class TestExtensionSubEntityPropagation:
    def test_extension_of_sub_entity_propagates_to_parent(self):
        """`mn_studentEducationOrganizationAssociationLanguageAcademicHonor`
        extends `StudentEducationOrganizationAssociationLanguageAcademicHonor`,
        which is a sub-entity of `StudentEducationOrganizationAssociation`.
        The extension's properties should also surface on the parent so
        state docs that flatten the sub-entity resolve."""
        spine = _spine(
            entities={
                "StudentEducationOrganizationAssociation": EntityEntry(properties={
                    "studentUniqueId": PropertyInfo(),
                }),
            },
            extensions={
                "mn_studentEducationOrganizationAssociationLanguageAcademicHonor": ExtensionEntry(
                    extends_entity="StudentEducationOrganizationAssociationLanguageAcademicHonor",
                    source_prefix="mn",
                    properties={"achievementCategoryDescriptor": PropertyInfo()},
                ),
            },
        )
        keys = spine.element_keys()
        # Attributed to the extended entity itself.
        assert (
            "StudentEducationOrganizationAssociationLanguageAcademicHonor",
            "achievementCategoryDescriptor",
        ) in keys
        # Also surfaces on the parent so unflatten rewrites match.
        assert (
            "StudentEducationOrganizationAssociation",
            "achievementCategoryDescriptor",
        ) in keys
