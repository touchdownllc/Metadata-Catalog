"""Recover AZ-style concatenated sub-collection entity names to their parents.

AZ (and likely WI/MN) flatten Ed-Fi sub-collections into standalone entity
tables: `CalendarDate.calendarEvents[]` is documented as a top-level entity
called `CalendarDateCalendarEvent`. The Swagger spine, in contrast, nests
sub-collection properties under the parent. This module bridges the two
views so a flattened record can be matched against its parent's spine keys.

Two strategies:

1. **Sub-collection scan (primary):** Iterate every entity's sub_collections
   and form `{ParentEntity}{SingularSubCollection}` (e.g., `calendarEvents` →
   `CalendarEvent`). This is what the Ed-Fi naming convention implies.

2. **Longest-prefix fallback:** Some AZ-documented sub-entities appear only
   in state extensions (e.g., `StudentSchoolAssociationLocalEducationAgency`)
   and aren't declared as sub_collections of any parent in the spine. For
   those, find the longest entity name in the spine that is a strict prefix
   of the concatenated name; that's the parent.

Abstract base classes (`EducationOrganization`, `StudentProgramAssociation`,
etc.) are NOT concrete API endpoints — their properties live on concrete
subclasses. Records against them cannot be matched to any spine key. They
are tagged separately in the gap log rather than rewritten.
"""

from __future__ import annotations

from src.models.spine import StateSpine


# Ed-Fi abstract base entities — no concrete API endpoint, properties live
# on concrete subclasses. Unmatched records under these names are out of
# scope for unflattening (there is no single parent to rewrite to).
ABSTRACT_BASES: frozenset[str] = frozenset({
    "EducationOrganization",
    "EducationOrganizationCategory",
    "GeneralStudentProgramAssociation",
    "StudentProgramAssociation",
})


def _depluralize(name: str) -> str:
    """Return the singular form of a sub-collection name.

    Sub-collections in Ed-Fi Swagger are plural camelCase (e.g.,
    `calendarEvents`, `disciplines`, `races`, `curriculumUseds`). We want
    the PascalCase singular for concatenation with the parent.
    """
    if not name:
        return name
    if name.endswith("ies") and len(name) > 3:
        stem = name[:-3] + "y"
    elif name.endswith("ses") and len(name) > 3:
        stem = name[:-2]
    elif name.endswith("s") and not name.endswith("ss") and len(name) > 1:
        stem = name[:-1]
    else:
        stem = name
    return stem[0].upper() + stem[1:]


def build_unflatten_map(spine: StateSpine) -> dict[str, tuple[str, str]]:
    """Map concatenated sub-entity name -> (parent_entity, sub_collection_name).

    Keys are case-insensitive (stored lowercased). Both the Swagger-declared
    `sub_entity` schema name and the `{Parent}{SingularSub}` convention are
    registered so either naming style in source docs resolves.
    """
    result: dict[str, tuple[str, str]] = {}
    for parent_name, entity in spine.catalog.entities.items():
        for sub_name, sub in entity.sub_collections.items():
            value = (parent_name, sub_name)
            if sub.sub_entity:
                result[sub.sub_entity.lower()] = value
            concatenated = f"{parent_name}{_depluralize(sub_name)}"
            result.setdefault(concatenated.lower(), value)
    return result


def resolve_parent(
    entity: str,
    unflatten_map: dict[str, tuple[str, str]],
    spine_entity_names: set[str],
) -> tuple[str, str] | None:
    """Return (parent_entity, provenance_tag) for a concatenated entity name.

    Tries the sub_collection-derived map first, then falls back to the
    longest entity-prefix split against the spine's entity names — this
    catches extension-only sub-entities that aren't declared as sub_collections
    anywhere in core Swagger.

    Returns None if no parent can be inferred (the caller should tag these
    as abstract-base or genuine unknowns in the gap log).
    """
    key = entity.lower()
    if key in unflatten_map:
        return unflatten_map[key]

    candidates = sorted(
        (n for n in spine_entity_names if n != entity and entity.lower().startswith(n.lower())),
        key=len,
        reverse=True,
    )
    for parent in candidates:
        remainder = entity[len(parent):]
        if remainder and remainder[0].isupper():
            return (parent, remainder[0].lower() + remainder[1:])

    # Suffix fallback: the source's bare sub-entity name matches a spine
    # entity ending with that suffix. Example: MN source says entity
    # `LanguageAcademicHonor`, spine has
    # `StudentEducationOrganizationAssociationLanguageAcademicHonor`.
    # Only accept when exactly one spine entity matches to avoid guessing.
    #
    # Return the FULL concatenated spine entity, not its head: extension
    # property propagation in `StateSpine.element_keys()` attributes the
    # extension's properties to the closest catalog parent (which is often
    # itself a sub-entity like `SEOALanguage`, NOT the head `SEOA`). Matching
    # against the head therefore misses; matching against the full spine
    # entity (where the extension declared its props) succeeds.
    cap_suffix = entity if entity and entity[0].isupper() else (entity[0].upper() + entity[1:]) if entity else entity
    suffix_matches = [
        n for n in spine_entity_names
        if n != entity
        and n.endswith(cap_suffix)
        and len(n) > len(cap_suffix)
    ]
    if len(suffix_matches) == 1:
        full = suffix_matches[0]
        head = full[: -len(cap_suffix)]
        # `head` must be a real parent entity — a guard against rewriting
        # through unrelated coincidental suffixes (e.g. `SubjectArea`
        # accidentally matching `MathSubjectArea` if `Math` weren't an entity).
        if head in spine_entity_names:
            sub_coll_name = cap_suffix[0].lower() + cap_suffix[1:]
            return (full, sub_coll_name)

    return None
