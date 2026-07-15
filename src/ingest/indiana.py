"""Parse Indiana IDOE Vendor Documentation Excel into pipeline data structures.

Source: `data/bootstrap/in/idoe_vendor_documentation_2024_v6.1.xlsx`, primary
sheet `API Datastructure`. The sheet is a flat row-per-(table, column) catalog
with table-name forward-fill (top-most filled `Table` cell carries down through
blank rows within the same sub-table). Schema-prefixed entity names: `edfi.X`
for core, `idoe.X` for IDOE 1.0.0 extensions.

Layout (cols 0-7, after the title row + header row):
    0: API Type        — `/ed-fi/`, `/idoe/identities/`, etc. (forward-filled)
    1: API Resource    — `localEducationAgencies`, `students` (forward-filled)
    2: Table           — `edfi.EducationOrganization`, `idoe.SchoolExtension`
                          (forward-filled within sub-collection groups)
    3: Column          — element name (`EducationOrganizationId`)
    4: Enumerations    — descriptor reference if applicable (`AddressTypeDescriptor`)
    5: Data Type       — SQL types: `int`, `nvarchar(75)`, `date`
    6: Req/Opt         — `Required`, `Required *`, `Optional`
    7: Unique ID       — `Y` (PK indicator) or blank

Headline cohort: 487 element rows across 91 unique tables (74 `edfi.*` core
+ 17 `idoe.*` extension), 448 under `/ed-fi/` API + 39 under `/idoe/`.

Prose enrichment (issue #156, 2026-05-04):

The XLSX itself carries no `definition_text` / `business_rules_text` columns —
IDOE delegates per-element prose to its public Confluence Knowledge Hub at
``https://idoe.atlassian.net/wiki/spaces/IKHTV/``. The harvester in
``src.ingest.idoe_confluence`` fetches the prose-bearing pages, extracts
descriptor code-value definitions from ``: Descriptors`` / ``: Types`` pages,
and pulls domain-narrative prose from ``: General Reporting Info`` /
``Reporting Guide:`` pages. ``run()`` wires those lookups into per-row
`definition_text` (descriptor-typed columns) and `business_rules_text`
(domain narrative attached via the API-resource-to-domain keymap).

Swagger-description fallback (issue #162 follow-on, 2026-05-04):

Confluence covers descriptor enumerations and per-domain narrative but
NOT per-property prose for non-descriptor IDOE-extension fields. The IN
swagger DOES carry IDOE-authored descriptions on ``idoe_*`` schemas
(212 of 288 IDOE-extension properties). For documented ``source="extension"``
rows where the Confluence pass yielded empty ``definition_text``,
``_swagger_extension_description()`` falls back to the spine catalog's
extension-property description. Gated to ``source="extension"`` so the
edFi_* rejection-as-Ed-Fi-prose discipline (ADR 0006) stays intact.

CRITICAL: module-level `run()` is a PLAIN function. Do NOT decorate it with
`@click.command()` — that makes `cli.py`'s `run_in()` invocation call
`Click.Command.__call__`, re-parse `sys.argv`, and die. See
`tests/test_ingest_in.py::TestCliWiring` for the regression guard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import openpyxl

from src.ingest.normalize import normalize_data_type, normalize_entity
from src.models.element import ElementRecord
from src.models.spine import StateSpine
from src.utils.paths import (
    state_elements_path,
    state_gap_log_path,
    state_spine_path,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_IN_SPINE_PATH = state_spine_path("IN")
_IN_XLSX_NAME = "idoe_vendor_documentation_2024_v6.1.xlsx"
_IN_XLSX_BOOTSTRAP_PATH = _PROJECT_ROOT / "data" / "bootstrap" / "in" / _IN_XLSX_NAME
_IN_XLSX_PATH = _PROJECT_ROOT / "data" / "raw" / "in" / _IN_XLSX_NAME
_IN_ELEMENTS_OUT = state_elements_path("IN", "source")
_IN_ELEMENTS_SPINE_OUT = state_elements_path("IN", "spine")
_IN_GAP_OUT = state_gap_log_path("IN")

# Sheet that carries the row-per-element catalog. Other sheets in the workbook
# describe vendor-cert workflow scenarios (Student Demographics, Student
# Discipline, etc.) that aren't per-element — out of scope for source-lens row
# enumeration; see data/bootstrap/in/README.md.
_API_DATASTRUCTURE_SHEET = "API Datastructure"


@dataclass
class INElementRow:
    """One row from the API Datastructure sheet, after forward-fill."""

    sheet_row: int
    api_type: str | None
    api_resource: str | None
    table_raw: str  # `edfi.EducationOrganization` / `idoe.SchoolExtension`
    column: str
    enumeration: str | None
    data_type_raw: str | None
    req_opt: str | None
    is_unique: bool


def _s(val: object) -> str | None:
    if val is None:
        return None
    s = str(val).strip().replace("\xa0", "")
    return s if s else None


def parse_in_api_datastructure(workbook_path: Path) -> list[INElementRow]:
    """Parse the API Datastructure sheet into flat INElementRow records.

    Forward-fills API Type / API Resource / Table across rows where the cell
    is blank. Skips the title row (row 1) and header row (row 2); element rows
    start at row 3.
    """
    wb = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    if _API_DATASTRUCTURE_SHEET not in wb.sheetnames:
        wb.close()
        raise ValueError(
            f"Expected sheet {_API_DATASTRUCTURE_SHEET!r} not found in {workbook_path.name}; "
            f"sheets present: {wb.sheetnames}"
        )

    ws = wb[_API_DATASTRUCTURE_SHEET]
    rows: list[INElementRow] = []

    last_api_type: str | None = None
    last_resource: str | None = None
    last_table: str | None = None

    for row_idx, raw in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
        if not raw or all(c is None for c in raw):
            continue
        # pad to at least 8 cols
        cells = list(raw) + [None] * (8 - len(raw)) if len(raw) < 8 else list(raw)

        api_type = _s(cells[0])
        resource = _s(cells[1])
        table = _s(cells[2])
        column = _s(cells[3])
        enumeration = _s(cells[4])
        data_type_raw = _s(cells[5])
        req_opt = _s(cells[6])
        unique_flag = _s(cells[7])

        # Forward-fill context cells
        if api_type:
            last_api_type = api_type
        if resource:
            last_resource = resource
        if table:
            last_table = table

        if not column:
            # Spacer row, sub-collection separator, or pure context-rotation row
            continue
        if not last_table:
            logger.warning("IN row %d has Column %r but no preceding Table — skipping", row_idx, column)
            continue

        rows.append(INElementRow(
            sheet_row=row_idx,
            api_type=last_api_type,
            api_resource=last_resource,
            table_raw=last_table,
            column=column,
            enumeration=enumeration,
            data_type_raw=data_type_raw,
            req_opt=req_opt,
            is_unique=(unique_flag is not None and unique_flag.upper().startswith("Y")),
        ))

    wb.close()
    return rows


def _table_is_extension(table_raw: str) -> bool:
    """`idoe.X` is an IDOE 1.0.0 extension; `edfi.X` is core."""
    return table_raw.lower().startswith("idoe.")


def _canonical_extension_name(table_raw: str) -> str:
    """Project `idoe.SchoolExtension` (XLSX form) → `idoe_schoolExtension` (spine form).

    The IN spine catalog stores extension keys in camelCase with underscore
    namespace (e.g., `idoe_schoolExtension`, `idoe_assessmentAccommodation`).
    The XLSX uses dot + PascalCase (`idoe.SchoolExtension`). For the
    `extension_name` audit field we keep a spine-aligned projection so analyst
    workbooks read consistently with the spine-lens artifact.
    """
    if not table_raw.lower().startswith("idoe."):
        return table_raw
    body = table_raw[len("idoe."):]
    if not body:
        return table_raw
    return f"idoe_{body[0].lower() + body[1:]}"


def _swagger_extension_description(
    catalog,
    extension_name: str | None,
    element_name: str,
) -> str:
    """Look up an IDOE-extension property's swagger description in the spine catalog.

    Issue #162 follow-on. The IDOE Confluence Knowledge Hub carries
    descriptor-code-value enumerations and per-domain narrative, but does
    NOT carry per-property prose for non-descriptor IDOE-extension fields
    (e.g. ``School.ChoiceIndicator``, ``Calendar.InstructionalTimeInMinutesIndicator``).
    For those rows the IN swagger DOES carry IDOE-authored descriptions —
    e.g. ``idoe_schoolExtension.choiceIndicator`` says *"Indicator of
    whether or not the school is a Choice School"*. Pull those into
    ``definition_text`` as a last-resort fallback after the Confluence
    lookup so the LLM extraction pool has more signal on these rows.

    GATED to ``source="extension"`` at the call-site: for ``edFi_*`` core
    rows the swagger description IS the upstream Ed-Fi prose the user
    already rejected as ``definition_text``, so widening to core rows
    would silently undo that rejection. ADR 0006 framed this swagger-
    description path as the "Option B Phase F fallback"; this lands
    Option B for IDOE-extension rows only.

    Resolution rules (case-insensitive, all on the spine catalog —
    same data source the dual-lens artifacts already trust):
    1. Locate the spine extension record by ``extension_name``
       (e.g. ``idoe_schoolExtension``).
    2. Try the element name verbatim, then first-letter-lowered, then
       (if the name ends in ``Id``) the bare-stem variants — bridging the
       XLSX's PascalCase + DescriptorId convention to the swagger's
       camelCase + Descriptor form.
    3. Search the extension's direct properties first, then its
       sub-collection properties (so sub-entity leaves like
       ``contract.contractDays`` resolve too).

    Returns the description string when one is found, empty string
    otherwise. Empty result is the honest "swagger doesn't carry prose
    for this row either" signal — typically FK key columns whose
    description lives on the FK target entity, not the extension.
    """
    if catalog is None or not extension_name or not element_name:
        return ""
    ext = catalog.extensions.get(extension_name)
    if ext is None:
        return ""

    candidates: set[str] = {element_name.lower()}
    candidates.add((element_name[0].lower() + element_name[1:]).lower())
    if element_name.endswith("Id") and len(element_name) > 2:
        stem = element_name[:-2]
        candidates.add(stem.lower())
        candidates.add((stem[0].lower() + stem[1:]).lower())

    def _description(prop) -> str:
        desc = getattr(prop, "description", None)
        return (desc or "").strip()

    for prop_name, prop in ext.properties.items():
        if prop_name.lower() in candidates:
            return _description(prop)
    for sub in ext.sub_collections.values():
        sub_props = getattr(sub, "properties", {}) or {}
        for prop_name, prop in sub_props.items():
            if prop_name.lower() in candidates:
                return _description(prop)
    return ""


def build_element_records(
    rows: list[INElementRow],
    *,
    edfi_version: str,
    source_document: str,
    catalog,
    confluence_digest: dict | None = None,
) -> list[ElementRecord]:
    """Convert parsed IN rows to ElementRecord instances.

    When ``confluence_digest`` is supplied (the harvested IDOE Knowledge Hub
    output from ``src.ingest.idoe_confluence.harvest``), descriptor-typed
    rows pick up ``definition_text`` from the per-code-value Confluence prose
    and every row picks up ``business_rules_text`` from the matching domain
    narrative (via the ``api_resource`` → Confluence-domain keymap). When
    omitted, both fields fall back to empty strings — matches pre-issue-156
    behaviour for tests that don't run the harvester.
    """
    from src.ingest.idoe_confluence import (
        descriptor_definition,
        domain_business_rules,
    )

    records: list[ElementRecord] = []
    for r in rows:
        is_ext = _table_is_extension(r.table_raw)
        normalized_entity = normalize_entity(r.table_raw, catalog)
        element_name = r.column
        # The XLSX often appends `Id` to a column name that's a descriptor FK;
        # the matching `Enumerations` cell carries the bare descriptor name.
        # Spine matching already handles `*DescriptorId` → `*Descriptor` aliasing,
        # so we leave the column name verbatim.

        if confluence_digest is not None:
            definition_text = descriptor_definition(
                confluence_digest, r.enumeration
            )
            business_rules_text = domain_business_rules(
                confluence_digest, r.api_resource
            )
        else:
            definition_text = ""
            business_rules_text = None

        # Issue #162 follow-on — for IDOE-extension rows where Confluence
        # didn't carry per-element prose, fall back to the IN swagger
        # description (which IS IDOE-authored on `idoe_*` schemas, unlike
        # `edFi_*` core rows where the description is upstream Ed-Fi prose
        # the user rejected as `definition_text`). Gated to
        # ``source="extension"`` so the rejection-as-Ed-Fi-prose holds.
        if is_ext and not definition_text:
            definition_text = _swagger_extension_description(
                catalog,
                _canonical_extension_name(r.table_raw),
                element_name,
            )

        records.append(ElementRecord(
            state="IN",
            edfi_version=edfi_version,
            domain=r.api_resource or r.table_raw,
            entity=normalized_entity,
            raw_entity=r.table_raw,
            element_name=element_name,
            data_type=normalize_data_type(r.data_type_raw),
            definition_text=definition_text,
            business_rules_text=business_rules_text,
            source="extension" if is_ext else "core",
            extension_name=_canonical_extension_name(r.table_raw) if is_ext else None,
            source_document=source_document,
            source_page_or_section=f"{_API_DATASTRUCTURE_SHEET}#row{r.sheet_row}",
            documented=True,
        ))
    return records


def run() -> None:
    """POC-3 IN ingestion: parse IDOE Vendor Documentation, match to spine.

    Writes:
      - `data/out/in_elements_source.json` (source-lens StateElements)
      - `data/out/in_elements_spine.json` (spine-lens dual artifact)
      - `data/out/in_gap_log.json` (spine-match diagnostics)
    """
    from src.ingest.shared import (
        assemble_source_driven,
        write_dual_lens_artifacts,
    )

    if not _IN_SPINE_PATH.exists():
        raise FileNotFoundError(
            f"No IN spine at {_IN_SPINE_PATH}. "
            f"Run `mc spine fetch --state IN --school-year 2027` + "
            f"`mc spine build --state IN` first."
        )
    spine = StateSpine.model_validate_json(_IN_SPINE_PATH.read_text(encoding="utf-8"))
    logger.info(
        "Loaded IN spine: %d core entities, %d extensions (Ed-Fi %s)",
        spine.entity_count, spine.extension_count, spine.edfi_version,
    )

    if _IN_XLSX_PATH.exists():
        xlsx_path = _IN_XLSX_PATH
    elif _IN_XLSX_BOOTSTRAP_PATH.exists():
        xlsx_path = _IN_XLSX_BOOTSTRAP_PATH
        logger.info(
            "Using bundled IN workbook at %s (no copy in data/raw/in/)",
            xlsx_path,
        )
    else:
        raise FileNotFoundError(
            f"IN Vendor Documentation XLSX not found at {_IN_XLSX_PATH} or "
            f"{_IN_XLSX_BOOTSTRAP_PATH}"
        )

    rows = parse_in_api_datastructure(xlsx_path)
    logger.info(
        "Parsed %s::%s: %d element rows across %d unique tables (%d core, %d idoe)",
        xlsx_path.name, _API_DATASTRUCTURE_SHEET, len(rows),
        len({r.table_raw for r in rows}),
        len({r.table_raw for r in rows if not _table_is_extension(r.table_raw)}),
        len({r.table_raw for r in rows if _table_is_extension(r.table_raw)}),
    )

    # Issue #156 — harvest IDOE Confluence Knowledge Hub for prose.
    from src.ingest.idoe_confluence import ensure_harvested
    confluence_digest = ensure_harvested()
    logger.info(
        "Loaded IDOE Confluence digest: %d descriptors, %d domain narratives",
        len(confluence_digest.get("descriptors") or {}),
        len(confluence_digest.get("domain_narratives") or {}),
    )

    records = build_element_records(
        rows,
        edfi_version=spine.edfi_version,
        source_document=xlsx_path.name,
        catalog=spine.catalog,
        confluence_digest=confluence_digest,
    )
    n_with_def = sum(1 for r in records if r.definition_text)
    n_with_rules = sum(1 for r in records if r.business_rules_text)
    logger.info(
        "Built %d IN ElementRecords (def_text: %d/%d = %.1f%%, "
        "business_rules_text: %d/%d = %.1f%%)",
        len(records),
        n_with_def, len(records), 100 * n_with_def / max(1, len(records)),
        n_with_rules, len(records), 100 * n_with_rules / max(1, len(records)),
    )

    records, recovered, assembly = assemble_source_driven(records, spine)

    # Shared adapter tail: dual-lens writes → gap log → swagger backfill
    # (issue #213 item 3 — the ~80-line sequence lives once in shared.py).
    write_dual_lens_artifacts(
        state="IN",
        spine=spine,
        records=records,
        assembly=assembly,
        recovered=recovered,
        source_document=xlsx_path.name,
        source_out=_IN_ELEMENTS_OUT,
        spine_out=_IN_ELEMENTS_SPINE_OUT,
        gap_out=_IN_GAP_OUT,
        spine_path=_IN_SPINE_PATH,
        spine_source_rel=str(_IN_SPINE_PATH.relative_to(_PROJECT_ROOT)),
        source_coverage_note=(
            "Of our IN API Datastructure rows, how many match a spine element "
            "(case-insensitive, FK+descriptor aliases)."
        ),
        spine_coverage_note=(
            "Of the spine's authoritative element slots, how many are "
            "represented in IN docs."
        ),
        logger=logger,
    )


if __name__ == "__main__":
    run()
