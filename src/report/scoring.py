"""Phase D cross-state scoring rollup — JSON + MD dual-write.

Reads the per-state per-record sidecars at
``data/out/{state}_scores_{lens}.json`` (Phase C2) and emits a
stakeholder-visible rollup for a given lens:

- ``data/out/scoring_report_{lens}.json`` — machine-readable rollup
  with per-state + cross-state aggregates.
- ``data/out/scoring_report_{lens}.md`` — human-readable summary.

Rationale (plan §9): Phase C2 shipped per-record scores; leadership
readers need a one-page-per-lens view. The cross-format axis (WI
Confluence vs AZ XLSX vs MN Mapping Matrix vs TX TWEDS) is surfaced
alongside raw means so that "TX 2.0 vs WI 2.5" doesn't read as "TX
docs are 20% worse" — most of that spread is format-driven ceilings
(inherited Phase D scope item #2).

CRITICAL: module-level ``run()`` is a plain function (POC-2 pitfall)
— do NOT decorate with ``@click.command()``. The Click wrapper lives
in ``src/poc3/cli.py``.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

from src.score.rules import SOURCE_DIMENSIONS, SPINE_DIMENSIONS
from src.states import SUPPORTED_STATES as _STATES
from src.utils.paths import out_dir, scoring_report_path, state_scores_path

_LOGGER = logging.getLogger(__name__)

# State → source-format axis (inherited Phase D scope #2). Leadership
# readers see per-format baselines alongside raw means so "TX 2.0 vs
# WI 2.5" doesn't read as a pure quality gap — most of that spread is
# format-driven (entity-level prose vs pipe-table cells vs metadata-
# thin matrix vs TEDS slugs).
STATE_FORMAT: dict[str, str] = {
    "AZ": "xlsx",
    "WI": "confluence",
    "MN": "mapping_matrix",
    "TX": "tweds",
    "IN": "xlsx",  # IDOE Vendor Documentation `API Datastructure` sheet — same parser family as AZ.
}


def _load_sidecar(
    state: str,
    lens: str,
    base: Path | None = None,
    *,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Load ``data/out/{state}_scores_{lens}.json`` as a dict.

    Issue #212 item 3: with a publish lineage, a stale sidecar must not
    silently feed the cross-state rollup (missing/corrupt already
    raise — this loader never swallowed).
    """
    path = (
        (base / f"{state.lower()}_scores_{lens}.json")
        if base is not None
        else state_scores_path(state, lens)  # type: ignore[arg-type]
    )
    from src.publish.manifest import verify_fresh

    verify_fresh(
        path, consumer="report scoring rollup", allow_stale=allow_stale
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _dim_names(lens: str) -> tuple[str, ...]:
    if lens == "spine":
        return SPINE_DIMENSIONS
    if lens == "source":
        return SOURCE_DIMENSIONS
    raise ValueError(f"unknown lens {lens!r}; supported: 'spine', 'source'")


def _review_reason_histogram(sidecar: dict[str, Any]) -> dict[str, int]:
    """Count occurrences of each ``review.reasons[*]`` across a sidecar."""
    from collections import Counter

    counts: Counter[str] = Counter()
    for score in sidecar.get("scores", []):
        for reason in score.get("review", {}).get("reasons", []):
            counts[reason] += 1
    return dict(counts.most_common())


def _build_state_block(state: str, lens: str, sidecar: dict[str, Any]) -> dict[str, Any]:
    scores = sidecar.get("scores", [])
    dim_names = _dim_names(lens)
    dim_stats = sidecar.get("dimension_stats", {})

    # Collected for potential diagnostic use; the MD + JSON rollup emits
    # the sidecar's header-level ``mean_quality_score`` instead, so this
    # list is currently unused. Read under the renamed key for
    # consistency with the sidecar shape.
    per_record_values = [
        s["_quality_mean_diagnostic"]
        for s in scores
        if s.get("_quality_mean_diagnostic") is not None
    ]

    # v23 — issue #106 (Q4): complexity_score on both lenses.
    complexity_values: list[int] = [
        s["complexity_score"]
        for s in scores
        if s.get("complexity_score") is not None
    ]

    # Phase F — NACHOS methodology surfaces. Read straight from the
    # sidecar header aggregates (aggregate.py writes them restricted to
    # in-scope rows, which is the population methodology actually
    # scores).
    adjusted_hist = sidecar.get("adjusted_nachos_score_histogram") or {}

    return {
        "state": state,
        "source_format": STATE_FORMAT[state],
        "record_count": sidecar.get("record_count", len(scores)),
        "scored_count": sidecar.get("scored_count", len(scores)),
        "edfi_version": sidecar.get("edfi_version"),
        "mean_quality_score": sidecar.get("mean_quality_score"),
        "mean_complexity_score": _mean([float(v) for v in complexity_values]),
        "needs_review_count": sidecar.get("needs_review_count", 0),
        "review_rate_pct": round(
            100.0 * sidecar.get("needs_review_count", 0) / max(len(scores), 1),
            2,
        ),
        "dimension_stats": {d: dim_stats.get(d, {}) for d in dim_names},
        "review_reason_histogram": _review_reason_histogram(sidecar),
        # Phase F.
        "in_scope_count": sidecar.get("in_scope_count", 0),
        "out_of_scope_count": (
            sidecar.get("scored_count", len(scores))
            - sidecar.get("in_scope_count", 0)
        ),
        "mean_nachos_score": sidecar.get("mean_nachos_score"),
        "mean_adjusted_nachos_score": sidecar.get("mean_adjusted_nachos_score"),
        "nachos_score_histogram": sidecar.get("nachos_score_histogram") or {},
        "adjusted_nachos_score_histogram": adjusted_hist,
    }


def _per_format_baselines(blocks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group states by ``source_format`` and compute per-format means.

    Leadership read: cross-format comparisons need interpretation —
    state formats have structurally different ceilings. Phase C2 audit
    carryover #2 (working journal 2026-04-26): "WI Confluence (rich
    prose) vs AZ XLSX (terse cells) vs MN Mapping Matrix (metadata-thin)
    vs TX TWEDS (entity-level prose) have structurally different ceilings".
    One-state-per-format is still informative — the "baseline" is just
    the one measurement point — but anything flagged "X vs peer" tells
    the reader to compare states inside their format, not across.
    """
    by_format: dict[str, list[dict[str, Any]]] = {}
    for b in blocks:
        by_format.setdefault(b["source_format"], []).append(b)
    out: dict[str, dict[str, Any]] = {}
    for fmt, members in by_format.items():
        quality_values = [
            m["mean_quality_score"]
            for m in members
            if m["mean_quality_score"] is not None
        ]
        out[fmt] = {
            "states": [m["state"] for m in members],
            "member_count": len(members),
            "mean_quality_score": _mean(quality_values),
        }
    return out


def _cross_state_dim_rollup(
    blocks: list[dict[str, Any]], lens: str
) -> dict[str, dict[str, Any]]:
    """Per-dimension mean + count across every state."""
    dim_names = _dim_names(lens)
    out: dict[str, dict[str, Any]] = {}
    for dim in dim_names:
        per_state_means = [
            b["dimension_stats"][dim]["mean"]
            for b in blocks
            if b["dimension_stats"].get(dim, {}).get("mean") is not None
        ]
        total_count = sum(
            b["dimension_stats"].get(dim, {}).get("count", 0) for b in blocks
        )
        out[dim] = {
            "per_state_mean": {
                b["state"]: b["dimension_stats"].get(dim, {}).get("mean")
                for b in blocks
            },
            "cross_state_mean": _mean(per_state_means) if per_state_means else None,
            "total_scored": total_count,
        }
    return out


def _nachos_cross_state_rollup(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Phase F — cross-state NACHOS rollup restricted to in-scope rows.

    Mean nachos / adjusted-nachos are weighted by in_scope_count (the
    population methodology actually scores). Per-tier histogram is
    the simple sum across states. Per-format baselines follow the
    quality-score pattern: methodology scoring distributions differ
    by source format (prose-rich Confluence vs metadata-thin Mapping
    Matrix), so grouping by format keeps the cross-state comparisons
    honest.
    """
    in_scope_total = sum(b.get("in_scope_count", 0) for b in blocks)
    out_of_scope_total = sum(b.get("out_of_scope_count", 0) for b in blocks)

    def _weighted(field: str) -> float | None:
        # Shared weighting core (issue #213 item 1) — this closure and
        # the analyst Score Card's cross-state aggregators used to
        # implement the same arithmetic independently.
        from src.report.score_card import weighted_mean

        m = weighted_mean([
            (float(v), b.get("in_scope_count", 0))
            for b in blocks
            if isinstance(v := b.get(field), (int, float))
        ])
        return round(m, 4) if m is not None else None

    # Sum per-tier histograms.
    tier_hist: dict[str, int] = {"0": 0, "1": 0, "2": 0, "3": 0}
    for b in blocks:
        h = b.get("nachos_score_histogram") or {}
        for tier, count in h.items():
            tier_hist[str(tier)] = tier_hist.get(str(tier), 0) + count

    # Sum per-bucket adjusted histograms (0.0..4.5 by 0.5).
    adj_hist: dict[str, int] = {
        f"{b:.1f}": 0 for b in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5)
    }
    for b in blocks:
        h = b.get("adjusted_nachos_score_histogram") or {}
        for bucket, count in h.items():
            adj_hist[bucket] = adj_hist.get(bucket, 0) + count

    # Per-format grouping.
    by_format: dict[str, list[dict[str, Any]]] = {}
    for b in blocks:
        by_format.setdefault(b["source_format"], []).append(b)
    per_format: dict[str, dict[str, Any]] = {}
    for fmt, members in by_format.items():
        fmt_in_scope_total = sum(m.get("in_scope_count", 0) for m in members)

        def _fmt_weighted(field: str, members=members) -> float | None:
            num = 0.0
            denom = 0
            for m in members:
                v = m.get(field)
                w = m.get("in_scope_count", 0)
                if isinstance(v, (int, float)) and w > 0:
                    num += v * w
                    denom += w
            return round(num / denom, 4) if denom else None

        per_format[fmt] = {
            "states": [m["state"] for m in members],
            "member_count": len(members),
            "in_scope_count": fmt_in_scope_total,
            "mean_nachos_score": _fmt_weighted("mean_nachos_score"),
            "mean_adjusted_nachos_score": _fmt_weighted(
                "mean_adjusted_nachos_score"
            ),
        }

    return {
        "in_scope_total": in_scope_total,
        "out_of_scope_total": out_of_scope_total,
        "cross_state_mean_nachos_score": _weighted("mean_nachos_score"),
        "cross_state_mean_adjusted_nachos_score": _weighted(
            "mean_adjusted_nachos_score"
        ),
        "nachos_tier_histogram": tier_hist,
        "adjusted_nachos_score_histogram": adj_hist,
        "per_format": per_format,
    }


def build_report(
    lens: str,
    states: tuple[str, ...] = _STATES,
    base: Path | None = None,
    *,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Assemble the rollup dict without writing to disk.

    ``base`` overrides ``data/out`` — tests point at a ``tmp_path``.
    """
    sidecars = [
        _load_sidecar(s, lens, base=base, allow_stale=allow_stale)
        for s in states
    ]
    blocks = [
        _build_state_block(s, lens, sidecar)
        for s, sidecar in zip(states, sidecars)
    ]

    # Plan-version stamp = consensus of the sidecars actually summarized
    # (mirrors reviewer_comparison_summary._detect_plan_version) — the
    # report tells the truth about its inputs instead of hardcoding a
    # literal (which sat at "2" for 25 version bumps; issue #211 item 1b).
    # Disagreement or absence → None, never a guess.
    versions = {
        str(v)
        for v in (sc.get("scoring_plan_version") for sc in sidecars)
        if v is not None
    }
    plan_version = next(iter(versions)) if len(versions) == 1 else None

    quality_values = [
        b["mean_quality_score"] for b in blocks if b["mean_quality_score"] is not None
    ]
    cross_state_mean_quality = _mean(quality_values)
    review_total = sum(b["needs_review_count"] for b in blocks)
    record_total = sum(b["record_count"] for b in blocks)

    return {
        "generated_on": date.today().isoformat(),
        "lens": lens,
        "dimensions": list(_dim_names(lens)),
        "per_state": blocks,
        "per_format_baselines": _per_format_baselines(blocks),
        "cross_state": {
            "state_count": len(blocks),
            "record_total": record_total,
            "review_total": review_total,
            "cross_state_mean_quality": cross_state_mean_quality,
            "cross_state_mean_quality_range": (
                round(max(quality_values) - min(quality_values), 4)
                if quality_values
                else None
            ),
            "dimension_rollup": _cross_state_dim_rollup(blocks, lens),
        },
        # Phase F — methodology rollup restricted to in-scope rows.
        "nachos": _nachos_cross_state_rollup(blocks),
        "scoring_plan_version": plan_version,
        "guidance": (
            "Cross-format comparisons require interpretation — states "
            "with richer source formats (Confluence prose, entity-level "
            "TWEDS narrative) land structurally higher on definition "
            "quality than metadata-thin formats (XLSX, Mapping Matrix). "
            "Prefer comparing states within their source_format peer "
            "group (per_format_baselines); the raw cross-state mean is "
            "a coverage snapshot, not an ordinal ranking."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lens = report["lens"]
    lines: list[str] = [
        f"# Cross-state scoring rollup — {lens}-lens",
        "",
        f"Generated on: {report['generated_on']}.",
        f"Lens: **{lens}** — dimensions: {', '.join(report['dimensions'])}.",
        "",
        report["guidance"],
        "",
        "## Per-state summary",
        "",
        "| State | Format | Records | Mean quality | Review flags | Review % |",
        "| :- | :- | -: | -: | -: | -: |",
    ]
    for b in report["per_state"]:
        q = b["mean_quality_score"]
        q_str = "n/a" if q is None else f"{q:.2f}"
        lines.append(
            f"| {b['state']} | {b['source_format']} | {b['record_count']:,} | "
            f"{q_str} | {b['needs_review_count']:,} | {b['review_rate_pct']:.1f}% |"
        )
    lines.extend([
        "",
        "## Per-format baselines",
        "",
        "States with the same ``source_format`` share structural ceilings "
        "(format-aware gates session, 2026-04-27). Use these to compare "
        "states *within* their peer group rather than reading raw "
        "cross-state means as quality rankings.",
        "",
        "| Format | States | Member count | Mean quality |",
        "| :- | :- | -: | -: |",
    ])
    for fmt, info in report["per_format_baselines"].items():
        q = info["mean_quality_score"]
        q_str = "n/a" if q is None else f"{q:.2f}"
        lines.append(
            f"| {fmt} | {', '.join(info['states'])} | "
            f"{info['member_count']} | {q_str} |"
        )
    lines.extend([
        "",
        "## Per-dimension rollup",
        "",
        "| Dimension | " + " | ".join(b["state"] for b in report["per_state"]) + " | Cross-state mean |",
        "| :- |" + " -: |" * (len(report["per_state"]) + 1),
    ])
    for dim, roll in report["cross_state"]["dimension_rollup"].items():
        row = [f"| {dim} |"]
        for b in report["per_state"]:
            v = roll["per_state_mean"].get(b["state"])
            row.append(" n/a |" if v is None else f" {v:.2f} |")
        csm = roll["cross_state_mean"]
        row.append(" n/a |" if csm is None else f" {csm:.2f} |")
        lines.append("".join(row))

    lines.extend(["", "## Review-queue reason histograms", ""])
    for b in report["per_state"]:
        lines.append(f"### {b['state']} ({b['needs_review_count']} flagged rows)")
        lines.append("")
        hist = b["review_reason_histogram"]
        if not hist:
            lines.append("_No review reasons — nothing flagged._")
            lines.append("")
            continue
        lines.append("| Reason | Count |")
        lines.append("| :- | -: |")
        for reason, n in hist.items():
            lines.append(f"| {reason} | {n:,} |")
        lines.append("")

    cs = report["cross_state"]
    csm = cs["cross_state_mean_quality"]
    csm_str = "n/a" if csm is None else f"{csm:.2f}"
    rng = cs["cross_state_mean_quality_range"]
    rng_str = "n/a" if rng is None else f"{rng:.2f}"
    lines.extend([
        "## Cross-state aggregate",
        "",
        f"- States: **{cs['state_count']}**",
        f"- Records scored: **{cs['record_total']:,}**",
        f"- Review flags total: **{cs['review_total']:,}**",
        f"- Cross-state mean quality: **{csm_str}** (spread {rng_str} high–low)",
        "",
    ])

    # Phase F — NACHOS methodology section.
    nachos = report.get("nachos") or {}
    lines.extend([
        "## NACHOS",
        "",
        "Methodology-conformant NACHOS score (`docs/NACHOS_Methodology_"
        "External review.xlsx`, Dec 2025 revision): business-logic "
        "complexity tier 0..3, with Adjusted NACHOS = base + extension/"
        "multi-entity adjustments, capped at 4.5. Numbers below are "
        "restricted to **in-scope rows** — the population methodology "
        "actually scores (unnecessary extensions, aggregation/"
        "concatenation, multi-entity calculations, core rows with "
        "conditional logic). Granular elements, pure descriptor lookups, "
        "and necessary extensions without logic are out-of-scope by "
        "design.",
        "",
        "Cross-format NACHOS comparisons need the same interpretation "
        "caveat the quality-score section carries — prose-rich state "
        "source formats land structurally higher on tier 3 "
        "(SUM/CONCAT/multi-entity language is more detectable) than "
        "metadata-thin formats. Prefer comparing inside format peer "
        "groups (per-format table below).",
        "",
        "### Per-state NACHOS",
        "",
        "| State | Format | Scored | In-scope | Mean NACHOS | Mean Adj NACHOS | tier 0/1/2/3 |",
        "| :- | :- | -: | -: | -: | -: | :- |",
    ])
    for b in report["per_state"]:
        in_scope_count = b.get("in_scope_count", 0)
        n_mean = b.get("mean_nachos_score")
        a_mean = b.get("mean_adjusted_nachos_score")
        n_str = "n/a" if n_mean is None else f"{n_mean:.2f}"
        a_str = "n/a" if a_mean is None else f"{a_mean:.2f}"
        hist = b.get("nachos_score_histogram") or {}
        h_str = (
            f"{hist.get('0', 0)}/{hist.get('1', 0)}/"
            f"{hist.get('2', 0)}/{hist.get('3', 0)}"
        )
        lines.append(
            f"| {b['state']} | {b['source_format']} | "
            f"{b['scored_count']:,} | {in_scope_count:,} | {n_str} | "
            f"{a_str} | {h_str} |"
        )

    lines.extend([
        "",
        "### Per-format NACHOS baselines",
        "",
        "| Format | States | Member count | In-scope | Mean NACHOS | Mean Adj NACHOS |",
        "| :- | :- | -: | -: | -: | -: |",
    ])
    per_format = (nachos.get("per_format") or {})
    for fmt, info in per_format.items():
        n_mean = info.get("mean_nachos_score")
        a_mean = info.get("mean_adjusted_nachos_score")
        n_str = "n/a" if n_mean is None else f"{n_mean:.2f}"
        a_str = "n/a" if a_mean is None else f"{a_mean:.2f}"
        lines.append(
            f"| {fmt} | {', '.join(info['states'])} | "
            f"{info['member_count']} | {info.get('in_scope_count', 0):,} | "
            f"{n_str} | {a_str} |"
        )

    lines.extend([
        "",
        "### Cross-state NACHOS aggregate",
        "",
        f"- In-scope rows total: **{nachos.get('in_scope_total', 0):,}**",
        f"- Out-of-scope rows total: **{nachos.get('out_of_scope_total', 0):,}**",
    ])
    csn = nachos.get("cross_state_mean_nachos_score")
    csa = nachos.get("cross_state_mean_adjusted_nachos_score")
    csn_str = "n/a" if csn is None else f"{csn:.2f}"
    csa_str = "n/a" if csa is None else f"{csa:.2f}"
    lines.extend([
        f"- Cross-state mean NACHOS (in-scope, in-scope-weighted): "
        f"**{csn_str}** _(format-confounded — see per-format table)_",
        f"- Cross-state mean Adjusted NACHOS (in-scope-weighted): "
        f"**{csa_str}**",
        "",
        "#### NACHOS tier histogram (all in-scope rows)",
        "",
        "| Tier | Count |",
        "| :- | -: |",
    ])
    tier_hist = nachos.get("nachos_tier_histogram") or {}
    for tier in ("0", "1", "2", "3"):
        lines.append(f"| {tier} | {tier_hist.get(tier, 0):,} |")

    lines.extend([
        "",
        "#### Adjusted NACHOS histogram (all in-scope rows)",
        "",
        "| Bucket | Count |",
        "| :- | -: |",
    ])
    adj_hist = nachos.get("adjusted_nachos_score_histogram") or {}
    for bucket in ("0.0", "0.5", "1.0", "1.5", "2.0", "2.5", "3.0", "3.5", "4.0", "4.5"):
        lines.append(f"| {bucket} | {adj_hist.get(bucket, 0):,} |")
    lines.append("")
    return "\n".join(lines)


def run(
    *,
    lens: str = "spine",
    states: tuple[str, ...] = _STATES,
    out: Path | None = None,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Write ``scoring_report_{lens}.{json,md}``. Returns the report dict.

    Preconditions: each state has ``data/out/{state}_scores_{lens}.json``.
    Regenerate missing sidecars with ``poc3 score aggregate --state X
    --lens {lens}``.
    """
    base = out or out_dir()
    base.mkdir(parents=True, exist_ok=True)
    report = build_report(
        lens, states=states, base=base, allow_stale=allow_stale
    )

    if out is not None:
        json_path = out / f"scoring_report_{lens}.json"
        md_path = out / f"scoring_report_{lens}.md"
    else:
        json_path = scoring_report_path(lens, "json")  # type: ignore[arg-type]
        md_path = scoring_report_path(lens, "md")  # type: ignore[arg-type]

    json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    _LOGGER.info("scoring rollup (%s): wrote %s and %s", lens, json_path, md_path)
    return report
