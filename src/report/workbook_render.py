"""Generic workbook renderer for the R2 declarative spec (issue #186 seq 2).

``render_table_sheet`` writes any table ``SheetSpec``: header row from
the spec (lens-filtered), one row per ``RowContext`` with each cell
produced by its column's extractor, the renderer-injected leading
``Row #`` address when the spec asks for it, and per-column widths.

Presentation finish (freeze panes / autofilter / tab colors / number
formats / conditional formatting) stays in ``analyst._finalize_workbook``
— a workbook-level post-pass resolved by header NAME, unchanged by R2.

Style primitives (``_set_cell`` formula guard, ``_set_widths``, header
font/fill) moved here from ``analyst.py``, which re-exports them for its
not-yet-flipped writers.
"""

from __future__ import annotations

from typing import Callable, Iterable

from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink
from openpyxl.worksheet.worksheet import Worksheet

from src.report.workbook_spec import (
    _DETAILS_FINISH,
    _SheetFinish,
    RowContext,
    SHEET_SPECS,
    SheetSpec,
)

_HEADER_FONT = Font(bold=True)
_HEADER_FILL = PatternFill(
    start_color="FFDDDDDD", end_color="FFDDDDDD", fill_type="solid"
)
# Analyst-input band headers — the "work" tab-color green, visually
# distinct from the grey machine columns (Option B band styling).
_ANALYST_HEADER_FILL = PatternFill(
    start_color="FFC6E0B4", end_color="FFC6E0B4", fill_type="solid"
)
# Single source of truth for the analyst-band behavior prose — rendered
# both as the band header-cell comment (below) and as the Legend sheet's
# "Analyst-input columns" section (analyst._write_legend_sheet), so the
# two surfaces can never diverge (issue #211 item 1c: the Legend carried
# a stale pre-Option-C "not yet preserved" claim for months).
ANALYST_BAND_NOTE = (
    "Analyst-input space — the pipeline never writes in these columns. "
    "Your entries round-trip: run `poc3 review ingest <workbook>` and "
    "every regeneration re-applies them (curation sidecar at "
    "data/curation/{state}.json)."
)
_ANALYST_BAND_NOTE = ANALYST_BAND_NOTE  # compat alias


def _set_cell(ws: Worksheet, row: int, col: int, value):
    """Write `value` into (row, col), forcing text data-type when the value is
    a string starting with `=`.

    Without this guard, openpyxl writes strings like WI's
    `"=== Element-specific rules ..."` as a formula (leading `=` is openpyxl's
    formula trigger). Excel then flags the workbook as corrupt: "Removed
    Records: Formula from /xl/worksheets/sheetN.xml".
    """
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"
    return cell


def _set_widths(ws: Worksheet, widths: dict[int, int]) -> None:
    for col_idx, w in widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = w


def render_table_sheet(
    ws: Worksheet,
    sheet: SheetSpec,
    rows: Iterable[RowContext],
    lens: str,
    *,
    title: str | None = None,
    row_font: Callable[[object], Font | None] | None = None,
) -> None:
    """Render one table sheet from its spec.

    ``title`` overrides ``sheet.title`` for title-family sheets (the
    spine-lens ``"{ST} — Documented only"`` subsets reuse the Reviewer
    View spec under per-state titles).

    ``row_font`` styles whole data rows from the ROW ITSELF (issue #213
    item 1): called once per row; a returned ``Font`` is applied to
    every cell the row rendered. Replaces the hand-maintained "remember
    which sheet rows to bold, sweep them after render" ladders the
    Entities-by-Domain and Commitment-Tracker writers each re-derived.
    """
    cols = sheet.columns_for(lens)
    ws.title = title or sheet.title
    headers = sheet.headers_for(lens)
    sheet_headers = ("Row #", *headers) if sheet.row_number_column else headers
    header_roles = [None] + [c.role for c in cols] if sheet.row_number_column \
        else [c.role for c in cols]
    first_analyst_col: int | None = None
    for col_idx, h in enumerate(sheet_headers, start=1):
        c = ws.cell(row=1, column=col_idx, value=h)
        c.font = _HEADER_FONT
        if header_roles[col_idx - 1] == "analyst_input":
            c.fill = _ANALYST_HEADER_FILL
            if first_analyst_col is None:
                first_analyst_col = col_idx
                c.comment = Comment(_ANALYST_BAND_NOTE, "POC-3")
        else:
            c.fill = _HEADER_FILL

    offset = 1 if sheet.row_number_column else 0
    wrap_cols = {
        i for i, c in enumerate(cols, start=1 + offset) if c.wrap
    }
    wrap_alignment = Alignment(wrap_text=True, vertical="top")
    row_idx = 2
    for ctx in rows:
        values = [c.extract(ctx) for c in cols]
        if sheet.row_number_column:
            values = [ctx.row_number, *values]
        font = row_font(ctx) if row_font is not None else None
        for col_idx, v in enumerate(values, start=1):
            cell = _set_cell(ws, row_idx, col_idx, v)
            if col_idx in wrap_cols:
                cell.alignment = wrap_alignment
            if font is not None:
                cell.font = font
        row_idx += 1

    widths = {
        i: sheet.default_width for i in range(1, len(sheet_headers) + 1)
    }
    if sheet.row_number_column:
        widths[1] = 8  # compact Row # address column
    for i, col in enumerate(cols, start=1 + offset):
        if col.width is not None:
            widths[i] = col.width
    _set_widths(ws, widths)


_LINK_FONT = Font(color="FF0563C1", underline="single")


def _header_column(ws: Worksheet, header: str) -> int | None:
    """1-based column index of ``header`` in row 1, or None."""
    for c in range(1, ws.max_column + 1):
        if ws.cell(row=1, column=c).value == header:
            return c
    return None


def apply_details_row_links(
    ws: Worksheet,
    positions: dict[int, int],
    *,
    header: str = "Row #",
    target_sheet: str = "Details",
) -> None:
    """Hyperlink each ``header`` cell to its rendered Details row.

    ``positions`` maps a Row-# value to the sheet row it landed on in
    the RENDERED Details sheet (built from the rendered context order,
    NOT ``row_number + 1`` arithmetic — so a filtered Details stream,
    e.g. Option D's documented-only relocation, can never mis-target a
    link). Row-# values absent from the map are left unlinked.
    """
    header_col = _header_column(ws, header)
    if header_col is None:
        return
    for r in range(2, ws.max_row + 1):
        cell = ws.cell(row=r, column=header_col)
        target_row = positions.get(cell.value)
        if target_row is None:
            continue
        cell.hyperlink = Hyperlink(
            ref=cell.coordinate,
            location=f"'{target_sheet}'!A{target_row}",
        )
        cell.font = _LINK_FONT


def apply_reviewed_echo(
    ws: Worksheet,
    details_ws: Worksheet,
    *,
    echo_header: str = "Reviewed? (from Details)",
    details_header: str = "Reviewed?",
) -> None:
    """Fill the queue's read-only progress column with live INDEX/MATCH
    formulas over the Details ``Reviewed?`` band column, keyed on Row #
    (issue #259 PR 3).

    Written POST-RENDER by direct ``cell.value`` assignment — the
    generic renderer's ``_set_cell`` deliberately escapes a leading
    ``=`` (the formula guard), and this is the one sanctioned formula
    surface. Every column is located by header-row scan (never spec
    index arithmetic), so column insertions on either sheet can't
    mis-target. MATCH is by Row-# VALUE, so analysts re-sorting or
    filtering Details never break the echo; MATCH misses (rows living
    on Documentation Gaps) render blank via IFERROR, and record-gone
    rows (no Row # value) are skipped outright. The trailing ``&""``
    coercion is load-bearing: a bare INDEX over an empty cell renders
    0, not blank — the column would look broken on day one.
    """
    echo_col = _header_column(ws, echo_header)
    row_col = _header_column(ws, "Row #")
    d_reviewed = _header_column(details_ws, details_header)
    d_row = _header_column(details_ws, "Row #")
    if None in (echo_col, row_col, d_reviewed, d_row):
        return
    reviewed = get_column_letter(d_reviewed)
    match = get_column_letter(d_row)
    row_ref = get_column_letter(row_col)
    title = details_ws.title
    if not title.isalnum():
        title = f"'{title}'"
    for r in range(2, ws.max_row + 1):
        if ws.cell(row=r, column=row_col).value is None:
            continue
        ws.cell(row=r, column=echo_col).value = (
            f"=IFERROR(INDEX({title}!${reviewed}:${reviewed},"
            f"MATCH(${row_ref}{r},{title}!${match}:${match},0))"
            f'&"","")'
        )


# --- Workbook-level presentation finish (Sequence-1 hygiene) ----------------

_TAB_COLORS: dict[str, str] = {
    "orient": "FFBDD7EE",  # blue — read-me-first surfaces
    "work": "FFC6E0B4",    # green — day-to-day working sheets
    "audit": "FFD9D9D9",   # grey — audit / reference material
}

_FLAG_YES_FILL = PatternFill(
    start_color="FFFFC7CE", end_color="FFFFC7CE", fill_type="solid"
)


def _finish_for(title: str) -> _SheetFinish:
    """Finish spec for a sheet title; suffix rule covers the per-state
    "{ST} — Documented only" family. KeyError on unknown titles is the
    build-time guard."""
    if title.endswith("— Documented only"):
        return _DETAILS_FINISH
    return SHEET_SPECS[title].finish


def _finalize_workbook(wb, lens: str) -> None:
    """Apply the sheet-finish registry to every sheet in a built workbook.

    Tab color always applies; freeze/filter/formats/conditional formatting
    only when the sheet has at least one data row (openpyxl ranges like
    ``X2:X1`` are invalid). Headers referenced by a spec but absent from a
    given sheet are skipped, which is what lets one spec serve both lenses.
    """
    for ws in wb.worksheets:
        spec = _finish_for(ws.title)
        ws.sheet_properties.tabColor = _TAB_COLORS[spec.group]
        if ws.max_row < 2:
            continue
        if spec.autofilter:
            ws.auto_filter.ref = ws.dimensions
        if spec.freeze:
            ws.freeze_panes = spec.freeze
        header_col = {
            ws.cell(row=1, column=c).value: c
            for c in range(1, ws.max_column + 1)
        }
        for header, fmt in spec.number_formats:
            col = header_col.get(header)
            if col is None:
                continue
            for r in range(2, ws.max_row + 1):
                cell = ws.cell(row=r, column=col)
                if cell.value is not None:
                    cell.number_format = fmt
        for header, lo, hi in spec.color_scale:
            col = header_col.get(header)
            if col is None:
                continue
            letter = get_column_letter(col)
            ws.conditional_formatting.add(
                f"{letter}2:{letter}{ws.max_row}",
                ColorScaleRule(
                    start_type="num", start_value=lo, start_color="FFFFFFFF",
                    mid_type="num", mid_value=(lo + hi) / 2,
                    mid_color="FFFFEB84",
                    end_type="num", end_value=hi, end_color="FFF8696B",
                ),
            )
        for header in spec.flag_yes:
            col = header_col.get(header)
            if col is None:
                continue
            letter = get_column_letter(col)
            ws.conditional_formatting.add(
                f"{letter}2:{letter}{ws.max_row}",
                # NOTE the inner quotes — `formula=["Yes"]` is the classic
                # silent no-op; Excel needs the string literal quoted.
                CellIsRule(
                    operator="equal", formula=['"Yes"'], fill=_FLAG_YES_FILL,
                ),
            )
