"""Phase E — reviewer digest report tests.

Fabricated rows exercise pattern ranking, response-category
heuristics, worked-example selection, and MD rendering. CLI wiring
gets a basic smoke test via the underlying ``run()`` helper with tmp
sidecar overrides.
"""

from __future__ import annotations

import json

import pytest

from src.report.review_digest import (
    _suggested_response_category,
    build_digest,
    comparison_row_to_dict,
    coverage_patterns,
    pick_worked_examples,
    render_digest_md,
    run,
    top_divergence_patterns,
)
from src.score.review_comparison import ComparisonRow


def _row(
    classification: str,
    state: str = "WI",
    reviewer_tier: int | None = 0,
    mc_tier: int | None = 0,
    reviewer_adj: float | None = 0.0,
    mc_adj: float | None = 0.0,
    tier_delta: int | None = None,
    reviewer_justification: str | None = None,
    mc_justification: str | None = None,
    reviewer_entity: str | None = "Calendar",
    reviewer_element: str | None = "calendarCode",
    mc_record_key: str | None = "WI|Calendar|calendarCode",
    mc_in_scope: bool | None = False,
) -> ComparisonRow:
    return ComparisonRow(
        state=state,
        lens="source",
        classification=classification,  # type: ignore[arg-type]
        reviewer_entity=reviewer_entity,
        reviewer_element=reviewer_element,
        reviewer_tier=reviewer_tier,
        reviewer_adj=reviewer_adj,
        reviewer_justification=reviewer_justification,
        mc_record_key=mc_record_key,
        mc_tier=mc_tier,
        mc_adj=mc_adj,
        mc_justification=mc_justification,
        mc_in_scope=mc_in_scope,
        tier_delta=tier_delta,
        adj_delta=None,
    )


# ---------------------------------------------------------------------------
# Suggested response heuristic
# ---------------------------------------------------------------------------


def test_response_category_key_sever_wins():
    row = _row(
        "key_sever_override",
        reviewer_justification="Severed by key change.",
    )
    assert _suggested_response_category(row) == "rubric_judgment_call"


def test_response_category_extension_keyword_triggers_prompt_tweak():
    # v10 — `in_scope_mismatch` retired; `tier_delta_1` exercises the
    # same prompt-tweak heuristic when the reviewer flags an extension.
    row = _row(
        "tier_delta_1",
        reviewer_justification="1 Unnecessary extension",
    )
    assert _suggested_response_category(row) == "prompt_tweak"


def test_response_category_defaults_to_rule_adjustment():
    row = _row(
        "tier_delta_1",
        reviewer_justification="Cross entity",
        mc_justification="tier_1_conditional",
    )
    assert _suggested_response_category(row) == "rule_adjustment"


def test_response_category_necessity_conflict_is_rubric_call():
    # 2026-07 hygiene — the 320-vs-5 necessity split is a definitional
    # disagreement (state-mandated ⇒ necessary vs could-core-represent-
    # it), not a prompt defect: route to the rubric session, don't
    # suggest tuning toward either observer.
    row = _row(
        "match_tier",
        reviewer_justification="0.5 Necessary extension",
        mc_justification="tier_0_none; +1 unnecessary_ext",
    )
    assert _suggested_response_category(row) == "rubric_judgment_call"
    reverse = _row(
        "match_tier",
        reviewer_justification="1 Unnecessary extension",
        mc_justification="tier_0_descriptor; +0.5 necessary_ext",
    )
    assert _suggested_response_category(reverse) == "rubric_judgment_call"


def test_response_category_fidelity_fold_with_na_reviewer_is_rubric_call():
    # The SF fold is POC-3's own vocabulary — a reviewer-NA row that
    # diverges only on the fold is an axis-membership question.
    row = _row(
        "match_tier",
        reviewer_justification="NA",
        mc_justification="tier_0_none; +1.0 fidelity_divergent_unclear",
    )
    assert _suggested_response_category(row) == "rubric_judgment_call"


def test_response_category_unnecessary_ext_alone_still_prompt_tweak():
    # No conflict pair (reviewer side has no necessity stance beyond
    # the keyword) — the legacy extension→prompt_tweak route holds.
    row = _row(
        "tier_delta_1",
        reviewer_justification="extension logic differs",
        mc_justification="tier_0_none; +1 unnecessary_ext",
    )
    assert _suggested_response_category(row) == "prompt_tweak"


# ---------------------------------------------------------------------------
# Pattern ranking
# ---------------------------------------------------------------------------


def test_patterns_rank_by_count_times_delta():
    rows = [
        # High-count tier-1 delta — weight 5*1 = 5
        *(
            _row("tier_delta_1", reviewer_tier=2, mc_tier=1, tier_delta=1)
            for _ in range(5)
        ),
        # Single tier-3 delta — weight 1*3 = 3
        _row("tier_delta_ge2", reviewer_tier=3, mc_tier=0, tier_delta=3),
        # Moderate tier-2 delta — weight 2*2 = 4
        *(
            _row("tier_delta_ge2", reviewer_tier=3, mc_tier=1, tier_delta=2)
            for _ in range(2)
        ),
    ]
    patterns = top_divergence_patterns(rows)
    # Count×delta ordering: 5, 4, 3
    assert patterns[0]["weight"] == 5
    assert patterns[1]["weight"] == 4
    assert patterns[2]["weight"] == 3


def test_patterns_exclude_match_exact_and_no_reviewer():
    # v10 — `in_scope_mismatch` retired; `tier_delta_1` exercises the
    # same exclusion semantics (real divergence patterns surface).
    rows = [
        _row("match_exact", reviewer_tier=0, mc_tier=0, tier_delta=0),
        _row("no_reviewer_row", reviewer_tier=None, mc_tier=2),
        _row("tier_delta_1", reviewer_tier=0, mc_tier=1, tier_delta=-1),
    ]
    patterns = top_divergence_patterns(rows)
    assert len(patterns) == 1
    assert patterns[0]["signature"]["classification"] == "tier_delta_1"


def test_patterns_pick_example_with_both_justifications():
    # Cluster has two rows; the first has empty justification, the
    # second has both — prefer the second for worked-example readability.
    rows = [
        _row(
            "tier_delta_1",
            reviewer_tier=2,
            mc_tier=1,
            tier_delta=1,
            reviewer_justification=None,
            mc_justification=None,
        ),
        _row(
            "tier_delta_1",
            reviewer_tier=2,
            mc_tier=1,
            tier_delta=1,
            reviewer_justification="1 Cross-entity",
            mc_justification="tier_1_conditional",
        ),
    ]
    patterns = top_divergence_patterns(rows)
    assert patterns[0]["example"]["reviewer_justification"] == "1 Cross-entity"


# ---------------------------------------------------------------------------
# 2026-07 hygiene — gap agreement, coverage split, reason pairs
# ---------------------------------------------------------------------------


def test_gap_rows_agreeing_exactly_are_not_divergence():
    # 104/105 of the MN source cluster that ranked #3 pre-hygiene:
    # tier AND adjustment agree — agreement via the gap join path.
    rows = [
        *(
            _row(
                "gap_row_match",
                reviewer_tier=0,
                mc_tier=0,
                reviewer_adj=0.0,
                mc_adj=0.0,
                tier_delta=0,
            )
            for _ in range(5)
        ),
    ]
    assert top_divergence_patterns(rows) == []


def test_gap_rows_with_score_disagreement_rank_with_subbucket():
    rows = [
        _row(
            "gap_row_match",
            reviewer_tier=2,
            mc_tier=0,
            reviewer_adj=2.0,
            mc_adj=0.0,
            tier_delta=2,
        ),
    ]
    patterns = top_divergence_patterns(rows)
    assert len(patterns) == 1
    assert patterns[0]["signature"]["classification"] == "gap_row_match·tier_delta_ge2"
    # Weight honors the tier delta like any direct-join divergence.
    assert patterns[0]["weight"] == 2


def test_gap_adjustment_only_disagreement_ranks_as_match_tier():
    rows = [
        _row(
            "gap_row_match",
            reviewer_tier=0,
            mc_tier=0,
            reviewer_adj=1.0,
            mc_adj=0.5,
            tier_delta=0,
        ),
    ]
    patterns = top_divergence_patterns(rows)
    assert len(patterns) == 1
    assert patterns[0]["signature"]["classification"] == "gap_row_match·match_tier"


def test_unscored_gap_and_no_mc_rows_route_to_coverage():
    rows = [
        _row("no_mc_row", mc_tier=None, mc_adj=None, mc_record_key=None),
        _row(
            "gap_row_match",
            reviewer_tier=0,
            mc_tier=None,
            mc_adj=None,
            reviewer_entity="Staff",
            reviewer_element="statusCode",
        ),
    ]
    assert top_divergence_patterns(rows) == []
    coverage = coverage_patterns(rows)
    assert len(coverage) == 1
    cluster = coverage[0]
    assert cluster["state"] == "WI"
    assert cluster["count"] == 2
    assert cluster["buckets"] == {"no_mc_row": 1, "gap_row_unscored": 1}
    assert {e["entity"] for e in cluster["top_entities"]} == {"Calendar", "Staff"}


def test_match_tier_clusters_split_by_reason_pair():
    # One (state, tier, tier) shape, two stories: the necessity
    # conflict and a fidelity-fold-only row must NOT lump together.
    rows = [
        *(
            _row(
                "match_tier",
                state="TX",
                reviewer_adj=0.5,
                mc_adj=1.0,
                tier_delta=0,
                reviewer_justification="0.5 Necessary extension",
                mc_justification="tier_0_none; +1 unnecessary_ext",
            )
            for _ in range(3)
        ),
        _row(
            "match_tier",
            state="TX",
            reviewer_adj=0.0,
            mc_adj=1.0,
            tier_delta=0,
            reviewer_justification="NA",
            mc_justification="tier_0_none; +1.0 fidelity_divergent_unclear",
        ),
    ]
    patterns = top_divergence_patterns(rows)
    assert len(patterns) == 2
    assert patterns[0]["count"] == 3
    assert patterns[0]["signature"]["reason_pair"] == "R:necessary ↔ P:unnecessary_ext"
    assert patterns[1]["signature"]["reason_pair"] == "R:na ↔ P:fidelity_fold"
    # Tier-delta buckets keep the tier story — no reason pair.
    delta_rows = [
        _row("tier_delta_1", reviewer_tier=1, mc_tier=0, tier_delta=1,
             reviewer_justification="0.5 Necessary extension",
             mc_justification="tier_0_none; +1 unnecessary_ext"),
    ]
    delta_patterns = top_divergence_patterns(delta_rows)
    assert delta_patterns[0]["signature"]["reason_pair"] is None


def test_build_digest_gap_agreement_rollup_and_coverage():
    rows = [
        _row(
            "gap_row_match",
            state="MN",
            reviewer_tier=0,
            mc_tier=0,
            reviewer_adj=0.0,
            mc_adj=0.0,
            tier_delta=0,
        ),
        _row(
            "gap_row_match",
            state="MN",
            reviewer_tier=3,
            mc_tier=0,
            reviewer_adj=3.0,
            mc_adj=0.0,
            tier_delta=3,
        ),
        _row("no_mc_row", state="WI", mc_tier=None, mc_adj=None),
    ]
    digest = build_digest(rows, lens="source", top_n=5)
    assert digest["gap_agreement"] == {
        "total": 2,
        "agree_exact": 1,
        "unscored": 0,
        "divergent": 1,
    }
    # Only the genuinely divergent gap row ranks.
    assert len(digest["top_patterns"]) == 1
    assert (
        digest["top_patterns"][0]["signature"]["classification"]
        == "gap_row_match·tier_delta_ge2"
    )
    assert [c["state"] for c in digest["coverage_patterns"]] == ["WI"]


def test_md_renders_coverage_section_and_gap_footnote():
    rows = [
        _row(
            "gap_row_match",
            state="MN",
            reviewer_tier=0,
            mc_tier=0,
            reviewer_adj=0.0,
            mc_adj=0.0,
            tier_delta=0,
        ),
        _row("no_mc_row", state="WI", mc_tier=None, mc_adj=None,
             reviewer_entity="Student", reviewer_element="Email"),
    ]
    digest = build_digest(rows, lens="source", top_n=5)
    md = render_digest_md(digest)
    assert "Coverage gaps — not rubric divergence" in md
    assert "`Student|Email`" in md
    # Gap footnote explains the exclusion.
    assert "**1 of 1** gap rows agree exactly" in md
    # Agreement never leaks into the divergence table.
    assert "gap_row_match·match_exact" not in md


def test_json_rows_carry_derived_fields():
    gap = _row(
        "gap_row_match",
        reviewer_tier=0,
        mc_tier=0,
        reviewer_adj=0.5,
        mc_adj=0.5,
        tier_delta=0,
        reviewer_justification="0.5 Necessary extension",
        mc_justification="gap_row[spine] tier_0_none; +0.5 necessary_ext",
    )
    payload = comparison_row_to_dict(gap)
    assert payload["gap_score_bucket"] == "match_exact"
    assert payload["reviewer_reason"] == "necessary"
    assert payload["mc_reason"] == "necessary_ext"
    direct = _row("match_exact")
    direct_payload = comparison_row_to_dict(direct)
    assert "gap_score_bucket" not in direct_payload
    assert direct_payload["reviewer_reason"] == "na"
    assert direct_payload["mc_reason"] == "none"


# ---------------------------------------------------------------------------
# Worked-example selection
# ---------------------------------------------------------------------------


def test_pick_worked_examples_falls_back_to_all_patterns():
    # Top has only prompt_tweak + rule_adjustment; all_patterns carries
    # a lower-ranked rubric_judgment_call that should still surface.
    top = [
        {"suggested_response": "prompt_tweak", "example": {}, "signature": {}, "count": 5, "weight": 5},
        {"suggested_response": "rule_adjustment", "example": {}, "signature": {}, "count": 4, "weight": 4},
    ]
    all_patterns = top + [
        {"suggested_response": "rubric_judgment_call", "example": {}, "signature": {}, "count": 1, "weight": 1},
    ]
    worked = pick_worked_examples(top, all_patterns)
    assert [w["suggested_response"] for w in worked] == [
        "prompt_tweak",
        "rule_adjustment",
        "rubric_judgment_call",
    ]


def test_pick_worked_examples_empty_when_no_patterns():
    assert pick_worked_examples([]) == []


# ---------------------------------------------------------------------------
# Full digest shape
# ---------------------------------------------------------------------------


def test_build_digest_totals_and_per_state():
    rows = [
        _row("match_exact", state="WI"),
        _row("tier_delta_1", state="MN", reviewer_tier=1, mc_tier=0, tier_delta=1),
        _row("tier_delta_ge2", state="TX", reviewer_tier=3, mc_tier=0, tier_delta=3),
        _row("no_reviewer_row", state="TX", reviewer_tier=None, mc_tier=2),
    ]
    digest = build_digest(rows, lens="source", top_n=5)
    # 3 reviewer rows overall (1 no_reviewer_row excluded from total).
    assert digest["overall"]["reviewer_rows"] == 3
    assert digest["overall"]["matched"] == 3
    assert digest["overall"]["match_pct"] == 100.0
    assert digest["lens"] == "source"
    # Per-state: MN had 1 reviewer row → matched.
    assert digest["per_state"]["MN"]["reviewer_rows"] == 1
    # Top patterns exclude match_exact + no_reviewer.
    assert len(digest["top_patterns"]) == 2


def test_md_render_contains_key_sections():
    rows = [
        _row("match_exact", state="WI"),
        _row(
            "key_sever_override",
            state="TX",
            reviewer_justification="Severed by the key change.",
            mc_justification="tier_0_none",
            tier_delta=0,
        ),
    ]
    digest = build_digest(rows, lens="source", top_n=5)
    md = render_digest_md(digest)
    assert "Reviewer-vs-POC-3 Divergence Digest" in md
    assert "Match rates" in md
    assert "Classification breakdown" in md
    assert "Top rubric-divergence patterns" in md
    assert "Worked examples" in md
    assert "Appendix" in md
    # Human-scored framing must be in the preamble.
    assert "human-scored" in md


def test_md_render_handles_pipes_in_justification():
    rows = [
        _row(
            "tier_delta_1",
            reviewer_tier=2,
            mc_tier=1,
            tier_delta=1,
            reviewer_justification="a | b | c",
            mc_justification="tier_1_conditional",
        ),
    ]
    digest = build_digest(rows, lens="source", top_n=5)
    md = render_digest_md(digest)
    # Pipes got escaped to avoid breaking the table in the worked-example
    # section (table-level justifications render as bullet list — the
    # MD inline escape is defensive for either surface).
    assert "a \\| b \\| c" in md


# ---------------------------------------------------------------------------
# run() end-to-end with tmp sidecar + reviewer file
# ---------------------------------------------------------------------------


def test_run_writes_md_and_json(tmp_path):
    # Fabricate the five per-state reviewer workbooks + a sidecar set.
    from tests.test_review_loader import _make_reviewer_dir, _std_row

    reviewer_dir = _make_reviewer_dir(
        tmp_path,
        rows_by_key={
            "wisconsin": [
                _std_row(
                    entity="Calendar",
                    element="calendarCode",
                    nachos=0,
                    adjusted=0.0,
                    justification="NA",
                    complex_bl="No",
                    unnecessary="No",
                    is_ext="No",
                ),
            ],
        },
    )

    # Sidecar fixtures for all five in-scope states — empty scores array
    # is valid (the fixture rows outside WI just have no sidecar overlap).
    for code in ("az", "wi", "mn", "tx", "in"):
        sidecar = tmp_path / f"{code}_scores_source.json"
        if code == "wi":
            scores = [
                {
                    "record_key": "WI|Calendar|calendarCode",
                    "dimensions": {
                        "nachos_score": {
                            "value": 0,
                            "rule_matched": "tier_0_none",
                            "inputs_used": [],
                            "confidence": "high",
                        }
                    },
                    "adjusted_nachos_score": 0.0,
                    "in_scope": True,
                    "nachos_justification": "tier_0_none",
                },
            ]
        else:
            scores = []
        sidecar.write_text(json.dumps({"scores": scores}), encoding="utf-8")

    out_md = tmp_path / "digest.md"
    out_json = tmp_path / "digest.json"
    digest = run(
        lens="source",
        top_n=5,
        reviewer_dir=reviewer_dir,
        sidecar_dir=tmp_path,
        out_path_md=out_md,
        out_path_json=out_json,
    )
    assert out_md.exists()
    assert out_json.exists()
    # AZ is on the digest roster (2026-07-07 basis change).
    assert "AZ" in digest["states"]
    # One WI row, matched exactly.
    assert digest["per_state"]["WI"]["match_pct"] == 100.0
    assert digest["per_state"]["WI"]["reviewer_rows"] == 1

    # JSON companion carries the raw rows — one per fixture-workbook row.
    payload = json.loads(out_json.read_text())
    assert "rows" in payload
    assert len(payload["rows"]) == 5
    assert {r["state"] for r in payload["rows"]} == {"AZ", "WI", "MN", "TX", "IN"}


def test_run_rejects_unknown_lens():
    with pytest.raises(ValueError, match="lens"):
        run(lens="nothing")
