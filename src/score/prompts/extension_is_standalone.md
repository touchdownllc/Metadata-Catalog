{# Phase C2 source-lens prompt for `extension_is_standalone`
   (extension_justification dimension, tier 3 / tier 2 split). Only
   evaluated when source=extension. True iff the extension can be
   implemented by a vendor WITHOUT also populating a companion
   extension. Standalone = self-contained in meaning and obligation.

   Authoring anchors (2026-04-25):
   - An extension that refers to another extension to disambiguate
     ("report this value together with X extension") is NOT
     standalone.
   - An extension whose meaning is unambiguous on its own (date
     field, integer count, descriptor with allowed-values list
     embedded) is standalone.
   - Collection extensions (e.g. mn_*.memberships) whose sub-props
     are standalone individually are themselves standalone if the
     definition makes the collection shape clear.

   POC-2 trap: LLMs over-rate "standalone" when a field technically
   works in isolation but the state's reporting narrative implies a
   companion extension. Mitigation: mark False when the narrative
   names another extension or a companion field as a reporting
   prerequisite.


   Polarity: inverted (True = default; False = affirmative claim with spans).
#}
# SYSTEM

You extract ONE factual claim about an Ed-Fi state EXTENSION element:
is the extension standalone — implementable without also populating a
companion extension? You answer true/false and quote the state's
dependency language verbatim for any False claim. Non-extension rows
are skipped upstream; every row in this batch has source=extension.

Rules:
- If extension_is_standalone is false, spans MUST contain >= 1 quote
  drawn verbatim from definition_text / business_rules_text /
  element_specific_rules showing the companion-extension dependency.
  Typical signals: "report together with {extension}", "must be
  populated alongside {field}", "requires the {X} extension be
  reported", "paired with {extension_name}".
- If extension_is_standalone is true, spans MUST be [].
- The standard Ed-Fi key / reference fields (studentUniqueId, school-
  Id, schoolYear) do NOT count as companion-extension dependencies —
  those are core identity, every row depends on them.
- Mentioning another extension as context (cross-reference,
  documentation hint) WITHOUT stating the vendor must populate it is
  standalone. Only genuine implementation dependency flips to False.
- If the state text is "(none)", return TRUE with confidence="low" —
  absence of dependency language is the default; the row stands on
  its own until the state says otherwise.
- Do NOT assume interdependency from extension_name similarity. Two
  extensions that happen to share a prefix are independent unless the
  state text says otherwise.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "extension_is_standalone": true | false,
     "spans": ["<verbatim dependency quote>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [positive] element_name=TotalInstructionalDays,
             source=extension,
             extension_name=az.CalendarExtension,
             definition_text="The total number of instructional days
                 in the school year, reported per ARS §15-901.",
             -> true; spans=[]
                (no companion-extension dependency language)

  [positive] element_name=attendance,
             source=extension,
             extension_name=mn_studentEarlyEducationProgramAssociation,
             definition_text="The total number of days the child
                 attended during the reporting period.",
             -> true; spans=[]
                (self-contained integer count)

  [negative] element_name=englishLearnerReclassificationDate,
             source=extension,
             extension_name=wi.EnglishLearnerExtension,
             definition_text="The date the student exited EL status.
                 Must be paired with the EnglishLearnerExit extension
                 on the same StudentEducationOrganizationAssociation.",
             -> false; spans=["Must be paired with the EnglishLearner-
                               Exit extension on the same
                               StudentEducationOrganizationAssociation."]

  [negative] element_name=serviceSettingDescriptor,
             source=extension,
             extension_name=mn_studentSpecialEducationProgramAssociation,
             definition_text="The setting in which the service is
                 delivered. Report together with the ServiceHoursPerWeek
                 extension for the same association row.",
             -> false; spans=["Report together with the
                               ServiceHoursPerWeek extension for the
                               same association row."]

  [neutral] element_name=BeginDate,
            source=extension,
            extension_name=az.CalendarExtension,
            definition_text="(none)",
            -> true (low confidence); spans=[]
               (no state text — default to standalone with low conf)

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
