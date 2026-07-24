"""The assessment-release contract — declarative field rosters + schema.

Names the envelope/payload split the sidecar has always had in
*content* (scoring-boundary plan item A2, ADR 0016 commitment 5) the
way ``report/workbook_spec.py`` made the column contract declarative:

- **Envelope** — the frozen, queryable promise: header identity/
  version/aggregate fields plus per-record identity/scalar fields.
  Changing ANY roster here is a deliberate contract change: bump
  ``SIDECAR_CONTRACT_VERSION`` (ADR 0017), regenerate the committed
  JSON Schema (``scripts/refresh_release_contract_schema.py``), and
  update ``tests/test_release_contract.py`` in the same diff.
- **Payload** — method-private detail (``dimensions``,
  ``fact_provenance``, justifications…): churns freely within a
  contract version; the schema deliberately leaves its internals
  unconstrained.

The committed schema (``docs/contracts/assessment-release.schema.json``)
is the cross-repo handshake artifact: the Metadata Catalog CI contract
test validates pipeline emits against it, so envelope drift fails a
build on BOTH sides instead of surfacing mid-integration.

Import direction: this module may import version constants from
``aggregate`` but ``aggregate`` never imports this module — the
contract describes the emit, it doesn't participate in it.
"""

from __future__ import annotations

import copy
from typing import Any

from src.score.aggregate import ADJUSTMENT_DRIVER_TOKENS, SIDECAR_CONTRACT_VERSION

__all__ = [
    "SIDECAR_CONTRACT_VERSION",
    "ENVELOPE_HEADER_FIELDS",
    "GAP_ENVELOPE_HEADER_FIELDS",
    "ENVELOPE_RECORD_FIELDS",
    "PAYLOAD_RECORD_FIELDS",
    "RECORD_FIELDS",
    "build_json_schema",
]

# Header roster for the source / spine score sidecars — the exact key
# set (and emit order) of the ``aggregate.run()`` header dict.
ENVELOPE_HEADER_FIELDS: tuple[str, ...] = (
    "state",
    "lens",
    "edfi_version",
    "scored_at",
    "model",
    "prompt_version",
    "scoring_plan_version",
    "contract_version",
    "assessor_type",
    "assessor_id",
    "snapshot_id",
    "snapshot_digest",
    "release_id",
    "record_count",
    "scored_count",
    "skipped_count",
    "mean_quality_score",
    "needs_review_count",
    "dimension_stats",
    "in_scope_count",
    "nachos_score_histogram",
    "adjusted_nachos_score_histogram",
    "mean_nachos_score",
    "mean_adjusted_nachos_score",
    "documentation_gap_count",
)

# The spine-anchored gap sidecar's header variant (``aggregate_gap.run``):
# shares the identity/version prefix, drops the adjusted-score
# aggregates (deterministic-only Step 1 posture), adds the surfacer
# echo block.
GAP_ENVELOPE_HEADER_FIELDS: tuple[str, ...] = (
    "state",
    "lens",
    "edfi_version",
    "scored_at",
    "model",
    "prompt_version",
    "scoring_plan_version",
    "contract_version",
    "assessor_type",
    "assessor_id",
    "snapshot_id",
    "snapshot_digest",
    "release_id",
    "record_count",
    "scored_count",
    "skipped_count",
    "mean_quality_score",
    "needs_review_count",
    "dimension_stats",
    "in_scope_count",
    "nachos_score_histogram",
    "mean_nachos_score",
    "gap_source_generated_at",
    "gap_discovery_counts",
    "step",
)

# Per-record envelope: identity + the stakeholder-queryable scalars.
# Contract v3 (issue #318, ADR 0020) appends the four MC-contract
# additions: tier_name, adjustment_drivers, extension_necessity,
# data_completeness.
ENVELOPE_RECORD_FIELDS: tuple[str, ...] = (
    "record_key",
    "entity",
    "element_name",
    "complexity_score",
    "adjusted_nachos_score",
    "in_scope",
    "confidence_composite",
    "discovery_lens",
    "documentation_source",
    "tier_name",
    "adjustment_drivers",
    "extension_necessity",
    "data_completeness",
)

# Per-record payload: method-private detail. Free to churn within a
# contract version — the schema constrains only their presence.
PAYLOAD_RECORD_FIELDS: tuple[str, ...] = (
    "dimensions",
    "fact_provenance",
    "review",
    "nachos_justification",
    "_quality_mean_diagnostic",
)

RECORD_FIELDS: tuple[str, ...] = ENVELOPE_RECORD_FIELDS + PAYLOAD_RECORD_FIELDS


def _nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


_HEADER_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "state": {"type": "string"},
    "lens": {"enum": ["source", "spine"]},
    "edfi_version": _nullable("string"),
    "scored_at": {"type": "string"},
    "model": {"type": "string"},
    "prompt_version": {"type": "string"},
    "scoring_plan_version": {"type": "string"},
    "contract_version": {"const": SIDECAR_CONTRACT_VERSION},
    # A3 identity block (ADR 0018). assessor_type is an open string —
    # "engine" today; "second-model" (B4 challenger), "human",
    # "synthesis" are ordinary future values, not schema changes.
    "assessor_type": {"type": "string"},
    "assessor_id": {"type": "string"},
    "snapshot_id": _nullable("string"),
    "snapshot_digest": _nullable("string"),
    "release_id": {"type": "string"},
    "record_count": {"type": "integer"},
    "scored_count": {"type": "integer"},
    "skipped_count": {"type": "integer"},
    "mean_quality_score": _nullable("number"),
    "needs_review_count": {"type": "integer"},
    "dimension_stats": {"type": "object"},
    "in_scope_count": {"type": "integer"},
    "nachos_score_histogram": {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    },
    "adjusted_nachos_score_histogram": {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    },
    "mean_nachos_score": _nullable("number"),
    "mean_adjusted_nachos_score": _nullable("number"),
    "documentation_gap_count": {"type": "integer"},
}

_ENVELOPE_RECORD_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "record_key": {"type": "string", "pattern": r"^[^|]+\|[^|]+\|.+$"},
    "entity": {"type": "string"},
    "element_name": {"type": "string"},
    "complexity_score": _nullable("integer"),
    "adjusted_nachos_score": _nullable("number"),
    "in_scope": {"type": "boolean"},
    "confidence_composite": {"enum": ["high", "medium", "low"]},
    "discovery_lens": {"type": "string"},
    "documentation_source": {"type": "string"},
    # Contract v3 (issue #318, ADR 0020). tier_name is deliberately a
    # nullable STRING, not an enum: the cascade token set is
    # SCORING_PLAN_VERSION (methodology) territory, and pinning it here
    # would force a contract bump on every cascade edit — violating the
    # ADR 0017 knob split. The token→label vocabulary ships as data in
    # methodology_{version}.json instead.
    "tier_name": _nullable("string"),
    "adjustment_drivers": {
        "type": "array",
        "items": {"enum": list(ADJUSTMENT_DRIVER_TOKENS)},
    },
    "extension_necessity": {
        "enum": ["necessary", "unnecessary", "unresolved", None],
    },
    # This pipeline emits pipeline_full / pipeline_machine_only;
    # legacy_import is reserved for MC-side backfill rows. The full
    # enum is committed so this schema and MC's stay identical.
    "data_completeness": {
        "enum": ["pipeline_full", "pipeline_machine_only", "legacy_import"],
    },
}


def header_field_schemas() -> dict[str, dict[str, Any]]:
    """Deep copy of the per-header-field schema fragments (issue #318 S4).

    Public accessor so ``publish/pipeline_records_contract.py`` can
    reuse the SAME fragments for the gap-sidecar def instead of
    restating them (the two schemas must not drift). A DEEP copy is
    load-bearing: a shallow one leaves nested objects and enum lists
    aliasing the module globals, so a caller's mutation would leak into
    the frozen release contract (caught in PR #330 review — a token
    appended through ``record_field_schemas()`` surfaced in a later
    ``build_json_schema()``)."""
    return copy.deepcopy(_HEADER_FIELD_SCHEMAS)


def record_field_schemas() -> dict[str, dict[str, Any]]:
    """Deep copy of the per-record envelope field fragments (issue #318
    S4); see :func:`header_field_schemas` for why deep."""
    return copy.deepcopy(_ENVELOPE_RECORD_FIELD_SCHEMAS)


def build_json_schema() -> dict[str, Any]:
    """The release contract as a JSON Schema (draft 2020-12).

    Frozen-envelope semantics: header and per-record key sets are
    exact (``additionalProperties: false`` + every field required);
    payload field INTERNALS are deliberately unconstrained. The
    committed copy at ``docs/contracts/assessment-release.schema.json``
    must equal this function's output byte-for-byte after JSON
    round-trip — ``tests/test_release_contract.py`` enforces the
    same-diff discipline, ``scripts/refresh_release_contract_schema.py``
    regenerates.
    """
    record_properties: dict[str, Any] = dict(_ENVELOPE_RECORD_FIELD_SCHEMAS)
    for payload_field in PAYLOAD_RECORD_FIELDS:
        record_properties[payload_field] = {
            "description": (
                "method-private payload — free to churn within a "
                "contract version"
            )
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": (
            "https://github.com/touchdownllc/nachos-ai-poc-3/blob/dev/"
            "docs/contracts/assessment-release.schema.json"
        ),
        "title": "POC-3 assessment release (score sidecar)",
        "description": (
            "Envelope contract for one scoring release: the "
            "{state}_scores_{source,spine}.json artifact. Envelope "
            "fields are frozen per contract version (ADR 0017); "
            "payload fields are method-private."
        ),
        "x-sidecar-contract-version": SIDECAR_CONTRACT_VERSION,
        "type": "object",
        "properties": {
            **_HEADER_FIELD_SCHEMAS,
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": record_properties,
                    "required": list(RECORD_FIELDS),
                    "additionalProperties": False,
                },
            },
        },
        "required": [*ENVELOPE_HEADER_FIELDS, "scores"],
        "additionalProperties": False,
    }
