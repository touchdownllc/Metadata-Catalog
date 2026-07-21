"""One state roster — `src.states.SUPPORTED_STATES` is the single home.

Sequence 4 PR A created the canonical roster; issue #211 item 5d found
ten-plus modules still carrying private copies. That drift class fails
*silently*: a sixth state would simply be omitted from gap-extract /
aggregate-gap / report surfaces (the PR #182 lesson through a different
door), and the frozen 4-state fixture shape it produced was exactly the
fresh-clone red suite of issue #211 item 2.

The grep test below is deliberately blunt — any literal 5-state sequence
outside ``states.py`` is a regression, comments and docstrings included
(prose copies drift too; reference the roster instead of restating it).
"""

from __future__ import annotations

import re
from pathlib import Path

from src.states import SUPPORTED_STATES

_SRC = Path(__file__).resolve().parent.parent / "src" / "poc3"

# Matches the roster written out literally with any quoting/spacing:
# ("AZ", "WI", "MN", "TX", "IN"), {"AZ","WI",...}, ["AZ", 'WI', ...].
_LITERAL_ROSTER = re.compile(
    r"""["']AZ["']\s*,\s*["']WI["']\s*,\s*["']MN["']\s*,\s*["']TX["']\s*,\s*["']IN["']"""
)


def test_no_module_redeclares_the_roster() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == "states.py":
            continue
        if _LITERAL_ROSTER.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(_SRC.parent.parent)))
    assert not offenders, (
        "literal 5-state roster found outside src.states — import "
        f"SUPPORTED_STATES instead: {offenders}"
    )


def test_ingest_module_map_covers_the_roster() -> None:
    """`publish/stages._INGEST_MODULES` is keyed per state (can't become a
    plain import) — pin its keys to the roster so a sixth state can't be
    silently missing from `mc publish`'s ingest stage."""
    from src.publish.stages import _INGEST_MODULES

    assert set(_INGEST_MODULES) == set(SUPPORTED_STATES)


def test_human_backfill_name_map_covers_the_roster() -> None:
    """`human_score_backfill._STATE_NORMALIZE` maps reviewer-file state
    names to codes — same can't-become-an-import shape; pin its values."""
    from src.report.human_score_backfill import _STATE_NORMALIZE

    assert set(_STATE_NORMALIZE.values()) == set(SUPPORTED_STATES)


def test_roster_reexports_stay_canonical() -> None:
    """Modules that historically exposed their own `SUPPORTED_STATES` now
    re-export the canonical tuple (identity, not just equality — a copy
    would satisfy `==` while drifting on the next edit)."""
    from src.score import extract, peer_gap

    assert extract.SUPPORTED_STATES is SUPPORTED_STATES
    assert peer_gap.SUPPORTED_STATES is SUPPORTED_STATES


def test_state_info_covers_the_roster() -> None:
    """`STATE_INFO` (issue #213 item 2) is the per-state descriptor registry
    the prefix regexes derive from — its keys must track the roster exactly
    (also enforced at import time in states.py; this pins it in CI)."""
    from src.states import STATE_INFO

    assert tuple(STATE_INFO) == SUPPORTED_STATES
    for state, info in STATE_INFO.items():
        assert info.code == state
        assert info.ext_prefixes, f"{state} must declare >=1 ext prefix"


def test_sandbox_urls_cover_the_roster() -> None:
    """`spine.fetch._SANDBOX_URLS` is content-rich (real per-state endpoint
    URLs — TX intentionally points at the local TSDS Docker stack) so it
    can't derive from the roster; pin its coverage instead. A sixth state
    fails here until someone supplies its swagger endpoints."""
    from src.spine.fetch import _SANDBOX_URLS

    assert set(_SANDBOX_URLS) == set(SUPPORTED_STATES)
    for state in SUPPORTED_STATES:
        assert {"resources", "descriptors"} <= set(_SANDBOX_URLS[state]), (
            f"{state} must configure both resources and descriptors URLs"
        )


def test_domain_sources_cover_the_roster() -> None:
    """Every rostered state must either register domain documentation in
    `domain_scope.DOMAIN_SOURCES` or be listed in the documented
    `NO_DOMAIN_SOURCES` allowlist — a sixth state fails here until someone
    decides (issue #213 item 2)."""
    from src.ingest.domain_scope import DOMAIN_SOURCES, NO_DOMAIN_SOURCES

    registered = {d.state.upper() for d in DOMAIN_SOURCES}
    undecided = set(SUPPORTED_STATES) - registered - set(NO_DOMAIN_SOURCES)
    assert not undecided, (
        "states with neither a DOMAIN_SOURCES entry nor an explicit "
        f"NO_DOMAIN_SOURCES listing: {sorted(undecided)} — register the "
        "state's domain documentation or add it to NO_DOMAIN_SOURCES with "
        "a rationale"
    )
    both = registered & set(NO_DOMAIN_SOURCES)
    assert not both, (
        f"states listed in NO_DOMAIN_SOURCES but carrying registry entries: "
        f"{sorted(both)} — remove one side"
    )
