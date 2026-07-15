"""SIS-never-populated domain filter for the spine lens.

SIS vendors typically do not wire up the following Ed-Fi domains in real
deployments. The reporting surface exists in Ed-Fi, but scoring the
(entity, element) positions inside these domains for SIS coverage is noise
— no vendor populates them. Drop them from the spine-lens artifact and
replace each with a single placeholder row per (state, domain) so
reviewers can see we intentionally ignored the domain (not a scraper bug).

Source-lens artifacts are unaffected — they remain the literal audit trail
of what each state actually documented.

Filter posture:

- **Primary (domain-map):** an entity whose declared Ed-Fi domains are a
  subset of `FILTERED_DOMAINS` is dropped. Shared entities (Student,
  Staff, Program, Section, Course — which also participate in Enrollment,
  Discipline, etc.) stay because their domain set is not a pure subset.
- **Explicit concept-anchor list:** a small hand-curated set of entities
  whose name and purpose clearly belong to a filtered domain even though
  Ed-Fi's domain tags spread them across 2+ domains (e.g., LearningStandard
  is tagged with `Standards`+`Assessment`+`CourseCatalog`+...; GradebookEntry
  with `StudentAcademicRecord`+`Gradebook`). The all-subset rule wouldn't
  catch these; the explicit list does.
- **Fallback (entity-name prefix):** state extensions named e.g.
  `tx_AssessmentExt` are not present in the committed domain map.
  When the domain lookup returns no domains, match the entity name
  against `_FALLBACK_PREFIXES`.
"""

from __future__ import annotations

from src.ingest.domain_scope import is_domain_enabled
from src.models.spine import StateSpine

FILTERED_DOMAINS: frozenset[str] = frozenset({
    "Assessment",
    "AssessmentMetadata",
    "AssessmentRegistration",
    "StudentAssessment",
    "Survey",
    "Standards",
    "Gradebook",
    "Intervention",
})

# Reviewer-facing collapsed labels — one placeholder per (state, label).
_ASSESSMENT_LABEL = "Assessment"
_SURVEY_LABEL = "Survey"
_LEARNING_STANDARD_LABEL = "LearningStandard"
_GRADEBOOK_LABEL = "Gradebook"
_INTERVENTION_LABEL = "Intervention"

PLACEHOLDER_LABELS: tuple[str, ...] = (
    _ASSESSMENT_LABEL,
    _SURVEY_LABEL,
    _LEARNING_STANDARD_LABEL,
    _GRADEBOOK_LABEL,
    _INTERVENTION_LABEL,
)

_EXPLICIT_ENTITY_FILTERS: dict[str, str] = {
    # Standards-concept entities — tagged with many adjacent domains but
    # SIS vendors do not populate learning-standard structure.
    "LearningStandard": _LEARNING_STANDARD_LABEL,
    "LearningStandardEquivalenceAssociation": _LEARNING_STANDARD_LABEL,
    # Gradebook-concept entities — tagged with StudentAcademicRecord as
    # well, but the gradebook reporting stream itself is SIS-unpopulated.
    "GradebookEntry": _GRADEBOOK_LABEL,
    "StudentGradebookEntry": _GRADEBOOK_LABEL,
    # Intervention-concept entities — tagged with StudentCohort as a
    # secondary domain; stakeholder view is that SIS vendors do not
    # populate the intervention reporting surface.
    "Cohort": _INTERVENTION_LABEL,
    "Intervention": _INTERVENTION_LABEL,
    "StudentCohortAssociation": _INTERVENTION_LABEL,
    "StudentInterventionAssociation": _INTERVENTION_LABEL,
}


_FALLBACK_PREFIXES: tuple[tuple[str, str], ...] = (
    # Order matters — longer prefixes first so `ObjectiveAssessment`
    # matches before the bare `Assessment` prefix would.
    ("ObjectiveAssessment", _ASSESSMENT_LABEL),
    ("StudentAssessment", _ASSESSMENT_LABEL),
    ("AssessmentItem", _ASSESSMENT_LABEL),
    ("Assessment", _ASSESSMENT_LABEL),
    ("Survey", _SURVEY_LABEL),
    ("LearningStandard", _LEARNING_STANDARD_LABEL),
    ("Gradebook", _GRADEBOOK_LABEL),
    ("Intervention", _INTERVENTION_LABEL),
)


def _collapse_label(domains: list[str]) -> str:
    s = set(domains)
    if s & {"Assessment", "AssessmentMetadata", "AssessmentRegistration", "StudentAssessment"}:
        return _ASSESSMENT_LABEL
    if "Survey" in s:
        return _SURVEY_LABEL
    if "Standards" in s:
        return _LEARNING_STANDARD_LABEL
    if "Gradebook" in s:
        return _GRADEBOOK_LABEL
    if "Intervention" in s:
        return _INTERVENTION_LABEL
    return domains[0]


# Map a filtered entity's evidence (declared Ed-Fi domains, or — for
# domain-less extension entities — its name prefix) to the `domain_scope`
# registry keys under which it can be re-enabled. Only the Assessment family
# is registry-gateable; Survey / Standards / Gradebook / Intervention are not
# in `domain_scope.NEW_DOMAINS`, so they are never un-filtered.
_ASSESSMENT_REGISTRY_DOMAINS: frozenset[str] = frozenset(
    {"Assessment", "AssessmentMetadata", "StudentAssessment"}
)
_ASSESSMENT_REGISTRY_PREFIXES: tuple[str, ...] = (
    "ObjectiveAssessment",
    "StudentAssessment",
    "AssessmentItem",
    "Assessment",
)


def _enablement_keys(entity_name: str, spine: StateSpine) -> set[str]:
    """Registry domain keys (`domain_scope.NEW_DOMAINS`) this entity could be
    re-enabled under. AssessmentRegistration is kept distinct from the broader
    Assessment family so a state can enable one without the other.

    Eligibility mirrors `_raw_filter_label`'s decision path so enablement only
    affects entities that were filtered *as* Assessment:

    - Explicit concept-anchor entities (LearningStandard, GradebookEntry,
      Cohort, Intervention, …) are filtered by identity, not domain. They are
      never registry-enableable — even though some (e.g. LearningStandard)
      carry an `Assessment` domain tag among several non-filtered domains, a
      tag that must NOT leak them back into the spine lens when Assessment is
      enabled.
    - Domain-tagged entities are eligible only when the domain-subset rule
      would have filtered them (`domains ⊆ FILTERED_DOMAINS`).
    - Domain-less extension entities fall back to the assessment name prefixes.
    """
    if entity_name in _EXPLICIT_ENTITY_FILTERS:
        return set()
    entity = spine.catalog.entities.get(entity_name)
    domains = set(entity.domains) if entity and entity.domains else set()
    keys: set[str] = set()
    if domains:
        if domains <= FILTERED_DOMAINS:
            if domains & _ASSESSMENT_REGISTRY_DOMAINS:
                keys.add("Assessment")
            if "AssessmentRegistration" in domains:
                keys.add("AssessmentRegistration")
    elif entity_name.startswith(_ASSESSMENT_REGISTRY_PREFIXES):
        keys.add("Assessment")
    return keys


def _raw_filter_label(entity_name: str, spine: StateSpine) -> str | None:
    """The unconditional collapse label for `entity_name`, ignoring per-state
    enablement.

    Lookup order:

    1. Explicit concept-anchor list — for entities Ed-Fi spreads across
       several domains but whose name and purpose belong to a filtered
       concept (LearningStandard, GradebookEntry, Intervention, etc.).
    2. Domain-subset rule — the entity's declared domains are all in
       `FILTERED_DOMAINS`.
    3. Entity-name prefix fallback — for extension entities that have no
       registered domains in the committed domain map.
    """
    if entity_name in _EXPLICIT_ENTITY_FILTERS:
        return _EXPLICIT_ENTITY_FILTERS[entity_name]
    entity = spine.catalog.entities.get(entity_name)
    domains = list(entity.domains) if entity and entity.domains else []
    if domains and set(domains) <= FILTERED_DOMAINS:
        return _collapse_label(domains)
    if not domains:
        for prefix, label in _FALLBACK_PREFIXES:
            if entity_name.startswith(prefix):
                return label
    return None


def entity_filter_domain(entity_name: str, spine: StateSpine) -> str | None:
    """Return the reviewer-facing domain label if `entity_name` should be
    collapsed out of the spine lens, else None.

    Computes the unconditional collapse label (`_raw_filter_label`) and then
    applies the per-state un-filter: if any registry key the entity could be
    enabled under is registered for `spine.state` in `domain_scope`, the
    entity stays in the spine lens (return None). With an empty
    `domain_scope.DOMAIN_SOURCES` this is always a no-op, so behavior is
    byte-identical to the pre-registry filter.
    """
    label = _raw_filter_label(entity_name, spine)
    if label is None:
        return None
    if any(is_domain_enabled(spine.state, key) for key in _enablement_keys(entity_name, spine)):
        return None
    return label


def placeholder_note(domain_label: str) -> str:
    return (
        f"SIS vendors typically do not populate the {domain_label} domain "
        "in real deployments. The spine-lens artifact collapses undocumented "
        f"{domain_label} spine positions into this single placeholder row; "
        f"any rows the state explicitly documented in the {domain_label} "
        "domain are retained inline above as real signals of state intent. "
        "Source-lens artifacts also keep state-documented rows verbatim as "
        "an audit trail."
    )
