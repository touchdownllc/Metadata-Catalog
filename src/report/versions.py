"""Methodology version-history registry for analyst-facing surfaces.

One consumer-relevant one-liner per ``SCORING_PLAN_VERSION`` from v18
onward, written for analysts (stakeholder terminology per issue #174 —
no internal shorthand). The authoritative engineering changelog stays
in CLAUDE.md / ``docs/working-journal.md``; this registry is the
render-ready subset a workbook or brief can print verbatim.

Non-drift contract: ``tests/test_report_versions.py`` asserts the
registry carries an entry for the live
``src.score.aggregate.SCORING_PLAN_VERSION`` — every future version
bump must land a one-liner here in the same diff.

This module is PRESENTATION-LAYER PROSE ONLY. It must never be
imported by ``rules.py`` / ``aggregate.py`` / ``deterministic.py`` —
nothing here is consulted by scoring.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "VersionEntry",
    "METHODOLOGY_VERSION_HISTORY",
    "current_version_entry",
]


@dataclass(frozen=True)
class VersionEntry:
    """One methodology version: number, date landed, analyst one-liner."""

    version: str
    date: str
    summary: str


# Ordered ascending by version (oldest first). Dates are the session
# dates recorded in docs/working-journal.md. Summaries describe what an
# analyst reading the scores needs to know changed — not how it was
# implemented.
METHODOLOGY_VERSION_HISTORY: tuple[VersionEntry, ...] = (
    VersionEntry(
        version="18",
        date="2026-04-29",
        summary=(
            "Two-tier extension weighting: +1 for an unnecessary state "
            "extension, +0.5 for a necessary one."
        ),
    ),
    VersionEntry(
        version="21",
        date="2026-04-30",
        summary=(
            "Close-out posture: headline aggregates restricted to "
            "state-authored documentation — swagger-only rows are "
            "surfaced but not counted in headline numbers."
        ),
    ),
    VersionEntry(
        version="22",
        date="2026-04-30",
        summary=(
            "Bare foreign-key calibration: WI submission-gate rows no "
            "longer read as conditional logic."
        ),
    ),
    VersionEntry(
        version="23",
        date="2026-05-02",
        summary=(
            "Complex Business Logic cost tier added to the source lens "
            "(previously API-model lens only)."
        ),
    ),
    VersionEntry(
        version="24",
        date="2026-05-02",
        summary=(
            "Non-stacking adjustments: when extension-necessity and "
            "meaning-divergence both apply at base 0, the larger "
            "adjustment applies, not the sum."
        ),
    ),
    VersionEntry(
        version="25",
        date="2026-05-02",
        summary=(
            "Typed reasons annotated on meaning-divergence adjustments "
            "(why the meaning diverges, not just that it does)."
        ),
    ),
    VersionEntry(
        version="26",
        date="2026-05-03",
        summary=(
            "Leaf-level cross-model borrow: sub-collection leaves the "
            "state documentation didn't enumerate are surfaced "
            "(uncounted in headline numbers)."
        ),
    ),
    VersionEntry(
        version="27",
        date="2026-06-29",
        summary=(
            "Assessment, AssessmentRegistration, Staff, and Finance "
            "domains brought into scope (TX and IN grow the coverage "
            "denominator)."
        ),
    ),
    VersionEntry(
        version="28",
        date="2026-07-13",
        summary=(
            "Analyst fact corrections: a human-corrected extraction "
            "input can now feed the rule cascade, with full provenance "
            "(the fact is marked human-corrected); scoring rules "
            "themselves are unchanged."
        ),
    ),
)


def current_version_entry() -> VersionEntry | None:
    """Return the entry matching the live ``SCORING_PLAN_VERSION``.

    Imports lazily so this prose module never becomes an import-time
    dependency of the scoring package (and vice versa).
    """
    from src.score.aggregate import SCORING_PLAN_VERSION

    for entry in METHODOLOGY_VERSION_HISTORY:
        if entry.version == SCORING_PLAN_VERSION:
            return entry
    return None
