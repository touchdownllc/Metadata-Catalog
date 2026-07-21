"""R2 declarative workbook spec — contract + equivalence tests.

The frozen header snapshots here ARE the column contract (they replace
the positional `column=N` pins in `test_report_analyst.py`): a rename or
reorder shows up as a one-line spec edit plus a one-line snapshot edit in
the same diff.

Option B (issue #186 sequence 2): the Reviewer View sheet became
"Details" — a 4-band layout (identity / scoring, adjusted-first /
analyst-input / far-right `AI:` machine annotations). The
row-equivalence tests pin the spec extractors against the
`analyst._details_row` adapters, which now return the ROLE-FILTERED
`human_score_backfill` projection (Details minus the 8-column
analyst-input band), plus hard-coded expected cell lists.
"""

from __future__ import annotations


from src.models.element import ElementRecord
from tests.factories import make_score
from src.report import analyst
from src.report import workbook_spec as ws
from src.report.workbook_spec import (
    BOTH,
    DETAILS_COLUMNS,
    EXTENSION_DETAILS_COLUMNS,
    REVIEWER_VIEW_COLUMNS,
    SCORING_SUMMARY_COLUMNS,
    RowContext,
)


# Deliberately divergent from tests/factories.make_record: this sheet-spec
# suite needs the richer Calendar/calendarCode base (defs, rules text, source
# doc) that the hard-coded expected cell lists below are written against.
def _record(**overrides) -> ElementRecord:
    base = dict(
        state="AZ",
        edfi_version="4.0",
        domain="SchoolCalendar",
        edfi_domain="SchoolCalendar",
        entity="Calendar",
        element_name="calendarCode",
        data_type="string",
        definition_text="A unique code for the calendar.",
        business_rules_text="Must be unique within the school.",
        source="core",
        source_document="calendar.pdf",
        source_page_or_section="p.3",
        documented=True,
    )
    base.update(overrides)
    return ElementRecord(**base)


def _score(**overrides) -> dict:
    # Body moved verbatim to tests/factories.py (issue #213 item 4a).
    return make_score(**overrides)


def _ctx(record=None, score=None, state="AZ", **kw) -> RowContext:
    r = record or _record()
    return RowContext(
        state=state,
        record=r,
        is_extension=ws._is_extension_record(r),
        edfi_domain=r.edfi_domain,
        score=score,
        **kw,
    )


def _extracted(columns, ctx, lens):
    return [c.extract(ctx) for c in columns if lens in c.lens]


def _projection_columns(lens):
    """Details columns minus the analyst-input band — the
    `human_score_backfill` / `_details_row` role-filtered projection."""
    return [
        c for c in DETAILS_COLUMNS
        if lens in c.lens and c.role != "analyst_input"
        # Overlay never joins recs — column excluded (issue #213 item 1).
        # `effective_score` excluded too (issue #248 Part C): human
        # consensus must never enter the engine-vs-human comparison.
        # `review_why`/`review_priority` likewise (issue #259 PR 2):
        # queue-navigation signal, not engine output.
        and c.key not in (
            "ai_recommendations", "effective_score",
            "review_why", "review_priority",
        )
    ]


def _extracted_projection(ctx, lens):
    return [c.extract(ctx) for c in _projection_columns(lens)]


# --- Frozen header snapshots (THE column contract) -------------------------
#
# Details (formerly Reviewer View) — Option B 4-band order:
# identity → scoring (Adjusted BEFORE Base, prose justification) →
# analyst-input (always empty, light-green headers) → `AI:` machinery.
# `Documentation Gap Reason` retired (5%-populated noise); regulatory
# citations moved out of References into their own Legislation column.

# The 11-column analyst-input band, in order (band 3). `Reviewed?` (the
# approval-column ask) + the §8.4 score overrides (adjusted + base,
# issue #250) lead the band (Option C).
ANALYST_INPUT_HEADERS = (
    "Reviewed?",
    "Analyst Adjusted Score (override)",
    "Analyst Base Score (override)",
    "Required",
    "Recommendations",
    "Ed-Fi Comments",
    "DS Next Steps",
    "State Response",
    "KB Reviewed",
    "Reviewed with State",
    "Validated By",
)

DETAILS_SOURCE_HEADERS = (
    # Band 1: identity
    "State",
    "Source Area",
    "Entity Name",
    "Ed-Fi Domain",
    "Data Element",
    "Data Type",
    # Band 2: scoring, headline (adjusted) axis first
    "Business Logic",
    "Complex Business Logic",
    "Adjusted NACHOS Score",
    "Effective Score (adjudicated)",
    "Base NACHOS Score",
    "Score Adjustments",
    "Justification for Adjusted NACHOS Score",
    "Unnecessary Extension ?",
    "Cross Entity Calculation ?",
    "Reason for extension necessity",
    "Business Logic (Formula)",
    "Reason for Complexity",
    "Multiple Entities Involved",
    "Is an extension",
    "Contributing extension",
    "References",
    "Legislation",
    # Band 3: analyst-input space (empty by design)
    *ANALYST_INPUT_HEADERS,
    # Band 4: AI machinery, far right
    "AI: Match Status",
    "AI: Documentation Source",
    "AI: Implementation Shape",
    "AI: Documentation Style",
    "AI: Documentation Gap",
    "AI: Confidence",
    "AI: Evidence",
    "AI: Needs Review",
    "AI: Review Route",
    "AI: Review Why",
    "AI: Review Priority",
    "AI: Recommendations",
)

DETAILS_SPINE_HEADERS = (
    # Band 1: identity (+ spine-only Ed-Fi Standard Definition)
    "State",
    "Source Area",
    "Entity Name",
    "Ed-Fi Domain",
    "Data Element",
    "Data Type",
    "Ed-Fi Standard Definition",
    # Band 2: scoring, headline (adjusted) axis first
    "Business Logic",
    "Complex Business Logic",
    "Adjusted NACHOS Score",
    "Effective Score (adjudicated)",
    "Base NACHOS Score",
    "Score Adjustments",
    "Justification for Adjusted NACHOS Score",
    "Unnecessary Extension ?",
    "Cross Entity Calculation ?",
    "Reason for extension necessity",
    "Business Logic (Formula)",
    "Reason for Complexity",
    "Multiple Entities Involved",
    "Is an extension",
    "Contributing extension",
    "References",
    "Legislation",
    # Band 3: analyst-input space (empty by design)
    *ANALYST_INPUT_HEADERS,
    # Band 4: AI machinery, far right (+ spine-only AI: Documented)
    "AI: Match Status",
    "AI: Documented",
    "AI: Documentation Source",
    "AI: Implementation Shape",
    "AI: Documentation Style",
    "AI: Documentation Gap",
    "AI: Confidence",
    "AI: Evidence",
    "AI: Needs Review",
    "AI: Review Route",
    "AI: Review Why",
    "AI: Review Priority",
    "AI: Recommendations",
)

# Extension Details (Option B — the analysts' extension-remediation grid).
EXTENSION_DETAILS_HEADERS = (
    "State",
    "Source Area",
    "Entity Name",
    "Ed-Fi Domain",
    "Data Element",
    "Data Type",
    "Contributing extension",
    "Adjusted NACHOS Score",
    "Base NACHOS Score",
    "Necessity",
    "Reason for extension necessity",
    "Design",
    "AI: Evidence",
    "AI: Confidence",
    "AI: Needs Review",
)

SCORING_SUMMARY_SOURCE_HEADERS = (
    "State",
    "Entity Name",
    "Data Element",
    "canonical_name_alignment",
    "definition_quality",
    "semantic_fidelity",
    "extension_justification",
    "business_logic_complexity",
    "complexity_score",
    "nachos_score",
    "adjusted_nachos_score",
    "nachos_justification",
    "confidence_composite",
    "needs_review",
    "review_reasons",
    "recommendations",
)

SCORING_SUMMARY_SPINE_HEADERS = (
    "State",
    "Entity Name",
    "Data Element",
    "documentation_completeness",
    "obligation_clarity",
    "business_logic_complexity",
    "complexity_score",
    "nachos_score",
    "adjusted_nachos_score",
    "nachos_justification",
    "confidence_composite",
    "needs_review",
    "review_reasons",
    "recommendations",
)


def _headers(columns, lens):
    return tuple(c.header for c in columns if lens in c.lens)


class TestHeaderContract:
    def test_details_source_headers(self):
        assert _headers(DETAILS_COLUMNS, "source") == DETAILS_SOURCE_HEADERS
        assert len(DETAILS_SOURCE_HEADERS) == 46  # +Row# = 47 rendered

    def test_details_spine_headers(self):
        assert _headers(DETAILS_COLUMNS, "spine") == DETAILS_SPINE_HEADERS
        assert len(DETAILS_SPINE_HEADERS) == 48  # +Row# = 49 rendered

    def test_reviewer_view_compat_alias(self):
        # Pre-Option-B name survives as an alias of the SAME tuple.
        assert REVIEWER_VIEW_COLUMNS is DETAILS_COLUMNS

    def test_extension_details_headers(self):
        # Lens-agnostic — every Extension Details column is on BOTH lenses.
        assert (
            _headers(EXTENSION_DETAILS_COLUMNS, "source")
            == EXTENSION_DETAILS_HEADERS
        )
        assert (
            _headers(EXTENSION_DETAILS_COLUMNS, "spine")
            == EXTENSION_DETAILS_HEADERS
        )

    def test_scoring_summary_source_headers(self):
        assert (
            _headers(SCORING_SUMMARY_COLUMNS, "source")
            == SCORING_SUMMARY_SOURCE_HEADERS
        )

    def test_scoring_summary_spine_headers(self):
        assert (
            _headers(SCORING_SUMMARY_COLUMNS, "spine")
            == SCORING_SUMMARY_SPINE_HEADERS
        )

    def test_matches_analyst_literal_tuples(self):
        """`analyst._DETAILS_HEADERS` is the ROLE-FILTERED backfill
        projection (Details minus the analyst-input band) — derived from
        the spec, so they cannot drift."""
        assert tuple(
            c.header for c in _projection_columns("source")
        ) == tuple(analyst._DETAILS_HEADERS)
        assert tuple(
            c.header for c in _projection_columns("spine")
        ) == tuple(analyst._DETAILS_HEADERS_SPINE)
        # 31/33 since issue #213 item 1 (ai-Recommendations excluded
        # from the overlay projection; was 32/34). Issue #248 Part C:
        # the pins DELIBERATELY hold — `effective_score` is excluded by
        # key, proving the human-consensus column stays out of the
        # `ai-` machine projection (the overlay measures the ENGINE).
        # Issue #259 PR 2: the pins STILL hold — `review_why` /
        # `review_priority` are excluded by key the same way.
        assert len(analyst._DETAILS_HEADERS) == 31
        assert len(analyst._DETAILS_HEADERS_SPINE) == 33
        for lens in ("source", "spine"):
            keys = {c.key for c in _projection_columns(lens)}
            assert "effective_score" not in keys
            assert "review_why" not in keys
            assert "review_priority" not in keys
        # Issue #213 item 1: `analyst._SOURCE/_SPINE_SCORES_HEADERS`
        # deleted (dead surface) — the spec registry IS the contract.
        assert _headers(SCORING_SUMMARY_COLUMNS, "source") == tuple(
            ws.SCORING_SUMMARY_SHEET.headers_for("source")
        )
        assert _headers(SCORING_SUMMARY_COLUMNS, "spine") == tuple(
            ws.SCORING_SUMMARY_SHEET.headers_for("spine")
        )

    def test_row_number_is_never_a_spec_column(self):
        """`Row #` is renderer-injected — if it enters a spec, the
        `human_score_backfill` ai- projection grows an `ai-Row #`."""
        for col in (
            DETAILS_COLUMNS + SCORING_SUMMARY_COLUMNS + EXTENSION_DETAILS_COLUMNS
        ):
            assert col.header != "Row #"

    def test_header_uniqueness_per_lens(self):
        for columns in (
            DETAILS_COLUMNS, SCORING_SUMMARY_COLUMNS, EXTENSION_DETAILS_COLUMNS
        ):
            for lens in ("source", "spine"):
                headers = _headers(columns, lens)
                assert len(headers) == len(set(headers))

    def test_keys_unique_per_sheet(self):
        for columns in (
            DETAILS_COLUMNS, SCORING_SUMMARY_COLUMNS, EXTENSION_DETAILS_COLUMNS
        ):
            keys = [c.key for c in columns]
            assert len(keys) == len(set(keys))

    def test_lens_membership_sane(self):
        spine_only = {
            c.header for c in DETAILS_COLUMNS if c.lens == ws.SPINE
        }
        assert spine_only == {"Ed-Fi Standard Definition", "AI: Documented"}
        assert not any(c.lens == ws.SOURCE for c in DETAILS_COLUMNS)


class TestAnalystInputBand:
    """Band 3 — the analyst-input space is a spec-level contract:
    exactly these 11 columns, contiguous, machine-empty (the pipeline
    never computes values there; Option C re-applies ingested curation),
    excluded from every machine projection."""

    def _band(self):
        return [c for c in DETAILS_COLUMNS if c.role == "analyst_input"]

    def test_band_is_exactly_the_eleven_headers_in_order(self):
        assert tuple(c.header for c in self._band()) == ANALYST_INPUT_HEADERS

    def test_band_is_contiguous_between_scoring_and_ai_bands(self):
        roles = [c.role for c in DETAILS_COLUMNS]
        first = roles.index("analyst_input")
        last = len(roles) - 1 - roles[::-1].index("analyst_input")
        assert roles[first : last + 1] == ["analyst_input"] * 11
        # Far-right band: everything after the analyst band is `AI:`.
        assert all(r == "ai" for r in roles[last + 1 :])

    def test_band_extractors_return_none_without_curation(self):
        contexts = [
            _ctx(),
            _ctx(score=_score()),
            _ctx(
                record=_record(source="extension", extension_name="az"),
                score=_score(
                    nachos_justification="tier_0_none; +0.5 necessary_ext"
                ),
            ),
        ]
        for col in self._band():
            for ctx in contexts:
                assert col.extract(ctx) is None, col.header

    def test_band_extractors_read_curation_values(self):
        """Option C round-trip: ingested values re-render by column KEY;
        keys without stored values stay None."""
        ctx = _ctx(score=_score())
        ctx.curation = {
            "reviewed": "Yes",
            "analyst_adjusted_override": 1.5,
            "analyst_base_override": 1.0,
            "edfi_comments": "3/6/26 - state confirms derivation",
        }
        by_key = {c.key: c for c in self._band()}
        assert by_key["reviewed"].extract(ctx) == "Yes"
        assert by_key["analyst_adjusted_override"].extract(ctx) == 1.5
        assert by_key["analyst_base_override"].extract(ctx) == 1.0
        assert (
            by_key["edfi_comments"].extract(ctx)
            == "3/6/26 - state confirms derivation"
        )
        assert by_key["validated_by"].extract(ctx) is None
        assert by_key["required"].extract(ctx) is None

    def test_band_extractors_ignore_unknown_curation_keys(self):
        ctx = _ctx()
        ctx.curation = {"not_a_band_key": "x"}
        for col in self._band():
            assert col.extract(ctx) is None, col.header

    def test_band_columns_present_on_both_lenses(self):
        for col in self._band():
            assert col.lens == BOTH, col.header

    def test_every_ai_prefixed_column_has_ai_role(self):
        for col in DETAILS_COLUMNS + EXTENSION_DETAILS_COLUMNS:
            if col.header.startswith("AI: "):
                assert col.role == "ai", col.header

    def test_every_ai_role_details_column_is_ai_prefixed(self):
        # The converse on Details: the display prefix IS the role marker.
        for col in DETAILS_COLUMNS:
            if col.role == "ai":
                assert col.header.startswith("AI: "), col.header


class TestLegendHooks:
    def test_legend_refs_resolve_to_rubric_dicts(self):
        from src.score import rubric

        for col in (
            DETAILS_COLUMNS + SCORING_SUMMARY_COLUMNS + EXTENSION_DETAILS_COLUMNS
        ):
            if col.legend is not None:
                target = getattr(rubric, col.legend)
                assert isinstance(target, (dict, tuple)) and target


class TestDetailsRowEquivalence:
    """Spec extraction (role-filtered to the backfill projection) ==
    the `analyst._details_row` spec adapters, across the value-shape
    variants the corpus actually contains."""

    def _assert_both_lenses(self, record, score, state="AZ"):
        is_ext = ws._is_extension_record(record)
        ctx = RowContext(
            state=state,
            record=record,
            is_extension=is_ext,
            edfi_domain=record.edfi_domain,
            score=score,
        )
        assert _extracted_projection(ctx, "source") == analyst._details_row(
            record, state, is_ext, record.edfi_domain, score
        )
        # cached_property groups are per-instance; fresh ctx for spine.
        ctx2 = RowContext(
            state=state,
            record=record,
            is_extension=is_ext,
            edfi_domain=record.edfi_domain,
            score=score,
        )
        assert _extracted_projection(ctx2, "spine") == analyst._details_row_spine(
            record, state, is_ext, record.edfi_domain, score
        )

    def test_unscored_row(self):
        self._assert_both_lenses(_record(), None)

    def test_scored_core_row(self):
        self._assert_both_lenses(_record(), _score())

    def test_scored_tier0_row_blanks_complexity_signals(self):
        score = _score(
            nachos_justification="tier_0_none",
            adjusted_nachos_score=0.0,
        )
        score["dimensions"]["nachos_score"] = {
            "value": 0,
            "rule_matched": "tier_0_none",
            "confidence": "high",
            "inputs_used": {"has_concatenation": True},
        }
        self._assert_both_lenses(_record(), score)

    def test_extension_necessary(self):
        record = _record(source="extension", extension_name="az")
        score = _score(
            nachos_justification="tier_0_none; +0.5 necessary_ext",
            adjusted_nachos_score=0.5,
        )
        self._assert_both_lenses(record, score)

    def test_extension_unnecessary(self):
        record = _record(source="extension", extension_name="az")
        score = _score(
            nachos_justification="tier_0_none; +1 unnecessary_ext",
            adjusted_nachos_score=1.0,
        )
        self._assert_both_lenses(record, score)

    def test_extension_unresolved_necessity(self):
        record = _record(source="extension", extension_name="az")
        score = _score(
            nachos_justification="tier_0_none; +0.5 necessary_ext",
            review={
                "needs_review": True,
                "reasons": ["extension_necessity_unresolved"],
                "route": None,
            },
        )
        self._assert_both_lenses(record, score)

    def test_needs_review_route_rederived(self):
        score = _score(
            review={
                "needs_review": True,
                "reasons": ["low_confidence_dimension:definition_quality"],
                "route": None,
            }
        )
        self._assert_both_lenses(_record(), score)

    def test_documentation_gap_fires(self):
        score = _score()
        score["dimensions"]["structural_depth"] = {"value": 3, "confidence": "high"}
        score["dimensions"]["documentation_style_tier"] = {
            "value": 0,
            "confidence": "high",
            "inputs_used": {"documentation_style": "unspecified"},
        }
        score["dimensions"]["documentation_gap"] = {"value": 1, "confidence": "high"}
        self._assert_both_lenses(_record(), score)

    def test_swagger_backfill_row(self):
        record = _record(
            domain="",
            documented=False,
            documentation_source="swagger",
            definition_text="",
            source_document=None,
            source_page_or_section=None,
        )
        self._assert_both_lenses(record, None)

    def test_unresolved_row(self):
        record = _record(source="unknown")
        self._assert_both_lenses(record, None)

    def test_multi_entity_and_cross_entity(self):
        score = _score()
        score["dimensions"]["nachos_score"]["inputs_used"] = {
            "has_cross_entity_logic__reconciled": True,
            "cross_entity_targets": 2,
            "has_aggregation": True,
        }
        self._assert_both_lenses(_record(), score)

    def test_regulatory_citations_split_out_of_references(self):
        record = _record(regulatory_citations=["A.R.S. 15-901", "A.R.S. 15-902"])
        self._assert_both_lenses(record, None)
        # Option B: citations no longer fold into References — they get
        # their own Legislation column.
        row = dict(
            zip(
                DETAILS_SOURCE_HEADERS,
                _extracted(DETAILS_COLUMNS, _ctx(record=record), "source"),
            )
        )
        assert row["References"] == "calendar.pdf / p.3"
        assert row["Legislation"] == "A.R.S. 15-901; A.R.S. 15-902"

    def test_hardcoded_scored_source_row(self):
        """Durable value pin over the FULL 42-column Details sheet row
        (band order: identity, scoring adjusted-first, empty analyst
        band, `AI:` machinery)."""
        row = _extracted(DETAILS_COLUMNS, _ctx(score=_score()), "source")
        assert row == [
            "AZ",
            "SchoolCalendar",
            "Calendar",
            "SchoolCalendar",
            "calendarCode",
            "string",
            "A unique code for the calendar.",
            1,                       # Complex Business Logic
            1.0,                     # Adjusted NACHOS Score (headline, FIRST)
            None,                    # Effective Score (no adjudication)
            1,                       # Base NACHOS Score
            None,                    # Score Adjustments (none applied)
            "Base 1 (one conditional decides the value)",  # prose justification
            "N/A",                   # Unnecessary Extension ? (core row)
            "No",                    # Cross Entity Calculation ?
            None,                    # Reason for extension necessity
            "Must be unique within the school.",
            "conditional logic",     # Reason for Complexity
            "No",                    # Multiple Entities Involved
            "No",                    # Is an extension
            None,                    # Contributing extension
            "calendar.pdf / p.3",    # References (doc pointer only)
            None,                    # Legislation
            # Analyst-input band (11 cols) — machine-empty; Option C
            # re-applies ingested curation, none present on this ctx.
            None, None, None, None, None, None, None, None, None, None,
            None,
            "Matched (core)",        # AI: Match Status
            "Source Doc",            # AI: Documentation Source
            "Moderate",              # AI: Implementation Shape
            "Conceptual",            # AI: Documentation Style
            "No",                    # AI: Documentation Gap
            "high",                  # AI: Confidence
            None,                    # AI: Evidence (no fact_provenance)
            "No",                    # AI: Needs Review
            None,                    # AI: Review Route
            None,                    # AI: Review Why (no queue entries on ctx)
            None,                    # AI: Review Priority
            None,                    # AI: Recommendations
        ]

    def test_hardcoded_scored_extension_row(self):
        """Second durable pin exercising the Option B prose surfaces:
        Score Adjustments components, prose justification with an
        adjustment clause, Reason for extension necessity, Legislation,
        and the AI: Evidence first-span-plus-pointer format."""
        record = _record(
            source="extension",
            extension_name="az",
            regulatory_citations=["A.R.S. 15-901"],
        )
        score = _score(
            nachos_justification="tier_0_none; +0.5 necessary_ext",
            adjusted_nachos_score=0.5,
        )
        score["dimensions"]["nachos_score"] = {
            "value": 0,
            "rule_matched": "tier_0_none",
            "confidence": "high",
            "inputs_used": {},
        }
        score["dimensions"]["extension_justification"] = {
            "value": 2,
            "rule_matched": "tier_2_necessary_companion",
            "confidence": "high",
        }
        score["fact_provenance"] = {
            "has_aggregation": {
                "value": True, "spans": ["sum of absence days"],
            },
            "extension_is_necessary": {
                "value": True, "spans": ["required for AZ statute reporting"],
            },
        }
        row = dict(
            zip(
                DETAILS_SOURCE_HEADERS,
                _extracted(DETAILS_COLUMNS, _ctx(record=record, score=score), "source"),
            )
        )
        assert row["Adjusted NACHOS Score"] == 0.5
        assert row["Base NACHOS Score"] == 0
        assert row["Score Adjustments"] == "+0.5 necessary extension"
        assert row["Justification for Adjusted NACHOS Score"] == (
            "Base 0 (no derivation logic); +0.5 necessary extension"
        )
        assert row["Unnecessary Extension ?"] == "No"
        # tier-0 gate blanks Reason for Complexity even with facts present.
        assert row["Reason for Complexity"] is None
        assert row["Reason for extension necessity"] == (
            "The extension was judged necessary but depends on other "
            "extensions (a companion design). "
            '— evidence: "required for AZ statute reporting"'
        )
        assert row["Is an extension"] == "Yes"
        assert row["Contributing extension"] == "az"
        assert row["References"] == "calendar.pdf / p.3"
        assert row["Legislation"] == "A.R.S. 15-901"
        assert row["AI: Match Status"] == "Matched (extension)"
        # First affirmative span in _EVIDENCE_FACTS scan order + pointer.
        assert row["AI: Evidence"] == (
            'has_aggregation: "sum of absence days" '
            "(+1 more — see audit workbook)"
        )
        assert row["AI: Needs Review"] == "No"


class TestScoringSummaryRowEquivalence:
    def test_hardcoded_scored_source_row(self):
        recs = [
            {"record_key": "AZ|Calendar|calendarCode", "dimension": "nachos_score", "impact": -3},
            {"record_key": "AZ|Calendar|calendarCode", "dimension": "definition_quality"},
        ]
        ctx = _ctx(score=_score(), recs=recs)
        row = _extracted(SCORING_SUMMARY_COLUMNS, ctx, "source")
        assert row == [
            "AZ",
            "Calendar",
            "calendarCode",
            2,      # canonical_name_alignment
            2,      # definition_quality
            3,      # semantic_fidelity
            None,   # extension_justification
            1,      # business_logic_complexity
            1,      # complexity_score
            1,      # nachos_score
            1.0,    # adjusted_nachos_score
            "tier_1_conditional",
            "high",
            "No",
            None,
            "2 recs: nachos_score (-3), definition_quality",
        ]

    def test_hardcoded_spine_row_with_review(self):
        score = _score(
            review={
                "needs_review": True,
                "reasons": ["a", "b"],
                "route": "SCORING",
            }
        )
        score["dimensions"].update(
            {
                "documentation_completeness": {"value": 3, "confidence": "high"},
                "obligation_clarity": {"value": 2, "confidence": "high"},
            }
        )
        ctx = _ctx(score=score)
        row = _extracted(SCORING_SUMMARY_COLUMNS, ctx, "spine")
        assert row == [
            "AZ",
            "Calendar",
            "calendarCode",
            3,      # documentation_completeness
            2,      # obligation_clarity
            1,      # business_logic_complexity
            1,      # complexity_score
            1,      # nachos_score
            1.0,
            "tier_1_conditional",
            "high",
            "Yes",
            "a; b",
            None,
        ]


class TestBackfillDerivation:
    def test_poc3_headers_derive_from_source_projection(self):
        """`ai-` prefix rule: role-filtered source projection, with any
        `AI: ` display prefix stripped FIRST so `AI: Needs Review` keeps
        its historical bare `ai-Needs Review` name (never
        `ai-AI: Needs Review`)."""
        from src.report import human_score_backfill as hsb

        assert hsb._MC_HEADERS == tuple(
            f"ai-{c.header.removeprefix('AI: ')}"
            for c in _projection_columns("source")
        )
        assert len(hsb._MC_HEADERS) == 31
        assert "ai-Needs Review" in hsb._MC_HEADERS
        assert "ai-Base NACHOS Score" in hsb._MC_HEADERS
        assert not any(h.startswith("ai-AI:") for h in hsb._MC_HEADERS)
        # Row # is renderer-injected, never a spec column, so the
        # projection can never grow a meaningless `ai-Row #`.
        assert "ai-Row #" not in hsb._MC_HEADERS
        # NOTE: `ai-Recommendations` IS present — it derives from the
        # machine `AI: Recommendations` column is EXCLUDED since issue
        # #213 item 1 (the overlay never joins recs — always-blank cells
        # were the #102 anti-pattern), as is the analyst-input band.
        assert "ai-Recommendations" not in hsb._MC_HEADERS


class TestAuditTrailFactory:
    def test_prefix_is_details_minus_analyst_band_per_lens(self):
        for lens in ("source", "spine"):
            cols = ws.audit_trail_columns(lens)
            rv = _projection_columns(lens)
            assert [c.header for c in cols[: len(rv)]] == [c.header for c in rv]
            # Audit sheet is uniformly compact — prefix widths reset to
            # the sheet default (18), never the Details 55s.
            assert all(c.width is None for c in cols[: len(rv)])
            # The empty analyst-input band stays off the audit sheet.
            assert not any(c.role == "analyst_input" for c in cols)

    def test_column_counts(self):
        # source: 32 Details projection + 12 facts + 2 span cols
        #         + 5 dims + 12 NACHOS/tail = 63 (+Row# = 64 rendered)
        assert len(ws.audit_trail_columns("source")) == 63
        # spine: 34 Details projection + 15 facts + 2 span cols
        #        + 3 dims + 12 NACHOS/tail = 66 (+Row# = 67 rendered)
        assert len(ws.audit_trail_columns("spine")) == 66

    def test_fact_block_order(self):
        for lens, facts in (
            ("source", ws._SOURCE_FACT_ORDER),
            ("spine", ws._SPINE_FACT_ORDER),
        ):
            headers = tuple(c.header for c in ws.audit_trail_columns(lens))
            # The audit prefix keeps ai_recommendations (only the
            # OVERLAY projection excludes it — issue #213 item 1) but
            # drops effective_score (issue #248 Part C — the audit
            # workbook builds contexts without adjudications; an
            # unthreaded column would be the #102 anti-pattern) and
            # review_why/review_priority (issue #259 PR 2 — same
            # unthreaded-context posture; the trail carries the raw
            # cascade reasons + route already).
            rv_len = len([
                c for c in DETAILS_COLUMNS
                if lens in c.lens
                and c.role != "analyst_input"
                and c.key not in (
                    "effective_score", "review_why", "review_priority",
                )
            ])
            assert headers[rv_len : rv_len + len(facts)] == facts
            assert headers[rv_len + len(facts) : rv_len + len(facts) + 2] == (
                "semantic_class_spans",
                "integration_class_spans",
            )

    def test_nachos_block_and_tail(self):
        for lens in ("source", "spine"):
            headers = tuple(c.header for c in ws.audit_trail_columns(lens))
            assert headers[-12:] == (
                "nachos_tier_rule",
                "nachos_adjustments",
                "nachos_score",
                "adjusted_nachos_score",
                "complexity_score",
                "confidence_composite",
                "rule_paths",
                "downgraded_facts",
                "human_corrected_facts",
                "needs_review",
                "review_route",
                "review_reasons",
            )

    def test_raw_tokens_survive_on_audit_trail(self):
        """The Details sheet renders prose justifications; the RAW rule /
        adjustment tokens must survive on the Audit Trail's
        nachos_tier_rule / nachos_adjustments columns."""
        record = _record(source="extension", extension_name="az")
        score = _score(nachos_justification="tier_0_none; +0.5 necessary_ext")
        score["dimensions"]["nachos_score"] = {
            "value": 0,
            "rule_matched": "tier_0_none",
            "confidence": "high",
            "inputs_used": {},
        }
        ctx = _ctx(record=record, score=score)
        cols = ws.audit_trail_columns("source")
        row = dict(zip([c.header for c in cols], [c.extract(ctx) for c in cols]))
        assert row["nachos_tier_rule"] == "tier_0_none"
        assert row["nachos_adjustments"] == "+0.5 necessary_ext"

    def test_row_values_align_with_headers(self):
        """Header/value alignment holds by construction — spot-check a
        scored extension row via header-name lookup."""
        record = _record(source="extension", extension_name="az")
        score = _score(nachos_justification="tier_0_none; +0.5 necessary_ext")
        score["fact_provenance"] = {
            "extension_is_necessary": {"value": True, "spans": ["quoted evidence"]},
            "semantic_class": {"value": "aligned", "spans": ["span a", "span b"]},
        }
        ctx = _ctx(record=record, score=score)
        cols = ws.audit_trail_columns("source")
        row = dict(zip([c.header for c in cols], [c.extract(ctx) for c in cols]))
        assert row["extension_is_necessary"] == "True"
        assert row["semantic_class_spans"] == "span a | span b"
        assert row["nachos_tier_rule"] == "tier_1_conditional"
        assert row["nachos_adjustments"] == "+0.5 necessary_ext"
        assert row["adjusted_nachos_score"] == 1.0
        assert row["needs_review"] == "No"
        assert row["review_reasons"] is None


class TestDictRowSheetHeaderContract:
    """Frozen header snapshots for the dict-row sheets — same posture as
    the Details / Scoring Summary snapshots above (the ColumnSpec
    definitions are otherwise these sheets' only header pin)."""

    EXPECTED = {
        "References": (
            "Domain", "Entity", "Reference", "In Ed-Fi Swagger?",
            "Observations",
        ),
        "Entities by Domain": ("Domain", "Entity", "Documented Elements"),
        "Review Queue": (
            "Row #", "Where", "Reviewed? (from Details)", "Route",
            "State", "Entity Name", "Data Element",
            "Adjusted NACHOS Score", "confidence_composite", "Why",
            "AI: Evidence",
        ),
        "Commitment Tracker": (
            "Row #", "State", "Entity Name", "Data Element",
            "Adjusted NACHOS Score", "Recommended Action",
            "NACHOS Points Resolved", "Projected Adjusted NACHOS Score",
            "Reason", "Other recommendations", "Adoption Timeline",
            "Commitment Status", "Comments",
        ),
        "API Model Gaps": (
            "State", "Entity", "Element", "Discovery", "API Model Data Type",
            "Extension Schema", "Implementation Shape", "FK Chain Depth",
            "Reference Fan-Out", "Sub-Collection Depth",
            "Descriptor Enum Breadth", "Entity Extension Footprint",
            "Documentation Style Tier", "Documentation Gap", "NACHOS Tier",
            "Adjusted NACHOS", "NACHOS Justification",
            "Confidence Composite", "Step",
        ),
        "Documentation Gaps": (
            "State", "Entity", "Element", "Discovery", "Provenance",
            "API Model Data Type", "Extension Schema",
            "Implementation Shape", "FK Chain Depth", "Reference Fan-Out",
            "Sub-Collection Depth", "Descriptor Enum Breadth",
            "Entity Extension Footprint", "Documentation Style Tier",
            "Documentation Gap", "NACHOS Tier", "Adjusted NACHOS",
            "NACHOS Justification", "Confidence Composite", "Step",
        ),
        "Recommendations": (
            "State", "Domain", "Entity", "Element", "Review Status",
            "Review Route", "Dimension", "Current Tier", "Target Tier",
            "Impact", "Recommendation", "Rationale", "Evidence Fact",
            "Fact Value", "Confidence",
        ),
        "Methodology Notes": ("Limitation", "Impact", "Why it exists"),
        "Peer Gaps": (
            "Slot", "Confidence", "States Present", "Current Posture",
            "Peer Consensus Format", "Recommended Fill", "Consensus Concept",
        ),
    }

    def test_headers_match_snapshots(self):
        for title, expected in self.EXPECTED.items():
            sheet = ws.SHEET_SPECS[title]
            assert sheet.headers_for("source") == expected, title
            # All dict-row sheets are lens-agnostic.
            assert sheet.headers_for("spine") == expected, title

    def test_registry_covers_every_sheet(self):
        assert set(ws.SHEET_SPECS) == {
            "Readme", "Update Log", "Legend", "Score Card",
            "Recommendations", "Details", "Extension Details",
            "Commitment Tracker",
            "Scoring Summary", "Review Queue", "Peer Gaps", "Audit Trail",
            "API Model Gaps", "Documentation Gaps", "References",
            "Entities by Domain", "Methodology Notes",
        }
        for sheet in ws.SHEET_SPECS.values():
            assert sheet.guide is not None and sheet.finish is not None


class TestSheetScopeConformance:
    """Option D light scope formalization: every sheet a builder writes
    must be registered and permitted by its spec's `scope` + `lens`."""

    def test_registry_scope_and_lens_declarations(self):
        by_title = ws.SHEET_SPECS
        assert by_title["Review Queue"].scope == {"per_state", "combined"}
        assert by_title["Commitment Tracker"].scope == {"per_state"}
        assert by_title["Peer Gaps"].scope == {"per_state"}
        assert by_title["API Model Gaps"].scope == {"per_state"}
        assert "spine" in by_title["API Model Gaps"].lens
        assert "source" not in by_title["API Model Gaps"].lens
        assert by_title["Documentation Gaps"].lens == ws.SOURCE

    def test_fixture_workbooks_respect_scope_and_lens(self, tmp_path):
        import openpyxl

        from tests.test_workbook_fingerprint import build_fixture_workbook

        for lens in ("source", "spine"):
            path = build_fixture_workbook(tmp_path / lens, lens)
            wb = openpyxl.load_workbook(path)
            for title in wb.sheetnames:
                if title.endswith("— Documented only"):
                    continue  # suffix-rule family (spine per-state)
                spec = ws.SHEET_SPECS[title]  # KeyError = unregistered
                assert lens in spec.lens or spec.kind == "block", (
                    f"sheet {title!r} rendered on {lens} but its spec "
                    f"declares lens={spec.lens}"
                )
                assert "per_state" in spec.scope, (
                    f"sheet {title!r} rendered on a per-state workbook "
                    f"but its spec declares scope={spec.scope}"
                )

    def test_finish_named_headers_exist_in_spec_columns(self):
        """Every header a `_SheetFinish` names (number_formats /
        color_scale / flag_yes) must exist among the sheet's own spec
        headers in at least one declared lens — the finish resolver
        silently skips misses at apply time (the one-spec-serves-both-
        lenses feature), which would also silently absorb a typo or a
        rename that forgot the finish entry (issue #213 item 1)."""
        for title, spec in ws.SHEET_SPECS.items():
            if spec.kind == "block":
                continue  # block sheets have no spec columns to check
            available: set[str] = {"Row #"}
            for lens in sorted(spec.lens):
                available.update(spec.headers_for(lens))
            if title == "Details":
                # The `--with-human` variant appends the Cmp: block at
                # render time; the shared finish legitimately names its
                # headers (they resolve only on comparison workbooks).
                for lens in sorted(spec.lens):
                    available.update(
                        ws.details_sheet_with_cmp().headers_for(lens)
                    )
            named = {h for h, _fmt in spec.finish.number_formats}
            named |= {h for h, _lo, _hi in spec.finish.color_scale}
            named |= set(spec.finish.flag_yes)
            missing = named - available
            assert not missing, (
                f"sheet {title!r}: finish names headers absent from the "
                f"spec in every declared lens: {sorted(missing)} — a "
                "rename forgot the finish entry, or a typo"
            )
