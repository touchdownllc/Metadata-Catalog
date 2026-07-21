"""Tests for the one-home alias grammar (issue #213 item 2).

Two jobs:

1. Unit-pin each grammar RULE in ``models/alias_grammar.py`` — the five
   consumers (element_keys / extension_element_keys / type index /
   canonical emits / gap surfacer) all derive their alias forms from
   these helpers, so a rule change here is THE grammar change.
2. Pin today's deliberate DIVERGENCES between consumers (which tiers
   each consumes) so closing one later is a visible test edit, not a
   silent side effect — the type index lacks the camel-collapse /
   EdOrg-subtype / qualifier tiers its old docstring claimed to mirror,
   and the gap surfacer checks a narrower set plus its own
   ``refName+CapKey`` form.

Plus the issue's "cheap first step": alias-tier provenance — the
``_tier_sink`` plumbing and the ``alias_tier_histogram`` gap-log block.
"""

from datetime import datetime, timezone

from src.models.alias_grammar import (
    EDORG_SUBTYPE_NAMES,
    collapse_camel_overlap,
    descriptor_variants,
    edorg_subtype_aliases,
    fk_prefixed_alias,
    id_stripped_alias,
    is_spa_template_target,
    naive_plural_forms,
    parent_stripped_tail,
    prefix_parent_of,
    reference_prefix,
    reference_qualifier,
    sub_entity_name_forms,
    uniqueid_id_alias,
)
from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    PropertyInfo,
    ReferenceInfo,
)
from src.models.spine import SpineSourceURLs, StateSpine


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


class TestGrammarRules:
    def test_descriptor_variants(self):
        assert descriptor_variants("gradeLevelDescriptor") == (
            "gradeLevelDescriptorId",
            "gradeLevel",
        )
        assert descriptor_variants("firstName") == ()
        # A name that IS the suffix produces no empty-stem alias.
        assert descriptor_variants("Descriptor") == ("DescriptorId",)

    def test_collapse_camel_overlap(self):
        assert (
            collapse_camel_overlap("assignmentSchool", "SchoolId")
            == "assignmentSchoolId"
        )
        assert collapse_camel_overlap("program", "EducationOrganizationId") is None

    def test_reference_prefix(self):
        assert reference_prefix("schoolReference") == "school"
        assert reference_prefix("noSuffix") == "noSuffix"

    def test_reference_qualifier(self):
        assert (
            reference_qualifier(
                "employmentStaffEducationOrganizationEmploymentAssociation",
                "StaffEducationOrganizationEmploymentAssociation",
            )
            == "employment"
        )
        assert reference_qualifier("school", "School") is None  # no remainder
        assert reference_qualifier("program", "EducationOrganization") is None

    def test_uniqueid_and_id_strip(self):
        assert uniqueid_id_alias("studentUniqueId") == "studentId"
        assert uniqueid_id_alias("schoolId") is None
        assert id_stripped_alias("programEducationOrganizationId") == (
            "programEducationOrganization"
        )
        assert id_stripped_alias("xId") is None  # too short to strip

    def test_fk_prefixed_alias(self):
        assert (
            fk_prefixed_alias("program", "educationOrganizationId")
            == "programEducationOrganizationId"
        )

    def test_edorg_subtype_aliases_order_and_coverage(self):
        aliases = list(edorg_subtype_aliases())
        # 4 forms per subtype, declared order — consumers register in
        # exactly this sequence (dict first-writer-wins depends on it).
        assert len(aliases) == 4 * len(EDORG_SUBTYPE_NAMES)
        assert aliases[:4] == [
            "School", "school", "schoolReference", "schoolId",
        ]

    def test_sub_entity_and_tail_forms(self):
        assert sub_entity_name_forms("GenderIdentity") == (
            "GenderIdentity", "genderIdentity",
        )
        assert parent_stripped_tail(
            "StudentEducationOrganizationAssociationDyslexiaRiskSet",
            "StudentEducationOrganizationAssociation",
        ) == ("DyslexiaRiskSet", "dyslexiaRiskSet")
        assert parent_stripped_tail("Unrelated", "Student") is None

    def test_naive_plural_forms(self):
        assert naive_plural_forms("GenderIdentity") == (
            "GenderIdentities", "genderIdentities",
        )
        assert naive_plural_forms("Address") is None  # already ends in s

    def test_prefix_parent_longest_first(self):
        entities = ["Student", "StudentEducationOrganizationAssociation"]
        assert (
            prefix_parent_of(
                "StudentEducationOrganizationAssociationGenderIdentity",
                entities,
            )
            == "StudentEducationOrganizationAssociation"
        )
        assert prefix_parent_of("Calendar", entities) is None

    def test_spa_template_targets(self):
        assert is_spa_template_target("StudentTitleIPartAProgramAssociation")
        assert is_spa_template_target(
            "StudentSpecialEducationProgramAssociationExtension"
        )
        assert not is_spa_template_target("StudentProgramAssociation")
        assert not is_spa_template_target("GeneralStudentProgramAssociation")
        assert not is_spa_template_target("Calendar")


class TestConsumerTierDivergences:
    """Pin WHICH tiers each consumer consumes today — the documented
    divergences (issue #213 item 2). Closing one becomes a deliberate
    edit here, never a refactor side effect."""

    def _fixture(self):
        return _spine({
            "Section": EntityEntry(
                references={
                    "assignmentSchoolReference": ReferenceInfo(
                        entity="School",
                        key_properties={"schoolId": PropertyInfo(type="integer")},
                    ),
                },
            ),
            "School": EntityEntry(properties={}),
        })

    def test_element_keys_has_camel_collapse_but_type_index_does_not(self):
        from src.ingest.shared import build_spine_type_index

        spine = self._fixture()
        keys = spine.element_keys()
        # element_keys: camel-collapsed composite FK alias present.
        assert ("Section", "assignmentSchoolId") in keys
        # type index: deliberately narrower — the collapsed alias is
        # absent (widening it is a measured follow-up decision; see the
        # alias-tier histogram in the gap log).
        idx = build_spine_type_index(spine)
        typed_names = {n for (e, n) in idx if e == "section"}
        assert "assignmentschoolschoolid" in typed_names  # prefixed form
        assert "assignmentschoolid" not in typed_names  # collapsed absent

    def test_gap_surfacer_checks_own_refname_capkey_form(self):
        # The surfacer's ref expansion includes `refName + CapKey`
        # (`assignmentSchoolReferenceSchoolId`) — its own extra form; no
        # other consumer emits it. Pinned via the helper composition it
        # now uses: fk_prefixed_alias(ref_name, kp).
        assert (
            fk_prefixed_alias("assignmentSchoolReference", "schoolId")
            == "assignmentSchoolReferenceSchoolId"
        )


class TestAliasTierProvenance:
    """Issue #213 item 2 — match-time tier provenance + gap-log histogram."""

    def _fixture(self):
        return _spine({
            "Calendar": EntityEntry(properties={
                "calendarTypeDescriptor": PropertyInfo(type="string"),
                "calendarCode": PropertyInfo(type="string"),
            }),
        })

    def test_tier_sink_tags_fuzzy_aliases(self):
        from src.ingest.shared import canonical_spine_emit_keys

        sink: dict = {}
        tiers: dict = {}
        canonical_spine_emit_keys(
            self._fixture(), _alias_sink=sink, _tier_sink=tiers
        )
        by_alias = {n: t for (_e, n), t in tiers.items()}
        assert by_alias["calendartypedescriptor"] == "primary"
        assert by_alias["calendartypedescriptorid"] == "descriptor_variant"
        assert by_alias["calendartype"] == "descriptor_variant"

    def test_histogram_lands_in_assembly_and_gap_log(self):
        from src.ingest.shared import (
            SourceFacts,
            assemble_spine_driven,
            build_gap_log,
        )
        from src.utils.matching import match_key

        spine = self._fixture()
        # Source doc used the MDE bare descriptor form + the plain name.
        source_index = {}
        for entity, name in (
            ("Calendar", "calendarType"),      # descriptor_variant tier
            ("Calendar", "calendarCode"),      # primary tier
        ):
            e, n = match_key(entity, name)
            source_index[(e, n.lower())] = SourceFacts()
        records, assembly = assemble_spine_driven(
            spine,
            source_index,
            [],
            state="XX",
            edfi_version="4.0",
        )
        assert assembly.alias_tier_histogram == {
            "descriptor_variant": 1,
            "primary": 1,
        }
        gap_log = build_gap_log(
            state="XX",
            spine=spine,
            spine_source_rel="data/spine/xx_spine.json",
            assembly=assembly,
            recovered=[],
            source_coverage_note="n",
            spine_coverage_note="n",
            alias_tier_histogram=assembly.alias_tier_histogram,
        )
        assert gap_log["alias_tier_histogram"] == {
            "descriptor_variant": 1,
            "primary": 1,
        }
