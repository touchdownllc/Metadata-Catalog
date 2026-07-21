"""Phase E — review_comparison.py classifier + joiner tests.

One fabricated-input test per classification bucket, plus join-shape
coverage (no_mc_row, no_reviewer_row, in-scope mismatch cases).
The real-data comparison is exercised at the digest level; this
module's tests stay hermetic.
"""

from __future__ import annotations

import json

import pytest

from src.score.review_comparison import (
    classification_counts,
    compare_state,
    load_gap_scores,
    load_sidecar,
    match_rate,
    run,
)
from src.score.review_loader import ReviewerRecord


def _reviewer(
    entity: str,
    element: str,
    tier: int | None,
    adj: float | None,
    justification: str | None = None,
    state: str = "WI",
) -> ReviewerRecord:
    return ReviewerRecord(
        state=state,
        entity=entity,
        element=element,
        nachos_score=tier,
        adjusted_nachos_score=adj,
        justification=justification,
        is_extension=None,
        unnecessary_extension=None,
        cross_entity=None,
        complex_business_logic=None,
    )


def _poc3(
    entity: str,
    element: str,
    tier: int | None,
    adj: float | None,
    in_scope: bool,
    justification: str | None = None,
    state: str = "WI",
) -> dict:
    key = f"{state}|{entity}|{element}"
    return {
        "record_key": key,
        "entity": entity,
        "element_name": element,
        "dimensions": {
            "nachos_score": {
                "value": tier,
                "rule_matched": justification or "tier_0_none",
                "inputs_used": [],
                "confidence": "high",
            }
        },
        "adjusted_nachos_score": adj,
        "in_scope": in_scope,
        "nachos_justification": justification,
    }


# ---------------------------------------------------------------------------
# Classification buckets
# ---------------------------------------------------------------------------


def test_match_exact():
    reviewer = [_reviewer("Calendar", "calendarCode", 0, 0.0, "NA")]
    # v10 — every poc3 row is in_scope=True. Tier-0 reviewer + tier-0
    # poc3 falls through to match_exact.
    poc3 = [_poc3("Calendar", "calendarCode", 0, 0.0, True)]
    rows = compare_state("WI", "source", reviewer, poc3)
    joined = [r for r in rows if r.classification != "no_reviewer_row"]
    assert len(joined) == 1
    assert joined[0].classification == "match_exact"


def test_match_tier_differs_in_adj():
    reviewer = [_reviewer("Calendar", "calendarCode", 3, 4.0, "0.5 Cross-entity")]
    poc3 = [_poc3("Calendar", "calendarCode", 3, 3.5, True, "tier_3_multi_if")]
    rows = compare_state("WI", "source", reviewer, poc3)
    joined = [r for r in rows if r.classification != "no_reviewer_row"]
    assert joined[0].classification == "match_tier"
    assert joined[0].tier_delta == 0
    assert joined[0].adj_delta == pytest.approx(0.5)


def test_tier_delta_1():
    reviewer = [_reviewer("Calendar", "calendarCode", 2, 3.0, "1 Unnecessary")]
    poc3 = [_poc3("Calendar", "calendarCode", 1, 1.0, True, "tier_1_conditional")]
    rows = compare_state("WI", "source", reviewer, poc3)
    joined = [r for r in rows if r.classification != "no_reviewer_row"]
    assert joined[0].classification == "tier_delta_1"
    assert joined[0].tier_delta == 1


def test_tier_delta_ge2():
    reviewer = [_reviewer("Calendar", "calendarCode", 3, 3.0, "NA")]
    poc3 = [_poc3("Calendar", "calendarCode", 1, 1.0, True, "tier_1_conditional")]
    rows = compare_state("WI", "source", reviewer, poc3)
    joined = [r for r in rows if r.classification != "no_reviewer_row"]
    assert joined[0].classification == "tier_delta_ge2"


def test_in_scope_mismatch_classification_retired():
    """v10 (2026-04-26) — methodology scope rectification removes the
    ``in_scope_mismatch`` classification. POC-3 in_scope is now uniformly
    True; the prior bucket compared poc3-in_scope against a reviewer
    heuristic (``adjusted != 0``) and would have fired on every reviewer
    tier-0 row. The classifier now falls through to tier-equality
    comparisons (match_exact / match_tier / tier_delta_*)."""
    # Reviewer marks tier-0 + adj=0.5 (necessary ext); poc3 tier-0 + adj=0.
    # Pre-v10 → in_scope_mismatch. Post-v10 → match_tier (tier matches,
    # adj differs).
    reviewer = [_reviewer("Calendar", "calendarCode", 0, 0.5, "0.5 Necessary")]
    poc3 = [_poc3("Calendar", "calendarCode", 0, 0.0, True)]
    rows = compare_state("WI", "source", reviewer, poc3)
    joined = [r for r in rows if r.classification != "no_reviewer_row"]
    assert joined[0].classification == "match_tier"


def test_reviewer_tier_0_vs_mc_tier_1_falls_through_to_tier_delta_1():
    """v10 — reviewer tier-0 vs poc3 tier-1 was pre-v10
    ``in_scope_mismatch``; now falls through to ``tier_delta_1``."""
    reviewer = [_reviewer("Calendar", "calendarCode", 0, 0.0, "NA")]
    poc3 = [_poc3("Calendar", "calendarCode", 1, 1.5, True, "tier_1_conditional")]
    rows = compare_state("WI", "source", reviewer, poc3)
    joined = [r for r in rows if r.classification != "no_reviewer_row"]
    assert joined[0].classification == "tier_delta_1"


def test_key_sever_override_precedes_tier_delta():
    # Even with a large tier delta, the key-sever marker wins.
    reviewer = [
        _reviewer(
            "CourseTranscriptExt",
            "course.LocalEducationAgencyId",
            0,
            3.0,
            "Ability to write on the entity is severed by the key change.",
            state="TX",
        )
    ]
    poc3 = [_poc3("CourseTranscriptExt", "courseLocalEducationAgencyId", 0, 0.0, True, state="TX")]
    rows = compare_state("TX", "source", reviewer, poc3)
    joined = [r for r in rows if r.classification != "no_reviewer_row"]
    assert joined[0].classification == "key_sever_override"


def test_key_sever_marker_also_matches_key_change_phrase():
    reviewer = [
        _reviewer(
            "CourseTranscriptExt",
            "sectionIdentifier",
            0,
            3.0,
            "Downstream key change makes this unwritable.",
            state="TX",
        )
    ]
    poc3 = [_poc3("CourseTranscriptExt", "sectionIdentifier", 2, 2.0, True, state="TX")]
    rows = compare_state("TX", "source", reviewer, poc3)
    joined = [r for r in rows if r.classification != "no_reviewer_row"]
    assert joined[0].classification == "key_sever_override"


def test_no_mc_row_for_reviewer_with_no_match():
    reviewer = [_reviewer("Calendars", "totallyFabricated", 2, 3.0, "text")]
    # v10 — poc3 row is in_scope=True with adj=0 (tier-0 / no
    # adjustment). It's quiet for ``no_reviewer_row`` filtering because
    # adj ≤ 0 (the v10 equivalent of the pre-v10 in_scope=False filter).
    poc3 = [_poc3("Calendar", "calendarCode", 0, 0.0, True)]
    rows = compare_state("WI", "source", reviewer, poc3)
    assert any(r.classification == "no_mc_row" for r in rows)
    # The POC-3 adj-0 record should NOT surface as no_reviewer_row.
    assert not any(r.classification == "no_reviewer_row" for r in rows)


def test_gap_row_match_recovers_reviewer_only_row():
    """When the reviewer row doesn't resolve to a sidecar key but DOES
    resolve to a row in the gap artifact, the comparison promotes the
    classification to ``gap_row_match`` (issue #66 Layer 2). Sidecar-side
    keys are unchanged; gap_lookup is the new lookup surface."""
    from src.utils.matching import entity_match_form

    reviewer = [_reviewer("PriorYearLeaver", "DiplomaType", 1, 0.5, "text", state="TX")]
    poc3: list[dict] = []  # nothing in sidecar
    # Synthetic gap lookup mirroring the gap surfacer's output for
    # `tx_priorYearLeaver / graduationSetDiplomaTypeDescriptor`. Includes
    # the descriptor-stripped leaf alias the lookup builder generates.
    gap_record = {
        "state": "TX",
        "entity": "PriorYearLeaver",
        "element_name": "graduationSetDiplomaTypeDescriptor",
        "discovery": "spine_only_full_entity",
        "leaf_name": "diplomaTypeDescriptor",
    }
    ent_n = entity_match_form("PriorYearLeaver")
    gap_lookup = {(ent_n, "diplomatype"): gap_record}

    rows = compare_state(
        "TX", "source", reviewer, poc3, gap_lookup=gap_lookup
    )
    classifications = [r.classification for r in rows]
    assert "gap_row_match" in classifications
    hit = next(r for r in rows if r.classification == "gap_row_match")
    assert hit.mc_record_key == (
        "TX|PriorYearLeaver|graduationSetDiplomaTypeDescriptor::gap"
    )
    assert "spine_only_full_entity" in (hit.mc_justification or "")
    # tier/adj are None because Layer 2 is audit-only (no scoring yet).
    assert hit.mc_tier is None
    assert hit.mc_adj is None


def test_no_gap_lookup_falls_through_to_no_mc_row():
    """When no gap_lookup is provided (or it's empty), the comparison
    behaves identically to pre-Layer-2 — falling through to
    ``no_mc_row``. Backward-compat guard for callers that haven't
    been migrated to load gap artifacts yet."""
    reviewer = [_reviewer("Calendars", "totallyFabricated", 2, 3.0, "text")]
    rows = compare_state("WI", "source", reviewer, [])
    assert any(r.classification == "no_mc_row" for r in rows)
    assert not any(r.classification == "gap_row_match" for r in rows)


# ---------------------------------------------------------------------------
# Layer 3 (issue #73) — gap_scores join populates tier/adj on gap_row_match
# ---------------------------------------------------------------------------


def _gap_score(
    state: str,
    entity: str,
    element: str,
    tier: int,
    adj: float,
    in_scope: bool = True,
    justification: str = "tier_0_none",
) -> dict:
    """Fabricate a gap-sidecar score record (subset of fields used by the join)."""
    return {
        "record_key": f"{state.upper()}|{entity}|{element}",
        "entity": entity,
        "element_name": element,
        "dimensions": {
            "nachos_score": {
                "value": tier,
                "rule_matched": justification,
                "inputs_used": [],
                "confidence": "high",
            }
        },
        "adjusted_nachos_score": adj,
        "in_scope": in_scope,
        "nachos_justification": justification,
        "discovery_lens": "spine_anchored",
    }


def test_gap_row_match_with_scores_populates_tier_and_adj():
    """Layer 3 — when ``gap_scores`` is provided, gap_row_match rows
    pivot from ``mc_tier=None`` to populated tier/adj/delta detail.
    The classification bucket stays ``gap_row_match`` (no re-bucketing)."""
    from src.utils.matching import entity_match_form

    reviewer = [_reviewer("PriorYearLeaver", "DiplomaType", 2, 2.5, "text", state="TX")]
    poc3: list[dict] = []  # nothing in source/spine sidecar

    gap_record = {
        "state": "TX",
        "entity": "PriorYearLeaver",
        "element_name": "graduationSetDiplomaTypeDescriptor",
        "discovery": "spine_only_full_entity",
        "leaf_name": "diplomaTypeDescriptor",
    }
    ent_n = entity_match_form("PriorYearLeaver")
    gap_lookup = {(ent_n, "diplomatype"): gap_record}

    gap_scores = {
        "TX|PriorYearLeaver|graduationSetDiplomaTypeDescriptor": _gap_score(
            "TX",
            "PriorYearLeaver",
            "graduationSetDiplomaTypeDescriptor",
            tier=0,
            adj=0.5,
            justification="tier_0_none_with_structural",
        )
    }

    rows = compare_state(
        "TX",
        "source",
        reviewer,
        poc3,
        gap_lookup=gap_lookup,
        gap_scores=gap_scores,
    )
    hit = next(r for r in rows if r.classification == "gap_row_match")
    # Canonical record_key (no ``::gap`` suffix) once we resolve to a
    # real scored row.
    assert hit.mc_record_key == (
        "TX|PriorYearLeaver|graduationSetDiplomaTypeDescriptor"
    )
    assert hit.mc_tier == 0
    assert hit.mc_adj == 0.5
    assert hit.mc_in_scope is True
    assert hit.tier_delta == 2  # reviewer 2 - poc3 0
    assert hit.adj_delta == pytest.approx(2.0)  # reviewer 2.5 - poc3 0.5
    # Discovery + sidecar justification both present in the composed string.
    assert "spine_only_full_entity" in (hit.mc_justification or "")
    assert "tier_0_none_with_structural" in (hit.mc_justification or "")


def test_gap_row_match_falls_back_to_layer2_when_score_missing():
    """When gap_lookup hits but gap_scores has no entry for that key,
    the row preserves the Layer-2 audit-only shape (synthetic ``::gap``
    record_key, ``mc_tier=None``). Hermetic guard for partially-
    regenerated artifacts (e.g., elements_gap.json present without
    scores_gap.json)."""
    from src.utils.matching import entity_match_form

    reviewer = [_reviewer("PriorYearLeaver", "DiplomaType", 1, 0.5, "text", state="TX")]
    poc3: list[dict] = []
    gap_record = {
        "state": "TX",
        "entity": "PriorYearLeaver",
        "element_name": "graduationSetDiplomaTypeDescriptor",
        "discovery": "spine_only_full_entity",
        "leaf_name": "diplomaTypeDescriptor",
    }
    ent_n = entity_match_form("PriorYearLeaver")
    gap_lookup = {(ent_n, "diplomatype"): gap_record}

    rows = compare_state(
        "TX",
        "source",
        reviewer,
        poc3,
        gap_lookup=gap_lookup,
        gap_scores={},  # explicitly empty — no scored gap row available
    )
    hit = next(r for r in rows if r.classification == "gap_row_match")
    assert hit.mc_record_key == (
        "TX|PriorYearLeaver|graduationSetDiplomaTypeDescriptor::gap"
    )
    assert hit.mc_tier is None
    assert hit.mc_adj is None


def test_load_gap_scores_basic_shape(tmp_path):
    """``load_gap_scores`` returns a record_key-keyed dict, falls back
    to ``{}`` when the gap sidecar is absent."""
    target = tmp_path / "tx_scores_gap.json"
    target.write_text(
        json.dumps(
            {
                "scores": [
                    _gap_score("TX", "PriorYearLeaver", "graduationSetDiplomaTypeDescriptor", 0, 0.5),
                    _gap_score("TX", "AcademicWeek", "beginDate", 0, 0.0),
                ]
            }
        ),
        encoding="utf-8",
    )
    idx = load_gap_scores("TX", sidecar_dir=tmp_path)
    assert set(idx.keys()) == {
        "TX|PriorYearLeaver|graduationSetDiplomaTypeDescriptor",
        "TX|AcademicWeek|beginDate",
    }
    # Missing artifact → empty dict (never raises).
    empty = load_gap_scores("WI", sidecar_dir=tmp_path)
    assert empty == {}


def test_run_loads_gap_artifacts_from_sidecar_dir(tmp_path):
    """End-to-end — ``run()`` picks up gap_lookup + gap_scores from the
    same ``sidecar_dir`` that holds source/spine sidecars and surfaces
    populated ``gap_row_match`` rows."""
    from src.utils.matching import entity_match_form

    # Source sidecar has nothing the reviewer can land on.
    for code in ("AZ", "WI", "MN", "TX", "IN"):
        target = tmp_path / f"{code.lower()}_scores_source.json"
        target.write_text(json.dumps({"scores": []}), encoding="utf-8")

    # TX gap artifacts (elements + scores).
    elements_gap = {
        "gaps": [
            {
                "state": "TX",
                "entity": "PriorYearLeaver",
                "element_name": "graduationSetDiplomaTypeDescriptor",
                "discovery": "spine_only_full_entity",
                "leaf_name": "diplomaTypeDescriptor",
            }
        ]
    }
    (tmp_path / "tx_elements_gap.json").write_text(
        json.dumps(elements_gap), encoding="utf-8"
    )
    (tmp_path / "tx_scores_gap.json").write_text(
        json.dumps(
            {
                "scores": [
                    _gap_score(
                        "TX",
                        "PriorYearLeaver",
                        "graduationSetDiplomaTypeDescriptor",
                        tier=0,
                        adj=0.5,
                        justification="tier_0_none_with_structural",
                    )
                ]
            }
        ),
        encoding="utf-8",
    )

    reviewer = [
        _reviewer("PriorYearLeaver", "DiplomaType", 2, 2.5, "text", state="TX"),
    ]
    rows = run(
        reviewer_records=reviewer,
        lens="source",
        sidecar_dir=tmp_path,
    )
    gap_rows = [r for r in rows if r.classification == "gap_row_match"]
    assert len(gap_rows) == 1
    hit = gap_rows[0]
    assert hit.state == "TX"
    assert hit.mc_tier == 0
    assert hit.mc_adj == 0.5
    assert hit.tier_delta == 2
    assert hit.mc_record_key == (
        "TX|PriorYearLeaver|graduationSetDiplomaTypeDescriptor"
    )
    # Silent normalize check — also asserts ``entity_match_form`` is on the
    # path that fed the lookup builder (regression guard for an import drop).
    assert entity_match_form("PriorYearLeaver")


def test_no_reviewer_row_filters_on_adjusted_score():
    """v10 — the no_reviewer_row surface filters on
    ``adjusted_nachos_score > 0`` (the cohort the reviewer would care
    about). Pre-v10 used the in_scope flag; post-rectification in_scope
    is uniformly True so adj > 0 is the equivalent semantic."""
    reviewer = []
    poc3 = [
        _poc3("Calendar", "calendarCode", 0, 0.0, True),  # adj=0 → quiet
        _poc3("Calendar", "extra", 2, 2.5, True, "tier_2"),  # adj>0 → surface
    ]
    rows = compare_state("WI", "source", reviewer, poc3)
    classifications = [r.classification for r in rows]
    assert classifications == ["no_reviewer_row"]
    assert rows[0].mc_record_key == "WI|Calendar|extra"


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


def test_classification_counts_and_match_rate():
    reviewer = [
        _reviewer("Calendar", "calendarCode", 0, 0.0),
        _reviewer("Calendar", "otherElement", 1, 1.5),
        _reviewer("Calendar", "fabricated", 2, 3.0),  # no_poc3
    ]
    poc3 = [
        # v10 — every poc3 row is in_scope=True. ``no_reviewer_row``
        # filters on adj>0 instead.
        _poc3("Calendar", "calendarCode", 0, 0.0, True),
        _poc3("Calendar", "otherElement", 2, 2.5, True, "tier_2"),
        _poc3("Calendar", "only_in_poc3", 3, 4.0, True, "tier_3"),  # no_reviewer (adj>0)
    ]
    rows = compare_state("WI", "source", reviewer, poc3)
    counts = classification_counts(rows)
    assert counts["match_exact"] == 1
    assert counts["no_mc_row"] == 1
    assert counts["no_reviewer_row"] == 1
    # tier_delta_1 for Calendar|otherElement (reviewer 1 vs POC-3 2).
    assert counts["tier_delta_1"] == 1

    matched, total, pct = match_rate(rows)
    # 2 reviewer rows matched + 1 unmatched = 2/3 reviewer rows matched.
    assert matched == 2
    assert total == 3
    assert pct == 66.7


# ---------------------------------------------------------------------------
# run() wiring
# ---------------------------------------------------------------------------


def test_run_loads_sidecar_dir_override(tmp_path):
    # Fabricate a sidecar per roster state so run() can find them.
    for code in ("AZ", "WI", "MN", "TX", "IN"):
        target = tmp_path / f"{code.lower()}_scores_source.json"
        target.write_text(
            json.dumps({"scores": [_poc3("Calendar", "calendarCode", 0, 0.0, True, state=code)]}),
            encoding="utf-8",
        )
    reviewer = [
        _reviewer("Calendar", "calendarCode", 0, 0.0, state="WI"),
        _reviewer("Calendar", "calendarCode", 0, 0.0, state="MN"),
        _reviewer("Calendar", "calendarCode", 0, 0.0, state="TX"),
    ]
    rows = run(
        reviewer_records=reviewer,
        lens="source",
        sidecar_dir=tmp_path,
    )
    # Three joined rows, one per state, all match_exact.
    match_exacts = [r for r in rows if r.classification == "match_exact"]
    assert len(match_exacts) == 3
    assert {r.state for r in match_exacts} == {"WI", "MN", "TX"}


def test_load_sidecar_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="aggregate"):
        load_sidecar("WI", "source", path=tmp_path / "nope.json")


def test_run_rejects_unknown_lens():
    with pytest.raises(ValueError, match="lens"):
        run(reviewer_records=[], lens="nothing", sidecar_dir=None)
