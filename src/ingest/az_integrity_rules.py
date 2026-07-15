"""Parse Arizona Integrity Rules PDFs into structured rule records.

Reads tabular PDFs from azed.gov that define business validation rules
applied to AzEDS data submissions. Each PDF covers a domain area
(ADM, ELL, SPED, etc.) identified by error code prefix.

Also handles the 58XXX Food Service Program rules which are in .docx format.
"""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import click
import pdfplumber


# Error code prefix -> domain mapping
_DOMAIN_MAP = {
    "10": "ADM",
    "20": "ADM",
    "21": "Accountability",
    "30": "ELL",
    "40": "SPED",
    "50": "Support Programs",
    "51": "Data Quality",
    "52": "Discipline",
    "57": "Gifted",
    "58": "Food Service",
    "59": "Homeless",
    "60": "STC",
    "70": "FRPL",
    "80": "DRP",
    "81": "PCCP",
    "90": "Calendar",
}


@dataclass
class IntegrityRule:
    """A single integrity validation rule from the AzEDS rules documents."""

    error_code: str
    description: str
    message: str
    severity: str
    active_current_year: bool
    source_document: str
    domain_category: str


def _infer_domain(error_code: str) -> str:
    """Map error code to domain category."""
    code = error_code.strip()
    # Try 2-char prefix first, then 1-char
    for prefix_len in (2, 1):
        prefix = code[:prefix_len]
        if prefix in _DOMAIN_MAP:
            return _DOMAIN_MAP[prefix]
    return "Unknown"


def _clean_text(text: str | None) -> str:
    """Clean extracted PDF text: normalize whitespace, strip."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def parse_integrity_rules_pdf(pdf_path: Path) -> list[IntegrityRule]:
    """Parse a single Integrity Rules PDF into IntegrityRule records.

    Each page contains a table with columns:
    - Col 0: Error Code
    - Col 1: Description (the business rule)
    - Col 2: Message
    - Col 3: Severity (Error/Warning)
    - Col 4: Comments
    - Col 5+: Active status per fiscal year
    """
    rules: list[IntegrityRule] = []
    source = pdf_path.name

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or len(row) < 5:
                        continue

                    error_code = _clean_text(row[0])

                    # Skip header rows and non-data rows
                    if not error_code or not error_code[0].isdigit():
                        continue

                    description = _clean_text(row[1])
                    message = _clean_text(row[2])
                    severity = _clean_text(row[3])
                    # Active status: col 5 contains "Active" or "Inactive" for current year
                    active_text = _clean_text(row[5]) if len(row) > 5 else ""
                    active = active_text.lower() == "active"

                    if description:  # Skip rows with empty descriptions
                        rules.append(IntegrityRule(
                            error_code=error_code,
                            description=description,
                            message=message,
                            severity=severity,
                            active_current_year=active,
                            source_document=source,
                            domain_category=_infer_domain(error_code),
                        ))

    return rules


def parse_integrity_rules_docx(docx_path: Path) -> list[IntegrityRule]:
    """Parse the Food Service (58XXX) rules from .docx format."""
    from docx import Document

    rules: list[IntegrityRule] = []
    source = docx_path.name

    doc = Document(docx_path)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if len(cells) < 4:
                continue

            error_code = cells[0]
            if not error_code or not error_code[0].isdigit():
                continue

            description = _clean_text(cells[1])
            message = _clean_text(cells[2]) if len(cells) > 2 else ""
            severity = _clean_text(cells[3]) if len(cells) > 3 else ""
            active_text = _clean_text(cells[5]) if len(cells) > 5 else ""
            active = active_text.lower() == "active"

            if description:
                rules.append(IntegrityRule(
                    error_code=error_code,
                    description=description,
                    message=message,
                    severity=severity,
                    active_current_year=active,
                    source_document=source,
                    domain_category=_infer_domain(error_code),
                ))

    return rules


def parse_all_integrity_rules(input_dir: Path) -> list[IntegrityRule]:
    """Parse all integrity rules files from the input directory.

    Looks for PDFs and DOCX files matching integrity rules naming patterns.
    """
    all_rules: list[IntegrityRule] = []

    # Find integrity rules files (they contain "xxx" or "XXX" or "Integrity" in name)
    for f in sorted(input_dir.iterdir()):
        if not f.is_file():
            continue

        name_lower = f.name.lower()
        is_integrity = (
            "xxx" in name_lower
            or "integrity" in name_lower
            or "rules" in name_lower
        )
        # Skip non-integrity files
        if not is_integrity:
            continue
        # Skip the vendor cert package and other non-rule files
        if "vendor" in name_lower or "use case" in name_lower:
            continue

        if f.suffix.lower() == ".pdf":
            rules = parse_integrity_rules_pdf(f)
            all_rules.extend(rules)
        elif f.suffix.lower() == ".docx":
            rules = parse_integrity_rules_docx(f)
            all_rules.extend(rules)

    return all_rules


def save_rules(rules: list[IntegrityRule], output_path: Path) -> None:
    """Save parsed rules to JSON."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([asdict(r) for r in rules], indent=2),
        encoding="utf-8",
    )


@click.command()
@click.option(
    "--input-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory containing integrity rules PDFs/DOCX files",
)
@click.option(
    "--output",
    default="data/input/arizona/az_integrity_rules.json",
    type=click.Path(path_type=Path),
    help="Output JSON file path",
)
def main(input_dir: Path, output: Path) -> None:
    """Parse all AZ Integrity Rules and produce a JSON catalog."""
    click.echo(f"Parsing integrity rules from: {input_dir}")

    rules = parse_all_integrity_rules(input_dir)

    click.echo(f"  Total rules parsed: {len(rules)}")

    # Summary by domain
    domains: dict[str, int] = {}
    for r in rules:
        domains[r.domain_category] = domains.get(r.domain_category, 0) + 1
    for domain, count in sorted(domains.items()):
        active = sum(1 for r in rules if r.domain_category == domain and r.active_current_year)
        click.echo(f"    {domain}: {count} rules ({active} active)")

    # Severity breakdown
    errors = sum(1 for r in rules if r.severity.lower() == "error")
    warnings = sum(1 for r in rules if r.severity.lower() == "warning")
    click.echo(f"  Errors: {errors}, Warnings: {warnings}")

    save_rules(rules, output)
    click.echo(f"  Saved: {output}")


if __name__ == "__main__":
    main()
