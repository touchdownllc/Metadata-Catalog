"""Phase E — joiner + classifier for reviewer vs POC-3 NACHOS scoring.

Joins ``ReviewerRecord``s (from ``review_loader``) against per-state
POC-3 sidecars (``data/out/{state}_scores_{lens}.json``) via the
reviewer-side keymap (``review_keymap``). Emits per-row comparison
dicts with a ``classification`` bucket for the digest.

**Human-scored framing, not ground truth.** The classifier describes
where reviewer and POC-3 diverge; it does NOT label either side as
correct. A ``tier_delta_ge2`` row is a *disagreement pattern*, not a
"POC-3 was wrong". Bucket labels and digest copy must uphold this
(see ``docs/archive/next-session/next-session-phase-e.md``).

CRITICAL: ``run()`` is a plain function; the Click wrapper lives in
``src/poc3/cli.py``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from src.score.review_keymap import (
    SpineIndex,
    build_poc3_lookup,
    reviewer_key_to_poc3_key,
)
from src.score.review_loader import ReviewerRecord
from src.states import SUPPORTED_STATES
from src.utils.paths import (
    state_elements_gap_path,
    state_scores_gap_path,
    state_scores_path,
)

_LOGGER = logging.getLogger(__name__)


Classification = Literal[
    "match_exact",
    "match_tier",
    "tier_delta_1",
    "tier_delta_ge2",
    "key_sever_override",
    "gap_row_match",
    "no_poc3_row",
    "no_reviewer_row",
]
# v12 (2026-04-28, issue #66 Layer 2) — ``gap_row_match`` added. A reviewer
# row that lands on a spine-anchored gap entry (``{state}_elements_gap.json``)
# instead of a sidecar score is recovered into this bucket rather than
# bucketed as ``no_poc3_row``. Layer 2 was audit-only — gap rows had no
# NACHOS score yet — so ``gap_row_match`` rows had no tier/adj.
#
# Layer 3 (2026-04-29, issue #73) — gap sidecars now carry full NACHOS
# scoring. ``compare_state`` accepts an optional ``gap_scores`` index and
# ``run()`` loads it from disk per state, populating ``poc3_tier``,
# ``poc3_adj``, ``poc3_in_scope``, plus ``tier_delta`` / ``adj_delta`` on
# gap_row_match rows whenever the canonical ``{STATE}|{entity}|{element}``
# resolves in the gap sidecar. Classification stays ``gap_row_match`` —
# the bucket label describes the lookup surface, not the score detail.
# When the gap sidecar is absent, behaviour falls back to the Layer 2
# audit-only shape (synthetic ``::gap`` record_key, None tier/adj).
# v10 (2026-04-26) — ``in_scope_mismatch`` retired per methodology scope
# rectification. POC-3 in_scope is now uniformly True so the bucket
# would fire on every reviewer tier-0 row and flood the digest.


# Reviewer-introduced ceilings in TX CourseTranscriptExt: adj=3 despite
# tier=0 with justification "severed by the key change ... Highest
# complexity value given this limitation." These don't map to POC-3's
# algorithmic arithmetic — flag as a distinct bucket per brief §4.
_KEY_SEVER_MARKERS = re.compile(r"\bkey\s+change|severed\b", re.IGNORECASE)


@dataclass(frozen=True)
class ComparisonRow:
    """One joined (reviewer, POC-3) pair after classification."""

    state: str
    lens: str
    classification: Classification

    # Reviewer side — may be None when the POC-3 side has no reviewer
    # counterpart (``no_reviewer_row`` bucket).
    reviewer_entity: str | None
    reviewer_element: str | None
    reviewer_tier: int | None
    reviewer_adj: float | None
    reviewer_justification: str | None

    # POC-3 side — may be None when reviewer references a row POC-3
    # doesn't materialize (``no_poc3_row`` bucket).
    poc3_record_key: str | None
    poc3_tier: int | None
    poc3_adj: float | None
    poc3_justification: str | None
    poc3_in_scope: bool | None

    # Deltas — None when either side is missing. Computed as
    # reviewer - POC-3 so a positive delta means reviewer scored higher.
    tier_delta: int | None
    adj_delta: float | None


def _reviewer_has_key_sever(justification: str | None) -> bool:
    if not justification:
        return False
    return bool(_KEY_SEVER_MARKERS.search(justification))


def _classify_joined(
    reviewer: ReviewerRecord,
    poc3_tier: int | None,
    poc3_adj: float | None,
) -> Classification:
    """Decide the classification bucket for a row with both sides present.

    Decision order (most specific → least specific):

    1. ``key_sever_override`` — reviewer justification mentions key-change
       /severance. Precedes the tier-delta buckets so the TX pattern is
       recognizable even when the arithmetic mismatch is small.
    2. Tier equality + adj equality → ``match_exact``.
    3. Tier equality + adj mismatch → ``match_tier``.
    4. ``|tier_delta| == 1`` → ``tier_delta_1``.
    5. Otherwise ``tier_delta_ge2``.

    v10 (2026-04-26) — ``in_scope_mismatch`` removed. Methodology scope
    rectification makes POC-3 ``in_scope`` uniformly True, so the prior
    classification (which compared poc3 ``in_scope`` against a reviewer
    heuristic ``adjusted != 0``) would have fired on every reviewer
    tier-0 row and flooded the digest with noise. Tier-0 vs tier-0 now
    falls through to ``match_exact``, which is correct.

    Note: when either tier is ``None`` (reviewer cell blank), the row
    falls through to ``tier_delta_ge2`` — these are rare in the real
    file (fill rate is 100% on WI/MN/TX per the brief) but we handle
    them rather than raise.
    """
    if _reviewer_has_key_sever(reviewer.justification):
        return "key_sever_override"

    r_tier = reviewer.nachos_score
    r_adj = reviewer.adjusted_nachos_score

    if r_tier is None or poc3_tier is None:
        return "tier_delta_ge2"

    if r_tier == poc3_tier:
        # Treat floats equal when within 0.01 to sidestep float noise —
        # both sides only emit 0.5-multiples so this is conservative.
        if (
            r_adj is not None
            and poc3_adj is not None
            and abs(r_adj - poc3_adj) < 0.01
        ):
            return "match_exact"
        if r_adj is None and poc3_adj is None:
            return "match_exact"
        return "match_tier"

    if abs(r_tier - poc3_tier) == 1:
        return "tier_delta_1"
    return "tier_delta_ge2"


def _scores_index(scores: Iterable[dict]) -> dict[str, dict]:
    """Map ``record_key`` → score dict for O(1) join lookup."""
    return {entry["record_key"]: entry for entry in scores if entry.get("record_key")}


def load_gap_scores(
    state: str, sidecar_dir: Path | None = None, *, allow_stale: bool = False
) -> dict[str, dict]:
    """Build ``record_key → gap-sidecar score dict`` for one state.

    Reads ``data/out/{state}_scores_gap.json`` (or the corresponding file
    under ``sidecar_dir`` for tests) and returns the same shape
    ``_scores_index`` produces for source/spine sidecars. When the gap
    sidecar is absent, returns ``{}`` so the comparator degrades to
    pre-Layer-3 behavior (gap_row_match rows with ``poc3_tier=None``).

    Layer 3 (issue #73) — gap sidecars now carry full NACHOS scoring,
    so reviewer rows that classify as ``gap_row_match`` can pivot from
    ``poc3_tier=None`` to populated tier/adj/delta detail. The classification
    bucket itself stays ``gap_row_match`` — the new fields are additive
    per-row score detail, not a re-bucketing.
    """
    if sidecar_dir is not None:
        path = sidecar_dir / f"{state.lower()}_scores_gap.json"
    else:
        path = state_scores_gap_path(state.upper())
    # Fail-loudly gate (seq 4 PR B): a stale gap sidecar silently
    # inflated the spine reviewer-comparison match rate 88.7% vs the
    # honest 74.4% (PR #182). With a publish lineage, staleness raises;
    # without a manifest this is exactly the legacy degrade below.
    from src.publish.manifest import verify_fresh

    verify_fresh(
        path,
        consumer="reviewer comparison (gap_row_match resolution)",
        allow_stale=allow_stale,
    )
    if not path.exists():
        _LOGGER.info(
            "gap sidecar %s absent — gap_row_match rows will keep "
            "poc3_tier=None for state %s",
            path, state,
        )
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _scores_index(payload.get("scores", []))


def load_gap_lookup(
    state: str, gap_dir: Path | None = None, *, allow_stale: bool = False
) -> dict[tuple[str, str], dict]:
    """Build ``(normalized_entity, lowered_element_alias) → gap record`` from disk.

    Same shape as ``build_poc3_lookup`` so the reviewer keymap can resolve
    against gap rows the same way it resolves against sidecar rows.
    Gracefully returns ``{}`` when the gap artifact has not been generated
    yet (``state_elements_gap_path`` absent) — the comparison degrades to
    pre-Layer-2 behavior in that case. ``gap_dir`` override is for tests.
    """
    from src.utils.matching import element_aliases, entity_match_form

    if gap_dir is not None:
        gap_path = gap_dir / f"{state.lower()}_elements_gap.json"
    else:
        gap_path = state_elements_gap_path(state.upper())
    # Fail-loudly gate — see load_gap_scores (the PR #182 class).
    from src.publish.manifest import verify_fresh

    verify_fresh(
        gap_path,
        consumer="reviewer comparison (gap_row_match resolution)",
        allow_stale=allow_stale,
    )
    if not gap_path.exists():
        _LOGGER.info(
            "gap artifact %s absent — skipping gap_row_match classification "
            "for state %s",
            gap_path, state,
        )
        return {}
    payload = json.loads(gap_path.read_text(encoding="utf-8"))
    lookup: dict[tuple[str, str], dict] = {}

    def _register(ent_n: str, name: str, gap: dict) -> None:
        lookup.setdefault((ent_n, name.lower()), gap)
        for alias in element_aliases(name):
            lookup.setdefault((ent_n, alias.lower()), gap)
        # Descriptor-strip variant — reviewer rows commonly write the bare
        # noun (``Sex``, ``GradeLevel``, ``ExitWithdrawType``) where the
        # spine names the property with a ``Descriptor`` suffix.
        if name.endswith("Descriptor") and len(name) > len("Descriptor"):
            bare = name[: -len("Descriptor")]
            lookup.setdefault((ent_n, bare.lower()), gap)
            for alias in element_aliases(bare):
                lookup.setdefault((ent_n, alias.lower()), gap)

    for gap in payload.get("gaps", []):
        entity = gap.get("entity") or ""
        element = gap.get("element_name") or ""
        if not entity or not element:
            continue
        ent_n = entity_match_form(entity)
        _register(ent_n, element, gap)
        # Sub-collection gaps store ``element_name`` as the concat form
        # (``graduationSetDiplomaTypeDescriptor``); reviewer rows often
        # name only the leaf (``DiplomaType``). Register the bare leaf
        # and Descriptor-stripped variant so reviewer candidates that
        # omit the sub-collection prefix still resolve.
        leaf = gap.get("leaf_name")
        if leaf:
            _register(ent_n, leaf, gap)
    return lookup


def _gap_record_key(state: str, gap: dict) -> str:
    """Synthesize a record_key for a gap row, mirroring sidecar conventions.

    Format: ``{STATE}|{entity}|{element_name}::gap`` — the ``::gap`` suffix
    flags Layer 2 audit-only rows so any sidecar consumer that happens to
    receive a gap key fails loud rather than silently misinterpreting it
    as a scored row.
    """
    return f"{state.upper()}|{gap.get('entity')}|{gap.get('element_name')}::gap"


def _gap_lookup_resolve(
    entity: str, element: str, gap_lookup: dict[tuple[str, str], dict]
) -> dict | None:
    """Try to resolve a reviewer (entity, element) pair against the gap lookup.

    Mirrors ``reviewer_key_to_poc3_key`` — same reviewer-side candidate
    generation so a reviewer row's ``tx_studentApplication / School.SchoolId``
    resolves to the gap entry written for the spine's
    ``StudentApplication / School`` (or whichever form the surfacer emitted).
    """
    from src.score.review_keymap import reviewer_element_candidates
    from src.utils.matching import entity_match_form

    if not entity or not element or not gap_lookup:
        return None
    ent_n = entity_match_form(entity.strip())
    for cand in reviewer_element_candidates(element.strip()):
        hit = gap_lookup.get((ent_n, cand))
        if hit is not None:
            return hit
    return None


def compare_state(
    state: str,
    lens: str,
    reviewer_records: list[ReviewerRecord],
    sidecar_scores: list[dict],
    gap_lookup: dict[tuple[str, str], dict] | None = None,
    gap_scores: dict[str, dict] | None = None,
    spine: SpineIndex | None = None,
) -> list[ComparisonRow]:
    """Join reviewer rows for one state against one POC-3 lens sidecar.

    Emits:
    - One row per reviewer record (matched or ``no_poc3_row``).
    - Plus one ``no_reviewer_row`` row for every POC-3 in-scope record
      that no reviewer row landed on — surfaces coverage the reviewer
      didn't touch (e.g., MN extension-driven rows the reviewer file
      skipped).

    Only POC-3 records with ``adjusted_nachos_score > 0`` surface as
    ``no_reviewer_row``. Pre-v10 this filter used the ``in_scope``
    boolean; with v10's uniformly-True in_scope, the equivalent
    semantic is "rows the reviewer would care about" = nonzero
    adjusted score. Tier-0 / adj-0 rows are the methodology floor and
    both sides agree by construction (reviewer also lands on tier 0
    for plain attribute fields) — surfacing them would flood the
    digest with no-information rows.
    """
    state_u = state.upper()
    lookup = build_poc3_lookup(sidecar_scores, state=state_u)
    by_key = _scores_index(sidecar_scores)
    gap_lookup = gap_lookup or {}
    gap_scores = gap_scores or {}

    matched_poc3_keys: set[str] = set()
    rows: list[ComparisonRow] = []

    for rec in reviewer_records:
        if rec.state != state_u:
            continue
        poc3_key = reviewer_key_to_poc3_key(rec.entity, rec.element, lookup, spine=spine)
        if poc3_key is None:
            # Pre-Layer-2 this would fall through to ``no_poc3_row``. Now
            # we first check the spine-anchored gap artifact: if the
            # reviewer row resolves there, classify as ``gap_row_match``
            # so analysts can distinguish "spine-known but source-silent"
            # rows (recoverable) from genuine reviewer-vs-source
            # divergence (R2 residual).
            gap_hit = _gap_lookup_resolve(rec.entity, rec.element, gap_lookup)
            if gap_hit is not None:
                # Layer 3 (issue #73) — gap sidecar carries full NACHOS
                # scoring, so populate tier/adj/in_scope/justification +
                # deltas when the canonical record_key resolves in
                # ``gap_scores``. Falls back to Layer-2 audit-only shape
                # (synthetic ``::gap`` key, None tier/adj) when the gap
                # sidecar is absent or the row didn't make it into the
                # scored set. Classification stays ``gap_row_match`` either
                # way — the bucket label describes the lookup surface, not
                # the score detail.
                canonical_key = (
                    f"{state_u}|{gap_hit.get('entity')}|"
                    f"{gap_hit.get('element_name')}"
                )
                gap_score = gap_scores.get(canonical_key)
                if gap_score is not None:
                    nachos_dim = (
                        gap_score.get("dimensions", {}).get("nachos_score", {}) or {}
                    )
                    poc3_tier = nachos_dim.get("value")
                    poc3_adj = gap_score.get("adjusted_nachos_score")
                    poc3_in_scope = gap_score.get("in_scope")
                    poc3_justification = (
                        f"gap_row[{gap_hit.get('discovery')}] "
                        f"{gap_score.get('nachos_justification') or ''}"
                    ).strip()
                    tier_delta = (
                        rec.nachos_score - poc3_tier
                        if rec.nachos_score is not None and poc3_tier is not None
                        else None
                    )
                    adj_delta = (
                        rec.adjusted_nachos_score - poc3_adj
                        if rec.adjusted_nachos_score is not None
                        and poc3_adj is not None
                        else None
                    )
                    poc3_record_key = canonical_key
                else:
                    poc3_tier = None
                    poc3_adj = None
                    poc3_in_scope = None
                    poc3_justification = f"gap_row[{gap_hit.get('discovery')}]"
                    tier_delta = None
                    adj_delta = None
                    poc3_record_key = _gap_record_key(state_u, gap_hit)
                rows.append(
                    ComparisonRow(
                        state=state_u,
                        lens=lens,
                        classification="gap_row_match",
                        reviewer_entity=rec.entity,
                        reviewer_element=rec.element,
                        reviewer_tier=rec.nachos_score,
                        reviewer_adj=rec.adjusted_nachos_score,
                        reviewer_justification=rec.justification,
                        poc3_record_key=poc3_record_key,
                        poc3_tier=poc3_tier,
                        poc3_adj=poc3_adj,
                        poc3_justification=poc3_justification,
                        poc3_in_scope=poc3_in_scope,
                        tier_delta=tier_delta,
                        adj_delta=adj_delta,
                    )
                )
                continue
            rows.append(
                ComparisonRow(
                    state=state_u,
                    lens=lens,
                    classification="no_poc3_row",
                    reviewer_entity=rec.entity,
                    reviewer_element=rec.element,
                    reviewer_tier=rec.nachos_score,
                    reviewer_adj=rec.adjusted_nachos_score,
                    reviewer_justification=rec.justification,
                    poc3_record_key=None,
                    poc3_tier=None,
                    poc3_adj=None,
                    poc3_justification=None,
                    poc3_in_scope=None,
                    tier_delta=None,
                    adj_delta=None,
                )
            )
            continue

        matched_poc3_keys.add(poc3_key)
        poc3 = by_key[poc3_key]
        nachos_dim = src.get("dimensions", {}).get("nachos_score", {}) or {}
        poc3_tier = nachos_dim.get("value")
        poc3_adj = src.get("adjusted_nachos_score")
        poc3_in_scope = src.get("in_scope")
        poc3_justification = src.get("nachos_justification")

        classification = _classify_joined(rec, poc3_tier, poc3_adj)

        tier_delta = (
            rec.nachos_score - poc3_tier
            if rec.nachos_score is not None and poc3_tier is not None
            else None
        )
        adj_delta = (
            rec.adjusted_nachos_score - poc3_adj
            if rec.adjusted_nachos_score is not None and poc3_adj is not None
            else None
        )

        rows.append(
            ComparisonRow(
                state=state_u,
                lens=lens,
                classification=classification,
                reviewer_entity=rec.entity,
                reviewer_element=rec.element,
                reviewer_tier=rec.nachos_score,
                reviewer_adj=rec.adjusted_nachos_score,
                reviewer_justification=rec.justification,
                poc3_record_key=poc3_key,
                poc3_tier=poc3_tier,
                poc3_adj=poc3_adj,
                poc3_justification=poc3_justification,
                poc3_in_scope=poc3_in_scope,
                tier_delta=tier_delta,
                adj_delta=adj_delta,
            )
        )

    # no_reviewer_row surface: POC-3 rows the reviewer missed that
    # carry a nonzero adjusted score (the cohort the reviewer would
    # have engaged with). Pre-v10 this filtered on `in_scope`; with
    # v10's uniformly-True in_scope, `adjusted_nachos_score > 0` is
    # the equivalent semantic.
    for entry in sidecar_scores:
        key = entry.get("record_key")
        if not key or key in matched_poc3_keys:
            continue
        adj = entry.get("adjusted_nachos_score") or 0
        if adj <= 0:
            continue
        parts = key.split("|", 2)
        entity = parts[1] if len(parts) >= 2 else ""
        element = parts[2] if len(parts) >= 3 else ""
        nachos_dim = entry.get("dimensions", {}).get("nachos_score", {}) or {}
        rows.append(
            ComparisonRow(
                state=state_u,
                lens=lens,
                classification="no_reviewer_row",
                reviewer_entity=None,
                reviewer_element=None,
                reviewer_tier=None,
                reviewer_adj=None,
                reviewer_justification=None,
                poc3_record_key=key,
                poc3_tier=nachos_dim.get("value"),
                poc3_adj=entry.get("adjusted_nachos_score"),
                poc3_justification=entry.get("nachos_justification"),
                poc3_in_scope=entry.get("in_scope"),
                tier_delta=None,
                adj_delta=None,
            )
        )

    return rows


def load_sidecar(state: str, lens: str, path: Path | None = None) -> list[dict]:
    """Load the scores array from a ``{state}_scores_{lens}.json`` sidecar.

    ``path`` override is for tests; default reads from
    ``data/out/`` via ``utils.paths``.
    """
    target = path or state_scores_path(state, lens)  # type: ignore[arg-type]
    if not target.exists():
        raise FileNotFoundError(
            f"sidecar not found: {target} — run `poc3 score aggregate` first."
        )
    payload = json.loads(target.read_text(encoding="utf-8"))
    return payload.get("scores", [])


def run(
    *,
    reviewer_records: list[ReviewerRecord],
    lens: str,
    states: Iterable[str] = SUPPORTED_STATES,
    sidecar_dir: Path | None = None,
    allow_stale: bool = False,
) -> list[ComparisonRow]:
    """Run the full comparison across states for one lens.

    ``sidecar_dir`` override lets tests point at a tmp directory;
    default uses the committed ``utils.paths`` helper.
    """
    if lens not in ("source", "spine"):
        raise ValueError(f"unknown lens {lens!r}; supported: 'source', 'spine'")

    all_rows: list[ComparisonRow] = []
    for state in states:
        sidecar_path = None
        if sidecar_dir is not None:
            sidecar_path = sidecar_dir / f"{state.lower()}_scores_{lens}.json"
        scores = load_sidecar(state, lens, path=sidecar_path)
        # Layer 2 + 3 (issue #66 / #73) — gap lookup recovers reviewer
        # rows that don't resolve to source/spine sidecars but DO match a
        # spine-anchored gap row. Gap scores (Layer 3) populate per-row
        # tier/adj detail when available; both lookups gracefully degrade
        # to ``{}`` when their artifacts are absent.
        gap_lookup = load_gap_lookup(
            state, gap_dir=sidecar_dir, allow_stale=allow_stale
        )
        gap_scores = load_gap_scores(
            state, sidecar_dir=sidecar_dir, allow_stale=allow_stale
        )
        # Issue #61 — spine FK-nav resolution. Reviewer rows like
        # ``staffSectionAssociation | section.schoolId`` enumerate
        # dotted-FK-nav paths the source-lens catches via its
        # ``{first}Reference`` column convention but spine-lens flattens
        # away. Loading the SpineIndex lets the keymap follow the
        # reference graph to the FK target's documented row. Source-lens
        # already resolves these via the Reference-column rule, so
        # passing the spine on both lenses is a no-op for source — kept
        # symmetric for clarity.
        spine = SpineIndex.from_state(state)
        rows = compare_state(
            state,
            lens,
            reviewer_records,
            scores,
            gap_lookup=gap_lookup,
            gap_scores=gap_scores,
            spine=spine,
        )
        all_rows.extend(rows)
        _LOGGER.info(
            "compared %s %s: %d rows (reviewer+POC-3 joined + no-poc3 + no-reviewer)",
            state,
            lens,
            len(rows),
        )
    return all_rows


def classification_counts(rows: list[ComparisonRow]) -> dict[str, int]:
    """Aggregate classification counts across rows (any state/lens split).

    Callers bucket rows first (by state or lens) before calling when
    they need per-group totals.
    """
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.classification] = counts.get(row.classification, 0) + 1
    return counts


def match_rate(rows: list[ComparisonRow]) -> tuple[int, int, float]:
    """Return ``(matched, reviewer_total, pct)`` for a slice of rows.

    Matched = any reviewer row that found a POC-3 counterpart (i.e.,
    every classification except ``no_poc3_row`` and
    ``no_reviewer_row``). The denominator is reviewer-originated rows
    only — ``no_reviewer_row`` entries don't count against the
    reviewer's coverage.
    """
    reviewer_rows = [r for r in rows if r.classification != "no_reviewer_row"]
    matched = sum(1 for r in reviewer_rows if r.classification != "no_poc3_row")
    total = len(reviewer_rows)
    pct = (matched / total * 100.0) if total else 0.0
    return matched, total, round(pct, 1)
