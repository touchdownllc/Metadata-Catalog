"""Phase A binary-fact extraction harness.

Scope (strict, per `docs/archive/next-session/next-session-scoring-phase-a.md`):
- One fact: ``has_conditional_logic`` (spine-lens).
- One state: AZ first ~200 ``documented=True`` rows with
  ``source in {core, extension, unknown}``, deterministic order.
- One lens: spine.
- Entity-batched prompts, on-disk cache, strict JSON Schema validation,
  evidence-span substring validator, per-run cost cap, ``--dry-run``
  offline mode.

This module is the heart of the harness; it composes prompts
(``prompts/has_conditional_logic.md``), dispatches to the Anthropic
client (``client.py``), consults the cache (``cache.py``), validates
every response (``schema.py`` + ``validate_span``), and streams a JSONL
artifact per state under
``data/out/scoring/phase_a/{STATE}_{fact}.jsonl``.

CRITICAL: module-level ``run()`` is a plain function. Do NOT decorate it
with ``@click.command()`` — the Click wrapper lives in ``src/cli.py``.
See ``tests/test_ingest_az.py::TestCliWiring`` for the regression pattern.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import jsonschema

from src.models.element import ElementRecord, StateElements
from src.score.cache import Cache, cache_key
from src.score.client import DEFAULT_MODEL, AnthropicClient, LLMClient, LLMResponse
# Shared dispatch engine + atomic JSONL writer (issue #213 item 3).
# The writer keeps its historical private name; tests import
# `extract._write_artifact`.
from src.score.dispatch import (
    DispatchItem,
    DispatchState,
    dispatch_batches,
    write_jsonl_artifact as _write_artifact,
)
from src.states import SUPPORTED_STATES as CANONICAL_STATES
from src.score.schema import (
    ENUM_NO_EVIDENCE_VALUES,
    ENUM_SPANS_OPTIONAL_VALUES,
    ENUM_VALUED_FACTS,
    FACT_SOURCE_FILTERS,
    INT_VALUED_FACTS,
    INVERTED_POLARITY_BOOL_FACTS,
    ScoringSchemaError,
    expected_item_keys,
    fact_output_schema,
)
from src.models.spine import StateSpine
from src.utils.paths import (
    scoring_phase_a_artifact_path,
    scoring_phase_a_dir,
    state_elements_path,
    state_spine_path,
)

_LOGGER = logging.getLogger(__name__)


def _load_spine_for(state: str) -> StateSpine | None:
    """Load a state's spine for the prompt-label reconstruction (issue #184).

    Tolerant: returns ``None`` if the spine artifact is absent (hermetic
    tests, environments without ``data/spine/``), in which case the prompt
    falls back to ``first.domain`` — see ``_prompt_domain_label``.
    """
    path = state_spine_path(state)
    if not path.exists():
        return None
    return StateSpine.model_validate_json(path.read_text(encoding="utf-8"))

# Every LLM fact whose prompt file has been authored. The tuple grows
# one fact at a time through Phase B (spine-lens) and Phase C2
# (source-lens) — extending it without authoring `prompts/{fact}.md`
# makes `render_prompt` fail fast. Deterministic facts gate themselves
# via `deterministic.DETERMINISTIC_FACTS`.
SUPPORTED_FACTS = (
    # Spine-lens LLM facts (Phase B).
    "has_conditional_logic",
    "definition_is_implementable",
    "required_when_stated",
    "conditional_reporting_stated",
    "populations_or_scope_stated",
    "has_cross_entity_logic",
    "has_aggregation",
    "cross_entity_targets",
    # Phase F — NACHOS methodology tier 3 alongside has_aggregation.
    # Standard polarity bool fact; runs on both spine + source lenses
    # (no FACT_SOURCE_FILTERS entry). Grounds on business_rules_text +
    # definition_text + element_specific_rules like has_aggregation.
    "has_concatenation",
    # Source-lens LLM facts (Phase C2). Prompt files added one-at-a-time
    # through the per-fact ritual; SUPPORTED_FACTS lists the roster of
    # authored prompts, so a name appears here only once its .md file
    # has landed (render_prompt errors otherwise).
    "definition_adds_detail_beyond_edfi",
    "semantic_class",
    # Side-quest PR A (2026-04-28): merged from `state_narrows_edfi_scope`
    # + `state_broadens_edfi_scope` into a single mutually-exclusive enum.
    "state_scope_delta",
    "extension_is_necessary",
    "extension_is_standalone",
    # H1 — productization signal (enum8). Applies to both core and
    # extension rows (see hypotheses doc §2.2; Doug's 89%-of-rows
    # distribution covers both). Promoted from experiment-scoped to a
    # durable fact alongside the v2 two-axis merge (plan version 9);
    # surfaces in fact_provenance as a PRODUCTIZATION_SIGNAL_FACT,
    # orthogonal to the complexity cascade.
    "integration_class",
    # Integration Profile (docs/archive/nachos-v2-two-axis-plan.md §4
    # Mitigation 2): five-category documentation-style classifier
    # feeding the ``documentation_style_tier`` dimension. Runs on both
    # lenses.
    "documentation_style",
    # Issue #59 (2026-04-29): typed refinement of the SF
    # `divergent_unclear` / `divergent_explained` cluster. Source-lens
    # observability-only — registered in
    # ``LENS_OBSERVABILITY_FACTS["source"]`` so it lands in
    # `fact_provenance` and the Recommendations sheet without entering
    # any rule cascade. See ``score/rules.py`` for the registration.
    "sourcing_constraint_documented",
)
# Phase A was AZ-only. Phase B fans out to all four states and Phase C2
# widens to source-lens. Fact support still advances one prompt at a
# time — `render_prompt` errors until the .md file lands.
# Roster imported from the one canonical home (re-exported here — tests
# and callers historically read `extract.SUPPORTED_STATES`).
SUPPORTED_STATES = CANONICAL_STATES
SUPPORTED_LENSES = ("spine", "source")

# Bump when any prompt template or rules change — invalidates cache by
# design. Phase B keeps the Phase A version string until a prompt edit
# post-Phase A lands; bumping here forces a full re-spend across every
# cached fact/state pair.
PROMPT_VERSION = "phase-a.v1"

_PROMPTS_DIR = Path(__file__).with_name("prompts")
_PROMPT_PATH = _PROMPTS_DIR / "has_conditional_logic.md"  # Phase A back-compat.
_SYSTEM_DELIM = "\n# USER\n"
_SYSTEM_HEADER = "# SYSTEM\n\n"


def prompt_path_for(fact: str) -> Path:
    """Return the prompt-file path for ``fact``.

    Raises ``FileNotFoundError`` for facts declared in ``SUPPORTED_FACTS``
    but not yet authored — callers should surface a clear error rather
    than silently falling back to another prompt (cache-wise, silent
    fallback would pollute cache entries for the wrong fact).
    """
    path = _PROMPTS_DIR / f"{fact}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"prompt file not found for fact {fact!r}: expected {path}"
        )
    return path


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _load_template(prompt_path: Path = _PROMPT_PATH) -> tuple[str, str]:
    """Return (system_text, user_template) split from the prompt file.

    The .md file uses a ``# SYSTEM`` / ``# USER`` split so the system
    prologue (rules + examples) can be prompt-cached independently from
    the per-entity user block. Leading `{# ... #}` Jinja-style comments
    are stripped before the split — they exist only for authoring
    context and should not reach the model.
    """
    raw = prompt_path.read_text(encoding="utf-8")
    # Strip leading {# ... #} author comments (Jinja-style) if present.
    raw = re.sub(r"^\s*\{#.*?#\}\s*", "", raw, count=1, flags=re.DOTALL)
    if _SYSTEM_DELIM not in raw:
        raise RuntimeError(
            f"prompt template missing '{_SYSTEM_DELIM.strip()}' marker: {prompt_path}"
        )
    head, user = raw.split(_SYSTEM_DELIM, 1)
    if head.startswith(_SYSTEM_HEADER):
        head = head[len(_SYSTEM_HEADER):]
    return head.strip() + "\n", user.lstrip("\n")


def _format_element_block(
    records: list[ElementRecord], *, lens: str = "spine"
) -> str:
    """Render the per-element block injected into the user prompt.

    Keeps the ordering explicit — LLM responses must match input order,
    and the deterministic-sort step upstream is the only ordering
    guarantee we hand the model.

    For ``lens="source"``, the block additionally carries
    ``edfi_standard_definition``, ``source``, and ``extension_name`` —
    source-lens LLM facts need the Ed-Fi baseline (comparison against
    the state's text) and the extension/core classification. Spine-lens
    blocks stay unchanged so Phase B cache hashes still match.
    """
    lines: list[str] = []
    for rec in records:
        lines.append(f"  - element_name: {rec.element_name}")
        lines.append(f"    data_type: {rec.data_type or '(none)'}")
        lines.append(
            f"    definition_text: {rec.definition_text.strip() if rec.definition_text else '(none)'}"
        )
        lines.append(
            f"    element_specific_rules: {rec.element_specific_rules.strip() if rec.element_specific_rules else '(none)'}"
        )
        if lens == "source":
            edfi_def = (rec.edfi_standard_definition or "").strip()
            lines.append(
                f"    edfi_standard_definition: {edfi_def if edfi_def else '(none)'}"
            )
            lines.append(f"    source: {rec.source}")
            lines.append(
                f"    extension_name: {rec.extension_name or '(none)'}"
            )
    return "\n".join(lines)


def render_prompt(
    entity: str,
    records: list[ElementRecord],
    state: str,
    *,
    prompt_path: Path = _PROMPT_PATH,
    fact: str | None = None,
    lens: str = "spine",
    domain_label: str | None = None,
) -> tuple[str, str]:
    """Render (system_text, user_text) for one entity batch.

    ``prompt_path`` wins over ``fact`` when both are set (estimator uses
    this to point at a proxy template). When only ``fact`` is provided,
    the per-fact template is resolved via ``prompt_path_for``. ``lens``
    gates whether the per-element block includes source-lens-only
    fields (``edfi_standard_definition``, ``source``, ``extension_name``).

    ``domain_label`` is the value substituted into the prompt's ``{domain}``
    slot. It is decoupled from the stored ``ElementRecord.domain`` field
    (issue #184): the prompt conveys the Ed-Fi domain, which used to live
    in ``domain`` for the spine lens before ``domain`` became the pure
    Source Area column. Callers pass the byte-identical legacy label
    (``shared._entity_domain`` for spine core/extension batches) so the
    prompt cache stays warm and reproducible. When ``None`` (e.g. the
    source lens, or direct test callers), it falls back to
    ``first.domain`` — the historical behavior.
    """
    if not records:
        raise ValueError("render_prompt requires at least one record")
    if prompt_path is _PROMPT_PATH and fact is not None:
        prompt_path = prompt_path_for(fact)
    system_text, user_template = _load_template(prompt_path)
    first = records[0]
    entity_rules = first.business_rules_text
    domain_value = domain_label if domain_label is not None else first.domain
    user_text = user_template.format_map(
        {
            "entity": entity,
            "domain": domain_value or "(none)",
            "state": state,
            "entity_business_rules": entity_rules.strip() if entity_rules else "(none)",
            "elements_block": _format_element_block(records, lens=lens),
        }
    )
    return system_text, user_text


# ---------------------------------------------------------------------------
# Evidence-span validator
# ---------------------------------------------------------------------------


# Curly quotes and apostrophes (U+2018/19/1A/1B, U+201C/1D/1E/1F) fold to
# their straight ASCII equivalents before substring match. This is NOT
# fuzzy matching — it's character-class normalization. The span text and
# the source narrative still have to match byte-for-byte after the fold;
# we're only tolerating the fact that Word / PDF exports and LLM outputs
# use different Unicode code points for what renders as the same glyph.
# Phase B measured this across 4 states: 15/15 remaining hallucinations
# (after the ≥1-valid-span relaxation) were curly vs straight quote
# drift. After folding: 0 false-negative downgrades on those rows.
_QUOTE_FOLD_MAP = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"',
}


def _normalize(s: str) -> str:
    """Normalize text for substring-match validation.

    Applies (in order): curly-quote fold, case fold, whitespace collapse.
    Keeps the validator principled — no paraphrase tolerance, no
    stemming, no edit-distance. Just character-class normalization
    for glyphs that render identically but use different code points.
    """
    return re.sub(r"\s+", " ", s.translate(_QUOTE_FOLD_MAP).casefold()).strip()


def validate_span(span: str, record: ElementRecord) -> bool:
    """Return True if ``span`` is a (normalized) substring of the record's narrative.

    Case-insensitive, whitespace-collapsed substring match with curly
    quotes folded to straight. No fuzzy matching — paraphrases still
    fail and downgrade the fact to ``unknown``. See
    `docs/archive/scoring-plan.md § 6.3` for the policy rationale.
    """
    haystack = " ".join(
        filter(
            None,
            [
                record.definition_text or "",
                record.business_rules_text or "",
                record.element_specific_rules or "",
            ],
        )
    )
    if not haystack.strip():
        return False
    norm_span = _normalize(span)
    if not norm_span:
        return False
    return norm_span in _normalize(haystack)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class RunConfig:
    fact: str = "has_conditional_logic"
    state: str = "AZ"
    lens: str = "spine"
    limit: int = 200
    model: str = DEFAULT_MODEL
    dry_run: bool = False
    cost_cap: float = 1.0
    prompt_version: str = PROMPT_VERSION
    elements_path: Path | None = None
    out_dir: Path | None = None
    cache_root: Path | None = None


@dataclass
class RunTelemetry:
    record_count: int = 0
    scored_count: int = 0
    skipped_count: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_usd: float = 0.0
    cache_hit_count: int = 0
    downgrade_count: int = 0
    batch_split_count: int = 0
    entities_processed: int = 0


def load_phase_a_records(
    state: str,
    lens: str,
    *,
    elements_path: Path | None = None,
    limit: int | None = None,
    source_filter: tuple[str, ...] | None = None,
) -> list[ElementRecord]:
    """Load + filter + sort the Phase A record pool.

    Filters to ``source in {core, extension, unknown}`` (skips ``filtered``
    placeholders) AND ``(documented=True OR documentation_source !=
    "source_doc")``. The disjunction lets swagger-backfilled rows
    (issue #70 entity-level, ``documentation_source="swagger"``) and
    leaf-borrow rows (issue #147 sub-collection cross-lens borrow,
    ``documentation_source="swagger_leaf"``) flow through extraction
    even though they carry ``documented=False`` under the v21
    close-out posture — the v21 design called for per-row scoring on
    those rows so the sidecar carries their tier; the
    ``documentation_source != "source_doc"`` clause makes that
    explicit. Spine-lens rows whose source doc is silent on the slot
    keep ``documentation_source="source_doc"`` and stay filtered out
    (those belong in the spine-anchored gap surface, not the regular
    scoring pool).

    ``source_filter`` optionally narrows the pool further — e.g.
    ``("extension",)`` for facts whose prompts only apply to extension
    rows (Phase D carryover #1). The ingest-level ``{core, extension,
    unknown}`` pre-filter still runs first, so ``source_filter`` must
    be a subset of that. Sorted deterministically by ``(entity,
    element_name)`` ASC so subsets are stable across runs and
    byte-identical replay holds.
    """
    path = elements_path or state_elements_path(state, lens)  # type: ignore[arg-type]
    data = StateElements.model_validate_json(path.read_text(encoding="utf-8"))
    keep = [
        r for r in data.elements
        if r.source in ("core", "extension", "unknown")
        and (
            r.documented
            or getattr(r, "documentation_source", "source_doc") != "source_doc"
        )
    ]
    if source_filter is not None:
        allowed = set(source_filter)
        keep = [r for r in keep if r.source in allowed]
    keep.sort(key=lambda r: (r.entity, r.element_name))
    if limit is not None:
        keep = keep[:limit]
    return keep


def _group_by_entity(records: Iterable[ElementRecord]) -> list[tuple[str, list[ElementRecord]]]:
    """Preserve original order while grouping — records are pre-sorted."""
    groups: dict[str, list[ElementRecord]] = {}
    order: list[str] = []
    for rec in records:
        if rec.entity not in groups:
            groups[rec.entity] = []
            order.append(rec.entity)
        groups[rec.entity].append(rec)
    return [(e, groups[e]) for e in order]


# Public alias — Phase B's estimator + multi-fact runner reuse the same
# entity-grouping as the extractor so batch boundaries stay consistent.
group_by_entity = _group_by_entity


def _schema_for(fact: str) -> dict[str, Any]:
    """Return the jsonschema dict for ``fact``'s response payload.

    Bool facts share one shape; count-int facts (``cross_entity_targets``)
    use ``integer``; enum facts (``semantic_class``) pair ``string``
    with an explicit ``enum`` to lock the token set.
    """
    if fact in ENUM_VALUED_FACTS:
        return fact_output_schema(
            fact, value_type="string", value_enum=ENUM_VALUED_FACTS[fact]
        )
    value_type = "integer" if fact in INT_VALUED_FACTS else "boolean"
    return fact_output_schema(fact, value_type=value_type)


def _validate_payload(
    payload: Any, fact: str = "has_conditional_logic"
) -> list[dict[str, Any]]:
    """Validate ``payload`` against ``fact``'s schema; log extra-key drift.

    Structural errors (missing key, wrong type, bad enum) still raise
    ``ScoringSchemaError``. Additional unexpected keys are logged at
    WARNING — the cold-run TX fanout hit a ``data_type`` hallucination
    on every item in one batch and crashed 94% of the way through a
    fact. Surviving that drift, with telemetry, is worth more than the
    strict-reject discipline.
    """
    try:
        jsonschema.validate(payload, _schema_for(fact))
    except jsonschema.ValidationError as exc:
        raise ScoringSchemaError(
            f"LLM response failed schema validation: {exc.message}",
            payload=payload,
        ) from exc
    allowed = expected_item_keys(fact)
    drift_counter: dict[str, int] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        extra = [k for k in item.keys() if k not in allowed]
        for key in extra:
            drift_counter[key] = drift_counter.get(key, 0) + 1
    if drift_counter:
        summary = ", ".join(
            f"{key} x{count}" for key, count in sorted(drift_counter.items())
        )
        _LOGGER.warning(
            "LLM response for fact %r carried %d unexpected key(s) across items: %s. "
            "Run continues; keys are ignored.",
            fact,
            sum(drift_counter.values()),
            summary,
        )
    return list(payload)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_dry_run(
    *,
    state: str,
    fact: str,
    lens: str,
    model: str,
    prompt_version: str,
    batches: list[tuple[str, list[ElementRecord], str, str]],
    out_dir: Path,
) -> Path:
    """Write prompts to ``<out_dir>/dry_run/`` + a ``manifest.json`` index.

    Returns the manifest path.
    """
    dry_dir = out_dir / "dry_run"
    dry_dir.mkdir(parents=True, exist_ok=True)
    # Clear stale prompt files from a prior dry-run so the manifest stays authoritative.
    for old in dry_dir.glob("*.prompt.md"):
        old.unlink()
    manifest: dict[str, Any] = {
        "state": state,
        "fact": fact,
        "lens": lens,
        "model": model,
        "prompt_version": prompt_version,
        "batches": [],
    }
    for idx, (entity, records, system_text, user_text) in enumerate(batches):
        name = f"{idx:03d}_{_safe_filename(entity)}.prompt.md"
        path = dry_dir / name
        text = f"# SYSTEM\n\n{system_text.rstrip()}\n\n# USER\n\n{user_text.rstrip()}\n"
        path.write_text(text, encoding="utf-8")
        manifest["batches"].append(
            {
                "index": idx,
                "entity": entity,
                "element_count": len(records),
                "prompt_file": name,
                "cache_key": cache_key(
                    _canonical_prompt(system_text, user_text),
                    model,
                    prompt_version,
                ),
                "element_names": [r.element_name for r in records],
            }
        )
    manifest_path = dry_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _safe_filename(entity: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", entity) or "entity"


def _canonical_prompt(system_text: str, user_text: str) -> str:
    """Canonical prompt bytes used to derive cache keys.

    Stable separator, stable whitespace — the Anthropic API sees the
    same content, and the cache key derives from these same bytes.
    """
    return f"<<SYSTEM>>\n{system_text.rstrip()}\n<<USER>>\n{user_text.rstrip()}\n"


def _process_payload(
    payload: list[dict[str, Any]],
    records: list[ElementRecord],
    fact: str = "has_conditional_logic",
    prompt_version: str = PROMPT_VERSION,
) -> tuple[list[dict[str, Any]], int]:
    """Turn a schema-valid LLM response into per-record artifact rows.

    Returns ``(rows, downgrades)`` where ``rows`` is one artifact dict
    per input record and ``downgrades`` counts validated-value
    downgrades (``validated_value=None``).

    Downgrade semantics by fact kind:

    - **Bool facts** — a True claim keeps ``validated_value=True`` if
      **at least one** span validates as a verbatim substring of the
      record's narrative. Only ``zero valid spans`` forces a
      ``hallucinated_span`` downgrade. This is a Phase B relaxation
      of Phase A's all-valid rule: Phase B's multi-state fanout
      surfaced common patterns where the LLM quotes multiple
      supporting spans and one near-miss (whitespace around ``+`` /
      ``-``, curly vs straight quotes, apostrophe-in-possessive)
      fails the strict substring matcher. Penalizing thorough
      quoting is the wrong incentive. Rows where any span failed
      validation carry ``any_invalid_spans=True`` so Phase D's
      review queue can surface them even when the fact was kept.
      False claims still downgrade on any emitted span
      (``spans_on_false_claim``).
    - **Int-valued facts** (``cross_entity_targets``) stay strict:
      ``validated_spans_valid_count == llm_value``. Count claims
      carry more semantic weight than narrative support, so a
      mismatch stays a ``count_span_mismatch`` downgrade.

    Phase A's ``test_downgrade_flow_preserves_raw`` and
    ``test_red_team_catches_hallucination`` tests still pass: their
    scenarios have zero valid spans, so the ``any`` rule still
    downgrades.
    """
    by_name = {item["element_name"]: item for item in payload}
    rows: list[dict[str, Any]] = []
    downgrades = 0
    is_int = fact in INT_VALUED_FACTS
    is_enum = fact in ENUM_VALUED_FACTS
    for rec in records:
        entry = by_name.get(rec.element_name)
        if entry is None:
            rows.append(
                {
                    "record_key": f"{rec.state}|{rec.entity}|{rec.element_name}",
                    "entity": rec.entity,
                    "element_name": rec.element_name,
                    "llm_value": None,
                    "validated_value": None,
                    "spans": [],
                    "confidence": "low",
                    "downgrade_reason": "missing_from_response",
                    "any_invalid_spans": False,
                    "model": None,
                    "prompt_version": prompt_version,
                }
            )
            downgrades += 1
            continue

        raw_spans = list(entry.get("spans") or [])
        validated_spans = [
            {"text": span, "valid": validate_span(span, rec)}
            for span in raw_spans
        ]
        any_invalid = any(not s["valid"] for s in validated_spans)

        downgrade_reason: str | None = None
        validated_value: bool | int | str | None

        if is_int:
            llm_value: bool | int | str = int(entry[fact])
            validated_value = llm_value
            valid_count = sum(1 for s in validated_spans if s["valid"])
            if llm_value == 0:
                # Count == 0 must ship zero spans — any span is an
                # inconsistent payload, same as a False bool claim.
                if raw_spans:
                    validated_value = None
                    downgrade_reason = "spans_on_zero_count"
                    downgrades += 1
            else:
                if valid_count != llm_value:
                    validated_value = None
                    downgrade_reason = "count_span_mismatch"
                    downgrades += 1
        elif is_enum:
            llm_value = str(entry[fact])
            validated_value = llm_value
            # Enum-polarity generalization.
            #
            # - Labels in ``ENUM_NO_EVIDENCE_VALUES[fact]`` are structural
            #   / no-evidence defaults — ``spans`` MUST be empty.
            #   ``semantic_class="not_applicable"`` (structural, no core
            #   anchor), ``integration_class="unknown"`` (narrative
            #   doesn't describe the integration shape), and
            #   ``documentation_style="unspecified"`` (narrative absent
            #   or thin) live here. Non-empty spans downgrade with
            #   reason ``spans_on_{label}``.
            # - Labels in ``ENUM_SPANS_OPTIONAL_VALUES[fact]`` accept
            #   paraphrase spans but empty is also legitimate —
            #   ``semantic_class="aligned"`` is the canonical example
            #   (thin narrative rows have no state text to quote).
            # - All remaining labels are the affirmative default: at
            #   least one valid span required, no-span rows downgrade
            #   with ``hallucinated_span`` — e.g. every affirmative
            #   documentation_style and integration_class label.
            no_evidence = ENUM_NO_EVIDENCE_VALUES.get(fact, frozenset())
            spans_optional = ENUM_SPANS_OPTIONAL_VALUES.get(fact, frozenset())
            if llm_value in no_evidence:
                if raw_spans:
                    validated_value = None
                    downgrade_reason = f"spans_on_{llm_value}"
                    downgrades += 1
            elif llm_value in spans_optional:
                if validated_spans and not any(s["valid"] for s in validated_spans):
                    validated_value = None
                    downgrade_reason = "hallucinated_span"
                    downgrades += 1
            else:
                if not validated_spans or not any(s["valid"] for s in validated_spans):
                    validated_value = None
                    downgrade_reason = "hallucinated_span"
                    downgrades += 1
        else:
            llm_value = bool(entry[fact])
            validated_value = llm_value
            is_inverted = fact in INVERTED_POLARITY_BOOL_FACTS
            # Default polarity: True is the affirmative claim (requires
            # spans); False is the denial (spans must be empty).
            # Inverted polarity (e.g. ``extension_is_standalone``): True
            # is the "no evidence needed" default, spans must be empty;
            # False is the affirmative dependency claim and requires
            # >= 1 valid span.
            affirmative = (not llm_value) if is_inverted else llm_value
            if affirmative:
                if not validated_spans or not any(s["valid"] for s in validated_spans):
                    validated_value = None
                    downgrade_reason = "hallucinated_span"
                    downgrades += 1
            else:
                if raw_spans:
                    validated_value = None
                    downgrade_reason = (
                        "spans_on_default_true" if is_inverted
                        else "spans_on_false_claim"
                    )
                    downgrades += 1

        rows.append(
            {
                "record_key": f"{rec.state}|{rec.entity}|{rec.element_name}",
                "entity": rec.entity,
                "element_name": rec.element_name,
                "llm_value": llm_value,
                "validated_value": validated_value,
                "spans": validated_spans,
                "confidence": entry["confidence"],
                "downgrade_reason": downgrade_reason,
                "any_invalid_spans": any_invalid,
                "model": None,
                "prompt_version": prompt_version,
            }
        )
    return rows, downgrades


ProgressCallback = Callable[[dict[str, Any]], None]


def _prompt_domain_label(
    group_records: list[ElementRecord],
    lens: str,
    spine: "StateSpine | None",
) -> str | None:
    """Return the byte-identical legacy ``{domain}`` prompt label for a batch.

    Issue #184 made ``ElementRecord.domain`` a pure Source Area column, so
    spine-lens core/extension rows no longer carry the Ed-Fi domain that
    the prompt used to render. To keep the prompt cache warm/reproducible
    we reconstruct the historical label here:

    - spine lens, core/extension batch: ``shared._entity_domain(entity)`` —
      exactly what the spine assembler wrote into ``domain`` pre-#184.
    - spine lens, unknown-source (hybrid-append) batch: those rows kept
      their Source Area in ``domain``, so ``first.domain`` is unchanged.
    - source lens: ``first.domain`` (Source Area) was always the label.

    Returns ``None`` to signal "fall back to ``first.domain``" (handled by
    ``render_prompt``) — used for the source lens, unknown-source spine
    batches, and when no spine is available (hermetic tests).
    """
    first = group_records[0]
    if lens == "spine" and spine is not None and first.source in ("core", "extension"):
        from src.ingest.shared import _entity_domain

        return _entity_domain(first.entity, spine)
    return None


def render_batches_for(
    *,
    fact: str,
    state: str,
    lens: str,
    limit: int | None = None,
    elements_path: Path | None = None,
    spine: "StateSpine | None" = None,
) -> list[tuple[str, list[ElementRecord], str, str]]:
    """Render every (entity, system_text, user_text) batch for a triple.

    Single source of truth for "given (fact, state, lens, limit, path),
    what would we send to the LLM?". Both ``run()`` (sync path, before
    the LLM dispatch loop) and the Anthropic Batch API path
    (``score.batch_runner``) use this — calling it twice for the same
    inputs produces byte-identical batches, which is what makes the
    cache key stable across sync and batch modes.

    The returned tuple shape ``(entity, group_records, system_text,
    user_text)`` is what ``run()``'s loop already expects, so the sync
    path is one line away from this helper. The batch path uses
    ``system_text`` + ``user_text`` directly to compute cache keys
    (``cache_key(_canonical_prompt(...), model, prompt_version)``) and
    to construct ``BatchRequestSpec`` rows.

    ``spine`` is used only to reconstruct the legacy ``{domain}`` prompt
    label for spine-lens batches (issue #184 — see ``_prompt_domain_label``).
    Production callers pass it; hermetic tests omit it and fall back to
    ``first.domain``.
    """
    _require_supported(fact, state, lens)
    records = load_phase_a_records(
        state,
        lens,
        elements_path=elements_path,
        limit=limit,
        source_filter=FACT_SOURCE_FILTERS.get(fact),
    )
    groups = _group_by_entity(records)
    prompt_path = prompt_path_for(fact)
    batches: list[tuple[str, list[ElementRecord], str, str]] = []
    for entity, group_records in groups:
        system_text, user_text = render_prompt(
            entity, group_records, state, prompt_path=prompt_path, lens=lens,
            domain_label=_prompt_domain_label(group_records, lens, spine),
        )
        batches.append((entity, group_records, system_text, user_text))
    return batches


def run(
    *,
    fact: str = "has_conditional_logic",
    state: str = "AZ",
    lens: str = "spine",
    limit: int | None = 200,
    dry_run: bool = False,
    cost_cap: float = 1.0,
    model: str = DEFAULT_MODEL,
    prompt_version: str = PROMPT_VERSION,
    elements_path: Path | None = None,
    out_dir: Path | None = None,
    cache_root: Path | None = None,
    client: LLMClient | None = None,
    progress_callback: ProgressCallback | None = None,
    validate_only: int | None = None,
) -> dict[str, Any]:
    """Execute the Phase A harness and return the run header dict.

    ``client`` is accepted for tests (monkeypatching). Normal callers
    leave it None so a fresh ``AnthropicClient`` is constructed
    (skipped entirely under ``--dry-run``).

    ``progress_callback`` fires once per entity batch with a dict
    carrying ``index``, ``total``, ``entity``, ``element_count``,
    ``cache_hit``, ``tokens_in``, ``tokens_out``, ``usd``,
    ``running_usd``, and ``downgrades_in_batch``. Used by the CLI for
    per-batch progress output; tests pass None.

    ``validate_only`` (Phase D carryover #3): when set to a positive
    int, clamps the record pool to that many rows and writes the
    artifact to ``{out_dir}/validate_only/`` instead of the normal
    phase_a/ destination. The header carries ``mode="validate-only"``
    so the CLI can surface a downgrade-reason histogram without
    polluting the real artifact on disk. Runs through the same
    LLM+validator path so polarity / schema bugs surface in 30
    seconds of budgeted API calls, not after a full-state fanout.

    Deterministic facts (``definition_present``,
    ``business_rules_present``, ``data_type_canonical``) short-circuit
    to ``deterministic.run()`` — no LLM, no cache, no ``dry_run`` /
    ``cost_cap`` semantics. Same artifact shape; only the header's
    ``mode`` reads ``"deterministic"``.
    """
    # Deterministic-fact short-circuit: no LLM, no cache, no prompt.
    # Must run before `_require_supported` since SUPPORTED_FACTS is the
    # LLM-fact gate; deterministic facts gate themselves via their own
    # DETERMINISTIC_FACTS tuple in `deterministic.py`.
    from src.score.deterministic import DETERMINISTIC_FACTS
    if fact in DETERMINISTIC_FACTS:
        if lens.lower() not in SUPPORTED_LENSES:
            print(
                f"error: --lens {lens!r} not supported (only: {SUPPORTED_LENSES})",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if state.upper() not in SUPPORTED_STATES:
            print(
                f"error: --state {state!r} not supported (only: {SUPPORTED_STATES})",
                file=sys.stderr,
            )
            raise SystemExit(2)
        from src.score.deterministic import run as run_deterministic
        return run_deterministic(
            fact=fact,
            state=state,
            lens=lens,
            limit=limit,
            out_dir=out_dir,
            elements_path=elements_path,
        )
    _require_supported(fact, state, lens)
    # ``validate_only`` clamps the record pool and redirects the artifact
    # to a scratch dir so sanity runs don't overwrite the committed
    # per-state JSONL. The header below carries ``mode="validate-only"``
    # so the CLI can distinguish the two flows.
    validate_only_mode = validate_only is not None and validate_only > 0
    effective_limit = min(validate_only, limit or validate_only) if validate_only_mode else limit
    cfg = RunConfig(
        fact=fact,
        state=state,
        lens=lens,
        limit=effective_limit,
        model=model,
        dry_run=dry_run,
        cost_cap=cost_cap,
        prompt_version=prompt_version,
        elements_path=elements_path,
        out_dir=out_dir,
        cache_root=cache_root,
    )

    # Render prompts up-front — cache keys and dry-run manifests depend
    # on it. ``render_batches_for`` is the shared seam used by both this
    # sync path and ``score.batch_runner.submit_run_all``; calling it
    # twice for the same triple yields byte-identical batches so the
    # cache hits regardless of which path populated it. The spine is
    # passed so spine-lens batches reconstruct the legacy {domain} prompt
    # label (issue #184) and the cache stays warm.
    batches = render_batches_for(
        fact=fact,
        state=state,
        lens=lens,
        limit=effective_limit,
        elements_path=elements_path,
        spine=_load_spine_for(state) if lens == "spine" else None,
    )

    out_root = out_dir or scoring_phase_a_dir()
    out_root.mkdir(parents=True, exist_ok=True)

    record_count = sum(len(group_records) for _, group_records, _, _ in batches)
    telemetry = RunTelemetry(record_count=record_count)

    if dry_run:
        manifest = _write_dry_run(
            state=state,
            fact=fact,
            lens=lens,
            model=model,
            prompt_version=prompt_version,
            batches=batches,
            out_dir=out_root,
        )
        header = {
            "__type": "header",
            "state": state,
            "lens": lens,
            "fact": fact,
            "scored_at": _now_iso(),
            "model": model,
            "prompt_version": prompt_version,
            "record_count": telemetry.record_count,
            "scored_count": 0,
            "skipped_count": telemetry.record_count,
            "entities_processed": len(batches),
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "total_usd": 0.0,
            "cache_hit_count": 0,
            "downgrade_count": 0,
            "mode": "dry-run",
            "dry_run_manifest": str(manifest.relative_to(out_root.parent)),
        }
        _LOGGER.info("phase-a dry-run complete: %d batches written to %s", len(batches), manifest.parent)
        return header

    cache = Cache(model, prompt_version, root=cache_root)
    if client is None:
        client = AnthropicClient(model=model)

    artifact_path = scoring_phase_a_artifact_path(state, fact, lens=lens)
    if out_dir is not None:
        artifact_path = out_dir / artifact_path.name
    if validate_only_mode:
        # Redirect to a scratch dir so sanity runs don't clobber the
        # real per-state JSONL. The validate-only artifact carries the
        # same shape so analysts can inspect downgrade-reason samples.
        artifact_path = artifact_path.parent / "validate_only" / artifact_path.name
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    rows_accumulator: list[dict[str, Any]] = []
    # Engine-owned dispatch state (cost_cap_hit / schema_error live
    # here so the header closure below can read them at persist time).
    dstate = DispatchState()

    def _current_header(*, status: str) -> dict[str, Any]:
        """Build an artifact header snapshot.

        ``status`` is one of: ``"running"`` (provisional, mid-stream),
        ``"complete"`` (clean finish), ``"aborted"`` (crash or cost-cap).
        Runner's skip-if-complete check only fires when
        ``status == "complete"`` AND ``scored_count == record_count`` —
        both gates together rule out resuming from a partially-streamed
        artifact that happens to have all rows present.
        """
        return {
            "__type": "header",
            "state": state,
            "lens": lens,
            "fact": fact,
            "scored_at": _now_iso(),
            "model": model,
            "prompt_version": prompt_version,
            "record_count": telemetry.record_count,
            "scored_count": telemetry.scored_count,
            "skipped_count": telemetry.record_count - telemetry.scored_count,
            "entities_processed": telemetry.entities_processed,
            "total_tokens_in": telemetry.total_tokens_in,
            "total_tokens_out": telemetry.total_tokens_out,
            "total_usd": round(telemetry.total_usd, 6),
            "cache_hit_count": telemetry.cache_hit_count,
            "downgrade_count": telemetry.downgrade_count,
            "mode": "validate-only" if validate_only_mode else "api",
            "cost_cap_hit": dstate.cost_cap_hit,
            "schema_error": (
                None if dstate.schema_error is None else str(dstate.schema_error)
            ),
            "status": status,
        }

    items = [
        DispatchItem(
            label=entity,
            system_text=system_text,
            user_text=user_text,
            context=group_records,
        )
        for entity, group_records, system_text, user_text in batches
    ]
    total_batches = len(items)

    def _on_cached(
        idx: int, item: DispatchItem, payload: Any, cached: dict[str, Any]
    ) -> None:
        group_records = item.context
        rows, downgrades = _process_payload(
            payload, group_records, fact, prompt_version=prompt_version
        )
        telemetry.cache_hit_count += 1
        batch_tokens_in = int(cached.get("tokens_in", 0))
        batch_tokens_out = int(cached.get("tokens_out", 0))
        telemetry.total_tokens_in += batch_tokens_in
        telemetry.total_tokens_out += batch_tokens_out
        telemetry.downgrade_count += downgrades
        telemetry.scored_count += len(group_records)
        telemetry.entities_processed += 1
        for row in rows:
            row["model"] = cached.get("model", model)
        rows_accumulator.extend(rows)
        # Stream: rewrite the artifact after every completed batch so a
        # SIGKILL preserves all work up to the last successful batch
        # (see §5a of the Phase B brief).
        _write_artifact(
            artifact_path, _current_header(status="running"), rows_accumulator
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "index": idx,
                    "total": total_batches,
                    "entity": item.label,
                    "element_count": len(group_records),
                    "cache_hit": True,
                    "tokens_in": batch_tokens_in,
                    "tokens_out": batch_tokens_out,
                    "usd": 0.0,
                    "running_usd": telemetry.total_usd,
                    "downgrades_in_batch": downgrades,
                }
            )

    def _on_fresh(
        idx: int, item: DispatchItem, payload: Any, response: LLMResponse
    ) -> None:
        group_records = item.context
        rows, downgrades = _process_payload(
            payload, group_records, fact, prompt_version=prompt_version
        )
        telemetry.total_tokens_in += response.tokens_in
        telemetry.total_tokens_out += response.tokens_out
        telemetry.total_usd += response.usd
        telemetry.downgrade_count += downgrades
        telemetry.scored_count += len(group_records)
        telemetry.entities_processed += 1
        if progress_callback is not None:
            progress_callback(
                {
                    "index": idx,
                    "total": total_batches,
                    "entity": item.label,
                    "element_count": len(group_records),
                    "cache_hit": False,
                    "tokens_in": response.tokens_in,
                    "tokens_out": response.tokens_out,
                    "usd": response.usd,
                    "running_usd": telemetry.total_usd,
                    "downgrades_in_batch": downgrades,
                }
            )
        for row in rows:
            row["model"] = response.model
        rows_accumulator.extend(rows)
        # Stream: rewrite the artifact after every completed batch
        # (§5a). On crash, the file has every row up to the last
        # successful batch plus a header where status="running".
        _write_artifact(
            artifact_path, _current_header(status="running"), rows_accumulator
        )

    def _persist(status: str) -> dict[str, Any]:
        header = _current_header(status=status)
        _write_artifact(artifact_path, header, rows_accumulator)
        return header

    return dispatch_batches(
        items,
        state=dstate,
        cache=cache,
        client=client,
        model=model,
        prompt_version=prompt_version,
        cost_cap=cost_cap,
        cost_cap_unit="entities",
        canonical_prompt=_canonical_prompt,
        validate_fn=lambda payload, item: _validate_payload(payload, fact),
        on_cached=_on_cached,
        on_fresh=_on_fresh,
        persist=_persist,
    )


def _require_supported(fact: str, state: str, lens: str) -> None:
    if fact not in SUPPORTED_FACTS:
        print(
            f"error: --fact {fact!r} not supported (only: {', '.join(SUPPORTED_FACTS)})",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if state.upper() not in SUPPORTED_STATES:
        print(
            f"error: --state {state!r} not supported (only: {', '.join(SUPPORTED_STATES)})",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if lens.lower() not in SUPPORTED_LENSES:
        print(
            f"error: --lens {lens!r} not supported (only: {', '.join(SUPPORTED_LENSES)})",
            file=sys.stderr,
        )
        raise SystemExit(2)
