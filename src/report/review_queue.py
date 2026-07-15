"""Phase D review-queue routing — POLICY / DATA_MODEL / SCORING / ANALYST.

First-principles triage (plan §10, no GT-tuned thresholds): maps the
``review.reasons`` list Phase C aggregate writes into one of four
routes so reviewers don't all look at the same 200-row pile.

Routing priority (first match wins):

1. ``DATA_MODEL`` — any ``inter_dim_inconsistency:*`` reason. These
   are structural mismatches (``documentation_completeness=0`` with
   ``business_logic_complexity>=2`` — "the docs don't describe the
   logic the rules found"). Signal for ingest-layer or data-model
   reviewers, not the LLM or the rubric.
2. ``POLICY`` — two or more ``hallucinated_input:*`` reasons on the
   same row. Concentrated fabrication = the prompt or rubric is
   asking the model for something it can't reliably produce on this
   population (e.g., WI's pipe-table descriptor-value-table rows
   trigger hallucinated spans on multiple facts at once).
3. ``SCORING`` — exactly one ``hallucinated_input:*`` reason.
   One-off span validator miss — more likely a validator-edge-case
   investigation than a prompt rewrite.
4. ``ANALYST`` — any other flag (notably ``low_confidence_dimension``).
   The model asked for human judgment; that's the analyst's job.

Zero reasons → no route (``needs_review=False`` skips routing
entirely). The priority ladder is deliberately conservative on
``POLICY``/``DATA_MODEL`` so those buckets stay small and actionable;
``ANALYST`` is the catch-all and expected to carry the volume.

CRITICAL: ``run()`` is a plain function. The Click wrapper lives in
``src/poc3/cli.py``. Reads per-state sidecars at
``data/out/{state}_scores_{lens}.json`` and writes
``data/out/review_queue_{lens}.{json,md}``.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from src.states import SUPPORTED_STATES as _STATES
from src.utils.paths import out_dir, state_scores_path

_LOGGER = logging.getLogger(__name__)

ROUTES: tuple[str, ...] = ("POLICY", "DATA_MODEL", "SCORING", "ANALYST")

# One prose line per route, rendered in the review-queue markdown table
# and consumed by the analyst workbook's Legend sheet
# (``src.score.rubric`` — lockstep-tested in
# ``tests/test_score_rubric.py``). Keys mirror ``ROUTES``; text is
# byte-identical to the historical inline dict (hoisted 2026-07,
# Sequence-1 presentation legend — no text change).
ROUTE_DESCRIPTIONS: dict[str, str] = {
    "POLICY": "Multi-fact hallucination on one row — prompt/rubric investigation.",
    "DATA_MODEL": "Structural inconsistency (e.g. docs missing but logic complex) — ingest/data-model question.",
    "SCORING": "Single hallucinated span — validator edge case investigation.",
    "ANALYST": "Low-confidence dimension — analyst judgment call.",
}


def route_review(reasons: list[str]) -> str | None:
    """First-match-wins routing for a single record's ``review.reasons``.

    Returns ``None`` when ``reasons`` is empty — the aggregate emitted
    ``needs_review=False`` and there's nothing to route.

    Phase F adds ``nachos_low_confidence_high_tier`` — in-scope NACHOS
    tier >= 3 with low composite confidence. The tier itself is
    stakeholder-visible (lands on Details col 9) so any uncertainty
    there is worth SCORING escalation (validator-edge-case
    investigation), not catch-all ANALYST review.

    v24 (issue #124 Option 2) adds ``fidelity_necessity_dual_fire`` —
    fires when the rubric's necessity fork AND v12 SF fold both fired
    at base 0 and the non-stacking max-of-two precedence rule discarded
    one branch's magnitude. Routes to ANALYST via the catch-all: the
    rule has fired correctly per the methodology; the analyst's job is
    to spot-check that max-of-two was the right call on this row, not
    investigate a validator/prompt regression.
    """
    if not reasons:
        return None
    if any(r.startswith("inter_dim_inconsistency:") for r in reasons):
        return "DATA_MODEL"
    hallucinated = [r for r in reasons if r.startswith("hallucinated_input:")]
    if len(hallucinated) >= 2:
        return "POLICY"
    if len(hallucinated) == 1:
        return "SCORING"
    # Phase F — nachos_low_confidence_high_tier escalates to SCORING
    # since the tier is stakeholder-visible and low-confidence there
    # is an actionable signal for validator/prompt review.
    if "nachos_low_confidence_high_tier" in reasons:
        return "SCORING"
    return "ANALYST"


def _load_sidecar(
    state: str,
    lens: str,
    base: Path | None = None,
    *,
    allow_stale: bool = False,
) -> dict[str, Any]:
    path = (
        (base / f"{state.lower()}_scores_{lens}.json")
        if base is not None
        else state_scores_path(state, lens)  # type: ignore[arg-type]
    )
    # Issue #212 item 3: with a publish lineage, a stale sidecar must
    # not silently feed the queue (missing/corrupt already raise —
    # this loader never swallowed).
    from src.publish.manifest import verify_fresh

    verify_fresh(
        path, consumer="report review-queue", allow_stale=allow_stale
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _per_record_entry(state: str, score: dict[str, Any]) -> dict[str, Any]:
    """Lean projection of one scored record for the review queue payload.

    Review-queue entries exist for routing/triage, not per-record
    scoring — the scalar quality mean adds nothing to the reviewer's
    decision and would invite the same conflation workbooks had to
    drop.
    """
    review = score.get("review", {}) or {}
    reasons = list(review.get("reasons", []) or [])
    return {
        "state": state,
        "record_key": score.get("record_key"),
        "entity": score.get("entity"),
        "element_name": score.get("element_name"),
        "complexity_score": score.get("complexity_score"),
        "confidence_composite": score.get("confidence_composite"),
        "reasons": reasons,
        "route": route_review(reasons),
    }


def build_queue(
    lens: str,
    states: tuple[str, ...] = _STATES,
    base: Path | None = None,
    *,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Assemble the review queue for ``lens`` across ``states``."""
    entries: list[dict[str, Any]] = []
    per_state_counts: dict[str, Counter] = {}
    for st in states:
        sidecar = _load_sidecar(st, lens, base=base, allow_stale=allow_stale)
        counts: Counter = Counter()
        for score in sidecar.get("scores", []):
            if not score.get("review", {}).get("needs_review"):
                continue
            entry = _per_record_entry(st, score)
            entries.append(entry)
            counts[entry["route"] or "UNROUTED"] += 1
        per_state_counts[st] = counts

    # Sort: by route priority (POLICY, DATA_MODEL, SCORING, ANALYST),
    # then state, then record_key for deterministic tiebreak. (Previously
    # included per_record_score ascending — dropped with the workbook
    # demotion; reviewers work the priority ladder, not a scalar
    # ordering.)
    route_order = {name: idx for idx, name in enumerate(ROUTES)}

    def _sort_key(e: dict[str, Any]) -> tuple:
        route = e["route"] or "ZZZZ"
        return (
            route_order.get(route, 99),
            e["state"],
            e["record_key"] or "",
        )

    entries.sort(key=_sort_key)

    # Cross-route totals for the rollup header.
    route_totals: Counter = Counter()
    for e in entries:
        route_totals[e["route"] or "UNROUTED"] += 1

    per_state_summary = {
        st: {
            "total": sum(counts.values()),
            "by_route": dict(counts),
        }
        for st, counts in per_state_counts.items()
    }

    return {
        "generated_on": date.today().isoformat(),
        "lens": lens,
        "route_priority": list(ROUTES),
        "route_totals": dict(route_totals),
        "per_state": per_state_summary,
        "entries": entries,
        "guidance": (
            "Review-queue routing is first-principles (plan §10, no GT-"
            "tuned thresholds). DATA_MODEL: structural mismatch ingest "
            "should verify. POLICY: the LLM fabricated on multiple facts "
            "for this row — prompt/rubric discussion. SCORING: single "
            "hallucinated span — validator edge case. ANALYST: normal "
            "low-confidence review."
        ),
    }


def render_markdown(queue: dict[str, Any], *, top_n: int = 30) -> str:
    lens = queue["lens"]
    lines: list[str] = [
        f"# Review queue — {lens}-lens",
        "",
        f"Generated on: {queue['generated_on']}.",
        "",
        queue["guidance"],
        "",
        "## Route totals",
        "",
        "| Route | Count | Description |",
        "| :- | -: | :- |",
    ]
    for route in ROUTES:
        count = queue["route_totals"].get(route, 0)
        lines.append(
            f"| {route} | {count:,} | {ROUTE_DESCRIPTIONS[route]} |"
        )

    lines.extend([
        "",
        "## Per-state × per-route counts",
        "",
        "| State | " + " | ".join(ROUTES) + " | Total |",
        "| :- |" + " -: |" * (len(ROUTES) + 1),
    ])
    for state, info in queue["per_state"].items():
        row = [f"| {state} |"]
        for route in ROUTES:
            row.append(f" {info['by_route'].get(route, 0):,} |")
        row.append(f" {info['total']:,} |")
        lines.append("".join(row))

    lines.extend([
        "",
        f"## Top {top_n} flagged records (sorted by route priority)",
        "",
        "| Route | State | Entity | Element | Reasons |",
        "| :- | :- | :- | :- | :- |",
    ])
    for entry in queue["entries"][:top_n]:
        reasons = "; ".join(entry["reasons"]) or "(none)"
        lines.append(
            f"| {entry['route'] or '(unrouted)'} | {entry['state']} | "
            f"{entry['entity']} | {entry['element_name']} | {reasons} |"
        )

    if len(queue["entries"]) > top_n:
        lines.extend([
            "",
            f"…and {len(queue['entries']) - top_n:,} more. Full list in "
            f"``data/out/review_queue_{lens}.json``.",
        ])
    lines.append("")
    return "\n".join(lines)


def run(
    *,
    lens: str = "spine",
    states: tuple[str, ...] = _STATES,
    out: Path | None = None,
    top_n: int = 30,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Write ``review_queue_{lens}.{json,md}`` and return the queue dict.

    ``top_n`` bounds the MD listing — the JSON payload carries every
    flagged record regardless, so the MD stays skimmable while the
    spreadsheet / analyst tools consume the full set from JSON.
    """
    base = out or out_dir()
    base.mkdir(parents=True, exist_ok=True)
    queue = build_queue(
        lens, states=states, base=base, allow_stale=allow_stale
    )
    if out is not None:
        json_path = out / f"review_queue_{lens}.json"
        md_path = out / f"review_queue_{lens}.md"
    else:
        json_path = base / f"review_queue_{lens}.json"
        md_path = base / f"review_queue_{lens}.md"
    json_path.write_text(
        json.dumps(queue, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(queue, top_n=top_n), encoding="utf-8")
    _LOGGER.info(
        "review-queue (%s): wrote %s + %s, routes=%s",
        lens, json_path, md_path, queue["route_totals"],
    )
    return queue
