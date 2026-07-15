"""Phase E — human-scored reviewer-file loader (per-state workbooks).

Parses the five per-state human-scored workbooks under
``docs/human-scored-files/`` into a list of ``ReviewerRecord`` objects
suitable for joining against POC-3 sidecars. The per-state basis
replaced the single ``SourceData_Agent_training_file.xlsx`` workbook on
2026-07-07 — the per-state files are the fresher authoring passes
(IN grew 52 → 362 rows; AZ is human-scored for the first time) and each
carries its own column layout, so columns are resolved by **normalized
header name** per ``ReviewerSource`` entry, never by fixed index.

**The reviewer workbooks are NOT committed to the repo** — they are
externally-supplied artifacts that live under ``docs/human-scored-files/``
(gitignored) only on workstations where they have been hand-placed. A
fresh clone of main will not have them, and the Phase E CLI surfaces
(``poc3 report review-digest``) will raise ``FileNotFoundError`` until
they are provided out-of-band. Loading is all-or-nothing: a partial
file set would produce silently misleading totals, so ANY missing file
refuses with the full list. Files in the same directory that are NOT in
``REVIEWER_SOURCES`` (``All Combined.xlsx``, the Nebraska extensions
workbook, the GADOE Georgia file) are deliberately never loaded —
combined disagrees with the per-state passes, and NE/GA have no POC-3
sidecars to join.

Read-only discipline (see ``docs/archive/next-session/next-session-phase-e.md``):
the workbooks stay under ``docs/`` and are never copied into ``data/``
or referenced from a rule / prompt / test fixture. The reviewer is
**one human-scored observer**, never ground truth — see
``feedback_human_scores_not_gt.md`` in user memory.

Duplicate (entity, element) rows inside a workbook (WI 21, TX 14,
IN 14) each load as their own ``ReviewerRecord`` and join independently
— the comparison layer's ``matched_poc3_keys`` set keeps
``no_reviewer_row`` accounting safe, and deduping here would silently
hide reviewer-side re-scores.

CRITICAL: ``run()`` is a plain function; the Click wrapper lives in
``src/poc3/cli.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

_LOGGER = logging.getLogger(__name__)


HUMAN_SCORED_DIR: Path = (
    Path(__file__).resolve().parents[2] / "docs" / "human-scored-files"
)


#: Every column role the loader can populate. ``ReviewerSource.headers``
#: maps each role to the workbook's normalized header string, or None
#: when the workbook genuinely lacks the column (IN has no Justification
#: or Complex Business Logic column; AZ has no Complex Business Logic).
ROLES: tuple[str, ...] = (
    "state",
    "entity",
    "element",
    "nachos",
    "adjusted",
    "justification",
    "is_extension",
    "unnecessary_extension",
    "cross_entity",
    "complex_business_logic",
)

#: Roles a source may not declare as absent — without them a row can't
#: be joined or compared at all.
REQUIRED_ROLES: tuple[str, ...] = ("entity", "element", "nachos", "adjusted")


@dataclass(frozen=True)
class ReviewerSource:
    """One per-state human-scored workbook + how to read it.

    ``headers`` values are **normalized** header strings (see
    ``normalize_header`` — all whitespace runs, including newlines,
    collapse to a single space), so ``"Adjusted NACHOS  Score"`` and
    ``"Is \\nExtension"`` in the raw cells match ``"Adjusted NACHOS
    Score"`` / ``"Is Extension"`` here. Exact full-string matching keeps
    decoy columns (``"Adjusted NACHOS Score - Original MSP"``) out.
    """

    key: str
    """Config name used by CLI surfaces (``arizona``, ``indiana``, …)."""

    state: str
    """POC-3 state code — authoritative for every row in the file
    (fixed-state semantics; the State cell is only a mismatch check)."""

    state_cell_value: str
    """Expected full state name in the State column (warn-on-mismatch
    audit signal for a wrong file dropped into the directory)."""

    filename: str
    """Exact xlsx filename under ``HUMAN_SCORED_DIR``."""

    sheet_name: str
    """Worksheet carrying the human-scored rows."""

    headers: Mapping[str, str | None]
    """Role → normalized expected header (None = column absent)."""

    def path(self, reviewer_dir: Path | None = None) -> Path:
        return (reviewer_dir or HUMAN_SCORED_DIR) / self.filename


#: The five-file comparison basis, ordered like
#: ``src.states.SUPPORTED_STATES`` (AZ, WI, MN, TX, IN). Header strings
#: were verified against the live files on 2026-07-07. MN's basis is the
#: ``Details`` sheet (672 rows) — NOT ``Details (2)`` (787 rows, the
#: superseded pass the "All Combined" workbook was built from). State
#: cells are never authoritative in any file — ``state`` is stamped
#: from the registry (fixed-state semantics).
REVIEWER_SOURCES: tuple[ReviewerSource, ...] = (
    ReviewerSource(
        key="arizona",
        state="AZ",
        state_cell_value="Arizona",
        filename="NACHOS Arizona 20260310.xlsx",
        sheet_name="Details",
        headers={
            "state": "State",
            "entity": "Entity Name",
            "element": "Data Element",
            "nachos": "NACHOS score",
            "adjusted": "Adjusted NACHOS Score",
            "justification": "Justification for Adjusted NACHOS Score",
            "is_extension": "Is Extension",
            "unnecessary_extension": "Unnecessary Extension ?",
            "cross_entity": "Cross Entity Calculation ?",
            "complex_business_logic": None,
        },
    ),
    ReviewerSource(
        key="wisconsin",
        state="WI",
        state_cell_value="Wisconsin",
        filename="NACHOS Wisconsin V2.xlsx",
        sheet_name="Details",
        headers={
            "state": "State",
            "entity": "Entity Name",
            "element": "Data Element",
            "nachos": "NACHOS score",
            "adjusted": "Adjusted NACHOS Score",
            "justification": "Justification for Adjusted NACHOS Score",
            "is_extension": "Is an extension",
            "unnecessary_extension": "Unnecessary Extension",
            "cross_entity": "Cross Entity",
            "complex_business_logic": "Complex Business Logic",
        },
    ),
    ReviewerSource(
        key="minnesota",
        state="MN",
        state_cell_value="Minnesota",
        filename="NACHOS Minnesota V4.xlsx",
        sheet_name="Details",
        headers={
            # The MN State header cell is the corrupted literal "es" —
            # the COLUMN is valid (cells carry "Minnesota"), so declare
            # the header as-is; a future fixed workbook fails loudly
            # here and the entry gets updated deliberately.
            "state": "es",
            "entity": "Entity Name",
            "element": "Data Element",
            "nachos": "NACHOS score",
            "adjusted": "Adjusted NACHOS Score",
            "justification": "Justification for Adjusted NACHOS Score",
            "is_extension": "Is an extension",
            "unnecessary_extension": "Unnecessary Extension",
            "cross_entity": "Cross Entity",
            "complex_business_logic": "Complex Business Logic",
        },
    ),
    ReviewerSource(
        key="texas",
        state="TX",
        state_cell_value="Texas",
        filename="NACHOS Texas V3_Jan15_2026.xlsx",
        sheet_name="Details",
        headers={
            "state": "State",
            "entity": "Entity Name",
            "element": "Data Element",
            "nachos": "NACHOS score",
            "adjusted": "Adjusted NACHOS Score",
            "justification": "Justification for Adjusted NACHOS Score",
            "is_extension": "Is an extension",
            "unnecessary_extension": "Unnecessary Extension",
            "cross_entity": "Cross Entity",
            "complex_business_logic": "Complex Business Logic",
        },
    ),
    ReviewerSource(
        key="indiana",
        state="IN",
        state_cell_value="Indiana",
        filename="NACHOS Indiana_complete.xlsx",
        sheet_name="Details",
        headers={
            "state": "State",
            "entity": "ResourceName",
            "element": "Data Element",
            "nachos": "NACHOS score",
            "adjusted": "Adjusted Score",
            "justification": None,
            "is_extension": "Is an extension",
            "unnecessary_extension": "Unnecessary Extension",
            "cross_entity": (
                "Cross entity reference. If yes, then adjust score by +0.5"
            ),
            "complex_business_logic": None,
        },
    ),
)

_IN_SCOPE_STATES: frozenset[str] = frozenset(s.state for s in REVIEWER_SOURCES)


@dataclass(frozen=True)
class ReviewerRecord:
    """One reviewer-pass row, with cell values trimmed + state stamped.

    Only the fields the comparison pipeline or digest needs are
    surfaced — References, DS Next Steps, Legislation columns live in
    the workbooks for audit context but don't feed Phase E's
    classification or reporting. They can be added later if the digest
    needs richer worked-example citations.
    """

    state: str
    """POC-3 code (AZ, WI, MN, TX, IN) — stamped from the
    ``ReviewerSource``, never parsed from the row."""

    entity: str
    """Raw reviewer entity name, whitespace-trimmed (NOT normalized —
    the keymap handles that, including ``idoe/`` / ``ed-fi/`` prefixes)."""

    element: str
    """Raw reviewer element name, whitespace-trimmed."""

    nachos_score: int | None
    """Reviewer's base NACHOS tier (0–3). Missing cells surface as None."""

    adjusted_nachos_score: float | None
    """Reviewer's adjusted NACHOS (0–4.5, in 0.5 increments)."""

    justification: str | None
    """Free-text reviewer rationale for the adjusted score. Used to
    classify ``key_sever_override`` rows + for digest worked examples.
    Always None for IN — its workbook has no justification column."""

    is_extension: str | None
    """Reviewer's ``Yes``/``No``/``None`` extension answer — orthogonal
    to POC-3's ``source == "extension"`` determination but useful for
    the extension-necessity divergence bucket."""

    unnecessary_extension: str | None
    """Reviewer's unnecessary-extension classification."""

    cross_entity: str | None
    """Reviewer's cross-entity annotation."""

    complex_business_logic: str | None
    """Reviewer's complexity flag (``Yes`` / blank). Always None for
    AZ and IN — their workbooks have no such column."""

    extras: dict[str, Any] = field(default_factory=dict)
    """Reserved for future audit fields without breaking the dataclass
    contract. Not populated in the current loader."""


def normalize_header(value: Any) -> str:
    """Collapse ALL whitespace runs (incl. newlines) to single spaces.

    The hand-authored workbooks carry ``"Adjusted NACHOS  Score"``
    (double space), ``"Is \\nExtension"`` (embedded newline), and
    trailing spaces — all reviewer typography, not identity. Blank /
    None cells normalize to ``""``.
    """
    if value is None:
        return ""
    return " ".join(str(value).split())


def resolve_columns(
    header_row: tuple[Any, ...] | list[Any],
    headers: Mapping[str, str | None],
    *,
    context: str,
) -> dict[str, int]:
    """Map declared role headers to 0-indexed columns, failing loudly.

    Raises ``ValueError`` when a required role is declared absent, a
    declared header is not found in the sheet, or a declared header
    appears more than once (ambiguous). ``context`` names the workbook
    (+ sheet) in every message so a hand-edit that renames a column is
    diagnosable from the traceback alone.
    """
    positions: dict[str, list[int]] = {}
    for idx, cell in enumerate(header_row):
        norm = normalize_header(cell)
        if norm:
            positions.setdefault(norm, []).append(idx)

    resolved: dict[str, int] = {}
    for role in ROLES:
        expected = headers.get(role)
        if expected is None:
            if role in REQUIRED_ROLES:
                raise ValueError(
                    f"{context}: required role {role!r} is declared absent "
                    "(header=None) — every reviewer source must map "
                    f"{REQUIRED_ROLES}"
                )
            continue
        found = positions.get(expected)
        if not found:
            available = ", ".join(sorted(positions))
            raise ValueError(
                f"{context}: header {expected!r} (role {role!r}) not found "
                f"in row 1. The workbook layout has changed — update the "
                f"ReviewerSource entry. Normalized headers present: "
                f"{available}"
            )
        if len(found) > 1:
            raise ValueError(
                f"{context}: header {expected!r} (role {role!r}) appears "
                f"{len(found)} times (columns {found}) — ambiguous; "
                "disambiguate the workbook or the ReviewerSource entry"
            )
        resolved[role] = found[0]
    return resolved


def _maybe_str(value: Any) -> str | None:
    """Coerce openpyxl cell to trimmed str or None.

    Openpyxl returns ``None`` for blank cells and the native type
    (``int``, ``float``, ``str``) for populated ones. Empty/whitespace
    strings normalize to ``None`` so downstream consumers only see
    meaningful text.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value).strip() or None


def _maybe_int(value: Any) -> int | None:
    """Coerce a numeric-or-string cell to int. None-safe.

    Reviewer NACHOS cells are stored as integers (0/1/2/3) but some
    rows may arrive as strings after round-trips. Parsing non-numeric
    strings yields ``None`` — the digest surfaces missing values
    rather than raising.
    """
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
    """Coerce a numeric-or-string cell to float. None-safe."""
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


def _identity_str(value: Any) -> str | None:
    """Entity/element cell → joinable string, or ``None``.

    Treats the literal ``NA`` / ``N/A`` placeholders the reviewer files
    use for identity cells as missing: such rows are unjoinable by
    construction (no POC-3 record key can ever resolve them) and would
    silently deflate the match rate as permanent ``no_poc3_row``
    entries (17 WI rows on the current basis: statusCode,
    patientIdentifier.*, …).
    """
    s = _maybe_str(value)
    if s is not None and s.strip().upper() in {"NA", "N/A"}:
        return None
    return s


def _cell(row: tuple[Any, ...], idx: int) -> Any:
    """Bounds-safe row access — openpyxl trims trailing-blank cells."""
    return row[idx] if idx < len(row) else None


def load_reviewer_source(
    source: ReviewerSource,
    reviewer_dir: Path | None = None,
) -> list[ReviewerRecord]:
    """Load one per-state workbook into ReviewerRecords.

    Every record is stamped ``state=source.state`` (fixed-state
    semantics). Rows drop when entity or element is blank or a literal
    ``NA``/``N/A`` placeholder (unjoinable). A populated State cell
    that doesn't match ``source.state_cell_value`` only WARNs — it is
    an audit signal for a wrong file in the directory, and blank State
    cells (TX carries one) never drop a row that has identity.

    Uses ``read_only=True, data_only=True`` — computed values only,
    streamed row-by-row (the TX workbook is 10 MB).
    """
    target = source.path(reviewer_dir)
    if not target.exists():
        raise FileNotFoundError(_missing_files_message([source.filename], target.parent))

    context = f"{source.filename} [{source.sheet_name}]"
    wb = load_workbook(target, read_only=True, data_only=True)
    try:
        if source.sheet_name not in wb.sheetnames:
            raise KeyError(
                f"{source.filename}: missing sheet {source.sheet_name!r}; "
                f"has {wb.sheetnames}"
            )
        ws = wb[source.sheet_name]
        rows = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            raise ValueError(f"{context}: sheet is empty") from None
        cols = resolve_columns(header_row, source.headers, context=context)

        records: list[ReviewerRecord] = []
        dropped_na_identity = 0
        state_mismatches = 0
        for row in rows:
            if all(v in (None, "") for v in row):
                continue
            raw_entity = _cell(row, cols["entity"])
            raw_element = _cell(row, cols["element"])
            entity = _identity_str(raw_entity)
            element = _identity_str(raw_element)
            if entity is None or element is None:
                if _maybe_str(raw_entity) is not None or _maybe_str(raw_element) is not None:
                    dropped_na_identity += 1
                continue

            if "state" in cols:
                state_raw = _maybe_str(_cell(row, cols["state"]))
                if state_raw is not None and state_raw != source.state_cell_value:
                    state_mismatches += 1

            def _opt(role: str) -> Any:
                idx = cols.get(role)
                return _cell(row, idx) if idx is not None else None

            records.append(
                ReviewerRecord(
                    state=source.state,
                    entity=entity,
                    element=element,
                    nachos_score=_maybe_int(_cell(row, cols["nachos"])),
                    adjusted_nachos_score=_maybe_float(_cell(row, cols["adjusted"])),
                    justification=_maybe_str(_opt("justification")),
                    is_extension=_maybe_str(_opt("is_extension")),
                    unnecessary_extension=_maybe_str(_opt("unnecessary_extension")),
                    cross_entity=_maybe_str(_opt("cross_entity")),
                    complex_business_logic=_maybe_str(_opt("complex_business_logic")),
                )
            )

        if state_mismatches:
            _LOGGER.warning(
                "%s: %d State cells differ from expected %r — verify the "
                "right workbook is in place (rows were kept, stamped %s)",
                source.filename,
                state_mismatches,
                source.state_cell_value,
                source.state,
            )
        if dropped_na_identity:
            _LOGGER.info(
                "%s: dropped %d rows with blank/NA entity or element "
                "(unjoinable by construction)",
                source.filename,
                dropped_na_identity,
            )
        _LOGGER.info(
            "%s: loaded %d %s reviewer records",
            source.filename,
            len(records),
            source.state,
        )
        return records
    finally:
        wb.close()


def _missing_files_message(missing: list[str], directory: Path) -> str:
    expected = "\n".join(f"  - {s.filename}" for s in REVIEWER_SOURCES)
    listed = "\n".join(f"  - {name}" for name in missing)
    return (
        f"reviewer workbook(s) not found under {directory}:\n{listed}\n\n"
        "Phase E requires the five per-state human-scored workbooks, "
        "which are NOT committed to this repo (the directory is "
        "gitignored). Obtain them out-of-band — ask the engagement "
        "owner — and place them at docs/human-scored-files/. Expected "
        f"files:\n{expected}\n\n"
        "Loading is all-or-nothing: a partial set would produce "
        "silently misleading comparison totals. See README §Phase E "
        "and docs/archive/next-session/next-session-phase-e.md for the "
        "human-scored framing the digest enforces."
    )


def load_reviewer_records(
    reviewer_dir: Path | None = None,
) -> list[ReviewerRecord]:
    """Load ALL five per-state workbooks, concatenated in registry order.

    ``reviewer_dir`` — override the default ``docs/human-scored-files/``
    location (tests point at a tmp directory). Missing files raise
    ``FileNotFoundError`` listing every absent filename (all-or-nothing
    — see the module docstring).

    Returns records in registry order (AZ, WI, MN, TX, IN), worksheet
    order within each state, so reviewers can cite row numbers if a
    digest finding needs a manual lookup.
    """
    directory = reviewer_dir or HUMAN_SCORED_DIR
    missing = [
        source.filename
        for source in REVIEWER_SOURCES
        if not source.path(reviewer_dir).exists()
    ]
    if missing:
        raise FileNotFoundError(_missing_files_message(missing, directory))

    records: list[ReviewerRecord] = []
    for source in REVIEWER_SOURCES:
        records.extend(load_reviewer_source(source, reviewer_dir))
    _LOGGER.info(
        "loaded %d reviewer records (%s) from %s",
        len(records),
        ", ".join(
            f"{source.state}={sum(1 for r in records if r.state == source.state)}"
            for source in REVIEWER_SOURCES
        ),
        directory,
    )
    return records


def reviewer_records_by_state(
    records: list[ReviewerRecord],
) -> dict[str, list[ReviewerRecord]]:
    """Group records by POC-3 state code for per-state comparison passes.

    Preserves load order inside each group (worksheet order) —
    downstream digest worked examples cite rows in the order the
    reviewer wrote them.
    """
    out: dict[str, list[ReviewerRecord]] = {s: [] for s in _IN_SCOPE_STATES}
    for rec in records:
        out.setdefault(rec.state, []).append(rec)
    return out
