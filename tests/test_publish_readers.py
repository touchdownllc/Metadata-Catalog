"""Fail-loudly reader gates (sequence 4 PR B) — the three sites that
previously degraded SILENTLY on missing/stale cross-artifact inputs now
refuse when the publish manifest establishes lineage, and stay exactly
legacy when no manifest exists."""

from __future__ import annotations

import json

import pytest

from src.publish import manifest as manifest_mod
from src.publish.manifest import (
    PublishManifest,
    StaleArtifactError,
    sha256_path,
)


def _track(manifest_path, artifact, producer, inputs):
    # The LIVE plan version, not a literal: verify_fresh refuses
    # version-mismatched artifacts since issue #212 item 1, and these
    # fixtures model correctly-produced (current-version) artifacts.
    from src.score.aggregate import SCORING_PLAN_VERSION

    m = PublishManifest.load_or_create(manifest_path)
    m.record(
        artifact,
        producer=producer,
        versions={"scoring_plan_version": SCORING_PLAN_VERSION},
        inputs={p: sha256_path(p) for p in inputs},
        stage_duration_s=0.1,
    )
    m.save()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """tmp out-dir + manifest redirect; returns (out, manifest_path)."""
    out = tmp_path / "out"
    out.mkdir()
    manifest_path = tmp_path / "publish_manifest.json"
    monkeypatch.setattr(manifest_mod, "_MANIFEST_PATH", manifest_path)
    return out, manifest_path


class TestAggregateSourceBorrowGate:
    """`aggregate --lens spine` reads the source sidecar (constraint a)."""

    def _sidecar(self, out, scores=()):
        path = out / "az_scores_source.json"
        path.write_text(
            json.dumps({"scores": list(scores)}), encoding="utf-8"
        )
        return path

    def _load(self, out, **kw):
        from src.score.aggregate import _load_source_extension_facts

        return _load_source_extension_facts(
            "AZ", artifacts_dir=out / "scoring" / "phase_a", **kw
        )

    def test_no_manifest_legacy_empty_dict(self, env):
        out, _mp = env
        assert self._load(out) == {}

    def test_tracked_but_missing_raises(self, env):
        out, mp = env
        sidecar = self._sidecar(out)
        _track(mp, sidecar, "aggregate-source", [])
        sidecar.unlink()
        with pytest.raises(StaleArtifactError, match="aggregate-source"):
            self._load(out)

    def test_stale_input_raises_and_allow_stale_degrades(self, env):
        out, mp = env
        sidecar = self._sidecar(out)
        upstream = out / "az_elements_source.json"
        upstream.write_text("[]", encoding="utf-8")
        _track(mp, sidecar, "aggregate-source", [upstream])
        upstream.write_text("[1]", encoding="utf-8")  # drift
        with pytest.raises(StaleArtifactError, match="--allow-stale"):
            self._load(out)
        assert self._load(out, allow_stale=True) == {}

    def test_fresh_reads_normally(self, env):
        out, mp = env
        sidecar = self._sidecar(
            out,
            scores=[{
                "record_key": "AZ|Calendar|code",
                "fact_provenance": {
                    "extension_is_necessary": {"value": True}
                },
            }],
        )
        _track(mp, sidecar, "aggregate-source", [])
        assert self._load(out) == {"az|calendar|code": True}


class TestReviewComparisonGapGate:
    """The PR #182 incident class — stale gap layer under the digest."""

    def test_stale_gap_sidecar_raises(self, env):
        from src.score.review_comparison import load_gap_scores

        out, mp = env
        gap = out / "az_scores_gap.json"
        gap.write_text(json.dumps({"scores": []}), encoding="utf-8")
        upstream = out / "az_elements_gap.json"
        upstream.write_text("[]", encoding="utf-8")
        _track(mp, gap, "aggregate-gap", [upstream])
        upstream.write_text("[1]", encoding="utf-8")
        with pytest.raises(StaleArtifactError, match="aggregate-gap"):
            load_gap_scores("AZ", sidecar_dir=out)
        assert load_gap_scores("AZ", sidecar_dir=out, allow_stale=True) == {}

    def test_stale_gap_elements_raises(self, env):
        from src.score.review_comparison import load_gap_lookup

        out, mp = env
        gap = out / "az_elements_gap.json"
        gap.write_text(json.dumps({"gaps": []}), encoding="utf-8")
        upstream = out / "az_elements_source.json"
        upstream.write_text("[]", encoding="utf-8")
        _track(mp, gap, "gap-surface", [upstream])
        upstream.write_text("[1]", encoding="utf-8")
        with pytest.raises(StaleArtifactError, match="gap-surface"):
            load_gap_lookup("AZ", gap_dir=out)

    def test_no_manifest_legacy_degrade(self, env):
        from src.score.review_comparison import (
            load_gap_lookup,
            load_gap_scores,
        )

        out, _mp = env
        assert load_gap_scores("AZ", sidecar_dir=out) == {}
        assert load_gap_lookup("AZ", gap_dir=out) == {}


class TestAnalystScoresSidecarGate:
    """Issue #212 item 3 — the loader that fills EVERY score cell in
    EVERY deliverable workbook had no freshness gate and swallowed
    corrupt files into {}."""

    def _sidecar(self, out, scores=()):
        path = out / "az_scores_source.json"
        path.write_text(
            json.dumps({"scores": list(scores)}), encoding="utf-8"
        )
        return path

    def _load(self, out, **kw):
        from src.report.analyst import _load_scores_sidecar

        return _load_scores_sidecar("AZ", "source", out, **kw)

    def test_no_manifest_legacy(self, env):
        out, _mp = env
        assert self._load(out) == {}  # missing → {}
        self._sidecar(
            out, scores=[{"record_key": "AZ|Calendar|code"}]
        )
        assert "AZ|Calendar|code" in self._load(out)

    def test_stale_raises_and_allow_stale_degrades(self, env):
        out, mp = env
        sidecar = self._sidecar(out)
        upstream = out / "az_elements_source.json"
        upstream.write_text("[]", encoding="utf-8")
        _track(mp, sidecar, "aggregate-source", [upstream])
        upstream.write_text("[1]", encoding="utf-8")  # drift
        with pytest.raises(StaleArtifactError, match="--allow-stale"):
            self._load(out)
        assert self._load(out, allow_stale=True) == {}

    def test_tracked_but_missing_raises(self, env):
        out, mp = env
        sidecar = self._sidecar(out)
        _track(mp, sidecar, "aggregate-source", [])
        sidecar.unlink()
        with pytest.raises(StaleArtifactError, match="aggregate-source"):
            self._load(out)

    def test_corrupt_raises_even_without_manifest(self, env):
        from src.utils.artifacts import ArtifactReadError

        out, _mp = env
        path = out / "az_scores_source.json"
        path.write_text('{"scores": [truncated', encoding="utf-8")
        with pytest.raises(ArtifactReadError, match="corrupt"):
            self._load(out)
        # allow_stale is a STALENESS escape, not a corruption escape.
        with pytest.raises(ArtifactReadError):
            self._load(out, allow_stale=True)


class TestReviewQueueRoutesGate:
    def test_corrupt_routes_artifact_raises(self, env):
        from src.report.analyst import _load_review_queue_routes
        from src.utils.artifacts import ArtifactReadError

        out, _mp = env
        (out / "review_queue_source.json").write_text(
            "not json", encoding="utf-8"
        )
        with pytest.raises(ArtifactReadError):
            _load_review_queue_routes("source", out)

    def test_stale_routes_artifact_raises(self, env):
        from src.report.analyst import _load_review_queue_routes

        out, mp = env
        queue = out / "review_queue_source.json"
        queue.write_text(json.dumps({"entries": []}), encoding="utf-8")
        upstream = out / "az_scores_source.json"
        upstream.write_text("{}", encoding="utf-8")
        _track(mp, queue, "report-review-queue", [upstream])
        upstream.write_text('{"drift": 1}', encoding="utf-8")
        with pytest.raises(StaleArtifactError, match="report-review-queue"):
            _load_review_queue_routes("source", out)
        assert _load_review_queue_routes(
            "source", out, allow_stale=True
        ) == {}


class TestRollupLoaderGates:
    """review-queue / scoring-rollup / recommendations sidecar loads
    refuse stale inputs under a publish lineage."""

    def _stale_sidecar(self, out, mp):
        sidecar = out / "az_scores_source.json"
        sidecar.write_text(json.dumps({"scores": []}), encoding="utf-8")
        upstream = out / "az_elements_source.json"
        upstream.write_text("[]", encoding="utf-8")
        _track(mp, sidecar, "aggregate-source", [upstream])
        upstream.write_text("[1]", encoding="utf-8")
        return sidecar

    def test_review_queue_loader_refuses_stale(self, env):
        from src.report.review_queue import _load_sidecar

        out, mp = env
        self._stale_sidecar(out, mp)
        with pytest.raises(StaleArtifactError):
            _load_sidecar("AZ", "source", base=out)
        assert _load_sidecar(
            "AZ", "source", base=out, allow_stale=True
        ) == {"scores": []}

    def test_scoring_rollup_loader_refuses_stale(self, env):
        from src.report.scoring import _load_sidecar

        out, mp = env
        self._stale_sidecar(out, mp)
        with pytest.raises(StaleArtifactError):
            _load_sidecar("AZ", "source", base=out)

    def test_recommendations_refuses_stale(self, env):
        from src.report.recommendations import generate_state

        out, mp = env
        self._stale_sidecar(out, mp)
        with pytest.raises(StaleArtifactError):
            generate_state("AZ", "source", base=out)
        result = generate_state(
            "AZ", "source", base=out, allow_stale=True
        )
        assert result["records"] == []

    def test_recommendations_corrupt_raises(self, env):
        from src.report.recommendations import generate_state
        from src.utils.artifacts import ArtifactReadError

        out, _mp = env
        (out / "az_scores_source.json").write_text(
            "{broken", encoding="utf-8"
        )
        with pytest.raises(ArtifactReadError):
            generate_state("AZ", "source", base=out)


class TestAggregateBorrowCorruptGate:
    """The parse-failure branch PR B left open: a CORRUPT source
    sidecar silently degraded every spine extension row to the
    conservative fallback."""

    def test_corrupt_source_sidecar_raises(self, env):
        from src.score.aggregate import _load_source_extension_facts
        from src.utils.artifacts import ArtifactReadError

        out, _mp = env
        (out / "az_scores_source.json").write_text(
            '{"scores": [tru', encoding="utf-8"
        )
        with pytest.raises(ArtifactReadError, match="fact-borrow"):
            _load_source_extension_facts(
                "AZ", artifacts_dir=out / "scoring" / "phase_a"
            )

    def test_explicit_source_sidecar_path(self, env, tmp_path):
        """The data/out ← phase_a path relationship is no longer
        hard-encoded: a nonstandard artifacts_dir can point at the real
        sidecar explicitly instead of silently missing it."""
        from src.score.aggregate import _load_source_extension_facts

        out, _mp = env
        elsewhere = tmp_path / "elsewhere" / "az_scores_source.json"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text(
            json.dumps({"scores": [{
                "record_key": "AZ|Calendar|code",
                "fact_provenance": {
                    "extension_is_necessary": {"value": True}
                },
            }]}),
            encoding="utf-8",
        )
        got = _load_source_extension_facts(
            "AZ",
            artifacts_dir=out / "scoring" / "phase_a",
            source_sidecar_path=elsewhere,
        )
        assert got == {"az|calendar|code": True}


class TestAnalystGapGate:
    def test_stale_gap_sidecar_raises(self, env):
        from src.report.analyst import _load_gap_scores_sidecar

        out, mp = env
        gap = out / "az_scores_gap.json"
        gap.write_text(json.dumps({"scores": []}), encoding="utf-8")
        upstream = out / "az_elements_gap.json"
        upstream.write_text("[]", encoding="utf-8")
        _track(mp, gap, "aggregate-gap", [upstream])
        upstream.write_text("[1]", encoding="utf-8")
        with pytest.raises(StaleArtifactError):
            _load_gap_scores_sidecar("AZ", out)
        assert _load_gap_scores_sidecar("AZ", out, allow_stale=True) == {}

    def test_no_manifest_legacy(self, env):
        from src.report.analyst import (
            _load_gap_metadata,
            _load_gap_scores_sidecar,
        )

        out, _mp = env
        assert _load_gap_scores_sidecar("AZ", out) == {}
        assert _load_gap_metadata("AZ", out) == {}
