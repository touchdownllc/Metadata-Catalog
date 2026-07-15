"""Per-element canonical records from state specifications."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ElementRecord(BaseModel):
    """A single data element extracted from a state specification."""

    # Reject unknown kwargs (issue #211 item 4): pydantic's default
    # extra="ignore" silently dropped a typo'd/ghost field for months
    # (wisconsin.py passed a nonexistent `extraction_confidence` on
    # every run) — a misspelled optional field in any adapter would
    # silently default and ship wrong data with zero signal.
    model_config = ConfigDict(extra="forbid")

    state: str = Field(description="State abbreviation, e.g. 'AZ', 'WI'")
    edfi_version: str = Field(description="Ed-Fi Data Standard version, e.g. '3.x', '5.x'")
    domain: str = Field(
        description=(
            "Source Area: state-specific reporting / grouping vocabulary "
            "(XLSX sheet / MN collection / IN API resource / TX TEDS entity / "
            "WI Confluence page). NOT the Ed-Fi domain — see `edfi_domain`. "
            "Empty string ('') when no source-document area applies (swagger "
            "backfill rows; spine-lens positions the source doc never "
            "mentioned)."
        )
    )
    edfi_domain: str | None = Field(
        default=None,
        description=(
            "Cross-state Ed-Fi domain, spine-derived from the entity via the "
            "canonical resolver (`ingest.shared.edfi_domain_for_entity`); "
            "multi-domain entities are joined with '; '. Distinct from "
            "`domain` (Source Area). None only where the entity resolves to "
            "no spine domain."
        ),
    )
    entity: str = Field(description="Normalized Ed-Fi entity name in PascalCase, e.g. 'StudentSchoolAttendanceEvent'")
    raw_entity: str | None = Field(
        default=None,
        description="Original entity name before normalization",
    )
    element_name: str = Field(description="Element name, e.g. 'actualDaysAttendance'")
    data_type: str | None = Field(default=None, description="Data type: decimal, string, etc.")
    definition_text: str = Field(description="State's full definition of this element")
    source: Literal["core", "extension", "unknown", "filtered"] = Field(
        default="unknown",
        description=(
            "Per-record extension attribution. 'core' = matches a core Ed-Fi "
            "property; 'extension' = matches a state-extension property; "
            "'unknown' = does not match any spine slot; 'filtered' = "
            "spine-lens placeholder row for a domain SIS vendors never "
            "populate (Assessment/Survey/LearningStandard/Gradebook/"
            "Intervention). Set by ingest adapters; consumed by the analyst "
            "report's 'Is an extension' and 'Match Status' columns."
        ),
    )
    extension_name: str | None = Field(
        default=None,
        description=(
            "Identifier of the extension this element came from when "
            "source='extension' — for AZ this is the raw entity name "
            "(e.g. 'az.CalendarExtension'); for WI/MN it's the spine "
            "extension's source_prefix (e.g. 'wi', 'mn')."
        ),
    )
    business_rules_text: str | None = Field(
        default=None,
        description="Entity-level shared business rules (reporting requirements, etc.)",
    )
    element_specific_rules: str | None = Field(
        default=None,
        description="Per-element rules (special instructions, regulatory citations).",
    )
    regulatory_citations: list[str] = Field(
        default_factory=list,
        description="Regulatory citations extracted from element text (e.g., '34 CFR 300.601', '20 U.S.C. 1416')",
    )
    related_entities: list[str] = Field(
        default_factory=list,
        description="Structured cross-entity references from the authoritative source",
    )
    descriptor_table_code: str | None = Field(
        default=None,
        description="Descriptor table identifier (e.g., 'C325' for AcademicSubject)",
    )
    descriptor_table_values: list[dict[str, str]] = Field(
        default_factory=list,
        description="Descriptor code values with labels (subset when cardinality is high)",
    )
    collections_text: str | None = Field(
        default=None,
        description="Raw collections-list text from the authoritative source (submission-scope signal).",
    )
    edfi_standard_definition: str | None = Field(
        default=None,
        description="Ed-Fi UDM definition for comparison (populated if core element)",
    )
    source_document: str | None = Field(default=None, description="Source filename")
    source_page_or_section: str | None = Field(default=None, description="Source location")
    documented: bool = Field(
        description=(
            "True when the state's source document covers this (entity, "
            "element) pair. Source-driven artifacts are True by construction. "
            "Spine-driven artifacts set it False for spine positions the "
            "source doc did not mention — the primary analyst signal under "
            "the spine lens. Required (no default) so every caller makes the "
            "choice explicit; the default would silently mask future code "
            "paths that construct records via a lens where `True` is wrong."
        ),
    )
    documentation_source: Literal["source_doc", "swagger", "swagger_leaf"] = Field(
        default="source_doc",
        description=(
            "Where the row originates. 'source_doc' = state's primary "
            "authored prose (Confluence / Matrix / XLSX / TWEDS); 'swagger' "
            "= swagger-backfilled row for an entity the source doc is "
            "silent on (issue #70 — swagger publication counts as state "
            "documentation when the primary source is silent on a whole "
            "entity); 'swagger_leaf' = leaf-level cross-lens borrow row, "
            "appended when the source doc covers the parent entity but "
            "doesn't enumerate this sub-collection / sub-entity leaf "
            "(issue #147 — methodology approved 2026-05-03 by Doug + Maria "
            "for keymap-join match-count visibility, ADR 0004). Defaults "
            "to 'source_doc' so every existing adapter call site stays "
            "valid; only the swagger-backfill module sets 'swagger' or "
            "'swagger_leaf'."
        ),
    )


class StateElements(BaseModel):
    """All elements extracted from a single state's specifications."""

    model_config = ConfigDict(extra="forbid")

    state: str
    edfi_version: str
    extracted_at: datetime
    element_count: int
    elements: list[ElementRecord]
