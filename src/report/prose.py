"""Editorial prose data for the analyst workbooks (issue #213 item 1).

Hand-authored analyst-facing context — source-scope narratives, the
coverage-semantics note, and the Methodology Notes ("Known Limitations")
rows. Extracted from ``analyst.py`` verbatim: a ~330-line literal tuple
inside a 3,600-line module is how the issue-#211 splice defect happened
(an edit near the tuple boundary landed inside an adjacent function
unnoticed). Beside ``versions.py`` because both are editorial data, not
rendering logic.

``analyst`` re-exports the old private names (``_KNOWN_LIMITATIONS``,
``_SOURCE_SCOPE_BY_STATE``, ``_COVERAGE_SEMANTICS_NOTE``,
``_filter_known_limitations``) for its existing consumers/tests; new
code should import from here.
"""

from __future__ import annotations

# Source-scope narrative per state — hardcoded because it's editorial context,
# not derivable from the source documents. Analyst reviewer 1 (Appendix A1/A2)
# flagged coverage numbers like 92.2% as "source-doc" coverage, not full Ed-Fi
# UDM coverage, and surfaced the risk that analysts would mistake out-of-scope
# domains (AZ assessment, MN discipline) for ingester bugs.
# Coverage numerator semantics narrative — analyst-review round-3 P3 flagged
# that `Source-document coverage` and `Spine coverage` use different numerator
# concepts (matched source-document rows vs. unique matched spine keys) and
# that the distinction wasn't explicit on the Score Card. Divergences are
# deliberate — e.g. WI shows 470/533 source rows vs. 467 unique spine keys
# because several Confluence pages document the same Ed-Fi field from multiple
# reporting angles, so a few source rows dedup onto the same spine slot.
COVERAGE_SEMANTICS_NOTE: str = (
    "Source-document coverage counts matched source-document rows "
    "(numerator/denominator are BOTH row counts). "
    "Spine coverage counts unique matched spine keys against the full Ed-Fi "
    "UDM (numerator is distinct spine positions, denominator is total spine "
    "positions for the state). "
    "Source and spine numerators can differ when multiple source rows dedup "
    "to the same spine slot — not a reconciliation defect."
)

SOURCE_SCOPE_BY_STATE: dict[str, str] = {
    "AZ": (
        "Arizona SEA Use Case 12.0 only — excludes statewide assessment "
        "(AzMERIT/AASA), educator certification, and finance domains. "
        "Assessment-related elements (StudentAssessment*, AssessmentItem) "
        "are intentionally absent from this catalog because the source "
        "document does not enumerate them."
    ),
    "WI": (
        "Wisconsin DPI Confluence — covers Enrollment, SPED, Rosters, "
        "Discipline (LEA), Credentials, Finance (SAFR). Rows may be missing "
        "if the Confluence page had a non-standard table layout; per-page "
        "scrape notes live in data/raw/wi/confluence/."
    ),
    "MN": (
        "MDE Data Mapping Matrix 2026-27 only — excludes discipline/MARSS, "
        "career-tech, and finance reporting. Discipline-related elements "
        "(DisciplineAction, DisciplineIncident, StudentDiscipline*) are "
        "intentionally absent from this catalog — they live in a separate "
        "MDE handbook that's not currently ingested."
    ),
    "TX": (
        "TWEDS v33 (Texas Education Data Standards) — TEA's official K-12 "
        "SIS reporting standard — enumerated against the TSDS Vendor SDK "
        "2026.2.2 spine (Ed-Fi Data Model 4.0.0 core + 147 TEA `tx_` "
        "extension entities, TEA descriptors pre-populated). Covers "
        "student enrollment, demographics, program participation (SPED / "
        "Bilingual / ESL / Title I), attendance reporting, academic "
        "record, and assessment. Teacher-preparation (TPDM) is out of "
        "scope for TEA's K-12 reporting stream and does not appear in "
        "either the source document or the TSDS spine. Unresolved rows "
        "(~12% of source) are concentrated in empty-placeholder TEA "
        "extension schemas (SPED tier-of-intensity attendance, grievance, "
        "video-camera request, requisition — published with no spine "
        "properties) plus a small set of flat-property composites "
        "(`Name`, `BirthData`) where TEDS groups Ed-Fi leaf properties "
        "under a single TEDS element name."
    ),
    "IN": (
        "IDOE Vendor Documentation v6.1 (`API Datastructure` sheet) — "
        "Indiana DOE's vendor-cert per-element catalog enumerated against the "
        "Ed-Fi Data Standard 5.2.0 spine plus 38 IDOE 1.0.0 extensions "
        "(`idoe_*` prefix). Covers education-organization registry, courses, "
        "calendars, master schedule, staff, student demographics + enrollment "
        "+ attendance + transcript + discipline + program participation. "
        "Validation-rules narrative (~175 domain-keyed rules in the companion "
        "`idoe_validation_rules.xlsx`) is NOT yet ingested as element-level "
        "`business_rules_text` — deferred to a Phase F follow-up if the "
        "headline match-rate would benefit. Unresolved rows concentrate on "
        "abstract base classes the spine collapses into concrete subclasses "
        "(e.g. `edfi.EducationOrganization` vs spine `LocalEducationAgency` / "
        "`School`)."
    ),
}

# Known Limitations rows — hardcoded analyst-facing context. Each row has
# (Limitation, Impact, Why it exists, applies_to). Prevents analysts from
# re-flagging things already in the backlog; addresses feedback2 §Add a Known
# Limitations Sheet and feedback1 §5 cross-state domain comparability notes.
#
# `applies_to` is a tuple of state abbreviations (or `"all"` for shared rows).
# Round 2.2 introduces per-workbook filtering (reviewer Moffatt #13): AZ- and
# MN-specific limitations previously leaked into every workbook. The combined
# `coverage_analyst.xlsx` shows all rows; per-state workbooks show only rows
# whose `applies_to` includes `"all"` or their state abbreviation.
KNOWN_LIMITATIONS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "In-scope NACHOS rows carry methodology scores; out-of-scope blank by design",
        "`NACHOS score` (col 9), `Adjusted NACHOS Score` (col 10), and "
        "`Justification` (col 11) fill from the sidecar for in-scope "
        "rows (aggregation/concatenation, multi-entity calculation, "
        "unnecessary extension, or core-row conditional logic). "
        "Out-of-scope rows render blank by design — granular elements, "
        "pure descriptor lookups, and necessary extensions without "
        "logic are not what methodology scopes. `Complex Business "
        "Logic` (col 8) continues to surface the spine-lens complexity "
        "tier (a cost signal, distinct from the NACHOS methodology "
        "number).",
        "Phase F (methodology-conformant NACHOS) wires the methodology "
        "spec (`docs/NACHOS_Methodology_External review.xlsx`) into the "
        "rule tree: tier 0..3 from aggregation/concatenation/multi-"
        "entity/conditional facts, plus +1/+0.5/+0.5 extension and "
        "multi-entity adjustments capped at 4.5. In-scope/out-of-scope "
        "is a rules-only classifier over the same fact pool (plan §5).",
        ("all",),
    ),
    (
        "Legacy template flag columns derive from the sidecar (issues #102, #107)",
        "`Unnecessary Extension ?`, `Cross Entity Calculation ?`, "
        "`Complexity Signals`, and `Multiple Entities Involved` are "
        "scannable Yes/No flags computed from the same fact pool the "
        "rule cascade consumed. Col 11 (`Justification for Adjusted "
        "NACHOS Score`) carries the prose narrative for in-scope rows; "
        "these four surface the underlying facts without re-running the "
        "LLM. `Unnecessary Extension ?` reads `N/A` on non-extension "
        "rows; ``Yes`` when the score applies the `+1 unnecessary_ext` "
        "weight (LLM verdict ``extension_is_necessary == False``); "
        "``No`` for the `+0.5 necessary_ext` weight (verdict True); "
        "blank for the unresolved/`extension_necessity_unresolved` "
        "review-flagged path. `Complexity Signals` (renamed from "
        "`Reason for Complexity` in #107) joins True flags from "
        "`nachos_score.inputs_used` (e.g., `aggregation; conditional "
        "logic`) and is empty whenever `nachos_score.value == 0` — "
        "this prevents the natural-key-concatenation contradiction "
        "where a tier-0 row would otherwise show 'concatenation' in "
        "the column. `Multiple Entities Involved` is `Yes` at the "
        "`cross_entity_targets >= 2` threshold that drives the Phase F "
        "multi-entity +0.5 adjustment.",
        "These four columns were pre-blanked from the v10 methodology "
        "rectification onward as a placeholder mirroring Doug's hand-"
        "scoring template, on the assumption that cols 9-11 carry the "
        "same signal as prose. Reviewers consistently read the blanks "
        "as missing data rather than redundant data; issue #102 wired "
        "them up deterministically from the per-record sidecar (no new "
        "LLM calls, no schema change). Issue #107 corrected two "
        "source-of-truth mismatches: (a) the original `Unnecessary "
        "Extension ?` mapping read a stricter rule gate than the "
        "score's +1 weight uses, under-reporting `Yes` by 518 rows "
        "across the four states; (b) the `Reason for Complexity` "
        "column listed factually-True flags even when the rule's tier "
        "was 0 (natural-key-concat path), creating contradictions "
        "with the score. The column was renamed `Complexity Signals` "
        "and gated on `nachos_score.value > 0`.",
        ("all",),
    ),
    (
        "Some rows marked `Unresolved` in `Match Status`",
        "Rows couldn't confidently match the spine",
        "Source-doc names don't always align with the deployed Ed-Fi spine; "
        "these rows are still shown so analysts can judge; "
        "see docs/archive/analyst-review-feedback*.md for patterns",
        ("all",),
    ),
    (
        "Blank `Data Type` can appear on Unresolved rows",
        "Matched rows always carry a spine-derived canonical type (Descriptor, "
        "Reference, or primitive). Unresolved rows may still show a source-"
        "inferred type when one was supplied by the source document (AZ/WI "
        "typically retain them); MN unresolved rows are blank because the MDE "
        "matrix has no source-type column.",
        "Canonical types are spine-derived (Round 2.2 contract): when a row "
        "matches the spine it takes the spine's type; when it doesn't, the "
        "source-verbatim type is preserved as an audit trail if the source "
        "supplied one, otherwise the cell is left blank rather than guessed. "
        "Compare `Match Status` to interpret blanks.",
        ("all",),
    ),
    (
        "`Source Area` column holds state-specific grouping vocabulary",
        "Not directly comparable across states",
        "AZ uses sheet names, WI uses Confluence domain names, MN uses MDE "
        "collection names; `Ed-Fi Domain` is the cross-state-normalized column",
        ("all",),
    ),
    (
        "Some matched rows have blank `Ed-Fi Domain`",
        "Three categories account for these blanks: (1) state-specific "
        "extensions (AZ Part C: `PartCAZEIP` / `PartCNotification` / `PartC"
        "Transition`; MN program-association extensions: `Student21stCentury"
        "LearningCenterGrantProgramAssociation` etc.); (2) core Ed-Fi entities "
        "shipped without a domain tag (`Parent`, `EducationOrganizationCategory`); "
        "(3) sub-collection / cross-domain entities whose parent mapping isn't "
        "in the Data Standard's `x-Ed-Fi-domains` index. Expected counts: AZ "
        "~22 rows, WI 0, MN ~24 rows.",
        "Ed-Fi `x-Ed-Fi-domains` tags are published only for core entities. "
        "State-specific extensions and a small set of core entities ship "
        "without a domain tag. We document these as blank rather than "
        "inventing domain assignments — inventing would obscure the Data "
        "Standard contract. When the Data Standard adds a domain tag for one "
        "of these entities upstream, the blank resolves on the next spine "
        "rebuild without code changes.",
        ("all",),
    ),
    (
        "`Business Logic (Formula)` text may be page-level not element-level",
        "WI's 425 rows carry duplicated page-level rules",
        "Confluence authors write rules at the page level; per-element split "
        "is scoring-phase work (see T3.5)",
        ("all",),
    ),
    (
        "Regulatory citations empty across all three states",
        "`References` column carries source-doc citations but no statute references",
        "AZ's integrity-rules PDFs use `A.R.S. §15-xxx` tokens we don't currently "
        "extract; WI/MN sources don't embed statute refs",
        ("all",),
    ),
    (
        "Source-scope limitations",
        "Catalog is 3.9%–6.8% of the full Ed-Fi UDM per state",
        "Each state's enrichment source covers a specific SEA workflow, not "
        "the full data standard; see Score Card's `Source scope` line",
        ("all",),
    ),
    (
        "Spine-lens: out-of-scope domains collapsed to placeholders "
        "(Assessment now enabled per-state, v27)",
        "Survey, LearningStandard, Gradebook, and Intervention entities "
        "appear only as a single `(filtered)` placeholder row each in the "
        "spine-lens artifact (Match Status: `Filtered (SIS never "
        "populates)`). Assessment and AssessmentRegistration collapse the "
        "same way EXCEPT where the state has them enabled in the "
        "`ingest.domain_scope` registry (v27, ADR 0007): TX and IN surface "
        "real Assessment coverage rows, and IN surfaces AssessmentRegistration "
        "— no placeholder in those (state, domain) artifacts. Source-lens "
        "artifacts are untouched — rows the state documented in these domains "
        "remain as an audit trail.",
        "SIS vendors do not populate the always-filtered surfaces "
        "(Survey / LearningStandard / Gradebook / Intervention) in real "
        "deployments; scoring per-element coverage for them would be noise. "
        "Assessment + AssessmentRegistration were filtered on the same "
        "premise but are now lifted per-state where the team has source "
        "documentation (the v27 scope broadening — ADR 0007 — reverses the "
        "SIS-never-populated call for those (state, domain) pairs). The "
        "spine-lens applies the filter via `src/ingest/domain_filter.py`, "
        "gated on `ingest.domain_scope` (domain-map subset rule + explicit "
        "concept-anchor list for mixed-domain entities like LearningStandard "
        "and GradebookEntry + entity-name prefix fallback).",
        ("all",),
    ),
    (
        "AZ-specific: 70XXX FRPL integrity rules not ingested",
        "AZ `Business Logic (Formula)` missing the `70XXX` (FRPL / Income "
        "Eligibility) rule series",
        "ADE publishes 70XXX rules only as inline text on a blog page "
        "(`/finance/new-frpl-reports-and-integrity-rules`), not as a PDF; "
        "10XXX/20XXX/30XXX/40XXX/50XXX/52XXX/57XXX/60XXX/80XXX/90XXX rule "
        "series ARE ingested from their published PDFs",
        ("AZ",),
    ),
    (
        "MN-specific: FK-reference path element names preserved",
        "A small set of MN rows (~8) retain reference-path element names like "
        "`StudentReference>StudentUniqueId > First Name` or `School.SchoolId`",
        "The MDE mapping matrix uses Ed-Fi JSON reference-path convention "
        "(`<ReferenceEntity>.<keyElement>` or `<Ref>>tail > subtail`) for "
        "some FK navigation rows. Spine matching already resolves these via "
        "path-tail aliasing (their Match Status is populated correctly). "
        "The path form is preserved as provenance; they resolve to the same "
        "canonical spine elements.",
        ("MN",),
    ),
    (
        "MN-specific source typo: `memsberships` preserved",
        "4 rows on `Student` entity display element names starting with "
        "`memsberships.` (should be `memberships`)",
        "The MDE 2026-27 Data Mapping Matrix spells it `memsberships` at "
        "source; we don't silently fuzzy-correct source typos — it would "
        "mask authoring issues ADE/MDE need to fix upstream",
        ("MN",),
    ),
    (
        "TX-specific: TPDM teacher-preparation reporting not in scope",
        "No `tpdm_*` extension rows appear in the TX workbook",
        "TWEDS v33 documents the TEA K-12 SIS reporting surface only, and "
        "the TSDS Vendor SDK 2026.2.2 spine ships TEA extensions without "
        "TPDM bundled — teacher-prep is a separate Ed-Fi extension family "
        "and a separate TEA reporting stream. AZ/WI/MN source docs also "
        "exclude teacher-prep, so this matches the cross-state convention. "
        "Revisit only if stakeholders request teacher-prep scoring visibility.",
        ("TX",),
    ),
    (
        "TX-specific: empty TEA extension schemas surface as Unresolved",
        "~49 rows on `GrievanceExt`, `SPEDVideoCameraRequestExt`, "
        "`RequisitionExt`, `OpenStaffPosition`, "
        "`SpecialEducationTierOfIntensityAttendance`, and "
        "`FlexibleSpecialEducationTierOfIntensityAttendance` render with "
        "`Match Status = Unresolved`",
        "These TEA extension schemas ship in the TSDS Vendor SDK 2026.2.2 "
        "spine with zero flat properties and zero references — they're "
        "placeholder extension shells where TEA has declared the reporting "
        "surface in TEDS but has not yet wired the matching properties "
        "into the Ed-Fi extension. The rows are shown (not filtered) so "
        "analysts see the full TEDS reporting surface; source-verbatim "
        "values are preserved as audit trail. Will resolve automatically "
        "when TEA publishes a spine bundle that fills in these extensions.",
        ("TX",),
    ),
    (
        "TX-specific: TEDS composite element names (`Name`, `BirthData`)",
        "~10 rows on `Parent`/`Staff`/`Student`/`StudentApplication` carry "
        "TEDS-grouping element names (`Name`, `BirthData`, `AttendanceEvent`) "
        "rather than the individual Ed-Fi leaf properties",
        "TEDS groups Ed-Fi leaf properties (firstName / middleName / "
        "lastSurname; birthDate / birthCity / birthCountryDescriptor) under "
        "a single reporting-style element name. Flattening TEDS's composite "
        "name into multiple Ed-Fi leaves would break POC-3's `one source "
        "row → one output row` contract that underpins cross-state "
        "comparability. Kept as Unresolved intentionally; the raw TEDS "
        "grouping is preserved in `Data Element` as an audit trail.",
        ("TX",),
    ),
    (
        "TX-specific: TEDS-only SPED reporting nuances on "
        "`StudentSpecialEducationProgramAssociation`",
        "~21 rows on SPED-program associations carry TEDS-specific "
        "begin/end-date bookkeeping (`DisabilitySetBeginDate`, "
        "`EducationalEnvironmentBeginDate`, `TierOfIntensityBeginDate`) and "
        "TEA-specific reporting sets that have no counterpart in the core "
        "Ed-Fi SPED spine",
        "TEA reports SPED program participation with finer temporal "
        "granularity than the core Ed-Fi SPED spine carries. The TSDS "
        "Vendor SDK 2026.2.2 extension bundle doesn't yet surface these "
        "begin/end-date fields as spine properties. Source-verbatim values "
        "are preserved as audit trail; resolves when TEA adds these "
        "properties to the TSDS Vendor SDK spine.",
        ("TX",),
    ),
    (
        "TX-specific: TEDS pinned to v33 (2026-27 reporting year)",
        "TEDS version appears in `source_document` as `TWEDS v33 (TEDS)`. "
        "Future TEDS releases (v34+) will require a re-scrape and a new "
        "ingest.",
        "TWEDS v33 is the current reporting-year standard as of 2026-04. "
        "Upgrades are mechanical — bump `TWEDS_VERSION` in "
        "`src/ingest/tx_tweds.py`, clear the cache, re-run `mc "
        "ingest tx`.",
        ("TX",),
    ),
    # Restored 2026-07 (issue #211 item 1a): this row was accidentally
    # spliced into `_elements_headers` during the issue-#174 rename
    # commit, silently dropping it from every generated Methodology
    # Notes sheet. CLAUDE.md's column contract names it explicitly;
    # `tests/test_report_analyst.py` now pins its presence.
    (
        "Stakeholder terminology renamed 2026-07 (issue #174)",
        "Artifacts generated before 2026-07 say 'Structural Depth' "
        "(now 'Implementation Shape'), 'Integration Profile' (now "
        "'NACHOS Score Context'), and 'spine' (now 'Ed-Fi Swagger/API "
        "model'); JSON sidecar fields, artifact filenames, and the "
        "`--lens spine` CLI value keep the internal names.",
        "Stakeholder-facing rename only — renaming join keys, file "
        "names, and code identifiers would churn the operator chain "
        "and Drive history with no consumer benefit. The Legend's "
        "'Terminology — former names' table carries the full mapping.",
        ("all",),
    ),
)


def filter_known_limitations(
    state: str | None,
) -> tuple[tuple[str, str, str], ...]:
    """Return KL rows applicable to a workbook.

    - When `state` is one of AZ/WI/MN, emit rows whose `applies_to` includes
      that state OR `"all"`. Analyst round-2.2 fix: per-state workbooks no
      longer leak state-specific rows (AZ 70XXX → don't show on WI/MN;
      MN memsberships → don't show on AZ/WI).
    - When `state` is None (combined workbook), emit all rows — the combined
      workbook is cross-state by design, so every state-specific row is in
      scope.
    """
    out: list[tuple[str, str, str]] = []
    for lim, impact, why, applies in KNOWN_LIMITATIONS:
        if state is None or "all" in applies or state.upper() in applies:
            out.append((lim, impact, why))
    return tuple(out)

