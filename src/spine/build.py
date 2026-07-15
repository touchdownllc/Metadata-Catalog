"""Parse Ed-Fi OpenAPI/Swagger JSON files into a lookup catalog.

Handles both Swagger 2.0 (DM 4.0, schemas in `definitions`) and
OpenAPI 3.0+ (DM 6.0, schemas in `components/schemas`).

Entry points: ``build_state_spine`` (via ``poc3 spine build``) and
``regenerate_domain_map`` (via ``poc3 spine domain-map``).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import click

from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    ExtensionEntry,
    PropertyInfo,
    ReferenceInfo,
    SubCollectionInfo,
)

logger = logging.getLogger(__name__)

# Property names Ed-Fi ODS/API emits as response infrastructure rather than
# schema elements: resource surrogate `id`, row-version audit fields (older
# `_etag` on MN's v3 ODS/API; newer `_lastModifiedDate` on v5+ AZ/WI/TX),
# and hypermedia `link`. `_ext` is kept deliberately — it's the runtime
# extension-container sentinel and we want it visible in the spine for now
# so reviewers can inspect how the API surfaces extension values. The
# catalog-level extensions walk (`extract_extensions`) enumerates the
# *actual* extension properties separately, so `_ext` being in the spine is
# double-counting, but it's informative double-counting — revisit after
# stakeholder review.
_SKIP_PROPERTY_NAMES: frozenset[str] = frozenset(
    {"id", "_etag", "_lastModifiedDate", "link"}
)

# -- Swagger format detection ────────────────────────────────────────────────


def _get_schemas(swagger: dict) -> dict:
    """Extract schema definitions from either Swagger 2.0 or OpenAPI 3.0+."""
    if "definitions" in swagger:
        return swagger["definitions"]
    return swagger.get("components", {}).get("schemas", {})


def _get_tags(swagger: dict) -> dict[str, dict]:
    """Build tag_name -> tag_info mapping from top-level tags array."""
    return {t["name"]: t for t in swagger.get("tags", [])}


def _get_path_entities(swagger: dict) -> set[str]:
    """Extract entity names that have API endpoints (top-level resources)."""
    entities: set[str] = set()
    for path in swagger.get("paths", {}):
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            # /ed-fi/students/{id} -> "students"
            # /tx/someResource/{id} -> "someResource"
            resource = parts[1].split("{")[0].rstrip("/")
            if resource:
                entities.add(resource)
    return entities


# -- Name normalization ──────────────────────────────────────────────────────

_EDFI_PREFIX = re.compile(r"^edFi_")
# Matches state prefixes (tx_, az_, wi_, mn_) AND the bundled Ed-Fi TPDM
# prefix (tpdm_). Widened from the 2-char-only form so
# `_resolve_extends_entity` strips `Tpdm_` off `Tpdm_credentialExtension`
# and correctly returns `Credential` as the extended core entity.
_STATE_PREFIX = re.compile(r"^[a-z]{2,5}_")


def schema_key_to_pascal(key: str) -> str:
    """Convert a swagger schema key to PascalCase entity name.

    edFi_studentSchoolAssociation -> StudentSchoolAssociation
    tx_courseTranscriptExtension -> CourseTranscriptExtension
    edFi_courseTranscript -> CourseTranscript
    """
    # Strip edFi_ prefix
    name = _EDFI_PREFIX.sub("", key)
    # Strip state prefix (tx_, az_) but remember it
    name = _STATE_PREFIX.sub("", name)
    # Capitalize first letter
    if name:
        name = name[0].upper() + name[1:]
    return name


def _detect_state_prefix(key: str) -> str | None:
    """Extract state prefix from schema key, e.g. 'tx_foo' -> 'tx'."""
    m = _STATE_PREFIX.match(key)
    return m.group(0).rstrip("_") if m else None


def _is_reference_schema(key: str) -> bool:
    """Check if a schema is a Reference type (e.g. edFi_schoolReference)."""
    return key.endswith("Reference")


# -- Property extraction ─────────────────────────────────────────────────────


def _extract_property(
    prop_name: str, prop_def: dict, required_set: set[str]
) -> PropertyInfo | None:
    """Extract a PropertyInfo from a swagger property definition.

    Returns None for $ref properties (those are references, not data elements).
    """
    if "$ref" in prop_def:
        return None

    # Array of sub-entities (e.g. academicSubjects) -- skip as a direct property
    # (handled separately as sub-collections)
    if prop_def.get("type") == "array":
        items = prop_def.get("items", {})
        if "$ref" in items:
            return None

    # Detect descriptor type
    prop_type = prop_def.get("type", "string")
    if prop_name.endswith("Descriptor"):
        prop_type = "descriptor"

    return PropertyInfo(
        description=prop_def.get("description", ""),
        type=prop_type,
        format=prop_def.get("format"),
        is_identity=prop_def.get("x-Ed-Fi-isIdentity", False),
        is_required=prop_name in required_set,
        max_length=prop_def.get("maxLength"),
    )


def _extract_reference(prop_name: str, prop_def: dict, schemas: dict) -> ReferenceInfo | None:
    """Extract a ReferenceInfo from a $ref property."""
    ref = prop_def.get("$ref")
    if not ref:
        return None

    # $ref: "#/definitions/edFi_schoolReference" or "#/components/schemas/edFi_schoolReference"
    ref_key = ref.split("/")[-1]
    if not _is_reference_schema(ref_key):
        return None

    # Resolve to the entity being referenced
    # edFi_schoolReference -> School
    entity_name = schema_key_to_pascal(ref_key.replace("Reference", ""))

    # Extract key properties from the reference schema itself
    # These are identity fields like educationOrganizationId on courseReference
    ref_schema = schemas.get(ref_key, {})
    desc = ref_schema.get("description")
    key_props: dict[str, PropertyInfo] = {}
    ref_required = set(ref_schema.get("required", []))
    for kp_name, kp_def in ref_schema.get("properties", {}).items():
        if "$ref" in kp_def or kp_name == "link":
            continue
        kp = _extract_property(kp_name, kp_def, ref_required)
        if kp:
            key_props[kp_name] = kp

    return ReferenceInfo(entity=entity_name, description=desc, key_properties=key_props)


def _extract_sub_collection(
    prop_name: str, prop_def: dict, schemas: dict
) -> SubCollectionInfo | None:
    """Extract a SubCollectionInfo from an array-of-$ref or single-$ref property.

    These are sub-entity collections (`addresses`, `gradeLevels`) or
    single sub-objects (TEA's `graduationProgramParticipationSet`,
    `charterWaitlistSet`) — properties whose value is another schema
    containing the actual data fields. Swagger 2.0 models multiplicity as
    `array` vs. bare `$ref`; either shape is semantically a sub-entity
    relationship for our matching purposes.
    """
    ref: str | None = None
    if prop_def.get("type") == "array":
        items = prop_def.get("items", {})
        ref = items.get("$ref")
    elif "$ref" in prop_def:
        ref = prop_def["$ref"]
    if not ref:
        return None

    ref_key = ref.split("/")[-1]
    # Skip references (handled separately)
    if _is_reference_schema(ref_key):
        return None

    sub_schema = schemas.get(ref_key, {})
    if not sub_schema:
        return None

    sub_entity = schema_key_to_pascal(ref_key)
    required_set = set(sub_schema.get("required", []))
    properties: dict[str, PropertyInfo] = {}

    for sp_name, sp_def in sub_schema.get("properties", {}).items():
        if sp_name in _SKIP_PROPERTY_NAMES:
            continue
        if "$ref" in sp_def:
            continue  # Skip nested references within sub-collections
        prop = _extract_property(sp_name, sp_def, required_set)
        if prop:
            properties[sp_name] = prop

    # Empty-properties sub-collections still get through: TEA "set"
    # placeholder schemas (e.g., `tx_schoolCharterWaitlistSet`) have zero
    # flat properties but the parent-prefix-stripping alias pass still needs
    # the sub_entity name to emit the tail as an element key on the parent.
    return SubCollectionInfo(
        sub_entity=sub_entity,
        description=sub_schema.get("description"),
        properties=properties,
    )


# -- Entity extraction ───────────────────────────────────────────────────────


def extract_entities(
    schemas: dict, tags: dict[str, dict], path_entities: set[str]
) -> dict[str, EntityEntry]:
    """Extract top-level Ed-Fi entities with their properties and references."""
    entities: dict[str, EntityEntry] = {}

    for key, schema in schemas.items():
        # Skip references
        if _is_reference_schema(key):
            continue

        # Only process edFi_ prefixed schemas (core model)
        if not key.startswith("edFi_"):
            continue

        pascal_name = schema_key_to_pascal(key)
        required_set = set(schema.get("required", []))

        # Extract properties, references, and sub-collections
        properties: dict[str, PropertyInfo] = {}
        references: dict[str, ReferenceInfo] = {}
        sub_collections: dict[str, SubCollectionInfo] = {}

        for prop_name, prop_def in schema.get("properties", {}).items():
            if prop_name in _SKIP_PROPERTY_NAMES:
                continue

            ref = _extract_reference(prop_name, prop_def, schemas)
            if ref:
                references[prop_name] = ref
                continue

            # Check for sub-collection (array of sub-entities)
            sub_coll = _extract_sub_collection(prop_name, prop_def, schemas)
            if sub_coll:
                sub_collections[prop_name] = sub_coll
                continue

            prop_info = _extract_property(prop_name, prop_def, required_set)
            if prop_info:
                properties[prop_name] = prop_info

        # Get domains from either schema-level x-Ed-Fi-domains or tags
        domains: list[str] = []
        if "x-Ed-Fi-domains" in schema:
            domains = schema["x-Ed-Fi-domains"]
        else:
            # DM 4.0: look up in tags by camelCase plural name
            camel = pascal_name[0].lower() + pascal_name[1:]
            for suffix in ["s", "es", ""]:
                tag_key = camel + suffix
                if tag_key in tags:
                    tag = tags[tag_key]
                    if "x-Ed-Fi-domains" in tag:
                        domains = tag["x-Ed-Fi-domains"]
                    break

        entities[pascal_name] = EntityEntry(
            description=schema.get("description") or _get_tag_description(pascal_name, tags),
            domains=domains,
            properties=properties,
            references=references,
            sub_collections=sub_collections,
        )

    return entities


def _get_tag_description(pascal_name: str, tags: dict[str, dict]) -> str | None:
    """Look up entity description from tags (Swagger 2.0 has descriptions in tags)."""
    camel = pascal_name[0].lower() + pascal_name[1:]
    for suffix in ["s", "es", ""]:
        tag = tags.get(camel + suffix)
        if tag and "description" in tag:
            return tag["description"]
    return None


# -- Extension extraction ────────────────────────────────────────────────────


def extract_extensions(
    schemas: dict, core_entities: dict[str, EntityEntry]
) -> dict[str, ExtensionEntry]:
    """Extract state extension schemas and link them to core entities."""
    extensions: dict[str, ExtensionEntry] = {}

    for key, schema in schemas.items():
        state_prefix = _detect_state_prefix(key)
        if not state_prefix and "Extension" not in key:
            continue
        if key.startswith("edFi_"):
            continue
        if _is_reference_schema(key):
            continue

        required_set = set(schema.get("required", []))
        properties: dict[str, PropertyInfo] = {}
        references: dict[str, ReferenceInfo] = {}
        sub_collections: dict[str, SubCollectionInfo] = {}

        for prop_name, prop_def in schema.get("properties", {}).items():
            if prop_name in _SKIP_PROPERTY_NAMES:
                continue

            ref = _extract_reference(prop_name, prop_def, schemas)
            if ref:
                references[prop_name] = ref
                continue

            sub_coll = _extract_sub_collection(prop_name, prop_def, schemas)
            if sub_coll:
                sub_collections[prop_name] = sub_coll
                continue

            if "$ref" in prop_def:
                continue

            prop_info = _extract_property(prop_name, prop_def, required_set)
            if prop_info:
                properties[prop_name] = prop_info

        if not (properties or references or sub_collections):
            continue

        # Determine which core entity this extends
        pascal_name = schema_key_to_pascal(key)
        extends_entity = _resolve_extends_entity(pascal_name, key, core_entities)
        prefix = state_prefix or "unknown"

        extensions[key] = ExtensionEntry(
            extends_entity=extends_entity,
            source_prefix=prefix,
            properties=properties,
            references=references,
            sub_collections=sub_collections,
        )

    return extensions


def _resolve_extends_entity(
    pascal_name: str, raw_key: str, core_entities: dict[str, EntityEntry]
) -> str:
    """Figure out which core entity an extension schema extends.

    Patterns:
    - tx_courseTranscriptExtension -> CourseTranscript
    - courseTranscriptExtensions -> CourseTranscript
    - tx_basicReportingPeriodAttendance -> (new entity, no core match)
    """
    # Strip "Extension" / "Extensions" suffix
    for suffix in ("Extensions", "Extension"):
        if pascal_name.endswith(suffix):
            base = pascal_name[: -len(suffix)]
            if base in core_entities:
                return base
            # Try case-insensitive
            lower = base.lower()
            for name in core_entities:
                if name.lower() == lower:
                    return name

    # No extension suffix -- this is likely a new state-specific entity
    return pascal_name


# -- Lookup index ────────────────────────────────────────────────────────────


def build_lookup_index(
    entities: dict[str, EntityEntry],
    extensions: dict[str, ExtensionEntry],
) -> dict[str, str]:
    """Build lowercase -> canonical entity name index with aliases."""
    index: dict[str, str] = {}

    def _add(key: str, name: str) -> None:
        # Don't overwrite a canonical mapping with a plural alias
        index.setdefault(key, name)

    for name in entities:
        lower = name.lower()
        index[lower] = name

        # Common aliases: strip trailing "Association"
        # Add Ext suffix alias (state elements often use e.g. CourseTranscriptExt)
        _add(lower + "ext", name)
        _add(lower + "extension", name)

        # Plural alias: state element files often use plural entity names
        # (e.g. WI uses "StudentSection504ProgramAssociations" while the
        # catalog has "StudentSection504ProgramAssociation"). Mirror the
        # singularization in src/stages/validate.py:_normalize_key.
        if lower.endswith("y"):
            _add(lower[:-1] + "ies", name)
        elif lower.endswith("ss") or lower.endswith("us") or lower.endswith("s"):
            pass
        else:
            _add(lower + "s", name)

    # Extension entities that are new (not extending core)
    for ext in extensions.values():
        if ext.extends_entity not in entities:
            lower = ext.extends_entity.lower()
            _add(lower, ext.extends_entity)

    return index

# -- Domain map (Ed-Fi Data Standard fallback) ───────────────────────────────


def build_domain_map(resources_path: Path) -> dict[str, list[str]]:
    """Extract {PascalEntityName: [domain, ...]} from a domain-rich swagger.

    Some state sandboxes (AZ) ship `x-Ed-Fi-domains` tags on every entity;
    others (WI, MN) strip them. Run this on a domain-rich source to produce a
    canonical asset that fills in missing domain assignments elsewhere.
    """
    with open(resources_path) as f:
        swagger = json.load(f)
    schemas = _get_schemas(swagger)
    tags = _get_tags(swagger)
    path_entities = _get_path_entities(swagger)
    entities = extract_entities(schemas, tags, path_entities)
    return {name: list(ent.domains) for name, ent in entities.items() if ent.domains}


def regenerate_domain_map(source_state: str = "AZ") -> Path:
    """Build and write data/spine/edfi_domain_map.json from a state's cached swagger."""
    source_state = source_state.upper()
    resources_path = (
        PROJECT_ROOT / "data" / "raw" / source_state.lower() / "swagger" / "resources.json"
    )
    if not resources_path.exists():
        raise FileNotFoundError(
            f"No cached swagger at {resources_path}. "
            f"Run `poc3 spine fetch --state {source_state}` first."
        )
    mapping = build_domain_map(resources_path)
    if not mapping:
        raise click.ClickException(
            f"Source {source_state} swagger has no x-Ed-Fi-domains tags — "
            f"pick a domain-rich source (AZ)."
        )
    out_path = PROJECT_ROOT / "data" / "spine" / "edfi_domain_map.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(dict(sorted(mapping.items())), indent=2) + "\n")
    logger.info("Wrote %d entity domains -> %s (source: %s)", len(mapping), out_path, source_state)
    return out_path


def _find_parent_with_domains(
    entity_name: str, domain_map: dict[str, list[str]]
) -> list[str] | None:
    """Longest-prefix match that returns domains from a parent entity.

    Ed-Fi swagger names sub-collection entities as `{ParentEntity}{SubName}`
    (e.g., `ApplicantProfileAddress` is a sub-collection on `ApplicantProfile`).
    Only the top-level resource entities ship `x-Ed-Fi-domains` tags, so the
    341 sub-collections in AZ's spine end up domainless. Walk every prefix
    of the sub-entity name (longest first) and return the first match that
    lands on a domain-mapped parent — that parent's domains are the most
    specific applicable inheritance.

    Returns None if no prefix is present in the domain map.
    """
    # Walk boundaries where an uppercase letter starts a new token, longest
    # prefix first. A candidate prefix must be at least 3 chars to avoid
    # spurious short matches.
    candidates: list[str] = []
    for i in range(len(entity_name) - 1, 0, -1):
        if entity_name[i].isupper() and len(entity_name[:i]) >= 3:
            candidates.append(entity_name[:i])
    for prefix in candidates:
        if prefix in domain_map:
            return list(domain_map[prefix])
    return None


def apply_domain_map(
    entities: dict[str, EntityEntry], domain_map: dict[str, list[str]]
) -> int:
    """Fill empty `domains` on entities from `domain_map`. Returns count filled.

    Existing non-empty domain lists are preserved — this only patches gaps.
    Two-pass fill:
        1. Direct name lookup in the map.
        2. Longest-prefix-parent inheritance for sub-collection entities that
           aren't individually tagged in swagger. This closes the analyst
           flag "341 '(unassigned)' rows dominate AZ's Entities by Domain sheet."
    """
    filled = 0
    for name, entry in entities.items():
        if entry.domains:
            continue
        if name in domain_map:
            entry.domains = list(domain_map[name])
            filled += 1
            continue
        parent_domains = _find_parent_with_domains(name, domain_map)
        if parent_domains is not None:
            entry.domains = parent_domains
            filled += 1
    return filled


# -- Catalog builder ─────────────────────────────────────────────────────────


def build_catalog(
    resources_path: Path,
    version: str,
    state_extensions_path: Path | None = None,
) -> EdFiCatalog:
    """Build a complete Ed-Fi catalog from swagger JSON files.

    Args:
        resources_path: Path to core resources swagger JSON
        version: Ed-Fi data model version (e.g. "4.0", "6.0")
        state_extensions_path: Optional path to state extensions swagger JSON
    """
    logger.info("Loading resources swagger: %s", resources_path)
    with open(resources_path) as f:
        swagger = json.load(f)

    schemas = _get_schemas(swagger)
    tags = _get_tags(swagger)
    path_entities = _get_path_entities(swagger)

    logger.info(
        "Detected %s format with %d schemas, %d API paths",
        "OpenAPI 3.0" if "openapi" in swagger else "Swagger 2.0",
        len(schemas),
        len(path_entities),
    )

    entities = extract_entities(schemas, tags, path_entities)
    logger.info("Extracted %d entities", len(entities))

    # Parse state extensions if provided
    extensions: dict[str, ExtensionEntry] = {}
    if state_extensions_path:
        logger.info("Loading state extensions: %s", state_extensions_path)
        with open(state_extensions_path) as f:
            ext_swagger = json.load(f)
        ext_schemas = _get_schemas(ext_swagger)
        ext_tags = _get_tags(ext_swagger)
        ext_path_entities = _get_path_entities(ext_swagger)
        extensions = extract_extensions(ext_schemas, entities)
        logger.info("Extracted %d extension schemas", len(extensions))

        # Some state bundles backport new edFi_* entities that postdate the
        # core swagger version (e.g. WI's 5.2 bundle ships
        # edFi_studentSection504ProgramAssociation and edFi_studentTransportation
        # which were added to core in 6.0). Without this merge, those entities
        # are missing from the catalog and `detect_extension_entity()` flags
        # them as state extensions, applying a spurious +0.5 entity-level
        # adjustment to every element in them.
        backported = extract_entities(ext_schemas, ext_tags, ext_path_entities)
        added = 0
        for name, entry in backported.items():
            if name not in entities:
                entities[name] = entry
                added += 1
        if added:
            logger.info(
                "Backported %d edFi_* entities from extensions bundle", added
            )

    lookup = build_lookup_index(entities, extensions)
    logger.info("Built lookup index with %d entries", len(lookup))

    return EdFiCatalog(
        version=version,
        source_files=[
            str(resources_path),
            *([] if not state_extensions_path else [str(state_extensions_path)]),
        ],
        entity_count=len(entities),
        extension_count=len(extensions),
        entities=entities,
        extensions=extensions,
        lookup_index=lookup,
    )


# -- Module paths ────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# -- POC-3 spine entry point ──────────────────────────────────────────────────

from datetime import datetime, timezone

from src.models.spine import SpineSourceURLs, StateSpine


def build_state_spine(state: str) -> StateSpine:
    """Load cached Swagger from data/raw/{state}/swagger/ and build a StateSpine.

    Writes data/spine/{state_lower}_spine.json. Assumes `poc3 spine fetch`
    has already been run for this state.
    """
    state = state.upper()
    raw_dir = PROJECT_ROOT / "data" / "raw" / state.lower() / "swagger"
    resources_path = raw_dir / "resources.json"

    if not resources_path.exists():
        raise FileNotFoundError(
            f"No cached swagger for {state} at {resources_path}. "
            f"Run `poc3 spine fetch --state {state}` first."
        )

    meta_path = raw_dir / "meta.json"
    meta: dict = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    with open(resources_path) as f:
        swagger = json.load(f)
    edfi_version = swagger.get("info", {}).get("version", "unknown")

    # State sandboxes return a combined bundle — core entities (edFi_*) and
    # state extensions (az_*, wi_*, etc.) in one resources.json. build_catalog
    # alone only extracts extensions from a separate file, so we assemble the
    # catalog here directly from the single combined swagger.
    schemas = _get_schemas(swagger)
    tags = _get_tags(swagger)
    path_entities = _get_path_entities(swagger)
    entities = extract_entities(schemas, tags, path_entities)
    extensions = extract_extensions(schemas, entities)

    domain_map_path = PROJECT_ROOT / "data" / "spine" / "edfi_domain_map.json"
    if domain_map_path.exists():
        domain_map = json.loads(domain_map_path.read_text())
        filled = apply_domain_map(entities, domain_map)
        if filled:
            logger.info("Filled domains on %d entities from %s", filled, domain_map_path.name)

    lookup = build_lookup_index(entities, extensions)

    catalog = EdFiCatalog(
        version=edfi_version,
        source_files=[str(resources_path)],
        entity_count=len(entities),
        extension_count=len(extensions),
        entities=entities,
        extensions=extensions,
        lookup_index=lookup,
    )

    fetched_at_raw = meta.get("fetched_at")
    fetched_at = (
        datetime.fromisoformat(fetched_at_raw)
        if fetched_at_raw
        else datetime.now(tz=timezone.utc)
    )

    urls = meta.get("urls", {})
    spine = StateSpine(
        state=state,
        edfi_version=edfi_version,
        fetched_at=fetched_at,
        school_year=meta.get("school_year"),
        source_urls=SpineSourceURLs(
            resources=urls.get("resources", str(resources_path)),
            descriptors=urls.get("descriptors"),
        ),
        catalog=catalog,
    )

    out_path = PROJECT_ROOT / "data" / "spine" / f"{state.lower()}_spine.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(spine.model_dump_json(indent=2), encoding="utf-8")
    logger.info(
        "Built %s spine: %d entities, %d extensions -> %s",
        state,
        catalog.entity_count,
        catalog.extension_count,
        out_path,
    )
    return spine

