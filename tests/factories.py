"""Shared test-object factories (issue #213 item 4a).

Plain importable functions — deliberately NOT conftest fixtures — so test
modules can wrap them in their own thin, locally-named defaults
(``_record`` / ``_rec`` / ``_mk``) without pytest fixture plumbing.

Consolidation policy: only helpers that were byte-equivalent or trivially
reconcilable across test files delegate here (the delegates pin their old
per-file defaults explicitly, so every constructed object is identical to
the pre-consolidation one). Deliberately divergent builders — e.g. the
richer Calendar/calendarCode records in ``test_workbook_spec`` /
``test_workbook_fingerprint``, or the keyword-driven partial score dict in
``test_report_analyst`` — stay local, marked with a comment.
"""

from __future__ import annotations

from typing import Any

from src.models.element import ElementRecord


def make_record(
    entity: str = "TestEntity",
    element_name: str = "testField",
    **overrides: Any,
) -> ElementRecord:
    """Minimal valid :class:`ElementRecord` for tests.

    Only the required fields carry factory defaults; every other field
    falls through to the pydantic model defaults, so delegating callers
    produce records byte-identical to constructing the model directly
    with the same explicit kwargs.
    """
    base: dict[str, Any] = {
        "state": "XX",
        "edfi_version": "4.0",
        "domain": "X",
        "entity": entity,
        "element_name": element_name,
        "definition_text": "",
        "documented": True,
    }
    base.update(overrides)
    return ElementRecord.model_validate(base)


def make_score(**overrides: Any) -> dict:
    """Full scores-sidecar row (source-lens shape) for render-layer tests.

    Body moved verbatim from ``test_workbook_spec._score``. ``overrides``
    replace top-level keys only (shallow update — same semantics as the
    original helper); pass a whole ``dimensions`` dict to change nested
    values.
    """
    base: dict[str, Any] = {
        "record_key": "AZ|Calendar|calendarCode",
        "entity": "Calendar",
        "element_name": "calendarCode",
        "dimensions": {
            "canonical_name_alignment": {"value": 2, "confidence": "high"},
            "definition_quality": {"value": 2, "confidence": "medium"},
            "semantic_fidelity": {"value": 3, "confidence": "high"},
            "extension_justification": {"value": None, "confidence": "high"},
            "business_logic_complexity": {"value": 1, "confidence": "high"},
            "nachos_score": {
                "value": 1,
                "rule_matched": "tier_1_conditional",
                "confidence": "high",
                "inputs_used": {
                    "has_conditional_logic__reconciled": True,
                    "has_cross_entity_logic__reconciled": False,
                    "cross_entity_targets": 0,
                },
            },
            "structural_depth": {"value": 2, "confidence": "high"},
            "documentation_style_tier": {
                "value": 1,
                "confidence": "high",
                "inputs_used": {"documentation_style": "conceptual"},
            },
            "documentation_gap": {"value": 0, "confidence": "high"},
        },
        "complexity_score": 1,
        "confidence_composite": "high",
        "review": {"needs_review": False, "reasons": [], "route": None},
        "adjusted_nachos_score": 1.0,
        "in_scope": True,
        "nachos_justification": "tier_1_conditional",
    }
    base.update(overrides)
    return base
