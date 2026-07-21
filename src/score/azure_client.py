"""Runtime selector for the main scoring client.

The production pipeline keeps using :mod:`src.score.client` by default.
When Ed-Fi's Azure endpoint variables are present, the selector swaps in
the matching Azure client while preserving the same call surface and a
distinct cache/model namespace.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from src.score.alt_clients import (
    AZURE_ANTHROPIC_ENDPOINT_ENV,
    AZURE_ANTHROPIC_KEY_ENV,
    AZURE_GPT_NAMESPACE,
    AZURE_GPT_WIRE_MODEL,
    AZURE_OPENAI_ENDPOINT_ENV,
    AZURE_OPENAI_KEY_ENV,
    AZURE_HAIKU_NAMESPACE,
    AZURE_HAIKU_WIRE_MODEL,
    AzureOpenAIResponsesClient,
    AzureAnthropicClient,
)
from src.score.client import AnthropicClient, DEFAULT_MAX_TOKENS, DEFAULT_MODEL, LLMClient


@dataclass(frozen=True)
class RuntimeClient:
    """Resolved client + model namespace for one pipeline run."""

    client: LLMClient
    model: str


def _has_azure_anthropic_env() -> bool:
    return bool(
        os.environ.get(AZURE_ANTHROPIC_ENDPOINT_ENV)
        and os.environ.get(AZURE_ANTHROPIC_KEY_ENV)
    )


def _has_azure_openai_env() -> bool:
    return bool(
        os.environ.get(AZURE_OPENAI_ENDPOINT_ENV)
        and os.environ.get(AZURE_OPENAI_KEY_ENV)
    )


def build_runtime_client(
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> RuntimeClient:
    """Return the default Anthropic client or Ed-Fi's Azure variant.

        The selector is intentionally environment-driven:

        - if ``ANTHROPIC_API_HAIKU_ENDPOINT`` + ``ANTHROPIC_API_HAIKU_KEY``
            are set, the pipeline uses ``AzureAnthropicClient``;
        - otherwise, if ``OPENAPI_API_GPT54`` + ``OPENAPI_API_GPT54_KEY``
            are set, the pipeline uses ``AzureOpenAIResponsesClient``;
        - otherwise it falls back to the standard Anthropic client.
    """

    if _has_azure_anthropic_env():
        client = AzureAnthropicClient(
            api_key=api_key,
            wire_model=AZURE_HAIKU_WIRE_MODEL,
            model=AZURE_HAIKU_NAMESPACE,
            max_tokens=max_tokens,
        )
        return RuntimeClient(client=client, model=client.model)

    if _has_azure_openai_env():
        client = AzureOpenAIResponsesClient(
            api_key=api_key,
            wire_model=AZURE_GPT_WIRE_MODEL,
            model=AZURE_GPT_NAMESPACE,
            max_output_tokens=max_tokens,
        )
        return RuntimeClient(client=client, model=client.model)

    client = AnthropicClient(model=model, api_key=api_key, max_tokens=max_tokens)
    return RuntimeClient(client=client, model=client.model)
