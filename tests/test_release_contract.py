"""Frozen release-contract test (scoring-boundary plan item A2).

The sidecar-envelope mirror of ``test_record_schema.py``: expected-field
frozensets restated HERE (not imported), length pins, missing/extra
diffs, plus live emits checked against the rosters and the committed
JSON Schema. From this point envelope drift fails a build instead of
surfacing at Metadata Catalog integration.

Changing any set below is a deliberate contract change: bump
``SIDECAR_CONTRACT_VERSION`` (ADR 0017), regenerate the schema via
``scripts/refresh_release_contract_schema.py``, and update this file —
all in the same diff.

MC PORT NOTE: this slice ports the contract *module* (rosters + schema
builder) and its committed schema. The live-emit classes below are
skipped until the aggregate ``run()`` actually emits the v3 envelope
(``compute_release_id`` / ``snapshot_identity`` / the 25-header,
13-record key set) — that lands with the aggregate envelope-identity
slice of MC-21. Unskip both classes in that diff.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from src.models.edfi_catalog import EdFiCatalog, EntityEntry, PropertyInfo
from src.models.spine import SpineSourceURLs, StateSpine
from src.score import release_contract
from src.score.aggregate import run as run_aggregate
from src.score.aggregate_gap import run as run_gap
from tests.test_score_aggregate import _seed_minimal_fact_pool

_EMIT_PENDING = (
    "live-emit assertions need the aggregate v3 emit (compute_release_id, "
    "snapshot_identity, and the 25-header / 13-record key set) — those land "
    "with the aggregate envelope-identity slice of MC-21. This slice ports "
    "the contract module + rosters + committed schema only; unskip here when "
    "the emit lands."
)

# Contract v2 (A3 / ADR 0018) adds the five identity fields:
# assessor_type, assessor_id, snapshot_id, snapshot_digest, release_id.
EXPECTED_ENVELOPE_HEADER: frozenset[str] = frozenset({
    "state", "lens", "edfi_version", "scored_at", "model",
    "prompt_version", "scoring_plan_version", "contract_version",
    "assessor_type", "assessor_id", "snapshot_id", "snapshot_digest",
    "release_id",
    "record_count", "scored_count", "skipped_count",
    "mean_quality_score", "needs_review_count", "dimension_stats",
    "in_scope_count", "nachos_score_histogram",
    "adjusted_nachos_score_histogram", "mean_nachos_score",
    "mean_adjusted_nachos_score", "documentation_gap_count",
})
assert len(EXPECTED_ENVELOPE_HEADER) == 25  # surface the count in one place

EXPECTED_GAP_HEADER: frozenset[str] = frozenset({
    "state", "lens", "edfi_version", "scored_at", "model",
    "prompt_version", "scoring_plan_version", "contract_version",
    "assessor_type", "assessor_id", "snapshot_id", "snapshot_digest",
    "release_id",
    "record_count", "scored_count", "skipped_count",
    "mean_quality_score", "needs_review_count", "dimension_stats",
    "in_scope_count", "nachos_score_histogram", "mean_nachos_score",
    "gap_source_generated_at", "gap_discovery_counts", "step",
})
assert len(EXPECTED_GAP_HEADER) == 25

# Contract v3 (issue #318 / ADR 0020) adds the four MC-contract
# per-record fields: tier_name, adjustment_drivers,
# extension_necessity, data_completeness.
EXPECTED_ENVELOPE_RECORD: frozenset[str] = frozenset({
    "record_key", "entity", "element_name", "complexity_score",
    "adjusted_nachos_score", "in_scope", "confidence_composite",
    "discovery_lens", "documentation_source",
    "tier_name", "adjustment_drivers", "extension_necessity",
    "data_completeness",
})
assert len(EXPECTED_ENVELOPE_RECORD) == 13

EXPECTED_PAYLOAD_RECORD: frozenset[str] = frozenset({
    "dimensions", "fact_provenance", "review", "nachos_justification",
    "_quality_mean_diagnostic",
})
assert len(EXPECTED_PAYLOAD_RECORD) == 5

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "contracts"
    / "assessment-release.schema.json"
)


def _diff_msg(surface: str, keys: set[str], expected: frozenset[str]) -> str:
    missing = expected - keys
    extra = keys - expected
    return f"{surface}: missing={sorted(missing)} extra={sorted(extra)}"


class TestFrozenRosters:
    """The module rosters ARE the contract — pinned against the
    restated frozensets so a roster edit is always a visible,
    deliberate diff here too."""

    def test_envelope_header_roster(self) -> None:
        keys = set(release_contract.ENVELOPE_HEADER_FIELDS)
        assert keys == EXPECTED_ENVELOPE_HEADER, _diff_msg(
            "ENVELOPE_HEADER_FIELDS", keys, EXPECTED_ENVELOPE_HEADER
        )
        assert len(release_contract.ENVELOPE_HEADER_FIELDS) == 25

    def test_gap_header_roster(self) -> None:
        keys = set(release_contract.GAP_ENVELOPE_HEADER_FIELDS)
        assert keys == EXPECTED_GAP_HEADER, _diff_msg(
            "GAP_ENVELOPE_HEADER_FIELDS", keys, EXPECTED_GAP_HEADER
        )
        assert len(release_contract.GAP_ENVELOPE_HEADER_FIELDS) == 25

    def test_record_rosters(self) -> None:
        env = set(release_contract.ENVELOPE_RECORD_FIELDS)
        pay = set(release_contract.PAYLOAD_RECORD_FIELDS)
        assert env == EXPECTED_ENVELOPE_RECORD, _diff_msg(
            "ENVELOPE_RECORD_FIELDS", env, EXPECTED_ENVELOPE_RECORD
        )
        assert pay == EXPECTED_PAYLOAD_RECORD, _diff_msg(
            "PAYLOAD_RECORD_FIELDS", pay, EXPECTED_PAYLOAD_RECORD
        )
        assert not env & pay, "a field cannot be both envelope and payload"
        assert set(release_contract.RECORD_FIELDS) == env | pay


@pytest.mark.skip(reason=_EMIT_PENDING)
class TestLiveEmitMatchesContract:
    """``aggregate.run()`` / ``aggregate_gap.run()`` emit EXACTLY the
    contracted key sets — no silent header or record-field drift. The
    header serializer is lens-independent, so the spine fixture covers
    both primary lenses."""

    def _spine_sidecar(self, tmp_path: Path) -> dict:
        _seed_minimal_fact_pool(tmp_path)
        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        return json.loads(out_path.read_text(encoding="utf-8"))

    def test_header_keys_exact(self, tmp_path: Path) -> None:
        payload = self._spine_sidecar(tmp_path)
        keys = set(payload) - {"scores"}
        assert keys == EXPECTED_ENVELOPE_HEADER, _diff_msg(
            "sidecar header", keys, EXPECTED_ENVELOPE_HEADER
        )

    def test_record_keys_exact(self, tmp_path: Path) -> None:
        payload = self._spine_sidecar(tmp_path)
        assert payload["scores"], "fixture emitted no records"
        for i, record in enumerate(payload["scores"]):
            keys = set(record)
            expected = EXPECTED_ENVELOPE_RECORD | EXPECTED_PAYLOAD_RECORD
            assert keys == expected, _diff_msg(f"record[{i}]", keys, expected)

    def test_sidecar_validates_against_committed_schema(
        self, tmp_path: Path
    ) -> None:
        payload = self._spine_sidecar(tmp_path)
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(payload, schema)

    def test_contract_v3_field_values(self, tmp_path: Path) -> None:
        """Contract v3 (issue #318 / ADR 0020) value shapes on a live
        emit: drivers ⊆ the canonical token enum, tier_name mirrors the
        nachos_score rule token, extension_necessity is enum-or-null
        (null on core rows), data_completeness is pipeline_full for the
        five registered-baseline states."""
        from src.score.aggregate import ADJUSTMENT_DRIVER_TOKENS

        payload = self._spine_sidecar(tmp_path)
        for record in payload["scores"]:
            assert set(record["adjustment_drivers"]) <= set(
                ADJUSTMENT_DRIVER_TOKENS
            )
            nachos = record["dimensions"].get("nachos_score")
            expected_tier = nachos["rule_matched"] if nachos else None
            assert record["tier_name"] == expected_tier
            assert record["extension_necessity"] in (
                "necessary", "unnecessary", "unresolved", None,
            )
            assert record["data_completeness"] == "pipeline_full"

    def test_gap_header_keys_exact(self, tmp_path: Path) -> None:
        catalog = EdFiCatalog(
            version="4.0",
            entity_count=1,
            extension_count=0,
            entities={
                "Calendar": EntityEntry(
                    properties={"calendarCode": PropertyInfo()}
                )
            },
            extensions={},
        )
        spine = StateSpine(
            state="TX",
            edfi_version="4.0",
            fetched_at=datetime.now(tz=timezone.utc),
            source_urls=SpineSourceURLs(resources="http://test/r.json"),
            catalog=catalog,
        )
        spine_path = tmp_path / "tx_spine.json"
        spine_path.write_text(spine.model_dump_json())
        gap_path = tmp_path / "tx_elements_gap.json"
        gap_path.write_text(
            json.dumps({
                "state": "TX",
                "generated_at": "2026-04-29T00:00:00Z",
                "gap_count": 1,
                "discovery_counts": {"spine_within_documented_entity": 1},
                "gaps": [{
                    "state": "TX",
                    "entity": "Calendar",
                    "element_name": "calendarCode",
                    "discovery": "spine_within_documented_entity",
                    "documented_in_source": False,
                    "spine_data_type": "String",
                    "spine_extension_name": None,
                    "rationale": "x",
                }],
            }),
            encoding="utf-8",
        )
        out_path = tmp_path / "tx_scores_gap.json"
        run_gap(
            state="TX",
            gap_path=gap_path,
            spine_path=spine_path,
            out_path=out_path,
        )
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        keys = set(payload) - {"scores"}
        assert keys == EXPECTED_GAP_HEADER, _diff_msg(
            "gap sidecar header", keys, EXPECTED_GAP_HEADER
        )


class TestSchemaSameDiffDiscipline:
    def test_committed_schema_matches_module(self) -> None:
        """The committed schema is generated, never hand-edited — it
        must equal ``build_json_schema()`` exactly (regenerate via
        ``scripts/refresh_release_contract_schema.py``)."""
        committed = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        assert committed == release_contract.build_json_schema()

    def test_schema_is_versioned_by_the_contract_knob(self) -> None:
        committed = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        assert (
            committed["x-sidecar-contract-version"]
            == release_contract.SIDECAR_CONTRACT_VERSION
        )

    def test_schema_rejects_header_drift(self) -> None:
        """additionalProperties: false is the frozen-envelope teeth —
        an uncontracted header key must fail validation."""
        schema = release_contract.build_json_schema()
        minimal_ok = {
            **{
                "state": "AZ", "lens": "spine", "edfi_version": "4.0",
                "scored_at": "2026-07-19T00:00:00Z", "model": "m",
                "prompt_version": "p",
                "scoring_plan_version": "30",
                "contract_version":
                    release_contract.SIDECAR_CONTRACT_VERSION,
                "assessor_type": "engine",
                "assessor_id": "poc3-nachos",
                "snapshot_id": None,
                "snapshot_digest": None,
                "release_id": "r" * 64,
                "record_count": 0, "scored_count": 0, "skipped_count": 0,
                "mean_quality_score": None, "needs_review_count": 0,
                "dimension_stats": {}, "in_scope_count": 0,
                "nachos_score_histogram": {},
                "adjusted_nachos_score_histogram": {},
                "mean_nachos_score": None,
                "mean_adjusted_nachos_score": None,
                "documentation_gap_count": 0,
            },
            "scores": [],
        }
        jsonschema.validate(minimal_ok, schema)
        drifted = {**minimal_ok, "surprise_header_key": 1}
        try:
            jsonschema.validate(drifted, schema)
        except jsonschema.ValidationError:
            return
        raise AssertionError(
            "schema accepted an uncontracted header key — the frozen "
            "envelope has no teeth"
        )


@pytest.mark.skip(reason=_EMIT_PENDING)
class TestSnapshotDigestStability:
    """Plan §9 risk: if canonicalization misses a volatile field,
    re-ingests mint spurious 'new snapshots'. Same content must mean
    same digest across re-serialization, key order, and timestamp
    churn — and only a real content change may move it."""

    _CONTENT = {
        "state": "AZ",
        "edfi_version": "4.0",
        "element_count": 1,
        "elements": [{"entity": "Student", "element_name": "id"}],
    }

    def _write(self, path, *, extracted_at: str, sort: bool, indent) -> None:
        payload = {**self._CONTENT, "extracted_at": extracted_at}
        path.write_text(
            json.dumps(payload, sort_keys=sort, indent=indent),
            encoding="utf-8",
        )

    def test_reserialization_reproduces_the_digest(self, tmp_path: Path) -> None:
        from src.utils.artifacts import snapshot_identity

        a = tmp_path / "az_elements_source.json"
        b = tmp_path / "az_elements_source_reingest.json"
        self._write(a, extracted_at="2026-01-01T00:00:00", sort=False, indent=2)
        # Re-ingest: new timestamp, different key order + formatting.
        self._write(b, extracted_at="2026-07-19T09:00:00", sort=True, indent=None)
        _id_a, digest_a = snapshot_identity(a, consumer="test")
        _id_b, digest_b = snapshot_identity(b, consumer="test")
        assert digest_a == digest_b

    def test_content_change_moves_the_digest(self, tmp_path: Path) -> None:
        from src.utils.artifacts import snapshot_identity

        a = tmp_path / "az_elements_source.json"
        b = tmp_path / "az_elements_source_changed.json"
        self._write(a, extracted_at="2026-01-01T00:00:00", sort=False, indent=2)
        changed = {**self._CONTENT, "element_count": 2}
        b.write_text(
            json.dumps({**changed, "extracted_at": "2026-01-01T00:00:00"}),
            encoding="utf-8",
        )
        _id_a, digest_a = snapshot_identity(a, consumer="test")
        _id_b, digest_b = snapshot_identity(b, consumer="test")
        assert digest_a != digest_b

    def test_missing_artifact_is_none_none(self, tmp_path: Path) -> None:
        from src.utils.artifacts import snapshot_identity

        assert snapshot_identity(
            tmp_path / "absent.json", consumer="test"
        ) == (None, None)

    def test_release_id_is_deterministic_and_identity_sensitive(self) -> None:
        from src.score.aggregate import compute_release_id

        kwargs = dict(
            state="AZ", lens="source", snapshot_digest="d" * 64,
            scoring_plan_version="30", contract_version="2",
            prompt_version="phase-a.v1", deterministic_version="det.v11",
            model="claude-sonnet-4-6", assessor_type="engine",
            assessor_id="poc3-nachos",
        )
        first = compute_release_id(**kwargs)
        assert first == compute_release_id(**kwargs)  # deterministic
        assert first != compute_release_id(
            **{**kwargs, "snapshot_digest": "e" * 64}
        )
        assert first != compute_release_id(
            **{**kwargs, "assessor_id": "second-model"}
        )
