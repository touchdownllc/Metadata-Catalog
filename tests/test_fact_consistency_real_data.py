"""Cross-lens fact consistency on the committed goldens.

Scoring's deterministic `data_type_canonical` fact reads `data_type` off
each record. Source-lens and spine-lens must agree on the concrete type
for rows that represent the same (entity, element_name) logical slot —
otherwise a fact marked "canonical type missing" on the source-lens side
while the spine-lens side has a type is a silent false negative.

Surfaced 2026-04-23 by Phase B fact-consistency QA: MN had 6 rows where
source-lens `data_type=None` but spine-lens carried `Integer`/`Number`/
`Descriptor`/`String`. Root cause was `build_spine_type_index` skipping
extension `references` and `sub_collections` — `canonical_spine_emit_keys`
walks both, so spine-lens emits their types while source-lens records
(matched to the spine via aliasing) never got the type propagated.

This suite pins the invariant that matched source-lens rows (i.e. rows
with `source != "unknown"`) carry a concrete data_type whenever the
corresponding spine-lens emit does. Joins strictly on (state, entity,
element_name) — normalization + aliasing are upstream concerns.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_DIR = _REPO_ROOT / "tests" / "golden"

STATES = ("az", "wi", "mn", "tx")


def _load_records(state: str, lens: str) -> list[dict]:
    path = _GOLDEN_DIR / f"{state}_elements_{lens}.json"
    return json.loads(path.read_text(encoding="utf-8"))["elements"]


@pytest.mark.parametrize("state", STATES)
def test_matched_source_rows_have_data_type_when_spine_does(state: str) -> None:
    """For every source-lens row with `source != "unknown"`, if the same
    (entity, element_name) exists on the spine-lens side with a concrete
    `data_type`, the source-lens row must also carry a concrete type.

    Rationale: matched source rows landed on a spine slot. The spine's
    canonical type should propagate during `populate_data_types_from_spine`
    — a None on the matched-row side means the spine-type index missed
    the slot (extension-ref/extension-subcoll gap), which will then
    pollute scoring's `data_type_canonical` fact with a false miss.
    """
    spine_type_by_key = {
        (r["entity"], r["element_name"]): r.get("data_type")
        for r in _load_records(state, "spine")
        if r.get("data_type")
    }

    offenders: list[tuple[str, str, str]] = []
    for r in _load_records(state, "source"):
        if r.get("source") == "unknown":
            continue
        if r.get("data_type"):
            continue
        spine_type = spine_type_by_key.get((r["entity"], r["element_name"]))
        if spine_type:
            offenders.append((r["entity"], r["element_name"], spine_type))

    assert not offenders, (
        f"{state}: {len(offenders)} matched source-lens rows missing "
        f"data_type while spine-lens has one — first 5: "
        f"{offenders[:5]}"
    )
