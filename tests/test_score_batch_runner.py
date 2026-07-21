"""Unit tests for ``src.score.batch_runner``.

The runner is the orchestration layer between the rendering helper
(``render_batches_for``), the SDK wrapper (``BatchClient``), the
manifest writer (``batch_manifest``), and the on-disk cache.

Tests inject:
- A fixture-driven record loader (no real spine or source data needed).
- A scripted ``BatchClient`` that records what was submitted and
  returns pre-baked results without touching Anthropic.
- ``tmp_path``-rooted cache + manifest dirs so on-disk state is
  isolated between tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.models.element import ElementRecord
from src.score import batch_runner as batch_runner_module
from src.score import extract as extract_module
from src.score.batch_client import (
    BatchItemResult,
    BatchRequestSpec,
    BatchSubmitResult,
)
from src.score.batch_manifest import BatchManifest, read_manifest
from src.score.batch_runner import (
    BatchCostGateError,
    DEFAULT_CHUNK_SIZE,
    collect_batch,
    submit_run_all,
)
from src.score.cache import Cache, cache_key
from src.score.extract import (
    PROMPT_VERSION,
    _canonical_prompt,
    render_batches_for,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_RED_TEAM_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "scoring_phase_a_red_team.json"


def _load_red_team() -> list[ElementRecord]:
    raw = json.loads(_RED_TEAM_FIXTURE.read_text(encoding="utf-8"))
    return [
        ElementRecord.model_validate({k: v for k, v in r.items() if not k.startswith("_")})
        for r in raw
    ]


def _patch_load_records(monkeypatch: pytest.MonkeyPatch, records: list[ElementRecord]) -> None:
    """Replace the extract module's loader with a fixture-driven stub."""
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
            keep = [r for r in keep if r.source in set(source_filter)]
        keep.sort(key=lambda r: (r.entity, r.element_name))
        if limit is not None:
            keep = keep[:limit]
        return keep

    monkeypatch.setattr(extract_module, "load_phase_a_records", _load)
    # Issue #184: these tests drive synthetic fixture records, so neutralize
    # the on-disk spine load — the prompt-label reconstruction falls back to
    # the record's own `domain`, keeping the cache-population path (which
    # also renders without a spine) byte-consistent with submit_run_all.
    monkeypatch.setattr(batch_runner_module, "_load_spine_for", lambda state: None)


@dataclass
class _ScriptedBatchClient:
    """Stand-in for ``BatchClient`` — records calls, returns pre-baked.

    ``submit_responses`` is FIFO-consumed (one per submit call).
    ``results_by_batch_id`` is a dict mapping batch_id → list of
    ``BatchItemResult`` to yield from ``iter_results``.
    """

    model: str = "claude-sonnet-4-6"
    submit_calls: list[list[BatchRequestSpec]] = None  # type: ignore[assignment]
    submit_responses: list[BatchSubmitResult] = None  # type: ignore[assignment]
    results_by_batch_id: dict[str, list[BatchItemResult]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.submit_calls = []
        if self.submit_responses is None:
            self.submit_responses = []
        if self.results_by_batch_id is None:
            self.results_by_batch_id = {}

    def submit(self, requests):
        spec_list = list(requests)
        self.submit_calls.append(spec_list)
        if not self.submit_responses:
            raise AssertionError("ScriptedBatchClient: ran out of submit responses")
        return self.submit_responses.pop(0)

    def status(self, batch_id):
        raise NotImplementedError("not used in these tests")

    def iter_results(self, batch_id):
        return iter(self.results_by_batch_id.get(batch_id, []))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# submit_run_all
# ---------------------------------------------------------------------------


class TestSubmitRunAll:
    def test_returns_empty_when_all_cache_hits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = _load_red_team()[:2]
        _patch_load_records(monkeypatch, records)

        # Pre-populate cache with every prompt the renderer would produce.
        cache = Cache("claude-sonnet-4-6", PROMPT_VERSION, root=tmp_path / "cache")
        batches = render_batches_for(
            fact="has_conditional_logic", state="AZ", lens="spine"
        )
        for entity, group_records, system_text, user_text in batches:
            canonical = _canonical_prompt(system_text, user_text)
            key = cache_key(canonical, "claude-sonnet-4-6", PROMPT_VERSION)
            cache.put(
                key, [{"placeholder": True}],
                tokens_in=10, tokens_out=2, usd=0.0, cache_read_tokens=0,
            )

        client = _ScriptedBatchClient()
        manifests = submit_run_all(
            states=["AZ"], lens="spine", facts=["has_conditional_logic"],
            cache_root=tmp_path / "cache",
            manifest_root=tmp_path / "batches",
            client=client,
        )
        assert manifests == []
        assert client.submit_calls == []

    def test_custom_id_equals_cache_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per the plan: custom_id is the SHA-256 cache key.

        This is the load-bearing invariant — break it and collect can
        no longer route batch results back to cache entries without an
        auxiliary lookup table.
        """
        records = _load_red_team()[:2]
        _patch_load_records(monkeypatch, records)

        client = _ScriptedBatchClient(
            submit_responses=[
                BatchSubmitResult(batch_id="batch_test", request_count=99, submitted_at=_now_iso())
            ],
        )
        submit_run_all(
            states=["AZ"], lens="spine", facts=["has_conditional_logic"],
            cache_root=tmp_path / "cache",
            manifest_root=tmp_path / "batches",
            client=client,
        )
        assert client.submit_calls, "submit was not called"
        for spec in client.submit_calls[0]:
            recomputed = cache_key(
                _canonical_prompt(spec.system_text, spec.user_text),
                "claude-sonnet-4-6", PROMPT_VERSION,
            )
            assert spec.custom_id == recomputed

    def test_writes_manifest_to_disk_with_items(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = _load_red_team()[:2]
        _patch_load_records(monkeypatch, records)

        client = _ScriptedBatchClient(
            submit_responses=[
                BatchSubmitResult(batch_id="batch_disk", request_count=99, submitted_at=_now_iso())
            ],
        )
        manifests = submit_run_all(
            states=["AZ"], lens="spine", facts=["has_conditional_logic"],
            cache_root=tmp_path / "cache",
            manifest_root=tmp_path / "batches",
            client=client,
        )
        assert len(manifests) == 1
        on_disk = read_manifest("batch_disk", root=tmp_path / "batches")
        assert on_disk.batch_id == "batch_disk"
        assert on_disk.model == "claude-sonnet-4-6"
        assert on_disk.prompt_version == PROMPT_VERSION
        assert on_disk.items, "manifest should record at least one item"
        assert all(it.lens == "spine" for it in on_disk.items)
        assert all(it.fact == "has_conditional_logic" for it in on_disk.items)

    def test_chunks_at_chunk_size(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = _load_red_team()
        _patch_load_records(monkeypatch, records)

        # Pre-bake one response per possible entity batch — the
        # red-team fixture has ~10–20 distinct entities and chunk_size=1
        # forces one submit per entity.
        client = _ScriptedBatchClient(
            submit_responses=[
                BatchSubmitResult(batch_id=f"batch_{i}", request_count=99, submitted_at=_now_iso())
                for i in range(50)
            ],
        )
        manifests = submit_run_all(
            states=["AZ"], lens="spine", facts=["has_conditional_logic"],
            cache_root=tmp_path / "cache",
            manifest_root=tmp_path / "batches",
            client=client,
            chunk_size=1,  # force one batch per item
        )
        assert len(manifests) >= 2  # red-team fixture has multiple entities
        assert len(client.submit_calls) == len(manifests)
        for call in client.submit_calls:
            assert len(call) == 1

    def test_default_chunk_size_is_anthropic_ceiling(self) -> None:
        """Sanity: the default chunk matches Anthropic's published 10k limit."""
        assert DEFAULT_CHUNK_SIZE == 10_000

    def test_cost_gate_raises_without_confirm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = _load_red_team()
        _patch_load_records(monkeypatch, records)

        client = _ScriptedBatchClient()
        with pytest.raises(BatchCostGateError) as exc:
            submit_run_all(
                states=["AZ"], lens="spine", facts=["has_conditional_logic"],
                cache_root=tmp_path / "cache",
                manifest_root=tmp_path / "batches",
                client=client,
                max_cost_usd=0.0,  # any cost trips the gate
                confirm=False,
            )
        # Operator-friendly: error mentions both the estimate and the gate.
        assert "exceeds" in str(exc.value)
        assert "--yes" in str(exc.value) or "confirm" in str(exc.value).lower()
        assert client.submit_calls == []

    def test_cost_gate_passes_with_confirm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = _load_red_team()[:2]
        _patch_load_records(monkeypatch, records)

        client = _ScriptedBatchClient(
            submit_responses=[
                BatchSubmitResult(batch_id="batch_confirmed", request_count=99, submitted_at=_now_iso())
            ],
        )
        manifests = submit_run_all(
            states=["AZ"], lens="spine", facts=["has_conditional_logic"],
            cache_root=tmp_path / "cache",
            manifest_root=tmp_path / "batches",
            client=client,
            max_cost_usd=0.0,
            confirm=True,
        )
        assert len(manifests) == 1
        assert client.submit_calls

    def test_skips_deterministic_facts_in_roster(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``phase_b_facts_for_lens`` includes deterministic + LLM facts.

        Only LLM facts produce LLM batches. Passing the full roster
        explicitly must not blow up on deterministic ones — they're
        filtered out silently.
        """
        records = _load_red_team()[:2]
        _patch_load_records(monkeypatch, records)

        client = _ScriptedBatchClient(
            submit_responses=[
                BatchSubmitResult(batch_id="batch_filtered", request_count=99, submitted_at=_now_iso())
            ],
        )
        manifests = submit_run_all(
            states=["AZ"], lens="spine",
            facts=["definition_present", "has_conditional_logic"],
            cache_root=tmp_path / "cache",
            manifest_root=tmp_path / "batches",
            client=client,
        )
        # definition_present is deterministic (no LLM), only
        # has_conditional_logic produces submitted items.
        assert len(manifests) == 1
        for it in manifests[0].items:
            assert it.fact == "has_conditional_logic"


# ---------------------------------------------------------------------------
# collect_batch
# ---------------------------------------------------------------------------


class TestCollectBatch:
    def _seed_manifest(
        self,
        tmp_path: Path,
        *,
        batch_id: str = "batch_collect",
        prompt_version: str = PROMPT_VERSION,
        model: str = "claude-sonnet-4-6",
    ) -> BatchManifest:
        from src.score.batch_manifest import BatchItem, write_manifest
        manifest = BatchManifest(
            batch_id=batch_id,
            submitted_at=_now_iso(),
            model=model,
            prompt_version=prompt_version,
            estimated_cost_usd=1.23,
            items=[
                BatchItem(
                    custom_id="aa" * 32,
                    state="AZ", lens="spine", fact="has_conditional_logic",
                    entity="StudentAssessment", record_count=5,
                    estimated_tokens_in=400,
                ),
                BatchItem(
                    custom_id="bb" * 32,
                    state="AZ", lens="spine", fact="has_conditional_logic",
                    entity="Course", record_count=3,
                    estimated_tokens_in=300,
                ),
            ],
        )
        write_manifest(manifest, root=tmp_path / "batches")
        return manifest

    def test_writes_cache_for_succeeded_only(self, tmp_path: Path) -> None:
        self._seed_manifest(tmp_path)
        results = [
            BatchItemResult(
                custom_id="aa" * 32, status="succeeded",
                payload=[{"element_name": "id", "has_conditional_logic": False, "spans": [], "confidence": "high"}],
                raw_text="ok", tokens_in=100, tokens_out=20, error=None,
            ),
            BatchItemResult(
                custom_id="bb" * 32, status="errored",
                payload=None, raw_text="", tokens_in=0, tokens_out=0,
                error="rate_limited",
            ),
        ]
        client = _ScriptedBatchClient(
            results_by_batch_id={"batch_collect": results},
        )

        summary = collect_batch(
            "batch_collect",
            cache_root=tmp_path / "cache",
            manifest_root=tmp_path / "batches",
            client=client,
        )
        assert summary.succeeded_count == 1
        assert summary.errored_count == 1
        assert summary.cache_writes == 1
        assert summary.actual_cost_usd > 0  # 100 in + 20 out at batch tier

        # The succeeded payload landed in cache — succeeded only.
        cache = Cache("claude-sonnet-4-6", PROMPT_VERSION, root=tmp_path / "cache")
        assert cache.get("aa" * 32) is not None
        assert cache.get("bb" * 32) is None

    def test_pricing_tier_marked_batch(self, tmp_path: Path) -> None:
        self._seed_manifest(tmp_path)
        results = [
            BatchItemResult(
                custom_id="aa" * 32, status="succeeded",
                payload=[{"placeholder": True}],
                raw_text="ok", tokens_in=200, tokens_out=50, error=None,
            ),
        ]
        client = _ScriptedBatchClient(
            results_by_batch_id={"batch_collect": results},
        )
        collect_batch(
            "batch_collect",
            cache_root=tmp_path / "cache",
            manifest_root=tmp_path / "batches",
            client=client,
        )
        cache = Cache("claude-sonnet-4-6", PROMPT_VERSION, root=tmp_path / "cache")
        entry = cache.get("aa" * 32)
        assert entry is not None
        assert entry.get("pricing_tier") == "batch"

    def test_refuses_collect_on_prompt_version_mismatch(self, tmp_path: Path) -> None:
        self._seed_manifest(tmp_path, prompt_version="OLD-VERSION")
        client = _ScriptedBatchClient(results_by_batch_id={"batch_collect": []})
        with pytest.raises(ValueError, match="prompt_version mismatch"):
            collect_batch(
                "batch_collect",
                cache_root=tmp_path / "cache",
                manifest_root=tmp_path / "batches",
                client=client,
            )

    def test_refuses_collect_on_model_mismatch(self, tmp_path: Path) -> None:
        self._seed_manifest(tmp_path, model="claude-old-model")
        client = _ScriptedBatchClient(results_by_batch_id={"batch_collect": []})
        with pytest.raises(ValueError, match="model mismatch"):
            collect_batch(
                "batch_collect",
                cache_root=tmp_path / "cache",
                manifest_root=tmp_path / "batches",
                client=client,
                model="claude-sonnet-4-6",
            )

    def test_skips_already_cached_keys(self, tmp_path: Path) -> None:
        """A sync run between submit and collect could pre-populate cache."""
        self._seed_manifest(tmp_path)
        cache = Cache("claude-sonnet-4-6", PROMPT_VERSION, root=tmp_path / "cache")
        cache.put(
            "aa" * 32, [{"already": "there"}],
            tokens_in=1, tokens_out=1, usd=0.0, cache_read_tokens=0,
        )

        results = [
            BatchItemResult(
                custom_id="aa" * 32, status="succeeded",
                payload=[{"different": "payload"}],
                raw_text="ok", tokens_in=200, tokens_out=50, error=None,
            ),
        ]
        client = _ScriptedBatchClient(
            results_by_batch_id={"batch_collect": results},
        )
        summary = collect_batch(
            "batch_collect",
            cache_root=tmp_path / "cache",
            manifest_root=tmp_path / "batches",
            client=client,
        )
        # Succeeded was counted but cache_writes stayed 0 (already cached).
        assert summary.succeeded_count == 1
        assert summary.cache_writes == 0
        # First-write-wins: original payload preserved.
        entry = cache.get("aa" * 32)
        assert entry is not None
        assert entry["response"] == [{"already": "there"}]

    def test_counts_every_failure_status(self, tmp_path: Path) -> None:
        self._seed_manifest(tmp_path)
        results = [
            BatchItemResult(
                custom_id=f"{i:064x}", status=status,
                payload=None, raw_text="", tokens_in=0, tokens_out=0,
                error=None,
            )
            for i, status in enumerate(["parse_failed", "errored", "canceled", "expired"])
        ]
        client = _ScriptedBatchClient(
            results_by_batch_id={"batch_collect": results},
        )
        summary = collect_batch(
            "batch_collect",
            cache_root=tmp_path / "cache",
            manifest_root=tmp_path / "batches",
            client=client,
        )
        assert summary.succeeded_count == 0
        assert summary.parse_failed_count == 1
        assert summary.errored_count == 1
        assert summary.canceled_count == 1
        assert summary.expired_count == 1
        assert summary.cache_writes == 0
        assert summary.actual_cost_usd == 0.0
