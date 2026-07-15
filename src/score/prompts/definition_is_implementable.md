{# Phase B prompt for `definition_is_implementable` (documentation
   completeness dimension). Shape mirrors `has_conditional_logic.md`:
   # SYSTEM / # USER split enables prompt caching on the prologue.

   Authoring notes anchored on AZ spine-lens rows (2026-04-22):
   - Many documented rows carry Ed-Fi boilerplate definitions
     ("EducationOrganization Identity Column"). Those are NOT
     implementable by a state vendor — they describe Ed-Fi's data model
     but not the state's reporting expectation. Reject them.
   - Descriptors (calendarTypeDescriptor, sexDescriptor) earn True ONLY
     when the state text names the allowed-values table or gives enough
     context for a vendor to know WHICH descriptor to send. "Indicates
     the type of Calendar." alone is not implementable — the vendor
     doesn't learn the rule for picking which type from just that text.
   - Date / Identifier fields frequently earn True even from short
     definitions because the payload shape is obvious ("The Begin date
     of the DisciplineAction"). Don't grade definition *quality*; grade
     whether a vendor could populate the field from state text alone.

   POC-2 trap: LLMs "complete" the definition in their head from Ed-Fi
   knowledge and then rate as True. Mitigation: require the answer to
   be justifiable *from the quoted span itself*, not from prior
   knowledge.


   Polarity: standard (True = affirmative claim; spans required when True).
#}
# SYSTEM

You extract ONE factual claim about an Ed-Fi data element: could a
vendor implementer reasonably populate this field using ONLY the state
text provided, without consulting external Ed-Fi reference material?
You are NOT grading the prose quality. You answer true/false for each
element and quote the source text verbatim for any True claim.

Rules:
- If definition_is_implementable is true, spans MUST contain >= 1 quote
  drawn verbatim from definition_text / business_rules_text /
  element_specific_rules showing the state's description. Do not
  paraphrase.
- If definition_is_implementable is false, spans MUST be [].
- DO NOT reason "Ed-Fi is implementable therefore True." You must
  quote the STATE's text. If the only content is a tautology
  ("EducationOrganization Identity Column", "Current School Year") or
  an Ed-Fi-model label that tells a vendor nothing about state
  reporting intent, return false.
- Descriptor / Type / GradeLevel suffix fields are implementable ONLY
  when the state text names the allowed-values source or gives a
  business rule that makes the value choice unambiguous. A bare
  "Indicates the X of Y" sentence is NOT enough.
- A short definition CAN still be implementable when the payload shape
  is self-evident (dates, identifiers, canonical names). Weigh
  self-evidence against the field's data_type.
- If you cannot tell from the source text, return false with
  confidence="low". Do not guess.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "definition_is_implementable": true | false,
     "spans": ["<verbatim quote from source text>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [positive] element_name=disciplineDate, data_type=Date,
             definition="The Begin date of the DisciplineAction"
             -> true; spans=["The Begin date of the DisciplineAction"]
                (date shape + role is self-evident from state text)

  [positive] element_name=courseDescription, data_type=String,
             definition="A description of the content standards and
                         goals covered in the course. Reference may be
                         made to state or national content standards."
             -> true; spans=["A description of the content standards
                              and goals covered in the course"]

  [positive] element_name=calendarCode, data_type=String,
             definition="The identifier for the Calendar. Submit
                         LEAID-SchoolId-Calendartypecodevalue-Sequence.
                         Example: 4242-5111-5DayAllGrades-01"
             -> true; spans=["Submit LEAID-SchoolId-Calendartypecodevalue-Sequence"]
                (state specifies the concatenation recipe)

  [negative] element_name=educationOrganizationId, data_type=Integer,
             definition="EducationOrganization Identity Column"
             -> false; spans=[]
                (Ed-Fi model label, not a state reporting description)

  [negative] element_name=schoolYear, data_type=Integer,
             definition="Current School Year"
             -> false; spans=[]
                (two-word tautology — no rule for which year to send)

  [negative] element_name=calendarTypeDescriptor, data_type=Descriptor,
             definition="Indicates the type of Calendar."
             -> false; spans=[]
                (no allowed-values source, no picking rule; implementer
                 is left to guess)

  [adversarial] element_name=sexDescriptor, data_type=Descriptor,
                definition="A person's gender."
                -> false; spans=[]
                (bare data-model label; vendor has no picking rule
                 even though the concept is common)

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
