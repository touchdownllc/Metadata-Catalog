"""Ed-Fi Azure AI Foundry endpoint clients — parity study only.

Two ``LLMClient`` implementations for the endpoints Ed-Fi supplied on
their Azure AI Foundry resource (2026-07-08):

* ``AzureAnthropicClient`` — Claude Haiku 4.5 behind the
  Anthropic-compatible route (``…/anthropic/v1/messages``). Subclasses
  ``AnthropicClient`` so retry, ``cache_control``, JSON parsing and
  telemetry come from the production client; only the base URL, key,
  and the wire/telemetry model split differ.
* ``AzureOpenAIResponsesClient`` — GPT-5.4 behind the OpenAI Responses
  route (``…/openai/v1/responses``).

Deliberately NOT wired into ``cli.py``: parity artifacts are driven by
``scripts/endpoint_parity_run.py`` into
``data/out/scoring/phase_a_parity/`` and never reach the production
phase_a tree (docs/edfi-endpoint-parity-plan.md, "Isolation by
construction"). Both clients report a namespaced ``model``
(``azure:…``) so cache entries land in shard directories disjoint from
every production namespace — the same mechanism that once kept the
retired Claude-Code transport's cache entries (lane removed by issue
#243) from colliding with API entries.

Mechanics verified live 2026-07-08 (probe table in the plan doc):
``x-api-key`` auth + ``cache_control`` accepted on the Anthropic
route; ``instructions``/``input`` mapping on the Responses route;
``temperature=0`` is accepted ONLY with ``reasoning.effort="none"``
(any real effort rejects the parameter); supported efforts are
``none|low|medium|high|xhigh`` (no ``minimal``); the deployment
resolves to snapshot ``gpt-5.4-2026-03-05``.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

import anthropic
import httpx

from src.score.client import (
    AnthropicClient,
    LLMResponse,
    _extract_json_payload,
)
from src.score.schema import ScoringSchemaError

_LOGGER = logging.getLogger(__name__)

# Env var names exactly as Ed-Fi's additions to `.env` (gitignored).
AZURE_ANTHROPIC_HAIKU_ENDPOINT_ENV = "ANTHROPIC_API_HAIKU_ENDPOINT"
AZURE_ANTHROPIC_HAIKU_KEY_ENV = "ANTHROPIC_API_HAIKU_KEY"
AZURE_ANTHROPIC_SONNET_ENDPOINT_ENV = "ANTHROPIC_API_SONNET_ENDPOINT"
AZURE_ANTHROPIC_SONNET_KEY_ENV = "ANTHROPIC_API_SONNET_KEY"
AZURE_OPENAI_ENDPOINT_ENV = "OPENAPI_API_GPT54"
AZURE_OPENAI_KEY_ENV = "OPENAPI_API_GPT54_KEY"

# Back-compat aliases for existing imports/callers.
AZURE_ANTHROPIC_ENDPOINT_ENV = AZURE_ANTHROPIC_HAIKU_ENDPOINT_ENV
AZURE_ANTHROPIC_KEY_ENV = AZURE_ANTHROPIC_HAIKU_KEY_ENV

# Wire model = the Azure deployment name sent on the request.
# Namespace = what lands in cache keys, artifact headers, and
# LLMResponse.model — MUST differ from every production model ID.
AZURE_HAIKU_WIRE_MODEL = "claude-haiku-4-5"
AZURE_HAIKU_NAMESPACE = "azure:claude-haiku-4-5"
AZURE_SONNET_WIRE_MODEL = "claude-sonnet-4-6"
AZURE_SONNET_NAMESPACE = "azure:claude-sonnet-4-6"
AZURE_GPT_WIRE_MODEL = "gpt-5.4"
AZURE_GPT_NAMESPACE = "azure:gpt-5.4"

# USD per 1M tokens for the GPT arm: (input, cached-input, output).
# Proxy at OpenAI gpt-5-family list pricing — we do not know Ed-Fi's
# negotiated Azure rates, and billing accrues to their resource either
# way. Token counts on the artifacts are the authoritative report.
_GPT_USD_PER_MTOK = (1.25, 0.125, 10.0)

_MAX_ATTEMPTS = 3
_BASE_DELAY_S = 2.0
_RETRYABLE_STATUS = (408, 429, 500, 502, 503, 504, 529)


class ContentFilterBlocked(RuntimeError):
    """Azure's content filter blocked the prompt or the completion.

    Raised distinctly from ``ScoringSchemaError`` so the parity report
    can count filter blocks separately from model JSON failures.
    """


def _strip_messages_suffix(endpoint: str) -> str:
    """Return the SDK base_url for a full ``…/v1/messages`` endpoint.

    `.env` carries the complete messages URL; the Anthropic SDK wants
    the prefix and appends ``/v1/messages`` itself. An endpoint already
    given as a bare prefix passes through unchanged.
    """
    return endpoint.rstrip("/").removesuffix("/v1/messages")


class AzureAnthropicClient(AnthropicClient):
    """Claude on Ed-Fi's Azure Anthropic-compatible route.

    Everything except construction and the wire/telemetry model split
    is inherited: retry envelope, ``cache_control`` system block, JSON
    payload parsing, token/USD telemetry (the ``azure:`` namespace has
    a proxy entry in ``_PRICING_USD_PER_MTOK``), and
    ``last_response_model`` snapshot capture (Azure resolves
    ``claude-haiku-4-5`` → ``claude-haiku-4-5-20251001``).
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        api_key: str | None = None,
        wire_model: str = AZURE_HAIKU_WIRE_MODEL,
        model: str = AZURE_HAIKU_NAMESPACE,
        max_tokens: int = 16384,
    ) -> None:
        raw_endpoint = endpoint or os.environ.get(AZURE_ANTHROPIC_HAIKU_ENDPOINT_ENV)
        key = api_key or os.environ.get(AZURE_ANTHROPIC_HAIKU_KEY_ENV)
        if not raw_endpoint or not key:
            raise RuntimeError(
                f"{AZURE_ANTHROPIC_HAIKU_ENDPOINT_ENV} / {AZURE_ANTHROPIC_HAIKU_KEY_ENV} "
                "not set — the Ed-Fi Azure endpoint + key live in .env "
                "(see docs/edfi-endpoint-parity-plan.md)"
            )
        # Deliberately no super().__init__() — it requires
        # ANTHROPIC_API_KEY, which this transport does not use.
        self._client = anthropic.Anthropic(
            api_key=key, base_url=_strip_messages_suffix(raw_endpoint)
        )
        self.model = model
        self.max_tokens = max_tokens
        self.last_response_model: str | None = None
        self._wire_model = wire_model

    @property
    def _request_model(self) -> str:
        return self._wire_model


class AzureOpenAIResponsesClient:
    """GPT on Ed-Fi's Azure OpenAI Responses route.

    Implements the ``LLMClient`` protocol. Prompt mapping:
    ``system_text`` → ``instructions``, ``user_text`` → ``input``.

    ``reasoning_effort`` is pinned explicitly on every request so runs
    are reproducible. The default ``"none"`` is the
    Sonnet-comparable arm (no extended thinking) and the only setting
    that also accepts ``temperature=0``; any real effort level drops
    the temperature pin (rejected by the API) — callers choosing one
    accept nondeterminism.

    ``cache_system`` is accepted for protocol compatibility and
    ignored: OpenAI prompt caching is automatic (no request-side
    control) and surfaces as ``usage.input_tokens_details.cached_tokens``,
    which maps onto ``LLMResponse.cache_read_tokens``.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        api_key: str | None = None,
        wire_model: str = AZURE_GPT_WIRE_MODEL,
        model: str = AZURE_GPT_NAMESPACE,
        max_output_tokens: int = 24576,
        reasoning_effort: str = "none",
        timeout_s: float = 300.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        resolved_endpoint = endpoint or os.environ.get(AZURE_OPENAI_ENDPOINT_ENV)
        key = api_key or os.environ.get(AZURE_OPENAI_KEY_ENV)
        if not resolved_endpoint or not key:
            raise RuntimeError(
                f"{AZURE_OPENAI_ENDPOINT_ENV} / {AZURE_OPENAI_KEY_ENV} "
                "not set — the Ed-Fi Azure endpoint + key live in .env "
                "(see docs/edfi-endpoint-parity-plan.md)"
            )
        self._endpoint = resolved_endpoint
        self._headers = {"api-key": key, "content-type": "application/json"}
        self._http = http_client or httpx.Client(timeout=timeout_s)
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.last_response_model: str | None = None
        self.last_reasoning_tokens: int = 0
        self._wire_model = wire_model

    def call(
        self,
        *,
        system_text: str,
        user_text: str,
        cache_system: bool = True,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self._wire_model,
            "instructions": system_text,
            "input": user_text,
            # Reasoning tokens count against this cap — 16384 (the
            # Anthropic-path output cap) + headroom.
            "max_output_tokens": self.max_output_tokens,
            "reasoning": {"effort": self.reasoning_effort},
        }
        if self.reasoning_effort == "none":
            body["temperature"] = 0

        data = self._post_with_retry(body)

        error = data.get("error")
        if error:
            if str(error.get("code", "")) == "content_filter":
                raise ContentFilterBlocked(str(error))
            raise RuntimeError(f"Responses API error: {error}")
        blocked = [
            f for f in (data.get("content_filters") or []) if f.get("blocked")
        ]
        if blocked:
            raise ContentFilterBlocked(
                f"Azure content filter blocked {len(blocked)} segment(s): {blocked}"
            )

        raw_text = "".join(
            content.get("text", "")
            for item in (data.get("output") or [])
            if item.get("type") == "message"
            for content in (item.get("content") or [])
            if content.get("type") == "output_text"
        )
        status = data.get("status")
        if status != "completed":
            reason = (data.get("incomplete_details") or {}).get("reason")
            raise ScoringSchemaError(
                f"Responses API returned status={status!r} (reason: {reason!r}) "
                "— response text unusable",
                payload=raw_text,
            )
        payload = _extract_json_payload(raw_text)

        usage = data.get("usage") or {}
        tokens_in = int(usage.get("input_tokens") or 0)
        tokens_out = int(usage.get("output_tokens") or 0)
        cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        self.last_reasoning_tokens = int(
            (usage.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
        )
        self.last_response_model = data.get("model")

        inp, cached_inp, out = _GPT_USD_PER_MTOK
        usd = (
            max(tokens_in - cached, 0) * inp
            + cached * cached_inp
            + tokens_out * out
        ) / 1_000_000

        return LLMResponse(
            payload=payload,
            raw_text=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_read_tokens=cached,
            cache_creation_tokens=0,
            usd=usd,
            model=self.model,
        )

    def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST once with the same retry envelope as ``AnthropicClient``."""
        last_err: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._http.post(
                    self._endpoint, headers=self._headers, json=body
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                self._sleep_before_retry(attempt, exc)
                continue
            if resp.status_code in _RETRYABLE_STATUS:
                last_err = RuntimeError(
                    f"Responses API HTTP {resp.status_code}: {resp.text[:300]}"
                )
                if attempt == _MAX_ATTEMPTS:
                    raise last_err
                self._sleep_before_retry(attempt, last_err)
                continue
            if resp.status_code != 200:
                # Azure signals prompt-level filter blocks as HTTP 400
                # with error.code == "content_filter".
                try:
                    error = resp.json().get("error") or {}
                except ValueError:
                    error = {}
                if str(error.get("code", "")) == "content_filter":
                    raise ContentFilterBlocked(str(error))
                raise RuntimeError(
                    f"Responses API HTTP {resp.status_code}: {resp.text[:500]}"
                )
            return resp.json()
        assert last_err is not None  # pragma: no cover — loop always raises first
        raise last_err

    @staticmethod
    def _sleep_before_retry(attempt: int, exc: Exception) -> None:
        delay = _BASE_DELAY_S * (2 ** (attempt - 1)) + random.uniform(0, _BASE_DELAY_S)
        _LOGGER.warning(
            "responses call failed (attempt %d/%d): %s — retrying in %.1fs",
            attempt, _MAX_ATTEMPTS, exc, delay,
        )
        time.sleep(delay)
