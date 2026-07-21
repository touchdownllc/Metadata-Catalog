"""Smoke test for the AZ Use Case XLSX adapter's end-to-end `run()`.

Live XLSX parse / enrichment are never hit — `parse_az_excel` is
monkeypatched to return a tiny fixture. The test's job is to guard the
Option 3b dual-lens contract: a successful `run()` writes BOTH
`az_elements_source.json` and `az_elements_spine.json`. Unit coverage
of the AZ XLSX parser itself lives alongside round-2 / round-2.2 / tier-5
regression suites.
"""

from __future__ import annotations

import pytest

import inspect
import json
from datetime import datetime, timezone

import click

from src.ingest import arizona
from src.ingest.arizona import (
    AZElementRow,
    AZEntityTable,
    run,
)
from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
)
from src.models.element import StateElements
from src.models.spine import SpineSourceURLs, StateSpine
from src.spine.build import build_lookup_index


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self):
        """POC-2 had `@click.command()` on module-level `run()` — when the
        POC-3 CLI called `run()` it invoked Click.Command.__call__, which
        re-parsed sys.argv and broke. Guard against a regression (gotcha #1
        in CLAUDE.md — bit WI/MN already)."""
        assert not isinstance(arizona.run, click.Command)
        assert inspect.isfunction(arizona.run)
        assert inspect.signature(arizona.run).parameters == {}


def _make_az_spine() -> StateSpine:
    """Minimal AZ spine: one core Student entity + one az_ extension."""
    entities = {
        "Student": EntityEntry(
            description="Ed-Fi Student.",
            domains=["Student Identification"],
            properties={
                "firstName": PropertyInfo(
                    description="First name.", type="string",
                ),
                "birthDate": PropertyInfo(
                    description="Birth date.", type="string", format="date",
                ),
            },
            references={},
            sub_collections={},
        ),
    }
    extensions = {
        "az_studentExtension": ExtensionEntry(
            extends_entity="Student",
            source_prefix="az",
            properties={
                "tribalAffiliation": PropertyInfo(
                    description="Tribal affiliation.", type="string",
                ),
            },
        ),
    }
    catalog = EdFiCatalog(
        version="5.2",
        entity_count=len(entities),
        extension_count=len(extensions),
        entities=entities,
        extensions=extensions,
        lookup_index=build_lookup_index(entities, extensions),
    )
    return StateSpine(
        state="AZ",
        edfi_version="5.2",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(
            resources="http://example.test/metadata/data/v3/resources/swagger.json",
        ),
        catalog=catalog,
    )


def _make_az_tables() -> list[AZEntityTable]:
    """Tiny XLSX fixture: one core entity + one az_ extension + one
    legacy-only entity to exercise the unresolved/demote path."""
    return [
        AZEntityTable(
            sheet_name="Student Identification",
            entity_name="edfi.Student",
            is_extension=False,
            requirement_level="Required",
            elements=[
                AZElementRow(
                    raw_name="FirstName (R)",
                    clean_name="firstName",
                    optionality="R",
                    data_type="String",
                    codes_ref=None,
                    description="The student's first name.",
                ),
                AZElementRow(
                    raw_name="BirthDate (R)",
                    clean_name="birthDate",
                    optionality="R",
                    data_type="Date",
                    codes_ref=None,
                    description="Student birth date.",
                ),
            ],
        ),
        AZEntityTable(
            sheet_name="Student Identification",
            entity_name="az.StudentExtension",
            is_extension=True,
            requirement_level="Optional",
            elements=[
                AZElementRow(
                    raw_name="TribalAffiliation (O)",
                    clean_name="tribalAffiliation",
                    optionality="O",
                    data_type="String",
                    codes_ref=None,
                    description="Tribal affiliation.",
                ),
            ],
        ),
        AZEntityTable(
            # AzEDS-only entity with no spine counterpart — source namespace
            # marks it `az.*`, but `demote_unmatched_to_unknown` should tag
            # it as `source="unknown"` because the spine doesn't carry it.
            sheet_name="Legacy Area",
            entity_name="az.LegacyAzEntity",
            is_extension=True,
            requirement_level="Optional",
            elements=[
                AZElementRow(
                    raw_name="LegacyField (O)",
                    clean_name="legacyField",
                    optionality="O",
                    data_type="String",
                    codes_ref=None,
                    description="Legacy field only AzEDS tracks.",
                ),
            ],
        ),
    ]


class TestEndToEndRun:
    """Regression guard: `run()` must write BOTH `_source.json` and
    `_spine.json` artifacts (Option 3b dual-lens contract). Without an
    end-to-end test, a future refactor could silently drop the
    `_spine.json` write and the suite would stay green.
    """

    def test_run_writes_both_lens_artifacts(self, tmp_path, monkeypatch):
        spine = _make_az_spine()
        spine_path = tmp_path / "data" / "spine" / "az_spine.json"
        spine_path.parent.mkdir(parents=True, exist_ok=True)
        spine_path.write_text(spine.model_dump_json(indent=2), encoding="utf-8")

        out_dir = tmp_path / "data" / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        xlsx_path = tmp_path / "Use_Case_12.0_test.xlsm"
        xlsx_path.write_bytes(b"")  # exists-check passes; parse is monkeypatched

        # Rules enrichment is optional — point at a path that doesn't exist
        # so run() hits the `else` branch and skips the rules step.
        rules_path = tmp_path / "no_az_integrity_rules.json"

        monkeypatch.setattr(arizona, "_MC_ROOT", tmp_path)
        monkeypatch.setattr(arizona, "_AZ_SPINE_PATH", spine_path)
        monkeypatch.setattr(arizona, "_AZ_XLSX_PATH", xlsx_path)
        monkeypatch.setattr(arizona, "_AZ_RULES_PATH", rules_path)
        monkeypatch.setattr(
            arizona, "_AZ_ELEMENTS_OUT", out_dir / "az_elements_source.json"
        )
        monkeypatch.setattr(
            arizona,
            "_AZ_ELEMENTS_SPINE_OUT",
            out_dir / "az_elements_spine.json",
        )
        monkeypatch.setattr(arizona, "_AZ_GAP_OUT", out_dir / "az_gap_log.json")
        monkeypatch.setattr(
            arizona, "parse_az_excel", lambda _path: _make_az_tables()
        )

        run()

        source_path = out_dir / "az_elements_source.json"
        spine_elements_path = out_dir / "az_elements_spine.json"
        gap_path = out_dir / "az_gap_log.json"
        assert source_path.exists()
        assert spine_elements_path.exists(), (
            "run() must write the spine-lens artifact (Option 3b contract)"
        )
        assert gap_path.exists()

        source_elements = StateElements.model_validate_json(
            source_path.read_text(encoding="utf-8")
        )
        assert source_elements.state == "AZ"
        assert source_elements.element_count == len(source_elements.elements)
        assert source_elements.element_count > 0
        # The legacy-only row should be `source="unknown"` after
        # demote_unmatched_to_unknown runs.
        legacy = next(
            (r for r in source_elements.elements if r.element_name == "legacyField"),
            None,
        )
        assert legacy is not None
        assert legacy.source == "unknown"

        spine_elements = StateElements.model_validate_json(
            spine_elements_path.read_text(encoding="utf-8")
        )
        assert spine_elements.state == "AZ"
        assert spine_elements.element_count == len(spine_elements.elements)
        spine_pairs = {
            (r.entity, r.element_name) for r in spine_elements.elements
        }
        # Canonical spine slots walked from the catalog:
        assert ("Student", "firstName") in spine_pairs
        assert ("Student", "birthDate") in spine_pairs
        assert ("Student", "tribalAffiliation") in spine_pairs
        # Hybrid append: legacy source row surfaces as source="unknown",
        # documented=True (audit trail).
        assert any(
            r.source == "unknown" and r.element_name == "legacyField"
            and r.documented
            for r in spine_elements.elements
        )

        gap = json.loads(gap_path.read_text(encoding="utf-8"))
        assert "source_coverage" in gap
        assert "spine_coverage" in gap

    def test_run_falls_back_to_bundled_workbook(self, tmp_path, monkeypatch):
        """On a fresh clone there's no copy under `data/raw/az/`; the
        adapter must fall back to the committed `docs/bootstrap/az/`
        workbook so `mc ingest az` works out of the box."""
        spine = _make_az_spine()
        spine_path = tmp_path / "data" / "spine" / "az_spine.json"
        spine_path.parent.mkdir(parents=True, exist_ok=True)
        spine_path.write_text(spine.model_dump_json(indent=2), encoding="utf-8")

        out_dir = tmp_path / "data" / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        absent_primary = tmp_path / "data" / "raw" / "az" / "missing.xlsm"
        bundled = tmp_path / "docs" / "bootstrap" / "az" / "Use_Case_12.0_20260227.xlsm"
        bundled.parent.mkdir(parents=True, exist_ok=True)
        bundled.write_bytes(b"")

        rules_path = tmp_path / "no_az_integrity_rules.json"

        monkeypatch.setattr(arizona, "_MC_ROOT", tmp_path)
        monkeypatch.setattr(arizona, "_AZ_SPINE_PATH", spine_path)
        monkeypatch.setattr(arizona, "_AZ_XLSX_PATH", absent_primary)
        monkeypatch.setattr(arizona, "_AZ_XLSX_BOOTSTRAP_PATH", bundled)
        monkeypatch.setattr(arizona, "_AZ_RULES_PATH", rules_path)
        monkeypatch.setattr(
            arizona, "_AZ_ELEMENTS_OUT", out_dir / "az_elements_source.json"
        )
        monkeypatch.setattr(
            arizona,
            "_AZ_ELEMENTS_SPINE_OUT",
            out_dir / "az_elements_spine.json",
        )
        monkeypatch.setattr(arizona, "_AZ_GAP_OUT", out_dir / "az_gap_log.json")

        seen_path = []

        def _capture(path):
            seen_path.append(path)
            return _make_az_tables()

        monkeypatch.setattr(arizona, "parse_az_excel", _capture)

        run()

        assert seen_path == [bundled], (
            f"parse_az_excel was called with {seen_path!r}; expected "
            f"bundled fallback {bundled!r}"
        )
        assert (out_dir / "az_elements_source.json").exists()
        assert (out_dir / "az_elements_spine.json").exists()


class TestParseFloor:
    """Issue #213 item 2: `detect_entity_tables`' hardcoded column scan
    drops unrecognized tables SILENTLY — `parse_az_excel` must raise when
    the end-of-parse corpus lands below the format-drift floors
    (`_MIN_ENTITY_TABLES` / `_MIN_ELEMENT_ROWS`, module-constant
    monkeypatch seam)."""

    @staticmethod
    def _write_minimal_workbook(path):
        """One recognizable entity table (2 element rows) — a 'truncated'
        corpus far below the floors."""
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Enrollment"
        # Entity header in col G (0-based col 6 — inside the 5-7 scan).
        ws.cell(row=1, column=7, value="edfi.Course")
        # "Column Name" header on the next row (0-based col 8).
        ws.cell(row=2, column=9, value="Column Name")
        ws.cell(row=2, column=10, value="Data Type")
        ws.cell(row=3, column=9, value="courseCode (R)")
        ws.cell(row=3, column=10, value="nvarchar(60)")
        ws.cell(row=4, column=9, value="courseTitle (O)")
        ws.cell(row=4, column=10, value="nvarchar(60)")
        wb.save(path)

    def test_truncated_corpus_raises(self, tmp_path):
        path = tmp_path / "truncated.xlsx"
        self._write_minimal_workbook(path)
        with pytest.raises(ValueError, match="source format may have changed"):
            arizona.parse_az_excel(path)

    def test_unrecognized_format_raises(self, tmp_path):
        """A workbook the column scan recognizes NOTHING in (the silent-drop
        failure mode) must also trip the floor."""
        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Enrollment"
        ws["A1"] = "Entity"
        ws["B1"] = "Field"
        ws["A2"] = "Course"
        ws["B2"] = "courseCode"
        path = tmp_path / "unrecognized.xlsx"
        wb.save(path)
        with pytest.raises(ValueError, match="source format may have changed"):
            arizona.parse_az_excel(path)

    def test_floor_is_a_monkeypatchable_module_constant(self, tmp_path, monkeypatch):
        path = tmp_path / "small.xlsx"
        self._write_minimal_workbook(path)
        monkeypatch.setattr(arizona, "_MIN_ENTITY_TABLES", 1)
        monkeypatch.setattr(arizona, "_MIN_ELEMENT_ROWS", 1)
        tables = arizona.parse_az_excel(path)
        assert len(tables) == 1
        assert len(tables[0].elements) == 2

    def test_bundled_corpus_clears_floor_with_headroom(self):
        """The committed bootstrap workbook must clear both floors by >=2x
        (calibration pin: 107 tables / 663 rows as of 2026-07-09 — if this
        fails after a workbook refresh, re-derive the floors)."""
        from pathlib import Path

        bootstrap = (
            Path(__file__).resolve().parent.parent
            / "docs" / "bootstrap" / "az" / "Use_Case_12.0_20260227.xlsm"
        )
        tables = arizona.parse_az_excel(bootstrap)  # must not raise
        rows = sum(len(t.elements) for t in tables)
        assert len(tables) >= 2 * arizona._MIN_ENTITY_TABLES
        assert rows >= 2 * arizona._MIN_ELEMENT_ROWS
