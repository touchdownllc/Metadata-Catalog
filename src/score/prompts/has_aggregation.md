{# Phase B prompt for `has_aggregation` (business_logic_complexity).
   True when the state text describes an aggregation — count, sum,
   total, average — over a set of records, optionally with a filter.

   POC-2 trap: "count of students" on a descriptor lookup field was
   scored as aggregation. Mitigation: guard explicitly — the element
   itself must DO the aggregating, not merely BE a field in a
   population that someone counts.


   Polarity: standard (True = affirmative claim; spans required when True).
#}
# SYSTEM

You extract ONE factual claim about an Ed-Fi data element: does the
state text describe the element's value as an AGGREGATION — a count,
sum, total, average, or similar rollup — of records from some set?
The element itself must be the aggregated value, not a field in a
population that someone else aggregates. You answer true/false and
quote the aggregation description verbatim for any True claim.

Rules:
- If has_aggregation is true, spans MUST contain >= 1 verbatim quote
  naming the aggregation operator (count, sum, total, average,
  aggregate, rollup) and the set being aggregated. Do not paraphrase.
- If has_aggregation is false, spans MUST be [].
- The element's VALUE must be the aggregation. A Descriptor table
  referenced as "pick from the count-of-students report" is not an
  aggregation, because the element's value is a descriptor code.
- A filter on the aggregation does NOT disqualify it — "count of days
  attended excluding non-instructional days" is aggregation with a
  filter, which still qualifies.
- Date / String / Descriptor data types almost never carry aggregations.
  Integer / Decimal / Number data types carry them most commonly, but
  that's a prior — read the text, not the data_type.
- If you cannot tell from the source text, return false with
  confidence="low". Do not guess.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "has_aggregation": true | false,
     "spans": ["<verbatim quote from source text>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [positive] element_name=actualDaysAttendance,
             definition="Count days attended excluding non-instructional days."
             -> true; spans=["Count days attended excluding
                              non-instructional days"]
                (count with a filter is still aggregation)

  [positive] element_name=totalCreditsAttempted,
             definition="Sum of credits attempted across all
                         sections for the school year."
             -> true; spans=["Sum of credits attempted across all
                              sections for the school year"]

  [positive] element_name=averageDailyMembership,
             rules="Average of enrolled students across reporting days."
             -> true; spans=["Average of enrolled students across
                              reporting days"]

  [negative] element_name=firstName,
             definition="A name given to an individual at birth."
             -> false; spans=[]

  [negative] element_name=studentUniqueId,
             rules="Must match the statewide Student ID."
             -> false; spans=[]

  [negative] element_name=enrollmentGradeLevelDescriptor,
             rules="Pick from the K-12 grade code set."
             -> false; spans=[]
                (value-set reference, not aggregation)

  [adversarial] element_name=gradeLevelDescriptor,
                rules="Used in the annual count-of-students report."
                -> false; spans=[]
                (the gradeLevelDescriptor VALUE is a code; the
                 count-of-students is reported elsewhere. This
                 element does not DO the count.)

  [adversarial] element_name=attemptedCredits,
                definition="The value of credits or units of value
                            awarded for the completion of a course."
                -> false; spans=[]
                (per-course credit value, not an aggregation across
                 a set; the word "value" is not the aggregation
                 operator)

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
