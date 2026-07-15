"""TWEDS (Texas Web-Enabled Data Standards) scraper for TX enrichment.

TEA publishes the TEDS through TWEDS at
`https://tealprod.tea.state.tx.us/TWEDSAPI/{version}/0/0/...` — a server-
rendered .NET app (not a JSON API despite the URL prefix). Two pages carry
what POC-3 needs:

1. `/DataComponents/Entity/List` — plain-HTML table of ~60 entities.
2. `/DataComponents` — embeds a JSON secondary-nav blob in a `jsonStr` JS
   variable; the Data Elements branch enumerates ~360 elements.

Entity detail pages load content via JS tabs (needs Playwright);
element detail pages are static HTML (httpx suffices).

Reused from the mature scraper at ``nachos-ai-poc-claude/src/ingest/tweds.py``
without the decomposition / DR-filter / catalog-supplement plumbing — POC-3
is ingestion-only so those scoring-prep layers are out of scope.

CRITICAL: all public functions are plain (NOT `@click.command`); they
return values to be consumed by `texas.run()`. See
`tests/test_ingest_tx.py::TestCliWiring`.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

TWEDS_BASE = "https://tealprod.tea.state.tx.us"
# Bump when TEA ships a new TEDS version. Reflected in meta.json so
# downstream consumers can see exactly what was scraped.
TWEDS_VERSION = 33
TWEDS_VERSION_PATH = f"/TWEDSAPI/{TWEDS_VERSION}/0/0"

DATA_COMPONENTS_URL = f"{TWEDS_BASE}{TWEDS_VERSION_PATH}/DataComponents"
ENTITY_LIST_URL = f"{TWEDS_BASE}{TWEDS_VERSION_PATH}/DataComponents/Entity/List"

_ELEMENT_ID_RE = re.compile(r"/DataComponents/DataElements/List/(\d+)")


# -- Link indexes ────────────────────────────────────────────────────────────


def _fetch(client: httpx.Client, url: str) -> str:
    resp = client.get(url, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _extract_entity_links(page_html: str) -> list[dict]:
    """Parse /DataComponents/Entity/List into [{id, name, url}, ...]."""
    soup = BeautifulSoup(page_html, "lxml")
    seen: dict[str, dict] = {}
    href_prefix = f"{TWEDS_VERSION_PATH}/DataComponents/Entity/List/"
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href.startswith(href_prefix):
            continue
        ent_id = href[len(href_prefix):].split("/")[0]
        if not ent_id.isdigit() or ent_id in seen:
            continue
        name = anchor.get_text(strip=True)
        if not name:
            continue
        seen[ent_id] = {"id": ent_id, "name": name, "url": TWEDS_BASE + href}
    if not seen:
        raise RuntimeError(
            f"No entity rows parsed from {ENTITY_LIST_URL}; page structure may have changed."
        )
    return sorted(seen.values(), key=lambda e: e["name"].lower())


def _extract_element_links(page_html: str) -> list[dict]:
    """Pull the Data Elements branch from /DataComponents secondary-nav tree."""
    match = re.search(r'jsonStr\s*=\s*"(.+?)"\s*;', page_html, re.DOTALL)
    if not match:
        raise RuntimeError(
            f"Secondary-navigation tree JSON not found in {DATA_COMPONENTS_URL}."
        )
    tree = json.loads(html.unescape(match.group(1).replace("&quot;", '"')))

    def walk(node: dict) -> dict | None:
        if "/DataComponents/DataElements/DataElementsList" in (node.get("LinkURL") or ""):
            return node
        for child in node.get("Children") or []:
            hit = walk(child)
            if hit:
                return hit
        return None

    de_node = walk(tree)
    if not de_node:
        raise RuntimeError("Data Elements node not found in secondary-nav tree.")

    seen: dict[str, dict] = {}
    for child in de_node.get("Children") or []:
        url = child.get("LinkURL") or ""
        m = _ELEMENT_ID_RE.search(url)
        if not m:
            continue
        elem_id = m.group(1)
        if elem_id in seen:
            continue
        seen[elem_id] = {
            "id": elem_id,
            "name": (child.get("Title") or "").strip(),
            "url": TWEDS_BASE + url,
        }
    if not seen:
        raise RuntimeError("Data Elements branch was empty in secondary-nav tree.")
    return sorted(seen.values(), key=lambda e: int(e["id"]))


def fetch_link_indexes(cache_dir: Path) -> tuple[list[dict], list[dict]]:
    """Return (entity_links, element_links) from TWEDS, caching to disk."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    entity_path = cache_dir / "entity_links.json"
    element_path = cache_dir / "element_links.json"

    if entity_path.exists() and element_path.exists():
        logger.info("Using cached TWEDS link indexes in %s", cache_dir)
        return (
            json.loads(entity_path.read_text(encoding="utf-8")),
            json.loads(element_path.read_text(encoding="utf-8")),
        )

    with httpx.Client(timeout=30.0) as client:
        logger.info("Fetching TWEDS entity list: %s", ENTITY_LIST_URL)
        entity_links = _extract_entity_links(_fetch(client, ENTITY_LIST_URL))
        logger.info("Found %d entities", len(entity_links))
        logger.info("Fetching TWEDS element tree: %s", DATA_COMPONENTS_URL)
        element_links = _extract_element_links(_fetch(client, DATA_COMPONENTS_URL))
        logger.info("Found %d data elements", len(element_links))

    entity_path.write_text(json.dumps(entity_links, indent=2), encoding="utf-8")
    element_path.write_text(json.dumps(element_links, indent=2), encoding="utf-8")
    return entity_links, element_links


# -- Element detail (httpx) ──────────────────────────────────────────────────


def fetch_element_page(client: httpx.Client, element_id: str) -> dict | None:
    """Parse a single element detail page. Returns dict or None on HTTP error."""
    url = f"{TWEDS_BASE}{TWEDS_VERSION_PATH}/DataComponents/DataElements/List/{element_id}"
    try:
        resp = client.get(url, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch element %s: %s", element_id, e)
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    return parse_element_page(soup, element_id=element_id, url=url)


def parse_element_page(
    soup: BeautifulSoup, *, element_id: str | None = None, url: str | None = None
) -> dict:
    """Parse element-detail HTML into a dict.

    Split out of `fetch_element_page` so tests can feed a synthetic
    BeautifulSoup tree without touching the network.
    """
    result: dict = {}
    if element_id:
        result["tweds_id"] = element_id
    if url:
        result["url"] = url

    for tbody in soup.find_all("tbody"):
        rows = tbody.find_all("tr")
        if not rows:
            continue
        header = rows[0].get_text(strip=True)

        if header.startswith("Data Element ID"):
            if len(rows) > 1:
                cells = rows[1].find_all("td")
                if len(cells) >= 4:
                    result["ecode"] = cells[0].get_text(strip=True)
                    result["name"] = cells[1].get_text(strip=True)
                    result["date_issued"] = cells[2].get_text(strip=True)
                    result["date_updated"] = cells[3].get_text(strip=True)

        elif header == "Definition":
            if len(rows) > 1:
                result["definition"] = rows[1].get_text(" ", strip=True)

        elif header == "Special Instructions":
            if len(rows) > 1:
                text = rows[1].get_text(strip=True)
                result["special_instructions"] = text if text else None

        elif header.startswith("Table Identification"):
            if len(rows) > 1:
                cells = rows[1].find_all("td")
                if len(cells) >= 4:
                    result["descriptor_table"] = cells[0].get_text(strip=True) or None
                    result["length"] = cells[1].get_text(strip=True) or None
                    result["data_type"] = cells[2].get_text(strip=True) or None
                    result["domain_of_values"] = cells[3].get_text(strip=True) or None

        elif header == "Used in Entities":
            if len(rows) > 1:
                links = rows[1].find_all("a")
                result["entities"] = [
                    a.get_text(strip=True) for a in links if a.get_text(strip=True)
                ]

        elif header == "Used in Domain":
            if len(rows) > 1:
                links = rows[1].find_all("a")
                result["domains"] = [
                    a.get_text(strip=True) for a in links if a.get_text(strip=True)
                ]

        elif header == "Former Data Element Name":
            if len(rows) > 1:
                text = rows[1].get_text(strip=True)
                result["former_name"] = text if text else None

        elif header.startswith("Used in Data Collections"):
            if len(rows) > 1:
                result["collections_text"] = rows[1].get_text(" ", strip=True)

    return result


def scrape_elements(
    element_links: list[dict], cache_dir: Path, delay: float = 0.5
) -> list[dict]:
    """Scrape all element detail pages.

    Uses cache if present AND complete (one record per link). A stale
    cache from a prior incomplete scrape is refused so downstream
    artifacts never encode a silently-truncated TEDS snapshot —
    intermittent server disconnects during a multi-minute scrape are
    common enough that the caller needs loud failure, not a cache that
    looks valid but is missing pages.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "elements_scraped.json"
    expected_ids = {link["id"] for link in element_links}
    if cache_file.exists():
        cached: list[dict] = json.loads(cache_file.read_text(encoding="utf-8"))
        cached_ids = {r.get("tweds_id") for r in cached if r.get("tweds_id")}
        missing = expected_ids - cached_ids
        if not missing:
            logger.info("Using cached element detail data from %s", cache_file)
            return cached
        logger.warning(
            "Element cache at %s missing %d of %d pages — re-scraping the "
            "gap. (Prior scrape likely hit a server disconnect.)",
            cache_file, len(missing), len(expected_ids),
        )
        results = list(cached)
        to_fetch = [link for link in element_links if link["id"] in missing]
    else:
        logger.info("Scraping %d TWEDS element detail pages...", len(element_links))
        results = []
        to_fetch = list(element_links)

    with httpx.Client(timeout=30.0) as client:
        for i, link in enumerate(to_fetch):
            data = fetch_element_page(client, link["id"])
            if data:
                results.append(data)
            if (i + 1) % 50 == 0:
                logger.info("  Progress: %d / %d", i + 1, len(to_fetch))
            time.sleep(delay)

    # Completeness guard: refuse to cache a partial scrape. One retry
    # pass for any IDs that still didn't land — covers transient
    # disconnects without masking a real site change.
    scraped_ids = {r.get("tweds_id") for r in results if r.get("tweds_id")}
    still_missing = expected_ids - scraped_ids
    if still_missing:
        logger.warning(
            "%d element page(s) missing after first pass — retrying once.",
            len(still_missing),
        )
        retry_links = [link for link in element_links if link["id"] in still_missing]
        with httpx.Client(timeout=30.0) as client:
            for link in retry_links:
                data = fetch_element_page(client, link["id"])
                if data:
                    results.append(data)
                time.sleep(delay * 2)  # back off harder on retry

    scraped_ids = {r.get("tweds_id") for r in results if r.get("tweds_id")}
    still_missing = expected_ids - scraped_ids
    if still_missing:
        sample = sorted(still_missing)[:10]
        raise RuntimeError(
            f"TWEDS scrape incomplete after retry: {len(still_missing)} of "
            f"{len(expected_ids)} element pages failed to load "
            f"(first 10 IDs: {sample}). Cache NOT written — re-run "
            f"`poc3 ingest tx` to resume, or delete "
            f"{cache_file} to start fresh."
        )

    cache_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    logger.info("Saved %d element records to %s", len(results), cache_file)
    _warn_if_parse_drift(results)
    return results


def _warn_if_parse_drift(elements: list[dict]) -> None:
    """Smoke-check that the parser is still getting definitions out.

    If TWEDS shifts table column order in a future release, the tbody
    parser silently returns None for affected fields. Log a loud warning
    when fewer than 80% of pages have a `definition` so regressions surface.
    """
    if not elements:
        return
    has_def = sum(1 for e in elements if e.get("definition"))
    ratio = has_def / len(elements)
    if ratio < 0.80:
        sample = next(
            (e.get("url") for e in elements if not e.get("definition")), "<unknown>"
        )
        logger.warning(
            "TWEDS element parse drift: only %.0f%% of %d pages carry a "
            "definition — first failure at %s. Re-check HTML structure.",
            ratio * 100, len(elements), sample,
        )


# -- Entity detail (Playwright) ──────────────────────────────────────────────


def _fetch_entity_pages_playwright(
    to_fetch: list[dict],
    results: list[dict],
    cache_file: Path,
    delay: float,
) -> None:
    """Network core of :func:`scrape_entities` — appends into
    ``results``, checkpointing to ``cache_file`` every 10 pages. Split
    out so the completeness guard around it is hermetically testable."""
    try:
        from playwright.sync_api import sync_playwright  # lazy import; optional dep
    except ImportError as exc:
        raise ImportError(
            "playwright is required to scrape TX TWEDS entity pages but is "
            "not installed — it is an optional extra (issue #213 item 4). "
            "Install it with: uv pip install -e '.[scrape]' "
            "(then: playwright install chromium)"
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for i, link in enumerate(to_fetch):
            url = link["url"]
            name = link["name"]
            logger.info("  [%d/%d] %s", i + 1, len(to_fetch), name)
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                data = _extract_entity_page(page)
                data["tweds_id"] = link["id"]
                data["url"] = url
                data["sidebar_name"] = name
                results.append(data)
            except Exception as e:
                logger.error("  Failed to scrape %s: %s", name, e)
            if (i + 1) % 10 == 0:
                cache_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
            time.sleep(delay)
        browser.close()


def scrape_entities(
    entity_links: list[dict], cache_dir: Path, delay: float = 2.0
) -> list[dict]:
    """Scrape all entity pages using Playwright.

    Uses cache if present AND complete (one record per link) — the same
    refuse-stale-cache posture as :func:`scrape_elements` (issue #212
    item 5: this function previously had NO completeness check —
    per-entity Playwright failures were logged and skipped, the partial
    result was cached, and every future run short-circuited on
    cache-exists, permanently dropping TEDS entities from the corpus
    with only an old log line as evidence). An incomplete cache
    re-scrapes only the gap; a scrape that stays incomplete after one
    retry pass raises (partial cache kept for resume).

    Playwright is required because TWEDS entity detail pages load their
    tab-panel content via JS (the raw HTML has `Loading...` placeholders).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "entities_scraped.json"
    expected_ids = {link["id"] for link in entity_links}
    if cache_file.exists():
        cached: list[dict] = json.loads(cache_file.read_text(encoding="utf-8"))
        cached_ids = {r.get("tweds_id") for r in cached if r.get("tweds_id")}
        missing = expected_ids - cached_ids
        if not missing:
            logger.info("Using cached entity detail data from %s", cache_file)
            return cached
        logger.warning(
            "Entity cache at %s missing %d of %d pages — re-scraping the "
            "gap. (Prior scrape likely lost pages to per-page Playwright "
            "failures.)",
            cache_file, len(missing), len(expected_ids),
        )
        results = list(cached)
        to_fetch = [link for link in entity_links if link["id"] in missing]
    else:
        logger.info(
            "Scraping %d TWEDS entity pages with Playwright...",
            len(entity_links),
        )
        results = []
        to_fetch = list(entity_links)

    _fetch_entity_pages_playwright(to_fetch, results, cache_file, delay)

    # Completeness guard + one retry pass — mirrors scrape_elements.
    scraped_ids = {r.get("tweds_id") for r in results if r.get("tweds_id")}
    still_missing = expected_ids - scraped_ids
    if still_missing:
        logger.warning(
            "%d entity page(s) missing after first pass — retrying once.",
            len(still_missing),
        )
        retry_links = [
            link for link in entity_links if link["id"] in still_missing
        ]
        _fetch_entity_pages_playwright(
            retry_links, results, cache_file, delay * 2
        )

    scraped_ids = {r.get("tweds_id") for r in results if r.get("tweds_id")}
    still_missing = expected_ids - scraped_ids
    if still_missing:
        # Keep the partial cache — it is the resume checkpoint — but
        # refuse to return it as if complete.
        cache_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
        sample = sorted(still_missing)[:10]
        raise RuntimeError(
            f"TWEDS entity scrape incomplete after retry: "
            f"{len(still_missing)} of {len(expected_ids)} entity pages "
            f"failed to load (first 10 IDs: {sample}). Partial cache "
            f"kept for resume — re-run `poc3 ingest tx`, or delete "
            f"{cache_file} to start fresh."
        )

    cache_file.write_text(json.dumps(results, indent=2), encoding="utf-8")
    logger.info("Saved %d entity records to %s", len(results), cache_file)
    return results


def _extract_entity_page(page) -> dict:
    """Click through tabs to force-load content, then extract via page.evaluate.

    Kept minimal — the JS returns a dict with:
      - entity_name
      - elements: [{name, ecode, length, data_type, descriptor_table, is_sub_entity, href}]
      - entity_description, general_reporting_requirements,
        special_reporting_requirements, data_element_reporting_requirements,
        examples
    """
    page.wait_for_timeout(2000)
    tabs = page.locator('[role="tab"]').all()
    for tab in tabs:
        try:
            tab.click()
            page.wait_for_timeout(500)
        except Exception:
            pass
    page.wait_for_timeout(1500)

    return page.evaluate(
        """() => {
            const result = {};
            const h4s = document.querySelectorAll('h4');
            for (const h4 of h4s) {
                const text = h4.textContent.trim();
                if (text.endsWith('Entity')) {
                    result.entity_name = text.replace(/\\s*Entity$/, '');
                    break;
                }
            }
            const table = document.querySelector('table.tea-table');
            if (table) {
                const tbodies = table.querySelectorAll('tbody');
                result.elements = [];
                for (const tbody of tbodies) {
                    const rows = tbody.querySelectorAll('tr');
                    for (const row of rows) {
                        const cells = row.querySelectorAll('td');
                        if (cells.length >= 8) {
                            const nameCell = cells[0];
                            const link = nameCell.querySelector('a');
                            const nameText = nameCell.textContent.trim();
                            const ecode = cells[7] ? cells[7].textContent.trim() : null;
                            const length = cells[4] ? cells[4].textContent.trim() : null;
                            const dataType = cells[5] ? cells[5].textContent.trim() : null;
                            const descriptorTable = cells[6] ? cells[6].textContent.trim() : null;
                            const isSubEntity = !ecode || ecode === '';
                            result.elements.push({
                                name: nameText.replace(/\\s*\\(may have multiple instances\\)/, ''),
                                ecode: ecode || null,
                                length: length || null,
                                data_type: dataType || null,
                                descriptor_table: descriptorTable || null,
                                is_sub_entity: isSubEntity,
                                href: link ? link.getAttribute('href') : null,
                            });
                        } else if (cells.length === 1) {
                            result.elements.push({
                                name: cells[0].textContent.trim(),
                                ecode: null,
                                is_sub_entity: true,
                            });
                        }
                    }
                }
            }
            const panels = document.querySelectorAll('[role="tabpanel"]');
            const tabNames = ['entity_description', 'general_reporting_requirements',
                              'special_reporting_requirements', 'data_element_reporting_requirements',
                              'examples'];
            panels.forEach((panel, i) => {
                const key = tabNames[i] || `tab_${i}`;
                const parts = [];
                for (const child of panel.children) {
                    const tag = child.tagName.toLowerCase();
                    const text = child.textContent.trim();
                    if (!text) continue;
                    if (tag === 'h3') {
                        parts.push('## ' + text);
                    } else if (tag === 'ul' || tag === 'ol') {
                        const items = child.querySelectorAll('li');
                        items.forEach(li => parts.push('- ' + li.textContent.trim()));
                    } else {
                        parts.push(text);
                    }
                }
                result[key] = parts.join('\\n');
            });
            return result;
        }"""
    )


# -- Post-processing helpers ─────────────────────────────────────────────────

# Regulatory citation patterns found in TWEDS Special Instructions prose.
# Order matters: longer/specific patterns first to avoid partial overlaps.
_CITATION_PATTERNS = [
    re.compile(r"\b34\s*CFR\s*\d+\.\d+(?:\([a-z0-9]+\))*", re.IGNORECASE),
    re.compile(r"\b20\s*U\.?S\.?C\.?\s*§?\s*\d+[a-z]?(?:\([a-z0-9]+\))*", re.IGNORECASE),
    re.compile(r"\bTEC\s*§\s*\d+(?:\.\d+)*", re.IGNORECASE),
    re.compile(r"\bOMB\.?\s*NO\.?:?\s*\d+-\d+", re.IGNORECASE),
    re.compile(r"\bPart\s+[A-Z]\b"),
    re.compile(r"\bChapter\s+\d+\b", re.IGNORECASE),
]


def extract_regulatory_citations(text: str | None) -> list[str]:
    """Extract TEC §, 34 CFR, 20 USC, OMB, Part X, Chapter N citations from prose."""
    if not text:
        return []
    seen: list[str] = []
    for pattern in _CITATION_PATTERNS:
        for match in pattern.finditer(text):
            cit = re.sub(r"\s+", " ", match.group(0)).strip()
            if cit not in seen:
                seen.append(cit)
    return seen


_DESCRIPTOR_CODE_RE = re.compile(r"\(([A-Z]\d+)\)\s*$")


def parse_descriptor_table_code(descriptor_table_text: str | None) -> str | None:
    """`AcademicSubject(C325)` -> `C325`."""
    if not descriptor_table_text:
        return None
    m = _DESCRIPTOR_CODE_RE.search(descriptor_table_text.strip())
    return m.group(1) if m else None


def clean_related_entities(entities: list[str] | None) -> list[str]:
    """Dedupe + collapse whitespace. TWEDS uses `Parent > Child` strings."""
    if not entities:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for e in entities:
        if not e:
            continue
        clean = re.sub(r"\s+", " ", e.strip())
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


# -- Orchestrator ────────────────────────────────────────────────────────────


def load_or_fetch_tweds(
    cache_dir: Path, *, offline_ok: bool = True
) -> tuple[list[dict], list[dict]] | None:
    """Return (entities, elements) from TWEDS.

    Cached scrapes are validated for COMPLETENESS against the link
    indexes before being trusted (issue #212 item 5): the previous
    fast-path loaded ``elements_scraped.json`` / ``entities_scraped.json``
    directly, bypassing the scrapers' expected-vs-cached ID guards — an
    incomplete cache from an interrupted run loaded silently forever.
    Both loads now route through :func:`scrape_elements` /
    :func:`scrape_entities`, which short-circuit at $0/no-network on a
    complete cache, re-scrape only the gap on an incomplete one, and
    raise when the gap cannot be closed.

    Returns None when the corpus cannot be completed and ``offline_ok``
    (no cache / network down / Playwright missing) — lets the caller
    fall back to the spine-only baseline, with a warning.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)

    entity_links_path = cache_dir / "entity_links.json"
    element_links_path = cache_dir / "element_links.json"

    if entity_links_path.exists() and element_links_path.exists():
        entity_links = json.loads(entity_links_path.read_text(encoding="utf-8"))
        element_links = json.loads(element_links_path.read_text(encoding="utf-8"))
    else:
        try:
            entity_links, element_links = fetch_link_indexes(cache_dir)
        except Exception as e:
            logger.warning("TWEDS link fetch failed: %s", e)
            if offline_ok:
                return None
            raise

    elements: list[dict]
    try:
        elements = scrape_elements(element_links, cache_dir)
    except Exception as e:
        logger.warning("TWEDS element scrape failed: %s", e)
        if offline_ok:
            return None
        raise

    entities: list[dict]
    try:
        entities = scrape_entities(entity_links, cache_dir)
    except Exception as e:
        logger.warning(
            "TWEDS entity scrape failed (Playwright not installed?): %s", e
        )
        if offline_ok:
            return None
        raise

    logger.info(
        "Loaded TWEDS: %d entities, %d elements (cache_dir=%s)",
        len(entities), len(elements), cache_dir,
    )
    return entities, elements
