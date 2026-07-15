"""Anthropic Messages Batch API SDK wrapper.

Companion to ``src.score.client.AnthropicClient`` — same constructor
posture (model + api_key + max_tokens), same retry-on-transient-errors
behaviour where it applies, but exposes batch-specific operations:
``submit`` / ``status`` / ``iter_results``.

Why this lives in its own module:

- The sync ``AnthropicClient`` is consumed inline (one call returns
  one parsed payload + telemetry). The batch path returns a
  ``batch_id`` that operators carry across separate ``submit`` and
  ``collect`` invocations — a different lifecycle that's clearer in
  its own surface area.
- Translating SDK objects to plain dataclasses isolates SDK version
  drift to one file. If ``anthropic.messages.batches`` reshapes a
  field, this module is the one place to update.
- The batch path does NOT use ``cache_control`` (each request stands
  alone in the asynchronous batch window), so the wrapper is free of
  the prompt-cache plumbing that complicates ``AnthropicClient.call``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

import anthropic

from src.score.client import DEFAULT_MAX_TOKENS, DEFAULT_MODEL, _extract_json_payload
from src.score.schema import ScoringSchemaError

_LOGGER = logging.getLogger(__name__)


# Status alphabet for ``BatchItemResult.status``. SDK reports
# "succeeded" / "errored" / "canceled" / "expired"; the wrapper adds a
# fifth value, "parse_failed", for the case where Anthropic returned a
# successful message but the JSON-payload extractor (the same
# ``_extract_json_payload`` the sync path uses) couldn't parse the
# text. Treating it as a distinct status keeps the collect side simple
# (cache only writes for "succeeded"; everything else is a re-run
# candidate) without needing a payload-vs-text split-check on every
# row.
ITEM_STATUS_VALUES = (
    "succeeded",
    "parse_failed",
    "errored",
    "canceled",
    "expired",
)


@dataclass(frozen=True)
class BatchRequestSpec:
    """One request to add to a batch.

    Caller-facing shape — decoupled from the SDK's ``Request`` type so
    the runner doesn't import SDK internals. ``BatchClient.submit``
    translates this into the SDK shape at the boundary.
    """

    custom_id: str
    system_text: str
    user_text: str


@dataclass(frozen=True)
class BatchSubmitResult:
    """Outcome of a single ``messages.batches.create`` call."""

    batch_id: str
    request_count: int
    submitted_at: str


@dataclass(frozen=True)
class BatchStatus:
    """Snapshot of a batch's processing status.

    ``processing_status`` is "in_progress" / "canceling" / "ended".
    ``request_counts`` is the SDK's per-status breakdown
    ({"processing": N, "succeeded": N, "errored": N, ...}); always a
    plain ``dict[str, int]`` so callers don't have to import SDK types.
    """

    batch_id: str
    processing_status: str
    request_counts: dict[str, int]
    ended_at: str | None


@dataclass(frozen=True)
class BatchItemResult:
    """Translated result for one item inside a batch.

    ``payload`` is non-None **only** when ``status == "succeeded"``.
    ``raw_text`` is preserved on parse failures so the operator can
    inspect what the model actually emitted; on hard SDK failures
    ("errored"/"canceled"/"expired") it stays empty.
    """

    custom_id: str
    status: str
    payload: Any
    raw_text: str
    tokens_in: int
    tokens_out: int
    error: str | None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _translate_result(sdk_result: Any, *, _logger: logging.Logger = _LOGGER) -> BatchItemResult:
    """Translate one SDK ``MessageBatchIndividualResponse`` to our dataclass.

    Failure-mode handling matches the sync path's posture: the same
    ``_extract_json_payload`` parses the text; a ``ScoringSchemaError``
    becomes ``status="parse_failed"`` so the collect side can skip
    cache writes and surface the raw text for review.
    """
    custom_id = getattr(sdk_result, "custom_id", "")
    result = getattr(sdk_result, "result", None)
    rtype = getattr(result, "type", "errored") if result is not None else "errored"

    if rtype != "succeeded":
        error_obj = getattr(result, "error", None) if result is not None else None
        error_str = None
        if error_obj is not None:
            # SDK error objects vary across versions — coerce to string
            # without assuming a specific attribute graph.
            error_str = str(error_obj)
        return BatchItemResult(
            custom_id=custom_id,
            status=rtype,
            payload=None,
            raw_text="",
            tokens_in=0,
            tokens_out=0,
            error=error_str,
        )

    message = getattr(result, "message", None)
    if message is None:
        return BatchItemResult(
            custom_id=custom_id,
            status="parse_failed",
            payload=None,
            raw_text="",
            tokens_in=0,
            tokens_out=0,
            error="batch result reported succeeded but carried no message",
        )

    content = getattr(message, "content", []) or []
    raw_text = "".join(
        getattr(block, "text", "")
        for block in content
        if getattr(block, "type", None) == "text"
    )
    usage = getattr(message, "usage", None)
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0

    try:
        payload = _extract_json_payload(raw_text)
    except ScoringSchemaError as exc:
        return BatchItemResult(
            custom_id=custom_id,
            status="parse_failed",
            payload=None,
            raw_text=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            error=str(exc),
        )

    return BatchItemResult(
        custom_id=custom_id,
        status="succeeded",
        payload=payload,
        raw_text=raw_text,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        error=None,
    )


class BatchClient:
    """Anthropic Messages Batch API wrapper.

    Three operations: ``submit`` (one batch per call, ≤10,000 items
    per Anthropic's limit), ``status`` (fetches current state),
    ``iter_results`` (streams translated results once the batch has
    ended). Mirrors ``AnthropicClient``'s constructor — model + api_key
    + max_tokens — so the same env-var convention applies.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        # Shared constant (issue #211 item 5a): this sat at a literal
        # 8192 after the sync client moved to 16384 for the v26 TX
        # leaf-borrow batches — the batch path would truncate exactly
        # those batches and silently defer them to full-price sync
        # re-runs.
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

    def submit(self, requests: Iterable[BatchRequestSpec]) -> BatchSubmitResult:
        """Submit one batch and return its ``batch_id`` + count + timestamp.

        Anthropic caps a single batch at 10,000 requests; the runner
        is responsible for chunking above that limit. This method
        passes through whatever it's given and lets the SDK raise on
        excess (so the operator sees Anthropic's error verbatim).
        """
        spec_list = list(requests)
        if not spec_list:
            raise ValueError(
                "BatchClient.submit() requires at least one request"
            )
        sdk_requests = [
            {
                "custom_id": spec.custom_id,
                "params": {
                    "model": self.model,
                    "max_tokens": self.max_tokens,
                    "temperature": 0,
                    "system": spec.system_text,
                    "messages": [
                        {"role": "user", "content": spec.user_text}
                    ],
                },
            }
            for spec in spec_list
        ]
        resp = self._client.messages.batches.create(requests=sdk_requests)
        return BatchSubmitResult(
            batch_id=resp.id,
            request_count=len(sdk_requests),
            submitted_at=_now_iso(),
        )

    def status(self, batch_id: str) -> BatchStatus:
        """Return the current processing status for ``batch_id``.

        Counts come back as a plain dict (succeeded/errored/canceled/
        expired/processing). The SDK uses a typed object; we normalize
        to a dict here so the rest of the codebase stays SDK-agnostic.
        """
        resp = self._client.messages.batches.retrieve(batch_id)
        rc = getattr(resp, "request_counts", None)
        counts: dict[str, int] = {}
        if rc is not None:
            for key in ("processing", "succeeded", "errored", "canceled", "expired"):
                counts[key] = int(getattr(rc, key, 0) or 0)
        return BatchStatus(
            batch_id=resp.id,
            processing_status=resp.processing_status,
            request_counts=counts,
            ended_at=getattr(resp, "ended_at", None),
        )

    def iter_results(self, batch_id: str) -> Iterator[BatchItemResult]:
        """Yield translated results for an ended batch.

        Calling this on a batch whose ``processing_status`` is not
        ``"ended"`` will raise an SDK-level error — that's by design;
        the operator should poll ``status`` first.
        """
        for sdk_result in self._client.messages.batches.results(batch_id):
            yield _translate_result(sdk_result)
