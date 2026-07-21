"""Root conftest — repo-wide test guards.

Deliberately tiny: the suite is hermetic by convention (fixtures build
their own artifacts under tmp_path); this file only makes the two
historical environment traps loud instead of mysterious.
"""

from __future__ import annotations

from pathlib import Path

import src

_REPO_SRC = Path(__file__).resolve().parent.parent / "src"


def pytest_sessionstart(session) -> None:  # noqa: ANN001 - pytest hook
    """Fail fast if ``poc3`` resolves anywhere but ``src/``.

    A stale copy in ``.venv/lib/python3.12/site-packages/poc3/`` (the
    "shadow-install trap") used to make tests import old code with no
    signal. ``[tool.pytest.ini_options] pythonpath = ["src"]`` should
    always win; if it doesn't, stop the run with an instructive error.
    """
    resolved = Path(src.__file__).resolve()
    if not resolved.is_relative_to(_REPO_SRC):
        raise RuntimeError(
            f"poc3 imported from {resolved}, not {_REPO_SRC} — a shadow "
            "install is masking the source tree. Fix: rm -rf "
            ".venv/lib/python3.12/site-packages/poc3/"
        )
