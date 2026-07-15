"""Anthropic SDK wrapper — retry, prompt caching, cost accounting.

Kept minimal on purpose. All cost-sensitive behaviour (per-run cap, cache
key derivation, schema validation) lives at the extractor level in
``extract.py``; this module does one thing: issue a single well-formed
Messages API call and return the parsed JSON + token/USD telemetry.

Token pricing is a module-level table (USD per 1M tokens, matching
Anthropic's public pricing as of 2026-04). It is the harness's only
source of truth for cost telemetry — Phase B rollups read the per-call
``usd`` value off cache entries written by this client.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

from src.score.schema import ScoringSchemaError

_LOGGER = logging.getLogger(__name__)


_FENCE_PATTERNS = (
    # ```json ... ```
    re.compile(r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE),
)


def _extract_json_payload(raw_text: str) -> Any:
    """Parse a Messages API text block into a Python object.

    Phase A assumed the model returns naked JSON. Phase B's wider fact
    surface surfaced the failure modes that assumption ignores:

    1. Empty text block (rare but observed — Sonnet sometimes returns
       zero text content, e.g. when a safety filter trims output).
    2. Markdown fence wrapping (``\u0060\u0060\u0060json\n[...]\n\u0060\u0060\u0060`` —
       common even with explicit "no fences" instructions).
    3. Prose prefix/suffix ("Here is the JSON:\n[...]\n\nLet me know.").

    Strategy: try strict parse first (still the fastest common path);
    fall back to fence strip; fall back to first-array regex. Raise
    ``ScoringSchemaError`` with a truncated preview on persistent
    failure so the caller can persist the aborted artifact without
    losing the reviewer's ability to see what the model sent.
    """
    if not raw_text or not raw_text.strip():
        raise ScoringSchemaError(
            "LLM response had empty text content (no JSON to parse)",
            payload=raw_text,
        )
    # Strict parse — the common path stays zero-overhead.
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    # Fence-stripped parse.
    for pattern in _FENCE_PATTERNS:
        m = pattern.match(raw_text)
        if m:
            try:
                return json.loads(m.group("body"))
            except json.JSONDecodeError:
                break

    # Extract a top-level [...] array, tolerating prose around it. Try
    # EVERY `[` position as a candidate start — Sonnet occasionally emits
    # a reasoning preamble that itself contains brackets (e.g. "[rule X]",
    # "- [the student]") before the real JSON array, so the first `[` may
    # not be the payload's. We try each in order and return the first that
    # yields valid JSON.
    search_from = 0
    while True:
        start = raw_text.find("[", search_from)
        if start < 0:
            break
        depth = 0
        in_string = False
        escape = False
        closed_at = -1
        for idx in range(start, len(raw_text)):
            ch = raw_text[idx]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    closed_at = idx
                    break
        if closed_at < 0:
            # Unbalanced from this start; no later `[` can help either.
            break
        candidate = raw_text[start:closed_at + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            search_from = start + 1
            continue

    preview = raw_text if len(raw_text) <= 500 else raw_text[:500] + "…[truncated]"
    raise ScoringSchemaError(
        f"LLM response was not valid JSON (neither strict, fence-stripped, "
        f"nor first-array extraction parsed): {preview!r}",
        payload=raw_text,
    )

DEFAULT_MODEL = "claude-sonnet-4-6"

# Output cap passed to the Messages API — ONE source of truth shared by
# the sync client below and score.batch_client.BatchClient (issue #211
# item 5a: the batch path kept an 8192 default after this was raised,
# re-exposing exactly the truncation the raise fixed). 16384 covers TX
# ``ARDInvited*``-class entity batches that swelled past 8192 once issue
# #147 (v26) added sub-collection leaf-borrow rows to the source-lens
# pool. Billing tracks actual output tokens, so the higher cap costs
# nothing on shorter responses.
DEFAULT_MAX_TOKENS = 16384

# USD per 1,000,000 tokens. Input/output + prompt-cache write/read tiers.
# Public Anthropic pricing as of 2026-04; bump when a model's pricing
# changes. Unknown model -> fall back to (3.0, 15.0) Sonnet-tier — we
# would rather over-estimate cost than under-estimate.
_PRICING_USD_PER_MTOK: dict[str, tuple[float, float, float, float]] = {
    # model_id: (input, output, cache_write, cache_read)
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 0.30),
    "claude-sonnet-4-7": (3.0, 15.0, 3.75, 0.30),
    "claude-opus-4-6": (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-7": (15.0, 75.0, 18.75, 1.50),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 1.25, 0.10),
    # Ed-Fi Azure AI Foundry parity-study namespace (alt_clients.py).
    # Proxy at public Anthropic Haiku pricing — actual billing accrues
    # to Ed-Fi's Azure resource; token counts are the authoritative
    # report. See docs/edfi-endpoint-parity-plan.md.
    "azure:claude-haiku-4-5": (1.0, 5.0, 1.25, 0.10),
}

# Anthropic Batch API pricing — input + output halved; the batch path
# does not set ``cache_control`` on requests (each batch item is a
# standalone call with no shared 5-min TTL semantics across the
# asynchronous batch window), so cache_write/cache_read tiers are not
# applicable. The two-tuple shape mirrors what ``usd_for_batch`` reads
# back: (input, output) — half the sync tier per Anthropic's published
# 50% discount on Messages Batch.
_PRICING_USD_PER_MTOK_BATCH: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (1.5, 7.5),
    "claude-sonnet-4-7": (1.5, 7.5),
    "claude-opus-4-6": (7.5, 37.5),
    "claude-opus-4-7": (7.5, 37.5),
    "claude-haiku-4-5-20251001": (0.5, 2.5),
    # Parity-study namespace — never used on the batch path, present
    # only to satisfy the sync/batch table-parity invariant.
    "azure:claude-haiku-4-5": (0.5, 2.5),
}

_MAX_ATTEMPTS = 3
_BASE_DELAY_S = 2.0


@dataclass(frozen=True)
class LLMResponse:
    """Parsed response + telemetry for a single Messages API call.

    ``tokens_in`` is the TOTAL input (uncached + cache_read +
    cache_creation); the per-tier splits ride alongside so cost rollups
    never have to reverse-engineer the uncached portion.
    """

    payload: Any
    raw_text: str
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    cache_creation_tokens: int
    usd: float
    model: str


class LLMClient(Protocol):
    """Structural type for anything that can answer a single fact-extraction call.

    ``AnthropicClient`` (direct Messages API) is the production
    implementation; ``alt_clients`` carries the endpoint-parity study's
    Azure variants.

    The protocol is the contract callers depend on; the cache layer keys on
    ``LLMResponse.model`` so different transports partition into separate
    namespaces automatically.
    """

    model: str

    def call(
        self,
        *,
        system_text: str,
        user_text: str,
        cache_system: bool = True,
    ) -> LLMResponse: ...


def _price_for(model: str) -> tuple[float, float, float, float]:
    return _PRICING_USD_PER_MTOK.get(model, (3.0, 15.0, 3.75, 0.30))


def _usd_for(
    model: str,
    *,
    uncached_in: int,
    tokens_out: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> float:
    """List-price USD from RAW usage components.

    ``uncached_in`` is the API's ``usage.input_tokens`` verbatim — on
    the Messages API it EXCLUDES the cache tiers, so no subtraction
    happens here. The old signature took a folded total and
    reverse-engineered the uncached portion; paired with a mis-firing
    "already folded in" heuristic in ``call()``, that double-subtracted
    the cache tiers and billed uncached input at the $0.30/M cache-read
    tier instead of $3/M — a systematic undercount in cache entries,
    run headers, --cost-cap enforcement, and the cumulative-spend
    ledger (issue #212 item 4).
    """
    inp, out, cw, cr = _price_for(model)
    total = (
        uncached_in * inp
        + tokens_out * out
        + cache_creation_tokens * cw
        + cache_read_tokens * cr
    )
    return total / 1_000_000


def _price_for_batch(model: str) -> tuple[float, float]:
    """Return (input, output) batch-tier USD per 1M tokens for ``model``.

    Falls back to the Sonnet-tier batch price (1.5, 7.5) for unknown
    models — same conservative posture as ``_price_for``: better to
    over-estimate than under-estimate on a billing line.
    """
    return _PRICING_USD_PER_MTOK_BATCH.get(model, (1.5, 7.5))


def usd_for_batch(
    model: str,
    *,
    tokens_in: int,
    tokens_out: int,
) -> float:
    """USD cost for a single Batch API request at batch-tier pricing.

    The batch path does not use ``cache_control``, so all input tokens
    bill at the (halved) batch input tier regardless of whether the
    same system text repeated in earlier batch items. Cache-tier
    parameters are deliberately absent from this signature — adding
    them would invite the kind of off-by-discount mistakes that already
    bit Phase B telemetry before atomic cache writes landed.
    """
    inp, out = _price_for_batch(model)
    total = tokens_in * inp + tokens_out * out
    return total / 1_000_000


class AnthropicClient:
    """Thin wrapper around ``anthropic.Anthropic`` with retry + caching.

    Parameters
    ----------
    model:
        Model ID. Cache keys include this value — swapping models
        invalidates cache by design.
    api_key:
        Optional override; defaults to ``ANTHROPIC_API_KEY``. Missing
        key raises immediately — callers (extractor) swallow this only
        when ``--dry-run`` was requested before the client is even
        instantiated.
    max_tokens:
        Output cap passed to the Messages API. Defaults to the shared
        ``DEFAULT_MAX_TOKENS`` (16384) — see that constant's comment for
        the v26 TX ``ARDInvited*`` truncation rationale. 8192 was
        sufficient for descriptor-heavy WI entities at v25; v26's TX
        widening pushed a small number of batches into truncation
        territory (truncated JSON → ``parse_failed``).
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set — export before running; "
                "see README for setup"
            )
        self._client = anthropic.Anthropic(api_key=key)
        self.model = model
        self.max_tokens = max_tokens
        self.last_response_model: str | None = None

    @property
    def _request_model(self) -> str:
        """Model ID sent on the wire.

        Defaults to ``self.model``. The Ed-Fi Azure parity client
        (``alt_clients.AzureAnthropicClient``) overrides this to send
        the Azure deployment name while ``self.model`` stays the
        namespaced cache/telemetry ID (``azure:…``).
        """
        return self.model

    def call(
        self,
        *,
        system_text: str,
        user_text: str,
        cache_system: bool = True,
    ) -> LLMResponse:
        """Call the Messages API once, with retry on transient errors.

        ``system_text`` carries the reusable prompt prologue (rules,
        rubric, examples). It is marked for prompt caching when
        ``cache_system=True`` so successive entity batches within the
        5-minute TTL read from the cache tier at ~10× discount.

        ``user_text`` carries the entity-specific context + per-element
        block. Not cached — it's different per call.
        """
        system_block: Any
        if cache_system:
            system_block = [
                {
                    "type": "text",
                    "text": system_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_block = system_text

        last_err: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = self._client.messages.create(
                    model=self._request_model,
                    max_tokens=self.max_tokens,
                    temperature=0,
                    system=system_block,
                    messages=[{"role": "user", "content": user_text}],
                )
            except (
                anthropic.APITimeoutError,
                anthropic.APIConnectionError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
            ) as exc:
                last_err = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                delay = _BASE_DELAY_S * (2 ** (attempt - 1))
                delay += random.uniform(0, _BASE_DELAY_S)
                _LOGGER.warning(
                    "anthropic call failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt, _MAX_ATTEMPTS, exc, delay,
                )
                time.sleep(delay)
                continue
            break
        else:
            assert last_err is not None
            raise last_err

        # Endpoint-resolved snapshot (e.g. `claude-haiku-4-5` →
        # `claude-haiku-4-5-20251001`) — provenance for parity runs.
        self.last_response_model = getattr(resp, "model", None)

        raw_text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
        payload = _extract_json_payload(raw_text)

        usage = resp.usage
        # Messages API contract (pinned; true for every SDK version this
        # project has used): ``usage.input_tokens`` EXCLUDES the cache
        # tiers. The old "already folded in whenever cache_read <=
        # input_tokens" heuristic mis-fired on exactly the common case
        # (per-entity user block >= the cached system prologue) and made
        # ``_usd_for`` double-subtract the cache tiers — issue #212
        # item 4. Total input is always the plain sum.
        uncached_in = getattr(usage, "input_tokens", 0) or 0
        tokens_out = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0

        usd = _usd_for(
            self.model,
            uncached_in=uncached_in,
            tokens_out=tokens_out,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_create,
        )

        return LLMResponse(
            payload=payload,
            raw_text=raw_text,
            tokens_in=uncached_in + cache_read + cache_create,
            tokens_out=tokens_out,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_create,
            usd=usd,
            model=self.model,
        )
