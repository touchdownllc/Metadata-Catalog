"""Issue #136 — back-fill human-scored workbooks with POC-3 source-lens scoring.

Single engine, multiple inputs. For each configured human-scored input
workbook this module:

1. Reads the input sheet preserving every original column verbatim.
2. Resolves each row's ``(state, entity, element)`` against POC-3's
   per-state source-lens sidecars via the Phase E reviewer keymap.
3. Appends the source-lens Reviewer-View columns — prefixed ``ai-`` so
   they read as additions next to the human-authored cells — onto each
   row. Unmatched and out-of-scope rows preserve origin cells with
   blank ``ai-`` cells.
4. Writes a ``Readme`` sheet stamping ``SCORING_PLAN_VERSION``, lens,
   per-state match rate, and out-of-scope counts.

Five predefined configs — one per state workbook in the per-state
human-scored basis (2026-07-07; ``review_loader.REVIEWER_SOURCES`` is
the single source of truth for filenames, sheets, and column headers —
``CONFIGS`` is DERIVED from it, so the digest pipeline and this overlay
engine can never drift apart on how a workbook is read). All dispatched
through the single ``poc3 report human-overlay --config <name>`` CLI
command: ``arizona`` / ``wisconsin`` / ``minnesota`` / ``texas`` /
``indiana``, each writing ``data/out/{name}_with_poc3_scores.xlsx``.
The former ``training-20pct`` / ``training-file`` / ``nachos-arizona``
configs (the single-training-file era) retired with the basis change.

All deliverables go to ``data/out/`` and are gitignored — attach
manually to the relevant email/Slack thread.

CRITICAL: module-level ``run()`` is plain — Click wrapper lives in
``src/poc3/cli.py`` (CLAUDE.md operational gotcha).

Phase E framing — back-fill is one-way. POC-3 cells are written into a
copy of the human input; no human signal flows back into rules /
prompts / sidecars (``feedback_human_scores_not_gt.md``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from src.models.element import ElementRecord, StateElements
from src.models.spine import StateSpine
from src.states import SUPPORTED_STATES
from src.report.analyst import (
    StateInputs,
    _edfi_domain_for,
)
from src.report.loaders import record_key as _record_key
from src.report.workbook_spec import (
    RowContext,
    _is_extension_record,
    backfill_columns,
)
from src.score.aggregate import SCORING_PLAN_VERSION
from src.score.aggregate_gap import synthesize_record as _synthesize_gap_record
from src.score.review_comparison import (
    _gap_lookup_resolve,
    load_gap_lookup,
    load_gap_scores,
)
from src.score.review_keymap import (
    SpineIndex,
    build_poc3_lookup,
    reviewer_key_to_poc3_key,
)
from src.score.review_loader import (
    HUMAN_SCORED_DIR,
    REVIEWER_SOURCES,
    ReviewerSource,
    resolve_columns,
)
from src.utils.paths import out_dir, spine_dir

_LOGGER = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# AI-prefixed POC-3 columns appended to every input row. Prefix flags the
# block as algorithm-generated so reviewers can scan human-authored vs.
# AI-back-filled signal at a glance. Column order + count inherits
# `analyst._DETAILS_HEADERS` — the role-filtered Details projection
# (Option B): the analyst-input band and the render-time `Row #` never
# enter the tuple, and the far-right machine columns that already carry
# an `AI: ` display prefix keep their historical bare `ai-` names
# (`AI: Needs Review` → `ai-Needs Review`) instead of double-prefixing.
_AI_PREFIX = "ai-"
_POC3_HEADERS: tuple[str, ...] = tuple(
    f"{_AI_PREFIX}{c.header.removeprefix('AI: ')}"
    for c in backfill_columns("source")
)


def _ai_cells(
    record: ElementRecord,
    state: str,
    is_ext: bool,
    edfi_domain: str | None,
    score: dict | None = None,
) -> list:
    """One origin row's ``ai-`` cell block, straight from the spec
    (issue #213 item 1 — previously routed through the
    ``analyst._details_row`` compat adapter, which existed only for
    this call)."""
    ctx = RowContext(
        state=state,
        record=record,
        is_extension=is_ext,
        edfi_domain=edfi_domain,
        score=score,
    )
    return [c.extract(ctx) for c in backfill_columns("source")]

# Filterable bucket label inserted between the human-authored origin
# block and the ``ai-`` block on workbooks where the input carries human
# tier + adjusted scores. Values mirror ``_CLASSIFICATION_ORDER`` so a
# reviewer can cross-reference the data sheet against the ai-summary
# classification block by exact bucket name.
_MATCH_STATUS_HEADER = "Match Status"

_HEADER_FONT = Font(bold=True)
_HEADER_FILL = PatternFill(
    start_color="FFDDDDDD", end_color="FFDDDDDD", fill_type="solid"
)

# Maps the reviewer file's full state names to codes for the join. AZ is
# included because the NACHOS Arizona workbook keys with full state names
# too. Indiana was added 2026-06-08 (June review follow-up) so IN reviewer
# rows join their source-lens sidecar instead of falling to ``out_of_scope``
# — IN's reviewer keymap (issue #162 walkers) and source/spine sidecars are
# all current. Kept as this module's own map so the digest pipeline's
# scope is unaffected (`score.review_loader` keys its REVIEWER_SOURCES
# registry per-file rather than via a shared name→code map). Nebraska
# stays out of scope: it is human-scored only, never AI-scored (no
# ``ne_*`` sidecars), so there is nothing to join.
# `tests/test_state_roster.py` pins this map's values to the canonical
# roster so a sixth state can't be silently missing here.
_STATE_NORMALIZE: dict[str, str] = {
    "Arizona": "AZ",
    "Wisconsin": "WI",
    "Minnesota": "MN",
    "Texas": "TX",
    "Indiana": "IN",
}

_IN_SCOPE_STATES: tuple[str, ...] = SUPPORTED_STATES


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackfillConfig:
    """One human-scored input + how to read its keys + where to write."""

    name: str
    """Short label (e.g. ``arizona``) used in logs + CLI."""

    input_path: Path
    """Absolute path to the input xlsx."""

    sheet_name: str
    """Sheet to read inside the input workbook."""

    output_path: Path
    """Absolute path for the back-filled output xlsx."""

    state_col: int
    """0-indexed column carrying the State value (full state name)."""

    entity_col: int
    """0-indexed column carrying the Entity Name."""

    element_col: int
    """0-indexed column carrying the Data Element name."""

    fixed_state: str | None = None
    """Override every row's state code (e.g. ``"AZ"`` for the AZ workbook).

    When set, the State column read still happens (used for the Readme
    audit), but resolution always targets ``fixed_state``. Useful for
    single-state workbooks whose State cell carries the long name and
    we want to short-circuit the normalize map.
    """

    human_nachos_col: int | None = None
    """0-indexed column carrying the human NACHOS tier (0–3).

    When set together with ``human_adj_col``, ``build_workbook`` adds an
    ``ai-summary`` sheet comparing human and AI scores per row.
    """

    human_adj_col: int | None = None
    """0-indexed column carrying the human adjusted NACHOS score (0–4.5)."""

    summary_exclude_states: tuple[str, ...] = ()
    """States to exclude from an additional ai-summary section.

    When non-empty, the ai-summary tab renders the standard "All states"
    block AND an extra "Excluding {states}" block computed from the same
    rows minus the listed state codes. (Historically used on the
    single-training-file basis to surface an MN-excluded view; unset on
    the per-state configs — each file is single-state.)
    """

    header_map: Mapping[str, str | None] | None = None
    """Role → normalized expected header (``review_loader`` vocabulary).

    When set, ``_resolve_config`` locates the int columns by header
    NAME via ``review_loader.resolve_columns`` (fail-loud on a renamed
    or duplicated header) and the int fields above are placeholders.
    When None, the int fields are used as-is (synthetic test configs +
    ``_detect_config`` autodetection).
    """


def config_from_reviewer_source(source: ReviewerSource) -> BackfillConfig:
    """Derive the overlay config for one per-state reviewer workbook.

    ``review_loader.REVIEWER_SOURCES`` owns filename / sheet / header
    truth; this factory only adds the overlay-specific bits (output
    path, fixed-state resolution). Column ints are placeholders —
    ``_resolve_config`` materializes them from ``header_map`` against
    the live header row at read time.
    """
    return BackfillConfig(
        name=source.key,
        input_path=HUMAN_SCORED_DIR / source.filename,
        sheet_name=source.sheet_name,
        output_path=out_dir() / f"{source.key}_with_poc3_scores.xlsx",
        state_col=-1,
        entity_col=-1,
        element_col=-1,
        fixed_state=source.state,
        header_map={
            role: source.headers.get(role)
            for role in ("state", "entity", "element", "nachos", "adjusted")
        },
    )


# Public registry the CLI dispatches against — derived from the
# per-state reviewer-source registry (2026-07-07 basis change) so the
# digest pipeline and the overlay engine read each workbook identically.
CONFIGS: dict[str, BackfillConfig] = {
    source.key: config_from_reviewer_source(source)
    for source in REVIEWER_SOURCES
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_cell(ws: Worksheet, row: int, col: int, value: Any):
    """Write ``value``, forcing text data-type when it starts with ``=``.

    Without this guard openpyxl writes strings like WI's
    ``"=== Element-specific rules ..."`` as formulas (leading ``=`` is
    the formula trigger). Excel then flags the workbook as corrupt:
    "We found a problem with some content / Removed Records: Formula
    from /xl/worksheets/sheetN.xml". Mirrors ``analyst._set_cell``.
    """
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"
    return cell


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return str(value).strip() or None


def _maybe_int(value: Any) -> int | None:
    """Coerce a numeric-or-string cell to int, None on blank/non-numeric."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class _OriginRow:
    """One row from the input — full original cells + key-column lookups."""

    row_idx: int
    cells: tuple[Any, ...]      # Verbatim row values, in column order.
    state_raw: str | None       # Resolved from ``cells[config.state_col]``.
    entity: str | None          # ``cells[config.entity_col]`` trimmed.
    element: str | None         # ``cells[config.element_col]`` trimmed.
    human_nachos: int | None = None   # Parsed from ``human_nachos_col`` when set.
    human_adj: float | None = None    # Parsed from ``human_adj_col`` when set.


@dataclass
class _StateContext:
    """Loaded source-lens artifacts for one state, keyed for fast lookup."""

    inputs: StateInputs
    scores_by_key: dict[str, dict]
    record_by_key: dict[str, ElementRecord]
    lookup: dict[tuple[str, str], str]
    spine_index: SpineIndex | None
    domain_cache: dict[str, str | None]
    # Issue #166 (follow-up §5) — spine-anchored gap artifact + its
    # deterministic scores. When a reviewer (entity, element) pair the
    # state's source doc was silent on resolves against the gap artifact,
    # the back-fill surfaces the gap row's score instead of leaving the
    # ai- block blank. Mirrors ``review_comparison``'s ``gap_row_match``
    # path so the deliverable workbook and the reviewer-comparison digest
    # agree on what counts as "the AI produced a score". The existence
    # comes from the swagger spine (gap surfacer at ingest); the score
    # comes from the normal deterministic gap pipeline (``aggregate-gap``)
    # — no reviewer/GT values are read.
    gap_lookup: dict[tuple[str, str], dict]
    gap_scores: dict[str, dict]
    edfi_version: str


@dataclass
class _ResolvedRow:
    """An origin row joined against POC-3 (or blank when out-of-scope/unmatched)."""

    origin: _OriginRow
    state_code: str | None  # Normalized POC-3 code or None.
    in_scope: bool          # state_code in _IN_SCOPE_STATES (the roster).
    matched: bool           # Resolved to a sidecar row (source-lens OR gap).
    poc3_cells: list[Any]   # Length == len(_POC3_HEADERS); blank when unmatched.
    record_key: str | None = None  # Sidecar key when matched (issue #147 leaf-recovery accounting).
    via_gap: bool = False   # Resolved against the spine-anchored gap artifact, not the source sidecar.


# ---------------------------------------------------------------------------
# Origin reader
# ---------------------------------------------------------------------------


def _resolve_config(config: BackfillConfig) -> BackfillConfig:
    """Materialize header-mapped column ints against the live header row.

    A no-op when ``config.header_map`` is None (int-column configs).
    Otherwise reads row 1 of the configured sheet and resolves each
    declared role by normalized header name (fail-loud on rename /
    duplicate via ``review_loader.resolve_columns``), returning a copy
    whose int fields are real. Idempotent — safe to call at every
    entry point.
    """
    if config.header_map is None:
        return config
    from dataclasses import replace as _dc_replace

    if not config.input_path.exists():
        raise FileNotFoundError(f"input file not found: {config.input_path}")
    wb = load_workbook(config.input_path, read_only=True, data_only=True)
    try:
        if config.sheet_name not in wb.sheetnames:
            raise KeyError(
                f"sheet {config.sheet_name!r} not found in "
                f"{config.input_path.name}; available: {wb.sheetnames}"
            )
        header_row = next(
            wb[config.sheet_name].iter_rows(min_row=1, max_row=1, values_only=True),
            (),
        )
    finally:
        wb.close()
    cols = resolve_columns(
        header_row,
        config.header_map,
        context=f"{config.input_path.name} [{config.sheet_name}]",
    )
    return _dc_replace(
        config,
        state_col=cols.get("state", -1),
        entity_col=cols["entity"],
        element_col=cols["element"],
        human_nachos_col=cols.get("nachos"),
        human_adj_col=cols.get("adjusted"),
    )


def _load_origin(
    config: BackfillConfig,
) -> tuple[list[Any], list[_OriginRow]]:
    """Read the configured sheet, returning ``(headers, rows)`` in input order.

    ``headers`` is the verbatim row-1 values. ``rows`` is one entry per
    non-blank data row, with ``cells`` carrying the full original tuple
    so the writer can replay it without lossy round-tripping.
    """
    config = _resolve_config(config)
    if not config.input_path.exists():
        raise FileNotFoundError(f"input file not found: {config.input_path}")
    wb = load_workbook(config.input_path, read_only=True, data_only=True)
    try:
        if config.sheet_name not in wb.sheetnames:
            raise KeyError(
                f"sheet {config.sheet_name!r} not found in {config.input_path.name}; "
                f"available: {wb.sheetnames}"
            )
        ws = wb[config.sheet_name]
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        headers = list(header_row)
        rows: list[_OriginRow] = []
        for idx, raw in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if all(v in (None, "") for v in raw):
                continue
            row_cells = tuple(raw)
            state_raw = _read_col(row_cells, config.state_col)
            entity = _read_col(row_cells, config.entity_col)
            element = _read_col(row_cells, config.element_col)
            human_nachos = (
                _maybe_int(_read_col(row_cells, config.human_nachos_col))
                if config.human_nachos_col is not None
                else None
            )
            human_adj = (
                _maybe_float(_read_col(row_cells, config.human_adj_col))
                if config.human_adj_col is not None
                else None
            )
            rows.append(
                _OriginRow(
                    row_idx=idx,
                    cells=row_cells,
                    state_raw=_maybe_str(state_raw),
                    entity=_maybe_str(entity),
                    element=_maybe_str(element),
                    human_nachos=human_nachos,
                    human_adj=human_adj,
                )
            )
        return headers, rows
    finally:
        wb.close()


def _read_col(cells: tuple[Any, ...], idx: int) -> Any:
    if 0 <= idx < len(cells):
        return cells[idx]
    return None


# ---------------------------------------------------------------------------
# State context loader
# ---------------------------------------------------------------------------


def _load_state_context(
    state: str,
    *,
    sidecar_dir: Path,
    spine_dir_path: Path,
) -> _StateContext | None:
    """Load source-lens elements + sidecar + spine for one state."""
    state_l = state.lower()
    sidecar_path = sidecar_dir / f"{state_l}_scores_source.json"
    elements_path = sidecar_dir / f"{state_l}_elements_source.json"
    spine_path = spine_dir_path / f"{state_l}_spine.json"
    if not sidecar_path.exists() or not elements_path.exists() or not spine_path.exists():
        _LOGGER.warning(
            "missing source-lens artifacts for %s (sidecar/elements/spine); "
            "rows will not back-fill",
            state,
        )
        return None
    elements = StateElements.model_validate_json(
        elements_path.read_text(encoding="utf-8")
    )
    spine = StateSpine.model_validate_json(spine_path.read_text(encoding="utf-8"))
    inputs = StateInputs(state=state, elements=elements, spine=spine)

    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    scores_list = payload.get("scores", [])
    scores_by_key = {
        e["record_key"]: e for e in scores_list if e.get("record_key")
    }
    record_by_key = {
        _record_key(state, r): r for r in elements.elements
    }
    lookup = build_poc3_lookup(scores_list, state=state)
    spine_index = SpineIndex.from_catalog(
        json.loads(spine_path.read_text(encoding="utf-8")).get("catalog", {})
    )

    domain_cache: dict[str, str | None] = {}
    for ename in inputs.spine.catalog.entities:
        domain_cache[ename] = _edfi_domain_for(inputs, ename)
    for r in elements.elements:
        if r.entity not in domain_cache:
            domain_cache[r.entity] = _edfi_domain_for(inputs, r.entity)

    # Spine-anchored gap artifact + its deterministic scores (issue #166).
    # Both degrade to ``{}`` when the artifact is absent, so the back-fill
    # falls back to the pre-gap behavior (blank ai- cells) without error.
    gap_lookup = load_gap_lookup(state, gap_dir=sidecar_dir)
    gap_scores = load_gap_scores(state, sidecar_dir=sidecar_dir)

    return _StateContext(
        inputs=inputs,
        scores_by_key=scores_by_key,
        record_by_key=record_by_key,
        lookup=lookup,
        spine_index=spine_index,
        domain_cache=domain_cache,
        gap_lookup=gap_lookup,
        gap_scores=gap_scores,
        edfi_version=spine.edfi_version,
    )


# ---------------------------------------------------------------------------
# Per-row resolver
# ---------------------------------------------------------------------------


def _resolve_row(
    origin: _OriginRow,
    contexts: dict[str, _StateContext],
    *,
    fixed_state: str | None = None,
) -> _ResolvedRow:
    """Project one origin row to its ai-cells (`len(_POC3_HEADERS)`),
    blank when unmatched."""
    blank = [None] * len(_POC3_HEADERS)
    if fixed_state is not None:
        state_code: str | None = fixed_state
    elif origin.state_raw is None:
        return _ResolvedRow(origin, None, False, False, blank)
    else:
        state_code = _STATE_NORMALIZE.get(origin.state_raw)
    if state_code is None or state_code not in _IN_SCOPE_STATES:
        return _ResolvedRow(origin, state_code, False, False, blank)
    ctx = contexts.get(state_code)
    if ctx is None or origin.entity is None or origin.element is None:
        return _ResolvedRow(origin, state_code, True, False, blank)
    record_key = reviewer_key_to_poc3_key(
        origin.entity, origin.element, ctx.lookup, spine=ctx.spine_index
    )
    if record_key is None:
        # Source-lens miss — try the spine-anchored gap artifact before
        # giving up. A reviewer (entity, element) the state's source doc
        # was silent on but the swagger spine carries surfaces here with a
        # deterministic gap score, mirroring ``review_comparison``'s
        # ``gap_row_match`` recovery (issue #166).
        return _resolve_via_gap(origin, state_code, ctx)
    record = ctx.record_by_key.get(record_key)
    if record is None:
        return _ResolvedRow(origin, state_code, True, False, blank)
    score = ctx.scores_by_key.get(record_key)
    is_ext = _is_extension_record(record)
    ed_domain = ctx.domain_cache.get(record.entity)
    poc3_cells = _ai_cells(record, state_code, is_ext, ed_domain, score)
    return _ResolvedRow(origin, state_code, True, True, poc3_cells, record_key)


def _resolve_via_gap(
    origin: _OriginRow,
    state_code: str,
    ctx: _StateContext,
) -> _ResolvedRow:
    """Resolve a source-lens miss against the spine-anchored gap artifact.

    Mirrors the ``gap_row_match`` branch of
    ``review_comparison.compare_state``: the reviewer (entity, element)
    pair resolves against ``{state}_elements_gap.json`` (existence sourced
    from the swagger spine at ingest), and the deterministic gap score from
    ``{state}_scores_gap.json`` populates the ai- block. The synthesized
    ElementRecord carries no reviewer/GT values — only spine-derived
    metadata (entity, element, data type, extension attribution) — so the
    no-GT discipline holds. Returns a blank-celled row (unchanged
    ``human_only`` behavior) when the gap artifact doesn't resolve the pair
    or carries no scored entry for it.
    """
    blank = [None] * len(_POC3_HEADERS)
    gap_hit = _gap_lookup_resolve(origin.entity, origin.element, ctx.gap_lookup)
    if gap_hit is None:
        return _ResolvedRow(origin, state_code, True, False, blank)
    canonical_key = (
        f"{state_code}|{gap_hit.get('entity')}|{gap_hit.get('element_name')}"
    )
    gap_score = ctx.gap_scores.get(canonical_key)
    if gap_score is None:
        # Gap artifact recognizes the pair but the deterministic gap
        # sidecar has no scored entry (Layer-2 audit-only shape). Keep the
        # ai- cells blank — surfacing an entity/element with no score would
        # misrepresent coverage. Re-run ``score aggregate-gap`` to populate.
        return _ResolvedRow(origin, state_code, True, False, blank)
    record = _synthesize_gap_record(
        gap_hit, state=state_code, edfi_version=ctx.edfi_version
    )
    # Gap rows are swagger-sourced by construction (the surfacer emits a row
    # only when the state's source doc is silent on a spine-known pair).
    # Stamp the provenance so the ai-Documentation Source cell reads
    # "Swagger" rather than the synthesize_record default of "source_doc".
    record = record.model_copy(update={"documentation_source": "swagger"})
    is_ext = _is_extension_record(record)
    ed_domain = ctx.domain_cache.get(record.entity)
    poc3_cells = _ai_cells(record, state_code, is_ext, ed_domain, gap_score)
    return _ResolvedRow(
        origin, state_code, True, True, poc3_cells, canonical_key, via_gap=True
    )


# ---------------------------------------------------------------------------
# Workbook writer
# ---------------------------------------------------------------------------


def _write_details_sheet(
    ws: Worksheet,
    *,
    sheet_title: str,
    origin_headers: list[Any],
    resolved: list[_ResolvedRow],
    include_match_status: bool = False,
) -> None:
    """Write the back-fill ``Details`` sheet.

    When ``include_match_status`` is True, insert a single
    ``Match Status`` column between the origin (human-authored) block
    and the ``ai-`` block, populated with the same bucket strings the
    ai-summary tab uses (``match_exact`` / ``match_tier`` /
    ``tier_delta_1`` / ``tier_delta_ge2`` / ``human_only`` /
    ``out_of_scope`` / ``ai_only`` / ``neither``). An Excel auto-filter
    is added on the same call so reviewers can filter by Match Status
    (or any column) without enabling AutoFilter manually.
    """
    ws.title = sheet_title
    full_headers = list(origin_headers)
    if include_match_status:
        full_headers.append(_MATCH_STATUS_HEADER)
    full_headers.extend(_POC3_HEADERS)
    for col_idx, h in enumerate(full_headers, start=1):
        c = ws.cell(row=1, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    for row_idx, rr in enumerate(resolved, start=2):
        cells = list(rr.origin.cells)
        if include_match_status:
            cells.append(_bucket_for_row(rr))
        cells.extend(rr.poc3_cells)
        for col_idx, value in enumerate(cells, start=1):
            _set_cell(ws, row_idx, col_idx, value)
    for col_idx in range(1, len(full_headers) + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 22
    for col_idx, h in enumerate(full_headers, start=1):
        if isinstance(h, str) and ("Business Logic" in h or h.endswith("References")):
            ws.column_dimensions[get_column_letter(col_idx)].width = 55
    if include_match_status:
        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = "A2"


def _match_rate_rows(
    resolved: list[_ResolvedRow],
) -> tuple[list[tuple[str, int, int, float]], int, int]:
    """Return ``(per-state rate rows, out-of-scope total, blank-state total)``."""
    by_state_total: dict[str, int] = {}
    by_state_matched: dict[str, int] = {}
    out_of_scope_total = 0
    blank_state_total = 0
    for rr in resolved:
        if rr.origin.state_raw is None and rr.state_code is None:
            blank_state_total += 1
            continue
        if not rr.in_scope:
            out_of_scope_total += 1
            continue
        sc = rr.state_code or "(none)"
        by_state_total[sc] = by_state_total.get(sc, 0) + 1
        if rr.matched:
            by_state_matched[sc] = by_state_matched.get(sc, 0) + 1
    table: list[tuple[str, int, int, float]] = []
    for sc in sorted(by_state_total):
        total = by_state_total[sc]
        matched = by_state_matched.get(sc, 0)
        pct = (matched / total * 100.0) if total else 0.0
        table.append((sc, matched, total, round(pct, 1)))
    return table, out_of_scope_total, blank_state_total


def _ai_score_for(rr: _ResolvedRow) -> tuple[int | None, float | None]:
    """Pull the AI tier + adjusted score out of the rendered ai- block."""
    if not rr.matched:
        return (None, None)
    # _POC3_HEADERS[i] aligns 1:1 with rr.poc3_cells[i]; locate the two
    # interesting cells by header name so this stays robust if the header
    # tuple is reordered.
    nachos_idx = _POC3_HEADERS.index("ai-Base NACHOS Score")
    # Single space post Sequence-1 hygiene — tracks `_DETAILS_HEADERS`.
    adj_idx = _POC3_HEADERS.index("ai-Adjusted NACHOS Score")
    nachos = rr.poc3_cells[nachos_idx]
    adj = rr.poc3_cells[adj_idx]
    if not isinstance(nachos, (int, float)):
        nachos = None
    return (
        int(nachos) if isinstance(nachos, (int, float)) else None,
        float(adj) if isinstance(adj, (int, float)) else None,
    )


def _classify(
    human_tier: int | None,
    human_adj: float | None,
    ai_tier: int | None,
    ai_adj: float | None,
) -> str:
    """Delegates to ``workbook_spec.classify_human_vs_ai`` (hoisted for
    the `--with-human` Details columns — one source of truth for the
    bucket vocabulary). See that function for the bucket definitions.

    Pure-function over scores only — assumes the caller has already
    handled the in-scope / out-of-scope distinction (``_summary_payload``
    overrides the bucket to ``out_of_scope`` before invoking).
    """
    from src.report.workbook_spec import classify_human_vs_ai

    return classify_human_vs_ai(human_tier, human_adj, ai_tier, ai_adj)


_CLASSIFICATION_ORDER: tuple[str, ...] = (
    "match_exact",
    "match_tier",
    "tier_delta_1",
    "tier_delta_ge2",
    "gap_row_match",
    "human_only",
    "out_of_scope",
    "ai_only",
    "neither",
)

def _count_with_pct(count: int, total: int) -> str:
    """Render a cell as ``"N (P.P%)"``; just ``"0"`` when total is zero."""
    if total <= 0:
        return str(count)
    pct = count / total * 100.0
    return f"{count} ({pct:.1f}%)"


# ``human_only`` and ``out_of_scope`` are kept as siblings to stop the
# classification block's count from silently swallowing rows where AI
# never had a sidecar to consult (state outside WI/MN/TX/AZ/IN). Match-rate
# metadata above the block already excludes those rows from its
# denominator; carrying ``out_of_scope`` as its own bucket keeps the
# two sections speaking the same language.
_CLASSIFICATION_DESCRIPTION: dict[str, str] = {
    "match_exact": "Tier and adjusted score agree (Δadj < 0.01).",
    "match_tier": "Tier agrees, adjusted score differs.",
    "tier_delta_1": "Tiers differ by 1.",
    "tier_delta_ge2": "Tiers differ by 2 or more.",
    "gap_row_match": (
        "Resolved via the spine-anchored gap artifact (swagger-sourced, "
        "deterministic gap score). Coverage recovery — the state's source "
        "doc was silent on the pair; tracked separately from source-lens "
        "scoring accuracy."
    ),
    "human_only": "In-scope row, human scored, AI couldn't resolve a sidecar row.",
    "out_of_scope": "State not in WI/MN/TX/AZ/IN (or blank); AI did not attempt resolution.",
    "ai_only": "AI scored, human cell blank.",
    "neither": "Both sides blank.",
}


def _bucket_for_row(rr: _ResolvedRow) -> str:
    """Classify one resolved row's match status.

    Out-of-scope rows return ``"out_of_scope"`` regardless of score
    presence — keeping the data sheet's Match Status column aligned
    with the ai-summary classification block. In-scope rows defer to
    ``_classify`` over the (human, ai) tier+adj pair.
    """
    if not rr.in_scope:
        return "out_of_scope"
    # Gap-sourced rows are coverage recoveries, not source-lens scoring
    # predictions — keep them in their own bucket so the scoring-accuracy
    # buckets (match_exact / tier_delta_*) stay clean. The ai- score cells
    # are still populated, so a reader can drill into the tier comparison
    # (and gap rows still appear in the tier confusion matrix below).
    if rr.via_gap:
        return "gap_row_match"
    ai_tier, ai_adj = _ai_score_for(rr)
    return _classify(rr.origin.human_nachos, rr.origin.human_adj, ai_tier, ai_adj)


def _summary_payload(resolved: list[_ResolvedRow]) -> dict[str, Any]:
    """Build the per-row classification + aggregate stats."""
    classified: list[
        tuple[str, str | None, int | None, float | None, int | None, float | None]
    ] = []
    counts: dict[str, int] = {bucket: 0 for bucket in _CLASSIFICATION_ORDER}
    counts_by_state: dict[str, dict[str, int]] = {}
    confusion: dict[tuple[Any, Any], int] = {}
    abs_tier_deltas: list[int] = []
    abs_adj_deltas: list[float] = []
    for rr in resolved:
        ai_tier, ai_adj = _ai_score_for(rr)
        bucket = _bucket_for_row(rr)
        if not rr.in_scope:
            # Out-of-scope rows weren't a real prediction attempt — the
            # state isn't in WI/MN/TX/AZ. Skip them from the tier
            # confusion matrix so the matrix reflects in-scope behavior
            # only.
            sc = "(out of scope)"
        else:
            sc = rr.state_code or "(unmapped)"
            confusion[(rr.origin.human_nachos, ai_tier)] = (
                confusion.get((rr.origin.human_nachos, ai_tier), 0) + 1
            )
        counts[bucket] += 1
        counts_by_state.setdefault(sc, {b: 0 for b in _CLASSIFICATION_ORDER})[bucket] += 1
        if (
            rr.origin.human_nachos is not None
            and ai_tier is not None
        ):
            abs_tier_deltas.append(abs(rr.origin.human_nachos - ai_tier))
        if (
            rr.origin.human_adj is not None
            and ai_adj is not None
        ):
            abs_adj_deltas.append(abs(rr.origin.human_adj - ai_adj))
        classified.append(
            (bucket, rr.state_code, rr.origin.human_nachos, rr.origin.human_adj, ai_tier, ai_adj)
        )
    return {
        "counts": counts,
        "counts_by_state": counts_by_state,
        "confusion": confusion,
        "tier_delta_mean_abs": (
            sum(abs_tier_deltas) / len(abs_tier_deltas) if abs_tier_deltas else None
        ),
        "tier_delta_max_abs": max(abs_tier_deltas) if abs_tier_deltas else None,
        "tier_delta_n": len(abs_tier_deltas),
        "adj_delta_mean_abs": (
            sum(abs_adj_deltas) / len(abs_adj_deltas) if abs_adj_deltas else None
        ),
        "adj_delta_max_abs": max(abs_adj_deltas) if abs_adj_deltas else None,
        "adj_delta_n": len(abs_adj_deltas),
    }


def _render_summary_section(
    ws: Worksheet,
    *,
    start_row: int,
    section_label: str,
    resolved: list[_ResolvedRow],
) -> int:
    """Render one summary block (classification + per-state + confusion + deltas).

    Returns the next free row index (one blank row after the section).
    """
    payload = _summary_payload(resolved)
    counts = payload["counts"]
    counts_by_state = payload["counts_by_state"]
    confusion = payload["confusion"]

    row = start_row
    _set_cell(ws, row, 1, section_label).font = _HEADER_FONT
    _set_cell(ws, row, 2, f"n = {len(resolved)}")
    row += 2

    _set_cell(ws, row, 1, "Classification").font = _HEADER_FONT
    _set_cell(ws, row, 2, "Count").font = _HEADER_FONT
    _set_cell(ws, row, 3, "% of total").font = _HEADER_FONT
    _set_cell(ws, row, 4, "Description").font = _HEADER_FONT
    row += 1
    total = len(resolved)
    for bucket in _CLASSIFICATION_ORDER:
        cnt = counts.get(bucket, 0)
        pct = (cnt / total * 100.0) if total else 0.0
        _set_cell(ws, row, 1, bucket)
        _set_cell(ws, row, 2, cnt)
        _set_cell(ws, row, 3, f"{pct:.1f}%")
        _set_cell(ws, row, 4, _CLASSIFICATION_DESCRIPTION[bucket])
        row += 1
    _set_cell(ws, row, 1, "TOTAL").font = _HEADER_FONT
    _set_cell(ws, row, 2, total).font = _HEADER_FONT
    row += 2

    if counts_by_state:
        _set_cell(ws, row, 1, "Per-state breakdown (count + % of state total)").font = _HEADER_FONT
        row += 1
        _set_cell(ws, row, 1, "State").font = _HEADER_FONT
        for col_offset, bucket in enumerate(_CLASSIFICATION_ORDER, start=2):
            _set_cell(ws, row, col_offset, bucket).font = _HEADER_FONT
        _set_cell(ws, row, 2 + len(_CLASSIFICATION_ORDER), "Total").font = _HEADER_FONT
        row += 1
        for sc in sorted(counts_by_state):
            _set_cell(ws, row, 1, sc)
            row_total = sum(counts_by_state[sc].get(b, 0) for b in _CLASSIFICATION_ORDER)
            for col_offset, bucket in enumerate(_CLASSIFICATION_ORDER, start=2):
                cnt = counts_by_state[sc].get(bucket, 0)
                _set_cell(ws, row, col_offset, _count_with_pct(cnt, row_total))
            _set_cell(ws, row, 2 + len(_CLASSIFICATION_ORDER), row_total)
            row += 1
        row += 1

    _set_cell(
        ws, row, 1,
        "Tier confusion (in-scope rows only; rows = human, cols = AI; "
        "cells = count + % of human-row total)",
    ).font = _HEADER_FONT
    row += 1
    tier_axis: list[Any] = [0, 1, 2, 3, None]
    _set_cell(ws, row, 1, "human \\ ai").font = _HEADER_FONT
    for col_offset, ai_tier in enumerate(tier_axis, start=2):
        _set_cell(ws, row, col_offset, "blank" if ai_tier is None else ai_tier).font = _HEADER_FONT
    _set_cell(ws, row, 2 + len(tier_axis), "Total").font = _HEADER_FONT
    row += 1
    for human_tier in tier_axis:
        _set_cell(ws, row, 1, "blank" if human_tier is None else human_tier).font = _HEADER_FONT
        row_total = sum(confusion.get((human_tier, t), 0) for t in tier_axis)
        for col_offset, ai_tier in enumerate(tier_axis, start=2):
            cnt = confusion.get((human_tier, ai_tier), 0)
            _set_cell(ws, row, col_offset, _count_with_pct(cnt, row_total))
        _set_cell(ws, row, 2 + len(tier_axis), row_total)
        row += 1
    row += 1

    _set_cell(ws, row, 1, "Both-scored deltas").font = _HEADER_FONT
    row += 1
    delta_rows = [
        ("Tier mean |Δ|", payload["tier_delta_mean_abs"]),
        ("Tier max |Δ|", payload["tier_delta_max_abs"]),
        ("Tier rows compared", payload["tier_delta_n"]),
        ("Adjusted mean |Δ|", payload["adj_delta_mean_abs"]),
        ("Adjusted max |Δ|", payload["adj_delta_max_abs"]),
        ("Adjusted rows compared", payload["adj_delta_n"]),
    ]
    for k, v in delta_rows:
        _set_cell(ws, row, 1, k)
        if isinstance(v, float):
            _set_cell(ws, row, 2, round(v, 3))
        else:
            _set_cell(ws, row, 2, v)
        row += 1
    return row + 2  # one blank row of separation between sections


def _classify_leaf_recovery(
    resolved: list[_ResolvedRow],
    contexts: dict[str, "_StateContext"],
) -> dict[str, int]:
    """Count matched rows per state whose sidecar carries
    ``documentation_source == "swagger_leaf"``.

    Issue #147 observability: surfaces the cohort of reviewer-named
    sub-collection / sub-entity leaves that the cross-lens borrow
    recovered for keymap-join. A row counts when (a) the keymap-join
    matched it, (b) the matched sidecar entry tags
    ``documentation_source == "swagger_leaf"``. ``human_only`` and
    ``out_of_scope`` rows do not contribute.
    """
    counts: dict[str, int] = {}
    for rr in resolved:
        if not rr.matched or rr.record_key is None:
            continue
        if rr.state_code is None:
            continue
        ctx = contexts.get(rr.state_code)
        if ctx is None:
            continue
        score = ctx.scores_by_key.get(rr.record_key)
        if score is None:
            continue
        if score.get("documentation_source") != "swagger_leaf":
            continue
        counts[rr.state_code] = counts.get(rr.state_code, 0) + 1
    return counts


def _classify_pattern_f_residual(
    resolved: list[_ResolvedRow],
    contexts: dict[str, "_StateContext"],
) -> dict[str, dict[str, int]]:
    """Bucket the human_only cohort by Pattern F (irreducible-by-keymap) cause.

    Returns ``{state: {bucket: count}}``. Counts only rows that fall in
    the analyst-relevant ``human_only`` bucket — in-scope, unmatched,
    AND human-scored. Skips ``neither``-bucket rows (no human score,
    no match) so the surfaced count aligns with the per-state
    classification block. Buckets:

    - ``F-na-entity`` — reviewer wrote ``"NA"`` as the entity name, a
      catch-all placeholder used in the WI training file for HL7
      immunization extension scope (see issue #119 for methodology
      decision).
    - ``F-entity-absent`` — entity name doesn't appear in the state's
      sidecar OR spine catalog. The state's source doc didn't surface
      this entity (chart-of-accounts financial domain, AZ Part-C
      AZEIPs program, etc.) — keymap recovery isn't possible.

    Pattern A/B/C/D buckets (recoverable by keymap work) are NOT
    counted here — those are PR-3-fixable or PR-3-deferred. This
    helper only surfaces the irreducible residual so the deliverable
    can document it transparently. Issue #138 PR 3.
    """
    counts: dict[str, dict[str, int]] = {}
    for rr in resolved:
        if not rr.in_scope or rr.matched:
            continue
        if rr.origin.human_nachos is None:
            continue
        state = rr.state_code
        if state is None:
            continue
        ctx = contexts.get(state)
        if ctx is None:
            continue
        ent = (rr.origin.entity or "").strip()
        ent_l = ent.lower()
        if not ent_l:
            continue
        bucket: str | None = None
        if ent_l == "na":
            bucket = "F-na-entity"
        else:
            sidecar_ents = {
                e.lower() for e in {
                    s["entity"] for s in ctx.scores_by_key.values()
                }
            }
            spine_ents = {
                e.lower()
                for e in (
                    set(ctx.inputs.spine.catalog.entities)
                    | set(ctx.inputs.spine.catalog.extensions)
                )
            }
            if ent_l not in sidecar_ents and ent_l not in spine_ents:
                bucket = "F-entity-absent"
        if bucket is None:
            continue
        counts.setdefault(state, {})[bucket] = (
            counts.setdefault(state, {}).get(bucket, 0) + 1
        )
    return counts


def _write_ai_summary_sheet(
    ws: Worksheet,
    *,
    config: BackfillConfig,
    resolved: list[_ResolvedRow],
    contexts: dict[str, "_StateContext"] | None = None,
) -> None:
    """Render the consolidated ai-summary tab.

    Single sheet for all three back-fill configs. Top section carries
    the readme-style metadata (methodology, lens, generated date, input
    file name, total rows, per-state match rate, out-of-scope counts,
    a one-paragraph note on row handling). When the config has
    ``human_nachos_col`` + ``human_adj_col`` set, an "All states"
    classification block follows, plus an optional "Excluding {states}"
    block when ``summary_exclude_states`` is non-empty.

    When ``contexts`` is supplied, an irreducible-residual line is
    added to the metadata block — Pattern F buckets (NA-entity
    placeholders, entities the state's source doc doesn't surface) the
    keymap can never recover. Issue #138 PR 3.
    """
    ws.title = "ai-summary"

    row = 1
    _set_cell(ws, row, 1, "Human vs AI scoring — summary").font = _HEADER_FONT
    row += 2

    rate_rows, out_of_scope_total, blank_state_total = _match_rate_rows(resolved)
    metadata: list[tuple[str, str]] = [
        ("Methodology version", f"SCORING_PLAN_VERSION = {SCORING_PLAN_VERSION}"),
        ("Lens", "source"),
        ("Generated", date.today().isoformat()),
        ("Config", config.name),
        ("Input file", config.input_path.name),
        ("Sheet", config.sheet_name),
        ("Total rows", str(len(resolved))),
        (
            "AI columns",
            f"{len(_POC3_HEADERS)} appended (prefix '{_AI_PREFIX}'); "
            "human-authored cells preserved verbatim.",
        ),
    ]
    for sc, matched, total, pct in rate_rows:
        metadata.append((f"Match rate — {sc}", f"{matched}/{total} ({pct:.1f}%)"))
    metadata.append(("Out of scope (state not WI/MN/TX/AZ/IN)", str(out_of_scope_total)))
    metadata.append(("Blank state cells", str(blank_state_total)))
    metadata.append(
        (
            "Note",
            "Out-of-scope rows preserve origin cells; ai- cells blank. "
            "In-scope rows whose entity/element does not resolve through "
            "the reviewer-keymap candidate set (parent entity omitted, "
            "FK-nav path the spine cannot follow, naming convention "
            "diverges) also render blank ai- cells.",
        ),
    )
    if contexts:
        f_buckets = _classify_pattern_f_residual(resolved, contexts)
        if f_buckets:
            line_parts: list[str] = []
            for state in sorted(f_buckets):
                buckets = f_buckets[state]
                segments = [
                    f"{label} {count}"
                    for label, count in sorted(buckets.items())
                ]
                line_parts.append(f"{state}: {', '.join(segments)}")
            note_text = (
                "; ".join(line_parts)
                + " — irreducible by keymap. F-na-entity rows are reviewer "
                "placeholders for entities outside the Ed-Fi spine "
                "(WI HL7 immunization scope tracked in issue #119). "
                "F-entity-absent rows name entities the state's source "
                "doc doesn't surface (e.g. WI chartOfAccounts financial "
                "domain, AZ Part-C AZEIPs)."
            )
            metadata.append(("Pattern F residual", note_text))
        leaf_counts = _classify_leaf_recovery(resolved, contexts)
        if leaf_counts:
            leaf_segments = [
                f"{state}: {leaf_counts[state]}"
                for state in sorted(leaf_counts)
            ]
            leaf_note = (
                ", ".join(leaf_segments)
                + " — sub-collection / sub-entity leaves the source doc "
                "didn't enumerate, recovered by cross-lens borrow from "
                "spine-lens (issue #147; documentation_source="
                "\"swagger_leaf\")."
            )
            metadata.append(
                ("Leaf rows recovered by cross-lens borrow", leaf_note)
            )
    for k, v in metadata:
        _set_cell(ws, row, 1, k)
        _set_cell(ws, row, 2, v)
        row += 1
    row += 1

    if config.human_nachos_col is not None and config.human_adj_col is not None:
        row = _render_summary_section(
            ws, start_row=row, section_label="All states", resolved=resolved,
        )

        if config.summary_exclude_states:
            excluded = set(config.summary_exclude_states)
            kept = [rr for rr in resolved if rr.state_code not in excluded]
            excluded_label = "/".join(sorted(excluded))
            row = _render_summary_section(
                ws,
                start_row=row,
                section_label=f"Excluding {excluded_label}",
                resolved=kept,
            )

    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 60
    for col_offset in range(3, 3 + len(_CLASSIFICATION_ORDER)):
        ws.column_dimensions[get_column_letter(col_offset)].width = 14
    ws.column_dimensions[get_column_letter(3 + len(_CLASSIFICATION_ORDER))].width = 60


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Human-score resolution for `report analyst --with-human` (Option C)
# ---------------------------------------------------------------------------

# Header-name candidates for autodetection (whitespace-normalized), in
# preference order. Covers our generated workbooks AND the hand-built
# analyst template (incl. the genuinely double-spaced legacy
# `Adjusted NACHOS  Score`, which normalization collapses).
_HUMAN_HEADER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "state": ("State",),
    # `ResourceName` = the IN per-state workbook's entity column.
    "entity": ("Entity Name", "Entity", "Table Name", "ResourceName"),
    "element": ("Data Element", "Element", "Field Name"),
    "nachos": ("NACHOS score", "NACHOS Score", "Base NACHOS Score"),
    # `Adjusted Score` = the IN per-state workbook's naming; last so the
    # canonical name keeps preference (the `- Original MSP` decoys can't
    # match either way — full-string comparison).
    "adjusted": ("Adjusted NACHOS Score", "Adjusted Score"),
}


def _norm_hdr(value: Any) -> str:
    import re

    return re.sub(r"\s+", " ", str(value or "").strip())


def _detect_config(input_path: Path, sheet_name: str | None) -> BackfillConfig:
    """Build a BackfillConfig by locating columns by header NAME."""
    wb = load_workbook(input_path, read_only=True, data_only=True)
    try:
        sheet = sheet_name or (
            "Details" if "Details" in wb.sheetnames else wb.sheetnames[0]
        )
        if sheet not in wb.sheetnames:
            raise ValueError(
                f"sheet {sheet!r} not in {input_path.name}; "
                f"available: {wb.sheetnames}"
            )
        header_row = next(
            wb[sheet].iter_rows(min_row=1, max_row=1, values_only=True), ()
        )
    finally:
        wb.close()
    col_of = {_norm_hdr(h): i for i, h in enumerate(header_row)}

    def find(kind: str) -> int | None:
        for candidate in _HUMAN_HEADER_CANDIDATES[kind]:
            if candidate in col_of:
                return col_of[candidate]
        return None

    entity_col = find("entity")
    element_col = find("element")
    nachos_col = find("nachos")
    adj_col = find("adjusted")
    if entity_col is None or element_col is None:
        raise ValueError(
            f"could not locate entity/element columns by header in "
            f"{input_path.name} sheet {sheet!r} — headers: "
            f"{[_norm_hdr(h) for h in header_row]}. Pass --human-config "
            f"for a known fixed-column file."
        )
    if nachos_col is None and adj_col is None:
        raise ValueError(
            f"no human score column found in {input_path.name} sheet "
            f"{sheet!r} (looked for {_HUMAN_HEADER_CANDIDATES['nachos']} / "
            f"{_HUMAN_HEADER_CANDIDATES['adjusted']})."
        )
    return BackfillConfig(
        name="with-human-autodetect",
        input_path=input_path,
        sheet_name=sheet,
        output_path=input_path,  # never written; required field
        state_col=find("state") if find("state") is not None else -1,
        entity_col=entity_col,
        element_col=element_col,
        human_nachos_col=nachos_col,
        human_adj_col=adj_col,
    )


def resolve_human_scores(
    input_path: Path,
    *,
    config: str | None = None,
    sheet_name: str | None = None,
    fixed_state: str | None = None,
    sidecar_dir: Path | None = None,
    spine_dir_path: Path | None = None,
) -> dict[str, tuple[int | None, float | None]]:
    """Map a human-scored workbook onto sidecar record keys.

    Returns ``{record_key: (human base tier, human adjusted)}`` for
    ``report analyst --with-human``. A named ``config`` reuses the
    proven per-state ``CONFIGS`` header map (with ``input_path``
    overriding the config's baked path — resolution stays fail-loud
    against the passed file's actual headers); otherwise columns are
    located by header NAME (`_detect_config`). Key resolution reuses
    the June keymap join (``reviewer_key_to_poc3_key``) so hand-written
    entity/element spellings land; rows that resolve only via the gap
    artifact are deliberately skipped — they have no Details row to
    decorate.
    """
    from dataclasses import replace as _dc_replace

    side = sidecar_dir or out_dir()
    spine_d = spine_dir_path or spine_dir()
    if config is not None:
        if config not in CONFIGS:
            raise ValueError(
                f"unknown --human-config {config!r}; "
                f"known: {sorted(CONFIGS)}"
            )
        cfg = _dc_replace(
            CONFIGS[config],
            input_path=input_path,
            **({"sheet_name": sheet_name} if sheet_name else {}),
        )
    else:
        cfg = _detect_config(input_path, sheet_name)
    if fixed_state is not None:
        cfg = _dc_replace(cfg, fixed_state=fixed_state.upper())

    _headers, origin_rows = _load_origin(cfg)
    contexts: dict[str, _StateContext | None] = {}
    out: dict[str, tuple[int | None, float | None]] = {}
    for origin in origin_rows:
        if origin.human_nachos is None and origin.human_adj is None:
            continue
        if cfg.fixed_state is not None:
            state_code: str | None = cfg.fixed_state
        elif origin.state_raw is not None:
            state_code = _STATE_NORMALIZE.get(origin.state_raw) or (
                origin.state_raw.upper()
                if origin.state_raw.upper() in _IN_SCOPE_STATES
                else None
            )
        else:
            state_code = None
        if state_code is None or state_code not in _IN_SCOPE_STATES:
            continue
        if state_code not in contexts:
            contexts[state_code] = _load_state_context(
                state_code, sidecar_dir=side, spine_dir_path=spine_d
            )
        ctx = contexts[state_code]
        if ctx is None or origin.entity is None or origin.element is None:
            continue
        record_key = reviewer_key_to_poc3_key(
            origin.entity, origin.element, ctx.lookup, spine=ctx.spine_index
        )
        if record_key is None or record_key in out:
            continue
        out[record_key] = (origin.human_nachos, origin.human_adj)
    return out


def build_workbook(
    config: BackfillConfig,
    *,
    sidecar_dir: Path | None = None,
    spine_dir_path: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    """Generate the back-fill workbook for ``config``. Returns the output path."""
    # Materialize header-mapped columns up front so the ai-summary /
    # Match-Status gates below see real human_nachos_col/human_adj_col.
    config = _resolve_config(config)
    side = sidecar_dir or out_dir()
    spine_d = spine_dir_path or spine_dir()
    dst = output_path or config.output_path

    headers, origin_rows = _load_origin(config)
    contexts: dict[str, _StateContext] = {}
    for state in _IN_SCOPE_STATES:
        ctx = _load_state_context(
            state, sidecar_dir=side, spine_dir_path=spine_d
        )
        if ctx is not None:
            contexts[state] = ctx
    resolved = [
        _resolve_row(o, contexts, fixed_state=config.fixed_state)
        for o in origin_rows
    ]

    include_match_status = (
        config.human_nachos_col is not None
        and config.human_adj_col is not None
    )
    wb = Workbook()
    details_ws = wb.active
    _write_details_sheet(
        details_ws,
        sheet_title=config.sheet_name,
        origin_headers=headers,
        resolved=resolved,
        include_match_status=include_match_status,
    )
    summary_ws = wb.create_sheet("ai-summary")
    _write_ai_summary_sheet(
        summary_ws, config=config, resolved=resolved, contexts=contexts
    )
    # ai-summary leads the workbook (public API — poking the private
    # `wb._sheets` list worked but bypassed openpyxl's ordering seam).
    wb.move_sheet(summary_ws, offset=-wb.index(summary_ws))

    dst.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dst)
    rate_rows, out_of_scope_total, blank_state_total = _match_rate_rows(resolved)
    _LOGGER.info(
        "%s workbook written to %s (rows=%d, out_of_scope=%d, blank_state=%d, match_rates=%s)",
        config.name,
        dst,
        len(resolved),
        out_of_scope_total,
        blank_state_total,
        ", ".join(
            f"{sc}={matched}/{total}({pct}%)"
            for sc, matched, total, pct in rate_rows
        ),
    )
    return dst


def run(
    config_name: str,
    *,
    sidecar_dir: Path | None = None,
    spine_dir_path: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    """Plain-function orchestration; Click wrapper lives in ``cli.py``.

    ``config_name`` selects from ``CONFIGS`` (e.g. ``"arizona"``).
    """
    if config_name not in CONFIGS:
        raise ValueError(
            f"unknown back-fill config {config_name!r}; "
            f"available: {sorted(CONFIGS)}"
        )
    return build_workbook(
        CONFIGS[config_name],
        sidecar_dir=sidecar_dir,
        spine_dir_path=spine_dir_path,
        output_path=output_path,
    )
