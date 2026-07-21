"""Hermetic tests for the Ed-Fi Azure parity clients (``alt_clients``).

No network: the Anthropic leg stubs the SDK's ``messages`` resource;
the OpenAI leg runs against ``httpx.MockTransport``. The contract under
test is the ``LLMClient`` protocol plus the parity-specific invariants:
wire/telemetry model split, prompt mapping, retry, content-filter
detection, and usage/USD accounting.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

from src.score import alt_clients
from src.score.alt_clients import (
    AZURE_GPT_NAMESPACE,
    AZURE_GPT_WIRE_MODEL,
    AZURE_HAIKU_NAMESPACE,
    AZURE_HAIKU_WIRE_MODEL,
    AzureAnthropicClient,
    AzureOpenAIResponsesClient,
    ContentFilterBlocked,
    _strip_messages_suffix,
)
from src.score.client import _PRICING_USD_PER_MTOK, AnthropicClient
from src.score.schema import ScoringSchemaError

_ENDPOINT = "https://example.services.ai.azure.com/anthropic/v1/messages"
_GPT_ENDPOINT = "https://example.services.ai.azure.com/openai/v1/responses"


# ---------------------------------------------------------------------------
# AzureAnthropicClient


class _StubMessages:
    def __init__(self, response: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._response


def _fake_anthropic_response(text: str = '[{"element": "x"}]') -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=10,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        model="claude-haiku-4-5-20251001",
    )


class TestAzureAnthropicClient:
    def test_missing_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(alt_clients.AZURE_ANTHROPIC_ENDPOINT_ENV, raising=False)
        monkeypatch.delenv(alt_clients.AZURE_ANTHROPIC_KEY_ENV, raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_HAIKU"):
            AzureAnthropicClient()

    def test_base_url_strips_messages_suffix(self) -> None:
        client = AzureAnthropicClient(endpoint=_ENDPOINT, api_key="k")
        base = str(client._client.base_url)
        assert "/v1/messages" not in base
        assert base.rstrip("/").endswith("/anthropic")

    def test_strip_suffix_passthrough_for_bare_prefix(self) -> None:
        assert _strip_messages_suffix("https://x.example/anthropic") == (
            "https://x.example/anthropic"
        )

    def test_wire_vs_telemetry_model_split(self) -> None:
        """Request carries the deployment name; cache/telemetry carry azure:*."""
        client = AzureAnthropicClient(endpoint=_ENDPOINT, api_key="k")
        stub = _StubMessages(_fake_anthropic_response())
        client._client.messages = stub  # type: ignore[assignment]

        resp = client.call(system_text="SYS", user_text="USER")

        assert stub.calls[0]["model"] == AZURE_HAIKU_WIRE_MODEL
        assert resp.model == AZURE_HAIKU_NAMESPACE
        assert client.last_response_model == "claude-haiku-4-5-20251001"
        # Prompt-caching system block preserved from the parent client.
        assert stub.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert stub.calls[0]["temperature"] == 0

    def test_namespace_has_pricing_entry(self) -> None:
        """USD telemetry must not fall back to the Sonnet tier silently."""
        assert AZURE_HAIKU_NAMESPACE in _PRICING_USD_PER_MTOK

    def test_production_client_request_model_defaults_to_model(self) -> None:
        client = AnthropicClient(api_key="k")
        assert client._request_model == client.model


# ---------------------------------------------------------------------------
# AzureOpenAIResponsesClient


def _ok_body(
    text: str = '[{"element": "x"}]',
    *,
    input_tokens: int = 100,
    output_tokens: int = 10,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    status: str = "completed",
) -> dict[str, Any]:
    return {
        "status": status,
        "model": "gpt-5.4-2026-03-05",
        "output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_tokens_details": {"cached_tokens": cached_tokens},
            "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
        "content_filters": [],
    }


def _gpt_client(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any
) -> AzureOpenAIResponsesClient:
    return AzureOpenAIResponsesClient(
        endpoint=_GPT_ENDPOINT,
        api_key="k",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


class TestAzureOpenAIResponsesClient:
    def test_missing_env_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(alt_clients.AZURE_OPENAI_ENDPOINT_ENV, raising=False)
        monkeypatch.delenv(alt_clients.AZURE_OPENAI_KEY_ENV, raising=False)
        with pytest.raises(RuntimeError, match="OPENAPI_API_GPT54"):
            AzureOpenAIResponsesClient()

    def test_happy_path_mapping_and_telemetry(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen.append(body)
            assert request.headers["api-key"] == "k"
            return httpx.Response(
                200, json=_ok_body(input_tokens=100, output_tokens=10, cached_tokens=20)
            )

        client = _gpt_client(handler)
        resp = client.call(system_text="SYS-PROLOGUE", user_text="USER-BLOCK")

        body = seen[0]
        assert body["model"] == AZURE_GPT_WIRE_MODEL
        assert body["instructions"] == "SYS-PROLOGUE"
        assert body["input"] == "USER-BLOCK"
        assert body["temperature"] == 0
        assert body["reasoning"] == {"effort": "none"}

        assert resp.payload == [{"element": "x"}]
        assert resp.model == AZURE_GPT_NAMESPACE
        assert resp.tokens_in == 100
        assert resp.tokens_out == 10
        assert resp.cache_read_tokens == 20
        assert resp.cache_creation_tokens == 0
        # (80 uncached * 1.25 + 20 cached * 0.125 + 10 out * 10.0) / 1M
        assert resp.usd == pytest.approx((80 * 1.25 + 20 * 0.125 + 10 * 10.0) / 1e6)
        assert client.last_response_model == "gpt-5.4-2026-03-05"

    def test_reasoning_effort_drops_temperature(self) -> None:
        """temperature is rejected by the API whenever reasoning is on."""
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(200, json=_ok_body(reasoning_tokens=64))

        client = _gpt_client(handler, reasoning_effort="low")
        client.call(system_text="s", user_text="u")

        assert "temperature" not in seen[0]
        assert seen[0]["reasoning"] == {"effort": "low"}
        assert client.last_reasoning_tokens == 64

    def test_fenced_json_parses(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=_ok_body('```json\n[{"element": "y"}]\n```')
            )

        resp = _gpt_client(handler).call(system_text="s", user_text="u")
        assert resp.payload == [{"element": "y"}]

    def test_prompt_content_filter_400_raises_distinctly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={"error": {"code": "content_filter", "message": "blocked"}},
            )

        with pytest.raises(ContentFilterBlocked):
            _gpt_client(handler).call(system_text="s", user_text="u")

    def test_completion_filter_block_raises_distinctly(self) -> None:
        body = _ok_body()
        body["content_filters"] = [{"blocked": True, "source_type": "completion"}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with pytest.raises(ContentFilterBlocked):
            _gpt_client(handler).call(system_text="s", user_text="u")

    def test_incomplete_status_raises_schema_error(self) -> None:
        body = _ok_body(status="incomplete")
        body["incomplete_details"] = {"reason": "max_output_tokens"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        with pytest.raises(ScoringSchemaError, match="max_output_tokens"):
            _gpt_client(handler).call(system_text="s", user_text="u")

    def test_retries_on_429_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(alt_clients.time, "sleep", lambda _s: None)
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(429, json={"error": {"message": "slow down"}})
            return httpx.Response(200, json=_ok_body())

        resp = _gpt_client(handler).call(system_text="s", user_text="u")
        assert len(attempts) == 2
        assert resp.payload == [{"element": "x"}]

    def test_non_retryable_4xx_raises_immediately(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(401, json={"error": {"message": "bad key"}})

        with pytest.raises(RuntimeError, match="401"):
            _gpt_client(handler).call(system_text="s", user_text="u")
        assert len(attempts) == 1
