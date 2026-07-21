"""Phase D review-queue routing — first-principles priority tests.

Covers:

- ``route_review`` priority ladder (DATA_MODEL > POLICY > SCORING > ANALYST).
- Empty-reasons returns None.
- ``build_queue`` aggregates across states and keeps per-state counts.
- ``render_markdown`` emits route totals + per-state table.
- ``run()`` dual-writes JSON + MD.
- CLI wiring.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from src.report import review_queue as rq_module
from src.report.review_queue import (
    ROUTES,
    build_queue,
    render_markdown,
    route_review,
    run as run_queue,
)


class TestRouteReview:
    def test_empty_reasons_returns_none(self) -> None:
        assert route_review([]) is None

    def test_inter_dim_inconsistency_routes_data_model(self) -> None:
        assert route_review(["inter_dim_inconsistency:docs_missing_but_logic_complex"]) == "DATA_MODEL"

    def test_data_model_wins_over_hallucination_and_confidence(self) -> None:
        """Priority: DATA_MODEL > POLICY > SCORING > ANALYST."""
        assert route_review([
            "inter_dim_inconsistency:x",
            "hallucinated_input:a",
            "hallucinated_input:b",
            "low_confidence_dimension:c",
        ]) == "DATA_MODEL"

    def test_two_plus_hallucinations_route_policy(self) -> None:
        assert route_review([
            "hallucinated_input:semantic_class",
            "hallucinated_input:state_scope_delta",
        ]) == "POLICY"

    def test_single_hallucination_routes_scoring(self) -> None:
        assert route_review(["hallucinated_input:semantic_class"]) == "SCORING"

    def test_low_confidence_only_routes_analyst(self) -> None:
        assert route_review(["low_confidence_dimension:semantic_fidelity"]) == "ANALYST"

    def test_unknown_reason_defaults_analyst(self) -> None:
        assert route_review(["something_we_didnt_anticipate:x"]) == "ANALYST"


def _write_sidecar(
    tmp_path: Path,
    state: str,
    lens: str,
    records: list[dict],
) -> None:
    payload = {
        "state": state,
        "lens": lens,
        "record_count": len(records),
        "scored_count": len(records),
        "needs_review_count": sum(
            1 for r in records if r["review"]["needs_review"]
        ),
        "scores": records,
    }
    (tmp_path / f"{state.lower()}_scores_{lens}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _flagged(
    record_key: str,
    entity: str,
    element_name: str,
    reasons: list[str],
    *,
    per_record: float | None = 1.5,
) -> dict:
    return {
        "record_key": record_key,
        "entity": entity,
        "element_name": element_name,
        "_quality_mean_diagnostic": per_record,
        "complexity_score": None,
        "confidence_composite": "low",
        "review": {
            "needs_review": bool(reasons),
            "reasons": sorted(set(reasons)),
            "route": route_review(sorted(set(reasons))),
        },
    }


class TestBuildQueue:
    def test_per_state_counts_by_route(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "AZ",
            "source",
            [
                _flagged("AZ|E|a", "E", "a", ["inter_dim_inconsistency:x"]),
                _flagged(
                    "AZ|E|b",
                    "E",
                    "b",
                    ["hallucinated_input:f1", "hallucinated_input:f2"],
                ),
                _flagged("AZ|E|c", "E", "c", ["hallucinated_input:f1"]),
                _flagged("AZ|E|d", "E", "d", ["low_confidence_dimension:dq"]),
                _flagged("AZ|E|e", "E", "e", []),  # not flagged — should not appear
            ],
        )
        for st in ("WI", "MN", "TX", "IN"):
            _write_sidecar(tmp_path, st, "source", [_flagged(f"{st}|X|y", "X", "y", [])])

        queue = build_queue("source", base=tmp_path)
        counts = queue["per_state"]["AZ"]["by_route"]
        assert counts["DATA_MODEL"] == 1
        assert counts["POLICY"] == 1
        assert counts["SCORING"] == 1
        assert counts["ANALYST"] == 1
        # Unflagged record never appears in the queue.
        assert queue["per_state"]["AZ"]["total"] == 4

    def test_entries_sorted_by_route_priority(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "AZ",
            "source",
            [
                _flagged("AZ|E|a", "E", "a", ["low_confidence_dimension:x"], per_record=2.0),
                _flagged("AZ|E|b", "E", "b", ["inter_dim_inconsistency:x"], per_record=2.0),
                _flagged("AZ|E|c", "E", "c", ["hallucinated_input:f1"], per_record=2.0),
            ],
        )
        for st in ("WI", "MN", "TX", "IN"):
            _write_sidecar(tmp_path, st, "source", [])
        queue = build_queue("source", base=tmp_path)
        routes = [e["route"] for e in queue["entries"]]
        # Priority ladder: DATA_MODEL first, then SCORING, then ANALYST.
        priority_index = [ROUTES.index(r) for r in routes]
        assert priority_index == sorted(priority_index)


class TestPhaseFNachosLowConfidenceHighTier:
    """Phase F trigger: in-scope tier >=3 + low confidence → SCORING.

    The trigger is emitted by aggregate.py's `_build_review_block`
    rather than by route_review (route_review categorizes reasons, it
    doesn't invent them). The review-queue test verifies that a record
    carrying the reason routes to SCORING."""

    def test_trigger_routes_to_scoring(self) -> None:
        """Plan §7: nachos_low_confidence_high_tier → SCORING bucket."""
        assert route_review(["nachos_low_confidence_high_tier"]) == "SCORING"

    def test_trigger_coexists_with_other_signals(self) -> None:
        """DATA_MODEL still wins over Phase F trigger (structural issue
        trumps the tier-confidence escalation). POLICY (2+ hallucinated)
        also wins."""
        assert route_review([
            "inter_dim_inconsistency:x",
            "nachos_low_confidence_high_tier",
        ]) == "DATA_MODEL"
        assert route_review([
            "hallucinated_input:a",
            "hallucinated_input:b",
            "nachos_low_confidence_high_tier",
        ]) == "POLICY"
        # Single hallucinated_input + Phase F trigger both route SCORING;
        # first-match-wins lands on hallucinated_input (documented trigger).
        assert route_review([
            "hallucinated_input:has_concatenation",
            "nachos_low_confidence_high_tier",
        ]) == "SCORING"

    def test_trigger_appears_in_queue_at_scoring(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "AZ",
            "spine",
            [
                _flagged(
                    "AZ|E|a",
                    "E",
                    "a",
                    ["nachos_low_confidence_high_tier"],
                ),
            ],
        )
        for st in ("WI", "MN", "TX", "IN"):
            _write_sidecar(tmp_path, st, "spine", [])
        queue = build_queue("spine", base=tmp_path)
        routes = [e["route"] for e in queue["entries"]]
        assert "SCORING" in routes

    def test_out_of_scope_row_does_not_trip_trigger(self, tmp_path: Path) -> None:
        """The aggregate only emits nachos_low_confidence_high_tier for
        in-scope rows (plan §7). Out-of-scope rows are skipped. This
        test verifies the contract at the `_flagged`-constructed level:
        a sidecar row with in_scope=False and no Phase F reason in its
        reasons list doesn't appear with a Phase F route."""
        # Flagged only for low_confidence (routes ANALYST), not Phase F.
        _write_sidecar(
            tmp_path,
            "AZ",
            "spine",
            [_flagged("AZ|E|a", "E", "a", ["low_confidence_dimension:x"])],
        )
        for st in ("WI", "MN", "TX", "IN"):
            _write_sidecar(tmp_path, st, "spine", [])
        queue = build_queue("spine", base=tmp_path)
        routes = [e["route"] for e in queue["entries"]]
        assert routes == ["ANALYST"]


class TestRenderMarkdown:
    def test_markdown_has_route_totals_and_states(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "AZ",
            "source",
            [_flagged("AZ|E|a", "E", "a", ["hallucinated_input:f"])],
        )
        for st in ("WI", "MN", "TX", "IN"):
            _write_sidecar(tmp_path, st, "source", [])
        queue = build_queue("source", base=tmp_path)
        md = render_markdown(queue, top_n=5)
        for route in ROUTES:
            assert route in md
        for st in ("AZ", "WI", "MN", "TX", "IN"):
            assert f"| {st} |" in md


class TestRun:
    def test_run_writes_json_and_md(self, tmp_path: Path) -> None:
        for st in ("AZ", "WI", "MN", "TX", "IN"):
            _write_sidecar(
                tmp_path,
                st,
                "spine",
                [_flagged(f"{st}|E|a", "E", "a", ["low_confidence_dimension:dc"])],
            )
        queue = run_queue(lens="spine", out=tmp_path)
        assert (tmp_path / "review_queue_spine.json").exists()
        assert (tmp_path / "review_queue_spine.md").exists()
        on_disk = json.loads((tmp_path / "review_queue_spine.json").read_text(encoding="utf-8"))
        assert on_disk["lens"] == "spine"
        assert on_disk == queue


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self) -> None:
        import inspect

        assert not isinstance(rq_module.run, click.Command)
        assert inspect.isfunction(rq_module.run)

    def test_cli_invokes_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}

        def _fake_run(
            *, lens: str, states=None, out=None, top_n: int = 30,
            allow_stale=False,
        ):
            captured["lens"] = lens
            captured["top_n"] = top_n
            return {
                "generated_on": "2026-04-21",
                "lens": lens,
                "route_priority": list(ROUTES),
                "route_totals": {"POLICY": 1, "DATA_MODEL": 0, "SCORING": 2, "ANALYST": 5},
                "per_state": {},
                "entries": [],
                "guidance": "",
            }

        monkeypatch.setattr(rq_module, "run", _fake_run)
        from src.cli import cli

        runner = CliRunner()
        result = runner.invoke(
            cli, ["report", "review-queue", "--lens", "spine", "--top-n", "5"]
        )
        assert result.exit_code == 0, result.output
        assert captured["lens"] == "spine"
        assert captured["top_n"] == 5
        assert "review queue (spine)" in result.output
