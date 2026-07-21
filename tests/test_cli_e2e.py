"""End-to-end CLI smoke — drives `mc ingest az` + the four report
commands via Click's CliRunner and verifies they produce schema-valid
artifacts without writing to the real `data/out/`.

Requires locally cached inputs (AZ XLSX + rules, all four state spines,
elements/gap logs for WI/MN/TX) that are gitignored and therefore absent
in CI. The test skips cleanly when any input is missing so the suite
stays green in both environments.

Scope: AZ-only ingest (no Docker / Playwright / Confluence). The reports
are invoked with all four states loaded so coverage/divergence have a
full 4-state cross-sectional view — WI/MN/TX inputs come from the
committed goldens via the `data/out/` mirror the dev machine carries
after a prior `mc ingest <state>` run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from src import cli as cli_module
from src.models.element import StateElements

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REAL_OUT = _REPO_ROOT / "data" / "out"
_REAL_RAW_AZ = _REPO_ROOT / "data" / "raw" / "az"
_REAL_SPINE = _REPO_ROOT / "data" / "spine"

_AZ_XLSX = _REAL_RAW_AZ / "Use_Case_12.0_20260227.xlsm"
_AZ_RULES = _REAL_RAW_AZ / "az_integrity_rules.json"
_ALL_STATES = ("az", "wi", "mn", "tx", "in")

_REQUIRED_INPUTS: list[Path] = [
    _AZ_XLSX,
    *[_REAL_SPINE / f"{s}_spine.json" for s in _ALL_STATES],
    *[_REAL_OUT / f"{s}_elements_source.json" for s in _ALL_STATES],
    *[_REAL_OUT / f"{s}_elements_spine.json" for s in _ALL_STATES],
    *[_REAL_OUT / f"{s}_gap_log.json" for s in _ALL_STATES],
]


@pytest.fixture
def tmp_out(tmp_path, monkeypatch):
    """Prep a tmp `data/out/` seeded with the three non-AZ states'
    elements + gap logs, and redirect every module that resolves the
    out-dir at import time to point into it. AZ outputs will be written
    fresh by `ingest az`; the other three remain at their pre-run
    snapshots so coverage/divergence/analyst have a 4-state view."""
    missing = [p for p in _REQUIRED_INPUTS if not p.exists()]
    if missing:
        pytest.skip(
            f"{len(missing)} cached input(s) missing — "
            f"run `mc ingest <state>` for each state first. "
            f"First missing: {missing[0].relative_to(_REPO_ROOT)}"
        )

    out = tmp_path / "out"
    out.mkdir()

    # Seed non-AZ inputs so reports can see all five states.
    for s in ("wi", "mn", "tx", "in"):
        for name in (
            f"{s}_elements_source.json",
            f"{s}_elements_spine.json",
            f"{s}_gap_log.json",
        ):
            shutil.copy(_REAL_OUT / name, out / name)

    # Redirect Arizona adapter's pre-resolved output paths.
    import src.ingest.arizona as arizona
    monkeypatch.setattr(arizona, "_AZ_ELEMENTS_OUT", out / "az_elements_source.json")
    monkeypatch.setattr(arizona, "_AZ_ELEMENTS_SPINE_OUT", out / "az_elements_spine.json")
    monkeypatch.setattr(arizona, "_AZ_GAP_OUT", out / "az_gap_log.json")

    # Redirect report modules that resolve `_OUT_DIR` at import time.
    import src.report.coverage as coverage
    import src.report.analyst as analyst
    monkeypatch.setattr(coverage, "_OUT_DIR", out)
    monkeypatch.setattr(analyst, "_OUT_DIR", out)

    # Divergence reaches for `out_dir()` / `divergence_path()` from
    # utils.paths — patch at the paths module so all downstream helpers
    # pick up the redirect.
    import src.utils.paths as paths_mod
    monkeypatch.setattr(paths_mod, "_OUT_DIR", out)

    return out


def _schema_check(path: Path) -> None:
    """Round-trip the produced JSON through `StateElements` and verify
    record fields match the 21-field contract from bucket 2."""
    from tests.test_record_schema import EXPECTED_FIELDS, VALID_SOURCES

    data = json.loads(path.read_text(encoding="utf-8"))
    StateElements.model_validate(data)  # parses or raises
    assert data["elements"], f"{path.name}: no elements"
    assert data["element_count"] == len(data["elements"]), (
        f"{path.name}: element_count disagrees with len(elements)"
    )
    for i, r in enumerate(data["elements"]):
        assert set(r.keys()) == EXPECTED_FIELDS, (
            f"{path.name} record[{i}]: field set drifted from the 21-field "
            f"contract"
        )
        assert r["source"] in VALID_SOURCES, (
            f"{path.name} record[{i}]: unknown source {r['source']!r}"
        )


def _invoke(runner: CliRunner, args: list[str]) -> None:
    result = runner.invoke(cli_module.cli, args, catch_exceptions=False)
    assert result.exit_code == 0, (
        f"`poc3 {' '.join(args)}` exited {result.exit_code}\n"
        f"stdout:\n{result.stdout}"
    )


@pytest.mark.realdata
def test_cli_ingest_az_then_reports(tmp_out: Path) -> None:
    """Full CLI path on AZ: ingest → two coverage lenses → two analyst
    lenses → divergence. Each command exits 0, writes its expected
    artifacts under the redirected `data/out/`, and every JSON output
    passes the bucket-2 schema contract."""
    runner = CliRunner()

    # 1. ingest az — writes the three AZ artifacts into tmp_out.
    _invoke(runner, ["ingest", "az"])

    az_source = tmp_out / "az_elements_source.json"
    az_spine = tmp_out / "az_elements_spine.json"
    az_gap = tmp_out / "az_gap_log.json"
    assert az_source.exists(), "ingest az did not write az_elements_source.json"
    assert az_spine.exists(), "ingest az did not write az_elements_spine.json"
    assert az_gap.exists(), "ingest az did not write az_gap_log.json"

    _schema_check(az_source)
    _schema_check(az_spine)
    # gap_log is free-form diagnostic dict; just parse it.
    assert json.loads(az_gap.read_text(encoding="utf-8"))["state"] == "AZ"

    # 2. report coverage (source and spine).
    _invoke(runner, ["report", "coverage"])
    assert (tmp_out / "coverage_report.json").exists()
    assert (tmp_out / "coverage_report.md").exists()

    _invoke(runner, ["report", "coverage", "--lens", "spine"])
    assert (tmp_out / "coverage_report_spine.json").exists()
    assert (tmp_out / "coverage_report_spine.md").exists()

    # Verify both coverage JSONs carry all four states (confirms the
    # non-AZ seed-copy + redirect worked end-to-end).
    for name in ("coverage_report.json", "coverage_report_spine.json"):
        rep = json.loads((tmp_out / name).read_text(encoding="utf-8"))
        states = {b["state"] for b in rep["per_state"]}
        assert states == {"AZ", "WI", "MN", "TX", "IN"}, (
            f"{name}: per_state states={states} — expected full 5-state view"
        )

    # 3. report analyst (source and spine), AZ only.
    _invoke(runner, ["report", "analyst", "--state", "az"])
    assert (tmp_out / "az_analyst.xlsx").exists()
    assert (tmp_out / "az_analyst.xlsx").stat().st_size > 0

    _invoke(runner, ["report", "analyst", "--state", "az", "--lens", "spine"])
    assert (tmp_out / "az_analyst_spine.xlsx").exists()
    assert (tmp_out / "az_analyst_spine.xlsx").stat().st_size > 0

    # 4. report divergence.
    _invoke(runner, ["report", "divergence"])
    assert (tmp_out / "lens_divergence.json").exists()
    assert (tmp_out / "lens_divergence.md").exists()

    divergence = json.loads(
        (tmp_out / "lens_divergence.json").read_text(encoding="utf-8")
    )
    states = {b["state"] for b in divergence["per_state"]}
    assert states == {"AZ", "WI", "MN", "TX", "IN"}

    # 5. Assert tests did NOT clobber the real data/out/ as a final
    # sanity — the monkeypatch is the whole point of the tmp fixture.
    az_real = _REAL_OUT / "az_elements_source.json"
    if az_real.exists():
        real_mtime = az_real.stat().st_mtime
        # The fixture was populated from data/out/ at test setup; reports
        # run against tmp_out only. The real file's mtime should be the
        # pre-test value. We can't strictly assert an unchanged mtime here
        # without snapshotting, but we can assert the produced AZ elements
        # live in tmp_out, not data/out/.
        assert az_source.resolve() != az_real.resolve()
