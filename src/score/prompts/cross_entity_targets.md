{# Phase B prompt for `cross_entity_targets` — the ONLY count-int fact
   in Phase B. Returns an integer count of distinct Ed-Fi entities
   referenced in the state text's business logic. Each distinct
   target entity gets one verbatim span.

   This fact is downstream of `has_cross_entity_logic`. If
   has_cross_entity_logic is False, cross_entity_targets is 0.
   If True, cross_entity_targets is the count of DISTINCT target
   entities — one per name, no duplicates.

   Validator (see extract.py::_process_payload):
   - If the integer claim does not equal the number of validated
     spans, the row downgrades to validated_value=null with
     downgrade_reason="count_span_mismatch".
   - If the claim is 0 and any span is emitted, it downgrades to
     null with downgrade_reason="spans_on_zero_count".


   Polarity: n/a (count-int — spans required per target, must number `value`).
#}
# SYSTEM

You extract ONE factual claim about an Ed-Fi data element: how many
DISTINCT other Ed-Fi entities does the state text reference in
business logic involving this element? Return 0 when there is no
cross-entity reference. Return N when N distinct target entities
appear, emitting ONE verbatim span per target.

Rules:
- The integer MUST equal the number of distinct target entities.
- spans MUST contain exactly as many entries as the integer; each
  span MUST be a verbatim quote mentioning one of the targets.
  Duplicate references to the same target entity collapse to a
  single span.
- Targets are Ed-Fi entities that are NOT the element's own host
  entity. Ed-Fi descriptor tables (CalendarTypeDescriptor,
  GradeLevelDescriptor) are VALUE SOURCES, not entities — do NOT
  count them.
- Confidence guidance (calibrate, do not default to "low"):
  * Text is silent or contains only a plain definition with no
    business rules → return 0 spans=[] with confidence="high"
    (no evidence means the count is certainly zero).
  * Text explicitly references only the host entity, descriptor
    tables, or non-entity concepts → return 0 spans=[] with
    confidence="high".
  * Text is AMBIGUOUS about whether a named concept is an Ed-Fi
    entity or just a common noun → return 0 spans=[] with
    confidence="low". Do not guess upward.
  * Text clearly names N distinct other entities → return N with
    one span per target at confidence="high". Drop to "medium"
    only if the verbatim span barely covers the naming.
- Avoid double-counting. Three mentions of "Student" across one
  rule are still ONE target.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "cross_entity_targets": <non-negative integer>,
     "spans": ["<verbatim quote naming one distinct target>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [positive count=1] element_name=calendarCode (host: CalendarDate),
                     rules="Must match a Calendar reported for the
                            same LEA and fiscal year."
                     -> 1; spans=["Must match a Calendar reported for
                                   the same LEA and fiscal year"]

  [positive count=2] element_name=sectionIdentifier (host: Attendance),
                     rules="Must match a Section and a Student record
                            both submitted for the same year."
                     -> 2; spans=["match a Section",
                                  "a Student record both submitted for
                                   the same year"]
                     (Section + Student = two distinct targets)

  [positive count=3] element_name=scheduleKey (host: StudentSchedule),
                     rules="Matches a Section, a Student, and a
                            Course record active for the year."
                     -> 3; spans=["Matches a Section",
                                  "a Student",
                                  "a Course record active for the
                                   year"]

  [zero] element_name=firstName (host: Student),
         definition="A name given to an individual at birth."
         -> 0; spans=[]

  [zero] element_name=studentUniqueId (host: Student),
         rules="Must match the statewide Student ID."
         -> 0; spans=[]
         (host = Student; the rule refers to the host itself)

  [zero] element_name=gradeLevelDescriptor (host: Enrollment),
         rules="Pick from the K-12 grade code set."
         -> 0; spans=[]
         (descriptor table, not an entity)

  [adversarial] element_name=studentIdentifier (host: Program),
                rules="Must match the Student record for the same
                       Student reported in the same fiscal year."
                -> 1; spans=["Must match the Student record"]
                (two mentions of Student collapse to one target)

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
