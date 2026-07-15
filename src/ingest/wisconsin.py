"""Scrape Wisconsin DPI Ed-Fi Confluence wiki into pipeline data structures.

Two-pass approach via Confluence REST API (public, no auth required):
  Pass 1 -- Crawl page tree: top index -> domain pages -> entity pages
  Pass 2 -- Fetch each entity page in storage format, parse HTML tables
           for element properties + business rules

Output: StateElements JSON with ElementRecords from WI Confluence as
the primary source for WI state specs.

Entry point: https://wisconsindpi.atlassian.net/wiki/spaces/widpiedfi/pages/2294032
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

from src.models.edfi_catalog import EdFiCatalog
from src.models.element import ElementRecord
from src.ingest.normalize import normalize_data_type, normalize_entity

logger = logging.getLogger(__name__)

CONFLUENCE_BASE = "https://wisconsindpi.atlassian.net"
API_V2 = f"{CONFLUENCE_BASE}/wiki/api/v2"
TOP_PAGE_ID = "2294032"

# Parse floors (issue #213 item 2). MN/IN raise loudly on source-format
# drift, but the WI Confluence parser SKIPS unrecognized tables/pages
# silently (`parse_data_properties_table` ignores tables whose headers
# don't say "property"/"element"; `build_element_records` skips pages that
# yield no elements) — a Confluence template change would quietly shrink
# the corpus while every downstream stage reports success. `run()` calls
# `enforce_parse_floor` on the `build_element_records` output. Today's
# actual counts (2026-07-09, tests/golden/wi_elements_source.json
# documented rows): 533 element records across 49 recognized entity pages
# — each floor is cleared >2x. Tests monkeypatch these module constants
# when exercising `run()` with small synthetic fixtures.
_MIN_PARSED_ROWS = 250  # today: 533 (2026-07-09)
_MIN_RECOGNIZED_TABLES = 20  # today: 49 pages yielding elements (2026-07-09)


def enforce_parse_floor(records: list["ElementRecord"]) -> None:
    """Raise when the parsed WI corpus falls below the format-drift floors.

    ``records`` is the ``build_element_records`` output; "recognized
    tables" counts the distinct source pages that yielded at least one
    element (each Confluence entity page contributes one data-properties
    table family).
    """
    rows = len(records)
    tables = len({r.source_page_or_section for r in records})
    if rows < _MIN_PARSED_ROWS or tables < _MIN_RECOGNIZED_TABLES:
        raise ValueError(
            f"WI source format may have changed: parsed {rows} element records "
            f"across {tables} recognized entity pages, below the floor of "
            f"{_MIN_PARSED_ROWS} rows / {_MIN_RECOGNIZED_TABLES} pages "
            "(2026-07-09 actual: 533 rows / 49 pages). The Confluence parser "
            "skips unrecognized tables silently — inspect the page structure "
            "(data-properties table headers) before lowering "
            "_MIN_PARSED_ROWS/_MIN_RECOGNIZED_TABLES."
        )

# Domain pages that are NOT entity endpoints (skip during element extraction)
_SKIP_DOMAIN_PAGES = {
    "SIS Choice Audit Requirements",
    "WISEdata Validations API",
}

# Entity name overrides for leaf domain pages where the page title doesn't
# map cleanly to an Ed-Fi entity name
_LEAF_DOMAIN_ENTITY_MAP = {
    "Migrant Education Program (Public LEA Only)": "StudentMigrantEducationProgramAssociation",
    "Pupil Transportation Report": "StudentTransportation",
    "Pupil Count Report for Membership (Public LEA Only)": "StudentSchoolAssociation",
    "Course Transcripts (Public LEA Only)": "CourseTranscript",
    "World Language Education (Public LEA Only)": "Section",
    "Career and Technical Education (Public LEAs Only)": "StudentCTEProgramAssociation",
    "Assessment (Public LEAs Only)": "StudentAssessment",
}


# ---------------------------------------------------------------------------
# Pass 1: Crawl page tree
# ---------------------------------------------------------------------------


def fetch_children(client: httpx.Client, page_id: str) -> list[dict]:
    """Fetch all child pages of a given page via Confluence v2 API."""
    children = []
    url = f"{API_V2}/pages/{page_id}/children"
    params = {"limit": 50}

    while url:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        children.extend(data.get("results", []))

        # Handle pagination
        next_link = data.get("_links", {}).get("next")
        if next_link:
            url = f"{CONFLUENCE_BASE}{next_link}" if next_link.startswith("/") else next_link
            params = {}  # params are in the next URL
        else:
            url = None

    return children


def crawl_page_tree(
    client: httpx.Client,
    cache_dir: Path,
    delay: float = 0.5,
) -> list[dict]:
    """Crawl the WI Confluence page tree and return entity page metadata.

    Some domain pages have child entity pages (e.g., Student & Enrollment ->
    /student, /schools, etc.). Others are leaf domains that contain element
    data directly on the domain page itself (e.g., Course Transcripts,
    World Language Education, CTE, Pupil Count, Migrant Ed, Transportation).

    Returns list of dicts: {page_id, title, domain}
    """
    cache_file = cache_dir / "page_tree.json"
    if cache_file.exists():
        tree = json.loads(cache_file.read_text(encoding="utf-8"))
        logger.info("Loaded page tree from cache: %d entity pages", len(tree))
        return tree

    logger.info("Crawling page tree from top page %s...", TOP_PAGE_ID)

    # Level 1: Domain pages
    domain_pages = fetch_children(client, TOP_PAGE_ID)
    logger.info("Found %d domain pages", len(domain_pages))
    time.sleep(delay)

    # Level 2: Entity pages under each domain
    entity_pages = []
    for dp in domain_pages:
        domain_title = dp["title"]
        domain_id = dp["id"]

        if domain_title in _SKIP_DOMAIN_PAGES:
            logger.info("  Skipping non-entity domain: %s", domain_title)
            continue

        children = fetch_children(client, domain_id)
        logger.info("  %s: %d child pages", domain_title, len(children))

        if children:
            # Domain has child entity pages
            for child in children:
                entity_pages.append({
                    "page_id": child["id"],
                    "title": child["title"],
                    "domain": domain_title,
                })
        else:
            # Leaf domain: element data is on the domain page itself
            logger.info("    -> treating as leaf entity page (data on domain page)")
            entity_pages.append({
                "page_id": domain_id,
                "title": domain_title,
                "domain": domain_title,
            })

        time.sleep(delay)

    # Cache the tree
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(entity_pages, indent=2), encoding="utf-8")
    logger.info("Cached page tree: %d entity pages", len(entity_pages))

    return entity_pages


# ---------------------------------------------------------------------------
# Pass 2: Fetch and parse entity pages
# ---------------------------------------------------------------------------


def fetch_entity_page(client: httpx.Client, page_id: str) -> dict:
    """Fetch a single entity page with storage-format body."""
    url = f"{API_V2}/pages/{page_id}"
    params = {"body-format": "storage"}
    resp = client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def parse_data_properties_table(soup: BeautifulSoup) -> list[dict]:
    """Extract data elements from the data properties table(s).

    WI Confluence uses tables with columns:
    #, Property Name, Data Type, Public (req'd), Choice (req'd), Definition

    Some pages have 6 columns, some may vary. We detect column headers
    dynamically and extract accordingly.
    """
    elements = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        # Detect header row
        header_row = rows[0]
        headers = []
        for cell in header_row.find_all(["th", "td"]):
            text = cell.get_text(strip=True).lower()
            headers.append(text)

        # Must have at least "property name" or "#" + "property" to be a data table
        header_text = " ".join(headers)
        if "property" not in header_text and "element" not in header_text:
            continue

        # Map column indices
        col_map = _map_columns(headers)
        if col_map.get("name") is None:
            continue

        header_count = len(headers)
        has_resource_col = col_map.get("resource") is not None
        # Identify the trailing "Data Element Page" / link column if present.
        # Confluence sometimes omits trailing empty cells too, so we may need
        # to insert placeholders at *both* the resource and the page-link
        # positions for off-by-2 rows (Phase B.3 follow-up, 2026-04-09).
        page_link_idx: int | None = None
        for hdr_idx, hdr_text in enumerate(headers):
            if "page" in hdr_text or "link" in hdr_text:
                page_link_idx = hdr_idx
                break
        last_resource = None  # Track last seen resource for carry-forward

        # Parse data rows
        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue

            # Handle missing cells: Confluence omits empty <td> tags entirely,
            # so rows without a resource value (and/or trailing page-link
            # value) have fewer cells than the header. Compensate by
            # inserting placeholders at the omitted positions.
            #
            # Two patterns observed in WI Confluence wikis:
            #   - len(cells) == header_count - 1: only the Resource/Endpoint
            #     cell is missing. Original Phase 0 handler.
            #   - len(cells) == header_count - 2: BOTH the Resource/Endpoint
            #     cell AND the trailing Data Element Page link cell are
            #     missing. Phase B.3 handler — without this, rows in
            #     /World Language Education and similar leaf-domain pages
            #     get column-shifted into garbage like
            #     `entity=ProgramTypeDescriptor element_name='string'`.
            placeholder_template = BeautifulSoup("<td></td>", "html.parser").td
            missing = header_count - len(cells)
            if missing == 1 and has_resource_col:
                cells.insert(col_map["resource"], placeholder_template)
            elif missing == 2 and has_resource_col and page_link_idx is not None:
                # Insert from the right first so the resource_idx stays valid.
                cells.insert(page_link_idx, BeautifulSoup("<td></td>", "html.parser").td)
                cells.insert(col_map["resource"], BeautifulSoup("<td></td>", "html.parser").td)

            row_elements = _parse_row(cells, col_map, page_link_idx=page_link_idx)
            for element in row_elements:
                # Carry forward the last seen resource name for rows with missing resource
                if element.get("resource"):
                    last_resource = element["resource"]
                elif last_resource and has_resource_col:
                    element["resource"] = last_resource
                elements.append(element)

    return elements


def _map_columns(headers: list[str]) -> dict[str, int | None]:
    """Map semantic column names to indices based on header text.

    Handles three WI Confluence table layouts:
    - Entity pages (7 cols): #, Property Name, Data Type, Public, Choice, Definition, Data Element Page
    - Leaf domain pages (8 cols): #, Resource Name, Property Name, Data Type, Public, Choice, Definition, Data Element Page
    - /calendars and /calendarDates page (7 cols): #, Endpoint, Property Name, Data Type, Public, Choice, Business Definition

    The Endpoint variant is treated as a "resource"-like column so the
    missing-cell carry-forward in parse_data_properties_table fires when
    Confluence omits empty Endpoint cells (Phase B.3 fix, 2026-04-09 —
    previously caused 10 garbage Calendar.<datatype> rows from column-shift).
    """
    col_map: dict[str, int | None] = {
        "number": None,
        "resource": None,  # "Resource Name" or "Endpoint" column (leaf domain / split-resource pages)
        "name": None,
        "type": None,
        "public_req": None,
        "choice_req": None,
        "definition": None,
    }

    for i, h in enumerate(headers):
        if h in ("#", "number", "#.") or h == "":
            if col_map["number"] is None:
                col_map["number"] = i
        elif "resource" in h or h == "endpoint":
            col_map["resource"] = i
        elif "property" in h:
            col_map["name"] = i
        elif h == "data type" or (h == "type" and col_map["type"] is None):
            col_map["type"] = i
        elif "public" in h:
            col_map["public_req"] = i
        elif "choice" in h:
            col_map["choice_req"] = i
        elif "definition" in h:
            col_map["definition"] = i

    # If no explicit definition column, use last content column (before "page" columns)
    if col_map["definition"] is None and len(headers) > 2:
        for i in range(len(headers) - 1, -1, -1):
            if "page" not in headers[i] and "link" not in headers[i]:
                col_map["definition"] = i
                break

    return col_map


_PROP_IDENT_RE = re.compile(r"^[a-z][a-zA-Z0-9_]*\*?$")

# Analyst review flagged rows whose cleaned property name still carries the
# extension schema prefix — e.g. the staff employment association Confluence
# entry produced
# `wi_staffEducationOrganizationEmploymentAssociationExtension.localPersonIdentificationCode`
# as a single property name. The extension identity belongs in
# `extension_name` (set downstream by `attribute_record_source` against the
# spine's extension-key map), not embedded in `element_name`.
_WI_EXTENSION_PREFIX_RE = re.compile(
    r"^wi_[A-Za-z0-9_]*?[Ee]xtension\.", flags=0
)

# Container holders like `WI_studentSchoolAssociationExtensions` (note the
# plural) are the JSON property that WRAPS the extension collection on an
# entity — they aren't data elements themselves. Analyst review flagged one
# such row leaking into Details. Spine grep confirms no legitimate Ed-Fi
# property ends in `Extensions` (plural).
_WI_EXTENSION_CONTAINER_RE = re.compile(
    r"^(?:wi_|WI_)[A-Za-z0-9_]*Extensions$"
)


def _strip_wi_extension_prefix(name: str) -> str:
    """Strip a leading `wi_<schema>Extension.` prefix from a property name.

    Flagged by analyst feedback (reviewer 2 #8). Safe because Ed-Fi property
    names are camelCase identifiers with no `wi_` prefix; stripping here
    leaves `extension_name` attribution to the spine-based matcher.
    """
    return _WI_EXTENSION_PREFIX_RE.sub("", name)


def _is_wi_extension_container(name: str) -> bool:
    """True if the name is an extension-container JSON field, not a property."""
    return bool(_WI_EXTENSION_CONTAINER_RE.match(name))


# Analyst feedback (reviewer 1 §3): `RccName` and `RccCommunityProviderReferencecommunityProviderId`
# are documented in the WI DPI Confluence wiki for Residential Care Center
# placements but don't resolve against the deployed Ed-Fi sandbox spine.
# `RccName` is a source truncation of the canonical spine property
# `rccNameOfInstitution` (confirmed against data/spine/wi_spine.json). The
# community-provider row is a Confluence concatenation glitch with no spine
# target — left as-is so `Match Status` reports it as `Unresolved`.
_WI_ELEMENT_NAME_CORRECTIONS: dict[str, str] = {
    "RccName": "rccNameOfInstitution",
}


def _apply_wi_element_corrections(name: str) -> str:
    """Rewrite known source-side truncations / typos to the canonical spine form."""
    return _WI_ELEMENT_NAME_CORRECTIONS.get(name, name)


def _normalize_descriptor_type_in_place(records: list[ElementRecord]) -> None:
    """Overwrite `data_type` to `"Descriptor"` when the element name has a
    descriptor suffix (case-insensitive).

    Parallels `arizona.py::_normalize_descriptor_type_in_place`. The WI
    Confluence source types descriptor fields as `string` (the underlying
    serialization), but analysts reason about them as descriptors. Round-2
    reviewer B flagged 129 matched-descriptor rows still typed as `String`
    in the workbook. Primitive types on non-descriptor fields are preserved
    verbatim; the spine-type override (`populate_data_types_from_spine`) then
    refines further.
    """
    for i, r in enumerate(records):
        low = r.element_name.lower()
        if low.endswith("descriptor") or low.endswith("descriptorid"):
            if r.data_type != "Descriptor":
                records[i] = r.model_copy(update={"data_type": "Descriptor"})


def _extract_property_names(cell: Tag) -> list[str]:
    """Extract property names from a Confluence property-name <td> cell.

    Phase B.5 follow-up (2026-04-09): some WI Confluence cells render the
    property name with a sub-collection structure or with appended
    annotation text. Two patterns:

    1. Sub-collection (parent + 1+ child key fields):
       <td>
         <p>programParticipationStatuses:</p>     ← parent ends with `:`
         <p>  participationStatusDescriptor</p>   ← child key field
         <p>  statusBeginDate</p>
         <p>  designatedBy</p>
         <p>  statusEndDate</p>
       </td>
       This represents 4 leaf fields under a sub-collection. The right
       canonical form is 4 dotted rows: `parent.child1`, `parent.child2`, …
       Same pattern with a single child:
       <td>
         <p>educationOrganizationReference:</p>
         <p>educationOrganizationId</p>
       </td>
       → 1 dotted row: `educationOrganizationReference.educationOrganizationId`

    2. Annotation footnote (parent + free-form sentence):
       <td>
         <p>residentLocalEducationAgencyReference. localEducationAgencyId</p>
         <p>🚩 This data property moved under the studentSchoolAssociation
            endpoint starting on the 2023-24 SY</p>
       </td>
       → 1 row: `residentLocalEducationAgencyReference. localEducationAgencyId`
       (the annotation is dropped). The first <p> already has the dotted
       form; the second <p> is documentation that should not be glued onto
       the property name. The follow-up <p>s are NOT camelCase identifiers
       so the sub-collection rule doesn't apply.

    Pre-fix, both cases collapsed via `cell.get_text(strip=True)` into one
    garbage string with no separator, producing element names like
    `localEducationAgencyIdThis data property moved...` and
    `programParticipationStatuses:participationStatusDescriptorstatusBegin
    Datedesignated...`. The Phase B.5 paren-strip regex missed both
    because there are no parens involved.

    Algorithm:
    - Get all direct-child <p> texts of the cell.
    - If there's only one (or zero), return its text — legacy behavior.
    - If the first <p> ends with `:` AND every following <p> is a
      camelCase identifier (the sub-collection signature), return one
      `parent.child` per child.
    - Otherwise, return only the first <p>'s text (the annotation case).
      Strip a trailing `:` if present.

    The returned list always has at least one entry; an empty list means
    the cell has no extractable name and the row should be dropped.
    """
    if cell is None:
        return []

    paragraphs = cell.find_all("p", recursive=False)
    if not paragraphs:
        # No <p> children — fall back to full text.
        text = cell.get_text(strip=True).rstrip(":").rstrip()
        return [text] if text else []

    p_texts = [p.get_text(strip=True) for p in paragraphs]
    p_texts = [t for t in p_texts if t]
    if not p_texts:
        text = cell.get_text(strip=True).rstrip(":").rstrip()
        return [text] if text else []

    if len(p_texts) == 1:
        return [p_texts[0].rstrip(":").rstrip()]

    parent = p_texts[0]
    children = p_texts[1:]
    parent_marks_subcollection = parent.endswith(":")
    parent_clean = parent.rstrip(":").rstrip().rstrip("*").rstrip()

    # Check if every follow-up paragraph is a clean camelCase identifier
    # (sub-collection child key signature). Strip leading whitespace
    # because Confluence indents children with non-breaking spaces or
    # leading spaces.
    children_clean = [c.lstrip().rstrip("*") for c in children]
    all_idents = all(_PROP_IDENT_RE.match(c) for c in children_clean)

    if parent_marks_subcollection and all_idents and parent_clean:
        # Sub-collection: emit one dotted row per child.
        return [f"{parent_clean}.{child}" for child in children_clean]

    # Annotation case (or any other multi-<p> shape): keep only the
    # first <p> as the property name. Drop trailing colon defensively.
    return [parent_clean]


def _parse_row(
    cells: list[Tag],
    col_map: dict[str, int | None],
    page_link_idx: int | None = None,
) -> list[dict]:
    """Parse a single table row into one or more element dicts.

    Returns a list because some Confluence cells encode a sub-collection
    parent + N child fields (e.g., `programParticipationStatuses` with
    4 child key fields), which Phase B.5 follow-up expands into N dotted
    rows. The vast majority of rows still produce exactly one dict.
    """
    def get_cell(key: str) -> str:
        idx = col_map.get(key)
        if idx is not None and idx < len(cells):
            return cells[idx].get_text(strip=True)
        return ""

    name_idx = col_map.get("name")
    name_cell = cells[name_idx] if name_idx is not None and name_idx < len(cells) else None
    names = _extract_property_names(name_cell)
    if not names:
        return []

    # Skip header-like rows that repeat column titles
    if names[0].lower() in ("property name", "element name", "name"):
        return []

    definition = get_cell("definition")
    data_type = get_cell("type")

    # Resource name (from leaf domain pages with "Resource Name" column)
    resource = get_cell("resource")

    # Determine optionality from public/choice columns
    public_req = get_cell("public_req").upper()
    choice_req = get_cell("choice_req").upper()

    # Extract "Data Element Page" link URL if present
    element_page_url = None
    if page_link_idx is not None and page_link_idx < len(cells):
        link_cell = cells[page_link_idx]
        a_tag = link_cell.find("a") if isinstance(link_cell, Tag) else None
        if a_tag and a_tag.get("href", "").startswith("http"):
            element_page_url = a_tag["href"]

    # Clean each name produced by `_extract_property_names` and emit one
    # element dict per name. The cleanup chain handles URLs, parenthetical
    # annotations, and embedded version markers (see Phase B.5 history
    # below). The B.5-follow-up multi-row path means a sub-collection cell
    # with N child key fields produces N rows here, all sharing the same
    # data_type / definition / resource / requirement levels.
    #
    # Phase B.5 (2026-04-09): the previous version-annotation regex only caught
    # patterns like `(2025-26 SY)` and `(for 2024-26 and later)` — anchored to
    # `(20YY-YY` and a closing paren at end-of-string. WI Confluence has at least
    # ~30 element rows with annotations the old regex missed:
    #
    #   - `alternativeCourseCode(used by Xello)`           — vendor mention, no year
    #   - `graduationStatusOptOut(Added 2026-27 SY)`       — Added marker
    #   - `repeatGradeIndicator(2024/25 SY and prior)`     — slash-format year
    #   - `enrollmentTypeDescriptor(moved to core ...)`    — refactor marker
    #   - `reasonExitedDescriptor(Removed 2025-26 SY)`     — Removed marker
    #   - `fiscalYear(under each Dimension Reference)`     — structural placement note
    #   - `cipCodeRemoved 2026-27(Moved under cteProgramServices ...)` — embedded
    #     marker WITHOUT parens (Confluence concatenates multi-line cells via
    #     get_text(); the source had `cipCode\nRemoved 2026-27\n(Moved...)`
    #     in three line-broken cells, all collapsed into one string)
    #
    # Strategy: strip everything from the first `(` to end-of-string (universal
    # paren-strip), then strip embedded `(Removed|Added|Moved) YYYY...` markers
    # that occur without parens. Ed-Fi property names are camelCase identifiers
    # with no parens, so paren-strip is safe. The (Removed|Added|Moved) regex
    # requires a 4-digit year right after to avoid eating legit fields like
    # `lastModified`.
    out: list[dict] = []
    for raw_name in names:
        clean_name = re.sub(r"https?://\S+", "", raw_name).strip()
        clean_name = re.sub(r"\s*\(.*$", "", clean_name).strip()
        clean_name = re.sub(
            r"\s*(?:Removed|Added|Moved)\s+\d{4}.*$", "", clean_name
        ).strip()
        clean_name = re.sub(r"\*+$", "", clean_name).strip()
        clean_name = re.sub(r"\.\s+", ".", clean_name)  # normalize "reference. fieldName" -> "reference.fieldName"
        clean_name = _strip_wi_extension_prefix(clean_name)
        clean_name = _apply_wi_element_corrections(clean_name)
        if not clean_name:
            continue
        # Skip extension-container holders; they wrap the extension schema
        # rather than describing a data element (analyst review #2 T2.6).
        if _is_wi_extension_container(clean_name):
            continue

        out.append({
            "raw_name": raw_name,
            "clean_name": clean_name,
            "resource": resource or None,
            "data_type": data_type or None,
            "public_required": public_req or None,
            "choice_required": choice_req or None,
            "definition": definition or None,
            "element_page_url": element_page_url,
        })

    return out


def extract_business_rules(soup: BeautifulSoup) -> str | None:
    """Extract business rules and validation text from the page.

    Looks for:
    - Text outside data property tables (posting rules, validation notes)
    - Content in expand macros not containing the data properties table
    - Explicit business rule sections
    """
    rules_parts = []

    # Get all text content, excluding the data properties tables
    # Look for paragraphs, lists, and panels outside tables
    for element in soup.find_all(["p", "li", "blockquote"]):
        # Skip if inside a table
        if element.find_parent("table"):
            continue

        text = element.get_text(strip=True)
        if not text or len(text) < 20:
            continue

        # Skip navigation/boilerplate
        if any(skip in text.lower() for skip in [
            "click here to view",
            "expand to see",
            "was this helpful",
            "powered by",
        ]):
            continue

        rules_parts.append(text)

    if not rules_parts:
        return None

    return "\n".join(rules_parts)


def extract_descriptor_tables(soup: BeautifulSoup) -> list[dict]:
    """Extract descriptor/code value tables (not the main data properties table).

    These contain valid values for descriptor fields (e.g., GradeLevelDescriptor codes).
    """
    descriptors = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        # Check if this is a descriptor table (has Code/Value type headers)
        header_row = rows[0]
        header_text = header_row.get_text(strip=True).lower()

        # Skip the main data properties table
        if "property name" in header_text or "#" == header_text[:1]:
            if "definition" in header_text:
                continue

        # Look for code/descriptor tables
        if any(kw in header_text for kw in ["code", "descriptor", "value", "type"]):
            headers = [c.get_text(strip=True) for c in header_row.find_all(["th", "td"])]
            for row in rows[1:]:
                cells = [c.get_text(strip=True) for c in row.find_all(["td"])]
                if cells:
                    descriptors.append({
                        "headers": headers,
                        "values": cells,
                    })

    return descriptors


# Minimum fraction of entity pages that must carry body content for a
# scrape to count as usable (issue #212 item 6). The live corpus has 48
# pages with 48 bodies (2026-07-09) — 0.9 tolerates a few genuinely
# empty pages without letting a half-failed crawl through.
_MIN_ENTITY_BODY_RATIO = 0.9


def scrape_all_entity_pages(
    client: httpx.Client,
    entity_pages: list[dict],
    cache_dir: Path,
    delay: float = 0.5,
) -> dict[str, dict]:
    """Fetch all entity pages and cache raw API responses.

    Returns dict keyed by page_id with parsed content.

    Issue #212 item 6: failure entries (empty ``body_html`` + an
    ``error`` tag) no longer poison the corpus — the resume logic used
    to skip anything already keyed, so one transient Confluence 5xx
    during the initial crawl silently removed that entity page from
    every future WI artifact until someone hand-deleted the cache.
    Failure entries are now re-attempted on every run, and the run
    refuses (raises, not warns) when fewer than
    ``_MIN_ENTITY_BODY_RATIO`` of the entity pages have body content.
    """
    cache_file = cache_dir / "entities_scraped.json"

    # Load existing cache for incremental scraping
    scraped: dict[str, dict] = {}
    if cache_file.exists():
        scraped = json.loads(cache_file.read_text(encoding="utf-8"))
        logger.info("Loaded %d cached entity pages", len(scraped))

    total = len(entity_pages)
    for i, ep in enumerate(entity_pages):
        page_id = ep["page_id"]
        # Skip only HEALTHY cache entries — a cached failure (empty
        # body) is a retry candidate, not a result.
        existing = scraped.get(page_id)
        if existing and existing.get("body_html"):
            continue

        logger.info(
            "  [%d/%d] Fetching: %s (%s)",
            i + 1, total, ep["title"], ep["domain"],
        )

        try:
            page_data = fetch_entity_page(client, page_id)
            body_html = page_data.get("body", {}).get("storage", {}).get("value", "")

            scraped[page_id] = {
                "page_id": page_id,
                "title": ep["title"],
                "domain": ep["domain"],
                "body_html": body_html,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        except httpx.HTTPError as e:
            logger.warning("  Failed to fetch %s: %s", ep["title"], e)
            scraped[page_id] = {
                "page_id": page_id,
                "title": ep["title"],
                "domain": ep["domain"],
                "body_html": "",
                "error": str(e),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }

        # Save incrementally every 10 pages
        if (i + 1) % 10 == 0:
            cache_file.write_text(json.dumps(scraped, indent=2), encoding="utf-8")
            logger.info("  Checkpoint: %d/%d pages cached", len(scraped), total)

        time.sleep(delay)

    # Final save
    cache_file.write_text(json.dumps(scraped, indent=2), encoding="utf-8")
    logger.info("Cached %d entity pages", len(scraped))

    # Corpus-completeness floor (issue #212 item 6): raising, not
    # warning — a mostly-empty scrape must never flow into artifacts.
    # The cache (including tagged failure entries) is kept: failures
    # re-attempt on the next run.
    expected_ids = [ep["page_id"] for ep in entity_pages]
    with_body = sum(
        1 for pid in expected_ids if (scraped.get(pid) or {}).get("body_html")
    )
    if expected_ids and with_body / len(expected_ids) < _MIN_ENTITY_BODY_RATIO:
        raise RuntimeError(
            f"WI Confluence scrape incomplete: only {with_body} of "
            f"{len(expected_ids)} entity pages have body content "
            f"(floor {_MIN_ENTITY_BODY_RATIO:.0%}). Failure entries are "
            f"tagged in {cache_file} and re-attempted on the next run — "
            f"re-run `mc ingest wi` once Confluence recovers, or "
            f"delete the cache to start fresh."
        )

    return scraped


# ---------------------------------------------------------------------------
# Pass 3: Fetch "Data Element Page" linked pages from dpi.wi.gov
# ---------------------------------------------------------------------------


def _extract_element_page_content(html: str) -> str | None:
    """Extract business-rules text from a dpi.wi.gov data element page.

    These are Drupal pages. We grab everything inside the main content area
    (headings, paragraphs, list items, table cells) and return it as plain
    text with light structure preserved via newlines.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Try common Drupal content wrappers in order of specificity
    content = (
        soup.find("div", class_="field-item")
        or soup.find("article")
        or soup.find("main")
        or soup.find("div", {"role": "main"})
        or soup.find("div", class_="region-content")
    )
    if content is None:
        content = soup.body or soup

    parts: list[str] = []
    for el in content.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "blockquote"]):
        text = el.get_text(strip=True)
        if not text or len(text) < 5:
            continue
        # Skip navigation/boilerplate
        if any(skip in text.lower() for skip in [
            "you are here",
            "skip to main",
            "powered by",
            "back to top",
            "wisconsin department of public instruction",
            "breadcrumb",
        ]):
            continue
        # Prefix headings to preserve structure
        if el.name and el.name.startswith("h"):
            parts.append(f"\n## {text}")
        else:
            parts.append(text)

    if not parts:
        return None

    result = "\n".join(parts).strip()
    # Trim excessively long pages (some have huge lookup tables)
    if len(result) > 8000:
        result = result[:8000] + "\n[... truncated]"
    return result


def fetch_element_pages(
    client: httpx.Client,
    element_page_urls: set[str],
    cache_dir: Path,
    delay: float = 0.5,
) -> dict[str, str | None]:
    """Fetch unique dpi.wi.gov data element pages and cache them.

    Returns dict mapping URL -> extracted business rules text (or None on failure).
    """
    cache_file = cache_dir / "element_pages.json"

    # Load existing cache for incremental fetching
    cached: dict[str, dict] = {}
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        logger.info("Loaded %d cached element pages", len(cached))

    # Re-attempt cached FAILURES (tagged "error"); a fetched-but-parse-
    # empty page (status 200, content None, no error tag) stays cached —
    # refetching it every run would be pure churn (issue #212 item 6).
    urls_to_fetch = [
        u for u in sorted(element_page_urls)
        if u not in cached or cached[u].get("error")
    ]
    if not urls_to_fetch:
        logger.info("All %d element pages already cached", len(cached))
    else:
        logger.info("Fetching %d new element pages (%d cached)", len(urls_to_fetch), len(cached))

        for i, url in enumerate(urls_to_fetch):
            logger.info("  [%d/%d] Fetching: %s", i + 1, len(urls_to_fetch), url)
            try:
                resp = client.get(url)
                resp.raise_for_status()
                text = _extract_element_page_content(resp.text)
                cached[url] = {
                    "url": url,
                    "content": text,
                    "status": resp.status_code,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }
            except httpx.HTTPError as e:
                logger.warning("  Failed to fetch %s: %s", url, e)
                cached[url] = {
                    "url": url,
                    "content": None,
                    "status": None,
                    "error": str(e),
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }

            # Checkpoint every 20 pages
            if (i + 1) % 20 == 0:
                cache_file.write_text(json.dumps(cached, indent=2), encoding="utf-8")
                logger.info("  Checkpoint: %d/%d element pages", len(cached), len(element_page_urls))

            time.sleep(delay)

        # Final save
        cache_file.write_text(json.dumps(cached, indent=2), encoding="utf-8")
        logger.info("Cached %d element pages total", len(cached))

    # Return url -> content mapping
    return {url: entry.get("content") for url, entry in cached.items()}


def collect_element_page_urls(scraped: dict[str, dict]) -> set[str]:
    """Scan all cached Confluence entity pages and collect Data Element Page URLs."""
    urls: set[str] = set()
    for page_data in scraped.values():
        html = page_data.get("body_html", "")
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a_tag in soup.find_all("a", href=lambda h: h and "dpi.wi.gov/wise/data-elements" in h):
            urls.add(a_tag["href"])
    return urls


# ---------------------------------------------------------------------------
# Build ElementRecords
# ---------------------------------------------------------------------------


def _merge_duplicate_records(records: list[ElementRecord]) -> list[ElementRecord]:
    """Collapse `(entity, element_name)` collisions into a single merged row.

    WI Confluence documents the same Ed-Fi field on multiple pages whenever
    the field is reused across reporting contexts (e.g., `Sections.schoolId`
    appears on Roster, World Language, and CTE pages with slightly different
    business definitions). The same page can also describe a field multiple
    times in a single table — `StudentSchoolAssociation.schoolId` is listed
    three times on page 56623105 (current required, legacy `(2024-25 and
    prior)`, and current conditional after the int->bigint reformat). Once
    the year-annotation regex strips the parenthetical, all three rows
    collapse to the same `(entity, element_name)` key.

    Pre-merge state at end of `build_element_records`: ~31 keys / 68 rows
    are duplicates. Downstream lookup-keyed code paths (validate.py,
    calibration, scoring caches) silently overwrite all but one of each set,
    losing the distinct business context. This pass merges them deterministically:

      - keep the FIRST record for ordering / domain / source page / entity
      - concat distinct `definition_text` values with " | "
      - concat distinct `business_rules_text` values with "\\n---\\n"
      - prefer the longest non-empty `data_type` (handles "big integer" vs
        "Integer" vs "biginteger" — picks the most descriptive form, also
        corrects typos like "sting" → "String" since "String" is longer)
      - concat distinct `source_page_or_section` URLs with " | " so reviewers
        can still trace each merged row back to all original WI pages

    Phase B.4 fix (2026-04-09). Trade-off: the 8 `Staff.firstName` /
    `lastSurname` / etc. rows that were duplicated by the `otherNames`
    nested array on page 13402113 also collapse here, since `otherNames` is
    documented as a flat array on the WI page rather than as a structurally
    distinct sub-entity.
    """
    from collections import defaultdict

    groups: dict[tuple[str, str], list[ElementRecord]] = defaultdict(list)
    for r in records:
        groups[(r.entity, r.element_name)].append(r)

    merged: list[ElementRecord] = []
    seen_keys: set[tuple[str, str]] = set()
    for r in records:
        key = (r.entity, r.element_name)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        group = groups[key]
        if len(group) == 1:
            merged.append(r)
            continue

        # Merge multiple records into one. Iterate in original order so the
        # first occurrence's metadata wins for fields we don't actively merge.
        def _distinct_concat(values: list[str | None], sep: str) -> str | None:
            seen: list[str] = []
            for v in values:
                if v and v not in seen:
                    seen.append(v)
            return sep.join(seen) if seen else None

        merged_definition = _distinct_concat(
            [g.definition_text for g in group], " | "
        ) or r.definition_text
        merged_rules = _distinct_concat(
            [g.business_rules_text for g in group], "\n---\n"
        )
        merged_sources = _distinct_concat(
            [g.source_page_or_section for g in group], " | "
        ) or r.source_page_or_section

        # Pick richest (longest non-empty) data_type. Ties: keep first.
        non_empty_types = [g.data_type for g in group if g.data_type]
        if non_empty_types:
            merged_data_type = max(non_empty_types, key=len)
        else:
            merged_data_type = r.data_type

        merged.append(r.model_copy(update={
            "data_type": merged_data_type,
            "definition_text": merged_definition,
            "business_rules_text": merged_rules,
            "source_page_or_section": merged_sources,
        }))

    return merged


def _clean_entity_name(title: str) -> str:
    """Extract entity name from Confluence page title.

    Titles look like:
        /studentSchoolAssociation
        /courses
        /contacts (aka. /parents)
        /studentSpecialEducationProgramAssociation (/sSEPA)
        "/sections (Public LEAs Only)"

    Returns the endpoint name stripped of slashes and parenthetical notes.
    """
    # Remove quotes
    name = title.strip().strip('"')

    # Extract the first /endpoint from the title
    match = re.match(r"/?(\w+)", name)
    if match:
        name = match.group(1)
    else:
        name = name

    # Convert endpoint to PascalCase entity name
    # e.g., studentSchoolAssociation -> StudentSchoolAssociation
    # e.g., courses -> Courses -> Course (singularize later via catalog)
    if name and name[0].islower():
        name = name[0].upper() + name[1:]

    # Apply canonical-entity renames so the stored display form matches the
    # spine (round-2 analyst review flagged WI plural
    # `StudentSchoolFoodServicesProgramAssociation` mismatching spine singular
    # `StudentSchoolFoodServiceProgramAssociation`).
    from src.utils.matching import _ENTITY_RENAMES
    if name in _ENTITY_RENAMES:
        name = _ENTITY_RENAMES[name]

    return name


def build_element_records(
    scraped: dict[str, dict],
    entity_pages: list[dict],
    catalog: EdFiCatalog | None = None,
    element_page_content: dict[str, str | None] | None = None,
    edfi_version: str = "5.2",
) -> list[ElementRecord]:
    """Parse scraped entity pages into ElementRecords.

    If *element_page_content* is provided (url -> text mapping from
    `fetch_element_pages`), elements with a linked Data Element Page
    get their element-specific rules prepended to business_rules_text.
    """
    records = []

    for ep in entity_pages:
        page_id = ep["page_id"]
        page_data = scraped.get(page_id)
        if not page_data or not page_data.get("body_html"):
            logger.warning("No content for %s -- skipping", ep["title"])
            continue

        soup = BeautifulSoup(page_data["body_html"], "html.parser")
        domain = ep["domain"]

        # Default entity name from page title, with override for known leaf domains
        raw_entity_default = ep["title"]
        if raw_entity_default in _LEAF_DOMAIN_ENTITY_MAP:
            entity_default = normalize_entity(_LEAF_DOMAIN_ENTITY_MAP[raw_entity_default], catalog)
        else:
            entity_base_default = _clean_entity_name(raw_entity_default)
            entity_default = normalize_entity(entity_base_default, catalog)

        # Parse data properties table
        elements = parse_data_properties_table(soup)

        # Extract business rules from the page
        business_rules = extract_business_rules(soup)

        # Extract descriptor tables for additional context
        descriptor_tables = extract_descriptor_tables(soup)
        descriptor_text = ""
        if descriptor_tables:
            parts = []
            for dt in descriptor_tables:
                row_text = " | ".join(dt["values"])
                parts.append(row_text)
            descriptor_text = "\nDescriptor values: " + "; ".join(parts)

        # Combine business rules with descriptor info
        full_rules = business_rules or ""
        if descriptor_text:
            full_rules = (full_rules + descriptor_text).strip() if full_rules else descriptor_text.strip()

        if not elements:
            logger.warning("No elements found in %s -- check page structure", ep["title"])
            continue

        page_url = f"{CONFLUENCE_BASE}/wiki/spaces/widpiedfi/pages/{page_id}"

        for elem in elements:
            # Determine entity: use "Resource Name" column if available (leaf domain pages),
            # otherwise use the page title
            resource = elem.get("resource")
            if resource:
                raw_entity = resource
                entity_base = _clean_entity_name(resource)
                entity = normalize_entity(entity_base, catalog)
            else:
                raw_entity = raw_entity_default
                entity = entity_default

            definition = elem.get("definition") or ""

            # Build optionality note into definition if useful
            opt_parts = []
            if elem.get("public_required"):
                opt_parts.append(f"Public: {elem['public_required']}")
            if elem.get("choice_required"):
                opt_parts.append(f"Choice: {elem['choice_required']}")
            opt_note = f" [{', '.join(opt_parts)}]" if opt_parts else ""

            # Attach element-specific rules from linked Data Element Page.
            # Only the element-specific portion goes here — entity-level
            # rules are already rendered once in the prompt template via the
            # shared `business_rules_text` parameter. Duplicating the
            # entity-level blob per element would blow the context window on
            # large entities like sEOA (33 elements × ~15K chars = 500K+).
            elem_rules = full_rules
            elem_url = elem.get("element_page_url")
            if elem_url and element_page_content:
                specific_rules = element_page_content.get(elem_url)
                if specific_rules:
                    elem_rules = (
                        f"=== Element-specific rules (from {elem_url}) ===\n"
                        f"{specific_rules}\n\n"
                        f"=== Entity-level rules (shared) ===\n"
                        f"(See entity-level business rules above)"
                    ).strip()

            records.append(ElementRecord(
                state="WI",
                edfi_version=edfi_version,
                domain=domain,
                entity=entity,
                raw_entity=raw_entity,
                element_name=elem["clean_name"],
                data_type=normalize_data_type(elem.get("data_type")),
                definition_text=definition + opt_note if definition else f"(no definition){opt_note}",
                business_rules_text=elem_rules or None,
                source_document="WI DPI Ed-Fi Confluence Wiki",
                source_page_or_section=page_url,
                documented=True,
            ))

    pre_merge_count = len(records)
    records = _merge_duplicate_records(records)
    if len(records) < pre_merge_count:
        logger.info(
            "Merged %d duplicate (entity, element_name) rows -> %d unique elements",
            pre_merge_count - len(records),
            len(records),
        )
    return records


# ---------------------------------------------------------------------------
# Enrichment report
# ---------------------------------------------------------------------------


def generate_enrichment_report(
    records: list[ElementRecord],
    element_page_content: dict[str, str | None] | None,
) -> dict:
    """Build a report of element-page enrichment coverage.

    Returns a dict suitable for JSON serialization with:
    - summary counts
    - per-entity breakdown (enriched / not enriched / no page col)
    - list of enriched elements with their source URLs
    - list of unenriched elements grouped by reason
    """
    enriched_elements: list[dict] = []
    unenriched_elements: list[dict] = []
    entity_stats: dict[str, dict] = {}

    for r in records:
        rules = r.business_rules_text or ""
        is_enriched = "=== Element-specific rules" in rules

        # Extract the URL if enriched
        url = None
        if is_enriched:
            import re as _re
            m = _re.search(r"=== Element-specific rules \(from (https://[^)]+)\)", rules)
            if m:
                url = m.group(1)

        entry = {
            "entity": r.entity,
            "element_name": r.element_name,
            "enriched": is_enriched,
            "element_page_url": url,
        }

        if is_enriched:
            enriched_elements.append(entry)
        else:
            unenriched_elements.append(entry)

        # Per-entity stats
        if r.entity not in entity_stats:
            entity_stats[r.entity] = {"total": 0, "enriched": 0, "unenriched": 0}
        entity_stats[r.entity]["total"] += 1
        if is_enriched:
            entity_stats[r.entity]["enriched"] += 1
        else:
            entity_stats[r.entity]["unenriched"] += 1

    # Compute per-entity enrichment rate
    entity_breakdown = []
    for entity_name in sorted(entity_stats):
        s = entity_stats[entity_name]
        rate = s["enriched"] / s["total"] if s["total"] > 0 else 0
        entity_breakdown.append({
            "entity": entity_name,
            "total": s["total"],
            "enriched": s["enriched"],
            "unenriched": s["unenriched"],
            "enrichment_rate": round(rate, 3),
        })

    content_available = len(element_page_content or {})
    content_with_text = sum(1 for v in (element_page_content or {}).values() if v)

    return {
        "summary": {
            "total_elements": len(records),
            "enriched": len(enriched_elements),
            "unenriched": len(unenriched_elements),
            "enrichment_rate": round(len(enriched_elements) / len(records), 3) if records else 0,
            "unique_element_pages_cached": content_available,
            "element_pages_with_content": content_with_text,
        },
        "entity_breakdown": entity_breakdown,
        "enriched_elements": enriched_elements,
        "unenriched_elements": unenriched_elements,
    }


# ---------------------------------------------------------------------------
# POC-3 orchestrator
# ---------------------------------------------------------------------------
#
# NOTE: module-level `run()` is a PLAIN FUNCTION called by `src.cli:ingest_wi`.
# Do NOT wrap this in `@click.command()` — that would turn it into a Click
# command whose `__call__` re-parses sys.argv and breaks the CLI routing.
# (See CLAUDE.md "Two gotchas resolved" — this bit us for two hours already.)

from src.utils.paths import (
    state_elements_path,
    state_gap_log_path,
    state_spine_path,
)

_MC_ROOT = Path(__file__).resolve().parents[2]
_WI_SPINE_PATH = state_spine_path("WI")
_WI_CACHE_DIR = _MC_ROOT / "data" / "raw" / "wi" / "confluence"
_WI_ELEMENTS_OUT = state_elements_path("WI", "source")
_WI_ELEMENTS_SPINE_OUT = state_elements_path("WI", "spine")
_WI_GAP_OUT = state_gap_log_path("WI")


def _load_or_fetch_confluence(
    cache_dir: Path, delay: float = 0.5
) -> tuple[list[dict], dict[str, dict], dict[str, str | None] | None]:
    """Load cached Confluence scrape or fetch from the live API.

    Returns (entity_pages, scraped_by_page_id, element_page_content).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    tree_cache = cache_dir / "page_tree.json"
    fetch_cache = cache_dir / "entities_scraped.json"
    elem_cache = cache_dir / "element_pages.json"

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        if tree_cache.exists():
            entity_pages = json.loads(tree_cache.read_text(encoding="utf-8"))
            logger.info("Loaded %d entity pages from tree cache", len(entity_pages))
        else:
            logger.info("Crawling WI Confluence page tree from %s/%s", CONFLUENCE_BASE, TOP_PAGE_ID)
            entity_pages = crawl_page_tree(client, cache_dir, delay=delay)

        if fetch_cache.exists():
            scraped = json.loads(fetch_cache.read_text(encoding="utf-8"))
            logger.info("Loaded %d scraped entity pages from cache", len(scraped))
        else:
            scraped = scrape_all_entity_pages(client, entity_pages, cache_dir, delay=delay)

        element_page_content: dict[str, str | None] | None = None
        if elem_cache.exists():
            cached_raw = json.loads(elem_cache.read_text(encoding="utf-8"))
            element_page_content = {url: entry.get("content") for url, entry in cached_raw.items()}
            logger.info("Loaded %d element pages from cache", len(element_page_content))
        else:
            elem_urls = collect_element_page_urls(scraped)
            if elem_urls:
                element_page_content = fetch_element_pages(client, elem_urls, cache_dir, delay=delay)

    return entity_pages, scraped, element_page_content


def run() -> None:
    """POC-3 WI ingestion: scrape Confluence, match to spine, emit gap log.

    Writes `data/out/wi_elements_source.json` (StateElements) and
    `data/out/wi_gap_log.json` (spine-match diagnostics).
    """
    from src.ingest.shared import (
        compute_coverage,
        populate_data_types_from_spine,
        populate_edfi_standard_definition_from_spine,
        run_unflatten_pass,
        stamp_edfi_domains,
        write_dual_lens_artifacts,
    )
    from src.models.spine import StateSpine
    from src.utils.matching import attribute_record_source

    if not _WI_SPINE_PATH.exists():
        raise FileNotFoundError(
            f"No WI spine at {_WI_SPINE_PATH}. "
            f"Run `mc spine fetch --state WI` + `mc spine build --state WI` first."
        )
    spine = StateSpine.model_validate_json(_WI_SPINE_PATH.read_text(encoding="utf-8"))
    logger.info(
        "Loaded WI spine: %d core entities, %d extensions (Ed-Fi %s)",
        spine.entity_count, spine.extension_count, spine.edfi_version,
    )

    entity_pages, scraped, element_page_content = _load_or_fetch_confluence(_WI_CACHE_DIR)

    records = build_element_records(
        scraped,
        entity_pages,
        catalog=spine.catalog,
        element_page_content=element_page_content,
        edfi_version=spine.edfi_version,
    )
    logger.info("Built %d ElementRecords from %d entity pages", len(records), len(entity_pages))

    # Format-drift floor (issue #213 item 2): the parser skips unrecognized
    # tables/pages silently, so enforce a corpus-size floor before anything
    # downstream consumes the records.
    enforce_parse_floor(records)

    # WI historically skips post-unflatten dedup (Confluence scrape has no
    # duplicates to collapse). WI is also the only adapter that runs the
    # descriptor-suffix normalization BETWEEN attribute + populate_types, so
    # it orchestrates the helpers inline rather than calling the single-shot
    # `assemble_source_driven(dedup=False)`.
    recovered = run_unflatten_pass(records, spine)
    attribute_record_source(records, spine)
    _normalize_descriptor_type_in_place(records)
    populate_data_types_from_spine(records, spine)
    populate_edfi_standard_definition_from_spine(records, spine)
    # Issue #184: stamp the Ed-Fi domain (distinct from `domain` = Source
    # Area) on every source-lens record. WI orchestrates the helpers inline
    # rather than via assemble_source_driven, so the stamp is explicit here.
    stamp_edfi_domains(records, spine)
    assembly = compute_coverage(records, spine)

    # Shared adapter tail: dual-lens writes → gap log → swagger backfill
    # (issue #213 item 3 — the ~80-line sequence lives once in shared.py).
    write_dual_lens_artifacts(
        state="WI",
        spine=spine,
        records=records,
        assembly=assembly,
        recovered=recovered,
        source_document="WI Ed-Fi Confluence",
        source_out=_WI_ELEMENTS_OUT,
        spine_out=_WI_ELEMENTS_SPINE_OUT,
        gap_out=_WI_GAP_OUT,
        spine_path=_WI_SPINE_PATH,
        spine_source_rel=str(_WI_SPINE_PATH.relative_to(_MC_ROOT)),
        source_coverage_note=(
            "Of our WI doc elements, how many match a spine element "
            "(case-insensitive, FK+descriptor aliases)."
        ),
        spine_coverage_note=(
            "Of the spine's authoritative element slots, how many are "
            "represented in WI docs."
        ),
        logger=logger,
    )


if __name__ == "__main__":
    run()
