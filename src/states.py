"""Canonical state roster — the ONE place the five-state list lives.

Everything else (CLI ``Choice`` lists, ``--state all`` expansions,
per-module ``_STATES`` defaults, the publish orchestrator) derives from
this tuple. Dependency-free by design so ``cli.py`` can import it at
module top without dragging in heavy modules.

``STATE_INFO`` (issue #213 item 2) carries the cheap per-state descriptors
that used to hide as literals in other modules — the class of literal that
silently misses a sixth state (the IN ``idoe`` episode: reviewer-file joins
sat at 18.2% until ``matching._STATE_EXT_PREFIX_RE`` learned the prefix).
Consumers DERIVE from this registry (``utils.matching._STATE_EXT_PREFIX_RE``,
``ingest.normalize._NS_PREFIX_RE``); registries too rich to derive
(``spine.fetch._SANDBOX_URLS``, ``ingest.domain_scope.DOMAIN_SOURCES``) are
pinned to the roster by ``tests/test_state_roster.py`` coverage guards.
"""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_STATES: tuple[str, ...] = ("AZ", "WI", "MN", "TX", "IN")


@dataclass(frozen=True)
class StateInfo:
    """Cheap, dependency-free per-state descriptors.

    ``ext_prefixes`` — the extension-namespace prefix token(s) this state's
    spine / source docs / reviewer files emit on extension (and sometimes
    core) entity names, WITHOUT the separator (``_``, ``/`` or ``.``):
    ``tx_studentApplication``, ``idoe/educationOrganizationOtherPersonnel``,
    ``wi.something``. Regex-safe literals only — anything needing regex
    syntax (e.g. the core-namespace ``ed-?fi``) stays an explicit extra
    token at the consuming derivation site, not here.
    """

    code: str
    ext_prefixes: tuple[str, ...]


#: Per-state descriptor registry. Keys MUST cover ``SUPPORTED_STATES``
#: exactly (enforced at import below + pinned in tests/test_state_roster.py)
#: so a sixth state fails loudly here instead of silently missing from the
#: derived regexes.
STATE_INFO: dict[str, StateInfo] = {
    "AZ": StateInfo(code="AZ", ext_prefixes=("az",)),
    "WI": StateInfo(code="WI", ext_prefixes=("wi",)),
    "MN": StateInfo(code="MN", ext_prefixes=("mn",)),
    "TX": StateInfo(code="TX", ext_prefixes=("tx",)),
    # IN's extension namespace is the agency acronym, not the postal code —
    # `in_`/`in.` never appears in IDOE artifacts, and adding it to the
    # prefix whitelist would over-strip legitimate names.
    "IN": StateInfo(code="IN", ext_prefixes=("idoe",)),
}

if tuple(STATE_INFO) != SUPPORTED_STATES:  # pragma: no cover — import-time guard
    raise RuntimeError(
        "STATE_INFO keys must match SUPPORTED_STATES exactly (order included) "
        f"— got {tuple(STATE_INFO)!r} vs {SUPPORTED_STATES!r}. Add/remove the "
        "StateInfo entry alongside the roster change."
    )
