"""Tests for the `mc publish` orchestrator (sequence 4 / R1)."""

from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

import click
import pytest

from src.publish import orchestrator
from src.publish.manifest import PublishManifest
from src.publish.stages import (
    STAGE_NAMES,
    STAGES,
    PublishOptions,
    Stage,
    StageContext,
)
from src.states import SUPPORTED_STATES


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self):
        assert not isinstance(orchestrator.run, click.Command)
        assert inspect.isfunction(orchestrator.run)

    def test_cli_command_calls_orchestrator_run(self, monkeypatch):
        from click.testing import CliRunner

        import src.publish as publish_pkg
        from src.cli import cli

        calls: dict = {}

        def fake_run(**kwargs):
            calls.update(kwargs)
            return orchestrator.PublishResult(dry_run=True)

        monkeypatch.setattr(publish_pkg, "run", fake_run)
        result = CliRunner().invoke(
            cli, ["publish", "--dry-run", "--state", "AZ", "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert calls["states"] == ("AZ",)
        assert calls["dry_run"] is True
        assert calls["assume_yes"] is True

    def test_unknown_from_stage_is_usage_error(self):
        from click.testing import CliRunner

        from src.cli import cli

        result = CliRunner().invoke(
            cli, ["publish", "--dry-run", "--from", "nope"]
        )
        assert result.exit_code != 0
        # Since issue #213 item 3, --from is click.Choice(STAGE_NAMES):
        # Click itself rejects the bad name (and lists valid stages)
        # before the orchestrator's own guard runs.
        assert "Invalid value for '--from'" in result.output
        assert "report-analyst" in result.output  # the choices are listed

    def test_state_is_repeatable(self, monkeypatch):
        """Issue #213 item 3: `publish --state AZ --state WI` passes the
        deduped multi-state tuple through; 'all' anywhere wins."""
        from click.testing import CliRunner

        import src.publish as publish_pkg
        from src.cli import cli
        from src.publish.orchestrator import PublishResult

        calls: dict = {}

        def _fake_run(**kwargs):
            calls.update(kwargs)
            return PublishResult(dry_run=True)

        monkeypatch.setattr(publish_pkg, "run", _fake_run)

        result = CliRunner().invoke(
            cli,
            ["publish", "--dry-run", "--state", "az", "--state", "WI",
             "--state", "az"],
        )
        assert result.exit_code == 0, result.output
        assert calls["states"] == ("AZ", "WI")

        calls.clear()
        result = CliRunner().invoke(
            cli, ["publish", "--dry-run", "--state", "AZ", "--state", "all"]
        )
        assert result.exit_code == 0, result.output
        assert calls["states"] == SUPPORTED_STATES


class TestRegistryIntegrity:
    """The ordering constraints, structurally enforced."""

    def _full_ctx(self, tmp_path):
        return StageContext(
            options=PublishOptions(
                states=SUPPORTED_STATES,
                refresh_spine=True,
                with_gap_llm=True,
                with_spine_workbooks=True,
            ),
            out_dir=tmp_path / "out",
            spine_dir=tmp_path / "spine",
        )

    def test_names_unique(self):
        assert len(STAGE_NAMES) == len(set(STAGE_NAMES))

    def test_every_consumed_path_is_produced_earlier(self, tmp_path):
        ctx = self._full_ctx(tmp_path)
        produced: set[str] = set()
        for stage in STAGES:
            for c in stage.consumes(ctx):
                assert str(c) in produced, (
                    f"stage '{stage.name}' consumes {Path(c).name} which "
                    f"no earlier stage produces — ordering constraint "
                    f"violated or path drift"
                )
            produced.update(str(p) for p in stage.produces(ctx))

    def test_constraint_a_spine_aggregate_consumes_source_sidecars(
        self, tmp_path
    ):
        ctx = self._full_ctx(tmp_path)
        spine_stage = next(s for s in STAGES if s.name == "aggregate-spine")
        consumed = {Path(c).name for c in spine_stage.consumes(ctx)}
        assert "az_scores_source.json" in consumed

    def test_constraint_b_digest_consumes_gap_layer(self, tmp_path):
        ctx = self._full_ctx(tmp_path)
        digest = next(
            s for s in STAGES if s.name == "report-review-digest"
        )
        consumed = {Path(c).name for c in digest.consumes(ctx)}
        assert "az_elements_gap.json" in consumed
        assert "az_scores_gap.json" in consumed

    def test_constraint_c_gap_surface_after_backfill(self):
        assert STAGE_NAMES.index("swagger-backfill") < STAGE_NAMES.index(
            "gap-surface"
        )

    def test_constraint_d_recs_and_queue_before_analyst(self):
        analyst = STAGE_NAMES.index("report-analyst")
        assert STAGE_NAMES.index("report-recommendations") < analyst
        assert STAGE_NAMES.index("report-review-queue") < analyst

    def test_llm_stages_flagged_and_optional_stages_default_off(self):
        by_name = {s.name: s for s in STAGES}
        assert by_name["extract-source"].cost == "llm"
        assert by_name["extract-spine"].cost == "llm"
        assert by_name["gap-extract"].cost == "llm"
        defaults = PublishOptions()
        assert not by_name["spine-fetch"].enabled(defaults)
        assert not by_name["gap-extract"].enabled(defaults)
        assert by_name["spine-fetch"].manifest_tracked is False


class TestLiteProfile:
    """The POC-Lite score-only stage subset (docs/pipeline-lite.md)."""

    # The lite chain: score production + the deliverable workbooks. The
    # free deterministic gap layer stays in (the analyst Documentation
    # Gaps sheet reads the gap sidecars — cutting them would let that
    # sheet drift silently after a lite re-ingest). The API-model-lens
    # scoring pass and every analysis/comparison report are cut.
    LITE_STAGES = {
        "spine-build",
        "ingest",
        "swagger-backfill",
        "gap-surface",
        "extract-source",
        "aggregate-source",
        "aggregate-gap",
        "report-recommendations",
        "report-review-queue",
        "report-analyst",
    }

    def _lite_ctx(self, tmp_path):
        return StageContext(
            options=PublishOptions(states=SUPPORTED_STATES, lite=True),
            out_dir=tmp_path / "out",
            spine_dir=tmp_path / "spine",
        )

    def test_lite_stage_membership(self):
        opts = PublishOptions(lite=True)
        enabled = {s.name for s in STAGES if s.enabled(opts)}
        assert enabled == self.LITE_STAGES

    def test_lite_with_refresh_spine_adds_the_fetch(self):
        opts = PublishOptions(lite=True, refresh_spine=True)
        enabled = {s.name for s in STAGES if s.enabled(opts)}
        assert enabled == self.LITE_STAGES | {"spine-fetch"}

    def test_lite_every_consumed_path_is_produced_earlier(self, tmp_path):
        """The structural ordering proof, re-run on the lite subset."""
        ctx = self._lite_ctx(tmp_path)
        produced: set[str] = set()
        for stage in STAGES:
            if not stage.enabled(ctx.options):
                continue
            for c in stage.consumes(ctx):
                assert str(c) in produced, (
                    f"lite stage '{stage.name}' consumes {Path(c).name} "
                    f"which no earlier lite-enabled stage produces"
                )
            produced.update(str(p) for p in stage.produces(ctx))

    def test_lite_review_queue_is_source_only(self, tmp_path):
        ctx = self._lite_ctx(tmp_path)
        queue = next(s for s in STAGES if s.name == "report-review-queue")
        produced = {Path(p).name for p in queue.produces(ctx)}
        assert produced == {"review_queue_source.json",
                            "review_queue_source.md"}
        consumed = {Path(c).name for c in queue.consumes(ctx)}
        assert "az_scores_spine.json" not in consumed
        assert "az_scores_source.json" in consumed

    def test_lite_analyst_skips_the_spine_queue(self, tmp_path):
        ctx = self._lite_ctx(tmp_path)
        analyst = next(s for s in STAGES if s.name == "report-analyst")
        consumed = {Path(c).name for c in analyst.consumes(ctx)}
        assert "review_queue_source.json" in consumed
        assert "review_queue_spine.json" not in consumed

    def test_lite_review_queue_body_runs_source_lens_only(
        self, monkeypatch, tmp_path
    ):
        import src.report.review_queue as review_queue_mod
        from src.publish.stages import _run_report_review_queue

        calls: list[str] = []
        monkeypatch.setattr(
            review_queue_mod, "run",
            lambda lens, states: calls.append(lens),
        )
        _run_report_review_queue(self._lite_ctx(tmp_path))
        assert calls == ["source"]


# ---------------------------------------------------------------------------
# Orchestrator behavior with injected fake stages
# ---------------------------------------------------------------------------


def _make_stages(tmp: Path, log: list[str], *, fail: str | None = None,
                 llm: set[str] = frozenset()) -> tuple[Stage, ...]:
    """Three-stage chain a → b → c over sentinel files in ``tmp``."""

    a_out = tmp / "a.json"
    b_out = tmp / "b.json"
    c_out = tmp / "c.json"

    def _mk(name: str, out: Path, consumes: tuple[Path, ...],
            cross: bool = False) -> Stage:
        def _run(ctx: StageContext) -> float:
            log.append(name)
            if fail == name:
                raise RuntimeError(f"{name} exploded")
            payload = "|".join(
                p.read_text(encoding="utf-8") for p in consumes if p.exists()
            )
            out.write_text(f"{name}({payload})", encoding="utf-8")
            return 1.5 if name in llm else 0.0

        return Stage(
            name=name,
            description=name,
            cost="llm" if name in llm else "free",
            run=_run,
            produces=lambda ctx, _o=out: (_o,),
            consumes=lambda ctx, _c=consumes: _c,
            cross_state=cross,
        )

    return (
        _mk("stage-a", a_out, ()),
        _mk("stage-b", b_out, (a_out,)),
        _mk("stage-c", c_out, (b_out,), cross=True),
    )


def _run(tmp: Path, stages, **kwargs):
    return orchestrator.run(
        stages=stages,
        manifest_path=tmp / "manifest.json",
        assume_yes=kwargs.pop("assume_yes", True),
        **kwargs,
    )


class TestOrchestrator:
    def test_full_run_completes_and_records_manifest(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        result = _run(tmp_path, stages)
        assert [o.status for o in result.outcomes] == ["complete"] * 3
        assert log == ["stage-a", "stage-b", "stage-c"]
        manifest = PublishManifest.load(tmp_path / "manifest.json")
        entry = manifest.entry_for(tmp_path / "b.json")
        assert entry["producer"] == "stage-b"
        [(key, _sha)] = entry["inputs"].items()
        assert key.endswith("a.json")
        assert manifest.data["publish_run"]["finished_at"]

    def test_second_run_skips_everything_fresh(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        _run(tmp_path, stages)
        log.clear()
        result = _run(tmp_path, stages)
        assert [o.status for o in result.outcomes] == ["skipped_fresh"] * 3
        assert log == []

    def test_changed_input_reruns_only_downstream(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        _run(tmp_path, stages)
        # Hand-edit stage-a's output → a itself re-runs (hash mismatch)
        # and the change propagates through b and c.
        (tmp_path / "a.json").write_text("tampered", encoding="utf-8")
        log.clear()
        result = _run(tmp_path, stages)
        assert log == ["stage-a", "stage-b", "stage-c"]
        assert [o.status for o in result.outcomes] == ["complete"] * 3

    def test_from_stage_forces_and_propagates(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        _run(tmp_path, stages)
        log.clear()
        result = _run(tmp_path, stages, from_stage="stage-b")
        # a skipped (before --from); b forced; c re-runs (dirty input).
        assert log == ["stage-b", "stage-c"]
        assert result.outcomes[0].status == "skipped_flag"

    def test_skip_flag(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        result = _run(tmp_path, stages, skip=("stage-b",))
        assert log == ["stage-a", "stage-c"]
        assert result.outcomes[1].status == "skipped_flag"

    def test_unknown_stage_name_raises(self, tmp_path):
        stages = _make_stages(tmp_path, [])
        with pytest.raises(ValueError, match="unknown stage"):
            _run(tmp_path, stages, from_stage="nope")
        with pytest.raises(ValueError, match="unknown stage"):
            _run(tmp_path, stages, skip=("nope",))

    def test_from_skip_conflict_raises(self, tmp_path):
        """Issue #211 item 5c: `--from X --skip X` used to be a green,
        successful-looking run that executed nothing (the skip branch
        fired before `forced` was computed)."""
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        with pytest.raises(ValueError, match="conflicts with --skip"):
            _run(tmp_path, stages, from_stage="stage-b", skip=("stage-b",))
        assert log == []

    def test_from_disabled_stage_raises_naming_flag(self, tmp_path):
        """Issue #211 item 5c, real registry: `--from gap-extract` without
        `--with-gap-llm` (and `--from spine-fetch` without
        `--refresh-spine`) silently no-op'd. The guard now names the
        enabling flag (`Stage.flag`)."""
        with pytest.raises(ValueError, match=r"--with-gap-llm"):
            _run(
                tmp_path, STAGES, from_stage="gap-extract",
                dry_run=True, out_dir_override=tmp_path,
                spine_dir_override=tmp_path,
            )
        with pytest.raises(ValueError, match=r"--refresh-spine"):
            _run(
                tmp_path, STAGES, from_stage="spine-fetch",
                dry_run=True, out_dir_override=tmp_path,
                spine_dir_override=tmp_path,
            )
        # With the flag supplied the same --from target is accepted.
        result = _run(
            tmp_path, STAGES, from_stage="gap-extract",
            with_gap_llm=True, dry_run=True, out_dir_override=tmp_path,
            spine_dir_override=tmp_path,
        )
        statuses = {o.name: o.status for o in result.outcomes}
        assert statuses["gap-extract"] == "would_run"

    def test_from_disabled_stage_generic_message_without_flag(self, tmp_path):
        """A disabled stage with no declared flag still refuses --from,
        with the generic hint."""
        log: list[str] = []
        a, b, c = _make_stages(tmp_path, log)
        b_disabled = replace(b, enabled=lambda opts: False)
        with pytest.raises(ValueError, match="disabled by the current options"):
            _run(tmp_path, (a, b_disabled, c), from_stage="stage-b")
        assert log == []

    def test_dry_run_executes_nothing_writes_nothing(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        result = _run(tmp_path, stages, dry_run=True)
        assert log == []
        assert [o.status for o in result.outcomes] == ["would_run"] * 3
        assert not (tmp_path / "manifest.json").exists()
        assert not (tmp_path / "a.json").exists()

    def test_dry_run_would_run_carries_a_reason(self, tmp_path):
        """Issue #213 item 3: the dry-run plan says WHY each stage would
        run — no manifest record on a cold plan, "forced by --from" on
        the forced stage."""
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        result = _run(tmp_path, stages, dry_run=True)
        for outcome in result.outcomes:
            assert outcome.status == "would_run"
            assert outcome.detail  # never blank
        assert "no manifest record" in result.outcomes[0].detail

        # A warm manifest + --from: the forced stage says so, and its
        # downstream cone re-runs because its inputs are rewritten.
        _run(tmp_path, stages)  # real run populates the manifest
        result = _run(tmp_path, stages, dry_run=True, from_stage="stage-b")
        by = {o.name: o for o in result.outcomes}
        assert by["stage-b"].detail == "forced by --from"
        assert by["stage-c"].status == "would_run"
        assert "rewritten this run" in by["stage-c"].detail

    def test_failure_stops_the_line(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log, fail="stage-b")
        result = _run(tmp_path, stages)
        assert result.failed == "stage-b"
        assert [o.status for o in result.outcomes] == [
            "complete", "failed", "not_reached"
        ]
        assert "stage-b exploded" in result.outcomes[1].detail
        assert "mc publish --from stage-b" in result.render_table()
        # Completed work is still in the manifest (resume support).
        manifest = PublishManifest.load(tmp_path / "manifest.json")
        assert manifest.entry_for(tmp_path / "a.json") is not None
        # Issue #213 item 3: the run ledger records where it failed, so
        # a finished-but-failed run is distinguishable from a clean one.
        assert manifest.data["publish_run"]["failed_at"] == "stage-b"

    def test_partial_states_skips_unfresh_cross_state_stage(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        result = _run(tmp_path, stages, states=("AZ",))
        # stage-c is cross_state; its input b.json IS dirty this run
        # (stage-b just ran) so the dirty carve-out lets it run.
        assert result.outcomes[2].status == "complete"
        # Now make c's input stale in manifest terms: fresh manifest but
        # a cross-state stage whose consumed path has NO entry.
        (tmp_path / "manifest.json").unlink()
        (tmp_path / "b.json").write_text("stale", encoding="utf-8")
        log.clear()
        stages2 = _make_stages(tmp_path, log)

        # Only run stage-c (skip a+b) with partial states: b.json is not
        # dirty and has no manifest entry → skipped_partial_states.
        result2 = _run(
            tmp_path, stages2, states=("AZ",),
            skip=("stage-a", "stage-b"),
        )
        assert result2.outcomes[2].status == "skipped_partial_states"

    def test_llm_gate_confirm_called_once(self, tmp_path):
        log: list[str] = []
        confirms: list[str] = []

        def fake_confirm(message: str) -> bool:
            confirms.append(message)
            return True

        stages = _make_stages(tmp_path, log, llm={"stage-a", "stage-b"})
        result = _run(
            tmp_path, stages, assume_yes=False, confirm=fake_confirm
        )
        assert len(confirms) == 1  # one gate, not one per LLM stage
        assert result.total_llm_usd == 3.0
        assert result.outcomes[0].cost_usd == 1.5

    def test_llm_gate_declined_aborts(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log, llm={"stage-b"})
        result = _run(
            tmp_path, stages, assume_yes=False, confirm=lambda m: False
        )
        assert result.failed == "stage-b"
        assert log == ["stage-a"]

    def test_cost_cap_exhaustion_aborts(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log, llm={"stage-a", "stage-b"})
        result = _run(tmp_path, stages, cost_cap=1.0)
        # stage-a runs (cap not yet exhausted), spends 1.5 > cap → b aborts.
        assert result.failed == "stage-b"
        assert "cost cap" in result.outcomes[1].detail

    def test_reports_only_maps_to_first_report_stage(self, tmp_path):
        report_stage = Stage(
            name="report-x",
            description="x",
            cost="free",
            run=lambda ctx: 0.0,
            produces=lambda ctx: (),
            consumes=lambda ctx: (),
        )
        log: list[str] = []
        stages = (*_make_stages(tmp_path, log), report_stage)
        result = _run(tmp_path, stages, reports_only=True)
        assert [o.status for o in result.outcomes[:3]] == [
            "skipped_flag"] * 3
        assert result.outcomes[3].status == "complete"


class TestVersionAwareFreshness:
    """Issue #212 item 1 — a version/code-token change must invalidate
    manifest freshness. A SCORING_PLAN_VERSION bump used to leave a warm
    `mc publish` reporting every stage skipped_fresh (artifact bytes
    unchanged), silently refusing the refresh the playbook requires."""

    def test_code_version_token_change_invalidates(self, tmp_path):
        log: list[str] = []
        token = {"v": "1"}
        stages = tuple(
            replace(s, code_versions=lambda: dict(token))
            for s in _make_stages(tmp_path, log)
        )
        _run(tmp_path, stages)
        log.clear()
        result = _run(tmp_path, stages)
        assert [o.status for o in result.outcomes] == ["skipped_fresh"] * 3
        token["v"] = "2"  # the version bump
        result = _run(tmp_path, stages)
        assert log == ["stage-a", "stage-b", "stage-c"]
        assert [o.status for o in result.outcomes] == ["complete"] * 3
        # And the new tokens are recorded — the next run is warm again.
        log.clear()
        result = _run(tmp_path, stages)
        assert [o.status for o in result.outcomes] == ["skipped_fresh"] * 3

    def test_registry_version_tokens(self, monkeypatch):
        import src.score.aggregate as agg
        from src.score.extract import PROMPT_VERSION

        by = {s.name: s for s in STAGES}
        agg_tokens = by["aggregate-source"].code_versions()
        assert agg_tokens["scoring_plan_version"] == agg.SCORING_PLAN_VERSION
        assert "code_fingerprint" in agg_tokens
        # Extract: prompt_version + fingerprint, NO plan version — a
        # plan bump requires re-aggregation, not re-extraction.
        ext_tokens = by["extract-source"].code_versions()
        assert ext_tokens["prompt_version"] == PROMPT_VERSION
        assert "scoring_plan_version" not in ext_tokens
        # Report stages carry the plan token; elements-only reports
        # (coverage/divergence) deliberately don't.
        assert "scoring_plan_version" in by["report-analyst"].code_versions()
        assert (
            "scoring_plan_version"
            not in by["report-coverage"].code_versions()
        )
        # The token is read LIVE (lazy import), so a bump is seen
        # without any registry rebuild.
        monkeypatch.setattr(agg, "SCORING_PLAN_VERSION", "999-drill")
        assert (
            by["aggregate-source"].code_versions()["scoring_plan_version"]
            == "999-drill"
        )

    def test_recorded_versions_are_stage_scoped(self, tmp_path):
        """The manifest records exactly the stage's own tokens — a
        blanket scoring_plan_version stamp on non-score artifacts would
        make reader-side verify_fresh refuse spines/elements after a
        plan bump even though they don't depend on the plan."""
        log: list[str] = []
        stages = _make_stages(tmp_path, log)  # code_versions default {}
        _run(tmp_path, stages)
        manifest = PublishManifest.load(tmp_path / "manifest.json")
        entry = manifest.entry_for(tmp_path / "a.json")
        assert entry["versions"] == {}

    def test_mutating_family_shares_tokens(self):
        """swagger-backfill is the LAST producer of the elements
        artifacts ingest writes — divergent code_versions between the
        two would read as permanently stale (see Stage.code_versions)."""
        by = {s.name: s for s in STAGES}
        assert (
            by["ingest"].code_versions()
            == by["swagger-backfill"].code_versions()
        )


class TestExternalInputs:
    """Issue #212 item 2 — out-of-band inputs (curation sidecars,
    reviewer workbooks, raw source caches) must re-run their consumers
    when they change, appear, or disappear."""

    def test_external_lifecycle_absent_appear_change_delete(self, tmp_path):
        log: list[str] = []
        ext = tmp_path / "curation.json"
        a, b, c = _make_stages(tmp_path, log)
        stages = (a, replace(b, externals=lambda ctx: (ext,)), c)

        _run(tmp_path, stages)  # ext absent → recorded as ABSENT
        log.clear()
        result = _run(tmp_path, stages)
        assert [o.status for o in result.outcomes] == ["skipped_fresh"] * 3

        ext.write_text("analyst edits", encoding="utf-8")  # APPEARS
        result = _run(tmp_path, stages)
        assert log == ["stage-b", "stage-c"]
        log.clear()
        result = _run(tmp_path, stages)
        assert [o.status for o in result.outcomes] == ["skipped_fresh"] * 3

        ext.write_text("newer edits", encoding="utf-8")  # CHANGES
        _run(tmp_path, stages)
        assert log == ["stage-b", "stage-c"]
        log.clear()

        ext.unlink()  # DISAPPEARS
        _run(tmp_path, stages)
        assert log == ["stage-b", "stage-c"]

    def test_untracked_producer_dirties_external_consumer(self, tmp_path):
        """The --refresh-spine shape: a manifest-untracked fetch stage
        produces a raw cache the next stage declares as external. The
        fetch used to declare produces=() — the refreshed swagger never
        dirtied spine-build and the whole cone skipped fresh."""
        log: list[str] = []
        raw = tmp_path / "raw_swagger"
        built = tmp_path / "spine.json"

        def _fetch(ctx: StageContext) -> float:
            log.append("fetch")
            raw.mkdir(exist_ok=True)
            (raw / "swagger.json").write_text("fetched", encoding="utf-8")
            return 0.0

        def _build(ctx: StageContext) -> float:
            log.append("build")
            built.write_text("built", encoding="utf-8")
            return 0.0

        fetch = Stage(
            name="fetch", description="", cost="free", run=_fetch,
            produces=lambda ctx: (raw,), consumes=lambda ctx: (),
            enabled=lambda opts: opts.refresh_spine,
            manifest_tracked=False,
        )
        build = Stage(
            name="build", description="", cost="free", run=_build,
            produces=lambda ctx: (built,), consumes=lambda ctx: (),
            externals=lambda ctx: (raw,),
        )
        stages = (fetch, build)

        _run(tmp_path, stages)  # no refresh: raw absent → ABSENT
        assert log == ["build"]
        log.clear()
        result = _run(tmp_path, stages, refresh_spine=True)
        # fetch always runs when enabled (untracked); its produce
        # dirties build's external in the same run.
        assert log == ["fetch", "build"]
        log.clear()
        # Warm again without refresh: raw exists and matches its record.
        result = _run(tmp_path, stages)
        assert [o.status for o in result.outcomes] == [
            "skipped_disabled", "skipped_fresh"
        ]

    def test_registry_externals_declared(self, tmp_path):
        ctx = StageContext(
            options=PublishOptions(
                states=SUPPORTED_STATES,
                refresh_spine=True,
                with_spine_workbooks=True,
            ),
            out_dir=tmp_path / "out",
            spine_dir=tmp_path / "spine",
        )
        by = {s.name: s for s in STAGES}
        # --refresh-spine wiring: fetch produces exactly what build
        # declares external.
        assert set(by["spine-fetch"].produces(ctx)) == set(
            by["spine-build"].externals(ctx)
        )
        assert by["spine-build"].externals(ctx)
        ingest_ext = {p.as_posix() for p in by["ingest"].externals(ctx)}
        assert any(p.endswith("data/raw/tx") for p in ingest_ext)
        assert any(p.endswith("docs/bootstrap/in") for p in ingest_ext)
        # Curation round-trip: report-analyst sees the sidecars.
        analyst_ext = {p.name for p in by["report-analyst"].externals(ctx)}
        assert analyst_ext == {
            "az.json", "wi.json", "mn.json", "tx.json", "in.json"
        }
        # Issue #249: fact corrections overlay at aggregate time, so a
        # `review correct-fact` write (or the first sidecar appearing)
        # must re-run the scoring cone — both lenses.
        for stage in ("aggregate-source", "aggregate-spine"):
            agg_ext = {p.name for p in by[stage].externals(ctx)}
            assert agg_ext == analyst_ext, stage
        # Reviewer-workbook basis: the digest sees all five files.
        digest_ext = by["report-review-digest"].externals(ctx)
        assert len(digest_ext) == 5
        assert all(p.suffix == ".xlsx" for p in digest_ext)


class TestSkipTaint:
    """Issue #212 item 7 — `--skip` of a NON-fresh stage must not get
    the downstream lineage blessed by the manifest."""

    def test_skip_nonfresh_taints_downstream(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        _run(tmp_path, stages)
        (tmp_path / "b.json").write_text("tampered", encoding="utf-8")
        log.clear()
        result = _run(tmp_path, stages, skip=("stage-b",))
        # c re-ran (its recorded input drifted) — on tainted input.
        assert log == ["stage-c"]
        by = {o.name: o for o in result.outcomes}
        assert by["stage-b"].status == "skipped_flag"
        assert "tainted" in by["stage-b"].detail
        assert by["stage-c"].status == "complete"
        assert "not recorded" in by["stage-c"].detail
        manifest = PublishManifest.load(tmp_path / "manifest.json")
        assert manifest.data["publish_run"]["tainted_stages"] == [
            "stage-b", "stage-c"
        ]
        assert manifest.data["publish_run"]["skipped_stages"] == ["stage-b"]
        # c's produce was NOT certified: its entry still carries the
        # pre-skip lineage, so the next honest run re-runs the cone.
        from src.publish.manifest import sha256_path

        entry = manifest.entry_for(tmp_path / "c.json")
        assert entry["sha256"] != sha256_path(tmp_path / "c.json")
        log.clear()
        result = _run(tmp_path, stages)
        assert log == ["stage-b", "stage-c"]
        manifest = PublishManifest.load(tmp_path / "manifest.json")
        entry = manifest.entry_for(tmp_path / "c.json")
        assert entry["sha256"] == sha256_path(tmp_path / "c.json")

    def test_skip_fresh_stage_is_benign(self, tmp_path):
        log: list[str] = []
        stages = _make_stages(tmp_path, log)
        _run(tmp_path, stages)
        log.clear()
        result = _run(tmp_path, stages, skip=("stage-b",))
        assert log == []
        by = {o.name: o for o in result.outcomes}
        assert by["stage-b"].detail == "--skip (was fresh)"
        assert by["stage-c"].status == "skipped_fresh"
        manifest = PublishManifest.load(tmp_path / "manifest.json")
        assert "tainted_stages" not in manifest.data["publish_run"]


class TestProduceExistenceGate:
    """Issue #212 item 7 — a stage that never writes a declared produce
    fails loudly instead of recording `complete`."""

    def test_missing_declared_produce_fails_stage(self, tmp_path):
        log: list[str] = []
        a, b, c = _make_stages(tmp_path, log)
        ghost = tmp_path / "ghost.json"
        bad = Stage(
            name="stage-bad", description="", cost="free",
            run=lambda ctx: 0.0,
            produces=lambda ctx: (ghost,),
            consumes=lambda ctx: (),
        )
        result = _run(tmp_path, (a, bad, b, c))
        assert result.failed == "stage-bad"
        by = {o.name: o for o in result.outcomes}
        assert by["stage-bad"].status == "failed"
        assert "registry drift" in by["stage-bad"].detail
        assert by["stage-b"].status == "not_reached"


class TestPerLensPhaseA:
    """Issue #212 item 7 — the extract stages declare per-lens FILE
    keys, not the shared phase_a directory (whose manifest entry was
    overwritten by whichever lens ran last)."""

    def _ctx(self, tmp_path):
        return StageContext(
            options=PublishOptions(states=SUPPORTED_STATES),
            out_dir=tmp_path / "out",
            spine_dir=tmp_path / "spine",
        )

    def test_per_lens_produce_files(self, tmp_path):
        ctx = self._ctx(tmp_path)
        by = {s.name: s for s in STAGES}
        src = {p.name for p in by["extract-source"].produces(ctx)}
        sp = {p.name for p in by["extract-spine"].produces(ctx)}
        assert "AZ_source_definition_present.jsonl" in src
        assert all("_source_" in n for n in src)
        assert all("_spine_" in n for n in sp)
        assert not src & sp
        # 5 states × the per-lens roster; a roster change re-derives
        # both this declaration and what run_all writes.
        from src.score.runner import phase_b_facts_for_lens

        assert len(src) == 5 * len(phase_b_facts_for_lens("source"))
        assert len(sp) == 5 * len(phase_b_facts_for_lens("spine"))

    def test_aggregate_consumes_own_lens_only(self, tmp_path):
        ctx = self._ctx(tmp_path)
        by = {s.name: s for s in STAGES}
        agg_src = {p.name for p in by["aggregate-source"].consumes(ctx)}
        assert "AZ_source_definition_present.jsonl" in agg_src
        assert not any("_spine_" in n for n in agg_src)
        agg_sp = {p.name for p in by["aggregate-spine"].consumes(ctx)}
        assert not any("_source_" in n and n.endswith(".jsonl")
                       for n in agg_sp)

    def test_spine_recs_declared_under_flag(self, tmp_path):
        by = {s.name: s for s in STAGES}
        flagged = StageContext(
            options=PublishOptions(
                states=SUPPORTED_STATES, with_spine_workbooks=True
            ),
            out_dir=tmp_path / "out",
            spine_dir=tmp_path / "spine",
        )
        produced = {
            p.name for p in by["report-recommendations"].produces(flagged)
        }
        assert "az_recommendations_spine.json" in produced
        consumed = {p.name for p in by["report-analyst"].consumes(flagged)}
        assert "az_recommendations_spine.json" in consumed
        # Default options: source lens only.
        default = self._ctx(tmp_path)
        produced = {
            p.name for p in by["report-recommendations"].produces(default)
        }
        assert "az_recommendations_spine.json" not in produced
        assert "az_recommendations_source.json" in produced


class TestOrchestratorLite:
    """--lite behavior at the orchestrator boundary."""

    def test_lite_conflicts_with_gap_llm(self, tmp_path):
        with pytest.raises(ValueError, match="with-gap-llm"):
            _run(tmp_path, _make_stages(tmp_path, []),
                 lite=True, with_gap_llm=True)

    def test_lite_conflicts_with_spine_workbooks(self, tmp_path):
        with pytest.raises(ValueError, match="with-spine-workbooks"):
            _run(tmp_path, _make_stages(tmp_path, []),
                 lite=True, with_spine_workbooks=True)

    def test_lite_from_a_lite_disabled_stage_raises(self, tmp_path):
        log: list[str] = []
        full_only = Stage(
            name="stage-full-only",
            description="cut by lite",
            cost="free",
            run=lambda ctx: 0.0,
            produces=lambda ctx: (),
            consumes=lambda ctx: (),
            enabled=lambda opts: not opts.lite,
        )
        stages = (*_make_stages(tmp_path, log), full_only)
        with pytest.raises(ValueError, match="--lite profile"):
            _run(tmp_path, stages, lite=True,
                 from_stage="stage-full-only")
        # Without --lite the same --from is fine.
        result = _run(tmp_path, stages, from_stage="stage-full-only")
        assert result.failed is None

    def test_lite_dry_run_plan_on_the_real_registry(self, tmp_path):
        result = orchestrator.run(
            dry_run=True, lite=True, assume_yes=True,
            manifest_path=tmp_path / "manifest.json",
        )
        by = {o.name: o.status for o in result.outcomes}
        assert by["extract-spine"] == "skipped_disabled"
        assert by["aggregate-spine"] == "skipped_disabled"
        assert by["report-coverage"] == "skipped_disabled"
        assert by["report-review-digest"] == "skipped_disabled"
        assert by["report-reviewer-comparison"] == "skipped_disabled"
        assert by["report-divergence"] == "skipped_disabled"
        assert by["report-scoring"] == "skipped_disabled"
        assert by["extract-source"] == "would_run"
        assert by["report-analyst"] == "would_run"

    def test_lite_reports_only_targets_first_lite_report_stage(
        self, tmp_path
    ):
        result = orchestrator.run(
            dry_run=True, lite=True, reports_only=True, assume_yes=True,
            manifest_path=tmp_path / "manifest.json",
        )
        by = {o.name: o.status for o in result.outcomes}
        # report-coverage is lite-disabled, so --reports-only resolves
        # to the first lite-ENABLED report stage instead.
        assert by["report-coverage"] == "skipped_disabled"
        assert by["report-recommendations"] == "would_run"
        assert by["aggregate-source"] == "skipped_flag"

    def test_cli_lite_flag_passes_through(self, monkeypatch):
        from click.testing import CliRunner

        import src.publish as publish_pkg
        from src.cli import cli

        calls: dict = {}

        def fake_run(**kwargs):
            calls.update(kwargs)
            return orchestrator.PublishResult(dry_run=True)

        monkeypatch.setattr(publish_pkg, "run", fake_run)
        result = CliRunner().invoke(cli, ["publish", "--dry-run", "--lite"])
        assert result.exit_code == 0, result.output
        assert calls["lite"] is True

    def test_cli_lite_conflict_is_usage_error(self):
        from click.testing import CliRunner

        from src.cli import cli

        result = CliRunner().invoke(
            cli, ["publish", "--dry-run", "--lite", "--with-spine-workbooks"]
        )
        assert result.exit_code != 0
        assert "with-spine-workbooks" in result.output
