"""Tests for element-name alias generation in utils/matching.py.

Coverage focuses on the alias expansions added to close MN's source-
coverage gap (see [touchdownllc/nachos-ai-poc-3#2]). These aliases
flow into `record_match_keys`, which is what state adapters use to
decide whether a source record matches a spine element slot.
"""

from src.utils.matching import element_aliases, entity_match_form, record_match_keys


class TestPathTailSplit:
    def test_dotted_path_emits_tail(self):
        """`school.schoolId` -> `schoolId` so nested FK forms match plain slots."""
        aliases = element_aliases("school.schoolId")
        assert "schoolId" in aliases

    def test_colon_path_splits_like_dot(self):
        """WI Confluence uses colon separators; must behave identically."""
        aliases = element_aliases("programReference: programName")
        assert "programName" in aliases


class TestWhitespaceCollapse:
    def test_two_word_label_collapses_to_pascal_and_camel(self):
        """MN matrix cell `Course Code` should match spine `courseCode`."""
        aliases = element_aliases("Course Code")
        assert "CourseCode" in aliases
        assert "courseCode" in aliases

    def test_whitespace_collapse_strips_parenthetical(self):
        """`Course Offering Reference (Local Course Code, School, Session)`
        collapses to `CourseOfferingReference` / `courseOfferingReference`."""
        aliases = element_aliases(
            "Course Offering Reference (Local Course Code, School, Session)"
        )
        assert "courseOfferingReference" in aliases

    def test_single_word_unchanged(self):
        """No internal whitespace means no new camelCase alias."""
        aliases = element_aliases("calendarCode")
        # Only the expected normalizations; no invented PascalCase form.
        assert "calendarCode" in aliases
        assert "CalendarCode" not in aliases


class TestArrowPathTail:
    def test_arrow_separated_path_emits_tail(self):
        """MN matrix uses `>` between nav segments
        (`StudentReference>StudentUniqueId`); tail must split out for matching."""
        aliases = element_aliases("StudentReference>StudentUniqueId")
        assert "StudentUniqueId" in aliases

    def test_arrow_with_spaces(self):
        """`Section > CourseOfferingReference > Course` should expose tail."""
        aliases = element_aliases("Section > CourseOfferingReference > Course")
        assert "Course" in aliases


class TestPluralDescriptorAlias:
    def test_s_descriptor_singularized(self):
        """`courseLevelCharacteristicsDescriptor` (MN plural) should also alias
        to `courseLevelCharacteristicDescriptor` (Ed-Fi singular form)."""
        aliases = element_aliases("courseLevelCharacteristicsDescriptor")
        assert "courseLevelCharacteristicDescriptor" in aliases

    def test_descriptor_without_plural_stem_unchanged(self):
        """Singular `*Descriptor` (no `s` before `Descriptor`) shouldn't fire
        the plural-stripping rule — guard against producing nonsense like
        stripping a final consonant arbitrarily."""
        aliases = element_aliases("calendarTypeDescriptor")
        # The original alias is present, and no truncated form was invented.
        assert "calendarTypeDescriptor" in aliases
        assert "calendarTypDescriptor" not in aliases
        assert "calendarTypeDescript" not in aliases


class TestRecordMatchKeys:
    def test_entity_lowercased_and_element_lowercased(self):
        """record_match_keys normalizes entity AND lowercases element aliases."""
        keys = record_match_keys("Calendar", "CalendarType")
        assert ("calendar", "calendartype") in keys

    def test_nested_element_emits_tail(self):
        """FK-reference-style element names include the tail as an alias."""
        keys = record_match_keys("Calendar", "schoolReference.schoolId")
        assert ("calendar", "schoolid") in keys

    def test_whitespace_label_via_record_match_keys(self):
        """Humanized labels flow through: MN `Primary Disability` on
        `StudentSpecialEducationProgramAssociation` emits a `primaryDisability`
        alias pair so spine matching can succeed without per-row cleanup."""
        keys = record_match_keys("StudentSpecialEducationProgramAssociation", "Primary Disability")
        # Entity normalization lowercases and depluralizes; element is lowercased.
        matched = {k for k in keys if k[1] == "primarydisability"}
        assert matched, f"expected primarydisability alias, got {sorted(keys)}"


class TestStateExtensionPrefix:
    def test_tx_prefix_stripped(self):
        """Spine extension entity `tx_studentApplication` joins to source-doc
        unprefixed `StudentApplication` after the prefix-strip rule."""
        assert entity_match_form("tx_studentApplication") == entity_match_form(
            "StudentApplication"
        )

    def test_other_state_prefixes(self):
        """Strip rule covers wi_, mn_, az_ in addition to tx_."""
        for prefix in ("wi_", "mn_", "az_"):
            assert entity_match_form(f"{prefix}calendar") == entity_match_form("Calendar")

    def test_underscore_without_state_prefix_preserved(self):
        """Generic underscores (e.g. `foo_bar`) are not state-extension prefixes
        and must not be stripped."""
        # `xy_` is not in the whitelist, so the leading segment stays.
        assert entity_match_form("xy_foo") == "xy_foo"

    def test_short_inputs_not_overstripped(self):
        """Inputs of length ≤ 3 cannot be a state-extension form; guard against
        mangling them."""
        assert entity_match_form("tx_") == "tx_"

    def test_idoe_underscore_prefix_stripped(self):
        """IDOE 1.0.0 extension entities use `idoe_` (underscore) on the spine
        side. Issue #160."""
        assert entity_match_form("idoe_schoolExtension") == entity_match_form(
            "SchoolExtension"
        )

    def test_idoe_path_prefix_stripped(self):
        """The reviewer file names IDOE extension entities with the
        API-path-style ``idoe/`` separator (e.g. captured from a swagger
        walk). The matcher must collide that with the bare PascalCase form
        the IN sidecar emits. Issue #160 — this was the root cause of IN's
        51.9% source-lens reviewer match rate; with the fix IN reaches
        100% (52/52)."""
        assert entity_match_form(
            "idoe/educationOrganizationOtherPersonnel"
        ) == entity_match_form("EducationOrganizationOtherPersonnel")
        assert entity_match_form(
            "idoe/studentAccommodation"
        ) == entity_match_form("StudentAccommodation")

    def test_ed_fi_path_prefix_stripped(self):
        """The per-state human-scored basis (2026-07-07) names IN core
        entities with the API-path-style ``ed-fi/`` namespace prefix —
        the core-namespace analogue of ``idoe/`` (29 rows in the IN
        workbook). Strip it so those rows join the unprefixed sidecar
        entities instead of landing in ``no_mc_row``."""
        assert entity_match_form(
            "ed-fi/studentSchoolAssociation"
        ) == entity_match_form("StudentSchoolAssociation")
        assert entity_match_form(
            "ed-fi/staffEducationOrganizationEmploymentAssociation"
        ) == entity_match_form("StaffEducationOrganizationEmploymentAssociation")
        # `ed-fi` without a separator, or mid-name, must not strip.
        assert entity_match_form("edfimatters") == "edfimatter"

    def test_edfi_dot_namespace_prefix_stripped(self):
        """The IN workbook's dominant naming is the IDOE-sheet
        dot-namespace style (``edfi.StudentEducationOrganizationAssociation``
        — ~295 of 362 rows). Without the strip those rows crater IN's
        match rate to 18% (296 ``no_mc_row``); with it, 262 of the
        295 resolve (the residual is genuine IDOE-doc coverage)."""
        assert entity_match_form(
            "edfi.StudentEducationOrganizationAssociation"
        ) == entity_match_form("StudentEducationOrganizationAssociation")
        assert entity_match_form("idoe.schoolExtension") == entity_match_form(
            "SchoolExtension"
        )
        # A bare dotted name outside the whitelist must not strip.
        assert entity_match_form("foo.bar") == "foo.bar"


class TestDerivedStatePrefixRegex:
    """Issue #213 item 2: `_STATE_EXT_PREFIX_RE` derives its per-state
    tokens from `src.states.STATE_INFO` (the IN 18.2%→94.5% episode was
    this literal silently missing `idoe`). Behavior must be EXACTLY the
    pre-derivation literal's."""

    # The literal as committed before the derivation (2026-07-07 shape).
    _LEGACY = r"^(?:tx|wi|mn|az|idoe|ed-?fi)[_/.](?=[A-Za-z0-9])"

    # Positive and negative probes covering every token, every separator,
    # case-insensitivity, and the lookahead guard.
    _PROBES = (
        "tx_studentApplication",
        "TX_StudentApplication",
        "wi_credentialExtension",
        "mn.calendarExtension",
        "az/section",
        "idoe/educationOrganizationOtherPersonnel",
        "idoe.schoolExtension",
        "edfi.StudentEducationOrganizationAssociation",
        "ed-fi/staffEducationOrganizationEmploymentAssociation",
        "edFi_something",
        # negatives: no separator / bare separator / non-whitelist tokens
        "edfimatters",
        "tx_",
        "in_foo",
        "index.value",
        "foo.bar",
        "Student_x",
        "education.organization",
        "xy_foo",
    )

    def test_behavior_identical_to_legacy_literal(self):
        import re

        from src.utils.matching import _STATE_EXT_PREFIX_RE

        legacy = re.compile(self._LEGACY, re.IGNORECASE)
        for name in self._PROBES:
            assert (
                _STATE_EXT_PREFIX_RE.sub("", name, count=1)
                == legacy.sub("", name, count=1)
            ), f"derived regex diverges from legacy literal on {name!r}"
            assert bool(_STATE_EXT_PREFIX_RE.match(name)) == bool(
                legacy.match(name)
            ), f"match() divergence on {name!r}"

    def test_every_state_prefix_is_derived_in(self):
        from src.states import STATE_INFO
        from src.utils.matching import _STATE_EXT_PREFIX_RE

        for state, info in STATE_INFO.items():
            for prefix in info.ext_prefixes:
                assert _STATE_EXT_PREFIX_RE.match(f"{prefix}_Something"), (
                    f"{state}'s ext prefix {prefix!r} not covered by the "
                    "derived _STATE_EXT_PREFIX_RE"
                )


class TestEntityMatchFormRename:
    def test_compat_alias_is_same_object(self):
        """`normalize_entity` survives as a deprecated alias (still imported
        by `ingest.shared.canonical_spine_emit_keys` — alias-grammar
        territory, issue #213 item 2 primary workstream)."""
        from src.utils import matching

        assert matching.normalize_entity is matching.entity_match_form
