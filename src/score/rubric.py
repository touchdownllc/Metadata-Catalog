"""Verbal scoring vocabulary — the single home for what the scores mean.

Human-readable meanings for every token the scoring pipeline surfaces
on analyst-facing sheets: NACHOS tiers, per-dimension tiers, rule
labels (``rule_matched``), NACHOS adjustment labels, confidence levels,
and the workbook display vocab (Match Status, Documentation Source,
Structural Depth, Documentation Style). Consumed by the analyst
workbook's Legend sheet (``src.report.analyst``). Also home to the
prose-justification renderer (``render_justification_prose``) that
turns the sidecar's ``nachos_justification`` token string into a
readable sentence for the Option B template-native workbook.

This module is PRESENTATION-LAYER PROSE ONLY. It must never be
imported by ``rules.py`` / ``aggregate.py`` / ``deterministic.py`` —
the dependency is one-way (rubric may import constants FROM score
modules; report code imports rubric). Nothing here participates in
scoring arithmetic, so editing a meaning never moves a score.

Non-drift tests in ``tests/test_score_rubric.py`` keep this module in
lockstep with the rule/schema constants it describes: every rule label
the cascades can emit has a meaning here, the adjustment labels are
keyed by the exact ``nachos_justification`` strings
(``aggregate.ADJ_LABEL_*``), and the display vocab mirrors the
analyst/report constants value-for-value.

Wording discipline: plain English for education-data analysts, one
sentence per entry, no jargon token without expansion. Meanings are
derived from the rule bodies in ``src.score.rules``, the adjustment
arithmetic in ``src.score.aggregate``, and the NACHOS methodology
tiers in ``docs/archive/scoring-plan.md`` §11 — not invented.
"""

from __future__ import annotations

from src.score.aggregate import (
    ADJ_LABEL_MULTI_ENTITY,
    ADJ_LABEL_NECESSARY_EXT,
    ADJ_LABEL_SF_EXPLAINED,
    ADJ_LABEL_SF_UNCLEAR,
    ADJ_LABEL_UNNECESSARY_EXT,
)

# ---------------------------------------------------------------------------
# NACHOS complexity tiers (methodology axis, 0..3)
# ---------------------------------------------------------------------------

# Source: docs/archive/scoring-plan.md §11 (Dec 2025 methodology spec,
# `Logic_Dec2025` rows 19-46) and the `nachos_score` cascade in
# src.score.rules. Tier 0 is valid output ("Send granular element /
# descriptor"), not a missing value.
NACHOS_TIER_MEANINGS: tuple[tuple[int, str], ...] = (
    (
        0,
        "Send the granular element or descriptor value as-is — a "
        "pass-through with no derivation logic beyond, at most, one "
        "simple IF.",
    ),
    (
        1,
        "One conditional (IF) statement using data within the same "
        "entity decides the value or whether it is reported.",
    ),
    (
        2,
        "The value is calculated from two or more entities, but "
        "without any aggregation or transformation.",
    ),
    (
        3,
        "The value requires aggregation or transformation — for "
        "example a SUM/COUNT across records or a CONCATENATE that "
        "assembles the value from multiple pieces.",
    ),
)


# ---------------------------------------------------------------------------
# Per-dimension tier meanings (quality / cost dimensions, 0..3)
# ---------------------------------------------------------------------------

# One tuple of (tier, meaning) per scored dimension, distilled from the
# rule cascades in src.score.rules. ``nachos_score`` is covered by
# NACHOS_TIER_MEANINGS above. Note ``business_logic_complexity`` is a
# cost axis: higher = costlier to integrate, NOT better.
DIMENSION_TIER_MEANINGS: dict[str, tuple[tuple[int, str], ...]] = {
    "canonical_name_alignment": (
        (0, "The element could not be matched to any Ed-Fi spine slot, so there is no canonical name to compare against."),
        (1, "The element matched an Ed-Fi spine slot but under a genuinely different name (a rename)."),
        (2, "A core element whose name differs from the canonical Ed-Fi name only in casing or other cosmetics."),
        (3, "The element's name matches the canonical Ed-Fi name exactly."),
    ),
    "definition_quality": (
        (0, "No definition text is present for the element."),
        (1, "A definition is present but thin — below the substantive-length threshold."),
        (2, "A substantive definition (60 or more characters) is present, though not confirmed to add detail beyond Ed-Fi."),
        (3, "The state's definition adds detail beyond the Ed-Fi standard definition."),
    ),
    "semantic_fidelity": (
        (0, "The state's meaning could not be resolved against Ed-Fi (the classifier fact was missing or downgraded) — flagged for review."),
        (1, "The state's meaning diverges from the Ed-Fi standard and the direction of divergence is not explained."),
        (2, "The state's meaning diverges from Ed-Fi but the documentation explains how — it narrows or broadens the Ed-Fi scope."),
        (3, "The state's meaning for the element is aligned with the Ed-Fi standard."),
    ),
    "extension_justification": (
        (0, "The extension was judged unnecessary and its name mirrors an existing core pattern — the strongest sign it duplicates core Ed-Fi."),
        (1, "Evidence about the extension's necessity or standalone design is partial or uncertain."),
        (2, "The extension was judged necessary but depends on other extensions (a companion design)."),
        (3, "The extension was judged necessary and standalone (a self-contained design)."),
    ),
    "documentation_completeness": (
        (0, "No definition is present for the element."),
        (1, "A definition and a canonical data type are present, but no business rules."),
        (2, "Definition, business rules, and a canonical data type are all present, but the narrative was not confirmed as implementable."),
        (3, "Definition, business rules, and canonical data type are all present, and a vendor could implement the field from the narrative alone (or from its enumerated descriptor values)."),
    ),
    "obligation_clarity": (
        (0, "The documentation says nothing about when or for whom the element must be reported."),
        (1, "Partial obligation detail — it states when the element is required or which populations it covers, but not both."),
        (2, "The documentation states conditional reporting rules — the element is reported only under stated conditions."),
        (3, "The documentation states both when the element is required and which populations or scope it applies to."),
    ),
    "business_logic_complexity": (
        (0, "No conditional, cross-entity, or aggregation logic — nothing to do beyond sending the value (higher tiers mean costlier integration, not better rows)."),
        (1, "Conditional logic or a single cross-entity reference is involved."),
        (2, "Aggregation is involved, or the logic reaches into two or more other entities."),
        (3, "Both aggregation and cross-entity logic are involved — the costliest combination to integrate."),
    ),
}


# ---------------------------------------------------------------------------
# Rule labels (``rule_matched``) — one entry per branch the cascades emit
# ---------------------------------------------------------------------------

# Every string literal the rule cascades in src.score.rules can assign
# to ``DimensionScore.rule_matched``. A handful of labels are shared by
# more than one dimension (e.g. ``tier_0_none``); their wording covers
# every use. The non-drift test regex-scans rules.py and asserts this
# dict matches the emitted set exactly.
RULE_MEANINGS: dict[str, str] = {
    # -- Shared fall-through / not-applicable labels ---------------------
    "tier_0_missing": "No definition text is present for this element.",
    "tier_0_none": "None of the signals this dimension looks for are present — the fall-through zero tier.",
    "tier_0_unresolved": "The rule could not resolve its inputs for this row (no spine match, or a missing/downgraded classifier fact) — flagged for review rather than scored on evidence.",
    "tier_1_minimal": "A definition is present but little else — minimal documentation without business rules or substantive supporting detail.",
    "tier_1_partial": "Only part of the evidence this dimension needs is present — enough for a low tier but not a higher one.",
    "not_applicable": "The row is not an extension, so extension justification does not apply — the dimension is excluded from the record's mean rather than scored zero.",
    "null_not_applicable": "Semantic fidelity does not apply to this row (for example an extension with no core counterpart) — excluded from the record's mean rather than scored zero.",
    # -- documentation_completeness (spine lens) -------------------------
    "tier_3_full": "Definition, business rules, and canonical data type are all present, and the definition alone is enough to implement the field.",
    "tier_3_descriptor_enum": "Definition, business rules, and canonical data type are all present, and the row's enumerated descriptor values serve as the implementation contract.",
    "tier_2_structure": "Definition, business rules, and canonical data type are present, but the narrative was not confirmed as implementable.",
    # -- obligation_clarity (spine lens) ----------------------------------
    "tier_3_required_scoped": "The documentation states both when the element is required and the populations or scope it applies to.",
    "tier_2_conditional": "The documentation states conditional reporting rules — the element is reported only under stated conditions.",
    "tier_0_descriptor_enum": "No obligation statements — what looked like population scope was an enumerated descriptor value list, which does not count as reporting scope.",
    # -- business_logic_complexity (both lenses; higher = costlier) ------
    "tier_3_agg_cross": "The row carries both aggregation and confirmed cross-entity logic — the costliest combination to integrate.",
    "tier_2_agg_or_multicross": "The row carries aggregation alone, or cross-entity logic that touches two or more distinct entities.",
    "tier_1_cond_or_cross": "The row carries conditional logic or a single-target cross-entity reference.",
    # -- structural_depth (Integration Profile, both lenses) -------------
    "tier_3_deep_fk_chain": "The element sits behind a foreign-key chain three or more hops deep — multi-hop referential-integrity work for the vendor.",
    "tier_3_natural_key_high_fan_out": "A natural-key (identity) element on an entity that five or more other entities reference — changing it ripples widely.",
    "tier_3_wide_descriptor": "The element carries a very wide descriptor value set (40 or more values) that vendors must handle.",
    "tier_2_medium_fk_chain": "The element sits behind a foreign-key chain two hops deep.",
    "tier_2_fan_out": "The element's entity is referenced by at least two other entities.",
    "tier_2_natural_key_sub_collection": "A natural-key (identity) element that lives inside a sub-collection.",
    "tier_2_heavy_extension_footprint": "The element's entity carries two or more state extensions.",
    "tier_2_broad_descriptor": "The element carries a broad descriptor value set (20 to 39 values).",
    "tier_1_natural_key": "The element is part of its entity's natural key, with no other structural signals.",
    "tier_1_fk_chain": "The element sits behind a single-hop foreign-key chain.",
    "tier_1_sub_collection": "The element lives inside one sub-collection.",
    "tier_1_extension_present": "The element's entity carries one state extension.",
    "tier_1_descriptor_breadth": "The element carries a small descriptor value set (5 to 19 values).",
    "tier_0_flat": "No structural signals — a plain scalar field on a root entity with no key role, references, extensions, or descriptor values.",
    # -- documentation_style_tier (Integration Profile, both lenses) -----
    "tier_3_prescriptive": "The narrative prescribes how to populate the field (format, assembly recipe, or pattern) — a vendor can build the value from the narrative alone.",
    "tier_2_conceptual": "The narrative describes what the element means but gives no assembly recipe — the vendor must decide the format.",
    "tier_1_cross_reference": "The narrative points to another system or document as the authority — the vendor must follow the pointer.",
    "tier_1_regulatory": "The narrative cites a statute, rule, or code without describing the field — the vendor must read the citation.",
    "tier_0_unspecified": "The narrative carries no usable information about the field (shown as Silent on the workbook) — the vendor reverse-engineers it from the field name.",
    # -- documentation_gap (Integration Profile second pass) -------------
    "tier_1_gap": "The documentation gap fired — the row carries real structural weight but the state's narrative does not prescribe how to populate it.",
    "tier_0_documented": "The row carries real structural weight and the narrative documents it well enough — no gap.",
    "tier_0_low_complexity": "The row is structurally light, so it does not need prescriptive documentation — no gap regardless of narrative style.",
    # -- nachos_score (methodology axis, both lenses) ---------------------
    "tier_3_aggregation": "The value requires aggregation (SUM/COUNT/AVG-style logic) — NACHOS tier 3.",
    "tier_3_concatenation": "The value is assembled by concatenating pieces together (a CONCATENATE-style derivation) — NACHOS tier 3.",
    "tier_2_multi_entity": "The value is calculated from two or more entities without aggregation or transformation — NACHOS tier 2.",
    "tier_1_conditional": "A conditional (IF) within one entity, or a single-target cross-entity lookup, decides the value — NACHOS tier 1.",
    "tier_0_descriptor": "The row sends an enumerated descriptor value with no derivation logic — NACHOS tier 0.",
    "tier_0_natural_key_format": "A state-mandated composite format on a natural-key identifier — a key-format specification the LEA composes once and vendors pass through, not a concatenation derivation, so NACHOS tier 0.",
    # -- canonical_name_alignment (source lens) ---------------------------
    "tier_3_exact": "The element's name matches the canonical Ed-Fi name exactly.",
    "tier_2_cosmetic": "A core element whose name differs from the canonical Ed-Fi name only in casing or other cosmetics.",
    "tier_1_resolved": "The element matched an Ed-Fi spine slot but under a genuinely different name (a rename).",
    # -- definition_quality (source lens) ----------------------------------
    "tier_3_detail_beyond_edfi": "The state's definition adds detail beyond the Ed-Fi standard definition.",
    "tier_2_substantive": "A substantive definition (60 or more characters) is present, though not confirmed to add detail beyond Ed-Fi.",
    # -- semantic_fidelity (source lens) -----------------------------------
    "tier_3_aligned": "The state's meaning for the element is aligned with the Ed-Fi standard.",
    "tier_2_divergent_explained": "The state's meaning diverges from Ed-Fi but the documentation explains how — it narrows or broadens the Ed-Fi scope.",
    "tier_1_divergent_unclear": "The state's meaning diverges from Ed-Fi without a clear explanation of how.",
    # -- extension_justification (source lens, extension rows only) --------
    "tier_0_unnecessary_mirror": "The extension was judged unnecessary and its name mirrors an existing core pattern — the strongest sign it duplicates core Ed-Fi.",
    "tier_3_necessary_standalone": "The extension was judged necessary and standalone (a self-contained design).",
    "tier_2_necessary_companion": "The extension was judged necessary but depends on other extensions (a companion design).",
}


# ---------------------------------------------------------------------------
# NACHOS adjustment labels (rendered in ``nachos_justification``)
# ---------------------------------------------------------------------------

# Keyed by the exact strings ``aggregate._compute_nachos_adjustments``
# renders — imported from the hoisted constants so the Legend can never
# drift from the sidecar text.
ADJUSTMENT_MEANINGS: dict[str, str] = {
    ADJ_LABEL_UNNECESSARY_EXT: "Adds 1.0 to the base NACHOS tier because the row is a state extension that the necessity judgment found unnecessary.",
    ADJ_LABEL_NECESSARY_EXT: "Adds 0.5 to the base NACHOS tier because the row is a state extension judged necessary — also the conservative fallback when necessity could not be resolved (those rows carry an extension_necessity_unresolved review flag).",
    ADJ_LABEL_MULTI_ENTITY: "Adds 0.5 because confirmed cross-entity logic touches two or more entities — an independent adjustment that always stacks on top of the others.",
    ADJ_LABEL_SF_EXPLAINED: "Adds 0.5 on source-lens rows whose base NACHOS tier is 0, when the state's meaning diverges from Ed-Fi with an explained direction (semantic fidelity tier 2).",
    ADJ_LABEL_SF_UNCLEAR: "Adds 1.0 on source-lens rows whose base NACHOS tier is 0, when the state's meaning diverges from Ed-Fi without a clear explanation (semantic fidelity tier 1 or 0).",
}

# Two reading notes for the Justification column, covering label shapes
# the entries above don't spell out. Single line by design — the Legend
# renders it as one cell.
ADJUSTMENT_NOTE: str = (
    "Reading notes: (1) a fidelity label may carry a parenthetical "
    "suffix naming the documented divergence mechanisms, e.g. "
    "'+1.0 fidelity_divergent_unclear (replaces_core_field_shape, "
    "transformation)' — these typed reasons come from the deterministic "
    "shape-divergence fact (det.v11) plus the state-scope-delta and "
    "sourcing-constraint facts (docs/adr/0003) and are informational "
    "only, never changing the magnitude; (2) since v24 (docs/adr/0002), "
    "when an extension-necessity adjustment and a fidelity fold both "
    "fire on the same row at base tier 0, the adjusted score takes the "
    "LARGER of the two (fidelity wins ties) instead of their sum — both "
    "labels still render for audit, so do not mentally sum the labels; "
    "such rows carry a fidelity_necessity_dual_fire review flag, the "
    "multi-entity adjustment still adds separately, and the adjusted "
    "score is capped at 4.5."
)


# ---------------------------------------------------------------------------
# Prose justification renderer (Option B template-native workbook)
# ---------------------------------------------------------------------------

# Short clauses — a few words each — for the tokens the ``nachos_score``
# cascade in src.score.rules emits as ``rule_matched``. Embedded in the
# rendered base clause as e.g. "Base 2 (calculated from multiple
# entities)". Ordered to mirror the cascade's first-match-wins order.
# Wording is the compressed form of the full sentences in RULE_MEANINGS;
# the non-drift test asserts this key set matches the nachos cascade's
# emitted literals exactly.
NACHOS_RULE_SHORT: dict[str, str] = {
    "tier_3_aggregation": "requires aggregation",
    "tier_0_natural_key_format": "state key format, passed through by vendors",
    "tier_3_concatenation": "assembled by concatenation",
    "tier_2_multi_entity": "calculated from multiple entities",
    "tier_1_conditional": "one conditional decides the value",
    "tier_0_descriptor": "descriptor pass-through",
    "tier_0_none": "no derivation logic",
}

# Short prose for each adjustment label — keyed by the exact
# ``aggregate.ADJ_LABEL_*`` strings, mirroring ADJUSTMENT_MEANINGS, so
# the prose renderer can never drift from the sidecar text.
ADJUSTMENT_SHORT: dict[str, str] = {
    ADJ_LABEL_UNNECESSARY_EXT: "+1.0 unnecessary extension",
    ADJ_LABEL_NECESSARY_EXT: "+0.5 necessary extension",
    ADJ_LABEL_MULTI_ENTITY: "+0.5 multiple entities involved",
    ADJ_LABEL_SF_EXPLAINED: "+0.5 meaning diverges from Ed-Fi (explained)",
    ADJ_LABEL_SF_UNCLEAR: "+1.0 meaning diverges from Ed-Fi (unexplained)",
}

# Human phrases for the typed-reason parenthetical atoms a fidelity
# label may carry (the reason strings ``aggregate._SF_TYPED_REASON_ENUMS``
# renders — det.v11 shape divergence, the state-scope-delta enum, and
# the issue #59 sourcing-constraint enum). Unmapped atoms fall back to
# underscore→space in the renderer, so a future enum value degrades
# readably instead of crashing.
ADJUSTMENT_REASON_SHORT: dict[str, str] = {
    "replaces_core_field_shape": "replaces a core field's shape",
    "narrows_core_scope": "narrows the Ed-Fi scope",
    "broadens_core_scope": "broadens the Ed-Fi scope",
    "transformation": "transformation required",
    "field_filter": "cross-field filter applies",
    "external_sourcing": "sourced from a separate authority",
    "custom_enumeration": "state-defined value set",
}

# Label sets for the v24 non-stacking honesty suffix: when an
# extension-necessity label and a fidelity label both render at base
# tier 0, the applied adjustment is the LARGER of the two, not the sum
# (see ADJUSTMENT_NOTE and aggregate._compute_nachos_adjustments).
_EXTENSION_ADJ_LABELS: frozenset[str] = frozenset(
    {ADJ_LABEL_UNNECESSARY_EXT, ADJ_LABEL_NECESSARY_EXT}
)
_FIDELITY_ADJ_LABELS: frozenset[str] = frozenset(
    {ADJ_LABEL_SF_EXPLAINED, ADJ_LABEL_SF_UNCLEAR}
)

_DUAL_FIRE_SUFFIX: str = " — larger adjustment applies, not the sum"


def _split_adjustment_csv(adj_csv: str) -> list[str]:
    """Split the adjustment list on ``", "`` at paren depth 0 only.

    A fidelity label's typed-reason parenthetical carries commas of its
    own (e.g. ``"+1.0 fidelity_divergent_unclear
    (replaces_core_field_shape, transformation)"``) — those must not
    split the label.
    """
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(adj_csv):
        ch = adj_csv[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif depth == 0 and adj_csv.startswith(", ", i):
            parts.append(adj_csv[start:i])
            start = i + 2
            i += 2
            continue
        i += 1
    parts.append(adj_csv[start:])
    return parts


def _split_trailing_parenthetical(label: str) -> tuple[str, str | None]:
    """Return ``(bare_label, parenthetical_body)``.

    ``parenthetical_body`` is the text inside a trailing ``" (...)"``
    suffix, or ``None`` when the label carries no parenthetical.
    """
    if label.endswith(")"):
        open_at = label.rfind(" (")
        if open_at != -1:
            return label[:open_at], label[open_at + 2 : -1]
    return label, None


def _render_adjustment_clause(label: str) -> str:
    """Render one adjustment label as short prose.

    Unknown labels pass through verbatim; a typed-reason parenthetical
    is re-appended with each atom translated through
    ADJUSTMENT_REASON_SHORT (fallback: underscore→space).
    """
    bare, paren = _split_trailing_parenthetical(label)
    short = ADJUSTMENT_SHORT.get(bare, bare)
    if paren is None:
        return short
    atoms = paren.split(", ")
    translated = ", ".join(
        ADJUSTMENT_REASON_SHORT.get(atom, atom.replace("_", " "))
        for atom in atoms
    )
    return f"{short} ({translated})"


def render_adjustments_prose(token_str: str | None) -> str | None:
    """Render ONLY the adjustment clauses of a ``nachos_justification``
    token string as prose (the Details "Score Adjustments" component
    column — base tier renders separately as its own column).

    ``None`` when the row carries no adjustments. Presentation only;
    same total behavior as :func:`render_justification_prose` (unknown
    labels render verbatim).
    """
    if not token_str:
        return None
    _base, sep, adj_csv = token_str.partition("; ")
    if not sep or not adj_csv:
        return None
    labels = _split_adjustment_csv(adj_csv)
    clauses = [_render_adjustment_clause(label) for label in labels]
    return "; ".join(clauses) or None


def split_adjustment_labels(token_str: str | None) -> list[str]:
    """Bare adjustment labels parsed from a full ``nachos_justification``
    token string, typed-reason parentheticals stripped.

    The structured counterpart of :func:`render_adjustments_prose` for
    consumers that need the labels themselves rather than prose (issue
    #248 override clustering). Parentheticals are dropped by design:
    typed reasons are row-specific annotations, and keeping them would
    shatter clusters that dispute the same adjustment. ``[]`` when the
    row carries no adjustments. Same total behavior as the prose
    renderers — unknown labels pass through verbatim, never raises.
    """
    if not token_str:
        return []
    _base, sep, adj_csv = token_str.partition("; ")
    if not sep or not adj_csv:
        return []
    return [
        _split_trailing_parenthetical(label)[0]
        for label in _split_adjustment_csv(adj_csv)
    ]


def render_justification_prose(
    token_str: str | None, base_tier: int | None
) -> str | None:
    """Render a sidecar ``nachos_justification`` token string as prose.

    ``token_str`` is the ``"{rule_matched}"`` or
    ``"{rule_matched}; {label1}, {label2}"`` string
    ``aggregate._compute_nachos_adjustments`` produces; ``base_tier``
    is the row's base NACHOS tier (``nachos_score.value``). Returns
    ``None`` for a missing/empty token string. Presentation only —
    never consulted by scoring, and deliberately total: unknown tokens
    render verbatim rather than crashing or blanking.
    """
    if not token_str:
        return None

    base_token, sep, adj_csv = token_str.partition("; ")
    short = NACHOS_RULE_SHORT.get(base_token, base_token)
    if base_tier is None:
        base_clause = short
    else:
        base_clause = f"Base {base_tier} ({short})"

    if not sep:
        return base_clause

    labels = _split_adjustment_csv(adj_csv)
    clauses = [_render_adjustment_clause(label) for label in labels]
    prose = base_clause + "; " + "; ".join(clauses)

    # v24 non-stacking honesty: both an extension-necessity label AND a
    # fidelity label at base 0 means the applied adjustment is
    # max-of-two, not the sum — say so instead of letting the reader
    # mentally add the labels.
    bare_labels = {_split_trailing_parenthetical(lbl)[0] for lbl in labels}
    if (
        base_tier == 0
        and bare_labels & _EXTENSION_ADJ_LABELS
        and bare_labels & _FIDELITY_ADJ_LABELS
    ):
        prose += _DUAL_FIRE_SUFFIX

    return prose


# ---------------------------------------------------------------------------
# Confidence composite (per-dimension and per-record)
# ---------------------------------------------------------------------------

# Keys mirror the confidence enum in src.score.schema
# (``fact_output_schema`` pins ["high", "medium", "low"]). The composite
# is the minimum across every fact/dimension consulted.
CONFIDENCE_MEANINGS: dict[str, str] = {
    "high": "Every fact this score consulted was extracted or computed with high confidence and none were downgraded by validation.",
    "medium": "At least one consulted fact carried only medium confidence — the score stands, with reduced certainty.",
    "low": "At least one consulted fact was low-confidence, missing, or downgraded by validation — treat the score as needing human review.",
}


# ---------------------------------------------------------------------------
# Workbook display vocab (mirrors src.report.analyst constants)
# ---------------------------------------------------------------------------

# Keys are the DISPLAY values from analyst._MATCH_STATUS_BY_SOURCE —
# hardcoded here (not imported) because analyst imports rubric, not the
# other way around; the lockstep test asserts value-set equality.
MATCH_STATUS_MEANINGS: dict[str, str] = {
    "Matched (core)": "The state's element resolved to a core Ed-Fi element in the Ed-Fi Swagger/API model.",
    "Matched (extension)": "The state's element resolved to a state-extension element in the Ed-Fi Swagger/API model.",
    "Unresolved": "The element did not match any spine slot — source-verbatim values are preserved and the row is not validated against Ed-Fi.",
    "Filtered (SIS never populates)": "A spine-lens placeholder for a whole Ed-Fi domain that student information systems never populate — collapsed to one row and excluded from scoring.",
}

# Keys are the DISPLAY values from analyst._DOC_SOURCE_DISPLAY.
DOC_SOURCE_MEANINGS: dict[str, str] = {
    "Source Doc": "The row's documentation was authored in the state's own source document — the prose the scoring pipeline actually reads.",
    "Swagger": "The state's source document is silent on this entity, so the row was backfilled from the Ed-Fi swagger definition (issue #70) — surfaced for visibility but not counted as documented.",
    "Swagger (leaf)": "The entity is documented but this specific sub-collection leaf is not, so the row was borrowed from the Ed-Fi Swagger/API model (v26, issue #147) — scored per-row but not counted as documented.",
}

# Keys are the DISPLAY values from analyst._STRUCTURAL_DEPTH_LABELS
# (verbal renders of structural_depth tiers 0..3).
STRUCTURAL_DEPTH_MEANINGS: dict[str, str] = {
    "Flat": "No structural signals — a plain scalar field with no key role, references, extensions, or descriptor values.",
    "Light": "One structural signal fires — for example natural-key membership, a single foreign-key hop, one sub-collection, one extension, or a small descriptor value set.",
    "Moderate": "Meaningful structural weight — a two-hop foreign-key chain, moderate fan-out, a natural key inside a sub-collection, multiple extensions, or a broad descriptor value set.",
    "Deep": "Heavy structural weight — a foreign-key chain three or more hops deep, a natural key that many entities reference, or a very wide descriptor value set.",
}

# Keys are the DISPLAY values from analyst._DOC_STYLE_DISPLAY ("Silent"
# is the workbook rename of the ``unspecified`` classifier value).
DOC_STYLE_MEANINGS: dict[str, str] = {
    "Prescriptive": "The narrative prescribes how to populate the field (format, assembly recipe, or pattern) — a vendor can build the value from the narrative alone.",
    "Conceptual": "The narrative describes what the element means but not how to assemble it — the vendor must decide the format.",
    "Cross-reference": "The narrative points to another system or document as the authority — the vendor must follow the pointer.",
    "Regulatory": "The narrative cites a statute, rule, or code without describing the field — the vendor must read the citation.",
    "Silent": "The narrative says nothing usable about the field — the vendor reverse-engineers it from the field name.",
}
