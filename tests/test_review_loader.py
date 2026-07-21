"""Phase E — per-state reviewer-file loader tests.

Fabricated-workbook unit tests exercise the loader's header-name
resolution + parsing hermetically (no dependency on the hand-placed
docs files). Real-file round-trip tests at the bottom load the five
workbooks under ``docs/human-scored-files/`` when present and pin the
per-state counts — a single source of truth for "is the workbook set
on disk the one the basis change described."

The synthetic header layouts mirror the REAL files' quirks on purpose
(double-space ``Adjusted NACHOS  Score``, AZ's embedded-newline
``Is \\nExtension``, MN's corrupted ``es`` State header, IN's
``ResourceName``/``Adjusted Score`` naming) so normalization is proven
against the shapes it exists for. No reviewer VALUES are copied here —
rows are fabricated.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from src.score.review_loader import (
    HUMAN_SCORED_DIR,
    REQUIRED_ROLES,
    REVIEWER_SOURCES,
    ReviewerSource,
    load_reviewer_records,
    load_reviewer_source,
    normalize_header,
    resolve_columns,
    reviewer_records_by_state,
)


def _source(key: str) -> ReviewerSource:
    return next(s for s in REVIEWER_SOURCES if s.key == key)


# Mirrors the WI/MN/TX layout (incl. the double-space typography and an
# `- Original MSP` decoy column that exact-match resolution must skip).
STANDARD_HEADER = [
    "State",
    "Domain",
    "Entity Name",
    "Data Element",
    "Data Type",
    "Required",
    "Business Logic (Redacted)",
    "Complex Business Logic",
    "NACHOS score",
    "Adjusted NACHOS  Score",
    "Justification for Adjusted NACHOS  Score",
    "Unnecessary Extension",
    "Cross Entity",
    "Reason for extension necessity logic",
    "Business Logic (Formula)",
    "Reason for Complexity",
    "Multiple Entities Involved",
    "Is an extension",
    "Recommendations",
    "Adjusted NACHOS  Score - Original MSP",
]

# Mirrors the AZ layout (newline headers, `?`-suffixed columns, no
# Complex Business Logic column).
AZ_HEADER = [
    "State",
    "Domain",
    "Entity Name",
    "Data Element",
    "Data Type",
    "Is\nRequired",
    "Is \nExtension",
    "Business Logic (Redacted)",
    "NACHOS score",
    "Adjusted NACHOS  Score",
    "Justification for Adjusted NACHOS  Score",
    "Unnecessary Extension ?",
    "Cross Entity Calculation ?",
    "Business Logic (Formula)",
    "Reason for Complexity",
    "Why Extension Is Necessary",
    "Recommendations",
]

# Mirrors the IN layout (ResourceName entity column, NACHOS score mid-
# sheet, `Adjusted Score` naming, long cross-entity header, no
# Justification / Complex Business Logic columns).
IN_HEADER = [
    "State",
    "Legislation",
    "Legislative Logic",
    "ResourceName",
    "Data Element",
    "Data Type",
    "Required",
    "Business Logic (Requirements)",
    "Is an extension",
    "Unnecessary Extension",
    "Is this a calculation?",
    "Reason for extension necessity logic",
    "NACHOS score",
    "Business Logic (Formula)",
    "Reason for Complexity",
    "In-Scope of Ed-Fi Data Standard focus",
    "Reason for In-Scope or Out-of/Scope",
    "Cross entity reference. If yes, then adjust score by +0.5",
    "Adjusted Score",
    "Recommendations",
]


def _std_row(
    state="Wisconsin",
    entity="Entity",
    element="element",
    nachos=0,
    adjusted=0.0,
    justification="x",
    complex_bl=None,
    unnecessary=None,
    cross=None,
    is_ext=None,
    decoy_adjusted=None,
):
    row = [None] * len(STANDARD_HEADER)
    row[0] = state
    row[2] = entity
    row[3] = element
    row[7] = complex_bl
    row[8] = nachos
    row[9] = adjusted
    row[10] = justification
    row[11] = unnecessary
    row[12] = cross
    row[17] = is_ext
    row[19] = decoy_adjusted
    return row


def _az_row(
    state="Arizona",
    entity="Entity",
    element="element",
    nachos=0,
    adjusted=0.0,
    justification=None,
    is_ext=None,
    unnecessary=None,
    cross=None,
):
    row = [None] * len(AZ_HEADER)
    row[0] = state
    row[2] = entity
    row[3] = element
    row[6] = is_ext
    row[8] = nachos
    row[9] = adjusted
    row[10] = justification
    row[11] = unnecessary
    row[12] = cross
    return row


def _in_row(
    state="Indiana",
    entity="Entity",
    element="element",
    nachos=0,
    adjusted=0.0,
    is_ext=None,
    unnecessary=None,
    cross=None,
):
    row = [None] * len(IN_HEADER)
    row[0] = state
    row[3] = entity
    row[4] = element
    row[8] = is_ext
    row[9] = unnecessary
    row[12] = nachos
    row[17] = cross
    row[18] = adjusted
    return row


def _write_workbook(
    path: Path,
    header: list,
    rows: list[list],
    sheet_name: str = "Details",
) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(header)
    for row in rows:
        padded = list(row) + [None] * (len(header) - len(row))
        ws.append(padded)
    wb.save(path)
    return path


def _make_reviewer_dir(
    tmp_path: Path,
    rows_by_key: dict[str, list[list]] | None = None,
) -> Path:
    """Write all five registry workbooks (minimal rows) into a tmp dir."""
    defaults: dict[str, tuple[list, list[list]]] = {
        "arizona": (AZ_HEADER, [_az_row(element="az-el")]),
        "wisconsin": (STANDARD_HEADER, [_std_row(element="wi-el")]),
        "minnesota": (
            ["es"] + STANDARD_HEADER[1:],
            [_std_row(state="Minnesota", element="mn-el")],
        ),
        "texas": (STANDARD_HEADER, [_std_row(state="Texas", element="tx-el")]),
        "indiana": (IN_HEADER, [_in_row(element="in-el")]),
    }
    directory = tmp_path / "human-scored-files"
    directory.mkdir()
    for key, (header, rows) in defaults.items():
        if rows_by_key and key in rows_by_key:
            rows = rows_by_key[key]
        _write_workbook(directory / _source(key).filename, header, rows)
    return directory


# ---------------------------------------------------------------------------
# Header normalization + column resolution
# ---------------------------------------------------------------------------


def test_normalize_header_collapses_all_whitespace():
    assert normalize_header("Adjusted NACHOS  Score") == "Adjusted NACHOS Score"
    assert normalize_header("Is \nExtension") == "Is Extension"
    assert normalize_header("  State  ") == "State"
    assert normalize_header(None) == ""


def test_resolve_columns_missing_declared_header_names_role():
    header = ["State", "Entity Name", "Data Elem"]  # renamed element col
    with pytest.raises(ValueError) as exc:
        resolve_columns(header, _source("wisconsin").headers, context="wi-test")
    msg = str(exc.value)
    assert "'Data Element'" in msg
    assert "'element'" in msg
    assert "wi-test" in msg


def test_resolve_columns_duplicate_declared_header_is_ambiguous():
    header = list(STANDARD_HEADER) + ["NACHOS score"]
    with pytest.raises(ValueError, match="appears 2 times"):
        resolve_columns(header, _source("wisconsin").headers, context="wi-test")


def test_resolve_columns_required_role_cannot_be_declared_absent():
    headers = dict(_source("wisconsin").headers)
    headers["nachos"] = None
    with pytest.raises(ValueError, match="required role 'nachos'"):
        resolve_columns(STANDARD_HEADER, headers, context="wi-test")
    assert "nachos" in REQUIRED_ROLES


def test_resolve_columns_decoy_msp_column_not_matched(tmp_path):
    """`Adjusted NACHOS  Score - Original MSP` must never satisfy the
    `adjusted` role — exact full-string matching after normalization."""
    path = _write_workbook(
        tmp_path / _source("wisconsin").filename,
        STANDARD_HEADER,
        [_std_row(adjusted=1.5, decoy_adjusted=99.0)],
    )
    records = load_reviewer_source(_source("wisconsin"), tmp_path)
    assert records[0].adjusted_nachos_score == 1.5
    assert path.exists()


# ---------------------------------------------------------------------------
# Per-source loading
# ---------------------------------------------------------------------------


def test_standard_layout_roles_land(tmp_path):
    _write_workbook(
        tmp_path / _source("wisconsin").filename,
        STANDARD_HEADER,
        [
            _std_row(
                entity="Calendar",
                element="calendarCode",
                nachos=0,
                adjusted=0.5,
                justification="0.5 Necessary extension",
                complex_bl="No",
                unnecessary="No",
                cross="Yes",
                is_ext="Yes",
            )
        ],
    )
    records = load_reviewer_source(_source("wisconsin"), tmp_path)
    assert len(records) == 1
    rec = records[0]
    assert rec.state == "WI"
    assert rec.entity == "Calendar"
    assert rec.element == "calendarCode"
    assert rec.nachos_score == 0
    assert rec.adjusted_nachos_score == 0.5
    assert rec.justification == "0.5 Necessary extension"
    assert rec.complex_business_logic == "No"
    assert rec.unnecessary_extension == "No"
    assert rec.cross_entity == "Yes"
    assert rec.is_extension == "Yes"


def test_az_layout_roles_land_and_complex_bl_is_none(tmp_path):
    """AZ's newline/`?`-suffixed headers resolve; the workbook has no
    Complex Business Logic column so the field is None by declaration."""
    _write_workbook(
        tmp_path / _source("arizona").filename,
        AZ_HEADER,
        [
            _az_row(
                entity="CourseTranscript",
                element="(ext/az) FinalLetterGradeDescriptor",
                nachos=2,
                adjusted=2.5,
                justification="why",
                is_ext="Yes",
                unnecessary="No",
                cross="No",
            )
        ],
    )
    records = load_reviewer_source(_source("arizona"), tmp_path)
    rec = records[0]
    assert rec.state == "AZ"
    assert rec.element == "(ext/az) FinalLetterGradeDescriptor"
    assert rec.is_extension == "Yes"
    assert rec.unnecessary_extension == "No"
    assert rec.cross_entity == "No"
    assert rec.complex_business_logic is None
    assert rec.adjusted_nachos_score == 2.5


def test_in_layout_roles_land_and_absent_columns_are_none(tmp_path):
    """IN: entity from ResourceName, `Adjusted Score` naming, long
    cross-entity header; no Justification / Complex-BL columns."""
    _write_workbook(
        tmp_path / _source("indiana").filename,
        IN_HEADER,
        [
            _in_row(
                entity="ed-fi/staffEducationOrganizationEmploymentAssociation",
                element="yearsOfPriorProfessionalExperience",
                nachos=0,
                adjusted=0.5,
                is_ext="Yes",
                unnecessary="No",
                cross="Yes",
            )
        ],
    )
    records = load_reviewer_source(_source("indiana"), tmp_path)
    rec = records[0]
    assert rec.state == "IN"
    assert rec.entity == "ed-fi/staffEducationOrganizationEmploymentAssociation"
    assert rec.element == "yearsOfPriorProfessionalExperience"
    assert rec.nachos_score == 0
    assert rec.adjusted_nachos_score == 0.5
    assert rec.cross_entity == "Yes"
    assert rec.justification is None
    assert rec.complex_business_logic is None


def test_mn_es_state_header_uses_fixed_state(tmp_path):
    """MN's State header cell is the corrupted literal `es` — the
    registry declares that literal so the (valid) column still resolves,
    and fixed-state semantics stamp MN regardless."""
    _write_workbook(
        tmp_path / _source("minnesota").filename,
        ["es"] + STANDARD_HEADER[1:],
        [_std_row(state="Minnesota", element="calendarCode")],
    )
    records = load_reviewer_source(_source("minnesota"), tmp_path)
    assert records[0].state == "MN"


def test_blank_state_cell_row_kept(tmp_path):
    """TX carries one row with a blank State cell but full identity —
    fixed-state semantics keep it (state never gates a row)."""
    _write_workbook(
        tmp_path / _source("texas").filename,
        STANDARD_HEADER,
        [_std_row(state=None, entity="CourseTranscriptExt", element="e")],
    )
    records = load_reviewer_source(_source("texas"), tmp_path)
    assert len(records) == 1
    assert records[0].state == "TX"


def test_state_cell_mismatch_warns_but_keeps_row(tmp_path, caplog):
    _write_workbook(
        tmp_path / _source("wisconsin").filename,
        STANDARD_HEADER,
        [_std_row(state="Texas", element="e1")],
    )
    with caplog.at_level("WARNING", logger="src.score.review_loader"):
        records = load_reviewer_source(_source("wisconsin"), tmp_path)
    assert len(records) == 1
    assert records[0].state == "WI"
    assert any(
        "Wisconsin" in m and "State cells differ" in m for m in caplog.messages
    )


def test_na_identity_rows_dropped_with_info_log(tmp_path, caplog):
    """Literal ``NA``/``N/A`` identity cells are unjoinable by
    construction (17 real WI cells: statusCode, patientIdentifier.*, …)
    and would sit in ``no_mc_row`` forever, deflating the match rate.
    They drop like blank cells, with an INFO count. ``NA`` in
    NON-identity cells (justification) is meaningful reviewer shorthand
    and must survive."""
    _write_workbook(
        tmp_path / _source("wisconsin").filename,
        STANDARD_HEADER,
        [
            _std_row(entity="NA", element="statusCode"),
            _std_row(entity="Entity", element="N/A"),
            _std_row(entity="na", element="element"),
            # Survivor — NA justification is not an identity cell.
            _std_row(entity="Entity", element="element", justification="NA"),
        ],
    )
    with caplog.at_level("INFO", logger="src.score.review_loader"):
        records = load_reviewer_source(_source("wisconsin"), tmp_path)
    assert len(records) == 1
    assert records[0].entity == "Entity"
    assert records[0].justification == "NA"
    assert any("dropped 3" in m and "unjoinable" in m for m in caplog.messages)


def test_blank_identity_rows_skipped_silently(tmp_path, caplog):
    _write_workbook(
        tmp_path / _source("wisconsin").filename,
        STANDARD_HEADER,
        [
            _std_row(entity=None, element="element"),
            _std_row(entity="Entity", element=None),
            _std_row(entity="", element=""),
        ],
    )
    with caplog.at_level("INFO", logger="src.score.review_loader"):
        records = load_reviewer_source(_source("wisconsin"), tmp_path)
    assert records == []
    # Rows with SOME populated identity-adjacent cell count as NA-drops;
    # here entity/element are the only gate — the first two rows carry a
    # populated sibling cell so they log, the fully-blank one doesn't.
    assert any("dropped 2" in m for m in caplog.messages)


def test_duplicate_keys_load_as_separate_records(tmp_path):
    """WI/TX/IN carry duplicate (entity, element) rows — each loads and
    joins independently (deduping would hide reviewer re-scores)."""
    _write_workbook(
        tmp_path / _source("wisconsin").filename,
        STANDARD_HEADER,
        [
            _std_row(entity="Entity", element="dup", nachos=1),
            _std_row(entity="Entity", element="dup", nachos=2),
        ],
    )
    records = load_reviewer_source(_source("wisconsin"), tmp_path)
    assert len(records) == 2
    assert {r.nachos_score for r in records} == {1, 2}


def test_whitespace_entity_element_trimmed(tmp_path):
    _write_workbook(
        tmp_path / _source("wisconsin").filename,
        STANDARD_HEADER,
        [_std_row(entity="  studentSchoolAssociation ", element="  schoolId  ")],
    )
    records = load_reviewer_source(_source("wisconsin"), tmp_path)
    assert records[0].entity == "studentSchoolAssociation"
    assert records[0].element == "schoolId"


def test_numeric_string_cells_coerced_and_non_numeric_none(tmp_path):
    _write_workbook(
        tmp_path / _source("texas").filename,
        STANDARD_HEADER,
        [
            _std_row(state="Texas", element="e1", nachos="2", adjusted="3.5"),
            _std_row(state="Texas", element="e2", nachos="NA", adjusted="N/A"),
            _std_row(state="Texas", element="e3", nachos=None, adjusted=None),
        ],
    )
    records = load_reviewer_source(_source("texas"), tmp_path)
    assert records[0].nachos_score == 2
    assert records[0].adjusted_nachos_score == 3.5
    assert records[1].nachos_score is None
    assert records[1].adjusted_nachos_score is None
    assert records[2].nachos_score is None
    assert records[2].adjusted_nachos_score is None


def test_missing_sheet_raises(tmp_path):
    _write_workbook(
        tmp_path / _source("wisconsin").filename,
        STANDARD_HEADER,
        [_std_row()],
        sheet_name="NotDetails",
    )
    with pytest.raises(KeyError, match="Details"):
        load_reviewer_source(_source("wisconsin"), tmp_path)


# ---------------------------------------------------------------------------
# All-five loading
# ---------------------------------------------------------------------------


def test_load_all_five_concatenates_in_registry_order(tmp_path):
    directory = _make_reviewer_dir(tmp_path)
    records = load_reviewer_records(directory)
    assert [r.state for r in records] == ["AZ", "WI", "MN", "TX", "IN"]
    assert [r.element for r in records] == [
        "az-el",
        "wi-el",
        "mn-el",
        "tx-el",
        "in-el",
    ]


def test_missing_files_raise_listing_every_absent_name(tmp_path):
    directory = _make_reviewer_dir(tmp_path)
    (directory / _source("texas").filename).unlink()
    (directory / _source("indiana").filename).unlink()
    with pytest.raises(FileNotFoundError) as exc:
        load_reviewer_records(directory)
    msg = str(exc.value)
    assert _source("texas").filename in msg
    assert _source("indiana").filename in msg
    assert "all-or-nothing" in msg
    assert "gitignored" in msg


def test_load_single_missing_source_raises_with_hint(tmp_path):
    with pytest.raises(FileNotFoundError, match="Phase E"):
        load_reviewer_source(_source("arizona"), tmp_path)


def test_reviewer_records_by_state_groups(tmp_path):
    directory = _make_reviewer_dir(
        tmp_path,
        rows_by_key={
            "wisconsin": [
                _std_row(element="e1"),
                _std_row(element="e3"),
            ],
        },
    )
    records = load_reviewer_records(directory)
    by_state = reviewer_records_by_state(records)
    assert [r.element for r in by_state["WI"]] == ["e1", "e3"]
    assert [r.element for r in by_state["AZ"]] == ["az-el"]
    assert [r.element for r in by_state["MN"]] == ["mn-el"]
    assert [r.element for r in by_state["TX"]] == ["tx-el"]
    assert [r.element for r in by_state["IN"]] == ["in-el"]


def test_registry_covers_supported_states_in_order():
    from src.states import SUPPORTED_STATES

    assert tuple(s.state for s in REVIEWER_SOURCES) == SUPPORTED_STATES


# ---------------------------------------------------------------------------
# Real-file round-trip — confirms the on-disk workbook set matches the
# 2026-07-07 basis change.
# ---------------------------------------------------------------------------

_ALL_FILES_PRESENT = all(s.path().exists() for s in REVIEWER_SOURCES)


@pytest.mark.realdata
@pytest.mark.skipif(
    not _ALL_FILES_PRESENT,
    reason="per-state reviewer workbooks not present on this checkout",
)
def test_real_files_state_counts_match_basis():
    """Pinned on the 2026-07-07 basis switch (docs/human-scored-files/):
    AZ 359 · WI 500 (518 raw − 18 blank/NA-identity) · MN 672 (the
    `Details` sheet, NOT `Details (2)`/787) · TX 794 (incl. the one
    blank-State row) · IN 362.

    Breakage here means a workbook changed version — update the
    ReviewerSource entry + this assertion intentionally, don't paper
    over.
    """
    records = load_reviewer_records()
    by_state = reviewer_records_by_state(records)
    assert len(by_state["AZ"]) == 359
    assert len(by_state["WI"]) == 500
    assert len(by_state["MN"]) == 672
    assert len(by_state["TX"]) == 794
    assert len(by_state["IN"]) == 362
    assert len(records) == 2687
    assert HUMAN_SCORED_DIR.name == "human-scored-files"


@pytest.mark.realdata
@pytest.mark.skipif(
    not _ALL_FILES_PRESENT,
    reason="per-state reviewer workbooks not present on this checkout",
)
def test_real_files_fill_rates_match_basis():
    """NACHOS + Adjusted are 100% filled on WI/MN/TX/IN; AZ ships 12
    unscored rows (347 of 359 scored)."""
    records = load_reviewer_records()
    by_state = reviewer_records_by_state(records)
    for state in ("WI", "MN", "TX", "IN"):
        assert all(r.nachos_score is not None for r in by_state[state])
        assert all(r.adjusted_nachos_score is not None for r in by_state[state])
    az_scored = [r for r in by_state["AZ"] if r.nachos_score is not None]
    assert len(az_scored) == 347
