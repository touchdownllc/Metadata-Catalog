"""Phase B `mc score estimate` — budget-gating estimator tests.

Covers the success criteria from `docs/next-session-scoring-phase-b.md § 3`:

- dry path: no LLM calls, no count_tokens calls (injected counter)
- proxy-prompt fallback for facts without their own authored prompt
- chars/4 fallback when ANTHROPIC_API_KEY is missing
- deterministic ordering: states outer (AZ/WI/MN/TX), facts inner (FACTS_LLM)
- limit semantics match `score extract`
- pricing-table wiring (no duplicate constants; client.py is the single source)
- markdown shape: header row, total row, proxy footnote when applicable
- CLI wiring — mutually-exclusive --fact/--facts, unknown-fact exit 2
- Phase A subset estimate matches actual $0.2166 within ±20% (uses an
  injected counter that mimics count_tokens' observed output)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from src.models.element import ElementRecord
from src.score import estimate as estimate_module
from src.score.client import _PRICING_USD_PER_MTOK
from src.score.estimate import (
    FACTS_LLM,
    OUTPUT_RATIO_ANCHOR,
    OUTPUT_RATIO_BAND_DEFAULT,
    SUPPORTED_STATES,  # noqa: F401 — import IS the test: estimate must re-export the roster
    EstimateRun,
    FactStateEstimate,
    _chars_over_four,
    _prompt_path_for,
    _usd_for_cold_run,
    estimate_fact_state,
    format_markdown,
    resolve_token_counter,
    run as run_estimate,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mk_record(
    *,
    entity: str = "Student",
    element_name: str = "studentId",
    definition_text: str = "The unique identifier for a student.",
    business_rules_text: str | None = None,
    element_specific_rules: str | None = None,
) -> ElementRecord:
    return ElementRecord.model_validate({
        "state": "AZ",
        "edfi_version": "3",
        "domain": "Student",
        "entity": entity,
        "element_name": element_name,
        "data_type": "String",
        "definition_text": definition_text,
        "source": "core",
        "extension_name": None,
        "business_rules_text": business_rules_text,
        "element_specific_rules": element_specific_rules,
        "regulatory_citations": [],
        "related_entities": [],
        "descriptor_table_code": None,
        "descriptor_table_values": [],
        "collections_text": None,
        "edfi_standard_definition": None,
        "source_document": None,
        "source_page_or_section": None,
        "documented": True,
    })


def _fake_records(n: int = 5) -> list[ElementRecord]:
    return [
        _mk_record(entity="Student", element_name=f"elem{i:02d}")
        for i in range(n)
    ]


def _constant_counter(n: int = 1000):
    """Return a counter that claims ``n`` input tokens per batch."""
    def _c(_system: str, _user: str, _model: str) -> int:
        return n
    return _c


# ---------------------------------------------------------------------------
# Imports + constants
# ---------------------------------------------------------------------------


def test_estimate_package_imports_cleanly() -> None:
    import importlib
    importlib.import_module("src.score.estimate")


def test_facts_llm_derives_from_live_registries() -> None:
    """Issue #213 item 3: FACTS_LLM is no longer a hand-kept Phase B
    tuple — it derives from the live per-lens roster ∩ the authored LLM
    facts, so a fact added to the pipeline is automatically estimable."""
    from src.score.estimate import FACTS_LLM_SOURCE, facts_llm_for_lens
    from src.score.extract import SUPPORTED_FACTS
    from src.score.runner import phase_b_facts_for_lens

    assert FACTS_LLM[0] == "has_conditional_logic"
    assert FACTS_LLM == tuple(
        f for f in phase_b_facts_for_lens("spine") if f in SUPPORTED_FACTS
    )
    assert FACTS_LLM_SOURCE == tuple(
        f for f in phase_b_facts_for_lens("source") if f in SUPPORTED_FACTS
    )
    assert facts_llm_for_lens("spine") == FACTS_LLM
    assert facts_llm_for_lens("source") == FACTS_LLM_SOURCE
    # Deterministic facts never enter the roster (no LLM cost to estimate).
    assert "definition_present" not in FACTS_LLM
    # The former hand-kept list's members are still present.
    assert "definition_is_implementable" in FACTS_LLM
    assert "cross_entity_targets" in FACTS_LLM
    assert "has_aggregation" in FACTS_LLM


def test_pricing_uses_client_table_not_local_copy() -> None:
    """USD must derive from `client._PRICING_USD_PER_MTOK` — no local dup.

    If this ever drifts, `score extract`'s live USD and `score estimate`'s
    projection fall out of sync and Phase B's estimate-vs-actual success
    criterion becomes meaningless.
    """
    inp, out, _cw, _cr = _PRICING_USD_PER_MTOK["claude-sonnet-4-6"]
    expected = (10_000 * inp + 1_000 * out) / 1_000_000
    assert _usd_for_cold_run("claude-sonnet-4-6", 10_000, 1_000) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Prompt path resolution
# ---------------------------------------------------------------------------


def test_prompt_path_resolves_authored_fact() -> None:
    path, is_proxy = _prompt_path_for("has_conditional_logic")
    assert path.exists()
    assert is_proxy is False


def test_prompt_path_falls_back_to_proxy_for_unauthored_fact() -> None:
    # Synthetic sentinel — the estimator resolves by filename existence,
    # not by FACTS_LLM membership, so any name that never gets an
    # authored .md file exercises the proxy path regardless of how many
    # real facts have landed.
    path, is_proxy = _prompt_path_for("fake_unauthored_fact_for_testing")
    assert path.exists()
    assert path.name == "has_conditional_logic.md"
    assert is_proxy is True


# ---------------------------------------------------------------------------
# Chars/4 heuristic + counter resolution
# ---------------------------------------------------------------------------


def test_chars_over_four_is_coarse_but_predictable() -> None:
    # 400 chars in system + 400 chars in user = 800/4 = 200 tokens.
    assert _chars_over_four("x" * 400, "y" * 400, "claude-sonnet-4-6") == 200


def test_resolve_token_counter_no_api_key_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    counter, source = resolve_token_counter()
    assert source == "chars/4"
    assert counter("hi", "there", "claude-sonnet-4-6") == (len("hi") + len("there")) // 4


def test_resolve_token_counter_injected_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch the network — injecting a counter is the escape hatch."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-would-be-used-otherwise")
    c = _constant_counter(42)
    counter, source = resolve_token_counter(counter=c)
    assert source == "injected"
    assert counter is c


def test_api_error_degrades_to_chars_over_four(caplog: pytest.LogCaptureFixture) -> None:
    """A count_tokens failure on one batch must not crash the run."""

    class ExplodingClient:
        class messages:
            @staticmethod
            def count_tokens(**_: Any) -> None:
                raise RuntimeError("boom")

    counter = estimate_module._make_count_tokens_counter(
        ExplodingClient(), "claude-sonnet-4-6"
    )
    tokens = counter("sys", "user-text", "claude-sonnet-4-6")
    # Falls back to chars/4 for the failing batch.
    assert tokens == (len("sys") + len("user-text")) // 4


# ---------------------------------------------------------------------------
# estimate_fact_state — the per-(state, fact) core
# ---------------------------------------------------------------------------


def test_estimate_fact_state_groups_and_counts() -> None:
    records = [
        _mk_record(entity="Student", element_name="a"),
        _mk_record(entity="Student", element_name="b"),
        _mk_record(entity="Staff", element_name="c"),
    ]
    # Two entity groups → two batches → 2 × 1000 = 2000 input tokens.
    est = estimate_fact_state(
        state="AZ",
        fact="has_conditional_logic",
        records=records,
        counter=_constant_counter(1000),
    )
    assert est.records == 3
    assert est.batches == 2
    assert est.input_tokens == 2000
    assert est.output_tokens_point == 372  # ceil(0.186 * 2000)


def test_estimate_fact_state_marks_proxy_for_unauthored_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inject a synthetic fact name into FACTS_LLM so the estimator
    # accepts it but no prompt file exists — forces the proxy path.
    sentinel = "fake_unauthored_fact_for_testing"
    monkeypatch.setattr(
        estimate_module, "FACTS_LLM", (*FACTS_LLM, sentinel)
    )
    records = _fake_records(1)
    est = estimate_fact_state(
        state="AZ",
        fact=sentinel,
        records=records,
        counter=_constant_counter(500),
    )
    assert est.proxy_prompt is True


def test_estimate_fact_state_usd_band_symmetric_around_point() -> None:
    records = _fake_records(1)
    est = estimate_fact_state(
        state="AZ",
        fact="has_conditional_logic",
        records=records,
        counter=_constant_counter(10_000),
    )
    # Point output = 10000 * 0.186 = 1860, lo = 10000 * 0.186 * 0.6 = 1116,
    # hi = 10000 * 0.186 * 1.4 = 2604. USD band must bracket the point.
    assert est.usd_lo < est.usd_point < est.usd_hi


def test_estimate_fact_state_validates_inputs() -> None:
    records = _fake_records(1)
    # Both real lenses are supported since issue #213 item 3; an unknown
    # lens still raises.
    with pytest.raises(ValueError, match="--lens"):
        estimate_fact_state(
            state="AZ", fact="has_conditional_logic", lens="gap",
            records=records, counter=_constant_counter(),
        )
    with pytest.raises(ValueError, match="--state"):
        estimate_fact_state(
            state="CA", fact="has_conditional_logic",
            records=records, counter=_constant_counter(),
        )
    with pytest.raises(ValueError, match="LLM fact set"):
        estimate_fact_state(
            state="AZ", fact="not_a_real_fact",
            records=records, counter=_constant_counter(),
        )


def test_estimate_fact_state_accepts_source_lens() -> None:
    """The flagship source lens is estimable (was Choice(["spine"]) /
    hard-raise before issue #213 item 3)."""
    records = _fake_records(2)
    est = estimate_fact_state(
        state="AZ", fact="extension_is_necessary", lens="source",
        records=records, counter=_constant_counter(500),
    )
    assert est.records == 2
    assert est.batches >= 1
    assert est.usd_point > 0


# ---------------------------------------------------------------------------
# run() — full multi-state, multi-fact iteration
# ---------------------------------------------------------------------------


def test_run_deterministic_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows must come back states-outer (AZ,WI,MN,TX) × facts-inner (FACTS_LLM order)."""
    call_log: list[tuple[str, str]] = []

    def _fake_load(state: str, lens: str, *, elements_path: Path | None = None, limit: int | None = None, source_filter: tuple[str, ...] | None = None):
        return _fake_records(2)

    monkeypatch.setattr(estimate_module, "load_phase_a_records", _fake_load)

    def _track_count(system: str, user: str, model: str) -> int:
        return 100

    result = run_estimate(
        states=["TX", "AZ", "MN", "WI"],  # Deliberately scrambled input order.
        facts=["has_aggregation", "has_conditional_logic"],  # Reverse of declared order.
        counter=_track_count,
    )
    seen = [(r.state, r.fact) for r in result.rows]
    assert seen == [
        ("AZ", "has_conditional_logic"),
        ("AZ", "has_aggregation"),
        ("WI", "has_conditional_logic"),
        ("WI", "has_aggregation"),
        ("MN", "has_conditional_logic"),
        ("MN", "has_aggregation"),
        ("TX", "has_conditional_logic"),
        ("TX", "has_aggregation"),
    ]
    _ = call_log  # silence unused warning in linters


def test_run_honors_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--limit N` must flow through to `load_phase_a_records`."""
    limits_seen: list[int | None] = []

    def _fake_load(state: str, lens: str, *, elements_path: Path | None = None, limit: int | None = None, source_filter: tuple[str, ...] | None = None):
        limits_seen.append(limit)
        return _fake_records(limit or 0)

    monkeypatch.setattr(estimate_module, "load_phase_a_records", _fake_load)

    run_estimate(
        states=["AZ"],
        facts=["has_conditional_logic"],
        limit=37,
        counter=_constant_counter(100),
    )
    assert limits_seen == [37]


def test_run_total_matches_row_sum() -> None:
    run_out = EstimateRun(
        model="claude-sonnet-4-6",
        lens="spine",
        limit=None,
        output_ratio=OUTPUT_RATIO_ANCHOR,
        band=OUTPUT_RATIO_BAND_DEFAULT,
        token_source="injected",
    )
    run_out.rows = [
        FactStateEstimate(
            state="AZ", fact="has_conditional_logic",
            records=10, batches=2,
            input_tokens=1000, output_tokens_point=186,
            usd_point=0.01, usd_lo=0.008, usd_hi=0.012,
            proxy_prompt=False,
        ),
        FactStateEstimate(
            state="WI", fact="has_conditional_logic",
            records=10, batches=2,
            input_tokens=1000, output_tokens_point=186,
            usd_point=0.02, usd_lo=0.016, usd_hi=0.024,
            proxy_prompt=False,
        ),
    ]
    assert run_out.total_usd_point == pytest.approx(0.03)
    assert run_out.total_usd_lo == pytest.approx(0.024)
    assert run_out.total_usd_hi == pytest.approx(0.036)
    assert run_out.any_proxy is False


# ---------------------------------------------------------------------------
# format_markdown
# ---------------------------------------------------------------------------


def test_format_markdown_has_header_rows_and_total(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_load(state: str, lens: str, *, elements_path: Path | None = None, limit: int | None = None, source_filter: tuple[str, ...] | None = None):
        return _fake_records(1)

    monkeypatch.setattr(estimate_module, "load_phase_a_records", _fake_load)

    result = run_estimate(
        states=["AZ", "WI"],
        facts=["has_conditional_logic"],
        counter=_constant_counter(1000),
    )
    md = format_markdown(result)
    assert md.startswith("Fact × state estimate")
    assert "| Fact | State | Records | Batches | Est in | Est out | Est USD (range) |" in md
    assert "| **total** |" in md
    assert "has_conditional_logic" in md
    # Both states present in table.
    assert "| AZ |" in md
    assert "| WI |" in md


def test_format_markdown_proxy_footnote_only_when_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_load(state: str, lens: str, *, elements_path: Path | None = None, limit: int | None = None, source_filter: tuple[str, ...] | None = None):
        return _fake_records(1)

    monkeypatch.setattr(estimate_module, "load_phase_a_records", _fake_load)

    authored_only = run_estimate(
        states=["AZ"],
        facts=["has_conditional_logic"],
        counter=_constant_counter(500),
    )
    assert "proxy prompt" not in format_markdown(authored_only)

    sentinel = "fake_unauthored_fact_for_testing"
    monkeypatch.setattr(
        estimate_module, "FACTS_LLM", (*FACTS_LLM, sentinel)
    )
    with_proxy = run_estimate(
        states=["AZ"],
        facts=[sentinel],
        counter=_constant_counter(500),
    )
    md = format_markdown(with_proxy)
    assert f"{sentinel}*" in md
    assert "proxy prompt" in md


def test_format_markdown_respects_no_band(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_load(state: str, lens: str, *, elements_path: Path | None = None, limit: int | None = None, source_filter: tuple[str, ...] | None = None):
        return _fake_records(1)

    monkeypatch.setattr(estimate_module, "load_phase_a_records", _fake_load)
    result = run_estimate(
        states=["AZ"],
        facts=["has_conditional_logic"],
        counter=_constant_counter(500),
    )
    md = format_markdown(result, show_band=False)
    # Band parentheticals absent when show_band=False.
    assert "–$" not in md


# ---------------------------------------------------------------------------
# Phase A subset accuracy (±20% of $0.2166)
# ---------------------------------------------------------------------------


@pytest.mark.realdata
def test_phase_a_subset_within_twenty_percent() -> None:
    """Injecting the measured Phase A token counts must land within ±20%
    of the actual cold-run cost — the only hard bound we have with one
    fact measured.

    Phase A actual: 37,410 input + 6,961 output = $0.2166 on Sonnet 4.6.
    Estimator input-count must match what `messages.count_tokens` would
    report (we inject that count here to avoid a network call).
    """
    measured_input_per_batch_for_az_200 = 37_410 // 38  # 38 batches in AZ 200-row run.

    def _phase_a_matched_counter(_s: str, _u: str, _m: str) -> int:
        return measured_input_per_batch_for_az_200

    # Use the real AZ spine records; requires the golden artifact.
    az_spine = Path(__file__).resolve().parent.parent / "data" / "out" / "az_elements_spine.json"
    if not az_spine.exists():
        pytest.skip("requires data/out/az_elements_spine.json")

    est = estimate_fact_state(
        state="AZ",
        fact="has_conditional_logic",
        limit=200,
        counter=_phase_a_matched_counter,
    )
    # The estimator's input_tokens should equal batches × per-batch count.
    assert est.batches == 38
    assert est.input_tokens == 38 * measured_input_per_batch_for_az_200
    # Ratio check against the Phase A actual ($0.2166):
    ratio = est.usd_point / 0.2166
    assert 0.80 <= ratio <= 1.20, (
        f"point estimate {est.usd_point:.4f} drifted >±20% from Phase A actual $0.2166 "
        f"(ratio {ratio:.3f})"
    )
    # And the range-band must always bracket the actual.
    assert est.usd_lo <= 0.2166 <= est.usd_hi


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_help_runs(self) -> None:
        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["score", "estimate", "--help"])
        assert result.exit_code == 0, result.output
        assert "budget-gating" in result.output.lower()

    def test_fact_and_facts_mutually_exclusive(self) -> None:
        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["score", "estimate", "--fact", "has_conditional_logic", "--facts", "all"],
        )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_unknown_fact_exits_two(self) -> None:
        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(
            cli, ["score", "estimate", "--fact", "totally_fake_fact"]
        )
        assert result.exit_code == 2
        assert "unknown fact" in result.output

    def test_dry_path_no_network(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CLI run with no API key + --state AZ --limit 1 exits 0 and
        never imports `anthropic.Anthropic`."""
        import anthropic

        def _fail(*_: Any, **__: Any) -> None:
            raise AssertionError("estimator must not construct a live Anthropic client when ANTHROPIC_API_KEY is unset")

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(anthropic, "Anthropic", _fail)

        # Redirect the records loader to a tiny synthetic pool.
        monkeypatch.setattr(
            estimate_module,
            "load_phase_a_records",
            lambda state, lens, *, elements_path=None, limit=None, source_filter=None: _fake_records(3),
        )

        from src.cli import cli
        runner = CliRunner()
        result = runner.invoke(
            cli, ["score", "estimate", "--state", "AZ", "--fact", "has_conditional_logic"]
        )
        assert result.exit_code == 0, result.output
        assert "has_conditional_logic" in result.output
        assert "chars/4" in result.output
