"""Tier 5 regression guards — round-3 analyst-review polish pass.

Locks in the P2/P3 items reported in `docs/archive/analyst-review-full-feedback.md`.
Grouped by finding so the next reviewer can read the file as a per-item
changelog.
"""

from __future__ import annotations

from src.ingest import minnesota
from src.report import analyst


# ---------------------------------------------------------------------------
# P2 #1 — MN composite+typo row split
# ---------------------------------------------------------------------------


class TestMNCompositeCommaListWithDottedToken:
    """Reviewer round-3 P2: MN row 132 rendered
    `CourseIdentificationSystem, CourseIndentificationCode.IdentificationCode`
    as a single matched-core cell. The cell is actually TWO distinct Ed-Fi
    elements (courseIdentificationSystemDescriptor + identificationCode, both
    under the `identificationCodes` sub-collection on `Course`). Widening
    `_expand_comma_list` to accept dotted tokens splits this into two rows.

    The second token's `CourseIndentificationCode.` prefix carries a source
    typo; the alias strip then reduces it to the bare `IdentificationCode`
    identifier, so the source typo is no longer visible on a matched row.
    """

    def test_dotted_token_participates_in_comma_split(self):
        tokens = minnesota._expand_comma_list(
            "CourseIdentificationSystem, CourseIndentificationCode.IdentificationCode"
        )
        assert tokens == [
            "CourseIdentificationSystem",
            "CourseIndentificationCode.IdentificationCode",
        ]

    def test_pure_identifier_list_still_expands(self):
        # Baseline — the pre-Tier-5 behavior for identifier-only cells must
        # continue to work (regression guard for the common MCCC row case).
        tokens = minnesota._expand_comma_list(
            "ProgramType, ProgramName, BeginDate"
        )
        assert tokens == ["ProgramType", "ProgramName", "BeginDate"]

    def test_human_phrase_with_parens_not_split(self):
        # Non-identifier tokens (spaces, parens) keep the cell whole so we
        # don't chop up author-written labels.
        value = "Course Reference (CourseIdentificationCode, CourseCode)"
        assert minnesota._expand_comma_list(value) == [value]

    def test_no_comma_passthrough(self):
        assert minnesota._expand_comma_list("schoolYear") == ["schoolYear"]

    def test_mixed_identifier_and_dotted_splits(self):
        # Confirms dotted + bare tokens in the same list both pass the widened
        # check. Order preserved so downstream pair-wise logic is stable.
        tokens = minnesota._expand_comma_list(
            "FooReference.barId, baz, Qux.quuxCode"
        )
        assert tokens == ["FooReference.barId", "baz", "Qux.quuxCode"]

    def test_dotted_token_strips_source_typo_prefix(self):
        """End-to-end: after split + alias strip, the typo-laden prefix
        `CourseIndentificationCode.` is removed and the visible element name
        is the clean bare identifier. This is the whole reason we split."""
        tokens = minnesota._expand_comma_list(
            "CourseIdentificationSystem, CourseIndentificationCode.IdentificationCode"
        )
        cleaned = [
            minnesota._clean_element_name(
                minnesota._strip_known_source_aliases(
                    minnesota._strip_entity_prefix(t, "Course")
                )
            )
            for t in tokens
        ]
        assert cleaned == ["CourseIdentificationSystem", "IdentificationCode"]


# ---------------------------------------------------------------------------
# P2 #2 — MN trailing `>` truncated nav path
# ---------------------------------------------------------------------------


class TestMNTrailingArrowNavPath:
    """Reviewer round-3 P2: MN row 118 displayed `CollegeCourseReference >`
    with a trailing `>`/whitespace residue from a truncated source path.
    `_strip_mn_nav_path` previously bailed out on an empty tail; now it
    segment-splits and filters empties so the last NAMED segment is used."""

    def test_trailing_arrow_falls_back_to_preceding_segment(self):
        assert minnesota._strip_mn_nav_path(
            "CollegeCourseReference >"
        ) == "collegeCourseReference"

    def test_trailing_arrow_no_whitespace(self):
        # `A>B>` with no trailing space still strips to `B`.
        assert minnesota._strip_mn_nav_path("Foo>Bar>") == "bar"

    def test_preserves_normal_nav_path_tail(self):
        # Regression — the earlier behavior for non-truncated nav paths
        # must still resolve to the terminal segment.
        assert minnesota._strip_mn_nav_path(
            "StudentReference>StudentUniqueId > LastSurname"
        ) == "lastSurname"

    def test_preserves_humanized_label(self):
        assert minnesota._strip_mn_nav_path(
            "StudentReference>StudentUniqueId > First Name"
        ) == "firstName"

    def test_single_arrow_still_strips(self):
        assert minnesota._strip_mn_nav_path(
            "StudentReference>StudentUniqueId"
        ) == "studentUniqueId"

    def test_no_arrow_passthrough(self):
        assert minnesota._strip_mn_nav_path("plainElement") == "plainElement"

    def test_only_arrows_returns_original(self):
        # Degenerate — `>` or `> >` with no named segments falls back to the
        # unchanged input rather than emitting an empty string.
        assert minnesota._strip_mn_nav_path(">") == ">"
        assert minnesota._strip_mn_nav_path(" > > ") == " > > "


# ---------------------------------------------------------------------------
# P3 #3 — Known Limitations blank-type wording
# ---------------------------------------------------------------------------


class TestKnownLimitationsBlankTypeWording:
    """Reviewer round-3 P3: the old KL text implied unresolved rows always
    render with a blank Data Type. In reality AZ/WI unresolved rows typically
    carry a source-inferred type; only MN's do not (the matrix has no source-
    type column). The revised wording calls this out."""

    def test_blank_type_row_present_in_all_variants(self):
        for state in (None, "AZ", "WI", "MN"):
            rows = analyst._filter_known_limitations(state)
            titles = [r[0] for r in rows]
            assert any(
                "Blank `Data Type`" in t for t in titles
            ), f"missing blank-type row for state={state}"

    def test_blank_type_wording_reflects_source_inferred_fallback(self):
        rows = analyst._filter_known_limitations(None)
        row = next(r for r in rows if r[0].startswith("Blank `Data Type`"))
        title, impact, why = row
        assert title == "Blank `Data Type` can appear on Unresolved rows"
        # Impact text must mention the source-inferred retention behavior
        # (AZ/WI) and MN's blank-only behavior explicitly.
        assert "source-inferred" in impact
        assert "MN" in impact and "blank" in impact.lower()
        # Why text must reference the canonical-type contract + audit-trail
        # fallback so analysts know the semantics are deliberate.
        assert "audit trail" in why.lower()
        assert "Match Status" in why


# ---------------------------------------------------------------------------
# P3 #5 — Known Limitations blank Ed-Fi Domain wording
# ---------------------------------------------------------------------------


class TestKnownLimitationsBlankDomainWording:
    """Reviewer round-3 P3 kept the blank-domain KL row but asked for the
    three categories (extension / cross-domain / source-specific) to be
    explicit in the entry."""

    def test_blank_domain_categories_enumerated(self):
        rows = analyst._filter_known_limitations(None)
        row = next(
            r for r in rows if "blank `Ed-Fi Domain`" in r[0]
        )
        _, impact, _ = row
        # The impact column should enumerate the three categories. We guard
        # the specific category phrasing so future rewrites don't silently
        # drop one.
        assert "state-specific extensions" in impact.lower()
        assert (
            "core ed-fi entities" in impact.lower()
            or "core entities" in impact.lower()
        )
        assert "sub-collection" in impact.lower() or "cross-domain" in impact.lower()


# ---------------------------------------------------------------------------
# P3 #4 — Coverage semantics on Score Card
# ---------------------------------------------------------------------------


class TestScoreCardCoverageSemantics:
    """Reviewer round-3 P3: the Score Card should explicitly label the two
    coverage numerators so analysts don't misread a numerator drift as a
    reconciliation defect."""

    def test_coverage_semantics_constant_mentions_both_metrics(self):
        note = analyst._COVERAGE_SEMANTICS_NOTE
        assert "source-document rows" in note.lower()
        assert "spine key" in note.lower()
        # Emphasize that divergence is deliberate, not a bug.
        assert "dedup" in note.lower() or "defect" in note.lower()

    def test_coverage_semantics_length_reasonable_for_score_card_cell(self):
        # Wrap_text on the Score Card cell expects a paragraph-length string,
        # not a novel. Bound it so renames don't turn the cell into a wall
        # of text.
        assert 100 < len(analyst._COVERAGE_SEMANTICS_NOTE) < 800
