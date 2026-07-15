"""State-facing recommendations generator — Track C (landing phase).

Closes the second brief-mandated output: per-element recommendations
that would reduce a row's NACHOS cost / raise a row's documentation
quality. The scoring evidence needed to author those recommendations
is already structured in the sidecar (``data/out/{state}_scores_{lens}.
json``), so the gap between "scored row" and "recommendation string" is
a formatting problem, not a scoring problem.

Design contract: ``docs/recommendations-generator.md``. Landing-plan
Track C. Deterministic templates first — one per ``(dimension,
rule_slug)`` combination — with optional LLM polish later. AZ-only
validation for the demo; the CLI surface supports ``--state`` so other
states can be brought on by clone-and-test.

Same discipline as ``score/rules.py``:

- ``import re`` is forbidden (enforced by
  ``tests/test_report_recommendations.py::TestPolicyDiscipline``).
- The deterministic layer never reads narrative text fields from the
  element files — it only consumes the structured sidecar (dimension
  values, rule slugs, fact provenance, review metadata). Narrative
  polish is gated behind the optional LLM layer, which does not ship
  in this prototype.

Public surface:

- ``Template`` — one recommendation template, keyed by
  ``(dimension, rule_slug)``.
- ``RECOMMENDATIONS`` — the lookup table. Covers every non-at-target
  slug that appears in the AZ source-lens and spine-lens sidecars.
- ``generate_for_row(row)`` — per-record recommendation objects.
- ``generate_state(state, lens, ...)`` — full-state generation;
  returns a dict matching the ``review_queue`` / ``divergence`` shape.
- ``render_markdown(result, hero_n=4)`` — human-readable digest.
- ``run(...)`` — dual-writes ``data/out/{state}_recommendations.
  {json,md}``. CRITICAL: plain function, NOT ``@click.command``. The
  Click wrapper lives in ``src/poc3/cli.py``.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from src.states import SUPPORTED_STATES
from src.utils.paths import out_dir, state_scores_gap_path, state_scores_path

_LOGGER = logging.getLogger(__name__)

# Lens names supported by the recommendations generator. ``source`` and
# ``spine`` read from ``state_scores_{lens}.json`` and produce per-dimension
# recommendations off the rule cascade. ``gap`` (issue #73) reads from
# ``state_scores_gap.json`` and produces ONE record-level recommendation per
# spine-anchored gap row plus an optional structural-depth callout — the
# rule-cascade templates fire mostly tier-0-spam on gap rows by construction
# and are deliberately bypassed.
Lens = Literal["source", "spine", "gap"]

# Dimensions where the methodology's "better" direction is LOWER (cost
# axis). ``nachos_score`` and the spine-lens ``business_logic_complexity``
# carry this property — both score implementation cost, so tier 0 is the
# at-target value and the recommendation is "simplify" rather than "lift"
# (plan §7.2 / §7.3, methodology Dec 2025 spec). For these dimensions the
# generator's target_tier is 0 and it emits recommendations when
# ``value > 0``. For every other dimension the target is 3 and
# recommendations emit when ``value < 3``.
_INVERSE_DIMENSIONS: frozenset[str] = frozenset(
    {"nachos_score", "business_logic_complexity"}
)


# (state, lens) → record_keys that must appear in hero_examples
# regardless of the natural ranking. Used to keep the verbatim
# citations in docs/recommendations.md §3.b and §5.4 stable across
# re-scorings and ranking tweaks — a pinned record stays in the hero
# list, the doc never goes stale against a shifted top-N. Keep this
# list short; pins pay for themselves only when the doc names the row.
_PINNED_HERO_KEYS: dict[tuple[str, str], tuple[str, ...]] = {
    ("AZ", "source"): ("AZ|Calendar|TotalInstructionalDays",),
}


# Entity → upstream-workflow class. Substring match (longest needle wins
# on ties via order). Lets the deterministic templates name a generic
# upstream business process keyed by the entity the recommendation
# applies to — covers ~80% of Maria-style specificity ("calendar and
# instructional-day authoring workflow") without the LLM polish that
# would be required to invent entity-specific upstream-process names.
_ENTITY_WORKFLOW_RULES: tuple[tuple[str, str], ...] = (
    # Specific patterns first; first match wins.
    ("FoodService", "school nutrition program workflow"),
    ("SpecialEducation", "special-education eligibility-determination workflow"),
    ("Section504", "504 eligibility-determination workflow"),
    ("DropOut", "dropout-recovery program workflow"),
    ("Discipline", "discipline-incident workflow"),
    ("Assessment", "assessment workflow"),
    ("CalendarDate", "calendar and instructional-day authoring workflow"),
    ("Calendar", "calendar and instructional-day authoring workflow"),
    ("GradingPeriod", "grading-period authoring workflow"),
    ("Session", "calendar and session authoring workflow"),
    ("Attendance", "attendance workflow"),
    ("Payroll", "financial-reporting workflow"),
    ("Budget", "financial-reporting workflow"),
    ("Migrant", "migrant-program eligibility workflow"),
    ("Homeless", "homeless-program eligibility workflow"),
    ("TitleI", "Title I program-eligibility workflow"),
    ("Language", "language-program eligibility workflow"),
    ("CTE", "CTE program-enrollment workflow"),
    ("EarlyEducation", "early-education program-enrollment workflow"),
    ("Eligibility", "eligibility-determination workflow"),
    ("ProgramEvaluation", "program-evaluation workflow"),
    ("Program", "program-funding workflow"),
    ("Transcript", "grade-reporting and transcript workflow"),
    ("Course", "course-catalog workflow"),
    ("Section", "section and course workflow"),
    ("Grade", "grade-reporting workflow"),
    ("Staff", "staff-records workflow"),
    ("Parent", "contact-records workflow"),
    ("Contact", "contact-records workflow"),
    ("Enrollment", "enrollment workflow"),
    ("StudentSchoolAssociation", "enrollment workflow"),
    ("StudentEducationOrganization", "enrollment workflow"),
    ("LocalEducationAgency", "organization master-data workflow"),
    ("School", "organization master-data workflow"),
    ("Student", "student-records workflow"),
)
_DEFAULT_WORKFLOW = "upstream business process"


def workflow_for(entity: str | None) -> str:
    """Return the upstream-workflow class string for ``entity``.

    Used by templates whose ``message`` carries a ``{workflow}``
    placeholder. Generic enough to cover all four states without
    state-specific knowledge; specific enough that the rendered message
    reads as a real instruction rather than "consider doing something
    upstream."
    """
    if not entity:
        return _DEFAULT_WORKFLOW
    for needle, label in _ENTITY_WORKFLOW_RULES:
        if needle in entity:
            return label
    return _DEFAULT_WORKFLOW


@dataclass(frozen=True)
class Template:
    """One recommendation template, keyed externally by ``(dimension, rule_slug)``.

    ``target_tier`` is the tier a reviewer could plausibly reach if
    they follow the recommendation. For cost-axis dimensions
    (``nachos_score``) this is 0 (simplify), not 3.

    ``message`` is a ``str.format`` template using named fields drawn
    from the scored row: ``entity``, ``element_name``, plus anything
    the generator injects.

    ``evidence_fact`` names the single fact whose value most directly
    supports the recommendation. If absent, the recommendation is
    structural (applies to every row with that rule slug) rather than
    fact-specific.

    ``rationale`` is a one-sentence analyst-facing gloss that appears
    in the markdown digest but not the per-record JSON — used to
    explain *why* this recommendation fires for this tier.
    """

    dimension: str
    rule_slug: str
    target_tier: int
    message: str
    evidence_fact: str | None = None
    rationale: str = ""


# ---------------------------------------------------------------------------
# Template catalog
# ---------------------------------------------------------------------------
#
# Covers every non-at-target rule slug observed on AZ source + spine
# sidecars (verified via Counter over data/out/az_scores_*.json). At-
# target slugs (tier_3_* aligned/exact/etc., extension_justification.
# not_applicable, semantic_fidelity.null_not_applicable) have no
# template entry — they are skipped by the generation loop.
#
# Dimension target tier is captured per-template so the rule for
# ``nachos_score`` (inverse axis, target=0) lives alongside its message
# rather than being a generator-loop branch. This keeps the intent
# ("simplify the logic") visible next to the recommendation text.


RECOMMENDATIONS: dict[tuple[str, str], Template] = {
    # --------------------------- source-lens ---------------------------
    ("canonical_name_alignment", "tier_0_unresolved"): Template(
        dimension="canonical_name_alignment",
        rule_slug="tier_0_unresolved",
        target_tier=3,
        evidence_fact="element_name_matches_canonical",
        message=(
            "The recommendation is to either rename `{entity}.{element_name}` "
            "to its Ed-Fi canonical equivalent within the state source "
            "specification or, if this is a genuinely novel state extension, "
            "mark it as such so it is no longer treated as an unresolved "
            "canonical match. Vendors then map the field directly to a known "
            "canonical slot or implement it as a documented extension rather "
            "than guessing at the target. This lifts canonical_name_alignment "
            "to tier 3."
        ),
        rationale=(
            "Unresolved names block automated mapping and force every SIS "
            "vendor to guess at the canonical target."
        ),
    ),
    # ("canonical_name_alignment", "tier_1_resolved") intentionally absent:
    # alias-resolved names work today — the spine alias table maps the
    # source name (abbreviation / pluralization / synonym) to the canonical
    # slot, vendors integrate without per-row guidance. Asking the state
    # to rename the source spec to remove the alias-table tolerance is an
    # Ed-Fi-Alliance-vs-state-author political ask rather than a vendor
    # blocker. The dimension still scores tier 1 on these rows in the
    # Scores sheet so the gap stays visible.
    # ("canonical_name_alignment", "tier_2_cosmetic") intentionally absent:
    # cosmetic-only casing drift is graded by the rule (still scored tier 2
    # in the Scores sheet) but does not generate a per-row recommendation.
    # The dilution risk of ~500 cosmetic rows across the four-state corpus
    # outweighs the value of nudging each one individually; align-to-canonical
    # guidance lives in the dimension overview prose instead.
    ("definition_quality", "tier_0_missing"): Template(
        dimension="definition_quality",
        rule_slug="tier_0_missing",
        target_tier=3,
        evidence_fact="definition_present",
        message=(
            "The recommendation is for the state authoring team to publish a "
            "definition for `{entity}.{element_name}` in the source "
            "specification — describing the business concept the element "
            "represents (not just its data type) and any state-specific "
            "narrowing beyond the Ed-Fi standard. Vendors then validate "
            "intent against the Ed-Fi canonical instead of inferring it from "
            "the element name alone. This lifts definition_quality to tier 3."
        ),
        rationale=(
            "Missing definitions force reviewers to infer intent from the "
            "element name alone, which is the single largest source of "
            "downstream misinterpretation."
        ),
    ),
    ("definition_quality", "tier_1_minimal"): Template(
        dimension="definition_quality",
        rule_slug="tier_1_minimal",
        target_tier=3,
        evidence_fact="definition_text_substantive",
        message=(
            "The recommendation is to expand the definition of "
            "`{entity}.{element_name}` in the state source specification to "
            "state the concept, the population it applies to, and any "
            "narrowing the state has added beyond the Ed-Fi standard. "
            "Vendors then have enough text to implement against rather than "
            "just an acknowledgment that the field exists. This lifts "
            "definition_quality to tier 3."
        ),
        rationale=(
            "Thin definitions pass the \"present\" check but don't give "
            "vendors enough to implement against."
        ),
    ),
    ("definition_quality", "tier_2_substantive"): Template(
        dimension="definition_quality",
        rule_slug="tier_2_substantive",
        target_tier=3,
        evidence_fact="definition_adds_detail_beyond_edfi",
        message=(
            "The recommendation is for the state authoring team to make "
            "explicit in the definition of `{entity}.{element_name}` what "
            "(if anything) the state adds beyond the Ed-Fi standard — "
            "additional business rules, scope narrowing, reporting cadence "
            "— or to cite the Ed-Fi definition directly when no extension is "
            "intended. Vendors then know whether to treat the element as "
            "canonical or as a state-specific variant. This lifts "
            "definition_quality to tier 3."
        ),
        rationale=(
            "Substantive-but-not-extended definitions are a signal the state "
            "either has nothing distinctive to add (OK) or hasn't documented "
            "what is distinctive (not OK) — the recommendation forces the "
            "decision."
        ),
    ),
    ("semantic_fidelity", "tier_0_unresolved"): Template(
        dimension="semantic_fidelity",
        rule_slug="tier_0_unresolved",
        target_tier=3,
        evidence_fact="semantic_class",
        message=(
            "The recommendation is to add language to the definition of "
            "`{entity}.{element_name}` stating whether the state uses this "
            "element in alignment with Ed-Fi, narrows its scope, or broadens "
            "it. Vendors then implement against a known semantic relationship "
            "rather than treating the canonical/state mapping as ambiguous. "
            "This lifts semantic_fidelity to tier 3."
        ),
        rationale=(
            "Unresolved semantic class is usually a documentation gap rather "
            "than a genuine ambiguity — a single sentence of clarification "
            "typically moves the row to tier 2 or tier 3."
        ),
    ),
    ("semantic_fidelity", "tier_1_divergent_unclear"): Template(
        dimension="semantic_fidelity",
        rule_slug="tier_1_divergent_unclear",
        target_tier=3,
        evidence_fact="semantic_class",
        message=(
            "The recommendation is for the state authoring team to either "
            "conform `{entity}.{element_name}` back to the Ed-Fi canonical "
            "definition or document the divergence direction explicitly — "
            "whether the state narrows Ed-Fi's scope (subset of populations, "
            "stricter validation) or broadens it (additional populations, "
            "looser validation). Vendors then validate against a known "
            "semantic intent rather than implementing an Ed-Fi-shaped "
            "element with hidden state-specific behavior. This lifts "
            "semantic_fidelity to tier 3."
        ),
        rationale=(
            "Unexplained divergence is the highest-risk pattern for vendor "
            "misimplementation — the element looks like Ed-Fi but behaves "
            "differently."
        ),
    ),
    ("semantic_fidelity", "tier_2_divergent_explained"): Template(
        dimension="semantic_fidelity",
        rule_slug="tier_2_divergent_explained",
        target_tier=3,
        evidence_fact="semantic_class",
        message=(
            "The recommendation is to confirm whether the documented "
            "divergence on `{entity}.{element_name}` is load-bearing for "
            "state reporting; if it is incidental rather than load-bearing, "
            "realign the definition to the Ed-Fi canonical so the alignment "
            "is direct. Vendors then implement once against the canonical "
            "instead of carrying per-state semantic guidance. This lifts "
            "semantic_fidelity to tier 3."
        ),
        rationale=(
            "Documented divergence is acceptable; the recommendation asks "
            "whether the divergence is load-bearing or just unexamined."
        ),
    ),
    ("extension_justification", "tier_0_unnecessary_mirror"): Template(
        dimension="extension_justification",
        rule_slug="tier_0_unnecessary_mirror",
        target_tier=3,
        evidence_fact="extension_mirrors_core_pattern",
        message=(
            "This element mirrors a core Ed-Fi field. The recommendation is "
            "to retire the extension `{entity}.{element_name}` and use the "
            "core Ed-Fi resource directly; if the extension is genuinely "
            "needed, document the distinguishing business rule — population, "
            "validation, or reporting cadence — that core Ed-Fi cannot "
            "express. Vendors then implement one canonical surface rather "
            "than carrying both core and a redundant state extension. This "
            "lifts extension_justification to tier 3."
        ),
        rationale=(
            "Unjustified extensions are pure cost: they expand the surface "
            "SIS vendors must implement without carrying state-specific "
            "value that justifies the expansion."
        ),
    ),
    ("extension_justification", "tier_1_partial"): Template(
        dimension="extension_justification",
        rule_slug="tier_1_partial",
        target_tier=3,
        evidence_fact="extension_is_necessary",
        message=(
            "The recommendation is for the state authoring team to "
            "strengthen the justification for extension "
            "`{entity}.{element_name}` by stating the specific state "
            "reporting requirement it satisfies and confirming the extension "
            "is self-contained rather than co-dependent on other extensions. "
            "Vendors then implement the extension once against a clear "
            "rationale rather than re-deriving necessity from context. This "
            "lifts extension_justification to tier 3."
        ),
        rationale=(
            "Partial justification often reflects incomplete documentation "
            "rather than a design problem — a clearer rationale typically "
            "moves the row to tier 2 or tier 3."
        ),
    ),
    ("extension_justification", "tier_2_necessary_companion"): Template(
        dimension="extension_justification",
        rule_slug="tier_2_necessary_companion",
        target_tier=3,
        evidence_fact="extension_is_standalone",
        message=(
            "The recommendation is to either consolidate the companion "
            "extensions that `{entity}.{element_name}` depends on into a "
            "single self-contained design, or document the companion "
            "dependency explicitly so vendors implement the cluster as a "
            "unit. Vendors then implement either one cohesive extension or "
            "a clearly-labeled set rather than discovering the dependency at "
            "integration time. This lifts extension_justification to tier 3."
        ),
        rationale=(
            "Companion-dependent extensions are implementable but increase "
            "vendor integration burden; consolidation or explicit dependency "
            "documentation reduces that cost."
        ),
    ),
    # --------------------------- spine-lens ---------------------------
    ("documentation_completeness", "tier_0_missing"): Template(
        dimension="documentation_completeness",
        rule_slug="tier_0_missing",
        target_tier=3,
        evidence_fact="definition_present",
        message=(
            "The recommendation is for the state authoring team to publish a "
            "definition for the canonical Ed-Fi slot "
            "`{entity}.{element_name}` within the state source specification "
            "— covering the business concept, data type, and any "
            "state-specific business rules. Vendors then have an end-to-end "
            "documentation surface against the spine rather than discovering "
            "the field only via the swagger. This lifts "
            "documentation_completeness to tier 3."
        ),
        rationale=(
            "Missing definitions are the baseline documentation gap; "
            "everything else in this cascade assumes a definition exists."
        ),
    ),
    ("documentation_completeness", "tier_1_minimal"): Template(
        dimension="documentation_completeness",
        rule_slug="tier_1_minimal",
        target_tier=3,
        evidence_fact="business_rules_present",
        message=(
            "The recommendation is to author business-rules text for "
            "`{entity}.{element_name}` in the state source specification — "
            "when it is required, how it is validated, and any cross-entity "
            "dependencies — or to confirm none apply. Vendors then have "
            "implementation guidance rather than just a definition that says "
            "what the field is. This lifts documentation_completeness to "
            "tier 3."
        ),
        rationale=(
            "Minimal-documentation rows tell vendors what the element *is* "
            "but not what to *do* with it; business rules close that gap."
        ),
    ),
    # ("documentation_completeness", "tier_2_structure") intentionally absent:
    # the row already has business-rules structure; the recommendation
    # would be aspirational "sharpen tier-2 prose into tier-3 implementable
    # language" without a concrete target. Hard for a state author to act
    # on, and dilutes attention from the tier-0 / tier-1 documentation gaps
    # that have a clearer ask. The dimension still scores tier 2 on these
    # rows in the Scores sheet.
    ("obligation_clarity", "tier_0_none"): Template(
        dimension="obligation_clarity",
        rule_slug="tier_0_none",
        target_tier=3,
        evidence_fact="required_when_stated",
        message=(
            "The recommendation is for the state authoring team to add an "
            "obligation clause to `{entity}.{element_name}` — a required-when "
            "condition, a conditional-reporting rule, or a populations/scope "
            "statement — so vendors can determine when to populate the field. "
            "Vendors then know when reporting is required rather than "
            "treating the field as universally optional. This lifts "
            "obligation_clarity to tier 3."
        ),
        rationale=(
            "Without any obligation signal vendors default to treating the "
            "element as optional, which creates silent reporting gaps."
        ),
    ),
    ("obligation_clarity", "tier_0_descriptor_enum"): Template(
        dimension="obligation_clarity",
        rule_slug="tier_0_descriptor_enum",
        target_tier=3,
        evidence_fact="required_when_stated",
        message=(
            "The recommendation is for the state authoring team to add an "
            "obligation clause to `{entity}.{element_name}` alongside the "
            "descriptor enumeration — a required-when condition, a "
            "conditional-reporting rule, or a populations/scope statement. "
            "Vendors then know not just *what* may be reported but *when* "
            "the field must be populated. This lifts obligation_clarity to "
            "tier 3."
        ),
        rationale=(
            "Descriptor enumeration is often mistaken for an obligation "
            "signal; the rule-cascade explicitly separates them."
        ),
    ),
    ("obligation_clarity", "tier_1_partial"): Template(
        dimension="obligation_clarity",
        rule_slug="tier_1_partial",
        target_tier=3,
        evidence_fact="required_when_stated",
        message=(
            "The recommendation is to complete the obligation pair on "
            "`{entity}.{element_name}` — state both the required-when "
            "condition and the population/scope it applies to, or confirm "
            "one does not apply. Vendors then implement the validation "
            "cleanly rather than guessing at the missing half. This lifts "
            "obligation_clarity to tier 3."
        ),
        rationale=(
            "Partial obligation statements leave vendors to guess at the "
            "other half; closing the pair is typically one sentence."
        ),
    ),
    ("obligation_clarity", "tier_2_conditional"): Template(
        dimension="obligation_clarity",
        rule_slug="tier_2_conditional",
        target_tier=3,
        evidence_fact="populations_or_scope_stated",
        message=(
            "The recommendation is to add a populations/scope statement "
            "alongside the existing conditional-reporting rule on "
            "`{entity}.{element_name}` so vendors know both *when* the rule "
            "fires and *for whom* it applies. Vendors then implement the "
            "validation against the full obligation contract. This lifts "
            "obligation_clarity to tier 3."
        ),
        rationale=(
            "Conditional-reporting rules describe trigger conditions; "
            "population scope answers the complementary question vendors "
            "need to implement validation."
        ),
    ),
    # --------------------------- NACHOS (inverse axis) ---------------------------
    # ``nachos_score`` is a cost axis — higher = more implementation burden.
    # Target tier is 0. Recommendations push the work upstream into the
    # entity-appropriate workflow so SIS vendors record a resolved value
    # rather than running the costly logic on reporting-API writes.
    ("nachos_score", "tier_1_conditional"): Template(
        dimension="nachos_score",
        rule_slug="tier_1_conditional",
        target_tier=0,
        evidence_fact="has_conditional_logic",
        message=(
            "The recommendation is to move the conditional rule that gates "
            "`{entity}.{element_name}` upstream into the {workflow} (or out "
            "to entity-level business rules), so the source system resolves "
            "the condition once before reporting. Vendors then record a "
            "resolved value without applying per-element conditional logic "
            "on every API write. This reduces the Score 1 conditional cost "
            "to a direct pass-through read."
        ),
        rationale=(
            "Element-level conditionals that actually describe entity-level "
            "behavior are the most common form of NACHOS cost inflation; "
            "re-scoping the rule reduces per-element complexity without "
            "losing the logic."
        ),
    ),
    ("nachos_score", "tier_2_multi_entity"): Template(
        dimension="nachos_score",
        rule_slug="tier_2_multi_entity",
        target_tier=0,
        evidence_fact="has_cross_entity_logic",
        message=(
            "The recommendation is to compute the multi-entity calculation "
            "for `{entity}.{element_name}` upstream within the {workflow}, "
            "where the participating entities are already resolved together; "
            "vendors then record the resolved value without joining across "
            "entities at report time. This reduces the Score 2 multi-entity "
            "calculation to a direct pass-through read."
        ),
        rationale=(
            "Multi-entity calculations are expensive to implement correctly; "
            "pre-computation at the source eliminates the join burden the "
            "cost tier was scoring."
        ),
    ),
    ("nachos_score", "tier_3_aggregation"): Template(
        dimension="nachos_score",
        rule_slug="tier_3_aggregation",
        target_tier=0,
        evidence_fact="has_aggregation",
        message=(
            "The recommendation is to compute the aggregation for "
            "`{entity}.{element_name}` upstream within the {workflow}, where "
            "the granular events are already aligned, and report the final "
            "value to the API. Vendors then record a resolved aggregate "
            "rather than running SUM/COUNT/AVG aggregation on reporting-API "
            "writes. This reduces the Score 3 aggregation complexity to a "
            "straightforward pass-through."
        ),
        rationale=(
            "Aggregation elements are the most expensive to implement "
            "correctly and the easiest to diverge on across vendors; "
            "pre-aggregation upstream eliminates both costs."
        ),
    ),
    ("nachos_score", "tier_3_concatenation"): Template(
        dimension="nachos_score",
        rule_slug="tier_3_concatenation",
        target_tier=0,
        evidence_fact="has_concatenation",
        message=(
            "The recommendation is to report the component fields of "
            "`{entity}.{element_name}` separately and let downstream "
            "consumers assemble the composite where one is needed; if the "
            "concatenated form is regulatory, document the exact format and "
            "delimiter rules so vendors produce identical output. Vendors "
            "then implement either a pass-through (separate fields) or a "
            "deterministic format application rather than open-ended string "
            "assembly. This reduces the Score 3 concatenation cost to a "
            "pass-through or deterministic format application."
        ),
        rationale=(
            "Concatenation rarely adds information; separating the "
            "components preserves the data and eliminates a format-drift "
            "risk."
        ),
    ),
    # --------------------------- business_logic_complexity (spine, inverse axis) ---------------------------
    # Spine-lens cost axis paired to ``nachos_score`` — same posture
    # (target_tier=0, push the work upstream) but scored over the
    # canonical-Ed-Fi field rather than the state extension.
    ("business_logic_complexity", "tier_1_cond_or_cross"): Template(
        dimension="business_logic_complexity",
        rule_slug="tier_1_cond_or_cross",
        target_tier=0,
        evidence_fact="has_conditional_logic",
        message=(
            "The recommendation is to push the conditional or single-target "
            "cross-entity logic that backs `{entity}.{element_name}` upstream "
            "into the {workflow}, where the participating data already lives "
            "together; vendors then record a resolved value without "
            "re-evaluating the condition or join at report time. This "
            "reduces business_logic_complexity from Score 1 to a "
            "pass-through read."
        ),
        rationale=(
            "Tier-1 cross-entity / conditional logic on a canonical slot "
            "usually reflects state-side composition that the source system "
            "already does; surfacing the resolved value eliminates the "
            "per-vendor reimplementation."
        ),
    ),
    ("business_logic_complexity", "tier_2_agg_or_multicross"): Template(
        dimension="business_logic_complexity",
        rule_slug="tier_2_agg_or_multicross",
        target_tier=0,
        evidence_fact="has_aggregation",
        message=(
            "The recommendation is to compute the aggregation or "
            "multi-target cross-entity result for `{entity}.{element_name}` "
            "upstream within the {workflow}, where the source events and "
            "target entities are already aligned, and report the resolved "
            "value. Vendors then record a single value instead of running "
            "aggregation or multi-entity joins on reporting-API writes. "
            "This reduces business_logic_complexity from Score 2 to a "
            "pass-through read."
        ),
        rationale=(
            "Tier-2 aggregation or multi-target cross-entity work on a "
            "canonical slot is the same Score-3-shaped cost as nachos_score, "
            "scored against the spine; the same upstream-aggregation move "
            "fixes both."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Sourcing-mechanism templates (issue #59)
# ---------------------------------------------------------------------------
#
# Issue #59 path #1: typed refinement of `semantic_fidelity =
# divergent_*` — the LLM fact `sourcing_constraint_documented` names
# the mechanism (transformation / field_filter / external_sourcing /
# custom_enumeration) behind a documented divergence so reviewers can
# route the row to the right transformation work instead of a generic
# "review the divergence" recommendation.
#
# Keyed by enum value rather than (dimension, rule_slug). Path #1
# leaves `adjusted_nachos_score` untouched — these are surface-only
# recommendations. They appear ALONGSIDE the regular per-dimension
# recommendation when a divergent SF row carries a non-`none` /
# non-`unspecified` mechanism. The `none` / `unspecified` enum values
# do not emit a recommendation (no actionable refinement to add).
#
# `dimension` is the synthetic label "sourcing_constraint" — chosen so
# the by-dimension counts in the digest cleanly separate these
# observability-only recommendations from the rule-cascade ones. The
# template_id is `sourcing_constraint.{mechanism}`.
SOURCING_MECHANISM_TEMPLATES: dict[str, Template] = {
    "transformation": Template(
        dimension="sourcing_constraint",
        rule_slug="transformation",
        target_tier=0,
        evidence_fact="sourcing_constraint_documented",
        message=(
            "The recommendation is to scope a value-transformation step "
            "for `{entity}.{element_name}` in the vendor's reporting "
            "pipeline — the state has documented a coercion / mapping "
            "recipe that converts the source value into the reportable "
            "shape. Vendor implementation is a deterministic transform "
            "(span-to-year, scaled-integer, code-table remap) on every "
            "write rather than a conditional rule; the divergence "
            "becomes a one-time integration cost rather than a "
            "per-row review."
        ),
        rationale=(
            "Transformation rules are the highest-information divergence "
            "mechanism — the recipe is quotable from the state text and "
            "directly drives a vendor's transform-function design."
        ),
    ),
    "field_filter": Template(
        dimension="sourcing_constraint",
        rule_slug="field_filter",
        target_tier=0,
        evidence_fact="sourcing_constraint_documented",
        message=(
            "The recommendation is to scope a cross-field filter rule "
            "for `{entity}.{element_name}` in the vendor's reporting "
            "pipeline — the state has documented that this field's "
            "value depends on the value of a sibling field on the same "
            "row. Vendor implementation is a row-level conditional "
            "before the API write, gated on the named sibling field; "
            "the divergence becomes a single validation rule rather "
            "than a per-row reviewer call."
        ),
        rationale=(
            "Cross-field constraints are invisible to the rubric (the "
            "constraint is data-shape, not rule text inside the "
            "element); naming the gating sibling field lets vendors "
            "implement the filter directly."
        ),
    ),
    "external_sourcing": Template(
        dimension="sourcing_constraint",
        rule_slug="external_sourcing",
        target_tier=0,
        evidence_fact="sourcing_constraint_documented",
        message=(
            "The recommendation is to scope an external-source "
            "integration for `{entity}.{element_name}` — the state has "
            "documented that the value is sourced from a separate "
            "authoritative system the rest of the entity does not come "
            "from. Vendor implementation is a routing step that pulls "
            "the value from the named external source (state survey, "
            "assessment vendor, upstream registry) before the API "
            "write, rather than expecting the SIS to supply it."
        ),
        rationale=(
            "Off-row sourcing is the most expensive divergence to miss "
            "— vendors that wire only to the SIS row will silently drop "
            "the value. Naming the external authority routes the work "
            "to the right integration team."
        ),
    ),
    "custom_enumeration": Template(
        dimension="sourcing_constraint",
        rule_slug="custom_enumeration",
        target_tier=0,
        evidence_fact="sourcing_constraint_documented",
        message=(
            "The recommendation is to implement validation for "
            "`{entity}.{element_name}` against the state-specific "
            "enumerated value set rather than the canonical Ed-Fi "
            "descriptor — the state has documented its own list of "
            "allowed values distinct from the Ed-Fi descriptor. Vendor "
            "implementation is a state-scoped lookup table maintained "
            "alongside the SIS code mappings; the divergence becomes "
            "a list-maintenance task rather than a per-row review."
        ),
        rationale=(
            "Custom enumerations are easy to confuse with Ed-Fi "
            "descriptor narrowings — naming the value set as "
            "state-specific lets vendors maintain a separate lookup "
            "rather than assuming the canonical descriptor applies."
        ),
    ),
}


# Sourcing-mechanism recommendations only emit on rows whose
# `semantic_fidelity` dimension is in the divergent cluster — the
# mechanism is a typed refinement of divergent_unclear / divergent_
# explained, not a free-standing classifier. On a row where SF is
# aligned (tier 3) or not_applicable (None), the mechanism fact may
# still resolve to a non-`none` value, but surfacing a recommendation
# there would clutter the analyst sheet with rows the methodology
# doesn't flag as divergent. Restrict to the SF rule slugs that fire
# on documented divergence:
_SOURCING_MECHANISM_SF_SLUGS: frozenset[str] = frozenset(
    {"tier_1_divergent_unclear", "tier_2_divergent_explained"}
)


# ---------------------------------------------------------------------------
# Gap-lens templates (issue #73) — spine-anchored rows the source doc is
# silent on. Documentation-derived dimensions correctly land at tier 0 across
# the gap corpus by construction; running the per-dimension rule cascade
# would emit ~5 "improve documentation" recs per row × 9.5K rows = ~50K
# rec-spam. Track A bypasses the cascade and emits ONE record-level rec per
# gap row plus an optional structural-depth callout when the row sits in a
# deep FK chain (``structural_depth >= 2``).
# ---------------------------------------------------------------------------


GAP_BASE_TEMPLATE: Template = Template(
    dimension="documentation_gap",
    rule_slug="spine_anchored",
    target_tier=0,  # informational; gap recs aren't a tier lift.
    evidence_fact=None,
    message=(
        "`{entity}.{element_name}` exists in the Ed-Fi Swagger/API model but {state}'s "
        "source documentation doesn't reference it. Action: add a row to "
        "the source doc covering this property; supply a definition (the "
        "public Ed-Fi description is acceptable verbatim if the state has "
        "no specific guidance); flag whether the property is in active use "
        "in the state's reporting flow. Documenting the element pulls it "
        "out of the spine-anchored gap surface and lets downstream "
        "vendor-integration views reason about it as authored state "
        "surface rather than a silent spine artifact."
    ),
    rationale=(
        "Source-undocumented spine entries land at NACHOS tier 0 across "
        "every documentation-derived dimension. The deterministic cure is "
        "documentation, not a rule edit — the structural facts are already "
        "there in the spine; only the state's narrative is missing."
    ),
)


GAP_STRUCTURAL_CALLOUT_TEMPLATE: Template = Template(
    dimension="documentation_gap",
    rule_slug="deep_fk_chain",
    target_tier=0,  # informational.
    evidence_fact=None,
    message=(
        "`{entity}.{element_name}` sits at implementation shape {depth} — "
        "this property is part of a deep FK chain (each hop is a foreign-"
        "key reference into another spine entity). Documenting it pays off "
        "structurally too: vendors implementing the FK chain need the "
        "property's intent to wire the navigation correctly, and analysts "
        "reading the workbook can't derive it from the chain shape alone."
    ),
    rationale=(
        "Deep FK chains compound the cost of an undocumented hop — each "
        "downstream consumer has to either re-derive the intent or skip the "
        "field. Implementation shape on a gap row is the single most actionable "
        "follow-on signal the gap surfacer carries."
    ),
)


# Threshold at which the structural-depth callout fires alongside the base
# gap rec. Depth 0–1 rows skip the callout; depth >= 2 rows surface it.
_GAP_STRUCTURAL_CALLOUT_MIN_DEPTH: int = 2


def _state_label(record_key: str | None) -> str:
    """Pull the state code (``AZ`` / ``WI`` / ``MN`` / ``TX``) off a record_key.

    Falls back to ``the state`` when the key is absent or malformed so the
    rendered message stays grammatical instead of leaking ``{state}``.
    """
    if not record_key:
        return "the state"
    head = record_key.split("|", 1)[0].strip().upper()
    if head in SUPPORTED_STATES:
        return head
    return "the state"


def generate_for_gap_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Produce the record-level gap recommendation object for one gap-sidecar row.

    Returns a dict in the same shape as ``generate_for_row`` so the
    aggregator (``generate_state``) and the markdown renderer can consume
    both flavours uniformly. Emits 1 rec (the base "document this element"
    template) plus an optional second rec when the row's
    ``structural_depth`` value is ``>= _GAP_STRUCTURAL_CALLOUT_MIN_DEPTH``.

    Gap rows score every documentation-derived dimension at tier 0 by
    construction, so the per-dimension rule cascade is deliberately
    bypassed for this lens — running it would emit ~5 boilerplate
    "improve documentation" recs per row × ~9.5K rows.
    """
    dimensions = row.get("dimensions", {}) or {}
    current_scores: dict[str, dict[str, Any]] = {
        dim_name: {
            "tier": dim_score.get("value"),
            "rule": dim_score.get("rule_matched"),
        }
        for dim_name, dim_score in dimensions.items()
    }

    state = _state_label(row.get("record_key"))
    nachos_dim = dimensions.get("nachos_score") or {}
    dim_confidence = nachos_dim.get("confidence", "high")

    base_message = GAP_BASE_TEMPLATE.message.format(
        entity=row.get("entity") or "",
        element_name=row.get("element_name") or "",
        state=state,
        workflow=workflow_for(row.get("entity")),
    )
    recommendations: list[dict[str, Any]] = [
        {
            "dimension": GAP_BASE_TEMPLATE.dimension,
            "current_tier": None,
            "target_tier": GAP_BASE_TEMPLATE.target_tier,
            "impact": 0,
            "current_rule": GAP_BASE_TEMPLATE.rule_slug,
            "template_id": (
                f"{GAP_BASE_TEMPLATE.dimension}.{GAP_BASE_TEMPLATE.rule_slug}"
            ),
            "message": base_message,
            "rationale": GAP_BASE_TEMPLATE.rationale,
            "evidence": None,
            "dimension_confidence": dim_confidence,
            "confidence": dim_confidence,
            "automation": "deterministic",
        }
    ]

    structural_depth_dim = dimensions.get("structural_depth") or {}
    depth_value = structural_depth_dim.get("value")
    if (
        isinstance(depth_value, int)
        and depth_value >= _GAP_STRUCTURAL_CALLOUT_MIN_DEPTH
    ):
        depth_message = GAP_STRUCTURAL_CALLOUT_TEMPLATE.message.format(
            entity=row.get("entity") or "",
            element_name=row.get("element_name") or "",
            depth=depth_value,
            state=state,
            workflow=workflow_for(row.get("entity")),
        )
        depth_confidence = structural_depth_dim.get("confidence", "high")
        recommendations.append(
            {
                "dimension": GAP_STRUCTURAL_CALLOUT_TEMPLATE.dimension,
                "current_tier": depth_value,
                "target_tier": GAP_STRUCTURAL_CALLOUT_TEMPLATE.target_tier,
                "impact": 0,
                "current_rule": GAP_STRUCTURAL_CALLOUT_TEMPLATE.rule_slug,
                "template_id": (
                    f"{GAP_STRUCTURAL_CALLOUT_TEMPLATE.dimension}."
                    f"{GAP_STRUCTURAL_CALLOUT_TEMPLATE.rule_slug}"
                ),
                "message": depth_message,
                "rationale": GAP_STRUCTURAL_CALLOUT_TEMPLATE.rationale,
                "evidence": None,
                "dimension_confidence": depth_confidence,
                "confidence": depth_confidence,
                "automation": "deterministic",
            }
        )

    review = row.get("review", {}) or {}
    return {
        "record_key": row.get("record_key"),
        "entity": row.get("entity"),
        "element_name": row.get("element_name"),
        "in_scope": row.get("in_scope"),
        "adjusted_nachos_score": row.get("adjusted_nachos_score"),
        "review_status": "pending" if review.get("needs_review") else "clean",
        "current_scores": current_scores,
        "recommendations": recommendations,
        # Carry the discovery_lens marker through so consumers (workbook
        # writers, the digest renderer) can distinguish gap-lens records
        # from source/spine-lens ones without re-reading the source sidecar.
        "discovery_lens": row.get("discovery_lens"),
    }


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _confidence_min(levels: list[str]) -> str:
    order = {"low": 0, "medium": 1, "high": 2}
    reverse = {v: k for k, v in order.items()}
    if not levels:
        return "low"
    return reverse[min(order.get(l, 0) for l in levels)]


def _evidence_block(
    row: Mapping[str, Any], template: Template
) -> dict[str, Any] | None:
    """Return ``{value, confidence, downgraded, downgrade_reason}`` for the
    template's evidence fact, or ``None`` if the template is structural.

    The evidence block is deliberately lean — it carries the minimum the
    analyst needs to verify the recommendation without having to re-open
    the sidecar. Quoted spans live in the extraction artifacts, not here.
    """
    if template.evidence_fact is None:
        return None
    provenance = row.get("fact_provenance", {}) or {}
    record = provenance.get(template.evidence_fact)
    if record is None:
        return None
    return {
        "fact": template.evidence_fact,
        "value": record.get("value"),
        "confidence": record.get("confidence", "low"),
        "downgraded": bool(record.get("downgraded", False)),
        "downgrade_reason": record.get("downgrade_reason"),
    }


def _needs_recommendation(dimension: str, value: int | None) -> bool:
    """Skip dimensions that are at-target or not-applicable.

    ``value is None`` → dimension is not applicable (e.g. extension_justification
    on core rows, semantic_fidelity=not_applicable). Skip.

    For inverse (cost) dimensions, the dimension needs a recommendation
    when ``value > 0``; for quality dimensions, when ``value < 3``.
    """
    if value is None:
        return False
    if dimension in _INVERSE_DIMENSIONS:
        return value > 0
    return value < 3


def generate_for_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Produce the recommendation object for one scored sidecar row.

    Returns a dict in the shape documented in
    ``docs/recommendations-generator.md §3.1``. Rows with zero
    applicable recommendations still appear in the output (with an
    empty ``recommendations`` list) so the full state is traceable.
    """
    dimensions = row.get("dimensions", {}) or {}
    current_scores: dict[str, dict[str, Any]] = {}
    recommendations: list[dict[str, Any]] = []

    for dim_name, dim_score in dimensions.items():
        value = dim_score.get("value")
        rule_slug = dim_score.get("rule_matched")
        current_scores[dim_name] = {
            "tier": value,
            "rule": rule_slug,
        }
        if not _needs_recommendation(dim_name, value):
            continue
        template = RECOMMENDATIONS.get((dim_name, rule_slug))
        if template is None:
            continue
        rendered = template.message.format(
            entity=row.get("entity", ""),
            element_name=row.get("element_name", ""),
            workflow=workflow_for(row.get("entity")),
        )
        evidence = _evidence_block(row, template)
        dim_confidence = dim_score.get("confidence", "low")
        evidence_confidence = (
            evidence["confidence"] if evidence is not None else dim_confidence
        )
        combined_confidence = _confidence_min([dim_confidence, evidence_confidence])
        recommendations.append(
            {
                "dimension": dim_name,
                "current_tier": value,
                "target_tier": template.target_tier,
                # Signed tier delta. Positive on quality axes (lift
                # toward tier 3); negative on cost axes (simplify
                # toward tier 0). abs(impact) is the leverage —
                # Phase 3 uses it for sheet ordering + hero picks.
                "impact": template.target_tier - value,
                "current_rule": rule_slug,
                "template_id": f"{dim_name}.{rule_slug}",
                "message": rendered,
                "rationale": template.rationale,
                "evidence": evidence,
                "dimension_confidence": dim_confidence,
                "confidence": combined_confidence,
                "automation": "deterministic",
            }
        )

    # Issue #59 — sourcing-mechanism refinement. Emits alongside the
    # regular per-dimension recommendation when the row's
    # `semantic_fidelity` lands in the divergent cluster AND the
    # `sourcing_constraint_documented` provenance carries a
    # non-`none` / non-`unspecified` mechanism. Path #1 surfaces the
    # mechanism on Recommendations without changing
    # `adjusted_nachos_score`.
    sf_score = dimensions.get("semantic_fidelity") or {}
    sf_rule = sf_score.get("rule_matched")
    if sf_rule in _SOURCING_MECHANISM_SF_SLUGS:
        provenance = row.get("fact_provenance", {}) or {}
        scd = provenance.get("sourcing_constraint_documented") or {}
        mechanism = scd.get("value")
        # Skip mechanisms that have no actionable refinement (default
        # "no constraint" labels) and skip downgraded provenance — a
        # downgraded fact carries `value=None`, which never matches.
        if (
            mechanism in SOURCING_MECHANISM_TEMPLATES
            and not scd.get("downgraded", False)
        ):
            template = SOURCING_MECHANISM_TEMPLATES[mechanism]
            rendered = template.message.format(
                entity=row.get("entity", ""),
                element_name=row.get("element_name", ""),
                workflow=workflow_for(row.get("entity")),
            )
            evidence = _evidence_block(row, template)
            dim_confidence = sf_score.get("confidence", "low")
            evidence_confidence = (
                evidence["confidence"] if evidence is not None else dim_confidence
            )
            combined_confidence = _confidence_min(
                [dim_confidence, evidence_confidence]
            )
            recommendations.append(
                {
                    # Synthetic dimension label so the digest's
                    # by_dimension counts cleanly separate the
                    # observability-only refinement from the rule-cascade
                    # recommendations.
                    "dimension": "sourcing_constraint",
                    "current_tier": None,
                    "target_tier": template.target_tier,
                    "impact": 0,
                    "current_rule": mechanism,
                    "template_id": f"sourcing_constraint.{mechanism}",
                    "message": rendered,
                    "rationale": template.rationale,
                    "evidence": evidence,
                    "dimension_confidence": dim_confidence,
                    "confidence": combined_confidence,
                    "automation": "deterministic",
                }
            )

    review = row.get("review", {}) or {}
    return {
        "record_key": row.get("record_key"),
        "entity": row.get("entity"),
        "element_name": row.get("element_name"),
        "in_scope": row.get("in_scope"),
        "adjusted_nachos_score": row.get("adjusted_nachos_score"),
        "review_status": "pending" if review.get("needs_review") else "clean",
        "current_scores": current_scores,
        "recommendations": recommendations,
    }


def _hero_score(record: Mapping[str, Any]) -> tuple:
    """Sort key for hero-example selection.

    Phase 3 promotes total abs-impact (leverage) to the primary sort
    key so the highest-leverage row wins — matches how a human analyst
    scans a scorecard. Ties break on the prior composite (coverage
    breadth, rec count, evidence confidence, record_key for stability).
    """
    recs = record.get("recommendations", []) or []
    n = len(recs)
    impact_sum = sum(
        abs(r.get("impact") or 0) for r in recs
    )
    has_template_for_every_dim = sum(
        1 for dim in (record.get("current_scores") or {}).values()
        if dim.get("tier") is not None
    )
    high_conf = sum(1 for r in recs if r.get("confidence") == "high")
    return (
        -impact_sum,
        -has_template_for_every_dim,
        -n,
        -high_conf,
        record.get("record_key") or "",
    )


def _pick_hero_examples(
    records: list[dict[str, Any]],
    hero_n: int,
    *,
    pinned_keys: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Select hero examples covering the widest template variety available.

    The caller-visible intent is "show the reader a few representative
    rows that exercise distinct recommendation patterns." The selection
    walks candidates sorted by ``_hero_score`` and greedily picks rows
    whose dominant template ID has not already been covered. Falls back
    to the top-ranked rows if template diversity can't be achieved at
    the requested count.

    ``pinned_keys`` force-includes specific record_keys at the front of
    the hero list (see ``_PINNED_HERO_KEYS``). Pinned records consume
    hero-list slots before the natural ranking runs, so a pin can push
    a higher-ranked row out when ``hero_n`` is small.
    """
    if hero_n <= 0 or not records:
        return []
    candidates = [r for r in records if r.get("recommendations")]
    if not candidates:
        return []
    candidates.sort(key=_hero_score)
    picked: list[dict[str, Any]] = []
    seen_templates: set[str] = set()
    by_key = {r.get("record_key"): r for r in candidates}
    for key in pinned_keys:
        rec = by_key.get(key)
        if rec is None or rec in picked:
            continue
        picked.append(rec)
        seen_templates.update({r["template_id"] for r in rec["recommendations"]})
        if len(picked) >= hero_n:
            break
    for rec in candidates:
        if len(picked) >= hero_n:
            break
        if rec in picked:
            continue
        tids = {r["template_id"] for r in rec["recommendations"]}
        if picked and tids.issubset(seen_templates):
            continue
        picked.append(rec)
        seen_templates.update(tids)
    # Fill with top-ranked if we ran out of diversity.
    for rec in candidates:
        if len(picked) >= hero_n:
            break
        if rec not in picked:
            picked.append(rec)
    return picked


def generate_state(
    state: str,
    lens: Lens = "source",
    *,
    base: Path | None = None,
    hero_n: int = 4,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Build the full recommendations payload for (state, lens).

    Reads ``data/out/{state}_scores_{lens}.json`` and emits per-record
    recommendation objects plus roll-up counts and hero examples. The
    structure mirrors ``review_queue.build_queue`` so consumers can load
    either artifact with the same shape of code.

    ``lens="gap"`` (issue #73) reads from ``state_scores_gap.json`` and
    routes per-row generation through ``generate_for_gap_row`` instead of
    the per-dimension rule cascade.
    """
    if lens == "gap":
        path = (
            (base / f"{state.lower()}_scores_gap.json")
            if base is not None
            else state_scores_gap_path(state)
        )
    else:
        path = (
            (base / f"{state.lower()}_scores_{lens}.json")
            if base is not None
            else state_scores_path(state, lens)  # type: ignore[arg-type]
        )
    # Issue #212 item 3: with a publish lineage, a stale sidecar must
    # not silently feed recommendations; a corrupt one always raises.
    from src.publish.manifest import verify_fresh
    from src.utils.artifacts import read_json_artifact

    verify_fresh(
        path, consumer="report recommendations", allow_stale=allow_stale
    )
    sidecar = read_json_artifact(path, consumer="report recommendations")
    scores = sidecar.get("scores", []) or []

    if lens == "gap":
        records: list[dict[str, Any]] = [generate_for_gap_row(row) for row in scores]
    else:
        records = [generate_for_row(row) for row in scores]

    template_counts: Counter = Counter()
    per_dimension: Counter = Counter()
    per_entity: Counter = Counter()
    rows_with_recs = 0
    rows_at_target = 0
    total_recs = 0
    for rec in records:
        if rec["recommendations"]:
            rows_with_recs += 1
            per_entity[rec.get("entity") or "(unknown)"] += len(rec["recommendations"])
        else:
            rows_at_target += 1
        for r in rec["recommendations"]:
            template_counts[r["template_id"]] += 1
            per_dimension[r["dimension"]] += 1
            total_recs += 1

    pinned = _PINNED_HERO_KEYS.get((state.upper(), lens), ())
    hero_examples = _pick_hero_examples(records, hero_n, pinned_keys=pinned)

    return {
        "generated_on": date.today().isoformat(),
        "state": state.upper(),
        "lens": lens,
        "source_sidecar": path.name,
        "prompt_version": sidecar.get("prompt_version"),
        "scoring_plan_version": sidecar.get("scoring_plan_version"),
        "model": sidecar.get("model"),
        "record_count": len(records),
        "rows_with_recommendations": rows_with_recs,
        "rows_at_target": rows_at_target,
        "recommendations_total": total_recs,
        "by_template": dict(template_counts.most_common()),
        "by_dimension": dict(per_dimension.most_common()),
        "by_entity_top": dict(per_entity.most_common(20)),
        "hero_examples": [r["record_key"] for r in hero_examples],
        "records": records,
        "guidance": (
            "Deterministic-template layer (Track C prototype). Each "
            "recommendation cites a single evidence fact from the scored "
            "sidecar; narrative polish is gated behind an optional LLM "
            "layer that is not enabled in this prototype. Rows flagged "
            "`review_status=pending` have at least one low-confidence "
            "dimension — the recommendation still applies but the "
            "underlying score should be validated first."
        ),
    }


# ---------------------------------------------------------------------------
# XLSX-friendly flattener (Track C scorecard integration)
# ---------------------------------------------------------------------------


# Sheet column order — pinned by the Phase 2 commit. v10 (2026-04-26) —
# `in_scope` column dropped per methodology scope rectification.
SHEET_ROW_KEYS: tuple[str, ...] = (
    "state",
    "entity",
    "element",
    "review_status",
    "dimension",
    "current_tier",
    "target_tier",
    "impact",
    "recommendation",
    "rationale",
    "evidence_fact",
    "fact_value",
    "confidence",
    "record_key",        # internal join key (Domain + Review Route lookups)
    "template_id",       # internal aux for telemetry / debug
)


def sheet_rows_for_workbook(
    state: str,
    lens: Lens = "source",
    *,
    base: Path | None = None,
    allow_stale: bool = False,
) -> list[dict[str, Any]]:
    """Flatten state-lens recommendations into per-(record × dimension) sheet rows.

    Returns one dict per below-target dimension across the state's
    records (no rows for at-target records). Shape mirrors the
    Recommendations sheet column contract documented in
    ``docs/track-c-scorecard-integration-plan.md`` §5.1, plus two
    internal aux keys (``record_key``, ``template_id``) the analyst
    writer uses for the Domain join and review-queue route lookup.

    The "Domain" and "Review Route" columns are filled by the analyst
    writer at render time, since they require the spine catalog and
    ``review_queue_{lens}.json`` respectively — those joins are not
    part of the recommendations module's responsibility surface.

    ``impact`` is the signed tier delta (target − current). Positive on
    the quality axis (higher = better), negative on the cost axis
    (target = 0, current > 0). Phase 3 will also expose this in the
    JSON / MD outputs and use it for sheet ordering.
    """
    result = generate_state(
        state, lens, base=base, allow_stale=allow_stale
    )
    rows: list[dict[str, Any]] = []
    for record in result["records"]:
        for rec in record["recommendations"]:
            evidence = rec.get("evidence") or {}
            rows.append(
                {
                    "state": result["state"],
                    "entity": record.get("entity") or "",
                    "element": record.get("element_name") or "",
                    "review_status": record.get("review_status") or "clean",
                    "dimension": rec["dimension"],
                    "current_tier": rec.get("current_tier"),
                    "target_tier": rec.get("target_tier"),
                    "impact": rec.get("impact"),
                    "recommendation": rec["message"],
                    "rationale": rec.get("rationale") or "",
                    "evidence_fact": evidence.get("fact"),
                    "fact_value": evidence.get("value"),
                    "confidence": rec.get("confidence") or "low",
                    "record_key": record.get("record_key"),
                    "template_id": rec.get("template_id"),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------


def _format_record_markdown(record: Mapping[str, Any], *, depth: int = 3) -> list[str]:
    heading = "#" * depth
    lines: list[str] = [
        f"{heading} `{record['entity']}.{record['element_name']}`",
        "",
    ]
    review = record.get("review_status") or "clean"
    lines.append(
        f"_Record key: `{record['record_key']}` · review: {review}_"
    )
    lines.append("")
    for rec in record.get("recommendations", []) or []:
        target = rec["target_tier"]
        current = rec["current_tier"]
        dim = rec["dimension"]
        # Issue #59 — sourcing_constraint is a typed-mechanism refinement,
        # not a tiered axis. Render the mechanism name instead of the
        # tier-shaped header so "tier None ↑ lift to tier 0" doesn't show.
        if dim == "sourcing_constraint":
            mechanism = rec.get("current_rule") or "(unknown)"
            lines.append(
                f"- **{dim}** (mechanism: {mechanism}, "
                f"confidence: {rec['confidence']})"
            )
        elif dim == "documentation_gap":
            # Issue #73 — gap recs are record-level (no tier lift). Render
            # the rule slug as a flavour tag and skip the "tier X → tier Y"
            # arrow that doesn't apply.
            slug = rec.get("current_rule") or "spine_anchored"
            lines.append(
                f"- **{dim}** (gap class: {slug}, "
                f"confidence: {rec['confidence']})"
            )
        else:
            direction = "↓ simplify to" if dim in _INVERSE_DIMENSIONS else "↑ lift to"
            impact = rec.get("impact")
            impact_str = f"impact {impact:+d}, " if isinstance(impact, int) else ""
            lines.append(
                f"- **{dim}** (tier {current} {direction} tier {target}, "
                f"{impact_str}confidence: {rec['confidence']})"
            )
        lines.append(f"  - {rec['message']}")
        if rec.get("rationale"):
            lines.append(f"  - _Rationale:_ {rec['rationale']}")
        if rec.get("evidence"):
            ev = rec["evidence"]
            ev_value = ev.get("value")
            lines.append(
                f"  - _Evidence:_ fact `{ev['fact']}` = `{ev_value}` "
                f"(confidence: {ev['confidence']})"
            )
    lines.append("")
    return lines


def render_markdown(result: Mapping[str, Any], *, hero_n: int = 4) -> str:
    """Render the markdown digest for a recommendations payload.

    Structure:

    1. Header (state, lens, sidecar source, counts).
    2. Template coverage table.
    3. Hero examples (top ``hero_n`` rows illustrating distinct patterns).
    4. Per-entity grouping of every row with at least one recommendation.
    """
    lines: list[str] = [
        f"# State-facing recommendations — {result['state']} ({result['lens']}-lens)",
        "",
        f"Generated on: {result['generated_on']}.",
        "",
        result["guidance"],
        "",
        "## Roll-up",
        "",
        "| Metric | Count |",
        "| :- | -: |",
        f"| Records scored | {result['record_count']:,} |",
        f"| Records with ≥1 recommendation | {result['rows_with_recommendations']:,} |",
        f"| Records already at target tier | {result['rows_at_target']:,} |",
        f"| Total recommendations | {result['recommendations_total']:,} |",
        "",
        "## Recommendations by dimension",
        "",
        "| Dimension | Count |",
        "| :- | -: |",
    ]
    for dim, count in result["by_dimension"].items():
        lines.append(f"| {dim} | {count:,} |")

    lines.extend([
        "",
        "## Top templates",
        "",
        "| Template ID | Count |",
        "| :- | -: |",
    ])
    for template_id, count in list(result["by_template"].items())[:12]:
        lines.append(f"| `{template_id}` | {count:,} |")

    records_by_key = {r["record_key"]: r for r in result["records"]}
    hero_keys = result.get("hero_examples", [])[:hero_n]
    if hero_keys:
        lines.extend([
            "",
            f"## Hero examples ({len(hero_keys)} rows)",
            "",
            "Picked to exercise distinct recommendation patterns; each row "
            "illustrates one or more templates the generator fires across "
            "the full state.",
            "",
        ])
        for key in hero_keys:
            record = records_by_key.get(key)
            if record is None:
                continue
            lines.extend(_format_record_markdown(record, depth=3))

    # Per-entity grouping — only rows with recommendations.
    by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in result["records"]:
        if record.get("recommendations"):
            by_entity[record.get("entity") or "(unknown)"].append(record)

    if by_entity:
        lines.extend([
            "## All recommendations by entity",
            "",
        ])
        for entity in sorted(by_entity):
            entity_records = by_entity[entity]
            total = sum(len(r["recommendations"]) for r in entity_records)
            lines.extend([
                f"### {entity} ({len(entity_records)} rows · {total} recommendations)",
                "",
            ])
            for record in entity_records:
                lines.extend(_format_record_markdown(record, depth=4))

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run(
    *,
    state: str = "AZ",
    lens: Lens = "source",
    out: Path | None = None,
    hero_n: int = 4,
    emit_md: bool = False,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Generate recommendations for ``state`` — JSON always, MD opt-in.

    ``out`` is the output directory (defaults to ``data/out``). When
    specified, artifacts land there and the same directory is used
    to locate the source sidecar (so tests can inject a scratch path).

    R4 artifact diet (Sequence-1, 2026-07): the MD digest (1–5 MB per
    state/lens — the "All recommendations by entity" section renders
    every row) has no downstream consumer in src/ and is now written
    only when ``emit_md`` is set. The JSON stays unconditional — the
    analyst workbook gates its Recommendations sheet on the JSON's
    existence (`analyst.run`).

    Returns the generated payload dict so callers can inspect counts
    without re-reading from disk.
    """
    base = out or out_dir()
    base.mkdir(parents=True, exist_ok=True)
    result = generate_state(
        state, lens, base=base, hero_n=hero_n, allow_stale=allow_stale
    )
    # Lens-aware filename so source- and spine-lens runs don't clobber each
    # other; mirrors every other per-state+lens report artifact in POC-3.
    json_path = base / f"{state.lower()}_recommendations_{lens}.json"
    json_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    md_note = ""
    if emit_md:
        md_path = base / f"{state.lower()}_recommendations_{lens}.md"
        md_path.write_text(render_markdown(result, hero_n=hero_n), encoding="utf-8")
        md_note = f" + {md_path}"
    _LOGGER.info(
        "recommendations (%s/%s): wrote %s%s, rows_with_recs=%d, total=%d",
        state, lens, json_path, md_note,
        result["rows_with_recommendations"], result["recommendations_total"],
    )
    return result
