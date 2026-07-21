"""Tests for Ed-Fi spine builder — backport detection and plural lookup."""

import json
from pathlib import Path

from src.spine.build import build_catalog


def _write_swagger(path: Path, schemas: dict) -> None:
    """Write a minimal OpenAPI 3.0 swagger file with given schemas."""
    swagger = {
        "openapi": "3.0.0",
        "info": {"title": "test", "version": "1.0"},
        "paths": {},
        "components": {"schemas": schemas},
    }
    path.write_text(json.dumps(swagger))


def _entity_schema(required: list[str] | None = None, **props) -> dict:
    """Build a minimal entity schema."""
    return {
        "type": "object",
        "required": required or [],
        "properties": {
            "id": {"type": "string"},
            **{name: {"type": "string"} for name in props},
        },
    }


class TestCatalogBackport:
    """Extensions bundles can backport new core (edFi_*) entities that
    didn't exist in the base Ed-Fi version. Spine must pick these up."""

    def test_backports_new_edfi_entities_from_extensions(self, tmp_path):
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_student": _entity_schema(studentUniqueId="string"),
        })

        ext_path = tmp_path / "ext.json"
        _write_swagger(ext_path, {
            "edFi_student": _entity_schema(studentUniqueId="string"),
            "edFi_studentTransportation": _entity_schema(
                transportationTypeDescriptor="string",
            ),
            "xx_someStateExtension": _entity_schema(customField="string"),
        })

        catalog = build_catalog(core_path, "5.2", ext_path)

        assert "StudentTransportation" in catalog.entities
        assert "Student" in catalog.entities
        assert "studenttransportation" in catalog.lookup_index

    def test_backport_does_not_overwrite_core_entity(self, tmp_path):
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_student": _entity_schema(studentUniqueId="string", legalName="string"),
        })

        ext_path = tmp_path / "ext.json"
        _write_swagger(ext_path, {
            "edFi_student": _entity_schema(studentUniqueId="string"),
        })

        catalog = build_catalog(core_path, "5.2", ext_path)
        student = catalog.entities["Student"]
        assert "legalName" in student.properties


class TestCatalogStateExtension:
    """has_state_extension and get_state_extensions_for_entity methods."""

    def test_has_state_extension_true(self, tmp_path):
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_courseTranscript": _entity_schema(courseCode="string"),
        })
        ext_path = tmp_path / "ext.json"
        _write_swagger(ext_path, {
            "wi_courseTranscriptExtension": _entity_schema(sectionReference="string"),
        })
        catalog = build_catalog(core_path, "5.2", ext_path)
        assert catalog.has_state_extension("CourseTranscript")

    def test_has_state_extension_false(self, tmp_path):
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_student": _entity_schema(studentUniqueId="string"),
        })
        catalog = build_catalog(core_path, "5.2")
        assert not catalog.has_state_extension("Student")

    def test_get_state_extensions(self, tmp_path):
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_school": _entity_schema(schoolId="string"),
        })
        ext_path = tmp_path / "ext.json"
        _write_swagger(ext_path, {
            "az_schoolExtension": _entity_schema(campusType="string"),
        })
        catalog = build_catalog(core_path, "5.2", ext_path)
        exts = catalog.get_state_extensions_for_entity("School")
        assert len(exts) == 1
        assert exts[0].extends_entity == "School"


class TestTpdmPrefix:
    """Widened `_STATE_PREFIX` regex (2-5 chars) must strip `tpdm_` so
    `_resolve_extends_entity` returns the core entity being extended,
    not the self-referenced `Tpdm_*Extension` pascal form."""

    def test_tpdm_extension_resolves_to_core_entity(self, tmp_path):
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_credential": _entity_schema(credentialIdentifier="string"),
        })
        ext_path = tmp_path / "ext.json"
        _write_swagger(ext_path, {
            "tpdm_credentialExtension": _entity_schema(
                programGatewayIndicator="string",
            ),
        })
        catalog = build_catalog(core_path, "5.2", ext_path)
        assert "tpdm_credentialExtension" in catalog.extensions
        ext = catalog.extensions["tpdm_credentialExtension"]
        assert ext.extends_entity == "Credential"
        assert ext.source_prefix == "tpdm"

    def test_two_char_state_prefix_still_resolves(self, tmp_path):
        """Regression guard — widening to {2,5} must keep AZ/WI/MN/TX (2-char)
        prefixes working."""
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_calendar": _entity_schema(calendarCode="string"),
        })
        ext_path = tmp_path / "ext.json"
        _write_swagger(ext_path, {
            "wi_calendarExtension": _entity_schema(wiField="string"),
            "mn_calendarExtension": _entity_schema(mnField="string"),
            "az_calendarExtension": _entity_schema(azField="string"),
            "tx_calendarExtension": _entity_schema(txField="string"),
        })
        catalog = build_catalog(core_path, "5.2", ext_path)
        for key, prefix in [
            ("wi_calendarExtension", "wi"),
            ("mn_calendarExtension", "mn"),
            ("az_calendarExtension", "az"),
            ("tx_calendarExtension", "tx"),
        ]:
            assert catalog.extensions[key].extends_entity == "Calendar"
            assert catalog.extensions[key].source_prefix == prefix


class TestCatalogPluralLookup:
    """State element files often use plural entity names; the lookup index
    must resolve these to the singular canonical catalog entry."""

    def test_plural_entity_name_resolves_to_canonical(self, tmp_path):
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_studentSection504ProgramAssociation": _entity_schema(
                beginDate="string", endDate="string",
            ),
        })

        catalog = build_catalog(core_path, "5.2")

        assert catalog.lookup_index["studentsection504programassociation"] == (
            "StudentSection504ProgramAssociation"
        )
        assert catalog.lookup_index.get("studentsection504programassociations") == (
            "StudentSection504ProgramAssociation"
        )

    def test_plural_does_not_overwrite_canonical_mapping(self, tmp_path):
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_student": _entity_schema(studentUniqueId="string"),
            "edFi_students": _entity_schema(groupId="string"),
        })

        catalog = build_catalog(core_path, "5.2")
        assert "Student" in catalog.entities
        assert "Students" in catalog.entities
        assert catalog.lookup_index["students"] == "Students"


class TestSkipApiInfrastructure:
    """API response infrastructure (id, _etag/_lastModifiedDate, link) must
    be dropped at catalog build time. `_ext` is kept so reviewers can see
    the runtime extension-container sentinel in the spine."""

    def test_etag_and_lastModifiedDate_dropped_from_core_entity(self, tmp_path):
        # Older Ed-Fi ODS/API (MN v3) uses `_etag`; newer (AZ/WI/TX v5+)
        # uses `_lastModifiedDate`. Both must be dropped so catalog
        # cardinality stays anchored to real schema elements.
        core_path = tmp_path / "core.json"
        schema = {
            "type": "object",
            "required": [],
            "properties": {
                "id": {"type": "string"},
                "_etag": {"type": "string"},
                "_lastModifiedDate": {"type": "string", "format": "date-time"},
                "link": {"type": "object"},
                "studentUniqueId": {"type": "string"},
            },
        }
        _write_swagger(core_path, {"edFi_student": schema})
        catalog = build_catalog(core_path, "5.2")

        student = catalog.entities["Student"]
        props = set(student.properties.keys())
        assert "_etag" not in props
        assert "_lastModifiedDate" not in props
        assert "link" not in props
        assert "id" not in props
        assert "studentUniqueId" in props

    def test_ext_container_kept_for_review_visibility(self, tmp_path):
        # `_ext` is intentionally NOT in the skip list — reviewers should
        # see the runtime extension-container sentinel in the spine so they
        # can trace how extension values surface at API response time.
        # (Actual extension properties come through `extract_extensions`.)
        core_path = tmp_path / "core.json"
        ext_stub = {
            "type": "object",
            "properties": {},
        }
        schema = {
            "type": "object",
            "required": [],
            "properties": {
                "id": {"type": "string"},
                "studentUniqueId": {"type": "string"},
                "_ext": {"$ref": "#/components/schemas/edFi_studentExtensions"},
            },
        }
        _write_swagger(core_path, {
            "edFi_student": schema,
            "edFi_studentExtensions": ext_stub,
        })
        catalog = build_catalog(core_path, "5.2")
        student = catalog.entities["Student"]
        # `_ext` resolves as a sub-collection (it $ref's another schema).
        assert "_ext" in student.sub_collections or "_ext" in student.properties

    def test_etag_dropped_from_sub_collection_properties(self, tmp_path):
        # Sub-collection walks also strip infrastructure fields. Same
        # invariant as the top-level entity walk.
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_student": {
                "type": "object",
                "required": [],
                "properties": {
                    "id": {"type": "string"},
                    "addresses": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/edFi_studentAddress"},
                    },
                },
            },
            "edFi_studentAddress": {
                "type": "object",
                "properties": {
                    "_etag": {"type": "string"},
                    "_lastModifiedDate": {"type": "string"},
                    "streetNumberName": {"type": "string"},
                    "city": {"type": "string"},
                },
            },
        })
        catalog = build_catalog(core_path, "5.2")
        student = catalog.entities["Student"]
        sub = student.sub_collections["addresses"]
        props = set(sub.properties.keys())
        assert "_etag" not in props
        assert "_lastModifiedDate" not in props
        assert "streetNumberName" in props
        assert "city" in props

    def test_etag_dropped_from_state_extension(self, tmp_path):
        # State extensions go through a separate walk in
        # `extract_extensions` — infrastructure fields dropped there too.
        core_path = tmp_path / "core.json"
        _write_swagger(core_path, {
            "edFi_student": _entity_schema(studentUniqueId="string"),
        })
        ext_path = tmp_path / "ext.json"
        _write_swagger(ext_path, {
            "edFi_student": _entity_schema(studentUniqueId="string"),
            "tx_studentExtension": {
                "type": "object",
                "properties": {
                    "_etag": {"type": "string"},
                    "_lastModifiedDate": {"type": "string"},
                    "stateField": {"type": "string"},
                },
            },
        })
        catalog = build_catalog(core_path, "5.2", ext_path)
        ext = catalog.extensions["tx_studentExtension"]
        props = set(ext.properties.keys())
        assert "_etag" not in props
        assert "_lastModifiedDate" not in props
        assert "stateField" in props
