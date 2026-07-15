"""Issue #248 Part A — override clustering diagnostic (render-only v1).

Aggregates analyst override disagreements (``curation.
override_disagreements_for`` output) so systematic patterns route to
the methodology loop: if analysts keep contesting rows that share an
adjustment label or base rule, that is evidence about a RULE, and the
legitimate response is a ``scoring_plan_version`` bump (cf. issue
#97/v22) — not row-by-row adjudication. Pure functions over dicts;
rendering lives in ``report.score_card``.

Two decisions are load-bearing here:

1. **Deltas always come from ``override − engine_value``, never from
   adjustment-label arithmetic.** v24 non-stacking dual-fire rows
   render BOTH the necessity and fidelity labels but apply
   ``max(...)``, not the sum — label sums lie on exactly the rows
   most likely to be contested. Such rows are counted in
   ``dual_fire_rows`` and the rendered table carries the caveat.
2. **Clusters count ALL disagreements, including adjudicated ones**
   (issue #248 Part B/C). Accumulating adjudications on a pattern is
   "a methodology change wearing a disguise" — the exact signal this
   diagnostic exists to catch. The Part B/C surface suppresses
   resolved rows from the Review Queue OVERRIDE block; do NOT mirror
   that filter here.

Cluster identity is ``(axis, kind, atoms)``:

- base-axis disagreements have no adjustments in play — they cluster
  by the rule-cascade provenance ``dimensions.nachos_score.
  rule_matched`` (``kind="rule"``);
- adjusted-axis disagreements cluster by the FULL label-set tuple
  parsed from ``nachos_justification`` (``kind="labels"``) — not
  per-label, because a per-row delta cannot honestly be attributed to
  N label clusters and the sum of cluster ``rows`` must reconcile to
  the disagreement count;
- an adjusted-axis disagreement with no adjustment labels is a
  dispute about the rule tier itself (adjusted = base + 0), so it
  clusters by ``rule_matched`` too — the issue's "second cut by
  dimension provenance", folded into the one table.
"""

from __future__ import annotations

from src.score.rubric import (
    ADJUSTMENT_SHORT,
    NACHOS_RULE_SHORT,
    split_adjustment_labels,
)

# review.reasons marker for v24 non-stacking rows (both labels render,
# the larger adjustment applies) — byte-pinned in aggregate.py.
_DUAL_FIRE_REASON = "fidelity_necessity_dual_fire"

_RULE_UNKNOWN = "(rule unknown)"

# Axis render order mirrors curation._OVERRIDE_AXES: adjusted (the
# headline axis) before base.
_AXIS_RANK = {"adjusted": 0, "base": 1}
_DIRECTION_RANK = {"raised": 0, "lowered": 1}


def _rule_matched(score: dict) -> str:
    """The base rule-cascade provenance label for one score record."""
    dims = score.get("dimensions") or {}
    nachos = dims.get("nachos_score") or {}
    return nachos.get("rule_matched") or _RULE_UNKNOWN


def _cluster_key(disagreement: dict, score: dict) -> tuple[str, str, tuple[str, ...]]:
    """``(axis, kind, atoms)`` — the cluster identity for one disagreement."""
    axis = disagreement["axis"]
    if axis == "adjusted":
        labels = split_adjustment_labels(score.get("nachos_justification"))
        if labels:
            # Label order is canonical — _compute_nachos_adjustments
            # appends in fixed order (necessity, multi_entity, SF).
            return (axis, "labels", tuple(labels))
    return (axis, "rule", (_rule_matched(score),))


def _cluster_label(key: tuple[str, str, tuple[str, ...]]) -> str:
    """Human-readable cluster name via the rubric glosses (total —
    unknown tokens render verbatim, the rubric convention)."""
    axis, kind, atoms = key
    if kind == "labels":
        return " + ".join(ADJUSTMENT_SHORT.get(label, label) for label in atoms)
    rule = atoms[0]
    gloss = NACHOS_RULE_SHORT.get(rule)
    rendered = f"rule: {rule} — {gloss}" if gloss else f"rule: {rule}"
    if axis == "adjusted":
        return f"(no adjustments) · {rendered}"
    return rendered


def cluster_override_disagreements(
    disagreements_by_state: dict[str, list[dict]],
    scores_by_state: dict[str, dict[str, dict]],
) -> list[dict]:
    """Cluster override disagreements by adjustment label-set / base rule.

    ``disagreements_by_state`` maps state → the
    ``curation.override_disagreements_for`` output for that state;
    ``scores_by_state`` maps state → ``{record_key: score record}``
    (the same dict the disagreements were computed against). Per-state
    callers pass single-entry dicts; the combined workbook passes all
    states.

    Returns one dict per ``(cluster, direction)`` group::

        {
            "axis": "adjusted" | "base",
            "cluster_key": (axis, kind, atoms),   # stable identity
            "cluster_label": str,                 # rendered name
            "direction": "raised" | "lowered",
            "rows": int,
            "mean_delta": float,                  # mean(override − engine)
            "dual_fire_rows": int,
            "state_counts": {state: rows},        # keys sorted
        }

    sorted deterministically (fingerprint-load-bearing): adjusted
    before base, biggest clusters first, then label, then raised
    before lowered.
    """
    groups: dict[tuple, dict] = {}
    for state in sorted(disagreements_by_state):
        scores = scores_by_state.get(state) or {}
        for d in disagreements_by_state[state] or []:
            score = scores.get(d["record_key"]) or {}
            key = _cluster_key(d, score)
            delta = d["override"] - d["engine_value"]
            # Zero is impossible — epsilon-gated upstream in
            # override_disagreements.
            direction = "raised" if delta > 0 else "lowered"
            group = groups.setdefault(
                (key, direction),
                {
                    "axis": key[0],
                    "cluster_key": key,
                    "cluster_label": _cluster_label(key),
                    "direction": direction,
                    "rows": 0,
                    "_delta_sum": 0.0,
                    "dual_fire_rows": 0,
                    "state_counts": {},
                },
            )
            group["rows"] += 1
            group["_delta_sum"] += delta
            reasons = (score.get("review") or {}).get("reasons") or []
            if _DUAL_FIRE_REASON in reasons:
                group["dual_fire_rows"] += 1
            counts = group["state_counts"]
            counts[state] = counts.get(state, 0) + 1

    out: list[dict] = []
    for group in groups.values():
        delta_sum = group.pop("_delta_sum")
        group["mean_delta"] = delta_sum / group["rows"]
        group["state_counts"] = dict(sorted(group["state_counts"].items()))
        out.append(group)
    out.sort(
        key=lambda g: (
            _AXIS_RANK.get(g["axis"], 99),
            -g["rows"],
            g["cluster_label"],
            _DIRECTION_RANK.get(g["direction"], 99),
        )
    )
    return out
