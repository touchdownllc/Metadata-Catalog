"""Pydantic models for the Ed-Fi UDM catalog extracted from swagger files."""

from pydantic import BaseModel, Field


class PropertyInfo(BaseModel):
    """A single property (data element) on an Ed-Fi entity."""

    description: str = ""
    type: str = "string"
    format: str | None = None
    is_identity: bool = False
    is_required: bool = False
    max_length: int | None = None


class ReferenceInfo(BaseModel):
    """A reference property pointing to another Ed-Fi entity."""

    entity: str = Field(description="Canonical PascalCase entity name")
    description: str | None = None
    key_properties: dict[str, PropertyInfo] = Field(
        default_factory=dict,
        description="Identity fields on the reference object",
    )


class SubCollectionInfo(BaseModel):
    """An array-of-sub-entity collection on an Ed-Fi entity."""

    sub_entity: str = Field(description="Canonical PascalCase sub-entity schema name")
    description: str | None = None
    properties: dict[str, PropertyInfo] = Field(
        default_factory=dict,
        description="Properties of the sub-entity",
    )


class EntityEntry(BaseModel):
    """A single Ed-Fi entity with its properties and references."""

    description: str | None = None
    domains: list[str] = Field(default_factory=list)
    properties: dict[str, PropertyInfo] = Field(default_factory=dict)
    references: dict[str, ReferenceInfo] = Field(default_factory=dict)
    sub_collections: dict[str, SubCollectionInfo] = Field(default_factory=dict)


class ExtensionEntry(BaseModel):
    """A state extension schema that adds properties to a core entity."""

    extends_entity: str = Field(description="Canonical core entity name this extends")
    source_prefix: str = Field(description="State prefix, e.g. 'wi', 'az'")
    properties: dict[str, PropertyInfo] = Field(default_factory=dict)
    references: dict[str, ReferenceInfo] = Field(default_factory=dict)
    sub_collections: dict[str, SubCollectionInfo] = Field(default_factory=dict)


class EdFiCatalog(BaseModel):
    """Complete Ed-Fi UDM catalog for a single data model version."""

    version: str
    source_files: list[str] = Field(default_factory=list)
    entity_count: int = 0
    extension_count: int = 0
    entities: dict[str, EntityEntry] = Field(default_factory=dict)
    extensions: dict[str, ExtensionEntry] = Field(default_factory=dict)
    lookup_index: dict[str, str] = Field(
        default_factory=dict,
        description="Lowercase normalized name -> canonical PascalCase entity name",
    )

    def get_state_extensions_for_entity(self, entity_name: str) -> list[ExtensionEntry]:
        """Return all state extension entries that extend a given core entity."""
        return [
            ext for ext in self.extensions.values()
            if ext.extends_entity == entity_name
        ]

    def has_state_extension(self, entity_name: str) -> bool:
        """True if any state extension in this catalog extends the given core entity."""
        return any(
            ext.extends_entity == entity_name
            for ext in self.extensions.values()
        )
