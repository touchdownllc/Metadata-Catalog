"""Regenerate the committed assessment-release JSON Schema.

DELIBERATE-refresh companion to ``score/release_contract.py`` (the
``refresh_workbook_fingerprints.py`` convention): run after any
envelope roster change, review the JSON diff, and commit it in the
same diff as the roster + ``SIDECAR_CONTRACT_VERSION`` change.
``tests/test_release_contract.py`` fails until the committed copy
matches the module.

Usage:  .venv/bin/python scripts/refresh_release_contract_schema.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.score.release_contract import build_json_schema  # noqa: E402

SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "contracts"
    / "assessment-release.schema.json"
)


def main() -> None:
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(
        json.dumps(build_json_schema(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
