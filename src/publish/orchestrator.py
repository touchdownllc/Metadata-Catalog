"""`poc3 publish` — one command for the whole refresh chain (R1).

Runs the stage registry in order, in-process, with:

- **skip-if-fresh**: a stage is skipped when every artifact it produces
  exists with a manifest hash matching the file on disk AND none of its
  consumed paths were rewritten earlier in this run (dirty-path
  propagation). Warm reruns are therefore no-ops; a changed input
  re-runs exactly the downstream cone.
- **stop-the-line**: the first failing stage aborts the run (continuing
  past a failed producer manufactures the stale-composite incident
  class this command exists to kill — PR #182); the error names the
  resume command (``poc3 publish --from <stage>``).
- **LLM gate**: before the first live LLM stage, confirm cost class +
  cap unless ``--yes``; cumulative spend tracked against ``--cost-cap``.
- **freshness manifest**: every produced artifact recorded with its
  input hashes (``publish_manifest.json``) — the reader-side
  ``verify_fresh`` gates (sequence 4 PR B) consume it.

WS-3 alignment: this step sequence IS the future hosted job's internal
step sequence (scoring-pipeline-design.md — "the orchestration playbook
encoded as the job's internal step sequence; CLI retained as a
dev/local harness").
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Literal

from src.publish.manifest import (
    ABSENT,
    PublishManifest,
    abs_for,
    sha256_path,
)
from src.publish.stages import (
    DEFAULT_COST_CAP,
    DEFAULT_TX_BASE_URL,
    STAGES,
    PublishOptions,
    Stage,
    StageContext,
)
from src.states import SUPPORTED_STATES

logger = logging.getLogger("src.publish")

Status = Literal[
    "complete",
    "skipped_fresh",
    "skipped_disabled",
    "skipped_flag",
    "skipped_partial_states",
    "failed",
    "not_reached",
    "would_run",
]


@dataclass
class StageOutcome:
    name: str
    status: Status
    duration_s: float = 0.0
    cost_usd: float = 0.0
    detail: str = ""


@dataclass
class PublishResult:
    outcomes: list[StageOutcome] = field(default_factory=list)
    total_llm_usd: float = 0.0
    failed: str | None = None
    dry_run: bool = False
    manifest_path: Path | None = None

    def render_table(self) -> str:
        width = max((len(o.name) for o in self.outcomes), default=10)
        lines = [
            f"{'stage':<{width}}  {'status':<22} {'time':>8}  {'cost':>8}"
        ]
        for o in self.outcomes:
            time_s = f"{o.duration_s:.1f}s" if o.duration_s else ""
            cost = f"${o.cost_usd:.2f}" if o.cost_usd else ""
            detail = f"  — {o.detail}" if o.detail else ""
            lines.append(
                f"{o.name:<{width}}  {o.status:<22} {time_s:>8}  "
                f"{cost:>8}{detail}"
            )
        if self.dry_run:
            lines.append("(dry run — nothing executed, nothing written)")
        elif self.failed:
            lines.append(
                f"FAILED at '{self.failed}' — resume with: "
                f"poc3 publish --from {self.failed}"
            )
        else:
            lines.append(
                f"done — total LLM spend this run: "
                f"${self.total_llm_usd:.2f}"
            )
        return "\n".join(lines)


def _default_confirm(message: str) -> bool:
    import click

    return click.confirm(message, default=True)


def run(
    *,
    states: tuple[str, ...] | None = None,
    from_stage: str | None = None,
    skip: tuple[str, ...] = (),
    reports_only: bool = False,
    dry_run: bool = False,
    cost_cap: float = DEFAULT_COST_CAP,
    assume_yes: bool = False,
    refresh_spine: bool = False,
    tx_base_url: str = DEFAULT_TX_BASE_URL,
    with_gap_llm: bool = False,
    with_spine_workbooks: bool = False,
    lite: bool = False,
    stages: tuple[Stage, ...] | None = None,
    manifest_path: Path | None = None,
    out_dir_override: Path | None = None,
    spine_dir_override: Path | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> PublishResult:
    """One-command refresh. Plain function — cli.py wraps it (never
    ``@click.command`` on a module ``run()``).

    The trailing keyword arguments (``stages`` / ``manifest_path`` /
    ``out_dir_override`` / ``spine_dir_override`` / ``confirm``) are
    test-injection points.
    """
    registry = stages if stages is not None else STAGES
    names = [s.name for s in registry]
    for label, value in (("--from", from_stage), *((("--skip", s) for s in skip))):
        if value is not None and value not in names:
            raise ValueError(
                f"unknown stage {value!r} for {label}; valid stages: "
                f"{', '.join(names)}"
            )
    if from_stage is not None and from_stage in skip:
        raise ValueError(
            f"--from {from_stage!r} conflicts with --skip "
            f"{from_stage!r}: the forced stage would be skipped and the "
            "run would report success having executed nothing"
        )
    if lite and with_gap_llm:
        raise ValueError(
            "--lite and --with-gap-llm conflict: the lite profile's only "
            "LLM stage is source-lens extraction (the gap layer stays "
            "deterministic)"
        )
    if lite and with_spine_workbooks:
        raise ValueError(
            "--lite and --with-spine-workbooks conflict: the API-model-"
            "lens workbooks need the spine-lens scoring the lite profile "
            "cuts"
        )

    selected = tuple(st.upper() for st in (states or SUPPORTED_STATES))
    options = PublishOptions(
        states=selected,
        refresh_spine=refresh_spine,
        with_gap_llm=with_gap_llm,
        with_spine_workbooks=with_spine_workbooks,
        tx_base_url=tx_base_url,
        cost_cap=cost_cap,
        lite=lite,
    )
    if lite and from_stage is not None:
        target = next(s for s in registry if s.name == from_stage)
        if not target.enabled(options) and target.enabled(
            replace(options, lite=False)
        ):
            lite_names = [s.name for s in registry if s.enabled(options)]
            raise ValueError(
                f"--from {from_stage!r} names a stage the --lite profile "
                f"disables; lite stages: {', '.join(lite_names)}"
            )
    if from_stage is not None:
        # Generalized from the lite guard above (issue #211 item 5c):
        # without this, `--from gap-extract` without `--with-gap-llm`
        # (or `--from spine-fetch` without `--refresh-spine`) was a
        # green, successful-looking run that executed nothing — the
        # skipped_disabled branch fired before `forced` was computed.
        target = next(s for s in registry if s.name == from_stage)
        if not target.enabled(options):
            hint = (
                f"enable it with {target.flag}"
                if target.flag
                else "it is disabled by the current options"
            )
            raise ValueError(
                f"--from {from_stage!r} names a disabled stage — {hint}"
            )
    if reports_only and from_stage is None:
        from_stage = next(
            (s.name for s in registry
             if s.name.startswith("report-") and s.enabled(options)),
            None,
        )
    from_index = names.index(from_stage) if from_stage is not None else 0
    ctx_kwargs = {}
    if out_dir_override is not None:
        ctx_kwargs["out_dir"] = out_dir_override
    if spine_dir_override is not None:
        ctx_kwargs["spine_dir"] = spine_dir_override
    ctx = StageContext(options=options, **ctx_kwargs)
    # Cross-state stages run against the FULL roster (when fresh).
    full_ctx = StageContext(
        options=replace(options, states=SUPPORTED_STATES),
        **ctx_kwargs,
    )
    partial = set(selected) != set(SUPPORTED_STATES)

    manifest = PublishManifest.load_or_create(manifest_path)
    if not dry_run:
        manifest.start_run(
            ["publish", "--state", ",".join(selected)]
            + (["--from", from_stage] if from_stage else [])
            + [f"--skip={s}" for s in skip]
            + (["--reports-only"] if reports_only else [])
        )

    result = PublishResult(dry_run=dry_run, manifest_path=manifest.path)
    confirm_fn = confirm or _default_confirm
    llm_gate_passed = assume_yes
    spent = 0.0
    dirty: set[str] = set()
    # Produces of --skip'd stages that were NOT fresh at skip time
    # (issue #212 item 7): downstream consumers run with a loud warning
    # and their outputs stay UNRECORDED, so the manifest never certifies
    # a lineage that deliberately omitted a non-fresh stage and the next
    # honest run re-runs the whole cone.
    tainted: set[str] = set()
    tainted_stages: list[str] = []
    aborted = False

    # Run-scoped hash memo: the recorded-inputs check re-verifies every
    # input of every produced artifact (that is what makes EXTERNAL
    # inputs propagate), and stages share inputs heavily — without the
    # memo the phase_a file family would be re-hashed once per consumer.
    # Entries are invalidated whenever a path is dirtied (rewritten).
    hash_memo: dict[str, str | None] = {}

    def _key(path: Path) -> str:
        return str(Path(path).resolve())

    def _hash(path: Path) -> str | None:
        key = _key(path)
        if key not in hash_memo:
            hash_memo[key] = sha256_path(Path(path))
        return hash_memo[key]

    def _mark_dirty(path: Path) -> None:
        key = _key(path)
        dirty.add(key)
        hash_memo.pop(key, None)

    def _staleness(stage: Stage, stage_ctx: StageContext) -> str | None:
        """Why the stage is NOT fresh (``None`` == fresh).

        Freshness = every produce exists with a matching manifest
        hash AND the stage's version/code tokens match the record AND no
        consumed/external path is dirty this run AND every recorded
        input still hashes to what the artifact was built from (this
        last check is what lets out-of-band changes — curation sidecars,
        reviewer workbooks, raw source caches — re-run their consumers,
        issue #212 items 1+2). The returned reason feeds the dry-run
        plan's ``would_run`` detail (issue #213 item 3)."""
        if not stage.manifest_tracked:
            return "not manifest-tracked (always runs)"
        produces = stage.produces(stage_ctx)
        if not produces:
            return "declares no produces"
        expected_versions = stage.code_versions()
        for p in produces:
            name = Path(p).name
            entry = manifest.entry_for(p)
            if entry is None:
                return f"{name} has no manifest record"
            recorded_versions = entry.get("versions") or {}
            for token, value in expected_versions.items():
                if recorded_versions.get(token) != value:
                    return f"{name}: {token} changed"
            current = _hash(p)
            if current is None:
                return f"{name} missing on disk"
            if current != entry.get("sha256"):
                return f"{name} differs from its manifest record"
            for rel_input, recorded in (entry.get("inputs") or {}).items():
                current_in = _hash(abs_for(rel_input))
                in_name = Path(rel_input).name
                if recorded == ABSENT:
                    if current_in is not None:
                        return f"{name}: input {in_name} appeared"
                elif current_in is None or current_in != recorded:
                    return f"{name}: input {in_name} changed"
        for c in (*stage.consumes(stage_ctx), *stage.externals(stage_ctx)):
            if _key(c) in dirty:
                return f"input {Path(c).name} rewritten this run"
        return None

    def _fresh(stage: Stage, stage_ctx: StageContext) -> bool:
        return _staleness(stage, stage_ctx) is None

    for idx, stage in enumerate(registry):
        if aborted:
            result.outcomes.append(StageOutcome(stage.name, "not_reached"))
            continue
        if not stage.enabled(options):
            result.outcomes.append(
                StageOutcome(stage.name, "skipped_disabled")
            )
            continue
        if stage.name in skip:
            # Probe freshness BEFORE honoring the skip: skipping a
            # stage that would not have been fresh taints its produces
            # — downstream stages otherwise run on stale output and the
            # manifest certifies the lineage (issue #212 item 7; the
            # sharpest case is `--skip swagger-backfill` after a
            # re-ingest, since the backfill MUTATES the elements
            # artifacts in place).
            if _fresh(stage, ctx):
                detail = "--skip (was fresh)"
            else:
                for p in stage.produces(ctx):
                    tainted.add(_key(p))
                tainted_stages.append(stage.name)
                detail = (
                    "--skip (stage was NOT fresh — its outputs are "
                    "tainted; downstream lineage will not be recorded)"
                )
                logger.warning(
                    "publish: --skip %s: the stage is NOT fresh; "
                    "downstream stages consume its stale output and "
                    "their manifest lineage is withheld this run",
                    stage.name,
                )
            result.outcomes.append(
                StageOutcome(stage.name, "skipped_flag", detail=detail)
            )
            continue
        if idx < from_index:
            result.outcomes.append(
                StageOutcome(
                    stage.name, "skipped_flag", detail="before --from"
                )
            )
            continue

        stage_ctx = ctx
        if stage.cross_state and partial:
            # Run cross-state stages against the full roster only when
            # every non-dirty input is manifest-fresh; otherwise skip
            # loudly — a partial refresh must never bake stale sibling
            # states into a combined artifact.
            stale = []
            for c in stage.consumes(full_ctx):
                if _key(c) in dirty:
                    continue
                entry = manifest.entry_for(c)
                current = _hash(c)
                if (
                    entry is None
                    or current is None
                    or current != entry.get("sha256")
                ):
                    stale.append(Path(c).name)
            if stale:
                result.outcomes.append(
                    StageOutcome(
                        stage.name,
                        "skipped_partial_states",
                        detail=(
                            "cross-state inputs not fresh for the full "
                            f"roster ({', '.join(sorted(stale)[:4])}"
                            f"{'…' if len(stale) > 4 else ''}) — run a "
                            "full publish"
                        ),
                    )
                )
                continue
            stage_ctx = full_ctx

        forced = from_stage is not None and idx == from_index
        staleness = None if forced else _staleness(stage, stage_ctx)
        if not forced and staleness is None:
            result.outcomes.append(
                StageOutcome(stage.name, "skipped_fresh")
            )
            continue

        if dry_run:
            # Carry the WHY (issue #213 item 3): forced by --from, or
            # the first staleness reason _staleness found.
            result.outcomes.append(
                StageOutcome(
                    stage.name,
                    "would_run",
                    detail=(
                        "forced by --from" if forced else (staleness or "")
                    ),
                )
            )
            for p in stage.produces(stage_ctx):
                _mark_dirty(p)
            continue

        if stage.cost == "llm":
            remaining = cost_cap - spent
            if remaining <= 0:
                result.failed = stage.name
                result.outcomes.append(
                    StageOutcome(
                        stage.name, "failed",
                        detail=f"cost cap ${cost_cap:.2f} exhausted",
                    )
                )
                aborted = True
                continue
            if not llm_gate_passed:
                if not confirm_fn(
                    f"stage '{stage.name}' calls the LLM (remaining cap "
                    f"${remaining:.2f}; warm prompt-cache reruns are $0). "
                    f"Continue?"
                ):
                    result.failed = stage.name
                    result.outcomes.append(
                        StageOutcome(
                            stage.name, "failed",
                            detail="declined at the LLM gate",
                        )
                    )
                    aborted = True
                    continue
                llm_gate_passed = True
            stage_ctx.remaining_cost_cap = remaining

        # A --skip'd non-fresh stage upstream taints this stage's inputs:
        # run anyway (the operator asked for the skip) but warn loudly
        # and withhold the manifest record, so the ledger never
        # certifies the deliberately-degraded lineage and the next
        # honest run re-runs this cone (issue #212 item 7).
        tainted_inputs = sorted(
            Path(c).name
            for c in (*stage.consumes(stage_ctx),
                      *stage.externals(stage_ctx))
            if _key(c) in tainted
        )
        if tainted_inputs:
            logger.warning(
                "publish: %s consumes output of a --skip'd NON-FRESH "
                "stage (%s) — running on possibly-stale input; its "
                "manifest lineage is withheld this run",
                stage.name,
                ", ".join(tainted_inputs[:4])
                + ("…" if len(tainted_inputs) > 4 else ""),
            )

        # Input hashes snapshot BEFORE the stage runs (what it consumed).
        consumed = {
            Path(c): _hash(c) for c in stage.consumes(stage_ctx)
        }
        externals = stage.externals(stage_ctx)
        logger.info("publish: %s — running", stage.name)
        started = time.monotonic()
        try:
            cost = float(stage.run(stage_ctx) or 0.0)
        except Exception as exc:  # noqa: BLE001 — stop-the-line
            duration = time.monotonic() - started
            logger.exception("publish: %s FAILED", stage.name)
            result.failed = stage.name
            result.outcomes.append(
                StageOutcome(
                    stage.name, "failed", duration_s=duration,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            aborted = True
            manifest.save()
            continue
        duration = time.monotonic() - started
        spent += cost

        produces = stage.produces(stage_ctx)
        # Registry↔run() drift gate (issue #212 item 7): every declared
        # produce must exist after the stage ran. `manifest.record()`
        # used to silently return on a missing artifact — the stage
        # read as complete and the drift was diagnosable only by
        # reading manifest JSON.
        missing = [Path(p) for p in produces if not Path(p).exists()]
        if missing:
            names = ", ".join(p.name for p in missing[:4]) + (
                "…" if len(missing) > 4 else ""
            )
            logger.error(
                "publish: %s declared produces it did not write: %s",
                stage.name, names,
            )
            result.failed = stage.name
            result.outcomes.append(
                StageOutcome(
                    stage.name, "failed", duration_s=duration,
                    detail=(
                        f"stage completed but never wrote declared "
                        f"artifact(s): {names} — publish stage registry "
                        f"drift"
                    ),
                )
            )
            aborted = True
            manifest.save()
            continue

        produced_set = {Path(p).resolve() for p in produces}
        if tainted_inputs:
            # Propagate the taint; downstream of this stage stays
            # unrecorded too.
            tainted.update(_key(p) for p in produces)
            tainted_stages.append(stage.name)
        elif stage.manifest_tracked:
            # Record exactly the stage's own tokens — a blanket
            # scoring_plan_version stamp on non-score artifacts (spines,
            # elements) would make reader-side verify_fresh refuse them
            # after a plan bump even though they don't depend on the
            # plan (issue #212 item 3 interaction).
            versions = dict(stage.code_versions())
            inputs: dict[Path, str | None] = {
                p: h
                for p, h in consumed.items()
                # Self-referencing inputs (mutating stages) would compare
                # an artifact against its pre-mutation self — drop them.
                if p.resolve() not in produced_set
            }
            # Externals snapshot AFTER the run (adapters may top up
            # their own caches mid-run — a pre-run hash would read as
            # drift forever). Missing externals record as ABSENT so
            # their later appearance re-runs the consumer.
            for e in externals:
                if Path(e).resolve() in produced_set:
                    continue
                hash_memo.pop(_key(e), None)
                inputs[Path(e)] = _hash(e) or ABSENT
            for p in produces:
                manifest.record(
                    p,
                    producer=stage.name,
                    versions=versions,
                    inputs=inputs,
                    stage_duration_s=duration,
                )
            manifest.save()
        for p in produces:
            _mark_dirty(p)
        logger.info(
            "publish: %s — complete in %.1fs%s",
            stage.name, duration, f" (${cost:.2f})" if cost else "",
        )
        if stage.name == "report-reviewer-comparison":
            logger.info(
                "publish: docs/reviewer-comparison.md is COMMITTED — "
                "remember to commit the regenerated doc"
            )
        result.outcomes.append(
            StageOutcome(
                stage.name, "complete", duration_s=duration, cost_usd=cost,
                detail=(
                    "ran on tainted input (--skip'd non-fresh stage "
                    "upstream) — lineage not recorded"
                    if tainted_inputs else ""
                ),
            )
        )

    result.total_llm_usd = spent
    if not dry_run:
        manifest.finish_run(
            spent,
            skipped_stages=tuple(skip),
            tainted_stages=tuple(dict.fromkeys(tainted_stages)),
            failed_at=result.failed,
        )
        manifest.save()
    return result
