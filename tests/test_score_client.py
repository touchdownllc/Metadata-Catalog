"""Unit tests for ``src.score.client`` pricing helpers.

Focuses on the batch-tier pricing surface introduced for the Anthropic
Messages Batch API path. Sync-tier pricing is exercised indirectly by
``test_score_estimate.py`` and ``test_score_phase_a.py`` cost rollups —
this file isolates the batch-only invariants so a regression in either
table fails fast without needing a full Phase A replay.
"""

from __future__ import annotations

import pytest

from src.score.client import (
    _PRICING_USD_PER_MTOK,
    _PRICING_USD_PER_MTOK_BATCH,
    usd_for_batch,
)


def test_batch_pricing_covers_every_sync_model() -> None:
    """Each model present in the sync table must exist in batch table."""
    missing = set(_PRICING_USD_PER_MTOK) - set(_PRICING_USD_PER_MTOK_BATCH)
    assert not missing, (
        f"batch pricing table missing entries for sync models: {sorted(missing)}"
    )


@pytest.mark.parametrize("model", sorted(_PRICING_USD_PER_MTOK))
def test_batch_input_output_is_half_of_sync(model: str) -> None:
    """Anthropic publishes batch as a flat 50% discount on input + output.

    If sync pricing changes (new model, repricing), this test fails until
    the batch table is updated to match — the goal is to keep the two
    tables synchronized rather than let them drift independently.
    """
    sync_in, sync_out, _cw, _cr = _PRICING_USD_PER_MTOK[model]
    batch_in, batch_out = _PRICING_USD_PER_MTOK_BATCH[model]
    assert batch_in == pytest.approx(sync_in / 2)
    assert batch_out == pytest.approx(sync_out / 2)


def test_usd_for_batch_known_model_matches_table() -> None:
    """USD must derive from ``_PRICING_USD_PER_MTOK_BATCH`` — no dup math.

    Mirrors the pattern in ``test_score_estimate.py:test_usd_for_cold_run``
    so the batch helper has the same single-source-of-truth contract.
    """
    inp, out = _PRICING_USD_PER_MTOK_BATCH["claude-sonnet-4-6"]
    expected = (10_000 * inp + 1_000 * out) / 1_000_000
    assert usd_for_batch(
        "claude-sonnet-4-6", tokens_in=10_000, tokens_out=1_000
    ) == pytest.approx(expected)


def test_usd_for_batch_unknown_model_falls_back_to_sonnet_tier() -> None:
    """Unknown model IDs over-estimate at Sonnet-tier batch pricing.

    Same conservative posture as the sync ``_price_for`` fallback —
    better to over-charge a billing line than to under-charge it.
    """
    sonnet_in, sonnet_out = _PRICING_USD_PER_MTOK_BATCH["claude-sonnet-4-6"]
    expected = (5_000 * sonnet_in + 500 * sonnet_out) / 1_000_000
    assert usd_for_batch(
        "claude-fake-9", tokens_in=5_000, tokens_out=500
    ) == pytest.approx(expected)


def test_usd_for_batch_zero_tokens_is_zero() -> None:
    assert usd_for_batch("claude-sonnet-4-6", tokens_in=0, tokens_out=0) == 0.0


# ---------------------------------------------------------------------------
# Sync-tier cost accounting (issue #212 item 4)
# ---------------------------------------------------------------------------


def test_usd_for_bills_raw_components_at_list_price() -> None:
    """``_usd_for`` takes the API's RAW usage components — no
    reverse-engineering of the uncached portion. The exact shape the
    old heuristic mis-billed: uncached input larger than the cached
    system prologue."""
    from src.score.client import _usd_for

    usd = _usd_for(
        "claude-sonnet-4-6",
        uncached_in=5000,
        tokens_out=1000,
        cache_read_tokens=3000,
        cache_creation_tokens=2000,
    )
    expected = (
        5000 * 3.0 + 1000 * 15.0 + 2000 * 3.75 + 3000 * 0.30
    ) / 1_000_000
    assert usd == pytest.approx(expected)


def test_call_bills_mixed_cache_and_input_correctly(monkeypatch) -> None:
    """(input=5000, cache_read=3000): the retired "already folded in"
    heuristic treated input_tokens as containing the cache tiers and
    double-subtracted them — billing 3000 uncached tokens at the
    $0.30/M cache-read tier instead of $3/M. The Messages API's
    ``usage.input_tokens`` EXCLUDES cache tiers (pinned here)."""
    from types import SimpleNamespace

    from src.score.client import AnthropicClient

    client = AnthropicClient(model="claude-sonnet-4-6", api_key="test-key")
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="[]")],
        usage=SimpleNamespace(
            input_tokens=5000,
            output_tokens=1000,
            cache_read_input_tokens=3000,
            cache_creation_input_tokens=0,
        ),
        model="claude-sonnet-4-6",
    )
    monkeypatch.setattr(
        client._client.messages, "create", lambda **kwargs: resp
    )
    out = client.call(system_text="sys", user_text="user")
    # Total input telemetry = plain sum of the tiers.
    assert out.tokens_in == 8000
    assert out.cache_read_tokens == 3000
    expected = (5000 * 3.0 + 1000 * 15.0 + 3000 * 0.30) / 1_000_000
    assert out.usd == pytest.approx(expected)
    # The old heuristic's answer, for the record: strictly less.
    undercount = ((5000 - 3000) * 3.0 + 1000 * 15.0 + 3000 * 0.30) / 1_000_000
    assert out.usd > undercount


def test_call_first_ttl_window_bills_cache_creation(monkeypatch) -> None:
    """First call of a TTL window: cache_creation > 0, cache_read = 0.
    The old guard ignored cache_creation entirely, mispricing it the
    same way."""
    from types import SimpleNamespace

    from src.score.client import AnthropicClient

    client = AnthropicClient(model="claude-sonnet-4-6", api_key="test-key")
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="[]")],
        usage=SimpleNamespace(
            input_tokens=4000,
            output_tokens=500,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=6000,
        ),
        model="claude-sonnet-4-6",
    )
    monkeypatch.setattr(
        client._client.messages, "create", lambda **kwargs: resp
    )
    out = client.call(system_text="sys", user_text="user")
    assert out.tokens_in == 10000
    expected = (4000 * 3.0 + 500 * 15.0 + 6000 * 3.75) / 1_000_000
    assert out.usd == pytest.approx(expected)
