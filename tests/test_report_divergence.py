"""Smoke tests for the lens-divergence report.

Builds minimal source+spine StateElements in a tmp dir, runs
`divergence.run(out=tmp)`, validates the JSON and Markdown output.

`TestRealDataInvariants` re-runs the divergence logic against the
committed goldens (+ data/spine/ when present) to pin structural
invariants the synthetic smoke cases can't cover: three-way
disjointness across full bucket contents, filtered-domain routing
on real data, and count-conservation against the underlying
source-/spine-lens artifacts. Skipped cleanly when data/spine/ is
absent (CI without a fresh spine fetch)."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import click
import pytest

from src.ingest.domain_filter import entity_filter_domain
from src.models.element import ElementRecord, StateElements
from src.models.spine import StateSpine
from src.report import divergence
from src.utils.matching import record_match_keys


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self):
        assert not isinstance(divergence.run, click.Command)
        assert inspect.isfunction(divergence.run)


def _write_pair(tmp: Path, state: str, source_rows: list[ElementRecord], spine_rows: list[ElementRecord]) -> Path:
    """Write source+spine elements JSON AND a minimal spine JSON under
    `tmp/data/{out,spine}/`. Returns the spine base path so callers can
    thread it into `divergence.run(..., spine_base=...)` without
    depending on ambient `data/spine/` state (fresh-clone friendly)."""
    out = tmp / "data" / "out"
    out.mkdir(parents=True, exist_ok=True)
    src = StateElements(
        state=state,
        edfi_version="4.0",
        extracted_at=datetime.now(timezone.utc),
        element_count=len(source_rows),
        elements=source_rows,
    )
    spn = StateElements(
        state=state,
        edfi_version="4.0",
        extracted_at=datetime.now(timezone.utc),
        element_count=len(spine_rows),
        elements=spine_rows,
    )
    (out / f"{state.lower()}_elements_source.json").write_text(
        src.model_dump_json(indent=2), encoding="utf-8"
    )
    (out / f"{state.lower()}_elements_spine.json").write_text(
        spn.model_dump_json(indent=2), encoding="utf-8"
    )
    return _write_empty_spine(tmp, state)


def _write_empty_spine(tmp: Path, state: str) -> Path:
    """Write a minimal StateSpine JSON with no entities under
    `tmp/data/spine/{state}_spine.json`. Sufficient for synthetic
    divergence tests that don't exercise filtered-domain routing (the
    `_is_filtered` predicate returns False for every entity against an
    empty catalog). Returns the spine base path."""
    from src.models.edfi_catalog import EdFiCatalog
    from src.models.spine import SpineSourceURLs, StateSpine

    catalog = EdFiCatalog(
        version="4.0.0",
        entity_count=0,
        extension_count=0,
        entities={},
        extensions={},
    )
    spine = StateSpine(
        state=state,
        edfi_version="4.0.0",
        fetched_at=datetime.now(timezone.utc),
        source_urls=SpineSourceURLs(resources="https://example/resources.json"),
        catalog=catalog,
    )
    spine_base = tmp / "data" / "spine"
    spine_base.mkdir(parents=True, exist_ok=True)
    (spine_base / f"{state.lower()}_spine.json").write_text(
        spine.model_dump_json(indent=2), encoding="utf-8"
    )
    return spine_base


def _record(entity: str, elem: str, *, source: str = "core", documented: bool = True) -> ElementRecord:
    return ElementRecord(
        state="TX",
        edfi_version="4.0",
        domain="Test",
        entity=entity,
        element_name=elem,
        definition_text=f"def for {elem}",
        source=source,
        documented=documented,
    )


class TestPerStateBlock:
    def test_record_counts_and_delta(self, tmp_path):
        source_rows = [_record("Student", "firstName"), _record("Student", "lastName")]
        spine_rows = [
            _record("Student", "firstName"),
            _record("Student", "lastName"),
            _record("Student", "middleName", documented=False),
        ]
        spine_base = _write_pair(tmp_path, "TX", source_rows, spine_rows)

        report = divergence.run(
            states=("TX",), out=tmp_path / "data" / "out", spine_base=spine_base
        )
        block = report["per_state"][0]
        assert block["state"] == "TX"
        assert block["record_counts"]["source"] == 2
        assert block["record_counts"]["spine"] == 3
        assert block["record_counts"]["delta"] == 1

    def test_spine_missing_bucket_counts_source_unknown_rows(self, tmp_path):
        source_rows = [
            _record("Student", "firstName"),
            _record("LegacyEntity", "legacyField", source="unknown"),
            _record("LegacyEntity", "anotherLegacy", source="unknown"),
        ]
        spine_rows = [
            _record("Student", "firstName"),
            # Hybrid append preserves unknowns from source
            _record("LegacyEntity", "legacyField", source="unknown"),
            _record("LegacyEntity", "anotherLegacy", source="unknown"),
        ]
        spine_base = _write_pair(tmp_path, "TX", source_rows, spine_rows)

        report = divergence.run(
            states=("TX",), out=tmp_path / "data" / "out", spine_base=spine_base
        )
        block = report["per_state"][0]
        assert block["buckets"]["spine_missing"]["count"] == 2
        sample_elements = {
            s["element_name"] for s in block["buckets"]["spine_missing"]["samples"]
        }
        assert "legacyField" in sample_elements
        assert "anotherLegacy" in sample_elements

    def test_undocumented_bucket_counts_spine_lens_documented_false(self, tmp_path):
        source_rows = [_record("Student", "firstName")]
        spine_rows = [
            _record("Student", "firstName", documented=True),
            _record("Student", "lastName", documented=False),
            _record("Student", "middleName", documented=False),
            _record("Student", "birthDate", documented=False),
        ]
        spine_base = _write_pair(tmp_path, "TX", source_rows, spine_rows)

        report = divergence.run(
            states=("TX",), out=tmp_path / "data" / "out", spine_base=spine_base
        )
        block = report["per_state"][0]
        assert block["buckets"]["undocumented"]["count"] == 3
        sample_elements = {
            s["element_name"] for s in block["buckets"]["undocumented"]["samples"]
        }
        assert sample_elements == {"lastName", "middleName", "birthDate"}

    def test_undocumented_excludes_spine_missing_append_rows(self, tmp_path):
        """The hybrid-append rows (source=unknown, documented=True) should
        NOT show up in the undocumented bucket — that bucket is reserved
        for canonical spine slots with no source coverage."""
        source_rows = [_record("Legacy", "fld", source="unknown")]
        spine_rows = [
            # One real undocumented core emit
            _record("Student", "firstName", documented=False),
            # One hybrid-append row (source=unknown; documented=True by construction)
            _record("Legacy", "fld", source="unknown", documented=True),
        ]
        spine_base = _write_pair(tmp_path, "TX", source_rows, spine_rows)

        report = divergence.run(
            states=("TX",), out=tmp_path / "data" / "out", spine_base=spine_base
        )
        block = report["per_state"][0]
        assert block["buckets"]["undocumented"]["count"] == 1
        assert block["buckets"]["spine_missing"]["count"] == 1

    def test_score_delta_is_not_yet_scored(self, tmp_path):
        spine_base = _write_pair(tmp_path, "TX", [], [])
        report = divergence.run(
            states=("TX",), out=tmp_path / "data" / "out", spine_base=spine_base
        )
        block = report["per_state"][0]
        assert block["score_delta"]["scored"] is False

    def test_alias_miss_separates_almost_matches_from_genuine_gaps(self, tmp_path):
        # Source names `Student.Address` (Pascal sub-entity form).
        # Spine emit uses `Student.addresses` (plural camel sub-collection).
        # Source alias expansion via record_match_keys produces
        # `(student, address)`; spine emit's own expansion produces
        # `(student, address)` too (via plural-strip). So their alias sets
        # intersect — this is an alias_miss (almost-matched), NOT a pure
        # undocumented.
        # `Student.otherField` has no source coverage at all → stays
        # undocumented.
        source_rows = [_record("Student", "Address")]
        spine_rows = [
            _record("Student", "addresses", documented=False),
            _record("Student", "otherField", documented=False),
        ]
        spine_base = _write_pair(tmp_path, "TX", source_rows, spine_rows)

        report = divergence.run(
            states=("TX",), out=tmp_path / "data" / "out", spine_base=spine_base
        )
        block = report["per_state"][0]
        # Exactly one of each.
        assert block["buckets"]["alias_miss"]["count"] == 1
        assert block["buckets"]["undocumented"]["count"] == 1
        alias_names = {
            s["element_name"] for s in block["buckets"]["alias_miss"]["samples"]
        }
        undoc_names = {
            s["element_name"] for s in block["buckets"]["undocumented"]["samples"]
        }
        assert alias_names == {"addresses"}
        assert undoc_names == {"otherField"}

    def test_alias_miss_empty_when_undocumented_rows_have_no_source_overlap(
        self, tmp_path
    ):
        # Pure gap — no overlap means alias_miss=0 and undocumented carries
        # the full count. Guards against the detector trivially claiming
        # everything is an alias_miss.
        source_rows = [_record("Student", "firstName")]
        spine_rows = [
            _record("Student", "firstName", documented=True),
            _record("Teacher", "salary", documented=False),
            _record("Teacher", "department", documented=False),
        ]
        spine_base = _write_pair(tmp_path, "TX", source_rows, spine_rows)

        report = divergence.run(
            states=("TX",), out=tmp_path / "data" / "out", spine_base=spine_base
        )
        block = report["per_state"][0]
        assert block["buckets"]["alias_miss"]["count"] == 0
        assert block["buckets"]["undocumented"]["count"] == 2


class TestFilteredDomainExclusion:
    """Rows on SIS-never-populated entities must be excluded from the
    divergence buckets — they're collapsed into spine-lens placeholders,
    not a real coverage gap."""

    def _write_spine_json(self, tmp: Path, state: str) -> Path:
        from datetime import datetime, timezone

        from src.models.edfi_catalog import EdFiCatalog, EntityEntry, PropertyInfo
        from src.models.spine import SpineSourceURLs, StateSpine

        catalog = EdFiCatalog(
            version="4.0.0",
            entity_count=2,
            extension_count=0,
            entities={
                "Student": EntityEntry(
                    description="Student",
                    domains=["StudentIdentificationAndDemographics"],
                    properties={
                        "studentUniqueId": PropertyInfo(type="string", is_identity=True),
                    },
                ),
                "SurveyResponse": EntityEntry(
                    description="SurveyResponse",
                    domains=["Survey"],
                    properties={
                        "id": PropertyInfo(type="string", is_identity=True),
                    },
                ),
            },
            extensions={},
        )
        spine = StateSpine(
            state=state,
            edfi_version="4.0.0",
            fetched_at=datetime.now(timezone.utc),
            source_urls=SpineSourceURLs(resources="https://example/resources.json"),
            catalog=catalog,
        )
        spine_base = tmp / "data" / "spine"
        spine_base.mkdir(parents=True, exist_ok=True)
        (spine_base / f"{state.lower()}_spine.json").write_text(
            spine.model_dump_json(indent=2), encoding="utf-8"
        )
        return spine_base

    def test_filtered_domain_rows_remain_in_spine_missing(self, tmp_path):
        # Soft-filter: documented intent in a filtered domain still belongs
        # in the spine_missing bucket. The bucket counts state-documented
        # source rows that the spine couldn't slot — that gap remains
        # whether the entity is filtered or not.
        source_rows = [
            _record("Student", "firstName"),
            _record("SurveyResponse", "rogueElement", source="unknown"),
        ]
        spine_rows = [_record("Student", "firstName", documented=True)]
        _write_pair(tmp_path, "TX", source_rows, spine_rows)
        spine_base = self._write_spine_json(tmp_path, "TX")

        report = divergence.run(
            states=("TX",),
            out=tmp_path / "data" / "out",
            spine_base=spine_base,
        )
        block = report["per_state"][0]
        assert block["buckets"]["spine_missing"]["count"] == 1

    def test_filtered_domain_rows_skip_undocumented_and_alias_miss(self, tmp_path):
        # Two undocumented spine rows — one on Student (keep), one on
        # SurveyResponse (drop). Also a source row naming the SurveyResponse
        # slot (would normally trigger alias_miss); it must also be skipped.
        source_rows = [_record("SurveyResponse", "id")]
        spine_rows = [
            _record("Student", "firstName", documented=False),
            _record("SurveyResponse", "id", documented=False),
        ]
        _write_pair(tmp_path, "TX", source_rows, spine_rows)
        spine_base = self._write_spine_json(tmp_path, "TX")

        report = divergence.run(
            states=("TX",),
            out=tmp_path / "data" / "out",
            spine_base=spine_base,
        )
        block = report["per_state"][0]
        assert block["buckets"]["alias_miss"]["count"] == 0
        assert block["buckets"]["undocumented"]["count"] == 1
        undoc_names = {
            s["element_name"] for s in block["buckets"]["undocumented"]["samples"]
        }
        assert undoc_names == {"firstName"}


class TestOutputFiles:
    def test_run_writes_json_and_md(self, tmp_path):
        spine_base = _write_pair(
            tmp_path,
            "TX",
            [_record("Student", "firstName")],
            [_record("Student", "firstName", documented=True)],
        )
        out = tmp_path / "data" / "out"
        divergence.run(states=("TX",), out=out, spine_base=spine_base)

        json_path = out / "lens_divergence.json"
        md_path = out / "lens_divergence.md"
        assert json_path.exists()
        assert md_path.exists()

        # JSON structure
        report = json.loads(json_path.read_text(encoding="utf-8"))
        assert report["lenses"] == ["source", "spine"]
        assert len(report["per_state"]) == 1
        assert "guidance" in report

        # Markdown has per-state heading
        md = md_path.read_text(encoding="utf-8")
        assert "# Lens-divergence report" in md
        assert "### TX" in md


_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tests" / "golden"
_SPINE_DIR = _REPO_ROOT / "data" / "spine"

_REAL_STATES = ("az", "wi", "mn", "tx")


def _load_golden(state: str, lens: str) -> list[ElementRecord]:
    path = _GOLDEN_DIR / f"{state}_elements_{lens}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [ElementRecord.model_validate(r) for r in raw["elements"]]


def _reconstruct_buckets(
    state: str, spine: StateSpine
) -> tuple[list[ElementRecord], list[ElementRecord], list[ElementRecord]]:
    """Mirror `divergence._build_state_block` on goldens to get FULL
    bucket lists (not the 20-sample truncation the JSON report carries).

    Replication is intentional: the invariants below assert properties
    of the three lists, not of the render. If divergence.py's logic
    diverges from this replica, the assertion surfaces the divergence.
    """
    src = _load_golden(state, "source")
    spn = _load_golden(state, "spine")

    def _is_filtered(entity: str) -> bool:
        return entity_filter_domain(entity, spine) is not None

    spine_missing = [r for r in src if r.source == "unknown"]

    source_match_keys: set[tuple[str, str]] = set()
    for r in src:
        if r.source in ("core", "extension"):
            source_match_keys |= record_match_keys(r.entity, r.element_name)

    alias_miss: list[ElementRecord] = []
    undocumented: list[ElementRecord] = []
    for r in spn:
        if r.documented or r.source not in ("core", "extension"):
            continue
        if _is_filtered(r.entity):
            continue
        keys = record_match_keys(r.entity, r.element_name)
        if keys & source_match_keys:
            alias_miss.append(r)
        else:
            undocumented.append(r)

    return spine_missing, undocumented, alias_miss


@pytest.mark.realdata
class TestRealDataInvariants:
    """Structural invariants against the four real state goldens.

    Skips when `data/spine/{state}_spine.json` is absent (CI without
    a fresh spine fetch). The goldens themselves are always committed,
    so the source/spine element lists are always available.
    """

    @pytest.fixture(params=_REAL_STATES)
    def state(self, request):
        return request.param

    @pytest.fixture
    def spine(self, state):
        path = _SPINE_DIR / f"{state}_spine.json"
        if not path.exists():
            pytest.skip(f"{path} not present")
        return StateSpine.model_validate_json(path.read_text(encoding="utf-8"))

    def test_buckets_pairwise_disjoint(self, state, spine):
        """No (entity, element_name) identity appears in more than one
        bucket. spine_missing rows come from source-lens; undocumented
        and alias_miss come from spine-lens — they could in principle
        collide at the same identity, so the invariant isn't a
        tautology."""
        sm, ud, am = _reconstruct_buckets(state, spine)
        sm_keys = {(r.entity, r.element_name) for r in sm}
        ud_keys = {(r.entity, r.element_name) for r in ud}
        am_keys = {(r.entity, r.element_name) for r in am}
        for a_name, a_keys, b_name, b_keys in (
            ("spine_missing", sm_keys, "undocumented", ud_keys),
            ("spine_missing", sm_keys, "alias_miss", am_keys),
            ("undocumented", ud_keys, "alias_miss", am_keys),
        ):
            overlap = a_keys & b_keys
            assert not overlap, (
                f"{state}: {len(overlap)} (entity, element_name) pair(s) "
                f"appear in both {a_name} and {b_name}: "
                f"{sorted(overlap)[:5]!r}"
            )

    def test_filtered_entities_route_to_spine_missing_or_nowhere(
        self, state, spine
    ):
        """A filtered-domain entity may appear in spine_missing when the
        state documented an unresolvable row there. It must NEVER appear
        in undocumented or alias_miss — those buckets are defined to
        exclude filtered entities (the soft-filter collapses the noise
        to placeholders)."""
        _, ud, am = _reconstruct_buckets(state, spine)
        for label, rows in (("undocumented", ud), ("alias_miss", am)):
            leaked = [
                r for r in rows
                if entity_filter_domain(r.entity, spine) is not None
            ]
            assert not leaked, (
                f"{state}: {len(leaked)} filtered-domain row(s) leaked "
                f"into the {label} bucket — soft-filter broken. "
                f"First sample: entity={leaked[0].entity!r} "
                f"element={leaked[0].element_name!r}"
            )

    def test_count_conservation(self, state, spine):
        """`len(undocumented) + len(alias_miss)` equals the number of
        undocumented non-filtered spine-lens core/extension emits (the
        full set of rows that undocumented+alias_miss partition).

        `len(spine_missing)` equals the number of source-lens rows with
        source='unknown' (the set spine_missing draws from). The
        hand-off's 'modulo placeholder and hybrid-append' phrasing
        bottoms out at these two clean identities."""
        sm, ud, am = _reconstruct_buckets(state, spine)
        spn = _load_golden(state, "spine")

        undoc_non_filtered = sum(
            1 for r in spn
            if (
                not r.documented
                and r.source in ("core", "extension")
                and entity_filter_domain(r.entity, spine) is None
            )
        )
        assert len(ud) + len(am) == undoc_non_filtered, (
            f"{state}: undocumented + alias_miss = {len(ud) + len(am)} "
            f"but undocumented non-filtered spine emits = "
            f"{undoc_non_filtered}"
        )

        src = _load_golden(state, "source")
        src_unknowns = sum(1 for r in src if r.source == "unknown")
        assert len(sm) == src_unknowns, (
            f"{state}: spine_missing = {len(sm)} but source-lens "
            f"source='unknown' count = {src_unknowns}"
        )
