"""Enrich AZ elements with business rules from Integrity Rules.

Maps parsed integrity rules to elements by domain affinity and keyword matching.
Populates the business_rules_text field on each ElementRecord.

Critical difference from prior POC: rules that don't match a specific element
are DROPPED, not broadcast to all elements in the domain. This trades coverage
for precision — better to have fewer, relevant rules than irrelevant ones
polluting the evidence extraction prompt.
"""

import json
from pathlib import Path

from src.models.element import ElementRecord

# Map integrity rule domains to Use Case sheet names (= element.domain values)
_RULE_DOMAIN_TO_SHEETS = {
    "ADM": [
        "Student Enrollment", "Student Withdrawal", "Student Demographics",
        "Student Attendance", "Learning Modality", "School Calendar",
    ],
    "Accountability": ["Student Enrollment", "Student Demographics"],
    "ELL": ["ELL Student Program"],
    "SPED": ["SPED Program"],
    "Support Programs": ["Support Program"],
    "Data Quality": [
        "Student Enrollment", "Student Demographics", "Staff",
        "Student Attendance", "Student Transcript",
    ],
    "Discipline": ["Discipline"],
    "Gifted": ["Support Program"],
    "Food Service": ["Food Service Program (NSLP)"],
    "Homeless": ["Homeless Program"],
    "STC": ["Student Transcript", "Master Schedule", "Staff"],
    "FRPL": ["Food Service Program (NSLP)"],
    "DRP": ["Dropout Recovery Program"],
    "PCCP": ["Student Transcript"],
    "Calendar": ["School Calendar"],
}

# Keywords that map to specific element names (AZ-specific abbreviations)
_KEYWORD_TO_ELEMENTS = {
    "DOA": ["SchoolID", "EducationOrganizationId"],
    "DOR": ["LocalEducationAgencyID", "ResidentDistrictID"],
    "FTE": ["MembershipFTEDescriptorID", "FTEStartDate", "FTEEndDate"],
    "grade": ["EntryGradeLevelDescriptorID", "GradeLevelDescriptorId"],
    "enrollment": ["EntryDate", "ExitWithdrawDate", "EntryTypeDescriptorID"],
    "withdrawal": ["ExitWithdrawDate", "ExitWithdrawTypeDescriptorID"],
    "attendance": ["AttendanceEventCategoryDescriptorID", "EventDate", "EventDuration"],
    "calendar": ["CalendarCode", "CalendarTypeDescriptorId"],
    "membership": ["MembershipTypeDescriptorID"],
    "tuition": ["TuitionPayerDescriptorID"],
    "special enrollment": ["SpecialEnrollmentDescriptorID"],
    "CEC": ["SpecialEnrollmentDescriptorID"],
    "birth": ["BirthDate", "BirthCountryDescriptorId", "BirthStateAbbreviationDescriptorId"],
    "discipline": ["IncidentIdentifier", "BehaviorDescriptorId"],
    "course": ["CourseCode", "LocalCourseCode"],
    "section": ["SectionIdentifier", "LocalCourseCode"],
    "staff": ["StaffUniqueId", "StaffClassificationDescriptorId"],
    "session": ["SessionName", "TermDescriptorID"],
    "program": ["ProgramName", "ProgramTypeDescriptorId"],
    "need": ["NeedDescriptorId"],
    "service": ["ServiceDescriptorId"],
}


def _find_matching_elements(
    rule_desc: str,
    domain_elements: list[ElementRecord],
) -> list[str]:
    """Find element names that match keywords in a rule description."""
    desc_lower = rule_desc.lower()
    matched: set[str] = set()

    # Direct element name matching
    for elem in domain_elements:
        if elem.element_name.lower() in desc_lower:
            matched.add(elem.element_name)

    # Keyword-based matching
    for keyword, element_names in _KEYWORD_TO_ELEMENTS.items():
        if keyword.lower() in desc_lower:
            domain_names = {e.element_name for e in domain_elements}
            for ename in element_names:
                if ename in domain_names:
                    matched.add(ename)

    return sorted(matched)


def enrich_records_with_rules(
    records: list[ElementRecord],
    rules_path: Path,
) -> list[ElementRecord]:
    """Populate business_rules_text from AZ integrity rules JSON.

    Only attaches rules that match a specific element by name or keyword.
    Rules that don't match any element are dropped (no broadcast fallback).

    Returns the same list of records (mutated in place) with business_rules_text
    populated where integrity rules matched.
    """
    if not rules_path.exists():
        return records

    rules = json.loads(rules_path.read_text(encoding="utf-8"))

    # Build domain index: domain -> list of records
    domain_index: dict[str, list[ElementRecord]] = {}
    for rec in records:
        domain_index.setdefault(rec.domain, []).append(rec)

    # Accumulate rule texts per (domain, element_name)
    rules_by_element: dict[tuple[str, str], list[str]] = {}

    for rule in rules:
        rule_domain = rule["domain_category"]
        sheets = _RULE_DOMAIN_TO_SHEETS.get(rule_domain, [])

        for sheet in sheets:
            domain_elements = domain_index.get(sheet, [])
            if not domain_elements:
                continue

            matched_elements = _find_matching_elements(
                rule["description"], domain_elements
            )

            if matched_elements:
                rule_text = f"[Rule {rule['error_code']}] {rule['description']}"
                for ename in matched_elements:
                    key = (sheet, ename)
                    rules_by_element.setdefault(key, []).append(rule_text)
            # No else branch — unmatched rules are dropped, NOT broadcast

    # Apply enrichment
    for rec in records:
        key = (rec.domain, rec.element_name)
        if key in rules_by_element:
            rule_texts = rules_by_element[key]
            unique_rules = list(dict.fromkeys(rule_texts))
            existing = rec.business_rules_text or ""
            if existing:
                rec.business_rules_text = existing + "\n" + "\n".join(unique_rules)
            else:
                rec.business_rules_text = "\n".join(unique_rules)

    return records
