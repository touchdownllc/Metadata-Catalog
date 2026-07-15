"""Normalize entity names and data types to canonical forms.

Entity normalization ensures consistent naming across states so cross-state
comparison and Ed-Fi catalog lookup both work.

Data type normalization maps SQL/vendor-specific types (bigint, nvarchar)
to a canonical semantic vocabulary (Integer, String, Date, etc.).

Entity examples:
    edfi.Course              -> Course
    az.SectionExtension      -> Section       (resolves via catalog)
    az.CourseTranscriptExtention -> CourseTranscript (typo fixed, resolved)
    CourseTranscriptExt      -> CourseTranscript (Ext alias in catalog)
    wi_studentApplication    -> StudentApplication (prefix stripped, PascalCase)
    az.StudentNeed           -> StudentNeed   (no catalog match, kept as-is)

Data type examples:
    bigint                   -> Integer
    nvarchar(255)            -> String
    decimal(5,2)             -> Decimal
    bit                      -> Boolean
    Descriptor               -> Descriptor    (already canonical)
"""

import re

from src.models.edfi_catalog import EdFiCatalog
from src.states import STATE_INFO

# Namespace prefixes: edfi., az., AZ., or two-letter state prefix with underscore.
# `idoe` accepted for Indiana — the IDOE Vendor Documentation XLSX uses
# `idoe.SchoolExtension` style table names; the spine extension catalog uses
# the underscore form `idoe_schoolExtension`. Both flavors strip cleanly here.
#
# Issue #213 item 2: the generic `[A-Za-z]{2}` class already covers every
# two-letter state prefix, but longer agency prefixes (IN's `idoe`) were a
# hidden literal a sixth state would silently miss. They now DERIVE from
# `src.states.STATE_INFO`; `edfi` is the core namespace, kept as an explicit
# extra token. The derived pattern string is pinned byte-for-byte to the
# pre-derivation literal `^(?:edfi|idoe|[A-Za-z]{2})[._/]` in
# tests/test_normalize.py.
_LONG_STATE_PREFIXES: tuple[str, ...] = tuple(
    dict.fromkeys(
        p
        for info in STATE_INFO.values()
        for p in info.ext_prefixes
        if len(p) > 2  # two-letter prefixes ride the generic class below
    )
)
_NS_PREFIX_RE = re.compile(
    "^(?:" + "|".join(("edfi", *_LONG_STATE_PREFIXES, "[A-Za-z]{2}")) + ")[._/]"
)

# Common typo in AZ data
_TYPO_EXTENTION = re.compile(r"Extention$")

# Common typo in WI Confluence: `/calenderDates` instead of `/calendarDates`.
# Applied as a whole-word substring replacement so `CalenderDate`,
# `CalenderDates`, `CalenderEvent` etc. all resolve to the correct stem.
_TYPO_CALENDER = re.compile(r"Calender", re.IGNORECASE)

# Explicit entity-name corrections for state-source authoring slips that
# don't fit a generic regex. Each maps a state-source spelling to its
# canonical Ed-Fi entity. Applied after namespace-prefix stripping but
# before catalog lookup so the catalog hit succeeds. Currently:
#   - MN matrix consistently spells `StudentSection504Placement…` where the
#     Ed-Fi 4.0 entity is `StudentSection504Plan…`.
_ENTITY_NAME_FIXES: dict[str, str] = {
    "StudentSection504PlacementProgramAssociation":
        "StudentSection504PlanProgramAssociation",
    "StudentSection504PlacementProgramAssociationExtension":
        "StudentSection504PlanProgramAssociationExtension",
    # MN matrix consistently uses Arabic numeral `1` where Ed-Fi entities
    # use Roman numeral `I` (`StudentTitle1PartA…` -> `StudentTitleIPartA…`).
    "StudentTitle1PartAProgramAssociation":
        "StudentTitleIPartAProgramAssociation",
    "StudentTitle1PartAProgramAssociationExtension":
        "StudentTitleIPartAProgramAssociationExtension",
}


def normalize_entity(
    raw_name: str,
    catalog: EdFiCatalog | None = None,
) -> str:
    """Normalize a raw entity name to canonical PascalCase form.

    Steps:
        1. Strip namespace prefix (edfi., az., wi_, etc.)
        2. Fix known typos (Extention -> Extension, Calender -> Calendar)
        3. Ensure first character is uppercase (PascalCase)
        4. Apply explicit entity-name corrections (e.g. MN's `Section504Placement…`
           -> Ed-Fi `Section504Plan…`)
        5. Resolve through Ed-Fi catalog lookup index if available
        6. If no catalog match, try stripping Extension/Ext suffix and re-check
        7. Try depluralization (Ed-Fi API endpoints are plural, entities singular)
        8. Fall back to cleaned name
    """
    name = raw_name.strip()

    name = _NS_PREFIX_RE.sub("", name)
    name = _TYPO_EXTENTION.sub("Extension", name)
    name = _TYPO_CALENDER.sub("Calendar", name)

    # Collapse internal whitespace — the AZ Use Case XLSX has at least one
    # entity with an internal space (`StudentDropOut RecoveryProgramMonthly
    # Updates`) that otherwise can't match a catalog key (which are all
    # space-free PascalCase). `raw_entity` preserves the source form for
    # round-trip audit; only the display/lookup form is collapsed here.
    name = re.sub(r"\s+", "", name)

    if name and name[0].islower():
        name = name[0].upper() + name[1:]

    name = _ENTITY_NAME_FIXES.get(name, name)

    if catalog is None:
        return name

    canonical = catalog.lookup_index.get(name.lower())
    if canonical:
        return canonical

    for suffix in ("Extension", "Ext"):
        if name.endswith(suffix) and len(name) > len(suffix):
            base = name[: -len(suffix)]
            canonical = catalog.lookup_index.get(base.lower())
            if canonical:
                return canonical

    for suffix, repl in (("ies", "y"), ("ses", "s"), ("s", "")):
        if name.endswith(suffix) and len(name) > len(suffix):
            singular = name[: -len(suffix)] + repl
            canonical = catalog.lookup_index.get(singular.lower())
            if canonical:
                return canonical

    # No catalog hit. For extension-only entities (where no core catalog entry
    # exists), fall back to the depluralized form as the display entity — Ed-Fi
    # convention is singular entity names and the reviewer flagged
    # `StudentDropOut RecoveryProgramMonthlyUpdates` (plural) when the spine
    # uses `StudentDropOutRecoveryProgramMonthlyUpdate` (singular). Only the
    # simple trailing-`s` strip; more aggressive depluralization risks
    # mangling entities like `Students` that should stay intact.
    if (
        name.endswith("s")
        and not name.endswith("ss")
        and not name.endswith("us")
        and not name.endswith("is")
        and len(name) > 4
    ):
        return name[:-1]

    return name


# ---------------------------------------------------------------------------
# Data type normalization
# ---------------------------------------------------------------------------

# WI Confluence authors write types like "big integer" (space-separated),
# "Big integer", and the known typo "big nteger"; collections are written as
# "array". Match those BEFORE the bare `int|integer|bigint` pattern so
# "big integer" doesn't fall through to a stale pass-through when the
# leading word matches the generic pattern. Order matters.
_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(?:n?varchar|text|string)", re.IGNORECASE), "String"),
    (re.compile(r"^big\s*integer", re.IGNORECASE), "Integer"),
    (re.compile(r"^big\s*nteger", re.IGNORECASE), "Integer"),  # known WI typo
    (re.compile(r"^array\b", re.IGNORECASE), "Collection"),
    (re.compile(r"^(?:int|integer|bigint|smallint|tinyint)\b", re.IGNORECASE), "Integer"),
    (re.compile(r"^(?:decimal|dec|numeric)\b", re.IGNORECASE), "Decimal"),
    (re.compile(r"^(?:number)\b", re.IGNORECASE), "Number"),
    # TODO(#213 item 2, deferred): `datetime` deliberately folds into "Date".
    # The canonical vocabulary predates a DateTime bucket, and every
    # committed element golden/artifact was normalized under this fold — the
    # WI/MN/TX raw source caches aren't committed, so a `datetime` →
    # "DateTime" split can't be proven byte-inert against the live corpora
    # from a fresh clone. Splitting it requires a deliberate golden regen in
    # the same PR; don't change the fold casually.
    (re.compile(r"^(?:date|datetime)\b", re.IGNORECASE), "Date"),
    (re.compile(r"^(?:time)\b", re.IGNORECASE), "Time"),
    (re.compile(r"^(?:bit|boolean)\b", re.IGNORECASE), "Boolean"),
    (re.compile(r"^(?:descriptor)\b", re.IGNORECASE), "Descriptor"),
    (re.compile(r"^(?:reference)\b", re.IGNORECASE), "Reference"),
]


def normalize_data_type(raw_type: str | None) -> str | None:
    """Normalize a data type string to a canonical semantic form.

    Maps SQL/vendor types (bigint, nvarchar(255), dec(7,2)) to canonical
    Ed-Fi semantic types (Integer, String, Decimal). Unrecognized types
    are returned as-is after stripping whitespace.
    """
    if raw_type is None:
        return None

    cleaned = raw_type.strip()
    if not cleaned:
        return None

    for pattern, canonical in _TYPE_PATTERNS:
        if pattern.match(cleaned):
            return canonical

    return cleaned
