"""Tests for the publish freshness manifest (sequence 4 / R1)."""

from __future__ import annotations


import pytest

from src.publish.manifest import (
    PublishManifest,
    StaleArtifactError,
    sha256_path,
    verify_fresh,
)


class TestSha256Path:
    def test_file_hash_is_stable(self, tmp_path):
        f = tmp_path / "a.json"
        f.write_text("{}", encoding="utf-8")
        assert sha256_path(f) == sha256_path(f)
        g = tmp_path / "b.json"
        g.write_text("{ }", encoding="utf-8")
        assert sha256_path(f) != sha256_path(g)

    def test_directory_digest_tracks_contents(self, tmp_path):
        d = tmp_path / "dir"
        d.mkdir()
        (d / "x.jsonl").write_text("1", encoding="utf-8")
        before = sha256_path(d)
        (d / "y.jsonl").write_text("2", encoding="utf-8")
        after = sha256_path(d)
        assert before != after
        # Same content → same digest (order-independent via sort).
        assert sha256_path(d) == after

    def test_missing_path_is_none(self, tmp_path):
        assert sha256_path(tmp_path / "nope") is None


class TestManifestRoundTrip:
    def test_record_save_load_entry(self, tmp_path):
        artifact = tmp_path / "az_scores_source.json"
        artifact.write_text("{}", encoding="utf-8")
        source = tmp_path / "az_elements_source.json"
        source.write_text("[]", encoding="utf-8")

        m = PublishManifest.load_or_create(tmp_path / "manifest.json")
        m.start_run(["publish"])
        m.record(
            artifact,
            producer="aggregate-source",
            versions={"scoring_plan_version": "27"},
            inputs={source: sha256_path(source)},
            stage_duration_s=1.25,
        )
        m.finish_run(0.0)
        m.save()

        loaded = PublishManifest.load(tmp_path / "manifest.json")
        assert loaded is not None
        entry = loaded.entry_for(artifact)
        assert entry["producer"] == "aggregate-source"
        assert entry["sha256"] == sha256_path(artifact)
        assert entry["versions"] == {"scoring_plan_version": "27"}
        [(input_key, input_sha)] = entry["inputs"].items()
        assert input_key.endswith("az_elements_source.json")
        assert input_sha == sha256_path(source)
        assert loaded.data["publish_run"]["finished_at"]

    def test_load_missing_returns_none(self, tmp_path):
        assert PublishManifest.load(tmp_path / "manifest.json") is None

    def test_record_missing_artifact_raises(self, tmp_path):
        """Silent-return on a missing artifact meant a stage that
        stopped writing a declared file recorded `complete` with no
        error (issue #212 item 7)."""
        m = PublishManifest.load_or_create(tmp_path / "manifest.json")
        with pytest.raises(ValueError, match="registry drift"):
            m.record(
                tmp_path / "ghost.json",
                producer="x", versions={}, inputs={}, stage_duration_s=0,
            )
        assert m.data["artifacts"] == {}


class TestVerifyFresh:
    def _setup(self, tmp_path, *, plan_version=None):
        from src.score.aggregate import SCORING_PLAN_VERSION

        artifact = tmp_path / "az_scores_gap.json"
        artifact.write_text('{"gap": 1}', encoding="utf-8")
        source = tmp_path / "az_elements_gap.json"
        source.write_text("[1]", encoding="utf-8")
        manifest_path = tmp_path / "manifest.json"
        m = PublishManifest.load_or_create(manifest_path)
        m.record(
            artifact,
            producer="aggregate-gap",
            versions={
                "scoring_plan_version": plan_version or SCORING_PLAN_VERSION
            },
            inputs={source: sha256_path(source)},
            stage_duration_s=0.1,
        )
        m.save()
        return artifact, source, manifest_path

    def test_no_manifest_is_silent(self, tmp_path):
        artifact = tmp_path / "a.json"
        artifact.write_text("{}", encoding="utf-8")
        assert verify_fresh(
            artifact, consumer="test",
            manifest_path=tmp_path / "manifest.json",
        ) == "no_manifest"

    def test_untracked_is_silent(self, tmp_path):
        artifact, _source, manifest_path = self._setup(tmp_path)
        other = tmp_path / "other.json"
        other.write_text("{}", encoding="utf-8")
        assert verify_fresh(
            other, consumer="test", manifest_path=manifest_path
        ) == "untracked"

    def test_fresh_when_inputs_match(self, tmp_path):
        artifact, _source, manifest_path = self._setup(tmp_path)
        assert verify_fresh(
            artifact, consumer="test", manifest_path=manifest_path
        ) == "fresh"

    def test_stale_input_raises_with_instructive_message(self, tmp_path):
        artifact, source, manifest_path = self._setup(tmp_path)
        source.write_text("[1, 2]", encoding="utf-8")  # drift
        with pytest.raises(StaleArtifactError) as exc:
            verify_fresh(
                artifact, consumer="report review-digest",
                manifest_path=manifest_path,
            )
        message = str(exc.value)
        assert "report review-digest" in message
        assert "aggregate-gap" in message           # names the producer
        assert "--from aggregate-gap" in message    # names the resume
        assert "--allow-stale" in message           # names the escape

    def test_missing_input_raises(self, tmp_path):
        artifact, source, manifest_path = self._setup(tmp_path)
        source.unlink()
        with pytest.raises(StaleArtifactError):
            verify_fresh(
                artifact, consumer="test", manifest_path=manifest_path
            )

    def test_allow_stale_warns_and_proceeds(self, tmp_path, caplog):
        artifact, source, manifest_path = self._setup(tmp_path)
        source.write_text("[1, 2]", encoding="utf-8")
        with caplog.at_level("WARNING"):
            verdict = verify_fresh(
                artifact, consumer="test",
                manifest_path=manifest_path, allow_stale=True,
            )
        assert verdict == "stale_allowed"
        assert any("ALLOW-STALE" in r.message for r in caplog.records)

    def test_manually_rerun_producer_warns_not_raises(self, tmp_path, caplog):
        artifact, _source, manifest_path = self._setup(tmp_path)
        artifact.write_text('{"gap": 2}', encoding="utf-8")  # newer artifact
        with caplog.at_level("WARNING"):
            verdict = verify_fresh(
                artifact, consumer="test", manifest_path=manifest_path
            )
        assert verdict == "manifest_out_of_date"

    def test_scoring_plan_version_mismatch_raises(self, tmp_path):
        """Issue #212 item 1, reader side: a sidecar produced at a
        superseded plan version must refuse to feed a join."""
        artifact, _source, manifest_path = self._setup(
            tmp_path, plan_version="1-superseded"
        )
        with pytest.raises(StaleArtifactError) as exc:
            verify_fresh(
                artifact, consumer="report review-digest",
                manifest_path=manifest_path,
            )
        message = str(exc.value)
        assert "scoring_plan_version" in message
        assert "1-superseded" in message
        assert "--allow-stale" in message
        assert verify_fresh(
            artifact, consumer="t",
            manifest_path=manifest_path, allow_stale=True,
        ) == "stale_allowed"

    def test_absent_external_appearing_raises(self, tmp_path):
        """Issue #212 item 2: an external input recorded ABSENT at
        production (e.g. no curation sidecar yet) reads stale the
        moment the file appears."""
        from src.publish.manifest import ABSENT
        from src.score.aggregate import SCORING_PLAN_VERSION

        artifact = tmp_path / "az_analyst.xlsx"
        artifact.write_text("wb", encoding="utf-8")
        curation = tmp_path / "az.json"
        manifest_path = tmp_path / "manifest.json"
        m = PublishManifest.load_or_create(manifest_path)
        m.record(
            artifact,
            producer="report-analyst",
            versions={"scoring_plan_version": SCORING_PLAN_VERSION},
            inputs={curation: ABSENT},
            stage_duration_s=0.1,
        )
        m.save()
        assert verify_fresh(
            artifact, consumer="t", manifest_path=manifest_path
        ) == "fresh"
        curation.write_text("{}", encoding="utf-8")  # appears
        with pytest.raises(StaleArtifactError):
            verify_fresh(
                artifact, consumer="t", manifest_path=manifest_path
            )

    def test_mutating_producer_self_input_excluded(self, tmp_path):
        """The orchestrator drops self-referencing inputs at record time;
        prove the reader stays quiet for a backfill-style mutation."""
        artifact = tmp_path / "az_elements_source.json"
        artifact.write_text("post-backfill", encoding="utf-8")
        spine = tmp_path / "az_spine.json"
        spine.write_text("spine", encoding="utf-8")
        manifest_path = tmp_path / "manifest.json"
        m = PublishManifest.load_or_create(manifest_path)
        m.record(
            artifact,
            producer="swagger-backfill",
            versions={},
            inputs={spine: sha256_path(spine)},  # self NOT recorded
            stage_duration_s=0.1,
        )
        m.save()
        assert verify_fresh(
            artifact, consumer="test", manifest_path=manifest_path
        ) == "fresh"
