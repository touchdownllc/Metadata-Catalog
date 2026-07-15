"""Cross-state ingest helpers shared by AZ / WI / MN / TX adapters.

Originally this logic lived in `minnesota.py` (because MN was the first adapter
to need spine-driven type inference). Round 2.2 promotes it to a shared module
so AZ and WI can apply the same canonical-type contract: **for matched records,
`data_type` is spine-derived**. Unmatched rows keep source-verbatim types as an
audit-trail fallback.

Phase 2 (Option 3b dual-lens) adds the pipeline helpers that consolidate the
post-parse plumbing the four adapters used to duplicate:

- `run_unflatten_pass(records, spine)` — identity-preserving sub-entity
  collapse loop (returns the `recovered` list, modifies records in place).
- `compute_coverage(records, spine)` — builds the SourceAssembly struct with
  record_keys, matched/unmatched lists, missing_from_docs, unmatched_by_entity.
- `demote_unmatched_to_unknown(records, spine)` — AZ-specific helper that
  treats pre-set `source` values as authoritative while still marking rows
  whose (entity, element) doesn't land on the spine as unknown.
- `assemble_source_driven(records, spine, *, dedup=True, attribute=True)` —
  one-shot orchestrator for WI/MN/TX (AZ keeps bespoke ordering around its
  cross-attribution fix and uses the helpers individually).

Other public API:

- `build_spine_type_index(spine)` — (entity_norm, element_lower) -> canonical type
- `canonical_type(prop_name, raw_type)` — single-property type resolution
- `populate_data_types_from_spine(records, spine)` — apply spine types to matched records
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.models.element import ElementRecord, StateElements
from src.models.alias_grammar import (
    SPA_TEMPLATE_ENTITY,
    capitalize_first,
    collapse_camel_overlap,
    descriptor_variants,
    edorg_subtype_aliases,
    fk_prefixed_alias,
    id_stripped_alias,
    is_spa_template_target,
    naive_plural_forms,
    parent_stripped_tail,
    prefix_parent_of,
    reference_prefix,
    reference_qualifier,
    sub_entity_name_forms,
    uniqueid_id_alias,
)
from src.models.spine import StateSpine


def canonical_type(
    prop_name: str,
    raw_type: str | None,
    raw_format: str | None = None,
) -> str:
    """Return a workbook-friendly data type for a spine property.

    Descriptor-suffixed names resolve to `"Descriptor"` regardless of the raw
    swagger type — analysts reason about these as descriptor values, not the
    underlying serialization (usually integer or string).

    Swagger models temporal types as `string` with `format: date` / `date-time`;
    we promote these to canonical `Date` / `DateTime` so reviewer-facing type
    cells read correctly (Round 2.2 fix — reviewer B flagged `BirthDate` typed
    `String` when the format-derived canonical is `Date`).

    Swagger models integer-ish types as `integer` with `format: int32` /
    `int64`; we don't split those further — both collapse to `Integer`.
    """
    if prop_name.endswith("Descriptor") or prop_name.endswith("DescriptorId"):
        return "Descriptor"
    if not raw_type:
        return ""
    if raw_type == "string" and raw_format:
        fmt = raw_format.lower()
        if fmt == "date":
            return "Date"
        if fmt == "date-time":
            return "DateTime"
        if fmt == "time":
            return "Time"
    return raw_type[0].upper() + raw_type[1:]


def build_spine_type_index(spine: StateSpine) -> dict[tuple[str, str], str]:
    """Build a (entity_norm, element_lower) -> canonical-type index from the spine.

    Mirrors the alias keying used by `utils.matching.match_key` so records
    that have been normalized to lowercase entity forms can look up types
    directly. Covers the same FK-alias forms that `StateSpine.element_keys()`
    emits — otherwise records whose spine-match succeeded via an FK alias
    (e.g., source cell `SchoolYear` resolving against `schoolYearTypeReference`'s
    key property `schoolYear`) get no type back.

    Reference properties (both the `*Reference` form and the bare-prefix alias)
    type as `"Reference"`. The round-2 analyst review flagged WI matched-core
    rows like `schoolReference` with blank Data Type — populating them here
    as `Reference` closes that gap.
    """
    from src.utils.matching import match_key

    idx: dict[tuple[str, str], str] = {}

    def _key(entity: str, name: str) -> tuple[str, str]:
        e, n = match_key(entity, name)
        return (e, n.lower())

    def _add(entity: str, name: str, type_str: str) -> None:
        idx.setdefault(_key(entity, name), type_str)

    def _add_with_descriptor_variants(entity: str, name: str, type_str: str) -> None:
        _add(entity, name, type_str)
        # Alias FORMS from alias_grammar; the variant TYPE is this
        # consumer's own policy (always "Descriptor").
        for variant in descriptor_variants(name):
            _add(entity, variant, "Descriptor")

    for entity_name, entity in spine.catalog.entities.items():
        for prop_name, prop in entity.properties.items():
            _add_with_descriptor_variants(
                entity_name,
                prop_name,
                canonical_type(prop_name, prop.type, prop.format),
            )
        for ref_name, ref in entity.references.items():
            _add(entity_name, ref_name, "Reference")
            prefix = reference_prefix(ref_name)
            if prefix and prefix != ref_name:
                _add(entity_name, prefix, "Reference")
            # Deliberately NARROWER than `element_keys` (issue #213 item
            # 2, documented divergence): no camel-collapse, no EdOrg
            # subtypes, no qualifier tier here — widening the type index
            # is a measured follow-up, not a refactor side effect.
            for kp_name, kp in ref.key_properties.items():
                kp_type = canonical_type(kp_name, kp.type, kp.format)
                _add_with_descriptor_variants(entity_name, kp_name, kp_type)
                uid = uniqueid_id_alias(kp_name)
                if uid:
                    _add(entity_name, uid, kp_type)
                if prefix and prefix != kp_name:
                    cap = capitalize_first(kp_name)
                    _add_with_descriptor_variants(
                        entity_name, fk_prefixed_alias(prefix, kp_name), kp_type
                    )
                    # NOTE the guard runs on the BARE cap key (pre-#213
                    # behavior), not the full prefixed alias like
                    # element_keys — same rule, different operand.
                    stripped_cap = id_stripped_alias(cap)
                    if stripped_cap:
                        _add(entity_name, prefix + stripped_cap, kp_type)
        for sub_name, sub in entity.sub_collections.items():
            _add(entity_name, sub_name, "Collection")
            for sp_name, sp in sub.properties.items():
                _add_with_descriptor_variants(
                    entity_name,
                    sp_name,
                    canonical_type(sp_name, sp.type, sp.format),
                )

    for ext in spine.catalog.extensions.values():
        for prop_name, prop in ext.properties.items():
            _add_with_descriptor_variants(
                ext.extends_entity,
                prop_name,
                canonical_type(prop_name, prop.type, prop.format),
            )
        # Extension references: flatten FK key properties onto the
        # extends_entity the same way `canonical_spine_emit_keys` does,
        # so source rows that land on a ref-flattened slot pick up a
        # type even when the extension has no direct properties.
        for ref_name, ref in ext.references.items():
            _add(ext.extends_entity, ref_name, "Reference")
            prefix = reference_prefix(ref_name)
            if prefix and prefix != ref_name:
                _add(ext.extends_entity, prefix, "Reference")
            for kp_name, kp in ref.key_properties.items():
                kp_type = canonical_type(kp_name, kp.type, kp.format)
                _add_with_descriptor_variants(ext.extends_entity, kp_name, kp_type)
                uid = uniqueid_id_alias(kp_name)
                if uid:
                    _add(ext.extends_entity, uid, kp_type)
                if prefix and prefix != kp_name:
                    cap = capitalize_first(kp_name)
                    _add_with_descriptor_variants(
                        ext.extends_entity,
                        fk_prefixed_alias(prefix, kp_name),
                        kp_type,
                    )
                    stripped_cap = id_stripped_alias(cap)
                    if stripped_cap:
                        _add(ext.extends_entity, prefix + stripped_cap, kp_type)
        # Extension sub-collections: flatten sub-props onto the
        # extends_entity (matches `_emit_sub_collection` in
        # `canonical_spine_emit_keys`).
        #
        # Sub-props are registered BEFORE the sub-coll wrapper so a
        # scalar sub-prop wins over a colliding wrapper name under
        # setdefault's first-writer-wins rule — source docs naming
        # such a collision (e.g., MN's `Membership`) typically mean
        # the scalar count, not the collection wrapper.
        for sub_name, sub in ext.sub_collections.items():
            for sp_name, sp in sub.properties.items():
                _add_with_descriptor_variants(
                    ext.extends_entity,
                    sp_name,
                    canonical_type(sp_name, sp.type, sp.format),
                )
            _add(ext.extends_entity, sub_name, "Collection")

        # Mirror the parent-propagation in `StateSpine.extension_element_keys()`.
        # Skip when `extends_entity` is itself a catalog entity (no cross-up
        # propagation to unrelated parents — matches the rule in spine.py).
        extended = ext.extends_entity
        if extended in spine.catalog.entities:
            continue
        parent_name = prefix_parent_of(extended, spine.catalog.entities)
        if parent_name is not None:
            for prop_name, prop in ext.properties.items():
                _add_with_descriptor_variants(
                    parent_name,
                    prop_name,
                    canonical_type(prop_name, prop.type, prop.format),
                )
            sub_tail = extended[len(parent_name):]
            if sub_tail and sub_tail[0].isupper():
                _add(parent_name, sub_tail[0].lower() + sub_tail[1:], "Collection")
                _add(parent_name, sub_tail, "Collection")
                plural_forms = naive_plural_forms(sub_tail)
                if plural_forms:
                    plural_cap, plural_lower = plural_forms
                    _add(parent_name, plural_cap, "Collection")
                    _add(parent_name, plural_lower, "Collection")

    # Inherited-identity propagation for concrete Student*ProgramAssociation
    # entities — see note in `StateSpine.element_keys`.
    template = spine.catalog.entities.get(SPA_TEMPLATE_ENTITY)
    if template is not None:
        template_entries: list[tuple[tuple[str, str], str]] = []
        tpl_entity_norm, _ = match_key(SPA_TEMPLATE_ENTITY, "")
        for (e_norm, n_lower), type_str in list(idx.items()):
            if e_norm == tpl_entity_norm:
                template_entries.append(((e_norm, n_lower), type_str))
        all_entity_names: set[str] = set(spine.catalog.entities.keys())
        for ext in spine.catalog.extensions.values():
            all_entity_names.add(ext.extends_entity)
        # Sort so setdefault-based first-writer-wins picks a deterministic
        # propagation target across processes. Without the sort, two entities
        # (e.g., a core Student*ProgramAssociation and a
        # StudentSpecialEducationProgramAssociationExtension) racing to write
        # the same (entity, element) key pick different winners per hash seed.
        for entity_name in sorted(all_entity_names):
            if not is_spa_template_target(entity_name):
                continue
            for (_, n_lower), type_str in template_entries:
                _add(entity_name, n_lower, type_str)

    return idx


def build_spine_description_index(
    spine: StateSpine,
) -> dict[tuple[str, str], str]:
    """Build (entity_lower, element_lower) → Ed-Fi description lookup.

    Wraps ``canonical_spine_emit_keys`` with its alias sink so every
    alias a source row might use (FK prefix, descriptor -Id, subtype
    expansion, etc.) points back at the canonical emit's description.
    Returned strings are never empty — emits without a description are
    omitted so callers can safely test by key presence.
    """
    from src.utils.matching import match_key

    alias_sink: dict[tuple[str, str], set[tuple[str, str]]] = {}
    emits = canonical_spine_emit_keys(spine, _alias_sink=alias_sink)

    def _key(entity: str, name: str) -> tuple[str, str]:
        e, n = match_key(entity, name)
        return (e, n.lower())

    idx: dict[tuple[str, str], str] = {}

    # Primary alias set — record-match forms for each emit's own slot.
    for emit in emits:
        desc = (emit.description or "").strip()
        if not desc:
            continue
        idx.setdefault(_key(emit.entity, emit.element_name), desc)

    # Secondary alias sink — EducationOrganization subtype expansion,
    # descriptor -Id / bare forms, FK-prefix + collapsed + strip-Id aliases.
    slot_to_desc = {
        _key(emit.entity, emit.element_name): (emit.description or "").strip()
        for emit in emits
    }
    for slot, aliases in alias_sink.items():
        desc = slot_to_desc.get(slot, "")
        if not desc:
            continue
        for alias_key in aliases:
            idx.setdefault(alias_key, desc)

    return idx


def populate_edfi_standard_definition_from_spine(
    records: list[ElementRecord], spine: StateSpine
) -> None:
    """Fill ``edfi_standard_definition`` on matched source-lens records.

    Source-lens records arrive from state adapters with only the state's
    text — the Ed-Fi baseline lives in the spine catalog's property
    descriptions and was never propagated. Phase C2's
    ``definition_adds_detail_beyond_edfi`` LLM fact needs both to
    compare, so propagate here after ``attribute_record_source``.

    Only fires on rows with ``source in {"core", "extension"}`` — those
    resolved to a spine slot and have a canonical Ed-Fi description to
    copy. ``source='unknown'`` rows have no spine anchor and keep their
    ``edfi_standard_definition`` unchanged (typically None).

    Does NOT overwrite a pre-existing non-empty value — state adapters
    that populate the field from another source (none today) stay
    authoritative.
    """
    from src.utils.matching import record_match_keys

    idx = build_spine_description_index(spine)

    for i, r in enumerate(records):
        if r.source == "unknown":
            continue
        if (r.edfi_standard_definition or "").strip():
            continue
        for key in sorted(record_match_keys(r.entity, r.element_name)):
            desc = idx.get(key)
            if desc:
                records[i] = r.model_copy(
                    update={"edfi_standard_definition": desc}
                )
                break


def populate_data_types_from_spine(
    records: list[ElementRecord], spine: StateSpine
) -> None:
    """Fill per-record `data_type` from the spine's property metadata.

    **Canonical-type contract:** for matched records (source in `{"core",
    "extension"}`), the spine is authoritative — we OVERWRITE any source-
    inferred type with the spine-derived canonical form. This resolves:

    - WI descriptor rows typed as `String` from raw Confluence cells.
    - Source typos like WI's `lastQualifyingMove: Bloolean`.
    - AZ `*DescriptorId` rows typed as Integer by the XLSX.
    - MN rows whose source cell has no type column at all.

    For unmatched (`source="unknown"`) records, we leave `data_type` untouched
    so the source-verbatim form remains as an audit trail.

    Runs AFTER `attribute_record_source` so the record's `entity` is already
    the canonical post-unflatten form. Uses `record_match_keys` aliases so
    descriptor-suffix variants and path-tail splits resolve the way the
    spine-match step does.

    Alias keys are sorted before iteration so the first-hit `break` picks a
    deterministic winner across processes — Python `set` iteration order
    depends on `PYTHONHASHSEED`, which would otherwise cause byte-unstable
    artifacts when ambiguous aliases hit different types.
    """
    from src.utils.matching import record_match_keys

    idx = build_spine_type_index(spine)

    for i, r in enumerate(records):
        if r.source == "unknown":
            continue
        for key in sorted(record_match_keys(r.entity, r.element_name)):
            type_str = idx.get(key)
            if type_str:
                records[i] = r.model_copy(update={"data_type": type_str})
                break


# ------------------------------------------------------------------------
# Phase 2 — shared post-parse pipeline helpers
# ------------------------------------------------------------------------


def _spine_match_helpers(spine: StateSpine):
    """Build (spine_keys, _matches) closures used across pipeline helpers.

    `spine_keys` is the lowercased (entity, element) set derived from
    `spine.element_keys()`; `_matches` tests a (entity, element_name)
    record via `record_match_keys` intersection.
    """
    from src.utils.matching import match_key, record_match_keys

    def _key(entity: str, name: str) -> tuple[str, str]:
        e, n = match_key(entity, name)
        return (e, n.lower())

    spine_keys = {_key(e, n) for (e, n) in spine.element_keys()}

    def _matches(entity: str, name: str) -> bool:
        return bool(record_match_keys(entity, name) & spine_keys)

    return spine_keys, _matches, _key


def run_unflatten_pass(
    records: list[ElementRecord], spine: StateSpine
) -> list[dict[str, str]]:
    """Identity-preserving unflatten pass used by all four adapters.

    For each record whose (entity, element_name) doesn't match a spine key,
    try to resolve the entity as a concatenated sub-entity of a core parent
    (e.g., `CalendarDateCalendarEvent` → parent `CalendarDate`, sub-collection
    `calendarEvents`). If the re-keyed pair matches, rewrite the record's
    entity in place and record the rewrite in the returned `recovered` list.

    This was duplicated verbatim in arizona.py, wisconsin.py, minnesota.py,
    and texas.py before Phase 2 consolidated it here.
    """
    from src.spine.unflatten import build_unflatten_map, resolve_parent

    _, _matches, _ = _spine_match_helpers(spine)
    unflatten_map = build_unflatten_map(spine)
    spine_entity_names = spine.entity_keys()
    recovered: list[dict[str, str]] = []

    for i, r in enumerate(records):
        if _matches(r.entity, r.element_name):
            continue
        parent_info = resolve_parent(r.entity, unflatten_map, spine_entity_names)
        if parent_info is None:
            continue
        parent, sub_coll_name = parent_info
        if not _matches(parent, r.element_name):
            continue
        original_entity = r.entity
        original_section = r.source_page_or_section or ""
        records[i] = r.model_copy(update={
            "entity": parent,
            "raw_entity": original_entity,
            "source_page_or_section": (
                f"{sub_coll_name} / {original_section}"
                if original_section else sub_coll_name
            ),
        })
        recovered.append({
            "from_entity": original_entity,
            "to_entity": parent,
            "sub_collection": sub_coll_name,
            "element_name": r.element_name,
        })

    return recovered


def demote_unmatched_to_unknown(
    records: list[ElementRecord], spine: StateSpine
) -> None:
    """AZ-specific: demote rows that don't land on the spine to source=unknown.

    AZ uniquely sets `source` at record-creation time from the XLSX namespace
    (`edfi.*` → core, `az.*` → extension). Rows whose (entity, element_name)
    don't actually match the spine shouldn't read as "Matched (core)" in the
    analyst workbook — they're genuinely unresolved. Demote them here while
    preserving pre-set `source` values on matched rows.
    """
    _, _matches, _ = _spine_match_helpers(spine)
    for i, r in enumerate(records):
        if not _matches(r.entity, r.element_name):
            records[i] = r.model_copy(update={
                "source": "unknown",
                "extension_name": None,
            })


@dataclass
class SourceAssembly:
    """Coverage book-keeping for a source-driven adapter run.

    Populated by `compute_coverage` after the final record list is in hand
    (post-unflatten, post-attribute, post-types, post-dedup).
    """

    records: list[ElementRecord]
    spine_keys: set[tuple[str, str]]
    record_keys: set[tuple[str, str]]
    matched_records: list[ElementRecord]
    unmatched_records: list[ElementRecord]
    missing_from_docs: set[tuple[str, str]]
    spine_extension_pairs: set[tuple[str, str]]
    unmatched_by_entity: dict[str, dict] = field(default_factory=dict)
    # Spine-lens only (issue #213 item 2): documented-match counts per
    # alias TIER — how much of the match rate rides on the fuzziest
    # grammar tiers (`fk_camel_collapsed`, `sub_entity_tail`, ...) vs
    # the primary name. Filled by `assemble_spine_driven`; empty on
    # source-driven assemblies.
    alias_tier_histogram: dict[str, int] = field(default_factory=dict)


def compute_coverage(
    records: list[ElementRecord], spine: StateSpine
) -> SourceAssembly:
    """Build per-state coverage book-keeping from a finalized record list."""
    from src.spine.unflatten import ABSTRACT_BASES
    from src.utils.matching import record_match_keys

    spine_keys, _matches, _key = _spine_match_helpers(spine)

    record_keys: set[tuple[str, str]] = set()
    for r in records:
        record_keys |= record_match_keys(r.entity, r.element_name)
    matched_records = [r for r in records if _matches(r.entity, r.element_name)]
    unmatched_records = [
        r for r in records if not _matches(r.entity, r.element_name)
    ]
    missing_from_docs = spine_keys - record_keys
    spine_extension_pairs = {
        _key(ext.extends_entity, prop)
        for ext in spine.catalog.extensions.values()
        for prop in ext.properties
    }

    unmatched_by_entity: dict[str, dict] = {}
    for r in unmatched_records:
        bucket = unmatched_by_entity.setdefault(
            r.entity,
            {
                "tag": "abstract_base" if r.entity in ABSTRACT_BASES else "unknown",
                "elements": [],
            },
        )
        bucket["elements"].append(r.element_name)
    for bucket in unmatched_by_entity.values():
        bucket["elements"] = sorted(set(bucket["elements"]))
    unmatched_by_entity = dict(sorted(unmatched_by_entity.items()))

    return SourceAssembly(
        records=records,
        spine_keys=spine_keys,
        record_keys=record_keys,
        matched_records=matched_records,
        unmatched_records=unmatched_records,
        missing_from_docs=missing_from_docs,
        spine_extension_pairs=spine_extension_pairs,
        unmatched_by_entity=unmatched_by_entity,
    )


def assemble_source_driven(
    records: list[ElementRecord],
    spine: StateSpine,
    *,
    dedup: bool = True,
    attribute: bool = True,
) -> tuple[list[ElementRecord], list[dict[str, str]], SourceAssembly]:
    """Shared WI/MN/TX pipeline: unflatten → attribute → types → dedup → coverage.

    AZ's adapter has bespoke interleaving (cross-attribution hint fix +
    demote-to-unknown around its pre-set namespace-driven `source`), so it
    calls the underlying helpers (`run_unflatten_pass`, `compute_coverage`,
    `demote_unmatched_to_unknown`) directly rather than this orchestrator.

    `dedup=False` preserves WI's historical no-dedup behavior (WI's
    Confluence scrape doesn't produce duplicates and the adapter has never
    called `dedup_records`).
    `attribute=False` is for AZ, which pre-sets `source` from namespace.

    Returns `(records, recovered, assembly)`. `records` may be a new list
    (post-dedup) distinct from the input.
    """
    from src.utils.dedup import dedup_records
    from src.utils.matching import attribute_record_source

    recovered = run_unflatten_pass(records, spine)
    if attribute:
        attribute_record_source(records, spine)
    populate_data_types_from_spine(records, spine)
    populate_edfi_standard_definition_from_spine(records, spine)
    if dedup:
        pre_dedup = len(records)
        records = dedup_records(records)
        if len(records) < pre_dedup:
            import logging
            logging.getLogger(__name__).info(
                "Post-unflatten dedup: %d -> %d records (collapsed %d duplicate pairs)",
                pre_dedup, len(records), pre_dedup - len(records),
            )
    stamp_edfi_domains(records, spine)
    assembly = compute_coverage(records, spine)
    return records, recovered, assembly


def build_gap_log(
    state: str,
    spine: StateSpine,
    spine_source_rel: str,
    assembly: SourceAssembly,
    recovered: list[dict[str, str]],
    source_coverage_note: str,
    spine_coverage_note: str,
    extra: dict | None = None,
    alias_tier_histogram: dict[str, int] | None = None,
) -> dict:
    """Build the canonical gap-log dict from a SourceAssembly.

    The `extra` dict is merged verbatim (e.g., TX adds `source_attribution`
    and `provenance`).
    """
    total = len(assembly.records)
    matched = len(assembly.matched_records)
    record_keys_on_spine = len(assembly.record_keys & assembly.spine_keys)
    gap_log = {
        "state": state,
        "spine_source": spine_source_rel,
        "spine_unique_element_keys": len(assembly.spine_keys),
        "spine_extension_element_count": len(assembly.spine_extension_pairs),
        "source_element_count": total,
        "source_coverage": {
            "matched": matched,
            "total": total,
            "pct": round(matched / total * 100, 1) if total else 0.0,
            "note": source_coverage_note,
        },
        "spine_coverage": {
            "matched_unique_keys": record_keys_on_spine,
            "total_spine_keys": len(assembly.spine_keys),
            "pct": (
                round(record_keys_on_spine / len(assembly.spine_keys) * 100, 1)
                if assembly.spine_keys else 0.0
            ),
            "note": spine_coverage_note,
        },
        "unmatched_source_count": len(assembly.unmatched_records),
        "unflatten_recovered_count": len(recovered),
        "unflatten_recovered": sorted(
            recovered, key=lambda d: (d["from_entity"], d["element_name"])
        ),
        "unmatched_by_entity": assembly.unmatched_by_entity,
        "missing_from_docs_count": len(assembly.missing_from_docs),
        "missing_from_docs_samples": sorted(assembly.missing_from_docs)[:50],
    }
    if alias_tier_histogram is not None:
        # Issue #213 item 2 — spine-lens documented matches per alias
        # tier ("primary" = the slot's own name/direct expansion; the
        # rest are the grammar's fuzzy tiers). Audits how much match
        # rate rides on the fuzziest tiers.
        gap_log["alias_tier_histogram"] = alias_tier_histogram
    if extra:
        gap_log.update(extra)
    return gap_log


# ------------------------------------------------------------------------
# Phase 3 — spine-driven assembler
# ------------------------------------------------------------------------


@dataclass
class SpineEmit:
    """One canonical (entity, element) emission from the spine catalog.

    `canonical_spine_emit_keys()` collapses `element_keys()` alias
    expansions (e.g., `schoolReference.schoolId` AND `schoolId`) into one
    logical emit per (entity, element_lower) key.
    """

    entity: str
    element_name: str
    data_type: str
    source: str  # "core" | "extension"
    extension_name: str | None = None
    domain: str = ""
    description: str = ""


@dataclass
class SourceFacts:
    """Source-document enrichment for a single (entity, element) slot.

    Populated by `build_source_index()` and looked up by
    `assemble_spine_driven()` to enrich spine-origin records with the
    state's source-doc text and descriptor/collection metadata.
    """

    definition_text: str = ""
    business_rules_text: str | None = None
    element_specific_rules: str | None = None
    regulatory_citations: list = field(default_factory=list)
    related_entities: list = field(default_factory=list)
    descriptor_table_code: str | None = None
    descriptor_table_values: list = field(default_factory=list)
    collections_text: str | None = None
    source_document: str | None = None
    source_page_or_section: str | None = None
    # Issue #184: the source row's Source Area, carried through so the
    # spine-driven assembler can stamp the real Source Area onto documented
    # spine-lens rows (instead of the Ed-Fi domain that lived in `domain`).
    domain: str = ""


def _entity_domain(entity_name: str, spine: StateSpine) -> str:
    """Return the primary domain for an entity, or empty string.

    Resolution order:

    1. Direct catalog hit on ``entity_name``.
    2. Longest strict-prefix catalog parent — sub-collection names like
       ``StudentSchoolAssociationLocalEducationAgency`` inherit from
       ``StudentSchoolAssociation``.
    3. Longest-common-prefix catalog sibling. Abstract bases like
       ``EducationOrganization`` and ``GeneralStudentProgramAssociation``
       are not materialized as concrete catalog entities, but their
       sibling implementations (e.g., ``EducationOrganizationNetwork``)
       carry the convention-named domain. Pick the sibling that shares
       the longest CamelCase-aligned prefix and has non-empty domains.

    Mirrors the workbook-side helper at
    ``src/report/analyst.py::_edfi_domain_for`` and extends it with
    the sibling fallback so swagger-backfilled rows always surface with a
    real domain rather than an empty string.
    """
    entities = spine.catalog.entities
    entity = entities.get(entity_name)
    if entity and entity.domains:
        return entity.domains[0]

    # Step 2: longest strict-prefix catalog parent.
    best_parent: str | None = None
    for candidate in entities:
        if (
            entity_name != candidate
            and len(entity_name) > len(candidate)
            and entity_name.startswith(candidate)
            and entity_name[len(candidate)].isupper()
            and entities[candidate].domains
        ):
            if best_parent is None or len(candidate) > len(best_parent):
                best_parent = candidate
    if best_parent is not None:
        return entities[best_parent].domains[0]

    # Step 3: longest CamelCase-aligned common-prefix sibling. Some
    # abstract bases (``EducationOrganization``,
    # ``GeneralStudentProgramAssociation``) are never materialized as
    # concrete catalog entities, but their concrete siblings carry the
    # right domain. Pick the sibling whose shared prefix is longest AND
    # ends at a CamelCase boundary in both names.
    best_overlap = 0
    best_sibling_domain: str | None = None
    for candidate in entities:
        if candidate == entity_name or not entities[candidate].domains:
            continue
        common_len = 0
        for i in range(min(len(entity_name), len(candidate))):
            if entity_name[i] != candidate[i]:
                break
            common_len = i + 1
        # Require a meaningful overlap (≥6 chars to avoid noisy matches
        # like ``Course*`` vs ``CourseTranscript``) AND a CamelCase
        # boundary on both sides — i.e., either we hit the end of one
        # name or the next char of each name is uppercase.
        if common_len < 6:
            continue
        boundary_self = (
            common_len == len(entity_name) or entity_name[common_len].isupper()
        )
        boundary_cand = (
            common_len == len(candidate) or candidate[common_len].isupper()
        )
        if not (boundary_self and boundary_cand):
            continue
        if common_len > best_overlap:
            best_overlap = common_len
            best_sibling_domain = entities[candidate].domains[0]
    if best_sibling_domain is not None:
        return best_sibling_domain

    return ""


def edfi_domain_for_entity(entity_name: str, spine: StateSpine) -> str | None:
    """Canonical entity -> Ed-Fi domain resolver for the ``edfi_domain`` field.

    Single source of truth (issue #184) unifying the two resolvers that
    previously disagreed: ``_entity_domain`` (single ``domains[0]``, with
    sibling fallback) and the workbook-side ``analyst._edfi_domain_for``
    (joined, no sibling fallback). This returns the **joined** form
    (``"; ".join(domains)``) AND keeps the full **3-step** fallback
    (direct -> longest-prefix parent -> longest CamelCase sibling), so
    every record's ``edfi_domain`` is populated consistently regardless of
    provenance and the analyst "Ed-Fi Domain" column can read it directly.

    Returns ``None`` only when neither the entity, any prefix parent, nor
    any CamelCase sibling carries domain data (Ed-Fi abstract bases that
    are never materialized as concrete catalog entities).
    """
    entities = spine.catalog.entities
    ent = entities.get(entity_name)
    if ent is not None and ent.domains:
        return "; ".join(ent.domains)

    # Step 2: longest strict-prefix catalog parent. Sub-collection /
    # concatenated names (``StudentSchoolAssociationLocalEducationAgency``)
    # inherit their parent's domains.
    best_parent: str | None = None
    for candidate in entities:
        if (
            entity_name != candidate
            and len(entity_name) > len(candidate)
            and entity_name.startswith(candidate)
            and entity_name[len(candidate)].isupper()
            and entities[candidate].domains
        ):
            if best_parent is None or len(candidate) > len(best_parent):
                best_parent = candidate
    if best_parent is not None:
        return "; ".join(entities[best_parent].domains)

    # Step 3: longest CamelCase-aligned common-prefix sibling. Abstract
    # bases (``EducationOrganization``, ``GeneralStudentProgramAssociation``)
    # aren't concrete catalog entities, but their concrete siblings carry
    # the right domains. Mirrors ``_entity_domain``'s step 3 but returns the
    # joined form.
    best_overlap = 0
    best_sibling: str | None = None
    for candidate in entities:
        if candidate == entity_name or not entities[candidate].domains:
            continue
        common_len = 0
        for i in range(min(len(entity_name), len(candidate))):
            if entity_name[i] != candidate[i]:
                break
            common_len = i + 1
        if common_len < 6:
            continue
        boundary_self = (
            common_len == len(entity_name) or entity_name[common_len].isupper()
        )
        boundary_cand = (
            common_len == len(candidate) or candidate[common_len].isupper()
        )
        if not (boundary_self and boundary_cand):
            continue
        if common_len > best_overlap:
            best_overlap = common_len
            best_sibling = candidate
    if best_sibling is not None:
        return "; ".join(entities[best_sibling].domains)

    return None


def stamp_edfi_domains(records: list[ElementRecord], spine: StateSpine) -> None:
    """Set ``edfi_domain`` on every record from its entity via the canonical
    resolver (issue #184). Mutates in place.

    Filtered-domain placeholder rows (``source=="filtered"``) are skipped —
    they are constructed with ``edfi_domain`` already set to their
    PLACEHOLDER_LABEL (the collapsed Ed-Fi domain they represent), and the
    canonical resolver would not recognize the synthetic
    ``"<Label> (filtered)"`` entity name.

    Called at every artifact-write chokepoint: ``assemble_source_driven``
    (MN/TX/IN), ``assemble_spine_driven`` (all spine lenses), the bespoke
    AZ/WI source paths, and the swagger-backfill append.
    """
    for r in records:
        if r.source == "filtered":
            continue
        r.edfi_domain = edfi_domain_for_entity(r.entity, spine)


def canonical_spine_emit_keys(
    spine: StateSpine,
    _alias_sink: dict[tuple[str, str], set[tuple[str, str]]] | None = None,
    _tier_sink: dict[tuple[str, str], str] | None = None,
) -> list[SpineEmit]:
    """Walk the spine catalog directly and emit one SpineEmit per logical
    (entity, element) position.

    Deliberately does NOT use `spine.element_keys()` — that method returns
    a `set` (non-deterministic iteration order) and emits alias variants
    (e.g., `schoolReference.schoolId` + `schoolId`) we want to collapse.
    This function iterates the catalog in sorted-key order so spine-driven
    output is reproducible across runs.

    Dedup key is `(entity_lower, element_lower)` so alias variants on
    different sides of the same logical slot collapse to one emit.

    `_alias_sink`, when provided, receives `{canonical_slot_key:
    match_alias_keys}` — one entry per emit, carrying the broader
    element_keys-style alias match-set the source index should be checked
    against. Used by `assemble_spine_driven` so per-emit match coverage
    mirrors `StateSpine.element_keys()` without inflating emit cardinality.
    Internal / experimental — public callers should not rely on this.
    """
    from src.utils.matching import record_match_keys

    emits: list[SpineEmit] = []
    seen: set[tuple[str, str]] = set()

    def _slot_key(entity: str, element_name: str) -> tuple[str, str]:
        return (entity.lower(), element_name.lower())

    def _add_alias(
        slot: tuple[str, str],
        entity: str,
        element_name: str,
        tier: str = "primary",
    ) -> None:
        if _alias_sink is None:
            return
        expanded = record_match_keys(entity, element_name)
        _alias_sink.setdefault(slot, set()).update(expanded)
        if _tier_sink is not None:
            # First-writer-wins: a key reachable both as a primary form
            # and via a fuzzy tier counts as primary (registration order
            # puts the emit's own form first).
            for alias_key in expanded:
                _tier_sink.setdefault(alias_key, tier)

    def _emit(
        entity: str,
        element_name: str,
        data_type: str,
        source: str,
        extension_name: str | None = None,
        domain: str = "",
        description: str = "",
    ) -> None:
        key = _slot_key(entity, element_name)
        if key in seen:
            return
        seen.add(key)
        _add_alias(key, entity, element_name)
        emits.append(SpineEmit(
            entity=entity,
            element_name=element_name,
            data_type=data_type,
            source=source,
            extension_name=extension_name,
            domain=domain,
            description=description,
        ))

    def _emit_prop(entity: str, prop_name: str, prop, source: str, **kw) -> None:
        _emit(
            entity,
            prop_name,
            canonical_type(prop_name, prop.type, prop.format),
            source,
            description=prop.description or "",
            **kw,
        )
        slot = _slot_key(entity, prop_name)
        # Descriptor-ID variant and MDE bare form: source docs sometimes
        # carry `-Id` suffix (`gradeLevelDescriptorId`) OR drop the
        # `Descriptor` suffix entirely (MDE: `gradeLevel` for
        # `gradeLevelDescriptor`). Register both as aliases on the same
        # canonical slot — they describe the same logical position, so
        # NOT new emits (would inflate denominator).
        for variant in descriptor_variants(prop_name):
            _add_alias(slot, entity, variant, tier="descriptor_variant")

    def _emit_reference_flattenings(
        entity: str, ref_name: str, ref, source: str, **kw
    ) -> None:
        # The reference itself (typed "Reference"). Reference descriptions
        # are almost always empty in Ed-Fi swaggers (the $ref target stub
        # carries none); emit "" when absent rather than inventing one.
        _emit(entity, ref_name, "Reference", source, description=ref.description or "", **kw)
        ref_slot = _slot_key(entity, ref_name)
        prefix = reference_prefix(ref_name)
        # Bare-prefix alias (MDE names the target entity itself):
        # `CourseOffering.courseReference` → also matches source
        # `CourseOffering.course`. Register as an alias on the same
        # canonical slot — doesn't inflate emit cardinality.
        if prefix and prefix != ref_name:
            _add_alias(ref_slot, entity, prefix, tier="ref_prefix")
        # Ed-Fi 4.0 collapsed EducationOrganization concrete subtypes
        # (School, LEA, SEA, ...) into a single
        # `educationOrganizationReference`. TEA / other SEAs still name
        # the concrete subtype in source docs. Register each subtype's
        # Pascal + camel forms as aliases on the generalized ref slot.
        if ref.entity == "EducationOrganization":
            for alias in edorg_subtype_aliases():
                _add_alias(ref_slot, entity, alias, tier="edorg_subtype")
        qualifier = reference_qualifier(prefix, ref.entity)
        # Each FK key property is emitted at the parent-entity level —
        # source docs usually flatten references to their key columns.
        for kp_name in sorted(ref.key_properties.keys()):
            kp = ref.key_properties[kp_name]
            _emit_prop(entity, kp_name, kp, source, **kw)
            kp_slot = _slot_key(entity, kp_name)
            # `*UniqueId` → `*Id` alias (AZ/TX docs use `studentId` for
            # spine `studentUniqueId`). Same logical slot, alias only.
            uid = uniqueid_id_alias(kp_name)
            if uid:
                _add_alias(kp_slot, entity, uid, tier="uniqueid_id")
            cap = capitalize_first(kp_name)
            # Reference-prefixed FK aliases like `programEducationOrganizationId`
            # when ref is `programReference` and key is `educationOrganizationId`.
            # Skip the trivial case where the key already starts with the
            # prefix (e.g., schoolReference.schoolId → would emit
            # `schoolSchoolId`, which no source doc ever uses).
            if (
                prefix
                and prefix != kp_name
                and not kp_name.lower().startswith(prefix.lower())
            ):
                fk_alias = fk_prefixed_alias(prefix, kp_name)
                _emit_prop(entity, fk_alias, kp, source, **kw)
                # Camel-overlap collapsed form + `Id` stripped form are
                # extra alias names for the SAME composite-FK slot.
                fk_slot = _slot_key(entity, fk_alias)
                collapsed = collapse_camel_overlap(prefix, cap)
                if collapsed:
                    _add_alias(
                        fk_slot, entity, collapsed,
                        tier="fk_camel_collapsed",
                    )
                stripped = id_stripped_alias(fk_alias)
                if stripped:
                    _add_alias(
                        fk_slot, entity, stripped, tier="fk_id_stripped"
                    )
            # Qualifier-prefixed FK alias as additional alias key on kp slot.
            if qualifier:
                _add_alias(
                    kp_slot, entity,
                    fk_prefixed_alias(qualifier, kp_name),
                    tier="fk_qualifier",
                )

    def _emit_sub_collection(
        entity: str, sub_name: str, sub, source: str, **kw
    ) -> None:
        _emit(entity, sub_name, "Collection", source, description=sub.description or "", **kw)
        sub_slot = _slot_key(entity, sub_name)
        for sp_name in sorted(sub.properties.keys()):
            sp = sub.properties[sp_name]
            _emit_prop(entity, sp_name, sp, source, **kw)
        # TEDS-style source docs sometimes name a sub-collection by its
        # concrete sub-entity type (`Parent.Address`, `Parent.DyslexiaRiskSet`)
        # rather than the collection property name (`addresses`). Register
        # Pascal, camel, and parent-prefix-stripped tail forms as aliases
        # on the sub-collection slot — all the same logical position.
        if sub.sub_entity:
            for form in sub_entity_name_forms(sub.sub_entity):
                _add_alias(sub_slot, entity, form, tier="sub_entity_name")
            tail_forms = parent_stripped_tail(sub.sub_entity, entity)
            if tail_forms:
                tail, tail_camel = tail_forms
                _add_alias(sub_slot, entity, tail, tier="sub_entity_tail")
                if tail_camel != tail:
                    _add_alias(
                        sub_slot, entity, tail_camel, tier="sub_entity_tail"
                    )

    for entity_name in sorted(spine.catalog.entities.keys()):
        entity = spine.catalog.entities[entity_name]
        domain = _entity_domain(entity_name, spine)
        for prop_name in sorted(entity.properties.keys()):
            _emit_prop(entity_name, prop_name, entity.properties[prop_name], "core", domain=domain)
        for ref_name in sorted(entity.references.keys()):
            _emit_reference_flattenings(
                entity_name, ref_name, entity.references[ref_name], "core", domain=domain,
            )
        for sub_name in sorted(entity.sub_collections.keys()):
            _emit_sub_collection(
                entity_name, sub_name, entity.sub_collections[sub_name], "core", domain=domain,
            )

    for ext_name in sorted(spine.catalog.extensions.keys()):
        ext = spine.catalog.extensions[ext_name]
        parent_domain = _entity_domain(ext.extends_entity, spine)
        kwargs = {"extension_name": ext_name, "domain": parent_domain}
        for prop_name in sorted(ext.properties.keys()):
            _emit_prop(ext.extends_entity, prop_name, ext.properties[prop_name], "extension", **kwargs)
        for ref_name in sorted(ext.references.keys()):
            _emit_reference_flattenings(
                ext.extends_entity, ref_name, ext.references[ref_name], "extension", **kwargs,
            )
        for sub_name in sorted(ext.sub_collections.keys()):
            _emit_sub_collection(
                ext.extends_entity, sub_name, ext.sub_collections[sub_name], "extension", **kwargs,
            )

    # Inherited-identity propagation for concrete `Student*ProgramAssociation`
    # entities. Ed-Fi flattens `StudentProgramAssociation` base fields
    # (`programType`, `programName`, identity refs) into every concrete
    # subclass at Swagger-emission time, but some state sandboxes (notably
    # MN) register concrete program associations ONLY as `*_` extensions
    # whose `extends_entity` is the concrete name — the concrete entity
    # itself is never materialized as a top-level catalog entity, so its
    # inherited-identity emits never reach the emit list via the main walk.
    # Mirror `StateSpine.element_keys`'s template-propagation loop at the
    # canonical-emit layer so source rows on concrete program associations
    # resolve against the template slots.
    template_entity = spine.catalog.entities.get(SPA_TEMPLATE_ENTITY)
    if template_entity is not None:
        template_emits = [
            e for e in emits if e.entity == SPA_TEMPLATE_ENTITY
        ]
        all_entity_names: set[str] = set(spine.catalog.entities.keys())
        for ext in spine.catalog.extensions.values():
            all_entity_names.add(ext.extends_entity)
        for target_entity in sorted(all_entity_names):
            if not is_spa_template_target(target_entity):
                continue
            target_domain = _entity_domain(target_entity, spine)
            for te in template_emits:
                _emit(
                    target_entity,
                    te.element_name,
                    te.data_type,
                    te.source,
                    extension_name=te.extension_name,
                    domain=target_domain or te.domain,
                    description=te.description,
                )
                # Propagate the template slot's alias match-set onto the
                # target slot so descriptor -Id / bare / FK-prefix aliases
                # also resolve under the concrete entity name.
                if _alias_sink is not None:
                    from src.utils.matching import normalize_entity
                    src_slot = _slot_key(SPA_TEMPLATE_ENTITY, te.element_name)
                    dst_slot = _slot_key(target_entity, te.element_name)
                    target_norm = normalize_entity(target_entity)
                    for (_, alias_elem_key) in _alias_sink.get(src_slot, set()):
                        _alias_sink.setdefault(dst_slot, set()).add(
                            (target_norm, alias_elem_key)
                        )
                        if _tier_sink is not None:
                            _tier_sink.setdefault(
                                (target_norm, alias_elem_key),
                                "spa_template",
                            )

    # Spine-lens filter: Reference and Collection wrapper slots
    # (`schoolReference`, `addresses`, ...) don't carry reviewer-actionable
    # content. Their flattened FK key properties (`schoolId`) and
    # sub-collection sub-properties (`city`, `streetNumberName`) are
    # emitted at the parent-entity level and stay — those are what
    # reviewers score. The alias-sink entries for the dropped slots
    # become dead weight (harmless — never queried since the slot isn't
    # in `emits`).
    return [e for e in emits if e.data_type not in ("Reference", "Collection")]


def build_source_index(
    source_records: list[ElementRecord],
) -> dict[tuple[str, str], SourceFacts]:
    """Index source rows by (entity_norm, element_lower) with alias expansion.

    Expected input is the POST-unflatten source_records returned by
    `assemble_source_driven()` (entity names canonical, dedup collapsed).
    For each record, `record_match_keys` expands (entity, element_name)
    into all alias forms so later spine-lens lookups via alias keys hit.

    First-record-wins under key collision (dedup already ran, so records
    arriving here should be unique per logical slot).
    """
    from src.utils.matching import record_match_keys

    index: dict[tuple[str, str], SourceFacts] = {}
    for r in source_records:
        facts = SourceFacts(
            definition_text=r.definition_text,
            business_rules_text=r.business_rules_text,
            element_specific_rules=r.element_specific_rules,
            regulatory_citations=list(r.regulatory_citations),
            related_entities=list(r.related_entities),
            descriptor_table_code=r.descriptor_table_code,
            descriptor_table_values=list(r.descriptor_table_values),
            collections_text=r.collections_text,
            source_document=r.source_document,
            source_page_or_section=r.source_page_or_section,
            domain=r.domain,
        )
        for key in record_match_keys(r.entity, r.element_name):
            index.setdefault(key, facts)
    return index


def assemble_spine_driven(
    spine: StateSpine,
    source_index: dict[tuple[str, str], SourceFacts],
    spine_missing_source_rows: list[ElementRecord],
    *,
    state: str,
    edfi_version: str,
    source_document: str | None = None,
) -> tuple[list[ElementRecord], SourceAssembly]:
    """Build the spine-driven artifact: one ElementRecord per canonical
    spine position, enriched from source_index where available.

    Spine-missing source rows (ones whose (entity, element) didn't land on
    any spine key) are appended as a hybrid tail with `source="unknown"`
    and `documented=True` — this preserves the audit trail of TEDS-only
    rows etc. (Risk A from the research).

    SIS-never-populated domains (Assessment / Survey / LearningStandard /
    Gradebook / Intervention) are collapsed per `domain_filter`: all rows
    on filtered entities drop and one placeholder row per (state, domain)
    surfaces instead, carrying a definition_text note.

    Returns `(records, assembly)`. `assembly` is built via the shared
    `compute_coverage` for symmetry with source-driven outputs.
    """
    from src.ingest.domain_filter import entity_filter_domain, placeholder_note
    from src.utils.matching import record_match_keys

    alias_sink: dict[tuple[str, str], set[tuple[str, str]]] = {}
    tier_sink: dict[tuple[str, str], str] = {}
    spine_emits = canonical_spine_emit_keys(
        spine, _alias_sink=alias_sink, _tier_sink=tier_sink
    )
    records: list[ElementRecord] = []
    filtered_labels: set[str] = set()
    # Issue #213 item 2 — documented-match provenance: which alias TIER
    # produced each hit, so the gap log can report how much of the match
    # rate rides on the fuzziest grammar tiers.
    alias_tier_histogram: dict[str, int] = {}

    for emit in spine_emits:
        label = entity_filter_domain(emit.entity, spine)

        facts: SourceFacts | None = None
        # The alias sink populated by canonical_spine_emit_keys carries
        # the richer element_keys-style aliases (bare-prefix refs,
        # EducationOrganization subtype expansion, sub-entity
        # Pascal/camel/tail, MDE bare-form descriptor) as match keys
        # without inflating emit cardinality. Fall back to record_match_keys
        # for the primary form only.
        slot = (emit.entity.lower(), emit.element_name.lower())
        candidates = alias_sink.get(slot) or record_match_keys(emit.entity, emit.element_name)
        # Sort aliases so first-hit is deterministic across processes.
        matched_alias: tuple[str, str] | None = None
        for alias_key in sorted(candidates):
            if alias_key in source_index:
                facts = source_index[alias_key]
                matched_alias = alias_key
                break

        documented = facts is not None
        if matched_alias is not None:
            tier = tier_sink.get(matched_alias, "primary")
            alias_tier_histogram[tier] = alias_tier_histogram.get(tier, 0) + 1
        # SIS-never-populated filter only collapses UNDOCUMENTED slots;
        # if the state explicitly documented an Assessment / Survey /
        # LearningStandard / Gradebook / Intervention element, retain the
        # row as a real signal of state intent. The placeholder still
        # emits when any slot was dropped, so reviewers see "domain
        # acknowledged + N retained + remainder collapsed".
        if label is not None and not documented:
            filtered_labels.add(label)
            continue

        if facts is None:
            facts = SourceFacts()

        records.append(ElementRecord(
            state=state,
            edfi_version=edfi_version,
            # Issue #184: `domain` is the Source Area, NOT the Ed-Fi domain.
            # For spine-driven rows that means the source row's area when
            # documented (carried via SourceFacts.domain) and "" when the
            # source doc never mentioned this spine position. The Ed-Fi
            # domain lives in `edfi_domain` (stamped below).
            domain=facts.domain,
            entity=emit.entity,
            element_name=emit.element_name,
            data_type=emit.data_type,
            source=emit.source,
            extension_name=emit.extension_name,
            documented=documented,
            edfi_standard_definition=emit.description or None,
            definition_text=facts.definition_text,
            business_rules_text=facts.business_rules_text,
            element_specific_rules=facts.element_specific_rules,
            regulatory_citations=facts.regulatory_citations,
            related_entities=facts.related_entities,
            descriptor_table_code=facts.descriptor_table_code,
            descriptor_table_values=facts.descriptor_table_values,
            collections_text=facts.collections_text,
            source_document=(
                facts.source_document if facts.source_document
                else source_document
            ),
            source_page_or_section=facts.source_page_or_section,
        ))

    # Hybrid append: source rows the spine didn't cover (the TX 90-row
    # audit trail). Preserve their attribution (source="unknown") and mark
    # documented — they came from the source doc. Filtered-domain rows
    # are kept here too: documented intent in any of the filtered domains
    # survives, the placeholder only collapses the noise of undocumented
    # spine positions.
    for r in spine_missing_source_rows:
        records.append(r.model_copy(update={"documented": True}))

    # Spine-lens filter: Reference / Collection wrapper data types are
    # excluded per the reviewer posture. `canonical_spine_emit_keys`
    # already drops them from the canonical walk; this final pass
    # catches hybrid-append source rows whose data_type landed as
    # Reference/Collection (e.g., WI source rows naming a sub-collection
    # wrapper that the spine didn't enumerate under that exact name).
    records = [r for r in records if r.data_type not in ("Reference", "Collection")]

    # One placeholder per filtered domain label. Sorted so output order
    # is reproducible across runs and test snapshots.
    for label in sorted(filtered_labels):
        records.append(ElementRecord(
            state=state,
            edfi_version=edfi_version,
            # Filtered placeholders are synthetic and self-identifying
            # (source="filtered", element_name="(filtered)"). `domain` keeps
            # the PLACEHOLDER_LABEL by contract (the scoring filter +
            # test_spine_lens_filtered_rows_are_placeholders key on it). The
            # label IS the collapsed Ed-Fi domain, so `edfi_domain` mirrors
            # it; stamp_edfi_domains skips source="filtered" rows so this
            # explicit value survives.
            domain=label,
            edfi_domain=label,
            entity=f"{label} (filtered)",
            element_name="(filtered)",
            data_type=None,
            source="filtered",
            documented=False,
            definition_text=placeholder_note(label),
            source_document=source_document,
        ))

    stamp_edfi_domains(records, spine)
    assembly = compute_coverage(records, spine)
    assembly.alias_tier_histogram = dict(sorted(alias_tier_histogram.items()))
    return records, assembly


def write_dual_lens_artifacts(
    *,
    state: str,
    spine: StateSpine,
    records: list[ElementRecord],
    assembly: SourceAssembly,
    recovered: list[dict[str, str]],
    source_document: str,
    source_out: Path,
    spine_out: Path,
    gap_out: Path,
    spine_path: Path,
    spine_source_rel: str,
    source_coverage_note: str,
    spine_coverage_note: str,
    gap_log_extra: dict[str, Any] | None = None,
    coverage_log_suffix: str = "",
    coverage_log_suffix_args: tuple[Any, ...] = (),
    logger: logging.Logger | None = None,
) -> None:
    """The shared adapter ``run()`` tail (issue #213 item 3).

    Every state adapter previously carried this ~80-line sequence
    verbatim: build the spine-driven (Option 3b dual-lens) artifact,
    write both lens artifacts, write the gap log, then hand off to the
    issue #70 swagger-as-source backfill. A sixth state now copies
    nothing.

    Parameterized divergences (everything else was byte-identical
    across the five adapters):

    - ``source_document`` — each state's provenance label.
    - ``source_coverage_note`` / ``spine_coverage_note`` — per-state
      gap-log prose.
    - ``gap_log_extra`` — TX merges ``source_attribution`` +
      ``provenance`` into its gap log (``build_gap_log(extra=...)``).
    - ``coverage_log_suffix`` / ``coverage_log_suffix_args`` — TX
      appends the attribution counts to the coverage log line.
    - ``logger`` — the adapter's own module logger so log records keep
      their historical logger names (defaults to this module's).

    Paths are passed in (not derived) so the adapters keep handing over
    the same module-level ``_XX_*_OUT`` constants their tests
    monkeypatch; ``spine_source_rel`` likewise stays caller-computed
    (``str(SPINE_PATH.relative_to(PROJECT_ROOT))`` — exact prior
    behavior, including the ValueError on a non-repo path).
    """
    log = logger if logger is not None else logging.getLogger(__name__)

    # -- Spine-driven artifact (Option 3b dual-lens, Phase 3) --
    source_index = build_source_index(records)
    spine_records, spine_assembly = assemble_spine_driven(
        spine=spine,
        source_index=source_index,
        spine_missing_source_rows=assembly.unmatched_records,
        state=state,
        edfi_version=spine.edfi_version,
        source_document=source_document,
    )
    spine_state_elements = StateElements(
        state=state,
        edfi_version=spine.edfi_version,
        extracted_at=datetime.now(timezone.utc),
        element_count=len(spine_records),
        elements=spine_records,
    )
    spine_out.parent.mkdir(parents=True, exist_ok=True)
    spine_out.write_text(
        spine_state_elements.model_dump_json(indent=2), encoding="utf-8"
    )
    documented_count = sum(1 for r in spine_records if r.documented)
    log.info(
        "Wrote %s (%d elements, spine lens — %d documented, %d undocumented)",
        spine_out,
        len(spine_records),
        documented_count,
        len(spine_records) - documented_count,
    )

    state_elements = StateElements(
        state=state,
        edfi_version=spine.edfi_version,
        extracted_at=datetime.now(timezone.utc),
        element_count=len(records),
        elements=records,
    )
    source_out.parent.mkdir(parents=True, exist_ok=True)
    source_out.write_text(state_elements.model_dump_json(indent=2), encoding="utf-8")
    log.info("Wrote %s (%d elements)", source_out, len(records))

    gap_log = build_gap_log(
        state=state,
        spine=spine,
        spine_source_rel=spine_source_rel,
        assembly=assembly,
        # Spine-lens documented-match provenance (issue #213 item 2;
        # the five per-adapter lines collapsed here at the #235 rebase).
        alias_tier_histogram=spine_assembly.alias_tier_histogram,
        recovered=recovered,
        source_coverage_note=source_coverage_note,
        spine_coverage_note=spine_coverage_note,
        extra=gap_log_extra,
    )
    gap_out.write_text(json.dumps(gap_log, indent=2), encoding="utf-8")
    log.info(
        "Wrote %s. Source coverage: %d/%d (%.1f%%) | Spine coverage: "
        "%d/%d (%.1f%%)" + coverage_log_suffix,
        gap_out,
        gap_log["source_coverage"]["matched"],
        gap_log["source_coverage"]["total"],
        gap_log["source_coverage"]["pct"],
        gap_log["spine_coverage"]["matched_unique_keys"],
        gap_log["spine_coverage"]["total_spine_keys"],
        gap_log["spine_coverage"]["pct"],
        *coverage_log_suffix_args,
    )

    # Issue #70 — swagger-as-source backfill. Treat swagger publication as
    # state documentation when the primary source is silent on a whole
    # entity. Pass the same module-level paths the adapter just wrote to so
    # monkey-patched test runs hit tmp paths instead of the real data/out
    # artifacts.
    from src.ingest.swagger_backfill import backfill

    backfill(
        state,
        src_path=source_out,
        spine_lens_path=spine_out,
        spine_path=spine_path,
    )
