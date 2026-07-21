"""Smoke tests for the cross-state coverage report.

Real `data/out` / `data/spine` JSON is never read by these tests — they
construct minimal synthetic `StateElements`, `StateSpine`, and gap-log
dicts, write them to a tmp dir, then `run(states=(...), out_dir=tmp)`.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import click

from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
)
from src.models.element import ElementRecord, StateElements
from src.models.spine import SpineSourceURLs, StateSpine
from src.report import coverage


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self):
        """POC-2 pitfall: decorating run() with @click.command breaks the CLI
        invocation path. Same guard pattern as tests/test_ingest_*."""
        assert not isinstance(coverage.run, click.Command)
        assert inspect.isfunction(coverage.run)


def _make_spine(
    state: str,
    core_entities: dict[str, list[str]],
    extensions: dict[str, str] | None = None,
) -> StateSpine:
    """Build a minimal StateSpine with entities (entity -> [prop_names])."""
    entities = {
        name: EntityEntry(
            description=f"{name} entity",
            properties={p: PropertyInfo() for p in props},
        )
        for name, props in core_entities.items()
    }
    ext_entries = {
        ext_name: ExtensionEntry(
            extends_entity=target,
            source_prefix=state.lower(),
            properties={f"{state.lower()}_extra": PropertyInfo()},
        )
        for ext_name, target in (extensions or {}).items()
    }
    catalog = EdFiCatalog(
        version="4.0.0",
        entity_count=len(entities),
        extension_count=len(ext_entries),
        entities=entities,
        extensions=ext_entries,
    )
    return StateSpine(
        state=state,
        edfi_version="4.0.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(
            resources=f"https://example/{state}/resources/swagger.json",
        ),
        catalog=catalog,
    )


def _make_elements(
    state: str,
    rows: list[tuple[str, str, str | None]],
    *,
    source: str = "core",
) -> StateElements:
    """Build StateElements. rows = [(entity, element_name, business_rules or None)].

    ``source`` defaults to "core" so the test rows count toward
    ``source.matched`` (which is now derived from elements via
    ``count(source != 'unknown')`` under v21). Tests that need to
    exercise the unmatched fallback can pass ``source="unknown"``
    explicitly.
    """
    records = [
        ElementRecord(
            state=state,
            edfi_version="4.0",
            domain="Test Domain",
            entity=entity,
            element_name=elem,
            definition_text=f"def for {elem}",
            business_rules_text=rules,
            documented=True,
            source=source,
        )
        for entity, elem, rules in rows
    ]
    return StateElements(
        state=state,
        edfi_version="4.0",
        extracted_at=datetime.now(timezone.utc),
        element_count=len(records),
        elements=records,
    )


def _make_gap_log(state: str, total: int, matched: int, unmatched: list[tuple[str, str, str]]) -> dict:
    by_entity: dict[str, dict] = {}
    for entity, element, tag in unmatched:
        by_entity.setdefault(entity, {"tag": tag, "elements": []})
        by_entity[entity]["elements"].append(element)
    return {
        "state": state,
        "spine_source": f"data/spine/{state.lower()}_spine.json",
        "source_coverage": {
            "matched": matched,
            "total": total,
            "pct": round(100.0 * matched / total, 1) if total else 0.0,
        },
        "spine_coverage": {"matched_unique_keys": matched, "total_spine_keys": 100, "pct": 0.0},
        "unmatched_source_count": len(unmatched),
        "unflatten_recovered_count": 0,
        "unflatten_recovered": [],
        "unmatched_by_entity": by_entity,
        "missing_from_docs_count": 0,
        "missing_from_docs_samples": [],
    }


def _write_fixture(tmp: Path, state: str, spine, elements, gap_log) -> None:
    (tmp / "data" / "out").mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "spine").mkdir(parents=True, exist_ok=True)
    s = state.lower()
    (tmp / "data" / "out" / f"{s}_elements_source.json").write_text(
        elements.model_dump_json(indent=2), encoding="utf-8"
    )
    (tmp / "data" / "out" / f"{s}_gap_log.json").write_text(
        json.dumps(gap_log), encoding="utf-8"
    )
    (tmp / "data" / "spine" / f"{s}_spine.json").write_text(
        spine.model_dump_json(indent=2), encoding="utf-8"
    )


class TestRun:
    def test_smoke_produces_json_and_md(self, tmp_path, monkeypatch):
        # Build three minimal states. Calendar is in all three (core entity),
        # Student is in WI+AZ only, MN has an extension-only `mn_specialProgram`.
        az_spine = _make_spine(
            "AZ",
            {"Calendar": ["calendarCode"], "Student": ["studentUniqueId"]},
            {"CalendarExtension": "Calendar"},
        )
        wi_spine = _make_spine(
            "WI",
            {"Calendar": ["calendarCode"], "Student": ["studentUniqueId"]},
        )
        mn_spine = _make_spine(
            "MN",
            {"Calendar": ["calendarCode"]},
            {"mn_specialProgram": "mn_specialProgram"},  # extension-only
        )

        az_elems = _make_elements("AZ", [
            ("Calendar", "calendarCode", "Must be unique"),
            ("Student", "studentUniqueId", None),
        ])
        wi_elems = _make_elements("WI", [
            ("Calendar", "calendarCode", "Annual unique"),
        ])
        mn_elems = _make_elements("MN", [
            ("Calendar", "calendarCode", None),
            ("Calendar", "notInSpine", None),
        ])

        az_gap = _make_gap_log("AZ", 2, 2, [])
        wi_gap = _make_gap_log("WI", 1, 1, [])
        mn_gap = _make_gap_log(
            "MN", 2, 1, [("Calendar", "notInSpine", "unknown")]
        )

        _write_fixture(tmp_path, "AZ", az_spine, az_elems, az_gap)
        _write_fixture(tmp_path, "WI", wi_spine, wi_elems, wi_gap)
        _write_fixture(tmp_path, "MN", mn_spine, mn_elems, mn_gap)

        # Redirect module-level constants to the tmp dir.
        monkeypatch.setattr(coverage, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(coverage, "_SPINE_DIR", tmp_path / "data" / "spine")

        # Explicit 3-state run so the test is robust against future
        # extensions of _STATES (e.g. Phase 6.4 added TX).
        report = coverage.run(
            states=("AZ", "WI", "MN"), out_dir=tmp_path / "data" / "out"
        )

        assert (tmp_path / "data" / "out" / "coverage_report.json").exists()
        md_path = tmp_path / "data" / "out" / "coverage_report.md"
        assert md_path.exists()
        md = md_path.read_text(encoding="utf-8")
        assert "Coverage Report" in md
        assert "Cross-state entity overlap" in md
        # Dual coverage header — reviewer 1 flagged the single coverage
        # number as a communication blocker.
        assert "Source coverage" in md
        assert "Spine coverage" in md

        assert len(report["per_state"]) == 3
        az_block = next(b for b in report["per_state"] if b["state"] == "AZ")
        assert az_block["source"]["matched"] == 2
        assert az_block["source"]["coverage_pct"] == 100.0
        assert az_block["enrichment"]["with_business_rules_text"] == 1
        assert az_block["enrichment"]["with_definition_text"] == 2

        mn_block = next(b for b in report["per_state"] if b["state"] == "MN")
        assert mn_block["unmatched_breakdown"].get("unknown") == 1

        cc = report["cross_state"]
        # `Calendar` normalizes to `calendar` and appears in all three states.
        assert "calendar" in cc["core_entities_all_states"]
        # `Student` is WI+AZ only (normalizes to `student`). Pair keys are
        # now produced via `itertools.combinations(sorted(states), 2)`, so
        # the alphabetized key is `AZ_and_WI_only`.
        assert "student" in cc["pair_only_entities"]["AZ_and_WI_only"]
        # MN's extension-only entity surfaces.
        assert any(
            e.startswith("mn_") or "special" in e
            for e in cc["extension_only_entities"]["MN"]
        )
        # 3 states → C(3,2)=3 pair buckets + 3 single-state buckets.
        pair_keys = [k for k in cc["entity_presence_counts"] if "_and_" in k]
        single_keys = [
            k
            for k in cc["entity_presence_counts"]
            if k.endswith("_only") and "_and_" not in k
        ]
        assert len(pair_keys) == 3
        assert len(single_keys) == 3

    def test_five_state_pair_combinations(self, tmp_path, monkeypatch):
        """Issue #155 update: 5 states → C(5,2)=10 pair buckets + 5 single-state buckets.

        Uses TX-style spine-only fixture (no extensions, no unmatched rows)
        alongside the same AZ/WI/MN fixtures + IN."""
        states_cfg = {
            "AZ": _make_spine("AZ", {"Calendar": ["calendarCode"]}),
            "WI": _make_spine("WI", {"Calendar": ["calendarCode"]}),
            "MN": _make_spine("MN", {"Calendar": ["calendarCode"]}),
            "TX": _make_spine("TX", {"Calendar": ["calendarCode"]}),
            "IN": _make_spine("IN", {"Calendar": ["calendarCode"]}),
        }
        for state, spine in states_cfg.items():
            elems = _make_elements(state, [("Calendar", "calendarCode", None)])
            gap = _make_gap_log(state, 1, 1, [])
            _write_fixture(tmp_path, state, spine, elems, gap)

        monkeypatch.setattr(coverage, "_OUT_DIR", tmp_path / "data" / "out")
        monkeypatch.setattr(coverage, "_SPINE_DIR", tmp_path / "data" / "spine")

        report = coverage.run(
            states=("AZ", "WI", "MN", "TX", "IN"), out_dir=tmp_path / "data" / "out"
        )
        assert len(report["per_state"]) == 5

        cc = report["cross_state"]
        assert cc["state_count"] == 5
        # C(5, 2) = 10 pair buckets.
        pair_keys = [k for k in cc["entity_presence_counts"] if "_and_" in k]
        single_keys = [
            k
            for k in cc["entity_presence_counts"]
            if k.endswith("_only") and "_and_" not in k
        ]
        assert len(pair_keys) == 10
        assert len(single_keys) == 5
        # `Calendar` in all 5 states → `all_states` bucket counts it.
        assert "calendar" in cc["core_entities_all_states"]
        # Markdown includes all 5 states + the alphabetized pair lines.
        md = (tmp_path / "data" / "out" / "coverage_report.md").read_text(
            encoding="utf-8"
        )
        assert "all 5 states" in md
        assert "AZ ∩ TX only:" in md
        assert "TX-only:" in md


class TestStateBlock:
    def test_enrichment_counts_are_accurate(self):
        spine = _make_spine("AZ", {"Calendar": ["calendarCode"]})
        elements = _make_elements("AZ", [
            ("Calendar", "calendarCode", "rule A"),
            ("Calendar", "beginDate", None),
            ("Calendar", "endDate", "rule B"),
        ])
        gap = _make_gap_log("AZ", 3, 3, [])
        si = coverage.StateInputs(state="AZ", elements=elements, gap_log=gap, spine=spine)
        block = coverage.build_state_block(si)
        assert block["enrichment"]["with_business_rules_text"] == 2
        assert block["enrichment"]["with_definition_text"] == 3
        assert block["spine"]["entities"] == 1
        assert block["source"]["matched"] == 3
        # Tier 2.5: spine_coverage is surfaced in the per-state block.
        assert block["spine_coverage"]["matched_unique_keys"] == 3
        assert block["spine_coverage"]["total_spine_keys"] == 100


class TestAzBusinessRuleFlag:
    """Reviewer 1 noted the generic "investigate ingest adapter" flag was
    misleading for AZ (the XLSX genuinely has no Business Rules column)."""

    def test_az_flag_rewords_to_expected_source_limitation(self):
        msg = coverage._business_rule_flag("AZ")
        assert "does not provide a Business Rules column" in msg
        assert "investigate" not in msg

    def test_non_az_flag_preserves_investigate_language(self):
        msg = coverage._business_rule_flag("WI")
        assert "investigate ingest adapter" in msg
