"""Phase B checkpoint hardening — streaming artifact + multi-fact runner.

Covers the §5 success criteria from `docs/next-session-scoring-phase-b.md`:

- `test_streaming_artifact_survives_abort`: mid-run crash leaves a
  partial artifact with N completed rows and `status="aborted"`.
- Skip-if-complete: a (state, fact) pair whose artifact header already
  says `status="complete"` with `scored_count == record_count` is not
  rerun by the runner.
- Global cost-cap: remaining = cap - spent; skips exhausted pairs
  with `status="skipped_cost_cap"` and does not call the extractor.
- Manifest streaming: crash after pair N leaves manifest reflecting
  N-1 pairs (§5b crash-safety contract).
- Deterministic ordering: states outer (AZ,WI,MN,TX), facts inner
  (PHASE_B_FACTS).
- CLI: `mc score run-all` wiring rejects unknown facts, honors
  `--state AZ --facts has_conditional_logic`.

No live LLM calls anywhere — `extract.run()` is either monkeypatched or
driven by a ScriptedClient from the Phase A fixtures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from src.models.element import ElementRecord
from src.score import extract as extract_module
from src.score import runner as runner_module
from src.score.client import LLMResponse
from src.score.extract import run as run_extract
from src.score.deterministic import SOURCE_DETERMINISTIC_FACTS
from src.score.runner import (
    RUN_MANIFEST_NAME,
    PairResult,
    RunManifest,
    is_pair_complete,
    phase_b_facts_for_lens,
    read_artifact_header,
    run_all,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@dataclass
class ScriptedClient:
    responses: list[Any]
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    calls: int = 0

    def call(self, *, system_text: str, user_text: str, cache_system: bool = True) -> LLMResponse:
        if not self.responses:
            raise AssertionError("ScriptedClient exhausted")
        next_resp = self.responses.pop(0)
        self.calls += 1
        if isinstance(next_resp, Exception):
            raise next_resp
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


def _mk_record(*, entity: str, element_name: str) -> ElementRecord:
    return ElementRecord.model_validate({
        "state": "AZ",
        "edfi_version": "3",
        "domain": "Test",
        "entity": entity,
        "element_name": element_name,
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
    })


def _patch_load_records(monkeypatch: pytest.MonkeyPatch, records: list[ElementRecord]) -> None:
    def _load(
        state: str,
        lens: str,
        *,
        elements_path: Path | None = None,
        limit: int | None = None,
        source_filter: tuple[str, ...] | None = None,
    ) -> list[ElementRecord]:
        kept = list(records)
        if source_filter is not None:
            allowed = set(source_filter)
            kept = [r for r in kept if r.source in allowed]
        keep = sorted(kept, key=lambda r: (r.entity, r.element_name))
        if limit is not None:
            keep = keep[:limit]
        return keep
    monkeypatch.setattr(extract_module, "load_phase_a_records", _load)


def _ok_payload_for(rec: ElementRecord) -> dict[str, Any]:
    return {
        "element_name": rec.element_name,
        "has_conditional_logic": False,
        "spans": [],
        "confidence": "high",
    }


# ---------------------------------------------------------------------------
# Streaming artifact — the §5a contract
# ---------------------------------------------------------------------------


class TestStreamingArtifact:
    def test_completed_run_writes_status_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = [
            _mk_record(entity="Student", element_name="first"),
            _mk_record(entity="Staff", element_name="second"),
        ]
        _patch_load_records(monkeypatch, records)
        # Extractor sorts batches by entity name: Staff < Student.
        client = ScriptedClient(
            responses=[
                [_ok_payload_for(records[1])],  # Staff
                [_ok_payload_for(records[0])],  # Student
            ]
        )

        header = run_extract(
            state="AZ",
            lens="spine",
            limit=2,
            out_dir=tmp_path,
            cache_root=tmp_path / "cache",
            client=client,
        )
        assert header["status"] == "complete"
        assert header["scored_count"] == 2
        assert header["record_count"] == 2

        artifact = tmp_path / "AZ_spine_has_conditional_logic.jsonl"
        lines = artifact.read_text(encoding="utf-8").splitlines()
        on_disk_header = json.loads(lines[0])
        assert on_disk_header["status"] == "complete"
        assert on_disk_header["scored_count"] == 2

    def test_aborted_run_preserves_rows_with_status_aborted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SIGKILL-equivalent (raised exception) mid-run must leave
        the artifact populated with all successfully-processed rows
        plus an `status="aborted"` header. This is the §5a crash-safety
        contract the runner relies on."""
        records = [
            _mk_record(entity="EntityA", element_name="a1"),
            _mk_record(entity="EntityB", element_name="b1"),
            _mk_record(entity="EntityC", element_name="c1"),
        ]
        _patch_load_records(monkeypatch, records)

        class _Boom(RuntimeError):
            pass

        client = ScriptedClient(
            responses=[
                [_ok_payload_for(records[0])],  # EntityA
                [_ok_payload_for(records[1])],  # EntityB
                _Boom("simulated network death"),
            ]
        )

        with pytest.raises(_Boom):
            run_extract(
                state="AZ",
                lens="spine",
                limit=3,
                out_dir=tmp_path,
                cache_root=tmp_path / "cache",
                client=client,
            )

        artifact = tmp_path / "AZ_spine_has_conditional_logic.jsonl"
        assert artifact.exists()
        lines = [json.loads(ln) for ln in artifact.read_text(encoding="utf-8").splitlines()]
        disk_header = lines[0]
        assert disk_header["status"] == "aborted"
        # Two batches completed before the crash.
        assert disk_header["scored_count"] == 2
        assert disk_header["record_count"] == 3
        # Header is line 1, 2 rows followed. Total 3 lines.
        assert len(lines) == 3
        assert {r["element_name"] for r in lines[1:]} == {"a1", "b1"}

    def test_provisional_header_written_after_first_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After the first batch completes but before the run ends,
        the on-disk artifact already has a header. Important so a
        SIGKILL even very early leaves something readable on disk."""
        records = [
            _mk_record(entity="Only", element_name="x"),
            _mk_record(entity="Second", element_name="y"),
        ]
        _patch_load_records(monkeypatch, records)

        artifact = tmp_path / "AZ_spine_has_conditional_logic.jsonl"

        # Custom client that inspects the filesystem between batches.
        file_states: list[int] = []

        class InspectingClient:
            model = "claude-sonnet-4-6"
            max_tokens = 4096

            def __init__(self, responses: list[Any]) -> None:
                self.responses = responses

            def call(self, *, system_text: str, user_text: str, cache_system: bool = True) -> LLMResponse:
                # Record the artifact size as we see it going in to each batch call.
                file_states.append(artifact.stat().st_size if artifact.exists() else 0)
                payload = self.responses.pop(0)
                return LLMResponse(
                    payload=payload,
                    raw_text=json.dumps(payload),
                    tokens_in=100,
                    tokens_out=30,
                    cache_read_tokens=0,
                    cache_creation_tokens=0,
                    usd=0.001,
                    model=self.model,
                )

        # sorted(["Only","Second"]) = ["Only","Second"] — Only is first.
        client = InspectingClient(
            responses=[
                [_ok_payload_for(records[0])],  # Only
                [_ok_payload_for(records[1])],  # Second
            ]
        )

        run_extract(
            state="AZ",
            lens="spine",
            limit=2,
            out_dir=tmp_path,
            cache_root=tmp_path / "cache",
            client=client,
        )
        # Before first batch: artifact absent (size 0).
        assert file_states[0] == 0
        # Before second batch: artifact non-empty (streamed after batch 1).
        assert file_states[1] > 0


# ---------------------------------------------------------------------------
# read_artifact_header + is_pair_complete
# ---------------------------------------------------------------------------


class TestPairCompleteness:
    def test_missing_artifact_not_complete(self, tmp_path: Path) -> None:
        assert is_pair_complete(tmp_path / "nonexistent.jsonl") is False
        assert read_artifact_header(tmp_path / "nonexistent.jsonl") is None

    def test_running_status_not_complete(self, tmp_path: Path) -> None:
        p = tmp_path / "AZ_x.jsonl"
        p.write_text(
            json.dumps({
                "__type": "header", "status": "running",
                "record_count": 10, "scored_count": 7,
            }) + "\n",
            encoding="utf-8",
        )
        assert is_pair_complete(p) is False

    def test_aborted_status_not_complete(self, tmp_path: Path) -> None:
        p = tmp_path / "AZ_x.jsonl"
        p.write_text(
            json.dumps({
                "__type": "header", "status": "aborted",
                "record_count": 10, "scored_count": 7,
            }) + "\n",
            encoding="utf-8",
        )
        assert is_pair_complete(p) is False

    def test_complete_status_and_counts_match(self, tmp_path: Path) -> None:
        p = tmp_path / "AZ_x.jsonl"
        p.write_text(
            json.dumps({
                "__type": "header", "status": "complete",
                "record_count": 10, "scored_count": 10,
            }) + "\n",
            encoding="utf-8",
        )
        assert is_pair_complete(p) is True

    def test_complete_status_mismatched_counts_not_complete(self, tmp_path: Path) -> None:
        p = tmp_path / "AZ_x.jsonl"
        p.write_text(
            json.dumps({
                "__type": "header", "status": "complete",
                "record_count": 10, "scored_count": 8,
            }) + "\n",
            encoding="utf-8",
        )
        assert is_pair_complete(p) is False

    def test_malformed_header_not_complete(self, tmp_path: Path) -> None:
        p = tmp_path / "AZ_x.jsonl"
        p.write_text("{not valid json\n", encoding="utf-8")
        assert is_pair_complete(p) is False
        assert read_artifact_header(p) is None

    def test_empty_file_not_complete(self, tmp_path: Path) -> None:
        p = tmp_path / "AZ_x.jsonl"
        p.write_text("", encoding="utf-8")
        assert is_pair_complete(p) is False

    # -- issue #212 item 7: the header alone is not enough -------------

    def _llm_header(self, tmp_path: Path, **overrides) -> Path:
        header = {
            "__type": "header", "status": "complete",
            "record_count": 10, "scored_count": 10,
            "lens": "spine", "model": "claude-sonnet-4-6",
            "prompt_version": "phase-a.v1",
        }
        header.update(overrides)
        p = tmp_path / "AZ_x.jsonl"
        p.write_text(json.dumps(header) + "\n", encoding="utf-8")
        return p

    def test_lens_mismatch_not_complete(self, tmp_path: Path) -> None:
        p = self._llm_header(tmp_path)
        assert is_pair_complete(p, lens="source") is False
        assert is_pair_complete(p, lens="spine") is True

    def test_model_mismatch_not_complete(self, tmp_path: Path) -> None:
        p = self._llm_header(tmp_path)
        assert is_pair_complete(p, model="claude-opus-4-7") is False
        assert is_pair_complete(p, model="claude-sonnet-4-6") is True

    def test_prompt_version_mismatch_not_complete(self, tmp_path: Path) -> None:
        p = self._llm_header(tmp_path)
        assert is_pair_complete(p, prompt_version="phase-a.v2") is False
        assert is_pair_complete(p, prompt_version="phase-a.v1") is True

    def test_deterministic_artifact_ignores_model_and_version(
        self, tmp_path: Path
    ) -> None:
        """Deterministic facts stamp model="deterministic" + their own
        det version; comparing those against the requested LLM
        model/prompt_version would re-run (and re-dirty) every det
        artifact on every publish."""
        p = self._llm_header(
            tmp_path, model="deterministic", prompt_version="det.v11"
        )
        assert is_pair_complete(
            p, model="claude-sonnet-4-6", prompt_version="phase-a.v1"
        ) is True

    def test_grown_record_pool_not_complete(self, tmp_path: Path) -> None:
        """The v26 leaf-borrow / v27 domain-expansion class: ingest adds
        rows, the artifact's own header still self-reports complete."""
        p = self._llm_header(tmp_path)
        assert is_pair_complete(p, expected_record_count=12) is False
        assert is_pair_complete(p, expected_record_count=10) is True

    def test_shrunk_record_pool_still_complete(self, tmp_path: Path) -> None:
        """Directional by design: a pool NARROWING must not trigger a
        re-run — the 2026-07-09 live drill found spine-lens artifacts
        carrying rows from a pre-v26 wider pool that are load-bearing
        for the spine sidecars; a re-run would rewrite them narrow."""
        p = self._llm_header(tmp_path)
        assert is_pair_complete(p, expected_record_count=4) is True


class TestRunAllPoolGuard:
    """run_all wires the live-pool count into skip-if-complete on the
    production path (issue #212 item 7)."""

    def _setup(self, tmp_path, monkeypatch, *, artifact_records: int,
               live_records: int):
        import src.score.extract as extract_mod
        import src.score.runner as runner_mod

        artifact = tmp_path / "AZ_spine_has_conditional_logic.jsonl"
        artifact.write_text(
            json.dumps({
                "__type": "header", "status": "complete",
                "record_count": artifact_records,
                "scored_count": artifact_records,
                "lens": "spine", "model": "claude-sonnet-4-6",
                "prompt_version": "phase-a.v1",
            }) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            runner_mod, "scoring_phase_a_artifact_path",
            lambda state, fact, lens: artifact,
        )
        monkeypatch.setattr(
            extract_mod, "load_phase_a_records",
            lambda state, lens, **kw: [object()] * live_records,
        )
        calls: list[str] = []

        def fake_extract(**kwargs):
            calls.append(kwargs["fact"])
            return {
                "status": "complete",
                "record_count": live_records,
                "scored_count": live_records,
                "total_usd": 0.0, "cache_hit_count": 0,
                "downgrade_count": 0,
            }

        return artifact, calls, fake_extract

    def test_grown_pool_reruns_pair(self, tmp_path, monkeypatch):
        artifact, calls, fake_extract = self._setup(
            tmp_path, monkeypatch, artifact_records=5, live_records=7
        )
        manifest = run_all(
            states=["AZ"], facts=["has_conditional_logic"], lens="spine",
            manifest_path=tmp_path / "manifest.json",
            extract_fn=fake_extract,
        )
        assert calls == ["has_conditional_logic"]
        assert manifest.pairs[0].status == "complete"

    def test_shrunk_pool_skips_with_loud_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        artifact, calls, fake_extract = self._setup(
            tmp_path, monkeypatch, artifact_records=9, live_records=4
        )
        with caplog.at_level("WARNING"):
            manifest = run_all(
                states=["AZ"], facts=["has_conditional_logic"], lens="spine",
                manifest_path=tmp_path / "manifest.json",
                extract_fn=fake_extract,
            )
        assert calls == []
        assert manifest.pairs[0].status == "skipped"
        assert any(
            "pool archaeology" in r.message for r in caplog.records
        )

    def test_matching_pool_still_skips(self, tmp_path, monkeypatch):
        artifact, calls, fake_extract = self._setup(
            tmp_path, monkeypatch, artifact_records=7, live_records=7
        )
        manifest = run_all(
            states=["AZ"], facts=["has_conditional_logic"], lens="spine",
            manifest_path=tmp_path / "manifest.json",
            extract_fn=fake_extract,
        )
        assert calls == []
        assert manifest.pairs[0].status == "skipped"


# ---------------------------------------------------------------------------
# Runner — skip-if-complete + cost cap + ordering
# ---------------------------------------------------------------------------


class TestRunAll:
    def test_deterministic_ordering(self, tmp_path: Path) -> None:
        """States outer in SUPPORTED_STATES order, facts inner in the
        lens's roster order — regardless of input arg order. Spine-lens
        default drops source-only LLM facts (Task 2 cost fix)."""
        call_order: list[tuple[str, str]] = []

        def _fake_extract(**kwargs: Any) -> dict[str, Any]:
            call_order.append((kwargs["state"], kwargs["fact"]))
            return _complete_header(kwargs["state"], kwargs["fact"], 10, 10, 0.01)

        spine_roster = phase_b_facts_for_lens("spine")
        manifest = run_all(
            states=["TX", "AZ", "WI", "MN", "IN"],
            facts=list(spine_roster),
            manifest_path=tmp_path / RUN_MANIFEST_NAME,
            out_dir=tmp_path,
            extract_fn=_fake_extract,
        )
        expected_pairs = [(s, f) for s in ("AZ", "WI", "MN", "TX", "IN") for f in spine_roster]
        assert call_order == expected_pairs
        assert len(manifest.pairs) == len(expected_pairs)
        assert all(p.status == "complete" for p in manifest.pairs)

    def test_skip_if_complete_does_not_call_extract(self, tmp_path: Path) -> None:
        """Pre-seed a complete artifact for AZ/has_conditional_logic.
        The runner must not invoke extract_fn for that pair."""
        artifact = tmp_path / "AZ_spine_has_conditional_logic.jsonl"
        artifact.write_text(
            json.dumps({
                "__type": "header",
                "status": "complete",
                "state": "AZ",
                "fact": "has_conditional_logic",
                # Real artifact headers always carry these three — the
                # issue #212 item 7 staleness guard compares them.
                "lens": "spine",
                "model": "claude-sonnet-4-6",
                "prompt_version": "phase-a.v1",
                "record_count": 200,
                "scored_count": 200,
                "total_usd": 0.2166,
                "cache_hit_count": 38,
                "downgrade_count": 0,
            }) + "\n",
            encoding="utf-8",
        )
        called: list[tuple[str, str]] = []

        def _fake_extract(**kwargs: Any) -> dict[str, Any]:
            called.append((kwargs["state"], kwargs["fact"]))
            return _complete_header(kwargs["state"], kwargs["fact"], 10, 10, 0.01)

        manifest = run_all(
            states=["AZ"],
            facts=["has_conditional_logic"],
            manifest_path=tmp_path / RUN_MANIFEST_NAME,
            out_dir=tmp_path,
            extract_fn=_fake_extract,
        )
        assert called == []
        assert manifest.pairs[0].status == "skipped"
        assert manifest.pairs[0].scored_count == 200
        assert manifest.pairs[0].total_usd == pytest.approx(0.2166)

    def test_incomplete_artifact_reruns(self, tmp_path: Path) -> None:
        """A `status="aborted"` or `"running"` artifact must re-invoke
        extract — the cache's replay inside extract.run() is what makes
        that cheap, but the runner's contract is "rerun if not complete"."""
        artifact = tmp_path / "AZ_spine_has_conditional_logic.jsonl"
        artifact.write_text(
            json.dumps({
                "__type": "header",
                "status": "aborted",
                "state": "AZ",
                "fact": "has_conditional_logic",
                "record_count": 200,
                "scored_count": 120,
                "total_usd": 0.12,
            }) + "\n",
            encoding="utf-8",
        )
        called: list[tuple[str, str]] = []

        def _fake_extract(**kwargs: Any) -> dict[str, Any]:
            called.append((kwargs["state"], kwargs["fact"]))
            return _complete_header(kwargs["state"], kwargs["fact"], 200, 200, 0.22)

        manifest = run_all(
            states=["AZ"],
            facts=["has_conditional_logic"],
            manifest_path=tmp_path / RUN_MANIFEST_NAME,
            out_dir=tmp_path,
            extract_fn=_fake_extract,
        )
        assert called == [("AZ", "has_conditional_logic")]
        assert manifest.pairs[0].status == "complete"

    def test_cost_cap_skips_remaining_pairs(self, tmp_path: Path) -> None:
        """If one pair spends enough to exhaust the global cap, later
        pairs get status=skipped_cost_cap and extract is not called for
        them."""
        state_calls: list[str] = []

        def _fake_extract(**kwargs: Any) -> dict[str, Any]:
            state_calls.append(kwargs["state"])
            # AZ consumes all the cap; WI should be skipped.
            return _complete_header(kwargs["state"], kwargs["fact"], 100, 100, 5.0)

        manifest = run_all(
            states=["AZ", "WI"],
            facts=["has_conditional_logic"],
            cost_cap=4.0,
            manifest_path=tmp_path / RUN_MANIFEST_NAME,
            out_dir=tmp_path,
            extract_fn=_fake_extract,
        )
        # AZ ran (spent $5), WI skipped.
        assert state_calls == ["AZ"]
        statuses = [p.status for p in manifest.pairs]
        assert statuses == ["complete", "skipped_cost_cap"]
        assert "cost cap" in (manifest.pairs[1].error or "").lower()

    def test_manifest_is_streamed_per_pair(self, tmp_path: Path) -> None:
        """Crash after pair N leaves manifest with N entries on disk.
        Simulated by capturing the manifest state across `extract` calls."""
        manifest_path = tmp_path / RUN_MANIFEST_NAME
        seen_on_disk: list[int] = []

        def _fake_extract(**kwargs: Any) -> dict[str, Any]:
            # Record how many pairs the manifest has at the moment this
            # call is fired (i.e., before the runner flushes the current pair).
            if manifest_path.exists():
                doc = json.loads(manifest_path.read_text(encoding="utf-8"))
                seen_on_disk.append(len(doc["pairs"]))
            return _complete_header(kwargs["state"], kwargs["fact"], 10, 10, 0.0)

        run_all(
            states=["AZ", "WI", "MN", "TX", "IN"],
            facts=["has_conditional_logic"],
            manifest_path=manifest_path,
            out_dir=tmp_path,
            extract_fn=_fake_extract,
        )
        # Five states → extract called 5 times; the manifest on disk
        # at each call reflects only the *previously completed* pairs
        # (0, 1, 2, 3, 4).
        assert seen_on_disk == [0, 1, 2, 3, 4]
        # Final manifest carries all five.
        final = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(final["pairs"]) == 5

    def test_failed_pair_halts_and_persists(self, tmp_path: Path) -> None:
        """A raised non-CostCap exception is recorded as status=failed
        and re-raised, *after* the manifest has been flushed to disk."""
        from src.score.schema import ScoringSchemaError

        manifest_path = tmp_path / RUN_MANIFEST_NAME

        def _fake_extract(**kwargs: Any) -> dict[str, Any]:
            if kwargs["state"] == "AZ":
                return _complete_header("AZ", kwargs["fact"], 10, 10, 0.01)
            raise ScoringSchemaError("boom", payload=[])

        with pytest.raises(ScoringSchemaError):
            run_all(
                states=["AZ", "WI"],
                facts=["has_conditional_logic"],
                manifest_path=manifest_path,
                out_dir=tmp_path,
                extract_fn=_fake_extract,
            )
        # Manifest on disk carries AZ=complete + WI=failed.
        final = json.loads(manifest_path.read_text(encoding="utf-8"))
        statuses = [p["status"] for p in final["pairs"]]
        assert statuses == ["complete", "failed"]
        assert "ScoringSchemaError" in (final["pairs"][1]["error"] or "")


class TestCheckpointAfter:
    """Phase D carryover #6: ``--checkpoint-after STATE`` pauses the
    fanout so a polarity / prompt bug stops after the cheapest state
    instead of walking through the whole cost cap."""

    def test_callback_invoked_after_named_state_completes(
        self, tmp_path: Path
    ) -> None:
        callback_fired_after: list[str] = []

        def _cb(state: str, manifest: RunManifest) -> bool:
            callback_fired_after.append(state)
            return True  # continue

        def _fake_extract(**kwargs: Any) -> dict[str, Any]:
            return _complete_header(kwargs["state"], kwargs["fact"], 5, 5, 0.01)

        run_all(
            states=["AZ", "WI", "MN", "TX", "IN"],
            facts=["has_conditional_logic"],
            manifest_path=tmp_path / RUN_MANIFEST_NAME,
            out_dir=tmp_path,
            extract_fn=_fake_extract,
            checkpoint_after="AZ",
            checkpoint_callback=_cb,
        )
        # Callback fires exactly once — after AZ's fact loop.
        assert callback_fired_after == ["AZ"]

    def test_callback_returning_false_halts_fanout(self, tmp_path: Path) -> None:
        called_states: list[str] = []

        def _fake_extract(**kwargs: Any) -> dict[str, Any]:
            called_states.append(kwargs["state"])
            return _complete_header(kwargs["state"], kwargs["fact"], 5, 5, 0.01)

        manifest = run_all(
            states=["AZ", "WI", "MN", "TX", "IN"],
            facts=["has_conditional_logic"],
            manifest_path=tmp_path / RUN_MANIFEST_NAME,
            out_dir=tmp_path,
            extract_fn=_fake_extract,
            checkpoint_after="AZ",
            checkpoint_callback=lambda state, manifest: False,
        )
        # Only AZ was extracted — callback returning False halted WI/MN/TX.
        assert set(called_states) == {"AZ"}
        # Manifest carries only AZ's pair.
        assert {p.state for p in manifest.pairs} == {"AZ"}

    def test_no_checkpoint_means_no_pause(self, tmp_path: Path) -> None:
        """Default behavior: no checkpoint_after → callback never fires."""
        fires: list[str] = []

        def _fake_extract(**kwargs: Any) -> dict[str, Any]:
            return _complete_header(kwargs["state"], kwargs["fact"], 5, 5, 0.01)

        run_all(
            states=["AZ", "WI"],
            facts=["has_conditional_logic"],
            manifest_path=tmp_path / RUN_MANIFEST_NAME,
            out_dir=tmp_path,
            extract_fn=_fake_extract,
            checkpoint_callback=lambda s, m: (fires.append(s), True)[1],  # type: ignore[func-returns-value]
        )
        assert fires == []  # checkpoint_after=None => callback ignored


def _complete_header(state: str, fact: str, record_count: int, scored_count: int, usd: float) -> dict[str, Any]:
    """Minimal complete-run header mock matching extract.run()'s shape."""
    return {
        "__type": "header",
        "state": state,
        "lens": "spine",
        "fact": fact,
        "status": "complete",
        "record_count": record_count,
        "scored_count": scored_count,
        "total_usd": usd,
        "cache_hit_count": 0,
        "downgrade_count": 0,
        "mode": "api",
    }


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestRunAllCliWiring:
    def test_help_runs(self) -> None:
        from src.cli import cli
        result = CliRunner().invoke(cli, ["score", "run-all", "--help"])
        assert result.exit_code == 0, result.output
        assert "skip-if-complete" in result.output.lower()

    def test_unknown_fact_exits_two(self) -> None:
        from src.cli import cli
        result = CliRunner().invoke(cli, ["score", "run-all", "--facts", "not_real"])
        assert result.exit_code == 2
        assert "unknown fact" in result.output

    def test_happy_path_invokes_runner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLI calls run_all with parsed args. Run_all is monkeypatched
        to avoid any network + filesystem work."""
        seen: dict[str, Any] = {}

        def _fake_run_all(**kwargs: Any) -> RunManifest:
            seen.update(kwargs)
            pair = PairResult(
                state="AZ", fact="has_conditional_logic",
                status="complete", record_count=10, scored_count=10,
                total_usd=0.01, cache_hit_count=0, downgrade_count=0,
                error=None, artifact=str(tmp_path / "AZ_spine_has_conditional_logic.jsonl"),
            )
            # Mirror the real runner's progress callback contract.
            cb = kwargs.get("progress")
            if cb is not None:
                cb(pair)
            return RunManifest(
                started_at="2026-04-22T00:00:00Z",
                ended_at="2026-04-22T00:00:01Z",
                pairs=[pair],
                total_usd=0.01,
            )

        monkeypatch.setattr(runner_module, "run_all", _fake_run_all)
        # Re-import path: the CLI calls `from src.score.runner import run_all`.
        # Our monkeypatch on the module covers that path.
        from src.cli import cli
        result = CliRunner().invoke(
            cli,
            ["score", "run-all", "--state", "AZ", "--facts", "has_conditional_logic", "--cost-cap", "5.0"],
        )
        assert result.exit_code == 0, result.output
        assert seen["states"] == ["AZ"]
        assert seen["facts"] == ["has_conditional_logic"]
        assert seen["cost_cap"] == 5.0
        assert "COMPLETE" in result.output
        assert "$0.0100 total" in result.output


# ---------------------------------------------------------------------------
# Lens-aware fact roster (self-onboarding gap #1 + #2)
# ---------------------------------------------------------------------------


class TestLensAwareFacts:
    """``phase_b_facts_for_lens`` is lens-aware: each lens's ``--facts all``
    roster includes exactly the facts its rule cascade + observability
    surface consumes, plus the shared productization signal axis.

    Post-merge Task 2: source-only LLM prompts (extension_*, state_narrows/
    broadens_edfi_scope, definition_adds_detail_beyond_edfi, semantic_class)
    drop off the spine roster, and spine-only LLM prompts
    (definition_is_implementable, required_when_stated,
    conditional_reporting_stated, populations_or_scope_stated) drop off the
    source roster — running them on the wrong lens was cold-spinning ~$2.50
    of LLM spend per state per run-all for no downstream reader.
    """

    def test_spine_roster_includes_expected_facts(self) -> None:
        roster = phase_b_facts_for_lens("spine")
        # Spine LLM rule inputs present.
        for fact in (
            "has_conditional_logic",
            "definition_is_implementable",
            "required_when_stated",
            "conditional_reporting_stated",
            "populations_or_scope_stated",
            "has_cross_entity_logic",
            "has_aggregation",
            "has_concatenation",
            "cross_entity_targets",
            "documentation_style",
        ):
            assert fact in roster, fact
        # Productization signal carried on both lenses.
        assert "integration_class" in roster
        # Spine observability: semantic_class is extracted but not consumed.
        assert "semantic_class" in roster
        # Shared deterministics.
        assert "is_natural_key" in roster
        for fact in (
            "fk_chain_depth",
            "reference_fan_out",
            "sub_collection_depth",
            "entity_extension_footprint",
            "descriptor_enum_breadth",
        ):
            assert fact in roster, fact

    def test_spine_roster_excludes_source_only_llm_facts(self) -> None:
        roster = phase_b_facts_for_lens("spine")
        for fact in (
            "definition_adds_detail_beyond_edfi",
            "state_scope_delta",
            "extension_is_necessary",
            "extension_is_standalone",
        ):
            assert fact not in roster, fact
        # Source-only deterministic facts must also stay off the spine roster.
        for fact in (
            "element_name_matches_canonical",
            "naming_deviation_cosmetic",
            "extension_mirrors_core_pattern",
        ):
            assert fact not in roster, fact

    def test_source_roster_includes_expected_facts(self) -> None:
        roster = phase_b_facts_for_lens("source")
        # Source-only LLM rule inputs present.
        for fact in (
            "definition_adds_detail_beyond_edfi",
            "semantic_class",
            "state_scope_delta",
            "extension_is_necessary",
            "extension_is_standalone",
        ):
            assert fact in roster, fact
        # Shared-with-spine Phase F NACHOS inputs present on source too.
        for fact in (
            "has_conditional_logic",
            "has_cross_entity_logic",
            "has_aggregation",
            "has_concatenation",
            "cross_entity_targets",
            "documentation_style",
        ):
            assert fact in roster, fact
        # Productization signal carried on both lenses.
        assert "integration_class" in roster
        # Source-lens-exclusive deterministic facts.
        for fact in SOURCE_DETERMINISTIC_FACTS:
            assert fact in roster
        # Shared spine-backed deterministic facts (needed by source rules too).
        assert "is_natural_key" in roster

    def test_source_roster_excludes_spine_only_llm_facts(self) -> None:
        """Main Task 2 cost fix: the four spine-only LLM prompts must not
        ride along on source-lens ``--facts all``."""
        roster = phase_b_facts_for_lens("source")
        for fact in (
            "definition_is_implementable",
            "required_when_stated",
            "conditional_reporting_stated",
            "populations_or_scope_stated",
        ):
            assert fact not in roster, fact

    def test_unknown_lens_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown lens"):
            phase_b_facts_for_lens("both")

    def test_run_all_source_lens_fans_out_source_deterministic(
        self, tmp_path: Path
    ) -> None:
        """`run_all --lens source --facts all` must schedule every
        source-deterministic fact (cold-run §4 fanout)."""
        scheduled: list[tuple[str, str]] = []

        def _fake_extract(**kwargs: Any) -> dict[str, Any]:
            scheduled.append((kwargs["state"], kwargs["fact"]))
            return _complete_header(kwargs["state"], kwargs["fact"], 5, 5, 0.0)

        run_all(
            states=["AZ"],
            facts=list(phase_b_facts_for_lens("source")),
            lens="source",
            manifest_path=tmp_path / RUN_MANIFEST_NAME,
            out_dir=tmp_path,
            extract_fn=_fake_extract,
        )
        scheduled_facts = {fact for _, fact in scheduled}
        for fact in SOURCE_DETERMINISTIC_FACTS:
            assert fact in scheduled_facts

    def test_cli_run_all_accepts_lens_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Self-onboarding gap: `run-all --lens source` must not be
        CLI-rejected. The runner is lens-agnostic; the Choice gate was
        stale from Phase B."""
        seen: dict[str, Any] = {}

        def _fake_run_all(**kwargs: Any) -> RunManifest:
            seen.update(kwargs)
            return RunManifest(
                started_at="2026-04-22T00:00:00Z",
                ended_at="2026-04-22T00:00:01Z",
                pairs=[],
                total_usd=0.0,
            )

        monkeypatch.setattr(runner_module, "run_all", _fake_run_all)
        from src.cli import cli
        result = CliRunner().invoke(
            cli,
            [
                "score", "run-all",
                "--state", "AZ",
                "--lens", "source",
                "--facts", "element_name_matches_canonical",
                "--cost-cap", "0.0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert seen["lens"] == "source"
        assert seen["facts"] == ["element_name_matches_canonical"]

    def test_cli_run_all_source_all_facts_expands_to_source_roster(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`run-all --lens source --facts all` expands to the source
        roster (PHASE_B_FACTS ∪ SOURCE_DETERMINISTIC_FACTS), so the
        downstream aggregate finds every artifact it needs."""
        seen: dict[str, Any] = {}

        def _fake_run_all(**kwargs: Any) -> RunManifest:
            seen.update(kwargs)
            return RunManifest(
                started_at="2026-04-22T00:00:00Z",
                ended_at="2026-04-22T00:00:01Z",
                pairs=[],
                total_usd=0.0,
            )

        monkeypatch.setattr(runner_module, "run_all", _fake_run_all)
        from src.cli import cli
        result = CliRunner().invoke(
            cli,
            ["score", "run-all", "--lens", "source", "--facts", "all"],
        )
        assert result.exit_code == 0, result.output
        for fact in SOURCE_DETERMINISTIC_FACTS:
            assert fact in seen["facts"]
