"""Unit tests for the per-(state, domain) source-documentation registry."""

from __future__ import annotations

import pytest

from src.ingest import domain_scope
from src.ingest.domain_scope import (
    DOMAIN_SOURCES,
    NEW_DOMAINS,
    DomainSource,
    enabled_domains,
    is_domain_enabled,
    sources_for,
)


def test_registry_is_seeded():
    assert len(DOMAIN_SOURCES) > 0
    # Every seeded domain is a recognized NEW_DOMAINS key.
    assert {d.domain for d in DOMAIN_SOURCES} <= set(NEW_DOMAINS)


def test_assessment_enablement_matches_v27_scope():
    # SCORING_PLAN_VERSION 27 (ADR 0007): TX + IN Assessment and IN
    # AssessmentRegistration are enabled; WI Assessment stays disabled (its
    # mapped page yields 0 documented rows). MN/AZ have no assessment docs.
    assert is_domain_enabled("TX", "Assessment") is True
    assert is_domain_enabled("IN", "Assessment") is True
    assert is_domain_enabled("IN", "AssessmentRegistration") is True
    assert is_domain_enabled("WI", "Assessment") is False
    assert is_domain_enabled("AZ", "Assessment") is False
    assert is_domain_enabled("MN", "Assessment") is False
    # AR is only meaningful where the spine carries those entities (AZ/WI/IN);
    # it is not enabled for MN/TX.
    for st in ("AZ", "WI", "MN", "TX"):
        assert is_domain_enabled(st, "AssessmentRegistration") is False


def test_staff_finance_ship_enabled_documentation_only():
    # Staff/Finance are not filtered, so enabling them is immaterial to the
    # filter; they ship enabled as pure provenance.
    for d in DOMAIN_SOURCES:
        if d.domain in ("Staff", "Finance"):
            assert d.enabled is True
    assert is_domain_enabled("WI", "Finance") is True
    assert is_domain_enabled("TX", "Staff") is True


def test_new_domains_membership():
    assert NEW_DOMAINS == ("Assessment", "AssessmentRegistration", "Staff", "Finance")


def test_domain_source_rejects_unknown_domain():
    with pytest.raises(ValueError):
        DomainSource(state="WI", domain="Survey")


def test_domain_source_rejects_empty_state():
    with pytest.raises(ValueError):
        DomainSource(state="", domain="Assessment")


def test_domain_source_is_frozen():
    src = DomainSource(state="WI", domain="Finance")
    with pytest.raises(Exception):
        src.state = "MN"  # type: ignore[misc]


def test_helpers_against_a_patched_registry(monkeypatch):
    registry = (
        DomainSource(
            state="WI",
            domain="Assessment",
            url="https://example/wi-assessment",
            page_title_entity_map=(("Assessment (Public LEAs Only)", "StudentAssessment"),),
        ),
        DomainSource(state="WI", domain="Finance", xlsx_sheets=("SAFR",)),
        DomainSource(state="MN", domain="Staff", xlsx_sheets=("Staff Elements",)),
        # Registered but staged off — present in sources_for, absent from the gate.
        DomainSource(state="TX", domain="AssessmentRegistration", enabled=False),
    )
    monkeypatch.setattr(domain_scope, "DOMAIN_SOURCES", registry)

    # is_domain_enabled — case-insensitive on state, exact on domain.
    assert is_domain_enabled("WI", "Assessment") is True
    assert is_domain_enabled("wi", "Finance") is True
    assert is_domain_enabled("WI", "Staff") is False
    assert is_domain_enabled("MN", "Staff") is True
    assert is_domain_enabled("AZ", "Assessment") is False
    # Disabled entry: registered but the gate stays closed.
    assert is_domain_enabled("TX", "AssessmentRegistration") is False

    # sources_for — returns entries regardless of enabled state.
    wi = sources_for("wi")
    assert {s.domain for s in wi} == {"Assessment", "Finance"}
    assert {s.domain for s in sources_for("TX")} == {"AssessmentRegistration"}

    # enabled_domains — enabled entries only.
    assert enabled_domains("WI") == frozenset({"Assessment", "Finance"})
    assert enabled_domains("MN") == frozenset({"Staff"})
    assert enabled_domains("TX") == frozenset()
    assert enabled_domains("AZ") == frozenset()
