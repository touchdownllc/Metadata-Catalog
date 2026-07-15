"""Lens-divergence report — cross-lens comparison per state.

Reads both `{state}_elements_source.json` and `{state}_elements_spine.json`
for each state and surfaces where the two lenses disagree:

- **Record-count delta** per state.
- **`spine_missing`**: source rows whose (entity, element) never landed on
  any canonical spine slot. These are preserved in both lenses
  (source-lens as `source="unknown"`; spine-lens as the hybrid append).
  For TX this is the ~90-row TEDS-only audit trail.
- **`undocumented`**: canonical spine slots no source record touched.
  Most of the spine-lens record volume under both 3b and pure-flip
  shapes — the "what's the gap" diagnostic.
- **`score_delta`**: placeholder section for post-scoring comparison.
  Empty until the scoring layer lands; the divergence report becomes
  the per-state-per-domain signal consumers use to decide whether to
  consolidate on one lens.

CRITICAL: module-level `run()` is a PLAIN function (POC-2 pitfall) —
do NOT decorate with `@click.command()`.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from src.ingest.domain_filter import entity_filter_domain
from src.models.element import StateElements
from src.models.spine import StateSpine
from src.report import fact_consistency
from src.states import SUPPORTED_STATES as _STATES
from src.utils.paths import divergence_path, out_dir, spine_dir
logger = logging.getLogger(__name__)


def _load_lens(
    state: str, lens: str, base: Path, *, allow_stale: bool = False
) -> StateElements:
    path = base / f"{state.lower()}_elements_{lens}.json"
    # Issue #212 item 3: with a publish lineage, stale elements must not
    # silently feed the cross-lens divergence report.
    from src.publish.manifest import verify_fresh

    verify_fresh(
        path, consumer="report divergence", allow_stale=allow_stale
    )
    return StateElements.model_validate_json(
        path.read_text(encoding="utf-8")
    )


def _load_spine(state: str, base: Path | None = None) -> StateSpine:
    base = base or spine_dir()
    spine_path = base / f"{state.lower()}_spine.json"
    return StateSpine.model_validate_json(spine_path.read_text(encoding="utf-8"))


def _build_state_block(
    state: str,
    out: Path,
    spine_base: Path | None = None,
    *,
    allow_stale: bool = False,
) -> dict:
    """Per-state divergence block.

    Layout (stable contract):

    ```
    {
      "state": "AZ",
      "record_counts": {"source": 1530, "spine": 6605, "delta": 5075},
      "buckets": {
        "spine_missing": {"count": 34, "samples": [...]},
        "undocumented":  {"count": 6208, "samples": [...]},
        "alias_miss":    {"count":   N, "samples": [...]}
      },
      "score_delta": {"scored": false, "note": "..."}
    }
    ```
    """
    from src.utils.matching import record_match_keys

    src = _load_lens(state, "source", out, allow_stale=allow_stale)
    spn = _load_lens(state, "spine", out, allow_stale=allow_stale)
    spine = _load_spine(state, base=spine_base)

    def _is_filtered(entity: str) -> bool:
        return entity_filter_domain(entity, spine) is not None

    # spine_missing: source rows that never matched the spine. These are the
    # source="unknown" rows in source-lens output. In spine-lens output,
    # they survive as the hybrid-append tail (also source="unknown") —
    # including filtered-domain rows, since the soft filter retains
    # documented intent. Don't skip filtered entities here; the documented
    # gap signal still belongs in the bucket.
    spine_missing_rows = [r for r in src.elements if r.source == "unknown"]

    # Union of every source-matched record's alias match-key set. An
    # undocumented spine row whose own alias set intersects this union was
    # "nearly matched" — its canonical slot was named by some source row but
    # under a different normalization than the spine-lens emit chose.
    source_match_keys: set[tuple[str, str]] = set()
    for r in src.elements:
        if r.source in ("core", "extension"):
            source_match_keys |= record_match_keys(r.entity, r.element_name)

    # undocumented + alias_miss surface canonical spine slots no source row
    # touched. Filtered-domain undocumented slots are exactly what the
    # placeholder collapses, so excluding them here keeps the gap signal
    # focused on domains a SIS vendor is expected to populate. Documented
    # filtered-domain rows are retained by the soft filter and won't
    # appear here regardless (`r.documented` is True).
    alias_miss_rows = []
    undocumented_rows = []
    for r in spn.elements:
        if r.documented or r.source not in ("core", "extension"):
            continue
        if _is_filtered(r.entity):
            continue
        keys = record_match_keys(r.entity, r.element_name)
        if keys & source_match_keys:
            alias_miss_rows.append(r)
        else:
            undocumented_rows.append(r)

    return {
        "state": state,
        "record_counts": {
            "source": src.element_count,
            "spine": spn.element_count,
            "delta": spn.element_count - src.element_count,
        },
        "buckets": {
            "spine_missing": {
                "description": (
                    "Source rows whose (entity, element) didn't match any "
                    "canonical spine slot. Preserved as audit trail."
                ),
                "count": len(spine_missing_rows),
                "samples": [
                    {
                        "entity": r.entity,
                        "element_name": r.element_name,
                        "data_type": r.data_type,
                        "source_document": r.source_document,
                    }
                    for r in spine_missing_rows[:20]
                ],
            },
            "undocumented": {
                "description": (
                    "Canonical spine slots the state's source doc did not "
                    "mention — no source-record alias lands on this slot. "
                    "Primary coverage-gap signal under the spine lens."
                ),
                "count": len(undocumented_rows),
                "samples": [
                    {
                        "entity": r.entity,
                        "element_name": r.element_name,
                        "data_type": r.data_type,
                        "source": r.source,
                        "extension_name": r.extension_name,
                    }
                    for r in undocumented_rows[:20]
                ],
            },
            "alias_miss": {
                "description": (
                    "Undocumented spine slots whose alias match-set DOES "
                    "intersect some source record — the source doc named a "
                    "position on the spine but under a form the canonical "
                    "emit chose differently. These are 'almost-matches' — "
                    "follow-ups for emit-layer alias expansion, distinct "
                    "from the genuine-gap undocumented signal."
                ),
                "count": len(alias_miss_rows),
                "samples": [
                    {
                        "entity": r.entity,
                        "element_name": r.element_name,
                        "data_type": r.data_type,
                        "source": r.source,
                        "extension_name": r.extension_name,
                    }
                    for r in alias_miss_rows[:20]
                ],
            },
        },
        "score_delta": {
            "scored": False,
            "note": (
                "Populated once the NACHOS scoring layer lands. "
                "Will report score delta per (state, domain) cell between "
                "source-lens and spine-lens runs; divergence is signal, "
                "not contamination (research §Given the stated drivers)."
            ),
        },
    }


def build_report(
    states: tuple[str, ...] = _STATES,
    out: Path | None = None,
    spine_base: Path | None = None,
    *,
    allow_stale: bool = False,
) -> dict:
    out = out or out_dir()
    return {
        "generated_on": date.today().isoformat(),
        "lenses": ["source", "spine"],
        "per_state": [
            _build_state_block(
                s, out, spine_base=spine_base, allow_stale=allow_stale
            )
            for s in states
        ],
        "fact_consistency": [fact_consistency.compute_state(s, out) for s in states],
        "guidance": (
            "This report surfaces where the source-driven and spine-driven "
            "lenses disagree per state. Treat divergence as signal, not "
            "contamination — the two lenses answer different questions "
            "(source: 'what the state told its vendors'; spine: 'what UDM "
            "slice the state documents'). Stakeholders use this to decide "
            "whether to consolidate on one lens or keep both permanently."
        ),
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# Lens-divergence report",
        "",
        f"Generated on: {report['generated_on']}.",
        f"Lenses compared: {', '.join(report['lenses'])}.",
        "",
        report["guidance"],
        "",
        "## Per-state counts",
        "",
        "| State | Source records | Spine records | Δ |",
        "| :- | -: | -: | -: |",
    ]
    for s in report["per_state"]:
        rc = s["record_counts"]
        lines.append(
            f"| {s['state']} | {rc['source']:,} | {rc['spine']:,} | "
            f"{rc['delta']:+,} |"
        )
    lines.extend(["", "## Buckets per state", ""])
    for s in report["per_state"]:
        lines.append(f"### {s['state']}")
        lines.append("")
        for name, bucket in s["buckets"].items():
            lines.append(f"- **{name}**: {bucket['count']:,}")
        lines.append("")
        # Score delta
        sd = s["score_delta"]
        if not sd["scored"]:
            lines.append(f"_Score delta_: not populated yet — {sd['note']}")
        lines.append("")
    lines.extend(fact_consistency.render_section(report.get("fact_consistency", [])))
    return "\n".join(lines)


def run(
    states: tuple[str, ...] = _STATES,
    out: Path | None = None,
    spine_base: Path | None = None,
    allow_stale: bool = False,
) -> dict:
    """Write lens_divergence.{json,md} to `data/out/` (or override).

    Preconditions: each state has both `{state}_elements_source.json` and
    `{state}_elements_spine.json` present. Regenerate with
    `poc3 ingest {state}` if missing.
    """
    out_path = out or out_dir()
    out_path.mkdir(parents=True, exist_ok=True)

    report = build_report(
        states, out_path, spine_base=spine_base, allow_stale=allow_stale
    )

    json_path = divergence_path("json")
    md_path = divergence_path("md")

    # If caller passed a custom out dir, respect it.
    if out is not None:
        json_path = out / "lens_divergence.json"
        md_path = out / "lens_divergence.md"

    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    logger.info("divergence: wrote %s and %s", json_path, md_path)
    return report


if __name__ == "__main__":
    run()
