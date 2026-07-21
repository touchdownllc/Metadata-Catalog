"""Issue #59 — `sourcing_constraint_documented` typed-mechanism refinement.

Path #1 landing: the new LLM fact lands as source-lens
observability (no rule-cascade contribution) and emits one
mechanism-specific Recommendations template per non-`none` enum value
when the row's `semantic_fidelity` lands in the divergent cluster.

Tests verified here:

- Schema registration: `ENUM_VALUED_FACTS` carries every label, both
  no-evidence defaults appear in `ENUM_NO_EVIDENCE_VALUES`, fact is in
  `SUPPORTED_FACTS`.
- Lens routing: `LENS_OBSERVABILITY_FACTS["source"]` carries the fact,
  spine roster does NOT (the prompt is source-lens-only).
- Prompt: prompt file exists, declares `Polarity: n/a (enum6 ...)`,
  passes the `_load_template` parse, lists every enum label in the
  system prompt.
- Recommendations: per-mechanism templates exist for the four
  actionable mechanisms; `none` and `unspecified` do NOT emit
  recommendations; the SF dimension cluster gate fires only on
  `tier_1_divergent_unclear` / `tier_2_divergent_explained`.
- Markdown: rendered digest avoids the "tier None ↑ lift to tier 0"
  shape and surfaces the mechanism name instead.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.report.recommendations import (
    SOURCING_MECHANISM_TEMPLATES,
    Template,
    generate_for_row,
    render_markdown,
)
from src.score.extract import SUPPORTED_FACTS, prompt_path_for, _load_template
from src.score.rules import LENS_OBSERVABILITY_FACTS
from src.score.runner import phase_b_facts_for_lens
from src.score.schema import (
    ENUM_NO_EVIDENCE_VALUES,
    ENUM_VALUED_FACTS,
    FACT_SOURCE_FILTERS,
)


_FACT = "sourcing_constraint_documented"
_ALL_LABELS = (
    "transformation",
    "field_filter",
    "external_sourcing",
    "custom_enumeration",
    "none",
    "unspecified",
)
_ACTIONABLE_LABELS = (
    "transformation",
    "field_filter",
    "external_sourcing",
    "custom_enumeration",
)


# ---------------------------------------------------------------------------
# Schema registration
# ---------------------------------------------------------------------------


class TestSchema:
    def test_fact_is_supported(self) -> None:
        assert _FACT in SUPPORTED_FACTS

    def test_enum_labels_match(self) -> None:
        assert _FACT in ENUM_VALUED_FACTS
        assert tuple(ENUM_VALUED_FACTS[_FACT]) == _ALL_LABELS

    def test_no_evidence_defaults(self) -> None:
        """`none` and `unspecified` are both no-evidence — empty spans."""
        assert ENUM_NO_EVIDENCE_VALUES.get(_FACT) == frozenset(
            {"none", "unspecified"}
        )

    def test_no_source_filter(self) -> None:
        """The fact extracts on every source-lens row regardless of source.

        Issue #59 §Acceptance criteria: "Extracted on all 4 states'
        source-lens rows".
        """
        assert _FACT not in FACT_SOURCE_FILTERS


# ---------------------------------------------------------------------------
# Lens routing
# ---------------------------------------------------------------------------


class TestLensRouting:
    def test_source_lens_observability(self) -> None:
        assert _FACT in LENS_OBSERVABILITY_FACTS["source"]

    def test_not_on_spine_lens(self) -> None:
        """Source-lens-only — the prompt grounds on the state's
        documented divergence; the spine lens has no parallel surface."""
        assert _FACT not in LENS_OBSERVABILITY_FACTS["spine"]

    def test_source_roster_includes_fact(self) -> None:
        roster = phase_b_facts_for_lens("source")
        assert _FACT in roster

    def test_spine_roster_excludes_fact(self) -> None:
        roster = phase_b_facts_for_lens("spine")
        assert _FACT not in roster


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


class TestPrompt:
    def test_prompt_file_exists(self) -> None:
        path = prompt_path_for(_FACT)
        assert path.exists()
        assert path.name == f"{_FACT}.md"

    def test_prompt_loads_cleanly(self) -> None:
        """`_load_template` accepts the prompt — split markers present,
        leading author-comment block strips."""
        path = prompt_path_for(_FACT)
        system, user = _load_template(path)
        assert system.strip()
        assert user.strip()
        # Polarity declaration is an author annotation; it must not leak
        # into the SYSTEM prompt.
        assert "Polarity:" not in system

    def test_polarity_declaration(self) -> None:
        """Prompt declares `n/a (enum...)` polarity — every enum fact
        must per `tests/test_score_prompt_polarity.py` convention."""
        path = prompt_path_for(_FACT)
        raw = path.read_text(encoding="utf-8")
        # The polarity line lives in the leading {# ... #} comment block.
        assert "Polarity: n/a" in raw
        assert "enum" in raw.lower()

    def test_system_prompt_lists_every_label(self) -> None:
        path = prompt_path_for(_FACT)
        system, _ = _load_template(path)
        for label in _ALL_LABELS:
            assert label in system, f"label {label!r} missing from system prompt"


# ---------------------------------------------------------------------------
# Recommendations templates
# ---------------------------------------------------------------------------


class TestSourcingMechanismTemplates:
    def test_template_per_actionable_mechanism(self) -> None:
        for label in _ACTIONABLE_LABELS:
            assert label in SOURCING_MECHANISM_TEMPLATES, (
                f"mechanism {label!r} missing from "
                f"SOURCING_MECHANISM_TEMPLATES"
            )

    def test_no_evidence_labels_have_no_template(self) -> None:
        """`none` / `unspecified` are no-evidence defaults — no
        recommendation surface."""
        assert "none" not in SOURCING_MECHANISM_TEMPLATES
        assert "unspecified" not in SOURCING_MECHANISM_TEMPLATES

    @pytest.mark.parametrize("label", list(_ACTIONABLE_LABELS))
    def test_template_shape(self, label: str) -> None:
        t = SOURCING_MECHANISM_TEMPLATES[label]
        assert isinstance(t, Template)
        assert t.dimension == "sourcing_constraint"
        assert t.rule_slug == label
        assert t.evidence_fact == _FACT
        assert t.message.strip()
        # Path #1 — informational, no scoring change. Target tier is 0
        # by convention (placeholder for the inverse-axis renderer that
        # consumes target_tier).
        assert t.target_tier == 0

    @pytest.mark.parametrize("label", list(_ACTIONABLE_LABELS))
    def test_template_renders_without_error(self, label: str) -> None:
        t = SOURCING_MECHANISM_TEMPLATES[label]
        rendered = t.message.format(
            entity="Calendar",
            element_name="totalInstructionalDays",
            workflow="upstream business process",
        )
        assert "Calendar" in rendered
        assert "totalInstructionalDays" in rendered


# ---------------------------------------------------------------------------
# generate_for_row — integration
# ---------------------------------------------------------------------------


def _dim(value: int | None, rule: str, *, confidence: str = "high") -> dict[str, Any]:
    return {
        "value": value,
        "rule_matched": rule,
        "inputs_used": {},
        "confidence": confidence,
    }


def _row_with_mechanism(
    *,
    sf_value: int | None = 1,
    sf_rule: str = "tier_1_divergent_unclear",
    mechanism: str | None = "transformation",
    downgraded: bool = False,
) -> dict[str, Any]:
    """Synthetic source-lens sidecar row exercising the SF + mechanism path."""
    dimensions = {
        "semantic_fidelity": _dim(sf_value, sf_rule),
    }
    fact_provenance: dict[str, dict[str, Any]] = {}
    if mechanism is not None:
        fact_provenance[_FACT] = {
            "value": mechanism,
            "confidence": "high",
            "downgraded": downgraded,
            "downgrade_reason": "hallucinated_span" if downgraded else None,
        }
    return {
        "record_key": "AZ|StudentSchoolAttendanceEvent|SchoolYear",
        "entity": "StudentSchoolAttendanceEvent",
        "element_name": "SchoolYear",
        "dimensions": dimensions,
        "fact_provenance": fact_provenance,
        "review": {"needs_review": False, "reasons": [], "route": None},
        "in_scope": True,
        "adjusted_nachos_score": 1.0,
        "nachos_justification": "tier_1",
    }


class TestGenerateForRowMechanism:
    @pytest.mark.parametrize("mechanism", list(_ACTIONABLE_LABELS))
    def test_actionable_mechanism_emits_recommendation(self, mechanism: str) -> None:
        row = _row_with_mechanism(mechanism=mechanism)
        result = generate_for_row(row)
        recs = [r for r in result["recommendations"] if r["dimension"] == "sourcing_constraint"]
        assert len(recs) == 1
        rec = recs[0]
        assert rec["template_id"] == f"sourcing_constraint.{mechanism}"
        assert rec["current_rule"] == mechanism
        assert rec["evidence"]["fact"] == _FACT
        assert rec["evidence"]["value"] == mechanism

    @pytest.mark.parametrize("mechanism", ["none", "unspecified"])
    def test_no_evidence_mechanism_does_not_emit(self, mechanism: str) -> None:
        """`none` / `unspecified` carry no actionable refinement —
        path #1 silently skips them on Recommendations."""
        row = _row_with_mechanism(mechanism=mechanism)
        result = generate_for_row(row)
        recs = [r for r in result["recommendations"] if r["dimension"] == "sourcing_constraint"]
        assert recs == []

    def test_downgraded_provenance_does_not_emit(self) -> None:
        """A downgraded fact carries `value=None` in `_fact_provenance`
        but the mechanism slot may still resolve from a hallucinated
        label. Path #1 skips downgraded provenance to avoid surfacing
        recommendations from rejected LLM output."""
        row = _row_with_mechanism(mechanism="transformation", downgraded=True)
        result = generate_for_row(row)
        recs = [r for r in result["recommendations"] if r["dimension"] == "sourcing_constraint"]
        assert recs == []

    def test_missing_provenance_does_not_emit(self) -> None:
        """Pre-extract sidecars do not carry the new fact —
        recommendations module silently degrades."""
        row = _row_with_mechanism(mechanism=None)
        result = generate_for_row(row)
        recs = [r for r in result["recommendations"] if r["dimension"] == "sourcing_constraint"]
        assert recs == []

    @pytest.mark.parametrize(
        "sf_rule",
        [
            "tier_3_aligned",
            "tier_0_unresolved",
            "null_not_applicable",
        ],
    )
    def test_only_fires_on_divergent_cluster(self, sf_rule: str) -> None:
        """Mechanism refinement is scoped to the divergent SF cluster.

        Even with a non-`none` mechanism resolved, rows whose SF lands at
        aligned / unresolved / not_applicable do not surface a mechanism
        recommendation — clutter management on the analyst sheet.
        """
        row = _row_with_mechanism(sf_rule=sf_rule, mechanism="transformation")
        result = generate_for_row(row)
        recs = [r for r in result["recommendations"] if r["dimension"] == "sourcing_constraint"]
        assert recs == []

    @pytest.mark.parametrize(
        "sf_rule",
        [
            "tier_1_divergent_unclear",
            "tier_2_divergent_explained",
        ],
    )
    def test_fires_on_each_divergent_slug(self, sf_rule: str) -> None:
        row = _row_with_mechanism(sf_rule=sf_rule, mechanism="transformation")
        result = generate_for_row(row)
        recs = [r for r in result["recommendations"] if r["dimension"] == "sourcing_constraint"]
        assert len(recs) == 1


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


class TestMarkdownRendering:
    def test_mechanism_label_appears_in_digest(self) -> None:
        """Issue #59 §Acceptance — the mechanism shows up on the
        markdown digest. Avoids the "tier None ↑ lift to tier 0" shape
        the generic per-dimension renderer would produce on a
        non-tiered axis."""
        row = _row_with_mechanism(mechanism="transformation")
        record = generate_for_row(row)
        result = {
            "generated_on": "2026-04-29",
            "state": "AZ",
            "lens": "source",
            "source_sidecar": "az_scores_source.json",
            "prompt_version": "phase-a.v1",
            "scoring_plan_version": 17,
            "model": "test",
            "record_count": 1,
            "rows_with_recommendations": 1,
            "rows_at_target": 0,
            "recommendations_total": len(record["recommendations"]),
            "by_template": {"sourcing_constraint.transformation": 1},
            "by_dimension": {"sourcing_constraint": 1},
            "by_entity_top": {},
            "hero_examples": [record["record_key"]],
            "records": [record],
            "guidance": "Test",
        }
        digest = render_markdown(result, hero_n=1)
        assert "sourcing_constraint" in digest
        assert "mechanism: transformation" in digest
        assert "tier None" not in digest
