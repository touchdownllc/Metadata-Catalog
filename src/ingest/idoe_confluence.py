"""Harvest IDOE Knowledge Hub Confluence prose into per-element enrichment data.

Source: https://idoe.atlassian.net/wiki/spaces/IKHTV/ — public Ed-Fi data
exchange knowledge hub. Anonymous Confluence REST API works.

Two prose-bearing page-type families:

1. ``{Domain}: Descriptors`` and ``{Domain}: Types`` — per-descriptor tables of
   ``(code_value, [short_description], description)``. Joins to (entity,
   element) via the IDOE ``Enumerations`` column from the API Datastructure
   sheet (descriptor name match). Yields ``definition_text`` for descriptor-
   typed elements.

2. ``{Domain}: General Reporting Info[rmation]`` and ``Reporting Guide:
   {Domain}`` — free-form narrative with overview / business-purpose /
   regulatory-citation prose. Joins to elements via a Confluence-domain →
   API-resource keymap (``_DOMAIN_RESOURCE_KEYMAP``). Yields
   ``business_rules_text``.

Caching: every fetched page is written to
``data/raw/in/confluence/pages/{pageId}.json``. The harvested digest is written
to ``data/raw/in/confluence/harvested.json`` and is what ``indiana.run()``
reads at ingest time.

Entry point: ``harvest()`` (or run ``poc3 ingest in`` — the IN adapter
delegates to ``ensure_harvested()``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

CONFLUENCE_BASE = "https://idoe.atlassian.net"
SPACE_KEY = "IKHTV"
API_V1_CONTENT = f"{CONFLUENCE_BASE}/wiki/rest/api/content"
API_V1_SPACE_PAGES = f"{CONFLUENCE_BASE}/wiki/rest/api/space/{SPACE_KEY}/content/page"

# Page-title patterns we harvest. Anything not matching is skipped — keeps the
# fetched corpus narrow and the regen cycle cheap.
_DESCRIPTOR_TITLE_RE = re.compile(r":\s*(Descriptors|Types)$", re.IGNORECASE)
_NARRATIVE_TITLE_RE = re.compile(
    r"(?:: General Reporting Info(?:rmation)?$|^Reporting Guide:\s)",
    re.IGNORECASE,
)

# Confluence Knowledge Hub uses a "Domain Name" prefix on each prose page
# (e.g. "Attendance: Descriptors"). Map those domain names to the IDOE API
# resources that the prose narrative covers, so the per-resource business-
# rules backfill in ``indiana.py`` can find the right narrative.
#
# Many-to-many is normal: ``Calendar`` covers calendars + calendarDates +
# gradingPeriods because the Reporting Guide blends them. Resource names use
# the camelCase form that appears in the IDOE API Datastructure sheet's
# ``API Resource`` column.
_DOMAIN_RESOURCE_KEYMAP: dict[str, tuple[str, ...]] = {
    "Attendance": ("studentSchoolAttendanceEvents",),
    "Calendar": ("calendars", "calendarDates"),
    "Cohorts (Grouping)": ("StudentCohortAssociation",),
    "Curricular Materials Assistance": (
        "studentCurricularMaterialProgramAssociations",
    ),
    "Membership": (
        "studentEducationOrganizationResponsibilityAssociation",
    ),
    "Pupil Enrollment": (
        "studentEducationOrganizationResponsibilityAssociation",
    ),
    "Enrollment": ("studentSchoolAssociations",),
    "Student Demographics": (
        "studentEducationOrganizationAssociations",
        "students",
        "parents",
        "studentParentAssociations",
    ),
    "Discipline": (
        "disciplineActions",
        "disciplineIncidents",
        "disciplineincidents",
        "studentDisciplineIncidentBehaviorAssociations",
    ),
    "Student Academic Record (Course Outcomes)": (
        "studentSectionAssociations",
        "courseTranscripts",
        "studentAcademicRecords",
        "sections",
        "sessions",
        "gradingPeriods",
        "courseOfferings",
        "courses",
    ),
    "Staff Assignment": (
        "staffEducationOrganizationAssignmentAssociation",
        "staffs",
    ),
    "Staff Contact": ("staffEducationOrganizationContactAssociation",),
    "Staff Employment": (
        "staffEducationOrganizationEmploymentAssociation",
    ),
    "Staff Injury": ("staffEducationOrganizationEmploymentAssociation",),
    "Staff Other Personnel": ("educationOrganizationOtherPersonnels",),
    "Staff Section": ("staffSectionAssociation",),
    "Special Education": ("studentSpecialEducationProgramAssociations",),
    "Special Education Evaluation": (
        "studentSpecialEducationProgramAssociations",
    ),
    "Special Education Termination": (
        "studentSpecialEducationProgramAssociations",
    ),
    "Title I": ("studentTitleIPartAProgramAssociations",),
    "Alternative Education": (
        "studentAlternativeEducationProgramAssociations",
    ),
    "Homebound and Hospitalized": ("studentProgramAssociations",),
    "Additional Student Programs": (
        "studentProgramAssociations",
        "programs",
        "StudentCTEProgramAssociations",
    ),
    "Multilingual Learners": ("studentProgramAssociations",),
    "Student Accommodations": (
        "studentEducationOrganizationAssessmentAccommodations",
        "assessmentAccommodations",
        "Surveys",
        "SurveyQuestions",
        "surveyQuestionResponses",
        "SurveyResponse",
        "surveyResponseEducationOrganizationTargetAssociations",
    ),
    "Graduate": ("studentAcademicRecords",),
}

# Resources that aren't covered by any IDOE domain narrative — listed here so
# the keymap-coverage smoke test in tests can assert intentional omission
# rather than silently failing. ``schools`` / ``localEducationAgencies`` in
# particular are EdOrg foundation rows that IDOE doesn't author per-domain
# prose for; the elements still get descriptor-level prose for descriptor-
# typed columns.
_NO_NARRATIVE_RESOURCES = frozenset({
    "schools",
    "localEducationAgencies",
    "Calendars",  # capitalized variant for state Calendar Events sub-collection
    "postSecondaryInstitutions",
    "communityProviders",
})


# ---------------------------------------------------------------------------
# Confluence client
# ---------------------------------------------------------------------------


def _new_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": "Mozilla/5.0 (POC-3 IDOE harvester)"},
        timeout=30.0,
        follow_redirects=True,
    )


def list_space_pages(client: httpx.Client) -> list[dict]:
    """Return all page stubs in the IKHTV space (id, title)."""
    pages: list[dict] = []
    start = 0
    limit = 200
    while True:
        resp = client.get(API_V1_SPACE_PAGES, params={"limit": limit, "start": start})
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or data.get("page", {}).get("results", []) or []
        if not results:
            break
        for r in results:
            pages.append({"id": r["id"], "title": r["title"]})
        size = data.get("size", len(results))
        if size < limit:
            break
        start += size
        time.sleep(0.2)
    return pages


def fetch_page_body(client: httpx.Client, page_id: str) -> dict:
    """Fetch a single page with storage-format body."""
    url = f"{API_V1_CONTENT}/{page_id}"
    resp = client.get(url, params={"expand": "body.storage"})
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Page filtering
# ---------------------------------------------------------------------------


def is_descriptor_page(title: str) -> bool:
    return bool(_DESCRIPTOR_TITLE_RE.search(title))


def is_narrative_page(title: str) -> bool:
    return bool(_NARRATIVE_TITLE_RE.search(title))


def page_domain(title: str) -> str | None:
    """Extract the domain prefix from a page title.

    ``"Calendar: Descriptors"`` → ``"Calendar"``.
    ``"Reporting Guide: Membership"`` → ``"Membership"``.
    ``"Enrollment: General Reporting Info"`` → ``"Enrollment"``.
    """
    if title.startswith("Reporting Guide:"):
        return title[len("Reporting Guide:"):].strip()
    if ":" in title:
        return title.split(":", 1)[0].strip()
    return None


# ---------------------------------------------------------------------------
# Body parsing
# ---------------------------------------------------------------------------


_SKIP_MACROS = {
    "appanvil-karma-designer",  # The header / banner layout JSON blob.
    "html-canvas",
    "iframe",
}


def _clean_storage_xml(body_xml: str) -> BeautifulSoup:
    """Parse Confluence storage XML and drop layout-macro JSON blobs.

    Wraps the body in a synthetic root element so BeautifulSoup parses the
    Confluence ``ac:`` namespaced tags as regular tags. Drops Karma-designer
    macros (the ones that embed long JSON parameters) so subsequent text-
    extraction doesn't pick up the JSON noise.
    """
    wrapped = (
        '<root xmlns:ac="ac" xmlns:ri="ri">' + body_xml + "</root>"
    )
    soup = BeautifulSoup(wrapped, "html.parser")
    for macro in soup.find_all("ac:structured-macro"):
        name = macro.get("ac:name", "")
        if name in _SKIP_MACROS:
            macro.decompose()
    return soup


_DESCRIPTOR_NAME_RE = re.compile(
    r"^[A-Z][A-Za-z0-9 ]*Descriptors?$"
)


def canonical_descriptor_key(name: str) -> str:
    """Canonicalize a descriptor name for lookup.

    Confluence labels descriptors in three flavours that all need to collide
    on a single key for matching against the XLSX ``Enumerations`` column:

    - ``CalendarEventDescriptor`` (PascalCase singular — the XLSX form)
    - ``Calendar Type Descriptors`` (spaced plural — Confluence heading form)
    - ``calendar event descriptor`` (any-case fallback)

    Strategy: lowercase, drop non-alphanumerics, strip a trailing ``s`` after
    ``descriptor``.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "", name.lower())
    if cleaned.endswith("descriptors"):
        cleaned = cleaned[:-1]  # plural → singular
    return cleaned


@dataclass
class DescriptorTable:
    descriptor_name: str
    rows: list[dict[str, str]] = field(default_factory=list)


def _heading_text_above(table: Tag) -> str | None:
    """Walk back from a table to find the nearest preceding text that names a
    descriptor (e.g. ``CalendarTypeDescriptors``).

    Confluence storage XML places descriptor labels in any of: an ``<h2>`` /
    ``<h3>`` / ``<strong>`` / a leading paragraph just before the table. We
    walk previous siblings of the table (and its ancestors) until we find a
    text node that matches the descriptor pattern.
    """
    candidates: list[Tag] = []
    cur = table
    # Walk up at most 3 ancestors and gather their preceding siblings
    for _ in range(4):
        if cur is None:
            break
        candidates.extend(cur.find_all_previous(
            ["h1", "h2", "h3", "h4", "p", "strong", "b"], limit=20,
        ))
        cur = cur.parent
    for cand in candidates:
        text = cand.get_text(strip=True)
        if not text:
            continue
        # Strip non-word noise (e.g. trailing emoji, parens)
        m = re.search(r"([A-Za-z][A-Za-z0-9 ]*Descriptors?)", text)
        if m:
            normalized = m.group(1).strip()
            # Single-word "Descriptor"/"Descriptors" bare label is too generic
            if normalized.lower() in ("descriptor", "descriptors"):
                continue
            return normalized
    return None


def _row_cells(row: Tag) -> list[str]:
    cells = row.find_all(["td", "th"])
    out: list[str] = []
    for c in cells:
        text = c.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        out.append(text)
    return out


def extract_descriptor_tables(soup: BeautifulSoup) -> list[DescriptorTable]:
    """Walk all tables in a Descriptors page and emit per-descriptor rows.

    A table qualifies if its preceding heading names a descriptor, OR its
    header row contains both a code-value column and a description column.
    """
    out: list[DescriptorTable] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_cells = _row_cells(rows[0])
        header_text = " ".join(header_cells).lower()
        # Must look like a descriptor table (code/value + description columns)
        # OR have a descriptor-named header above it.
        looks_like_descriptor = (
            ("code" in header_text or "value" in header_text or "short description" in header_text)
            and ("description" in header_text or "definition" in header_text or "uses" in header_text)
        )
        descriptor_name = _heading_text_above(table)
        if not (looks_like_descriptor or descriptor_name):
            continue
        # Map header columns to canonical roles
        col_map: dict[str, int] = {}
        for i, h in enumerate(header_cells):
            hl = h.lower()
            if "code" in hl or hl in ("value", "values", "code values", "code value"):
                col_map.setdefault("code", i)
            elif "short description" in hl:
                col_map.setdefault("short", i)
            elif "description" in hl or "definition" in hl or "uses" in hl:
                col_map.setdefault("desc", i)
        if "desc" not in col_map and len(header_cells) >= 2:
            # Fallback: treat last column as the description.
            col_map["desc"] = len(header_cells) - 1
        if "code" not in col_map and len(header_cells) >= 1:
            col_map["code"] = 0

        if not descriptor_name:
            descriptor_name = "UNKNOWN_DESCRIPTOR"

        table_rec = DescriptorTable(descriptor_name=descriptor_name)
        seen_codes: set[str] = set()
        for row in rows[1:]:
            cells = _row_cells(row)
            if not cells:
                continue
            code = cells[col_map["code"]] if col_map.get("code") is not None and col_map["code"] < len(cells) else ""
            desc = cells[col_map["desc"]] if col_map.get("desc") is not None and col_map["desc"] < len(cells) else ""
            short = cells[col_map["short"]] if col_map.get("short") is not None and col_map["short"] < len(cells) else ""
            code = code.strip()
            desc = desc.strip()
            short = short.strip()
            if not code:
                continue
            if code in seen_codes:
                continue
            seen_codes.add(code)
            table_rec.rows.append({
                "code_value": code,
                "short_description": short,
                "description": desc,
            })
        if table_rec.rows:
            out.append(table_rec)
    return out


_NARRATIVE_SKIP_TOKENS = (
    "click here to view",
    "was this documentation helpful",
    "submit a ticket",
    "powered by",
    "feedback",
    "did this documentation help",
    "submit feedback",
)


def extract_narrative(soup: BeautifulSoup) -> str:
    """Pull free-form narrative paragraphs / list items from a page.

    Used for ``Reporting Guide:`` and ``: General Reporting Info`` pages, where
    the prose explains business purpose / regulatory citation / scope.
    """
    parts: list[str] = []
    for el in soup.find_all(["p", "li", "blockquote"]):
        if el.find_parent("table"):
            continue
        text = el.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        if len(text) < 20:
            continue
        lower = text.lower()
        if any(skip in lower for skip in _NARRATIVE_SKIP_TOKENS):
            continue
        # Skip the residual Karma JSON ("name":"page" / "templateId":"..."),
        # which sneaks in via paragraphs that hold layout config.
        if text.count('":"') > 2 or text.count('","') > 4:
            continue
        parts.append(text)
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# Cache + harvest orchestration
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    """``data/raw/in/confluence/`` — gitignored under ``data/raw/``."""
    project_root = Path(__file__).resolve().parents[2]
    return project_root / "data" / "raw" / "in" / "confluence"


def _pages_cache_dir() -> Path:
    return _cache_dir() / "pages"


def _list_cache_path() -> Path:
    return _cache_dir() / "page_index.json"


def _digest_path() -> Path:
    return _cache_dir() / "harvested.json"


def _load_or_fetch_page(client: httpx.Client, page_id: str) -> dict:
    pages_dir = _pages_cache_dir()
    cached = pages_dir / f"{page_id}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))
    data = fetch_page_body(client, page_id)
    pages_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(data), encoding="utf-8")
    return data


def _load_or_fetch_index(client: httpx.Client) -> list[dict]:
    cache = _list_cache_path()
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    pages = list_space_pages(client)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(pages, indent=2), encoding="utf-8")
    return pages


def harvest(force: bool = False) -> dict:
    """Run the full Confluence harvest. Returns the digest dict and writes it
    to ``data/raw/in/confluence/harvested.json``.

    Subsequent calls are cache-served unless ``force=True``.
    """
    digest_path = _digest_path()
    if digest_path.exists() and not force:
        return json.loads(digest_path.read_text(encoding="utf-8"))

    descriptors: dict[str, dict[str, str]] = {}
    descriptor_short: dict[str, dict[str, str]] = {}
    domain_narratives: dict[str, str] = {}
    descriptor_pages: list[str] = []
    narrative_pages: list[str] = []

    with _new_client() as client:
        pages = _load_or_fetch_index(client)
        logger.info("IDOE Confluence: %d pages in space %s", len(pages), SPACE_KEY)

        # Filter to prose-bearing titles
        descriptor_titles = [p for p in pages if is_descriptor_page(p["title"])]
        narrative_titles = [p for p in pages if is_narrative_page(p["title"])]
        logger.info(
            "  %d descriptor pages + %d narrative pages will be harvested",
            len(descriptor_titles), len(narrative_titles),
        )

        for p in descriptor_titles:
            try:
                page = _load_or_fetch_page(client, p["id"])
            except Exception as exc:
                logger.warning("Failed to fetch %s (%s): %s", p["title"], p["id"], exc)
                continue
            body_xml = page.get("body", {}).get("storage", {}).get("value", "")
            if not body_xml:
                continue
            descriptor_pages.append(p["title"])
            soup = _clean_storage_xml(body_xml)
            for table in extract_descriptor_tables(soup):
                key = canonical_descriptor_key(table.descriptor_name)
                if not key or not table.rows:
                    continue
                bucket = descriptors.setdefault(key, {})
                short_bucket = descriptor_short.setdefault(key, {})
                for row in table.rows:
                    code = row["code_value"]
                    desc = row["description"]
                    short = row["short_description"]
                    if desc and code not in bucket:
                        bucket[code] = desc
                    if short and code not in short_bucket:
                        short_bucket[code] = short
            time.sleep(0.15)

        for p in narrative_titles:
            try:
                page = _load_or_fetch_page(client, p["id"])
            except Exception as exc:
                logger.warning("Failed to fetch %s (%s): %s", p["title"], p["id"], exc)
                continue
            body_xml = page.get("body", {}).get("storage", {}).get("value", "")
            if not body_xml:
                continue
            narrative_pages.append(p["title"])
            soup = _clean_storage_xml(body_xml)
            text = extract_narrative(soup)
            if not text:
                continue
            domain = page_domain(p["title"])
            if not domain:
                continue
            # Concatenate narratives across pages for the same domain (Reporting
            # Guide + General Reporting Info both contribute).
            existing = domain_narratives.get(domain)
            if existing:
                domain_narratives[domain] = existing + "\n\n" + text
            else:
                domain_narratives[domain] = text

    digest = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_url": f"{CONFLUENCE_BASE}/wiki/spaces/{SPACE_KEY}/",
        "page_count": len(descriptor_pages) + len(narrative_pages),
        "descriptor_pages": sorted(descriptor_pages),
        "narrative_pages": sorted(narrative_pages),
        "descriptors": descriptors,
        "descriptor_short": descriptor_short,
        "domain_narratives": domain_narratives,
        "domain_resource_keymap": _DOMAIN_RESOURCE_KEYMAP,
    }
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(json.dumps(digest, indent=2, sort_keys=True), encoding="utf-8")
    logger.info(
        "Harvested IDOE Confluence: %d descriptors, %d domain narratives → %s",
        len(descriptors), len(domain_narratives), digest_path,
    )
    return digest


def ensure_harvested(force: bool = False) -> dict:
    """Public alias for ``harvest`` — used by ``indiana.run()`` at ingest time."""
    return harvest(force=force)


# ---------------------------------------------------------------------------
# Per-element prose lookup helpers (consumed by indiana.py)
# ---------------------------------------------------------------------------


def descriptor_definition(
    digest: dict,
    descriptor_name: str | None,
    *,
    code_value: str | None = None,
) -> str:
    """Return ``definition_text`` for an element backed by a descriptor.

    If ``code_value`` is supplied and matches, return that single value's
    description. Otherwise return a digest of all known code values for the
    descriptor (one per line, ``"<code>: <description>"``), capped at the
    first ~12 entries to keep the field readable.
    """
    if not descriptor_name:
        return ""
    descriptors_map = digest.get("descriptors") or {}
    bucket = descriptors_map.get(canonical_descriptor_key(descriptor_name)) or {}
    if not bucket:
        return ""
    if code_value and code_value in bucket:
        return bucket[code_value]
    # Render up to 12 code-value definitions; if more, append "(...)" footer.
    items = list(bucket.items())
    rendered = "\n".join(f"{c}: {d}" for c, d in items[:12])
    if len(items) > 12:
        rendered += f"\n(+{len(items) - 12} additional values)"
    return f"Descriptor {descriptor_name} permits:\n{rendered}"


def domain_business_rules(digest: dict, api_resource: str | None) -> str | None:
    """Return ``business_rules_text`` for an element under ``api_resource``.

    Looks up the Confluence-domain → API-resource keymap in reverse: which
    domain narrative covers this resource? Returns the first match (the
    keymap is roughly 1:1 from resource to domain).
    """
    if not api_resource:
        return None
    keymap = digest.get("domain_resource_keymap") or _DOMAIN_RESOURCE_KEYMAP
    narratives = digest.get("domain_narratives") or {}
    for domain, resources in keymap.items():
        if api_resource in resources and domain in narratives:
            return narratives[domain]
    return None
