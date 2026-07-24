"""Phase C per-record aggregate — facts + dimensions → scoring sidecar.

Joins ``DimensionScore``s from the rule stage (``src.score.rules``) with
the underlying fact pool and emits ``data/out/{state}_scores_{lens}.json``
per plan §8.1.

Per-record quality aggregate (spine-lens): arithmetic mean of
``documentation_completeness`` + ``obligation_clarity``. Per plan §7.3,
``business_logic_complexity`` is NOT summed into the quality aggregate
— higher complexity means a costlier integration, not a higher-quality
row. Complexity is surfaced as its own field (``complexity_score``)
alongside the per-record quality mean.

Confidence composite (plan §9): min across dimension confidences,
demoted to ``"low"`` on any downgraded input. The Phase C1 surface
keeps the composite as ``"high"/"medium"/"low"`` (categorical); the
numeric 0..1 composite from §9 stays a Phase D elaboration.

Review flagging (Phase C1 — first-principles only):

- Any fact's ``downgrade_reason`` is non-null → ``hallucinated_input``.
- Any dimension has ``confidence="low"`` → ``low_confidence_dimension``.
- ``documentation_completeness==0`` with ``business_logic_complexity>=2``
  → ``inter_dim_inconsistency``.

Phase D's routing (POLICY / SCORING / ANALYST / DATA_MODEL buckets)
layers on top of these reasons.

CRITICAL: module-level ``run()`` is a plain function. The Click wrapper
lives in ``src/cli.py``. See
``tests/test_ingest_az.py::TestCliWiring`` for the regression pattern.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.models.element import StateElements
from src.score.rules import (
    DOCUMENTATION_GAP_DIMENSIONS,
    DOCUMENTATION_STYLE_DIMENSIONS,
    DimensionScore,
    FactResult,
    FactView,
    LENS_OBSERVABILITY_FACTS,
    _reconcile_cross_entity,
    NACHOS_AXIS_DIMENSIONS,
    PRODUCTIZATION_SIGNAL_FACTS,
    SOURCE_DIMENSIONS,
    SOURCE_RULE_INPUTS,
    SPINE_DIMENSIONS,
    SPINE_RULE_INPUTS,
    STRUCTURAL_DEPTH_DIMENSIONS,
    load_fact_pool,
    score_record,
)
from src.states import SUPPORTED_STATES
from src.utils.artifacts import read_json_artifact
from src.utils.paths import (
    scoring_phase_a_dir,
    state_elements_path,
    state_scores_path,
)

_LOGGER = logging.getLogger(__name__)

# Sidecar-envelope contract version (scoring-boundary ADR 0017). Ported
# ahead of the aggregate v3 emit so `score/release_contract.py` can
# freeze the release envelope now; the emit that stamps this and
# produces the v3 per-record fields lands with the aggregate
# envelope-identity slice (MC-21). At v28 the aggregate still emits the
# v28 shape — nothing reads these constants yet except the contract.
SIDECAR_CONTRACT_VERSION: str = "3"

# Canonical adjustment-driver tokens (issue #318 / MC schema contract).
# BYTE-IDENTITY IS LOAD-BEARING: the committed release schema pins these
# exact spellings. Ported ahead of the emit that produces them.
DRIVER_UNNECESSARY_EXT: str = "unnecessary_ext"
DRIVER_NECESSARY_EXT: str = "necessary_ext"
DRIVER_MULTI_ENTITY: str = "multi_entity"
DRIVER_SF_EXPLAINED: str = "fidelity_divergent_explained"
DRIVER_SF_UNCLEAR: str = "fidelity_divergent_unclear"
ADJUSTMENT_DRIVER_TOKENS: tuple[str, ...] = (
    DRIVER_UNNECESSARY_EXT,
    DRIVER_NECESSARY_EXT,
    DRIVER_MULTI_ENTITY,
    DRIVER_SF_EXPLAINED,
    DRIVER_SF_UNCLEAR,
)

SCORING_PLAN_VERSION: str = "28"
# v28 — issue #249 fact-level curation overlay (2026-07-13). Analyst
# corrections to LLM-extracted facts (recorded by `mc review
# correct-fact` in the curation sidecar's `facts` blocks) are overlaid
# onto the fact pool AFTER loading and BEFORE the rule cascade runs, so
# the unchanged rules recompute from the corrected input. A corrected
# fact serializes with `"provenance": "human_corrected"` in
# `fact_provenance` (emitted only when set — sidecars without
# corrections stay byte-identical to v27 apart from this header
# version). The rules, prompts, and cache are untouched: the bump marks
# the sidecar-shape capability (a new possible per-fact key + the
# human-corrected input channel), per the CLAUDE.md guardrail.
# Individual corrections are per-row data-level annotations and never
# bump the version. v1 scope: LLM facts only (deterministic facts are
# code-bug territory), same-lens-only application; the spine lens picks
# up corrected source `extension_is_necessary` through the existing
# sidecar borrow below. ADR 0012.
# v27 — Ed-Fi domain expansion: Assessment + AssessmentRegistration enabled
# per-(state,domain) via `ingest.domain_scope` (ADR 0007, 2026-06-29). TX +
# IN Assessment and IN AssessmentRegistration lift the spine-lens placeholder
# collapse, growing those states' coverage denominators (TX +216, IN +354
# spine slots); documented counts and source-lens artifacts are unchanged.
# Staff/Finance were never filtered. WI Assessment registered but left
# disabled (mapped page yields 0 documented rows). This is the ingest+coverage
# step; LLM scoring over the newly-surfaced rows is a separate follow-up.
# v26 — issue #147 leaf-level cross-lens borrow (2026-05-03). Extends
# the issue #70 swagger-as-documentation mechanism to fill within-entity
# leaves on documented entities. Methodology approved Slack 2026-05-03
# by Doug + Maria: leaf-level borrowing is OK as long as borrowed rows
# are clearly identified (new `documentation_source="swagger_leaf"`
# value) and borrowed rows should increase the keymap-join match count
# (they appear in the per-row sidecar by virtue of per-row scoring
# running unconditionally). Two structural changes:
#
# 1. `ElementRecord.documentation_source` widens from
#    `Literal["source_doc", "swagger"]` to
#    `Literal["source_doc", "swagger", "swagger_leaf"]`. Sidecar shape
#    change → version bump per the CLAUDE.md guardrail.
#
# 2. `swagger_backfill.collect_leaf_backfill_records()` walks the spine
#    catalog one extra time, emitting rows for spine slots whose parent
#    entity IS in the source doc but whose `(entity, element)` alias
#    expansion is missing from source-lens. Borrowed rows carry
#    `documented=False` (matches the v21 close-out posture for
#    entity-level swagger backfill) so headline NACHOS aggregates stay
#    restricted to authored prose; only the keymap-join match count and
#    the `source.coverage_pct` metric grow. The authored-only
#    `documentation_provenance.source_doc.coverage_pct` is naturally
#    protected because `gap_log` is built before backfill runs.
#
# Cohort sizing (issue #147 estimate, to verify post-regen): AZ ~50
# leaves recovered (`Address.*`, `Language.*`, `services.*`, etc.) →
# match share ≥75%; WI ~21 leaves (`Student/Locals.*`, etc.) →
# `human_only` (NA-excluded) below 5%; MN/TX small or zero. Spine-lens
# unchanged (parallel to v21 swagger-backfill posture; the backfill never
# flips spine-lens rows). LLM cost ~$0 because borrowed rows have byte-identical
# content to spine-lens rows so the SHA-256 prompt-cache key hits.
# Pattern F residual at the ai-summary tab gains a "Leaf rows recovered
# by cross-lens borrow" observability line. ADR `docs/adr/0004-issue-147-
# leaf-level-cross-lens-borrow.md`.
#
# v25 — issue #124 PR 2: Internal Tidying + #111 deterministic fact
# (2026-05-02). Layered on top of v24's Option 2 axis change. Two
# behavior changes, both annotation-only on the v12 SF fold label —
# `adjusted_nachos_score` magnitudes are unchanged from v24:
#
# 1. New deterministic fact `extension_fidelity_divergence` (det.v11)
#    surfaces shape-divergence between an extension and its same-entity
#    core counterpart by name-stem + data-type-bucket comparison. Fires
#    as `"replaces_core_field_shape"` when the extension's stem (after
#    stripping `Descriptor`/`Id`/`UniqueId`/`Reference` suffixes)
#    shares a 5+ char case-insensitive prefix with a core element on
#    the same entity AND their data-type buckets differ (e.g., Boolean
#    vs Descriptor). Source-lens only by registration in
#    `SOURCE_DETERMINISTIC_FACTS`. Path A from issue #111; the AZ #111
#    examples (`EligibilitySourceDescriptorId`,
#    `EligibilityStatusDescriptorId`) don't fire because their core
#    counterparts (`directCertificationIndicator`) don't share a stem
#    — those rows keep the unannotated SF fold label, which is the
#    honest outcome (heuristic limitation documented in det.v11).
#
# 2. SF fold label annotation: when the v12 SF fold fires on a
#    source-lens row at base 0, `_compute_nachos_adjustments` now reads
#    `extension_fidelity_divergence`, `state_scope_delta`, and the
#    existing `sourcing_constraint_documented` LLM fact and renders
#    typed reasons as a parenthetical on the SF label —
#    `+1.0 fidelity_divergent_unclear (replaces_core_field_shape, transformation)`.
#    Reason order: shape-divergence first (det fact), then scope
#    delta (LLM enum), then sourcing constraint (LLM enum). When no
#    typed reasons fire the label format is unchanged from v24
#    (`+1.0 fidelity_divergent_unclear`). The annotation is purely
#    informational — it tells analysts *which* downstream divergence
#    mechanisms drove a fold without changing the magnitude or the
#    audit-trail label structure.
#
# Internal Tidying scope kept conservative: the v12 SF fold remains
# the single source of magnitude (no graduation of #59 typed reasons
# to magnitude-bumping; reintroducing those as bumps would re-create
# the stacking problem v24 just closed). Future calibration (e.g.
# differentiating shape-replacement vs. typed-refinement magnitudes)
# is a downstream reviewer-pass call, not implicit in this PR. Pure
# aggregate + det-fact change; no LLM cost (no re-extract); no prompt
# edit. ADR `docs/adr/0003-issue-124-tidying-shape-divergence.md`.
#
# v24 — issue #124 Option 2 non-stacking axis (2026-05-02). Closes the
# long-running stacking problem at base 0: the v12 SF fold
# (`+0.5`/`+1.0` source-lens fidelity adjustment) and the v18 two-tier
# extension weighting (`+0.5`/`+1` necessity adjustment) currently
# SUMMED when both fired on the same row. The reviewer treats the
# situation as ONE judgment about the extension; POC-3's stacking
# produces `adjusted_nachos_score == +2.0` vs the reviewer's `0.5`/`1.0`
# on the 76-row source-lens cohort (AZ 17, WI 1, MN 24, TX 34 at v23).
# Option 2 from the #124 decision pack (PR #126) keeps both axes but
# takes MAX-OF-TWO with fidelity-wins tie-break when both fire at base
# 0. Both branches still render in `nachos_justification` for audit
# clarity (today's label format preserved); new
# `fidelity_necessity_dual_fire` review reason flags rows where the
# non-stacking rule fired so analysts can find them. Multi-entity
# adjustment continues to add separately (independent axis). Footgun
# note: a reader of `nachos_justification` should NOT mentally sum the
# labels — `adjusted_nachos_score` reflects the max-of-two, not the
# sum, on dual-fire rows. Pure aggregate-layer change; no LLM cost (no
# re-extract); no prompt edit. Calibration source: Slack agreement
# with Doug + Maria 2026-05-02 against the four-option deliberation in
# the decision pack. ADR `docs/adr/0002-issue-124-option2-non-stacking.md`.
#
# v23 — issue #106 (Q4 of POC closeout reviewer bundle, 2026-05-02).
# `business_logic_complexity` graduates to source-lens. Pre-v23 it was
# spine-lens-only by registration (`_SPINE_RULES`); v23 adds it to
# `_SOURCE_RULES` so source-lens sidecars carry a `complexity_score`
# 0-3 cost tier on every scored row. Same rule body, same fact inputs
# (`has_aggregation`, `has_concatenation`, `has_cross_entity_logic`,
# `has_conditional_logic` — all already in `SOURCE_RULE_INPUTS`); the
# only change is registration. Reviewer-View column shape: source-lens
# Reviewer View grows 28 → 29 cols (`Complex Business Logic` inserted at
# col 8 between `Business Logic` and `NACHOS score`, mirroring spine);
# downstream-shifted post-#113 trailing cols `Needs Review` / `Review
# Route` move from cols 27/28 → 28/29. Audit Trail follows: source-lens
# 56 → 59 cols (one Reviewer View prepend, one dim-tier audit cell
# added, one `complexity_score` tail entry). No LLM cost — pure
# registration + presentation change. Self-decided by the project lead
# (the smaller of the four POC-closeout binary methodology calls; no
# stakeholder dependency).
#
# v22 — issue #97 bare-FK parent-entity gate calibration (2026-04-30).
# Closes the issue #80 B6 follow-on calibration call: documented WI rows
# whose `has_conditional_logic` cited evidence is the parent-entity
# submission gate annotation (`[Public: …, Choice: …]`) and whose
# element name is FK-shaped (Id/UniqueId/Reference) had been firing
# tier_1_conditional in `business_logic_complexity` and `nachos_score`.
# Methodologically the conditional applies to the parent record's
# submission gate, not to the FK reference's own value or presence.
# v22 adds a deterministic fact `element_only_parent_entity_gate` (det
# .v10) that fires on this exact shape (FK-named + def carries the
# gate annotation + element_specific_rules empty) and rule-layer
# suppression in both BLC and nachos_score that overrides
# has_conditional_logic to False. The LLM verdict stays in the
# sidecar's fact_provenance for observability — only the rule-layer
# tier changes. Pure deterministic fact + rule edit; no LLM cost
# (cache stays warm, prompts unchanged). Affects 7 records (1 WI source
# `DisciplineIncident|schoolId` + 6 WI spine rows on
# StudentEducationOrganizationResponsibilityAssociation +
# StudentLanguageInstructionProgramAssociation + GradingPeriod +
# LocalActual). The annotation pattern is WI-Confluence-specific by
# construction; AZ/MN/TX narratives don't carry it so the rule is
# naturally inert there. Calibration analysis + classification in
# issue #97.
#
# v21 — issue #70 swagger-as-source middle-path landing (2026-04-30).
# Keeps the v20 ingest mechanism (swagger-backfill appends synthetic
# source-lens rows for spine entities the state's primary source doc is
# silent on; rows carry `documentation_source="swagger"` for visibility)
# but reverts the documented/scoring posture to be conservative pending
# a stakeholder methodology call:
#   * source-lens swagger rows carry `documented=False` (v20 was True)
#   * spine-lens swagger keys are NOT flipped to documented=True (v20 was)
#   * aggregate.run filters rows with `documentation_source!="source_doc"`
#     out of headline coverage, dim distributions, mean NACHOS, and the
#     review queue — per-row scoring still runs so the sidecar carries
#     each row's tier for reviewer-comparison alignment
#   * coverage report drops the v20 `total_documented` block; keeps
#     `documentation_provenance.source_doc.coverage_pct` (matches v19
#     baselines) plus a `swagger_rows` audit count
# The Documentation Source column on the analyst workbook (col 27 source
# / 29 spine) is preserved so analysts can see which rows surface via
# swagger. Whether to promote swagger publication to "documented" is a
# methodology call deferred to the POC close-out review — flipping
# back to v20 is localized to the `documented=False` flag and the
# aggregate-time filter.
#
# v20 — issue #70 swagger-as-source methodology widening (2026-04-29).
# Adds `documentation_source` field on `ElementRecord` (Literal
# "source_doc" | "swagger") and a swagger-backfill ingest step that
# appends synthetic source-lens rows for spine entities the state's
# primary source doc is silent on. Source-lens coverage now reports a
# dual metric (`source_doc_coverage_pct` + `total_documented_coverage_pct`)
# and the analyst Reviewer View carries a trailing `Documentation
# Source` column (cols 27 source / 29 spine). Cache stays warm for
# previously-extracted rows because per-fact `prompt_version` is
# unchanged; only newly-appended swagger-backfill rows incur extraction
# cost. Methodology stance: same NACHOS rubric, no tier cap — swagger-
# only rows score low naturally (definition thin, no business-rules
# text) and that's honest signal. Within-entity gaps stay in the gap
# surface (issue #66) — only `spine_only_full_entity` rows promote.
#
# v19 — issue #80 B6 documented-row LLM extract (2026-04-29). Closes
# the v17→v18 prompt/cache-mismatch carry-over: PR #78 added element-
# scoped grade-band / population / school-type conditionals as a
# positive class to the `has_conditional_logic` prompt but did NOT
# re-extract, so the documented-row JSONLs still answered against the
# v16 prompt while issue #73's gap-row surface had picked up the new
# prompt at extraction time. v19 re-extracts `has_conditional_logic`
# across all 4 states × 2 lenses, reunifying the prompt-pinning story
# across the documented + gap surfaces. Pure prompt-side bump — the
# cache layer's (prompt_text, model_id, prompt_version) hash auto-
# invalidated for this fact only; PROMPT_VERSION stays at phase-a.v1
# (bumping would burn ~$40 refilling the other 13 prompts). Aggregate
# / rule logic unchanged from v18; sidecar shape unchanged. Closes #80.
# v18 — issue #84 reintroduce two-tier extension weighting (2026-04-29).
# Closes the disclosed v17 divergence from the public Ed-Fi NACHOS
# overview (recommendations.md §7.3). Now that issue #85 tightened the
# `extension_is_necessary` prompt + cold-re-extracted all 715 audit-
# trail rows ($1.41 LLM, 16.5 % verdict-flip rate, calibration ready),
# `_compute_nachos_adjustments` reads the per-element judgment and
# forks: True → +0.5 necessary_ext, False → +1 unnecessary_ext, None →
# conservative +0.5 with `extension_necessity_unresolved` review flag.
# The v17 B3 "blanket +0.5 by membership" path is retired. B5 element-
# only bare-FK suppression in `nachos_score` (rules.py) and the cross-
# lens fact-borrow on spine-lens stay unchanged. Pure aggregate-layer
# revert + restore — no rule edits, no prompt edits beyond #85's, no
# new LLM cost (post-edit JSONLs from #85 already on disk).
# v17 — issue #63 B3 + B5 + B1-gate-drop (POC interim methodology
# decisions, 2026-04-29). Three coordinated changes that flow from
# the methodology calls recorded in `docs/adr/0001-issue-63-methodology-
# calls.md` and `docs/recommendations.md` §6.5:
# - **B3 blanket extension necessity** (RETIRED in v18): collapsed the
#   `unnecessary_ext / necessary_ext / unresolved_ext` branches in
#   `_compute_nachos_adjustments` into a single `+0.5 necessary_ext`
#   for any `is_extension == True` row. v18 reintroduces the fork.
# - **B1 gate drop**: the bare-FK low-confidence gate from v15 became
#   redundant under blanket and was removed. The det.v8
#   `element_narrative_present` deterministic fact is RETAINED — now
#   consumed by B5's bare-FK gate in `nachos_score` (rules.py).
# - **B5 element-only scope** (carries forward in v18): new det.v9 fact
#   `element_is_bare_fk_reference` (element_name matches a spine
#   entity reference name AND `element_narrative_present == False`).
#   The `nachos_score` rule suppresses the `has_cross_entity_logic`
#   contribution to the NACHOS tier when this fact fires.
# Pure rule + aggregate-layer change; no LLM cost; no prompt edit.
# Audit trail (`extension_is_necessary`, `extension_justification`
# dimension, `has_cross_entity_logic` fact) all preserved unchanged.
# Renumbered from v16 → v17 during 2026-04-29 rebase (B2 took v16).
# v16 — issue #63 B2 (2026-04-29): tighten `has_conditional_logic`
# prompt to exclude pure value-range / format / pointer constraints.
# Adds three negative examples (DisciplineDate value-range, SchoolId
# format range with reserved-value note, Organization-FK pointer to
# external rule source) plus a rule paragraph spelling out that
# conditional logic requires (a) two-or-more value branches keyed on
# a condition, (b) presence-vs-absence keyed on a condition, or (c) a
# derivation rule that inspects another field's value — single-branch
# validity checks on the element's own value don't qualify. Prompt
# text change re-keys the cache for `has_conditional_logic` only;
# PROMPT_VERSION intentionally NOT bumped (would burn $40+ refilling
# the other 13 prompts). Same pattern as v13. Phase A re-extracted
# for `has_conditional_logic` only across all 4 states × 2 lenses.
# Renumbered from v15 → v16 during 2026-04-29 rebase (B1 took v15).
# v15 — issue #63 B1 (2026-04-29): bare-FK low-confidence gate on the
# extension `+1 unnecessary_ext` adjustment. When the LLM's "False"
# judgment on `extension_is_necessary` for a source-lens extension row
# arrives with LOW confidence on BOTH judgment facts (necessary AND
# standalone) AND the source has no narrative anchor
# (`definition_present == False`), flip the adj path from
# `unnecessary_ext` (+1) to the conservative unresolved path (+0.5 +
# `bare_fk_low_confidence` review flag). Closes a measured class of
# bare-FK overcounts surfaced by issue #63 Phase 1 (PR #71). Pure
# adj-layer change; no rule / prompt edit; no fact-pool shape change.
# Renumbered from v14 → v15 during 2026-04-29 rebase (issue #66 Layer 3
# took v14 on main first).
# v14 — sidecar shape change: ``discovery_lens`` per-record field added
# (issue #66 Layer 3, 2026-04-29). Default ``"source"`` for ordinary
# rows enumerated in the state's source doc; ``"spine_anchored"`` for
# rows the spine-anchored gap surfacer (issue #66 Layer 2) recovered.
# The field gives downstream consumers (workbooks, recommendations)
# a way to distinguish authentic source-doc rows from spine-only-known
# rows when both eventually carry NACHOS scores. Methodology rule
# arithmetic and prompts are unchanged; phase_a artifacts stay valid;
# v13 sidecars roll forward through aggregate without LLM re-extraction.
# v13 — side-quest PR A simplification (2026-04-28): merged
# `state_narrows_edfi_scope` + `state_broadens_edfi_scope` into a single
# `state_scope_delta` enum3 (narrows | broadens | neutral). The
# semantic_fidelity rule's tier-2 XOR collapses to a single membership
# test on the enum. Sidecar shape changes (fact_provenance loses two
# bools, gains one enum) so the version bump is required by the
# CLAUDE.md rule "rule / prompt / methodology / sidecar-shape changes
# bump SCORING_PLAN_VERSION." PROMPT_VERSION intentionally NOT bumped:
# the new prompt's text creates a fresh cache key naturally; the
# unchanged 13 prompts continue to hit cache at phase-a.v1 — bumping
# globally would burn $40+ on refilling them for zero added correctness.
# v12 — semantic_fidelity folded into adjusted_nachos_score under the
# B-gated `base0` scheme (issue #55, 2026-04-28). The SF adjustment
# fires only when ``nachos_dim.value == 0`` so it never stacks on top
# of a rubric base that already credited the same divergence evidence
# (eliminates the 72-row double-count surfaced by the menu analysis).
# Source-lens only (spine-lens has no SF dimension). Methodology rule
# arithmetic and prompts are unchanged; phase_a artifacts stay valid.
# v11 — Integration Profile rename (display layer + sidecar key rename:
# structural_complexity → structural_depth, documentation_explicitness →
# documentation_style_tier, undocumented_complexity → documentation_gap).
# Methodology semantics unchanged from v10 (in_scope=True default per
# `Logic_Dec2025` rows 33-46); the bump reflects sidecar shape change.
# See `docs/plan-integration-profile-rename.md`.
# v10 — methodology scope rectification (2026-04-26): in_scope=True
# default per `Logic_Dec2025` rows 33-46. See
# `docs/archive/methodology-scope-rectification.md`.

# Phase F NACHOS methodology constants (plan §4, §5).
_NACHOS_MAX_ADJUSTED_SCORE: float = 4.5
# v18 (issue #84, 2026-04-29): two-tier extension weighting reinstated
# after the v17 collapse (B3 blanket). The +1 unnecessary / +0.5
# necessary fork mirrors the public Ed-Fi NACHOS overview. The
# `extension_is_necessary` LLM judgment drives the fork, with #85's
# tightened prompt as the calibration prerequisite. Mutually exclusive
# branches; unresolved (None) falls back to +0.5 with a review flag.
_NACHOS_ADJ_UNNECESSARY_EXT: float = 1.0
_NACHOS_ADJ_NECESSARY_EXT: float = 0.5  # mutually exclusive with +1
_NACHOS_ADJ_MULTI_ENTITY: float = 0.5
# v12 semantic_fidelity → adjusted_nachos_score adjustment magnitudes
# (B-gated `base0`, issue #55). Source-lens only.
_NACHOS_ADJ_FIDELITY_DIVERGENT_EXPLAINED: float = 0.5  # SF tier 2
_NACHOS_ADJ_FIDELITY_DIVERGENT_UNCLEAR: float = 1.0    # SF tier 1 / 0

# Adjustment-label constants — the exact strings
# ``_compute_nachos_adjustments`` renders into ``nachos_justification``.
# Hoisted (Sequence-1 presentation legend, 2026-07) so the analyst
# workbook's Legend sheet (``src.score.rubric``) can document each
# label without drifting from the rendered text. BYTE-IDENTITY IS
# LOAD-BEARING: sidecars are pinned by existing tests and
# ``tests/test_score_rubric.py`` guards these exact spellings — a
# changed byte here is a scoring-output change, not a rename.
ADJ_LABEL_UNNECESSARY_EXT: str = "+1 unnecessary_ext"
ADJ_LABEL_NECESSARY_EXT: str = "+0.5 necessary_ext"
ADJ_LABEL_MULTI_ENTITY: str = "+0.5 multi_entity"
ADJ_LABEL_SF_EXPLAINED: str = "+0.5 fidelity_divergent_explained"
ADJ_LABEL_SF_UNCLEAR: str = "+1.0 fidelity_divergent_unclear"

# Which dimensions count toward the per-record quality aggregate. Plan
# §7.3: complexity is inverted (higher = costlier) and never summed
# into quality. Explicit tuple so a future dimension addition forces
# a deliberate decision.
SPINE_QUALITY_DIMENSIONS: tuple[str, ...] = (
    "documentation_completeness",
    "obligation_clarity",
)
SPINE_COMPLEXITY_DIMENSION: str = "business_logic_complexity"

# v23 (issue #106 / closeout-bundle Q4) — source-lens complexity
# dimension. Mirrors SPINE_COMPLEXITY_DIMENSION; same rule body, same
# fact inputs. Wiring it as a named constant (rather than reusing
# SPINE_COMPLEXITY_DIMENSION directly) keeps the lens dispatch in
# ``score_one`` symmetric with the quality / rule_inputs lookups.
SOURCE_COMPLEXITY_DIMENSION: str = "business_logic_complexity"

# Source-lens has no complexity/quality split for the four original
# §7.1 dimensions — plan §7.3 treats them as quality-equivalent. Phase F
# adds ``nachos_score`` to ``SOURCE_DIMENSIONS`` on a distinct
# methodology axis; it must NOT fold into the per-record quality mean
# (methodology axis ≠ quality axis). v23 adds business_logic_complexity
# to source-lens — also carved out of the quality mean (cost axis, not
# quality axis), mirroring the spine-lens carve-out.
SOURCE_QUALITY_DIMENSIONS: tuple[str, ...] = tuple(
    d for d in SOURCE_DIMENSIONS
    if d not in NACHOS_AXIS_DIMENSIONS
    and d not in STRUCTURAL_DEPTH_DIMENSIONS
    and d not in DOCUMENTATION_STYLE_DIMENSIONS
    and d not in DOCUMENTATION_GAP_DIMENSIONS
    and d != SOURCE_COMPLEXITY_DIMENSION
)


_CONFIDENCE_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
_CONFIDENCE_FROM_ORDER: dict[int, str] = {v: k for k, v in _CONFIDENCE_ORDER.items()}


@dataclass(frozen=True)
class ScoredRecord:
    record_key: str
    entity: str
    element_name: str
    dimensions: dict[str, DimensionScore]
    # Leading underscore marks this as an internal sidecar diagnostic —
    # not for surfacing on stakeholder workbooks. It collapses mixed-axis
    # per-dimension tiers into one format-confounded scalar (TX TWEDS 1.94
    # vs WI Confluence 2.47 reflects format, not quality); the header-level
    # ``mean_quality_score`` and per-dimension means are the interesting
    # diagnostics.
    _quality_mean_diagnostic: float | None
    complexity_score: int | None
    confidence_composite: str
    fact_provenance: dict[str, dict[str, Any]]
    review: dict[str, Any]
    # Phase F NACHOS methodology surfaces (plan §4, §5, §6).
    # ``adjusted_nachos_score`` = nachos tier + extension/multi-entity
    # adjustments, capped at 4.5 per methodology. None if nachos_score
    # couldn't evaluate. ``in_scope`` is the rules-only classifier result
    # (plan §5) — out-of-scope rows blank cols 9/10/11 on the workbook
    # but still carry the scalar in the sidecar for audit.
    # ``nachos_justification`` is the deterministic
    # "{rule_matched}; +<adj labels>" string for the analyst column.
    adjusted_nachos_score: float | None
    in_scope: bool
    nachos_justification: str | None
    # v13 — provenance flag distinguishing authentic source-doc rows from
    # spine-anchored gap rows the surfacer recovered (issue #66 Layer 2 /
    # Layer 3). ``"source"`` (default) for rows enumerated in the state's
    # source doc; ``"spine_anchored"`` for rows aggregated from
    # ``{state}_elements_gap.json``. Lets downstream consumers (workbooks
    # / recommendations / reviewer comparison) display gap-derived rows
    # distinctly without relying on string-prefix conventions on
    # ``record_key``.
    discovery_lens: str = "source"
    # Issue #70 v21 close-out posture: rows surfaced by the swagger
    # backfill carry ``documentation_source="swagger"``. Per-row scoring
    # still runs (so the sidecar carries their tier for reviewer-comparison
    # alignment), but ``aggregate.run`` excludes them from headline coverage
    # and mean-NACHOS computations. Whether to promote swagger publication
    # to "documented" remains a methodology call.
    documentation_source: str = "source_doc"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mean(values: list[int]) -> float | None:
    """Arithmetic mean of dimension values; returns None if empty."""
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _min_confidence(levels: list[str]) -> str:
    if not levels:
        return "low"
    lowest = min((_CONFIDENCE_ORDER.get(lv, 0) for lv in levels), default=0)
    return _CONFIDENCE_FROM_ORDER[lowest]


def _fact_provenance(view: FactView, facts: list[str]) -> dict[str, dict[str, Any]]:
    """One line per fact: confidence, downgrade reason, and value.

    Keeps the audit trail self-contained — anyone reading the sidecar
    can see why a dimension confidence landed where it did without
    cross-referencing the JSONL artifact.

    Facts whose ``FactResult`` carries non-empty ``spans`` additionally
    surface a ``spans: list[str]`` key — the verbatim match text that
    fired the gate (deterministic facts like
    ``descriptor_values_enumerated``) or the validated LLM evidence
    quote (productization-signal facts like ``integration_class``).
    ``_row_to_fact_result`` already drops ``valid=False`` LLM-span
    entries, so anything that lands here has passed substring
    validation. Facts without spans omit the key entirely so the
    sidecar stays tight on the common case.
    """
    raw = view.raw_fact_results(facts)
    out: dict[str, dict[str, Any]] = {}
    for fact in facts:
        result = raw.get(fact)
        if result is None:
            out[fact] = {
                "value": None,
                "confidence": "low",
                "downgraded": True,
                "downgrade_reason": "missing_from_artifact",
            }
            continue
        entry: dict[str, Any] = {
            "value": result.value,
            "confidence": result.confidence,
            "downgraded": result.downgraded,
            "downgrade_reason": result.downgrade_reason,
        }
        if result.spans:
            entry["spans"] = list(result.spans)
        if result.provenance:
            # Issue #249: `human_corrected` when the curation overlay
            # replaced the extracted value. Emitted only when set, so
            # sidecars without corrections stay byte-identical.
            entry["provenance"] = result.provenance
        out[fact] = entry
    return out


def _build_review_block(
    dimensions: dict[str, DimensionScore],
    fact_provenance: dict[str, dict[str, Any]],
    lens: str,
    *,
    in_scope: bool = True,
    nachos_tier: int | None = None,
    confidence_composite: str = "high",
) -> dict[str, Any]:
    """Phase C1 review signals — first-principles triggers per plan §9.

    Phase F adds the ``nachos_low_confidence_high_tier`` trigger (plan
    §7): in-scope rows scoring NACHOS tier >=3 with low confidence
    composite get flagged for SCORING review. Out-of-scope rows don't
    trip this trigger — they're not shown on stakeholder surfaces so
    investing reviewer attention there is low-value.
    """
    reasons: list[str] = []

    for fact, prov in fact_provenance.items():
        if prov["downgraded"]:
            reasons.append(f"hallucinated_input:{fact}")

    for dim_name, dim in dimensions.items():
        if dim.confidence == "low":
            reasons.append(f"low_confidence_dimension:{dim_name}")

    if lens == "spine":
        doc = dimensions.get("documentation_completeness")
        blc = dimensions.get(SPINE_COMPLEXITY_DIMENSION)
        if (
            doc is not None
            and blc is not None
            and doc.value == 0
            and blc.value is not None
            and blc.value >= 2
        ):
            reasons.append("inter_dim_inconsistency:docs_missing_but_logic_complex")

    # Phase F trigger: high-tier NACHOS with low confidence on in-scope
    # rows — the score is stakeholder-visible on the workbook, so any
    # uncertainty at tier 3 is worth reviewer escalation.
    if (
        in_scope
        and nachos_tier is not None
        and nachos_tier >= 3
        and confidence_composite == "low"
    ):
        reasons.append("nachos_low_confidence_high_tier")

    sorted_reasons = sorted(set(reasons))
    # Phase D: attach the first-principles route so downstream consumers
    # (review queue, analyst workbook, rollup MD) see it inline.
    from src.report.review_queue import route_review  # avoid import cycle
    return {
        "needs_review": bool(sorted_reasons),
        "reasons": sorted_reasons,
        "route": route_review(sorted_reasons),
    }


# ---------------------------------------------------------------------------
# Phase F — NACHOS methodology adjustment arithmetic + in-scope classifier
# ---------------------------------------------------------------------------


def _load_source_extension_facts(
    state: str,
    artifacts_dir: Path | None,
    *,
    allow_stale: bool = False,
    source_sidecar_path: Path | None = None,
) -> dict[str, bool | None]:
    """Return ``{record_key.lower(): extension_is_necessary_value}`` for a state.

    Spine-lens sidecar aggregation for source=extension rows needs
    ``extension_is_necessary`` — a fact the runtime only extracts on
    source-lens (FACT_SOURCE_FILTERS restricts it to
    ``source='extension'`` rows, and those rows live in the source-lens
    elements catalog). Reading the source-lens sidecar at spine-lens
    aggregate time keeps Phase F's +1/+0.5 extension adjustment honest
    on both lens outputs.

    Returns an empty dict when the source-lens sidecar doesn't exist —
    spine-lens aggregate falls back to "extension necessity unresolved"
    (conservative +0.5 necessary adjustment + review flag). Operator
    playbook: always run ``aggregate --lens source`` before
    ``--lens spine`` after a new extraction so the adjustments land on
    fresh data.

    Issue #94 (case-insensitive join): source-lens record_keys carry the
    state's source-document casing (AZ XLSX uses PascalCase
    ``BeginDate``; TX TWEDS likewise) while spine-lens record_keys carry
    the swagger schema's canonical camelCase ``beginDate``. Same
    conceptual element, different render. The dict is keyed on
    ``record_key.lower()`` so the cross-lens borrow joins
    case-insensitively; ``_compute_nachos_adjustments`` looks up by
    ``view.record_key.lower()`` to match. WI/MN happen to render
    consistently across lenses and were unaffected; AZ and TX were
    losing ~432 spine extension rows to the conservative
    ``extension_necessity_unresolved`` fallback before this fix.
    Collision risk on Ed-Fi schemas is nil — PascalCase vs camelCase is
    a render choice within a single namespace, not a name distinction
    — but a conflicting collision (same lowered key, different
    extension_is_necessary value) is logged at WARNING.
    """
    # ``source_sidecar_path`` makes the test seam explicit (issue #212
    # item 3): the artifacts_dir fallback hard-encodes the
    # ``data/out/scoring/phase_a`` → ``data/out`` relationship, so a
    # nonstandard artifacts_dir used to produce a silent miss (empty
    # dict → conservative fallback) with no way to point at the real
    # sidecar.
    if source_sidecar_path is not None:
        source_sidecar = source_sidecar_path
    elif artifacts_dir is not None:
        source_sidecar = artifacts_dir.parent.parent / f"{state.lower()}_scores_source.json"
    else:
        source_sidecar = state_scores_path(state, "source")  # type: ignore[arg-type]
    # Fail-loudly gate (seq 4 PR B): with a publish lineage established,
    # a missing or stale source sidecar raises instead of silently
    # degrading spine extension adjustments to the conservative
    # `extension_necessity_unresolved` fallback. No manifest → exactly
    # the legacy behavior below.
    from src.publish.manifest import verify_fresh

    verify_fresh(
        source_sidecar,
        consumer="score aggregate --lens spine (extension fact-borrow)",
        allow_stale=allow_stale,
    )
    if not source_sidecar.exists():
        return {}
    # A CORRUPT source sidecar must never silently degrade every spine
    # extension row to the conservative fallback (issue #212 item 3 —
    # seq-4 PR B closed the stale/missing branches; this closes the
    # parse-failure branch).
    payload = read_json_artifact(
        source_sidecar,
        consumer="score aggregate --lens spine (extension fact-borrow)",
    )
    out: dict[str, bool | None] = {}
    for entry in payload.get("scores", []):
        key = entry.get("record_key")
        if key is None:
            continue
        # v21 close-out posture: swagger-backfilled rows are
        # ``documented=False`` and skipped by the LLM extraction pool,
        # so their ``extension_is_necessary`` provenance is empty. Skip
        # them in the borrow dict so spine-lens extension rows that
        # have no source-doc-authored counterpart fall through to the
        # conservative ``extension_necessity_unresolved`` fallback —
        # matching v19's semantics where these keys were simply absent
        # from the source-lens sidecar. Without this gate, swagger keys
        # would shadow the missing-key fallback with a None value, which
        # ``_compute_nachos_adjustments`` would interpret differently
        # and skew spine-lens means away from v19 baselines.
        if entry.get("documentation_source") == "swagger":
            continue
        prov = entry.get("fact_provenance", {}) or {}
        ext_nec = prov.get("extension_is_necessary", {}) or {}
        value = ext_nec.get("value")
        lower_key = key.lower()
        if lower_key in out and out[lower_key] != value:
            _LOGGER.warning(
                "extension_is_necessary borrow lookup collision on %s: "
                "two source-lens keys differ only in case but disagree "
                "on extension_is_necessary (%r vs %r); keeping first.",
                lower_key, out[lower_key], value,
            )
            continue
        out[lower_key] = value
    return out


def _load_fact_corrections(
    state: str, lens: str, curation_base: Path | None
) -> dict[str, dict[str, dict]]:
    """Same-lens analyst fact corrections from the curation sidecar.

    Lazy import — the curation module is the single home of sidecar
    schema knowledge (the mirror image of ``curation.adjudicate``'s
    lazy import of this module's ``SCORING_PLAN_VERSION``; both
    directions stay function-local so no import cycle forms).
    """
    from src.report.curation import fact_corrections_for

    return fact_corrections_for(state, lens, base=curation_base)


def _apply_fact_corrections(
    pool: dict[str, FactView],
    corrections: dict[str, dict[str, dict]],
    *,
    state: str,
    lens: str,
) -> dict[str, int]:
    """Overlay analyst fact corrections onto the loaded pool (issue #249).

    Runs AFTER ``load_fact_pool`` and BEFORE the rule cascade, so the
    unchanged rules recompute from the corrected value. The replacement
    ``FactResult`` carries ``confidence="high"`` / ``downgraded=False``
    (a human assertion is top-trust in this vocabulary — the row stops
    being flagged for a shaky input a human has verified) and
    ``provenance="human_corrected"`` so the sidecar shows exactly which
    facts a human touched. Spans are dropped: the model's evidence
    supported the *wrong* value; the rationale lives in the curation
    sidecar and the model's original stays in the phase_a artifacts +
    prompt cache (never written here).

    ``mc review correct-fact`` is the validation gate; apply-time is
    tolerant-but-loud so a hand-edited or drifted sidecar degrades to
    warnings instead of crashing ``mc publish``: unknown records,
    non-LLM facts, facts absent from this pool, and type-invalid values
    are skipped with a warning each. Corrections whose value the model
    now agrees with still apply (the overlay is the record of what the
    human said) but are counted ``redundant`` — the "prompt got fixed,
    correction retirable" signal in the run log.
    """
    from src.score import deterministic, extract
    from src.score.schema import parse_fact_value

    stats = {"applied": 0, "records": 0, "skipped": 0, "redundant": 0}
    for record_key in sorted(corrections):
        view = pool.get(record_key)
        if view is None:
            _LOGGER.warning(
                "fact correction skipped: %s is not in the %s %s pool "
                "(record renamed or removed since capture?)",
                record_key, state, lens,
            )
            stats["skipped"] += len(corrections[record_key])
            continue
        applied_here = 0
        for fact in sorted(corrections[record_key]):
            block = corrections[record_key][fact]
            if (
                fact in deterministic.DETERMINISTIC_FACTS
                or fact not in extract.SUPPORTED_FACTS
            ):
                _LOGGER.warning(
                    "fact correction skipped on %s: %r is not a "
                    "correctable LLM fact (hand-edited sidecar?)",
                    record_key, fact,
                )
                stats["skipped"] += 1
                continue
            current = view.raw_fact_results([fact])[fact]
            if current is None or current.downgrade_reason == "filtered_by_source":
                _LOGGER.warning(
                    "fact correction skipped on %s: %s is not extracted "
                    "for this record on the %s lens",
                    record_key, fact, lens,
                )
                stats["skipped"] += 1
                continue
            try:
                corrected = parse_fact_value(fact, str(block.get("value")))
            except ValueError as exc:
                _LOGGER.warning(
                    "fact correction skipped on %s: %s", record_key, exc
                )
                stats["skipped"] += 1
                continue
            if corrected == current.value:
                stats["redundant"] += 1
            view.apply_correction(
                FactResult(
                    fact=fact,
                    value=corrected,
                    confidence="high",
                    downgraded=False,
                    downgrade_reason=None,
                    spans=(),
                    provenance="human_corrected",
                )
            )
            stats["applied"] += 1
            applied_here += 1
        if applied_here:
            stats["records"] += 1
    if stats["applied"] or stats["skipped"]:
        _LOGGER.info(
            "fact corrections (%s %s): applied %d across %d records "
            "(%d redundant — model now agrees), skipped %d",
            state, lens, stats["applied"], stats["records"],
            stats["redundant"], stats["skipped"],
        )
    return stats


# v25 (issue #124 PR 2 / #111) — typed-reason annotation order on the
# SF fold label. Det fact first (highest specificity per the #111
# framing), then state-scope-delta enum, then the #59 sourcing
# constraint enum. Each entry: (fact_name, value → reason_string).
# A value mapping returning None means "no reason for this value"
# (e.g., ``state_scope_delta == "neutral"`` is the no-evidence
# default and shouldn't surface).
_SF_TYPED_REASON_ENUMS: tuple[
    tuple[str, dict[str, str | None]], ...
] = (
    (
        "extension_fidelity_divergence",
        {
            "replaces_core_field_shape": "replaces_core_field_shape",
            "none": None,
        },
    ),
    (
        "state_scope_delta",
        {
            "narrows": "narrows_core_scope",
            "broadens": "broadens_core_scope",
            "neutral": None,
        },
    ),
    (
        "sourcing_constraint_documented",
        {
            "transformation": "transformation",
            "field_filter": "field_filter",
            "external_sourcing": "external_sourcing",
            "custom_enumeration": "custom_enumeration",
            "none": None,
            "unspecified": None,
        },
    ),
)


def _collect_sf_typed_reasons(view: FactView) -> list[str]:
    """Read the v25 SF-fold typed-reason facts off ``view`` and return
    the parenthetical reason list in canonical order.

    Returns a list of reason strings — empty when no typed-reason fact
    fires, otherwise the ordered list per ``_SF_TYPED_REASON_ENUMS``.
    Skips facts that are missing from the view, downgraded, or carrying
    a no-evidence default value (mapped to ``None``).
    """
    reasons: list[str] = []
    for fact_name, value_map in _SF_TYPED_REASON_ENUMS:
        if not view.has(fact_name):
            continue
        value = view.enum(fact_name)
        if value is None:
            continue
        reason = value_map.get(value)
        if reason is None:
            continue
        reasons.append(reason)
    return reasons


def _compute_nachos_adjustments(
    view: FactView,
    nachos_dim: DimensionScore | None,
    *,
    source_ext_necessity: dict[str, bool | None] | None = None,
    sf_dim: DimensionScore | None = None,
) -> tuple[float | None, bool, str | None, list[str]]:
    """Plan §4/§5/§6 arithmetic + in-scope classifier + justification.

    Returns ``(adjusted_nachos_score, in_scope, nachos_justification,
    review_reasons)``. Consumes the ``DimensionScore`` for
    ``nachos_score`` + the record's FactView for the cross-fact inputs
    (source/extension classification, extension necessity, cross-entity
    reconciliation, aggregation/concat, conditional).

    Cross-lens fact-borrow: on spine-lens rows with
    ``source == "extension"``, ``extension_is_necessary`` isn't in the
    spine-lens fact pool (the runtime filters that fact to source-lens
    extension rows). ``source_ext_necessity`` lets the caller pass the
    source-lens sidecar lookup so the adjustment arithmetic lands
    consistently across lenses. Missing lookup → conservative fallback
    (treat as necessary extension +0.5, flag
    ``extension_necessity_unresolved``).

    ``sf_dim`` (semantic_fidelity DimensionScore, source-lens only) —
    when ``nachos_dim.value == 0`` AND ``sf_dim.value`` is one of
    {0, 1, 2}, fold a fidelity adjustment into the score per issue #55
    (B-gated ``base0`` scheme): tier 2 → +0.5, tier 1 / 0 → +1.0. The
    base0 gate enforces non-overlap with the rubric base — SF reads the
    same source text the rubric reads, so stacking on rows where the
    rubric already credited divergence would double-count. Spine-lens
    callers pass ``None`` (semantic_fidelity is source-lens only).
    """
    review_reasons: list[str] = []
    if nachos_dim is None or nachos_dim.value is None:
        return None, False, None, review_reasons

    base = nachos_dim.value
    is_extension = view.source == "extension"

    # v18 — issue #84 (2026-04-29): two-tier extension weighting
    # reinstated. The per-element `extension_is_necessary` judgment
    # drives the fork: True → +0.5 necessary_ext, False → +1
    # unnecessary_ext, None → conservative +0.5 with
    # `extension_necessity_unresolved` review flag. Cross-lens fact-
    # borrow on spine-lens (`source_ext_necessity`) carries forward
    # from v17. Calibration prerequisite landed via #85 prompt-
    # tightening + cold re-extract on all 4 states.
    ext_necessary: bool | None
    if is_extension:
        direct = view.raw_fact_results(["extension_is_necessary"]).get(
            "extension_is_necessary"
        )
        if direct is not None and direct.value is not None:
            ext_necessary = bool(direct.value)
        else:
            # Issue #94: borrow dict is keyed on ``record_key.lower()`` so
            # source-lens PascalCase / spine-lens camelCase joins resolve.
            # Tests + production both pass dicts built by
            # ``_load_source_extension_facts``; in-test direct construction
            # follows the same lower-cased contract.
            lower_key = view.record_key.lower()
            if (
                source_ext_necessity is not None
                and lower_key in source_ext_necessity
            ):
                borrowed = source_ext_necessity[lower_key]
                if borrowed is None:
                    ext_necessary = None
                else:
                    ext_necessary = bool(borrowed)
            else:
                ext_necessary = None
    else:
        ext_necessary = None

    # Cross-entity reconciliation via the SAME rule-stage helper the
    # cascade uses (`rules._reconcile_cross_entity`) so a future
    # calibration change lands once (issue #213 item 3). The helper's
    # flag is `hce AND targets >= 1`; the multi-entity fork below adds
    # `targets >= 2`, so `hce and targets >= 2` is arithmetically
    # identical to the previous inline `hce_raw and targets >= 2`.
    # v16 — B5 (issue #63 POC interim call): bare FK references are
    # element-only scope, so the cross-entity / multi-entity adjustment
    # is suppressed for them in lockstep with the rule-stage gate
    # in `nachos_score` (the same two-line suppression the cascade
    # applies after calling the helper).
    # Element-level cross-entity narrative still
    # surfaces through `has_conditional_logic`; only the structural
    # FK-chain reach is gated here.
    hce, targets = _reconcile_cross_entity(view)
    if view.bool("element_is_bare_fk_reference"):
        hce = False
        targets = 0
    multi_entity = hce and targets >= 2

    # v23 (issue #124 Option 2) — compute the necessity branch and the
    # SF fold branch independently, then apply non-stacking
    # max-of-two with fidelity-wins tie-break when BOTH fire at base 0.
    # Multi-entity stays an independent additive axis. Both labels
    # render in `nachos_justification` for audit clarity even when the
    # non-stacking rule discards one branch's magnitude — the
    # `fidelity_necessity_dual_fire` review reason marks dual-fire rows.
    necessity_adj: float = 0.0
    necessity_label: str | None = None
    if is_extension:
        if ext_necessary is False:
            necessity_adj = _NACHOS_ADJ_UNNECESSARY_EXT
            necessity_label = ADJ_LABEL_UNNECESSARY_EXT
        elif ext_necessary is True:
            necessity_adj = _NACHOS_ADJ_NECESSARY_EXT
            necessity_label = ADJ_LABEL_NECESSARY_EXT
        else:
            # Unresolved (None): conservative fallback to +0.5 with
            # review flag so analysts can audit the missing judgment.
            necessity_adj = _NACHOS_ADJ_NECESSARY_EXT
            necessity_label = ADJ_LABEL_NECESSARY_EXT
            review_reasons.append("extension_necessity_unresolved")

    # v12 semantic_fidelity → adjusted_nachos_score (B-gated `base0`,
    # issue #55). Fires on source-lens rows where the rubric base is 0
    # AND SF is below tier 3. Tier 2 (divergent_explained) → +0.5;
    # tier 1 (divergent_unclear) and tier 0 (unresolved) → +1.0. Spine
    # lens passes ``sf_dim=None`` so this branch is a no-op there.
    #
    # v25 — issue #124 PR 2 / #111: when SF fold fires, annotate the
    # label with typed-reason parentheticals from
    # ``extension_fidelity_divergence`` (det.v11),
    # ``state_scope_delta``, and ``sourcing_constraint_documented`` so
    # analysts can see *which* downstream mechanisms drove the fold
    # without changing the magnitude. Order: shape-divergence first,
    # then scope delta, then sourcing constraint. Empty annotation
    # leaves the v24 label format unchanged.
    sf_adj: float = 0.0
    sf_label: str | None = None
    if (
        sf_dim is not None
        and sf_dim.value is not None
        and base == 0
    ):
        sf_v = sf_dim.value
        if sf_v == 2:
            sf_adj = _NACHOS_ADJ_FIDELITY_DIVERGENT_EXPLAINED
            sf_label = ADJ_LABEL_SF_EXPLAINED
        elif sf_v in (0, 1):
            sf_adj = _NACHOS_ADJ_FIDELITY_DIVERGENT_UNCLEAR
            sf_label = ADJ_LABEL_SF_UNCLEAR
        if sf_label is not None:
            typed_reasons = _collect_sf_typed_reasons(view)
            if typed_reasons:
                sf_label = f"{sf_label} ({', '.join(typed_reasons)})"

    # Non-stacking precedence: when BOTH branches fire at base 0, take
    # max-of-two. Fidelity wins on >= comparison so the #111 framing
    # ("this is a fidelity refinement") carries on tied magnitudes.
    if base == 0 and necessity_label is not None and sf_label is not None:
        nec_sf_combined = sf_adj if sf_adj >= necessity_adj else necessity_adj
        review_reasons.append("fidelity_necessity_dual_fire")
    else:
        nec_sf_combined = necessity_adj + sf_adj

    multi_entity_adj = _NACHOS_ADJ_MULTI_ENTITY if multi_entity else 0.0
    adj = nec_sf_combined + multi_entity_adj

    # Label order preserved: necessity → multi-entity → SF fold.
    adj_labels: list[str] = []
    if necessity_label is not None:
        adj_labels.append(necessity_label)
    if multi_entity:
        adj_labels.append(ADJ_LABEL_MULTI_ENTITY)
    if sf_label is not None:
        adj_labels.append(sf_label)

    adjusted = min(base + adj, _NACHOS_MAX_ADJUSTED_SCORE)

    # In-scope = True default (v10 methodology scope rectification,
    # 2026-04-26). Methodology spec
    # `docs/NACHOS_Methodology_External review.xlsx` `Logic_Dec2025`
    # rows 33-46 defines in-scope as the default for every Ed-Fi-mappable
    # element; the 0-3 tier is the complexity score *within* in-scope.
    # Out-of-scope is a narrow, explicitly-enumerated set (flat-file
    # validations, data-quality / state-conformance enforcement,
    # extensions not pushed to SIS) that the current fact set cannot
    # detect deterministically. Prior versions (v4-v9) gated in_scope on
    # rule triggers, which incorrectly blanked ~90% of rows that the
    # methodology scores as tier 0. Future analyst-override mechanism
    # may flip individual rows to False for the narrow exclusion set.
    in_scope = True

    # Justification text: "rule_matched[; adj labels]" — plan §6.
    rule_label = nachos_dim.rule_matched
    if adj_labels:
        nachos_justification = f"{rule_label}; {', '.join(adj_labels)}"
    else:
        nachos_justification = rule_label

    return adjusted, in_scope, nachos_justification, review_reasons


def _score_one(
    view: FactView,
    lens: str,
    *,
    source_ext_necessity: dict[str, bool | None] | None = None,
) -> ScoredRecord:
    """Score one record end-to-end: rules → dimensions → aggregate.

    ``source_ext_necessity`` is the spine-lens cross-lens fact-borrow
    lookup (see ``_load_source_extension_facts``). Ignored on
    source-lens — extension_is_necessary is in-pool already there.
    """
    dim_list = score_record(view, lens=lens)
    dimensions = {d.name: d for d in dim_list}

    if lens == "spine":
        quality_dim_names = SPINE_QUALITY_DIMENSIONS
        complexity_dim = SPINE_COMPLEXITY_DIMENSION
        rule_inputs = SPINE_RULE_INPUTS
    elif lens == "source":
        quality_dim_names = SOURCE_QUALITY_DIMENSIONS
        complexity_dim = SOURCE_COMPLEXITY_DIMENSION
        rule_inputs = SOURCE_RULE_INPUTS
    else:
        raise ValueError(f"unknown lens {lens!r}; supported: 'spine', 'source'")

    quality_values = [
        d.value
        for name, d in dimensions.items()
        if name in quality_dim_names and d.value is not None
    ]
    quality_mean_diagnostic = _mean(quality_values)

    if complexity_dim is not None:
        complexity = dimensions.get(complexity_dim)
        complexity_score = complexity.value if complexity is not None else None
    else:
        complexity_score = None

    confidence_composite = _min_confidence(
        [d.confidence for d in dimensions.values()]
    )

    # H1 widening: include productization-signal facts in
    # ``fact_provenance`` alongside rule inputs. Rules consult only
    # rule_inputs (via FactView); fact_provenance is the observability
    # surface so the productization axis needs to land here to be
    # downstream-visible. Only signals actually loaded by
    # ``load_fact_pool`` (i.e. present on ``view``) surface — missing
    # productization-signal artifacts are logged as warnings but don't
    # leave a stale "missing_from_artifact" entry.
    #
    # LENS_OBSERVABILITY_FACTS layers on top — facts extracted by the
    # shared harness but outside this lens's rule-input set. Spine
    # picks up ``semantic_class`` here so the spine workbook can
    # surface the alignment verdict even though the spine cascade
    # doesn't consult it.
    lens_obs = LENS_OBSERVABILITY_FACTS.get(lens, ())
    provenance_facts = (
        sorted(rule_inputs)
        + [f for f in PRODUCTIZATION_SIGNAL_FACTS if view.has(f)]
        + [f for f in lens_obs if view.has(f)]
    )
    fact_provenance = _fact_provenance(view, provenance_facts)

    # Phase F: NACHOS methodology arithmetic + in-scope classifier.
    # ``sf_dim`` is source-lens only (semantic_fidelity is not in
    # SPINE_DIMENSIONS); ``dimensions.get`` returns ``None`` on spine
    # so the v12 adjustment branch is naturally a no-op there.
    nachos_dim = dimensions.get("nachos_score")
    sf_dim = dimensions.get("semantic_fidelity")
    (
        adjusted_nachos_score,
        in_scope,
        nachos_justification,
        extra_review_reasons,
    ) = _compute_nachos_adjustments(
        view,
        nachos_dim,
        source_ext_necessity=source_ext_necessity,
        sf_dim=sf_dim,
    )

    review = _build_review_block(
        dimensions,
        fact_provenance,
        lens,
        in_scope=in_scope,
        nachos_tier=nachos_dim.value if nachos_dim is not None else None,
        confidence_composite=confidence_composite,
    )
    # Merge the ``extension_necessity_unresolved`` review reason from
    # the adjustment arithmetic — it surfaces on records where the
    # spine-lens aggregate couldn't resolve extension necessity.
    if extra_review_reasons:
        merged = sorted(set(review["reasons"]) | set(extra_review_reasons))
        from src.report.review_queue import route_review  # avoid import cycle
        review = {
            "needs_review": True,
            "reasons": merged,
            "route": route_review(merged),
        }

    return ScoredRecord(
        record_key=view.record_key,
        entity=view.entity,
        element_name=view.element_name,
        dimensions=dimensions,
        _quality_mean_diagnostic=quality_mean_diagnostic,
        complexity_score=complexity_score,
        confidence_composite=confidence_composite,
        fact_provenance=fact_provenance,
        review=review,
        adjusted_nachos_score=adjusted_nachos_score,
        in_scope=in_scope,
        nachos_justification=nachos_justification,
        documentation_source=getattr(view, "documentation_source", "source_doc"),
    )


# ---------------------------------------------------------------------------
# Artifact shape
# ---------------------------------------------------------------------------


def _dimension_to_dict(d: DimensionScore) -> dict[str, Any]:
    return {
        "value": d.value,
        "rule_matched": d.rule_matched,
        "inputs_used": d.inputs_used,
        "confidence": d.confidence,
    }


def _scored_record_to_dict(sr: ScoredRecord) -> dict[str, Any]:
    return {
        "record_key": sr.record_key,
        "entity": sr.entity,
        "element_name": sr.element_name,
        "dimensions": {
            name: _dimension_to_dict(d) for name, d in sr.dimensions.items()
        },
        "_quality_mean_diagnostic": sr._quality_mean_diagnostic,
        "complexity_score": sr.complexity_score,
        "confidence_composite": sr.confidence_composite,
        "fact_provenance": sr.fact_provenance,
        "review": sr.review,
        # Phase F NACHOS methodology surfaces.
        "adjusted_nachos_score": sr.adjusted_nachos_score,
        "in_scope": sr.in_scope,
        "nachos_justification": sr.nachos_justification,
        # v13 — discovery_lens provenance.
        "discovery_lens": sr.discovery_lens,
        # v21 — surface the documentation_source on each scored row so
        # downstream consumers (workbooks, reviewer-comparison) can render
        # swagger-backfilled rows distinctly. Aggregate-level filtering
        # already happens above this serializer; the per-row field is for
        # display + audit.
        "documentation_source": sr.documentation_source,
    }


def _infer_edfi_version(state: str, lens: str) -> str | None:
    """Read edfi_version from the elements artifact header — don't guess."""
    elements_path = state_elements_path(state, lens)  # type: ignore[arg-type]
    if not elements_path.exists():
        return None
    try:
        data = StateElements.model_validate_json(
            elements_path.read_text(encoding="utf-8")
        )
    except Exception:  # pragma: no cover — malformed artifact is its own error
        return None
    return data.edfi_version


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run(
    *,
    state: str,
    lens: str = "spine",
    artifacts_dir: Path | None = None,
    out_path: Path | None = None,
    model: str = "claude-sonnet-4-6",
    prompt_version: str = "phase-a.v1",
    allow_stale: bool = False,
    source_sidecar_path: Path | None = None,
    curation_base: Path | None = None,
) -> dict[str, Any]:
    """Score every record with a full fact pool; write the §8.1 sidecar.

    ``artifacts_dir`` — where per-fact JSONL artifacts live (defaults to
    ``data/out/scoring/phase_a/``).

    ``out_path`` — sidecar destination (defaults to
    ``data/out/{state}_scores_{lens}.json``).

    ``source_sidecar_path`` — explicit location of the source-lens
    sidecar for the spine-lens extension fact-borrow; defaults to
    deriving it from ``artifacts_dir`` (issue #212 item 3 test seam).

    ``curation_base`` — where the committed curation sidecars live
    (defaults to ``data/curation/``); analyst fact corrections found
    there overlay the pool before the rule cascade runs (issue #249).
    Tests pass a tmp dir so hermetic runs can never read a committed
    correction.

    ``model`` / ``prompt_version`` — recorded in the sidecar header for
    reproducibility. These are metadata only; the rule stage doesn't
    consult them.

    Returns the header dict (sidecar minus the ``scores`` array) for
    callers that want a summary without re-reading the file.
    """
    if lens not in ("spine", "source"):
        raise ValueError(f"unknown lens {lens!r}; supported: 'spine', 'source'")

    state = state.upper()
    root = artifacts_dir or scoring_phase_a_dir()
    pool = load_fact_pool(state, lens, artifacts_dir=root)

    # Issue #249: analyst fact corrections overlay the pool BEFORE the
    # rule cascade (and before the spine borrow below reads anything),
    # so the unchanged rules recompute from human-corrected input.
    corrections = _load_fact_corrections(state, lens, curation_base)
    if corrections:
        _apply_fact_corrections(pool, corrections, state=state, lens=lens)

    # Phase F cross-lens fact-borrow: when aggregating spine-lens, load
    # the source-lens sidecar once so source=extension rows can read
    # extension_is_necessary for the +1/+0.5 adjustment. Empty dict on
    # source-lens and on spine-lens runs where the source sidecar
    # doesn't exist yet — records fall back to
    # "extension_necessity_unresolved" with a +0.5 conservative
    # adjustment.
    source_ext_necessity: dict[str, bool | None] | None = None
    if lens == "spine":
        source_ext_necessity = _load_source_extension_facts(
            state,
            artifacts_dir=root,
            allow_stale=allow_stale,
            source_sidecar_path=source_sidecar_path,
        )

    scored: list[ScoredRecord] = [
        _score_one(v, lens, source_ext_necessity=source_ext_necessity)
        for v in pool.values()
    ]

    # Issue #70 v21 close-out posture: swagger-backfilled rows ride along
    # in the source-lens artifact for workbook + reviewer-comparison
    # visibility, but headline aggregates (mean NACHOS, dim distributions,
    # review queue) are computed over rows the state actually documented.
    # Filter on ``documented`` (carried via ``FactView`` from
    # ``ElementRecord``) — works for both lenses without lens-specific
    # branching: source-lens swagger rows carry ``documented=False`` and
    # spine-lens rows for swagger-only entities also stay
    # ``documented=False`` under v21 (the v20 spine-flip is reverted).
    # Per-row scoring (rules and the resulting dimension values on each
    # ``ScoredRecord``) still runs for these rows so the sidecar carries
    # them; the suppression is purely at the rollup layer.
    def _is_documented_authored(s: ScoredRecord) -> bool:
        return _scored_documented_lookup.get(s.record_key, False)

    # ``ScoredRecord`` doesn't carry the ``documented`` flag directly —
    # we look it up from the ``pool`` (FactView dict) the scorer just
    # consumed, since both keep the same record_key.
    _scored_documented_lookup: dict[str, bool] = {
        view.record_key: getattr(view, "documented", False)
        for view in pool.values()
    }
    headline_scored = [s for s in scored if _is_documented_authored(s)]

    needs_review_count = sum(
        1 for s in headline_scored if s.review["needs_review"]
    )

    per_record_values = [
        s._quality_mean_diagnostic
        for s in headline_scored
        if s._quality_mean_diagnostic is not None
    ]
    mean_quality: float | None = None
    if per_record_values:
        mean_quality = round(sum(per_record_values) / len(per_record_values), 4)

    dim_names = SPINE_DIMENSIONS if lens == "spine" else SOURCE_DIMENSIONS
    dim_stats: dict[str, dict[str, Any]] = {}
    for dim_name in dim_names:
        values = [
            s.dimensions[dim_name].value
            for s in headline_scored
            if dim_name in s.dimensions and s.dimensions[dim_name].value is not None
        ]
        dist = {i: values.count(i) for i in (0, 1, 2, 3)}
        dim_stats[dim_name] = {
            "count": len(values),
            "mean": round(sum(values) / len(values), 4) if values else None,
            "distribution": dist,
        }

    # Phase F NACHOS header aggregates (plan §6): in-scope count, tier
    # histogram + mean restricted to in-scope rows (the population
    # methodology actually scores), adjusted-score histogram. Stakeholders
    # should read these — the rollup MD NACHOS section wires them through.
    in_scope_count = sum(1 for s in headline_scored if s.in_scope)
    in_scope_nachos_values: list[int] = [
        s.dimensions["nachos_score"].value
        for s in headline_scored
        if s.in_scope
        and "nachos_score" in s.dimensions
        and s.dimensions["nachos_score"].value is not None
    ]
    in_scope_adjusted_values: list[float] = [
        s.adjusted_nachos_score
        for s in headline_scored
        if s.in_scope and s.adjusted_nachos_score is not None
    ]
    nachos_score_histogram = {
        str(i): in_scope_nachos_values.count(i) for i in (0, 1, 2, 3)
    }
    # Bucket the adjusted-score histogram by 0.5 increments 0..4.5.
    adjusted_histogram: dict[str, int] = {
        f"{b:.1f}": 0 for b in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5)
    }
    for v in in_scope_adjusted_values:
        # Round to nearest 0.5 for bucket indexing — the adjustment
        # arithmetic only produces 0.5-multiples.
        bucket_idx = round(v * 2) / 2
        key = f"{bucket_idx:.1f}"
        if key in adjusted_histogram:
            adjusted_histogram[key] += 1
    mean_nachos_score = (
        round(sum(in_scope_nachos_values) / len(in_scope_nachos_values), 4)
        if in_scope_nachos_values
        else None
    )
    mean_adjusted_nachos_score = (
        round(sum(in_scope_adjusted_values) / len(in_scope_adjusted_values), 4)
        if in_scope_adjusted_values
        else None
    )

    # Integration Profile — documentation_gap hit count. Plan §4
    # Mitigation 3: "Aggregate stats in the sidecar header:
    # documentation_gap_count per state." Convenience field; the same
    # count is derivable from
    # ``dimension_stats["documentation_gap"]["distribution"]["1"]``,
    # but stakeholder-facing readers should not have to dig.
    gap_stats = dim_stats.get("documentation_gap", {})
    gap_dist = gap_stats.get("distribution", {})
    documentation_gap_count = int(gap_dist.get(1, 0))

    header: dict[str, Any] = {
        "state": state,
        "lens": lens,
        "edfi_version": _infer_edfi_version(state, lens),
        "scored_at": _now_iso(),
        "model": model,
        "prompt_version": prompt_version,
        "scoring_plan_version": SCORING_PLAN_VERSION,
        "record_count": len(scored),
        "scored_count": len(scored),
        "skipped_count": 0,
        "mean_quality_score": mean_quality,
        "needs_review_count": needs_review_count,
        "dimension_stats": dim_stats,
        # Phase F NACHOS header aggregates.
        "in_scope_count": in_scope_count,
        "nachos_score_histogram": nachos_score_histogram,
        "adjusted_nachos_score_histogram": adjusted_histogram,
        "mean_nachos_score": mean_nachos_score,
        "mean_adjusted_nachos_score": mean_adjusted_nachos_score,
        # Integration Profile — documentation-gap signal count.
        "documentation_gap_count": documentation_gap_count,
    }

    payload: dict[str, Any] = dict(header)
    payload["scores"] = [_scored_record_to_dict(s) for s in scored]

    destination = out_path or state_scores_path(state, lens)  # type: ignore[arg-type]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _LOGGER.info(
        "wrote %s: %d records · mean quality=%s · review=%d",
        destination,
        len(scored),
        mean_quality,
        needs_review_count,
    )
    return header


# ---------------------------------------------------------------------------
# Convenience: all four states
# ---------------------------------------------------------------------------


def run_all(
    *,
    lens: str = "spine",
    states: list[str] | None = None,
    artifacts_dir: Path | None = None,
    out_dir: Path | None = None,
    model: str = "claude-sonnet-4-6",
    prompt_version: str = "phase-a.v1",
    allow_stale: bool = False,
    curation_base: Path | None = None,
) -> list[dict[str, Any]]:
    """Aggregate for every state in ``states`` (default AZ/WI/MN/TX)."""
    targets = states or list(SUPPORTED_STATES)
    headers: list[dict[str, Any]] = []
    for state in targets:
        out_path = None
        if out_dir is not None:
            out_path = out_dir / f"{state.lower()}_scores_{lens}.json"
        headers.append(
            run(
                state=state,
                lens=lens,
                artifacts_dir=artifacts_dir,
                out_path=out_path,
                model=model,
                prompt_version=prompt_version,
                allow_stale=allow_stale,
                curation_base=curation_base,
            )
        )
    return headers
