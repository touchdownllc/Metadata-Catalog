"""Phase B commit 3 — deterministic facts (no LLM, no cache, no network).

Covers §6 of docs/next-session-scoring-phase-b.md:

- three computations (definition_present, business_rules_present,
  data_type_canonical) behave correctly against edge-case records
- artifact shape matches the LLM-fact shape (same keys per row,
  header with mode="deterministic" + status="complete")
- `extract.run()` short-circuits to the deterministic module when
  the fact is in DETERMINISTIC_FACTS — never touches the cache or
  client
- `mc score run-all --facts all` picks up deterministic facts
  automatically (they're first in PHASE_B_FACTS)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from src.models.element import ElementRecord
from src.score import deterministic as det_module
from src.score import extract as extract_module
from src.score.deterministic import (
    CANONICAL_DATA_TYPES,
    DETERMINISTIC_FACTS,
    DETERMINISTIC_VERSION,
    FactContext,
    LENS_INDEPENDENT_FACTS,
    SOURCE_DETERMINISTIC_FACTS,
    build_fact_context,
    business_rules_present,
    compute_fact,
    data_type_canonical,
    definition_present,
    definition_text_substantive,
    descriptor_values_enumerated,
    descriptor_values_enumerated_spans,
    element_name_matches_canonical,
    extension_fidelity_divergence,
    extension_mirrors_core_pattern,
    naming_deviation_cosmetic,
    run as run_deterministic,
)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _mk(**overrides: Any) -> ElementRecord:
    base = {
        "state": "AZ",
        "edfi_version": "3",
        "domain": "Student",
        "entity": "Student",
        "element_name": "studentId",
        "data_type": "String",
        "definition_text": "The unique identifier.",
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


# ---------------------------------------------------------------------------
# Per-fact computations
# ---------------------------------------------------------------------------


class TestDefinitionPresent:
    def test_nonempty_true(self) -> None:
        assert definition_present(_mk(definition_text="hello")) is True

    def test_empty_false(self) -> None:
        assert definition_present(_mk(definition_text="")) is False

    def test_whitespace_only_false(self) -> None:
        assert definition_present(_mk(definition_text="   \t\n")) is False

    def test_single_character_true(self) -> None:
        assert definition_present(_mk(definition_text="x")) is True

    def test_business_rules_only_rescues_tweds_shape(self) -> None:
        """TWEDS stores per-element prose at the entity level on
        business_rules_text. definition_text empty + business_rules_text
        populated must still score True (audit §3)."""
        rec = _mk(
            definition_text="",
            business_rules_text="The assessment family for state reporting.",
        )
        assert definition_present(rec) is True

    def test_element_specific_rules_only_true(self) -> None:
        """Third narrative slot also rescues."""
        rec = _mk(
            definition_text="",
            business_rules_text=None,
            element_specific_rules="See reporting rules R-12.",
        )
        assert definition_present(rec) is True

    def test_all_narrative_empty_false(self) -> None:
        rec = _mk(
            definition_text="",
            business_rules_text=None,
            element_specific_rules="   ",
        )
        assert definition_present(rec) is False


class TestDefinitionTextSubstantive:
    def test_short_definition_false(self) -> None:
        rec = _mk(definition_text="Grade level.")
        assert definition_text_substantive(rec) is False

    def test_long_definition_true(self) -> None:
        rec = _mk(
            definition_text=(
                "The grade level the student is expected to be in at the "
                "time of the enrollment, reported as an Ed-Fi descriptor."
            )
        )
        assert definition_text_substantive(rec) is True

    def test_aggregates_business_rules(self) -> None:
        """Entity-level prose on business_rules_text combines with a short
        definition_text to clear the length threshold (audit §4)."""
        rec = _mk(
            definition_text="Grade level.",
            business_rules_text=(
                " Reported for each enrollment record per state "
                "reporting rules R-08 and R-12."
            ),
        )
        assert definition_text_substantive(rec) is True

    def test_element_specific_rules_contribute(self) -> None:
        rec = _mk(
            definition_text="",
            business_rules_text=None,
            element_specific_rules=(
                "Must be reported for every enrolled student at the end "
                "of the snapshot window, regardless of attendance status."
            ),
        )
        assert definition_text_substantive(rec) is True

    def test_all_empty_false(self) -> None:
        rec = _mk(
            definition_text="",
            business_rules_text=None,
            element_specific_rules="   ",
        )
        assert definition_text_substantive(rec) is False


class TestBusinessRulesPresent:
    def test_none_false(self) -> None:
        assert business_rules_present(_mk(business_rules_text=None)) is False

    def test_empty_false(self) -> None:
        assert business_rules_present(_mk(business_rules_text="")) is False

    def test_whitespace_only_false(self) -> None:
        assert business_rules_present(_mk(business_rules_text="  ")) is False

    def test_nonempty_true(self) -> None:
        assert business_rules_present(_mk(business_rules_text="If X then Y.")) is True


class TestDataTypeCanonical:
    def test_none_false(self) -> None:
        assert data_type_canonical(_mk(data_type=None)) is False

    def test_empty_false(self) -> None:
        assert data_type_canonical(_mk(data_type="")) is False

    @pytest.mark.parametrize("type_name", [
        "String", "Integer", "Number", "Decimal", "Boolean",
        "Date", "DateTime", "Time", "Descriptor",
        "Reference", "Collection", "Array",
    ])
    def test_canonical_types_true(self, type_name: str) -> None:
        assert data_type_canonical(_mk(data_type=type_name)) is True

    def test_lowercase_form_false(self) -> None:
        """The canonical set is capitalized; lowercase swagger leaks are not."""
        assert data_type_canonical(_mk(data_type="string")) is False

    def test_freeform_false(self) -> None:
        """WI's historical `Bloolean` typo should score False."""
        assert data_type_canonical(_mk(data_type="Bloolean")) is False

    def test_set_covers_observed_values(self) -> None:
        """Every data_type observed in current AZ/WI/MN/TX spine-lens
        documented rows (2026-04-22 survey) must be canonical or None.
        This guards against silent ingest regressions adding an
        unexpected free-form type."""
        observed = {"Boolean", "Date", "DateTime", "Decimal", "Descriptor",
                    "Integer", "Number", "String"}
        assert observed.issubset(CANONICAL_DATA_TYPES)


# ---------------------------------------------------------------------------
# descriptor_values_enumerated (Phase C gate)
# ---------------------------------------------------------------------------


class TestDescriptorValuesEnumerated:
    def test_non_descriptor_row_false(self) -> None:
        """A vanilla String element never triggers the gate, even with an
        enumeration-shaped definition."""
        rec = _mk(
            data_type="String",
            element_name="notes",
            definition_text="Descriptor values: A | B | C | D",
        )
        assert descriptor_values_enumerated(rec) is False

    def test_descriptor_by_data_type_with_enumeration_true(self) -> None:
        rec = _mk(
            data_type="Descriptor",
            element_name="programType",
            business_rules_text="Descriptor values: /foo | uri://ed-fi.org/X | a | b",
        )
        assert descriptor_values_enumerated(rec) is True

    def test_descriptor_by_name_suffix_with_enumeration_true(self) -> None:
        rec = _mk(
            data_type="String",
            element_name="calendarTypeDescriptor",
            business_rules_text="Descriptor values: /calendars | X | Y | Z",
        )
        assert descriptor_values_enumerated(rec) is True

    def test_descriptor_with_table_values_true(self) -> None:
        rec = _mk(
            data_type="Descriptor",
            element_name="gradeLevelDescriptor",
            descriptor_table_values=[{"code": "K", "label": "Kindergarten"}],
            definition_text="indicates the grade",
        )
        assert descriptor_values_enumerated(rec) is True

    def test_descriptor_no_enumeration_false(self) -> None:
        """Semantic-only definition on a descriptor row should not fire —
        the LLM can extract implementable facts from it normally."""
        rec = _mk(
            data_type="Descriptor",
            element_name="providerCategoryDescriptor",
            definition_text="Indicates the category of the provider.",
            business_rules_text="",
        )
        assert descriptor_values_enumerated(rec) is False

    def test_mn_quoted_enumeration_true(self) -> None:
        """MN Mapping-Matrix compact enumeration: `= 'a', 'b', 'c'`."""
        rec = _mk(
            data_type="Descriptor",
            element_name="programTypeDescriptor",
            definition_text=(
                "MDE mapping: EE-STUDENT.Early Education Program; "
                "Enumeration: Extension.ProgramTypeDescriptor = "
                "'EE-ECFE','EE-SR','EE-SR+', or 'EE-VPK'"
            ),
        )
        assert descriptor_values_enumerated(rec) is True

    def test_prose_per_code_three_matches_true(self) -> None:
        """Phase D det.v5: 3+ `CODE:Label` prose tokens on a descriptor
        row fire the gate (WI ``disabilityDescriptor`` pattern)."""
        rec = _mk(
            data_type="Descriptor",
            element_name="disabilityDescriptor",
            business_rules_text=(
                "N:No disability. "
                "A:Autism means a developmental disability significantly "
                "affecting social interaction. "
                "DB:Deafblind means concomitantly deaf or hard of hearing "
                "and blind. "
                "EBD:Emotional behavioral disability."
            ),
        )
        assert descriptor_values_enumerated(rec) is True

    def test_prose_per_code_two_matches_false(self) -> None:
        """Under the 3-hit threshold the gate stays quiet — incidental
        acronym prose like ``USES:The...`` shouldn't false-positive."""
        rec = _mk(
            data_type="Descriptor",
            element_name="fooDescriptor",
            business_rules_text=(
                "USES:The element reports X. "
                "FYI:Reporting conventions align with federal standards."
            ),
        )
        assert descriptor_values_enumerated(rec) is False

    def test_prose_per_code_non_descriptor_row_false(self) -> None:
        """Gate still guards on descriptor-row-kind first — the pattern
        alone cannot fire on a non-descriptor narrative."""
        rec = _mk(
            data_type="String",
            element_name="comments",
            business_rules_text=(
                "A:Autism. DB:Deafblind. EBD:Emotional. H:Hearing."
            ),
        )
        assert descriptor_values_enumerated(rec) is False

    def test_pipe_pattern_requires_three_pipes(self) -> None:
        """A single pipe in a URL or shell-ish string must not fire the gate."""
        rec = _mk(
            data_type="Descriptor",
            element_name="stubDescriptor",
            business_rules_text="See https://example.com/path?x=1|y=2 for details.",
        )
        assert descriptor_values_enumerated(rec) is False

    def test_curly_quoted_enumeration_true(self) -> None:
        """Curly quotes (U+2018/2019) fold to match the straight-quote pattern."""
        rec = _mk(
            data_type="Descriptor",
            element_name="stubDescriptor",
            definition_text="Values: \u2018foo\u2019, \u2018bar\u2019, \u2018baz\u2019",
        )
        assert descriptor_values_enumerated(rec) is True

    def test_empty_narrative_false(self) -> None:
        rec = _mk(
            data_type="Descriptor",
            element_name="stubDescriptor",
            definition_text="",
            business_rules_text=None,
            element_specific_rules=None,
        )
        assert descriptor_values_enumerated(rec) is False


# ---------------------------------------------------------------------------
# descriptor_values_enumerated_spans (H3 — deterministic span capture)
# ---------------------------------------------------------------------------


class TestDescriptorValuesEnumeratedSpans:
    """The span function is ground truth; the boolean is derived from it.

    Every existing ``descriptor_values_enumerated`` contract still holds
    — tests live in :class:`TestDescriptorValuesEnumerated` unchanged.
    This class covers the new ``list[str]`` surface: what span text
    each pattern emits, and that the boolean still agrees on the
    empty/non-empty cutover.
    """

    def test_non_descriptor_row_empty(self) -> None:
        rec = _mk(
            data_type="String",
            element_name="notes",
            definition_text="Descriptor values: A | B | C | D",
        )
        assert descriptor_values_enumerated_spans(rec) == []
        assert descriptor_values_enumerated(rec) is False

    def test_structural_fast_path_emits_code_label_pairs(self) -> None:
        """``descriptor_table_values`` → one ``"{code}: {label}"`` per entry."""
        rec = _mk(
            data_type="Descriptor",
            element_name="gradeLevelDescriptor",
            descriptor_table_values=[
                {"code": "K", "label": "Kindergarten"},
                {"code": "01", "label": "First grade"},
            ],
            definition_text="indicates the grade",
        )
        spans = descriptor_values_enumerated_spans(rec)
        assert spans == ["K: Kindergarten", "01: First grade"]

    def test_literal_descriptor_values_prefix_through_eol(self) -> None:
        """WI ``Descriptor values:`` prefix — span runs to end of line."""
        rec = _mk(
            data_type="String",
            element_name="calendarTypeDescriptor",
            business_rules_text=(
                "Preamble narrative.\n"
                "Descriptor values: /calendars | uri://ed-fi.org/X | a | b\n"
                "Trailing narrative."
            ),
        )
        spans = descriptor_values_enumerated_spans(rec)
        assert (
            "Descriptor values: /calendars | uri://ed-fi.org/X | a | b"
            in spans
        )
        assert descriptor_values_enumerated(rec) is True

    def test_pipe_enumeration_matches_whole_pipe_chunk(self) -> None:
        """WI three-pipe pattern — span is the whole matched pipe run."""
        rec = _mk(
            data_type="Descriptor",
            element_name="programType",
            business_rules_text="Values: | foo | uri://ed-fi.org/X | a | b",
        )
        spans = descriptor_values_enumerated_spans(rec)
        assert any("|" in s and s.count("|") >= 3 for s in spans)
        assert descriptor_values_enumerated(rec) is True

    def test_az_summary_evaluation_valid_values_span(self) -> None:
        """AZ hero row: ``Valid values are 1 to 4 …`` enumerates via the
        literal + pipe shapes depending on how arizona.py packs the
        business-rules text. Here the WI-style literal is the format
        that actually ships on AZ ``summaryEvaluationNumericRating``:
        the literal ``Descriptor values:`` prefix does not appear, but
        a pipe-shaped enumeration does not either — the AZ pattern is
        prose with quoted labels. The quoted-token pattern carries."""
        rec = _mk(
            data_type="Descriptor",
            element_name="summaryEvaluationNumericRating",
            business_rules_text=(
                "Valid values are 1 to 4 for 'Decreased', "
                "'Remained the same', 'Improved', 'Did not need to improve'."
            ),
        )
        spans = descriptor_values_enumerated_spans(rec)
        # The quoted-enum pattern captures adjacent quoted pairs.
        assert any("'Decreased'" in s and "'Remained the same'" in s for s in spans)
        assert descriptor_values_enumerated(rec) is True

    def test_mn_quoted_enumeration_span_includes_tokens(self) -> None:
        """MN ``= 'a','b','c'`` → captured verbatim in a single span."""
        rec = _mk(
            data_type="Descriptor",
            element_name="programTypeDescriptor",
            definition_text=(
                "MDE mapping: EE-STUDENT.Early Education Program; "
                "Enumeration: Extension.ProgramTypeDescriptor = "
                "'EE-ECFE','EE-SR','EE-SR+', or 'EE-VPK'"
            ),
        )
        spans = descriptor_values_enumerated_spans(rec)
        # At minimum, the literal-prefix span runs to end of blob.
        assert any("Enumeration:" in s for s in spans)
        # And the quoted-pattern captures the adjacent-quoted chunk.
        assert any("'EE-ECFE'" in s for s in spans)
        assert descriptor_values_enumerated(rec) is True

    def test_prose_per_code_emits_one_span_per_token(self) -> None:
        """WI ``A:Autism … DB:Deafblind … EBD:Emotional …`` — one span
        per code-token expanded to the next token's start so the full
        descriptive phrase is readable."""
        text = (
            "A:Autism means a developmental disability. "
            "DB:Deafblind means concomitantly deaf and blind. "
            "EBD:Emotional behavioral disability."
        )
        rec = _mk(
            data_type="Descriptor",
            element_name="disabilityDescriptor",
            business_rules_text=text,
        )
        spans = descriptor_values_enumerated_spans(rec)
        # Three tokens above the threshold → three spans.
        assert len(spans) >= 3
        assert spans[0].startswith("A:Autism")
        assert spans[1].startswith("DB:Deafblind")
        assert spans[2].startswith("EBD:Emotional")
        assert descriptor_values_enumerated(rec) is True

    def test_prose_per_code_below_threshold_empty(self) -> None:
        """Under the 3-hit threshold the spans stay empty — same
        cutover as the boolean gate."""
        rec = _mk(
            data_type="Descriptor",
            element_name="fooDescriptor",
            business_rules_text=(
                "USES:The element reports X. "
                "FYI:Reporting conventions align with federal standards."
            ),
        )
        assert descriptor_values_enumerated_spans(rec) == []
        assert descriptor_values_enumerated(rec) is False

    def test_boolean_derives_from_spans_non_empty(self) -> None:
        """Regression guard: the boolean IS ``bool(spans)`` — not a
        parallel code path that can drift out of sync."""
        rec_enum = _mk(
            data_type="Descriptor",
            element_name="programTypeDescriptor",
            business_rules_text="Descriptor values: x | y | z | w",
        )
        rec_plain = _mk(
            data_type="Descriptor",
            element_name="providerCategoryDescriptor",
            definition_text="Indicates the category of the provider.",
        )
        assert bool(descriptor_values_enumerated_spans(rec_enum)) is True
        assert descriptor_values_enumerated(rec_enum) is True
        assert descriptor_values_enumerated_spans(rec_plain) == []
        assert descriptor_values_enumerated(rec_plain) is False


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class TestComputeFact:
    def test_dispatches_to_named_fn(self) -> None:
        rec = _mk(business_rules_text="rule")
        assert compute_fact("business_rules_present", rec) is True

    def test_unknown_fact_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown deterministic fact"):
            compute_fact("not_a_real_one", _mk())

    def test_context_fact_without_context_raises(self) -> None:
        with pytest.raises(ValueError, match="requires a FactContext"):
            compute_fact("element_name_matches_canonical", _mk())

    def test_context_fact_with_context_dispatches(self) -> None:
        ctx = FactContext(
            canonical_by_alias={("student", "firstname"): ("Student", "firstName", None)},
            core_element_names=frozenset({"firstName"}),
            core_element_names_lower=frozenset({"firstname"}),
            natural_key_slots=frozenset(),
        )
        rec = _mk(entity="Student", element_name="firstName", source="core")
        assert compute_fact("element_name_matches_canonical", rec, context=ctx) is True


# ---------------------------------------------------------------------------
# Source-lens deterministic facts (Phase C2) — require FactContext
# ---------------------------------------------------------------------------


def _ctx(
    canonical: dict[tuple[str, str], tuple[str, str, str | None]] | None = None,
    core_names: frozenset[str] | None = None,
    natural_key_slots: frozenset[tuple[str, str]] | None = None,
    core_elements_by_entity: dict[str, tuple[tuple[str, str], ...]] | None = None,
) -> FactContext:
    names = core_names or frozenset()
    return FactContext(
        canonical_by_alias=canonical or {},
        core_element_names=names,
        core_element_names_lower=frozenset(n.lower() for n in names),
        natural_key_slots=natural_key_slots or frozenset(),
        core_elements_by_entity=core_elements_by_entity or {},
    )


class TestElementNameMatchesCanonical:
    def test_exact_canonical_match_true(self) -> None:
        ctx = _ctx({("student", "firstname"): ("Student", "firstName", None)})
        rec = _mk(entity="Student", element_name="firstName", source="core")
        assert element_name_matches_canonical(rec, ctx) is True

    def test_casing_mismatch_false(self) -> None:
        ctx = _ctx({("student", "firstname"): ("Student", "firstName", None)})
        rec = _mk(entity="Student", element_name="FirstName", source="core")
        assert element_name_matches_canonical(rec, ctx) is False

    def test_unknown_source_always_false(self) -> None:
        """Unresolved rows have no canonical to match against."""
        ctx = _ctx({("student", "firstname"): ("Student", "firstName", None)})
        rec = _mk(entity="Student", element_name="firstName", source="unknown")
        assert element_name_matches_canonical(rec, ctx) is False

    def test_no_alias_hit_false(self) -> None:
        """Record's alias keys don't resolve to any spine slot → False."""
        ctx = _ctx({("school", "schoolid"): ("School", "schoolId", None)})
        rec = _mk(entity="Student", element_name="firstName", source="core")
        assert element_name_matches_canonical(rec, ctx) is False

    def test_state_drops_descriptor_suffix_true(self) -> None:
        """TEDS-shaped rows — state name matches canonical after the
        canonical's trailing 'Descriptor' is stripped. Tier-3 claim."""
        ctx = _ctx({
            ("assessment", "titleofassessment"): (
                "Assessment", "titleOfAssessmentDescriptor", None
            ),
        })
        rec = _mk(
            entity="Assessment", element_name="titleOfAssessment", source="core"
        )
        assert element_name_matches_canonical(rec, ctx) is True

    def test_state_pascalcase_minus_descriptor_suffix_false(self) -> None:
        """PascalCase + suffix drop is cosmetic, not exact — tier-2 owns
        it, not tier-3. Guards against the naive case-fold that would
        collapse the tier-2 cosmetic branch."""
        ctx = _ctx({
            ("assessment", "titleofassessment"): (
                "Assessment", "titleOfAssessmentDescriptor", None
            ),
        })
        rec = _mk(
            entity="Assessment", element_name="TitleOfAssessment", source="core"
        )
        assert element_name_matches_canonical(rec, ctx) is False

    def test_pure_case_drift_no_suffix_false(self) -> None:
        """Pure PascalCase/camelCase drift without a descriptor suffix
        is cosmetic, not exact. Regression guard on the tier split."""
        ctx = _ctx({("student", "firstname"): ("Student", "firstName", None)})
        rec = _mk(entity="Student", element_name="FirstName", source="core")
        assert element_name_matches_canonical(rec, ctx) is False


class TestNamingDeviationCosmetic:
    def test_case_only_difference_true(self) -> None:
        ctx = _ctx({("student", "firstname"): ("Student", "firstName", None)})
        rec = _mk(entity="Student", element_name="FirstName", source="core")
        assert naming_deviation_cosmetic(rec, ctx) is True

    def test_plural_difference_false(self) -> None:
        """Plural/singular is NOT treated as cosmetic — semantics differ
        between a single address and a collection of addresses."""
        ctx = _ctx({("student", "addresses"): ("Student", "addresses", None)})
        rec = _mk(entity="Student", element_name="address", source="core")
        assert naming_deviation_cosmetic(rec, ctx) is False

    def test_exact_match_false(self) -> None:
        """Fires only when the names differ — exact matches return False
        so the tier 3 branch owns the row, not tier 2."""
        ctx = _ctx({("student", "firstname"): ("Student", "firstName", None)})
        rec = _mk(entity="Student", element_name="firstName", source="core")
        assert naming_deviation_cosmetic(rec, ctx) is False

    def test_deep_rename_false(self) -> None:
        ctx = _ctx({("student", "firstname"): ("Student", "firstName", None)})
        rec = _mk(entity="Student", element_name="givenName", source="core")
        assert naming_deviation_cosmetic(rec, ctx) is False

    def test_unknown_source_false(self) -> None:
        ctx = _ctx({("student", "firstname"): ("Student", "firstName", None)})
        rec = _mk(entity="Student", element_name="FirstName", source="unknown")
        assert naming_deviation_cosmetic(rec, ctx) is False

    def test_teds_descriptor_suffix_fold_cosmetic_true(self) -> None:
        """TX TEDS 'TitleOfAssessment' vs Ed-Fi
        'titleOfAssessmentDescriptor' — PascalCase + suffix drop = tier-2
        cosmetic. Format-aware gate fix (audit §2)."""
        ctx = _ctx({
            ("assessment", "titleofassessment"): (
                "Assessment", "titleOfAssessmentDescriptor", None
            ),
        })
        rec = _mk(
            entity="Assessment", element_name="TitleOfAssessment", source="core"
        )
        assert naming_deviation_cosmetic(rec, ctx) is True

    def test_tier_3_suffix_match_returns_false(self) -> None:
        """Don't double-count rows the tier-3 suffix fold already owns
        (state 'titleOfAssessment' camelCase, canonical w/ suffix) — they
        belong to element_name_matches_canonical."""
        ctx = _ctx({
            ("assessment", "titleofassessment"): (
                "Assessment", "titleOfAssessmentDescriptor", None
            ),
        })
        rec = _mk(
            entity="Assessment", element_name="titleOfAssessment", source="core"
        )
        assert naming_deviation_cosmetic(rec, ctx) is False


class TestExtensionMirrorsCorePattern:
    def test_extension_reusing_core_name_true(self) -> None:
        ctx = _ctx(core_names=frozenset({"attendance", "membership"}))
        rec = _mk(
            entity="StudentSchoolAssociation",
            element_name="attendance",
            source="extension",
        )
        assert extension_mirrors_core_pattern(rec, ctx) is True

    def test_extension_with_novel_name_false(self) -> None:
        ctx = _ctx(core_names=frozenset({"firstName", "lastName"}))
        rec = _mk(
            entity="School",
            element_name="tribalAffiliationCode",
            source="extension",
        )
        assert extension_mirrors_core_pattern(rec, ctx) is False

    def test_core_source_false(self) -> None:
        """Only extension rows can mirror — core rows ARE the pattern."""
        ctx = _ctx(core_names=frozenset({"firstName"}))
        rec = _mk(entity="Student", element_name="firstName", source="core")
        assert extension_mirrors_core_pattern(rec, ctx) is False

    def test_unknown_source_false(self) -> None:
        ctx = _ctx(core_names=frozenset({"firstName"}))
        rec = _mk(entity="Student", element_name="firstName", source="unknown")
        assert extension_mirrors_core_pattern(rec, ctx) is False

    def test_case_fold_pascal_vs_camel_mirror_true(self) -> None:
        """AZ 'StudentUniqueId' mirrors Ed-Fi core 'studentUniqueId' —
        must match case-insensitively. Format-aware gate fix (audit §5)."""
        ctx = _ctx(core_names=frozenset({"studentUniqueId", "schoolId"}))
        rec = _mk(
            entity="Student",
            element_name="StudentUniqueId",
            source="extension",
        )
        assert extension_mirrors_core_pattern(rec, ctx) is True

    def test_case_fold_short_name_mirror_true(self) -> None:
        """TX extension rows like 'Student' / 'School' must mirror core
        'student' / 'school' even though the casing differs."""
        ctx = _ctx(core_names=frozenset({"student", "school"}))
        rec = _mk(entity="tx_Foo", element_name="Student", source="extension")
        assert extension_mirrors_core_pattern(rec, ctx) is True


class TestExtensionFidelityDivergence:
    """Issue #124 PR 2 / #111 (det.v11): name-stem + data-type-bucket
    comparison between an extension and same-entity core counterparts.

    Heuristic: 5+ char case-insensitive prefix overlap on stems (after
    stripping ``Descriptor`` / ``Id`` / ``UniqueId`` / ``Reference``
    suffixes); shape divergence requires the buckets to differ. The
    canonical case is an extension descriptor over a core boolean.
    """

    def _ctx_for(
        self,
        entity: str,
        *core_pairs: tuple[str, str],
    ) -> FactContext:
        return _ctx(
            core_elements_by_entity={
                entity.lower(): tuple(
                    (n.lower(), t) for n, t in core_pairs
                ),
            },
        )

    def test_descriptor_extension_over_boolean_core_fires(self) -> None:
        """Canonical #111-shape: extension Descriptor stem-matches core
        Boolean → ``replaces_core_field_shape``."""
        ctx = self._ctx_for(
            "StudentProgram",
            ("eligibilityIndicator", "Boolean"),
        )
        rec = _mk(
            entity="StudentProgram",
            element_name="EligibilityDescriptor",
            data_type="Descriptor",
            source="extension",
        )
        assert (
            extension_fidelity_divergence(rec, ctx) == "replaces_core_field_shape"
        )

    def test_descriptor_extension_over_string_core_fires(self) -> None:
        """Extension Descriptor matched against a String core element on
        the same entity is also a shape divergence (descriptor → text)."""
        ctx = self._ctx_for(
            "CourseTranscript",
            ("finalLetterGradeEarned", "String"),
        )
        rec = _mk(
            entity="CourseTranscript",
            element_name="FinalLetterGradeDescriptorId",
            data_type="Descriptor",
            source="extension",
        )
        assert (
            extension_fidelity_divergence(rec, ctx) == "replaces_core_field_shape"
        )

    def test_same_bucket_no_divergence(self) -> None:
        """Same data-type bucket on stem-matched core counterpart →
        ``"none"`` (no shape divergence)."""
        ctx = self._ctx_for(
            "Student",
            ("studentName", "String"),
        )
        rec = _mk(
            entity="Student",
            element_name="StudentNameSuffix",
            data_type="String",
            source="extension",
        )
        assert extension_fidelity_divergence(rec, ctx) == "none"

    def test_no_stem_match_no_divergence(self) -> None:
        """Net-new field with no stem match against any core element on
        the same entity → ``"none"``."""
        ctx = self._ctx_for(
            "Student",
            ("studentUniqueId", "String"),
            ("birthDate", "Date"),
        )
        rec = _mk(
            entity="Student",
            element_name="tribalAffiliationCode",
            data_type="String",
            source="extension",
        )
        assert extension_fidelity_divergence(rec, ctx) == "none"

    def test_core_source_returns_none(self) -> None:
        """Core rows are never extensions — fact returns ``"none"``
        regardless of name overlap."""
        ctx = self._ctx_for(
            "Student",
            ("eligibilityIndicator", "Boolean"),
        )
        rec = _mk(
            entity="Student",
            element_name="eligibilityIndicator",
            data_type="Boolean",
            source="core",
        )
        assert extension_fidelity_divergence(rec, ctx) == "none"

    def test_unknown_source_returns_none(self) -> None:
        ctx = self._ctx_for(
            "Student",
            ("eligibilityIndicator", "Boolean"),
        )
        rec = _mk(
            entity="Student",
            element_name="eligibilityDescriptor",
            data_type="Descriptor",
            source="unknown",
        )
        assert extension_fidelity_divergence(rec, ctx) == "none"

    def test_short_stem_short_circuits_to_none(self) -> None:
        """A stem under the 5-char prefix-min returns ``"none"`` even
        if a core element shares the prefix — guards against incidental
        3-4-char overlaps (``id``, ``code``, ``date``)."""
        ctx = self._ctx_for(
            "Whatever",
            ("idCode", "Integer"),
        )
        rec = _mk(
            entity="Whatever",
            element_name="IdReference",
            data_type="Descriptor",
            source="extension",
        )
        assert extension_fidelity_divergence(rec, ctx) == "none"

    def test_no_core_elements_for_entity_returns_none(self) -> None:
        """Extension on an entity that has no core elements catalogued
        (or that we don't have spine coverage for) returns ``"none"``."""
        ctx = _ctx(core_elements_by_entity={})
        rec = _mk(
            entity="ObscureEntity",
            element_name="WhateverDescriptor",
            data_type="Descriptor",
            source="extension",
        )
        assert extension_fidelity_divergence(rec, ctx) == "none"

    def test_same_name_extension_skipped(self) -> None:
        """Same-name extension shadowing a core element by name is the
        ``extension_mirrors_core_pattern`` signal, not fidelity
        divergence — fact returns ``"none"`` to avoid double-counting."""
        ctx = self._ctx_for(
            "Student",
            ("description", "String"),
        )
        rec = _mk(
            entity="Student",
            element_name="description",
            data_type="Descriptor",
            source="extension",
        )
        # Even though buckets differ (Descriptor vs String), the name is
        # an exact match — that's mirror, not shape-divergence.
        assert extension_fidelity_divergence(rec, ctx) == "none"

    def test_az_111_eligibility_examples_dont_fire(self) -> None:
        """Heuristic limitation: the AZ #111 worked examples
        (``EligibilitySourceDescriptorId`` / core
        ``directCertificationIndicator``) do NOT share a 5-char stem
        prefix and so don't fire. Documented in det.v11 docstring;
        this test pins the limitation so a future heuristic change
        notices the cohort move."""
        ctx = self._ctx_for(
            "StudentSchoolFoodServiceProgramAssociation",
            ("directCertificationIndicator", "Boolean"),
        )
        rec = _mk(
            entity="StudentSchoolFoodServiceProgramAssociation",
            element_name="EligibilitySourceDescriptorId",
            data_type="Descriptor",
            source="extension",
        )
        assert extension_fidelity_divergence(rec, ctx) == "none"


@pytest.mark.realdata
class TestBuildFactContext:
    def test_produces_non_empty_index_for_real_spine(self) -> None:
        """Smoke check: building the context against a real spine should
        populate both the alias map and the core-name set. The concrete
        sizes are covered by the per-state smoke tests downstream."""
        from pathlib import Path
        from src.models.spine import StateSpine

        spine_path = Path(__file__).resolve().parents[1] / "data" / "spine" / "az_spine.json"
        if not spine_path.exists():
            pytest.skip(f"spine not present: {spine_path}")
        spine = StateSpine.model_validate_json(spine_path.read_text(encoding="utf-8"))
        ctx = build_fact_context(spine)
        assert len(ctx.canonical_by_alias) > 0
        assert len(ctx.core_element_names) > 0
        assert len(ctx.natural_key_slots) > 0

    def test_natural_key_slots_include_calendar_identity(self) -> None:
        """AZ Calendar.calendarCode is the canonical natural-key regression
        case — Doug's review flagged it as wrongly tier-3 concatenation.
        Ensure the slot set contains the three FK/identity rows that
        fire in the NACHOS guard."""
        from pathlib import Path
        from src.models.spine import StateSpine
        from src.utils.matching import record_match_keys

        spine_path = Path(__file__).resolve().parents[1] / "data" / "spine" / "az_spine.json"
        if not spine_path.exists():
            pytest.skip(f"spine not present: {spine_path}")
        spine = StateSpine.model_validate_json(spine_path.read_text(encoding="utf-8"))
        ctx = build_fact_context(spine)
        for entity in ("Calendar", "CalendarDate", "StudentSchoolAssociation"):
            any_match = any(
                key in ctx.natural_key_slots
                for key in record_match_keys(entity, "CalendarCode")
            )
            assert any_match, f"{entity}.CalendarCode not in natural_key_slots"


class TestIsNaturalKey:
    """Covers the guard backing the NACHOS tier_0_natural_key_format branch.

    Uses ``record_match_keys``-normalized slot keys, so tests assert the
    full round trip: slot stored under the spine's canonical form,
    record side presented in the state-doc form (PascalCase / plural
    drift), and ``is_natural_key`` resolving them as equivalent.
    """

    def _ctx_with_slots(self, *slots: tuple[str, str]) -> FactContext:
        from src.utils.matching import record_match_keys

        collected: set[tuple[str, str]] = set()
        for entity, element in slots:
            collected.update(record_match_keys(entity, element))
        return _ctx(natural_key_slots=frozenset(collected))

    def test_direct_identity_property_true(self) -> None:
        """Spine-canonical camelCase slot + state-doc PascalCase record
        resolve as the same natural key via alias expansion."""
        from src.score.deterministic import is_natural_key

        ctx = self._ctx_with_slots(("Calendar", "calendarCode"))
        rec = _mk(entity="Calendar", element_name="CalendarCode", source="core")
        assert is_natural_key(rec, ctx) is True

    def test_fk_key_property_identity_true(self) -> None:
        """FK column that participates in the parent's natural key —
        ``CalendarDate.calendarCode`` carries identity by virtue of
        the ``calendarReference`` FK, so it should register as a
        natural-key element on CalendarDate."""
        from src.score.deterministic import is_natural_key

        ctx = self._ctx_with_slots(("CalendarDate", "calendarCode"))
        rec = _mk(entity="CalendarDate", element_name="CalendarCode", source="core")
        assert is_natural_key(rec, ctx) is True

    def test_non_identity_element_false(self) -> None:
        """An element that exists on the entity but isn't flagged
        is_identity (e.g., Calendar.endDate) returns False."""
        from src.score.deterministic import is_natural_key

        ctx = self._ctx_with_slots(("Calendar", "calendarCode"))
        rec = _mk(entity="Calendar", element_name="EndDate", source="core")
        assert is_natural_key(rec, ctx) is False

    def test_empty_slots_false(self) -> None:
        """No spine slots → every record returns False (defensive guard
        against constructing FactContext with an empty natural-key set
        on a spine where `is_identity` wasn't populated)."""
        from src.score.deterministic import is_natural_key

        ctx = _ctx()
        rec = _mk(entity="Calendar", element_name="CalendarCode", source="core")
        assert is_natural_key(rec, ctx) is False

    def test_unrelated_entity_false(self) -> None:
        """Slot on a different entity must not leak across entity names."""
        from src.score.deterministic import is_natural_key

        ctx = self._ctx_with_slots(("Calendar", "calendarCode"))
        rec = _mk(entity="Student", element_name="calendarCode", source="core")
        assert is_natural_key(rec, ctx) is False

    def test_extension_identity_true(self) -> None:
        """State extensions that add identity properties surface on the
        ``extends_entity`` slot set via `_collect_natural_key_slots`'s
        extension loop."""
        from src.models.edfi_catalog import (
            EdFiCatalog,
            EntityEntry,
            ExtensionEntry,
            PropertyInfo,
        )
        from src.models.spine import SpineSourceURLs, StateSpine
        from src.score.deterministic import build_fact_context, is_natural_key
        from datetime import datetime, timezone

        catalog = EdFiCatalog(
            version="4.0.0",
            entities={
                "FooEntity": EntityEntry(
                    properties={
                        "fooId": PropertyInfo(type="string", is_identity=True),
                        "fooLabel": PropertyInfo(type="string"),
                    },
                ),
            },
            extensions={
                "az_fooEntityExtension": ExtensionEntry(
                    extends_entity="FooEntity",
                    source_prefix="az",
                    properties={
                        "fooAzKey": PropertyInfo(type="string", is_identity=True),
                    },
                ),
            },
            entity_count=1,
            extension_count=1,
        )
        spine = StateSpine(
            state="AZ",
            edfi_version="4.0.0",
            fetched_at=datetime.now(timezone.utc),
            source_urls=SpineSourceURLs(resources="https://example/swagger.json"),
            catalog=catalog,
        )
        ctx = build_fact_context(spine)
        ext_rec = _mk(entity="FooEntity", element_name="fooAzKey", source="extension")
        assert is_natural_key(ext_rec, ctx) is True
        core_rec = _mk(entity="FooEntity", element_name="fooId", source="core")
        assert is_natural_key(core_rec, ctx) is True
        non_identity_rec = _mk(entity="FooEntity", element_name="fooLabel", source="core")
        assert is_natural_key(non_identity_rec, ctx) is False


class TestSourceDeterministicFactsRegistered:
    def test_dispatch_table_covers_declared_facts(self) -> None:
        """Every name in SOURCE_DETERMINISTIC_FACTS must be dispatchable
        via compute_fact — catches typos in the catalog tuple."""
        ctx = _ctx()
        rec = _mk(source="core")
        for fact in SOURCE_DETERMINISTIC_FACTS:
            # definition_present is record-only; others need ctx. Either
            # path should not raise on an unknown-fact route.
            try:
                compute_fact(fact, rec, context=ctx)
            except ValueError as exc:
                pytest.fail(f"{fact} not routed by compute_fact: {exc}")

    def test_all_facts_union_covers_both_lenses(self) -> None:
        for fact in LENS_INDEPENDENT_FACTS:
            assert fact in DETERMINISTIC_FACTS
        for fact in SOURCE_DETERMINISTIC_FACTS:
            assert fact in DETERMINISTIC_FACTS


# ---------------------------------------------------------------------------
# v2 structural-complexity axis
# ---------------------------------------------------------------------------


def _tiny_catalog_spine():
    """Build a minimal StateSpine catalog for structural-fact unit tests.

    Two FK chains, a sub-collection, and an extension — enough shape to
    exercise all four context-scoped structural facts without loading a
    real-state spine (those spines stay in data/ for smoke tests).

    Graph:

        LEA (root) ← School ← Calendar ← CalendarDate
                                             └── gradeLevels[sub]
        tx_extSchoolExt extends School (entity_extension_footprint=1)
    """
    from datetime import datetime, timezone

    from src.models.edfi_catalog import (
        EdFiCatalog,
        EntityEntry,
        ExtensionEntry,
        PropertyInfo,
        ReferenceInfo,
        SubCollectionInfo,
    )
    from src.models.spine import SpineSourceURLs, StateSpine

    catalog = EdFiCatalog(
        version="4.0.0",
        entities={
            "LocalEducationAgency": EntityEntry(
                properties={
                    "localEducationAgencyId": PropertyInfo(
                        type="string", is_identity=True
                    ),
                    "nameOfInstitution": PropertyInfo(type="string"),
                },
            ),
            "School": EntityEntry(
                properties={
                    "schoolId": PropertyInfo(type="string", is_identity=True),
                },
                references={
                    "localEducationAgencyReference": ReferenceInfo(
                        entity="LocalEducationAgency",
                        key_properties={
                            "localEducationAgencyId": PropertyInfo(
                                type="string", is_identity=True
                            ),
                        },
                    ),
                },
            ),
            "Calendar": EntityEntry(
                properties={
                    "calendarCode": PropertyInfo(type="string", is_identity=True),
                },
                references={
                    "schoolReference": ReferenceInfo(
                        entity="School",
                        key_properties={
                            "schoolId": PropertyInfo(
                                type="string", is_identity=True
                            ),
                        },
                    ),
                },
            ),
            "CalendarDate": EntityEntry(
                properties={
                    "date": PropertyInfo(type="date", is_identity=True),
                },
                references={
                    "calendarReference": ReferenceInfo(
                        entity="Calendar",
                        key_properties={
                            "calendarCode": PropertyInfo(
                                type="string", is_identity=True
                            ),
                        },
                    ),
                },
                sub_collections={
                    "gradeLevels": SubCollectionInfo(
                        sub_entity="CalendarDateGradeLevel",
                        properties={
                            "gradeLevelDescriptor": PropertyInfo(
                                type="descriptor"
                            ),
                        },
                    ),
                },
            ),
        },
        extensions={
            "tx_schoolExtension": ExtensionEntry(
                extends_entity="School",
                source_prefix="tx",
                properties={
                    "schoolClassification": PropertyInfo(type="string"),
                },
            ),
        },
        entity_count=4,
        extension_count=1,
    )
    return StateSpine(
        state="TEST",
        edfi_version="4.0.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(resources="https://example/swagger.json"),
        catalog=catalog,
    )


class TestDescriptorEnumBreadth:
    """Record-only int fact — counts ``descriptor_table_values`` entries."""

    def test_empty_list_zero(self) -> None:
        from src.score.deterministic import descriptor_enum_breadth

        rec = _mk(descriptor_table_values=[])
        assert descriptor_enum_breadth(rec) == 0

    def test_populated_list_returns_len(self) -> None:
        from src.score.deterministic import descriptor_enum_breadth

        rec = _mk(
            descriptor_table_values=[
                {"code": "A", "label": "Autism"},
                {"code": "B", "label": "Blind"},
                {"code": "C", "label": "Cognitive"},
            ]
        )
        assert descriptor_enum_breadth(rec) == 3

    def test_non_descriptor_row_zero(self) -> None:
        """A plain scalar field with no descriptor table carries 0 —
        the fact doesn't synthesize breadth from narrative."""
        from src.score.deterministic import descriptor_enum_breadth

        rec = _mk(
            entity="Student",
            element_name="firstName",
            data_type="String",
            descriptor_table_values=[],
        )
        assert descriptor_enum_breadth(rec) == 0

    def test_registered_in_lens_independent(self) -> None:
        assert "descriptor_enum_breadth" in LENS_INDEPENDENT_FACTS
        assert "descriptor_enum_breadth" in DETERMINISTIC_FACTS

    def test_compute_fact_dispatches(self) -> None:
        """Record-only route — context not required."""
        rec = _mk(descriptor_table_values=[{"code": "X", "label": "X"}])
        assert compute_fact("descriptor_enum_breadth", rec) == 1


class TestStructuralFactsContext:
    """Integration tests against the shared tiny spine.

    ``build_fact_context`` is the code path the production runner hits;
    these tests confirm the slot-builders land the right counts under
    normal alias expansion.
    """

    def _ctx_from_spine(self) -> FactContext:
        return build_fact_context(_tiny_catalog_spine())

    def test_fk_chain_depth_root_entity_zero(self) -> None:
        from src.score.deterministic import fk_chain_depth

        ctx = self._ctx_from_spine()
        rec = _mk(entity="LocalEducationAgency", element_name="localEducationAgencyId")
        # LEA is a root (no outgoing FKs in this catalog).
        assert fk_chain_depth(rec, ctx) == 0

    def test_fk_chain_depth_one_hop(self) -> None:
        from src.score.deterministic import fk_chain_depth

        ctx = self._ctx_from_spine()
        rec = _mk(entity="School", element_name="schoolId")
        assert fk_chain_depth(rec, ctx) == 1

    def test_fk_chain_depth_multi_hop(self) -> None:
        """Calendar → School → LEA = 2 hops, CalendarDate → Calendar + chain = 3."""
        from src.score.deterministic import fk_chain_depth

        ctx = self._ctx_from_spine()
        cal_rec = _mk(entity="Calendar", element_name="calendarCode")
        assert fk_chain_depth(cal_rec, ctx) == 2
        date_rec = _mk(entity="CalendarDate", element_name="date")
        assert fk_chain_depth(date_rec, ctx) == 3

    def test_fk_chain_depth_unknown_entity_zero(self) -> None:
        """Safe default for rows whose entity isn't in the spine —
        ``source='unknown'`` ingest rows should return 0, not raise."""
        from src.score.deterministic import fk_chain_depth

        ctx = self._ctx_from_spine()
        rec = _mk(entity="NoSuchEntity", element_name="whatever", source="unknown")
        assert fk_chain_depth(rec, ctx) == 0

    def test_fk_chain_depth_case_insensitive_lookup(self) -> None:
        """Records named in camelCase / lowercase still match the
        PascalCase spine key via ``.lower()`` normalization."""
        from src.score.deterministic import fk_chain_depth

        ctx = self._ctx_from_spine()
        rec = _mk(entity="school", element_name="schoolId")  # camelCase entity
        assert fk_chain_depth(rec, ctx) == 1

    def test_reference_fan_out_leaf_zero(self) -> None:
        """CalendarDate isn't referenced by anything else → fan-out 0."""
        from src.score.deterministic import reference_fan_out

        ctx = self._ctx_from_spine()
        rec = _mk(entity="CalendarDate", element_name="date")
        assert reference_fan_out(rec, ctx) == 0

    def test_reference_fan_out_counts_distinct_sources(self) -> None:
        """School is referenced by Calendar, and LEA by School — each
        gets one fan-out source in this tiny catalog."""
        from src.score.deterministic import reference_fan_out

        ctx = self._ctx_from_spine()
        school_rec = _mk(entity="School", element_name="schoolId")
        assert reference_fan_out(school_rec, ctx) == 1
        lea_rec = _mk(entity="LocalEducationAgency", element_name="localEducationAgencyId")
        assert reference_fan_out(lea_rec, ctx) == 1

    def test_reference_fan_out_broadcasts_to_all_elements(self) -> None:
        """Every element of School shares the same fan-out count,
        not only identity fields."""
        from src.score.deterministic import reference_fan_out

        ctx = self._ctx_from_spine()
        # School has a single declared scalar property; make up a
        # non-identity probe element on School — broadcast semantics
        # mean fan-out is still 1 regardless of the element name.
        rec = _mk(entity="School", element_name="randomNonIdentityColumn")
        assert reference_fan_out(rec, ctx) == 1

    def test_sub_collection_depth_inside_sub_zero_to_one(self) -> None:
        """CalendarDate.gradeLevels is a declared sub-collection → its
        entries and member property lift sub_collection_depth to 1;
        non-sub elements stay at 0."""
        from src.score.deterministic import sub_collection_depth

        ctx = self._ctx_from_spine()
        sub_rec = _mk(entity="CalendarDate", element_name="gradeLevels")
        assert sub_collection_depth(sub_rec, ctx) == 1
        prop_rec = _mk(entity="CalendarDate", element_name="gradeLevelDescriptor")
        assert sub_collection_depth(prop_rec, ctx) == 1
        scalar_rec = _mk(entity="CalendarDate", element_name="date")
        assert sub_collection_depth(scalar_rec, ctx) == 0

    def test_entity_extension_footprint_extended_entity(self) -> None:
        """School carries one TX extension → footprint 1."""
        from src.score.deterministic import entity_extension_footprint

        ctx = self._ctx_from_spine()
        rec = _mk(entity="School", element_name="schoolId")
        assert entity_extension_footprint(rec, ctx) == 1

    def test_entity_extension_footprint_unextended_entity_zero(self) -> None:
        from src.score.deterministic import entity_extension_footprint

        ctx = self._ctx_from_spine()
        rec = _mk(entity="Calendar", element_name="calendarCode")
        assert entity_extension_footprint(rec, ctx) == 0

    def test_entity_extension_footprint_broadcasts(self) -> None:
        """Every element of School inherits the same footprint."""
        from src.score.deterministic import entity_extension_footprint

        ctx = self._ctx_from_spine()
        rec = _mk(entity="School", element_name="anyRandomField")
        assert entity_extension_footprint(rec, ctx) == 1


class TestStructuralFactsDispatch:
    """All five facts route cleanly through ``compute_fact``."""

    def test_all_structural_facts_dispatch(self) -> None:
        spine = _tiny_catalog_spine()
        ctx = build_fact_context(spine)
        rec = _mk(entity="School", element_name="schoolId")
        values = {
            fact: compute_fact(fact, rec, context=ctx)
            for fact in (
                "is_natural_key",
                "fk_chain_depth",
                "reference_fan_out",
                "sub_collection_depth",
                "entity_extension_footprint",
            )
        }
        assert values["is_natural_key"] is True
        assert values["fk_chain_depth"] == 1
        assert values["reference_fan_out"] == 1
        assert values["sub_collection_depth"] == 0
        assert values["entity_extension_footprint"] == 1

    def test_context_facts_require_context(self) -> None:
        """Missing context raises instead of silently returning zero."""
        rec = _mk(entity="School", element_name="schoolId")
        for fact in (
            "fk_chain_depth",
            "reference_fan_out",
            "sub_collection_depth",
            "entity_extension_footprint",
        ):
            with pytest.raises(ValueError, match="requires a FactContext"):
                compute_fact(fact, rec)


class TestStructuralFactCycleResistance:
    """The FK-depth walker must not loop forever on catalog cycles."""

    def test_self_reference_does_not_recurse(self) -> None:
        """A self-referencing entity terminates at a bounded depth —
        the cycle-breaker short-circuits at the second visit. Fan-out
        explicitly drops self-edges (they don't introduce a new
        downstream entity)."""
        from datetime import datetime, timezone

        from src.models.edfi_catalog import (
            EdFiCatalog,
            EntityEntry,
            PropertyInfo,
            ReferenceInfo,
        )
        from src.models.spine import SpineSourceURLs, StateSpine
        from src.score.deterministic import (
            fk_chain_depth,
            reference_fan_out,
        )

        catalog = EdFiCatalog(
            version="4.0.0",
            entities={
                "SelfRef": EntityEntry(
                    properties={"id": PropertyInfo(type="string", is_identity=True)},
                    references={
                        "parentRef": ReferenceInfo(
                            entity="SelfRef",
                            key_properties={
                                "id": PropertyInfo(type="string", is_identity=True),
                            },
                        ),
                    },
                ),
            },
            entity_count=1,
        )
        spine = StateSpine(
            state="TEST",
            edfi_version="4.0.0",
            fetched_at=datetime.now(timezone.utc),
            source_urls=SpineSourceURLs(resources="https://example/swagger.json"),
            catalog=catalog,
        )
        ctx = build_fact_context(spine)
        rec = _mk(entity="SelfRef", element_name="id")
        # Depth terminates — the critical property is "doesn't loop."
        # Exact value is 1 (the self-edge counted once before the
        # cycle-breaker fires), but any bounded integer is acceptable
        # for regression coverage.
        depth = fk_chain_depth(rec, ctx)
        assert isinstance(depth, int)
        assert 0 <= depth <= 5
        # Self-reference doesn't inflate fan-out — the target is the
        # same entity, filtered by ``_note``'s self-edge guard.
        assert reference_fan_out(rec, ctx) == 0


@pytest.mark.realdata
class TestStructuralFactsAgainstRealSpine:
    """Smoke tests against AZ spine — mirror the ``TestBuildFactContext``
    pattern. Skip when spine file isn't present (fresh clones pre-fetch).
    """

    def _load_ctx(self) -> FactContext | None:
        from pathlib import Path
        from src.models.spine import StateSpine

        spine_path = Path(__file__).resolve().parents[1] / "data" / "spine" / "az_spine.json"
        if not spine_path.exists():
            return None
        spine = StateSpine.model_validate_json(
            spine_path.read_text(encoding="utf-8")
        )
        return build_fact_context(spine)

    def test_student_fan_out_is_high(self) -> None:
        """AZ Student is referenced by many entities — fan-out >= 20.
        Tier-3 gate on natural-key-high-fan-out depends on this signal
        being large in real spines."""
        ctx = self._load_ctx()
        if ctx is None:
            pytest.skip("AZ spine not present")
        assert ctx.reference_fan_out_by_entity.get("student", 0) >= 20

    def test_distribution_is_non_trivial(self) -> None:
        """Ship criterion (plan §3.1): at least the FK depth map has
        nonzero values across the AZ spine. All-zero would indicate a
        broken graph walk."""
        ctx = self._load_ctx()
        if ctx is None:
            pytest.skip("AZ spine not present")
        depths = list(ctx.fk_chain_depth_by_entity.values())
        assert max(depths) > 0
        assert sum(1 for d in depths if d > 0) > 10


# ---------------------------------------------------------------------------
# Artifact emission
# ---------------------------------------------------------------------------


class TestRun:
    def test_artifact_shape_matches_llm_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The header + row shape must match `extract.run()`'s LLM-path
        output so Phase C's rule stage consumes both paths uniformly."""
        records = [
            _mk(entity="Student", element_name="a", definition_text="def"),
            _mk(entity="Student", element_name="b", definition_text=""),
            _mk(entity="Staff", element_name="c", definition_text="   "),
        ]
        monkeypatch.setattr(
            det_module,
            "load_phase_a_records",
            lambda state, lens, *, elements_path=None, limit=None: records,
        )

        header = run_deterministic(
            fact="definition_present",
            state="AZ",
            out_dir=tmp_path,
        )
        assert header["mode"] == "deterministic"
        assert header["status"] == "complete"
        assert header["fact"] == "definition_present"
        assert header["record_count"] == 3
        assert header["scored_count"] == 3
        assert header["true_count"] == 1
        assert header["false_count"] == 2
        assert header["total_usd"] == 0.0
        assert header["cache_hit_count"] == 0

        artifact = tmp_path / "AZ_spine_definition_present.jsonl"
        lines = [json.loads(ln) for ln in artifact.read_text(encoding="utf-8").splitlines()]
        assert lines[0] == header
        rows = lines[1:]
        assert len(rows) == 3
        # Row schema parity with LLM rows.
        for row in rows:
            assert set(row.keys()) >= {
                "record_key", "entity", "element_name", "llm_value",
                "validated_value", "spans", "confidence", "downgrade_reason",
                "model", "prompt_version",
            }
            assert row["model"] == "deterministic"
            assert row["prompt_version"] == DETERMINISTIC_VERSION
            assert row["spans"] == []
            assert row["confidence"] == "high"
            assert row["downgrade_reason"] is None
            # llm_value and validated_value agree (no downgrade concept here).
            assert row["llm_value"] == row["validated_value"]
        # Per-record values match the three input records.
        assert [r["validated_value"] for r in rows] == [True, False, False]
        # record_key format matches LLM-fact rows.
        assert rows[0]["record_key"] == "AZ|Student|a"

    def test_descriptor_values_enumerated_emits_spans_into_artifact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H3 — ``descriptor_values_enumerated`` rows carry ``spans``
        through the artifact (previously always empty). Records that
        don't fire the gate still have ``spans=[]`` so the shape is
        uniform; Phase C consumers read the row's ``validated_value``
        (boolean) and can optionally inspect ``spans`` for the match
        text that fired the gate."""
        records = [
            _mk(
                entity="Student",
                element_name="fires_gate",
                data_type="Descriptor",
                business_rules_text=(
                    "Descriptor values: /calendars | X | Y | Z"
                ),
            ),
            _mk(
                entity="Student",
                element_name="empty_narrative",
                data_type="Descriptor",
                definition_text="",
                business_rules_text=None,
                element_specific_rules=None,
            ),
            _mk(
                entity="Student",
                element_name="non_descriptor_row",
                data_type="String",
                definition_text="Descriptor values: a | b | c | d",
            ),
        ]
        monkeypatch.setattr(
            det_module,
            "load_phase_a_records",
            lambda state, lens, *, elements_path=None, limit=None: records,
        )

        header = run_deterministic(
            fact="descriptor_values_enumerated",
            state="AZ",
            out_dir=tmp_path,
        )
        assert header["true_count"] == 1
        assert header["false_count"] == 2

        artifact = tmp_path / "AZ_spine_descriptor_values_enumerated.jsonl"
        rows = [
            json.loads(ln)
            for ln in artifact.read_text(encoding="utf-8").splitlines()[1:]
        ]
        by_element = {r["element_name"]: r for r in rows}
        fires = by_element["fires_gate"]
        assert fires["validated_value"] is True
        assert fires["spans"]
        assert any(
            "Descriptor values: /calendars" in s for s in fires["spans"]
        )
        assert all(isinstance(s, str) for s in fires["spans"])
        # Non-firing rows still emit the key as an empty list so row
        # shape stays uniform with LLM-path rows.
        assert by_element["empty_narrative"]["spans"] == []
        assert by_element["non_descriptor_row"]["spans"] == []

    def test_run_honors_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        limits_seen: list[int | None] = []

        def _loader(state: str, lens: str, *, elements_path: Path | None = None, limit: int | None = None):
            limits_seen.append(limit)
            return [_mk(element_name=f"e{i}") for i in range(limit or 0)]

        monkeypatch.setattr(det_module, "load_phase_a_records", _loader)
        run_deterministic(
            fact="definition_present",
            state="AZ",
            limit=5,
            out_dir=tmp_path,
        )
        assert limits_seen == [5]

    def test_run_rejects_unknown_fact(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="not a deterministic fact"):
            run_deterministic(
                fact="has_conditional_logic",
                state="AZ",
                out_dir=tmp_path,
            )


# ---------------------------------------------------------------------------
# extract.run dispatch — the "short-circuit" contract
# ---------------------------------------------------------------------------


class TestExtractRunDispatch:
    def test_deterministic_fact_short_circuits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`extract.run(fact="definition_present", ...)` must dispatch
        to deterministic.run() and never touch the LLM client."""
        records = [_mk(element_name="x", definition_text="hello")]
        monkeypatch.setattr(
            extract_module,
            "load_phase_a_records",
            lambda state, lens, *, elements_path=None, limit=None: records,
        )
        monkeypatch.setattr(
            det_module,
            "load_phase_a_records",
            lambda state, lens, *, elements_path=None, limit=None: records,
        )

        def _fail(*_: Any, **__: Any) -> None:
            raise AssertionError("deterministic path must not construct an Anthropic client")

        monkeypatch.setattr(extract_module, "AnthropicClient", _fail)

        header = extract_module.run(
            fact="definition_present",
            state="AZ",
            lens="spine",
            limit=1,
            out_dir=tmp_path,
        )
        assert header["mode"] == "deterministic"
        assert header["true_count"] == 1

    def test_dispatch_rejects_unsupported_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deterministic path still gates on SUPPORTED_STATES."""
        monkeypatch.setattr(
            det_module,
            "load_phase_a_records",
            lambda state, lens, *, elements_path=None, limit=None: [],
        )
        with pytest.raises(SystemExit) as exc:
            extract_module.run(
                fact="definition_present",
                state="CA",
                lens="spine",
                limit=1,
                out_dir=tmp_path,
            )
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# PHASE_B_FACTS includes the deterministic trio
# ---------------------------------------------------------------------------


def test_phase_b_facts_includes_deterministic() -> None:
    from src.score.deterministic import LENS_INDEPENDENT_FACTS
    from src.score.runner import PHASE_B_FACTS
    for fact in LENS_INDEPENDENT_FACTS:
        assert fact in PHASE_B_FACTS


# ---------------------------------------------------------------------------
# Issue #97 — element_only_parent_entity_gate
# ---------------------------------------------------------------------------


class TestElementOnlyParentEntityGate:
    """det.v10 (issue #97, 2026-04-30): True iff the element name is FK-
    shaped (Id/UniqueId/Reference) AND ``definition_text`` carries a
    parent-entity submission gate annotation (``[Public: …, Choice: …]``)
    AND ``element_specific_rules`` is empty.

    Used by ``business_logic_complexity`` and ``nachos_score`` to
    suppress ``has_conditional_logic`` on rows where the only
    conditional evidence is the parent-record gate (the WI Confluence
    layout copies the entity-level gate annotation into every child
    FK element's definition).
    """

    def test_fires_on_wi_school_id_with_public_choice_annotation(self) -> None:
        rec = _mk(
            entity="GradingPeriod",
            element_name="schoolId",
            definition_text=(
                "The identifier assigned to a school. "
                "Format change from Integer to Big Integer (2025-26 SY and later) "
                "[Public: REQ'D, Choice: NOT REQ'D]"
            ),
            element_specific_rules=None,
        )
        assert det_module.element_only_parent_entity_gate(rec) is True

    def test_fires_on_education_organization_id(self) -> None:
        rec = _mk(
            entity="LocalActual",
            element_name="educationOrganizationId",
            definition_text=(
                "The identifier assigned to a Education Organization. "
                "(LEA ID (7 digits or less) or School ID (6 digits or less) "
                "[Public: REQ'D, Choice: NOT REQ'D]"
            ),
        )
        assert det_module.element_only_parent_entity_gate(rec) is True

    def test_fires_on_student_unique_id(self) -> None:
        rec = _mk(
            entity="StudentLanguageInstructionProgramAssociation",
            element_name="studentUniqueId",
            definition_text=(
                "A unique alphanumeric code assigned to a student. "
                "[Public: CONDITIONALLY REQ'D, Choice: NOT REQ'D]"
            ),
        )
        assert det_module.element_only_parent_entity_gate(rec) is True

    def test_fires_on_misspelled_conditinally(self) -> None:
        """The WI Confluence corpus carries the typo
        ``CONDITINALLY`` (sic) on several rows; the regex matches it
        because it's permissive on the gate-code text."""
        rec = _mk(
            entity="StudentLanguageInstructionProgramAssociation",
            element_name="educationOrganizationId",
            definition_text=(
                "The identifier assigned to an education organization. "
                "[Public: CONDITINALLY REQ'D, Choice: NOT REQ'D]"
            ),
        )
        assert det_module.element_only_parent_entity_gate(rec) is True

    def test_fires_on_reference_suffix(self) -> None:
        rec = _mk(
            entity="StudentSchoolAssociation",
            element_name="schoolReference",
            definition_text="Pointer to school. [Public: REQ'D, Choice: NOT REQ'D]",
        )
        assert det_module.element_only_parent_entity_gate(rec) is True

    def test_inert_when_element_specific_rules_present(self) -> None:
        """When ``element_specific_rules`` carries an element-scoped
        rule, the suppression doesn't apply even if a parent-gate
        annotation is also in the definition — the element rule is
        independent conditional evidence."""
        rec = _mk(
            entity="StudentSpecialEducationProgramAssociation",
            element_name="RccCommunityProviderReferencecommunityProviderId",
            definition_text=(
                "The unique identifier assigned to the RCC facility. "
                "[Public: CONDITIONALLY REQ'D, Choice: NOT REQ'D]"
            ),
            element_specific_rules=(
                "In-State RCC ID is a required data element on the sSEPA "
                "record whenever a student is placed in an in-state RCC."
            ),
        )
        assert det_module.element_only_parent_entity_gate(rec) is False

    def test_inert_when_no_gate_annotation(self) -> None:
        """WI rows whose definition has element-specific value rules but
        no Public/Choice annotation
        (`LocalEducationAgency|educationOrganizationId`) retain their
        LLM verdict — the suppression doesn't apply."""
        rec = _mk(
            entity="LocalEducationAgency",
            element_name="educationOrganizationId",
            definition_text=(
                "For restricted accounts, It will be LEAID (7 digits or less); "
                "otherwise DPI 48856"
            ),
        )
        assert det_module.element_only_parent_entity_gate(rec) is False

    def test_inert_when_residue_carries_element_specific_rule(self) -> None:
        """Even when the gate annotation is present and ESR is empty,
        the suppression doesn't fire if the definition_text carries an
        element-specific rule alongside the annotation. Real-data
        example: ``residentLocalEducationAgencyReference`` carries
        "Resident District is not expected for students who live in
        the district…" inline with the gate annotation. The 180-char
        residue cap separates this Bucket-B shape from Bucket-A
        rows that leave only ~50-175 chars of generic Ed-Fi spine
        description after stripping the annotation."""
        rec = _mk(
            entity="StudentSchoolAssociation",
            element_name="residentLocalEducationAgencyReference",
            definition_text=(
                "The district in which the student resides. "
                "In WISEdata, Resident District is not expected for "
                "students who live in the district and receive their "
                "primary K-12 educational services from the district "
                "submitting the data. The Resident District is assumed "
                "to be the submitting district unless otherwise noted "
                "by inclusion of data in the Resident District field. "
                "[Public: CONDITIONALLY REQ'D, Choice: NOT REQ'D]"
            ),
        )
        assert det_module.element_only_parent_entity_gate(rec) is False

    def test_inert_on_rcc_provider_reference_residue(self) -> None:
        """The other real-world Bucket-B residue case:
        ``RccCommunityProviderReferencecommunityProviderId`` carries
        navigational + business-rules-text content beyond the gate
        annotation. ESR is empty in real data so the residue cap is
        the load-bearing carve-out."""
        rec = _mk(
            entity="StudentSpecialEducationProgramAssociation",
            element_name="RccCommunityProviderReferencecommunityProviderId",
            definition_text=(
                "The unique identifier assigned to the RCC facility for "
                "placementswithin the state, Information about Residential "
                "Care Centers (RCCs) may be obtained from the WISEdata API "
                "via a GET request on the /communityProvidersendpoint. "
                "[Public: CONDITIONALLY REQ'D, Choice: NOT REQ'D]"
            ),
        )
        assert det_module.element_only_parent_entity_gate(rec) is False

    def test_inert_on_az_descriptor_id_with_value_rule(self) -> None:
        """AZ descriptor-Id rows carry element-specific narrative without
        Public/Choice annotations — naturally inert."""
        rec = _mk(
            entity="StudentSchoolAssociation",
            element_name="ExitWithdrawTypeDescriptorID",
            definition_text=(
                "A unique identifier used as Primary Key, not derived from "
                "business logic, when acting as Foreign Key, references the "
                "parent table. This element is required if an "
                "ExitWithdrawDate is provided."
            ),
        )
        assert det_module.element_only_parent_entity_gate(rec) is False

    def test_inert_on_tx_student_id_first_char_rule(self) -> None:
        """TX `StudentId` carries element-specific value constraints in
        ``element_specific_rules`` — naturally inert."""
        rec = _mk(
            entity="Student",
            element_name="StudentId",
            definition_text=(
                "StudentId is the student's Social Security number or a "
                "state-approved alternative identification number."
            ),
            element_specific_rules=(
                "When available, the student's Social Security number "
                "should be used. The first character of StudentId must "
                "be \"S\" or \"0\"-\"8\"."
            ),
        )
        assert det_module.element_only_parent_entity_gate(rec) is False

    def test_inert_on_non_fk_named_element(self) -> None:
        """A non-FK-shaped element name doesn't fire even if a gate
        annotation is present — the suppression targets bare FK refs."""
        rec = _mk(
            entity="Contact",
            element_name="telephoneNumber",
            definition_text=(
                "Contact phone. [Public: REQ'D, Choice: OPT]"
            ),
        )
        assert det_module.element_only_parent_entity_gate(rec) is False

    def test_inert_on_empty_definition(self) -> None:
        rec = _mk(
            entity="Foo",
            element_name="schoolId",
            definition_text="",
        )
        assert det_module.element_only_parent_entity_gate(rec) is False

    def test_dispatch_via_compute_fact(self) -> None:
        """compute_fact dispatches to the new function — record-only,
        no FactContext needed."""
        rec = _mk(
            entity="GradingPeriod",
            element_name="schoolId",
            definition_text=(
                "The identifier assigned to a school. "
                "[Public: REQ'D, Choice: NOT REQ'D]"
            ),
        )
        assert (
            compute_fact("element_only_parent_entity_gate", rec) is True
        )

    def test_listed_in_lens_independent_facts(self) -> None:
        """The fact is record-only and applies to both lenses."""
        assert "element_only_parent_entity_gate" in LENS_INDEPENDENT_FACTS

    def test_deterministic_version_bumped(self) -> None:
        # det.v11 — issue #124 PR 2 / #111 (2026-05-02): adds
        # ``extension_fidelity_divergence``. det.v10 was issue #97
        # (2026-04-30): added ``element_only_parent_entity_gate``.
        assert DETERMINISTIC_VERSION == "det.v11"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_score_extract_deterministic_fact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mc score extract --fact definition_present --state AZ`
        runs with no API key and emits the deterministic summary line."""
        records = [
            _mk(element_name="a", definition_text="yes"),
            _mk(element_name="b", definition_text=""),
        ]
        monkeypatch.setattr(
            det_module,
            "load_phase_a_records",
            lambda state, lens, *, elements_path=None, limit=None: records,
        )
        monkeypatch.setattr(
            extract_module,
            "scoring_phase_a_artifact_path",
            lambda state, fact, lens="spine": tmp_path / f"{state.upper()}_{lens}_{fact}.jsonl",
        )
        # Also patch deterministic's own reference to the path helper.
        monkeypatch.setattr(
            det_module,
            "scoring_phase_a_artifact_path",
            lambda state, fact, lens="spine": tmp_path / f"{state.upper()}_{lens}_{fact}.jsonl",
        )
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from src.cli import cli
        result = CliRunner().invoke(
            cli,
            ["score", "extract", "--fact", "definition_present", "--state", "AZ", "--limit", "2"],
        )
        assert result.exit_code == 0, result.output
        assert "deterministic definition_present" in result.output
        assert "1/2 true (50.0%)" in result.output
