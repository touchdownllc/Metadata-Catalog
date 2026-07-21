"""Tests for the Ed-Fi Data Standard domain map fallback.

WI/MN sandbox swagger strips `x-Ed-Fi-domains`. AZ's swagger carries them on
every tag; `build_domain_map` extracts a canonical
`{PascalName: [domains]}` asset from a domain-rich source, and
`apply_domain_map` patches entities with empty `domains[]` at spine-build
time. Existing non-empty assignments must be preserved.
"""

import json
from pathlib import Path

from src.models.edfi_catalog import EntityEntry
from src.spine.build import apply_domain_map, build_domain_map


def _write_swagger_with_domain_tags(path: Path, tag_domains: dict[str, list[str]]) -> None:
    """Write a Swagger 2.0 swagger where each tag carries an x-Ed-Fi-domains list.

    The tag name is the camelCase plural of the schema key (stripped of edFi_),
    matching how the Ed-Fi Alliance ODS emits them.
    """
    schemas = {}
    tags = []
    for tag_name, domains in tag_domains.items():
        # tag_name like "students" -> schema "edFi_student"
        singular = tag_name.rstrip("s")
        schemas[f"edFi_{singular}"] = {
            "type": "object",
            "required": [],
            "properties": {"id": {"type": "string"}, f"{singular}UniqueId": {"type": "string"}},
        }
        tags.append({"name": tag_name, "x-Ed-Fi-domains": domains})
    swagger = {
        "swagger": "2.0",
        "info": {"title": "test", "version": "1.0"},
        "paths": {},
        "definitions": schemas,
        "tags": tags,
    }
    path.write_text(json.dumps(swagger))


class TestBuildDomainMap:
    def test_extracts_domains_from_tags(self, tmp_path):
        sw = tmp_path / "az.json"
        _write_swagger_with_domain_tags(
            sw,
            {
                "students": ["Student"],
                "schools": ["EducationOrganization"],
                "assessments": ["Assessment", "AssessmentMetadata"],
            },
        )
        mapping = build_domain_map(sw)
        assert mapping["Student"] == ["Student"]
        assert mapping["School"] == ["EducationOrganization"]
        assert mapping["Assessment"] == ["Assessment", "AssessmentMetadata"]

    def test_skips_entities_without_domains(self, tmp_path):
        """Entities whose tags lack x-Ed-Fi-domains are omitted from the map."""
        sw = tmp_path / "mixed.json"
        swagger = {
            "swagger": "2.0",
            "info": {"title": "t", "version": "1.0"},
            "paths": {},
            "definitions": {
                "edFi_student": {"type": "object", "properties": {"id": {"type": "string"}}},
                "edFi_school": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
            "tags": [
                {"name": "students", "x-Ed-Fi-domains": ["Student"]},
                {"name": "schools"},
            ],
        }
        sw.write_text(json.dumps(swagger))
        mapping = build_domain_map(sw)
        assert "Student" in mapping
        assert "School" not in mapping


class TestApplyDomainMap:
    def test_fills_empty_domains(self):
        entities = {
            "Student": EntityEntry(domains=[]),
            "School": EntityEntry(domains=[]),
        }
        domain_map = {"Student": ["Student"], "School": ["EducationOrganization"]}
        filled = apply_domain_map(entities, domain_map)
        assert filled == 2
        assert entities["Student"].domains == ["Student"]
        assert entities["School"].domains == ["EducationOrganization"]

    def test_preserves_existing_domains(self):
        """Entities with non-empty domains are never overwritten (AZ stays AZ)."""
        entities = {"Student": EntityEntry(domains=["CustomDomain"])}
        filled = apply_domain_map(entities, {"Student": ["Student"]})
        assert filled == 0
        assert entities["Student"].domains == ["CustomDomain"]

    def test_ignores_entities_not_in_map(self):
        """State-specific / extension-only entities with no map entry stay empty."""
        entities = {"MnStudentADSISProgramAssociation": EntityEntry(domains=[])}
        filled = apply_domain_map(entities, {"Student": ["Student"]})
        assert filled == 0
        assert entities["MnStudentADSISProgramAssociation"].domains == []


class TestParentPrefixInheritance:
    """Sub-collection entities (named `{Parent}{SubName}`) aren't individually
    tagged in swagger — only the top-level Parent gets an x-Ed-Fi-domains tag.
    Without fallback inheritance, AZ's 341 sub-entities dominate the
    `Entities by Domain` sheet as `(unassigned)` (reviewer 1 flagged this as
    the single highest-leverage fix)."""

    def test_sub_entity_inherits_parent_domains(self):
        entities = {
            "ApplicantProfile": EntityEntry(domains=[]),
            "ApplicantProfileAddress": EntityEntry(domains=[]),
            "ApplicantProfileAddressPeriod": EntityEntry(domains=[]),
        }
        domain_map = {"ApplicantProfile": ["RecruitingAndStaffing"]}
        filled = apply_domain_map(entities, domain_map)
        # Parent + two sub-entities all resolve.
        assert filled == 3
        assert entities["ApplicantProfile"].domains == ["RecruitingAndStaffing"]
        assert entities["ApplicantProfileAddress"].domains == ["RecruitingAndStaffing"]
        assert entities["ApplicantProfileAddressPeriod"].domains == ["RecruitingAndStaffing"]

    def test_longest_prefix_wins_over_shorter(self):
        """`StudentSchoolAssociation` is preferred over `Student` for a
        `StudentSchoolAssociationExtension` lookup."""
        entities = {
            "StudentSchoolAssociationExtension": EntityEntry(domains=[]),
        }
        domain_map = {
            "Student": ["Student"],
            "StudentSchoolAssociation": ["EducationOrgRelationship"],
        }
        filled = apply_domain_map(entities, domain_map)
        assert filled == 1
        # Longest-prefix wins.
        assert (
            entities["StudentSchoolAssociationExtension"].domains
            == ["EducationOrgRelationship"]
        )

    def test_no_prefix_match_leaves_empty(self):
        """No longest-prefix match means the entity stays unassigned."""
        entities = {"CompletelyUnrelatedThing": EntityEntry(domains=[])}
        filled = apply_domain_map(entities, {"Student": ["Student"]})
        assert filled == 0
        assert entities["CompletelyUnrelatedThing"].domains == []

    def test_requires_pascal_boundary(self):
        """The prefix must end at a PascalCase boundary (uppercase transition)
        so `Stu` doesn't accidentally match `StudentFoo`."""
        entities = {"StudentFoo": EntityEntry(domains=[])}
        domain_map = {"Stu": ["WrongDomain"]}
        filled = apply_domain_map(entities, domain_map)
        # `Stu` + `dentFoo` is NOT a valid Pascal boundary split.
        assert filled == 0
        assert entities["StudentFoo"].domains == []

    def test_existing_domains_still_preserved_under_fallback(self):
        """Parent-prefix inheritance must not overwrite existing assignments."""
        entities = {
            "ApplicantProfile": EntityEntry(domains=["Original"]),
            "ApplicantProfileAddress": EntityEntry(domains=["AlreadyAssigned"]),
        }
        filled = apply_domain_map(entities, {"ApplicantProfile": ["RecruitingAndStaffing"]})
        assert filled == 0
        assert entities["ApplicantProfile"].domains == ["Original"]
        assert entities["ApplicantProfileAddress"].domains == ["AlreadyAssigned"]


class TestCanonicalDomainMapAsset:
    """The committed data/spine/edfi_domain_map.json asset must load cleanly and
    cover at least the core Ed-Fi entities WI/MN ingest relies on."""

    def test_asset_loads_and_covers_core_entities(self):
        repo_root = Path(__file__).resolve().parents[1]
        asset = repo_root / "data" / "spine" / "edfi_domain_map.json"
        assert asset.exists(), "Regenerate via `mc spine domain-map --source AZ`"
        mapping = json.loads(asset.read_text())
        # A handful of anchor entities any reasonable Ed-Fi deployment ships.
        for name in ("Student", "School", "LocalEducationAgency", "Course", "StudentSchoolAssociation"):
            assert name in mapping, f"domain map missing core entity {name}"
            assert mapping[name], f"{name} maps to empty domain list"
