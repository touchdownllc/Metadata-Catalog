"""Peer-state triangulation runner — contract + behaviour tests.

Covers the Day 4 scope from `docs/archive/nachos-v2-two-axis-plan.md` §4
Mitigation 4:

- module-level `run()` is a plain function (POC-2 CliWiring trap).
- output JSON-schema rejects malformed payloads (slot_key mismatch,
  missing required keys, bad enum on posture/confidence).
- dry-run renders prompts + manifest without constructing the LLM client.
- cache hits skip the LLM entirely — replays read identical bytes.
- cost-cap is a hard stop before the next slot.
- the artifact carries a header row first, then one row per slot, with
  states_present + structural_tiers reflecting input bundles.
- multi-state slot bundling: states without `documented=True` for a slot
  are dropped from the bundle.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
import pytest

from src.models.element import ElementRecord, StateElements
from src.score import peer_gap as peer_gap_module
from src.score.client import LLMResponse
from src.score.peer_gap import (
    PROMPT_VERSION,
    SEED_SLOTS,
    SlotBundle,
    StateSlotView,
    _validate_output,
    all_multi_state_slots,
    build_slot_bundles,
    render_prompt,
    run as run_peer_gap,
)
from src.score.schema import CostCapExceeded, ScoringSchemaError


# ---------------------------------------------------------------------------
# CLI wiring regression
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_run_is_plain_function_not_click_command(self) -> None:
        assert not isinstance(peer_gap_module.run, click.Command)
        assert inspect.isfunction(peer_gap_module.run)


# ---------------------------------------------------------------------------
# Output schema validation
# ---------------------------------------------------------------------------


def _good_payload(slot_key: str = "Calendar|calendarCode") -> dict[str, Any]:
    return {
        "slot_key": slot_key,
        "consensus_concept": "All states agree this is a calendar code.",
        "per_state_divergences": [
            {"state": "AZ", "posture": "prescriptive", "summary": "AZ prescribes."},
            {"state": "WI", "posture": "conceptual", "summary": "WI describes."},
        ],
        "suggested_fills": [
            {
                "state": "WI", "current_posture": "conceptual",
                "peer_consensus_format": "AZ format example",
                "recommended_fill": "Consider documenting an assembly recipe.",
            }
        ],
        "confidence": "medium",
        "confidence_rationale": "Two states agree.",
    }


class TestOutputSchema:
    def test_good_payload_validates(self) -> None:
        out = _validate_output(_good_payload(), "Calendar|calendarCode")
        assert out["slot_key"] == "Calendar|calendarCode"

    def test_slot_key_mismatch_rejected(self) -> None:
        with pytest.raises(ScoringSchemaError):
            _validate_output(_good_payload("Wrong|key"), "Calendar|calendarCode")

    def test_missing_required_key_rejected(self) -> None:
        bad = _good_payload()
        del bad["confidence"]
        with pytest.raises(ScoringSchemaError):
            _validate_output(bad, bad["slot_key"])

    def test_bad_posture_enum_rejected(self) -> None:
        bad = _good_payload()
        bad["per_state_divergences"][0]["posture"] = "informative"  # not in enum
        with pytest.raises(ScoringSchemaError):
            _validate_output(bad, bad["slot_key"])

    def test_bad_state_enum_rejected(self) -> None:
        bad = _good_payload()
        bad["per_state_divergences"][0]["state"] = "CA"  # not supported
        with pytest.raises(ScoringSchemaError):
            _validate_output(bad, bad["slot_key"])

    def test_empty_suggested_fills_allowed(self) -> None:
        good = _good_payload()
        good["suggested_fills"] = []
        out = _validate_output(good, good["slot_key"])
        assert out["suggested_fills"] == []


# ---------------------------------------------------------------------------
# Bundle builder
# ---------------------------------------------------------------------------


def _stub_record(state: str, entity: str, element: str, *, documented: bool = True,
                 source: str = "core", definition: str = "stub def",
                 data_type: str = "String") -> ElementRecord:
    return ElementRecord.model_validate({
        "state": state,
        "edfi_version": "3",
        "domain": "TestDomain",
        "entity": entity,
        "raw_entity": None,
        "element_name": element,
        "data_type": data_type,
        "definition_text": definition,
        "source": source,
        "extension_name": None,
        "business_rules_text": "",
        "element_specific_rules": "",
        "regulatory_citations": [],
        "related_entities": [],
        "descriptor_table_code": None,
        "descriptor_table_values": [],
        "collections_text": None,
        "edfi_standard_definition": "Ed-Fi standard definition.",
        "source_document": "test",
        "source_page_or_section": None,
        "documented": documented,
    })


def _write_state_elements(tmp_path: Path, state: str, records: list[ElementRecord]) -> Path:
    payload = StateElements(
        state=state,
        edfi_version="3",
        extracted_at="2026-04-27T00:00:00Z",
        element_count=len(records),
        elements=records,
    )
    path = tmp_path / f"{state.lower()}_elements_spine.json"
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    return path


def test_build_slot_bundles_drops_undocumented_state(tmp_path: Path) -> None:
    paths = {}
    paths["AZ"] = _write_state_elements(tmp_path, "AZ", [
        _stub_record("AZ", "Calendar", "calendarCode"),
    ])
    paths["WI"] = _write_state_elements(tmp_path, "WI", [
        _stub_record("WI", "Calendar", "calendarCode", documented=False),
    ])
    paths["MN"] = _write_state_elements(tmp_path, "MN", [])
    paths["TX"] = _write_state_elements(tmp_path, "TX", [
        _stub_record("TX", "Calendar", "calendarCode"),
    ])
    paths["IN"] = _write_state_elements(tmp_path, "IN", [])  # absent slot
    bundles = build_slot_bundles(
        [("Calendar", "calendarCode")],
        elements_paths=paths,
        scores_paths={},  # no struct tier data
    )
    assert len(bundles) == 1
    states = [v.state for v in bundles[0].states]
    assert states == ["AZ", "TX"], "WI undocumented → dropped; MN+IN absent → dropped"


def test_build_slot_bundles_skips_filtered_source(tmp_path: Path) -> None:
    paths = {}
    paths["AZ"] = _write_state_elements(tmp_path, "AZ", [
        _stub_record("AZ", "Calendar", "calendarCode", source="filtered"),
    ])
    paths["WI"] = _write_state_elements(tmp_path, "WI", [
        _stub_record("WI", "Calendar", "calendarCode"),
    ])
    paths["MN"] = _write_state_elements(tmp_path, "MN", [])
    paths["TX"] = _write_state_elements(tmp_path, "TX", [])
    paths["IN"] = _write_state_elements(tmp_path, "IN", [])
    bundles = build_slot_bundles(
        [("Calendar", "calendarCode")],
        elements_paths=paths,
        scores_paths={},
    )
    assert len(bundles) == 1
    assert [v.state for v in bundles[0].states] == ["WI"]


def test_build_slot_bundles_drops_zero_state_slots(tmp_path: Path) -> None:
    paths = {st: _write_state_elements(tmp_path, st, []) for st in ("AZ", "WI", "MN", "TX", "IN")}
    bundles = build_slot_bundles(
        [("Nonexistent", "field")],
        elements_paths=paths,
        scores_paths={},
    )
    assert bundles == []


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _bundle_for_render() -> SlotBundle:
    return SlotBundle(
        entity="Calendar",
        element_name="calendarCode",
        data_type="String",
        edfi_standard_definition="The unique calendar identifier.",
        states=(
            StateSlotView(
                state="AZ", documented=True, structural_tier=3,
                definition_text="Submit LEAID-SchoolId-...",
                business_rules_text="",
                element_specific_rules="",
                edfi_standard_definition="",
                data_type="String", domain="SchoolCalendar",
            ),
            StateSlotView(
                state="WI", documented=True, structural_tier=3,
                definition_text="The calendar identifier.",
                business_rules_text="",
                element_specific_rules="",
                edfi_standard_definition="",
                data_type="String", domain="SchoolCalendar",
            ),
        ),
    )


def test_render_prompt_carries_slot_metadata() -> None:
    bundle = _bundle_for_render()
    system, user = render_prompt(bundle)
    assert "slot_key: Calendar|calendarCode" in user
    assert "states_present" in user
    assert "AZ, WI" in user
    assert "state: AZ" in user
    assert "state: WI" in user
    assert "structural_tier: 3" in user
    assert "Submit LEAID-SchoolId-..." in user
    assert "The unique calendar identifier." in user  # edfi standard def


# ---------------------------------------------------------------------------
# Runner — dry-run + cache + cost-cap
# ---------------------------------------------------------------------------


@dataclass
class ScriptedClient:
    """Minimal AnthropicClient stand-in. FIFO over `responses`."""
    responses: list[Any]
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 8192
    calls: list[dict[str, Any]] = field(default_factory=list)

    def call(self, *, system_text: str, user_text: str, cache_system: bool = True) -> LLMResponse:
        if not self.responses:
            raise AssertionError("ScriptedClient ran out of responses")
        nxt = self.responses.pop(0)
        self.calls.append({"system_text": system_text, "user_text": user_text})
        if isinstance(nxt, LLMResponse):
            return nxt
        return LLMResponse(
            payload=nxt,
            raw_text=json.dumps(nxt),
            tokens_in=200,
            tokens_out=80,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            usd=0.001,
            model=self.model,
        )


def _seed_two_state_slot(tmp_path: Path) -> dict[str, Path]:
    paths = {}
    paths["AZ"] = _write_state_elements(tmp_path, "AZ", [
        _stub_record("AZ", "Calendar", "calendarCode", definition="AZ format"),
    ])
    paths["WI"] = _write_state_elements(tmp_path, "WI", [
        _stub_record("WI", "Calendar", "calendarCode", definition="WI concept"),
    ])
    paths["MN"] = _write_state_elements(tmp_path, "MN", [])
    paths["TX"] = _write_state_elements(tmp_path, "TX", [])
    paths["IN"] = _write_state_elements(tmp_path, "IN", [])
    return paths


def test_dry_run_skips_llm_and_writes_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run never constructs AnthropicClient and emits a manifest + per-slot prompt files."""
    paths = _seed_two_state_slot(tmp_path)

    def _fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dry-run must not construct AnthropicClient")
    monkeypatch.setattr(peer_gap_module, "AnthropicClient", _fail)

    out_dir = tmp_path / "out"
    header = run_peer_gap(
        slots=[("Calendar", "calendarCode")],
        out_dir=out_dir,
        elements_paths=paths,
        scores_paths={},
        dry_run=True,
    )
    assert header["mode"] == "dry-run"
    dry_dir = out_dir / "dry_run_peer_gap"
    assert (dry_dir / "manifest.json").exists()
    assert list(dry_dir.glob("*.prompt.md"))


def test_cache_hit_skips_llm(tmp_path: Path) -> None:
    paths = _seed_two_state_slot(tmp_path)
    payload = _good_payload("Calendar|calendarCode")
    client = ScriptedClient(responses=[payload])

    out_dir = tmp_path / "out"
    cache_root = tmp_path / "cache"

    # First run populates the cache.
    h1 = run_peer_gap(
        slots=[("Calendar", "calendarCode")],
        out_dir=out_dir,
        elements_paths=paths,
        scores_paths={},
        client=client,
        cache_root=cache_root,
    )
    assert h1["scored_count"] == 1
    assert h1["cache_hit_count"] == 0

    # Second run consumes the cache; if the runner over-calls, the
    # ScriptedClient would error (responses list is empty).
    client2 = ScriptedClient(responses=[])
    h2 = run_peer_gap(
        slots=[("Calendar", "calendarCode")],
        out_dir=out_dir,
        elements_paths=paths,
        scores_paths={},
        client=client2,
        cache_root=cache_root,
    )
    assert h2["scored_count"] == 1
    assert h2["cache_hit_count"] == 1
    assert client2.calls == []


def test_cost_cap_aborts_before_next_call(tmp_path: Path) -> None:
    paths = {}
    paths["AZ"] = _write_state_elements(tmp_path, "AZ", [
        _stub_record("AZ", "Calendar", "calendarCode"),
        _stub_record("AZ", "Course", "courseCode"),
    ])
    paths["WI"] = _write_state_elements(tmp_path, "WI", [
        _stub_record("WI", "Calendar", "calendarCode"),
        _stub_record("WI", "Course", "courseCode"),
    ])
    paths["MN"] = _write_state_elements(tmp_path, "MN", [])
    paths["TX"] = _write_state_elements(tmp_path, "TX", [])

    pricey = LLMResponse(
        payload=_good_payload("Calendar|calendarCode"),
        raw_text="",
        tokens_in=10, tokens_out=10,
        cache_read_tokens=0, cache_creation_tokens=0,
        usd=10.0,  # blow past any sane cap
        model="claude-sonnet-4-6",
    )
    client = ScriptedClient(responses=[pricey, _good_payload("Course|courseCode")])

    out_dir = tmp_path / "out"
    cache_root = tmp_path / "cache"
    with pytest.raises(CostCapExceeded):
        run_peer_gap(
            slots=[("Calendar", "calendarCode"), ("Course", "courseCode")],
            states=("AZ", "WI", "MN", "TX"),
            out_dir=out_dir,
            elements_paths=paths,
            scores_paths={},
            client=client,
            cache_root=cache_root,
            cost_cap=1.0,
        )

    # Aborted artifact carries the partial work and status="aborted".
    artifact = out_dir / "peer_gap.jsonl"
    with artifact.open() as fh:
        head = json.loads(fh.readline())
    assert head["status"] == "aborted"
    assert head["cost_cap_hit"] is True
    assert head["scored_count"] == 1  # first slot landed before cap fired


def test_artifact_row_carries_states_and_tiers(tmp_path: Path) -> None:
    paths = _seed_two_state_slot(tmp_path)
    # Point scores_paths at non-existent files so the loader returns {}
    # for every state — keeps the test independent of `data/out/` state.
    scores = {st: tmp_path / f"missing_{st}_scores.json" for st in ("AZ", "WI", "MN", "TX", "IN")}
    payload = _good_payload("Calendar|calendarCode")
    client = ScriptedClient(responses=[payload])
    out_dir = tmp_path / "out"
    cache_root = tmp_path / "cache"
    run_peer_gap(
        slots=[("Calendar", "calendarCode")],
        out_dir=out_dir,
        elements_paths=paths,
        scores_paths=scores,
        client=client,
        cache_root=cache_root,
    )
    rows = []
    with (out_dir / "peer_gap.jsonl").open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    assert rows[0]["__type"] == "header"
    assert rows[0]["status"] == "complete"
    body = rows[1]
    assert body["slot_key"] == "Calendar|calendarCode"
    assert body["states_present"] == ["AZ", "WI"]
    assert body["structural_tiers"] == {"AZ": None, "WI": None}  # no scores_paths in fixture
    assert body["confidence"] == "medium"
    assert body["prompt_version"] == PROMPT_VERSION


# ---------------------------------------------------------------------------
# Slot manifests — historical seed + full multi-state enumeration
# ---------------------------------------------------------------------------


def test_seed_slots_size_and_uniqueness() -> None:
    """The seed manifest is exactly 10 slots (plan §5 Day 4 pilot) and unique.

    Frozen post-pilot so reproducibility of the 2026-04-24 run survives —
    the pilot validated peer-gap signal quality before the full fanout.
    """
    assert len(SEED_SLOTS) == 10
    assert len(set(SEED_SLOTS)) == 10


def test_all_multi_state_slots_filters_to_documented(tmp_path: Path) -> None:
    """``all_multi_state_slots`` returns every slot documented in ≥2 states,
    sorted by (entity, element), and excludes single-state + undocumented slots.
    """
    from src.models.element import StateElements

    def _write(state: str, rows: list[dict[str, Any]]) -> Path:
        data = StateElements.model_validate({
            "state": state, "edfi_version": "3",
            "extracted_at": "2026-04-27T00:00:00Z",
            "element_count": len(rows), "elements": rows,
        })
        path = tmp_path / f"{state.lower()}_elements_spine.json"
        path.write_text(data.model_dump_json(indent=2), encoding="utf-8")
        return path

    def _row(
        entity: str, element: str, *, documented: bool = True,
        source: str = "core",
    ) -> dict[str, Any]:
        return {
            "state": "AZ", "edfi_version": "3", "domain": "Test",
            "entity": entity, "element_name": element, "data_type": "String",
            "definition_text": "x" if documented else "",
            "source": source, "extension_name": None,
            "business_rules_text": None, "element_specific_rules": None,
            "regulatory_citations": [], "related_entities": [],
            "descriptor_table_code": None, "descriptor_table_values": [],
            "collections_text": None, "edfi_standard_definition": None,
            "source_document": None, "source_page_or_section": None,
            "documented": documented,
        }

    paths = {
        "AZ": _write("AZ", [
            _row("Shared", "field"),
            _row("AZonly", "field"),
            _row("FilteredAZ", "field", source="filtered"),
        ]),
        "WI": _write("WI", [
            _row("Shared", "field"),
            _row("WionlyUndocumented", "field", documented=False),
        ]),
        "MN": _write("MN", [_row("Shared", "field")]),
        "TX": _write("TX", []),
    }
    slots = all_multi_state_slots(
        states=("AZ", "WI", "MN", "TX"), elements_paths=paths,
    )
    # "Shared" documented in AZ+WI+MN → included (count 3 ≥ 2).
    assert ("Shared", "field") in slots
    # Everything else appears in only one state (or zero after filters).
    assert ("AZonly", "field") not in slots
    assert ("WionlyUndocumented", "field") not in slots
    assert ("FilteredAZ", "field") not in slots
    # Sorted deterministically.
    assert list(slots) == sorted(slots)


def test_all_multi_state_slots_min_states_param(tmp_path: Path) -> None:
    """The ``min_states`` knob tightens / loosens the threshold."""
    from src.models.element import StateElements

    def _row(entity: str, element: str) -> dict[str, Any]:
        return {
            "state": "AZ", "edfi_version": "3", "domain": "Test",
            "entity": entity, "element_name": element, "data_type": "String",
            "definition_text": "x", "source": "core", "extension_name": None,
            "business_rules_text": None, "element_specific_rules": None,
            "regulatory_citations": [], "related_entities": [],
            "descriptor_table_code": None, "descriptor_table_values": [],
            "collections_text": None, "edfi_standard_definition": None,
            "source_document": None, "source_page_or_section": None,
            "documented": True,
        }

    paths = {}
    for state, rows in [
        ("AZ", [_row("E", "a"), _row("E", "b")]),
        ("WI", [_row("E", "a")]),
        ("MN", []),
        ("TX", []),
    ]:
        data = StateElements.model_validate({
            "state": state, "edfi_version": "3",
            "extracted_at": "2026-04-27T00:00:00Z",
            "element_count": len(rows), "elements": rows,
        })
        p = tmp_path / f"{state.lower()}_elements_spine.json"
        p.write_text(data.model_dump_json(indent=2), encoding="utf-8")
        paths[state] = p

    # With default min_states=2: (E,a) qualifies (AZ+WI), (E,b) does not.
    slots = all_multi_state_slots(
        states=("AZ", "WI", "MN", "TX"), elements_paths=paths,
    )
    assert ("E", "a") in slots
    assert ("E", "b") not in slots
    # min_states=1 returns both.
    slots_all = all_multi_state_slots(
        states=("AZ", "WI", "MN", "TX"), elements_paths=paths, min_states=1,
    )
    assert ("E", "a") in slots_all
    assert ("E", "b") in slots_all


def test_all_multi_state_slots_partial_paths_raises(tmp_path: Path) -> None:
    """An explicit ``elements_paths`` that omits a rostered state raises.

    Regression for issue #211 item 2: the omitted state used to fall
    back silently to the real ``data/out/{state}_elements_spine.json`` —
    red suite on fresh clones, and "hermetic" tests quietly reading the
    production IN artifact on populated machines.
    """
    from src.models.element import StateElements

    paths = {}
    for state in ("AZ", "WI", "MN", "TX"):  # deliberately no IN
        data = StateElements.model_validate({
            "state": state, "edfi_version": "3",
            "extracted_at": "2026-04-27T00:00:00Z",
            "element_count": 0, "elements": [],
        })
        p = tmp_path / f"{state.lower()}_elements_spine.json"
        p.write_text(data.model_dump_json(indent=2), encoding="utf-8")
        paths[state] = p

    with pytest.raises(ValueError, match="missing states: IN"):
        all_multi_state_slots(elements_paths=paths)
    with pytest.raises(ValueError, match="missing states: IN"):
        build_slot_bundles([("E", "a")], elements_paths=paths)
