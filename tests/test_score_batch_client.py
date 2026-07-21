"""Unit tests for ``src.score.batch_client``.

The SDK is mocked at the ``anthropic.Anthropic`` constructor level so
no live calls fire. Each fake mirrors only the surface area the
wrapper actually touches — ``messages.batches.create`` /
``.retrieve`` / ``.results`` — using ``types.SimpleNamespace`` because
the SDK's typed objects expose attribute access (not dict access)
and SimpleNamespace replicates that with no boilerplate.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import anthropic

from src.score.batch_client import (
    BatchClient,
    BatchRequestSpec,
    _translate_result,
)


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every BatchClient test needs ANTHROPIC_API_KEY in the env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")


class _FakeBatchesAPI:
    """Records calls + returns pre-baked SDK-shaped responses."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.retrieve_calls: list[str] = []
        self.results_calls: list[str] = []
        self.create_response: Any = None
        self.retrieve_response: Any = None
        self.results_response: list[Any] = []

    def create(self, *, requests: list[dict[str, Any]]) -> Any:
        self.create_calls.append({"requests": requests})
        return self.create_response

    def retrieve(self, batch_id: str) -> Any:
        self.retrieve_calls.append(batch_id)
        return self.retrieve_response

    def results(self, batch_id: str) -> Any:
        self.results_calls.append(batch_id)
        return iter(self.results_response)


class _FakeAnthropic:
    """Stand-in for ``anthropic.Anthropic`` exposing only what we use."""

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key
        self.messages = SimpleNamespace(batches=_FakeBatchesAPI())


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> _FakeAnthropic:
    instance: dict[str, _FakeAnthropic] = {}

    def _ctor(*, api_key: str) -> _FakeAnthropic:
        fake = _FakeAnthropic(api_key=api_key)
        instance["fake"] = fake
        return fake

    monkeypatch.setattr(anthropic, "Anthropic", _ctor)
    # Construction is deferred to the test calling BatchClient(); return
    # a thunk-shaped object the test can re-grab after construction.
    return instance  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


def test_submit_builds_correct_sdk_request_shape(fake_sdk: dict[str, _FakeAnthropic]) -> None:
    client = BatchClient(model="claude-sonnet-4-6", max_tokens=4096)
    fake = fake_sdk["fake"]
    fake.messages.batches.create_response = SimpleNamespace(id="batch_01abcd")

    specs = [
        BatchRequestSpec(custom_id="aa" * 32, system_text="SYS-1", user_text="USR-1"),
        BatchRequestSpec(custom_id="bb" * 32, system_text="SYS-2", user_text="USR-2"),
    ]
    result = client.submit(specs)

    assert result.batch_id == "batch_01abcd"
    assert result.request_count == 2
    assert result.submitted_at  # ISO string

    call = fake.messages.batches.create_calls[0]
    assert len(call["requests"]) == 2
    first = call["requests"][0]
    assert first["custom_id"] == "aa" * 32
    assert first["params"]["model"] == "claude-sonnet-4-6"
    assert first["params"]["max_tokens"] == 4096
    assert first["params"]["temperature"] == 0
    assert first["params"]["system"] == "SYS-1"
    assert first["params"]["messages"] == [{"role": "user", "content": "USR-1"}]
    # Critical contract: the batch path does NOT use cache_control.
    # If a future maintainer wraps system as a list with cache_control,
    # this assertion fires before any production batch is submitted.
    assert isinstance(first["params"]["system"], str)


def test_submit_with_no_requests_raises_value_error(fake_sdk: dict[str, _FakeAnthropic]) -> None:
    client = BatchClient()
    with pytest.raises(ValueError, match="at least one request"):
        client.submit([])


def test_submit_translates_iterable_input(fake_sdk: dict[str, _FakeAnthropic]) -> None:
    """Generator-style iterables must work, not just list/tuple."""
    client = BatchClient()
    fake = fake_sdk["fake"]
    fake.messages.batches.create_response = SimpleNamespace(id="batch_gen")

    def _gen():
        yield BatchRequestSpec(custom_id="cc" * 32, system_text="S", user_text="U")
        yield BatchRequestSpec(custom_id="dd" * 32, system_text="S", user_text="U")

    result = client.submit(_gen())
    assert result.request_count == 2
    assert result.batch_id == "batch_gen"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_normalizes_request_counts_dict(fake_sdk: dict[str, _FakeAnthropic]) -> None:
    client = BatchClient()
    fake = fake_sdk["fake"]
    fake.messages.batches.retrieve_response = SimpleNamespace(
        id="batch_xyz",
        processing_status="ended",
        ended_at="2026-04-25T12:00:00Z",
        request_counts=SimpleNamespace(
            processing=0, succeeded=8, errored=1, canceled=0, expired=1
        ),
    )

    status = client.status("batch_xyz")
    assert status.batch_id == "batch_xyz"
    assert status.processing_status == "ended"
    assert status.ended_at == "2026-04-25T12:00:00Z"
    assert status.request_counts == {
        "processing": 0,
        "succeeded": 8,
        "errored": 1,
        "canceled": 0,
        "expired": 1,
    }


def test_status_handles_missing_request_counts(fake_sdk: dict[str, _FakeAnthropic]) -> None:
    """A still-queueing batch may have no request_counts attr yet."""
    client = BatchClient()
    fake = fake_sdk["fake"]
    fake.messages.batches.retrieve_response = SimpleNamespace(
        id="batch_q",
        processing_status="in_progress",
        ended_at=None,
        request_counts=None,
    )
    status = client.status("batch_q")
    assert status.processing_status == "in_progress"
    assert status.ended_at is None
    assert status.request_counts == {}


# ---------------------------------------------------------------------------
# iter_results
# ---------------------------------------------------------------------------


def _make_succeeded_sdk_result(custom_id: str, payload: Any, *, tokens_in: int = 200, tokens_out: int = 50) -> Any:
    text = json.dumps(payload)
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out),
            ),
        ),
    )


def _make_errored_sdk_result(custom_id: str, *, error: str = "model_overloaded") -> Any:
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(
            type="errored",
            error=SimpleNamespace(type="overloaded_error", message=error),
        ),
    )


def test_iter_results_translates_succeeded_with_json_payload(fake_sdk: dict[str, _FakeAnthropic]) -> None:
    client = BatchClient()
    fake = fake_sdk["fake"]
    payload = [{"element_name": "id", "has_conditional_logic": False, "spans": [], "confidence": "medium"}]
    fake.messages.batches.results_response = [
        _make_succeeded_sdk_result("aa" * 32, payload, tokens_in=300, tokens_out=80),
    ]

    results = list(client.iter_results("batch_id"))
    assert len(results) == 1
    r = results[0]
    assert r.status == "succeeded"
    assert r.custom_id == "aa" * 32
    assert r.payload == payload
    assert r.tokens_in == 300
    assert r.tokens_out == 80
    assert r.error is None


def test_iter_results_marks_errored_status_with_error_string(fake_sdk: dict[str, _FakeAnthropic]) -> None:
    client = BatchClient()
    fake = fake_sdk["fake"]
    fake.messages.batches.results_response = [
        _make_errored_sdk_result("bb" * 32, error="rate_limited"),
    ]

    results = list(client.iter_results("batch_id"))
    assert len(results) == 1
    r = results[0]
    assert r.status == "errored"
    assert r.payload is None
    assert r.tokens_in == 0
    assert r.tokens_out == 0
    assert r.error is not None
    assert "rate_limited" in r.error


def test_iter_results_marks_parse_failed_on_invalid_json(fake_sdk: dict[str, _FakeAnthropic]) -> None:
    """Anthropic-side success but our extractor can't parse — distinct status."""
    client = BatchClient()
    fake = fake_sdk["fake"]
    bad = SimpleNamespace(
        custom_id="cc" * 32,
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="this is not json at all")],
                usage=SimpleNamespace(input_tokens=100, output_tokens=20),
            ),
        ),
    )
    fake.messages.batches.results_response = [bad]

    results = list(client.iter_results("batch_id"))
    r = results[0]
    assert r.status == "parse_failed"
    assert r.payload is None
    assert r.raw_text == "this is not json at all"
    assert r.tokens_in == 100  # tokens still accounted for — Anthropic billed us
    assert r.tokens_out == 20
    assert r.error is not None


def test_iter_results_handles_succeeded_without_message(fake_sdk: dict[str, _FakeAnthropic]) -> None:
    """SDK occasionally returns succeeded with no message — defensive."""
    client = BatchClient()
    fake = fake_sdk["fake"]
    bad = SimpleNamespace(
        custom_id="dd" * 32,
        result=SimpleNamespace(type="succeeded", message=None),
    )
    fake.messages.batches.results_response = [bad]

    results = list(client.iter_results("batch_id"))
    r = results[0]
    assert r.status == "parse_failed"
    assert r.payload is None
    assert r.error is not None
    assert "no message" in r.error


def test_iter_results_handles_canceled_and_expired(fake_sdk: dict[str, _FakeAnthropic]) -> None:
    client = BatchClient()
    fake = fake_sdk["fake"]
    fake.messages.batches.results_response = [
        SimpleNamespace(
            custom_id="ee" * 32,
            result=SimpleNamespace(type="canceled", error=None),
        ),
        SimpleNamespace(
            custom_id="ff" * 32,
            result=SimpleNamespace(type="expired", error=None),
        ),
    ]
    results = list(client.iter_results("batch_id"))
    assert [r.status for r in results] == ["canceled", "expired"]
    assert all(r.payload is None for r in results)


# ---------------------------------------------------------------------------
# constructor
# ---------------------------------------------------------------------------


def test_constructor_raises_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY not set"):
        BatchClient()


def test_translate_result_helper_used_directly() -> None:
    """``_translate_result`` is the single translation point. Smoke-test
    it without the BatchClient wrapper to confirm the contract."""
    succeeded = SimpleNamespace(
        custom_id="aabb",
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"ok": true}')],
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
            ),
        ),
    )
    out = _translate_result(succeeded)
    assert out.status == "succeeded"
    assert out.payload == {"ok": True}


def test_max_tokens_default_shared_with_sync_client(
    fake_sdk: dict[str, _FakeAnthropic],
) -> None:
    """One max_tokens source of truth (issue #211 item 5a).

    The batch path kept a literal 8192 after the sync client was raised
    to 16384 for the v26 TX ``ARDInvited*`` leaf-borrow batches — the
    batch runs would truncate exactly those batches (truncated JSON →
    ``parse_failed``) and silently defer them to full-price sync
    re-runs. Both constructors now default to the shared constant.
    """
    import inspect

    from src.score.client import DEFAULT_MAX_TOKENS, AnthropicClient

    assert BatchClient().max_tokens == DEFAULT_MAX_TOKENS
    sync_default = inspect.signature(AnthropicClient.__init__).parameters[
        "max_tokens"
    ].default
    assert sync_default == DEFAULT_MAX_TOKENS
