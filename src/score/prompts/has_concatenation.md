{# Phase F prompt for `has_concatenation` (NACHOS methodology tier 3).
   True when the state text describes the element's VALUE as being
   assembled from multiple parts via concatenation or string-join —
   SQL CONCAT, `||` operator, f-strings/template joins, or prose
   descriptions of composite codes ("6-position field made up of race
   codes concatenated alphabetically").

   POC-2/Phase-B trap: descriptor-reference rows whose display labels
   read "CODE:Label" (e.g. "A:Autism, DB:Deafblind") are enumerated
   value-set tables, NOT concatenation. The element's stored value
   is the code, not the assembled display text.

   Polarity: standard (True = affirmative claim; spans required when True).
#}
# SYSTEM

You extract ONE factual claim about an Ed-Fi data element: does the
state text describe the element's value as a CONCATENATION — a string
built by joining multiple parts together (SQL CONCAT, `||` operator,
string + / template / f-string joins, or a prose description of a
composite field built by concatenating fields). You answer true/false
and quote the concatenation description verbatim for any True claim.

Rules:
- If has_concatenation is true, spans MUST contain >= 1 verbatim quote
  naming the concatenation operator (CONCAT, concatenate, concatenated,
  joined, assembled, composite string, `||`) and the parts being joined.
  Do not paraphrase.
- If has_concatenation is false, spans MUST be [].
- The element's VALUE must BE the concatenation. A descriptor code
  whose display label happens to use a "CODE:Description" format is
  not concatenation — the stored value is the code.
- Descriptor value-set enumerations (pipe-delimited or prose lists
  of "A:Autism, DB:Deafblind, EBD:Emotional...") are value-set
  catalogs, NOT concatenation. The element's value is one code, not
  the joined list.
- Numeric aggregation (SUM, COUNT, AVG) is `has_aggregation`, not
  `has_concatenation`. A field that totals credits is aggregation;
  a field whose value is multiple race codes glued together in
  alphabetical order is concatenation.
- Composite fields with prose descriptions like "6-position field
  made up of race codes concatenated alphabetically" ARE
  concatenation even without a CONCAT operator — the key signal is
  that the VALUE is assembled from multiple parts.
- Date / Descriptor data types almost never carry concatenations.
  String data types carry them most commonly, but that's a prior —
  read the text, not the data_type.
- If you cannot tell from the source text, return false with
  confidence="low". Do not guess.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "has_concatenation": true | false,
     "spans": ["<verbatim quote from source text>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [positive] element_name=racialEthnicCode,
             definition="6-position field made up of race/ethnicity
                         codes concatenated alphabetically."
             -> true; spans=["6-position field made up of race/ethnicity
                              codes concatenated alphabetically"]
                (composite code assembled by concatenation)

  [positive] element_name=displayName,
             rules="CONCAT(firstName, ' ', lastSurname) where both
                    fields are populated."
             -> true; spans=["CONCAT(firstName, ' ', lastSurname)
                              where both fields are populated"]

  [positive] element_name=programCompositeKey,
             definition="Composite key formed by joining
                         programTypeDescriptor || '-' || schoolYear."
             -> true; spans=["Composite key formed by joining
                              programTypeDescriptor || '-' || schoolYear"]

  [negative] element_name=firstName,
             definition="A name given to an individual at birth."
             -> false; spans=[]

  [negative] element_name=totalCreditsAttempted,
             definition="Sum of credits attempted across all
                         sections for the school year."
             -> false; spans=[]
                (aggregation, not concatenation — use has_aggregation)

  [adversarial] element_name=disabilityDescriptor,
                rules="Values: A:Autism; DB:Deafblind;
                       EBD:Emotional/Behavioral Disorder; ..."
             -> false; spans=[]
                (enumerated descriptor value set — the stored value
                 is a single code, not a joined string)

  [adversarial] element_name=gradeLevelDescriptor,
                definition="Pick from the K-12 grade code set."
             -> false; spans=[]
                (value-set reference, not concatenation)

  [adversarial] element_name=studentUniqueId,
                definition="A 10-character statewide-unique identifier
                            for the student."
             -> false; spans=[]
                (length constraint is not concatenation — the value
                 is a single identifier, not assembled from parts)

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
