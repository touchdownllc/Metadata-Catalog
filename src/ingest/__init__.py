"""Per-state ingestion adapters.

``INGEST_MODULES`` is the one state→adapter-module registry (issue #213
item 3): ``cli.py`` generates the five ``mc ingest <state>`` commands
from it and ``publish/stages`` derives its stage fanout from it, so a
sixth state is registered exactly once. Keys are the canonical state
codes from :data:`src.states.SUPPORTED_STATES`; values are module
names under ``src.ingest`` whose module-level ``run()`` is the
adapter entry point (plain function — never ``@click.command``; see
CLAUDE.md).

This module deliberately does NOT import the adapters — the CLI's
lazy-import convention keeps ``mc --help`` fast.
"""

from __future__ import annotations

INGEST_MODULES: dict[str, str] = {
    "AZ": "arizona",
    "WI": "wisconsin",
    "MN": "minnesota",
    "TX": "texas",
    "IN": "indiana",
}
