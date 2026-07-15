"""Entity and element name normalization for cross-state matching.

State-specific parsers (AZ XLSX, WI Confluence, MN GitHub) use different
naming conventions for the same underlying Ed-Fi entities and elements:

- Entity plurals: "Calendars" vs "Calendar"
- Acronym casing: "CTEProgram..." vs "CteProgram..."
- Ext suffix: "CourseTranscriptExt" vs "CourseTranscript"
- Whitespace: "foo (bar)" vs "foo(bar)"

These normalizers produce a canonical form so cross-state comparison and
spine alignment work consistently.
"""

import re

from src.states import STATE_INFO

# Explicit entity renames where simple rules don't work. Map source-side display
# forms → canonical spine form (what the Ed-Fi swagger calls it). Direction
# matters — the rename normalizes AWAY from the source variant so `match_key`
# lines up with `StateSpine.element_keys()`.
_ENTITY_RENAMES: dict[str, str] = {
    "CourseTranscriptExt": "CourseTranscript",
    "StudentRestraintEvent": "RestraintEvent",
    # WI Confluence uses plural `FoodServices`; spine is singular `FoodService`.
    "StudentSchoolFoodServicesProgramAssociation": "StudentSchoolFoodServiceProgramAssociation",
}


# State-extension namespace prefixes the spine and reviewer files emit on TEA /
# WI / MN / AZ / IDOE extension entities (e.g. `tx_studentApplication`,
# `idoe/educationOrganizationOtherPersonnel`, `idoe_schoolExtension`). The
# state's source doc generally enumerates the same entity unprefixed
# (`StudentApplication`); strip the prefix so both sides resolve to the same
# canonical form during reviewer-comparison joins. Conservative whitelist —
# generic prefix-stripping would over-fire on legitimate names. Both `_` and
# `/` separators are recognized: reviewer files name IDOE extensions with the
# API-path-style `idoe/X` while POC-3 sidecars use the bare PascalCase form.
# Issue #160. `ed-?fi` + the `.` separator joined the whitelist 2026-07-07:
# the per-state human-scored basis (IN workbook) names core entities BOTH
# API-path-style (`ed-fi/staffEducationOrganizationEmploymentAssociation`,
# 29 rows) and IDOE-sheet dot-namespace style
# (`edfi.StudentEducationOrganizationAssociation`, ~295 rows) — the
# core-namespace analogues of `idoe/X` / `idoe.X`, equally safe to strip
# (POC-3 record keys are always bare).
#
# Issue #213 item 2: the per-state tokens DERIVE from
# `src.states.STATE_INFO` so a sixth state's prefix joins automatically
# (the IN 18.2%→94.5% episode was exactly this literal missing `idoe`).
# `ed-?fi` is a core-namespace token, not a state prefix, so it stays an
# explicit extra here. Behavior-equivalence with the pre-derivation literal
# `^(?:tx|wi|mn|az|idoe|ed-?fi)[_/.](?=[A-Za-z0-9])` is pinned in
# tests/test_matching.py (token order in the alternation is immaterial —
# all alternatives are disjoint at the anchored position).
_EXTRA_PREFIX_TOKENS: tuple[str, ...] = ("ed-?fi",)
_STATE_PREFIX_TOKENS: tuple[str, ...] = tuple(
    dict.fromkeys(p for info in STATE_INFO.values() for p in info.ext_prefixes)
)
_STATE_EXT_PREFIX_RE = re.compile(
    r"^(?:"
    + "|".join(_STATE_PREFIX_TOKENS + _EXTRA_PREFIX_TOKENS)
    + r")[_/.](?=[A-Za-z0-9])",
    re.IGNORECASE,
)


def entity_match_form(name: str) -> str:
    """Normalize an entity name to the lowercase/singular MATCHING form.

    Handles: explicit renames, state-extension prefix strip, acronym casing,
    Ed-Fi plural->singular.

    Renamed from ``normalize_entity`` (issue #213 item 2): two same-named
    functions with opposite semantics existed — ``ingest.normalize
    .normalize_entity`` returns the canonical PascalCase DISPLAY form, while
    this one returns the lowercased/depluralized join key. The name now says
    which one you get.
    """
    if name in _ENTITY_RENAMES:
        name = _ENTITY_RENAMES[name]

    # Strip state-extension prefix (`tx_studentApplication` →
    # `studentApplication`, `idoe/educationOrganizationOtherPersonnel` →
    # `educationOrganizationOtherPersonnel`) before lower-casing so the
    # prefix-stripped form joins to the source-doc's unprefixed enumeration.
    name = _STATE_EXT_PREFIX_RE.sub("", name, count=1)

    lower = name.lower()

    if (
        lower.endswith("s")
        and not lower.endswith("ss")
        and not lower.endswith("us")
        and not lower.endswith("is")
        and not lower.endswith("sis")
        and len(lower) > 4
    ):
        lower = lower[:-1]

    return lower


# Deprecated compat alias — prefer `entity_match_form`. Kept because
# `ingest.shared.canonical_spine_emit_keys` (alias-grammar-generator
# territory — the issue #213 item 2 primary workstream) still imports the
# old name; retire the alias when that generator lands.
normalize_entity = entity_match_form


def normalize_element(name: str) -> str:
    """Normalize an element name for matching.

    Handles: whitespace before parentheses, e.g.
    "alternativeCourseCode (used by Xello)" -> "alternativeCourseCode(used by Xello)"
    """
    return re.sub(r"\s+\(", "(", name)


def element_aliases(name: str) -> set[str]:
    """Generate conservative alias forms for nested/surfaced element paths.

    Intentionally narrow: strips leading path segments from dotted names
    such as `school.schoolId` -> `schoolId`. No fuzzy matching.
    """
    normalized = normalize_element(name)
    aliases = {normalized}

    stripped_note = re.sub(r"\s*\([^)]*\)\s*$", "", normalized).strip()
    if stripped_note:
        aliases.add(stripped_note)

    dotted = normalized.replace(": ", ".").replace(":", ".")
    aliases.add(dotted)
    aliases.add(normalized.replace(": ", "").replace(":", ""))

    # `>`-separated nav paths (MN matrix style: `StudentReference>StudentUniqueId`)
    # are equivalent to dot paths for matching purposes — flatten them into the
    # dotted form so the same path-tail logic applies.
    arrow_normalized = re.sub(r"\s*>\s*", ".", dotted)
    if arrow_normalized != dotted:
        aliases.add(arrow_normalized)

    # Path-tail splits: `schoolReference.schoolId` -> `schoolId`. Apply to all
    # forms so colon-separated Confluence paths and `>`-separated MN nav
    # paths all split correctly.
    for source in (normalized, dotted, arrow_normalized):
        parts = source.split(".")
        if len(parts) > 1:
            for idx in range(1, len(parts)):
                aliases.add(".".join(parts[idx:]))
            aliases.add(parts[-1])

    # Internal whitespace collapse: MN mapping-matrix authors occasionally
    # write human-friendly labels like `Course Code` or `Course Offering
    # Reference` where the Ed-Fi form is the PascalCase identifier. Also
    # strips any trailing parenthetical note before collapsing. Both the
    # fully-PascalCase (`CourseCode`) and first-letter-lowered
    # (`courseCode`) forms are emitted.
    whitespace_source = re.sub(r"\s*\([^)]*\)\s*", " ", stripped_note or normalized).strip()
    if " " in whitespace_source:
        tokens = [t for t in re.split(r"\s+", whitespace_source) if t]
        if tokens:
            pascal = "".join(t[0].upper() + t[1:] if t else t for t in tokens)
            aliases.add(pascal)
            aliases.add(pascal[0].lower() + pascal[1:])

    # Plural-collection descriptor names (`courseLevelCharacteristicsDescriptor`)
    # — MN matrix authors sometimes pluralize the noun before `Descriptor`
    # whereas Ed-Fi keeps the singular form (`courseLevelCharacteristicDescriptor`).
    # Emit the singular alias when the pattern matches.
    for alias in list(aliases):
        if alias.endswith("sDescriptor") and len(alias) > len("sDescriptor"):
            aliases.add(alias[: -len("sDescriptor")] + "Descriptor")

    # Singular ↔ plural sub-collection aliases. Ed-Fi 4.0 names
    # sub-collections in the plural (`addresses`, `telephones`,
    # `electronicMails`) but TEDS-style source docs name them singularly
    # (`Parent.Address`, `Parent.Telephone`). Emit plural candidates for
    # singular aliases and vice versa so either direction resolves. Also
    # applies to entity-style sub-entities (`*Set` → `*Sets`).
    for alias in list(aliases):
        if not alias:
            continue
        lower_alias = alias.lower()
        # Plural candidates from a singular
        if not lower_alias.endswith("s"):
            aliases.add(alias + "s")
        elif lower_alias.endswith("y") and len(alias) > 2:
            aliases.add(alias[:-1] + "ies")
        # Singular from a plural — only strip when the result is meaningful
        # (avoid stripping `s` off `address` → `addres`). Use the same
        # guards as `entity_match_form`.
        if (
            lower_alias.endswith("s")
            and not lower_alias.endswith("ss")
            and not lower_alias.endswith("us")
            and not lower_alias.endswith("is")
            and not lower_alias.endswith("sis")
            and len(alias) > 4
        ):
            if lower_alias.endswith("ies") and len(alias) > 4:
                aliases.add(alias[:-3] + "y")
            elif lower_alias.endswith("es") and len(alias) > 5:
                aliases.add(alias[:-2])
                aliases.add(alias[:-1])
            else:
                aliases.add(alias[:-1])

    return aliases


def match_key(entity: str, element_name: str) -> tuple[str, str]:
    """Produce a normalized (entity, element) key for matching."""
    return (entity_match_form(entity), normalize_element(element_name))


def record_match_keys(entity: str, element_name: str) -> set[tuple[str, str]]:
    """All candidate (entity, element) keys for matching a source record to the spine.

    Emits the direct key AND all element aliases (path-tail splits), so nested
    Confluence-style names like `school.schoolId` also match the plain `schoolId`
    spine slot. Entity is normalized once; element forms are lowercased for
    case-insensitive comparison.
    """
    entity_norm = entity_match_form(entity)
    return {
        (entity_norm, alias.lower())
        for alias in element_aliases(element_name)
    }


def attribute_record_source(records, spine) -> None:
    """Set per-record `source`/`extension_name` on records by spine lookup.

    Mutates `records` in place (replacing each via `model_copy`). Three outcomes:

    - any of the record's match-keys lands on a spine extension key
      → `source="extension"`, `extension_name=<extension catalog key>`
        (e.g., `mn_calendarExtension`, `wi_credentialExtension`)
    - record matches the spine but only on core keys
      → `source="core"`, `extension_name=None`
    - no spine match at all
      → `source="unknown"`, `extension_name=None`

    Use this AFTER unflatten recovery, so the rewritten `entity` is what we
    look up. AZ sets `source` at record-creation time (the XLSX namespace
    explicitly marks `edfi.*` vs `az.*`); WI/MN have no per-element extension
    signal in their source docs and rely on this spine-derived attribution.
    """
    spine_pairs_lower = {match_key(e, n) for (e, n) in spine.element_keys()}
    spine_pairs_lower = {(e, n.lower()) for (e, n) in spine_pairs_lower}
    ext_pairs_lower: dict[tuple[str, str], str] = {}
    for (e, n), ext_key in spine.extension_element_keys().items():
        e_norm, n_norm = match_key(e, n)
        ext_pairs_lower[(e_norm, n_norm.lower())] = ext_key

    for i, r in enumerate(records):
        # Sort so the first extension-key hit is deterministic across
        # processes — `record_match_keys` returns a set whose iteration
        # order depends on PYTHONHASHSEED. Without the sort, a record with
        # aliases landing on two different extensions could attribute to
        # either one depending on the run.
        keys = sorted(record_match_keys(r.entity, r.element_name))
        ext_hit = next((ext_pairs_lower[k] for k in keys if k in ext_pairs_lower), None)
        if ext_hit is not None:
            records[i] = r.model_copy(update={"source": "extension", "extension_name": ext_hit})
        elif any(k in spine_pairs_lower for k in keys):
            records[i] = r.model_copy(update={"source": "core", "extension_name": None})
        else:
            records[i] = r.model_copy(update={"source": "unknown", "extension_name": None})
