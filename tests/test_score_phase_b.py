"""Phase B LLM-fact extensions — contract + behaviour tests.

Covers:

- schema generalization: ``fact_output_schema(fact, value_type)``
- per-fact prompt-path resolution via ``prompt_path_for``
- ``_validate_payload`` + ``_process_payload`` dispatch by fact name
- count-int validator path (``cross_entity_targets``)
- end-to-end ``run()`` with a scripted client on one LLM fact
- Phase B red-team fixture shape

Phase A's suite (``tests/test_score_phase_a.py``) pins
``has_conditional_logic``'s contract. Phase B tests add additional facts
without reshaping those Phase A expectations.

No live LLM calls anywhere — ``AnthropicClient`` is monkeypatched with
the ``ScriptedClient`` pattern borrowed from the Phase A suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from src.models.element import ElementRecord
from src.score import extract as extract_module
from src.score.client import LLMResponse, _extract_json_payload
from src.score.extract import (
    SUPPORTED_FACTS,
    _process_payload,
    _validate_payload,
    _schema_for,
    prompt_path_for,
    render_prompt,
    run as run_extract,
)
from src.score.schema import (
    INT_VALUED_FACTS,
    ScoringSchemaError,
    fact_output_schema,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_RED_TEAM_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "scoring_phase_b_red_team.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class ScriptedClient:
    """Minimal AnthropicClient stand-in — FIFO-consumes pre-baked payloads."""

    responses: list[Any]
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def call(self, *, system_text: str, user_text: str, cache_system: bool = True) -> LLMResponse:
        if not self.responses:
            raise AssertionError("ScriptedClient ran out of responses")
        next_resp = self.responses.pop(0)
        self.calls.append({"system_text": system_text, "user_text": user_text})
        if isinstance(next_resp, LLMResponse):
            return next_resp
        return LLMResponse(
            payload=next_resp,
            raw_text=json.dumps(next_resp),
            tokens_in=200,
            tokens_out=50,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            usd=0.0015,
            model=self.model,
        )


def _mk_record(**overrides: Any) -> ElementRecord:
    base = {
        "state": "AZ",
        "edfi_version": "3",
        "domain": "Test",
        "entity": "TestEntity",
        "element_name": "testField",
        "data_type": "String",
        "definition_text": "",
        "source": "core",
        "extension_name": None,
        "business_rules_text": None,
        "element_specific_rules": None,
        "regulatory_citations": [],
        "related_entities": [],
        "descriptor_table_code": None,
        "descriptor_table_values": [],
        "collections_text": None,
        "edfi_standard_definition": None,
        "source_document": None,
        "source_page_or_section": None,
        "documented": True,
    }
    base.update(overrides)
    return ElementRecord.model_validate(base)


def _load_red_team(fact: str | None = None) -> list[ElementRecord]:
    raw = json.loads(_RED_TEAM_FIXTURE.read_text(encoding="utf-8"))
    if fact is not None:
        raw = [r for r in raw if r.get("_red_team", {}).get("fact") == fact]
    return [
        ElementRecord.model_validate({k: v for k, v in r.items() if not k.startswith("_")})
        for r in raw
    ]


def _patch_load_records(monkeypatch: pytest.MonkeyPatch, records: list[ElementRecord]) -> None:
    def _load(
        state: str,
        lens: str,
        *,
        elements_path: Path | None = None,
        limit: int | None = None,
        source_filter: tuple[str, ...] | None = None,
    ) -> list[ElementRecord]:
        keep = list(records)
        if source_filter is not None:
            allowed = set(source_filter)
            keep = [r for r in keep if r.source in allowed]
        keep.sort(key=lambda r: (r.entity, r.element_name))
        if limit is not None:
            keep = keep[:limit]
        return keep

    monkeypatch.setattr(extract_module, "load_phase_a_records", _load)


# ---------------------------------------------------------------------------
# Schema generalization
# ---------------------------------------------------------------------------


def test_supported_facts_contains_phase_b_llm_facts() -> None:
    # Phase A anchor + 7 Phase B spine-lens facts + 6 Phase C2
    # source-lens facts = 14 total LLM facts. The Phase B set must
    # stay a strict subset of SUPPORTED_FACTS (no accidental drops).
    phase_b = {
        "has_conditional_logic",
        "definition_is_implementable",
        "required_when_stated",
        "conditional_reporting_stated",
        "populations_or_scope_stated",
        "has_cross_entity_logic",
        "has_aggregation",
        "cross_entity_targets",
    }
    assert phase_b <= set(SUPPORTED_FACTS)


def test_fact_output_schema_accepts_bool_fact_payload() -> None:
    schema = fact_output_schema("definition_is_implementable")
    payload = [
        {
            "element_name": "x",
            "definition_is_implementable": True,
            "spans": ["quoted span"],
            "confidence": "high",
        }
    ]
    jsonschema.validate(payload, schema)


def test_fact_output_schema_rejects_wrong_key_for_bool_fact() -> None:
    # has_conditional_logic payload against a definition_is_implementable schema.
    schema = fact_output_schema("definition_is_implementable")
    bad = [
        {
            "element_name": "x",
            "has_conditional_logic": True,
            "spans": [],
            "confidence": "high",
        }
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_fact_output_schema_int_valued_accepts_integer() -> None:
    schema = fact_output_schema("cross_entity_targets", value_type="integer")
    payload = [
        {
            "element_name": "x",
            "cross_entity_targets": 2,
            "spans": ["Section", "Student"],
            "confidence": "medium",
        }
    ]
    jsonschema.validate(payload, schema)


def test_fact_output_schema_int_valued_rejects_boolean() -> None:
    schema = fact_output_schema("cross_entity_targets", value_type="integer")
    bad = [
        {
            "element_name": "x",
            "cross_entity_targets": True,
            "spans": [],
            "confidence": "low",
        }
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


def test_int_valued_facts_includes_cross_entity_targets() -> None:
    assert "cross_entity_targets" in INT_VALUED_FACTS
    # Bool facts are NOT in the set.
    assert "has_conditional_logic" not in INT_VALUED_FACTS


def test_enum_span_contracts_cover_every_enum_fact_label() -> None:
    """Enum-polarity registries are the declarative source of truth for
    which spans contract each enum-fact label falls under. Labels appearing
    in ``ENUM_NO_EVIDENCE_VALUES[fact]`` must ship empty spans; labels
    in ``ENUM_SPANS_OPTIONAL_VALUES[fact]`` may ship empty or paraphrase
    spans; every remaining label is the affirmative default (≥1 valid
    span required).

    Invariants enforced here:
      - No label may appear in BOTH registries for the same fact (the
        two sets partition the optional-span space).
      - Every label named in either registry must be a declared label
        of that fact in ``ENUM_VALUED_FACTS`` (catches typos like
        ``unknow`` for ``unknown`` at import time).
    """
    from src.score.schema import (
        ENUM_NO_EVIDENCE_VALUES,
        ENUM_SPANS_OPTIONAL_VALUES,
        ENUM_VALUED_FACTS,
    )

    for fact, labels in ENUM_VALUED_FACTS.items():
        label_set = set(labels)
        no_evidence = ENUM_NO_EVIDENCE_VALUES.get(fact, frozenset())
        spans_optional = ENUM_SPANS_OPTIONAL_VALUES.get(fact, frozenset())
        # Label typos in a polarity registry would silently promote a
        # label to the affirmative default; fail fast.
        stray_no_evidence = no_evidence - label_set
        stray_optional = spans_optional - label_set
        assert not stray_no_evidence, (
            f"{fact}: ENUM_NO_EVIDENCE_VALUES carries non-existent "
            f"label(s) {sorted(stray_no_evidence)!r}"
        )
        assert not stray_optional, (
            f"{fact}: ENUM_SPANS_OPTIONAL_VALUES carries non-existent "
            f"label(s) {sorted(stray_optional)!r}"
        )
        # No label may be both "no evidence" and "spans optional".
        overlap = no_evidence & spans_optional
        assert not overlap, (
            f"{fact}: label(s) {sorted(overlap)!r} appear in BOTH "
            f"ENUM_NO_EVIDENCE_VALUES and ENUM_SPANS_OPTIONAL_VALUES"
        )
    assert "definition_is_implementable" not in INT_VALUED_FACTS


def test_schema_for_dispatches_by_fact_name() -> None:
    bool_schema = _schema_for("definition_is_implementable")
    int_schema = _schema_for("cross_entity_targets")
    assert bool_schema["items"]["properties"]["definition_is_implementable"]["type"] == "boolean"
    assert int_schema["items"]["properties"]["cross_entity_targets"]["type"] == "integer"


# ---------------------------------------------------------------------------
# Per-fact prompt path
# ---------------------------------------------------------------------------


def test_prompt_path_for_authored_fact_resolves() -> None:
    path = prompt_path_for("definition_is_implementable")
    assert path.exists()
    assert path.name == "definition_is_implementable.md"


def test_prompt_path_for_unauthored_fact_raises() -> None:
    with pytest.raises(FileNotFoundError):
        prompt_path_for("fake_fact_never_authored")


def test_render_prompt_resolves_fact_when_no_prompt_path_given() -> None:
    rec = _mk_record(definition_text="short")
    system, user = render_prompt(
        rec.entity, [rec], "AZ", fact="definition_is_implementable"
    )
    # System prologue mentions the fact name.
    assert "definition_is_implementable" in system
    # User block carries element line + entity context.
    assert "element_name: testField" in user


# ---------------------------------------------------------------------------
# _validate_payload / _process_payload — bool facts
# ---------------------------------------------------------------------------


def test_validate_payload_bool_fact_round_trip() -> None:
    payload = [
        {
            "element_name": "x",
            "definition_is_implementable": False,
            "spans": [],
            "confidence": "high",
        }
    ]
    # No raise.
    validated = _validate_payload(payload, "definition_is_implementable")
    assert validated == payload


def test_validate_payload_bool_fact_halts_on_drift() -> None:
    bad = [
        {
            "element_name": "x",
            "definition_is_implementable": "yes please",  # not a bool
            "spans": [],
            "confidence": "high",
        }
    ]
    with pytest.raises(ScoringSchemaError):
        _validate_payload(bad, "definition_is_implementable")


def test_validate_payload_extra_property_hallucination_is_logged_not_fatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LLM hallucinating an extra key (TX cold-run's ``data_type`` drift)
    must NOT crash the pair mid-fanout. The schema tolerates the extra
    key; ``_validate_payload`` logs a warning so polarity / schema drift
    is still visible in run telemetry."""
    payload = [
        {
            "element_name": "x",
            "definition_is_implementable": True,
            "spans": ["foo"],
            "confidence": "high",
            "data_type": "String",  # hallucinated echo-back
        },
        {
            "element_name": "y",
            "definition_is_implementable": False,
            "spans": [],
            "confidence": "medium",
            "data_type": "Integer",
        },
    ]
    with caplog.at_level("WARNING", logger="src.score.extract"):
        validated = _validate_payload(payload, "definition_is_implementable")
    assert validated == payload  # payload returned, not truncated
    # A single WARNING summarises the drift across items.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "expected a warning log on extra-key drift"
    message = warnings[-1].getMessage()
    assert "data_type" in message
    assert "definition_is_implementable" in message


def test_validate_payload_known_polarity_error_still_fails_fast() -> None:
    """The extra-property relaxation must NOT loosen polarity /
    structural checks. Wrong value type for the fact still raises."""
    bad = [
        {
            "element_name": "x",
            "definition_is_implementable": "absolutely",  # should be bool
            "spans": [],
            "confidence": "high",
            "data_type": "String",  # unexpected but tolerated
        }
    ]
    # Value-type drift still raises — the extra key doesn't mask it.
    with pytest.raises(ScoringSchemaError):
        _validate_payload(bad, "definition_is_implementable")


def test_validate_payload_missing_required_key_still_fails_fast() -> None:
    """Missing required keys still raise even under the relaxed schema."""
    bad = [
        {
            "element_name": "x",
            # `definition_is_implementable` missing
            "spans": [],
            "confidence": "high",
        }
    ]
    with pytest.raises(ScoringSchemaError):
        _validate_payload(bad, "definition_is_implementable")


def test_validate_payload_bad_confidence_enum_still_fails_fast() -> None:
    """Confidence enum still strict — LLM can't sneak in a new confidence
    label just because the extra-property gate loosened."""
    bad = [
        {
            "element_name": "x",
            "definition_is_implementable": True,
            "spans": ["foo"],
            "confidence": "certainly",  # not in {high, medium, low}
        }
    ]
    with pytest.raises(ScoringSchemaError):
        _validate_payload(bad, "definition_is_implementable")


def test_process_payload_bool_fact_validates_spans() -> None:
    rec = _mk_record(
        element_name="calendarCode",
        definition_text="Submit LEAID-SchoolId-Calendartypecodevalue-Sequence.",
    )
    payload = [
        {
            "element_name": "calendarCode",
            "definition_is_implementable": True,
            "spans": ["Submit LEAID-SchoolId-Calendartypecodevalue-Sequence"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "definition_is_implementable")
    assert downgrades == 0
    assert rows[0]["llm_value"] is True
    assert rows[0]["validated_value"] is True
    assert rows[0]["spans"][0]["valid"] is True


class TestQuoteFoldValidator:
    """Curly quote fold: LLM paraphrases U+2018/19/1C/1D to straight
    ASCII quotes; Word/PDF exports often keep curly. Both sides must
    normalize so substring match isn't a byte-level equality check
    against ephemeral Unicode glyph choices.

    Phase B 4-state fanout measured this: every remaining hallucination
    after the ≥1-valid-span relaxation was curly vs straight drift.
    These cases reproduce the real patterns observed in WI/MN/TX.
    """

    def test_curly_double_quote_in_source_straight_in_span(self) -> None:
        # Source text uses U+201C / U+201D; LLM returns straight double quotes.
        rec = _mk_record(
            definition_text='Enumeration: Core.ProgramTypeDescriptor = \u201CPSEO Program\u201D',
        )
        from src.score.extract import validate_span
        # Straight quotes in span — would have failed without the fold.
        assert validate_span('Enumeration: Core.ProgramTypeDescriptor = "PSEO Program"', rec)

    def test_curly_single_quote_in_source_straight_in_span(self) -> None:
        rec = _mk_record(
            definition_text='A unique alphanumeric code. Also known as \u2018Roster Code\u2019',
        )
        from src.score.extract import validate_span
        assert validate_span("Also known as 'Roster Code'", rec)

    def test_curly_apostrophe_in_possessive(self) -> None:
        # TX TEDS pattern: the ESY services' contact hours are counted...
        rec = _mk_record(
            definition_text="The ESY services\u2019 contact hours are counted in 30-minute increments.",
        )
        from src.score.extract import validate_span
        assert validate_span("The ESY services' contact hours are counted in 30-minute increments.", rec)

    def test_paraphrase_still_rejected_even_with_quote_fold(self) -> None:
        # Fold is character-class only; full paraphrases still fail.
        rec = _mk_record(
            definition_text="The number of actual contact hours each student was served.",
        )
        from src.score.extract import validate_span
        assert not validate_span("The hours each student received services.", rec)


def test_process_payload_bool_fact_partial_valid_spans_kept() -> None:
    """Phase B relaxation: a True claim with >=1 valid span + >=1
    near-miss span stays validated_value=True. The row carries
    any_invalid_spans=True so Phase D's review queue can surface it.

    Regression guard for the commit that relaxed all-valid to
    any-valid — under the old rule this would have downgraded.
    """
    rec = _mk_record(
        element_name="elemX",
        definition_text="CalendarEvent indicates type of event; Waiver day, Instructional day.",
    )
    payload = [
        {
            "element_name": "elemX",
            "definition_is_implementable": True,
            "spans": [
                "CalendarEvent indicates type of event",  # valid
                "Waivers do not apply to programs",       # invalid (paraphrase)
            ],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "definition_is_implementable")
    assert downgrades == 0
    assert rows[0]["validated_value"] is True
    assert rows[0]["any_invalid_spans"] is True
    assert rows[0]["downgrade_reason"] is None


def test_process_payload_bool_fact_all_valid_spans_no_flag() -> None:
    rec = _mk_record(
        element_name="elemY",
        definition_text="CalendarCode indicates campus calendar; NumberDaysTaught indicates days of instruction.",
    )
    payload = [
        {
            "element_name": "elemY",
            "definition_is_implementable": True,
            "spans": [
                "CalendarCode indicates campus calendar",
                "NumberDaysTaught indicates days of instruction",
            ],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "definition_is_implementable")
    assert downgrades == 0
    assert rows[0]["validated_value"] is True
    assert rows[0]["any_invalid_spans"] is False


def test_process_payload_bool_fact_zero_valid_spans_still_downgrades() -> None:
    """When every span fails validation, we STILL downgrade — that's
    the real hallucination case the validator exists to catch."""
    rec = _mk_record(element_name="elemZ", definition_text="Text A.")
    payload = [
        {
            "element_name": "elemZ",
            "definition_is_implementable": True,
            "spans": ["fabricated span 1", "fabricated span 2"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "definition_is_implementable")
    assert downgrades == 1
    assert rows[0]["validated_value"] is None
    assert rows[0]["downgrade_reason"] == "hallucinated_span"
    assert rows[0]["any_invalid_spans"] is True


def test_process_payload_bool_fact_downgrades_hallucinated_span() -> None:
    rec = _mk_record(
        element_name="xyz",
        definition_text="A description of the content standards.",
    )
    payload = [
        {
            "element_name": "xyz",
            "definition_is_implementable": True,
            "spans": ["fabricated quote not in source"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "definition_is_implementable")
    assert downgrades == 1
    assert rows[0]["llm_value"] is True
    assert rows[0]["validated_value"] is None
    assert rows[0]["downgrade_reason"] == "hallucinated_span"


def test_process_payload_bool_fact_false_with_spans_downgrades() -> None:
    rec = _mk_record(element_name="xyz", definition_text="Text")
    payload = [
        {
            "element_name": "xyz",
            "definition_is_implementable": False,
            "spans": ["spurious span on a false claim"],
            "confidence": "medium",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "definition_is_implementable")
    assert downgrades == 1
    assert rows[0]["llm_value"] is False
    assert rows[0]["validated_value"] is None
    assert rows[0]["downgrade_reason"] == "spans_on_false_claim"


# ---------------------------------------------------------------------------
# _process_payload — count-int fact (cross_entity_targets)
# ---------------------------------------------------------------------------


def test_process_payload_count_int_exact_match() -> None:
    rec = _mk_record(
        element_name="sectionRef",
        business_rules_text="Must match the associated Section and the associated Student.",
    )
    payload = [
        {
            "element_name": "sectionRef",
            "cross_entity_targets": 2,
            "spans": ["associated Section", "associated Student"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "cross_entity_targets")
    assert downgrades == 0
    assert rows[0]["llm_value"] == 2
    assert rows[0]["validated_value"] == 2
    assert all(s["valid"] for s in rows[0]["spans"])


def test_process_payload_count_int_mismatch_downgrades() -> None:
    rec = _mk_record(
        element_name="sectionRef",
        business_rules_text="Must match the associated Section.",
    )
    # LLM claims 2 targets but only 1 valid span.
    payload = [
        {
            "element_name": "sectionRef",
            "cross_entity_targets": 2,
            "spans": ["associated Section", "fabricated span never appearing"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "cross_entity_targets")
    assert downgrades == 1
    assert rows[0]["llm_value"] == 2
    assert rows[0]["validated_value"] is None
    assert rows[0]["downgrade_reason"] == "count_span_mismatch"


def test_process_payload_count_int_zero_with_no_spans_clean() -> None:
    rec = _mk_record(element_name="noRef", business_rules_text="Plain field.")
    payload = [
        {
            "element_name": "noRef",
            "cross_entity_targets": 0,
            "spans": [],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "cross_entity_targets")
    assert downgrades == 0
    assert rows[0]["llm_value"] == 0
    assert rows[0]["validated_value"] == 0


def test_process_payload_count_int_zero_with_spans_downgrades() -> None:
    rec = _mk_record(element_name="noRef", business_rules_text="Plain field.")
    payload = [
        {
            "element_name": "noRef",
            "cross_entity_targets": 0,
            "spans": ["stray span"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "cross_entity_targets")
    assert downgrades == 1
    assert rows[0]["downgrade_reason"] == "spans_on_zero_count"


def test_process_payload_missing_from_response_downgrades() -> None:
    rec = _mk_record(element_name="elemA")
    payload: list[dict[str, Any]] = []  # LLM skipped this record
    rows, downgrades = _process_payload(payload, [rec], "definition_is_implementable")
    assert downgrades == 1
    assert rows[0]["downgrade_reason"] == "missing_from_response"


# ---------------------------------------------------------------------------
# _process_payload — enum fact (documentation_style, v2 Day 2)
# ---------------------------------------------------------------------------
#
# semantic_class uses three labels with three distinct span contracts
# (aligned: spans_optional, divergent: spans_required, not_applicable:
# no_spans). documentation_style (plan §4 Mitigation 2) adds a five-
# category classifier. The per-label contract is driven by the pair
# ``ENUM_NO_EVIDENCE_VALUES`` + ``ENUM_SPANS_OPTIONAL_VALUES`` (affirmative
# default = not-in-either) so new enum facts don't require per-label
# branches in _process_payload. These tests exercise both enum facts
# to guard the shared contract tables.


def test_process_payload_documentation_style_prescriptive_requires_span() -> None:
    rec = _mk_record(
        element_name="calendarCode",
        definition_text="Unique calendar identifier.",
        business_rules_text="Submit LEAID-SchoolId-CalendarTypeCodeValue-Sequence.",
    )
    payload = [
        {
            "element_name": "calendarCode",
            "documentation_style": "prescriptive",
            "spans": ["Submit LEAID-SchoolId-CalendarTypeCodeValue-Sequence."],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "documentation_style")
    assert downgrades == 0
    assert rows[0]["validated_value"] == "prescriptive"
    assert rows[0]["spans"][0]["valid"] is True


def test_process_payload_documentation_style_prescriptive_no_span_downgrades() -> None:
    """`prescriptive` is the tier-3 affirmative claim — requires >=1 valid
    span. An empty span list is a contradiction: the classifier chose the
    label that means "the narrative prescribes HOW" without quoting the
    prescription."""
    rec = _mk_record(element_name="x", definition_text="Some prose here.")
    payload = [
        {
            "element_name": "x",
            "documentation_style": "prescriptive",
            "spans": [],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "documentation_style")
    assert downgrades == 1
    assert rows[0]["validated_value"] is None
    assert rows[0]["downgrade_reason"] == "hallucinated_span"


def test_process_payload_documentation_style_unspecified_no_spans_ok() -> None:
    """`unspecified` asserts no informational content — spans must be []."""
    rec = _mk_record(element_name="x", definition_text="(none)")
    payload = [
        {
            "element_name": "x",
            "documentation_style": "unspecified",
            "spans": [],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "documentation_style")
    assert downgrades == 0
    assert rows[0]["validated_value"] == "unspecified"


def test_process_payload_documentation_style_unspecified_with_spans_downgrades() -> None:
    """Spans on an `unspecified` claim contradict the label — downgrade
    with a label-specific reason (`spans_on_unspecified`) so Phase D's
    review queue can route this distinctly from a hallucinated_span."""
    rec = _mk_record(element_name="x", definition_text="(none)")
    payload = [
        {
            "element_name": "x",
            "documentation_style": "unspecified",
            "spans": ["some stray span"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "documentation_style")
    assert downgrades == 1
    assert rows[0]["validated_value"] is None
    assert rows[0]["downgrade_reason"] == "spans_on_unspecified"


def test_process_payload_documentation_style_cross_reference_requires_span() -> None:
    rec = _mk_record(
        element_name="schoolNumber",
        business_rules_text="MDE mapping: School Calendar.School Number",
    )
    payload = [
        {
            "element_name": "schoolNumber",
            "documentation_style": "cross_reference",
            "spans": ["MDE mapping: School Calendar.School Number"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "documentation_style")
    assert downgrades == 0
    assert rows[0]["validated_value"] == "cross_reference"


def test_process_payload_documentation_style_regulatory_requires_span() -> None:
    rec = _mk_record(
        element_name="compulsoryAge",
        business_rules_text="Per Wis. Stat. 118.15.",
    )
    payload = [
        {
            "element_name": "compulsoryAge",
            "documentation_style": "regulatory",
            "spans": ["Per Wis. Stat. 118.15."],
            "confidence": "medium",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "documentation_style")
    assert downgrades == 0
    assert rows[0]["validated_value"] == "regulatory"


def test_process_payload_documentation_style_conceptual_hallucinated_span_downgrades() -> None:
    """Regression guard: a conceptual claim with a fabricated span
    (nothing validates) downgrades, same as any other affirmative-
    default label. If this ever stops firing, the enum-polarity
    dispatch in ``_process_payload`` (ENUM_NO_EVIDENCE_VALUES +
    ENUM_SPANS_OPTIONAL_VALUES registries) has drifted."""
    rec = _mk_record(element_name="x", definition_text="Real prose.")
    payload = [
        {
            "element_name": "x",
            "documentation_style": "conceptual",
            "spans": ["fabricated span not in source"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "documentation_style")
    assert downgrades == 1
    assert rows[0]["validated_value"] is None
    assert rows[0]["downgrade_reason"] == "hallucinated_span"


def test_process_payload_semantic_class_not_applicable_downgrade_reason_is_label_specific() -> None:
    """Regression guard for the shared contract dispatch — the pre-v2
    code used the literal string `spans_on_not_applicable`; the post-v2
    code derives it from the label via f`spans_on_{label}`. Both paths
    must produce the same reason for the existing semantic_class
    fanout."""
    rec = _mk_record(element_name="x", definition_text="Any text.")
    payload = [
        {
            "element_name": "x",
            "semantic_class": "not_applicable",
            "spans": ["stray span"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec], "semantic_class")
    assert downgrades == 1
    assert rows[0]["downgrade_reason"] == "spans_on_not_applicable"


# ---------------------------------------------------------------------------
# End-to-end: run() with a scripted client on definition_is_implementable
# ---------------------------------------------------------------------------


def test_run_definition_is_implementable_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = _load_red_team(fact="definition_is_implementable")
    assert records, "red-team fixture must carry records for this fact"
    _patch_load_records(monkeypatch, records)

    # Group records by entity (matches extractor's grouping) and build
    # a scripted response per batch that mirrors the expected label.
    by_entity: dict[str, list[ElementRecord]] = {}
    for rec in sorted(records, key=lambda r: (r.entity, r.element_name)):
        by_entity.setdefault(rec.entity, []).append(rec)

    raw = json.loads(_RED_TEAM_FIXTURE.read_text(encoding="utf-8"))
    expected_by_key = {
        (r["entity"], r["element_name"]): r["_red_team"]
        for r in raw
        if r["_red_team"]["fact"] == "definition_is_implementable"
    }

    responses: list[Any] = []
    for entity, group in by_entity.items():
        batch: list[dict[str, Any]] = []
        for rec in group:
            meta = expected_by_key[(rec.entity, rec.element_name)]
            if meta["expected"]:
                # Positive — quote a real substring from definition_text.
                defn = rec.definition_text or ""
                span = defn[:40].strip() if defn else ""
                batch.append({
                    "element_name": rec.element_name,
                    "definition_is_implementable": True,
                    "spans": [span] if span else [],
                    "confidence": "high",
                })
            else:
                batch.append({
                    "element_name": rec.element_name,
                    "definition_is_implementable": False,
                    "spans": [],
                    "confidence": "medium",
                })
        responses.append(batch)

    client = ScriptedClient(responses=responses)

    header = run_extract(
        fact="definition_is_implementable",
        state="AZ",
        lens="spine",
        limit=len(records),
        out_dir=tmp_path,
        cache_root=tmp_path / "cache",
        client=client,
    )
    assert header["fact"] == "definition_is_implementable"
    assert header["scored_count"] == len(records)
    assert header["status"] == "complete"
    # Artifact landed on disk with one row per record.
    artifact = tmp_path / "AZ_spine_definition_is_implementable.jsonl"
    assert artifact.exists()
    rows = [json.loads(ln) for ln in artifact.read_text().splitlines()[1:]]
    assert len(rows) == len(records)
    # Every positive row's validated_value stays True (valid span).
    true_rows = [r for r in rows if r["llm_value"] is True]
    assert all(r["validated_value"] is True for r in true_rows), (
        "positive spans in the red team are substrings of the definition; "
        "downgrades here would signal a validator regression"
    )


def test_run_with_unauthored_fact_raises_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """extract.run() rejects a fact that's SUPPORTED_FACTS-listed but
    has no prompt file — regression guard against pointing runs at a
    stub fact and silently reusing another fact's prompt + cache."""
    # Temporarily inject a fact name nobody has authored.
    sentinel = "synthetic_phase_c_fact"
    monkeypatch.setattr(
        extract_module, "SUPPORTED_FACTS", (*SUPPORTED_FACTS, sentinel)
    )
    records = [_mk_record()]
    _patch_load_records(monkeypatch, records)
    with pytest.raises(FileNotFoundError):
        run_extract(
            fact=sentinel,
            state="AZ",
            lens="spine",
            limit=1,
            dry_run=True,
            out_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# Red-team fixture shape
# ---------------------------------------------------------------------------


def test_red_team_fixture_every_record_declares_fact() -> None:
    raw = json.loads(_RED_TEAM_FIXTURE.read_text(encoding="utf-8"))
    assert raw, "fixture must be non-empty"
    for record in raw:
        meta = record.get("_red_team")
        assert meta and "fact" in meta, (
            f"record {record.get('element_name')} missing _red_team.fact"
        )
        assert meta["fact"] in SUPPORTED_FACTS, (
            f"_red_team.fact={meta['fact']!r} not in SUPPORTED_FACTS"
        )
        assert meta["label"] in {"positive", "negative", "adversarial"}
        assert isinstance(meta["expected"], (bool, int))


def test_red_team_fixture_has_positives_and_negatives_for_definition_is_implementable() -> None:
    raw = json.loads(_RED_TEAM_FIXTURE.read_text(encoding="utf-8"))
    fact_records = [r for r in raw if r["_red_team"]["fact"] == "definition_is_implementable"]
    positives = [r for r in fact_records if r["_red_team"]["expected"] is True]
    negatives = [r for r in fact_records if r["_red_team"]["expected"] is False]
    assert len(positives) >= 3, "prompt review needs >=3 positive anchors"
    assert len(negatives) >= 3, "prompt review needs >=3 negative anchors"
    assert any(r["_red_team"]["label"] == "adversarial" for r in fact_records)


# ---------------------------------------------------------------------------
# Per-fact prompt-rendering smoke tests
# ---------------------------------------------------------------------------


_BOOL_LLM_FACTS = (
    "has_conditional_logic",
    "definition_is_implementable",
    "required_when_stated",
    "conditional_reporting_stated",
    "populations_or_scope_stated",
    "has_cross_entity_logic",
    "has_aggregation",
)


@pytest.mark.parametrize("fact", _BOOL_LLM_FACTS)
def test_bool_fact_prompt_renders_and_schema_accepts(fact: str) -> None:
    """For every authored bool LLM fact:
    - prompt template file exists + renders without KeyError
    - schema accepts a payload keyed by the fact name
    - schema rejects a payload keyed by the wrong fact name
    """
    path = prompt_path_for(fact)
    assert path.exists()
    rec = _mk_record(
        definition_text="Sample description.",
        business_rules_text="Sample rule.",
    )
    system, user = render_prompt(rec.entity, [rec], "AZ", fact=fact)
    assert fact in system
    assert "element_name: testField" in user

    schema = fact_output_schema(fact)
    good = [{"element_name": "x", fact: False, "spans": [], "confidence": "low"}]
    jsonschema.validate(good, schema)

    # Wrong fact key must be rejected.
    wrong = [
        {
            "element_name": "x",
            "unrelated_fact_name": False,
            "spans": [],
            "confidence": "low",
        }
    ]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(wrong, schema)


def test_count_int_prompt_renders_and_schema_accepts() -> None:
    path = prompt_path_for("cross_entity_targets")
    assert path.exists()
    rec = _mk_record(
        business_rules_text="Must match a Calendar for the same LEA.",
    )
    system, user = render_prompt(
        rec.entity, [rec], "AZ", fact="cross_entity_targets"
    )
    assert "cross_entity_targets" in system
    schema = fact_output_schema("cross_entity_targets", value_type="integer")
    jsonschema.validate(
        [
            {
                "element_name": "x",
                "cross_entity_targets": 1,
                "spans": ["Calendar"],
                "confidence": "medium",
            }
        ],
        schema,
    )


# ---------------------------------------------------------------------------
# End-to-end: run() with a scripted client on cross_entity_targets (count-int)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _extract_json_payload — robust parser (client.py)
# ---------------------------------------------------------------------------


class TestExtractJsonPayload:
    def test_strict_parse_happy_path(self) -> None:
        out = _extract_json_payload('[{"a": 1}]')
        assert out == [{"a": 1}]

    def test_empty_text_raises(self) -> None:
        with pytest.raises(ScoringSchemaError) as excinfo:
            _extract_json_payload("")
        assert "empty text content" in str(excinfo.value)

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(ScoringSchemaError):
            _extract_json_payload("   \n\t  ")

    def test_json_fence_wrapped(self) -> None:
        out = _extract_json_payload('```json\n[{"a": 1}]\n```')
        assert out == [{"a": 1}]

    def test_bare_fence_wrapped(self) -> None:
        out = _extract_json_payload('```\n[{"b": 2}]\n```')
        assert out == [{"b": 2}]

    def test_prose_prefix_and_suffix(self) -> None:
        text = 'Here is the analysis:\n\n[{"element_name": "x"}]\n\nLet me know.'
        out = _extract_json_payload(text)
        assert out == [{"element_name": "x"}]

    def test_nested_brackets_inside_strings(self) -> None:
        # Span strings with literal `[Rule 90002]` must not confuse the depth tracker.
        text = '[{"element_name": "z", "spans": ["[Rule 90002] keep"]}]'
        out = _extract_json_payload(text)
        assert out == [{"element_name": "z", "spans": ["[Rule 90002] keep"]}]

    def test_prose_then_array_with_nested_rule_text(self) -> None:
        text = (
            'Thinking:\nThe answer is below.\n'
            '[{"element_name": "c", "spans": ["If X then [Y]"]}]'
        )
        out = _extract_json_payload(text)
        assert out == [{"element_name": "c", "spans": ["If X then [Y]"]}]

    def test_prose_with_decoy_brackets_then_array(self) -> None:
        # Sonnet occasionally writes a reasoning preamble that itself
        # contains brackets ("- [rule 60000]", quoted example like `[...]`)
        # before the real JSON array. The first `[` must not trap the
        # parser — iterate to later candidates until one parses.
        text = (
            'Looking at each element, I need to determine — see [rule 60000] '
            'and the example [wrong] format.\n\n'
            'Here is the JSON:\n'
            '[{"element_name": "q", "required_when_stated": false, '
            '"spans": [], "confidence": "high"}]'
        )
        out = _extract_json_payload(text)
        assert out == [
            {
                "element_name": "q",
                "required_when_stated": False,
                "spans": [],
                "confidence": "high",
            }
        ]

    def test_malformed_beyond_rescue_raises_with_preview(self) -> None:
        garbage = "this is not json at all, just words"
        with pytest.raises(ScoringSchemaError) as excinfo:
            _extract_json_payload(garbage)
        assert "not valid JSON" in str(excinfo.value)
        # Preview must be in the error for post-hoc inspection.
        assert "this is not json at all" in str(excinfo.value)

    def test_preview_truncated_for_long_input(self) -> None:
        with pytest.raises(ScoringSchemaError) as excinfo:
            _extract_json_payload("x" * 1000)
        assert "truncated" in str(excinfo.value)


def test_run_cross_entity_targets_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = _load_red_team(fact="cross_entity_targets")
    assert records
    _patch_load_records(monkeypatch, records)

    raw = json.loads(_RED_TEAM_FIXTURE.read_text(encoding="utf-8"))
    meta_by_key = {
        (r["entity"], r["element_name"]): r["_red_team"]
        for r in raw
        if r["_red_team"]["fact"] == "cross_entity_targets"
    }

    # Group by entity (extractor's ordering).
    by_entity: dict[str, list[ElementRecord]] = {}
    for rec in sorted(records, key=lambda r: (r.entity, r.element_name)):
        by_entity.setdefault(rec.entity, []).append(rec)

    responses: list[Any] = []
    for entity, group in by_entity.items():
        batch: list[dict[str, Any]] = []
        for rec in group:
            meta = meta_by_key[(rec.entity, rec.element_name)]
            n = int(meta["expected"])
            # Build spans by chunking the record's business_rules_text into
            # substrings that actually validate (substring of the haystack).
            spans: list[str] = []
            rules = rec.business_rules_text or ""
            if n == 1 and "Calendar" in rules:
                spans = ["match a Calendar"]
            elif n == 2 and "Section" in rules and "Student" in rules:
                spans = ["match a Section", "a Student record"]
            batch.append({
                "element_name": rec.element_name,
                "cross_entity_targets": n,
                "spans": spans,
                "confidence": "high",
            })
        responses.append(batch)

    client = ScriptedClient(responses=responses)

    header = run_extract(
        fact="cross_entity_targets",
        state="AZ",
        lens="spine",
        limit=len(records),
        out_dir=tmp_path,
        cache_root=tmp_path / "cache",
        client=client,
    )
    assert header["status"] == "complete"
    assert header["fact"] == "cross_entity_targets"
    assert header["scored_count"] == len(records)
    artifact = tmp_path / "AZ_spine_cross_entity_targets.jsonl"
    rows = [json.loads(ln) for ln in artifact.read_text().splitlines()[1:]]
    # Each validated_value must match the expected int (no downgrades).
    for row in rows:
        expected = meta_by_key[(row["entity"], row["element_name"])]["expected"]
        assert row["validated_value"] == expected, (
            f"row {row['element_name']} expected {expected} got {row['validated_value']} "
            f"downgrade={row['downgrade_reason']}"
        )
    assert header["downgrade_count"] == 0
