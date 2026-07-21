"""Unit tests for ``src.report.override_clusters`` (issue #248 Part A).

Pure-function tests over hand-built disagreement dicts (the
``curation.override_disagreements_for`` output shape) + score-record
dicts — no workbooks, no filesystem. Rendering coverage lives in
``tests/test_report_analyst.py::TestScoreCardOverrideClusters``.
"""

from __future__ import annotations

import pytest

from src.report.override_clusters import cluster_override_disagreements


def _disagreement(
    key: str = "AZ|Calendar|calendarCode",
    axis: str = "adjusted",
    override: float = 1.0,
    engine: float = 2.5,
) -> dict:
    entity, element = key.split("|")[1:]
    return {
        "record_key": key,
        "entity": entity,
        "element_name": element,
        "axis": axis,
        "override": override,
        "engine_value": engine,
        "why": "…",
    }


def _score(
    justification: str | None = "tier_0_none; +0.5 necessary_ext",
    rule: str | None = "tier_0_none",
    reasons: list[str] | None = None,
) -> dict:
    score: dict = {"nachos_justification": justification}
    if rule is not None:
        score["dimensions"] = {"nachos_score": {"rule_matched": rule}}
    if reasons is not None:
        score["review"] = {"needs_review": True, "reasons": reasons}
    return score


def _cluster(disagreements, scores, state="AZ"):
    return cluster_override_disagreements({state: disagreements}, {state: scores})


class TestClusterIdentity:
    def test_single_adjusted_disagreement(self):
        [c] = _cluster(
            [_disagreement(override=1.0, engine=2.5)],
            {"AZ|Calendar|calendarCode": _score()},
        )
        assert c["axis"] == "adjusted"
        assert c["cluster_key"] == ("adjusted", "labels", ("+0.5 necessary_ext",))
        assert c["cluster_label"] == "+0.5 necessary extension"
        assert c["direction"] == "lowered"
        assert c["rows"] == 1
        assert c["mean_delta"] == pytest.approx(-1.5)
        assert c["dual_fire_rows"] == 0
        assert c["state_counts"] == {"AZ": 1}

    def test_multi_label_row_clusters_by_full_label_set(self):
        # One row carrying two labels is ONE cluster (the label-set),
        # never one cluster per label — cluster rows must reconcile to
        # the disagreement count.
        [c] = _cluster(
            [_disagreement()],
            {"AZ|Calendar|calendarCode": _score(
                "tier_0_none; +1 unnecessary_ext, +0.5 multi_entity"
            )},
        )
        assert c["cluster_key"] == (
            "adjusted", "labels", ("+1 unnecessary_ext", "+0.5 multi_entity")
        )
        assert c["cluster_label"] == (
            "+1.0 unnecessary extension + +0.5 multiple entities involved"
        )
        assert c["rows"] == 1

    def test_dual_fire_delta_from_override_not_label_sum(self):
        # v24 non-stacking: both labels render but max() applies —
        # engine adjusted is 1.0 (0 + max(0.5, 1.0))… the label sum
        # (1.5) must never enter delta math.
        [c] = _cluster(
            [_disagreement(override=2.0, engine=1.0)],
            {"AZ|Calendar|calendarCode": _score(
                "tier_0_none; +0.5 necessary_ext, "
                "+1.0 fidelity_divergent_unclear",
                reasons=["fidelity_necessity_dual_fire"],
            )},
        )
        assert c["mean_delta"] == pytest.approx(1.0)  # 2.0 − 1.0, not 2.0 − 1.5
        assert c["dual_fire_rows"] == 1

    def test_parenthetical_variants_merge_into_one_cluster(self):
        # Typed-reason parentheticals are row-specific — two rows with
        # the same bare label but different reasons are ONE cluster
        # (also exercises the comma-at-depth-1 split).
        scores = {
            "AZ|A|x": _score(
                "tier_0_none; +1.0 fidelity_divergent_unclear "
                "(replaces_core_field_shape, transformation)"
            ),
            "AZ|B|y": _score("tier_0_none; +1.0 fidelity_divergent_unclear"),
        }
        [c] = _cluster(
            [
                _disagreement("AZ|A|x", override=0.0, engine=1.0),
                _disagreement("AZ|B|y", override=0.0, engine=1.0),
            ],
            scores,
        )
        assert c["cluster_key"] == (
            "adjusted", "labels", ("+1.0 fidelity_divergent_unclear",)
        )
        assert c["rows"] == 2

    def test_base_axis_clusters_by_rule_matched(self):
        [c] = _cluster(
            [_disagreement(axis="base", override=0.0, engine=1.0)],
            {"AZ|Calendar|calendarCode": _score(rule="tier_1_conditional")},
        )
        assert c["axis"] == "base"
        assert c["cluster_key"] == ("base", "rule", ("tier_1_conditional",))
        assert c["cluster_label"] == (
            "rule: tier_1_conditional — one conditional decides the value"
        )

    def test_adjusted_axis_without_labels_falls_back_to_rule(self):
        # No adjustments → adjusted = base + 0; the dispute is about
        # the rule tier itself (the issue's "second cut by dimension
        # provenance", folded into the one table).
        [c] = _cluster(
            [_disagreement(override=1.0, engine=0.0)],
            {"AZ|Calendar|calendarCode": _score("tier_0_none")},
        )
        assert c["cluster_key"] == ("adjusted", "rule", ("tier_0_none",))
        assert c["cluster_label"] == (
            "(no adjustments) · rule: tier_0_none — no derivation logic"
        )

    def test_missing_rule_matched_falls_back_to_unknown(self):
        [c] = _cluster(
            [_disagreement(axis="base", override=0.0, engine=1.0)],
            {"AZ|Calendar|calendarCode": {}},
        )
        assert c["cluster_key"] == ("base", "rule", ("(rule unknown)",))
        assert c["cluster_label"] == "rule: (rule unknown)"

    def test_unknown_future_label_passes_through_verbatim(self):
        [c] = _cluster(
            [_disagreement()],
            {"AZ|Calendar|calendarCode": _score("tier_0_none; +2 future_axis")},
        )
        assert c["cluster_label"] == "+2 future_axis"


class TestGroupingMath:
    def test_direction_splits_same_label_set(self):
        scores = {
            "AZ|A|x": _score(),
            "AZ|B|y": _score(),
        }
        raised, lowered = _cluster(
            [
                _disagreement("AZ|A|x", override=3.0, engine=1.0),
                _disagreement("AZ|B|y", override=0.5, engine=1.0),
            ],
            scores,
        )
        assert (raised["direction"], lowered["direction"]) == ("raised", "lowered")
        assert raised["mean_delta"] == pytest.approx(2.0)
        assert lowered["mean_delta"] == pytest.approx(-0.5)

    def test_mean_delta_over_three_rows(self):
        scores = {f"AZ|E{i}|x": _score() for i in range(3)}
        [c] = _cluster(
            [
                _disagreement("AZ|E0|x", override=0.0, engine=1.0),
                _disagreement("AZ|E1|x", override=0.5, engine=1.0),
                _disagreement("AZ|E2|x", override=0.0, engine=2.0),
            ],
            scores,
        )
        assert c["rows"] == 3
        assert c["mean_delta"] == pytest.approx((-1.0 - 0.5 - 2.0) / 3)

    def test_small_delta_direction_sign(self):
        # 0.02 clears the upstream epsilon gate (0.01) — direction
        # still resolves from the sign.
        [c] = _cluster(
            [_disagreement(override=2.52, engine=2.5)],
            {"AZ|Calendar|calendarCode": _score()},
        )
        assert c["direction"] == "raised"

    def test_multi_state_counts(self):
        clusters = cluster_override_disagreements(
            {
                "AZ": [
                    _disagreement("AZ|A|x", override=0.0, engine=1.0),
                    _disagreement("AZ|B|y", override=0.5, engine=1.0),
                ],
                "TX": [_disagreement("TX|C|z", override=0.0, engine=1.0)],
            },
            {
                "AZ": {"AZ|A|x": _score(), "AZ|B|y": _score()},
                "TX": {"TX|C|z": _score()},
            },
        )
        [c] = clusters
        assert c["rows"] == 3
        assert c["state_counts"] == {"AZ": 2, "TX": 1}

    def test_missing_score_record_still_clusters(self):
        # A disagreement whose record vanished from the scores dict
        # (shouldn't happen — disagreements are computed against it —
        # but total behavior beats a KeyError): rule-unknown fallback.
        [c] = _cluster([_disagreement()], {})
        assert c["cluster_key"] == ("adjusted", "rule", ("(rule unknown)",))


class TestOrdering:
    def test_adjusted_before_base_then_size_then_label(self):
        scores = {
            "AZ|A|x": _score(),
            "AZ|B|y": _score(),
            "AZ|C|z": _score(rule="tier_1_conditional"),
        }
        clusters = _cluster(
            [
                # base-axis cluster of 2 (bigger) …
                _disagreement("AZ|A|x", axis="base", override=0.0, engine=1.0),
                _disagreement("AZ|B|y", axis="base", override=0.0, engine=1.0),
                # … still sorts after the adjusted-axis cluster of 1.
                _disagreement("AZ|C|z", override=0.0, engine=1.0),
            ],
            scores,
        )
        assert [c["axis"] for c in clusters] == ["adjusted", "base"]
        assert clusters[1]["rows"] == 2

    def test_raised_sorts_before_lowered_within_a_cluster(self):
        scores = {"AZ|A|x": _score(), "AZ|B|y": _score()}
        clusters = _cluster(
            [
                _disagreement("AZ|A|x", override=0.0, engine=1.0),
                _disagreement("AZ|B|y", override=2.0, engine=1.0),
            ],
            scores,
        )
        assert [c["direction"] for c in clusters] == ["raised", "lowered"]

    def test_deterministic_across_input_order(self):
        scores = {
            "AZ|A|x": _score("tier_0_none; +1 unnecessary_ext"),
            "AZ|B|y": _score(),
        }
        rows = [
            _disagreement("AZ|A|x", override=0.0, engine=1.0),
            _disagreement("AZ|B|y", override=0.0, engine=0.5),
        ]
        forward = _cluster(rows, scores)
        backward = _cluster(list(reversed(rows)), scores)
        assert [c["cluster_key"] for c in forward] == [
            c["cluster_key"] for c in backward
        ]
