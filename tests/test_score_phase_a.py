"""Phase A binary-fact extraction harness — contract + behaviour tests.

Covers the success criteria from `docs/next-session-scoring-phase-a.md`:

- dry-run renders prompts + manifest with no network
- strict JSON Schema halts on drift
- cache keys are stable + deterministic
- cache hits skip the LLM
- cost cap is a hard stop
- evidence-span validator: exact / whitespace / paraphrase-reject
- downgrade flow preserves raw claim + sets hallucinated_span reason
- red-team fixture catches at least one hallucinated span
- dry-run replay is byte-identical to the committed goldens
- `src/score/` imports don't regress any existing test module

No live LLM calls anywhere — the `AnthropicClient` is monkeypatched with
a scripted double that returns controlled payloads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from src.models.element import ElementRecord
from src.score import extract as extract_module
from src.score.cache import Cache, cache_key
from src.score.client import LLMResponse
from src.score.extract import (
    PROMPT_VERSION,
    _canonical_prompt,
    _process_payload,
    load_phase_a_records,
    render_prompt,
    run as run_extract,
    validate_span,
)
from src.score.schema import (
    FACT_OUTPUT_SCHEMA,
    CostCapExceeded,
    ScoringSchemaError,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN_ROOT = _REPO_ROOT / "tests" / "golden" / "scoring_phase_a"
_RED_TEAM_FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "scoring_phase_a_red_team.json"
_AZ_SPINE = _REPO_ROOT / "data" / "out" / "az_elements_spine.json"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _load_red_team() -> list[ElementRecord]:
    raw = json.loads(_RED_TEAM_FIXTURE.read_text(encoding="utf-8"))
    return [ElementRecord.model_validate({k: v for k, v in r.items() if not k.startswith("_")}) for r in raw]


@dataclass
class ScriptedClient:
    """Minimal AnthropicClient stand-in. Hands back pre-baked responses.

    `responses` is consumed FIFO, one per entity batch. Each entry is
    either a Python object (the `payload`) or an `LLMResponse` instance.
    Raises if the test over-calls.
    """

    responses: list[Any]
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def call(self, *, system_text: str, user_text: str, cache_system: bool = True) -> LLMResponse:
        if not self.responses:
            raise AssertionError("ScriptedClient ran out of responses — test over-calls LLM")
        next_resp = self.responses.pop(0)
        self.calls.append({"system_text": system_text, "user_text": user_text})
        if isinstance(next_resp, LLMResponse):
            return next_resp
        return LLMResponse(
            payload=next_resp,
            raw_text=json.dumps(next_resp),
            tokens_in=200,
            tokens_out=50,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            usd=0.0015,
            model=self.model,
        )


# ---------------------------------------------------------------------------
# CLI wiring regression
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self) -> None:
        """`run()` must stay a plain function. POC-2 trap: `@click.command`
        wrapper re-parses sys.argv when the CLI imports and calls run()."""
        import inspect
        assert not isinstance(extract_module.run, click.Command)
        assert inspect.isfunction(extract_module.run)


# ---------------------------------------------------------------------------
# Imports don't regress
# ---------------------------------------------------------------------------


def test_score_package_imports_cleanly() -> None:
    import importlib
    for mod in ("src.score", "src.score.extract", "src.score.cache", "src.score.client", "src.score.schema"):
        importlib.import_module(mod)


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


@pytest.mark.realdata
@pytest.mark.skipif(not _AZ_SPINE.exists(), reason="requires data/out/az_elements_spine.json")
def test_dry_run_skips_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run writes prompts + manifest and never imports the LLM client."""

    def _fail_on_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dry-run must not construct AnthropicClient")

    monkeypatch.setattr(extract_module, "AnthropicClient", _fail_on_client)

    header = run_extract(
        state="AZ",
        lens="spine",
        limit=20,
        dry_run=True,
        out_dir=tmp_path,
        prompt_version=PROMPT_VERSION,
    )
    assert header["mode"] == "dry-run"
    dry_dir = tmp_path / "dry_run"
    assert (dry_dir / "manifest.json").exists()
    prompt_files = list(dry_dir.glob("*.prompt.md"))
    assert len(prompt_files) == header["entities_processed"] > 0


@pytest.mark.realdata
def test_dry_run_no_api_key_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With ANTHROPIC_API_KEY unset, --dry-run still exits 0."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if not _AZ_SPINE.exists():
        pytest.skip("requires data/out/az_elements_spine.json")
    from src.cli import cli
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "score", "extract",
            "--dry-run",
            "--limit", "4",
        ],
    )
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Golden replay
# ---------------------------------------------------------------------------


@pytest.mark.realdata
@pytest.mark.skipif(not _AZ_SPINE.exists(), reason="requires data/out/az_elements_spine.json")
def test_dry_run_golden_replay(tmp_path: Path) -> None:
    """Byte-identical replay of the committed Phase A dry-run goldens.

    Excludes nothing — the dry-run manifest carries no timestamps, and
    the prompt files are deterministic by construction. If this drifts,
    regenerate via `scripts/refresh_goldens_phase_a.py` and review the
    diff in the PR.
    """
    run_extract(
        state="AZ",
        lens="spine",
        limit=200,
        dry_run=True,
        out_dir=tmp_path,
        prompt_version=PROMPT_VERSION,
    )
    regenerated_manifest = json.loads(
        (tmp_path / "dry_run" / "manifest.json").read_text(encoding="utf-8")
    )
    golden_manifest = json.loads(
        (_GOLDEN_ROOT / "az_dry_run_manifest.json").read_text(encoding="utf-8")
    )
    assert regenerated_manifest == golden_manifest, (
        "Phase A dry-run manifest drifted; "
        "regenerate via scripts/refresh_goldens_phase_a.py if intentional"
    )

    for golden_prompt in sorted((_GOLDEN_ROOT / "prompts").glob("*.prompt.md")):
        regenerated = (tmp_path / "dry_run" / golden_prompt.name).read_text(encoding="utf-8")
        expected = golden_prompt.read_text(encoding="utf-8")
        assert regenerated == expected, f"Prompt drifted: {golden_prompt.name}"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_schema_validation_halts_on_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema drift raises ScoringSchemaError; CLI surfaces exit code 3."""
    records = _load_red_team()
    _patch_load_records(monkeypatch, records)

    # Response missing required "confidence" field.
    bad_payload = [
        {
            "element_name": records[0].element_name,
            "has_conditional_logic": False,
            "spans": [],
        }
    ]
    client = ScriptedClient(responses=[bad_payload])

    with pytest.raises(ScoringSchemaError):
        run_extract(
            state="AZ",
            lens="spine",
            limit=1,
            out_dir=tmp_path,
            cache_root=tmp_path / "cache",
            client=client,
        )


def test_cli_schema_error_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI exits 3 on schema drift (wire-up test for the click handler)."""
    records = _load_red_team()
    _patch_load_records(monkeypatch, records)

    client = ScriptedClient(
        responses=[
            [{"element_name": records[0].element_name, "has_conditional_logic": "not-bool", "spans": [], "confidence": "high"}]
        ]
    )

    def _fake_client_ctor(**_: Any) -> ScriptedClient:
        return client

    monkeypatch.setattr(extract_module, "AnthropicClient", _fake_client_ctor)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    # Redirect the default artifact path into tmp_path so the CLI-driven
    # test never writes to production `data/out/scoring/phase_a/`.
    monkeypatch.setattr(
        extract_module,
        "scoring_phase_a_artifact_path",
        lambda state, fact, lens="spine": tmp_path / f"{state.upper()}_{lens}_{fact}.jsonl",
    )

    from src.cli import cli
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "score", "extract",
            "--limit", "1",
            "--cost-cap", "10",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 3, result.output


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_key_stable() -> None:
    """Same inputs -> same SHA; bumping prompt_version changes SHA."""
    k1 = cache_key("hello prompt", "claude-sonnet-4-6", "phase-a.v1")
    k2 = cache_key("hello prompt", "claude-sonnet-4-6", "phase-a.v1")
    k3 = cache_key("hello prompt", "claude-sonnet-4-6", "phase-a.v2")
    k4 = cache_key("hello prompt", "claude-opus-4-7", "phase-a.v1")
    assert k1 == k2
    assert k1 != k3
    assert k1 != k4
    assert len(k1) == 64  # SHA-256 hex


def test_cache_get_skips_malformed_lines(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A partial-write or otherwise corrupt JSONL line must not crash
    lookups for unrelated keys. POC-3 saw this in the wild on 2026-04-20
    when a partial write left a literal `Mere{` prefix on one entry.
    """
    cache = Cache("claude-sonnet-4-6", "phase-a.v1", root=tmp_path)
    key_good = cache_key("good prompt", "claude-sonnet-4-6", "phase-a.v1")
    cache.put(
        key_good,
        [{"element_name": "x", "has_conditional_logic": False, "spans": [], "confidence": "high"}],
        tokens_in=100, tokens_out=20, usd=0.001, cache_read_tokens=0,
    )
    # Corrupt: prepend a bad line, then the legit line stays below.
    shard = tmp_path / f"{key_good[:2]}.jsonl"
    original = shard.read_text(encoding="utf-8")
    shard.write_text("not-json-at-all\n" + original, encoding="utf-8")

    caplog.set_level("WARNING")
    hit = cache.get(key_good)
    assert hit is not None
    assert hit["key"] == key_good
    assert any("malformed cache line" in rec.message for rec in caplog.records)


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = Cache("claude-sonnet-4-6", "phase-a.v1", root=tmp_path)
    key = cache_key("prompt body", "claude-sonnet-4-6", "phase-a.v1")
    assert cache.get(key) is None

    payload = [{"element_name": "x", "has_conditional_logic": False, "spans": [], "confidence": "high"}]
    entry = cache.put(key, payload, tokens_in=100, tokens_out=20, usd=0.001, cache_read_tokens=0)
    assert entry["response"] == payload

    hit = cache.get(key)
    assert hit is not None
    assert hit["response"] == payload
    assert hit["tokens_in"] == 100


def test_cache_put_crash_leaves_prior_entries_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash partway through ``put`` must not corrupt prior entries.

    Issue #212 item 7: ``put`` is a single O_APPEND write + fsync, so a
    mid-write death can leave at most one partial TRAILING line — prior
    entries' bytes are physically untouched (appends never rewrite
    them, unlike the old read-shard-then-replace cycle, which was also
    unsafe across processes). ``get()`` skips the malformed tail.
    """
    import os as _os

    cache = Cache("claude-sonnet-4-6", "phase-a.v1", root=tmp_path)
    key_first = cache_key("first prompt", "claude-sonnet-4-6", "phase-a.v1")
    payload_first = [{"element_name": "a", "has_conditional_logic": False, "spans": [], "confidence": "high"}]
    cache.put(key_first, payload_first, tokens_in=1, tokens_out=1, usd=0.0, cache_read_tokens=0)
    shard = tmp_path / f"{key_first[:2]}.jsonl"
    before_bytes = shard.read_bytes()

    # Simulate the kernel killing the process mid-append: the first
    # os.write lands only half the line, then the "process dies".
    from src.score import cache as cache_mod

    real_write = _os.write

    def _partial_then_boom(fd: int, data) -> int:
        real_write(fd, bytes(data[: len(data) // 2]))
        raise OSError("simulated crash mid-append")

    monkeypatch.setattr(cache_mod.os, "write", _partial_then_boom)

    # Collide into the same shard via the SHA namespace.
    key_second = None
    for candidate in range(10_000):
        k = cache_key(f"second prompt {candidate}", "claude-sonnet-4-6", "phase-a.v1")
        if k[:2] == key_first[:2] and k != key_first:
            key_second = k
            break
    assert key_second is not None
    payload_second = [{"element_name": "b", "has_conditional_logic": True, "spans": [], "confidence": "high"}]
    with pytest.raises(OSError):
        cache.put(key_second, payload_second, tokens_in=1, tokens_out=1, usd=0.0, cache_read_tokens=0)
    monkeypatch.setattr(cache_mod.os, "write", real_write)

    # Prior bytes are an exact prefix of the shard; first entry still
    # reads; the partial tail is skipped, so key_second is a clean miss.
    assert shard.read_bytes().startswith(before_bytes)
    hit = cache.get(key_first)
    assert hit is not None and hit["key"] == key_first
    assert cache.get(key_second) is None

    # And the shard heals: a healthy re-put of the same key appends a
    # valid line after the partial one and reads back fine.
    cache.put(key_second, payload_second, tokens_in=1, tokens_out=1, usd=0.0, cache_read_tokens=0)
    hit2 = cache.get(key_second)
    assert hit2 is not None and hit2["key"] == key_second


def test_cache_put_appends_without_rewriting_existing_bytes(
    tmp_path: Path,
) -> None:
    """Two same-shard puts: the second physically appends — the first
    entry's bytes stay an untouched prefix (the additive-only contract,
    now enforced by construction; also what makes concurrent
    multi-process writers safe under O_APPEND)."""
    cache = Cache("claude-sonnet-4-6", "phase-a.v1", root=tmp_path)
    key_first = cache_key("first prompt", "claude-sonnet-4-6", "phase-a.v1")
    cache.put(key_first, [], tokens_in=1, tokens_out=1, usd=0.0, cache_read_tokens=0)
    shard = tmp_path / f"{key_first[:2]}.jsonl"
    before_bytes = shard.read_bytes()

    key_second = None
    for candidate in range(10_000):
        k = cache_key(f"second prompt {candidate}", "claude-sonnet-4-6", "phase-a.v1")
        if k[:2] == key_first[:2] and k != key_first:
            key_second = k
            break
    assert key_second is not None
    cache.put(key_second, [], tokens_in=1, tokens_out=1, usd=0.0, cache_read_tokens=0)
    after = shard.read_bytes()
    assert after.startswith(before_bytes)
    assert len(after) > len(before_bytes)
    assert cache.get(key_first) is not None
    assert cache.get(key_second) is not None


def test_write_artifact_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #212 item 7: the per-batch streaming artifact rewrite goes
    through tmp + fsync + os.replace — a crash mid-rewrite leaves the
    previous complete artifact, never a truncated JSONL."""
    # The writer is shared via score/dispatch.py since issue #213 item 3;
    # extract still re-exports it as `_write_artifact`.
    from src.score import dispatch as dispatch_mod
    from src.score.extract import _write_artifact

    path = tmp_path / "AZ_spine_x.jsonl"
    _write_artifact(path, {"__type": "header"}, [{"r": 1}])
    before = path.read_bytes()
    assert not path.with_suffix(".jsonl.tmp").exists()

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(dispatch_mod.os, "replace", _boom)
    with pytest.raises(OSError):
        _write_artifact(path, {"__type": "header"}, [{"r": 1}, {"r": 2}])
    assert path.read_bytes() == before  # pre-crash artifact intact


def test_process_payload_stamps_run_prompt_version() -> None:
    """Issue #212 item 7: rows carry the RUN's prompt_version, not the
    module constant — an overridden version produced rows whose
    provenance field lied (header right, rows wrong)."""
    records = _load_red_team()[:1]
    rec = records[0]
    payload = [{
        "element_name": rec.element_name,
        "has_conditional_logic": False,
        "spans": [],
        "confidence": "high",
    }]
    rows, _downgrades = _process_payload(
        payload, records, "has_conditional_logic",
        prompt_version="phase-a.v9-override",
    )
    assert all(r["prompt_version"] == "phase-a.v9-override" for r in rows)
    # The missing-from-response synthetic row is stamped the same way.
    rows, _downgrades = _process_payload(
        [], records, "has_conditional_logic",
        prompt_version="phase-a.v9-override",
    )
    assert rows[0]["downgrade_reason"] == "missing_from_response"
    assert rows[0]["prompt_version"] == "phase-a.v9-override"


def test_cache_hit_skips_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed cache with the answer; run() must not invoke the client."""
    records = _load_red_team()[:1]
    _patch_load_records(monkeypatch, records)

    # Compute the exact cache key the harness will look up.
    system_text, user_text = render_prompt(records[0].entity, records, "AZ")
    canonical = _canonical_prompt(system_text, user_text)
    key = cache_key(canonical, "claude-sonnet-4-6", PROMPT_VERSION)
    payload = [
        {
            "element_name": records[0].element_name,
            "has_conditional_logic": False,
            "spans": [],
            "confidence": "high",
        }
    ]
    cache = Cache("claude-sonnet-4-6", PROMPT_VERSION, root=tmp_path / "cache")
    cache.put(key, payload, tokens_in=200, tokens_out=50, usd=0.002, cache_read_tokens=0)

    client = ScriptedClient(responses=[])  # empty — must not be called

    header = run_extract(
        state="AZ",
        lens="spine",
        limit=1,
        out_dir=tmp_path,
        cache_root=tmp_path / "cache",
        client=client,
    )
    assert header["cache_hit_count"] == 1
    assert header["total_usd"] == 0.0
    assert client.calls == []


# ---------------------------------------------------------------------------
# Cost cap
# ---------------------------------------------------------------------------


def test_cost_cap_hard_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap halts before the next call; partial JSONL flushed."""
    records = _load_red_team()[:4]  # 4 distinct entities => 4 batches
    # Force distinct entities so each record becomes its own batch.
    for i, rec in enumerate(records):
        rec.entity = f"EntityBatch{i}"
    _patch_load_records(monkeypatch, records)

    expensive_response = LLMResponse(
        payload=[
            {
                "element_name": records[0].element_name,
                "has_conditional_logic": False,
                "spans": [],
                "confidence": "high",
            }
        ],
        raw_text="",
        tokens_in=400_000,
        tokens_out=1_000,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        usd=0.8,
        model="claude-sonnet-4-6",
    )
    # Rebuild payloads for each record — single-record batches.
    payloads: list[Any] = []
    for rec in records:
        payloads.append(
            LLMResponse(
                payload=[
                    {
                        "element_name": rec.element_name,
                        "has_conditional_logic": False,
                        "spans": [],
                        "confidence": "high",
                    }
                ],
                raw_text="",
                tokens_in=400_000,
                tokens_out=1_000,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                usd=0.8,
                model="claude-sonnet-4-6",
            )
        )
    client = ScriptedClient(responses=payloads)

    artifact = tmp_path / "AZ_spine_has_conditional_logic.jsonl"
    with pytest.raises(CostCapExceeded):
        run_extract(
            state="AZ",
            lens="spine",
            limit=4,
            out_dir=tmp_path,
            cache_root=tmp_path / "cache",
            client=client,
            cost_cap=1.0,
        )
    # Partial artifact exists and carries the cost_cap_hit flag.
    assert artifact.exists()
    lines = [json.loads(ln) for ln in artifact.read_text(encoding="utf-8").splitlines()]
    header = lines[0]
    assert header["cost_cap_hit"] is True
    # Spent $0.80 x 2 = $1.60 before the cap-check halts call 3, so 2 scored.
    assert header["scored_count"] == 2
    assert len(lines) == 3  # header + 2 rows


# ---------------------------------------------------------------------------
# Span validator
# ---------------------------------------------------------------------------


def _mk_record(**overrides: Any) -> ElementRecord:
    base = {
        "state": "AZ",
        "edfi_version": "3",
        "domain": "Test",
        "entity": "TestEntity",
        "element_name": "testField",
        "data_type": "String",
        "definition_text": "",
        "source": "core",
        "extension_name": None,
        "business_rules_text": None,
        "element_specific_rules": None,
        "regulatory_citations": [],
        "related_entities": [],
        "descriptor_table_code": None,
        "descriptor_table_values": [],
        "collections_text": None,
        "edfi_standard_definition": None,
        "source_document": None,
        "source_page_or_section": None,
        "documented": True,
    }
    base.update(overrides)
    return ElementRecord.model_validate(base)


class TestSpanValidator:
    def test_accepts_exact_match(self) -> None:
        rec = _mk_record(business_rules_text="If the student withdraws, report the exit date.")
        assert validate_span("If the student withdraws, report the exit date.", rec) is True

    def test_accepts_case_insensitive(self) -> None:
        rec = _mk_record(business_rules_text="IF the STUDENT withdraws.")
        assert validate_span("if the student withdraws.", rec) is True

    def test_normalizes_whitespace(self) -> None:
        rec = _mk_record(business_rules_text="If  the  student\n withdraws.")
        assert validate_span("If the student withdraws.", rec) is True

    def test_rejects_paraphrase(self) -> None:
        rec = _mk_record(business_rules_text="Count days attended excluding non-instructional days.")
        # LLM paraphrases into a different phrase — validator must refuse.
        assert validate_span("counts days while dropping non-school days", rec) is False

    def test_rejects_empty(self) -> None:
        rec = _mk_record(business_rules_text="Something else.")
        assert validate_span("", rec) is False

    def test_rejects_when_haystack_empty(self) -> None:
        rec = _mk_record()
        assert validate_span("anything", rec) is False


# ---------------------------------------------------------------------------
# Downgrade flow + red-team
# ---------------------------------------------------------------------------


def test_downgrade_flow_preserves_raw(tmp_path: Path) -> None:
    rec = _mk_record(
        element_name="xyz",
        business_rules_text="Count days attended excluding non-instructional days.",
    )
    payload = [
        {
            "element_name": "xyz",
            "has_conditional_logic": True,
            "spans": ["fabricated quote that does not appear"],
            "confidence": "high",
        }
    ]
    rows, downgrades = _process_payload(payload, [rec])
    assert downgrades == 1
    assert rows[0]["llm_value"] is True
    assert rows[0]["validated_value"] is None
    assert rows[0]["downgrade_reason"] == "hallucinated_span"
    assert rows[0]["spans"][0]["valid"] is False
    # Raw LLM claim preserved for audit.
    assert rows[0]["spans"][0]["text"] == "fabricated quote that does not appear"


def test_red_team_catches_hallucination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthesize a response that includes one hallucinated span; run the
    full harness on the red-team fixture; assert >=1 downgrade."""
    raw = json.loads(_RED_TEAM_FIXTURE.read_text(encoding="utf-8"))
    records = [
        ElementRecord.model_validate({k: v for k, v in r.items() if not k.startswith("_")})
        for r in raw
    ]
    expected_true_by_key = {
        (r["entity"], r["element_name"]): r["_red_team"]["expected"] is True
        for r in raw
    }

    # Re-sort by (entity, element_name) — matches the extractor's order.
    order_pairs = sorted(expected_true_by_key.keys())
    # Group by entity while preserving sort order.
    grouped: dict[str, list[ElementRecord]] = {}
    order: list[str] = []
    for rec in sorted(records, key=lambda r: (r.entity, r.element_name)):
        if rec.entity not in grouped:
            order.append(rec.entity)
            grouped[rec.entity] = []
        grouped[rec.entity].append(rec)

    responses: list[Any] = []
    hallucinated_once = False
    for entity in order:
        batch_payload = []
        for rec in grouped[entity]:
            should_be_true = expected_true_by_key[(rec.entity, rec.element_name)]
            if should_be_true and not hallucinated_once:
                batch_payload.append(
                    {
                        "element_name": rec.element_name,
                        "has_conditional_logic": True,
                        "spans": ["This quote is entirely fabricated."],
                        "confidence": "high",
                    }
                )
                hallucinated_once = True
            else:
                batch_payload.append(
                    {
                        "element_name": rec.element_name,
                        "has_conditional_logic": False,
                        "spans": [],
                        "confidence": "medium",
                    }
                )
        responses.append(batch_payload)
    assert hallucinated_once, "red-team fixture must contain at least one expected-True record"

    _patch_load_records(monkeypatch, records)
    client = ScriptedClient(responses=responses)

    header = run_extract(
        state="AZ",
        lens="spine",
        limit=len(records),
        out_dir=tmp_path,
        cache_root=tmp_path / "cache",
        client=client,
    )
    assert header["downgrade_count"] >= 1, "red-team run must catch >=1 hallucinated span"
    artifact = tmp_path / "AZ_spine_has_conditional_logic.jsonl"
    rows = [json.loads(ln) for ln in artifact.read_text(encoding="utf-8").splitlines()[1:]]
    downgraded = [r for r in rows if r["downgrade_reason"] == "hallucinated_span"]
    assert len(downgraded) >= 1


def test_red_team_fixture_matches_element_record_schema() -> None:
    records = _load_red_team()
    assert len(records) >= 12
    for rec in records:
        assert rec.state == "AZ"
        assert rec.documented is True


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


def test_fact_output_schema_accepts_minimal_valid_payload() -> None:
    import jsonschema
    payload = [
        {"element_name": "x", "has_conditional_logic": True, "spans": ["y"], "confidence": "high"},
        {"element_name": "z", "has_conditional_logic": False, "spans": [], "confidence": "low"},
    ]
    jsonschema.validate(payload, FACT_OUTPUT_SCHEMA)


def test_fact_output_schema_tolerates_additional_properties() -> None:
    """Additional properties are tolerated — ``_validate_payload`` logs
    the drift but doesn't raise. The cold-run TX fanout surfaced a
    ``data_type`` hallucination that crashed a pair at 500/594 records
    with ``additionalProperties: False`` enforcement; surviving the
    drift (with a warning in the run log) keeps the pair shippable."""
    import jsonschema
    payload = [
        {
            "element_name": "x",
            "has_conditional_logic": True,
            "spans": ["y"],
            "confidence": "high",
            "data_type": "String",  # LLM hallucination seen on TX
        }
    ]
    # No ValidationError — schema is permissive on unknown keys.
    jsonschema.validate(payload, FACT_OUTPUT_SCHEMA)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_load_records(monkeypatch: pytest.MonkeyPatch, records: list[ElementRecord]) -> None:
    """Replace the module's record loader with a fixture-driven stub."""

    def _load(
        state: str,
        lens: str,
        *,
        elements_path: Path | None = None,
        limit: int | None = None,
        source_filter: tuple[str, ...] | None = None,
    ) -> list[ElementRecord]:
        keep = list(records)
        if source_filter is not None:
            allowed = set(source_filter)
            keep = [r for r in keep if r.source in allowed]
        keep.sort(key=lambda r: (r.entity, r.element_name))
        if limit is not None:
            keep = keep[:limit]
        return keep

    monkeypatch.setattr(extract_module, "load_phase_a_records", _load)


# ---------------------------------------------------------------------------
# Source-filter (Phase D carryover #1)
# ---------------------------------------------------------------------------


@pytest.mark.realdata
class TestLoadPhaseARecordsSourceFilter:
    """``source_filter`` narrows the pool to rows whose ``source`` is in the
    allowed tuple — the Phase D extension-fact filter lives here."""

    def test_source_filter_extension_only(self) -> None:
        """AZ source-lens with ``source_filter=("extension",)`` returns only
        extension rows, not core or unknown."""
        if not _REPO_ROOT.joinpath("data", "out", "az_elements_source.json").exists():
            pytest.skip("AZ source-lens artifact not on disk")
        all_rows = load_phase_a_records("AZ", "source")
        ext_only = load_phase_a_records(
            "AZ", "source", source_filter=("extension",)
        )
        assert ext_only, "extension-only filter returned empty list"
        assert len(ext_only) < len(all_rows), (
            "source_filter did not narrow the pool — something's off"
        )
        assert all(r.source == "extension" for r in ext_only)

    def test_source_filter_none_keeps_all(self) -> None:
        """``source_filter=None`` is the unchanged pre-Phase-D behavior."""
        if not _REPO_ROOT.joinpath("data", "out", "az_elements_source.json").exists():
            pytest.skip("AZ source-lens artifact not on disk")
        baseline = load_phase_a_records("AZ", "source")
        explicit_none = load_phase_a_records("AZ", "source", source_filter=None)
        assert [(r.entity, r.element_name) for r in explicit_none] == [
            (r.entity, r.element_name) for r in baseline
        ]


class TestValidateOnlyMode:
    """``--validate-only N`` (Phase D carryover #3): short sanity run
    that clamps the pool to N records, redirects the artifact to a
    scratch subdir, and surfaces downgrade reasons without overwriting
    the committed per-state JSONL."""

    def test_validate_only_clamps_and_redirects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        records = [
            _mk_record(element_name=f"elem{i}", source="core", entity="Entity", definition_text="d")
            for i in range(10)
        ]
        _patch_load_records(monkeypatch, records)

        scripted = ScriptedClient(responses=[
            [
                {"element_name": f"elem{i}", "has_conditional_logic": False, "spans": [], "confidence": "high"}
                for i in range(3)
            ]
        ])

        header = run_extract(
            fact="has_conditional_logic",
            state="AZ",
            lens="spine",
            limit=10,
            cost_cap=5.0,
            out_dir=tmp_path,
            cache_root=tmp_path / "cache",
            client=scripted,
            validate_only=3,
        )
        # Pool clamped to 3.
        assert header["record_count"] == 3
        assert header["scored_count"] == 3
        # Mode marker surfaces to downstream consumers.
        assert header["mode"] == "validate-only"
        # Scratch artifact under validate_only/ subdir — committed JSONL
        # at the normal phase_a/ path is untouched.
        scratch = tmp_path / "validate_only" / "AZ_spine_has_conditional_logic.jsonl"
        assert scratch.exists()
        normal = tmp_path / "AZ_spine_has_conditional_logic.jsonl"
        assert not normal.exists()

    def test_validate_only_none_is_normal_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``validate_only=None`` preserves existing API: artifact lands
        at the normal path and header mode is "api"."""
        records = [
            _mk_record(element_name=f"elem{i}", source="core", entity="Entity", definition_text="d")
            for i in range(2)
        ]
        _patch_load_records(monkeypatch, records)

        scripted = ScriptedClient(responses=[
            [
                {"element_name": f"elem{i}", "has_conditional_logic": False, "spans": [], "confidence": "high"}
                for i in range(2)
            ]
        ])

        header = run_extract(
            fact="has_conditional_logic",
            state="AZ",
            lens="spine",
            limit=None,
            cost_cap=5.0,
            out_dir=tmp_path,
            cache_root=tmp_path / "cache",
            client=scripted,
        )
        assert header["mode"] == "api"
        assert (tmp_path / "AZ_spine_has_conditional_logic.jsonl").exists()


class TestExtractRunAppliesSourceFilter:
    """``extract.run()`` must narrow the pool via ``FACT_SOURCE_FILTERS``
    before batching — otherwise the LLM wastes spend on rows whose prompt
    doesn't apply to them (Phase D carryover #1)."""

    def test_extension_is_necessary_sees_only_extension_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With mixed core/extension/unknown input, the extractor forwards
        only ``source='extension'`` rows to the LLM batching."""
        records = [
            _mk_record(element_name="core_elem", source="core", entity="Student", definition_text="d"),
            _mk_record(element_name="ext_elem_a", source="extension", entity="Student", extension_name="ext", definition_text="d"),
            _mk_record(element_name="ext_elem_b", source="extension", entity="Student", extension_name="ext", definition_text="d"),
            _mk_record(element_name="unk_elem", source="unknown", entity="Student", definition_text="d"),
        ]
        _patch_load_records(monkeypatch, records)

        seen_user_texts: list[str] = []

        def _ok(rec_names: list[str]) -> dict:
            return {
                "payload": [
                    {
                        "element_name": n,
                        "extension_is_necessary": True,
                        "spans": ["d"],
                        "confidence": "high",
                    }
                    for n in rec_names
                ],
            }

        scripted = ScriptedClient(responses=[_ok(["ext_elem_a", "ext_elem_b"])["payload"]])

        orig_call = scripted.call

        def _tracking_call(*, system_text: str, user_text: str, cache_system: bool = True) -> LLMResponse:
            seen_user_texts.append(user_text)
            return orig_call(system_text=system_text, user_text=user_text, cache_system=cache_system)

        scripted.call = _tracking_call  # type: ignore[method-assign]

        header = run_extract(
            fact="extension_is_necessary",
            state="AZ",
            lens="source",
            limit=None,
            cost_cap=5.0,
            out_dir=tmp_path,
            cache_root=tmp_path / "cache",
            client=scripted,
        )
        # Only the two extension rows were scored; core + unknown were
        # filtered out before batching.
        assert header["record_count"] == 2
        assert header["scored_count"] == 2
        # The LLM never saw core_elem or unk_elem.
        assert len(seen_user_texts) == 1
        user_text = seen_user_texts[0]
        assert "ext_elem_a" in user_text
        assert "ext_elem_b" in user_text
        assert "core_elem" not in user_text
        assert "unk_elem" not in user_text

    def test_non_filtered_fact_sees_all_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fact not in ``FACT_SOURCE_FILTERS`` still sees every row — the
        filter is opt-in, not a universal narrowing."""
        records = [
            _mk_record(element_name="a", source="core", entity="Student", definition_text="d"),
            _mk_record(element_name="b", source="extension", entity="Student", definition_text="d"),
            _mk_record(element_name="c", source="unknown", entity="Student", definition_text="d"),
        ]
        _patch_load_records(monkeypatch, records)

        scripted = ScriptedClient(responses=[
            [
                {"element_name": n, "has_conditional_logic": False, "spans": [], "confidence": "high"}
                for n in ("a", "b", "c")
            ],
        ])

        header = run_extract(
            fact="has_conditional_logic",
            state="AZ",
            lens="spine",
            limit=None,
            cost_cap=5.0,
            out_dir=tmp_path,
            cache_root=tmp_path / "cache",
            client=scripted,
        )
        assert header["record_count"] == 3
        assert header["scored_count"] == 3
