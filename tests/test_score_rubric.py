"""Non-drift tests for ``src.score.rubric`` — the verbal scoring vocabulary.

``rubric.py`` is presentation-layer prose consumed by the analyst
workbook's Legend sheet. These tests keep it in lockstep with the
constants it describes:

1. Every ``rule_matched`` literal the rule cascades can emit has a
   meaning (regex scan over ``rules.py`` source, exact set match).
2. Adjustment meanings are keyed by the hoisted ``ADJ_LABEL_*``
   constants, and no un-hoisted label-shaped literal hides in
   ``aggregate.py`` (a future label must be hoisted to render).
3. The ``ADJ_LABEL_*`` constants are byte-identical to the historical
   ``nachos_justification`` strings — sidecars are pinned by existing
   tests, so a changed byte here is a scoring-output change.
4. Display vocab (Match Status / Documentation Source / Structural
   Depth / Documentation Style / review routes) mirrors the analyst
   and review-queue constants value-for-value.
5. Confidence meanings match the schema enum; dimension tier meanings
   cover exactly the scored dimensions at exactly tiers 0..3; every
   meaning is a non-empty single line.
"""

from __future__ import annotations

import inspect
import re

import src.report.analyst as analyst
import src.report.review_queue as review_queue
import src.score.aggregate as aggregate
import src.score.rules as rules
from src.score import rubric
from src.score.schema import FACT_OUTPUT_SCHEMA


# ---------------------------------------------------------------------------
# 1. RULE_MEANINGS covers every rule_matched literal rules.py can emit
# ---------------------------------------------------------------------------

# Matches how rules.py writes rule labels: double-quoted string
# literals, either tier-prefixed (``"tier_3_full"``) or one of the two
# non-tier not-applicable labels (``"not_applicable"`` from
# extension_justification, ``"null_not_applicable"`` from
# semantic_fidelity). Downgrade reasons (``missing_from_artifact``,
# ``filtered_by_source``) deliberately don't match.
_RULE_LITERAL_RE = re.compile(
    r'"(tier_\d+_[a-z0-9_]+|null_not_applicable|not_applicable)"'
)


def test_rule_meanings_cover_every_emitted_rule_literal() -> None:
    source = inspect.getsource(rules)
    found = set(_RULE_LITERAL_RE.findall(source))
    # Sanity check on the scan itself — the cascades emit dozens of
    # labels; a near-empty scan means the regex drifted from how
    # rules.py writes literals, not that the labels went away.
    assert len(found) >= 15, (
        f"rule-literal scan found only {len(found)} labels — "
        "regex out of sync with rules.py?"
    )
    missing = found - set(rubric.RULE_MEANINGS)
    stale = set(rubric.RULE_MEANINGS) - found
    assert not missing, f"rule labels without a Legend meaning: {sorted(missing)}"
    assert not stale, f"Legend meanings for labels rules.py no longer emits: {sorted(stale)}"


# ---------------------------------------------------------------------------
# 2 + 3. Adjustment labels — hoisted constants, no strays, byte identity
# ---------------------------------------------------------------------------

_ADJ_LABEL_CONSTANTS: frozenset[str] = frozenset(
    {
        aggregate.ADJ_LABEL_UNNECESSARY_EXT,
        aggregate.ADJ_LABEL_NECESSARY_EXT,
        aggregate.ADJ_LABEL_MULTI_ENTITY,
        aggregate.ADJ_LABEL_SF_EXPLAINED,
        aggregate.ADJ_LABEL_SF_UNCLEAR,
    }
)


def test_adjustment_meanings_keyed_by_hoisted_constants() -> None:
    assert set(rubric.ADJUSTMENT_MEANINGS) == set(_ADJ_LABEL_CONSTANTS)


def test_no_unhoisted_adjustment_label_literals_in_aggregate() -> None:
    """Any future ``"+N.N some_label"`` literal must be a hoisted constant.

    Scans aggregate.py source for adjustment-label-shaped string
    literals outside the ``ADJ_LABEL_*`` constant definitions. A hit
    means someone in-lined a new justification label instead of
    hoisting it — the Legend (and this suite) would silently miss it.
    """
    source = inspect.getsource(aggregate)
    non_constant_lines = [
        line
        for line in source.splitlines()
        if not re.match(r"^ADJ_LABEL_[A-Z_]+(?:\s*:\s*str)?\s*=", line)
    ]
    label_shaped = re.compile(r'"\+\d(?:\.\d)? [a-z_]+')
    offending = [ln for ln in non_constant_lines if label_shaped.search(ln)]
    assert offending == [], (
        "adjustment-label literal(s) found outside the ADJ_LABEL_* "
        f"constants — hoist them: {offending}"
    )


def test_adjustment_label_byte_identity() -> None:
    """The hoist must not have moved a byte — sidecars pin these strings."""
    assert aggregate.ADJ_LABEL_UNNECESSARY_EXT == "+1 unnecessary_ext"
    assert aggregate.ADJ_LABEL_NECESSARY_EXT == "+0.5 necessary_ext"
    assert aggregate.ADJ_LABEL_MULTI_ENTITY == "+0.5 multi_entity"
    assert aggregate.ADJ_LABEL_SF_EXPLAINED == "+0.5 fidelity_divergent_explained"
    assert aggregate.ADJ_LABEL_SF_UNCLEAR == "+1.0 fidelity_divergent_unclear"


# ---------------------------------------------------------------------------
# 4. Lockstep with the analyst / review-queue display vocab
# ---------------------------------------------------------------------------


def test_match_status_meanings_mirror_analyst_display_values() -> None:
    assert set(rubric.MATCH_STATUS_MEANINGS) == set(
        analyst._MATCH_STATUS_BY_SOURCE.values()
    )


def test_doc_source_meanings_mirror_analyst_display_values() -> None:
    assert set(rubric.DOC_SOURCE_MEANINGS) == set(
        analyst._DOC_SOURCE_DISPLAY.values()
    )


def test_structural_depth_meanings_mirror_analyst_labels() -> None:
    assert set(rubric.STRUCTURAL_DEPTH_MEANINGS) == set(
        analyst._STRUCTURAL_DEPTH_LABELS.values()
    )


def test_doc_style_meanings_mirror_analyst_display_values() -> None:
    assert set(rubric.DOC_STYLE_MEANINGS) == set(
        analyst._DOC_STYLE_DISPLAY.values()
    )


def test_route_descriptions_mirror_routes() -> None:
    assert set(review_queue.ROUTE_DESCRIPTIONS) == set(review_queue.ROUTES)


# ---------------------------------------------------------------------------
# 5. Confidence vocabulary matches the schema enum
# ---------------------------------------------------------------------------


def test_confidence_meanings_match_schema_enum() -> None:
    schema_enum = FACT_OUTPUT_SCHEMA["items"]["properties"]["confidence"]["enum"]
    assert set(rubric.CONFIDENCE_MEANINGS) == set(schema_enum)


# ---------------------------------------------------------------------------
# 6. Dimension tier meanings cover exactly the scored dimensions, 0..3
# ---------------------------------------------------------------------------


def test_dimension_tier_meanings_cover_scored_dimensions() -> None:
    expected = (
        set(analyst._SOURCE_DIM_ORDER) | set(analyst._SPINE_DIM_ORDER)
    ) - {"nachos_score"}
    assert set(rubric.DIMENSION_TIER_MEANINGS) == expected


def test_dimension_tier_meanings_have_exactly_tiers_0_to_3() -> None:
    for dim, tiers in rubric.DIMENSION_TIER_MEANINGS.items():
        assert [tier for tier, _ in tiers] == [0, 1, 2, 3], (
            f"{dim} must carry exactly tiers 0..3 in order"
        )


def test_nachos_tier_meanings_have_exactly_tiers_0_to_3() -> None:
    assert [tier for tier, _ in rubric.NACHOS_TIER_MEANINGS] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# 7. Every meaning string is non-empty and single-line
# ---------------------------------------------------------------------------


def _all_meaning_strings():
    yield from (meaning for _, meaning in rubric.NACHOS_TIER_MEANINGS)
    for tiers in rubric.DIMENSION_TIER_MEANINGS.values():
        yield from (meaning for _, meaning in tiers)
    yield from rubric.RULE_MEANINGS.values()
    yield from rubric.ADJUSTMENT_MEANINGS.values()
    yield rubric.ADJUSTMENT_NOTE
    yield from rubric.CONFIDENCE_MEANINGS.values()
    yield from rubric.MATCH_STATUS_MEANINGS.values()
    yield from rubric.DOC_SOURCE_MEANINGS.values()
    yield from rubric.STRUCTURAL_DEPTH_MEANINGS.values()
    yield from rubric.DOC_STYLE_MEANINGS.values()
    yield from review_queue.ROUTE_DESCRIPTIONS.values()


def test_every_meaning_is_non_empty_and_single_line() -> None:
    for meaning in _all_meaning_strings():
        assert isinstance(meaning, str)
        assert meaning.strip(), "empty Legend meaning"
        assert "\n" not in meaning, f"multi-line Legend meaning: {meaning[:60]}…"


# ---------------------------------------------------------------------------
# 8. Prose justification renderer (Option B template-native workbook)
# ---------------------------------------------------------------------------


def test_nachos_rule_short_covers_exactly_the_nachos_cascade() -> None:
    """NACHOS_RULE_SHORT keys == the literals ``nachos_score`` emits.

    Same source-scan approach as the RULE_MEANINGS test, scoped to the
    nachos cascade function. Docstring mentions use double backticks,
    not double quotes, so the scan sees only real string literals.
    """
    source = inspect.getsource(rules.nachos_score)
    found = set(_RULE_LITERAL_RE.findall(source))
    assert len(found) >= 5, (
        f"nachos-cascade scan found only {len(found)} labels — "
        "regex out of sync with rules.py?"
    )
    missing = found - set(rubric.NACHOS_RULE_SHORT)
    stale = set(rubric.NACHOS_RULE_SHORT) - found
    assert not missing, f"nachos tokens without a short clause: {sorted(missing)}"
    assert not stale, f"short clauses for tokens the cascade no longer emits: {sorted(stale)}"


def test_nachos_rule_short_keys_are_known_rule_meanings() -> None:
    """Every short clause compresses a full RULE_MEANINGS sentence."""
    assert set(rubric.NACHOS_RULE_SHORT) <= set(rubric.RULE_MEANINGS)


def test_adjustment_short_keyed_by_hoisted_constants() -> None:
    assert set(rubric.ADJUSTMENT_SHORT) == set(rubric.ADJUSTMENT_MEANINGS)
    assert set(rubric.ADJUSTMENT_SHORT) == set(_ADJ_LABEL_CONSTANTS)


def test_adjustment_reason_short_covers_typed_reason_atoms() -> None:
    """Reason phrases stay in lockstep with the atoms aggregate renders.

    The parenthetical atoms are the non-None values of the
    ``_SF_TYPED_REASON_ENUMS`` value maps (NOT the raw schema enum
    labels — e.g. ``narrows`` renders as ``narrows_core_scope``).
    """
    atoms = {
        reason
        for _, value_map in aggregate._SF_TYPED_REASON_ENUMS
        for reason in value_map.values()
        if reason is not None
    }
    assert set(rubric.ADJUSTMENT_REASON_SHORT) == atoms


def test_render_prose_base_only() -> None:
    assert (
        rubric.render_justification_prose("tier_1_conditional", 1)
        == "Base 1 (one conditional decides the value)"
    )


def test_render_prose_base_plus_one_adjustment() -> None:
    assert (
        rubric.render_justification_prose("tier_0_none; +0.5 necessary_ext", 0)
        == "Base 0 (no derivation logic); +0.5 necessary extension"
    )


def test_render_prose_typed_reason_parenthetical_with_internal_comma() -> None:
    """The comma INSIDE the parenthetical must not split the label."""
    prose = rubric.render_justification_prose(
        "tier_0_none; +1.0 fidelity_divergent_unclear "
        "(replaces_core_field_shape, transformation)",
        0,
    )
    assert prose == (
        "Base 0 (no derivation logic); "
        "+1.0 meaning diverges from Ed-Fi (unexplained) "
        "(replaces a core field's shape, transformation required)"
    )


def test_render_prose_unmapped_reason_atom_falls_back_to_spaces() -> None:
    prose = rubric.render_justification_prose(
        "tier_0_none; +0.5 fidelity_divergent_explained (future_new_reason)",
        0,
    )
    assert prose == (
        "Base 0 (no derivation logic); "
        "+0.5 meaning diverges from Ed-Fi (explained) (future new reason)"
    )


def test_render_prose_dual_fire_suffix_at_base_zero() -> None:
    prose = rubric.render_justification_prose(
        "tier_0_none; +1 unnecessary_ext, +1.0 fidelity_divergent_unclear",
        0,
    )
    assert prose == (
        "Base 0 (no derivation logic); +1.0 unnecessary extension; "
        "+1.0 meaning diverges from Ed-Fi (unexplained)"
        " — larger adjustment applies, not the sum"
    )


def test_render_prose_dual_fire_suffix_with_parenthetical_label() -> None:
    """The fidelity label still counts as dual-fire when annotated."""
    prose = rubric.render_justification_prose(
        "tier_0_descriptor; +0.5 necessary_ext, "
        "+0.5 fidelity_divergent_explained (narrows_core_scope)",
        0,
    )
    assert prose is not None
    assert prose.endswith(" — larger adjustment applies, not the sum")


def test_render_prose_no_dual_fire_suffix_above_base_zero() -> None:
    prose = rubric.render_justification_prose(
        "tier_1_conditional; +1 unnecessary_ext, "
        "+1.0 fidelity_divergent_unclear",
        1,
    )
    assert prose is not None
    assert "larger adjustment applies" not in prose


def test_render_prose_no_dual_fire_suffix_without_both_families() -> None:
    prose = rubric.render_justification_prose(
        "tier_0_none; +1 unnecessary_ext, +0.5 multi_entity", 0
    )
    assert prose == (
        "Base 0 (no derivation logic); +1.0 unnecessary extension; "
        "+0.5 multiple entities involved"
    )


def test_render_prose_unknown_tokens_pass_through_verbatim() -> None:
    assert (
        rubric.render_justification_prose("tier_9_mystery", 2)
        == "Base 2 (tier_9_mystery)"
    )
    assert (
        rubric.render_justification_prose("tier_0_none; +7 mystery_label", 0)
        == "Base 0 (no derivation logic); +7 mystery_label"
    )


def test_render_prose_none_and_empty_return_none() -> None:
    assert rubric.render_justification_prose(None, 0) is None
    assert rubric.render_justification_prose("", 0) is None


def test_render_prose_base_tier_none_renders_short_clause_only() -> None:
    assert (
        rubric.render_justification_prose("tier_0_none", None)
        == "no derivation logic"
    )


def test_render_prose_multi_adjustment_ordering_preserved() -> None:
    """Necessity → multi-entity → SF fold, exactly as the sidecar renders."""
    prose = rubric.render_justification_prose(
        "tier_0_none; +1 unnecessary_ext, +0.5 multi_entity, "
        "+1.0 fidelity_divergent_unclear (transformation, field_filter)",
        0,
    )
    assert prose == (
        "Base 0 (no derivation logic); +1.0 unnecessary extension; "
        "+0.5 multiple entities involved; "
        "+1.0 meaning diverges from Ed-Fi (unexplained) "
        "(transformation required, cross-field filter applies)"
        " — larger adjustment applies, not the sum"
    )


def test_every_short_meaning_is_non_empty_and_single_line() -> None:
    for meaning in (
        list(rubric.NACHOS_RULE_SHORT.values())
        + list(rubric.ADJUSTMENT_SHORT.values())
        + list(rubric.ADJUSTMENT_REASON_SHORT.values())
    ):
        assert isinstance(meaning, str)
        assert meaning.strip(), "empty short meaning"
        assert "\n" not in meaning, f"multi-line short meaning: {meaning[:60]}…"


# ---------------------------------------------------------------------------
# split_adjustment_labels — structured counterpart of the prose renderer
# (issue #248 override clustering)
# ---------------------------------------------------------------------------


def test_split_labels_none_empty_and_bare_rule_return_empty() -> None:
    assert rubric.split_adjustment_labels(None) == []
    assert rubric.split_adjustment_labels("") == []
    assert rubric.split_adjustment_labels("tier_0_none") == []


def test_split_labels_single() -> None:
    assert rubric.split_adjustment_labels(
        "tier_0_none; +0.5 necessary_ext"
    ) == ["+0.5 necessary_ext"]


def test_split_labels_multi_preserves_emitted_order() -> None:
    assert rubric.split_adjustment_labels(
        "tier_0_none; +1 unnecessary_ext, +0.5 multi_entity"
    ) == ["+1 unnecessary_ext", "+0.5 multi_entity"]


def test_split_labels_strips_parenthetical_with_internal_comma() -> None:
    # The comma INSIDE the typed-reason parenthetical must neither
    # split the label nor survive into the bare label.
    assert rubric.split_adjustment_labels(
        "tier_0_none; +0.5 necessary_ext, "
        "+1.0 fidelity_divergent_unclear "
        "(replaces_core_field_shape, transformation)"
    ) == ["+0.5 necessary_ext", "+1.0 fidelity_divergent_unclear"]
