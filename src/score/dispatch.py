"""Shared LLM dispatch plumbing (issue #213 item 3).

Two things live here:

- :func:`write_jsonl_artifact` — the atomic header+rows JSONL writer
  that was previously triplicated verbatim across ``extract.py``,
  ``deterministic.py``, and ``peer_gap.py``.
- :func:`dispatch_batches` — the cache-hit → cost-cap → call →
  validate → ``cache.put`` skeleton that ``extract.run`` and
  ``peer_gap.run`` each carried as a ~120-line copy. The engine owns
  ONLY the transport mechanics; everything observable per item
  (telemetry counters, row building, streaming artifact rewrites,
  progress callbacks) stays with the caller via the ``on_cached`` /
  ``on_fresh`` hooks so each runner's exact behavior — including its
  ordering of write-vs-callback — is preserved.

Neither ``batch_runner`` (a submit/collect flow with no dispatch loop)
nor ``gap_extract`` (delegates to ``extract.run``) carries this
skeleton; the two genuine copies were extract + peer_gap.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.score.cache import Cache, cache_key
from src.score.client import LLMClient, LLMResponse
from src.score.schema import CostCapExceeded, ScoringSchemaError


def write_jsonl_artifact(
    path: Path,
    header: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    """Rewrite a header+rows JSONL artifact atomically.

    tmp + fsync + ``os.replace`` — called after every batch (streaming
    checkpoint), so a plain "w" rewrite left a SIGKILL window where the
    file was truncated mid-write: an opaque JSONDecodeError at aggregate
    time (issue #212 item 7). Same discipline as ``cache.put`` /
    ``batch_manifest``. Ensures the parent directory exists (the
    peer_gap/deterministic copies did; extract's callers pre-created it
    — the mkdir is a no-op there).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(header, sort_keys=True, ensure_ascii=False) + "\n")
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


@dataclass(frozen=True)
class DispatchItem:
    """One pre-rendered prompt the engine may need to send.

    ``label`` is the human-facing unit name (entity for extract,
    slot_key for peer-gap). ``context`` is opaque caller payload
    (extract's record group / peer-gap's bundle) threaded back through
    the hooks and ``validate_fn``.
    """

    label: str
    system_text: str
    user_text: str
    context: Any = None


@dataclass
class DispatchState:
    """Mutable engine-side state the caller's header builder may read.

    ``processed_count`` counts cached + fresh completions — it feeds
    the cost-cap message, matching both original loops (extract's
    ``entities_processed`` and peer-gap's ``scored_count`` both counted
    cached hits too).
    """

    total_usd: float = 0.0
    processed_count: int = 0
    cost_cap_hit: bool = False
    schema_error: ScoringSchemaError | None = None


def dispatch_batches(
    items: list[DispatchItem],
    *,
    state: DispatchState,
    cache: Cache,
    client: LLMClient,
    model: str,
    prompt_version: str,
    cost_cap: float,
    cost_cap_unit: str,
    canonical_prompt: Callable[[str, str], str],
    validate_fn: Callable[[Any, DispatchItem], Any],
    on_cached: Callable[[int, DispatchItem, Any, dict[str, Any]], None],
    on_fresh: Callable[[int, DispatchItem, Any, LLMResponse], None],
    persist: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Run the shared dispatch loop; return the final header dict.

    Per item, in order (behavior-identical to the pre-extraction loops
    in ``extract.run`` and ``peer_gap.run``):

    1. cache lookup by ``cache_key(canonical_prompt(...), model,
       prompt_version)`` — hit: validate the cached payload (a raise
       here propagates to the abort path WITHOUT recording
       ``schema_error``, as before), then hand off to ``on_cached``;
    2. cost cap: if the engine's running spend has reached ``cost_cap``
       BEFORE the call, set ``cost_cap_hit`` and raise
       :class:`CostCapExceeded` with the original message shape
       (``cost_cap_unit`` = "entities" / "slots");
    3. live call → ``validate_fn`` (a ``ScoringSchemaError`` here IS
       recorded on ``state.schema_error`` before re-raising) →
       ``cache.put`` (raw components incl. ``cache_read_tokens`` —
       the post-#221 accounting) → hand off to ``on_fresh``.

    Any exception (schema error, cost cap, network error, SIGINT)
    triggers ``persist("aborted")`` + re-raise. A clean finish calls
    ``persist("complete")``.

    ``state`` is constructed by the CALLER (before its header closure
    is defined) so the closure can read ``cost_cap_hit`` /
    ``schema_error`` at persist time. Caller-owned state also keeps the
    engine thread-safe — ``gap_extract.run_all`` drives ``extract.run``
    from a per-state ``ThreadPoolExecutor``. ``persist(status)`` builds
    the caller's header, writes the artifact, and returns the header
    dict.
    """
    try:
        for idx, item in enumerate(items):
            canonical = canonical_prompt(item.system_text, item.user_text)
            key = cache_key(canonical, model, prompt_version)
            cached = cache.get(key)
            if cached is not None:
                payload = validate_fn(cached["response"], item)
                state.processed_count += 1
                on_cached(idx, item, payload, cached)
                continue

            if state.total_usd >= cost_cap:
                state.cost_cap_hit = True
                raise CostCapExceeded(
                    f"cost cap ${cost_cap:.2f} reached after "
                    f"{state.processed_count} {cost_cap_unit} "
                    f"(spent ${state.total_usd:.4f})"
                )

            response: LLMResponse = client.call(
                system_text=item.system_text,
                user_text=item.user_text,
            )
            try:
                payload = validate_fn(response.payload, item)
            except ScoringSchemaError as exc:
                state.schema_error = exc
                raise
            cache.put(
                key,
                response.payload,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                usd=response.usd,
                cache_read_tokens=response.cache_read_tokens,
            )
            state.total_usd += response.usd
            state.processed_count += 1
            on_fresh(idx, item, payload, response)
        final_status = "complete"
    except BaseException:
        # Any exit via exception: persist whatever we have with
        # status="aborted", re-raise.
        persist("aborted")
        raise
    return persist(final_status)
