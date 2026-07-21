"""Tests for issue #136 — human-score back-fill workbooks (per-state configs)."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path

import click
import openpyxl
from openpyxl import Workbook

from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    PropertyInfo,
    ReferenceInfo,
)
from src.models.element import ElementRecord, StateElements
from src.models.spine import SpineSourceURLs, StateSpine
from src.report import human_score_backfill as hsb
from src.score.review_loader import REVIEWER_SOURCES


# ---------------------------------------------------------------------------
# Fixtures: minimal AZ/WI/MN/TX state context (2 entities each).
# ---------------------------------------------------------------------------


def _spine(state: str) -> StateSpine:
    school_ref = ReferenceInfo(
        entity="School",
        key_properties={"schoolId": PropertyInfo(type="integer", is_identity=True)},
    )
    academic_week = EntityEntry(
        description="A week of instruction.",
        domains=["SchoolCalendar"],
        properties={"beginDate": PropertyInfo(type="date")},
    )
    ssa = EntityEntry(
        description="StudentSchoolAssociation",
        domains=["Enrollment"],
        properties={"entryDate": PropertyInfo(type="date")},
        references={"schoolReference": school_ref},
    )
    school = EntityEntry(
        description="School",
        domains=["EducationOrganization"],
        properties={"schoolId": PropertyInfo(type="integer", is_identity=True)},
    )
    catalog = EdFiCatalog(
        version="4.0.0",
        entity_count=3,
        extension_count=0,
        entities={
            "AcademicWeek": academic_week,
            "StudentSchoolAssociation": ssa,
            "School": school,
        },
        extensions={},
    )
    return StateSpine(
        state=state,
        edfi_version="4.0.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(resources=f"https://example/{state}/swagger.json"),
        catalog=catalog,
    )


def _elements(state: str) -> StateElements:
    records = [
        ElementRecord(
            state=state,
            edfi_version="4.0",
            domain="SchoolCalendar",
            entity="AcademicWeek",
            element_name="beginDate",
            data_type="date",
            definition_text="The week's start date.",
            source="core",
            documented=True,
        ),
        ElementRecord(
            state=state,
            edfi_version="4.0",
            domain="Enrollment",
            entity="StudentSchoolAssociation",
            element_name="entryDate",
            data_type="date",
            definition_text="Date the student began enrollment.",
            source="core",
            documented=True,
        ),
    ]
    return StateElements(
        state=state,
        edfi_version="4.0",
        extracted_at=datetime.now(timezone.utc),
        element_count=len(records),
        elements=records,
    )


_NACHOS_INPUTS_TIER0 = {
    "has_aggregation": False,
    "has_concatenation": False,
    "has_cross_entity_logic": False,
    "cross_entity_targets": 0,
    "has_conditional_logic": False,
    "descriptor_values_enumerated": False,
    "is_natural_key": False,
    "element_is_bare_fk_reference": False,
    "element_only_parent_entity_gate": False,
    "has_cross_entity_logic__reconciled": False,
    "has_conditional_logic__reconciled": False,
}


def _nachos_inputs_tier1():
    inputs = dict(_NACHOS_INPUTS_TIER0)
    inputs["has_conditional_logic"] = True
    inputs["has_conditional_logic__reconciled"] = True
    return inputs


def _scores(state: str) -> list[dict]:
    return [
        {
            "record_key": f"{state}|AcademicWeek|beginDate",
            "entity": "AcademicWeek",
            "element_name": "beginDate",
            "complexity_score": 0,
            "dimensions": {
                "nachos_score": {
                    "value": 0,
                    "rule_matched": "tier_0_none",
                    "inputs_used": dict(_NACHOS_INPUTS_TIER0),
                    "confidence": "high",
                },
            },
            "adjusted_nachos_score": 0.0,
            "in_scope": True,
            "nachos_justification": "tier_0_none",
            "review": {"needs_review": False, "reasons": [], "route": None},
        },
        {
            "record_key": f"{state}|StudentSchoolAssociation|entryDate",
            "entity": "StudentSchoolAssociation",
            "element_name": "entryDate",
            "complexity_score": 1,
            "dimensions": {
                "nachos_score": {
                    "value": 1,
                    "rule_matched": "tier_1_conditional",
                    "inputs_used": _nachos_inputs_tier1(),
                    "confidence": "high",
                },
            },
            "adjusted_nachos_score": 1.0,
            "in_scope": True,
            "nachos_justification": "tier_1_conditional",
            "review": {"needs_review": False, "reasons": [], "route": None},
        },
    ]


def _write_state_artifacts(state: str, sidecar_dir: Path, spine_dir: Path) -> None:
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    spine_dir.mkdir(parents=True, exist_ok=True)
    s = state.lower()
    (sidecar_dir / f"{s}_elements_source.json").write_text(
        _elements(state).model_dump_json(indent=2), encoding="utf-8"
    )
    (sidecar_dir / f"{s}_scores_source.json").write_text(
        json.dumps({"scores": _scores(state)}), encoding="utf-8"
    )
    (spine_dir / f"{s}_spine.json").write_text(
        _spine(state).model_dump_json(indent=2), encoding="utf-8"
    )


def _gap_elements(state: str) -> dict:
    """Minimal ``{state}_elements_gap.json`` payload (issue #166 gap-wiring).

    One swagger-sourced gap row on an entity the source-lens fixtures don't
    enumerate (``Calendar``), so reviewer rows that miss the source sidecar
    fall through to the gap path. ``leaf_name`` lets ``_gap_lookup_resolve``
    match the bare reviewer leaf (``daysInSession``) against the
    extension-prefixed ``element_name`` (``MNDaysInSession``).
    """
    return {
        "state": state,
        "gap_count": 1,
        "gaps": [
            {
                "state": state,
                "entity": "Calendar",
                "element_name": "MNDaysInSession",
                "discovery": "spine_within_documented_entity",
                "documented_in_source": False,
                "spine_data_type": "Number",
                "spine_extension_name": "calendarExtensions",
                "rationale": "Entity enumerated in source doc but element "
                "missing; contributed by extension calendarExtensions",
                "sub_collection": "MN",
                "leaf_name": "daysInSession",
            }
        ],
    }


def _gap_scores(state: str, *, scored: bool = True) -> dict:
    """Deterministic gap sidecar payload matching ``_gap_elements``.

    When ``scored`` is False, returns an empty scores list to exercise the
    Layer-2 audit-only path (gap artifact recognizes the pair but no
    deterministic score exists → ai- cells stay blank).
    """
    if not scored:
        return {"scores": []}
    return {
        "scores": [
            {
                "record_key": f"{state}|Calendar|MNDaysInSession",
                "entity": "Calendar",
                "element_name": "MNDaysInSession",
                "complexity_score": 0,
                "dimensions": {
                    "nachos_score": {
                        "value": 0,
                        "rule_matched": "tier_0_none",
                        "inputs_used": dict(_NACHOS_INPUTS_TIER0),
                        "confidence": "high",
                    },
                },
                "adjusted_nachos_score": 0.5,
                "in_scope": True,
                "nachos_justification": "tier_0_none (+0.5 necessary_ext)",
                "documentation_source": "swagger",
                "review": {"needs_review": False, "reasons": [], "route": None},
            }
        ]
    }


def _write_gap_artifacts(
    state: str, sidecar_dir: Path, *, scored: bool = True
) -> None:
    s = state.lower()
    (sidecar_dir / f"{s}_elements_gap.json").write_text(
        json.dumps(_gap_elements(state)), encoding="utf-8"
    )
    (sidecar_dir / f"{s}_scores_gap.json").write_text(
        json.dumps(_gap_scores(state, scored=scored)), encoding="utf-8"
    )


def _write_origin_xlsx(
    path: Path, headers: list, rows: list[list], sheet_name: str = "Details"
) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(path)


def _setup_state_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    sidecar_dir = tmp_path / "out"
    spine_dir = tmp_path / "spine"
    for state in ("AZ", "WI", "MN", "TX", "IN"):
        _write_state_artifacts(state, sidecar_dir, spine_dir)
    return sidecar_dir, spine_dir


# ---------------------------------------------------------------------------
# CLI / shape sanity.
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_run_is_plain_function(self):
        assert not isinstance(hsb.run, click.Command)
        assert inspect.isfunction(hsb.run)

    def test_human_overlay_dispatches_config(self, monkeypatch, tmp_path):
        """`report human-overlay --config X` dispatches the named
        per-state config through the shared engine."""
        from click.testing import CliRunner

        captured: dict = {}

        def _fake_run(config_name, *, output_path=None):
            captured["config_name"] = config_name
            captured["output_path"] = output_path
            return tmp_path / "out.xlsx"

        monkeypatch.setattr(hsb, "run", _fake_run)
        from src.cli import cli

        result = CliRunner().invoke(
            cli, ["report", "human-overlay", "--config", "arizona"],
        )
        assert result.exit_code == 0, result.output
        assert captured["config_name"] == "arizona"
        assert "arizona: workbook written to" in result.output

    def test_old_backfill_commands_are_gone(self):
        from src.cli import report

        for old in ("training-20pct", "training-file", "nachos-arizona"):
            assert old not in report.commands
        assert "human-overlay" in report.commands

    def test_human_overlay_choice_list_matches_configs(self):
        """The Click Choice list is hardcoded (cli.py lazy-import
        convention) — this pin keeps it in lockstep with CONFIGS."""
        from src.cli import report

        cmd = report.commands["human-overlay"]
        config_param = next(p for p in cmd.params if p.name == "config_name")
        assert set(config_param.type.choices) == set(hsb.CONFIGS)

    def test_analyst_human_config_choice_list_matches_configs(self):
        """`report analyst --human-config` carries the same hardcoded
        Choice list — pin it to CONFIGS too (it was unpinned before the
        2026-07-07 basis change)."""
        from src.cli import report

        cmd = report.commands["analyst"]
        param = next(p for p in cmd.params if p.name == "human_config")
        assert set(param.type.choices) == set(hsb.CONFIGS)


class TestHeaders:
    def test_ai_prefix_on_all_32_poc3_headers(self):
        from src.report.analyst import _DETAILS_HEADERS

        # 32 post Option B (issue #186 seq 2): the role-filtered Details
        # projection — the 8-column analyst-input band is excluded, and
        # the render-time `Row #` col is deliberately not part of the
        # tuple, so no `ai-Row #` appears. Headers carrying the `AI: `
        # display prefix keep their historical bare `ai-` names
        # (`AI: Needs Review` → `ai-Needs Review`).
        assert len(hsb._MC_HEADERS) == len(_DETAILS_HEADERS) == 31
        for ai_h, src_h in zip(hsb._MC_HEADERS, _DETAILS_HEADERS):
            assert ai_h == f"ai-{src_h.removeprefix('AI: ')}"

    def test_configs_registered(self):
        """CONFIGS is derived from REVIEWER_SOURCES (2026-07-07 basis) —
        one per-state config, fixed-state, header-map-driven."""
        assert set(hsb.CONFIGS) == {
            "arizona", "wisconsin", "minnesota", "texas", "indiana",
        }
        by_key = {s.key: s for s in REVIEWER_SOURCES}
        for key, cfg in hsb.CONFIGS.items():
            src = by_key[key]
            assert cfg.fixed_state == src.state
            assert cfg.input_path.name == src.filename
            assert cfg.input_path.parent.name == "human-scored-files"
            assert cfg.sheet_name == src.sheet_name
            assert cfg.output_path.name == f"{key}_with_mc_scores.xlsx"
            # header_map drives column resolution; every file declares
            # both score roles so the ai-summary gate activates
            # post-resolution.
            assert cfg.header_map is not None
            assert cfg.header_map["entity"]
            assert cfg.header_map["element"]
            assert cfg.header_map["nachos"]
            assert cfg.header_map["adjusted"]


# ---------------------------------------------------------------------------
# Origin loader.
# ---------------------------------------------------------------------------


class TestLoadOrigin:
    def test_reads_rows_skipping_blanks(self, tmp_path):
        path = tmp_path / "in.xlsx"
        _write_origin_xlsx(path, [
            "State", "Entity Name", "Data Element", "Data Type",
            "Required", "Random Sample",
        ], [
            ["Texas", "AcademicWeek", "beginDate", "date", "Yes", "Yes"],
            [None, None, None, None, None, None],
            ["Indiana", "Foo", "bar", "string", None, "Yes"],
        ])
        cfg = hsb.BackfillConfig(
            name="t",
            input_path=path,
            sheet_name="Details",
            output_path=tmp_path / "out.xlsx",
            state_col=0,
            entity_col=1,
            element_col=2,
        )
        headers, rows = hsb._load_origin(cfg)
        assert headers[0] == "State"
        assert [r.state_raw for r in rows] == ["Texas", "Indiana"]
        assert [r.row_idx for r in rows] == [2, 4]
        assert rows[0].entity == "AcademicWeek"
        assert rows[0].cells == (
            "Texas", "AcademicWeek", "beginDate", "date", "Yes", "Yes",
        )

    def test_state_entity_element_columns_obey_config_offsets(self, tmp_path):
        path = tmp_path / "in.xlsx"
        _write_origin_xlsx(path, [
            "State", "Domain", "Entity Name", "Data Element", "Data Type", "Required",
        ], [
            ["Wisconsin", "Enrollment", "AcademicWeek", "beginDate", "date", "Yes"],
        ])
        cfg = hsb.BackfillConfig(
            name="t", input_path=path, sheet_name="Details",
            output_path=tmp_path / "out.xlsx",
            state_col=0, entity_col=2, element_col=3,
        )
        _, rows = hsb._load_origin(cfg)
        assert rows[0].state_raw == "Wisconsin"
        assert rows[0].entity == "AcademicWeek"
        assert rows[0].element == "beginDate"

    def test_human_score_columns_parsed_when_configured(self, tmp_path):
        path = tmp_path / "in.xlsx"
        _write_origin_xlsx(path, [
            "State", "Entity", "Elem", "NACHOS", "Adj",
        ], [
            ["Texas", "X", "y", 2, 2.5],
            ["Texas", "X", "z", None, None],
        ])
        cfg = hsb.BackfillConfig(
            name="t", input_path=path, sheet_name="Details",
            output_path=tmp_path / "out.xlsx",
            state_col=0, entity_col=1, element_col=2,
            human_nachos_col=3, human_adj_col=4,
        )
        _, rows = hsb._load_origin(cfg)
        assert rows[0].human_nachos == 2
        assert rows[0].human_adj == 2.5
        assert rows[1].human_nachos is None
        assert rows[1].human_adj is None

    def test_unknown_sheet_raises(self, tmp_path):
        path = tmp_path / "in.xlsx"
        _write_origin_xlsx(path, ["A"], [["x"]], sheet_name="Sheet1")
        cfg = hsb.BackfillConfig(
            name="t", input_path=path, sheet_name="DoesNotExist",
            output_path=tmp_path / "out.xlsx",
            state_col=0, entity_col=0, element_col=0,
        )
        try:
            hsb._load_origin(cfg)
        except KeyError as e:
            assert "DoesNotExist" in str(e)
        else:  # pragma: no cover
            raise AssertionError("expected KeyError")


# ---------------------------------------------------------------------------
# header_map-driven column resolution (per-state configs, 2026-07-07).
# ---------------------------------------------------------------------------

# The AZ per-state workbook's raw headers — embedded newline + double
# space; normalization must land the roles at cols 0/2/3/8/9.
_AZ_SHAPED_HEADERS = [
    "State", "Domain", "Entity Name", "Data Element", "Data Type",
    "Is\nRequired", "Is \nExtension", "Business Logic (Redacted)",
    "NACHOS score", "Adjusted NACHOS  Score",
    "Justification for Adjusted NACHOS  Score",
    "Unnecessary Extension ?", "Cross Entity Calculation ?",
]

# The IN per-state workbook's naming — ResourceName entity column,
# NACHOS mid-sheet, `Adjusted Score`.
_IN_SHAPED_HEADERS = [
    "State", "Legislation", "Legislative Logic", "ResourceName",
    "Data Element", "Data Type", "Required",
    "Business Logic (Requirements)", "Is an extension",
    "Unnecessary Extension", "Is this a calculation?",
    "Reason for extension necessity logic", "NACHOS score",
    "Business Logic (Formula)", "Reason for Complexity",
    "In-Scope of Ed-Fi Data Standard focus",
    "Reason for In-Scope or Out-of/Scope",
    "Cross entity reference. If yes, then adjust score by +0.5",
    "Adjusted Score",
]


def _reviewer_source(key: str):
    return next(s for s in REVIEWER_SOURCES if s.key == key)


class TestHeaderMapResolution:
    def test_resolve_config_materializes_columns(self, tmp_path):
        path = tmp_path / "az.xlsx"
        _write_origin_xlsx(path, _AZ_SHAPED_HEADERS, [
            ["Arizona", "Dom", "CourseTranscript", "finalLetterGrade",
             "String", "Yes", "No", "—", 2, 2.5, "why", None, None],
        ])
        cfg = dc_replace(
            hsb.config_from_reviewer_source(_reviewer_source("arizona")),
            input_path=path,
        )
        resolved = hsb._resolve_config(cfg)
        assert resolved.state_col == 0
        assert resolved.entity_col == 2
        assert resolved.element_col == 3
        assert resolved.human_nachos_col == 8
        assert resolved.human_adj_col == 9

    def test_load_origin_reads_via_header_map(self, tmp_path):
        path = tmp_path / "az.xlsx"
        _write_origin_xlsx(path, _AZ_SHAPED_HEADERS, [
            ["Arizona", "Dom", "CourseTranscript", "finalLetterGrade",
             "String", "Yes", "No", "—", 2, 2.5, "why", None, None],
        ])
        cfg = dc_replace(
            hsb.config_from_reviewer_source(_reviewer_source("arizona")),
            input_path=path,
        )
        _, rows = hsb._load_origin(cfg)
        assert rows[0].entity == "CourseTranscript"
        assert rows[0].element == "finalLetterGrade"
        assert rows[0].human_nachos == 2
        assert rows[0].human_adj == 2.5

    def test_resolve_config_fails_loudly_on_renamed_header(self, tmp_path):
        headers = list(_AZ_SHAPED_HEADERS)
        headers[3] = "Element Name"  # was `Data Element`
        path = tmp_path / "az.xlsx"
        _write_origin_xlsx(path, headers, [])
        cfg = dc_replace(
            hsb.config_from_reviewer_source(_reviewer_source("arizona")),
            input_path=path,
        )
        try:
            hsb._resolve_config(cfg)
        except ValueError as e:
            assert "'Data Element'" in str(e)
            assert "az.xlsx" in str(e)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")

    def test_resolve_config_noop_without_header_map(self, tmp_path):
        cfg = hsb.BackfillConfig(
            name="t", input_path=tmp_path / "missing.xlsx",
            sheet_name="Details", output_path=tmp_path / "out.xlsx",
            state_col=0, entity_col=1, element_col=2,
        )
        assert hsb._resolve_config(cfg) is cfg

    def test_detect_config_autodetects_in_shaped_headers(self, tmp_path):
        """`--with-human` autodetection covers the IN workbook naming
        (ResourceName / Adjusted Score) without a --human-config."""
        path = tmp_path / "in.xlsx"
        _write_origin_xlsx(path, _IN_SHAPED_HEADERS, [])
        cfg = hsb._detect_config(path, None)
        assert cfg.entity_col == 3
        assert cfg.element_col == 4
        assert cfg.human_nachos_col == 12
        assert cfg.human_adj_col == 18


# ---------------------------------------------------------------------------
# 20pct config (no human scores) — the simple case.
# ---------------------------------------------------------------------------


class TestBuildTraining20pct:
    def _setup(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        _write_origin_xlsx(input_path, [
            "State", "Entity Name", "Data Element", "Data Type",
            "Required", "Random Sample",
        ], [
            ["Wisconsin", "AcademicWeek", "beginDate", "date", "Yes", "Yes"],
            ["Texas", "StudentSchoolAssociation", "entryDate", "date", "No", "Yes"],
            ["Minnesota", "AcademicWeek", "missingElement", "string", None, "Yes"],
            ["Indiana", "Calendar", "calendarCode", "string", None, "Yes"],
            ["Nebraska", "School", "schoolId", "integer", None, "Yes"],
            [None, "OrphanEntity", "orphanElement", "string", None, "Yes"],
        ])
        output_path = tmp_path / "out.xlsx"
        return input_path, output_path, sidecar_dir, spine_dir

    def test_writes_workbook_and_keeps_origin_cells(self, tmp_path):
        input_path, output_path, sidecar_dir, spine_dir = self._setup(tmp_path)
        cfg = hsb.BackfillConfig(
            name="synthetic-min", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=1, element_col=2,
        )
        produced = hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        assert produced == output_path
        wb = openpyxl.load_workbook(output_path)
        # No ai-summary because human_nachos_col is unset.
        # Single ai-summary tab (Readme content folded in) for all configs.
        assert wb.sheetnames == ["ai-summary", "Details"]
        details = wb["Details"]
        assert details.max_column == 37  # 6 origin + 31 ai- (#213: no ai-Recommendations)
        assert details.max_row == 7
        headers = [details.cell(row=1, column=c).value for c in range(1, 38)]
        assert headers[:6] == [
            "State", "Entity Name", "Data Element", "Data Type",
            "Required", "Random Sample",
        ]
        assert headers[6] == "ai-State"
        # Last ai- column is Review Route since issue #213 item 1
        # (ai-Recommendations excluded — overlay never joins recs).
        assert headers[-1] == "ai-Review Route"
        assert "ai-Recommendations" not in headers

    def test_in_scope_matched_rows_carry_ai_cells(self, tmp_path):
        input_path, output_path, sidecar_dir, spine_dir = self._setup(tmp_path)
        cfg = hsb.BackfillConfig(
            name="synthetic-min", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=1, element_col=2,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        details = wb["Details"]
        headers = [details.cell(row=1, column=c).value for c in range(1, 38)]
        nachos_idx = headers.index("ai-Base NACHOS Score")
        # Single-spaced post Sequence-1 hygiene (the ai- block derives
        # from `_DETAILS_HEADERS`; origin fixtures keep the double space).
        adj_idx = headers.index("ai-Adjusted NACHOS Score")
        wi = [details.cell(row=2, column=c).value for c in range(1, 39)]
        assert wi[0] == "Wisconsin"
        assert wi[headers.index("ai-State")] == "WI"
        assert wi[nachos_idx] == 0
        assert wi[adj_idx] == 0.0
        tx = [details.cell(row=3, column=c).value for c in range(1, 39)]
        assert tx[nachos_idx] == 1
        assert tx[adj_idx] == 1.0

    def test_unmatched_in_scope_row_has_blank_ai_cells(self, tmp_path):
        input_path, output_path, sidecar_dir, spine_dir = self._setup(tmp_path)
        cfg = hsb.BackfillConfig(
            name="synthetic-min", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=1, element_col=2,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        details = wb["Details"]
        # Row 4 = Minnesota, missingElement → blank ai- cells.
        cells = [details.cell(row=4, column=c).value for c in range(1, 39)]
        assert cells[0] == "Minnesota"
        for v in cells[6:]:
            assert v is None

    def test_out_of_scope_rows_have_blank_ai_cells(self, tmp_path):
        input_path, output_path, sidecar_dir, spine_dir = self._setup(tmp_path)
        cfg = hsb.BackfillConfig(
            name="synthetic-min", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=1, element_col=2,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        details = wb["Details"]
        for r in (5, 6, 7):
            cells = [details.cell(row=r, column=c).value for c in range(1, 39)]
            for v in cells[6:]:
                assert v is None, f"row {r}: ai- cell unexpectedly populated: {v!r}"


# ---------------------------------------------------------------------------
# training-file config: state col 0, entity col 2, element col 3 + human scores.
# ---------------------------------------------------------------------------


class TestBuildTrainingFile:
    def _setup(self, tmp_path: Path):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        origin_headers = [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Required", "Business Logic (Redacted)", "Complex Business Logic",
            "NACHOS score", "Adjusted NACHOS Score",
            "Justification for Adjusted NACHOS  Score",
        ]
        _write_origin_xlsx(input_path, origin_headers, [
            # WI: human tier 0, AI tier 0 → match_exact.
            ["Wisconsin", "Enrollment", "AcademicWeek", "beginDate", "date",
             "Yes", "—", "No", 0, 0, "tier_0"],
            # TX: human tier 2 / adj 2.5; AI tier 1 / adj 1.0 → tier_delta_1.
            ["Texas", "Enrollment", "StudentSchoolAssociation", "entryDate", "date",
             "No", "—", "Yes", 2, 2.5, "human_complex"],
            # NE: out of scope (never AI-scored), human tier 1 →
            # out_of_scope (separated from in-scope `human_only` so the
            # classification block stops conflating "AI failed to resolve"
            # with "AI never tried because the state isn't in scope").
            # (Indiana is now in-scope — see test_indiana_row_now_in_scope.)
            ["Nebraska", "X", "Foo", "bar", "string",
             "No", "—", "No", 1, 1.0, "out-of-scope"],
        ])
        output_path = tmp_path / "out.xlsx"
        return input_path, output_path, sidecar_dir, spine_dir

    def test_origin_cols_preserved_and_ai_block_appended(self, tmp_path):
        input_path, output_path, sidecar_dir, spine_dir = self._setup(tmp_path)
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        # ai-summary present because human cols are configured.
        assert wb.sheetnames == ["ai-summary", "Details"]
        details = wb["Details"]
        # 11 origin + 1 Match Status + 31 ai- = 43 cols (issue #213
        # item 1: ai-Recommendations excluded from the projection).
        assert details.max_column == 43
        headers = [details.cell(row=1, column=c).value for c in range(1, 44)]
        assert headers[11] == "Match Status"
        assert headers[12] == "ai-State"

    def test_ai_summary_renders_excluded_section_when_configured(self, tmp_path):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        origin_headers = [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Required", "Business Logic (Redacted)", "Complex Business Logic",
            "NACHOS score", "Adjusted NACHOS Score",
        ]
        _write_origin_xlsx(input_path, origin_headers, [
            # 2 WI rows: 1 match_exact + 1 unscored on human side (ai_only).
            ["Wisconsin", "Enr", "AcademicWeek", "beginDate", "date",
             "Yes", "—", "No", 0, 0],
            ["Wisconsin", "Enr", "StudentSchoolAssociation", "entryDate", "date",
             "Yes", "—", "No", None, None],
            # 1 MN row, human scored but AI can't resolve → human_only.
            ["Minnesota", "X", "AcademicWeek", "missingElement", "string",
             "No", "—", "No", 2, 2.0],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
            summary_exclude_states=("MN",),
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        s = wb["ai-summary"]
        # Find both section labels.
        labels = {
            s.cell(row=r, column=1).value: r
            for r in range(1, s.max_row + 1)
            if isinstance(s.cell(row=r, column=1).value, str)
        }
        assert "All states" in labels
        assert "Excluding MN" in labels
        # Each section has its own n.
        all_n = s.cell(row=labels["All states"], column=2).value
        excl_n = s.cell(row=labels["Excluding MN"], column=2).value
        assert all_n == "n = 3"
        assert excl_n == "n = 2"

    def test_per_state_breakdown_and_confusion_carry_percentages(self, tmp_path):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        origin_headers = [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Required", "Business Logic (Redacted)", "Complex Business Logic",
            "NACHOS score", "Adjusted NACHOS Score",
        ]
        _write_origin_xlsx(input_path, origin_headers, [
            # 2 WI rows: both match_exact (tier 0).
            ["Wisconsin", "Enr", "AcademicWeek", "beginDate", "date",
             "Yes", "—", "No", 0, 0],
            ["Wisconsin", "Enr", "StudentSchoolAssociation", "entryDate", "date",
             "Yes", "—", "No", 1, 1.0],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        s = wb["ai-summary"]
        # Locate the per-state breakdown header row.
        breakdown_label_row = None
        for r in range(1, s.max_row + 1):
            v = s.cell(row=r, column=1).value
            if isinstance(v, str) and v.startswith("Per-state breakdown"):
                breakdown_label_row = r
                break
        assert breakdown_label_row is not None
        # Header row immediately follows the label.
        header_row = breakdown_label_row + 1
        # First data row holds WI (only state present).
        wi_row = header_row + 1
        wi_cells = [s.cell(row=wi_row, column=c).value for c in range(1, 10)]
        assert wi_cells[0] == "WI"
        # Both WI rows are match_exact → "2 (100.0%)" in that bucket.
        # _CLASSIFICATION_ORDER[0] == "match_exact" — column 2 of breakdown.
        assert wi_cells[1] == "2 (100.0%)"
        # The Total cell remains a raw int.
        total_col = 2 + len(hsb._CLASSIFICATION_ORDER)
        assert s.cell(row=wi_row, column=total_col).value == 2

        # Confusion matrix: human row tier 0, AI col 0 should read "1 (100.0%)".
        confusion_label_row = None
        for r in range(1, s.max_row + 1):
            v = s.cell(row=r, column=1).value
            if isinstance(v, str) and v.startswith("Tier confusion"):
                confusion_label_row = r
                break
        assert confusion_label_row is not None
        # +2 to skip the label and the column-header row to the first data row (human=0).
        zero_row = confusion_label_row + 2
        # human=0 has 1 row in this fixture; ai=0 col is col 2.
        assert s.cell(row=zero_row, column=2).value == "1 (100.0%)"
        assert s.cell(row=zero_row, column=2 + 5).value == 1  # Total col

    def test_ai_summary_classifies_rows(self, tmp_path):
        input_path, output_path, sidecar_dir, spine_dir = self._setup(tmp_path)
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        s = wb["ai-summary"]
        # Walk the classification block: find row whose col 1 == bucket name.
        cell_map: dict[str, int] = {}
        for r in range(1, s.max_row + 1):
            v = s.cell(row=r, column=1).value
            if isinstance(v, str):
                cell_map[v] = r
        # WI tier 0/0 → match_exact; TX tier 2 vs 1 → tier_delta_1;
        # IN row is out-of-scope (state not in WI/MN/TX/AZ) → out_of_scope,
        # NOT human_only — the latter is now reserved for in-scope rows
        # where AI couldn't resolve a sidecar.
        assert s.cell(row=cell_map["match_exact"], column=2).value == 1
        assert s.cell(row=cell_map["tier_delta_1"], column=2).value == 1
        assert s.cell(row=cell_map["out_of_scope"], column=2).value == 1
        assert s.cell(row=cell_map["human_only"], column=2).value == 0

    def test_out_of_scope_split_from_in_scope_unresolved(self, tmp_path):
        """In-scope unresolved → human_only; out-of-scope → out_of_scope.

        Headline match-rate denominators only count in-scope rows
        (Nebraska excluded — never AI-scored), so the classification block
        must use the same definition or readers see two contradicting
        "% AI couldn't resolve" numbers across the same sheet. This pins
        the split.
        """
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        origin_headers = [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Required", "Business Logic (Redacted)", "Complex Business Logic",
            "NACHOS score", "Adjusted NACHOS Score",
        ]
        _write_origin_xlsx(input_path, origin_headers, [
            # In-scope WI row that AI cannot resolve → human_only.
            ["Wisconsin", "X", "AcademicWeek", "missingElement", "string",
             "No", "—", "No", 1, 1.0],
            # Out-of-scope NE row, human scored → out_of_scope.
            ["Nebraska", "X", "Foo", "bar", "string",
             "No", "—", "No", 2, 2.0],
            # Out-of-scope NE row, human BLANK → still out_of_scope (state
            # gates first; the human-blank/ai-blank `neither` bucket is
            # reserved for in-scope rows).
            ["Nebraska", "X", "Baz", "qux", "string",
             "No", "—", "No", None, None],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        s = wb["ai-summary"]
        cell_map = {
            s.cell(row=r, column=1).value: r
            for r in range(1, s.max_row + 1)
            if isinstance(s.cell(row=r, column=1).value, str)
        }
        assert s.cell(row=cell_map["human_only"], column=2).value == 1
        assert s.cell(row=cell_map["out_of_scope"], column=2).value == 2
        assert s.cell(row=cell_map["neither"], column=2).value == 0
        # Per-state breakdown: out-of-scope rows render under the
        # "(out of scope)" row label (matches the metadata's "Out of
        # scope (state not WI/MN/TX/AZ/IN)" wording above).
        breakdown_label_row = next(
            r for r in range(1, s.max_row + 1)
            if isinstance(s.cell(row=r, column=1).value, str)
            and s.cell(row=r, column=1).value.startswith("Per-state breakdown")
        )
        per_state_labels = {
            s.cell(row=r, column=1).value
            for r in range(breakdown_label_row + 2, s.max_row + 1)
        }
        assert "(out of scope)" in per_state_labels
        assert "(unmapped)" not in per_state_labels
        assert "WI" in per_state_labels

    def test_confusion_matrix_excludes_out_of_scope_rows(self, tmp_path):
        """Out-of-scope rows are bucketed separately and must not appear
        in the (h=*, ai=blank) cells of the confusion matrix; otherwise
        readers double-count them against the dedicated `out_of_scope`
        bucket in the headline classification.
        """
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        origin_headers = [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Required", "Business Logic (Redacted)", "Complex Business Logic",
            "NACHOS score", "Adjusted NACHOS Score",
        ]
        _write_origin_xlsx(input_path, origin_headers, [
            # In-scope WI tier 0 → contributes 1 to confusion (h=0,ai=0).
            ["Wisconsin", "Enr", "AcademicWeek", "beginDate", "date",
             "Yes", "—", "No", 0, 0],
            # Out-of-scope NE tier 0 → MUST NOT contribute to confusion.
            ["Nebraska", "X", "Foo", "bar", "string",
             "No", "—", "No", 0, 0],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        s = wb["ai-summary"]
        confusion_label_row = next(
            r for r in range(1, s.max_row + 1)
            if isinstance(s.cell(row=r, column=1).value, str)
            and s.cell(row=r, column=1).value.startswith("Tier confusion")
        )
        assert "in-scope rows only" in s.cell(row=confusion_label_row, column=1).value
        # human=0 is the first data row; total col = 2 + len(tier_axis=5) = 7.
        zero_row = confusion_label_row + 2
        # Only 1 row should reach confusion (the WI in-scope one).
        assert s.cell(row=zero_row, column=7).value == 1
        # ai=0 cell should be "1 (100.0%)" — Nebraska's tier-0 score does
        # NOT inflate the count.
        assert s.cell(row=zero_row, column=2).value == "1 (100.0%)"

    def test_indiana_row_now_in_scope(self, tmp_path):
        """Indiana joins its source-lens sidecar (June 2026 follow-up).

        IN was previously bucketed ``out_of_scope``; now that
        ``_STATE_NORMALIZE`` maps "Indiana" → "IN" (its keymap + sidecars
        are current), an IN reviewer row that resolves to the sidecar lands
        in a scoring bucket, not ``out_of_scope``. Nebraska remains the
        out-of-scope exemplar (never AI-scored).
        """
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        origin_headers = [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Required", "Business Logic (Redacted)", "Complex Business Logic",
            "NACHOS score", "Adjusted NACHOS Score",
        ]
        _write_origin_xlsx(input_path, origin_headers, [
            # IN row that resolves to the IN source-lens sidecar (tier 0/0).
            ["Indiana", "SchoolCalendar", "AcademicWeek", "beginDate", "date",
             "Yes", "—", "No", 0, 0],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        details = wb["Details"]
        headers = [
            details.cell(row=1, column=c).value
            for c in range(1, details.max_column + 1)
        ]
        # ai cells populated → IN resolved (not out_of_scope / blank).
        assert details.cell(row=2, column=headers.index("ai-State") + 1).value == "IN"
        assert details.cell(row=2, column=headers.index("ai-Base NACHOS Score") + 1).value == 0
        ms_col = headers.index("Match Status") + 1
        assert details.cell(row=2, column=ms_col).value == "match_exact"


# ---------------------------------------------------------------------------
# nachos-arizona config: fixed_state="AZ".
# ---------------------------------------------------------------------------


class TestBuildNachosArizona:
    def test_fixed_state_az_resolves_against_az_sidecar(self, tmp_path):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        origin_headers = [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Is Required", "Is Extension", "Business Logic (Redacted)",
            "NACHOS score", "Adjusted NACHOS Score",
            "Justification for Adjusted NACHOS  Score",
        ]
        _write_origin_xlsx(input_path, origin_headers, [
            ["Arizona", "Enrollment", "StudentSchoolAssociation", "entryDate",
             "date", "Yes", "No", "—", 1, 1.0, "agree"],
            ["Arizona", "SchoolCalendar", "AcademicWeek", "beginDate",
             "date", "Yes", "No", "—", 3, 3.5, "human_complex"],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-az", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            fixed_state="AZ",
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        assert wb.sheetnames == ["ai-summary", "Details"]
        details = wb["Details"]
        headers = [details.cell(row=1, column=c).value for c in range(1, details.max_column + 1)]
        ai_state_idx = headers.index("ai-State")
        ai_nachos_idx = headers.index("ai-Base NACHOS Score")
        # Row 2: AZ verbatim preserved + AI tier 1.
        row2 = [details.cell(row=2, column=c).value for c in range(1, details.max_column + 1)]
        assert row2[0] == "Arizona"
        assert row2[ai_state_idx] == "AZ"
        assert row2[ai_nachos_idx] == 1
        # Row 3: human said tier 3, AI says tier 0 → tier_delta_ge2.
        # Verify via ai-summary.
        s = wb["ai-summary"]
        cell_map = {
            s.cell(row=r, column=1).value: r
            for r in range(1, s.max_row + 1)
            if isinstance(s.cell(row=r, column=1).value, str)
        }
        # Row 2 is match_exact (1==1, 1.0==1.0); row 3 is tier_delta_ge2 (3 vs 0).
        assert s.cell(row=cell_map["match_exact"], column=2).value == 1
        assert s.cell(row=cell_map["tier_delta_ge2"], column=2).value == 1


# ---------------------------------------------------------------------------
# Match Status column on the Details sheet — reviewer filterability.
# ---------------------------------------------------------------------------


class TestMatchStatusColumn:
    """Match Status column inserted between origin and ai- block."""

    def _origin_headers(self) -> list[str]:
        return [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Required", "Business Logic (Redacted)", "Complex Business Logic",
            "NACHOS score", "Adjusted NACHOS Score",
        ]

    def test_column_inserted_between_origin_and_ai_when_human_cols_set(
        self, tmp_path
    ):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        _write_origin_xlsx(input_path, self._origin_headers(), [
            ["Wisconsin", "Enr", "AcademicWeek", "beginDate", "date",
             "Yes", "—", "No", 0, 0],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        details = wb["Details"]
        headers = [details.cell(row=1, column=c).value for c in range(1, details.max_column + 1)]
        # Column lands at index 10 (0-indexed), right after the 10
        # origin cols and right before ai-State.
        assert headers[10] == "Match Status"
        assert headers[11] == "ai-State"

    def test_column_omitted_when_human_cols_unset(self, tmp_path):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        _write_origin_xlsx(input_path, [
            "State", "Entity Name", "Data Element", "Data Type",
            "Required", "Random Sample",
        ], [
            ["Wisconsin", "AcademicWeek", "beginDate", "date", "Yes", "Yes"],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-min", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=1, element_col=2,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        details = wb["Details"]
        headers = [details.cell(row=1, column=c).value for c in range(1, details.max_column + 1)]
        assert "Match Status" not in headers

    def test_values_match_summary_buckets(self, tmp_path):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        _write_origin_xlsx(input_path, self._origin_headers(), [
            # WI tier 0/0 vs AI 0/0.0 → match_exact.
            ["Wisconsin", "Enr", "AcademicWeek", "beginDate", "date",
             "Yes", "—", "No", 0, 0],
            # TX tier 2/2.5 vs AI 1/1.0 → tier_delta_1.
            ["Texas", "Enr", "StudentSchoolAssociation", "entryDate", "date",
             "No", "—", "Yes", 2, 2.5],
            # MN unmatched element + human-scored → human_only.
            ["Minnesota", "X", "AcademicWeek", "missingElement", "string",
             "No", "—", "No", 1, 1.0],
            # Out-of-scope state (Nebraska, never AI-scored) → out_of_scope.
            ["Nebraska", "X", "Foo", "bar", "string",
             "No", "—", "No", 1, 1.0],
            # WI matched but human blank → ai_only.
            ["Wisconsin", "Enr", "StudentSchoolAssociation", "entryDate", "date",
             "Yes", "—", "Yes", None, None],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        details = wb["Details"]
        headers = [details.cell(row=1, column=c).value for c in range(1, details.max_column + 1)]
        ms_col = headers.index("Match Status") + 1
        statuses = [
            details.cell(row=r, column=ms_col).value
            for r in range(2, details.max_row + 1)
        ]
        assert statuses == [
            "match_exact",
            "tier_delta_1",
            "human_only",
            "out_of_scope",
            "ai_only",
        ]

    def test_auto_filter_enabled_when_column_present(self, tmp_path):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        _write_origin_xlsx(input_path, self._origin_headers(), [
            ["Wisconsin", "Enr", "AcademicWeek", "beginDate", "date",
             "Yes", "—", "No", 0, 0],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        details = wb["Details"]
        # Filter ref spans the full sheet so every column is filterable.
        assert details.auto_filter.ref == details.dimensions
        assert details.freeze_panes == "A2"

    def test_bucket_for_row_helper_handles_out_of_scope(self):
        rr = hsb._ResolvedRow(
            origin=hsb._OriginRow(
                row_idx=2,
                cells=("Nebraska", "Foo", "bar"),
                state_raw="Nebraska",
                entity="Foo",
                element="bar",
                human_nachos=2,
                human_adj=2.0,
            ),
            state_code=None,
            in_scope=False,
            matched=False,
            mc_cells=[None] * len(hsb._MC_HEADERS),
        )
        assert hsb._bucket_for_row(rr) == "out_of_scope"


# ---------------------------------------------------------------------------
# Classification helpers — pure-function tests.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pattern F residual surfacing — issue #138 PR 3.
# ---------------------------------------------------------------------------


class TestPatternFResidualLine:
    def test_na_entity_and_entity_absent_surface_when_human_scored(self, tmp_path):
        """In-scope unmatched + human-scored rows bucket into Pattern F counts.

        Verifies that the ai-summary block adds a "Pattern F residual"
        row enumerating per-state buckets (`F-na-entity` for ``"NA"``
        entities, `F-entity-absent` for entities not in spine/sidecar)
        — and that rows lacking a human score (`neither` bucket)
        DON'T inflate the count.
        """
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        origin_headers = [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Required", "Business Logic (Redacted)", "Complex Business Logic",
            "NACHOS score", "Adjusted NACHOS Score",
        ]
        _write_origin_xlsx(input_path, origin_headers, [
            # WI NA-entity placeholder, human-scored → F-na-entity = 1.
            ["Wisconsin", "X", "NA", "anyField", "string",
             "No", "—", "No", 1, 1.0],
            # WI entity not in sidecar/spine, human-scored → F-entity-absent = 1.
            ["Wisconsin", "X", "ChartOfAccounts", "FundCode", "string",
             "No", "—", "No", 2, 2.0],
            # WI entity not in sidecar/spine, human BLANK → `neither` bucket;
            # MUST NOT count toward Pattern F.
            ["Wisconsin", "X", "ChartOfAccounts", "ProgramCode", "string",
             "No", "—", "No", None, None],
            # WI in-scope match (control row) — entity in sidecar.
            ["Wisconsin", "Enr", "AcademicWeek", "beginDate", "date",
             "Yes", "—", "No", 0, 0],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        s = wb["ai-summary"]
        f_residual_row = next(
            (
                r for r in range(1, s.max_row + 1)
                if s.cell(row=r, column=1).value == "Pattern F residual"
            ),
            None,
        )
        assert f_residual_row is not None, "Pattern F residual line missing"
        text = s.cell(row=f_residual_row, column=2).value
        assert "WI: F-entity-absent 1, F-na-entity 1" in text
        # The note references issue #119 + the chartOfAccounts /
        # AZ Part-C examples for analyst context.
        assert "issue #119" in text
        assert "chartOfAccounts" in text

    def test_pattern_f_omitted_when_no_residual(self, tmp_path):
        """Workbooks where every in-scope unmatched row is recoverable-pattern
        (or every row matched) skip the Pattern F line entirely."""
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        origin_headers = [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Required", "Business Logic (Redacted)", "Complex Business Logic",
            "NACHOS score", "Adjusted NACHOS Score",
        ]
        # Single WI in-scope row that resolves cleanly. No Pattern F-bucket
        # rows present.
        _write_origin_xlsx(input_path, origin_headers, [
            ["Wisconsin", "Enr", "AcademicWeek", "beginDate", "date",
             "Yes", "—", "No", 0, 0],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        s = wb["ai-summary"]
        labels = {
            s.cell(row=r, column=1).value
            for r in range(1, s.max_row + 1)
        }
        assert "Pattern F residual" not in labels


# ---------------------------------------------------------------------------
# Classification helpers — pure-function tests.
# ---------------------------------------------------------------------------


class TestClassifyHelper:
    def test_match_exact_when_tier_and_adj_agree(self):
        assert hsb._classify(1, 1.0, 1, 1.0) == "match_exact"

    def test_match_tier_when_adj_differs(self):
        assert hsb._classify(1, 1.0, 1, 1.5) == "match_tier"

    def test_tier_delta_1(self):
        assert hsb._classify(2, 2.0, 1, 1.0) == "tier_delta_1"

    def test_tier_delta_ge2(self):
        assert hsb._classify(3, 3.0, 0, 0.0) == "tier_delta_ge2"

    def test_human_only(self):
        assert hsb._classify(2, 2.0, None, None) == "human_only"

    def test_ai_only(self):
        assert hsb._classify(None, None, 1, 1.0) == "ai_only"

    def test_neither(self):
        assert hsb._classify(None, None, None, None) == "neither"


# ---------------------------------------------------------------------------
# Excel-corruption guard.
# ---------------------------------------------------------------------------


class TestExcelCorruption:
    def test_leading_equals_strings_render_as_text(self, tmp_path):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        wi_elements_path = sidecar_dir / "wi_elements_source.json"
        payload = json.loads(wi_elements_path.read_text(encoding="utf-8"))
        payload["elements"][0]["business_rules_text"] = (
            "=== Element-specific rules (from https://example) ===\nLine"
        )
        wi_elements_path.write_text(json.dumps(payload), encoding="utf-8")

        input_path = tmp_path / "in.xlsx"
        _write_origin_xlsx(input_path, [
            "State", "Entity Name", "Data Element", "Data Type",
            "Required", "Random Sample",
        ], [
            ["Wisconsin", "AcademicWeek", "beginDate", "date", "Yes", "Yes"],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-min", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=1, element_col=2,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        details = wb["Details"]
        headers = [details.cell(row=1, column=c).value for c in range(1, details.max_column + 1)]
        bl_idx = headers.index("ai-Business Logic (Formula)") + 1
        cell = details.cell(row=2, column=bl_idx)
        assert isinstance(cell.value, str)
        assert cell.value.startswith("===")
        assert cell.data_type == "s"


# ---------------------------------------------------------------------------
# ai-summary metadata block (formerly the Readme tab — now folded in).
# ---------------------------------------------------------------------------


class TestSummaryMetadata:
    def test_carries_methodology_and_match_rates(self, tmp_path):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        input_path = tmp_path / "in.xlsx"
        _write_origin_xlsx(input_path, [
            "State", "Entity Name", "Data Element", "Data Type",
            "Required", "Random Sample",
        ], [
            ["Wisconsin", "AcademicWeek", "beginDate", "date", "Yes", "Yes"],
            ["Texas", "StudentSchoolAssociation", "entryDate", "date", "No", "Yes"],
            ["Minnesota", "AcademicWeek", "missingElement", "string", None, "Yes"],
            ["Nebraska", "Foo", "bar", "string", None, "Yes"],
        ])
        output_path = tmp_path / "out.xlsx"
        cfg = hsb.BackfillConfig(
            name="synthetic-min", input_path=input_path, sheet_name="Details",
            output_path=output_path,
            state_col=0, entity_col=1, element_col=2,
        )
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        wb = openpyxl.load_workbook(output_path)
        s = wb["ai-summary"]
        # Walk the sheet collecting (col1, col2) until we hit the
        # classification block (which only appears when human cols are
        # configured — not the case here).
        pairs = {
            s.cell(row=r, column=1).value: s.cell(row=r, column=2).value
            for r in range(1, s.max_row + 1)
            if isinstance(s.cell(row=r, column=1).value, str)
        }
        from src.score.aggregate import SCORING_PLAN_VERSION

        assert pairs["Methodology version"] == (
            f"SCORING_PLAN_VERSION = {SCORING_PLAN_VERSION}"
        )
        assert pairs["Lens"] == "source"
        assert pairs["Total rows"] == "4"
        assert pairs["Match rate — WI"] == "1/1 (100.0%)"
        assert pairs["Match rate — TX"] == "1/1 (100.0%)"
        assert pairs["Match rate — MN"] == "0/1 (0.0%)"
        assert pairs["Out of scope (state not WI/MN/TX/AZ/IN)"] == "1"


class TestRunDispatch:
    def test_unknown_config_raises(self):
        try:
            hsb.run("does-not-exist")
        except ValueError as exc:
            assert "does-not-exist" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")


# ---------------------------------------------------------------------------
# Gap-row recovery (issue #166 — follow-up §5 coverage-gap wiring).
#
# Reviewer rows the state's source doc was silent on but the swagger spine
# carries resolve against the spine-anchored gap artifact instead of leaving
# the ai- block blank. Mirrors review_comparison's gap_row_match recovery so
# the deliverable workbook and the reviewer-comparison digest agree on what
# counts as "the AI produced a score".
# ---------------------------------------------------------------------------


class TestGapRowMatch:
    def _setup(self, tmp_path: Path, *, scored: bool = True, gap: bool = True):
        sidecar_dir, spine_dir = _setup_state_artifacts(tmp_path)
        if gap:
            _write_gap_artifacts("MN", sidecar_dir, scored=scored)
        input_path = tmp_path / "in.xlsx"
        origin_headers = [
            "State", "Domain", "Entity Name", "Data Element", "Data Type",
            "Required", "Business Logic (Redacted)", "Complex Business Logic",
            "NACHOS score", "Adjusted NACHOS Score",
            "Justification for Adjusted NACHOS  Score",
        ]
        _write_origin_xlsx(input_path, origin_headers, [
            # MN: source-lens miss (Calendar not in source fixtures) but the
            # bare leaf resolves to the swagger-sourced gap row.
            ["Minnesota", "Calendar", "Calendar", "daysInSession", "number",
             "No", "—", "Yes", 3, 4.5, "human_complex"],
        ])
        cfg = hsb.BackfillConfig(
            name="synthetic-standard", input_path=input_path, sheet_name="Details",
            output_path=tmp_path / "out.xlsx",
            state_col=0, entity_col=2, element_col=3,
            human_nachos_col=8, human_adj_col=9,
        )
        return cfg, sidecar_dir, spine_dir

    def _details_headers_and_row(self, output_path: Path, row: int = 2):
        wb = openpyxl.load_workbook(output_path)
        details = wb["Details"]
        headers = [
            details.cell(row=1, column=c).value
            for c in range(1, details.max_column + 1)
        ]
        cells = [
            details.cell(row=row, column=c).value
            for c in range(1, details.max_column + 1)
        ]
        return headers, cells

    def test_gap_sourced_row_carries_ai_score(self, tmp_path):
        cfg, sidecar_dir, spine_dir = self._setup(tmp_path)
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        headers, cells = self._details_headers_and_row(cfg.output_path)
        nachos = cells[headers.index("ai-Base NACHOS Score")]
        # Single-spaced post Sequence-1 hygiene (origin fixture headers
        # above keep the external reviewer file's double space).
        adj = cells[headers.index("ai-Adjusted NACHOS Score")]
        # Deterministic gap score surfaced (tier 0, +0.5 necessary-ext adj).
        assert nachos == 0
        assert adj == 0.5

    def test_gap_sourced_row_bucketed_as_gap_row_match(self, tmp_path):
        cfg, sidecar_dir, spine_dir = self._setup(tmp_path)
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        headers, cells = self._details_headers_and_row(cfg.output_path)
        assert cells[headers.index("Match Status")] == "gap_row_match"

    def test_gap_sourced_row_flagged_swagger_provenance(self, tmp_path):
        cfg, sidecar_dir, spine_dir = self._setup(tmp_path)
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        headers, cells = self._details_headers_and_row(cfg.output_path)
        assert cells[headers.index("ai-Documentation Source")] == "Swagger"

    def test_gap_row_counts_as_matched_in_rate(self, tmp_path):
        cfg, sidecar_dir, spine_dir = self._setup(tmp_path)
        resolved = [
            hsb._resolve_row(o, _ctx(sidecar_dir, spine_dir))
            for o in hsb._load_origin(cfg)[1]
        ]
        rate_rows, _, _ = hsb._match_rate_rows(resolved)
        mn = next(r for r in rate_rows if r[0] == "MN")
        assert mn[1] == 1 and mn[2] == 1  # 1 matched / 1 total

    def test_gap_recognized_but_unscored_stays_blank(self, tmp_path):
        # Gap artifact present, but no deterministic gap score → ai- blank.
        cfg, sidecar_dir, spine_dir = self._setup(tmp_path, scored=False)
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        headers, cells = self._details_headers_and_row(cfg.output_path)
        assert cells[headers.index("ai-Base NACHOS Score")] is None
        assert cells[headers.index("Match Status")] == "human_only"

    def test_no_gap_artifact_falls_back_to_human_only(self, tmp_path):
        # Absent gap artifact → unchanged pre-gap behavior (blank ai-).
        cfg, sidecar_dir, spine_dir = self._setup(tmp_path, gap=False)
        hsb.build_workbook(cfg, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir)
        headers, cells = self._details_headers_and_row(cfg.output_path)
        assert cells[headers.index("ai-Base NACHOS Score")] is None
        assert cells[headers.index("Match Status")] == "human_only"

    def test_gap_row_match_in_classification_order(self):
        assert "gap_row_match" in hsb._CLASSIFICATION_ORDER
        assert "gap_row_match" in hsb._CLASSIFICATION_DESCRIPTION


def _ctx(sidecar_dir: Path, spine_dir: Path) -> dict:
    """Build the per-state context map the resolver expects."""
    contexts: dict = {}
    for state in ("AZ", "WI", "MN", "TX", "IN"):
        c = hsb._load_state_context(
            state, sidecar_dir=sidecar_dir, spine_dir_path=spine_dir
        )
        if c is not None:
            contexts[state] = c
    return contexts
