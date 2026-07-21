"""H1 / Session 3 — integration_class enum-valued LLM fact.

Covers:

- Prompt authoring anchors: prompt file exists, has a Polarity line,
  renders cleanly against an ElementRecord.
- Schema: accepts all eight enum labels, rejects unknown labels +
  wrong value types.
- Polarity: the ``unknown`` label is the no-evidence default —
  ``unknown`` with spans downgrades to ``spans_on_unknown``;
  affirmative labels with no valid spans downgrade to
  ``hallucinated_span``.
- Registry generalization: ``semantic_class``'s existing polarity
  still works under the ``ENUM_NO_EVIDENCE_VALUES`` /
  ``ENUM_SPANS_OPTIONAL_VALUES`` registries (regression guard; the
  pre-refactor hard-coded branch shipped under those three labels).
- SUPPORTED_FACTS carries ``integration_class`` so the prompt-path
  resolver picks up the new file.
- Eight positive cases — one per enum label — drawn from the worked
  examples in ``docs/archive/az-human-comparison-hypotheses.md §2.2``.

No live LLM spend anywhere — ``_process_payload`` is called against
hand-built payload dicts, matching the pattern in
``tests/test_score_phase_b.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema
import pytest

from src.models.element import ElementRecord
from tests.factories import make_record
from src.score.extract import (
    SUPPORTED_FACTS,
    _process_payload,
    prompt_path_for,
    render_prompt,
)
from src.score.schema import (
    ENUM_NO_EVIDENCE_VALUES,
    ENUM_SPANS_OPTIONAL_VALUES,
    ENUM_VALUED_FACTS,
    fact_output_schema,
)


_ALL_LABELS = (
    "sis_native",
    "sis_custom_extension",
    "descriptor_mapped",
    "concatenation",
    "calculation",
    "conditional_derivation",
    "cross_entity_reference",
    "unknown",
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _mk(**overrides: Any) -> ElementRecord:
    base: dict[str, Any] = {
        "state": "AZ",
        "edfi_version": "3",
        "domain": "Test",
        "data_type": "String",
        "source": "core",
    }
    base.update(overrides)
    return make_record(**base)


# ---------------------------------------------------------------------------
# Prompt + registry surface
# ---------------------------------------------------------------------------


class TestPromptFile:
    def test_prompt_file_exists(self) -> None:
        """``prompt_path_for`` resolves the integration_class.md file."""
        path = prompt_path_for("integration_class")
        assert path.exists()
        assert path.name == "integration_class.md"

    def test_prompt_renders_for_integration_class(self) -> None:
        """``render_prompt`` builds SYSTEM + USER without raising.
        Guards against the {# ... #} block / SYSTEM / USER delimiter
        drift that would break the extract harness at fanout time."""
        rec = _mk(
            entity="StudentAcademicRecordReference",
            element_name="StudentUniqueId",
            definition_text="The unique identifier of the student.",
        )
        system_text, user_text = render_prompt(
            entity="StudentAcademicRecordReference",
            records=[rec],
            state="AZ",
            fact="integration_class",
            lens="source",
        )
        assert system_text.strip()
        assert "integration_class" in system_text
        # Per tests/test_score_prompt_polarity.py, the Polarity header
        # must not leak into SYSTEM.
        assert "Polarity:" not in system_text
        assert "StudentUniqueId" in user_text

    def test_supported_facts_contains_integration_class(self) -> None:
        assert "integration_class" in SUPPORTED_FACTS


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_integration_class_registered_in_enum_valued_facts(self) -> None:
        assert "integration_class" in ENUM_VALUED_FACTS
        assert set(ENUM_VALUED_FACTS["integration_class"]) == set(_ALL_LABELS)

    def test_unknown_is_the_no_evidence_default(self) -> None:
        assert ENUM_NO_EVIDENCE_VALUES["integration_class"] == frozenset({"unknown"})

    def test_no_spans_optional_label(self) -> None:
        """Every affirmative integration_class label requires evidence —
        there is no ``spans optional`` carve-out like semantic_class has
        for ``aligned``. Guards against future drift where someone adds
        an integration-class label and forgets to require evidence."""
        optional = ENUM_SPANS_OPTIONAL_VALUES.get("integration_class", frozenset())
        assert optional == frozenset()

    @pytest.mark.parametrize("label", _ALL_LABELS)
    def test_schema_accepts_each_enum_label(self, label: str) -> None:
        schema = fact_output_schema(
            "integration_class",
            value_type="string",
            value_enum=_ALL_LABELS,
        )
        spans = [] if label == "unknown" else ["quoted span"]
        payload = [
            {
                "element_name": "x",
                "integration_class": label,
                "spans": spans,
                "confidence": "high",
            }
        ]
        jsonschema.validate(payload, schema)

    def test_schema_rejects_bogus_label(self) -> None:
        schema = fact_output_schema(
            "integration_class",
            value_type="string",
            value_enum=_ALL_LABELS,
        )
        bad = [
            {
                "element_name": "x",
                "integration_class": "sql_derived",
                "spans": [],
                "confidence": "high",
            }
        ]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)

    def test_schema_rejects_boolean_value(self) -> None:
        schema = fact_output_schema(
            "integration_class",
            value_type="string",
            value_enum=_ALL_LABELS,
        )
        bad = [
            {
                "element_name": "x",
                "integration_class": True,
                "spans": [],
                "confidence": "high",
            }
        ]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


# ---------------------------------------------------------------------------
# _process_payload polarity — the load-bearing refactor
# ---------------------------------------------------------------------------


class TestProcessPayloadPolarity:
    def test_unknown_with_empty_spans_keeps_value(self) -> None:
        rec = _mk(element_name="terseLabel", definition_text="bare column label")
        payload = [
            {
                "element_name": "terseLabel",
                "integration_class": "unknown",
                "spans": [],
                "confidence": "low",
            }
        ]
        rows, downgrades = _process_payload(payload, [rec], fact="integration_class")
        assert downgrades == 0
        assert rows[0]["validated_value"] == "unknown"
        assert rows[0]["downgrade_reason"] is None

    def test_unknown_with_spans_downgrades_spans_on_unknown(self) -> None:
        """Polarity guard — `unknown` is the no-evidence default, any
        emitted span is an inconsistent payload."""
        rec = _mk(
            element_name="terseLabel",
            definition_text="bare column label",
        )
        payload = [
            {
                "element_name": "terseLabel",
                "integration_class": "unknown",
                "spans": ["bare column label"],
                "confidence": "low",
            }
        ]
        rows, downgrades = _process_payload(payload, [rec], fact="integration_class")
        assert downgrades == 1
        assert rows[0]["validated_value"] is None
        assert rows[0]["downgrade_reason"] == "spans_on_unknown"

    def test_affirmative_without_spans_downgrades_hallucinated(self) -> None:
        rec = _mk(
            element_name="firstName",
            definition_text="The first name of the student.",
        )
        payload = [
            {
                "element_name": "firstName",
                "integration_class": "sis_native",
                "spans": [],
                "confidence": "high",
            }
        ]
        rows, downgrades = _process_payload(payload, [rec], fact="integration_class")
        assert downgrades == 1
        assert rows[0]["validated_value"] is None
        assert rows[0]["downgrade_reason"] == "hallucinated_span"

    def test_affirmative_with_all_invalid_spans_downgrades(self) -> None:
        rec = _mk(
            element_name="firstName",
            definition_text="The first name of the student.",
        )
        payload = [
            {
                "element_name": "firstName",
                "integration_class": "sis_native",
                "spans": ["span that does not appear"],
                "confidence": "medium",
            }
        ]
        rows, downgrades = _process_payload(payload, [rec], fact="integration_class")
        assert downgrades == 1
        assert rows[0]["validated_value"] is None
        assert rows[0]["downgrade_reason"] == "hallucinated_span"

    def test_affirmative_with_at_least_one_valid_span_keeps_value(self) -> None:
        rec = _mk(
            element_name="firstName",
            definition_text="The first name of the student.",
        )
        payload = [
            {
                "element_name": "firstName",
                "integration_class": "sis_native",
                "spans": [
                    "The first name of the student.",
                    "not actually present",
                ],
                "confidence": "medium",
            }
        ]
        rows, downgrades = _process_payload(payload, [rec], fact="integration_class")
        assert downgrades == 0
        assert rows[0]["validated_value"] == "sis_native"
        # any_invalid_spans stays True so the Phase D review queue can
        # still flag the row — keeping the fact AND surfacing the near-
        # miss span is the Phase B relaxation.
        assert rows[0].get("any_invalid_spans") is True


# ---------------------------------------------------------------------------
# Regression guard: semantic_class under the generalized enum branch
# ---------------------------------------------------------------------------


class TestSemanticClassStillWorks:
    """The H1 refactor generalizes ``_process_payload``'s enum branch
    from hard-coded ``aligned`` / ``divergent`` / ``not_applicable``
    to a registry lookup. These tests pin that semantic_class's
    polarity contract still holds under the generalized path — if
    they break, the generalization broke the existing consumer."""

    def test_not_applicable_with_spans_downgrades(self) -> None:
        rec = _mk(
            element_name="extField",
            source="extension",
            definition_text="State-specific reporting code.",
        )
        payload = [
            {
                "element_name": "extField",
                "semantic_class": "not_applicable",
                "spans": ["State-specific reporting code."],
                "confidence": "high",
            }
        ]
        rows, downgrades = _process_payload(payload, [rec], fact="semantic_class")
        assert downgrades == 1
        assert rows[0]["validated_value"] is None
        assert rows[0]["downgrade_reason"] == "spans_on_not_applicable"

    def test_aligned_with_empty_spans_keeps_value(self) -> None:
        """`aligned` is the spans-optional label — empty spans are OK
        even on an affirmative value."""
        rec = _mk(
            element_name="educationOrganizationId",
            edfi_standard_definition="The identifier assigned to an EducationOrganization.",
            definition_text="EducationOrganization Identity Column",
        )
        payload = [
            {
                "element_name": "educationOrganizationId",
                "semantic_class": "aligned",
                "spans": [],
                "confidence": "low",
            }
        ]
        rows, downgrades = _process_payload(payload, [rec], fact="semantic_class")
        assert downgrades == 0
        assert rows[0]["validated_value"] == "aligned"

    def test_divergent_without_spans_downgrades(self) -> None:
        rec = _mk(
            element_name="gradeLevelDescriptor",
            edfi_standard_definition="The grade level for which a student enrolls.",
            definition_text="The grade level for which a student is enrolled in the fall reporting window.",
        )
        payload = [
            {
                "element_name": "gradeLevelDescriptor",
                "semantic_class": "divergent",
                "spans": [],
                "confidence": "high",
            }
        ]
        rows, downgrades = _process_payload(payload, [rec], fact="semantic_class")
        assert downgrades == 1
        assert rows[0]["validated_value"] is None
        assert rows[0]["downgrade_reason"] == "hallucinated_span"


# ---------------------------------------------------------------------------
# Eight positive cases — one per enum label
# ---------------------------------------------------------------------------


class TestPositiveCasesPerLabel:
    """Each label's worked example from the prompt + hypotheses doc §2.2,
    driven through ``_process_payload``. Smoke-confirms that the
    pipeline accepts each label, keeps the value, and preserves the
    span on a realistic ElementRecord."""

    def _drive(
        self,
        rec: ElementRecord,
        label: str,
        spans: list[str],
    ) -> dict[str, Any]:
        payload = [
            {
                "element_name": rec.element_name,
                "integration_class": label,
                "spans": spans,
                "confidence": "high",
            }
        ]
        rows, downgrades = _process_payload(payload, [rec], fact="integration_class")
        assert downgrades == 0, (
            f"{label}: expected no downgrade on a positive case, got "
            f"{rows[0]['downgrade_reason']!r}"
        )
        assert rows[0]["validated_value"] == label
        return rows[0]

    def test_sis_native_firstName(self) -> None:
        rec = _mk(
            element_name="firstName",
            definition_text="The first name of the student.",
        )
        self._drive(rec, "sis_native", ["The first name of the student."])

    def test_sis_custom_extension_evaluationDate(self) -> None:
        rec = _mk(
            element_name="evaluationDate",
            source="extension",
            business_rules_text=(
                "Custom SIS data element recorded for program "
                "evaluations; state-required extension to the SIS schema."
            ),
        )
        self._drive(
            rec,
            "sis_custom_extension",
            ["Custom SIS data element"],
        )

    def test_descriptor_mapped_TermDescriptor(self) -> None:
        rec = _mk(
            element_name="TermDescriptor",
            data_type="Descriptor",
            business_rules_text=(
                "DETERMINE appropriate descriptor value based on SIS "
                "course classification (semester, trimester, quarter, "
                "summer)."
            ),
        )
        self._drive(
            rec,
            "descriptor_mapped",
            ["DETERMINE appropriate descriptor value based on SIS course classification"],
        )

    def test_concatenation_CalendarCode(self) -> None:
        rec = _mk(
            element_name="CalendarCode",
            business_rules_text=(
                "CONCATENATE LEAID-SchoolId-CalendarTypeCode-Sequence "
                "to form the unique calendar identifier."
            ),
        )
        self._drive(
            rec,
            "concatenation",
            ["CONCATENATE LEAID-SchoolId-CalendarTypeCode-Sequence"],
        )

    def test_calculation_eventDuration(self) -> None:
        rec = _mk(
            element_name="eventDuration",
            definition_text=(
                "CALCULATE approximate duration based on program "
                "meeting minutes over total school day minutes."
            ),
        )
        self._drive(
            rec,
            "calculation",
            ["CALCULATE approximate duration based on program meeting minutes over total school day minutes"],
        )

    def test_conditional_derivation_beginDate(self) -> None:
        rec = _mk(
            element_name="beginDate",
            element_specific_rules=(
                "IF student has an IEP THEN begin date of SPED program "
                "services ELSE program enrollment date."
            ),
        )
        self._drive(
            rec,
            "conditional_derivation",
            ["IF student has an IEP THEN begin date of SPED program services"],
        )

    def test_cross_entity_reference_StudentUniqueId(self) -> None:
        rec = _mk(
            entity="StudentAcademicRecordReference",
            element_name="StudentUniqueId",
            definition_text=(
                "The student unique ID from the referenced "
                "StudentAcademicRecord."
            ),
        )
        self._drive(
            rec,
            "cross_entity_reference",
            ["The student unique ID from the referenced StudentAcademicRecord"],
        )

    def test_unknown_EducationOrganizationId(self) -> None:
        """`unknown` with empty spans on a terse identity-column row."""
        rec = _mk(
            element_name="EducationOrganizationId",
            definition_text="EducationOrganization Identity Column",
        )
        self._drive(rec, "unknown", [])


# ---------------------------------------------------------------------------
# Spans on sidecar — productization-signal surface
# ---------------------------------------------------------------------------


class TestRowToFactResultExtractsValidLlmSpans:
    """``rules._row_to_fact_result`` lifts ``.text`` from ``valid=True``
    LLM-shape span dicts (``{"text": ..., "valid": ...}``) alongside
    deterministic string spans — keeping ``FactResult.spans`` homogeneous
    at ``tuple[str, ...]`` while dropping hallucinated entries the
    substring validator rejected."""

    def test_valid_llm_spans_flow_through(self) -> None:
        from src.score.rules import _row_to_fact_result

        row = {
            "record_key": "AZ|Calendar|BeginDate",
            "entity": "Calendar",
            "element_name": "BeginDate",
            "llm_value": "sis_native",
            "validated_value": "sis_native",
            "confidence": "medium",
            "downgrade_reason": None,
            "any_invalid_spans": False,
            "spans": [
                {"text": "The first date the track is valid.", "valid": True},
            ],
        }
        result = _row_to_fact_result("integration_class", row)
        assert result.value == "sis_native"
        assert result.spans == ("The first date the track is valid.",)

    def test_invalid_llm_spans_filtered(self) -> None:
        """Mixed valid/invalid LLM span dicts — only ``valid=True``
        entries surface. ``valid=False`` and malformed shapes drop."""
        from src.score.rules import _row_to_fact_result

        row = {
            "record_key": "AZ|X|y",
            "entity": "X",
            "element_name": "y",
            "llm_value": "concatenation",
            "validated_value": "concatenation",
            "confidence": "high",
            "downgrade_reason": None,
            "any_invalid_spans": True,
            "spans": [
                {"text": "CONCATENATE A-B-C", "valid": True},
                {"text": "not a real quote", "valid": False},
                {"text": None, "valid": True},  # malformed — no str text
                {"valid": True},  # missing text entirely
                "deterministic-string-path",  # still accepted
            ],
        }
        result = _row_to_fact_result("integration_class", row)
        assert result.spans == (
            "CONCATENATE A-B-C",
            "deterministic-string-path",
        )

    def test_empty_spans_stay_empty(self) -> None:
        from src.score.rules import _row_to_fact_result

        row = {
            "record_key": "AZ|X|y",
            "entity": "X",
            "element_name": "y",
            "llm_value": "unknown",
            "validated_value": "unknown",
            "confidence": "low",
            "downgrade_reason": None,
            "any_invalid_spans": False,
            "spans": [],
        }
        result = _row_to_fact_result("integration_class", row)
        assert result.spans == ()


class TestIntegrationClassSpansSurfaceInSidecar:
    """End-to-end: a Phase A JSONL row with validated LLM spans lands
    in ``fact_provenance.integration_class.spans`` after
    ``run_aggregate`` writes the sidecar."""

    def test_spans_land_on_fact_provenance(self, tmp_path: Path) -> None:
        import json

        from src.score.aggregate import run as run_aggregate
        from tests.test_score_aggregate import (
            _row,
            _seed_minimal_fact_pool,
            _write_fact_artifact,
        )

        _seed_minimal_fact_pool(tmp_path)
        # Productization-signal fact — loaded optionally by
        # ``load_fact_pool`` and surfaced in ``fact_provenance`` when
        # present. The row mirrors the real Phase A shape: LLM-path
        # ``spans`` is ``list[dict]`` with ``text`` + ``valid`` keys.
        integration_row = _row("AZ|Student|id", "Student", "id", "concatenation")
        integration_row["spans"] = [
            {
                "text": "CONCATENATE LEAID-SchoolId-CalendarTypeCode-Sequence",
                "valid": True,
            },
            {"text": "spurious quote", "valid": False},
        ]
        _write_fact_artifact(tmp_path, "AZ", "integration_class", [integration_row])

        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        fp = payload["scores"][0]["fact_provenance"]
        assert fp["integration_class"]["value"] == "concatenation"
        assert fp["integration_class"]["spans"] == [
            "CONCATENATE LEAID-SchoolId-CalendarTypeCode-Sequence"
        ]

    def test_unknown_label_omits_spans_key(self, tmp_path: Path) -> None:
        """Polarity guard: ``unknown`` with empty spans lands without
        the spans key (matches the compact-sidecar contract)."""
        import json

        from src.score.aggregate import run as run_aggregate
        from tests.test_score_aggregate import (
            _row,
            _seed_minimal_fact_pool,
            _write_fact_artifact,
        )

        _seed_minimal_fact_pool(tmp_path)
        integration_row = _row("AZ|Student|id", "Student", "id", "unknown")
        integration_row["spans"] = []
        _write_fact_artifact(tmp_path, "AZ", "integration_class", [integration_row])

        out_path = tmp_path / "az_scores_spine.json"
        run_aggregate(state="AZ", artifacts_dir=tmp_path, out_path=out_path)
        payload = json.loads(out_path.read_text())
        fp = payload["scores"][0]["fact_provenance"]
        assert fp["integration_class"]["value"] == "unknown"
        assert "spans" not in fp["integration_class"]
