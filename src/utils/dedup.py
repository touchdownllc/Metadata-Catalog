"""Post-unflatten deduplication for (entity, element_name) collisions.

Before Tier 2, AZ ran dedup inside `build_element_records` — BEFORE the
unflatten loop rewrote records whose entity was a concatenated sub-entity
name (e.g., `StudentProgramAttendanceEventActivityTimeLog` → parent
`StudentProgramAttendanceEvent`). Post-unflatten duplicates slipped
through: 53 pairs in AZ output, 28 in MN. Analyst feedback (reviewer 1
§1) asked for single-row-per-(entity, element) with provenance preserved.

This module centralizes the dedup pass so both `arizona.run()` and
`minnesota.run()` can invoke it AFTER unflatten recovery but BEFORE writing
the JSON output. Winner preference: `source="core"` > `source="extension"`
> `source="unknown"`. Non-winner definition text is folded in with
`[also: ...]` markers; contributing extension identities are merged into
the winner's definition text so no provenance is lost.
"""

from __future__ import annotations

import logging

from src.models.element import ElementRecord

logger = logging.getLogger(__name__)


_SOURCE_PRIORITY = {"core": 0, "extension": 1, "unknown": 2}


def _pick_winner(group: list[ElementRecord]) -> ElementRecord:
    """Lowest `_SOURCE_PRIORITY` wins; ties broken by first-seen order."""
    return min(group, key=lambda r: _SOURCE_PRIORITY.get(r.source, 99))


def _merge_definitions(
    winner: ElementRecord, others: list[ElementRecord]
) -> str:
    """Fold distinct non-winner definitions in as `[also: ...]` markers.

    Skips tokens that are substrings (either direction) of already-seen
    definitions so near-duplicate wording doesn't pile up. Attributes each
    appended fragment to its originating `raw_entity` for audit.
    """
    base_def = winner.definition_text or ""
    seen = {base_def.strip().lower()}
    additions: list[str] = []
    for other in others:
        other_def = (other.definition_text or "").strip()
        if not other_def:
            continue
        other_lower = other_def.lower()
        if other_lower in seen:
            continue
        if any(other_lower in s or s in other_lower for s in seen):
            continue
        seen.add(other_lower)
        origin = other.raw_entity or "unknown"
        additions.append(f"[also: {other_def} (from {origin})]")
    if additions:
        return base_def + " " + " ".join(additions)
    return base_def


def _merge_extension_names(
    winner: ElementRecord, others: list[ElementRecord]
) -> str | None:
    """Combine contributing extension names when a group spans extensions.

    When the winner is core but at least one loser was an extension, the
    definition text is the right place to note the extension provenance;
    the winner's `extension_name` stays `None` so the `Is an extension`
    boolean still reads correctly at the per-record level.

    When the winner itself is an extension and the losers are other
    extensions with different names, concatenate with `; ` — analysts
    benefit from seeing all contributors.
    """
    if winner.source != "extension":
        return winner.extension_name
    ext_names: list[str] = []
    if winner.extension_name:
        ext_names.append(winner.extension_name)
    for other in others:
        if other.source == "extension" and other.extension_name:
            if other.extension_name not in ext_names:
                ext_names.append(other.extension_name)
    return "; ".join(ext_names) if ext_names else None


def dedup_records(records: list[ElementRecord]) -> list[ElementRecord]:
    """Collapse (entity, element_name) duplicates with provenance preserved.

    The group's winner is the highest-priority record by `source` (core >
    extension > unknown); losing records contribute their distinct
    definition text (appended via `[also: ...]` markers) and — when
    applicable — their extension schema name (joined with `; `).

    Returns a new list with a single record per `(entity, element_name)`
    key. Input list is NOT mutated.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for i, rec in enumerate(records):
        key = (rec.entity, rec.element_name)
        groups.setdefault(key, []).append(i)

    keep_indices: set[int] = set()
    result_at: dict[int, ElementRecord] = {}

    for key, indices in groups.items():
        if len(indices) == 1:
            keep_indices.add(indices[0])
            result_at[indices[0]] = records[indices[0]]
            continue

        group = [records[i] for i in indices]
        winner = _pick_winner(group)
        others = [r for r in group if r is not winner]

        merged_def = _merge_definitions(winner, others)
        merged_ext = _merge_extension_names(winner, others)

        merged = winner.model_copy(update={
            "definition_text": merged_def,
            "extension_name": merged_ext,
        })

        winner_idx = indices[group.index(winner)]
        keep_indices.add(winner_idx)
        result_at[winner_idx] = merged

        logger.info(
            "Dedup %s.%s: collapsed %d records (winner source=%s)",
            key[0], key[1], len(indices), winner.source,
        )

    return [result_at[i] for i in sorted(keep_indices)]
