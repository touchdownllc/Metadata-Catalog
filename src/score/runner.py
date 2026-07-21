"""Phase B multi-fact runner — loops (state, fact) pairs.

Calls ``extract.run()`` for each pair. **Skip-if-complete:** if a pair's
artifact header already reports ``status="complete"`` with
``scored_count == record_count``, the pair is skipped. This makes the
runner idempotent — a rerun after a crash (or a planned kill) only
does unfinished work.

The cache IS the checkpoint. Cache keys hash ``(prompt_text, model_id,
prompt_version)``, so a rerun within the same ``prompt_version`` reads
every prior batch from cache at $0 USD. Bumping ``prompt_version``
invalidates the cache by design; changing the model ID does the same
automatically. See ``cache.py`` module docstring.

Runner manifest: every run writes
``data/out/scoring/phase_b/run_manifest.json`` with per-pair status
(``complete`` / ``skipped`` / ``partial`` / ``failed``) plus cost
telemetry. Stream-written after every pair, so a SIGKILL in the middle
of the outer loop preserves the manifest up to that point too.

Global ``--cost-cap`` behaviour: the runner tracks running USD across
all pairs. Each ``extract.run()`` invocation inherits
``max(cost_cap - spent_so_far, 0)`` as its individual cap. A pair whose
individual cap is 0 exits with ``cost_cap_hit=True`` immediately and
the remaining pairs are skipped with status=``skipped_cost_cap``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.score.client import DEFAULT_MODEL
from src.score.azure_client import build_runtime_client
from src.score.deterministic import (
    LENS_INDEPENDENT_FACTS,
    SHARED_CONTEXT_FACTS,
    SOURCE_DETERMINISTIC_FACTS,
)
from src.score.extract import PROMPT_VERSION, SUPPORTED_FACTS, run as run_extract
from src.score.rules import (
    LENS_OBSERVABILITY_FACTS,
    PRODUCTIZATION_SIGNAL_FACTS,
    SOURCE_RULE_INPUTS,
    SPINE_RULE_INPUTS,
)
from src.score.schema import CostCapExceeded, ScoringSchemaError
from src.utils.paths import (
    scoring_phase_a_artifact_path,
    scoring_phase_b_dir,
)

_LOGGER = logging.getLogger(__name__)

# Ordering manifest of every Phase B fact that could appear on either
# lens's roster — the union of lens-independent deterministic facts and
# every authored LLM prompt. Used by tests as a deterministic-order
# reference and by ``phase_b_facts_for_lens`` as the canonical ordering
# the filtered-by-lens roster inherits.
#
# **Do not import this directly as a ``--facts all`` source** — it carries
# both spine-only and source-only LLM facts. Use
# ``phase_b_facts_for_lens(lens)`` so source runs stop cold-spinning
# ``definition_is_implementable`` / ``required_when_stated`` /
# ``conditional_reporting_stated`` / ``populations_or_scope_stated`` (and
# symmetrically, spine runs stop touching source-only extension /
# narrowing / broadening prompts). The un-filtered roster was wasting
# ~$2.50 per state per lens at Phase B prices (post-merge 2026-04-24).
PHASE_B_FACTS: tuple[str, ...] = LENS_INDEPENDENT_FACTS + tuple(SUPPORTED_FACTS)

# Deterministic spine-backed facts both lenses' rule stages consult. Kept
# outside ``LENS_INDEPENDENT_FACTS`` (those are record-only facts that
# don't need a ``FactContext``) and outside ``SOURCE_DETERMINISTIC_FACTS``
# (those are source-lens exclusive). Added to every lens's roster via
# ``phase_b_facts_for_lens``.
#
# Re-exported from ``deterministic.SHARED_CONTEXT_FACTS`` so the list
# stays in one place. ``is_natural_key`` (PR #17) + the four
# structural-depth count facts (Integration Profile Day 1) all live in
# that tuple.
_SHARED_CONTEXT_DETERMINISTIC_FACTS: tuple[str, ...] = SHARED_CONTEXT_FACTS

# LLM facts each lens's rule cascade actually consults — derived from
# ``SUPPORTED_FACTS ∩ *_RULE_INPUTS`` so authoring a new prompt with a
# matching rule-input entry picks it up automatically. Source-only
# prompts (``extension_is_necessary``, ``state_scope_delta``,
# etc.) drop off the spine roster, and spine-only prompts
# (``definition_is_implementable``, ``required_when_stated``,
# ``conditional_reporting_stated``, ``populations_or_scope_stated``)
# drop off the source roster. ``SUPPORTED_FACTS`` order is preserved so
# the runner's deterministic fanout stays stable.
_SPINE_LLM_RULE_FACTS: tuple[str, ...] = tuple(
    f for f in SUPPORTED_FACTS if f in SPINE_RULE_INPUTS
)
_SOURCE_LLM_RULE_FACTS: tuple[str, ...] = tuple(
    f for f in SUPPORTED_FACTS if f in SOURCE_RULE_INPUTS
)


def phase_b_facts_for_lens(lens: str) -> tuple[str, ...]:
    """Return the ``--facts all`` roster for ``lens``.

    The roster is composed of:

    - ``LENS_INDEPENDENT_FACTS`` — record-only deterministic facts both
      lenses' rule stages read.
    - The lens's LLM rule inputs (``SUPPORTED_FACTS ∩ *_RULE_INPUTS``) —
      source-only LLM prompts drop off the spine roster and vice versa.
      Running them on the wrong lens was wasting ~$2.50/state per
      ``score run-all`` before post-merge cleanup Task 2.
    - ``PRODUCTIZATION_SIGNAL_FACTS`` — orthogonal productization axis
      (currently ``integration_class``) surfaced as observability on
      both lenses via ``fact_provenance``.
    - ``LENS_OBSERVABILITY_FACTS[lens]`` — extracted-but-not-consumed
      facts for the lens (spine carries ``semantic_class`` so the spine
      workbook can show the alignment verdict even though spine's
      cascade doesn't score against it).
    - ``SHARED_CONTEXT_FACTS`` — deterministic spine-backed facts both
      lenses consume (``is_natural_key`` + the v2 structural-complexity
      axis).
    - ``SOURCE_DETERMINISTIC_FACTS`` — source-lens-exclusive
      deterministic facts (``element_name_matches_canonical``,
      ``naming_deviation_cosmetic``, ``extension_mirrors_core_pattern``).
      Skipped on spine.

    Entries are deduped in insertion order so overlap between the roster
    components stays stable and the runner's deterministic fanout is
    preserved.
    """
    if lens not in ("spine", "source"):
        raise ValueError(
            f"unknown lens {lens!r}; supported: 'spine', 'source'"
        )

    lens_llm = _SPINE_LLM_RULE_FACTS if lens == "spine" else _SOURCE_LLM_RULE_FACTS
    obs_facts = LENS_OBSERVABILITY_FACTS.get(lens, ())

    roster: list[str] = []
    seen: set[str] = set()

    def _extend(facts: tuple[str, ...]) -> None:
        for fact in facts:
            if fact in seen:
                continue
            roster.append(fact)
            seen.add(fact)

    _extend(LENS_INDEPENDENT_FACTS)
    _extend(lens_llm)
    _extend(PRODUCTIZATION_SIGNAL_FACTS)
    _extend(obs_facts)
    _extend(_SHARED_CONTEXT_DETERMINISTIC_FACTS)
    if lens == "source":
        _extend(SOURCE_DETERMINISTIC_FACTS)

    return tuple(roster)

# Canonical roster lives in src.states — re-exported here for the
# existing importers (back-compat).
from src.states import SUPPORTED_STATES  # noqa: E402,F401

RUN_MANIFEST_NAME = "run_manifest.json"


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class PairResult:
    state: str
    fact: str
    status: str  # "complete" | "skipped" | "skipped_cost_cap" | "partial" | "failed"
    record_count: int
    scored_count: int
    total_usd: float
    cache_hit_count: int
    downgrade_count: int
    error: str | None
    artifact: str


@dataclass
class RunManifest:
    started_at: str
    ended_at: str | None = None
    model: str = DEFAULT_MODEL
    lens: str = "spine"
    prompt_version: str = PROMPT_VERSION
    cost_cap: float = 0.0
    limit: int | None = None
    total_usd: float = 0.0
    pairs: list[PairResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "model": self.model,
            "lens": self.lens,
            "prompt_version": self.prompt_version,
            "cost_cap": self.cost_cap,
            "limit": self.limit,
            "total_usd": round(self.total_usd, 6),
            "pairs": [p.__dict__ for p in self.pairs],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_artifact_header(path: Path) -> dict[str, Any] | None:
    """Return the artifact header dict, or None if the file is missing/empty.

    Robust against zero-byte files and truncated first lines — both
    treated as "no header yet, needs a run." Malformed JSON is treated
    the same (we do not want to silently skip a pair whose artifact was
    corrupted mid-write).
    """
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return None
    if not first.strip():
        return None
    try:
        return json.loads(first)
    except json.JSONDecodeError:
        return None


def is_pair_complete(
    artifact_path: Path,
    *,
    lens: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    expected_record_count: int | None = None,
) -> bool:
    """Skip-if-complete predicate: the §5b contract.

    A pair counts as complete iff its artifact header has
    ``status="complete"`` AND ``scored_count == record_count``. Anything
    else (missing file, ``status="running"``, ``status="aborted"``,
    mismatched counts) returns False so the runner re-invokes the pair
    and lets the cache replay provide the idempotent shortcut.

    Issue #212 item 7: the predicate used to trust ONLY the artifact's
    own header — after ingest grew the record pool (the v26 leaf-borrow
    / v27 domain-expansion class), or under a different requested
    model/prompt_version/lens, ``run-all`` reported ``skipped`` on a
    stale artifact. The optional keyword checks compare the header
    against what THIS run would produce. ``model``/``prompt_version``
    are only compared on LLM artifacts (deterministic facts stamp
    ``model="deterministic"`` + their own det version).

    The record-count check is deliberately DIRECTIONAL: only pool
    GROWTH (``expected_record_count > record``) marks the pair stale —
    new rows are missing their facts and a re-run adds them (cache
    replay keeps existing rows ~free). Pool SHRINKAGE is NOT staleness:
    live-drilling this guard (2026-07-09) found the spine-lens
    artifacts carry rows from a pre-v26 WIDER extraction pool that
    today's ``load_phase_a_records`` filter no longer admits — those
    artifacts are load-bearing for the spine sidecars, and a re-run
    would rewrite them narrow and collapse spine coverage. Shrinkage is
    a methodology question (surfaced as a WARNING by ``run_all``), not
    a re-extraction trigger.
    """
    header = read_artifact_header(artifact_path)
    if header is None:
        return False
    if header.get("status") != "complete":
        return False
    scored = header.get("scored_count")
    record = header.get("record_count")
    if scored is None or record is None:
        return False
    if scored != record or scored <= 0:
        return False
    if lens is not None and header.get("lens") != lens:
        return False
    if header.get("model") != "deterministic":
        if model is not None and header.get("model") != model:
            return False
        if (
            prompt_version is not None
            and header.get("prompt_version") != prompt_version
        ):
            return False
    if (
        expected_record_count is not None
        and expected_record_count > int(record)
    ):
        return False
    return True


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


ProgressCallback = Callable[[PairResult], None]


CheckpointCallback = Callable[[str, "RunManifest"], bool]


def run_all(
    *,
    states: list[str],
    facts: list[str],
    lens: str = "spine",
    limit: int | None = None,
    model: str = DEFAULT_MODEL,
    cost_cap: float = 50.0,
    prompt_version: str = PROMPT_VERSION,
    manifest_path: Path | None = None,
    out_dir: Path | None = None,
    cache_root: Path | None = None,
    progress: ProgressCallback | None = None,
    extract_fn: Callable[..., dict[str, Any]] = run_extract,
    checkpoint_after: str | None = None,
    checkpoint_callback: CheckpointCallback | None = None,
) -> RunManifest:
    """Run every (state, fact) pair. Streams a manifest after each pair.

    ``extract_fn`` is injected for tests — defaults to
    ``extract.run``. Production callers leave it alone.

    ``checkpoint_after`` (Phase D carryover #6): pause after every
    pair for the named state completes and let the caller inspect the
    manifest before the remaining states fan out. The Phase C2
    ``extension_is_standalone`` polarity bug fired mid-fanout and cost
    $3.00 on three more states after AZ's $0.45 downgrade. With
    ``--checkpoint-after AZ``, the operator sees AZ's downgrade-count
    before WI/MN/TX spend anything. ``checkpoint_callback`` receives
    ``(state, manifest)`` and returns True to continue or False to
    halt; CLI default prints a summary and prompts interactively.
    """
    # Deterministic ordering: states outer in SUPPORTED_STATES order,
    # facts inner in PHASE_B_FACTS order (plan §6.2 authority). Source-
    # lens runs widen the inner order with ``SOURCE_DETERMINISTIC_FACTS``
    # so ``aggregate --lens source`` downstream finds every artifact it
    # needs without a follow-up extraction pass.
    runtime = build_runtime_client(model=model)
    model = runtime.model

    requested_states = {s.upper() for s in states}
    ordered_states = [s for s in SUPPORTED_STATES if s in requested_states]
    requested_facts = set(facts)
    lens_facts = phase_b_facts_for_lens(lens)
    ordered_facts = [f for f in lens_facts if f in requested_facts]

    started_at = _now_iso()
    manifest = RunManifest(
        started_at=started_at,
        model=model,
        lens=lens,
        prompt_version=prompt_version,
        cost_cap=cost_cap,
        limit=limit,
    )

    resolved_manifest_path = manifest_path or (scoring_phase_b_dir() / RUN_MANIFEST_NAME)
    resolved_manifest_path.parent.mkdir(parents=True, exist_ok=True)

    def _flush_manifest() -> None:
        manifest.ended_at = _now_iso()
        resolved_manifest_path.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # Initial flush — a crash before the first pair still leaves a manifest.
    _flush_manifest()

    checkpoint_target = checkpoint_after.upper() if checkpoint_after else None

    # Live-pool counts for the skip-if-complete staleness guard (issue
    # #212 item 7). Memoized per (state, source_filter) so the elements
    # artifact parses once per distinct filter, not once per fact. Only
    # computed on the production path (no out_dir override): test
    # harnesses that redirect artifacts to a tmp dir pair them with
    # injected extract_fn fakes, not the real elements catalog.
    pool_counts: dict[tuple[str, tuple[str, ...] | None], int | None] = {}

    def _expected_count(state: str, fact: str) -> int | None:
        if out_dir is not None:
            return None
        from src.score.extract import load_phase_a_records
        from src.score.schema import FACT_SOURCE_FILTERS

        source_filter = FACT_SOURCE_FILTERS.get(fact)
        cache_key = (state, source_filter)
        if cache_key not in pool_counts:
            try:
                pool_counts[cache_key] = len(
                    load_phase_a_records(
                        state, lens, limit=limit,
                        source_filter=source_filter,
                    )
                )
            except FileNotFoundError:
                # No elements artifact — the guard stands down and the
                # extraction itself raises the real error.
                pool_counts[cache_key] = None
        return pool_counts[cache_key]

    for state in ordered_states:
        for fact in ordered_facts:
            artifact_path = scoring_phase_a_artifact_path(state, fact, lens=lens)
            if out_dir is not None:
                artifact_path = out_dir / artifact_path.name

            expected = _expected_count(state, fact)
            if is_pair_complete(
                artifact_path,
                lens=lens,
                model=model,
                prompt_version=prompt_version,
                expected_record_count=expected,
            ):
                header = read_artifact_header(artifact_path) or {}
                artifact_records = int(header.get("record_count", 0))
                if expected is not None and artifact_records > expected:
                    # Pool SHRINKAGE — see is_pair_complete: the
                    # artifact carries rows today's pool filter no
                    # longer admits. Loud observability, never a
                    # destructive re-run.
                    _LOGGER.warning(
                        "(%s, %s): artifact covers %d records but the "
                        "current extraction pool yields %d — the "
                        "artifact predates a pool narrowing and is "
                        "kept as-is (re-extracting would rewrite it "
                        "narrow). Extraction-pool archaeology needed "
                        "if this is unexpected.",
                        state, fact, artifact_records, expected,
                    )
                pair = PairResult(
                    state=state,
                    fact=fact,
                    status="skipped",
                    record_count=int(header.get("record_count", 0)),
                    scored_count=int(header.get("scored_count", 0)),
                    total_usd=float(header.get("total_usd", 0.0)),
                    cache_hit_count=int(header.get("cache_hit_count", 0)),
                    downgrade_count=int(header.get("downgrade_count", 0)),
                    error=None,
                    artifact=str(artifact_path),
                )
                manifest.pairs.append(pair)
                if progress is not None:
                    progress(pair)
                _flush_manifest()
                continue

            remaining = max(cost_cap - manifest.total_usd, 0.0)
            if remaining <= 0.0:
                pair = PairResult(
                    state=state,
                    fact=fact,
                    status="skipped_cost_cap",
                    record_count=0,
                    scored_count=0,
                    total_usd=0.0,
                    cache_hit_count=0,
                    downgrade_count=0,
                    error=(
                        f"global cost cap ${cost_cap:.2f} exhausted "
                        f"(spent ${manifest.total_usd:.4f}); skipping"
                    ),
                    artifact=str(artifact_path),
                )
                manifest.pairs.append(pair)
                if progress is not None:
                    progress(pair)
                _flush_manifest()
                continue

            try:
                header = extract_fn(
                    fact=fact,
                    state=state,
                    lens=lens,
                    limit=limit,
                    cost_cap=remaining,
                    model=model,
                    prompt_version=prompt_version,
                    out_dir=out_dir,
                    cache_root=cache_root,
                )
                status = "complete" if header.get("status") == "complete" else "partial"
                pair = PairResult(
                    state=state,
                    fact=fact,
                    status=status,
                    record_count=int(header.get("record_count", 0)),
                    scored_count=int(header.get("scored_count", 0)),
                    total_usd=float(header.get("total_usd", 0.0)),
                    cache_hit_count=int(header.get("cache_hit_count", 0)),
                    downgrade_count=int(header.get("downgrade_count", 0)),
                    error=None,
                    artifact=str(artifact_path),
                )
            except CostCapExceeded as exc:
                # Partial artifact is already on disk from the streaming
                # writes inside extract.run(). Capture telemetry from it.
                disk_header = read_artifact_header(artifact_path) or {}
                pair = PairResult(
                    state=state,
                    fact=fact,
                    status="partial",
                    record_count=int(disk_header.get("record_count", 0)),
                    scored_count=int(disk_header.get("scored_count", 0)),
                    total_usd=float(disk_header.get("total_usd", 0.0)),
                    cache_hit_count=int(disk_header.get("cache_hit_count", 0)),
                    downgrade_count=int(disk_header.get("downgrade_count", 0)),
                    error=f"cost_cap_exceeded: {exc}",
                    artifact=str(artifact_path),
                )
            except (ScoringSchemaError, Exception) as exc:  # noqa: BLE001 — record + halt
                disk_header = read_artifact_header(artifact_path) or {}
                pair = PairResult(
                    state=state,
                    fact=fact,
                    status="failed",
                    record_count=int(disk_header.get("record_count", 0)),
                    scored_count=int(disk_header.get("scored_count", 0)),
                    total_usd=float(disk_header.get("total_usd", 0.0)),
                    cache_hit_count=int(disk_header.get("cache_hit_count", 0)),
                    downgrade_count=int(disk_header.get("downgrade_count", 0)),
                    error=f"{type(exc).__name__}: {exc}",
                    artifact=str(artifact_path),
                )
                manifest.pairs.append(pair)
                manifest.total_usd += pair.total_usd
                if progress is not None:
                    progress(pair)
                _flush_manifest()
                raise

            manifest.pairs.append(pair)
            manifest.total_usd += pair.total_usd
            if progress is not None:
                progress(pair)
            _flush_manifest()

        # Inner fact loop for this state completed — run the checkpoint
        # hook if the caller asked to pause here.
        if checkpoint_target is not None and state == checkpoint_target:
            if checkpoint_callback is not None:
                if not checkpoint_callback(state, manifest):
                    _LOGGER.info(
                        "checkpoint-after %s: callback returned False, halting run",
                        state,
                    )
                    _flush_manifest()
                    return manifest

    _flush_manifest()
    return manifest
