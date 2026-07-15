"""Spine-anchored gap aggregate — issue #73 Step 1 (deterministic-only).

Reads ``{state}_elements_gap.json`` (the Layer 2 surfacer output) and
emits ``{state}_scores_gap.json`` carrying NACHOS-shape sidecar entries
for each gap row. Every record lands with ``discovery_lens=
"spine_anchored"`` so downstream consumers (workbooks, reviewer
comparison) can keep gap-derived rows visually distinct from authentic
source-doc rows.

Step 1 scope: **no LLM extraction**. The 11 deterministic facts populate
from the spine catalog + each row's structural metadata; the 13
LLM-path facts surface as ``downgraded=True`` / ``confidence=low`` /
``downgrade_reason="missing_from_artifact"`` — the same shape
``load_fact_pool`` already produces for an LLM artifact that's silent on
a record. Step 2 (sample LLM extraction, ~$16) and Step 3 (full LLM
extraction, ~$160 cold) ride this scaffolding without further wiring.

Shape of the sidecar artifact mirrors the source/spine sidecar contract
to keep workbook + reviewer-comparison consumers uniform: same header
keys (``state``, ``edfi_version``, ``model``, ``prompt_version``,
``record_count``, ``dimension_stats``, NACHOS aggregates, …) plus a
``lens`` value of ``"spine_gap"`` so the file is self-identifying.

CRITICAL: module-level ``run()`` is a plain function. The Click wrapper
lives in ``src/cli.py``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

from src.models.element import ElementRecord
from src.models.spine import StateSpine
from src.states import SUPPORTED_STATES
from src.score.aggregate import (
    SCORING_PLAN_VERSION,
    ScoredRecord,
    _now_iso,
    _scored_record_to_dict,
    _score_one,
)
from src.score.deterministic import (
    DETERMINISTIC_FACTS,
    FactContext,
    build_fact_context,
    compute_fact,
    descriptor_values_enumerated_spans,
)
from src.score.rules import (
    FactResult,
    FactView,
    SPINE_DIMENSIONS,
    SPINE_RULE_INPUTS,
)
from src.utils.paths import (
    state_elements_gap_path,
    state_elements_path,
    state_scores_gap_path,
    state_spine_path,
)

_LOGGER = logging.getLogger(__name__)

# Discovery-lens tag the Step 1 path attaches to every emitted record.
DISCOVERY_LENS_SPINE_ANCHORED: str = "spine_anchored"

# Sidecar header tag — distinguishes the artifact from source / spine
# sidecars at a glance and gives the workbook surface code a cheap
# discriminator without parsing record-level fields.
GAP_LENS_TAG: str = "spine_gap"

# Rule-cascade lens. Gap rows are spine-anchored by construction; the
# spine-lens cascade is the architecturally correct choice (issue #73
# §"Open design questions" — recommend spine-lens).
_RULE_LENS: str = "spine"

# Subset of ``DETERMINISTIC_FACTS`` that doesn't need the spine context.
# We always have a context here, so this distinction is informational
# only — kept to mirror the deterministic.py vocabulary and make the
# fact-by-fact dispatch readable.
_LLM_INPUT_FACTS: tuple[str, ...] = tuple(
    sorted(set(SPINE_RULE_INPUTS) - set(DETERMINISTIC_FACTS))
)


# ---------------------------------------------------------------------------
# Synthesis: gap dict → ElementRecord
# ---------------------------------------------------------------------------


def synthesize_record(
    gap: dict[str, Any],
    *,
    state: str,
    edfi_version: str,
    documented: bool = False,
) -> ElementRecord:
    """Build an in-memory ElementRecord from a gap surfacer dict.

    Gap rows have no narrative text by construction — the surfacer only
    emits a row when the state's source doc is silent on that
    (entity, element). Every narrative field stays empty, which means
    ``definition_present`` / ``definition_text_substantive`` / the
    descriptor enumeration patterns all evaluate False — exactly the
    structural signal we want.

    ``source`` follows the gap row's extension attribution: rows whose
    spine slot lives on a state extension carry ``source="extension"``
    so the rule cascade's extension-aware branches behave consistently
    with the source/spine pipeline.

    ``documented`` defaults to False (truthful — gap rows are precisely
    the population the source doc was silent on). Callers feeding the
    Step 2/3 LLM extract pipeline pass ``documented=True`` because
    ``load_phase_a_records`` filters to ``documented=True`` rows; the
    flag carries no semantics through the LLM call itself.
    """
    extension_name = gap.get("spine_extension_name")
    source: str = "extension" if extension_name else "core"
    # TODO(#213 item 2, deferred): `domain` here carries the gap DISCOVERY
    # label ("spine_anchored"/...), not a post-#184 Source Area, and
    # `edfi_domain` is never stamped (this helper deliberately takes no
    # spine). Left as-is on purpose: gap_extract serializes these records
    # into the on-disk sample-elements artifact and the record's `domain`
    # feeds the `{domain}` prompt slot for the gap LLM fold, so changing it
    # invalidates the banked prompt cache (real $ on the next
    # `--with-gap-llm` run). The scoring cascade reads neither field, and
    # gap sidecar rows serialize neither — the label is prompt/observability
    # surface only. Fix alongside a deliberate prompt_version bump.
    return ElementRecord(
        state=state,
        edfi_version=edfi_version,
        domain=gap.get("discovery", "spine_anchored"),
        entity=gap["entity"],
        element_name=gap["element_name"],
        data_type=gap.get("spine_data_type"),
        definition_text="",
        source=source,  # type: ignore[arg-type]
        extension_name=extension_name,
        documented=documented,
    )


# Back-compat alias — older callers still import the underscore form.
_synthesize_record = synthesize_record


# ---------------------------------------------------------------------------
# Synthesis: ElementRecord → FactView (deterministic facts populated)
# ---------------------------------------------------------------------------


def _record_key(state: str, record: ElementRecord) -> str:
    return f"{state.upper()}|{record.entity}|{record.element_name}"


def _phase_a_gap_dir() -> Path:
    """Return ``data/out/scoring/phase_a_gap/`` — Step 2/3 LLM artifact root.

    Sibling to ``data/out/scoring/phase_a/`` (the source/spine artifact
    root). Defined here too (as well as in ``gap_extract``) so aggregate
    can read the directory without importing ``gap_extract`` and fanning
    out to its full dependency surface.
    """
    from src.utils.paths import out_dir as _out_dir

    return _out_dir() / "scoring" / "phase_a_gap"


def _load_gap_llm_pool(
    state: str, *, artifact_dir: Path | None = None
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return ``{fact: {record_key: artifact_row}}`` for any gap LLM artifacts.

    Walks every ``{state}_spine_{fact}.jsonl`` file under
    ``artifact_dir``; missing files surface as missing keys in the
    returned dict (caller falls back to the missing-from-artifact stub
    on a per-record basis). ``artifact_dir=None`` means "no LLM pool"
    — returns ``{}`` so the deterministic-only Step 1 path stays
    hermetic regardless of what's on disk under
    ``data/out/scoring/phase_a_gap/``. Step 2 / Step 3 callers pass the
    explicit directory.
    """
    from src.score.rules import _iter_artifact_rows  # type: ignore[attr-defined]

    if artifact_dir is None:
        return {}
    root = artifact_dir
    if not root.exists():
        return {}
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted(root.glob(f"{state.upper()}_spine_*.jsonl")):
        # Filename shape: {STATE}_spine_{fact}.jsonl
        stem = path.stem
        prefix = f"{state.upper()}_spine_"
        if not stem.startswith(prefix):
            continue
        fact = stem[len(prefix) :]
        _header, rows = _iter_artifact_rows(path)
        out[fact] = {}
        for row in rows:
            key = row.get("record_key")
            if key:
                out[fact][key] = row
    return out


def _build_fact_view(
    record: ElementRecord,
    *,
    state: str,
    fact_context: FactContext,
    llm_pool: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> FactView:
    """Compute deterministic facts and build a FactView.

    Deterministic facts populate with their real value at
    ``confidence="high"``. LLM rule inputs read from ``llm_pool`` when
    available — Step 2 / Step 3 populate it via the gap-extract
    pipeline. When the pool is empty (Step 1) or a fact is absent for
    this record, the ``FactView`` receives the missing-from-artifact
    stub so the rule cascade collapses that dimension to low confidence
    — same contract ``load_fact_pool`` enforces on the source/spine
    pipeline.
    """
    from src.score.rules import _row_to_fact_result  # type: ignore[attr-defined]

    facts: dict[str, FactResult] = {}

    for fact in DETERMINISTIC_FACTS:
        try:
            value = compute_fact(fact, record, context=fact_context)
        except Exception:  # pragma: no cover — defensive; a malformed gap
            facts[fact] = FactResult(
                fact=fact,
                value=None,
                confidence="low",
                downgraded=True,
                downgrade_reason="deterministic_compute_error",
            )
            continue

        spans: tuple[str, ...]
        if fact == "descriptor_values_enumerated":
            # Mirror the artifact-emission shape so fact_provenance
            # rendering stays uniform with the source/spine pipeline.
            spans = tuple(descriptor_values_enumerated_spans(record))
        else:
            spans = ()

        facts[fact] = FactResult(
            fact=fact,
            value=value,
            confidence="high",
            downgraded=False,
            downgrade_reason=None,
            spans=spans,
        )

    record_key = _record_key(state, record)
    pool = llm_pool or {}
    for fact in _LLM_INPUT_FACTS:
        rows_for_fact = pool.get(fact)
        row = rows_for_fact.get(record_key) if rows_for_fact else None
        if row is not None:
            facts[fact] = _row_to_fact_result(fact, row)
        else:
            facts[fact] = FactResult(
                fact=fact,
                value=None,
                confidence="low",
                downgraded=True,
                downgrade_reason="missing_from_artifact",
            )

    return FactView(
        record_key=record_key,
        entity=record.entity,
        element_name=record.element_name,
        facts=facts,
        source=record.source,
        extension_name=record.extension_name,
        documented=record.documented,
        documentation_source=getattr(record, "documentation_source", "source_doc"),
    )


# ---------------------------------------------------------------------------
# Sidecar header / metadata
# ---------------------------------------------------------------------------


def _infer_edfi_version(state: str) -> str | None:
    """Read ``edfi_version`` from the source-lens elements artifact.

    Gap surfacing depends on the source-lens artifact existing, so this
    is the natural place to read the version. Falls back to ``None`` if
    the file is missing — the sidecar header still writes, just with
    ``edfi_version=None`` (informational only).
    """
    from src.models.element import StateElements

    source_path = state_elements_path(state, "source")  # type: ignore[arg-type]
    if not source_path.exists():
        return None
    try:
        elements = StateElements.model_validate_json(
            source_path.read_text(encoding="utf-8")
        )
    except Exception:  # pragma: no cover — malformed artifact
        return None
    return elements.edfi_version


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _load_gap_records(
    state: str, *, gap_path: Path | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the gap artifact and return ``(gaps, envelope_metadata)``.

    Envelope metadata carries the ``generated_at`` / ``discovery_counts``
    fields the surfacer wrote so the gap sidecar can echo them.
    """
    path = gap_path or state_elements_gap_path(state)
    if not path.exists():
        raise FileNotFoundError(
            f"Gap artifact missing for {state}: {path}. "
            f"Run `mc ingest gap --state {state}` first."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    gaps = payload.get("gaps", [])
    envelope = {
        "generated_at": payload.get("generated_at"),
        "discovery_counts": payload.get("discovery_counts", {}),
        "gap_count": payload.get("gap_count", len(gaps)),
    }
    return gaps, envelope


def run(
    *,
    state: str,
    gap_path: Path | None = None,
    spine_path: Path | None = None,
    out_path: Path | None = None,
    model: str = "deterministic",
    prompt_version: str = "step1.det.v1",
    llm_artifact_dir: Path | None = None,
) -> dict[str, Any]:
    """Score every gap row with the spine-lens rule cascade; write the sidecar.

    ``model`` / ``prompt_version`` are recorded in the header for
    reproducibility. Defaults reflect the Step 1 deterministic-only
    posture; Step 2/3 callers override with the LLM model + prompt
    version they ran the extract under.

    ``llm_artifact_dir`` (issue #73 Step 2/3): when set, aggregate reads
    LLM-fact artifacts from this directory in addition to deterministic
    facts. Per-record values that exist in the artifacts populate the
    rule cascade with real LLM values; rows missing from an artifact
    fall back to the missing-from-artifact stub. Default ``None`` is
    Step 1 behaviour — pure deterministic, no live disk read against
    ``data/out/scoring/phase_a_gap/``. The CLI surfaces a flag for
    Step 2/3 to opt in.

    Returns the header dict (sidecar minus ``scores``) for callers that
    want a summary without re-reading the file.
    """
    state = state.upper()

    spine_p = spine_path or state_spine_path(state)
    if not spine_p.exists():
        raise FileNotFoundError(
            f"Spine missing for {state}: {spine_p}. "
            f"Run `mc spine fetch --state {state}` + "
            f"`mc spine build --state {state}` first."
        )
    spine = StateSpine.model_validate_json(
        spine_p.read_text(encoding="utf-8")
    )
    fact_context = build_fact_context(spine)

    gaps, envelope = _load_gap_records(state, gap_path=gap_path)
    edfi_version = _infer_edfi_version(state) or spine.edfi_version

    llm_pool = _load_gap_llm_pool(state, artifact_dir=llm_artifact_dir)

    scored: list[ScoredRecord] = []
    for gap in gaps:
        record = _synthesize_record(
            gap, state=state, edfi_version=edfi_version
        )
        view = _build_fact_view(
            record,
            state=state,
            fact_context=fact_context,
            llm_pool=llm_pool,
        )
        scored_record = _score_one(view, _RULE_LENS, source_ext_necessity=None)
        # Tag every row with the discovery-lens provenance flag so
        # downstream consumers don't need to special-case the artifact
        # path. dataclasses.replace works on the frozen ScoredRecord
        # without violating the immutability contract.
        scored_record = dataclasses.replace(
            scored_record, discovery_lens=DISCOVERY_LENS_SPINE_ANCHORED
        )
        scored.append(scored_record)

    needs_review_count = sum(1 for s in scored if s.review["needs_review"])

    per_record_values = [
        s._quality_mean_diagnostic
        for s in scored
        if s._quality_mean_diagnostic is not None
    ]
    mean_quality: float | None = None
    if per_record_values:
        mean_quality = round(sum(per_record_values) / len(per_record_values), 4)

    dim_stats: dict[str, dict[str, Any]] = {}
    for dim_name in SPINE_DIMENSIONS:
        values = [
            s.dimensions[dim_name].value
            for s in scored
            if dim_name in s.dimensions and s.dimensions[dim_name].value is not None
        ]
        dist = {i: values.count(i) for i in (0, 1, 2, 3)}
        dim_stats[dim_name] = {
            "count": len(values),
            "mean": round(sum(values) / len(values), 4) if values else None,
            "distribution": dist,
        }

    in_scope_count = sum(1 for s in scored if s.in_scope)
    in_scope_nachos_values: list[int] = [
        s.dimensions["nachos_score"].value
        for s in scored
        if s.in_scope
        and "nachos_score" in s.dimensions
        and s.dimensions["nachos_score"].value is not None
    ]
    nachos_score_histogram = {
        str(i): in_scope_nachos_values.count(i) for i in (0, 1, 2, 3)
    }
    mean_nachos_score = (
        round(sum(in_scope_nachos_values) / len(in_scope_nachos_values), 4)
        if in_scope_nachos_values
        else None
    )

    discovery_counts: dict[str, int] = {}
    for gap in gaps:
        d = gap.get("discovery", "unknown")
        discovery_counts[d] = discovery_counts.get(d, 0) + 1

    header: dict[str, Any] = {
        "state": state,
        "lens": GAP_LENS_TAG,
        "edfi_version": edfi_version,
        "scored_at": _now_iso(),
        "model": model,
        "prompt_version": prompt_version,
        "scoring_plan_version": SCORING_PLAN_VERSION,
        "record_count": len(scored),
        "scored_count": len(scored),
        "skipped_count": 0,
        "mean_quality_score": mean_quality,
        "needs_review_count": needs_review_count,
        "dimension_stats": dim_stats,
        "in_scope_count": in_scope_count,
        "nachos_score_histogram": nachos_score_histogram,
        "mean_nachos_score": mean_nachos_score,
        # Gap-specific provenance — echoed from the surfacer envelope so
        # downstream consumers can see the source artifact's generated_at
        # and discovery class breakdown without reopening the gap file.
        "gap_source_generated_at": envelope.get("generated_at"),
        "gap_discovery_counts": discovery_counts,
        "step": "1",
    }

    payload: dict[str, Any] = dict(header)
    payload["scores"] = [_scored_record_to_dict(s) for s in scored]

    destination = out_path or state_scores_gap_path(state)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _LOGGER.info(
        "wrote %s: %d gap records · mean quality=%s · review=%d",
        destination,
        len(scored),
        mean_quality,
        needs_review_count,
    )
    return header


def run_all(
    *,
    states: list[str] | None = None,
    out_dir: Path | None = None,
    model: str = "deterministic",
    prompt_version: str = "step1.det.v1",
    llm_artifact_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Aggregate gap-row sidecars for every state in ``states`` (default: full roster)."""
    targets = states or list(SUPPORTED_STATES)
    headers: list[dict[str, Any]] = []
    for state in targets:
        out_path = None
        if out_dir is not None:
            out_path = out_dir / f"{state.lower()}_scores_gap.json"
        headers.append(
            run(
                state=state,
                out_path=out_path,
                model=model,
                prompt_version=prompt_version,
                llm_artifact_dir=llm_artifact_dir,
            )
        )
    return headers
