"""Score Card engine — per-state + combined blocks (issue #213 item 1).

The Score Card sub-language extracted from ``analyst.py``: state
rollups, frequency tables, the metrics block, the Domains × bins
matrix, the Implementation Shape × Documentation Style heatmap, and the
combined (cross-state) block stack. ``analyst`` re-exports the old
private names for its tests; the builders call ``write_score_card`` /
``build_combined_score_card``.

Population discipline (unchanged): every per-state headline block
counts DOCUMENTED rows scored in-scope — the same population as the
sidecar-header histograms — via the ONE shared predicate
``record_in_scope`` (issue #213 item 1: three subtly-different inline
definitions used to gate the headline, the rollup, and the Commitment
Tracker what-if).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from src.models.element import ElementRecord
from src.report.loaders import (
    StateInputs,
    format_source_coverage,
    format_spine_coverage,
    record_key,
)
from src.report.prose import COVERAGE_SEMANTICS_NOTE, SOURCE_SCOPE_BY_STATE
from src.report.workbook_render import (
    _HEADER_FILL,
    _HEADER_FONT,
    _set_cell,
    _set_widths,
)
from src.report.workbook_spec import _legacy_template_fields


def record_in_scope(score: dict) -> bool:
    """THE in-scope predicate for workbook populations.

    v10 made ``in_scope`` universal on every score row, so the default
    only matters for synthetic/test dicts: a row missing the field
    counts as in-scope (the headline population's historical reading).
    Shared by the state rollup, the Score Card headline population, and
    the Commitment Tracker what-if — three inline variants used to
    disagree on the missing-field default (issue #213 item 1).
    """
    return bool(score.get("in_scope", True))


def weighted_mean(pairs: list[tuple[float, int]]) -> float | None:
    """Weighted mean over ``(value, weight)`` pairs; ``None`` when no
    positive weight. The one home for the cross-state weighting
    arithmetic (previously re-implemented here and in
    ``report/scoring.py``)."""
    num = 0.0
    denom = 0
    for v, w in pairs:
        if w > 0:
            num += v * w
            denom += w
    return (num / denom) if denom else None


_SOURCE_DIMENSIONS: tuple[str, ...] = (
    "canonical_name_alignment",
    "definition_quality",
    "semantic_fidelity",
    "extension_justification",
)
_SPINE_DIMENSIONS: tuple[str, ...] = (
    "documentation_completeness",
    "obligation_clarity",
)
_REVIEW_ROUTES: tuple[str, ...] = ("POLICY", "DATA_MODEL", "SCORING", "ANALYST")


def _dimension_names(lens: str) -> tuple[str, ...]:
    return _SPINE_DIMENSIONS if lens == "spine" else _SOURCE_DIMENSIONS


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _compute_state_rollup(
    si: StateInputs,
    scores_for_state: dict[str, dict],
    lens: str,
    adjudications: dict[str, dict] | None = None,
) -> dict | None:
    """Build a POC-2-style rollup for one state (lens-aware).

    Partitions scored records by ``ElementRecord.source`` (core /
    extension / unknown) for demographic counts only. Per-dimension
    means echo the sidecar header's ``dimension_stats.mean`` but are
    recomputed here from paired records (source-filter can drop rows
    the sidecar header counted, so the recompute keeps the rollup
    consistent with what actually lands on the workbook).

    Previously this block also computed ``mean_per_record_score`` and
    core/extension/unknown mean partitions of that scalar. Those were
    dropped in the demote-per-record-score phase — the arithmetic
    mean of mixed-axis dimension tiers was format-confounded and
    invited the same conflation the ``NACHOS score`` column had to
    shed. Per-dimension means (still emitted) are the honest
    diagnostic.

    Returns ``None`` when `scores_for_state` is empty (no sidecar
    loaded) — caller should skip the rollup block entirely.
    """
    if not scores_for_state:
        return None
    core_count = 0
    ext_count = 0
    unknown_count = 0
    scored_count = 0
    complexity_scores: list[int] = []
    needs_review = 0
    route_counts: dict[str, int] = {r: 0 for r in _REVIEW_ROUTES}
    dim_values: dict[str, list[float]] = {d: [] for d in _dimension_names(lens)}
    # Phase F — NACHOS aggregates restricted to in-scope rows (the
    # population methodology actually scores).
    in_scope_count = 0
    nachos_in_scope_values: list[int] = []
    adjusted_in_scope_values: list[float] = []
    nachos_tier_hist = {i: 0 for i in (0, 1, 2, 3)}
    # Issue #248 Part C — the effective aggregate is computed over
    # EXACTLY the population of the engine aggregate it renders beside
    # (fresh-adjudication value where present, else engine adjusted).
    adjudicated_count = 0
    effective_in_scope_values: list[float] = []
    for r in si.elements.elements:
        key = record_key(si.state, r)
        score = scores_for_state.get(key)
        if score is None:
            continue
        scored_count += 1
        if r.source == "core":
            core_count += 1
        elif r.source == "extension":
            ext_count += 1
        elif r.source == "unknown":
            unknown_count += 1
        # v23 — complexity_score on both lenses (issue #106 / Q4).
        cx = score.get("complexity_score")
        if isinstance(cx, (int, float)):
            complexity_scores.append(int(cx))
        dims = score.get("dimensions") or {}
        for dname in dim_values:
            dv = dims.get(dname, {}).get("value")
            if isinstance(dv, (int, float)):
                dim_values[dname].append(float(dv))
        # Phase F NACHOS aggregates — the ONE shared predicate (a row
        # missing the universal field counts in-scope, same as the
        # headline population; production rows always carry it).
        if record_in_scope(score):
            in_scope_count += 1
            nachos_dim = dims.get("nachos_score") or {}
            ntier = nachos_dim.get("value")
            if isinstance(ntier, (int, float)):
                nachos_in_scope_values.append(int(ntier))
                nachos_tier_hist[int(ntier)] = nachos_tier_hist.get(int(ntier), 0) + 1
            adj = score.get("adjusted_nachos_score")
            if isinstance(adj, (int, float)):
                adjusted_in_scope_values.append(float(adj))
                adjudication = (adjudications or {}).get(key)
                if adjudication and adjudication.get("fresh"):
                    adjudicated_count += 1
                    effective_in_scope_values.append(
                        float(adjudication["value"])
                    )
                else:
                    effective_in_scope_values.append(float(adj))
        review = score.get("review") or {}
        if review.get("needs_review"):
            needs_review += 1
            route = review.get("route")
            if route in route_counts:
                route_counts[route] += 1
    return {
        "state": si.state,
        "lens": lens,
        "scored_count": scored_count,
        "record_count": si.elements.element_count,
        "core_count": core_count,
        "extension_count": ext_count,
        "unknown_count": unknown_count,
        "needs_review_count": needs_review,
        "route_counts": route_counts,
        "dimension_means": {
            d: _mean(vs) for d, vs in dim_values.items()
        },
        "dimension_counts": {d: len(vs) for d, vs in dim_values.items()},
        "mean_complexity_score": (
            _mean([float(c) for c in complexity_scores]) if complexity_scores else None
        ),
        # Phase F.
        "in_scope_count": in_scope_count,
        "out_of_scope_count": scored_count - in_scope_count,
        "mean_nachos_score": (
            _mean([float(v) for v in nachos_in_scope_values])
            if nachos_in_scope_values else None
        ),
        "mean_adjusted_nachos_score": (
            _mean(adjusted_in_scope_values)
            if adjusted_in_scope_values else None
        ),
        "nachos_tier_histogram": nachos_tier_hist,
        # Issue #248 Part C — adjudication rollup. Stale blocks are
        # counted from the resolved dict directly (a record-gone stale
        # adjudication never joins the loop above).
        "adjudicated_count": adjudicated_count,
        "adjudicated_stale_count": sum(
            1 for a in (adjudications or {}).values() if not a.get("fresh")
        ),
        "mean_effective_adjusted_score": (
            _mean(effective_in_scope_values)
            if effective_in_scope_values else None
        ),
    }


def _fmt_float(x: float | None, places: int = 2) -> str:
    return "" if x is None else f"{x:.{places}f}"


def _fmt_pct(numer: int, denom: int) -> str:
    if not denom:
        return "0.0%"
    return f"{numer / denom * 100:.1f}%"


def _fmt_pct_float(ratio: float | None) -> str | None:
    """Format a 0-1 ratio as a percent string (Score Card Block B)."""
    if ratio is None:
        return None
    return f"{ratio * 100:.1f}%"


def _write_score_card_rollup(
    ws: Worksheet,
    start_row: int,
    rollup: dict,
    *,
    title: str | None = None,
    include_nachos: bool = True,
) -> int:
    """Append a POC-2-shaped `metric | value` rollup block to Score Card.

    Layout mirrors POC-2's Extension_Analysis sheet — plain two-column
    table, bold metric labels, numeric values in col 2. Returns the
    next free row index so callers can stack additional blocks below.

    ``include_nachos=False`` drops the NACHOS methodology sub-block —
    the Option B per-state Score Card surfaces those numbers in its own
    frequency/metric blocks (documented-only population) instead.
    """
    lens = rollup["lens"]
    row = start_row
    header = ws.cell(
        row=row, column=1,
        value=title or f"Scoring rollup (lens: {lens})"
    )
    header.font = Font(bold=True, size=12)
    row += 1
    ws.cell(row=row, column=1, value="metric").font = _HEADER_FONT
    ws.cell(row=row, column=1).fill = _HEADER_FILL
    ws.cell(row=row, column=2, value="value").font = _HEADER_FONT
    ws.cell(row=row, column=2).fill = _HEADER_FILL
    row += 1

    scored = rollup["scored_count"]
    total = rollup["record_count"]
    entries: list[tuple[str, object]] = [
        ("records_scored", scored),
        ("records_total", total),
    ]
    # v23 — mean_complexity_score on both lenses (issue #106 / Q4).
    if rollup["mean_complexity_score"] is not None:
        entries.append(
            ("mean_complexity_score", _fmt_float(rollup["mean_complexity_score"]))
        )
    entries.extend([
        (
            "needs_review_count",
            f"{rollup['needs_review_count']} ({_fmt_pct(rollup['needs_review_count'], scored)})",
        ),
        ("route_POLICY", rollup["route_counts"]["POLICY"]),
        ("route_DATA_MODEL", rollup["route_counts"]["DATA_MODEL"]),
        ("route_SCORING", rollup["route_counts"]["SCORING"]),
        ("route_ANALYST", rollup["route_counts"]["ANALYST"]),
        ("core_count", rollup["core_count"]),
        ("extension_count", rollup["extension_count"]),
    ])
    if rollup["unknown_count"]:
        entries.append(("unknown_count", rollup["unknown_count"]))
    for dname, dmean in rollup["dimension_means"].items():
        entries.append((f"mean_{dname}", _fmt_float(dmean)))

    for label, value in entries:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=value)
        row += 1

    if not include_nachos:
        return row

    # Phase F — dedicated NACHOS block, separated by a blank row so
    # leadership reads the methodology-axis numbers distinct from the
    # quality-axis metrics above.
    row += 1
    nachos_header = ws.cell(row=row, column=1, value="NACHOS methodology")
    nachos_header.font = Font(bold=True, size=12)
    row += 1
    nachos_entries: list[tuple[str, object]] = [
        ("in_scope_count", rollup["in_scope_count"]),
        ("out_of_scope_count", rollup["out_of_scope_count"]),
        ("mean_nachos_score", _fmt_float(rollup["mean_nachos_score"])),
        (
            "mean_adjusted_nachos_score",
            _fmt_float(rollup["mean_adjusted_nachos_score"]),
        ),
    ]
    # Issue #248 Part C — adjudication rollup beside the engine mean
    # (.get defaults: pre-#248 placeholder rollups lack the keys).
    n_adjudicated = rollup.get("adjudicated_count", 0)
    n_stale = rollup.get("adjudicated_stale_count", 0)
    adjudicated_display = f"{n_adjudicated} of {rollup['in_scope_count']}"
    if n_stale:
        adjudicated_display += f" ({n_stale} stale — re-adjudicate)"
    nachos_entries.append(("adjudicated_rows", adjudicated_display))
    if n_adjudicated:
        nachos_entries.append((
            "mean_effective_adjusted_score",
            _fmt_float(rollup.get("mean_effective_adjusted_score")),
        ))
    hist = rollup["nachos_tier_histogram"]
    for tier in (0, 1, 2, 3):
        nachos_entries.append((f"nachos_tier_{tier}_count", hist.get(tier, 0)))
    for label, value in nachos_entries:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=value)
        row += 1
    return row


def _compute_structural_doc_matrix(
    scores_for_state: dict[str, dict],
) -> tuple[dict[tuple[int, int], int], int]:
    """Bucket records into a 4×4 ``(structural_depth, doc_style_tier)`` matrix.

    Returns ``(matrix, audit_count)`` where ``matrix`` keys are every
    ``(s, d)`` pair with ``s, d ∈ {0, 1, 2, 3}`` (zero-filled so the
    cell always renders) and ``audit_count`` tallies records missing
    either dimension — those go into a footnote, not the matrix.

    The top-left 2×2 (``structural_depth >= 2 AND doc_style_tier <= 1``)
    is the Documentation Gap quadrant — callers annotate it above the
    rendered matrix.
    """
    matrix: dict[tuple[int, int], int] = {
        (s, d): 0 for s in range(4) for d in range(4)
    }
    audit_count = 0
    for score in scores_for_state.values():
        dims = score.get("dimensions") or {}
        s_dim = dims.get("structural_depth") or {}
        d_dim = dims.get("documentation_style_tier") or {}
        s_val = s_dim.get("value") if isinstance(s_dim, dict) else None
        d_val = d_dim.get("value") if isinstance(d_dim, dict) else None
        if (
            isinstance(s_val, (int, float))
            and isinstance(d_val, (int, float))
            and 0 <= int(s_val) <= 3
            and 0 <= int(d_val) <= 3
        ):
            matrix[(int(s_val), int(d_val))] += 1
        else:
            audit_count += 1
    return matrix, audit_count


def _heatmap_fill_color(ratio: float) -> str:
    """White → yellow → red ramp for the structural × doc matrix cells.

    ratio in ``[0, 1]``: 0 → white, 0.5 → yellow, 1.0 → red. Returned as
    an openpyxl ARGB hex string (leading ``FF`` is the alpha channel).
    """
    if ratio <= 0:
        return "FFFFFFFF"
    if ratio >= 1:
        return "FFFF6666"
    if ratio <= 0.5:
        t = ratio / 0.5
        b = int(255 - (255 - 170) * t)
        return f"FFFFFF{b:02X}"
    t = (ratio - 0.5) / 0.5
    g = int(255 - (255 - 102) * t)
    b = int(170 - (170 - 102) * t)
    return f"FFFF{g:02X}{b:02X}"


# Heatmap axis labels — verbal forms of the underlying tier integers.
# Rows map 1:1 from structural_depth tier; column labels surface the
# Documentation Style label per tier. Tier 1 covers both
# ``cross_reference`` and ``regulatory`` — the column reads
# ``Cross-ref / Reg`` so neither posture is hidden.
_HEATMAP_STRUCTURAL_DEPTH_LABELS: dict[int, str] = {
    0: "Flat",
    1: "Light",
    2: "Moderate",
    3: "Deep",
}
_HEATMAP_DOC_STYLE_LABELS: dict[int, str] = {
    0: "Silent",
    1: "Cross-ref / Reg",
    2: "Conceptual",
    3: "Prescriptive",
}


def _write_structural_doc_heatmap(
    ws: Worksheet,
    matrix: dict[tuple[int, int], int],
    audit_count: int,
    *,
    start_row: int,
) -> int:
    """Render the 4×4 Structural Depth × Documentation Style heatmap starting at ``start_row``.

    Layout::

        Structural Depth × Documentation Style heatmap (Documentation Gap quadrant: top-left 2×2)
        N = count of records per (Structural Depth, Documentation Style) bucket.
                       Silent  Cross-ref / Reg  Conceptual  Prescriptive
        Deep             N            N             N            N
        Moderate         N            N             N            N
        Light            N            N             N            N
        Flat             N            N             N            N

    Structural axis runs Deep → Flat top-down so the Documentation Gap
    quadrant (Structural Depth ≥ Moderate AND Documentation Style ≤
    Cross-reference) lands in the visually-prominent top-left 2×2.
    The four in-quadrant cells shade white → yellow → red proportional
    to each cell / quadrant max (not matrix max — out-of-quadrant
    cells like ``Deep · Conceptual`` are typically the densest, and
    the heatmap exists to point at the *gap*, not at where rows pile
    up). A medium border outlines the 2×2 so the lead area is obvious
    at a glance. When ``audit_count`` > 0, a footnote row surfaces
    the count of records whose Integration Profile dimensions were
    missing / unresolved.

    Column-axis note: tier 1 covers both ``cross_reference`` and
    ``regulatory`` documentation styles; the column reads
    ``Cross-ref / Reg`` so both postures land in the same column
    without either being hidden.

    Returns the next free row index below the rendered block.
    """
    row = start_row
    header = ws.cell(
        row=row, column=1,
        value=(
            "Implementation Shape × Documentation Style heatmap "
            "(Documentation Gap quadrant: top-left 2×2)"
        ),
    )
    header.font = Font(bold=True, size=12)
    row += 1
    ws.cell(
        row=row, column=1,
        value="N = count of records per (Implementation Shape, Documentation Style) bucket.",
    ).font = Font(italic=True)
    row += 1

    # Column header row: "" | Silent | Cross-ref / Reg | Conceptual | Prescriptive.
    ws.cell(row=row, column=1, value="")
    for d in range(4):
        c = ws.cell(row=row, column=2 + d, value=_HEATMAP_DOC_STYLE_LABELS[d])
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1

    # Color ramp is scaled to the *quadrant* max — out-of-quadrant cells
    # (e.g. the conceptual-doc column where rows naturally pile up) get a
    # neutral fill so the warm color tracks the metric the heatmap is
    # named after, not raw cell density.
    quadrant_max = max(
        matrix.get((s, d), 0) for s in (2, 3) for d in (0, 1)
    )
    thick = Side(border_style="medium", color="FF000000")
    none_side = Side(border_style=None)

    # Rows: Deep, Moderate, Light, Flat (descending so Documentation Gap quadrant is top-left).
    for s in (3, 2, 1, 0):
        label = ws.cell(row=row, column=1, value=_HEATMAP_STRUCTURAL_DEPTH_LABELS[s])
        label.font = _HEADER_FONT
        label.fill = _HEADER_FILL
        for d in range(4):
            count = matrix.get((s, d), 0)
            cell = ws.cell(row=row, column=2 + d, value=count)
            cell.alignment = Alignment(horizontal="center")
            in_quadrant = s >= 2 and d <= 1
            if in_quadrant and quadrant_max > 0:
                ratio = count / quadrant_max
                cell.fill = PatternFill(
                    start_color=_heatmap_fill_color(ratio),
                    end_color=_heatmap_fill_color(ratio),
                    fill_type="solid",
                )
            if in_quadrant:
                cell.border = Border(
                    top=thick if s == 3 else none_side,
                    bottom=thick if s == 2 else none_side,
                    left=thick if d == 0 else none_side,
                    right=thick if d == 1 else none_side,
                )
        row += 1

    if audit_count:
        row += 1
        ws.cell(
            row=row, column=1,
            value=(
                f"Audit: {audit_count} record(s) omitted (structural or doc "
                "dimension missing/unresolved)."
            ),
        ).font = Font(italic=True)
        row += 1

    return row


# --- Per-state Score Card (Option B — the analysts' three blocks) ----------
#
# Population discipline: every headline block counts DOCUMENTED rows
# scored in-scope — the same population as the sidecar-header histograms
# (`aggregate.headline_scored`), so the workbook headline matches the
# report/scoring numbers. Swagger-backfilled rows are excluded and the
# sheet says so. Pipeline diagnostics (Block D) deliberately keep the
# all-scored-rows population — they describe the pipeline, not the state.

_ADJUSTED_BINS: tuple[float, ...] = tuple(i / 2 for i in range(10))  # 0.0–4.5
_BASE_BINS: tuple[int, ...] = (0, 1, 2, 3)
_HIGH_EFFORT_THRESHOLD = 2.0


@dataclass
class _ScoreCardContext:
    """Inputs shared by the per-state Score Card blocks."""

    si: StateInputs
    scores_for_state: dict[str, dict] | None
    lens: str
    # Issue #248 Part C — resolved adjudications for this state/lens
    # ({record_key: dict} from `curation.adjudications_for`), or None.
    adjudications: dict[str, dict] | None = None

    @cached_property
    def rollup(self) -> dict | None:
        if not self.scores_for_state:
            return None
        return _compute_state_rollup(
            self.si, self.scores_for_state, self.lens, self.adjudications
        )

    @cached_property
    def headline(self) -> list[tuple[ElementRecord, dict]]:
        """Documented rows scored in-scope — the headline population."""
        if not self.scores_for_state:
            return []
        pairs: list[tuple[ElementRecord, dict]] = []
        for r in self.si.elements.elements:
            if not r.documented:
                continue
            score = self.scores_for_state.get(record_key(self.si.state, r))
            if score is None or not record_in_scope(score):
                continue
            pairs.append((r, score))
        return pairs

    @cached_property
    def adjusted_values(self) -> list[float]:
        return [
            float(s["adjusted_nachos_score"])
            for _r, s in self.headline
            if s.get("adjusted_nachos_score") is not None
        ]

    @cached_property
    def base_values(self) -> list[int]:
        out: list[int] = []
        for _r, s in self.headline:
            dim = (s.get("dimensions") or {}).get("nachos_score") or {}
            v = dim.get("value")
            if isinstance(v, int):
                out.append(v)
        return out

    @cached_property
    def effective_values(self) -> list[float]:
        """Adjudicated-else-engine over EXACTLY the ``adjusted_values``
        row set (issue #248 Part C) — the two means stay comparable."""
        out: list[float] = []
        for r, s in self.headline:
            if s.get("adjusted_nachos_score") is None:
                continue
            adj = (self.adjudications or {}).get(
                record_key(self.si.state, r)
            )
            if adj and adj.get("fresh"):
                out.append(float(adj["value"]))
            else:
                out.append(float(s["adjusted_nachos_score"]))
        return out

    @cached_property
    def adjudicated_headline_count(self) -> int:
        """Headline rows carrying a FRESH adjudication."""
        return sum(
            1
            for r, s in self.headline
            if s.get("adjusted_nachos_score") is not None
            and (
                adj := (self.adjudications or {}).get(
                    record_key(self.si.state, r)
                )
            )
            and adj.get("fresh")
        )

    @cached_property
    def stale_adjudication_count(self) -> int:
        """ALL stale adjudications for this state/lens (including
        record-gone ones that never join the headline)."""
        return sum(
            1
            for a in (self.adjudications or {}).values()
            if not a.get("fresh")
        )

    @cached_property
    def extension_split(self) -> tuple[int, int, int]:
        """(necessary, unnecessary, unresolved) among headline extensions —
        the SAME justification-label classification as the Details
        `Unnecessary Extension ?` column, so sheet and grid can't disagree."""
        nec = unnec = unres = 0
        for r, s in self.headline:
            if r.source != "extension":
                continue
            verdict = _legacy_template_fields(s, True)[0]
            if verdict == "Yes":
                unnec += 1
            elif verdict == "No":
                nec += 1
            else:
                unres += 1
        return (nec, unnec, unres)


def _bin_adjusted(value: float) -> float:
    """Snap an adjusted score onto the 0.5-step bins (defensive — the
    pipeline only produces exact halves, capped at 4.5)."""
    snapped = round(value * 2) / 2
    return min(max(snapped, _ADJUSTED_BINS[0]), _ADJUSTED_BINS[-1])


def _score_card_header_block(
    ws: Worksheet, start_row: int, ctx: _ScoreCardContext
) -> int:
    """Title, methodology subtitle, and the explicit population note."""
    from src.score.aggregate import SCORING_PLAN_VERSION

    ws["A1"] = f"NACHOS Score Card — {ctx.si.state}"
    ws["A1"].font = Font(bold=True, size=14)
    subtitle = ws.cell(
        row=2, column=1,
        value=(
            f"Methodology v{SCORING_PLAN_VERSION} — inputs, freshness "
            "stamps and version history on the Update Log sheet"
        ),
    )
    subtitle.font = Font(italic=True)
    if ctx.headline:
        note = ws.cell(
            row=3, column=1,
            value=(
                f"Population: {len(ctx.headline)} documented rows scored "
                "in-scope. Swagger-backfilled rows (AI: Documentation "
                "Source ≠ Source Doc) are excluded from these aggregates; "
                "pipeline diagnostics at the bottom cover all scored rows."
            ),
        )
        note.font = Font(italic=True)
    _set_widths(ws, {1: 38, **{c: 12 for c in range(2, 15)}})
    return 5


def _write_frequency_table(
    ws: Worksheet,
    row: int,
    title: str,
    axis_label: str,
    bins: tuple,
    counts: dict,
    total_rows: int,
    number_format: str,
    *,
    cumulative: bool = False,
) -> int:
    """One score-frequency table writer for both workbook grammars.

    ``cumulative=True`` appends the All-Combined columns (Cumulative % +
    Survival % ≥ score) — previously a near-duplicate block in
    ``_combined_frequency_block`` differing by exactly those two columns
    (issue #213 item 1).
    """
    ws.cell(row=row, column=1, value=title).font = Font(bold=True, size=12)
    row += 1
    headers: tuple[str, ...] = (axis_label, "Count", "Contribution", "% of rows")
    if cumulative:
        headers = (*headers, "Cumulative %", "Survival % (≥ score)")
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1
    total_contribution = 0.0
    running = 0
    for b in bins:
        count = counts.get(b, 0)
        contribution = float(b) * count
        total_contribution += contribution
        ws.cell(row=row, column=1, value=b).number_format = number_format
        ws.cell(row=row, column=2, value=count)
        ws.cell(row=row, column=3, value=contribution).number_format = "0.0"
        ws.cell(row=row, column=4, value=_fmt_pct(count, total_rows))
        if cumulative:
            survival = total_rows - running  # rows with score >= this bin
            running += count
            ws.cell(row=row, column=5, value=_fmt_pct(running, total_rows))
            ws.cell(row=row, column=6, value=_fmt_pct(survival, total_rows))
        row += 1
    total_label = ws.cell(row=row, column=1, value="Total Complexity Points")
    total_label.font = Font(bold=True)
    total_cell = ws.cell(row=row, column=3, value=total_contribution)
    total_cell.font = Font(bold=True)
    total_cell.number_format = "0.0"
    return row + 2


def _score_card_frequency_block(
    ws: Worksheet, start_row: int, ctx: _ScoreCardContext
) -> int:
    """Block A — score-frequency tables, headline (adjusted) axis FIRST."""
    if not ctx.headline:
        return start_row
    n = len(ctx.headline)
    adjusted_counts: dict[float, int] = {}
    for v in ctx.adjusted_values:
        b = _bin_adjusted(v)
        adjusted_counts[b] = adjusted_counts.get(b, 0) + 1
    row = _write_frequency_table(
        ws, start_row,
        "Score frequency — Adjusted NACHOS Score (headline axis)",
        "Adjusted NACHOS Score", _ADJUSTED_BINS, adjusted_counts, n, "0.0",
    )
    base_counts: dict[int, int] = {}
    for v in ctx.base_values:
        base_counts[v] = base_counts.get(v, 0) + 1
    return _write_frequency_table(
        ws, row,
        "Score frequency — Base NACHOS Score",
        "Base NACHOS Score", _BASE_BINS, base_counts, n, "0",
    )


def _score_card_metrics_block(
    ws: Worksheet, start_row: int, ctx: _ScoreCardContext
) -> int:
    """Block B — the analysts' metric list; definitions live in the labels."""
    if not ctx.headline:
        return start_row
    n = len(ctx.headline)
    adjusted = ctx.adjusted_values
    base = ctx.base_values
    total_points = sum(adjusted)
    mean_adjusted = (sum(adjusted) / len(adjusted)) if adjusted else None
    mean_base = (sum(base) / len(base)) if base else None
    high_effort = sum(1 for v in adjusted if v >= _HIGH_EFFORT_THRESHOLD)
    nec, unnec, unres = ctx.extension_split
    n_ext = nec + unnec + unres

    ws.cell(row=start_row, column=1, value="Metrics").font = Font(
        bold=True, size=12
    )
    row = start_row + 1
    entries: list[tuple[str, object]] = [
        ("Total Complexity Points (sum of adjusted scores)",
         round(total_points, 1)),
        ("Mean Adjusted NACHOS Score", _fmt_float(mean_adjusted)),
    ]
    # Issue #248 Part C — the consensus layer beside the engine mean.
    # The count renders ALWAYS (an honest "0 of N"); the effective mean
    # only when a fresh adjudication exists (at zero it is byte-
    # identical to the engine mean — pure noise).
    n_adjudicated = ctx.adjudicated_headline_count
    n_stale = ctx.stale_adjudication_count
    adjudicated_display = f"{n_adjudicated} of {n}"
    if n_stale:
        adjudicated_display += f" ({n_stale} stale — re-adjudicate)"
    entries.append(
        ("Adjudicated rows (team consensus)", adjudicated_display)
    )
    if n_adjudicated:
        entries.append((
            "Mean Effective Score (adjudicated-else-engine)",
            _fmt_float(_mean(ctx.effective_values)),
        ))
    entries.extend([
        ("% Avg Complexity Score (mean ÷ max 4.5)",
         _fmt_pct_float(mean_adjusted / 4.5 if mean_adjusted is not None else None)),
        ("Mean Base NACHOS Score", _fmt_float(mean_base)),
        (f"% High Effort Fields (adjusted ≥ {_HIGH_EFFORT_THRESHOLD})",
         _fmt_pct(high_effort, n)),
        ("# Extensions", n_ext),
        ("# Necessary Extensions", f"{nec} ({_fmt_pct(nec, n_ext)})"),
        ("# Unnecessary Extensions", f"{unnec} ({_fmt_pct(unnec, n_ext)})"),
    ])
    if unres:
        entries.append(("# Unresolved necessity (flagged for review)", unres))
    for label, value in entries:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=value)
        row += 1
    return row + 1


def _score_card_domain_matrix_block(
    ws: Worksheet, start_row: int, ctx: _ScoreCardContext
) -> int:
    """Block C — Domains × adjusted-score-bin matrix with per-domain
    totals and a Grand Total.

    Multi-domain records keep their full '; '-joined `edfi_domain` label
    as ONE row (counted once — totals reconcile to row counts); blank →
    (unassigned), sorted last.
    """
    if not ctx.headline:
        return start_row
    by_domain: dict[str, list[float]] = {}
    for r, s in ctx.headline:
        label = r.edfi_domain or "(unassigned)"
        v = s.get("adjusted_nachos_score")
        by_domain.setdefault(label, []).append(
            _bin_adjusted(float(v)) if v is not None else None
        )
    ws.cell(
        row=start_row, column=1,
        value="Domains × Adjusted NACHOS Score",
    ).font = Font(bold=True, size=12)
    row = start_row + 1
    headers = ["Ed-Fi Domain", *(f"{b:.1f}" for b in _ADJUSTED_BINS),
               "Total", "Mean adjusted"]
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1
    first_data_row = row
    grand_counts = {b: 0 for b in _ADJUSTED_BINS}
    grand_total = 0
    grand_values: list[float] = []
    for label in sorted(by_domain, key=lambda d: (d == "(unassigned)", d)):
        values = by_domain[label]
        counts = {b: 0 for b in _ADJUSTED_BINS}
        present = [v for v in values if v is not None]
        for v in present:
            counts[v] += 1
            grand_counts[v] += 1
        grand_values.extend(present)
        grand_total += len(values)
        ws.cell(row=row, column=1, value=label)
        for col_idx, b in enumerate(_ADJUSTED_BINS, start=2):
            ws.cell(row=row, column=col_idx, value=counts[b])
        ws.cell(row=row, column=len(_ADJUSTED_BINS) + 2, value=len(values))
        ws.cell(
            row=row, column=len(_ADJUSTED_BINS) + 3,
            value=_fmt_float(sum(present) / len(present)) if present else None,
        )
        row += 1
    gt = ws.cell(row=row, column=1, value="Grand Total")
    gt.font = Font(bold=True)
    for col_idx, b in enumerate(_ADJUSTED_BINS, start=2):
        c = ws.cell(row=row, column=col_idx, value=grand_counts[b])
        c.font = Font(bold=True)
    ws.cell(
        row=row, column=len(_ADJUSTED_BINS) + 2, value=grand_total
    ).font = Font(bold=True)
    ws.cell(
        row=row, column=len(_ADJUSTED_BINS) + 3,
        value=_fmt_float(sum(grand_values) / len(grand_values))
        if grand_values else None,
    ).font = Font(bold=True)
    # Light count shading over the domain rows (white → yellow).
    if row - 1 >= first_data_row:
        top_left = get_column_letter(2) + str(first_data_row)
        bottom_right = get_column_letter(len(_ADJUSTED_BINS) + 1) + str(row - 1)
        ws.conditional_formatting.add(
            f"{top_left}:{bottom_right}",
            ColorScaleRule(
                start_type="num", start_value=0, start_color="FFFFFFFF",
                end_type="max", end_color="FFFFEB84",
            ),
        )
    return row + 2


def _score_card_diagnostics_block(
    ws: Worksheet, start_row: int, ctx: _ScoreCardContext
) -> int:
    """Block D — pipeline diagnostics over ALL scored rows (labeled so)."""
    if ctx.rollup is None:
        return start_row
    next_row = _write_score_card_rollup(
        ws, start_row, ctx.rollup,
        title="Pipeline diagnostics (AI) — all scored rows",
        include_nachos=False,
    )
    return next_row + 1


# Issue #248 Part A — override clustering diagnostic. Constant strings:
# the fingerprint fixture renders the empty state, so every byte here is
# golden-pinned (deterministic by construction).
_OVERRIDE_CLUSTERS_TITLE = "Override clustering — analyst vs engine"
_OVERRIDE_CLUSTERS_SUBTITLE = (
    "Clusters analyst override disagreements by adjustment label "
    "(adjusted axis) or base rule (base axis). Δ = override − engine; "
    "dual-fire rows apply the larger adjustment, not the sum."
)
_OVERRIDE_CLUSTERS_EMPTY = (
    "No analyst override disagreements captured for this lens."
)


def _write_override_clusters_table(
    ws: Worksheet,
    start_row: int,
    clusters: list[dict],
    *,
    include_states: bool,
) -> int:
    """The override-clustering table (issue #248 Part A) — shared by the
    per-state and combined Score Cards.

    Renders a labeled empty state rather than skipping: the block's
    header lands in the fingerprint goldens, so a regression that drops
    the block fails CI, and the sheet self-documents that the loop
    exists but has no data yet (the honestly-empty-queue posture).
    """
    title = ws.cell(row=start_row, column=1, value=_OVERRIDE_CLUSTERS_TITLE)
    title.font = Font(bold=True, size=12)
    subtitle = ws.cell(
        row=start_row + 1, column=1, value=_OVERRIDE_CLUSTERS_SUBTITLE
    )
    subtitle.font = Font(italic=True)
    row = start_row + 2
    if not clusters:
        empty = ws.cell(row=row, column=1, value=_OVERRIDE_CLUSTERS_EMPTY)
        empty.font = Font(italic=True)
        return row + 2
    headers: tuple[str, ...] = (
        "Cluster", "Axis", "Direction", "Rows", "Mean Δ (override − engine)",
    )
    if include_states:
        headers = (*headers, "States")
    headers = (*headers, "Notes")
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1
    for cluster in clusters:
        ws.cell(row=row, column=1, value=cluster["cluster_label"])
        ws.cell(row=row, column=2, value=cluster["axis"])
        ws.cell(row=row, column=3, value=cluster["direction"])
        ws.cell(row=row, column=4, value=cluster["rows"])
        ws.cell(row=row, column=5, value=f"{cluster['mean_delta']:+.2f}")
        col = 6
        if include_states:
            ws.cell(
                row=row, column=col,
                value=", ".join(
                    f"{st} {n}" for st, n in cluster["state_counts"].items()
                ),
            )
            col += 1
        if cluster["dual_fire_rows"]:
            ws.cell(
                row=row, column=col,
                value=(
                    f"dual-fire ×{cluster['dual_fire_rows']} — larger "
                    "adjustment applies, not the sum"
                ),
            )
        row += 1
    return row + 1


def _score_card_override_clusters_block(
    ws: Worksheet, start_row: int, ctx: _ScoreCardContext
) -> int:
    """Block E — override clustering diagnostic (issue #248 Part A).

    Systematic disagreement with a rule routes to the methodology loop
    (a plan-version bump), not row-level adjudication — this table is
    how that pattern becomes visible. Deliberately counts ALL
    disagreements including adjudicated ones (see
    ``override_clusters`` module docstring). Invisible when the state
    has no scores sidecar at all (every sibling block's posture);
    labeled-empty when scored but disagreement-free.
    """
    if not ctx.scores_for_state:
        return start_row
    # Lazy import — curation imports workbook_spec at module level
    # (same idiom as analyst._write_review_queue_sheet).
    from src.report.curation import override_disagreements_for
    from src.report.override_clusters import cluster_override_disagreements

    state = ctx.si.state
    disagreements = override_disagreements_for(
        state, ctx.scores_for_state, ctx.lens
    )
    clusters = cluster_override_disagreements(
        {state: disagreements}, {state: ctx.scores_for_state}
    )
    return _write_override_clusters_table(
        ws, start_row, clusters, include_states=False
    )


# Issue #249 — fact-corrections diagnostic. Constant strings: the
# fingerprint fixture renders the empty state, so every byte here is
# golden-pinned (deterministic by construction).
_FACT_CORRECTIONS_TITLE = "Fact corrections — human-corrected extraction inputs"
_FACT_CORRECTIONS_SUBTITLE = (
    "Analyst corrections to LLM-extracted facts (poc3 review "
    "correct-fact), clustered by fact name. A pattern here is a PROMPT "
    "weakness — route it to the prompt-version-bump path; corrections "
    "never tune extraction."
)
_FACT_CORRECTIONS_EMPTY = (
    "No fact corrections recorded for this lens — every extracted "
    "fact stands as extracted."
)


def _write_fact_corrections_table(
    ws: Worksheet,
    start_row: int,
    clusters: list[dict],
    *,
    include_states: bool,
) -> int:
    """The fact-corrections table (issue #249) — shared by the
    per-state and combined Score Cards. Same labeled-empty posture as
    the override-clustering block: the header lands in the fingerprint
    goldens, so a regression that drops the block fails CI."""
    title = ws.cell(row=start_row, column=1, value=_FACT_CORRECTIONS_TITLE)
    title.font = Font(bold=True, size=12)
    subtitle = ws.cell(
        row=start_row + 1, column=1, value=_FACT_CORRECTIONS_SUBTITLE
    )
    subtitle.font = Font(italic=True)
    row = start_row + 2
    if not clusters:
        empty = ws.cell(row=row, column=1, value=_FACT_CORRECTIONS_EMPTY)
        empty.font = Font(italic=True)
        return row + 2
    headers: tuple[str, ...] = ("Fact", "Correction", "Rows")
    if include_states:
        headers = (*headers, "States")
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1
    for cluster in clusters:
        ws.cell(row=row, column=1, value=cluster["fact"])
        ws.cell(row=row, column=2, value=cluster["flip"])
        ws.cell(row=row, column=3, value=cluster["rows"])
        if include_states:
            ws.cell(
                row=row, column=4,
                value=", ".join(
                    f"{st} {n}" for st, n in cluster["state_counts"].items()
                ),
            )
        row += 1
    return row + 1


def _score_card_fact_corrections_block(
    ws: Worksheet, start_row: int, ctx: _ScoreCardContext
) -> int:
    """Fact-corrections diagnostic (issue #249) — corrections clustered
    by fact name, beside the override-clustering block it completes:
    wrong fact → correct-fact (this table routes the systematic case to
    the prompt loop); wrong rule → version bump (Block E); genuinely
    idiosyncratic row → adjudication. Invisible when the state has no
    scores sidecar at all (every sibling block's posture);
    labeled-empty when scored but correction-free.
    """
    if not ctx.scores_for_state:
        return start_row
    from src.report.curation import fact_corrections_for
    from src.report.fact_corrections import cluster_corrections_by_fact

    state = ctx.si.state
    corrections = fact_corrections_for(state, ctx.lens)
    clusters = cluster_corrections_by_fact({state: corrections})
    return _write_fact_corrections_table(
        ws, start_row, clusters, include_states=False
    )


def _score_card_heatmap_block(
    ws: Worksheet, start_row: int, ctx: _ScoreCardContext
) -> int:
    """Implementation Shape × Documentation Style heatmap — API-model-lens
    workbooks only (the doc-gap surface lives on the retiring API-model
    deliverable until Option D's Documentation Gaps sheet exists)."""
    if ctx.lens != "spine" or ctx.rollup is None:
        return start_row
    matrix, audit_count = _compute_structural_doc_matrix(ctx.scores_for_state)
    if any(matrix.values()) or audit_count:
        # Return the heatmap's next free row (issue #213 item 1: the
        # return used to be discarded — safe only while this block is
        # last in the stack; a block appended after it would overwrite).
        return _write_structural_doc_heatmap(
            ws, matrix, audit_count, start_row=start_row
        )
    return start_row


# Ordered per-state Score Card blocks — each writes at the row it is
# given and returns the next block's start row.
_SCORE_CARD_BLOCKS_PER_STATE: tuple = (
    _score_card_header_block,
    _score_card_frequency_block,
    _score_card_metrics_block,
    _score_card_domain_matrix_block,
    _score_card_diagnostics_block,
    _score_card_override_clusters_block,
    _score_card_fact_corrections_block,
    _score_card_heatmap_block,
)


def _write_score_card(
    ws: Worksheet,
    si: StateInputs,
    scores_for_state: dict[str, dict] | None = None,
    lens: str = "source",
    adjudications: dict[str, dict] | None = None,
) -> None:
    """Per-state Score Card — a fold over `_SCORE_CARD_BLOCKS_PER_STATE`."""
    ws.title = "Score Card"
    ctx = _ScoreCardContext(
        si=si, scores_for_state=scores_for_state, lens=lens,
        adjudications=adjudications,
    )
    row = 1
    for block in _SCORE_CARD_BLOCKS_PER_STATE:
        row = block(ws, row, ctx)



# --- Combined (cross-state) Score Card --------------------------------------

def _aggregate_nachos_cross_state(per_state_rollups: list[dict]) -> dict:
    """Cross-state NACHOS rollup (Phase F).

    Sums in-scope counts, means weighted by in-scope count, sums per-tier
    histograms. Isolated from the quality-axis rollup so the NACHOS
    methodology signal stays visually distinct in the Score Card.
    """
    in_scope_total = sum(r.get("in_scope_count", 0) for r in per_state_rollups)
    out_of_scope_total = sum(r.get("out_of_scope_count", 0) for r in per_state_rollups)

    def _weighted(field: str) -> float | None:
        return weighted_mean([
            (float(v), r.get("in_scope_count", 0))
            for r in per_state_rollups
            if isinstance(v := r.get(field), (int, float))
        ])

    tier_hist = {i: 0 for i in (0, 1, 2, 3)}
    for r in per_state_rollups:
        h = r.get("nachos_tier_histogram") or {}
        for tier, count in h.items():
            tier_hist[int(tier)] = tier_hist.get(int(tier), 0) + count

    return {
        "in_scope_count": in_scope_total,
        "out_of_scope_count": out_of_scope_total,
        "mean_nachos_score": _weighted("mean_nachos_score"),
        "mean_adjusted_nachos_score": _weighted("mean_adjusted_nachos_score"),
        "nachos_tier_histogram": tier_hist,
        # Issue #248 Part C — counts sum; the effective mean carries the
        # same in-scope weighting as the engine mean beside it.
        "adjudicated_count": sum(
            r.get("adjudicated_count", 0) for r in per_state_rollups
        ),
        "adjudicated_stale_count": sum(
            r.get("adjudicated_stale_count", 0) for r in per_state_rollups
        ),
        "mean_effective_adjusted_score": _weighted(
            "mean_effective_adjusted_score"
        ),
    }


def _aggregate_cross_state_rollup(
    per_state_rollups: list[dict],
    lens: str,
) -> dict:
    """Record-count-weighted aggregate across scored states.

    Dimension means are weighted by each state's ``dimension_counts[d]``
    (not ``scored_count``) because ``extension_justification`` excludes
    core rows and ``semantic_fidelity`` excludes ``not_applicable`` rows
    — the per-dimension denominator varies.
    """
    scored_total = sum(r["scored_count"] for r in per_state_rollups)
    record_total = sum(r["record_count"] for r in per_state_rollups)
    core_total = sum(r["core_count"] for r in per_state_rollups)
    ext_total = sum(r["extension_count"] for r in per_state_rollups)
    unknown_total = sum(r["unknown_count"] for r in per_state_rollups)
    needs_review_total = sum(r["needs_review_count"] for r in per_state_rollups)
    route_totals = {rt: 0 for rt in _REVIEW_ROUTES}
    for r in per_state_rollups:
        for rt, c in r["route_counts"].items():
            route_totals[rt] += c

    def _weighted(field: str, weight_field: str) -> float | None:
        return weighted_mean([
            (float(v), w)
            for r in per_state_rollups
            if isinstance(v := r.get(field), (int, float))
            and isinstance(w := r.get(weight_field), int)
        ])

    dim_names = _dimension_names(lens)
    dim_means = {
        d: weighted_mean([
            (float(m), r["dimension_counts"].get(d, 0))
            for r in per_state_rollups
            if isinstance(m := r["dimension_means"].get(d), (int, float))
        ])
        for d in dim_names
    }
    return {
        "state": "Cross",
        "lens": lens,
        "scored_count": scored_total,
        "record_count": record_total,
        "core_count": core_total,
        "extension_count": ext_total,
        "unknown_count": unknown_total,
        "needs_review_count": needs_review_total,
        "route_counts": route_totals,
        "dimension_means": dim_means,
        "dimension_counts": {
            d: sum(r["dimension_counts"].get(d, 0) for r in per_state_rollups)
            for d in dim_names
        },
        "mean_complexity_score": _weighted("mean_complexity_score", "scored_count"),
    }


def _write_combined_score_card_rollup(
    ws: Worksheet,
    start_row: int,
    per_state_rollups: list[dict],
    cross: dict,
    lens: str,
) -> int:
    """Cross-state rollup matrix — metric rows × (AZ, WI, MN, TX, Cross) cols.

    Stacks below the existing per-state summary. Bold header row; numeric
    cells rendered with 2-decimal floats and integer counts inline. Same
    metrics as the per-state block.
    """
    state_codes = [r["state"] for r in per_state_rollups]
    row = start_row
    ws.cell(
        row=row, column=1,
        value=f"Scoring rollup — cross-state (lens: {lens})",
    ).font = Font(bold=True, size=12)
    row += 1

    headers = ["metric", *state_codes, "Cross"]
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1

    def _scored_denom(rollup: dict) -> int:
        return rollup["scored_count"]

    def _pct_str(rollup: dict) -> str:
        denom = _scored_denom(rollup)
        return f"{rollup['needs_review_count']} ({_fmt_pct(rollup['needs_review_count'], denom)})"

    dim_names = _dimension_names(lens)

    metrics: list[tuple] = [
        ("records_scored", lambda r: r["scored_count"]),
        ("records_total", lambda r: r["record_count"]),
    ]
    # v23 — mean_complexity_score on both lenses (issue #106 / Q4).
    metrics.append(
        ("mean_complexity_score", lambda r: _fmt_float(r["mean_complexity_score"]))
    )
    metrics.extend([
        ("needs_review_count", _pct_str),
        ("route_POLICY", lambda r: r["route_counts"]["POLICY"]),
        ("route_DATA_MODEL", lambda r: r["route_counts"]["DATA_MODEL"]),
        ("route_SCORING", lambda r: r["route_counts"]["SCORING"]),
        ("route_ANALYST", lambda r: r["route_counts"]["ANALYST"]),
        ("core_count", lambda r: r["core_count"]),
        ("extension_count", lambda r: r["extension_count"]),
    ])
    if any(r["unknown_count"] for r in per_state_rollups) or cross["unknown_count"]:
        metrics.append(("unknown_count", lambda r: r["unknown_count"]))
    for d in dim_names:
        metrics.append((f"mean_{d}", lambda r, _d=d: _fmt_float(r["dimension_means"].get(_d))))

    for label, fn in metrics:
        ws.cell(row=row, column=1, value=label)
        for col_offset, r in enumerate(per_state_rollups, start=2):
            ws.cell(row=row, column=col_offset, value=fn(r))
        ws.cell(row=row, column=2 + len(per_state_rollups), value=fn(cross))
        row += 1

    # Phase F — NACHOS methodology block. Cross-state weighted by
    # in-scope count, per-state reads straight from the rollup.
    row += 1
    ws.cell(
        row=row, column=1,
        value="NACHOS methodology",
    ).font = Font(bold=True, size=12)
    row += 1
    nachos_cross = _aggregate_nachos_cross_state(per_state_rollups)

    def _tier_count(r: dict, tier: int) -> int:
        return (r.get("nachos_tier_histogram") or {}).get(tier, 0)

    def _adjudicated_cell(r: dict) -> str:
        display = f"{r.get('adjudicated_count', 0)} of {r.get('in_scope_count', 0)}"
        stale = r.get("adjudicated_stale_count", 0)
        if stale:
            display += f" ({stale} stale — re-adjudicate)"
        return display

    nachos_metrics: list[tuple] = [
        ("in_scope_count", lambda r: r.get("in_scope_count", 0)),
        ("out_of_scope_count", lambda r: r.get("out_of_scope_count", 0)),
        ("mean_nachos_score", lambda r: _fmt_float(r.get("mean_nachos_score"))),
        (
            "mean_adjusted_nachos_score",
            lambda r: _fmt_float(r.get("mean_adjusted_nachos_score")),
        ),
        # Issue #248 Part C — fixed-shape matrix rows render always
        # (zeros are honest here); the effective mean stays blank via
        # _fmt_float(None) when no state has an adjudication.
        ("adjudicated_rows", _adjudicated_cell),
        (
            "mean_effective_adjusted_score",
            lambda r: _fmt_float(r.get("mean_effective_adjusted_score"))
            if r.get("adjudicated_count", 0)
            else "",
        ),
    ]
    for tier in (0, 1, 2, 3):
        nachos_metrics.append(
            (f"nachos_tier_{tier}_count", lambda r, _t=tier: _tier_count(r, _t))
        )
    for label, fn in nachos_metrics:
        ws.cell(row=row, column=1, value=label)
        for col_offset, r in enumerate(per_state_rollups, start=2):
            ws.cell(row=row, column=col_offset, value=fn(r))
        ws.cell(row=row, column=2 + len(per_state_rollups), value=fn(nachos_cross))
        row += 1
    return row


@dataclass
class _CombinedScoreCardContext:
    """Inputs shared by the combined (cross-state) Score Card blocks."""

    state_inputs: list[StateInputs]
    scores_by_state: dict[str, dict[str, dict]] | None
    lens: str
    # Issue #248 Part C — {state: {record_key: resolved adjudication}}.
    adjudications_by_state: dict[str, dict[str, dict]] | None = None


def _combined_state_table_block(
    ws: Worksheet, start_row: int, ctx: _CombinedScoreCardContext
) -> int:
    """Title + one-row-per-state coverage table."""
    ws["A1"] = "NACHOS POC-3 Scorecard — All States Combined"
    ws["A1"].font = Font(bold=True, size=14)
    headers = (
        "State",
        # Honest relabel — swagger `info.version`, not the Data Standard
        # number (see `_score_card_metadata_block`).
        "Ed-Fi API version (swagger)",
        "Spine entities",
        "Spine extensions",
        "Element count",
        "Source-document coverage",
        "Spine coverage (of full Ed-Fi UDM)",
        "Source scope",
    )
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=start_row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    for idx, si in enumerate(ctx.state_inputs, start=start_row + 1):
        ws.cell(row=idx, column=1, value=si.state)
        ws.cell(row=idx, column=2, value=si.spine.edfi_version)
        ws.cell(row=idx, column=3, value=si.spine.entity_count)
        ws.cell(row=idx, column=4, value=si.spine.extension_count)
        ws.cell(row=idx, column=5, value=si.elements.element_count)
        ws.cell(row=idx, column=6, value=format_source_coverage(si))
        ws.cell(row=idx, column=7, value=format_spine_coverage(si))
        scope_cell = ws.cell(
            row=idx, column=8, value=SOURCE_SCOPE_BY_STATE.get(si.state, "")
        )
        scope_cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[idx].height = 75
    _set_widths(ws, {1: 12, 2: 18, 3: 18, 4: 20, 5: 18, 6: 26, 7: 32, 8: 90})
    # Two blank rows below the last state row give a visual break before
    # the semantics note.
    return start_row + 1 + len(ctx.state_inputs) + 2


def _combined_semantics_stamps_block(
    ws: Worksheet, start_row: int, ctx: _CombinedScoreCardContext
) -> int:
    """Coverage-semantics note + freshness stamps (mirrors per-state)."""
    label_cell = ws.cell(row=start_row, column=1, value="Coverage semantics")
    label_cell.font = Font(bold=True)
    note_cell = ws.cell(row=start_row, column=2, value=COVERAGE_SEMANTICS_NOTE)
    note_cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[start_row].height = 75

    # Sequence-1 hygiene: freshness stamps. Lazy import — see
    # `_score_card_metadata_block`.
    from src.score.aggregate import SCORING_PLAN_VERSION

    for offset, (label, value) in enumerate(
        (
            ("Scoring plan version", SCORING_PLAN_VERSION),
            (
                "Workbook generated",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        ),
        start=1,
    ):
        ws.cell(row=start_row + offset, column=1, value=label).font = Font(bold=True)
        ws.cell(row=start_row + offset, column=2, value=value)
    # +2 stamp rows, then two blank rows before the rollup.
    return start_row + 5


def _combined_rollup_block(
    ws: Worksheet, start_row: int, ctx: _CombinedScoreCardContext
) -> int:
    if not ctx.scores_by_state:
        return start_row
    per_state_rollups: list[dict] = []
    for si in ctx.state_inputs:
        state_scores = ctx.scores_by_state.get(si.state) or {}
        r = _compute_state_rollup(
            si, state_scores, ctx.lens,
            (ctx.adjudications_by_state or {}).get(si.state),
        )
        if r is None:
            # Placeholder so column layout stays aligned with state_inputs.
            r = {
                "state": si.state, "lens": ctx.lens, "scored_count": 0,
                "record_count": si.elements.element_count,
                "core_count": 0, "extension_count": 0, "unknown_count": 0,
                "needs_review_count": 0,
                "route_counts": {rt: 0 for rt in _REVIEW_ROUTES},
                "dimension_means": {d: None for d in _dimension_names(ctx.lens)},
                "dimension_counts": {d: 0 for d in _dimension_names(ctx.lens)},
                "mean_complexity_score": None,
            }
        per_state_rollups.append(r)
    cross = _aggregate_cross_state_rollup(per_state_rollups, ctx.lens)
    return _write_combined_score_card_rollup(
        ws, start_row, per_state_rollups, cross, ctx.lens,
    ) or start_row


def _combined_frequency_block(
    ws: Worksheet, start_row: int, ctx: _CombinedScoreCardContext
) -> int:
    """Cross-state adjusted-score frequency with Cumulative % and
    Survival % (≥ score) columns — the All-Combined workbook grammar.
    Population: documented rows scored in-scope, all states."""
    if not ctx.scores_by_state:
        return start_row
    values: list[float] = []
    for si in ctx.state_inputs:
        sub = _ScoreCardContext(
            si=si,
            scores_for_state=ctx.scores_by_state.get(si.state) or {},
            lens=ctx.lens,
        )
        values.extend(sub.adjusted_values)
    if not values:
        return start_row
    counts: dict[float, int] = {}
    for v in values:
        b = _bin_adjusted(v)
        counts[b] = counts.get(b, 0) + 1
    return _write_frequency_table(
        ws, start_row,
        "Score frequency — Adjusted NACHOS Score, all states "
        "(documented rows scored in-scope)",
        "Adjusted NACHOS Score", _ADJUSTED_BINS, counts, len(values), "0.0",
        cumulative=True,
    )


def _combined_per_state_stack_block(
    ws: Worksheet, start_row: int, ctx: _CombinedScoreCardContext
) -> int:
    """Per-state Score Card block stack (Option D — the All-Combined
    grammar: each state's adjusted-first frequency table + metric list
    under a bold state header, reusing the per-state block functions)."""
    row = start_row
    for si in ctx.state_inputs:
        # Through the `_set_cell` formula guard — the leading `=` would
        # otherwise be written as an (invalid) formula and Excel flags
        # the workbook as corrupt ("Removed Records: Formula").
        header = _set_cell(ws, row, 1, f"== {si.state} ==")
        header.font = Font(bold=True, size=13)
        row += 2
        state_ctx = _ScoreCardContext(
            si=si,
            scores_for_state=(ctx.scores_by_state or {}).get(si.state),
            lens=ctx.lens,
            adjudications=(ctx.adjudications_by_state or {}).get(si.state),
        )
        row = _score_card_frequency_block(ws, row, state_ctx)
        row = _score_card_metrics_block(ws, row, state_ctx)
        row += 1
    return row


def _combined_override_clusters_block(
    ws: Worksheet, start_row: int, ctx: _CombinedScoreCardContext
) -> int:
    """Cross-state override clustering (issue #248 Part A) — the same
    table as the per-state Block E with a States column, so a pattern
    spread across states reads as one cluster."""
    if not ctx.scores_by_state:
        return start_row
    from src.report.curation import override_disagreements_for
    from src.report.override_clusters import cluster_override_disagreements

    by_state = {
        si.state: override_disagreements_for(
            si.state, ctx.scores_by_state.get(si.state) or {}, ctx.lens
        )
        for si in ctx.state_inputs
    }
    clusters = cluster_override_disagreements(by_state, ctx.scores_by_state)
    return _write_override_clusters_table(
        ws, start_row, clusters, include_states=True
    )


def _combined_fact_corrections_block(
    ws: Worksheet, start_row: int, ctx: _CombinedScoreCardContext
) -> int:
    """Cross-state fact-corrections diagnostic (issue #249) — the same
    table as the per-state block with a States column, so a prompt
    weakness spread across states reads as one cluster."""
    if not ctx.scores_by_state:
        return start_row
    from src.report.curation import fact_corrections_for
    from src.report.fact_corrections import cluster_corrections_by_fact

    by_state = {
        si.state: fact_corrections_for(si.state, ctx.lens)
        for si in ctx.state_inputs
    }
    clusters = cluster_corrections_by_fact(by_state)
    return _write_fact_corrections_table(
        ws, start_row, clusters, include_states=True
    )


# Ordered combined Score Card blocks — same fold shape as per-state.
_SCORE_CARD_BLOCKS_COMBINED: tuple = (
    _combined_state_table_block,
    _combined_semantics_stamps_block,
    _combined_frequency_block,
    _combined_per_state_stack_block,
    _combined_rollup_block,
    _combined_override_clusters_block,
    _combined_fact_corrections_block,
)


def _build_combined_score_card(
    ws: Worksheet,
    state_inputs: list[StateInputs],
    scores_by_state: dict[str, dict[str, dict]] | None = None,
    lens: str = "source",
    adjudications_by_state: dict[str, dict[str, dict]] | None = None,
) -> None:
    """Combined Score Card — a fold over `_SCORE_CARD_BLOCKS_COMBINED`."""
    ws.title = "Score Card"
    ctx = _CombinedScoreCardContext(
        state_inputs=state_inputs, scores_by_state=scores_by_state, lens=lens,
        adjudications_by_state=adjudications_by_state,
    )
    row = 3
    for block in _SCORE_CARD_BLOCKS_COMBINED:
        row = block(ws, row, ctx)

