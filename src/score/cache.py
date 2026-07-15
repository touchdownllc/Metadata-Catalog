"""On-disk JSONL prompt cache for scoring extraction.

Key = SHA-256 over ``prompt_text + "\\0" + model_id + "\\0" + prompt_version``.
Storage: one JSONL line per entry under
``data/cache/scoring/{model}/{prompt_version}/{key_prefix}.jsonl`` where
``key_prefix`` is the first two hex chars of the SHA (256-directory fan-out
cap; keeps any single file small). Mode-agnostic — API, inline, and any
future subagent-dispatch mode all populate and read the same cache.

The cache is *additive-only*: a stale entry is never rewritten. If the
prompt template or rules change, bump ``prompt_version`` rather than
editing existing cache files.

The cache IS the checkpoint
---------------------------

Phase B commit 2 formalized this: the JSONL artifact under
``data/out/scoring/phase_a/`` is a **view** over the cache, regenerable
from it at $0. A SIGKILL mid-run leaves the cache fully populated up to
the last completed batch. On restart, the extractor re-renders every
batch, hashes each prompt, hits the cache, and rebuilds the artifact
identically. This means:

- There is **no separate checkpoint file** — the cache provides crash
  safety for free.
- **Invalidation rules:** bump ``prompt_version`` for any prompt or
  rules change; the cache key also folds in ``model_id`` so model
  swaps invalidate automatically. These two levers are the only way to
  force a re-spend.
- **Never edit a cache entry in place.** Even fixing a bug — bump the
  version instead. The additive contract is what lets historical runs
  replay byte-identically.

Operators who change a ``.md`` prompt file without bumping
``PROMPT_VERSION`` in ``extract.py`` will silently reuse the old cache.
See ``docs/archive/next-session/next-session-scoring-phase-b.md § "Cache-vs-prompt-version
drift"`` risk for the tripwire plan.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.utils.paths import scoring_cache_dir

_LOGGER = logging.getLogger(__name__)


# Per-shard locks for in-process writers (issue #73's parallel state
# extracts). Since issue #212 item 7 the write path is a single
# O_APPEND ``write()`` per entry, which the kernel already serializes —
# including across processes, where this map never reached — so the
# lock is belt-and-suspenders keeping each entry's write+fsync cycle
# tidy rather than load-bearing for correctness.
_SHARD_LOCKS_GLOBAL: dict[Path, threading.Lock] = {}
_SHARD_LOCKS_GLOBAL_LOCK = threading.Lock()


def _get_shard_lock(path: Path) -> threading.Lock:
    """Return the lock guarding writes to ``path`` (256-way shard file)."""
    with _SHARD_LOCKS_GLOBAL_LOCK:
        lock = _SHARD_LOCKS_GLOBAL.get(path)
        if lock is None:
            lock = threading.Lock()
            _SHARD_LOCKS_GLOBAL[path] = lock
        return lock


def cache_key(prompt_text: str, model_id: str, prompt_version: str) -> str:
    """Return the SHA-256 hex digest used to key cache entries.

    ``state`` / ``entity`` are NOT separately hashed — they are implicit
    in ``prompt_text``. Extra key components mask cache hits across
    re-renders of the same prompt for the same entity.
    """
    h = hashlib.sha256()
    h.update(prompt_text.encode("utf-8"))
    h.update(b"\0")
    h.update(model_id.encode("utf-8"))
    h.update(b"\0")
    h.update(prompt_version.encode("utf-8"))
    return h.hexdigest()


class Cache:
    """Append-only JSONL cache partitioned by key prefix.

    Reads scan the matching prefix file (small; ~1/256th of total).
    Writes are single-line ``O_APPEND`` appends + fsync (issue #212
    item 7): the kernel serializes concurrent appenders — including
    across PROCESSES, which the old read-shard-then-``os.replace``
    cycle did not — so no writer can clobber another's paid entry, and
    existing bytes are physically never rewritten (the additive-only
    contract, now enforced by construction). A crash mid-write can
    leave one partial trailing line; ``get()`` skips malformed lines,
    so the worst case is re-spending that single call.
    """

    def __init__(self, model: str, prompt_version: str, *, root: Path | None = None) -> None:
        self.model = model
        self.prompt_version = prompt_version
        self._root = root if root is not None else scoring_cache_dir(model, prompt_version)

    def _path_for(self, key: str) -> Path:
        return self._root / f"{key[:2]}.jsonl"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path_for(key)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    # A single corrupt line (e.g. an interrupted write
                    # that dropped a partial JSON object) must not
                    # blow up a lookup for an unrelated key — the
                    # cache is a multi-entry JSONL file. Log and skip.
                    _LOGGER.warning(
                        "skipping malformed cache line %s:%d (%s)",
                        path, line_no, exc,
                    )
                    continue
                if entry.get("key") == key:
                    return entry
        return None

    def put(
        self,
        key: str,
        response: Any,
        *,
        tokens_in: int,
        tokens_out: int,
        usd: float,
        cache_read_tokens: int,
        pricing_tier: str = "sync",
    ) -> dict[str, Any]:
        """Append a new cache entry and return it.

        Duplicate keys are a no-op at the storage level (we still append
        once) — the first write wins on read. Callers should check via
        ``get()`` before calling ``put()``.

        ``pricing_tier`` (default ``"sync"``) marks whether the entry was
        produced by an inline ``messages.create`` call (sync, default)
        or by collecting an Anthropic Messages Batch API result
        (``"batch"``). Cache *behaviour* is identical either way — the
        tag is purely informational, surfaced for cost-rollup tooling
        that wants to attribute cumulative USD by tier.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        entry = {
            "key": key,
            "response": response,
            "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out),
            "usd": float(usd),
            "cache_read_tokens": int(cache_read_tokens),
            "cached_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "model": self.model,
            "prompt_version": self.prompt_version,
            "pricing_tier": pricing_tier,
        }
        path = self._path_for(key)
        # O_APPEND single-line append + fsync (issue #212 item 7). The
        # previous read-whole-shard → stage-tmp → os.replace cycle was
        # atomic against crashes but only THREAD-safe: two *processes*
        # writing the same shard (two terminals, or `poc3 publish`
        # beside a manual `score extract`) could interleave
        # read/replace and silently drop one writer's PAID entry. With
        # O_APPEND the kernel serializes writers — nobody's entry is
        # ever clobbered — and the O(shard²) rewrite cost is gone. The
        # crash story changes shape but not safety: a die mid-write can
        # leave one partial TRAILING line (never the mid-file `Mere`
        # bleed of 2026-04-24, which came from a buffered append with
        # no fsync), and ``get()`` skips malformed lines by design, so
        # the worst case is re-spending that one call.
        #
        # The per-shard threading lock stays as in-process belt-and-
        # suspenders (single write() syscall per entry either way).
        new_line = (
            json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        with _get_shard_lock(path):
            # Self-heal a crash-left partial tail: if the shard doesn't
            # end in a newline, appending directly would glue this entry
            # onto the malformed line and ``get()`` would skip BOTH —
            # every later entry in the shard becomes unreadable (the
            # exact black-hole shape of the 2026-04-24 `Mere` bleed).
            # A leading newline isolates the damage to the one partial
            # line; spurious empty lines are skipped by ``get()``.
            if path.exists() and path.stat().st_size > 0:
                with path.open("rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        new_line = b"\n" + new_line
            fd = os.open(
                path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644
            )
            try:
                view = memoryview(new_line)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        return entry
