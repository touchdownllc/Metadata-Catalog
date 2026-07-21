"""Output-path helpers for POC-3 artifacts.

Centralizes the `data/out/{state}_elements_{lens}.json`,
`data/out/{state}_gap_log.json`, and `data/spine/{state}_spine.json`
conventions so adapter, report, and test code can share one source of
truth for artifact locations.
"""

from pathlib import Path
from typing import Literal

# `paths.py` lives at `src/utils/paths.py`; project root is two levels up.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_OUT_DIR = _PROJECT_ROOT / "data" / "out"
_SPINE_DIR = _PROJECT_ROOT / "data" / "spine"

Lens = Literal["source", "spine"]


def _safe_path_component(value: str) -> str:
    """Return a Windows-safe path component derived from ``value``.

    Cache namespaces may include characters such as ``:`` that are
    valid in cache keys and headers but invalid in Windows directory
    names. Keep the component readable while stripping reserved
    characters and trimming trailing dots/spaces.
    """
    cleaned = []
    for char in value:
        if char in '<>:"/\\|?*':
            cleaned.append("_")
        else:
            cleaned.append(char)
    result = "".join(cleaned).rstrip(". ")
    return result or "default"


def project_root() -> Path:
    """Return the POC-3 project root (parent of `src/` and `data/`)."""
    return _PROJECT_ROOT


def out_dir() -> Path:
    """Return the canonical `data/out` directory."""
    return _OUT_DIR


def spine_dir() -> Path:
    """Return the canonical `data/spine` directory."""
    return _SPINE_DIR


def state_elements_path(state: str, lens: Lens = "source") -> Path:
    """Return `data/out/{state}_elements_{lens}.json` for the given state + lens."""
    return _OUT_DIR / f"{state.lower()}_elements_{lens}.json"


def state_elements_gap_path(state: str) -> Path:
    """Return `data/out/{state}_elements_gap.json` — the spine-anchored gap artifact.

    Sibling to `state_elements_path(state, "source")`. Issue #66 Layer 2:
    walks the source-lens output + spine catalog to surface entity/element
    pairs the spine knows about but the state's source doc is silent on.
    Generated post-hoc; never overwrites or mutates the source-lens file.
    """
    return _OUT_DIR / f"{state.lower()}_elements_gap.json"


def state_gap_log_path(state: str) -> Path:
    """Return `data/out/{state}_gap_log.json` for the given state."""
    return _OUT_DIR / f"{state.lower()}_gap_log.json"


def state_spine_path(state: str) -> Path:
    """Return `data/spine/{state}_spine.json` for the given state."""
    return _SPINE_DIR / f"{state.lower()}_spine.json"


def coverage_report_path(lens: Lens, ext: str) -> Path:
    """Return `data/out/coverage_report_{lens}.{ext}` for the given lens."""
    return _OUT_DIR / f"coverage_report_{lens}.{ext}"


def divergence_path(ext: str) -> Path:
    """Return `data/out/lens_divergence.{ext}` for the cross-lens divergence report."""
    return _OUT_DIR / f"lens_divergence.{ext}"


def scoring_phase_a_dir() -> Path:
    """Return `data/out/scoring/phase_a/` — Phase A extraction artifact root."""
    return _OUT_DIR / "scoring" / "phase_a"


def scoring_phase_a_artifact_path(state: str, fact: str, lens: Lens = "spine") -> Path:
    """Return `data/out/scoring/phase_a/{state}_{lens}_{fact}.jsonl` for a Phase A run.

    ``lens`` is part of the filename so the five lens-independent facts
    (``definition_present``, ``business_rules_present``,
    ``data_type_canonical``, ``descriptor_values_enumerated``,
    ``definition_text_substantive``) can coexist on disk for both
    lenses without mutual overwrite. Each artifact's header still
    carries ``"lens"`` for auditing; the filename is the read-side
    discriminator so consumers don't have to open every JSONL to tell
    lenses apart.
    """
    return scoring_phase_a_dir() / f"{state.upper()}_{lens}_{fact}.jsonl"


def scoring_cache_dir(model: str, prompt_version: str) -> Path:
    """Return `data/cache/scoring/{model}/{prompt_version}/` — mode-agnostic prompt cache."""
    return (
        _PROJECT_ROOT
        / "data"
        / "cache"
        / "scoring"
        / _safe_path_component(model)
        / _safe_path_component(prompt_version)
    )


def scoring_batches_dir() -> Path:
    """Return `data/cache/scoring/batches/` — Anthropic Batch API manifests.

    Sibling to the per-(model, prompt_version) cache dirs. One JSON file
    per submitted batch (filename = batch_id), recording the
    {custom_id → (state, lens, fact, entity)} mapping so ``score
    batches collect <batch_id>`` can validate and write through the
    cache layer without re-rendering prompts.
    """
    return _PROJECT_ROOT / "data" / "cache" / "scoring" / "batches"


def scoring_phase_b_dir() -> Path:
    """Return `data/out/scoring/phase_b/` — Phase B extraction artifact root."""
    return _OUT_DIR / "scoring" / "phase_b"


def scoring_phase_b_estimate_path() -> Path:
    """Return `data/out/scoring/phase_b/estimate.md` — Phase B cost-estimate markdown."""
    return scoring_phase_b_dir() / "estimate.md"


def state_scores_path(state: str, lens: Lens = "spine") -> Path:
    """Return `data/out/{state}_scores_{lens}.json` — Phase C per-record sidecar.

    Parallel file to `state_elements_{lens}.json`; never mutates the
    21-field element record. Written by `src.score.aggregate.run()`
    after the rule stage joins facts into dimensions.
    """
    return _OUT_DIR / f"{state.lower()}_scores_{lens}.json"


def state_scores_gap_path(state: str) -> Path:
    """Return `data/out/{state}_scores_gap.json` — spine-anchored gap sidecar.

    Sibling to `state_scores_{lens}.json` carrying scored records derived
    from `{state}_elements_gap.json`. Records inside this artifact are
    tagged ``discovery_lens="spine_anchored"`` so downstream consumers
    (workbooks / reviewer comparison) can distinguish them from authentic
    source-doc rows. Issue #73 Step 1: deterministic-only pass — LLM
    facts surface as downgraded, deterministic structural facts populate.
    """
    return _OUT_DIR / f"{state.lower()}_scores_gap.json"


def scoring_report_path(lens: Lens, ext: str) -> Path:
    """Return `data/out/scoring_report_{lens}.{ext}` — Phase D cross-state rollup.

    Follows the same JSON+MD dual-write pattern as `divergence_path`.
    One file per lens (source/spine); Phase D's `report/scoring.py`
    writes both extensions in a single `run()` call so reviewers can
    cross-reference the machine-readable JSON from the human-readable MD.
    """
    return _OUT_DIR / f"scoring_report_{lens}.{ext}"
