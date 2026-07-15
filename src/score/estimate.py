"""Phase B budget-gating estimator — cost estimate without LLM spend.

Renders every prompt batch via the existing extractor's prompt pipeline,
counts input tokens per batch (via
``anthropic.Anthropic().messages.count_tokens()`` when an API key is
present, else a ``chars/4`` fallback), and projects output tokens from
Phase A's measured **18.6% out/in ratio** (6961 output / 37410 input on
AZ 200-row single-fact cold run). Applies ``client._PRICING_USD_PER_MTOK``
as the single source of truth for USD — the estimator and the live
harness share one pricing table.

CRITICAL: this module never calls ``messages.create()``. It is safe to
run without spending a cent. ``count_tokens`` is a non-billable endpoint
but still requires ``ANTHROPIC_API_KEY`` for auth; fall back to
``chars/4`` when unavailable or on transient error.

Rationale for the range band
----------------------------

Phase A measured *one* fact's out/in ratio. Other LLM facts will differ
(``cross_entity_targets`` with multi-span arrays will run hotter than
``has_conditional_logic``'s single span). The honest posture for Phase B
commit 1 is **point ± 40%** on output tokens. Post-Phase B, when ≥3
facts have measured ratios, the band tightens to ±15% (see
``docs/archive/next-session/next-session-scoring-phase-b.md § 3``).

Proxy-template caveat
---------------------

Phase B authors each LLM fact's prompt in a separate commit. Until each
prompt ships, the estimator uses ``has_conditional_logic.md`` as a
**proxy template** for facts without their own prompt file. This is
honest for input-token estimation (the user block — per-element data —
is identical across facts) and provisional for system-block tokens
(which will refine as each prompt lands). Proxy-using rows are marked
``*`` in the output with a footnote.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.models.element import ElementRecord
from src.score.client import DEFAULT_MODEL, _PRICING_USD_PER_MTOK
from src.score.extract import (
    _PROMPT_PATH,
    _PROMPTS_DIR,
    group_by_entity,
    load_phase_a_records,
    render_prompt,
)

_LOGGER = logging.getLogger(__name__)


def _llm_roster(lens: str) -> tuple[str, ...]:
    """Derive the lens's LLM-fact roster from the live registries.

    ``phase_b_facts_for_lens`` is the runner's per-lens ``--facts all``
    roster; intersecting with ``extract.SUPPORTED_FACTS`` drops the
    deterministic facts (which never hit the LLM and therefore have no
    cost to estimate). Issue #213 item 3 retired the hand-kept 8-fact
    Phase B tuple, which had drifted 4+ facts behind the live spine
    roster (and never knew the source lens existed).
    """
    from src.score.extract import SUPPORTED_FACTS
    from src.score.runner import phase_b_facts_for_lens

    return tuple(
        f for f in phase_b_facts_for_lens(lens) if f in SUPPORTED_FACTS
    )


# Derived at import from the live registries. `FACTS_LLM` keeps its
# historical name (spine lens) for existing importers/tests;
# `facts_llm_for_lens` is the lens-aware accessor (reads the module
# globals at call time so tests can monkeypatch the rosters).
FACTS_LLM: tuple[str, ...] = _llm_roster("spine")
FACTS_LLM_SOURCE: tuple[str, ...] = _llm_roster("source")


def facts_llm_for_lens(lens: str) -> tuple[str, ...]:
    """Return the LLM-fact roster for ``lens`` (module-global backed)."""
    return FACTS_LLM_SOURCE if lens == "source" else FACTS_LLM


# Canonical roster lives in src.states — re-exported here for the
# existing importers (back-compat).
from src.states import SUPPORTED_STATES  # noqa: E402,F401
SUPPORTED_LENSES: tuple[str, ...] = ("spine", "source")

# Phase A anchor: 6961 output tokens / 37410 input tokens = 0.186.
OUTPUT_RATIO_ANCHOR: float = 0.186

# Band widens the point estimate for honest uncertainty. Post-Phase B
# when ≥3 facts have measured ratios, shrink to 0.15.
OUTPUT_RATIO_BAND_DEFAULT: float = 0.40


# ---------------------------------------------------------------------------
# Prompt-path resolution
# ---------------------------------------------------------------------------


def _prompt_path_for(fact: str) -> tuple[Path, bool]:
    """Return ``(path, is_proxy)`` for ``fact``.

    Falls back to ``has_conditional_logic.md`` when the fact-specific
    prompt file hasn't been authored yet. ``is_proxy=True`` signals the
    estimator to mark the row with ``*`` and append a footnote.
    """
    direct = _PROMPTS_DIR / f"{fact}.md"
    if direct.exists():
        return direct, False
    return _PROMPT_PATH, True


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


TokenCounter = Callable[[str, str, str], int]


def _chars_over_four(system_text: str, user_text: str, model: str) -> int:
    """Fallback estimator when ``count_tokens`` is unavailable.

    ``chars // 4`` is the classic conservative LLM token heuristic.
    Deliberately coarse — the ±40% band carries the uncertainty.
    """
    _ = model  # Signature parity with count_tokens.
    return (len(system_text) + len(user_text)) // 4


def _make_count_tokens_counter(client: Any, model: str) -> TokenCounter:
    """Wrap an ``anthropic.Anthropic`` client as a ``TokenCounter``.

    On API error (rate limit, network blip, auth rejection) the counter
    falls back to ``chars // 4`` for the offending batch — estimator
    runs must never crash mid-way.
    """
    def _count(system_text: str, user_text: str, model_id: str) -> int:
        try:
            result = client.messages.count_tokens(
                model=model_id,
                system=system_text,
                messages=[{"role": "user", "content": user_text}],
            )
            return int(result.input_tokens)
        except Exception as exc:  # noqa: BLE001 — deliberate: degrade, don't crash
            _LOGGER.warning(
                "count_tokens failed (%s) — falling back to chars/4 for this batch",
                exc,
            )
            return _chars_over_four(system_text, user_text, model_id)
    _ = model
    return _count


def resolve_token_counter(
    *,
    model: str = DEFAULT_MODEL,
    counter: TokenCounter | None = None,
) -> tuple[TokenCounter, str]:
    """Return ``(counter, source)`` — either API-based or ``chars/4``.

    ``source`` is a short human-readable tag (``"count_tokens"`` or
    ``"chars/4"``) used in the markdown footer for auditability.
    Tests pass ``counter=`` directly to skip all API plumbing.
    """
    if counter is not None:
        return counter, "injected"
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _chars_over_four, "chars/4"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        return _make_count_tokens_counter(client, model), "count_tokens"
    except Exception as exc:  # noqa: BLE001 — degrade to heuristic
        _LOGGER.warning(
            "anthropic SDK unavailable for count_tokens (%s) — using chars/4",
            exc,
        )
        return _chars_over_four, "chars/4"


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------


def _usd_for_cold_run(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cold-run USD: no cache hits assumed.

    Live runs amortize cache-read tokens at ~10× discount after the
    first batch per TTL, but the estimator's first-run posture is cold.
    Cache-read discount widens the actual/estimate gap *downward* —
    being conservative (over-estimate) beats sticker-shock surprise.
    """
    inp, out, _cw, _cr = _PRICING_USD_PER_MTOK.get(model, (3.0, 15.0, 3.75, 0.30))
    return (input_tokens * inp + output_tokens * out) / 1_000_000


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class BatchEstimate:
    state: str
    fact: str
    entity: str
    element_count: int
    input_tokens: int
    output_tokens_point: int


@dataclass
class FactStateEstimate:
    state: str
    fact: str
    records: int
    batches: int
    input_tokens: int
    output_tokens_point: int
    usd_point: float
    usd_lo: float
    usd_hi: float
    proxy_prompt: bool


@dataclass
class EstimateRun:
    model: str
    lens: str
    limit: int | None
    output_ratio: float
    band: float
    token_source: str
    rows: list[FactStateEstimate] = field(default_factory=list)

    @property
    def total_usd_point(self) -> float:
        return sum(r.usd_point for r in self.rows)

    @property
    def total_usd_lo(self) -> float:
        return sum(r.usd_lo for r in self.rows)

    @property
    def total_usd_hi(self) -> float:
        return sum(r.usd_hi for r in self.rows)

    @property
    def any_proxy(self) -> bool:
        return any(r.proxy_prompt for r in self.rows)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def estimate_fact_state(
    *,
    state: str,
    fact: str,
    lens: str = "spine",
    limit: int | None = None,
    model: str = DEFAULT_MODEL,
    output_ratio: float = OUTPUT_RATIO_ANCHOR,
    band: float = OUTPUT_RATIO_BAND_DEFAULT,
    counter: TokenCounter | None = None,
    records: list[ElementRecord] | None = None,
) -> FactStateEstimate:
    """Estimate cost for one (state, fact) pair.

    ``records`` override is for tests — production callers omit it and
    the pool is loaded via ``load_phase_a_records``.
    """
    if lens not in SUPPORTED_LENSES:
        raise ValueError(f"--lens {lens!r} not supported (only: {SUPPORTED_LENSES})")
    if state.upper() not in SUPPORTED_STATES:
        raise ValueError(f"--state {state!r} not supported (only: {SUPPORTED_STATES})")
    lens_facts = facts_llm_for_lens(lens)
    if fact not in lens_facts:
        raise ValueError(
            f"--fact {fact!r} not in the {lens}-lens LLM fact set: {lens_facts}"
        )

    if records is None:
        from src.score.schema import FACT_SOURCE_FILTERS
        records = load_phase_a_records(
            state.upper(),
            lens,
            limit=limit,
            source_filter=FACT_SOURCE_FILTERS.get(fact),
        )
    groups = group_by_entity(records)
    prompt_path, is_proxy = _prompt_path_for(fact)

    token_counter = counter if counter is not None else _chars_over_four

    total_in = 0
    batch_count = 0
    for entity, group_records in groups:
        system_text, user_text = render_prompt(
            entity, group_records, state.upper(), prompt_path=prompt_path,
            lens=lens,
        )
        in_tokens = token_counter(system_text, user_text, model)
        total_in += in_tokens
        batch_count += 1

    out_point = math.ceil(output_ratio * total_in)
    out_lo = math.ceil(max(0.0, output_ratio * (1.0 - band)) * total_in)
    out_hi = math.ceil(output_ratio * (1.0 + band) * total_in)

    return FactStateEstimate(
        state=state.upper(),
        fact=fact,
        records=len(records),
        batches=batch_count,
        input_tokens=total_in,
        output_tokens_point=out_point,
        usd_point=_usd_for_cold_run(model, total_in, out_point),
        usd_lo=_usd_for_cold_run(model, total_in, out_lo),
        usd_hi=_usd_for_cold_run(model, total_in, out_hi),
        proxy_prompt=is_proxy,
    )


def run(
    *,
    states: list[str],
    facts: list[str],
    lens: str = "spine",
    limit: int | None = None,
    model: str = DEFAULT_MODEL,
    band: float = OUTPUT_RATIO_BAND_DEFAULT,
    output_ratio: float = OUTPUT_RATIO_ANCHOR,
    counter: TokenCounter | None = None,
) -> EstimateRun:
    """Iterate (state, fact) pairs; return a structured estimate.

    Deterministic order: states loop outer (AZ, WI, MN, TX, IN), facts
    loop inner in the lens roster's order (``facts_llm_for_lens``).
    Matches the fanout ordering described in the plan's Phase B §6.2.
    """
    token_counter, source = resolve_token_counter(model=model, counter=counter)

    # Normalize + preserve deterministic ordering.
    ordered_states = [s for s in SUPPORTED_STATES if s in {x.upper() for x in states}]
    requested_facts = {f for f in facts}
    ordered_facts = [f for f in facts_llm_for_lens(lens) if f in requested_facts]

    run_out = EstimateRun(
        model=model,
        lens=lens,
        limit=limit,
        output_ratio=output_ratio,
        band=band,
        token_source=source,
    )
    for state in ordered_states:
        for fact in ordered_facts:
            estimate = estimate_fact_state(
                state=state,
                fact=fact,
                lens=lens,
                limit=limit,
                model=model,
                output_ratio=output_ratio,
                band=band,
                counter=token_counter,
            )
            run_out.rows.append(estimate)
    return run_out


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------


def _fmt_usd(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def format_markdown(result: EstimateRun, *, show_band: bool = True) -> str:
    """Return the markdown table + footer for an EstimateRun."""
    ratio_pct = result.output_ratio * 100
    band_pct = int(round(result.band * 100))
    header = (
        f"Fact × state estimate "
        f"(model={result.model}, ratio={ratio_pct:.1f}% "
        f"±{band_pct}%, tokens={result.token_source})"
    )
    lines: list[str] = [header, ""]
    lines.append(
        "| Fact | State | Records | Batches | Est in | Est out | Est USD (range) |"
    )
    lines.append("|---|:---:|---:|---:|---:|---:|---:|")

    for row in result.rows:
        label = row.fact + ("*" if row.proxy_prompt else "")
        if show_band:
            usd_cell = (
                f"{_fmt_usd(row.usd_point)} "
                f"({_fmt_usd(row.usd_lo)}–{_fmt_usd(row.usd_hi)})"
            )
        else:
            usd_cell = _fmt_usd(row.usd_point)
        lines.append(
            f"| {label} | {row.state} | {row.records} | {row.batches} | "
            f"{row.input_tokens:,} | {row.output_tokens_point:,} | {usd_cell} |"
        )

    total_cell = (
        f"{_fmt_usd(result.total_usd_point)} "
        f"({_fmt_usd(result.total_usd_lo)}–{_fmt_usd(result.total_usd_hi)})"
        if show_band
        else _fmt_usd(result.total_usd_point)
    )
    lines.append(f"| **total** | | | | | | **{total_cell}** |")

    if result.any_proxy:
        lines.append("")
        lines.append(
            "`*` = proxy prompt (fact-specific prompt not authored yet; "
            "input-token counts are accurate for the per-element user block "
            "but approximate for the system prologue)."
        )
    return "\n".join(lines) + "\n"
