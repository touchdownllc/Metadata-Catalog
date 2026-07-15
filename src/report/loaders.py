"""Artifact loaders + record keying for the report layer (issue #213 item 1).

The single home for "read a pipeline artifact for a workbook" policy:
every sidecar loader here goes through ``verify_fresh`` (stale/missing
with a publish lineage ⇒ refuse, ``allow_stale`` to override) and
``read_json_artifact`` (corrupt ⇒ ``ArtifactReadError``, never a silent
``{}``) — the issue #212 item-3 posture, held in ONE place instead of
six copies across ``analyst.py``.

Also the record-keying trio (``record_key`` / ``canonical_sort_key`` /
``row_number_index``) shared by the analyst and audit builders — the
``Row #`` cross-workbook address is derived here.

All loaders take explicit directories (call-time resolution). The old
``analyst._load_inputs`` read elements/spine from module-level
``_OUT_DIR``/``_SPINE_DIR`` even when ``run(out_dir=X)`` pointed scores
at ``X`` — a mixed-generation workbook seam, closed by threading
``out_dir``/``spine_dir`` through. ``analyst`` re-exports the old
private names as compat shims; new code imports from here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src.models.element import ElementRecord, StateElements
from src.models.spine import StateSpine
from src.utils.artifacts import read_json_artifact

logger = logging.getLogger(__name__)


@dataclass
class StateInputs:
    state: str
    elements: StateElements
    spine: StateSpine
    gap_log: dict = field(default_factory=dict)


def load_inputs(
    state: str,
    lens: str = "source",
    *,
    out_dir: Path,
    spine_dir: Path,
) -> StateInputs:
    """Load per-state inputs for the given lens.

    Source lens (default) reads `{state}_elements_source.json`. Spine lens
    reads `{state}_elements_spine.json` (Phase 3 dual-artifact output).
    Gap log is lens-independent.
    """
    s = state.lower()
    elements = StateElements.model_validate_json(
        (out_dir / f"{s}_elements_{lens}.json").read_text(encoding="utf-8")
    )
    spine = StateSpine.model_validate_json(
        (spine_dir / f"{s}_spine.json").read_text(encoding="utf-8")
    )
    gap_log_path = out_dir / f"{s}_gap_log.json"
    gap_log: dict = {}
    if gap_log_path.exists():
        gap_log = json.loads(gap_log_path.read_text(encoding="utf-8"))
    return StateInputs(state=state, elements=elements, spine=spine, gap_log=gap_log)


def load_scores_sidecar(
    state: str, lens: str, base: Path, *, allow_stale: bool = False
) -> dict:
    """Return a per-state score sidecar keyed by ``record_key``.

    Looks for ``{state}_scores_{lens}.json`` under ``base`` (the workbook's
    out-dir). Returns ``{}`` when no sidecar is present — Phase D
    integration is opt-in; workbooks rendered in test fixtures without
    a sidecar preserve the pre-Phase-D "cols 8-11 blank" contract
    (`tests/test_report_analyst.py:164` pins `max_column == 22` AND the
    values).

    Issue #212 item 3: this loader fills EVERY score cell in EVERY
    deliverable workbook, and it used to have no freshness gate and a
    silent corrupt-file swallow — a stale or truncated sidecar rendered
    a workbook of "unscored"/pre-drift numbers with zero warning. With
    a publish lineage a stale/missing sidecar now raises
    (``--allow-stale`` to override); a corrupt file always raises.
    """
    path = base / f"{state.lower()}_scores_{lens}.json"
    from src.publish.manifest import verify_fresh

    verify_fresh(
        path,
        consumer="report analyst (Details scoring bands)",
        allow_stale=allow_stale,
    )
    if not path.exists():
        return {}
    payload = read_json_artifact(
        path, consumer="report analyst (Details scoring bands)"
    )
    scores_by_key: dict[str, dict] = {}
    for entry in payload.get("scores", []):
        key = entry.get("record_key")
        if key:
            scores_by_key[key] = entry
    return scores_by_key


def load_gap_scores_sidecar(
    state: str, base: Path, *, allow_stale: bool = False
) -> dict:
    """Return the spine-anchored gap sidecar keyed by ``record_key``.

    Looks for ``{state}_scores_gap.json`` under ``base``. Returns ``{}``
    when absent — workbooks render the Spine Gap sheet only when both
    the gap elements artifact and the gap sidecar are present, so
    callers without a sidecar simply omit the sheet.
    """
    path = base / f"{state.lower()}_scores_gap.json"
    # Fail-loudly gate (seq 4 PR B): with a publish lineage, a stale or
    # tracked-but-missing gap sidecar raises instead of silently
    # omitting/mis-rendering the gap surface. No manifest → legacy.
    from src.publish.manifest import verify_fresh

    verify_fresh(
        path,
        consumer="report analyst (API Model Gaps sheet)",
        allow_stale=allow_stale,
    )
    if not path.exists():
        return {}
    payload = read_json_artifact(
        path, consumer="report analyst (API Model Gaps sheet)"
    )
    scores_by_key: dict[str, dict] = {}
    for entry in payload.get("scores", []):
        key = entry.get("record_key")
        if key:
            scores_by_key[key] = entry
    return scores_by_key


def load_gap_metadata(
    state: str, base: Path, *, allow_stale: bool = False
) -> dict[str, dict]:
    """Return per-record gap metadata keyed by ``{state}|entity|element_name``.

    Reads ``{state}_elements_gap.json`` and rebuilds a record-keyed
    lookup so the Spine Gap workbook sheet can join surfacer fields
    (``discovery`` / ``spine_data_type`` / ``spine_extension_name``)
    against the score sidecar without re-traversing the spine catalog.
    """
    path = base / f"{state.lower()}_elements_gap.json"
    from src.publish.manifest import verify_fresh

    verify_fresh(
        path,
        consumer="report analyst (API Model Gaps sheet)",
        allow_stale=allow_stale,
    )
    if not path.exists():
        return {}
    payload = read_json_artifact(
        path, consumer="report analyst (API Model Gaps sheet)"
    )
    out: dict[str, dict] = {}
    for g in payload.get("gaps", []):
        entity = g.get("entity")
        elem = g.get("element_name")
        if not entity or not elem:
            continue
        out[f"{state.upper()}|{entity}|{elem}"] = g
    return out


def load_review_queue_routes(
    lens: str, base: Path, *, allow_stale: bool = False
) -> dict[str, str]:
    """Return ``record_key → route`` for the lens's review-queue artifact.

    Returns ``{}`` when ``review_queue_{lens}.json`` is not on disk
    (test fixtures, pre-Phase-D runs); the Recommendations sheet then
    writes ``"—"`` in the Review Route column. With a publish lineage a
    stale/missing artifact raises; a corrupt file always raises (issue
    #212 item 3 — the sheet used to quietly print ``—`` routes).
    """
    path = base / f"review_queue_{lens}.json"
    from src.publish.manifest import verify_fresh

    verify_fresh(
        path,
        consumer="report analyst (Review Route column)",
        allow_stale=allow_stale,
    )
    if not path.exists():
        return {}
    payload = read_json_artifact(
        path, consumer="report analyst (Review Route column)"
    )
    out: dict[str, str] = {}
    for entry in payload.get("entries", []) or []:
        key = entry.get("record_key")
        route = entry.get("route")
        if key and route:
            out[key] = route
    return out


def load_peer_gap_artifact(path: Path) -> list[dict]:
    """Load slot rows from a peer-gap JSONL artifact.

    The artifact's first line is a run-header (``__type == "header"``);
    remaining lines are one per slot. Returns slot rows only. Missing
    file → empty list; the caller logs + skips the sheet.
    """
    if not path.exists():
        return []
    slots: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("__type") == "header":
                    continue
                slots.append(rec)
    except OSError:
        return []
    return slots


# --- record keying + the canonical row order --------------------------------


def record_key(state: str, record: ElementRecord) -> str:
    """Mirror the key shape the score sidecar writes."""
    return f"{state.upper()}|{record.entity}|{record.element_name}"


# Sorts after every printable character — used so blank Source Area
# (swagger-backfill rows carry `domain=""` post-#184) lands LAST, not first.
_SORT_LAST = "￿"


def canonical_sort_key(state: str, record: ElementRecord) -> tuple:
    """Shared row order for Reviewer View / Scoring Summary / Audit Trail.

    Documented (authored-prose) rows sort first, then Source Area with
    blanks last (swagger/swagger_leaf rows carry ``domain=""``), then
    entity, element. One shared key means the three sheets agree
    row-for-row (Scoring Summary skips unscored rows — its gaps in the
    `Row #` sequence are the cross-reference feature) and the workbook
    opens on documented rows instead of swagger backfill.
    """
    return (
        state,
        0 if record.documented else 1,
        record.domain or _SORT_LAST,
        record.entity,
        record.element_name,
    )


def row_number_index(state_inputs: list[StateInputs]) -> dict[str, int]:
    """Stable ``Row #`` per record: 1-based position under the canonical
    sort across ALL workbook rows, keyed by ``record_key``.

    Computed once per workbook so the same record carries the same number
    on every sheet (including the spine-lens "Documented only" subset).
    Rendered at write time as a leading column — deliberately NOT part of
    `_DETAILS_HEADERS`, so `human_score_backfill._MC_HEADERS` (derived
    from that tuple) never grows a meaningless ``ai-Row #`` column.

    Raises when record keys collide across the row pool — a silent
    last-wins index would mis-target the Review Queue / Commitment
    Tracker hyperlinks (issue #213 item 1 pin; ingest dedup means this
    never fires on production artifacts).
    """
    pairs = [
        (si.state, r) for si in state_inputs for r in si.elements.elements
    ]
    pairs.sort(key=lambda t: canonical_sort_key(t[0], t[1]))
    index = {record_key(st, r): i for i, (st, r) in enumerate(pairs, start=1)}
    if len(index) != len(pairs):
        raise ValueError(
            f"duplicate record keys collapse the Row # index: "
            f"{len(pairs)} rows -> {len(index)} keys — hyperlink targets "
            "would silently mis-address; dedup the element artifact first"
        )
    return index


# --- StateInputs display formatters (Update Log / Score Card) ---------------


def display_source_url(url: str | None) -> str:
    """Render a spine source URL for the Score Card.

    TX fetches from the local TSDS Vendor SDK Docker stack (no public
    sandbox exists — see CLAUDE.md "Source URLs"), so its swagger URLs
    are genuinely `http://localhost:26030/...`. Annotate rather than
    suppress: the URL stays reproducible for operators while stakeholders
    reading the workbook see why it isn't a public endpoint.
    """
    if not url:
        return ""
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host in ("localhost", "127.0.0.1"):
        return (
            f"{url} — local TSDS Vendor SDK stack "
            "(not a public endpoint; see infra/tsds-sdk)"
        )
    return url


def format_source_coverage(si: StateInputs) -> str:
    """Render `matched/total (pct%)` from the gap log's `source_coverage`
    block. Falls back to element count when gap_log is absent (tests)."""
    sc = (si.gap_log or {}).get("source_coverage") or {}
    matched = sc.get("matched")
    total = sc.get("total")
    pct = sc.get("pct")
    if matched is None or total is None:
        return f"{si.elements.element_count} ingested rows (gap-log unavailable)"
    return f"{matched}/{total} ({pct}%)"


def format_spine_coverage(si: StateInputs) -> str:
    """Render `matched_unique_keys/total_spine_keys (pct%)` — the analyst-
    facing "how much of the full Ed-Fi UDM this catalog represents" metric."""
    sp = (si.gap_log or {}).get("spine_coverage") or {}
    matched = sp.get("matched_unique_keys")
    total = sp.get("total_spine_keys")
    pct = sp.get("pct")
    if matched is None or total is None:
        return "(gap-log unavailable)"
    return f"{matched}/{total} ({pct}%)"
