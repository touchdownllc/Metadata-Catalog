"""Tests for spine/fetch.py and the StateSpine build wrapper."""

from __future__ import annotations

import json

import httpx
import pytest

from src.spine import build as build_mod
from src.spine import fetch as fetch_mod
from src.spine.fetch import fetch_state_swagger, sandbox_url


class TestSandboxUrl:
    def test_az_does_not_need_school_year(self):
        url = sandbox_url("AZ", "resources", school_year=2026)
        assert "azeds.azed.gov" in url
        assert "{school_year}" not in url

    def test_wi_substitutes_school_year(self):
        url = sandbox_url("WI", "resources", school_year=2026)
        assert "EdFiWebApiV7/2026/metadata" in url
        assert "{school_year}" not in url

    def test_tx_points_at_local_docker(self):
        """TX uses the local TSDS SDK Docker ODS — the default base must
        match the committed compose bundle's WebAPI port (26030,
        infra/tsds-sdk/). Issue #213 item 2: the default previously said
        8080, which no committed compose file has ever served.
        See docs/archive/tx-ingestion-plan.md."""
        url = sandbox_url("TX", "resources", school_year=2026)
        assert url.startswith("http://localhost:26030/")
        assert "/metadata/data/v3/resources/swagger.json" in url

    def test_unknown_state_raises(self):
        # `XX` is never registered in _SANDBOX_URLS — a reliable
        # "unknown state" probe. (TX was previously the sentinel before
        # Phase 6 wired up local-Docker ingestion.)
        with pytest.raises(ValueError, match="No Ed-Fi sandbox URL configured"):
            sandbox_url("XX", "resources", school_year=2026)

    def test_base_url_override_bypasses_table(self):
        url = sandbox_url(
            "XX", "resources", school_year=2026,
            base_url="http://localhost:9999/metadata/data/v3",
        )
        assert url == "http://localhost:9999/metadata/data/v3/resources/swagger.json"


class TestFetchStateSwagger:
    def test_fetch_writes_resources_descriptors_and_meta(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fetch_mod, "_PROJECT_ROOT", tmp_path)

        fake_resources = {"openapi": "3.0.0", "info": {"version": "6.0"}, "paths": {}}
        fake_descriptors = {"openapi": "3.0.0", "info": {"version": "6.0"}, "paths": {}}

        def handler(request: httpx.Request) -> httpx.Response:
            if "resources" in request.url.path:
                return httpx.Response(200, json=fake_resources)
            if "descriptors" in request.url.path:
                return httpx.Response(200, json=fake_descriptors)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)

        out_dir = fetch_state_swagger("AZ", school_year=2026, client=client)

        assert (out_dir / "resources.json").exists()
        assert (out_dir / "descriptors.json").exists()
        assert (out_dir / "meta.json").exists()

        meta = json.loads((out_dir / "meta.json").read_text())
        assert meta["state"] == "AZ"
        assert meta["school_year"] is None  # AZ URL has no school_year template
        assert "urls" in meta and "resources" in meta["urls"]

        resources = json.loads((out_dir / "resources.json").read_text())
        assert resources["info"]["version"] == "6.0"

    def test_base_url_override_fetches_from_custom_host(self, tmp_path, monkeypatch):
        """Phase 6.1: `base_url` overrides the _SANDBOX_URLS table. Used for TX
        (local Docker) where the port may differ across developers."""
        monkeypatch.setattr(fetch_mod, "_PROJECT_ROOT", tmp_path)

        seen_hosts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_hosts.append(request.url.host)
            return httpx.Response(
                200, json={"openapi": "3.0.0", "info": {"version": "5.2"}}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        out_dir = fetch_state_swagger(
            "TX",
            base_url="http://localhost:9999/metadata/data/v3",
            client=client,
        )

        # Both fetches should hit the overridden host, not the default TX entry.
        assert seen_hosts == ["localhost", "localhost"]
        meta = json.loads((out_dir / "meta.json").read_text())
        assert meta["state"] == "TX"
        assert meta["base_url_override"] == "http://localhost:9999/metadata/data/v3"
        assert "localhost:9999" in meta["urls"]["resources"]

    def test_wi_school_year_recorded_in_meta(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fetch_mod, "_PROJECT_ROOT", tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"openapi": "3.0.0", "info": {"version": "5.2"}})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        out_dir = fetch_state_swagger("WI", school_year=2026, client=client)

        meta = json.loads((out_dir / "meta.json").read_text())
        assert meta["school_year"] == 2026
        assert "2026" in meta["urls"]["resources"]


class TestBuildStateSpine:
    def test_builds_spine_from_cached_swagger(self, tmp_path, monkeypatch):
        monkeypatch.setattr(build_mod, "PROJECT_ROOT", tmp_path)

        raw_dir = tmp_path / "data" / "raw" / "az" / "swagger"
        raw_dir.mkdir(parents=True)
        swagger = {
            "openapi": "3.0.0",
            "info": {"version": "6.0", "title": "AZ"},
            "paths": {"/ed-fi/students/{id}": {}},
            "components": {
                "schemas": {
                    "edFi_student": {
                        "type": "object",
                        "required": ["studentUniqueId"],
                        "properties": {
                            "id": {"type": "string"},
                            "studentUniqueId": {"type": "string"},
                            "firstName": {"type": "string"},
                        },
                    },
                    "az_studentExtension": {
                        "type": "object",
                        "required": [],
                        "properties": {
                            "tribalAffiliationDescriptor": {"type": "string"},
                        },
                    },
                }
            },
        }
        (raw_dir / "resources.json").write_text(json.dumps(swagger))
        (raw_dir / "meta.json").write_text(
            json.dumps(
                {
                    "state": "AZ",
                    "school_year": None,
                    "fetched_at": "2026-04-14T12:00:00+00:00",
                    "urls": {
                        "resources": "https://example.invalid/resources/swagger.json",
                        "descriptors": "https://example.invalid/descriptors/swagger.json",
                    },
                }
            )
        )

        spine = build_mod.build_state_spine("AZ")

        assert spine.state == "AZ"
        assert spine.edfi_version == "6.0"
        assert "Student" in spine.catalog.entities
        assert spine.catalog.extension_count >= 1

        out_path = tmp_path / "data" / "spine" / "az_spine.json"
        assert out_path.exists()
        on_disk = json.loads(out_path.read_text())
        assert on_disk["state"] == "AZ"
        assert on_disk["catalog"]["entity_count"] >= 1

    def test_missing_cached_swagger_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(build_mod, "PROJECT_ROOT", tmp_path)
        with pytest.raises(FileNotFoundError, match="spine fetch"):
            build_mod.build_state_spine("AZ")
