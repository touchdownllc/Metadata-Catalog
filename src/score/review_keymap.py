"""Reviewer-file → POC-3 record-key normalizer for Phase E comparison.

The per-state human-scored workbooks under ``docs/human-scored-files/``
(see ``review_loader.REVIEWER_SOURCES``) name (entity, element) pairs
using Ed-Fi-standard conventions (plural entity collections, camelCase
elements, dotted FK-ref navigation paths, ``idoe/`` / ``ed-fi/``
namespace prefixes). POC-3 sidecars name them state-specifically (MN
matrix PascalCase, WI concat of sub-collection name + element, TX TEDS
PascalCase with ``Ext`` suffix). This module bridges the two so a
reviewer row can be joined against a POC-3
``{state}_scores_{lens}.json`` sidecar without renaming either side.

**Spine FK-nav traversal (issue #61, MN-spine widening).** Reviewer
rows like ``staffSectionAssociation | section.schoolId`` enumerate
dotted-FK-nav paths against the Ed-Fi swagger. Source-lens sidecars
(MN Mapping Matrix, WI Confluence) carry the FK as a single column
(``SectionReference``), so reviewer's ``{first}Reference`` candidate
(rule #5) catches them. Spine-lens sidecars flatten references into
their target entities' rows (``Section`` becomes its own row set
with ``localCourseCode``, ``schoolId``, …), so the same reviewer
text doesn't resolve on the same parent entity. ``SpineIndex`` walks
the spine catalog to follow ``{ref}.{leaf}`` to the FK target (and
transitively through that target's references for multi-hop cases
like ``section.schoolId`` → CourseOffering's schoolReference key
property), letting the reviewer row land on the canonical spine row
that owns the leaf.

**Read-only comparison discipline** — see ``docs/archive/next-session/next-session-phase-e.md``.
The keymap never mutates POC-3 artifacts; it produces ``record_key``
strings for the comparison pipeline to look up. No reviewer data is
copied into ``data/``; no rule / prompt / test is seeded from reviewer
values.

**Candidate generation is the whole game.** The normalizer is a
best-effort matcher, not a proof. Where conventions diverge (e.g. MN
Mapping Matrix doesn't enumerate ``calendarCode``), the reviewer row
falls through to ``None`` and the comparison pipeline buckets it as
``no_mc_row``. That's the signal — don't paper over it with fuzzy
string matching.

Match-rate expectations measured against the 2026-04-23 sidecars
(post-Path-B aggregate, post-``{ref}Reference`` rule):

| lens / state | WI | MN | TX |
|---|---:|---:|---:|
| source | ~87% | ~34% | ~85% |
| spine  | ~86% | ~28% | ~38% |

MN reflects Mapping-Matrix coverage thinness; TX-spine reflects the
TEA-extension-only spine scope. Both are real coverage signal, not
naming mismatch — they belong in the ``no_mc_row`` bucket. The
``{ref}Reference`` rule recovered 17 MN-source rows previously lost to
naming — the remainder of MN's ``no_mc_row`` volume is reviewer
enumeration of Ed-Fi swagger sub-collections (``studentIndicators.*``,
``addresses.*``, ``disabilities.*``) that the MN Mapping Matrix
genuinely does not document.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from src.utils.matching import (
    element_aliases,
    normalize_element,
    entity_match_form,
)

_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")
_REVIEWER_PREFIX_RE = re.compile(r"^\s*\(ext/[a-z0-9_-]+\)\s*", re.IGNORECASE)


def _strip_trailing_paren(text: str) -> str:
    """Drop a single trailing parenthetical (``"... (2025-26 onward)"``).

    Reviewer rows frequently annotate elements with effective-year notes
    or extension provenance. The annotation is noise for join purposes.
    """
    return _TRAILING_PAREN_RE.sub("", text).strip()


def _strip_reviewer_prefix(text: str) -> str:
    """Drop a leading ``(ext/{state})`` annotation.

    Doug's AZ workbook flags extension-contributed elements with a
    literal ``(ext/az) `` prefix on the element name (e.g.
    ``(ext/az) FinalLetterGradeDescriptor``). The annotation is reviewer
    bookkeeping, not part of the Ed-Fi identifier — strip it before
    candidate generation so all downstream rules see the bare element.
    Case-insensitive on the whole prefix; tolerates leading whitespace.
    """
    return _REVIEWER_PREFIX_RE.sub("", text).strip()


def _dotted_segments(element: str) -> list[str]:
    """Split an element on ``.`` / ``>`` into ordered segments."""
    normalized = normalize_element(element)
    # Arrow-separated navigation paths (MN Mapping Matrix style) behave
    # like dots for splitting.
    normalized = re.sub(r"\s*>\s*", ".", normalized)
    return [p for p in normalized.split(".") if p]


def reviewer_element_candidates(element: str) -> list[str]:
    """All lowercased element forms to try when matching to a POC-3 key.

    Returned in **priority order** — more-specific candidates first,
    broader fallbacks last — so ``reviewer_key_to_mc_key`` prefers
    the most semantically faithful match when multiple POC-3 records
    are compatible. E.g. WI ``StudentSchoolAssociation`` has both
    ``schoolId`` and ``schoolReference``; the reviewer's
    ``school.schoolId`` must prefer ``schoolId`` (path-tail) over
    ``schoolReference`` (``{ref}Reference`` fallback).

    Duplicates are filtered in insertion order.

    Builds from nine sources, each motivated by a measured reviewer
    convention (see module docstring for match-rate context):

    1. **Direct + existing aliases.** ``element_aliases`` already
       handles path-tail splits, colon/arrow normalization, plural
       sub-collection flips, whitespace-to-PascalCase. Lowered to
       case-fold against POC-3 keys.
    2. **Trailing-parenthetical strip.** Reviewer rows like
       ``nextYearSchool.schoolId (For 2025-26 onward)`` — the notation
       is not part of the Ed-Fi identifier and must be stripped before
       matching.
    9. **Reviewer extension-provenance prefix strip.** Doug's AZ
       workbook tags extension-contributed elements with a literal
       ``(ext/az)`` prefix (e.g. ``(ext/az) FinalLetterGradeDescriptor``).
       Stripped at the head of the cascade so all downstream rules see
       the bare element. Issue #138.
    3. **Dotted-concat.** ``gradeLevels.gradeLevelDescriptor`` → the
       POC-3 concat form ``gradeLevelsgradeLevelDescriptor`` emitted by
       ``spine/unflatten.py`` for sub-collection elements. Also
       emits drop-leading-ref variants (``section.CourseOffering.SchoolId``
       → ``CourseOfferingSchoolId``, ``SchoolId``).
    4. **Leading segment.** For FK reference paths like
       ``School.SchoolId``, POC-3 state source docs sometimes store the
       FK as the parent-entity column (``School``) rather than its
       primary key. Emit the first segment as a candidate.
    5. **Leading-segment ``Reference`` suffix** *(fallback — after
       path-tail).* MN source + spine sidecars frequently store an FK
       as ``{ref}Reference`` — e.g. reviewer
       ``educationOrganization.educationOrganizationId`` maps to POC-3
       ``educationOrganizationReference``. Measured gain on MN source:
       +17 in-scope reviewer rows (2026-04-23 digest). Runs after the
       path-tail and leading-segment rules so that states which
       enumerate both the leaf field AND a ``Reference`` column (WI)
       prefer the leaf match the reviewer's dotted path implied.
    6. **``{ref}Reference{leaf}`` concat.** WI source emits FK paths
       as a single concatenated column (``schoolYearTypeReferenceschoolYear``)
       rather than splitting the leaf into its own field. Reviewer
       writes the dotted form (``schoolYearType.schoolYear``); this
       candidate adds ``{first}Reference{tailConcat}`` so the joined
       column resolves. Layered on the existing ``{ref}Reference``
       rule (#5) to cover both column conventions.
    7. **Collection-prefix descriptor.** WI source concatenates a
       sub-collection name onto its child descriptor
       (``gradeLevels`` × ``gradeLevelDescriptor`` =
       ``gradeLevelsgradeLevelDescriptor``). Reviewer writes the bare
       descriptor; this candidate infers the parent-collection prefix
       by stripping ``Descriptor`` and pluralizing the resulting noun.
       Fires only on single-segment descriptor inputs — multi-segment
       forms already produce the concat via the dotted-concat rule.
    8. **Descriptor-strip fallback.** MN Mapping Matrix uses
       enumeration names (``CalendarType``) where the reviewer uses the
       Ed-Fi descriptor form (``calendarTypeDescriptor``). Trailing
       ``descriptor`` is stripped as a last-resort candidate. The
       inverse (add ``Descriptor``) lives on the POC-3-side lookup
       generator, not here, to keep the reviewer-candidate set bounded.
    """
    # Two head-of-cascade strips: the reviewer-provenance prefix
    # (``(ext/az) Foo`` → ``Foo``) runs first so the trailing-paren
    # cleanup sees the bare element, and all downstream rules see
    # neither annotation.
    cleaned = _strip_trailing_paren(_strip_reviewer_prefix(element))
    # Reviewer cells sometimes carry embedded whitespace from in-cell
    # text wrapping (e.g. AZ ``"TrackLocalEducationAgencyReference\n.LocalEducationAgencyId"``).
    # Collapse internal whitespace runs and trim around the ``.``
    # separator so subsequent dotted-path / segment logic treats the
    # form as a clean sequence of tokens. Conservative — pure-token
    # element names (no whitespace) are unaffected.
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s*\.\s*", ".", cleaned).strip()
    # Process the cleaned form only — the annotated form (e.g.
    # ``"schoolId (2025-26 onward)"``) never matches any POC-3 key and
    # only inflates the candidate set with noise.
    src = cleaned or element

    ordered: list[str] = []
    seen: set[str] = set()

    def _push(cand: str) -> None:
        if cand not in seen:
            seen.add(cand)
            ordered.append(cand)

    _push(normalize_element(src).lower())
    for alias in element_aliases(src):
        _push(alias.lower())

    segments = _dotted_segments(src)
    if len(segments) > 1:
        _push("".join(segments).lower())
        for idx in range(1, len(segments)):
            _push("".join(segments[idx:]).lower())
        first = segments[0].lower()
        tail_concat = "".join(segments[1:]).lower()
        _push(first)
        # {ref}Reference fallback — runs AFTER path-tail/leading-segment
        # so states enumerating both a leaf field and a Reference column
        # resolve to the leaf, matching the reviewer's dotted intent.
        _push(first + "reference")
        _push(first + "references")
        # `{ref}Reference{leaf}` concat — WI source emits FK paths as a
        # single concatenated column (e.g. `schoolYearTypeReferenceschoolYear`)
        # rather than splitting the FK leaf into its own field. Reviewer
        # writes the dotted form (`schoolYearType.schoolYear`); this
        # candidate bridges the two.
        if tail_concat:
            _push(first + "reference" + tail_concat)

    # Collection-prefix descriptor — WI source concatenates a sub-collection
    # name onto its child descriptor (e.g. `gradeLevels` × `gradeLevelDescriptor`
    # = `gradeLevelsgradeLevelDescriptor`). Reviewer writes the bare descriptor
    # (`gradeLevelDescriptor`); infer the parent-collection prefix by stripping
    # the `Descriptor` suffix and pluralizing the resulting noun, then prepend
    # back onto the original. Fires only on single-segment descriptor inputs
    # (multi-segment forms already produce the concat via the dotted-concat
    # rule above).
    if len(segments) <= 1:
        bare = src
        if bare.lower().endswith("descriptor") and len(bare) > len("Descriptor"):
            stem = bare[: -len("Descriptor")]
            if stem and stem[0].isalpha():
                # Pluralize the stem: `gradeLevel` → `gradeLevels`,
                # `calendarEvent` → `calendarEvents`. Conservative — append
                # `s` only; if a stem ends in `y` the existing `element_aliases`
                # path-tail logic handles plural variants.
                if not stem.lower().endswith("s"):
                    plural = stem + "s"
                    _push((plural + bare).lower())

    # Descriptor-strip is always last-resort. Widened (issue #138) to
    # also catch ``*DescriptorId`` / ``*DescriptorID`` inputs — Doug's
    # AZ workbook writes the bare descriptor (``CourseAttemptResultDescriptor``)
    # while the AZ source XLSX surfaces the FK-resolved column name
    # (``CourseAttemptResultDescriptorId``). Emit both the ``Id``-stripped
    # form (``*descriptor``) and the bare stem (``*``) so either side of
    # the join can land. The original descriptor-strip still fires from
    # the snapshot, producing the bare stem from inputs that arrive as
    # ``*Descriptor`` directly.
    for cand in list(ordered):
        if cand.endswith("descriptorid") and len(cand) > len("descriptorid"):
            _push(cand[:-2])                            # *descriptor
            _push(cand[: -len("descriptorid")])         # bare stem
        elif cand.endswith("descriptor") and len(cand) > len("descriptor"):
            _push(cand[: -len("descriptor")])

    return ordered


# Issue #138 PR 3 — Pattern D curated synonyms.
#
# Reviewer rows whose `(entity, element)` pair can't be reconciled by the
# generic candidate cascade or the FK walker, but DO have a documented
# semantic counterpart in the POC-3 sidecar under a different name (or
# different parent entity), get a curated entry here. The table maps
# `(reviewer_entity, reviewer_element) → (target_entity, target_element)`
# — both lookup sides resolve through `entity_match_form` / lower-case so
# entries can stay in canonical Ed-Fi PascalCase regardless of how the
# reviewer / sidecar happens to spell them.
#
# Discipline:
# - One entry per measured human-only row that has a real semantic
#   target. Speculative pairs ("close enough") stay out — better to
#   surface as Pattern F (no recovery) than to misjoin.
# - `dict.setdefault` semantics preserve exact-name matches; synonyms
#   only fill gaps the cascade left.
# - Cross-entity entries (e.g. Ed-Fi 4.0 demographics moving from
#   `Student` to `StudentEducationOrganizationAssociation`, AZ flattening
#   demographics under `StudentDemographic`) are encoded as the
#   tuple-typed values; same-entity renames also use the tuple form so
#   the table reads uniformly.
# - Reviewer typos (e.g. AZ `actionDisciplineActionLength` ←
#   `ActualDisciplineActionLength`, AZ `GerenationCodeSuffix` ←
#   `GenerationCodeSuffix`) join here rather than the prompt — the
#   reviewer file is read-only and we adapt the join.
_REVIEWER_TO_MC_SYNONYMS: dict[
    str, dict[tuple[str, str], tuple[str, str]]
] = {
    "WI": {
        # Same-entity rename
        ("Student", "MultipleBirthIndicator"): ("Student", "multipleBirthStatus"),
        # Ed-Fi 4.0 moved Student demographics onto
        # StudentEducationOrganizationAssociation (per-EdOrg-scoped).
        ("Student", "SexType"): (
            "StudentEducationOrganizationAssociation",
            "sexDescriptor",
        ),
        ("Student", "RaceCodes"): (
            "StudentEducationOrganizationAssociation",
            "raceDescriptor",
        ),
        ("Student", "IsHispanicLatino"): (
            "StudentEducationOrganizationAssociation",
            "hispanicLatinoEthnicity",
        ),
        # Staff still carries demographics inline in WI source.
        ("Staff", "SexType"): ("Staff", "sexDescriptor"),
        ("Staff", "IsHispanicLatino"): ("Staff", "hispanicLatinoEthnicity"),
        ("Staff", "EntityID"): ("Staff", "staffUniqueId"),
        # Issue #138 PR 5 — BirthLocation sub-collection flatten.
        # WI source-of-truth doesn't carry a `BirthLocation` sub-collection
        # entity; instead it inlines the leaves on Student with a `birth`
        # prefix (e.g. `birthCity`, `birthCountryDescriptor`,
        # `birthStateAbbreviationDescriptor`). The reviewer wrote the
        # canonical Ed-Fi dotted form. Three entries — `BirthLocation.County`
        # is excluded (no `birthCounty` in sidecar; Pattern F).
        ("Student", "BirthLocation.City"): ("Student", "birthCity"),
        ("Student", "BirthLocation.Country"): (
            "Student",
            "birthCountryDescriptor",
        ),
        ("Student", "BirthLocation.StateAbbreviation"): (
            "Student",
            "birthStateAbbreviationDescriptor",
        ),
    },
    "AZ": {
        # AZ source flattens sub-collections under shorter parent entity
        # names (e.g. `CalendarDateCalendarEvent` for the canonical
        # `CalendarDate.CalendarEvent` sub-collection), and demographics
        # live under `StudentDemographic` / `StaffDemographic` rather
        # than the canonical `*EducationOrganizationAssociation` parents.
        # Issue #147 v26: the `CalendarDate.CalendarEventDescriptor`
        # synonym that previously mapped reviewer rows to the renamed
        # wrapper entity is now obsolete — leaf-borrow appends a
        # canonical `CalendarDate.calendarEventDescriptor` row to the
        # source-lens artifact, which the keymap's exact-name resolution
        # finds first.
        ("StudentEducationOrganizationAssociation", "SexDescriptor"): (
            "StudentDemographic",
            "SexDescriptorId",
        ),
        ("StudentEducationOrganizationAssociation", "HispanicLatinoEthnicity"): (
            "StudentDemographic",
            "HispanicLatinoEthnicity",
        ),
        ("StudentEducationOrganizationAssociation", "TribalAffiliationDescriptor"): (
            "StudentDemographicTribalAffiliation",
            "TribalAffiliationDescriptorId",
        ),
        ("Staff", "SexDescriptor"): ("StaffDemographic", "SexDescriptorId"),
        ("Staff", "HispanicLatinoEthnicity"): (
            "StaffDemographic",
            "HispanicLatinoEthnicity",
        ),
        # Reviewer typos (one-letter mistakes in Doug's AZ workbook).
        ("DisciplineAction", "actionDisciplineActionLength"): (
            "DisciplineAction",
            "ActualDisciplineActionLength",
        ),
        ("Staff", "GerenationCodeSuffix"): ("Staff", "GenerationCodeSuffix"),
        # Reviewer dropped the `Indicator` suffix that AZ source omits too.
        ("StudentSectionAssociation", "DualCreditIndicator"): (
            "StudentSectionAssociation",
            "DualCredit",
        ),
        # Issue #138 PR 5 — `Reference` suffix drop on AZ Calendar's
        # extension-contributed `TrackLocalEducationAgency` field. After
        # the existing `(ext/az)` prefix-strip (PR #140), the residual
        # `TrackLocalEducationAgencyReference` doesn't match the sidecar's
        # `TrackLocalEducationAgency`. The synonym key is the post-strip
        # form because `reviewer_element_candidates` strips `(ext/state)`
        # head-of-cascade before any lookup.
        ("Calendar", "TrackLocalEducationAgencyReference"): (
            "Calendar",
            "TrackLocalEducationAgency",
        ),
    },
    "TX": {
        # TX TWEDS sidecar uses lowerCamelCase + ``Descriptor`` suffix
        # for descriptor-typed fields; the reviewer writes the bare
        # PascalCase stem (Ed-Fi convention before TX's TWEDS rename
        # pass). Same-entity rename across the board — no parent move.
        #
        # PriorYearLeaver: TX-extension entity capturing snapshot data
        # for students who exited in the prior reporting year. Carries
        # demographics + diploma + endorsement + post-secondary fields
        # inline.
        ("PriorYearLeaver", "GradeLevel"): ("PriorYearLeaver", "gradeLevelDescriptor"),
        ("PriorYearLeaver", "ExitWithdrawType"): (
            "PriorYearLeaver",
            "exitWithdrawTypeDescriptor",
        ),
        ("PriorYearLeaver", "Sex"): ("PriorYearLeaver", "sexDescriptor"),
        ("PriorYearLeaver", "AssociateDegreeIndicator"): (
            "PriorYearLeaver",
            "associateDegreeIndicatorDescriptor",
        ),
        ("PriorYearLeaver", "FinancialAidApplication"): (
            "PriorYearLeaver",
            "financialAidApplicationDescriptor",
        ),
        ("PriorYearLeaver", "EndorsementCompleted"): (
            "PriorYearLeaver",
            "endorsementCompletedDescriptor",
        ),
        ("PriorYearLeaver", "PostSecondaryCertificationLicensure"): (
            "PriorYearLeaver",
            "postSecondaryCertificationLicensureDescriptor",
        ),
        ("PriorYearLeaver", "PostSecondaryCertLicensureResult"): (
            "PriorYearLeaver",
            "postSecondaryCertLicensureResultDescriptor",
        ),
        ("PriorYearLeaver", "IBCVendor"): (
            "PriorYearLeaver",
            "ibcVendorDescriptor",
        ),
        ("PriorYearLeaver", "DiplomaType"): (
            "PriorYearLeaver",
            "diplomaTypeDescriptor",
        ),
        ("PriorYearLeaver", "AchievementCategory"): (
            "PriorYearLeaver",
            "achievementCategoryDescriptor",
        ),
        ("PriorYearLeaver", "TexasFirstEarlyHSCompletionProgram"): (
            "PriorYearLeaver",
            "texasFirstEarlyHSCompletionProgramDescriptor",
        ),
        ("PriorYearLeaver", "AddressType"): (
            "PriorYearLeaver",
            "addressTypeDescriptor",
        ),
        ("PriorYearLeaver", "StateAbbreviation"): (
            "PriorYearLeaver",
            "stateAbbreviationDescriptor",
        ),
        ("PriorYearLeaver", "ElectronicMailType"): (
            "PriorYearLeaver",
            "electronicMailTypeDescriptor",
        ),
        ("PriorYearLeaver", "TelephoneNumberType"): (
            "PriorYearLeaver",
            "telephoneNumberTypeDescriptor",
        ),
        # PriorYearLeaverParent — same TWEDS naming convention.
        ("PriorYearLeaverParent", "GenerationCode"): (
            "PriorYearLeaverParent",
            "generationCodeDescriptor",
        ),
        ("PriorYearLeaverParent", "AddressType"): (
            "PriorYearLeaverParent",
            "addressTypeDescriptor",
        ),
        ("PriorYearLeaverParent", "StateAbbreviation"): (
            "PriorYearLeaverParent",
            "stateAbbreviationDescriptor",
        ),
        ("PriorYearLeaverParent", "ElectronicMailType"): (
            "PriorYearLeaverParent",
            "electronicMailTypeDescriptor",
        ),
        ("PriorYearLeaverParent", "TelephoneNumberType"): (
            "PriorYearLeaverParent",
            "telephoneNumberTypeDescriptor",
        ),
        # PriorYearLeaverStudentParentAssociation — FK keys flatten in
        # the sidecar without the dotted-path nav the reviewer used.
        ("PriorYearLeaverStudentParentAssociation", "PriorYearLeaverParent.ParentUniqueId"): (
            "PriorYearLeaverStudentParentAssociation",
            "priorYearLeaverParentParentUId",
        ),
        ("PriorYearLeaverStudentParentAssociation", "PriorYearLeaver.StudentUniqueId"): (
            "PriorYearLeaverStudentParentAssociation",
            "priorYearLeaverStudentUId",
        ),
        ("PriorYearLeaverStudentParentAssociation", "Relation"): (
            "PriorYearLeaverStudentParentAssociation",
            "relationDescriptor",
        ),
        # StudentDisciplineIncidentAssociation — bare ``Behavior`` stem
        # in reviewer; sidecar carries the descriptor-suffixed name.
        ("StudentDisciplineIncidentAssociation", "Behavior"): (
            "StudentDisciplineIncidentAssociation",
            "behaviorDescriptor",
        ),
        # LocalEducationAgency — TX TWEDS source omits the LEA prefix
        # the reviewer kept in the workbook.
        ("LocalEducationAgency", "LEAGrievanceLink"): (
            "LocalEducationAgency",
            "GrievanceLink",
        ),
    },
}


def build_mc_lookup(
    scores: Iterable[dict],
    *,
    state: str | None = None,
) -> dict[tuple[str, str], str]:
    """Build ``(normalized_entity, lowered_element_alias) → record_key`` for MC sidecar.

    Registers the direct key PLUS every form returned by
    ``element_aliases`` for the element, so reviewer-side candidates
    that land on any alias resolve to the MC key. Collisions favor
    the first-registered key — ``dict.setdefault`` keeps the mapping
    deterministic (input order is stable because sidecars are
    sorted by record_key at write time).

    Expects each ``scores`` entry to have a ``record_key`` of the
    shape ``"{STATE}|{Entity}|{element}"`` as written by
    ``src.score.aggregate._scored_record_to_dict``.

    When ``state`` is provided AND the state has a curated synonym map
    in ``_REVIEWER_TO_MC_SYNONYMS``, a second pass registers each
    ``(reviewer_entity, reviewer_element) → record_key`` mapping by
    resolving the synonym target against the already-built lookup.
    Synonym entries whose target isn't in the sidecar are silently
    skipped (defensive — keeps the table forward-compatible with
    sidecar regen). ``setdefault`` semantics ensure curated synonyms
    never override an exact-name match. Issue #138 PR 3.
    """
    lookup: dict[tuple[str, str], str] = {}
    for entry in scores:
        key = entry.get("record_key")
        if not key:
            continue
        parts = key.split("|", 2)
        if len(parts) != 3:
            continue
        _, entity, element = parts
        ent_n = entity_match_form(entity)
        lookup.setdefault((ent_n, element.lower()), key)
        for alias in element_aliases(element):
            lookup.setdefault((ent_n, alias.lower()), key)
        # Issue #138 Pattern A — sidecar elements ending in ``DescriptorId``
        # / ``DescriptorID`` register ``*Descriptor`` and bare-stem
        # aliases so reviewer rows that write the bare descriptor (Doug's
        # AZ convention) resolve here. ``setdefault`` keeps the original
        # exact-name registration winning on collisions.
        el_lower = element.lower()
        if el_lower.endswith("descriptorid") and len(el_lower) > len("descriptorid"):
            descriptor_form = el_lower[:-2]                       # *descriptor
            bare_stem = el_lower[: -len("descriptorid")]          # *
            lookup.setdefault((ent_n, descriptor_form), key)
            lookup.setdefault((ent_n, bare_stem), key)

    # Pattern D synonym pass — runs after the flat alias registration so
    # curated synonyms never override an exact-name match.
    if state:
        synonyms = _REVIEWER_TO_MC_SYNONYMS.get(state.upper(), {})
        for (rev_entity, rev_element), (tgt_entity, tgt_element) in synonyms.items():
            tgt_key = lookup.get(
                (entity_match_form(tgt_entity), tgt_element.lower())
            )
            if tgt_key is None:
                continue
            lookup.setdefault(
                (entity_match_form(rev_entity), rev_element.lower()),
                tgt_key,
            )
    return lookup


class SpineIndex:
    """Pre-processed spine catalog optimized for FK-nav traversal.

    Merges core entities and their extensions so an extension-only
    entity (MN program associations, TEA ``tx_*`` new entities) carries
    its references through the same lookup surface as core entities.
    Holds a normalized-entity-name index so callers can resolve a
    reviewer's plural / casing-different entity name in one dict hit.

    Built lazily via ``SpineIndex.from_state(state)``; tests can also
    pass a pre-loaded catalog dict via ``SpineIndex.from_catalog``.
    """

    def __init__(self, entities: dict[str, dict]):
        self._entities = entities
        self._by_norm: dict[str, tuple[str, dict]] = {}
        for name, data in entities.items():
            self._by_norm[entity_match_form(name)] = (name, data)

    @classmethod
    def from_catalog(cls, catalog: dict) -> "SpineIndex":
        """Build a SpineIndex from a raw spine catalog dict.

        Merges per-extension references / properties / sub-collections
        onto the extended entity. Extension-only entities (``extends_entity``
        names a concrete entity that ISN'T itself in ``entities``) get a
        synthetic merged record so reviewer rows on those entities still
        resolve through the FK-nav walker.
        """
        merged: dict[str, dict] = {}
        for name, data in catalog.get("entities", {}).items():
            merged[name] = {
                "properties": dict(data.get("properties", {})),
                "references": dict(data.get("references", {})),
                "sub_collections": dict(data.get("sub_collections", {})),
            }
        for ext_data in catalog.get("extensions", {}).values():
            target = ext_data.get("extends_entity")
            if not target:
                continue
            entry = merged.setdefault(
                target,
                {"properties": {}, "references": {}, "sub_collections": {}},
            )
            entry["properties"].update(ext_data.get("properties", {}))
            entry["references"].update(ext_data.get("references", {}))
            entry["sub_collections"].update(ext_data.get("sub_collections", {}))
        return cls(merged)

    @classmethod
    def from_state(cls, state: str, spine_path: Path | None = None) -> "SpineIndex | None":
        """Load a state's spine and build the index, or return None when absent.

        ``spine_path`` override is for tests; default reads from
        ``data/spine/{state}_spine.json`` via ``utils.paths``.
        """
        from src.utils.paths import state_spine_path

        path = spine_path or state_spine_path(state.upper())
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_catalog(payload.get("catalog", {}))

    def find_entity(self, name: str) -> tuple[str, dict] | None:
        """Return ``(canonical_name, entity_data)`` for an entity by any casing/plural form."""
        return self._by_norm.get(entity_match_form(name))

    def follow_reference(self, entity_data: dict, segment: str) -> tuple[str, dict] | None:
        """Follow a path segment through ``entity_data.references``.

        ``segment`` may be the bare reference name (``section``), the
        Swagger ``Reference`` form (``sectionReference``), or any case
        variant. Returns the FK target's ``(canonical_name, data)`` if
        the target is in the index, or ``(target_name, {})`` for an
        abstract/missing target so callers can still attempt a lookup
        (e.g., polymorphic ``EducationOrganization`` on Ed-Fi 4.0).

        **Pluralized reference segments (issue #166).** MN reviewer rows
        write FK-nav segments in the *plural* collection form the source
        matrix uses — e.g. ``studentSchoolFoodServiceProgramAssociation |
        programs.programName`` where the spine reference is the singular
        ``programReference``. When the literal segment doesn't match any
        reference, retry with the depluralized form (``programs`` →
        ``program``) so the reviewer's plural path resolves to the
        canonical singular reference. Purely additive — the singular retry
        only fires after the literal comparison misses, so no existing
        resolution changes.
        """
        refs = entity_data.get("references", {})

        def _match(seg_lower: str) -> tuple[str, dict] | None:
            # Reviewer rows sometimes write the bare prefix (``section``)
            # and sometimes the full reference name (``sectionReference``).
            # Match against both forms with one comparison.
            for ref_name, ref_data in refs.items():
                rn_lower = ref_name.lower()
                rn_short = (
                    rn_lower[: -len("reference")]
                    if rn_lower.endswith("reference")
                    else rn_lower
                )
                if rn_lower == seg_lower or rn_short == seg_lower:
                    target = ref_data.get("entity")
                    if not target:
                        return None
                    hit = self._by_norm.get(entity_match_form(target))
                    return hit if hit is not None else (target, {})
            return None

        # Pass 1 — literal segment (unchanged behavior; literal always wins).
        hit = _match(segment.lower())
        if hit is not None:
            return hit
        # Pass 2 — depluralized fallback (issue #166). MN reviewer rows write
        # FK-nav segments in the plural collection form the source matrix
        # uses (``programs.programName``) where the spine reference is the
        # singular ``programReference``. Only fires when the literal segment
        # matched nothing, so no existing resolution changes.
        if segment:
            from src.spine.unflatten import _depluralize

            dep = _depluralize(segment[0].upper() + segment[1:]).lower()
            if dep != segment.lower():
                return _match(dep)
        return None

    def references(self, entity_data: dict) -> Iterable[tuple[str, dict]]:
        """Yield ``(ref_name, ref_data)`` pairs for an entity's references."""
        return entity_data.get("references", {}).items()


def _resolve_via_fk_nav(
    entity: str,
    element: str,
    lookup: dict[tuple[str, str], str],
    spine: SpineIndex,
) -> str | None:
    """Resolve a reviewer ``{ref}.{leaf}`` (or deeper) path via spine traversal.

    Walks the dotted path through the spine's reference graph: each
    non-leaf segment must match a reference on the current entity; the
    last segment is the leaf to look up. Resolution is two-stage:

    1. Try ``(target_entity, leaf_candidates)`` directly — handles
       ``courseOffering | course.courseCode`` → ``MN|Course|courseCode``.
    2. If the leaf isn't documented on the target, walk the target's
       references for any whose ``key_properties`` contain the leaf,
       then try ``(further_target, leaf_candidates)``. This catches the
       multi-hop case (``staffSectionAssociation | section.schoolId``,
       where Section's ``courseOfferingReference`` carries ``schoolId``
       and CourseOffering owns the documented row).

    Hop budget caps at 3 to keep traversal bounded — schoolId-style
    keys can transit Section → CourseOffering → School in the worst
    case. Visited-set prevents cycles in mutually-referencing entities.
    """
    cleaned = _strip_trailing_paren(element.strip()) or element.strip()
    segments = _dotted_segments(cleaned)
    if len(segments) < 2:
        return None
    parent_pair = spine.find_entity(entity.strip())
    if parent_pair is None:
        return None

    cur_name, cur_data = parent_pair
    visited = {cur_name}
    for seg in segments[:-1]:
        nxt = spine.follow_reference(cur_data, seg)
        if nxt is None:
            return None
        cur_name, cur_data = nxt
        visited.add(cur_name)

    leaf = segments[-1]

    def _try(target_name: str) -> str | None:
        target_norm = entity_match_form(target_name)
        for cand in reviewer_element_candidates(leaf):
            hit = lookup.get((target_norm, cand))
            if hit is not None:
                return hit
        return None

    direct = _try(cur_name)
    if direct is not None:
        return direct

    return _walk_transitive_for_leaf(cur_data, leaf, lookup, spine, visited, depth=3)


def _walk_transitive_for_leaf(
    entity_data: dict,
    leaf: str,
    lookup: dict[tuple[str, str], str],
    spine: SpineIndex,
    visited: set[str],
    depth: int,
) -> str | None:
    """Search references whose ``key_properties`` contain ``leaf``, then recurse.

    The schoolId leaf on Section ultimately owes to CourseOffering's
    schoolReference, which Section borrows via its courseOfferingReference.
    The walker checks each reference's key_properties for a case-insensitive
    leaf match, follows the reference to its target, and tries the lookup
    there. Recurses up to ``depth`` hops total.
    """
    if depth <= 0:
        return None
    leaf_lower = leaf.lower()
    for _, ref_data in spine.references(entity_data):
        kp = ref_data.get("key_properties", {})
        if not any(k.lower() == leaf_lower for k in kp):
            continue
        target = ref_data.get("entity")
        if not target or target in visited:
            continue
        target_norm = entity_match_form(target)
        for cand in reviewer_element_candidates(leaf):
            hit = lookup.get((target_norm, cand))
            if hit is not None:
                return hit
        target_pair = spine.find_entity(target)
        if target_pair is None:
            continue
        result = _walk_transitive_for_leaf(
            target_pair[1], leaf, lookup, spine, visited | {target}, depth - 1
        )
        if result is not None:
            return result
    return None


def _key_property_matches_leaf(
    key_properties: dict, leaf_candidates: set[str]
) -> bool:
    """True if any key-property name shares a candidate form with the leaf.

    Compares both directions through ``reviewer_element_candidates`` so a
    key-property like ``programTypeDescriptor`` matches a reviewer leaf of
    ``programType`` (descriptor-strip alias) or ``programTypeDescriptor``
    (direct). Keeps the comparison symmetric — the candidate generator is
    already bounded, so the per-reference work stays cheap.
    """
    for kp_name in key_properties:
        kp_cands = {c.lower() for c in reviewer_element_candidates(kp_name)}
        kp_cands.add(kp_name.lower())
        if kp_cands & leaf_candidates:
            return True
    return False


def _resolve_via_leaf_search(
    entity: str,
    element: str,
    lookup: dict[tuple[str, str], str],
    spine: SpineIndex,
) -> str | None:
    """Resolve a single-segment reviewer leaf via parent-entity FK-walk.

    Activates after the direct candidate cascade AND
    ``_resolve_via_fk_nav`` (dotted-path walker) miss. The reviewer wrote
    a flat element name (no dotted path) that the parent entity doesn't
    document directly — but a reference on the parent owns the leaf as a
    key property, so the documented row lives on that reference's target
    (or transitively through further references).

    Issue #138 PR 2: closes TX's ``StudentLanguageInstructionProgramAssociation
    | programTypeDescriptor`` cluster (the human writes the descriptor
    flat; the documented row sits on Program via ``programReference``).
    Same shape recovers ``Section | SchoolYear`` (via Session) and
    ``StudentSectionAssociation | localCourseCode`` (via CourseOffering)
    as side-effects.

    Hop budget = 3, mirroring ``_walk_transitive_for_leaf``. The
    visited-set prevents cycles in mutually-referencing entity graphs.
    """
    cleaned = _strip_trailing_paren(_strip_reviewer_prefix(element.strip())) or element.strip()
    segments = _dotted_segments(cleaned)
    if len(segments) != 1:
        return None
    parent_pair = spine.find_entity(entity.strip())
    if parent_pair is None:
        return None
    cur_name, cur_data = parent_pair
    leaf = segments[0]
    leaf_candidates = {c.lower() for c in reviewer_element_candidates(leaf)}
    return _walk_for_leaf_via_candidates(
        cur_data, leaf_candidates, lookup, spine, {cur_name}, depth=3
    )


def _walk_for_leaf_via_candidates(
    entity_data: dict,
    leaf_candidates: set[str],
    lookup: dict[tuple[str, str], str],
    spine: SpineIndex,
    visited: set[str],
    depth: int,
) -> str | None:
    """Search references whose ``key_properties`` share a candidate with the leaf.

    Same shape as ``_walk_transitive_for_leaf`` but matches via the
    reviewer-side candidate set on both the key-property name and the
    leaf, so descriptor-strip / Id-strip aliases bridge them. Used by the
    single-segment leaf-search path; the dotted-FK walker still uses the
    exact-name match because the dotted parser has already split the leaf
    off as a literal token.
    """
    if depth <= 0:
        return None
    for _, ref_data in spine.references(entity_data):
        kp = ref_data.get("key_properties", {})
        if not _key_property_matches_leaf(kp, leaf_candidates):
            continue
        target = ref_data.get("entity")
        if not target or target in visited:
            continue
        target_norm = entity_match_form(target)
        for cand in leaf_candidates:
            hit = lookup.get((target_norm, cand))
            if hit is not None:
                return hit
        target_pair = spine.find_entity(target)
        if target_pair is None:
            continue
        result = _walk_for_leaf_via_candidates(
            target_pair[1], leaf_candidates, lookup, spine, visited | {target}, depth - 1
        )
        if result is not None:
            return result
    return None


def _resolve_via_subentity_scatter(
    entity: str,
    element: str,
    lookup: dict[tuple[str, str], str],
    spine: SpineIndex,
) -> str | None:
    """Resolve a reviewer flat-leaf when the documented row sits on a sibling sub-entity.

    Activates after the direct cascade and the dotted-FK / leaf-search
    walkers miss. The reviewer wrote ``(parent, leaf)`` against the
    canonical Ed-Fi parent entity name; the spine partitioned the leaf
    onto a sub-collection or extension-contributed child entity whose
    POC-3 record key follows the
    ``{ParentEntity}{SingularSubCollection}`` Ed-Fi naming convention.

    Issue #162: closes IN's ``staffEducationOrganizationEmploymentAssociation
    | contractDays`` cluster (lives on
    ``StaffEducationOrganizationEmploymentAssociationContract``) and
    ``idoe/studentAlternativeEducationProgramAssociation
    | programMeetingTimeDescriptor`` (lives on
    ``StudentAlternativeEducationProgramAssociationProgramMeetingTime``).
    Generalizes — any state's reviewer rows that name a parent entity for
    a leaf the spine scatters onto a sibling sub-entity resolve here
    without per-state synonym entries.

    Discipline: only fires when the merged parent's sub-collection
    actually carries the leaf as a property AND the derived child
    record exists in the lookup. ``setdefault``-style miss cascade
    keeps the keymap's "fall through to no_mc_row honestly" posture
    on genuine source-doc gaps.
    """
    cleaned = _strip_trailing_paren(_strip_reviewer_prefix(element.strip())) or element.strip()
    segments = _dotted_segments(cleaned)
    if len(segments) != 1:
        return None
    parent_pair = spine.find_entity(entity.strip())
    if parent_pair is None:
        return None
    parent_name, parent_data = parent_pair
    leaf = segments[0]
    leaf_candidates = {c.lower() for c in reviewer_element_candidates(leaf)}

    # Local import keeps the module's existing top-level import surface
    # untouched — `unflatten._depluralize` is the same singular-PascalCase
    # helper the spine producer uses to derive sub-entity names, so the
    # matcher mirrors the producer convention exactly.
    from src.spine.unflatten import _depluralize

    for sub_name, sub_data in parent_data.get("sub_collections", {}).items():
        sub_props_lower = {p.lower() for p in sub_data.get("properties", {})}
        if not (sub_props_lower & leaf_candidates):
            continue
        derived_child = parent_name + _depluralize(sub_name)
        derived_norm = entity_match_form(derived_child)
        for cand in leaf_candidates:
            hit = lookup.get((derived_norm, cand))
            if hit is not None:
                return hit
    return None


def reviewer_key_to_mc_key(
    entity: str,
    element: str,
    lookup: dict[tuple[str, str], str],
    spine: SpineIndex | None = None,
) -> str | None:
    """Look up the first MC ``record_key`` matching a reviewer pair.

    Returns ``None`` when no candidate resolves — callers surface this
    as the ``no_mc_row`` comparison bucket.

    When ``spine`` is provided, three walker paths fire on direct-cascade
    miss (in order):

    1. **Dotted-FK walker** (``_resolve_via_fk_nav``, issue #61) — for
       ``staffSectionAssociation | section.schoolId`` style inputs. Walks
       each non-leaf segment through the reference graph, resolves the
       leaf on the FK target.
    2. **Single-segment leaf-search** (``_resolve_via_leaf_search``,
       issue #138 PR 2) — for flat leaves like ``programTypeDescriptor``
       on a parent that doesn't document it directly. Walks the parent's
       references whose key-properties share a candidate with the leaf,
       resolves on the documented target.
    3. **Sub-entity scatter** (``_resolve_via_subentity_scatter``, issue
       #162) — for flat leaves where the spine partitioned the leaf onto a
       sibling sub-collection / extension-contributed child entity (e.g.
       ``staffEducationOrganizationEmploymentAssociation | contractDays``
       lives on ``…AssociationContract``). Derives the child's record key
       via the ``{ParentEntity}{SingularSubCollection}`` convention.

    Direct candidates always win; walkers fire in declared order so a
    reviewer's explicit dotted path stays preferred over implicit leaf-
    search / scatter heuristics.
    """
    if not entity or not element:
        return None
    ent_n = entity_match_form(entity.strip())
    for cand in reviewer_element_candidates(element.strip()):
        hit = lookup.get((ent_n, cand))
        if hit is not None:
            return hit
    if spine is None:
        return None
    nav = _resolve_via_fk_nav(entity, element, lookup, spine)
    if nav is not None:
        return nav
    leaf_search = _resolve_via_leaf_search(entity, element, lookup, spine)
    if leaf_search is not None:
        return leaf_search
    return _resolve_via_subentity_scatter(entity, element, lookup, spine)
