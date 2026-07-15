"""Publish freshness manifest (R1) — external, artifact-keyed.

``data/out/publish_manifest.json`` maps repo-relative artifact paths to
``{sha256, producer, produced_at, stage_duration_s, versions,
inputs: {path: sha256}}`` plus a ``publish_run`` block. It is EXTERNAL
by design: the scoring sidecars stay byte-identical (the standing
reporting-layer constraint), so freshness metadata lives beside the
artifacts, never inside them.

Two consumers:

- the orchestrator's skip-if-fresh (compare each produced artifact's
  recorded hash against the current file, plus the run's dirty-path
  propagation — see ``src.publish.orchestrator``);
- reader-side ``verify_fresh`` (sequence 4 PR B): report modules that
  join across artifacts REFUSE to run on stale inputs instead of
  silently degrading (the PR #182 gap-staleness incident class). The
  refusal message template mirrors ``batch_runner.collect_batch``'s
  prompt-version drift guard.

Directories are hashed as a digest over (relative name, file sha256)
pairs so a whole artifact family (e.g. ``data/out/scoring/phase_a/``)
can be tracked as one input.

A note on mutating stages (``swagger-backfill`` rewrites the elements
artifacts ingest produced): entries are keyed by ARTIFACT and reflect
the LAST writer — ``producer`` simply becomes the later stage — and
``record()`` drops self-referencing inputs (a stage's own outputs) so
``verify_fresh`` never compares an artifact against its pre-mutation
self.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.utils.paths import out_dir, project_root

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1

# Sentinel recorded as an input hash when a declared EXTERNAL input did
# not exist at production time (issue #212 item 2). A curation sidecar
# or reviewer workbook that *appears* later then reads as drift — the
# consumer re-runs — instead of staying invisible forever (the old
# behavior: missing inputs were silently dropped from the record).
ABSENT = "<absent>"

# Module constants baked at import — tests monkeypatch these (the
# `_OUT_DIR` convention from CLAUDE.md operational gotchas).
_MANIFEST_PATH = out_dir() / "publish_manifest.json"
_PROJECT_ROOT = project_root()


class StaleArtifactError(RuntimeError):
    """An artifact's recorded inputs no longer match the files on disk —
    a downstream consumer would silently compute on stale data."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rel(path: Path) -> str:
    """Repo-relative manifest key (absolute fallback for test tmp dirs)."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(_PROJECT_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def abs_for(rel_input: str) -> Path:
    """Inverse of :func:`_rel` — resolve a recorded input key to a path."""
    candidate = Path(rel_input)
    if not candidate.is_absolute():
        candidate = _PROJECT_ROOT / rel_input
    return candidate


def sha256_path(path: Path) -> str | None:
    """sha256 of a file, or a stable digest of a directory tree.

    ``None`` when the path doesn't exist (callers treat missing inputs
    as unrecordable rather than erroring — existence policy is the
    stage's business, not the manifest's).
    """
    p = Path(path)
    if p.is_file():
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    if p.is_dir():
        h = hashlib.sha256()
        for f in sorted(p.rglob("*")):
            if not f.is_file():
                continue
            # Hidden files/dirs (.DS_Store, editor droppings) never
            # participate: raw-cache directories are tracked as inputs
            # since issue #212 item 2, and Finder metadata churn must
            # not read as source-document drift.
            if any(part.startswith(".") for part in f.relative_to(p).parts):
                continue
            h.update(f.relative_to(p).as_posix().encode())
            h.update(b"\0")
            file_hash = hashlib.sha256()
            with f.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    file_hash.update(chunk)
            h.update(file_hash.digest())
        return h.hexdigest()
    return None


@dataclass
class PublishManifest:
    """The artifact-keyed freshness ledger. Streaming-saved per stage."""

    path: Path
    data: dict = field(default_factory=dict)

    @classmethod
    def load_or_create(cls, path: Path | None = None) -> "PublishManifest":
        p = path or _MANIFEST_PATH
        if p.exists():
            return cls(path=p, data=json.loads(p.read_text(encoding="utf-8")))
        return cls(
            path=p,
            data={"manifest_version": MANIFEST_VERSION, "artifacts": {}},
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "PublishManifest | None":
        p = path or _MANIFEST_PATH
        if not p.exists():
            return None
        return cls(path=p, data=json.loads(p.read_text(encoding="utf-8")))

    # -- run bookkeeping ----------------------------------------------------

    def start_run(self, argv: list[str]) -> None:
        self.data["publish_run"] = {
            "started_at": _now_iso(),
            "finished_at": None,
            "argv": argv,
            "total_llm_usd": 0.0,
        }

    def finish_run(
        self,
        total_llm_usd: float,
        *,
        skipped_stages: tuple[str, ...] = (),
        tainted_stages: tuple[str, ...] = (),
        failed_at: str | None = None,
    ) -> None:
        """Close the run block. ``skipped_stages`` / ``tainted_stages``
        make ``--skip`` lineage auditable (issue #212 item 7): a skipped
        NOT-fresh stage taints its downstream cone, and the ledger says
        so instead of certifying the run as an ordinary success.
        ``failed_at`` (issue #213 item 3) records the stage a
        stop-the-line abort happened at, so a finished-but-failed run is
        distinguishable from a clean one in the ledger itself."""
        run = self.data.setdefault("publish_run", {})
        run["finished_at"] = _now_iso()
        run["total_llm_usd"] = round(total_llm_usd, 4)
        if skipped_stages:
            run["skipped_stages"] = list(skipped_stages)
        if tainted_stages:
            run["tainted_stages"] = list(tainted_stages)
        if failed_at:
            run["failed_at"] = failed_at

    # -- artifact records ---------------------------------------------------

    def record(
        self,
        artifact: Path,
        *,
        producer: str,
        versions: dict[str, str],
        inputs: dict[Path, str | None],
        stage_duration_s: float,
    ) -> None:
        """Record one produced artifact.

        Missing CONSUMED inputs are dropped (``None`` hash); externals
        the orchestrator snapshots as :data:`ABSENT` pass through so a
        later appearance reads as drift. Self-referencing inputs must be
        excluded by the caller.

        Raises ``ValueError`` when the artifact itself is missing —
        recording ``complete`` for a file the stage never wrote is
        registry↔run() drift, and silence here meant a permanently
        never-fresh stage diagnosed only by reading manifest JSON
        (issue #212 item 7).
        """
        sha = sha256_path(artifact)
        if sha is None:
            raise ValueError(
                f"stage '{producer}' declares {_rel(artifact)} in its "
                f"produces but the file does not exist after the stage "
                f"ran — publish stage registry drift"
            )
        self.data.setdefault("artifacts", {})[_rel(artifact)] = {
            "sha256": sha,
            "producer": producer,
            "produced_at": _now_iso(),
            "stage_duration_s": round(stage_duration_s, 2),
            "versions": versions,
            "inputs": {
                _rel(p): h for p, h in inputs.items() if h is not None
            },
        }

    def entry_for(self, artifact: Path) -> dict | None:
        return (self.data.get("artifacts") or {}).get(_rel(artifact))

    def save(self) -> None:
        self.data["generated_at"] = _now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def verify_fresh(
    artifact: Path,
    *,
    consumer: str,
    manifest_path: Path | None = None,
    allow_stale: bool = False,
) -> str:
    """Reader-side freshness gate (wired into consumers in seq-4 PR B).

    Verdicts:
      - ``no_manifest``          — no manifest on disk: legacy behavior,
        nothing to check (fresh clones / test fixtures stay silent);
      - ``untracked``            — manifest exists but never recorded
        this artifact: silent (artifact predates publish, or a bespoke
        path);
      - ``fresh``                — every recorded input hash matches the
        file currently on disk;
      - ``manifest_out_of_date`` — the ARTIFACT's own hash differs from
        its record (someone re-ran its producer manually): WARN only —
        the artifact is newer than the ledger, not stale;
      - ``stale_allowed``        — inputs drifted but ``allow_stale``:
        WARN and proceed on degraded numbers.

    Raises :class:`StaleArtifactError` when a recorded input's current
    hash differs and ``allow_stale`` is False — the artifact was built
    from files that have since changed (the silent-degrade incident
    class this manifest exists to kill).

    Also raises when the artifact's recorded ``scoring_plan_version``
    differs from the code's current value (issue #212 item 1): after a
    plan bump the sidecars MUST be regenerated before any reader joins
    across them, and pre-bump artifacts reading as fresh was exactly
    the silent-no-op this gate exists to kill. Entries recorded without
    the version key (hand-built test fixtures) skip the check.
    """
    manifest = PublishManifest.load(manifest_path)
    if manifest is None:
        return "no_manifest"
    entry = manifest.entry_for(artifact)
    if entry is None:
        return "untracked"

    recorded_plan = (entry.get("versions") or {}).get("scoring_plan_version")
    if recorded_plan is not None:
        from src.score.aggregate import SCORING_PLAN_VERSION

        if recorded_plan != SCORING_PLAN_VERSION:
            message = (
                f"{_rel(artifact)} was produced at scoring_plan_version "
                f"{recorded_plan} but the code is now at "
                f"{SCORING_PLAN_VERSION} — {consumer} would join across "
                f"artifacts from a superseded methodology. Re-run: mc "
                f"publish (stage '{entry.get('producer')}' and its "
                f"downstream cone). Pass --allow-stale to proceed on the "
                f"pre-bump numbers."
            )
            if allow_stale:
                logger.warning("ALLOW-STALE: %s", message)
                return "stale_allowed"
            raise StaleArtifactError(message)

    if not Path(artifact).exists():
        # The manifest tracks this artifact but the file is gone —
        # someone swept data/out (or a partial restore). Consuming code
        # would silently degrade to its missing-file fallback; with a
        # publish lineage established that silence is exactly the
        # incident class this gate kills.
        message = (
            f"{_rel(artifact)} is tracked by the publish manifest (stage "
            f"'{entry.get('producer')}') but MISSING on disk — {consumer} "
            f"would silently degrade. Re-run: mc publish --from "
            f"{entry.get('producer')}. Pass --allow-stale to accept the "
            f"degraded fallback."
        )
        if allow_stale:
            logger.warning("ALLOW-STALE: %s", message)
            return "stale_allowed"
        raise StaleArtifactError(message)

    stale: list[tuple[str, str, str]] = []  # (input, recorded, current)
    for rel_input, recorded in (entry.get("inputs") or {}).items():
        current = sha256_path(abs_for(rel_input))
        if recorded == ABSENT:
            # External input absent at production time — its APPEARANCE
            # is the drift signal (e.g. a first curation sidecar).
            if current is not None:
                stale.append((rel_input, ABSENT, current))
            continue
        if current is None or current != recorded:
            stale.append((rel_input, recorded, current or "<missing>"))
    if stale:
        lines = "; ".join(
            f"{name} (recorded {rec[:8]}…, current {cur[:8] if cur != '<missing>' else cur})"
            for name, rec, cur in stale
        )
        message = (
            f"{_rel(artifact)} is stale for {consumer}: input(s) changed "
            f"since stage '{entry.get('producer')}' produced it — {lines}. "
            f"Re-run: mc publish --from {entry.get('producer')} "
            f"(or the stage's own command). Pass --allow-stale to proceed "
            f"on the stale artifact (degraded numbers — see PR #182)."
        )
        if allow_stale:
            logger.warning("ALLOW-STALE: %s", message)
            return "stale_allowed"
        raise StaleArtifactError(message)

    current_self = sha256_path(Path(artifact))
    if current_self is not None and current_self != entry.get("sha256"):
        logger.warning(
            "%s differs from its manifest record (producer '%s' re-run "
            "manually?) — the artifact is NEWER than the ledger; consider "
            "a fresh `mc publish` to restore lineage",
            _rel(artifact), entry.get("producer"),
        )
        return "manifest_out_of_date"
    return "fresh"
