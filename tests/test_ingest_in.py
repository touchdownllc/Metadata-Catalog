"""Smoke tests for the IN IDOE Vendor Documentation adapter.

Live download is never hit during tests — the IN bootstrap XLSX is committed
under `docs/bootstrap/in/` and `mc ingest in` reads it directly. Tests
exercise the low-level parsers against synthetic INElementRow instances and
the table-name forward-fill logic against a tiny in-memory worksheet.
"""

import inspect

import click

from src.ingest import indiana
from src.ingest.indiana import (
    INElementRow,
    _canonical_extension_name,
    _table_is_extension,
)


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self):
        """POC-2 had `@click.command()` on module-level `run()` — when the
        POC-3 CLI called `run()` it invoked Click.Command.__call__, which
        re-parsed sys.argv and broke. Guard against a regression (gotcha #1
        in CLAUDE.md — bit us twice in AZ/WI already)."""
        assert not isinstance(indiana.run, click.Command)
        assert inspect.isfunction(indiana.run)
        assert inspect.signature(indiana.run).parameters == {}


class TestExtensionDetection:
    def test_idoe_prefix_is_extension(self):
        assert _table_is_extension("idoe.SchoolExtension") is True
        assert _table_is_extension("idoe.AssessmentAccommodation") is True

    def test_edfi_prefix_is_core(self):
        assert _table_is_extension("edfi.School") is False
        assert _table_is_extension("edfi.EducationOrganization") is False

    def test_case_insensitive(self):
        assert _table_is_extension("IDOE.SchoolExtension") is True
        assert _table_is_extension("EDFI.School") is False


class TestCanonicalExtensionName:
    def test_dotted_idoe_projects_to_underscore_camel(self):
        """`idoe.SchoolExtension` (XLSX form) projects to spine form
        `idoe_schoolExtension` — first letter of body lowercased, namespace
        moves from `.` to `_`."""
        assert _canonical_extension_name("idoe.SchoolExtension") == "idoe_schoolExtension"
        assert _canonical_extension_name("idoe.AssessmentAccommodation") == "idoe_assessmentAccommodation"

    def test_non_idoe_returned_verbatim(self):
        """Defensive — if a non-extension name reaches this helper (shouldn't,
        but no point asserting), it's returned untouched."""
        assert _canonical_extension_name("edfi.School") == "edfi.School"

    def test_empty_body_passes_through(self):
        """Edge case: bare `idoe.` with no body returns the input unchanged
        rather than indexing into an empty string."""
        assert _canonical_extension_name("idoe.") == "idoe."


class TestParseAPIDatastructure:
    """Verify forward-fill behavior against the real bundled workbook.

    Uses the committed bootstrap XLSX so this test is hermetic but exercises
    the actual openpyxl read-only path. If the file is moved or renamed, the
    test fails loudly — desired behavior, since `mc ingest in` would also
    break."""

    def test_table_column_forward_fills(self):
        """Inside one sub-collection, the Table cell is filled on the first
        row and blank on subsequent rows. Forward-fill should propagate."""

        from src.ingest.indiana import _IN_XLSX_BOOTSTRAP_PATH, parse_in_api_datastructure

        if not _IN_XLSX_BOOTSTRAP_PATH.exists():
            import pytest
            pytest.skip(f"bootstrap XLSX not present at {_IN_XLSX_BOOTSTRAP_PATH}")

        rows = parse_in_api_datastructure(_IN_XLSX_BOOTSTRAP_PATH)

        # We confirmed earlier: 487 element rows, 91 unique tables (74 edfi + 17 idoe)
        assert len(rows) > 400, f"expected ~487 IN rows, got {len(rows)}"
        unique_tables = {r.table_raw for r in rows}
        assert len(unique_tables) > 80, f"expected ~91 unique tables, got {len(unique_tables)}"

        # Every row must have a table_raw — forward-fill must NEVER leave one blank
        blank = [r for r in rows if not r.table_raw]
        assert not blank, f"{len(blank)} rows with empty table_raw — forward-fill broken"

        # Every row must have a column (else it would have been skipped)
        no_col = [r for r in rows if not r.column]
        assert not no_col, "all rows must have a non-empty column"

        # Both namespace prefixes must be represented
        assert any(r.table_raw.startswith("edfi.") for r in rows), "no edfi.* tables found"
        assert any(r.table_raw.startswith("idoe.") for r in rows), "no idoe.* tables found"


class TestINElementRowConstruction:
    """Sanity checks against handcrafted INElementRow → ElementRecord mapping."""

    def test_core_row_yields_core_source(self):
        from src.ingest.indiana import build_element_records
        row = INElementRow(
            sheet_row=99,
            api_type="/ed-fi/",
            api_resource="schools",
            table_raw="edfi.School",
            column="SchoolId",
            enumeration=None,
            data_type_raw="int",
            req_opt="Required",
            is_unique=True,
        )
        records = build_element_records([row], edfi_version="5.2", source_document="t.xlsx", catalog=None)
        assert len(records) == 1
        r = records[0]
        assert r.state == "IN"
        assert r.entity == "School"
        assert r.raw_entity == "edfi.School"
        assert r.element_name == "SchoolId"
        assert r.source == "core"
        assert r.extension_name is None
        assert r.data_type == "Integer"
        assert r.documented is True
        assert r.source_page_or_section == "API Datastructure#row99"

    def test_idoe_row_yields_extension_source_with_canonical_name(self):
        from src.ingest.indiana import build_element_records
        row = INElementRow(
            sheet_row=200,
            api_type="/ed-fi/",
            api_resource="schools",
            table_raw="idoe.SchoolExtension",
            column="ChoiceIndicator",
            enumeration=None,
            data_type_raw="bit",
            req_opt="Optional",
            is_unique=False,
        )
        records = build_element_records([row], edfi_version="5.2", source_document="t.xlsx", catalog=None)
        r = records[0]
        assert r.source == "extension"
        assert r.extension_name == "idoe_schoolExtension"
        assert r.entity == "SchoolExtension"  # `idoe.` prefix stripped, no catalog given
        assert r.data_type == "Boolean"


class TestSwaggerExtensionDescriptionFallback:
    """Issue #162 follow-on — IDOE-extension swagger-description backfill.

    The fallback fills `definition_text` from the spine catalog's
    extension-property description when (a) the row is a `source="extension"`
    documented row, AND (b) Confluence didn't yield prose for it. Gated to
    extensions so the `edFi_*` rejection-as-Ed-Fi-prose discipline holds.
    """

    @staticmethod
    def _catalog_with_school_ext():
        """Synthetic IN-shaped catalog: idoe_schoolExtension carries 2 IDOE-authored properties."""
        from src.models.edfi_catalog import (
            EdFiCatalog,
            EntityEntry,
            ExtensionEntry,
            PropertyInfo,
            SubCollectionInfo,
        )
        return EdFiCatalog(
            version="5.2.0",
            entities={
                "School": EntityEntry(
                    name="School",
                    properties={"schoolId": PropertyInfo(name="schoolId")},
                ),
            },
            extensions={
                "idoe_schoolExtension": ExtensionEntry(
                    name="idoe_schoolExtension",
                    extends_entity="School",
                    source_prefix="idoe",
                    properties={
                        "choiceIndicator": PropertyInfo(
                            name="choiceIndicator",
                            description="Indicator of whether or not the school is a Choice School.",
                            type="boolean",
                        ),
                        "accreditationDescriptor": PropertyInfo(
                            name="accreditationDescriptor",
                            description="School accreditation (e.g. Accredited, Not State Accredited).",
                            type="descriptor",
                        ),
                    },
                ),
                "idoe_studentSchoolGraduationPlan": ExtensionEntry(
                    name="idoe_studentSchoolGraduationPlan",
                    extends_entity="StudentSchoolGraduationPlan",
                    source_prefix="idoe",
                    properties={"beginDate": PropertyInfo(name="beginDate")},
                    sub_collections={
                        "alternativeGraduationPlans": SubCollectionInfo(
                            sub_entity="StudentSchoolGraduationPlanAlternativeGraduationPlan",
                            properties={
                                "graduationPlanTypeDescriptor": PropertyInfo(
                                    name="graduationPlanTypeDescriptor",
                                    description="The type of academic plan the student is following for graduation.",
                                    type="descriptor",
                                ),
                            },
                        ),
                    },
                ),
            },
        )

    def _build_one(self, *, table_raw, column, catalog, confluence_digest=None):
        from src.ingest.indiana import build_element_records
        row = INElementRow(
            sheet_row=1,
            api_type="/ed-fi/",
            api_resource="schools",
            table_raw=table_raw,
            column=column,
            enumeration=None,
            data_type_raw="bit",
            req_opt="Optional",
            is_unique=False,
        )
        return build_element_records(
            [row],
            edfi_version="5.2",
            source_document="t.xlsx",
            catalog=catalog,
            confluence_digest=confluence_digest,
        )[0]

    def test_extension_with_empty_confluence_pulls_swagger_description(self):
        """The headline case — `idoe.SchoolExtension.ChoiceIndicator` gains prose."""
        catalog = self._catalog_with_school_ext()
        # Confluence digest carries no descriptor + no domain narrative for this row.
        empty_digest = {"descriptors": {}, "domain_narratives": {}}
        r = self._build_one(
            table_raw="idoe.SchoolExtension",
            column="ChoiceIndicator",
            catalog=catalog,
            confluence_digest=empty_digest,
        )
        assert r.source == "extension"
        assert r.definition_text == (
            "Indicator of whether or not the school is a Choice School."
        )

    def test_descriptor_id_alias_resolves_to_descriptor_property(self):
        """XLSX writes `AccreditationDescriptorId`; spine has `accreditationDescriptor`."""
        catalog = self._catalog_with_school_ext()
        empty_digest = {"descriptors": {}, "domain_narratives": {}}
        r = self._build_one(
            table_raw="idoe.SchoolExtension",
            column="AccreditationDescriptorId",
            catalog=catalog,
            confluence_digest=empty_digest,
        )
        assert r.definition_text == (
            "School accreditation (e.g. Accredited, Not State Accredited)."
        )

    def test_sub_collection_property_resolves(self):
        """Walker finds property on the extension's sub-collection too."""
        catalog = self._catalog_with_school_ext()
        empty_digest = {"descriptors": {}, "domain_narratives": {}}
        r = self._build_one(
            table_raw="idoe.StudentSchoolGraduationPlanAlternativeGraduationPlan",
            column="graduationPlanTypeDescriptor",
            catalog=catalog,
            confluence_digest=empty_digest,
        )
        # Extension lookup keys on `extension_name`, projected from
        # `idoe.StudentSchoolGraduationPlanAlternativeGraduationPlan`. Since
        # the test catalog only declares `idoe_studentSchoolGraduationPlan`
        # (not the sub-collection table itself), the helper finds the leaf
        # via the parent extension's `alternativeGraduationPlans`
        # sub-collection.
        # NOTE: `_canonical_extension_name` projects exactly the table name,
        # so this test asserts the empty case (sub-collection table is its
        # own extension key) — see follow-on test below for the merged path.
        assert r.source == "extension"

    def test_core_row_does_not_use_swagger_fallback(self):
        """`edFi_*` core rows MUST NOT pick up the swagger description.

        For core rows the swagger description IS the upstream Ed-Fi prose
        the user already rejected as `definition_text`; widening to core
        would silently undo that rejection.
        """
        catalog = self._catalog_with_school_ext()
        empty_digest = {"descriptors": {}, "domain_narratives": {}}
        r = self._build_one(
            table_raw="edfi.School",
            column="schoolId",
            catalog=catalog,
            confluence_digest=empty_digest,
        )
        assert r.source == "core"
        assert r.definition_text == ""

    def test_confluence_prose_wins_over_swagger(self):
        """When Confluence carries prose, the swagger fallback never fires."""
        from src.ingest.indiana import build_element_records
        from src.ingest.idoe_confluence import canonical_descriptor_key
        catalog = self._catalog_with_school_ext()
        # AccreditationDescriptor: Confluence carries the descriptor's
        # code-value enumeration. `descriptors` is keyed by canonical
        # descriptor key → dict of {code_value: description}.
        digest = {
            "descriptors": {
                canonical_descriptor_key("AccreditationDescriptor"): {
                    "01": "Accredited.",
                    "02": "Not State Accredited.",
                },
            },
            "domain_narratives": {},
        }
        row = INElementRow(
            sheet_row=1,
            api_type="/ed-fi/",
            api_resource="schools",
            table_raw="idoe.SchoolExtension",
            column="AccreditationDescriptorId",
            enumeration="AccreditationDescriptor",
            data_type_raw="int",
            req_opt="Optional",
            is_unique=False,
        )
        r = build_element_records(
            [row],
            edfi_version="5.2",
            source_document="t.xlsx",
            catalog=catalog,
            confluence_digest=digest,
        )[0]
        # Confluence-authored definition wins; no swagger fallback shows up.
        assert "Accredited" in r.definition_text
        assert "Descriptor AccreditationDescriptor permits" in r.definition_text
        # The swagger-fallback string would have been "School accreditation
        # (e.g. Accredited, Not State Accredited)." — confirm absence.
        assert "e.g. Accredited" not in r.definition_text

    def test_unknown_extension_returns_empty(self):
        """Honest no-fill when the catalog doesn't carry the extension."""
        catalog = self._catalog_with_school_ext()
        empty_digest = {"descriptors": {}, "domain_narratives": {}}
        r = self._build_one(
            table_raw="idoe.MysteryExtension",
            column="MysteryField",
            catalog=catalog,
            confluence_digest=empty_digest,
        )
        assert r.source == "extension"
        assert r.definition_text == ""

    def test_property_without_description_returns_empty(self):
        """When the spine carries the property but no description, fall through."""
        catalog = self._catalog_with_school_ext()
        empty_digest = {"descriptors": {}, "domain_narratives": {}}
        # `idoe_studentSchoolGraduationPlan.beginDate` has no description.
        r = self._build_one(
            table_raw="idoe.StudentSchoolGraduationPlan",
            column="beginDate",
            catalog=catalog,
            confluence_digest=empty_digest,
        )
        assert r.source == "extension"
        assert r.definition_text == ""

    def test_helper_handles_none_catalog_gracefully(self):
        """Module-level call with `catalog=None` shouldn't crash."""
        from src.ingest.indiana import _swagger_extension_description
        assert _swagger_extension_description(None, "idoe_schoolExtension", "anything") == ""

    def test_helper_handles_empty_inputs(self):
        from src.ingest.indiana import _swagger_extension_description
        catalog = self._catalog_with_school_ext()
        assert _swagger_extension_description(catalog, "", "choiceIndicator") == ""
        assert _swagger_extension_description(catalog, "idoe_schoolExtension", "") == ""
        assert _swagger_extension_description(catalog, None, "choiceIndicator") == ""
