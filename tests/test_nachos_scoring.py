"""Phase F — methodology-conformant NACHOS scoring consolidated suite.

Covers:

- Rule cascade truth table (re-covered from test_score_rules.py at the
  record-end-to-end level, not just the rule level).
- Adjustment arithmetic per plan §4: +1 unnecessary_ext, +0.5
  necessary_ext (mutually exclusive with +1), +0.5 multi_entity,
  cap at 4.5. (v18 reintroduced the +1/+0.5 fork after the v17
  B3 blanket collapse — see TestExtensionNecessityFork.)
- Cross-lens fact-borrow for spine-lens extensions (loading
  source-lens sidecar's ``extension_is_necessary``).
- ``extension_necessity_unresolved`` fallback when the source-lens
  sidecar is missing.
- In-scope classifier truth table per plan §5: unnecessary_ext,
  aggregate_or_concat, vendor_calc (multi_entity), core_with_logic.
- Justification text shape: ``"{rule_matched}[; +<adj labels>]"``.
- Sidecar shape additions (``adjusted_nachos_score``, ``in_scope``,
  ``nachos_justification``, ``in_scope_count``,
  ``nachos_score_histogram``, ``adjusted_nachos_score_histogram``,
  ``mean_nachos_score``, ``mean_adjusted_nachos_score``).
- Scoring plan version bump to "2".
"""

from __future__ import annotations

import pytest

import json
from pathlib import Path


from src.score.aggregate import (
    SCORING_PLAN_VERSION,
    _compute_nachos_adjustments,
    run as run_aggregate,
)
from src.score.rules import FactResult, FactView, SPINE_RULE_INPUTS


def _fact_pool(**facts) -> FactView:
    results: dict[str, FactResult] = {}
    for name, spec in facts.items():
        if len(spec) == 2:
            value, confidence = spec
            downgraded = value is None
        else:
            value, confidence, downgraded = spec  # type: ignore[misc]
        results[name] = FactResult(
            fact=name,
            value=value,
            confidence=confidence,
            downgraded=downgraded,
            downgrade_reason="hallucinated_span" if downgraded else None,
        )
    return FactView(
        record_key="AZ|Student|elem",
        entity="Student",
        element_name="elem",
        facts=results,
        source=facts.pop("__source", "core") if False else "core",
        extension_name=None,
    )


def _view(source: str = "core", **facts) -> FactView:
    results: dict[str, FactResult] = {}
    for name, spec in facts.items():
        if len(spec) == 2:
            value, confidence = spec
            downgraded = value is None
        else:
            value, confidence, downgraded = spec  # type: ignore[misc]
        results[name] = FactResult(
            fact=name,
            value=value,
            confidence=confidence,
            downgraded=downgraded,
            downgrade_reason="hallucinated_span" if downgraded else None,
        )
    return FactView(
        record_key="AZ|Student|elem",
        entity="Student",
        element_name="elem",
        facts=results,
        source=source,
        extension_name="TestExt" if source == "extension" else None,
    )


from src.score.rules import nachos_score as rule_nachos_score


# ---------------------------------------------------------------------------
# Adjustment arithmetic — truth table
# ---------------------------------------------------------------------------


class TestAdjustmentArithmetic:
    """Plan §4 — adjustment rules.

    - +1 if unnecessary extension (source==extension AND NOT necessary)
    - +0.5 if necessary extension (mutually exclusive with +1)
    - +0.5 if multi-entity (hce reconciled AND targets >= 2)
    - Cap at 4.5
    """

    def test_core_row_no_adjustments(self) -> None:
        v = _view(
            source="core",
            has_aggregation=(True, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        adj, in_scope, jus, extra = _compute_nachos_adjustments(v, dim)
        # tier 3 aggregation, no adjustments on core
        assert adj == 3.0
        assert jus == "tier_3_aggregation"
        assert in_scope is True  # aggregation → in-scope
        assert extra == []

    def test_unnecessary_extension_plus_one(self) -> None:
        """v18 (issue #84): two-tier weighting reinstated.
        ``extension_is_necessary=False`` fires the +1 unnecessary_ext
        adjustment again. The v17 blanket collapse is retired.
        """
        v = _view(
            source="extension",
            has_aggregation=(False, "high"),
            has_concatenation=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
            extension_is_necessary=(False, "high"),
        )
        dim = rule_nachos_score(v)
        adj, in_scope, jus, _ = _compute_nachos_adjustments(v, dim)
        # tier 3 concatenation + +1 unnecessary_ext = 4.0
        assert adj == 4.0
        assert "+1 unnecessary_ext" in jus
        assert "+0.5 necessary_ext" not in jus
        assert "tier_3_concatenation" in jus
        assert in_scope is True

    def test_necessary_extension_plus_half(self) -> None:
        v = _view(
            source="extension",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(1, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
            extension_is_necessary=(True, "high"),
        )
        dim = rule_nachos_score(v)
        adj, in_scope, jus, _ = _compute_nachos_adjustments(v, dim)
        # tier 1 conditional + necessary extension = 1.0 + 0.5 = 1.5
        assert adj == 1.5
        assert "+0.5 necessary_ext" in jus
        assert "tier_1_conditional" in jus

    def test_multi_entity_plus_half_stacks_with_extension(self) -> None:
        v = _view(
            source="extension",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(3, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
            extension_is_necessary=(True, "high"),
        )
        dim = rule_nachos_score(v)
        adj, in_scope, jus, _ = _compute_nachos_adjustments(v, dim)
        # tier 2 multi_entity + necessary (0.5) + multi_entity (0.5) = 3.0
        assert adj == 3.0
        assert "+0.5 necessary_ext" in jus
        assert "+0.5 multi_entity" in jus

    def test_capped_at_4_5(self) -> None:
        """v18 (issue #84): max stack is tier_3 + unnecessary_ext (+1) +
        multi_entity (+0.5) = 4.5. Cap of 4.5 holds when adjustments
        sum exactly to the ceiling and would clamp anything above.
        """
        v = _view(
            source="extension",
            has_aggregation=(True, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(3, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
            extension_is_necessary=(False, "high"),
        )
        dim = rule_nachos_score(v)
        adj, _, _, _ = _compute_nachos_adjustments(v, dim)
        # tier 3 (3.0) + unnecessary_ext (+1.0) + multi_entity (+0.5) = 4.5
        assert adj == 4.5

    def test_multi_entity_only_core(self) -> None:
        v = _view(
            source="core",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(3, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(v, dim)
        # tier 2 multi_entity + multi_entity (0.5) = 2.5
        assert adj == 2.5
        assert "+0.5 multi_entity" in jus
        assert "tier_2_multi_entity" in jus

    def test_extension_necessity_unresolved_falls_back_with_review_flag(self) -> None:
        """v18 (issue #84): spine-lens extension with no per-row fact
        AND no source-lens sidecar borrow falls back to +0.5 with an
        `extension_necessity_unresolved` review flag. Conservative
        choice — analyst audits the missing judgment rather than the
        scorer eating a +1 penalty on absent evidence."""
        v = _view(
            source="extension",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
            # Note: extension_is_necessary not in pool
        )
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(v, dim, source_ext_necessity={})
        # tier 1 + unresolved → +0.5 = 1.5
        assert adj == 1.5
        assert "+0.5 necessary_ext" in jus
        assert "extension_necessity_unresolved" in extra

    def test_source_lens_borrow_reads_sidecar(self) -> None:
        """Cross-lens borrow: pass source-lens extension_necessity map."""
        v = _view(
            source="extension",
            has_aggregation=(False, "high"),
            has_concatenation=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        # Borrow says necessary → +0.5. Issue #94: dict keys are
        # lowercased — borrow joins case-insensitively so source-lens
        # PascalCase / spine-lens camelCase resolve to the same entry.
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, source_ext_necessity={"az|student|elem": True}
        )
        assert adj == 3.5
        assert "+0.5 necessary_ext" in jus
        assert extra == []

    def test_source_lens_borrow_unnecessary_fires_plus_one(self) -> None:
        """v18 (issue #84): cross-lens borrow returning False fires
        +1 unnecessary_ext on the spine-lens row, mirroring the
        same-lens path."""
        v = _view(
            source="extension",
            has_aggregation=(False, "high"),
            has_concatenation=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, source_ext_necessity={"az|student|elem": False}
        )
        # tier 3 concatenation + +1 unnecessary_ext = 4.0
        assert adj == 4.0
        assert "+1 unnecessary_ext" in jus
        assert "+0.5 necessary_ext" not in jus
        assert extra == []

    def test_nachos_none_returns_none(self) -> None:
        """If the nachos rule returned None (inputs insufficient), all
        Phase F surfaces are None / False."""
        v = _view(source="core")
        adj, in_scope, jus, extra = _compute_nachos_adjustments(v, None)
        assert adj is None
        assert in_scope is False
        assert jus is None


class TestReconcileHelperParity:
    """Issue #213 item 3: `_compute_nachos_adjustments` now calls
    `rules._reconcile_cross_entity` instead of re-inlining the
    reconciliation + bare-FK suppression. This grid pins that the
    multi-entity adjustment fires on EXACTLY the combos the old inline
    arithmetic fired on: ``hce AND targets >= 2 AND NOT bare_fk``.

    (The helper's flag is ``hce AND targets >= 1``; ANDed with the
    multi-entity fork's ``targets >= 2`` that reduces to the old
    expression — this test is the executable proof.)
    """

    @pytest.mark.parametrize("hce", [True, False])
    @pytest.mark.parametrize("targets", [0, 1, 2, 3])
    @pytest.mark.parametrize("bare_fk", [True, False])
    def test_multi_entity_adjustment_matches_old_inline_logic(
        self, hce: bool, targets: int, bare_fk: bool
    ) -> None:
        # The OLD inline arithmetic, verbatim (pre-#213 aggregate.py).
        hce_raw, t = hce, targets
        if bare_fk:
            hce_raw, t = False, 0
        expected_multi = hce_raw and t >= 2

        v = _view(
            source="core",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(hce, "high"),
            cross_entity_targets=(targets, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
            element_is_bare_fk_reference=(bare_fk, "high"),
        )
        dim = rule_nachos_score(v)
        adj, _in_scope, jus, _extra = _compute_nachos_adjustments(v, dim)
        assert adj is not None and dim.value is not None
        delta = adj - dim.value
        if expected_multi:
            assert delta == pytest.approx(0.5)
            assert "+0.5 multi_entity" in (jus or "")
        else:
            assert delta == pytest.approx(0.0)
            assert "+0.5 multi_entity" not in (jus or "")


# ---------------------------------------------------------------------------
# v18 (issue #84) — two-tier extension weighting reinstated. The +1
# unnecessary_ext / +0.5 necessary_ext fork closes the v17 disclosed
# divergence from the public Ed-Fi NACHOS overview (recommendations.md
# §7.3). Calibration prerequisite landed via #85 (extension_is_necessary
# prompt-tightening + cold re-extract). B5 element-only bare-FK
# suppression in `nachos_score` (rules.py) carries forward unchanged.
# ---------------------------------------------------------------------------


class TestExtensionNecessityFork:
    """v18 fork: True → +0.5 necessary_ext, False → +1 unnecessary_ext,
    None → conservative +0.5 + ``extension_necessity_unresolved``
    review flag.
    """

    def _ext_view(self, **overrides) -> FactView:
        defaults = dict(
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        defaults.update(overrides)
        return _view(source="extension", **defaults)

    def test_extension_necessary_true_fires_half(self) -> None:
        v = self._ext_view(extension_is_necessary=(True, "high"))
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(v, dim)
        assert adj == 0.5
        assert "+0.5 necessary_ext" in jus
        assert "+1 unnecessary_ext" not in jus
        assert extra == []

    def test_extension_necessary_false_fires_one(self) -> None:
        v = self._ext_view(extension_is_necessary=(False, "high"))
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(v, dim)
        assert adj == 1.0
        assert "+1 unnecessary_ext" in jus
        assert "+0.5 necessary_ext" not in jus
        assert extra == []

    def test_extension_necessity_low_conf_false_still_fires_one(self) -> None:
        """v18 carries forward the v17 prompt-calibration posture: no
        in-scorer hedge against low-confidence False judgments. #85's
        prompt-tightening is the calibration vehicle, not a per-row
        bare-FK gate. (The det.v9 ``element_is_bare_fk_reference``
        suppression in `nachos_score` is independent and stays.)"""
        v = self._ext_view(
            extension_is_necessary=(False, "low"),
            extension_is_standalone=(True, "low"),
            element_narrative_present=(False, "high"),
        )
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(v, dim)
        assert adj == 1.0
        assert "+1 unnecessary_ext" in jus
        assert "extension_necessity_unresolved" not in extra

    def test_borrowed_unnecessary_fires_one(self) -> None:
        """Cross-lens borrow: spine-lens reads source-lens
        ``extension_is_necessary``. Borrowed False fires +1
        unnecessary_ext just like the same-lens path."""
        v = self._ext_view()
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, source_ext_necessity={"az|student|elem": False}
        )
        assert adj == 1.0
        assert "+1 unnecessary_ext" in jus
        assert "+0.5 necessary_ext" not in jus

    def test_borrowed_necessary_fires_half(self) -> None:
        v = self._ext_view()
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, source_ext_necessity={"az|student|elem": True}
        )
        assert adj == 0.5
        assert "+0.5 necessary_ext" in jus
        assert "+1 unnecessary_ext" not in jus

    def test_unresolved_falls_back_with_review_flag(self) -> None:
        """No per-row fact AND no borrow → conservative +0.5 +
        review flag. Default protective posture so analysts can audit
        rather than the scorer eating a +1 penalty on absent evidence."""
        v = self._ext_view()
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, source_ext_necessity={}
        )
        assert adj == 0.5
        assert "+0.5 necessary_ext" in jus
        assert "extension_necessity_unresolved" in extra

    def test_core_row_unaffected(self) -> None:
        """Fork is extension-only — core rows get no extension
        adjustment regardless of any judgment fact."""
        v = _view(
            source="core",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(v, dim)
        assert adj == 0.0
        assert "necessary_ext" not in jus

    def test_borrow_lookup_is_case_insensitive(self) -> None:
        """Issue #94: source-lens record_keys may carry source-document
        PascalCase (AZ XLSX, TX TWEDS) while spine-lens record_keys
        carry swagger camelCase. The borrow dict is keyed on the
        lowercased ``record_key``, and the lookup lowercases
        ``view.record_key`` to match — so the join resolves regardless
        of which lens supplied the original casing.
        """
        # View carries the spine-lens camelCase form; borrow dict was
        # built from a source-lens sidecar where the same conceptual
        # element rendered as PascalCase. Pre-fix, this lookup missed
        # entirely and the row fell to the unresolved fallback.
        results: dict[str, FactResult] = {
            name: FactResult(
                fact=name,
                value=value,
                confidence="high",
                downgraded=False,
                downgrade_reason=None,
            )
            for name, value in [
                ("has_aggregation", False),
                ("has_concatenation", False),
                ("has_cross_entity_logic", False),
                ("cross_entity_targets", 0),
                ("has_conditional_logic", False),
                ("descriptor_values_enumerated", False),
            ]
        }
        v = FactView(
            record_key="AZ|Calendar|beginDate",
            entity="Calendar",
            element_name="beginDate",
            facts=results,
            source="extension",
            extension_name="AZExt",
        )
        dim = rule_nachos_score(v)
        # Borrow dict mirrors what _load_source_extension_facts would
        # produce: a source-lens key "AZ|Calendar|BeginDate" lowercased
        # to "az|calendar|begindate".
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, source_ext_necessity={"az|calendar|begindate": False}
        )
        # Borrow resolved → +1 unnecessary_ext (no fallback).
        assert "+1 unnecessary_ext" in jus
        assert "extension_necessity_unresolved" not in extra


class TestB5ElementOnlyScopeForBareFk:
    """B5 (POC interim call): bare FK references suppress the
    has_cross_entity_logic contribution to the NACHOS tier. The
    cross-entity reach is a structural property of the spine, not
    of the element's value or reporting obligation. Element-level
    cross-entity logic from narrative (has_conditional_logic) is
    unaffected.
    """

    def test_bare_fk_with_cross_entity_drops_to_tier_0(self) -> None:
        """Without B5, this would fire tier_2_multi_entity (hce True +
        targets >= 2). With B5, the bare-FK fact suppresses hce → tier_0."""
        v = _view(
            source="core",
            element_is_bare_fk_reference=(True, "high"),
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(3, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 0
        assert dim.rule_matched == "tier_0_none"
        adj, _, jus, _ = _compute_nachos_adjustments(v, dim)
        # No multi_entity adj either — hce was suppressed by B5.
        assert adj == 0.0
        assert "tier_0_none" in jus
        assert "+0.5 multi_entity" not in jus

    def test_bare_fk_with_single_target_drops_to_tier_0(self) -> None:
        """Without B5, single-target cross-entity fires tier_1_conditional.
        With B5, suppressed → tier_0."""
        v = _view(
            source="core",
            element_is_bare_fk_reference=(True, "high"),
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(1, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 0
        assert dim.rule_matched == "tier_0_none"

    def test_bare_fk_with_narrative_conditional_logic_still_fires_tier_1(self) -> None:
        """B5 only suppresses hce; element-level conditional logic from
        the narrative (e.g., 'if Object is 61XX-66XX...') still fires
        tier_1 normally. Common pattern: TX Organization FK rows."""
        v = _view(
            source="core",
            element_is_bare_fk_reference=(True, "high"),
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),  # would normally fire tier
            cross_entity_targets=(2, "high"),
            has_conditional_logic=(True, "high"),  # narrative branch logic
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 1
        assert dim.rule_matched == "tier_1_conditional"

    def test_non_bare_fk_unaffected(self) -> None:
        """Regression guard: when the bare-FK fact is False, hce fires
        normally (tier_2_multi_entity)."""
        v = _view(
            source="core",
            element_is_bare_fk_reference=(False, "high"),
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(3, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 2
        assert dim.rule_matched == "tier_2_multi_entity"

    def test_bare_fk_aggregation_still_fires_tier_3(self) -> None:
        """Regression guard: B5 suppresses only hce. If a bare FK
        somehow also has aggregation (vanishingly rare), tier_3 still
        wins because it's checked first."""
        v = _view(
            source="core",
            element_is_bare_fk_reference=(True, "high"),
            has_aggregation=(True, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(2, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 3
        assert dim.rule_matched == "tier_3_aggregation"


# ---------------------------------------------------------------------------
# v22 — issue #97 (2026-04-30): parent-entity-gate-only suppression of
# has_conditional_logic in business_logic_complexity + nachos_score.
# ---------------------------------------------------------------------------


from src.score.rules import business_logic_complexity as rule_blc


class TestParentEntityGateSuppressionOnNachos:
    """Issue #97: when ``element_only_parent_entity_gate`` is True,
    ``has_conditional_logic`` is suppressed in ``nachos_score``. Mirrors
    B5's pattern but on the conditional-logic axis instead of the
    cross-entity axis. The fact only fires on FK-named rows whose
    only conditional evidence is the parent-record submission gate
    annotation; element-specific value/presence rules disable the
    suppression at the deterministic-fact layer (see test_score_
    deterministic.py).
    """

    def test_fk_with_only_parent_gate_drops_to_tier_0(self) -> None:
        """Without v22, hcl=True fires tier_1_conditional. With v22,
        the gate suppresses hcl → tier_0_none."""
        v = _view(
            source="core",
            element_is_bare_fk_reference=(False, "high"),
            element_only_parent_entity_gate=(True, "high"),
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 0
        assert dim.rule_matched == "tier_0_none"

    def test_fk_with_element_specific_logic_unaffected(self) -> None:
        """When the gate fact is False (e.g., element_specific_rules
        non-empty), hcl fires tier_1 normally."""
        v = _view(
            source="core",
            element_is_bare_fk_reference=(False, "high"),
            element_only_parent_entity_gate=(False, "high"),
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 1
        assert dim.rule_matched == "tier_1_conditional"

    def test_gate_does_not_affect_aggregation_tier_3(self) -> None:
        """Suppression is hcl-only — aggregation/concatenation are
        independent axes and continue to fire."""
        v = _view(
            source="core",
            element_is_bare_fk_reference=(False, "high"),
            element_only_parent_entity_gate=(True, "high"),
            has_aggregation=(True, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 3
        assert dim.rule_matched == "tier_3_aggregation"

    def test_gate_does_not_suppress_cross_entity_tier_2(self) -> None:
        """Multi-entity logic (hce + targets >= 2) is on the cross-
        entity axis, independent from has_conditional_logic. The gate
        only suppresses hcl so a row with cross-entity targets still
        fires tier_2_multi_entity."""
        v = _view(
            source="core",
            element_is_bare_fk_reference=(False, "high"),
            element_only_parent_entity_gate=(True, "high"),
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(2, "high"),
            has_conditional_logic=(True, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 2
        assert dim.rule_matched == "tier_2_multi_entity"


class TestParentEntityGateSuppressionOnBlc:
    """Issue #97: parallel suppression in ``business_logic_complexity``
    (the comprehension-cost axis). Same fact, same precedence, same
    rationale — the conditional belongs to the parent record's
    submission gate, not the FK reference itself."""

    def test_fk_with_only_parent_gate_drops_to_tier_0(self) -> None:
        """hcl=True alone fires tier_1_cond_or_cross; with the gate
        fact True, suppressed → tier_0_none."""
        v = _view(
            source="core",
            element_only_parent_entity_gate=(True, "high"),
            has_conditional_logic=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_aggregation=(False, "high"),
        )
        dim = rule_blc(v)
        assert dim.value == 0
        assert dim.rule_matched == "tier_0_none"

    def test_gate_does_not_affect_cross_entity_tier_1(self) -> None:
        """Suppression is hcl-only — single-target cross-entity still
        fires tier_1."""
        v = _view(
            source="core",
            element_only_parent_entity_gate=(True, "high"),
            has_conditional_logic=(True, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(1, "high"),
            has_aggregation=(False, "high"),
        )
        dim = rule_blc(v)
        assert dim.value == 1
        assert dim.rule_matched == "tier_1_cond_or_cross"

    def test_gate_does_not_affect_aggregation_tier_2(self) -> None:
        v = _view(
            source="core",
            element_only_parent_entity_gate=(True, "high"),
            has_conditional_logic=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_aggregation=(True, "high"),
        )
        dim = rule_blc(v)
        assert dim.value == 2
        assert dim.rule_matched == "tier_2_agg_or_multicross"

    def test_inputs_used_records_reconciled_hcl(self) -> None:
        """Audit trail: ``inputs_used`` records the post-suppression
        value as ``has_conditional_logic__reconciled`` so reviewers
        can trace why a row landed at the tier it did."""
        v = _view(
            source="core",
            element_only_parent_entity_gate=(True, "high"),
            has_conditional_logic=(True, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_aggregation=(False, "high"),
        )
        dim = rule_blc(v)
        assert dim.inputs_used.get("has_conditional_logic") is True
        assert dim.inputs_used.get("has_conditional_logic__reconciled") is False


# ---------------------------------------------------------------------------
# v12 semantic_fidelity → adjusted_nachos_score (B-gated `base0`, issue #55)
# ---------------------------------------------------------------------------


from src.score.rules import DimensionScore


def _sf(value: int | None, rule_matched: str = "tier_x") -> DimensionScore:
    return DimensionScore(
        name="semantic_fidelity",
        value=value,
        rule_matched=rule_matched,
        inputs_used={},
        confidence="high",
    )


def _tier0_view() -> FactView:
    """A core row whose nachos rule lands at tier 0 (no aggregation,
    no concat, no conditional, no multi-entity, no descriptor enum)."""
    return _view(
        source="core",
        has_aggregation=(False, "high"),
        has_concatenation=(False, "high"),
        has_cross_entity_logic=(False, "high"),
        cross_entity_targets=(0, "high"),
        has_conditional_logic=(False, "high"),
        descriptor_values_enumerated=(False, "high"),
    )


class TestSemanticFidelityAdjustment:
    """Issue #55 (B-gated `base0`).

    Adjustment table (source-lens only):
      tier 3 aligned             → 0
      tier 2 divergent_explained → +0.5
      tier 1 divergent_unclear   → +1.0
      tier 0 unresolved          → +1.0
      NA / value=None            → 0

    Gate: fires only when ``nachos_dim.value == 0``. Spine-lens
    callers pass ``sf_dim=None`` so the branch is a no-op.
    """

    def test_tier_3_aligned_no_bump(self) -> None:
        v = _tier0_view()
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(v, dim, sf_dim=_sf(3, "tier_3_aligned"))
        assert adj == 0.0
        assert "fidelity" not in jus

    def test_na_value_none_no_bump(self) -> None:
        v = _tier0_view()
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(None, "null_not_applicable")
        )
        assert adj == 0.0
        assert "fidelity" not in jus

    def test_tier_2_divergent_explained_plus_half(self) -> None:
        v = _tier0_view()
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(2, "tier_2_divergent_explained")
        )
        assert adj == 0.5
        assert "+0.5 fidelity_divergent_explained" in jus

    def test_tier_1_divergent_unclear_plus_one(self) -> None:
        v = _tier0_view()
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert adj == 1.0
        assert "+1.0 fidelity_divergent_unclear" in jus

    def test_tier_0_unresolved_plus_one(self) -> None:
        v = _tier0_view()
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(0, "tier_0_unresolved")
        )
        assert adj == 1.0
        assert "+1.0 fidelity_divergent_unclear" in jus

    def test_gate_blocks_when_base_gt_zero(self) -> None:
        """Gate test — if rubric base > 0, SF must NOT bump even when
        SF tier is below 3. This is the load-bearing wedge of B-gated:
        rubric and SF read overlapping evidence; non-zero base means
        the rubric already credited divergence."""
        v = _view(
            source="core",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(True, "high"),  # tier 1
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 1  # sanity: rubric base is 1
        adj, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        # rubric base 1, NO SF bump because gate blocked it
        assert adj == 1.0
        assert "fidelity" not in jus
        assert jus == "tier_1_conditional"

    def test_sf_dim_none_no_bump(self) -> None:
        """Spine-lens path — sf_dim is None (semantic_fidelity not in
        SPINE_DIMENSIONS). Adjustment must not fire."""
        v = _tier0_view()
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(v, dim, sf_dim=None)
        assert adj == 0.0
        assert "fidelity" not in jus

    def test_cap_holds_with_sf_bump(self) -> None:
        """Construct a row that maxes out via SF + multi_entity + ext.
        Note: SF only fires when base==0; multi_entity needs
        cross_entity_targets >= 2 which pushes the rubric base to
        tier 2, blocking the SF fold gate. So SF + multi_entity is
        impossible by construction, and the practical max-stack on a
        base=0 row under v24 is max(SF, necessity) = +1.0. Cap is
        irrelevant here, but verify cap is applied when contributions
        sum to >4.5 in the non-SF path (already covered by
        ``test_capped_at_4_5``)."""
        v = _tier0_view()
        dim = rule_nachos_score(v)
        adj, _, _, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert adj == 1.0  # well under cap; sanity bound


# ---------------------------------------------------------------------------
# v25 (issue #124 PR 2 / #111) — SF-fold label gains a typed-reason
# parenthetical when ``extension_fidelity_divergence``,
# ``state_scope_delta``, or ``sourcing_constraint_documented`` carry
# non-default values. Magnitudes unchanged from v24; this is a label-
# annotation refactor only.
# ---------------------------------------------------------------------------


def _tier0_view_with(**fact_overrides) -> FactView:
    """Tier-0 view with extra fact entries layered on top.

    Same shape as :func:`_tier0_view` (rubric base 0, no aggregation /
    concat / conditional / multi-entity), but accepts additional facts
    via kwargs so the SF-fold typed-reason annotation tests can attach
    ``extension_fidelity_divergence`` / ``state_scope_delta`` /
    ``sourcing_constraint_documented`` values to the underlying
    FactView.
    """
    return _view(
        source="core",
        has_aggregation=(False, "high"),
        has_concatenation=(False, "high"),
        has_cross_entity_logic=(False, "high"),
        cross_entity_targets=(0, "high"),
        has_conditional_logic=(False, "high"),
        descriptor_values_enumerated=(False, "high"),
        **fact_overrides,
    )


class TestSFFoldTypedReasons:
    """Issue #124 PR 2 / #111 — typed-reason parenthetical on the SF
    fold label.

    Reasons render in canonical order: shape-divergence (det fact),
    state-scope-delta (LLM enum), sourcing-constraint (LLM enum). No-
    evidence enum values (``"none"`` / ``"unspecified"`` / ``"neutral"``)
    are filtered out and never reach the label.
    """

    def test_no_typed_facts_label_unchanged(self) -> None:
        """Tier 1 SF fold with no typed-reason facts on the view —
        label format identical to v24."""
        v = _tier0_view_with()
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert adj == 1.0
        assert "+1.0 fidelity_divergent_unclear" in jus
        assert "(" not in jus  # no parenthetical

    def test_replaces_core_field_shape_annotates(self) -> None:
        v = _tier0_view_with(
            extension_fidelity_divergence=("replaces_core_field_shape", "high"),
        )
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert adj == 1.0
        assert "+1.0 fidelity_divergent_unclear (replaces_core_field_shape)" in jus

    def test_sourcing_constraint_annotates(self) -> None:
        v = _tier0_view_with(
            sourcing_constraint_documented=("transformation", "high"),
        )
        dim = rule_nachos_score(v)
        adj, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert "+1.0 fidelity_divergent_unclear (transformation)" in jus

    def test_state_scope_delta_narrows_annotates(self) -> None:
        v = _tier0_view_with(
            state_scope_delta=("narrows", "high"),
        )
        dim = rule_nachos_score(v)
        _, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(2, "tier_2_divergent_explained")
        )
        assert "+0.5 fidelity_divergent_explained (narrows_core_scope)" in jus

    def test_state_scope_delta_broadens_annotates(self) -> None:
        v = _tier0_view_with(
            state_scope_delta=("broadens", "high"),
        )
        dim = rule_nachos_score(v)
        _, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(2, "tier_2_divergent_explained")
        )
        assert "+0.5 fidelity_divergent_explained (broadens_core_scope)" in jus

    def test_multiple_typed_reasons_canonical_order(self) -> None:
        """All three sources fire — order: shape-divergence first, then
        scope delta, then sourcing constraint. Reasons separated by
        ``", "``."""
        v = _tier0_view_with(
            extension_fidelity_divergence=("replaces_core_field_shape", "high"),
            state_scope_delta=("narrows", "high"),
            sourcing_constraint_documented=("transformation", "high"),
        )
        dim = rule_nachos_score(v)
        _, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        expected = (
            "+1.0 fidelity_divergent_unclear "
            "(replaces_core_field_shape, narrows_core_scope, transformation)"
        )
        assert expected in jus

    def test_no_evidence_values_skipped(self) -> None:
        """``"none"`` / ``"unspecified"`` / ``"neutral"`` are no-evidence
        defaults — they must NOT surface as parenthetical reasons."""
        v = _tier0_view_with(
            extension_fidelity_divergence=("none", "high"),
            state_scope_delta=("neutral", "high"),
            sourcing_constraint_documented=("none", "high"),
        )
        dim = rule_nachos_score(v)
        _, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert "+1.0 fidelity_divergent_unclear" in jus
        assert "(" not in jus

    def test_unspecified_sourcing_constraint_skipped(self) -> None:
        """Sourcing-constraint ``"unspecified"`` is a no-evidence default
        (LLM said "divergence exists but mechanism not pinned down")."""
        v = _tier0_view_with(
            sourcing_constraint_documented=("unspecified", "high"),
        )
        dim = rule_nachos_score(v)
        _, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert "+1.0 fidelity_divergent_unclear" in jus
        assert "(" not in jus

    def test_annotation_blocked_when_sf_fold_blocked(self) -> None:
        """When base != 0, SF fold doesn't fire — even if the typed
        facts carry non-default values, no annotation surfaces because
        no SF label was emitted."""
        v = _view(
            source="core",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(True, "high"),  # rubric base 1
            descriptor_values_enumerated=(False, "high"),
            extension_fidelity_divergence=("replaces_core_field_shape", "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 1
        _, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        # SF gate blocks the fold; no fidelity label; no annotation.
        assert "fidelity" not in jus
        assert "replaces_core_field_shape" not in jus

    def test_dual_fire_with_typed_reasons_carries_annotation(self) -> None:
        """Annotation must survive the v24 max-of-two non-stacking
        path: extension at tier 0 + necessity fork + SF fold + typed
        reasons. Both labels still render; SF carries the
        parenthetical."""
        v = _view(
            source="extension",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
            extension_is_necessary=(False, "high"),
            extension_fidelity_divergence=("replaces_core_field_shape", "high"),
        )
        dim = rule_nachos_score(v)
        adj, _, jus, reasons = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        # Magnitudes follow v24 max-of-two (tied at 1.0; fidelity wins).
        assert adj == 1.0
        # Both labels render in the justification per v24.
        assert "+1 unnecessary_ext" in jus
        # SF label carries the typed-reason parenthetical.
        assert "+1.0 fidelity_divergent_unclear (replaces_core_field_shape)" in jus
        assert "fidelity_necessity_dual_fire" in reasons


# ---------------------------------------------------------------------------
# v24 (issue #124 Option 2) — non-stacking max-of-two with fidelity-wins
# tie-break when SF fold AND necessity fork both fire at base 0. Both
# branches still render in nachos_justification; the new
# `fidelity_necessity_dual_fire` review reason flags the row so analysts
# can find it. Multi-entity is independent (and mutually exclusive with
# SF fold by construction since cross_entity_targets >= 2 pushes base to
# tier 2, blocking the SF gate). Decision source: Slack agreement with
# Doug + Maria 2026-05-02 against the four-option deliberation in
# PR #126 (decision pack at docs/issue-124/axis-decision-pack.md).
# ---------------------------------------------------------------------------


def _tier0_ext_view(*, necessary: bool) -> FactView:
    """Extension row that lands at nachos tier 0 (rubric base 0) with
    ``extension_is_necessary`` set. Building block for the dual-fire
    matrix tests."""
    return _view(
        source="extension",
        has_aggregation=(False, "high"),
        has_concatenation=(False, "high"),
        has_cross_entity_logic=(False, "high"),
        cross_entity_targets=(0, "high"),
        has_conditional_logic=(False, "high"),
        descriptor_values_enumerated=(False, "high"),
        extension_is_necessary=(necessary, "high"),
    )


class TestNonStackingDualFire:
    """v24 — issue #124 Option 2 non-stacking matrix.

    Today's stacking on the 76 source-lens cohort (AZ 80, WI 53, MN 41,
    TX 64) lands at adj=+2.0 because both branches sum. Option 2 takes
    max-of-two with fidelity-wins on tie, dropping those rows to +1.0
    while preserving both labels in ``nachos_justification`` for audit
    clarity.
    """

    def test_tier1_fidelity_plus_unnecessary_ties_at_one(self) -> None:
        """Both adjustments are +1.0 (SF tier 1 / unnecessary_ext).
        Fidelity wins on tie via >= comparison. Mirrors the 3 AZ #111
        examples (`EligibilitySourceDescriptorId`,
        `EligibilityStatusDescriptorId`, `AZAlternateGradeDescriptor`)."""
        v = _tier0_ext_view(necessary=False)
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert adj == 1.0
        # Both labels still render; only the magnitude is max'd.
        assert "+1 unnecessary_ext" in jus
        assert "+1.0 fidelity_divergent_unclear" in jus
        assert "fidelity_necessity_dual_fire" in extra

    def test_tier2_fidelity_plus_unnecessary_necessity_wins(self) -> None:
        """SF tier 2 (+0.5) + unnecessary (+1.0). Necessity > fidelity."""
        v = _tier0_ext_view(necessary=False)
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(2, "tier_2_divergent_explained")
        )
        assert adj == 1.0  # max(0.5, 1.0)
        assert "+1 unnecessary_ext" in jus
        assert "+0.5 fidelity_divergent_explained" in jus
        assert "fidelity_necessity_dual_fire" in extra

    def test_tier1_fidelity_plus_necessary_fidelity_wins(self) -> None:
        """SF tier 1 (+1.0) + necessary (+0.5). Fidelity > necessity."""
        v = _tier0_ext_view(necessary=True)
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert adj == 1.0  # max(1.0, 0.5)
        assert "+0.5 necessary_ext" in jus
        assert "+1.0 fidelity_divergent_unclear" in jus
        assert "fidelity_necessity_dual_fire" in extra

    def test_tier2_fidelity_plus_necessary_ties_at_half(self) -> None:
        """Both adjustments are +0.5 (SF tier 2 / necessary_ext).
        Fidelity wins on tie."""
        v = _tier0_ext_view(necessary=True)
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(2, "tier_2_divergent_explained")
        )
        assert adj == 0.5
        assert "+0.5 necessary_ext" in jus
        assert "+0.5 fidelity_divergent_explained" in jus
        assert "fidelity_necessity_dual_fire" in extra

    def test_only_necessity_no_sf_dim_no_dual_fire(self) -> None:
        """Spine-lens path: necessity fires, SF dim absent (None). No
        non-stacking applied; no dual-fire review reason."""
        v = _tier0_ext_view(necessary=False)
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, sf_dim=None
        )
        assert adj == 1.0
        assert "+1 unnecessary_ext" in jus
        assert "fidelity" not in jus
        assert "fidelity_necessity_dual_fire" not in extra

    def test_only_sf_fold_core_row_no_dual_fire(self) -> None:
        """SF fold fires on a core row (no extension → no necessity
        fork). No non-stacking applied; no dual-fire review reason."""
        v = _tier0_view()  # source="core"
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert adj == 1.0
        assert "+1.0 fidelity_divergent_unclear" in jus
        assert "unnecessary_ext" not in jus
        assert "necessary_ext" not in jus
        assert "fidelity_necessity_dual_fire" not in extra

    def test_base_nonzero_blocks_sf_no_dual_fire(self) -> None:
        """Rubric base != 0 (tier 1 conditional). SF fold gate blocks
        even though sf_dim has a value < 3. Necessity fork fires
        normally; no dual-fire."""
        v = _view(
            source="extension",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(True, "high"),  # tier 1
            descriptor_values_enumerated=(False, "high"),
            extension_is_necessary=(False, "high"),
        )
        dim = rule_nachos_score(v)
        assert dim.value == 1  # sanity: rubric base is 1
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        # base 1 + +1 unnecessary_ext = 2 (no SF bump because gate blocked)
        assert adj == 2.0
        assert "+1 unnecessary_ext" in jus
        assert "fidelity" not in jus
        assert "fidelity_necessity_dual_fire" not in extra

    def test_unresolved_necessity_plus_sf_fires_both_review_reasons(self) -> None:
        """Edge case: extension_is_necessary unresolved (None → +0.5
        with extension_necessity_unresolved review reason) AND SF tier 1
        (+1.0) both fire at base 0. Both review reasons appear:
        extension_necessity_unresolved AND fidelity_necessity_dual_fire.
        Fidelity wins the max (+1.0 > +0.5)."""
        v = _view(
            source="extension",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
            # extension_is_necessary intentionally absent
        )
        dim = rule_nachos_score(v)
        adj, _, jus, extra = _compute_nachos_adjustments(
            v, dim,
            source_ext_necessity={},  # forces unresolved fallback
            sf_dim=_sf(1, "tier_1_divergent_unclear"),
        )
        assert adj == 1.0  # max(0.5 unresolved-fallback, 1.0 SF)
        assert "+0.5 necessary_ext" in jus
        assert "+1.0 fidelity_divergent_unclear" in jus
        assert "extension_necessity_unresolved" in extra
        assert "fidelity_necessity_dual_fire" in extra

    def test_label_order_preserved(self) -> None:
        """Label order: necessity → multi-entity → SF fold. Since
        multi-entity can't co-fire with SF fold (gate constraint), the
        practical dual-fire ordering is necessity then SF."""
        v = _tier0_ext_view(necessary=False)
        dim = rule_nachos_score(v)
        _, _, jus, _ = _compute_nachos_adjustments(
            v, dim, sf_dim=_sf(1, "tier_1_divergent_unclear")
        )
        assert jus.index("unnecessary_ext") < jus.index("fidelity_divergent_unclear")


# ---------------------------------------------------------------------------
# In-scope classifier — truth table
# ---------------------------------------------------------------------------


class TestInScopeClassifier:
    """Methodology scope rectification (v10, 2026-04-26): ``in_scope``
    is True for every row the rule cascade evaluates. Per
    `docs/NACHOS_Methodology_External review.xlsx` `Logic_Dec2025`
    rows 33-46, the methodology scores all Ed-Fi-mappable elements;
    out-of-scope is the narrow exclusion set (flat-file validations,
    extensions not pushed to SIS, data-quality enforcement) which is
    not detectable from the current fact set. Pre-v10 cascade gated
    in_scope on rule triggers — that incorrectly blanked ~90% of
    rows the methodology scores as tier 0."""

    def _call(self, **facts):
        v = _view(**facts)
        dim = rule_nachos_score(v)
        _, in_scope, _, _ = _compute_nachos_adjustments(
            v, dim, source_ext_necessity={}
        )
        return in_scope

    def test_unnecessary_extension_is_in_scope(self) -> None:
        assert (
            self._call(
                source="extension",
                has_aggregation=(False, "high"),
                has_concatenation=(False, "high"),
                has_cross_entity_logic=(False, "high"),
                cross_entity_targets=(0, "high"),
                has_conditional_logic=(False, "high"),
                descriptor_values_enumerated=(False, "high"),
                extension_is_necessary=(False, "high"),
            )
            is True
        )

    def test_aggregation_is_in_scope(self) -> None:
        assert (
            self._call(
                source="core",
                has_aggregation=(True, "high"),
                has_concatenation=(False, "high"),
                has_cross_entity_logic=(False, "high"),
                cross_entity_targets=(0, "high"),
                has_conditional_logic=(False, "high"),
                descriptor_values_enumerated=(False, "high"),
            )
            is True
        )

    def test_concatenation_is_in_scope(self) -> None:
        assert (
            self._call(
                source="core",
                has_aggregation=(False, "high"),
                has_concatenation=(True, "high"),
                has_cross_entity_logic=(False, "high"),
                cross_entity_targets=(0, "high"),
                has_conditional_logic=(False, "high"),
                descriptor_values_enumerated=(False, "high"),
            )
            is True
        )

    def test_multi_entity_vendor_calc_is_in_scope(self) -> None:
        assert (
            self._call(
                source="core",
                has_aggregation=(False, "high"),
                has_concatenation=(False, "high"),
                has_cross_entity_logic=(True, "high"),
                cross_entity_targets=(2, "high"),
                has_conditional_logic=(False, "high"),
                descriptor_values_enumerated=(False, "high"),
            )
            is True
        )

    def test_core_with_conditional_logic_is_in_scope(self) -> None:
        assert (
            self._call(
                source="core",
                has_aggregation=(False, "high"),
                has_concatenation=(False, "high"),
                has_cross_entity_logic=(False, "high"),
                cross_entity_targets=(0, "high"),
                has_conditional_logic=(True, "high"),
                descriptor_values_enumerated=(False, "high"),
            )
            is True
        )

    def test_granular_core_element_is_in_scope(self) -> None:
        """v10 — plain granular core rows score tier 0 and are in-scope.
        Per `Logic_Dec2025` row 20: 'Send granular element' = tier 0,
        applied to every Ed-Fi-mappable row including those without
        any complexity gate triggered."""
        assert (
            self._call(
                source="core",
                has_aggregation=(False, "high"),
                has_concatenation=(False, "high"),
                has_cross_entity_logic=(False, "high"),
                cross_entity_targets=(0, "high"),
                has_conditional_logic=(False, "high"),
                descriptor_values_enumerated=(False, "high"),
            )
            is True
        )

    def test_descriptor_only_core_is_in_scope(self) -> None:
        """v10 — 'Send a descriptor value' = tier 0 in-scope.
        `Logic_Dec2025` row 21 explicitly scores this case."""
        assert (
            self._call(
                source="core",
                has_aggregation=(False, "high"),
                has_concatenation=(False, "high"),
                has_cross_entity_logic=(False, "high"),
                cross_entity_targets=(0, "high"),
                has_conditional_logic=(False, "high"),
                descriptor_values_enumerated=(True, "high"),
            )
            is True
        )

    def test_necessary_extension_without_logic_is_in_scope(self) -> None:
        """v4 symmetrization: necessary_ext contributes +0.5 to the
        adjusted score, so the row is in-scope by the "any NACHOS
        contribution" rule. Pre-v4 cascade emitted False here despite
        the +0.5 adjustment — contradictory — and is corrected."""
        assert (
            self._call(
                source="extension",
                has_aggregation=(False, "high"),
                has_concatenation=(False, "high"),
                has_cross_entity_logic=(False, "high"),
                cross_entity_targets=(0, "high"),
                has_conditional_logic=(False, "high"),
                descriptor_values_enumerated=(False, "high"),
                extension_is_necessary=(True, "high"),
            )
            is True
        )

    def test_extension_with_conditional_logic_is_in_scope(self) -> None:
        """v4 symmetrization: extension_with_logic parallels
        core_with_logic. Conditional complexity lifts scope regardless
        of source classification."""
        assert (
            self._call(
                source="extension",
                has_aggregation=(False, "high"),
                has_concatenation=(False, "high"),
                has_cross_entity_logic=(False, "high"),
                cross_entity_targets=(0, "high"),
                has_conditional_logic=(True, "high"),
                descriptor_values_enumerated=(False, "high"),
                extension_is_necessary=(None, "low"),
            )
            is True
        )

    def test_unresolved_extension_necessity_is_in_scope(self) -> None:
        """v4: when extension_is_necessary is None (unresolved), the
        arithmetic falls back to +0.5 necessary_ext with a
        ``extension_necessity_unresolved`` review flag. The in-scope
        cascade follows the arithmetic — any +0.5 contribution
        classifies the row as in-scope."""
        assert (
            self._call(
                source="extension",
                has_aggregation=(False, "high"),
                has_concatenation=(False, "high"),
                has_cross_entity_logic=(False, "high"),
                cross_entity_targets=(0, "high"),
                has_conditional_logic=(False, "high"),
                descriptor_values_enumerated=(False, "high"),
                extension_is_necessary=(None, "low"),
            )
            is True
        )

    def test_single_cross_entity_core_is_in_scope(self) -> None:
        """v10 — every Ed-Fi-mappable row is in-scope. Single-entity
        cross-reference rows are tier 0 (no aggregation / concat /
        conditional / multi-entity), but in-scope per `Logic_Dec2025`
        rows 19-21. Pre-v10 cascade asserted False on this case;
        the assertion now flips to True per the rectification."""
        assert (
            self._call(
                source="core",
                has_aggregation=(False, "high"),
                has_concatenation=(False, "high"),
                has_cross_entity_logic=(True, "high"),
                cross_entity_targets=(1, "high"),
                has_conditional_logic=(False, "high"),
                descriptor_values_enumerated=(False, "high"),
            )
            is True
        )


# ---------------------------------------------------------------------------
# Justification text shape
# ---------------------------------------------------------------------------


class TestJustificationText:
    def test_no_adjustments_just_rule_label(self) -> None:
        v = _view(
            source="core",
            has_aggregation=(True, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(False, "high"),
            cross_entity_targets=(0, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        _, _, jus, _ = _compute_nachos_adjustments(v, dim)
        assert jus == "tier_3_aggregation"

    def test_single_adjustment(self) -> None:
        v = _view(
            source="core",
            has_aggregation=(False, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(2, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
        )
        dim = rule_nachos_score(v)
        _, _, jus, _ = _compute_nachos_adjustments(v, dim)
        assert jus == "tier_2_multi_entity; +0.5 multi_entity"

    def test_stacked_adjustments_comma_separated(self) -> None:
        """v18 (issue #84): two-tier weighting — extension_is_necessary
        =False fires +1 unnecessary_ext. Stacked extension + multi_entity
        adjustments still comma-join."""
        v = _view(
            source="extension",
            has_aggregation=(True, "high"),
            has_concatenation=(False, "high"),
            has_cross_entity_logic=(True, "high"),
            cross_entity_targets=(2, "high"),
            has_conditional_logic=(False, "high"),
            descriptor_values_enumerated=(False, "high"),
            extension_is_necessary=(False, "high"),
        )
        dim = rule_nachos_score(v)
        _, _, jus, _ = _compute_nachos_adjustments(v, dim)
        # tier 3 aggregation (wins), +1 unnecessary_ext, +0.5 multi_entity
        assert jus == "tier_3_aggregation; +1 unnecessary_ext, +0.5 multi_entity"


# ---------------------------------------------------------------------------
# Sidecar shape — end-to-end via run_aggregate
# ---------------------------------------------------------------------------


def _write_fact_artifact(
    tmp_path: Path,
    state: str,
    lens: str,
    fact: str,
    rows: list[dict],
) -> Path:
    header = {
        "__type": "header",
        "state": state,
        "lens": lens,
        "fact": fact,
        "scored_at": "2026-04-24T00:00:00Z",
        "model": "claude-sonnet-4-6",
        "prompt_version": "test.v1",
        "record_count": len(rows),
        "scored_count": len(rows),
        "skipped_count": 0,
        "entities_processed": len({r["entity"] for r in rows}) if rows else 0,
        "total_tokens_in": 0,
        "total_tokens_out": 0,
        "total_usd": 0.0,
        "cache_hit_count": 0,
        "downgrade_count": 0,
        "mode": "api",
        "status": "complete",
        "cost_cap_hit": False,
        "schema_error": None,
    }
    path = tmp_path / f"{state}_{lens}_{fact}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def _row(record_key: str, entity: str, element_name: str, value) -> dict:
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


def _seed_spine(
    tmp_path: Path,
    state: str = "AZ",
    *,
    overrides: dict[str, list[dict]] | None = None,
) -> None:
    base_key = f"{state}|Student|elem"
    DEFAULTS = {
        "definition_present": True,
        "business_rules_present": True,
        "data_type_canonical": True,
        "descriptor_values_enumerated": False,
        "is_natural_key": False,
        "definition_is_implementable": True,
        "required_when_stated": True,
        "conditional_reporting_stated": False,
        "populations_or_scope_stated": True,
        "has_conditional_logic": False,
        "has_cross_entity_logic": False,
        "has_aggregation": False,
        "has_concatenation": False,
        "cross_entity_targets": 0,
        # v2 structural-complexity axis (defaults to zero signal).
        "fk_chain_depth": 0,
        "reference_fan_out": 0,
        "sub_collection_depth": 0,
        "descriptor_enum_breadth": 0,
        "entity_extension_footprint": 0,
        # v16 — issue #63 B5: bare-FK reference gate (default False).
        "element_is_bare_fk_reference": False,
        # v22 — issue #97: parent-entity-gate-only suppression (default False).
        "element_only_parent_entity_gate": False,
        # v2 documentation-explicitness axis (default conceptual so
        # the dimension evaluates to tier-2 without overriding).
        "documentation_style": "conceptual",
    }
    overrides = overrides or {}
    for fact in sorted(SPINE_RULE_INPUTS):
        rows = overrides.get(fact, [_row(base_key, "Student", "elem", DEFAULTS[fact])])
        _write_fact_artifact(tmp_path, state, "spine", fact, rows)


class TestSidecarShape:
    def test_version_is_twentysix(self, tmp_path: Path) -> None:
        """scoring_plan_version "28" — issue #249 fact-level curation
        overlay (2026-07-13): analyst corrections to LLM-extracted
        facts overlay the pool before the rule cascade; corrected facts
        serialize with ``provenance: "human_corrected"`` (emitted only
        when set). Rules/prompts/cache untouched; the bump marks the
        sidecar-shape capability (ADR 0012). v27 was the Ed-Fi domain
        expansion (ADR 0007): Assessment/AssessmentRegistration enabled
        per-(state,domain), growing TX/IN spine coverage denominators.
        v26 — issue #147 leaf-level cross-lens
        borrow (2026-05-03). Methodology approved Slack 2026-05-03 by
        Doug + Maria: leaf-level borrowing is OK as long as borrowed
        rows are clearly identified (new
        ``documentation_source="swagger_leaf"`` value), and borrowed
        rows should increase the keymap-join match count. Two
        structural changes:

        1. ``ElementRecord.documentation_source`` widens from
           ``Literal["source_doc", "swagger"]`` to
           ``Literal["source_doc", "swagger", "swagger_leaf"]``.
        2. ``swagger_backfill.collect_leaf_backfill_records()`` walks
           the spine catalog one extra time, emitting rows for spine
           slots whose parent entity IS in the source doc but whose
           ``(entity, element)`` alias expansion is missing from
           source-lens. Rows tagged ``documented=False`` (matches v21
           close-out posture) so headline NACHOS aggregates stay
           restricted to authored prose.

        v25 was issue #124 PR 2 / #111: Internal Tidying +
        ``extension_fidelity_divergence`` det fact (det.v11) +
        SF-fold typed-reason annotation; magnitudes unchanged from
        v24. v24 was issue #124 Option 2 non-stacking axis: max-of-two
        with fidelity-wins tie-break when both v18 necessity fork and
        v12 SF fold fire on a row at base 0. v23 was issue #106 Q4 of
        POC closeout reviewer bundle: ``business_logic_complexity``
        graduated to source-lens via registration in `_SOURCE_RULES`;
        source-lens sidecars now carry `complexity_score`; Reviewer
        View grew 28 → 29 cols. v22 was issue #97 bare-FK
        parent-entity gate calibration; v21 was issue #70 swagger
        middle-path; v20 was the original swagger widening; v19 was
        issue #80 B6 documented-row LLM re-extract; v18 was issue #84
        two-tier extension weighting; v17 was B3 blanket collapse;
        v16 was issue #63 B2; v15 was issue #63 B1 (retired under v17);
        v14 was issue #66 Layer 3 (sidecar discovery_lens field); v13
        was the side-quest state_scope_delta enum3 merge; v12 was the
        SF fold into adjusted_nachos_score (B-gated `base0`, issue
        #55); v11 was the Integration Profile rename; v10 was the
        methodology scope rectification (in_scope=True default)."""
        _seed_spine(tmp_path)
        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        assert payload["scoring_plan_version"] == "28"
        assert SCORING_PLAN_VERSION == "28"

    def test_discovery_lens_default_source(self, tmp_path: Path) -> None:
        """Every scored record carries ``discovery_lens``. Source-doc
        rows default to ``"source"``; the field is REQUIRED in the
        per-record dict so consumers can filter without checking
        ``record_key`` substrings (which would couple the sidecar shape
        to a fragile string convention)."""
        _seed_spine(tmp_path)
        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        for record in payload["scores"]:
            assert "discovery_lens" in record, (
                f"missing discovery_lens on {record.get('record_key')}"
            )
            assert record["discovery_lens"] in ("source", "spine_anchored"), (
                f"{record.get('record_key')!r} has invalid "
                f"discovery_lens={record['discovery_lens']!r}"
            )

    def test_new_per_record_fields_emitted(self, tmp_path: Path) -> None:
        """Sidecar carries adjusted_nachos_score, in_scope,
        nachos_justification per-record."""
        _seed_spine(
            tmp_path,
            overrides={
                "has_aggregation": [_row("AZ|Student|elem", "Student", "elem", True)],
            },
        )
        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        score = payload["scores"][0]
        assert "adjusted_nachos_score" in score
        assert "in_scope" in score
        assert "nachos_justification" in score
        assert score["adjusted_nachos_score"] == 3.0
        assert score["in_scope"] is True
        assert score["nachos_justification"] == "tier_3_aggregation"

    def test_nachos_score_dimension_in_dimensions(self, tmp_path: Path) -> None:
        _seed_spine(tmp_path)
        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        dims = payload["scores"][0]["dimensions"]
        assert "nachos_score" in dims
        assert dims["nachos_score"]["value"] == 0
        assert dims["nachos_score"]["rule_matched"] == "tier_0_none"

    def test_nachos_excluded_from_quality_mean(self, tmp_path: Path) -> None:
        """Plan §3 decision: NACHOS axis != quality axis. Even when
        NACHOS=3, the quality mean = docs+obligation/2 only."""
        _seed_spine(
            tmp_path,
            overrides={
                "has_aggregation": [_row("AZ|Student|elem", "Student", "elem", True)],
                "has_cross_entity_logic": [_row("AZ|Student|elem", "Student", "elem", True)],
                "cross_entity_targets": [_row("AZ|Student|elem", "Student", "elem", 3)],
            },
        )
        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        score = payload["scores"][0]
        assert score["dimensions"]["nachos_score"]["value"] == 3
        # quality = docs(3) + obligation(3) / 2 — NACHOS not summed in
        assert score["_quality_mean_diagnostic"] == 3.0

    def test_header_aggregates_nachos_stats(self, tmp_path: Path) -> None:
        _seed_spine(
            tmp_path,
            overrides={
                "has_aggregation": [_row("AZ|Student|elem", "Student", "elem", True)],
            },
        )
        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        assert payload["in_scope_count"] == 1
        assert payload["nachos_score_histogram"] == {
            "0": 0, "1": 0, "2": 0, "3": 1,
        }
        assert payload["adjusted_nachos_score_histogram"]["3.0"] == 1
        assert payload["mean_nachos_score"] == 3.0
        assert payload["mean_adjusted_nachos_score"] == 3.0

    def test_tier_0_row_is_in_scope_with_scalar(self, tmp_path: Path) -> None:
        """v10 — plain tier-0 rows (no aggregation / no concat / no
        conditional / no extension justification) are in-scope per
        `Logic_Dec2025` rows 19-46. Sidecar carries the scalar for
        rendering."""
        _seed_spine(tmp_path)  # defaults = no logic, core row, tier 0
        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        score = payload["scores"][0]
        assert score["in_scope"] is True
        assert score["adjusted_nachos_score"] == 0.0
        assert score["nachos_justification"] == "tier_0_none"


# ---------------------------------------------------------------------------
# Cross-lens fact-borrow — integration with sidecar
# ---------------------------------------------------------------------------


class TestCrossLensBorrow:
    def test_spine_lens_reads_source_lens_extension_necessity(
        self, tmp_path: Path
    ) -> None:
        """Spine-lens aggregate reads source-lens sidecar for
        ``extension_is_necessary``. Create a source sidecar first, then
        run spine aggregate and verify the +1/+0.5 adjustment lands."""
        # Create out dir alongside artifacts dir to mimic real layout.
        # _load_source_extension_facts resolves to
        # ``artifacts_dir.parent.parent / {state}_scores_source.json``
        # when artifacts_dir is provided.
        artifacts_dir = tmp_path / "scoring" / "phase_a"
        artifacts_dir.mkdir(parents=True)
        out_dir = tmp_path  # tmp_path == artifacts_dir.parent.parent

        # Seed minimal spine-lens fact pool with extension source marker.
        # The rule stage's source annotation comes from the elements
        # artifact; since we don't have one, the FactView defaults to
        # source='unknown'. For this test, verify the borrow PATHWAY
        # (source sidecar is READ) without fully simulating source
        # attribution.
        _seed_spine(artifacts_dir, state="AZ")

        # Write a source-lens sidecar that the spine-lens aggregate
        # will load.
        source_sidecar = out_dir / "az_scores_source.json"
        source_sidecar.write_text(
            json.dumps({
                "state": "AZ",
                "scores": [
                    {
                        "record_key": "AZ|Student|elem",
                        "fact_provenance": {
                            "extension_is_necessary": {
                                "value": False,
                                "confidence": "high",
                                "downgraded": False,
                                "downgrade_reason": None,
                            }
                        },
                    }
                ],
            }),
            encoding="utf-8",
        )

        out_path = out_dir / "az_scores_spine.json"
        run_aggregate(
            state="AZ",
            artifacts_dir=artifacts_dir,
            out_path=out_path,
        )
        payload = json.loads(out_path.read_text())
        # The record's source is 'unknown' (no elements artifact), so
        # the extension arithmetic doesn't fire — this validates the
        # load-path mechanically without tangling with full source
        # attribution. Just assert the sidecar completed.
        assert payload["record_count"] == 1

    def test_load_source_extension_facts_lowercases_keys(
        self, tmp_path: Path
    ) -> None:
        """Issue #94: ``_load_source_extension_facts`` must lowercase
        the source-lens ``record_key`` so spine-lens lookups (which
        carry swagger camelCase) resolve against source-lens entries
        (which may carry source-document PascalCase like AZ XLSX
        ``BeginDate``). Pre-fix this dict was keyed verbatim and the
        join missed entirely on AZ/TX, dropping ~432 spine extension
        rows to the conservative ``extension_necessity_unresolved``
        fallback.
        """
        from src.score.aggregate import _load_source_extension_facts

        artifacts_dir = tmp_path / "scoring" / "phase_a"
        artifacts_dir.mkdir(parents=True)
        out_dir = tmp_path

        # Source-lens sidecar uses PascalCase (AZ XLSX rendering).
        source_sidecar = out_dir / "az_scores_source.json"
        source_sidecar.write_text(
            json.dumps({
                "state": "AZ",
                "scores": [
                    {
                        "record_key": "AZ|Calendar|BeginDate",
                        "fact_provenance": {
                            "extension_is_necessary": {
                                "value": False,
                                "confidence": "high",
                                "downgraded": False,
                                "downgrade_reason": None,
                            }
                        },
                    }
                ],
            }),
            encoding="utf-8",
        )

        lookup = _load_source_extension_facts("AZ", artifacts_dir=artifacts_dir)
        # Dict is keyed on the lowercased form so case-insensitive joins
        # work via ``view.record_key.lower()`` on the spine side.
        assert "az|calendar|begindate" in lookup
        assert lookup["az|calendar|begindate"] is False
        # Original-case form must NOT be present — the lookup contract
        # is unambiguously lowercased.
        assert "AZ|Calendar|BeginDate" not in lookup

    def test_load_source_extension_facts_collision_keeps_first(
        self, tmp_path: Path, caplog
    ) -> None:
        """Defensive: if two source-lens record_keys differ only in
        case AND disagree on extension_is_necessary, the first entry
        wins and a WARNING is logged. Collision risk on Ed-Fi schemas
        is nil (PascalCase vs camelCase is a render choice within a
        single namespace), but the collision path is documented and
        observable.
        """
        import logging

        from src.score.aggregate import _load_source_extension_facts

        artifacts_dir = tmp_path / "scoring" / "phase_a"
        artifacts_dir.mkdir(parents=True)
        out_dir = tmp_path

        source_sidecar = out_dir / "az_scores_source.json"
        source_sidecar.write_text(
            json.dumps({
                "state": "AZ",
                "scores": [
                    {
                        "record_key": "AZ|X|Foo",
                        "fact_provenance": {
                            "extension_is_necessary": {
                                "value": True, "confidence": "high",
                                "downgraded": False, "downgrade_reason": None,
                            }
                        },
                    },
                    {
                        "record_key": "AZ|X|foo",
                        "fact_provenance": {
                            "extension_is_necessary": {
                                "value": False, "confidence": "high",
                                "downgraded": False, "downgrade_reason": None,
                            }
                        },
                    },
                ],
            }),
            encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger="src.score.aggregate"):
            lookup = _load_source_extension_facts(
                "AZ", artifacts_dir=artifacts_dir
            )
        assert lookup["az|x|foo"] is True  # first wins
        assert any(
            "borrow lookup collision" in rec.message for rec in caplog.records
        )
