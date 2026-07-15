"""Scoring pipeline — LLM fact extraction + algorithmic rule scoring.

Phase A scope: single-fact extraction harness (`has_conditional_logic`,
spine-lens, AZ). Ships the primitives every later phase reuses: prompt
rendering, strict JSON Schema validation, evidence-span substring
validator, on-disk JSONL cache keyed on prompt hash, and cost telemetry
gated by a per-run USD cap.

Downstream (Phase B) fans out the remaining 10 spine-lens facts and 4
states on this same substrate. Phase C adds the rule stage + source
lens. Nothing in this package depends on LLM scoring of rubrics — see
`docs/archive/scoring-plan.md § 3` for the fact/score separation.
"""

from src.score.schema import (
    FACT_OUTPUT_SCHEMA,
    CostCapExceeded,
    ScoringSchemaError,
)

__all__ = [
    "FACT_OUTPUT_SCHEMA",
    "CostCapExceeded",
    "ScoringSchemaError",
]
