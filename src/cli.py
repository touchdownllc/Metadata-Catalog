"""POC-3 CLI entry point. Subcommands: spine, ingest, report, score, review."""

from __future__ import annotations

from pathlib import Path

import click

from src.ingest import INGEST_MODULES
from src.publish.stages import DEFAULT_COST_CAP, DEFAULT_TX_BASE_URL, STAGE_NAMES
from src.states import SUPPORTED_STATES

# One source of truth for the state roster (src.states) — these shared
# Choice objects replace the previously duplicated per-command literals.
_STATE_CHOICES = click.Choice(list(SUPPORTED_STATES), case_sensitive=False)
_STATE_CHOICES_WITH_ALL = click.Choice(
    [*SUPPORTED_STATES, "all"], case_sensitive=False
)


def _expand_states(state_opt: str) -> tuple[str, ...]:
    """Expand the shared ``--state all`` idiom to the canonical roster.

    One helper for the ~8 copies of ``SUPPORTED_STATES if state.lower()
    == "all" else (state.upper(),)`` (issue #213 item 3).
    """
    if state_opt.lower() == "all":
        return tuple(SUPPORTED_STATES)
    return (state_opt.upper(),)


# --- shared option factories (issue #213 item 3) -----------------------------
# Repeated option groups built once; per-command help/defaults stay
# parameters so every command's --help output is byte-identical.


def _state_all_option(*, default: str, help: str):
    return click.option(
        "--state",
        "state_opt",
        type=_STATE_CHOICES_WITH_ALL,
        default=default,
        show_default=True,
        help=help,
    )


def _lens_option(
    *,
    default: str | None = "source",
    help: str,
    required: bool = False,
    choices: tuple[str, ...] = ("source", "spine"),
):
    kwargs: dict = {
        "type": click.Choice(list(choices), case_sensitive=False),
        "help": help,
    }
    if required:
        kwargs["required"] = True
    else:
        kwargs["default"] = default
        kwargs["show_default"] = True
    return click.option("--lens", **kwargs)


def _allow_stale_option(
    help: str = (
        "Proceed on inputs the publish manifest marks stale/missing "
        "(degraded numbers — see PR #182)."
    ),
):
    return click.option("--allow-stale", is_flag=True, help=help)


def _model_option(
    help: str = "Anthropic model ID (defaults to the harness's current Sonnet).",
):
    return click.option("--model", type=str, default=None, help=help)


@click.group()
def cli() -> None:
    """NACHOS POC-3 — ingestion-first pipeline."""


@cli.group()
def spine() -> None:
    """Fetch and build the Ed-Fi Swagger/API model (internal name: spine)."""


@spine.command("fetch")
@click.option("--state", type=_STATE_CHOICES, required=True)
@click.option("--school-year", type=int, default=2026, show_default=True,
              help="Used for WI (fills {schoolYearFromRoute}) and IN (year-prefixed path "
                   "/{school_year}/metadata/...). IN currently hosts /2026/ + /2027/; "
                   "/2027/ is the active deployment as of 2026-05.")
@click.option("--base-url", type=str, default=None,
              help="Override the sandbox base URL (up through '.../data/v3'). "
                   "Intended for TX (local Docker); ignored when the default is correct.")
def spine_fetch(state: str, school_year: int, base_url: str | None) -> None:
    """Fetch resources + descriptors Swagger JSON from a state Ed-Fi sandbox."""
    from src.spine.fetch import fetch_state_swagger
    fetch_state_swagger(state.upper(), school_year=school_year, base_url=base_url)


@spine.command("build")
@click.option("--state", type=_STATE_CHOICES, required=True)
def spine_build(state: str) -> None:
    """Build per-state spine manifest from cached Swagger."""
    from src.spine.build import build_state_spine
    build_state_spine(state.upper())


@spine.command("domain-map")
@click.option("--source", type=click.Choice(["AZ", "WI", "MN"], case_sensitive=False),
              default="AZ", show_default=True,
              help="State whose cached swagger carries x-Ed-Fi-domains tags.")
def spine_domain_map(source: str) -> None:
    """(Re)generate data/spine/edfi_domain_map.json from a domain-rich state swagger."""
    from src.spine.build import regenerate_domain_map
    regenerate_domain_map(source.upper())


@cli.group()
def ingest() -> None:
    """Per-state ingestion adapters (AZ, WI, MN, TX, IN)."""


# The five per-state ingest commands are generated from the one
# state→module registry (`src.ingest.INGEST_MODULES` — issue #213
# item 3). Help text stays per-state; behavior (lazy import + call the
# module-level plain `run()`) is identical to the former hand-written
# commands.
_INGEST_HELP: dict[str, str] = {
    "AZ": "Ingest Arizona (XLSX + PDFs) and enrich spine.",
    "WI": "Ingest Wisconsin (Confluence) and enrich spine.",
    "MN": "Ingest Minnesota (GitHub / MetaEd) and enrich spine.",
    "TX": "Ingest Texas — TWEDS v33 source, spine-enriched via local TSDS SDK.",
    "IN": "Ingest Indiana — IDOE Vendor Documentation XLSX, spine-enriched.",
}


def _make_ingest_command(code: str) -> click.Command:
    @ingest.command(code.lower(), help=_INGEST_HELP[code])
    def _ingest_state() -> None:
        import importlib

        module = importlib.import_module(f"src.ingest.{INGEST_MODULES[code]}")
        module.run()

    return _ingest_state


for _code in SUPPORTED_STATES:
    _make_ingest_command(_code)


@ingest.command("swagger-backfill")
@click.option(
    "--state",
    type=_STATE_CHOICES_WITH_ALL,
    required=True,
    help="State code, or 'all' to backfill AZ + WI + MN + TX + IN in one pass.",
)
def ingest_swagger_backfill(state: str) -> None:
    """Append swagger-as-source rows for entities the state's source doc is silent on.

    Reads ``{state}_elements_source.json`` + ``{state}_spine.json`` and rewrites
    both lens artifacts: appends synthetic source-lens rows carrying
    ``documentation_source="swagger"`` for spine-only entities, and flips the
    matching spine-lens rows from ``documented=False`` to ``documented=True``.
    Issue #70 — swagger publication counts as state documentation when the
    primary source is silent on a whole entity.
    """
    from src.ingest.swagger_backfill import run as run_backfill

    for code in _expand_states(state):
        run_backfill(code)


@ingest.command("gap")
@click.option(
    "--state",
    type=_STATE_CHOICES_WITH_ALL,
    required=True,
    help="State code, or 'all' to surface gaps for AZ + WI + MN + TX + IN in one pass.",
)
def ingest_gap(state: str) -> None:
    """Surface spine-anchored coverage gaps to data/out/{state}_elements_gap.json.

    Reads {state}_elements_source.json + {state}_spine.json and writes a
    sibling gap artifact enumerating spine (entity, element) pairs the
    state's source doc is silent on. Source-lens output is byte-unchanged
    (the gap is a separate artifact). Issue #66 Layer 2.
    """
    from src.ingest.gap_surfacer import run as run_gap

    for code in _expand_states(state):
        run_gap(code)


@cli.group()
def report() -> None:
    """Cross-state coverage and analyst exports."""


@report.command("coverage")
@_lens_option(
    help=(
        "Which lens to report on. 'source' reads {state}_elements_source.json "
        "(source-driven shape). 'spine' selects the API-model lens "
        "(flag value kept for operator stability) — reads "
        "{state}_elements_spine.json, the Ed-Fi Swagger/API-model-"
        "enumerated, source-enriched shape."
    ),
)
@_allow_stale_option()
def report_coverage(lens: str, allow_stale: bool) -> None:
    """Write data/out/coverage_report{_lens}.{json,md}."""
    from src.report.coverage import run as run_coverage
    run_coverage(lens=lens.lower(), allow_stale=allow_stale)


@report.command("analyst")
@click.option("--state", type=_STATE_CHOICES)
@click.option(
    "--all", "all_states", is_flag=True,
    help="Produce per-state workbooks + combined coverage workbook "
         "(this is already the default when --state is omitted; flag "
         "kept for invocation compatibility).",
)
@_lens_option(
    help=(
        "Analyst workbook lens. 'source' (default) produces "
        "{state}_analyst.xlsx. 'spine' selects the API-model lens (flag "
        "value kept for operator stability) — produces "
        "{state}_analyst_spine.xlsx with an AI: Documented column, "
        "per-state 'Documented only' sheets, and the API Model Gaps "
        "sheet."
    ),
)
@click.option(
    "--with-human",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Human-scored workbook to compare against: appends the Cmp: "
        "Status / Base Δ / Adj Δ / Why columns to the Details sheet "
        "(the June follow-up comparison format) and writes SEPARATE "
        "*_with_human.xlsx copies — the pipeline deliverables on disk "
        "are never overwritten. Columns are located by header name; "
        "pass --human-config for a known per-state file."
    ),
)
@click.option(
    "--human-config",
    type=click.Choice(
        ["arizona", "wisconsin", "minnesota", "texas", "indiana"],
        case_sensitive=False,
    ),
    default=None,
    help="Use a known per-state workbook column mapping "
         "(docs/human-scored-files/ basis) instead of header-name "
         "autodetection.",
)
@_allow_stale_option()
def report_analyst(
    state: str | None,
    all_states: bool,
    lens: str,
    with_human: Path | None,
    human_config: str | None,
    allow_stale: bool,
) -> None:
    """Produce analyst XLSX matching NACHOS Template shape."""
    # `all_states` is accepted for invocation compat but not forwarded:
    # `run()` always produces per-state + combined when `state` is None
    # (the old parameter was never read — issue #213 item 1 dead surface).
    del all_states
    from src.report.analyst import run as run_analyst
    run_analyst(
        state=state.upper() if state else None,
        lens=lens.lower(),
        with_human=with_human,
        human_config=human_config.lower() if human_config else None,
        allow_stale=allow_stale,
    )


@report.command("audit")
@click.option("--state", type=_STATE_CHOICES, required=True)
@_lens_option(
    help="Which lens's rows/facts to audit ('spine' = API-model lens).",
)
def report_audit(state: str, lens: str) -> None:
    """Write the on-demand audit workbook {state}_audit{_spine}.xlsx.

    The full Audit Trail surface (every row, every fact/span/dim/rule
    column) — removed from the analyst deliverables under Option D and
    kept one command away here. Row # remains the shared address into
    the analyst workbook's Details sheet.
    """
    from src.report.audit import run as run_audit

    path = run_audit(state=state.upper(), lens=lens.lower())
    click.echo(f"wrote {path}")


@report.command("scoring")
@_lens_option(
    default="spine",
    help="Lens whose sidecars feed the rollup (reads data/out/{state}_scores_{lens}.json).",
)
@_state_all_option(
    default="all",
    help="State(s) to include. Use 'all' for AZ+WI+MN+TX+IN.",
)
@_allow_stale_option()
def report_scoring(lens: str, state_opt: str, allow_stale: bool) -> None:
    """Write data/out/scoring_report_{lens}.{json,md} — Phase D cross-state rollup."""
    from src.report.scoring import run as run_scoring

    states = _expand_states(state_opt)
    report = run_scoring(
        lens=lens.lower(), states=states, allow_stale=allow_stale
    )
    cs = report["cross_state"]
    csm = cs["cross_state_mean_quality"]
    csm_str = "n/a" if csm is None else f"{csm:.2f}"
    click.echo(
        f"scoring rollup ({lens}): {cs['state_count']} states · "
        f"{cs['record_total']:,} records · "
        f"{cs['review_total']:,} review flags · "
        f"mean quality {csm_str}"
    )


@report.command("review-queue")
@_lens_option(
    default="spine",
    help="Lens whose sidecars feed the review queue.",
)
@click.option(
    "--top-n",
    type=int,
    default=30,
    show_default=True,
    help="Max flagged rows rendered in the MD listing (JSON carries every row).",
)
@_allow_stale_option()
def report_review_queue(lens: str, top_n: int, allow_stale: bool) -> None:
    """Write data/out/review_queue_{lens}.{json,md} — Phase D review-queue routing."""
    from src.report.review_queue import run as run_review_queue

    queue = run_review_queue(
        lens=lens.lower(), top_n=top_n, allow_stale=allow_stale
    )
    totals = queue["route_totals"]
    click.echo(
        f"review queue ({lens}): "
        + ", ".join(f"{k}={v:,}" for k, v in totals.items())
        + f" — {len(queue['entries']):,} flagged total"
    )


@report.command("review-digest")
@_lens_option(
    required=True,
    help="Lens whose sidecars the reviewer file is compared against.",
)
@click.option(
    "--top-n",
    type=int,
    default=10,
    show_default=True,
    help="Max divergence patterns rendered in the MD digest.",
)
@_allow_stale_option()
def report_review_digest(lens: str, top_n: int, allow_stale: bool) -> None:
    """Phase E: write data/out/review_digest_{lens}.{json,md}.

    Compares the five per-state human-scored workbooks
    (docs/human-scored-files/ — hand-placed, gitignored) against POC-3
    per-record sidecars. Human-scored framing — never labels either
    side correct.
    """
    from src.report.review_digest import run as run_review_digest

    digest = run_review_digest(
        lens=lens.lower(), top_n=top_n, allow_stale=allow_stale
    )
    overall = digest["overall"]
    click.echo(
        f"review-digest ({lens}): "
        f"{overall['matched']:,}/{overall['reviewer_rows']:,} matched "
        f"({overall['match_pct']:.1f}%) · "
        f"top_patterns={len(digest['top_patterns'])}"
    )


@report.command("reviewer-comparison")
def report_reviewer_comparison() -> None:
    """Refresh ``docs/reviewer-comparison.md`` from current review-digest output.

    Reads ``data/out/review_digest_{source,spine}.json`` plus one source-
    lens sidecar per state for the ``scoring_plan_version`` stamp and
    writes the committed ``docs/reviewer-comparison.md`` summary doc.
    Run after ``mc report review-digest --lens source`` and ``--lens
    spine`` complete; commit the regenerated doc on the same PR that
    bumps ``SCORING_PLAN_VERSION`` (CLAUDE.md operator playbook).
    """
    from src.report.reviewer_comparison_summary import run as run_summary

    summary = run_summary()
    plan_v = summary.get("scoring_plan_version") or "unknown"
    lenses = ", ".join(summary.get("lenses") or [])
    click.echo(
        f"reviewer-comparison: docs/reviewer-comparison.md refreshed "
        f"(scoring_plan_version={plan_v}, lenses={lenses})"
    )


@report.command("divergence")
@_allow_stale_option()
def report_divergence(allow_stale: bool) -> None:
    """Write data/out/lens_divergence.{json,md} — cross-lens comparison per state.

    Requires both {state}_elements_source.json and {state}_elements_spine.json
    for each state (regenerate with `mc ingest <state>` if missing).
    """
    from src.report.divergence import run as run_divergence
    run_divergence(allow_stale=allow_stale)


def _run_human_score_backfill(config_name: str, output_path: Path | None) -> None:
    from src.report.human_score_backfill import run as run_backfill

    out = run_backfill(config_name, output_path=output_path)
    click.echo(f"{config_name}: workbook written to {out}")


@report.command("human-overlay")
@click.option(
    "--config",
    "config_name",
    required=True,
    # Hardcoded to honor cli.py's lazy-import convention (importing
    # `human_score_backfill.CONFIGS` at decoration time would defeat it).
    # `tests/test_report_human_score_backfill.py` pins this Choice list
    # against `set(CONFIGS)` so the two cannot drift.
    type=click.Choice(
        ["arizona", "wisconsin", "minnesota", "texas", "indiana"],
        case_sensitive=False,
    ),
    help=(
        "Which per-state human-scored workbook to overlay (see "
        "human_score_backfill.CONFIGS — derived from "
        "review_loader.REVIEWER_SOURCES, the docs/human-scored-files/ "
        "basis). Each writes data/out/{name}_with_mc_scores.xlsx."
    ),
)
@click.option(
    "--output-path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Override output xlsx (defaults to the config's data/out path).",
)
def report_human_overlay(config_name: str, output_path: Path | None) -> None:
    """Issue #136: overlay a human-scored workbook with POC-3 ai- columns.

    One engine (`human_score_backfill.build_workbook`), one command,
    five per-state configs (2026-07-07 basis — the former
    training-20pct/training-file/nachos-arizona configs retired with
    the single training file). Copies the origin columns verbatim and
    appends the ai-prefixed source-lens Reviewer View cells for every
    row whose (entity, element) resolves to a POC-3 sidecar
    (gap-artifact fallthrough included). Read-only against existing
    sidecars.
    """
    _run_human_score_backfill(config_name.lower(), output_path)


@report.command("recommendations")
@click.option(
    "--state",
    type=_STATE_CHOICES,
    default="AZ",
    show_default=True,
    help="State whose scored sidecar feeds the recommendations.",
)
@_lens_option(
    choices=("source", "spine", "gap"),
    help=(
        "Lens whose sidecar drives the recommendation templates. "
        "``source`` and ``spine`` walk the per-dimension rule cascade; "
        "``gap`` (issue #73) emits one record-level rec per spine-anchored "
        "gap row plus an optional structural-depth callout."
    ),
)
@click.option(
    "--hero-n",
    type=int,
    default=4,
    show_default=True,
    help="Number of hero examples to surface at the top of the MD digest.",
)
@click.option(
    "--emit-md",
    is_flag=True,
    default=False,
    help=(
        "Also write the markdown digest (1-5 MB per state/lens; no "
        "downstream consumer — R4 artifact diet made JSON-only the "
        "default)."
    ),
)
@_allow_stale_option()
def report_recommendations(
    state: str, lens: str, hero_n: int, emit_md: bool, allow_stale: bool
) -> None:
    """State-facing recommendations — deterministic template layer (Track C)."""
    from src.report.recommendations import run as run_recommendations

    result = run_recommendations(
        state=state.upper(),
        lens=lens.lower(),  # type: ignore[arg-type]
        hero_n=hero_n,
        emit_md=emit_md,
        allow_stale=allow_stale,
    )
    click.echo(
        f"recommendations ({result['state']}/{result['lens']}): "
        f"{result['rows_with_recommendations']:,} rows with recs · "
        f"{result['recommendations_total']:,} total · "
        f"{result['rows_at_target']:,} already at target"
    )


@cli.group()
def score() -> None:
    """Scoring pipeline — extract binary facts, apply rules, aggregate per-record."""


@score.command("extract")
@click.option(
    "--fact",
    type=str,
    default="has_conditional_logic",
    show_default=True,
    help=(
        "Binary or count-int fact to extract. Phase B LLM facts: "
        "has_conditional_logic, definition_is_implementable, "
        "required_when_stated, conditional_reporting_stated, "
        "populations_or_scope_stated, has_cross_entity_logic, "
        "has_aggregation, cross_entity_targets. "
        "Deterministic facts: definition_present, business_rules_present, "
        "data_type_canonical."
    ),
)
@click.option(
    "--state",
    type=_STATE_CHOICES,
    default="AZ",
    show_default=True,
    help="State to score (Phase B fans out to all four).",
)
@_lens_option(
    default="spine",
    help="Lens to score ('spine' = API-model lens; both lenses supported).",
)
@click.option(
    "--limit",
    type=int,
    default=200,
    show_default=True,
    help="Max documented records to extract (sorted by entity,element_name).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Render prompts + manifest without hitting the LLM. No API key required.",
)
@click.option(
    "--cost-cap",
    type=float,
    default=1.0,
    show_default=True,
    help="Hard USD cap on this run; halts before the next LLM call once exceeded.",
)
@_model_option()
@click.option(
    "--validate-only",
    type=int,
    default=None,
    metavar="N",
    help=(
        "Sanity-run N records and print a downgrade-reason histogram. "
        "Writes the artifact to data/out/scoring/phase_a/validate_only/ "
        "so the committed per-state JSONL is untouched. Useful before "
        "fanning out a new fact/prompt across all 4 states. "
        "Bounded cost — N rows worth of API calls."
    ),
)
def score_extract(
    fact: str,
    state: str,
    lens: str,
    limit: int,
    dry_run: bool,
    cost_cap: float,
    model: str | None,
    validate_only: int | None,
) -> None:
    """Extract one binary fact for one state/lens (Phase A harness).

    Exit codes: 0 success, 2 usage error, 3 schema drift, 4 API failure
    after retries, 5 cost cap hit.
    """
    from src.score.extract import DEFAULT_MODEL, run as run_extract
    from src.score.schema import CostCapExceeded, ScoringSchemaError

    try:
        import anthropic  # noqa: F401  (imported lazily — only needed for non-dry-run API calls)
    except ImportError:
        if not dry_run:
            click.echo("error: anthropic SDK not installed — run `uv sync`", err=True)
            raise SystemExit(4)

    resolved_model = model or DEFAULT_MODEL

    from src.cli_render import echo_extract_result, render_extract_progress

    try:
        header = run_extract(
            fact=fact,
            state=state.upper(),
            lens=lens.lower(),
            limit=limit,
            dry_run=dry_run,
            cost_cap=cost_cap,
            model=resolved_model,
            progress_callback=None if dry_run else render_extract_progress,
            validate_only=validate_only,
        )
    except ScoringSchemaError as exc:
        click.echo(f"error: LLM response failed schema validation: {exc}", err=True)
        raise SystemExit(3)
    except CostCapExceeded as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(5)

    echo_extract_result(header, fact=fact, state=state, lens=lens.lower())


@score.command("estimate")
@_state_all_option(
    default="AZ",
    help="State(s) to estimate. Use 'all' for AZ+WI+MN+TX+IN.",
)
@_lens_option(
    default="spine",
    choices=("spine", "source"),
    help="Lens to estimate ('spine' = API-model lens; both lenses supported).",
)
@click.option(
    "--fact",
    "fact_opt",
    type=str,
    default=None,
    help=(
        "Comma-separated fact names, or 'all' for every LLM fact in the "
        "selected lens's roster. "
        "Mutually exclusive with --facts. Facts without an authored prompt "
        "use has_conditional_logic.md as a proxy template (marked '*')."
    ),
)
@click.option(
    "--facts",
    "facts_opt",
    type=str,
    default=None,
    help="Alias for --fact (e.g., --facts all). Mutually exclusive with --fact.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Max documented records per state (sorted by entity,element_name).",
)
@_model_option()
@click.option(
    "--band/--no-band",
    default=True,
    show_default=True,
    help="Print the ±40% uncertainty range band alongside the point estimate.",
)
@click.option(
    "--write/--no-write",
    default=False,
    show_default=True,
    help="Also write the markdown to data/out/scoring/phase_b/estimate.md.",
)
def score_estimate(
    state_opt: str,
    lens: str,
    fact_opt: str | None,
    facts_opt: str | None,
    limit: int | None,
    model: str | None,
    band: bool,
    write: bool,
) -> None:
    """Estimate LLM cost per (state, fact) without spending (budget-gating).

    Uses `anthropic.messages.count_tokens()` (non-billable) when
    ANTHROPIC_API_KEY is set; else falls back to a chars/4 heuristic.
    Output tokens are projected from Phase A's 18.6% out/in ratio with a
    ±40% range band.
    """
    from src.score.estimate import (
        DEFAULT_MODEL,
        facts_llm_for_lens,
        format_markdown,
        run as run_estimate,
    )
    from src.utils.paths import project_root, scoring_phase_b_estimate_path

    if fact_opt and facts_opt:
        click.echo("error: --fact and --facts are mutually exclusive", err=True)
        raise SystemExit(2)
    lens_facts = facts_llm_for_lens(lens.lower())
    raw_facts = fact_opt or facts_opt or "has_conditional_logic"
    if raw_facts.strip().lower() == "all":
        selected_facts: list[str] = list(lens_facts)
    else:
        selected_facts = [f.strip() for f in raw_facts.split(",") if f.strip()]
    unknown = [f for f in selected_facts if f not in lens_facts]
    if unknown:
        click.echo(
            f"error: unknown fact(s) {unknown}; {lens.lower()}-lens LLM facts "
            f"are {list(lens_facts)}",
            err=True,
        )
        raise SystemExit(2)

    states = list(_expand_states(state_opt))

    result = run_estimate(
        states=states,
        facts=selected_facts,
        lens=lens.lower(),
        limit=limit,
        model=model or DEFAULT_MODEL,
    )
    markdown = format_markdown(result, show_band=band)
    click.echo(markdown, nl=False)

    if write:
        path = scoring_phase_b_estimate_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        click.echo(f"\nwrote {path.relative_to(project_root())}")


@score.command("run-all")
@_state_all_option(
    default="all",
    help="State(s) to run. Use 'all' for AZ+WI+MN+TX+IN.",
)
@click.option(
    "--lens",
    type=click.Choice(["spine", "source"], case_sensitive=False),
    default="spine",
    show_default=True,
    help=(
        "Lens to score. Spine fans out the Phase B roster; source adds "
        "the Phase C2 deterministic + LLM facts so `aggregate --lens "
        "source` finds every artifact it needs."
    ),
)
@click.option(
    "--facts",
    "facts_opt",
    type=str,
    default="all",
    show_default=True,
    help=(
        "Comma-separated fact names, or 'all' for every fact in the "
        "current lens's roster (spine = Phase B; source = Phase B + "
        "source-lens deterministic + Phase C2)."
    ),
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Max documented records per state (sorted by entity,element_name).",
)
@click.option(
    "--cost-cap",
    type=float,
    default=50.0,
    show_default=True,
    help="Global USD cap across all (state, fact) pairs.",
)
@_model_option()
@click.option(
    "--checkpoint-after",
    type=_STATE_CHOICES,
    default=None,
    help=(
        "Pause after the named state's fact fanout completes and prompt "
        "for y/n to continue. Use with a new fact's first cold run "
        "(e.g., --checkpoint-after AZ) so a polarity / prompt bug that "
        "fires mid-fanout stops after the cheapest state instead of "
        "walking through WI/MN/TX at full spend."
    ),
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    default=False,
    help=(
        "Skip the interactive prompt at --checkpoint-after (proceed "
        "automatically). Also OKs the --batch cost gate when used with "
        "--batch. Useful in CI or scripted runs where you want the "
        "checkpoint summary in the log but can't respond to the "
        "confirmation."
    ),
)
@click.option(
    "--batch",
    "batch_mode",
    is_flag=True,
    default=False,
    help=(
        "Submit cache-miss prompts to the Anthropic Messages Batch API "
        "(50%% off, 24h SLA) instead of the synchronous API path. Prints "
        "the batch_id(s) and exits — collect with `mc score batches "
        "collect <batch_id>` once Anthropic finishes. No artifacts "
        "are written by submit; the next normal `score run-all` builds "
        "sidecars from the (now cached) responses at $0 LLM."
    ),
)
@click.option(
    "--max-cost",
    type=float,
    default=10.0,
    show_default=True,
    help=(
        "Estimated-cost gate for --batch. Submission aborts if the "
        "estimated batch-tier USD exceeds this value unless --yes is "
        "also set. Ignored without --batch."
    ),
)
def score_run_all(
    state_opt: str,
    lens: str,
    facts_opt: str,
    limit: int | None,
    cost_cap: float,
    model: str | None,
    checkpoint_after: str | None,
    assume_yes: bool,
    batch_mode: bool,
    max_cost: float,
) -> None:
    """Run every (state, fact) pair with skip-if-complete + streaming manifest.

    Idempotent: rerun after a crash or deliberate kill and only
    unfinished pairs execute; completed pairs read their cached
    artifact headers and are skipped. Cache replay in the extractor
    makes partial pairs cheap to resume.

    With ``--batch``, this becomes a Batch API submission instead.
    Renders every prompt the sync path would call, filters cache hits,
    submits the misses to ``messages.batches.create``, prints the
    ``batch_id``(s), and exits. Collect with ``score batches collect
    <batch_id>`` once the batch ends; then re-run ``score run-all``
    (no flag) to build sidecars at $0 from the populated cache.
    """
    from src.score.runner import (
        phase_b_facts_for_lens,
        run_all as run_all_runner,
    )

    lens_facts = phase_b_facts_for_lens(lens.lower())
    if facts_opt.strip().lower() == "all":
        selected_facts = list(lens_facts)
    else:
        selected_facts = [f.strip() for f in facts_opt.split(",") if f.strip()]
    unknown = [f for f in selected_facts if f not in lens_facts]
    if unknown:
        click.echo(
            f"error: unknown fact(s) {unknown}; currently available: "
            f"{list(lens_facts)}",
            err=True,
        )
        raise SystemExit(2)

    states = list(_expand_states(state_opt))

    from src.cli_render import (
        echo_batch_submission,
        render_run_all_checkpoint,
        render_run_all_pair,
    )

    # One home for the model ID (issue #213 item 3): extract.DEFAULT_MODEL
    # (re-exported from score.client). Imported lazily per the CLI
    # convention.
    from src.score.extract import DEFAULT_MODEL

    resolved_model = model or DEFAULT_MODEL

    if batch_mode:
        if checkpoint_after:
            click.echo(
                "error: --checkpoint-after is incompatible with --batch (no per-pair "
                "progress to checkpoint in batch submission)",
                err=True,
            )
            raise SystemExit(2)
        from src.score.batch_runner import BatchCostGateError, submit_run_all
        try:
            manifests = submit_run_all(
                states=states,
                lens=lens.lower(),
                facts=selected_facts,
                limit=limit,
                model=resolved_model,
                max_cost_usd=max_cost,
                confirm=assume_yes,
            )
        except BatchCostGateError as exc:
            click.echo(f"error: {exc}", err=True)
            raise SystemExit(2)
        if not manifests:
            click.echo(
                "All prompts already cached — no batch submitted. Run "
                "`mc score run-all` (no --batch) to build sidecars from cache."
            )
            return
        echo_batch_submission(manifests)
        return

    def _checkpoint(state: str, manifest) -> bool:
        return render_run_all_checkpoint(state, manifest, assume_yes=assume_yes)

    manifest = run_all_runner(
        states=states,
        facts=selected_facts,
        lens=lens.lower(),
        limit=limit,
        model=resolved_model,
        cost_cap=cost_cap,
        progress=render_run_all_pair,
        checkpoint_after=checkpoint_after,
        checkpoint_callback=_checkpoint if checkpoint_after else None,
    )
    click.echo(
        f"\nrun-all: {len(manifest.pairs)} pairs · "
        f"${manifest.total_usd:.4f} total · cap ${cost_cap:.2f}"
    )


@score.command("aggregate")
@_state_all_option(
    default="all",
    help="State(s) to aggregate. Use 'all' for AZ+WI+MN+TX+IN.",
)
@_lens_option(
    default="spine",
    choices=("spine", "source"),
    help="Lens to aggregate — `spine` (Phase C1) or `source` (Phase C2).",
)
@click.option(
    "--model",
    type=str,
    default="claude-sonnet-4-6",
    show_default=True,
    help="Model-id recorded in the sidecar header (metadata only).",
)
@click.option(
    "--prompt-version",
    type=str,
    default="phase-a.v1",
    show_default=True,
    help="Prompt-version recorded in the sidecar header (metadata only).",
)
@click.option(
    "--allow-stale",
    is_flag=True,
    help="Proceed when the publish manifest marks the source-lens "
         "sidecar stale/missing (spine extension adjustments degrade "
         "to the conservative unresolved fallback).",
)
def score_aggregate(
    state_opt: str, lens: str, model: str, prompt_version: str,
    allow_stale: bool,
) -> None:
    """Score every record: join facts → rules → per-record sidecar.

    Reads per-fact JSONL artifacts from data/out/scoring/phase_a/ and
    writes data/out/{state}_scores_{lens}.json per plan §8.1.
    """
    from src.score.aggregate import run as run_aggregate, run_all as run_all_aggregate

    if state_opt.lower() == "all":
        headers = run_all_aggregate(
            lens=lens.lower(), model=model, prompt_version=prompt_version,
            allow_stale=allow_stale,
        )
    else:
        headers = [
            run_aggregate(
                state=state_opt.upper(),
                lens=lens.lower(),
                model=model,
                prompt_version=prompt_version,
                allow_stale=allow_stale,
            )
        ]

    for h in headers:
        quality = h.get("mean_quality_score")
        quality_str = "n/a" if quality is None else f"{quality:.2f}"
        click.echo(
            f"  [{h['state']}] records={h['record_count']} "
            f"mean_quality={quality_str} "
            f"review_flags={h['needs_review_count']}"
        )


@score.command("aggregate-gap")
@_state_all_option(
    default="all",
    help="State(s) to aggregate. Use 'all' for AZ+WI+MN+TX+IN.",
)
@click.option(
    "--model",
    type=str,
    default="deterministic",
    show_default=True,
    help="Model-id recorded in the sidecar header (metadata only).",
)
@click.option(
    "--prompt-version",
    type=str,
    default="step1.det.v1",
    show_default=True,
    help="Prompt-version recorded in the sidecar header (metadata only).",
)
@click.option(
    "--with-llm",
    is_flag=True,
    default=False,
    help=(
        "Read LLM-fact artifacts from data/out/scoring/phase_a_gap/ in "
        "addition to deterministic facts. Step 2 / Step 3 callers set "
        "this after running `score gap-extract`. Default is Step 1 "
        "deterministic-only."
    ),
)
def score_aggregate_gap(
    state_opt: str, model: str, prompt_version: str, with_llm: bool
) -> None:
    """Score spine-anchored gap rows; emit data/out/{state}_scores_gap.json.

    Issue #73 Step 1: deterministic-only pass over `{state}_elements_gap.json`.
    Every emitted record carries `discovery_lens="spine_anchored"`.
    LLM-dependent dimensions surface at tier 0 with low confidence —
    Step 2 / Step 3 (LLM extraction) re-runs through the same pipeline
    with `--with-llm` and lifts those dimensions onto real values.
    """
    from src.score.aggregate_gap import (
        _phase_a_gap_dir,
        run as run_gap,
        run_all as run_all_gap,
    )

    llm_dir = _phase_a_gap_dir() if with_llm else None

    if state_opt.lower() == "all":
        headers = run_all_gap(
            model=model,
            prompt_version=prompt_version,
            llm_artifact_dir=llm_dir,
        )
    else:
        headers = [
            run_gap(
                state=state_opt.upper(),
                model=model,
                prompt_version=prompt_version,
                llm_artifact_dir=llm_dir,
            )
        ]

    for h in headers:
        quality = h.get("mean_quality_score")
        quality_str = "n/a" if quality is None else f"{quality:.2f}"
        click.echo(
            f"  [{h['state']}] gap_records={h['record_count']} "
            f"mean_quality={quality_str} "
            f"review_flags={h['needs_review_count']} "
            f"in_scope={h['in_scope_count']}"
        )


@score.command("gap-extract")
@_state_all_option(
    default="all",
    help="State(s) to extract gap-row LLM facts for.",
)
@click.option(
    "--sample-pct",
    type=float,
    default=0.10,
    show_default=True,
    help=(
        "Fraction of gap rows to sample per state. Step 2 default: 0.10 "
        "(10 % stratified). Step 3 / full extract: 1.0."
    ),
)
@click.option(
    "--seed",
    type=int,
    default=73,
    show_default=True,
    help="PRNG seed for sample selection. Default 73 = issue number.",
)
@click.option(
    "--cost-cap",
    type=float,
    default=25.0,
    show_default=True,
    help=(
        "Per-state cost ceiling across all 12 LLM facts. Step 2 default "
        "$25 gives ~75 %% headroom over the ~$14 expected spend on a "
        "10 %% sample of one state."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Render prompts without hitting the LLM. No API key required.",
)
def score_gap_extract(
    state_opt: str,
    sample_pct: float,
    seed: int,
    cost_cap: float,
    dry_run: bool,
) -> None:
    """Run the 12 spine-lens LLM facts over sampled gap rows (issue #73 Step 2/3).

    Sampled rows feed through the existing `extract.run()` pipeline;
    artifacts land under `data/out/scoring/phase_a_gap/` so they cannot
    collide with the source/spine artifacts under `phase_a/`. Re-run
    `mc score aggregate-gap --with-llm` afterward to fold the new LLM
    values into the gap sidecars.
    """
    from src.score.gap_extract import run_all as run_all_gap_extract

    from src.cli_render import echo_gap_extract_results

    states = list(_expand_states(state_opt))
    results = run_all_gap_extract(
        states=states,
        sample_pct=sample_pct,
        seed=seed,
        cost_cap=cost_cap,
        dry_run=dry_run,
    )
    echo_gap_extract_results(results)


@score.command("gap-downgrade-summary")
@_state_all_option(
    default="all",
    help="State(s) to summarize.",
)
def score_gap_downgrade_summary(state_opt: str) -> None:
    """Print per-fact downgrade rates across the gap-extract artifacts.

    Issue #73 Step 2 verification: halt before Step 3 if any fact's
    downgrade rate exceeds 30 % on the sample.
    """
    from src.cli_render import echo_gap_downgrade_summary
    from src.score.gap_extract import downgrade_summary

    states = list(_expand_states(state_opt))
    summary = downgrade_summary(states=states)

    if not summary["states_loaded"]:
        click.echo("No gap-extract artifacts found.")
        click.echo("  Run `mc score gap-extract` first.")
        return

    echo_gap_downgrade_summary(summary)


@score.command("peer-gap")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Render prompts + manifest without hitting the LLM. No API key required.",
)
@click.option(
    "--cost-cap",
    type=float,
    default=15.0,
    show_default=True,
    help=(
        "Hard USD cap on this run; halts before the next slot once "
        "exceeded. Full multi-state fanout at ~230 slots costs ~$7 at "
        "the prompt's typical $0.03/slot; default gives ~2x headroom."
    ),
)
@click.option(
    "--slot-set",
    type=click.Choice(["all-multi-state", "seed"], case_sensitive=False),
    default="all-multi-state",
    show_default=True,
    help=(
        "'all-multi-state' fans out to every (entity, element) spine "
        "slot documented in ≥ 2 states (production default). 'seed' "
        "replays the historical 10-slot pilot — useful for "
        "reproducibility checks; cache replay makes it $0 after the "
        "original 2026-04-24 run."
    ),
)
@_model_option()
def score_peer_gap(
    dry_run: bool, cost_cap: float, slot_set: str, model: str | None,
) -> None:
    """Run the peer-state triangulation fanout — Wave 2 Mitigation 4.

    Reads each multi-state (entity, element) bundle's per-state
    narratives from the spine-lens elements files + structural tier
    from the spine-lens scoring sidecars, prompts the LLM for one
    cross-state synthesis per slot, and writes
    ``data/out/scoring/phase_a/peer_gap.jsonl``.

    Default slot set is ``all-multi-state`` — every spine slot
    documented in ≥ 2 states. The ``seed`` set replays the original
    10-slot pilot for reproducibility.
    """
    from src.score.peer_gap import (
        DEFAULT_MODEL,
        SEED_SLOTS,
        all_multi_state_slots,
        run as run_peer_gap,
    )
    from src.score.schema import CostCapExceeded, ScoringSchemaError

    try:
        import anthropic  # noqa: F401
    except ImportError:
        if not dry_run:
            click.echo("error: anthropic SDK not installed — run `uv sync`", err=True)
            raise SystemExit(4)

    resolved_model = model or DEFAULT_MODEL

    from src.cli_render import echo_peer_gap_result, render_peer_gap_progress

    if slot_set.lower() == "seed":
        selected_slots = SEED_SLOTS
    else:
        selected_slots = all_multi_state_slots()

    try:
        header = run_peer_gap(
            slots=selected_slots,
            model=resolved_model,
            cost_cap=cost_cap,
            dry_run=dry_run,
            progress_callback=None if dry_run else render_peer_gap_progress,
        )
    except ScoringSchemaError as exc:
        click.echo(f"error: peer-gap response failed schema validation: {exc}", err=True)
        raise SystemExit(3)
    except CostCapExceeded as exc:
        click.echo(f"error: cost cap exceeded: {exc}", err=True)
        raise SystemExit(5)

    echo_peer_gap_result(header, dry_run=dry_run, cost_cap=cost_cap)


@score.group("batches")
def score_batches() -> None:
    """Anthropic Messages Batch API helpers (list / status / collect).

    Use ``mc score run-all --batch`` to submit. The commands here
    manage the lifecycle of a batch once Anthropic has accepted it:

    \b
      mc score batches list             # show submitted manifests
      mc score batches status <id>      # fetch live processing_status
      mc score batches collect <id>     # write results through cache
    """


@score_batches.command("list")
def score_batches_list() -> None:
    """List submitted batch manifests on disk."""
    from src.score.batch_manifest import list_manifests

    manifests = list_manifests()
    if not manifests:
        click.echo("No batch manifests on disk.")
        return
    click.echo(f"{'batch_id':<48} {'submitted_at':<22} {'items':>6} {'est_usd':>9}  model")
    for m in manifests:
        click.echo(
            f"{m.batch_id:<48} {m.submitted_at:<22} {len(m.items):>6} "
            f"${m.estimated_cost_usd:>7.2f}  {m.model}"
        )


@score_batches.command("status")
@click.argument("batch_id")
def score_batches_status(batch_id: str) -> None:
    """Fetch live processing_status for ``batch_id`` from Anthropic."""
    from src.score.batch_client import BatchClient

    client = BatchClient()
    status = client.status(batch_id)
    click.echo(f"batch_id: {status.batch_id}")
    click.echo(f"processing_status: {status.processing_status}")
    if status.ended_at:
        click.echo(f"ended_at: {status.ended_at}")
    if status.request_counts:
        click.echo("request_counts:")
        for key in ("processing", "succeeded", "errored", "canceled", "expired"):
            if key in status.request_counts:
                click.echo(f"  {key:<12} {status.request_counts[key]}")


@score_batches.command("collect")
@click.argument("batch_id")
@_model_option()
def score_batches_collect(batch_id: str, model: str | None) -> None:
    """Fetch results for ``batch_id`` and write through the cache.

    After this command, run ``mc score run-all`` (no --batch) to
    build sidecars from the populated cache at $0 LLM. Failed items
    (errored / parse_failed / canceled / expired) are not cached;
    a sync re-run picks them up automatically.
    """
    from src.score.batch_runner import collect_batch

    # One home for the model ID (issue #213 item 3): extract.DEFAULT_MODEL
    # (re-exported from score.client). Imported lazily per the CLI
    # convention.
    from src.score.extract import DEFAULT_MODEL

    resolved_model = model or DEFAULT_MODEL
    summary = collect_batch(batch_id, model=resolved_model)
    click.echo(
        f"batch {summary.batch_id}: {summary.succeeded_count} succeeded "
        f"({summary.cache_writes} written to cache now)"
    )
    if summary.parse_failed_count:
        click.echo(f"  parse_failed: {summary.parse_failed_count}")
    if summary.errored_count:
        click.echo(f"  errored:      {summary.errored_count}")
    if summary.canceled_count:
        click.echo(f"  canceled:     {summary.canceled_count}")
    if summary.expired_count:
        click.echo(f"  expired:      {summary.expired_count}")
    click.echo(f"  actual cost:  ${summary.actual_cost_usd:.4f} (batch tier)")


@cli.command("publish")
@click.option("--state", "state", multiple=True,
              type=_STATE_CHOICES_WITH_ALL, default=("all",),
              show_default=True,
              help="Restrict per-state stages to the named state(s); "
                   "repeatable. Cross-state stages then run only when "
                   "the other states' inputs are manifest-fresh.")
@click.option("--from", "from_stage", type=click.Choice(STAGE_NAMES),
              default=None,
              help="Start at this stage (forces it to run; later stages "
                   "follow normal freshness rules). See --dry-run for "
                   "stage names.")
@click.option("--skip", multiple=True,
              help="Skip the named stage(s). Repeatable.")
@click.option("--reports-only", is_flag=True,
              help="Shorthand for --from report-coverage.")
@click.option("--dry-run", is_flag=True,
              help="Print the resolved plan (would_run / skipped and why) "
                   "without executing or writing anything.")
@click.option("--cost-cap", type=float, default=DEFAULT_COST_CAP,
              show_default=True,
              help="Cumulative LLM spend ceiling for this run (USD).")
@click.option("--yes", "assume_yes", is_flag=True,
              help="Skip the LLM-stage confirmation gate.")
@click.option("--refresh-spine", is_flag=True,
              help="Enable the spine-fetch stage (default: pinned "
                   "artifacts — the WS-3 pattern; TX needs the local "
                   "TSDS Docker stack).")
@click.option("--tx-base-url", default=DEFAULT_TX_BASE_URL,
              show_default=True,
              help="TX sandbox base URL (local TSDS stack) for "
                   "--refresh-spine.")
@click.option("--with-gap-llm", is_flag=True,
              help="Enable the optional LLM gap-extract stage.")
@click.option("--with-spine-workbooks", is_flag=True,
              help="Also produce the internal/QA API-model-lens "
                   "workbooks.")
@click.option("--lite", is_flag=True,
              help="POC-Lite profile (docs/pipeline-lite.md): the "
                   "score-only path — ingest, source-lens extraction + "
                   "scoring, and the deliverable workbooks. Cuts the "
                   "API-model-lens scoring pass and the coverage/"
                   "divergence/reviewer-comparison analysis reports; the "
                   "free deterministic gap layer stays in so the "
                   "workbooks' Documentation Gaps sheet can't go stale. "
                   "Conflicts with --with-gap-llm/--with-spine-workbooks.")
def publish(
    state: tuple[str, ...],
    from_stage: str | None,
    skip: tuple[str, ...],
    reports_only: bool,
    dry_run: bool,
    cost_cap: float,
    assume_yes: bool,
    refresh_spine: bool,
    tx_base_url: str,
    with_gap_llm: bool,
    with_spine_workbooks: bool,
    lite: bool,
) -> None:
    """Run the full refresh chain with ordering + a freshness manifest.

    Encodes the ~35-invocation operator playbook — including its four
    silent ordering constraints — as one command. Warm reruns skip
    every fresh stage; a changed input re-runs exactly its downstream
    cone. See CLAUDE.md "Operator playbook" for the background.
    """
    import sys

    from src.publish import run as run_publish

    # --state is repeatable (issue #213 item 3): "all" anywhere wins;
    # otherwise dedupe in first-seen order. The orchestrator already
    # takes an arbitrary states tuple (partial-roster handling included).
    if any(s.lower() == "all" for s in state):
        states = SUPPORTED_STATES
    else:
        states = tuple(dict.fromkeys(s.upper() for s in state))
    try:
        result = run_publish(
            states=states,
            from_stage=from_stage,
            skip=tuple(skip),
            reports_only=reports_only,
            dry_run=dry_run,
            cost_cap=cost_cap,
            assume_yes=assume_yes,
            refresh_spine=refresh_spine,
            tx_base_url=tx_base_url,
            with_gap_llm=with_gap_llm,
            with_spine_workbooks=with_spine_workbooks,
            lite=lite,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    click.echo(result.render_table())
    if result.failed:
        sys.exit(1)


@cli.group()
def review() -> None:
    """Analyst round-trip — ingest workbook edits back into curation."""


@review.command("ingest")
@click.argument(
    "workbook",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--author",
    type=str,
    default=None,
    help="Who made these edits (recorded on every captured value; "
         "xlsx carries no cell-level authorship).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Scan and report what would be captured without writing.",
)
def review_ingest(workbook: Path, author: str | None, dry_run: bool) -> None:
    """Read the analyst-input band back into data/curation/{state}.json.

    Reads the green analyst-input columns from the workbook's Details
    sheet, keys rows by (State, Entity Name, Data Element), validates
    them against the elements artifact, and merges non-blank values into
    the per-state curation sidecar (newest-wins per column, prior values
    kept in history; blank cells never clear stored values). Every
    ``mc report analyst`` regeneration re-applies the sidecar, so
    workbook regeneration stops destroying analyst work (issue #186
    Option C / design §8.4).
    """
    from src.report.curation import run as run_ingest

    report = run_ingest(workbook, author=author, dry_run=dry_run)
    click.echo(report.render_text())


@review.command("adjudicate")
@click.option("--state", required=True, help="State code (e.g. tx).")
@click.option("--entity", required=True, help="Entity Name, verbatim.")
@click.option("--element", required=True, help="Data Element, verbatim.")
@click.option(
    "--value",
    type=float,
    required=True,
    help="The team-consensus adjusted score.",
)
@click.option(
    "--lens",
    type=click.Choice(["source", "spine"]),
    default="source",
    show_default=True,
    help="Which lens's engine score the consensus is about.",
)
@click.option(
    "--agreed-by",
    "agreed_by",
    multiple=True,
    required=True,
    help="Repeatable — one per analyst in the consensus.",
)
@click.option(
    "--rationale",
    required=True,
    help="Why the team's score differs from the engine's.",
)
@click.option(
    "--allow-stale",
    is_flag=True,
    help="Stamp the engine score even if the publish manifest says the "
         "scores sidecar is stale.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be recorded without writing.",
)
def review_adjudicate(
    state: str,
    entity: str,
    element: str,
    value: float,
    lens: str,
    agreed_by: tuple[str, ...],
    rationale: str,
    allow_stale: bool,
    dry_run: bool,
) -> None:
    """Record team consensus for one row's adjusted score (issue #248).

    Writes an `adjudication` block into data/curation/{state}.json with
    full provenance (who agreed, when, why, the engine score and plan
    version at decision time). The engine score is NEVER modified —
    the consensus renders as `Effective Score (adjudicated)` beside it,
    and adjudications never feed prompts, rules, or tests (the GT
    boundary applies to our own consensus too). If the engine score or
    plan version later moves, the adjudication goes stale: it renders
    blank and the row re-enters the Review Queue as RE-ADJUDICATE.
    """
    from src.report.curation import adjudicate

    report = adjudicate(
        state,
        entity,
        element,
        value=value,
        agreed_by=agreed_by,
        rationale=rationale,
        lens=lens,
        allow_stale=allow_stale,
        dry_run=dry_run,
    )
    click.echo(report.render_text())


@review.command("correct-fact")
@click.option("--state", required=True, help="State code (e.g. tx).")
@click.option("--entity", required=True, help="Entity Name, verbatim.")
@click.option("--element", required=True, help="Data Element, verbatim.")
@click.option(
    "--fact",
    required=True,
    help="The LLM-extracted fact to correct (e.g. has_conditional_logic).",
)
@click.option(
    "--value",
    required=True,
    help="The corrected value — true/false for bool facts, an integer "
         "for count facts, an allowed token for enum facts.",
)
@click.option(
    "--lens",
    type=click.Choice(["source", "spine"]),
    default="source",
    show_default=True,
    help="Which lens's extraction the correction is about.",
)
@click.option(
    "--author",
    required=True,
    help="Who is asserting the corrected value.",
)
@click.option(
    "--rationale",
    required=True,
    help="Why the extracted value is wrong.",
)
@click.option(
    "--allow-stale",
    is_flag=True,
    help="Stamp the prior value even if the publish manifest says the "
         "scores sidecar is stale.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be recorded without writing.",
)
def review_correct_fact(
    state: str,
    entity: str,
    element: str,
    fact: str,
    value: str,
    lens: str,
    author: str,
    rationale: str,
    allow_stale: bool,
    dry_run: bool,
) -> None:
    """Correct one LLM-extracted fact on one record (issue #249).

    Writes a `facts` block into data/curation/{state}.json with full
    provenance (author, rationale, the prior extracted value and plan
    version at correction time). The next `mc score aggregate` run
    overlays the corrected value onto the fact pool BEFORE the rule
    cascade runs, so the score recomputes for a reason the audit trail
    fully explains — the fact's sidecar provenance becomes
    `human_corrected`. The prompt cache is never written (it stays the
    immutable record of what the model said). Deterministic facts are
    rejected: a wrong deterministic fact is a code bug, not a curation
    entry. Corrections never feed prompts, rules, or tests (GT
    boundary); a correction pattern across many rows is a prompt
    weakness — route it to the prompt-version-bump path.
    """
    from src.report.curation import correct_fact

    report = correct_fact(
        state,
        entity,
        element,
        fact=fact,
        value=value,
        rationale=rationale,
        author=author,
        lens=lens,
        allow_stale=allow_stale,
        dry_run=dry_run,
    )
    click.echo(report.render_text())


def main() -> None:
    """Console-script entry point for `mc` and `python -m mc`.

    Loads a repo-root `.env` (if present) before dispatching so that
    `ANTHROPIC_API_KEY` and any future secrets don't need to be exported
    into the shell. Already-set env vars win — `.env` is a default, not
    an override. `.env` is gitignored; see `.env.example` for the
    expected shape.
    """
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=False)
    cli(prog_name="mc")


if __name__ == "__main__":
    main()
