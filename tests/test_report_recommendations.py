"""Track C — state-facing recommendations generator tests.

Verifies the deterministic template layer at
``src/report/recommendations.py``:

- Per-template render produces the expected message with the record's
  entity / element_name threaded through and the right evidence fact.
- ``generate_for_row`` skips at-target dimensions, skips not-applicable
  dimensions, and emits one recommendation per non-at-target dimension
  with a matching template.
- ``generate_state`` aggregates per-dimension + per-template counts.
- ``run()`` dual-writes JSON + MD.
- CLI wiring: ``mc report recommendations`` invokes the module's
  ``run()`` with the right kwargs.
- Policy discipline mirrors ``score/rules.py``: no ``import re``, no
  narrative-text field reads.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from src.report import recommendations as rec_module
from src.report.recommendations import (
    GAP_BASE_TEMPLATE,
    RECOMMENDATIONS,
    Template,
    generate_for_gap_row,
    generate_for_row,
    generate_state,
    render_markdown,
    run as run_recommendations,
    workflow_for,
)


# ---------------------------------------------------------------------------
# Fixtures — minimal scored sidecar rows
# ---------------------------------------------------------------------------


def _dim(value: int | None, rule: str, *, confidence: str = "high") -> dict[str, Any]:
    return {
        "value": value,
        "rule_matched": rule,
        "inputs_used": {},
        "confidence": confidence,
    }


def _provenance(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "value": value,
            "confidence": "high",
            "downgraded": False,
            "downgrade_reason": None,
        }
        for name, value in facts.items()
    }


def _row(
    *,
    record_key: str = "AZ|Calendar|BeginDate",
    entity: str = "Calendar",
    element_name: str = "BeginDate",
    dimensions: dict[str, dict[str, Any]],
    facts: dict[str, Any] | None = None,
    in_scope: bool = True,
    needs_review: bool = False,
) -> dict[str, Any]:
    return {
        "record_key": record_key,
        "entity": entity,
        "element_name": element_name,
        "dimensions": dimensions,
        "fact_provenance": _provenance(facts or {}),
        "review": {
            "needs_review": needs_review,
            "reasons": ["low_confidence_dimension:x"] if needs_review else [],
            "route": "ANALYST" if needs_review else None,
        },
        "in_scope": in_scope,
        "adjusted_nachos_score": 1.5,
        "nachos_justification": "tier_1",
    }


# ---------------------------------------------------------------------------
# Template catalog
# ---------------------------------------------------------------------------


class TestTemplateCatalog:
    def test_covers_every_non_at_target_az_source_slug(self) -> None:
        """Every AZ source-lens rule slug that ever fires with a
        below-target value must have a template. The catalog is a
        superset of what AZ actually needs so new states don't regress."""
        required = {
            ("canonical_name_alignment", "tier_0_unresolved"),
            # tier_1_resolved + tier_2_cosmetic deliberately absent — see
            # template-catalog comments in src/report/recommendations.py.
            ("definition_quality", "tier_0_missing"),
            ("definition_quality", "tier_1_minimal"),
            ("definition_quality", "tier_2_substantive"),
            ("semantic_fidelity", "tier_1_divergent_unclear"),
            ("semantic_fidelity", "tier_2_divergent_explained"),
            ("extension_justification", "tier_0_unnecessary_mirror"),
            ("extension_justification", "tier_1_partial"),
            ("extension_justification", "tier_2_necessary_companion"),
            ("nachos_score", "tier_1_conditional"),
            ("nachos_score", "tier_3_aggregation"),
            ("nachos_score", "tier_3_concatenation"),
        }
        missing = required - set(RECOMMENDATIONS.keys())
        assert not missing, f"missing templates: {sorted(missing)}"

    def test_covers_every_non_at_target_az_spine_slug(self) -> None:
        """Spine-lens slugs that fire below target on AZ."""
        required = {
            ("documentation_completeness", "tier_0_missing"),
            ("documentation_completeness", "tier_1_minimal"),
            # tier_2_structure deliberately absent — see template-catalog
            # comment in src/report/recommendations.py.
            ("obligation_clarity", "tier_0_none"),
            ("obligation_clarity", "tier_0_descriptor_enum"),
            ("obligation_clarity", "tier_1_partial"),
            ("obligation_clarity", "tier_2_conditional"),
            ("nachos_score", "tier_1_conditional"),
            ("nachos_score", "tier_3_aggregation"),
            ("nachos_score", "tier_3_concatenation"),
            # Phase 1 (Apr 23) — spine-lens cost axis; both fire on
            # AZ/WI/MN/TX spine. Same posture as nachos_score: target=0,
            # push the work upstream.
            ("business_logic_complexity", "tier_1_cond_or_cross"),
            ("business_logic_complexity", "tier_2_agg_or_multicross"),
        }
        missing = required - set(RECOMMENDATIONS.keys())
        assert not missing, f"missing spine templates: {sorted(missing)}"

    def test_minimum_11_templates(self) -> None:
        """Track C contract pins a minimum of 11 templates (sketch §4.3)."""
        assert len(RECOMMENDATIONS) >= 11

    @pytest.mark.parametrize(
        "key",
        sorted(RECOMMENDATIONS.keys()),
    )
    def test_template_shape(self, key: tuple[str, str]) -> None:
        template = RECOMMENDATIONS[key]
        assert isinstance(template, Template)
        assert template.dimension == key[0]
        assert template.rule_slug == key[1]
        assert template.message.strip()
        assert template.target_tier in (0, 1, 2, 3)
        # Inverse axis dimensions always target 0; quality dimensions
        # target 3. ``business_logic_complexity`` joined nachos_score on
        # the cost axis in the Apr 23 voice rewrite (rules.py:480 already
        # documented it as cost-axis).
        if template.dimension in ("nachos_score", "business_logic_complexity"):
            assert template.target_tier == 0
        else:
            assert template.target_tier == 3

    @pytest.mark.parametrize(
        "key",
        sorted(RECOMMENDATIONS.keys()),
    )
    def test_template_renders_without_error(self, key: tuple[str, str]) -> None:
        template = RECOMMENDATIONS[key]
        # Inverse-axis templates (nachos_score / business_logic_complexity)
        # carry a ``{workflow}`` placeholder filled by ``workflow_for`` at
        # generation time; the test passes a synthetic value so the
        # ``str.format`` call doesn't KeyError on those templates.
        rendered = template.message.format(
            entity="E", element_name="x", workflow="upstream business process"
        )
        assert "E.x" in rendered or "`E.x`" in rendered or "E" in rendered


# ---------------------------------------------------------------------------
# Workflow lookup — entity → upstream-workflow class
# ---------------------------------------------------------------------------


class TestWorkflowLookup:
    @pytest.mark.parametrize(
        "entity,expected_substr",
        [
            ("Calendar", "calendar"),
            ("CalendarDate", "calendar"),
            ("StudentSchoolAttendanceEvent", "attendance"),
            ("StudentSpecialEducationProgramAssociation", "special-education"),
            ("CourseTranscript", "transcript"),
            ("PayrollExt", "financial"),
            ("StudentSchoolFoodServiceProgramAssociation", "nutrition"),
            ("LocalEducationAgency", "organization"),
            ("Section", "section and course"),
        ],
    )
    def test_known_entity_substring_maps_to_workflow(
        self, entity: str, expected_substr: str
    ) -> None:
        assert expected_substr in workflow_for(entity)

    def test_empty_entity_returns_default(self) -> None:
        assert workflow_for(None) == "upstream business process"
        assert workflow_for("") == "upstream business process"

    def test_unknown_entity_returns_default(self) -> None:
        assert (
            workflow_for("CompletelyMadeUpEntityName")
            == "upstream business process"
        )


# ---------------------------------------------------------------------------
# generate_for_row
# ---------------------------------------------------------------------------


class TestGenerateForRow:
    def test_quality_dim_below_target_emits_recommendation(self) -> None:
        row = _row(
            dimensions={
                "canonical_name_alignment": _dim(0, "tier_0_unresolved"),
            },
            facts={"element_name_matches_canonical": False},
        )
        result = generate_for_row(row)
        assert len(result["recommendations"]) == 1
        rec = result["recommendations"][0]
        assert rec["dimension"] == "canonical_name_alignment"
        assert rec["current_tier"] == 0
        assert rec["target_tier"] == 3
        assert rec["current_rule"] == "tier_0_unresolved"
        assert "BeginDate" in rec["message"]
        assert rec["evidence"]["fact"] == "element_name_matches_canonical"
        assert rec["evidence"]["value"] is False
        assert rec["automation"] == "deterministic"

    def test_inverse_dim_above_zero_emits_recommendation(self) -> None:
        row = _row(
            dimensions={
                "nachos_score": _dim(3, "tier_3_aggregation"),
            },
            facts={"has_aggregation": True},
        )
        result = generate_for_row(row)
        assert len(result["recommendations"]) == 1
        rec = result["recommendations"][0]
        assert rec["dimension"] == "nachos_score"
        assert rec["current_tier"] == 3
        assert rec["target_tier"] == 0  # inverse axis
        assert "simplify" in rec["message"].lower() or "aggregate" in rec["message"].lower()

    def test_quality_dim_at_target_skipped(self) -> None:
        row = _row(
            dimensions={
                "canonical_name_alignment": _dim(3, "tier_3_exact"),
            },
        )
        result = generate_for_row(row)
        assert result["recommendations"] == []
        # current_scores still reports the dimension.
        assert result["current_scores"]["canonical_name_alignment"]["tier"] == 3

    def test_inverse_dim_at_target_skipped(self) -> None:
        """nachos_score=0 is already at target — skip, don't recommend."""
        row = _row(
            dimensions={
                "nachos_score": _dim(0, "tier_0_none"),
            },
        )
        result = generate_for_row(row)
        assert result["recommendations"] == []

    def test_business_logic_complexity_at_target_skipped(self) -> None:
        """business_logic_complexity=0 is already at target — skip.

        Phase 1 (Apr 23) added this dimension to the inverse-axis set;
        the regression matters because it previously fell into the
        quality-axis branch and would have asked for a tier_0_none
        template that does not exist.
        """
        row = _row(
            dimensions={
                "business_logic_complexity": _dim(0, "tier_0_none"),
            },
        )
        result = generate_for_row(row)
        assert result["recommendations"] == []

    def test_business_logic_complexity_above_zero_emits(self) -> None:
        """tier_2_agg_or_multicross fires the new spine-lens template."""
        row = _row(
            entity="Calendar",
            element_name="totalInstructionalDays",
            dimensions={
                "business_logic_complexity": _dim(
                    2, "tier_2_agg_or_multicross"
                ),
            },
            facts={"has_aggregation": True},
        )
        result = generate_for_row(row)
        assert len(result["recommendations"]) == 1
        rec = result["recommendations"][0]
        assert rec["dimension"] == "business_logic_complexity"
        assert rec["current_tier"] == 2
        assert rec["target_tier"] == 0
        # Workflow substitution — Calendar entity should resolve to the
        # calendar workflow class via workflow_for().
        assert "calendar" in rec["message"].lower()
        # Pass-through close phrase locks the Maria-shaped tier-delta
        # framing in.
        assert "pass-through" in rec["message"].lower()

    def test_not_applicable_dim_skipped(self) -> None:
        """value=None (e.g. extension_justification on core rows) is skipped."""
        row = _row(
            dimensions={
                "extension_justification": _dim(None, "not_applicable"),
            },
        )
        result = generate_for_row(row)
        assert result["recommendations"] == []
        # But current_scores preserves the None so the full audit trail is visible.
        assert result["current_scores"]["extension_justification"]["tier"] is None

    def test_missing_template_silently_skipped(self) -> None:
        """A rule slug that has no template entry is a no-op (not an error).

        This lets new rule slugs ship without breaking the generator; the
        catalog can be extended in a follow-up PR."""
        row = _row(
            dimensions={
                "canonical_name_alignment": _dim(1, "unknown_rule_slug"),
            },
        )
        result = generate_for_row(row)
        assert result["recommendations"] == []

    def test_confidence_min_of_dimension_and_evidence(self) -> None:
        """Recommendation confidence = min(dim_confidence, evidence_confidence)."""
        row = _row(
            dimensions={
                "canonical_name_alignment": _dim(0, "tier_0_unresolved", confidence="medium"),
            },
            facts={"element_name_matches_canonical": False},
        )
        # Dim=medium, evidence=low → combined=low.
        row["fact_provenance"]["element_name_matches_canonical"]["confidence"] = "low"
        result = generate_for_row(row)
        assert result["recommendations"][0]["confidence"] == "low"

    def test_review_status_pending_when_needs_review(self) -> None:
        row = _row(
            dimensions={"canonical_name_alignment": _dim(0, "tier_0_unresolved")},
            needs_review=True,
        )
        result = generate_for_row(row)
        assert result["review_status"] == "pending"

    def test_review_status_clean_otherwise(self) -> None:
        row = _row(dimensions={"canonical_name_alignment": _dim(0, "tier_0_unresolved")})
        result = generate_for_row(row)
        assert result["review_status"] == "clean"


# ---------------------------------------------------------------------------
# generate_state / run()
# ---------------------------------------------------------------------------


def _write_sidecar(
    tmp_path: Path, state: str, lens: str, rows: list[dict[str, Any]]
) -> Path:
    payload = {
        "state": state,
        "lens": lens,
        "edfi_version": "v5.0.0",
        "scored_at": "2026-04-23T00:00:00Z",
        "model": "test",
        "prompt_version": "v1",
        "scoring_plan_version": 4,
        "record_count": len(rows),
        "scored_count": len(rows),
        "skipped_count": 0,
        "scores": rows,
    }
    path = tmp_path / f"{state.lower()}_scores_{lens}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestGenerateState:
    def test_counts_roll_up_correctly(self, tmp_path: Path) -> None:
        rows = [
            _row(
                record_key="AZ|E|a",
                entity="E",
                element_name="a",
                dimensions={
                    "canonical_name_alignment": _dim(0, "tier_0_unresolved"),
                    "definition_quality": _dim(3, "tier_3_detail_beyond_edfi"),
                    "nachos_score": _dim(0, "tier_0_none"),
                },
            ),
            _row(
                record_key="AZ|E|b",
                entity="E",
                element_name="b",
                dimensions={
                    "canonical_name_alignment": _dim(3, "tier_3_exact"),
                    "definition_quality": _dim(1, "tier_1_minimal"),
                    "nachos_score": _dim(3, "tier_3_aggregation"),
                },
                facts={"has_aggregation": True},
            ),
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        result = generate_state("AZ", "source", base=tmp_path)
        assert result["record_count"] == 2
        assert result["rows_with_recommendations"] == 2
        assert result["rows_at_target"] == 0
        assert result["recommendations_total"] == 3
        assert result["by_dimension"] == {
            "canonical_name_alignment": 1,
            "definition_quality": 1,
            "nachos_score": 1,
        }

    def test_row_fully_at_target_does_not_inflate_rec_count(
        self, tmp_path: Path
    ) -> None:
        rows = [
            _row(
                record_key="AZ|E|a",
                entity="E",
                element_name="a",
                dimensions={
                    "canonical_name_alignment": _dim(3, "tier_3_exact"),
                    "definition_quality": _dim(3, "tier_3_detail_beyond_edfi"),
                    "nachos_score": _dim(0, "tier_0_none"),
                },
            ),
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        result = generate_state("AZ", "source", base=tmp_path)
        assert result["rows_with_recommendations"] == 0
        assert result["rows_at_target"] == 1
        assert result["recommendations_total"] == 0

    def test_hero_examples_are_record_keys(self, tmp_path: Path) -> None:
        rows = [
            _row(
                record_key=f"AZ|E|{i}",
                entity="E",
                element_name=str(i),
                dimensions={
                    "canonical_name_alignment": _dim(0, "tier_0_unresolved"),
                },
            )
            for i in range(6)
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        result = generate_state("AZ", "source", base=tmp_path, hero_n=3)
        assert len(result["hero_examples"]) <= 3
        for key in result["hero_examples"]:
            assert key.startswith("AZ|E|")


class TestRun:
    def test_run_writes_json_only_by_default(self, tmp_path: Path) -> None:
        """R4 artifact diet: the multi-MB MD digest has no downstream
        consumer, so the default run writes JSON only."""
        rows = [
            _row(
                record_key="AZ|E|a",
                entity="E",
                element_name="a",
                dimensions={"canonical_name_alignment": _dim(0, "tier_0_unresolved")},
            )
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        result = run_recommendations(state="AZ", lens="source", out=tmp_path)
        json_path = tmp_path / "az_recommendations_source.json"
        md_path = tmp_path / "az_recommendations_source.md"
        assert json_path.exists()
        assert not md_path.exists()
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        assert on_disk == result

    def test_run_emit_md_writes_both(self, tmp_path: Path) -> None:
        rows = [
            _row(
                record_key="AZ|E|a",
                entity="E",
                element_name="a",
                dimensions={"canonical_name_alignment": _dim(0, "tier_0_unresolved")},
            )
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        result = run_recommendations(
            state="AZ", lens="source", out=tmp_path, emit_md=True,
        )
        json_path = tmp_path / "az_recommendations_source.json"
        md_path = tmp_path / "az_recommendations_source.md"
        assert json_path.exists()
        assert md_path.exists()
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        assert on_disk == result
        md = md_path.read_text(encoding="utf-8")
        assert "AZ" in md
        assert "source-lens" in md
        assert "`E.a`" in md


class TestRenderMarkdown:
    def test_markdown_has_roll_up_and_hero_and_entities(self, tmp_path: Path) -> None:
        rows = [
            _row(
                record_key="AZ|E|a",
                entity="E",
                element_name="a",
                dimensions={
                    "canonical_name_alignment": _dim(0, "tier_0_unresolved"),
                    "nachos_score": _dim(3, "tier_3_aggregation"),
                },
                facts={
                    "element_name_matches_canonical": False,
                    "has_aggregation": True,
                },
            )
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        result = generate_state("AZ", "source", base=tmp_path)
        md = render_markdown(result, hero_n=1)
        assert "## Roll-up" in md
        assert "## Recommendations by dimension" in md
        assert "## Hero examples" in md
        assert "## All recommendations by entity" in md
        # Inverse-axis arrow rendering for nachos_score.
        assert "↓ simplify to tier 0" in md


# ---------------------------------------------------------------------------
# Phase 3 — signed impact, leverage-weighted hero ranking, pin list
# ---------------------------------------------------------------------------


class TestImpactField:
    """Phase 3 adds a signed ``impact`` to every recommendation so the
    XLSX sheet, the markdown digest, and the hero ranker can all
    consult a single field. Positive on quality axes (lift), negative
    on cost axes (simplify)."""

    def test_quality_axis_impact_is_positive(self) -> None:
        row = _row(
            dimensions={
                "canonical_name_alignment": _dim(0, "tier_0_unresolved"),
            },
            facts={"element_name_matches_canonical": False},
        )
        rec = generate_for_row(row)["recommendations"][0]
        assert rec["impact"] == 3
        assert rec["target_tier"] - rec["current_tier"] == rec["impact"]

    def test_cost_axis_impact_is_negative(self) -> None:
        row = _row(
            dimensions={
                "nachos_score": _dim(3, "tier_3_aggregation"),
            },
            facts={"has_aggregation": True},
        )
        rec = generate_for_row(row)["recommendations"][0]
        assert rec["impact"] == -3

    def test_business_logic_complexity_impact_is_negative(self) -> None:
        """BLC is a cost axis (Phase 1 _INVERSE_DIMENSIONS fix). Impact
        must stay signed-negative so abs-ranking treats it identically
        to nachos_score."""
        row = _row(
            dimensions={
                "business_logic_complexity": _dim(
                    2, "tier_2_agg_or_multicross"
                ),
            },
            facts={"has_aggregation": True},
        )
        rec = generate_for_row(row)["recommendations"][0]
        assert rec["impact"] == -2


class TestHeroRanking:
    """Phase 3 promotes total abs-impact to the primary hero-ranking
    key. Higher-leverage rows outrank lower-leverage rows even when
    they have fewer firing dimensions."""

    def test_higher_impact_row_outranks_lower_impact_row(
        self, tmp_path: Path
    ) -> None:
        # Row A: one rec with impact +3 (low leverage on quality axis).
        # Row B: one rec with impact -3 (cost axis; ranking is by abs).
        rows = [
            _row(
                record_key="AZ|E|low",
                entity="E",
                element_name="low",
                dimensions={
                    "canonical_name_alignment": _dim(0, "tier_0_unresolved"),
                },
                facts={"element_name_matches_canonical": False},
            ),
            _row(
                record_key="AZ|E|high",
                entity="E",
                element_name="high",
                dimensions={
                    "nachos_score": _dim(3, "tier_3_aggregation"),
                },
                facts={"has_aggregation": True},
            ),
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        result = generate_state("AZ", "source", base=tmp_path, hero_n=1)
        # With hero_n=1 only the highest-leverage row survives the pick.
        assert result["hero_examples"] == ["AZ|E|high"]


class TestPinnedHero:
    """Phase 3 ships a ``_PINNED_HERO_KEYS`` registry so docs that cite
    specific hero rows verbatim (see docs/recommendations.md §3.b and
    §5.4) stay stable across re-scorings. Pinned records enter the
    hero list regardless of ranking."""

    def test_pin_forces_inclusion_when_ranking_would_exclude(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two rows: ``leader`` has higher leverage (would naturally win
        # at hero_n=1); ``pinned`` has lower leverage but is pinned.
        rows = [
            _row(
                record_key="AZ|E|leader",
                entity="E",
                element_name="leader",
                dimensions={
                    "nachos_score": _dim(3, "tier_3_aggregation"),
                },
                facts={"has_aggregation": True},
            ),
            _row(
                record_key="AZ|E|pinned",
                entity="E",
                element_name="pinned",
                dimensions={
                    "canonical_name_alignment": _dim(0, "tier_0_unresolved"),
                },
                facts={"element_name_matches_canonical": False},
            ),
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        monkeypatch.setitem(
            rec_module._PINNED_HERO_KEYS,
            ("AZ", "source"),
            ("AZ|E|pinned",),
        )
        result = generate_state("AZ", "source", base=tmp_path, hero_n=1)
        assert result["hero_examples"] == ["AZ|E|pinned"]

    def test_pin_does_not_fire_when_record_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pin that names a record that isn't in the sidecar is a
        silent no-op, not an error — so stale pins don't break a run."""
        rows = [
            _row(
                record_key="AZ|E|leader",
                entity="E",
                element_name="leader",
                dimensions={
                    "nachos_score": _dim(3, "tier_3_aggregation"),
                },
                facts={"has_aggregation": True},
            ),
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        monkeypatch.setitem(
            rec_module._PINNED_HERO_KEYS,
            ("AZ", "source"),
            ("AZ|E|does_not_exist",),
        )
        result = generate_state("AZ", "source", base=tmp_path, hero_n=1)
        # Leader still wins — the stale pin silently drops.
        assert result["hero_examples"] == ["AZ|E|leader"]

    def test_az_source_pins_calendar_totalinstructionaldays(self) -> None:
        """The AZ source-lens pin is the one citation-critical pin the
        recommendations doc relies on (§3.b exec summary + §5.4 hero
        row). Lock the key so a careless rename of the record surfaces
        as a test failure rather than a silent doc-drift."""
        assert rec_module._PINNED_HERO_KEYS.get(("AZ", "source")) == (
            "AZ|Calendar|TotalInstructionalDays",
        )


class TestMarkdownImpactSuffix:
    """The markdown digest exposes ``impact +N`` / ``impact -N`` on each
    recommendation line so a human reader can scan a scorecard for the
    highest-leverage rows without opening the XLSX."""

    def test_markdown_shows_positive_impact(self, tmp_path: Path) -> None:
        rows = [
            _row(
                dimensions={
                    "canonical_name_alignment": _dim(0, "tier_0_unresolved"),
                },
                facts={"element_name_matches_canonical": False},
            )
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        result = generate_state("AZ", "source", base=tmp_path)
        md = render_markdown(result, hero_n=1)
        assert "impact +3" in md

    def test_markdown_shows_negative_impact(self, tmp_path: Path) -> None:
        rows = [
            _row(
                dimensions={
                    "nachos_score": _dim(3, "tier_3_aggregation"),
                },
                facts={"has_aggregation": True},
            )
        ]
        _write_sidecar(tmp_path, "AZ", "source", rows)
        result = generate_state("AZ", "source", base=tmp_path)
        md = render_markdown(result, hero_n=1)
        assert "impact -3" in md


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self) -> None:
        assert not isinstance(rec_module.run, click.Command)
        assert inspect.isfunction(rec_module.run)

    def test_cli_invokes_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_run(
            *, state: str, lens: str, out: Path | None = None,
            hero_n: int = 4, emit_md: bool = False, allow_stale: bool = False,
        ):
            captured["state"] = state
            captured["lens"] = lens
            captured["hero_n"] = hero_n
            captured["emit_md"] = emit_md
            return {
                "state": state,
                "lens": lens,
                "record_count": 0,
                "rows_with_recommendations": 0,
                "rows_at_target": 0,
                "recommendations_total": 0,
            }

        monkeypatch.setattr(rec_module, "run", _fake_run)
        from src.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["report", "recommendations", "--state", "AZ", "--lens", "source", "--hero-n", "2"],
        )
        assert result.exit_code == 0, result.output
        assert captured["state"] == "AZ"
        assert captured["lens"] == "source"
        assert captured["hero_n"] == 2
        # R4 artifact diet: MD is opt-in; the default CLI run passes False.
        assert captured["emit_md"] is False
        assert "recommendations (AZ/source)" in result.output

    def test_cli_emit_md_flag_passes_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_run(
            *, state: str, lens: str, out: Path | None = None,
            hero_n: int = 4, emit_md: bool = False, allow_stale: bool = False,
        ):
            captured["emit_md"] = emit_md
            return {
                "state": state,
                "lens": lens,
                "record_count": 0,
                "rows_with_recommendations": 0,
                "rows_at_target": 0,
                "recommendations_total": 0,
            }

        monkeypatch.setattr(rec_module, "run", _fake_run)
        from src.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["report", "recommendations", "--state", "AZ", "--emit-md"],
        )
        assert result.exit_code == 0, result.output
        assert captured["emit_md"] is True

    def test_cli_accepts_gap_lens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_run(
            *, state: str, lens: str, out: Path | None = None,
            hero_n: int = 4, emit_md: bool = False, allow_stale: bool = False,
        ):
            captured["state"] = state
            captured["lens"] = lens
            captured["hero_n"] = hero_n
            return {
                "state": state,
                "lens": lens,
                "record_count": 0,
                "rows_with_recommendations": 0,
                "rows_at_target": 0,
                "recommendations_total": 0,
            }

        monkeypatch.setattr(rec_module, "run", _fake_run)
        from src.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["report", "recommendations", "--state", "WI", "--lens", "gap", "--hero-n", "2"],
        )
        assert result.exit_code == 0, result.output
        assert captured["lens"] == "gap"
        assert "recommendations (WI/gap)" in result.output


# ---------------------------------------------------------------------------
# Issue #73 — gap-row recommendations surface
# ---------------------------------------------------------------------------


def _gap_row(
    *,
    record_key: str = "AZ|Calendar|beginDate",
    entity: str = "Calendar",
    element_name: str = "beginDate",
    structural_depth: int = 0,
    nachos_score_value: int = 0,
    needs_review: bool = False,
    discovery_lens: str = "spine_anchored",
    adjusted_nachos_score: float = 0.0,
) -> dict[str, Any]:
    """Fabricate a gap-sidecar row (subset of fields the gap-rec generator reads)."""
    return {
        "record_key": record_key,
        "entity": entity,
        "element_name": element_name,
        "dimensions": {
            "documentation_completeness": _dim(0, "tier_0_missing"),
            "obligation_clarity": _dim(0, "tier_0_none"),
            "business_logic_complexity": _dim(0, "tier_0_none"),
            "structural_depth": _dim(structural_depth, f"tier_{structural_depth}"),
            "documentation_style_tier": _dim(0, "tier_0_unspecified"),
            "nachos_score": _dim(nachos_score_value, "tier_0_none"),
            "documentation_gap": _dim(1, "tier_1_gap"),
        },
        "fact_provenance": {},
        "review": {
            "needs_review": needs_review,
            "reasons": ["low_confidence_dimension:x"] if needs_review else [],
            "route": "ANALYST" if needs_review else None,
        },
        "in_scope": True,
        "adjusted_nachos_score": adjusted_nachos_score,
        "nachos_justification": "tier_0_none",
        "discovery_lens": discovery_lens,
    }


def _write_gap_sidecar(
    tmp_path: Path, state: str, rows: list[dict[str, Any]]
) -> Path:
    payload = {
        "state": state,
        "lens": "gap",
        "edfi_version": "v5.0.0",
        "scored_at": "2026-04-29T00:00:00Z",
        "model": "test",
        "prompt_version": "step3.full.v1",
        "scoring_plan_version": 17,
        "record_count": len(rows),
        "scored_count": len(rows),
        "skipped_count": 0,
        "scores": rows,
    }
    path = tmp_path / f"{state.lower()}_scores_gap.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestGenerateForGapRow:
    """``generate_for_gap_row`` emits one record-level rec per gap row plus
    an optional structural-depth callout. Gap rows score every documentation
    -derived dimension at tier 0 by construction; the per-dimension cascade
    is deliberately bypassed (would emit ~5 boilerplate recs per row)."""

    def test_shallow_row_emits_single_base_rec(self) -> None:
        row = _gap_row(structural_depth=0)
        result = generate_for_gap_row(row)
        recs = result["recommendations"]
        assert len(recs) == 1
        rec = recs[0]
        assert rec["dimension"] == GAP_BASE_TEMPLATE.dimension
        assert rec["template_id"] == "documentation_gap.spine_anchored"
        assert rec["current_tier"] is None
        # Base message threads entity, element, and state through.
        assert "Calendar.beginDate" in rec["message"]
        assert "AZ" in rec["message"]
        assert rec["evidence"] is None

    def test_depth_one_does_not_fire_structural_callout(self) -> None:
        row = _gap_row(structural_depth=1)
        recs = generate_for_gap_row(row)["recommendations"]
        assert [r["template_id"] for r in recs] == [
            "documentation_gap.spine_anchored"
        ]

    def test_depth_two_fires_structural_callout(self) -> None:
        row = _gap_row(structural_depth=2)
        recs = generate_for_gap_row(row)["recommendations"]
        template_ids = [r["template_id"] for r in recs]
        assert template_ids == [
            "documentation_gap.spine_anchored",
            "documentation_gap.deep_fk_chain",
        ]
        callout = recs[1]
        assert callout["current_tier"] == 2
        assert "implementation shape 2" in callout["message"]

    def test_depth_three_fires_structural_callout_with_depth_in_text(self) -> None:
        row = _gap_row(structural_depth=3, entity="Section", element_name="sectionId")
        recs = generate_for_gap_row(row)["recommendations"]
        callout = next(r for r in recs if r["template_id"] == "documentation_gap.deep_fk_chain")
        assert "implementation shape 3" in callout["message"]
        assert "Section.sectionId" in callout["message"]

    def test_record_shape_matches_source_lens_generator(self) -> None:
        """Gap-row record dict carries the same keys ``generate_for_row``
        emits, so ``generate_state`` and the markdown renderer can consume
        both flavours uniformly."""
        row = _gap_row()
        result = generate_for_gap_row(row)
        assert set(
            ["record_key", "entity", "element_name", "in_scope",
             "adjusted_nachos_score", "review_status", "current_scores",
             "recommendations"]
        ).issubset(result.keys())
        # Plus the gap-specific marker.
        assert result["discovery_lens"] == "spine_anchored"

    def test_state_label_pulled_from_record_key(self) -> None:
        for code in ("AZ", "WI", "MN", "TX", "IN"):
            row = _gap_row(record_key=f"{code}|E|x")
            rec = generate_for_gap_row(row)["recommendations"][0]
            assert f"{code}'s source documentation" in rec["message"]

    def test_state_label_falls_back_when_key_malformed(self) -> None:
        row = _gap_row(record_key="bogus")
        rec = generate_for_gap_row(row)["recommendations"][0]
        assert "the state's source documentation" in rec["message"]


class TestGenerateStateGapLens:
    """End-to-end — ``generate_state(state, lens="gap")`` reads
    ``data/out/{state}_scores_gap.json`` and dispatches to the gap-row
    generator."""

    def test_dispatches_to_gap_row_generator(self, tmp_path: Path) -> None:
        rows = [
            _gap_row(record_key="AZ|E|a", structural_depth=0),
            _gap_row(record_key="AZ|E|b", structural_depth=2),
            _gap_row(record_key="AZ|E|c", structural_depth=3),
        ]
        _write_gap_sidecar(tmp_path, "AZ", rows)
        result = generate_state("AZ", "gap", base=tmp_path)

        assert result["record_count"] == 3
        assert result["rows_with_recommendations"] == 3
        assert result["rows_at_target"] == 0
        # 3 base recs + 2 structural callouts (rows b + c).
        assert result["recommendations_total"] == 5
        assert set(result["by_template"]) == {
            "documentation_gap.spine_anchored",
            "documentation_gap.deep_fk_chain",
        }
        assert result["by_template"]["documentation_gap.spine_anchored"] == 3
        assert result["by_template"]["documentation_gap.deep_fk_chain"] == 2

    def test_per_dimension_cascade_not_invoked(self, tmp_path: Path) -> None:
        """Even though gap rows have ``documentation_completeness=0`` (a
        below-target value the source-lens cascade would template), the
        gap path bypasses ``RECOMMENDATIONS`` entirely. The only
        dimensions appearing in ``by_dimension`` are ``documentation_gap``."""
        rows = [_gap_row(record_key="AZ|E|x", structural_depth=2)]
        _write_gap_sidecar(tmp_path, "AZ", rows)
        result = generate_state("AZ", "gap", base=tmp_path)
        assert set(result["by_dimension"]) == {"documentation_gap"}

    def test_run_dual_writes_gap_artifacts(self, tmp_path: Path) -> None:
        """Gap lens under `emit_md=True` still dual-writes (the R4 diet
        default is JSON-only — asserted in `TestRun`)."""
        rows = [_gap_row(record_key="AZ|E|a", structural_depth=2)]
        _write_gap_sidecar(tmp_path, "AZ", rows)
        result = run_recommendations(
            state="AZ", lens="gap", out=tmp_path, emit_md=True,
        )
        json_path = tmp_path / "az_recommendations_gap.json"
        md_path = tmp_path / "az_recommendations_gap.md"
        assert json_path.exists()
        assert md_path.exists()
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        assert on_disk == result
        md = md_path.read_text(encoding="utf-8")
        # Header carries the gap-lens label; gap-class flavour tag appears
        # on each rec instead of a "tier X → tier Y" arrow.
        assert "AZ" in md
        assert "gap-lens" in md
        assert "documentation_gap" in md
        assert "gap class:" in md
        # No misleading "↑ lift to tier 0" arrow on the base gap rec.
        assert "↑ lift to tier 0" not in md.split("documentation_gap.spine_anchored")[0]


# ---------------------------------------------------------------------------
# Policy discipline — no narrative reads, no `import re`
# ---------------------------------------------------------------------------


class TestPolicyDiscipline:
    """Same discipline as `score/rules.py` (tests/test_score_rules.py):
    the deterministic recommendations layer never imports `re` and never
    reads narrative-text element fields. Narrative polish belongs in the
    LLM layer, which is gated and not part of the default path."""

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

    def _module_path(self) -> Path:
        return Path(rec_module.__file__)

    def test_re_not_imported(self) -> None:
        tree = ast.parse(self._module_path().read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "re", "recommendations.py must not import `re`"
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "re", "recommendations.py must not import from `re`"

    def test_no_regex_calls(self) -> None:
        tree = ast.parse(self._module_path().read_text(encoding="utf-8"))
        forbidden = {"search", "match", "findall", "fullmatch", "compile", "sub"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden:
                if isinstance(node.value, ast.Name) and node.value.id == "re":
                    pytest.fail(f"recommendations.py must not call re.{node.attr}")

    def test_no_narrative_field_reads(self) -> None:
        tree = ast.parse(self._module_path().read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in self._NARRATIVE_FIELDS:
                pytest.fail(
                    f"recommendations.py references narrative field `{node.attr}` — "
                    "narrative polish belongs in the optional LLM layer."
                )
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
                and node.slice.value in self._NARRATIVE_FIELDS
            ):
                pytest.fail(
                    f"recommendations.py subscript-reads `{node.slice.value}` — "
                    "narrative polish belongs in the optional LLM layer."
                )
