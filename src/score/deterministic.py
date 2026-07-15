"""Deterministic facts — computed from ElementRecord (+ spine where noted).

Plan §6.2 / §6.4 — no LLM, no cache, no network.

**Record-only (spine-lens + any lens, no context needed):**

- ``definition_present``            — ``bool(record.definition_text.strip())``
- ``business_rules_present``        — ``bool(record.business_rules_text.strip())``
- ``data_type_canonical``           — ``record.data_type`` is in the canonical
                                      Ed-Fi type set
- ``descriptor_values_enumerated``  — record is a descriptor row AND its
                                      narrative enumerates the value set
                                      (Phase C gate — see below)

**Source-lens-specific (require a ``FactContext`` carrying the state's spine):**

- ``element_name_matches_canonical``  — exact case-sensitive match between
                                        ``record.element_name`` and the
                                        spine's canonical element name.
- ``naming_deviation_cosmetic``       — no exact match, but the deviation
                                        is trivial (casing or plural).
- ``extension_mirrors_core_pattern``  — source=extension row whose element
                                        name also appears as a core spine
                                        emit (candidate for "unnecessary
                                        extension" in plan §7.1).
- ``is_natural_key``                  — record maps to a spine slot flagged
                                        ``is_identity=True`` (direct property
                                        or FK key-property). Guards NACHOS
                                        tier-3 concatenation against natural-
                                        key format specs.

**Integration Profile — Structural Depth axis (spine-backed,
lens-independent):** five count-valued facts that feed the
``structural_depth`` dimension (plan
``docs/archive/nachos-v2-two-axis-plan.md`` §3.1). All derive from the
spine catalog — never from narrative — so their values cannot be
confounded by doc culture.

- ``fk_chain_depth``          — per-entity depth in the reference
                                graph (0 = root entity, N = N FK hops
                                from a root). Broadcast to every
                                element of the entity.
- ``reference_fan_out``       — per-entity count of *other* entities
                                that reference this one. Broadcast to
                                every element of the entity.
- ``sub_collection_depth``    — 1 when the (entity, element) slot sits
                                inside one of the entity's declared
                                sub-collections; 0 otherwise.
- ``descriptor_enum_breadth`` — record-only count of
                                ``descriptor_table_values`` entries.
                                0 for non-descriptor rows.
- ``entity_extension_footprint`` — per-entity count of catalog
                                extensions that extend this entity.
                                Broadcast to every element.

Artifact shape matches the LLM-fact artifact exactly (same header
fields, same per-row keys). The header's ``mode`` field distinguishes:
``"deterministic"`` vs ``"api"`` / ``"dry-run"``. This means Phase C's
rule stage doesn't care how a fact was produced — it reads the same
JSONL shape regardless.

Naming caveat — ``llm_value`` is a misnomer on this path. It is a
*shared-schema* key, named for the LLM artifacts where it carries the
model's raw pre-validation claim. On the deterministic path **no model
runs**: ``llm_value`` holds the Python computation's result and
``validated_value`` mirrors it exactly (deterministic facts are exact,
so there is nothing for the span validator to downgrade — hence
``confidence`` is always ``"high"`` and ``downgrade_reason`` always
``None``). The authoritative "how was this produced?" tell is the
``model`` / ``mode`` field (``"deterministic"``), NOT the key name.
Scoring keys on ``validated_value``; ``llm_value`` is audit-only here.
The key is left unrenamed so the LLM and deterministic paths stay
byte-for-byte shape-identical for the Phase C reader.

These facts exist to (a) smoke-test the multi-fact artifact contract
before LLM spend, (b) be consumable by the Phase C rule stage as-is,
and (c) let the Phase B cross-lens QA check (§8) compare them across
lenses cheaply.

``descriptor_values_enumerated`` landed in Phase C to gate two Phase D
soft-FP clusters surfaced by the Phase B full-extraction run:

- WI descriptor-value-table hallucinations (21 rows downgraded on
  ``definition_is_implementable``) — WI's Confluence scraper emits the
  descriptor value table as pipe-delimited spans inside
  ``business_rules_text``; the LLM synthesizes them into single-line
  quotes that don't survive substring validation.
- MN program-type descriptor rows with bare enumeration spans like
  ``Extension.ProgramTypeDescriptor = 'EE-ECFE', 'EE-SR', ...`` — fact
  4 (``populations_or_scope_stated``) carried several as medium-conf
  True because enumerated-value-set language mimics scope language.

In both cases the right call is to treat "definition enumerates its
value set" as a structural signal, not to retune narrative prompts.
The rule stage downweights fact-1/4/5 contributions on rows where this
fact fires (see ``src.score.rules``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.models.element import ElementRecord
from src.models.spine import StateSpine
# Shared atomic JSONL writer (issue #213 item 3) — historical name kept.
from src.score.dispatch import write_jsonl_artifact as _write_artifact
from src.score.extract import load_phase_a_records
from src.utils.paths import (
    scoring_phase_a_artifact_path,
    state_spine_path,
)

_LOGGER = logging.getLogger(__name__)


# Facts whose computation reads only the record — safe to call without a
# ``FactContext`` and used by cross-lens QA since the computation is
# lens-symmetric. Phase B shipped four; Phase C2 adds the length-based
# ``definition_text_substantive`` gate used by source-lens
# ``definition_quality`` (plan §7.1) — record-only, so eligible here.
LENS_INDEPENDENT_FACTS: tuple[str, ...] = (
    "definition_present",
    "element_narrative_present",
    "business_rules_present",
    "data_type_canonical",
    "descriptor_values_enumerated",
    "definition_text_substantive",
    "descriptor_enum_breadth",
    "element_only_parent_entity_gate",
)

# Source-lens deterministic facts (plan §6.1 / §6.4). ``definition_present``,
# ``element_narrative_present`` and ``definition_text_substantive`` are
# shared with the lens-independent set (record-only); the remaining four
# need spine access to look up canonical slot names, cross-entity patterns,
# and natural-key identity.
SOURCE_DETERMINISTIC_FACTS: tuple[str, ...] = (
    "definition_present",
    "element_narrative_present",
    "definition_text_substantive",
    "element_name_matches_canonical",
    "naming_deviation_cosmetic",
    "extension_mirrors_core_pattern",
    "is_natural_key",
    # v25 / det.v11 (issue #124 PR 2 / #111, 2026-05-02): name-stem +
    # data-type-bucket comparison between an extension and core
    # elements on the same entity. Surfaced as a typed-reason
    # annotation in the v12 SF fold label inside
    # ``aggregate._compute_nachos_adjustments``.
    "extension_fidelity_divergence",
)

# Spine-backed deterministic facts shared by both lenses' rule stages.
# Separate from ``LENS_INDEPENDENT_FACTS`` because these require a
# ``FactContext`` built from the state's spine. ``is_natural_key``
# landed in PR #17; the four v2 structural-complexity facts land here
# alongside it so both lenses see the same structural signal.
SHARED_CONTEXT_FACTS: tuple[str, ...] = (
    "is_natural_key",
    "fk_chain_depth",
    "reference_fan_out",
    "sub_collection_depth",
    "entity_extension_footprint",
    "element_is_bare_fk_reference",
)


# Union — used by dispatch checks in ``extract.py`` and ``runner.py``.
# Ordering preserves LENS_INDEPENDENT_FACTS first, then source-only
# additions, so artifacts written in catalog order are deterministic.
DETERMINISTIC_FACTS: tuple[str, ...] = (
    *LENS_INDEPENDENT_FACTS,
    "element_name_matches_canonical",
    "naming_deviation_cosmetic",
    "extension_mirrors_core_pattern",
    "extension_fidelity_divergence",
    *SHARED_CONTEXT_FACTS,
)

# Canonical Ed-Fi data type set. Must stay aligned with
# `src.ingest.shared.canonical_type()` — a new canonical form added
# there needs a matching entry here, or genuine new types will
# silently score False for ``data_type_canonical``. Empty string and
# ``None`` are NOT canonical (indicates unresolved / unmatched).
#
# Observed in real data (AZ/WI/MN/TX spine-lens documented rows,
# 2026-04-22): Boolean, Date, DateTime, Decimal, Descriptor, Integer,
# Number, String. Reference/Collection are filtered at spine-emit time
# but kept here for completeness in case that policy changes.
CANONICAL_DATA_TYPES: frozenset[str] = frozenset({
    "String",
    "Integer",
    "Number",
    "Decimal",
    "Boolean",
    "Date",
    "DateTime",
    "Time",
    "Descriptor",
    "Reference",
    "Collection",
    "Array",
})

# Bump if rule logic changes. Cache-free by design, so the version
# lives here rather than on disk — runners pass it through to the
# artifact header for Phase C's reproducibility ledger.
# det.v11 — issue #124 PR 2 / issue #111 (2026-05-02): adds
# ``extension_fidelity_divergence``. Returns ``"replaces_core_field_shape"``
# iff a ``source='extension'`` row's element-name stem (after stripping
# common Ed-Fi suffixes like ``Descriptor``/``Id``/``UniqueId``/``Reference``)
# shares a 5+ char case-insensitive prefix with a CORE element on the
# same entity AND their data-type buckets differ (e.g., Boolean vs
# Descriptor, String vs Descriptor). Surfaced as a typed-reason
# annotation in the v12 SF fold label inside
# ``aggregate._compute_nachos_adjustments``. Source-lens only by
# registration (``SOURCE_DETERMINISTIC_FACTS``); spine-lens callers
# don't consume it because ``sf_dim`` is None there.
# det.v10 — issue #97 (2026-04-30): adds
# ``element_only_parent_entity_gate``. True iff the element name is FK-
# shaped (ends in Id/UniqueId/Reference, case-insensitive) AND the
# definition_text contains a parent-entity submission gate annotation
# (``[Public: <code>, Choice: <code>]`` — WI Confluence layout) AND
# element_specific_rules is empty. ``business_logic_complexity`` and
# ``nachos_score`` read it to suppress the ``has_conditional_logic``
# contribution on rows where the only conditional evidence is the parent-
# record submission gate, not an element-level value/presence rule. The
# annotation pattern is WI-Confluence-specific by construction; AZ/MN/TX
# narratives do not carry it, so the fact is naturally inert there.
# det.v9 — issue #63 B5 (POC interim methodology call, 2026-04-29):
# adds ``element_is_bare_fk_reference`` (element_name matches a known
# spine entity name AND no element-specific narrative AND not a
# self-reference). Used by ``nachos_score`` to suppress the
# ``has_cross_entity_logic`` contribution to the NACHOS tier when the
# row is a bare FK reference. The det.v8 ``element_narrative_present``
# fact added in v14 (B1) is RETAINED — now consumed by
# ``element_is_bare_fk_reference`` here, even though B1's adj-layer
# bare-FK gate was dropped under B3 blanket (v16).
# det.v8 — issue #63 B1 (2026-04-29): adds ``element_narrative_present``
# (definition_text OR element_specific_rules — excludes shared
# entity-level business_rules_text). Originally drove aggregate.py's
# bare-FK low-confidence gate; under B3 blanket (v16) the gate is
# dropped but the fact is preserved as input to B5's bare-FK gate.
DETERMINISTIC_VERSION: str = "det.v11"


# Enumeration-signal patterns for ``descriptor_values_enumerated``. All
# three fire off narrative fields only — ``descriptor_table_values`` is
# checked structurally beforehand. Patterns kept narrow on purpose:
# broader matches (e.g. any single comma between quoted tokens) would
# over-gate and hide legitimate prose-style descriptor definitions that
# the LLM actually handles cleanly.
_DESCRIPTOR_ENUM_LITERALS: tuple[str, ...] = (
    "Descriptor values:",  # WI Confluence scraper prefix
    "Enumeration:",        # MN Mapping Matrix prefix
)
# Three pipes in close proximity — the signature WI descriptor-value-
# table shape (``/calendars | uri://… | Student Specific | Student
# Specific; School | School | School …``). Avoids matching single-pipe
# incidental content (URL query strings, shell snippets).
_PIPE_ENUM_PATTERN = re.compile(r"\|[^|\n]{1,80}\|[^|\n]{1,80}\|[^|\n]{1,80}")
# Two or more adjacent quoted tokens separated by commas — the MN
# ``= 'a', 'b', 'c'`` shape. Straight and curly quotes both fold.
_QUOTED_ENUM_PATTERN = re.compile(
    r"['\u2018\u2019][^'\u2018\u2019\n]{1,60}['\u2018\u2019]\s*,\s*['\u2018\u2019][^'\u2018\u2019\n]{1,60}['\u2018\u2019]"
)
# Prose per-code descriptions — the WI ``A:Autism means...DB:Deafblind...
# EBD:Emotional behavioral disability...`` shape that carries enumerated
# descriptor values as ordinary narrative text (no pipe table, no
# literal prefix). Each match is a short code (1-5 uppercase letters +
# optional single digit), colon, then a capitalised-word label.
# Requires 3+ matches across the combined narrative so incidental
# acronym prose like ``USES:The...`` doesn't fire the gate alone. Phase D
# ``det.v5`` extension: catches WI ``disabilityDescriptor`` /
# ``raceDescriptor`` / ``shortenedSchoolDayIndicator`` whose LLM spans
# synthesised from this per-code prose list.
_PROSE_PER_CODE_PATTERN = re.compile(r"\b[A-Z]{1,5}\d?:[A-Z][a-z]")
_PROSE_PER_CODE_MIN_HITS = 3



# ---------------------------------------------------------------------------
# The three facts
# ---------------------------------------------------------------------------


def definition_present(record: ElementRecord) -> bool:
    """True iff any narrative field carries non-whitespace content.

    Aggregates ``definition_text`` + ``business_rules_text`` +
    ``element_specific_rules`` so entity-level prose is visible to the
    gate. TWEDS (TX) stores per-element documentation on the entity-shared
    ``business_rules_text`` block; checking ``definition_text`` alone
    misses 195/203 TX tier-0 rows whose docs are actually present. The
    aggregated gate is format-neutral across AZ/WI/MN/TX.
    """
    return bool(
        (record.definition_text or "").strip()
        or (record.business_rules_text or "").strip()
        or (record.element_specific_rules or "").strip()
    )


def element_narrative_present(record: ElementRecord) -> bool:
    """True iff this element has element-specific narrative prose.

    Stricter cousin of ``definition_present``: only counts
    ``definition_text`` and ``element_specific_rules`` — excludes
    ``business_rules_text``, which TX TWEDS shares across every element
    of an entity (one entity-level block carried by all child rows). The
    distinction matters for B1 (issue #63): a bare-FK reference whose
    only "documentation" is the entity-shared prose has no element-level
    signal, but ``definition_present`` would still report True. This fact
    is the gate the bare-FK low-confidence adjustment uses to decide
    whether the +1 unnecessary penalty has any narrative anchor.
    """
    return bool(
        (record.definition_text or "").strip()
        or (record.element_specific_rules or "").strip()
    )


def business_rules_present(record: ElementRecord) -> bool:
    """True iff ``business_rules_text`` has non-whitespace content."""
    txt = record.business_rules_text
    return bool(txt and txt.strip())


def data_type_canonical(record: ElementRecord) -> bool:
    """True iff ``data_type`` is a canonical Ed-Fi type name.

    ``None`` and empty string both score False — they signal a
    ``source="unknown"`` row that spine enrichment didn't resolve.
    """
    return record.data_type in CANONICAL_DATA_TYPES


# Threshold from plan §7.1 — a 60-char definition separates a one-line
# label ("Grade level for this enrollment") from a substantive
# description ("Indicates the grade level of the student at the time of
# this enrollment record, reported as an Ed-Fi gradeLevelDescriptor
# value…"). Chosen to match the source-lens ``definition_quality``
# tier 2 branch.
_DEFINITION_TEXT_SUBSTANTIVE_MIN = 60


def definition_text_substantive(record: ElementRecord) -> bool:
    """True iff the combined narrative passes the length gate.

    Backs the source-lens ``definition_quality`` tier 2 branch
    (plan §7.1) — "definition present but thin" vs "definition with
    substance". Aggregates ``definition_text`` + ``business_rules_text``
    + ``element_specific_rules`` for the same reason
    ``definition_present`` does — TWEDS-style entity-level prose would
    otherwise be invisible to the gate. WI 44/45 tier-1 rows on
    ``definition_quality`` lift to tier-2 with this change; TX 9/10;
    AZ 13/126; MN 0/70 (Mapping Matrix really is metadata-thin).
    """
    parts = [
        (record.definition_text or "").strip(),
        (record.business_rules_text or "").strip(),
        (record.element_specific_rules or "").strip(),
    ]
    combined = " ".join(p for p in parts if p)
    return len(combined) >= _DEFINITION_TEXT_SUBSTANTIVE_MIN


def descriptor_enum_breadth(record: ElementRecord) -> int:
    """Count of enumerated descriptor values on this record.

    Record-only (no spine needed): returns
    ``len(record.descriptor_table_values)``. Non-descriptor rows and
    descriptor rows without an ingest-extracted value table both score
    0. The ingest adapters populate this list with one entry per
    {code, label} pair when the source document enumerates a descriptor
    value set structurally (AZ XLSX descriptor sheets, TWEDS descriptor
    codelists, MN matrix enumerations). Narrative-only enumerations —
    the ``A:Autism…DB:Deafblind`` prose patterns ``descriptor_values_
    enumerated`` picks up — don't populate the structured list, so they
    score 0 here. This is intentional: narrative enumeration is a *doc
    explicitness* signal; structural breadth is about how many distinct
    codes a vendor has to handle.
    """
    return len(record.descriptor_table_values or [])


# ---------------------------------------------------------------------------
# Spine-aware fact context (source-lens facts 1/2/10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactContext:
    """Beyond-record data the source-lens deterministic facts need.

    ``canonical_by_alias`` maps every record-match alias key to the
    spine's canonical (entity, element_name, extension_name) for that
    slot. Populated via ``build_fact_context()`` — callers share one
    context across all records for a given state.

    ``core_element_names`` is the set of element names that appear on
    any ``source='core'`` spine emit. ``core_element_names_lower`` is
    the same set case-folded, used by ``extension_mirrors_core_pattern``
    to catch PascalCase-vs-camelCase drift (AZ ``StudentUniqueId`` vs
    Ed-Fi ``studentUniqueId``; TX ``Student`` / ``School`` vs
    ``student`` / ``school``) as the same cosmetic mirror.

    ``natural_key_slots`` is the set of ``(entity, element_name)`` pairs
    whose spine property (or FK key-property) carries ``is_identity=True``
    — the entity's natural-key fields. Backs the ``is_natural_key``
    deterministic fact. Populated from both core-catalog properties /
    references / sub-collections and from state-extension additions so
    TEA / MN / WI extensions that redeclare identity fields (rare but
    allowed) surface through the same fact.

    **Integration Profile — Structural Depth maps** (plan
    ``docs/archive/nachos-v2-two-axis-plan.md`` §3.1). All four are derived from
    the spine catalog alone — no narrative input — so their signals
    carry across doc-culture variance without confound. Keyed by
    ``entity_name.lower()`` so PascalCase state-doc records and camelCase
    spine emits converge.

    - ``fk_chain_depth_by_entity``: max FK-hop distance from each
      entity to a root reporting entity in the reference graph.
    - ``reference_fan_out_by_entity``: number of distinct *other*
      entities whose references target this entity.
    - ``sub_collection_slots``: alias-expanded set of (entity, element)
      pairs that sit inside a declared sub-collection (depth 1).
    - ``entity_extension_footprint_by_entity``: number of catalog
      extensions whose ``extends_entity`` matches this entity.
    """

    canonical_by_alias: dict[tuple[str, str], tuple[str, str, str | None]]
    core_element_names: frozenset[str]
    core_element_names_lower: frozenset[str]
    natural_key_slots: frozenset[tuple[str, str]]
    # v2 structural-complexity maps default to empty so existing callers
    # that only supplied the Phase C2 / PR #17 fields keep working. A
    # context built via ``build_fact_context`` populates every field.
    fk_chain_depth_by_entity: dict[str, int] = field(default_factory=dict)
    reference_fan_out_by_entity: dict[str, int] = field(default_factory=dict)
    sub_collection_slots: frozenset[tuple[str, str]] = field(
        default_factory=frozenset
    )
    entity_extension_footprint_by_entity: dict[str, int] = field(
        default_factory=dict
    )
    # v25 / det.v11 (issue #124 PR 2 / #111): per-entity catalog of
    # CORE element (name_lower, canonical_data_type) pairs used by
    # ``extension_fidelity_divergence`` to compare an extension's name-
    # stem + data-type bucket against same-entity core counterparts.
    # Empty default so callers building a partial FactContext (tests)
    # remain compatible — the fact returns ``"none"`` when no core
    # elements exist for the record's entity.
    core_elements_by_entity: dict[str, tuple[tuple[str, str], ...]] = field(
        default_factory=dict
    )


def build_fact_context(spine: StateSpine) -> FactContext:
    """Build a ``FactContext`` from the state's spine catalog.

    Single state pass, shared across every record. Drives the source-
    lens name-match facts and the extension-mirror check.
    """
    from src.ingest.shared import canonical_spine_emit_keys
    from src.utils.matching import record_match_keys

    alias_sink: dict[tuple[str, str], set[tuple[str, str]]] = {}
    emits = canonical_spine_emit_keys(spine, _alias_sink=alias_sink)

    slot_to_emit = {
        (e.entity.lower(), e.element_name.lower()): e for e in emits
    }

    canonical_by_alias: dict[tuple[str, str], tuple[str, str, str | None]] = {}

    # Primary record-match alias keys — derived directly from each emit's
    # (entity, element_name). Added first so exact matches win.
    for emit in emits:
        canonical_target = (emit.entity, emit.element_name, emit.extension_name)
        for key in record_match_keys(emit.entity, emit.element_name):
            canonical_by_alias.setdefault(key, canonical_target)

    # Secondary aliases surfaced by ``canonical_spine_emit_keys``' sink —
    # EducationOrganization subtype expansion, descriptor -Id forms,
    # FK-prefix aliases, etc. Only filled in when the primary pass left
    # the alias unclaimed, so more-specific matches take precedence.
    for slot, aliases in alias_sink.items():
        emit = slot_to_emit.get(slot)
        if emit is None:
            continue
        canonical_target = (emit.entity, emit.element_name, emit.extension_name)
        for alias_key in aliases:
            canonical_by_alias.setdefault(alias_key, canonical_target)

    core_names = frozenset(e.element_name for e in emits if e.source == "core")
    core_names_lower = frozenset(n.lower() for n in core_names)

    natural_key_slots = _collect_natural_key_slots(spine)
    fk_chain_depth_by_entity = _collect_fk_chain_depth(spine)
    reference_fan_out_by_entity = _collect_reference_fan_out(spine)
    sub_collection_slots = _collect_sub_collection_slots(spine)
    entity_extension_footprint_by_entity = (
        _collect_entity_extension_footprint(spine)
    )
    core_elements_by_entity = _collect_core_elements_by_entity(spine)

    return FactContext(
        canonical_by_alias=canonical_by_alias,
        core_element_names=core_names,
        core_element_names_lower=core_names_lower,
        natural_key_slots=natural_key_slots,
        fk_chain_depth_by_entity=fk_chain_depth_by_entity,
        reference_fan_out_by_entity=reference_fan_out_by_entity,
        sub_collection_slots=sub_collection_slots,
        entity_extension_footprint_by_entity=entity_extension_footprint_by_entity,
        core_elements_by_entity=core_elements_by_entity,
    )


def _collect_natural_key_slots(spine: StateSpine) -> frozenset[tuple[str, str]]:
    """Walk the spine catalog and emit every ``(entity, element_name)`` pair
    whose spine declaration carries ``is_identity=True``.

    Covers three shapes:

    - **Direct properties** with ``is_identity=True`` on the entity
      (e.g., ``Calendar.calendarCode``, ``CalendarDate.date``).
    - **Reference key-properties** with ``is_identity=True`` — when an FK
      participates in the parent entity's natural key (e.g., a
      ``CalendarDate`` row carries ``calendarCode`` / ``schoolId`` /
      ``schoolYear`` as part of its own identity by virtue of the
      ``calendarReference`` FK). Emitted under the parent entity so
      source-doc rows that list the FK column directly (AZ XLSX,
      TEDS-style docs) resolve.
    - **State extensions** — same two shapes re-applied against the
      ``extends_entity``; identity columns added by a state extension
      surface on the core entity's slot set.

    Keys are stored under ``record_match_keys`` alias expansion
    (lowercased, plural-folded) so the source-lens record side — which
    carries PascalCase state-doc names like ``CalendarCode`` — matches
    against the spine's canonical camelCase ``calendarCode`` without
    either side needing case-sensitive equality.
    """
    from src.utils.matching import record_match_keys

    slots: set[tuple[str, str]] = set()

    def _add_slot(entity_name: str, element_name: str) -> None:
        for key in record_match_keys(entity_name, element_name):
            slots.add(key)

    def _add_entity(entity_name: str, props: dict, refs: dict, subs: dict) -> None:
        for pname, prop in props.items():
            if prop.is_identity:
                _add_slot(entity_name, pname)
        for ref_name, ref in refs.items():
            for kp, kpi in ref.key_properties.items():
                if kpi.is_identity:
                    _add_slot(entity_name, kp)
        # Sub-collection properties are not themselves identity fields on
        # the parent entity — identity is carried by the parent's own
        # props/refs. Leaving subs out of the slot set.
        _ = subs  # unused; retained in signature for shape symmetry

    for entity_name, entity in spine.catalog.entities.items():
        _add_entity(
            entity_name,
            entity.properties,
            entity.references,
            entity.sub_collections,
        )

    for ext in spine.catalog.extensions.values():
        _add_entity(
            ext.extends_entity,
            ext.properties,
            ext.references,
            ext.sub_collections,
        )

    return frozenset(slots)


# Cycle-safe depth ceiling. Ed-Fi reference graphs are DAG-shaped in
# practice; the cap is a belt-and-braces guard so a malformed spine or
# extension-introduced cycle can't spin the DFS forever.
_FK_CHAIN_DEPTH_CAP: int = 20


def _collect_fk_chain_depth(spine: StateSpine) -> dict[str, int]:
    """Entity → max FK-hop distance to a root reporting entity.

    Root = an entity with no outgoing references that resolve inside
    this catalog. Depth(entity) = 1 + max(depth(target)) over every
    outgoing ref whose target is also in the catalog. DFS with
    memoization; cycles short-circuit at ``_FK_CHAIN_DEPTH_CAP``.

    Keys are lowercased canonical entity names so lookup against a
    record's (possibly PascalCase-vs-camel) ``entity`` field converges
    via ``entity.lower()``.
    """
    # Build outgoing map once: entity → set of target entities that
    # exist in the catalog. Extensions contribute their references to
    # the extended entity so TEA/MN/WI extensions that add FKs deepen
    # the core entity's chain.
    outgoing: dict[str, set[str]] = {
        name: set() for name in spine.catalog.entities
    }
    for entity_name, entity in spine.catalog.entities.items():
        for ref in entity.references.values():
            if ref.entity in spine.catalog.entities:
                outgoing[entity_name].add(ref.entity)
    for ext in spine.catalog.extensions.values():
        extended = ext.extends_entity
        if extended not in outgoing:
            outgoing[extended] = set()
        for ref in ext.references.values():
            if ref.entity in spine.catalog.entities:
                outgoing[extended].add(ref.entity)

    memo: dict[str, int] = {}

    def _depth(name: str, stack: frozenset[str]) -> int:
        if name in memo:
            return memo[name]
        if name in stack:
            # Cycle — don't recurse further through this edge.
            return 0
        if len(stack) >= _FK_CHAIN_DEPTH_CAP:
            return _FK_CHAIN_DEPTH_CAP
        targets = outgoing.get(name, set())
        if not targets:
            memo[name] = 0
            return 0
        best = 0
        new_stack = stack | {name}
        for tgt in targets:
            candidate = 1 + _depth(tgt, new_stack)
            if candidate > best:
                best = candidate
        memo[name] = best
        return best

    return {name.lower(): _depth(name, frozenset()) for name in outgoing}


def _collect_reference_fan_out(spine: StateSpine) -> dict[str, int]:
    """Entity → count of *other* entities whose references target it.

    Measures how many downstream reporting entities a vendor has to
    keep consistent with this one — a row-level edit ripples to every
    fan-out target. Core entities + extensions both contribute
    outgoing edges; the result is attributed to the *target* entity.

    Keys lowercased for record-side lookup.
    """
    counts: dict[str, set[str]] = {}

    def _note(source_entity: str, target_entity: str) -> None:
        if target_entity not in spine.catalog.entities:
            return
        if target_entity == source_entity:
            # Self-references don't count as fan-out; a self-referencing
            # entity hasn't introduced a new dependency.
            return
        counts.setdefault(target_entity, set()).add(source_entity)

    for entity_name, entity in spine.catalog.entities.items():
        for ref in entity.references.values():
            _note(entity_name, ref.entity)
    for ext in spine.catalog.extensions.values():
        for ref in ext.references.values():
            _note(ext.extends_entity, ref.entity)

    return {name.lower(): len(sources) for name, sources in counts.items()}


def _collect_sub_collection_slots(
    spine: StateSpine,
) -> frozenset[tuple[str, str]]:
    """Every (entity, element) pair that sits inside a sub-collection.

    Records resolving to one of these keys score ``sub_collection_depth
    = 1``; records outside the set score 0. The catalog models nested
    arrays as flat ``SubCollectionInfo`` entries with a single level of
    ``properties`` — genuine multi-level nesting (sub-collection of a
    sub-collection's sub-entity) isn't represented here, so we cap at
    depth 1. If a future spine build surfaces deeper nesting, swap this
    to a depth map.

    Keys go through ``record_match_keys`` alias expansion so state-doc
    rows with PascalCase entity names resolve against camelCase spine
    keys without bespoke normalization.
    """
    from src.utils.matching import record_match_keys

    slots: set[tuple[str, str]] = set()

    def _add(entity_name: str, element_name: str) -> None:
        for key in record_match_keys(entity_name, element_name):
            slots.add(key)

    def _visit(
        entity_name: str, subs: dict
    ) -> None:
        for sub_name, sub in subs.items():
            _add(entity_name, sub_name)
            for prop_name in sub.properties:
                _add(entity_name, prop_name)
            if sub.sub_entity:
                pascal = sub.sub_entity
                camel = pascal[0].lower() + pascal[1:] if pascal else ""
                if pascal:
                    _add(entity_name, pascal)
                if camel and camel != pascal:
                    _add(entity_name, camel)

    for entity_name, entity in spine.catalog.entities.items():
        _visit(entity_name, entity.sub_collections)
    for ext in spine.catalog.extensions.values():
        _visit(ext.extends_entity, ext.sub_collections)

    return frozenset(slots)


def _collect_entity_extension_footprint(
    spine: StateSpine,
) -> dict[str, int]:
    """Entity → number of catalog extensions that extend it.

    Counts distinct extension schemas, not total fields added — a
    single extension that adds 30 fields contributes 1, matching the
    plan's framing of "how many teams/authors are grafting onto this
    entity". Keys lowercased.
    """
    counts: dict[str, int] = {}
    for ext in spine.catalog.extensions.values():
        key = ext.extends_entity.lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


def _collect_core_elements_by_entity(
    spine: StateSpine,
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Entity-lower → tuple of ``(element_name_lower, canonical_data_type)`` for CORE-only elements.

    Walks ``spine.catalog.entities`` (not extensions) and emits one entry
    per direct property + reference key-property + sub-collection
    property. Reference / Collection slots are filtered out — the
    fidelity-divergence comparison cares about value-bearing data
    elements, not structural pointers. Canonical data type comes from
    :func:`src.ingest.shared.canonical_type` so Descriptor-suffixed
    names resolve to ``"Descriptor"`` regardless of the underlying
    swagger type, matching how :func:`populate_data_types_from_spine`
    backs the same canonicalization onto ``ElementRecord.data_type``.

    Used by :func:`extension_fidelity_divergence` to find candidate
    core counterparts for an extension on the same entity.
    """
    from src.ingest.shared import canonical_type

    accum: dict[str, list[tuple[str, str]]] = {}

    def _add(entity_name: str, prop_name: str, prop_type: str | None,
             prop_format: str | None) -> None:
        canonical = canonical_type(prop_name, prop_type, prop_format)
        if canonical in ("", "Reference", "Collection"):
            return
        accum.setdefault(entity_name.lower(), []).append(
            (prop_name.lower(), canonical)
        )

    for entity_name, entity in spine.catalog.entities.items():
        for prop_name, prop in entity.properties.items():
            _add(entity_name, prop_name, prop.type, prop.format)
        for ref in entity.references.values():
            for kp_name, kp in ref.key_properties.items():
                _add(entity_name, kp_name, kp.type, kp.format)
        for sub in entity.sub_collections.values():
            for sp_name, sp in sub.properties.items():
                _add(entity_name, sp_name, sp.type, sp.format)

    return {k: tuple(v) for k, v in accum.items()}


def _lookup_canonical(
    record: ElementRecord, context: FactContext
) -> tuple[str, str, str | None] | None:
    """Find the spine's canonical (entity, element_name, extension_name)
    for a record's slot, using record-match alias expansion.

    Returns None when no spine slot claims the record's key — the row is
    source-unknown territory.
    """
    from src.utils.matching import record_match_keys

    for key in sorted(record_match_keys(record.entity, record.element_name)):
        hit = context.canonical_by_alias.get(key)
        if hit is not None:
            return hit
    return None


def element_name_matches_canonical(
    record: ElementRecord, context: FactContext
) -> bool:
    """True iff ``record.element_name`` equals the spine's canonical name
    for this slot, either exactly or after stripping a trailing
    ``Descriptor`` suffix from the canonical side (case-sensitive on
    both branches).

    ``source='unknown'`` rows always score False — they didn't resolve to
    a canonical slot. Rows that resolved via an alias but kept a
    non-canonical verbatim name (e.g., WI ``SchoolYear`` for spine
    ``schoolYear``) also score False — ``naming_deviation_cosmetic``
    catches the trivial ones.

    The descriptor-suffix fold is necessary because Ed-Fi 4.0 names
    FK-to-descriptor columns with the suffix
    (``titleOfAssessmentDescriptor``) while TEDS/TWEDS store the same
    concept as ``titleOfAssessment``. Both compares stay case-sensitive
    so ordinary case drift (``FirstName`` vs ``firstName``) still falls
    through to the tier-2 cosmetic branch in ``canonical_name_alignment``.
    """
    if record.source == "unknown":
        return False
    hit = _lookup_canonical(record, context)
    if hit is None:
        return False
    _, canonical_name, _ = hit
    return (
        record.element_name == canonical_name
        or record.element_name == canonical_name.removesuffix("Descriptor")
    )


def naming_deviation_cosmetic(
    record: ElementRecord, context: FactContext
) -> bool:
    """True iff the element name differs from canonical only in casing
    (or casing plus a trailing ``Descriptor`` suffix on the canonical).

    Fires only when ``element_name_matches_canonical`` is False. A
    deviation is "cosmetic" when the names are equal under
    case-insensitive comparison — the Pascal-vs-camel drift that's the
    dominant pattern in AZ (XLSX), MN (matrix), and TX (TEDS). Deeper
    deviations (renames, typos, plural vs singular) score False and
    are picked up by ``canonical_name_alignment`` tier 1.
    Pluralization is deliberately NOT treated as cosmetic — "address"
    and "addresses" carry different semantics.

    The trailing-``Descriptor`` suffix is folded on the canonical side
    before the case-insensitive compare to catch TEDS/TWEDS rows like
    ``TitleOfAssessment`` vs Ed-Fi ``titleOfAssessmentDescriptor`` —
    same slot, cosmetic drift only. Without the fold those rows fall
    all the way to tier-1 as measurement artifacts.
    """
    if record.source == "unknown":
        return False
    hit = _lookup_canonical(record, context)
    if hit is None:
        return False
    _, canonical_name, _ = hit
    if record.element_name == canonical_name:
        return False
    # Don't double-count rows that tier-3 already owns via suffix fold.
    if record.element_name == canonical_name.removesuffix("Descriptor"):
        return False
    state_lower = record.element_name.lower()
    canon_lower = canonical_name.lower()
    if state_lower == canon_lower:
        return True
    return state_lower == canon_lower.removesuffix("descriptor")


def is_natural_key(record: ElementRecord, context: FactContext) -> bool:
    """True iff ``record`` maps to a spine slot flagged ``is_identity=True``.

    Resolves via ``record_match_keys`` alias expansion on both sides —
    the record's (entity, element_name) is expanded to the same alias
    key set used by ``element_name_matches_canonical``, and each alias
    is tested against ``context.natural_key_slots`` (itself the spine's
    is-identity slots). A hit on any alias resolves the record as a
    natural-key element.

    Used by the NACHOS rule to guard tier-3 concatenation — a
    state-mandated composite format on a natural-key identifier
    (AZ ``CalendarCode`` = ``LEAID-SchoolId-CalendarTypeCodeValue-
    Sequence``) is a key-format specification, not a CONCATENATE
    derivation that re-computes from upstream columns. False otherwise
    (including ``source='unknown'`` rows that didn't resolve to a spine
    slot).

    Rationale: NACHOS methodology tier 3 captures derivation cost —
    SUM / COUNT / CONCAT that computes a value from elsewhere. A natural
    key carrying a delimiter format is a different beast: the LEA
    composes the key once per record and submits it, and vendors pass
    it through. Scoring it tier 3 conflates format specs with
    derivations and over-penalises a state's identifier convention.
    """
    from src.utils.matching import record_match_keys

    if not context.natural_key_slots:
        return False
    for key in record_match_keys(record.entity, record.element_name):
        if key in context.natural_key_slots:
            return True
    return False


def fk_chain_depth(record: ElementRecord, context: FactContext) -> int:
    """Depth of the record's entity in the spine's reference graph.

    Broadcast value — every element of entity X returns the same depth
    that X holds. 0 for a root entity; N for an entity that reaches a
    root only via N FK hops. Returns 0 for ``source='unknown'`` rows
    and for any entity name the spine doesn't know (safe default: treat
    unknown entities as roots).
    """
    return context.fk_chain_depth_by_entity.get(record.entity.lower(), 0)


def reference_fan_out(record: ElementRecord, context: FactContext) -> int:
    """Number of other entities whose references target this record's entity.

    Broadcast value — every element of entity X returns the same
    fan-out count. 0 for leaf entities (nothing points at them) and for
    entities the spine doesn't know.
    """
    return context.reference_fan_out_by_entity.get(record.entity.lower(), 0)


def sub_collection_depth(record: ElementRecord, context: FactContext) -> int:
    """1 when the record's slot sits inside a declared sub-collection; 0 otherwise.

    Catalog models nested arrays as flat ``SubCollectionInfo`` entries,
    so we can only reliably report depth 0 vs 1. A future spine build
    with deeper nesting would swap ``sub_collection_slots`` for a depth
    map and this function with a dict lookup.
    """
    from src.utils.matching import record_match_keys

    if not context.sub_collection_slots:
        return 0
    for key in record_match_keys(record.entity, record.element_name):
        if key in context.sub_collection_slots:
            return 1
    return 0


def entity_extension_footprint(
    record: ElementRecord, context: FactContext
) -> int:
    """Number of catalog extensions whose ``extends_entity`` matches this entity.

    Broadcast value — every element of entity X returns the same
    footprint. Counts extensions, not extension-contributed fields.
    Zero for entities with no extensions (most of the core catalog).
    """
    return context.entity_extension_footprint_by_entity.get(
        record.entity.lower(), 0
    )


def element_is_bare_fk_reference(
    record: ElementRecord, context: FactContext
) -> bool:
    """B5 (issue #63 v16, 2026-04-29): true iff this element is a bare FK reference.

    Three conditions:

    - ``element_name`` matches a known spine entity name
      (case-insensitive)
    - the record's own entity differs from the matched entity (so
      self-references are not flagged)
    - no element-specific narrative — both ``definition_text`` and
      ``element_specific_rules`` are empty (entity-shared
      ``business_rules_text`` doesn't count, since TX TWEDS attaches
      one entity block to every child row)

    Used by ``nachos_score`` (and indirectly by
    ``business_logic_complexity``) to suppress the
    ``has_cross_entity_logic`` contribution to the NACHOS tier when
    the row is a bare FK reference. Bare FK references carry no
    element-level integration logic — the cross-entity reach is a
    structural property of the spine, not of the element's value or
    reporting obligation. The structural-depth signal still surfaces
    on the workbook through the Integration Profile dimensions
    (``fk_chain_depth`` / ``reference_fan_out``); B5 just moves it
    off the NACHOS axis where it doesn't belong.

    Element-level cross-entity logic from narrative (e.g., "if Object
    is 61XX-66XX, Organization will be changed to 999") is unaffected
    — that fires through ``has_conditional_logic`` and still scores
    ``tier_1_conditional`` normally.

    See `docs/adr/0001-issue-63-methodology-calls.md` (B5) and
    `docs/recommendations.md` §6.5.3 for the methodology call this
    fact implements.
    """
    if not record.element_name:
        return False
    has_def = bool((record.definition_text or "").strip())
    has_esr = bool((record.element_specific_rules or "").strip())
    if has_def or has_esr:
        return False
    elem_lower = record.element_name.lower()
    if elem_lower not in context.fk_chain_depth_by_entity:
        return False
    # Guard: don't flag a self-reference (an element on entity X named
    # "X" is unusual but if it exists, it's not a bare FK).
    if elem_lower == record.entity.lower():
        return False
    return True


# Issue #97 (2026-04-30): FK-name suffix + parent-entity submission gate
# discriminator for ``element_only_parent_entity_gate``.
#
# ``_FK_NAME_SUFFIX_RE`` — element_name endings that signal a foreign-key
# reference field by Ed-Fi naming convention: ``…Id`` (e.g.,
# ``schoolId``, ``EntryTypeDescriptorID``), ``…UniqueId`` (e.g.,
# ``studentUniqueId``), or ``…Reference`` (e.g.,
# ``residentLocalEducationAgencyReference``). Case-insensitive so AZ
# PascalCase (``DescriptorID``) and Ed-Fi camelCase (``schoolId``) both
# match. The suffix-only test is intentional — the prefix doesn't need
# to resolve to a known spine entity for this calibration; the question
# is whether the element shape is a bare FK reference, not whether the
# referenced entity exists in *this* state's spine.
_FK_NAME_SUFFIX_RE = re.compile(r"(UniqueId|Reference|Id)$", re.IGNORECASE)
# ``_PARENT_ENTITY_GATE_RE`` — the WI Confluence annotation that
# describes a record's submission gate by school type (Public / Choice).
# Captures both spellings observed in the corpus
# (``[Public: REQ'D, Choice: NOT REQ'D]`` and the typo
# ``[Public: CONDITINALLY REQ'D, Choice: NOT REQ'D]``) plus the
# canonically-spelt ``CONDITIONALLY``. The semicolon-or-comma separator
# is permissive; the bracketed-pair shape is the load-bearing signal.
# Both straight and curly apostrophes match because WI Confluence uses
# both inconsistently.
_PARENT_ENTITY_GATE_RE = re.compile(
    r"\[\s*Public\s*:\s*[^,\]]+[,;]\s*Choice\s*:\s*[^\]]+\]",
    re.IGNORECASE,
)
# ``_PARENT_GATE_RESIDUE_MAX`` — after stripping the gate annotation,
# the remaining definition_text must be short. Bucket-A rows (parent-
# entity gate only) leave at most ~175 chars of generic Ed-Fi spine
# description ("The identifier assigned to a school", "A unique
# alphanumeric code assigned to a student"). Bucket-B rows that ALSO
# carry an element-specific rule alongside the gate annotation
# (``residentLocalEducationAgencyReference``: "Resident District is not
# expected for students who live in the district…",
# ``RccCommunityProviderReferencecommunityProviderId``: "Information
# about Residential Care Centers (RCCs) may be obtained from the
# WISEdata API…") leave 222+ chars after stripping, so a 180-char cap
# cleanly separates them. The cap is the calibration boundary — if a
# future Bucket-A row has more verbose spine description than this, it
# will fall to the LLM verdict instead of the suppression; that's a
# more conservative outcome than over-suppressing a Bucket-B row.
_PARENT_GATE_RESIDUE_MAX: int = 180


def element_only_parent_entity_gate(record: ElementRecord) -> bool:
    """Issue #97 (2026-04-30): True iff this row's only conditional
    signal is a parent-entity submission gate annotation.

    Four conditions:

    - ``element_name`` ends in ``Id``/``UniqueId``/``Reference``
      (case-insensitive) — Ed-Fi convention for a foreign-key reference
      field.
    - ``definition_text`` contains a parent-entity submission gate
      annotation (the WI Confluence ``[Public: <code>, Choice: <code>]``
      pattern that describes whether the *whole record* is required
      under that school type).
    - ``element_specific_rules`` is empty (no element-scoped value or
      presence rule outside the annotation).
    - The residue of ``definition_text`` after stripping the annotation
      is short (≤ ``_PARENT_GATE_RESIDUE_MAX`` chars) — generic Ed-Fi
      spine boilerplate, not element-specific rule prose. Bucket-A
      records leave 49-174 chars; Bucket-B records that carry both the
      gate annotation and an element-specific rule alongside it leave
      222+ chars after stripping, so the cap separates them cleanly.

    Used by ``business_logic_complexity`` (rules.py) and ``nachos_score``
    to suppress the ``has_conditional_logic`` contribution on these
    rows. Methodologically the conditional belongs to the parent
    record's submission gate, not to the FK reference's own value or
    presence — every FK on every conditionally-submitted parent would
    otherwise carry a phantom conditional signal.

    The annotation pattern is WI-Confluence-specific by construction;
    AZ XLSX, MN Mapping Matrix, and TX TWEDS narratives do not emit
    ``[Public: …, Choice: …]`` text, so the fact is naturally inert on
    those states. FK-shaped rows on AZ/MN/TX with element-specific
    value rules (TX ``StudentId`` first-character constraints, AZ
    ``…DescriptorId`` "Submitted for courses that…" patterns) are
    untouched — those have non-empty ``definition_text`` without the
    gate annotation, so the rule does not fire.

    See `docs/adr/0001-issue-63-methodology-calls.md` for the parallel
    B5 precedent (``element_is_bare_fk_reference``); the calibration
    rationale + classification of the 29 FK-shaped True flips lives in
    issue #97 and the v22 plan-version commit.
    """
    name = (record.element_name or "")
    if not _FK_NAME_SUFFIX_RE.search(name):
        return False
    if (record.element_specific_rules or "").strip():
        return False
    deftext = record.definition_text or ""
    if not _PARENT_ENTITY_GATE_RE.search(deftext):
        return False
    residue = _PARENT_ENTITY_GATE_RE.sub("", deftext).strip(" .,;\n\t")
    return len(residue) <= _PARENT_GATE_RESIDUE_MAX


def extension_mirrors_core_pattern(
    record: ElementRecord, context: FactContext
) -> bool:
    """True iff a ``source='extension'`` row's element name also appears
    as a core spine emit somewhere in the catalog (case-insensitive).

    Signal for plan §7.1's ``extension_justification`` tier 0 — a state
    extension re-declaring a core field's name is a candidate for "not
    necessary". Non-extension rows always score False by construction.

    The case-fold is necessary because AZ/TX source docs use PascalCase
    (``StudentUniqueId``, ``Student``, ``School``) while Ed-Fi spine
    emits are camelCase (``studentUniqueId``, ``student``, ``school``).
    The case-sensitive version missed these as cosmetic duplicates.
    """
    if record.source != "extension":
        return False
    return record.element_name.lower() in context.core_element_names_lower


# Issue #124 PR 2 / #111 (2026-05-02) — ``extension_fidelity_divergence``
# heuristics. Stem prefix length kept at 5 to filter incidental
# 3-4-letter overlaps (`name`, `code`, `type`) while still firing on
# substantive concept matches (`student`, `school`, `eligib`).
_FIDELITY_NAME_SUFFIXES: tuple[str, ...] = (
    "DescriptorId",
    "Descriptor",
    "UniqueId",
    "Reference",
    "Id",
)
_FIDELITY_STEM_PREFIX_MIN: int = 5
# Coarse data-type buckets. Two elements with shapes in different
# buckets are treated as a *material* shape divergence; within-bucket
# differences (Integer vs Decimal — both scalar numerics) are not.
# ``Boolean`` and ``Descriptor`` separate explicitly because the
# canonical #111 example is "extension descriptor over core boolean".
_FIDELITY_TYPE_BUCKETS: dict[str, str] = {
    "Boolean": "boolean",
    "String": "scalar_text",
    "Integer": "scalar_numeric",
    "Number": "scalar_numeric",
    "Decimal": "scalar_numeric",
    "Date": "scalar_date",
    "DateTime": "scalar_date",
    "Time": "scalar_date",
    "Descriptor": "descriptor",
}


def _fidelity_strip_suffix_lower(name_lower: str) -> str:
    """Return ``name_lower`` with the longest matching Ed-Fi suffix removed.

    Matches against case-folded ``_FIDELITY_NAME_SUFFIXES`` so an input
    like ``eligibilitysourcedescriptor`` collapses to
    ``eligibilitysource`` and ``studentuniqueid`` collapses to
    ``student``.
    """
    for suffix in _FIDELITY_NAME_SUFFIXES:
        s = suffix.lower()
        if name_lower.endswith(s) and len(name_lower) > len(s):
            return name_lower[: -len(s)]
    return name_lower


def _fidelity_stem_overlap(a: str, b: str) -> bool:
    """Symmetric ``_FIDELITY_STEM_PREFIX_MIN``-char prefix overlap on stems.

    Either ``a`` starts with the first 5 chars of ``b`` or vice versa.
    Both stems must already be lowercased.
    """
    if (
        len(a) < _FIDELITY_STEM_PREFIX_MIN
        or len(b) < _FIDELITY_STEM_PREFIX_MIN
    ):
        return False
    return (
        a.startswith(b[:_FIDELITY_STEM_PREFIX_MIN])
        or b.startswith(a[:_FIDELITY_STEM_PREFIX_MIN])
    )


def extension_fidelity_divergence(
    record: ElementRecord, context: FactContext
) -> str:
    """Issue #124 PR 2 / #111 (2026-05-02): typed signal for an extension
    that replaces a core field with a different shape.

    Returns one of:

    - ``"replaces_core_field_shape"`` — ``source='extension'`` row whose
      name-stem (after stripping ``Descriptor``/``Id``/``UniqueId``/
      ``Reference`` suffixes) shares a 5+ char case-insensitive prefix
      with a CORE element on the same entity AND their data-type
      buckets differ (e.g., Boolean vs Descriptor, String vs
      Descriptor). The canonical case is an extension descriptor that
      replaces a core boolean / scalar with a richer enumerated value.
    - ``"none"`` — non-extension rows, extensions whose entity has no
      core elements catalogued, extensions whose name-stem has no
      same-entity core counterpart sharing a prefix, or extensions
      whose counterpart sits in the same data-type bucket (no shape
      divergence).

    Source-lens consumers (the v12 SF fold annotation in
    :func:`aggregate._compute_nachos_adjustments`) use the value to
    render a typed-reason parenthetical in the ``+1.0
    fidelity_divergent_unclear`` label. Spine-lens callers don't
    consume it — the SF fold dimension is source-lens only.

    Heuristic limitations are intentional and documented:

    - Same-name-different-type cases hit reliably (extension
      ``Description: Descriptor`` over core ``description: String``).
    - Conceptually-related but lexically-different cases (the AZ #111
      ``EligibilitySourceDescriptor`` paired with a hypothetical
      ``directCertificationIndicator`` core boolean) won't fire — the
      names don't share a stem prefix. Such rows still take the SF
      fold magnitude; only the typed-reason annotation is missing,
      which keeps the label honest about how it was derived. A future
      LLM-extracted variant of this fact (path B from issue #111)
      could close the lexical gap if the cohort warrants the cost.
    """
    if record.source != "extension":
        return "none"
    if not record.data_type or not record.element_name:
        return "none"
    ext_bucket = _FIDELITY_TYPE_BUCKETS.get(record.data_type)
    if ext_bucket is None:
        return "none"

    ext_stem = _fidelity_strip_suffix_lower(record.element_name.lower())
    if len(ext_stem) < _FIDELITY_STEM_PREFIX_MIN:
        return "none"

    core_elements = context.core_elements_by_entity.get(
        record.entity.lower(), ()
    )
    for core_name_lower, core_data_type in core_elements:
        if core_name_lower == record.element_name.lower():
            # Same-name extension shadowing a core element by name —
            # ``extension_mirrors_core_pattern`` already covers this
            # signal independent of shape; skip so we don't double-
            # surface as fidelity divergence.
            continue
        core_stem = _fidelity_strip_suffix_lower(core_name_lower)
        if not _fidelity_stem_overlap(ext_stem, core_stem):
            continue
        core_bucket = _FIDELITY_TYPE_BUCKETS.get(core_data_type)
        if core_bucket is None:
            continue
        if ext_bucket != core_bucket:
            return "replaces_core_field_shape"
    return "none"


def _is_descriptor_row(record: ElementRecord) -> bool:
    """Cheap structural check for descriptor-kind rows.

    True when any one of the spine-derived markers fires: canonical
    ``data_type == "Descriptor"``, an ``element_name`` ending with the
    Ed-Fi ``…Descriptor`` suffix, or a populated descriptor-table field
    on the record. A row that matches none of these is not a descriptor
    slot and ``descriptor_values_enumerated`` never fires on it.
    """
    if record.data_type == "Descriptor":
        return True
    if record.element_name.endswith("Descriptor"):
        return True
    if record.descriptor_table_values:
        return True
    if record.descriptor_table_code:
        return True
    return False


def descriptor_values_enumerated_spans(record: ElementRecord) -> list[str]:
    """Return the enumeration-match spans on a descriptor row, in doc order.

    Same gate logic as :func:`descriptor_values_enumerated` — the
    structural fast-path (``descriptor_table_values``) plus the four
    narrative patterns. An empty list means the gate does not fire.
    Each span is a readable verbatim excerpt of the match text so
    downstream ``fact_provenance`` consumers see *which* enumeration
    token(s) triggered the deterministic fact — not just a boolean.

    The span list is:

    - For the structural fast-path, one ``"{code}: {label}"`` per
      ``descriptor_table_values`` entry (falling back to whichever side
      of the pair is populated).
    - For the literal-prefix paths, the slice from the literal through
      end-of-line (or end-of-blob).
    - For the pipe / quoted paths, each whole-match substring from
      :func:`re.findall`.
    - For the prose-per-code path, one span per code-token expanded to
      the next token's start (so the full descriptive phrase is
      readable) — only emitted when the ``≥ 3`` threshold fires, same
      as the boolean gate.
    """
    if not _is_descriptor_row(record):
        return []
    spans: list[str] = []
    if record.descriptor_table_values:
        for item in record.descriptor_table_values:
            code = (item.get("code") or "").strip()
            label = (item.get("label") or "").strip()
            if code and label:
                spans.append(f"{code}: {label}")
            elif code:
                spans.append(code)
            elif label:
                spans.append(label)
        return spans
    blob = "\n".join(
        filter(
            None,
            [
                record.definition_text or "",
                record.business_rules_text or "",
                record.element_specific_rules or "",
            ],
        )
    )
    if not blob:
        return []
    for literal in _DESCRIPTOR_ENUM_LITERALS:
        idx = blob.find(literal)
        while idx != -1:
            eol = blob.find("\n", idx)
            end = eol if eol != -1 else len(blob)
            spans.append(blob[idx:end].strip())
            idx = blob.find(literal, idx + len(literal))
    spans.extend(m.strip() for m in _PIPE_ENUM_PATTERN.findall(blob))
    spans.extend(m.strip() for m in _QUOTED_ENUM_PATTERN.findall(blob))
    prose_matches = list(_PROSE_PER_CODE_PATTERN.finditer(blob))
    if len(prose_matches) >= _PROSE_PER_CODE_MIN_HITS:
        for i, m in enumerate(prose_matches):
            start = m.start()
            end = (
                prose_matches[i + 1].start()
                if i + 1 < len(prose_matches)
                else len(blob)
            )
            spans.append(blob[start:end].strip().rstrip(".,;"))
    return spans


def descriptor_values_enumerated(record: ElementRecord) -> bool:
    """True iff the descriptor row carries at least one enumeration match.

    Derived from :func:`descriptor_values_enumerated_spans` — the two
    share the same gate logic, and the boolean is just
    ``bool(spans)``. The span list is the ground truth: any consumer
    that wants the match text calls the span function; any consumer
    that only needs the gate reads this boolean. Keeping the boolean
    around preserves every existing caller's contract.
    """
    return bool(descriptor_values_enumerated_spans(record))


_COMPUTATIONS: dict[str, Callable[[ElementRecord], bool | int]] = {
    "definition_present": definition_present,
    "element_narrative_present": element_narrative_present,
    "business_rules_present": business_rules_present,
    "data_type_canonical": data_type_canonical,
    "descriptor_values_enumerated": descriptor_values_enumerated,
    "definition_text_substantive": definition_text_substantive,
    "descriptor_enum_breadth": descriptor_enum_breadth,
    "element_only_parent_entity_gate": element_only_parent_entity_gate,
}


# Facts that can additionally produce verbatim evidence spans alongside
# the boolean. Entries are record-only functions returning ``list[str]``.
# Non-entries leave ``spans=[]`` in the artifact row (historical shape).
# Only deterministic facts with clear positional match text qualify —
# semantic facts (``definition_present``) have nothing meaningful to
# emit as a span.
_SPAN_COMPUTATIONS: dict[str, Callable[[ElementRecord], list[str]]] = {
    "descriptor_values_enumerated": descriptor_values_enumerated_spans,
}


_CONTEXT_COMPUTATIONS: dict[
    str, Callable[[ElementRecord, FactContext], bool | int | str]
] = {
    "element_name_matches_canonical": element_name_matches_canonical,
    "naming_deviation_cosmetic": naming_deviation_cosmetic,
    "extension_mirrors_core_pattern": extension_mirrors_core_pattern,
    "extension_fidelity_divergence": extension_fidelity_divergence,
    "is_natural_key": is_natural_key,
    "fk_chain_depth": fk_chain_depth,
    "reference_fan_out": reference_fan_out,
    "sub_collection_depth": sub_collection_depth,
    "entity_extension_footprint": entity_extension_footprint,
    "element_is_bare_fk_reference": element_is_bare_fk_reference,
}


def compute_fact(
    fact: str,
    record: ElementRecord,
    *,
    context: FactContext | None = None,
) -> bool | int | str:
    """Dispatch to the named deterministic computation.

    Facts in ``_COMPUTATIONS`` are record-only and ignore ``context``.
    Facts in ``_CONTEXT_COMPUTATIONS`` require a ``FactContext`` —
    omission raises ``ValueError`` rather than silently scoring False.
    Return type is ``bool`` for boolean facts, ``int`` for count facts
    (``descriptor_enum_breadth`` + the v2 structural axis), and
    ``str`` for enum facts (``extension_fidelity_divergence``).
    """
    if fact in _COMPUTATIONS:
        return _COMPUTATIONS[fact](record)
    if fact in _CONTEXT_COMPUTATIONS:
        if context is None:
            raise ValueError(
                f"{fact!r} requires a FactContext (build via "
                f"build_fact_context(spine))"
            )
        return _CONTEXT_COMPUTATIONS[fact](record, context)
    raise ValueError(
        f"unknown deterministic fact {fact!r}; supported: {DETERMINISTIC_FACTS}"
    )


# ---------------------------------------------------------------------------
# Artifact emission
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _record_key(rec: ElementRecord) -> str:
    return f"{rec.state}|{rec.entity}|{rec.element_name}"


def _row_for(
    fact: str,
    rec: ElementRecord,
    value: bool | int | str,
    *,
    spans: list[str] | None = None,
) -> dict[str, Any]:
    """Build one artifact row matching the LLM-fact row shape.

    ``spans`` stays empty by default — deterministic facts without
    positional match text carry no evidence. Facts registered in
    :data:`_SPAN_COMPUTATIONS` (e.g. ``descriptor_values_enumerated``)
    pass a ``list[str]`` of verbatim match excerpts, which surfaces
    through ``FactResult.spans`` into the per-record sidecar's
    ``fact_provenance`` entry. ``confidence`` is always ``"high"`` —
    these computations are exact. ``downgrade_reason`` stays None.

    ``value`` is the post-computation value — ``bool`` for boolean
    facts (the Phase A/B baseline), ``int`` for count facts
    (``descriptor_enum_breadth`` + the v2 structural axis), or
    ``str`` for enum facts (``extension_fidelity_divergence``). The
    JSON writer handles all three types without branching.

    The fact-name key under which ``value`` lands mirrors the LLM
    schema: ``has_conditional_logic`` for that fact,
    ``definition_present`` for this one, etc. Downstream consumers
    look it up by fact name, so the key name matters.
    """
    return {
        "record_key": _record_key(rec),
        "entity": rec.entity,
        "element_name": rec.element_name,
        # ``llm_value`` is a shared-schema key (see module docstring): no
        # model runs on this path, so it carries the computed value and
        # ``validated_value`` mirrors it. The deterministic tell is the
        # ``model``/``mode`` field, not this key. Scoring reads
        # ``validated_value``; ``llm_value`` is audit-only here.
        "llm_value": value,
        "validated_value": value,
        "spans": list(spans) if spans else [],
        "confidence": "high",
        "downgrade_reason": None,
        "model": "deterministic",
        "prompt_version": DETERMINISTIC_VERSION,
    }


def run(
    *,
    fact: str,
    state: str,
    lens: str = "spine",
    limit: int | None = None,
    out_dir: Path | None = None,
    elements_path: Path | None = None,
    prompt_version: str = DETERMINISTIC_VERSION,
) -> dict[str, Any]:
    """Compute a deterministic fact across records; emit a JSONL artifact.

    Matches the ``extract.run()`` signature's core shape (fact, state,
    lens, limit, out_dir) so the runner's dispatch doesn't care which
    path produced the artifact.

    Returns the artifact header dict.
    """
    if fact not in DETERMINISTIC_FACTS:
        raise ValueError(
            f"{fact!r} is not a deterministic fact; supported: {DETERMINISTIC_FACTS}"
        )

    records = load_phase_a_records(
        state.upper(), lens, elements_path=elements_path, limit=limit
    )

    context: FactContext | None = None
    if fact in _CONTEXT_COMPUTATIONS:
        spine_path = state_spine_path(state.upper())
        if not spine_path.exists():
            raise FileNotFoundError(
                f"{fact!r} needs the spine for {state.upper()}, but "
                f"{spine_path} is missing — run `mc spine fetch --state "
                f"{state.upper()}` + `mc spine build --state {state.upper()}` first."
            )
        spine = StateSpine.model_validate_json(
            spine_path.read_text(encoding="utf-8")
        )
        context = build_fact_context(spine)

    rows: list[dict[str, Any]] = []
    # For bool facts, ``true_count`` is the usual "fires" count. For int
    # facts, it collapses to "nonzero count" — still a useful
    # at-a-glance-is-this-signal-dead telemetry for the CLI and tests.
    # For enum facts (``extension_fidelity_divergence``), ``"none"`` is
    # the no-evidence default and is treated as falsy alongside False/0.
    # The raw int / enum rows are still on disk for the rule stage.
    true_count = 0
    span_fn = _SPAN_COMPUTATIONS.get(fact)
    for rec in records:
        value = compute_fact(fact, rec, context=context)
        if isinstance(value, str):
            fires = bool(value) and value != "none"
        else:
            fires = bool(value)
        if fires:
            true_count += 1
        spans = span_fn(rec) if span_fn is not None else None
        rows.append(_row_for(fact, rec, value, spans=spans))

    artifact_path = scoring_phase_a_artifact_path(state.upper(), fact, lens=lens)
    if out_dir is not None:
        artifact_path = out_dir / artifact_path.name

    header: dict[str, Any] = {
        "__type": "header",
        "state": state.upper(),
        "lens": lens,
        "fact": fact,
        "scored_at": _now_iso(),
        "model": "deterministic",
        "prompt_version": prompt_version,
        "record_count": len(records),
        "scored_count": len(records),
        "skipped_count": 0,
        "entities_processed": len({r.entity for r in records}),
        "total_tokens_in": 0,
        "total_tokens_out": 0,
        "total_usd": 0.0,
        "cache_hit_count": 0,
        "downgrade_count": 0,
        "mode": "deterministic",
        "status": "complete",
        "cost_cap_hit": False,
        "schema_error": None,
        "true_count": true_count,
        "false_count": len(records) - true_count,
    }

    _write_artifact(artifact_path, header, rows)
    _LOGGER.info(
        "deterministic fact %s/%s: %d/%d true (%.1f%%)",
        state.upper(), fact, true_count, len(records),
        (true_count / len(records) * 100) if records else 0.0,
    )
    return header
