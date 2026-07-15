"""Parse Arizona Use Case 12.0 Excel into pipeline data structures.

Reads domain sheets from the AzEDS Use Case Excel file and extracts all data
element definitions organized by Ed-Fi entity. Entities are namespaced as
edfi.* (core) or az.* (extension), giving us structural classification for free.

Produces:
- StateElements (Stage 1 output) from all ~642 elements

Known source-side limitations (Phase B.8 documentation, 2026-04-09 — see
docs/ingest-layer-audit.md §9.13):

- **`business_rules_text` is always None for AZ elements.** The Use Case Excel
  has no business rules / formula column — only Property Name, Data Type,
  Codes, and Description. `build_element_records` sets
  `business_rules_text=None`. This is a
  source-data limitation, not a parser bug. WI populates this field from
  its Confluence source; AZ has only definition text to work from.

- **Internal-whitespace entity names are preserved verbatim.** The Use Case
  source has at least one entity with an internal space —
  `StudentDropOut RecoveryProgramMonthlyUpdates`. `_clean_entity_name`
  intentionally does not collapse internal whitespace so the canonical AZ
  output stays faithful to the source.

- **One element has a truncated `definition_text`:**
  `SectionExternalProviderTeacher.BeginDate` carries
  `definition_text = "The first date the SectionExternalProviderTeacher"`,
  which is incomplete mid-phrase. Verified to be a source-side issue: the
  Description cell in the `Master Schedule` sheet literally contains that
  exact truncated string with no continuation. The parser is innocent.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl

from src.models.edfi_catalog import EdFiCatalog
from src.models.element import ElementRecord
from src.ingest.normalize import normalize_data_type, normalize_entity

logger = logging.getLogger(__name__)


# Sheets that don't contain Ed-Fi data element tables.
#
# Excluded by design:
#   - Code Values, NeedCategoryMapping, Tribal Affiliations: lookup/mapping
#     tables, not entity definitions.
#   - Change Log: revision history.
#   - Identity: AzEDS /identities API (vendor identity-resolution service).
#     Not part of the Ed-Fi data standard. Schools call these endpoints to
#     look up UniqueIDs; they don't populate them as part of submitting data.
#     The bare "Identities" entity header doesn't match _ENTITY_RE so the
#     parser already drops this sheet — this list documents that as
#     intentional rather than accidental. See docs/ingest-layer-audit.md §9.8.
#
# Previously excluded but reinstated by Phase B.2 (2026-04-09):
#   - IDEA Part C: contains 3 az.PartC* extension entity tables (PartCAZEIP,
#     PartCNotification, PartCTransition) with ~17 valid IDEA federal-
#     compliance fields. The skip was incorrect.
_SKIP_SHEETS = frozenset([
    "Code Values",
    "NeedCategoryMapping",
    "Change Log",
    "Tribal Affiliations",
    "Identity",
])

# Parse floors (issue #213 item 2). MN/IN raise loudly on source-format
# drift, but the hardcoded column scan in `detect_entity_tables` DROPS
# unrecognized tables silently — a renamed "Column Name" header or shifted
# entity columns would quietly shrink the corpus while every downstream
# stage reports success. `parse_az_excel` raises when the end-of-parse
# corpus falls below these floors. Today's actual counts (2026-07-09,
# data/bootstrap/az/Use_Case_12.0_20260227.xlsm): 107 entity tables /
# 663 raw element rows — each floor is cleared >2x. Tests monkeypatch
# these module constants for small synthetic workbooks.
_MIN_ENTITY_TABLES = 50  # today: 107 (2026-07-09)
_MIN_ELEMENT_ROWS = 300  # today: 663 (2026-07-09)

# Regex to match entity names like edfi.StudentSchoolAssociation or az.SectionExtension
_ENTITY_RE = re.compile(r"^(edfi|az)\.+\s*\w+", re.IGNORECASE)

# Regex to extract optionality suffix from element names: (R), (C), (O)
# Also handles variants: "( R)", "(O)*", "(O) *"
_OPTIONALITY_RE = re.compile(r"\s*\(\s*([RCO])\s*\)\s*\*?\s*$")


@dataclass
class AZElementRow:
    """A single data element extracted from the Use Case Excel."""

    raw_name: str
    clean_name: str
    optionality: str | None  # R, C, O, or None
    data_type: str
    codes_ref: str | None
    description: str


@dataclass
class AZEntityTable:
    """A group of elements belonging to one entity within a domain sheet."""

    sheet_name: str
    entity_name: str
    is_extension: bool
    requirement_level: str  # "Required", "Optional*", etc.
    elements: list[AZElementRow] = field(default_factory=list)


def _str_or_none(val: object) -> str | None:
    """Convert cell value to stripped string or None."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _clean_entity_name(raw: str) -> str:
    """Normalize entity name: trim outer whitespace, fix double dots.

    INTENTIONAL: internal whitespace is preserved verbatim. The AZ Use Case
    source has at least one entity name with an internal space —
    `az.StudentDropOut RecoveryProgramMonthlyUpdates`. We preserve it so the
    canonical AZ output stays faithful to the source. A future engineer who
    "fixes" this by collapsing internal whitespace will break round-trip
    fidelity to the source spreadsheet.
    """
    name = raw.strip()
    # Fix double dots: az..Foo -> az.Foo
    name = re.sub(r"\.{2,}", ".", name)
    # Internal whitespace deliberately preserved — see docstring above.
    return name


def _normalize_descriptor_type_in_place(records: list[ElementRecord]) -> None:
    """Overwrite `data_type` to `"Descriptor"` when the element name has a
    descriptor suffix.

    Analyst review flagged that the AZ source XLSX stores descriptor fields
    with the underlying serialization type (Integer for `*DescriptorId`, or
    occasionally Date — row 107 PartCTransition.DelayDescriptorId, where the
    source type is `Date` but the definition clearly describes a descriptor).
    For analyst review and scoring, `Descriptor` is the meaningful canonical
    form. Primitive types on non-descriptor fields are preserved verbatim.
    """
    for i, r in enumerate(records):
        low = r.element_name.lower()
        if low.endswith("descriptor") or low.endswith("descriptorid"):
            if r.data_type != "Descriptor":
                records[i] = r.model_copy(update={"data_type": "Descriptor"})


def _canonicalize_az_extension_name(
    raw: str, known_extension_keys: set[str] | None = None
) -> str:
    """Canonicalize the analyst-facing extension_name for AZ records.

    Applied separately from `_clean_entity_name` (which preserves verbatim
    source for `raw_entity` / `entity`). Fixes four source-side quirks flagged
    by analyst review:
        - `AZ.` (uppercase) normalized to `az.` (the dominant convention)
        - `Extention` typo corrected to `Extension`
        - Internal whitespace collapsed, e.g.
          `az.StudentDropOut RecoveryProgramMonthlyUpdates`
          -> `az.StudentDropOutRecoveryProgramMonthlyUpdates`
        - Trailing-`s` pluralization is stripped when the spine catalog has
          the singular form as a known extension schema. Round-2 reviewer B
          flagged `az.StudentDropOutRecoveryProgramMonthlyUpdates` as an
          XLSX plural that should render as the singular spine-extension
          form (`az.StudentDropOutRecoveryProgramMonthlyUpdate`).

    `known_extension_keys` is the set of canonical AZ extension catalog keys
    (from `spine.catalog.extensions`) — e.g. `{"az_studentDropOutRecoveryProgramMonthlyUpdate",
    "az_calendarExtension", ...}`. When provided and the singularized form's
    camelCase projection matches a key, we strip the trailing `s`. When None,
    we skip singularization entirely — preserves backward-compat for callers
    that don't have spine access.
    """
    name = raw.strip()
    # Lowercase namespace prefix.
    if name.startswith("AZ."):
        name = "az." + name[3:]
    # Fix trailing `Extention` typo; reuse the matching regex behavior used
    # in `normalize_entity` (normalize.py:_TYPO_EXTENTION).
    name = re.sub(r"Extention$", "Extension", name)
    # Collapse internal whitespace (NOT applied to raw_entity / entity —
    # those preserve verbatim per _clean_entity_name).
    name = re.sub(r"\s+", "", name)

    # Spine-validated trailing-`s` singularization. The display form
    # `az.XxxYyys` projects to spine key `az_xxxYyys` (lowercase first segment
    # letter); singular projects to `az_xxxYyy`. Only strip when the singular
    # form is a known extension schema — avoids mangling names that legitimately
    # end in `s` (none currently, but defensive).
    if (
        known_extension_keys
        and name.lower().startswith("az.")
        and name.endswith("s")
        and not name.endswith("ss")
        and not name.endswith("us")
        and not name.endswith("is")
        and len(name) > len("az.") + 3
    ):
        singular = name[:-1]
        # Project "az.FooBar" -> "az_fooBar" (preserve all characters past the
        # first one after the prefix).
        pascal_body = singular[len("az."):]
        if pascal_body:
            camel_body = pascal_body[0].lower() + pascal_body[1:]
            if f"az_{camel_body}" in known_extension_keys:
                name = singular
    return name


def _parse_element_name(raw: str) -> tuple[str, str | None]:
    """Extract clean name and optionality suffix from raw element name.

    Examples:
        "EntryDate (R)" -> ("EntryDate", "R")
        "ExitWithdrawDate (C)" -> ("ExitWithdrawDate", "C")
        "CalendarCode (R )" -> ("CalendarCode", "R")
        "StudentUniqueID" -> ("StudentUniqueID", None)
    """
    raw = raw.strip()
    m = _OPTIONALITY_RE.search(raw)
    if m:
        clean = raw[: m.start()].strip()
        return clean, m.group(1).strip()
    return raw, None


def _is_entity_cell(val: object) -> bool:
    """Check if a cell value looks like an entity name (edfi.* or az.*)."""
    if not val or not isinstance(val, str):
        return False
    return bool(_ENTITY_RE.match(val.strip()))


def _is_element_row(row: tuple, name_col: int, type_col: int) -> bool:
    """Check if a row contains an element definition (has name and data type)."""
    if name_col >= len(row) or type_col >= len(row):
        return False
    name = row[name_col]
    dtype = row[type_col]
    if not name or not isinstance(name, str):
        return False
    name_s = name.strip()
    # Skip header rows
    if name_s == "Column Name":
        return False
    # Must have a data type
    if not dtype or not isinstance(dtype, str):
        return False
    return True


def detect_entity_tables(rows: list[tuple], sheet_name: str) -> list[AZEntityTable]:
    """Detect all entity table boundaries in a sheet.

    Scans for entity names (edfi.*/az.*) in columns 5-7 and identifies
    the element rows that follow. Handles three structural variants:

    1. Standard: entity header row, then "Column Name" header row, then elements
    2. Same-row: entity + "Column Name" on same row, elements on next row
    3. No header: entity row, elements start directly (e.g., CalendarDateCalendarEvent)

    Also handles the special case where entity header has the first element
    on the same row (e.g., az.CourseTranscriptExtention).
    """
    tables: list[AZEntityTable] = []

    i = 0
    while i < len(rows):
        row = rows[i]
        entity_name = None
        entity_col = None
        req_level = ""

        # Look for entity name in columns 5-7 only (skip col 0-4 which are API sections)
        for col in (7, 6, 5):
            if col < len(row) and _is_entity_cell(row[col]):
                entity_name = _clean_entity_name(str(row[col]))
                entity_col = col
                # Check for requirement level in col 6
                if col == 7 and 6 < len(row) and row[6] and isinstance(row[6], str):
                    req_level = str(row[6]).strip()
                break

        if not entity_name:
            i += 1
            continue

        is_ext = entity_name.lower().startswith("az.")

        # Determine element column layout based on entity column position
        # When entity is in col 5 (Learning Modality variant): name=6, type=7, codes=8, desc=9
        # When entity is in col 6 or 7 (standard): name=8, type=9, codes=10, desc=11
        if entity_col is not None and entity_col <= 5:
            name_col, type_col, codes_col, desc_col = 6, 7, 8, 9
        else:
            name_col, type_col, codes_col, desc_col = 8, 9, 10, 11

        # Refine column offsets by finding where "Column Name" actually appears
        # Check if "Column Name" is on the same row
        same_row_cn = any(
            c == "Column Name" for c in row if c and isinstance(c, str)
        )
        # If same-row Column Name found, use its position to set offsets
        if same_row_cn:
            cn_pos = next(
                (k for k, c in enumerate(row) if c == "Column Name"), name_col
            )
            name_col = cn_pos
            type_col = cn_pos + 1
            codes_col = cn_pos + 2
            desc_col = cn_pos + 3

        # Check if "Column Name" is on the next row
        next_row_cn = (
            i + 1 < len(rows)
            and any(
                c == "Column Name"
                for c in rows[i + 1]
                if c and isinstance(c, str)
            )
        )
        # If next-row Column Name found, use its position
        if next_row_cn and not same_row_cn:
            cn_pos = next(
                (k for k, c in enumerate(rows[i + 1]) if c == "Column Name"),
                name_col,
            )
            name_col = cn_pos
            type_col = cn_pos + 1
            codes_col = cn_pos + 2
            desc_col = cn_pos + 3

        # Check if entity row also has element data (e.g., az.CourseTranscriptExtention)
        has_element_on_entity_row = (
            not same_row_cn
            and not next_row_cn
            and _is_element_row(row, name_col, type_col)
        )

        # Determine where elements start
        if has_element_on_entity_row:
            elem_start = i  # Elements start on this row
        elif same_row_cn:
            elem_start = i + 1  # Elements start on next row
        elif next_row_cn:
            elem_start = i + 2  # Skip header row
        else:
            # No Column Name found -- elements start on next row
            elem_start = i + 1

        # Extract elements
        table = AZEntityTable(
            sheet_name=sheet_name,
            entity_name=entity_name,
            is_extension=is_ext,
            requirement_level=req_level,
        )

        j = elem_start
        while j < len(rows):
            erow = rows[j]

            # Stop conditions: empty row, or next entity header
            if all(c is None for c in erow):
                break
            # Check if this row is a new entity header
            if any(
                col < len(erow) and _is_entity_cell(erow[col])
                for col in (5, 6, 7)
            ):
                # But not the current entity row itself
                if j != i:
                    break

            if _is_element_row(erow, name_col, type_col):
                raw_name = str(erow[name_col]).strip()
                clean_name, optionality = _parse_element_name(raw_name)
                data_type = str(erow[type_col]).strip()
                codes_ref = _str_or_none(erow[codes_col]) if codes_col < len(erow) else None
                description = _str_or_none(erow[desc_col]) if desc_col < len(erow) else None

                table.elements.append(AZElementRow(
                    raw_name=raw_name,
                    clean_name=clean_name,
                    optionality=optionality,
                    data_type=data_type,
                    codes_ref=codes_ref,
                    description=description or "",
                ))

            j += 1

        if table.elements:
            tables.append(table)

        # Move past the elements we just processed
        i = max(i + 1, j)

    return tables


def parse_az_excel(workbook_path: Path) -> list[AZEntityTable]:
    """Parse all entity tables from the AZ Use Case Excel file.

    Args:
        workbook_path: Path to the Use Case 12.0 .xlsm file.

    Returns:
        List of AZEntityTable with extracted elements.
    """
    wb = openpyxl.load_workbook(
        workbook_path, read_only=True, data_only=True, keep_links=False
    )

    all_tables: list[AZEntityTable] = []

    for sheet_name in wb.sheetnames:
        if sheet_name.strip() in _SKIP_SHEETS:
            continue

        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        tables = detect_entity_tables(rows, sheet_name.strip())
        all_tables.extend(tables)

    wb.close()

    # End-of-parse corpus floor (issue #213 item 2): the column scan drops
    # unrecognized tables silently, so a structural workbook change would
    # otherwise shrink the corpus without any failure signal.
    total_rows = sum(len(t.elements) for t in all_tables)
    if len(all_tables) < _MIN_ENTITY_TABLES or total_rows < _MIN_ELEMENT_ROWS:
        raise ValueError(
            f"AZ source format may have changed: parsed {len(all_tables)} entity "
            f"tables / {total_rows} element rows from {workbook_path.name}, below "
            f"the floor of {_MIN_ENTITY_TABLES} tables / {_MIN_ELEMENT_ROWS} rows "
            "(2026-07-09 actual: 107 tables / 663 rows). detect_entity_tables "
            "drops unrecognized tables silently — inspect the workbook layout "
            "(entity columns 5-7, 'Column Name' headers) before lowering "
            "_MIN_ENTITY_TABLES/_MIN_ELEMENT_ROWS."
        )
    return all_tables


# Known typos in AZ Use Case Excel -- corrected at ingestion
_AZ_ELEMENT_NAME_CORRECTIONS = {
    "StudenUniqueID": "StudentUniqueID",
    "StudentUniqeID": "StudentUniqueID",
    "ProgramTypeDescriptionId": "ProgramTypeDescriptorId",
    "staffUniqueId": "StaffUniqueID",
}


def build_element_records(
    tables: list[AZEntityTable],
    edfi_version: str = "5.2",
    source_document: str = "Use_Case_12.0_20260227.xlsm",
    catalog: EdFiCatalog | None = None,
) -> list[ElementRecord]:
    """Convert parsed AZ entity tables to ElementRecord instances."""
    records: list[ElementRecord] = []

    for table in tables:
        normalized = normalize_entity(table.entity_name, catalog)
        for elem in table.elements:
            element_name = _AZ_ELEMENT_NAME_CORRECTIONS.get(
                elem.clean_name, elem.clean_name
            )
            records.append(ElementRecord(
                state="AZ",
                edfi_version=edfi_version,
                domain=table.sheet_name,
                entity=normalized,
                raw_entity=table.entity_name,
                element_name=element_name,
                data_type=normalize_data_type(elem.data_type),
                definition_text=elem.description,
                # AZ source has no business rules / formula column — see
                # module docstring "Known source-side limitations". Always None.
                business_rules_text=None,
                source="extension" if table.is_extension else "core",
                extension_name=(
                    _canonicalize_az_extension_name(table.entity_name)
                    if table.is_extension else None
                ),
                source_document=source_document,
                source_page_or_section=table.sheet_name,
                documented=True,
            ))

    # NOTE: Dedup intentionally NOT called here — it used to run pre-unflatten,
    # but that missed the 53 (entity, element_name) collisions introduced by
    # the unflatten loop in `run()` (flagged by analyst review). The final
    # dedup pass runs in `run()` AFTER unflatten. See utils/dedup.py.
    return records


# POC-3: the authoritative catalog is the live-fetched Swagger spine at
# data/spine/az_spine.json. Entity normalization during ingest uses
# spine.catalog so AZ names align with core Ed-Fi forms.
from src.utils.paths import (
    state_elements_path,
    state_gap_log_path,
    state_spine_path,
)

_MC_ROOT = Path(__file__).resolve().parents[2]
_AZ_SPINE_PATH = state_spine_path("AZ")
_AZ_XLSX_PATH = _MC_ROOT / "data" / "raw" / "az" / "Use_Case_12.0_20260227.xlsm"
# Fresh-clone bootstrap: the workbook lives under `data/bootstrap/az/` so
# `mc ingest az` works out of the box. A copy in `data/raw/az/` still
# wins when present — devs re-downloading from ADE don't have to touch
# `data/bootstrap/`.
_AZ_XLSX_BOOTSTRAP_PATH = (
    _MC_ROOT / "data" / "bootstrap" / "az" / "Use_Case_12.0_20260227.xlsm"
)
_AZ_RULES_PATH = _MC_ROOT / "data" / "raw" / "az" / "az_integrity_rules.json"
# Fresh-clone bootstrap: the parsed integrity-rules JSON lives under
# `data/bootstrap/az/` so `mc ingest az` enriches business_rules_text
# out of the box. A copy in `data/raw/az/` still wins when present —
# devs who re-run `python -m src.ingest.az_integrity_rules` against an
# updated PDF set don't have to touch `data/bootstrap/`.
_AZ_RULES_BOOTSTRAP_PATH = (
    _MC_ROOT / "data" / "bootstrap" / "az" / "az_integrity_rules.json"
)
_AZ_ELEMENTS_OUT = state_elements_path("AZ", "source")
_AZ_ELEMENTS_SPINE_OUT = state_elements_path("AZ", "spine")
_AZ_GAP_OUT = state_gap_log_path("AZ")


def run() -> None:
    """POC-3 AZ ingestion: parse XLSX, enrich with integrity rules, match to spine.

    Writes `data/out/az_elements_source.json` (StateElements) and
    `data/out/az_gap_log.json` (spine-match diagnostics).
    """
    from src.ingest.az_enrich import enrich_records_with_rules
    from src.ingest.shared import (
        compute_coverage,
        demote_unmatched_to_unknown,
        populate_data_types_from_spine,
        populate_edfi_standard_definition_from_spine,
        run_unflatten_pass,
        stamp_edfi_domains,
        write_dual_lens_artifacts,
    )
    from src.models.spine import StateSpine
    from src.utils.dedup import dedup_records

    if not _AZ_SPINE_PATH.exists():
        raise FileNotFoundError(
            f"No AZ spine at {_AZ_SPINE_PATH}. "
            f"Run `mc spine fetch --state AZ` + `mc spine build --state AZ` first."
        )
    spine = StateSpine.model_validate_json(_AZ_SPINE_PATH.read_text(encoding="utf-8"))
    logger.info(
        "Loaded AZ spine: %d core entities, %d extensions (Ed-Fi %s)",
        spine.entity_count, spine.extension_count, spine.edfi_version,
    )

    if _AZ_XLSX_PATH.exists():
        xlsx_path = _AZ_XLSX_PATH
    elif _AZ_XLSX_BOOTSTRAP_PATH.exists():
        xlsx_path = _AZ_XLSX_BOOTSTRAP_PATH
        logger.info(
            "Using bundled AZ workbook at %s (no copy in data/raw/az/)",
            xlsx_path,
        )
    else:
        raise FileNotFoundError(
            f"AZ Use Case XLSX not found at {_AZ_XLSX_PATH} or "
            f"{_AZ_XLSX_BOOTSTRAP_PATH}"
        )

    tables = parse_az_excel(xlsx_path)
    total_raw = sum(len(t.elements) for t in tables)
    logger.info(
        "Parsed %s: %d entity tables (%d core, %d extension), %d raw elements",
        xlsx_path.name,
        len(tables),
        sum(1 for t in tables if not t.is_extension),
        sum(1 for t in tables if t.is_extension),
        total_raw,
    )

    records = build_element_records(
        tables,
        edfi_version=spine.edfi_version,
        source_document=_AZ_XLSX_PATH.name,
        catalog=spine.catalog,
    )
    logger.info("Built %d ElementRecords (from %d raw rows)", len(records), total_raw)

    # Round 2.2: re-canonicalize extension_name against the spine's extension
    # catalog so XLSX plural forms display as the singular spine form.
    known_ext_keys = set(spine.catalog.extensions.keys())
    for i, r in enumerate(records):
        if r.extension_name:
            canonical = _canonicalize_az_extension_name(r.extension_name, known_ext_keys)
            if canonical != r.extension_name:
                records[i] = r.model_copy(update={"extension_name": canonical})

    _normalize_descriptor_type_in_place(records)

    rules_path = _AZ_RULES_PATH if _AZ_RULES_PATH.exists() else _AZ_RULES_BOOTSTRAP_PATH
    if rules_path.exists():
        records = enrich_records_with_rules(records, rules_path)
        enriched = sum(1 for r in records if r.business_rules_text)
        logger.info("Integrity-rules enrichment: %d/%d records carry business_rules_text (source=%s)", enriched, len(records), rules_path.name)
    else:
        logger.warning("No az_integrity_rules.json at %s or %s — skipping rules enrichment", _AZ_RULES_PATH, _AZ_RULES_BOOTSTRAP_PATH)

    # AZ-bespoke pipeline: unflatten → cross-attribution hint fix → dedup →
    # demote-to-unknown → spine-type contract. The shared orchestrator
    # `assemble_source_driven` does not fit AZ because AZ pre-sets `source`
    # from namespace at record-creation time (no `attribute_record_source`
    # call) and needs the cross-attribution fix between unflatten and dedup.
    recovered = run_unflatten_pass(records, spine)

    # Round 2.2: narrow fix for reviewer B's row 42 cross-attribution.
    # The AZ `StudentDropOutRecoveryProgramMonthlyUpdate` extension declares
    # its own properties but not the entity's inherited identity, so the
    # spine's parent-propagation loop walks them up to `Student` — wrong
    # semantic parent. Target ONLY rows that were rewritten from a
    # StudentDropOut...MonthlyUpdate(s) raw entity to `Student`.
    _AZ_CROSS_ATTRIBUTION_HINTS: tuple[str, ...] = (
        "StudentDropOutRecoveryProgramMonthlyUpdate",
    )
    for i, r in enumerate(records):
        if r.entity != "Student":
            continue
        raw_collapsed = (r.raw_entity or "").replace(" ", "").replace(".", "")
        for hint in _AZ_CROSS_ATTRIBUTION_HINTS:
            if hint.lower() in raw_collapsed.lower():
                records[i] = r.model_copy(update={
                    "entity": hint,
                    "source": "unknown",
                    "extension_name": None,
                })
                break

    pre_dedup = len(records)
    records = dedup_records(records)
    if len(records) < pre_dedup:
        logger.info(
            "Post-unflatten dedup: %d -> %d records (collapsed %d duplicate pairs)",
            pre_dedup, len(records), pre_dedup - len(records),
        )

    # AZ sets `source` at creation time from the XLSX namespace (`edfi.*` →
    # core, `az.*` → extension). Rows that don't actually match the spine
    # get demoted to `unknown` so Match Status reconciles with the gap log.
    demote_unmatched_to_unknown(records, spine)

    # Canonical-type contract: for matched records, `data_type` is spine-
    # derived. Runs AFTER demote_unmatched_to_unknown so unresolved rows
    # (source=="unknown") keep source-verbatim types as an audit trail.
    populate_data_types_from_spine(records, spine)
    populate_edfi_standard_definition_from_spine(records, spine)

    # Issue #184: stamp the Ed-Fi domain (distinct from `domain` = Source
    # Area) on every source-lens record. AZ orchestrates the helpers inline
    # rather than via assemble_source_driven, so the stamp is explicit here.
    stamp_edfi_domains(records, spine)

    assembly = compute_coverage(records, spine)

    # Shared adapter tail: dual-lens writes → gap log → swagger backfill
    # (issue #213 item 3 — the ~80-line sequence lives once in shared.py).
    write_dual_lens_artifacts(
        state="AZ",
        spine=spine,
        records=records,
        assembly=assembly,
        recovered=recovered,
        source_document=_AZ_XLSX_PATH.name,
        source_out=_AZ_ELEMENTS_OUT,
        spine_out=_AZ_ELEMENTS_SPINE_OUT,
        gap_out=_AZ_GAP_OUT,
        spine_path=_AZ_SPINE_PATH,
        spine_source_rel=str(_AZ_SPINE_PATH.relative_to(_MC_ROOT)),
        source_coverage_note=(
            "Of our AZ doc elements, how many match a spine element "
            "(case-insensitive, FK+descriptor aliases)."
        ),
        spine_coverage_note=(
            "Of the spine's authoritative element slots, how many are "
            "represented in AZ docs."
        ),
        logger=logger,
    )


if __name__ == "__main__":
    run()
