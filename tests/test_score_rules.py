"""Phase C rule-stage unit tests — boundary cases per dimension.

Covers:

- Per-dimension tier boundaries (3/2/1/0) using ``FactView`` directly.
- ``descriptor_values_enumerated`` gate behavior on
  ``documentation_completeness`` (OR-path for implementable) and
  ``obligation_clarity`` (demotes fact-4).
- Cross-fact reconciliation on ``business_logic_complexity``
  (has_cross_entity_logic without a supporting target count).
- Confidence composite: downgraded input forces low.
- ``load_fact_pool`` reads the committed Phase B artifacts and produces
  a non-empty pool for every state — smoke test against real data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.score.rules import (
    DimensionScore,
    FactResult,
    FactView,
    SOURCE_DIMENSIONS,
    SOURCE_RULE_INPUTS,
    SPINE_DIMENSIONS,
    SPINE_RULE_INPUTS,
    business_logic_complexity,
    canonical_name_alignment,
    definition_quality,
    documentation_completeness,
    documentation_style_tier,
    extension_justification,
    load_fact_pool,
    nachos_score,
    obligation_clarity,
    score_record,
    semantic_fidelity,
    documentation_gap,
)


def _view(*, source: str = "core", extension_name: str | None = None, **facts: tuple) -> FactView:
    """Construct a FactView from {fact: (value, confidence[, downgraded])} shorthand."""
    results: dict[str, FactResult] = {}
    for name, spec in facts.items():
        if len(spec) == 2:
            value, confidence = spec
            downgraded = value is None
            reason = "hallucinated_span" if downgraded else None
        else:
            value, confidence, downgraded = spec  # type: ignore[misc]
            reason = "hallucinated_span" if downgraded else None
        results[name] = FactResult(
            fact=name,
            value=value,
            confidence=confidence,
            downgraded=downgraded,
            downgrade_reason=reason,
        )
    return FactView(
        record_key="TEST|Entity|elem",
        entity="Entity",
        element_name="elem",
        facts=results,
        source=source,
        extension_name=extension_name,
    )


# ---------------------------------------------------------------------------
# documentation_completeness
# ---------------------------------------------------------------------------


class TestDocumentationCompleteness:
    def test_tier_3_full(self) -> None:
        v = _view(
            definition_present=(True, "high"),
            business_rules_present=(True, "high"),
            data_type_canonical=(True, "high"),
            definition_is_implementable=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = documentation_completeness(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_full"
        assert score.confidence == "high"

    def test_tier_3_descriptor_enum_substitutes_implementable(self) -> None:
        """Descriptor-enum gate supplies the implementability signal when
        the LLM downgraded fact-1 (WI hallucination FP closure)."""
        v = _view(
            definition_present=(True, "high"),
            business_rules_present=(True, "high"),
            data_type_canonical=(True, "high"),
            definition_is_implementable=(None, "medium", True),
            descriptor_values_enumerated=(True, "high"),
        )
        score = documentation_completeness(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_descriptor_enum"
        # Downgraded input forces confidence to low.
        assert score.confidence == "low"

    def test_tier_2_structure_no_implementable(self) -> None:
        v = _view(
            definition_present=(True, "high"),
            business_rules_present=(True, "high"),
            data_type_canonical=(True, "high"),
            definition_is_implementable=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = documentation_completeness(v)
        assert score.value == 2
        assert score.rule_matched == "tier_2_structure"

    def test_tier_1_minimal(self) -> None:
        v = _view(
            definition_present=(True, "high"),
            business_rules_present=(False, "high"),
            data_type_canonical=(True, "high"),
            definition_is_implementable=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = documentation_completeness(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_minimal"

    def test_tier_0_missing_definition(self) -> None:
        v = _view(
            definition_present=(False, "high"),
            business_rules_present=(True, "high"),
            data_type_canonical=(True, "high"),
            definition_is_implementable=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = documentation_completeness(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_missing"

    def test_inputs_used_records_every_input(self) -> None:
        v = _view(
            definition_present=(True, "high"),
            business_rules_present=(True, "high"),
            data_type_canonical=(True, "high"),
            definition_is_implementable=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = documentation_completeness(v)
        assert set(score.inputs_used) == {
            "definition_present",
            "business_rules_present",
            "data_type_canonical",
            "definition_is_implementable",
            "descriptor_values_enumerated",
        }


# ---------------------------------------------------------------------------
# obligation_clarity
# ---------------------------------------------------------------------------


class TestObligationClarity:
    def test_tier_3_required_scoped(self) -> None:
        v = _view(
            required_when_stated=(True, "high"),
            populations_or_scope_stated=(True, "high"),
            conditional_reporting_stated=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = obligation_clarity(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_required_scoped"

    def test_tier_2_conditional(self) -> None:
        v = _view(
            required_when_stated=(False, "high"),
            populations_or_scope_stated=(False, "high"),
            conditional_reporting_stated=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = obligation_clarity(v)
        assert score.value == 2
        assert score.rule_matched == "tier_2_conditional"

    def test_tier_1_partial_populations_only(self) -> None:
        v = _view(
            required_when_stated=(False, "high"),
            populations_or_scope_stated=(True, "high"),
            conditional_reporting_stated=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = obligation_clarity(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_partial"

    def test_tier_0_none(self) -> None:
        v = _view(
            required_when_stated=(False, "high"),
            populations_or_scope_stated=(False, "high"),
            conditional_reporting_stated=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = obligation_clarity(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_none"

    def test_descriptor_enum_demotes_fact_4(self) -> None:
        """MN program-type soft-FP cluster: enumerated value-set
        language that scored True on populations_or_scope_stated must
        not inflate obligation_clarity."""
        v = _view(
            required_when_stated=(False, "high"),
            populations_or_scope_stated=(True, "high"),
            conditional_reporting_stated=(False, "high"),
            descriptor_values_enumerated=(True, "high"),
        )
        score = obligation_clarity(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_descriptor_enum"

    def test_descriptor_enum_does_not_affect_required_when(self) -> None:
        """Gate only demotes fact-4; fact-5 (required_when_stated) still
        contributes."""
        v = _view(
            required_when_stated=(True, "high"),
            populations_or_scope_stated=(True, "high"),
            conditional_reporting_stated=(False, "high"),
            descriptor_values_enumerated=(True, "high"),
        )
        score = obligation_clarity(v)
        # populations demoted → fell through to tier_1_partial via rws only
        assert score.value == 1
        assert score.rule_matched == "tier_1_partial"


# ---------------------------------------------------------------------------
# business_logic_complexity
# ---------------------------------------------------------------------------


class TestBusinessLogicComplexity:
    def test_tier_3_agg_cross(self) -> None:
        v = _view(
            has_aggregation=(True, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(2, "high"),
            has_conditional_logic=(False, "high"),
        )
        score = business_logic_complexity(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_agg_cross"

    def test_tier_2_agg_only(self) -> None:
        v = _view(
            has_aggregation=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
        )
        score = business_logic_complexity(v)
        assert score.value == 2
        assert score.rule_matched == "tier_2_agg_or_multicross"

    def test_tier_2_multicross(self) -> None:
        v = _view(
            has_aggregation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(3, "high"),
            has_conditional_logic=(False, "high"),
        )
        score = business_logic_complexity(v)
        assert score.value == 2
        assert score.rule_matched == "tier_2_agg_or_multicross"

    def test_tier_1_single_cross(self) -> None:
        v = _view(
            has_aggregation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(1, "high"),
            has_conditional_logic=(False, "high"),
        )
        score = business_logic_complexity(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_cond_or_cross"

    def test_tier_1_conditional_only(self) -> None:
        v = _view(
            has_aggregation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(True, "high"),
        )
        score = business_logic_complexity(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_cond_or_cross"

    def test_reconcile_cross_without_targets(self) -> None:
        """Phase D carryover #6 reconciliation: fact-5 True with count=0
        demotes to False."""
        v = _view(
            has_aggregation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
        )
        score = business_logic_complexity(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_none"
        # Reconciled value is surfaced in inputs_used for audit
        assert score.inputs_used["has_cross_entity_logic__reconciled"] is False

    def test_tier_0_none(self) -> None:
        v = _view(
            has_aggregation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
        )
        score = business_logic_complexity(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_none"


# ---------------------------------------------------------------------------
# nachos_score — Phase F methodology-conformant NACHOS tier 0..3
# ---------------------------------------------------------------------------


class TestNachosScore:
    """Methodology-conformant NACHOS rule (Phase F).

    Shares inputs with ``business_logic_complexity`` but applies
    different tier boundaries. The two rules must coexist (Phase F
    plan §2 decision 1): ``business_logic_complexity`` is a
    comprehension-cost signal for the analyst Complex Business Logic
    column, ``nachos_score`` is the methodology NACHOS number.
    """

    def test_tier_3_aggregation(self) -> None:
        v = _view(
            has_aggregation=(True, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_aggregation"

    def test_tier_3_concatenation(self) -> None:
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_concatenation"

    def test_tier_3_aggregation_wins_over_concatenation(self) -> None:
        """Both signals present → aggregation fires first (methodology
        treats both as tier 3; cascade order is cosmetic but stable)."""
        v = _view(
            has_aggregation=(True, "high"),
            has_concatenation=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_aggregation"

    def test_tier_0_natural_key_format_guards_concatenation(self) -> None:
        """Doug's review case — AZ ``Calendar.CalendarCode`` submits
        ``LEAID-SchoolId-CalendarTypeCodeValue-Sequence`` as its value.
        The LLM fact-extractor correctly flags ``has_concatenation=True``
        (prose describes a composite field built from parts), but the
        field is a natural-key identifier. The guard downgrades to
        tier 0 so a state-mandated key-format spec doesn't score the
        same as a real CONCATENATE derivation."""
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(True, "high"),
            is_natural_key=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_natural_key_format"

    def test_natural_key_without_concatenation_does_not_short_circuit(self) -> None:
        """The guard only fires when BOTH has_concatenation and
        is_natural_key are True. A natural-key element that doesn't
        carry concatenation falls through to the rest of the cascade
        normally (e.g., a key with a conditional rule scores tier 1)."""
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            is_natural_key=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_conditional"

    def test_concatenation_on_non_natural_key_still_tier_3(self) -> None:
        """Regression guard — the classic tier-3 concatenation case
        (racialEthnicCode-style: '6-position field made up of race codes
        concatenated alphabetically') on a non-identity element stays
        tier 3."""
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(True, "high"),
            is_natural_key=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_concatenation"

    def test_aggregation_still_wins_on_natural_key(self) -> None:
        """Edge case — a natural-key column that also aggregates (rare;
        essentially synthetic) still scores tier 3. The guard sits
        below aggregation in the cascade by design."""
        v = _view(
            has_aggregation=(True, "high"),
            has_concatenation=(True, "high"),
            is_natural_key=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_aggregation"

    def test_tier_2_multi_entity(self) -> None:
        """Reconciled cross-entity logic with 2+ targets, no agg/concat."""
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(2, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 2
        assert score.rule_matched == "tier_2_multi_entity"

    def test_tier_2_multi_entity_demoted_by_aggregation(self) -> None:
        """Aggregation trumps multi-entity — tier 3 wins."""
        v = _view(
            has_aggregation=(True, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(3, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_aggregation"

    def test_tier_1_single_entity_cross(self) -> None:
        """Single-target cross-entity logic scores tier 1."""
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(1, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_conditional"

    def test_tier_1_conditional_only(self) -> None:
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_conditional"

    def test_tier_0_descriptor(self) -> None:
        """descriptor_values_enumerated with no positive logic → tier 0
        descriptor (methodology 'Send a descriptor value')."""
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(True, "high"),
        )
        score = nachos_score(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_descriptor"

    def test_tier_0_descriptor_demoted_by_aggregation(self) -> None:
        """Rare: descriptor row that also carries aggregation logic —
        tier 3 wins (aggregation check precedes descriptor gate per plan
        §3)."""
        v = _view(
            has_aggregation=(True, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(True, "high"),
        )
        score = nachos_score(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_aggregation"

    def test_tier_0_none_granular(self) -> None:
        """Fallthrough: no logic signals, no descriptor → tier 0 granular."""
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_none"

    def test_reconcile_cross_without_targets(self) -> None:
        """hce=True with targets=0 reconciles to False — no tier 2,
        falls through to tier_0_none unless another signal fires."""
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_none"
        assert score.inputs_used["has_cross_entity_logic__reconciled"] is False

    def test_inputs_used_includes_nachos_fact_set(self) -> None:
        v = _view(
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        assert {
            "has_aggregation",
            "has_concatenation",
            "has_cross_entity_logic",
            "cross_entity_targets",
            "has_conditional_logic",
            "descriptor_values_enumerated",
        }.issubset(set(score.inputs_used))

    def test_downgraded_input_forces_low_confidence(self) -> None:
        v = _view(
            has_aggregation=(True, "high"),
            has_concatenation=(None, "high", True),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = nachos_score(v)
        # Aggregation still fires (not the downgraded fact) but
        # confidence composite drops because has_concatenation
        # downgraded.
        assert score.value == 3
        assert score.confidence == "low"


# ---------------------------------------------------------------------------
# Confidence composite
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_min_confidence_propagates(self) -> None:
        v = _view(
            definition_present=(True, "high"),
            business_rules_present=(True, "high"),
            data_type_canonical=(True, "medium"),
            definition_is_implementable=(True, "low"),
            descriptor_values_enumerated=(False, "high"),
        )
        score = documentation_completeness(v)
        assert score.confidence == "low"

    def test_downgraded_forces_low(self) -> None:
        v = _view(
            definition_present=(True, "high"),
            business_rules_present=(True, "high"),
            data_type_canonical=(True, "high"),
            definition_is_implementable=(None, "high", True),
            descriptor_values_enumerated=(False, "high"),
        )
        score = documentation_completeness(v)
        assert score.confidence == "low"


# ---------------------------------------------------------------------------
# structural_depth (Integration Profile)
# ---------------------------------------------------------------------------


class TestStructuralComplexity:
    """Cascade tests for the v2 structural-complexity dimension.

    Each test supplies the six structural facts directly to isolate the
    tier boundaries from upstream deterministic computation. Integration
    coverage lives in ``test_score_deterministic.TestStructuralFactsAgainstRealSpine``.
    """

    def _view_struct(self, **overrides: tuple) -> FactView:
        """All six structural facts at zero-signal default; override to lift."""
        defaults: dict[str, tuple] = {
            "is_natural_key": (False, "high"),
            "fk_chain_depth": (0, "high"),
            "reference_fan_out": (0, "high"),
            "sub_collection_depth": (0, "high"),
            "descriptor_enum_breadth": (0, "high"),
            "entity_extension_footprint": (0, "high"),
        }
        defaults.update(overrides)
        return _view(**defaults)

    def test_tier_0_flat_no_signal(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(self._view_struct())
        assert score.value == 0
        assert score.rule_matched == "tier_0_flat"
        assert score.name == "structural_depth"

    def test_tier_1_natural_key_alone(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(is_natural_key=(True, "high"))
        )
        assert score.value == 1
        assert score.rule_matched == "tier_1_natural_key"

    def test_tier_1_fk_chain_depth_one(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(fk_chain_depth=(1, "high"))
        )
        assert score.value == 1
        assert score.rule_matched == "tier_1_fk_chain"

    def test_tier_1_sub_collection_alone(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(sub_collection_depth=(1, "high"))
        )
        assert score.value == 1
        assert score.rule_matched == "tier_1_sub_collection"

    def test_tier_1_descriptor_small_breadth(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(descriptor_enum_breadth=(5, "high"))
        )
        assert score.value == 1
        assert score.rule_matched == "tier_1_descriptor_breadth"

    def test_tier_1_extension_present(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(entity_extension_footprint=(1, "high"))
        )
        assert score.value == 1
        assert score.rule_matched == "tier_1_extension_present"

    def test_tier_2_medium_fk_chain(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(fk_chain_depth=(2, "high"))
        )
        assert score.value == 2
        assert score.rule_matched == "tier_2_medium_fk_chain"

    def test_tier_2_fan_out_without_natural_key(self) -> None:
        """Moderate fan-out alone registers tier 2 — a non-identity
        field on a fanned-out-against entity still carries some weight."""
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(reference_fan_out=(3, "high"))
        )
        assert score.value == 2
        assert score.rule_matched == "tier_2_fan_out"

    def test_tier_2_natural_key_in_sub_collection(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(
                is_natural_key=(True, "high"),
                sub_collection_depth=(1, "high"),
            )
        )
        assert score.value == 2
        assert score.rule_matched == "tier_2_natural_key_sub_collection"

    def test_tier_2_heavy_extension_footprint(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(entity_extension_footprint=(3, "high"))
        )
        assert score.value == 2
        assert score.rule_matched == "tier_2_heavy_extension_footprint"

    def test_tier_2_broad_descriptor(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(descriptor_enum_breadth=(25, "high"))
        )
        assert score.value == 2
        assert score.rule_matched == "tier_2_broad_descriptor"

    def test_tier_3_deep_fk_chain(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(fk_chain_depth=(3, "high"))
        )
        assert score.value == 3
        assert score.rule_matched == "tier_3_deep_fk_chain"

    def test_tier_3_natural_key_high_fan_out(self) -> None:
        """The ripple-cost signal: an identity column on an entity that
        N >= 5 other entities reference. Classic Student.studentUniqueId shape."""
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(
                is_natural_key=(True, "high"),
                reference_fan_out=(10, "high"),
            )
        )
        assert score.value == 3
        assert score.rule_matched == "tier_3_natural_key_high_fan_out"

    def test_tier_3_high_fan_out_without_natural_key_stays_tier_2(self) -> None:
        """fan-out alone (no identity) doesn't jump to tier 3 — an
        accessory column on a fanned-against entity isn't the ripple point."""
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(reference_fan_out=(10, "high"))
        )
        assert score.value == 2
        assert score.rule_matched == "tier_2_fan_out"

    def test_tier_3_wide_descriptor_alone(self) -> None:
        """Descriptor breadth >= 40 is tier 3 regardless of FK context."""
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(descriptor_enum_breadth=(50, "high"))
        )
        assert score.value == 3
        assert score.rule_matched == "tier_3_wide_descriptor"

    def test_inputs_used_snapshot_includes_all_six(self) -> None:
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(is_natural_key=(True, "high"))
        )
        assert set(score.inputs_used) == {
            "is_natural_key",
            "fk_chain_depth",
            "reference_fan_out",
            "sub_collection_depth",
            "descriptor_enum_breadth",
            "entity_extension_footprint",
        }

    def test_confidence_propagates(self) -> None:
        """Low confidence on any structural fact degrades the dimension."""
        from src.score.rules import structural_depth

        score = structural_depth(
            self._view_struct(fk_chain_depth=(3, "low"))
        )
        assert score.confidence == "low"

    def test_registered_in_both_lens_rule_inputs(self) -> None:
        from src.score.rules import SPINE_RULE_INPUTS

        for fact in (
            "is_natural_key",
            "fk_chain_depth",
            "reference_fan_out",
            "sub_collection_depth",
            "descriptor_enum_breadth",
            "entity_extension_footprint",
        ):
            assert fact in SPINE_RULE_INPUTS
            assert fact in SOURCE_RULE_INPUTS


# ---------------------------------------------------------------------------
# score_record orchestrates the three dimensions
# ---------------------------------------------------------------------------


class TestScoreRecord:
    def test_returns_all_spine_dimensions(self) -> None:
        v = _view(
            definition_present=(True, "high"),
            business_rules_present=(True, "high"),
            data_type_canonical=(True, "high"),
            definition_is_implementable=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
            required_when_stated=(True, "high"),
            populations_or_scope_stated=(True, "high"),
            conditional_reporting_stated=(False, "high"),
            has_conditional_logic=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            cross_entity_targets=(0, "high"),
            documentation_style=("conceptual", "high"),
        )
        scores = score_record(v)
        assert [s.name for s in scores] == list(SPINE_DIMENSIONS)
        # docs=3, obligation=3, complexity=0, structural=0 (no spine
        # facts in view), doc_explicitness=2 (conceptual), nachos=0,
        # documentation_gap=0 (structural<2 → tier_0_low_complexity).
        assert [s.value for s in scores] == [3, 3, 0, 0, 2, 0, 0]

    def test_unknown_lens_raises(self) -> None:
        v = _view()
        with pytest.raises(ValueError, match="unknown lens"):
            score_record(v, lens="not_a_real_lens")

    def test_source_lens_runs_five_rules(self) -> None:
        v = _view(
            source="core",
            element_name_matches_canonical=(True, "high"),
            naming_deviation_cosmetic=(False, "high"),
            definition_present=(True, "high"),
            definition_adds_detail_beyond_edfi=(False, "medium"),
            definition_text_substantive=(True, "high"),
            semantic_class=("aligned", "high"),
            state_scope_delta=("neutral", "high"),
            extension_is_necessary=(False, "high"),
            extension_is_standalone=(False, "high"),
            extension_mirrors_core_pattern=(False, "high"),
            # Phase F NACHOS inputs (shared with spine).
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        scores = score_record(v, lens="source")
        assert [s.name for s in scores] == list(SOURCE_DIMENSIONS)


# ---------------------------------------------------------------------------
# Source-lens rules (Phase C2)
# ---------------------------------------------------------------------------


class TestCanonicalNameAlignment:
    def test_tier_3_exact_core(self) -> None:
        v = _view(
            source="core",
            element_name_matches_canonical=(True, "high"),
            naming_deviation_cosmetic=(False, "high"),
        )
        score = canonical_name_alignment(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_exact"

    def test_tier_3_exact_extension(self) -> None:
        """Extensions with exact canonical name also score tier 3 —
        the extension still landed on a canonical slot."""
        v = _view(
            source="extension",
            element_name_matches_canonical=(True, "high"),
            naming_deviation_cosmetic=(False, "high"),
        )
        assert canonical_name_alignment(v).value == 3

    def test_tier_2_cosmetic_core_only(self) -> None:
        v = _view(
            source="core",
            element_name_matches_canonical=(False, "high"),
            naming_deviation_cosmetic=(True, "high"),
        )
        score = canonical_name_alignment(v)
        assert score.value == 2
        assert score.rule_matched == "tier_2_cosmetic"

    def test_tier_2_cosmetic_extension_falls_through_to_tier_1(self) -> None:
        """Extensions renaming canonicals should not get the cosmetic
        tier 2 free pass — renames are a design choice, not drift."""
        v = _view(
            source="extension",
            element_name_matches_canonical=(False, "high"),
            naming_deviation_cosmetic=(True, "high"),
        )
        score = canonical_name_alignment(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_resolved"

    def test_tier_1_resolved_rename(self) -> None:
        v = _view(
            source="core",
            element_name_matches_canonical=(False, "high"),
            naming_deviation_cosmetic=(False, "high"),
        )
        assert canonical_name_alignment(v).value == 1

    def test_tier_0_unresolved(self) -> None:
        v = _view(
            source="unknown",
            element_name_matches_canonical=(False, "high"),
            naming_deviation_cosmetic=(False, "high"),
        )
        score = canonical_name_alignment(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_unresolved"


class TestDefinitionQuality:
    def test_tier_3_detail_beyond_edfi(self) -> None:
        v = _view(
            definition_present=(True, "high"),
            definition_adds_detail_beyond_edfi=(True, "medium"),
            definition_text_substantive=(True, "high"),
        )
        assert definition_quality(v).value == 3

    def test_tier_2_substantive_without_detail(self) -> None:
        v = _view(
            definition_present=(True, "high"),
            definition_adds_detail_beyond_edfi=(False, "medium"),
            definition_text_substantive=(True, "high"),
        )
        score = definition_quality(v)
        assert score.value == 2
        assert score.rule_matched == "tier_2_substantive"

    def test_tier_1_thin_definition(self) -> None:
        v = _view(
            definition_present=(True, "high"),
            definition_adds_detail_beyond_edfi=(False, "medium"),
            definition_text_substantive=(False, "high"),
        )
        assert definition_quality(v).value == 1

    def test_tier_0_no_definition(self) -> None:
        v = _view(
            definition_present=(False, "high"),
            definition_adds_detail_beyond_edfi=(False, "medium"),
            definition_text_substantive=(False, "high"),
        )
        assert definition_quality(v).value == 0


class TestSemanticFidelity:
    def test_tier_3_aligned(self) -> None:
        v = _view(
            semantic_class=("aligned", "high"),
            state_scope_delta=("neutral", "high"),
        )
        assert semantic_fidelity(v).value == 3

    def test_tier_2_narrows_explains_divergence(self) -> None:
        v = _view(
            semantic_class=("divergent", "high"),
            state_scope_delta=("narrows", "high"),
        )
        score = semantic_fidelity(v)
        assert score.value == 2
        assert score.rule_matched == "tier_2_divergent_explained"

    def test_tier_2_broadens_explains_divergence(self) -> None:
        v = _view(
            semantic_class=("divergent", "high"),
            state_scope_delta=("broadens", "high"),
        )
        assert semantic_fidelity(v).value == 2

    def test_divergent_with_neutral_scope_delta_falls_through_to_tier_1(self) -> None:
        """Pre-PR-A this was the (narrows=True, broadens=True) case
        guarded by an XOR test — direction not uniquely explained, so
        tier 2 must not fire. Post-PR-A the merged enum is mutually
        exclusive by construction; the load-bearing case is now LLM
        returning ``divergent`` semantic_class with ``neutral`` scope
        delta (i.e., divergence detected but direction not pinned).
        Same destination tier — semantic_class handling kicks in."""
        v = _view(
            semantic_class=("divergent", "high"),
            state_scope_delta=("neutral", "high"),
        )
        score = semantic_fidelity(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_divergent_unclear"

    def test_not_applicable_nulls_the_dimension(self) -> None:
        v = _view(
            semantic_class=("not_applicable", "high"),
            state_scope_delta=("neutral", "high"),
        )
        score = semantic_fidelity(v)
        assert score.value is None
        assert score.rule_matched == "null_not_applicable"

    def test_tier_0_unresolved_when_semantic_class_downgraded(self) -> None:
        v = _view(
            semantic_class=(None, "low", True),
            state_scope_delta=("neutral", "high"),
        )
        score = semantic_fidelity(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_unresolved"


class TestExtensionJustification:
    def test_core_row_returns_null(self) -> None:
        v = _view(source="core")
        score = extension_justification(v)
        assert score.value is None
        assert score.rule_matched == "not_applicable"

    def test_unknown_row_returns_null(self) -> None:
        v = _view(source="unknown")
        assert extension_justification(v).value is None

    def test_tier_3_necessary_standalone(self) -> None:
        v = _view(
            source="extension",
            extension_is_necessary=(True, "high"),
            extension_is_standalone=(True, "high"),
            extension_mirrors_core_pattern=(False, "high"),
        )
        assert extension_justification(v).value == 3

    def test_tier_2_necessary_companion(self) -> None:
        v = _view(
            source="extension",
            extension_is_necessary=(True, "high"),
            extension_is_standalone=(False, "high"),
            extension_mirrors_core_pattern=(False, "high"),
        )
        score = extension_justification(v)
        assert score.value == 2
        assert score.rule_matched == "tier_2_necessary_companion"

    def test_tier_0_unnecessary_mirror(self) -> None:
        v = _view(
            source="extension",
            extension_is_necessary=(False, "high"),
            extension_is_standalone=(False, "high"),
            extension_mirrors_core_pattern=(True, "high"),
        )
        score = extension_justification(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_unnecessary_mirror"

    def test_tier_1_partial_when_not_necessary_and_not_mirror(self) -> None:
        v = _view(
            source="extension",
            extension_is_necessary=(False, "high"),
            extension_is_standalone=(True, "high"),
            extension_mirrors_core_pattern=(False, "high"),
        )
        assert extension_justification(v).value == 1


# ---------------------------------------------------------------------------
# documentation_style_tier (Integration Profile)
# ---------------------------------------------------------------------------


class TestDocumentationExplicitness:
    """Plan ``docs/archive/nachos-v2-two-axis-plan.md`` §3.1 — five-category
    classifier → 0..3 tier mapping."""

    def test_tier_3_prescriptive(self) -> None:
        v = _view(documentation_style=("prescriptive", "high"))
        score = documentation_style_tier(v)
        assert score.value == 3
        assert score.rule_matched == "tier_3_prescriptive"
        assert score.confidence == "high"

    def test_tier_2_conceptual(self) -> None:
        v = _view(documentation_style=("conceptual", "high"))
        score = documentation_style_tier(v)
        assert score.value == 2
        assert score.rule_matched == "tier_2_conceptual"

    def test_tier_1_cross_reference(self) -> None:
        v = _view(documentation_style=("cross_reference", "medium"))
        score = documentation_style_tier(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_cross_reference"
        assert score.confidence == "medium"

    def test_tier_1_regulatory(self) -> None:
        """Regulatory and cross_reference both map to tier 1 — the
        narrative exists but doesn't prescribe or describe the field.
        The distinct rule_matched label preserves the provenance so
        analysts can tell them apart."""
        v = _view(documentation_style=("regulatory", "high"))
        score = documentation_style_tier(v)
        assert score.value == 1
        assert score.rule_matched == "tier_1_regulatory"

    def test_tier_0_unspecified(self) -> None:
        v = _view(documentation_style=("unspecified", "high"))
        score = documentation_style_tier(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_unspecified"

    def test_tier_0_unresolved_when_fact_missing(self) -> None:
        """Classifier fact missing from the view — rule evaluates but
        surfaces tier_0_unresolved so the aggregate's review block
        flags the row."""
        v = _view()
        score = documentation_style_tier(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_unresolved"

    def test_downgraded_classifier_forces_low_confidence(self) -> None:
        """Downgraded classifier (hallucinated span) → the composite
        demotes confidence to low and the rule resolves to
        tier_0_unresolved because ``enum()`` surfaces None on downgrade."""
        v = _view(documentation_style=(None, "low", True))
        score = documentation_style_tier(v)
        assert score.value == 0
        assert score.rule_matched == "tier_0_unresolved"
        assert score.confidence == "low"

    def test_inputs_used_records_the_classifier(self) -> None:
        v = _view(documentation_style=("conceptual", "high"))
        score = documentation_style_tier(v)
        assert score.inputs_used == {"documentation_style": "conceptual"}


# ---------------------------------------------------------------------------
# documentation_gap (Integration Profile — second-pass rule)
# ---------------------------------------------------------------------------


def _dim(name: str, value: int | None, *, confidence: str = "high") -> DimensionScore:
    """Build a stub first-pass ``DimensionScore`` for second-pass rule tests."""
    return DimensionScore(
        name=name,
        value=value,
        rule_matched=f"stub_{value}",
        inputs_used={},
        confidence=confidence,
    )


class TestUndocumentedComplexity:
    """Plan ``docs/archive/nachos-v2-two-axis-plan.md`` §3.2 / Day 3, Mitigation 3.

    Composes over first-pass ``structural_depth`` +
    ``documentation_style_tier`` — fires (value=1) when the spine says
    the row is structurally complex AND narrative says the state
    doesn't prescribe how to populate it.
    """

    def test_tier_1_gap_high_struct_low_doc(self) -> None:
        """structural=2 AND doc=1 → gap signal fires."""
        first = {
            "structural_depth": _dim("structural_depth", 2),
            "documentation_style_tier": _dim("documentation_style_tier", 1),
        }
        score = documentation_gap(first)
        assert score.value == 1
        assert score.rule_matched == "tier_1_gap"
        assert score.name == "documentation_gap"
        assert score.confidence == "high"

    def test_tier_1_gap_deep_struct_unspecified_doc(self) -> None:
        """structural=3 AND doc=0 (unspecified) → gap signal fires."""
        first = {
            "structural_depth": _dim("structural_depth", 3),
            "documentation_style_tier": _dim("documentation_style_tier", 0),
        }
        score = documentation_gap(first)
        assert score.value == 1
        assert score.rule_matched == "tier_1_gap"

    def test_tier_0_documented_high_struct_prescriptive_doc(self) -> None:
        """structural=3 AND doc=3 (prescriptive) → no gap. The state
        prescribed how to populate the structurally-complex field."""
        first = {
            "structural_depth": _dim("structural_depth", 3),
            "documentation_style_tier": _dim("documentation_style_tier", 3),
        }
        score = documentation_gap(first)
        assert score.value == 0
        assert score.rule_matched == "tier_0_documented"

    def test_tier_0_documented_medium_doc_blocks_gap(self) -> None:
        """structural=2 AND doc=2 (conceptual) → no gap. Conceptual docs
        are above the gap threshold even though not prescriptive."""
        first = {
            "structural_depth": _dim("structural_depth", 2),
            "documentation_style_tier": _dim("documentation_style_tier", 2),
        }
        score = documentation_gap(first)
        assert score.value == 0
        assert score.rule_matched == "tier_0_documented"

    def test_tier_0_low_complexity_scalar_leaf(self) -> None:
        """structural=0 → no gap regardless of doc style. A scalar leaf
        on a flat entity doesn't need prescriptive docs."""
        first = {
            "structural_depth": _dim("structural_depth", 0),
            "documentation_style_tier": _dim("documentation_style_tier", 0),
        }
        score = documentation_gap(first)
        assert score.value == 0
        assert score.rule_matched == "tier_0_low_complexity"

    def test_tier_0_low_complexity_struct_one(self) -> None:
        """structural=1 stays under the threshold — a single structural
        signal (marked natural key, shallow FK, one sub-collection) is
        not enough weight for the gap signal to fire."""
        first = {
            "structural_depth": _dim("structural_depth", 1),
            "documentation_style_tier": _dim("documentation_style_tier", 0),
        }
        score = documentation_gap(first)
        assert score.value == 0
        assert score.rule_matched == "tier_0_low_complexity"

    def test_tier_0_unresolved_when_structural_missing(self) -> None:
        """structural_depth absent from first-pass map → unresolved."""
        first = {
            "documentation_style_tier": _dim("documentation_style_tier", 1),
        }
        score = documentation_gap(first)
        assert score.value is None
        assert score.rule_matched == "tier_0_unresolved"

    def test_tier_0_unresolved_when_doc_value_is_none(self) -> None:
        """documentation_style_tier exists but its value is None
        (e.g., downgraded classifier) → unresolved."""
        first = {
            "structural_depth": _dim("structural_depth", 2),
            "documentation_style_tier": _dim(
                "documentation_style_tier", None, confidence="low"
            ),
        }
        score = documentation_gap(first)
        assert score.value is None
        assert score.rule_matched == "tier_0_unresolved"
        assert score.confidence == "low"

    def test_confidence_min_across_inputs(self) -> None:
        """Composite confidence = min across the two input dimensions."""
        first = {
            "structural_depth": _dim(
                "structural_depth", 2, confidence="high"
            ),
            "documentation_style_tier": _dim(
                "documentation_style_tier", 1, confidence="medium"
            ),
        }
        score = documentation_gap(first)
        assert score.value == 1
        assert score.confidence == "medium"

    def test_inputs_used_records_tier_values(self) -> None:
        """inputs_used captures the two tier values consulted — the audit
        trail is self-contained without re-reading the first-pass list."""
        first = {
            "structural_depth": _dim("structural_depth", 3),
            "documentation_style_tier": _dim("documentation_style_tier", 1),
        }
        score = documentation_gap(first)
        assert score.inputs_used == {
            "structural_depth": 3,
            "documentation_style_tier": 1,
        }


class TestScoreRecordSecondPass:
    """Integration check: ``score_record`` runs the first-pass rules
    then the second-pass ``documentation_gap`` rule and appends it
    to the return list. The second-pass dimension sees the just-computed
    first-pass values, not the raw fact pool.
    """

    def test_spine_second_pass_fires_on_high_struct_low_doc(self) -> None:
        # Deep FK chain (structural tier 3) + unspecified narrative
        # (doc_explicitness tier 0). Gap signal must fire.
        v = _view(
            definition_present=(True, "high"),
            business_rules_present=(False, "high"),
            data_type_canonical=(True, "high"),
            definition_is_implementable=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
            required_when_stated=(False, "high"),
            populations_or_scope_stated=(False, "high"),
            conditional_reporting_stated=(False, "high"),
            has_conditional_logic=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            cross_entity_targets=(0, "high"),
            is_natural_key=(True, "high"),
            fk_chain_depth=(3, "high"),
            reference_fan_out=(0, "high"),
            sub_collection_depth=(0, "high"),
            descriptor_enum_breadth=(0, "high"),
            entity_extension_footprint=(0, "high"),
            documentation_style=("unspecified", "high"),
        )
        scores = score_record(v, lens="spine")
        by_name = {s.name: s for s in scores}
        assert by_name["structural_depth"].value == 3
        assert by_name["documentation_style_tier"].value == 0
        assert by_name["documentation_gap"].value == 1
        assert by_name["documentation_gap"].rule_matched == "tier_1_gap"

    def test_source_second_pass_runs_and_appends_last(self) -> None:
        # Conceptual narrative (doc=2) on a flat scalar (structural=0)
        # — not a gap. Second-pass dimension still appears in the return
        # list so the sidecar shape is stable across all rows.
        v = _view(
            source="core",
            element_name_matches_canonical=(True, "high"),
            naming_deviation_cosmetic=(False, "high"),
            definition_present=(True, "high"),
            definition_adds_detail_beyond_edfi=(False, "medium"),
            definition_text_substantive=(True, "high"),
            semantic_class=("aligned", "high"),
            state_scope_delta=("neutral", "high"),
            extension_is_necessary=(False, "high"),
            extension_is_standalone=(False, "high"),
            extension_mirrors_core_pattern=(False, "high"),
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
            documentation_style=("conceptual", "high"),
        )
        scores = score_record(v, lens="source")
        assert [s.name for s in scores] == list(SOURCE_DIMENSIONS)
        # Ordering: documentation_gap appended at the end.
        assert scores[-1].name == "documentation_gap"
        assert scores[-1].value == 0
        assert scores[-1].rule_matched == "tier_0_low_complexity"


# ---------------------------------------------------------------------------
# Real-data smoke — load Phase B artifacts for each state
# ---------------------------------------------------------------------------


class TestLoadFactPoolSourceFilter:
    """When a fact is registered in ``FACT_SOURCE_FILTERS`` and its
    artifact only carries the allowed source subset, non-matching
    records must surface as ``filtered_by_source`` (not downgraded) so
    the aggregate's review block doesn't flag them. Phase D carryover #1.
    """

    def _write_artifact(
        self,
        tmp_path: Path,
        state: str,
        fact: str,
        rows: list[dict],
        lens: str = "source",
    ) -> None:
        import json

        header = {
            "__type": "header",
            "state": state,
            "lens": lens,
            "fact": fact,
            "record_count": len(rows),
            "scored_count": len(rows),
            "mode": "api",
            "status": "complete",
            "model": "claude-sonnet-4-6",
            "prompt_version": "test.v1",
        }
        path = tmp_path / f"{state}_{lens}_{fact}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(header) + "\n")
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def _row(self, record_key: str, entity: str, element_name: str, value) -> dict:
        return {
            "record_key": record_key,
            "entity": entity,
            "element_name": element_name,
            "llm_value": value,
            "validated_value": value,
            "spans": [],
            "confidence": "high",
            "downgrade_reason": None,
            "any_invalid_spans": False,
            "model": "claude-sonnet-4-6",
            "prompt_version": "test.v1",
        }

    def test_filtered_by_source_marker_not_downgraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-extension record that's absent from an extension-only
        fact's artifact gets ``filtered_by_source`` (not downgraded)."""
        # Seed an unrelated non-filtered fact for both rows so the record
        # keys appear in the pool; seed extension_is_necessary with ONLY
        # the extension row.
        self._write_artifact(
            tmp_path,
            "AZ",
            "definition_present",
            [
                self._row("AZ|Student|core_x", "Student", "core_x", True),
                self._row("AZ|Student|ext_y", "Student", "ext_y", True),
            ],
        )
        self._write_artifact(
            tmp_path,
            "AZ",
            "extension_is_necessary",
            [self._row("AZ|Student|ext_y", "Student", "ext_y", True)],
        )

        # Monkey-patch _load_record_meta to return our synthetic source map.
        import src.score.rules as rules_module

        def _fake_meta(state: str, lens: str) -> dict:
            # 4-tuple: (source, extension_name, documented, documentation_source).
            # ``documented`` + ``documentation_source`` were added at v21
            # to support headline filtering of swagger-backfilled rows.
            return {
                "AZ|Student|core_x": ("core", None, True, "source_doc"),
                "AZ|Student|ext_y": ("extension", "ext", True, "source_doc"),
            }

        monkeypatch.setattr(rules_module, "_load_record_meta", _fake_meta)

        pool = load_fact_pool(
            "AZ",
            lens="source",
            facts=("definition_present", "extension_is_necessary"),
            artifacts_dir=tmp_path,
        )
        core_view = pool["AZ|Student|core_x"]
        ext_view = pool["AZ|Student|ext_y"]

        core_raw = core_view.raw_fact_results(["extension_is_necessary"])
        assert core_raw["extension_is_necessary"].value is None
        assert core_raw["extension_is_necessary"].downgraded is False
        assert core_raw["extension_is_necessary"].downgrade_reason == "filtered_by_source"
        # Confidence stays high — the filter is deterministic policy,
        # not an uncertainty signal.
        assert core_raw["extension_is_necessary"].confidence == "high"

        ext_raw = ext_view.raw_fact_results(["extension_is_necessary"])
        assert ext_raw["extension_is_necessary"].value is True
        assert ext_raw["extension_is_necessary"].downgraded is False


@pytest.mark.realdata
class TestRealArtifactSmoke:
    @pytest.mark.parametrize("state", ["AZ", "WI", "MN", "TX", "IN"])
    def test_load_fact_pool_against_real_artifacts(self, state: str) -> None:
        """Phase B artifacts on disk for 10 of 11 facts; Phase C adds the
        11th (descriptor_values_enumerated). Until that artifact is
        written this smoke is gated on the 10 we already have, plus the
        four deterministic facts from Phase B."""
        # Only load facts whose artifacts are on disk — the C1 run-all
        # call later will write descriptor_values_enumerated too.
        from src.utils.paths import scoring_phase_a_dir

        facts_on_disk = [
            f
            for f in sorted(SPINE_RULE_INPUTS)
            if (scoring_phase_a_dir() / f"{state}_spine_{f}.jsonl").exists()
        ]
        if len(facts_on_disk) < 10:
            pytest.skip(
                f"{state}: only {len(facts_on_disk)} fact artifacts on disk; "
                f"run `mc score run-all --state {state.lower()} --facts all` first"
            )
        pool = load_fact_pool(state, lens="spine", facts=facts_on_disk)
        assert pool, f"{state}: fact pool must be non-empty"
        # Spot-check one record: score_record must produce 5 dimensions
        # (Phase F added ``nachos_score``; v2 added ``structural_depth``).
        first = next(iter(pool.values()))
        scores = score_record(first)
        assert len(scores) == len(SPINE_DIMENSIONS)
        for s in scores:
            assert s.value is None or 0 <= s.value <= 3


# ---------------------------------------------------------------------------
# Policy discipline — AST check
# ---------------------------------------------------------------------------


class TestPolicyDiscipline:
    """Per plan §3.3: rules.py cannot import `re` and cannot read
    narrative text fields. The rule tree is a flat declarative cascade
    over facts + deterministic field-presence booleans."""

    _NARRATIVE_FIELDS = (
        "definition_text",
        "business_rules_text",
        "element_specific_rules",
        "edfi_standard_definition",
        "descriptor_table_values",
        "descriptor_table_code",
        "regulatory_citations",
        "related_entities",
        "collections_text",
        "raw_entity",
        "source_document",
        "source_page_or_section",
    )

    def _rules_module_path(self) -> Path:
        from src.score import rules as rules_module

        return Path(rules_module.__file__)

    def test_re_not_imported(self) -> None:
        import ast

        tree = ast.parse(self._rules_module_path().read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "re", "rules.py must not import `re`"
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "re", "rules.py must not import from `re`"

    def test_no_regex_calls(self) -> None:
        import ast

        tree = ast.parse(self._rules_module_path().read_text(encoding="utf-8"))
        forbidden = {"search", "match", "findall", "fullmatch", "compile", "sub", "split"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                # Only flag if the value is `re` — avoid false hits on e.g.
                # `str.split`.
                if isinstance(node.value, ast.Name) and node.value.id == "re":
                    pytest.fail(f"rules.py must not call re.{node.attr}")

    def test_no_narrative_field_reads(self) -> None:
        """No rule body references a narrative-text attribute. Any
        need for narrative inspection is a new fact, not a new branch."""
        import ast

        tree = ast.parse(self._rules_module_path().read_text(encoding="utf-8"))
        # Collect all attribute names referenced by any expression.
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in self._NARRATIVE_FIELDS:
                pytest.fail(
                    f"rules.py references narrative field `{node.attr}` — "
                    "write a new fact instead (plan §3.3)."
                )
            # Subscript access like record["definition_text"] is rarer
            # but equally forbidden.
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and node.slice.value in self._NARRATIVE_FIELDS
            ):
                pytest.fail(
                    f"rules.py subscript-reads `{node.slice.value}` — "
                    "write a new fact instead (plan §3.3)."
                )
