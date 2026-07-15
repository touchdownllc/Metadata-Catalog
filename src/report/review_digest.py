"""Phase E — reviewer-vs-POC-3 divergence digest.

Writes ``data/out/review_digest_{lens}.{json,md}`` — the Phase E
output per ``docs/archive/next-session/next-session-phase-e.md``. Summarizes, per
(state, lens):

- **Match-rate table** — how many reviewer rows found a POC-3
  counterpart.
- **Classification breakdown** — count per bucket
  (match_exact, match_tier, tier_delta_1, tier_delta_ge2,
  in_scope_mismatch, key_sever_override, no_poc3_row, no_reviewer_row).
- **Top 10 divergence patterns** — ranked by
  ``row_count * max(1, |tier_delta|)`` so high-count same-delta
  patterns rise alongside rarer-but-larger-delta ones. Each entry
  names a concrete example, surfaces both justifications, and
  proposes a response **category** (prompt tweak / rule adjustment /
  rubric judgment call). The category is a suggestion — the user
  decides what to change (see the human-scored framing in the
  session brief). Only genuine *score* disagreements rank here:
  ``gap_row_match`` rows whose tier AND adjustment agree are
  agreement-by-another-join-path, not divergence, and coverage
  buckets (``no_poc3_row``, unscored gap rows) are split into their
  own section (2026-07 digest hygiene — see
  ``docs/archive/next-session/next-session-digest-hygiene.md``).
- **Coverage gaps section** — ``no_poc3_row`` + unscored
  ``gap_row_match`` clusters per state. These are join-coverage
  facts, not rubric divergence; no response category applies.
- **Three worked examples** — one per response category, drawn from
  real rows.
- **Appendix** — per-state, per-classification drill-down tables.

**Discipline reminder:** this is comparison output, not correctness
scoring. The digest never asserts reviewer or POC-3 is "correct"; it
names where they diverge and proposes places to look.

CRITICAL: ``run()`` is a plain function; the Click wrapper lives in
``src/poc3/cli.py``.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from src.score.review_comparison import (
    ComparisonRow,
    classification_counts,
    match_rate,
    run as run_comparison,
)
from src.score.review_loader import load_reviewer_records
from src.states import SUPPORTED_STATES
from src.utils.paths import out_dir

_LOGGER = logging.getLogger(__name__)


# The digest roster mirrors the POC-3 state roster: the per-state
# human-scored basis (2026-07-07) covers all five states — AZ joined
# the comparison when its workbook entered ``review_loader.REVIEWER_SOURCES``.
IN_SCOPE_STATES: tuple[str, ...] = SUPPORTED_STATES

# Bucket rendering order on the MD page — agreement buckets first, then
# escalating divergence, then join-coverage buckets last.
_BUCKET_ORDER: tuple[str, ...] = (
    "match_exact",
    "match_tier",
    "tier_delta_1",
    "tier_delta_ge2",
    "in_scope_mismatch",  # retired in v10; kept at 0 for backward-compat columns.
    "key_sever_override",
    "gap_row_match",  # added 2026-04-29 (issue #73 Layer 3 production surface).
    "no_poc3_row",
    "no_reviewer_row",
)

# Response category heuristics. These are suggestions for the user to
# consider, not decisions by the loop.
_RESPONSE_RULE_ADJUSTMENT = "rule_adjustment"
_RESPONSE_PROMPT_TWEAK = "prompt_tweak"
_RESPONSE_RUBRIC_JUDGMENT_CALL = "rubric_judgment_call"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------


# Normalized justification-reason classes (2026-07 digest hygiene).
# Reviewer side — parsed from the free-text justification; POC-3 side —
# parsed from the ``nachos_justification`` rule/adjustment tokens.
_REVIEWER_REASON_NECESSARY = "necessary"
_REVIEWER_REASON_UNNECESSARY = "unnecessary"
_REVIEWER_REASON_NA = "na"
_REVIEWER_REASON_OTHER = "other"

_POC3_REASON_UNNECESSARY = "unnecessary_ext"
_POC3_REASON_NECESSARY = "necessary_ext"
_POC3_REASON_FIDELITY = "fidelity_fold"
_POC3_REASON_NONE = "none"

# "necessary_ext" is a substring of "unnecessary_ext" — guard with a
# negative lookbehind so the necessary probe can't fire on the
# unnecessary token.
_POC3_UNNECESSARY_RE = re.compile(r"\bunnecessary_ext\b")
_POC3_NECESSARY_RE = re.compile(r"(?<!un)necessary_ext\b")
_POC3_FIDELITY_RE = re.compile(r"fidelity_divergent")


def _reviewer_reason(justification: str | None) -> str:
    """Normalize a reviewer justification to a coarse reason class.

    The reviewer file writes adjustment rationale as boilerplate
    prefixes ("0.5 Necessary extension", "1 Unnecessary extension") or
    the literal "NA". Check unnecessary FIRST — "unnecessary extension"
    contains "necessary extension" as a substring.
    """
    if justification is None or justification.strip().upper() in {"", "NA", "N/A"}:
        return _REVIEWER_REASON_NA
    lowered = justification.lower()
    if "unnecessary extension" in lowered:
        return _REVIEWER_REASON_UNNECESSARY
    if "necessary extension" in lowered:
        return _REVIEWER_REASON_NECESSARY
    return _REVIEWER_REASON_OTHER


def _poc3_reason(justification: str | None) -> str:
    """Normalize a POC-3 ``nachos_justification`` to a reason class.

    Necessity tokens dominate: a row carrying both ``unnecessary_ext``
    and a fidelity fold classifies as unnecessary — the necessity axis
    is the disagreement surface the reason pair exists to expose.
    ``fidelity_fold`` is reported only when the SF fold is the row's
    sole adjustment vocabulary.
    """
    if not justification:
        return _POC3_REASON_NONE
    if _POC3_UNNECESSARY_RE.search(justification):
        return _POC3_REASON_UNNECESSARY
    if _POC3_NECESSARY_RE.search(justification):
        return _POC3_REASON_NECESSARY
    if _POC3_FIDELITY_RE.search(justification):
        return _POC3_REASON_FIDELITY
    return _POC3_REASON_NONE


# Reason pairs where the two observers apply *different definitions* of
# extension necessity — a rubric decision, not a model defect on either
# side (320-vs-5 direction asymmetry on the 2026-07 source lens).
_NECESSITY_CONFLICT_PAIRS = {
    (_REVIEWER_REASON_NECESSARY, _POC3_REASON_UNNECESSARY),
    (_REVIEWER_REASON_UNNECESSARY, _POC3_REASON_NECESSARY),
}


def _gap_score_bucket(row: ComparisonRow) -> str | None:
    """Score-agreement sub-bucket for a ``gap_row_match`` row.

    ``gap_row_match`` describes the JOIN SURFACE (the reviewer row
    resolved via the spine-anchored gap artifact), not a score
    disagreement — Layer 3 gap rows carry full NACHOS scores, so the
    same tier/adj comparison ``_classify_joined`` applies to direct
    joins is computed here as a derived view. Returns ``None`` for
    non-gap rows; ``"unscored"`` when either side has no tier to
    compare (Layer-2 fallback shape).
    """
    if row.classification != "gap_row_match":
        return None
    if row.reviewer_tier is None or row.poc3_tier is None:
        return "unscored"
    if row.reviewer_tier == row.poc3_tier:
        if (
            row.reviewer_adj is not None
            and row.poc3_adj is not None
            and abs(row.reviewer_adj - row.poc3_adj) < 0.01
        ):
            return "match_exact"
        if row.reviewer_adj is None and row.poc3_adj is None:
            return "match_exact"
        return "match_tier"
    if abs(row.reviewer_tier - row.poc3_tier) == 1:
        return "tier_delta_1"
    return "tier_delta_ge2"


def _display_classification(row: ComparisonRow) -> str:
    """Classification label for clustering/rendering.

    Divergent gap rows surface as ``gap_row_match·<sub-bucket>`` so a
    gap-joined tier disagreement doesn't cluster with (or hide behind)
    the join-surface label alone. Non-gap rows pass through.
    """
    if row.classification == "gap_row_match":
        return f"gap_row_match·{_gap_score_bucket(row)}"
    return row.classification


def _suggested_response_category(row: ComparisonRow) -> str:
    """Heuristic category for a divergence row.

    - ``key_sever_override`` → rubric judgment call (analyst-introduced
      ceiling; POC-3's algorithmic arithmetic doesn't model it).
    - Necessity-conflict reason pairs (reviewer says necessary, POC-3
      says unnecessary — or the reverse) → rubric judgment call: the
      two observers hold different *definitions* of extension
      necessity, so tuning a prompt toward either side would bake one
      rubric choice in silently.
    - Fidelity-fold-only divergence where the reviewer recorded
      nothing (justification NA) → rubric judgment call: the SF fold
      is POC-3's own vocabulary (v12/v24/v25), absent from the
      reviewer's rubric, so these rows diverge by construction.
    - Remaining rows whose justification mentions "extension" →
      prompt tweak (``extension_is_necessary`` is an LLM fact).
    - Everything else → rule adjustment candidate.

    Returning a suggestion does NOT mean POC-3 is wrong. The user
    decides what to change.
    """
    if row.classification == "key_sever_override":
        return _RESPONSE_RUBRIC_JUDGMENT_CALL
    reviewer_reason = _reviewer_reason(row.reviewer_justification)
    poc3_reason = _poc3_reason(row.poc3_justification)
    if (reviewer_reason, poc3_reason) in _NECESSITY_CONFLICT_PAIRS:
        return _RESPONSE_RUBRIC_JUDGMENT_CALL
    if (
        poc3_reason == _POC3_REASON_FIDELITY
        and reviewer_reason == _REVIEWER_REASON_NA
    ):
        return _RESPONSE_RUBRIC_JUDGMENT_CALL
    justification = (row.reviewer_justification or "") + " " + (row.poc3_justification or "")
    if "extension" in justification.lower():
        return _RESPONSE_PROMPT_TWEAK
    return _RESPONSE_RULE_ADJUSTMENT


def _reason_pair(row: ComparisonRow) -> str:
    """Reason-pair sub-clustering key — populated for match_tier shapes.

    ``match_tier`` (same tier, adjustment disagrees) lumps several
    distinct stories under one (state, tier, tier) signature — on the
    2026-07 source lens, TX match_tier 0→0 held 189 rows spanning a
    necessity conflict (129), fidelity-fold-only rows (~31), and a
    remainder. The normalized pair splits them into interpretable
    clusters. Tier-delta buckets keep the tier movement as their
    story: the pair stays blank there.
    """
    if not _display_classification(row).endswith("match_tier"):
        return ""
    return (
        f"R:{_reviewer_reason(row.reviewer_justification)}"
        f" ↔ P:{_poc3_reason(row.poc3_justification)}"
    )


def _pattern_signature(row: ComparisonRow) -> tuple[str, str, str, str, str]:
    """Coarse clustering key for pattern aggregation.

    ``(state, display_classification, reviewer_tier_or_blank,
    poc3_tier_or_blank, reason_pair)`` — fine enough to cluster similar
    divergences but coarse enough to hit meaningful counts. Excludes
    entity/element so the same tier-delta pattern across many rows in
    the same state shows up as a single row in the digest. Gap rows
    cluster by their score sub-bucket; match_tier shapes additionally
    split by the normalized justification reason pair.
    """
    rt = "" if row.reviewer_tier is None else str(row.reviewer_tier)
    pt = "" if row.poc3_tier is None else str(row.poc3_tier)
    return (row.state, _display_classification(row), rt, pt, _reason_pair(row))


def _pattern_weight(count: int, tier_delta: int | None) -> int:
    """Sort-weight used to rank patterns.

    ``count * max(1, |tier_delta|)`` — a high-count zero-delta
    disagreement (e.g. in-scope mismatches) gets the same weight as a
    tier-1 delta with the same count, but a tier-2 delta wins over a
    tier-1 delta at equal count.
    """
    delta = 1 if tier_delta is None else max(1, abs(tier_delta))
    return count * delta


def _divergent_rows(rows: Iterable[ComparisonRow]) -> list[ComparisonRow]:
    """Keep only rows where reviewer and POC-3 actually disagree on a score.

    Excludes:

    - ``match_exact`` (agreement) and ``no_reviewer_row`` (POC-3 row
      with no reviewer opinion — not a disagreement, just uncovered).
    - ``no_poc3_row`` — join-coverage gap, not a rubric disagreement;
      surfaces in the coverage section instead (pre-2026-07 these
      burned top-10 slots with misleading response-category hints).
    - ``gap_row_match`` rows whose score sub-bucket is ``match_exact``
      (both tier AND adjustment agree — agreement via the gap join
      path; 104/105 of the MN source cluster that ranked #3) or
      ``unscored`` (nothing to compare; routed to coverage).
    """
    skip = {"match_exact", "no_reviewer_row", "no_poc3_row"}
    out: list[ComparisonRow] = []
    for r in rows:
        if r.classification in skip:
            continue
        if r.classification == "gap_row_match" and _gap_score_bucket(r) in {
            "match_exact",
            "unscored",
        }:
            continue
        out.append(r)
    return out


def _coverage_rows(rows: Iterable[ComparisonRow]) -> list[ComparisonRow]:
    """Reviewer rows with no comparable POC-3 score.

    ``no_poc3_row`` (no join at all) plus unscored ``gap_row_match``
    rows (gap join resolved but the gap sidecar carries no score —
    Layer-2 fallback shape). These are coverage facts, not rubric
    divergence.
    """
    out: list[ComparisonRow] = []
    for r in rows:
        if r.classification == "no_poc3_row":
            out.append(r)
        elif (
            r.classification == "gap_row_match"
            and _gap_score_bucket(r) == "unscored"
        ):
            out.append(r)
    return out


def top_divergence_patterns(
    rows: list[ComparisonRow], top_n: int = 10
) -> list[dict[str, Any]]:
    """Compute the top-N divergence patterns by weighted count.

    Each returned dict carries:

    - ``signature`` — the (state, classification, r_tier, p_tier,
      reason_pair) key. Gap rows carry ``gap_row_match·<sub-bucket>``
      classifications; ``reason_pair`` is populated for match_tier
      shapes only (``None`` otherwise).
    - ``count`` — rows matching the signature.
    - ``weight`` — ``count * max(1, |tier_delta|)``.
    - ``example`` — concrete (entity, element, justifications) from the
      signature's first row. Prefer rows with both justifications
      populated over blank-justification rows, for readability.
    - ``suggested_response`` — heuristic category.
    """
    divergent = _divergent_rows(rows)
    by_signature: dict[tuple[str, str, str, str, str], list[ComparisonRow]] = defaultdict(list)
    for row in divergent:
        by_signature[_pattern_signature(row)].append(row)

    patterns: list[dict[str, Any]] = []
    for signature, cluster in by_signature.items():
        count = len(cluster)
        # Use the first row's tier_delta (they should all match the
        # signature) to compute weight. If both tiers are None, delta
        # is None and weight falls back to ``count`` via _pattern_weight.
        rep = cluster[0]
        weight = _pattern_weight(count, rep.tier_delta)
        # Pick the most informative example: prefer rows where both
        # justifications are populated so the worked-example section
        # doesn't print a pair of blanks.
        example = next(
            (r for r in cluster if r.reviewer_justification and r.poc3_justification),
            rep,
        )
        patterns.append(
            {
                "signature": {
                    "state": signature[0],
                    "classification": signature[1],
                    "reviewer_tier": int(signature[2]) if signature[2] else None,
                    "poc3_tier": int(signature[3]) if signature[3] else None,
                    "reason_pair": signature[4] or None,
                },
                "count": count,
                "weight": weight,
                "suggested_response": _suggested_response_category(example),
                "example": {
                    "entity": example.reviewer_entity or example.poc3_record_key,
                    "element": example.reviewer_element
                    or (example.poc3_record_key or "").split("|", 2)[-1],
                    "record_key": example.poc3_record_key,
                    "reviewer_tier": example.reviewer_tier,
                    "reviewer_adj": example.reviewer_adj,
                    "reviewer_justification": example.reviewer_justification,
                    "poc3_tier": example.poc3_tier,
                    "poc3_adj": example.poc3_adj,
                    "poc3_justification": example.poc3_justification,
                    "poc3_in_scope": example.poc3_in_scope,
                },
            }
        )

    patterns.sort(key=lambda p: (-p["weight"], -p["count"], p["signature"]["state"]))
    return patterns[:top_n]


def coverage_patterns(rows: list[ComparisonRow]) -> list[dict[str, Any]]:
    """Per-state clusters of coverage-gap rows (see ``_coverage_rows``).

    These deliberately carry NO ``suggested_response`` — a response
    category on a join-coverage gap reads as a scoring to-do when the
    row is a documentation/enumeration fact (the pre-2026-07 digest
    tagged a 274-row MN coverage cluster ``prompt_tweak``). Counts and
    entity roll-ups only; the durable triage of these populations
    lives in the reviewer-comparison caveats + the June follow-up
    docs, not here.
    """
    coverage = _coverage_rows(rows)
    by_state: dict[str, list[ComparisonRow]] = defaultdict(list)
    for row in coverage:
        by_state[row.state].append(row)

    out: list[dict[str, Any]] = []
    for state in sorted(by_state):
        cluster = by_state[state]
        entity_counts = Counter(r.reviewer_entity or "?" for r in cluster)
        buckets = Counter(
            "no_poc3_row" if r.classification == "no_poc3_row" else "gap_row_unscored"
            for r in cluster
        )
        example = next(
            (r for r in cluster if r.reviewer_entity and r.reviewer_element),
            cluster[0],
        )
        out.append(
            {
                "state": state,
                "count": len(cluster),
                "buckets": dict(buckets),
                "top_entities": [
                    {"entity": entity, "count": count}
                    for entity, count in entity_counts.most_common(3)
                ],
                "example": {
                    "entity": example.reviewer_entity,
                    "element": example.reviewer_element,
                },
            }
        )
    out.sort(key=lambda c: (-c["count"], c["state"]))
    return out


def pick_worked_examples(
    top_patterns: list[dict[str, Any]],
    all_patterns: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Pick one example per response-category bucket.

    Returns up to three entries (one per category) in this fixed order:
    prompt tweak, rule adjustment, rubric judgment call. Prefers the
    top-ranked pattern for each category, but falls back to the
    highest-weighted pattern from ``all_patterns`` when a category
    isn't represented in the top-N slice. This keeps the
    ``rubric_judgment_call`` slot populated even when key-sever
    overrides don't dominate the divergence ranking.

    Absence of a category means the digest has zero divergence rows
    matching that suggestion heuristic — that's the finding, not a bug.
    """
    order = [_RESPONSE_PROMPT_TWEAK, _RESPONSE_RULE_ADJUSTMENT, _RESPONSE_RUBRIC_JUDGMENT_CALL]
    by_category: dict[str, dict[str, Any]] = {}
    for pattern in top_patterns:
        cat = pattern["suggested_response"]
        if cat not in by_category:
            by_category[cat] = pattern
    if all_patterns is not None:
        for pattern in all_patterns:
            cat = pattern["suggested_response"]
            if cat not in by_category:
                by_category[cat] = pattern
    return [by_category[cat] for cat in order if cat in by_category]


# ---------------------------------------------------------------------------
# Digest assembly
# ---------------------------------------------------------------------------


def build_digest(
    rows: list[ComparisonRow], lens: str, top_n: int = 10
) -> dict[str, Any]:
    """Top-level digest structure for the given lens's comparison rows."""
    per_state_stats: dict[str, dict[str, Any]] = {}
    for state in IN_SCOPE_STATES:
        state_rows = [r for r in rows if r.state == state]
        matched, total, pct = match_rate(state_rows)
        per_state_stats[state] = {
            "reviewer_rows": total,
            "matched": matched,
            "match_pct": pct,
            "classification_counts": {
                bucket: classification_counts(state_rows).get(bucket, 0)
                for bucket in _BUCKET_ORDER
            },
        }

    all_patterns = top_divergence_patterns(rows, top_n=10**6)
    patterns = all_patterns[:top_n]
    worked = pick_worked_examples(patterns, all_patterns=all_patterns)
    coverage = coverage_patterns(rows)

    # Gap-agreement rollup — how much of the gap_row_match bucket is
    # agreement-by-another-join-path vs genuine score divergence. The
    # classification-breakdown table keeps the raw bucket totals for
    # continuity; this footnote-feeding stat explains why those totals
    # don't reappear one-for-one in the divergence ranking.
    gap_buckets = Counter(
        _gap_score_bucket(r) for r in rows if r.classification == "gap_row_match"
    )
    gap_agreement = {
        "total": sum(gap_buckets.values()),
        "agree_exact": gap_buckets.get("match_exact", 0),
        "unscored": gap_buckets.get("unscored", 0),
        "divergent": sum(
            n
            for bucket, n in gap_buckets.items()
            if bucket not in {"match_exact", "unscored"}
        ),
    }

    total_matched, total_reviewer, total_pct = match_rate(rows)

    return {
        "generated_at": _now_iso(),
        "lens": lens,
        "states": list(IN_SCOPE_STATES),
        "overall": {
            "reviewer_rows": total_reviewer,
            "matched": total_matched,
            "match_pct": total_pct,
            "classification_counts": {
                bucket: classification_counts(rows).get(bucket, 0)
                for bucket in _BUCKET_ORDER
            },
        },
        "per_state": per_state_stats,
        "gap_agreement": gap_agreement,
        "top_patterns": patterns,
        "coverage_patterns": coverage,
        "worked_examples": worked,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_pct(matched: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{matched / total * 100:.1f}%"


def _fmt_justification(text: str | None) -> str:
    """Inline-safe justification for MD tables.

    Justifications carry newlines + pipes that would break a table
    row. Collapse to single-line and escape pipes.
    """
    if not text:
        return "—"
    collapsed = " / ".join(line.strip() for line in text.splitlines() if line.strip())
    return collapsed.replace("|", "\\|")


def _fmt_tier(value: int | None) -> str:
    return "—" if value is None else str(value)


def _fmt_adj(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def render_digest_md(digest: dict[str, Any]) -> str:
    """Render the digest to the stakeholder-visible Markdown page.

    Kept pure — no filesystem side effects. ``run()`` handles the
    write.
    """
    lines: list[str] = []
    lens = digest["lens"]
    lines.append(f"# Reviewer-vs-POC-3 Divergence Digest — `{lens}` lens")
    lines.append("")
    lines.append(
        "_Independent-review comparison — **human-scored framing**. "
        "The reviewer is one observer; POC-3 is another. This digest "
        "names where they diverge; it does NOT assert correctness on "
        "either side._"
    )
    lines.append("")
    lines.append(f"- Generated: `{digest['generated_at']}`")
    lines.append(f"- States: {', '.join(digest['states'])}")
    lines.append(
        f"- Reviewer-originated rows: **{digest['overall']['reviewer_rows']:,}** · "
        f"matched to POC-3: **{digest['overall']['matched']:,}** "
        f"({digest['overall']['match_pct']:.1f}%)"
    )
    lines.append("")

    # --- Match-rate table ------------------------------------------------
    lines.append("## Match rates")
    lines.append("")
    lines.append("| State | Reviewer rows | Matched | Match % |")
    lines.append("|---|---:|---:|---:|")
    for state, stats in digest["per_state"].items():
        lines.append(
            f"| {state} | {stats['reviewer_rows']:,} | {stats['matched']:,} | "
            f"{stats['match_pct']:.1f}% |"
        )
    lines.append("")

    # --- Classification breakdown ---------------------------------------
    lines.append("## Classification breakdown")
    lines.append("")
    header = "| State | " + " | ".join(_BUCKET_ORDER) + " |"
    sep = "|---|" + "|".join(["---:"] * len(_BUCKET_ORDER)) + "|"
    lines.append(header)
    lines.append(sep)
    for state in digest["states"]:
        counts = digest["per_state"][state]["classification_counts"]
        row = [state] + [f"{counts.get(b, 0):,}" for b in _BUCKET_ORDER]
        lines.append("| " + " | ".join(row) + " |")
    total_counts = digest["overall"]["classification_counts"]
    total_row = ["**total**"] + [f"{total_counts.get(b, 0):,}" for b in _BUCKET_ORDER]
    lines.append("| " + " | ".join(total_row) + " |")
    lines.append("")

    gap_agreement = digest.get("gap_agreement") or {}
    if gap_agreement.get("total"):
        note = (
            f"_`gap_row_match` labels the join surface (spine-anchored gap "
            f"slot), not a score disagreement: "
            f"**{gap_agreement['agree_exact']:,} of "
            f"{gap_agreement['total']:,}** gap rows agree exactly on tier + "
            f"adjustment and are excluded from the divergence ranking "
            f"below; {gap_agreement['divergent']:,} genuinely diverge and "
            f"rank as `gap_row_match·<sub-bucket>`"
        )
        if gap_agreement.get("unscored"):
            note += (
                f"; {gap_agreement['unscored']:,} carry no comparable "
                f"score and are listed under coverage gaps"
            )
        lines.append(note + "._")
        lines.append("")

    # --- Top patterns ---------------------------------------------------
    lines.append("## Top rubric-divergence patterns")
    lines.append("")
    lines.append(
        "_Ranked by ``count × max(1, |tier_delta|)``. Each row is a "
        "cluster of rows with the same (state, classification, reviewer "
        "tier, POC-3 tier, reason pair) signature. Only score "
        "disagreements rank here — agreement rows (including gap rows "
        "that agree exactly) and coverage gaps (`no_poc3_row`, unscored "
        "gap rows — see the coverage section) never appear. The "
        "**suggested response** is a heuristic hint (prompt tweak / rule "
        "adjustment / rubric judgment call) — the user decides._"
    )
    lines.append("")
    lines.append(
        "| # | State | Classification | Reason pair | Reviewer tier → POC-3 tier | Rows | Weight | Suggested | Example |"
    )
    lines.append("|---:|---|---|---|---:|---:|---:|---|---|")
    for idx, pat in enumerate(digest["top_patterns"], 1):
        sig = pat["signature"]
        example = pat["example"]
        example_label = (
            f"`{example['entity']}|{example['element']}`"
            if example["entity"]
            else "—"
        )
        tier_label = (
            f"{_fmt_tier(sig['reviewer_tier'])} → {_fmt_tier(sig['poc3_tier'])}"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    str(idx),
                    sig["state"],
                    sig["classification"],
                    sig.get("reason_pair") or "—",
                    tier_label,
                    f"{pat['count']:,}",
                    f"{pat['weight']:,}",
                    pat["suggested_response"],
                    example_label,
                ]
            )
            + " |"
        )
    lines.append("")

    # --- Coverage gaps ---------------------------------------------------
    lines.append("## Coverage gaps — not rubric divergence")
    lines.append("")
    lines.append(
        "_Reviewer rows with no comparable POC-3 score: `no_poc3_row` "
        "(no join at all) plus gap-joined rows the gap sidecar never "
        "scored. These are join-coverage facts — documentation and "
        "enumeration gaps, deferred gap-surfacer follow-ups, or rows "
        "outside the swagger spine — NOT scoring disagreements, so no "
        "response category applies. Population triage lives in "
        "`docs/reviewer-comparison.md` (see the `no_poc3_row` caveat) "
        "and the June follow-up analyses; on the spine lens this bucket "
        "additionally holds present-but-unscored swagger-backfill / "
        "leaf-borrow rows (v21 posture)._"
    )
    lines.append("")
    coverage = digest.get("coverage_patterns") or []
    if not coverage:
        lines.append("_No coverage-gap rows in this lens._")
        lines.append("")
    else:
        lines.append("| State | Rows | Buckets | Top entities | Example |")
        lines.append("|---|---:|---|---|---|")
        for cluster in coverage:
            buckets_label = " · ".join(
                f"{bucket} {count:,}"
                for bucket, count in sorted(cluster["buckets"].items())
            )
            entities_label = " · ".join(
                f"`{e['entity']}` ({e['count']})"
                for e in cluster["top_entities"]
            )
            ex = cluster["example"]
            example_label = (
                f"`{ex['entity']}|{ex['element']}`" if ex.get("entity") else "—"
            )
            lines.append(
                f"| {cluster['state']} | {cluster['count']:,} | "
                f"{buckets_label} | {entities_label} | {example_label} |"
            )
        lines.append("")

    # --- Worked examples ------------------------------------------------
    lines.append("## Worked examples")
    lines.append("")
    if not digest["worked_examples"]:
        lines.append(
            "_No divergence patterns surfaced in this lens — digest reports "
            "agreement-by-construction or full coverage absence._"
        )
        lines.append("")
    for pat in digest["worked_examples"]:
        ex = pat["example"]
        lines.append(f"### `{pat['suggested_response']}` — {ex['entity']} | {ex['element']}")
        lines.append("")
        lines.append(f"- **State:** {pat['signature']['state']} · lens: `{lens}`")
        lines.append(f"- **Classification:** `{pat['signature']['classification']}`")
        lines.append(
            f"- **Reviewer:** tier={_fmt_tier(ex['reviewer_tier'])} · "
            f"adj={_fmt_adj(ex['reviewer_adj'])}"
        )
        lines.append(
            f"- **POC-3:** tier={_fmt_tier(ex['poc3_tier'])} · "
            f"adj={_fmt_adj(ex['poc3_adj'])} · "
            f"in_scope={ex['poc3_in_scope']}"
        )
        lines.append(f"- **Reviewer justification:** {_fmt_justification(ex['reviewer_justification'])}")
        lines.append(f"- **POC-3 justification:** {_fmt_justification(ex['poc3_justification'])}")
        lines.append(f"- **Pattern count:** {pat['count']} rows share this signature")
        lines.append("")

    # --- Appendix drill-down -------------------------------------------
    lines.append("## Appendix — per-state classification drill-down")
    lines.append("")
    lines.append(
        "_Full bucket totals by state. Counts also appear in the "
        "JSON companion (``review_digest_{lens}.json``) for "
        "downstream automation._"
    )
    lines.append("")
    for state in digest["states"]:
        counts = digest["per_state"][state]["classification_counts"]
        matched = digest["per_state"][state]["matched"]
        total = digest["per_state"][state]["reviewer_rows"]
        lines.append(f"### {state}")
        lines.append("")
        lines.append(f"- Reviewer rows: {total:,} · matched: {matched:,} ({_fmt_pct(matched, total)})")
        lines.append("- Buckets:")
        for bucket in _BUCKET_ORDER:
            lines.append(f"  - `{bucket}`: {counts.get(bucket, 0):,}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def comparison_row_to_dict(row: ComparisonRow) -> dict[str, Any]:
    """JSON-friendly dump of a ComparisonRow + digest-derived fields.

    The derived fields are additive (downstream jq consumers keep
    working): ``gap_score_bucket`` (gap rows only — the score-agreement
    sub-bucket), ``reviewer_reason`` / ``poc3_reason`` (the normalized
    justification classes the reason-pair clustering uses).
    """
    payload = asdict(row)
    bucket = _gap_score_bucket(row)
    if bucket is not None:
        payload["gap_score_bucket"] = bucket
    payload["reviewer_reason"] = _reviewer_reason(row.reviewer_justification)
    payload["poc3_reason"] = _poc3_reason(row.poc3_justification)
    return payload


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    *,
    lens: str,
    top_n: int = 10,
    reviewer_dir: Path | None = None,
    sidecar_dir: Path | None = None,
    out_path_md: Path | None = None,
    out_path_json: Path | None = None,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Load reviewer + sidecars for ``lens``, build + write the digest.

    ``reviewer_dir`` overrides the default ``docs/human-scored-files/``
    per-state workbook directory (tests point at a tmp dir).

    Returns the digest dict (same content as the JSON file) for
    programmatic callers. Writes two files:

    - ``data/out/review_digest_{lens}.md`` — stakeholder-visible.
    - ``data/out/review_digest_{lens}.json`` — machine-readable,
      carries every raw comparison row alongside the aggregated
      digest structure.
    """
    if lens not in ("source", "spine"):
        raise ValueError(f"unknown lens {lens!r}; supported: 'source', 'spine'")

    records = load_reviewer_records(reviewer_dir)
    rows = run_comparison(
        reviewer_records=records,
        lens=lens,
        states=IN_SCOPE_STATES,
        sidecar_dir=sidecar_dir,
        allow_stale=allow_stale,
    )
    digest = build_digest(rows, lens=lens, top_n=top_n)

    md_target = out_path_md or (out_dir() / f"review_digest_{lens}.md")
    json_target = out_path_json or (out_dir() / f"review_digest_{lens}.json")

    md_target.parent.mkdir(parents=True, exist_ok=True)
    md_target.write_text(render_digest_md(digest), encoding="utf-8")

    # Attach the raw rows to the JSON payload so consumers can drill
    # into every comparison without re-running the pipeline. Keep the
    # MD page summary-only — adding 1.7K rows to MD would drown it.
    json_payload = dict(digest)
    json_payload["rows"] = [comparison_row_to_dict(row) for row in rows]
    json_target.write_text(
        json.dumps(json_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    _LOGGER.info(
        "wrote %s + %s: %d rows · overall match %.1f%%",
        md_target,
        json_target,
        len(rows),
        digest["overall"]["match_pct"],
    )
    return digest
