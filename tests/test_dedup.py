"""Tests for the shared post-unflatten dedup pass.

AZ's pre-unflatten dedup missed 53 `(entity, element_name)` collisions
that the unflatten loop introduced when it rewrote sub-entity names to
their catalog parent. MN had no dedup at all (28 duplicates flagged by
analyst review). Tier 2 moves dedup into a shared utility invoked from
both states' `run()` after unflatten completes.
"""

from src.models.element import ElementRecord
from src.utils.dedup import dedup_records
from tests.factories import make_record


def _rec(
    entity: str,
    element_name: str,
    *,
    source: str = "core",
    extension_name: str | None = None,
    definition: str = "",
    raw_entity: str | None = None,
) -> ElementRecord:
    return make_record(
        entity,
        element_name,
        domain="D",
        definition_text=definition,
        source=source,
        extension_name=extension_name,
        raw_entity=raw_entity,
    )


class TestDedupRecords:
    def test_no_duplicates_returns_unchanged(self):
        records = [
            _rec("A", "x"),
            _rec("A", "y"),
            _rec("B", "x"),
        ]
        out = dedup_records(records)
        assert len(out) == 3

    def test_collapses_same_entity_element_pair(self):
        records = [
            _rec("A", "x", source="core", definition="first"),
            _rec("A", "x", source="core", definition="second"),
        ]
        out = dedup_records(records)
        assert len(out) == 1

    def test_core_winner_preferred_over_extension(self):
        """When a core + extension record share a key, core wins."""
        records = [
            _rec("A", "x", source="extension", extension_name="az.Ext", definition="ext side"),
            _rec("A", "x", source="core", definition="core side"),
        ]
        out = dedup_records(records)
        assert len(out) == 1
        winner = out[0]
        assert winner.source == "core"
        # Core winner keeps extension_name=None; extension provenance lives in
        # the definition text so the boolean "Is an extension" stays truthful.
        assert winner.extension_name is None

    def test_extension_winner_preferred_over_unknown(self):
        records = [
            _rec("A", "x", source="unknown", definition="unresolved"),
            _rec("A", "x", source="extension", extension_name="wi_ext", definition="ext"),
        ]
        out = dedup_records(records)
        assert len(out) == 1
        assert out[0].source == "extension"
        assert out[0].extension_name == "wi_ext"

    def test_definition_text_merges_with_also_markers(self):
        records = [
            _rec("A", "x", source="core", definition="primary", raw_entity="edfi.A"),
            _rec("A", "x", source="extension", definition="secondary", raw_entity="az.AExtension"),
        ]
        out = dedup_records(records)
        assert "primary" in out[0].definition_text
        assert "secondary" in out[0].definition_text
        assert "az.AExtension" in out[0].definition_text  # audit trail

    def test_identical_definitions_not_duplicated(self):
        """When all duplicate records share the same definition text, the
        `[also: ...]` marker should not appear."""
        records = [
            _rec("A", "x", source="core", definition="same text"),
            _rec("A", "x", source="core", definition="same text"),
        ]
        out = dedup_records(records)
        assert out[0].definition_text == "same text"

    def test_extension_group_joins_contributing_extension_names(self):
        """When the winner is an extension and losers are other extensions,
        their names join with `; ` so analysts see all contributors."""
        records = [
            _rec("A", "x", source="extension", extension_name="az.Foo"),
            _rec("A", "x", source="extension", extension_name="az.Bar"),
        ]
        out = dedup_records(records)
        assert len(out) == 1
        assert "az.Foo" in out[0].extension_name
        assert "az.Bar" in out[0].extension_name

    def test_stable_ordering(self):
        """Output preserves the first-seen order of kept indices so downstream
        consumers can rely on stable iteration."""
        records = [
            _rec("A", "x", source="core"),
            _rec("B", "y", source="core"),
            _rec("A", "x", source="extension"),  # collapses into [0]
            _rec("C", "z", source="core"),
        ]
        out = dedup_records(records)
        entities = [r.entity for r in out]
        assert entities == ["A", "B", "C"]
