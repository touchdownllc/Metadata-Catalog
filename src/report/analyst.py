"""Analyst XLSX export — matches NACHOS Template.xlsx shape, NO Texas data.

Template discipline (from next-session-prompt.md, Phase 4b):
    The shipped `NACHOS Template.xlsx` at repo root has TEXAS GT data in its
    Details / Reference sheets. **We use it for shape only**: replicate
    header rows + column counts, but do NOT read or carry through any of
    its cell VALUES. Sheet names in our output are audience-first (Reviewer
    View / Scoring Summary / Audit Trail / etc. — see issue #56), not
    template-verbatim. Score Card and Entities by Domain sheets in the
    template have no standard header row — we write fresh layouts that are
    analyst-friendly.

In practice we build the workbook from scratch. An earlier attempt to
`load_workbook(Template)` + delete rows hung on openpyxl's styled 1MB
source; building fresh is ~2 orders of magnitude faster and sidesteps the
Texas-contamination risk entirely.

Outputs (per plan):
    - `data/out/{state}_analyst.xlsx`  — one per state, when run(state=X).
    - `data/out/coverage_analyst.xlsx` — combined, when run(state=None).

Scoring columns (`NACHOS score`, `Adjusted NACHOS Score`, `Complex Business
Logic`, etc.) are written as `None` — genuinely blank, not `""` or `"NA"`.
This matches POC-3's ingestion-only scope.

CRITICAL: module-level `run()` is a PLAIN function. Do NOT decorate it with
`@click.command()` — see `tests/test_report_analyst.py::TestCliWiring`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.worksheet import Worksheet

from src.models.element import ElementRecord
from src.states import SUPPORTED_STATES

# R2 declarative workbook spec (issue #186 sequence 2). The value-group
# helpers + display maps moved to `workbook_spec` verbatim; they are
# re-imported here so every existing consumer (`human_score_backfill`,
# `tests/test_score_rubric.py` lockstep tests, `TestLegacyTemplateFields`)
# keeps its import path. New code should import from `workbook_spec`.
from src.report.workbook_spec import (  # noqa: F401  (re-exports)
    _DOC_SOURCE_DISPLAY,
    _DOC_STYLE_DISPLAY,
    _MATCH_STATUS_BY_SOURCE,
    _STRUCTURAL_DEPTH_LABELS,
    _doc_source_label,
    _integration_profile_fields,
    _is_extension_record,
    _legacy_template_fields,
    _match_status,
    _references_for_record,
    _review_cells,
    _score_fields,
    _summarize_record_recommendations,
    _format_fact_value,
    _format_fact_spans,
    _synthesize_rule_paths,
    _synthesize_downgraded_facts,
    _SEMANTIC_CLASS_SPANS_HEADER,
    _INTEGRATION_CLASS_SPANS_HEADER,
    _SOURCE_FACT_ORDER,
    _SPINE_FACT_ORDER,
    _SOURCE_DIM_ORDER,
    _SPINE_DIM_ORDER,
    _NACHOS_ELEMENTS_COLUMNS,
    _DETAILS_FINISH,
    _SheetFinish,
    AUDIT_TRAIL_SHEET,
    ENTITIES_BY_DOMAIN_SHEET,
    KNOWN_LIMITATIONS_SHEET,
    PEER_GAPS_SHEET,
    RECOMMENDATIONS_SHEET,
    REFERENCE_SHEET,
    REVIEW_QUEUE_SHEET,
    COMMITMENT_TRACKER_SHEET,
    DETAILS_COLUMNS,
    DOCUMENTATION_GAPS_SHEET,
    backfill_columns,
    DETAILS_SHEET,
    DIMENSION_DISPLAY,
    EXTENSION_DETAILS_SHEET,
    HumanCells,
    details_sheet_with_cmp,
    SCORING_SUMMARY_COLUMNS,
    SCORING_SUMMARY_SHEET,
    SPINE_GAP_SHEET,
    SHEET_SPECS,
    RowContext,
)
from src.report.workbook_render import (  # noqa: F401  (re-exports)
    _HEADER_FONT,
    _HEADER_FILL,
    ANALYST_BAND_NOTE,
    _finalize_workbook,
    _finish_for,
    _set_cell,
    _set_widths,
    apply_details_row_links,
    apply_reviewed_echo,
    render_table_sheet,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_OUT_DIR = _PROJECT_ROOT / "data" / "out"
_SPINE_DIR = _PROJECT_ROOT / "data" / "spine"

_STATES = SUPPORTED_STATES

# Peer-gap artifact lives under the scoring/phase_a subtree. Per-state
# analyst workbooks read this file (when present) to render the Peer
# Gaps sheet. `run()` derives the full path as
# `{out_dir}/{_PEER_GAP_REL_PATH}` so tests that monkeypatch `_OUT_DIR`
# see the artifact via the tmp_path override.
_PEER_GAP_REL_PATH = Path("scoring") / "phase_a" / "peer_gap.jsonl"


# Artifact loaders + record keying moved to `report/loaders.py` (issue
# #213 item 1) — the single home for the verify_fresh /
# read_json_artifact reader policy and the Row-# address derivation.
# Old private names re-imported below for tests/consumers
# (`audit`, `human_score_backfill`); `_load_inputs` stays a shim
# resolving this module's `_OUT_DIR`/`_SPINE_DIR` at CALL time (the
# analyst-test monkeypatch seam).
from src.report.loaders import (  # noqa: E402, F401  (re-exports)
    StateInputs,
    load_inputs,
    load_scores_sidecar as _load_scores_sidecar,
    load_gap_scores_sidecar as _load_gap_scores_sidecar,
    load_gap_metadata as _load_gap_metadata,
    load_review_queue_routes as _load_review_queue_routes,
    load_peer_gap_artifact as _load_peer_gap_artifact,
    record_key as _record_key,
    canonical_sort_key as _canonical_sort_key,
    row_number_index as _row_number_index,
    display_source_url as _display_source_url,
    format_source_coverage as _format_source_coverage,
    format_spine_coverage as _format_spine_coverage,
)


def _load_inputs(state: str, lens: str = "source") -> StateInputs:
    """Compat shim — resolves this module's ``_OUT_DIR``/``_SPINE_DIR``
    at call time (the analyst-test monkeypatch seam). New code calls
    ``loaders.load_inputs`` with explicit directories."""
    return load_inputs(state, lens, out_dir=_OUT_DIR, spine_dir=_SPINE_DIR)

# Editorial prose data (coverage-semantics note, per-state source-scope
# narratives, Methodology Notes rows) moved to `report/prose.py` (issue
# #213 item 1) — a ~330-line literal tuple inside this module is how the
# issue-#211 splice defect happened. Old private names kept as compat
# aliases for tests/consumers; new code imports from `prose`.
from src.report.prose import (  # noqa: E402, F401  (re-exports)
    COVERAGE_SEMANTICS_NOTE as _COVERAGE_SEMANTICS_NOTE,
    KNOWN_LIMITATIONS as _KNOWN_LIMITATIONS,
    SOURCE_SCOPE_BY_STATE as _SOURCE_SCOPE_BY_STATE,
    filter_known_limitations as _filter_known_limitations,
)

# `_KNOWN_LIMITATIONS_HEADERS` → `workbook_spec.KNOWN_LIMITATIONS_COLUMNS` (R2).

# Header rows lifted (text only) from NACHOS Template.xlsx row 1. Analyst
# feedback: the original "Domain" column mixed source-document reporting
# areas with what analysts reasonably expect to be Ed-Fi Data Standard
# domains; renamed to `Source Area` and a separate `Ed-Fi Domain` column
# now carries the spine-derived assignment.
# The `human_score_backfill` comparison-workbook projection: every
# Details column EXCEPT the analyst-input band (eight always-blank `ai-`
# columns there would be the #102 anti-pattern) and the renderer-injected
# `Row #`. Derived from the spec, so renames track automatically; frozen
# header snapshots live in `tests/test_workbook_spec.py`.


# `_backfill_columns` moved to `workbook_spec.backfill_columns` (issue
# #213 item 1 — pure spec logic); compat alias below.
_backfill_columns = backfill_columns


_DETAILS_HEADERS: tuple[str, ...] = tuple(
    c.header for c in _backfill_columns("source")
)

_DETAILS_HEADERS_SPINE: tuple[str, ...] = tuple(
    c.header for c in _backfill_columns("spine")
)

_REFERENCE_HEADERS: tuple[str, ...] = (
    "Domain",
    "Entity",
    "Reference",
    "In Ed-Fi Swagger?",
    "Observations",
)

_ENTITIES_BY_DOMAIN_HEADERS: tuple[str, ...] = ("Domain", "Entity")

_README_HEADERS: tuple[str, ...] = ("Sheet", "What it shows", "Who it's for")

# `_HEADER_FONT` / `_HEADER_FILL` moved to `workbook_render` (R2) — re-imported above.


# --- Sheet finish registry → `workbook_spec.SHEET_SPECS` (R2) --------------
#
# The `_SheetFinish` dataclass + per-sheet finish entries live on the
# unified SheetSpec registry; `_finalize_workbook` + `_finish_for` moved
# to `workbook_render` (re-imported above). Derived compat views below
# keep the Sequence-1 names importable for tests.

_SHEET_FINISH: dict[str, _SheetFinish] = {
    t: s.finish for t, s in SHEET_SPECS.items()
}

logger = logging.getLogger(__name__)

# Row style handed to `render_table_sheet(row_font=...)` by writers whose
# row dicts carry a `bold` marker (subtotal / Grand Total rows).
_BOLD_FONT = Font(bold=True)

# Option D (issue #186): the source-lens Details sheet shows DOCUMENTED
# rows only — the analysts' native ~2.4k-row shape. The undocumented
# swagger/swagger_leaf mass (60-80% of rows) relocates to the
# "Documentation Gaps" sheet. Toggle kept as a one-line revert if the
# analysts want the old everything-grid back. Row numbers stay computed
# over ALL rows (documented-first canonical sort ⇒ documented Row #s
# are unchanged by the relocation).
_DETAILS_DOCUMENTED_ONLY = True


def _details_stream(element_contexts, lens):
    # The rows the Details (and Extension Details) sheets render.
    if _DETAILS_DOCUMENTED_ONLY and lens == "source":
        return [c for c in element_contexts if c.record.documented]
    return element_contexts


# `StateInputs` / `_load_inputs` → `loaders` (re-imported above).


# `_is_extension_record` / `_match_status` / `_references_for_record` /
# `_MATCH_STATUS_BY_SOURCE` moved to `workbook_spec` (R2) — re-imported above.


# `_set_cell` / `_set_widths` moved to `workbook_render` (R2) — re-imported above.


# `_display_source_url` / `_format_source_coverage` /
# `_format_spine_coverage` → `loaders` (re-imported above).


# Score Card engine moved to `report/score_card.py` (issue #213 item 1)
# — per-state + combined blocks, the shared in-scope predicate, and the
# rollup/heatmap machinery. Old private names re-imported below for
# tests; builders call `_write_score_card` / `_build_combined_score_card`.
from src.report.score_card import (  # noqa: E402, F401  (re-exports)
    _ADJUSTED_BINS,
    _BASE_BINS,
    _HIGH_EFFORT_THRESHOLD,
    _REVIEW_ROUTES,
    _SOURCE_DIMENSIONS,
    _SPINE_DIMENSIONS,
    _ScoreCardContext,
    _CombinedScoreCardContext,
    record_in_scope as _record_in_scope,
    _aggregate_cross_state_rollup,
    _aggregate_nachos_cross_state,
    _bin_adjusted,
    _build_combined_score_card,
    _compute_state_rollup,
    _compute_structural_doc_matrix,
    _dimension_names,
    _fmt_float,
    _fmt_pct,
    _fmt_pct_float,
    _heatmap_fill_color,
    _mean,
    _write_score_card,
    _write_score_card_rollup,
    _write_structural_doc_heatmap,
)


def _write_adjudication_register(
    ws: Worksheet,
    start_row: int,
    entries: list[tuple[str, str, dict]],
    *,
    include_state: bool,
) -> int:
    """The adjudication register (issue #248 Part C) — rationale + who +
    when for every recorded consensus, rendered after the methodology
    version history so decisions stay auditable even after their
    OVERRIDE queue rows resolve away.

    ``entries`` is ``[(state, record_key, resolved adjudication)]`` from
    ``curation.adjudications_for``. Renders nothing (returns
    ``start_row``) when empty — workbooks without adjudications keep
    their Update Log byte-stable. The combined workbook passes
    ``include_state=True`` for one merged table with a State column.
    """
    if not entries:
        return start_row
    row = start_row + 1
    ws.cell(
        row=row, column=1,
        value="Adjudication register (team consensus — engine score "
              "never modified)",
    ).font = Font(bold=True, size=12)
    row += 1
    headers: tuple[str, ...] = (
        "Entity", "Data Element", "Adjudicated Score",
        "Engine at decision", "Plan at decision", "Status",
        "Agreed by", "Decided at", "Rationale",
    )
    if include_state:
        headers = ("State", *headers)
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1
    for state, _key, adj in sorted(entries, key=lambda t: (t[0], t[1])):
        status = (
            "fresh"
            if adj["fresh"]
            else f"STALE — re-adjudicate ({adj['stale_reason']})"
        )
        cells: list[object] = [
            adj.get("entity"),
            adj.get("element_name"),
            adj.get("value"),
            adj.get("engine_score_at_decision"),
            f"v{adj.get('plan_version_at_decision')}",
            status,
            "; ".join(adj.get("agreed_by") or []),
            adj.get("decided_at"),
            adj.get("rationale"),
        ]
        if include_state:
            cells.insert(0, state)
        for col_idx, value in enumerate(cells, start=1):
            ws.cell(row=row, column=col_idx, value=value)
        rationale_cell = ws.cell(row=row, column=len(cells))
        rationale_cell.alignment = Alignment(wrap_text=True, vertical="top")
        row += 1
    # Width touch-up only when the register rendered — a no-register
    # Update Log stays byte-identical (fingerprint-pinned). Columns 1-3
    # inherit the sheet's stamp-table widths; Rationale gets room.
    ncols = len(headers)
    _set_widths(ws, {**{c: 22 for c in range(4, ncols)}, ncols: 70})
    return row


def _write_fact_correction_register(
    ws: Worksheet,
    start_row: int,
    entries: list[dict],
    *,
    include_state: bool,
) -> int:
    """The fact-corrections register (issue #249) — who corrected which
    extraction input, why, and whether the overlay has landed yet.

    ``entries`` is ``fact_corrections.resolve_corrections`` output.
    Renders nothing (returns ``start_row``) when empty — workbooks
    without corrections keep their Update Log byte-stable, the
    adjudication-register convention. The ``Status`` column is the
    honesty signal: ``pending re-aggregate`` means the curation sidecar
    holds the correction but the scores sidecar predates it (run
    ``mc score aggregate`` / ``mc publish``).
    """
    if not entries:
        return start_row
    row = start_row + 1
    ws.cell(
        row=row, column=1,
        value="Fact-corrections register (human-corrected extraction "
              "inputs — the rule cascade is unchanged)",
    ).font = Font(bold=True, size=12)
    row += 1
    headers: tuple[str, ...] = (
        "Entity", "Data Element", "Fact", "Correction", "Lens",
        "Status", "Author", "Corrected at", "Rationale",
    )
    if include_state:
        headers = ("State", *headers)
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1
    for entry in sorted(
        entries,
        key=lambda e: (e["state"], e["record_key"], e["fact"]),
    ):
        cells: list[object] = [
            entry.get("entity"),
            entry.get("element_name"),
            entry.get("fact"),
            entry.get("flip"),
            entry.get("lens"),
            entry.get("status"),
            entry.get("author"),
            entry.get("corrected_at"),
            entry.get("rationale"),
        ]
        if include_state:
            cells.insert(0, entry["state"])
        for col_idx, value in enumerate(cells, start=1):
            ws.cell(row=row, column=col_idx, value=value)
        rationale_cell = ws.cell(row=row, column=len(cells))
        rationale_cell.alignment = Alignment(wrap_text=True, vertical="top")
        row += 1
    ncols = len(headers)
    _set_widths(ws, {**{c: 22 for c in range(4, ncols)}, ncols: 70})
    return row


def _write_update_log(
    ws: Worksheet,
    si: StateInputs,
    lens: str,
    *,
    adjudications: dict[str, dict] | None = None,
    fact_corrections: list[dict] | None = None,
) -> None:
    """Update Log — what generated this workbook + the methodology
    version history (Option B; absorbs the former Score Card metadata
    block, relabeled per issue #174), plus the adjudication register
    when any consensus exists (issue #248 Part C) and the
    fact-corrections register when any correction exists (issue
    #249)."""
    from src.score.aggregate import SCORING_PLAN_VERSION
    from src.report.versions import METHODOLOGY_VERSION_HISTORY

    ws.title = "Update Log"
    ws["A1"] = f"Update Log — {si.state}"
    ws["A1"].font = Font(bold=True, size=14)
    lines = [
        ("State", si.state),
        ("Methodology version", f"v{SCORING_PLAN_VERSION}"),
        (
            "Workbook generated",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
        # Honest relabel — the value is the swagger `info.version`, not
        # the Ed-Fi Data Standard number (Sequence-1 lesson).
        ("Ed-Fi API version (swagger info.version)", si.spine.edfi_version),
        ("API model fetched at", si.spine.fetched_at.isoformat()),
        ("API model entity count", si.spine.entity_count),
        ("API model extension count", si.spine.extension_count),
        ("Ingested element count", si.elements.element_count),
        ("Ingested elements extracted at", si.elements.extracted_at.isoformat()),
        ("Source-document coverage", _format_source_coverage(si)),
        ("Ed-Fi API model coverage (of full UDM)", _format_spine_coverage(si)),
        ("Coverage semantics", _COVERAGE_SEMANTICS_NOTE),
        ("Source scope", _SOURCE_SCOPE_BY_STATE.get(si.state, "")),
        ("Source URL (resources)", _display_source_url(si.spine.source_urls.resources)),
        ("Source URL (descriptors)", _display_source_url(si.spine.source_urls.descriptors)),
    ]
    row = 3
    wrapped_labels = {"Source scope", "Coverage semantics"}
    for label, value in lines:
        ws.cell(row=row, column=1, value=label).font = Font(bold=True)
        ws.cell(row=row, column=2, value=value)
        if label in wrapped_labels:
            ws.cell(row=row, column=2).alignment = Alignment(
                wrap_text=True, vertical="top"
            )
            ws.row_dimensions[row].height = 75
        row += 1

    row += 1
    ws.cell(
        row=row, column=1, value="Methodology version history"
    ).font = Font(bold=True, size=12)
    row += 1
    for col_idx, h in enumerate(("Version", "Date", "What changed"), start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1
    # Newest first — the reader wants "what changed since I last looked".
    for entry in reversed(METHODOLOGY_VERSION_HISTORY):
        ws.cell(row=row, column=1, value=f"v{entry.version}")
        ws.cell(row=row, column=2, value=entry.date)
        cell = ws.cell(row=row, column=3, value=entry.summary)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        row += 1
    row = _write_adjudication_register(
        ws, row,
        [(si.state, key, adj) for key, adj in (adjudications or {}).items()],
        include_state=False,
    )
    row = _write_fact_correction_register(
        ws, row, fact_corrections or [], include_state=False
    )
    _set_widths(ws, {1: 38, 2: 90, 3: 110})


def _write_combined_update_log(
    ws: Worksheet,
    state_inputs: list[StateInputs],
    lens: str,
    *,
    adjudications_by_state: dict[str, dict[str, dict]] | None = None,
    fact_corrections_by_state: dict[str, list[dict]] | None = None,
) -> None:
    """Combined-workbook Update Log (issue #213 item 1: `_build_combined`
    never wrote one, which also disabled `curation`'s stale-workbook
    warning — it scans only "Update Log*" sheets for the "Workbook
    generated" stamp — on exactly the workbook where stale-Row#
    confusion is likeliest).

    Same grammar as the per-state sheet: stamps, then per-state input
    freshness as a table (one row per state instead of a label column),
    then the methodology version history.
    """
    from src.score.aggregate import SCORING_PLAN_VERSION
    from src.report.versions import METHODOLOGY_VERSION_HISTORY

    ws.title = "Update Log"
    ws["A1"] = "Update Log — All States Combined"
    ws["A1"].font = Font(bold=True, size=14)
    row = 3
    for label, value in (
        ("Methodology version", f"v{SCORING_PLAN_VERSION}"),
        (
            "Workbook generated",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    ):
        ws.cell(row=row, column=1, value=label).font = Font(bold=True)
        ws.cell(row=row, column=2, value=value)
        row += 1

    row += 1
    ws.cell(
        row=row, column=1, value="Per-state inputs"
    ).font = Font(bold=True, size=12)
    row += 1
    headers = (
        "State",
        # Honest relabel — swagger `info.version`, not the Data
        # Standard number (Sequence-1 lesson).
        "Ed-Fi API version (swagger info.version)",
        "API model fetched at",
        "Ingested element count",
        "Ingested elements extracted at",
        "Source-document coverage",
        "Ed-Fi API model coverage (of full UDM)",
    )
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1
    for si in state_inputs:
        ws.cell(row=row, column=1, value=si.state)
        ws.cell(row=row, column=2, value=si.spine.edfi_version)
        ws.cell(row=row, column=3, value=si.spine.fetched_at.isoformat())
        ws.cell(row=row, column=4, value=si.elements.element_count)
        ws.cell(row=row, column=5, value=si.elements.extracted_at.isoformat())
        ws.cell(row=row, column=6, value=_format_source_coverage(si))
        ws.cell(row=row, column=7, value=_format_spine_coverage(si))
        row += 1

    row += 1
    note = ws.cell(row=row, column=1, value="Coverage semantics")
    note.font = Font(bold=True)
    note_cell = ws.cell(row=row, column=2, value=_COVERAGE_SEMANTICS_NOTE)
    note_cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[row].height = 75
    row += 2

    ws.cell(
        row=row, column=1, value="Methodology version history"
    ).font = Font(bold=True, size=12)
    row += 1
    for col_idx, h in enumerate(("Version", "Date", "What changed"), start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1
    # Newest first — same convention as the per-state sheet.
    for entry in reversed(METHODOLOGY_VERSION_HISTORY):
        ws.cell(row=row, column=1, value=f"v{entry.version}")
        ws.cell(row=row, column=2, value=entry.date)
        cell = ws.cell(row=row, column=3, value=entry.summary)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        row += 1
    # One merged register with a State column (issue #248 design pass).
    register_entries = [
        (state, key, adj)
        for state, adjs in sorted((adjudications_by_state or {}).items())
        for key, adj in adjs.items()
    ]
    row = _write_adjudication_register(
        ws, row, register_entries, include_state=True
    )
    correction_entries = [
        entry
        for _state, entries in sorted(
            (fact_corrections_by_state or {}).items()
        )
        for entry in entries
    ]
    row = _write_fact_correction_register(
        ws, row, correction_entries, include_state=True
    )
    _set_widths(ws, {1: 38, 2: 42, 3: 28, 4: 20, 5: 28, 6: 26, 7: 32})


# Value-group helpers (`_score_fields`, `_integration_profile_fields`,
# `_review_cells`, `_legacy_template_fields`) + display maps moved to
# `workbook_spec` (R2) — re-imported above.


def _details_row(
    r: ElementRecord,
    state: str,
    is_ext: bool,
    edfi_domain: str | None,
    score: dict | None = None,
) -> list:
    """Source-lens comparison-projection row — spec-driven adapter (R2).

    Extracts the `human_score_backfill` projection (Details columns
    minus the analyst-input band) in `_DETAILS_HEADERS` order; this
    signature is a compat surface for `human_score_backfill` (which
    builds its `ai-` cell block by calling this directly).
    """
    ctx = RowContext(
        state=state,
        record=r,
        is_extension=is_ext,
        edfi_domain=edfi_domain,
        score=score,
    )
    return [c.extract(ctx) for c in _backfill_columns("source")]


def _details_row_spine(
    r: ElementRecord,
    state: str,
    is_ext: bool,
    edfi_domain: str | None,
    score: dict | None = None,
) -> list:
    """Spine-lens comparison-projection row — spec-driven adapter (R2)."""
    ctx = RowContext(
        state=state,
        record=r,
        is_extension=is_ext,
        edfi_domain=edfi_domain,
        score=score,
    )
    return [c.extract(ctx) for c in _backfill_columns("spine")]


def _edfi_domain_for(si: StateInputs, entity_name: str) -> str | None:
    """Join the spine entity's Ed-Fi domains for rendering in the workbook.

    Thin delegate to the canonical resolver
    ``ingest.shared.edfi_domain_for_entity`` (issue #184) — the single
    source of truth shared by ingest (which stamps ``edfi_domain`` on every
    record) and this report. Kept as a shim so existing callers
    (``human_score_backfill``, the ``_write_details`` cache builders) need
    no change.
    """
    from src.ingest.shared import edfi_domain_for_entity

    return edfi_domain_for_entity(entity_name, si.spine)


def _element_row_contexts(
    state_inputs: list[StateInputs],
    scores_by_state: dict[str, dict[str, dict]] | None,
    row_numbers: dict[str, int] | None,
    *,
    recs_lookup: dict[str, list[dict]] | None = None,
    scored_only: bool = False,
    curation_by_state: dict[str, dict[str, dict[str, object]]] | None = None,
    adjudications_by_state: dict[str, dict[str, dict]] | None = None,
    human_lookup: dict[str, tuple] | None = None,
    review_flags: dict[str, dict] | None = None,
) -> list[RowContext]:
    """One canonical per-element row stream for every spec-rendered sheet.

    Flattens all records across ``state_inputs``, applies the canonical
    shared sort (documented rows first, blank Source Area last — the
    order Reviewer View / Scoring Summary / Audit Trail agree on), joins
    the score sidecar by ``record_key``, and resolves the Ed-Fi Domain.

    Issue #184: the Ed-Fi Domain column reads the stored ``edfi_domain``
    field (stamped at ingest by the canonical resolver) — no on-the-fly
    recompute, so there is a single source of truth. Records that predate
    the field (None) fall back to the same canonical resolver via the
    ``_edfi_domain_for`` shim, so the value is identical either way;
    production records are always stamped.

    ``scored_only=True`` skips unscored rows (Scoring Summary) — the gaps
    that leaves in the ``Row #`` sequence are the cross-sheet address
    feature, not a bug.
    """
    si_by_state = {si.state: si for si in state_inputs}
    pairs: list[tuple[str, ElementRecord]] = []
    for si in state_inputs:
        for r in si.elements.elements:
            pairs.append((si.state, r))
    # Canonical shared sort — documented rows first, blank Source Area last.
    pairs.sort(key=lambda t: _canonical_sort_key(t[0], t[1]))
    contexts: list[RowContext] = []
    for state, record in pairs:
        key = _record_key(state, record)
        score: dict | None = None
        if scores_by_state is not None:
            score = scores_by_state.get(state, {}).get(key)
        if scored_only and score is None:
            continue
        ed_domain = record.edfi_domain
        if ed_domain is None and state in si_by_state:
            ed_domain = _edfi_domain_for(si_by_state[state], record.entity)
        contexts.append(
            RowContext(
                state=state,
                record=record,
                is_extension=_is_extension_record(record),
                edfi_domain=ed_domain,
                score=score,
                recs=(recs_lookup or {}).get(key, []),
                row_number=(row_numbers or {}).get(key),
                # Option C round-trip — ingested analyst-input values
                # re-applied into the band on every regeneration.
                curation=(curation_by_state or {}).get(state, {}).get(key),
                # Issue #248 Part C — resolved adjudication for the
                # Effective Score column (fresh renders, stale blanks).
                adjudication=(
                    (adjudications_by_state or {}).get(state, {}).get(key)
                ),
                # `--with-human`: joined human row (Cmp: columns only).
                human=(
                    HumanCells(*human_lookup[key])
                    if human_lookup and key in human_lookup
                    else None
                ),
                # Issue #259 PR 2 — the record's merged queue signal
                # for the `AI: Review Why` / `AI: Review Priority`
                # columns (same entries the queue renders).
                review_flag=(review_flags or {}).get(key),
            )
        )
    return contexts


def _recs_lookup(recs_by_state: dict[str, list[dict]] | None) -> dict[str, list[dict]]:
    """Recommendations rows keyed by ``record_key`` for O(1) per-row joins."""
    lookup: dict[str, list[dict]] = {}
    for _state, state_recs in (recs_by_state or {}).items():
        for r in state_recs:
            lookup.setdefault(r["record_key"], []).append(r)
    return lookup


def _write_reference_sheet(ws: Worksheet, state_inputs: list[StateInputs]) -> None:
    """FK / cross-entity reference inventory — one row per (entity,
    reference) pair in the spine catalog. Row source only; columns live
    in ``workbook_spec.REFERENCE_COLUMNS``."""
    rows: list[dict] = []
    for si in state_inputs:
        core_entity_names = set(si.spine.catalog.entities.keys())
        # Entity name -> list of domain labels (first domain when multi).
        domain_for_entity: dict[str, str] = {}
        for ename, ent in si.spine.catalog.entities.items():
            domain_for_entity[ename] = (ent.domains[0] if ent.domains else "")
        for ename in sorted(core_entity_names):
            ent = si.spine.catalog.entities[ename]
            has_ext = si.spine.catalog.has_state_extension(ename)
            observations = (
                f"{si.state} extension defined by state — review for deviation"
                if has_ext
                else "No deviation from Ed-Fi Model found for the references used."
            )
            for ref_name, ref in ent.references.items():
                rows.append({
                    "domain": domain_for_entity.get(ename) or "",
                    "entity": ename,
                    "reference": ref_name,
                    "in_swagger": "Yes" if ref.entity in core_entity_names else "No",
                    "observations": observations,
                })
    render_table_sheet(ws, REFERENCE_SHEET, rows, "source")


# Sheet-title → (what it shows, who it's for). Guide prose lives on the
# unified `workbook_spec.SHEET_SPECS` registry (R2); this derived view
# keeps the Sequence-1 name importable. `_write_readme_sheet` raises
# KeyError for a sheet title missing from the registry, so a new sheet
# cannot ship undocumented (build-time non-drift guard).
_SHEET_GUIDE: dict[str, tuple[str, str]] = {
    t: s.guide for t, s in SHEET_SPECS.items()
}


def _legend_section(
    ws: Worksheet,
    row: int,
    title: str,
    entries: list[tuple],
    headers: tuple[str, ...] = ("Value", "Meaning"),
) -> int:
    """Write one titled mini-table; return the next free row (1 blank gap)."""
    ws.cell(row=row, column=1, value=title).font = Font(bold=True, size=12)
    row += 1
    for col_idx, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    row += 1
    for entry in entries:
        for col_idx, v in enumerate(entry, start=1):
            _set_cell(ws, row, col_idx, v)
        ws.cell(row=row, column=len(entry)).alignment = Alignment(
            wrap_text=True, vertical="top"
        )
        row += 1
    return row + 1


def _write_legend_sheet(ws: Worksheet, lens: str) -> None:
    """In-book glossary (Sequence-1 hygiene) — Indiana's hand-built
    workbooks embed the scoring rubric on a sheet; the generated
    workbooks never did, leaving tiers, adjustment tokens, Match Status,
    and confidence values unexplained (a documented trust-eroder).

    Every KEY column below iterates imported constants (`score.rubric`,
    `score.schema`, `report.review_queue`, this module's display maps);
    only the meaning prose lives in `score/rubric.py`. Non-drift tests in
    `tests/test_score_rubric.py` keep prose and constants in lockstep, so
    the Legend cannot silently fall behind the methodology.
    """
    from src.report.review_queue import ROUTE_DESCRIPTIONS, ROUTES
    from src.score import rubric
    from src.score.schema import ENUM_VALUED_FACTS

    ws.title = "Legend"
    ws["A1"] = "Legend — scoring vocabulary"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = (
        "Generated from the scoring rule/schema constants — this sheet "
        "cannot drift from the methodology that produced the scores."
    )
    row = 4

    row = _legend_section(
        ws, row, "NACHOS complexity tiers (NACHOS score, 0–3)",
        [(tier, meaning) for tier, meaning in rubric.NACHOS_TIER_MEANINGS],
        headers=("Tier", "Meaning"),
    )
    row = _legend_section(
        ws, row, "Score adjustments (Adjusted NACHOS Score = tier + adjustments)",
        list(rubric.ADJUSTMENT_MEANINGS.items()),
        headers=("Adjustment", "Meaning"),
    )
    note = ws.cell(row=row - 1, column=1, value=f"Note: {rubric.ADJUSTMENT_NOTE}")
    note.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[row - 1].height = 60
    row += 1

    dim_order = _SPINE_DIM_ORDER if lens == "spine" else _SOURCE_DIM_ORDER
    dim_rows: list[tuple] = []
    for dim in dim_order:
        if dim == "nachos_score":
            continue
        for tier, meaning in rubric.DIMENSION_TIER_MEANINGS[dim]:
            dim_rows.append((dim, tier, meaning))
    row = _legend_section(
        ws, row, "Quality-dimension tiers (Scoring Summary / Audit Trail)",
        dim_rows, headers=("Dimension", "Tier", "Meaning"),
    )

    row = _legend_section(
        ws, row, "Match Status",
        [(v, rubric.MATCH_STATUS_MEANINGS[v]) for v in _MATCH_STATUS_BY_SOURCE.values()],
    )
    row = _legend_section(
        ws, row, "Documentation Source",
        [(v, rubric.DOC_SOURCE_MEANINGS[v]) for v in _DOC_SOURCE_DISPLAY.values()],
    )
    # NACHOS Score Context (formerly "Integration Profile", issue #174)
    # — the contextual labels beside the NACHOS scalar.
    row = _legend_section(
        ws, row,
        "NACHOS Score Context — Implementation Shape "
        "(formerly Structural Depth)",
        [(v, rubric.STRUCTURAL_DEPTH_MEANINGS[v]) for v in _STRUCTURAL_DEPTH_LABELS.values()],
    )
    row = _legend_section(
        ws, row, "NACHOS Score Context — Documentation Style",
        [
            (v, rubric.DOC_STYLE_MEANINGS[v])
            # dict.fromkeys: the display map is many-to-one safe (dedup).
            for v in dict.fromkeys(_DOC_STYLE_DISPLAY.values())
        ],
    )
    row = _legend_section(
        ws, row, "Confidence",
        list(rubric.CONFIDENCE_MEANINGS.items()),
    )
    row = _legend_section(
        ws, row, "Review routes (Needs Review = Yes)",
        [(rt, ROUTE_DESCRIPTIONS[rt]) for rt in ROUTES],
        headers=("Route", "Meaning"),
    )
    row = _legend_section(
        ws, row, "Rule paths (Audit Trail `nachos_tier_rule` / rule columns)",
        sorted(rubric.RULE_MEANINGS.items()),
        headers=("Rule", "Meaning"),
    )
    row = _legend_section(
        ws, row, "Audit Trail enum facts (allowed values)",
        [(fact, ", ".join(values)) for fact, values in ENUM_VALUED_FACTS.items()],
        headers=("Fact", "Allowed values"),
    )
    # Issue #174 — generated former-name mapping so artifacts produced
    # before 2026-07 stay traceable against the new labels.
    from src.report.workbook_spec import TERMINOLOGY_FORMER_NAMES

    # Column range + behavior prose both derived (issue #211 item 1c):
    # the range from the spec's analyst_input role, the prose from the
    # same constant that renders as the band header-cell comment — the
    # Legend can no longer contradict the shipped round-trip behavior.
    band_headers = [c.header for c in DETAILS_COLUMNS if c.role == "analyst_input"]
    row = _legend_section(
        ws, row, "Analyst-input columns (Details, green headers)",
        [(
            f"{band_headers[0]} … {band_headers[-1]}",
            ANALYST_BAND_NOTE
            + " The `AI:` prefix marks pipeline-produced columns.",
        )],
        headers=("Columns", "Meaning"),
    )
    _legend_section(
        ws, row, "Terminology — former names (pre-2026-07 artifacts)",
        [(cur, f"{former} — {note}") for cur, former, note in TERMINOLOGY_FORMER_NAMES],
        headers=("Current term", "Former name"),
    )
    _set_widths(ws, {1: 42, 2: 90, 3: 90})


def _sheet_guide_entry(title: str) -> tuple[str, str]:
    """Guide prose for a sheet title; suffix rule covers the per-state
    spine-lens "{ST} — Documented only" family. KeyError on an unknown
    title is deliberate — see `_SHEET_GUIDE`."""
    if title.endswith("— Documented only"):
        return (
            "Reviewer View, filtered to rows the state's source actually "
            "documents (excludes hybrid-append spine-missing rows). `Row #` "
            "matches the unfiltered Reviewer View. Spine-lens per-state "
            "only.",
            "Reviewers",
        )
    return _SHEET_GUIDE[title]


# "Working a review pass" — zero-code workflow guidance rendered below
# the Readme sheet table (issue #259 PR 1, option F). Lives here rather
# than workbook_spec: the Readme is a kind="block" sheet with no column
# contract, and this prose is workflow guidance, not a data contract.
_README_REVIEW_PASS_TIPS: tuple[tuple[str, str], ...] = (
    (
        "Two windows",
        "View → New Window, then Arrange All: keep the Review Queue in "
        "one window and Details in the other. Hyperlinks jump only in "
        "the active window, so your place on the queue never moves.",
    ),
    (
        "Jump back",
        "After following a Row # link, press F5 then Enter — Go To "
        "remembers the cell you came from, so this returns you to your "
        "place on the queue.",
    ),
    (
        "Where a link lands",
        "The queue's Where column names the sheet a Row # link opens: "
        "the Details block sorts first, then Documentation Gaps, then "
        "records no longer in the workbook (no link).",
    ),
)


def _write_readme_sheet(ws: Worksheet, wb: Workbook) -> None:
    """Self-documenting Readme tab — sheet-by-sheet guide at workbook
    position 1, generated from the sheets actually present, followed by
    the "Working a review pass" tips block (issue #259).

    Called LAST by the builders (after every other sheet exists) with the
    position-1 placeholder worksheet, so the listing can never drift from
    the real tab inventory.
    """
    ws.title = "Readme"
    for col_idx, h in enumerate(_README_HEADERS, start=1):
        c = ws.cell(row=1, column=col_idx, value=h)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    for row_idx, title in enumerate(wb.sheetnames, start=2):
        desc, audience = _sheet_guide_entry(title)
        _set_cell(ws, row_idx, 1, title)
        _set_cell(ws, row_idx, 2, desc)
        _set_cell(ws, row_idx, 3, audience)
        ws.cell(row=row_idx, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    # Tips block — one blank spacer row after the sheet table.
    tips_header_row = len(wb.sheetnames) + 3
    for col_idx in range(1, len(_README_HEADERS) + 1):
        c = ws.cell(row=tips_header_row, column=col_idx)
        c.font = _HEADER_FONT
        c.fill = _HEADER_FILL
    ws.cell(row=tips_header_row, column=1, value="Working a review pass")
    for offset, (tip, prose) in enumerate(_README_REVIEW_PASS_TIPS, start=1):
        r = tips_header_row + offset
        _set_cell(ws, r, 1, tip)
        _set_cell(ws, r, 2, prose)
        ws.cell(row=r, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    _set_widths(ws, {1: 28, 2: 80, 3: 32})


def _write_entities_by_domain(ws: Worksheet, state_inputs: list[StateInputs]) -> None:
    """Ed-Fi API model catalog grouped by domain, in the analysts'
    grouped-list grammar: documented-element counts per entity, a bold
    subtotal row per domain, and a Grand Total. "(unassigned)" sorts
    LAST (Sequence-1 hygiene).
    """
    pairs: set[tuple[str, str]] = set()
    documented_counts: dict[str, int] = {}
    for si in state_inputs:
        for r in si.elements.elements:
            if r.documented:
                documented_counts[r.entity] = documented_counts.get(r.entity, 0) + 1
        for ename, ent in si.spine.catalog.entities.items():
            if ent.domains:
                for dom in ent.domains:
                    pairs.add((dom, ename))
            else:
                pairs.add(("(unassigned)", ename))
        # Extensions inherit the target entity's domain assignment implicitly.
    by_domain: dict[str, list[str]] = {}
    for dom, ent in pairs:
        by_domain.setdefault(dom, []).append(ent)

    rows: list[dict] = []
    grand_entities = 0
    grand_documented = 0
    for dom in sorted(by_domain, key=lambda d: (d == "(unassigned)", d)):
        entities = sorted(by_domain[dom])
        dom_documented = 0
        for ent in entities:
            count = documented_counts.get(ent, 0)
            dom_documented += count
            rows.append({
                "domain": dom,
                "entity": ent,
                "documented_elements": count or None,
            })
        rows.append({
            "domain": f"Total — {dom}",
            "entity": f"{len(entities)} entities",
            "documented_elements": dom_documented,
            "bold": True,
        })
        grand_entities += len(entities)
        grand_documented += dom_documented
    rows.append({
        "domain": "Grand Total",
        "entity": f"{grand_entities} entities",
        "documented_elements": grand_documented,
        "bold": True,
    })

    # Subtotal/Grand-Total rows carry their own `bold` marker — the
    # renderer's `row_font` hook styles them (issue #213 item 1: the old
    # remember-sheet-rows-then-sweep ladder re-derived row offsets by hand).
    render_table_sheet(
        ws, ENTITIES_BY_DOMAIN_SHEET, rows, "source",
        row_font=lambda row: _BOLD_FONT if row.get("bold") else None,
    )


def _review_queue_entries(
    scores_by_state: dict[str, dict[str, dict]],
    *,
    row_numbers: dict[str, int] | None = None,
    lens: str = "source",
) -> list[tuple]:
    """Assemble the Review Queue's flagged rows — unsorted, position-free.

    Entry shape: ``(priority, state, entity, element_name, payload)``.
    ``priority`` is the ladder sort key (-2 RE-ADJUDICATE, -1 OVERRIDE,
    then the ``review_queue.ROUTES`` indices, 99 unrouted); payloads
    carry ``record_key`` so callers can group rows per record. Where a
    row's Row # link lands (Details vs Documentation Gaps) is
    deliberately NOT decided here — that is the sheet writer's
    positional concern (issue #259).

    Analyst-override disagreements carry the workbook-only ``OVERRIDE``
    route label (§8.4: the engine score is never modified — the
    disagreement itself is the review signal). ``OVERRIDE`` is
    deliberately NOT added to ``review_queue.ROUTES``: that tuple drives
    the ``review_queue_{lens}.json`` artifacts, which stay
    curation-blind. Then the cascade's flagged rows on the POLICY /
    DATA_MODEL / SCORING / ANALYST priority ladder.

    A record can appear more than once — up to one OVERRIDE row per
    contested override axis (adjusted and/or base, issue #250: each
    disagreement keeps its own unambiguous why-text) plus one row under
    its cascade route (needs_review) — deliberately: the rows answer
    different questions ("the analyst disagrees on this layer" vs "the
    engine wants review") and collapsing them would hide one signal
    behind another (issue #213 item 1: documented rather than deduped).

    Issue #248 Part C: a FRESH adjudication resolves its record's
    ADJUSTED-axis disagreement, so that OVERRIDE row is suppressed
    (base-axis rows are untouched — adjudication is adjusted-only).
    STALE adjudications (engine or plan moved since the decision)
    become ``RE-ADJUDICATE`` rows (priority -2): a previously DECIDED
    row whose ground shifted is the queue's most actionable item.
    ``RE-ADJUDICATE`` is workbook-only like ``OVERRIDE`` — never part
    of ``review_queue.ROUTES``.
    """
    # Local imports avoid circular dependencies: report.review_queue and
    # report.curation import src.utils.paths; analyst imports neither
    # at module import time.
    from src.report.curation import (
        adjudications_for,
        override_disagreements_for,
    )
    from src.report.review_queue import ROUTES, route_review
    from src.report.workbook_spec import evidence_from_score

    route_order = {name: idx for idx, name in enumerate(ROUTES)}
    flagged: list[tuple] = []
    # OVERRIDE rows first (sort key -1, ahead of the ROUTES ladder).
    # Loaded straight from the curation sidecar (not the threaded flat
    # view) because the same-lens gate needs the captured-lens
    # provenance the flat view drops. One row per contested axis; the
    # queue's `Adjusted NACHOS Score` column always shows the engine
    # adjusted value regardless of axis (a base-axis row's tier numbers
    # live in the why-text, never in the adjusted column).
    for state, scores in sorted(scores_by_state.items()):
        adjudications = adjudications_for(state, scores, lens)
        for d in override_disagreements_for(state, scores, lens):
            adj = adjudications.get(d["record_key"])
            if d["axis"] == "adjusted" and adj and adj["fresh"]:
                # Consensus resolved this disagreement (issue #248).
                continue
            score = scores.get(d["record_key"]) or {}
            flagged.append((
                -1,
                state,
                d["entity"] or "",
                d["element_name"] or "",
                {
                    "route": "OVERRIDE",
                    "record_key": d["record_key"],
                    "state": state,
                    "entity": d["entity"],
                    "element_name": d["element_name"],
                    "adjusted": score.get("adjusted_nachos_score"),
                    "confidence_composite": score.get("confidence_composite"),
                    "why": d["why"],
                    "evidence": evidence_from_score(score),
                    "row_number": (row_numbers or {}).get(d["record_key"]),
                },
            ))
        # Stale adjudications — RE-ADJUDICATE rows at the very top. A
        # record whose sidecar entry disappeared still renders (engine
        # cells blank, no Row # link): the decision stays auditable.
        for key, adj in sorted(adjudications.items()):
            if adj["fresh"]:
                continue
            score = scores.get(key) or {}
            flagged.append((
                -2,
                state,
                adj["entity"] or "",
                adj["element_name"] or "",
                {
                    "route": "RE-ADJUDICATE",
                    "record_key": key,
                    "state": state,
                    "entity": adj["entity"],
                    "element_name": adj["element_name"],
                    "adjusted": score.get("adjusted_nachos_score"),
                    "confidence_composite": score.get("confidence_composite"),
                    "why": (
                        f"adjudicated {adj['value']:g} "
                        f"(plan v{adj['plan_version_at_decision']}) is "
                        f"stale — {adj['stale_reason']}; re-adjudicate"
                    ),
                    "evidence": evidence_from_score(score),
                    "row_number": (row_numbers or {}).get(key),
                },
            ))
    for state, scores in scores_by_state.items():
        for key, score in scores.items():
            review = score.get("review", {}) or {}
            if not review.get("needs_review"):
                continue
            reasons = list(review.get("reasons", []) or [])
            route = review.get("route") or route_review(reasons)
            flagged.append((
                route_order.get(route, 99),
                state,
                score.get("entity", ""),
                score.get("element_name", ""),
                {
                    "route": route,
                    "record_key": key,
                    "state": state,
                    "entity": score.get("entity"),
                    "element_name": score.get("element_name"),
                    "adjusted": score.get("adjusted_nachos_score"),
                    "confidence_composite": score.get("confidence_composite"),
                    "why": "; ".join(reasons) or None,
                    "evidence": evidence_from_score(score),
                    "row_number": (row_numbers or {}).get(key),
                },
            ))
    return flagged


# Internal ladder priority → the rendered 1-6 scale on the Details
# `AI: Review Priority` column (issue #259 PR 2). 99 (unrouted) maps to
# nothing: the row renders on the queue but has no ladder position.
_QUEUE_PRIORITY_RENDER = {-2: 1, -1: 2, 0: 3, 1: 4, 2: 5, 3: 6}


def _review_flags(entries: list[tuple]) -> dict[str, dict]:
    """Merge queue entries per record for the Details review columns
    (issue #259 PR 2): ``priority`` = the record's best ladder position
    (rendered 1-6; None when every row is unrouted), ``why`` = the
    record's why-texts joined '; ' in ladder order. Built from the SAME
    entries the queue renders, so the two surfaces cannot diverge —
    including the #248 fresh-adjudication suppression, which already
    happened upstream in ``_review_queue_entries``.
    """
    by_key: dict[str, list[tuple]] = {}
    for entry in sorted(entries, key=lambda t: t[:4]):
        key = entry[4].get("record_key")
        if key:
            by_key.setdefault(key, []).append(entry)
    flags: dict[str, dict] = {}
    for key, rows in by_key.items():
        whys = [r[4]["why"] for r in rows if r[4].get("why")]
        flags[key] = {
            "priority": _QUEUE_PRIORITY_RENDER.get(min(r[0] for r in rows)),
            "why": "; ".join(whys) or None,
        }
    return flags


def _queue_where(
    row_number: int | None,
    details_positions: dict[int, int],
    gaps_positions: dict[int, int],
) -> tuple[int, str]:
    """Classify where a queue row's Row # link lands (issue #259 PR 1).

    Membership in the RENDERED position maps is the whole test — a
    record dropped from the score sidecar but still present in the
    elements artifact has a live Details row, so classifying by
    sidecar absence would lie about where the link goes. ``(record
    gone)`` therefore means "on no rendered sheet" (row_number is None
    or in neither map); those rows carry no link.
    """
    if row_number in details_positions:
        return 0, "Details"
    if row_number in gaps_positions:
        return 1, "Documentation Gaps"
    return 2, "(record gone)"


def _write_review_queue_sheet(
    ws: Worksheet,
    entries: list[tuple],
    *,
    details_positions: dict[int, int] | None = None,
    gaps_positions: dict[int, int] | None = None,
    lens: str = "source",
) -> None:
    """The "Review Queue" front sheet — what needs a human (Option C;
    formerly combined-only, mid-workbook).

    Takes ``_review_queue_entries`` output and renders it
    location-first (issue #259 PR 1): rows whose Row # link lands on
    Details (the scalar-score follow-ups — the pass's real work) sort
    ahead of the Documentation Gaps block, with record-gone stubs last
    instead of sharing RE-ADJUDICATE's top tier. Within each location
    group the ladder holds: RE-ADJUDICATE, OVERRIDE, then the four
    cascade routes. The ``Where`` column names the link target so a
    jump is never a surprise.

    Renders even when nothing is flagged — an honestly-empty queue
    beats a missing tab. Row # carries the target row's address
    (hyperlinked by the caller via ``apply_details_row_links``, one
    pass per target sheet).
    """
    details_positions = details_positions or {}
    gaps_positions = gaps_positions or {}
    ordered: list[tuple] = []
    for priority, state, entity, element, payload in entries:
        loc_group, where = _queue_where(
            payload.get("row_number"), details_positions, gaps_positions
        )
        payload["where"] = where
        ordered.append((loc_group, priority, state, entity, element, payload))
    ordered.sort(key=lambda t: t[:5])
    render_table_sheet(
        ws, REVIEW_QUEUE_SHEET, [entry for *_sort, entry in ordered], lens
    )


def _write_commitment_tracker(
    ws: Worksheet,
    state: str,
    recs_rows: list[dict],
    scores: dict[str, dict],
    element_contexts: list[RowContext],
    row_numbers: dict[str, int] | None,
    *,
    curation_for_state: dict[str, dict[str, object]] | None = None,
) -> None:
    """Commitment Tracker — the analysts' remediation ledger, generated.

    One row per element with a cost-axis (``nachos_score``)
    recommendation. "NACHOS Points Resolved" v1 is the honest
    deterministic claim: adopting the recommendation removes the row's
    BASE-tier points (``points = current_tier``,
    ``projected = adjusted − points`` floored at 0) — score adjustments
    (e.g. ``+0.5 necessary_ext``) deliberately remain, and the Reason
    cell says so on every row. Quality-axis recommendations list in
    "Other recommendations" and contribute 0 points.

    Per-entity bold subtotal rows; a Grand Total row carries the
    what-if: recomputing the Score Card headline population's mean
    adjusted with every projected value applied. Analyst commitment
    columns (Adoption Timeline / Commitment Status / Comments) re-apply
    from the curation sidecar like the Details band.
    """
    from src.score.rubric import NACHOS_RULE_SHORT, render_adjustments_prose

    by_key: dict[str, list[dict]] = {}
    for r in recs_rows:
        if r.get("record_key"):
            by_key.setdefault(r["record_key"], []).append(r)

    tracker_rows: list[dict] = []
    projected_by_key: dict[str, float] = {}
    for key, recs in by_key.items():
        cost = [r for r in recs if r.get("dimension") == "nachos_score"]
        if not cost:
            continue
        rec = cost[0]
        score = scores.get(key) or {}
        adjusted = score.get("adjusted_nachos_score")
        base_tier = rec.get("current_tier")
        points = base_tier if isinstance(base_tier, int) else 0
        projected: float | None = None
        if isinstance(adjusted, (int, float)):
            projected = max(0.0, round(float(adjusted) - points, 2))
            projected_by_key[key] = projected
        rule = (
            (score.get("dimensions") or {}).get("nachos_score") or {}
        ).get("rule_matched")
        reason = f"removes base tier {points}"
        if rule:
            reason += f" ({NACHOS_RULE_SHORT.get(rule, rule)})"
        adj_prose = render_adjustments_prose(score.get("nachos_justification"))
        if adj_prose:
            reason += f"; remains: {adj_prose}"
        other = "; ".join(
            f"{DIMENSION_DISPLAY.get(r['dimension'], r['dimension'])}"
            f" ({r.get('impact'):+d})"
            for r in recs
            if r.get("dimension") != "nachos_score"
            and isinstance(r.get("impact"), int)
        ) or None
        cur = (curation_for_state or {}).get(key) or {}
        tracker_rows.append({
            "row_number": (row_numbers or {}).get(key),
            "state": state,
            "entity": rec.get("entity") or score.get("entity") or "",
            "element": rec.get("element") or score.get("element_name") or "",
            "adjusted": adjusted,
            "action": rec.get("recommendation"),
            "points": points,
            "projected": projected,
            "reason": reason,
            "other": other,
            "adoption_timeline": cur.get("adoption_timeline"),
            "commitment_status": cur.get("commitment_status"),
            "commitment_comments": cur.get("commitment_comments"),
        })
    tracker_rows.sort(key=lambda r: (r["entity"], r["element"]))

    # Interleave per-entity subtotal rows — each carries its own `bold`
    # marker for the renderer's `row_font` hook (issue #213 item 1: the
    # old remember-sheet-rows-then-sweep ladder re-derived offsets by hand).
    rows_out: list[dict] = []
    total_points = 0

    def _flush_subtotal(entity: str, points_sum: int) -> None:
        rows_out.append({"entity": f"{entity} — subtotal",
                         "points": points_sum, "bold": True})

    current_entity: str | None = None
    entity_points = 0
    for r in tracker_rows:
        if current_entity is not None and r["entity"] != current_entity:
            _flush_subtotal(current_entity, entity_points)
            entity_points = 0
        current_entity = r["entity"]
        entity_points += r["points"]
        total_points += r["points"]
        rows_out.append(r)
    if current_entity is not None:
        _flush_subtotal(current_entity, entity_points)

    # Grand Total what-if over the Score Card headline population
    # (documented, scored, in-scope) — the SAME shared predicate the
    # Score Card headline uses (`score_card.record_in_scope`), so the
    # two sheets structurally can't disagree (issue #213 item 1: this
    # site used `is not False`, the headline `get(..., True)` — same
    # answer on production rows, but an unpinned invariant).
    population = [
        c for c in element_contexts
        if c.state == state
        and c.record.documented
        and c.score is not None
        and _record_in_scope(c.score)
    ]
    sentence = None
    if population:
        def _adj(c: RowContext) -> float:
            v = (c.score or {}).get("adjusted_nachos_score")
            return float(v) if isinstance(v, (int, float)) else 0.0

        before = sum(_adj(c) for c in population) / len(population)
        after = sum(
            projected_by_key.get(_record_key(c.state, c.record), _adj(c))
            for c in population
        ) / len(population)
        sentence = (
            f"Adopting all {len(tracker_rows)} recommendations moves "
            f"{state} mean adjusted {before:.2f} → {after:.2f} "
            f"(documented scored in-scope population, n={len(population)})"
        )
    rows_out.append({
        "entity": "Grand Total",
        "points": total_points,
        "reason": sentence,
        "bold": True,
    })

    render_table_sheet(
        ws, COMMITMENT_TRACKER_SHEET, rows_out, "source",
        row_font=lambda row: _BOLD_FONT if row.get("bold") else None,
    )


# `_SPINE_SCORES_HEADERS` / `_SOURCE_SCORES_HEADERS` deleted (issue #213
# item 1 dead surface) — they survived only for tests, which now read
# `SCORING_SUMMARY_SHEET.headers_for(lens)` directly.


# `_summarize_record_recommendations` moved to `workbook_spec` (R2) —
# re-imported above.


# `_write_scores_sheet` replaced by the generic spec renderer (R2):
# `render_table_sheet(..., SCORING_SUMMARY_SHEET, ...)` over
# `_element_row_contexts(..., scored_only=True)`.


# --- Spine Gap sheet (issue #73 Step 1) ----------------------------------
#
# One row per spine-anchored gap record carrying its NACHOS-shape score.
# Step 1 ships deterministic-only structural columns lit up; LLM-dependent
# columns land at tier 0 / low confidence and re-light when Step 2/3
# extracts populate the corresponding fact artifacts. Sheet sits at the
# end of the spine workbook so it does not disturb the column-shape
# contract on Reviewer View / Scoring Summary / Audit Trail.

def _write_spine_gap_sheet(
    ws: Worksheet,
    state: str,
    gap_scores: dict[str, dict],
    gap_meta: dict[str, dict],
) -> None:
    """Render every gap-row score under the per-state Spine Gap sheet.

    Sorted by (entity, element_name) so re-runs produce stable diffs.
    Row source only; columns live in ``workbook_spec.SPINE_GAP_COLUMNS``.
    """
    keys_sorted = sorted(
        gap_scores.keys(),
        key=lambda k: (
            gap_scores[k].get("entity") or "",
            gap_scores[k].get("element_name") or "",
        ),
    )
    rows = [
        {"state": state, "score": gap_scores[k], "meta": gap_meta.get(k)}
        for k in keys_sorted
    ]
    render_table_sheet(ws, SPINE_GAP_SHEET, rows, "spine")


_PROVENANCE_BY_DOC_SOURCE = {
    "swagger": "Swagger backfill (entity-level)",
    "swagger_leaf": "Swagger leaf borrow",
}


def _documentation_gap_rows(
    state: str,
    element_contexts: list[RowContext],
    gap_scores: dict[str, dict] | None,
    gap_meta: dict[str, dict] | None,
) -> list[dict]:
    """Rows for the source workbook Documentation Gaps sheet.

    Unifies the two undocumented populations under one (entity,
    element) sort: the spine-anchored gap sidecar (slots the source
    doc never mentions) and the ``documented=False`` swagger-backfill
    rows relocated out of Details (Option D). ``Provenance`` labels
    which is which. Row dicts reuse the SPINE_GAP_COLUMNS accessor
    shape ({state, score, meta}); relocated rows synthesize meta from
    the ElementRecord and reuse their source-lens score when scored.
    """
    keyed: list[tuple[str, str, dict]] = []
    for k, score in (gap_scores or {}).items():
        meta = dict((gap_meta or {}).get(k) or {})
        meta["provenance"] = "API model gap (not in source doc)"
        keyed.append((
            score.get("entity") or "",
            score.get("element_name") or "",
            {"state": state, "score": score, "meta": meta},
        ))
    for c in element_contexts:
        if c.state != state or c.record.documented:
            continue
        score = c.score or {
            "entity": c.record.entity,
            "element_name": c.record.element_name,
        }
        meta = {
            "provenance": _PROVENANCE_BY_DOC_SOURCE.get(
                c.record.documentation_source,
                "Swagger backfill (entity-level)",
            ),
            "spine_data_type": c.record.data_type,
            "spine_extension_name": c.record.extension_name,
        }
        keyed.append((
            c.record.entity or "", c.record.element_name or "",
            {
                "state": state,
                "score": score,
                "meta": meta,
                # Canonical Row # — the queue's link-target key (issue
                # #259 PR 1). Gap-sidecar slots deliberately carry none:
                # they never enter the queue, so a link can't want them.
                "row_number": c.row_number,
            },
        ))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [row for _e, _n, row in keyed]


# --- Track C scorecard integration: Recommendations sheet -----------------
#
# Plan: docs/track-c-scorecard-integration-plan.md §5.1. One row per
# (record × below-target dimension); 15 columns (v10 — `In Scope`
# dropped). The shape is identical across all four states + both lenses
# — multi-state workbooks (combined coverage) just append more rows
# under the same header.

# `_load_review_queue_routes` → `loaders` (re-imported above).


def _write_recommendations_sheet(
    ws: Worksheet,
    state_inputs: list[StateInputs],
    recs_by_state: dict[str, list[dict]],
    lens: str,
    *,
    review_queue_routes: dict[str, str] | None = None,
) -> None:
    """Track C "Recommendations" sheet — one row per (record × below-target
    dimension). Joins Ed-Fi domain (from spine catalog) and review-queue
    route (from ``review_queue_{lens}.json``) at render time; the wrapped
    ``{"rec", "domain", "route"}`` row dicts feed
    ``workbook_spec.RECOMMENDATIONS_COLUMNS``.

    Phase 3 sort: (state, entity, element, -abs(impact), dimension) —
    within each (state, entity, element) group the highest-leverage row
    appears first, dimension is the final tiebreaker for stability.
    """
    routes = review_queue_routes or {}
    si_by_state = {si.state: si for si in state_inputs}

    flat: list[dict] = []
    for _state, rec_rows in recs_by_state.items():
        flat.extend(rec_rows)

    def _abs_impact(r: dict) -> int:
        impact = r.get("impact")
        return abs(impact) if isinstance(impact, int) else 0

    flat.sort(
        key=lambda r: (
            r.get("state") or "",
            r.get("entity") or "",
            r.get("element") or "",
            -_abs_impact(r),
            r.get("dimension") or "",
        )
    )

    domain_cache: dict[tuple[str, str], str | None] = {}
    rows: list[dict] = []
    for r in flat:
        state = r.get("state") or ""
        entity = r.get("entity") or ""
        si = si_by_state.get(state)
        if si is not None:
            cache_key = (state, entity)
            if cache_key not in domain_cache:
                domain_cache[cache_key] = _edfi_domain_for(si, entity)
            domain = domain_cache[cache_key]
        else:
            domain = None
        rows.append({
            "rec": r,
            "domain": domain,
            "route": routes.get(r.get("record_key") or "") or "—",
        })
    render_table_sheet(ws, RECOMMENDATIONS_SHEET, rows, lens)


# Fact ordering is stable per-lens; matches sidecar output so header-column
# alignment across states is consistent. Source-lens surfaces 13 facts,
# spine-lens 15, covering every LLM rule input + the productization +
# observability signals. Deterministic structural-complexity count facts
# (``fk_chain_depth`` / ``reference_fan_out`` / ``sub_collection_depth``
# / ``descriptor_enum_breadth`` / ``entity_extension_footprint``) stay
# off the Audit Trail fact block — the Reviewer View "Structural Depth"
# column summarises them and the raw integers are visual noise without a
# per-bucket rendering story. The ``documentation_style`` classifier is
# appended so the per-element raw label rides alongside the tier on the
# Audit Trail surface.
# Fact/dim order tuples, span headers, and the NACHOS audit block moved
# to `workbook_spec` (R2) — re-imported above.


def _elements_headers(lens: str) -> tuple[str, ...]:
    """Header row for the wide `Audit Trail` sheet — spec-derived (R2).

    Compat shim over ``workbook_spec.audit_trail_columns``: the Reviewer
    View columns (unchanged shape) + per-fact values + companion spans +
    per-dim tiers + the NACHOS methodology block + the review tail.
    """
    return AUDIT_TRAIL_SHEET.headers_for(lens)


# `_format_fact_value` / `_format_fact_spans` / `_synthesize_rule_paths` /
# `_synthesize_downgraded_facts` moved to `workbook_spec`; `_elements_row` +
# `_write_elements_sheet` replaced by the generic spec renderer over
# `AUDIT_TRAIL_SHEET` (R2).




# `_filter_known_limitations` moved to `prose.filter_known_limitations`
# (issue #213 item 1) — re-imported above.


def _write_known_limitations(ws: Worksheet, state: str | None = None) -> None:
    """Static sheet listing known limitations — prevents analysts re-flagging
    things already in the backlog (reviewer 2 §Add a Known Limitations Sheet).

    When `state` is a single state abbreviation, rows are filtered to include
    only shared (`applies_to=all`) + that state's specific rows (Round 2.2,
    analyst reviewer Moffatt #13 — state-specific limitations were leaking
    across all workbooks).
    """
    render_table_sheet(
        ws, KNOWN_LIMITATIONS_SHEET, _filter_known_limitations(state), "source"
    )


# `_load_peer_gap_artifact` → `loaders` (re-imported above).


def _peer_gap_fills_for_state(slots: list[dict], state: str) -> list[dict]:
    """Flatten per-state suggested fills from the slot rows.

    For every slot, emits one row per ``suggested_fills`` entry whose
    ``state`` matches ``state``. Slot-level context (``slot_key``,
    ``confidence``, ``states_present``, ``consensus_concept``) is joined
    onto each row so the output is self-contained for rendering.
    Sorted alphabetically by ``slot_key`` — same slot_key aligns across
    AZ/WI/MN/TX workbooks for side-by-side reading.
    """
    state = state.upper()
    rows: list[dict] = []
    for slot in slots:
        slot_key = slot.get("slot_key") or ""
        confidence = slot.get("confidence") or ""
        states_present = slot.get("states_present") or []
        consensus_concept = slot.get("consensus_concept") or ""
        for fill in slot.get("suggested_fills") or []:
            if (fill.get("state") or "").upper() != state:
                continue
            rows.append({
                "slot_key": slot_key,
                "confidence": confidence,
                "states_present": list(states_present),
                "current_posture": fill.get("current_posture") or "",
                "peer_consensus_format": fill.get("peer_consensus_format") or "",
                "recommended_fill": fill.get("recommended_fill") or "",
                "consensus_concept": consensus_concept,
            })
    rows.sort(key=lambda r: r["slot_key"])
    return rows


def _write_peer_gaps_sheet(ws: Worksheet, fills: list[dict]) -> None:
    """Render the Peer Gaps sheet from pre-flattened per-state fill rows.

    Columns live in ``workbook_spec.PEER_GAPS_COLUMNS``; prose columns
    (peer_consensus_format, recommended_fill, consensus_concept) widen
    and wrap there.
    """
    render_table_sheet(ws, PEER_GAPS_SHEET, fills, "source")


def _build(
    state_inputs: list[StateInputs],
    lens: str = "source",
    *,
    combined: bool,
    scores_by_state: dict[str, dict[str, dict]] | None = None,
    peer_gap_slots: list[dict] | None = None,
    recs_by_state: dict[str, list[dict]] | None = None,
    review_queue_routes: dict[str, str] | None = None,
    gap_scores_by_state: dict[str, dict[str, dict]] | None = None,
    gap_meta_by_state: dict[str, dict[str, dict]] | None = None,
    curation_by_state: dict[str, dict[str, dict[str, object]]] | None = None,
    adjudications_by_state: dict[str, dict[str, dict]] | None = None,
    fact_corrections_by_state: dict[str, list[dict]] | None = None,
    human_lookup: dict[str, tuple] | None = None,
) -> Workbook:
    """ONE workbook assembly for both scopes (issue #213 item 1).

    ``_build_workbook`` / ``_build_combined`` shared ~80% of their
    bodies; the differences are per-scope gates, now explicit inline
    (``combined`` + the per-state-only sheets whose SheetSpec declares
    ``scope={"per_state"}``: Peer Gaps, Commitment Tracker, API Model
    Gaps). Sheet order is the analysts' template order; the Review
    Queue is inserted at physical position 2 regardless of creation
    time, so both scopes end with identical tab layouts to the
    pre-unification builders (proven cell-identical by
    ``scripts/diff_workbooks.py``).
    """
    wb = Workbook()
    # Readme — self-documenting tab guide at workbook position 1 (issue
    # #56 follow-up). The default Workbook() sheet holds the slot; it is
    # POPULATED LAST so the guide lists exactly the sheets this build
    # produced. Do not write into `wb.active` mid-build.
    readme_ws = wb.active
    readme_ws.title = "Readme"
    single_state = len(state_inputs) == 1 and not combined
    state0 = state_inputs[0]
    # Stable Row # index — one canonical-order numbering shared by
    # Details / Extension Details / Scoring Summary / Audit Trail /
    # Documented only. Combined-workbook numbers intentionally differ
    # from per-state ones (cross-state ordering).
    row_numbers = _row_number_index(state_inputs)
    # Update Log — inputs, freshness stamps, methodology version
    # history (Option B; absorbs the former Score Card metadata block).
    # The combined variant tabulates per-state input freshness and
    # carries the "Workbook generated" stamp `mc review ingest`'s
    # stale-workbook warning scans for (issue #213 item 1 — the
    # combined workbook previously had no Update Log at all).
    if combined:
        _write_combined_update_log(
            wb.create_sheet("Update Log"), state_inputs, lens,
            adjudications_by_state=adjudications_by_state,
            fact_corrections_by_state=fact_corrections_by_state,
        )
    else:
        _write_update_log(
            wb.create_sheet("Update Log"), state0, lens,
            adjudications=(adjudications_by_state or {}).get(state0.state),
            fact_corrections=(
                (fact_corrections_by_state or {}).get(state0.state)
            ),
        )
    # Score Card — the analysts' blocks (per-state) or the All-Combined
    # grammar (state table + cross-state frequency + per-state stack).
    if combined:
        _build_combined_score_card(
            wb.create_sheet("Score Card"),
            state_inputs,
            scores_by_state=scores_by_state,
            lens=lens,
            adjudications_by_state=adjudications_by_state,
        )
    else:
        scores_for_state0 = (
            (scores_by_state or {}).get(state0.state) if scores_by_state else None
        )
        _write_score_card(
            wb.create_sheet("Score Card"),
            state0, scores_for_state=scores_for_state0, lens=lens,
            adjudications=(adjudications_by_state or {}).get(state0.state),
        )

    # Remaining sheets follow the analysts' template order (issue #186
    # Option B): work sheets first (Details → Extension Details →
    # Recommendations), then lens-specific views, then the audit /
    # reference block with Legend + Methodology Notes at the end. Each
    # block is gated on the data being present, so missing sidecars /
    # recs / peer gaps simply omit the corresponding sheet without
    # renumbering the rest.
    #
    # Recommendations — what to act on.
    if recs_by_state:
        _write_recommendations_sheet(
            wb.create_sheet("Recommendations"),
            state_inputs,
            recs_by_state,
            lens=lens,
            review_queue_routes=review_queue_routes,
        )
    # Review-queue entries — computed ONCE, before the row contexts:
    # the queue sheet renders them AND the Details `AI: Review Why` /
    # `AI: Review Priority` columns read the per-record merge, so both
    # surfaces derive from one composition (issue #259 PR 2 — lens
    # gating, fresh-adjudication suppression, and sentence templates
    # are never duplicated).
    queue_entries = (
        _review_queue_entries(
            scores_by_state, row_numbers=row_numbers, lens=lens
        )
        if scores_by_state
        else []
    )
    # Details — pinned per-element columns; the public column contract
    # asserted by `tests/test_workbook_spec.py` header snapshots.
    element_contexts = _element_row_contexts(
        state_inputs, scores_by_state, row_numbers,
        recs_lookup=_recs_lookup(recs_by_state),
        curation_by_state=curation_by_state,
        adjudications_by_state=adjudications_by_state,
        human_lookup=human_lookup,
        review_flags=_review_flags(queue_entries),
    )
    details_contexts = _details_stream(element_contexts, lens)
    render_table_sheet(
        wb.create_sheet("Details"),
        # `--with-human` appends the Cmp: block at render time; the
        # sheet keeps the "Details" title so finish/guide resolve as
        # usual and no machine projection is affected.
        details_sheet_with_cmp() if human_lookup else DETAILS_SHEET,
        details_contexts,
        lens,
    )
    # Details rendered-position map (Row # value → sheet row) — link
    # targets for the Review Queue + Commitment Tracker. Computed from
    # the RENDERED order, so the documented-only relocation can never
    # mis-target a link (rows absent from Details simply get no link).
    details_positions = {
        ctx.row_number: i
        for i, ctx in enumerate(details_contexts, start=2)
        if ctx.row_number is not None
    }
    # Documentation Gaps rows — computed BEFORE the queue (issue #259
    # PR 1) so the queue's Where column and gaps-link targets derive
    # from the actually-built rows; the sheet itself renders later in
    # template tab order. Same gate as the render.
    gap_rows: list[dict] = []
    if lens == "source" and (combined or single_state):
        for si in state_inputs:
            gap_rows.extend(
                _documentation_gap_rows(
                    si.state,
                    element_contexts,
                    (gap_scores_by_state or {}).get(si.state),
                    (gap_meta_by_state or {}).get(si.state),
                )
            )
    # Gaps rendered-position map (Row # value → sheet row) — mirrors
    # details_positions. Only the relocated documented=False rows carry
    # a row_number; gap-sidecar slots never enter the queue.
    gaps_positions = {
        row["row_number"]: i
        for i, row in enumerate(gap_rows, start=2)
        if row.get("row_number") is not None
    }
    # Review Queue — the front work sheet, physical position 2 (right
    # after Readme; explicit index): what needs a human, location-first
    # (Details block, then Documentation Gaps, then record-gone stubs),
    # priority-laddered within each block; Row # hyperlinks land on the
    # row's home sheet (issue #259 PR 1). Renders whenever scores
    # exist, even with zero flagged rows (an honestly-empty queue beats
    # a missing tab).
    if scores_by_state:
        queue_ws = wb.create_sheet("Review Queue", 1)
        _write_review_queue_sheet(
            queue_ws,
            queue_entries,
            details_positions=details_positions,
            gaps_positions=gaps_positions,
            lens=lens,
        )
        apply_details_row_links(queue_ws, details_positions)
        if gaps_positions:
            apply_details_row_links(
                queue_ws, gaps_positions, target_sheet="Documentation Gaps"
            )
        # Issue #259 PR 3 — the read-only `Reviewed? (from Details)`
        # progress echo: INDEX/MATCH formulas over the Details band
        # column, written post-render (the one sanctioned formula
        # surface; see `apply_reviewed_echo`).
        apply_reviewed_echo(queue_ws, wb["Details"])
    # Extension Details — one row per extension element; same Row # as
    # Details (Option B; follows the documented-only relocation).
    ext_contexts = [c for c in details_contexts if c.is_extension]
    if ext_contexts:
        render_table_sheet(
            wb.create_sheet("Extension Details"),
            EXTENSION_DETAILS_SHEET,
            ext_contexts,
            lens,
        )
    # Commitment Tracker — the analysts' remediation ledger (per-state
    # only; gated on recommendations + scores being present).
    if recs_by_state and scores_by_state and single_state:
        _st = state0.state
        _recs_rows = recs_by_state.get(_st) or []
        _scores_st = scores_by_state.get(_st) or {}
        if _recs_rows and _scores_st:
            tracker_ws = wb.create_sheet("Commitment Tracker")
            _write_commitment_tracker(
                tracker_ws,
                _st,
                _recs_rows,
                _scores_st,
                element_contexts,
                row_numbers,
                curation_for_state=(curation_by_state or {}).get(_st),
            )
            apply_details_row_links(tracker_ws, details_positions)
    # Documentation Gaps (Option D, source lens) — the spine signal +
    # the relocated undocumented rows, one honest coverage sheet.
    # Combined stacks all states' rows; per-state renders its own.
    # (Rows computed above, pre-queue — issue #259 PR 1.)
    if gap_rows:
        render_table_sheet(
            wb.create_sheet("Documentation Gaps"),
            DOCUMENTATION_GAPS_SHEET,
            gap_rows,
            lens,
        )
    # Scoring Summary — per-record dimension detail; API-model-lens
    # per-state workbooks only (retired from source per Option B and
    # from the combined deliverable per Option D).
    if scores_by_state and lens == "spine" and not combined:
        render_table_sheet(
            wb.create_sheet("Scoring Summary"),
            SCORING_SUMMARY_SHEET,
            _element_row_contexts(
                state_inputs,
                scores_by_state,
                row_numbers,
                recs_lookup=_recs_lookup(recs_by_state),
                scored_only=True,
            ),
            lens,
        )
    # {state} — Documented only (API-model lens, per-state workbooks):
    # mirrors the Details column layout, rows filtered to
    # `documented=True AND source != 'unknown'`. The `unknown` carve-out
    # keeps hybrid-append rows (spine_missing) out of the view.
    if lens == "spine" and not combined:
        for si in state_inputs:
            render_table_sheet(
                wb.create_sheet(f"{si.state} — Documented only"),
                DETAILS_SHEET,
                [
                    ctx
                    for ctx in element_contexts
                    if ctx.state == si.state
                    and ctx.record.documented
                    and ctx.record.source != "unknown"
                ],
                "spine",
                # Workbook-level index: filtered rows keep the Row # they
                # carry on the unfiltered Reviewer View.
                title=f"{si.state} — Documented only",
            )
    # Peer Gaps — cross-state context (per-state workbooks only).
    if peer_gap_slots and single_state:
        fills = _peer_gap_fills_for_state(peer_gap_slots, state0.state)
        if fills:
            _write_peer_gaps_sheet(wb.create_sheet("Peer Gaps"), fills)
    # Audit Trail removed from the deliverable workbook (Option D) —
    # the full audit surface is one command away: `mc report audit
    # --state {ST} [--lens spine]` (report/audit.py). Row # stays the
    # shared cross-workbook address.
    # API Model Gaps (spine-lens per-state workbooks only) — issue #73
    # Step 1. Renders the spine-anchored gap sidecar so analysts can
    # navigate the structural surface of rows the state's source doc
    # is silent on. Sheet appears only when both gap artifacts are
    # available for the state; otherwise omitted without renumbering.
    if (
        lens == "spine"
        and single_state
        and gap_scores_by_state
        and gap_meta_by_state
    ):
        gscores = gap_scores_by_state.get(state0.state) or {}
        gmeta = gap_meta_by_state.get(state0.state) or {}
        if gscores:
            _write_spine_gap_sheet(
                wb.create_sheet("API Model Gaps"),
                state0.state,
                gscores,
                gmeta,
            )
    # Audit / reference block: Entities by Domain, References, then the
    # lookup surfaces (Legend, Methodology Notes) at the end — Option B
    # puts the working sheets up front; Legend moved from position 2.
    ebd_ws = wb.create_sheet("Entities by Domain")
    _write_entities_by_domain(ebd_ws, state_inputs)
    ref_ws = wb.create_sheet("References")
    _write_reference_sheet(ref_ws, state_inputs)
    _write_legend_sheet(wb.create_sheet("Legend"), lens)
    kl_ws = wb.create_sheet("Methodology Notes")
    # Per-state workbooks pass their own state so the notes are filtered
    # to shared rows + state-specific rows only; combined shows all.
    _write_known_limitations(
        kl_ws, state=state0.state if single_state else None
    )
    # Readme LAST — generated from the sheets actually present above.
    _write_readme_sheet(readme_ws, wb)
    # Presentation finish (freeze/filter/tab colors/formats/CF) — one
    # registry-driven pass over the completed workbook.
    _finalize_workbook(wb, lens)
    return wb


def _build_workbook(
    state_inputs: list[StateInputs],
    lens: str = "source",
    **kwargs,
) -> Workbook:
    """Per-state workbook — thin wrapper over the unified `_build`."""
    return _build(state_inputs, lens, combined=False, **kwargs)


def _build_combined(
    state_inputs: list[StateInputs],
    lens: str = "source",
    **kwargs,
) -> Workbook:
    """Combined workbook — thin wrapper over the unified `_build`."""
    return _build(state_inputs, lens, combined=True, **kwargs)



def run(
    state: str | None = None,
    out_dir: Path | None = None,
    lens: str = "source",
    with_human: Path | None = None,
    human_config: str | None = None,
    allow_stale: bool = False,
    spine_dir: Path | None = None,
) -> list[Path]:
    """Produce analyst XLSX workbook(s) under the given lens.

    - `state='AZ'` (or WI/MN/TX) writes `{state}_analyst[_spine].xlsx`.
    - `state=None` (default) writes all five per-state workbooks PLUS
      `coverage_analyst[_spine].xlsx` (combined). (The old `all_states`
      flag was never read — combined output always keyed off `state`.)
    - Every artifact read resolves against `out_dir` (issue #213 item 1:
      elements/gap-logs used to read module-level `_OUT_DIR` even when
      `out_dir` pointed elsewhere — a mixed-generation workbook seam);
      the spine catalog resolves against `spine_dir` (default
      `data/spine`).
    - `lens='source'` (default) preserves today's filenames.
      `lens='spine'` suffixes `_spine` on every produced filename to
      keep the two lenses physically separate on disk (the 3b design
      contract — no mixed-iteration failure mode).
    - `with_human=<path>` writes SEPARATE `*_with_human.xlsx` copies —
      the pipeline deliverables on disk are never overwritten by a
      comparison run (Cmp: is a side-by-side working artifact, not a
      deliverable mutation; no flag-less re-run needed to restore).
    """
    out = out_dir or _OUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    suffix = "_spine" if lens == "spine" else ""
    human_suffix = "_with_human" if with_human is not None else ""

    produced: list[Path] = []
    if state is not None:
        targets = [state.upper()]
        produce_combined = False
    else:
        targets = list(_STATES)
        produce_combined = True

    # Load per-state score sidecars once — if any exist, Reviewer View
    # cols 8-9 get filled and Scoring Summary + Audit Trail sheets land
    # on each workbook (Phase D). Sidecars are optional by design: tests
    # / analysts running the report before scoring has run get the pre-
    # Phase-D blank-cols-8-11 contract without any opt-out flag.
    scores_by_state: dict[str, dict[str, dict]] = {}
    for st in targets:
        sidecar = _load_scores_sidecar(
            st, lens, out, allow_stale=allow_stale
        )
        if sidecar:
            scores_by_state[st] = sidecar

    # Peer-gap artifact — optional; when present, per-state workbooks
    # render the Peer Gaps sheet. Shared across per-state workbooks so
    # we read it once per run().
    peer_gap_slots = _load_peer_gap_artifact(out / _PEER_GAP_REL_PATH)
    if not peer_gap_slots:
        logger.info(
            "analyst[%s]: peer_gap artifact absent or empty at %s — "
            "Peer Gaps sheet omitted",
            lens, out / _PEER_GAP_REL_PATH,
        )

    # Track C scorecard integration (Phase 2) — load per-state
    # recommendation rows + the lens-scoped review-queue routes. Both
    # are optional: if no recommendations sidecar exists for a state,
    # that state's workbook simply ships without the Recommendations
    # sheet (same posture as Scoring Summary being optional pre-Phase-D).
    from src.report.recommendations import sheet_rows_for_workbook

    recs_by_state: dict[str, list[dict]] = {}
    for st in targets:
        rec_path = out / f"{st.lower()}_recommendations_{lens}.json"
        if not rec_path.exists():
            continue
        try:
            recs_by_state[st] = sheet_rows_for_workbook(
                st, lens, base=out, allow_stale=allow_stale
            )
        except FileNotFoundError:
            # Absent recs sidecar → workbook ships without the sheet.
            # Corrupt/stale sidecars raise out of generate_state (issue
            # #212 item 3) — deliberately NOT caught here.
            continue

    review_queue_routes = _load_review_queue_routes(
        lens, out, allow_stale=allow_stale
    )

    # Option C round-trip — ingested analyst edits (curation sidecars at
    # data/curation/{state}.json) re-applied into the analyst-input band
    # on EVERY regeneration; that re-apply is the whole point, so there
    # is no opt-out flag. Absent sidecars simply leave the band empty.
    from src.report.curation import adjudications_for, curation_values_for

    curation_by_state: dict[str, dict[str, dict[str, object]]] = {}
    for st in targets:
        cur = curation_values_for(st)
        if cur:
            curation_by_state[st] = cur

    # Issue #248 Part C — resolved adjudications (fresh/stale computed
    # against the loaded scores) for the Effective Score column, the
    # queue's OVERRIDE suppression / RE-ADJUDICATE rows, the Update Log
    # register, and the Score Card rollups. Same no-opt-out posture as
    # the curation re-apply.
    adjudications_by_state: dict[str, dict[str, dict]] = {}
    for st in targets:
        adj = adjudications_for(st, scores_by_state.get(st) or {}, lens)
        if adj:
            adjudications_by_state[st] = adj

    # Issue #249 — resolved fact corrections (applied / pending
    # re-aggregate / record gone, computed against the loaded scores)
    # for the Update Log register. Same no-opt-out posture.
    from src.report.curation import fact_corrections_for
    from src.report.fact_corrections import resolve_corrections

    fact_corrections_by_state: dict[str, list[dict]] = {}
    for st in targets:
        resolved = resolve_corrections(
            st,
            fact_corrections_for(st, lens),
            scores_by_state.get(st) or {},
        )
        if resolved:
            fact_corrections_by_state[st] = resolved

    # `--with-human` — resolve a human-scored workbook onto record keys
    # once per run; the Details sheet then renders the appended Cmp:
    # comparison block (June follow-up format, generalized).
    human_lookup: dict[str, tuple] | None = None
    if with_human is not None:
        from src.report.human_score_backfill import resolve_human_scores

        human_lookup = resolve_human_scores(
            Path(with_human),
            config=human_config,
            fixed_state=(targets[0] if state is not None else None),
        ) or None
        logger.info(
            "analyst[%s]: --with-human resolved %d row(s) from %s",
            lens, len(human_lookup or {}), with_human,
        )

    # Spine-anchored gap sidecars + metadata (issue #73 Step 1) — both
    # lenses since Option D: the spine workbook renders API Model Gaps,
    # the SOURCE workbook folds the same signal into Documentation Gaps.
    # Same opt-in posture as the primary score sidecar: absent artifacts
    # simply mean the sheet renders without gap-sidecar rows.
    gap_scores_by_state: dict[str, dict[str, dict]] = {}
    gap_meta_by_state: dict[str, dict[str, dict]] = {}
    for st in targets:
        gscores = _load_gap_scores_sidecar(
            st, out, allow_stale=allow_stale
        )
        if not gscores:
            continue
        gap_scores_by_state[st] = gscores
        gap_meta_by_state[st] = _load_gap_metadata(
            st, out, allow_stale=allow_stale
        )

    per_state_inputs: dict[str, StateInputs] = {}
    for st in targets:
        si = load_inputs(
            st, lens=lens, out_dir=out, spine_dir=spine_dir or _SPINE_DIR
        )
        per_state_inputs[st] = si
        wb = _build_workbook(
            [si],
            lens=lens,
            scores_by_state={st: scores_by_state[st]} if st in scores_by_state else None,
            peer_gap_slots=peer_gap_slots or None,
            recs_by_state={st: recs_by_state[st]} if st in recs_by_state else None,
            review_queue_routes=review_queue_routes or None,
            gap_scores_by_state=(
                {st: gap_scores_by_state[st]}
                if st in gap_scores_by_state else None
            ),
            gap_meta_by_state=(
                {st: gap_meta_by_state[st]}
                if st in gap_meta_by_state else None
            ),
            curation_by_state=(
                {st: curation_by_state[st]}
                if st in curation_by_state else None
            ),
            adjudications_by_state=(
                {st: adjudications_by_state[st]}
                if st in adjudications_by_state else None
            ),
            fact_corrections_by_state=(
                {st: fact_corrections_by_state[st]}
                if st in fact_corrections_by_state else None
            ),
            human_lookup=human_lookup,
        )
        path = out / f"{st.lower()}_analyst{suffix}{human_suffix}.xlsx"
        wb.save(path)
        logger.info(
            "analyst[%s]: wrote %s (%d rows)",
            lens, path, si.elements.element_count,
        )
        produced.append(path)

    if produce_combined:
        ordered = [per_state_inputs[s] for s in _STATES]
        wb = _build_combined(
            ordered,
            lens=lens,
            scores_by_state=scores_by_state or None,
            recs_by_state=recs_by_state or None,
            review_queue_routes=review_queue_routes or None,
            curation_by_state=curation_by_state or None,
            adjudications_by_state=adjudications_by_state or None,
            fact_corrections_by_state=fact_corrections_by_state or None,
            human_lookup=human_lookup,
            gap_scores_by_state=gap_scores_by_state or None,
            gap_meta_by_state=gap_meta_by_state or None,
        )
        path = out / f"coverage_analyst{suffix}{human_suffix}.xlsx"
        wb.save(path)
        logger.info("analyst[%s]: wrote combined %s", lens, path)
        produced.append(path)

    return produced
