"""Texas (TX) ingestion — TWEDS source + spine enrichment.

Source-driven adapter, parallel to WI/MN/AZ: TWEDS v33 is the authoritative
source document and enumerates which (entity, element) rows exist. The
Ed-Fi swagger spine enriches — canonical names via
``attribute_record_source``, canonical types via
``populate_data_types_from_spine``, FK/descriptor alias resolution via the
shared matching machinery.

Pipeline (mirrors ``minnesota.run``):

1. Load spine (TX Ed-Fi 3 / local Docker ODS).
2. Load TWEDS scrape (``data/raw/tx/tweds/{entities,elements}_scraped.json``).
   Required — if the cache is absent and a live scrape fails, ingest fails
   loud (previous behavior was silent spine-only fallback).
3. Emit one ``ElementRecord`` per (entity, element) TEDS pair.
4. Unflatten concatenated sub-entity names back to their catalog parent.
5. Attribute ``source`` (core / extension / unknown) against the spine.
6. Apply canonical-type contract — matched rows get spine-derived
   ``data_type``; unresolved rows keep TEDS-verbatim type.
7. Post-unflatten dedup to collapse duplicate (entity, element_name) pairs
   (MN-style, not AZ-style).
8. Serialize ``tx_elements_source.json`` + ``tx_gap_log.json``.

CRITICAL: ``run()`` is a plain function, NOT ``@click.command()``-decorated
— see ``tests/test_ingest_tx.py::TestCliWiring``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.ingest import tx_tweds
from src.ingest.normalize import normalize_entity
from src.models.element import ElementRecord
from src.models.spine import StateSpine

logger = logging.getLogger(__name__)

from src.utils.paths import (
    state_elements_path,
    state_gap_log_path,
    state_spine_path,
)

_POC3_ROOT = Path(__file__).resolve().parents[2]
_TX_SPINE_PATH = state_spine_path("TX")
_TX_TWEDS_CACHE_DIR = _POC3_ROOT / "data" / "raw" / "tx" / "tweds"
_TX_ELEMENTS_OUT = state_elements_path("TX", "source")
_TX_ELEMENTS_SPINE_OUT = state_elements_path("TX", "spine")
_TX_GAP_OUT = state_gap_log_path("TX")


# -- TWEDS joining helpers ───────────────────────────────────────────────────


def _normalize_join_key(s: str) -> str:
    """Case-insensitive, whitespace-collapsed join key for TWEDS lookups."""
    return "".join(s.split()).lower() if s else ""


def _entity_alias_keys(entity_name: str) -> tuple[str, ...]:
    """Candidate normalized keys for a TWEDS entity name.

    TEA names many entities with a TWEDS-specific ``Ext`` suffix
    (``CourseTranscriptExt``, ``BudgetExt``, ``PayrollExt``) to flag
    TX-extension carriage. The spine stores these under vanilla Ed-Fi
    names (``CourseTranscript``, ``Budget``, ``Payroll``). Emit both forms
    so element-detail lookups succeed regardless of which side drops
    the suffix.
    """
    base = _normalize_join_key(entity_name)
    if not base:
        return ()
    keys = {base}
    if base.endswith("ext") and len(base) > 3:
        keys.add(base[:-3])
    return tuple(keys)


def _build_element_detail_index(
    elements: list[dict],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (by_ecode, by_name) lookup maps over the detailed element pages.

    ``elements`` is the flat ~400-item list scraped from TWEDS DataElements.
    Each per-entity row (inside an entity's ``elements`` list) has an
    ``ecode`` + ``name`` that we can key against either of these.
    """
    by_ecode: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for e in elements:
        if e.get("ecode"):
            by_ecode[e["ecode"]] = e
        if e.get("name"):
            by_name[_normalize_join_key(e["name"])] = e
    return by_ecode, by_name


def _build_entity_level_rules(entity: dict) -> str | None:
    """Concatenate GR + SR + DR from an entity scrape into one rules block.

    TWEDS marks 'no requirements' with a boilerplate sentence — we skip
    those so the field only carries real signal. Used to populate
    ``ElementRecord.business_rules_text`` — every element on the entity
    gets the same entity-level rules.
    """
    parts: list[str] = []
    gr = (entity.get("general_reporting_requirements") or "").strip()
    sr = (entity.get("special_reporting_requirements") or "").strip()
    dr = (entity.get("data_element_reporting_requirements") or "").strip()
    if gr and "Currently, there are no general reporting requirements" not in gr:
        parts.append(f"General Requirements: {gr}")
    if sr and "Currently, there are no special reporting requirements" not in sr:
        parts.append(f"Special Requirements: {sr}")
    if dr:
        parts.append(f"Data Element Reporting Requirements: {dr}")
    if not parts:
        return None
    return "=== Entity-level rules (shared) ===\n" + "\n\n".join(parts)


def _normalize_tx_entity(raw: str, spine: StateSpine) -> str:
    """PascalCase-preserving entity normalizer for TEDS names.

    Delegates to the catalog-aware ``ingest.normalize.normalize_entity``,
    which handles the TEA-specific ``Ext`` suffix automatically via the
    catalog lookup fallback: ``CourseTranscriptExt`` → catalog miss →
    strip-``Ext`` retry → ``CourseTranscript`` catalog hit. When the TEDS
    entity has no catalog counterpart (e.g., ``BasicReportingPeriodAttendance``),
    the raw form is returned unchanged so the row surfaces as ``Unresolved``
    with a clean analyst-visible entity name.
    """
    return normalize_entity(raw, spine.catalog)


def _clean_element_name(raw: str) -> str:
    """Strip TWEDS rendering artifacts from an element name.

    Some TEDS cells carry trailing notes like
    ``"AssessmentPerformanceLevel\\n\\n(may have multiple instances)"``.
    We keep the first non-empty line as the bare identifier.
    """
    if not raw:
        return ""
    first_line = raw.split("\n", 1)[0].strip()
    # Collapse any internal whitespace defensively.
    return " ".join(first_line.split())


# -- Record emission ─────────────────────────────────────────────────────────


def build_element_records(
    spine: StateSpine,
    entities: list[dict],
    elements: list[dict],
    *,
    tweds_version: int = tx_tweds.TWEDS_VERSION,
) -> list[ElementRecord]:
    """Emit one ElementRecord per (entity, element) TEDS pair.

    Iterates the TWEDS entity scrape; each entity contributes one record
    per item in its ``elements`` list. Element-detail lookups (definition,
    descriptor-table code, citations, related entities) come from the
    flat ``elements`` catalog via ecode or normalized-name lookup.

    ``source`` is set to ``"unknown"`` provisionally — ``run()`` calls
    ``attribute_record_source`` immediately after to classify against the
    spine. Same for ``data_type``: TEDS-provided raw type is stored here,
    then overwritten by ``populate_data_types_from_spine`` for matched
    rows (canonical-type contract).
    """
    by_ecode, by_name = _build_element_detail_index(elements)
    source_tag = f"TWEDS v{tweds_version} (TEDS)"
    entity_rules_cache: dict[str, str | None] = {}
    records: list[ElementRecord] = []

    for ent in entities:
        raw_entity = (
            ent.get("entity_name") or ent.get("sidebar_name") or ""
        ).strip()
        if not raw_entity:
            continue

        entity_norm = _normalize_tx_entity(raw_entity, spine)
        if raw_entity not in entity_rules_cache:
            entity_rules_cache[raw_entity] = _build_entity_level_rules(ent)
        business_rules = entity_rules_cache[raw_entity]

        for elem in ent.get("elements") or []:
            elem_name_raw = elem.get("name") or ""
            element_name = _clean_element_name(elem_name_raw)
            if not element_name:
                continue

            elem_key = _normalize_join_key(element_name)
            ecode = elem.get("ecode")
            elem_detail: dict | None = None
            if ecode:
                elem_detail = by_ecode.get(ecode)
            if elem_detail is None:
                elem_detail = by_name.get(elem_key)

            # Per-entity-row fields first (lighter detail, always present).
            data_type = elem.get("data_type") or None
            descriptor_table_code = tx_tweds.parse_descriptor_table_code(
                elem.get("descriptor_table")
            )

            definition_text = ""
            element_specific_rules: str | None = None
            regulatory_citations: list[str] = []
            related_entities: list[str] = []
            collections_text: str | None = None

            if elem_detail is not None:
                definition = (elem_detail.get("definition") or "").strip()
                if definition:
                    definition_text = definition
                if not data_type:
                    dt = (elem_detail.get("data_type") or "").strip()
                    if dt:
                        data_type = dt
                if not descriptor_table_code:
                    descriptor_table_code = tx_tweds.parse_descriptor_table_code(
                        elem_detail.get("descriptor_table")
                    )
                si = (elem_detail.get("special_instructions") or "").strip()
                if si:
                    element_specific_rules = si
                citations_source = "\n\n".join(
                    x for x in [si, elem_detail.get("former_name")] if x
                )
                regulatory_citations = tx_tweds.extract_regulatory_citations(
                    citations_source
                ) or []
                related_entities = tx_tweds.clean_related_entities(
                    elem_detail.get("entities")
                ) or []
                ct = (elem_detail.get("collections_text") or "").strip() or None
                if ct:
                    collections_text = ct

            section_ecode = ecode or (elem_detail or {}).get("ecode")
            source_page = (
                f"Entity: {raw_entity}, E-code: {section_ecode}"
                if section_ecode
                else f"Entity: {raw_entity}"
            )

            records.append(
                ElementRecord(
                    state="TX",
                    edfi_version=spine.edfi_version,
                    # ``domain`` is the workbook's `Source Area` column —
                    # the TEDS entity this row came from. Parallels AZ
                    # (sheet name) / WI (Confluence domain) / MN
                    # (collection). `Ed-Fi Domain` is populated separately
                    # by the analyst report's spine-catalog lookup.
                    domain=raw_entity,
                    entity=entity_norm,
                    raw_entity=raw_entity,
                    element_name=element_name,
                    data_type=data_type,
                    definition_text=definition_text,
                    business_rules_text=business_rules,
                    element_specific_rules=element_specific_rules,
                    regulatory_citations=regulatory_citations,
                    related_entities=related_entities,
                    descriptor_table_code=descriptor_table_code,
                    collections_text=collections_text,
                    source_document=source_tag,
                    source_page_or_section=source_page,
                    source="unknown",
                    extension_name=None,
                    documented=True,
                )
            )

    return records


# -- Orchestrator ────────────────────────────────────────────────────────────


def run() -> None:
    """TX ingestion: TWEDS-sourced records + spine enrichment + gap log.

    Writes ``data/out/tx_elements_source.json`` and ``data/out/tx_gap_log.json``.
    Requires both:

    - ``data/spine/tx_spine.json`` (``poc3 spine fetch --state TX
      --base-url http://localhost:PORT/metadata/data/v3`` +
      ``poc3 spine build --state TX``).
    - ``data/raw/tx/tweds/{entities,elements}_scraped.json`` — pre-scraped
      TWEDS cache. If missing, ``tx_tweds.load_or_fetch_tweds`` attempts
      a fresh scrape (Playwright required). No silent spine-only fallback
      anymore — if TEDS can't be loaded, this raises.
    """
    from src.ingest.shared import (
        assemble_source_driven,
        write_dual_lens_artifacts,
    )

    if not _TX_SPINE_PATH.exists():
        raise FileNotFoundError(
            f"No TX spine at {_TX_SPINE_PATH}. "
            f"Run `poc3 spine fetch --state TX --base-url <local_docker_url>` "
            f"+ `poc3 spine build --state TX` first."
        )
    spine = StateSpine.model_validate_json(
        _TX_SPINE_PATH.read_text(encoding="utf-8")
    )
    logger.info(
        "Loaded TX spine: %d core entities, %d extensions (Ed-Fi %s)",
        spine.entity_count, spine.extension_count, spine.edfi_version,
    )

    loaded = tx_tweds.load_or_fetch_tweds(_TX_TWEDS_CACHE_DIR, offline_ok=False)
    if loaded is None:
        raise RuntimeError(
            f"Could not load TWEDS cache from {_TX_TWEDS_CACHE_DIR}. "
            f"TX ingestion is TEDS-sourced; either populate the cache or "
            f"ensure a fresh Playwright scrape can run."
        )
    tweds_entities, tweds_elements = loaded
    logger.info(
        "Loaded TWEDS v%d: %d entities, %d element detail pages",
        tx_tweds.TWEDS_VERSION, len(tweds_entities), len(tweds_elements),
    )

    records = build_element_records(spine, tweds_entities, tweds_elements)
    logger.info(
        "Built %d TX ElementRecords from TEDS (before unflatten/dedup)",
        len(records),
    )

    records, recovered, assembly = assemble_source_driven(records, spine)

    # TX-only: the gap log carries source-attribution counts + a
    # provenance note, and the coverage log line appends the attribution
    # (the one genuine divergence among the five adapter tails).
    source_counts = {"core": 0, "extension": 0, "unknown": 0}
    for r in records:
        source_counts[r.source] = source_counts.get(r.source, 0) + 1

    # Shared adapter tail: dual-lens writes → gap log → swagger backfill
    # (issue #213 item 3 — the ~80-line sequence lives once in shared.py).
    write_dual_lens_artifacts(
        state="TX",
        spine=spine,
        records=records,
        assembly=assembly,
        recovered=recovered,
        source_document=f"TWEDS v{tx_tweds.TWEDS_VERSION} (TEDS)",
        source_out=_TX_ELEMENTS_OUT,
        spine_out=_TX_ELEMENTS_SPINE_OUT,
        gap_out=_TX_GAP_OUT,
        spine_path=_TX_SPINE_PATH,
        spine_source_rel=str(_TX_SPINE_PATH.relative_to(_POC3_ROOT)),
        source_coverage_note=(
            "Of our TWEDS v33 TEDS rows (entity/element pairs), how many "
            "match a spine element (case-insensitive, FK+descriptor aliases)."
        ),
        spine_coverage_note=(
            "Of the spine's authoritative element slots (full Ed-Fi UDM), "
            "how many are represented in the TX TEDS reporting surface."
        ),
        gap_log_extra={
            "source_attribution": source_counts,
            "provenance": (
                f"TWEDS v{tx_tweds.TWEDS_VERSION} (TEDS) enumerates which "
                f"(entity, element) rows are in the TX reporting surface. "
                f"Ed-Fi {spine.edfi_version} spine (local Docker ODS) enriches "
                f"canonical names, attribution, and canonical types. Matched "
                f"rows get spine-derived canonical types; unresolved rows keep "
                f"TEDS-verbatim type as audit trail."
            ),
        },
        coverage_log_suffix=" | Attribution: %d core / %d extension / %d unknown",
        coverage_log_suffix_args=(
            source_counts["core"],
            source_counts["extension"],
            source_counts["unknown"],
        ),
        logger=logger,
    )


if __name__ == "__main__":
    run()
