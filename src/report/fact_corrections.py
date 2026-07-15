"""Issue #249 — fact-correction render helpers (register + clustering).

Pure functions over dicts (the ``override_clusters`` idiom); rendering
lives in ``report.score_card`` (corrections-by-fact-name diagnostic)
and ``report.analyst`` (Update Log fact-corrections register).

Two postures are load-bearing here:

1. **The cluster table is the prompt-weakness router.** A correction
   fixes ONE row; a correction PATTERN (many rows, one fact) is
   evidence about the extraction PROMPT, and the legitimate response
   is a prompt-version bump — never tuning the prompt against the
   corrections themselves (GT boundary: corrections are human
   observations, not training signal).
2. **The register is honest about apply state.** ``correct-fact``
   writes the curation sidecar; the score only moves when ``score
   aggregate`` re-runs. A workbook regenerated in between renders the
   correction as ``pending re-aggregate`` rather than pretending the
   overlay landed — the same honestly-stale posture as the
   adjudication register's STALE rows.
"""

from __future__ import annotations


def _flip(block: dict) -> str:
    """``prior → corrected`` rendered for one correction block."""
    return f"{block.get('prior_value')} → {block.get('value')}"


def resolve_corrections(
    state: str,
    corrections: dict[str, dict[str, dict]],
    scores: dict[str, dict],
) -> list[dict]:
    """Register entries for one state's same-lens fact corrections.

    ``corrections`` is ``curation.fact_corrections_for`` output
    (``{record_key: {fact: block}}``); ``scores`` is the loaded scores
    sidecar keyed by record. Returns one dict per correction with
    identity (entity/element from the score record, else parsed from
    the record key — the ``adjudications_for`` fallback), the rendered
    ``flip``, and a ``status``:

    - ``applied`` — the sidecar's fact carries ``human_corrected`` and
      the corrected value (the overlay ran);
    - ``pending re-aggregate`` — the sidecar predates the correction
      (run ``mc score aggregate`` / ``mc publish``);
    - ``record gone`` — the record is no longer scored at all.

    Sorted by ``(record_key, fact)`` for deterministic rendering.
    """
    out: list[dict] = []
    for record_key in sorted(corrections):
        score = scores.get(record_key)
        key_parts = record_key.split("|")
        for fact in sorted(corrections[record_key]):
            block = corrections[record_key][fact]
            if score is None:
                status = "record gone"
            else:
                prov = (score.get("fact_provenance") or {}).get(fact) or {}
                applied = (
                    prov.get("provenance") == "human_corrected"
                    and prov.get("value") == block.get("value")
                )
                status = "applied" if applied else "pending re-aggregate"
            out.append({
                "state": state,
                "record_key": record_key,
                "entity": (score or {}).get("entity")
                or (key_parts[1] if len(key_parts) == 3 else None),
                "element_name": (score or {}).get("element_name")
                or (key_parts[2] if len(key_parts) == 3 else None),
                "fact": fact,
                "flip": _flip(block),
                "lens": block.get("lens"),
                "author": block.get("author"),
                "corrected_at": block.get("corrected_at"),
                "rationale": block.get("rationale"),
                "status": status,
            })
    return out


def cluster_corrections_by_fact(
    corrections_by_state: dict[str, dict[str, dict[str, dict]]],
) -> list[dict]:
    """Cluster fact corrections by ``(fact, flip)`` — the routing signal.

    ``corrections_by_state`` maps state → the
    ``curation.fact_corrections_for`` output for that state (per-state
    callers pass single-entry dicts; the combined workbook passes all
    states). Returns one dict per ``(fact, flip)`` group::

        {
            "fact": str,
            "flip": "prior → corrected",
            "rows": int,
            "state_counts": {state: rows},   # keys sorted
        }

    sorted deterministically (fingerprint-load-bearing): biggest
    clusters first, then fact name, then flip. Row counts reconcile to
    the total correction count — one correction lands in exactly one
    cluster.
    """
    groups: dict[tuple[str, str], dict] = {}
    for state in sorted(corrections_by_state):
        for blocks in (corrections_by_state[state] or {}).values():
            for fact in sorted(blocks):
                flip = _flip(blocks[fact])
                group = groups.setdefault(
                    (fact, flip),
                    {
                        "fact": fact,
                        "flip": flip,
                        "rows": 0,
                        "state_counts": {},
                    },
                )
                group["rows"] += 1
                counts = group["state_counts"]
                counts[state] = counts.get(state, 0) + 1

    out = list(groups.values())
    for group in out:
        group["state_counts"] = dict(sorted(group["state_counts"].items()))
    out.sort(key=lambda g: (-g["rows"], g["fact"], g["flip"]))
    return out
