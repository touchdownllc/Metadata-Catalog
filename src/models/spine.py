"""Per-state spine manifest — the authoritative scope derived from live sandbox Swagger."""

from datetime import datetime

from pydantic import BaseModel, Field

from src.models.alias_grammar import (
    EDORG_SUBTYPE_NAMES,
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
from src.models.edfi_catalog import EdFiCatalog

# Compat aliases — `ingest.shared` (and older tests) import the rule
# constants under their pre-#213 private names; the one home is now
# `models/alias_grammar.py`.
_EDORG_SUBTYPE_NAMES = EDORG_SUBTYPE_NAMES
_collapse_camel_overlap = collapse_camel_overlap


class SpineSourceURLs(BaseModel):
    resources: str
    descriptors: str | None = None


class StateSpine(BaseModel):
    """The authoritative domain/entity/element scope for a single state,
    fetched live from that state's Ed-Fi sandbox Swagger."""

    state: str = Field(description="State abbreviation, e.g. 'AZ', 'WI', 'MN'")
    edfi_version: str = Field(description="Ed-Fi Data Standard version declared in swagger info.version")
    fetched_at: datetime = Field(description="When the source swagger was fetched")
    school_year: int | None = Field(
        default=None,
        description="School year used to fill {schoolYearFromRoute} (WI only)",
    )
    source_urls: SpineSourceURLs
    catalog: EdFiCatalog = Field(description="Parsed domain/entity/element catalog")

    @property
    def entity_count(self) -> int:
        return self.catalog.entity_count

    @property
    def extension_count(self) -> int:
        return self.catalog.extension_count

    def entity_keys(self) -> set[str]:
        """All canonical entity names in the spine, including core and extensions."""
        keys: set[str] = set(self.catalog.entities.keys())
        for ext in self.catalog.extensions.values():
            keys.add(ext.extends_entity)
        return keys

    def element_keys(self) -> set[tuple[str, str]]:
        """All matchable (entity, element_name) pairs in the spine.

        Covers core entities and state extensions, and emits expansions so that
        state docs' conventions (which differ from Swagger naming) can match:

        - Plain property names: `calendarCode`
        - Reference objects: `schoolReference` **plus** the flattened FK fields
          inside them (`schoolId`, `schoolYear`) — state docs typically list
          the FK, not the Swagger reference object.
        - Reference-prefixed FKs: `programReference.educationOrganizationId`
          also emits `programEducationOrganizationId`, because AZ-style docs
          disambiguate multiple FKs to the same base field by prefixing the
          reference name (minus the `Reference` suffix). Same treatment for
          Descriptor keys (`{ref}SomethingDescriptorId`).
        - Sub-collection arrays: `gradeLevels`
        - Descriptor properties emit both the Swagger name (`calendarTypeDescriptor`)
          and the `-Id`-suffixed variant (`calendarTypeDescriptorId`) that
          AZ/TX-style docs use.
        - State-extension properties (same Descriptor expansion).

        Used by ingest adapters to answer 'is this element in the spine?'.
        """
        pairs: set[tuple[str, str]] = set()

        def _add_with_descriptor_variants(entity_name: str, prop_name: str) -> None:
            pairs.add((entity_name, prop_name))
            # AZ/TX `-Id` suffix + MDE bare form — one rule home
            # (alias_grammar; issue #213 item 2). NOTE the pre-#213 body
            # emitted the bare stem even when empty-string; the helper
            # skips empty stems (a name that IS "Descriptor" — no such
            # property exists in any state spine, snapshot-verified).
            for variant in descriptor_variants(prop_name):
                pairs.add((entity_name, variant))

        def _add_refs_and_subs(
            entity_name: str,
            refs: dict,
            subs: dict,
        ) -> None:
            """Emit spine keys for a (refs, sub_collections) bundle.

            Shared between core entities and extension entities so TEA-style
            extensions that redeclare FK references (`schoolReference`,
            `studentReference`) on TEA-new entities like
            `BasicReportingPeriodAttendance` go through the same FK-alias
            expansion as core. Without this, those references were dropped
            and TEDS `School`/`Student` element rows stayed Unresolved.
            """
            for ref_name, ref in refs.items():
                pairs.add((entity_name, ref_name))
                prefix = reference_prefix(ref_name)
                # MDE mapping-matrix cells often name the target entity itself
                # (`Course`, `Student`, `Responsibility`) in place of Swagger's
                # `courseReference` / `studentReference`. Emit the reference's
                # bare prefix as an alias so those rows resolve.
                if prefix and prefix != ref_name:
                    pairs.add((entity_name, prefix))
                # Ed-Fi 4.0 collapsed the EducationOrganization concrete
                # subtypes (School, LocalEducationAgency, etc.) into a single
                # `educationOrganizationReference`. TEA (and other SEAs) still
                # author source docs using the concrete subtype names, so emit
                # each subtype's Pascal + camel form as an alias on the same
                # parent entity. Safe to apply cross-state because no Ed-Fi
                # entity has both `educationOrganizationReference` and a
                # subtype-specific FK on the same parent.
                if ref.entity == "EducationOrganization":
                    for alias in edorg_subtype_aliases():
                        pairs.add((entity_name, alias))
                qualifier = reference_qualifier(prefix, ref.entity)

                def _add_fk_alias(alias: str) -> None:
                    """Emit a composite-FK alias plus its Id-stripped form
                    (MDE drops the `Id` suffix — see alias_grammar)."""
                    _add_with_descriptor_variants(entity_name, alias)
                    stripped = id_stripped_alias(alias)
                    if stripped:
                        pairs.add((entity_name, stripped))

                for kp in ref.key_properties:
                    _add_with_descriptor_variants(entity_name, kp)
                    # `*UniqueId` surrogate keys referenced as plain `*Id`.
                    uid = uniqueid_id_alias(kp)
                    if uid:
                        pairs.add((entity_name, uid))
                    if prefix and prefix != kp:
                        _add_fk_alias(fk_prefixed_alias(prefix, kp))
                        collapsed = collapse_camel_overlap(
                            prefix, capitalize_first(kp)
                        )
                        if collapsed:
                            _add_fk_alias(collapsed)
                    if qualifier:
                        _add_fk_alias(fk_prefixed_alias(qualifier, kp))
            for sub_name, sub in subs.items():
                pairs.add((entity_name, sub_name))
                for sp in sub.properties:
                    _add_with_descriptor_variants(entity_name, sp)
                # TEDS-style source docs sometimes name a sub-collection by
                # its concrete sub-entity type (`Parent.Address`,
                # `ObjectiveAssessment.AssessmentPerformanceLevel`) rather
                # than the collection property name (`addresses`,
                # `performanceLevels`). Emit the sub-entity Pascal form and
                # its camelCase variant as aliases on the parent so those
                # rows resolve.
                if sub.sub_entity:
                    for form in sub_entity_name_forms(sub.sub_entity):
                        pairs.add((entity_name, form))
                    # TEA `{ParentEntity}{SetName}` → bare-tail alias
                    # (`DyslexiaRiskSet`) — rule in alias_grammar.
                    tail_forms = parent_stripped_tail(
                        sub.sub_entity, entity_name
                    )
                    if tail_forms:
                        tail, tail_camel = tail_forms
                        pairs.add((entity_name, tail))
                        pairs.add((entity_name, tail_camel))

        for entity_name, entity in self.catalog.entities.items():
            for prop in entity.properties:
                _add_with_descriptor_variants(entity_name, prop)
            _add_refs_and_subs(entity_name, entity.references, entity.sub_collections)

        for ext in self.catalog.extensions.values():
            for prop in ext.properties:
                _add_with_descriptor_variants(ext.extends_entity, prop)
            # Extensions that redeclare FK references (TEA `tx_*` new-entity
            # extensions like `tx_basicReportingPeriodAttendance` ship
            # `schoolReference`/`studentReference` that `extract_extensions`
            # now preserves) get the same FK-alias expansion as core entities.
            _add_refs_and_subs(
                ext.extends_entity, ext.references, ext.sub_collections
            )
            # Extension-extending sub-entities (e.g. MN's
            # `mn_studentEducationOrganizationAssociationLanguageAcademicHonor`
            # extends `StudentEducationOrganizationAssociationLanguageAcademicHonor`,
            # a sub-entity of `StudentEducationOrganizationAssociation`) surface
            # their properties only on the concatenated name by default. When
            # the extends_entity splits as `{ParentEntity}{SubName}` where
            # `ParentEntity` is a catalog entity, also attribute the properties
            # to the parent so source docs listing the bare sub-entity name
            # resolve after unflatten's suffix-match rewrite.
            extended = ext.extends_entity
            # Most-specific concrete ancestor via the shared longest-first
            # prefix-parent rule (alias_grammar.prefix_parent_of). NOTE:
            # deliberately NO `extended in catalog.entities` guard here —
            # `extension_element_keys` HAS one (analyst round-2
            # misattribution fix); this membership walk keeps the wider
            # pre-#213 behavior byte-for-byte (documented divergence).
            parent_name = prefix_parent_of(extended, self.catalog.entities)
            if parent_name is not None:
                for prop in ext.properties:
                    _add_with_descriptor_variants(parent_name, prop)

        # Inherited-identity synthesis for concrete `Student*ProgramAssociation`
        # entities. Ed-Fi flattens the abstract `GeneralStudentProgramAssociation`
        # / `StudentProgramAssociation` base into every concrete subclass at
        # Swagger-emission time, but some state sandboxes (notably MN) register
        # concrete program associations ONLY as `mn_*` extensions whose
        # `extends_entity` is the concrete name — the concrete entity itself is
        # never materialized in `catalog.entities`, so its inherited identity
        # fields (`programType`, `programName`, `studentUniqueId`, etc.) never
        # surface through the normal property walk. Copy the shared base's
        # element keys onto every such concrete entity so state docs that list
        # the inherited fields resolve.
        template = self.catalog.entities.get(SPA_TEMPLATE_ENTITY)
        if template is not None:
            template_keys = {n for e, n in pairs if e == SPA_TEMPLATE_ENTITY}
            all_entity_names = {e for e, _ in pairs}
            for ext in self.catalog.extensions.values():
                all_entity_names.add(ext.extends_entity)
            for entity_name in all_entity_names:
                if not is_spa_template_target(entity_name):
                    continue
                for k in template_keys:
                    pairs.add((entity_name, k))

        return pairs

    def extension_element_keys(self) -> dict[tuple[str, str], str]:
        """Per-extension attribution map: (entity, element_name) -> extension_key.

        Parallel to the extension-contribution loop in `element_keys()` (props
        on `ext.extends_entity` plus parent-propagation for sub-entity
        extensions), but tags each key with the contributing extension's
        catalog key (e.g., `mn_calendarExtension`, `wi_credentialExtension`) —
        the dict key under `catalog.extensions`. The key carries both the state
        prefix and the specific extension schema name, so downstream consumers
        get finer attribution than `source_prefix` alone (which would only say
        `mn` / `wi`).

        Used by ingest adapters to mark per-record `ElementRecord.source =
        "extension"` and `extension_name = <extension_key>` instead of the
        coarse entity-level "any extension on this entity" heuristic.

        When two extensions contribute the same key, the first-iteration win
        is arbitrary — the only consumer is the analyst report's free-text
        `extension_name` column, which is informational.
        """
        pairs: dict[tuple[str, str], str] = {}

        def _add(entity_name: str, prop_name: str, ext_key: str) -> None:
            pairs.setdefault((entity_name, prop_name), ext_key)
            for variant in descriptor_variants(prop_name):
                pairs.setdefault((entity_name, variant), ext_key)

        for ext_key, ext in self.catalog.extensions.items():
            for prop in ext.properties:
                _add(ext.extends_entity, prop, ext_key)
            # Reference-carried FK aliases contributed by the extension get
            # the same attribution. The ref name itself and the prefix-only
            # alias (`schoolReference` → `school`) are the common cases TEDS
            # source docs use; the key-property expansion is optional for
            # attribution (analyst reporting only cares about the target
            # element's ext_key) but kept for symmetry with element_keys.
            for ref_name, ref in ext.references.items():
                _add(ext.extends_entity, ref_name, ext_key)
                prefix = reference_prefix(ref_name)
                if prefix and prefix != ref_name:
                    _add(ext.extends_entity, prefix, ext_key)
                # Same EducationOrganization subtype expansion as
                # `_add_refs_and_subs` so extension-contributed
                # `educationOrganizationReference` slots also attribute the
                # concrete-subtype aliases back to the extension.
                if ref.entity == "EducationOrganization":
                    for alias in edorg_subtype_aliases():
                        _add(ext.extends_entity, alias, ext_key)
                for kp in ref.key_properties:
                    _add(ext.extends_entity, kp, ext_key)
            for sub_name, sub in ext.sub_collections.items():
                _add(ext.extends_entity, sub_name, ext_key)
                if sub.sub_entity:
                    for form in sub_entity_name_forms(sub.sub_entity):
                        _add(ext.extends_entity, form, ext_key)
                    # Same `{ParentEntity}{SetName}` → `SetName` tail
                    # stripping as the core `_add_refs_and_subs` path.
                    tail_forms = parent_stripped_tail(
                        sub.sub_entity, ext.extends_entity
                    )
                    if tail_forms:
                        tail, tail_camel = tail_forms
                        _add(ext.extends_entity, tail, ext_key)
                        _add(ext.extends_entity, tail_camel, ext_key)
                for sp in sub.properties:
                    _add(ext.extends_entity, sp, ext_key)
            extended = ext.extends_entity
            # When the extension's target is itself a top-level catalog entity
            # (e.g., StudentAssessment), don't propagate its properties up to
            # a prefix parent (Student). Analyst review round-2 flagged rows
            # on `Student` getting misattributed to `mn_studentAssessmentExtension`
            # via this loop — StudentAssessment is the correct target, not Student.
            # Propagation exists for concatenated SUB-ENTITY names that aren't
            # materialized in the catalog (e.g., `StudentEducationOrganization
            # AssociationGenderIdentity` → propagate to `StudentEducation
            # OrganizationAssociation`); when `extended` is itself in the
            # catalog, no propagation is warranted.
            if extended in self.catalog.entities:
                continue
            parent_name = prefix_parent_of(extended, self.catalog.entities)
            if parent_name is not None:
                for prop in ext.properties:
                    _add(parent_name, prop, ext_key)
                # Also emit the sub-entity name itself as a "pseudo-element"
                # on the parent. MN mapping-matrix authors sometimes name
                # the sub-entity (e.g., `GenderIdentities` on sEOA) as the
                # element value rather than drilling into a specific
                # property like `genderIdentityDescriptor`. Emit the bare
                # sub-entity tail (singular) plus a naive plural variant
                # so the analyst-facing element value still attributes to
                # the right extension schema.
                sub_tail = extended[len(parent_name):]
                if sub_tail and sub_tail[0].isupper():
                    _add(parent_name, sub_tail[0].lower() + sub_tail[1:], ext_key)
                    _add(parent_name, sub_tail, ext_key)
                    plural_forms = naive_plural_forms(sub_tail)
                    if plural_forms:
                        plural_cap, plural_lower = plural_forms
                        _add(parent_name, plural_cap, ext_key)
                        _add(parent_name, plural_lower, ext_key)

        return pairs
