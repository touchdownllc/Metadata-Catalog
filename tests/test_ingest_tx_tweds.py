"""Tests for the TWEDS scraper + parser helpers.

Covers:
- Link-index parsers (entity-list HTML + secondary-nav jsonStr).
- Element-detail HTML parser (tbody structure).
- Helpers: regulatory citations, descriptor code, related-entities cleanup.

Under Option B, TWEDS IS the authoritative record source for TX, not an
enrichment overlay. End-to-end ingestion tests (record generation,
attribution, gap-log schema) live in ``test_ingest_tx.py``; this file
focuses on the raw scraper/parser surface.
"""

from __future__ import annotations

import html
import inspect
import json

import pytest
from bs4 import BeautifulSoup

from src.ingest import tx_tweds


# -- Link-index parsing ──────────────────────────────────────────────────────


class TestEntityLinkExtraction:
    def test_parses_anchor_rows(self):
        page_html = f"""
            <html><body>
              <a href="{tx_tweds.TWEDS_VERSION_PATH}/DataComponents/Entity/List/1001">
                StudentSchoolAssociation
              </a>
              <a href="{tx_tweds.TWEDS_VERSION_PATH}/DataComponents/Entity/List/1002">
                SchoolCampus
              </a>
              <a href="/unrelated/link">should-skip</a>
            </body></html>
        """
        links = tx_tweds._extract_entity_links(page_html)
        ids = {l["id"] for l in links}
        assert ids == {"1001", "1002"}
        urls = {l["url"] for l in links}
        assert all(u.startswith(tx_tweds.TWEDS_BASE) for u in urls)
        # Sorted alphabetically by name
        assert links[0]["name"] == "SchoolCampus"

    def test_dedupes_repeat_anchors(self):
        href = f"{tx_tweds.TWEDS_VERSION_PATH}/DataComponents/Entity/List/42"
        page_html = f"""
            <html><body>
              <a href="{href}">Calendar</a>
              <a href="{href}/elements">Calendar again (child link)</a>
            </body></html>
        """
        links = tx_tweds._extract_entity_links(page_html)
        assert len(links) == 1
        assert links[0]["id"] == "42"

    def test_raises_on_empty_page(self):
        with pytest.raises(RuntimeError, match="No entity rows parsed"):
            tx_tweds._extract_entity_links("<html><body><p>no anchors</p></body></html>")


class TestElementLinkExtraction:
    def _embed_tree(self, tree: dict) -> str:
        # The real TWEDS page stores the JSON as an HTML-entity-encoded
        # string inside a `jsonStr = "…";` assignment. Mimic that shape.
        return (
            "<html><body><script>var jsonStr = \""
            + html.escape(json.dumps(tree)).replace('"', "&quot;")
            + "\";</script></body></html>"
        )

    def test_parses_data_elements_branch(self):
        tree = {
            "Title": "Root",
            "LinkURL": "",
            "Children": [
                {
                    "Title": "Data Elements",
                    "LinkURL": f"{tx_tweds.TWEDS_VERSION_PATH}/DataComponents/DataElements/DataElementsList",
                    "Children": [
                        {
                            "Title": "AbsenceEventCategory",
                            "LinkURL": f"{tx_tweds.TWEDS_VERSION_PATH}/DataComponents/DataElements/List/9001",
                        },
                        {
                            "Title": "ActualHoursTaught",
                            "LinkURL": f"{tx_tweds.TWEDS_VERSION_PATH}/DataComponents/DataElements/List/9002",
                        },
                    ],
                },
            ],
        }
        links = tx_tweds._extract_element_links(self._embed_tree(tree))
        assert [l["id"] for l in links] == ["9001", "9002"]
        assert [l["name"] for l in links] == ["AbsenceEventCategory", "ActualHoursTaught"]
        assert all(l["url"].startswith(tx_tweds.TWEDS_BASE) for l in links)

    def test_raises_when_jsonstr_missing(self):
        with pytest.raises(RuntimeError, match="Secondary-navigation tree JSON"):
            tx_tweds._extract_element_links("<html></html>")


# -- Element-detail parsing ──────────────────────────────────────────────────


ELEMENT_PAGE_FIXTURE = """
<html><body>
  <table>
    <tbody>
      <tr><td>Data Element ID / Data Element Name / Date Issued / Date Updated</td></tr>
      <tr>
        <td>E1234</td>
        <td>CalendarCode</td>
        <td>2021-01-01</td>
        <td>2024-06-15</td>
      </tr>
    </tbody>
    <tbody>
      <tr><td>Definition</td></tr>
      <tr><td>The identifier for the calendar. Must be unique within school and year per 19 TAC § 129.1025.</td></tr>
    </tbody>
    <tbody>
      <tr><td>Special Instructions</td></tr>
      <tr><td>See 34 CFR 300.601 and TEC § 25.081 for attendance calendar rules.</td></tr>
    </tbody>
    <tbody>
      <tr><td>Table Identification / Length / Data Type / Domain Of Values</td></tr>
      <tr>
        <td>CalendarType(C041)</td>
        <td>10</td>
        <td>character</td>
        <td>values...</td>
      </tr>
    </tbody>
    <tbody>
      <tr><td>Used in Entities</td></tr>
      <tr><td><a href="#">Calendar</a> <a href="#">Calendar &gt; CalendarDate</a></td></tr>
    </tbody>
    <tbody>
      <tr><td>Used in Data Collections</td></tr>
      <tr><td>PEIMS Summer</td></tr>
    </tbody>
  </table>
</body></html>
"""


class TestElementDetailParser:
    def test_parses_all_expected_fields(self):
        soup = BeautifulSoup(ELEMENT_PAGE_FIXTURE, "lxml")
        data = tx_tweds.parse_element_page(soup, element_id="1234", url="http://x")
        assert data["tweds_id"] == "1234"
        assert data["ecode"] == "E1234"
        assert data["name"] == "CalendarCode"
        assert "identifier for the calendar" in data["definition"]
        assert "34 CFR 300.601" in data["special_instructions"]
        assert data["descriptor_table"] == "CalendarType(C041)"
        assert data["length"] == "10"
        assert data["data_type"] == "character"
        assert "Calendar" in data["entities"]
        assert data["collections_text"] == "PEIMS Summer"


# -- Post-processing helpers ─────────────────────────────────────────────────


class TestRegulatoryCitations:
    def test_extracts_mixed_citations(self):
        text = (
            "The data element must be reported per 34 CFR 300.601(a) and "
            "20 U.S.C. 1416(b). See TEC § 29.095. Part C applies. Chapter 89."
        )
        cits = tx_tweds.extract_regulatory_citations(text)
        # Order: longest/specific first (CFR, USC, TEC, OMB, Part, Chapter)
        assert "34 CFR 300.601(a)" in cits
        assert any("20 U.S.C. 1416" in c for c in cits)
        assert "TEC § 29.095" in cits
        assert "Part C" in cits
        assert any("Chapter 89" in c for c in cits)

    def test_dedupes_and_preserves_first_seen(self):
        cits = tx_tweds.extract_regulatory_citations("TEC § 1.001 TEC § 1.001 TEC § 2.002")
        assert cits == ["TEC § 1.001", "TEC § 2.002"]

    def test_none_input_returns_empty(self):
        assert tx_tweds.extract_regulatory_citations(None) == []
        assert tx_tweds.extract_regulatory_citations("") == []


class TestDescriptorCodeParser:
    def test_extracts_code(self):
        assert tx_tweds.parse_descriptor_table_code("AcademicSubject(C325)") == "C325"
        assert tx_tweds.parse_descriptor_table_code("CalendarType(C041) ") == "C041"

    def test_returns_none_for_no_code(self):
        assert tx_tweds.parse_descriptor_table_code("") is None
        assert tx_tweds.parse_descriptor_table_code(None) is None
        assert tx_tweds.parse_descriptor_table_code("No Table") is None


class TestRelatedEntitiesClean:
    def test_dedupe_and_collapse(self):
        raw = ["Calendar  ", "Calendar > CalendarDate", "Calendar  ", "  "]
        out = tx_tweds.clean_related_entities(raw)
        assert out == ["Calendar", "Calendar > CalendarDate"]

    def test_none_input(self):
        assert tx_tweds.clean_related_entities(None) == []


# -- API surface ─────────────────────────────────────────────────────────────


class TestCliWiring:
    """Regression guard — all public entry points must be plain functions,
    not Click commands (same gotcha as the other state adapters)."""

    def test_tweds_public_functions_are_plain(self):
        for name in (
            "fetch_link_indexes",
            "scrape_elements",
            "scrape_entities",
            "load_or_fetch_tweds",
            "fetch_element_page",
            "parse_element_page",
            "extract_regulatory_citations",
            "parse_descriptor_table_code",
            "clean_related_entities",
        ):
            fn = getattr(tx_tweds, name)
            assert inspect.isfunction(fn), f"{name} is not a plain function"


# -- Element scrape completeness ─────────────────────────────────────────────


class TestScrapeElementsCompleteness:
    """`scrape_elements` must never accept a partial cache nor quietly
    write one. Surfaced 2026-04-21 during stakeholder onboarding: a
    fresh scrape hit three transient disconnects, cached 399/402 pages,
    and subsequent runs silently read back the incomplete snapshot —
    only detected downstream via golden drift."""

    def test_rejects_partial_cache_and_refills_gap(self, tmp_path, monkeypatch):
        links = [{"id": str(i), "name": f"e{i}", "url": f"u{i}"} for i in (1, 2, 3)]
        # Seed cache with only 2 of the 3 expected IDs — the classic
        # server-disconnect residue.
        (tmp_path / "elements_scraped.json").write_text(
            json.dumps([{"tweds_id": "1", "name": "e1"}, {"tweds_id": "2", "name": "e2"}]),
            encoding="utf-8",
        )

        fetched: list[str] = []

        def fake_fetch(_client, element_id: str):
            fetched.append(element_id)
            return {"tweds_id": element_id, "name": f"e{element_id}"}

        monkeypatch.setattr(tx_tweds, "fetch_element_page", fake_fetch)
        monkeypatch.setattr(tx_tweds.time, "sleep", lambda *_a, **_kw: None)

        out = tx_tweds.scrape_elements(links, tmp_path, delay=0)
        assert len(out) == 3
        # Only the missing ID should have been refetched.
        assert fetched == ["3"]
        # Cache now carries all three.
        cached = json.loads((tmp_path / "elements_scraped.json").read_text("utf-8"))
        assert {r["tweds_id"] for r in cached} == {"1", "2", "3"}

    def test_retries_once_then_raises_if_still_incomplete(self, tmp_path, monkeypatch):
        links = [{"id": str(i), "name": f"e{i}", "url": f"u{i}"} for i in (1, 2)]

        def always_fail_on_2(_client, element_id: str):
            if element_id == "2":
                return None
            return {"tweds_id": element_id, "name": f"e{element_id}"}

        monkeypatch.setattr(tx_tweds, "fetch_element_page", always_fail_on_2)
        monkeypatch.setattr(tx_tweds.time, "sleep", lambda *_a, **_kw: None)

        with pytest.raises(RuntimeError, match="TWEDS scrape incomplete"):
            tx_tweds.scrape_elements(links, tmp_path, delay=0)

        # Cache must NOT be written on incomplete scrape — downstream
        # consumers rely on its absence to know the scrape is unhealthy.
        assert not (tmp_path / "elements_scraped.json").exists()

    def test_uses_cache_as_is_when_complete(self, tmp_path, monkeypatch):
        links = [{"id": "1", "name": "e1", "url": "u1"}]
        (tmp_path / "elements_scraped.json").write_text(
            json.dumps([{"tweds_id": "1", "name": "cached-e1"}]),
            encoding="utf-8",
        )

        def should_not_fetch(*_a, **_kw):
            raise AssertionError("fetch should not be called when cache is complete")

        monkeypatch.setattr(tx_tweds, "fetch_element_page", should_not_fetch)

        out = tx_tweds.scrape_elements(links, tmp_path, delay=0)
        assert out == [{"tweds_id": "1", "name": "cached-e1"}]


class TestScrapeEntitiesCompleteness:
    """Issue #212 item 5: `scrape_entities` had NO completeness check —
    per-entity Playwright failures were logged and skipped, the partial
    result was cached, and every future run short-circuited on
    cache-exists (permanently missing TEDS entities)."""

    def _fake_core(self, pages_by_id):
        def core(to_fetch, results, cache_file, delay):
            for link in to_fetch:
                page = pages_by_id.get(link["id"])
                if page is not None:
                    results.append(page)
        return core

    def test_complete_cache_short_circuits_without_playwright(
        self, tmp_path, monkeypatch
    ):
        links = [{"id": "1", "name": "e1", "url": "u1"}]
        (tmp_path / "entities_scraped.json").write_text(
            json.dumps([{"tweds_id": "1", "entity_name": "cached"}]),
            encoding="utf-8",
        )

        def boom(*_a, **_kw):
            raise AssertionError("network core must not run on a complete cache")

        monkeypatch.setattr(tx_tweds, "_fetch_entity_pages_playwright", boom)
        out = tx_tweds.scrape_entities(links, tmp_path, delay=0)
        assert out == [{"tweds_id": "1", "entity_name": "cached"}]

    def test_incomplete_cache_rescrapes_only_the_gap(
        self, tmp_path, monkeypatch
    ):
        links = [{"id": str(i), "name": f"e{i}", "url": f"u{i}"} for i in (1, 2, 3)]
        (tmp_path / "entities_scraped.json").write_text(
            json.dumps([
                {"tweds_id": "1", "entity_name": "e1"},
                {"tweds_id": "2", "entity_name": "e2"},
            ]),
            encoding="utf-8",
        )
        seen: list[list[str]] = []

        def core(to_fetch, results, cache_file, delay):
            seen.append([l["id"] for l in to_fetch])
            for link in to_fetch:
                results.append({"tweds_id": link["id"], "entity_name": link["name"]})

        monkeypatch.setattr(tx_tweds, "_fetch_entity_pages_playwright", core)
        out = tx_tweds.scrape_entities(links, tmp_path, delay=0)
        assert seen == [["3"]]
        assert {r["tweds_id"] for r in out} == {"1", "2", "3"}
        cached = json.loads((tmp_path / "entities_scraped.json").read_text("utf-8"))
        assert {r["tweds_id"] for r in cached} == {"1", "2", "3"}

    def test_persistent_gap_raises_and_keeps_partial_for_resume(
        self, tmp_path, monkeypatch
    ):
        links = [{"id": str(i), "name": f"e{i}", "url": f"u{i}"} for i in (1, 2)]
        monkeypatch.setattr(
            tx_tweds, "_fetch_entity_pages_playwright",
            self._fake_core({"1": {"tweds_id": "1", "entity_name": "e1"}}),
        )
        with pytest.raises(RuntimeError, match="entity scrape incomplete"):
            tx_tweds.scrape_entities(links, tmp_path, delay=0)
        # Partial cache IS kept (unlike elements' fail-before-write) —
        # it is the Playwright resume checkpoint; the raise is what
        # keeps it from being consumed as complete.
        cached = json.loads((tmp_path / "entities_scraped.json").read_text("utf-8"))
        assert {r["tweds_id"] for r in cached} == {"1"}


class TestLoadOrFetchCompleteness:
    """Issue #212 item 5: the production path loaded the caches
    directly, bypassing both scrapers' completeness guards — an
    incomplete cache from an interrupted run loaded silently forever."""

    def _links(self, tmp_path, n_elements=2, n_entities=1):
        element_links = [
            {"id": str(i), "name": f"el{i}", "url": f"u{i}"}
            for i in range(1, n_elements + 1)
        ]
        entity_links = [
            {"id": f"E{i}", "name": f"en{i}", "url": f"v{i}"}
            for i in range(1, n_entities + 1)
        ]
        (tmp_path / "element_links.json").write_text(
            json.dumps(element_links), encoding="utf-8"
        )
        (tmp_path / "entity_links.json").write_text(
            json.dumps(entity_links), encoding="utf-8"
        )
        return entity_links, element_links

    def test_incomplete_element_cache_is_refilled_not_trusted(
        self, tmp_path, monkeypatch
    ):
        self._links(tmp_path)
        # Incomplete element cache (1 of 2) + complete entity cache.
        (tmp_path / "elements_scraped.json").write_text(
            json.dumps([{"tweds_id": "1", "name": "el1"}]), encoding="utf-8"
        )
        (tmp_path / "entities_scraped.json").write_text(
            json.dumps([{"tweds_id": "E1", "entity_name": "en1"}]),
            encoding="utf-8",
        )
        fetched: list[str] = []

        def fake_fetch(_client, element_id):
            fetched.append(element_id)
            return {"tweds_id": element_id, "name": f"el{element_id}"}

        monkeypatch.setattr(tx_tweds, "fetch_element_page", fake_fetch)
        monkeypatch.setattr(tx_tweds.time, "sleep", lambda *_a, **_kw: None)
        result = tx_tweds.load_or_fetch_tweds(tmp_path)
        assert result is not None
        entities, elements = result
        assert fetched == ["2"]  # only the gap
        assert {r["tweds_id"] for r in elements} == {"1", "2"}

    def test_incomplete_cache_offline_falls_back_to_none(
        self, tmp_path, monkeypatch
    ):
        self._links(tmp_path)
        (tmp_path / "elements_scraped.json").write_text(
            json.dumps([{"tweds_id": "1", "name": "el1"}]), encoding="utf-8"
        )

        def offline(_client, element_id):
            return None  # server unreachable → page never lands

        monkeypatch.setattr(tx_tweds, "fetch_element_page", offline)
        monkeypatch.setattr(tx_tweds.time, "sleep", lambda *_a, **_kw: None)
        # offline_ok (default): warn + None → caller falls back to the
        # spine-only baseline instead of ingesting a truncated corpus.
        assert tx_tweds.load_or_fetch_tweds(tmp_path) is None

    def test_complete_caches_load_without_network(self, tmp_path, monkeypatch):
        self._links(tmp_path)
        (tmp_path / "elements_scraped.json").write_text(
            json.dumps([
                {"tweds_id": "1", "name": "el1"},
                {"tweds_id": "2", "name": "el2"},
            ]),
            encoding="utf-8",
        )
        (tmp_path / "entities_scraped.json").write_text(
            json.dumps([{"tweds_id": "E1", "entity_name": "en1"}]),
            encoding="utf-8",
        )

        def boom(*_a, **_kw):
            raise AssertionError("no network on complete caches")

        monkeypatch.setattr(tx_tweds, "fetch_element_page", boom)
        monkeypatch.setattr(tx_tweds, "_fetch_entity_pages_playwright", boom)
        result = tx_tweds.load_or_fetch_tweds(tmp_path)
        assert result is not None
        entities, elements = result
        assert len(elements) == 2 and len(entities) == 1


class TestPlaywrightOptionalExtra:
    """playwright moved to `[project.optional-dependencies] scrape`
    (issue #213 item 4c) — the lazy import site must fail with the
    install hint, not a bare ModuleNotFoundError."""

    def test_missing_playwright_raises_friendly_install_hint(
        self, tmp_path, monkeypatch
    ):
        import sys

        # A None entry in sys.modules makes the import machinery raise
        # ImportError without uninstalling anything.
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        with pytest.raises(ImportError, match=r"uv pip install -e '\.\[scrape\]'"):
            tx_tweds._fetch_entity_pages_playwright(
                [{"url": "http://x", "name": "n", "id": "1"}],
                [],
                tmp_path / "cache.json",
                0.0,
            )
