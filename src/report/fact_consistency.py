"""Cross-lens fact-consistency QA — Phase B commit 6.

For each state, computes the 3 spine-lens deterministic facts
(`definition_present`, `business_rules_present`, `data_type_canonical`)
on BOTH lens artifacts, joins on ``(entity, element_name)``, and
flags disagreements.

Why this matters: when a source-lens row and a spine-lens row name
the same (entity, element_name), they enrich from the same underlying
source-doc text. Their deterministic facts SHOULD be identical. Any
divergence is either a lens-plumbing bug (e.g., enrichment losing
business-rules text on one path) or a known intentional policy
difference — both worth surfacing per plan § 10.3.

Merge gate per `docs/archive/next-session/next-session-scoring-phase-b-fact-validation.md`
(commit 6): zero disagreements across all 4 states. Disagreements here
are signals, not failures by themselves — they tell us the two lenses
aren't drifting silently.

The resulting per-state summary is folded into the lens-divergence
report at render time; `run()` emits nothing on its own.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.models.element import StateElements
from src.score.deterministic import LENS_INDEPENDENT_FACTS, compute_fact

logger = logging.getLogger(__name__)


def _load_lens(state: str, lens: str, base: Path) -> StateElements:
    path = base / f"{state.lower()}_elements_{lens}.json"
    return StateElements.model_validate_json(path.read_text(encoding="utf-8"))


def compute_state(state: str, out: Path) -> dict[str, Any]:
    """Compute fact-consistency summary for a single state.

    Joins source-lens and spine-lens records on ``(entity, element_name)``.
    Only rows with ``documented=True`` in BOTH lenses contribute — an
    undocumented spine slot has no source text to cross-check, and a
    spine-missing source row has no spine counterpart at all.

    Returns::

        {
          "state": "AZ",
          "shared_documented": 380,
          "total_disagreements": 0,
          "per_fact": {
            "definition_present": {"count": 0, "samples": []},
            "business_rules_present": {"count": 0, "samples": []},
            "data_type_canonical": {"count": 0, "samples": []},
          },
        }
    """
    src = _load_lens(state, "source", out)
    spn = _load_lens(state, "spine", out)

    src_by_key = {(r.entity, r.element_name): r for r in src.elements if r.documented}
    spn_by_key = {(r.entity, r.element_name): r for r in spn.elements if r.documented}
    shared = sorted(set(src_by_key) & set(spn_by_key))

    per_fact: dict[str, dict[str, Any]] = {
        fact: {"count": 0, "samples": []} for fact in LENS_INDEPENDENT_FACTS
    }

    for key in shared:
        src_rec = src_by_key[key]
        spn_rec = spn_by_key[key]
        for fact in LENS_INDEPENDENT_FACTS:
            src_val = compute_fact(fact, src_rec)
            spn_val = compute_fact(fact, spn_rec)
            if src_val == spn_val:
                continue
            per_fact[fact]["count"] += 1
            if len(per_fact[fact]["samples"]) < 10:
                per_fact[fact]["samples"].append(
                    {
                        "entity": src_rec.entity,
                        "element_name": src_rec.element_name,
                        "source_value": src_val,
                        "spine_value": spn_val,
                    }
                )

    return {
        "state": state,
        "shared_documented": len(shared),
        "total_disagreements": sum(f["count"] for f in per_fact.values()),
        "per_fact": per_fact,
    }


def render_section(fact_consistency: list[dict[str, Any]]) -> list[str]:
    """Render the `## Fact consistency — cross-lens QA` MD section."""
    lines = [
        "## Fact consistency — cross-lens QA",
        "",
        (
            "For each state, the 3 spine-lens deterministic facts "
            "(`definition_present`, `business_rules_present`, "
            "`data_type_canonical`) are recomputed on both the source-lens "
            "and spine-lens artifacts and joined on `(entity, element_name)`. "
            "A disagreement means the two lens paths produced different "
            "enrichment for the same logical slot — signal worth "
            "investigating before relying on scoring output from either lens."
        ),
        "",
        "| State | Shared documented rows | Total disagreements |",
        "| :- | -: | -: |",
    ]
    for block in fact_consistency:
        lines.append(
            f"| {block['state']} | {block['shared_documented']:,} | "
            f"{block['total_disagreements']:,} |"
        )
    lines.append("")
    any_disagreement = any(b["total_disagreements"] > 0 for b in fact_consistency)
    if any_disagreement:
        lines.extend(["### Disagreement detail", ""])
        for block in fact_consistency:
            if block["total_disagreements"] == 0:
                continue
            lines.append(f"#### {block['state']}")
            lines.append("")
            for fact, detail in block["per_fact"].items():
                if detail["count"] == 0:
                    continue
                lines.append(f"- **{fact}**: {detail['count']} disagreements")
                for sample in detail["samples"][:5]:
                    lines.append(
                        f"  - `{sample['entity']}.{sample['element_name']}` "
                        f"source={sample['source_value']} "
                        f"spine={sample['spine_value']}"
                    )
            lines.append("")
    else:
        lines.append(
            "_All deterministic facts agree across lenses for every shared "
            "documented row — no disagreements to detail._"
        )
        lines.append("")
    return lines
