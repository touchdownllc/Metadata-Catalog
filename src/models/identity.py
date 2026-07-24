"""Durable subject identity — the ONE home of the record-key format.

``record_key`` is the pipeline's subject id: it keys the score
sidecars, the committed curation sidecars (``data/curation/
{state}.json`` — human decisions that must survive regeneration), the
cross-lens necessity borrow, and every reviewer-comparison join. The
format is LOAD-BEARING and frozen byte-for-byte by
``tests/test_models_identity.py``: changing it orphans committed
curation entries and breaks every cross-artifact join at once.

Scoring-boundary plan item A1 (ADR 0016): this module replaces the
dozen-plus literal ``f"{state}|{entity}|{element}"`` constructions
that had grown across score/ and report/. It is also the future home
of the open "durable subject identity across state spec versions"
question — when that gets an answer (e.g. rename mapping between
spec versions), it lands here, once.

Deliberately dependency-free (stdlib only): identity must be
importable from anywhere in the tree without cycles.
"""

from __future__ import annotations

__all__ = ["record_key"]


def record_key(state: str, entity: str, element_name: str) -> str:
    """``{STATE}|{entity}|{element_name}`` — state uppercased, entity
    and element verbatim (they carry the source document's casing;
    cross-lens joins that need case-insensitivity lower the WHOLE key
    at the join site, never here)."""
    return f"{state.upper()}|{entity}|{element_name}"
