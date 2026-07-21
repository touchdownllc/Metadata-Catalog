"""Unit tests for ``src.score.batch_manifest``.

The manifest is the audit record for one Anthropic Batch API
submission. Tests cover write/read round-trip, list ordering, the
explicit not-found error message, and atomic-write recovery (stale
``.tmp`` files left by a SIGKILL must not break ``list_manifests``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.score.batch_manifest import (
    BatchItem,
    BatchManifest,
    list_manifests,
    manifest_path,
    read_manifest,
    write_manifest,
)


def _make_manifest(batch_id: str = "batch_01abcd", *, items: int = 2) -> BatchManifest:
    return BatchManifest(
        batch_id=batch_id,
        submitted_at="2026-04-25T10:00:00Z",
        model="claude-sonnet-4-6",
        prompt_version="9",
        estimated_cost_usd=12.345678,
        items=[
            BatchItem(
                custom_id=f"{i:064x}",
                state="AZ",
                lens="spine",
                fact="has_conditional_logic",
                entity="StudentAssessment",
                record_count=42,
                estimated_tokens_in=1234,
            )
            for i in range(items)
        ],
    )


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    manifest = _make_manifest()
    target = write_manifest(manifest, root=tmp_path)
    assert target == manifest_path(manifest.batch_id, root=tmp_path)
    assert target.exists()

    loaded = read_manifest(manifest.batch_id, root=tmp_path)
    assert loaded.batch_id == manifest.batch_id
    assert loaded.submitted_at == manifest.submitted_at
    assert loaded.model == manifest.model
    assert loaded.prompt_version == manifest.prompt_version
    assert loaded.estimated_cost_usd == pytest.approx(manifest.estimated_cost_usd)
    assert len(loaded.items) == len(manifest.items)
    for orig, got in zip(manifest.items, loaded.items):
        assert orig == got


def test_read_missing_manifest_raises_with_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError) as exc:
        read_manifest("batch_doesnotexist", root=tmp_path)
    # Operator-friendly error must include the resolved path so the
    # mistake is obvious (typo'd id, wrong dir).
    assert "batch_doesnotexist" in str(exc.value)
    assert str(tmp_path) in str(exc.value)


def test_list_manifests_sorts_by_submitted_at_ascending(tmp_path: Path) -> None:
    older = _make_manifest("batch_old")
    older.submitted_at = "2026-04-23T08:00:00Z"
    newer = _make_manifest("batch_new")
    newer.submitted_at = "2026-04-25T08:00:00Z"
    middle = _make_manifest("batch_mid")
    middle.submitted_at = "2026-04-24T08:00:00Z"

    write_manifest(newer, root=tmp_path)
    write_manifest(older, root=tmp_path)
    write_manifest(middle, root=tmp_path)

    listed = list_manifests(root=tmp_path)
    assert [m.batch_id for m in listed] == ["batch_old", "batch_mid", "batch_new"]


def test_list_manifests_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert list_manifests(root=missing) == []


def test_list_manifests_skips_stale_tmp_files(tmp_path: Path) -> None:
    """A SIGKILL between tmp-stage and ``os.replace`` leaves a ``.tmp``.

    ``list_manifests`` must ignore those without raising — they are
    not valid manifest JSON yet, and a future ``write_manifest`` call
    will overwrite (or rename onto) the real path anyway.
    """
    write_manifest(_make_manifest("batch_real"), root=tmp_path)
    stale_tmp = tmp_path / "batch_partial.json.tmp"
    stale_tmp.write_text("{ partial json that didn't finish writing", encoding="utf-8")
    listed = list_manifests(root=tmp_path)
    assert [m.batch_id for m in listed] == ["batch_real"]


def test_list_manifests_skips_corrupt_json(tmp_path: Path) -> None:
    """A genuinely corrupt manifest is skipped, not raised on.

    Corrupt manifest = manual edit gone wrong, partial copy from
    another machine, etc. We log nothing here and move on; the
    operator can spot the missing batch in ``score batches list``.
    """
    write_manifest(_make_manifest("batch_real"), root=tmp_path)
    corrupt = tmp_path / "batch_garbage.json"
    corrupt.write_text("not json at all", encoding="utf-8")
    listed = list_manifests(root=tmp_path)
    assert [m.batch_id for m in listed] == ["batch_real"]


def test_write_manifest_creates_parent_dir(tmp_path: Path) -> None:
    """Calling write before any prior write must mkdir -p the root."""
    nested = tmp_path / "data" / "cache" / "scoring" / "batches"
    assert not nested.exists()
    write_manifest(_make_manifest("batch_nested"), root=nested)
    assert (nested / "batch_nested.json").exists()


def test_write_manifest_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    """Successful write deletes the staging tmp file via os.replace."""
    write_manifest(_make_manifest("batch_atomic"), root=tmp_path)
    assert (tmp_path / "batch_atomic.json").exists()
    assert not (tmp_path / "batch_atomic.json.tmp").exists()


def test_to_dict_rounds_estimated_cost_to_six_places(tmp_path: Path) -> None:
    """The on-disk JSON rounds USD to 6 places — same as run_manifest.json."""
    manifest = _make_manifest()
    manifest.estimated_cost_usd = 1.123456789
    target = write_manifest(manifest, root=tmp_path)
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert raw["estimated_cost_usd"] == 1.123457
