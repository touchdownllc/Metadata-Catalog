"""Swagger-as-source backfill (issue #70 + issue #147).

Two cohorts share this module:

1. **Entity-level backfill (issue #70).** When a state's primary source
   document (Confluence / Matrix / XLSX / TWEDS) is silent on a whole
   entity that the state's live Ed-Fi swagger publishes, treat the
   swagger publication itself as a form of state documentation. Rows
   carry ``documentation_source="swagger"``.

2. **Leaf-level cross-lens borrow (issue #147).** When the source doc
   covers the parent entity but doesn't enumerate a sub-collection /
   sub-entity leaf the canonical Ed-Fi data model carries (e.g. AZ
   ``Address.streetNumberName``, WI ``Student/Locals.*``), pull the
   spine-lens row in so the keymap-join in
   ``human_score_backfill`` can find a sidecar match for the
   reviewer-named leaf. Rows carry ``documentation_source="swagger_leaf"``.
   Methodology approved 2026-05-03 by Doug + Maria; see ADR 0004.

Methodology stance (shared):

- Use the same NACHOS rubric for backfilled rows; no tier cap. They
  score low naturally (definition thin, no business-rules text, no
  regulatory citations) — that's honest signal.
- Apply the same SIS-never-populated domain filter (Assessment / Survey /
  Standards / Gradebook / Intervention) as the spine-lens placeholder
  collapse. SIS-vendor posture doesn't change because the state ships
  these in swagger.
- Distinguish provenance via ``ElementRecord.documentation_source`` —
  three values (``"source_doc"`` / ``"swagger"`` / ``"swagger_leaf"``).
- v21 close-out posture: rows surface in the source-lens artifact for
  workbook + reviewer-comparison visibility but carry
  ``documented=False`` so headline NACHOS aggregates stay restricted to
  state-authored prose.

This module mutates ``data/out/{state}_elements_source.json`` in place
(appends backfill rows). The spine-lens artifact is untouched under
v21 (not read, not written). Adapter ``run()`` calls ``backfill(state)``
after writing both lens artifacts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from src.ingest.domain_filter import entity_filter_domain
from src.ingest.shared import canonical_spine_emit_keys, stamp_edfi_domains
from src.models.element import ElementRecord, StateElements
from src.models.spine import StateSpine
from src.utils.matching import entity_match_form, record_match_keys
from src.utils.paths import state_elements_path, state_spine_path

logger = logging.getLogger(__name__)

# Reference / Collection wrapper rows are dropped from spine-lens scoring;
# match that posture for swagger-backfill so we don't inflate the surface
# with rows nothing scores against.
_NON_SCORING_DATA_TYPES: frozenset[str] = frozenset({"Reference", "Collection"})


def _documented_entity_set(elements: StateElements) -> set[str]:
    """Return the set of normalized entity names the source doc covers.

    A swagger-backfill row is appended only for entities whose normalized
    name does NOT appear in this set. Filtered-domain placeholder rows
    (``source='filtered'``) are excluded from the membership test —
    they're a synthetic spine-lens artifact, not real state coverage.
    """
    return {
        entity_match_form(r.entity)
        for r in elements.elements
        if r.source != "filtered"
    }


def _build_swagger_record(
    state: str,
    edfi_version: str,
    entity: str,
    element_name: str,
    data_type: str,
    record_source: str,
    extension_name: str | None,
    domain: str,
    description: str,
) -> ElementRecord:
    """Construct one swagger-backfilled ``ElementRecord``.

    ``definition_text`` and ``edfi_standard_definition`` both carry the
    spine property description so downstream scoring (which reads
    ``definition_text``) and spine-lens display (which reads
    ``edfi_standard_definition``) both see the same text. Empty narrative
    fields stay empty — that's the honest signal that no authored prose
    exists for this row.
    """
    return ElementRecord(
        state=state,
        edfi_version=edfi_version,
        # Issue #184: swagger backfill rows have no source-document area, so
        # `domain` (Source Area) is blank. The Ed-Fi domain lives in
        # `edfi_domain`; `collect_backfill_records` canonicalizes it to the
        # joined form via stamp_edfi_domains.
        domain="",
        edfi_domain=domain or None,
        entity=entity,
        element_name=element_name,
        data_type=data_type,
        source=record_source,
        extension_name=extension_name,
        documented=False,
        documentation_source="swagger",
        edfi_standard_definition=description or None,
        definition_text=description or "",
        business_rules_text=None,
        element_specific_rules=None,
        regulatory_citations=[],
        related_entities=[],
        descriptor_table_code=None,
        descriptor_table_values=[],
        collections_text=None,
        source_document=f"{state} Ed-Fi Swagger (vendored)",
        source_page_or_section=f"swagger:/{entity}/{element_name}",
    )


def collect_backfill_records(
    state: str,
    spine: StateSpine,
    documented_entities: set[str],
) -> list[ElementRecord]:
    """Walk the spine catalog and emit swagger-backfilled records for every
    spine slot whose entity is silent in the state's source doc.

    Skips:
    - SIS-never-populated domains via ``entity_filter_domain``.
    - Entities the source doc already enumerated.
    - Reference / Collection wrapper data types (consistent with the
      spine-lens scoring filter).

    Pure function — does not write to disk. Public so tests can drive
    it with synthetic spines.
    """
    documented_norm = {n for n in documented_entities}
    out: list[ElementRecord] = []
    seen: set[tuple[str, str]] = set()
    for emit in canonical_spine_emit_keys(spine):
        if entity_filter_domain(emit.entity, spine) is not None:
            continue
        if entity_match_form(emit.entity) in documented_norm:
            continue
        if emit.data_type in _NON_SCORING_DATA_TYPES:
            continue
        key = (emit.entity, emit.element_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            _build_swagger_record(
                state=state,
                edfi_version=spine.edfi_version,
                entity=emit.entity,
                element_name=emit.element_name,
                data_type=emit.data_type,
                record_source=emit.source,
                extension_name=emit.extension_name,
                domain=emit.domain,
                description=emit.description,
            )
        )
    # Issue #184: canonicalize edfi_domain to the joined-resolver form so
    # backfill rows match the rest of the artifact regardless of caller.
    stamp_edfi_domains(out, spine)
    return out


def _build_swagger_leaf_record(
    state: str,
    edfi_version: str,
    entity: str,
    element_name: str,
    data_type: str,
    record_source: str,
    extension_name: str | None,
    domain: str,
    description: str,
) -> ElementRecord:
    """Construct one leaf-level cross-lens-borrow ``ElementRecord``.

    Mirrors ``_build_swagger_record`` but tags the row
    ``documentation_source="swagger_leaf"`` and annotates
    ``source_page_or_section`` with a ``(leaf-borrow)`` suffix so the
    audit trail distinguishes the issue #147 cohort from the issue #70
    entity-level cohort. ``documented=False`` matches the v21 close-out
    posture (excluded from headline NACHOS aggregates; still appears in
    per-row sidecar so the keymap-join finds the row).
    """
    return ElementRecord(
        state=state,
        edfi_version=edfi_version,
        # Issue #184: leaf-borrow rows have no source-document area, so
        # `domain` (Source Area) is blank; the Ed-Fi domain lives in
        # `edfi_domain` (canonicalized by collect_leaf_backfill_records).
        domain="",
        edfi_domain=domain or None,
        entity=entity,
        element_name=element_name,
        data_type=data_type,
        source=record_source,
        extension_name=extension_name,
        documented=False,
        documentation_source="swagger_leaf",
        edfi_standard_definition=description or None,
        definition_text=description or "",
        business_rules_text=None,
        element_specific_rules=None,
        regulatory_citations=[],
        related_entities=[],
        descriptor_table_code=None,
        descriptor_table_values=[],
        collections_text=None,
        source_document=f"{state} Ed-Fi Swagger (vendored)",
        source_page_or_section=f"swagger:/{entity}/{element_name} (leaf-borrow)",
    )


def _source_lens_match_keys(elements: StateElements) -> set[tuple[str, str]]:
    """Build the alias-expanded match-key set for source-lens rows.

    Mirrors how ``assemble_spine_driven`` indexes source rows for spine
    enrichment: each row contributes the full ``record_match_keys``
    expansion (bare-prefix refs, EducationOrganization subtype expansion,
    sub-entity Pascal/camel/tail forms, MDE bare-form descriptors).
    Used by ``collect_leaf_backfill_records`` to decide whether a spine
    emit is already represented in source-lens under any alias.
    Filtered placeholder rows are excluded — they're synthetic.
    """
    out: set[tuple[str, str]] = set()
    for r in elements.elements:
        if r.source == "filtered":
            continue
        out |= record_match_keys(r.entity, r.element_name)
    return out


def _spine_subcollection_only_keys(
    spine: StateSpine,
) -> set[tuple[str, str]]:
    """Return ``(entity, element_name)`` pairs that are sub-collection /
    sub-entity leaves in the spine and NOT also direct properties of
    the same entity.

    Issue #147 narrowing (Plan B): the cross-lens borrow targets only
    the reviewer-named sub-collection / sub-entity navigation cohort
    (`Address.*`, `Locals.*`, `services.*`, `OtherName.*`, etc.), NOT
    every missing property on a documented entity. The narrowing
    matters because canonical_spine_emit_keys flattens sub-entity
    leaves into the parent's namespace (`Student.addresses[].streetNumberName`
    becomes `(Student, streetNumberName)`), so without this filter the
    borrow expands to ~16x the issue's stated cohort and dilutes the
    audit signal.

    The set is built per-entity:

    - ``direct``: properties declared directly on the entity (or on a
      state extension of that entity).
    - ``sub``: properties declared on any sub-collection of the entity
      (or any sub-collection of a state extension of the entity).

    Returned set is ``sub - direct``: leaves that exist ONLY through a
    sub-collection path, never as a direct property. References /
    reference key flattenings are excluded by construction (they go
    through ``_emit_reference_flattenings`` and contribute to neither
    set).
    """
    catalog = spine.catalog
    direct: set[tuple[str, str]] = set()
    sub: set[tuple[str, str]] = set()
    for entity_name, entry in catalog.entities.items():
        for prop_name in entry.properties.keys():
            direct.add((entity_name, prop_name))
        for sc in entry.sub_collections.values():
            for prop_name in sc.properties.keys():
                sub.add((entity_name, prop_name))
    for ext in catalog.extensions.values():
        target = ext.extends_entity
        for prop_name in ext.properties.keys():
            direct.add((target, prop_name))
        for sc in ext.sub_collections.values():
            for prop_name in sc.properties.keys():
                sub.add((target, prop_name))
    return sub - direct


def collect_leaf_backfill_records(
    state: str,
    spine: StateSpine,
    documented_entities: set[str],
    source_lens_keys: set[tuple[str, str]],
) -> list[ElementRecord]:
    """Walk the spine catalog and emit leaf-borrow records for spine slots
    whose parent entity IS documented but whose ``(entity, element)``
    pair is missing from source-lens (under any alias).

    Issue #147 cohort, narrowed (Plan B): only sub-collection /
    sub-entity leaves. Skip rules:
    - SIS-never-populated domains via ``entity_filter_domain``.
    - Entities the source doc has NOT enumerated (those go through the
      entity-level pass instead — see ``collect_backfill_records``).
    - Reference / Collection wrapper data types.
    - Spine emits whose alias expansion intersects ``source_lens_keys``
      (the leaf is already represented in source-lens under some alias).
    - Spine emits whose ``(entity, element_name)`` is NOT in the
      sub-collection-only set (direct properties + reference key
      flattenings stay out of the leaf-borrow cohort; see
      ``_spine_subcollection_only_keys``).

    Pure function — does not write to disk. Public so tests can drive
    it with synthetic spines.
    """
    documented_norm = {n for n in documented_entities}
    subcollection_keys = _spine_subcollection_only_keys(spine)
    out: list[ElementRecord] = []
    seen: set[tuple[str, str]] = set()
    for emit in canonical_spine_emit_keys(spine):
        if entity_filter_domain(emit.entity, spine) is not None:
            continue
        if entity_match_form(emit.entity) not in documented_norm:
            continue
        if emit.data_type in _NON_SCORING_DATA_TYPES:
            continue
        if (emit.entity, emit.element_name) not in subcollection_keys:
            continue
        emit_aliases = record_match_keys(emit.entity, emit.element_name)
        if emit_aliases & source_lens_keys:
            continue
        key = (emit.entity, emit.element_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            _build_swagger_leaf_record(
                state=state,
                edfi_version=spine.edfi_version,
                entity=emit.entity,
                element_name=emit.element_name,
                data_type=emit.data_type,
                record_source=emit.source,
                extension_name=emit.extension_name,
                domain=emit.domain,
                description=emit.description,
            )
        )
    # Issue #184: canonicalize edfi_domain (see collect_backfill_records).
    stamp_edfi_domains(out, spine)
    return out


def backfill(
    state: str,
    *,
    src_path: Path | None = None,
    spine_lens_path: Path | None = None,
    spine_path: Path | None = None,
) -> int:
    """Append swagger-backfill rows to source-lens. Returns the count of
    rows appended to source-lens (the count of new scoring positions).

    Path overrides exist so adapter ``run()`` can pass the same module-
    level path constants it wrote to (which tests monkey-patch via
    ``monkeypatch.setattr(state_module, "_STATE_ELEMENTS_OUT", ...)``);
    without that, the backfill would read/write the real
    ``data/out/...`` artifacts even under tmp-path-isolated tests.
    ``spine_lens_path`` is accepted for signature compatibility but the
    spine-lens artifact is neither read nor written under the v21
    close-out posture (issue #213 item 3 removed the load-and-discard).

    Side effects: rewrites the source-lens elements artifact at the
    resolved path.
    """
    state_u = state.upper()
    src_p = src_path or state_elements_path(state_u, "source")
    _ = spine_lens_path  # kept for caller compatibility; unused under v21
    spine_p = spine_path or state_spine_path(state_u)

    src_elements = StateElements.model_validate_json(
        src_p.read_text(encoding="utf-8")
    )
    spine = StateSpine.model_validate_json(spine_p.read_text(encoding="utf-8"))

    documented = _documented_entity_set(src_elements)
    entity_records = collect_backfill_records(state_u, spine, documented)
    # Issue #147: leaf-level cross-lens borrow. The source_lens_keys set
    # is built from the ORIGINAL src_elements (before entity-level append)
    # so the leaf pass doesn't see entity-level swagger rows as covered
    # source-lens rows — the leaves on those entities are already
    # enumerated through entity-level backfill and don't need leaf-borrow.
    source_lens_keys = _source_lens_match_keys(src_elements)
    leaf_records = collect_leaf_backfill_records(
        state_u, spine, documented, source_lens_keys
    )
    backfill_records = entity_records + leaf_records
    if not backfill_records:
        logger.info("[%s] swagger-backfill: no rows to append", state_u)
        return 0

    appended = src_elements.elements + backfill_records
    new_src = StateElements(
        state=src_elements.state,
        edfi_version=src_elements.edfi_version,
        extracted_at=datetime.now(timezone.utc),
        element_count=len(appended),
        elements=appended,
    )
    src_p.write_text(new_src.model_dump_json(indent=2), encoding="utf-8")

    # v21 middle path (issue #70 close-out posture): rows surface in the
    # source-lens artifact for workbook + reviewer-comparison visibility but
    # do NOT count as "documented". The spine-lens row stays
    # ``documented=False`` (its pre-v20 default) and aggregate excludes
    # ``documented=False`` rows from headline coverage / mean NACHOS. Same
    # posture for both ``"swagger"`` (entity-level, issue #70) and
    # ``"swagger_leaf"`` (leaf-level, issue #147). The spine-lens artifact
    # is deliberately untouched (and no longer even read).
    logger.info(
        "[%s] swagger-backfill: appended %d entity-level + %d leaf-level "
        "source-lens rows (documented=False; spine-lens unchanged)",
        state_u, len(entity_records), len(leaf_records),
    )
    return len(backfill_records)


def run(state: str) -> int:
    """CLI entry — run swagger-backfill for a single state.

    Plain function (NOT @click.command) per the CLI-wiring convention in
    CLAUDE.md / `tests/test_cli_e2e.py::TestCliWiring`. The Click command
    in `cli.py` calls this.
    """
    return backfill(state)
