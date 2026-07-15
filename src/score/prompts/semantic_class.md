{# Phase C2 source-lens prompt for `semantic_class` (semantic_fidelity
   dimension). The only enum3 fact in the plan — the LLM picks ONE of
   three labels that classify how the state's meaning relates to the
   Ed-Fi standard meaning for the same slot.

   Labels:
     - aligned         — state definition expresses the same concept /
                         same scope as edfi_standard_definition. Minor
                         wording differences are fine; the MEANING is
                         the same.
     - divergent       — state definition expresses a different concept
                         / different scope. Overlap may exist but the
                         state's reporting intent differs from Ed-Fi's.
                         Narrower scope, broader scope, or a redefined
                         sub-domain all count as divergent.
     - not_applicable  — no Ed-Fi counterpart exists (edfi_standard_
                         definition is "(none)" AND source=extension
                         with no core-entity slot). Extensions that
                         RE-DECLARE a core field don't count — that's
                         aligned (same concept) or divergent (changed
                         meaning).

   Authoring anchors (cross-state 2026-04-25):
   - WI often uses verbose Confluence prose vs terse Ed-Fi. If the
     state text paraphrases the same concept, mark aligned — wording
     differences alone do NOT make something divergent.
   - MN's "MDE mapping: X" pointer rows often leave the LLM without
     enough signal to decide. Those get low-conf aligned by default
     unless the state text contradicts Ed-Fi.
   - TX TEDS rows often add narrow scope ("reported only for X
     students"). Narrow-scope additions are divergent (state
     narrows), not aligned.
   - AZ extension rows extending a core concept with the same label
     and similar wording are aligned. Only mark not_applicable when
     source=extension AND edfi_standard_definition is "(none)".

   POC-2 trap: LLMs confuse "shorter wording" with "different
   meaning" and mis-classify. Mitigation: require evidence spans that
   a human reviewer can reason about, and constrain not_applicable to
   the hard structural case only.


   Polarity: n/a (enum3 — spans required for aligned w/quotes and divergent; empty for not_applicable).
#}
# SYSTEM

You classify how a state's definition of an Ed-Fi data element relates
to the Ed-Fi standard definition: aligned (same concept), divergent
(different scope or meaning), or not_applicable (no Ed-Fi counterpart).
For aligned and divergent you quote state text verbatim that supports
the classification. For not_applicable you return [] spans.

Rules:
- If semantic_class is "aligned", spans MUST contain >= 1 quote drawn
  verbatim from the state's definition_text / business_rules_text /
  element_specific_rules that expresses the same concept or scope as
  edfi_standard_definition. Exact wording need not match — a paraphrase
  of the same meaning is aligned.
- If semantic_class is "divergent", spans MUST contain >= 1 quote from
  the state text that expresses a DIFFERENT concept, narrower scope,
  broader scope, or a redefined sub-domain from what Ed-Fi says. Name
  the divergence in the span choice.
- If semantic_class is "not_applicable", spans MUST be []. This label
  applies ONLY when source=extension AND edfi_standard_definition is
  "(none)" — i.e. a state extension with no Ed-Fi core counterpart.
- Verbatim copies of edfi_standard_definition are aligned.
- Shorter / longer wording without a meaning shift is aligned.
- Narrower scope ("reported only for X", "excludes Y"), broader scope
  ("includes both X and Y"), regulatory redefinition, and sub-domain
  restriction are divergent.
- An extension row that repeats a core field name with a similar
  definition is aligned (or divergent if meaning shifts), NOT
  not_applicable — the core concept exists.
- If the state text is too thin to judge (bare label, "MDE mapping:
  X" pointer, identity column tag), return aligned with confidence=
  "low" and a span showing the text you based the call on. Do not
  guess divergent without a signal.
- If you cannot tell at all (state text = "(none)" or only
  edfi_standard_definition is visible), return aligned with
  confidence="low" and spans=[], using the absence of contradicting
  state text as the alignment signal.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "semantic_class": "aligned" | "divergent" | "not_applicable",
     "spans": ["<verbatim quote from state text>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [aligned] element_name=firstName,
            edfi_standard_definition="A name given to an individual at
                birth, baptism, or during another naming ceremony, or
                through legal change.",
            definition_text="First name of the student.",
            -> aligned; spans=["First name of the student."]
               (paraphrase of the same concept; wording is thinner but
                meaning is the same)

  [divergent] element_name=gradeLevelDescriptor,
              edfi_standard_definition="The grade level for which a
                  student enrolls.",
              definition_text="The grade level for which a student is
                  enrolled in the fall reporting window. Use the
                  October 1 snapshot enrollment.",
              -> divergent; spans=["in the fall reporting window",
                                   "Use the October 1 snapshot enrollment."]
                 (state narrows scope to a specific reporting window)

  [divergent] element_name=tribalAffiliationDescriptor,
              edfi_standard_definition="The tribe with which the
                  student is affiliated.",
              definition_text="The BIA-recognized tribe with which the
                  student is affiliated; includes state-recognized
                  tribes listed in Wis. Stat. 14.37.",
              -> divergent; spans=["includes state-recognized tribes
                                    listed in Wis. Stat. 14.37"]
                 (state broadens scope to include state-recognized
                  tribes Ed-Fi's BIA-only reading excludes)

  [not_applicable] element_name=alaskaNativeRegionalCorporation,
                   edfi_standard_definition="(none)",
                   definition_text="The Alaska Native Regional
                       Corporation the student is affiliated with.",
                   source=extension,
                   -> not_applicable; spans=[]
                      (state extension, no Ed-Fi core counterpart)

  [aligned] element_name=educationOrganizationId,
            edfi_standard_definition="The identifier assigned to an
                EducationOrganization.",
            definition_text="EducationOrganization Identity Column",
            -> aligned (low confidence); spans=["EducationOrganization
                                                 Identity Column"]
               (identity-column label aligns with Ed-Fi's identifier
                concept; thin text so confidence=low)

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
