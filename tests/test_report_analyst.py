"""Smoke tests for the analyst XLSX export.

No Texas data can leak here — `NACHOS Template.xlsx` is never read by the
module (we build from scratch). These tests just prove shape + row count +
genuinely-blank scoring columns against a synthetic single-state fixture.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import click
import openpyxl
import pytest

from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
    ReferenceInfo,
)
from src.models.element import ElementRecord, StateElements
from src.models.spine import SpineSourceURLs, StateSpine
from src.report import analyst


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self):
        assert not isinstance(analyst.run, click.Command)
        assert inspect.isfunction(analyst.run)


def _az_spine() -> StateSpine:
    ref = ReferenceInfo(
        entity="School",
        key_properties={"schoolId": PropertyInfo(type="integer", is_identity=True)},
    )
    calendar = EntityEntry(
        description="Calendar",
        domains=["SchoolCalendar"],
        properties={"calendarCode": PropertyInfo(max_length=60)},
        references={"schoolReference": ref},
    )
    school = EntityEntry(description="School", domains=["EducationOrganization"])
    ext = ExtensionEntry(
        extends_entity="Calendar",
        source_prefix="az",
        properties={"az_extra": PropertyInfo()},
    )
    catalog = EdFiCatalog(
        version="4.0.0",
        entity_count=2,
        extension_count=1,
        entities={"Calendar": calendar, "School": school},
        extensions={"CalendarExtension": ext},
    )
    return StateSpine(
        state="AZ",
        edfi_version="4.0.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(resources="https://example/AZ/resources/swagger.json"),
        catalog=catalog,
    )


def _az_elements() -> StateElements:
    records = [
        ElementRecord(
            state="AZ",
            edfi_version="4.0",
            domain="SchoolCalendar",
            entity="Calendar",
            element_name="calendarCode",
            data_type="string",
            definition_text="A unique code for the calendar.",
            business_rules_text="Must be unique within the school.",
            source="core",
            source_document="calendar.pdf",
            source_page_or_section="p.3",
            documented=True,
        ),
        ElementRecord(
            state="AZ",
            edfi_version="4.0",
            domain="EducationOrganization",
            entity="School",
            element_name="schoolId",
            data_type="integer",
            definition_text="Local school identifier.",
            source="core",
            documented=True,
        ),
    ]
    return StateElements(
        state="AZ",
        edfi_version="4.0",
        extracted_at=datetime.now(timezone.utc),
        element_count=len(records),
        elements=records,
    )


def _az_gap_log() -> dict:
    """Minimal gap-log fixture so the Score Card dual-coverage metrics
    render with concrete numbers rather than "(gap-log unavailable)".
    """
    return {
        "state": "AZ",
        "source_coverage": {"matched": 2, "total": 2, "pct": 100.0},
        "spine_coverage": {
            "matched_unique_keys": 2,
            "total_spine_keys": 100,
            "pct": 2.0,
        },
    }


def _write_az_fixture(tmp: Path) -> None:
    (tmp / "data" / "out").mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "spine").mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "out" / "az_elements_source.json").write_text(
        _az_elements().model_dump_json(indent=2), encoding="utf-8"
    )
    (tmp / "data" / "spine" / "az_spine.json").write_text(
        _az_spine().model_dump_json(indent=2), encoding="utf-8"
    )
    (tmp / "data" / "out" / "az_gap_log.json").write_text(
        json.dumps(_az_gap_log()), encoding="utf-8"
    )


def _col(ws, header: str) -> int:
    """1-based column index of ``header`` in row 1 of ``ws``.

    Column POSITIONS are pinned by the frozen spec snapshots in
    `tests/test_workbook_spec.py` + the structural fingerprint goldens;
    these tests assert VALUES by header name so a column reorder/rename
    (Option B) only touches the spec and its snapshot files.
    """
    headers = [
        ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)
    ]
    try:
        return headers.index(header) + 1
    except ValueError:
        raise AssertionError(
            f"header {header!r} not found in row 1 of sheet {ws.title!r}; "
            f"available headers: {headers}"
        ) from None


def _cell(ws, row: int, header: str):
    """Value at (``row``, column named ``header``) — lookup by name."""
    return ws.cell(row=row, column=_col(ws, header)).value


class TestRunSingleState:
    def test_produces_workbook_with_expected_sheets(self, tmp_path, monkeypatch):
        _write_az_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        produced = analyst.run(state="AZ", out_dir=tmp_path / "data" / "out")
        assert len(produced) == 1
        path = produced[0]
        assert path.name == "az_analyst.xlsx"

        wb = openpyxl.load_workbook(path)
        # Option B order: work sheets up front, lookup surfaces (Legend,
        # Methodology Notes) at the end; Update Log new at position 2.
        assert wb.sheetnames == [
            "Readme", "Update Log", "Score Card", "Details",
            "Entities by Domain", "References", "Legend",
            "Methodology Notes",
        ]

        # Readme sheet — self-documenting tab guide. 3 cols, header row +
        # one row per sheet ACTUALLY in the workbook (post Sequence-1
        # hygiene the Readme is generated from `wb.sheetnames`, including
        # a "Readme" self-row first, so it can never drift from the tabs),
        # then the "Working a review pass" tips block (issue #259 PR 1).
        readme = wb["Readme"]
        readme_headers = [readme.cell(row=1, column=c).value for c in range(1, 4)]
        assert readme_headers == ["Sheet", "What it shows", "Who it's for"]
        # Sanity-check a known row (slice: the tips block follows).
        last_sheet_row = 1 + len(wb.sheetnames)
        sheet_col = [_cell(readme, r, "Sheet") for r in range(2, last_sheet_row + 1)]
        assert sheet_col == wb.sheetnames
        # Tips block: one blank spacer row, then the section header.
        assert _cell(readme, last_sheet_row + 1, "Sheet") is None
        assert _cell(readme, last_sheet_row + 2, "Sheet") == "Working a review pass"
        assert readme.max_row == last_sheet_row + 2 + len(
            analyst._README_REVIEW_PASS_TIPS
        )
        assert "Score Card" in sheet_col
        assert "Details" in sheet_col
        assert "Methodology Notes" in sheet_col

        details = wb["Details"]
        # Column ORDER/shape is pinned by the frozen header snapshots in
        # tests/test_workbook_spec.py + the fingerprint goldens; here we
        # assert VALUES by header name only.

        # We wrote two records; max_row includes header.
        assert details.max_row == 3
        # Row 2 is the first data record — canonical sort by source-area then entity.
        # EducationOrganization (School/schoolId) sorts before SchoolCalendar (Calendar/calendarCode).
        # Row # is the stable 1-based canonical-order address.
        assert _cell(details, 2, "Row #") == 1
        assert _cell(details, 3, "Row #") == 2
        assert _cell(details, 2, "State") == "AZ"
        assert _cell(details, 2, "Entity Name") == "School"
        assert _cell(details, 2, "Data Element") == "schoolId"
        assert _cell(details, 3, "State") == "AZ"
        assert _cell(details, 3, "Entity Name") == "Calendar"
        assert _cell(details, 3, "Data Element") == "calendarCode"

        # Ed-Fi Domain populated from spine lookup.
        assert _cell(details, 2, "Ed-Fi Domain") == "EducationOrganization"  # School entity
        assert _cell(details, 3, "Ed-Fi Domain") == "SchoolCalendar"         # Calendar entity

        # Scoring columns are GENUINELY blank (not empty-string, not "NA").
        for header in (
            "Complex Business Logic",
            "Base NACHOS Score",
            "Adjusted NACHOS Score",
            "Score Adjustments",
            "Justification for Adjusted NACHOS Score",
            "Unnecessary Extension ?",
            "Cross Entity Calculation ?",
            "Reason for Complexity",
            "Multiple Entities Involved",
        ):
            assert _cell(details, 2, header) is None, (
                f"Details {header!r} row 2 leaked a value: "
                f"{_cell(details, 2, header)!r}"
            )

        # Analyst-input band is empty BY DESIGN (Option B band 3).
        for header in (
            "Required", "Recommendations", "Ed-Fi Comments",
            "DS Next Steps", "State Response", "KB Reviewed",
            "Reviewed with State", "Validated By",
        ):
            assert _cell(details, 2, header) is None, (
                f"analyst-input {header!r} row 2 leaked a value"
            )

        # Row 3 (Calendar) has the business-rules + references.
        assert _cell(details, 3, "Business Logic") == "A unique code for the calendar."
        assert _cell(details, 3, "Business Logic (Formula)") == "Must be unique within the school."
        assert _cell(details, 3, "AI: Match Status") == "Matched (core)"
        assert _cell(details, 3, "Is an extension") == "No"
        # Contributing extension — None for core.
        assert _cell(details, 3, "Contributing extension") is None
        assert _cell(details, 3, "References") == "calendar.pdf / p.3"
        # No regulatory citations in the fixture → Legislation blank.
        assert _cell(details, 3, "Legislation") is None

        # School entity has no extension and is core.
        assert _cell(details, 2, "AI: Match Status") == "Matched (core)"
        assert _cell(details, 2, "Is an extension") == "No"
        assert _cell(details, 2, "Contributing extension") is None

        # NO Texas rows.
        for r in range(2, details.max_row + 1):
            assert _cell(details, r, "State") != "Texas"

        # Update Log carries the workbook metadata (moved off Score Card
        # per Option B; #174 relabels).
        ul = wb["Update Log"]
        labels = [ul.cell(row=r, column=1).value for r in range(3, 18)]
        assert "State" in labels
        assert "API model entity count" in labels
        # Dual coverage labels + Source scope.
        assert "Source-document coverage" in labels
        assert "Ed-Fi API model coverage (of full UDM)" in labels
        assert "Source scope" in labels

        # References sheet has at least the one ref we created. (Full
        # header list pinned by the spec snapshots; presence-check the
        # load-bearing headers only.)
        ref = wb["References"]
        assert ref.max_row >= 2
        assert _col(ref, "Entity") and _col(ref, "Reference")

        # Entities by Domain populated from spine.
        ebd = wb["Entities by Domain"]
        assert ebd.max_row >= 2
        assert _col(ebd, "Domain") and _col(ebd, "Entity")


class TestPerRecordExtensionAttribution:
    """`Is an extension` column reads from `ElementRecord.source` (Phase 5a),
    not the entity-level `_is_extension_entity` heuristic. A core element on
    an entity that has an extension reads `No`; only elements explicitly
    attributed `source="extension"` read `Yes`."""

    def test_extension_record_renders_yes(self, tmp_path, monkeypatch):
        spine = _az_spine()
        records = [
            ElementRecord(
                state="AZ",
                edfi_version="4.0",
                domain="SchoolCalendar",
                entity="Calendar",
                element_name="az_extra",
                definition_text="extension-only field",
                source="extension",
                extension_name="az.CalendarExtension",
                documented=True,
            ),
        ]
        elements = StateElements(
            state="AZ",
            edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=1,
            elements=records,
        )
        (tmp_path / "data" / "out").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "spine").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "out" / "az_elements_source.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp_path / "data" / "spine" / "az_spine.json").write_text(
            spine.model_dump_json(indent=2), encoding="utf-8"
        )

        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=tmp_path / "data" / "out")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        assert _cell(details, 2, "AI: Match Status") == "Matched (extension)"
        assert _cell(details, 2, "Is an extension") == "Yes"
        assert _cell(details, 2, "Contributing extension") == "az.CalendarExtension"


class TestMatchStatusUnresolved:
    """Source="unknown" records render as `Unresolved` in the Match Status
    column so analysts don't mistake them for confirmed core/extension
    matches (reviewer 2 P1)."""

    def test_unknown_record_renders_unresolved(self, tmp_path, monkeypatch):
        spine = _az_spine()
        records = [
            ElementRecord(
                state="AZ",
                edfi_version="4.0",
                domain="SchoolCalendar",
                entity="Calendar",
                element_name="totallyMadeUpField",
                definition_text="unmatched",
                source="unknown",
                documented=True,
            ),
        ]
        elements = StateElements(
            state="AZ",
            edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=1,
            elements=records,
        )
        (tmp_path / "data" / "out").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "spine").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "out" / "az_elements_source.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp_path / "data" / "spine" / "az_spine.json").write_text(
            spine.model_dump_json(indent=2), encoding="utf-8"
        )

        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=tmp_path / "data" / "out")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        assert _cell(details, 2, "AI: Match Status") == "Unresolved"
        # Unknown rows still render "Is an extension = No" — the boolean
        # column only tracks extension-ness, not confidence.
        assert _cell(details, 2, "Is an extension") == "No"


class TestKnownLimitationsSheet:
    """The static `Known Limitations` sheet pre-empts analyst re-flagging of
    known-backlog items (reviewer 2 §Add a Known Limitations Sheet)."""

    def test_sheet_exists_with_expected_rows(self, tmp_path, monkeypatch):
        _write_az_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=tmp_path / "data" / "out")
        wb = openpyxl.load_workbook(path)
        # Sheet retitled "Methodology Notes" (Option B / #174).
        assert "Methodology Notes" in wb.sheetnames

        kl = wb["Methodology Notes"]
        # Header row + filtered limitation rows. Round 2.2 changed KL to
        # state-scoped filtering: AZ workbook shows only rows whose
        # `applies_to` is `"all"` or includes `"AZ"`.
        assert kl.cell(row=1, column=1).value == "Limitation"
        assert kl.cell(row=1, column=2).value == "Impact"
        assert kl.cell(row=1, column=3).value == "Why it exists"
        expected_rows = analyst._filter_known_limitations("AZ")
        assert kl.max_row == 1 + len(expected_rows)

        # Spot-check distinctive rows.
        column_a = [kl.cell(row=r, column=1).value for r in range(2, kl.max_row + 1)]
        assert (
            "In-scope NACHOS rows carry methodology scores; "
            "out-of-scope blank by design"
        ) in column_a
        assert "Source-scope limitations" in column_a
        # AZ-specific row is present; MN-specific rows are NOT.
        assert any("AZ-specific" in (v or "") for v in column_a)
        assert not any("MN-specific" in (v or "") for v in column_a)

    def test_terminology_rename_row_present(self, tmp_path, monkeypatch):
        """The issue-#174 terminology row is the one Methodology Notes row
        the workbook contract (CLAUDE.md) names explicitly. It silently
        vanished when a bad splice moved it out of `_KNOWN_LIMITATIONS`
        into `_elements_headers` (commit ace3c36; issue #211 item 1a) —
        no test asserted it, so 2,064 tests stayed green. This pin keeps
        it on every state's sheet (`applies_to = ("all",)`).
        """
        _write_az_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=tmp_path / "data" / "out")
        wb = openpyxl.load_workbook(path)
        kl = wb["Methodology Notes"]
        column_a = [
            kl.cell(row=r, column=1).value for r in range(2, kl.max_row + 1)
        ]
        assert (
            "Stakeholder terminology renamed 2026-07 (issue #174)"
        ) in column_a

    def test_elements_headers_shim_stays_callable(self):
        """`_elements_headers` was corrupted into a guaranteed TypeError by
        the same splice (issue #211 item 1a) while an archived brief
        still directs future work at it — keep the compat shim callable
        and spec-equivalent for both lenses.
        """
        for lens in ("source", "spine"):
            assert analyst._elements_headers(lens) == (
                analyst.AUDIT_TRAIL_SHEET.headers_for(lens)
            )


class TestSourceScopeAndDualCoverage:
    """Update Log must carry per-state `Source scope` narrative plus both
    coverage metrics (reviewer 1 Appendix A1/A2 + §5 communication
    blocker; moved off Score Card per Option B)."""

    def test_source_scope_and_coverage_values_present(self, tmp_path, monkeypatch):
        _write_az_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=tmp_path / "data" / "out")
        wb = openpyxl.load_workbook(path)
        ul = wb["Update Log"]
        rows = {
            ul.cell(row=r, column=1).value: ul.cell(row=r, column=2).value
            for r in range(3, ul.max_row + 1)
        }
        assert "Use Case 12.0" in (rows.get("Source scope") or "")
        assert rows.get("Source-document coverage") == "2/2 (100.0%)"
        assert rows.get("Ed-Fi API model coverage (of full UDM)") == "2/100 (2.0%)"


class TestFourStateCombinedWorkbook:
    """Phase 6.4: `analyst.run()` with no state arg produces per-state workbooks
    for every entry in `_STATES` PLUS a combined `coverage_analyst.xlsx`. With
    TX added, that's 4 per-state workbooks + 1 combined."""

    def _write_minimal_state(self, tmp: Path, state: str) -> None:
        spine = _az_spine()
        spine = spine.model_copy(update={"state": state})
        elements = ElementRecord(
            state=state,
            edfi_version="4.0",
            domain="SchoolCalendar",
            entity="Calendar",
            element_name="calendarCode",
            definition_text=f"{state} calendar code",
            source="core",
            documented=True,
        )
        state_elements = StateElements(
            state=state,
            edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=1,
            elements=[elements],
        )
        (tmp / "data" / "out").mkdir(parents=True, exist_ok=True)
        (tmp / "data" / "spine").mkdir(parents=True, exist_ok=True)
        (tmp / "data" / "out" / f"{state.lower()}_elements_source.json").write_text(
            state_elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp / "data" / "spine" / f"{state.lower()}_spine.json").write_text(
            spine.model_dump_json(indent=2), encoding="utf-8"
        )

    def test_run_all_states_produces_five_per_state_plus_combined(
        self, tmp_path, monkeypatch
    ):
        for state in ("AZ", "WI", "MN", "TX", "IN"):
            self._write_minimal_state(tmp_path, state)

        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        produced = analyst.run(out_dir=tmp_path / "data" / "out")
        names = sorted(p.name for p in produced)
        # 5 per-state + 1 combined = 6 workbooks.
        assert names == [
            "az_analyst.xlsx",
            "coverage_analyst.xlsx",
            "in_analyst.xlsx",
            "mn_analyst.xlsx",
            "tx_analyst.xlsx",
            "wi_analyst.xlsx",
        ]

        combined_path = tmp_path / "data" / "out" / "coverage_analyst.xlsx"
        wb = openpyxl.load_workbook(combined_path)
        details = wb["Details"]
        # Header + one row per state = 6 rows total.
        assert details.max_row == 6
        states_col = {_cell(details, r, "State") for r in range(2, 7)}
        assert states_col == {"AZ", "WI", "MN", "TX", "IN"}

        # Score Card cross-state summary lists all 5 states.
        sc = wb["Score Card"]
        sc_states = {sc.cell(row=r, column=1).value for r in range(4, 9)}
        assert sc_states == {"AZ", "WI", "MN", "TX", "IN"}


class TestSpineLensWorkbook:
    """Spine-lens-only workbook shape: Documented column + filtered sheet."""

    def _write_az_spine_fixture(self, tmp: Path) -> None:
        (tmp / "data" / "out").mkdir(parents=True, exist_ok=True)
        (tmp / "data" / "spine").mkdir(parents=True, exist_ok=True)
        # Three spine-lens rows: 2 documented (1 core, 1 extension-attributed),
        # 1 undocumented. The spine-lens pipeline may also append a hybrid
        # `source="unknown" documented=True` audit-trail row — include one.
        rows = [
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="SchoolCalendar",
                entity="Calendar", element_name="calendarCode",
                definition_text="doc",
                edfi_standard_definition="A unique code for the calendar.",
                source="core", documented=True,
            ),
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="EducationOrganization",
                entity="School", element_name="schoolId",
                definition_text="",
                edfi_standard_definition="The identifier assigned to a school by the SEA.",
                source="core", documented=False,
            ),
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="Audit Trail",
                entity="LegacyEntity", element_name="legacyField",
                definition_text="legacy", source="unknown", documented=True,
            ),
        ]
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(rows), elements=rows,
        )
        (tmp / "data" / "out" / "az_elements_spine.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp / "data" / "spine" / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp / "data" / "out" / "az_gap_log.json").write_text(
            json.dumps(_az_gap_log()), encoding="utf-8"
        )

    def test_details_sheet_has_documented_column(self, tmp_path, monkeypatch):
        self._write_az_spine_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(
            state="AZ", out_dir=tmp_path / "data" / "out", lens="spine",
        )
        assert path.name == "az_analyst_spine.xlsx"
        wb = openpyxl.load_workbook(path)
        headers = [c.value for c in wb["Details"][1]]
        # Presence only — the slot position is pinned by the spec snapshot.
        # (Column carries the "AI: " machine-annotation prefix, Option B.)
        assert "AI: Documented" in headers

    def test_documented_column_values_reflect_record_flag(self, tmp_path, monkeypatch):
        self._write_az_spine_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(
            state="AZ", out_dir=tmp_path / "data" / "out", lens="spine",
        )
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        # Map element name → documented cell value.
        by_name: dict[str, str] = {}
        for row_idx in range(2, details.max_row + 1):
            by_name[_cell(details, row_idx, "Data Element")] = \
                _cell(details, row_idx, "AI: Documented")
        assert by_name["calendarCode"] == "Yes"
        assert by_name["schoolId"] == "No"
        assert by_name["legacyField"] == "Yes"

    def test_documented_only_sheet_exists_and_filters_correctly(
        self, tmp_path, monkeypatch
    ):
        self._write_az_spine_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(
            state="AZ", out_dir=tmp_path / "data" / "out", lens="spine",
        )
        wb = openpyxl.load_workbook(path)
        assert "AZ — Documented only" in wb.sheetnames
        sheet = wb["AZ — Documented only"]
        # Header row shared with main Details sheet.
        main_headers = [c.value for c in wb["Details"][1]]
        filt_headers = [c.value for c in sheet[1]]
        assert filt_headers == main_headers
        # Only the one documented + source!='unknown' row (calendarCode).
        # legacyField has documented=True but source=='unknown', so filtered out.
        # schoolId has documented=False, filtered out.
        assert sheet.max_row - 1 == 1
        assert _cell(sheet, 2, "Data Element") == "calendarCode"

    def test_source_lens_workbook_has_no_documented_column(self, tmp_path, monkeypatch):
        # Belt-and-suspenders — column-count invariants in source lens matter
        # for downstream consumers.
        _write_az_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=tmp_path / "data" / "out")
        wb = openpyxl.load_workbook(path)
        headers = [c.value for c in wb["Details"][1]]
        assert "AI: Documented" not in headers
        assert "Documented only" not in " ".join(wb.sheetnames)

    def test_details_sheet_has_edfi_standard_definition_column(
        self, tmp_path, monkeypatch
    ):
        self._write_az_spine_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(
            state="AZ", out_dir=tmp_path / "data" / "out", lens="spine",
        )
        wb = openpyxl.load_workbook(path)
        headers = [c.value for c in wb["Details"][1]]
        # Presence only — the "between Data Type and Business Logic" slot
        # is pinned by the spec snapshot. (Column was renamed `Business
        # Logic (Redacted)` → `Business Logic` per issue #102 follow-on.)
        assert "Ed-Fi Standard Definition" in headers

    def test_edfi_standard_definition_cells_populated_from_record(
        self, tmp_path, monkeypatch
    ):
        self._write_az_spine_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(
            state="AZ", out_dir=tmp_path / "data" / "out", lens="spine",
        )
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        by_name: dict[str, object] = {}
        for row_idx in range(2, details.max_row + 1):
            by_name[_cell(details, row_idx, "Data Element")] = (
                _cell(details, row_idx, "Ed-Fi Standard Definition")
            )
        # Documented and undocumented rows both carry their spec definition.
        assert by_name["calendarCode"] == "A unique code for the calendar."
        assert by_name["schoolId"] == "The identifier assigned to a school by the SEA."
        # Hybrid-append audit-trail row has no spec definition.
        assert by_name["legacyField"] is None

    def test_filtered_sheet_inherits_edfi_definition_column(
        self, tmp_path, monkeypatch
    ):
        # The "Documented only" sheet shares the Details column layout,
        # so the new column rides along for free.
        self._write_az_spine_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(
            state="AZ", out_dir=tmp_path / "data" / "out", lens="spine",
        )
        wb = openpyxl.load_workbook(path)
        headers = [c.value for c in wb["AZ — Documented only"][1]]
        assert "Ed-Fi Standard Definition" in headers

    def test_source_lens_column_count_unchanged(self, tmp_path, monkeypatch):
        # Belt-and-braces shape pin (deliberately KEPT alongside the spec
        # snapshots): source-lens Details renders exactly 47 columns
        # (leading render-time Row # + 46 spec columns — Option B bands,
        # the Option C analyst-input columns, the #250 base override,
        # the #248 Effective Score + the #259 Review Why/Priority pair).
        # The full header ORDER lives in tests/test_workbook_spec.py;
        # this guards the rendered sheet's width end-to-end.
        _write_az_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=tmp_path / "data" / "out")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        assert details.max_column == 47
        headers = [c.value for c in details[1]]
        assert "Ed-Fi Standard Definition" not in headers  # spine-only


class TestPhaseDScoringIntegration:
    """Phase D: when a ``{state}_scores_{lens}.json`` sidecar is present
    on disk, Details cols 8-9 fill with complexity + NACHOS score, and a
    new ``Scores`` sheet appears with per-record dimension detail. When
    no sidecar is present, the pre-Phase-D invariant holds: cols 8-11
    blank, no Scores sheet.
    """

    def _write_scores_sidecar(
        self,
        out_dir: Path,
        state: str,
        lens: str,
        records: list[ElementRecord],
        *,
        in_scope: bool = True,
        nachos_tier: int = 0,
        nachos_rule: str = "tier_0_none",
        adjusted: float = 0.0,
        justification: str | None = "tier_0_none",
    ) -> None:
        scores = []
        for r in records:
            key = f"{state}|{r.entity}|{r.element_name}"
            if lens == "spine":
                dims = {
                    "documentation_completeness": {"value": 3, "confidence": "high"},
                    "obligation_clarity": {"value": 2, "confidence": "high"},
                    "business_logic_complexity": {"value": 1, "confidence": "high"},
                    "nachos_score": {
                        "value": nachos_tier,
                        "rule_matched": nachos_rule,
                        "confidence": "high",
                    },
                }
                complexity = 1
                per_record = 2.5
            else:
                dims = {
                    "canonical_name_alignment": {"value": 2, "confidence": "high"},
                    "definition_quality": {"value": 2, "confidence": "medium"},
                    "semantic_fidelity": {"value": 3, "confidence": "high"},
                    "extension_justification": {"value": None, "confidence": "high"},
                    # v23 — issue #106 (Q4): business_logic_complexity on
                    # source-lens, mirroring spine.
                    "business_logic_complexity": {"value": 1, "confidence": "high"},
                    "nachos_score": {
                        "value": nachos_tier,
                        "rule_matched": nachos_rule,
                        "confidence": "high",
                    },
                }
                complexity = 1
                per_record = 2.3
            scores.append({
                "record_key": key,
                "entity": r.entity,
                "element_name": r.element_name,
                "dimensions": dims,
                "_quality_mean_diagnostic": per_record,
                "complexity_score": complexity,
                "confidence_composite": "high",
                "review": {
                    "needs_review": True,
                    "reasons": ["low_confidence_dimension:definition_quality"],
                    "route": None,
                },
                # Phase F sidecar fields.
                "adjusted_nachos_score": adjusted,
                "in_scope": in_scope,
                "nachos_justification": justification,
            })
        path = out_dir / f"{state.lower()}_scores_{lens}.json"
        payload = {
            "state": state,
            "lens": lens,
            "record_count": len(records),
            "scored_count": len(records),
            "needs_review_count": len(records),
            "scores": scores,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_nachos_cells_filled_when_sidecar_present(self, tmp_path, monkeypatch):
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        self._write_scores_sidecar(out, "AZ", "source", records)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        # v10 — every row renders its tier 0-3. Default fixture: tier 0,
        # adj 0. Option B: the justification cell renders PROSE (raw
        # tokens survive on Audit Trail only).
        assert _cell(details, 2, "Base NACHOS Score") == 0
        assert _cell(details, 2, "Adjusted NACHOS Score") == 0.0
        assert (
            _cell(details, 2, "Justification for Adjusted NACHOS Score")
            == "Base 0 (no derivation logic)"
        )

    def test_scoring_cells_blank_when_sidecar_missing(self, tmp_path, monkeypatch):
        """Pre-Phase-D invariant: without a sidecar, scoring cols blank."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        for header in (
            "Complex Business Logic", "Base NACHOS Score", "Adjusted NACHOS Score",
        ):
            assert _cell(details, 2, header) is None
        assert "Scoring Summary" not in wb.sheetnames

    def test_scores_sheet_surfaces_dimensions_and_reasons(
        self, tmp_path, monkeypatch
    ):
        """Option B: Scoring Summary is RETIRED from per-state SOURCE
        workbooks (every column survives on Audit Trail); the review
        reasons it used to carry surface on Audit Trail's
        `review_reasons` column instead."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        self._write_scores_sidecar(out, "AZ", "source", records)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert "Scoring Summary" not in wb.sheetnames
        # Option D: the Audit Trail lives in its own on-demand workbook.
        assert "Audit Trail" not in wb.sheetnames
        from src.report.audit import run as run_audit

        audit_wb = openpyxl.load_workbook(
            run_audit(state="AZ", lens="source", out=out)
        )
        audit = audit_wb["Audit Trail"]
        # Two data rows = two records.
        assert audit.max_row == 3
        reasons = [_cell(audit, r, "review_reasons") for r in (2, 3)]
        for rx in reasons:
            assert "low_confidence_dimension:definition_quality" in rx

    def test_spine_lens_cols_fill_and_scores_sheet(self, tmp_path, monkeypatch):
        """Spine-lens uses a wider Details sheet; the complexity / score /
        adjusted / justification cells still fill from the sidecar."""
        records = _az_elements().elements
        elements = _az_elements()
        spine = _az_spine()
        out = tmp_path / "data" / "out"
        (out).mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "spine").mkdir(parents=True, exist_ok=True)
        (out / "az_elements_spine.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp_path / "data" / "spine" / "az_spine.json").write_text(
            spine.model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(json.dumps(_az_gap_log()), encoding="utf-8")
        self._write_scores_sidecar(out, "AZ", "spine", records)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="spine")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        # v10 — every row is in-scope and renders its tier 0-3.
        # Fixture defaults: tier 0, adj 0; prose justification (Option B).
        assert _cell(details, 2, "Complex Business Logic") == 1
        assert _cell(details, 2, "Base NACHOS Score") == 0
        assert _cell(details, 2, "Adjusted NACHOS Score") == 0.0
        assert (
            _cell(details, 2, "Justification for Adjusted NACHOS Score")
            == "Base 0 (no derivation logic)"
        )
        # Scores sheet includes the complexity_score + NACHOS columns for
        # spine lens. v10 — `in_scope` column dropped.
        scores = wb["Scoring Summary"]
        header = [
            scores.cell(row=1, column=c).value
            for c in range(1, scores.max_column + 1)
        ]
        assert "business_logic_complexity" in header
        assert "complexity_score" in header
        assert "nachos_score" in header
        assert "adjusted_nachos_score" in header
        assert "in_scope" not in header
        assert "nachos_justification" in header


class TestPhaseFNachosWorkbook:
    """Phase F — NACHOS methodology fills Details cols 10/11/12 on every
    row (v10 — methodology scope rectification 2026-04-26). Tier 0 is a
    valid score per `Logic_Dec2025` rows 19-21 ("Send granular element"
    / "Send a descriptor value"); pre-v10 blanking was a misreading of
    the spec that masked ~90% of rows."""

    def _write_scores_sidecar_in_scope(
        self,
        out_dir: Path,
        state: str,
        lens: str,
        records: list[ElementRecord],
    ) -> None:
        """Helper: seed a sidecar with tier-3 aggregation fill."""
        integration = TestPhaseDScoringIntegration()
        integration._write_scores_sidecar(
            out_dir,
            state,
            lens,
            records,
            in_scope=True,
            nachos_tier=3,
            nachos_rule="tier_3_aggregation",
            adjusted=3.0,
            justification="tier_3_aggregation",
        )

    def test_tier_3_row_fills_nachos_cells(self, tmp_path, monkeypatch):
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        self._write_scores_sidecar_in_scope(out, "AZ", "source", records)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        assert _cell(details, 2, "Base NACHOS Score") == 3
        assert _cell(details, 2, "Adjusted NACHOS Score") == 3.0
        # Prose justification (Option B); raw token on Audit Trail.
        assert (
            _cell(details, 2, "Justification for Adjusted NACHOS Score")
            == "Base 3 (requires aggregation)"
        )

    def test_tier_0_row_fills_nachos_cells_with_zero(self, tmp_path, monkeypatch):
        """v10 regression guard — tier-0 rows render their scalar, not
        blanks. Replaces pre-v10 ``test_out_of_scope_blanks_cols_9_10_11``
        which mis-implemented the methodology."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        # Default fixture: in_scope=True, tier=0, adj=0, justification "tier_0_none".
        integration = TestPhaseDScoringIntegration()
        integration._write_scores_sidecar(out, "AZ", "source", records)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        assert _cell(details, 2, "Base NACHOS Score") == 0
        assert _cell(details, 2, "Adjusted NACHOS Score") == 0.0
        assert (
            _cell(details, 2, "Justification for Adjusted NACHOS Score")
            == "Base 0 (no derivation logic)"
        )

    def test_score_card_has_nachos_block(self, tmp_path, monkeypatch):
        """Option B rebuilt the per-state Score Card: the NACHOS numbers
        now live in the adjusted-first frequency tables + Metrics list
        (documented-rows-scored-in-scope population); the old
        `NACHOS methodology` rollup sub-block is per-state-retired
        (survives on the combined workbook)."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        self._write_scores_sidecar_in_scope(out, "AZ", "source", records)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        sc = wb["Score Card"]
        col_a_values = [sc.cell(row=r, column=1).value for r in range(1, sc.max_row + 1)]
        assert (
            "Score frequency — Adjusted NACHOS Score (headline axis)"
            in col_a_values
        )
        assert "Score frequency — Base NACHOS Score" in col_a_values
        assert "Metrics" in col_a_values
        assert "Mean Adjusted NACHOS Score" in col_a_values
        assert "Mean Base NACHOS Score" in col_a_values
        assert "Domains × Adjusted NACHOS Score" in col_a_values
        assert "Pipeline diagnostics (AI) — all scored rows" in col_a_values
        # The old rollup NACHOS sub-block is gone from per-state.
        assert "NACHOS methodology" not in col_a_values
        # Both fixture rows are tier 3 / adjusted 3.0 — Metrics reflect it.
        by_label = {
            sc.cell(row=r, column=1).value: sc.cell(row=r, column=2).value
            for r in range(1, sc.max_row + 1)
        }
        assert by_label["Mean Adjusted NACHOS Score"] == "3.00"
        assert by_label["Mean Base NACHOS Score"] == "3.00"

    def test_elements_sheet_nachos_columns(self, tmp_path, monkeypatch):
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        self._write_scores_sidecar_in_scope(out, "AZ", "source", records)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        from src.report.audit import run as run_audit

        analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(
            run_audit(state="AZ", lens="source", out=out)
        )
        elements = wb["Audit Trail"]
        headers = [
            elements.cell(row=1, column=c).value
            for c in range(1, elements.max_column + 1)
        ]
        # v10 — `in_scope` column dropped; 4 NACHOS columns remain.
        assert "in_scope" not in headers
        assert "nachos_tier_rule" in headers
        assert "nachos_adjustments" in headers
        assert "nachos_score" in headers
        assert "adjusted_nachos_score" in headers
        # Value spot-check — tier-3 fixture renders rule label.
        assert _cell(elements, 2, "nachos_tier_rule") == "tier_3_aggregation"


class TestIntegrationProfileDetailsColumns:
    """Integration Profile trailing Details columns (Option B: carried in
    the far-right `AI:` machine-annotation band; #174 renamed Structural
    Depth → Implementation Shape):

    - AI: Implementation Shape — verbal label from underlying integer
      tier (Flat / Light / Moderate / Deep).
    - AI: Documentation Style — display label from the
      ``documentation_style`` classifier enum (Prescriptive /
      Conceptual / Cross-reference / Regulatory / Silent).
    - AI: Documentation Gap — "Yes" / "No" / None.
    - Documentation Gap Reason is RETIRED from the workbook (5%-populated
      noise per the consumability review); the composition logic survives
      in `_integration_profile_fields` and is pinned at helper level.
    """

    def _write_integration_profile_sidecar(
        self,
        out_dir: Path,
        state: str,
        lens: str,
        record: ElementRecord,
        *,
        struct_value: int | None,
        doc_value: int | None,
        doc_style: str | None,
        gap_value: int | None,
    ) -> None:
        """Seed a minimal scoring sidecar carrying Integration Profile dimension scalars."""
        key = f"{state}|{record.entity}|{record.element_name}"
        dims: dict = {
            "structural_depth": {
                "value": struct_value,
                "rule_matched": "stub",
                "inputs_used": {},
                "confidence": "high",
            },
            "documentation_style_tier": {
                "value": doc_value,
                "rule_matched": "stub",
                "inputs_used": {"documentation_style": doc_style},
                "confidence": "high",
            },
            "documentation_gap": {
                "value": gap_value,
                "rule_matched": "stub",
                "inputs_used": {
                    "structural_depth": struct_value,
                    "documentation_style_tier": doc_value,
                },
                "confidence": "high",
            },
        }
        payload = {
            "state": state,
            "lens": lens,
            "record_count": 1,
            "scored_count": 1,
            "needs_review_count": 0,
            "scores": [
                {
                    "record_key": key,
                    "entity": record.entity,
                    "element_name": record.element_name,
                    "dimensions": dims,
                    "_quality_mean_diagnostic": None,
                    "complexity_score": None,
                    "confidence_composite": "high",
                    "fact_provenance": {},
                    "review": {"needs_review": False, "reasons": [], "route": None},
                    "adjusted_nachos_score": None,
                    "in_scope": False,
                    "nachos_justification": None,
                }
            ],
        }
        (out_dir / f"{state.lower()}_scores_{lens}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_source_lens_trailing_cols_populate_from_sidecar(
        self, tmp_path, monkeypatch
    ):
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        # Gap-signal row: deep FK chain + unspecified narrative.
        first_record = next(
            r for r in _az_elements().elements
            if r.element_name == "calendarCode"
        )
        self._write_integration_profile_sidecar(
            out, "AZ", "source", first_record,
            struct_value=3, doc_value=0, doc_style="unspecified", gap_value=1,
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        # Find the calendarCode row by Data Element.
        row_idx = None
        for r in range(2, details.max_row + 1):
            if _cell(details, r, "Data Element") == "calendarCode":
                row_idx = r
                break
        assert row_idx is not None

        # Tier-3 structural + unspecified narrative renders as
        # Deep / Silent + Gap=Yes.
        assert _cell(details, row_idx, "AI: Implementation Shape") == "Deep"
        assert _cell(details, row_idx, "AI: Documentation Style") == "Silent"
        assert _cell(details, row_idx, "AI: Documentation Gap") == "Yes"
        # The Gap Reason column is retired from every workbook surface
        # (Details AND Audit Trail — the audit prefix is the Details
        # projection); the composed reason survives at helper level only.
        headers = [c.value for c in details[1]]
        assert "Documentation Gap Reason" not in headers
        from src.report.audit import run as run_audit

        audit_wb = openpyxl.load_workbook(
            run_audit(state="AZ", lens="source", out=out)
        )
        assert (
            "Documentation Gap Reason"
            not in [c.value for c in audit_wb["Audit Trail"][1]]
        )

    def test_gap_reason_composition_survives_at_helper_level(self):
        """Coverage replacement for the retired Documentation Gap Reason
        column: `_integration_profile_fields` still composes the
        "{Shape} · {Style}" reason when the gap fires."""
        score = {
            "dimensions": {
                "structural_depth": {"value": 3, "confidence": "high"},
                "documentation_style_tier": {
                    "value": 0,
                    "confidence": "high",
                    "inputs_used": {"documentation_style": "unspecified"},
                },
                "documentation_gap": {"value": 1, "confidence": "high"},
            },
        }
        shape, style, gap, reason = analyst._integration_profile_fields(score)
        assert (shape, style, gap) == ("Deep", "Silent", "Yes")
        assert reason == "Deep · Silent"

    def test_doc_gap_renders_no_when_gap_does_not_fire(
        self, tmp_path, monkeypatch
    ):
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        first_record = next(
            r for r in _az_elements().elements
            if r.element_name == "calendarCode"
        )
        self._write_integration_profile_sidecar(
            out, "AZ", "source", first_record,
            struct_value=2, doc_value=3, doc_style="prescriptive", gap_value=0,
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        row_idx = next(
            r for r in range(2, details.max_row + 1)
            if _cell(details, r, "Data Element") == "calendarCode"
        )
        # Moderate structural + Prescriptive narrative → Gap=No.
        assert _cell(details, row_idx, "AI: Implementation Shape") == "Moderate"
        assert _cell(details, row_idx, "AI: Documentation Style") == "Prescriptive"
        assert _cell(details, row_idx, "AI: Documentation Gap") == "No"

    def test_doc_gap_blank_when_dimension_unresolved(
        self, tmp_path, monkeypatch
    ):
        """When the second-pass rule could not evaluate (e.g., downgraded
        input dimension), Documentation Gap renders as None/blank — not
        ``"No"`` — so reviewers see the distinction. The Score Card's
        review block catches these via the low-confidence-dimension
        trigger."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        first_record = next(
            r for r in _az_elements().elements
            if r.element_name == "calendarCode"
        )
        self._write_integration_profile_sidecar(
            out, "AZ", "source", first_record,
            struct_value=2, doc_value=None, doc_style=None, gap_value=None,
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        row_idx = next(
            r for r in range(2, details.max_row + 1)
            if _cell(details, r, "Data Element") == "calendarCode"
        )
        # Structural=2 → Moderate label; doc style + gap unresolved →
        # both remaining columns blank.
        assert _cell(details, row_idx, "AI: Implementation Shape") == "Moderate"
        assert _cell(details, row_idx, "AI: Documentation Style") is None
        assert _cell(details, row_idx, "AI: Documentation Gap") is None

    def test_trailing_cols_blank_without_sidecar(self, tmp_path, monkeypatch):
        """Pre-sidecar invariant: when no scoring sidecar exists, the
        Integration Profile trailing cols stay blank. Matches the Phase
        D invariant for the NACHOS triplet."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        for r in range(2, details.max_row + 1):
            for header in (
                "AI: Implementation Shape", "AI: Documentation Style",
                "AI: Documentation Gap",
            ):
                assert _cell(details, r, header) is None


class TestLegacyTemplateFields:
    """Issue #102 wired four pre-blanked legacy template columns to the
    per-record sidecar (positions post Sequence-1 hygiene):
        - Unnecessary Extension ?  (col 13 src / 14 spine)
        - Cross Entity Calculation ? (col 14 src / 15 spine)
        - Complexity Signals       (col 16 src / 17 spine; renamed from
                                    `Reason for Complexity` in #107)
        - Multiple Entities Involved (col 17 src / 18 spine)

    Issue #107 corrected two source-of-truth mismatches:
    - `Unnecessary Extension ?` now reads the `+1 unnecessary_ext` /
      `+0.5 necessary_ext` adjustment label from `nachos_justification`
      (the same source-of-truth Phase F arithmetic uses) instead of
      `extension_justification.rule_matched` (which had a stricter
      mirror-required gate that under-reported "Yes" by 518 rows).
    - `Complexity Signals` blanks the cell when `nachos_score.value == 0`
      so reviewers don't see "concatenation" next to a tier-0 score on
      natural-key concat rows.
    """

    # Deliberately divergent from tests/factories.make_score: this is a
    # keyword-driven PARTIAL score dict (only the keys _legacy_template_fields
    # reads), not the full sidecar row shape.
    def _score(
        self,
        *,
        nachos_value: int = 0,
        nachos_justification: str = "tier_0_none",
        review_reasons: list[str] | None = None,
        has_aggregation: bool = False,
        has_concatenation: bool = False,
        has_cross_entity_logic_reconciled: bool = False,
        cross_entity_targets: int = 0,
        has_conditional_logic_reconciled: bool = False,
    ) -> dict:
        return {
            "nachos_justification": nachos_justification,
            "review": {
                "needs_review": bool(review_reasons),
                "reasons": review_reasons or [],
                "route": None,
            },
            "dimensions": {
                "nachos_score": {
                    "value": nachos_value,
                    "rule_matched": "tier_0_none",
                    "inputs_used": {
                        "has_aggregation": has_aggregation,
                        "has_concatenation": has_concatenation,
                        "has_cross_entity_logic__reconciled": (
                            has_cross_entity_logic_reconciled
                        ),
                        "has_conditional_logic__reconciled": (
                            has_conditional_logic_reconciled
                        ),
                        "cross_entity_targets": cross_entity_targets,
                    },
                },
            },
        }

    def test_returns_all_none_when_score_missing(self):
        assert analyst._legacy_template_fields(None, is_extension=False) == (
            None, None, None, None,
        )
        assert analyst._legacy_template_fields(None, is_extension=True) == (
            None, None, None, None,
        )

    # Bug 1 (#107): Unnecessary Extension reads the +1/+0.5 adjustment
    # label from nachos_justification, not extension_justification.rule_matched.
    def test_unnecessary_yes_when_justification_carries_plus1_label(self):
        score = self._score(
            nachos_justification="tier_0_none; +1 unnecessary_ext",
        )
        unn, *_ = analyst._legacy_template_fields(score, is_extension=True)
        assert unn == "Yes"

    def test_unnecessary_yes_with_compound_justification_string(self):
        # Real sidecars carry tier + multiple adj labels e.g.
        # "tier_1_conditional; +1 unnecessary_ext, +0.5 fidelity_divergent_explained"
        score = self._score(
            nachos_value=1,
            nachos_justification=(
                "tier_1_conditional; +1 unnecessary_ext, "
                "+0.5 fidelity_divergent_explained"
            ),
        )
        unn, *_ = analyst._legacy_template_fields(score, is_extension=True)
        assert unn == "Yes"

    def test_unnecessary_no_when_justification_carries_plus0_5_necessary(self):
        score = self._score(
            nachos_justification="tier_0_none; +0.5 necessary_ext",
        )
        unn, *_ = analyst._legacy_template_fields(score, is_extension=True)
        assert unn == "No"

    def test_unnecessary_blank_when_review_flags_unresolved(self):
        # Conservative +0.5 path: ext_necessary == None → +0.5 label PLUS
        # `extension_necessity_unresolved` review reason. Column should be
        # blank (None) so the Needs Review flag carries the signal — neither
        # "Yes" nor "No" misclassifies the missing verdict.
        score = self._score(
            nachos_justification="tier_0_none; +0.5 necessary_ext",
            review_reasons=["extension_necessity_unresolved"],
        )
        unn, *_ = analyst._legacy_template_fields(score, is_extension=True)
        assert unn is None

    def test_unnecessary_n_a_on_non_extension_row(self):
        # Non-extension rows render N/A regardless of justification content.
        score = self._score(nachos_justification="tier_0_none")
        unn, *_ = analyst._legacy_template_fields(score, is_extension=False)
        assert unn == "N/A"

    def test_unnecessary_blank_for_extension_with_no_adj_label(self):
        # Defensive — shouldn't happen given current aggregate code, but
        # we don't fabricate Yes/No when the justification is silent.
        score = self._score(nachos_justification="tier_0_none")
        unn, *_ = analyst._legacy_template_fields(score, is_extension=True)
        assert unn is None

    def test_cross_entity_calculation_reads_reconciled_input(self):
        true_score = self._score(has_cross_entity_logic_reconciled=True)
        false_score = self._score(has_cross_entity_logic_reconciled=False)
        _, ce_true, *_ = analyst._legacy_template_fields(true_score, is_extension=False)
        _, ce_false, *_ = analyst._legacy_template_fields(false_score, is_extension=False)
        assert ce_true == "Yes"
        assert ce_false == "No"

    def test_multi_entity_threshold_at_two(self):
        for n, expected in ((0, "No"), (1, "No"), (2, "Yes"), (3, "Yes"), (10, "Yes")):
            score = self._score(cross_entity_targets=n)
            *_, multi = analyst._legacy_template_fields(score, is_extension=False)
            assert multi == expected, f"targets={n} should render {expected!r}"

    # Bug 3 (#107): Complexity Signals blanks when nachos_score.value == 0.
    def test_complexity_signals_composes_label_from_true_flags(self):
        score = self._score(
            nachos_value=3,
            has_aggregation=True,
            has_conditional_logic_reconciled=True,
        )
        *_, signals, _ = analyst._legacy_template_fields(score, is_extension=False)
        assert signals == "aggregation; conditional logic"

    def test_complexity_signals_includes_all_four_signals_in_order(self):
        score = self._score(
            nachos_value=3,
            has_aggregation=True,
            has_concatenation=True,
            has_cross_entity_logic_reconciled=True,
            has_conditional_logic_reconciled=True,
        )
        *_, signals, _ = analyst._legacy_template_fields(score, is_extension=False)
        assert signals == (
            "aggregation; concatenation; cross-entity calculation; conditional logic"
        )

    def test_complexity_signals_blank_when_no_flag_fires(self):
        score = self._score()  # all flags default False, value defaults 0
        *_, signals, _ = analyst._legacy_template_fields(score, is_extension=False)
        assert signals is None

    def test_complexity_signals_blank_when_score_is_zero_even_if_flags_true(self):
        # Bug 3 — natural-key concatenation case: tier_0_natural_key_format
        # zero-rates the concat flag, so the column should be empty even
        # though has_concatenation is True. Without this gate the cell
        # would read "concatenation" alongside NACHOS=0, contradicting
        # the score from a reviewer's perspective.
        score = self._score(
            nachos_value=0,
            has_concatenation=True,
            has_cross_entity_logic_reconciled=True,
        )
        *_, signals, _ = analyst._legacy_template_fields(score, is_extension=False)
        assert signals is None

    def test_complexity_signals_falls_back_to_unreconciled_conditional(self):
        # Older sidecars may carry only `has_conditional_logic` without the
        # `__reconciled` suffix. Helper should still surface it. nachos_value
        # set non-zero so the empty-when-zero gate doesn't fire.
        score = {
            "nachos_justification": "tier_1_conditional",
            "dimensions": {
                "nachos_score": {
                    "value": 1,
                    "inputs_used": {"has_conditional_logic": True},
                },
            },
        }
        *_, signals, _ = analyst._legacy_template_fields(score, is_extension=False)
        assert signals == "conditional logic"

    def test_workbook_renders_derived_values_for_in_scope_row(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: a sidecar with aggregation/multi-entity/conditional
        signal renders the four legacy cols with derived values, not None."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        # Custom sidecar: tier-3 aggregation + cross-entity True (3 targets) +
        # conditional True. Non-extension rows in the fixture so col 12 → "N/A".
        scores = []
        for r in records:
            scores.append({
                "record_key": f"AZ|{r.entity}|{r.element_name}",
                "entity": r.entity,
                "element_name": r.element_name,
                "dimensions": {
                    "canonical_name_alignment": {"value": 3, "confidence": "high"},
                    "definition_quality": {"value": 2, "confidence": "high"},
                    "semantic_fidelity": {"value": 3, "confidence": "high"},
                    "extension_justification": {
                        "value": None,
                        "rule_matched": "not_applicable",
                        "confidence": "high",
                    },
                    "nachos_score": {
                        "value": 3,
                        "rule_matched": "tier_3_aggregation",
                        "confidence": "high",
                        "inputs_used": {
                            "has_aggregation": True,
                            "has_concatenation": False,
                            "has_cross_entity_logic__reconciled": True,
                            "has_conditional_logic__reconciled": True,
                            "cross_entity_targets": 3,
                        },
                    },
                },
                "complexity_score": None,
                "confidence_composite": "high",
                "review": {"needs_review": False, "reasons": [], "route": None},
                "adjusted_nachos_score": 4.5,
                "in_scope": True,
                "nachos_justification": "tier_3_aggregation",
            })
        (out / "az_scores_source.json").write_text(json.dumps({
            "state": "AZ", "lens": "source",
            "record_count": len(records),
            "scored_count": len(records),
            "needs_review_count": 0,
            "scores": scores,
        }), encoding="utf-8")
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        assert _cell(details, 2, "Unnecessary Extension ?") == "N/A"  # non-extension
        assert _cell(details, 2, "Cross Entity Calculation ?") == "Yes"
        # Option B restored the analysts' original column name (safe
        # again because the tier-0 blank gate holds — issue #107).
        assert _cell(details, 2, "Reason for Complexity") == (
            "aggregation; cross-entity calculation; conditional logic"
        )
        assert _cell(details, 2, "Multiple Entities Involved") == "Yes"  # 3 >= 2
        header_row = [c.value for c in details[1]]
        assert "Reason for Complexity" in header_row
        assert "Complexity Signals" not in header_row

    def test_workbook_blank_when_no_sidecar(self, tmp_path, monkeypatch):
        """Pre-Phase-D invariant: without a sidecar the four legacy cols
        stay blank end-to-end (helper short-circuits on score=None)."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        for r in range(2, details.max_row + 1):
            for header in (
                "Unnecessary Extension ?", "Cross Entity Calculation ?",
                "Reason for Complexity", "Multiple Entities Involved",
            ):
                assert _cell(details, r, header) is None


class TestNeedsReviewTrailingColumns:
    """Issue #113 — Reviewer View carries POC-3's own ``needs_review`` +
    ``review_route`` flags as trailing cols. NOT a reviewer-verdict
    surface — these read POC-3's own cascade output (low-confidence
    facts, hallucinated spans, unresolved verdicts) so reviewers can
    filter the Reviewer View to "what POC-3 asked for human eyes on"
    without flipping to the Review Queue sheet.
    """

    def _write_two_record_sidecar(
        self, out: Path, state: str, lens: str,
        records: list[ElementRecord],
    ) -> None:
        """Sidecar with one flagged + one not-flagged record so both code
        paths in `_review_cells` are exercised end-to-end."""
        scores = []
        for idx, r in enumerate(records):
            flagged = idx == 0
            review = (
                {
                    "needs_review": True,
                    "reasons": ["hallucinated_input:foo", "hallucinated_input:bar"],
                    "route": "POLICY",
                }
                if flagged
                else {"needs_review": False, "reasons": [], "route": None}
            )
            scores.append({
                "record_key": f"{state}|{r.entity}|{r.element_name}",
                "entity": r.entity,
                "element_name": r.element_name,
                "dimensions": {},
                "fact_provenance": {},
                "complexity_score": None,
                "confidence_composite": "high",
                "review": review,
                "in_scope": True,
                "adjusted_nachos_score": 0.0,
                "nachos_justification": "tier_0_none",
            })
        (out / f"{state.lower()}_scores_{lens}.json").write_text(
            json.dumps({
                "state": state, "lens": lens,
                "record_count": len(records), "scored_count": len(records),
                "needs_review_count": 1, "scores": scores,
            }),
            encoding="utf-8",
        )

    def test_source_lens_trailing_cols_yes_no_route(self, tmp_path, monkeypatch):
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        # Two-record fixture; first row flagged, second row clean.
        self._write_two_record_sidecar(out, "AZ", "source", records)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]

        # Rows are sorted by state/source-area/entity, so row 2/3
        # ordering depends on the fixture. Pull (Needs Review, Review
        # Route) cell pairs and verify both states surface correctly.
        seen: dict[tuple[str | None, str | None], int] = {}
        for r in range(2, details.max_row + 1):
            cells = (
                _cell(details, r, "AI: Needs Review"),
                _cell(details, r, "AI: Review Route"),
            )
            seen[cells] = seen.get(cells, 0) + 1
        # Exactly one row flagged Yes/POLICY, exactly one row No/blank.
        assert seen.get(("Yes", "POLICY")) == 1
        assert seen.get(("No", None)) == 1

    def test_spine_lens_trailing_cols_yes_no_route(self, tmp_path, monkeypatch):
        records = _az_elements().elements
        elements = _az_elements()
        spine = _az_spine()
        out = tmp_path / "data" / "out"
        out.mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "spine").mkdir(parents=True, exist_ok=True)
        (out / "az_elements_spine.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp_path / "data" / "spine" / "az_spine.json").write_text(
            spine.model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(json.dumps(_az_gap_log()), encoding="utf-8")
        self._write_two_record_sidecar(out, "AZ", "spine", records)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="spine")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        # Belt-and-braces shape pin (deliberately KEPT alongside the spec
        # snapshots): spine-lens Details renders exactly 49 columns
        # (leading render-time Row # + 48 spec columns — Option B bands,
        # the Option C analyst-input columns, the #250 base override,
        # the #248 Effective Score + the #259 Review Why/Priority pair).
        assert details.max_column == 49

        seen: dict[tuple[str | None, str | None], int] = {}
        for r in range(2, details.max_row + 1):
            cells = (
                _cell(details, r, "AI: Needs Review"),
                _cell(details, r, "AI: Review Route"),
            )
            seen[cells] = seen.get(cells, 0) + 1
        assert seen.get(("Yes", "POLICY")) == 1
        assert seen.get(("No", None)) == 1

    def test_unscored_row_renders_blank_cells(self, tmp_path, monkeypatch):
        """Pre-Phase-D invariant: without a sidecar, the trailing flag
        cells stay blank (unscored row → score=None → (None, None))."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        for r in range(2, details.max_row + 1):
            assert _cell(details, r, "AI: Needs Review") is None
            assert _cell(details, r, "AI: Review Route") is None

    def test_route_fallback_when_sidecar_lacks_route(self, tmp_path, monkeypatch):
        """Defensive: a sidecar that flags a row but doesn't carry the
        ``route`` field (e.g. a Phase-D-pre-route sidecar replayed from
        cache) should still render a route via ``route_review`` from
        the reasons. ``hallucinated_input:foo`` × 1 routes to SCORING."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        scores = []
        for r in records:
            scores.append({
                "record_key": f"AZ|{r.entity}|{r.element_name}",
                "entity": r.entity,
                "element_name": r.element_name,
                "dimensions": {},
                "fact_provenance": {},
                "complexity_score": None,
                "confidence_composite": "high",
                "review": {
                    "needs_review": True,
                    "reasons": ["hallucinated_input:foo"],
                    # `route` deliberately missing.
                },
                "in_scope": True,
                "adjusted_nachos_score": 0.0,
                "nachos_justification": "tier_0_none",
            })
        (out / "az_scores_source.json").write_text(json.dumps({
            "state": "AZ", "lens": "source",
            "record_count": len(records), "scored_count": len(records),
            "needs_review_count": len(records), "scores": scores,
        }), encoding="utf-8")
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        for r in range(2, details.max_row + 1):
            assert _cell(details, r, "AI: Needs Review") == "Yes"
            assert _cell(details, r, "AI: Review Route") == "SCORING"


class TestSourceLensComplexBusinessLogicColumn:
    """v23 / issue #106 (Q4 of POC closeout reviewer bundle) — source-
    lens Reviewer View carries `Complex Business Logic` (mirrors spine).
    Pre-v23 it was always-blank-and-dropped; v23 restores the column
    populated with the same 0-3 cost tier the rule has always produced.
    The cell is the per-row complexity_score from the aggregated sidecar;
    rendering blank when score is missing.
    """

    def test_scored_row_carries_cost_tier(self, tmp_path, monkeypatch):
        """The Complex Business Logic cell reads `complexity_score` from
        the sidecar."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        # _write_scores_sidecar seeds complexity_score=1 on source-lens
        # rows (post-v23). Confirm the cell renders that integer verbatim.
        TestPhaseDScoringIntegration()._write_scores_sidecar(
            out, "AZ", "source", records,
            in_scope=True, nachos_tier=2, nachos_rule="tier_2_aggregation",
            adjusted=2.0, justification="tier_2_aggregation",
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]

        # Both data rows carry the seeded complexity_score=1.
        for r in range(2, details.max_row + 1):
            assert _cell(details, r, "Complex Business Logic") == 1

    def test_unscored_row_renders_blank(self, tmp_path, monkeypatch):
        """Records missing from the sidecar render blank Complex Business
        Logic — same blank-on-unscored contract as the NACHOS cells.
        """
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        # No sidecar written — every row appears unscored.
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]

        for r in range(2, details.max_row + 1):
            assert _cell(details, r, "Complex Business Logic") is None


@pytest.mark.realdata
class TestLegacyColumnConsistencyAcrossRules:
    """Issue #107 — cross-rule consistency tests over real sidecars.

    The class of bug fixed in #107 (`Unnecessary Extension ?` reading a
    different source-of-truth than the score's +1/+0.5 weight) was not
    caught by #102's unit tests because those tests verified the helper's
    (sidecar field → column value) mapping in isolation. Both halves of
    each assertion were authored together, making them tautological.

    These tests load the real sidecars on disk and assert end-to-end
    properties between what `_compute_nachos_adjustments` writes
    (`nachos_justification` adjustment labels) and what the helper
    renders for display. If these don't line up, reviewers see a column
    that contradicts the score on the same row.

    The tests skip cleanly when sidecars are missing (gitignored, fresh
    clone before any `score aggregate` run). On dev machines with
    sidecars present the assertions guard the invariants directly.
    """

    _STATES = ("AZ", "WI", "MN", "TX", "IN")

    def _load_sidecar(self, state: str, lens: str) -> dict | None:
        path = (
            Path(__file__).resolve().parents[1]
            / "data" / "out" / f"{state.lower()}_scores_{lens}.json"
        )
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _is_extension(self, score: dict) -> bool:
        # Mirrors how `_details_row` derives `is_ext` for the helper:
        # extension_justification.rule_matched != "not_applicable" indicates
        # source-lens extensions; on spine-lens we infer from the +1/+0.5
        # extension adjustment label that aggregate writes only for
        # `view.source == "extension"` rows.
        dims = score.get("dimensions") or {}
        ej = dims.get("extension_justification") or {}
        ej_rule = ej.get("rule_matched")
        if ej_rule and ej_rule != "not_applicable":
            return True
        just = score.get("nachos_justification") or ""
        return "unnecessary_ext" in just or "necessary_ext" in just

    def test_unnecessary_extension_yes_iff_score_applies_plus1_unnecessary(self):
        """The `Unnecessary Extension ?` column reads "Yes" exactly when
        the score's justification includes `+1 unnecessary_ext`. This is
        the property #107 Bug 1 violated — under the prior implementation
        518 rows had the +1 weight applied but the column read "No".
        """
        checked_any = False
        for state in self._STATES:
            for lens in ("source", "spine"):
                data = self._load_sidecar(state, lens)
                if data is None:
                    continue
                checked_any = True
                for r in data["scores"]:
                    is_ext = self._is_extension(r)
                    unn, *_ = analyst._legacy_template_fields(r, is_ext)
                    just = r.get("nachos_justification") or ""
                    has_plus1 = "+1 unnecessary_ext" in just
                    if has_plus1:
                        assert unn == "Yes", (
                            f"{state}/{lens} {r.get('record_key')}: justification "
                            f"applies +1 unnecessary_ext but column rendered {unn!r}"
                        )
                    if unn == "Yes":
                        assert has_plus1, (
                            f"{state}/{lens} {r.get('record_key')}: column says "
                            f"'Yes' but justification {just!r} lacks +1 unnecessary_ext"
                        )
        if not checked_any:
            import pytest
            pytest.skip("no sidecars on disk; run `mc score aggregate` to populate")

    def test_unnecessary_no_iff_score_applies_plus0_5_necessary_and_resolved(self):
        """`Unnecessary Extension ? = "No"` corresponds to the resolved
        `+0.5 necessary_ext` path (LLM verdict True). The unresolved path
        also writes `+0.5 necessary_ext` but adds the
        `extension_necessity_unresolved` review reason — those rows must
        render blank, not "No", so the Needs Review flag carries the
        unresolved signal honestly.
        """
        checked_any = False
        for state in self._STATES:
            for lens in ("source", "spine"):
                data = self._load_sidecar(state, lens)
                if data is None:
                    continue
                checked_any = True
                for r in data["scores"]:
                    is_ext = self._is_extension(r)
                    unn, *_ = analyst._legacy_template_fields(r, is_ext)
                    just = r.get("nachos_justification") or ""
                    reasons = (r.get("review") or {}).get("reasons") or []
                    has_plus0_5 = "+0.5 necessary_ext" in just
                    is_unresolved = "extension_necessity_unresolved" in reasons
                    if unn == "No":
                        assert has_plus0_5 and not is_unresolved, (
                            f"{state}/{lens} {r.get('record_key')}: column 'No' "
                            f"requires +0.5 necessary_ext label AND no unresolved "
                            f"review flag (just={just!r}, reasons={reasons!r})"
                        )
                    if has_plus0_5 and is_unresolved and is_ext:
                        assert unn is None, (
                            f"{state}/{lens} {r.get('record_key')}: unresolved "
                            f"extension necessity should render blank, got {unn!r}"
                        )
        if not checked_any:
            import pytest
            pytest.skip("no sidecars on disk; run `mc score aggregate` to populate")

    def test_unnecessary_n_a_iff_non_extension(self):
        """`N/A` renders only on non-extension rows. Conversely, every
        extension row gets a non-N/A verdict (Yes / No / blank-unresolved).
        """
        checked_any = False
        for state in self._STATES:
            for lens in ("source", "spine"):
                data = self._load_sidecar(state, lens)
                if data is None:
                    continue
                checked_any = True
                for r in data["scores"]:
                    is_ext = self._is_extension(r)
                    unn, *_ = analyst._legacy_template_fields(r, is_ext)
                    if not is_ext:
                        assert unn == "N/A", (
                            f"{state}/{lens} {r.get('record_key')}: non-extension "
                            f"row should render 'N/A', got {unn!r}"
                        )
                    else:
                        assert unn != "N/A", (
                            f"{state}/{lens} {r.get('record_key')}: extension row "
                            f"should not render 'N/A'"
                        )
        if not checked_any:
            import pytest
            pytest.skip("no sidecars on disk; run `mc score aggregate` to populate")

    def test_complexity_signals_blank_when_score_is_zero(self):
        """Issue #107 Bug 3 — `Complexity Signals` must be blank whenever
        `nachos_score.value == 0`. Without this gate, natural-key
        concatenation rows render "concatenation" alongside a NACHOS
        score of 0, contradicting the score from a reviewer's perspective.
        """
        checked_any = False
        for state in self._STATES:
            for lens in ("source", "spine"):
                data = self._load_sidecar(state, lens)
                if data is None:
                    continue
                checked_any = True
                for r in data["scores"]:
                    dims = r.get("dimensions") or {}
                    ns = dims.get("nachos_score") or {}
                    value = ns.get("value")
                    if value != 0:
                        continue
                    is_ext = self._is_extension(r)
                    *_, signals, _ = analyst._legacy_template_fields(r, is_ext)
                    assert signals is None, (
                        f"{state}/{lens} {r.get('record_key')}: nachos_score=0 "
                        f"but Complexity Signals rendered {signals!r}"
                    )
        if not checked_any:
            import pytest
            pytest.skip("no sidecars on disk; run `mc score aggregate` to populate")


class TestFormulaGuard:
    def test_cells_starting_with_equals_are_stored_as_text(self, tmp_path, monkeypatch):
        """Regression: WI business_rules_text fields start with `=== Element-
        specific rules …`. Without the _set_cell data_type='s' guard, openpyxl
        writes these as formulas and Excel flags the workbook as corrupt
        ('Removed Records: Formula from /xl/worksheets/sheetN.xml').
        """
        spine = _az_spine()
        record = ElementRecord(
            state="WI",
            edfi_version="4.0",
            domain="Student",
            entity="Calendar",
            element_name="calendarCode",
            definition_text="ok",
            business_rules_text="=== Element-specific rules (from …) ===",
            documented=True,
        )
        elements = StateElements(
            state="WI",
            edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=1,
            elements=[record],
        )
        (tmp_path / "data" / "out").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "spine").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "out" / "wi_elements_source.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp_path / "data" / "spine" / "wi_spine.json").write_text(
            spine.model_dump_json(indent=2), encoding="utf-8"
        )

        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="WI", out_dir=tmp_path / "data" / "out")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        formula_cell = details.cell(
            row=2, column=_col(details, "Business Logic (Formula)")
        )
        assert formula_cell.data_type == "s"
        assert formula_cell.value == "=== Element-specific rules (from …) ==="


class TestScoreCardRollup:
    """Score Card gets a POC-2-style rollup block when a score sidecar is
    loaded. Per-state workbook: `metric | value` two-column. Combined
    workbook: `metric × (states, Cross)` matrix. Absent a sidecar, Score
    Card keeps its pre-Phase-D metadata-only shape (backfill invariant).
    """

    def _mixed_source_records(self) -> list[ElementRecord]:
        """Two core + one extension row — lets us exercise the partition."""
        return [
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="SchoolCalendar",
                entity="Calendar", element_name="calendarCode",
                definition_text="A unique code for the calendar.",
                business_rules_text="Must be unique within the school.",
                source="core", documented=True,
            ),
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="EducationOrganization",
                entity="School", element_name="schoolId",
                definition_text="Local school identifier.",
                source="core", documented=True,
            ),
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="SchoolCalendar",
                entity="Calendar", element_name="az_extra",
                definition_text="Extension field.",
                source="extension", documented=True,
                extension_name="az.CalendarExtension",
            ),
        ]

    def _write_mixed_sidecar(
        self, out_dir: Path, state: str, lens: str,
        records: list[ElementRecord],
    ) -> None:
        # Per-record scores chosen so the partition is detectable:
        #   core rows: 2.0 and 3.0  → mean 2.5
        #   extension row: 1.0
        per_records = {"calendarCode": 2.0, "schoolId": 3.0, "az_extra": 1.0}
        review_routes = {"calendarCode": None, "schoolId": None, "az_extra": "SCORING"}
        scores = []
        for r in records:
            if lens == "spine":
                dims = {
                    "documentation_completeness": {"value": 2, "confidence": "high"},
                    "obligation_clarity": {"value": 1, "confidence": "high"},
                    "business_logic_complexity": {"value": 1, "confidence": "high"},
                }
                complexity = 1
            else:
                dims = {
                    "canonical_name_alignment": {"value": 2, "confidence": "high"},
                    "definition_quality": {"value": 2, "confidence": "high"},
                    "semantic_fidelity": {"value": 3, "confidence": "high"},
                    "extension_justification": (
                        {"value": 1, "confidence": "high"}
                        if r.source == "extension"
                        else {"value": None, "confidence": "high"}
                    ),
                }
                complexity = None
            route = review_routes.get(r.element_name)
            scores.append({
                "record_key": f"{state}|{r.entity}|{r.element_name}",
                "entity": r.entity, "element_name": r.element_name,
                "dimensions": dims,
                "_quality_mean_diagnostic": per_records[r.element_name],
                "complexity_score": complexity,
                "confidence_composite": "high",
                "review": {
                    "needs_review": route is not None,
                    "reasons": [] if route is None else ["hallucinated_input:foo"],
                    "route": route,
                },
            })
        (out_dir / f"{state.lower()}_scores_{lens}.json").write_text(
            json.dumps({
                "state": state, "lens": lens,
                "record_count": len(records), "scored_count": len(records),
                "needs_review_count": sum(1 for s in scores if s["review"]["needs_review"]),
                "scores": scores,
            }),
            encoding="utf-8",
        )

    def test_per_state_rollup_block_renders_when_sidecar_loaded(
        self, tmp_path, monkeypatch
    ):
        out = tmp_path / "data" / "out"
        (out).mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "spine").mkdir(parents=True, exist_ok=True)
        records = self._mixed_source_records()
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(records), elements=records,
        )
        (out / "az_elements_source.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp_path / "data" / "spine" / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(json.dumps(_az_gap_log()), encoding="utf-8")
        self._write_mixed_sidecar(out, "AZ", "source", records)

        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")

        wb = openpyxl.load_workbook(path)
        sc = wb["Score Card"]
        rollup = {
            sc.cell(row=r, column=1).value: sc.cell(row=r, column=2).value
            for r in range(1, sc.max_row + 1)
        }
        # Option B retitled the per-state rollup block and scoped it as
        # pipeline diagnostics (all scored rows — NOT the headline
        # documented-only population).
        assert "Pipeline diagnostics (AI) — all scored rows" in rollup
        assert rollup["records_scored"] == 3
        # core / extension demographic counts remain (partition means
        # were demoted — the arithmetic mean of mixed-axis tiers was
        # format-confounded).
        assert rollup["core_count"] == 2
        assert rollup["extension_count"] == 1
        assert "mean_core_score" not in rollup
        assert "mean_extension_score" not in rollup
        assert "mean_per_record_score" not in rollup
        # Review routing: one SCORING flag, everything else 0
        assert rollup["route_SCORING"] == 1
        assert rollup["route_POLICY"] == 0
        assert rollup["route_DATA_MODEL"] == 0
        assert rollup["route_ANALYST"] == 0
        # Per-dimension means surface (source-lens: 4 dimensions).
        assert rollup["mean_canonical_name_alignment"] == "2.00"
        assert rollup["mean_semantic_fidelity"] == "3.00"
        # extension_justification only populated for the extension row → mean 1.00
        assert rollup["mean_extension_justification"] == "1.00"

    def test_per_state_rollup_omitted_when_sidecar_missing(
        self, tmp_path, monkeypatch
    ):
        """Backfill invariant: no sidecar → Score Card keeps metadata-only shape."""
        _write_az_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=tmp_path / "data" / "out")
        wb = openpyxl.load_workbook(path)
        sc = wb["Score Card"]
        labels = [
            sc.cell(row=r, column=1).value
            for r in range(1, sc.max_row + 1)
        ]
        assert all(
            not (isinstance(v, str) and "Pipeline diagnostics" in v)
            for v in labels
        )
        # Issue #248 Part A: the override-clustering block is likewise
        # invisible without a sidecar (no curation loop without scores).
        assert all(
            not (isinstance(v, str) and "Override clustering" in v)
            for v in labels
        )

    def test_spine_lens_rollup_includes_complexity(self, tmp_path, monkeypatch):
        out = tmp_path / "data" / "out"
        (out).mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "spine").mkdir(parents=True, exist_ok=True)
        records = self._mixed_source_records()
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(records), elements=records,
        )
        (out / "az_elements_spine.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp_path / "data" / "spine" / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(json.dumps(_az_gap_log()), encoding="utf-8")
        self._write_mixed_sidecar(out, "AZ", "spine", records)

        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="spine")

        wb = openpyxl.load_workbook(path)
        sc = wb["Score Card"]
        rollup = {
            sc.cell(row=r, column=1).value: sc.cell(row=r, column=2).value
            for r in range(1, sc.max_row + 1)
        }
        assert "Pipeline diagnostics (AI) — all scored rows" in rollup
        # Spine rollup includes mean_complexity_score. Fixture complexity=1 on
        # every row → mean 1.00.
        assert rollup["mean_complexity_score"] == "1.00"
        # Spine-lens dimensions only (no extension_justification etc).
        assert "mean_documentation_completeness" in rollup
        assert "mean_obligation_clarity" in rollup
        assert "mean_canonical_name_alignment" not in rollup

    def test_combined_workbook_rollup_matrix(self, tmp_path, monkeypatch):
        """Combined (coverage) workbook renders metric × (states, Cross)
        matrix. Cross column is record-count-weighted."""
        out = tmp_path / "data" / "out"
        (out).mkdir(parents=True, exist_ok=True)
        spine_dir = tmp_path / "data" / "spine"
        spine_dir.mkdir(parents=True, exist_ok=True)

        # Only AZ + WI get sidecars; MN + TX are sidecar-less so the
        # placeholder-row path is exercised.
        for state in ("AZ", "WI", "MN", "TX", "IN"):
            records = [
                ElementRecord(
                    state=state, edfi_version="4.0", domain="SchoolCalendar",
                    entity="Calendar", element_name="calendarCode",
                    definition_text=f"{state} calendar code",
                    source="core", documented=True,
                ),
            ]
            elements = StateElements(
                state=state, edfi_version="4.0",
                extracted_at=datetime.now(timezone.utc),
                element_count=1, elements=records,
            )
            (out / f"{state.lower()}_elements_source.json").write_text(
                elements.model_dump_json(indent=2), encoding="utf-8"
            )
            spine = _az_spine().model_copy(update={"state": state})
            (spine_dir / f"{state.lower()}_spine.json").write_text(
                spine.model_dump_json(indent=2), encoding="utf-8"
            )

        # Build sidecars only for AZ + WI.
        for state in ("AZ", "WI"):
            records = [
                ElementRecord(
                    state=state, edfi_version="4.0", domain="SchoolCalendar",
                    entity="Calendar", element_name="calendarCode",
                    definition_text="x", source="core", documented=True,
                ),
            ]
            (out / f"{state.lower()}_scores_source.json").write_text(
                json.dumps({
                    "state": state, "lens": "source",
                    "record_count": 1, "scored_count": 1, "needs_review_count": 0,
                    "scores": [{
                        "record_key": f"{state}|Calendar|calendarCode",
                        "entity": "Calendar", "element_name": "calendarCode",
                        "dimensions": {
                            "canonical_name_alignment": {"value": 2},
                            "definition_quality": {"value": 2},
                            "semantic_fidelity": {"value": 3},
                            "extension_justification": {"value": None},
                        },
                        "_quality_mean_diagnostic": (2.0 if state == "AZ" else 3.0),
                        "complexity_score": None,
                        "confidence_composite": "high",
                        "review": {"needs_review": False, "reasons": [], "route": None},
                    }],
                }),
                encoding="utf-8",
            )

        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)
        produced = analyst.run(out_dir=out, lens="source")
        combined = next(p for p in produced if p.name == "coverage_analyst.xlsx")
        wb = openpyxl.load_workbook(combined)
        sc = wb["Score Card"]

        # Locate the rollup header row, then read column 1 (metric) + 2..5
        # (AZ, WI, MN, TX) + 6 (Cross).
        rollup_row = None
        for r in range(1, sc.max_row + 1):
            v = sc.cell(row=r, column=1).value
            if isinstance(v, str) and v.startswith("Scoring rollup — cross-state"):
                rollup_row = r
                break
        assert rollup_row is not None, "combined Score Card missing rollup header"
        header_row = rollup_row + 1
        headers = [sc.cell(row=header_row, column=c).value for c in range(1, 8)]
        assert headers == ["metric", "AZ", "WI", "MN", "TX", "IN", "Cross"]

        # Build metric → row dict starting after the header row.
        by_metric = {}
        for r in range(header_row + 1, sc.max_row + 1):
            label = sc.cell(row=r, column=1).value
            if not label:
                break
            by_metric[label] = [sc.cell(row=r, column=c).value for c in range(2, 8)]

        # AZ+WI scored; MN+TX+IN unscored so they read 0; Cross sums to 2.
        assert by_metric["records_scored"] == [1, 1, 0, 0, 0, 2]
        # `mean_per_record_score` was demoted — the arithmetic mean of
        # mixed-axis tiers was format-confounded and invited the same
        # conflation the workbook's NACHOS-score column had to shed.
        # Per-dimension means remain (recomputed from paired records).
        assert "mean_per_record_score" not in by_metric
        assert "mean_core_score" not in by_metric
        assert "mean_extension_score" not in by_metric
        # Cross route counts tally across all states (0 for everything here).
        assert by_metric["route_POLICY"] == [0, 0, 0, 0, 0, 0]


class TestElementsSheet:
    """Wide `Elements` sheet — Details columns + per-fact + per-dim +
    rule_paths + downgraded_facts + review routing on every record.

    Mirrors POC-2's Elements tab intent: analysts can audit the
    "inputs → rule → tier → score" chain for any element without leaving
    the workbook. Sheet renders only when a sidecar is loaded; every
    Details row is present (unscored rows have blank scoring cells).
    """

    def _two_records_one_extension(self) -> list[ElementRecord]:
        return [
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="SchoolCalendar",
                entity="Calendar", element_name="calendarCode",
                definition_text="A unique code for the calendar.",
                business_rules_text="Must be unique within the school.",
                source="core", documented=True,
            ),
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="SchoolCalendar",
                entity="Calendar", element_name="az_extra",
                definition_text="Extension field.",
                source="extension", documented=True,
                extension_name="az.CalendarExtension",
            ),
        ]

    def _write_provenance_sidecar(
        self, out_dir: Path, state: str, lens: str,
        records: list[ElementRecord],
    ) -> None:
        """Sidecar with rich fact_provenance + downgrade flags so the
        Elements row builder is fully exercised (rule_paths +
        downgraded_facts both populate)."""
        scores = []
        for r in records:
            if lens == "spine":
                fact_provenance = {
                    "definition_present": {"value": True, "confidence": "high",
                                            "downgraded": False, "downgrade_reason": None},
                    "business_rules_present": {"value": False, "confidence": "high",
                                                "downgraded": False, "downgrade_reason": None},
                    "data_type_canonical": {"value": True, "confidence": "high",
                                             "downgraded": False, "downgrade_reason": None},
                    # A genuine downgrade — surfaces in `downgraded_facts`.
                    "definition_is_implementable": {
                        "value": None, "confidence": "low",
                        "downgraded": True, "downgrade_reason": "hallucinated_span",
                    },
                    "descriptor_values_enumerated": {"value": False, "confidence": "high",
                                                     "downgraded": False, "downgrade_reason": None},
                    "required_when_stated": {"value": False, "confidence": "high",
                                              "downgraded": False, "downgrade_reason": None},
                    "conditional_reporting_stated": {"value": False, "confidence": "high",
                                                      "downgraded": False, "downgrade_reason": None},
                    "populations_or_scope_stated": {"value": False, "confidence": "high",
                                                     "downgraded": False, "downgrade_reason": None},
                    "has_conditional_logic": {"value": False, "confidence": "high",
                                               "downgraded": False, "downgrade_reason": None},
                    "has_aggregation": {"value": False, "confidence": "high",
                                         "downgraded": False, "downgrade_reason": None},
                    "has_cross_entity_logic": {"value": False, "confidence": "high",
                                                "downgraded": False, "downgrade_reason": None},
                    "cross_entity_targets": {"value": 0, "confidence": "high",
                                              "downgraded": False, "downgrade_reason": None},
                    # Productization-signal fact — carries validated
                    # LLM-evidence spans that the Elements sheet
                    # surfaces in the adjacent `integration_class_spans`
                    # column.
                    "integration_class": {
                        "value": "concatenation", "confidence": "high",
                        "downgraded": False, "downgrade_reason": None,
                        "spans": [
                            "CONCATENATE LEAID-SchoolId-CalendarTypeCode-Sequence",
                        ],
                    },
                    # Observability-only on spine — not a spine rule
                    # input (see ``rules.LENS_OBSERVABILITY_FACTS``),
                    # but surfaced on the spine workbook alongside the
                    # productization-signal label so analysts see both
                    # verdicts + their verbatim evidence without
                    # sheet-hopping.
                    "semantic_class": {
                        "value": "aligned", "confidence": "medium",
                        "downgraded": False, "downgrade_reason": None,
                        "spans": ["Matches the Ed-Fi calendar code contract."],
                    },
                }
                dimensions = {
                    "documentation_completeness": {
                        "value": 2, "rule_matched": "tier_2_structure",
                        "inputs_used": {}, "confidence": "high",
                    },
                    "obligation_clarity": {
                        "value": 0, "rule_matched": "tier_0_none",
                        "inputs_used": {}, "confidence": "high",
                    },
                    "business_logic_complexity": {
                        "value": 0, "rule_matched": "tier_0_none",
                        "inputs_used": {}, "confidence": "high",
                    },
                }
                per_record = 1.0
                complexity = 0
            else:
                fact_provenance = {
                    "element_name_matches_canonical": {"value": True, "confidence": "high",
                                                        "downgraded": False, "downgrade_reason": None},
                    "naming_deviation_cosmetic": {"value": False, "confidence": "high",
                                                   "downgraded": False, "downgrade_reason": None},
                    "extension_mirrors_core_pattern": {"value": False, "confidence": "high",
                                                        "downgraded": False, "downgrade_reason": None},
                    "definition_present": {"value": True, "confidence": "high",
                                            "downgraded": False, "downgrade_reason": None},
                    "definition_text_substantive": {"value": True, "confidence": "high",
                                                     "downgraded": False, "downgrade_reason": None},
                    "definition_adds_detail_beyond_edfi": {"value": False, "confidence": "high",
                                                            "downgraded": False, "downgrade_reason": None},
                    # count_span_mismatch downgrade on the LABEL, but
                    # individually validated spans still flow (the
                    # filter drops valid=False entries, not the whole
                    # list) — keeps the "downgraded facts can still
                    # carry span evidence" contract tested on the
                    # workbook surface.
                    "semantic_class": {
                        "value": None, "confidence": "low",
                        "downgraded": True, "downgrade_reason": "count_span_mismatch",
                        "spans": ["The state narrows the Ed-Fi definition to the SBOE-approved subset."],
                    },
                    "state_scope_delta": {"value": "neutral", "confidence": "high",
                                            "downgraded": False, "downgrade_reason": None},
                    # Filtered (not downgraded) for core rows — should NOT appear in downgraded_facts.
                    "extension_is_necessary": {
                        "value": None, "confidence": "high",
                        "downgraded": False, "downgrade_reason": "filtered_by_source",
                    },
                    "extension_is_standalone": {
                        "value": None, "confidence": "high",
                        "downgraded": False, "downgrade_reason": "filtered_by_source",
                    },
                    "integration_class": {
                        "value": "sis_native", "confidence": "medium",
                        "downgraded": False, "downgrade_reason": None,
                        "spans": ["The first date the track is valid."],
                    },
                }
                dimensions = {
                    "canonical_name_alignment": {
                        "value": 3, "rule_matched": "tier_3_exact",
                        "inputs_used": {}, "confidence": "high",
                    },
                    "definition_quality": {
                        "value": 2, "rule_matched": "tier_2_substantive",
                        "inputs_used": {}, "confidence": "high",
                    },
                    "semantic_fidelity": {
                        "value": None, "rule_matched": "downgraded",
                        "inputs_used": {}, "confidence": "low",
                    },
                    "extension_justification": {
                        "value": None, "rule_matched": "not_applicable",
                        "inputs_used": {}, "confidence": "high",
                    },
                }
                per_record = 2.5
                complexity = None
            scores.append({
                "record_key": f"{state}|{r.entity}|{r.element_name}",
                "entity": r.entity, "element_name": r.element_name,
                "dimensions": dimensions,
                "_quality_mean_diagnostic": per_record,
                "complexity_score": complexity,
                "confidence_composite": "medium",
                "fact_provenance": fact_provenance,
                "review": {
                    "needs_review": True,
                    "reasons": ["hallucinated_input:foo"],
                    "route": "SCORING",
                },
            })
        (out_dir / f"{state.lower()}_scores_{lens}.json").write_text(
            json.dumps({
                "state": state, "lens": lens,
                "record_count": len(records), "scored_count": len(records),
                "needs_review_count": len(records),
                "scores": scores,
            }),
            encoding="utf-8",
        )

    def _write_inputs(self, tmp_path: Path, lens: str,
                      records: list[ElementRecord]) -> None:
        out = tmp_path / "data" / "out"
        out.mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "spine").mkdir(parents=True, exist_ok=True)
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(records), elements=records,
        )
        suffix = "_spine" if lens == "spine" else "_source"
        (out / f"az_elements{suffix}.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp_path / "data" / "spine" / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(json.dumps(_az_gap_log()), encoding="utf-8")

    def test_sheet_absent_when_no_sidecar(self, tmp_path, monkeypatch):
        _write_az_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=tmp_path / "data" / "out")
        wb = openpyxl.load_workbook(path)
        assert "Audit Trail" not in wb.sheetnames

    def test_source_lens_headers_and_fact_values(self, tmp_path, monkeypatch):
        records = self._two_records_one_extension()
        self._write_inputs(tmp_path, "source", records)
        out = tmp_path / "data" / "out"
        self._write_provenance_sidecar(out, "AZ", "source", records)

        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        from src.report.audit import run as run_audit

        analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(
            run_audit(state="AZ", lens="source", out=out)
        )
        assert "Audit Trail" in wb.sheetnames
        ws = wb["Audit Trail"]
        # Belt-and-braces shape pin (deliberately KEPT alongside the spec
        # snapshots): source-lens Audit Trail renders exactly 64 columns
        # (leading Row # + the 63-column factory output: Details
        # projection prefix + fact/span/dim/NACHOS/review blocks). Block
        # ORDER lives in tests/test_workbook_spec.py
        # (TestAuditTrailFactory); this guards the rendered width.
        assert ws.max_column == 64
        # All Details rows present (2 here) + header row = 3.
        assert ws.max_row == 3
        # Spot-check fact + dim cell values from the calendarCode (core)
        # row — row 3 under the canonical sort (az_extra < calendarCode).
        assert _cell(ws, 3, "Data Element") == "calendarCode"
        # element_name_matches_canonical = True ⇒ "True"
        assert _cell(ws, 3, "element_name_matches_canonical") == "True"
        # canonical_name_alignment dim = 3
        assert _cell(ws, 3, "canonical_name_alignment") == 3
        # rule_paths concatenates all four dim:rule pairs.
        rp = _cell(ws, 3, "rule_paths")
        assert "canonical_name_alignment:tier_3_exact" in rp
        assert "extension_justification:not_applicable" in rp
        # downgraded_facts surfaces only `downgraded=True` facts —
        # `semantic_class:count_span_mismatch` should be there;
        # `extension_is_necessary` (filtered_by_source, not downgraded)
        # should NOT.
        dg = _cell(ws, 3, "downgraded_facts")
        assert "semantic_class:count_span_mismatch" in dg
        assert "extension_is_necessary" not in dg
        # review_route propagates.
        assert _cell(ws, 3, "review_route") == "SCORING"
        # integration_class label + companion spans column carry the
        # productization-signal evidence the sidecar seeded.
        assert _cell(ws, 3, "integration_class") == "sis_native"
        assert (
            _cell(ws, 3, analyst._INTEGRATION_CLASS_SPANS_HEADER)
            == "The first date the track is valid."
        )
        # semantic_class divergence evidence — even though the LABEL
        # was downgraded for count_span_mismatch, the individually
        # validated span survives the filter and surfaces here.
        assert (
            _cell(ws, 3, analyst._SEMANTIC_CLASS_SPANS_HEADER)
            == "The state narrows the Ed-Fi definition to the SBOE-approved subset."
        )

    def test_spine_lens_complexity_and_fact_values(
        self, tmp_path, monkeypatch
    ):
        records = self._two_records_one_extension()
        self._write_inputs(tmp_path, "spine", records)
        out = tmp_path / "data" / "out"
        self._write_provenance_sidecar(out, "AZ", "spine", records)

        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        from src.report.audit import run as run_audit

        analyst.run(state="AZ", out_dir=out, lens="spine")
        wb = openpyxl.load_workbook(
            run_audit(state="AZ", lens="spine", out=out)
        )
        ws = wb["Audit Trail"]
        # Spine-lens block order/width is pinned by the spec snapshots
        # (TestAuditTrailFactory) + fingerprint golden; values by name.
        # complexity_score cell = 0 from fixture.
        assert _cell(ws, 2, "complexity_score") == 0
        # downgraded_facts surfaces the spine downgrade.
        assert (
            "definition_is_implementable:hallucinated_span"
            in _cell(ws, 2, "downgraded_facts")
        )
        # integration_class productization-signal label + verbatim
        # LLM-evidence span both surface on the spine lens.
        assert _cell(ws, 2, "integration_class") == "concatenation"
        assert (
            _cell(ws, 2, analyst._INTEGRATION_CLASS_SPANS_HEADER)
            == "CONCATENATE LEAID-SchoolId-CalendarTypeCode-Sequence"
        )
        # semantic_class observability surface on spine — label +
        # spans surface alongside integration_class even though
        # spine's cascade doesn't consult this fact.
        assert _cell(ws, 2, "semantic_class") == "aligned"
        assert (
            _cell(ws, 2, analyst._SEMANTIC_CLASS_SPANS_HEADER)
            == "Matches the Ed-Fi calendar code contract."
        )

    def test_unscored_rows_appear_with_blank_scoring_cells(
        self, tmp_path, monkeypatch
    ):
        """Every Details row appears in Elements; rows missing from the
        sidecar render with blank scoring cells (the 'merge Details +
        Scores' contract requires no row dropping)."""
        records = self._two_records_one_extension()
        self._write_inputs(tmp_path, "source", records)
        out = tmp_path / "data" / "out"
        # Sidecar covers ONLY the first record — second record is unscored.
        self._write_provenance_sidecar(out, "AZ", "source", records[:1])

        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        from src.report.audit import run as run_audit

        analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(
            run_audit(state="AZ", lens="source", out=out)
        )
        ws = wb["Audit Trail"]
        # Both records appear (header + 2 data rows).
        assert ws.max_row == 3
        # Canonical sort puts az_extra before calendarCode, so row 3 is
        # the scored calendarCode record and row 2 the unscored az_extra.
        # Row 3 (calendarCode) scored: confidence_composite = "medium" per fixture
        assert _cell(ws, 3, "confidence_composite") == "medium"
        # Row 2 (az_extra) unscored: every fact + dim + tail cell is blank.
        for h in (
            *analyst._SOURCE_FACT_ORDER,
            *analyst._SOURCE_DIM_ORDER,
            "confidence_composite", "rule_paths",
            "downgraded_facts", "review_route",
        ):
            assert _cell(ws, 2, h) is None, (
                f"unscored row 2, col {h}: expected blank, got "
                f"{_cell(ws, 2, h)!r}"
            )

    def test_combined_workbook_carries_elements_too(self, tmp_path, monkeypatch):
        """The coverage_analyst workbook gets an Elements sheet pooling
        every state's rows when at least one sidecar is loaded."""
        spine_dir = tmp_path / "data" / "spine"
        out = tmp_path / "data" / "out"
        out.mkdir(parents=True, exist_ok=True)
        spine_dir.mkdir(parents=True, exist_ok=True)
        for state in ("AZ", "WI", "MN", "TX", "IN"):
            records = [
                ElementRecord(
                    state=state, edfi_version="4.0", domain="SchoolCalendar",
                    entity="Calendar", element_name="calendarCode",
                    definition_text="x", source="core", documented=True,
                ),
            ]
            elements = StateElements(
                state=state, edfi_version="4.0",
                extracted_at=datetime.now(timezone.utc),
                element_count=1, elements=records,
            )
            (out / f"{state.lower()}_elements_source.json").write_text(
                elements.model_dump_json(indent=2), encoding="utf-8"
            )
            spine = _az_spine().model_copy(update={"state": state})
            (spine_dir / f"{state.lower()}_spine.json").write_text(
                spine.model_dump_json(indent=2), encoding="utf-8"
            )
        # Sidecar only for AZ.
        self._write_provenance_sidecar(
            out, "AZ", "source",
            [ElementRecord(
                state="AZ", edfi_version="4.0", domain="SchoolCalendar",
                entity="Calendar", element_name="calendarCode",
                definition_text="x", source="core", documented=True,
            )],
        )

        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)
        produced = analyst.run(out_dir=out, lens="source")
        combined = next(p for p in produced if p.name == "coverage_analyst.xlsx")
        wb = openpyxl.load_workbook(combined)
        # Option D: no Audit Trail on the combined deliverable either —
        # the audit surface is the per-state on-demand workbook.
        assert "Audit Trail" not in wb.sheetnames
        from src.report.audit import run as run_audit

        audit_wb = openpyxl.load_workbook(
            run_audit(state="AZ", lens="source", out=out)
        )
        ws = audit_wb["Audit Trail"]
        # AZ audit: 1 record + header row.
        assert ws.max_row == 2
        assert _cell(ws, 2, "Data Element") == "calendarCode"
        # First state (AZ) is scored; the others have blank scoring cells.
        # Probe confidence_composite — AZ scored, others blank.
        conf_values = [_cell(ws, r, "confidence_composite") for r in range(2, 7)]
        # AZ is row 2 (state input ordering); confidence "medium" per fixture.
        assert conf_values[0] == "medium"
        # Other states unscored → None.
        assert conf_values[1:] == [None, None, None, None]


class TestPeerGapsSheet:
    """Peer Gaps sheet — per-state workbook surfaces rows from
    ``data/out/scoring/phase_a/peer_gap.jsonl``. One row per
    ``suggested_fills`` entry where the fill's ``state`` matches this
    workbook's state. Missing artifact → sheet absent; no exception.
    """

    def _write_peer_gap_artifact(self, out_dir: Path, slots: list[dict]) -> Path:
        """Write a peer_gap.jsonl at the canonical relative path under out_dir.

        Prepends a header line matching the real runner's artifact shape.
        """
        target = out_dir / "scoring" / "phase_a" / "peer_gap.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "__type": "header",
            "kind": "peer_gap",
            "prompt_version": "peer-gap.v1",
            "bundle_count": len(slots),
            "scored_count": len(slots),
            "status": "complete",
        }
        with target.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(header) + "\n")
            for s in slots:
                fh.write(json.dumps(s) + "\n")
        return target

    def _slot(
        self,
        *,
        slot_key: str,
        confidence: str,
        states_present: list[str],
        consensus_concept: str,
        fills: list[dict],
    ) -> dict:
        return {
            "slot_key": slot_key,
            "confidence": confidence,
            "confidence_rationale": "fixture",
            "consensus_concept": consensus_concept,
            "states_present": states_present,
            "per_state_divergences": [
                {"state": s, "posture": "conceptual", "summary": "fixture"}
                for s in states_present
            ],
            "suggested_fills": fills,
        }

    def test_sheet_absent_when_artifact_missing(self, tmp_path, monkeypatch):
        _write_az_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(
            state="AZ", out_dir=tmp_path / "data" / "out", lens="source",
        )
        wb = openpyxl.load_workbook(path)
        assert "Peer Gaps" not in wb.sheetnames

    def test_sheet_filters_to_state(self, tmp_path, monkeypatch):
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        # Two slots, one with an AZ fill, one with a TX fill. AZ workbook
        # should carry one row (the AZ fill) only.
        slots = [
            self._slot(
                slot_key="Calendar|calendarCode",
                confidence="medium",
                states_present=["AZ", "WI", "TX"],
                consensus_concept="concept A",
                fills=[{
                    "state": "AZ",
                    "current_posture": "conceptual",
                    "peer_consensus_format": "WI: wi format",
                    "recommended_fill": "Consider adding AZ format",
                }],
            ),
            self._slot(
                slot_key="Course|courseCode",
                confidence="high",
                states_present=["WI", "TX"],
                consensus_concept="concept B",
                fills=[{
                    "state": "TX",
                    "current_posture": "conceptual",
                    "peer_consensus_format": "WI: course spec",
                    "recommended_fill": "TX should prescribe a format",
                }],
            ),
        ]
        self._write_peer_gap_artifact(out, slots)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert "Peer Gaps" in wb.sheetnames
        ws = wb["Peer Gaps"]
        # Header row + one AZ-fill row. (Header list pinned by the spec
        # snapshots; values asserted by name.)
        assert ws.max_row == 2
        assert _cell(ws, 2, "Slot") == "Calendar|calendarCode"
        assert _cell(ws, 2, "Confidence") == "medium"
        assert _cell(ws, 2, "States Present") == "AZ+WI+TX"
        assert _cell(ws, 2, "Current Posture") == "conceptual"
        assert _cell(ws, 2, "Recommended Fill") == "Consider adding AZ format"

    def test_row_count_matches_state_fills(self, tmp_path, monkeypatch):
        """Five slots with 10 total fills across AZ/WI/MN/TX — per-state
        row counts match the ground-truth distribution."""
        # Build a distribution: AZ=3, WI=2, MN=4, TX=1 → 10 total across 5 slots.
        target_counts = {"AZ": 3, "WI": 2, "MN": 4, "TX": 1}
        slot_templates = [
            ("Calendar|calendarCode", ["AZ", "WI", "MN", "TX", "IN"]),
            ("Course|courseCode", ["AZ", "WI", "MN"]),
            ("Student|studentUniqueId", ["AZ", "MN", "TX"]),
            ("Section|sectionIdentifier", ["MN", "WI"]),
            ("Staff|staffUniqueId", ["AZ", "MN"]),
        ]
        # Explicitly assign per-slot fills to hit target totals.
        slot_fill_plan = {
            "Calendar|calendarCode": ["AZ", "WI", "MN"],
            "Course|courseCode": ["AZ", "MN"],
            "Student|studentUniqueId": ["AZ", "TX"],
            "Section|sectionIdentifier": ["WI", "MN"],
            "Staff|staffUniqueId": ["MN"],
        }
        # Sanity: ensure the plan's totals match target_counts.
        from collections import Counter as _Counter
        totals = _Counter(s for fills in slot_fill_plan.values() for s in fills)
        assert dict(totals) == target_counts, (
            f"test-internal: fill plan produced {dict(totals)}, "
            f"expected {target_counts}"
        )

        slots = []
        for slot_key, states_present in slot_templates:
            fill_states = slot_fill_plan[slot_key]
            slots.append(self._slot(
                slot_key=slot_key,
                confidence="medium",
                states_present=states_present,
                consensus_concept=f"concept for {slot_key}",
                fills=[
                    {
                        "state": s,
                        "current_posture": "conceptual",
                        "peer_consensus_format": "fixture peer format",
                        "recommended_fill": f"fill for {s} on {slot_key}",
                    }
                    for s in fill_states
                ],
            ))

        # Set up per-state fixtures: each state needs its own elements/
        # spine file for analyst.run to load. Reuse AZ fixture helper,
        # then duplicate for the other three states.
        out = tmp_path / "data" / "out"
        spine_dir = tmp_path / "data" / "spine"
        out.mkdir(parents=True, exist_ok=True)
        spine_dir.mkdir(parents=True, exist_ok=True)
        for state in ("AZ", "WI", "MN", "TX", "IN"):
            records = [
                ElementRecord(
                    state=state, edfi_version="4.0", domain="SchoolCalendar",
                    entity="Calendar", element_name="calendarCode",
                    definition_text="x", source="core", documented=True,
                ),
            ]
            elements = StateElements(
                state=state, edfi_version="4.0",
                extracted_at=datetime.now(timezone.utc),
                element_count=1, elements=records,
            )
            (out / f"{state.lower()}_elements_source.json").write_text(
                elements.model_dump_json(indent=2), encoding="utf-8"
            )
            spine = _az_spine().model_copy(update={"state": state})
            (spine_dir / f"{state.lower()}_spine.json").write_text(
                spine.model_dump_json(indent=2), encoding="utf-8"
            )
        self._write_peer_gap_artifact(out, slots)

        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)
        produced = analyst.run(out_dir=out, lens="source")
        per_state = {
            p.name: p for p in produced if p.name.endswith("_analyst.xlsx")
        }
        for state, expected in target_counts.items():
            wb = openpyxl.load_workbook(per_state[f"{state.lower()}_analyst.xlsx"])
            assert "Peer Gaps" in wb.sheetnames, (
                f"{state} workbook missing Peer Gaps sheet"
            )
            ws = wb["Peer Gaps"]
            # max_row = 1 header + expected data rows.
            assert ws.max_row == expected + 1, (
                f"{state}: expected {expected} rows, got {ws.max_row - 1}"
            )

    def test_high_confidence_slot_carries_peer_format(self, tmp_path, monkeypatch):
        """A HIGH-confidence slot → the Peer Consensus Format column
        carries the joined peer-format string (not empty)."""
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        slots = [
            self._slot(
                slot_key="Calendar|calendarTypeDescriptor",
                confidence="high",
                states_present=["AZ", "WI", "MN", "TX", "IN"],
                consensus_concept="consensus concept prose",
                fills=[{
                    "state": "AZ",
                    "current_posture": "prescriptive",
                    "peer_consensus_format": (
                        "WI: 'uri://ed-fi.org/CalendarTypeDescriptor | ...'; "
                        "TX: 'CalendarType indicates the type of attendance...'"
                    ),
                    "recommended_fill": (
                        "Consider adding an enumeration of the allowed descriptor values."
                    ),
                }],
            ),
        ]
        self._write_peer_gap_artifact(out, slots)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        ws = wb["Peer Gaps"]
        assert _cell(ws, 2, "Confidence") == "high"
        pcf = _cell(ws, 2, "Peer Consensus Format")
        assert pcf is not None and pcf.startswith("WI:")
        assert "TX:" in pcf
        # Consensus Concept column carries the slot-level concept so the
        # row is self-contained.
        assert _cell(ws, 2, "Consensus Concept") == "consensus concept prose"


class TestScoreCardHeatmap:
    """Score Card 4×4 Implementation Shape × Documentation Style heatmap.

    A 4×4 matrix keyed by ``(structural_depth, documentation_style_tier)``
    is appended when the per-state sidecar carries Integration Profile
    dimensions. Top-left 2×2 (``s>=2 AND d<=1``) is the Documentation
    Gap quadrant. Option B scopes the heatmap to the API-MODEL (spine)
    lens only — the doc-gap surface lives on the retiring API-model
    deliverable until Option D's Documentation Gaps sheet exists.
    """

    def _write_integration_profile_matrix_sidecar(
        self,
        out_dir: Path,
        state: str,
        lens: str,
        records_with_dims: list[tuple[ElementRecord, int | None, int | None]],
    ) -> None:
        """Seed a sidecar carrying structural + doc dimension scalars
        for every record. ``None`` on either dim → audit bucket."""
        scores = []
        for rec, s_val, d_val in records_with_dims:
            key = f"{state}|{rec.entity}|{rec.element_name}"
            dims = {
                "structural_depth": {
                    "value": s_val,
                    "rule_matched": "fixture",
                    "inputs_used": {},
                    "confidence": "high",
                },
                "documentation_style_tier": {
                    "value": d_val,
                    "rule_matched": "fixture",
                    "inputs_used": {"documentation_style": "fixture"},
                    "confidence": "high",
                },
            }
            # Satisfy source-lens rollup expectations (canonical_name_alignment etc.).
            if lens == "source":
                dims.update({
                    "canonical_name_alignment": {"value": 2, "confidence": "high"},
                    "definition_quality": {"value": 2, "confidence": "high"},
                    "semantic_fidelity": {"value": 2, "confidence": "high"},
                    "extension_justification": {"value": None, "confidence": "high"},
                })
            scores.append({
                "record_key": key,
                "entity": rec.entity,
                "element_name": rec.element_name,
                "dimensions": dims,
                "complexity_score": None,
                "confidence_composite": "high",
                "fact_provenance": {},
                "review": {"needs_review": False, "reasons": [], "route": None},
                "adjusted_nachos_score": None,
                "in_scope": False,
                "nachos_justification": None,
            })
        payload = {
            "state": state, "lens": lens,
            "record_count": len(records_with_dims),
            "scored_count": len(records_with_dims),
            "needs_review_count": 0,
            "scores": scores,
        }
        (out_dir / f"{state.lower()}_scores_{lens}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _build_records(self, n: int) -> list[ElementRecord]:
        """Build ``n`` synthetic AZ Calendar records with unique element names."""
        return [
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="SchoolCalendar",
                entity="Calendar", element_name=f"field{i}",
                definition_text=f"fixture {i}",
                source="core", documented=True,
            )
            for i in range(n)
        ]

    def _locate_heatmap(self, ws):
        """Return ``(header_row, first_data_row)`` for the heatmap block.

        The heatmap header row carries the prose starting with
        "Implementation Shape ×" (#174 retitle). The axis-label row
        (Silent | Cross-ref / Reg | Conceptual | Prescriptive) is 2 rows
        below (italic caption sits between). First data row is one below
        the axis-label row.
        """
        for r in range(1, ws.max_row + 1):
            v = ws.cell(row=r, column=1).value
            if isinstance(v, str) and v.startswith(
                "Implementation Shape × Documentation Style"
            ):
                return r, r + 3
        raise AssertionError("heatmap header row not found")

    def test_compute_matrix_buckets_correctly(self):
        """Pure helper test — dim-presence → 4×4 bucket, audit count
        captures missing dims."""
        scores = {
            "k1": {"dimensions": {
                "structural_depth": {"value": 3},
                "documentation_style_tier": {"value": 0},
            }},
            "k2": {"dimensions": {
                "structural_depth": {"value": 3},
                "documentation_style_tier": {"value": 0},
            }},
            "k3": {"dimensions": {
                "structural_depth": {"value": 1},
                "documentation_style_tier": {"value": 3},
            }},
            # Missing doc → audit.
            "k4": {"dimensions": {
                "structural_depth": {"value": 2},
                "documentation_style_tier": {"value": None},
            }},
            # Missing struct → audit.
            "k5": {"dimensions": {
                "documentation_style_tier": {"value": 2},
            }},
        }
        matrix, audit = analyst._compute_structural_doc_matrix(scores)
        assert matrix[(3, 0)] == 2
        assert matrix[(1, 3)] == 1
        # Every other cell zero (matrix is zero-filled).
        other_zero = sum(
            v for k, v in matrix.items() if k not in {(3, 0), (1, 3)}
        )
        assert other_zero == 0
        assert audit == 2

    def test_heatmap_cells_sum_to_record_count(self, tmp_path, monkeypatch):
        """Fixture with a known tier distribution — the 16 cells sum to
        the scored record count (minus audit-bucket records). Spine lens:
        the heatmap is API-model-lens-only under Option B."""
        out = tmp_path / "data" / "out"
        spine_dir = tmp_path / "data" / "spine"
        out.mkdir(parents=True, exist_ok=True)
        spine_dir.mkdir(parents=True, exist_ok=True)
        # 6 records with explicit (s, d) tiers; 1 record missing a dim.
        records = self._build_records(7)
        tier_assignments: list[tuple[int | None, int | None]] = [
            (3, 0), (3, 0), (2, 1), (2, 2), (1, 3), (0, 3),
            (None, 2),
        ]
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(records), elements=records,
        )
        (out / "az_elements_spine.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (spine_dir / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(json.dumps(_az_gap_log()), encoding="utf-8")
        self._write_integration_profile_matrix_sidecar(
            out, "AZ", "spine",
            [(rec, s, d) for rec, (s, d) in zip(records, tier_assignments)],
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)

        [path] = analyst.run(state="AZ", out_dir=out, lens="spine")
        wb = openpyxl.load_workbook(path)
        sc = wb["Score Card"]
        _, first_data_row = self._locate_heatmap(sc)
        # Rows in order Deep, Moderate, Light, Flat; cols 2..5 are
        # Silent, Cross-ref / Reg, Conceptual, Prescriptive.
        total = 0
        for row_offset in range(4):
            for col_offset in range(4):
                v = sc.cell(
                    row=first_data_row + row_offset,
                    column=2 + col_offset,
                ).value
                assert isinstance(v, int), (
                    f"cell at row {first_data_row + row_offset}, "
                    f"col {2 + col_offset} is {v!r}"
                )
                total += v
        # 6 records bucketed; 1 omitted to audit.
        assert total == 6
        # Audit footnote renders below the matrix.
        col_a = [sc.cell(row=r, column=1).value for r in range(1, sc.max_row + 1)]
        audit_lines = [
            v for v in col_a
            if isinstance(v, str) and v.startswith("Audit:")
        ]
        assert len(audit_lines) == 1
        assert "1 record" in audit_lines[0]

    def test_documentation_gap_quadrant_highlights_expected_cells(self, tmp_path, monkeypatch):
        """Fixture with every record landing in the Documentation Gap
        quadrant (structural >=2 AND doc <=1). All 5 records sit in the
        top-left 2×2; other 12 cells are zero. Spine lens (Option B)."""
        out = tmp_path / "data" / "out"
        spine_dir = tmp_path / "data" / "spine"
        out.mkdir(parents=True, exist_ok=True)
        spine_dir.mkdir(parents=True, exist_ok=True)
        records = self._build_records(5)
        tier_assignments = [(3, 0), (3, 1), (2, 0), (2, 1), (2, 0)]
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(records), elements=records,
        )
        (out / "az_elements_spine.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (spine_dir / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(json.dumps(_az_gap_log()), encoding="utf-8")
        self._write_integration_profile_matrix_sidecar(
            out, "AZ", "spine",
            [(rec, s, d) for rec, (s, d) in zip(records, tier_assignments)],
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)

        [path] = analyst.run(state="AZ", out_dir=out, lens="spine")
        wb = openpyxl.load_workbook(path)
        sc = wb["Score Card"]
        _, first_data_row = self._locate_heatmap(sc)

        # Top-left 2×2 = rows Deep, Moderate × cols Silent, Cross-ref / Reg
        # (row offsets 0,1 · col offsets 0,1).
        undoc_total = 0
        for row_offset in (0, 1):
            for col_offset in (0, 1):
                undoc_total += sc.cell(
                    row=first_data_row + row_offset,
                    column=2 + col_offset,
                ).value
        # All 5 records sit in the quadrant.
        assert undoc_total == 5

        # Every other cell must be zero.
        outside = 0
        for row_offset in range(4):
            for col_offset in range(4):
                if row_offset <= 1 and col_offset <= 1:
                    continue
                outside += sc.cell(
                    row=first_data_row + row_offset,
                    column=2 + col_offset,
                ).value
        assert outside == 0

    def test_heatmap_renders_when_dimensions_missing(self, tmp_path, monkeypatch):
        """All records missing the structural dim → the matrix is all
        zeros, and the audit footnote surfaces the full count. Spine
        lens (Option B)."""
        out = tmp_path / "data" / "out"
        spine_dir = tmp_path / "data" / "spine"
        out.mkdir(parents=True, exist_ok=True)
        spine_dir.mkdir(parents=True, exist_ok=True)
        records = self._build_records(3)
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(records), elements=records,
        )
        (out / "az_elements_spine.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (spine_dir / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(json.dumps(_az_gap_log()), encoding="utf-8")
        self._write_integration_profile_matrix_sidecar(
            out, "AZ", "spine",
            [(rec, None, 2) for rec in records],
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)

        [path] = analyst.run(state="AZ", out_dir=out, lens="spine")
        wb = openpyxl.load_workbook(path)
        sc = wb["Score Card"]
        # Heatmap still rendered (audit_count > 0 is enough).
        _, first_data_row = self._locate_heatmap(sc)
        # Every cell is zero — every record went to the audit bucket.
        for row_offset in range(4):
            for col_offset in range(4):
                assert sc.cell(
                    row=first_data_row + row_offset,
                    column=2 + col_offset,
                ).value == 0
        col_a = [sc.cell(row=r, column=1).value for r in range(1, sc.max_row + 1)]
        audit_lines = [
            v for v in col_a
            if isinstance(v, str) and v.startswith("Audit:")
        ]
        assert audit_lines and "3 record" in audit_lines[0]

    def test_heatmap_absent_on_source_lens(self, tmp_path, monkeypatch):
        """Option B: the heatmap is API-model-lens-only — a SOURCE
        workbook whose sidecar carries Integration Profile dims must NOT
        render it."""
        out = tmp_path / "data" / "out"
        spine_dir = tmp_path / "data" / "spine"
        out.mkdir(parents=True, exist_ok=True)
        spine_dir.mkdir(parents=True, exist_ok=True)
        records = self._build_records(3)
        tier_assignments = [(3, 0), (2, 1), (1, 3)]
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(records), elements=records,
        )
        (out / "az_elements_source.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (spine_dir / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(json.dumps(_az_gap_log()), encoding="utf-8")
        self._write_integration_profile_matrix_sidecar(
            out, "AZ", "source",
            [(rec, s, d) for rec, (s, d) in zip(records, tier_assignments)],
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        sc = openpyxl.load_workbook(path)["Score Card"]
        for r in range(1, sc.max_row + 1):
            v = sc.cell(row=r, column=1).value
            assert not (
                isinstance(v, str)
                and v.startswith("Implementation Shape × Documentation Style")
            )


# ---------------------------------------------------------------------------
# Track C scorecard integration — Phase 2 (Recommendations sheet + Scores
# summary column) + Phase 3 (impact-DESC sort within record). Uses live
# recommendation generation against synthetic scoring sidecars + recs
# sidecar; the analyst reads both and writes the Recommendations sheet
# alongside Scores.
# ---------------------------------------------------------------------------


import pytest


def _write_scoring_with_rules(
    out: Path, state: str, lens: str, records: list[ElementRecord]
) -> None:
    """Phase 2 fixture — write a sidecar whose below-target dimensions
    carry the ``rule_matched`` strings the recommendations module needs
    to look up a template. The base PhaseD helper omits rule_matched
    (it predates the templates), so a Track C test seeded by it would
    silently produce zero recommendations.
    """
    scores = []
    for r in records:
        key = f"{state}|{r.entity}|{r.element_name}"
        if lens == "spine":
            dims = {
                "documentation_completeness": {
                    "value": 1, "rule_matched": "tier_1_minimal",
                    "confidence": "high",
                },
                "obligation_clarity": {
                    "value": 0, "rule_matched": "tier_0_none",
                    "confidence": "high",
                },
                "business_logic_complexity": {
                    "value": 1, "rule_matched": "tier_1_cond_or_cross",
                    "confidence": "high",
                },
                "nachos_score": {
                    "value": 3, "rule_matched": "tier_3_aggregation",
                    "confidence": "high",
                },
            }
            complexity = 1
        else:
            dims = {
                "canonical_name_alignment": {
                    "value": 1, "rule_matched": "tier_1_resolved",
                    "confidence": "high",
                },
                "definition_quality": {
                    "value": 2, "rule_matched": "tier_2_substantive",
                    "confidence": "medium",
                },
                "semantic_fidelity": {
                    "value": 3, "rule_matched": "tier_3_aligned",
                    "confidence": "high",
                },
                "extension_justification": {
                    "value": 0, "rule_matched": "tier_0_unnecessary_mirror",
                    "confidence": "high",
                },
                "nachos_score": {
                    "value": 3, "rule_matched": "tier_3_aggregation",
                    "confidence": "high",
                },
            }
            complexity = None
        # Provenance for the evidence facts each fired template cites.
        provenance = {
            "element_name_matches_canonical": {
                "value": False, "confidence": "high",
                "downgraded": False, "downgrade_reason": None,
            },
            "definition_adds_detail_beyond_edfi": {
                "value": False, "confidence": "high",
                "downgraded": False, "downgrade_reason": None,
            },
            "extension_mirrors_core_pattern": {
                "value": True, "confidence": "high",
                "downgraded": False, "downgrade_reason": None,
            },
            "has_aggregation": {
                "value": True, "confidence": "high",
                "downgraded": False, "downgrade_reason": None,
            },
            "has_conditional_logic": {
                "value": True, "confidence": "high",
                "downgraded": False, "downgrade_reason": None,
            },
            "business_rules_present": {
                "value": False, "confidence": "high",
                "downgraded": False, "downgrade_reason": None,
            },
            "required_when_stated": {
                "value": False, "confidence": "high",
                "downgraded": False, "downgrade_reason": None,
            },
        }
        scores.append({
            "record_key": key,
            "entity": r.entity,
            "element_name": r.element_name,
            "dimensions": dims,
            "fact_provenance": provenance,
            "complexity_score": complexity,
            "confidence_composite": "high",
            "review": {
                "needs_review": False, "reasons": [], "route": None,
            },
            "adjusted_nachos_score": 3.0,
            "in_scope": True,
            "nachos_justification": "tier_3_aggregation",
        })
    payload = {
        "state": state, "lens": lens,
        "record_count": len(records), "scored_count": len(records),
        "needs_review_count": 0, "scores": scores,
    }
    (out / f"{state.lower()}_scores_{lens}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _seed_full_az_fixture(tmp_path: Path, lens: str) -> Path:
    """Stand up the analyst's input set: elements + spine + scores
    sidecar + recommendations sidecar (via the live recommendations
    generator) + a one-entry review_queue stub.

    Returns the ``data/out`` directory the analyst expects.
    """
    from src.report.recommendations import run as run_recommendations

    out = tmp_path / "data" / "out"
    spine_dir = tmp_path / "data" / "spine"
    out.mkdir(parents=True, exist_ok=True)
    spine_dir.mkdir(parents=True, exist_ok=True)

    elements = _az_elements()
    spine = _az_spine()
    if lens == "spine":
        (out / "az_elements_spine.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
    else:
        (out / "az_elements_source.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
    (spine_dir / "az_spine.json").write_text(
        spine.model_dump_json(indent=2), encoding="utf-8"
    )
    (out / "az_gap_log.json").write_text(json.dumps(_az_gap_log()), encoding="utf-8")

    # Scores sidecar with rule_matched + fact_provenance — required for
    # the recommendations module to render a non-empty payload.
    _write_scoring_with_rules(out, "AZ", lens, list(elements.elements))

    # Generate the recommendations sidecar from the scores sidecar.
    run_recommendations(state="AZ", lens=lens, out=out)

    # Stub a review-queue artifact so the route join surfaces a real
    # value for at least one record (verifies the join path; the rest
    # render "—").
    first_record = elements.elements[0]
    record_key = f"AZ|{first_record.entity}|{first_record.element_name}"
    (out / f"review_queue_{lens}.json").write_text(
        json.dumps({
            "lens": lens,
            "entries": [{
                "state": "AZ",
                "record_key": record_key,
                "entity": first_record.entity,
                "element_name": first_record.element_name,
                "route": "POLICY",
            }],
        }),
        encoding="utf-8",
    )
    return out


class TestRecommendationsSheet:
    """Phase 2 (Track C scorecard integration) — Recommendations sheet
    is wired into both per-state and combined workbooks across both
    lenses; the Scores sheet carries a recommendations summary column;
    the source-lens Reviewer View stays at the 28-col contract.
    Phase 3 — within each (state, entity, element) group, rows are
    ordered by abs(impact) DESC."""

    @pytest.mark.parametrize("lens", ["source", "spine"])
    def test_recommendations_sheet_shape_and_round_trip(
        self, tmp_path, monkeypatch, lens
    ):
        out = _seed_full_az_fixture(tmp_path, lens)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens=lens)
        wb = openpyxl.load_workbook(path)
        assert "Recommendations" in wb.sheetnames

        ws = wb["Recommendations"]
        # Full header list is pinned by the spec snapshots + fingerprint
        # golden; presence-check the load-bearing headers only.
        headers = [
            ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)
        ]
        for h in ("State", "Dimension", "Impact", "Recommendation", "Evidence Fact"):
            assert h in headers

        # Round-trip: every row in the recommendations sidecar appears
        # at least once in the sheet (one row per below-target dim).
        rec_path = out / f"az_recommendations_{lens}.json"
        rec_payload = json.loads(rec_path.read_text())
        expected_rows = sum(
            len(r["recommendations"]) for r in rec_payload["records"]
        )
        # Guard against silent fixture drift — the test only proves
        # anything when at least one recommendation actually fires.
        assert expected_rows > 0, "fixture produced no recommendations"
        assert ws.max_row - 1 == expected_rows, (
            f"sheet has {ws.max_row - 1} rows; expected {expected_rows}"
        )

        # Spot-check: every row carries a non-empty Recommendation +
        # Evidence Fact.
        for row_idx in range(2, ws.max_row + 1):
            assert _cell(ws, row_idx, "Recommendation"), (
                f"row {row_idx} has empty Recommendation"
            )
            assert _cell(ws, row_idx, "Evidence Fact"), (
                f"row {row_idx} has empty Evidence Fact"
            )

    def test_review_queue_route_joined_per_row(self, tmp_path, monkeypatch):
        """The Recommendations sheet's Review Route column joins from
        ``review_queue_{lens}.json`` by record_key; rows without a queue
        entry render the em-dash placeholder."""
        out = _seed_full_az_fixture(tmp_path, "source")
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        ws = wb["Recommendations"]
        routes = [
            _cell(ws, r, "Review Route") for r in range(2, ws.max_row + 1)
        ]
        # At least one POLICY route (the single seeded entry) and at
        # least one em-dash (any record outside the seeded entry).
        assert "POLICY" in routes
        assert any(r == "—" for r in routes)

    def test_scores_sheet_recommendations_summary_present(
        self, tmp_path, monkeypatch
    ):
        """Phase 2 added a `recommendations` column at the end of Scoring
        Summary; the cell carries a comma-joined dimension list with
        optional impact deltas (e.g. ``2 recs: definition_quality (+1),
        …``). Option B retired Scoring Summary from per-state SOURCE
        workbooks, so the sheet-level check runs on the SPINE lens."""
        out = _seed_full_az_fixture(tmp_path, "spine")
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="spine")
        wb = openpyxl.load_workbook(path)
        scores = wb["Scoring Summary"]
        # The "recommendations" column position is pinned by the spec
        # snapshot; just confirm the cell content is meaningful for a
        # row with recs.
        summary_cells = [
            _cell(scores, r, "recommendations")
            for r in range(2, scores.max_row + 1)
        ]
        non_empty = [s for s in summary_cells if s]
        assert non_empty, "expected at least one summary cell to be non-empty"
        # Each summary starts with "<n> recs:" then a comma list of dims.
        for s in non_empty:
            assert s.startswith(("1 recs:", "2 recs:", "3 recs:", "4 recs:"))

    def test_details_ai_recommendations_pointer_on_source_lens(
        self, tmp_path, monkeypatch
    ):
        """The per-row recommendations pointer that Scoring Summary used
        to carry on source workbooks now lives on Details as the far-
        right `AI: Recommendations` column."""
        out = _seed_full_az_fixture(tmp_path, "source")
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert "Scoring Summary" not in wb.sheetnames
        details = wb["Details"]
        pointer_cells = [
            _cell(details, r, "AI: Recommendations")
            for r in range(2, details.max_row + 1)
        ]
        non_empty = [s for s in pointer_cells if s]
        assert non_empty, "expected at least one AI: Recommendations cell"
        for s in non_empty:
            assert s.startswith(("1 recs:", "2 recs:", "3 recs:", "4 recs:"))

    def test_combined_workbook_includes_all_states_in_recommendations(
        self, tmp_path, monkeypatch
    ):
        """Combined coverage workbook merges all four states' recs into
        the same sheet; reviewers see a cross-state recommendation list."""
        out = _seed_full_az_fixture(tmp_path, "source")
        spine_dir = tmp_path / "data" / "spine"
        # Stamp out per-state element + spine + sidecar + rec files for
        # the other three states using AZ's data so the combined
        # workbook can build cleanly.
        from src.report.recommendations import run as run_recommendations
        elements = _az_elements()
        spine = _az_spine()
        for st in ("WI", "MN", "TX", "IN"):
            (out / f"{st.lower()}_elements_source.json").write_text(
                elements.model_copy(update={"state": st}).model_dump_json(indent=2),
                encoding="utf-8",
            )
            (spine_dir / f"{st.lower()}_spine.json").write_text(
                spine.model_copy(update={"state": st}).model_dump_json(indent=2),
                encoding="utf-8",
            )
            (out / f"{st.lower()}_gap_log.json").write_text(
                json.dumps(_az_gap_log()), encoding="utf-8"
            )
            _write_scoring_with_rules(
                out, st, "source",
                [r.model_copy(update={"state": st}) for r in elements.elements],
            )
            run_recommendations(state=st, lens="source", out=out)

        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)
        produced = analyst.run(out_dir=out, lens="source")
        combined = next(p for p in produced if p.name == "coverage_analyst.xlsx")
        wb = openpyxl.load_workbook(combined)
        assert "Recommendations" in wb.sheetnames
        ws = wb["Recommendations"]
        states_seen = {
            _cell(ws, r, "State") for r in range(2, ws.max_row + 1)
        }
        assert states_seen == {"AZ", "WI", "MN", "TX", "IN"}

    def test_sheet_rows_sorted_by_abs_impact_within_record(
        self, tmp_path, monkeypatch
    ):
        """Phase 3 sort — within each (state, entity, element) group,
        rows are ordered by abs(impact) DESC so the highest-leverage
        recommendation surfaces first. Dimension name is the final
        tiebreaker for stability."""
        out = _seed_full_az_fixture(tmp_path, "source")
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")

        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        ws = wb["Recommendations"]
        # Group rows by (state, entity, element) and confirm the Impact
        # column is non-increasing in abs value across each group.
        # Impact on cost axes is signed negative, so compare on abs()
        # to verify the sort key.
        prev_key = None
        prev_abs_impact: int | None = None
        for r in range(2, ws.max_row + 1):
            key = (
                _cell(ws, r, "State"),
                _cell(ws, r, "Entity"),
                _cell(ws, r, "Element"),
            )
            impact = _cell(ws, r, "Impact")
            abs_impact = abs(impact) if isinstance(impact, int) else 0
            if key == prev_key and prev_abs_impact is not None:
                assert abs_impact <= prev_abs_impact, (
                    f"row {r} breaks abs-impact DESC ordering: "
                    f"prev={prev_abs_impact} current={abs_impact} key={key}"
                )
            prev_key = key
            prev_abs_impact = abs_impact


class TestSequence1Hygiene:
    """Sequence-1 consumability hygiene: canonical documented-first sort,
    stable leading Row # shared across sheets, Readme generated from the
    sheets actually present, and Score Card freshness stamps."""

    def _records(self) -> list[ElementRecord]:
        return [
            # Documented rows in two Source Areas + one swagger-backfill-
            # style row (documented=False, blank Source Area post-#184).
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="SchoolCalendar",
                entity="Calendar", element_name="calendarCode",
                definition_text="doc", source="core", documented=True,
            ),
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="EducationOrganization",
                entity="School", element_name="schoolId",
                definition_text="doc", source="core", documented=True,
            ),
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="",
                entity="Calendar", element_name="swaggerOnlyField",
                definition_text="", source="core", documented=False,
            ),
        ]

    def _write_fixture(self, tmp: Path, lens: str = "source") -> Path:
        out = tmp / "data" / "out"
        out.mkdir(parents=True, exist_ok=True)
        (tmp / "data" / "spine").mkdir(parents=True, exist_ok=True)
        rows = self._records()
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(rows), elements=rows,
        )
        (out / f"az_elements_{lens}.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp / "data" / "spine" / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(
            json.dumps(_az_gap_log()), encoding="utf-8"
        )
        return out

    def _write_single_score(self, out: Path, lens: str = "source") -> None:
        """Sidecar scoring ONLY calendarCode — Scoring Summary gets one
        row, so its Row # sequence carries a gap by design."""
        (out / f"az_scores_{lens}.json").write_text(json.dumps({
            "state": "AZ", "lens": lens, "record_count": 3,
            "scored_count": 1, "needs_review_count": 0,
            "scores": [{
                "record_key": "AZ|Calendar|calendarCode",
                "entity": "Calendar", "element_name": "calendarCode",
                "dimensions": {
                    "nachos_score": {
                        "value": 1, "rule_matched": "tier_1_conditional",
                        "confidence": "high",
                    },
                },
                "complexity_score": 1,
                "confidence_composite": "high",
                "review": {"needs_review": False, "reasons": [], "route": None},
                "adjusted_nachos_score": 1.0,
                "in_scope": True,
                "nachos_justification": "tier_1_conditional",
            }],
        }), encoding="utf-8")

    def test_documented_rows_sort_first_blank_source_area_last(
        self, tmp_path, monkeypatch
    ):
        out = self._write_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        details = wb["Details"]
        # Option D: source-lens Details = DOCUMENTED rows only, in the
        # canonical order; the undocumented swagger row relocates to the
        # Documentation Gaps sheet with its provenance labeled.
        elements_in_order = [
            _cell(details, r, "Data Element")
            for r in range(2, details.max_row + 1)
        ]
        assert elements_in_order == ["schoolId", "calendarCode"]
        # Row # keeps the ALL-rows canonical numbering, so relocation
        # never renumbers the documented rows.
        assert [_cell(details, r, "Row #") for r in (2, 3)] == [1, 2]
        gaps = wb["Documentation Gaps"]
        gap_elements = {
            _cell(gaps, r, "Element") for r in range(2, gaps.max_row + 1)
        }
        assert "swaggerOnlyField" in gap_elements
        prov = {
            _cell(gaps, r, "Provenance") for r in range(2, gaps.max_row + 1)
        }
        assert "Swagger backfill (entity-level)" in prov

    def test_row_number_agrees_across_sheets(self, tmp_path, monkeypatch):
        # Spine lens — Scoring Summary (the scored-only sheet whose Row #
        # gaps are the feature) survives only on spine per-state
        # workbooks under Option B.
        out = self._write_fixture(tmp_path, lens="spine")
        self._write_single_score(out, lens="spine")
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="spine")
        wb = openpyxl.load_workbook(path)

        def row_numbers_by_element(ws) -> dict[str, int]:
            return {
                _cell(ws, r, "Data Element"): _cell(ws, r, "Row #")
                for r in range(2, ws.max_row + 1)
            }

        details = row_numbers_by_element(wb["Details"])
        # Option D: the audit surface moved to its own workbook — the
        # Row # address must STILL agree across workbooks (same
        # canonical sort + row-number index).
        from src.report.audit import run as run_audit

        audit_wb = openpyxl.load_workbook(
            run_audit(state="AZ", lens="spine", out=out)
        )
        audit = row_numbers_by_element(audit_wb["Audit Trail"])
        scores = row_numbers_by_element(wb["Scoring Summary"])
        assert details == audit == {
            "schoolId": 1, "calendarCode": 2, "swaggerOnlyField": 3,
        }
        # Only calendarCode is scored — Scoring Summary shows exactly its
        # Details address; the missing 1 and 3 ARE the feature.
        assert scores == {"calendarCode": 2}

    def test_readme_matches_sheets_present_spine_lens(self, tmp_path, monkeypatch):
        """Spine workbook (with the '{ST} — Documented only' suffix-rule
        sheet) documents exactly its own tab inventory."""
        out = tmp_path / "data" / "out"
        out.mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "spine").mkdir(parents=True, exist_ok=True)
        rows = self._records()
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(rows), elements=rows,
        )
        (out / "az_elements_spine.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp_path / "data" / "spine" / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="spine")
        wb = openpyxl.load_workbook(path)
        readme = wb["Readme"]
        # Slice to the sheet table — the "Working a review pass" tips
        # block follows it (issue #259 PR 1).
        last_sheet_row = 1 + len(wb.sheetnames)
        listed = [
            _cell(readme, r, "Sheet")
            for r in range(2, last_sheet_row + 1)
        ]
        assert listed == wb.sheetnames
        assert "AZ — Documented only" in listed
        # Every listed sheet carries guide prose.
        for r in range(2, last_sheet_row + 1):
            assert _cell(readme, r, "What it shows")

    def test_update_log_at_position_2_legend_present(
        self, tmp_path, monkeypatch
    ):
        """Option B: Update Log is the new position-2 sheet; Legend
        survives but moved to the trailing lookup block."""
        out = self._write_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert wb.sheetnames[1] == "Update Log"
        assert "Legend" in wb.sheetnames
        assert wb.sheetnames.index("Legend") > wb.sheetnames.index("Details")

    def test_update_log_stamps_and_honest_version_label(
        self, tmp_path, monkeypatch
    ):
        from src.report.versions import METHODOLOGY_VERSION_HISTORY
        from src.score.aggregate import SCORING_PLAN_VERSION

        out = self._write_fixture(tmp_path)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ul = openpyxl.load_workbook(path)["Update Log"]
        by_label = {
            ul.cell(row=r, column=1).value: ul.cell(row=r, column=2).value
            for r in range(3, ul.max_row + 1)
        }
        assert by_label["Methodology version"] == f"v{SCORING_PLAN_VERSION}"
        # Parseable ISO timestamp — never pin the value (non-reproducible
        # by design; workbooks are gitignored).
        datetime.fromisoformat(by_label["Workbook generated"])
        # Honest relabel: swagger info.version, NOT "Ed-Fi Data Standard".
        assert "Ed-Fi API version (swagger info.version)" in by_label
        assert "Ed-Fi Data Standard version" not in by_label
        # #174 relabels: "API model" replaces "Spine" on the input stamps.
        assert "API model fetched at" in by_label
        assert "Ed-Fi API model coverage (of full UDM)" in by_label
        assert "Source-document coverage" in by_label
        assert "Coverage semantics" in by_label
        assert "Source scope" in by_label

        # Methodology version history table — newest entry first.
        col_a = [ul.cell(row=r, column=1).value for r in range(1, ul.max_row + 1)]
        assert "Methodology version history" in col_a
        header_row = col_a.index("Methodology version history") + 2
        assert [
            ul.cell(row=header_row, column=c).value for c in (1, 2, 3)
        ] == ["Version", "Date", "What changed"]
        newest = METHODOLOGY_VERSION_HISTORY[-1]
        assert ul.cell(row=header_row + 1, column=1).value == f"v{newest.version}"
        assert ul.cell(row=header_row + 1, column=2).value == newest.date
        assert ul.cell(row=header_row + 1, column=3).value == newest.summary
        # Oldest entry renders last.
        oldest = METHODOLOGY_VERSION_HISTORY[0]
        last_row = header_row + len(METHODOLOGY_VERSION_HISTORY)
        assert ul.cell(row=last_row, column=1).value == f"v{oldest.version}"

    def test_localhost_source_url_annotated(self, tmp_path, monkeypatch):
        out = self._write_fixture(tmp_path)
        # Rewrite the spine with a localhost fetch URL (the TX local-stack
        # shape) — the Update Log must annotate, not suppress.
        spine = _az_spine()
        spine.source_urls.resources = (
            "http://localhost:26030/metadata/data/v3/resources/swagger.json"
        )
        (tmp_path / "data" / "spine" / "az_spine.json").write_text(
            spine.model_dump_json(indent=2), encoding="utf-8"
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ul = openpyxl.load_workbook(path)["Update Log"]
        by_label = {
            ul.cell(row=r, column=1).value: ul.cell(row=r, column=2).value
            for r in range(3, ul.max_row + 1)
        }
        url_cell = by_label["Source URL (resources)"]
        assert url_cell.startswith("http://localhost:26030/")
        assert "local TSDS Vendor SDK stack" in url_cell
        # Public URLs pass through unannotated.
        assert analyst._display_source_url(
            "https://example/AZ/resources/swagger.json"
        ) == "https://example/AZ/resources/swagger.json"


class TestFinalizeWorkbook:
    """Sequence-1 presentation pass: registry-driven freeze panes,
    autofilter, tab colors, number formats, and conditional formatting."""

    def _built_workbook(self, tmp_path, monkeypatch, lens: str = "source"):
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        if lens == "spine":
            (out / "az_elements_spine.json").write_text(
                _az_elements().model_dump_json(indent=2), encoding="utf-8"
            )
        records = _az_elements().elements
        TestPhaseDScoringIntegration()._write_scores_sidecar(
            out, "AZ", lens, records,
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens=lens)
        return openpyxl.load_workbook(path)

    def test_freeze_filter_and_tab_colors_per_registry(self, tmp_path, monkeypatch):
        wb = self._built_workbook(tmp_path, monkeypatch)
        details = wb["Details"]
        # Identity block (Row # + cols A–G) stays visible while scrolling.
        assert details.freeze_panes == "H2"
        assert details.auto_filter.ref  # covers the full used range
        assert details.sheet_properties.tabColor.rgb == "FFC6E0B4"  # work
        # Scoring Summary retired from per-state SOURCE workbooks.
        assert "Scoring Summary" not in wb.sheetnames
        # Option D: Audit Trail lives in the on-demand audit workbook —
        # its registry finish applies there.
        assert "Audit Trail" not in wb.sheetnames
        from src.report import audit as audit_mod
        from src.report.analyst import _OUT_DIR as _cur_out  # noqa: F401

        audit_wb = openpyxl.load_workbook(
            audit_mod.run(state="AZ", lens="source", out=analyst._OUT_DIR)
        )
        audit = audit_wb["Audit Trail"]
        assert audit.freeze_panes == "G2"
        assert audit.sheet_properties.tabColor.rgb == "FFD9D9D9"  # audit
        readme = wb["Readme"]
        assert readme.sheet_properties.tabColor.rgb == "FFBDD7EE"  # orient
        assert not readme.auto_filter.ref  # key-value/orient sheets unfiltered
        assert wb["Score Card"].sheet_properties.tabColor.rgb == "FFBDD7EE"
        assert wb["Update Log"].sheet_properties.tabColor.rgb == "FFBDD7EE"

    def test_scoring_summary_finish_on_spine_workbook(self, tmp_path, monkeypatch):
        # Scoring Summary survives on the spine per-state workbook; its
        # finish (freeze/filter) still applies there.
        wb = self._built_workbook(tmp_path, monkeypatch, lens="spine")
        scores = wb["Scoring Summary"]
        assert scores.freeze_panes == "E2"
        assert scores.auto_filter.ref
        assert scores.sheet_properties.tabColor.rgb == "FFC6E0B4"  # work

    def test_number_formats_and_conditional_formatting(self, tmp_path, monkeypatch):
        wb = self._built_workbook(tmp_path, monkeypatch)
        details = wb["Details"]
        headers = [c.value for c in details[1]]
        nachos_col = headers.index("Base NACHOS Score") + 1
        adj_col = headers.index("Adjusted NACHOS Score") + 1
        # Fixture rows are scored (tier 0 / adj 0.0) — formats applied.
        assert details.cell(row=2, column=nachos_col).number_format == "0"
        assert details.cell(row=2, column=adj_col).number_format == "0.0"
        # Conditional formatting present on the two score columns + the
        # Needs Review Yes-highlight (fixture sidecar sets needs_review).
        cf_ranges = " ".join(str(cf.sqref) for cf in details.conditional_formatting)
        from openpyxl.utils import get_column_letter
        assert get_column_letter(nachos_col) in cf_ranges
        assert get_column_letter(adj_col) in cf_ranges
        nr_col = headers.index("AI: Needs Review") + 1
        assert get_column_letter(nr_col) in cf_ranges

    def test_every_sheet_title_resolves_in_registry(self, tmp_path, monkeypatch):
        """The build-time non-drift guard: a sheet this build produced
        that lacked a `_SHEET_FINISH` entry would have raised KeyError
        during `run()`; assert resolution explicitly + the negative."""
        import pytest

        wb = self._built_workbook(tmp_path, monkeypatch)
        for title in wb.sheetnames:
            assert analyst._finish_for(title) is not None
        assert analyst._finish_for("AZ — Documented only") is analyst._DETAILS_FINISH
        with pytest.raises(KeyError):
            analyst._finish_for("Some Future Sheet")


class TestLegendSheet:
    """The generated in-book glossary: covers the full analyst-facing
    scoring vocabulary from the live constants. Option B moved Legend
    from position 2 (Update Log now sits there) to the trailing lookup
    block."""

    def _legend_values(self, tmp_path, monkeypatch) -> set:
        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert "Legend" in wb.sheetnames
        assert wb.sheetnames[1] == "Update Log"  # Legend no longer at pos 2
        ws = wb["Legend"]
        values = set()
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if v is not None:
                    values.add(v)
        return values

    def test_legend_covers_vocabulary(self, tmp_path, monkeypatch):
        from src.report.review_queue import ROUTES
        from src.score import rubric
        from src.score.schema import ENUM_VALUED_FACTS

        values = self._legend_values(tmp_path, monkeypatch)
        # Every display vocabulary value the workbook can render is
        # explained in-book.
        for v in analyst._MATCH_STATUS_BY_SOURCE.values():
            assert v in values
        for v in analyst._DOC_SOURCE_DISPLAY.values():
            assert v in values
        for v in analyst._STRUCTURAL_DEPTH_LABELS.values():
            assert v in values
        for v in analyst._DOC_STYLE_DISPLAY.values():
            assert v in values
        for route in ROUTES:
            assert route in values
        for tier in (0, 1, 2, 3):
            assert tier in values
        for label in rubric.ADJUSTMENT_MEANINGS:
            assert label in values
        for rule in rubric.RULE_MEANINGS:
            assert rule in values
        for fact in ENUM_VALUED_FACTS:
            assert fact in values
        for conf in rubric.CONFIDENCE_MEANINGS:
            assert conf in values

    def test_legend_new_option_b_sections(self, tmp_path, monkeypatch):
        """Option B added two generated sections: the analyst-input band
        explainer and the #174 former-names terminology table."""
        from src.report.workbook_spec import TERMINOLOGY_FORMER_NAMES

        values = self._legend_values(tmp_path, monkeypatch)
        assert "Analyst-input columns (Details, green headers)" in values
        assert "Terminology — former names (pre-2026-07 artifacts)" in values
        # Section retitles for the NACHOS Score Context blocks.
        assert (
            "NACHOS Score Context — Implementation Shape "
            "(formerly Structural Depth)"
        ) in values
        assert "NACHOS Score Context — Documentation Style" in values
        # Every terminology row renders its current term + former name.
        for current, former, note in TERMINOLOGY_FORMER_NAMES:
            assert current in values
            assert f"{former} — {note}" in values

    def test_legend_band_prose_matches_band_comment(self, tmp_path, monkeypatch):
        """Issue #211 item 1c: the Legend's analyst-band explainer renders
        from the same constant as the band header-cell comment
        (`workbook_render.ANALYST_BAND_NOTE`), so the two surfaces can't
        diverge — the Legend told analysts entries were "not yet
        preserved" for months after the Option-C round-trip shipped,
        directly discouraging use of the band. The column range is
        derived from the spec's `analyst_input` role for the same
        reason (the old prose still said "Required … Validated By"
        after `Reviewed?` + the override column joined the band).
        """
        from src.report.workbook_render import ANALYST_BAND_NOTE
        from src.report.workbook_spec import DETAILS_COLUMNS

        values = self._legend_values(tmp_path, monkeypatch)
        prose = [
            v for v in values
            if isinstance(v, str) and v.startswith(ANALYST_BAND_NOTE)
        ]
        assert len(prose) == 1, "Legend must render ANALYST_BAND_NOTE verbatim"
        assert "round-trip" in prose[0]
        assert "not yet preserved" not in prose[0]
        band = [c.header for c in DETAILS_COLUMNS if c.role == "analyst_input"]
        assert f"{band[0]} … {band[-1]}" in values


class TestReviewQueueFrontSheet:
    """Option C PR 2 — the Review Queue as the per-state front work sheet
    (position 2), with OVERRIDE disagreement rows first and Row #
    hyperlinks into the rendered Details sheet."""

    def _setup(self, tmp_path, monkeypatch, *, curation_payload=None,
               needs_review=True):
        from src.report import curation as curation_mod

        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        cur_dir = tmp_path / "data" / "curation"
        records = _az_elements().elements
        integration = TestPhaseDScoringIntegration()
        integration._write_scores_sidecar(
            out, "AZ", "source", records, adjusted=2.5,
        )
        if not needs_review:
            # Rewrite the sidecar with the review flag off.
            sidecar_path = out / "az_scores_source.json"
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
            for s in payload["scores"]:
                s["review"] = {"needs_review": False, "reasons": [],
                               "route": None}
            sidecar_path.write_text(json.dumps(payload), encoding="utf-8")
        if curation_payload is not None:
            cur_dir.mkdir(parents=True, exist_ok=True)
            (cur_dir / "az.json").write_text(
                json.dumps(curation_payload), encoding="utf-8"
            )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        monkeypatch.setattr(curation_mod, "_CURATION_DIR", cur_dir)
        return out

    @staticmethod
    def _curation(value, *, lens="source", column="analyst_adjusted_override"):
        return {
            "version": 1,
            "state": "AZ",
            "entries": {
                "AZ|Calendar|calendarCode": {
                    "values": {
                        column: {"value": value, "lens": lens},
                    },
                    "history": [],
                }
            },
        }

    def test_queue_is_position_2_with_spec_headers(self, tmp_path, monkeypatch):
        out = self._setup(tmp_path, monkeypatch)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert wb.sheetnames[0] == "Readme"
        assert wb.sheetnames[1] == "Review Queue"
        ws = wb["Review Queue"]
        headers = tuple(c.value for c in ws[1])
        assert headers == (
            "Row #", "Where", "Reviewed? (from Details)", "Route",
            "State", "Entity Name", "Data Element",
            "Adjusted NACHOS Score", "confidence_composite", "Why",
            "AI: Evidence",
        )
        # Flagged fixture rows render with the joined reasons as Why.
        assert ws.max_row > 1
        assert _cell(ws, 2, "Why") == "low_confidence_dimension:definition_quality"
        # All-documented fixture: every row lives on Details (issue #259).
        assert _cell(ws, 2, "Where") == "Details"

    def test_override_rows_sort_first_and_links_hit_the_right_row(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(
            tmp_path, monkeypatch, curation_payload=self._curation(1.0)
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        ws = wb["Review Queue"]
        assert _cell(ws, 2, "Route") == "OVERRIDE"
        assert _cell(ws, 2, "Entity Name") == "Calendar"
        assert _cell(ws, 2, "Why") == "analyst override 1 vs engine adjusted 2.5"
        assert _cell(ws, 2, "Adjusted NACHOS Score") == 2.5
        # Engine score cells untouched: the override lives in the band.
        details = wb["Details"]
        # The hyperlink must land on the row whose identity cells match —
        # asserted against the TARGET coordinates, not link arithmetic.
        link_cell = ws.cell(row=2, column=_col(ws, "Row #"))
        assert link_cell.hyperlink is not None
        loc = link_cell.hyperlink.location
        assert loc.startswith("'Details'!A")
        target_row = int(loc.rsplit("A", 1)[1])
        assert _cell(details, target_row, "Entity Name") == "Calendar"
        assert _cell(details, target_row, "Data Element") == "calendarCode"
        assert (
            details.cell(row=target_row, column=1).value == link_cell.value
        )
        # Cascade-flagged rows follow, still routed.
        assert _cell(ws, 3, "Route") != "OVERRIDE"

    def test_base_override_row_states_axis_in_why(
        self, tmp_path, monkeypatch
    ):
        """Issue #250 — a base-axis disagreement renders its own OVERRIDE
        row; the numeric column still shows the ENGINE ADJUSTED value
        (the base tier numbers live only in the why-text)."""
        out = self._setup(
            tmp_path, monkeypatch,
            curation_payload=self._curation(
                1.0, column="analyst_base_override"
            ),
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Review Queue"]
        assert _cell(ws, 2, "Route") == "OVERRIDE"
        assert _cell(ws, 2, "Why") == "analyst base override 1 vs engine base 0"
        assert _cell(ws, 2, "Adjusted NACHOS Score") == 2.5

    def test_both_axes_render_one_override_row_per_axis(
        self, tmp_path, monkeypatch
    ):
        """A record contested on both layers appears twice under
        OVERRIDE — adjusted (headline) first — each row hyperlinking to
        the same Details row (documented-not-deduped, issues #213/#250)."""
        payload = self._curation(1.5)
        payload["entries"]["AZ|Calendar|calendarCode"]["values"][
            "analyst_base_override"
        ] = {"value": 1.0, "lens": "source"}
        out = self._setup(tmp_path, monkeypatch, curation_payload=payload)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        ws = wb["Review Queue"]
        assert _cell(ws, 2, "Route") == "OVERRIDE"
        assert _cell(ws, 3, "Route") == "OVERRIDE"
        assert _cell(ws, 2, "Why") == "analyst override 1.5 vs engine adjusted 2.5"
        assert _cell(ws, 3, "Why") == "analyst base override 1 vs engine base 0"
        links = {
            ws.cell(row=r, column=_col(ws, "Row #")).hyperlink.location
            for r in (2, 3)
        }
        assert len(links) == 1  # same Details row, two contested axes
        assert _cell(ws, 4, "Route") != "OVERRIDE"

    def test_agreeing_override_produces_no_override_row(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(
            tmp_path, monkeypatch, curation_payload=self._curation(2.5)
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Review Queue"]
        routes = {
            _cell(ws, r, "Route") for r in range(2, ws.max_row + 1)
        }
        assert "OVERRIDE" not in routes

    def test_agreeing_base_override_produces_no_override_row(
        self, tmp_path, monkeypatch
    ):
        # Fixture engine base tier is 0 — an override of 0.0 agrees.
        out = self._setup(
            tmp_path, monkeypatch,
            curation_payload=self._curation(
                0.0, column="analyst_base_override"
            ),
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Review Queue"]
        routes = {
            _cell(ws, r, "Route") for r in range(2, ws.max_row + 1)
        }
        assert "OVERRIDE" not in routes

    def test_spine_captured_override_does_not_flag_source_render(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(
            tmp_path, monkeypatch,
            curation_payload=self._curation(1.0, lens="spine"),
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Review Queue"]
        routes = {
            _cell(ws, r, "Route") for r in range(2, ws.max_row + 1)
        }
        assert "OVERRIDE" not in routes

    def test_spine_captured_base_override_does_not_flag_source_render(
        self, tmp_path, monkeypatch
    ):
        # The base axis is lens-gated exactly like the adjusted axis.
        out = self._setup(
            tmp_path, monkeypatch,
            curation_payload=self._curation(
                1.0, lens="spine", column="analyst_base_override"
            ),
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Review Queue"]
        routes = {
            _cell(ws, r, "Route") for r in range(2, ws.max_row + 1)
        }
        assert "OVERRIDE" not in routes

    def test_empty_queue_renders_header_only(self, tmp_path, monkeypatch):
        out = self._setup(tmp_path, monkeypatch, needs_review=False)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert wb.sheetnames[1] == "Review Queue"
        ws = wb["Review Queue"]
        assert ws.max_row == 1  # honestly-empty beats a missing tab

    def test_no_scores_no_queue_sheet(self, tmp_path, monkeypatch):
        from src.report import curation as curation_mod

        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        monkeypatch.setattr(
            curation_mod, "_CURATION_DIR", tmp_path / "data" / "curation"
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        assert "Review Queue" not in openpyxl.load_workbook(path).sheetnames


class TestReviewQueueLocationFirstSort:
    """Issue #259 PR 1 — the queue groups by where each row's Row # link
    lands (Details → Documentation Gaps → record gone), keeps the
    priority ladder within each group, names the target sheet in the
    Where column, and links the Documentation Gaps block instead of
    leaving those Row # cells dangling."""

    def _records(self) -> list[ElementRecord]:
        return [
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="SchoolCalendar",
                entity="Calendar", element_name="calendarCode",
                definition_text="doc", source="core", documented=True,
            ),
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="EducationOrganization",
                entity="School", element_name="schoolId",
                definition_text="doc", source="core", documented=True,
            ),
            ElementRecord(
                state="AZ", edfi_version="4.0", domain="",
                entity="Calendar", element_name="swaggerOnlyField",
                definition_text="", source="core", documented=False,
            ),
        ]

    def _setup(self, tmp_path, monkeypatch, *, ghost_score=False):
        from src.report import curation as curation_mod

        out = tmp_path / "data" / "out"
        out.mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "spine").mkdir(parents=True, exist_ok=True)
        rows = self._records()
        elements = StateElements(
            state="AZ", edfi_version="4.0",
            extracted_at=datetime.now(timezone.utc),
            element_count=len(rows), elements=rows,
        )
        (out / "az_elements_source.json").write_text(
            elements.model_dump_json(indent=2), encoding="utf-8"
        )
        (tmp_path / "data" / "spine" / "az_spine.json").write_text(
            _az_spine().model_dump_json(indent=2), encoding="utf-8"
        )
        (out / "az_gap_log.json").write_text(
            json.dumps(_az_gap_log()), encoding="utf-8"
        )
        scored = list(rows)
        if ghost_score:
            # A score whose record no longer exists in the elements
            # artifact — e.g. a source re-ingest dropped the row.
            scored.append(ElementRecord(
                state="AZ", edfi_version="4.0", domain="Assessment",
                entity="Assessment", element_name="ghostField",
                definition_text="", source="core", documented=True,
            ))
        TestPhaseDScoringIntegration()._write_scores_sidecar(
            out, "AZ", "source", scored, adjusted=2.5,
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        monkeypatch.setattr(
            curation_mod, "_CURATION_DIR", tmp_path / "data" / "curation"
        )
        return out

    def test_gaps_rows_group_after_details_and_link_to_gaps_sheet(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(tmp_path, monkeypatch)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        ws = wb["Review Queue"]
        # All three scored rows are cascade-flagged with the same route;
        # the undocumented one groups after the documented block anyway.
        wheres = [_cell(ws, r, "Where") for r in range(2, ws.max_row + 1)]
        assert wheres == ["Details", "Details", "Documentation Gaps"]
        elements_in_order = [
            _cell(ws, r, "Data Element") for r in range(2, ws.max_row + 1)
        ]
        assert elements_in_order == [
            "calendarCode", "schoolId", "swaggerOnlyField",
        ]
        # The relocated row's Row # link lands on Documentation Gaps, on
        # the row whose identity cells match (no more dangling links).
        gaps = wb["Documentation Gaps"]
        link_cell = ws.cell(row=4, column=_col(ws, "Row #"))
        assert link_cell.hyperlink is not None
        loc = link_cell.hyperlink.location
        assert loc.startswith("'Documentation Gaps'!A")
        target_row = int(loc.rsplit("A", 1)[1])
        assert _cell(gaps, target_row, "Element") == "swaggerOnlyField"
        # Documented rows still link into Details.
        details_loc = ws.cell(
            row=2, column=_col(ws, "Row #")
        ).hyperlink.location
        assert details_loc.startswith("'Details'!A")

    def test_record_gone_rows_sort_last_without_links(
        self, tmp_path, monkeypatch
    ):
        """A scored record on no rendered sheet sorts LAST under
        '(record gone)' with no link — pre-#259 such rows could top the
        queue while their Row # cell went nowhere."""
        out = self._setup(tmp_path, monkeypatch, ghost_score=True)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Review Queue"]
        last = ws.max_row
        assert _cell(ws, last, "Where") == "(record gone)"
        assert _cell(ws, last, "Data Element") == "ghostField"
        assert _cell(ws, last, "Row #") is None
        assert ws.cell(row=last, column=_col(ws, "Row #")).hyperlink is None
        # Location-first: the gone row lands below even the
        # Documentation Gaps block, despite sorting first by entity.
        wheres = [_cell(ws, r, "Where") for r in range(2, last + 1)]
        assert wheres == [
            "Details", "Details", "Documentation Gaps", "(record gone)",
        ]


class TestReviewQueueReviewedEcho:
    """Issue #259 PR 3 — the queue's read-only `Reviewed? (from
    Details)` progress column: a live INDEX/MATCH echo of the Details
    band mark, keyed on Row # so analyst re-sorting never breaks it."""

    _ECHO = "Reviewed? (from Details)"

    def test_rows_with_a_home_sheet_carry_the_formula(
        self, tmp_path, monkeypatch
    ):
        out = TestReviewQueueLocationFirstSort()._setup(tmp_path, monkeypatch)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Review Queue"]
        col = _col(ws, self._ECHO)
        # Details AND Documentation Gaps rows both get the formula —
        # MATCH misses on the gaps rows resolve to "" via IFERROR.
        assert ws.max_row > 1
        for r in range(2, ws.max_row + 1):
            v = ws.cell(row=r, column=col).value
            assert isinstance(v, str)
            assert v.startswith("=IFERROR(INDEX(Details!$")
            # The `&""` coercion is load-bearing: bare INDEX over an
            # empty cell renders 0, not blank.
            assert v.endswith('&"","")')
            # Keyed on THIS row's Row # cell (queue column A).
            assert f"MATCH($A{r}," in v
        # Direct post-render assignment lands as a real formula — the
        # renderer's `_set_cell` guard is deliberately bypassed.
        assert ws.cell(row=2, column=col).data_type == "f"

    def test_record_gone_rows_stay_blank(self, tmp_path, monkeypatch):
        out = TestReviewQueueLocationFirstSort()._setup(
            tmp_path, monkeypatch, ghost_score=True
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Review Queue"]
        last = ws.max_row
        assert _cell(ws, last, "Where") == "(record gone)"
        assert ws.cell(row=last, column=_col(ws, self._ECHO)).value is None


class TestDetailsReviewWhyPriority:
    """Issue #259 PR 2 — `AI: Review Why` / `AI: Review Priority` on
    Details: the queue's signal merged per record, derived from the
    SAME `_review_queue_entries` the queue renders (threaded via
    `RowContext.review_flag`), so the two surfaces cannot diverge."""

    _WHY = "AI: Review Why"
    _PRIORITY = "AI: Review Priority"
    _CASCADE_REASON = "low_confidence_dimension:definition_quality"

    def _details_cell(self, wb, element, header):
        return TestAdjudicationRendering._details_cell(wb, element, header)

    def test_cascade_rows_carry_queue_why_and_route_priority(
        self, tmp_path, monkeypatch
    ):
        out = TestReviewQueueFrontSheet()._setup(tmp_path, monkeypatch)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        for element in ("calendarCode", "schoolId"):
            assert self._details_cell(
                wb, element, self._WHY
            ) == self._CASCADE_REASON
            # ANALYST route = ladder position 6.
            assert self._details_cell(wb, element, self._PRIORITY) == 6
        # The text IS the queue's Why — same composition, verbatim.
        queue = wb["Review Queue"]
        assert _cell(queue, 2, "Why") == self._details_cell(
            wb, _cell(queue, 2, "Data Element"), self._WHY
        )

    def test_unflagged_rows_render_blank(self, tmp_path, monkeypatch):
        out = TestReviewQueueFrontSheet()._setup(
            tmp_path, monkeypatch, needs_review=False
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        for element in ("calendarCode", "schoolId"):
            assert self._details_cell(wb, element, self._WHY) is None
            assert self._details_cell(wb, element, self._PRIORITY) is None

    def test_override_prepends_sentence_and_lifts_priority(
        self, tmp_path, monkeypatch
    ):
        out = TestReviewQueueFrontSheet()._setup(
            tmp_path, monkeypatch,
            curation_payload=TestReviewQueueFrontSheet._curation(1.0),
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        # Contested record: OVERRIDE sentence first (ladder order), then
        # the cascade reasons; priority = the record's BEST position (2).
        assert self._details_cell(wb, "calendarCode", self._WHY) == (
            "analyst override 1 vs engine adjusted 2.5; "
            + self._CASCADE_REASON
        )
        assert self._details_cell(wb, "calendarCode", self._PRIORITY) == 2
        # The uncontested record keeps its cascade-only signal.
        assert self._details_cell(
            wb, "schoolId", self._WHY
        ) == self._CASCADE_REASON
        assert self._details_cell(wb, "schoolId", self._PRIORITY) == 6

    def test_fresh_adjudication_suppresses_override_sentence(
        self, tmp_path, monkeypatch
    ):
        # Mirrors the queue exactly (free by construction — same
        # entries): a fresh adjudication resolves the adjusted-axis
        # disagreement, so its sentence never reaches Details either.
        out = TestReviewQueueFrontSheet()._setup(
            tmp_path, monkeypatch,
            curation_payload=TestAdjudicationRendering._payload(
                value=2.0, override=1.0
            ),
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert self._details_cell(
            wb, "calendarCode", self._WHY
        ) == self._CASCADE_REASON
        assert self._details_cell(wb, "calendarCode", self._PRIORITY) == 6

    def test_stale_adjudication_tops_priority_and_joins_all_flavors(
        self, tmp_path, monkeypatch
    ):
        from src.score.aggregate import SCORING_PLAN_VERSION

        out = TestReviewQueueFrontSheet()._setup(
            tmp_path, monkeypatch,
            curation_payload=TestAdjudicationRendering._payload(
                value=2.0, override=1.0, plan="0"
            ),
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        # Stale consensus: RE-ADJUDICATE (1) leads, the re-surfaced
        # OVERRIDE sentence follows, cascade reasons last — one string,
        # ladder order, '; '-joined.
        assert self._details_cell(wb, "calendarCode", self._WHY) == (
            f"adjudicated 2 (plan v0) is stale — plan v0 → "
            f"v{SCORING_PLAN_VERSION}; re-adjudicate; "
            "analyst override 1 vs engine adjusted 2.5; "
            + self._CASCADE_REASON
        )
        assert self._details_cell(wb, "calendarCode", self._PRIORITY) == 1
        # Consistency: Details why == the record's queue Whys, joined in
        # queue (ladder) order.
        queue = wb["Review Queue"]
        whys = [
            _cell(queue, r, "Why")
            for r in range(2, queue.max_row + 1)
            if _cell(queue, r, "Data Element") == "calendarCode"
        ]
        assert "; ".join(whys) == self._details_cell(
            wb, "calendarCode", self._WHY
        )


class TestScoreCardOverrideClusters:
    """Issue #248 Part A — the override-clustering diagnostic block on
    the Score Card (per-state Block E + the combined cross-state
    variant), fed by curation override disagreements."""

    _TITLE = "Override clustering — analyst vs engine"
    _EMPTY = "No analyst override disagreements captured for this lens."

    def _setup(self, tmp_path, monkeypatch, *, curation_payload=None,
               adjusted=2.5, justification="tier_0_none"):
        from src.report import curation as curation_mod

        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        cur_dir = tmp_path / "data" / "curation"
        records = _az_elements().elements
        TestPhaseDScoringIntegration()._write_scores_sidecar(
            out, "AZ", "source", records,
            adjusted=adjusted, justification=justification,
        )
        if curation_payload is not None:
            cur_dir.mkdir(parents=True, exist_ok=True)
            (cur_dir / "az.json").write_text(
                json.dumps(curation_payload), encoding="utf-8"
            )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        monkeypatch.setattr(curation_mod, "_CURATION_DIR", cur_dir)
        return out

    @staticmethod
    def _find_row(ws, value):
        for r in range(1, ws.max_row + 1):
            if ws.cell(row=r, column=1).value == value:
                return r
        return None

    def _cluster_rows(self, ws):
        """{cluster label: (axis, direction, rows, mean Δ, notes)} read
        from the per-state table (no States column)."""
        title_row = self._find_row(ws, self._TITLE)
        assert title_row is not None, "clustering block missing"
        header_row = title_row + 2
        assert ws.cell(row=header_row, column=1).value == "Cluster"
        out = {}
        for r in range(header_row + 1, ws.max_row + 1):
            label = ws.cell(row=r, column=1).value
            if not label:
                break
            out[label] = tuple(
                ws.cell(row=r, column=c).value for c in range(2, 7)
            )
        return out

    def test_adjusted_axis_labels_cluster_renders(self, tmp_path, monkeypatch):
        out = self._setup(
            tmp_path, monkeypatch,
            curation_payload=TestReviewQueueFrontSheet._curation(1.0),
            justification="tier_0_none; +0.5 necessary_ext",
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Score Card"]
        rows = self._cluster_rows(ws)
        assert rows == {
            "+0.5 necessary extension":
                ("adjusted", "lowered", 1, "-1.50", None),
        }

    def test_base_axis_clusters_by_rule(self, tmp_path, monkeypatch):
        out = self._setup(
            tmp_path, monkeypatch,
            curation_payload=TestReviewQueueFrontSheet._curation(
                1.0, column="analyst_base_override"
            ),
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Score Card"]
        rows = self._cluster_rows(ws)
        # Fixture base tier is 0 → the analyst raised by 1.0.
        assert rows == {
            "rule: tier_0_none — no derivation logic":
                ("base", "raised", 1, "+1.00", None),
        }

    def test_dual_fire_note_renders(self, tmp_path, monkeypatch):
        out = self._setup(
            tmp_path, monkeypatch,
            curation_payload=TestReviewQueueFrontSheet._curation(2.0),
            adjusted=1.0,
            justification=(
                "tier_0_none; +0.5 necessary_ext, "
                "+1.0 fidelity_divergent_unclear"
            ),
        )
        # Mark the fixture rows dual-fire (the sidecar writer's default
        # reasons are confidence flags).
        sidecar = out / "az_scores_source.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        for s in payload["scores"]:
            s["review"]["reasons"] = ["fidelity_necessity_dual_fire"]
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Score Card"]
        rows = self._cluster_rows(ws)
        label = (
            "+0.5 necessary extension + "
            "+1.0 meaning diverges from Ed-Fi (unexplained)"
        )
        # Δ from override − engine (2.0 − 1.0), NEVER the label sum.
        assert rows == {
            label: ("adjusted", "raised", 1, "+1.00",
                    "dual-fire ×1 — larger adjustment applies, not the sum"),
        }

    def test_scored_but_disagreement_free_renders_labeled_empty_state(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(tmp_path, monkeypatch)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Score Card"]
        title_row = self._find_row(ws, self._TITLE)
        assert title_row is not None
        assert ws.cell(row=title_row + 2, column=1).value == self._EMPTY

    def test_combined_workbook_clusters_across_states(
        self, tmp_path, monkeypatch
    ):
        """Cross-state view: the same label-set contested in two states
        reads as ONE cluster with a per-state States cell."""
        from src.report import curation as curation_mod

        out = tmp_path / "data" / "out"
        out.mkdir(parents=True, exist_ok=True)
        spine_dir = tmp_path / "data" / "spine"
        spine_dir.mkdir(parents=True, exist_ok=True)
        cur_dir = tmp_path / "data" / "curation"
        cur_dir.mkdir(parents=True, exist_ok=True)
        integration = TestPhaseDScoringIntegration()
        for state in ("AZ", "WI", "MN", "TX", "IN"):
            records = [
                ElementRecord(
                    state=state, edfi_version="4.0", domain="SchoolCalendar",
                    entity="Calendar", element_name="calendarCode",
                    definition_text=f"{state} calendar code",
                    source="core", documented=True,
                ),
            ]
            elements = StateElements(
                state=state, edfi_version="4.0",
                extracted_at=datetime.now(timezone.utc),
                element_count=1, elements=records,
            )
            (out / f"{state.lower()}_elements_source.json").write_text(
                elements.model_dump_json(indent=2), encoding="utf-8"
            )
            spine = _az_spine().model_copy(update={"state": state})
            (spine_dir / f"{state.lower()}_spine.json").write_text(
                spine.model_dump_json(indent=2), encoding="utf-8"
            )
            if state in ("AZ", "WI"):
                integration._write_scores_sidecar(
                    out, state, "source", records, adjusted=2.5,
                    justification="tier_0_none; +0.5 necessary_ext",
                )
                (cur_dir / f"{state.lower()}.json").write_text(
                    json.dumps({
                        "version": 1,
                        "state": state,
                        "entries": {
                            f"{state}|Calendar|calendarCode": {
                                "values": {
                                    "analyst_adjusted_override": {
                                        "value": 1.0, "lens": "source",
                                    },
                                },
                                "history": [],
                            }
                        },
                    }),
                    encoding="utf-8",
                )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)
        monkeypatch.setattr(curation_mod, "_CURATION_DIR", cur_dir)
        produced = analyst.run(out_dir=out, lens="source")
        combined = next(p for p in produced if p.name == "coverage_analyst.xlsx")
        ws = openpyxl.load_workbook(combined)["Score Card"]
        title_row = self._find_row(ws, self._TITLE)
        assert title_row is not None
        header_row = title_row + 2
        headers = tuple(
            ws.cell(row=header_row, column=c).value for c in range(1, 8)
        )
        assert headers == (
            "Cluster", "Axis", "Direction", "Rows",
            "Mean Δ (override − engine)", "States", "Notes",
        )
        data = tuple(
            ws.cell(row=header_row + 1, column=c).value for c in range(1, 7)
        )
        assert data == (
            "+0.5 necessary extension", "adjusted", "lowered", 2,
            "-1.50", "AZ 1, WI 1",
        )


class TestAdjudicationRendering:
    """Issue #248 Part C — Effective Score column, OVERRIDE suppression /
    RE-ADJUDICATE queue rows, the Update Log register, and the Score
    Card adjudication rollup."""

    _EFFECTIVE = "Effective Score (adjudicated)"

    @staticmethod
    def _payload(value=2.0, *, engine_at=2.5, lens="source", plan=None,
                 override=None, override_column="analyst_adjusted_override"):
        from src.score.aggregate import SCORING_PLAN_VERSION

        entry: dict = {
            "values": {},
            "history": [],
            "adjudication": {
                "status": "adjudicated",
                "axis": "adjusted",
                "value": value,
                "lens": lens,
                "agreed_by": ["Doug", "Maria", "Chris"],
                "decided_at": "2026-07-13T12:00:00+00:00",
                "rationale": "single-source condition; consensus 07-13",
                "engine_score_at_decision": engine_at,
                "plan_version_at_decision": plan or SCORING_PLAN_VERSION,
            },
        }
        if override is not None:
            entry["values"][override_column] = {
                "value": override, "lens": lens,
            }
        return {
            "version": 2,
            "state": "AZ",
            "entries": {"AZ|Calendar|calendarCode": entry},
        }

    def _setup(self, tmp_path, monkeypatch, payload):
        return TestReviewQueueFrontSheet()._setup(
            tmp_path, monkeypatch, curation_payload=payload
        )

    @staticmethod
    def _details_cell(wb, element, header):
        ws = wb["Details"]
        for r in range(2, ws.max_row + 1):
            if _cell(ws, r, "Data Element") == element:
                return _cell(ws, r, header)
        raise AssertionError(f"{element} not found on Details")

    @staticmethod
    def _routes(wb):
        ws = wb["Review Queue"]
        return [_cell(ws, r, "Route") for r in range(2, ws.max_row + 1)]

    def test_fresh_adjudication_renders_and_resolves_override(
        self, tmp_path, monkeypatch
    ):
        from src.score.aggregate import SCORING_PLAN_VERSION

        out = self._setup(
            tmp_path, monkeypatch, self._payload(value=2.0, override=1.0)
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        # Effective renders beside the UNTOUCHED engine score.
        assert self._details_cell(wb, "calendarCode", self._EFFECTIVE) == 2.0
        assert self._details_cell(
            wb, "calendarCode", "Adjusted NACHOS Score"
        ) == 2.5
        # The adjusted-axis disagreement is resolved — no OVERRIDE row.
        assert "OVERRIDE" not in self._routes(wb)
        assert "RE-ADJUDICATE" not in self._routes(wb)
        # Register renders on the Update Log (per-state: no State col).
        ul = wb["Update Log"]
        labels = [
            ul.cell(row=r, column=1).value for r in range(1, ul.max_row + 1)
        ]
        reg_title = next(
            (v for v in labels
             if isinstance(v, str) and v.startswith("Adjudication register")),
            None,
        )
        assert reg_title is not None
        header_row = labels.index(reg_title) + 2
        assert ul.cell(row=header_row, column=1).value == "Entity"
        data = [
            ul.cell(row=header_row + 1, column=c).value for c in range(1, 10)
        ]
        assert data == [
            "Calendar", "calendarCode", 2.0, 2.5,
            f"v{SCORING_PLAN_VERSION}",
            "fresh", "Doug; Maria; Chris", "2026-07-13T12:00:00+00:00",
            "single-source condition; consensus 07-13",
        ]

    def test_fresh_adjudication_leaves_base_override_row(
        self, tmp_path, monkeypatch
    ):
        # Adjudication is adjusted-axis only — a base-axis disagreement
        # on the same record still queues.
        out = self._setup(
            tmp_path, monkeypatch,
            self._payload(
                value=2.0, override=1.0,
                override_column="analyst_base_override",
            ),
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        ws = wb["Review Queue"]
        assert _cell(ws, 2, "Route") == "OVERRIDE"
        assert _cell(ws, 2, "Why") == "analyst base override 1 vs engine base 0"
        assert self._details_cell(wb, "calendarCode", self._EFFECTIVE) == 2.0

    def test_stale_adjudication_blanks_effective_and_flags(
        self, tmp_path, monkeypatch
    ):
        from src.score.aggregate import SCORING_PLAN_VERSION

        out = self._setup(
            tmp_path, monkeypatch,
            self._payload(value=2.0, engine_at=0.5, override=1.0),
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        # Stale consensus never renders.
        assert self._details_cell(wb, "calendarCode", self._EFFECTIVE) is None
        ws = wb["Review Queue"]
        # RE-ADJUDICATE tops the queue; the unresolved OVERRIDE follows.
        assert _cell(ws, 2, "Route") == "RE-ADJUDICATE"
        assert _cell(ws, 2, "Why") == (
            f"adjudicated 2 (plan v{SCORING_PLAN_VERSION}) is stale — "
            "engine adjusted moved 0.5 → 2.5; re-adjudicate"
        )
        assert _cell(ws, 2, "Adjusted NACHOS Score") == 2.5
        assert _cell(ws, 3, "Route") == "OVERRIDE"
        # Register still lists the decision, marked stale.
        ul = wb["Update Log"]
        cells = [
            ul.cell(row=r, column=6).value for r in range(1, ul.max_row + 1)
        ]
        assert any(
            isinstance(v, str) and v.startswith("STALE — re-adjudicate")
            for v in cells
        )

    def test_plan_bump_makes_adjudication_stale(self, tmp_path, monkeypatch):
        out = self._setup(
            tmp_path, monkeypatch, self._payload(value=2.0, plan="0")
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert self._details_cell(wb, "calendarCode", self._EFFECTIVE) is None
        assert "RE-ADJUDICATE" in self._routes(wb)

    def test_spine_adjudication_invisible_on_source_render(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(
            tmp_path, monkeypatch, self._payload(value=2.0, lens="spine")
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert self._details_cell(wb, "calendarCode", self._EFFECTIVE) is None
        assert "RE-ADJUDICATE" not in self._routes(wb)
        ul = wb["Update Log"]
        labels = [
            ul.cell(row=r, column=1).value for r in range(1, ul.max_row + 1)
        ]
        assert not any(
            isinstance(v, str) and v.startswith("Adjudication register")
            for v in labels
        )

    def test_score_card_rollup_counts_and_effective_mean(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(tmp_path, monkeypatch, self._payload(value=2.0))
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        sc = openpyxl.load_workbook(path)["Score Card"]
        metrics = {
            sc.cell(row=r, column=1).value: sc.cell(row=r, column=2).value
            for r in range(1, sc.max_row + 1)
        }
        n = metrics["records_scored"]
        assert metrics["Adjudicated rows (team consensus)"] == f"1 of {n}"
        # Fixture: every headline row engine 2.5, one adjudicated to
        # 2.0 → mean effective = (2.0 + 2.5·(n−1)) / n.
        expected = (2.0 + 2.5 * (n - 1)) / n
        assert metrics[
            "Mean Effective Score (adjudicated-else-engine)"
        ] == f"{expected:.2f}"

    def test_score_card_rollup_without_adjudications_is_honest_zero(
        self, tmp_path, monkeypatch
    ):
        out = TestReviewQueueFrontSheet()._setup(tmp_path, monkeypatch)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        sc = openpyxl.load_workbook(path)["Score Card"]
        metrics = {
            sc.cell(row=r, column=1).value: sc.cell(row=r, column=2).value
            for r in range(1, sc.max_row + 1)
        }
        n = metrics["records_scored"]
        assert metrics["Adjudicated rows (team consensus)"] == f"0 of {n}"
        # At zero the effective mean is byte-identical to the engine
        # mean — pure noise, so the row is absent.
        assert "Mean Effective Score (adjudicated-else-engine)" not in metrics

    def test_end_to_end_adjudicate_then_render_then_plan_bump(
        self, tmp_path, monkeypatch
    ):
        """The full loop: `curation.adjudicate()` → render (Effective
        populated, OVERRIDE gone) → simulated plan bump → stale
        everywhere."""
        from src.report import curation as curation_mod

        out = self._setup(
            tmp_path, monkeypatch,
            # Start from a plain disagreeing override, no adjudication.
            TestReviewQueueFrontSheet._curation(1.0),
        )
        cur_dir = tmp_path / "data" / "curation"
        report = curation_mod.adjudicate(
            "az", "Calendar", "calendarCode",
            value=2.0, agreed_by=("Doug", "Maria"), rationale="e2e",
            out_base=out, curation_base=cur_dir,
        )
        assert report.engine_score_at_decision == 2.5
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert self._details_cell(wb, "calendarCode", self._EFFECTIVE) == 2.0
        assert "OVERRIDE" not in self._routes(wb)
        # Simulate the next methodology version.
        import src.score.aggregate as aggregate_mod

        monkeypatch.setattr(aggregate_mod, "SCORING_PLAN_VERSION", "99")
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert self._details_cell(wb, "calendarCode", self._EFFECTIVE) is None
        routes = self._routes(wb)
        assert "RE-ADJUDICATE" in routes
        assert "OVERRIDE" in routes  # the disagreement re-surfaces

    def test_combined_workbook_register_merges_states(
        self, tmp_path, monkeypatch
    ):
        from src.report import curation as curation_mod
        from src.score.aggregate import SCORING_PLAN_VERSION

        out = tmp_path / "data" / "out"
        out.mkdir(parents=True, exist_ok=True)
        spine_dir = tmp_path / "data" / "spine"
        spine_dir.mkdir(parents=True, exist_ok=True)
        cur_dir = tmp_path / "data" / "curation"
        cur_dir.mkdir(parents=True, exist_ok=True)
        integration = TestPhaseDScoringIntegration()
        for state in ("AZ", "WI", "MN", "TX", "IN"):
            records = [
                ElementRecord(
                    state=state, edfi_version="4.0", domain="SchoolCalendar",
                    entity="Calendar", element_name="calendarCode",
                    definition_text=f"{state} calendar code",
                    source="core", documented=True,
                ),
            ]
            elements = StateElements(
                state=state, edfi_version="4.0",
                extracted_at=datetime.now(timezone.utc),
                element_count=1, elements=records,
            )
            (out / f"{state.lower()}_elements_source.json").write_text(
                elements.model_dump_json(indent=2), encoding="utf-8"
            )
            spine = _az_spine().model_copy(update={"state": state})
            (spine_dir / f"{state.lower()}_spine.json").write_text(
                spine.model_dump_json(indent=2), encoding="utf-8"
            )
            if state in ("AZ", "WI"):
                integration._write_scores_sidecar(
                    out, state, "source", records, adjusted=2.5,
                )
                payload = self._payload(value=2.0)
                payload["state"] = state
                payload["entries"] = {
                    f"{state}|Calendar|calendarCode": payload["entries"].pop(
                        "AZ|Calendar|calendarCode"
                    )
                }
                (cur_dir / f"{state.lower()}.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)
        monkeypatch.setattr(curation_mod, "_CURATION_DIR", cur_dir)
        produced = analyst.run(out_dir=out, lens="source")
        combined = next(p for p in produced if p.name == "coverage_analyst.xlsx")
        ul = openpyxl.load_workbook(combined)["Update Log"]
        labels = [
            ul.cell(row=r, column=1).value for r in range(1, ul.max_row + 1)
        ]
        reg_title = next(
            v for v in labels
            if isinstance(v, str) and v.startswith("Adjudication register")
        )
        header_row = labels.index(reg_title) + 2
        headers = [
            ul.cell(row=header_row, column=c).value for c in range(1, 11)
        ]
        assert headers == [
            "State", "Entity", "Data Element", "Adjudicated Score",
            "Engine at decision", "Plan at decision", "Status",
            "Agreed by", "Decided at", "Rationale",
        ]
        rows = [
            [ul.cell(row=header_row + i, column=c).value for c in (1, 6, 7)]
            for i in (1, 2)
        ]
        assert rows == [
            ["AZ", f"v{SCORING_PLAN_VERSION}", "fresh"],
            ["WI", f"v{SCORING_PLAN_VERSION}", "fresh"],
        ]


class TestCommitmentTracker:
    """Option C PR 3 — the generated remediation ledger with honest
    points-resolved v1 math."""

    def _setup(self, tmp_path, monkeypatch, *, justification, adjusted,
               tier=3, rule="tier_3_aggregation", curation_payload=None):
        from src.report import curation as curation_mod

        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        cur_dir = tmp_path / "data" / "curation"
        records = _az_elements().elements
        integration = TestPhaseDScoringIntegration()
        integration._write_scores_sidecar(
            out, "AZ", "source", records,
            nachos_tier=tier, nachos_rule=rule,
            adjusted=adjusted, justification=justification,
        )
        # Recommendations existence gate (rows regenerate from scores).
        (out / "az_recommendations_source.json").write_text(
            "{}", encoding="utf-8"
        )
        if curation_payload is not None:
            cur_dir.mkdir(parents=True, exist_ok=True)
            (cur_dir / "az.json").write_text(
                json.dumps(curation_payload), encoding="utf-8"
            )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        monkeypatch.setattr(curation_mod, "_CURATION_DIR", cur_dir)
        return out

    def test_points_resolved_v1_math_and_reason(self, tmp_path, monkeypatch):
        out = self._setup(
            tmp_path, monkeypatch,
            justification="tier_3_aggregation; +0.5 necessary_ext",
            adjusted=3.5,
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        assert "Commitment Tracker" in wb.sheetnames
        ws = wb["Commitment Tracker"]
        # First data row is a real element row (sorted entity, element).
        assert _cell(ws, 2, "NACHOS Points Resolved") == 3
        assert _cell(ws, 2, "Projected Adjusted NACHOS Score") == 0.5
        assert _cell(ws, 2, "Adjusted NACHOS Score") == 3.5
        reason = _cell(ws, 2, "Reason")
        assert reason.startswith("removes base tier 3")
        assert "remains:" in reason  # the adjustment caveat, verbatim
        assert _cell(ws, 2, "Recommended Action")

    def test_entity_subtotals_and_grand_total_sentence(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(
            tmp_path, monkeypatch,
            justification="tier_3_aggregation", adjusted=3.0,
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Commitment Tracker"]
        entities = [
            _cell(ws, r, "Entity Name") for r in range(2, ws.max_row + 1)
        ]
        # Two fixture entities → two subtotal rows + Grand Total last.
        assert sum(1 for e in entities if e and e.endswith("— subtotal")) == 2
        assert entities[-1] == "Grand Total"
        gt_row = ws.max_row
        assert _cell(ws, gt_row, "NACHOS Points Resolved") == 6  # 3 + 3
        sentence = _cell(ws, gt_row, "Reason")
        assert "moves AZ mean adjusted 3.00 → 0.00" in sentence
        assert "n=2" in sentence
        # Subtotal + Grand Total rows are bold.
        assert ws.cell(row=gt_row, column=1).font.bold

    def test_tracker_analyst_columns_round_trip(self, tmp_path, monkeypatch):
        payload = {
            "version": 1,
            "state": "AZ",
            "entries": {
                "AZ|Calendar|calendarCode": {
                    "values": {
                        "adoption_timeline": {"value": "2027-28",
                                              "lens": "source"},
                        "commitment_status": {"value": "Committed",
                                              "lens": "source"},
                    },
                    "history": [],
                }
            },
        }
        out = self._setup(
            tmp_path, monkeypatch,
            justification="tier_3_aggregation", adjusted=3.0,
            curation_payload=payload,
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Commitment Tracker"]
        row = next(
            r for r in range(2, ws.max_row + 1)
            if _cell(ws, r, "Data Element") == "calendarCode"
        )
        assert _cell(ws, row, "Adoption Timeline") == "2027-28"
        assert _cell(ws, row, "Commitment Status") == "Committed"

    def test_tracker_links_hit_details_rows(self, tmp_path, monkeypatch):
        out = self._setup(
            tmp_path, monkeypatch,
            justification="tier_3_aggregation", adjusted=3.0,
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        wb = openpyxl.load_workbook(path)
        ws = wb["Commitment Tracker"]
        details = wb["Details"]
        link_cell = ws.cell(row=2, column=_col(ws, "Row #"))
        assert link_cell.hyperlink is not None
        target_row = int(link_cell.hyperlink.location.rsplit("A", 1)[1])
        assert (
            _cell(details, target_row, "Data Element")
            == _cell(ws, 2, "Data Element")
        )

    def test_no_recs_gate_no_tracker(self, tmp_path, monkeypatch):
        from src.report import curation as curation_mod

        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        TestPhaseDScoringIntegration()._write_scores_sidecar(
            out, "AZ", "source", records, nachos_tier=3,
            nachos_rule="tier_3_aggregation", adjusted=3.0,
            justification="tier_3_aggregation",
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        monkeypatch.setattr(
            curation_mod, "_CURATION_DIR", tmp_path / "data" / "curation"
        )
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        assert "Commitment Tracker" not in openpyxl.load_workbook(
            path
        ).sheetnames


class TestWithHumanComparison:
    """Option C PR 3 — `report analyst --with-human` Cmp: columns."""

    def _human_file(self, tmp_path, rows):
        path = tmp_path / "human_scores.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Details"
        ws.append(["State", "Entity Name", "Data Element",
                   "NACHOS score", "Adjusted NACHOS Score"])
        for r in rows:
            ws.append(r)
        wb.save(path)
        return path

    def _setup(self, tmp_path, monkeypatch, *, adjusted=1.0, tier=1):
        from src.report import curation as curation_mod

        _write_az_fixture(tmp_path)
        out = tmp_path / "data" / "out"
        records = _az_elements().elements
        TestPhaseDScoringIntegration()._write_scores_sidecar(
            out, "AZ", "source", records, nachos_tier=tier,
            nachos_rule="tier_1_conditional", adjusted=adjusted,
            justification="tier_1_conditional",
        )
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", tmp_path / "data" / "spine")
        monkeypatch.setattr(
            curation_mod, "_CURATION_DIR", tmp_path / "data" / "curation"
        )
        # resolve_human_scores reads sidecars/spine via out_dir()/spine_dir
        # defaults — point it at the fixture dirs.
        from src.report import human_score_backfill as hsb
        monkeypatch.setattr(hsb, "out_dir", lambda: out)
        monkeypatch.setattr(
            hsb, "spine_dir", lambda: tmp_path / "data" / "spine"
        )
        return out

    def test_cmp_columns_render_when_human_file_given(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(tmp_path, monkeypatch, adjusted=1.0, tier=1)
        human = self._human_file(tmp_path, [
            ["Arizona", "Calendar", "calendarCode", 3, 3.0],
        ])
        [path] = analyst.run(
            state="AZ", out_dir=out, lens="source", with_human=human
        )
        # Comparison runs write a SEPARATE copy, never the deliverable.
        assert path.name == "az_analyst_with_human.xlsx"
        ws = openpyxl.load_workbook(path)["Details"]
        headers = [c.value for c in ws[1]]
        assert headers[-4:] == [
            "Cmp: Status", "Cmp: Base Δ", "Cmp: Adj Δ", "Cmp: Why"
        ]
        row = next(
            r for r in range(2, ws.max_row + 1)
            if _cell(ws, r, "Data Element") == "calendarCode"
        )
        assert _cell(ws, row, "Cmp: Status") == "tier_delta_ge2"
        assert _cell(ws, row, "Cmp: Base Δ") == 2
        assert _cell(ws, row, "Cmp: Adj Δ") == 2.0
        assert _cell(ws, row, "Cmp: Why") == "human 3 vs AI 1 (Δbase +2, Δadj +2.0)"
        # Rows the human file lacks stay blank (filterable, not noise).
        other = next(
            r for r in range(2, ws.max_row + 1)
            if _cell(ws, r, "Data Element") == "schoolId"
        )
        assert _cell(ws, other, "Cmp: Status") is None

    def test_no_flag_no_cmp_columns(self, tmp_path, monkeypatch):
        out = self._setup(tmp_path, monkeypatch)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        assert path.name == "az_analyst.xlsx"
        headers = [
            c.value for c in openpyxl.load_workbook(path)["Details"][1]
        ]
        assert not any(h and h.startswith("Cmp:") for h in headers)

    def test_with_human_never_overwrites_deliverable(
        self, tmp_path, monkeypatch
    ):
        """A comparison run must leave the pipeline-created workbook on
        disk byte-identical — the Cmp: copy is a side-by-side working
        artifact, not a deliverable mutation."""
        out = self._setup(tmp_path, monkeypatch, adjusted=1.0, tier=1)
        [deliverable] = analyst.run(state="AZ", out_dir=out, lens="source")
        assert deliverable.name == "az_analyst.xlsx"
        before = deliverable.read_bytes()

        human = self._human_file(tmp_path, [
            ["Arizona", "Calendar", "calendarCode", 3, 3.0],
        ])
        [cmp_path] = analyst.run(
            state="AZ", out_dir=out, lens="source", with_human=human
        )
        assert cmp_path.name == "az_analyst_with_human.xlsx"
        assert cmp_path != deliverable
        assert deliverable.read_bytes() == before

    def test_backfill_projection_unchanged_by_cmp(self):
        """`_MC_HEADERS` (the ai- projection) must never grow Cmp:
        columns — they are render-time only."""
        from src.report.human_score_backfill import _MC_HEADERS

        assert not any("Cmp:" in h for h in _MC_HEADERS)
        # 31 since issue #213 item 1 (ai-Recommendations excluded).
        assert len(_MC_HEADERS) == 31


class TestNoFormulaCells:
    """Regression for the Excel 'Removed Records: Formula' corruption:
    no workbook cell may carry openpyxl's formula data type UNLESS it
    is the one sanctioned formula surface. Every writer must route
    leading-`=` strings through the `_set_cell` guard (the combined
    Score Card's `== ST ==` state headers shipped as formulas once —
    Excel refused to open the workbook). Issue #259 PR 3 adds the one
    exception: the Review Queue's `Reviewed? (from Details)` progress
    echo, written post-render by `apply_reviewed_echo` with a
    known-valid formula shape."""

    @staticmethod
    def _assert_no_formula_cells(wb):
        def sanctioned(sheet, cell):
            if sheet.title != "Review Queue":
                return False
            header = sheet.cell(row=1, column=cell.column).value
            return (
                header == "Reviewed? (from Details)"
                and isinstance(cell.value, str)
                and cell.value.startswith("=IFERROR(INDEX(")
            )

        offenders = [
            (sh.title, c.coordinate, c.value)
            for sh in wb.worksheets
            for row in sh.iter_rows()
            for c in row
            if c.data_type == "f" and not sanctioned(sh, c)
        ]
        assert offenders == [], offenders

    def test_combined_workbook_has_no_formula_cells(
        self, tmp_path, monkeypatch
    ):
        from src.report import curation as curation_mod

        spine_dir = tmp_path / "data" / "spine"
        out = tmp_path / "data" / "out"
        out.mkdir(parents=True, exist_ok=True)
        spine_dir.mkdir(parents=True, exist_ok=True)
        integration = TestPhaseDScoringIntegration()
        for state in ("AZ", "WI", "MN", "TX", "IN"):
            records = [
                ElementRecord(
                    state=state, edfi_version="4.0", domain="SchoolCalendar",
                    entity="Calendar", element_name="calendarCode",
                    definition_text="x", source="core", documented=True,
                ),
            ]
            elements = StateElements(
                state=state, edfi_version="4.0",
                extracted_at=datetime.now(timezone.utc),
                element_count=1, elements=records,
            )
            (out / f"{state.lower()}_elements_source.json").write_text(
                elements.model_dump_json(indent=2), encoding="utf-8"
            )
            spine = _az_spine().model_copy(update={"state": state})
            (spine_dir / f"{state.lower()}_spine.json").write_text(
                spine.model_dump_json(indent=2), encoding="utf-8"
            )
            integration._write_scores_sidecar(out, state, "source", records)
        monkeypatch.setattr(analyst, "_OUT_DIR", out)
        monkeypatch.setattr(analyst, "_SPINE_DIR", spine_dir)
        monkeypatch.setattr(
            curation_mod, "_CURATION_DIR", tmp_path / "data" / "curation"
        )
        produced = analyst.run(out_dir=out, lens="source")
        for path in produced:
            self._assert_no_formula_cells(openpyxl.load_workbook(path))

    def test_per_state_fixture_workbooks_have_no_formula_cells(
        self, tmp_path
    ):
        from tests.test_workbook_fingerprint import build_fixture_workbook

        for lens in ("source", "spine"):
            wb = openpyxl.load_workbook(
                build_fixture_workbook(tmp_path / lens, lens)
            )
            self._assert_no_formula_cells(wb)


class TestFactCorrectionRenderSurfaces:
    """Issue #249 — the fact-corrections register on the Update Log,
    the Score Card corrections-by-fact-name table, and the audit
    workbook's human_corrected_facts column."""

    _KEY = "AZ|Calendar|calendarCode"

    def _payload(self, *, value=False, prior=True):
        return {
            "version": 3,
            "state": "AZ",
            "entries": {
                self._KEY: {
                    "values": {},
                    "history": [],
                    "facts": {
                        "has_conditional_logic": {
                            "value": value,
                            "lens": "source",
                            "author": "Chris Moffatt",
                            "corrected_at": "2026-07-13T12:00:00+00:00",
                            "rationale": "calendar, not conditionality",
                            "prior_value": prior,
                            "prior_provenance": "llm",
                            "plan_version_at_correction": "28",
                        },
                    },
                },
            },
        }

    def _setup(self, tmp_path, monkeypatch, payload=None):
        return TestReviewQueueFrontSheet()._setup(
            tmp_path, monkeypatch, curation_payload=payload
        )

    @staticmethod
    def _mark_applied(out, *, value=False):
        """Rewrite the scores sidecar as if aggregate re-ran under the
        correction: the fact carries human_corrected + the value."""
        sidecar = out / "az_scores_source.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        for s in payload["scores"]:
            if s["record_key"] == "AZ|Calendar|calendarCode":
                s.setdefault("fact_provenance", {})[
                    "has_conditional_logic"
                ] = {
                    "value": value,
                    "confidence": "high",
                    "downgraded": False,
                    "downgrade_reason": None,
                    "provenance": "human_corrected",
                }
        sidecar.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _register_rows(wb):
        ul = wb["Update Log"]
        labels = [
            ul.cell(row=r, column=1).value for r in range(1, ul.max_row + 1)
        ]
        title = next(
            (v for v in labels if isinstance(v, str)
             and v.startswith("Fact-corrections register")),
            None,
        )
        if title is None:
            return None, None
        header_row = labels.index(title) + 2
        return ul, header_row

    def test_register_renders_pending_then_applied(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(tmp_path, monkeypatch, self._payload())
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ul, header_row = self._register_rows(openpyxl.load_workbook(path))
        assert ul is not None
        assert ul.cell(row=header_row, column=1).value == "Entity"
        data = [
            ul.cell(row=header_row + 1, column=c).value for c in range(1, 10)
        ]
        # Sidecar predates the correction — honest pending status.
        assert data == [
            "Calendar", "calendarCode", "has_conditional_logic",
            "True → False", "source", "pending re-aggregate",
            "Chris Moffatt", "2026-07-13T12:00:00+00:00",
            "calendar, not conditionality",
        ]

        self._mark_applied(out, value=False)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ul, header_row = self._register_rows(openpyxl.load_workbook(path))
        assert (
            ul.cell(row=header_row + 1, column=6).value == "applied"
        )

    def test_no_corrections_no_register(self, tmp_path, monkeypatch):
        out = self._setup(tmp_path, monkeypatch)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ul, _ = self._register_rows(openpyxl.load_workbook(path))
        assert ul is None

    def test_score_card_table_clusters_by_fact(self, tmp_path, monkeypatch):
        out = self._setup(tmp_path, monkeypatch, self._payload())
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Score Card"]
        values = [
            ws.cell(row=r, column=1).value for r in range(1, ws.max_row + 1)
        ]
        title_row = values.index(
            "Fact corrections — human-corrected extraction inputs"
        ) + 1
        header_row = title_row + 2
        assert ws.cell(row=header_row, column=1).value == "Fact"
        assert ws.cell(row=header_row + 1, column=1).value == (
            "has_conditional_logic"
        )
        assert ws.cell(row=header_row + 1, column=2).value == "True → False"
        assert ws.cell(row=header_row + 1, column=3).value == 1

    def test_score_card_empty_state_when_scored_but_uncorrected(
        self, tmp_path, monkeypatch
    ):
        out = self._setup(tmp_path, monkeypatch)
        [path] = analyst.run(state="AZ", out_dir=out, lens="source")
        ws = openpyxl.load_workbook(path)["Score Card"]
        values = [
            ws.cell(row=r, column=1).value for r in range(1, ws.max_row + 1)
        ]
        assert (
            "Fact corrections — human-corrected extraction inputs" in values
        )
        assert any(
            isinstance(v, str) and v.startswith("No fact corrections")
            for v in values
        )

    def test_audit_workbook_shows_corrected_facts_column(
        self, tmp_path, monkeypatch
    ):
        from src.report.audit import run as run_audit

        out = self._setup(tmp_path, monkeypatch, self._payload())
        self._mark_applied(out, value=False)
        ws = openpyxl.load_workbook(
            run_audit(state="AZ", lens="source", out=out)
        )["Audit Trail"]
        col = next(
            c for c in range(1, ws.max_column + 1)
            if ws.cell(row=1, column=c).value == "human_corrected_facts"
        )
        cells = {
            ws.cell(row=r, column=col).value
            for r in range(2, ws.max_row + 1)
        }
        assert "has_conditional_logic:False" in cells
