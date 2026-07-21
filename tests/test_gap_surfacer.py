"""Tests for the spine-anchored gap surfacer (issue #66 Layer 2).

Verifies the surfacer's three core behaviors:

- ``spine_only_full_entity`` discovery for entities the source-doc
  doesn't enumerate but the spine carries (typically TEA / WI / MN
  extension entities).
- ``spine_within_documented_entity`` discovery for entities that DO
  appear in the source but are missing specific elements the spine
  knows.
- Source-lens contract preservation: the surfacer does not mutate
  ``{state}_elements_source.json``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingest import domain_scope
from src.ingest.gap_surfacer import (
    DISCOVERY_FULL_ENTITY,
    DISCOVERY_WITHIN,
    surface_gaps,
    write_gap_artifact,
)
from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
    ReferenceInfo,
    SubCollectionInfo,
)
from src.models.element import ElementRecord, StateElements
from src.models.spine import SpineSourceURLs, StateSpine
from tests.factories import make_record


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch):
    """Isolate gap-surfacer filter behavior from the live ``domain_scope``
    seed (which enables TX/IN Assessment). ``test_filtered_domain_skipped``
    builds a ``state='TX'`` spine and asserts Assessment is skipped."""
    monkeypatch.setattr(domain_scope, "DOMAIN_SOURCES", ())


def _spine(entities: dict, extensions: dict | None = None) -> StateSpine:
    catalog = EdFiCatalog(
        version="4.0",
        entity_count=len(entities),
        extension_count=len(extensions or {}),
        entities=entities,
        extensions=extensions or {},
    )
    return StateSpine(
        state="TX",
        edfi_version="4.0",
        fetched_at=datetime.now(tz=timezone.utc),
        source_urls=SpineSourceURLs(resources="http://test/resources.json"),
        catalog=catalog,
    )


def _record(entity: str, element_name: str) -> ElementRecord:
    return make_record(entity, element_name, state="TX")


def _state_elements(records: list[ElementRecord]) -> StateElements:
    return StateElements(
        state="TX",
        edfi_version="4.0",
        extracted_at=datetime.now(tz=timezone.utc),
        element_count=len(records),
        elements=records,
    )


def _write_fixture(tmp_path: Path, elements: StateElements, spine: StateSpine):
    elements_path = tmp_path / "tx_elements_source.json"
    spine_path = tmp_path / "tx_spine.json"
    elements_path.write_text(elements.model_dump_json(indent=2))
    spine_path.write_text(spine.model_dump_json(indent=2))
    return elements_path, spine_path


def test_spine_only_full_entity_emits_for_missing_extension(tmp_path):
    """A spine extension entity not enumerated in source surfaces every prop
    as ``spine_only_full_entity``. Models the TX `tx_priorYearLeaver` case."""
    spine = _spine(
        entities={"Calendar": EntityEntry(properties={"calendarCode": PropertyInfo()})},
        extensions={
            "tx_priorYearLeaver": ExtensionEntry(
                extends_entity="PriorYearLeaver",  # entity not in source
                source_prefix="tx",
                properties={
                    "studentUId": PropertyInfo(type="string"),
                    "exitWithdrawDate": PropertyInfo(type="string", format="date"),
                },
            ),
        },
    )
    elements = _state_elements([_record("Calendar", "calendarCode")])
    ep, sp = _write_fixture(tmp_path, elements, spine)

    gaps = surface_gaps("TX", elements_path=ep, spine_path=sp)

    pyl_gaps = [g for g in gaps if g["entity"] == "PriorYearLeaver"]
    assert len(pyl_gaps) == 2
    assert all(g["discovery"] == DISCOVERY_FULL_ENTITY for g in pyl_gaps)
    assert all(g["spine_extension_name"] == "tx_priorYearLeaver" for g in pyl_gaps)
    types = {g["element_name"]: g["spine_data_type"] for g in pyl_gaps}
    assert types["studentUId"] == "String"
    assert types["exitWithdrawDate"] == "Date"


def test_spine_within_documented_entity_for_missing_field(tmp_path):
    """When the source documents the entity but not a specific spine field,
    the gap row classifies as ``spine_within_documented_entity``. Models WI
    Student fields like `personReference` / `preferredFirstName`."""
    spine = _spine(
        entities={
            "Student": EntityEntry(
                properties={
                    "studentUniqueId": PropertyInfo(),
                    "preferredFirstName": PropertyInfo(),
                    "maidenName": PropertyInfo(),
                }
            )
        },
    )
    # Source documents Student/studentUniqueId but not the other two.
    elements = _state_elements([_record("Student", "studentUniqueId")])
    ep, sp = _write_fixture(tmp_path, elements, spine)

    gaps = surface_gaps("TX", elements_path=ep, spine_path=sp)

    student_gaps = sorted(g["element_name"] for g in gaps if g["entity"] == "Student")
    assert student_gaps == ["maidenName", "preferredFirstName"]
    assert all(
        g["discovery"] == DISCOVERY_WITHIN
        for g in gaps
        if g["entity"] == "Student"
    )


def test_documented_property_does_not_emit_gap(tmp_path):
    """Source documents `Calendar/calendarCode`; gap surfacer must not emit
    a row for that primary. Guards the join symmetry — false positives
    on documented elements are the surfacer's primary failure mode."""
    spine = _spine(
        entities={"Calendar": EntityEntry(properties={"calendarCode": PropertyInfo()})},
    )
    elements = _state_elements([_record("Calendar", "calendarCode")])
    ep, sp = _write_fixture(tmp_path, elements, spine)

    gaps = surface_gaps("TX", elements_path=ep, spine_path=sp)
    assert all(
        g["element_name"] != "calendarCode" for g in gaps if g["entity"] == "Calendar"
    )


def test_sub_collection_concat_form_emitted(tmp_path):
    """Sub-collection properties are emitted using the source-side concat
    form (parent + leaf) so the gap matches the source-adapter convention.
    Records ``sub_collection`` + ``leaf_name`` so the reviewer-comparison
    lookup can register both concat and bare-leaf aliases."""
    spine = _spine(
        entities={
            "Calendar": EntityEntry(
                properties={"calendarCode": PropertyInfo()},
                sub_collections={
                    "gradeLevels": SubCollectionInfo(
                        sub_entity="CalendarGradeLevel",
                        properties={"gradeLevelDescriptor": PropertyInfo()},
                    )
                },
            )
        },
    )
    # Source documents only the Calendar primary key, not the sub-collection.
    elements = _state_elements([_record("Calendar", "calendarCode")])
    ep, sp = _write_fixture(tmp_path, elements, spine)

    gaps = surface_gaps("TX", elements_path=ep, spine_path=sp)

    sub_gaps = [g for g in gaps if g["element_name"] == "gradeLevelsGradeLevelDescriptor"]
    assert len(sub_gaps) == 1
    g = sub_gaps[0]
    assert g["sub_collection"] == "gradeLevels"
    assert g["leaf_name"] == "gradeLevelDescriptor"


def test_sub_collection_documented_via_concat_no_gap(tmp_path):
    """Source emits the WI-style concat form ``gradeLevelsgradeLevelDescriptor``
    (no separator). The surfacer must recognize this as documenting the
    sub-collection property and NOT emit a gap row."""
    spine = _spine(
        entities={
            "Calendar": EntityEntry(
                sub_collections={
                    "gradeLevels": SubCollectionInfo(
                        sub_entity="CalendarGradeLevel",
                        properties={"gradeLevelDescriptor": PropertyInfo()},
                    )
                },
            )
        },
    )
    elements = _state_elements(
        [_record("Calendar", "gradeLevelsgradeLevelDescriptor")]
    )
    ep, sp = _write_fixture(tmp_path, elements, spine)

    gaps = surface_gaps("TX", elements_path=ep, spine_path=sp)
    assert not [g for g in gaps if g["entity"] == "Calendar"]


def test_reference_documented_by_fk_leaf_no_gap(tmp_path):
    """Source documents ``Calendar/schoolId`` (the FK leaf); spine has
    ``Calendar.schoolReference`` with ``schoolId`` as a key property. The
    leaf form satisfies the reference, so no gap row for the reference."""
    spine = _spine(
        entities={
            "Calendar": EntityEntry(
                references={
                    "schoolReference": ReferenceInfo(
                        entity="School",
                        key_properties={"schoolId": PropertyInfo()},
                    )
                }
            )
        },
    )
    elements = _state_elements([_record("Calendar", "schoolId")])
    ep, sp = _write_fixture(tmp_path, elements, spine)

    gaps = surface_gaps("TX", elements_path=ep, spine_path=sp)
    assert not [g for g in gaps if g["entity"] == "Calendar"]


def test_filtered_domain_skipped(tmp_path):
    """Entities whose domains are SIS-never-populated (Assessment / Survey /
    Standards / Gradebook / Intervention) are skipped — those are filtered
    out of the spine-lens placeholder layer too, so flagging them as gaps
    would inflate the artifact with non-actionable noise."""
    spine = _spine(
        entities={
            # Assessment is in FILTERED_DOMAINS via the `_FALLBACK_PREFIXES`
            # match on the entity name prefix.
            "Assessment": EntityEntry(
                properties={"assessmentIdentifier": PropertyInfo()}
            )
        },
    )
    elements = _state_elements([])
    ep, sp = _write_fixture(tmp_path, elements, spine)

    gaps = surface_gaps("TX", elements_path=ep, spine_path=sp)
    assert all(g["entity"] != "Assessment" for g in gaps)


def test_source_lens_artifact_byte_unchanged(tmp_path):
    """Methodology contract: writing the gap artifact must not modify the
    source-lens file. The surfacer is read-only on its inputs."""
    spine = _spine(
        entities={"Calendar": EntityEntry(properties={"calendarCode": PropertyInfo()})},
        extensions={
            "tx_priorYearLeaver": ExtensionEntry(
                extends_entity="PriorYearLeaver",
                source_prefix="tx",
                properties={"studentUId": PropertyInfo()},
            ),
        },
    )
    elements = _state_elements([_record("Calendar", "calendarCode")])
    ep, sp = _write_fixture(tmp_path, elements, spine)
    pre_bytes = ep.read_bytes()

    surface_gaps("TX", elements_path=ep, spine_path=sp)

    assert ep.read_bytes() == pre_bytes


def test_write_artifact_envelope_shape(tmp_path):
    """``write_gap_artifact`` produces the expected JSON envelope:
    state / generated_at / gap_count / discovery_counts / gaps."""
    spine = _spine(
        entities={"Calendar": EntityEntry(properties={"calendarCode": PropertyInfo()})},
        extensions={
            "tx_priorYearLeaver": ExtensionEntry(
                extends_entity="PriorYearLeaver",
                source_prefix="tx",
                properties={"studentUId": PropertyInfo()},
            ),
        },
    )
    elements = _state_elements([_record("Calendar", "calendarCode")])
    ep, sp = _write_fixture(tmp_path, elements, spine)
    gaps = surface_gaps("TX", elements_path=ep, spine_path=sp)

    # Redirect output via monkey-patching the path helper through env-style
    # — easier: write via _emit_gap path directly.
    from src.utils import paths

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    orig = paths._OUT_DIR
    try:
        paths._OUT_DIR = out_dir
        out_path = write_gap_artifact("TX", gaps)
        payload = json.loads(out_path.read_text())
        assert payload["state"] == "TX"
        assert payload["gap_count"] == len(gaps)
        assert "spine_only_full_entity" in payload["discovery_counts"]
        assert payload["gaps"] == gaps
    finally:
        paths._OUT_DIR = orig


def test_deterministic_output_order(tmp_path):
    """Re-running the surfacer with the same inputs produces an identical
    record sequence — invariance is needed for golden-test stability and
    PYTHONHASHSEED-resistant CI."""
    spine = _spine(
        entities={
            "Calendar": EntityEntry(
                properties={"calendarCode": PropertyInfo(), "calendarName": PropertyInfo()}
            ),
            "Student": EntityEntry(
                properties={"studentUniqueId": PropertyInfo(), "firstName": PropertyInfo()}
            ),
        },
    )
    elements = _state_elements([])
    ep, sp = _write_fixture(tmp_path, elements, spine)

    g1 = surface_gaps("TX", elements_path=ep, spine_path=sp)
    g2 = surface_gaps("TX", elements_path=ep, spine_path=sp)
    assert [(g["entity"], g["element_name"]) for g in g1] == [
        (g["entity"], g["element_name"]) for g in g2
    ]
