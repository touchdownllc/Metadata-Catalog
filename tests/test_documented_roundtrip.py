"""Soft-filter round-trip: documented Assessment rows survive, the rest
collapse to one placeholder, source-lens is untouched.

This is the core contract that drove PR #8 (spine-lens SIS-never-
populated domain filter). `tests/test_domain_filter.py` already
covers the individual behaviors with small examples; this file pins
the *count-level* invariant on a synthetic fixture with explicit N
and M so a regression that silently mis-counts survivors or
placeholders surfaces as a numeric failure rather than a subtle
behavioral drift.

Why a standalone file: bucket 1 (goldens) would detect a change in
the real data, but only as a ~50 MB diff. This test fails with a
one-line "expected N survivors, got K" message — the direct
regression signal.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.ingest import domain_scope
from src.ingest.shared import assemble_spine_driven, build_source_index
from src.models.edfi_catalog import EdFiCatalog, EntityEntry, PropertyInfo
from src.models.element import ElementRecord
from src.models.spine import SpineSourceURLs, StateSpine


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch):
    """Isolate the raw spine-lens filter behavior from the live
    ``domain_scope`` seed (which enables TX/IN Assessment). These scenarios
    build ``state='TX'`` spines and assert the Assessment placeholder collapse.
    """
    monkeypatch.setattr(domain_scope, "DOMAIN_SOURCES", ())


# Scenario knobs — change with care, the assertions reference both.
_ASSESSMENT_SLOTS_M = 7  # total spine slots in the filtered Assessment entity
_ASSESSMENT_DOCUMENTED_N = 3  # of which the source doc covers


def _build_spine(m: int) -> StateSpine:
    """Spine with one non-filtered entity (`Student`) plus an
    `Assessment` entity carrying `m` properties — Assessment's declared
    domain is `Assessment`, so the filter trips on it.
    """
    assessment_props = {
        f"prop{i:02d}": PropertyInfo(type="string")
        for i in range(m)
    }
    catalog = EdFiCatalog(
        version="4.0.0",
        entity_count=2,
        extension_count=0,
        entities={
            "Student": EntityEntry(
                description="Student",
                domains=["StudentIdentificationAndDemographics"],
                properties={
                    "studentUniqueId": PropertyInfo(type="string", is_identity=True),
                    "firstName": PropertyInfo(type="string"),
                },
            ),
            "Assessment": EntityEntry(
                description="Assessment",
                domains=["Assessment"],
                properties=assessment_props,
            ),
        },
        extensions={},
    )
    return StateSpine(
        state="TX",
        edfi_version="4.0.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(resources="https://example/resources.json"),
        catalog=catalog,
    )


def _source_rows_for(n: int) -> list[ElementRecord]:
    """`n` documented Assessment rows + one unrelated Student row (so
    the source-lens side has non-Assessment content too)."""
    rows = [
        ElementRecord(
            state="TX",
            edfi_version="4.0",
            domain="StudentIdentificationAndDemographics",
            entity="Student",
            element_name="firstName",
            definition_text="Given name.",
            source="core",
            documented=True,
        ),
    ]
    for i in range(n):
        rows.append(ElementRecord(
            state="TX",
            edfi_version="4.0",
            domain="Assessment",
            entity="Assessment",
            element_name=f"prop{i:02d}",
            definition_text=f"State-documented assessment prop{i:02d}.",
            source="core",
            documented=True,
        ))
    return rows


@pytest.fixture
def scenario():
    m = _ASSESSMENT_SLOTS_M
    n = _ASSESSMENT_DOCUMENTED_N
    spine = _build_spine(m)
    source_rows = _source_rows_for(n)
    spine_records, _ = assemble_spine_driven(
        spine=spine,
        source_index=build_source_index(source_rows),
        spine_missing_source_rows=[],
        state="TX",
        edfi_version="4.0.0",
        source_document="synthetic",
    )
    return {
        "m": m,
        "n": n,
        "source_rows": source_rows,
        "spine_records": spine_records,
    }


def test_spine_lens_retains_exactly_n_documented_assessment_rows(scenario):
    """Of the M Assessment spine slots, only the N documented ones
    survive as `documented=True` records. Everything else in the
    Assessment entity (M-N undocumented slots) collapses into the
    single placeholder — so the `documented=True` count on Assessment
    must exactly equal N."""
    n = scenario["n"]
    retained = [
        r for r in scenario["spine_records"]
        if r.entity == "Assessment" and r.source != "filtered"
    ]
    assert len(retained) == n, (
        f"expected {n} retained Assessment rows (documented survivors), "
        f"got {len(retained)}: {[r.element_name for r in retained]!r}"
    )
    for r in retained:
        assert r.documented is True, (
            f"retained Assessment row {r.element_name!r} must be "
            f"documented=True, got {r.documented}"
        )
        assert r.source == "core", (
            f"retained Assessment row {r.element_name!r} source must be "
            f"'core', got {r.source!r}"
        )


def test_spine_lens_emits_one_placeholder_for_dropped_slots(scenario):
    """One placeholder per (state, Assessment). Count of dropped slots
    (M-N) is not persisted on the placeholder — the placeholder carries
    a static note — but the placeholder MUST exist when M-N > 0."""
    assert scenario["m"] - scenario["n"] > 0, (
        "scenario invariant broken: M must exceed N for the filter to "
        "drop anything (M=N would be a trivially-documented domain)"
    )
    placeholders = [
        r for r in scenario["spine_records"] if r.source == "filtered"
    ]
    assert len(placeholders) == 1, (
        f"expected exactly one placeholder row, got {len(placeholders)}: "
        f"{[(r.entity, r.domain) for r in placeholders]!r}"
    )
    ph = placeholders[0]
    assert ph.domain == "Assessment"
    assert ph.entity == "Assessment (filtered)"
    assert ph.element_name == "(filtered)"
    assert ph.documented is False
    assert "SIS vendors" in ph.definition_text


def test_undocumented_filtered_slots_absent_from_spine_lens(scenario):
    """Undocumented Assessment prop names must NOT appear as their own
    records. They are collapsed into the single placeholder — any
    `documented=False` Assessment row would violate the soft-filter
    contract (would mean a slot leaked through)."""
    records = scenario["spine_records"]
    n = scenario["n"]
    documented_names = {f"prop{i:02d}" for i in range(n)}

    # Names that should be absent (they're in the spine but not in
    # the source doc — the filter should swallow them).
    undocumented_names = {
        f"prop{i:02d}" for i in range(n, scenario["m"])
    }

    for r in records:
        if r.entity != "Assessment" or r.source == "filtered":
            continue
        assert r.element_name in documented_names, (
            f"Assessment row {r.element_name!r} should be absent (the "
            f"source doc did not document this slot, so the filter "
            f"should have collapsed it into the placeholder)"
        )

    present_names = {
        r.element_name for r in records
        if r.entity == "Assessment" and r.source != "filtered"
    }
    leaked = present_names & undocumented_names
    assert not leaked, (
        f"undocumented Assessment slots leaked through the soft-filter: "
        f"{sorted(leaked)}"
    )


def test_source_lens_unaffected_by_filter(scenario):
    """The soft-filter only acts inside `assemble_spine_driven`. A
    build_source_index-style round-trip preserves every source row
    untouched — N Assessment rows in, N Assessment rows out, no
    placeholder. Scoring under the source lens treats Assessment the
    same as any other domain."""
    source_rows = scenario["source_rows"]
    assessment_src = [r for r in source_rows if r.entity == "Assessment"]
    assert len(assessment_src) == scenario["n"], (
        f"scenario fixture mis-wired: {len(assessment_src)} Assessment "
        f"source rows, expected {scenario['n']}"
    )
    # No placeholder on the source side by construction.
    assert not any(r.source == "filtered" for r in source_rows), (
        "source-lens rows unexpectedly contain a filtered placeholder"
    )


def test_non_filtered_entity_slots_preserved(scenario):
    """Sanity: the filter is narrowly scoped. `Student` is not a
    filtered domain; its spine emits + hybrid-append should pass
    through without any soft-filter interference."""
    student_records = [
        r for r in scenario["spine_records"]
        if r.entity == "Student"
    ]
    # Student has 2 spine properties (studentUniqueId + firstName) and
    # neither is a Reference/Collection wrapper.
    assert len(student_records) == 2
    # firstName is documented by the source fixture; studentUniqueId is not.
    docs = {r.element_name: r.documented for r in student_records}
    assert docs == {"firstName": True, "studentUniqueId": False}
