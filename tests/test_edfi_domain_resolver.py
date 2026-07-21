"""Issue #184 — canonical `edfi_domain_for_entity` resolver + the
prompt-label decoupling that keeps the extraction cache warm.

`edfi_domain_for_entity` is the single source of truth that unified the
two resolvers which previously disagreed: `shared._entity_domain` (single
`domains[0]`, with a CamelCase-sibling fallback) and the workbook-side
`analyst._edfi_domain_for` (joined, no sibling fallback). The canonical
form is the **superset**: joined `"; ".join(domains)` AND the full 3-step
fallback (direct -> longest-prefix parent -> CamelCase sibling).
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.ingest.shared import _entity_domain, edfi_domain_for_entity
from src.models.edfi_catalog import EdFiCatalog, EntityEntry, PropertyInfo
from src.models.element import ElementRecord
from src.models.spine import SpineSourceURLs, StateSpine
from src.score.extract import _prompt_domain_label
from tests.factories import make_record


def _spine() -> StateSpine:
    """Synthetic spine exercising every resolver branch.

    - ``Assessment``: multi-domain (joined-form coverage).
    - ``Student``: single-domain (prefix-parent fallback source).
    - ``EducationOrganizationNetwork``: concrete sibling of the abstract
      base ``EducationOrganization`` (sibling-fallback source).
    """
    assessment = EntityEntry(
        description="Assessment",
        domains=["Assessment", "AssessmentMetadata"],
        properties={
            "assessmentTitle": PropertyInfo(description="t", type="string"),
        },
    )
    student = EntityEntry(
        description="Student",
        domains=["Student Enrollment"],
        properties={
            "studentUniqueId": PropertyInfo(
                description="id", type="string", is_identity=True
            ),
        },
    )
    eon = EntityEntry(
        description="EducationOrganizationNetwork",
        domains=["Education Organization"],
        properties={
            "networkPurpose": PropertyInfo(description="p", type="string"),
        },
    )
    catalog = EdFiCatalog(
        version="4.0.0",
        entity_count=3,
        extension_count=0,
        entities={
            "Assessment": assessment,
            "Student": student,
            "EducationOrganizationNetwork": eon,
        },
        extensions={},
    )
    return StateSpine(
        state="AZ",
        edfi_version="4.0.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(resources="https://example/resources.json"),
        catalog=catalog,
    )


class TestEdfiDomainForEntity:
    def test_direct_multi_domain_joined(self) -> None:
        assert edfi_domain_for_entity("Assessment", _spine()) == (
            "Assessment; AssessmentMetadata"
        )

    def test_direct_single_domain(self) -> None:
        assert edfi_domain_for_entity("Student", _spine()) == "Student Enrollment"

    def test_longest_prefix_parent_fallback(self) -> None:
        # `StudentAddress` is not in the catalog; it inherits `Student`'s
        # domain via the CamelCase prefix-parent rule.
        assert edfi_domain_for_entity("StudentAddress", _spine()) == (
            "Student Enrollment"
        )

    def test_camelcase_sibling_fallback(self) -> None:
        # The abstract base `EducationOrganization` is never materialized,
        # but its concrete sibling carries the domain.
        assert edfi_domain_for_entity("EducationOrganization", _spine()) == (
            "Education Organization"
        )

    def test_none_when_unresolvable(self) -> None:
        assert edfi_domain_for_entity("ZzzUnknown", _spine()) is None

    def test_superset_of_entity_domain(self) -> None:
        # The canonical resolver's first domain matches the legacy single
        # resolver for a multi-domain entity (joined[0] == _entity_domain).
        spine = _spine()
        joined = edfi_domain_for_entity("Assessment", spine)
        assert joined is not None
        assert joined.split("; ")[0] == _entity_domain("Assessment", spine)


def _rec(entity: str, source: str, domain: str) -> ElementRecord:
    return make_record(
        entity, "x", state="AZ", edfi_version="4.0.0", domain=domain, source=source
    )


class TestPromptDomainLabel:
    """The extraction prompt's `{domain}` slot must stay byte-identical to
    its pre-#184 value so the cache stays warm. For spine-lens core/
    extension batches that means the legacy `_entity_domain` (single)
    label, NOT the new joined `edfi_domain` and NOT the cleaned Source
    Area in `domain`."""

    def test_spine_core_uses_entity_domain_single(self) -> None:
        spine = _spine()
        # Post-#184 the record's `domain` is the Source Area (""), but the
        # prompt label must reconstruct the legacy single Ed-Fi domain.
        label = _prompt_domain_label([_rec("Assessment", "core", "")], "spine", spine)
        assert label == _entity_domain("Assessment", spine) == "Assessment"

    def test_spine_extension_uses_entity_domain(self) -> None:
        spine = _spine()
        label = _prompt_domain_label(
            [_rec("Student", "extension", "")], "spine", spine
        )
        assert label == "Student Enrollment"

    def test_spine_unknown_falls_back_to_record_domain(self) -> None:
        # Hybrid-append rows keep their Source Area in `domain`; the label
        # falls back to it (None -> render_prompt uses first.domain).
        spine = _spine()
        assert _prompt_domain_label(
            [_rec("WeirdEntity", "unknown", "Some Area")], "spine", spine
        ) is None

    def test_source_lens_falls_back_to_record_domain(self) -> None:
        spine = _spine()
        assert _prompt_domain_label(
            [_rec("Assessment", "core", "MySheet")], "source", spine
        ) is None

    def test_no_spine_falls_back(self) -> None:
        assert _prompt_domain_label(
            [_rec("Assessment", "core", "")], "spine", None
        ) is None
