"""Reviewer-comparison summary doc generator (Phase E synthesis).

Reads ``data/out/review_digest_{source,spine}.json`` (produced by
``mc report review-digest``) plus a sample sidecar header for the
``scoring_plan_version`` stamp, and emits a stakeholder-facing markdown
doc to ``docs/reviewer-comparison.md`` (or a path the caller picks).

The doc is intentionally lean: counts, classification breakdowns,
gap_row_match Layer-3 detail, and top divergence patterns. Editorial
commentary (F1 / F2 / F3 narrative status, methodology calls,
disclosure framing) lives in ``docs/recommendations.md`` §6 / §7 — this
module references those sections rather than duplicating them, so a
version bump that mechanically rotates the numbers does not require
re-authoring narrative text.

Usage discipline (CLAUDE.md operator playbook):

- Refresh whenever ``SCORING_PLAN_VERSION`` bumps (or whenever the
  source/spine sidecars are re-aggregated). Sequence is ``score
  aggregate`` → ``report review-digest --lens source`` and ``--lens
  spine`` → ``report reviewer-comparison``.
- Commit the regenerated doc on the same PR that bumps the plan
  version, alongside CLAUDE.md status pointer + working-journal entry.

CRITICAL: ``run()`` is a plain function; the Click wrapper lives in
``src/cli.py``.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.states import SUPPORTED_STATES
from src.utils.paths import out_dir, project_root, state_scores_path

_LOGGER = logging.getLogger(__name__)


# Order in which classification buckets are listed across the per-state
# tables — agreement first, then escalating divergence, then join-shape.
_BUCKET_ORDER: tuple[str, ...] = (
    "match_exact",
    "match_tier",
    "tier_delta_1",
    "tier_delta_ge2",
    "key_sever_override",
    "gap_row_match",
    "no_mc_row",
    "no_reviewer_row",
)

# Lenses we render. Both source and spine ship review_digest_*.json
# from the existing pipeline; the doc lays them side by side.
_LENSES: tuple[str, ...] = ("source", "spine")


def _load_digest(lens: str, *, base: Path | None = None) -> dict[str, Any]:
    target = (base or out_dir()) / f"review_digest_{lens}.json"
    if not target.exists():
        raise FileNotFoundError(
            f"review digest missing: {target} — run "
            f"`mc report review-digest --lens {lens}` first."
        )
    return json.loads(target.read_text(encoding="utf-8"))


def _detect_plan_version(states: Iterable[str], lens: str) -> str | None:
    """Read one sidecar header per state to confirm they all agree on the
    plan version. Returns the version when consensus, ``None`` when the
    sidecars are missing or disagree (caller renders ``unknown``)."""
    versions: set[str] = set()
    for state in states:
        path = state_scores_path(state, lens)  # type: ignore[arg-type]
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        v = payload.get("scoring_plan_version")
        if v is not None:
            versions.add(str(v))
    if len(versions) == 1:
        return next(iter(versions))
    return None


def _gap_row_stats(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Layer-3 detail on the ``gap_row_match`` cohort.

    Returns counts (total, populated tier, exact agreement),
    |tier_delta| distribution, and a per-state breakdown.
    ``populated_tier`` should equal ``total`` on a fully-refreshed
    pipeline — when it doesn't, the gap sidecar coverage is incomplete
    (some gap rows weren't scored). ``agree_exact`` counts rows where
    tier AND adjustment both agree — agreement via the gap join path,
    which the digest excludes from its divergence ranking (2026-07
    digest hygiene)."""
    gap_rows = [r for r in rows if r.get("classification") == "gap_row_match"]
    populated = [r for r in gap_rows if r.get("mc_tier") is not None]
    deltas: Counter[int] = Counter()
    agree_exact = 0
    for r in populated:
        td = r.get("tier_delta")
        if isinstance(td, int):
            deltas[abs(td)] += 1
        # Prefer the digest's derived sub-bucket; recompute for older
        # JSONs that predate the field.
        bucket = r.get("gap_score_bucket")
        if bucket == "match_exact":
            agree_exact += 1
        elif bucket is None:
            ad = r.get("adj_delta")
            if td == 0 and isinstance(ad, (int, float)) and abs(ad) < 0.01:
                agree_exact += 1
    by_state: Counter[str] = Counter(r.get("state", "?") for r in gap_rows)
    return {
        "total": len(gap_rows),
        "populated_tier": len(populated),
        "agree_exact": agree_exact,
        "abs_tier_delta_dist": dict(sorted(deltas.items())),
        "by_state": dict(by_state),
    }


def build_summary(
    digests: Mapping[str, Mapping[str, Any]],
    *,
    plan_version: str | None,
    refresh_date: str,
) -> dict[str, Any]:
    """Pure-Python builder — no filesystem side effects.

    ``digests`` maps lens → loaded review_digest_<lens>.json payload.
    Returns the summary structure ``run`` will both write to JSON and
    feed into the markdown renderer.
    """
    summary: dict[str, Any] = {
        "refresh_date": refresh_date,
        "scoring_plan_version": plan_version,
        "lenses": list(digests.keys()),
        "per_lens": {},
    }
    for lens, digest in digests.items():
        overall = digest.get("overall") or {}
        per_state = digest.get("per_state") or {}
        rows = digest.get("rows") or []
        top_patterns = digest.get("top_patterns") or []
        summary["per_lens"][lens] = {
            "states": digest.get("states") or list(per_state.keys()),
            "reviewer_rows": overall.get("reviewer_rows"),
            "matched": overall.get("matched"),
            "match_pct": overall.get("match_pct"),
            "classification_counts": overall.get("classification_counts") or {},
            "per_state": {
                state: {
                    "reviewer_rows": payload.get("reviewer_rows"),
                    "matched": payload.get("matched"),
                    "match_pct": payload.get("match_pct"),
                    "classification_counts": payload.get(
                        "classification_counts"
                    )
                    or {},
                }
                for state, payload in per_state.items()
            },
            "gap_row_match": _gap_row_stats(rows),
            "top_patterns": top_patterns,
            "coverage_patterns": digest.get("coverage_patterns") or [],
        }
    return summary


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt_int(n: Any) -> str:
    if isinstance(n, (int, float)):
        return f"{int(n):,}"
    return "—"


def _fmt_pct(n: Any) -> str:
    if isinstance(n, (int, float)):
        return f"{float(n):.1f} %"
    return "—"


def _classification_table(
    classification_counts: Mapping[str, Mapping[str, int]],
    states: Iterable[str],
) -> list[str]:
    """Render one classification breakdown table.

    ``classification_counts`` maps state code → bucket → count.
    Includes a totals row.
    """
    states_list = list(states)
    header = "| State | " + " | ".join(_BUCKET_ORDER) + " |"
    align = (
        "| :- | " + " | ".join(["-:"] * len(_BUCKET_ORDER)) + " |"
    )
    lines = [header, align]
    totals: Counter[str] = Counter()
    for state in states_list:
        counts = classification_counts.get(state) or {}
        row_cells = [_fmt_int(counts.get(b, 0)) for b in _BUCKET_ORDER]
        for b in _BUCKET_ORDER:
            totals[b] += int(counts.get(b, 0))
        lines.append(f"| {state} | " + " | ".join(row_cells) + " |")
    total_cells = [_fmt_int(totals.get(b, 0)) for b in _BUCKET_ORDER]
    lines.append("| **total** | " + " | ".join(f"**{c}**" for c in total_cells) + " |")
    return lines


def _top_patterns_table(top_patterns: list[Mapping[str, Any]]) -> list[str]:
    if not top_patterns:
        return ["_No divergence patterns surfaced._"]
    lines = [
        "| # | State | Classification | Reason pair | Reviewer → POC-3 | Rows | Example |",
        "| -: | :- | :- | :- | :- | -: | :- |",
    ]
    for i, pat in enumerate(top_patterns, start=1):
        sig = pat.get("signature") or {}
        ex = pat.get("example") or {}
        rt = sig.get("reviewer_tier")
        pt = sig.get("mc_tier")
        rt_s = "—" if rt is None else str(rt)
        pt_s = "—" if pt is None else str(pt)
        ent = ex.get("entity") or "?"
        elem = ex.get("element") or "?"
        lines.append(
            f"| {i} | {sig.get('state', '?')} | "
            f"`{sig.get('classification', '?')}` | "
            f"{sig.get('reason_pair') or '—'} | "
            f"{rt_s} → {pt_s} | "
            f"{_fmt_int(pat.get('count'))} | "
            f"`{ent}\\|{elem}` |"
        )
    return lines


def _coverage_table(coverage: list[Mapping[str, Any]]) -> list[str]:
    if not coverage:
        return ["_No coverage-gap rows in this lens._"]
    lines = [
        "| State | Rows | Buckets | Top entities |",
        "| :- | -: | :- | :- |",
    ]
    for cluster in coverage:
        buckets = cluster.get("buckets") or {}
        buckets_label = " · ".join(
            f"{bucket} {_fmt_int(count)}" for bucket, count in sorted(buckets.items())
        )
        entities_label = " · ".join(
            f"`{e.get('entity')}` ({e.get('count')})"
            for e in (cluster.get("top_entities") or [])
        )
        lines.append(
            f"| {cluster.get('state', '?')} | {_fmt_int(cluster.get('count'))} | "
            f"{buckets_label or '—'} | {entities_label or '—'} |"
        )
    return lines


def render_markdown(summary: Mapping[str, Any]) -> str:
    """Render the markdown doc from a summary dict.

    Pure function — no filesystem side effects. ``run`` writes the
    output; tests can call this directly with a fabricated summary.
    """
    plan_v = summary.get("scoring_plan_version") or "unknown"
    refresh = summary.get("refresh_date") or "unknown"
    lenses = list(summary.get("per_lens", {}).keys())

    lines: list[str] = [
        f"# Reviewer-vs-POC-3 NACHOS comparison — `scoring_plan_version: {plan_v}`",
        "",
        f"**Refresh date:** {refresh} · "
        f"**Lenses:** {', '.join(lenses) or '—'} · "
        f"**LLM spend:** $0 (pure-Python re-aggregation against "
        f"`data/out/review_digest_{{lens}}.json`).",
        "",
        "**Framing.** The reviewer basis — five per-state human-scored "
        "workbooks under `docs/human-scored-files/` (AZ, WI, MN, TX, IN; "
        "see `review_loader.REVIEWER_SOURCES`) — is **one observer**; "
        "POC-3 is another. This artifact names where the two agree and "
        "diverge against the current sidecars. It does NOT assert "
        "correctness on either side; match rates are a change-detection "
        "signal, not a quality metric.",
        "",
        "**Basis change (2026-07-07).** The comparison basis switched "
        "from the single 1,712-row training workbook "
        "(`SourceData_Agent_training_file.xlsx`, WI/MN/TX/IN) to the "
        "five per-state workbooks (~2,687 rows; AZ newly in scope; IN "
        "grew 52 → 362 rows; MN's basis is the V4 workbook's `Details` "
        "sheet). Numbers on either side of this date are NOT comparable "
        "— both the row populations and the per-state authoring passes "
        "changed.",
        "",
        "**Refresh discipline.** This doc is regenerated each time "
        "`SCORING_PLAN_VERSION` bumps. Run "
        "`mc report reviewer-comparison` after `report review-digest "
        "--lens source` and `--lens spine` to refresh. See the operator "
        "playbook in CLAUDE.md.",
        "",
        "## Headline — match rates",
        "",
    ]

    # Match rate table — one row per (lens, state, total)
    lines.extend([
        "| Lens | State | Reviewer rows | Matched | Match % |",
        "| :- | :- | -: | -: | -: |",
    ])
    for lens, payload in summary.get("per_lens", {}).items():
        for state, st in (payload.get("per_state") or {}).items():
            lines.append(
                f"| {lens} | {state} | "
                f"{_fmt_int(st.get('reviewer_rows'))} | "
                f"{_fmt_int(st.get('matched'))} | "
                f"{_fmt_pct(st.get('match_pct'))} |"
            )
        lines.append(
            f"| **{lens}** | **total** | "
            f"**{_fmt_int(payload.get('reviewer_rows'))}** | "
            f"**{_fmt_int(payload.get('matched'))}** | "
            f"**{_fmt_pct(payload.get('match_pct'))}** |"
        )

    lines.extend([
        "",
        "Match rate counts every classification bucket as \"matched\" "
        "except `no_mc_row` and `no_reviewer_row`. `gap_row_match` "
        "counts as matched (the row resolved to a spine-anchored gap "
        "score under issue #73 Layer 3).",
        "",
        "**`no_mc_row` includes present-but-undocumented rows.** "
        "Swagger-backfill and leaf-borrow rows (`documented=False` under "
        "the v21 close-out posture) are surfaced by POC-3 but never "
        "scored, and once they are present in the source lens they are no "
        "longer emitted as gaps. A reviewer row that resolves to one "
        "therefore classifies as `no_mc_row`, not `gap_row_match`. The "
        "spine match rate counts rows MC *scored*, not every row it is "
        "*aware of* — so keeping the gap pipeline current yields a "
        "strictly lower, more honest spine match rate than a stale gap "
        "sidecar that still listed those rows as gap matches. The gap "
        "layer is regenerated as part of the refresh chain (see the "
        "operator playbook in CLAUDE.md) so it cannot silently drift "
        "behind the source-coverage expansion again.",
        "",
        "**Unjoinable reviewer rows are dropped at the loader "
        "(2026-07-01).** Rows whose entity or element cell is literally "
        "`NA` / `N/A` (18 WI rows on the per-state basis: statusCode, "
        "patientIdentifier.*, …) can never resolve to a POC-3 record "
        "key; they would sit in `no_mc_row` permanently, deflating "
        "the match rate. Dropping them shrinks the reviewer-row "
        "denominators relative to the raw workbook row counts.",
        "",
    ])

    # Per-lens classification breakdowns
    for lens, payload in summary.get("per_lens", {}).items():
        states = payload.get("states") or list(
            (payload.get("per_state") or {}).keys()
        )
        per_state = payload.get("per_state") or {}
        cc_by_state = {
            state: (per_state.get(state) or {}).get("classification_counts")
            or {}
            for state in states
        }
        lines.extend([
            f"## Classification breakdown — {lens} lens",
            "",
        ])
        lines.extend(_classification_table(cc_by_state, states))
        lines.append("")

    # Gap_row_match deep-dive
    lines.extend([
        "## Gap-row match cohort (Layer 3 detail)",
        "",
        "Layer 3 (issue #73, PR #88) joins reviewer `gap_row_match` rows "
        "against `data/out/{state}_scores_mc_gap.json`. Where a canonical "
        "record_key resolves, the comparison row populates POC-3 tier / "
        "adjusted score / tier delta. The classification bucket itself "
        "stays `gap_row_match` — these counts segment the bucket by "
        "scoring shape. `Agree exactly` counts rows where tier AND "
        "adjustment both agree: agreement via the gap join path, which "
        "the digest excludes from its divergence ranking (2026-07 "
        "hygiene) — a large gap bucket is mostly *agreement*, not a "
        "divergence signal.",
        "",
        "| Lens | Total gap rows | Populated tier | Agree exactly | |Δ tier|=0 | |Δ|=1 | |Δ|=2 | |Δ|=3 | By state |",
        "| :- | -: | -: | -: | -: | -: | -: | -: | :- |",
    ])
    for lens, payload in summary.get("per_lens", {}).items():
        gap = payload.get("gap_row_match") or {}
        dist = gap.get("abs_tier_delta_dist") or {}
        by_state = gap.get("by_state") or {}
        by_state_str = (
            " · ".join(f"{s} {n}" for s, n in by_state.items()) if by_state else "—"
        )
        lines.append(
            f"| {lens} | {_fmt_int(gap.get('total'))} | "
            f"{_fmt_int(gap.get('populated_tier'))} | "
            f"{_fmt_int(gap.get('agree_exact'))} | "
            f"{_fmt_int(dist.get(0, 0))} | {_fmt_int(dist.get(1, 0))} | "
            f"{_fmt_int(dist.get(2, 0))} | {_fmt_int(dist.get(3, 0))} | "
            f"{by_state_str} |"
        )
    lines.append("")
    lines.append(
        "When `Populated tier` lags `Total gap rows`, the gap sidecar "
        "coverage is incomplete — re-run `mc score gap-extract` and "
        "`aggregate-gap` to refresh."
    )
    lines.append("")

    # Top divergence patterns per lens
    for lens, payload in summary.get("per_lens", {}).items():
        top = list(payload.get("top_patterns") or [])
        lines.extend([
            f"## Top divergence patterns — {lens} lens",
            "",
            "Score disagreements only (2026-07 hygiene): agreement rows "
            "— including gap rows that agree exactly — and coverage "
            "buckets never rank here. Coverage gaps are tabled "
            "separately below. The `Reason pair` column splits "
            "`match_tier` clusters by normalized justification class "
            "(e.g. reviewer *necessary* vs POC-3 *unnecessary_ext* — "
            "the extension-necessity definition split routed to the "
            "rubric working session).",
            "",
        ])
        lines.extend(_top_patterns_table(top))
        lines.append("")

    # Coverage gaps per lens
    for lens, payload in summary.get("per_lens", {}).items():
        coverage = list(payload.get("coverage_patterns") or [])
        lines.extend([
            f"## Coverage gaps — {lens} lens",
            "",
            "Reviewer rows with no comparable POC-3 score "
            "(`no_mc_row` + unscored gap rows) — join-coverage "
            "facts, not rubric divergence; no response category "
            "See the `no_mc_row` caveat above for why the "
            "spine bucket is structurally larger.",
            "",
        ])
        lines.extend(_coverage_table(coverage))
        lines.append("")

    # Editorial pointer + out of scope
    lines.extend([
        "## Editorial commentary lives elsewhere",
        "",
        "This doc is mechanically regenerated, so it stays focused on "
        "stats. Narrative and methodology framing live in:",
        "",
        "- `docs/recommendations.md` §6 — Limitations & open methodology "
        "questions (covers the SF-fold artifact, source-coverage gap, "
        "key-sever override, F1 / F2 / F3 status).",
        "- `docs/recommendations.md` §7.3 — Disclosed divergence on "
        "Adjusted NACHOS extension weighting (the v17 B3 collapse).",
        "- `docs/archive/reviewer-comparison-2026-04-28.md` — original "
        "v12 reviewer-comparison synthesis preserved as a historical "
        "snapshot.",
        "",
        "## Out of scope (human-scored framing discipline)",
        "",
        "Per `docs/archive/next-session/next-session-phase-e.md`:",
        "",
        "- **No GT vocabulary.** \"Ground truth\" / \"accuracy\" / "
        "\"correctness\" never appear in this artifact. Use \"reviewer "
        "score\", \"rubric divergence\", \"disagreement pattern\".",
        "- **No retraining of any rule, prompt, or fact against the "
        "reviewer file.** Findings F1 / F2 / F3 are open methodology "
        "questions, not POC-3 bug reports.",
        "- **All five POC-3 states carry a reviewer counterpart** since "
        "the 2026-07-07 per-state basis (AZ joined via its own "
        "workbook). Human-scored files for states outside the POC-3 "
        "roster (Nebraska, Georgia) sit in the same directory but are "
        "deliberately never loaded — no sidecars exist to join.",
        "",
        "## Source artifacts",
        "",
        "- `data/out/review_digest_source.json` / `_spine.json` — "
        "machine-readable per-row + per-pattern data this doc rolls up.",
        "- `data/out/{state}_scores_{source,spine,gap}.json` — the "
        "sidecars carrying the `scoring_plan_version` stamp.",
        "",
    ])

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


_DEFAULT_DOC_PATH = "docs/reviewer-comparison.md"


def run(
    *,
    out_path: Path | None = None,
    base: Path | None = None,
    states_for_version_check: Iterable[str] = SUPPORTED_STATES,
) -> dict[str, Any]:
    """Build the reviewer-comparison summary doc.

    Reads ``data/out/review_digest_{source,spine}.json`` and one source-
    lens sidecar header per state to stamp the plan version. Writes
    ``docs/reviewer-comparison.md`` (or ``out_path`` if provided).
    Returns the summary dict so tests / callers can introspect the
    counts without re-reading the doc.
    """
    digests: dict[str, dict[str, Any]] = {
        lens: _load_digest(lens, base=base) for lens in _LENSES
    }
    plan_version = _detect_plan_version(states_for_version_check, lens="source")
    summary = build_summary(
        digests,
        plan_version=plan_version,
        refresh_date=date.today().isoformat(),
    )
    target = out_path or (project_root() / _DEFAULT_DOC_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(summary), encoding="utf-8")
    _LOGGER.info(
        "wrote %s — plan_version=%s · lenses=%s",
        target,
        plan_version,
        list(digests.keys()),
    )
    return summary
