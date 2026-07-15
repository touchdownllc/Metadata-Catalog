"""Live Swagger fetcher for state Ed-Fi sandboxes.

Writes raw resources.json + descriptors.json + meta.json sidecar to
data/raw/{state_lower}/swagger/. Meta sidecar carries source URL and
fetched_at timestamp for later provenance.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# `fetch.py` lives at `src/spine/fetch.py`; project root is two levels up.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Sandbox URLs per state. {school_year} gets substituted for WI.
#
# TX points to the local TSDS SDK Docker ODS — there is no public TX sandbox.
# The committed compose bundle (infra/tsds-sdk/) exposes the WebAPI on host
# port 26030; `mc publish` passes this same base explicitly
# (publish/stages.DEFAULT_TX_BASE_URL). The URL is a default only; pass
# `--base-url` to override when the local port differs. See
# docs/archive/tx-ingestion-plan.md Phase 6.0-6.1. (Issue #213 item 2: this
# default previously said port 8080, which no committed compose file has ever
# served — dead-but-wrong.)
#
# Roster coverage: every SUPPORTED_STATES member must have an entry here —
# pinned by tests/test_state_roster.py so a sixth state can't be silently
# unfetchable.
_SANDBOX_URLS: dict[str, dict[str, str]] = {
    "AZ": {
        "resources": "https://sandbox-rest-api-r12.azeds.azed.gov/metadata/data/v3/resources/swagger.json",
        "descriptors": "https://sandbox-rest-api-r12.azeds.azed.gov/metadata/data/v3/descriptors/swagger.json",
    },
    "WI": {
        "resources": "https://as-edfiwebapiv7-uat.azurewebsites.net/EdFiWebApiV7/{school_year}/metadata/data/v3/resources/swagger.json",
        "descriptors": "https://as-edfiwebapiv7-uat.azurewebsites.net/EdFiWebApiV7/{school_year}/metadata/data/v3/descriptors/swagger.json",
    },
    "MN": {
        "resources": "https://test.api.education.mn.gov/edfiapi/metadata/data/v3/resources/swagger.json",
        "descriptors": "https://test.api.education.mn.gov/edfiapi/metadata/data/v3/descriptors/swagger.json",
    },
    "TX": {
        "resources": "http://localhost:26030/metadata/data/v3/resources/swagger.json",
        "descriptors": "http://localhost:26030/metadata/data/v3/descriptors/swagger.json",
    },
    "IN": {
        "resources": "https://dataexchangevendor.doe.in.gov/{school_year}/metadata/data/v3/resources/swagger.json",
        "descriptors": "https://dataexchangevendor.doe.in.gov/{school_year}/metadata/data/v3/descriptors/swagger.json",
    },
}


def _base_to_kind_url(base_url: str, kind: str) -> str:
    """Append `{kind}/swagger.json` to a base Ed-Fi metadata URL.

    Accepts either `http://host/metadata/data/v3` or the same path with a
    trailing slash — callers should provide the base up through `.../v3`.
    """
    return f"{base_url.rstrip('/')}/{kind}/swagger.json"


def sandbox_url(
    state: str,
    kind: str,
    school_year: int,
    base_url: str | None = None,
) -> str:
    """Return the concrete sandbox URL for a state + kind (resources/descriptors).

    When `base_url` is provided, it overrides the `_SANDBOX_URLS[state]` entry
    — the return value is `{base_url}/{kind}/swagger.json`. Used for TX where
    the local-Docker port may differ across developers.
    """
    state = state.upper()
    if base_url is not None:
        return _base_to_kind_url(base_url, kind)
    if state not in _SANDBOX_URLS:
        raise ValueError(f"No Ed-Fi sandbox URL configured for {state}")
    if kind not in _SANDBOX_URLS[state]:
        raise ValueError(f"No {kind} URL configured for {state}")
    return _SANDBOX_URLS[state][kind].replace("{school_year}", str(school_year))


def raw_swagger_dir(state: str) -> Path:
    return _PROJECT_ROOT / "data" / "raw" / state.lower() / "swagger"


def fetch_state_swagger(
    state: str,
    *,
    school_year: int = 2026,
    base_url: str | None = None,
    client: httpx.Client | None = None,
) -> Path:
    """Fetch resources + descriptors swagger for a state into data/raw/.../swagger/.

    When `base_url` is provided, it replaces the `_SANDBOX_URLS[state]` entry —
    used for TX (local Docker) where the host port may differ across developers.
    Pass the base up through `.../data/v3` (no `resources/swagger.json` tail).

    Returns the output directory path.
    """
    state = state.upper()
    if base_url is None and state not in _SANDBOX_URLS:
        raise ValueError(f"No sandbox URL configured for {state}")

    out_dir = raw_swagger_dir(state)
    out_dir.mkdir(parents=True, exist_ok=True)

    close_client = False
    if client is None:
        client = httpx.Client(timeout=120.0, follow_redirects=True)
        close_client = True

    fetched_at = datetime.now(tz=timezone.utc)
    urls_used: dict[str, str] = {}
    try:
        for kind in ("resources", "descriptors"):
            url = sandbox_url(state, kind, school_year, base_url=base_url)
            urls_used[kind] = url
            logger.info("Fetching %s %s: %s", state, kind, url)
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
            target = out_dir / f"{kind}.json"
            target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            logger.info("  wrote %s (%d bytes)", target.name, target.stat().st_size)
    finally:
        if close_client:
            client.close()

    template = (
        _SANDBOX_URLS.get(state, {}).get("resources", "")
        if base_url is None
        else ""
    )
    meta = {
        "state": state,
        "school_year": school_year if "{school_year}" in template else None,
        "fetched_at": fetched_at.isoformat(),
        "urls": urls_used,
        "base_url_override": base_url,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    logger.info("Wrote %s/meta.json", out_dir)

    return out_dir
