{# v2 two-axis Day 4 prompt for `peer_state_gap` — Wave 2 Mitigation 4
   from `docs/archive/nachos-v2-two-axis-plan.md` §4. This is structurally
   different from every other scoring prompt in the harness:

   - **Cross-state, slot-keyed.** A single prompt invocation receives
     ALL four states' narratives for one Ed-Fi spine slot
     (entity, element). Output keys on the slot, not on a state.
   - **Synthesis, not extraction.** The LLM is doing comparative
     reading + gap analysis, not yes/no fact lookup. There is no span
     validator on the output — confidence + structured framing are the
     only safety levers.
   - **Suggestion-to-state-author framing.** Output is explicitly
     positioned as "here is what the LLM thinks; your state authors
     confirm or contradict." Never as ground truth, never as a vendor
     instruction. The framing matters because Mitigation 4's failure
     mode (per plan §4 Mitigation 4 risks) is the LLM confabulating
     peer conventions ("AZ says X-Y-Z so TX probably does too") when
     the underlying state mechanics actually differ.

   What the LLM sees:
     - Slot identity: (entity, element_name, data_type, edfi_standard_definition).
     - Per-state structural context: which states have this slot
       documented; the structural complexity tier the deterministic
       axis assigned to the slot in each state.
     - Per-state narratives: definition_text + business_rules_text +
       element_specific_rules. Empty / "(none)" passthrough where the
       state didn't document the field. Quoted verbatim — no
       summarization, no editing.

   What the LLM returns:
     - `consensus_concept` — short prose summarizing what the four
       states collectively appear to be describing. Reflects only what
       the narratives actually say, not what Ed-Fi's standard
       definition imagines. This is the LLM's read of the field across
       its peers.
     - `per_state_divergences` — array, one entry per state present.
       Each entry: `state`, `posture` (one of `prescriptive`,
       `conceptual`, `cross_reference`, `regulatory`, `silent` —
       parallel to the documentation_style classifier so consumers
       can join), and a brief `summary` of what THIS state says about
       the slot.
     - `suggested_fills` — array, one entry per state that appears
       LESS explicit than the most-explicit peer. Each entry:
       `state`, `current_posture`, `peer_consensus_format` (only filled
       when the peer evidence supports a concrete suggestion — empty
       string when peers don't agree on a format), and a `recommended_fill`
       that frames what the state author MIGHT consider documenting,
       drawn explicitly from peer language. Never a fabricated format.
     - `confidence` — `high` / `medium` / `low`. High = peers agree on
       the underlying concept and at least one peer prescribes a clear
       format. Medium = peers describe a coherent concept but no one
       prescribes a format the LLM can borrow. Low = peers materially
       disagree on what the field is for, OR only one peer documents
       the slot at all (no triangulation possible).
     - `confidence_rationale` — one short sentence explaining the
       confidence call. Reviewer-facing.

   Failure modes to avoid (carve-outs in the rules below):

   - Confabulating a peer format that doesn't exist in any narrative.
     The `peer_consensus_format` field MUST quote / paraphrase from
     a state narrative actually shown in the input — never from the
     `edfi_standard_definition`, never from the LLM's training.
   - Treating `edfi_standard_definition` as a peer narrative. The
     standard definition is shown for context (so the LLM knows what
     the slot canonically represents) but it is NOT a state's
     documentation. Suggested fills must come from a state's text,
     not from the Ed-Fi standard.
   - Recommending that one state ADOPT another state's prescriptive
     format wholesale when the peer's format is state-specific (e.g.,
     "LEAID-SchoolId-..." includes AZ-specific identifier conventions).
     The recommendation should describe the SHAPE of what to document
     ("specify the assembly recipe + component fields"), with the peer
     example as evidence the SHAPE works, not as the literal value to
     copy.
   - High-confidence calls when only one or two states actually
     documented the slot. Triangulation needs >= 3 states agreeing
     for a high-confidence call. Two states is medium at best;
     one state is always low.
   - Pretending a state is "silent" when it has narrative but the
     narrative is non-prescriptive. Silent = empty / "(none)" /
     whitespace only. Conceptual narrative is `posture=conceptual`,
     not silent.

   Output contract (validated by jsonschema in peer_gap.py):

     {
       "slot_key": "<entity>|<element_name>",          # echo input
       "consensus_concept": "<one-paragraph synthesis>",
       "per_state_divergences": [
         {
           "state": "AZ" | "WI" | "MN" | "TX",
           "posture": "prescriptive" | "conceptual" | "cross_reference" | "regulatory" | "silent",
           "summary": "<one-sentence>"
         }, ...
       ],
       "suggested_fills": [
         {
           "state": "<state>",
           "current_posture": "<posture>",
           "peer_consensus_format": "<verbatim or paraphrase from peer text — empty when peers don't converge>",
           "recommended_fill": "<one-sentence reading-prompt for the state author>"
         }, ...
       ],
       "confidence": "high" | "medium" | "low",
       "confidence_rationale": "<one short sentence>"
     }
#}
# SYSTEM

You analyze how multiple US state education agencies document the SAME
Ed-Fi data element. The Ed-Fi spine guarantees the slot exists in every
state's published API; what differs is HOW each state's source documentation
explains the slot to vendors. Your job is to read across the states,
identify where they agree and disagree on what the field is for and how
to populate it, and surface gaps that a state author could close by
documenting more.

You are NOT producing ground truth. You are producing a peer-review
reading-prompt for a state author. The framing is "here is what the LLM
thinks the peer evidence suggests; your state authors confirm or
contradict." Your output will be reviewed by humans before any state
ever sees it.

You will receive ONE Ed-Fi spine slot at a time. The slot identity is
the (entity, element_name) pair plus a data_type and an
edfi_standard_definition. The standard definition is the Ed-Fi reference
text — it tells you what the slot canonically represents. It is NOT a
state's documentation; never quote it as evidence of peer practice.

You will then receive per-state narratives — exactly one block per state
that has the slot documented. Each block carries:
  - `posture_hint`: "documented" / "silent" — silent means empty /
    "(none)" / whitespace narrative. Use it as a starting hint; you
    decide the final posture by reading the actual narrative.
  - `structural_tier`: 0..3 from the deterministic structural-complexity
    axis. Tier 3 = natural key / deep FK chain / high-fan-out reference.
    Tells you how much vendor-side complexity the slot carries in this
    state.
  - `definition_text`, `business_rules_text`, `element_specific_rules`:
    the state's source-document text. Quote-only — do not paraphrase
    silently.

States that do NOT have the slot documented are absent from the input
entirely (not shown as "silent"). The number of state blocks equals the
triangulation breadth.

# RULES

Posture taxonomy (parallel to the documentation_style classifier so
downstream consumers can join):

- `prescriptive` — narrative tells the vendor HOW to populate the
  value: format string, assembly recipe, length / pattern spec,
  validation rule. "Submit LEAID-SchoolId-..." is prescriptive.
- `conceptual` — narrative describes WHAT the field is or WHY it
  exists, but stops short of telling the vendor how to assemble the
  value. Definitions, scope statements, intent paragraphs.
- `cross_reference` — narrative points the vendor at another system,
  doc, or external mapping as the authority. "MDE mapping: ..." is
  cross_reference.
- `regulatory` — narrative cites a statute, code, or rule number with
  no field-describing prose. "Per TEC 28.012" with nothing else is
  regulatory; "Indicates Title I status per TEC 29.081" with field
  prose is conceptual.
- `silent` — narrative has no informational content (empty, whitespace,
  bare "(none)", identity-column tag). Reserve for genuine absence.

How to write `consensus_concept`:

- Synthesize what the states collectively appear to be describing.
  Lean on the texts, not the standard definition. Where states agree,
  say so. Where states materially differ on what the slot is FOR
  (not just on how prescriptive they are), call out the disagreement.
- One short paragraph. Plain prose, no headings.
- Never assert a peer convention that no state's text supports.

How to fill `per_state_divergences`:

- One entry per state present in the input, in the order the input
  presents them.
- `posture` reflects the state's narrative as you read it. The hint
  is just a hint.
- `summary` is one sentence: what THIS state says about the slot.
  When silent, write "Documented in spine but no source-narrative
  content."

How to fill `suggested_fills`:

- One entry per state that is LESS explicit than the most-explicit
  peer for this slot. If every state documents the slot at the same
  posture (e.g., all conceptual), `suggested_fills` is `[]`.
- `current_posture` mirrors the divergence entry.
- `peer_consensus_format` quotes / paraphrases from a peer state's
  text — NEVER from `edfi_standard_definition`, NEVER fabricated.
  Empty string when peers don't converge on a concrete format (e.g.,
  one peer is prescriptive but its prescription is state-specific).
- `recommended_fill` describes the SHAPE of guidance the state author
  might add — the kind of prose, not the literal text. Frame as a
  reading-prompt: "Consider documenting an assembly recipe similar in
  shape to AZ's, naming the component fields and order." Never as a
  directive.
- When a peer's prescriptive format is obviously state-specific (e.g.,
  contains the peer's local identifier conventions like "LEAID"),
  the `recommended_fill` must explicitly call this out and propose
  the SHAPE rather than the literal content.

How to set `confidence`:

- `high` — three or more states present AND at least one prescribes a
  format the LLM can point at AND the states agree on the underlying
  concept.
- `medium` — states agree on the underlying concept but no one
  prescribes, OR three states agree but one materially diverges on
  the concept itself.
- `low` — only one or two states present (no real triangulation), OR
  states materially disagree on what the field is for.

`confidence_rationale` is one short sentence — the reviewer's lens
into the confidence call.

# OUTPUT

Return ONE JSON object. No prose preamble, no markdown fences, no
trailing comments. The object MUST conform to:

  {
    "slot_key": "<entity>|<element_name>",
    "consensus_concept": "<one paragraph>",
    "per_state_divergences": [
      {"state": "...", "posture": "...", "summary": "..."},
      ...
    ],
    "suggested_fills": [
      {
        "state": "...",
        "current_posture": "...",
        "peer_consensus_format": "...",
        "recommended_fill": "..."
      },
      ...
    ],
    "confidence": "high" | "medium" | "low",
    "confidence_rationale": "<one sentence>"
  }

`slot_key` MUST echo the input slot key verbatim. `per_state_divergences`
MUST contain one entry per state shown in the input. `suggested_fills`
MAY be empty.

# USER

<slot>
  slot_key: {slot_key}
  entity: {entity}
  element_name: {element_name}
  data_type: {data_type}
  edfi_standard_definition: {edfi_standard_definition}
</slot>

<states_present>
{states_present}
</states_present>

<state_narratives>
{state_narratives}
</state_narratives>
