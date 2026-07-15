"""Anthropic Messages Batch API orchestration.

Two operations:

- ``submit_run_all`` — render every (state, lens, fact, entity) prompt
  the sync ``run_all`` would call, filter cache hits, build
  ``BatchRequestSpec`` rows for the misses, gate on estimated cost,
  chunk into ≤``DEFAULT_CHUNK_SIZE`` per batch, submit each chunk via
  ``BatchClient``, and write one ``BatchManifest`` per submitted batch.

- ``collect_batch`` — fetch results for one ``batch_id`` and write
  successful payloads through ``Cache.put``. Refuses to collect if
  ``prompt_version`` or ``model`` has drifted from the manifest's
  recorded values (cache keys would no longer align). Failed items
  are logged and counted; the next sync ``run_all`` re-runs them.

The cache is the seam. Submit only writes manifests; collect only
writes cache. The actual artifacts (sidecars under
``data/out/scoring/phase_a/``) come from a normal sync ``run_all``
afterwards, which finds every entry already cached → ~$0 spend on
that pass.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

from src.score.batch_client import BatchClient, BatchRequestSpec
from src.score.batch_manifest import (
    BatchItem,
    BatchManifest,
    read_manifest,
    write_manifest,
)
from src.score.cache import Cache, cache_key
from src.score.client import DEFAULT_MODEL, usd_for_batch
from src.score.estimate import OUTPUT_RATIO_ANCHOR, _chars_over_four
from src.score.extract import (
    PROMPT_VERSION,
    SUPPORTED_FACTS,
    _canonical_prompt,
    _load_spine_for,
    render_batches_for,
)
from src.score.runner import phase_b_facts_for_lens

_LOGGER = logging.getLogger(__name__)


# Anthropic's published per-batch ceiling. Bigger jobs are split across
# multiple batches by ``submit_run_all``.
DEFAULT_CHUNK_SIZE = 10_000


class BatchCostGateError(RuntimeError):
    """Raised by ``submit_run_all`` when estimated cost > max_cost_usd.

    The operator must explicitly re-invoke with ``confirm=True`` (CLI
    ``--yes``) to proceed. This is the one explicit confirmation gate
    before the wallet opens — once a batch is submitted, Anthropic
    bills regardless of whether the operator changes their mind.
    """


@dataclass(frozen=True)
class CollectSummary:
    """Outcome of one ``collect_batch`` call."""

    batch_id: str
    succeeded_count: int
    cache_writes: int
    parse_failed_count: int
    errored_count: int
    canceled_count: int
    expired_count: int
    actual_cost_usd: float


# ---------------------------------------------------------------------------
# Plan / submit
# ---------------------------------------------------------------------------


def _plan_pairs(
    *,
    states: list[str],
    lens: str,
    facts: list[str] | None,
    limit: int | None,
    elements_path: Path | None,
    model: str,
    prompt_version: str,
    cache_root: Path | None,
) -> list[tuple[BatchRequestSpec, BatchItem]]:
    """Render every cache-miss (state, fact, entity) and pair specs+items.

    Returned list is in deterministic order (state outer in input
    order, fact inner in ``phase_b_facts_for_lens`` / caller-supplied
    order, entity inner in render order). Items already present in
    the cache are skipped — the batch carries only what would
    actually cost money.
    """
    cache = Cache(model, prompt_version, root=cache_root)

    if facts is None:
        roster = list(phase_b_facts_for_lens(lens))
    else:
        roster = list(facts)
    # Filter to LLM facts only — deterministic facts don't go through
    # the LLM at all (``deterministic.py`` short-circuit), so they
    # carry no cache misses to submit.
    llm_facts = [f for f in roster if f in SUPPORTED_FACTS]

    pairs: list[tuple[BatchRequestSpec, BatchItem]] = []
    for state in states:
        state_norm = state.upper()
        # Load the spine once per state so spine-lens batches reconstruct
        # the legacy {domain} prompt label (issue #184) — keeps the batch
        # path's cache keys byte-identical to the sync path.
        spine = _load_spine_for(state_norm) if lens == "spine" else None
        for fact in llm_facts:
            batches = render_batches_for(
                fact=fact,
                state=state_norm,
                lens=lens,
                limit=limit,
                elements_path=elements_path,
                spine=spine,
            )
            for entity, group_records, system_text, user_text in batches:
                canonical = _canonical_prompt(system_text, user_text)
                key = cache_key(canonical, model, prompt_version)
                if cache.get(key) is not None:
                    # Already cached from a prior sync run or a
                    # previous batch collect — skip submission.
                    continue
                spec = BatchRequestSpec(
                    custom_id=key,
                    system_text=system_text,
                    user_text=user_text,
                )
                tokens_estimate = _chars_over_four(system_text, user_text, model)
                item = BatchItem(
                    custom_id=key,
                    state=state_norm,
                    lens=lens,
                    fact=fact,
                    entity=entity,
                    record_count=len(group_records),
                    estimated_tokens_in=tokens_estimate,
                )
                pairs.append((spec, item))
    return pairs


def _estimate_cost_usd(items: list[BatchItem], *, model: str) -> float:
    """Sum batch-tier USD over estimated input + estimated output tokens.

    Output tokens are estimated via ``OUTPUT_RATIO_ANCHOR`` (18.6%
    anchor, same as ``src.score.estimate``). The estimate's ±40%
    band is not propagated here — the cost gate is binary (exceed or
    not), so the point estimate is sufficient. Operators who want a
    band can run ``mc score estimate`` separately first.
    """
    total_in = sum(it.estimated_tokens_in for it in items)
    total_out_estimate = math.ceil(total_in * OUTPUT_RATIO_ANCHOR)
    return usd_for_batch(model, tokens_in=total_in, tokens_out=total_out_estimate)


def submit_run_all(
    *,
    states: list[str],
    lens: str,
    facts: list[str] | None = None,
    limit: int | None = None,
    model: str = DEFAULT_MODEL,
    prompt_version: str = PROMPT_VERSION,
    elements_path: Path | None = None,
    cache_root: Path | None = None,
    manifest_root: Path | None = None,
    max_cost_usd: float = 10.0,
    confirm: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    client: BatchClient | None = None,
) -> list[BatchManifest]:
    """Submit cache-miss prompts for ``run_all`` to the Batch API.

    Returns one ``BatchManifest`` per submitted batch (multiple if
    the cache-miss count exceeds ``chunk_size`` — Anthropic's 10,000-
    requests-per-batch ceiling). Returns ``[]`` (no submission, no
    manifest) when every prompt is already cached.

    Raises ``BatchCostGateError`` if the estimated cost exceeds
    ``max_cost_usd`` and ``confirm`` is False — operators must opt
    in to spend above the gate.

    ``client`` is injected for tests; production callers leave it
    None and a fresh ``BatchClient`` is constructed.
    """
    pairs = _plan_pairs(
        states=[s.upper() for s in states],
        lens=lens,
        facts=facts,
        limit=limit,
        elements_path=elements_path,
        model=model,
        prompt_version=prompt_version,
        cache_root=cache_root,
    )

    if not pairs:
        # All cache hits — nothing to submit. Caller can move
        # straight to a sync ``run_all`` for the (free) sidecar
        # build pass.
        return []

    items_only = [item for _spec, item in pairs]
    estimated_total = _estimate_cost_usd(items_only, model=model)

    if estimated_total > max_cost_usd and not confirm:
        raise BatchCostGateError(
            f"estimated batch cost ${estimated_total:.2f} exceeds "
            f"--max-cost ${max_cost_usd:.2f} ({len(pairs)} requests). "
            f"Re-run with --yes to confirm, or raise --max-cost."
        )

    if client is None:
        client = BatchClient(model=model)

    manifests: list[BatchManifest] = []
    for chunk_start in range(0, len(pairs), chunk_size):
        chunk = pairs[chunk_start:chunk_start + chunk_size]
        chunk_specs = [spec for spec, _item in chunk]
        chunk_items = [item for _spec, item in chunk]
        chunk_estimate = _estimate_cost_usd(chunk_items, model=model)

        result = client.submit(chunk_specs)

        manifest = BatchManifest(
            batch_id=result.batch_id,
            submitted_at=result.submitted_at,
            model=model,
            prompt_version=prompt_version,
            estimated_cost_usd=chunk_estimate,
            items=chunk_items,
        )
        write_manifest(manifest, root=manifest_root)
        manifests.append(manifest)
        _LOGGER.info(
            "submitted batch %s (%d items, est $%.2f)",
            result.batch_id, len(chunk_items), chunk_estimate,
        )

    return manifests


# ---------------------------------------------------------------------------
# Collect
# ---------------------------------------------------------------------------


def collect_batch(
    batch_id: str,
    *,
    model: str = DEFAULT_MODEL,
    prompt_version: str = PROMPT_VERSION,
    cache_root: Path | None = None,
    manifest_root: Path | None = None,
    client: BatchClient | None = None,
) -> CollectSummary:
    """Fetch results for ``batch_id`` and write through the cache.

    Refuses to proceed if the manifest's ``prompt_version`` or
    ``model`` differs from the live values — the cache keys recorded
    in the manifest would no longer match what the renderer would
    produce now, so writes would land in stale slots.

    Each succeeded item is written to cache via ``Cache.put`` with
    ``pricing_tier="batch"`` so cumulative-cost tooling can attribute
    spend by tier. Items with status ``parse_failed`` /
    ``errored`` / ``canceled`` / ``expired`` are counted but not
    cached — the next sync ``run_all`` re-runs them via the inline
    path so the operator gets per-item retry behaviour for free.
    """
    manifest = read_manifest(batch_id, root=manifest_root)

    if manifest.prompt_version != prompt_version:
        raise ValueError(
            f"prompt_version mismatch on batch {batch_id}: manifest "
            f"recorded {manifest.prompt_version!r} but live "
            f"PROMPT_VERSION is {prompt_version!r}. The cache keys in "
            f"the manifest no longer match what the renderer would "
            f"produce now — re-run submit on the new prompt_version."
        )
    if manifest.model != model:
        raise ValueError(
            f"model mismatch on batch {batch_id}: manifest recorded "
            f"{manifest.model!r} but live model is {model!r}. Same "
            f"key-alignment issue as prompt_version drift — re-submit "
            f"on the live model."
        )

    cache = Cache(model, prompt_version, root=cache_root)
    if client is None:
        client = BatchClient(model=model)

    counts = {
        "succeeded": 0,
        "parse_failed": 0,
        "errored": 0,
        "canceled": 0,
        "expired": 0,
    }
    cache_writes = 0
    actual_cost = 0.0

    for result in client.iter_results(batch_id):
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.status != "succeeded":
            if result.error:
                _LOGGER.warning(
                    "batch %s item %s status=%s error=%s",
                    batch_id, result.custom_id, result.status, result.error,
                )
            continue
        # A sync run between submit and collect could have populated
        # the same key. Don't overwrite — the additive cache contract
        # (``cache.py`` module docstring) means first write wins, but
        # we save a redundant put either way.
        if cache.get(result.custom_id) is not None:
            continue
        item_usd = usd_for_batch(
            model, tokens_in=result.tokens_in, tokens_out=result.tokens_out
        )
        actual_cost += item_usd
        cache.put(
            result.custom_id,
            result.payload,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            usd=item_usd,
            cache_read_tokens=0,
            pricing_tier="batch",
        )
        cache_writes += 1

    return CollectSummary(
        batch_id=batch_id,
        succeeded_count=counts["succeeded"],
        cache_writes=cache_writes,
        parse_failed_count=counts["parse_failed"],
        errored_count=counts["errored"],
        canceled_count=counts["canceled"],
        expired_count=counts["expired"],
        actual_cost_usd=actual_cost,
    )
