"""Tests for the swagger-as-source backfill module (issue #70).

Covers the three core behaviors:

- Source-lens append: spine-only entities (entities the state's source
  doc is silent on) get one synthetic record per scalar / FK key /
  sub-collection element. Reference and Collection wrappers are skipped
  (consistent with the spine-lens scoring filter).
- Domain filter: SIS-never-populated domains (Assessment / Survey /
  Standards / Gradebook / Intervention) stay out of backfill.
- Spine-lens posture (v21 close-out): the existing spine-lens rows for
  backfilled entities are left at ``documented=False`` — the v20 flip is
  not performed under v21's middle-path scoring posture. Per-row scoring
  still runs against swagger rows so the sidecar carries their tier; the
  ``aggregate.run`` headline filters them out via
  ``documentation_source=="source_doc"``.

Also exercises the gap surfacer's "swagger-only entity" skip path so a
backfilled entity does not show up as ``spine_only_full_entity`` (or as
``spine_within_documented_entity``) in the gap artifact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.ingest import domain_scope
from src.ingest.gap_surfacer import surface_gaps
from src.ingest.swagger_backfill import (
    backfill,
    collect_backfill_records,
    collect_leaf_backfill_records,
)
from src.utils.matching import record_match_keys
from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
    ReferenceInfo,
    SubCollectionInfo,
)
from src.models.element import ElementRecord, StateElements


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch):
    """Isolate swagger-backfill filter behavior from the live ``domain_scope``
    seed (which enables TX/IN Assessment). The ``test_skips_filtered_domain*``
    tests build ``state='TX'`` spines and assert Assessment is skipped."""
    monkeypatch.setattr(domain_scope, "DOMAIN_SOURCES", ())
from src.models.spine import SpineSourceURLs, StateSpine


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


def _record(
    entity: str,
    element_name: str,
    *,
    source: str = "core",
    documented: bool = True,
    documentation_source: str = "source_doc",
    edfi_standard_definition: str | None = None,
) -> ElementRecord:
    return ElementRecord(
        state="TX",
        edfi_version="4.0",
        domain="X",
        entity=entity,
        element_name=element_name,
        definition_text="",
        source=source,
        documented=documented,
        documentation_source=documentation_source,
        edfi_standard_definition=edfi_standard_definition,
    )


def _state_elements(records: list[ElementRecord]) -> StateElements:
    return StateElements(
        state="TX",
        edfi_version="4.0",
        extracted_at=datetime.now(tz=timezone.utc),
        element_count=len(records),
        elements=records,
    )


def _write_lens_artifacts(
    tmp_path: Path,
    src_records: list[ElementRecord],
    spine_records: list[ElementRecord],
    spine: StateSpine,
) -> tuple[Path, Path, Path]:
    src_path = tmp_path / "tx_elements_source.json"
    spine_lens_path = tmp_path / "tx_elements_spine.json"
    spine_path = tmp_path / "tx_spine.json"
    src_path.write_text(_state_elements(src_records).model_dump_json(indent=2))
    spine_lens_path.write_text(
        _state_elements(spine_records).model_dump_json(indent=2)
    )
    spine_path.write_text(spine.model_dump_json(indent=2))
    return src_path, spine_lens_path, spine_path


class TestCollectBackfillRecords:
    def test_skips_documented_entities(self):
        """Entities the source doc covers (any single row counts) get no
        backfill rows — only entities entirely absent from source-lens
        qualify under the issue #70 stance."""
        spine = _spine(
            entities={
                "Calendar": EntityEntry(
                    properties={"calendarCode": PropertyInfo(description="cal")}
                ),
                "PriorYearLeaver": EntityEntry(
                    properties={"diplomaType": PropertyInfo(description="dt")}
                ),
            }
        )
        # documented_entities is the set of normalized (lowercased) entity
        # names — the production caller computes this from source-lens
        # records via `_documented_entity_set`.
        recs = collect_backfill_records(
            "TX", spine, documented_entities={"calendar"}
        )
        # Only PriorYearLeaver — Calendar is already in source.
        assert {r.entity for r in recs} == {"PriorYearLeaver"}

    def test_skips_filtered_domain_entities(self):
        """SIS-never-populated domains (Assessment / Survey / etc.) stay
        out of swagger backfill — same posture as the spine-lens
        placeholder collapse."""
        spine = _spine(
            entities={
                "Assessment": EntityEntry(
                    domains=["Assessment"],
                    properties={"assessmentTitle": PropertyInfo(description="t")},
                ),
                "PriorYearLeaver": EntityEntry(
                    properties={"diplomaType": PropertyInfo(description="dt")}
                ),
            }
        )
        recs = collect_backfill_records(
            "TX", spine, documented_entities=set()
        )
        assert {r.entity for r in recs} == {"PriorYearLeaver"}

    def test_skips_reference_and_collection_wrappers(self):
        """Reference and Collection wrapper rows are excluded — they're
        dropped from spine-lens scoring, so emitting them in the
        source-lens backfill would inflate the surface with rows that
        nothing scores against."""
        spine = _spine(
            entities={
                "PriorYearLeaver": EntityEntry(
                    properties={"diplomaType": PropertyInfo(description="dt")},
                    references={
                        "studentReference": ReferenceInfo(
                            entity="Student",
                            key_properties={
                                "studentUniqueId": PropertyInfo(description="sid"),
                            },
                        ),
                    },
                    sub_collections={
                        "addresses": SubCollectionInfo(
                            sub_entity="Address",
                            properties={"city": PropertyInfo(description="c")},
                        ),
                    },
                )
            }
        )
        recs = collect_backfill_records(
            "TX", spine, documented_entities=set()
        )
        types = sorted({r.data_type for r in recs})
        # Reference and Collection wrappers excluded; scalar + FK key
        # property + sub-collection leaf survive.
        assert "Reference" not in types
        assert "Collection" not in types
        # Each surviving record carries documentation_source=swagger.
        assert all(r.documentation_source == "swagger" for r in recs)

    def test_extension_entity_attribution(self):
        """Extension-only entities (no core counterpart) get backfill rows
        attributed to the extension's source_prefix — TEA tx_* extensions
        are the headline case."""
        spine = _spine(
            entities={},
            extensions={
                "tx_priorYearLeaver": ExtensionEntry(
                    extends_entity="PriorYearLeaver",
                    source_prefix="tx",
                    properties={
                        "diplomaType": PropertyInfo(description="dt"),
                    },
                ),
            },
        )
        recs = collect_backfill_records(
            "TX", spine, documented_entities=set()
        )
        assert len(recs) == 1
        r = recs[0]
        assert r.entity == "PriorYearLeaver"
        assert r.source == "extension"
        assert r.extension_name == "tx_priorYearLeaver"
        assert r.documentation_source == "swagger"


class TestBackfillIntegration:
    def test_appends_source_lens_without_flipping_spine_lens(self, tmp_path):
        """v21 close-out posture (issue #70 middle path): source-lens grows
        by the backfill count with rows tagged ``documentation_source=
        "swagger"`` and ``documented=False``; the spine-lens row for the
        same (entity, element) is left untouched (no flip).

        Per-row scoring still runs for swagger rows (so the sidecar carries
        their tier for reviewer-comparison alignment), but headline
        aggregates filter them out — see ``aggregate.run`` for the
        ``documentation_source=="source_doc"`` gate.
        """
        spine = _spine(
            entities={
                "Calendar": EntityEntry(
                    properties={"calendarCode": PropertyInfo(description="cal")}
                ),
                "PriorYearLeaver": EntityEntry(
                    properties={
                        "diplomaType": PropertyInfo(description="diploma"),
                    }
                ),
            }
        )
        src = [_record("Calendar", "calendarCode")]
        spine_lens = [
            _record("Calendar", "calendarCode"),
            _record(
                "PriorYearLeaver",
                "diplomaType",
                documented=False,
                edfi_standard_definition="diploma",
            ),
        ]
        src_path, spine_lens_path, spine_path = _write_lens_artifacts(
            tmp_path, src, spine_lens, spine
        )

        added = backfill(
            "TX",
            src_path=src_path,
            spine_lens_path=spine_lens_path,
            spine_path=spine_path,
        )
        assert added == 1

        # Source-lens carries the backfill row tagged swagger but
        # NOT counted as documented under v21.
        new_src = StateElements.model_validate_json(
            src_path.read_text(encoding="utf-8")
        )
        swagger_rows = [
            r for r in new_src.elements
            if r.documentation_source == "swagger"
        ]
        assert len(swagger_rows) == 1
        assert swagger_rows[0].entity == "PriorYearLeaver"
        assert swagger_rows[0].element_name == "diplomaType"
        assert swagger_rows[0].definition_text == "diploma"
        assert swagger_rows[0].documented is False
        assert swagger_rows[0].source_document == "TX Ed-Fi Swagger (vendored)"

        # Spine-lens row for the same (entity, element) is left at its
        # pre-backfill state — under v21 the spine flip is not performed.
        new_spine = StateElements.model_validate_json(
            spine_lens_path.read_text(encoding="utf-8")
        )
        spine_row = [
            r for r in new_spine.elements
            if r.entity == "PriorYearLeaver" and r.element_name == "diplomaType"
        ][0]
        assert spine_row.documented is False
        # documentation_source on the spine row stays at its
        # pre-backfill default (``"source_doc"``) — the v20 flip would
        # have set it to ``"swagger"``; v21 leaves the spine row alone.
        assert spine_row.documentation_source == "source_doc"

    def test_no_backfill_when_source_covers_everything(self, tmp_path):
        """If every spine entity has at least one source-lens row, the
        backfill is a no-op — the methodology only promotes ``spine_only_
        full_entity`` rows, never within-entity gaps."""
        spine = _spine(
            entities={
                "Calendar": EntityEntry(
                    properties={"calendarCode": PropertyInfo(description="cal")}
                ),
            }
        )
        src = [_record("Calendar", "calendarCode")]
        spine_lens = [_record("Calendar", "calendarCode")]
        src_path, spine_lens_path, spine_path = _write_lens_artifacts(
            tmp_path, src, spine_lens, spine
        )
        added = backfill(
            "TX",
            src_path=src_path,
            spine_lens_path=spine_lens_path,
            spine_path=spine_path,
        )
        assert added == 0


class TestLeafBackfill:
    """Issue #147 leaf-level cross-lens borrow (Plan B narrowed scope).

    Walks the spine catalog for entities the source doc DOES cover and
    emits records for sub-collection / sub-entity leaves missing from
    source-lens — NOT direct properties or reference-key flattenings.
    The narrowing matches the reviewer-named cohort patterns
    (``Address.*``, ``Locals.*``, ``services.*``, ``OtherName.*``,
    etc.) the issue describes; without it the borrow expands ~16x to
    cover every missing property on a documented entity.

    Posture mirrors v21 swagger backfill: rows tagged
    ``documented=False`` + ``documentation_source="swagger_leaf"`` so
    headline NACHOS aggregates stay restricted to authored prose while
    the keymap-join in ``human_score_backfill`` finds a sidecar match.
    """

    def test_emits_records_for_missing_subcollection_leaves(self):
        """Sub-collection leaves missing from source-lens are emitted —
        canonical Ed-Fi sub-collection / sub-entity navigation cohort
        per issue #147 (``Address.*``, ``Locals.*``, etc.)."""
        spine = _spine(
            entities={
                "Student": EntityEntry(
                    properties={
                        "studentUniqueId": PropertyInfo(description="sid"),
                    },
                    sub_collections={
                        "addresses": SubCollectionInfo(
                            sub_entity="StudentAddress",
                            properties={
                                "streetNumberName": PropertyInfo(description="street"),
                                "city": PropertyInfo(description="city"),
                                "stateAbbreviationDescriptor": PropertyInfo(
                                    description="state"
                                ),
                            },
                        ),
                    },
                ),
            }
        )
        # Source-lens covers the parent identity + one address leaf;
        # city + stateAbbreviationDescriptor should be borrowed.
        source_lens_keys = (
            record_match_keys("Student", "studentUniqueId")
            | record_match_keys("Student", "streetNumberName")
        )
        recs = collect_leaf_backfill_records(
            "TX",
            spine,
            documented_entities={"student"},
            source_lens_keys=source_lens_keys,
        )
        assert {(r.entity, r.element_name) for r in recs} == {
            ("Student", "city"),
            ("Student", "stateAbbreviationDescriptor"),
        }

    def test_skips_direct_properties_under_plan_b_narrowing(self):
        """Plan B narrowing: direct properties of documented entities
        are NOT in the leaf-borrow cohort even when missing from
        source-lens. Only sub-collection leaves qualify."""
        spine = _spine(
            entities={
                "Student": EntityEntry(
                    properties={
                        "lastName": PropertyInfo(description="last"),
                        "firstName": PropertyInfo(description="first"),
                    }
                ),
            }
        )
        # Source-lens covers only lastName. Without Plan B the borrow
        # would emit firstName; with Plan B it stays out (firstName is
        # a direct property, not a sub-collection leaf).
        recs = collect_leaf_backfill_records(
            "TX",
            spine,
            documented_entities={"student"},
            source_lens_keys=record_match_keys("Student", "lastName"),
        )
        assert recs == []

    def test_skips_reference_key_flattenings_under_plan_b_narrowing(self):
        """Plan B narrowing: reference-key flattenings (e.g. ``Calendar.
        schoolId`` from ``schoolReference``) are NOT in the leaf-borrow
        cohort. They go through ``_emit_reference_flattenings`` and
        contribute to neither direct nor sub_collection sets."""
        spine = _spine(
            entities={
                "Calendar": EntityEntry(
                    properties={
                        "calendarCode": PropertyInfo(description="cc"),
                    },
                    references={
                        "schoolReference": ReferenceInfo(
                            entity="School",
                            key_properties={
                                "schoolId": PropertyInfo(description="sid"),
                            },
                        ),
                    },
                ),
            }
        )
        recs = collect_leaf_backfill_records(
            "TX",
            spine,
            documented_entities={"calendar"},
            source_lens_keys=record_match_keys("Calendar", "calendarCode"),
        )
        # schoolId is a reference flattening, not a sub-collection leaf
        # → excluded under Plan B.
        assert recs == []

    def test_uses_swagger_leaf_documentation_source(self):
        spine = _spine(
            entities={
                "Student": EntityEntry(
                    sub_collections={
                        "otherNames": SubCollectionInfo(
                            sub_entity="StudentOtherName",
                            properties={
                                "firstName": PropertyInfo(description="first"),
                            },
                        ),
                    },
                ),
            }
        )
        recs = collect_leaf_backfill_records(
            "TX",
            spine,
            documented_entities={"student"},
            source_lens_keys=set(),
        )
        assert len(recs) == 1
        r = recs[0]
        assert r.documentation_source == "swagger_leaf"
        assert r.documented is False
        # source_page_or_section carries the (leaf-borrow) marker for
        # audit-trail clarity vs the issue #70 entity-level cohort.
        assert "(leaf-borrow)" in (r.source_page_or_section or "")

    def test_skips_leaves_already_in_source_lens(self):
        spine = _spine(
            entities={
                "Student": EntityEntry(
                    sub_collections={
                        "addresses": SubCollectionInfo(
                            sub_entity="StudentAddress",
                            properties={
                                "city": PropertyInfo(description="city"),
                                "streetNumberName": PropertyInfo(description="s"),
                            },
                        ),
                    },
                ),
            }
        )
        # Source-lens already covers BOTH leaves under canonical names.
        source_lens_keys = (
            record_match_keys("Student", "city")
            | record_match_keys("Student", "streetNumberName")
        )
        recs = collect_leaf_backfill_records(
            "TX",
            spine,
            documented_entities={"student"},
            source_lens_keys=source_lens_keys,
        )
        assert recs == []

    def test_skips_undocumented_entities(self):
        """Entities not in ``documented_entities`` go through the
        entity-level pass (``collect_backfill_records``); the leaf pass
        skips them so the two cohorts don't double-emit."""
        spine = _spine(
            entities={
                "PriorYearLeaver": EntityEntry(
                    sub_collections={
                        "diplomas": SubCollectionInfo(
                            sub_entity="PriorYearLeaverDiploma",
                            properties={
                                "diplomaType": PropertyInfo(description="dt"),
                            },
                        ),
                    },
                ),
            }
        )
        recs = collect_leaf_backfill_records(
            "TX",
            spine,
            documented_entities=set(),
            source_lens_keys=set(),
        )
        assert recs == []

    def test_skips_filtered_domain(self):
        """SIS-never-populated domains stay out of leaf backfill — same
        filter as the entity-level pass."""
        spine = _spine(
            entities={
                "Assessment": EntityEntry(
                    domains=["Assessment"],
                    sub_collections={
                        "scores": SubCollectionInfo(
                            sub_entity="AssessmentScore",
                            properties={
                                "scoreValue": PropertyInfo(description="v"),
                            },
                        ),
                    },
                ),
                "Student": EntityEntry(
                    sub_collections={
                        "addresses": SubCollectionInfo(
                            sub_entity="StudentAddress",
                            properties={
                                "city": PropertyInfo(description="city"),
                            },
                        ),
                    },
                ),
            }
        )
        recs = collect_leaf_backfill_records(
            "TX",
            spine,
            documented_entities={"assessment", "student"},
            source_lens_keys=set(),
        )
        entities_emitted = {r.entity for r in recs}
        assert "Assessment" not in entities_emitted
        assert "Student" in entities_emitted

    def test_backfill_combines_entity_and_leaf_passes(self, tmp_path):
        """Integration test: ``backfill()`` runs both passes; the
        source-lens artifact grows by both cohorts and rows carry the
        right ``documentation_source`` value per cohort."""
        spine = _spine(
            entities={
                # Documented entity with a sub-collection leaf → leaf pass.
                "Student": EntityEntry(
                    properties={
                        "studentUniqueId": PropertyInfo(description="sid"),
                    },
                    sub_collections={
                        "addresses": SubCollectionInfo(
                            sub_entity="StudentAddress",
                            properties={
                                "city": PropertyInfo(description="city"),
                            },
                        ),
                    },
                ),
                # Undocumented entity → entity pass.
                "PriorYearLeaver": EntityEntry(
                    properties={
                        "diplomaType": PropertyInfo(description="diploma"),
                    }
                ),
            }
        )
        src = [_record("Student", "studentUniqueId")]
        spine_lens = [
            _record("Student", "studentUniqueId"),
            _record(
                "Student",
                "city",
                documented=False,
                edfi_standard_definition="city",
            ),
            _record(
                "PriorYearLeaver",
                "diplomaType",
                documented=False,
                edfi_standard_definition="diploma",
            ),
        ]
        src_path, spine_lens_path, spine_path = _write_lens_artifacts(
            tmp_path, src, spine_lens, spine
        )

        added = backfill(
            "TX",
            src_path=src_path,
            spine_lens_path=spine_lens_path,
            spine_path=spine_path,
        )
        # 1 entity-level (PriorYearLeaver.diplomaType) + 1 leaf-level
        # (Student.city — sub-collection leaf, qualifies under Plan B).
        assert added == 2

        new_src = StateElements.model_validate_json(
            src_path.read_text(encoding="utf-8")
        )
        by_provenance = {
            r.documentation_source: r
            for r in new_src.elements
            if r.documentation_source != "source_doc"
        }
        assert "swagger" in by_provenance
        assert "swagger_leaf" in by_provenance
        # Entity-level row is on the undocumented entity.
        assert by_provenance["swagger"].entity == "PriorYearLeaver"
        # Leaf-level row is on the documented entity, sub-collection leaf.
        leaf_row = by_provenance["swagger_leaf"]
        assert leaf_row.entity == "Student"
        assert leaf_row.element_name == "city"
        assert leaf_row.documented is False
        assert leaf_row.definition_text == "city"

        # Spine-lens artifact unchanged (parallel to v21 swagger posture).
        new_spine = StateElements.model_validate_json(
            spine_lens_path.read_text(encoding="utf-8")
        )
        leaf_spine_row = next(
            r for r in new_spine.elements
            if r.entity == "Student" and r.element_name == "city"
        )
        assert leaf_spine_row.documented is False
        assert leaf_spine_row.documentation_source == "source_doc"


class TestGapSurfacerSkipsSwaggerEntities:
    def test_swagger_only_entity_skipped_from_gap_artifact(self, tmp_path):
        """A swagger-backfilled entity should not surface as
        ``spine_only_full_entity`` in the gap artifact — the backfill
        already claims the whole entity in the source-lens artifact."""
        spine = _spine(
            entities={
                "Calendar": EntityEntry(
                    properties={"calendarCode": PropertyInfo(description="cal")}
                ),
                "PriorYearLeaver": EntityEntry(
                    properties={
                        "diplomaType": PropertyInfo(description="diploma"),
                    }
                ),
            }
        )
        # Source-lens carries an authored row for Calendar plus a
        # swagger-backfill row for PriorYearLeaver.
        src = [
            _record("Calendar", "calendarCode"),
            _record(
                "PriorYearLeaver",
                "diplomaType",
                documentation_source="swagger",
            ),
        ]
        elements_path = tmp_path / "tx_elements_source.json"
        spine_path = tmp_path / "tx_spine.json"
        elements_path.write_text(_state_elements(src).model_dump_json(indent=2))
        spine_path.write_text(spine.model_dump_json(indent=2))

        gaps = surface_gaps(
            "TX", elements_path=elements_path, spine_path=spine_path
        )
        # PriorYearLeaver is fully claimed by swagger backfill — no gap
        # rows for it. Calendar is covered authentically — also no gaps.
        assert all(g["entity"] != "PriorYearLeaver" for g in gaps), (
            f"swagger-only entity leaked into gap artifact: {gaps}"
        )
