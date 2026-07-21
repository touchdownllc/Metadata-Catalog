"""Cross-lens consistency invariants.

Source-lens and spine-lens are two projections of the same underlying
(state source doc, Ed-Fi spine) join. Scoring will read BOTH and assume
they agree on the rows they share. This suite pins the invariants that
keep them in lockstep:

- Every spine-lens row sourced from the source doc has a corresponding
  source-lens row (no phantom documentation).
- Hybrid-append spine rows (source='unknown') are a subset of source-
  lens unknowns (modulo the Reference/Collection wrapper filter).
- Filtered-domain placeholders are a spine-lens construct only, and
  their domain labels come from `ingest/domain_filter.PLACEHOLDER_LABELS`.
- The coverage report's headline % for each (state, lens) matches the
  per-record math derived from the goldens. If the JSON disagrees with
  what the records say, the test surfaces the discrepancy.
- The markdown table's percentages parse back to the same values as
  the JSON's `coverage_pct`.

The coverage-report and markdown checks skip cleanly when
`data/out/coverage_report*.{json,md}` are absent (CI without a fresh
pipeline run). The record-level invariants always run against the
committed goldens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.ingest.domain_filter import PLACEHOLDER_LABELS
from src.utils.matching import entity_match_form

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tests" / "golden"
_DATA_OUT = _REPO_ROOT / "data" / "out"

STATES = ("az", "wi", "mn", "tx")


def _load_records(state: str, lens: str) -> list[dict]:
    path = _GOLDEN_DIR / f"{state}_elements_{lens}.json"
    return json.loads(path.read_text(encoding="utf-8"))["elements"]


@pytest.mark.parametrize("state", STATES)
def test_documented_spine_count_bounded_by_source(state: str) -> None:
    """Documented spine-lens emits (source in {core, extension}, not
    hybrid-append) cannot exceed the source-lens rows eligible to have
    enriched them (source != 'unknown' — i.e., landed on a spine slot).

    The one-directional inequality is as much as the artifact alone can
    verify without replicating `canonical_spine_emit_keys`' richer
    alias sink (descriptor-suffix strip, FK prefix, EdOrg subtype
    expansion). Scoring only needs to know the spine-lens "documented"
    rows were all grounded in source-doc content — an upper bound
    tied to source-lens matched count is that grounding.
    """
    src_matched = sum(
        1 for r in _load_records(state, "source") if r["source"] != "unknown"
    )
    spine_emit_documented = sum(
        1 for r in _load_records(state, "spine")
        if r["documented"] and r["source"] in ("core", "extension")
    )
    assert spine_emit_documented <= src_matched, (
        f"{state}: spine-lens has {spine_emit_documented} documented "
        f"core/extension emits but source-lens only has {src_matched} "
        f"matched rows to enrich them — documentation claim exceeds "
        f"source grounding"
    )


@pytest.mark.parametrize("state", STATES)
def test_documented_spine_entities_covered_by_source(state: str) -> None:
    """Every entity that appears with a `documented=True` spine emit
    must also appear in the source-lens golden. Catches the phantom-
    entity case: a spine slot surfaces as documented on an entity the
    source doc never mentioned (which would imply a broken enrichment
    join, since docs are the only thing that could have documented it).
    """
    src_entities = {
        entity_match_form(r["entity"]) for r in _load_records(state, "source")
    }
    for i, r in enumerate(_load_records(state, "spine")):
        if not r["documented"] or r["source"] not in ("core", "extension"):
            continue
        norm = entity_match_form(r["entity"])
        assert norm in src_entities, (
            f"{state}/spine record[{i}] documented entity "
            f"{r['entity']!r} (normalized={norm!r}) does not appear in "
            f"source-lens — phantom documentation claim"
        )


@pytest.mark.parametrize("state", STATES)
def test_hybrid_append_rows_come_from_source(state: str) -> None:
    """Every spine-lens `source='unknown'` row maps to a source-lens
    `source='unknown'` row with the same (entity, element_name). The
    reverse direction doesn't hold — source-lens unknowns with
    data_type in {Reference, Collection} are filtered out of spine-lens
    by the wrapper-drop pass at the tail of `assemble_spine_driven`.
    """
    source_unknowns = {
        (r["entity"], r["element_name"])
        for r in _load_records(state, "source")
        if r["source"] == "unknown"
    }
    for i, r in enumerate(_load_records(state, "spine")):
        if r["source"] != "unknown":
            continue
        key = (r["entity"], r["element_name"])
        assert key in source_unknowns, (
            f"{state}/spine record[{i}] source='unknown' "
            f"(entity={r['entity']!r}, element_name={r['element_name']!r}) "
            f"has no matching source-lens Unresolved row"
        )


@pytest.mark.parametrize("state", STATES)
def test_hybrid_append_reverse_drops_only_wrappers(state: str) -> None:
    """Source-lens unknowns missing from spine-lens are exactly those
    whose data_type is a Reference/Collection wrapper. The spine lens
    drops these in its final filter pass; any other drop would mean
    hybrid-append lost a row on a non-wrapper path.
    """
    spine_unknowns = {
        (r["entity"], r["element_name"])
        for r in _load_records(state, "spine")
        if r["source"] == "unknown"
    }
    for i, r in enumerate(_load_records(state, "source")):
        if r["source"] != "unknown":
            continue
        key = (r["entity"], r["element_name"])
        if key in spine_unknowns:
            continue
        assert r["data_type"] in ("Reference", "Collection"), (
            f"{state}/source record[{i}] source='unknown' "
            f"data_type={r['data_type']!r} missing from spine-lens "
            f"hybrid-append tail — only Reference/Collection wrappers "
            f"are expected to drop"
        )


@pytest.mark.parametrize("state", STATES)
def test_placeholders_well_scoped(state: str) -> None:
    """Spine-lens placeholder rows: count ≤ |PLACEHOLDER_LABELS|,
    exactly one per (state, domain) the filter dropped, domain labels
    are a subset of PLACEHOLDER_LABELS. Source-lens has zero."""
    src_filtered = [r for r in _load_records(state, "source") if r["source"] == "filtered"]
    assert not src_filtered, (
        f"{state}/source: {len(src_filtered)} filtered placeholder rows; "
        f"source-lens should have none"
    )

    placeholders = [r for r in _load_records(state, "spine") if r["source"] == "filtered"]
    labels = [r["domain"] for r in placeholders]
    assert len(labels) == len(set(labels)), (
        f"{state}/spine duplicate placeholder domain labels: {labels}"
    )
    assert set(labels) <= set(PLACEHOLDER_LABELS), (
        f"{state}/spine placeholder domains {sorted(set(labels))} "
        f"include values outside PLACEHOLDER_LABELS "
        f"{sorted(PLACEHOLDER_LABELS)}"
    )
    assert len(placeholders) <= len(PLACEHOLDER_LABELS), (
        f"{state}/spine has {len(placeholders)} placeholder rows; "
        f"max is {len(PLACEHOLDER_LABELS)}"
    )


@pytest.mark.realdata
@pytest.mark.parametrize("state", STATES)
def test_source_coverage_pct_matches_records(state: str) -> None:
    """Source-lens `source.coverage_pct` in coverage_report.json must
    equal `round(count(source != 'unknown') / total * 100, 1)` as
    derived from the source-lens golden. Skips cleanly when the
    coverage report isn't on disk (CI without `data/out/`)."""
    report_path = _DATA_OUT / "coverage_report.json"
    if not report_path.exists():
        pytest.skip("data/out/coverage_report.json not present")
    report = json.loads(report_path.read_text(encoding="utf-8"))

    records = _load_records(state, "source")
    total = len(records)
    matched = sum(1 for r in records if r["source"] != "unknown")
    expected_pct = round(matched / total * 100, 1) if total else 0.0

    block = next(b for b in report["per_state"] if b["state"] == state.upper())
    assert block["source"]["records"] == total, (
        f"{state}: coverage JSON records={block['source']['records']} "
        f"disagrees with golden elements count={total}"
    )
    assert block["source"]["matched"] == matched, (
        f"{state}: coverage JSON matched={block['source']['matched']} "
        f"disagrees with golden count(source!='unknown')={matched}"
    )
    assert block["source"]["coverage_pct"] == expected_pct, (
        f"{state}: coverage JSON pct={block['source']['coverage_pct']} "
        f"disagrees with record-derived pct={expected_pct}"
    )


@pytest.mark.realdata
@pytest.mark.parametrize("state", STATES)
def test_spine_documentation_pct_matches_records(state: str) -> None:
    """Spine-lens `documentation.coverage_pct` in coverage_report_spine.json
    must equal `round(count(documented) / count(source != 'filtered')
    * 100, 1)` from the spine-lens golden — filtered placeholders
    excluded from both numerator and denominator (soft-filter contract)."""
    report_path = _DATA_OUT / "coverage_report_spine.json"
    if not report_path.exists():
        pytest.skip("data/out/coverage_report_spine.json not present")
    report = json.loads(report_path.read_text(encoding="utf-8"))

    records = _load_records(state, "spine")
    scored = [r for r in records if r["source"] != "filtered"]
    total = len(scored)
    documented = sum(1 for r in scored if r["documented"])
    expected_pct = round(documented / total * 100, 1) if total else 0.0

    block = next(b for b in report["per_state"] if b["state"] == state.upper())
    doc = block["documentation"]
    assert doc["lens_records"] == total, (
        f"{state}: spine coverage JSON lens_records={doc['lens_records']} "
        f"disagrees with golden count(source!='filtered')={total}"
    )
    assert doc["documented"] == documented, (
        f"{state}: spine coverage JSON documented={doc['documented']} "
        f"disagrees with golden count(documented)={documented}"
    )
    assert doc["coverage_pct"] == expected_pct, (
        f"{state}: spine coverage JSON pct={doc['coverage_pct']} "
        f"disagrees with record-derived pct={expected_pct}"
    )
    assert doc["filtered_domain_placeholders"] == (len(records) - total), (
        f"{state}: spine coverage JSON filtered_domain_placeholders="
        f"{doc['filtered_domain_placeholders']} disagrees with "
        f"golden's placeholder count={len(records) - total}"
    )


_MD_ROW = re.compile(
    # | AZ | 395/412 (95.9%) | 395/12187 (3.2%) | ...
    r"^\|\s*(?P<state>[A-Z]{2})\s*\|"
    r"\s*(?P<src_num>\d+)/(?P<src_den>\d+)\s*\((?P<src_pct>[\d.]+)%\)\s*\|"
    r"\s*(?P<sp_num>\d+)/(?P<sp_den>\d+)\s*\((?P<sp_pct>[\d.]+)%\)\s*\|",
    re.MULTILINE,
)


def _parse_md_rows(md_path: Path) -> dict[str, dict]:
    """Pull (state, src_pct, sp_pct) triples out of the markdown table."""
    text = md_path.read_text(encoding="utf-8")
    rows: dict[str, dict] = {}
    for m in _MD_ROW.finditer(text):
        rows[m["state"]] = {
            "src_pct": float(m["src_pct"]),
            "sp_pct": float(m["sp_pct"]),
            "src_num": int(m["src_num"]),
            "sp_num": int(m["sp_num"]),
        }
    return rows


@pytest.mark.realdata
@pytest.mark.parametrize("lens", ("source", "spine"))
def test_markdown_percentages_match_json(lens: str) -> None:
    """Every percentage rendered in coverage_report{,.spine}.md parses
    back to the same value carried in coverage_report{,.spine}.json.
    Guards against a divergent renderer (e.g., changed rounding)."""
    json_path = _DATA_OUT / (
        "coverage_report_spine.json" if lens == "spine" else "coverage_report.json"
    )
    md_path = _DATA_OUT / (
        "coverage_report_spine.md" if lens == "spine" else "coverage_report.md"
    )
    if not json_path.exists() or not md_path.exists():
        pytest.skip(f"coverage report files for lens={lens} not present")

    report = json.loads(json_path.read_text(encoding="utf-8"))
    md_rows = _parse_md_rows(md_path)

    for block in report["per_state"]:
        state = block["state"]
        assert state in md_rows, f"markdown missing row for {state}"
        row = md_rows[state]

        # Source column cell is always the source-lens coverage_pct.
        assert row["src_pct"] == block["source"]["coverage_pct"], (
            f"{state} {lens}: markdown source%={row['src_pct']} "
            f"disagrees with JSON source.coverage_pct="
            f"{block['source']['coverage_pct']}"
        )
        # Spine column cell renders `spine_coverage.coverage_pct` in both
        # lens markdowns (see render_markdown in report/coverage.py).
        assert row["sp_pct"] == block["spine_coverage"]["coverage_pct"], (
            f"{state} {lens}: markdown spine%={row['sp_pct']} disagrees "
            f"with JSON spine_coverage.coverage_pct="
            f"{block['spine_coverage']['coverage_pct']}"
        )
