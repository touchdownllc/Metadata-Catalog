"""Cross-state coverage report — aggregates per-state ingestion outputs.

Consumes `data/out/{state}_elements_source.json`, `data/out/{state}_gap_log.json`,
and `data/spine/{state}_spine.json` for AZ/WI/MN/TX and emits:

- `data/out/coverage_report.json` — machine-readable per-state + cross-state blocks.
- `data/out/coverage_report.md` — skim-friendly summary with the overlap table.

CRITICAL: module-level `run()` is a PLAIN function. Do NOT decorate it with
`@click.command()` — see `tests/test_report_coverage.py::TestCliWiring`.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from pathlib import Path

from src.models.element import StateElements
from src.models.spine import StateSpine
from src.states import SUPPORTED_STATES
from src.utils.matching import entity_match_form

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_OUT_DIR = _PROJECT_ROOT / "data" / "out"
_SPINE_DIR = _PROJECT_ROOT / "data" / "spine"

_STATES = SUPPORTED_STATES

logger = logging.getLogger(__name__)


@dataclass
class StateInputs:
    """All inputs needed to build a single state's coverage block."""

    state: str
    elements: StateElements
    gap_log: dict
    spine: StateSpine
    lens: str = "source"


def _load_state(
    state: str, lens: str = "source", *, allow_stale: bool = False
) -> StateInputs:
    """Load a state's elements + gap-log + spine under the given lens.

    lens="source" reads `{state}_elements_source.json` (today's shape —
    the artifact the source-driven pipeline emits).
    lens="spine" reads `{state}_elements_spine.json` (the spine-enumerated,
    source-enriched artifact introduced in Phase 3).

    The gap log is lens-independent (always from the source-driven run).
    """
    s = state.lower()
    # Issue #212 item 3: with a publish lineage, stale elements/gap-log
    # artifacts must not silently feed coverage percentages.
    from src.publish.manifest import verify_fresh

    for name in (f"{s}_elements_{lens}.json", f"{s}_gap_log.json"):
        verify_fresh(
            _OUT_DIR / name,
            consumer="report coverage",
            allow_stale=allow_stale,
        )
    elements = StateElements.model_validate_json(
        (_OUT_DIR / f"{s}_elements_{lens}.json").read_text(encoding="utf-8")
    )
    gap_log = json.loads((_OUT_DIR / f"{s}_gap_log.json").read_text(encoding="utf-8"))
    spine = StateSpine.model_validate_json(
        (_SPINE_DIR / f"{s}_spine.json").read_text(encoding="utf-8")
    )
    return StateInputs(
        state=state, elements=elements, gap_log=gap_log, spine=spine, lens=lens
    )


def build_state_block(si: StateInputs) -> dict:
    """Build the per-state coverage JSON block (plan §Phase 4).

    Under the source lens (default), the primary metric is
    `source.coverage_pct` — the fraction of state-declared
    (entity, element) pairs that land on a spine slot.

    Under the spine lens, the primary metric is
    `documentation.coverage_pct` — the fraction of canonical spine
    positions that the source doc covers. Numerically equivalent to
    today's `spine_coverage.coverage_pct` — just renamed and elevated.

    Issue #70 — under the source lens we also surface a dual
    documentation metric:
    - ``source_doc_coverage_pct`` — % of authored-prose rows matching
      the spine ("how clean is the authored prose?"). Unchanged by the
      swagger-backfill rows since gap_log is computed pre-backfill.
    - ``total_documented_coverage_pct`` — % of the API spine surface
      with any documentation (authored OR swagger-backfilled). The
      broader "what fraction of the API surface has any documentation?"
      reading.
    """
    records = si.elements.elements
    enrichment = {
        "with_definition_text": sum(1 for r in records if r.definition_text),
        "with_business_rules_text": sum(1 for r in records if r.business_rules_text),
        "with_regulatory_citations": sum(1 for r in records if r.regulatory_citations),
    }

    unmatched_tags: Counter[str] = Counter()
    for info in si.gap_log.get("unmatched_by_entity", {}).values():
        tag = info.get("tag", "unknown")
        unmatched_tags[tag] += len(info.get("elements", []))

    source_coverage = si.gap_log.get("source_coverage", {})
    spine_coverage = si.gap_log.get("spine_coverage", {})

    # Issue #70 — count swagger-backfilled rows separately so the dual
    # coverage metric distinguishes authored prose from swagger backfill.
    swagger_rows = sum(
        1 for r in records
        if getattr(r, "documentation_source", "source_doc") == "swagger"
    )

    block = {
        "state": si.state,
        "lens": si.lens,
        "spine": {
            "entities": si.spine.entity_count,
            "extensions": si.spine.extension_count,
            "edfi_version": si.spine.edfi_version,
        },
        "source": {
            # v21 close-out posture: ``records`` and ``matched`` are
            # derived directly from the source-lens elements artifact,
            # so the ``records == len(elements)`` invariant and
            # ``matched == count(source != 'unknown')`` invariant hold
            # over the full artifact (authored prose + swagger-
            # backfilled rows). The authored-only spine coverage is in
            # ``documentation_provenance.source_doc.coverage_pct``.
            "records": len(records),
            "matched": sum(1 for r in records if r.source != "unknown"),
            "coverage_pct": (
                round(
                    sum(1 for r in records if r.source != "unknown")
                    / len(records) * 100,
                    1,
                )
                if records
                else 0.0
            ),
            "unmatched": si.gap_log.get("unmatched_source_count", 0),
            "unflatten_recovered": si.gap_log.get("unflatten_recovered_count", 0),
        },
        "spine_coverage": {
            "matched_unique_keys": spine_coverage.get("matched_unique_keys", 0),
            "total_spine_keys": spine_coverage.get("total_spine_keys", 0),
            "coverage_pct": spine_coverage.get("pct", 0.0),
        },
        "enrichment": enrichment,
        "unmatched_breakdown": dict(unmatched_tags),
    }

    if si.lens == "source":
        # v21 close-out posture (issue #70): swagger-backfilled rows
        # surface in the source-lens artifact for visibility but are NOT
        # counted as documented for headline coverage. The headline
        # ``source_doc.coverage_pct`` matches the v19 baseline. The
        # ``swagger_rows`` count stays as a sidecar audit field so analysts
        # can see how many spine slots got surfaced via swagger; whether
        # to promote that to a coverage reading is a methodology call
        # deferred to the close-out review.
        m_authored = spine_coverage.get("matched_unique_keys", 0)
        t_spine = spine_coverage.get("total_spine_keys", 0)
        block["documentation_provenance"] = {
            "source_doc_rows": sum(
                1 for r in records
                if getattr(r, "documentation_source", "source_doc") == "source_doc"
            ),
            "swagger_rows": swagger_rows,
            "source_doc": {
                "matched_unique_keys": m_authored,
                "total_spine_keys": t_spine,
                "coverage_pct": (
                    round(m_authored / t_spine * 100, 1) if t_spine else 0.0
                ),
            },
        }

    if si.lens == "spine":
        # Under the spine lens the artifact enumerates every canonical spine
        # slot. `documented=True` counts slots the source doc touched —
        # equivalent to today's spine_coverage, elevated as the primary metric.
        # Filtered placeholder rows (SIS-never-populated domains) are excluded
        # from the documentation ratio — they're not a real scoring position.
        scored = [r for r in records if r.source != "filtered"]
        total = len(scored)
        documented = sum(1 for r in scored if r.documented)
        # Count spine-missing audit-trail appends separately.
        spine_missing = sum(1 for r in scored if r.source == "unknown")
        filtered_count = sum(1 for r in records if r.source == "filtered")
        block["documentation"] = {
            "lens_records": total,
            "documented": documented,
            "undocumented": total - documented,
            "coverage_pct": round(documented / total * 100, 1) if total else 0.0,
            "spine_missing_append_count": spine_missing,
            "filtered_domain_placeholders": filtered_count,
        }

    return block


def _spine_entity_names(spine: StateSpine) -> set[str]:
    """Normalized entity name set (core + extension targets)."""
    names = set(spine.catalog.entities.keys())
    for ext in spine.catalog.extensions.values():
        names.add(ext.extends_entity)
    return {entity_match_form(n) for n in names}


def _spine_extension_only_entities(spine: StateSpine) -> set[str]:
    """Entities introduced purely as state extensions (no core-entity presence).

    We intentionally collect extension ENTITY names (not extends_entity) so
    concrete extension entities like `mn_studentADSISProgramAssociation` are
    surfaced even when they don't extend a core entity.
    """
    core = {entity_match_form(n) for n in spine.catalog.entities}
    out: set[str] = set()
    for ext_name, ext in spine.catalog.extensions.items():
        target = entity_match_form(ext.extends_entity)
        if target not in core:
            out.add(entity_match_form(ext_name))
    return out


def _state_element_keys(spine: StateSpine) -> dict[str, set[str]]:
    """Map normalized-entity -> set of lowered element keys for that entity."""
    by_entity: dict[str, set[str]] = defaultdict(set)
    for entity, element in spine.element_keys():
        by_entity[entity_match_form(entity)].add(element.lower())
    return by_entity


def build_cross_state_block(states: list[StateInputs]) -> dict:
    """Cross-state entity overlap + element-level overlap for shared entities.

    Generic over N states (was hardcoded 3 for AZ/WI/MN; Phase 6.4 extended
    for TX). Pair combinations are produced via `itertools.combinations`
    over the input states sorted alphabetically.
    """
    state_order = [si.state for si in states]
    state_count = len(state_order)
    entity_sets: dict[str, set[str]] = {
        si.state: _spine_entity_names(si.spine) for si in states
    }
    all_entities = set().union(*entity_sets.values()) if entity_sets else set()

    presence: dict[str, set[str]] = {
        ent: {st for st, es in entity_sets.items() if ent in es}
        for ent in all_entities
    }

    core_all = sorted(e for e, st in presence.items() if len(st) == state_count)
    pair_overlap: dict[str, list[str]] = {}
    for a, b in combinations(sorted(state_order), 2):
        key = f"{a}_and_{b}_only"
        pair_overlap[key] = sorted(
            e for e, st in presence.items() if st == {a, b}
        )
    single: dict[str, list[str]] = {}
    for st in state_order:
        single[f"{st}_only"] = sorted(
            e for e, s in presence.items() if s == {st}
        )

    element_keys_by_state = {si.state: _state_element_keys(si.spine) for si in states}
    shared_element_counts: dict[str, dict[str, int]] = {}
    flagged_core_entities: list[dict] = []
    for ent in core_all:
        per_state = {st: element_keys_by_state[st].get(ent, set()) for st in state_order}
        shared = set.intersection(*per_state.values()) if per_state else set()
        entry = {
            "entity": ent,
            "shared_elements": len(shared),
            "per_state_elements": {st: len(per_state[st]) for st in state_order},
        }
        shared_element_counts[ent] = entry
        if len(shared) > 10:
            flagged_core_entities.append(entry)
    flagged_core_entities.sort(key=lambda r: r["shared_elements"], reverse=True)

    extension_only_by_state = {
        si.state: sorted(_spine_extension_only_entities(si.spine)) for si in states
    }

    return {
        "state_count": state_count,
        "states": state_order,
        "entity_presence_counts": {
            "all_states": len(core_all),
            **{k: len(v) for k, v in pair_overlap.items()},
            **{k: len(v) for k, v in single.items()},
        },
        "core_entities_all_states": core_all,
        "pair_only_entities": pair_overlap,
        "single_state_entities": single,
        "shared_element_counts": shared_element_counts,
        "flagged_core_entities": flagged_core_entities,
        "extension_only_entities": extension_only_by_state,
    }


def render_markdown(report: dict) -> str:
    """Render a skim-friendly (<25 line) summary table + overlap + flags."""
    today = date.today().isoformat()
    lines: list[str] = [f"# Coverage Report — {today}", ""]
    n_states = len(report.get("per_state", []))
    lines.append(f"_{n_states}-state report._")
    lines.append("")
    lines.append(
        "> Non-engineer reviewers: see "
        "[`docs/stakeholder-lens-preview.md`](../../docs/stakeholder-lens-preview.md) "
        "for the source-vs-spine-lens framing before reading the table below."
    )
    lines.append("")
    lines.append(
        "| State | Source coverage | Spine coverage | Doc provenance (issue #70) | Enrichment | Spine size |"
    )
    lines.append("|---|---|---|---|---|---|")
    for st_block in report["per_state"]:
        src = st_block["source"]
        spc = st_block.get("spine_coverage", {})
        enr = st_block["enrichment"]
        sp = st_block["spine"]
        prov = st_block.get("documentation_provenance")
        spc_cell = (
            f"{spc['matched_unique_keys']}/{spc['total_spine_keys']} "
            f"({spc['coverage_pct']}%)"
            if spc.get("total_spine_keys")
            else "(not available)"
        )
        if prov:
            sd = prov["source_doc"]
            swag_n = prov.get("swagger_rows", 0)
            prov_cell = (
                f"authored {sd['coverage_pct']}% · "
                f"swagger surfaced {swag_n} rows"
            )
        else:
            prov_cell = "(spine lens)"
        lines.append(
            f"| {st_block['state']} | "
            f"{src['matched']}/{src['records']} ({src['coverage_pct']}%) | "
            f"{spc_cell} | "
            f"{prov_cell} | "
            f"{enr['with_business_rules_text']} with rules, "
            f"{enr['with_definition_text']} with defs | "
            f"{sp['entities']}e/{sp['extensions']}x |"
        )
    lines.append("")
    lines.append(
        "> `Source coverage` counts MATCHED SOURCE-DOCUMENT ROWS against the "
        "total source-document row count (both sides are row counts).  \n"
        "> `Spine coverage` counts UNIQUE MATCHED SPINE KEYS against the "
        "total spine key count for the state (numerator is distinct spine "
        "positions, denominator is total spine positions). A ~4% number "
        "means the source covers a specific SEA workflow, not the full data "
        "standard — see each Score Card's `Source scope` line.  \n"
        "> `Doc provenance` (issue #70 v21 close-out) — `authored` is the "
        "source-doc-only spine coverage (\"how clean is the authored "
        "prose?\"); `swagger surfaced` is the count of swagger-only "
        "entities the methodology surfaces in the source-lens artifact "
        "for visibility but does NOT count as documented under v21. "
        "Whether to promote swagger publication to a coverage reading "
        "is a methodology call deferred to the close-out review."
    )
    lines.append("")
    lines.append("## Cross-state entity overlap")
    cs = report["cross_state"]
    cc = cs["entity_presence_counts"]
    n_states = cs.get("state_count", len(cs.get("states", [])))
    lines.append(f"- Core (all {n_states} states): {cc['all_states']} entities")
    # Pair buckets (WI_and_AZ_only, etc.) — rendered in combinations order.
    pair_keys = [k for k in cc if k.endswith("_only") and "_and_" in k]
    for key in pair_keys:
        a, b = key.removesuffix("_only").split("_and_")
        lines.append(f"- {a} ∩ {b} only: {cc[key]}")
    # Single-state buckets (XX_only, no `_and_`).
    single_keys = [
        k for k in cc if k.endswith("_only") and "_and_" not in k
    ]
    for key in single_keys:
        st = key.removesuffix("_only")
        lines.append(f"- {st}-only: {cc[key]}")
    lines.append("")

    flagged = report["cross_state"]["flagged_core_entities"]
    lines.append("## Top aligned core entities (>10 shared elements)")
    if flagged:
        for row in flagged[:10]:
            lines.append(
                f"- {row['entity']}: {row['shared_elements']} shared elements"
            )
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Flags")
    any_flag = False
    for st_block in report["per_state"]:
        enr = st_block["enrichment"]
        state = st_block["state"]
        if enr["with_business_rules_text"] == 0:
            any_flag = True
            lines.append(_business_rule_flag(state))
        if enr["with_regulatory_citations"] == 0:
            lines.append(
                f"- {state}: 0 regulatory citations — "
                "source may not embed statutory references"
            )
            any_flag = True
    if not any_flag:
        lines.append("- (none)")

    return "\n".join(lines) + "\n"


def _business_rule_flag(state: str) -> str:
    """Render the per-state business-rule flag message.

    Reviewer 1 flagged the generic "investigate ingest adapter" wording as
    misleading for AZ — AZ's source XLSX genuinely has no Business Rules
    column, so there is nothing for the ingester to investigate. Softened
    per state to reflect source capability.
    """
    if state == "AZ":
        return (
            "- AZ: 0 business rules — source XLSX does not provide a "
            "Business Rules column (expected)."
        )
    return (
        f"- {state}: 0 business-rule enrichment — investigate ingest adapter"
    )


def build_report(states: list[StateInputs], lens: str = "source") -> dict:
    return {
        "generated_on": date.today().isoformat(),
        "lens": lens,
        "per_state": [build_state_block(si) for si in states],
        "cross_state": build_cross_state_block(states),
    }


def _coverage_output_paths(out: Path, lens: str) -> tuple[Path, Path]:
    """Return (json_path, md_path) for the given lens.

    Source lens keeps today's `coverage_report.{json,md}` names for
    backwards compat. Spine lens uses `coverage_report_spine.{json,md}`.
    """
    if lens == "spine":
        return (
            out / "coverage_report_spine.json",
            out / "coverage_report_spine.md",
        )
    return (out / "coverage_report.json", out / "coverage_report.md")


def run(
    states: tuple[str, ...] = _STATES,
    out_dir: Path | None = None,
    lens: str = "source",
    allow_stale: bool = False,
) -> dict:
    """Generate coverage_report{_lens}.{json,md} in `data/out/`.

    `states` lets tests override the default AZ/WI/MN/TX set.
    `out_dir` lets tests redirect output.
    `lens` selects which per-state elements artifact to read
    (`source` → today's source-driven shape; `spine` → Phase 3
    spine-enumerated shape).
    """
    out = out_dir or _OUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    inputs = [
        _load_state(s, lens=lens, allow_stale=allow_stale) for s in states
    ]
    report = build_report(inputs, lens=lens)

    json_path, md_path = _coverage_output_paths(out, lens)
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    logger.info("coverage[%s]: wrote %s and %s", lens, json_path, md_path)
    return report
