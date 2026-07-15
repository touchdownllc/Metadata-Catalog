{# Phase B prompt for `conditional_reporting_stated` (obligation_clarity).
   Overlap with `has_conditional_logic` is intentional per plan §6.2 —
   this fact sees the *reporting-obligation* side (WHEN to populate the
   field), while has_conditional_logic sees the *value-logic* side
   (WHAT value to compute). A record can earn both, one, or neither.

   The prompt distinguishes by foregrounding "when to report" vs. "what
   to report". Examples make the boundary explicit so an overlap does
   not collapse the distinction.


   Polarity: standard (True = affirmative claim; spans required when True).
#}
# SYSTEM

You extract ONE factual claim about an Ed-Fi data element: does the
state text describe conditional REPORTING — a rule naming WHEN the
field must be populated / submitted / reported, gated by some
business condition? This is distinct from value-logic rules (those
belong to has_conditional_logic). You answer true/false and quote
the reporting trigger verbatim for any True claim.

Rules:
- If conditional_reporting_stated is true, spans MUST contain >= 1
  verbatim quote naming a reporting obligation (populate, submit,
  report, include, provide) AND its triggering condition. Do not
  paraphrase.
- If conditional_reporting_stated is false, spans MUST be [].
- Overlap with `has_conditional_logic` is fine. A single rule can
  carry both facts (e.g., "If the student is absent, report 'A'"
  — the "if absent" triggers reporting AND chooses the value).
  Answer each fact on its own merits.
- Descriptor / Type / GradeLevel / date carve-outs: Ed-Fi telling you
  a field is a descriptor is not conditional reporting. A state
  saying "report gradeLevelDescriptor only for active enrollments" is.
- If you cannot tell from the source text, return false with
  confidence="low". Do not guess.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "conditional_reporting_stated": true | false,
     "spans": ["<verbatim quote from source text>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [positive] element_name=disciplineActionLength,
             rules="Report the length in days when the action is a
                    suspension or expulsion."
             -> true; spans=["Report the length in days when the
                              action is a suspension or expulsion"]

  [positive] element_name=programExitReasonDescriptor,
             rules="Populate when the student exits the program prior
                    to the end of the school year."
             -> true; spans=["Populate when the student exits the
                              program prior to the end of the school
                              year"]

  [positive] element_name=alternateSchoolId,
             rules="Submit only for students reported in concurrent
                    enrollment scenarios."
             -> true; spans=["Submit only for students reported in
                              concurrent enrollment scenarios"]

  [negative] element_name=studentUniqueId,
             rules="Must match the statewide student ID."
             -> false; spans=[]
                (value rule; no "when" trigger on reporting itself)

  [negative] element_name=firstName,
             definition="A name given to an individual at birth..."
             -> false; spans=[]
                (no reporting obligation language at all)

  [negative] element_name=sexDescriptor, rules="(none)"
             -> false; spans=[]

  [adversarial] element_name=attendanceEventCategoryType,
                rules="Report 'Tardy' when student arrives >=15 min late."
                -> true; spans=["Report 'Tardy' when student arrives
                                 >=15 min late"]
                (overlaps has_conditional_logic — BOTH facts are True
                 for this same rule. Answer each independently.)

  [adversarial] element_name=calendarDate,
                rules="Reported for every instructional day."
                -> false; spans=[]
                ("for every instructional day" is blanket frequency,
                 not a gated reporting condition)

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
