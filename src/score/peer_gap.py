"""Peer-state triangulation runner — Wave 2 Mitigation 4.

Plan origin: ``docs/archive/nachos-v2-two-axis-plan.md`` §4 Mitigation 4.

Structurally different from ``src.score.extract``:

- **Slot-keyed, cross-state.** One LLM invocation per (entity, element)
  spine slot. The user block carries narratives from EVERY state that
  has the slot documented. The output keys on the slot.
- **Synthesis, not extraction.** No span validator — the output is
  comparative reading, not yes/no fact lookup. Confidence + structured
  framing are the safety levers; humans review before any state sees
  the suggestion.
- **One artifact, one row per slot.** Writes
  ``data/out/scoring/phase_a/peer_gap.jsonl`` (header + one row per
  slot). NOT integrated into the rule-cascade / aggregate pipeline —
  peer-gap output is stakeholder-facing artifact (Score Card / docs/
  recommendations.md §2/§4 inputs), not a per-record scoring dimension.

CRITICAL: module-level ``run()`` is a plain function. The Click wrapper
lives in ``src/cli.py``. See
``tests/test_ingest_az.py::TestCliWiring`` for the regression pattern.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import jsonschema

from src.models.element import ElementRecord, StateElements
from src.score.cache import Cache, cache_key
from src.score.client import DEFAULT_MODEL, AnthropicClient, LLMClient, LLMResponse
# Shared dispatch engine + atomic JSONL writer (issue #213 item 3) —
# the writer keeps its historical private name.
from src.score.dispatch import (
    DispatchItem,
    DispatchState,
    dispatch_batches,
    write_jsonl_artifact as _write_artifact,
)
from src.score.extract import _canonical_prompt, _load_template
from src.score.schema import ScoringSchemaError
from src.states import SUPPORTED_STATES
from src.utils.paths import scoring_phase_a_dir, state_elements_path, state_scores_path

_LOGGER = logging.getLogger(__name__)

# Bump when the prompt template changes — invalidates cache by design.
PROMPT_VERSION = "peer-gap.v1"

# Historical seed slots — the 10-slot pilot shipped 2026-04-24 that
# validated the peer-gap signal ($0.29, 2 high / 6 medium / 2 low
# confidence, zero hallucination, one genuine failure-mode catch on
# TX Session.beginDate misapplied bulk-glossary dump). Mix of identifier /
# descriptor / date data types; mostly 4-state slots with a few 3-state
# slots that surfaced as headline AZ-prescriptive vs WI-conceptual gaps
# in the Day 1-3 ranking pass.
#
# Preserved as a frozen reference so the pilot run is reproducible and
# so regression tests have a stable slot list. Full-coverage runs use
# ``all_multi_state_slots()`` instead — the seed is a historical
# artifact, not the primary slot source.
SEED_SLOTS: tuple[tuple[str, str], ...] = (
    ("Section", "sectionIdentifier"),
    ("Calendar", "calendarCode"),
    ("Student", "studentUniqueId"),
    ("DisciplineIncident", "incidentIdentifier"),
    ("StaffSectionAssociation", "classroomPositionDescriptor"),
    ("Course", "courseCode"),
    ("Session", "beginDate"),
    ("Calendar", "calendarTypeDescriptor"),
    ("CourseOffering", "localCourseCode"),
    ("StudentSchoolAssociation", "entryGradeLevelDescriptor"),
)

_PROMPTS_DIR = Path(__file__).with_name("prompts")
_PROMPT_PATH = _PROMPTS_DIR / "peer_state_gap.md"
_ARTIFACT_NAME = "peer_gap.jsonl"


def _require_paths_cover_states(
    states: tuple[str, ...], elements_paths: dict[str, Path] | None,
) -> None:
    """Closed-world guard: an explicit ``elements_paths`` must cover ``states``.

    Without this, a caller that seeds a partial dict silently falls
    through to the real ``data/out`` artifacts for the omitted states —
    red suite on fresh clones, and "hermetic" tests quietly coupled to
    production data everywhere else (issue #211 item 2). Passing
    ``elements_paths=None`` keeps the production behavior: every state
    resolves via ``state_elements_path``.
    """
    if elements_paths is None:
        return
    missing = [st for st in states if st not in elements_paths]
    if missing:
        raise ValueError(
            "elements_paths provided but missing states: "
            f"{', '.join(missing)} — pass states= to restrict the "
            "roster; omitted states never fall back to the real "
            "data/out artifacts"
        )


def all_multi_state_slots(
    *,
    states: Iterable[str] = SUPPORTED_STATES,
    elements_paths: dict[str, Path] | None = None,
    min_states: int = 2,
) -> tuple[tuple[str, str], ...]:
    """Every (entity, element) spine slot documented in ``min_states``+ states.

    Walks each state's spine-lens elements file and counts states where
    the (entity, element) row carries ``documented=True`` and isn't
    filtered-out (``source != "filtered"``). Slots with ≥ ``min_states``
    are returned, sorted by (entity, element) so fanout ordering stays
    deterministic across runs.

    Used by the widened peer-gap fanout — replaces the 10-slot
    ``SEED_SLOTS`` pilot with full coverage of the multi-state surface.
    At four-state workspace cardinality and current spine, this yields
    ~230-240 slots; under ~$7 at the prompt's typical $0.03/slot cost.
    """
    states = tuple(states)
    _require_paths_cover_states(states, elements_paths)
    paths = elements_paths or {}
    counters: dict[tuple[str, str], int] = {}
    for st in states:
        records = _load_state_narratives(
            st, "spine", elements_path=paths.get(st),
        )
        for key, rec in records.items():
            if rec.source == "filtered":
                continue
            if not rec.documented:
                continue
            counters[key] = counters.get(key, 0) + 1
    multi = [k for k, n in counters.items() if n >= min_states]
    multi.sort()
    return tuple(multi)


# ---------------------------------------------------------------------------
# Output schema (JSON-schema validation; no span validator)
# ---------------------------------------------------------------------------


_POSTURE_VALUES = (
    "prescriptive", "conceptual", "cross_reference", "regulatory", "silent",
)
_CONFIDENCE_VALUES = ("high", "medium", "low")

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,  # tolerate harmless drift
    "required": [
        "slot_key", "consensus_concept", "per_state_divergences",
        "suggested_fills", "confidence", "confidence_rationale",
    ],
    "properties": {
        "slot_key": {"type": "string", "minLength": 1},
        "consensus_concept": {"type": "string", "minLength": 1},
        "per_state_divergences": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": ["state", "posture", "summary"],
                "properties": {
                    "state": {"type": "string", "enum": list(SUPPORTED_STATES)},
                    "posture": {"type": "string", "enum": list(_POSTURE_VALUES)},
                    "summary": {"type": "string", "minLength": 1},
                },
            },
        },
        "suggested_fills": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "required": [
                    "state", "current_posture", "peer_consensus_format",
                    "recommended_fill",
                ],
                "properties": {
                    "state": {"type": "string", "enum": list(SUPPORTED_STATES)},
                    "current_posture": {
                        "type": "string", "enum": list(_POSTURE_VALUES),
                    },
                    "peer_consensus_format": {"type": "string"},
                    "recommended_fill": {"type": "string", "minLength": 1},
                },
            },
        },
        "confidence": {"type": "string", "enum": list(_CONFIDENCE_VALUES)},
        "confidence_rationale": {"type": "string", "minLength": 1},
    },
}


def _validate_output(payload: Any, slot_key: str) -> dict[str, Any]:
    """Validate ``payload`` against ``_OUTPUT_SCHEMA``; check slot_key echo.

    Raises ``ScoringSchemaError`` on either jsonschema failure or
    slot_key mismatch — the runner persists the partial artifact and
    re-raises so the operator sees the failure mode.
    """
    try:
        jsonschema.validate(payload, _OUTPUT_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise ScoringSchemaError(
            f"peer-gap response failed schema validation for {slot_key!r}: {exc.message}",
            payload=payload,
        ) from exc
    if payload["slot_key"] != slot_key:
        raise ScoringSchemaError(
            f"peer-gap slot_key mismatch — expected {slot_key!r}, got {payload['slot_key']!r}",
            payload=payload,
        )
    return dict(payload)


# ---------------------------------------------------------------------------
# Slot loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateSlotView:
    """Per-state read of one spine slot."""

    state: str
    documented: bool
    structural_tier: int | None
    definition_text: str
    business_rules_text: str
    element_specific_rules: str
    edfi_standard_definition: str
    data_type: str | None
    domain: str | None


@dataclass(frozen=True)
class SlotBundle:
    """All states' views of one (entity, element) slot."""

    entity: str
    element_name: str
    data_type: str | None
    edfi_standard_definition: str
    states: tuple[StateSlotView, ...]

    @property
    def slot_key(self) -> str:
        return f"{self.entity}|{self.element_name}"


def _load_state_narratives(
    state: str, lens: str = "spine", *, elements_path: Path | None = None,
) -> dict[tuple[str, str], ElementRecord]:
    """Read one state's spine-lens (or source-lens) elements file into a slot map."""
    path = elements_path or state_elements_path(state, lens)  # type: ignore[arg-type]
    data = StateElements.model_validate_json(path.read_text(encoding="utf-8"))
    return {(r.entity, r.element_name): r for r in data.elements}


def _load_state_struct_tiers(
    state: str, *, scores_path: Path | None = None,
) -> dict[tuple[str, str], int]:
    """Read structural_depth tier per slot from the spine-lens sidecar."""
    path = scores_path or state_scores_path(state, "spine")
    if not path.exists():
        return {}
    sidecar = json.loads(path.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], int] = {}
    for rec in sidecar.get("scores", []):
        dim = rec.get("dimensions", {}).get("structural_depth")
        if not dim:
            continue
        v = dim.get("value")
        if v is None:
            continue
        out[(rec["entity"], rec["element_name"])] = int(v)
    return out


def build_slot_bundles(
    slots: Iterable[tuple[str, str]],
    *,
    states: Iterable[str] = SUPPORTED_STATES,
    elements_paths: dict[str, Path] | None = None,
    scores_paths: dict[str, Path] | None = None,
) -> list[SlotBundle]:
    """Build the per-slot bundle each state contributes to.

    A state is INCLUDED for a slot only when its spine-lens elements
    file carries a ``documented=True`` row for that (entity, element).
    States that lack the slot are omitted from the bundle (peer-gap
    triangulation reflects only states that actually published the
    field).
    """
    states = tuple(states)
    _require_paths_cover_states(states, elements_paths)
    elements_paths = elements_paths or {}
    scores_paths = scores_paths or {}
    per_state_records: dict[str, dict[tuple[str, str], ElementRecord]] = {}
    per_state_struct: dict[str, dict[tuple[str, str], int]] = {}
    for st in states:
        per_state_records[st] = _load_state_narratives(
            st, "spine", elements_path=elements_paths.get(st),
        )
        per_state_struct[st] = _load_state_struct_tiers(
            st, scores_path=scores_paths.get(st),
        )

    bundles: list[SlotBundle] = []
    for entity, element in slots:
        key = (entity, element)
        # Pick the canonical data_type / edfi_standard_definition from
        # the first state that has the slot. Spine-lens carries the
        # canonical Ed-Fi values regardless of which state we pick.
        data_type: str | None = None
        edfi_def = ""
        views: list[StateSlotView] = []
        for st in states:
            rec = per_state_records[st].get(key)
            if rec is None:
                continue
            if rec.source == "filtered":
                continue
            if not rec.documented:
                continue
            if data_type is None:
                data_type = rec.data_type
            if not edfi_def and rec.edfi_standard_definition:
                edfi_def = rec.edfi_standard_definition.strip()
            views.append(
                StateSlotView(
                    state=st,
                    documented=rec.documented,
                    structural_tier=per_state_struct[st].get(key),
                    definition_text=(rec.definition_text or "").strip(),
                    business_rules_text=(rec.business_rules_text or "").strip(),
                    element_specific_rules=(rec.element_specific_rules or "").strip(),
                    edfi_standard_definition=(rec.edfi_standard_definition or "").strip(),
                    data_type=rec.data_type,
                    # Issue #184: cross-state slot bundling groups by the
                    # Ed-Fi domain, not the per-state Source Area.
                    domain=rec.edfi_domain,
                )
            )
        if not views:
            _LOGGER.warning("slot %s|%s has no documented state — skipping", entity, element)
            continue
        bundles.append(
            SlotBundle(
                entity=entity,
                element_name=element,
                data_type=data_type,
                edfi_standard_definition=edfi_def,
                states=tuple(views),
            )
        )
    return bundles


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


_MAX_NARRATIVE_CHARS = 6000  # per state per field — guard against blow-up


def _truncate(text: str, max_chars: int = _MAX_NARRATIVE_CHARS) -> str:
    """Hard-cap a narrative field; preserves head, marks tail as elided.

    Some entities (StudentSpecialEducationProgramAssociation,
    DisciplineAction) carry multi-thousand-line business_rules_text
    blocks. The peer-gap prompt sends 4 states' worth of narrative in
    one shot — without a cap, a single slot can blow past the model's
    8K output budget on the way IN. A 6000-char head per field keeps
    the prompt well-formed; truncation marker tells the LLM the tail
    was elided so it doesn't fabricate "the rest says X."
    """
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n[…truncated for prompt-length cap…]"


def _format_state_narrative(view: StateSlotView) -> str:
    """Render one state's narrative block."""
    posture_hint = (
        "documented" if (
            view.definition_text or view.business_rules_text or view.element_specific_rules
        ) else "silent"
    )
    parts = [
        f"  state: {view.state}",
        f"    posture_hint: {posture_hint}",
        f"    structural_tier: {view.structural_tier if view.structural_tier is not None else '(unknown)'}",
        f"    data_type: {view.data_type or '(unknown)'}",
        f"    definition_text: {_truncate(view.definition_text) if view.definition_text else '(none)'}",
        f"    business_rules_text: {_truncate(view.business_rules_text) if view.business_rules_text else '(none)'}",
        f"    element_specific_rules: {_truncate(view.element_specific_rules) if view.element_specific_rules else '(none)'}",
    ]
    return "\n".join(parts)


def render_prompt(bundle: SlotBundle, *, prompt_path: Path = _PROMPT_PATH) -> tuple[str, str]:
    """Render (system_text, user_text) for one slot bundle."""
    system_text, user_template = _load_template(prompt_path)
    states_present = ", ".join(v.state for v in bundle.states)
    state_blocks = "\n\n".join(_format_state_narrative(v) for v in bundle.states)
    user_text = user_template.format_map(
        {
            "slot_key": bundle.slot_key,
            "entity": bundle.entity,
            "element_name": bundle.element_name,
            "data_type": bundle.data_type or "(unknown)",
            "edfi_standard_definition": (
                bundle.edfi_standard_definition if bundle.edfi_standard_definition else "(none)"
            ),
            "states_present": states_present,
            "state_narratives": state_blocks,
        }
    )
    return system_text, user_text


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class PeerGapTelemetry:
    bundle_count: int = 0
    scored_count: int = 0
    cache_hit_count: int = 0
    skipped_count: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_usd: float = 0.0


ProgressCallback = Callable[[dict[str, Any]], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_dry_run(
    *,
    bundles: list[SlotBundle],
    rendered: list[tuple[SlotBundle, str, str]],
    out_dir: Path,
    model: str,
    prompt_version: str,
) -> Path:
    """Write per-slot prompt files + manifest under ``<out_dir>/dry_run/``."""
    dry_dir = out_dir / "dry_run_peer_gap"
    dry_dir.mkdir(parents=True, exist_ok=True)
    for old in dry_dir.glob("*.prompt.md"):
        old.unlink()
    manifest: dict[str, Any] = {
        "kind": "peer_gap",
        "model": model,
        "prompt_version": prompt_version,
        "bundle_count": len(bundles),
        "bundles": [],
    }
    for idx, (bundle, system_text, user_text) in enumerate(rendered):
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", bundle.slot_key)
        name = f"{idx:03d}_{safe}.prompt.md"
        path = dry_dir / name
        path.write_text(
            f"# SYSTEM\n\n{system_text.rstrip()}\n\n# USER\n\n{user_text.rstrip()}\n",
            encoding="utf-8",
        )
        canonical = _canonical_prompt(system_text, user_text)
        manifest["bundles"].append(
            {
                "index": idx,
                "slot_key": bundle.slot_key,
                "states": [v.state for v in bundle.states],
                "prompt_file": name,
                "cache_key": cache_key(canonical, model, prompt_version),
            }
        )
    manifest_path = dry_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def run(
    *,
    slots: Iterable[tuple[str, str]] | None = None,
    states: Iterable[str] = SUPPORTED_STATES,
    model: str = DEFAULT_MODEL,
    prompt_version: str = PROMPT_VERSION,
    cost_cap: float = 5.0,
    dry_run: bool = False,
    out_dir: Path | None = None,
    cache_root: Path | None = None,
    elements_paths: dict[str, Path] | None = None,
    scores_paths: dict[str, Path] | None = None,
    client: LLMClient | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Execute the peer-state triangulation runner.

    Returns the run header. Writes ``data/out/scoring/phase_a/peer_gap.jsonl``
    (one row per slot) unless ``out_dir`` redirects the destination.

    When ``slots`` is None, the runner defaults to ``SEED_SLOTS`` — the
    historical 10-slot pilot. Full-coverage runs pass
    ``slots=all_multi_state_slots()`` explicitly (the CLI does this as
    its default, post-pilot).
    """
    slot_list = tuple(slots) if slots is not None else SEED_SLOTS
    bundles = build_slot_bundles(
        slot_list,
        states=states,
        elements_paths=elements_paths,
        scores_paths=scores_paths,
    )

    out_root = out_dir or scoring_phase_a_dir()
    out_root.mkdir(parents=True, exist_ok=True)

    rendered: list[tuple[SlotBundle, str, str]] = []
    for bundle in bundles:
        system_text, user_text = render_prompt(bundle)
        rendered.append((bundle, system_text, user_text))

    if dry_run:
        manifest = _write_dry_run(
            bundles=bundles,
            rendered=rendered,
            out_dir=out_root,
            model=model,
            prompt_version=prompt_version,
        )
        return {
            "__type": "header",
            "kind": "peer_gap",
            "scored_at": _now_iso(),
            "model": model,
            "prompt_version": prompt_version,
            "bundle_count": len(bundles),
            "scored_count": 0,
            "cache_hit_count": 0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "total_usd": 0.0,
            "mode": "dry-run",
            "dry_run_manifest": str(manifest.relative_to(out_root.parent)),
            "status": "complete",
        }

    cache = Cache(model, prompt_version, root=cache_root)
    if client is None:
        client = AnthropicClient(model=model)

    artifact_path = out_root / _ARTIFACT_NAME
    telemetry = PeerGapTelemetry(bundle_count=len(bundles))
    rows: list[dict[str, Any]] = []
    # Engine-owned dispatch state (cost_cap_hit / schema_error live
    # here so the header closure below can read them at persist time).
    dstate = DispatchState()

    def _header(*, status: str) -> dict[str, Any]:
        return {
            "__type": "header",
            "kind": "peer_gap",
            "scored_at": _now_iso(),
            "model": model,
            "prompt_version": prompt_version,
            "bundle_count": telemetry.bundle_count,
            "scored_count": telemetry.scored_count,
            "skipped_count": telemetry.skipped_count,
            "cache_hit_count": telemetry.cache_hit_count,
            "total_tokens_in": telemetry.total_tokens_in,
            "total_tokens_out": telemetry.total_tokens_out,
            "total_usd": round(telemetry.total_usd, 6),
            "mode": "api",
            "cost_cap": cost_cap,
            "cost_cap_hit": dstate.cost_cap_hit,
            "schema_error": (
                None if dstate.schema_error is None else str(dstate.schema_error)
            ),
            "status": status,
        }

    items = [
        DispatchItem(
            label=bundle.slot_key,
            system_text=system_text,
            user_text=user_text,
            context=bundle,
        )
        for bundle, system_text, user_text in rendered
    ]
    total_items = len(items)

    def _on_cached(
        idx: int, item: DispatchItem, payload: Any, cached: dict[str, Any]
    ) -> None:
        bundle = item.context
        tokens_in = int(cached.get("tokens_in", 0))
        tokens_out = int(cached.get("tokens_out", 0))
        telemetry.cache_hit_count += 1
        telemetry.total_tokens_in += tokens_in
        telemetry.total_tokens_out += tokens_out
        telemetry.scored_count += 1
        row = _build_row(bundle, payload, model=cached.get("model", model), cached=True)
        rows.append(row)
        _write_artifact(artifact_path, _header(status="running"), rows)
        if progress_callback is not None:
            progress_callback(
                {
                    "index": idx, "total": total_items,
                    "slot_key": bundle.slot_key,
                    "states": [v.state for v in bundle.states],
                    "cache_hit": True, "tokens_in": tokens_in,
                    "tokens_out": tokens_out, "usd": 0.0,
                    "running_usd": telemetry.total_usd,
                    "confidence": payload.get("confidence"),
                }
            )

    def _on_fresh(
        idx: int, item: DispatchItem, payload: Any, response: LLMResponse
    ) -> None:
        bundle = item.context
        telemetry.total_tokens_in += response.tokens_in
        telemetry.total_tokens_out += response.tokens_out
        telemetry.total_usd += response.usd
        telemetry.scored_count += 1
        row = _build_row(bundle, payload, model=response.model, cached=False)
        rows.append(row)
        _write_artifact(artifact_path, _header(status="running"), rows)
        if progress_callback is not None:
            progress_callback(
                {
                    "index": idx, "total": total_items,
                    "slot_key": bundle.slot_key,
                    "states": [v.state for v in bundle.states],
                    "cache_hit": False,
                    "tokens_in": response.tokens_in,
                    "tokens_out": response.tokens_out,
                    "usd": response.usd,
                    "running_usd": telemetry.total_usd,
                    "confidence": payload.get("confidence"),
                }
            )

    def _persist(status: str) -> dict[str, Any]:
        header = _header(status=status)
        _write_artifact(artifact_path, header, rows)
        return header

    return dispatch_batches(
        items,
        state=dstate,
        cache=cache,
        client=client,
        model=model,
        prompt_version=prompt_version,
        cost_cap=cost_cap,
        cost_cap_unit="slots",
        canonical_prompt=_canonical_prompt,
        validate_fn=lambda payload, item: _validate_output(
            payload, item.context.slot_key
        ),
        on_cached=_on_cached,
        on_fresh=_on_fresh,
        persist=_persist,
    )


def _build_row(
    bundle: SlotBundle,
    payload: dict[str, Any],
    *,
    model: str | None,
    cached: bool,
) -> dict[str, Any]:
    """Wrap an LLM payload + bundle context as one artifact row."""
    return {
        "slot_key": bundle.slot_key,
        "entity": bundle.entity,
        "element_name": bundle.element_name,
        "data_type": bundle.data_type,
        "edfi_standard_definition": bundle.edfi_standard_definition,
        "states_present": [v.state for v in bundle.states],
        "structural_tiers": {v.state: v.structural_tier for v in bundle.states},
        "consensus_concept": payload.get("consensus_concept"),
        "per_state_divergences": payload.get("per_state_divergences", []),
        "suggested_fills": payload.get("suggested_fills", []),
        "confidence": payload.get("confidence"),
        "confidence_rationale": payload.get("confidence_rationale"),
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "cached": cached,
    }
