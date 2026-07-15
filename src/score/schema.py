"""Strict JSON Schema + typed errors for the extraction harness.

Drift halts the run — we raise `ScoringSchemaError` with the offending
payload rather than silently `.get()`-defaulting. POC-2 burned on trust
in LLM output shape; POC-3 verifies every response.

Phase B generalizes the schema: each LLM fact's response payload uses
the fact name as the value key (``has_conditional_logic``,
``definition_is_implementable``, ``cross_entity_targets``, …). Build
one via ``fact_output_schema(fact_name)`` — the harness calls this at
runtime per fact. ``FACT_OUTPUT_SCHEMA`` stays exported as the
Phase A ``has_conditional_logic`` shape for backward compat.
"""

from __future__ import annotations

from typing import Any


def fact_output_schema(
    fact: str,
    *,
    value_type: str = "boolean",
    value_enum: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a JSON Schema for a single LLM fact's response payload.

    ``value_type``: ``"boolean"`` for every Phase B bool fact,
    ``"integer"`` for ``cross_entity_targets`` (count-int), or
    ``"string"`` for Phase C2's ``semantic_class`` enum3. Pair
    ``value_type="string"`` with ``value_enum`` to pin the allowed
    tokens.

    Additional properties are tolerated (not rejected) — the cold-run
    TX fanout surfaced an LLM hallucination where the model echoed back
    ``data_type`` on every item in one batch and crashed the pair at
    500/594 records. Dropping ``additionalProperties: False`` lets the
    run keep going while ``_validate_payload`` logs a warning naming
    the unexpected keys so polarity / schema drift is still visible in
    the run logs. The strict keys — required presence, fact value
    type, spans-array shape, confidence enum — remain load-bearing and
    still raise ``ScoringSchemaError`` on a real drift.
    """
    fact_schema: dict[str, Any] = {"type": value_type}
    if value_enum is not None:
        fact_schema["enum"] = list(value_enum)
    return {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["element_name", fact, "spans", "confidence"],
            "properties": {
                "element_name": {"type": "string", "minLength": 1},
                fact: fact_schema,
                "spans": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
            },
        },
    }


# Known-safe keys per item — ``element_name``, ``spans``, ``confidence``,
# and the fact-name key itself. Anything else is a hallucinated
# additional property; ``_validate_payload`` logs a warning (not raises)
# so a cold run doesn't die on a pair that's 94% of the way done.
EXPECTED_ITEM_KEYS_BASE: frozenset[str] = frozenset(
    {"element_name", "spans", "confidence"}
)


def expected_item_keys(fact: str) -> frozenset[str]:
    """Return the set of keys a payload item is allowed to carry for ``fact``.

    Used by ``_validate_payload`` to surface unexpected-key warnings
    without failing the run.
    """
    return EXPECTED_ITEM_KEYS_BASE | {fact}


# Phase A's fixed-shape export. The harness now routes through
# ``fact_output_schema`` at runtime; this constant is preserved for
# tests and any external consumer that imports it by name.
FACT_OUTPUT_SCHEMA: dict[str, Any] = fact_output_schema("has_conditional_logic")


# Facts whose value is an integer (count-int) rather than boolean.
# Kept here rather than in ``extract.py`` so schema + extract share one
# source of truth.
INT_VALUED_FACTS: frozenset[str] = frozenset({"cross_entity_targets"})


# than boolean. Plan §6.1 declared ``semantic_class`` as the first
# non-bool, non-int fact. H1 / Session 3 added ``integration_class`` —
# the productization signal. v2 Day 2 (docs/archive/nachos-v2-two-axis-plan.md
# §4 Mitigation 2) added ``documentation_style``, a five-category
# classifier feeding the ``documentation_style_tier`` dimension.
# Register here alongside allowed values; the extract harness reads
# the tuple as the schema enum and the polarity registries below
# (``ENUM_NO_EVIDENCE_VALUES``, ``ENUM_SPANS_OPTIONAL_VALUES``) drive
# per-value downgrade semantics.
ENUM_VALUED_FACTS: dict[str, tuple[str, ...]] = {
    "semantic_class": ("aligned", "divergent", "not_applicable"),
    "integration_class": (
        "sis_native",
        "sis_custom_extension",
        "descriptor_mapped",
        "concatenation",
        "calculation",
        "conditional_derivation",
        "cross_entity_reference",
        "unknown",
    ),
    "documentation_style": (
        "prescriptive",
        "conceptual",
        "cross_reference",
        "regulatory",
        "unspecified",
    ),
    # Side-quest PR A (2026-04-28): merged from `state_narrows_edfi_scope`
    # + `state_broadens_edfi_scope` into a single mutually-exclusive enum.
    # "neutral" is the no-evidence default (registered below); narrows /
    # broadens require >= 1 verbatim span. Consumed by `semantic_fidelity`.
    "state_scope_delta": ("narrows", "broadens", "neutral"),
    # Issue #59 (2026-04-29): typed refinement of `semantic_fidelity =
    # divergent_*`. Names the mechanism behind a documented divergence
    # so reviewers can route to the right transformation work instead
    # of a generic "review the divergence." Source-lens only;
    # observability-only landing (path #1 from the issue) — surfaces in
    # `fact_provenance` and per-mechanism Recommendations templates,
    # but does NOT feed any rule cascade.
    "sourcing_constraint_documented": (
        "transformation",       # value coercion / mapping recipe (AZ SchoolYear "2015-2016" -> "2016")
        "field_filter",         # cross-field constraint at the source-row level (WI email org-type filter)
        "external_sourcing",    # value sourced off the SIS row from a separate authority (TX home-language survey)
        "custom_enumeration",   # state-specific enumerated value set not in the Ed-Fi descriptor
        "none",                 # no constraint documented (no-evidence default — empty spans)
        "unspecified",          # source flags divergence but doesn't pin down the mechanism (no-evidence default — empty spans)
    ),
    # Issue #124 PR 2 / #111 (2026-05-02): deterministic enum produced
    # by ``deterministic.extension_fidelity_divergence`` (det.v11). Names
    # the shape-divergence mechanism behind an extension that replaces
    # a core field with a different data-type bucket. Surfaces as a
    # typed-reason annotation in the v12 SF fold label (source-lens
    # only) inside ``aggregate._compute_nachos_adjustments``.
    # Registered here for symmetry with the LLM enum facts even though
    # this fact bypasses the LLM extraction schema; the runner reads
    # the entry to know about the value space when serializing.
    "extension_fidelity_divergence": (
        "replaces_core_field_shape",  # shape divergence on stem-matched core counterpart
        "none",                       # default — no shape divergence detected
    ),
}


# Per-fact "no-evidence" labels for enum facts. A payload carrying one
# of these labels MUST ship ``spans=[]``; a non-empty spans list
# downgrades the row with reason ``spans_on_{value}``. Keeps the
# enum-polarity contract declarative — adding a new enum fact with its
# no-evidence default value doesn't require an edit to
# ``extract.py::_process_payload``'s enum branch.
ENUM_NO_EVIDENCE_VALUES: dict[str, frozenset[str]] = {
    "semantic_class": frozenset({"not_applicable"}),
    "integration_class": frozenset({"unknown"}),
    "documentation_style": frozenset({"unspecified"}),
    "state_scope_delta": frozenset({"neutral"}),
    # Issue #59 — both "none" (no constraint documented) and
    # "unspecified" (constraint exists but mechanism not pinned down)
    # are no-evidence defaults: the LLM has nothing verbatim to quote.
    "sourcing_constraint_documented": frozenset({"none", "unspecified"}),
}


# Per-fact enum labels where spans are OPTIONAL — the LLM may ship
# spans as paraphrase support but empty spans are also legitimate
# (e.g. ``semantic_class="aligned"`` on a row with no state narrative
# to quote). A label NOT in this set and NOT in
# ``ENUM_NO_EVIDENCE_VALUES`` is treated as the affirmative default:
# at least one valid span is required, no-span rows downgrade with
# ``hallucinated_span``. ``integration_class`` and
# ``documentation_style`` intentionally have no "spans optional"
# label — every affirmative integration-class / doc-style claim needs
# evidence in the narrative.
ENUM_SPANS_OPTIONAL_VALUES: dict[str, frozenset[str]] = {
    "semantic_class": frozenset({"aligned"}),
}


# Bool facts whose span convention is inverted from the default.
# Default convention: True = affirmative claim requires >= 1 valid span;
# False = denial ships with empty spans. Inverted facts are ones where
# *False* is the affirmative claim (requires evidence) and True is the
# "no evidence needed" default — e.g. ``extension_is_standalone``, where
# True = "no companion-extension dependency" is the prompt's default
# state and only a False claim carries dependency-language spans.
INVERTED_POLARITY_BOOL_FACTS: frozenset[str] = frozenset({
    "extension_is_standalone",
})


# Facts whose prompt only applies to rows with specific ``source`` values.
# Sending non-extension rows to an extension-only fact wastes LLM spend
# and pollutes the aggregate's review queue (every core/unknown row surfaces
# as ``hallucinated_input:extension_is_*`` because the LLM correctly
# declines to answer on rows the prompt wasn't written for). The extract
# runner filters records against this mapping before batching.
FACT_SOURCE_FILTERS: dict[str, tuple[str, ...]] = {
    "extension_is_necessary": ("extension",),
    "extension_is_standalone": ("extension",),
}


def parse_fact_value(fact: str, raw: str) -> bool | int | str:
    """Parse + validate a human-supplied fact value (issue #249).

    The validation seam for ``poc3 review correct-fact``: an analyst
    correction can never write a value this schema would have rejected
    from the model. Typing follows the same registries the extract
    harness uses — enum facts validate against ``ENUM_VALUED_FACTS``,
    ``INT_VALUED_FACTS`` are non-negative counts, everything else is a
    strict ``true``/``false`` bool. Raises ``ValueError`` with a
    pointed message (callers wrap it into their own abort type).
    """
    text = raw.strip()
    if fact in ENUM_VALUED_FACTS:
        allowed = ENUM_VALUED_FACTS[fact]
        token = text.lower()
        if token not in allowed:
            raise ValueError(
                f"{fact} is an enum fact; {raw!r} is not one of: "
                + ", ".join(allowed)
            )
        return token
    if fact in INT_VALUED_FACTS:
        try:
            count = int(text)
        except ValueError:
            raise ValueError(
                f"{fact} is a count fact; {raw!r} is not an integer"
            ) from None
        if count < 0:
            raise ValueError(
                f"{fact} is a count fact; {count} is negative"
            )
        return count
    token = text.lower()
    if token not in ("true", "false"):
        raise ValueError(
            f"{fact} is a bool fact; expected 'true' or 'false', got {raw!r}"
        )
    return token == "true"


class ScoringSchemaError(Exception):
    """Raised when an LLM response fails strict JSON Schema validation.

    Carries the offending payload for artifact-side debugging. Exit code 3
    bubbles out of the CLI when this fires — never silently skip.
    """

    def __init__(self, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.payload = payload


class CostCapExceeded(Exception):
    """Raised before the next LLM call once `accumulated_usd >= cost_cap`.

    Any partial output is flushed to the artifact JSONL before this
    propagates; exit code 5.
    """
