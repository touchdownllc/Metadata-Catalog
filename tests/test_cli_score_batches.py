"""CLI wiring tests for the Anthropic Batch API surface.

Covers:
- ``score run-all --batch`` happy path (submit + print).
- ``score run-all --batch`` cache-all-hit messaging.
- ``score run-all --batch --checkpoint-after AZ`` incompatibility.
- ``score run-all --batch`` cost-gate exit.
- ``score batches list`` empty + populated output.
- ``score batches status <id>`` output formatting.
- ``score batches collect <id>`` summary output.

All tests mock the runner / client at the lazy-import seam so no
network calls occur and no real ``data/cache/scoring/batches/`` is
touched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from click.testing import CliRunner

from src.score import batch_runner as batch_runner_module
from src.score import batch_manifest as batch_manifest_module
from src.score.batch_client import BatchStatus
from src.score.batch_manifest import BatchItem, BatchManifest
from src.score.batch_runner import BatchCostGateError, CollectSummary


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _make_manifest(batch_id: str, *, items: int = 3, est: float = 4.20) -> BatchManifest:
    return BatchManifest(
        batch_id=batch_id,
        submitted_at="2026-04-25T10:00:00Z",
        model="claude-sonnet-4-6",
        prompt_version="9",
        estimated_cost_usd=est,
        items=[
            BatchItem(
                custom_id=f"{i:064x}",
                state="AZ", lens="spine", fact="has_conditional_logic",
                entity="StudentAssessment", record_count=4,
                estimated_tokens_in=200,
            )
            for i in range(items)
        ],
    )


# ---------------------------------------------------------------------------
# score run-all --batch
# ---------------------------------------------------------------------------


class TestRunAllBatchFlag:
    def test_submits_and_prints_batch_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _fake_submit(**kwargs: Any) -> list[BatchManifest]:
            captured.update(kwargs)
            return [_make_manifest("batch_one"), _make_manifest("batch_two")]

        monkeypatch.setattr(batch_runner_module, "submit_run_all", _fake_submit)

        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["score", "run-all", "--batch", "--state", "AZ", "--lens", "spine"],
        )
        assert result.exit_code == 0, result.output
        assert "Submitted 2 batches" in result.output
        assert "batch_one" in result.output
        assert "batch_two" in result.output
        assert "mc score batches collect batch_one" in result.output
        assert "mc score batches collect batch_two" in result.output
        # Confirm flags routed correctly
        assert captured["lens"] == "spine"
        assert captured["states"] == ["AZ"]
        assert captured["confirm"] is False  # --yes not passed
        assert captured["max_cost_usd"] == 10.0  # default

    def test_yes_flag_routes_to_confirm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def _fake_submit(**kwargs: Any) -> list[BatchManifest]:
            captured.update(kwargs)
            return [_make_manifest("batch_yes")]

        monkeypatch.setattr(batch_runner_module, "submit_run_all", _fake_submit)

        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["score", "run-all", "--batch", "--yes", "--max-cost", "0.01"],
        )
        assert result.exit_code == 0, result.output
        assert captured["confirm"] is True
        assert captured["max_cost_usd"] == pytest.approx(0.01)

    def test_all_cached_prints_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            batch_runner_module, "submit_run_all", lambda **_: []
        )

        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["score", "run-all", "--batch"])
        assert result.exit_code == 0, result.output
        assert "already cached" in result.output
        assert "no batch submitted" in result.output.lower()

    def test_checkpoint_after_with_batch_exits_two(self) -> None:
        """``--checkpoint-after`` is meaningful only for the sync path."""
        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["score", "run-all", "--batch", "--checkpoint-after", "AZ"],
        )
        assert result.exit_code == 2
        assert "incompatible" in result.output.lower()

    def test_cost_gate_exits_two(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_submit(**_: Any) -> list[BatchManifest]:
            raise BatchCostGateError(
                "estimated batch cost $14.00 exceeds --max-cost $10.00 (1247 requests). "
                "Re-run with --yes to confirm."
            )

        monkeypatch.setattr(batch_runner_module, "submit_run_all", _fake_submit)

        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["score", "run-all", "--batch"])
        assert result.exit_code == 2
        assert "exceeds" in result.output
        assert "$10.00" in result.output


# ---------------------------------------------------------------------------
# score batches list
# ---------------------------------------------------------------------------


class TestBatchesList:
    def test_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(batch_manifest_module, "list_manifests", lambda **_: [])

        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["score", "batches", "list"])
        assert result.exit_code == 0, result.output
        assert "No batch manifests" in result.output

    def test_populated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manifests = [
            _make_manifest("batch_alpha", items=5, est=2.50),
            _make_manifest("batch_beta", items=12, est=8.40),
        ]
        monkeypatch.setattr(
            batch_manifest_module, "list_manifests", lambda **_: manifests
        )

        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["score", "batches", "list"])
        assert result.exit_code == 0, result.output
        assert "batch_alpha" in result.output
        assert "batch_beta" in result.output
        # Numbers appear right-aligned in the table; check for the digits.
        assert "2.50" in result.output
        assert "8.40" in result.output
        # Header row
        assert "submitted_at" in result.output
        assert "items" in result.output


# ---------------------------------------------------------------------------
# score batches status
# ---------------------------------------------------------------------------


class TestBatchesStatus:
    def test_prints_processing_status_and_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Construct a fake BatchClient class (constructor signature:
        # ``BatchClient(*, model=..., api_key=None, max_tokens=8192)``).
        # The CLI imports lazily; monkeypatching the class on the
        # module level makes the lazy import return our fake.
        class _FakeBatchClient:
            def __init__(self, **_: Any) -> None:
                pass

            def status(self, batch_id: str) -> BatchStatus:
                return BatchStatus(
                    batch_id=batch_id,
                    processing_status="ended",
                    request_counts={
                        "processing": 0, "succeeded": 7, "errored": 1,
                        "canceled": 0, "expired": 0,
                    },
                    ended_at="2026-04-25T11:30:00Z",
                )

        from src.score import batch_client as batch_client_module
        monkeypatch.setattr(batch_client_module, "BatchClient", _FakeBatchClient)

        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["score", "batches", "status", "batch_xyz"])
        assert result.exit_code == 0, result.output
        assert "batch_xyz" in result.output
        assert "ended" in result.output
        assert "succeeded" in result.output
        assert "7" in result.output


# ---------------------------------------------------------------------------
# score batches collect
# ---------------------------------------------------------------------------


class TestBatchesCollect:
    def test_prints_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fake_collect(batch_id: str, **_: Any) -> CollectSummary:
            return CollectSummary(
                batch_id=batch_id,
                succeeded_count=10,
                cache_writes=10,
                parse_failed_count=0,
                errored_count=2,
                canceled_count=0,
                expired_count=0,
                actual_cost_usd=2.345,
            )

        monkeypatch.setattr(batch_runner_module, "collect_batch", _fake_collect)

        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["score", "batches", "collect", "batch_xyz"])
        assert result.exit_code == 0, result.output
        assert "batch_xyz" in result.output
        assert "10 succeeded" in result.output
        assert "errored:" in result.output
        assert "2" in result.output
        assert "$2.3450" in result.output  # %.4f format
        # Failure types with zero count are suppressed
        assert "canceled:" not in result.output
        assert "parse_failed:" not in result.output

    def test_silences_zero_failure_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A clean run with all succeeded shouldn't show empty failure lines."""
        def _fake_collect(batch_id: str, **_: Any) -> CollectSummary:
            return CollectSummary(
                batch_id=batch_id,
                succeeded_count=5,
                cache_writes=5,
                parse_failed_count=0,
                errored_count=0,
                canceled_count=0,
                expired_count=0,
                actual_cost_usd=0.789,
            )

        monkeypatch.setattr(batch_runner_module, "collect_batch", _fake_collect)

        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["score", "batches", "collect", "batch_clean"])
        assert result.exit_code == 0, result.output
        assert "5 succeeded" in result.output
        for noisy in ("parse_failed", "errored", "canceled", "expired"):
            # Be precise: each failure label appears with a colon when
            # surfaced; absence of the colon-form means no row was
            # printed for that class.
            assert f"{noisy}:" not in result.output


# ---------------------------------------------------------------------------
# Click wiring sanity
# ---------------------------------------------------------------------------


class TestClickWiring:
    def test_run_all_help_documents_batch_flag(self) -> None:
        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["score", "run-all", "--help"])
        assert result.exit_code == 0
        assert "--batch" in result.output
        assert "--max-cost" in result.output
        # Click wraps long help text — search for unique substrings the
        # wrapper can't split mid-token.
        assert "Anthropic" in result.output
        assert "Batch API" in result.output

    def test_batches_help_lists_subcommands(self) -> None:
        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["score", "batches", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "status" in result.output
        assert "collect" in result.output
