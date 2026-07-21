"""Pure-function tests for `report.fact_corrections` (issue #249) —
register resolution (applied / pending re-aggregate / record gone) and
the corrections-by-fact-name clustering. No filesystem; hand-built
dicts, the `test_override_clusters` idiom."""

from __future__ import annotations

from src.report.fact_corrections import (
    cluster_corrections_by_fact,
    resolve_corrections,
)


def _block(value=False, prior=True, lens="source", **kw):
    return {
        "value": value,
        "lens": lens,
        "author": kw.get("author", "Chris Moffatt"),
        "corrected_at": kw.get("corrected_at", "2026-07-13T12:00:00+00:00"),
        "rationale": kw.get("rationale", "calendar, not conditionality"),
        "prior_value": prior,
        "prior_provenance": "llm",
        "plan_version_at_correction": "28",
    }


def _score(fact="has_conditional_logic", value=False, corrected=True):
    prov = {"value": value, "confidence": "high", "downgraded": False,
            "downgrade_reason": None}
    if corrected:
        prov["provenance"] = "human_corrected"
    return {
        "record_key": "AZ|Calendar|calendarCode",
        "entity": "Calendar",
        "element_name": "calendarCode",
        "fact_provenance": {fact: prov},
    }


_KEY = "AZ|Calendar|calendarCode"


class TestResolveCorrections:
    def test_applied_when_sidecar_carries_the_correction(self):
        out = resolve_corrections(
            "AZ",
            {_KEY: {"has_conditional_logic": _block(value=False)}},
            {_KEY: _score(value=False, corrected=True)},
        )
        [entry] = out
        assert entry["status"] == "applied"
        assert entry["entity"] == "Calendar"
        assert entry["element_name"] == "calendarCode"
        assert entry["fact"] == "has_conditional_logic"
        assert entry["flip"] == "True → False"
        assert entry["lens"] == "source"
        assert entry["author"] == "Chris Moffatt"

    def test_pending_when_sidecar_predates_the_correction(self):
        # The sidecar still shows the extracted value with no
        # human_corrected marker — aggregate hasn't re-run.
        out = resolve_corrections(
            "AZ",
            {_KEY: {"has_conditional_logic": _block(value=False)}},
            {_KEY: _score(value=True, corrected=False)},
        )
        assert out[0]["status"] == "pending re-aggregate"

    def test_pending_when_value_moved_past_the_correction(self):
        # human_corrected marker present but under a DIFFERENT value
        # (a newer correction not yet re-aggregated) — still pending.
        out = resolve_corrections(
            "AZ",
            {_KEY: {"has_conditional_logic": _block(value=True)}},
            {_KEY: _score(value=False, corrected=True)},
        )
        assert out[0]["status"] == "pending re-aggregate"

    def test_record_gone_falls_back_to_key_identity(self):
        out = resolve_corrections(
            "AZ",
            {_KEY: {"has_conditional_logic": _block()}},
            {},
        )
        [entry] = out
        assert entry["status"] == "record gone"
        assert entry["entity"] == "Calendar"
        assert entry["element_name"] == "calendarCode"

    def test_sorted_by_key_then_fact(self):
        corrections = {
            "AZ|School|schoolId": {"semantic_class": _block(
                value="divergent", prior="aligned")},
            _KEY: {
                "semantic_class": _block(value="divergent",
                                         prior="aligned"),
                "has_conditional_logic": _block(),
            },
        }
        out = resolve_corrections("AZ", corrections, {})
        assert [(e["record_key"], e["fact"]) for e in out] == [
            (_KEY, "has_conditional_logic"),
            (_KEY, "semantic_class"),
            ("AZ|School|schoolId", "semantic_class"),
        ]


class TestClusterCorrectionsByFact:
    def test_same_fact_same_flip_clusters_across_states(self):
        by_state = {
            "TX": {"TX|A|x": {"has_conditional_logic": _block()},
                   "TX|A|y": {"has_conditional_logic": _block()}},
            "AZ": {_KEY: {"has_conditional_logic": _block()}},
        }
        [cluster] = cluster_corrections_by_fact(by_state)
        assert cluster["fact"] == "has_conditional_logic"
        assert cluster["flip"] == "True → False"
        assert cluster["rows"] == 3
        assert cluster["state_counts"] == {"AZ": 1, "TX": 2}

    def test_different_flips_do_not_merge(self):
        by_state = {"AZ": {
            _KEY: {"has_conditional_logic": _block(value=False,
                                                   prior=True)},
            "AZ|School|schoolId": {"has_conditional_logic": _block(
                value=True, prior=False)},
        }}
        clusters = cluster_corrections_by_fact(by_state)
        assert len(clusters) == 2
        assert {c["flip"] for c in clusters} == {
            "True → False", "False → True",
        }

    def test_ordering_biggest_first_then_fact_name(self):
        by_state = {"AZ": {
            _KEY: {"semantic_class": _block(value="divergent",
                                            prior="aligned")},
            "AZ|School|schoolId": {
                "has_conditional_logic": _block(),
                "semantic_class": _block(value="divergent",
                                         prior="aligned"),
            },
        }}
        clusters = cluster_corrections_by_fact(by_state)
        assert [(c["fact"], c["rows"]) for c in clusters] == [
            ("semantic_class", 2),
            ("has_conditional_logic", 1),
        ]

    def test_row_counts_reconcile_to_total_corrections(self):
        by_state = {"AZ": {
            _KEY: {"has_conditional_logic": _block(),
                   "semantic_class": _block(value="divergent",
                                            prior="aligned")},
        }}
        clusters = cluster_corrections_by_fact(by_state)
        assert sum(c["rows"] for c in clusters) == 2

    def test_empty_input_is_empty(self):
        assert cluster_corrections_by_fact({}) == []
        assert cluster_corrections_by_fact({"AZ": {}}) == []
