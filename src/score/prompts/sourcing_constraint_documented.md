{# Issue #59 source-lens prompt for `sourcing_constraint_documented`.
   The fact is a typed refinement of `semantic_fidelity = divergent_*`:
   when the source flags a row as divergent, what is the underlying
   mechanism that produces the divergence?

   Plan origin: docs/recommendations.md (after issue #59 lands) — the
   under-counted-cost cluster (issue #55 sample analysis, 7 of 12
   sampled rows) traced four mechanisms behind divergent_unclear /
   divergent_explained that the v12 SF mechanism couldn't tell apart:

     - transformation:  value coercion / mapping recipe at the field
                        level. AZ StudentSchoolAttendanceEvent.SchoolYear
                        "2015-2016" -> "2016" is the canonical shape.
                        The state describes a rule that converts the
                        source value into the reportable shape.

     - field_filter:    cross-field constraint at the source-row
                        level. WI email "only certain Organization
                        types contribute" is the canonical shape: the
                        field's value depends on a sibling-field
                        condition the rubric doesn't see, because the
                        constraint is data-shape-on-the-row, not rule
                        text inside this element.

     - external_sourcing: the value comes from a separate authority
                        the rest of the entity does NOT come from.
                        TX Language home-language survey is the
                        canonical shape: the row's value is sourced
                        from the state's home-language survey
                        instrument, not the SIS row supplying the
                        rest of the Language entity. Routing /
                        sourcing complexity invisible to the rubric.

     - custom_enumeration: the state defines its own enumerated value
                        set distinct from the Ed-Fi descriptor — not
                        a narrow / broaden of the canonical descriptor
                        (that's `state_scope_delta`), but a fresh
                        state-specific list the vendor must implement
                        and validate against.

     - none:            no constraint is documented. The state text
                        does not flag any sourcing / transformation /
                        filter / custom-enum mechanism. Empty narrative,
                        a one-line definition with no rules, or a
                        bare descriptor reference all land here.

     - unspecified:     the state flags divergence (or carries
                        divergence-shaped language) but does not pin
                        down which mechanism. "Different from Ed-Fi"
                        without elaboration, "see state guidance" with
                        no included guidance, or text that points at
                        an upstream system without naming the actual
                        sourcing / transformation / filter rule.

   Authoring anchors (cross-state 2026-04-29 — kept terse):

   - Transformation needs a verbatim recipe: a span quoting the
     coercion / format / mapping rule. "2015-2016 -> 2016" or
     "submit as YYYY" qualifies; "this differs from Ed-Fi" alone does
     NOT — that's `unspecified`.
   - Field_filter needs a quote naming the gating sibling field or
     filter condition: "reported only when Organization is a charter
     authorizer", "this column applies only when CourseLevelDescriptor
     is `Honors`". Pure population-scope language ("only for grades
     9-12") is NOT field_filter — that's a state_scope_delta narrowing.
   - External_sourcing needs a quote pointing at a SEPARATE
     authoritative source for the value. "Sourced from the home-language
     survey", "value provided by the state assessment vendor",
     "populated from MDE upstream registry". A pointer to another
     Ed-Fi entity in the SAME spec is not external_sourcing — that's
     intra-spec structure, not off-row sourcing.
   - Custom_enumeration needs a quote describing a state-specific
     enumerated value set: "Use the TEA-defined list of program
     codes", "Wisconsin tribal affiliation values listed in
     PI 41.04". A row that uses an Ed-Fi descriptor with a narrowed
     subset is `state_scope_delta = narrows`, not custom_enumeration.
     Custom_enumeration is reserved for value sets the state
     introduces independently of the Ed-Fi descriptor.

   Picking when multiple mechanisms apply: pick the dominant signal
   (the one a reviewer routing to a vendor team would call out first)
   and quote both clauses in spans. If you genuinely cannot pick,
   prefer transformation > external_sourcing > field_filter >
   custom_enumeration in that order — transformation is the highest
   information label and the easiest to verify against the quoted
   recipe.

   POC-2 trap this prompt avoids: the v12 SF mechanism produces
   `+1.0 fidelity_divergent_unclear` for every divergent_unclear row,
   regardless of whether the underlying work is a transformation, a
   constraint, or off-row sourcing. The label is correct directionally
   but coarse. This fact lets reviewers route the rows to different
   recommendations without changing the +1.0 magnitude (path #1 from
   the issue — score-influencing magnitude refinement is path #2 and
   out of scope here).

   Polarity: n/a (enum6 — transformation/field_filter/external_sourcing/custom_enumeration require >= 1 valid span; none/unspecified require empty spans).
#}
# SYSTEM

You classify the MECHANISM behind a documented divergence between a
state's definition of an Ed-Fi data element and the Ed-Fi standard.
You pick ONE label from a fixed six-category set that names what KIND
of work the divergence implies — a transformation, a cross-field
filter, off-row external sourcing, a custom enumeration, no constraint
documented, or an unspecified mechanism.

Categories (one-of, closed set):

- transformation:    the state documents a value coercion or mapping
                     recipe — a rule that converts the source value
                     into the reportable shape. Examples: span-to-year
                     coercion ("2015-2016" -> "2016"), date-format
                     normalization, code-table remap, scaled
                     percentage to integer.

- field_filter:      the state documents a cross-field constraint at
                     the row level — the value's contribution depends
                     on the value of a sibling field on the same row
                     (or on the row's containing entity having a
                     specific shape). Examples: "only certain
                     Organization types contribute", "reported only
                     when CourseLevelDescriptor is Honors". Population
                     filters that gate WHO is reported (not which
                     row's field is populated) are NOT field_filter —
                     those are state_scope_delta narrowings.

- external_sourcing: the state documents that the value is sourced
                     from a SEPARATE authoritative system the rest of
                     the entity does not come from. Examples: home-
                     language survey, state assessment vendor file,
                     upstream MDE registry. Pointers to another
                     entity in the SAME Ed-Fi spec are not external_
                     sourcing.

- custom_enumeration: the state defines its own enumerated value set
                     distinct from the Ed-Fi descriptor for this
                     element. The vendor must implement against the
                     state's list rather than the canonical Ed-Fi
                     descriptor. Narrowed / broadened use of an Ed-Fi
                     descriptor is NOT custom_enumeration — that's
                     state_scope_delta.

- none:              the state text documents no transformation /
                     filter / external sourcing / custom enumeration.
                     Definition + business rules carry no mechanism
                     language. The default for thinly-documented rows
                     and for rows that clearly align with the Ed-Fi
                     standard.

- unspecified:       the state flags divergence (or carries
                     divergence-shaped language) but does not pin
                     down which mechanism. "Different from Ed-Fi"
                     with no elaboration, "see state guidance" with
                     no included guidance, or hand-wavy off-spec
                     references that don't name the actual rule.

Rules:

- Pick exactly one label. No multi-label answers. When a row genuinely
  carries multiple mechanisms (rare; usually a transformation
  alongside a field_filter), pick the dominant signal — the one a
  reviewer routing to a vendor team would call out first — and quote
  both clauses in spans. Tie-break order: transformation,
  external_sourcing, field_filter, custom_enumeration. Use this only
  when the row is genuinely ambiguous, not as a default.
- IMPORTANT — only the STATE's narrative matters. On source-lens
  input you will see `edfi_standard_definition:` — that is the
  Ed-Fi standard, shown for background, NOT the state's
  documentation. Classify based solely on `definition_text`,
  `business_rules_text`, and `element_specific_rules`. Never quote
  from `edfi_standard_definition` in spans.
- For transformation, field_filter, external_sourcing, and
  custom_enumeration, spans MUST contain >= 1 quote drawn verbatim
  from the state's narrative naming the mechanism. The quote is the
  evidence that lets a reviewer verify the classification —
  transformation needs the recipe, field_filter needs the
  cross-field clause, external_sourcing needs the pointer at the
  other authority, custom_enumeration needs the reference to the
  state-specific value set.
- For none and unspecified, spans MUST be []. Both labels mean "no
  evidence" — there is nothing to quote because the narrative either
  carries no mechanism (none) or carries divergence-shaped language
  without naming the mechanism (unspecified).
- Quote evidence verbatim. Case and whitespace are tolerated; the
  validator folds curly quotes and collapses spaces. Paraphrases do
  not count — if you cannot quote it verbatim, the label does not
  apply.
- Population / scope language ("reported only for students in grade
  K-3", "limited to the fall window") is NOT field_filter or
  transformation — that is `state_scope_delta = narrows` and is owned
  by a different fact. Default to `none` if the row's only divergence
  signal is population scope.
- Format / length / pattern descriptions in isolation ("up to 10
  characters", "alphanumeric") are NOT transformation — those are
  `documentation_style = prescriptive` shape, not a value mapping.
  Transformation requires a SOURCE-to-REPORTABLE recipe, not just a
  format description for the reportable form.
- A row whose narrative is empty / "(none)" / whitespace is `none`
  with confidence = "high" — no documentation, no mechanism.
- A row whose narrative aligns with the Ed-Fi standard (paraphrase,
  alignment language) is `none` — there is no divergence to refine.
- If the narrative flags divergence but you cannot tell which
  mechanism applies (e.g., "this differs from the Ed-Fi standard;
  see state guidance"), return `unspecified` with confidence = "low".
  Do not guess transformation / field_filter / external_sourcing /
  custom_enumeration without a quotable phrase.
- Confidence guidance: `high` for clear, single-quote evidence;
  `medium` for inferable mechanisms with paraphrase-shaped support;
  `low` for marginal calls. `unspecified` should generally be `low`
  or `medium` — high-confidence `unspecified` is a contradiction.

Return a JSON array — no prose, no markdown fences — with one object
per input element, in input order, conforming to:

  [
    {"element_name": "<must match input exactly>",
     "sourcing_constraint_documented":
       "transformation" | "field_filter" | "external_sourcing" |
       "custom_enumeration" | "none" | "unspecified",
     "spans": ["<verbatim quote from state text>", ...],
     "confidence": "high" | "medium" | "low"}
  ]

Examples:

  [transformation] element_name=SchoolYear,
                   edfi_standard_definition="The identifier for the school year.",
                   definition_text="The school year of the attendance event.",
                   business_rules_text="Reported as a single year (YYYY). Convert source span '2015-2016' to '2016' (the spring year).",
                   -> transformation;
                      spans=["Convert source span '2015-2016' to '2016' (the spring year)."]
                      (explicit value-coercion recipe — vendor implements
                       a span-to-year transform on every write)

  [transformation] element_name=AverageDailyAttendance,
                   edfi_standard_definition="A measure of average daily attendance.",
                   business_rules_text="Reported as a percentage scaled by 10000 (e.g. 95.42% -> 9542).",
                   -> transformation;
                      spans=["Reported as a percentage scaled by 10000 (e.g. 95.42% -> 9542)."]
                      (scaled-integer transformation recipe)

  [field_filter] element_name=ElectronicMailAddress,
                 edfi_standard_definition="The electronic mail address.",
                 business_rules_text="Reported only when EducationOrganizationCategory is `LocalEducationAgency` or `School`. Other organization types do not contribute an email value to this field.",
                 -> field_filter;
                    spans=["Reported only when EducationOrganizationCategory is `LocalEducationAgency` or `School`."]
                    (cross-field constraint on a sibling field's value
                     gates whether this field carries a value at all)

  [external_sourcing] element_name=LanguageHomeUseDescriptor,
                      edfi_standard_definition="The language used at home.",
                      business_rules_text="Sourced from the state-administered Home Language Survey instrument; not derived from the SIS.",
                      -> external_sourcing;
                         spans=["Sourced from the state-administered Home Language Survey instrument; not derived from the SIS."]
                         (value comes from a separate authoritative
                          system, not the SIS row that supplies the
                          rest of the entity)

  [custom_enumeration] element_name=ProgramCode,
                       edfi_standard_definition="The type of program.",
                       business_rules_text="Use the TEA-defined program code list published in PEIMS Section 4.2; do not use Ed-Fi ProgramTypeDescriptor values.",
                       -> custom_enumeration;
                          spans=["Use the TEA-defined program code list published in PEIMS Section 4.2; do not use Ed-Fi ProgramTypeDescriptor values."]
                          (state-specific enumerated value set distinct
                           from the Ed-Fi descriptor)

  [none] element_name=firstName,
         edfi_standard_definition="A name given to an individual at birth, baptism, or naming ceremony.",
         definition_text="First name of the student.",
         -> none; spans=[]
            (narrative aligns with Ed-Fi; no divergence mechanism)

  [none] element_name=identifierCode,
         edfi_standard_definition="A unique identifier.",
         definition_text="(none)",
         business_rules_text="(none)",
         -> none; spans=[]
            (no narrative; nothing to refine)

  [unspecified] element_name=ParticipationStatus,
                edfi_standard_definition="The student's participation status.",
                business_rules_text="This element differs from the Ed-Fi standard; refer to state guidance for the correct value.",
                -> unspecified; spans=[]
                   (divergence flagged but mechanism not named —
                    cannot tell whether transformation, filter,
                    external sourcing, or custom enumeration applies)

  [none] element_name=GradeLevel,
         edfi_standard_definition="The grade level for which a student enrolls.",
         business_rules_text="Reported only for students active on October 1 of the reporting year.",
         -> none; spans=[]
            (population/scope narrowing, not a sourcing or
             transformation mechanism — owned by state_scope_delta)

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
