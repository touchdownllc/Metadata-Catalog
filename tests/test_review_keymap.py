"""Phase E — reviewer→POC-3 keymap unit tests.

Each convention the module normalizes has a dedicated test. Round-trip
coverage against real sidecar data lives in
``test_review_keymap_real_data`` at the bottom — it's the hermetic
sanity check that the fabricated rules actually meet the reviewer file
on the ground.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.score.review_keymap import (
    SpineIndex,
    build_mc_lookup,
    reviewer_element_candidates,
    reviewer_key_to_mc_key,
)


# ---------------------------------------------------------------------------
# reviewer_element_candidates — individual conventions
# ---------------------------------------------------------------------------


def test_direct_element_lowered():
    cands = reviewer_element_candidates("calendarCode")
    assert "calendarcode" in cands


def test_trailing_parenthetical_stripped():
    cands = reviewer_element_candidates(
        "nextYearSchool.schoolId (For 2025-26 onward)"
    )
    # The cleaned form drives the dotted-refs and aliases.
    assert "nextyearschool.schoolid" in cands
    assert "schoolid" in cands
    # The annotated form should NOT leak into candidate set.
    assert not any("onward" in c for c in cands)


def test_dotted_tail_and_concat():
    cands = reviewer_element_candidates("gradeLevels.gradeLevelDescriptor")
    # Path-tail leaf (from element_aliases):
    assert "gradeleveldescriptor" in cands
    # Concatenated form (POC-3 sub-collection concat):
    assert "gradelevelsgradeleveldescriptor" in cands


def test_dotted_three_segments_drops_leading_refs():
    cands = reviewer_element_candidates(
        "section.CourseOffering.School.SchoolId"
    )
    # Drop-one-leading-ref concat:
    assert "courseofferingschoolschoolid" in cands
    assert "schoolschoolid" in cands
    # Pure tail:
    assert "schoolid" in cands
    # Leading-segment-only (FK ref column fallback):
    assert "section" in cands


def test_leading_segment_for_fk_ref():
    # TX: `School.SchoolId` on StudentCTEProgramAssociation; POC-3 source
    # stores the FK as `School` (the whole reference column), not `SchoolId`.
    cands = reviewer_element_candidates("School.SchoolId")
    assert "school" in cands
    assert "schoolid" in cands


def test_leading_segment_reference_suffix():
    # MN source stores FK columns as `{ref}Reference`; reviewer enumerates
    # the FK target (`educationOrganization.educationOrganizationId`). The
    # candidate set must offer `educationorganizationreference` so the
    # dotted-path form resolves.
    cands = reviewer_element_candidates("educationOrganization.educationOrganizationId")
    assert "educationorganizationreference" in cands
    assert "educationorganizationreferences" in cands


def test_descriptor_strip_fallback():
    # MN Matrix enumerates `CalendarType`; reviewer writes
    # `calendarTypeDescriptor`. Descriptor-strip lets them meet.
    cands = reviewer_element_candidates("calendarTypeDescriptor")
    assert "calendartype" in cands
    assert "calendartypedescriptor" in cands


def test_arrow_navigation_treated_as_dotted():
    cands = reviewer_element_candidates("StudentReference>StudentUniqueId")
    assert "studentuniqueid" in cands
    assert "studentreferencestudentuniqueid" in cands


def test_no_trailing_paren_no_extra_sources():
    # When there's no trailing paren, the cleaned form equals the
    # original, so we don't process the same string twice.
    cands_plain = reviewer_element_candidates("calendarCode")
    cands_annot = reviewer_element_candidates("calendarCode ")
    assert cands_plain == cands_annot


def test_empty_element_yields_no_segments():
    # Guard against pathological inputs.
    cands = reviewer_element_candidates("")
    # lowered empty string is the only member.
    assert cands == [""]


def test_candidates_ordered_path_tail_before_reference():
    # Priority: path-tail leaf must come before `{ref}Reference`
    # fallback so states with BOTH enumerated resolve to the leaf.
    cands = reviewer_element_candidates("school.schoolId")
    assert "schoolid" in cands
    assert "schoolreference" in cands
    assert cands.index("schoolid") < cands.index("schoolreference")


def test_ref_reference_leaf_concat():
    # WI source concatenates FK paths into a single column
    # (`schoolYearTypeReferenceschoolYear`); reviewer writes the dotted
    # form. The new `{ref}Reference{tailConcat}` candidate bridges them.
    cands = reviewer_element_candidates("schoolYearType.schoolYear")
    assert "schoolyeartypereferenceschoolyear" in cands


def test_collection_prefix_descriptor_inference():
    # Reviewer writes a bare `*Descriptor`; WI source stores it as the
    # parent-collection-name + descriptor concat (`gradeLevels` ×
    # `gradeLevelDescriptor` = `gradeLevelsgradeLevelDescriptor`).
    cands = reviewer_element_candidates("gradeLevelDescriptor")
    assert "gradelevelsgradeleveldescriptor" in cands

    cands = reviewer_element_candidates("calendarEventDescriptor")
    assert "calendareventscalendareventdescriptor" in cands


def test_collection_prefix_only_on_single_segment():
    # Multi-segment descriptors (`gradeLevels.gradeLevelDescriptor`) hit
    # the dotted-concat rule directly; the inference rule should not
    # fire and double-emit. Confirms the guard.
    cands = reviewer_element_candidates("gradeLevels.gradeLevelDescriptor")
    # Original concat form from rule #3 is sufficient:
    assert "gradelevelsgradeleveldescriptor" in cands


# ---------------------------------------------------------------------------
# Issue #138 — Pattern B (reviewer-prefix strip) + Pattern A (descriptor-Id)
# ---------------------------------------------------------------------------


def test_reviewer_prefix_strip_ext_az():
    # Doug's AZ workbook annotates extension-contributed elements with
    # `(ext/az) `. Stripped at the head of the cascade so all
    # downstream rules see the bare element.
    cands = reviewer_element_candidates("(ext/az) FinalLetterGradeDescriptor")
    assert "finallettergradedescriptor" in cands
    # Descriptor-strip still fires from the bare form:
    assert "finallettergrade" in cands
    # The annotated form must NOT leak into the candidate set.
    assert not any(c.startswith("(ext") for c in cands)


def test_reviewer_prefix_strip_other_states():
    # The strip is state-agnostic (the regex matches any `(ext/<state>)`
    # token) so a future TX or WI workbook using the same convention
    # resolves on the same code path.
    for prefix in ("(ext/wi)", "(ext/tx)", "(ext/mn)"):
        cands = reviewer_element_candidates(f"{prefix} mainSPEDSchool")
        assert "mainspedschool" in cands, prefix


def test_reviewer_prefix_strip_case_insensitive():
    # Reviewers may use mixed case for the prefix (`(EXT/AZ)`); the
    # strip fires regardless of casing.
    cands = reviewer_element_candidates("(EXT/AZ) mainSPEDSchool")
    assert "mainspedschool" in cands


def test_reviewer_prefix_strip_does_not_fire_on_trailing_paren():
    # Negative test — the existing trailing-paren cleanup must remain
    # the only thing that fires on annotations like
    # `nextYearSchool.schoolId (For 2025-26 onward)`.
    cands_a = reviewer_element_candidates("schoolId (2025-26 onward)")
    cands_b = reviewer_element_candidates("schoolId")
    # The cleaned form (`schoolId`) drives both; trailing-paren strip
    # produces an identical post-cleanup state.
    assert "schoolid" in cands_a
    assert "schoolid" in cands_b


def test_descriptor_id_widening_in_candidate_cascade():
    # Reviewer writes the bare descriptor (`CourseAttemptResultDescriptor`);
    # AZ source XLSX surfaces it as `CourseAttemptResultDescriptorId`
    # (the FK-resolved column name). The descriptor-strip widening at
    # the foot of the cascade emits both the `*descriptor` form and
    # the bare stem so the cross-direction join works in either
    # candidate-vs-sidecar registration order.
    cands = reviewer_element_candidates("CourseAttemptResultDescriptorId")
    assert "courseattemptresultdescriptorid" in cands  # direct
    assert "courseattemptresultdescriptor" in cands    # *Id stripped
    assert "courseattemptresult" in cands               # bare stem


def test_build_mc_lookup_aliases_descriptor_id_sidecar_keys():
    # Sidecar keys ending in `*DescriptorId` register `*Descriptor`
    # and bare-stem aliases on the POC-3 side, so reviewer rows that
    # write the bare descriptor resolve here too.
    scores = [
        {"record_key": "AZ|CourseTranscript|FinalLetterGradeDescriptorId"},
    ]
    lookup = build_mc_lookup(scores)
    assert ("coursetranscript", "finallettergradedescriptorid") in lookup
    assert ("coursetranscript", "finallettergradedescriptor") in lookup
    assert ("coursetranscript", "finallettergrade") in lookup
    # Existing exact-name registration wins on a collision (setdefault).
    scores_collide = [
        {"record_key": "AZ|CourseTranscript|FinalLetterGrade"},
        {"record_key": "AZ|CourseTranscript|FinalLetterGradeDescriptorId"},
    ]
    lookup_collide = build_mc_lookup(scores_collide)
    assert lookup_collide[("coursetranscript", "finallettergrade")] == (
        "AZ|CourseTranscript|FinalLetterGrade"
    )


# ---------------------------------------------------------------------------
# build_mc_lookup + reviewer_key_to_mc_key
# ---------------------------------------------------------------------------


def _synthetic_scores() -> list[dict]:
    """Mimic the shape of ``data/out/{state}_scores_{lens}.json``:scores[].

    Only ``record_key`` is consulted — other fields omitted.
    """
    return [
        {"record_key": "WI|Calendar|calendarCode"},
        {"record_key": "WI|Calendar|gradeLevelsgradeLevelDescriptor"},
        {"record_key": "MN|Calendar|CalendarType"},  # MN source PascalCase
        {"record_key": "TX|StudentCTEProgramAssociation|School"},
        {"record_key": "TX|CourseTranscriptExt|collegeCreditHours"},
        {
            "record_key": (
                "MN|Student21stCenturyLearningCenterGrantProgramAssociation|"
                "EducationOrganizationReference"
            )
        },
    ]


def test_lookup_registers_direct_and_alias_forms():
    lookup = build_mc_lookup(_synthetic_scores())
    # Direct lower:
    assert ("calendar", "calendarcode") in lookup
    # Alias from path-tail on the concat form:
    assert ("calendar", "gradelevelsgradeleveldescriptor") in lookup


def test_resolve_plural_entity():
    lookup = build_mc_lookup(_synthetic_scores())
    # Reviewer writes `Calendars` (plural); POC-3 uses `Calendar`. Both
    # normalize to `calendar`.
    assert reviewer_key_to_mc_key("Calendars", "calendarCode", lookup) == (
        "WI|Calendar|calendarCode"
    )


def test_resolve_dotted_concat():
    lookup = build_mc_lookup(_synthetic_scores())
    assert reviewer_key_to_mc_key(
        "Calendar", "gradeLevels.gradeLevelDescriptor", lookup
    ) == "WI|Calendar|gradeLevelsgradeLevelDescriptor"


def test_resolve_descriptor_strip():
    lookup = build_mc_lookup(_synthetic_scores())
    # MN Mapping Matrix stores `CalendarType`; reviewer writes Ed-Fi form.
    assert reviewer_key_to_mc_key(
        "Calendars", "calendarTypeDescriptor", lookup
    ) == "MN|Calendar|CalendarType"


def test_resolve_fk_ref_leading_segment():
    lookup = build_mc_lookup(_synthetic_scores())
    # TX source stores `School` as the FK column; reviewer writes
    # `School.SchoolId`.
    assert reviewer_key_to_mc_key(
        "StudentCTEProgramAssociation", "School.SchoolId", lookup
    ) == "TX|StudentCTEProgramAssociation|School"


def test_resolve_fk_ref_reference_suffix():
    # MN source stores FK as `{ref}Reference`; reviewer writes
    # `educationOrganization.educationOrganizationId`.
    lookup = build_mc_lookup(_synthetic_scores())
    assert reviewer_key_to_mc_key(
        "Student21stCenturyLearningCenterGrantProgramAssociation",
        "educationOrganization.educationOrganizationId",
        lookup,
    ) == (
        "MN|Student21stCenturyLearningCenterGrantProgramAssociation|"
        "EducationOrganizationReference"
    )


def test_resolve_trailing_paren_stripped():
    lookup = build_mc_lookup(_synthetic_scores())
    assert reviewer_key_to_mc_key(
        "CourseTranscriptExt",
        "collegeCreditHours (2025-26 and later)",
        lookup,
    ) == "TX|CourseTranscriptExt|collegeCreditHours"


def test_unmatched_returns_none():
    lookup = build_mc_lookup(_synthetic_scores())
    # Nothing in the fixture matches `totallyFabricated`.
    assert reviewer_key_to_mc_key("Calendar", "totallyFabricated", lookup) is None


def test_empty_entity_or_element_returns_none():
    lookup = build_mc_lookup(_synthetic_scores())
    assert reviewer_key_to_mc_key("", "calendarCode", lookup) is None
    assert reviewer_key_to_mc_key("Calendar", "", lookup) is None
    assert reviewer_key_to_mc_key(None, "calendarCode", lookup) is None  # type: ignore[arg-type]


def test_whitespace_padded_inputs_are_trimmed():
    lookup = build_mc_lookup(_synthetic_scores())
    # Reviewer file has trailing-space entity names (e.g.
    # `"studentContactAssociation "`) and padding on elements.
    assert reviewer_key_to_mc_key(
        "Calendars ", " calendarCode", lookup
    ) == "WI|Calendar|calendarCode"


def test_setdefault_first_wins_on_collision():
    # Two records that normalize to the same entity/element alias key —
    # lookup should hold the FIRST.
    scores = [
        {"record_key": "WI|Calendar|calendarCode"},
        {"record_key": "WI|Calendars|calendarCode"},  # hypothetical dup
    ]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key("Calendar", "calendarCode", lookup) == (
        "WI|Calendar|calendarCode"
    )


# ---------------------------------------------------------------------------
# Issue #138 PR 3 — Pattern D curated synonyms
# ---------------------------------------------------------------------------


def test_synonym_same_entity_rename():
    """Same-entity synonym (`MultipleBirthIndicator` ↔ `multipleBirthStatus`)."""
    from src.score.review_keymap import _REVIEWER_TO_MC_SYNONYMS

    scores = [{"record_key": "WI|Student|multipleBirthStatus"}]
    lookup = build_mc_lookup(scores, state="WI")
    assert reviewer_key_to_mc_key(
        "Student", "MultipleBirthIndicator", lookup
    ) == "WI|Student|multipleBirthStatus"
    # Confirm the entry registered (the synonym table claims the
    # mapping, and `build_mc_lookup` materialized it).
    assert ("Student", "MultipleBirthIndicator") in _REVIEWER_TO_MC_SYNONYMS["WI"]


def test_synonym_cross_entity_rename():
    """Cross-entity synonym (Student demographics moved to StudentEducationOrgAssoc)."""
    scores = [
        {"record_key": "WI|Student|studentUniqueId"},
        {"record_key": "WI|StudentEducationOrganizationAssociation|sexDescriptor"},
    ]
    lookup = build_mc_lookup(scores, state="WI")
    # Reviewer wrote `Student | SexType`; sidecar carries it on
    # StudentEducationOrganizationAssociation per Ed-Fi 4.0.
    assert reviewer_key_to_mc_key(
        "Student", "SexType", lookup
    ) == "WI|StudentEducationOrganizationAssociation|sexDescriptor"


def test_synonym_only_fires_when_state_provided():
    """Without `state=`, the synonym pass is skipped — preserves prior callers."""
    scores = [{"record_key": "WI|Student|multipleBirthStatus"}]
    lookup_no_state = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "Student", "MultipleBirthIndicator", lookup_no_state
    ) is None


def test_synonym_for_unknown_state_is_skipped():
    """Unknown state code passes silently (no synonym map registered)."""
    scores = [{"record_key": "WI|Student|multipleBirthStatus"}]
    lookup = build_mc_lookup(scores, state="NA")
    # No synonyms register; the only candidate is the direct cascade,
    # which doesn't match `MultipleBirthIndicator` against
    # `multipleBirthStatus`.
    assert reviewer_key_to_mc_key(
        "Student", "MultipleBirthIndicator", lookup
    ) is None


def test_synonym_does_not_override_exact_match():
    """`setdefault` preserves the exact-name registration over the synonym.

    The synonym table aliases AZ `Staff | GerenationCodeSuffix` (typo) to
    `Staff | GenerationCodeSuffix`. If the sidecar happens to also have
    a literal row called `GerenationCodeSuffix`, that exact-name match
    wins and the synonym entry is silently shadowed.
    """
    scores = [
        # Exact-name match first.
        {"record_key": "AZ|Staff|GerenationCodeSuffix"},
        {"record_key": "AZ|Staff|GenerationCodeSuffix"},
    ]
    lookup = build_mc_lookup(scores, state="AZ")
    # The exact-name registration won — synonym entry never overrides.
    assert reviewer_key_to_mc_key(
        "Staff", "GerenationCodeSuffix", lookup
    ) == "AZ|Staff|GerenationCodeSuffix"


def test_synonym_skipped_when_target_not_in_sidecar():
    """A synonym pointing to an absent sidecar row registers nothing.

    Defensive — keeps the table forward-compatible with sidecar regen.
    """
    # Sidecar is missing the `multipleBirthStatus` target, so the
    # `(Student, MultipleBirthIndicator)` synonym entry must not register.
    scores = [{"record_key": "WI|Student|studentUniqueId"}]
    lookup = build_mc_lookup(scores, state="WI")
    assert reviewer_key_to_mc_key(
        "Student", "MultipleBirthIndicator", lookup
    ) is None
    # And `(Student, MultipleBirthIndicator)` is NOT a registered key.
    assert ("student", "multiplebirthindicator") not in lookup


# ---------------------------------------------------------------------------
# Issue #138 PR 4 — TX synonyms (PriorYearLeaver descriptor + FK-flatten)
# ---------------------------------------------------------------------------


def test_tx_synonym_block_carries_priorYearLeaver_descriptor_renames():
    """TX block covers the bare → ``Descriptor``-suffixed lowerCamel rename.

    TX TWEDS sidecar names descriptor fields in lowerCamelCase with a
    ``Descriptor`` suffix; the reviewer writes the bare PascalCase stem.
    The synonym entries close that gap. Pinned to a sample so an
    accidental table edit surfaces here.
    """
    from src.score.review_keymap import _REVIEWER_TO_MC_SYNONYMS

    tx = _REVIEWER_TO_MC_SYNONYMS["TX"]
    # Spot-check three representative entries — same-entity descriptor
    # rename pattern dominates.
    assert tx[("PriorYearLeaver", "GradeLevel")] == (
        "PriorYearLeaver",
        "gradeLevelDescriptor",
    )
    assert tx[("PriorYearLeaver", "ExitWithdrawType")] == (
        "PriorYearLeaver",
        "exitWithdrawTypeDescriptor",
    )
    assert tx[("StudentDisciplineIncidentAssociation", "Behavior")] == (
        "StudentDisciplineIncidentAssociation",
        "behaviorDescriptor",
    )


def test_tx_synonym_dotted_fk_flatten_resolves():
    """`PriorYearLeaverParent.ParentUniqueId` → `priorYearLeaverParentParentUId`.

    Dotted FK paths in the reviewer file flatten into TWEDS sidecar
    names without separator (``priorYearLeaverParentParentUId``). The
    spine FK walker can't reach this because the source-lens sidecar
    doesn't carry the segments as references — the synonym table is
    the only path.
    """
    scores = [
        {
            "record_key": (
                "TX|PriorYearLeaverStudentParentAssociation"
                "|priorYearLeaverParentParentUId"
            )
        },
        {
            "record_key": (
                "TX|PriorYearLeaverStudentParentAssociation"
                "|priorYearLeaverStudentUId"
            )
        },
    ]
    lookup = build_mc_lookup(scores, state="TX")
    assert reviewer_key_to_mc_key(
        "PriorYearLeaverStudentParentAssociation",
        "PriorYearLeaverParent.ParentUniqueId",
        lookup,
    ) == (
        "TX|PriorYearLeaverStudentParentAssociation|priorYearLeaverParentParentUId"
    )
    assert reviewer_key_to_mc_key(
        "PriorYearLeaverStudentParentAssociation",
        "PriorYearLeaver.StudentUniqueId",
        lookup,
    ) == (
        "TX|PriorYearLeaverStudentParentAssociation|priorYearLeaverStudentUId"
    )


def test_embedded_newline_in_dotted_cell_normalizes_to_clean_path():
    """Reviewer cells with text-wrapping artifacts normalize cleanly.

    AZ workbook ships ``Calendar / (ext/az) TrackLocalEducationAgencyReference\\n.LocalEducationAgencyId``
    — the literal embedded newline comes from Excel's in-cell text
    wrapping, not from any semantic intent. Without whitespace
    cleanup, the dotted-segment split keeps ``\\n`` inside the leading
    segment and no candidate matches the synonym key. The cleanup in
    ``reviewer_element_candidates`` collapses whitespace + trims around
    ``.`` so the form joins like a normal dotted path.
    """
    from src.score.review_keymap import reviewer_element_candidates

    cands = reviewer_element_candidates(
        "TrackLocalEducationAgencyReference\n.LocalEducationAgencyId"
    )
    # The leading-segment candidate must be the whitespace-clean form
    # the synonym table keys against.
    assert "tracklocaleducationagencyreference" in cands


def test_wi_synonym_birthlocation_dotted_path_flatten_resolves():
    """`Student/BirthLocation.{City,Country,StateAbbreviation}` flatten to `birth*`.

    WI source-of-truth doesn't carry a `BirthLocation` sub-collection
    entity; the leaves inline on `Student` with a `birth` prefix. The
    reviewer wrote the canonical Ed-Fi dotted form, so the synonym
    table is the only path. `BirthLocation.County` deliberately stays
    out (Pattern F — no `birthCounty` in sidecar).
    """
    scores = [
        {"record_key": "WI|Student|birthCity"},
        {"record_key": "WI|Student|birthCountryDescriptor"},
        {"record_key": "WI|Student|birthStateAbbreviationDescriptor"},
    ]
    lookup = build_mc_lookup(scores, state="WI")
    assert reviewer_key_to_mc_key(
        "Student", "BirthLocation.City", lookup
    ) == "WI|Student|birthCity"
    assert reviewer_key_to_mc_key(
        "Student", "BirthLocation.Country", lookup
    ) == "WI|Student|birthCountryDescriptor"
    assert reviewer_key_to_mc_key(
        "Student", "BirthLocation.StateAbbreviation", lookup
    ) == "WI|Student|birthStateAbbreviationDescriptor"


def test_az_synonym_calendar_reference_suffix_drop_resolves():
    """AZ `(ext/az) TrackLocalEducationAgencyReference` → sidecar `TrackLocalEducationAgency`.

    After the existing `(ext/az)` prefix-strip (PR #140), the residual
    cell value is `TrackLocalEducationAgencyReference` — sidecar carries
    the same name without the `Reference` suffix. The synonym key uses
    the post-strip form because `reviewer_element_candidates` strips
    head-of-cascade.
    """
    scores = [{"record_key": "AZ|Calendar|TrackLocalEducationAgency"}]
    lookup = build_mc_lookup(scores, state="AZ")
    # Prefix-strip happens upstream; pass the post-strip form here as
    # the cascade would after `_strip_reviewer_prefix`.
    assert reviewer_key_to_mc_key(
        "Calendar", "TrackLocalEducationAgencyReference", lookup
    ) == "AZ|Calendar|TrackLocalEducationAgency"
    # End-to-end check: pass the full reviewer-written value with the
    # `(ext/az)` prefix and confirm it still resolves through the
    # cascade + synonym table.
    assert reviewer_key_to_mc_key(
        "Calendar", "(ext/az) TrackLocalEducationAgencyReference", lookup
    ) == "AZ|Calendar|TrackLocalEducationAgency"


def test_tx_synonym_lea_prefix_drop_resolves():
    """`LocalEducationAgency.LEAGrievanceLink` → sidecar's `GrievanceLink`.

    TX TWEDS source omits the LEA prefix the reviewer kept; the synonym
    table catches the drop on this single field.
    """
    scores = [{"record_key": "TX|LocalEducationAgency|GrievanceLink"}]
    lookup = build_mc_lookup(scores, state="TX")
    assert reviewer_key_to_mc_key(
        "LocalEducationAgency", "LEAGrievanceLink", lookup
    ) == "TX|LocalEducationAgency|GrievanceLink"


@pytest.mark.realdata
def test_synonym_table_round_trip_against_real_sidecars():
    """Every entry in `_REVIEWER_TO_MC_SYNONYMS` resolves correctly.

    Skips when the per-state sidecar artifact isn't generated locally.
    Walks the table per state; loads the live `{state}_scores_source.json`
    and asserts each `(rev_entity, rev_element)` resolves to the
    documented `(target_entity, target_element)`.
    """
    from src.score.review_keymap import _REVIEWER_TO_MC_SYNONYMS

    for state, mapping in _REVIEWER_TO_MC_SYNONYMS.items():
        sidecar = REPO_ROOT / "data" / "out" / f"{state.lower()}_scores_source.json"
        if not sidecar.exists():
            pytest.skip(f"sidecar not generated: {sidecar}")
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        lookup = build_mc_lookup(payload.get("scores", []), state=state)
        for (rev_ent, rev_el), (tgt_ent, tgt_el) in mapping.items():
            got = reviewer_key_to_mc_key(rev_ent, rev_el, lookup)
            expected_suffix = f"|{tgt_ent}|{tgt_el}"
            assert got is not None, (
                f"{state} synonym {rev_ent}|{rev_el} → "
                f"{tgt_ent}|{tgt_el} did not resolve"
            )
            assert got.endswith(expected_suffix), (
                f"{state} synonym {rev_ent}|{rev_el} resolved to {got!r}, "
                f"expected suffix {expected_suffix!r}"
            )


# ---------------------------------------------------------------------------
# Real-data round-trip — proves the rules meet measured reviewer rows.
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "state,lens,reviewer_pair,expected_suffix",
    [
        # WI source — sub-collection leaf. Pre-v26 the source-lens didn't
        # carry this row at all; the keymap fell back to the synthetic
        # ``gradeLevelsgradeLevelDescriptor`` concat name. v26
        # (issue #147) leaf-borrow appends the spine-canonical row
        # ``WI|Calendar|gradeLevelDescriptor``, which the keymap's
        # path-tail rule now resolves to. Either resolution is honest
        # and the v26 form is preferred (it points at a row the spine
        # actually emits).
        (
            "wi",
            "source",
            ("Calendar", "gradeLevels.gradeLevelDescriptor"),
            "|Calendar|gradeLevelDescriptor",
        ),
        # WI source — dotted FK ref resolves via path-tail to flat element
        (
            "wi",
            "source",
            ("studentSchoolAssociation", "school.schoolId"),
            "|StudentSchoolAssociation|schoolId",
        ),
        # MN source — PascalCase matrix + Descriptor strip
        (
            "mn",
            "source",
            ("Calendars", "calendarTypeDescriptor"),
            "|Calendar|CalendarType",
        ),
        # MN spine — plural entity + camelCase direct
        (
            "mn",
            "spine",
            ("Calendars", "calendarTypeDescriptor"),
            "|Calendar|calendarTypeDescriptor",
        ),
        # TX source — 4-segment dotted; leading segment wins on FK refs
        (
            "tx",
            "source",
            ("StudentCTEProgramAssociation", "School.SchoolId"),
            "|StudentCTEProgramAssociation|School",
        ),
        # TX spine — descriptor on extension
        (
            "tx",
            "spine",
            ("CourseTranscriptExt", "collegeCreditHours"),
            "|CourseTranscriptExt|collegeCreditHours",
        ),
        # MN source — {ref}Reference rule: reviewer dotted FK resolves
        # to POC-3's `{first-segment}Reference` column.
        (
            "mn",
            "source",
            (
                "studentEarlyEducationProgramAssociation",
                "calendar.calendarCode",
            ),
            "|StudentEarlyEducationProgramAssociation|calendarReference",
        ),
        # Issue #138 Pattern A — reviewer writes bare descriptor, AZ
        # source XLSX surfaces it as the FK-resolved column with
        # `Id` suffix.
        (
            "az",
            "source",
            ("CourseTranscript", "CourseAttemptResultDescriptor"),
            "|CourseTranscript|CourseAttemptResultDescriptorId",
        ),
        # Issue #138 Pattern B — reviewer prefix `(ext/az)` stripped at
        # the head of the cascade so the bare element name resolves.
        (
            "az",
            "source",
            (
                "StudentSpecialEducationProgramAssociation",
                "(ext/az) mainSPEDSchool",
            ),
            "|StudentSpecialEducationProgramAssociation|MainSPEDSchool",
        ),
        # Issue #138 Pattern B+A — reviewer prefix AND bare descriptor
        # both fire; sidecar carries the `Id`-suffixed form.
        (
            "az",
            "source",
            (
                "CourseTranscript",
                "(ext/az) FinalLetterGradeDescriptor",
            ),
            "|CourseTranscript|FinalLetterGradeDescriptorId",
        ),
    ],
)
@pytest.mark.realdata
def test_real_sidecars_resolve(state, lens, reviewer_pair, expected_suffix):
    """Smoke-test the normalizer against the actual sidecars on disk.

    Marks as a round-trip because a breakage here signals that either
    the reviewer conventions have shifted (new file) or POC-3 sidecar
    conventions have shifted (ingest rewrite). Either way, the digest
    match rate moves — the user should see this in test output.
    """
    sidecar = REPO_ROOT / "data" / "out" / f"{state}_scores_{lens}.json"
    if not sidecar.exists():
        pytest.skip(f"sidecar not generated: {sidecar}")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    lookup = build_mc_lookup(payload.get("scores", []))
    entity, element = reviewer_pair
    got = reviewer_key_to_mc_key(entity, element, lookup)
    assert got is not None, f"no match for reviewer pair {reviewer_pair}"
    assert got.endswith(expected_suffix), f"got {got!r}, expected suffix {expected_suffix!r}"


# ---------------------------------------------------------------------------
# SpineIndex + FK-nav resolution (issue #61)
# ---------------------------------------------------------------------------


def _section_catalog() -> dict:
    """Synthetic spine catalog for FK-nav tests.

    Models a slice of MN's reference graph that exercises every traversal
    branch:
    - 1-hop: ``courseOffering.schoolReference`` → School (target has the
      leaf as a property → direct resolve).
    - 2-hop transitive: ``staffSectionAssociation.section`` → Section,
      then Section.courseOfferingReference owns ``schoolId`` →
      CourseOffering's documented row resolves.
    - sub-collection / non-reference segments fall through (None).
    """
    return {
        "entities": {
            "School": {
                "properties": {"schoolId": {}, "nameOfInstitution": {}},
                "references": {},
                "sub_collections": {},
            },
            "CourseOffering": {
                "properties": {"localCourseCode": {}, "schoolId": {}, "schoolYear": {}},
                "references": {
                    "schoolReference": {
                        "entity": "School",
                        "key_properties": {"schoolId": {}},
                    },
                },
                "sub_collections": {},
            },
            "Section": {
                "properties": {"sectionIdentifier": {}, "localCourseCode": {}},
                "references": {
                    "courseOfferingReference": {
                        "entity": "CourseOffering",
                        "key_properties": {
                            "localCourseCode": {},
                            "schoolId": {},
                            "schoolYear": {},
                            "sessionName": {},
                        },
                    },
                },
                "sub_collections": {},
            },
            "StaffSectionAssociation": {
                "properties": {"classroomPositionDescriptor": {}},
                "references": {
                    "sectionReference": {
                        "entity": "Section",
                        "key_properties": {
                            "localCourseCode": {},
                            "schoolId": {},
                            "schoolYear": {},
                            "sectionIdentifier": {},
                            "sessionName": {},
                        },
                    },
                },
                "sub_collections": {},
            },
        },
        "extensions": {},
    }


def _section_scores() -> list[dict]:
    """Documented spine sidecar for the synthetic catalog."""
    return [
        {"record_key": "MN|Section|localCourseCode"},
        {"record_key": "MN|Section|sectionIdentifier"},
        {"record_key": "MN|CourseOffering|schoolId"},
        {"record_key": "MN|CourseOffering|schoolYear"},
        {"record_key": "MN|StaffSectionAssociation|classroomPositionDescriptor"},
    ]


def test_spine_index_merges_extensions_into_extended_entity():
    catalog = {
        "entities": {
            "Calendar": {"properties": {"calendarCode": {}}, "references": {}, "sub_collections": {}},
        },
        "extensions": {
            "calendarExtension": {
                "extends_entity": "Calendar",
                "properties": {"customField": {}},
                "references": {
                    "additionalRef": {
                        "entity": "School",
                        "key_properties": {"schoolId": {}},
                    },
                },
                "sub_collections": {},
            },
        },
    }
    spine = SpineIndex.from_catalog(catalog)
    pair = spine.find_entity("Calendar")
    assert pair is not None
    name, data = pair
    assert name == "Calendar"
    # Merge added the extension property + reference onto the core entity.
    assert "customField" in data["properties"]
    assert "additionalRef" in data["references"]


def test_spine_index_extension_only_entity_resolvable():
    """Extension targeting an entity not in `entities` still creates a record."""
    catalog = {
        "entities": {},
        "extensions": {
            "mn_studentPSEOConcurrentProgramAssociation": {
                "extends_entity": "StudentPSEOConcurrentProgramAssociation",
                "properties": {},
                "references": {
                    "studentReference": {
                        "entity": "Student",
                        "key_properties": {"studentUniqueId": {}},
                    },
                },
                "sub_collections": {},
            },
        },
    }
    spine = SpineIndex.from_catalog(catalog)
    pair = spine.find_entity("studentPSEOConcurrentProgramAssociation")
    assert pair is not None
    name, data = pair
    assert name == "StudentPSEOConcurrentProgramAssociation"
    assert "studentReference" in data["references"]


def test_spine_index_find_entity_normalizes_plural_and_casing():
    spine = SpineIndex.from_catalog(_section_catalog())
    # Plural and casing variants land on the canonical name.
    assert spine.find_entity("staffSectionAssociations")[0] == "StaffSectionAssociation"
    assert spine.find_entity("StaffSectionAssociation")[0] == "StaffSectionAssociation"


def test_fk_nav_one_hop_target_property_match():
    """`courseOffering | course.courseCode` → Course's row when Course has it."""
    spine = SpineIndex.from_catalog({
        "entities": {
            "Course": {
                "properties": {"courseCode": {}, "educationOrganizationId": {}},
                "references": {},
                "sub_collections": {},
            },
            "CourseOffering": {
                "properties": {},
                "references": {
                    "courseReference": {
                        "entity": "Course",
                        "key_properties": {"courseCode": {}, "educationOrganizationId": {}},
                    },
                },
                "sub_collections": {},
            },
        },
        "extensions": {},
    })
    scores = [
        {"record_key": "MN|Course|courseCode"},
        {"record_key": "MN|Course|educationOrganizationId"},
    ]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "courseOffering", "course.courseCode", lookup, spine=spine
    ) == "MN|Course|courseCode"


def test_fk_nav_two_hop_transitive_via_target_references():
    """`staffSectionAssociation | section.schoolId` → CourseOffering owns schoolId."""
    spine = SpineIndex.from_catalog(_section_catalog())
    lookup = build_mc_lookup(_section_scores())
    # Section doesn't carry schoolId directly; the walker follows
    # Section.courseOfferingReference (which has schoolId in its
    # key_properties) and lands on CourseOffering's documented row.
    assert reviewer_key_to_mc_key(
        "staffSectionAssociation", "section.schoolId", lookup, spine=spine
    ) == "MN|CourseOffering|schoolId"


def test_fk_nav_three_segment_walks_each_hop():
    """`sections | courseOffering.sessionName` → Session's row via 2-segment walk."""
    catalog = {
        "entities": {
            "Session": {
                "properties": {"sessionName": {}, "schoolId": {}},
                "references": {},
                "sub_collections": {},
            },
            "CourseOffering": {
                "properties": {"localCourseCode": {}},
                "references": {
                    "sessionReference": {
                        "entity": "Session",
                        "key_properties": {"sessionName": {}, "schoolId": {}},
                    },
                },
                "sub_collections": {},
            },
            "Section": {
                "properties": {"sectionIdentifier": {}},
                "references": {
                    "courseOfferingReference": {
                        "entity": "CourseOffering",
                        "key_properties": {"sessionName": {}},
                    },
                },
                "sub_collections": {},
            },
        },
        "extensions": {},
    }
    spine = SpineIndex.from_catalog(catalog)
    scores = [{"record_key": "MN|Session|sessionName"}]
    lookup = build_mc_lookup(scores)
    # CourseOffering doesn't have sessionName as a property; walker
    # transits to Session via sessionReference → resolve there.
    assert reviewer_key_to_mc_key(
        "section", "courseOffering.sessionName", lookup, spine=spine
    ) == "MN|Session|sessionName"


def _program_assoc_catalog() -> dict:
    """Catalog modeling MN program associations referencing Program.

    The reviewer writes the FK-nav segment in the *plural* collection form
    the MN source matrix uses (``programs.programName``) where the spine
    reference is the singular ``programReference`` → issue #166.
    """
    return {
        "entities": {
            "Program": {
                "properties": {
                    "programName": {},
                    "programTypeDescriptor": {},
                    "educationOrganizationId": {},
                },
                "references": {},
                "sub_collections": {},
            },
            "StudentSchoolFoodServiceProgramAssociation": {
                "properties": {"beginDate": {}},
                "references": {
                    "programReference": {
                        "entity": "Program",
                        "key_properties": {
                            "programName": {},
                            "programTypeDescriptor": {},
                            "educationOrganizationId": {},
                        },
                    },
                },
                "sub_collections": {},
            },
        },
        "extensions": {},
    }


def test_fk_nav_pluralized_reference_segment_resolves():
    """`...ProgramAssociation | programs.programName` → Program via plural→singular.

    Issue #166: the reviewer writes ``programs`` (the source-matrix
    collection name); the spine reference is the singular
    ``programReference``. The depluralized fallback in
    ``SpineIndex.follow_reference`` bridges them.
    """
    spine = SpineIndex.from_catalog(_program_assoc_catalog())
    scores = [
        {"record_key": "MN|Program|programName"},
        {"record_key": "MN|Program|programTypeDescriptor"},
    ]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "studentSchoolFoodServiceProgramAssociation",
        "programs.programName",
        lookup,
        spine=spine,
    ) == "MN|Program|programName"
    assert reviewer_key_to_mc_key(
        "studentSchoolFoodServiceProgramAssociation",
        "programs.programTypeDescriptor",
        lookup,
        spine=spine,
    ) == "MN|Program|programTypeDescriptor"


def test_follow_reference_literal_singular_still_matches():
    """Literal singular segment (``program``) resolves unchanged — additive guard."""
    spine = SpineIndex.from_catalog(_program_assoc_catalog())
    pair = spine.find_entity("StudentSchoolFoodServiceProgramAssociation")
    assert pair is not None
    _, data = pair
    # Both the literal `program` and the depluralized-from-`programs` form
    # land on the same Program target.
    assert spine.follow_reference(data, "program")[0] == "Program"
    assert spine.follow_reference(data, "programs")[0] == "Program"
    assert spine.follow_reference(data, "programReference")[0] == "Program"


def test_follow_reference_plural_fallback_does_not_invent_matches():
    """Depluralized fallback only fires on real references — no false positives."""
    spine = SpineIndex.from_catalog(_program_assoc_catalog())
    _, data = spine.find_entity("StudentSchoolFoodServiceProgramAssociation")
    # `cohorts` depluralizes to `cohort`, which is not a reference here.
    assert spine.follow_reference(data, "cohorts") is None


def test_fk_nav_returns_none_when_target_undocumented():
    """Honest no-match: target entity exists but the leaf isn't in the sidecar."""
    spine = SpineIndex.from_catalog(_section_catalog())
    # Lookup omits CourseOffering rows — schoolId isn't documented anywhere
    # the walker can reach.
    scores = [{"record_key": "MN|Section|sectionIdentifier"}]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "staffSectionAssociation", "section.schoolId", lookup, spine=spine
    ) is None


def test_fk_nav_returns_none_when_first_segment_not_a_reference():
    """Reviewer's qualifier doesn't map to a real reference name → no match."""
    spine = SpineIndex.from_catalog(_section_catalog())
    lookup = build_mc_lookup(_section_scores())
    # `bogusRef` isn't a reference on StaffSectionAssociation.
    assert reviewer_key_to_mc_key(
        "staffSectionAssociation", "bogusRef.schoolId", lookup, spine=spine
    ) is None


def test_fk_nav_skipped_when_spine_omitted():
    """Without spine arg, FK-nav doesn't fire — preserves prior behavior."""
    lookup = build_mc_lookup(_section_scores())
    assert reviewer_key_to_mc_key(
        "staffSectionAssociation", "section.schoolId", lookup
    ) is None


def test_fk_nav_does_not_override_direct_match():
    """Direct same-entity match wins over FK-nav (path-tail leaf preferred)."""
    spine = SpineIndex.from_catalog(_section_catalog())
    scores = [
        # Both rows exist; the candidate generator yields `localcoursecode`
        # first and finds it on StaffSectionAssociation directly.
        {"record_key": "MN|StaffSectionAssociation|localCourseCode"},
        {"record_key": "MN|Section|localCourseCode"},
    ]
    lookup = build_mc_lookup(scores)
    # Reviewer's `section.localCourseCode` would FK-nav to Section, but the
    # path-tail candidate `localcoursecode` lands on StaffSectionAssociation
    # FIRST via the existing rules — reviewer's intent on the parent entity
    # takes precedence over cross-entity walk.
    got = reviewer_key_to_mc_key(
        "staffSectionAssociation", "section.localCourseCode", lookup, spine=spine
    )
    assert got == "MN|StaffSectionAssociation|localCourseCode"


def test_fk_nav_with_reference_suffix_in_first_segment():
    """`grades | gradingPeriodReference.schoolYear` resolves via reference name match."""
    catalog = {
        "entities": {
            "GradingPeriod": {
                "properties": {"schoolId": {}, "schoolYear": {}},
                "references": {},
                "sub_collections": {},
            },
            "Grade": {
                "properties": {"letterGradeEarned": {}},
                "references": {
                    "gradingPeriodReference": {
                        "entity": "GradingPeriod",
                        "key_properties": {"schoolId": {}, "schoolYear": {}},
                    },
                },
                "sub_collections": {},
            },
        },
        "extensions": {},
    }
    spine = SpineIndex.from_catalog(catalog)
    scores = [{"record_key": "MN|GradingPeriod|schoolYear"}]
    lookup = build_mc_lookup(scores)
    # Reviewer wrote `gradingPeriodReference` (with the `Reference` suffix);
    # the walker should match it against `gradingPeriodReference` directly.
    assert reviewer_key_to_mc_key(
        "grades", "gradingPeriodReference.schoolYear", lookup, spine=spine
    ) == "MN|GradingPeriod|schoolYear"


# ---------------------------------------------------------------------------
# Single-segment leaf-search walker (issue #138 PR 2)
#
# Pattern C-flat: reviewer writes a leaf as a single segment (no dotted
# path) on a parent entity that doesn't document it directly, but a
# reference on the parent owns the leaf as a key property. The walker
# follows that reference to the documented row.
# ---------------------------------------------------------------------------


def _program_catalog() -> dict:
    """Synthetic catalog modeling the TX `programTypeDescriptor` cluster.

    StudentProgramAssociation borrows Program's identity via
    programReference whose key_properties include `programTypeDescriptor`;
    Program documents the leaf as a property.
    """
    return {
        "entities": {
            "Program": {
                "properties": {
                    "programName": {},
                    "programTypeDescriptor": {},
                    "programId": {},
                },
                "references": {},
                "sub_collections": {},
            },
            "StudentProgramAssociation": {
                "properties": {"beginDate": {}, "endDate": {}},
                "references": {
                    "programReference": {
                        "entity": "Program",
                        "key_properties": {
                            "educationOrganizationId": {},
                            "programName": {},
                            "programTypeDescriptor": {},
                        },
                    },
                    "studentReference": {
                        "entity": "Student",
                        "key_properties": {"studentUniqueId": {}},
                    },
                },
                "sub_collections": {},
            },
        },
        "extensions": {},
    }


def test_leaf_search_single_segment_via_key_property():
    """Reviewer's flat `programTypeDescriptor` resolves via Program FK."""
    spine = SpineIndex.from_catalog(_program_catalog())
    scores = [{"record_key": "TX|Program|programTypeDescriptor"}]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "StudentProgramAssociation", "programTypeDescriptor", lookup, spine=spine
    ) == "TX|Program|programTypeDescriptor"


def test_leaf_search_via_descriptor_strip_alias():
    """Source stores descriptor stripped (`ProgramType`); reviewer wrote bare descriptor.

    Walker matches the leaf-candidate set against the key-property-candidate
    set — the descriptor-strip alias bridges `programTypeDescriptor` (kp)
    and `ProgramType` (sidecar), letting the reviewer's `programTypeDescriptor`
    land on `TX|Program|ProgramType` (the TX-source convention).
    """
    spine = SpineIndex.from_catalog(_program_catalog())
    scores = [{"record_key": "TX|Program|ProgramType"}]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "StudentProgramAssociation", "programTypeDescriptor", lookup, spine=spine
    ) == "TX|Program|ProgramType"


def test_leaf_search_via_descriptor_add_alias():
    """Reviewer wrote `programType`; key-property is `programTypeDescriptor`.

    Symmetric case to the descriptor-strip alias — the walker matches
    the leaf-candidate set against the key-property's own candidate set,
    so the descriptor-strip alias on `programTypeDescriptor` (kp →
    `programtype`) intersects the leaf candidates (`{programtype}`).
    """
    spine = SpineIndex.from_catalog(_program_catalog())
    scores = [{"record_key": "TX|Program|ProgramType"}]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "StudentProgramAssociation", "programType", lookup, spine=spine
    ) == "TX|Program|ProgramType"


def test_leaf_search_two_hop_via_intermediate_reference():
    """Section | localCourseCode → CourseOffering's row via courseOfferingReference."""
    spine = SpineIndex.from_catalog(_section_catalog())
    scores = [{"record_key": "MN|CourseOffering|localCourseCode"}]
    lookup = build_mc_lookup(scores)
    # Section.courseOfferingReference's key_properties include localCourseCode;
    # the walker follows the FK and finds the documented row on CourseOffering.
    assert reviewer_key_to_mc_key(
        "Section", "localCourseCode", lookup, spine=spine
    ) == "MN|CourseOffering|localCourseCode"


def test_leaf_search_does_not_override_direct_match():
    """Direct same-entity candidate match wins over the leaf-search walker."""
    spine = SpineIndex.from_catalog(_program_catalog())
    # Both the parent entity AND the FK target document the leaf.
    scores = [
        {"record_key": "TX|StudentProgramAssociation|programTypeDescriptor"},
        {"record_key": "TX|Program|programTypeDescriptor"},
    ]
    lookup = build_mc_lookup(scores)
    # Direct match on the parent wins — leaf-search only fires on miss.
    assert reviewer_key_to_mc_key(
        "StudentProgramAssociation", "programTypeDescriptor", lookup, spine=spine
    ) == "TX|StudentProgramAssociation|programTypeDescriptor"


def test_leaf_search_skipped_when_spine_omitted():
    """Without spine, no walker fires — preserves prior behavior."""
    scores = [{"record_key": "TX|Program|programTypeDescriptor"}]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "StudentProgramAssociation", "programTypeDescriptor", lookup
    ) is None


def test_leaf_search_returns_none_when_no_reference_carries_leaf():
    """Honest no-match: the parent's references don't own the leaf in any kp."""
    spine = SpineIndex.from_catalog(_program_catalog())
    scores = [{"record_key": "TX|Program|programTypeDescriptor"}]
    lookup = build_mc_lookup(scores)
    # `studentReference` carries `studentUniqueId` only; `unrelatedField` is
    # nowhere on the parent or its references.
    assert reviewer_key_to_mc_key(
        "StudentProgramAssociation", "unrelatedField", lookup, spine=spine
    ) is None


def test_leaf_search_only_fires_on_single_segment():
    """Multi-segment inputs go through the dotted walker, not leaf-search.

    Confirms the activation guard: a dotted reviewer path bypasses the
    single-segment leaf-search even if the dotted walker also misses.
    """
    spine = SpineIndex.from_catalog(_program_catalog())
    scores = [{"record_key": "TX|Program|programTypeDescriptor"}]
    lookup = build_mc_lookup(scores)
    # `bogus.programTypeDescriptor` has no `bogus` reference on the parent,
    # so the dotted walker fails. The single-segment search must NOT then
    # try its luck on `programTypeDescriptor` alone — that would override
    # the reviewer's explicit (if broken) dotted path.
    assert reviewer_key_to_mc_key(
        "StudentProgramAssociation", "bogus.programTypeDescriptor", lookup, spine=spine
    ) is None


def test_leaf_search_strips_reviewer_prefix():
    """`(ext/az) Foo` is stripped before single-segment leaf-search fires."""
    spine = SpineIndex.from_catalog(_program_catalog())
    scores = [{"record_key": "TX|Program|programTypeDescriptor"}]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "StudentProgramAssociation",
        "(ext/tx) programTypeDescriptor",
        lookup,
        spine=spine,
    ) == "TX|Program|programTypeDescriptor"


def test_leaf_search_respects_visited_set_across_cycles():
    """Mutually-referencing entities don't loop the walker."""
    catalog = {
        "entities": {
            "EntityA": {
                "properties": {},
                "references": {
                    "bRef": {
                        "entity": "EntityB",
                        "key_properties": {"sharedLeaf": {}},
                    },
                },
                "sub_collections": {},
            },
            "EntityB": {
                "properties": {},
                "references": {
                    "aRef": {
                        "entity": "EntityA",
                        "key_properties": {"sharedLeaf": {}},
                    },
                },
                "sub_collections": {},
            },
        },
        "extensions": {},
    }
    spine = SpineIndex.from_catalog(catalog)
    # No documented row anywhere — walker must terminate cleanly via the
    # depth budget + visited set without recursing into a cycle.
    lookup = build_mc_lookup([{"record_key": "MN|EntityA|other"}])
    assert reviewer_key_to_mc_key(
        "EntityA", "sharedLeaf", lookup, spine=spine
    ) is None


def test_leaf_search_hop_budget_caps_at_three():
    """Walker bottoms out at depth=3; chains longer than 3 hops don't resolve."""
    # Build a 5-hop linear chain A → B → C → D → E; documented row is on E.
    def _link(target: str) -> dict:
        return {
            "properties": {},
            "references": {
                "nextRef": {
                    "entity": target,
                    "key_properties": {"deepLeaf": {}},
                },
            },
            "sub_collections": {},
        }

    catalog = {
        "entities": {
            "A": _link("B"),
            "B": _link("C"),
            "C": _link("D"),
            "D": _link("E"),
            "E": {
                "properties": {"deepLeaf": {}},
                "references": {},
                "sub_collections": {},
            },
        },
        "extensions": {},
    }
    spine = SpineIndex.from_catalog(catalog)
    lookup = build_mc_lookup([{"record_key": "MN|E|deepLeaf"}])
    # 4 hops from A reach E, but the budget caps recursion at depth=3
    # measured from the parent — so this clean walk must not resolve.
    # (The first hop is A→B at depth=3; B→C at depth=2; C→D at depth=1;
    # D's recursive call gets depth=0 and bails before checking E.)
    assert reviewer_key_to_mc_key(
        "A", "deepLeaf", lookup, spine=spine
    ) is None


# ---------------------------------------------------------------------------
# Real-data round-trip with FK-nav — issue #61 acceptance check.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state,lens,reviewer_pair,expected_suffix",
    [
        # Issue #61: MN-spine source-only-35 cohort. Each pair was
        # previously bucketed as `no_mc_row`; with FK-nav they resolve.
        (
            "mn",
            "spine",
            ("staffSectionAssociation", "section.schoolId"),
            "|CourseOffering|schoolId",
        ),
        (
            "mn",
            "spine",
            ("grades", "gradingPeriodReference.schoolYear"),
            "|GradingPeriod|schoolYear",
        ),
        (
            "mn",
            "spine",
            ("courseOffering", "course.courseCode"),
            "|Course|courseCode",
        ),
        (
            "mn",
            "spine",
            ("studentEducationOrganizationResponsibilityAssociation", "student.studentUniqueId"),
            "|Student|studentUniqueId",
        ),
        # Issue #138 PR 2: TX `programTypeDescriptor` cluster — single-segment
        # leaf-search walks programReference → Program. The TX-source convention
        # stores it descriptor-stripped (`ProgramType`), so the candidate alias
        # bridges reviewer's `programTypeDescriptor` to `Program|ProgramType`.
        (
            "tx",
            "source",
            ("StudentLanguageInstructionProgramAssociation", "programTypeDescriptor"),
            "|Program|ProgramType",
        ),
    ],
)
@pytest.mark.realdata
def test_real_sidecars_resolve_via_fk_nav(state, lens, reviewer_pair, expected_suffix):
    """Round-trip: real MN spine catalog + sidecar resolves the issue-#61 cohort.

    Skips when the spine catalog or sidecar isn't generated locally —
    the test is a coverage check on the live dual-lens artifacts, not
    a hermetic regression. Pairs with the synthetic-catalog tests above.
    """
    sidecar = REPO_ROOT / "data" / "out" / f"{state}_scores_{lens}.json"
    spine_path = REPO_ROOT / "data" / "spine" / f"{state}_spine.json"
    if not sidecar.exists() or not spine_path.exists():
        pytest.skip(f"artifact missing: {sidecar} or {spine_path}")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    lookup = build_mc_lookup(payload.get("scores", []))
    spine = SpineIndex.from_state(state.upper())
    assert spine is not None
    entity, element = reviewer_pair
    got = reviewer_key_to_mc_key(entity, element, lookup, spine=spine)
    assert got is not None, f"FK-nav didn't resolve {reviewer_pair}"
    assert got.endswith(expected_suffix), f"got {got!r}, expected suffix {expected_suffix!r}"


@pytest.mark.realdata
def test_mn_spine_match_count_widens_with_fk_nav():
    """Acceptance: MN spine matches gain ≥10 of the source-only-35 cohort.

    Issue #61's reproducer: count reviewer pairs that match in MN-source
    but not MN-spine. With FK-nav, this delta should drop meaningfully.
    Threshold is conservative (≥10) so prompt/sidecar drift doesn't
    flake the test — the actual gain measured 2026-04-29 was ~14.
    """
    spine_path = REPO_ROOT / "data" / "spine" / "mn_spine.json"
    src_sidecar = REPO_ROOT / "data" / "out" / "mn_scores_source.json"
    spine_sidecar = REPO_ROOT / "data" / "out" / "mn_scores_spine.json"
    if not (spine_path.exists() and src_sidecar.exists() and spine_sidecar.exists()):
        pytest.skip("MN spine/sidecar artifacts not generated")
    spine = SpineIndex.from_state("MN")
    src_payload = json.loads(src_sidecar.read_text(encoding="utf-8"))
    spine_payload = json.loads(spine_sidecar.read_text(encoding="utf-8"))

    # Load reviewer rows; skip if the workbooks aren't present locally.
    try:
        from src.score.review_loader import load_reviewer_records
    except ImportError:  # pragma: no cover
        pytest.skip("review_loader not importable")
    try:
        reviewer = load_reviewer_records()
    except FileNotFoundError:
        pytest.skip("per-state reviewer workbooks not present")

    src_lookup = build_mc_lookup(src_payload.get("scores", []))
    spine_lookup = build_mc_lookup(spine_payload.get("scores", []))

    src_match = {
        (r.entity, r.element)
        for r in reviewer
        if r.state == "MN"
        and reviewer_key_to_mc_key(r.entity, r.element, src_lookup) is not None
    }
    spine_match_no_walk = {
        (r.entity, r.element)
        for r in reviewer
        if r.state == "MN"
        and reviewer_key_to_mc_key(r.entity, r.element, spine_lookup) is not None
    }
    spine_match_with_walk = {
        (r.entity, r.element)
        for r in reviewer
        if r.state == "MN"
        and reviewer_key_to_mc_key(r.entity, r.element, spine_lookup, spine=spine)
        is not None
    }

    baseline_only_on_source = src_match - spine_match_no_walk
    fk_nav_only_on_source = src_match - spine_match_with_walk

    # FK-nav recovers a meaningful chunk; tolerate prompt-drift jitter.
    recovered = len(baseline_only_on_source) - len(fk_nav_only_on_source)
    assert recovered >= 10, (
        f"FK-nav only recovered {recovered} MN-spine matches; "
        f"expected ≥10. baseline_only_on_source={len(baseline_only_on_source)}, "
        f"fk_nav_only_on_source={len(fk_nav_only_on_source)}"
    )
    # Spine matches grow strictly; never regress.
    assert spine_match_with_walk >= spine_match_no_walk


@pytest.mark.realdata
@pytest.mark.parametrize("state", ["WI", "TX"])
def test_other_states_spine_match_count_does_not_regress(state):
    """Acceptance: WI + TX spine match counts are non-decreasing under FK-nav."""
    spine_path = REPO_ROOT / "data" / "spine" / f"{state.lower()}_spine.json"
    sidecar = REPO_ROOT / "data" / "out" / f"{state.lower()}_scores_spine.json"
    if not (spine_path.exists() and sidecar.exists()):
        pytest.skip(f"{state} spine/sidecar artifacts not generated")
    spine = SpineIndex.from_state(state)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    lookup = build_mc_lookup(payload.get("scores", []))

    try:
        from src.score.review_loader import load_reviewer_records
    except ImportError:  # pragma: no cover
        pytest.skip("review_loader not importable")
    try:
        reviewer = load_reviewer_records()
    except FileNotFoundError:
        pytest.skip("per-state reviewer workbooks not present")

    no_walk = sum(
        1
        for r in reviewer
        if r.state == state
        and reviewer_key_to_mc_key(r.entity, r.element, lookup) is not None
    )
    with_walk = sum(
        1
        for r in reviewer
        if r.state == state
        and reviewer_key_to_mc_key(r.entity, r.element, lookup, spine=spine)
        is not None
    )
    assert with_walk >= no_walk, (
        f"{state} spine match count regressed under FK-nav: "
        f"{no_walk} → {with_walk}"
    )


# ---------------------------------------------------------------------------
# AZ dotted-path miss diagnostic — issue #138 PR 2(b)
#
# The PR 2(a) walker clears the TX `programTypeDescriptor` cluster and
# the cross-state flat-leaf cohort. AZ has a separate cluster of 32
# in-scope `human_only` rows whose reviewer input writes 2+-segment
# dotted paths the dotted-path walker (_resolve_via_fk_nav) should
# handle but doesn't. This diagnostic surfaces the failure mode per row
# so PR 3 (synonym table) can size the AZ recovery scope without
# re-doing the analysis.
#
# The diagnostic is a re-runnable test, not an acceptance gate — it
# asserts on the SHAPE of the miss distribution (all-or-nothing buckets)
# rather than fixing the count, so prompt drift / sidecar regen don't
# flake it.
# ---------------------------------------------------------------------------


def _classify_az_dotted_miss(
    entity: str,
    element: str,
    lookup: dict,
    spine,
) -> str:
    """Return a bucket name explaining why an AZ dotted-path row didn't resolve.

    Buckets:
    - ``RESOLVED``: keymap finds a match (post-walker; not a miss).
    - ``E1-parent-not-in-spine``: parent entity isn't in the spine.
    - ``E2-follow_reference-miss-{N}``: the N-th non-leaf segment doesn't
      match any reference name on the current entity (most common —
      indicates a sub-collection or unmodeled segment).
    - ``E3-leaf-not-on-target``: walker traverses fine, but no candidate
      of the leaf is documented on the FK target's sidecar rows.
    """
    from src.score.review_keymap import (
        _strip_reviewer_prefix,
        _strip_trailing_paren,
        _dotted_segments,
        reviewer_element_candidates,
        reviewer_key_to_mc_key,
    )
    from src.utils.matching import entity_match_form as _ne

    got = reviewer_key_to_mc_key(entity, element, lookup, spine=spine)
    if got is not None:
        return "RESOLVED"
    cleaned = _strip_trailing_paren(_strip_reviewer_prefix(element)) or element
    segments = _dotted_segments(cleaned)
    if len(segments) < 2:
        return "non-dotted"
    parent_pair = spine.find_entity(entity)
    if parent_pair is None:
        return "E1-parent-not-in-spine"
    cur_name, cur_data = parent_pair
    for i, seg in enumerate(segments[:-1]):
        nxt = spine.follow_reference(cur_data, seg)
        if nxt is None:
            return f"E2-follow_reference-miss-{i}"
        cur_name, cur_data = nxt
    leaf = segments[-1]
    target_norm = _ne(cur_name)
    for cand in reviewer_element_candidates(leaf):
        if lookup.get((target_norm, cand)) is not None:
            return "E0-walker-bug"
    return "E3-leaf-not-on-target"


@pytest.mark.realdata
def test_az_dotted_path_residual_is_subcollection_not_fk():
    """Diagnose AZ's residual `C-dotted-path` cohort.

    Issue #138 PR 2(a) clears the flat-leaf cluster; the dotted-path
    cohort fails earlier in the walker. The diagnostic asserts that the
    residual misses concentrate on the **first segment** of
    ``follow_reference`` — i.e., the reviewer's first dotted token names
    a sub-collection (``Address``, ``Language``, ``Race``,
    ``StudentCharacteristic``, ``StudentIdentificationCode``,
    ``IdentificationCode``, ``ElectronicMail``, ``OfferedGradeLevels``,
    ``CourseLevelCharacteristics``, ``ClassPeriods``, ``services``,
    ``LearningModalities``, ``OtherName``, ``discipline``,
    ``studentDisciplineIncidentBehaviorAssociation``) that the AZ source
    XLSX flattens under a different parent entity (e.g.
    ``StudentDirectoryAddress`` rather than the Ed-Fi canonical
    ``StudentEducationOrganizationAssociationAddress``).

    This is NOT a spine-side fix — the spine correctly omits these
    segments because they aren't FK references. The recovery path is
    a curated synonym table layered on the AZ sub-collection naming
    (PR 3 in the four-PR plan).

    Tracking: issue #142 — AZ sub-collection naming divergence — 32
    reviewer dotted-path rows (#138 PR 2 follow-up).
    """
    az_workbook = REPO_ROOT / "data" / "out" / "arizona_with_mc_scores.xlsx"
    az_sidecar = REPO_ROOT / "data" / "out" / "az_scores_source.json"
    az_spine = REPO_ROOT / "data" / "spine" / "az_spine.json"
    if not (az_workbook.exists() and az_sidecar.exists() and az_spine.exists()):
        pytest.skip("AZ workbook / sidecar / spine artifact missing")

    from openpyxl import load_workbook

    sc = json.loads(az_sidecar.read_text(encoding="utf-8"))
    lookup = build_mc_lookup(sc.get("scores", []))
    spine = SpineIndex.from_state("AZ")
    assert spine is not None

    wb = load_workbook(az_workbook, data_only=True)
    ws = wb["Details"]
    hdr = [c.value for c in ws[1]]
    # Positional layout per `_strip_reviewer_prefix` companion docstring +
    # the issue-#138 prevalence diagnostic: the AZ workbook uses
    # state/entity/element at fixed cols 1/3/4, NACHOS score at col 9, and
    # the AI score column is looked up by header (post-pipeline addition).
    entity_col = 3
    element_col = 4
    human_score_col = 9
    # Option B (#191) renamed the projection header `ai-NACHOS score` →
    # `ai-Base NACHOS Score`; accept either so the diagnostic runs
    # against pre- and post-rename workbooks on disk.
    try:
        ai_score_col = hdr.index("ai-Base NACHOS Score") + 1
    except ValueError:
        ai_score_col = hdr.index("ai-NACHOS score") + 1

    cohort = []
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, human_score_col).value is None:
            continue
        if ws.cell(r, ai_score_col).value is not None:
            continue
        ent = ws.cell(r, entity_col).value or ""
        el = ws.cell(r, element_col).value or ""
        if "." in el and "(ext/" not in el.lower():
            cohort.append((ent, el))

    if not cohort:
        pytest.skip("no AZ dotted-path human-only rows in workbook")

    buckets: dict[str, int] = {}
    for ent, el in cohort:
        b = _classify_az_dotted_miss(ent, el, lookup, spine)
        buckets[b] = buckets.get(b, 0) + 1

    # The diagnostic shape: every miss should land at E2-follow_reference
    # on segment 0 (sub-collection name). If a row resolves it's because
    # PR 2(a)'s leaf-search hit by accident (acceptable), but the bulk of
    # the residual stays at E2-0.
    e2_first_segment = buckets.get("E2-follow_reference-miss-0", 0)
    resolved = buckets.get("RESOLVED", 0)
    leaf_misses = buckets.get("E3-leaf-not-on-target", 0)

    # Sanity: most rows are E2-on-segment-0 (sub-collection names).
    # Conservative threshold so residual cleanup in subsequent PRs
    # doesn't flake the test.
    assert e2_first_segment >= int(0.6 * len(cohort)), (
        f"AZ dotted-path residual shape changed: bucketization={buckets}; "
        f"expected ≥60% at E2-follow_reference-miss-0 "
        f"(sub-collection naming divergence)"
    )
    # Walker bug check: an E0 bucket would mean the walker traversed but
    # missed the leaf despite the lookup carrying it.
    assert buckets.get("E0-walker-bug", 0) == 0, (
        f"unexpected E0-walker-bug bucket: {buckets}"
    )


# ---------------------------------------------------------------------------
# Sub-entity scatter walker — issue #162
#
# Reviewer writes ``(parent, leaf)`` against the canonical Ed-Fi parent
# entity name; the spine partitions the leaf onto a sibling sub-entity
# (sub-collection child or extension-contributed ``*Extension`` /
# ``*Contract``) whose POC-3 record key follows the
# ``{ParentEntity}{SingularSubCollection}`` Ed-Fi naming convention.
# The walker derives the child name from the merged-parent's
# sub-collection map and looks the leaf up there.
# ---------------------------------------------------------------------------


def _employment_contract_catalog() -> dict:
    """Synthetic IN-shaped catalog: parent + extension contributing a sub-collection."""
    return {
        "entities": {
            "StaffEducationOrganizationEmploymentAssociation": {
                "properties": {"hireDate": {}},
                "references": {},
                "sub_collections": {},
            },
        },
        "extensions": {
            "idoe_staffEducationOrganizationEmploymentAssociationExtension": {
                "extends_entity": "StaffEducationOrganizationEmploymentAssociation",
                "properties": {},
                "references": {},
                "sub_collections": {
                    "contract": {
                        "properties": {
                            "contractDays": {},
                            "percentTitleISalary": {},
                            "supplementalSalary": {},
                        },
                    },
                },
            },
        },
    }


def _alt_ed_program_catalog() -> dict:
    """Synthetic IN-shaped catalog: idoe extension entity + plural sub-collection."""
    return {
        "entities": {
            "StudentAlternativeEducationProgramAssociation": {
                "properties": {"beginDate": {}},
                "references": {},
                "sub_collections": {},
            },
        },
        "extensions": {
            "idoe_studentAlternativeEducationProgramAssociation": {
                "extends_entity": "StudentAlternativeEducationProgramAssociation",
                "properties": {},
                "references": {},
                "sub_collections": {
                    "programMeetingTimes": {
                        "properties": {"programMeetingTimeDescriptor": {}},
                    },
                },
            },
        },
    }


def test_subentity_scatter_resolves_extension_contract_sibling():
    """IN's `staffEducationOrganizationEmploymentAssociation | contractDays` cluster.

    Reviewer wrote the parent entity name; the spine scatters
    `contractDays` onto the `idoe_*Extension`'s `contract` sub-collection.
    POC-3 sidecar carries it under
    `StaffEducationOrganizationEmploymentAssociationContract` per the
    `{Parent}{SingularSubCollection}` naming convention.
    """
    spine = SpineIndex.from_catalog(_employment_contract_catalog())
    scores = [
        {"record_key": "IN|StaffEducationOrganizationEmploymentAssociationContract|contractDays"},
        {"record_key": "IN|StaffEducationOrganizationEmploymentAssociationContract|percentTitleISalary"},
        {"record_key": "IN|StaffEducationOrganizationEmploymentAssociationContract|supplementalSalary"},
    ]
    lookup = build_mc_lookup(scores)
    for leaf in ("contractDays", "percentTitleISalary", "supplementalSalary"):
        assert reviewer_key_to_mc_key(
            "staffEducationOrganizationEmploymentAssociation", leaf, lookup, spine=spine
        ) == f"IN|StaffEducationOrganizationEmploymentAssociationContract|{leaf}"


def test_subentity_scatter_strips_idoe_namespace_prefix():
    """`idoe/X` reviewer entity prefix is stripped before scatter lookup.

    Pattern B in issue #162: reviewer writes the API-path-style
    `idoe/studentAlternativeEducationProgramAssociation`; entity
    normalization strips `idoe/` so the lookup lands on the canonical
    Ed-Fi parent before the scatter walker derives the child name.
    """
    spine = SpineIndex.from_catalog(_alt_ed_program_catalog())
    scores = [
        {
            "record_key": (
                "IN|StudentAlternativeEducationProgramAssociationProgramMeetingTime"
                "|programMeetingTimeDescriptor"
            )
        },
    ]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "idoe/studentAlternativeEducationProgramAssociation",
        "programMeetingTimeDescriptor",
        lookup,
        spine=spine,
    ) == (
        "IN|StudentAlternativeEducationProgramAssociationProgramMeetingTime"
        "|programMeetingTimeDescriptor"
    )


def test_subentity_scatter_only_fires_on_single_segment():
    """Multi-segment inputs go through the dotted walker, not the scatter walker.

    Mirrors the activation guard on `_resolve_via_leaf_search` —
    a reviewer's explicit dotted path stays preferred over the implicit
    scatter heuristic.
    """
    spine = SpineIndex.from_catalog(_employment_contract_catalog())
    scores = [
        {"record_key": "IN|StaffEducationOrganizationEmploymentAssociationContract|contractDays"},
    ]
    lookup = build_mc_lookup(scores)
    # Dotted path with bogus first segment — neither the dotted-FK walker nor
    # the leaf-search resolves; the scatter walker must NOT then fire on the
    # leaf alone (would silently override the reviewer's explicit path).
    assert reviewer_key_to_mc_key(
        "staffEducationOrganizationEmploymentAssociation",
        "bogus.contractDays",
        lookup,
        spine=spine,
    ) is None


def test_subentity_scatter_does_not_override_direct_match():
    """Direct same-entity candidate match wins over the scatter walker."""
    spine = SpineIndex.from_catalog(_employment_contract_catalog())
    # Both the parent entity AND the scattered child document the leaf —
    # the parent's direct-cascade hit wins, scatter never fires.
    scores = [
        {"record_key": "IN|StaffEducationOrganizationEmploymentAssociation|contractDays"},
        {"record_key": "IN|StaffEducationOrganizationEmploymentAssociationContract|contractDays"},
    ]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "staffEducationOrganizationEmploymentAssociation", "contractDays", lookup, spine=spine
    ) == "IN|StaffEducationOrganizationEmploymentAssociation|contractDays"


def test_subentity_scatter_skipped_when_spine_omitted():
    """Without spine, the scatter walker can't fire — preserves prior behavior."""
    scores = [
        {"record_key": "IN|StaffEducationOrganizationEmploymentAssociationContract|contractDays"},
    ]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "staffEducationOrganizationEmploymentAssociation", "contractDays", lookup
    ) is None


def test_subentity_scatter_returns_none_when_leaf_not_in_any_subcollection():
    """Honest no-match: the parent's sub-collections don't carry the leaf.

    This is the issue #162 residual case (7 of 11 rows): the reviewer
    rated a field that IDOE's source doc never enumerated, so no scored
    record exists anywhere in the spine sidecar. The walker must return
    None rather than fabricating a match — `no_mc_row` is the honest
    signal per the keymap module's discipline.
    """
    spine = SpineIndex.from_catalog(_employment_contract_catalog())
    scores = [
        {"record_key": "IN|StaffEducationOrganizationEmploymentAssociationContract|contractDays"},
    ]
    lookup = build_mc_lookup(scores)
    assert reviewer_key_to_mc_key(
        "staffEducationOrganizationEmploymentAssociation", "unrelatedField", lookup, spine=spine
    ) is None


def test_subentity_scatter_returns_none_when_derived_child_not_in_lookup():
    """Walker derives the child name correctly but the row isn't scored.

    Mirrors the IDOE extension-row gap pattern (issue #162's
    `studentContactAssociation | legalDesignee`): the spine carries the
    leaf on an extension entity, but the source doc never enumerated it
    so no documented row reaches the sidecar. Walker must return None.
    """
    catalog = {
        "entities": {
            "StudentContactAssociation": {
                "properties": {},
                "references": {},
                "sub_collections": {},
            },
        },
        "extensions": {
            "idoe_studentContactAssociationExtension": {
                "extends_entity": "StudentContactAssociation",
                "properties": {"legalDesignee": {}},
                "references": {},
                "sub_collections": {},
            },
        },
    }
    spine = SpineIndex.from_catalog(catalog)
    # The extension contributes `legalDesignee` directly as a property on
    # the merged parent; no sub-collection scatter applies. Walker returns
    # None; the row falls through to no_mc_row.
    lookup = build_mc_lookup([])
    assert reviewer_key_to_mc_key(
        "studentContactAssociation", "legalDesignee", lookup, spine=spine
    ) is None


@pytest.mark.parametrize(
    "reviewer_pair,expected_suffix",
    [
        # Issue #162 — IN spine-lens cluster the matcher fix recovers.
        # 3× staff employment Contract sibling + 1× alt-ed program sub-collection.
        (
            ("staffEducationOrganizationEmploymentAssociation", "contractDays"),
            "|StaffEducationOrganizationEmploymentAssociationContract|contractDays",
        ),
        (
            ("staffEducationOrganizationEmploymentAssociation", "percentTitleISalary"),
            "|StaffEducationOrganizationEmploymentAssociationContract|percentTitleISalary",
        ),
        (
            ("staffEducationOrganizationEmploymentAssociation", "supplementalSalary"),
            "|StaffEducationOrganizationEmploymentAssociationContract|supplementalSalary",
        ),
        (
            ("idoe/studentAlternativeEducationProgramAssociation", "programMeetingTimeDescriptor"),
            (
                "|StudentAlternativeEducationProgramAssociationProgramMeetingTime"
                "|programMeetingTimeDescriptor"
            ),
        ),
    ],
)
@pytest.mark.realdata
def test_in_spine_subentity_scatter_real_sidecar(reviewer_pair, expected_suffix):
    """Round-trip on the real IN spine catalog + sidecar.

    Skips when the IN spine catalog or sidecar isn't generated locally.
    The four pairs were the matcher-recoverable subset of the issue #162
    11-row residual; the remaining 7 are honest source-doc gaps tracked
    in the issue's "structural divergence" disposition.
    """
    sidecar = REPO_ROOT / "data" / "out" / "in_scores_spine.json"
    spine_path = REPO_ROOT / "data" / "spine" / "in_spine.json"
    if not sidecar.exists() or not spine_path.exists():
        pytest.skip(f"artifact missing: {sidecar} or {spine_path}")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    lookup = build_mc_lookup(payload.get("scores", []))
    spine = SpineIndex.from_state("IN")
    assert spine is not None
    entity, element = reviewer_pair
    got = reviewer_key_to_mc_key(entity, element, lookup, spine=spine)
    assert got is not None, f"sub-entity scatter didn't resolve {reviewer_pair}"
    assert got.endswith(expected_suffix), (
        f"got {got!r}, expected suffix {expected_suffix!r}"
    )


@pytest.mark.realdata
@pytest.mark.parametrize("state", ["WI", "MN", "TX", "AZ"])
def test_other_states_spine_match_count_does_not_regress_under_scatter(state):
    """Acceptance: WI / MN / TX / AZ spine match counts are non-decreasing under scatter walker.

    Issue #162 added a third walker (sub-entity scatter) on top of the
    issue #61 / #138 walkers. Confirm the new walker doesn't accidentally
    over-fire on the four pre-existing states.
    """
    spine_path = REPO_ROOT / "data" / "spine" / f"{state.lower()}_spine.json"
    sidecar = REPO_ROOT / "data" / "out" / f"{state.lower()}_scores_spine.json"
    if not (spine_path.exists() and sidecar.exists()):
        pytest.skip(f"{state} spine/sidecar artifacts not generated")
    spine = SpineIndex.from_state(state)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    lookup = build_mc_lookup(payload.get("scores", []))

    try:
        from src.score.review_loader import load_reviewer_records
    except ImportError:  # pragma: no cover
        pytest.skip("review_loader not importable")
    reviewer = load_reviewer_records()

    # Walker fires only when spine is provided; compare with-walker vs.
    # without-walker counts.
    no_walk = sum(
        1
        for r in reviewer
        if r.state == state
        and reviewer_key_to_mc_key(r.entity, r.element, lookup) is not None
    )
    with_walk = sum(
        1
        for r in reviewer
        if r.state == state
        and reviewer_key_to_mc_key(r.entity, r.element, lookup, spine=spine)
        is not None
    )
    assert with_walk >= no_walk, (
        f"{state} spine match count regressed under scatter walker: "
        f"{no_walk} → {with_walk}"
    )
