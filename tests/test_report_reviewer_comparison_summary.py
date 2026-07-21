"""Tests for ``src/report/reviewer_comparison_summary.py``.

Verifies the doc generator that powers ``mc report
reviewer-comparison``:

- ``build_summary`` rolls up the right shape from review_digest_*.json.
- ``_gap_row_stats`` segments the gap_row_match bucket correctly.
- ``render_markdown`` renders the headline / classification / gap /
  top-patterns sections without losing field data.
- ``run`` writes a stable doc to a tmp_path output, includes the
  detected ``scoring_plan_version`` in the header, and degrades
  gracefully when sidecars are missing.
- CLI wiring: ``mc report reviewer-comparison`` invokes
  ``run_summary``.

Hermetic — no real sidecar data is read, all inputs are fabricated
under tmp_path.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from src.report import reviewer_comparison_summary as rcs


# ---------------------------------------------------------------------------
# Fixtures — minimal review_digest payloads + sidecars
# ---------------------------------------------------------------------------


def _digest_payload(
    *,
    lens: str,
    classification_counts: dict[str, int] | None = None,
    per_state: dict[str, dict[str, Any]] | None = None,
    rows: list[dict[str, Any]] | None = None,
    top_patterns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": "2026-04-29T00:00:00Z",
        "lens": lens,
        "states": list((per_state or {}).keys()),
        "overall": {
            "reviewer_rows": sum(
                (st.get("reviewer_rows") or 0) for st in (per_state or {}).values()
            ),
            "matched": sum(
                (st.get("matched") or 0) for st in (per_state or {}).values()
            ),
            "match_pct": 0.0,
            "classification_counts": classification_counts or {},
        },
        "per_state": per_state or {},
        "top_patterns": top_patterns or [],
        "worked_examples": [],
        "rows": rows or [],
    }


def _state_payload(
    *,
    reviewer_rows: int,
    matched: int,
    classification_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "reviewer_rows": reviewer_rows,
        "matched": matched,
        "match_pct": round(100 * matched / reviewer_rows, 1) if reviewer_rows else 0.0,
        "classification_counts": classification_counts or {},
    }


def _row(
    *,
    state: str = "WI",
    classification: str = "match_exact",
    mc_tier: int | None = 0,
    tier_delta: int | None = 0,
) -> dict[str, Any]:
    return {
        "state": state,
        "classification": classification,
        "mc_tier": mc_tier,
        "tier_delta": tier_delta,
    }


def _write_digest(
    base: Path, lens: str, payload: dict[str, Any]
) -> Path:
    target = base / f"review_digest_{lens}.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _write_sidecar(base: Path, state: str, lens: str, version: str) -> Path:
    target = base / f"{state.lower()}_scores_{lens}.json"
    target.write_text(
        json.dumps(
            {
                "state": state,
                "lens": lens,
                "scoring_plan_version": version,
                "scored_count": 1,
                "scores": [],
            }
        ),
        encoding="utf-8",
    )
    return target


# ---------------------------------------------------------------------------
# _gap_row_stats
# ---------------------------------------------------------------------------


class TestGapRowStats:
    def test_segments_total_populated_and_delta_distribution(self) -> None:
        rows = [
            _row(state="WI", classification="gap_row_match", mc_tier=0, tier_delta=0),
            _row(state="MN", classification="gap_row_match", mc_tier=1, tier_delta=2),
            _row(state="MN", classification="gap_row_match", mc_tier=0, tier_delta=-1),
            # No tier populated — counts toward total but not abs-delta.
            _row(state="TX", classification="gap_row_match", mc_tier=None, tier_delta=None),
            # Different bucket — excluded.
            _row(state="WI", classification="match_exact"),
        ]
        stats = rcs._gap_row_stats(rows)
        assert stats["total"] == 4
        assert stats["populated_tier"] == 3
        assert stats["abs_tier_delta_dist"] == {0: 1, 1: 1, 2: 1}
        assert stats["by_state"] == {"WI": 1, "MN": 2, "TX": 1}

    def test_agree_exact_prefers_digest_subbucket(self) -> None:
        # 2026-07 hygiene — digests stamp gap_score_bucket per gap row;
        # rows without the field (older JSONs) fall back to the
        # tier_delta + adj_delta recompute.
        rows = [
            {**_row(classification="gap_row_match", mc_tier=0, tier_delta=0),
             "gap_score_bucket": "match_exact"},
            {**_row(classification="gap_row_match", mc_tier=0, tier_delta=0),
             "gap_score_bucket": "match_tier"},
            # Fallback path: no bucket field, exact agreement by deltas.
            {**_row(classification="gap_row_match", mc_tier=0, tier_delta=0),
             "adj_delta": 0.0},
            # Fallback path: tier agrees, adjustment doesn't.
            {**_row(classification="gap_row_match", mc_tier=0, tier_delta=0),
             "adj_delta": 0.5},
        ]
        stats = rcs._gap_row_stats(rows)
        assert stats["agree_exact"] == 2

    def test_empty_rows_returns_zero_stats(self) -> None:
        stats = rcs._gap_row_stats([])
        assert stats == {
            "total": 0,
            "populated_tier": 0,
            "agree_exact": 0,
            "abs_tier_delta_dist": {},
            "by_state": {},
        }


# ---------------------------------------------------------------------------
# build_summary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    def test_rolls_up_per_lens_with_top_level_metadata(self) -> None:
        digest_source = _digest_payload(
            lens="source",
            classification_counts={"match_exact": 10, "gap_row_match": 5},
            per_state={
                "WI": _state_payload(
                    reviewer_rows=10,
                    matched=8,
                    classification_counts={"match_exact": 6, "gap_row_match": 2},
                ),
            },
            rows=[
                _row(classification="gap_row_match", mc_tier=0, tier_delta=0),
                _row(classification="match_exact"),
            ],
            top_patterns=[
                {
                    "signature": {
                        "state": "WI",
                        "classification": "match_tier",
                        "reviewer_tier": 0,
                        "mc_tier": 0,
                    },
                    "count": 3,
                    "weight": 3,
                    "example": {"entity": "E", "element": "x"},
                }
            ],
        )
        digest_spine = _digest_payload(lens="spine", per_state={"WI": _state_payload(reviewer_rows=10, matched=7)})
        summary = rcs.build_summary(
            {"source": digest_source, "spine": digest_spine},
            plan_version="17",
            refresh_date="2026-04-29",
        )
        assert summary["scoring_plan_version"] == "17"
        assert summary["refresh_date"] == "2026-04-29"
        assert summary["lenses"] == ["source", "spine"]
        per_lens_source = summary["per_lens"]["source"]
        assert per_lens_source["matched"] == 8
        assert per_lens_source["per_state"]["WI"]["match_pct"] == 80.0
        assert per_lens_source["gap_row_match"]["total"] == 1
        assert per_lens_source["gap_row_match"]["populated_tier"] == 1
        assert per_lens_source["top_patterns"][0]["count"] == 3


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


class TestRenderMarkdown:
    def _summary(self) -> dict[str, Any]:
        return rcs.build_summary(
            {
                "source": _digest_payload(
                    lens="source",
                    classification_counts={
                        "match_exact": 10, "match_tier": 1, "tier_delta_1": 0,
                        "tier_delta_ge2": 0, "key_sever_override": 0,
                        "gap_row_match": 2, "no_mc_row": 0, "no_reviewer_row": 0,
                    },
                    per_state={
                        "WI": _state_payload(
                            reviewer_rows=13,
                            matched=11,
                            classification_counts={
                                "match_exact": 10, "match_tier": 1,
                                "gap_row_match": 2,
                            },
                        ),
                    },
                    rows=[
                        _row(classification="gap_row_match", mc_tier=0, tier_delta=0),
                        _row(classification="gap_row_match", mc_tier=1, tier_delta=1),
                    ],
                    top_patterns=[
                        {
                            "signature": {
                                "state": "WI",
                                "classification": "tier_delta_1",
                                "reviewer_tier": 0,
                                "mc_tier": 1,
                            },
                            "count": 4,
                            "example": {"entity": "Calendar", "element": "code"},
                        },
                    ],
                ),
                "spine": _digest_payload(lens="spine", per_state={}),
            },
            plan_version="17",
            refresh_date="2026-04-29",
        )

    def test_header_carries_plan_version_and_date(self) -> None:
        md = rcs.render_markdown(self._summary())
        assert "`scoring_plan_version: 17`" in md
        assert "**Refresh date:** 2026-04-29" in md

    def test_classification_breakdown_includes_gap_row_match_column(self) -> None:
        md = rcs.render_markdown(self._summary())
        # Header carries the gap_row_match column …
        assert "gap_row_match" in md
        # … and the WI row reports 2 in that column.
        wi_lines = [line for line in md.splitlines() if line.startswith("| WI |")]
        assert wi_lines, "expected a WI row in the classification table"
        # Bucket order from _BUCKET_ORDER puts gap_row_match in position 6.
        cells = [c.strip() for c in wi_lines[0].split("|")[1:-1]]
        assert cells[0] == "WI"
        # match_exact, match_tier, tier_delta_1, tier_delta_ge2,
        # key_sever_override, gap_row_match, no_mc_row, no_reviewer_row
        assert cells[6] == "2"  # gap_row_match value

    def test_gap_cohort_section_renders_distribution(self) -> None:
        md = rcs.render_markdown(self._summary())
        assert "## Gap-row match cohort (Layer 3 detail)" in md
        # Distribution: |Δ|=0 → 1, |Δ|=1 → 1, |Δ|=2 → 0, |Δ|=3 → 0.
        gap_section = md.split("## Gap-row match cohort")[1].split("##")[0]
        # source lens row carries Total=2, Populated=2, Agree exactly=0
        # (fixture rows carry no adj_delta/gap_score_bucket), |Δ|=0=1,
        # |Δ|=1=1.
        assert "| source | 2 | 2 | 0 | 1 | 1 | 0 | 0 |" in gap_section

    def test_top_patterns_section_renders_when_patterns_present(self) -> None:
        md = rcs.render_markdown(self._summary())
        assert "## Top divergence patterns — source lens" in md
        assert "`Calendar\\|code`" in md

    def test_editorial_pointer_present(self) -> None:
        md = rcs.render_markdown(self._summary())
        assert "docs/recommendations.md" in md
        assert "docs/archive/reviewer-comparison-2026-04-28.md" in md

    def test_unknown_plan_version_renders_explicit_label(self) -> None:
        summary = self._summary()
        summary["scoring_plan_version"] = None
        md = rcs.render_markdown(summary)
        assert "`scoring_plan_version: unknown`" in md


# ---------------------------------------------------------------------------
# run() — end-to-end with hermetic tmp_path
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_writes_doc_with_detected_plan_version(self, tmp_path: Path) -> None:
        # Fabricate review digests for both lenses.
        for lens in ("source", "spine"):
            _write_digest(
                tmp_path,
                lens,
                _digest_payload(
                    lens=lens,
                    classification_counts={"match_exact": 1},
                    per_state={
                        "WI": _state_payload(
                            reviewer_rows=1, matched=1,
                            classification_counts={"match_exact": 1},
                        ),
                    },
                    rows=[],
                ),
            )
        # Plant a sidecar with plan version 17 so the detector picks it up.
        # ``_detect_plan_version`` reads from ``state_scores_path`` (data/out)
        # which we monkeypatch via the tmp_path layout.

        # The simplest approach: monkeypatch the helper.
        # (Alternative: write the sidecar to data/out — but that pollutes
        # the working tree, so monkeypatch is preferable for tests.)
        out_path = tmp_path / "reviewer-comparison.md"
        # Use a direct call — hand the digests via base override.
        original_detect = rcs._detect_plan_version
        try:
            rcs._detect_plan_version = lambda states, lens: "17"  # type: ignore[assignment]
            summary = rcs.run(out_path=out_path, base=tmp_path)
        finally:
            rcs._detect_plan_version = original_detect  # type: ignore[assignment]
        assert out_path.exists()
        md = out_path.read_text()
        assert "scoring_plan_version: 17" in md
        assert summary["scoring_plan_version"] == "17"
        assert "source" in summary["per_lens"]
        assert "spine" in summary["per_lens"]

    def test_run_raises_when_digest_missing(self, tmp_path: Path) -> None:
        # Only source digest planted — spine missing.
        _write_digest(
            tmp_path, "source",
            _digest_payload(lens="source", per_state={}),
        )
        with pytest.raises(FileNotFoundError, match="review_digest_spine"):
            rcs.run(out_path=tmp_path / "doc.md", base=tmp_path)

    def test_run_renders_unknown_plan_version_when_sidecars_missing(
        self, tmp_path: Path
    ) -> None:
        for lens in ("source", "spine"):
            _write_digest(
                tmp_path, lens,
                _digest_payload(lens=lens, per_state={}),
            )
        out_path = tmp_path / "reviewer-comparison.md"
        # No sidecars planted; _detect_plan_version returns None →
        # render_markdown emits ``unknown`` in the header.
        original_detect = rcs._detect_plan_version
        try:
            rcs._detect_plan_version = lambda states, lens: None  # type: ignore[assignment]
            rcs.run(out_path=out_path, base=tmp_path)
        finally:
            rcs._detect_plan_version = original_detect  # type: ignore[assignment]
        md = out_path.read_text()
        assert "scoring_plan_version: unknown" in md


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self) -> None:
        assert not isinstance(rcs.run, click.Command)
        assert inspect.isfunction(rcs.run)

    def test_cli_invokes_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_run(*, out_path: Path | None = None, base: Path | None = None,
                       states_for_version_check=("WI", "MN", "TX")):
            captured["out_path"] = out_path
            captured["base"] = base
            return {
                "scoring_plan_version": "17",
                "refresh_date": "2026-04-29",
                "lenses": ["source", "spine"],
                "per_lens": {},
            }

        monkeypatch.setattr(rcs, "run", _fake_run)
        from src.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["report", "reviewer-comparison"])
        assert result.exit_code == 0, result.output
        assert "scoring_plan_version=17" in result.output
        assert "lenses=source, spine" in result.output
