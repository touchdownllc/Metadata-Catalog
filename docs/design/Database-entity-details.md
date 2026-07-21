# Database Entity Details

This document lists the entities and fields collected by the POC-3 pipeline.
Because the backend database is not finalized yet, it focuses on what each
stage produces (ingest, phase-A facts, and phase-C scoring) and how updates are
handled (snapshot-replace, status-upsert, or curation merge-preserve).

Terminology note: in this document, `spine` means the `Ed-Fi Swagger / API
model catalog` used as the API-model coverage lens.

---

## Entity: `source_elements`

**Summary** One row per element record extracted from a state's primary source
document (AZ XLSX, WI Confluence, MN Mapping Matrix, TX TWEDS, IN IDOE XLSX).
Rows in this table are the source-lens ingest output. They feed the
fact-extraction stage so the scoring pipeline can run against documentation the
state explicitly authored or backfilled from the Ed-Fi swagger.

**Created at stage** Ingest step — `poc3 ingest <state>`.

| Field | Description | Data Type |
|---|---|---|
| `state` | State code, e.g. `AZ`, `WI`, `MN`, `TX`, `IN` | TEXT |
| `edfi_version` | Ed-Fi version string from the artifact header | TEXT |
| `domain` | Source Area: the state's own grouping vocabulary (XLSX sheet, Confluence page, MN collection, IN API resource, TX TEDS entity). Empty for swagger-backfill rows and spine-lens rows the source doc never mentioned. Not the Ed-Fi domain; see edfi_domain. | TEXT |
| `edfi_domain` | Cross-state Ed-Fi domain, spine-derived from the entity; multi-domain entities joined with "; ". NULL when the entity resolves to no spine domain. | TEXT |
| `entity` | Normalized entity name, e.g. `Section` | TEXT |
| `raw_entity` | Original entity string from the source document, e.g. `edfi.Section` or `az.SectionExtension` | TEXT |
| `element_name` | Field or property name | TEXT |
| `data_type` | Canonical or source-verbatim data type | TEXT |
| `definition_text` | State-authored definition text, or text backfilled from the Ed-Fi swagger | TEXT |
| `source` | Element origin: `core`, `extension`, `unknown`, or `filtered` | TEXT |
| `extension_name` | Extension identifier when `source = 'extension'` | TEXT |
| `business_rules_text` | Entity-level shared business rules narrative | TEXT |
| `element_specific_rules` | Per-element rules or special instructions | TEXT |
| `regulatory_citations` | Array of extracted regulatory citations | JSONB |
| `related_entities` | Array of cross-entity references | JSONB |
| `descriptor_table_code` | Descriptor table identifier | TEXT |
| `descriptor_table_values` | Array of descriptor code/value objects | JSONB |
| `collections_text` | Raw collection or submission-scope text | TEXT |
| `edfi_standard_definition` | Ed-Fi standard definition when available from the swagger | TEXT |
| `source_document` | Originating source document name | TEXT |
| `source_page_or_section` | Location within the source document | TEXT |
| `documented` | Whether the state source explicitly documents this element | BOOLEAN |
| `documentation_source` | Provenance label: `source_doc`, `swagger`, or `swagger_leaf` | TEXT |

**Does ingest erase and recreate all rows for a state?** Yes. The current sync
deletes every row where `state = <state>` before inserting the new snapshot.
Each `poc3 ingest <state>` run produces a fresh set of rows for that state. No
historical snapshots are retained.

---

## Entity: `spine_elements`

**Summary** One row per element record produced by walking the Ed-Fi swagger
spine catalog directly, enriched with any documentation the state's source has
for that element. This is the spine-lens counterpart of `source_elements`. Rows
in this table enumerate the full spine catalog for a state and mark elements as
`documented` or `documented = false` depending on whether the state's source doc
covers them. Spine elements also feed fact extraction.

**Created at stage** Ingest step — same as `source_elements`.

| Field | Description | Data Type |
|---|---|---|
| `state` | State code | TEXT |
| `edfi_version` | Ed-Fi version string from the artifact header | TEXT |
| `domain` | Source Area: the state's own grouping vocabulary (XLSX sheet, Confluence page, MN collection, IN API resource, TX TEDS entity). Empty for swagger-backfill rows and spine-lens rows the source doc never mentioned. Not the Ed-Fi domain; see edfi_domain. | TEXT |
| `edfi_domain` | Cross-state Ed-Fi domain, spine-derived from the entity; multi-domain entities joined with "; ". NULL when the entity resolves to no spine domain. | TEXT |
| `entity` | Normalized entity name | TEXT |
| `raw_entity` | Original entity string from the spine or source | TEXT |
| `element_name` | Field or property name | TEXT |
| `data_type` | Canonical or source-verbatim data type | TEXT |
| `definition_text` | Definition text from state source when available, otherwise from the swagger | TEXT |
| `source` | Element origin: `core`, `extension`, `unknown`, or `filtered` | TEXT |
| `extension_name` | Extension identifier when `source = 'extension'` | TEXT |
| `business_rules_text` | Entity-level shared business rules narrative | TEXT |
| `element_specific_rules` | Per-element rules or special instructions | TEXT |
| `regulatory_citations` | Array of extracted regulatory citations | JSONB |
| `related_entities` | Array of cross-entity references | JSONB |
| `descriptor_table_code` | Descriptor table identifier | TEXT |
| `descriptor_table_values` | Array of descriptor code/value objects | JSONB |
| `collections_text` | Raw collection or submission-scope text | TEXT |
| `edfi_standard_definition` | Ed-Fi standard definition from the swagger | TEXT |
| `source_document` | Originating source document name | TEXT |
| `source_page_or_section` | Location within the source document | TEXT |
| `documented` | Whether the state source explicitly documents this element | BOOLEAN |
| `documentation_source` | Provenance label: `source_doc`, `swagger`, or `swagger_leaf` | TEXT |

**Does ingest erase and recreate all rows for a state?** Yes — same full-replace
behavior as `source_elements`. Deletes all rows for the state and inserts the
new spine-lens snapshot.

---

## Entity: `ingestion_status`

**Summary** One row per `(state, lens)` pair tracking the most recent successful
ingest sync. This is the metadata control record that tells you what was last
loaded, when it was loaded, and how many elements it contained. Query this table
to check whether a state's ingest data is current before starting fact
extraction.

**Created at stage** Written or upserted as part of Ingest step.

| Field | Description | Data Type |
|---|---|---|
| `state` | State code (PK component) | TEXT |
| `lens` | `source` or `spine` (PK component) | TEXT |
| `edfi_version` | Ed-Fi version from the artifact | TEXT |
| `extracted_at` | Timestamp when the ingest adapter produced the artifact | TIMESTAMPTZ |
| `element_count` | Number of element rows in the artifact | INTEGER |
| `source_inputs` | Common input list for the snapshot. Source lens stores unique source document filenames; spine lens stores the swagger URLs used to build the spine manifest. | JSONB |
| `status` | Sync status; current code always writes `completed` | TEXT |
| `synced_at` | Timestamp when the PostgreSQL sync ran | TIMESTAMPTZ |

**Does ingest erase and recreate all rows for a state?** No. This table is
upserted — the existing row for `(state, lens)` is updated in place on every
sync. It always reflects the latest sync run.

**Freshness lineage note** poc3 publish now writes a freshness
manifest (data/out/publish_manifest.json) with artifact-keyed sha256 lineage —
producer, input hashes, and versions for every artifact in the chain. It answers
the same "is this data current?" question as both status tables(`ingestion_status`, `score_status`) but with more
detail, and the WS-3 design doc says the run-ledger schema should start from it.
Worth referencing here so the DB design doesn't grow a parallel freshness
mechanism that drifts from the one the pipeline already maintains.

---

## Entity: `gap_logs`

**Summary** One row per state summarizing the gap analysis between the state's
source document and the Ed-Fi spine catalog. The gap log captures how well the
source document covers the spine, which spine elements are unmatched, and what
the unflatten recovery produced. This is a diagnostic artifact; it is computed
and stored after the ingest step.

**Created at stage** Ingest step — `poc3 ingest <state>` (or `poc3 ingest gap
<state>` for a gap-only refresh). The `gap_logs` table stores one summary row
per state.

| Field | Description | Data Type |
|---|---|---|
| `state` | State code (PK) | TEXT |
| `spine_source` | Relative path to the spine JSON file used for the gap computation | TEXT |
| `spine_unique_element_keys` | Total unique element keys in the spine catalog in scope | INTEGER |
| `spine_extension_element_count` | Count of extension elements in the spine | INTEGER |
| `source_element_count` | Number of source-lens rows considered during the gap analysis | INTEGER |
| `source_coverage` | JSON object with `matched`, `total`, `pct`, `note` | JSONB |
| `spine_coverage` | JSON object with `matched_unique_keys`, `total_spine_keys`, `pct`, `note` | JSONB |
| `unmatched_source_count` | Number of source rows that could not be matched to a spine key | INTEGER |
| `unflatten_recovered_count` | Number of keys recovered by the unflatten heuristic | INTEGER |
| `unflatten_recovered` | Array of objects describing each recovered mapping (from_entity, to_entity, sub_collection, element_name) | JSONB |
| `unmatched_by_entity` | Object keyed by entity name, with the count of unmatched elements for each | JSONB |
| `missing_from_docs_count` | Total spine keys not covered by the state's source doc | INTEGER |
| `missing_from_docs_samples` | Sample array of missing spine keys | JSONB |
| `extra` | Free-form state-specific additions from `build_gap_log(..., extra=...)` | JSONB |
| `synced_at` | Timestamp when this row was written to PostgreSQL | TIMESTAMPTZ |

**Does ingest erase and recreate all rows for a state?** Yes. One row per state.
Re-running gap analysis for a state replaces the existing row.

---

## Entity: `fact_runs`

**Summary** One row per phase-A fact extraction job. Tracks which state, lens,
fact name, model, and prompt version produced a batch of observations. This is
the parent record for all `fact_observations` rows produced in that job. It also
stores run-level header metrics from the phase-A artifact header
(`record_count`, `scored_count`, `entities_processed`, cost/caching counters,
and deterministic `true_count`/`false_count`).

**Created at stage** Phase-A fact extraction — created or resolved when a fact
extraction job starts. The job streams observation rows into `fact_observations`
and updates run metadata from the artifact header as extraction
progresses/completes.

| Field | Description | Data Type |
|---|---|---|
| `fact_run_id` | Surrogate primary key | BIGSERIAL |
| `state` | State code | TEXT |
| `lens` | `source` or `spine` | TEXT |
| `fact_name` | Fact label, e.g. `business_rules_present` or `has_conditional_logic` | TEXT |
| `artifact_name` | Short label for the run, e.g. `AZ_source_business_rules_present` | TEXT |
| `artifact_path` | Path to the emitted artifact file (optional but useful for replay/debug) | TEXT |
| `prompt_version` | Prompt or extraction version used for this run | TEXT |
| `model_id` | Model identifier for LLM-based runs; `deterministic` for det-fact runs (`header.model`) | TEXT |
| `mode` | Extraction mode from header, e.g. `deterministic` or `llm` | TEXT |
| `status` | Header status for the run, e.g. `complete` | TEXT |
| `cost_cap_hit` | Whether the run hit its configured cost cap | BOOLEAN |
| `schema_error` | Header-level schema validation error text, when present | TEXT |
| `scored_at` | Header timestamp for artifact completion | TIMESTAMPTZ |
| `source_hash` | Optional checksum for deduplicating repeated submissions of the same job | TEXT |
| `record_count` | Number of observation rows in the run | INTEGER |
| `scored_count` | Number of rows scored in the run | INTEGER |
| `skipped_count` | Number of rows skipped in the run | INTEGER |
| `entities_processed` | Distinct entities processed in the run | INTEGER |
| `total_tokens_in` | Input tokens consumed by the run (`0` for deterministic) | INTEGER |
| `total_tokens_out` | Output tokens produced by the run (`0` for deterministic) | INTEGER |
| `total_usd` | Total run cost in USD (`0.0` for deterministic) | DOUBLE PRECISION |
| `cache_hit_count` | Number of cache hits during extraction | INTEGER |
| `downgrade_count` | Number of downgraded outputs in the run | INTEGER |
| `true_count` | Number of rows where the fact fired (or nonzero count for int facts) | INTEGER |
| `false_count` | Number of rows where the fact did not fire | INTEGER |
| `started_at` | Timestamp when the extraction job started | TIMESTAMPTZ |
| `finished_at` | Timestamp when the extraction job finished | TIMESTAMPTZ |
| `header` | Full raw header JSON for forward-compatible replay and audit | JSONB |
| `imported_at` | Timestamp when this run record was first created in PostgreSQL | TIMESTAMPTZ |
| `notes` | Free-form operator notes | TEXT |

**Does a new run erase prior fact observations?** For the normal operational
flow, yes — it should replace the prior fact snapshot for the same state+lens
after ingest refreshes the underlying `source_elements` or `spine_elements`
rows. Facts are derived from the latest element snapshot, so rerunning fact
extraction after ingest should recalculate facts against the newer element rows.

---

## Entity: `fact_observations`

**Summary** One row per extracted fact observation — the primary storage for
every binary or enum fact the pipeline produces about a single element. Each row
belongs to a `fact_runs` parent and carries the extracted value, confidence,
justification, source spans, and the raw payload for forward compatibility.

**Created at stage** Phase-A fact extraction — written directly as facts are
produced. Each observation is inserted the moment it is available within its
parent job.

| Field | Description | Data Type |
|---|---|---|
| `fact_observation_id` | Surrogate primary key | BIGSERIAL |
| `fact_run_id` | Foreign key to the parent `fact_runs` row | BIGINT |
| `record_index` | Zero-based position of this observation within the job, combined with `fact_run_id` as a unique key | INTEGER |
| `state` | State code; duplicated for partition-friendly filtering | TEXT |
| `lens` | `source` or `spine`; duplicated for the same reason | TEXT |
| `entity` | Normalized entity name the fact was extracted for | TEXT |
| `element_name` | Element or property the fact is about | TEXT |
| `fact_name` | Extracted fact label, e.g. `has_conditional_logic` | TEXT |
| `fact_value` | Extracted value stored as text to preserve mixed enums and booleans | TEXT |
| `confidence` | Confidence label or score from the extractor | TEXT |
| `justification` | Human-readable rationale from the extractor | TEXT |
| `source_spans` | Array of evidence spans or citations when available | JSONB |
| `raw_payload` | Full original observation payload preserved for forward-compatible replay | JSONB |
| `created_at` | Timestamp when this row was inserted | TIMESTAMPTZ |

**Does a new extraction erase prior observations?** For the normal operational
flow, yes — the prior fact snapshot for the same state+lens should be replaced
when extraction is rerun against refreshed element rows. If you intentionally
keep history, that should be an explicit design choice, for example by retaining
older `fact_run_id` versions separately. The default current-state model should
be: latest ingest snapshot in `source_elements` / `spine_elements`, latest
derived fact snapshot in `fact_observations`.

**Analyst fact corrections never rewrite this table.** Corrections to
LLM-extracted facts (see `curation_records.facts`) are applied as an overlay at
score-aggregate time; `fact_observations` and the prompt cache stay the
immutable record of what the model said. A corrected fact's value in
`score_records` therefore intentionally diverges from this table —
`fact_provenance` marks it `human_corrected`, and that marker is the
reconciliation key. Any integrity check asserting that score facts match
observations must exempt human-corrected facts.

## Entity: `score_records`

**Summary** One row per scored element, written by `poc3 score aggregate`. This
is the central result table of the pipeline. It holds the per-element NACHOS
score, quality diagnostics, confidence, review flags, and the full dimension
breakdown for both the source lens and the spine lens. Rows in this table are
the input for analyst workbooks and reviewer comparisons.

**Created at stage** Phase-C scoring step — `poc3 score aggregate --state
<state> --lens <source|spine>`. The aggregate pipeline writes to populate this
table. Aggregate consumes fact observations plus the state's curation data:
same-lens analyst fact corrections (`curation_records.facts`) are overlaid onto
the loaded fact pool before the rule cascade runs, so rows here derive from
facts and standing corrections together — not from `fact_observations` alone.

| Field | Description | Data Type |
|---|---|---|
| `state` | State code (PK component) | TEXT |
| `lens` | `source` or `spine` (PK component) | TEXT |
| `record_key` | Unique element key in the form `STATE\|Entity\|element_name` (PK component) | TEXT |
| `entity` | Normalized entity name | TEXT |
| `element_name` | Field or property name | TEXT |
| `quality_mean_diagnostic` | Internal arithmetic mean of the quality dimension scores (not surfaced on workbooks) | DOUBLE PRECISION |
| `complexity_score` | Business logic complexity tier: 0–3 | INTEGER |
| `confidence_composite` | Minimum confidence across all dimensions: `high`, `medium`, or `low` | TEXT |
| `adjusted_nachos_score` | Final NACHOS score after extension and multi-entity adjustments, capped at 4.5 | DOUBLE PRECISION |
| `in_scope` | Whether this element is in scope for NACHOS methodology scoring | BOOLEAN |
| `nachos_justification` | Human-readable rule label and adjustment breakdown string | TEXT |
| `discovery_lens` | Provenance: `source` for source-doc rows, `spine_anchored` for gap-recovered rows | TEXT |
| `documentation_source` | Provenance label: `source_doc`, `swagger`, or `swagger_leaf` | TEXT |
| `dimensions` | Per-dimension score objects (value, rule_matched, inputs_used, confidence) | JSONB |
| `fact_provenance` | Per-fact audit trail (value, confidence, downgraded, downgrade_reason, spans). When an analyst fact correction replaced the extracted value at aggregate time, the entry additionally carries `provenance = 'human_corrected'` with confidence forced to `high`, downgrade flags cleared, and spans dropped; the key is emitted only when set, so uncorrected facts are byte-identical to before. Absent `provenance` means model-extracted. | JSONB |
| `review` | Review block (needs_review, reasons, route) | JSONB |

**Does scoring erase and recreate all rows for a state?** Yes. Each aggregate
run deletes all rows where `state = <state> AND lens = <lens>` before inserting
the new snapshot. Re-running `poc3 score aggregate` for a state+lens fully
replaces that lens's scored rows. Source-lens and spine-lens rows are
independent — aggregating one lens does not affect the other lens's rows.
Each run also re-reads the curation data, so standing fact corrections
re-apply to every new snapshot. A correction recorded after the last aggregate
run is `pending re-aggregate` until the next run — a status derivable by
comparing `curation_records.facts` against this table's `fact_provenance`
(states: `applied`, `pending re-aggregate`, `record gone`).

---

## Entity: `curation_records`

**Summary** Human curation persisted per state in committed sidecars, keyed by
the same record key used in `score_records` (`STATE\|Entity\|element_name`).
Three kinds of human input live here, none derivable from source documents or
swagger reprocessing: (1) the workbook analyst-input band captured by
`poc3 review ingest` — score overrides on both axes, reviewed flags, analyst
comments, and commitment-tracker fields; (2) an optional per-record
`adjudication` block — team consensus on the adjusted score, written by
`poc3 review adjudicate`; (3) an optional per-record `facts` block — analyst
corrections to LLM-extracted facts, written by `poc3 review correct-fact` and
applied as a scoring input on the next aggregate run. Every value carries
author and timestamp metadata.

**Created at stage** Review step — three writers: `poc3 review ingest`
(workbook band, from returned review workbooks), `poc3 review adjudicate`
(adjudication block), and `poc3 review correct-fact` (fact corrections). The
latter two are CLI-direct and involve no workbook round-trip.

| Field | Description | Data Type |
|---|---|---|
| `state` | State code. Sidecar file is `data/curation/{state}.json` (lowercase filename); record keys use uppercase code. | TEXT |
| `version` | Sidecar schema version (currently `3`: v2 added the `adjudication` block, v3 the `facts` block). Additive-convergent: older files stay readable — the newer blocks are simply absent — and converge to the current version on their next write; no bulk rewrite. | INTEGER |
| `updated_at` | Most recent write touching the state's sidecar (file-level in today's sidecar; per-record recency is in each value's `ingested_at`, an adjudication's `decided_at`, or a fact correction's `corrected_at`) | TIMESTAMPTZ |
| `record_key` | Element key in the form `STATE\|Entity\|element_name`, same key `score_records` uses. Lens-agnostic: one row per element covers both lenses. | TEXT |
| `entity` | Normalized entity name (derived from key) | TEXT |
| `element_name` | Field or property name (derived from key) | TEXT |
| `reviewed` | Workbook `Reviewed?` (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `analyst_adjusted_override` | Workbook `Analyst Adjusted Score (override)` — the analyst's contested value for the headline adjusted score. Numeric; never replaces the engine score. Disagreement with the engine's adjusted score routes the row to review (one OVERRIDE queue row per contested axis; a fresh adjudication suppresses this axis's row). Stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`. | JSONB |
| `analyst_base_override` | Workbook `Analyst Base Score (override)` — the analyst's contested value for the rule-cascade base tier, so the headline score and the tier can be contested independently. Same numeric handling and per-axis review routing as the adjusted override, gated on its own captured lens. Stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`. | JSONB |
| `required` | Workbook `Required` (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `recommendations` | Workbook `Recommendations` (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `edfi_comments` | Workbook Details-sheet `Ed-Fi Comments` (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `ds_next_steps` | Workbook `DS Next Steps` (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `state_response` | Workbook `State Response` (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `kb_reviewed` | Workbook `KB Reviewed` (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `reviewed_with_state` | Workbook `Reviewed with State` (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `validated_by` | Workbook `Validated By` (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `adoption_timeline` | Commitment tracker adoption window (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `commitment_status` | Commitment tracker negotiation status (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `commitment_comments` | Commitment-tracker free-form notes (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `adjudication` | Team-consensus block for the record's adjusted score, written by `poc3 review adjudicate` — adjusted axis only, because the base tier is pure rule output (consensus that a tier is wrong is a rule problem, not an adjudication). Shaped `{ status, axis, value, lens, agreed_by, decided_at, rationale, engine_score_at_decision, plan_version_at_decision }`. The engine score is never mutated; the two `_at_decision` stamps make staleness computable at read time — if the engine's adjusted score moves past ±0.01, the plan version changes, or the record is no longer scored, the consensus stops rendering and the row re-enters the review queue as RE-ADJUDICATE. A fresh adjudication renders as `Effective Score (adjudicated)` and suppresses the adjusted-axis override disagreement row. | JSONB |
| `facts` | Analyst corrections to LLM-extracted facts, keyed by fact name, written by `poc3 review correct-fact`. Each entry shaped `{ value, lens, author, corrected_at, rationale, prior_value, prior_provenance, plan_version_at_correction }`. LLM facts only — deterministic facts are refused (a wrong deterministic value is a code bug, not curation) — and values are validated against the extraction schema before write. Unlike adjudication stamps, `prior_*` and the plan stamp are provenance, not staleness gates: the newest correction always applies on the next `score aggregate` run, where the fact's sidecar provenance becomes `human_corrected`. `prior_provenance` is `llm`, `llm_downgraded:{reason}`, or `human_corrected`. | JSONB |
| `history` | Per-record change history (single list capped at 50 entries). Band-column entries are shaped `{ column, value, author, replaced_at, source_workbook }`; replaced adjudications and fact corrections append `{ column, value, replaced_at }` where `column` is `adjudication` or `fact:{fact_name}` and `value` preserves the entire prior block (author metadata lives inside the block). | JSONB |

**How are updates handled?** This entity is merge-preserve, not snapshot-replace
and not status-upsert. Review ingest merges per curated column using newest-wins
semantics with bounded history. Blank workbook cells do not clear stored values.
The adjudication and fact-correction writers follow the same newest-wins plus
capped-history pattern (the replaced block moves whole into `history`); the
blank-cell rule applies to the workbook band only, since the CLI writers always
carry explicit values. Re-ingest and re-score flows must leave this curation
data untouched — and re-score flows must re-read it, because fact corrections
are a scoring input.

**Why this entity has a different durability posture** Unlike ingest, fact, and
score outputs, these rows are human-authored and cannot be regenerated by
rerunning pipeline stages. They therefore require backup and retention controls
at least as strict as committed source artifacts. The `facts` block goes
further: it is a scoring input — `poc3 score aggregate` overlays
same-lens corrections onto the fact pool before the rule cascade — so losing
these rows would change published scores on the next aggregate run, not just
drop annotations.

---

## Entity: `score_status`

**Summary** One row per `(state, lens)` pair tracking the latest score aggregate
run. Stores header-level statistics such as mean quality score, NACHOS
histogram, needs-review count, and scoring plan version. Use this table to
verify that a state's scores are current and to surface run-level KPIs without
querying the full `score_records` table.

**Created at stage** Phase-C scoring step — upserted by
`maybe_sync_scores_payload(...)` as part of every successful aggregate run.

| Field | Description | Data Type |
|---|---|---|
| `state` | State code (PK component) | TEXT |
| `lens` | `source` or `spine` (PK component) | TEXT |
| `edfi_version` | Ed-Fi version recorded in the score sidecar | TEXT |
| `scored_at` | Timestamp when the scoring pipeline produced the artifact | TIMESTAMPTZ |
| `record_count` | Total number of records in the sidecar | INTEGER |
| `scored_count` | Number of records that were scored | INTEGER |
| `skipped_count` | Number of records skipped during scoring | INTEGER |
| `mean_quality_score` | Arithmetic mean of per-record quality scores for documented-authored rows | DOUBLE PRECISION |
| `needs_review_count` | Number of rows flagged for reviewer attention | INTEGER |
| `scoring_plan_version` | Scoring plan version string, e.g. `28`. Rule, prompt, methodology, and sidecar-shape changes bump it (v28 added the `human_corrected` provenance capability); individual fact corrections never do — they are per-row data annotations. | TEXT |
| `model` | Model identifier used for LLM fact extraction, e.g. `claude-sonnet-4-6` | TEXT |
| `prompt_version` | Prompt version string, e.g. `phase-a.v1` | TEXT |
| `in_scope_count` | Number of in-scope rows used for NACHOS histogram and mean | INTEGER |
| `documentation_gap_count` | Count of rows where the documentation gap dimension fired | INTEGER |
| `header` | Full sidecar header JSON excluding the `scores` array. This includes `dimension_stats`, `nachos_score_histogram`, `adjusted_nachos_score_histogram`, `mean_nachos_score`, and `mean_adjusted_nachos_score`. | JSONB |
| `synced_at` | Timestamp when PostgreSQL sync ran | TIMESTAMPTZ |

**Does scoring erase and recreate all rows for a state?** No. This table is
upserted. The existing `(state, lens)` row is updated in place on every
aggregate run. It always reflects the latest run's statistics.

**Freshness lineage note** poc3 publish now writes a freshness
manifest (data/out/publish_manifest.json) with artifact-keyed sha256 lineage —
producer, input hashes, and versions for every artifact in the chain. It answers
the same "is this data current?" question as both status tables(`ingestion_status`, `score_status`) but with more
detail, and the WS-3 design doc says the run-ledger schema should start from it.
Worth referencing here so the DB design doesn't grow a parallel freshness
mechanism that drifts from the one the pipeline already maintains.

---
