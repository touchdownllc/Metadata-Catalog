"""The publish stage registry — the operator playbook encoded as data.

Each :class:`Stage` names what it runs (an in-process call into the
existing module ``run()``/``run_all()`` functions — the repo-wide
convention; no subprocess anywhere in ``src/poc3``), what it produces,
and what it consumes. Ordering constraints therefore stop being tribal
knowledge:

  (a) ``aggregate-source`` before ``aggregate-spine`` — the spine
      aggregate reads the source sidecar for the Phase-F extension
      fact-borrow (silently degrades to ``extension_necessity_
      unresolved`` when missing);
  (b) the gap layer (``gap-surface`` + ``aggregate-gap``) before
      ``report-review-digest`` / ``report-reviewer-comparison`` — a
      stale gap layer silently inflated the spine match rate 88.7% vs
      honest 74.4% (PR #182);
  (c) ``swagger-backfill`` before ``gap-surface`` — the gap surfacer
      skips swagger-backfilled entities, which only exist after the
      backfill ran;
  (d) ``report-recommendations`` + ``report-review-queue`` before
      ``report-analyst`` — the workbook joins both artifacts (the
      README chain historically ran analyst FIRST, baking stale
      recommendations/routes into workbooks).

``tests/test_publish.py`` proves all four structurally: every consumed
path must appear in an earlier stage's ``produces`` (or be a declared
external input).

The **lite profile** (``PublishOptions.lite`` / ``poc3 publish --lite``,
docs/pipeline-lite.md) runs the score-only path: it disables the
API-model-lens scoring pass (``extract-spine`` / ``aggregate-spine``)
and the analysis/comparison reports (coverage, review-digest,
reviewer-comparison, divergence, scoring rollups), and narrows the
review queue + the analyst stage's queue consumption to the source
lens. The free deterministic gap layer (``gap-surface`` +
``aggregate-gap``) deliberately STAYS in lite: the analyst workbook's
Documentation Gaps sheet reads the gap sidecars, and cutting them would
let that sheet drift silently after a lite re-ingest (the PR #182
incident class). The structural constraint test runs against the lite
stage subset too.

Stage callables lazily import their targets (the ``cli.py`` pattern) so
``poc3 publish --dry-run`` stays fast.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from src.ingest import INGEST_MODULES as _INGEST_MODULES
from src.states import SUPPORTED_STATES
from src.utils.paths import out_dir, project_root, spine_dir

DEFAULT_TX_BASE_URL = "http://localhost:26030/metadata/data/v3"
DEFAULT_COST_CAP = 80.0


@dataclass(frozen=True)
class PublishOptions:
    """User-facing knobs, resolved once by the orchestrator."""

    states: tuple[str, ...] = SUPPORTED_STATES
    refresh_spine: bool = False
    with_gap_llm: bool = False
    with_spine_workbooks: bool = False
    tx_base_url: str = DEFAULT_TX_BASE_URL
    cost_cap: float = DEFAULT_COST_CAP
    # POC-Lite (docs/pipeline-lite.md): score-only path — no API-model
    # lens scoring, no analysis/comparison reports.
    lite: bool = False


@dataclass
class StageContext:
    """What a stage callable may read: options + resolved directories.

    ``remaining_cost_cap`` is refreshed by the orchestrator before each
    LLM stage (global cap minus spend so far).

    ``raw_dir`` / ``curation_dir`` / ``human_scored_dir`` anchor the
    EXTERNAL inputs (issue #212 item 2) — files humans or fetches change
    out-of-band, which the freshness model must still see.
    """

    options: PublishOptions
    out_dir: Path = field(default_factory=out_dir)
    spine_dir: Path = field(default_factory=spine_dir)
    docs_dir: Path = field(default_factory=lambda: project_root() / "docs")
    raw_dir: Path = field(
        default_factory=lambda: project_root() / "data" / "raw"
    )
    curation_dir: Path = field(
        default_factory=lambda: project_root() / "data" / "curation"
    )
    human_scored_dir: Path = field(
        default_factory=lambda: (
            project_root() / "docs" / "human-scored-files"
        )
    )
    remaining_cost_cap: float = DEFAULT_COST_CAP

    @property
    def states(self) -> tuple[str, ...]:
        return self.options.states


@dataclass(frozen=True)
class Stage:
    """One orchestrated step. ``run`` returns the stage's LLM spend in
    USD (0.0 for free stages)."""

    name: str
    description: str
    cost: Literal["free", "llm"]
    run: Callable[[StageContext], float]
    produces: Callable[[StageContext], tuple[Path, ...]]
    consumes: Callable[[StageContext], tuple[Path, ...]]
    enabled: Callable[[PublishOptions], bool] = lambda opts: True
    # CLI flag that turns a default-OFF stage on (e.g. "--with-gap-llm").
    # Purely for error messages: `enabled` is an opaque lambda, so the
    # orchestrator's `--from <disabled stage>` guard (issue #211 item 5c)
    # reads this to tell the operator which flag to pass. None on
    # always-on and lite-gated stages (the lite guard has its own text).
    flag: str | None = None
    # Cross-state stages (combined workbook, cross-state reports) only
    # run on a partial --state selection when the other states' inputs
    # are still fresh; the orchestrator enforces this.
    cross_state: bool = False
    # False → never skipped via the manifest (network fetches); these
    # stages rely on their own internal idempotency.
    manifest_tracked: bool = True
    # EXTERNAL inputs (issue #212 item 2): files changed out-of-band —
    # raw source caches, curation sidecars, hand-placed reviewer
    # workbooks. Unlike `consumes` they need no in-chain producer (the
    # registry's structural test exempts them). The orchestrator
    # snapshots them AFTER the stage runs (adapters may top up their own
    # caches mid-run) and records them into the manifest's `inputs` —
    # missing files as the ABSENT sentinel — so `_fresh()` re-runs the
    # stage when one changes, appears, or disappears.
    externals: Callable[[StageContext], tuple[Path, ...]] = lambda ctx: ()
    # Version/code tokens (issue #212 item 1): every token returned here
    # must match the manifest record or the stage is stale. Coarse by
    # design — the warm chain is ~35s at $0, so over-triggering is
    # nearly free while under-triggering was the silent-no-op incident
    # class. NOTE: a mutating stage (swagger-backfill) becomes the LAST
    # producer of the artifacts it rewrites, so stages sharing produced
    # artifacts MUST share the same code_versions callable or they
    # invalidate each other forever.
    code_versions: Callable[[], dict[str, str]] = lambda: {}


# --- code fingerprints (issue #212 item 1) -----------------------------------
#
# One coarse sha256 per package family over every .py/.md source file.
# A fingerprint mismatch anywhere in the family re-runs the family's
# stages; over-triggering costs ~seconds on the warm chain while
# under-triggering was the "publish said success but refreshed nothing"
# incident class. `PROMPT_VERSION` rides alongside for the extract
# stages (it is already the prompt-cache namespace, i.e. the natural
# re-spend token). The lru_cache is per-process: publish runs are
# short-lived CLI invocations, so source edits between runs are always
# seen.


@functools.lru_cache(maxsize=None)
def _source_fingerprint(*packages: str) -> str:
    root = Path(__file__).resolve().parents[1]  # src/poc3
    h = hashlib.sha256()
    for pkg in packages:
        base = root / pkg
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.suffix not in (".py", ".md"):
                continue
            h.update(f.relative_to(root).as_posix().encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()[:16]


def _plan_version() -> dict[str, str]:
    from src.score.aggregate import SCORING_PLAN_VERSION

    return {"scoring_plan_version": SCORING_PLAN_VERSION}


def _spine_code_versions() -> dict[str, str]:
    return {"code_fingerprint": _source_fingerprint("spine", "models", "utils")}


def _ingest_code_versions() -> dict[str, str]:
    # Shared verbatim by ingest / swagger-backfill / gap-surface: the
    # backfill MUTATES the elements artifacts ingest produced, so it
    # becomes their recorded producer — divergent tokens between the
    # two would read as permanently stale (see Stage.code_versions).
    return {"code_fingerprint": _source_fingerprint("ingest", "models", "utils")}


def _score_code_versions() -> dict[str, str]:
    return {
        **_plan_version(),
        "code_fingerprint": _source_fingerprint("score", "models", "utils"),
    }


def _extract_code_versions() -> dict[str, str]:
    from src.score.extract import PROMPT_VERSION

    # No scoring_plan_version here: a plan bump requires re-AGGREGATION,
    # not re-extraction (facts are upstream of the rule cascade). The
    # score-package fingerprint already covers prompt-file edits
    # (prompts/*.md) and extraction-code changes.
    return {
        "prompt_version": PROMPT_VERSION,
        "code_fingerprint": _source_fingerprint("score", "models", "utils"),
    }


def _report_code_versions() -> dict[str, str]:
    # Reports render score content (rubric prose lives in score/), so
    # both packages participate. This retires the documented
    # `poc3 publish --from report-analyst` workaround for
    # presentation-layer code changes — the fingerprint now sees them.
    return {
        **_plan_version(),
        "code_fingerprint": _source_fingerprint(
            "report", "score", "models", "utils"
        ),
    }


def _coverage_code_versions() -> dict[str, str]:
    # Coverage/divergence read elements only — no scoring content, so
    # no plan-version token (a plan bump alone must not re-run them).
    return {
        "code_fingerprint": _source_fingerprint(
            "report", "score", "models", "utils"
        ),
    }


# --- path helpers (all repo-relative under data/) ---------------------------


def _elements(ctx: StageContext, lens: str) -> tuple[Path, ...]:
    return tuple(
        ctx.out_dir / f"{st.lower()}_elements_{lens}.json"
        for st in ctx.states
    )


def _scores(ctx: StageContext, lens: str) -> tuple[Path, ...]:
    return tuple(
        ctx.out_dir / f"{st.lower()}_scores_{lens}.json" for st in ctx.states
    )


def _spines(ctx: StageContext) -> tuple[Path, ...]:
    return tuple(
        ctx.spine_dir / f"{st.lower()}_spine.json" for st in ctx.states
    )


def _gap_elements(ctx: StageContext) -> tuple[Path, ...]:
    return tuple(
        ctx.out_dir / f"{st.lower()}_elements_gap.json" for st in ctx.states
    )


def _phase_a_files(ctx: StageContext, lens: str) -> tuple[Path, ...]:
    """Per-lens extraction artifacts as EXPLICIT file keys.

    Both extract stages used to declare the shared ``phase_a`` directory
    as their produce (issue #212 item 7): the manifest entry was
    overwritten by whichever lens ran last, its recorded inputs were
    wrong half the time, and scratch subdirs (``validate_only/``,
    ``dry_run/``, ``run_manifest.json``) polluted the directory hash.
    The file list is derived from the SAME roster function the extract
    stage body passes to ``run_all`` — the declaration cannot drift from
    what the stage writes, and a fact added to the roster reads as a
    missing (stale) artifact until extracted.
    """
    from src.score.runner import phase_b_facts_for_lens

    return tuple(
        ctx.out_dir / "scoring" / "phase_a"
        / f"{st.upper()}_{lens}_{fact}.jsonl"
        for st in ctx.states
        for fact in phase_b_facts_for_lens(lens)
    )


def _phase_a_gap(ctx: StageContext) -> Path:
    return ctx.out_dir / "scoring" / "phase_a_gap"


def _raw_swagger(ctx: StageContext) -> tuple[Path, ...]:
    """Raw swagger caches (``data/raw/{st}/swagger/``) — produced by
    spine-fetch, external inputs of spine-build."""
    return tuple(ctx.raw_dir / st.lower() / "swagger" for st in ctx.states)


def _raw_source_inputs(ctx: StageContext) -> tuple[Path, ...]:
    """The ingest adapters' out-of-band inputs, coarse by design: the
    whole per-state raw cache dir (TWEDS cache, Confluence scrapes, MN
    matrix, AZ/IN XLSX drops) plus the committed bootstrap fallbacks.
    A refreshed source document re-runs ingest; hidden-file churn is
    excluded by ``sha256_path``."""
    bootstrap = project_root() / "docs" / "bootstrap"
    return (
        *(ctx.raw_dir / st.lower() for st in ctx.states),
        *(bootstrap / st.lower() for st in ctx.states),
    )


def _curation_sidecars(ctx: StageContext) -> tuple[Path, ...]:
    """Analyst round-trip sidecars (``data/curation/{state}.json``) —
    written by `poc3 review ingest`, re-applied by every report-analyst
    run. Declaring them makes `review ingest` → `poc3 publish`
    actually regenerate the workbooks (issue #212 item 2)."""
    return tuple(
        ctx.curation_dir / f"{st.lower()}.json" for st in ctx.states
    )


def _reviewer_workbooks(ctx: StageContext) -> tuple[Path, ...]:
    """The five hand-placed human-scored workbooks
    (``review_loader.REVIEWER_SOURCES``) the review-digest resolves
    reviewer rows from — swapping one in must re-run the digest."""
    from src.score.review_loader import REVIEWER_SOURCES

    return tuple(
        s.path(ctx.human_scored_dir) for s in REVIEWER_SOURCES
    )


def _recs(ctx: StageContext, lens: str) -> tuple[Path, ...]:
    return tuple(
        ctx.out_dir / f"{st.lower()}_recommendations_{lens}.json"
        for st in ctx.states
    )


def _analyst_workbooks(ctx: StageContext) -> tuple[Path, ...]:
    suffixes = [""]
    if ctx.options.with_spine_workbooks:
        suffixes.append("_spine")
    paths = []
    for suffix in suffixes:
        paths.extend(
            ctx.out_dir / f"{st.lower()}_analyst{suffix}.xlsx"
            for st in ctx.states
        )
        paths.append(ctx.out_dir / f"coverage_analyst{suffix}.xlsx")
    return tuple(paths)


# --- stage bodies (lazy imports, in-process calls) ---------------------------


def _run_spine_fetch(ctx: StageContext) -> float:
    from src.spine.fetch import fetch_state_swagger

    for st in ctx.states:
        base_url = ctx.options.tx_base_url if st == "TX" else None
        try:
            fetch_state_swagger(st, base_url=base_url)
        except Exception as exc:  # noqa: BLE001 — re-raise with runbook
            if st == "TX":
                raise RuntimeError(
                    "TX spine fetch failed — the local TSDS Vendor SDK "
                    "stack is probably down. Bring it up with: cd "
                    "infra/tsds-sdk && DOCKER_DEFAULT_PLATFORM=linux/amd64 "
                    "docker compose -p tsds_ods_sdk_2026_2_2 --env-file "
                    "./settings.env up -d   (see CLAUDE.md Source URLs). "
                    f"Original error: {exc}"
                ) from exc
            raise
    return 0.0


def _run_spine_build(ctx: StageContext) -> float:
    from src.spine.build import build_state_spine

    for st in ctx.states:
        build_state_spine(st)
    return 0.0


def _run_ingest(ctx: StageContext) -> float:
    for st in ctx.states:
        module = importlib.import_module(
            f"src.ingest.{_INGEST_MODULES[st]}"
        )
        module.run()
    return 0.0


def _run_swagger_backfill(ctx: StageContext) -> float:
    from src.ingest.swagger_backfill import run as run_backfill

    for st in ctx.states:
        run_backfill(st)
    return 0.0


def _run_gap_surface(ctx: StageContext) -> float:
    from src.ingest.gap_surfacer import run as run_gap

    for st in ctx.states:
        run_gap(st)
    return 0.0


def _extract(ctx: StageContext, lens: str) -> float:
    from src.score.runner import phase_b_facts_for_lens, run_all

    manifest = run_all(
        states=list(ctx.states),
        facts=phase_b_facts_for_lens(lens),
        lens=lens,
        cost_cap=ctx.remaining_cost_cap,
    )
    return float(manifest.total_usd)


def _run_aggregate(ctx: StageContext, lens: str) -> float:
    from src.score.aggregate import run_all

    run_all(lens=lens, states=list(ctx.states))
    return 0.0


def _run_gap_extract(ctx: StageContext) -> float:
    from src.score.gap_extract import run_all

    results = run_all(
        states=list(ctx.states), cost_cap=ctx.remaining_cost_cap
    )
    return float(sum(r.total_usd for r in results))


def _run_aggregate_gap(ctx: StageContext) -> float:
    from src.score.aggregate_gap import run_all

    kwargs = {}
    if ctx.options.with_gap_llm:
        # One home for the model ID (issue #213 item 3): extract's
        # DEFAULT_MODEL (re-exported from score.client).
        from src.score.extract import DEFAULT_MODEL

        kwargs = {
            "model": DEFAULT_MODEL,
            "prompt_version": "step3.full.v1",
        }
    run_all(states=list(ctx.states), **kwargs)
    return 0.0


def _run_report_coverage(ctx: StageContext) -> float:
    from src.report.coverage import run

    for lens in ("source", "spine"):
        run(states=ctx.states, lens=lens)
    return 0.0


def _run_report_recommendations(ctx: StageContext) -> float:
    from src.report.recommendations import run

    lenses = ["source"]
    if ctx.options.with_spine_workbooks:
        lenses.append("spine")
    for st in ctx.states:
        for lens in lenses:
            run(state=st, lens=lens)
    return 0.0


def _review_queue_lenses(ctx: StageContext) -> tuple[str, ...]:
    """Lite runs the source-lens queue only (no spine sidecars exist)."""
    return ("source",) if ctx.options.lite else ("source", "spine")


def _run_report_review_queue(ctx: StageContext) -> float:
    from src.report.review_queue import run

    for lens in _review_queue_lenses(ctx):
        run(lens=lens, states=ctx.states)
    return 0.0


def _run_report_analyst(ctx: StageContext) -> float:
    from src.report.analyst import run

    # `state=None` produces all five per-state workbooks + combined (the
    # old `all_states` flag was never read — issue #213 item 1).
    run(lens="source")
    if ctx.options.with_spine_workbooks:
        run(lens="spine")
    return 0.0


def _run_report_review_digest(ctx: StageContext) -> float:
    from src.report.review_digest import run

    for lens in ("source", "spine"):
        run(lens=lens)
    return 0.0


def _run_reviewer_comparison(ctx: StageContext) -> float:
    from src.report.reviewer_comparison_summary import run

    run()
    return 0.0


def _run_divergence(ctx: StageContext) -> float:
    from src.report.divergence import run

    run(states=ctx.states)
    return 0.0


def _run_report_scoring(ctx: StageContext) -> float:
    from src.report.scoring import run

    for lens in ("source", "spine"):
        run(lens=lens, states=ctx.states)
    return 0.0


# --- the registry, in dependency order ---------------------------------------

STAGES: tuple[Stage, ...] = (
    Stage(
        name="spine-fetch",
        description="Fetch swagger from the state sandboxes (TX needs the "
                    "local TSDS Docker stack). Default OFF — pinned "
                    "artifacts refresh manually/quarterly (WS-3 pattern). "
                    "WI/IN school-year selection stays a manual concern "
                    "(`poc3 spine fetch`).",
        cost="free",
        run=_run_spine_fetch,
        # The raw caches ARE the fetch's output: declaring them lets the
        # refreshed swagger dirty spine-build's externals in the same
        # run — `--refresh-spine` used to fetch and then watch the whole
        # downstream cone skip fresh (issue #212 item 2).
        produces=_raw_swagger,
        consumes=lambda ctx: (),
        enabled=lambda opts: opts.refresh_spine,
        flag="--refresh-spine",
        manifest_tracked=False,
    ),
    Stage(
        name="spine-build",
        description="Build per-state spine manifests from cached swagger.",
        cost="free",
        run=_run_spine_build,
        produces=_spines,
        consumes=lambda ctx: (),
        externals=_raw_swagger,
        code_versions=_spine_code_versions,
    ),
    Stage(
        name="ingest",
        description="Per-state ingestion adapters (both lens artifacts).",
        cost="free",
        run=_run_ingest,
        produces=lambda ctx: (
            *_elements(ctx, "source"),
            *_elements(ctx, "spine"),
            *(ctx.out_dir / f"{st.lower()}_gap_log.json"
              for st in ctx.states),
        ),
        consumes=_spines,
        externals=_raw_source_inputs,
        code_versions=_ingest_code_versions,
    ),
    Stage(
        name="swagger-backfill",
        description="Append swagger-as-source rows; flips matching spine "
                    "rows to documented (issue #70). Mutates the elements "
                    "artifacts in place.",
        cost="free",
        run=_run_swagger_backfill,
        produces=lambda ctx: (
            *_elements(ctx, "source"),
            *_elements(ctx, "spine"),
        ),
        consumes=lambda ctx: (
            *_elements(ctx, "source"),
            *_elements(ctx, "spine"),
            *_spines(ctx),
        ),
        code_versions=_ingest_code_versions,
    ),
    Stage(
        name="gap-surface",
        description="Surface spine-anchored gap rows (constraint c: AFTER "
                    "swagger-backfill — the surfacer skips backfilled "
                    "entities).",
        cost="free",
        run=_run_gap_surface,
        produces=_gap_elements,
        consumes=lambda ctx: (*_elements(ctx, "source"), *_spines(ctx)),
        code_versions=_ingest_code_versions,
    ),
    Stage(
        name="extract-source",
        description="Phase A/B fact extraction, source lens (LLM; prompt "
                    "cache makes warm reruns $0).",
        cost="llm",
        run=lambda ctx: _extract(ctx, "source"),
        produces=lambda ctx: _phase_a_files(ctx, "source"),
        consumes=lambda ctx: _elements(ctx, "source"),
        code_versions=_extract_code_versions,
    ),
    Stage(
        name="extract-spine",
        description="Phase A/B fact extraction, API-model lens (LLM).",
        cost="llm",
        run=lambda ctx: _extract(ctx, "spine"),
        produces=lambda ctx: _phase_a_files(ctx, "spine"),
        consumes=lambda ctx: _elements(ctx, "spine"),
        enabled=lambda opts: not opts.lite,
        code_versions=_extract_code_versions,
    ),
    Stage(
        name="aggregate-source",
        description="Source-lens sidecars (MUST precede aggregate-spine — "
                    "constraint a).",
        cost="free",
        run=lambda ctx: _run_aggregate(ctx, "source"),
        produces=lambda ctx: _scores(ctx, "source"),
        consumes=lambda ctx: (
            *_phase_a_files(ctx, "source"),
            *_elements(ctx, "source"),
        ),
        # Issue #249: analyst fact corrections overlay the fact pool at
        # aggregate time, so a `review correct-fact` write (or the
        # first-ever curation sidecar appearing) must re-run the
        # scoring cone — same declaration as report-analyst's.
        externals=_curation_sidecars,
        code_versions=_score_code_versions,
    ),
    Stage(
        name="aggregate-spine",
        description="API-model-lens sidecars (reads the source sidecars "
                    "for the Phase-F extension fact-borrow).",
        cost="free",
        run=lambda ctx: _run_aggregate(ctx, "spine"),
        produces=lambda ctx: _scores(ctx, "spine"),
        consumes=lambda ctx: (
            *_phase_a_files(ctx, "spine"),
            *_elements(ctx, "spine"),
            *_scores(ctx, "source"),  # constraint (a), now structural
        ),
        # Issue #249: spine-lens corrections apply here (source-lens
        # ones arrive via the sidecar borrow, covered by consumes).
        externals=_curation_sidecars,
        enabled=lambda opts: not opts.lite,
        code_versions=_score_code_versions,
    ),
    Stage(
        name="gap-extract",
        description="Optional LLM gap-row extraction (default OFF; "
                    "deterministic gap scoring needs no LLM).",
        cost="llm",
        run=_run_gap_extract,
        produces=lambda ctx: (_phase_a_gap(ctx),),
        consumes=_gap_elements,
        enabled=lambda opts: opts.with_gap_llm and not opts.lite,
        flag="--with-gap-llm",
        code_versions=_extract_code_versions,
    ),
    Stage(
        name="aggregate-gap",
        description="Deterministic gap sidecars (constraint b producer).",
        cost="free",
        run=_run_aggregate_gap,
        produces=lambda ctx: tuple(
            ctx.out_dir / f"{st.lower()}_scores_gap.json"
            for st in ctx.states
        ),
        consumes=lambda ctx: (
            *_gap_elements(ctx),
            *((_phase_a_gap(ctx),) if ctx.options.with_gap_llm else ()),
        ),
        code_versions=_score_code_versions,
    ),
    Stage(
        name="report-coverage",
        description="Coverage reports, both lenses.",
        cost="free",
        run=_run_report_coverage,
        produces=lambda ctx: tuple(
            ctx.out_dir / f"coverage_report{sfx}.{ext}"
            for sfx in ("", "_spine")
            for ext in ("json", "md")
        ),
        consumes=lambda ctx: (
            *_elements(ctx, "source"),
            *_elements(ctx, "spine"),
        ),
        cross_state=True,
        enabled=lambda opts: not opts.lite,
        code_versions=_coverage_code_versions,
    ),
    Stage(
        name="report-recommendations",
        description="Per-state recommendations JSON (constraint d: BEFORE "
                    "report-analyst, which joins it into the workbook).",
        cost="free",
        run=_run_report_recommendations,
        # The spine-lens recs were written UNDECLARED under
        # --with-spine-workbooks (issue #212 item 7) — untracked, never
        # dirtying anything, skippable-fresh while stale.
        produces=lambda ctx: (
            *_recs(ctx, "source"),
            *(_recs(ctx, "spine")
              if ctx.options.with_spine_workbooks else ()),
        ),
        consumes=lambda ctx: (
            *_scores(ctx, "source"),
            *(_scores(ctx, "spine")
              if ctx.options.with_spine_workbooks else ()),
        ),
        code_versions=_report_code_versions,
    ),
    Stage(
        name="report-review-queue",
        description="Review-queue artifacts, both lenses (constraint d: "
                    "BEFORE report-analyst).",
        cost="free",
        run=_run_report_review_queue,
        produces=lambda ctx: tuple(
            ctx.out_dir / f"review_queue_{lens}.{ext}"
            for lens in _review_queue_lenses(ctx)
            for ext in ("json", "md")
        ),
        consumes=lambda ctx: (
            *_scores(ctx, "source"),
            *(() if ctx.options.lite else _scores(ctx, "spine")),
        ),
        cross_state=True,
        code_versions=_report_code_versions,
    ),
    Stage(
        name="report-analyst",
        description="The deliverable workbooks: 5 per-state + 1 combined "
                    "(spine set opt-in via --with-spine-workbooks).",
        cost="free",
        run=_run_report_analyst,
        produces=_analyst_workbooks,
        consumes=lambda ctx: (
            *_elements(ctx, "source"),
            *_elements(ctx, "spine"),
            *_scores(ctx, "source"),
            *(_scores(ctx, "spine")
              if ctx.options.with_spine_workbooks else ()),
            *_recs(ctx, "source"),
            *(_recs(ctx, "spine")
              if ctx.options.with_spine_workbooks else ()),
            ctx.out_dir / "review_queue_source.json",
            # Lite never produces the spine queue; the analyst loader
            # tolerates its absence (returns {} routes for that lens).
            *(() if ctx.options.lite
              else (ctx.out_dir / "review_queue_spine.json",)),
        ),
        # The round-trip promise: `poc3 review ingest <workbook>` →
        # `poc3 publish` regenerates the workbooks with the analyst
        # edits re-applied (issue #212 item 2).
        externals=_curation_sidecars,
        cross_state=True,
        code_versions=_report_code_versions,
    ),
    Stage(
        name="report-review-digest",
        description="Reviewer digests, both lenses (constraint b: AFTER "
                    "the gap layer — a stale gap layer silently inflated "
                    "the spine match rate, PR #182).",
        cost="free",
        run=_run_report_review_digest,
        produces=lambda ctx: tuple(
            ctx.out_dir / f"review_digest_{lens}.{ext}"
            for lens in ("source", "spine")
            for ext in ("json", "md")
        ),
        consumes=lambda ctx: (
            *_scores(ctx, "source"),
            *_scores(ctx, "spine"),
            *_gap_elements(ctx),
            *(ctx.out_dir / f"{st.lower()}_scores_gap.json"
              for st in ctx.states),
        ),
        # Swapping in an updated reviewer workbook (2026-07-07 happened
        # exactly this way) must re-run the digest.
        externals=_reviewer_workbooks,
        cross_state=True,
        enabled=lambda opts: not opts.lite,
        code_versions=_report_code_versions,
    ),
    Stage(
        name="report-reviewer-comparison",
        description="The committed reviewer-comparison doc (REMEMBER to "
                    "commit docs/reviewer-comparison.md).",
        cost="free",
        run=_run_reviewer_comparison,
        produces=lambda ctx: (
            project_root() / "docs" / "reviewer-comparison.md",
        ),
        consumes=lambda ctx: (
            ctx.out_dir / "review_digest_source.json",
            ctx.out_dir / "review_digest_spine.json",
        ),
        cross_state=True,
        enabled=lambda opts: not opts.lite,
        code_versions=_report_code_versions,
    ),
    Stage(
        name="report-divergence",
        description="Cross-lens divergence report.",
        cost="free",
        run=_run_divergence,
        produces=lambda ctx: tuple(
            ctx.out_dir / f"lens_divergence.{ext}" for ext in ("json", "md")
        ),
        consumes=lambda ctx: (
            *_elements(ctx, "source"),
            *_elements(ctx, "spine"),
        ),
        cross_state=True,
        enabled=lambda opts: not opts.lite,
        code_versions=_coverage_code_versions,
    ),
    Stage(
        name="report-scoring",
        description="Cross-state scoring rollups, both lenses.",
        cost="free",
        run=_run_report_scoring,
        produces=lambda ctx: tuple(
            ctx.out_dir / f"scoring_report_{lens}.{ext}"
            for lens in ("source", "spine")
            for ext in ("json", "md")
        ),
        consumes=lambda ctx: (
            *_scores(ctx, "source"),
            *_scores(ctx, "spine"),
        ),
        cross_state=True,
        enabled=lambda opts: not opts.lite,
        code_versions=_report_code_versions,
    ),
)

STAGE_NAMES: tuple[str, ...] = tuple(s.name for s in STAGES)

# Excluded from v1 (documented): `score peer-gap` (not in the operator
# refresh chain), `report human-overlay` (external reviewer files),
# batch-transport extraction (the WS-3 job will own submit/collect).
