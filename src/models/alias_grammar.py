"""The alias-expansion grammar — every rule ONCE (issue #213 item 2).

"What names can a source doc use for this spine slot" used to be
independently re-implemented in five places, synchronized by comments:
``StateSpine.element_keys`` / ``extension_element_keys``
(models/spine.py), ``build_spine_type_index`` /
``canonical_spine_emit_keys`` (ingest/shared.py), and the reference
concat-expansions in ``ingest/gap_surfacer.py``. Issues #147/#162/#184
each had to touch that grammar N times or the coverage %, type index,
description index, and gap artifact silently drifted apart — the
PR #182 failure class at the code layer.

This module is the one home for the RULES. Each helper is a pure
function over names/catalog-node data; the five consumers keep their
own walks, value mappings, and first-writer-wins policies (those are
genuine per-consumer policy — e.g. the gap surfacer deliberately checks
a narrower alias set, and the type index registers extension sub-props
before the collection wrapper), but every alias FORM they emit now
comes from here. A grammar change lands once; a consumer that should
pick it up does so by calling the helper — and a consumer that
deliberately does NOT is visible as an explicit absent call, not a
silent fork.

Byte-identity discipline: this refactor shipped with before/after
snapshots of all six derived outputs (element_keys /
extension_element_keys / type index / description index / canonical
emits / alias sink) on the five real state spines — every hash held.
The known present-day divergences BETWEEN consumers (e.g. the type
index lacks the camel-collapse and EdOrg-subtype tiers its docstring
claimed to mirror; the gap surfacer checks a ``refName+CapKey`` form no
other consumer emits) are preserved exactly and documented at the call
sites — closing them is a measured follow-up decision, not a silent
side effect of a refactor.
"""

from __future__ import annotations

import re
from typing import Iterator

_CAMEL_WORD_RE = re.compile(r"(?:^[a-z]+)|(?:[A-Z][a-z]*)")


# Ed-Fi EducationOrganization concrete subtypes. When a parent entity carries
# the generalized `educationOrganizationReference`, TEDS-style source docs
# often name the element by the concrete subtype (`School`,
# `LocalEducationAgency`, etc.) because that's what TEA reports publish. Ed-Fi
# 4.0+ collapsed these into a single FK at the spine level, so matching
# requires a semantic-alias pass. All subtypes are concrete descendants of
# `EducationOrganization` in the Ed-Fi UDM.
EDORG_SUBTYPE_NAMES: tuple[str, ...] = (
    "School",
    "LocalEducationAgency",
    "StateEducationAgency",
    "EducationServiceCenter",
    "PostSecondaryInstitution",
    "CommunityOrganization",
    "CommunityProvider",
    "EducationOrganizationNetwork",
)


def capitalize_first(name: str) -> str:
    """`schoolId` → `SchoolId` (leading-char upper, remainder verbatim)."""
    return name[0].upper() + name[1:] if name else name


def camel_of(pascal: str) -> str:
    """`GenderIdentity` → `genderIdentity` (leading-char lower)."""
    return pascal[0].lower() + pascal[1:] if pascal else pascal


def collapse_camel_overlap(prefix: str, cap_key: str) -> str | None:
    """Return camelCase prefix + cap_key with duplicated word overlap removed.

    `assignmentSchool` + `SchoolId` -> `assignmentSchoolId`
    `externalEducationOrganization` + `EducationOrganizationId`
        -> `externalEducationOrganizationId`
    `program` + `EducationOrganizationId` -> None (no overlap).
    """
    prefix_parts = _CAMEL_WORD_RE.findall(prefix)
    key_parts = _CAMEL_WORD_RE.findall(cap_key)
    if not prefix_parts or not key_parts:
        return None
    max_k = 0
    limit = min(len(prefix_parts), len(key_parts))
    for k in range(1, limit + 1):
        if [p.lower() for p in prefix_parts[-k:]] == [p.lower() for p in key_parts[:k]]:
            max_k = k
    if max_k == 0:
        return None
    return "".join(prefix_parts) + "".join(key_parts[max_k:])


def descriptor_variants(prop_name: str) -> tuple[str, ...]:
    """Alias forms for a `*Descriptor` property; empty otherwise.

    AZ/TX docs suffix descriptor keys with `Id`; MDE docs drop the
    `Descriptor` suffix entirely (e.g. `ProgramType` for
    `programTypeDescriptor`). Emits `<name>Id` then the bare stem —
    consumer registration order follows this tuple's order everywhere.
    """
    if not prop_name.endswith("Descriptor"):
        return ()
    variants = [prop_name + "Id"]
    bare = prop_name[: -len("Descriptor")]
    if bare:
        variants.append(bare)
    return tuple(variants)


def reference_prefix(ref_name: str) -> str:
    """`schoolReference` → `school`; a non-`*Reference` name is its own
    prefix (both the MDE bare-target-entity alias and the FK-prefixed
    composite forms build on this)."""
    return (
        ref_name[: -len("Reference")]
        if ref_name.endswith("Reference")
        else ref_name
    )


def edorg_subtype_aliases() -> Iterator[str]:
    """The four alias forms per EducationOrganization concrete subtype.

    Emitted on any entity whose reference targets `EducationOrganization`
    (safe cross-state: no Ed-Fi entity carries both the generalized ref
    and a subtype-specific FK on the same parent). Order: Pascal, camel,
    `<camel>Reference`, `<camel>Id` per subtype, subtypes in declared
    order — every consumer registers in exactly this sequence.
    """
    for subtype in EDORG_SUBTYPE_NAMES:
        sub_camel = camel_of(subtype)
        yield subtype
        yield sub_camel
        yield sub_camel + "Reference"
        yield sub_camel + "Id"


def reference_qualifier(prefix: str, target_entity: str | None) -> str | None:
    """Qualifier = ref prefix with the target-entity name stripped from
    the tail, e.g. `employmentStaffEducationOrganizationEmploymentAssociation`
    (targeting `StaffEducationOrganizationEmploymentAssociation`) →
    `employment`. None when the prefix doesn't embed the target name."""
    if (
        target_entity
        and prefix.lower().endswith(target_entity.lower())
        and len(prefix) > len(target_entity)
    ):
        return prefix[: -len(target_entity)]
    return None


def uniqueid_id_alias(kp_name: str) -> str | None:
    """Ed-Fi `*UniqueId` surrogate keys are often referenced in state docs
    as plain `*Id` (e.g., `studentUniqueId` → `studentId`)."""
    if kp_name.endswith("UniqueId"):
        return kp_name[: -len("UniqueId")] + "Id"
    return None


def fk_prefixed_alias(prefix: str, kp_name: str) -> str:
    """`programReference` + `educationOrganizationId` →
    `programEducationOrganizationId` — AZ-style docs disambiguate multiple
    FKs to the same base field by prefixing the reference name."""
    return prefix + capitalize_first(kp_name)


def id_stripped_alias(alias: str) -> str | None:
    """MDE docs drop the `Id` suffix from composite FKs
    (`programEducationOrganizationId` → `programEducationOrganization`).
    Only strip when the remainder is long enough that `Id` is meaningful
    as a suffix (avoid producing 2-3 char names)."""
    if alias.endswith("Id") and len(alias) > 4:
        return alias[:-2]
    return None


def sub_entity_name_forms(sub_entity: str) -> tuple[str, ...]:
    """TEDS-style source docs sometimes name a sub-collection by its
    concrete sub-entity type (`Parent.Address`) rather than the collection
    property name (`addresses`) — Pascal form first, camel when distinct."""
    if not sub_entity:
        return ()
    camel = camel_of(sub_entity)
    return (sub_entity, camel) if camel != sub_entity else (sub_entity,)


def parent_stripped_tail(pascal: str, parent_entity: str) -> tuple[str, str] | None:
    """TEA `*Set` sub-entities are named `{ParentEntity}{SetName}` in the
    schema; TEDS source docs drop the parent prefix and name the tail
    (`DyslexiaRiskSet`). Returns (Tail, tailCamel) when the split produces
    a meaningful Pascal identifier, else None."""
    if (
        len(pascal) > len(parent_entity)
        and pascal.startswith(parent_entity)
        and pascal[len(parent_entity)].isupper()
    ):
        tail = pascal[len(parent_entity):]
        return tail, camel_of(tail)
    return None


def naive_plural_forms(tail: str) -> tuple[str, str] | None:
    """Naive English plural for pseudo-element sub-entity tails
    (`GenderIdentity` → (`GenderIdentities`, `genderIdentities`)); None
    when the tail already ends in `s` (skip the plural overload)."""
    if not tail or tail.endswith("s"):
        return None
    plural_cap = tail[:-1] + "ies" if tail.endswith("y") else tail + "s"
    return plural_cap, camel_of(plural_cap)


def prefix_parent_of(extended: str, catalog_entity_names) -> str | None:
    """Resolve a concatenated sub-entity name to its most specific concrete
    catalog ancestor: `{ParentEntity}{SubName}` where `ParentEntity` is a
    catalog entity and the split boundary is an upper-case letter.
    Iterates longest-first so nested parent names (`Student` vs
    `StudentEducationOrganizationAssociation`) resolve to the most
    specific ancestor. None when no prefix parent exists."""
    for parent_name in sorted(catalog_entity_names, key=len, reverse=True):
        if (
            extended != parent_name
            and len(extended) > len(parent_name)
            and extended.startswith(parent_name)
            and extended[len(parent_name)].isupper()
        ):
            return parent_name
    return None


# Inherited-identity template propagation (see `StateSpine.element_keys`):
# Ed-Fi flattens the abstract `GeneralStudentProgramAssociation` /
# `StudentProgramAssociation` base into every concrete subclass at
# Swagger-emission time, but some state sandboxes (notably MN) register
# concrete program associations ONLY as extensions — the concrete entity
# is never materialized, so its inherited identity fields never surface
# through the normal walks.
SPA_TEMPLATE_ENTITY = "StudentProgramAssociation"
_SPA_EXCLUDED = ("StudentProgramAssociation", "GeneralStudentProgramAssociation")


def is_spa_template_target(entity_name: str) -> bool:
    """Concrete `Student*ProgramAssociation` entities that inherit the
    template's identity fields (excluding the template/abstract base)."""
    if entity_name in _SPA_EXCLUDED:
        return False
    return entity_name.endswith("ProgramAssociation") or entity_name.endswith(
        "ProgramAssociationExtension"
    )
