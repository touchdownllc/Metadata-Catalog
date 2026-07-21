"""Tests for cross-lens fact-consistency QA (Phase B commit 6).

Covers ``src.report.fact_consistency`` directly and its integration
with ``src.report.divergence``: the fact-consistency section should
appear in the rendered lens_divergence.md for every run, and
disagreements should surface per-state + per-fact.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.models.element import ElementRecord, StateElements
from src.report import divergence, fact_consistency
from src.score.deterministic import LENS_INDEPENDENT_FACTS


def _write_pair(
    tmp: Path,
    state: str,
    source_rows: list[ElementRecord],
    spine_rows: list[ElementRecord],
) -> Path:
    out = tmp / "data" / "out"
    out.mkdir(parents=True, exist_ok=True)
    src = StateElements(
        state=state,
        edfi_version="4.0",
        extracted_at=datetime.now(timezone.utc),
        element_count=len(source_rows),
        elements=source_rows,
    )
    spn = StateElements(
        state=state,
        edfi_version="4.0",
        extracted_at=datetime.now(timezone.utc),
        element_count=len(spine_rows),
        elements=spine_rows,
    )
    (out / f"{state.lower()}_elements_source.json").write_text(
        src.model_dump_json(indent=2), encoding="utf-8"
    )
    (out / f"{state.lower()}_elements_spine.json").write_text(
        spn.model_dump_json(indent=2), encoding="utf-8"
    )
    return out


def _write_empty_spine(tmp: Path, state: str) -> Path:
    """Write a minimal empty StateSpine so `divergence.run()` doesn't
    fall back to the ambient `data/spine/` (fresh-clone friendly)."""
    from src.models.edfi_catalog import EdFiCatalog
    from src.models.spine import SpineSourceURLs, StateSpine

    catalog = EdFiCatalog(
        version="4.0.0", entity_count=0, extension_count=0,
        entities={}, extensions={},
    )
    spine = StateSpine(
        state=state,
        edfi_version="4.0.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(resources="https://example/resources.json"),
        catalog=catalog,
    )
    spine_base = tmp / "data" / "spine"
    spine_base.mkdir(parents=True, exist_ok=True)
    (spine_base / f"{state.lower()}_spine.json").write_text(
        spine.model_dump_json(indent=2), encoding="utf-8"
    )
    return spine_base


def _rec(
    entity: str,
    elem: str,
    *,
    definition_text: str = "def for it",
    business_rules_text: str = "",
    data_type: str = "String",
    documented: bool = True,
    source: str = "core",
) -> ElementRecord:
    return ElementRecord(
        state="TX",
        edfi_version="4.0",
        domain="Test",
        entity=entity,
        element_name=elem,
        definition_text=definition_text,
        business_rules_text=business_rules_text,
        data_type=data_type,
        source=source,
        documented=documented,
    )


class TestComputeState:
    def test_zero_disagreements_when_lenses_match(self, tmp_path):
        rows = [
            _rec("Student", "firstName", business_rules_text="mandatory"),
            _rec("Student", "lastName"),
        ]
        out = _write_pair(tmp_path, "TX", rows, rows)
        block = fact_consistency.compute_state("TX", out)
        assert block["state"] == "TX"
        assert block["shared_documented"] == 2
        assert block["total_disagreements"] == 0
        for fact in LENS_INDEPENDENT_FACTS:
            assert block["per_fact"][fact]["count"] == 0

    def test_business_rules_disagreement_surfaces(self, tmp_path):
        src = [_rec("Student", "firstName", business_rules_text="mandatory")]
        spn = [_rec("Student", "firstName", business_rules_text="")]
        out = _write_pair(tmp_path, "TX", src, spn)
        block = fact_consistency.compute_state("TX", out)
        assert block["total_disagreements"] == 1
        detail = block["per_fact"]["business_rules_present"]
        assert detail["count"] == 1
        sample = detail["samples"][0]
        assert sample["entity"] == "Student"
        assert sample["element_name"] == "firstName"
        assert sample["source_value"] is True
        assert sample["spine_value"] is False

    def test_data_type_disagreement_surfaces(self, tmp_path):
        # Spine-enriched canonical Integer vs source-verbatim "INT(10)"
        src = [_rec("Student", "grade", data_type="INT(10)")]
        spn = [_rec("Student", "grade", data_type="Integer")]
        out = _write_pair(tmp_path, "TX", src, spn)
        block = fact_consistency.compute_state("TX", out)
        detail = block["per_fact"]["data_type_canonical"]
        assert detail["count"] == 1
        assert detail["samples"][0]["source_value"] is False
        assert detail["samples"][0]["spine_value"] is True

    def test_undocumented_rows_excluded_from_join(self, tmp_path):
        # Spine-lens undocumented rows have no source counterpart to cross-
        # check — they must not pollute the shared-documented count.
        src = [_rec("Student", "firstName")]
        spn = [
            _rec("Student", "firstName"),
            _rec("Student", "lastName", documented=False),
        ]
        out = _write_pair(tmp_path, "TX", src, spn)
        block = fact_consistency.compute_state("TX", out)
        assert block["shared_documented"] == 1
        assert block["total_disagreements"] == 0

    def test_source_unknown_rows_without_spine_counterpart_excluded(self, tmp_path):
        # A spine-missing source row has no spine-lens twin under the same
        # (entity, element_name) — join must drop it silently.
        src = [
            _rec("Student", "firstName"),
            _rec("Legacy", "legacyField", source="unknown"),
        ]
        spn = [_rec("Student", "firstName")]
        out = _write_pair(tmp_path, "TX", src, spn)
        block = fact_consistency.compute_state("TX", out)
        assert block["shared_documented"] == 1
        assert block["total_disagreements"] == 0

    def test_sample_cap_at_ten_rows(self, tmp_path):
        # The cap prevents a noisy state from blowing up the MD file.
        src = [
            _rec("Student", f"fld{i}", business_rules_text=f"rule {i}")
            for i in range(15)
        ]
        spn = [_rec("Student", f"fld{i}") for i in range(15)]
        out = _write_pair(tmp_path, "TX", src, spn)
        block = fact_consistency.compute_state("TX", out)
        assert block["total_disagreements"] == 15
        assert len(block["per_fact"]["business_rules_present"]["samples"]) == 10


class TestDivergenceIntegration:
    def test_fact_consistency_in_report_payload(self, tmp_path):
        rows = [_rec("Student", "firstName")]
        _write_pair(tmp_path, "TX", rows, rows)
        spine_base = _write_empty_spine(tmp_path, "TX")
        report = divergence.run(
            states=("TX",), out=tmp_path / "data" / "out", spine_base=spine_base
        )
        assert "fact_consistency" in report
        assert len(report["fact_consistency"]) == 1
        assert report["fact_consistency"][0]["state"] == "TX"

    def test_markdown_renders_fact_consistency_section(self, tmp_path):
        rows = [_rec("Student", "firstName")]
        _write_pair(tmp_path, "TX", rows, rows)
        spine_base = _write_empty_spine(tmp_path, "TX")
        out = tmp_path / "data" / "out"
        divergence.run(states=("TX",), out=out, spine_base=spine_base)
        md = (out / "lens_divergence.md").read_text(encoding="utf-8")
        assert "## Fact consistency — cross-lens QA" in md
        assert "Shared documented rows" in md
        assert "no disagreements to detail" in md

    def test_markdown_renders_disagreement_detail_when_present(self, tmp_path):
        src = [_rec("Student", "firstName", business_rules_text="mandatory")]
        spn = [_rec("Student", "firstName", business_rules_text="")]
        _write_pair(tmp_path, "TX", src, spn)
        spine_base = _write_empty_spine(tmp_path, "TX")
        out = tmp_path / "data" / "out"
        divergence.run(states=("TX",), out=out, spine_base=spine_base)
        md = (out / "lens_divergence.md").read_text(encoding="utf-8")
        assert "### Disagreement detail" in md
        assert "business_rules_present" in md
        assert "Student.firstName" in md

    def test_json_payload_includes_fact_consistency(self, tmp_path):
        rows = [_rec("Student", "firstName")]
        _write_pair(tmp_path, "TX", rows, rows)
        spine_base = _write_empty_spine(tmp_path, "TX")
        out = tmp_path / "data" / "out"
        divergence.run(states=("TX",), out=out, spine_base=spine_base)
        payload = json.loads((out / "lens_divergence.json").read_text(encoding="utf-8"))
        assert "fact_consistency" in payload
        assert payload["fact_consistency"][0]["state"] == "TX"
