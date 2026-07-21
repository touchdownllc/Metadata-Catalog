"""Phase C per-record aggregate sidecar — shape, math, review signals.

Covers:

- _quality_mean_diagnostic = mean of quality dimensions (NOT
  complexity); internal-only sidecar diagnostic, not surfaced on
  workbooks.
- complexity_score surfaced separately.
- confidence_composite = min across dimension confidences.
- review.needs_review fires on downgraded facts, low-conf dims,
  inter-dim inconsistency.
- Sidecar path is `data/out/{state}_scores_{lens}.json` (plan §8.1).
- Dimension stats header records per-dim distribution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.score.aggregate import (
    SCORING_PLAN_VERSION,
    run as run_aggregate,
    run_all as run_all_aggregate,
)
from src.score.rules import SPINE_RULE_INPUTS


def _write_fact_artifact(
    tmp_path: Path,
    state: str,
    fact: str,
    rows: list[dict],
    *,
    model: str = "claude-sonnet-4-6",
    mode: str = "api",
) -> Path:
    """Write one per-fact artifact in the shape the rules loader expects."""
    header = {
        "__type": "header",
        "state": state,
        "lens": "spine",
        "fact": fact,
        "scored_at": "2026-04-24T00:00:00Z",
        "model": model,
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
        "mode": mode,
        "status": "complete",
        "cost_cap_hit": False,
        "schema_error": None,
    }
    path = tmp_path / f"{state}_{header['lens']}_{fact}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(header) + "\n")
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def _row(
    record_key: str,
    entity: str,
    element_name: str,
    value,
    confidence: str = "high",
    downgrade_reason: str | None = None,
) -> dict:
    """Build one artifact row mimicking the real LLM-path shape."""
    validated = None if downgrade_reason is not None else value
    return {
        "record_key": record_key,
        "entity": entity,
        "element_name": element_name,
        "llm_value": value,
        "validated_value": validated,
        "spans": [],
        "confidence": confidence,
        "downgrade_reason": downgrade_reason,
        "any_invalid_spans": False,
        "model": "claude-sonnet-4-6",
        "prompt_version": "test.v1",
    }


def _seed_minimal_fact_pool(
    tmp_path: Path,
    state: str = "AZ",
    *,
    overrides: dict[str, list[dict]] | None = None,
) -> None:
    """Seed every fact artifact in SPINE_RULE_INPUTS for a single record
    with 'tier_3_full across the board' defaults. Overrides replace
    specific facts' row lists."""
    base_key = f"{state}|Student|id"
    # Sensible tier-3 defaults.
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
        # v16 — issue #63 B5: bare-FK reference gate (default False; the
        # test's 'AZ|Student|id' element doesn't match a spine entity name).
        "element_is_bare_fk_reference": False,
        # v22 — issue #97: parent-entity-gate-only suppression (default False).
        "element_only_parent_entity_gate": False,
        # v2 documentation-explicitness axis (default conceptual so the
        # dimension evaluates to tier 2 without requiring overrides).
        "documentation_style": "conceptual",
    }
    overrides = overrides or {}
    for fact in sorted(SPINE_RULE_INPUTS):
        if fact in overrides:
            rows = overrides[fact]
        else:
            rows = [_row(base_key, "Student", "id", DEFAULTS[fact])]
        _write_fact_artifact(tmp_path, state, fact, rows)


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------


class TestPerRecordAggregate:
    def test_tier_3_quality_and_zero_complexity(self, tmp_path: Path) -> None:
        _seed_minimal_fact_pool(tmp_path)
        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())

        assert payload["state"] == "AZ"
        assert payload["lens"] == "spine"
        assert payload["scoring_plan_version"] == SCORING_PLAN_VERSION
        assert payload["record_count"] == 1
        assert payload["scored_count"] == 1

        score = payload["scores"][0]
        assert score["record_key"] == "AZ|Student|id"
        dims = score["dimensions"]
        assert dims["documentation_completeness"]["value"] == 3
        assert dims["obligation_clarity"]["value"] == 3
        assert dims["business_logic_complexity"]["value"] == 0

        # _quality_mean_diagnostic = mean of quality dims only (docs +
        # obligation). Leading underscore marks this as an internal
        # sidecar diagnostic — not surfaced on workbooks.
        assert score["_quality_mean_diagnostic"] == 3.0
        # complexity surfaced separately.
        assert score["complexity_score"] == 0
        assert score["confidence_composite"] == "high"
        assert score["review"]["needs_review"] is False

    def test_complexity_excluded_from_quality_mean(self, tmp_path: Path) -> None:
        """Per plan §7.3: complexity never sums into the quality mean,
        even when it's at tier 3."""
        _seed_minimal_fact_pool(
            tmp_path,
            overrides={
                "has_aggregation": [_row("AZ|X|y", "X", "y", True)],
                "has_cross_entity_logic": [_row("AZ|X|y", "X", "y", True)],
                "cross_entity_targets": [_row("AZ|X|y", "X", "y", 2)],
                "has_conditional_logic": [_row("AZ|X|y", "X", "y", True)],
                # Quality defaults still tier 3:
                "definition_present": [_row("AZ|X|y", "X", "y", True)],
                "business_rules_present": [_row("AZ|X|y", "X", "y", True)],
                "data_type_canonical": [_row("AZ|X|y", "X", "y", True)],
                "descriptor_values_enumerated": [_row("AZ|X|y", "X", "y", False)],
                "definition_is_implementable": [_row("AZ|X|y", "X", "y", True)],
                "required_when_stated": [_row("AZ|X|y", "X", "y", True)],
                "populations_or_scope_stated": [_row("AZ|X|y", "X", "y", True)],
                "conditional_reporting_stated": [_row("AZ|X|y", "X", "y", False)],
            },
        )
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        score = payload["scores"][0]
        assert score["_quality_mean_diagnostic"] == 3.0  # quality dims only
        assert score["complexity_score"] == 3  # not folded in

    def test_documentation_style_tier_excluded_from_quality_mean_spine(
        self, tmp_path: Path
    ) -> None:
        """v2 Day 2 axis carve-out: `documentation_style_tier` is a
        style axis, not a quality axis. SPINE_QUALITY_DIMENSIONS is
        hardcoded to docs_completeness + obligation_clarity, so the
        doc-explicitness tier never folds in. Regression guard in case
        a future commit widens SPINE_QUALITY_DIMENSIONS reflexively."""
        _seed_minimal_fact_pool(
            tmp_path,
            overrides={
                # unspecified → tier 0; quality dims still tier 3.
                "documentation_style": [
                    _row("AZ|Student|id", "Student", "id", "unspecified")
                ],
            },
        )
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        score = payload["scores"][0]
        # Quality mean is still 3.0 — unspecified=tier 0 did not bring
        # down the mean because doc-explicitness is not a quality dim.
        assert score["_quality_mean_diagnostic"] == 3.0
        # The dimension is still emitted, just not summed in.
        assert score["dimensions"]["documentation_style_tier"]["value"] == 0
        assert (
            score["dimensions"]["documentation_style_tier"]["rule_matched"]
            == "tier_0_unspecified"
        )

    def test_source_quality_dimensions_carves_out_doc_style_tier(self) -> None:
        """The SOURCE_QUALITY_DIMENSIONS tuple must exclude all four
        axis dimensions (NACHOS, Structural Depth, Documentation Style,
        Documentation Gap). Pure import-time shape check — keeps the
        Integration Profile axis carve-outs intact without standing up
        a full source-lens aggregate."""
        from src.score.aggregate import SOURCE_QUALITY_DIMENSIONS

        assert "documentation_style_tier" not in SOURCE_QUALITY_DIMENSIONS
        assert "structural_depth" not in SOURCE_QUALITY_DIMENSIONS
        assert "nachos_score" not in SOURCE_QUALITY_DIMENSIONS
        # v2 Day 3 — documentation_gap is a gap-signal boolean
        # axis, not a quality axis.
        assert "documentation_gap" not in SOURCE_QUALITY_DIMENSIONS
        # Positive check: the original four §7.1 quality dims survive.
        assert set(SOURCE_QUALITY_DIMENSIONS) == {
            "canonical_name_alignment",
            "definition_quality",
            "semantic_fidelity",
            "extension_justification",
        }

    def test_documentation_gap_emitted_per_record(
        self, tmp_path: Path
    ) -> None:
        """v2 Day 3 second-pass dimension surfaces on every record. A
        deep FK chain (structural tier 3) with unspecified narrative
        (doc_explicitness tier 0) fires the gap signal."""
        _seed_minimal_fact_pool(
            tmp_path,
            overrides={
                # Structural tier 3 — deep FK chain.
                "fk_chain_depth": [
                    _row("AZ|Student|id", "Student", "id", 3)
                ],
                "is_natural_key": [
                    _row("AZ|Student|id", "Student", "id", True)
                ],
                # Doc explicitness tier 0 — unspecified narrative.
                "documentation_style": [
                    _row("AZ|Student|id", "Student", "id", "unspecified")
                ],
            },
        )
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        score = payload["scores"][0]
        undoc = score["dimensions"]["documentation_gap"]
        assert undoc["value"] == 1
        assert undoc["rule_matched"] == "tier_1_gap"
        # inputs_used captures the two consulted tier values.
        assert undoc["inputs_used"] == {
            "structural_depth": 3,
            "documentation_style_tier": 0,
        }
        # Quality mean is unaffected — axis carve-out holds.
        assert score["_quality_mean_diagnostic"] == 3.0

    def test_documentation_gap_count_in_header(
        self, tmp_path: Path
    ) -> None:
        """Header carries an `documentation_gap_count` convenience
        field (plan §4 Mitigation 3) so stakeholder readers don't have
        to dig into the nested distribution histogram."""
        _seed_minimal_fact_pool(
            tmp_path,
            overrides={
                "fk_chain_depth": [
                    _row("AZ|Student|id", "Student", "id", 3)
                ],
                "is_natural_key": [
                    _row("AZ|Student|id", "Student", "id", True)
                ],
                "documentation_style": [
                    _row("AZ|Student|id", "Student", "id", "unspecified")
                ],
            },
        )
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        assert payload["documentation_gap_count"] == 1

    def test_documentation_gap_count_zero_when_no_gap(
        self, tmp_path: Path
    ) -> None:
        """Flat scalar leaf on a root entity → no gap fires → count=0."""
        _seed_minimal_fact_pool(tmp_path)
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        assert payload["documentation_gap_count"] == 0

    def test_mixed_quality_rounds(self, tmp_path: Path) -> None:
        """docs=3 + obligation=1 → 2.0 mean."""
        _seed_minimal_fact_pool(
            tmp_path,
            overrides={
                "required_when_stated": [_row("AZ|Student|id", "Student", "id", True)],
                "populations_or_scope_stated": [_row("AZ|Student|id", "Student", "id", False)],
                "conditional_reporting_stated": [_row("AZ|Student|id", "Student", "id", False)],
            },
        )
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        score = payload["scores"][0]
        assert score["_quality_mean_diagnostic"] == 2.0

    def test_descriptor_values_enumerated_spans_surface_in_provenance(
        self, tmp_path: Path
    ) -> None:
        """H3 — when the deterministic fact emits match-text spans,
        ``fact_provenance`` surfaces them. The key is only present on
        facts with a non-empty span list so the sidecar stays tight."""
        def _row_with_spans(rec_key, entity, el, value, spans):
            base = _row(rec_key, entity, el, value)
            base["spans"] = list(spans)
            return base

        _seed_minimal_fact_pool(
            tmp_path,
            overrides={
                "descriptor_values_enumerated": [
                    _row_with_spans(
                        "AZ|Student|id",
                        "Student",
                        "id",
                        True,
                        ["Descriptor values: /calendars | X | Y | Z"],
                    )
                ],
            },
        )
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        fp = payload["scores"][0]["fact_provenance"]
        assert fp["descriptor_values_enumerated"]["value"] is True
        assert fp["descriptor_values_enumerated"]["spans"] == [
            "Descriptor values: /calendars | X | Y | Z"
        ]
        # Facts without spans omit the key — sidecar stays compact.
        assert "spans" not in fp["definition_present"]


# ---------------------------------------------------------------------------
# Review signals
# ---------------------------------------------------------------------------


class TestReviewSignals:
    def test_downgraded_fact_flags_review(self, tmp_path: Path) -> None:
        _seed_minimal_fact_pool(
            tmp_path,
            overrides={
                "definition_is_implementable": [
                    _row(
                        "AZ|Student|id",
                        "Student",
                        "id",
                        True,
                        confidence="medium",
                        downgrade_reason="hallucinated_span",
                    )
                ],
            },
        )
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        score = payload["scores"][0]
        assert score["review"]["needs_review"] is True
        assert (
            "hallucinated_input:definition_is_implementable"
            in score["review"]["reasons"]
        )

    def test_aggregate_attaches_review_route(self, tmp_path: Path) -> None:
        """Phase D: sidecar review block carries the first-principles
        route inline (plan §10). Downgraded fact → SCORING; inter-dim
        inconsistency → DATA_MODEL."""
        _seed_minimal_fact_pool(
            tmp_path,
            overrides={
                "definition_is_implementable": [
                    _row(
                        "AZ|Student|id",
                        "Student",
                        "id",
                        True,
                        confidence="medium",
                        downgrade_reason="hallucinated_span",
                    )
                ],
            },
        )
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        review = payload["scores"][0]["review"]
        assert review["needs_review"] is True
        assert review["route"] == "SCORING"

    def test_inter_dim_inconsistency_flagged(self, tmp_path: Path) -> None:
        """docs=0 with complexity>=2 is implausible — flag it."""
        _seed_minimal_fact_pool(
            tmp_path,
            overrides={
                # Tier-0 docs: no definition
                "definition_present": [_row("AZ|X|y", "X", "y", False)],
                "business_rules_present": [_row("AZ|X|y", "X", "y", False)],
                "data_type_canonical": [_row("AZ|X|y", "X", "y", False)],
                "definition_is_implementable": [_row("AZ|X|y", "X", "y", False)],
                "descriptor_values_enumerated": [_row("AZ|X|y", "X", "y", False)],
                "required_when_stated": [_row("AZ|X|y", "X", "y", False)],
                "populations_or_scope_stated": [_row("AZ|X|y", "X", "y", False)],
                "conditional_reporting_stated": [_row("AZ|X|y", "X", "y", False)],
                # Tier-2 complexity: aggregation alone
                "has_aggregation": [_row("AZ|X|y", "X", "y", True)],
                "has_cross_entity_logic": [_row("AZ|X|y", "X", "y", False)],
                "cross_entity_targets": [_row("AZ|X|y", "X", "y", 0)],
                "has_conditional_logic": [_row("AZ|X|y", "X", "y", False)],
            },
        )
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        score = payload["scores"][0]
        assert score["dimensions"]["documentation_completeness"]["value"] == 0
        assert score["dimensions"]["business_logic_complexity"]["value"] == 2
        assert score["review"]["needs_review"] is True
        assert any(
            r.startswith("inter_dim_inconsistency") for r in score["review"]["reasons"]
        )


# ---------------------------------------------------------------------------
# Header stats
# ---------------------------------------------------------------------------


class TestHeaderStats:
    def test_dimension_distribution_histogram(self, tmp_path: Path) -> None:
        """Header reports per-dim {0,1,2,3} histogram so Phase D can
        rollup without re-reading the scores array."""
        _seed_minimal_fact_pool(tmp_path)
        out_path = tmp_path / "out.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        stats = payload["dimension_stats"]
        assert set(stats) == {
            "documentation_completeness",
            "obligation_clarity",
            "business_logic_complexity",
            "structural_depth",
            "documentation_style_tier",
            "nachos_score",
            # v2 Day 3 — second-pass composite over structural + doc axes.
            "documentation_gap",
        }
        for dim, dim_stats in stats.items():
            # JSON serializes int dict keys to strings; the shape is
            # still a 4-bucket histogram over tier {0,1,2,3}.
            assert set(dim_stats["distribution"]) == {"0", "1", "2", "3"}
            assert dim_stats["count"] == 1


# ---------------------------------------------------------------------------
# Sidecar path
# ---------------------------------------------------------------------------


class TestDefaultPath:
    def test_default_sidecar_path(self) -> None:
        from src.utils.paths import project_root, state_scores_path

        p = state_scores_path("AZ", "spine")
        assert p == project_root() / "data" / "out" / "az_scores_spine.json"


# ---------------------------------------------------------------------------
# run_all convenience
# ---------------------------------------------------------------------------


class TestRunAll:
    def test_run_all_covers_requested_states(self, tmp_path: Path) -> None:
        for state in ("AZ", "WI"):
            _seed_minimal_fact_pool(tmp_path, state=state)
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        headers = run_all_aggregate(
            lens="spine",
            states=["AZ", "WI"],
            artifacts_dir=tmp_path,
            out_dir=out_dir,
        )
        assert [h["state"] for h in headers] == ["AZ", "WI"]
        assert (out_dir / "az_scores_spine.json").exists()
        assert (out_dir / "wi_scores_spine.json").exists()

    def test_unknown_lens_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown lens"):
            run_aggregate(state="AZ", lens="holographic")

    def test_missing_fact_artifact_gives_helpful_message(
        self, tmp_path: Path
    ) -> None:
        """A fact artifact missing from disk must raise FileNotFoundError
        naming the fact AND the exact command to populate it.

        Self-onboarding gap: `aggregate --lens source` used to fail with
        a bare 'fact artifact missing' and no hint that three source-
        deterministic facts hadn't been extracted. The message now names
        the fact, the lens, and the `score extract` command so a new
        developer can recover without reading the traceback into the
        extractor's internals."""
        # Seed *almost* every fact — leave one source-deterministic
        # fact (element_name_matches_canonical) absent so aggregate
        # trips on it. Pick source lens because that's where the cold-
        # run gap surfaced.
        from src.score.rules import SOURCE_RULE_INPUTS
        missing_fact = "element_name_matches_canonical"
        state = "AZ"
        base_key = f"{state}|Student|id"
        row = _row(base_key, "Student", "id", True)
        for fact in sorted(SOURCE_RULE_INPUTS):
            if fact == missing_fact:
                continue
            path = tmp_path / f"{state}_source_{fact}.jsonl"
            header = {
                "__type": "header",
                "state": state,
                "lens": "source",
                "fact": fact,
                "scored_at": "2026-04-24T00:00:00Z",
                "model": "deterministic",
                "prompt_version": "det.v5",
                "record_count": 1,
                "scored_count": 1,
                "skipped_count": 0,
                "entities_processed": 1,
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_usd": 0.0,
                "cache_hit_count": 0,
                "downgrade_count": 0,
                "mode": "deterministic",
                "status": "complete",
                "cost_cap_hit": False,
                "schema_error": None,
            }
            with path.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(header) + "\n")
                fh.write(json.dumps(row) + "\n")

        with pytest.raises(FileNotFoundError) as excinfo:
            run_aggregate(
                state=state,
                lens="source",
                artifacts_dir=tmp_path,
                out_path=tmp_path / "az_scores_source.json",
            )
        message = str(excinfo.value)
        assert missing_fact in message
        assert "mc score extract" in message
        assert "--lens source" in message
        assert f"--state {state}" in message


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_score_aggregate_invokes_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mc score aggregate --state AZ` calls run() with the
        expected kwargs and prints a summary line."""
        from click.testing import CliRunner

        captured: dict = {}

        def _fake_run(**kwargs):
            captured.update(kwargs)
            return {
                "state": kwargs["state"],
                "record_count": 1,
                "mean_quality_score": 2.5,
                "needs_review_count": 0,
            }

        from src.score import aggregate as agg_module

        monkeypatch.setattr(agg_module, "run", _fake_run)

        from src.cli import cli

        result = CliRunner().invoke(
            cli, ["score", "aggregate", "--state", "AZ", "--lens", "spine"]
        )
        assert result.exit_code == 0, result.output
        assert captured["state"] == "AZ"
        assert captured["lens"] == "spine"
        assert "[AZ] records=1" in result.output
        assert "mean_quality=2.50" in result.output


# ---------------------------------------------------------------------------
# Fact-correction overlay (issue #249)
# ---------------------------------------------------------------------------


def _write_correction_sidecar(
    cur: Path,
    *,
    state: str = "AZ",
    record_key: str = "AZ|Student|id",
    fact: str = "has_conditional_logic",
    value=True,
    lens: str = "spine",
) -> None:
    """Curation sidecar with one `facts` block (schema v3, issue #249)."""
    cur.mkdir(parents=True, exist_ok=True)
    (cur / f"{state.lower()}.json").write_text(
        json.dumps({
            "version": 3,
            "state": state,
            "updated_at": "2026-07-13T12:00:00+00:00",
            "entries": {
                record_key: {
                    "values": {},
                    "history": [],
                    "facts": {
                        fact: {
                            "value": value,
                            "lens": lens,
                            "author": "Chris Moffatt",
                            "corrected_at": "2026-07-13T12:00:00+00:00",
                            "rationale": "hand-verified against source",
                            "prior_value": None,
                            "prior_provenance": "llm",
                            "plan_version_at_correction":
                                SCORING_PLAN_VERSION,
                        },
                    },
                },
            },
        }),
        encoding="utf-8",
    )


class TestFactCorrectionOverlay:
    """Corrections overlay the pool BEFORE the rule cascade — the
    unchanged rules recompute, the sidecar records `human_corrected`,
    and a tree without corrections stays byte-identical."""

    def _aggregate(self, tmp_path: Path, cur: Path) -> dict:
        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(
            state="AZ", lens="spine", artifacts_dir=tmp_path,
            out_path=out_path, curation_base=cur,
        )
        return json.loads(out_path.read_text(encoding="utf-8"))

    def test_correction_changes_the_cascade_outcome(self, tmp_path):
        """Defaults score nachos tier 0; a corrected
        has_conditional_logic=True re-runs the same cascade into
        tier_1_conditional. The score moved because the INPUT moved —
        no rule was touched."""
        _seed_minimal_fact_pool(tmp_path)
        cur = tmp_path / "curation"
        _write_correction_sidecar(cur)
        payload = self._aggregate(tmp_path, cur)
        rec = payload["scores"][0]
        assert rec["dimensions"]["nachos_score"]["value"] == 1
        assert (
            rec["dimensions"]["nachos_score"]["rule_matched"]
            == "tier_1_conditional"
        )
        prov = rec["fact_provenance"]["has_conditional_logic"]
        assert prov == {
            "value": True,
            "confidence": "high",
            "downgraded": False,
            "downgrade_reason": None,
            "provenance": "human_corrected",
        }

    def test_baseline_without_correction_is_tier_zero(self, tmp_path):
        """Control for the test above: same pool, empty curation dir."""
        _seed_minimal_fact_pool(tmp_path)
        payload = self._aggregate(tmp_path, tmp_path / "curation")
        rec = payload["scores"][0]
        assert rec["dimensions"]["nachos_score"]["value"] == 0
        prov = rec["fact_provenance"]["has_conditional_logic"]
        assert "provenance" not in prov

    def test_corrected_downgraded_fact_clears_review_reason(self, tmp_path):
        """A downgraded fact flags `hallucinated_input:{fact}`; a human
        correction clears the downgrade (the input is now
        human-verified, not a shaky LLM claim)."""
        base_key = "AZ|Student|id"
        _seed_minimal_fact_pool(
            tmp_path,
            overrides={
                "has_conditional_logic": [_row(
                    base_key, "Student", "id", True,
                    confidence="low",
                    downgrade_reason="hallucinated_span",
                )],
            },
        )
        cur = tmp_path / "curation"

        baseline = self._aggregate(tmp_path, tmp_path / "no_curation")
        assert (
            "hallucinated_input:has_conditional_logic"
            in baseline["scores"][0]["review"]["reasons"]
        )

        _write_correction_sidecar(cur, value=False)
        payload = self._aggregate(tmp_path, cur)
        rec = payload["scores"][0]
        assert (
            "hallucinated_input:has_conditional_logic"
            not in rec["review"]["reasons"]
        )
        prov = rec["fact_provenance"]["has_conditional_logic"]
        assert prov["value"] is False
        assert prov["downgraded"] is False
        assert prov["confidence"] == "high"
        assert prov["provenance"] == "human_corrected"

    def test_no_corrections_byte_identical(self, tmp_path):
        """The v28 byte-stability proof: an empty curation dir and a
        sidecar with no `facts` blocks produce byte-identical output."""
        _seed_minimal_fact_pool(tmp_path)
        out_a = tmp_path / "a_scores.json"
        out_b = tmp_path / "b_scores.json"
        empty = tmp_path / "curation_empty"
        no_facts = tmp_path / "curation_no_facts"
        no_facts.mkdir(parents=True)
        (no_facts / "az.json").write_text(
            json.dumps({
                "version": 3, "state": "AZ", "updated_at": None,
                "entries": {"AZ|Student|id": {
                    "values": {"reviewed": {"value": "Yes",
                                            "lens": "spine"}},
                    "history": [],
                }},
            }),
            encoding="utf-8",
        )
        run_aggregate(state="AZ", lens="spine", artifacts_dir=tmp_path,
                      out_path=out_a, curation_base=empty)
        run_aggregate(state="AZ", lens="spine", artifacts_dir=tmp_path,
                      out_path=out_b, curation_base=no_facts)
        a = out_a.read_text(encoding="utf-8")
        b = out_b.read_text(encoding="utf-8")
        # Timestamps differ; normalize scored_at before comparing.
        a_doc, b_doc = json.loads(a), json.loads(b)
        a_doc.pop("scored_at", None)
        b_doc.pop("scored_at", None)
        assert a_doc == b_doc
        assert "human_corrected" not in a

    def test_skip_paths_warn_not_crash(self, tmp_path, caplog):
        """Apply-time is tolerant-but-loud: a hand-edited sidecar with
        an unknown record, a det fact, an invalid value, and a fact not
        in this pool degrades to warnings — `mc publish` must not
        crash on drifted curation."""
        _seed_minimal_fact_pool(tmp_path)
        cur = tmp_path / "curation"
        cur.mkdir(parents=True)
        block = {
            "lens": "spine", "author": "x",
            "corrected_at": "2026-07-13T12:00:00+00:00",
            "rationale": "r", "prior_value": None,
            "prior_provenance": "llm",
            "plan_version_at_correction": SCORING_PLAN_VERSION,
        }
        (cur / "az.json").write_text(
            json.dumps({
                "version": 3, "state": "AZ", "updated_at": None,
                "entries": {
                    "AZ|Gone|record": {"values": {}, "history": [],
                                       "facts": {"has_conditional_logic":
                                                 {**block, "value": True}}},
                    "AZ|Student|id": {"values": {}, "history": [], "facts": {
                        "definition_present": {**block, "value": False},
                        "has_conditional_logic":
                            {**block, "value": "maybe"},
                        "semantic_class":
                            {**block, "value": "divergent"},
                    }},
                },
            }),
            encoding="utf-8",
        )
        import logging

        with caplog.at_level(logging.WARNING, logger="src.score.aggregate"):
            payload = self._aggregate(tmp_path, cur)
        rec = payload["scores"][0]
        # Nothing applied — outcome identical to the no-correction run.
        assert rec["dimensions"]["nachos_score"]["value"] == 0
        assert "human_corrected" not in json.dumps(payload)
        text = caplog.text
        assert "not in the AZ spine pool" in text
        assert "not a correctable LLM fact" in text
        assert "bool fact" in text
        assert "not extracted" in text

    def test_redundant_correction_still_applies(self, tmp_path, caplog):
        """A correction the model now agrees with still applies (the
        overlay is the record of what the human said) and is counted
        redundant in the run log — the 'correction retirable' signal."""
        _seed_minimal_fact_pool(tmp_path)
        cur = tmp_path / "curation"
        _write_correction_sidecar(cur, value=False)  # extracted is False
        import logging

        with caplog.at_level(logging.INFO, logger="src.score.aggregate"):
            payload = self._aggregate(tmp_path, cur)
        rec = payload["scores"][0]
        prov = rec["fact_provenance"]["has_conditional_logic"]
        assert prov["value"] is False
        assert prov["provenance"] == "human_corrected"
        assert rec["dimensions"]["nachos_score"]["value"] == 0
        assert "1 redundant" in caplog.text

    def test_cross_lens_correction_is_inert(self, tmp_path):
        """A source-lens correction never applies to a spine aggregate
        — same-lens-only by design (the spine picks up corrected source
        extension_is_necessary via the sidecar borrow instead)."""
        _seed_minimal_fact_pool(tmp_path)
        cur = tmp_path / "curation"
        _write_correction_sidecar(cur, lens="source")
        payload = self._aggregate(tmp_path, cur)
        rec = payload["scores"][0]
        assert rec["dimensions"]["nachos_score"]["value"] == 0
        assert "provenance" not in rec["fact_provenance"][
            "has_conditional_logic"
        ]

    def test_run_all_threads_curation_base(self, tmp_path):
        _seed_minimal_fact_pool(tmp_path)
        cur = tmp_path / "curation"
        _write_correction_sidecar(cur)
        out = tmp_path / "sidecars"
        out.mkdir()
        run_all_aggregate(
            lens="spine", states=["AZ"], artifacts_dir=tmp_path,
            out_dir=out, curation_base=cur,
        )
        payload = json.loads(
            (out / "az_scores_spine.json").read_text(encoding="utf-8")
        )
        prov = payload["scores"][0]["fact_provenance"][
            "has_conditional_logic"
        ]
        assert prov["provenance"] == "human_corrected"

    def test_borrow_reads_corrected_source_sidecar_value(self, tmp_path):
        """The propagation contract: `_load_source_extension_facts`
        reads `fact_provenance.extension_is_necessary.value` from the
        source sidecar — a human-corrected value there IS the borrowed
        value; provenance does not gate the borrow."""
        from src.score.aggregate import _load_source_extension_facts

        sidecar = tmp_path / "az_scores_source.json"
        sidecar.write_text(
            json.dumps({
                "state": "AZ", "lens": "source",
                "scores": [{
                    "record_key": "AZ|StudentNeed|need",
                    "documentation_source": "source_doc",
                    "fact_provenance": {
                        "extension_is_necessary": {
                            "value": True,
                            "confidence": "high",
                            "downgraded": False,
                            "downgrade_reason": None,
                            "provenance": "human_corrected",
                        },
                    },
                }],
            }),
            encoding="utf-8",
        )
        borrowed = _load_source_extension_facts(
            "AZ", artifacts_dir=tmp_path, allow_stale=True,
            source_sidecar_path=sidecar,
        )
        assert borrowed == {"az|studentneed|need": True}
