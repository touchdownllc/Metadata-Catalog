"""Declarative workbook spec — R2 of the 2026-07 consumability review (#186).

One data structure per workbook surface: column name → source field →
lens → width → format → legend hook. ``report/analyst.py`` renders these
specs generically (``report/workbook_render.py``); the column-contract
tests assert the spec instead of positional pins; the Legend sheet and
the CLAUDE.md column contract point here.

Design rules:

- **No openpyxl import.** This module is pure data + extractor
  callables, so CLI help, docs generation, and prose surfaces can import
  labels without dragging in the spreadsheet stack.
- **One spec, per-column lens membership.** The source/spine Reviewer
  View shapes differ by pure insertion (``Ed-Fi Standard Definition``,
  ``Documented`` are spine-only), so a single ordered tuple filtered by
  ``lens`` reproduces both header tuples exactly. Sheets whose per-lens
  orders genuinely diverge (Audit Trail fact blocks) use a
  ``columns_factory`` instead.
- **``Row #`` is NOT a ColumnSpec.** It stays a renderer-injected
  leading column (``SheetSpec.row_number_column``) so
  ``human_score_backfill._POC3_HEADERS`` — derived from the source-lens
  header tuple — can never grow an ``ai-Row #``.
- Value-group helpers (``_score_fields`` etc.) moved here verbatim from
  ``analyst.py`` (which re-exports them); their behavior is pinned by
  ``TestLegacyTemplateFields`` and the R2 cell-diff harness
  (``scripts/diff_workbooks.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Callable, Literal, NamedTuple

from src.models.element import ElementRecord

CellValue = str | int | float | None

# Lens membership sets. BOTH is the default; a column tagged SOURCE or
# SPINE appears only in that lens's rendering of the sheet.
SOURCE: frozenset[str] = frozenset({"source"})
SPINE: frozenset[str] = frozenset({"spine"})
BOTH: frozenset[str] = frozenset({"source", "spine"})

# Coarse column role. Inert in R2; Option B uses it to band the Details
# sheet and to filter the `ai-` projection in human_score_backfill
# (everything except `analyst_input` — plus the injected Row #, which is
# never a ColumnSpec in the first place). `cmp` marks the render-time
# human-comparison columns (`--with-human`) — they never enter
# DETAILS_COLUMNS, so no machine projection can grow them.
Role = Literal["identity", "data", "score", "ai", "analyst_input", "cmp"]


# --- Value-group helpers (moved verbatim from analyst.py) -----------------

_MATCH_STATUS_BY_SOURCE: dict[str, str] = {
    "core": "Matched (core)",
    "extension": "Matched (extension)",
    "unknown": "Unresolved",
    "filtered": "Filtered (SIS never populates)",
}


def _is_extension_record(record: ElementRecord) -> bool:
    """True iff THIS specific element was contributed by a state extension.

    Reads the per-record `source` field set by ingest adapters (AZ uses the
    XLSX `edfi.*` / `az.*` namespace; WI/MN derive via spine lookup in
    `attribute_record_source`). Replaces the old entity-level heuristic
    that flagged every element on a Calendar as `Yes` whenever AZ had a
    `CalendarExtension`, regardless of whether the element itself was core.
    """
    return record.source == "extension"


def _match_status(record: ElementRecord) -> str:
    """Human-readable match-confidence label derived from `record.source`.

    Analysts flagged that `source="unknown"` rows rendered identically to
    confirmed core/extension matches in the workbook (only the boolean
    `Is an extension` column was surfaced). This label makes confidence
    explicit so unresolved rows don't look validated.
    """
    return _MATCH_STATUS_BY_SOURCE.get(record.source, "Unresolved")


def _references_for_record(record: ElementRecord) -> str | None:
    """Flatten source doc pointers + regulatory citations into one cell."""
    bits: list[str] = []
    doc = record.source_document
    loc = record.source_page_or_section
    if doc and loc:
        bits.append(f"{doc} / {loc}")
    elif doc:
        bits.append(doc)
    elif loc:
        bits.append(loc)
    if record.regulatory_citations:
        bits.append("; ".join(record.regulatory_citations))
    return " | ".join(bits) if bits else None


def _score_fields(score: dict | None) -> tuple:
    """Return ``(complex_bl, nachos, adjusted_nachos, justification)``.

    Fills the four stakeholder-visible scoring cells from the per-record
    sidecar. v10 methodology scope rectification (2026-04-26) — every
    row that the rule cascade evaluated renders its 0-3 tier; tier 0 is
    valid output ("Send granular element / descriptor") not a missing
    value. Prior versions blanked tier-0 rows on a too-narrow in_scope
    gate that mis-implemented `Logic_Dec2025` rows 33-46.

    ``complex_bl`` surfaces the spine-lens ``business_logic_complexity``
    integer (0..3, higher = costlier). Source-lens records have no
    complexity dimension → None.

    ``nachos`` is the methodology tier (0..3), distinct from
    ``complexity_score`` (a cost axis): plan §2 decision 1 — the two
    signals coexist.
    """
    if not score:
        return (None, None, None, None)
    complex_bl = score.get("complexity_score")
    dims = score.get("dimensions") or {}
    nachos_dim = dims.get("nachos_score") or {}
    nachos_tier = nachos_dim.get("value") if isinstance(nachos_dim, dict) else None
    adjusted = score.get("adjusted_nachos_score")
    justification = score.get("nachos_justification")
    return (complex_bl, nachos_tier, adjusted, justification)


_STRUCTURAL_DEPTH_LABELS: dict[int, str] = {
    0: "Flat",
    1: "Light",
    2: "Moderate",
    3: "Deep",
}

_DOC_STYLE_DISPLAY: dict[str, str] = {
    "prescriptive": "Prescriptive",
    "conceptual": "Conceptual",
    "cross_reference": "Cross-reference",
    "regulatory": "Regulatory",
    # Display-layer rename of the ``unspecified`` classifier label.
    # Enum value remains ``unspecified`` in the sidecar / artifacts
    # until the Tier 3 prompt-rename ships; the workbook reads it
    # as "Silent" today.
    "unspecified": "Silent",
}


def _integration_profile_fields(score: dict | None) -> tuple:
    """Return ``(structural_depth, doc_style, doc_gap, doc_gap_reason)``.

    Integration Profile workbook columns. Reads the ``structural_depth``,
    ``documentation_style_tier``, and ``documentation_gap`` dimensions
    from the per-record sidecar.

    - ``structural_depth`` — verbal label (Flat / Light / Moderate /
      Deep) computed from the underlying integer tier. The integer
      stays in the JSON sidecar for sort/filter/heatmap rendering.
    - ``doc_style`` — display label (Prescriptive / Conceptual /
      Cross-reference / Regulatory / Silent) surfaced from the raw
      ``documentation_style`` classifier enum. ``Silent`` is the
      display rename of the ``unspecified`` enum value; the prompt-
      level enum rename is deferred to a future Tier 3 PR.
    - ``doc_gap`` — ``"Yes"`` when the gap signal fires, ``"No"`` when
      it evaluated but didn't fire, ``None`` when the rule could not
      evaluate.
    - ``doc_gap_reason`` — composes from the two upstream display
      labels (e.g. ``"Deep · Silent"``) when ``doc_gap == "Yes"``;
      empty otherwise. Self-explaining the quadrant on the row.
    """
    if not score:
        return (None, None, None, None)
    dims = score.get("dimensions") or {}
    struct_dim = dims.get("structural_depth") or {}
    doc_dim = dims.get("documentation_style_tier") or {}
    gap_dim = dims.get("documentation_gap") or {}
    struct_tier = struct_dim.get("value") if isinstance(struct_dim, dict) else None
    structural_depth_label = (
        _STRUCTURAL_DEPTH_LABELS.get(struct_tier)
        if isinstance(struct_tier, int)
        else None
    )
    doc_inputs = doc_dim.get("inputs_used") if isinstance(doc_dim, dict) else None
    raw_style = (
        doc_inputs.get("documentation_style") if isinstance(doc_inputs, dict) else None
    )
    doc_style_label = (
        _DOC_STYLE_DISPLAY.get(raw_style) if isinstance(raw_style, str) else None
    )
    gap_value = gap_dim.get("value") if isinstance(gap_dim, dict) else None
    if gap_value is None:
        doc_gap = None
        doc_gap_reason = None
    elif gap_value == 1:
        doc_gap = "Yes"
        if structural_depth_label and doc_style_label:
            doc_gap_reason = f"{structural_depth_label} · {doc_style_label}"
        else:
            doc_gap_reason = None
    else:
        doc_gap = "No"
        doc_gap_reason = None
    return (structural_depth_label, doc_style_label, doc_gap, doc_gap_reason)


_DOC_SOURCE_DISPLAY: dict[str, str] = {
    "source_doc": "Source Doc",
    "swagger": "Swagger",
    "swagger_leaf": "Swagger (leaf)",
}


def _doc_source_label(r: ElementRecord) -> str:
    """Return the workbook-facing label for ``documentation_source`` (issue #70)."""
    return _DOC_SOURCE_DISPLAY.get(
        getattr(r, "documentation_source", "source_doc"), "Source Doc"
    )


def _review_cells(score: dict | None) -> tuple[str | None, str | None]:
    """Return ``(needs_review, review_route)`` for the trailing Reviewer View cols (issue #113).

    - Unscored row (``score is None``) → ``(None, None)``. The row was
      never put through the rule cascade, so the flag has no defined
      value; blank reads honestly.
    - ``review.needs_review == True`` → ``("Yes", route)``. Route is the
      sidecar-stored ``review.route`` (populated by
      ``aggregate._review_block`` via ``route_review``); on the
      defensive path where the sidecar predates Phase D's route field
      we re-derive via ``route_review`` from ``review.reasons``.
    - ``review.needs_review == False`` → ``("No", None)``. Route is
      blank when the row was not flagged.
    """
    if score is None:
        return (None, None)
    review = score.get("review") or {}
    if not review.get("needs_review"):
        return ("No", None)
    route = review.get("route")
    if not route:
        # Local import avoids workbook_spec → review_queue import-time
        # cycle (review_queue → utils.paths only).
        from src.report.review_queue import route_review

        route = route_review(list(review.get("reasons", []) or []))
    return ("Yes", route or "(unrouted)")


def _legacy_template_fields(
    score: dict | None,
    is_extension: bool,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return ``(unnecessary_ext, cross_entity, complexity_signals, multi_entity)``.

    Issue #102 wired four pre-blanked legacy template columns to the
    per-record sidecar; issue #107 corrected two source-of-truth
    mismatches surfaced by analyst review.

    - ``Unnecessary Extension ?`` — reads the ``+1 unnecessary_ext`` /
      ``+0.5 necessary_ext`` adjustment label that ``_compute_nachos_-
      adjustments`` writes into ``nachos_justification``. This is the
      same source-of-truth Phase F arithmetic uses (gate:
      ``extension_is_necessary == False``). The prior implementation
      read ``extension_justification.rule_matched``, whose
      ``tier_0_unnecessary_mirror`` requires the additional
      ``extension_mirrors_core_pattern == True`` — a stricter gate
      that under-reported "Yes" by 518 rows across all four states.
      Reading the justification string also covers spine-lens
      extension rows whose ``extension_is_necessary`` arrives via the
      cross-lens borrow rather than direct extraction. Unresolved
      rows (``extension_is_necessary == None``, conservative +0.5
      fallback + review flag) render as ``None`` so the Needs Review
      flag carries the signal without misclassifying as either side.
    - ``Cross Entity Calculation ?`` — ``"Yes"`` / ``"No"`` from the
      ``has_cross_entity_logic__reconciled`` input the nachos_score
      rule actually consumed (post bare-FK suppression).
    - ``Complexity Signals`` (column header renamed from ``Reason for
      Complexity`` in #107) — structured label joining True flags
      from ``nachos_score.inputs_used``. Renders as
      ``"aggregation; conditional logic"`` etc. Bug 3 gate: blanks
      the cell entirely when ``nachos_score.value == 0`` so reviewers
      don't see contradictory signals next to a zero score (e.g. the
      natural-key-concatenation rule path zero-rates the concat flag).
    - ``Multiple Entities Involved`` — ``"Yes"`` / ``"No"`` flag at the
      ``cross_entity_targets >= 2`` threshold that drives the Phase F
      multi-entity +0.5 adjustment. NOTE: the helper currently reads
      the raw pre-suppression ``cross_entity_targets`` from
      ``inputs_used``; for bare-FK rows the rule layer suppresses
      multi-entity in the score but ``inputs_used`` retains the raw
      count, so this column can over-report "Yes" by ~1 row across
      the corpus. Tracked in #107 Bug 2 (separate fix scope).
    """
    if not score:
        return (None, None, None, None)
    dims = score.get("dimensions") or {}

    if not is_extension:
        unnecessary = "N/A"
    else:
        just = score.get("nachos_justification") or ""
        review = score.get("review") or {}
        review_reasons = review.get("reasons") or []
        unresolved = "extension_necessity_unresolved" in review_reasons
        if "+1 unnecessary_ext" in just:
            unnecessary = "Yes"
        elif unresolved:
            # Conservative +0.5 path with `extension_necessity_unresolved`
            # review flag — the LLM verdict is None, neither Yes nor No.
            # Blank cell + Needs Review flag is the honest rendering.
            unnecessary = None
        elif "+0.5 necessary_ext" in just:
            unnecessary = "No"
        else:
            # Extension row but no adjustment label found in justification.
            # Shouldn't happen given current `_compute_nachos_adjustments`
            # (every extension row gets one of the two labels), but render
            # defensively as blank rather than misclassify.
            unnecessary = None

    nachos_dim = dims.get("nachos_score") or {}
    nachos_value = (
        nachos_dim.get("value") if isinstance(nachos_dim, dict) else None
    )
    inputs = nachos_dim.get("inputs_used") if isinstance(nachos_dim, dict) else None
    inputs = inputs or {}

    cross_entity_raw = inputs.get("has_cross_entity_logic__reconciled")
    if cross_entity_raw is True:
        cross_entity = "Yes"
    elif cross_entity_raw is False:
        cross_entity = "No"
    else:
        cross_entity = None

    targets = inputs.get("cross_entity_targets")
    if isinstance(targets, int):
        multi_entity = "Yes" if targets >= 2 else "No"
    else:
        multi_entity = None

    if nachos_value == 0:
        # Bug 3 fix — empty Complexity Signals when the score's
        # complexity tier is 0. Without this gate the natural-key
        # concatenation rule path (`tier_0_natural_key_format`) renders
        # "concatenation" alongside a NACHOS score of 0, contradicting
        # the score from a reviewer's perspective.
        reason = None
    else:
        conditional = inputs.get(
            "has_conditional_logic__reconciled", inputs.get("has_conditional_logic")
        )
        parts: list[str] = []
        if inputs.get("has_aggregation"):
            parts.append("aggregation")
        if inputs.get("has_concatenation"):
            parts.append("concatenation")
        if cross_entity_raw:
            parts.append("cross-entity calculation")
        if conditional:
            parts.append("conditional logic")
        reason = "; ".join(parts) if parts else None

    return (unnecessary, cross_entity, reason, multi_entity)


def _summarize_record_recommendations(
    record_recs: list[dict],
) -> str | None:
    """Format the inline Scoring-Summary-sheet pointer to the Recommendations sheet.

    Joins the dimension names (deduped, sorted by appearance) so the
    Scoring Summary reader can see at a glance which axes triggered.
    Cost-axis rows append the signed tier delta (e.g. ``nachos_score
    (-3)``) as the impact summary the brief calls out. Returns ``None``
    when the record has no firing recommendations — the cell is left
    empty so reviewers can sort by "rows that need attention" trivially.
    """
    if not record_recs:
        return None
    seen: list[str] = []
    deltas: dict[str, int] = {}
    for r in record_recs:
        dim = r.get("dimension")
        if not dim:
            continue
        if dim not in seen:
            seen.append(dim)
        impact = r.get("impact")
        if isinstance(impact, int) and impact != 0 and dim not in deltas:
            deltas[dim] = impact
    if not seen:
        return None
    parts: list[str] = []
    for dim in seen:
        if dim in deltas:
            sign = "+" if deltas[dim] > 0 else ""
            parts.append(f"{dim} ({sign}{deltas[dim]})")
        else:
            parts.append(dim)
    return f"{len(seen)} recs: " + ", ".join(parts)


# --- Row context -----------------------------------------------------------


class ScoreFields(NamedTuple):
    complex_bl: int | None
    nachos_tier: int | None
    adjusted: float | None
    justification: str | None


class IntegrationProfile(NamedTuple):
    structural_depth: str | None
    doc_style: str | None
    doc_gap: str | None
    doc_gap_reason: str | None


class LegacyFields(NamedTuple):
    unnecessary_ext: str | None
    cross_entity: str | None
    complexity_signals: str | None
    multi_entity: str | None


class ReviewCells(NamedTuple):
    needs_review: str | None
    route: str | None


class HumanCells(NamedTuple):
    """One human-scored row joined onto a Details row (`--with-human`)."""

    tier: int | None
    adj: float | None


@dataclass
class RowContext:
    """Everything a per-element column extractor may read.

    One instance per rendered row. The value-group helpers are exposed as
    cached properties so each group is computed once per row regardless
    of how many columns read it — the same per-row cost as the old
    monolithic row builders.

    ``recs`` carries the record's Recommendations-sheet rows (for the
    Scoring Summary pointer column); ``row_number`` the canonical-sort
    address injected as the leading ``Row #`` cell.
    """

    state: str
    record: ElementRecord
    is_extension: bool
    edfi_domain: str | None
    score: dict | None = None
    recs: list[dict] = field(default_factory=list)
    row_number: int | None = None
    # Option C round-trip: this row's ingested analyst-input values
    # ({column key: value} from the curation sidecar), or None. Read by
    # the `role="analyst_input"` band extractors only.
    curation: dict[str, CellValue] | None = None
    # Issue #248 Part C: this row's resolved adjudication (lens-filtered,
    # freshness-computed by `curation.adjudications_for`), or None. Read
    # by the `effective_score` extractor only; STALE blocks render blank.
    adjudication: dict | None = None
    # `--with-human`: the human-scored row joined by record key, or None
    # when the human file has no row for this element. Read by the
    # render-time CMP_COLUMNS only.
    human: HumanCells | None = None
    # Issue #259 PR 2: this record's merged review-queue signal
    # ({"priority": 1-6 | None, "why": str}) derived from the SAME
    # `_review_queue_entries` the queue renders — never recomposed — or
    # None when the record has no queue row. Read by the `review_why` /
    # `review_priority` extractors only.
    review_flag: dict | None = None

    @cached_property
    def score_fields(self) -> ScoreFields:
        return ScoreFields(*_score_fields(self.score))

    @cached_property
    def integration_profile(self) -> IntegrationProfile:
        return IntegrationProfile(*_integration_profile_fields(self.score))

    @cached_property
    def legacy(self) -> LegacyFields:
        return LegacyFields(*_legacy_template_fields(self.score, self.is_extension))

    @cached_property
    def review(self) -> ReviewCells:
        return ReviewCells(*_review_cells(self.score))


Extractor = Callable[[RowContext], CellValue]


# --- Spec dataclasses -------------------------------------------------------


@dataclass(frozen=True)
class ColumnSpec:
    """One workbook column: identity, source, lens membership, presentation.

    ``key`` is the stable internal name; ``header`` is the external
    display label — the ONLY thing a stakeholder-facing rename (#174,
    Option B) touches. ``legend`` names the ``score.rubric`` glossary
    that defines this column's display vocabulary (lockstep-tested).
    """

    key: str
    header: str
    extract: Extractor
    lens: frozenset[str] = BOTH
    role: Role = "data"
    width: int | None = None  # None → sheet default
    number_format: str | None = None
    color_scale: tuple[float, float] | None = None
    flag_yes: bool = False
    # Data cells wrap text (top-aligned) — prose columns.
    wrap: bool = False
    legend: str | None = None
    doc: str = ""


@dataclass(frozen=True)
class _SheetFinish:
    """Presentation finish applied by ``workbook_render._finalize_workbook``
    as a workbook-level post-pass. Columns are resolved BY HEADER NAME at
    apply time, so one finish serves both lenses; headers absent from a
    given sheet are skipped.

    NOTE the finish is deliberately NOT derived from the ColumnSpecs:
    the Audit Trail reuses Reviewer View columns but applies no number
    formats and scales only its lowercase NACHOS audit block — per-sheet
    presentation is a real degree of freedom, so it stays explicit here.
    """

    group: str  # tab-color group: "orient" | "work" | "audit"
    freeze: str | None = None
    autofilter: bool = False
    # (header name, Excel number format) pairs.
    number_formats: tuple[tuple[str, str], ...] = ()
    # (header name, scale min, scale max) — 3-color scale white→yellow→red.
    color_scale: tuple[tuple[str, float, float], ...] = ()
    # Header names whose "Yes" cells get the highlight fill.
    flag_yes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SheetSpec:
    """One workbook sheet: title, orientation guide, presentation finish,
    and (for table sheets) the ordered column list.

    ``columns_factory`` replaces ``columns`` for sheets whose per-lens
    column orders genuinely diverge (Audit Trail — the fact blocks do
    not interleave). ``row_number_column`` injects the leading
    ``Row #`` address at render time; it is deliberately not a
    ColumnSpec (see module docstring).
    """

    title: str
    # (what it shows, who it's for) — feeds the generated Readme sheet.
    guide: tuple[str, str] | None = None
    # Presentation finish — feeds `_finalize_workbook`.
    finish: _SheetFinish | None = None
    kind: Literal["table", "block"] = "table"
    columns: tuple[ColumnSpec, ...] = ()
    columns_factory: Callable[[str], tuple[ColumnSpec, ...]] | None = None
    row_number_column: bool = False
    lens: frozenset[str] = BOTH
    scope: frozenset[str] = frozenset({"per_state", "combined"})
    default_width: int = 22

    def columns_for(self, lens: str) -> tuple[ColumnSpec, ...]:
        if self.columns_factory is not None:
            return self.columns_factory(lens)
        return tuple(c for c in self.columns if lens in c.lens)

    def headers_for(self, lens: str) -> tuple[str, ...]:
        return tuple(c.header for c in self.columns_for(lens))


# --- Details columns (Option B template-native reshape, issue #186) --------
#
# The analysts' canonical grid in four visually distinct bands:
#   1. identity (grey headers)
#   2. scoring, headline-axis order — Adjusted BEFORE Base, adjustments
#      itemized as components, prose justifications (issue #136 shape)
#   3. analyst-input space (light-green headers, `role="analyst_input"`)
#      — empty BY DESIGN; the pipeline never writes here (round-trip
#      lands with Option C)
#   4. `AI:`-prefixed machine annotations, far right ("annotations live
#      to the far right" — the analysts' own convention)
#
# The `ai-` comparison-workbook projection (`human_score_backfill`) takes
# every non-analyst_input column; `AI:`-prefixed headers are not
# double-prefixed there.


def _x_prose_justification(c: RowContext) -> CellValue:
    from src.score.rubric import render_justification_prose

    return render_justification_prose(
        c.score_fields.justification, c.score_fields.nachos_tier
    )


def _x_score_adjustments(c: RowContext) -> CellValue:
    from src.score.rubric import render_adjustments_prose

    return render_adjustments_prose(c.score_fields.justification)


def _x_effective_score(c: RowContext) -> CellValue:
    """Issue #248 Part C: the FRESH adjudicated value, else blank.

    Blank means "no adjudication or stale" — a stale consensus never
    renders (the row re-enters the Review Queue as RE-ADJUDICATE
    instead), so the reader can never mistake machine output for
    human-ratified output or vice versa."""
    adj = c.adjudication
    if adj and adj.get("fresh"):
        return adj.get("value")
    return None


def _x_reason_for_extension_necessity(c: RowContext) -> CellValue:
    """Prose for the analysts' "Reason for extension necessity" column.

    Extension rows only: the `extension_justification` rule-path sentence
    (from `rubric.RULE_MEANINGS`), plus the first validated
    `extension_is_necessary` evidence span, plus the documented sourcing
    constraint when one was extracted (non-default enum values only).
    """
    if not c.is_extension or not c.score:
        return None
    from src.score.rubric import RULE_MEANINGS

    bits: list[str] = []
    ext_dim = ((c.score or {}).get("dimensions") or {}).get(
        "extension_justification"
    ) or {}
    rule = ext_dim.get("rule_matched")
    if rule:
        bits.append(RULE_MEANINGS.get(rule, rule))
    fp = (c.score or {}).get("fact_provenance") or {}
    spans = (fp.get("extension_is_necessary") or {}).get("spans") or []
    texts = [s for s in spans if isinstance(s, str) and s]
    if texts:
        bits.append(f'— evidence: "{texts[0]}"')
    constraint = (fp.get("sourcing_constraint_documented") or {}).get("value")
    if constraint and constraint not in ("none", "unspecified"):
        bits.append(f"documented sourcing constraint: {constraint}")
    return " ".join(bits) or None


def _x_references_doc_only(c: RowContext) -> CellValue:
    """Source doc / page pointers ONLY — regulatory citations moved to
    their own `Legislation` column (they were previously folded in)."""
    doc = c.record.source_document
    loc = c.record.source_page_or_section
    if doc and loc:
        return f"{doc} / {loc}"
    return doc or loc or None


def _x_legislation(c: RowContext) -> CellValue:
    if c.record.regulatory_citations:
        return "; ".join(c.record.regulatory_citations)
    return None


# Decision-driving facts surfaced in `AI: Evidence`, in scan order.
_EVIDENCE_FACTS: tuple[str, ...] = (
    "has_aggregation",
    "has_concatenation",
    "has_cross_entity_logic",
    "has_conditional_logic",
    "extension_is_necessary",
)


def evidence_from_score(score: dict | None) -> CellValue:
    """First validated span from the decision-driving facts (affirmative
    values only) + a count pointer at the rest — compactness is the
    analysts' revealed preference; the full spans live on the audit
    workbook (`poc3 report audit`).

    Score-dict-level so non-RowContext surfaces (the Review Queue sheet
    builder) can reuse it.
    """
    fp = (score or {}).get("fact_provenance") or {}
    found: list[tuple[str, str]] = []
    for fact in _EVIDENCE_FACTS:
        entry = fp.get(fact)
        if not isinstance(entry, dict) or entry.get("value") is not True:
            continue
        for span in entry.get("spans") or []:
            if isinstance(span, str) and span:
                found.append((fact, span))
    if not found:
        return None
    fact, span = found[0]
    more = len(found) - 1
    cell = f'{fact}: "{span}"'
    if more:
        cell += f" (+{more} more — see audit workbook)"
    return cell


def _x_evidence(c: RowContext) -> CellValue:
    return evidence_from_score(c.score)


# --- Human-comparison surface (`report analyst --with-human`) ---------------
#
# The June follow-up comparison format, generalized: when a human-scored
# file is supplied, four `Cmp:` columns append to Details AT RENDER TIME
# (`details_sheet_with_cmp`). They never enter DETAILS_COLUMNS, so the
# `ai-` backfill projection and the Audit Trail factory are structurally
# unaffected.


def classify_human_vs_ai(
    human_tier: int | None,
    human_adj: float | None,
    ai_tier: int | None,
    ai_adj: float | None,
) -> str:
    """Classification matching ``review_comparison._classify_joined``
    shape (hoisted from ``human_score_backfill._classify``, which now
    delegates here — one source of truth for the bucket vocabulary).

    Buckets:
      - ``match_exact``  — tier equal AND adj within 0.01
      - ``match_tier``   — tier equal, adj differs
      - ``tier_delta_1`` — |tier_delta| == 1
      - ``tier_delta_ge2`` — |tier_delta| ≥ 2
      - ``ai_only``      — human tier blank, ai tier present
      - ``human_only``   — human tier present, ai tier blank
      - ``neither``      — both blank
    """
    if human_tier is None and ai_tier is None:
        return "neither"
    if human_tier is None:
        return "ai_only"
    if ai_tier is None:
        return "human_only"
    if human_tier == ai_tier:
        if (
            human_adj is not None
            and ai_adj is not None
            and abs(human_adj - ai_adj) < 0.01
        ):
            return "match_exact"
        if human_adj is None and ai_adj is None:
            return "match_exact"
        return "match_tier"
    if abs(human_tier - ai_tier) == 1:
        return "tier_delta_1"
    return "tier_delta_ge2"


def _x_cmp_status(c: RowContext) -> CellValue:
    # Blank when the human file has no row — 3k+ rows of `ai_only` is
    # noise; filterable blanks match the June format's revealed shape.
    if c.human is None:
        return None
    return classify_human_vs_ai(
        c.human.tier,
        c.human.adj,
        c.score_fields.nachos_tier,
        (c.score or {}).get("adjusted_nachos_score"),
    )


def _x_cmp_base_delta(c: RowContext) -> CellValue:
    if c.human is None or c.human.tier is None:
        return None
    ai_tier = c.score_fields.nachos_tier
    if ai_tier is None:
        return None
    return c.human.tier - ai_tier


def _x_cmp_adj_delta(c: RowContext) -> CellValue:
    if c.human is None or c.human.adj is None:
        return None
    ai_adj = (c.score or {}).get("adjusted_nachos_score")
    if not isinstance(ai_adj, (int, float)):
        return None
    return round(c.human.adj - float(ai_adj), 2)


def _x_cmp_why(c: RowContext) -> CellValue:
    if c.human is None:
        return None
    ht, ha = c.human.tier, c.human.adj
    at = c.score_fields.nachos_tier
    aa = (c.score or {}).get("adjusted_nachos_score")
    if ht is None and at is None:
        return "no scores either side"
    if ht is None:
        return "no human score"
    if at is None:
        return "no engine score"
    deltas = [f"Δbase {ht - at:+d}"]
    if ha is not None and isinstance(aa, (int, float)):
        deltas.append(f"Δadj {ha - float(aa):+.1f}")
    return f"human {ht} vs AI {at} ({', '.join(deltas)})"


CMP_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        "cmp_status", "Cmp: Status", _x_cmp_status, role="cmp", width=14,
        doc="Human-vs-AI bucket (classify_human_vs_ai vocabulary); blank "
            "when the human file has no row for this element.",
    ),
    ColumnSpec(
        "cmp_base_delta", "Cmp: Base Δ", _x_cmp_base_delta, role="cmp",
        width=10, doc="human base tier − AI base tier.",
    ),
    ColumnSpec(
        "cmp_adj_delta", "Cmp: Adj Δ", _x_cmp_adj_delta, role="cmp",
        width=10, doc="human adjusted − AI adjusted.",
    ),
    ColumnSpec(
        "cmp_why", "Cmp: Why", _x_cmp_why, role="cmp", width=44, wrap=True,
        doc="One-line comparison trace (the June follow-up format).",
    ),
)


def details_sheet_with_cmp() -> SheetSpec:
    """The Details SheetSpec with the render-time `Cmp:` block appended
    (used only when `report analyst --with-human` supplies a file)."""
    return replace(DETAILS_SHEET, columns=(*DETAILS_COLUMNS, *CMP_COLUMNS))


def _analyst_input_col(
    key: str,
    header: str,
    *,
    number_format: str | None = None,
    width: int | None = None,
) -> ColumnSpec:
    return ColumnSpec(
        key, header,
        # Analyst-owned cells re-apply ingested curation values (Option C
        # round-trip); the pipeline itself never computes anything here.
        lambda c, _k=key: (c.curation or {}).get(_k),
        role="analyst_input",
        number_format=number_format,
        width=width,
        doc=(
            "Analyst-input space — the pipeline never writes here; "
            "entries round-trip via `poc3 review ingest` and are "
            "re-applied on every regeneration (curation sidecar at "
            "data/curation/{state}.json)."
        ),
    )


DETAILS_COLUMNS: tuple[ColumnSpec, ...] = (
    # --- Band 1: identity -------------------------------------------------
    ColumnSpec(
        "state", "State", lambda c: c.state, role="identity",
        doc="Two-letter state code.",
    ),
    ColumnSpec(
        "source_area", "Source Area", lambda c: c.record.domain,
        role="identity",
        doc=(
            "State source-document reporting area (post-#184: never the "
            "Ed-Fi domain; blank on swagger-backfill rows)."
        ),
    ),
    ColumnSpec(
        "entity", "Entity Name", lambda c: c.record.entity, role="identity",
        doc="Canonical Ed-Fi entity (resolved against the API model).",
    ),
    ColumnSpec(
        "edfi_domain", "Ed-Fi Domain", lambda c: c.edfi_domain,
        role="identity",
        doc=(
            "Ed-Fi Data Standard domain(s), derived from the Ed-Fi "
            "Swagger/API model via the canonical resolver (issue #184); "
            "'; '-joined when multiple."
        ),
    ),
    ColumnSpec(
        "element", "Data Element", lambda c: c.record.element_name,
        role="identity",
        doc="Element / property name.",
    ),
    ColumnSpec(
        "data_type", "Data Type", lambda c: c.record.data_type,
        role="identity",
        doc="API-model contract data type.",
    ),
    ColumnSpec(
        "edfi_standard_definition", "Ed-Fi Standard Definition",
        lambda c: c.record.edfi_standard_definition,
        lens=SPINE, width=55, role="identity",
        doc=(
            "What the Ed-Fi spec says (API-model lens only) — context "
            "for undocumented rows whose Business Logic is empty."
        ),
    ),
    # --- Band 2: scoring, headline-axis order ------------------------------
    ColumnSpec(
        "business_logic", "Business Logic",
        lambda c: c.record.definition_text or None,
        width=55,
        doc="State-authored definition prose (issue #102 rename).",
    ),
    ColumnSpec(
        "complex_business_logic", "Complex Business Logic",
        lambda c: c.score_fields.complex_bl,
        role="score", number_format="0",
        doc=(
            "business_logic_complexity cost tier 0-3 (v23 graduated it "
            "to the source lens; issue #106)."
        ),
    ),
    # Headline axis FIRST — the June PPV misreading was reviewers scoring
    # the adjusted axis against a workbook that led with the base tier.
    ColumnSpec(
        "adjusted_nachos_score", "Adjusted NACHOS Score",
        lambda c: c.score_fields.adjusted,
        role="score", number_format="0.0", color_scale=(0, 4.5),
        legend="ADJUSTMENT_MEANINGS",
        doc=(
            "THE headline score: base tier + adjustments, capped at 4.5 "
            "— the axis the human rubric scores."
        ),
    ),
    # Issue #248 Part C — the human-consensus layer beside the engine
    # column. No color_scale on purpose: visually distinct from the
    # machine score it sits next to.
    ColumnSpec(
        "effective_score", "Effective Score (adjudicated)",
        _x_effective_score,
        role="score", number_format="0.0", width=14,
        doc=(
            "Team-consensus adjudicated score (`poc3 review adjudicate`) "
            "— blank unless a FRESH adjudication exists for this lens, so "
            "machine output is never mistaken for human-ratified output. "
            "Stale adjudications (engine or plan moved since the "
            "decision) render blank and flag RE-ADJUDICATE on the Review "
            "Queue. The engine score is never modified."
        ),
    ),
    ColumnSpec(
        "base_nachos_score", "Base NACHOS Score",
        lambda c: c.score_fields.nachos_tier,
        role="score", number_format="0", color_scale=(0, 3),
        legend="NACHOS_TIER_MEANINGS",
        doc="Base NACHOS methodology tier 0-3 (blank when unscored).",
    ),
    ColumnSpec(
        "score_adjustments", "Score Adjustments",
        _x_score_adjustments,
        role="score", width=34, legend="ADJUSTMENT_MEANINGS",
        doc=(
            "Itemized adjustment components (prose) — Adjusted = Base + "
            "these; blank when none applied."
        ),
    ),
    ColumnSpec(
        "nachos_justification", "Justification for Adjusted NACHOS Score",
        _x_prose_justification,
        role="score", width=60, legend="RULE_MEANINGS",
        doc=(
            "Prose sentence: base rule path + adjustments (raw tokens "
            "survive on the audit workbook only — poc3 report audit)."
        ),
    ),
    ColumnSpec(
        "unnecessary_extension", "Unnecessary Extension ?",
        lambda c: c.legacy.unnecessary_ext,
        role="score",
        doc=(
            "From the +1 unnecessary / +0.5 necessary extension "
            "adjustment (issue #107); N/A on core rows, blank when "
            "necessity is unresolved."
        ),
    ),
    ColumnSpec(
        "cross_entity", "Cross Entity Calculation ?",
        lambda c: c.legacy.cross_entity,
        role="score",
        doc="has_cross_entity_logic as the scoring rule consumed it.",
    ),
    ColumnSpec(
        "reason_for_extension_necessity", "Reason for extension necessity",
        _x_reason_for_extension_necessity,
        role="score", width=70, wrap=True,
        doc=(
            "Extension rows: the necessity rule-path sentence + first "
            "evidence quote + documented sourcing constraint."
        ),
    ),
    ColumnSpec(
        "business_logic_formula", "Business Logic (Formula)",
        lambda c: c.record.business_rules_text,
        width=55,
        doc="State-authored business rules / formula text.",
    ),
    ColumnSpec(
        "reason_for_complexity", "Reason for Complexity",
        lambda c: c.legacy.complexity_signals,
        role="score",
        doc=(
            "Detected patterns (aggregation; concatenation; cross-entity; "
            "conditional) — the analysts' original column name, safe "
            "again because the tier-0 blank gate holds (issue #107)."
        ),
    ),
    ColumnSpec(
        "multi_entity", "Multiple Entities Involved",
        lambda c: c.legacy.multi_entity,
        role="score",
        doc="cross_entity_targets >= 2 (drives the +0.5 multi-entity adj).",
    ),
    ColumnSpec(
        "is_extension", "Is an extension",
        lambda c: "Yes" if c.is_extension else "No",
        doc="record.source == 'extension' (element-level, not entity).",
    ),
    ColumnSpec(
        "contributing_extension", "Contributing extension",
        lambda c: c.record.extension_name,
        doc="Extension namespace that contributed the element.",
    ),
    ColumnSpec(
        "references", "References",
        _x_references_doc_only,
        width=40,
        doc=(
            "Source doc / page pointers (regulatory citations now have "
            "their own Legislation column)."
        ),
    ),
    ColumnSpec(
        "legislation", "Legislation",
        _x_legislation,
        width=40,
        doc=(
            "Regulatory citations from ingest (`regulatory_citations`) — "
            "previously folded into References, never surfaced standalone."
        ),
    ),
    # --- Band 3: analyst-input space ---------------------------------------
    # `Reviewed?` (the approval-column ask, 05-01 session) and the §8.4
    # score overrides lead the band — the approval/override actions sit
    # immediately after the engine's scoring columns. Two override axes
    # (issue #250): adjusted (the headline score) and base (the rule-
    # cascade tier), so the analyst can say which layer they disagree
    # with. Neither override ever replaces the engine score;
    # disagreement routes to review (`curation.override_disagreements`,
    # one Review Queue row per contested axis).
    _analyst_input_col("reviewed", "Reviewed?"),
    _analyst_input_col(
        "analyst_adjusted_override", "Analyst Adjusted Score (override)",
        number_format="0.0", width=14,
    ),
    # Base-axis override accepts half-tier judgments (e.g. 1.5) — hence
    # "0.0" rather than the engine base column's integer "0" format.
    _analyst_input_col(
        "analyst_base_override", "Analyst Base Score (override)",
        number_format="0.0", width=14,
    ),
    _analyst_input_col("required", "Required"),
    _analyst_input_col("recommendations", "Recommendations"),
    _analyst_input_col("edfi_comments", "Ed-Fi Comments"),
    _analyst_input_col("ds_next_steps", "DS Next Steps"),
    _analyst_input_col("state_response", "State Response"),
    _analyst_input_col("kb_reviewed", "KB Reviewed"),
    _analyst_input_col("reviewed_with_state", "Reviewed with State"),
    _analyst_input_col("validated_by", "Validated By"),
    # --- Band 4: AI machinery, far right ------------------------------------
    ColumnSpec(
        "match_status", "AI: Match Status",
        lambda c: _match_status(c.record),
        role="ai", legend="MATCH_STATUS_MEANINGS",
        doc="API-model match confidence derived from record.source.",
    ),
    ColumnSpec(
        "documented", "AI: Documented",
        lambda c: "Yes" if c.record.documented else "No",
        lens=SPINE, role="ai",
        doc=(
            "API-model lens: did the state's source doc mention this "
            "canonical slot?"
        ),
    ),
    ColumnSpec(
        "documentation_source", "AI: Documentation Source",
        lambda c: _doc_source_label(c.record),
        role="ai", legend="DOC_SOURCE_MEANINGS",
        doc="Source Doc / Swagger / Swagger (leaf) provenance (issue #70).",
    ),
    ColumnSpec(
        "implementation_shape", "AI: Implementation Shape",
        lambda c: c.integration_profile.structural_depth,
        role="ai", legend="STRUCTURAL_DEPTH_MEANINGS",
        doc=(
            "Flat / Light / Moderate / Deep — the field's structural "
            "shape in the Ed-Fi Swagger/API model (#174 rename of "
            "Structural Depth)."
        ),
    ),
    ColumnSpec(
        "documentation_style", "AI: Documentation Style",
        lambda c: c.integration_profile.doc_style,
        role="ai", legend="DOC_STYLE_MEANINGS",
        doc="How the state writes the field (Silent = no narrative).",
    ),
    ColumnSpec(
        "documentation_gap", "AI: Documentation Gap",
        lambda c: c.integration_profile.doc_gap,
        role="ai",
        doc=(
            "Yes when docs look thinner than the implementation shape "
            "requires (the Reason column retired to the audit workbook — 5% "
            "populated)."
        ),
    ),
    ColumnSpec(
        "confidence", "AI: Confidence",
        lambda c: (c.score or {}).get("confidence_composite"),
        role="ai", legend="CONFIDENCE_MEANINGS",
        doc="Composite extraction confidence (high / medium / low).",
    ),
    ColumnSpec(
        "evidence", "AI: Evidence",
        _x_evidence,
        role="ai", width=80, wrap=True,
        doc=(
            "First validated evidence quote from the decision-driving "
            "facts (+N more pointer; full spans on the audit workbook)."
        ),
    ),
    ColumnSpec(
        "needs_review", "AI: Needs Review",
        lambda c: c.review.needs_review,
        role="ai", flag_yes=True,
        doc=(
            "POC-3's own cascade flag (issue #113) — NOT a reviewer "
            "verdict; blank when unscored."
        ),
    ),
    ColumnSpec(
        "review_route", "AI: Review Route",
        lambda c: c.review.route,
        role="ai",
        doc="POLICY / DATA_MODEL / SCORING / ANALYST when flagged.",
    ),
    # Issue #259 PR 2 — the queue's signal, merged per record, so a
    # pass can be worked entirely inside Details: filter Needs Review,
    # sort by Priority, read Why in place, write in the band. Values
    # derive from the SAME `_review_queue_entries` the queue renders
    # (threaded via `RowContext.review_flag`) — the texts can never
    # diverge. Both columns are excluded from `backfill_columns` AND
    # `audit_trail_columns` (the `effective_score` posture): the `ai-`
    # comparison overlay measures the ENGINE, and the audit trail
    # already carries the raw cascade reasons. NOTE: keep
    # `ai_recommendations` the LAST Details column — the audit-prefix
    # test derives its offset from that position.
    ColumnSpec(
        "review_why", "AI: Review Why",
        lambda c: (c.review_flag or {}).get("why"),
        role="ai", width=80, wrap=True,
        doc=(
            "Why this row is on the Review Queue — cascade reasons "
            "('; '-joined), override-disagreement sentence(s), and/or "
            "the stale-adjudication sentence; blank when not flagged."
        ),
    ),
    ColumnSpec(
        "review_priority", "AI: Review Priority",
        lambda c: (c.review_flag or {}).get("priority"),
        role="ai", width=10,
        doc=(
            "Queue-ladder position: 1 RE-ADJUDICATE, 2 OVERRIDE, 3-6 "
            "POLICY→ANALYST; blank when not flagged. Sort Details by "
            "this to work in queue order; restore via Row #."
        ),
    ),
    ColumnSpec(
        "ai_recommendations", "AI: Recommendations",
        lambda c: _summarize_record_recommendations(c.recs),
        role="ai", width=60,
        doc=(
            "Pointer at this row's Recommendations-sheet entries "
            "(dimension list + cost-axis delta) — replaces the retired "
            "per-state Scoring Summary pointer."
        ),
    ),
)

# Compat alias for the pre-Option-B name (audit factory, analyst imports).
REVIEWER_VIEW_COLUMNS = DETAILS_COLUMNS


# --- Scoring Summary columns ------------------------------------------------
#
# Reproduces `_SPINE_SCORES_HEADERS` / `_SOURCE_SCORES_HEADERS` exactly:
# shared identity block, then per-lens dimension columns (the two lens
# orders interleave cleanly), then the shared NACHOS/review tail.


def _dim_value(name: str) -> Extractor:
    def extract(c: RowContext) -> CellValue:
        dims = (c.score or {}).get("dimensions", {})
        dim_info = dims.get(name)
        return dim_info.get("value") if isinstance(dim_info, dict) else None

    return extract


def _dim_col(name: str, lens: frozenset[str]) -> ColumnSpec:
    return ColumnSpec(
        f"dim_{name}", name, _dim_value(name), lens=lens, role="score",
        legend="DIMENSION_TIER_MEANINGS",
        doc=f"{name} dimension tier.",
    )


SCORING_SUMMARY_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("state", "State", lambda c: c.state, role="identity"),
    ColumnSpec(
        "entity", "Entity Name", lambda c: c.record.entity, role="identity"
    ),
    ColumnSpec(
        "element", "Data Element", lambda c: c.record.element_name,
        role="identity",
    ),
    _dim_col("documentation_completeness", SPINE),
    _dim_col("obligation_clarity", SPINE),
    _dim_col("canonical_name_alignment", SOURCE),
    _dim_col("definition_quality", SOURCE),
    _dim_col("semantic_fidelity", SOURCE),
    _dim_col("extension_justification", SOURCE),
    # v23 — issue #106 (Q4): business_logic_complexity on both lenses.
    _dim_col("business_logic_complexity", BOTH),
    ColumnSpec(
        "complexity_score", "complexity_score",
        lambda c: (c.score or {}).get("complexity_score"),
        role="score", number_format="0",
        doc="Cost-axis tier (0-3).",
    ),
    # Phase F NACHOS block. v10 — `in_scope` column dropped.
    ColumnSpec(
        "nachos_score", "nachos_score",
        lambda c: c.score_fields.nachos_tier,
        role="score", number_format="0", color_scale=(0, 3),
        legend="NACHOS_TIER_MEANINGS",
    ),
    ColumnSpec(
        "adjusted_nachos_score", "adjusted_nachos_score",
        lambda c: (c.score or {}).get("adjusted_nachos_score"),
        role="score", number_format="0.0", color_scale=(0, 4.5),
    ),
    ColumnSpec(
        "nachos_justification", "nachos_justification",
        lambda c: (c.score or {}).get("nachos_justification"),
        role="score", legend="RULE_MEANINGS",
    ),
    ColumnSpec(
        "confidence_composite", "confidence_composite",
        lambda c: (c.score or {}).get("confidence_composite"),
        role="ai", legend="CONFIDENCE_MEANINGS",
    ),
    ColumnSpec(
        "needs_review", "needs_review",
        lambda c: "Yes"
        if ((c.score or {}).get("review") or {}).get("needs_review")
        else "No",
        role="ai", flag_yes=True,
    ),
    ColumnSpec(
        "review_reasons", "review_reasons",
        lambda c: "; ".join(
            ((c.score or {}).get("review") or {}).get("reasons", []) or []
        )
        or None,
        role="ai", width=80,
        doc="Every flagged review signal, '; '-joined for grep-ability.",
    ),
    # Track C scorecard integration (Phase 2) — inline pointer from
    # Scoring Summary row → Recommendations sheet row(s).
    ColumnSpec(
        "recommendations", "recommendations",
        lambda c: _summarize_record_recommendations(c.recs),
        role="ai", width=60,
        doc="Dimension list + cost-axis delta, e.g. 'nachos_score (-3)'.",
    ),
)


# --- Sheet specs ------------------------------------------------------------
#
# One SheetSpec per workbook sheet: title, orientation guide (feeds the
# generated Readme), presentation finish (feeds `_finalize_workbook`),
# and — for table sheets — the ordered column list. `SHEET_SPECS` at the
# bottom of this module is the single title-keyed registry; a sheet
# title missing from it raises KeyError at build time (non-drift guard).

# Shared by Details and the spine-lens "{ST} — Documented only" family
# (resolved via the suffix rule in `workbook_render._finish_for`).
_DETAILS_FINISH = _SheetFinish(
    group="work",
    freeze="H2",  # Row # + identity cols A-G stay visible while scrolling
    autofilter=True,
    number_formats=(
        ("Adjusted NACHOS Score", "0.0"),
        ("Effective Score (adjudicated)", "0.0"),
        ("Base NACHOS Score", "0"),
        ("Complex Business Logic", "0"),
        ("Analyst Adjusted Score (override)", "0.0"),
        ("Analyst Base Score (override)", "0.0"),
        # `--with-human` render-time columns — header-name resolution
        # simply skips these when the Cmp: block isn't rendered.
        ("Cmp: Base Δ", "0"),
        ("Cmp: Adj Δ", "0.0"),
    ),
    color_scale=(
        ("Adjusted NACHOS Score", 0, 4.5),
        ("Base NACHOS Score", 0, 3),
    ),
    flag_yes=("AI: Needs Review",),
)

DETAILS_SHEET = SheetSpec(
    title="Details",
    guide=(
        "The working grid — one row per element in the analysts' "
        "canonical column order: identity, then scoring with Adjusted "
        "NACHOS Score as the headline axis (base tier + itemized "
        "adjustments shown as components; Effective Score (adjudicated) "
        "beside it renders team consensus when one exists — the engine "
        "score is never modified), then the green analyst-input band "
        "(yours — the pipeline never writes there), then the "
        "AI-prefixed machine annotations far right. Leading `Row #` is "
        "the stable cross-sheet row address (same number on Audit "
        "Trail; survives your own re-sorts and filters).",
        "Analysts + stakeholders",
    ),
    finish=_DETAILS_FINISH,
    columns=DETAILS_COLUMNS,
    row_number_column=True,
)

# Compat alias (pre-Option-B name).
REVIEWER_VIEW_SHEET = DETAILS_SHEET


def backfill_columns(lens: str) -> tuple:
    """The ``human_score_backfill`` comparison projection (issue #213
    item 1 — pure spec logic, moved here from ``analyst``): every
    Details column EXCEPT the analyst-input band (eight always-blank
    ``ai-`` columns there would be the issue-#102 anti-pattern), the
    renderer-injected ``Row #`` (never a spec column by design), and
    ``ai_recommendations`` — the overlay flow never joins the
    recommendations artifact, so the column shipped ~2,700 always-blank
    cells (the exact #102 anti-pattern; excluded like the analyst band
    rather than joined, because the overlay's job is score comparison,
    not remediation prose — issue #213 item 1). The analyst workbooks'
    own Details sheet keeps the populated ``AI: Recommendations``.
    ``effective_score`` is likewise excluded (issue #248 Part C): the
    overlay measures the ENGINE against human scores, and a
    human-consensus column folded into the ``ai-`` projection would
    contaminate the very comparison it exists for. ``review_why`` /
    ``review_priority`` follow the same posture (issue #259 PR 2):
    they are queue-navigation signal, not engine output the overlay
    compares — and the overlay flow builds contexts without the
    threaded queue entries, so they would ship always-blank.
    """
    return tuple(
        c for c in DETAILS_SHEET.columns_for(lens)
        if c.role != "analyst_input"
        and c.key not in (
            "ai_recommendations", "effective_score",
            "review_why", "review_priority",
        )
    )

SCORING_SUMMARY_SHEET = SheetSpec(
    title="Scoring Summary",
    guide=(
        "Per-record dimension scores (one row per scored element). "
        "Sort by 'recommendations' or 'needs_review' to triage rows "
        "that need attention. `Row #` matches Reviewer View — gaps in "
        "the sequence are unscored rows.",
        "Analysts",
    ),
    finish=_SheetFinish(
        group="work",
        freeze="E2",  # Row # / State / Entity / Element stay visible
        autofilter=True,
        number_formats=(
            ("nachos_score", "0"),
            ("adjusted_nachos_score", "0.0"),
            ("complexity_score", "0"),
        ),
        color_scale=(
            ("nachos_score", 0, 3),
            ("adjusted_nachos_score", 0, 4.5),
        ),
        flag_yes=("needs_review",),
    ),
    columns=SCORING_SUMMARY_COLUMNS,
    row_number_column=True,
)


# --- Audit Trail ------------------------------------------------------------
#
# The one sheet whose per-lens column orders genuinely diverge (the fact
# blocks share facts at different relative positions), so it composes
# via a `columns_factory` instead of per-column lens membership: the
# lens-filtered Reviewer View columns (widths reset — the audit sheet is
# uniformly compact), then per-fact provenance values, companion span
# columns, per-dim tiers, the NACHOS methodology block, and the review
# tail. Header/value alignment holds by construction — each ColumnSpec
# carries its own extractor.

_SOURCE_FACT_ORDER: tuple[str, ...] = (
    "element_name_matches_canonical",
    "naming_deviation_cosmetic",
    "extension_mirrors_core_pattern",
    "definition_present",
    "definition_text_substantive",
    "definition_adds_detail_beyond_edfi",
    "semantic_class",
    # Side-quest PR A (2026-04-28): merged scope-delta enum replaces the
    # two pre-PR-A booleans — one column for the direction label.
    "state_scope_delta",
    "extension_is_necessary",
    "extension_is_standalone",
    # Integration Profile — per-element doc style label (also on
    # Reviewer View as a display label; audited raw here).
    "documentation_style",
    # Productization-signal fact — not a rule input.
    "integration_class",
)
_SPINE_FACT_ORDER: tuple[str, ...] = (
    "definition_present",
    "business_rules_present",
    "data_type_canonical",
    "definition_is_implementable",
    "descriptor_values_enumerated",
    "required_when_stated",
    "conditional_reporting_stated",
    "populations_or_scope_stated",
    "has_conditional_logic",
    "has_aggregation",
    "has_cross_entity_logic",
    "cross_entity_targets",
    "documentation_style",
    # Observability-only on spine (see `rules.LENS_OBSERVABILITY_FACTS`).
    "semantic_class",
    "integration_class",
)

_SEMANTIC_CLASS_SPANS_HEADER: str = "semantic_class_spans"
_INTEGRATION_CLASS_SPANS_HEADER: str = "integration_class_spans"

_SOURCE_DIM_ORDER: tuple[str, ...] = (
    "canonical_name_alignment",
    "definition_quality",
    "semantic_fidelity",
    "extension_justification",
    # v23 — issue #106: business_logic_complexity graduated to source.
    "business_logic_complexity",
    # Phase F — NACHOS methodology axis; distinct from quality axis.
    "nachos_score",
)
_SPINE_DIM_ORDER: tuple[str, ...] = (
    "documentation_completeness",
    "obligation_clarity",
    "business_logic_complexity",
    "nachos_score",
)

# Phase F — NACHOS methodology audit block (appended after dim tiers).
# v10 — `in_scope` column dropped (uniformly True after rectification).
_NACHOS_ELEMENTS_COLUMNS: tuple[str, ...] = (
    "nachos_tier_rule",
    "nachos_adjustments",
    "nachos_score",
    "adjusted_nachos_score",
)


def _format_fact_value(entry: dict | None):
    """Unwrap a fact_provenance entry for the workbook cell.

    Bool/int/enum values render verbatim; ``null`` becomes an empty
    cell. The accompanying `downgraded`/`downgrade_reason` fields are
    surfaced separately via the `downgraded_facts` column — we do NOT
    overload the fact column with that signal.
    """
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    if value is None:
        return None
    if isinstance(value, bool):
        return "True" if value else "False"
    return value


def _format_fact_spans(entry: dict | None) -> str | None:
    """Join the validated LLM-evidence spans on a ``fact_provenance``
    entry with `` | `` for a companion Audit-Trail-sheet column.

    ``_row_to_fact_result`` already drops ``valid=False`` entries, so
    anything surfaced on ``fact_provenance[fact].spans`` has passed
    substring validation. Missing key, empty list, or non-dict entry
    renders as a blank cell — matches the ``unknown`` / downgraded
    paths where no affirmative evidence survived.
    """
    if not isinstance(entry, dict):
        return None
    spans = entry.get("spans")
    if not isinstance(spans, list) or not spans:
        return None
    texts = [s for s in spans if isinstance(s, str) and s]
    return " | ".join(texts) if texts else None


def _synthesize_rule_paths(dimensions: dict | None) -> str | None:
    """`{dim}:{rule_matched}` pipe-separated — POC-2 `justification` analog.

    Gives analysts a compact audit trail of which rule fired per
    dimension without needing to cross-reference rules.py. Empty when
    no dimensions carry a `rule_matched` field (e.g., an unscored row).
    """
    if not isinstance(dimensions, dict):
        return None
    parts = []
    for dim, info in dimensions.items():
        if isinstance(info, dict):
            rule = info.get("rule_matched")
            if rule:
                parts.append(f"{dim}:{rule}")
    return " | ".join(parts) if parts else None


def _synthesize_downgraded_facts(fact_provenance: dict | None) -> str | None:
    """`{fact}:{reason}` pipe-separated for facts with `downgraded=True`.

    Only surfaces genuine downgrades — `filtered_by_source` (fact
    deliberately skipped for non-matching rows) is a separate concept
    that leaves `downgraded=False`, so those don't show here.
    """
    if not isinstance(fact_provenance, dict):
        return None
    bits = []
    for fact, info in fact_provenance.items():
        if isinstance(info, dict) and info.get("downgraded"):
            reason = info.get("downgrade_reason") or "unspecified"
            bits.append(f"{fact}:{reason}")
    return " | ".join(bits) if bits else None


def _synthesize_corrected_facts(fact_provenance: dict | None) -> str | None:
    """`{fact}:{value}` pipe-separated for human-corrected facts.

    Issue #249 — facts whose extracted value was replaced by an analyst
    correction before the rule cascade ran carry
    ``provenance="human_corrected"`` in the sidecar; this column makes
    the audit workbook show exactly which inputs a human touched (the
    rationale and prior value live in the curation sidecar).
    """
    if not isinstance(fact_provenance, dict):
        return None
    bits = []
    for fact, info in fact_provenance.items():
        if isinstance(info, dict) and info.get("provenance") == "human_corrected":
            bits.append(f"{fact}:{info.get('value')}")
    return " | ".join(bits) if bits else None


def _fact_provenance(c: RowContext) -> dict:
    return (c.score or {}).get("fact_provenance") or {}


def _score_dims(c: RowContext) -> dict:
    return (c.score or {}).get("dimensions") or {}


def _fact_col(fact: str) -> ColumnSpec:
    return ColumnSpec(
        f"fact_{fact}", fact,
        lambda c, _f=fact: _format_fact_value(_fact_provenance(c).get(_f)),
        role="ai",
        doc=f"Extracted fact `{fact}` (blank when null/unscored).",
    )


def _spans_col(header: str, fact: str) -> ColumnSpec:
    # Width 80 — verbatim LLM-evidence quotes need room to render
    # without forcing the reader to widen manually.
    return ColumnSpec(
        f"spans_{fact}", header,
        lambda c, _f=fact: _format_fact_spans(_fact_provenance(c).get(_f)),
        role="ai", width=80,
        doc=f"Validated evidence spans for `{fact}`, ' | '-joined.",
    )


def _audit_dim_col(dim: str) -> ColumnSpec:
    return ColumnSpec(
        f"dim_{dim}", dim,
        lambda c, _d=dim: (_score_dims(c).get(_d) or {}).get("value"),
        role="score",
        doc=f"{dim} dimension tier.",
    )


def _x_nachos_tier_rule(c: RowContext) -> CellValue:
    return (_score_dims(c).get("nachos_score") or {}).get("rule_matched")


def _x_nachos_adjustments(c: RowContext) -> CellValue:
    """Adjustments suffix from the justification (everything after the
    first ``"; "`` — plan §6 encodes it there). Empty when none."""
    justification = (c.score or {}).get("nachos_justification")
    if isinstance(justification, str) and "; " in justification:
        return justification.split("; ", 1)[1]
    return None


def _x_review_reasons(c: RowContext) -> CellValue:
    review = (c.score or {}).get("review") or {}
    return "; ".join(review.get("reasons", []) or []) or None


_AUDIT_NACHOS_AND_TAIL: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        "audit_nachos_tier_rule", "nachos_tier_rule", _x_nachos_tier_rule,
        role="score", legend="RULE_MEANINGS", width=28,
        doc="Which nachos_score cascade branch fired.",
    ),
    ColumnSpec(
        "audit_nachos_adjustments", "nachos_adjustments",
        _x_nachos_adjustments, role="score", legend="ADJUSTMENT_MEANINGS",
        width=36,
        doc="Adjustment labels applied on top of the base tier.",
    ),
    ColumnSpec(
        "audit_nachos_score", "nachos_score",
        lambda c: c.score_fields.nachos_tier,
        role="score", color_scale=(0, 3),
    ),
    ColumnSpec(
        "audit_adjusted_nachos_score", "adjusted_nachos_score",
        lambda c: (c.score or {}).get("adjusted_nachos_score"),
        role="score", color_scale=(0, 4.5),
    ),
    # v23 — complexity_score on both lenses (issue #106 / Q4).
    ColumnSpec(
        "audit_complexity_score", "complexity_score",
        lambda c: (c.score or {}).get("complexity_score"),
        role="score",
    ),
    ColumnSpec(
        "audit_confidence_composite", "confidence_composite",
        lambda c: (c.score or {}).get("confidence_composite"),
        role="ai", legend="CONFIDENCE_MEANINGS",
    ),
    ColumnSpec(
        "audit_rule_paths", "rule_paths",
        lambda c: _synthesize_rule_paths(_score_dims(c)),
        role="ai", width=60,
        doc="'{dim}:{rule}' pipe-joined across all dimensions.",
    ),
    ColumnSpec(
        "audit_downgraded_facts", "downgraded_facts",
        lambda c: _synthesize_downgraded_facts(_fact_provenance(c)),
        role="ai", width=40,
        doc="'{fact}:{reason}' for genuinely downgraded facts.",
    ),
    # Issue #249 — which extraction inputs a human corrected (the
    # overlay applied at aggregate time; rationale + prior value live
    # in the curation sidecar).
    ColumnSpec(
        "audit_human_corrected_facts", "human_corrected_facts",
        lambda c: _synthesize_corrected_facts(_fact_provenance(c)),
        role="ai", width=40,
        doc="'{fact}:{value}' for analyst-corrected facts (issue #249).",
    ),
    ColumnSpec(
        "audit_needs_review", "needs_review",
        lambda c: (
            ("Yes" if ((c.score or {}).get("review") or {}).get("needs_review") else "No")
            if c.score
            else None
        ),
        role="ai", flag_yes=True,
    ),
    ColumnSpec(
        "audit_review_route", "review_route",
        lambda c: ((c.score or {}).get("review") or {}).get("route"),
        role="ai",
    ),
    ColumnSpec(
        "audit_review_reasons", "review_reasons", _x_review_reasons,
        role="ai", width=60,
    ),
)


def audit_trail_columns(lens: str) -> tuple[ColumnSpec, ...]:
    """Audit Trail column list for one lens — composed, not enumerated.

    Prepends every Reviewer View column (same order, widths reset — the
    audit sheet is uniformly compact at the sheet default) with per-fact
    values, companion span columns, per-dim tiers (nachos_score stripped:
    it surfaces in the NACHOS block instead), the NACHOS methodology
    block, and the review tail.
    """
    facts = _SPINE_FACT_ORDER if lens == "spine" else _SOURCE_FACT_ORDER
    base_dims = _SPINE_DIM_ORDER if lens == "spine" else _SOURCE_DIM_ORDER
    dims = tuple(d for d in base_dims if d != "nachos_score")
    return (
        *(
            replace(c, width=None)
            for c in DETAILS_COLUMNS
            # The empty analyst-input band stays off the audit sheet —
            # eight always-blank columns there would be the #102
            # anti-pattern with zero audit value. `effective_score`
            # likewise (issue #248 Part C): the audit workbook is the
            # ENGINE's provenance surface and builds contexts without
            # adjudications — the consensus layer's audit trail is the
            # Update Log register + the curation sidecar history.
            # `review_why` / `review_priority` likewise (issue #259
            # PR 2): audit contexts carry no queue entries, and the
            # trail already has the raw cascade reasons + route.
            if lens in c.lens
            and c.role != "analyst_input"
            and c.key not in (
                "effective_score", "review_why", "review_priority",
            )
        ),
        *(_fact_col(f) for f in facts),
        _spans_col(_SEMANTIC_CLASS_SPANS_HEADER, "semantic_class"),
        _spans_col(_INTEGRATION_CLASS_SPANS_HEADER, "integration_class"),
        *(_audit_dim_col(d) for d in dims),
        *_AUDIT_NACHOS_AND_TAIL,
    )


AUDIT_TRAIL_SHEET = SheetSpec(
    title="Audit Trail",
    guide=(
        "Wide per-record audit — every fact, span, dimension tier, "
        "rule path, and downgrade flag on one row. The 'why this score' "
        "surface. Lives in its own on-demand workbook: "
        "`poc3 report audit --state {ST}` (Option D — no longer an "
        "analyst-workbook sheet).",
        "Engineers / methodology debug",
    ),
    finish=_SheetFinish(
        group="audit",
        freeze="G2",
        autofilter=True,
        # Scales/flags target the lowercase NACHOS audit block, NOT the
        # Reviewer View prefix columns (which stay unformatted here).
        color_scale=(
            ("nachos_score", 0, 3),
            ("adjusted_nachos_score", 0, 4.5),
        ),
        flag_yes=("needs_review",),
    ),
    columns_factory=audit_trail_columns,
    row_number_column=True,
    default_width=18,
)


# --- Dict-row sheet specs ---------------------------------------------------
#
# The remaining table sheets render plain per-row dicts (or tuples)
# produced by small builders in `analyst.py` (loaders / flatteners stay
# there; WHAT each column says lives here). Every column carries an
# explicit width transcribed from the retired imperative writers.


def _item(key: str, default: CellValue = None) -> Callable[[dict], CellValue]:
    def extract(row: dict) -> CellValue:
        return row.get(key, default)

    return extract


REFERENCE_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("domain", "Domain", _item("domain"), width=28),
    ColumnSpec("entity", "Entity", _item("entity"), width=36),
    ColumnSpec("reference", "Reference", _item("reference"), width=36),
    ColumnSpec("in_swagger", "In Ed-Fi Swagger?", _item("in_swagger"), width=18),
    ColumnSpec("observations", "Observations", _item("observations"), width=60),
)

REFERENCE_SHEET = SheetSpec(
    title="References",
    guide=(
        "FK / cross-entity reference inventory (distinct from the "
        "Details 'References' column, which carries source-doc "
        "pointers).",
        "Anyone investigating entity relationships",
    ),
    finish=_SheetFinish(group="audit", freeze="A2", autofilter=True),
    columns=REFERENCE_COLUMNS,
)

ENTITIES_BY_DOMAIN_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("domain", "Domain", _item("domain"), width=32),
    ColumnSpec("entity", "Entity", _item("entity"), width=40),
    ColumnSpec(
        "documented_elements", "Documented Elements",
        _item("documented_elements"), width=20,
        doc="Documented-element count for the entity (blank on subtotal rows).",
    ),
)

ENTITIES_BY_DOMAIN_SHEET = SheetSpec(
    title="Entities by Domain",
    guide=(
        "Ed-Fi Swagger/API model catalog grouped by Ed-Fi Data Standard "
        "domain, with documented-element subtotals per domain and a "
        "Grand Total.",
        "Anyone orienting on entity coverage",
    ),
    # No autofilter — subtotal rows inside a filter range mis-sort.
    finish=_SheetFinish(group="audit", freeze="A2"),
    columns=ENTITIES_BY_DOMAIN_COLUMNS,
)

REVIEW_QUEUE_COLUMNS: tuple[ColumnSpec, ...] = (
    # Row # here is a DATA column (`_item`), not the renderer-injected
    # address: the queue is a subset of Details, so each row carries the
    # Details address it points at (hyperlinked post-render by
    # `workbook_render.apply_details_row_links`). The never-a-spec-column
    # rule protects the Details-derived `ai-` backfill projection, which
    # this dict-row sheet never feeds.
    ColumnSpec(
        "row", "Row #", _item("row_number"), width=8,
        doc=(
            "This row's home-sheet address (see Where) — hyperlinked "
            "to the row."
        ),
    ),
    ColumnSpec(
        "where", "Where", _item("where"), width=20,
        doc=(
            "Which sheet the Row # link lands on: Details (documented "
            "rows — the pass's real work, the queue's top block), "
            "Documentation Gaps (the relocated documented=False rows), "
            "or (record gone) — a recorded decision whose record is on "
            "no rendered sheet (no link)."
        ),
    ),
    # Issue #259 PR 3 — read-only progress echo. The spec column renders
    # BLANK (extractor returns None); `workbook_render.apply_reviewed_echo`
    # fills it post-render with INDEX/MATCH formulas (the generic
    # renderer's `_set_cell` formula guard is deliberately bypassed
    # there). Plain role — NOT analyst_input: the green band fill would
    # say "write here", and writing stays Details-only (ingest never
    # scans this sheet regardless).
    ColumnSpec(
        "reviewed_echo", "Reviewed? (from Details)",
        lambda r: None,
        width=14,
        doc=(
            "Live echo of the Details `Reviewed?` mark (INDEX/MATCH on "
            "Row #, computed when the workbook opens) — read-only "
            "progress signal; write on Details, never here. Blank for "
            "rows without a Details `Reviewed?` entry."
        ),
    ),
    ColumnSpec(
        "route", "Route",
        lambda r: r.get("route") or "(unrouted)",
        width=14,
        doc=(
            "RE-ADJUDICATE (a stale team adjudication — the engine or "
            "plan version moved since the decision) sorts first, then "
            "OVERRIDE (analyst score disagrees with the engine — the "
            "§8.4 calibration signal; a fresh adjudication resolves its "
            "adjusted-axis row off the queue), then the POLICY / "
            "DATA_MODEL / SCORING / ANALYST priority ladder."
        ),
    ),
    ColumnSpec("state", "State", _item("state"), width=8),
    ColumnSpec("entity", "Entity Name", _item("entity"), width=36),
    ColumnSpec("element", "Data Element", _item("element_name"), width=30),
    ColumnSpec(
        "adjusted", "Adjusted NACHOS Score", _item("adjusted"),
        width=12,
        doc="The engine's adjusted score for the flagged row.",
    ),
    ColumnSpec(
        "confidence_composite", "confidence_composite",
        _item("confidence_composite"), width=18,
        legend="CONFIDENCE_MEANINGS",
    ),
    ColumnSpec(
        "why", "Why", _item("why"),
        width=80, wrap=True,
        doc=(
            "Route reasons ('; '-joined), the override disagreement "
            "sentence (analyst X vs engine Y), or the stale-adjudication "
            "sentence (adjudicated X … is stale — reason; re-adjudicate)."
        ),
    ),
    ColumnSpec(
        "evidence", "AI: Evidence", _item("evidence"),
        role="ai", width=60, wrap=True,
        doc=(
            "First validated span for the flagged row (same renderer as "
            "the Details column — `evidence_from_score`)."
        ),
    ),
)

REVIEW_QUEUE_SHEET = SheetSpec(
    title="Review Queue",
    guide=(
        "Open here — what needs a human. Rows group by where they live "
        "(the Where column): Details first — the score follow-ups that "
        "are the pass's real work — then Documentation Gaps, then "
        "records no longer in the workbook. Within each group, priority "
        "order: stale team adjudications (RE-ADJUDICATE) first, then "
        "analyst override disagreements (OVERRIDE — a row drops off "
        "once the team adjudicates it), then the rule cascade's routed "
        "rows. Row # links to the row on its home sheet; the tier-0 "
        "mass stays one filter away.",
        "Analysts",
    ),
    finish=_SheetFinish(
        group="work", freeze="A2", autofilter=True,
        number_formats=(("Adjusted NACHOS Score", "0.0"),),
    ),
    columns=REVIEW_QUEUE_COLUMNS,
)


def _gap_dims(row: dict) -> dict:
    return (row.get("score") or {}).get("dimensions") or {}


def _gap_sd_inputs(row: dict) -> dict:
    return (_gap_dims(row).get("structural_depth") or {}).get("inputs_used") or {}


def _gap_score_item(key: str) -> Callable[[dict], CellValue]:
    def extract(row: dict) -> CellValue:
        return (row.get("score") or {}).get(key)

    return extract


def _gap_meta_item(key: str) -> Callable[[dict], CellValue]:
    def extract(row: dict) -> CellValue:
        return (row.get("meta") or {}).get(key)

    return extract


def _gap_sd_input(key: str) -> Callable[[dict], CellValue]:
    def extract(row: dict) -> CellValue:
        return _gap_sd_inputs(row).get(key)

    return extract


def _gap_dim_value(dim: str) -> Callable[[dict], CellValue]:
    def extract(row: dict) -> CellValue:
        return (_gap_dims(row).get(dim) or {}).get("value")

    return extract


# One row per spine-anchored gap record (issue #73 Step 1). Structural
# inputs come from `dimensions["structural_depth"].inputs_used` so the
# row reflects exactly what the rule cascade consumed; absent fields
# render blank (tolerant of partial sidecars).
SPINE_GAP_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("state", "State", _item("state"), width=6),
    ColumnSpec("entity", "Entity", _gap_score_item("entity"), width=30),
    ColumnSpec("element", "Element", _gap_score_item("element_name"), width=32),
    ColumnSpec("discovery", "Discovery", _gap_meta_item("discovery"), width=32),
    ColumnSpec(
        "spine_data_type", "API Model Data Type",
        _gap_meta_item("spine_data_type"), width=18,
    ),
    ColumnSpec(
        "extension_schema", "Extension Schema",
        _gap_meta_item("spine_extension_name"), width=22,
    ),
    ColumnSpec(
        "structural_depth", "Implementation Shape",
        _gap_dim_value("structural_depth"), width=16,
        legend="STRUCTURAL_DEPTH_MEANINGS",
    ),
    ColumnSpec("fk_chain_depth", "FK Chain Depth", _gap_sd_input("fk_chain_depth"), width=14),
    ColumnSpec(
        "reference_fan_out", "Reference Fan-Out",
        _gap_sd_input("reference_fan_out"), width=16,
    ),
    ColumnSpec(
        "sub_collection_depth", "Sub-Collection Depth",
        _gap_sd_input("sub_collection_depth"), width=18,
    ),
    ColumnSpec(
        "descriptor_enum_breadth", "Descriptor Enum Breadth",
        _gap_sd_input("descriptor_enum_breadth"), width=20,
    ),
    ColumnSpec(
        "entity_extension_footprint", "Entity Extension Footprint",
        _gap_sd_input("entity_extension_footprint"), width=22,
    ),
    ColumnSpec(
        "documentation_style_tier", "Documentation Style Tier",
        _gap_dim_value("documentation_style_tier"), width=18,
        legend="DOC_STYLE_MEANINGS",
    ),
    ColumnSpec(
        "documentation_gap", "Documentation Gap",
        _gap_dim_value("documentation_gap"), width=16,
    ),
    ColumnSpec("nachos_tier", "NACHOS Tier", _gap_dim_value("nachos_score"), width=12),
    ColumnSpec(
        "adjusted_nachos", "Adjusted NACHOS",
        _gap_score_item("adjusted_nachos_score"), width=16,
    ),
    ColumnSpec(
        "nachos_justification", "NACHOS Justification",
        _gap_score_item("nachos_justification"), width=60,
        legend="RULE_MEANINGS",
    ),
    ColumnSpec(
        "confidence_composite", "Confidence Composite",
        _gap_score_item("confidence_composite"), width=14,
        legend="CONFIDENCE_MEANINGS",
    ),
    # Sidecar header carries `step` ("1" = deterministic pass); the
    # per-row column makes provenance visible without opening the JSON.
    ColumnSpec(
        "step", "Step",
        lambda r: "1"
        if (r.get("score") or {}).get("discovery_lens") == "spine_anchored"
        else None,
        width=6,
    ),
)

SPINE_GAP_SHEET = SheetSpec(
    title="API Model Gaps",
    guide=(
        "Canonical Ed-Fi Swagger/API model slots this state's source "
        "documentation is silent on, with implementation shape and the "
        "deterministic score shape. The coverage story behind blank "
        "cells. API-model-lens per-state workbooks only.",
        "Analysts (coverage)",
    ),
    finish=_SheetFinish(group="work", freeze="A2", autofilter=True),
    columns=SPINE_GAP_COLUMNS,
    lens=SPINE,
    scope=frozenset({"per_state"}),
)


# --- Documentation Gaps (Option D — the spine signal folded into the
# SOURCE workbook) ------------------------------------------------------------
#
# One sheet unifying the two undocumented populations the analysts kept
# meeting as confusing blanks:
#   * API-model gap slots (the gap sidecar — entity/element pairs the
#     source doc never mentions at all), and
#   * the swagger/swagger_leaf backfill rows RELOCATED out of Details
#     (documented=False; they used to be 60-80% of the Details grid).
# `Provenance` says which population a row belongs to. Reuses the
# SPINE_GAP_COLUMNS accessors verbatim (same {state, score, meta} row
# dicts; relocated rows synthesize their meta from the ElementRecord).

_PROVENANCE_COL = ColumnSpec(
    "provenance", "Provenance", _gap_meta_item("provenance"), width=34,
    doc=(
        "API model gap (not in source doc) / Swagger backfill "
        "(entity-level) / Swagger leaf borrow — which undocumented "
        "population this row belongs to."
    ),
)

DOCUMENTATION_GAPS_COLUMNS: tuple[ColumnSpec, ...] = (
    *SPINE_GAP_COLUMNS[:4],
    _PROVENANCE_COL,
    *SPINE_GAP_COLUMNS[4:],
)

DOCUMENTATION_GAPS_SHEET = SheetSpec(
    title="Documentation Gaps",
    guide=(
        "Everything the Ed-Fi Swagger/API model knows about that this "
        "state's source documentation does not: API-model gap slots "
        "plus the swagger-backfilled undocumented rows relocated out "
        "of Details (Details now shows documented rows only). The "
        "honest coverage story behind what used to be blank cells.",
        "Analysts (coverage)",
    ),
    finish=_SheetFinish(
        group="work",
        freeze="A2",
        autofilter=True,
        number_formats=(("Adjusted NACHOS", "0.0"),),
    ),
    columns=DOCUMENTATION_GAPS_COLUMNS,
    lens=SOURCE,
)


def _format_fact_value_for_sheet(value):
    """Render a fact value for the XLSX Fact Value cell.

    Booleans become "True"/"False" (openpyxl treats raw Python bools
    fine but reviewers prefer the textual form alongside string facts
    like ``semantic_class=narrows``). ``None`` stays ``None`` so the
    cell is blank.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "True" if value else "False"
    return value


def _rec_item(key: str) -> Callable[[dict], CellValue]:
    """Recommendations rows arrive as ``{"rec": <row>, "domain": …,
    "route": …}`` wrappers — the domain/route joins happen in analyst."""

    def extract(row: dict) -> CellValue:
        return (row.get("rec") or {}).get(key)

    return extract


def _rec_item_str(key: str) -> Callable[[dict], CellValue]:
    def extract(row: dict) -> CellValue:
        return (row.get("rec") or {}).get(key) or ""

    return extract


# Analyst-facing display names for internal dimension slugs — the
# Recommendations sheet previously leaked raw `structural_depth`-style
# identifiers into a rendered column (#174).
DIMENSION_DISPLAY: dict[str, str] = {
    "canonical_name_alignment": "Canonical Name Alignment",
    "definition_quality": "Definition Quality",
    "semantic_fidelity": "Semantic Fidelity",
    "extension_justification": "Extension Justification",
    "documentation_completeness": "Documentation Completeness",
    "obligation_clarity": "Obligation Clarity",
    "business_logic_complexity": "Complex Business Logic",
    "nachos_score": "NACHOS Score",
    "structural_depth": "Implementation Shape",
    "documentation_style_tier": "Documentation Style",
    "documentation_gap": "Documentation Gap",
}


def _x_rec_dimension(row: dict) -> CellValue:
    slug = (row.get("rec") or {}).get("dimension") or ""
    return DIMENSION_DISPLAY.get(slug, slug)


# Track C — one row per (record × below-target dimension); 15 columns
# (v10 — `In Scope` dropped). Same shape across states + lenses.
RECOMMENDATIONS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("state", "State", _rec_item_str("state"), width=8),
    ColumnSpec("domain", "Domain", _item("domain"), width=22),
    ColumnSpec("entity", "Entity", _rec_item_str("entity"), width=30),
    ColumnSpec("element", "Element", _rec_item_str("element"), width=28),
    ColumnSpec(
        "review_status", "Review Status", _rec_item_str("review_status"),
        width=14,
    ),
    ColumnSpec("review_route", "Review Route", _item("route"), width=14),
    ColumnSpec(
        "dimension", "Dimension", _x_rec_dimension, width=26,
        doc="Analyst-facing dimension name (raw slugs live on the audit workbook).",
    ),
    ColumnSpec("current_tier", "Current Tier", _rec_item("current_tier"), width=12),
    ColumnSpec("target_tier", "Target Tier", _rec_item("target_tier"), width=12),
    ColumnSpec("impact", "Impact", _rec_item("impact"), width=8),
    ColumnSpec(
        "recommendation", "Recommendation", _rec_item_str("recommendation"),
        width=80, wrap=True,
    ),
    ColumnSpec(
        "rationale", "Rationale", _rec_item_str("rationale"),
        width=60, wrap=True,
    ),
    ColumnSpec("evidence_fact", "Evidence Fact", _rec_item("evidence_fact"), width=26),
    ColumnSpec(
        "fact_value", "Fact Value",
        lambda r: _format_fact_value_for_sheet((r.get("rec") or {}).get("fact_value")),
        width=14,
    ),
    ColumnSpec("confidence", "Confidence", _rec_item_str("confidence"), width=12),
)

RECOMMENDATIONS_SHEET = SheetSpec(
    title="Recommendations",
    guide=(
        "Per-element edit suggestions, sorted by leverage. The action "
        "list — highest-impact row first within each (state, entity, "
        "element) group.",
        "Stakeholders + analysts",
    ),
    finish=_SheetFinish(group="work", freeze="A2", autofilter=True),
    columns=RECOMMENDATIONS_COLUMNS,
)

# Static editorial sheet — rows are (limitation, impact, why) tuples from
# `analyst._filter_known_limitations`; every cell wraps.
KNOWN_LIMITATIONS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("limitation", "Limitation", lambda t: t[0], width=42, wrap=True),
    ColumnSpec("impact", "Impact", lambda t: t[1], width=52, wrap=True),
    ColumnSpec("why", "Why it exists", lambda t: t[2], width=70, wrap=True),
)

KNOWN_LIMITATIONS_SHEET = SheetSpec(
    title="Methodology Notes",
    guide=(
        "Things we already know about and don't need flagged. Read "
        "this BEFORE the rest of the workbook to skip already-cataloged "
        "caveats.",
        "Everyone",
    ),
    finish=_SheetFinish(group="audit", freeze="A2", autofilter=True),
    columns=KNOWN_LIMITATIONS_COLUMNS,
)

PEER_GAPS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("slot", "Slot", _item("slot_key"), width=40),
    ColumnSpec("confidence", "Confidence", _item("confidence"), width=12),
    ColumnSpec(
        "states_present", "States Present",
        lambda r: "+".join(r.get("states_present") or []),
        width=16,
    ),
    ColumnSpec(
        "current_posture", "Current Posture", _item("current_posture"),
        width=18,
    ),
    ColumnSpec(
        "peer_consensus_format", "Peer Consensus Format",
        lambda r: r.get("peer_consensus_format") or None,
        width=80, wrap=True,
    ),
    ColumnSpec(
        "recommended_fill", "Recommended Fill",
        lambda r: r.get("recommended_fill") or None,
        width=80, wrap=True,
    ),
    ColumnSpec(
        "consensus_concept", "Consensus Concept",
        lambda r: r.get("consensus_concept") or None,
        width=80, wrap=True,
    ),
)

PEER_GAPS_SHEET = SheetSpec(
    title="Peer Gaps",
    guide=(
        "For each gap on this state, what each peer state does at the "
        "same slot. Per-state workbooks only.",
        "Stakeholders + analysts",
    ),
    finish=_SheetFinish(group="work", freeze="A2", autofilter=True),
    columns=PEER_GAPS_COLUMNS,
    scope=frozenset({"per_state"}),
)


# --- Extension Details (Option B — the analysts' extension grid) -----------

_EXT_DESIGN_LABELS: dict[str, str] = {
    "tier_3_necessary_standalone": "Standalone",
    "tier_2_necessary_companion": "Companion",
    "tier_0_unnecessary_mirror": "Mirrors core pattern",
}


def _x_necessity(c: RowContext) -> CellValue:
    """Positive framing of the Details `Unnecessary Extension ?` cell —
    same source of truth (the justification adjustment label), so the
    two sheets can never disagree."""
    if not c.is_extension:
        return None
    verdict = c.legacy.unnecessary_ext
    if verdict == "Yes":
        return "Unnecessary"
    if verdict == "No":
        return "Necessary"
    return "Unresolved"


def _x_extension_design(c: RowContext) -> CellValue:
    rule = (
        ((c.score or {}).get("dimensions") or {}).get("extension_justification")
        or {}
    ).get("rule_matched")
    if not rule:
        return None
    return _EXT_DESIGN_LABELS.get(rule, "Uncertain")


def _x_necessity_evidence(c: RowContext) -> CellValue:
    fp = (c.score or {}).get("fact_provenance") or {}
    return _format_fact_spans(fp.get("extension_is_necessary"))


_DETAILS_BY_KEY = {c.key: c for c in DETAILS_COLUMNS}

EXTENSION_DETAILS_COLUMNS: tuple[ColumnSpec, ...] = (
    _DETAILS_BY_KEY["state"],
    _DETAILS_BY_KEY["source_area"],
    _DETAILS_BY_KEY["entity"],
    _DETAILS_BY_KEY["edfi_domain"],
    _DETAILS_BY_KEY["element"],
    _DETAILS_BY_KEY["data_type"],
    _DETAILS_BY_KEY["contributing_extension"],
    _DETAILS_BY_KEY["adjusted_nachos_score"],
    _DETAILS_BY_KEY["base_nachos_score"],
    ColumnSpec(
        "necessity", "Necessity", _x_necessity,
        role="score", width=14,
        doc="Necessary / Unnecessary / Unresolved (same source of truth "
            "as the Details 'Unnecessary Extension ?' column).",
    ),
    _DETAILS_BY_KEY["reason_for_extension_necessity"],
    ColumnSpec(
        "design", "Design", _x_extension_design,
        role="score", width=20,
        doc="Standalone / Companion / Mirrors core pattern / Uncertain "
            "(from the extension_justification rule path).",
    ),
    ColumnSpec(
        "necessity_evidence", "AI: Evidence", _x_necessity_evidence,
        role="ai", width=80, wrap=True,
        doc="Validated extension_is_necessary evidence spans.",
    ),
    _DETAILS_BY_KEY["confidence"],
    _DETAILS_BY_KEY["needs_review"],
)

EXTENSION_DETAILS_SHEET = SheetSpec(
    title="Extension Details",
    guide=(
        "One row per extension element: necessity verdict + reason + "
        "design shape, with the extension's scores. Same `Row #` as "
        "Details.",
        "Analysts (extension remediation)",
    ),
    finish=_SheetFinish(
        group="work",
        freeze="A2",
        autofilter=True,
        number_formats=(
            ("Adjusted NACHOS Score", "0.0"),
            ("Base NACHOS Score", "0"),
        ),
        color_scale=(
            ("Adjusted NACHOS Score", 0, 4.5),
            ("Base NACHOS Score", 0, 3),
        ),
        flag_yes=("AI: Needs Review",),
    ),
    columns=EXTENSION_DETAILS_COLUMNS,
    row_number_column=True,
)


# --- Commitment Tracker (Option C — the analysts' remediation ledger) -------
#
# Dict-row sheet: one row per element carrying a cost-axis
# (`nachos_score`) recommendation, grouped by entity with bold subtotal
# rows and a Grand Total what-if. "NACHOS Points Resolved" v1 is the
# HONEST deterministic claim: adopting the recommendation removes the
# row's base-tier points (`points = current_tier`,
# `projected = adjusted − points`, floor 0) — score ADJUSTMENTS (e.g.
# `+0.5 necessary_ext`) deliberately remain, and the Reason cell says so
# on every row. Quality-axis recommendations are listed in "Other
# recommendations" and contribute 0 points.

COMMITMENT_TRACKER_COLUMNS: tuple[ColumnSpec, ...] = (
    # Row # is a DATA column (subset sheet) — see the Review Queue note.
    ColumnSpec(
        "row", "Row #", _item("row_number"), width=8,
        doc="This row's Details-sheet address — hyperlinked to the row.",
    ),
    ColumnSpec("state", "State", _item("state"), width=8),
    ColumnSpec("entity", "Entity Name", _item("entity"), width=36),
    ColumnSpec("element", "Data Element", _item("element"), width=30),
    ColumnSpec(
        "adjusted", "Adjusted NACHOS Score", _item("adjusted"), width=12,
        doc="The engine's adjusted score today.",
    ),
    ColumnSpec(
        "action", "Recommended Action", _item("action"), width=70,
        wrap=True,
        doc="The cost-axis recommendation message for this element.",
    ),
    ColumnSpec(
        "points", "NACHOS Points Resolved", _item("points"), width=12,
        doc="Base-tier points removed if the recommendation is adopted "
            "(v1 = the row's base tier; adjustments remain — see Reason).",
    ),
    ColumnSpec(
        "projected", "Projected Adjusted NACHOS Score", _item("projected"),
        width=14,
        doc="adjusted − points, floor 0 (adjustments deliberately kept).",
    ),
    ColumnSpec(
        "reason", "Reason", _item("reason"), width=55, wrap=True,
        doc="What the points claim covers — and what it deliberately "
            "does not (remaining adjustments).",
    ),
    ColumnSpec(
        "other", "Other recommendations", _item("other"), width=44,
        wrap=True,
        doc="Quality-axis recommendations on the same row (0 points).",
    ),
    # Analyst-owned commitment columns — same round-trip mechanism as
    # the Details band (`poc3 review ingest` reads this sheet too).
    ColumnSpec(
        "adoption_timeline", "Adoption Timeline",
        _item("adoption_timeline"), role="analyst_input", width=20,
        doc="Analyst-input: the state's committed adoption window "
            "(round-trips via `poc3 review ingest`).",
    ),
    ColumnSpec(
        "commitment_status", "Commitment Status",
        _item("commitment_status"), role="analyst_input", width=18,
        doc="Analyst-input: negotiation status (round-trips).",
    ),
    ColumnSpec(
        "commitment_comments", "Comments",
        _item("commitment_comments"), role="analyst_input", width=50,
        wrap=True,
        doc="Analyst-input: free-form commitment notes (round-trips).",
    ),
)

COMMITMENT_TRACKER_SHEET = SheetSpec(
    title="Commitment Tracker",
    guide=(
        "Remediation ledger: every element with a cost-axis "
        "recommendation, its NACHOS Points Resolved if adopted (v1 = "
        "base-tier points only; adjustments remain — Reason says so), "
        "per-entity subtotals, and a Grand Total what-if over the Score "
        "Card population. Green columns are yours — they round-trip via "
        "`poc3 review ingest`.",
        "Analysts (state negotiation)",
    ),
    # No autofilter — subtotal rows inside a filter range mis-sort
    # (Entities-by-Domain precedent).
    finish=_SheetFinish(
        group="work",
        freeze="A2",
        number_formats=(
            ("Adjusted NACHOS Score", "0.0"),
            ("Projected Adjusted NACHOS Score", "0.0"),
            ("NACHOS Points Resolved", "0"),
        ),
    ),
    columns=COMMITMENT_TRACKER_COLUMNS,
    scope=frozenset({"per_state"}),
)


UPDATE_LOG_SHEET = SheetSpec(
    title="Update Log",
    guide=(
        "What generated this workbook (inputs, coverage, scope, "
        "freshness stamps), the methodology version history — one "
        "line per consumer-relevant change — and, when any exist, the "
        "adjudication register (team-consensus score decisions with "
        "who agreed, when, and why) and the fact-corrections register "
        "(human-corrected extraction inputs with author, rationale, "
        "and whether the re-score has landed).",
        "Everyone",
    ),
    finish=_SheetFinish(group="orient"),
    kind="block",
)


# --- Stakeholder terminology (issue #174) -----------------------------------
#
# Central internal→external mapping. Rendered labels use the CURRENT
# terms; code identifiers, JSON sidecar fields, artifact filenames, and
# the `--lens source|spine` CLI values keep the internal names (renaming
# join keys and Drive history is churn without consumer benefit). The
# Legend's generated "Terminology — former names" section and the
# Methodology Notes row are built from this tuple, so pre-2026-07
# artifacts stay traceable — the #174 acceptance criterion.

TERMINOLOGY_FORMER_NAMES: tuple[tuple[str, str, str], ...] = (
    (
        "NACHOS Score Context",
        "Integration Profile",
        "Methodology docs; never a workbook column header.",
    ),
    (
        "Implementation Shape",
        "Structural Depth",
        "Workbook column (pre-2026-07); the sidecar field stays "
        "`structural_depth`.",
    ),
    (
        "Ed-Fi Swagger/API model",
        "Ed-Fi spine",
        "Artifact filenames (`*_spine.json`, `*_analyst_spine.xlsx`) and "
        "code identifiers keep `spine`.",
    ),
    (
        "API-model lens",
        "spine lens",
        "CLI flag value stays `--lens spine`.",
    ),
)


# --- Block sheets + the unified registry ------------------------------------
#
# Block sheets (Readme / Legend / Score Card) have no columnar spec —
# they render via dedicated writers — but they live in the SAME registry
# so guide + finish have exactly one home per sheet and the KeyError
# guard covers every tab.

README_SHEET = SheetSpec(
    title="Readme",
    guide=("This guide.", "Everyone"),
    finish=_SheetFinish(group="orient"),
    kind="block",
)

LEGEND_SHEET = SheetSpec(
    title="Legend",
    guide=(
        "Glossary of every scoring token in the workbook — NACHOS tiers, "
        "adjustment labels, quality-dimension tiers, Match Status, "
        "Documentation Source, confidence, review routes. Generated from "
        "the scoring constants, so it can't drift.",
        "Everyone",
    ),
    finish=_SheetFinish(group="orient"),
    kind="block",
)

SCORE_CARD_SHEET = SheetSpec(
    title="Score Card",
    guide=(
        "State metadata, coverage %, NACHOS-tier rollup, the "
        "override-clustering diagnostic (analyst-override disagreements "
        "grouped by adjustment label / base rule), the fact-corrections "
        "table (human-corrected extraction inputs clustered by fact — "
        "a pattern there is a prompt weakness), and the structural × "
        "documentation 2×2 heatmap. The orientation surface — open this "
        "first.",
        "Everyone",
    ),
    finish=_SheetFinish(group="orient"),
    kind="block",
)

WORKBOOK_SHEETS: tuple[SheetSpec, ...] = (
    README_SHEET,
    UPDATE_LOG_SHEET,
    LEGEND_SHEET,
    SCORE_CARD_SHEET,
    EXTENSION_DETAILS_SHEET,
    COMMITMENT_TRACKER_SHEET,
    RECOMMENDATIONS_SHEET,
    DETAILS_SHEET,
    SCORING_SUMMARY_SHEET,
    REVIEW_QUEUE_SHEET,
    PEER_GAPS_SHEET,
    AUDIT_TRAIL_SHEET,
    SPINE_GAP_SHEET,
    DOCUMENTATION_GAPS_SHEET,
    REFERENCE_SHEET,
    ENTITIES_BY_DOMAIN_SHEET,
    KNOWN_LIMITATIONS_SHEET,
)

# Title-keyed registry. `workbook_render._finish_for` and
# `analyst._sheet_guide_entry` resolve through this dict — an
# unregistered sheet title raises KeyError at build time, so a new sheet
# cannot ship without a guide + finish (the Sequence-1 non-drift guard,
# now with a single home per sheet).
SHEET_SPECS: dict[str, SheetSpec] = {s.title: s for s in WORKBOOK_SHEETS}
