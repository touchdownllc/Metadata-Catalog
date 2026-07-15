{# Phase C2 source-lens prompt for `extension_is_necessary`
   (extension_justification dimension). Only evaluated when
   source=extension. True iff the extension carries state-specific
   content Ed-Fi's core model doesn't already express — a genuine
   reporting need, not a cosmetic rename or a duplicate.

   Authoring anchors (2026-04-25):
   - AZ az.CalendarExtension.BeginDate / EndDate / TotalInstructional-
     Days — Ed-Fi's calendar model has analogous concepts in
     CalendarDate, but the state's intent here (track-level metadata,
     LEA-reported) isn't a plain duplicate. Usually necessary.
   - WI extensions carrying regulatory citations (Wis. Stat. §) are
     almost always necessary.
   - MN mn_* extensions on Student*ProgramAssociation capture
     MDE-specific attendance/membership metrics with no Ed-Fi
     counterpart — necessary.
   - TX TEDS extensions that simply re-declare core fields
     (tx_Student.StudentUniqueId) are NOT necessary — cosmetic
     duplicates. Return False.

   POC-2 trap: LLMs over-generously rate extensions as necessary
   because "state authored it so it must be needed." Mitigation: the
   answer must be grounded in a SPAN showing the state-specific
   reporting need, not in a default assumption.

   Issue #85 tightening (2026-04-29):
   - MN's source convention writes "MDE mapping: <table>.<field>" as the
     element definition. That is a data-source routing pointer, not
     evidence of state-specific need. When that is the ONLY state
     narrative on a bare-FK or core-mirror element, return False
     (confidence=medium). Exception: pair the pointer with a state-
     namespaced descriptor enum carrying state-unique values
     (Extension.X / MN.X), and necessity holds.
   - Spans must be INDEPENDENT evidence — a regulatory anchor, a state-
     specific population/window, a state-namespaced enum value, or a
     state-unique calculation rule. Restating the definition itself is
     not independent evidence; the def is the claim, not the proof.

   Polarity: standard (True = affirmative claim; spans required when True).
#}
# SYSTEM

You extract ONE factual claim about an Ed-Fi state EXTENSION element:
is the extension necessary — does the state carry a specific
reporting need that the Ed-Fi core model does NOT already cover? You
answer true/false and quote the state's justification verbatim for
any True claim. Non-extension rows are skipped upstream; every row in
this batch has source=extension.

Rules:
- If extension_is_necessary is true, spans MUST contain >= 1 quote
  drawn verbatim from definition_text / business_rules_text /
  element_specific_rules showing the state-specific need. Typical
  signals: regulatory citation, state-specific population / reporting
  window, state-specific measurement, field the core Ed-Fi model
  cannot express even as a flattened reference.
- If extension_is_necessary is false, spans MUST be [].
- A cosmetic rename of a core field (extension declares the same
  concept with a different casing / synonym) is NOT necessary — the
  core field already covers it.
- A duplicate of a core field under the same name (boilerplate
  propagation) is NOT necessary.
- **State-wrapper temporal fields** (BeginDate, EndDate,
  ServiceBeginDate, ServiceEndDate, EntryDate, ExitWithdrawDate,
  ParentalPermissionSetEndDate, and similar date markers) on a
  state-extension entity are NOT necessary UNLESS the state text
  explicitly cites a state-specific reporting window, regulatory
  anchor, or population filter that the core entity's date field
  cannot carry. Re-describing the field in state-flavored prose
  ("the first instructional day a student is assigned to the
  CTEProgramSvc descriptor") is NOT evidence of necessity — that's
  the cosmetic-duplicate pattern. The core program-association
  entity already carries Begin/End date semantics.
- State regulatory citations (statute/code references not in Ed-Fi)
  make the extension necessary ONLY when they appear verbatim in the
  state text.
- If the state text is "(none)" OR a bare model-label ("Identity
  Column", "Reference"), return false with confidence="low" — the
  LLM cannot verify a reporting need from metadata alone.
- **MN data-source-routing pointer pattern.** When the entire
  definition_text is `MDE mapping: <state-table>.<state-field>` (or
  the same shape with `; MDE entity: ...` / `; Via reference: ...` /
  `; role: ...` clauses) and there is no element-specific rule
  carrying a regulatory anchor or population filter, treat the
  pointer as data-source routing — not as evidence of state-specific
  need. Return false with confidence="medium". Exception: the
  pointer is followed by `Enumeration: Extension.<Name>Descriptor`,
  `Enumeration: MN.<Name>Descriptor`, or a fixed state-unique value
  (`= 'MNC'`, `= "EE-ECFE"`). In that case the descriptor is
  state-namespaced and the element stays true (confidence=medium)
  because the state-specific content lives in the enumeration.
- **Spans must be independent evidence.** A span counts only when it
  cites a regulatory/statute anchor, a state-specific population or
  reporting window, a state-namespaced enum value, or a
  state-unique calculation/edit rule. **Restating the definition
  itself does NOT count** — the definition is the claim being
  evaluated, not the proof. If the only candidate span is a
  paraphrase of the state def with no independent evidence, prefer
  false (confidence=medium) or true with confidence=medium and a
  span that quotes the independent anchor instead.
- **Confidence calibration.**
  - high — span quotes a regulatory citation, a state-specific
    population/reporting window, a state-namespaced descriptor with
    state-unique values, or a state-unique edit rule verbatim.
  - medium — state-flavored prose names a state program / agency /
    institution but no regulatory anchor or state-population filter
    is cited.
  - low — narrative is empty, a bare model-label, or a pure
    data-source-routing pointer with no state-specific signal.
- Do NOT reason "state bothered to extend, so it must be necessary."
  Require evidence.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "extension_is_necessary": true | false,
     "spans": ["<verbatim state span showing the reporting need>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [positive] element_name=TotalInstructionalDays,
             source=extension,
             extension_name=az.CalendarExtension,
             definition_text="The total number of instructional days
                 in the school year, reported per ARS §15-901.",
             -> true; spans=["reported per ARS §15-901."]
                (state-specific regulatory anchor)

  [positive] element_name=attendance,
             source=extension,
             extension_name=mn_studentEarlyEducationProgramAssociation,
             definition_text="The total number of days the child
                 attended during the reporting period. Used by MDE to
                 compute average daily attendance under Minn. Stat.
                 124D.162.",
             -> true; spans=["Used by MDE to compute average daily
                              attendance under Minn. Stat. 124D.162."]

  [negative] element_name=StudentUniqueId,
             source=extension,
             extension_name=tx_Student,
             definition_text="The unique identifier assigned to the
                 student by the state.",
             edfi_standard_definition="The unique identifier assigned
                 to the student by the state or assigning
                 organization.",
             -> false; spans=[]
                (duplicate of core StudentUniqueId — cosmetic rename)

  [negative] element_name=BeginDate,
             source=extension,
             extension_name=az.CalendarExtension,
             definition_text="The first date the track is valid.",
             edfi_standard_definition="The first date of the track.",
             -> false; spans=[]
                (paraphrase of core concept — not a distinct need)

  [negative] element_name=SchoolID,
             source=extension,
             extension_name=az.StudentSchoolAssociationExtension,
             definition_text="Identity Column",
             -> false; spans=[]
                (model-label tag only — no evidence of need)

  [negative — STATE-WRAPPER SERVICE DATE]
             element_name=ServiceBeginDate,
             source=extension,
             extension_name=tx_StudentCTEProgramAssociation,
             definition_text="CTEProgSvc ServiceBeginDate (E3055)
                 is the first instructional day in the current
                 school year a student is assigned to the
                 CTEProgramSvc descriptor.",
             edfi_standard_definition (core program-association
                 beginDate): "The earliest date the student is
                 associated with the education organization through
                 this program.",
             -> false; spans=[]
                (TX wraps a core program-association date field; the
                reporting need is already expressed by the core entity's
                beginDate. Re-describing the field in state-flavored
                prose is not evidence of state-specific necessity. No
                regulatory citation, no state-unique window cited.)

  [negative — PARENTAL-PERMISSION DATE WRAPPER]
             element_name=ParentalPermissionSetEndDate,
             source=extension,
             extension_name=tx_StudentLanguageInstructionProgramAssociation,
             definition_text="ParentalPermissionSetEndDate (E3043)
                 is the first day after the last instructional day
                 a student was assigned to the ParentalPermission
                 descriptor.",
             element_specific_rules="(none)",
             -> false; spans=[]
                (no regulatory citation, no state-specific reporting
                window distinct from the standard EndDate; cosmetic
                state-wrapper of a date field already carried by the
                core program-association entity)

  [negative — MN MDE-MAPPING POINTER, NO STATE-NAMESPACED ENUM]
             element_name=PercentEnrolled,
             source=extension,
             extension_name=studentSchoolAssociationExtensions,
             definition_text="MDE mapping: Student Enrollment.percentEnrolled",
             element_specific_rules="(none)",
             edfi_standard_definition="Percent Enrolled",
             -> false; spans=[]
                (pure data-source routing pointer — tells MDE staff
                which MDE-source field feeds this Ed-Fi element.
                No regulatory anchor, no state-specific population,
                no state-namespaced enum. Concept is core. Return
                false with confidence=medium.)

  [negative — MN MDE-MAPPING POINTER ON BARE FK]
             element_name=LocalEducationAgencyId,
             source=extension,
             extension_name=mn_studentSchoolAssociationExtension,
             definition_text="MDE mapping: Student Enrollment.transportingDistrictNumber",
             element_specific_rules="(none)",
             edfi_standard_definition="The identifier assigned to a local education agency.",
             -> false; spans=[]
                (bare FK reference whose only narrative is a
                data-source routing pointer; the FK target — LEA —
                is core. The state's choice to source it from
                `transportingDistrictNumber` is a routing detail,
                not an element-level reporting need. Return false
                with confidence=medium.)

  [positive — MN MDE-MAPPING + STATE-NAMESPACED ENUM]
             element_name=OptOutIndicatorsDescriptor,
             source=extension,
             extension_name=mn_studentEducationOrganizationAssociationExtension,
             definition_text="MDE mapping: Student Demographic.optOutIndicator_MNC; Enumeration: optOutIndicatorsDescriptor = 'MNC'",
             element_specific_rules="(none)",
             -> true;
                spans=["Enumeration: optOutIndicatorsDescriptor = 'MNC'"]
                (state-namespaced descriptor pinned to a fixed
                MN-unique value; the state-specific content is in
                the enumeration choice, even though the element
                slot mirrors a core-shape descriptor. Confidence
                medium because no regulatory anchor is cited.)

  [negative — SELF-REFERENTIAL SPAN (state-flavor paraphrase)]
             element_name=ASDBEligibilityDate,
             source=extension,
             extension_name=az.PartCAZEIP,
             definition_text="Date student became eligible for ASDB",
             element_specific_rules="(none)",
             edfi_standard_definition="The date the student is identified as eligible for Arizona State School for the Deaf and Blind.",
             -> false; spans=[]
                (the only candidate span — "Date student became
                eligible for ASDB" — would just restate the def
                itself. Self-referential paraphrase is not
                independent evidence. The state-specific institution
                name (ASDB) names a state program, but no regulatory
                anchor, reporting window, or population filter is
                cited at the element level. If business_rules_text
                or element_specific_rules add an independent state
                anchor, return true with that anchor as the span.)

# USER

<entity_context>
  entity: {entity}
  domain: {domain}
  state: {state}
  business_rules_text (entity-level): {entity_business_rules}
</entity_context>

<elements>
{elements_block}
</elements>
