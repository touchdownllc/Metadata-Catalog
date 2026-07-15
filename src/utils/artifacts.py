"""Loud JSON artifact reading (issue #212 item 3).

The high-traffic sidecar readers used to swallow ``OSError`` /
``JSONDecodeError`` into ``{}`` — a CORRUPT artifact (truncated write,
disk fault) rendered exactly like "scoring hasn't run", producing
workbooks full of blanks or silently degraded joins with zero warning.
Parse failures are not a policy decision the way missing files are
(tests and pre-publish flows legitimately run without sidecars);
corruption always deserves a raise.

One helper so every reader raises the same instructive error; the Pri-3
``report/loaders.py`` consolidation (issue #213) can absorb this as the
single policy home later.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ArtifactReadError(RuntimeError):
    """An artifact exists but cannot be read/parsed — corruption, not
    absence. Never fall back to the missing-file behavior."""


def read_json_artifact(path: Path, *, consumer: str) -> Any:
    """Parse a JSON artifact, raising :class:`ArtifactReadError` on a
    corrupt or unreadable file.

    Missing-file policy stays with the caller (check ``path.exists()``
    first when absence is legitimate) — this helper only guarantees
    that a file which IS there never silently reads as empty.
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactReadError(
            f"{path} is unreadable or corrupt ({type(exc).__name__}: "
            f"{exc}) — {consumer} refuses to treat a corrupt artifact "
            f"as 'not produced'. Regenerate it (poc3 publish re-runs "
            f"the producing stage; see the publish manifest for its "
            f"producer)."
        ) from exc
