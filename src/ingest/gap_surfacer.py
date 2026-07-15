"""Spine-anchored coverage-gap surfacer (issue #66 Layer 2).

Walks ``{state}_elements_source.json`` + ``{state}_spine.json`` and emits a
sibling artifact ``{state}_elements_gap.json`` enumerating every spine
``(entity, element)`` pair the state's source doc is silent on. Two
``discovery`` classes:

- ``spine_within_documented_entity`` — entity DOES appear in the source
  doc but this specific element is missing from the source's enumeration.
- ``spine_only_full_entity`` — entity does not appear in the source doc at
  all (typically TEA / WI / MN extension entities the state's source-doc
  scrape didn't reach).

Methodology discipline (CLAUDE.md "Scope guardrails"):

- The surfacer is **a separate artifact**. It does NOT mutate
  ``{state}_elements_source.json`` — the source-lens contract ("the
  state's source doc enumerates what's in scope; spine enriches") is
  preserved by construction.
- The surfacer never reads the reviewer xlsx. The reviewer pass
  oracled this gap shape into focus (issue #62 → #66) but is not a
  data input — gap output depends only on spine + source-lens output.
- A consumer that treats the gap file as in-scope is making an
  explicit choice. Source-lens percentages stay comparable across
  states because the gap rows live elsewhere.

Output schema (one record per gap pair):

```jsonc
{
  "state": "TX",
  "entity": "tx_priorYearLeaver",
  "element_name": "DiplomaType",
  "discovery": "spine_only_full_entity",
  "documented_in_source": false,
  "spine_data_type": "Descriptor",
  "spine_extension_name": "tx_priorYearLeaver",
  "rationale": "Entity not enumerated in source doc; spine catalog confirms TEA extension entity"
}
```

CLI: ``poc3 ingest gap --state STATE`` (or ``--state all``). Outputs land
at ``data/out/{state}_elements_gap.json``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.ingest.domain_filter import entity_filter_domain
from src.ingest.shared import canonical_type
from src.models.alias_grammar import (
    capitalize_first,
    fk_prefixed_alias,
    reference_prefix,
)
from src.models.element import StateElements
from src.models.spine import StateSpine
from src.utils.matching import (
    match_key,
    entity_match_form,
    record_match_keys,
)
from src.utils.paths import (
    state_elements_gap_path,
    state_elements_path,
    state_spine_path,
)

logger = logging.getLogger(__name__)

# discovery classes — keep short, machine-readable; consumers may switch on
# these directly.
DISCOVERY_WITHIN = "spine_within_documented_entity"
DISCOVERY_FULL_ENTITY = "spine_only_full_entity"


# `schoolId` → `SchoolId` — one rule home (alias_grammar; issue #213
# item 2). Kept under the historical local name for the concat call sites.
_capitalize_first = capitalize_first


def _source_keys(records: Iterable) -> set[tuple[str, str]]:
    """Build the alias-expanded source-side membership set.

    For each source record ``r``, expand ``record_match_keys(r.entity,
    r.element_name)`` into ``(normalized_entity, lowered_alias)`` pairs.
    Used to test whether a spine pair is already documented under any
    naming convention the state's source doc emitted.
    """
    keys: set[tuple[str, str]] = set()
    for r in records:
        keys |= record_match_keys(r.entity, r.element_name)
    return keys


def _source_entities(records: Iterable) -> set[str]:
    """Set of normalized entity names the source doc touched at all.

    Drives the discovery classification: if the entity appears here, gap
    rows for that entity are ``spine_within_documented_entity``;
    otherwise ``spine_only_full_entity``.
    """
    return {entity_match_form(r.entity) for r in records}


def _swagger_backfilled_entities(records: Iterable) -> set[str]:
    """Set of normalized entity names whose source-lens presence is
    exclusively via swagger backfill (issue #70).

    The swagger-backfill module appends rows only for entities the source
    doc was silent on; a partial-coverage entity never triggers backfill.
    So the membership test reduces to "any row carries
    documentation_source='swagger'." Used by ``surface_gaps()`` to skip
    these entities entirely — their full property set is now claimed as
    documented (low-quality, but documented), so emitting them as
    ``spine_only_full_entity`` would double-count and emitting their
    Reference/Collection slots as ``spine_within_documented_entity``
    would mis-classify swagger-only entities as authored-prose entities.
    """
    return {
        entity_match_form(r.entity)
        for r in records
        if getattr(r, "documentation_source", "source_doc") == "swagger"
    }


def _is_documented(
    entity: str,
    element_candidates: Iterable[str],
    source_keys: set[tuple[str, str]],
) -> bool:
    """Return True if any candidate (entity, element) form is in source_keys.

    ``element_candidates`` is an ordered set of equivalent source-side
    representations the spine primary may map to (bare leaf, FK concat,
    sub-collection concat). Any one match marks the spine slot
    documented.
    """
    for cand in element_candidates:
        e_norm, n_norm = match_key(entity, cand)
        if (e_norm, n_norm.lower()) in source_keys:
            return True
        # Also test record_match_keys-expanded aliases — covers
        # path-tail / arrow-nav variants the source might have emitted.
        for ent_n, alias_lower in record_match_keys(entity, cand):
            if (ent_n, alias_lower) in source_keys:
                return True
    return False


def _emit_gap(
    state: str,
    entity: str,
    element_name: str,
    *,
    discovery: str,
    spine_data_type: str,
    spine_extension_name: str | None,
    rationale: str,
    sub_collection: str | None = None,
    leaf_name: str | None = None,
) -> dict:
    """Build a gap record dict.

    ``element_name`` is the canonical source-side form (concat for
    sub-collection elements). ``sub_collection`` + ``leaf_name`` carry
    the deconstructed parts so the reviewer-comparison lookup can
    register both the concat and bare-leaf aliases — without that,
    reviewer rows like ``PriorYearLeaver / DiplomaType`` would never
    resolve to the gap row whose concat name is
    ``graduationSetDiplomaTypeDescriptor``.
    """
    record = {
        "state": state,
        "entity": entity,
        "element_name": element_name,
        "discovery": discovery,
        "documented_in_source": False,
        "spine_data_type": spine_data_type,
        "spine_extension_name": spine_extension_name,
        "rationale": rationale,
    }
    if sub_collection:
        record["sub_collection"] = sub_collection
    if leaf_name:
        record["leaf_name"] = leaf_name
    return record


def _classify_discovery(
    entity: str, source_entities: set[str]
) -> tuple[str, str]:
    """Return ``(discovery_class, rationale_prefix)``."""
    if entity_match_form(entity) in source_entities:
        return (
            DISCOVERY_WITHIN,
            "Entity enumerated in source doc but element missing from its row set",
        )
    return (
        DISCOVERY_FULL_ENTITY,
        "Entity not enumerated in source doc; spine catalog confirms canonical position",
    )


def surface_gaps(
    state: str,
    elements_path: Path | None = None,
    spine_path: Path | None = None,
) -> list[dict]:
    """Walk spine catalog vs source-lens output; return gap records (newest API).

    Pure function — does not write to disk. Public so tests can drive it
    with synthetic spines / fixtures without involving the filesystem.
    The CLI wrapper ``run()`` handles disk IO and aggregates per-state
    results into ``state_elements_gap_path(state)``.
    """
    elements_p = elements_path or state_elements_path(state, "source")
    spine_p = spine_path or state_spine_path(state)

    elements = StateElements.model_validate_json(
        elements_p.read_text(encoding="utf-8")
    )
    spine = StateSpine.model_validate_json(spine_p.read_text(encoding="utf-8"))

    src_keys = _source_keys(elements.elements)
    src_ents = _source_entities(elements.elements)
    swagger_ents = _swagger_backfilled_entities(elements.elements)

    gaps: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (entity_lower, element_lower)

    # Apply the same SIS-never-populated domain filter as the spine-lens
    # placeholder collapse — entities in Assessment / Survey / Standards /
    # Gradebook / Intervention are not authentic gap signal because no
    # SIS vendor populates them. Treat them as out-of-scope here so the
    # gap artifact stays focused on the ingestion surface that matters
    # to scoring.
    def _filtered(entity_name: str) -> bool:
        return entity_filter_domain(entity_name, spine) is not None

    def _swagger_only(entity_name: str) -> bool:
        """True when the entity's source-lens presence is exclusively
        swagger-backfilled (issue #70). Skip it in the gap surface — the
        backfill claims the whole entity, so no within-entity gap rows
        should be emitted.
        """
        return entity_match_form(entity_name) in swagger_ents

    def _track(entity: str, element_name: str) -> bool:
        """Return True if this (entity, element) was not previously emitted."""
        key = (entity.lower(), element_name.lower())
        if key in seen:
            return False
        seen.add(key)
        return True

    def _document_check(
        entity: str,
        element_name: str,
        also: list[str] | None = None,
    ) -> bool:
        candidates = [element_name]
        if also:
            candidates.extend(also)
        return _is_documented(entity, candidates, src_keys)

    # -- core entities --------------------------------------------------
    for entity_name, entity in spine.catalog.entities.items():
        if _filtered(entity_name):
            continue
        if _swagger_only(entity_name):
            continue
        # Top-level scalar/descriptor properties.
        for prop_name, prop in entity.properties.items():
            if _document_check(entity_name, prop_name):
                continue
            if not _track(entity_name, prop_name):
                continue
            disc, rat = _classify_discovery(entity_name, src_ents)
            gaps.append(
                _emit_gap(
                    state,
                    entity_name,
                    prop_name,
                    discovery=disc,
                    spine_data_type=canonical_type(
                        prop_name, prop.type, prop.format
                    ),
                    spine_extension_name=None,
                    rationale=rat,
                )
            )

        # References + their key properties. We treat the reference as
        # documented if the source touched any of its FK projections —
        # source docs commonly enumerate the leaf FK rather than the
        # reference object itself.
        for ref_name, ref in entity.references.items():
            ref_concat_aliases: list[str] = []
            prefix = reference_prefix(ref_name)
            # Deliberately NARROWER than `element_keys` (issue #213 item
            # 2, documented divergence): no camel-collapse, no
            # UniqueId→Id, no EdOrg subtypes, no descriptor variants —
            # so the gap artifact can classify a slot undocumented that
            # coverage counted matched. Widening is a measured follow-up
            # (see the alias-tier histogram in the gap log). The
            # `ref_name + CapKey` form below is this consumer's OWN
            # extra (`schoolReferenceSchoolId`) — no other consumer
            # emits it.
            for kp in ref.key_properties:
                ref_concat_aliases.append(kp)
                if prefix and prefix != kp:
                    ref_concat_aliases.append(fk_prefixed_alias(prefix, kp))
                    ref_concat_aliases.append(fk_prefixed_alias(ref_name, kp))

            if _document_check(entity_name, ref_name, also=ref_concat_aliases):
                continue
            if not _track(entity_name, ref_name):
                continue
            disc, rat = _classify_discovery(entity_name, src_ents)
            gaps.append(
                _emit_gap(
                    state,
                    entity_name,
                    ref_name,
                    discovery=disc,
                    spine_data_type="Reference",
                    spine_extension_name=None,
                    rationale=rat,
                )
            )

        # Sub-collections — emit one gap per sub-collection property
        # (most analyst-relevant grain) using the source-side concat form
        # as the canonical element name so it lines up with how
        # state-adapter ingest emits sub-collection rows.
        for sub_name, sub in entity.sub_collections.items():
            for sp_name, sp in sub.properties.items():
                concat = sub_name + _capitalize_first(sp_name)
                if _document_check(
                    entity_name, sp_name, also=[concat, sub_name]
                ):
                    continue
                if not _track(entity_name, concat):
                    continue
                disc, rat = _classify_discovery(entity_name, src_ents)
                gaps.append(
                    _emit_gap(
                        state,
                        entity_name,
                        concat,
                        discovery=disc,
                        spine_data_type=canonical_type(
                            sp_name, sp.type, sp.format
                        ),
                        spine_extension_name=None,
                        rationale=rat,
                        sub_collection=sub_name,
                        leaf_name=sp_name,
                    )
                )

    # -- extensions -----------------------------------------------------
    # Extensions attribute back to ``ext.extends_entity``. The discovery
    # classification uses ``extends_entity`` for the "is the entity in
    # source?" test, but the emitted record carries ``extends_entity`` as
    # the entity name (matching the source-lens convention) and
    # ``ext_key`` as the spine_extension_name attribution.
    for ext_key, ext in spine.catalog.extensions.items():
        target = ext.extends_entity
        if _filtered(target):
            continue
        if _swagger_only(target):
            continue
        for prop_name, prop in ext.properties.items():
            if _document_check(target, prop_name):
                continue
            if not _track(target, prop_name):
                continue
            disc, rat = _classify_discovery(target, src_ents)
            # Extension attribution upgrades the rationale prefix —
            # surfaces the extension key explicitly so analysts know
            # which schema contributed.
            rat_ext = (
                f"{rat}; contributed by extension {ext_key}"
                if disc == DISCOVERY_WITHIN
                else (
                    "Entity not enumerated in source doc; spine catalog "
                    f"confirms extension entity {ext_key}"
                )
            )
            gaps.append(
                _emit_gap(
                    state,
                    target,
                    prop_name,
                    discovery=disc,
                    spine_data_type=canonical_type(
                        prop_name, prop.type, prop.format
                    ),
                    spine_extension_name=ext_key,
                    rationale=rat_ext,
                )
            )

        # Extension references (e.g. TEA `tx_priorYearLeaver` redeclares
        # `studentReference`). Treat documented if the FK leaf is in
        # source; otherwise emit.
        for ref_name, ref in ext.references.items():
            ref_concat_aliases: list[str] = []
            prefix = reference_prefix(ref_name)
            # Deliberately NARROWER than `element_keys` (issue #213 item
            # 2, documented divergence): no camel-collapse, no
            # UniqueId→Id, no EdOrg subtypes, no descriptor variants —
            # so the gap artifact can classify a slot undocumented that
            # coverage counted matched. Widening is a measured follow-up
            # (see the alias-tier histogram in the gap log). The
            # `ref_name + CapKey` form below is this consumer's OWN
            # extra (`schoolReferenceSchoolId`) — no other consumer
            # emits it.
            for kp in ref.key_properties:
                ref_concat_aliases.append(kp)
                if prefix and prefix != kp:
                    ref_concat_aliases.append(fk_prefixed_alias(prefix, kp))
                    ref_concat_aliases.append(fk_prefixed_alias(ref_name, kp))

            if _document_check(target, ref_name, also=ref_concat_aliases):
                continue
            if not _track(target, ref_name):
                continue
            disc, rat = _classify_discovery(target, src_ents)
            rat_ext = (
                f"{rat}; contributed by extension {ext_key}"
                if disc == DISCOVERY_WITHIN
                else (
                    "Entity not enumerated in source doc; spine catalog "
                    f"confirms extension entity {ext_key}"
                )
            )
            gaps.append(
                _emit_gap(
                    state,
                    target,
                    ref_name,
                    discovery=disc,
                    spine_data_type="Reference",
                    spine_extension_name=ext_key,
                    rationale=rat_ext,
                )
            )

        # Extension sub-collections.
        for sub_name, sub in ext.sub_collections.items():
            for sp_name, sp in sub.properties.items():
                concat = sub_name + _capitalize_first(sp_name)
                if _document_check(
                    target, sp_name, also=[concat, sub_name]
                ):
                    continue
                if not _track(target, concat):
                    continue
                disc, rat = _classify_discovery(target, src_ents)
                rat_ext = (
                    f"{rat}; contributed by extension {ext_key}"
                    if disc == DISCOVERY_WITHIN
                    else (
                        "Entity not enumerated in source doc; spine "
                        f"catalog confirms extension entity {ext_key}"
                    )
                )
                gaps.append(
                    _emit_gap(
                        state,
                        target,
                        concat,
                        discovery=disc,
                        spine_data_type=canonical_type(
                            sp_name, sp.type, sp.format
                        ),
                        spine_extension_name=ext_key,
                        rationale=rat_ext,
                        sub_collection=sub_name,
                        leaf_name=sp_name,
                    )
                )

    # Stable order: by (entity, element) so re-runs produce byte-identical
    # output across processes (avoids hash-order churn in PYTHONHASHSEED).
    gaps.sort(key=lambda g: (g["entity"], g["element_name"]))
    return gaps


def write_gap_artifact(state: str, gaps: list[dict]) -> Path:
    """Write ``{state}_elements_gap.json`` with the standard envelope."""
    out_path = state_elements_gap_path(state)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    discovery_counts: dict[str, int] = {}
    for g in gaps:
        discovery_counts[g["discovery"]] = (
            discovery_counts.get(g["discovery"], 0) + 1
        )
    payload = {
        "state": state.upper(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gap_count": len(gaps),
        "discovery_counts": discovery_counts,
        "gaps": gaps,
    }
    out_path.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    logger.info("Wrote %d gap rows to %s", len(gaps), out_path)
    return out_path


def run(state: str) -> Path:
    """CLI entry — surface gaps for a single state and write the artifact.

    Plain function (NOT @click.command) per the CLI-wiring convention in
    CLAUDE.md / `tests/test_cli_e2e.py::TestCliWiring`. The Click command
    in `cli.py` calls this.
    """
    gaps = surface_gaps(state.upper())
    return write_gap_artifact(state.upper(), gaps)
