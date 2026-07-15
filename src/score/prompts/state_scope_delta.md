{# Phase C2 source-lens prompt for `state_scope_delta` (semantic_fidelity
   dimension, tier 2 trigger). Merged from `state_narrows_edfi_scope` +
   `state_broadens_edfi_scope` (PR A of the simplification side quest,
   2026-04-28). One question, one comparison: how does the state's scope
   relate to Ed-Fi's baseline?

   - "narrows":  state restricts Ed-Fi's scope (fewer populations, events,
                 time windows, or legal bases). WI "reported only for
                 students in special education settings" / TX TEDS
                 snapshot-window narrowing are canonical shapes.
   - "broadens": state expands Ed-Fi's scope (more populations, events,
                 subtypes, legal bases; union-with-additional-categories).
                 WI "includes BIA-recognized AND state-recognized tribes"
                 is the canonical shape; TX TEDS occasionally folds a
                 subtype into the parent ("reported for both regular and
                 special education").
   - "neutral":  no scope delta. State text is aligned, paraphrased, or
                 adds detail/format rules without restricting or
                 expanding WHO/WHAT is reported. MN Mapping-Matrix rows
                 mostly land here (1:1 mappings).

   Authoring anchors (carried forward from the two predecessor prompts,
   2026-04-25): a format/concatenation rule without a scope shift is
   "neutral" (detail, owned by `definition_adds_detail_beyond_edfi`).
   A tight description that restates Ed-Fi verbatim is "neutral"
   (alignment, owned by `semantic_class`). Narrowing and broadening are
   mutually exclusive — a row that does both at once should pick the
   stronger signal and call out the second clause in spans; if truly
   ambiguous, return "neutral" with confidence="low".

   Polarity: n/a (enum3 with "neutral" as the no-evidence default; spans required for "narrows" and "broadens", spans MUST be [] for "neutral").
#}
# SYSTEM

You extract ONE factual claim about scope delta: does the state's
definition NARROW, BROADEN, or leave NEUTRAL the scope of the Ed-Fi
standard definition for this element? You answer with one of three
labels and quote the relevant clause verbatim from the state text for
narrows/broadens claims.

Rules:
- Output value is one of "narrows" | "broadens" | "neutral" (lowercase,
  exact spelling).
- If state_scope_delta is "narrows", spans MUST contain >= 1 quote
  drawn verbatim from definition_text / business_rules_text /
  element_specific_rules showing the restriction clause. Typical
  shapes: "reported only for X", "excludes Y", "limited to Z",
  "restricted to the fall window", "does not include W".
- If state_scope_delta is "broadens", spans MUST contain >= 1 quote
  showing the expansion clause. Typical shapes: "includes X and Y",
  "also covers Z", "expanded to include", "plus W", "in addition to
  the Ed-Fi values".
- If state_scope_delta is "neutral", spans MUST be [].
- A scope delta REQUIRES Ed-Fi to describe a baseline scope the state
  modifies. If edfi_standard_definition is "(none)" or itself silent on
  scope, the state has nothing to narrow or broaden — return "neutral".
- Adding a format / concatenation rule, identifier shape, or
  submission detail without restricting or expanding WHO or WHAT is
  reported is NOT a scope delta — return "neutral" (that's detail,
  owned by `definition_adds_detail_beyond_edfi`).
- Tight description that restates Ed-Fi verbatim is alignment, not a
  scope delta — return "neutral" (alignment is owned by
  `semantic_class`).
- Narrowing and broadening are mutually exclusive in the answer slot.
  If a row genuinely shows both (e.g., "covers community-based
  partners but only during the fall window"), pick the dominant
  signal and quote both clauses in spans.
- If you cannot tell from the source text, return "neutral" with
  confidence="low". Do not guess narrows/broadens.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "state_scope_delta": "narrows" | "broadens" | "neutral",
     "spans": ["<verbatim quote of the modifying clause>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [narrows] element_name=gradeLevelDescriptor,
            edfi_standard_definition="The grade level for which a
                student enrolls.",
            definition_text="The grade level for which a student is
                enrolled. Report only for students active on October
                1 of the reporting year.",
            -> "narrows"; spans=["Report only for students active on
                                  October 1 of the reporting year."]

  [narrows] element_name=homelessServiceProvided,
            edfi_standard_definition="An indication of the type of
                homeless services provided to the student.",
            definition_text="An indication of the type of homeless
                services provided. Excludes summer enrichment and
                after-school tutoring.",
            -> "narrows"; spans=["Excludes summer enrichment and
                                  after-school tutoring."]

  [broadens] element_name=tribalAffiliation,
             edfi_standard_definition="The tribe with which the
                 student is affiliated.",
             definition_text="The tribe with which the student is
                 affiliated. Includes BIA-recognized and state-
                 recognized tribes listed in Wis. Stat. 14.37.",
             -> "broadens"; spans=["Includes BIA-recognized and state-
                                    recognized tribes"]

  [broadens] element_name=programTypeDescriptor,
             edfi_standard_definition="The type of program the
                 student is enrolled in.",
             definition_text="The type of program. Also covers
                 early-childhood services provided through
                 community-based partners outside the LEA.",
             -> "broadens"; spans=["Also covers early-childhood
                                    services provided through
                                    community-based partners
                                    outside the LEA."]

  [neutral] element_name=firstName,
            edfi_standard_definition="A name given to an individual
                at birth, baptism, or naming ceremony.",
            definition_text="First name of the student.",
            -> "neutral"; spans=[]
               (thinner wording, but no scope shift)

  [neutral] element_name=calendarCode,
            edfi_standard_definition="The identifier for the calendar.",
            definition_text="The identifier for the Calendar. Submit
                LEAID-SchoolId-Calendartypecodevalue-Sequence.",
            -> "neutral"; spans=[]
               (format/identifier rule, not a scope delta)

  [neutral] element_name=anonymousFlag,
            edfi_standard_definition="(none)",
            definition_text="Y if the student response is anonymized
                for this submission.",
            -> "neutral"; spans=[]
               (no Ed-Fi baseline to compare against)

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
