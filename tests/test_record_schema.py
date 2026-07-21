"""Per-record schema contract enforced across every (state, lens) pair.

Scoring will read these artifacts. Any silent shape drift — a renamed
field, a retyped field, a default that flips a flag from required to
optional — breaks downstream assumptions. The goldens freeze the output
byte-for-byte; this suite freezes the *record-level invariants* so a
targeted rename or type change surfaces as a schema failure before it
shows up as a mystery diff in bucket 1.

One parametrized set across both lenses × all four states. Each record
is checked individually so the failing-record sample is actionable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingest.domain_filter import PLACEHOLDER_LABELS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tests" / "golden"

STATES = ("az", "wi", "mn", "tx", "in")
LENSES = ("source", "spine")
_CASES = [(s, l) for s in STATES for l in LENSES]

EXPECTED_FIELDS: frozenset[str] = frozenset({
    "state",
    "edfi_version",
    "domain",
    "edfi_domain",
    "entity",
    "raw_entity",
    "element_name",
    "data_type",
    "definition_text",
    "source",
    "extension_name",
    "business_rules_text",
    "element_specific_rules",
    "regulatory_citations",
    "related_entities",
    "descriptor_table_code",
    "descriptor_table_values",
    "collections_text",
    "edfi_standard_definition",
    "source_document",
    "source_page_or_section",
    "documented",
    "documentation_source",
})
assert len(EXPECTED_FIELDS) == 23  # surface this constant in one place

VALID_SOURCES: frozenset[str] = frozenset({"core", "extension", "unknown", "filtered"})
VALID_DOCUMENTATION_SOURCES: frozenset[str] = frozenset(
    {"source_doc", "swagger", "swagger_leaf"}
)


def _load_records(state: str, lens: str) -> list[dict]:
    path = _GOLDEN_DIR / f"{state}_elements_{lens}.json"
    return json.loads(path.read_text(encoding="utf-8"))["elements"]


def _is_placeholder(record: dict) -> bool:
    """Filtered-domain placeholder rows have a stable fingerprint:
    `source='filtered'` + `element_name='(filtered)'`. They're the only
    rows permitted to sit in a filtered domain with no source-doc
    grounding — everything else in a filtered domain must be retained
    with `documented=True`."""
    return record["source"] == "filtered" and record["element_name"] == "(filtered)"


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_all_23_fields_present(state: str, lens: str) -> None:
    """Every record carries exactly the 23 declared fields — no extras,
    no missing. Catches field renames, removals, and accidental
    `exclude_none` dumps that would strip optional keys. (Issue #184
    added `edfi_domain`, taking the contract 22 -> 23.)"""
    records = _load_records(state, lens)
    assert records, f"{state}/{lens}: golden empty"
    for i, r in enumerate(records):
        keys = set(r.keys())
        missing = EXPECTED_FIELDS - keys
        extra = keys - EXPECTED_FIELDS
        assert not missing and not extra, (
            f"{state}/{lens} record[{i}] (entity={r.get('entity')!r}, "
            f"element_name={r.get('element_name')!r}): "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_required_non_null_fields(state: str, lens: str) -> None:
    """Five string identity fields must be non-None and non-empty:
    state, edfi_version, entity, element_name. `documented` must be a
    real bool (never None). `domain` is checked separately — it may be
    empty in spine-lens for entities the Ed-Fi domain map doesn't cover.
    """
    for i, r in enumerate(_load_records(state, lens)):
        for key in ("state", "edfi_version", "entity", "element_name"):
            assert isinstance(r[key], str) and r[key], (
                f"{state}/{lens} record[{i}]: {key}={r[key]!r} "
                f"must be a non-empty string"
            )
        assert isinstance(r["domain"], str), (
            f"{state}/{lens} record[{i}] (entity={r['entity']!r}): "
            f"domain={r['domain']!r} must be a string (may be empty "
            f"in spine-lens)"
        )
        assert isinstance(r["documented"], bool), (
            f"{state}/{lens} record[{i}] (entity={r['entity']!r}): "
            f"documented={r['documented']!r} must be bool"
        )


@pytest.mark.parametrize("state", STATES)
def test_source_lens_domain_non_empty(state: str) -> None:
    """Authored source-lens records always carry a domain — adapters
    populate it from the source doc's section header. An empty domain on
    an authored row would mean an adapter lost its grouping context.

    Swagger-backfilled rows (issue #70 v21 close-out posture; issue
    #147 leaf-level extension) inherit their domain from the spine
    catalog via ``ingest.shared._entity_domain``'s longest-prefix /
    sibling fallback. A small residue of swagger / swagger_leaf rows
    on Ed-Fi abstract base classes (``GeneralStudentProgramAssociation``,
    etc.) genuinely has no single domain in the spine — those rows are
    allowed empty here; the workbook layer renders the cell blank.
    """
    for i, r in enumerate(_load_records(state, "source")):
        if r.get("documentation_source") in ("swagger", "swagger_leaf"):
            # See docstring — backfilled rows inherit from spine;
            # abstract-base residue is acceptable to be empty.
            continue
        assert r["domain"], (
            f"{state}/source record[{i}] (entity={r['entity']!r}, "
            f"element_name={r['element_name']!r}): domain unexpectedly empty"
        )


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_source_enum_membership(state: str, lens: str) -> None:
    """`source` is always one of the four Literal values from
    ElementRecord. Scoring branches on this — an unexpected value
    would silently route a row to the wrong dimension."""
    for i, r in enumerate(_load_records(state, lens)):
        assert r["source"] in VALID_SOURCES, (
            f"{state}/{lens} record[{i}]: source={r['source']!r} "
            f"not in {sorted(VALID_SOURCES)}"
        )


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_documentation_source_enum_membership(state: str, lens: str) -> None:
    """`documentation_source` is always one of the three Literal values
    from ElementRecord. Issue #70 — entity-level swagger backfill sets
    ``"swagger"``; issue #147 — leaf-level cross-lens borrow sets
    ``"swagger_leaf"``; everything else stays ``"source_doc"`` by
    construction."""
    for i, r in enumerate(_load_records(state, lens)):
        assert r["documentation_source"] in VALID_DOCUMENTATION_SOURCES, (
            f"{state}/{lens} record[{i}]: documentation_source="
            f"{r['documentation_source']!r} not in "
            f"{sorted(VALID_DOCUMENTATION_SOURCES)}"
        )


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_list_fields_are_lists(state: str, lens: str) -> None:
    """Three list-typed fields: always a list (possibly empty), never
    None. Pydantic defaults them via factory, so a None here would
    signal a hand-constructed record that bypassed the model."""
    for i, r in enumerate(_load_records(state, lens)):
        for key in ("regulatory_citations", "related_entities", "descriptor_table_values"):
            assert isinstance(r[key], list), (
                f"{state}/{lens} record[{i}] (entity={r['entity']!r}): "
                f"{key}={r[key]!r} must be a list"
            )


@pytest.mark.parametrize("state", STATES)
def test_source_lens_all_documented(state: str) -> None:
    """Authored source-driven records carry ``documented=True`` by
    construction — every row came from the state's source document. A
    False value on an authored row would mean a hybrid-append path leaked
    into the source artifact, which the lens-separation contract forbids.

    Swagger-backfilled rows (``documentation_source in {"swagger",
    "swagger_leaf"}``; issue #70 v21 close-out posture for entity-level,
    issue #147 for leaf-level) ride along in the source-lens artifact
    for visibility but carry ``documented=False`` — they're surfaced
    for workbook + reviewer-comparison without claiming the state
    authored them.
    """
    for i, r in enumerate(_load_records(state, "source")):
        if r.get("documentation_source") in ("swagger", "swagger_leaf"):
            assert r["documented"] is False, (
                f"{state}/source record[{i}] (entity={r['entity']!r}, "
                f"element_name={r['element_name']!r}): backfilled "
                f"row must carry documented=False under v21/v26"
            )
            continue
        assert r["documented"] is True, (
            f"{state}/source record[{i}] (entity={r['entity']!r}, "
            f"element_name={r['element_name']!r}): authored row must carry "
            f"documented=True"
        )


@pytest.mark.parametrize("state", STATES)
def test_source_lens_has_no_filtered_rows(state: str) -> None:
    """Filter-domain placeholders are a spine-lens-only construct.
    Source-lens keeps state-documented filtered-domain rows verbatim;
    no `source='filtered'` rows should appear."""
    for i, r in enumerate(_load_records(state, "source")):
        assert r["source"] != "filtered", (
            f"{state}/source record[{i}]: source='filtered' forbidden "
            f"in source-lens"
        )


@pytest.mark.parametrize("state", STATES)
def test_spine_lens_filtered_rows_are_placeholders(state: str) -> None:
    """Every spine-lens `source='filtered'` row is a well-formed
    placeholder: documented=False, entity='<Label> (filtered)',
    element_name='(filtered)', domain is one of PLACEHOLDER_LABELS.
    Scoring's filter-aware path will key on this exact shape."""
    for i, r in enumerate(_load_records(state, "spine")):
        if r["source"] != "filtered":
            continue
        assert r["documented"] is False, (
            f"{state}/spine record[{i}]: filtered placeholder must have "
            f"documented=False, got {r['documented']}"
        )
        assert r["element_name"] == "(filtered)", (
            f"{state}/spine record[{i}]: filtered placeholder must have "
            f"element_name='(filtered)', got {r['element_name']!r}"
        )
        assert r["entity"].endswith(" (filtered)"), (
            f"{state}/spine record[{i}]: filtered placeholder entity "
            f"should end with ' (filtered)', got {r['entity']!r}"
        )
        assert r["domain"] in PLACEHOLDER_LABELS, (
            f"{state}/spine record[{i}]: filtered domain "
            f"{r['domain']!r} not in {PLACEHOLDER_LABELS}"
        )


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_unknown_source_implies_documented(state: str, lens: str) -> None:
    """`source='unknown'` means the row didn't land on any spine slot,
    but it came from the source doc — so it's always documented=True
    in both lenses. (In spine-lens these are the hybrid-append rows;
    in source-lens they're the literal Unresolved tail.)"""
    for i, r in enumerate(_load_records(state, lens)):
        if r["source"] != "unknown":
            continue
        assert r["documented"] is True, (
            f"{state}/{lens} record[{i}] (entity={r['entity']!r}, "
            f"element_name={r['element_name']!r}): source='unknown' "
            f"implies documented=True, got {r['documented']}"
        )


@pytest.mark.parametrize("state", STATES)
def test_spine_lens_edfi_standard_definition_nullability(state: str) -> None:
    """`edfi_standard_definition` may be None ONLY when the row is a
    filtered placeholder, a hybrid-append (source='unknown'), or an
    extension whose spine emit carries no description. Rows with
    `source in {core, extension}` that are spine-originated (not
    appended) should carry the spine description when one exists —
    an unexpected None here signals a broken enrichment path."""
    for i, r in enumerate(_load_records(state, "spine")):
        esd = r["edfi_standard_definition"]
        if r["source"] == "filtered":
            assert esd is None, (
                f"{state}/spine record[{i}]: filtered placeholder should "
                f"have edfi_standard_definition=None, got {esd!r}"
            )
            continue
        if r["source"] == "unknown":
            assert esd is None, (
                f"{state}/spine record[{i}] (hybrid-append): "
                f"edfi_standard_definition must be None, got {esd!r}"
            )
            continue
        # Core emits always carry a spine description (Ed-Fi swagger docs
        # every property); extensions may occasionally lack one (some state
        # extensions ship property stubs without descriptions).
        if r["source"] == "core":
            assert esd is not None, (
                f"{state}/spine record[{i}] (core emit, entity="
                f"{r['entity']!r}, element_name={r['element_name']!r}): "
                f"edfi_standard_definition unexpectedly None"
            )


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_state_field_matches_parametrize(state: str, lens: str) -> None:
    """Every record's `state` field matches the enclosing file's state
    (uppercase). Catches mis-plated records slipping between adapters."""
    expected = state.upper()
    for i, r in enumerate(_load_records(state, lens)):
        assert r["state"] == expected, (
            f"{state}/{lens} record[{i}]: state={r['state']!r} "
            f"expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# Issue #184 — `domain` (Source Area) vs `edfi_domain` separation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_edfi_domain_is_string_or_none(state: str, lens: str) -> None:
    """`edfi_domain` is always a string or None — never missing, never a
    non-string. It is the cross-state Ed-Fi domain, spine-derived from the
    entity; None only when the entity resolves to no spine domain."""
    for i, r in enumerate(_load_records(state, lens)):
        assert "edfi_domain" in r, (
            f"{state}/{lens} record[{i}]: edfi_domain key missing"
        )
        assert r["edfi_domain"] is None or isinstance(r["edfi_domain"], str), (
            f"{state}/{lens} record[{i}] (entity={r['entity']!r}): "
            f"edfi_domain={r['edfi_domain']!r} must be str or None"
        )


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_no_split_edfi_domain_per_entity(state: str, lens: str) -> None:
    """Issue #184 invariant: every record of a given entity shares ONE
    `edfi_domain`, regardless of provenance (documented vs swagger vs
    undocumented). This is the bug the issue closed — the old `domain`
    field carried the Source Area for documented rows and the Ed-Fi domain
    for swagger rows, so grouping by it mis-bucketed siblings of the same
    concept. `edfi_domain` is computed purely from the entity, so it must
    not split. Filtered placeholders are synthetic (their entity name is
    `<Label> (filtered)`) and excluded."""
    by_entity: dict[str, set] = {}
    for r in _load_records(state, lens):
        if r["source"] == "filtered":
            continue
        by_entity.setdefault(r["entity"], set()).add(r["edfi_domain"])
    split = {e: doms for e, doms in by_entity.items() if len(doms) > 1}
    assert not split, (
        f"{state}/{lens}: {len(split)} entities carry split edfi_domain "
        f"tags (should be one per entity): "
        f"{dict(list(split.items())[:5])}"
    )


@pytest.mark.parametrize("state", STATES)
def test_swagger_rows_have_blank_source_area(state: str) -> None:
    """Issue #184: swagger / swagger_leaf backfill rows have no source-
    document area, so `domain` (Source Area) is the empty string. Their
    Ed-Fi domain lives in `edfi_domain` instead."""
    for i, r in enumerate(_load_records(state, "source")):
        if r.get("documentation_source") in ("swagger", "swagger_leaf"):
            assert r["domain"] == "", (
                f"{state}/source record[{i}] (entity={r['entity']!r}, "
                f"element_name={r['element_name']!r}): swagger row must have "
                f"blank Source Area domain, got {r['domain']!r}"
            )


@pytest.mark.parametrize("state", STATES)
def test_spine_undocumented_rows_have_blank_source_area(state: str) -> None:
    """Issue #184: spine-lens positions the source doc never mentioned have
    no Source Area — `domain` is "". (Documented spine rows carry the real
    Source Area threaded from the matched source row; hybrid-append
    source='unknown' rows keep their own area; filtered placeholders keep
    their label.)"""
    for i, r in enumerate(_load_records(state, "spine")):
        if r["source"] in ("unknown", "filtered"):
            continue
        if r.get("documentation_source") != "source_doc":
            continue
        if not r["documented"]:
            assert r["domain"] == "", (
                f"{state}/spine record[{i}] (entity={r['entity']!r}, "
                f"element_name={r['element_name']!r}): undocumented spine "
                f"position must have blank Source Area, got {r['domain']!r}"
            )


def test_element_record_rejects_unknown_fields() -> None:
    """`extra="forbid"` on the contract models (issue #211 item 4).

    Pydantic's default `extra="ignore"` silently dropped a ghost
    `extraction_confidence` kwarg from the WI adapter on every run — a
    misspelled optional field in any adapter would silently default and
    ship wrong data with zero signal. The contract now rejects unknown
    keys at construction.
    """
    import pydantic

    from src.models.element import ElementRecord, StateElements

    valid = {
        "state": "AZ",
        "edfi_version": "3",
        "domain": "Test",
        "entity": "Student",
        "element_name": "studentUniqueId",
        "definition_text": "x",
        "documented": True,
    }
    ElementRecord(**valid)  # sanity: the valid shape still constructs
    with pytest.raises(pydantic.ValidationError, match="extraction_confidence"):
        ElementRecord(**valid, extraction_confidence=0.85)
    with pytest.raises(pydantic.ValidationError, match="bogus_field"):
        StateElements(
            state="AZ",
            edfi_version="3",
            extracted_at="2026-07-08T00:00:00Z",
            element_count=0,
            elements=[],
            bogus_field=1,
        )
