"""Phase D cross-state scoring rollup — shape + markdown tests.

Covers:

- ``build_report`` aggregates per-state sidecars into per-state blocks,
  per-format baselines, and cross-state dimension rollup.
- ``render_markdown`` emits the stakeholder summary table.
- ``run()`` dual-writes JSON + MD to the configured out dir.
- CLI wiring (``mc report scoring``) smoke test.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from src.report import scoring as scoring_module
from src.report.scoring import (
    STATE_FORMAT,
    build_report,
    render_markdown,
    run as run_scoring,
)
from src.score.aggregate import SCORING_PLAN_VERSION


def _write_sidecar(
    tmp_path: Path,
    state: str,
    lens: str,
    *,
    mean_quality: float | None,
    needs_review: int,
    record_count: int = 100,
    dim_means: dict[str, float] | None = None,
    reasons: dict[str, int] | None = None,
    in_scope_count: int = 0,
    nachos_hist: dict[str, int] | None = None,
    adjusted_hist: dict[str, int] | None = None,
    mean_nachos: float | None = None,
    mean_adjusted: float | None = None,
    plan_version: str = SCORING_PLAN_VERSION,
) -> Path:
    """Write a minimal sidecar matching ``aggregate.run()`` output shape."""
    dim_stats = {
        name: {"count": record_count, "mean": mean, "distribution": {"0": 0, "1": 0, "2": 0, "3": 0}}
        for name, mean in (dim_means or {}).items()
    }
    payload = {
        "state": state,
        "lens": lens,
        "edfi_version": "4.0.0",
        "scoring_plan_version": plan_version,
        "record_count": record_count,
        "scored_count": record_count,
        "mean_quality_score": mean_quality,
        "needs_review_count": needs_review,
        "dimension_stats": dim_stats,
        # Phase F header aggregates.
        "in_scope_count": in_scope_count,
        "nachos_score_histogram": nachos_hist or {"0": 0, "1": 0, "2": 0, "3": 0},
        "adjusted_nachos_score_histogram": adjusted_hist or {},
        "mean_nachos_score": mean_nachos,
        "mean_adjusted_nachos_score": mean_adjusted,
        "scores": [
            {
                "record_key": f"{state}|X|a",
                "_quality_mean_diagnostic": mean_quality,
                "complexity_score": 0 if lens == "spine" else None,
                "review": {
                    "needs_review": bool(reasons),
                    "reasons": sorted([r for r, n in (reasons or {}).items() for _ in range(n)]),
                    "route": None,
                },
                "in_scope": False,
                "adjusted_nachos_score": None,
                "nachos_justification": None,
            }
        ],
    }
    path = tmp_path / f"{state.lower()}_scores_{lens}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestBuildReport:
    def test_per_state_blocks_populated(self, tmp_path: Path) -> None:
        for st, mean, review in [
            ("AZ", 2.0, 10), ("WI", 2.5, 5), ("MN", 1.8, 8), ("TX", 2.1, 15), ("IN", 2.3, 6),
        ]:
            _write_sidecar(
                tmp_path,
                st,
                "source",
                mean_quality=mean,
                needs_review=review,
                dim_means={
                    "canonical_name_alignment": 1.5,
                    "definition_quality": 2.0,
                    "semantic_fidelity": 2.5,
                    "extension_justification": 1.2,
                },
            )
        report = build_report("source", base=tmp_path)
        assert report["lens"] == "source"
        assert len(report["per_state"]) == 5
        state_names = [b["state"] for b in report["per_state"]]
        assert state_names == ["AZ", "WI", "MN", "TX", "IN"]
        # Format axis attached.
        for b in report["per_state"]:
            assert b["source_format"] == STATE_FORMAT[b["state"]]
        # Cross-state mean averages per-state means (not per-record).
        assert report["cross_state"]["cross_state_mean_quality"] == pytest.approx(
            (2.0 + 2.5 + 1.8 + 2.1 + 2.3) / 5, abs=1e-4
        )
        # Review total aggregates flags.
        assert report["cross_state"]["review_total"] == 10 + 5 + 8 + 15 + 6

    def test_per_format_baselines_grouped_correctly(self, tmp_path: Path) -> None:
        for st in ("AZ", "WI", "MN", "TX", "IN"):
            _write_sidecar(
                tmp_path,
                st,
                "source",
                mean_quality=2.0,
                needs_review=1,
                dim_means={},
            )
        report = build_report("source", base=tmp_path)
        baselines = report["per_format_baselines"]
        assert set(baselines) == {"xlsx", "confluence", "mapping_matrix", "tweds"}
        # IN joins the `xlsx` cohort with AZ; other formats stay at 1 member.
        for fmt, info in baselines.items():
            expected = 2 if fmt == "xlsx" else 1
            assert info["member_count"] == expected, f"{fmt}: {info['member_count']}"

    def test_review_reason_histogram_sums_across_rows(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "AZ",
            "source",
            mean_quality=2.0,
            needs_review=1,
            reasons={"hallucinated_input:semantic_class": 1, "low_confidence_dimension:semantic_fidelity": 1},
            dim_means={},
        )
        for st in ("WI", "MN", "TX", "IN"):
            _write_sidecar(tmp_path, st, "source", mean_quality=2.0, needs_review=0, dim_means={})
        report = build_report("source", base=tmp_path)
        az = next(b for b in report["per_state"] if b["state"] == "AZ")
        assert az["review_reason_histogram"] == {
            "hallucinated_input:semantic_class": 1,
            "low_confidence_dimension:semantic_fidelity": 1,
        }


class TestRenderMarkdown:
    def test_markdown_contains_all_states_and_guidance(self, tmp_path: Path) -> None:
        for st in ("AZ", "WI", "MN", "TX", "IN"):
            _write_sidecar(
                tmp_path,
                st,
                "source",
                mean_quality=2.0,
                needs_review=1,
                dim_means={
                    "canonical_name_alignment": 1.5,
                    "definition_quality": 2.0,
                    "semantic_fidelity": 2.5,
                    "extension_justification": 1.2,
                },
            )
        report = build_report("source", base=tmp_path)
        md = render_markdown(report)
        for st in ("AZ", "WI", "MN", "TX", "IN"):
            assert st in md
        assert "Per-format baselines" in md
        assert "Cross-format comparisons require interpretation" in md
        # Every dimension name appears in the table.
        for dim in ("canonical_name_alignment", "definition_quality", "semantic_fidelity"):
            assert dim in md


class TestRun:
    def test_run_writes_json_and_md(self, tmp_path: Path) -> None:
        for st in ("AZ", "WI", "MN", "TX", "IN"):
            _write_sidecar(
                tmp_path, st, "spine", mean_quality=2.0, needs_review=0,
                dim_means={
                    "documentation_completeness": 2.0,
                    "obligation_clarity": 1.5,
                    "business_logic_complexity": 0.5,
                },
            )
        report = run_scoring(lens="spine", out=tmp_path)
        json_path = tmp_path / "scoring_report_spine.json"
        md_path = tmp_path / "scoring_report_spine.md"
        assert json_path.exists()
        assert md_path.exists()
        on_disk = json.loads(json_path.read_text(encoding="utf-8"))
        assert on_disk["lens"] == "spine"
        assert on_disk == report


class TestPhaseFNachosSection:
    """Phase F — ``## NACHOS`` section in rollup MD + NACHOS block in JSON."""

    def test_markdown_contains_nachos_section(self, tmp_path: Path) -> None:
        for st in ("AZ", "WI", "MN", "TX", "IN"):
            _write_sidecar(
                tmp_path,
                st,
                "spine",
                mean_quality=2.0,
                needs_review=1,
                in_scope_count=10,
                nachos_hist={"0": 2, "1": 3, "2": 3, "3": 2},
                mean_nachos=1.5,
                mean_adjusted=1.8,
                dim_means={},
            )
        report = build_report("spine", base=tmp_path)
        md = render_markdown(report)
        assert "## NACHOS" in md
        assert "Per-state NACHOS" in md
        assert "Per-format NACHOS baselines" in md
        assert "Cross-state NACHOS aggregate" in md
        assert "NACHOS tier histogram" in md
        assert "Adjusted NACHOS histogram" in md
        # Cross-state weighted mean surfaces.
        assert "Cross-state mean NACHOS" in md

    def test_json_carries_nachos_rollup(self, tmp_path: Path) -> None:
        for st in ("AZ", "WI", "MN", "TX", "IN"):
            _write_sidecar(
                tmp_path,
                st,
                "spine",
                mean_quality=2.0,
                needs_review=0,
                in_scope_count=20,
                nachos_hist={"0": 5, "1": 5, "2": 5, "3": 5},
                mean_nachos=1.5,
                mean_adjusted=2.0,
                dim_means={},
            )
        report = build_report("spine", base=tmp_path)
        nachos = report["nachos"]
        # 5 states × 20 = 100 in-scope.
        assert nachos["in_scope_total"] == 100
        # Cross-state mean weighted by in-scope: all states 20, mean 1.5.
        assert nachos["cross_state_mean_nachos_score"] == pytest.approx(1.5, abs=1e-4)
        # Tier histogram sums across states.
        assert nachos["nachos_tier_histogram"] == {
            "0": 25, "1": 25, "2": 25, "3": 25,
        }
        # Per-format rollup groups by STATE_FORMAT.
        assert set(nachos["per_format"]) == {
            "xlsx", "confluence", "mapping_matrix", "tweds",
        }

    def test_scoring_plan_version_echoes_sidecar_consensus(
        self, tmp_path: Path
    ) -> None:
        """The stamp is the consensus of the summarized sidecars — never a
        literal (issue #211 item 1b: a hardcoded "2" survived 25 version
        bumps because the old test pinned the literal). The fixture stamps
        the live ``SCORING_PLAN_VERSION``, so this test drifts WITH the
        constant instead of against it.
        """
        for st in ("AZ", "WI", "MN", "TX", "IN"):
            _write_sidecar(
                tmp_path, st, "spine", mean_quality=2.0, needs_review=0,
                dim_means={},
            )
        report = build_report("spine", base=tmp_path)
        assert report["scoring_plan_version"] == SCORING_PLAN_VERSION

    def test_scoring_plan_version_disagreement_yields_none(
        self, tmp_path: Path
    ) -> None:
        """Mixed-version sidecars → None, never a guess (mirrors
        ``reviewer_comparison_summary._detect_plan_version``)."""
        for st, ver in [
            ("AZ", SCORING_PLAN_VERSION), ("WI", "1"), ("MN", "1"),
            ("TX", "1"), ("IN", "1"),
        ]:
            _write_sidecar(
                tmp_path, st, "spine", mean_quality=2.0, needs_review=0,
                dim_means={}, plan_version=ver,
            )
        report = build_report("spine", base=tmp_path)
        assert report["scoring_plan_version"] is None


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self) -> None:
        """`run()` must stay plain (POC-2 pitfall)."""
        import inspect

        assert not isinstance(scoring_module.run, click.Command)
        assert inspect.isfunction(scoring_module.run)

    def test_cli_command_invokes_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mc report scoring --lens spine` calls ``run_scoring`` with the
        right lens and prints the summary."""
        captured: dict = {}

        def _fake_run(*, lens: str, states=None, out=None, allow_stale=False):
            captured["lens"] = lens
            return {
                "generated_on": "2026-04-21",
                "lens": lens,
                "dimensions": [],
                "per_state": [],
                "per_format_baselines": {},
                "cross_state": {
                    "state_count": 2,
                    "record_total": 100,
                    "review_total": 0,
                    "cross_state_mean_quality": 2.0,
                    "cross_state_mean_quality_range": 0.0,
                    "dimension_rollup": {},
                },
                "guidance": "",
            }

        monkeypatch.setattr(scoring_module, "run", _fake_run)
        from src.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["report", "scoring", "--lens", "spine", "--state", "AZ"])
        assert result.exit_code == 0, result.output
        assert captured["lens"] == "spine"
        assert "scoring rollup (spine)" in result.output
