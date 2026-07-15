"""Phase C rule stage — facts → dimension scores.

Implements plan §7.2 pseudocode as executable Python. Each dimension is
a first-match-wins cascade over binary / count-int fact values plus
deterministic field-presence booleans. **No narrative text** is ever
consulted here. If a rule body needs a text pattern, the answer is a
new fact, not a new branch (plan §3.3).

Two disciplines are tested (``tests/test_rules_policy_discipline.py``):

1. ``import re`` is forbidden in this module.
2. Rule bodies never reference record narrative fields
   (``definition_text``, ``business_rules_text``,
   ``element_specific_rules``, ``edfi_standard_definition``,
   ``descriptor_table_values``, ``regulatory_citations``,
   ``related_entities``, ``collections_text``) — they only consult the
   ``FactView`` passed in.

Public surface:

- ``DimensionScore`` — dataclass carrying ``(name, value, rule_matched,
  inputs_used, confidence)`` per §7.3.
- ``FactView`` — keyed accessor over the per-record fact pool. Provides
  ``bool``, ``count``, ``confidence``, ``downgraded`` methods; refuses
  to surface raw narrative.
- ``load_fact_pool`` — reads Phase A/B JSONL artifacts for a
  (state, lens) pair and returns ``{record_key: FactView}``.
- ``score_record`` — runs every dimension rule against one FactView and
  returns ``list[DimensionScore]``.
- ``score_state`` — loads all facts for a (state, lens) pair and scores
  every record; returns a list of ``(record_key, [DimensionScore])``.

Rules (plan §7.2):

``documentation_completeness``
  inputs: ``definition_present``, ``business_rules_present``,
  ``data_type_canonical``, ``definition_is_implementable``,
  ``descriptor_values_enumerated``. The descriptor-enum gate acts as
  an OR-path for "implementable" — enumerated value sets carry their
  own implementation contract.

``obligation_clarity``
  inputs: ``required_when_stated``, ``conditional_reporting_stated``,
  ``populations_or_scope_stated``, ``descriptor_values_enumerated``.
  The gate DISABLES fact-4 (populations_or_scope_stated) on descriptor-
  enumerated rows — enumerated-value restriction is not population
  scope.

``business_logic_complexity``
  inputs: ``has_conditional_logic``, ``has_cross_entity_logic``,
  ``cross_entity_targets``, ``has_aggregation``. Note per §7.3:
  higher = more costly, NOT "better." Never summed into a quality
  aggregate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.utils.paths import scoring_phase_a_artifact_path

_LOGGER = logging.getLogger(__name__)

SPINE_DIMENSIONS: tuple[str, ...] = (
    "documentation_completeness",
    "obligation_clarity",
    "business_logic_complexity",
    "structural_depth",
    "documentation_style_tier",
    "nachos_score",
    # Integration Profile second-pass dimension composed over
    # structural_depth + documentation_style_tier. Sits last in the
    # tuple so dim_stats keeps first-pass dimensions contiguous.
    "documentation_gap",
)

SOURCE_DIMENSIONS: tuple[str, ...] = (
    "canonical_name_alignment",
    "definition_quality",
    "semantic_fidelity",
    "extension_justification",
    # v23 — issue #106 (Q4 of closeout bundle): business_logic_complexity
    # graduates to source-lens. Same rule body, same fact inputs (already
    # in SOURCE_RULE_INPUTS); registration moves the dim into the
    # source-lens cascade so analysts get a symmetric "cost-to-integrate"
    # signal across both lenses.
    "business_logic_complexity",
    "structural_depth",
    "documentation_style_tier",
    "nachos_score",
    # Integration Profile second-pass dimension; same composition as
    # spine-lens.
    "documentation_gap",
)

# Dimensions on the NACHOS methodology axis — excluded from the per-record
# quality-mean diagnostic (aggregate.py's ``SPINE_QUALITY_DIMENSIONS``
# already carves out ``business_logic_complexity`` for the same reason:
# it's a cost axis, not a quality axis). NACHOS sits on its own
# methodology axis per Phase F plan §3 — mixing it into the quality
# mean would re-introduce the confounding the demote-per-record-score
# phase just shed.
NACHOS_AXIS_DIMENSIONS: frozenset[str] = frozenset({"nachos_score"})

# Integration Profile — Structural Depth axis (plan
# ``docs/archive/nachos-v2-two-axis-plan.md`` §3). Excluded from the per-record
# quality mean for the same reason as NACHOS: deeper structure =
# costlier integration, not a quality signal. The Integration Profile
# composes this axis with ``documentation_style_tier`` into the
# ``documentation_gap`` signal.
STRUCTURAL_DEPTH_DIMENSIONS: frozenset[str] = frozenset({"structural_depth"})

# Integration Profile — Documentation Style axis (plan
# ``docs/archive/nachos-v2-two-axis-plan.md`` §3 / Day 2). Carved out of the
# per-record quality mean alongside ``structural_depth`` — this axis
# measures documentation STYLE (prescriptive / conceptual /
# cross_reference / regulatory / unspecified), not documentation
# QUALITY. A terse-doc state with conceptual narratives is not a
# low-quality state; it is a state whose narrative style is
# conceptual. Folding it into the quality mean would re-introduce the
# documentation-culture confound the Integration Profile is designed
# to split out.
DOCUMENTATION_STYLE_DIMENSIONS: frozenset[str] = frozenset(
    {"documentation_style_tier"}
)

# Integration Profile — Documentation Gap axis (plan
# ``docs/archive/nachos-v2-two-axis-plan.md`` §3.2 / Day 3, Mitigation 3). Carved
# out of the per-record source-lens quality mean because this axis is a
# gap-signal boolean (0/1), not a per-record quality tier. Folding it
# into the arithmetic mean would conflate "did the gap fire" with "is
# this row well-documented" — the whole point of the dimension is to
# surface gaps the other dimensions can't see.
DOCUMENTATION_GAP_DIMENSIONS: frozenset[str] = frozenset(
    {"documentation_gap"}
)

# Every fact the spine-lens rule stage reads. Kept here so a typo in a
# rule body surfaces as a clean "missing fact in pool" diagnostic rather
# than a silent False.
SPINE_RULE_INPUTS: frozenset[str] = frozenset(
    {
        # Deterministic — always high confidence.
        "definition_present",
        "business_rules_present",
        "data_type_canonical",
        "descriptor_values_enumerated",
        "is_natural_key",
        # Integration Profile — Structural Depth axis (plan ``docs/archive/nachos-v2-two-axis-plan.md``).
        "fk_chain_depth",
        "reference_fan_out",
        "sub_collection_depth",
        "descriptor_enum_breadth",
        "entity_extension_footprint",
        # v16 — issue #63 B5 (POC interim methodology call): bare FK
        # reference detection. nachos_score reads it to suppress the
        # has_cross_entity_logic contribution to the NACHOS tier.
        "element_is_bare_fk_reference",
        # v22 — issue #97 (2026-04-30): FK-named row whose only
        # conditional evidence is a parent-entity submission gate
        # annotation. business_logic_complexity + nachos_score read it
        # to suppress the has_conditional_logic contribution.
        "element_only_parent_entity_gate",
        # LLM-extracted.
        "definition_is_implementable",
        "required_when_stated",
        "conditional_reporting_stated",
        "populations_or_scope_stated",
        "has_conditional_logic",
        "has_cross_entity_logic",
        "has_aggregation",
        "has_concatenation",
        "cross_entity_targets",
        # Integration Profile — Documentation Style axis (plan Day 2).
        "documentation_style",
    }
)

# Plan §6.1 / §7.1 — the ten source-lens facts the rule stage consults.
# Includes 4 deterministic + 6 LLM, same "one fact = one file" rule.
# Phase F adds the business-logic fact trio (has_aggregation,
# has_concatenation, has_cross_entity_logic, cross_entity_targets,
# has_conditional_logic) so source-lens nachos_score has the same inputs
# the spine-lens rule consults. descriptor_values_enumerated is likewise
# added as the tier-0 anchor.
SOURCE_RULE_INPUTS: frozenset[str] = frozenset(
    {
        # Deterministic.
        "element_name_matches_canonical",
        "naming_deviation_cosmetic",
        "definition_present",
        "definition_text_substantive",
        "extension_mirrors_core_pattern",
        "descriptor_values_enumerated",
        "is_natural_key",
        # Integration Profile — Structural Depth axis (plan ``docs/archive/nachos-v2-two-axis-plan.md``).
        "fk_chain_depth",
        "reference_fan_out",
        "sub_collection_depth",
        "descriptor_enum_breadth",
        "entity_extension_footprint",
        # v16 — issue #63 B5: bare FK reference detection (shared with spine).
        "element_is_bare_fk_reference",
        # v22 — issue #97 (shared with spine): FK-named row whose only
        # conditional evidence is a parent-entity submission gate.
        "element_only_parent_entity_gate",
        # LLM-extracted (source-lens exclusive).
        "definition_adds_detail_beyond_edfi",
        "semantic_class",
        # Side-quest PR A (2026-04-28): merged from
        # ``state_narrows_edfi_scope`` + ``state_broadens_edfi_scope``
        # into a single mutually-exclusive enum.
        "state_scope_delta",
        "extension_is_necessary",
        "extension_is_standalone",
        # LLM-extracted (shared with spine — Phase F NACHOS inputs).
        "has_conditional_logic",
        "has_cross_entity_logic",
        "has_aggregation",
        "has_concatenation",
        "cross_entity_targets",
        # Integration Profile — Documentation Style axis (plan Day 2).
        "documentation_style",
    }
)


# H1 / Session 3 — productization-signal facts. These surface in the
# per-record sidecar's ``fact_provenance`` block as observability
# (for downstream templates / recommendations generators / analyst
# workbooks that want the sourcing class) but DO NOT enter any rule
# cascade. Per plan §Q2 resolution, integration_class is a
# productization axis orthogonal to the complexity-scoring axis;
# wiring it into ``SPINE_RULE_INPUTS`` / ``SOURCE_RULE_INPUTS`` would
# require a defensible contribution story (is `calculation` more
# complex than `concatenation`?) the 357-row pattern-match does not
# yet support. A later PR can promote productization signals into
# rule inputs if real signal emerges; this set stays separate for
# now.
#
# Artifacts are loaded by ``load_fact_pool`` alongside rule-input
# artifacts and treated as OPTIONAL — a missing artifact logs a
# warning and lets aggregate proceed, rather than failing the run.
# This preserves the existing "rule inputs are hard prerequisites"
# contract (missing → FileNotFoundError) while letting
# productization signals ship incrementally.
PRODUCTIZATION_SIGNAL_FACTS: tuple[str, ...] = (
    "integration_class",
)


# Per-lens observability facts — extracted by the shared run-all
# harness but NOT consumed by the lens's rule cascade. Surfaces them
# through ``fact_provenance`` so analysts/stakeholders can see the
# verdict even where the lens's scoring pipeline ignores the input.
#
# Rationale: ``semantic_class`` is a source-lens rule input (feeds
# ``semantic_fidelity`` on source) and intentionally absent from
# ``SPINE_RULE_INPUTS`` because spine's cascade doesn't need it. But
# the extractor runs the prompt against spine-lens records too (the
# fanout extracts every fact by default), so the artifact exists on
# disk at ``data/out/scoring/phase_a/{state}_spine_semantic_class.jsonl``.
# Promoting it to observability-only on spine lets analysts see the
# alignment verdict in the spine workbook without any rule-cascade
# change — zero impact on spine scoring arithmetic.
#
# Loaded as OPTIONAL (same contract as productization signals): a
# missing artifact logs a warning and aggregate proceeds.
LENS_OBSERVABILITY_FACTS: dict[str, tuple[str, ...]] = {
    # Issue #59 (2026-04-29) — `sourcing_constraint_documented` is a
    # typed refinement of `semantic_fidelity = divergent_*` that names
    # the divergence mechanism (transformation / field_filter /
    # external_sourcing / custom_enumeration). Source-lens-only because
    # the prompt grounds on the state's documented divergence; the
    # spine lens has no parallel surface. Path #1 from the issue: surface
    # the mechanism in `fact_provenance` + Recommendations templates,
    # leave `adjusted_nachos_score` untouched.
    #
    # Issue #124 PR 2 / #111 (2026-05-02) — `extension_fidelity_divergence`
    # is a deterministic enum (det.v11) produced by
    # `deterministic.extension_fidelity_divergence`. It surfaces shape-
    # divergence between an extension and its same-entity core
    # counterpart and lands as a typed-reason annotation in the v12 SF
    # fold label inside `aggregate._compute_nachos_adjustments`. Source-
    # lens only by registration in `SOURCE_DETERMINISTIC_FACTS`; the
    # spine-lens cascade has no SF dim so there's no consumer there.
    "source": (
        "sourcing_constraint_documented",
        "extension_fidelity_divergence",
    ),
    "spine": ("semantic_class",),
}

_CONFIDENCE_ORDER: Mapping[str, int] = {"low": 0, "medium": 1, "high": 2}
_CONFIDENCE_FROM_ORDER: Mapping[int, str] = {v: k for k, v in _CONFIDENCE_ORDER.items()}


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionScore:
    """One rule-stage outcome for one dimension.

    ``value`` is 0..3 for every dimension the rule evaluated. ``None``
    means the rule could not evaluate (inputs missing); the aggregate
    layer treats ``None`` as "needs review" and excludes the dimension
    from the per-record arithmetic mean.

    ``rule_matched`` names the branch that fired (e.g. ``"tier_3_full"``,
    ``"descriptor_enum_gate"``). Lets reviewers trace any score back to
    a single labelled rule path without re-running the rule.

    ``inputs_used`` records the fact values consulted — keeps the audit
    trail self-contained. Values are validated booleans / ints / None;
    no narrative text ever lands here.

    ``confidence`` is the min of the per-fact confidence levels across
    inputs consulted, forced to ``"low"`` if any consulted fact was
    downgraded to ``validated_value=None`` (hallucinated span,
    count-span mismatch, missing from response).
    """

    name: str
    value: int | None
    rule_matched: str
    inputs_used: dict[str, Any] = field(default_factory=dict)
    confidence: str = "low"


@dataclass(frozen=True)
class FactResult:
    """One fact's outcome for one record — what the rule stage actually reads.

    Never carries narrative text. ``value`` is the post-validation
    value: ``True``/``False`` for bool facts, a non-negative int for
    count facts, a string for enum facts (``semantic_class``), or
    ``None`` when validation downgraded the LLM's claim.
    ``confidence`` is the LLM-stated confidence (or ``"high"`` for
    deterministic facts). ``downgraded`` flips True when
    ``validated_value`` is None.

    ``spans`` carries verbatim match text. Deterministic facts that
    register in ``deterministic._SPAN_COMPUTATIONS`` (today:
    ``descriptor_values_enumerated``) emit strings directly. LLM-path
    facts emit ``list[dict]`` span shapes in the Phase A JSONL
    (``{"text": ..., "valid": ...}``); ``_row_to_fact_result`` lifts
    ``.text`` from ``valid=True`` entries so this tuple stays
    homogeneously ``tuple[str, ...]`` regardless of source shape. The
    ``valid`` filter is load-bearing — hallucinated spans the
    substring validator rejected must never surface as evidence.
    Rules never see this field (``FactView`` has no accessor for it)
    so the policy-discipline contract holds.

    ``provenance`` is ``None`` for every fact as extracted (LLM or
    deterministic — the artifact itself is the record of origin) and
    ``"human_corrected"`` when the issue #249 curation overlay replaced
    the extracted value with an analyst correction before the rule
    cascade ran. The aggregate layer serializes it into
    ``fact_provenance`` only when set, so sidecars without corrections
    stay byte-identical.
    """

    fact: str
    value: bool | int | str | None
    confidence: str
    downgraded: bool
    downgrade_reason: str | None
    spans: tuple[str, ...] = ()
    provenance: str | None = None


class FactView:
    """Keyed read-only view over one record's fact results.

    This is the **only** interface rules use to consult per-record
    data. It exposes accessors for typed values + confidence and
    refuses to return anything a rule shouldn't depend on. Anything
    that would need narrative text should become a new fact.

    ``source`` and ``extension_name`` are categorical ingest
    attributes (not narrative) and surface here so source-lens rules
    can consult them without touching the record. They carry the
    same semantics as on ``ElementRecord``.
    """

    __slots__ = (
        "record_key",
        "entity",
        "element_name",
        "source",
        "extension_name",
        "documented",
        "documentation_source",
        "_facts",
    )

    def __init__(
        self,
        record_key: str,
        entity: str,
        element_name: str,
        facts: Mapping[str, FactResult],
        *,
        source: str = "unknown",
        extension_name: str | None = None,
        documented: bool = False,
        documentation_source: str = "source_doc",
    ) -> None:
        self.record_key = record_key
        self.entity = entity
        self.element_name = element_name
        self.source = source
        self.extension_name = extension_name
        self.documented = documented
        self.documentation_source = documentation_source
        self._facts = dict(facts)

    def has(self, fact: str) -> bool:
        return fact in self._facts

    def bool(self, fact: str, *, default: bool = False) -> bool:
        """Return the fact's bool value, or ``default`` if missing/downgraded.

        Rules call this for any bool fact. A downgraded (None-valued)
        fact resolves to ``default`` — the caller records low
        confidence so reviewers can see the cliff.
        """
        result = self._facts.get(fact)
        if result is None or result.value is None:
            return default
        return bool(result.value)

    def count(self, fact: str, *, default: int = 0) -> int:
        """Return the fact's int value, or ``default`` if missing/downgraded."""
        result = self._facts.get(fact)
        if result is None or result.value is None:
            return default
        return int(result.value)

    def enum(self, fact: str, *, default: str | None = None) -> str | None:
        """Return the fact's string value, or ``default`` if missing/downgraded.

        Used by source-lens rules reading ``semantic_class``. Unlike
        ``bool``/``count``, ``None`` is a meaningful default — callers
        typically pass it and branch on ``is None``.
        """
        result = self._facts.get(fact)
        if result is None or result.value is None:
            return default
        return str(result.value)

    def confidence(self, fact: str) -> str:
        """Return ``"high"``/``"medium"``/``"low"``. Missing fact → ``"low"``."""
        result = self._facts.get(fact)
        if result is None:
            return "low"
        return result.confidence

    def downgraded(self, fact: str) -> bool:
        """True if the fact exists and was downgraded post-validation."""
        result = self._facts.get(fact)
        return bool(result and result.downgraded)

    def missing(self, facts: Sequence[str]) -> list[str]:
        """Return the subset of ``facts`` not in this view's pool."""
        return [f for f in facts if f not in self._facts]

    def snapshot(self, facts: Sequence[str]) -> dict[str, Any]:
        """Return a plain dict of fact → value for the listed facts.

        Used to populate ``DimensionScore.inputs_used``. Missing facts
        surface as ``None``; downgraded facts surface as ``None`` too
        (same sentinel; the aggregate layer inspects ``.downgraded`` on
        the raw FactResult via ``raw_fact_results`` when it needs to
        distinguish).
        """
        out: dict[str, Any] = {}
        for f in facts:
            result = self._facts.get(f)
            out[f] = None if result is None or result.value is None else result.value
        return out

    def raw_fact_results(self, facts: Sequence[str]) -> dict[str, FactResult | None]:
        """Escape-hatch for the aggregate layer's confidence composite.

        Rule bodies must not call this — it returns raw FactResults,
        which carry metadata the aggregate layer needs (downgrade
        reason) but rules don't. The policy-discipline AST test does
        not forbid this method because the aggregate is a separate
        module; rules.py's own rule functions never call it.
        """
        return {f: self._facts.get(f) for f in facts}

    def apply_correction(self, result: FactResult) -> None:
        """Replace one fact's result — the issue #249 overlay seam.

        The ONLY sanctioned mutation of a view's fact pool: the
        aggregate layer applies analyst fact corrections here after
        loading the pool and before the rule cascade runs, so rules
        read the corrected value through the ordinary accessors. Rule
        bodies must never call this (same policy-discipline posture as
        ``raw_fact_results``). The replacement carries
        ``provenance="human_corrected"`` so the sidecar shows exactly
        which facts a human touched.
        """
        self._facts[result.fact] = result


# ---------------------------------------------------------------------------
# Confidence composite for a single dimension
# ---------------------------------------------------------------------------


def _confidence_for_dim(facts: FactView, consulted: Sequence[str]) -> str:
    """Min confidence across consulted facts, demoted to ``"low"`` on any downgrade.

    Missing facts surface as ``"low"`` too — a rule cannot be confident
    about an input it did not see. The aggregate layer re-derives this
    across dimensions for the per-record confidence composite.
    """
    level = _CONFIDENCE_ORDER["high"]
    for f in consulted:
        if facts.downgraded(f):
            return "low"
        lv = _CONFIDENCE_ORDER.get(facts.confidence(f), 0)
        if lv < level:
            level = lv
    return _CONFIDENCE_FROM_ORDER[level]


# ---------------------------------------------------------------------------
# Rule bodies
# ---------------------------------------------------------------------------


_DOC_INPUTS = (
    "definition_present",
    "business_rules_present",
    "data_type_canonical",
    "definition_is_implementable",
    "descriptor_values_enumerated",
)


def documentation_completeness(facts: FactView) -> DimensionScore:
    """Plan §7.2 — spine-lens documentation_completeness.

    Tiers (first match wins):

    - ``tier_3_full``           — all four structural present + row is
      LLM-implementable OR descriptor-enumerated.
    - ``tier_2_structure``      — definition + rules + canonical type,
      implementable not confirmed.
    - ``tier_1_minimal``        — definition + canonical type only.
    - ``tier_0_missing``        — no definition.

    The descriptor-enum gate acts as a substitute for
    ``definition_is_implementable``: when the gate fires, the row's
    value set is enumerated in the narrative, which is itself an
    implementation contract. This closes the WI
    descriptor-value-table hallucination FP (Phase B 2026-04-24).
    """
    dp = facts.bool("definition_present")
    brp = facts.bool("business_rules_present")
    dtc = facts.bool("data_type_canonical")
    di = facts.bool("definition_is_implementable")
    dve = facts.bool("descriptor_values_enumerated")
    effective_implementable = di or dve

    inputs = facts.snapshot(_DOC_INPUTS)

    if dp and brp and dtc and effective_implementable:
        rule = "tier_3_full" if di else "tier_3_descriptor_enum"
        value = 3
    elif dp and brp and dtc:
        rule = "tier_2_structure"
        value = 2
    elif dp and dtc:
        rule = "tier_1_minimal"
        value = 1
    else:
        rule = "tier_0_missing"
        value = 0

    return DimensionScore(
        name="documentation_completeness",
        value=value,
        rule_matched=rule,
        inputs_used=inputs,
        confidence=_confidence_for_dim(facts, _DOC_INPUTS),
    )


_OBL_INPUTS = (
    "required_when_stated",
    "conditional_reporting_stated",
    "populations_or_scope_stated",
    "descriptor_values_enumerated",
)


def obligation_clarity(facts: FactView) -> DimensionScore:
    """Plan §7.2 — spine-lens obligation_clarity.

    Tiers (first match wins):

    - ``tier_3_required_scoped`` — ``required_when_stated`` AND
      ``populations_or_scope_stated``.
    - ``tier_2_conditional``     — ``conditional_reporting_stated``.
    - ``tier_1_partial``         — ``populations_or_scope_stated`` or
      ``required_when_stated``.
    - ``tier_0_none``            — none of the above.

    Descriptor-enum gate: when ``descriptor_values_enumerated`` is
    True, fact-4 (``populations_or_scope_stated``) is treated as False
    for this dimension — enumerated value-set restriction is not
    "populations or scope" in the obligation sense. This closes the MN
    program-type soft-FP cluster (Phase B 2026-04-24).
    """
    rws = facts.bool("required_when_stated")
    crs = facts.bool("conditional_reporting_stated")
    pos_raw = facts.bool("populations_or_scope_stated")
    dve = facts.bool("descriptor_values_enumerated")
    pos = False if dve else pos_raw  # descriptor-enum demotes fact-4

    inputs = facts.snapshot(_OBL_INPUTS)
    inputs["populations_or_scope_stated__gated"] = pos

    if rws and pos:
        rule = "tier_3_required_scoped"
        value = 3
    elif crs:
        rule = "tier_2_conditional"
        value = 2
    elif pos or rws:
        rule = "tier_1_partial"
        value = 1
    else:
        rule = (
            "tier_0_descriptor_enum"
            if dve and pos_raw
            else "tier_0_none"
        )
        value = 0

    return DimensionScore(
        name="obligation_clarity",
        value=value,
        rule_matched=rule,
        inputs_used=inputs,
        confidence=_confidence_for_dim(facts, _OBL_INPUTS),
    )


_BLC_INPUTS = (
    "has_conditional_logic",
    "has_cross_entity_logic",
    "cross_entity_targets",
    "has_aggregation",
    # v22 — issue #97 (2026-04-30): FK-named row whose only conditional
    # evidence is a parent-entity submission gate annotation. Suppresses
    # has_conditional_logic; recorded in inputs_used for audit.
    "element_only_parent_entity_gate",
)


def business_logic_complexity(facts: FactView) -> DimensionScore:
    """Plan §7.2 — spine-lens business_logic_complexity.

    **Higher = more costly, NOT "better."** Rollups present this as a
    dedicated "complexity" column, never summed into a quality aggregate
    (plan §7.3).

    Tiers (first match wins):

    - ``tier_3_agg_cross``       — aggregation AND cross-entity logic.
    - ``tier_2_agg_or_multicross`` — aggregation alone OR (cross-entity
      + ≥2 distinct targets).
    - ``tier_1_cond_or_cross``   — conditional logic OR single-target
      cross-entity.
    - ``tier_0_none``            — none of the above.

    **Cross-fact reconciliation** (Phase D carryover #6): if
    ``has_cross_entity_logic`` is True but ``cross_entity_targets``
    is 0 (or vice versa), the count fact is authoritative — fact-5
    without a supporting count acts as False. This is the documented
    reconciliation policy from the Phase C brief.

    **Issue #97 calibration** (v22, 2026-04-30): when
    ``element_only_parent_entity_gate`` is True, suppress
    ``has_conditional_logic``. The conditional belongs to the parent
    record's submission gate (a WI Confluence
    ``[Public: …, Choice: …]`` annotation describing whether the whole
    record is required for that school type), not to the FK reference's
    own value or presence. Element-level conditional logic surfaced by
    a non-gate span (TX ``StudentId`` first-character constraint, AZ
    descriptor presence rules) is unaffected.
    """
    hcl = facts.bool("has_conditional_logic")
    has_agg = facts.bool("has_aggregation")

    # Reconciliation: hce requires a supporting target count. Shared
    # helper keeps the Phase F ``nachos_score`` rule and this
    # comprehension-cost rule in lockstep on "real cross-entity logic".
    hce, targets = _reconcile_cross_entity(facts)

    # v22 — issue #97 (2026-04-30): suppress has_conditional_logic on
    # FK-named rows whose only conditional evidence is the parent-
    # entity submission gate annotation. See
    # ``element_only_parent_entity_gate`` (deterministic.py det.v10).
    if facts.bool("element_only_parent_entity_gate"):
        hcl = False

    inputs = facts.snapshot(_BLC_INPUTS)
    inputs["has_cross_entity_logic__reconciled"] = hce
    inputs["has_conditional_logic__reconciled"] = hcl

    if has_agg and hce:
        rule = "tier_3_agg_cross"
        value = 3
    elif has_agg or (hce and targets >= 2):
        rule = "tier_2_agg_or_multicross"
        value = 2
    elif hcl or hce:
        rule = "tier_1_cond_or_cross"
        value = 1
    else:
        rule = "tier_0_none"
        value = 0

    return DimensionScore(
        name="business_logic_complexity",
        value=value,
        rule_matched=rule,
        inputs_used=inputs,
        confidence=_confidence_for_dim(facts, _BLC_INPUTS),
    )


_STRUCTURAL_INPUTS = (
    "is_natural_key",
    "fk_chain_depth",
    "reference_fan_out",
    "sub_collection_depth",
    "descriptor_enum_breadth",
    "entity_extension_footprint",
)


# Tier thresholds for ``structural_depth``. Initial values per plan
# §3.1 "tier boundaries to be tuned after initial run." The structural
# axis is lens-independent by construction, so spine-lens and source-lens
# consult the same thresholds. Ship-criterion: distribution is
# non-trivial across AZ/WI/MN/TX (not all-0 or all-3). Tune after the
# Day 1 eyeball pass if any state pins to a single tier.
_STRUCT_FK_DEPTH_TIER_3: int = 3
_STRUCT_FK_DEPTH_TIER_2: int = 2
_STRUCT_FAN_OUT_TIER_3: int = 5
_STRUCT_FAN_OUT_TIER_2: int = 2
_STRUCT_DESCRIPTOR_BREADTH_TIER_2: int = 20
_STRUCT_DESCRIPTOR_BREADTH_TIER_1: int = 5
_STRUCT_EXTENSION_FOOTPRINT_TIER_2: int = 2


def structural_depth(facts: FactView) -> DimensionScore:
    """Integration Profile — structural depth from the spine alone.

    Plan ``docs/archive/nachos-v2-two-axis-plan.md`` §3.1. Every input is a
    deterministic, count-valued fact derived from the Ed-Fi spine
    catalog — never from narrative — so the dimension is immune to
    documentation-culture confound.

    Tiers (first match wins):

    - ``tier_3_deep_or_broad`` — deep FK chain (depth >= 3) OR a
      natural-key element inside a high-fan-out entity (fan-out >= 5)
      OR a wide descriptor enumeration (breadth >= 20). These are the
      rows where vendors confront multi-hop referential integrity,
      broad downstream coupling, or large value-set handling.
    - ``tier_2_composite`` — medium FK chain (depth 2) OR moderate
      fan-out (>= 2) OR natural-key in a sub-collection OR an entity
      with >= 2 extensions OR a mid-breadth descriptor (>= 5 values).
      Meaningful structural weight but not headline-grade.
    - ``tier_1_marked`` — single structural signal fires: is_natural_key
      alone, fk_chain_depth >= 1, one sub-collection membership, one
      extension on the entity, or a small descriptor enumeration
      (>= 1 value).
    - ``tier_0_flat`` — no structural signals. Scalar leaf on a root
      entity with no extensions, fan-out, or descriptor breadth.

    The tier 3 short-circuit on natural-key + high fan-out captures
    the classic "edit this field and N other entities light up"
    pattern. fan-out alone (no is_natural_key) stays tier 2 — the
    entity is fanned-out-against, but a non-identity field on it
    doesn't carry the ripple weight.
    """
    is_nk = facts.bool("is_natural_key")
    fk_depth = facts.count("fk_chain_depth")
    fan_out = facts.count("reference_fan_out")
    sub_depth = facts.count("sub_collection_depth")
    desc_breadth = facts.count("descriptor_enum_breadth")
    ext_footprint = facts.count("entity_extension_footprint")

    inputs = facts.snapshot(_STRUCTURAL_INPUTS)

    # Tier 3 gates — any one of these is enough.
    if fk_depth >= _STRUCT_FK_DEPTH_TIER_3:
        rule = "tier_3_deep_fk_chain"
        value = 3
    elif is_nk and fan_out >= _STRUCT_FAN_OUT_TIER_3:
        rule = "tier_3_natural_key_high_fan_out"
        value = 3
    elif desc_breadth >= _STRUCT_DESCRIPTOR_BREADTH_TIER_2 * 2:
        # Wide descriptor enumerations (>= 40 values) are tier 3 on
        # their own — a vendor handling 40+ codes is doing real work
        # even without an FK chain behind it.
        rule = "tier_3_wide_descriptor"
        value = 3
    # Tier 2 gates.
    elif fk_depth >= _STRUCT_FK_DEPTH_TIER_2:
        rule = "tier_2_medium_fk_chain"
        value = 2
    elif fan_out >= _STRUCT_FAN_OUT_TIER_2:
        rule = "tier_2_fan_out"
        value = 2
    elif is_nk and sub_depth >= 1:
        rule = "tier_2_natural_key_sub_collection"
        value = 2
    elif ext_footprint >= _STRUCT_EXTENSION_FOOTPRINT_TIER_2:
        rule = "tier_2_heavy_extension_footprint"
        value = 2
    elif desc_breadth >= _STRUCT_DESCRIPTOR_BREADTH_TIER_2:
        rule = "tier_2_broad_descriptor"
        value = 2
    # Tier 1 gates.
    elif is_nk:
        rule = "tier_1_natural_key"
        value = 1
    elif fk_depth >= 1:
        rule = "tier_1_fk_chain"
        value = 1
    elif sub_depth >= 1:
        rule = "tier_1_sub_collection"
        value = 1
    elif ext_footprint >= 1:
        rule = "tier_1_extension_present"
        value = 1
    elif desc_breadth >= _STRUCT_DESCRIPTOR_BREADTH_TIER_1:
        rule = "tier_1_descriptor_breadth"
        value = 1
    else:
        rule = "tier_0_flat"
        value = 0

    return DimensionScore(
        name="structural_depth",
        value=value,
        rule_matched=rule,
        inputs_used=inputs,
        confidence=_confidence_for_dim(facts, _STRUCTURAL_INPUTS),
    )


_DOC_STYLE_TIER_INPUTS = ("documentation_style",)


def documentation_style_tier(facts: FactView) -> DimensionScore:
    """Integration Profile — documentation-style tier from narrative.

    Plan ``docs/archive/nachos-v2-two-axis-plan.md`` §3.1 / Day 2. Reads the
    five-category ``documentation_style`` classifier enum and maps it
    to a 0..3 tier. The axis measures documentation STYLE, not
    documentation QUALITY — a conceptual narrative on a structurally
    trivial field is appropriate; a conceptual narrative on a deep
    FK-chain natural key is the documentation gap composed with
    ``structural_depth``.

    Tiers (first match wins):

    - ``tier_3_prescriptive``    — narrative prescribes HOW to
      populate (format, assembly recipe, length/pattern spec).
      Vendor can build the value from the narrative alone.
    - ``tier_2_conceptual``      — narrative describes WHAT the
      element means without an assembly recipe. Vendor knows the
      intent but must decide the format.
    - ``tier_1_cross_reference`` — narrative points at another system
      / doc as the authority. Vendor has to follow the pointer.
    - ``tier_1_regulatory``      — narrative cites authority (statute
      / rule / code) without describing the field. Vendor has to
      read the citation.
    - ``tier_0_unspecified``     — no informational content in the
      narrative. Vendor reverse-engineers from the field name.
    - ``tier_0_unresolved``      — classifier fact missing or
      downgraded. Caller flags for review.

    Confidence tracks the classifier's LLM confidence; any downgrade
    demotes to ``"low"`` per the shared composite rule.
    """
    style = facts.enum("documentation_style")

    inputs = facts.snapshot(_DOC_STYLE_TIER_INPUTS)

    if style == "prescriptive":
        rule = "tier_3_prescriptive"
        value = 3
    elif style == "conceptual":
        rule = "tier_2_conceptual"
        value = 2
    elif style == "cross_reference":
        rule = "tier_1_cross_reference"
        value = 1
    elif style == "regulatory":
        rule = "tier_1_regulatory"
        value = 1
    elif style == "unspecified":
        rule = "tier_0_unspecified"
        value = 0
    else:
        rule = "tier_0_unresolved"
        value = 0

    return DimensionScore(
        name="documentation_style_tier",
        value=value,
        rule_matched=rule,
        inputs_used=inputs,
        confidence=_confidence_for_dim(facts, _DOC_STYLE_TIER_INPUTS),
    )


# ---------------------------------------------------------------------------
# Second-pass rule bodies — consume first-pass DimensionScores, not facts.
# ---------------------------------------------------------------------------


_DOC_GAP_INPUTS = (
    "structural_depth",
    "documentation_style_tier",
)


def _min_confidence_of_dims(dims: Sequence[DimensionScore]) -> str:
    """Min-across-dims confidence for the second-pass composite.

    Mirrors ``_confidence_for_dim`` but reads already-computed
    ``DimensionScore.confidence`` values rather than per-fact confidences.
    Empty sequence → ``"low"`` (a composite with no inputs is by
    definition not trustworthy).
    """
    if not dims:
        return "low"
    lowest = min(
        (_CONFIDENCE_ORDER.get(d.confidence, 0) for d in dims), default=0
    )
    return _CONFIDENCE_FROM_ORDER[lowest]


def documentation_gap(
    first_pass: Mapping[str, DimensionScore],
) -> DimensionScore:
    """Integration Profile — the documentation-gap signal (second pass).

    Plan ``docs/archive/nachos-v2-two-axis-plan.md`` §3.2 / Mitigation 3.
    Second-pass rule: unlike the fact-consuming rules above, this rule
    composes over the already-computed ``structural_depth`` and
    ``documentation_style_tier`` dimensions. It fires (value=1) when
    the spine says a row carries real integration weight (structural >=
    2) AND narrative says the state doesn't prescribe how to populate
    it (doc style tier <= 1). That's the exact "the vendor reverse-
    engineers what the state should have documented" pattern the v1
    NACHOS scorecard couldn't produce.

    Values (boolean-coded as 0/1 so ``dim_stats.distribution`` stays
    readable):

    - ``value=1`` (``tier_1_gap``) — structural >= 2 AND doc <= 1.
    - ``value=0`` (``tier_0_documented``) — structural >= 2 AND doc >= 2.
    - ``value=0`` (``tier_0_low_complexity``) — structural < 2. A scalar
      leaf on a flat entity doesn't need prescriptive docs regardless
      of the state's narrative style; not a gap.
    - ``value=None`` (``tier_0_unresolved``) — either input dimension is
      missing from the first-pass or its value is ``None``. The
      aggregate layer's review block catches this via the
      low-confidence-dimension trigger.

    Confidence: min of the two input dimensions' confidences. Downgrade
    on either axis propagates — this is a gap signal; a downgrade on
    either axis means the gap determination itself is uncertain.
    """
    struct = first_pass.get("structural_depth")
    doc = first_pass.get("documentation_style_tier")

    inputs: dict[str, Any] = {
        "structural_depth": struct.value if struct is not None else None,
        "documentation_style_tier": doc.value if doc is not None else None,
    }

    consulted: list[DimensionScore] = [d for d in (struct, doc) if d is not None]
    composite_confidence = _min_confidence_of_dims(consulted)

    if (
        struct is None
        or doc is None
        or struct.value is None
        or doc.value is None
    ):
        return DimensionScore(
            name="documentation_gap",
            value=None,
            rule_matched="tier_0_unresolved",
            inputs_used=inputs,
            confidence=composite_confidence,
        )

    if struct.value >= 2 and doc.value <= 1:
        rule = "tier_1_gap"
        value = 1
    elif struct.value >= 2:
        rule = "tier_0_documented"
        value = 0
    else:
        rule = "tier_0_low_complexity"
        value = 0

    return DimensionScore(
        name="documentation_gap",
        value=value,
        rule_matched=rule,
        inputs_used=inputs,
        confidence=composite_confidence,
    )


_NACHOS_INPUTS = (
    "has_aggregation",
    "has_concatenation",
    "has_cross_entity_logic",
    "cross_entity_targets",
    "has_conditional_logic",
    "descriptor_values_enumerated",
    "is_natural_key",
    # v16 — B5 (issue #63 POC interim call): bare FK references don't
    # carry NACHOS cross-entity logic by themselves. Read from
    # `element_is_bare_fk_reference` deterministic fact.
    "element_is_bare_fk_reference",
    # v22 — issue #97 (2026-04-30): FK-named rows whose only
    # conditional evidence is a parent-entity submission gate don't
    # carry NACHOS conditional logic by themselves. Suppresses the
    # tier_1_conditional contribution.
    "element_only_parent_entity_gate",
)


def _reconcile_cross_entity(facts: FactView) -> tuple[bool, int]:
    """Shared cross-entity reconciliation (Phase D carryover #6).

    Returns ``(reconciled_flag, target_count)``. ``reconciled_flag`` is
    True only when ``has_cross_entity_logic`` is True AND
    ``cross_entity_targets`` >= 1. Used by both
    ``business_logic_complexity`` and Phase F's ``nachos_score`` so the
    two rules stay in lockstep on what "real cross-entity logic" means.
    """
    raw_hce = facts.bool("has_cross_entity_logic")
    targets = facts.count("cross_entity_targets")
    return (raw_hce and targets >= 1, targets)


def nachos_score(facts: FactView) -> DimensionScore:
    """Phase F — methodology-conformant NACHOS tier 0..3.

    Distinct from ``business_logic_complexity`` (which is a
    comprehension-cost signal surfaced for the analyst's
    ``Complex Business Logic`` column). ``nachos_score`` implements
    the Dec 2025 methodology spec (``docs/NACHOS_Methodology_External
    review.xlsx``): tier 3 = CONCATENATE/SUM aggregation or
    transformation, tier 2 = multi-entity calculation without
    aggregation, tier 1 = single-entity conditional, tier 0 =
    granular-element or descriptor-value lookup.

    Inputs are shared with ``business_logic_complexity`` but the tier
    boundaries differ — this rule reads the same facts through a
    different rubric. It runs on both spine-lens and source-lens; on
    source-lens it's only meaningful for rows whose facts were
    extracted (see ``SOURCE_RULE_INPUTS``).

    Tiers (first-match-wins cascade):

    - ``tier_3_aggregation``          — ``has_aggregation`` → 3
      (SUM/COUNT/AVG — methodology example "aggregation of any type").
    - ``tier_0_natural_key_format``   — ``has_concatenation`` AND
      ``is_natural_key`` AND NOT aggregation → 0. A state-mandated
      composite format on a natural-key identifier (AZ ``CalendarCode``
      = ``LEAID-SchoolId-CalendarTypeCodeValue-Sequence``) is a
      key-format specification, not a CONCATENATE derivation. The LEA
      composes the key once and submits it; vendors pass it through.
      Scoring it tier 3 conflates format specs with derivations. Guard
      sits below aggregation (a key field that also aggregates is
      vanishingly rare; tier 3 stays the right call if both fire) and
      above the generic concatenation branch.
    - ``tier_3_concatenation``        — ``has_concatenation`` → 3
      (CONCAT/string-join — methodology example "CONCATENATE function").
    - ``tier_2_multi_entity``         — reconciled cross-entity logic AND
      ``cross_entity_targets`` >= 2 AND no aggregation/concatenation → 2
      (methodology: "Calculation logic with multiple entities without
      aggregation or transformation").
    - ``tier_1_conditional``          — single-entity conditional
      (``has_conditional_logic`` OR single-target cross-entity) → 1
      (methodology tier 0 "ONE if statement" conflated with tier 1
      "if statement within same entity" pending a count fact — see
      v2 candidates).
    - ``tier_0_descriptor``           — ``descriptor_values_enumerated``
      AND no positive logic signal → 0 (methodology tier 0
      "Send a descriptor value").
    - ``tier_0_none``                 — fallthrough → 0 (granular element,
      no logic).

    Ordering note: aggregation/concatenation check precedes the
    descriptor gate. A descriptor row that also carries aggregation
    logic (rare but possible) correctly scores tier 3.
    """
    has_agg = facts.bool("has_aggregation")
    has_concat = facts.bool("has_concatenation")
    is_nk = facts.bool("is_natural_key")
    hce, targets = _reconcile_cross_entity(facts)
    hcl = facts.bool("has_conditional_logic")
    dve = facts.bool("descriptor_values_enumerated")

    # v16 — B5 (issue #63 POC interim methodology call, 2026-04-29):
    # element-only scope for bare FK references. When the element is a
    # bare FK reference (element_name matches a known spine entity AND
    # no element-specific narrative), suppress the cross-entity-logic
    # contribution to the NACHOS tier. The FK-chain reach is a
    # structural property of the spine, not of the element's value or
    # reporting obligation. Element-level narrative cross-entity logic
    # (LLM-extracted via has_conditional_logic) is unaffected.
    # Authority: docs/adr/0001-issue-63-methodology-calls.md (B5).
    if facts.bool("element_is_bare_fk_reference"):
        hce = False
        targets = 0

    # v22 — issue #97 (2026-04-30): suppress has_conditional_logic on
    # FK-named rows whose only conditional evidence is the parent-
    # entity submission gate annotation. The conditional applies to
    # the parent record's submission, not to the FK reference's own
    # value or presence. Element-level conditional logic surfaced by a
    # non-gate span (a value rule, a presence-by-population rule) is
    # unaffected because the suppressing fact requires both the FK
    # name shape and an empty element_specific_rules — element-scoped
    # rules disable the suppression.
    if facts.bool("element_only_parent_entity_gate"):
        hcl = False

    inputs = facts.snapshot(_NACHOS_INPUTS)
    inputs["has_cross_entity_logic__reconciled"] = hce
    inputs["has_conditional_logic__reconciled"] = hcl

    if has_agg:
        rule = "tier_3_aggregation"
        value = 3
    elif has_concat and is_nk:
        rule = "tier_0_natural_key_format"
        value = 0
    elif has_concat:
        rule = "tier_3_concatenation"
        value = 3
    elif hce and targets >= 2:
        rule = "tier_2_multi_entity"
        value = 2
    elif hcl or (hce and targets == 1):
        rule = "tier_1_conditional"
        value = 1
    elif dve:
        rule = "tier_0_descriptor"
        value = 0
    else:
        rule = "tier_0_none"
        value = 0

    return DimensionScore(
        name="nachos_score",
        value=value,
        rule_matched=rule,
        inputs_used=inputs,
        confidence=_confidence_for_dim(facts, _NACHOS_INPUTS),
    )


_SPINE_RULES = (
    documentation_completeness,
    obligation_clarity,
    business_logic_complexity,
    structural_depth,
    documentation_style_tier,
    nachos_score,
)


# ---------------------------------------------------------------------------
# Source-lens rule bodies (plan §7.1)
# ---------------------------------------------------------------------------


_CNA_INPUTS = (
    "element_name_matches_canonical",
    "naming_deviation_cosmetic",
)


def canonical_name_alignment(facts: FactView) -> DimensionScore:
    """Plan §7.1 — source-lens canonical_name_alignment.

    Tiers (first match wins):

    - ``tier_3_exact``     — element resolved to a spine slot and its
      name matches canonical exactly.
    - ``tier_2_cosmetic``  — core row whose name differs from canonical
      only in casing. Extensions intentionally rename; tier 2 stays
      core-only so extension cosmetic drift doesn't mask real intent.
    - ``tier_1_resolved``  — matched to a spine slot but neither exact
      nor cosmetic (rename).
    - ``tier_0_unresolved`` — source=unknown (no canonical to match).
    """
    exact = facts.bool("element_name_matches_canonical")
    cosmetic = facts.bool("naming_deviation_cosmetic")

    inputs = facts.snapshot(_CNA_INPUTS)
    inputs["source"] = facts.source

    if facts.source in ("core", "extension") and exact:
        rule = "tier_3_exact"
        value = 3
    elif facts.source == "core" and cosmetic:
        rule = "tier_2_cosmetic"
        value = 2
    elif facts.source in ("core", "extension"):
        rule = "tier_1_resolved"
        value = 1
    else:
        rule = "tier_0_unresolved"
        value = 0

    return DimensionScore(
        name="canonical_name_alignment",
        value=value,
        rule_matched=rule,
        inputs_used=inputs,
        confidence=_confidence_for_dim(facts, _CNA_INPUTS),
    )


_DQ_INPUTS = (
    "definition_present",
    "definition_adds_detail_beyond_edfi",
    "definition_text_substantive",
)


def definition_quality(facts: FactView) -> DimensionScore:
    """Plan §7.1 — source-lens definition_quality.

    Tiers (first match wins):

    - ``tier_3_detail_beyond_edfi`` — definition present AND the LLM
      confirmed the state adds detail beyond the Ed-Fi standard.
    - ``tier_2_substantive``        — definition present AND at least
      60 chars (``definition_text_substantive``); treats depth as a
      proxy for quality when the LLM didn't confirm detail.
    - ``tier_1_minimal``            — definition present but thin.
    - ``tier_0_missing``            — no definition.
    """
    dp = facts.bool("definition_present")
    dad = facts.bool("definition_adds_detail_beyond_edfi")
    substantive = facts.bool("definition_text_substantive")

    inputs = facts.snapshot(_DQ_INPUTS)

    if dp and dad:
        rule = "tier_3_detail_beyond_edfi"
        value = 3
    elif dp and substantive:
        rule = "tier_2_substantive"
        value = 2
    elif dp:
        rule = "tier_1_minimal"
        value = 1
    else:
        rule = "tier_0_missing"
        value = 0

    return DimensionScore(
        name="definition_quality",
        value=value,
        rule_matched=rule,
        inputs_used=inputs,
        confidence=_confidence_for_dim(facts, _DQ_INPUTS),
    )


_SF_INPUTS = (
    "semantic_class",
    # Side-quest PR A (2026-04-28): merged from `state_narrows_edfi_scope`
    # + `state_broadens_edfi_scope`. The XOR test below collapses to a
    # single enum check (`narrows` or `broadens` triggers tier 2).
    "state_scope_delta",
)


def semantic_fidelity(facts: FactView) -> DimensionScore:
    """Plan §7.1 — source-lens semantic_fidelity.

    Tiers (first match wins):

    - ``tier_3_aligned``             — ``semantic_class == aligned``.
    - ``tier_2_divergent_explained`` — ``state_scope_delta`` is
      ``narrows`` or ``broadens``: the direction of divergence is
      explained. (Pre-PR-A this was an XOR over two separate booleans;
      the merged enum is mutually exclusive by construction so the
      check is a single membership test.)
    - ``tier_1_divergent_unclear``   — ``semantic_class == divergent``
      without a clear narrows/broadens signal.
    - ``null_not_applicable``        — ``semantic_class == not_applicable``
      (e.g., extension with no core anchor). Value is ``None`` so the
      aggregate layer excludes the dimension from the record's mean
      rather than scoring a zero it couldn't have reached.
    - ``tier_0_unresolved``          — semantic_class missing /
      downgraded. Caller flags for review.
    """
    sc = facts.enum("semantic_class")
    scope_delta = facts.enum("state_scope_delta")

    inputs = facts.snapshot(_SF_INPUTS)

    if sc == "aligned":
        return DimensionScore(
            name="semantic_fidelity",
            value=3,
            rule_matched="tier_3_aligned",
            inputs_used=inputs,
            confidence=_confidence_for_dim(facts, _SF_INPUTS),
        )
    if scope_delta in ("narrows", "broadens"):
        return DimensionScore(
            name="semantic_fidelity",
            value=2,
            rule_matched="tier_2_divergent_explained",
            inputs_used=inputs,
            confidence=_confidence_for_dim(facts, _SF_INPUTS),
        )
    if sc == "divergent":
        return DimensionScore(
            name="semantic_fidelity",
            value=1,
            rule_matched="tier_1_divergent_unclear",
            inputs_used=inputs,
            confidence=_confidence_for_dim(facts, _SF_INPUTS),
        )
    if sc == "not_applicable":
        return DimensionScore(
            name="semantic_fidelity",
            value=None,
            rule_matched="null_not_applicable",
            inputs_used=inputs,
            confidence=_confidence_for_dim(facts, _SF_INPUTS),
        )
    return DimensionScore(
        name="semantic_fidelity",
        value=0,
        rule_matched="tier_0_unresolved",
        inputs_used=inputs,
        confidence=_confidence_for_dim(facts, _SF_INPUTS),
    )


_EJ_INPUTS = (
    "extension_is_necessary",
    "extension_is_standalone",
    "extension_mirrors_core_pattern",
)


def extension_justification(facts: FactView) -> DimensionScore:
    """Plan §7.1 — source-lens extension_justification.

    Evaluated only for ``source == "extension"`` rows; core and
    unknown rows return ``value=None`` + ``rule_matched="not_applicable"``
    so the aggregate layer excludes the dimension from the per-record
    mean (not a zero — the dimension genuinely doesn't apply).

    Tiers (first match wins) for extension rows:

    - ``tier_0_unnecessary_mirror`` — LLM says the extension is not
      necessary AND deterministic fact says its name mirrors a core
      pattern. Strongest unnecessary signal.
    - ``tier_3_necessary_standalone`` — LLM says necessary AND
      standalone (self-contained design).
    - ``tier_2_necessary_companion`` — LLM says necessary but not
      standalone (depends on other extensions).
    - ``tier_1_partial``            — fallback when necessity/
      standalone claims are partial or downgraded.
    """
    if facts.source != "extension":
        return DimensionScore(
            name="extension_justification",
            value=None,
            rule_matched="not_applicable",
            inputs_used={"source": facts.source},
            confidence="high",
        )

    necessary = facts.bool("extension_is_necessary")
    standalone = facts.bool("extension_is_standalone")
    mirrors = facts.bool("extension_mirrors_core_pattern")

    inputs = facts.snapshot(_EJ_INPUTS)

    if not necessary and mirrors:
        rule = "tier_0_unnecessary_mirror"
        value = 0
    elif necessary and standalone:
        rule = "tier_3_necessary_standalone"
        value = 3
    elif necessary and not standalone:
        rule = "tier_2_necessary_companion"
        value = 2
    else:
        rule = "tier_1_partial"
        value = 1

    return DimensionScore(
        name="extension_justification",
        value=value,
        rule_matched=rule,
        inputs_used=inputs,
        confidence=_confidence_for_dim(facts, _EJ_INPUTS),
    )


_SOURCE_RULES = (
    canonical_name_alignment,
    definition_quality,
    semantic_fidelity,
    extension_justification,
    business_logic_complexity,
    structural_depth,
    documentation_style_tier,
    nachos_score,
)

# Integration Profile — rules that compose over first-pass DimensionScore
# values rather than the per-record fact pool. Run AFTER the first-pass
# tuple for each lens so their input dimensions are already computed.
# ``score_record`` guarantees execution order: first-pass rules land in
# the return list first, second-pass rules append at the end.
_SECOND_PASS_RULES = (documentation_gap,)


def score_record(facts: FactView, lens: str = "spine") -> list[DimensionScore]:
    """Run every rule for ``lens`` against one record's FactView.

    Two-phase execution (Integration Profile): first-pass rules consume
    the FactView only; second-pass rules consume the first-pass
    DimensionScore dict. Second-pass rules live in ``_SECOND_PASS_RULES``
    and run against both lenses — the ``documentation_gap`` signal
    composes over dimensions that exist on both rosters.

    ``lens="spine"`` runs the six first-pass spine rules; ``lens=
    "source"`` runs the seven first-pass source rules. Second-pass
    rules append at the end of the return list so existing consumers
    that index into first-pass positions keep working.
    """
    if lens == "spine":
        first_rules = _SPINE_RULES
    elif lens == "source":
        first_rules = _SOURCE_RULES
    else:
        raise ValueError(f"unknown lens {lens!r}; supported: 'spine', 'source'")
    first_pass = [rule(facts) for rule in first_rules]
    first_pass_map: dict[str, DimensionScore] = {d.name: d for d in first_pass}
    second_pass = [rule(first_pass_map) for rule in _SECOND_PASS_RULES]
    return [*first_pass, *second_pass]


# ---------------------------------------------------------------------------
# Fact pool loader — reads extraction JSONL artifacts
# ---------------------------------------------------------------------------


def _iter_artifact_rows(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read one fact artifact JSONL → (header, rows). Empty file → empty."""
    if not path.exists():
        return {}, []
    lines = [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    if not lines:
        return {}, []
    header = json.loads(lines[0])
    rows = [json.loads(ln) for ln in lines[1:]]
    return header, rows


def _row_to_fact_result(fact: str, row: dict[str, Any]) -> FactResult:
    """Convert one artifact row into a ``FactResult``.

    Handles both LLM-path rows (with ``any_invalid_spans``,
    ``downgrade_reason``) and deterministic-path rows (no
    ``any_invalid_spans`` key, ``downgrade_reason=None``).

    ``spans`` carries string entries. Deterministic-path rows store
    spans as ``list[str]``; LLM-path rows store spans as
    ``list[dict]`` (``{"text": ..., "valid": ...}``). Only entries
    with ``valid=True`` flow through — hallucinated spans the
    substring validator rejected are dropped. Callers see a
    homogeneous ``tuple[str, ...]`` regardless of the source shape, so
    the aggregate layer's ``fact_provenance[fact].spans`` surfaces
    verbatim evidence for productization-signal facts (e.g.
    ``integration_class``) alongside the deterministic
    ``descriptor_values_enumerated`` match text.
    """
    validated = row.get("validated_value")
    downgrade_reason = row.get("downgrade_reason")
    confidence = row.get("confidence", "low")
    downgraded = validated is None and (
        downgrade_reason is not None or row.get("llm_value") is not None
    )
    raw_spans = row.get("spans") or ()
    span_texts: list[str] = []
    for s in raw_spans:
        if isinstance(s, str):
            span_texts.append(s)
        elif (
            isinstance(s, dict)
            and s.get("valid") is True
            and isinstance(s.get("text"), str)
        ):
            span_texts.append(s["text"])
    spans = tuple(span_texts)
    return FactResult(
        fact=fact,
        value=validated,
        confidence=confidence,
        downgraded=downgraded,
        downgrade_reason=downgrade_reason,
        spans=spans,
    )


def _load_record_meta(
    state: str, lens: str
) -> dict[str, tuple[str, str | None, bool, str]]:
    """Load ``record_key -> (source, extension_name, documented, documentation_source)``.

    Reads the enriched elements JSON (the same artifact the scorer
    loaded records from) and builds a lookup keyed by the same
    ``{state}|{entity}|{element_name}`` form the fact artifacts use.
    Missing files surface as an empty dict; callers fall back to
    ``("unknown", None, False, "source_doc")`` — the safe interpretation
    for rule bodies that gate on these attributes.

    ``documented`` and ``documentation_source`` are surfaced on the
    ``FactView`` so aggregate-time headline computations can exclude
    swagger-backfilled rows from coverage / mean NACHOS without losing
    per-row scoring (issue #70 v21 close-out posture).
    """
    from src.models.element import StateElements
    from src.utils.paths import state_elements_path

    path = state_elements_path(state.upper(), lens)  # type: ignore[arg-type]
    if not path.exists():
        return {}
    elements = StateElements.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    meta: dict[str, tuple[str, str | None, bool, str]] = {}
    for r in elements.elements:
        key = f"{state.upper()}|{r.entity}|{r.element_name}"
        meta.setdefault(
            key,
            (
                r.source,
                r.extension_name,
                r.documented,
                getattr(r, "documentation_source", "source_doc"),
            ),
        )
    return meta


def load_fact_pool(
    state: str,
    lens: str = "spine",
    *,
    facts: Sequence[str] | None = None,
    artifacts_dir: Path | None = None,
) -> dict[str, FactView]:
    """Load every fact artifact for (state, lens) and return per-record views.

    The return dict is keyed by ``record_key`` (``{state}|{entity}|
    {element_name}``). Each value is a ``FactView`` carrying every fact
    found on disk for that record plus the record's ingest-level
    ``source`` / ``extension_name`` (loaded from the state-elements
    JSON) so rule bodies can gate on extension-vs-core without
    re-reading the record.

    Raises ``FileNotFoundError`` if no artifact exists for a requested
    fact; the aggregate layer treats a missing fact as a run-prerequisite
    failure, not an in-rule condition.
    """
    if lens == "spine":
        default_inputs = SPINE_RULE_INPUTS
    elif lens == "source":
        default_inputs = SOURCE_RULE_INPUTS
    else:
        raise ValueError(f"unknown lens {lens!r}; supported: 'spine', 'source'")

    if facts is not None:
        wanted = tuple(facts)
        # Explicit override — every requested fact is a hard
        # prerequisite, matching the pre-H1 contract.
        optional_facts: frozenset[str] = frozenset()
    else:
        # Default behaviour loads rule inputs (hard prerequisite) plus
        # productization-signal facts and per-lens observability facts
        # (both optional — missing artifacts log a warning and the
        # fact is omitted from the pool, rather than failing the run).
        # See ``rules.PRODUCTIZATION_SIGNAL_FACTS`` and
        # ``rules.LENS_OBSERVABILITY_FACTS`` for the rationale.
        lens_obs = LENS_OBSERVABILITY_FACTS.get(lens, ())
        wanted = tuple(sorted(
            set(default_inputs)
            | set(PRODUCTIZATION_SIGNAL_FACTS)
            | set(lens_obs)
        ))
        optional_facts = frozenset(PRODUCTIZATION_SIGNAL_FACTS) | frozenset(lens_obs)

    # fact → { record_key → row }
    per_fact: dict[str, dict[str, dict[str, Any]]] = {}
    # record_key → (entity, element_name)
    record_meta: dict[str, tuple[str, str]] = {}

    loaded: list[str] = []
    for fact in wanted:
        if artifacts_dir is not None:
            path = artifacts_dir / f"{state.upper()}_{lens}_{fact}.jsonl"
        else:
            path = scoring_phase_a_artifact_path(state, fact, lens=lens)
        if not path.exists():
            if fact in optional_facts:
                # Productization-signal fact artifact not present yet;
                # aggregate proceeds without the signal. A later
                # re-extract can populate it. Log once so the omission
                # is visible in the run logs.
                _LOGGER.warning(
                    "productization-signal fact %r artifact missing for "
                    "(%s, %s): %s — aggregate proceeds without the signal",
                    fact, state.upper(), lens, path,
                )
                continue
            raise FileNotFoundError(
                f"fact artifact missing for ({state}, {fact}): {path}. "
                f"Populate it with "
                f"`mc score extract --state {state.upper()} --lens {lens} "
                f"--fact {fact} --cost-cap 3.0` "
                f"(or `mc score run-all --state {state.upper()} --lens {lens}` "
                f"to fan out every fact), then rerun aggregate."
            )
        loaded.append(fact)
        header, rows = _iter_artifact_rows(path)
        if header.get("state", state.upper()) != state.upper():
            raise ValueError(
                f"artifact {path} has state={header.get('state')!r}, expected {state.upper()!r}"
            )
        per_fact[fact] = {}
        for row in rows:
            key = row["record_key"]
            per_fact[fact][key] = row
            if key not in record_meta:
                record_meta[key] = (row["entity"], row["element_name"])

    source_by_key = _load_record_meta(state, lens)

    # Facts whose prompt only applies to rows with specific ``source``
    # values — a record correctly missing from the artifact because the
    # extractor filtered it out should not surface as a downgrade. Import
    # locally to avoid a cycle with ``score.schema``.
    from src.score.schema import FACT_SOURCE_FILTERS

    views: dict[str, FactView] = {}
    for key, (entity, element_name) in record_meta.items():
        source, extension_name, documented, documentation_source = source_by_key.get(
            # Default ``documented=True`` when the elements file is
            # absent (test fixtures, alternative artifact roots) — the
            # absence of meta means we can't tell, and the safe
            # interpretation that preserves pre-v21 aggregation behavior
            # is "this row counts toward headline stats." Real data has
            # an elements file alongside the artifacts, so this default
            # only fires for unit tests that seed JSONL pools without
            # a sibling elements file.
            key, ("unknown", None, True, "source_doc")
        )
        fact_results: dict[str, FactResult] = {}
        for fact in loaded:
            row = per_fact[fact].get(key)
            if row is None:
                allowed_sources = FACT_SOURCE_FILTERS.get(fact)
                if allowed_sources is not None and source not in allowed_sources:
                    # Extraction intentionally skipped this row — the
                    # fact doesn't apply to its source. Null value, no
                    # downgrade flag, high confidence (the filter is
                    # a policy decision, not uncertainty).
                    fact_results[fact] = FactResult(
                        fact=fact,
                        value=None,
                        confidence="high",
                        downgraded=False,
                        downgrade_reason="filtered_by_source",
                    )
                    continue
                # Fact artifact exists but this record missing from it —
                # treat as downgraded so confidence composite demotes.
                fact_results[fact] = FactResult(
                    fact=fact,
                    value=None,
                    confidence="low",
                    downgraded=True,
                    downgrade_reason="missing_from_artifact",
                )
                continue
            fact_results[fact] = _row_to_fact_result(fact, row)
        views[key] = FactView(
            record_key=key,
            entity=entity,
            element_name=element_name,
            facts=fact_results,
            source=source,
            extension_name=extension_name,
            documented=documented,
            documentation_source=documentation_source,
        )
    return views


def score_state(
    state: str,
    lens: str = "spine",
    *,
    artifacts_dir: Path | None = None,
) -> list[tuple[FactView, list[DimensionScore]]]:
    """Run the rule stage across every record for (state, lens).

    Returns ``[(view, [DimensionScore])]`` in the natural key order
    supplied by ``load_fact_pool`` (insertion order = artifact row order
    = deterministic entity/element_name sort).
    """
    pool = load_fact_pool(state, lens, artifacts_dir=artifacts_dir)
    return [(view, score_record(view, lens=lens)) for view in pool.values()]
