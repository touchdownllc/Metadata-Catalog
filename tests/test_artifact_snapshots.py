"""Golden-snapshot tests for per-state elements artifacts.

Freezes `data/out/{state}_elements_{source,spine}.json` under
`tests/golden/` as committed contract fixtures. Two layers:

1. **Shape round-trip (always runs).** Each golden loads back through
   `StateElements`, re-serializes, and must round-trip to the same JSON.
   Guards against silent `ElementRecord` / `StateElements` shape drift —
   if a field is renamed, removed, retyped, or has its default changed,
   this test fails with a field-level diff.

2. **Drift check against `data/out/` (skipped when absent).** On a dev
   machine with a fresh pipeline run, `data/out/{state}_elements_{lens}.json`
   compared field-level against the golden. On mismatch, the first 20
   differences print (path, before, after) so the author can tell
   intentional regeneration from a regression. CI (no `data/out/`)
   skips this layer cleanly.

`extracted_at` is stripped from both sides — it's the run timestamp and
would diff on every execution.

Regeneration ritual lives in `tests/golden/README.md`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.models.element import StateElements

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tests" / "golden"
_DATA_OUT = _REPO_ROOT / "data" / "out"

STATES = ("az", "wi", "mn", "tx", "in")
LENSES = ("source", "spine")
_CASES = [(s, l) for s in STATES for l in LENSES]


def _strip_timestamp(payload: dict) -> dict:
    """Remove run-dependent `extracted_at` before comparing."""
    out = dict(payload)
    out.pop("extracted_at", None)
    return out


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _diff_paths(
    golden: object,
    current: object,
    path: str = "",
    limit: int = 20,
) -> list[tuple[str, object, object]]:
    """Walk two JSON-ish trees and yield up to `limit` field-level diffs.

    Each diff is `(path, golden_value, current_value)`. Dict keys that
    appear on only one side are reported as `<missing>` on the other.
    Lists compare element-wise; extra tail elements surface under
    synthesized `[i]` paths.
    """
    diffs: list[tuple[str, object, object]] = []

    def _walk(a: object, b: object, p: str) -> None:
        if len(diffs) >= limit:
            return
        if type(a) is not type(b):
            diffs.append((p or "<root>", a, b))
            return
        if isinstance(a, dict):
            for key in sorted(set(a) | set(b)):
                if len(diffs) >= limit:
                    return
                sub_path = f"{p}.{key}" if p else key
                if key not in a:
                    diffs.append((sub_path, "<missing>", b[key]))
                    continue
                if key not in b:
                    diffs.append((sub_path, a[key], "<missing>"))
                    continue
                _walk(a[key], b[key], sub_path)
            return
        if isinstance(a, list):
            for i in range(max(len(a), len(b))):
                if len(diffs) >= limit:
                    return
                sub_path = f"{p}[{i}]"
                if i >= len(a):
                    diffs.append((sub_path, "<missing>", b[i]))
                    continue
                if i >= len(b):
                    diffs.append((sub_path, a[i], "<missing>"))
                    continue
                _walk(a[i], b[i], sub_path)
            return
        if a != b:
            diffs.append((p or "<root>", a, b))

    _walk(golden, current, path)
    return diffs


def _format_diffs(diffs: list[tuple[str, object, object]]) -> str:
    lines = [f"  {len(diffs)} field(s) changed (showing first 20):"]
    for path, g, c in diffs[:20]:
        lines.append(f"    {path}:")
        lines.append(f"      golden : {g!r}")
        lines.append(f"      current: {c!r}")
    return "\n".join(lines)


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_golden_exists_and_loads(state: str, lens: str) -> None:
    """Smoke: every (state, lens) golden parses as JSON and has records."""
    path = _GOLDEN_DIR / f"{state}_elements_{lens}.json"
    assert path.exists(), f"missing golden: {path.relative_to(_REPO_ROOT)}"
    data = _load_json(path)
    assert data["state"].lower() == state
    assert data["element_count"] == len(data["elements"])
    assert data["element_count"] > 0


@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_golden_roundtrips_through_model(state: str, lens: str) -> None:
    """Golden payload survives `StateElements` parse → dump unchanged.

    Catches shape drift — a removed field, renamed field, retyped field,
    or a changed default would make the re-dump diverge from the golden.
    """
    path = _GOLDEN_DIR / f"{state}_elements_{lens}.json"
    golden = _load_json(path)

    parsed = StateElements.model_validate({
        **golden,
        "extracted_at": "2000-01-01T00:00:00",
    })
    redumped = _strip_timestamp(json.loads(parsed.model_dump_json()))

    diffs = _diff_paths(golden, redumped, limit=20)
    assert not diffs, (
        f"Round-trip diff for {state}/{lens} "
        f"(golden vs StateElements re-dump):\n{_format_diffs(diffs)}"
    )


@pytest.mark.realdata
@pytest.mark.parametrize(("state", "lens"), _CASES)
def test_data_out_matches_golden_when_present(state: str, lens: str) -> None:
    """Drift check: if `data/out/<state>_elements_<lens>.json` is on disk
    (local dev after a fresh pipeline run), it must match the golden
    field-for-field. CI without `data/out/` skips cleanly.

    On mismatch the first 20 changed paths print so the author can tell
    whether to regenerate the golden or investigate a pipeline regression.
    """
    out_path = _DATA_OUT / f"{state}_elements_{lens}.json"
    if not out_path.exists():
        pytest.skip(f"data/out/{out_path.name} not present; skipping drift check")

    golden = _load_json(_GOLDEN_DIR / f"{state}_elements_{lens}.json")
    current = _strip_timestamp(_load_json(out_path))

    diffs = _diff_paths(golden, current, limit=20)
    assert not diffs, (
        f"Golden drift for {state}/{lens} (data/out vs tests/golden):\n"
        f"{_format_diffs(diffs)}\n"
        f"If the change is intentional, regenerate goldens "
        f"(see tests/golden/README.md)."
    )
