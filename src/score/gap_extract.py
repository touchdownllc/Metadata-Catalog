"""Gap-row LLM extract — issue #73 Step 2 / Step 3.

Selects a deterministic sample (Step 2) or the full corpus (Step 3) from
``{state}_elements_gap.json``, synthesizes ElementRecord instances, and
feeds them through the standard LLM extract pipeline. Artifacts land
under ``data/out/scoring/phase_a_gap/`` so they are physically separate
from the source/spine phase_a artifacts — there is no risk of
cross-contamination on disk and ``aggregate_gap`` reads from the gap
directory only.

Step 2 contract (``--sample-pct 0.10 --seed 73``): 10 % stratified by
state (each state contributes its own 10 %), reproducible across runs.
Step 3 contract (``--sample-pct 1.0``): every gap row.

CRITICAL: module-level ``run()`` is a plain function. The Click wrapper
lives in ``src/cli.py``. See
``tests/test_score_aggregate_gap.py::TestCliWiring`` for the regression
pattern.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.models.element import ElementRecord, StateElements
from src.models.spine import StateSpine
from src.score.aggregate_gap import (
    synthesize_record,
)
from src.score.deterministic import DETERMINISTIC_FACTS
from src.states import SUPPORTED_STATES
from src.score.extract import (
    DEFAULT_MODEL,
    PROMPT_VERSION,
    SUPPORTED_FACTS,
    run as run_extract,
)
from src.score.rules import (
    LENS_OBSERVABILITY_FACTS,
    PRODUCTIZATION_SIGNAL_FACTS,
    SPINE_RULE_INPUTS,
)
from src.utils.paths import (
    out_dir,
    state_elements_gap_path,
    state_spine_path,
)

_LOGGER = logging.getLogger(__name__)

# Defaults from issue #73 Step 2.
DEFAULT_SAMPLE_PCT: float = 0.10
DEFAULT_SEED: int = 73

# 12 LLM facts that cover the spine-lens scoring surface for gap rows.
# Built from the single source of truth in rules.py + extract.py:
# - SPINE_RULE_INPUTS minus DETERMINISTIC_FACTS = 10 LLM rule inputs.
# - LENS_OBSERVABILITY_FACTS["spine"] = 1 (semantic_class).
# - PRODUCTIZATION_SIGNAL_FACTS = 1 (integration_class).
# Filtered by SUPPORTED_FACTS so an artifact's prompt file is guaranteed
# to exist before we hit the LLM. Sorted for deterministic execution
# order (the runner's default ordering preserves PHASE_B_FACTS order
# but for our small 12-fact loop alphabetical is fine and stable).
SPINE_LLM_FACTS_FOR_GAP: tuple[str, ...] = tuple(
    sorted(
        (
            (set(SPINE_RULE_INPUTS) - set(DETERMINISTIC_FACTS))
            | set(LENS_OBSERVABILITY_FACTS.get("spine", ()))
            | set(PRODUCTIZATION_SIGNAL_FACTS)
        )
        & set(SUPPORTED_FACTS)
    )
)


def _phase_a_gap_dir() -> Path:
    """Return ``data/out/scoring/phase_a_gap/`` — gap-extract artifact root.

    Sibling to ``data/out/scoring/phase_a/`` (the source/spine artifact
    root) so the per-fact JSONLs for gap rows never collide with the
    documented-row artifacts. ``aggregate_gap`` reads from here when it
    needs LLM-fact values for gap rows.
    """
    return out_dir() / "scoring" / "phase_a_gap"


def _gap_sample_dir() -> Path:
    """Return ``data/out/scoring/gap_sample/`` — synthesized elements artifacts.

    The gap-extract pipeline writes a per-state synthesized
    StateElements file here so ``extract.run()`` can consume it via
    ``--elements-path``. One file per state per run (overwritten on
    subsequent runs).
    """
    return out_dir() / "scoring" / "gap_sample"


def _gap_sample_elements_path(state: str) -> Path:
    return _gap_sample_dir() / f"{state.lower()}_elements_gap_extract.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class GapExtractResult:
    """Per-state outcome of a Step 2 / Step 3 extract.

    Mirrors the per-pair telemetry surfaced by ``runner.RunManifest`` so
    the CLI can echo a uniform summary regardless of which step ran.
    """

    state: str
    sample_count: int
    fact_results: list[dict[str, Any]] = field(default_factory=list)
    total_usd: float = 0.0
    total_downgrades: int = 0
    total_records: int = 0


# ---------------------------------------------------------------------------
# Sample selection
# ---------------------------------------------------------------------------


def select_sample(
    state: str,
    *,
    sample_pct: float = DEFAULT_SAMPLE_PCT,
    seed: int = DEFAULT_SEED,
    gap_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return ``ceil(sample_pct * N)`` gap rows for the state, deterministically.

    Uses a state-namespaced PRNG seeded via SHA-256 of the state code so
    the sample is stable across runs AND processes, but distinct per
    state. (Issue #211 item 5b: the original ``seed + hash(state)`` used
    the per-process-salted builtin ``hash`` — every invocation selected
    a different "reproducible" sample, breaking cache-resume and
    cross-run comparisons.) The returned rows preserve the surfacer's
    stable sort order so the synthesized elements artifact is
    byte-deterministic.

    ``sample_pct=1.0`` returns every row in original order (used by Step
    3's full-extract path; equivalent to ``select_sample`` followed by a
    re-sort, which the surfacer already gives us).
    """
    path = gap_path or state_elements_gap_path(state)
    if not path.exists():
        raise FileNotFoundError(
            f"Gap artifact missing for {state}: {path}. "
            f"Run `mc ingest gap --state {state}` first."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    gaps = list(payload.get("gaps", []))
    if not gaps:
        return []
    if sample_pct >= 1.0:
        return gaps
    if sample_pct <= 0.0:
        return []

    n = math.ceil(sample_pct * len(gaps))
    state_ns = int.from_bytes(
        hashlib.sha256(state.upper().encode("utf-8")).digest()[:4], "big"
    )
    rng = random.Random(seed + state_ns)
    indexed = list(enumerate(gaps))
    rng.shuffle(indexed)
    chosen_indices = sorted(idx for idx, _ in indexed[:n])
    return [gaps[i] for i in chosen_indices]


# ---------------------------------------------------------------------------
# Synthesized elements artifact (the seam into extract.run())
# ---------------------------------------------------------------------------


def _infer_edfi_version(state: str) -> str:
    """Best-effort version lookup from the spine; defaults to ``"4.0"``."""
    spine_p = state_spine_path(state)
    if spine_p.exists():
        try:
            spine = StateSpine.model_validate_json(
                spine_p.read_text(encoding="utf-8")
            )
            return spine.edfi_version
        except Exception:  # pragma: no cover — malformed spine
            pass
    return "4.0"


def write_sample_elements_artifact(
    state: str, gaps: list[dict[str, Any]], *, out_path: Path | None = None
) -> Path:
    """Synthesize a StateElements artifact from a gap-row sample.

    Records carry ``documented=True`` on purpose — ``load_phase_a_records``
    filters to ``documented=True`` rows, and the flag is informational
    only inside the LLM call. Per-record narrative fields stay empty by
    construction (gap rows have no source-doc narrative) so prompts will
    receive only the structural metadata the spine catalog provides.

    Returns the artifact path so the caller can pass it to
    ``extract.run(elements_path=...)``.
    """
    edfi_version = _infer_edfi_version(state)
    records: list[ElementRecord] = [
        synthesize_record(
            g, state=state.upper(), edfi_version=edfi_version, documented=True
        )
        for g in gaps
    ]
    payload = StateElements(
        state=state.upper(),
        edfi_version=edfi_version,
        extracted_at=datetime.now(timezone.utc),
        element_count=len(records),
        elements=records,
    )
    destination = out_path or _gap_sample_elements_path(state)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        payload.model_dump_json(indent=2), encoding="utf-8"
    )
    return destination


# ---------------------------------------------------------------------------
# Extract loop
# ---------------------------------------------------------------------------


def run(
    *,
    state: str,
    sample_pct: float = DEFAULT_SAMPLE_PCT,
    seed: int = DEFAULT_SEED,
    cost_cap: float = 25.0,
    facts: tuple[str, ...] = SPINE_LLM_FACTS_FOR_GAP,
    model: str = DEFAULT_MODEL,
    prompt_version: str = PROMPT_VERSION,
    dry_run: bool = False,
    gap_path: Path | None = None,
    out_dir: Path | None = None,
    cache_root: Path | None = None,
) -> GapExtractResult:
    """Sample gap rows for one state and run every LLM fact through extract.

    ``cost_cap`` is the **global** ceiling for this state's run across
    every fact. Each fact call receives its own remaining-budget value
    (``cap - cumulative_spend``); a fact that runs over its share aborts
    cleanly via ``CostCapExceeded`` and the loop continues on the next
    fact only if budget remains.

    Returns a ``GapExtractResult`` telemetry dict so the CLI can render
    a per-state summary; full per-fact headers live on disk.
    """
    state = state.upper()
    gaps = select_sample(state, sample_pct=sample_pct, seed=seed, gap_path=gap_path)
    if not gaps:
        return GapExtractResult(state=state, sample_count=0)

    elements_path = write_sample_elements_artifact(state, gaps)
    artifact_dir = out_dir or _phase_a_gap_dir()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    result = GapExtractResult(state=state, sample_count=len(gaps))
    cumulative_usd = 0.0

    for fact in facts:
        if cumulative_usd >= cost_cap:
            _LOGGER.warning(
                "gap-extract %s: cost cap $%.2f exhausted before fact %s",
                state, cost_cap, fact,
            )
            result.fact_results.append({
                "fact": fact,
                "status": "skipped_cost_cap",
                "total_usd": 0.0,
                "downgrade_count": 0,
                "scored_count": 0,
            })
            continue

        remaining = max(cost_cap - cumulative_usd, 0.0)
        try:
            header = run_extract(
                fact=fact,
                state=state,
                lens="spine",
                limit=None,
                cost_cap=remaining,
                model=model,
                prompt_version=prompt_version,
                dry_run=dry_run,
                elements_path=elements_path,
                out_dir=artifact_dir,
                cache_root=cache_root,
            )
        except Exception as exc:  # noqa: BLE001 — capture + continue
            _LOGGER.error("gap-extract %s/%s failed: %s", state, fact, exc)
            result.fact_results.append({
                "fact": fact,
                "status": "error",
                "error": str(exc),
                "total_usd": 0.0,
                "downgrade_count": 0,
                "scored_count": 0,
            })
            continue

        usd = float(header.get("total_usd", 0.0) or 0.0)
        downgrades = int(header.get("downgrade_count", 0) or 0)
        scored = int(header.get("scored_count", 0) or 0)
        result.fact_results.append({
            "fact": fact,
            "status": header.get("status", "complete"),
            "total_usd": usd,
            "downgrade_count": downgrades,
            "scored_count": scored,
            "record_count": int(header.get("record_count", 0) or 0),
        })
        result.total_usd += usd
        result.total_downgrades += downgrades
        result.total_records += scored
        cumulative_usd += usd

    return result


def run_all(
    *,
    states: list[str] | None = None,
    sample_pct: float = DEFAULT_SAMPLE_PCT,
    seed: int = DEFAULT_SEED,
    cost_cap: float = 25.0,
    facts: tuple[str, ...] = SPINE_LLM_FACTS_FOR_GAP,
    model: str = DEFAULT_MODEL,
    prompt_version: str = PROMPT_VERSION,
    dry_run: bool = False,
    max_workers: int | None = None,
) -> list[GapExtractResult]:
    """Run gap-extract for every state in ``states`` (default 4-state).

    States run in parallel by default — one ``ThreadPoolExecutor``
    worker per state. Each state's flow internally remains sequential
    across its 12 facts (rate-limit safe; serialized writes to that
    state's synthesized elements artifact). The artifact directory is
    flat per-state (``{STATE}_spine_{fact}.jsonl``) so concurrent state
    runs cannot collide. The Anthropic cache is keyed by
    ``(prompt_text, model, prompt_version)`` — different states produce
    different per-batch user-text, so cache writes also don't race.

    ``max_workers=None`` defaults to ``len(targets)`` so 4 states run
    4-way concurrent. Pass ``max_workers=1`` to force sequential
    execution (debugging / cost-isolated runs / order-stable test
    fixtures). ``cost_cap`` is per-state in either mode.

    The progress log interleaves across workers; the per-state results
    are returned in input order regardless of completion timing so
    downstream rendering stays deterministic.
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = [s.upper() for s in (states or SUPPORTED_STATES)]
    if not targets:
        return []

    workers = max_workers if max_workers is not None else len(targets)
    if workers <= 1 or len(targets) == 1:
        return [
            run(
                state=s,
                sample_pct=sample_pct,
                seed=seed,
                cost_cap=cost_cap,
                facts=facts,
                model=model,
                prompt_version=prompt_version,
                dry_run=dry_run,
            )
            for s in targets
        ]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                run,
                state=s,
                sample_pct=sample_pct,
                seed=seed,
                cost_cap=cost_cap,
                facts=facts,
                model=model,
                prompt_version=prompt_version,
                dry_run=dry_run,
            ): s
            for s in targets
        }
        # Preserve input order in the returned list — callers (CLI rendering,
        # tests) depend on stable state ordering even when workers finish
        # out of order.
        results_by_state: dict[str, GapExtractResult] = {}
        for fut, state in futures.items():
            results_by_state[state] = fut.result()
    return [results_by_state[s] for s in targets]


# ---------------------------------------------------------------------------
# Downgrade-rate summary
# ---------------------------------------------------------------------------


def _read_artifact_header(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    line = path.open(encoding="utf-8").readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def downgrade_summary(
    states: list[str] | None = None,
    *,
    facts: tuple[str, ...] = SPINE_LLM_FACTS_FOR_GAP,
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    """Aggregate downgrade-rate telemetry across the gap-extract artifacts.

    Returns a dict shaped for both human-readable rendering (CLI) and
    Step 2 verification (the issue's >30 % halt criterion). Per-fact
    downgrade rate is the cross-state weighted mean.

    Output:

    ```jsonc
    {
      "states_loaded": ["AZ", "WI", ...],  // the full roster
      "per_fact": {
        "definition_is_implementable": {
          "scored": 954, "downgrades": 17, "rate": 0.0178, "by_state": {...}
        },
        ...
      },
      "overall_rate": 0.0345,
      "halt_recommended": false
    }
    ```
    """
    states_in = states or list(SUPPORTED_STATES)
    root = artifact_dir or _phase_a_gap_dir()
    states_loaded: list[str] = []
    per_fact: dict[str, dict[str, Any]] = {}
    grand_scored = 0
    grand_downgrades = 0

    for fact in facts:
        per_fact[fact] = {
            "scored": 0,
            "downgrades": 0,
            "rate": 0.0,
            "by_state": {},
        }
        for state in states_in:
            artifact = root / f"{state.upper()}_spine_{fact}.jsonl"
            header = _read_artifact_header(artifact)
            if not header:
                continue
            if state.upper() not in states_loaded:
                states_loaded.append(state.upper())
            scored = int(header.get("scored_count", 0) or 0)
            downgrades = int(header.get("downgrade_count", 0) or 0)
            per_fact[fact]["scored"] += scored
            per_fact[fact]["downgrades"] += downgrades
            per_fact[fact]["by_state"][state.upper()] = {
                "scored": scored,
                "downgrades": downgrades,
                "rate": (downgrades / scored) if scored else 0.0,
            }
        scored_total = per_fact[fact]["scored"]
        if scored_total:
            per_fact[fact]["rate"] = round(
                per_fact[fact]["downgrades"] / scored_total, 4
            )
        grand_scored += scored_total
        grand_downgrades += per_fact[fact]["downgrades"]

    overall_rate = (
        round(grand_downgrades / grand_scored, 4) if grand_scored else 0.0
    )
    halt = any(stats["rate"] > 0.30 for stats in per_fact.values())
    return {
        "states_loaded": states_loaded,
        "per_fact": per_fact,
        "total_scored": grand_scored,
        "total_downgrades": grand_downgrades,
        "overall_rate": overall_rate,
        "halt_recommended": halt,
        "halt_threshold": 0.30,
    }
