"""Unit tests for ``src.score.extract.render_batches_for``.

The helper is the shared rendering seam between the sync ``run()``
path and the Anthropic Batch API path. Its key contract: calling it
twice for the same (fact, state, lens, limit) inputs produces
byte-identical batches. If that invariant breaks, the cache key
collisions stop and warm reruns regress to cold.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.models.element import ElementRecord
from src.score import extract as extract_module
from src.score.extract import render_batches_for

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RED_TEAM_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "scoring_phase_a_red_team.json"


def _load_red_team() -> list[ElementRecord]:
    raw = json.loads(_RED_TEAM_FIXTURE.read_text(encoding="utf-8"))
    return [
        ElementRecord.model_validate({k: v for k, v in r.items() if not k.startswith("_")})
        for r in raw
    ]


def _patch_load_records(monkeypatch: pytest.MonkeyPatch, records: list[ElementRecord]) -> None:
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


def test_render_batches_groups_by_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    """One batch per entity, preserving record order within a batch."""
    records = _load_red_team()
    _patch_load_records(monkeypatch, records)

    batches = render_batches_for(
        fact="has_conditional_logic", state="AZ", lens="spine"
    )
    assert batches, "fixture must produce at least one batch"

    # Each batch's group_records share the same entity name.
    for entity, group_records, _system, _user in batches:
        assert all(r.entity == entity for r in group_records)


def test_render_batches_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling twice with same inputs yields byte-identical prompt text.

    This is the cache-stability invariant — if it breaks, sync writes
    one cache key while the batch path computes a different key, and
    warm reruns regress from $0 to cold.
    """
    records = _load_red_team()
    _patch_load_records(monkeypatch, records)

    a = render_batches_for(fact="has_conditional_logic", state="AZ", lens="spine")
    b = render_batches_for(fact="has_conditional_logic", state="AZ", lens="spine")

    assert len(a) == len(b)
    for (e_a, _, sys_a, usr_a), (e_b, _, sys_b, usr_b) in zip(a, b):
        assert e_a == e_b
        assert sys_a == sys_b
        assert usr_a == usr_b


def test_render_batches_honors_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    records = _load_red_team()
    _patch_load_records(monkeypatch, records)

    batches = render_batches_for(
        fact="has_conditional_logic", state="AZ", lens="spine", limit=1
    )
    record_count = sum(len(group) for _, group, _, _ in batches)
    assert record_count == 1


def test_render_batches_rejects_unsupported_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    records = _load_red_team()
    _patch_load_records(monkeypatch, records)

    with pytest.raises(SystemExit):
        render_batches_for(fact="not_a_real_fact", state="AZ", lens="spine")


def test_render_batches_rejects_unsupported_state(monkeypatch: pytest.MonkeyPatch) -> None:
    records = _load_red_team()
    _patch_load_records(monkeypatch, records)

    with pytest.raises(SystemExit):
        render_batches_for(fact="has_conditional_logic", state="ZZ", lens="spine")


def test_render_batches_rejects_unsupported_lens(monkeypatch: pytest.MonkeyPatch) -> None:
    records = _load_red_team()
    _patch_load_records(monkeypatch, records)

    with pytest.raises(SystemExit):
        render_batches_for(fact="has_conditional_logic", state="AZ", lens="quantum")


def test_render_batches_returns_empty_when_no_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """No records → no batches. Caller (batch_runner) handles empty cleanly."""
    _patch_load_records(monkeypatch, [])

    batches = render_batches_for(
        fact="has_conditional_logic", state="AZ", lens="spine"
    )
    assert batches == []
