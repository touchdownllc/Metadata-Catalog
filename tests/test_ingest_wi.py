"""Smoke tests for the WI Confluence adapter.

Tests exercise `build_element_records` and related parsers against synthetic
HTML fixtures. Live Confluence is never hit — the real fetch/crawl is
covered by cached `data/raw/wi/confluence/*.json` at runtime.
"""

import inspect
import json
from datetime import datetime, timezone

import click
import pytest
from bs4 import BeautifulSoup

from src.ingest import wisconsin
from src.ingest.wisconsin import (
    build_element_records,
    extract_business_rules,
    parse_data_properties_table,
    run,
)
from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    PropertyInfo,
)
from src.models.element import StateElements
from src.models.spine import SpineSourceURLs, StateSpine
from src.spine.build import build_lookup_index


# A minimal Confluence entity-page HTML fragment mirroring the real page
# structure: a data-properties table + a "Business Rules" section.
_SAMPLE_PAGE_HTML = """
<h1>CalendarDate</h1>
<table>
  <thead>
    <tr>
      <th>#</th>
      <th>Property Name</th>
      <th>Data Type</th>
      <th>Public (req'd)</th>
      <th>Choice (req'd)</th>
      <th>Definition</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>1</td>
      <td>calendarCode</td>
      <td>string</td>
      <td>R</td>
      <td>R</td>
      <td>The calendar code identifying this calendar.</td>
    </tr>
    <tr>
      <td>2</td>
      <td>date</td>
      <td>date</td>
      <td>R</td>
      <td>R</td>
      <td>The calendar date.</td>
    </tr>
  </tbody>
</table>
<h2>Business Rules</h2>
<ul><li>Date must be within the school year.</li></ul>
"""


@pytest.fixture
def synthetic_scrape():
    """Fake scraped entity page + entity_pages list, mirroring cache shape."""
    entity_pages = [{
        "page_id": "test-1",
        "title": "CalendarDate",
        "domain": "Calendar",
    }]
    scraped = {
        "test-1": {
            "title": "CalendarDate",
            "body_html": _SAMPLE_PAGE_HTML,
            "url": "https://example.test/calendardate",
        },
    }
    return entity_pages, scraped


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self):
        """POC-2 had `@click.command()` on module-level `run()` — when the
        POC-3 CLI called `run()` it invoked Click.Command.__call__, which
        re-parsed sys.argv and broke. Guard against a regression."""
        assert not isinstance(wisconsin.run, click.Command)
        assert inspect.isfunction(wisconsin.run)
        assert inspect.signature(wisconsin.run).parameters == {}


class TestParseDataPropertiesTable:
    def test_extracts_property_rows(self):
        soup = BeautifulSoup(_SAMPLE_PAGE_HTML, "html.parser")
        props = parse_data_properties_table(soup)
        names = [p["clean_name"] for p in props]
        assert "calendarCode" in names
        assert "date" in names


class TestExtractBusinessRules:
    def test_returns_rule_text(self):
        soup = BeautifulSoup(_SAMPLE_PAGE_HTML, "html.parser")
        rules = extract_business_rules(soup)
        assert rules is not None
        assert "school year" in rules.lower()


class TestBuildElementRecords:
    def test_smoke_produces_records_with_wi_state(self, synthetic_scrape):
        entity_pages, scraped = synthetic_scrape
        records = build_element_records(scraped, entity_pages, catalog=None)
        assert records, "expected at least one ElementRecord from sample HTML"
        for r in records:
            assert r.state == "WI"
            assert r.element_name in {"calendarCode", "date"}
            assert r.entity  # some non-empty entity name

    def test_business_rules_flow_through(self, synthetic_scrape):
        entity_pages, scraped = synthetic_scrape
        records = build_element_records(scraped, entity_pages, catalog=None)
        # At least one record should carry the page-level business rule text
        # (WI scraper attaches page-wide rules to every element on the page).
        assert any(
            r.business_rules_text and "school year" in r.business_rules_text.lower()
            for r in records
        )


class TestExtensionPrefixStripping:
    """Analyst P2 (reviewer 2 #8): WI Confluence cells sometimes emit a
    property name with the extension schema prefix still attached, e.g.
    `wi_staffEducationOrganizationEmploymentAssociationExtension.localPersonIdentificationCode`.
    The element_name should be the bare identifier; extension attribution is
    handled downstream via spine lookup."""

    def test_strips_leading_extension_prefix(self):
        from src.ingest.wisconsin import _strip_wi_extension_prefix
        assert (
            _strip_wi_extension_prefix(
                "wi_staffEducationOrganizationEmploymentAssociationExtension.localPersonIdentificationCode"
            )
            == "localPersonIdentificationCode"
        )

    def test_strips_extension_prefix_capital_E(self):
        """Defensive — regex allows both `Extension.` and `extension.`."""
        from src.ingest.wisconsin import _strip_wi_extension_prefix
        assert (
            _strip_wi_extension_prefix("wi_graduationPlanExtension.fieldName")
            == "fieldName"
        )

    def test_leaves_non_extension_names_alone(self):
        from src.ingest.wisconsin import _strip_wi_extension_prefix
        assert _strip_wi_extension_prefix("calendarCode") == "calendarCode"
        # A record whose name genuinely contains a dot but no wi_ prefix.
        assert (
            _strip_wi_extension_prefix("schoolReference.schoolId")
            == "schoolReference.schoolId"
        )


class TestExtensionContainerFilter:
    """Analyst feedback (T2.6): `WI_studentSchoolAssociationExtensions`
    leaked into Details as a data element row. The container holds the
    extension; its child properties are the actual data elements.
    `_is_wi_extension_container` detects the pattern safely."""

    def test_detects_uppercase_prefix_plural_container(self):
        from src.ingest.wisconsin import _is_wi_extension_container
        assert _is_wi_extension_container("WI_studentSchoolAssociationExtensions")

    def test_detects_lowercase_prefix_plural_container(self):
        from src.ingest.wisconsin import _is_wi_extension_container
        assert _is_wi_extension_container("wi_calendarExtensions")

    def test_singular_extension_prefixed_element_not_container(self):
        """A property named `wi_*Extension.foo` is an element after prefix
        stripping, not a container — only the plural holder is the container."""
        from src.ingest.wisconsin import _is_wi_extension_container
        assert not _is_wi_extension_container("wi_calendarExtension")

    def test_plain_names_not_containers(self):
        from src.ingest.wisconsin import _is_wi_extension_container
        assert not _is_wi_extension_container("calendarCode")
        assert not _is_wi_extension_container("schoolReference.schoolId")
        # A property named `extensions` without the wi_/WI_ prefix is not
        # filtered — if it ever exists in a spec, it's a real property.
        assert not _is_wi_extension_container("extensions")


class TestWiElementCorrections:
    """Analyst feedback (reviewer 1 §3): `RccName` in Confluence is a
    source-side truncation of the canonical spine property
    `rccNameOfInstitution`. Correction table rewrites to the canonical form
    so spine match succeeds and the row attributes as extension."""

    def test_rcc_name_rewrites_to_canonical(self):
        from src.ingest.wisconsin import _apply_wi_element_corrections
        assert (
            _apply_wi_element_corrections("RccName") == "rccNameOfInstitution"
        )

    def test_uncorrected_names_passthrough(self):
        from src.ingest.wisconsin import _apply_wi_element_corrections
        assert _apply_wi_element_corrections("calendarCode") == "calendarCode"
        assert _apply_wi_element_corrections("RccCity") == "RccCity"


def _make_wi_spine() -> StateSpine:
    """Minimal WI spine: one core entity matching the sample scrape."""
    entities = {
        "CalendarDate": EntityEntry(
            description="A date associated with a calendar.",
            domains=["Calendar"],
            properties={
                "calendarCode": PropertyInfo(
                    description="The calendar code.", type="string",
                ),
                "date": PropertyInfo(
                    description="The calendar date.",
                    type="string", format="date",
                ),
            },
            references={},
            sub_collections={},
        ),
    }
    catalog = EdFiCatalog(
        version="5.2",
        entity_count=len(entities),
        extension_count=0,
        entities=entities,
        extensions={},
        lookup_index=build_lookup_index(entities, {}),
    )
    return StateSpine(
        state="WI",
        edfi_version="5.2",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(
            resources="http://example.test/metadata/data/v3/resources/swagger.json",
        ),
        catalog=catalog,
    )


class TestEndToEndRun:
    """Regression guard: `run()` must write BOTH `_source.json` AND
    `_spine.json` artifacts (Option 3b dual-lens contract). No existing
    unit test exercises `wisconsin.run()` end-to-end — the Phase 3
    `populate_data_types_from_spine` import bug that shipped to the
    WI overnight run was caught only by a manual `mc ingest wi`.
    """

    def test_run_writes_both_lens_artifacts(
        self, tmp_path, monkeypatch, synthetic_scrape
    ):
        entity_pages, scraped = synthetic_scrape
        element_page_content: dict[str, str | None] = {}

        spine = _make_wi_spine()
        spine_path = tmp_path / "data" / "spine" / "wi_spine.json"
        spine_path.parent.mkdir(parents=True, exist_ok=True)
        spine_path.write_text(spine.model_dump_json(indent=2), encoding="utf-8")

        out_dir = tmp_path / "data" / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(wisconsin, "_MC_ROOT", tmp_path)
        monkeypatch.setattr(wisconsin, "_WI_SPINE_PATH", spine_path)
        monkeypatch.setattr(
            wisconsin, "_WI_ELEMENTS_OUT", out_dir / "wi_elements_source.json"
        )
        monkeypatch.setattr(
            wisconsin,
            "_WI_ELEMENTS_SPINE_OUT",
            out_dir / "wi_elements_spine.json",
        )
        monkeypatch.setattr(wisconsin, "_WI_GAP_OUT", out_dir / "wi_gap_log.json")
        monkeypatch.setattr(wisconsin, "_WI_CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(
            wisconsin,
            "_load_or_fetch_confluence",
            lambda *a, **kw: (entity_pages, scraped, element_page_content),
        )
        # The synthetic scrape is deliberately tiny — lower the issue #213
        # format-drift floors below it (module-constant monkeypatch seam).
        monkeypatch.setattr(wisconsin, "_MIN_PARSED_ROWS", 0)
        monkeypatch.setattr(wisconsin, "_MIN_RECOGNIZED_TABLES", 0)

        run()

        source_path = out_dir / "wi_elements_source.json"
        spine_elements_path = out_dir / "wi_elements_spine.json"
        gap_path = out_dir / "wi_gap_log.json"
        assert source_path.exists()
        assert spine_elements_path.exists(), (
            "run() must write the spine-lens artifact (Option 3b contract)"
        )
        assert gap_path.exists()

        source_elements = StateElements.model_validate_json(
            source_path.read_text(encoding="utf-8")
        )
        assert source_elements.state == "WI"
        assert source_elements.element_count == len(source_elements.elements)
        assert source_elements.element_count > 0

        spine_elements = StateElements.model_validate_json(
            spine_elements_path.read_text(encoding="utf-8")
        )
        assert spine_elements.state == "WI"
        assert spine_elements.element_count == len(spine_elements.elements)
        # Spine lens walks the catalog — we have 2 properties on CalendarDate,
        # so expect at least those two canonical slots.
        spine_pairs = {
            (r.entity, r.element_name) for r in spine_elements.elements
        }
        assert ("CalendarDate", "calendarCode") in spine_pairs
        assert ("CalendarDate", "date") in spine_pairs

        gap = json.loads(gap_path.read_text(encoding="utf-8"))
        assert "source_coverage" in gap
        assert "spine_coverage" in gap


class TestEntityScrapeNegativeCaching:
    """Issue #212 item 6: HTTP failures were cached as real entries
    (empty body + error tag) and the resume logic skipped anything
    already keyed — one transient Confluence 5xx silently removed that
    entity page from every future WI artifact."""

    _PAGES = [
        {"page_id": "p1", "title": "T1", "domain": "D"},
        {"page_id": "p2", "title": "T2", "domain": "D"},
    ]

    def _body(self, text):
        return {"body": {"storage": {"value": text}}}

    def test_cached_failure_is_reattempted_and_healed(
        self, tmp_path, monkeypatch
    ):
        from src.ingest import wisconsin

        # Seed the cache with one healthy entry and one failure entry.
        (tmp_path / "entities_scraped.json").write_text(
            json.dumps({
                "p1": {"page_id": "p1", "title": "T1", "domain": "D",
                       "body_html": "<p>ok</p>"},
                "p2": {"page_id": "p2", "title": "T2", "domain": "D",
                       "body_html": "", "error": "boom (transient 5xx)"},
            }),
            encoding="utf-8",
        )
        fetched: list[str] = []

        def fake_fetch(_client, page_id):
            fetched.append(page_id)
            return self._body(f"<p>healed {page_id}</p>")

        monkeypatch.setattr(wisconsin, "fetch_entity_page", fake_fetch)
        monkeypatch.setattr(wisconsin.time, "sleep", lambda *_a: None)
        scraped = wisconsin.scrape_all_entity_pages(
            client=None, entity_pages=self._PAGES, cache_dir=tmp_path,
        )
        # Only the failure entry was re-attempted; it healed in place.
        assert fetched == ["p2"]
        assert scraped["p2"]["body_html"] == "<p>healed p2</p>"
        assert "error" not in scraped["p2"]
        cached = json.loads(
            (tmp_path / "entities_scraped.json").read_text("utf-8")
        )
        assert cached["p2"]["body_html"] == "<p>healed p2</p>"

    def test_mostly_failed_scrape_raises_not_warns(
        self, tmp_path, monkeypatch
    ):
        import httpx

        from src.ingest import wisconsin

        def failing_fetch(_client, page_id):
            raise httpx.ConnectError("confluence down")

        monkeypatch.setattr(wisconsin, "fetch_entity_page", failing_fetch)
        monkeypatch.setattr(wisconsin.time, "sleep", lambda *_a: None)
        with pytest.raises(RuntimeError, match="entity pages have body"):
            wisconsin.scrape_all_entity_pages(
                client=None, entity_pages=self._PAGES, cache_dir=tmp_path,
            )
        # The cache (with tagged failures) is kept for the next attempt.
        cached = json.loads(
            (tmp_path / "entities_scraped.json").read_text("utf-8")
        )
        assert cached["p1"]["error"]
        assert cached["p1"]["body_html"] == ""


class TestElementPageNegativeCaching:
    def test_only_error_entries_are_refetched(self, tmp_path, monkeypatch):
        """A cached fetch FAILURE re-attempts; a fetched-but-parse-empty
        page (content None, no error tag) stays cached — refetching it
        every run would be pure churn."""
        from src.ingest import wisconsin

        (tmp_path / "element_pages.json").write_text(
            json.dumps({
                "https://dpi.wi.gov/ok": {
                    "url": "https://dpi.wi.gov/ok",
                    "content": "rules text", "status": 200,
                },
                "https://dpi.wi.gov/parse-empty": {
                    "url": "https://dpi.wi.gov/parse-empty",
                    "content": None, "status": 200,
                },
                "https://dpi.wi.gov/failed": {
                    "url": "https://dpi.wi.gov/failed",
                    "content": None, "status": None,
                    "error": "boom (transient 5xx)",
                },
            }),
            encoding="utf-8",
        )
        fetched: list[str] = []

        class FakeResp:
            status_code = 200
            text = "<main><p>business rules recovered here</p></main>"

            def raise_for_status(self):
                return None

        class FakeClient:
            def get(self, url):
                fetched.append(url)
                return FakeResp()

        monkeypatch.setattr(wisconsin.time, "sleep", lambda *_a: None)
        out = wisconsin.fetch_element_pages(
            FakeClient(),
            {
                "https://dpi.wi.gov/ok",
                "https://dpi.wi.gov/parse-empty",
                "https://dpi.wi.gov/failed",
            },
            tmp_path,
        )
        assert fetched == ["https://dpi.wi.gov/failed"]
        assert out["https://dpi.wi.gov/ok"] == "rules text"
        assert out["https://dpi.wi.gov/failed"] is not None


class TestParseFloor:
    """Issue #213 item 2: the WI Confluence parser SKIPS unrecognized
    tables/pages silently — `run()` calls `enforce_parse_floor` on the
    `build_element_records` output so a Confluence template change can't
    quietly shrink the corpus (`_MIN_PARSED_ROWS` /
    `_MIN_RECOGNIZED_TABLES`, module-constant monkeypatch seam)."""

    @staticmethod
    def _records(n_rows: int, n_pages: int) -> list:
        from src.models.element import ElementRecord

        return [
            ElementRecord(
                state="WI",
                edfi_version="5.2",
                domain="Enrollment",
                entity=f"Entity{i % n_pages}",
                element_name=f"element{i}",
                data_type="String",
                definition_text="x",
                source_document="WI DPI Ed-Fi Confluence Wiki",
                source_page_or_section=f"https://example/pages/{i % n_pages}",
                documented=True,
            )
            for i in range(n_rows)
        ]

    def test_truncated_corpus_raises(self):
        with pytest.raises(ValueError, match="source format may have changed"):
            wisconsin.enforce_parse_floor(self._records(5, 2))

    def test_enough_rows_but_too_few_recognized_pages_raises(self):
        """The two floors are independent — a template change that folds
        everything onto one recognized page must still trip."""
        with pytest.raises(ValueError, match="source format may have changed"):
            wisconsin.enforce_parse_floor(self._records(400, 1))

    def test_floor_is_a_monkeypatchable_module_constant(self, monkeypatch):
        monkeypatch.setattr(wisconsin, "_MIN_PARSED_ROWS", 3)
        monkeypatch.setattr(wisconsin, "_MIN_RECOGNIZED_TABLES", 2)
        wisconsin.enforce_parse_floor(self._records(5, 2))  # must not raise

    def test_golden_corpus_clears_floor_with_headroom(self):
        """Today's real corpus (committed golden; `source_doc` rows = the
        parse-derived population) must clear both floors by >=2x
        (calibration pin: 533 rows / 49 pages as of 2026-07-09 — if this
        fails after a source refresh, re-derive the floors). Raw-JSON load:
        the golden deliberately strips volatile fields like `extracted_at`,
        so it doesn't round-trip through StateElements."""
        from pathlib import Path

        golden = (
            Path(__file__).resolve().parent
            / "golden" / "wi_elements_source.json"
        )
        data = json.loads(golden.read_text(encoding="utf-8"))
        parsed = [
            e for e in data["elements"]
            if e.get("documentation_source") == "source_doc"
        ]
        pages = {e.get("source_page_or_section") for e in parsed}
        assert len(parsed) >= 2 * wisconsin._MIN_PARSED_ROWS
        assert len(pages) >= 2 * wisconsin._MIN_RECOGNIZED_TABLES
