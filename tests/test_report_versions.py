"""Non-drift tests for ``src.report.versions`` — methodology history.

The load-bearing test is the non-drift one: the registry MUST carry an
entry for the live ``SCORING_PLAN_VERSION``, so every future version
bump is forced to land an analyst-facing one-liner in the same diff.
The rest keep the registry render-ready (unique versions, non-empty
single-line summaries, consistent ordering) and hold the issue #174
terminology line (no internal shorthand in analyst-facing prose).
"""

from __future__ import annotations

from src.report.versions import (
    METHODOLOGY_VERSION_HISTORY,
    VersionEntry,
    current_version_entry,
)
from src.score.aggregate import SCORING_PLAN_VERSION

# Case-insensitive substring checks. "API model" is the sanctioned
# stakeholder name and contains none of these, so no allowlist carve-out
# is needed.
_BANNED_INTERNAL_TERMS = ("spine", "integration profile", "structural depth")


class TestCurrentVersionNonDrift:
    def test_registry_has_entry_for_live_version(self):
        entry = current_version_entry()
        assert entry is not None, (
            f"METHODOLOGY_VERSION_HISTORY has no entry for the live "
            f"SCORING_PLAN_VERSION={SCORING_PLAN_VERSION!r} — every "
            f"version bump must add an analyst one-liner to "
            f"src.report.versions in the same diff."
        )
        assert entry.version == SCORING_PLAN_VERSION

    def test_current_entry_is_the_registry_object(self):
        entry = current_version_entry()
        assert entry in METHODOLOGY_VERSION_HISTORY


class TestRegistryShape:
    def test_entries_are_version_entries(self):
        assert METHODOLOGY_VERSION_HISTORY, "registry must not be empty"
        for entry in METHODOLOGY_VERSION_HISTORY:
            assert isinstance(entry, VersionEntry)

    def test_versions_unique(self):
        versions = [e.version for e in METHODOLOGY_VERSION_HISTORY]
        assert len(versions) == len(set(versions))

    def test_summaries_non_empty_single_line(self):
        for entry in METHODOLOGY_VERSION_HISTORY:
            assert entry.summary.strip(), f"v{entry.version}: empty summary"
            assert entry.summary == entry.summary.strip(), (
                f"v{entry.version}: summary has leading/trailing whitespace"
            )
            assert "\n" not in entry.summary, (
                f"v{entry.version}: summary must be a single line"
            )

    def test_dates_non_empty(self):
        for entry in METHODOLOGY_VERSION_HISTORY:
            assert entry.date.strip(), f"v{entry.version}: empty date"

    def test_ordered_ascending_by_version(self):
        numeric = [int(e.version) for e in METHODOLOGY_VERSION_HISTORY]
        assert numeric == sorted(numeric), (
            "registry must be ordered ascending by version (oldest first)"
        )
        assert len(numeric) == len(set(numeric))


class TestStakeholderTerminology:
    def test_no_banned_internal_terms_in_summaries(self):
        for entry in METHODOLOGY_VERSION_HISTORY:
            lowered = entry.summary.lower()
            for term in _BANNED_INTERNAL_TERMS:
                assert term not in lowered, (
                    f"v{entry.version}: summary uses internal term "
                    f"{term!r} — analyst-facing prose follows the issue "
                    f"#174 vocabulary (use 'API model' etc.)"
                )
