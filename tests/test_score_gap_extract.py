"""Gap-row LLM extract — issue #73 Step 2 / Step 3.

Covers the pure-Python pieces of the extract pipeline:

- ``select_sample`` is deterministic (seed-stable + proportional).
- ``write_sample_elements_artifact`` writes a valid StateElements file
  that ``extract.run()`` will accept.
- ``downgrade_summary`` aggregates per-fact downgrade rates from
  on-disk gap-extract artifacts.
- ``aggregate_gap.run(llm_artifact_dir=…)`` folds Step 2/3 LLM values
  into the gap sidecar (real LLM values override the
  missing-from-artifact stub).
- CLI wiring regression (module-level run is a plain function).

Live LLM extract is not exercised here — that's the production run
behind a cost cap.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.models.edfi_catalog import (
    EdFiCatalog,
    EntityEntry,
    PropertyInfo,
)
from src.models.element import StateElements
from src.models.spine import SpineSourceURLs, StateSpine
from src.score.aggregate_gap import (
    run as run_gap_aggregate,
)
from src.score.gap_extract import (
    DEFAULT_SAMPLE_PCT,
    DEFAULT_SEED,
    SPINE_LLM_FACTS_FOR_GAP,
    downgrade_summary,
    select_sample,
    write_sample_elements_artifact,
)


def _spine() -> StateSpine:
    catalog = EdFiCatalog(
        version="4.0",
        entity_count=1,
        extension_count=0,
        entities={
            "Calendar": EntityEntry(properties={"calendarCode": PropertyInfo()})
        },
        extensions={},
    )
    return StateSpine(
        state="TX",
        edfi_version="4.0",
        fetched_at=datetime.now(tz=timezone.utc),
        source_urls=SpineSourceURLs(resources="http://test/r.json"),
        catalog=catalog,
    )


def _write_gap_artifact(path: Path, n: int) -> None:
    """Write a deterministic gap artifact with ``n`` rows."""
    gaps = [
        {
            "state": "TX",
            "entity": f"Entity{i:03d}",
            "element_name": f"prop{i:03d}",
            "discovery": "spine_within_documented_entity",
            "documented_in_source": False,
            "spine_data_type": "String",
            "spine_extension_name": None,
            "rationale": "x",
        }
        for i in range(n)
    ]
    payload = {
        "state": "TX",
        "generated_at": "2026-04-29T00:00:00Z",
        "gap_count": n,
        "discovery_counts": {"spine_within_documented_entity": n},
        "gaps": gaps,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Sample selection
# ---------------------------------------------------------------------------


def test_sample_count_matches_pct(tmp_path: Path) -> None:
    """10 % of 100 → 10 (math.ceil ensures rounding-up so a 1-row gap
    artifact still emits a 1-row sample)."""
    gap_path = tmp_path / "tx_elements_gap.json"
    _write_gap_artifact(gap_path, 100)
    sample = select_sample("TX", sample_pct=0.10, gap_path=gap_path)
    assert len(sample) == 10


def test_sample_is_deterministic(tmp_path: Path) -> None:
    """Same (state, seed, gap_path) returns identical row ordering twice."""
    gap_path = tmp_path / "tx_elements_gap.json"
    _write_gap_artifact(gap_path, 50)
    a = select_sample("TX", sample_pct=0.20, seed=73, gap_path=gap_path)
    b = select_sample("TX", sample_pct=0.20, seed=73, gap_path=gap_path)
    assert [r["element_name"] for r in a] == [r["element_name"] for r in b]


def test_sample_is_deterministic_across_processes(tmp_path: Path) -> None:
    """The selection is pinned to exact rows — cross-PROCESS determinism.

    Issue #211 item 5b: the state namespace used to come from builtin
    ``hash(state)``, which is salted per process (PYTHONHASHSEED), so
    the "reproducible 10% stratified sample" silently changed on every
    invocation — breaking cache-resume (re-spending $) and cross-run
    comparisons. The namespace is now SHA-256-derived; these indices
    were computed once and must never change for (TX, seed=73, n=50,
    pct=0.20). If this test fails, the sampling function's determinism
    contract broke — do NOT just re-pin the numbers.
    """
    gap_path = tmp_path / "tx_elements_gap.json"
    _write_gap_artifact(gap_path, 50)
    sample = select_sample("TX", sample_pct=0.20, seed=73, gap_path=gap_path)
    assert [r["element_name"] for r in sample] == [
        f"prop{i:03d}" for i in (10, 11, 16, 18, 20, 26, 28, 38, 41, 43)
    ]


def test_sample_pct_one_returns_all(tmp_path: Path) -> None:
    """Step 3 path — sample_pct=1.0 returns every row in surfacer order."""
    gap_path = tmp_path / "tx_elements_gap.json"
    _write_gap_artifact(gap_path, 25)
    sample = select_sample("TX", sample_pct=1.0, gap_path=gap_path)
    assert len(sample) == 25


def test_sample_zero_returns_empty(tmp_path: Path) -> None:
    gap_path = tmp_path / "tx_elements_gap.json"
    _write_gap_artifact(gap_path, 25)
    assert select_sample("TX", sample_pct=0.0, gap_path=gap_path) == []


def test_sample_uses_default_pct_and_seed_constants(tmp_path: Path) -> None:
    """Sanity: the public constants are wired through to keyword defaults."""
    assert DEFAULT_SAMPLE_PCT == 0.10
    assert DEFAULT_SEED == 73


# ---------------------------------------------------------------------------
# Synthesized elements artifact
# ---------------------------------------------------------------------------


def test_write_sample_elements_artifact_roundtrips(tmp_path: Path) -> None:
    gap_path = tmp_path / "tx_elements_gap.json"
    _write_gap_artifact(gap_path, 5)
    sample = select_sample("TX", sample_pct=1.0, gap_path=gap_path)

    out_path = tmp_path / "tx_elements_gap_extract.json"
    write_sample_elements_artifact("TX", sample, out_path=out_path)

    payload = StateElements.model_validate_json(out_path.read_text())
    assert payload.element_count == 5
    # Records carry documented=True so load_phase_a_records keeps them.
    for r in payload.elements:
        assert r.documented is True
        assert r.source in ("core", "extension")
        assert r.definition_text == ""


# ---------------------------------------------------------------------------
# Downgrade summary
# ---------------------------------------------------------------------------


def _write_artifact(
    path: Path, *, scored: int, downgrades: int, fact: str = "definition_is_implementable"
) -> None:
    """Write a minimal phase-a artifact with the telemetry the summary reads."""
    header = {
        "__type": "header",
        "state": "TX",
        "lens": "spine",
        "fact": fact,
        "scored_count": scored,
        "downgrade_count": downgrades,
        "record_count": scored,
        "total_usd": 0.42,
        "model": "claude-sonnet-4-6",
        "prompt_version": "phase-a.v1",
        "mode": "api",
        "status": "complete",
    }
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")


def test_downgrade_summary_flags_high_rate(tmp_path: Path) -> None:
    """One fact at 50 % downgrade trips the halt-recommended flag."""
    _write_artifact(
        tmp_path / "TX_spine_definition_is_implementable.jsonl",
        scored=20, downgrades=10, fact="definition_is_implementable",
    )
    _write_artifact(
        tmp_path / "TX_spine_has_aggregation.jsonl",
        scored=20, downgrades=2, fact="has_aggregation",
    )
    summary = downgrade_summary(
        states=["TX"],
        facts=("definition_is_implementable", "has_aggregation"),
        artifact_dir=tmp_path,
    )
    assert summary["halt_recommended"] is True
    assert summary["per_fact"]["definition_is_implementable"]["rate"] == 0.5
    assert summary["per_fact"]["has_aggregation"]["rate"] == 0.1


def test_downgrade_summary_clean_pass(tmp_path: Path) -> None:
    """All facts at 10 % downgrade → no halt."""
    for fact in ("definition_is_implementable", "has_aggregation"):
        _write_artifact(
            tmp_path / f"TX_spine_{fact}.jsonl",
            scored=100, downgrades=10, fact=fact,
        )
    summary = downgrade_summary(
        states=["TX"],
        facts=("definition_is_implementable", "has_aggregation"),
        artifact_dir=tmp_path,
    )
    assert summary["halt_recommended"] is False
    assert summary["overall_rate"] == 0.1


def test_downgrade_summary_handles_missing_artifacts(tmp_path: Path) -> None:
    """No artifacts on disk → empty states_loaded + zero counts."""
    summary = downgrade_summary(
        states=["TX"],
        facts=("definition_is_implementable",),
        artifact_dir=tmp_path,
    )
    assert summary["states_loaded"] == []
    assert summary["total_scored"] == 0
    assert summary["halt_recommended"] is False


# ---------------------------------------------------------------------------
# aggregate_gap reads LLM artifacts when llm_artifact_dir is set
# ---------------------------------------------------------------------------


def _write_llm_artifact_for_record(
    path: Path,
    *,
    fact: str,
    record_key: str,
    entity: str,
    element_name: str,
    value,
    confidence: str = "high",
) -> None:
    """Write a single-row LLM-shape artifact the rule cascade can consume."""
    header = {
        "__type": "header",
        "state": record_key.split("|")[0],
        "lens": "spine",
        "fact": fact,
        "scored_count": 1,
        "downgrade_count": 0,
        "record_count": 1,
        "total_usd": 0.0,
        "model": "claude-sonnet-4-6",
        "prompt_version": "phase-a.v1",
        "mode": "api",
        "status": "complete",
    }
    row = {
        "record_key": record_key,
        "entity": entity,
        "element_name": element_name,
        "llm_value": value,
        "validated_value": value,
        "spans": [],
        "confidence": confidence,
        "downgrade_reason": None,
        "any_invalid_spans": False,
        "model": "claude-sonnet-4-6",
        "prompt_version": "phase-a.v1",
    }
    path.write_text(
        json.dumps(header) + "\n" + json.dumps(row) + "\n",
        encoding="utf-8",
    )


def test_aggregate_gap_picks_up_llm_artifacts(tmp_path: Path) -> None:
    """When ``llm_artifact_dir`` points at populated artifacts, the rule
    cascade folds those values in. ``definition_is_implementable=True``
    on a Calendar gap row should drop ``hallucinated_input:
    definition_is_implementable`` from the review reasons."""
    spine = _spine()
    spine_path = tmp_path / "tx_spine.json"
    spine_path.write_text(spine.model_dump_json())
    gap_path = tmp_path / "tx_elements_gap.json"
    _write_gap_artifact(gap_path, 1)
    # Override the entity name on the single gap row to match Calendar so
    # the synthesized record's record_key matches the LLM artifact below.
    payload = json.loads(gap_path.read_text())
    payload["gaps"][0]["entity"] = "Calendar"
    payload["gaps"][0]["element_name"] = "calendarCode"
    gap_path.write_text(json.dumps(payload))

    llm_dir = tmp_path / "phase_a_gap"
    llm_dir.mkdir()
    _write_llm_artifact_for_record(
        llm_dir / "TX_spine_definition_is_implementable.jsonl",
        fact="definition_is_implementable",
        record_key="TX|Calendar|calendarCode",
        entity="Calendar",
        element_name="calendarCode",
        value=True,
    )

    out_path = tmp_path / "tx_scores_gap.json"
    run_gap_aggregate(
        state="TX",
        gap_path=gap_path,
        spine_path=spine_path,
        out_path=out_path,
        llm_artifact_dir=llm_dir,
    )
    record = json.loads(out_path.read_text())["scores"][0]

    fp = record["fact_provenance"]["definition_is_implementable"]
    assert fp["value"] is True
    assert fp["downgraded"] is False
    assert fp["confidence"] == "high"

    # The other 11 LLM facts stay missing-from-artifact, but
    # definition_is_implementable should NOT appear in the review reasons.
    reasons = record["review"]["reasons"]
    assert "hallucinated_input:definition_is_implementable" not in reasons


def test_aggregate_gap_default_no_llm_pool(tmp_path: Path) -> None:
    """Without ``llm_artifact_dir``, aggregate stays Step-1 deterministic.

    Even if a real `data/out/scoring/phase_a_gap/` directory exists on
    the host machine (post-Step-2 run), the default behaviour reads
    nothing — preserves test hermeticity.
    """
    spine = _spine()
    spine_path = tmp_path / "tx_spine.json"
    spine_path.write_text(spine.model_dump_json())
    gap_path = tmp_path / "tx_elements_gap.json"
    _write_gap_artifact(gap_path, 1)
    out_path = tmp_path / "tx_scores_gap.json"

    run_gap_aggregate(
        state="TX",
        gap_path=gap_path,
        spine_path=spine_path,
        out_path=out_path,
        # llm_artifact_dir omitted on purpose
    )
    record = json.loads(out_path.read_text())["scores"][0]
    fp = record["fact_provenance"]["definition_is_implementable"]
    assert fp["value"] is None
    assert fp["downgraded"] is True


# ---------------------------------------------------------------------------
# CLI wiring contract
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_run_is_plain_function(self) -> None:
        from src.score import gap_extract

        assert not hasattr(gap_extract.run, "callback"), (
            "gap_extract.run must be a plain function — Click wrapper "
            "lives in cli.py per CLAUDE.md."
        )

    def test_spine_llm_facts_for_gap_count_matches_benchmark(self) -> None:
        """The 12-fact set is the cost-benchmark anchor. Adding/removing
        a fact would invalidate the issue's $0.0149/record number; this
        test makes that explicit so a future change re-triggers a cost
        review."""
        assert len(SPINE_LLM_FACTS_FOR_GAP) == 12
