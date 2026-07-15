"""Parse Minnesota MDE Ed-Fi Data Mapping Matrix into pipeline data structures.

Unlike AZ (a per-entity element tree) or WI (Confluence wiki pages), MN's
authoritative state source is a **mapping matrix** XLSX: each row declares
"MDE field X maps to Ed-Fi entity Y, element Z". That means spine alignment
is already 1:1 per row — there's no entity-table detection, no HTML scraping.

Source: `mn-mde-edfi/MDE-EdFi-Documentation` GitHub repo, current-year
    `2026-27 MDE Ed-Fi Documentation/2026-2027 Data Mapping Matrix Ed-Fi
    Suite 3 v 6.2.xlsx`. Cached locally at `data/raw/mn/github/` with auto-
    download when missing, mirroring the WI Confluence auto-cache pattern.

Sheets processed:
    - `Student Enrollment Elements`: 199 data rows, 8 columns.
    - `MCCC Elements`: 156 data rows, 10 columns.
    - `List of Custom Descriptors`: skipped (descriptor URI catalog, not
      per-element mappings — useful only for descriptor value enrichment).

Known source-side wrinkles (handled, not parser bugs):

- **`Ed-Fi Element Name` is sometimes a multi-line business rule**, e.g.
  `"To indicate Kindergarten Schedule\\nCore.Calendar.CalendarCode = …"`.
  These rows are retained but the rule text goes into `business_rules_text`
  and `element_name` falls back to the MDE element name so the row stays
  comparable.
- **`Ed-Fi Entity` may be a reference path** like
  `"Course.EducationOrganizationReference"`. We split on the first `.` so
  spine matching uses the root entity (`Course`); the full reference path
  is preserved in `raw_entity`.
- **`Ed-Fi Element Name` in MCCC rows sometimes holds a foreign key dotted
  name** like `Course.EducationOrganizationReference.postSecondaryInstitutionId`.
  `record_match_keys` already emits path-tail aliases, so those resolve.

CRITICAL: module-level `run()` is a PLAIN function. Do NOT decorate it with
`@click.command()` — that makes `cli.py`'s `run_mn()` invocation call
`Click.Command.__call__`, re-parse `sys.argv`, and die. See
`tests/test_ingest_mn.py::TestCliWiring` for the regression guard.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import openpyxl

from src.ingest.normalize import normalize_data_type, normalize_entity
from src.models.element import ElementRecord
from src.models.spine import StateSpine

logger = logging.getLogger(__name__)


from src.utils.paths import (
    state_elements_path,
    state_gap_log_path,
    state_spine_path,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MN_SPINE_PATH = state_spine_path("MN")
_MN_GITHUB_DIR = _PROJECT_ROOT / "data" / "raw" / "mn" / "github"
_MN_XLSX_NAME = "data_mapping_matrix_2026-27.xlsx"
_MN_XLSX_PATH = _MN_GITHUB_DIR / _MN_XLSX_NAME
_MN_XLSX_URL = (
    "https://raw.githubusercontent.com/mn-mde-edfi/MDE-EdFi-Documentation/master/"
    "2026-27%20MDE%20Ed-Fi%20Documentation/"
    "2026-2027%20Data%20Mapping%20Matrix%20Ed-Fi%20Suite%203%20v%206.2.xlsx"
)
_MN_ELEMENTS_OUT = state_elements_path("MN", "source")
_MN_ELEMENTS_SPINE_OUT = state_elements_path("MN", "spine")
_MN_GAP_OUT = state_gap_log_path("MN")

# Sheets in the Data Mapping Matrix that carry row-per-element mappings.
_ELEMENT_SHEETS = ("Student Enrollment Elements", "MCCC Elements")

# Heuristic: the Ed-Fi Element Name column sometimes holds a multi-line
# business rule instead of a clean identifier. Detect with newline or
# '=' (comparison) — identifiers never contain either.
def _looks_like_rule(value: str) -> bool:
    return "\n" in value or "=" in value


@dataclass
class MNMatrixRow:
    """One row from a mapping-matrix sheet, pre-canonicalization."""

    sheet: str
    row_id: str
    collection: str | None
    mde_entity: str | None  # present only on MCCC sheet
    mde_group: str | None
    mde_element: str | None
    edfi_entity_raw: str | None
    edfi_element_raw: str | None
    mapping_method: str | None
    enumeration: str | None
    notes: str | None


def _s(val: object) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _download_xlsx_if_missing() -> None:
    if _MN_XLSX_PATH.exists():
        return
    _MN_GITHUB_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading MN mapping matrix from %s", _MN_XLSX_URL)
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        resp = client.get(_MN_XLSX_URL)
        resp.raise_for_status()
        _MN_XLSX_PATH.write_bytes(resp.content)
    logger.info("  wrote %s (%d bytes)", _MN_XLSX_PATH.name, _MN_XLSX_PATH.stat().st_size)


def parse_mn_matrix(workbook_path: Path) -> list[MNMatrixRow]:
    """Parse the two element-mapping sheets into flat MNMatrixRow records.

    Column detection is header-driven so small layout changes don't break
    parsing silently. Each target column is required to appear in the
    header; missing columns raise immediately rather than silently emit
    Nones.
    """
    wb = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    rows: list[MNMatrixRow] = []

    # Accepted header variants (key -> set of lowered header strings).
    header_aliases: dict[str, set[str]] = {
        "row_id": {"row id"},
        "collection": {"collection"},
        "mde_entity": {"mde entity"},
        "mde_group": {"mde element group"},
        "mde_element": {"mde element name"},
        "edfi_entity": {"ed-fi entity"},
        "edfi_element": {"ed-fi element name"},
        "mapping_method": {"element mapping method"},
        "enumeration": {"ed-fi enumeration descriptor/type"},
        "notes": {"notes for discussion"},
    }

    for sheet_name in _ELEMENT_SHEETS:
        if sheet_name not in wb.sheetnames:
            logger.warning("Expected sheet %r missing from %s", sheet_name, workbook_path.name)
            continue
        ws = wb[sheet_name]
        sheet_rows = list(ws.iter_rows(values_only=True))
        if not sheet_rows:
            continue

        header = [(_s(c) or "").lower() for c in sheet_rows[0]]
        cols: dict[str, int] = {}
        for key, aliases in header_aliases.items():
            idx = next((i for i, h in enumerate(header) if h in aliases), None)
            if idx is not None:
                cols[key] = idx

        for required in ("row_id", "edfi_entity", "edfi_element"):
            if required not in cols:
                raise ValueError(
                    f"Sheet {sheet_name!r}: missing required header for {required!r}. "
                    f"Found headers: {header}"
                )

        for erow in sheet_rows[1:]:
            row_id = _s(erow[cols["row_id"]]) if cols["row_id"] < len(erow) else None
            if row_id is None:
                continue  # blank/spacer row

            def _cell(key: str) -> str | None:
                idx = cols.get(key)
                if idx is None or idx >= len(erow):
                    return None
                return _s(erow[idx])

            # Skip header-style narrative rows where edfi_entity is empty.
            edfi_entity_raw = _cell("edfi_entity")
            if edfi_entity_raw is None:
                continue

            rows.append(MNMatrixRow(
                sheet=sheet_name,
                row_id=row_id,
                collection=_cell("collection"),
                mde_entity=_cell("mde_entity"),
                mde_group=_cell("mde_group"),
                mde_element=_cell("mde_element"),
                edfi_entity_raw=edfi_entity_raw,
                edfi_element_raw=_cell("edfi_element"),
                mapping_method=_cell("mapping_method"),
                enumeration=_cell("enumeration"),
                notes=_cell("notes"),
            ))

    wb.close()
    return rows


_ENTITY_PATH_RE = re.compile(r"\s*>\s*|\.")


def _split_root_entity(raw: str) -> tuple[str, str | None]:
    """Split an Ed-Fi entity reference path into (concat_entity, suffix_info).

    MN uses three entity-path conventions in the same column:
        - Plain name: "Calendar"
        - Dot reference: "Course.EducationOrganizationReference"
        - Angle-bracket navigation: "Course > AssessmentTool",
          "Grade > StudentSectionAssociation > CollegeCourseReference > Course"

    For angle-bracket navigations, concatenating PascalCase segments produces
    a flattened sub-collection name (e.g. `CourseAssessmentTool`) that
    `spine.unflatten` can resolve back to the parent entity. For dot
    references, treat everything after the first dot as a reference suffix
    rather than part of the entity name, since those describe a reference
    path (`Course` has a reference called `EducationOrganizationReference`),
    not a flattened sub-collection.
    """
    raw = raw.strip()
    if ">" in raw:
        parts = [p.strip() for p in re.split(r"\s*>\s*", raw) if p.strip()]
        # Normalize internal whitespace in each segment before concatenating
        # so "LevelCharacteristics" style single-word segments still PascalCase.
        parts = [re.sub(r"\s+", "", p) for p in parts]
        if not parts:
            return raw, None
        concat = "".join(p[0].upper() + p[1:] if p else p for p in parts)
        return concat, " > ".join(parts[1:]) if len(parts) > 1 else None
    if "." in raw:
        root, suffix = raw.split(".", 1)
        return root.strip(), suffix.strip() or None
    return raw, None


# A cell whose element-name value is actually a comma-separated list of
# multiple identifiers. We only expand when ALL comma-separated tokens look
# like identifiers (alphanumeric, no spaces within a token) to avoid chopping
# up human-written descriptions.
_IDENT_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# MN matrix uses Arabic numeral 1 in places Ed-Fi uses Roman I
# (`Title1PartAParticipant` vs spine's `titleIPartAParticipant`). Apply the
# substitution to element names too — the entity-name path is handled in
# `normalize._ENTITY_NAME_FIXES`.
_TITLE1_RE = re.compile(r"\bTitle1(?=[A-Za-z])")


def _collapse_whitespace_to_camel(name: str) -> str:
    """Collapse internal whitespace in a label like `First Name` to camelCase.

    MN matrix authors sometimes write humanized labels inside FK-navigation
    paths (`StudentReference>StudentUniqueId > First Name`). After
    `_strip_mn_nav_path` reduces that to `First Name`, we still need to
    produce the camelCase identifier `firstName` for display consistency.

    Preserves the input unchanged when there's no internal whitespace.
    """
    if " " not in name.strip():
        return name
    tokens = [t for t in re.split(r"\s+", name.strip()) if t]
    if not tokens:
        return name
    pascal = "".join(t[0].upper() + t[1:] if t else t for t in tokens)
    if pascal:
        return pascal[0].lower() + pascal[1:]
    return name


def _strip_mn_nav_path(name: str) -> str:
    """Strip FK-navigation path prefixes from an MN element name.

    The MDE mapping matrix encodes reference navigation as `>`-separated
    segments — e.g., `StudentReference>StudentUniqueId > LastSurname`. Spine
    matching already handles these via path-tail alias emission in
    `utils.matching.element_aliases`. For display, we want the terminal
    segment so the workbook reads cleanly. Examples:

        `StudentReference>StudentUniqueId`               -> `studentUniqueId`
        `StudentReference>StudentUniqueId > LastSurname` -> `lastSurname`
        `StudentReference>StudentUniqueId > First Name`  -> `firstName`
        `CollegeCourseReference >`                       -> `collegeCourseReference`

    Only fires when `>` is present — dotted FK paths are still handled by
    `_strip_known_source_aliases` / `_strip_entity_prefix`. Trailing
    whitespace / collapsed-internal-space is normalized via
    `_collapse_whitespace_to_camel` so humanized labels become camelCase.

    Truncated source paths like `CollegeCourseReference >` (trailing `>` with
    no following segment) fall back to the preceding named segment — analyst-
    review round-3 P2 flagged these as unclean artifacts. A naive
    `rsplit(">", 1)[-1]` returns an empty string in that case, so we do a
    segment-based split + empty-filter instead to pick the last non-empty
    segment reliably.
    """
    if ">" not in name:
        return name
    segments = [s.strip() for s in name.split(">")]
    segments = [s for s in segments if s]
    if not segments:
        return name
    tail = segments[-1]
    tail = _collapse_whitespace_to_camel(tail)
    # Normalize first-letter case to match Ed-Fi camelCase element convention
    # when the tail is a single identifier (no dots, no existing lowercase).
    if tail and "." not in tail and tail[0].isupper():
        tail = tail[0].lower() + tail[1:]
    return tail


def _clean_element_name(name: str) -> str:
    # Title1 → TitleI (Roman-numeral normalization; see _TITLE1_RE docstring).
    cleaned = _TITLE1_RE.sub("TitleI", name)
    # Strip trailing dots that the MDE matrix occasionally leaves (e.g.,
    # `GenderIdentities.`) — these are authorial typos, not path separators.
    cleaned = cleaned.rstrip(".")
    # Strip FK-navigation path prefixes (analyst review round-2 reviewer B).
    cleaned = _strip_mn_nav_path(cleaned)
    return cleaned


# MN matrix authors occasionally append a role hint in parentheses to the
# Ed-Fi Element Name column — e.g. `AcademicHonorCategory (Part of Identity)`,
# `Language (Optional Collection)`, `HonorDescription (Part of Identity)`. The
# bare identifier is needed for spine matching; the hint belongs in the
# definition text where analysts can still see it.
_ANNOTATION_RE = re.compile(r"\s*\(([^)]+)\)\s*$")


def _strip_element_annotations(name: str) -> tuple[str, str | None]:
    """Return (bare_name, annotation_text). Annotation is None if absent."""
    m = _ANNOTATION_RE.search(name)
    if m:
        bare = name[: m.start()].strip()
        return bare, m.group(1).strip()
    return name, None


def _strip_entity_prefix(name: str, root_entity: str | None) -> str:
    """Strip a leading `<entity>.` prefix from the element name.

    Flagged by analyst review: MN row with entity `Student` sometimes carries
    element `Student.StudentUniqueId` where every other row uses the bare
    identifier. Compare case-insensitively so `student.` stripping works even
    when the root_entity form differs slightly in case.
    """
    if not root_entity:
        return name
    prefix = root_entity + "."
    if name.startswith(prefix):
        return name[len(prefix):]
    if name.lower().startswith(prefix.lower()):
        return name[len(prefix):]
    return name


# MN matrix authors sometimes prefix element names with a source alias that
# isn't the row's Ed-Fi Entity — e.g., `SEOA.FirstName` on a Student row,
# `BirthData.BirthDate`, `Core.Student.StudentUniqueId`, `Name.LastSurname`.
# Round-2 analyst review flagged 46 rows across 17 such prefixes. Strip a
# closed list of known source aliases so the element name is the bare
# identifier the spine can match on. `memsberships` is a source-side typo
# (should be `memberships`) — we strip it as a prefix but leave the typo
# itself documented in Known Limitations.
_MN_DOTTED_SOURCE_ALIASES: tuple[str, ...] = (
    "Core.Student",
    "SEOA",
    "BirthData",
    "Name",
    "OtherName",
    "Membership",
    "memsberships",
    "Transportation",
    "MeetingTimes",
    "CourseIdentificationCode",
    "CourseIndentificationCode",  # source-side typo; document in KL
    "EducationOrganizationIndicator",
    "NeglectedOrDelinquentProgramService",
    "postsecondaryinstitution",
    "staffReference",
    # Round 2.2 additions — bare entity-name and FK-reference prefixes the
    # MN matrix uses in `Ed-Fi Element Name` to disambiguate path semantics.
    # The spine-match step already resolves these via path-tail aliases, but
    # the display-side `element_name` was retaining the prefix. Reviewer B
    # flagged this as a clarity problem (workbook readability, not matching).
    "Student",
    "School",
    "EducationOrganization",
    "TransportingLocalEducationAgencyReference",
    "addresses",
)


def _strip_known_source_aliases(name: str) -> str:
    """Strip any of a closed list of MN source-alias prefixes from element name.

    Applied AFTER `_strip_entity_prefix` so entity-matched prefixes take
    precedence. Case-insensitive match on prefix + `.`; preserves the tail
    exactly. Iterates to a fixed point — round-2.2 reviewer flagged rows
    like `Transportation.TransportingLocalEducationAgencyReference.Local
    EducationAgencyId` where stripping just `Transportation.` leaves a
    residual chain that also starts with an aliased prefix. Fixed-point
    iteration (bounded by len(aliases) iterations as a safety guard)
    ensures full stripping without ordering sensitivity.
    """
    if "." not in name:
        return name
    prefixes = sorted(_MN_DOTTED_SOURCE_ALIASES, key=len, reverse=True)
    max_iter = len(prefixes) + 1
    for _ in range(max_iter):
        stripped = None
        for prefix in prefixes:
            pfx = prefix + "."
            if name.lower().startswith(pfx.lower()):
                stripped = name[len(pfx):]
                break
        if stripped is None or not stripped or "." not in stripped:
            if stripped is not None:
                name = stripped
            break
        name = stripped
    return name


# Shared spine-type helpers were promoted to `src.ingest.shared` in Round 2.2
# so AZ and WI can apply the same canonical-type contract. Re-exported here as
# module-local names for backward compat with existing tests / callers.
from src.ingest.shared import (  # noqa: E402,F401 — deliberate re-exports
    build_spine_type_index as _build_spine_type_index,
    canonical_type as _canonical_type,
    populate_data_types_from_spine as _populate_data_types_from_spine,
)


def _expand_comma_list(name: str) -> list[str]:
    """Split a comma-separated element cell when every token is identifier-shaped.

    MN mapping-matrix authors occasionally pack two distinct Ed-Fi elements
    into a single cell, e.g. `CourseIdentificationSystem, CourseIndentificationCode.
    IdentificationCode`. Tokens may be bare identifiers OR dotted paths —
    analyst-review P2 flagged that the narrower ident-only check left composite
    rows like the above unsplit, rendering a matched-core row with an unclean
    visible label (and a source typo in the prefix). Using `_PATH_TOKEN_RE`
    accepts dotted tokens so downstream `_strip_known_source_aliases` +
    `_strip_entity_prefix` normalize each split token independently.

    Human-prose cells like `Course Reference (CourseIdentificationCode, CourseCode)`
    still stay unsplit — spaces and parens reject both regexes.
    """
    if "," not in name:
        return [name]
    tokens = [t.strip() for t in name.split(",")]
    if len(tokens) < 2:
        return [name]
    if all(_PATH_TOKEN_RE.match(t) for t in tokens):
        return tokens
    return [name]


# Multi-target splitters. Used only when the Ed-Fi Entity cell lists 2+
# entities (comma or `&` separated) — analyst feedback flagged MN rows like
# `Student, StudentEducationOrganizationAssociation` / `BirthData.BirthDate,
# SEOA.BirthDate` that violate the one-row-per-element premise.
_MULTI_ENTITY_SEP_RE = re.compile(r"\s*[,&]\s*")
_ENTITY_TOKEN_RE = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_PATH_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")


def _split_multi_entity(entity_raw: str) -> list[str] | None:
    """Return a list of entity names when the cell is a multi-target list.

    Returns None if the cell is a single entity or the list fails quality
    checks (contains non-PascalCase tokens). Recognizes both `,` and `&`
    separators — MN row 295 uses `StudentEducationOrganizationAssociation &
    Student`, everywhere else uses a comma.
    """
    parts = [p.strip() for p in _MULTI_ENTITY_SEP_RE.split(entity_raw) if p.strip()]
    if len(parts) < 2:
        return None
    if not all(_ENTITY_TOKEN_RE.match(p) for p in parts):
        return None
    return parts


def _expand_multi_target_elements(name: str) -> list[str] | None:
    """Aggressive comma-split for element cells in multi-target rows.

    Looser than `_expand_comma_list` — accepts dotted paths like
    `BirthData.BirthDate` so `BirthData.BirthDate, SEOA.BirthDate` splits
    correctly. Returns None when any token is obviously non-identifier (has
    embedded spaces, parenthetical hints, etc.) so ambiguous cells fall back
    to the single-token path.
    """
    if "," not in name:
        return [name]
    tokens = [t.strip() for t in name.split(",")]
    if len(tokens) < 2:
        return None
    if all(_PATH_TOKEN_RE.match(t) for t in tokens):
        return tokens
    return None


def _expand_multi_target_rows(rows: list[MNMatrixRow]) -> list[MNMatrixRow]:
    """Fan out rows whose `Ed-Fi Entity` cell names multiple target entities.

    Analyst review flagged MN rows with entity cells like
    `Student, StudentEducationOrganizationAssociation` paired with element
    cells like `BirthData.BirthDate, SEOA.BirthDate`. These violate the
    one-row-per-element premise. We emit one pseudo-row per (entity, element)
    pair so the downstream single-target logic handles them uniformly.

    Strategy:
        - If entity_count == element_count, pair 1:1 (parallel lists).
        - Else, emit the full cross-product (N*M rows).

    Rows without multi-entity signals pass through unchanged.
    """
    import dataclasses

    expanded: list[MNMatrixRow] = []
    for r in rows:
        if r.edfi_entity_raw is None:
            expanded.append(r)
            continue
        entity_list = _split_multi_entity(r.edfi_entity_raw)
        if entity_list is None:
            expanded.append(r)
            continue
        primary = r.edfi_element_raw or ""
        element_list = _expand_multi_target_elements(primary)
        if element_list is None:
            # Elements can't be split confidently — fall back to a single
            # composite element so the row still lands in the catalog as an
            # unresolved multi-target (spine match will likely miss).
            element_list = [primary]
        if len(entity_list) == len(element_list):
            pairs = list(zip(entity_list, element_list))
        else:
            pairs = [(e, el) for e in entity_list for el in element_list]
        for ent, elem in pairs:
            expanded.append(dataclasses.replace(
                r,
                edfi_entity_raw=ent,
                edfi_element_raw=elem,
            ))
    return expanded


def build_element_records(
    rows: list[MNMatrixRow],
    edfi_version: str,
    source_document: str,
    catalog=None,
) -> list[ElementRecord]:
    """Convert parsed MN matrix rows to ElementRecord instances."""
    records: list[ElementRecord] = []

    rows = _expand_multi_target_rows(rows)

    for r in rows:
        if r.edfi_entity_raw is None:
            continue
        # MN matrix uses literal "-" in `Ed-Fi Entity` and/or `Ed-Fi Element
        # Name` to mark MDE fields with no Ed-Fi mapping (entity OR element
        # left blank). These rows carry no resolvable target and shouldn't
        # pollute the source-coverage denominator.
        if r.edfi_entity_raw.strip() == "-":
            continue
        if (r.edfi_element_raw or "").strip() == "-":
            continue
        root_entity, ref_suffix = _split_root_entity(r.edfi_entity_raw)
        normalized_entity = normalize_entity(root_entity, catalog)

        edfi_element = r.edfi_element_raw or ""
        rule_text: str | None = None
        if edfi_element and _looks_like_rule(edfi_element):
            # Cell holds a multi-line business rule, not an identifier.
            rule_text = edfi_element
            element_names = [r.mde_element or "(unmapped rule)"]
        else:
            primary = edfi_element or r.mde_element or "(unnamed)"
            element_names = _expand_comma_list(primary)

        definition_parts: list[str] = []
        if r.mde_group and r.mde_element:
            definition_parts.append(f"MDE mapping: {r.mde_group}.{r.mde_element}")
        elif r.mde_element:
            definition_parts.append(f"MDE mapping: {r.mde_element}")
        if r.mde_entity:
            definition_parts.append(f"MDE entity: {r.mde_entity}")
        if r.enumeration and r.enumeration.lower() != "n/a":
            definition_parts.append(f"Enumeration: {r.enumeration}")
        if ref_suffix:
            definition_parts.append(f"Via reference: {ref_suffix}")
        definition_text = "; ".join(definition_parts)

        combined_rules: list[str] = []
        if rule_text:
            combined_rules.append(rule_text)
        if r.notes:
            combined_rules.append(f"Notes: {r.notes}")
        business_rules_text = "\n".join(combined_rules) if combined_rules else None

        raw_entity = r.edfi_entity_raw
        if r.mde_group:
            raw_entity = f"mn:{r.mde_group}/{r.edfi_entity_raw}"

        for raw_element_name in element_names:
            bare, annotation = _strip_element_annotations(raw_element_name)
            bare = _strip_entity_prefix(bare, root_entity)
            bare = _strip_entity_prefix(bare, normalized_entity)
            bare = _strip_known_source_aliases(bare)
            bare = _clean_element_name(bare)

            row_definition = definition_text
            if annotation:
                role_note = f"role: {annotation}"
                row_definition = (
                    f"{row_definition}; {role_note}" if row_definition else role_note
                )

            records.append(ElementRecord(
                state="MN",
                edfi_version=edfi_version,
                domain=r.collection or r.sheet,
                entity=normalized_entity,
                raw_entity=raw_entity,
                element_name=bare,
                data_type=normalize_data_type(None),
                definition_text=row_definition,
                business_rules_text=business_rules_text,
                source_document=source_document,
                source_page_or_section=f"{r.sheet}#row{r.row_id}",
                documented=True,
            ))

    return records


def run() -> None:
    """POC-3 MN ingestion: parse Data Mapping Matrix, match to spine.

    Writes `data/out/mn_elements_source.json` (StateElements) and
    `data/out/mn_gap_log.json` (spine-match diagnostics).
    """
    from src.ingest.shared import (
        assemble_source_driven,
        write_dual_lens_artifacts,
    )

    if not _MN_SPINE_PATH.exists():
        raise FileNotFoundError(
            f"No MN spine at {_MN_SPINE_PATH}. "
            f"Run `poc3 spine fetch --state MN` + `poc3 spine build --state MN` first."
        )
    spine = StateSpine.model_validate_json(_MN_SPINE_PATH.read_text(encoding="utf-8"))
    logger.info(
        "Loaded MN spine: %d core entities, %d extensions (Ed-Fi %s)",
        spine.entity_count, spine.extension_count, spine.edfi_version,
    )

    _download_xlsx_if_missing()
    if not _MN_XLSX_PATH.exists():
        raise FileNotFoundError(f"MN mapping matrix not found at {_MN_XLSX_PATH}")

    rows = parse_mn_matrix(_MN_XLSX_PATH)
    logger.info(
        "Parsed %s: %d rows across %d sheets",
        _MN_XLSX_PATH.name, len(rows),
        len({r.sheet for r in rows}),
    )

    records = build_element_records(
        rows,
        edfi_version=spine.edfi_version,
        source_document=_MN_XLSX_PATH.name,
        catalog=spine.catalog,
    )
    logger.info("Built %d MN ElementRecords", len(records))

    records, recovered, assembly = assemble_source_driven(records, spine)

    # Shared adapter tail: dual-lens writes → gap log → swagger backfill
    # (issue #213 item 3 — the ~80-line sequence lives once in shared.py).
    write_dual_lens_artifacts(
        state="MN",
        spine=spine,
        records=records,
        assembly=assembly,
        recovered=recovered,
        source_document=_MN_XLSX_PATH.name,
        source_out=_MN_ELEMENTS_OUT,
        spine_out=_MN_ELEMENTS_SPINE_OUT,
        gap_out=_MN_GAP_OUT,
        spine_path=_MN_SPINE_PATH,
        spine_source_rel=str(_MN_SPINE_PATH.relative_to(_PROJECT_ROOT)),
        source_coverage_note=(
            "Of our MN mapping-matrix rows, how many match a spine element "
            "(case-insensitive, FK+descriptor aliases)."
        ),
        spine_coverage_note=(
            "Of the spine's authoritative element slots, how many are "
            "represented in MN docs."
        ),
        logger=logger,
    )


if __name__ == "__main__":
    run()
