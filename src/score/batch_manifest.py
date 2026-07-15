"""Anthropic Batch API submission manifests.

A manifest is the audit record for one ``messages.batches.create`` call.
It captures the (model, prompt_version) the batch was rendered against
plus the {custom_id → (state, lens, fact, entity)} mapping so
``score batches collect <batch_id>`` can:

1. Reject collection if live ``PROMPT_VERSION`` has bumped since
   submission (cache keys would no longer match what the new code
   expects).
2. Surface "what was this batch for?" without re-running the renderer.
3. Detect partial-failure follow-ups (the failed item's row in the
   manifest tells the operator which (state, lens, fact, entity) needs
   a sync re-run).

Storage: ``data/cache/scoring/batches/<batch_id>.json`` — one file per
submission. Atomic write (stage to ``.tmp``, fsync, ``os.replace``) so
a SIGKILL between Anthropic returning ``batch_id`` and the manifest
landing on disk leaves either the prior state or the post-write state,
never partway between.

The manifest does NOT include rendered prompt text — that would
duplicate the cache. ``custom_id`` is the SHA-256 cache key, so the
prompt can always be re-rendered deterministically via
``score.extract.render_prompt`` if needed for audit.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.utils.paths import scoring_batches_dir


@dataclass(frozen=True)
class BatchItem:
    """One (state, lens, fact, entity) request inside a submitted batch.

    ``custom_id`` is the SHA-256 cache key (``cache.cache_key(...)``) —
    deterministic, so the collect path can hash the rendered prompt
    again to verify alignment without trusting the manifest blindly.
    ``estimated_tokens_in`` is an estimate from the renderer-time token
    count; the actual usage comes back on the result and is what
    settles the bill.
    """

    custom_id: str
    state: str
    lens: str
    fact: str
    entity: str
    record_count: int
    estimated_tokens_in: int


@dataclass
class BatchManifest:
    """One submitted Anthropic Messages Batch.

    Frozen-after-write semantics: the manifest is never updated post-
    submission. A second submit (e.g. for the items that errored on
    the first batch) writes a *new* manifest with a new ``batch_id``.
    """

    batch_id: str
    submitted_at: str
    model: str
    prompt_version: str
    estimated_cost_usd: float
    items: list[BatchItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "submitted_at": self.submitted_at,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "items": [asdict(it) for it in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BatchManifest":
        items = [BatchItem(**it) for it in data.get("items", [])]
        return cls(
            batch_id=data["batch_id"],
            submitted_at=data["submitted_at"],
            model=data["model"],
            prompt_version=data["prompt_version"],
            estimated_cost_usd=float(data.get("estimated_cost_usd", 0.0)),
            items=items,
        )


def manifest_path(batch_id: str, *, root: Path | None = None) -> Path:
    """Return the path for ``batch_id``'s manifest under ``root``."""
    base = root if root is not None else scoring_batches_dir()
    return base / f"{batch_id}.json"


def write_manifest(manifest: BatchManifest, *, root: Path | None = None) -> Path:
    """Atomically write ``manifest`` to ``data/cache/scoring/batches/``.

    Stage-to-tmp + ``os.replace`` mirrors ``Cache.put`` (cache.py:115)
    so the same crash-safety guarantee applies — a SIGKILL mid-write
    leaves the manifest at its pre- or post-write state, never partway
    through. Returns the resolved path.
    """
    target = manifest_path(manifest.batch_id, root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, target)
    return target


def read_manifest(batch_id: str, *, root: Path | None = None) -> BatchManifest:
    """Read and parse the manifest for ``batch_id``.

    Raises ``FileNotFoundError`` if the manifest doesn't exist (typical
    operator mistake: typo'd batch_id, or `submit` failed before the
    manifest landed). The error message includes the resolved path so
    the operator can confirm where they expected it to be.
    """
    target = manifest_path(batch_id, root=root)
    if not target.exists():
        raise FileNotFoundError(
            f"no batch manifest at {target} — confirm batch_id and re-check "
            f"`mc score batches list`"
        )
    data = json.loads(target.read_text(encoding="utf-8"))
    return BatchManifest.from_dict(data)


def list_manifests(*, root: Path | None = None) -> list[BatchManifest]:
    """Return all manifests, sorted by ``submitted_at`` ascending.

    Returns ``[]`` if the manifest directory doesn't exist (no batches
    ever submitted) — same shape so callers don't need a separate
    "is the dir there?" probe.
    """
    base = root if root is not None else scoring_batches_dir()
    if not base.exists():
        return []
    manifests: list[BatchManifest] = []
    for path in sorted(base.glob("*.json")):
        # Skip in-progress ``.tmp`` files left by a SIGKILL between
        # tmp-stage and rename. They're not valid JSON manifests yet.
        if path.suffix == ".tmp":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        manifests.append(BatchManifest.from_dict(data))
    manifests.sort(key=lambda m: m.submitted_at)
    return manifests
