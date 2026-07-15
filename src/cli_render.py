"""CLI progress/summary renderers (issue #213 item 3).

The twin per-batch progress renderers and the post-run summary echo
blocks for ``mc score extract`` / ``mc score peer-gap`` lived
inline in ``cli.py``. They are pure presentation — every string here is
byte-identical to the pre-extraction ``cli.py`` output. ``cli.py``
keeps only option parsing + dispatch.
"""

from __future__ import annotations

import click


# ---------------------------------------------------------------------------
# `mc score extract`
# ---------------------------------------------------------------------------


def render_extract_progress(event: dict) -> None:
    """Per-entity-batch progress line for ``score extract``."""
    # Dry-run path never invokes the callback; keep output crisp.
    tag = "cache" if event["cache_hit"] else "llm  "
    click.echo(
        f"  [{event['index'] + 1:>2}/{event['total']}] {tag} {event['entity']:<52s} "
        f"elems={event['element_count']:>2}  "
        f"in={event['tokens_in']:>5}  out={event['tokens_out']:>4}  "
        f"${event['usd']:.4f}  (run ${event['running_usd']:.4f})"
        + (f"  downgrades={event['downgrades_in_batch']}" if event["downgrades_in_batch"] else "")
    )


def echo_extract_result(
    header: dict, *, fact: str, state: str, lens: str
) -> None:
    """Post-run summary for ``score extract`` (all modes)."""
    mode = header.get("mode")
    if mode == "dry-run":
        click.echo(
            f"dry-run: {header['entities_processed']} entity batches, "
            f"{header['record_count']} records, manifest at "
            f"{header['dry_run_manifest']}"
        )
    elif mode == "deterministic":
        true_count = header.get("true_count", 0)
        total = header.get("record_count", 0)
        pct = (true_count / total * 100) if total else 0.0
        click.echo(
            f"deterministic {fact}: {true_count}/{total} true ({pct:.1f}%) · "
            f"{header['entities_processed']} entities · $0.0000"
        )
    elif mode == "validate-only":
        print_validate_only_summary(header, fact=fact, state=state, lens=lens)
    else:
        click.echo(
            f"extracted {header['scored_count']}/{header['record_count']} records "
            f"across {header['entities_processed']} entities · "
            f"${header['total_usd']:.4f} · "
            f"{header['cache_hit_count']} cache hits · "
            f"{header['downgrade_count']} downgrades"
        )


def print_validate_only_summary(
    header: dict, *, fact: str, state: str, lens: str
) -> None:
    """Surface downgrade-reason histogram for a ``--validate-only`` run.

    Reads the scratch artifact the harness wrote to
    ``phase_a/validate_only/{state}_{lens}_{fact}.jsonl`` and emits a
    per-reason count plus up to five sample rows per reason so a
    polarity / schema bug is obvious on screen (Phase D carryover #3).
    """
    import json as _json
    from collections import Counter

    from src.utils.paths import scoring_phase_a_dir

    artifact = (
        scoring_phase_a_dir()
        / "validate_only"
        / f"{state.upper()}_{lens}_{fact}.jsonl"
    )
    click.echo(
        f"validate-only: {header['scored_count']}/{header['record_count']} records · "
        f"${header['total_usd']:.4f} · "
        f"{header['cache_hit_count']} cache hits · "
        f"{header['downgrade_count']} downgrades"
    )
    if not artifact.exists():
        click.echo(f"  (no artifact at {artifact} — skipping sample list)")
        return
    reason_counter: Counter = Counter()
    samples: dict[str, list[dict]] = {}
    with artifact.open("r", encoding="utf-8") as fh:
        for idx, line in enumerate(fh):
            if idx == 0:
                continue  # header row
            row = _json.loads(line)
            reason = row.get("downgrade_reason")
            if reason is None:
                continue
            reason_counter[reason] += 1
            samples.setdefault(reason, []).append(row)
    if not reason_counter:
        click.echo("  no downgrades — sanity clean")
        return
    click.echo("  downgrade-reason histogram:")
    for reason, count in reason_counter.most_common():
        click.echo(f"    {count:>3} {reason}")
    click.echo("  samples (first 2 per reason):")
    for reason, rows in samples.items():
        for row in rows[:2]:
            click.echo(
                f"    [{reason}] {row.get('record_key', '?')} — "
                f"llm_value={row.get('llm_value')!r} "
                f"confidence={row.get('confidence')!r} "
                f"spans={len(row.get('spans', []))}"
            )


# ---------------------------------------------------------------------------
# `mc score peer-gap`
# ---------------------------------------------------------------------------


def render_peer_gap_progress(event: dict) -> None:
    """Per-slot progress line for ``score peer-gap``."""
    tag = "cache" if event["cache_hit"] else "llm  "
    confidence = event.get("confidence") or "?"
    states = ",".join(event["states"])
    click.echo(
        f"  [{event['index'] + 1:>2}/{event['total']}] {tag} "
        f"{event['slot_key']:<60s} states=[{states}] "
        f"in={event['tokens_in']:>5} out={event['tokens_out']:>4} "
        f"${event['usd']:.4f}  (run ${event['running_usd']:.4f}) "
        f"conf={confidence}"
    )


def echo_peer_gap_result(
    header: dict, *, dry_run: bool, cost_cap: float
) -> None:
    """Post-run summary for ``score peer-gap`` (all modes)."""
    if dry_run:
        click.echo(
            f"peer-gap dry-run: {header['bundle_count']} bundles → "
            f"{header['dry_run_manifest']}"
        )
    else:
        click.echo(
            f"peer-gap complete: {header['scored_count']}/{header['bundle_count']} slots · "
            f"cache hits {header['cache_hit_count']} · "
            f"${header['total_usd']:.4f} total · cap ${cost_cap:.2f}"
        )


# ---------------------------------------------------------------------------
# `mc score run-all`
# ---------------------------------------------------------------------------


def render_run_all_pair(pair) -> None:
    """Per-(state, fact) progress line for ``score run-all``."""
    tag = pair.status.upper()
    line = (
        f"  [{tag:<16}] {pair.state} · {pair.fact:<32} "
        f"records={pair.scored_count}/{pair.record_count} "
        f"${pair.total_usd:.4f} "
        f"cache={pair.cache_hit_count} downgrades={pair.downgrade_count}"
    )
    if pair.error:
        line += f" err={pair.error}"
    click.echo(line)


def render_run_all_checkpoint(state: str, manifest, *, assume_yes: bool) -> bool:
    """The ``--checkpoint-after`` summary + confirm gate."""
    state_pairs = [p for p in manifest.pairs if p.state == state]
    total_downgrades = sum(p.downgrade_count for p in state_pairs)
    total_spend = sum(p.total_usd for p in state_pairs)
    total_records = sum(p.scored_count for p in state_pairs)
    click.echo(
        f"\n--- checkpoint after {state} ---\n"
        f"  {len(state_pairs)} pairs · "
        f"${total_spend:.4f} · {total_downgrades} downgrades · "
        f"{total_records} records scored\n"
        f"  per-pair downgrade detail:"
    )
    for p in state_pairs:
        tag = "HIGH" if p.record_count and p.downgrade_count / max(p.record_count, 1) > 0.15 else "ok  "
        click.echo(
            f"    [{tag}] {p.fact:<32} dg={p.downgrade_count:>3}/{p.record_count:<3} "
            f"${p.total_usd:.4f}"
        )
    if assume_yes:
        click.echo("  --yes set — continuing without prompt\n")
        return True
    return click.confirm("continue fanout to remaining states?", default=True)


def echo_batch_submission(manifests) -> None:
    """Post-submit summary for ``score run-all --batch``."""
    plural = "es" if len(manifests) > 1 else ""
    click.echo(f"Submitted {len(manifests)} batch{plural}:")
    for m in manifests:
        click.echo(
            f"  {m.batch_id}  items={len(m.items)}  est=${m.estimated_cost_usd:.2f}"
        )
    click.echo("\nWait ~5–60 min, then collect:")
    for m in manifests:
        click.echo(f"  mc score batches collect {m.batch_id}")
    click.echo(
        "\nAfter collect, run `mc score run-all` (no --batch) to build "
        "sidecars from the populated cache."
    )


# ---------------------------------------------------------------------------
# `mc score gap-extract` / `gap-downgrade-summary`
# ---------------------------------------------------------------------------


def echo_gap_extract_results(results) -> None:
    """Per-state + grand-total summary for ``score gap-extract``."""
    grand_usd = 0.0
    grand_downgrades = 0
    grand_records = 0
    for r in results:
        click.echo(
            f"  [{r.state}] sample={r.sample_count} "
            f"facts_complete={sum(1 for f in r.fact_results if f['status'] == 'complete')}/{len(r.fact_results)} "
            f"usd=${r.total_usd:.4f} "
            f"downgrades={r.total_downgrades}/{r.total_records}"
        )
        grand_usd += r.total_usd
        grand_downgrades += r.total_downgrades
        grand_records += r.total_records

    click.echo(
        f"\nTotal: ${grand_usd:.2f} across {grand_records} fact-rows "
        f"(downgrade_count={grand_downgrades}, "
        f"rate={grand_downgrades / grand_records if grand_records else 0:.4f})"
    )
    click.echo(
        "\nNext: `mc score aggregate-gap --with-llm` to fold these "
        "values into the gap sidecars, then "
        "`mc score gap-downgrade-summary` for the halt-criterion check."
    )


def echo_gap_downgrade_summary(summary: dict) -> None:
    """Per-fact downgrade-rate table for ``score gap-downgrade-summary``."""
    click.echo(
        f"States loaded: {', '.join(summary['states_loaded'])}"
    )
    click.echo(
        f"Total scored: {summary['total_scored']} · "
        f"downgrades: {summary['total_downgrades']} · "
        f"overall rate: {summary['overall_rate']:.4f}"
    )
    click.echo(f"\nPer-fact downgrade rates (halt threshold > 0.30):")
    rows = sorted(
        summary["per_fact"].items(),
        key=lambda kv: kv[1]["rate"],
        reverse=True,
    )
    for fact, stats in rows:
        flag = " ⚠ HALT" if stats["rate"] > 0.30 else ""
        click.echo(
            f"  {fact:<40} {stats['downgrades']:>4}/{stats['scored']:<5} "
            f"= {stats['rate']:.4f}{flag}"
        )

    if summary["halt_recommended"]:
        click.echo(
            "\nHALT RECOMMENDED: at least one fact > 30 % downgrade rate. "
            "Investigate before committing Step 3 spend."
        )
    else:
        click.echo(
            "\nAll facts at or below 30 % — Step 3 path is clean to authorize."
        )
