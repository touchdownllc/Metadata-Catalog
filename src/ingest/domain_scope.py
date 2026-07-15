"""Per-(state, domain) source-documentation registry.

Single source of truth for the Ed-Fi domains POC-3 has *additional* state
source documentation for, and where that documentation lives. Adding a new
state-resource link is one ``DomainSource`` entry in ``DOMAIN_SOURCES`` — the
domain filter, the state adapters, and the analyst scope narrative all read
this registry rather than carrying their own hard-coded per-domain maps.

The four domains this registry governs (`NEW_DOMAINS`):

- ``Staff`` / ``Finance`` — already flow through both lenses today (they are
  not in ``domain_filter.FILTERED_DOMAINS``). A registry entry here only
  records *where* a state's new Staff/Finance documentation lives so the
  relevant adapter can ingest it; it does not change any filter behavior.
- ``Assessment`` / ``AssessmentRegistration`` — collapsed out of the spine
  lens by ``domain_filter`` today. A registry entry for one of these lifts
  the placeholder collapse **for that (state, domain) pair only**, so the
  domain joins that state's spine coverage denominator. The two are separate
  keys so a state can enable one without the other.

**Empty-registry invariant.** With ``DOMAIN_SOURCES`` empty, every consumer
behaves exactly as it did before this module existed: ``domain_filter`` still
collapses Assessment/AssessmentRegistration, and adapters ingest only their
default source documents. The registry is purely additive.

``AssessmentRegistration`` is a newer Ed-Fi domain and is **not present in
every state's deployed swagger** — it is in the AZ/WI/IN spines but absent
from MN/TX. Enabling it for a state whose spine lacks those entities yields
source-lens rows only (no spine coverage to populate); see the working
journal / ADR 0007.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Domains this registry governs. ``domain`` on every ``DomainSource`` must be
#: one of these. Assessment-family keys gate the spine-lens filter; Staff and
#: Finance are already unfiltered and are listed here only so their source
#: documentation can be registered uniformly.
NEW_DOMAINS: tuple[str, ...] = (
    "Assessment",
    "AssessmentRegistration",
    "Staff",
    "Finance",
)


@dataclass(frozen=True)
class DomainSource:
    """One state's source documentation for one newly-in-scope domain.

    Only the adapter-specific pointer fields relevant to the state's source
    type are populated; the rest stay empty. All collection fields are tuples
    (not lists/dicts) so the dataclass stays frozen/hashable and safe as a
    module-level default.

    ``enabled`` decouples *registered/documented* from *spine-lens enabled*.
    A registered-but-disabled (``enabled=False``) entry records that a state's
    documentation for a domain exists and where it lives, without lifting the
    spine-lens placeholder collapse — used to stage the Assessment /
    AssessmentRegistration un-filter behind the methodology sign-off
    (``is_domain_enabled`` returns False until it is flipped). Staff / Finance
    are not filtered, so ``enabled`` is immaterial for them (kept ``True``).

    Fields by adapter:

    - WI (Confluence): ``confluence_page_ids`` (extra crawl roots beyond the
      default top page) and ``page_title_entity_map`` (``(page_title, entity)``
      pairs that extend ``wisconsin._LEAF_DOMAIN_ENTITY_MAP``).
    - AZ / MN / IN (XLSX): ``xlsx_path`` (a new workbook, else the state's
      default file) and ``xlsx_sheets`` (sheet names to ingest — extends MN's
      ``_ELEMENT_SHEETS`` allowlist; informational for AZ/IN which auto-detect).
    - TX (TWEDS): ``tweds_entities`` (TEDS entity names to scrape).
    - ``url`` / ``notes``: provenance of the supplied link and free-text context.
    """

    state: str
    domain: str
    enabled: bool = True
    url: str = ""
    confluence_page_ids: tuple[str, ...] = ()
    page_title_entity_map: tuple[tuple[str, str], ...] = ()
    xlsx_path: str = ""
    xlsx_sheets: tuple[str, ...] = ()
    tweds_entities: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        if self.domain not in NEW_DOMAINS:
            raise ValueError(
                f"DomainSource.domain={self.domain!r} is not one of "
                f"NEW_DOMAINS={NEW_DOMAINS!r}"
            )
        if not self.state:
            raise ValueError("DomainSource.state must be non-empty")


#: The registry, seeded with the domain documentation already wired into the
#: state adapters (mined from code, 2026-06-29). Each entry records where a
#: state's source documentation for a domain lives.
#:
#: ``enabled`` tier:
#: - **Staff / Finance** — not in ``domain_filter.FILTERED_DOMAINS``; they
#:   already flow through both lenses today. ``enabled=True`` is immaterial to
#:   the filter (it never consults the registry for them) — these entries are
#:   pure provenance.
#: - **Assessment / AssessmentRegistration** — collapsed out of the spine lens
#:   today. Seeded with ``enabled=False`` so artifacts stay byte-identical;
#:   flipping any of these to ``enabled=True`` lifts that (state, domain)
#:   placeholder collapse and is the methodology change gated on the
#:   ``SCORING_PLAN_VERSION`` bump + ADR 0007 + Doug/Maria sign-off.
DOMAIN_SOURCES: tuple[DomainSource, ...] = (
    # ---- Staff (already flows; documentation-only) ----
    DomainSource(
        state="AZ", domain="Staff",
        notes="Use Case 12.0 .xlsm; Staff sheets via az_enrich._RULE_DOMAIN_TO_SHEETS. 38 documented rows.",
    ),
    DomainSource(
        state="IN", domain="Staff",
        url="https://idoe.atlassian.net/wiki/spaces/IKHTV/",
        notes="IDOE Confluence (space IKHTV) + Vendor Docs xlsx; 6 Staff page maps in idoe_confluence. 42 documented rows.",
    ),
    DomainSource(
        state="MN", domain="Staff",
        url="https://raw.githubusercontent.com/mn-mde-edfi/MDE-EdFi-Documentation/master/2026-27%20MDE%20Ed-Fi%20Documentation/2026-2027%20Data%20Mapping%20Matrix%20Ed-Fi%20Suite%203%20v%206.2.xlsx",
        notes="MDE Data Mapping Matrix; Staff rows arrive via FK refs in the Student Enrollment / MCCC sheets. 5 documented rows.",
    ),
    DomainSource(
        state="TX", domain="Staff",
        notes="TWEDS v33 (TEDS) scrape (data/raw/tx/tweds/). 82 documented rows.",
    ),
    DomainSource(
        state="WI", domain="Staff",
        url="https://wisconsindpi.atlassian.net/wiki/spaces/widpiedfi/pages/2294032",
        notes="DPI Confluence crawl (TOP_PAGE_ID=2294032); Staff demographics inline. 58 documented rows.",
    ),
    # ---- Finance (already flows; documentation-only) ----
    DomainSource(
        state="WI", domain="Finance",
        url="https://wisconsindpi.atlassian.net/wiki/spaces/widpiedfi/pages/2294032",
        notes="DPI Confluence — SAFR pages (chartOfAccount / LocalActual). 15 documented rows.",
    ),
    # ---- Assessment ----
    # TX + IN enabled under SCORING_PLAN_VERSION 27 (ADR 0007, 2026-06-29).
    DomainSource(
        state="TX", domain="Assessment", enabled=True,
        notes="TWEDS v33 (TEDS) assessment entities. 31 documented rows. "
              "Enabled v27 — lifts the TX spine-lens Assessment placeholder.",
    ),
    DomainSource(
        state="IN", domain="Assessment", enabled=True,
        url="https://idoe.atlassian.net/wiki/spaces/IKHTV/",
        notes="IDOE Confluence assessment-accommodation page. 3 documented rows. "
              "Enabled v27 — lifts the IN spine-lens Assessment placeholder.",
    ),
    DomainSource(
        state="WI", domain="Assessment", enabled=False,
        url="https://wisconsindpi.atlassian.net/wiki/spaces/widpiedfi/pages/2294032",
        page_title_entity_map=(("Assessment (Public LEAs Only)", "StudentAssessment"),),
        notes="DPI Confluence assessment page is mapped (wisconsin._LEAF_DOMAIN_ENTITY_MAP) "
              "but yields 0 documented pure-assessment rows today — investigate the page parse "
              "before enabling. Deliberately left disabled in the v27 enablement.",
    ),
    # ---- AssessmentRegistration ----
    DomainSource(
        state="IN", domain="AssessmentRegistration", enabled=True,
        url="https://idoe.atlassian.net/wiki/spaces/IKHTV/",
        notes="IDOE Confluence. 5 documented rows. AR entities present in the IN spine. "
              "Enabled v27 — lifts the IN spine-lens AssessmentRegistration collapse. "
              "(AR entities are absent from MN/TX spines, so AR is not enabled there.)",
    ),
)


#: States that deliberately carry NO ``DomainSource`` entries. Every
#: ``src.states.SUPPORTED_STATES`` member must either appear in
#: ``DOMAIN_SOURCES`` or be listed here explicitly —
#: ``tests/test_state_roster.py`` fails otherwise (issue #213 item 2), so a
#: sixth state can't ship in the "nobody decided about its domain
#: documentation" limbo silently. Empty today: all five states register at
#: least a Staff entry.
NO_DOMAIN_SOURCES: frozenset[str] = frozenset()


def sources_for(state: str) -> list[DomainSource]:
    """All registered ``DomainSource`` entries for ``state`` (case-insensitive),
    enabled or not — adapters use this to see provenance regardless of state."""
    s = state.upper()
    return [d for d in DOMAIN_SOURCES if d.state.upper() == s]


def is_domain_enabled(state: str, domain: str) -> bool:
    """True iff an **enabled** source link is registered for this (state,
    domain) pair.

    This is the gate ``domain_filter`` consults to decide whether to lift the
    spine-lens placeholder collapse for a filtered domain. Case-insensitive on
    state; ``domain`` is matched exactly against ``NEW_DOMAINS`` keys. A
    registered-but-disabled (``enabled=False``) entry returns False here.
    """
    s = state.upper()
    return any(
        d.state.upper() == s and d.domain == domain and d.enabled
        for d in DOMAIN_SOURCES
    )


def enabled_domains(state: str) -> frozenset[str]:
    """The set of ``NEW_DOMAINS`` keys **enabled** for ``state``."""
    return frozenset(d.domain for d in sources_for(state) if d.enabled)
