# Database Entity Details — DRAFT update

> **DRAFT — not yet the contract.** This revision of the merged baseline
> (curation schema v3) folds in the Monday technical-session decisions and the Ingestion & Scoring
> PRD (PR #3 as amended by PR #8). It is staged for review against **MC-17**; once
> the open decision notes below are resolved in the room or in Jira, this replaces
> the current doc via the agreed change flow (doc update + contract-test update
> land together).
>
> **Sources:** current entity doc (merged) · `docs/PRD-Ingestion-Scoring.md`
> (PR #3 + PR #8 review edits) · Monday session deck (POC-3 → Metadata Catalog) ·
> kick-off outcomes · verification against POC-3 `dev`. New entities and new
> fields are marked *(new)*. Decisions are asserted in place with their
> grounds; the one correction they imply outside this doc — the PRD's
> adjudication field list contradicting its own routing section — is tracked
> on the PRD follow-ups list.
>
> A machine-readable companion, `schemas/pipeline-records.schema.json`, mirrors every
> entity here as a JSON Schema. It is the artifact the CI contract test validates
> pipeline-emitted records against, so drift between the pipeline and the DDL
> fails a build instead of surfacing at integration.

This document lists the entities and fields the pipeline hands the storage
database. It is the **logical contract** between the pipeline (producer) and the
database (consumer): fields, types, grain, and per-entity update semantics.
Physical design — table decomposition, keys, indexes, constraints, history-table
internals, transactionality — is the database side's to decide and is not
specified here.

Terminology note: in this document, `spine` means the Ed-Fi Swagger / API model
catalog used as the API-model coverage lens.

---

## Conventions and cross-cutting rules

### The grain

`record_key = STATE|Entity|element_name` is the join key across scored and
curated data. `lens` (`source` or `spine`) is **part of the grain** on every
scored table: the two lenses are parallel result sets and are never averaged.
The unit of scoring is `(state, lens, record_key)`.

### Update disciplines

Five disciplines, keyed to whether a re-run can regenerate the data:

| Discipline | Meaning | Applies to |
|---|---|---|
| **retain-by-run** *(new — generalizes snapshot-replace)* | Each run writes a new snapshot keyed by its run id; prior snapshots are retained up to a retention depth; "current" is a named view over the latest completed run. Retention depth 1 degenerates to the old snapshot-replace behavior. | `source_elements`, `spine_elements`, `gap_logs` |
| **append** | Rows are only ever added, never updated or deleted | `ingestion_runs`, `fact_runs`, `scoring_runs`, `score_labels` |
| **status-upsert** | One control row per key, updated in place; always reflects the latest run | `ingestion_status`, `score_status` |
| **snapshot-replace** | Delete-for-scope, re-insert; the table is a current projection | `score_records`, `fact_observations` (replace-with-run) |
| **merge-preserve** | Human-authored; merged newest-wins with bounded history; never wiped by re-ingest or re-score, and re-read by every re-score | `curation_records` |

**The one rule that can't break:** `curation_records` is merge-preserve.
Everything else is regenerable by a re-run; human input is not.

### Run identity and the three version numbers

Three version numbers travel on run records and move **independently** — one
shared number that bumps for everything conflates unrelated changes:

| Version | Field | What bumps it |
|---|---|---|
| Envelope shape | `contract_version` | A change to the emitted record structure — the envelope (this contract). Already implemented as `SIDECAR_CONTRACT_VERSION` (currently `2`, ADR 0017); envelope evolution bumps this knob without invalidating adjudications. Transform migrations key off it. |
| Scoring methodology | `scoring_plan_version` | Rules, prompts' meaning, adjustment magnitudes — methodology semantics only (ADR 0017 moved sidecar-shape changes to `contract_version`). Reporting, score labels, and curation staleness key off it. |
| State source documentation | `snapshot_id` + `snapshot_digest` | Content identity of the elements artifact a run consumed. Already implemented (ADR 0018): `snapshot_id` is `{filename stem}@{digest[:16]}`; `snapshot_digest` is sha256 over a canonical form of the artifact (volatile run timestamps stripped), so same content means same digest across re-ingests and formatting differences. This is the PRD's `state_requirements_tag` — the doc uses the pipeline's names. |

A fourth identity travels with them: **`release_id`** — a deterministic hash
over the full run identity, designed as the import adapter's **idempotency
key**. The landing zone dedupes on it; curation decision stamps record it.

Every score row's run traces to all of these: which envelope shape it was
emitted under, which methodology produced it, and which edition of the state's
documentation it scored — provably, via the digest.

### Assessor identity

A scoring run is one assessor's sweep over a whole snapshot. The run record
carries `assessor_type` and `assessor_id` — already emitted (ADR 0018) as
`engine` / `poc3-nachos`. `assessor_type` is deliberately an **open string**,
not a closed enum: a second reader, a human pass, or a synthesis is an ordinary
future value (the ADR names `second-model`, `human`, `synthesis`), not a schema
change. Score rows inherit both through their run. This buys
every plausible scoring evolution — second reader, multi-rater synthesis, human
workbook scores as data — as appended runs, no migration.

The boundary drawn deliberately: **population-scale raters write score rows;
subject-scale verdicts stay in `curation_records`.** An adjudication or override
reacts to one row of one run, so adjudication is *not* an assessor type, and
"effective" values are computed where the two layers join — never written back
into score rows.

### Data-completeness flags (legacy backfill)

Backfilled rows from the 8 manually-scored states will never have fact
observations, citations, `scoring_plan_version` lineage, or run history.
"Absent because legacy" must be distinguishable from "absent because something
broke", so score rows carry `data_completeness` *(new)*:

| Value | Meaning |
|---|---|
| `pipeline_full` | Full pipeline provenance: facts, citations, plan version, run lineage |
| `pipeline_machine_only` | Full pipeline provenance, but no manual baseline exists (the 4 never-manually-scored states) |
| `legacy_import` | Backfilled manual scores; no fact provenance or scoring-logic history |

Loader validation: a `legacy_import` row cannot claim full provenance (no
`fact_provenance`, no `scoring_plan_version`). Dashboards and reports render
legacy rows distinctly and never fabricate evidence where none exists. A legacy
backfill lands as its own `scoring_runs` row with `assessor_type = 'human'`
(ADR 0018's naming), so legacy scores sit in the same history mechanism as
everything else.

### Publication is not a side effect of scoring

Reports read **published** results, not the latest run: a completed run changes
nothing on a dashboard until someone promotes it, and rollback is moving the
pointer back. The principle binds now; the pointer mechanism is storage design
(MC-6), additive whenever a second methodology or candidate run makes it
necessary.

### The three contract seams

Future changes should stay contained to one seam:

1. **The element record** — ingestion → everything downstream
   (`source_elements` / `spine_elements` and their run ledger).
2. **The score record** — the typed envelope reporting reads
   (promoted columns) plus the evidence payload the scorer owns (JSONB).
3. **The read views** — store → dashboard/PDF (`current_scores`,
   `state_summary`, …). Consumers bind to view names, never to physical tables.

### Delivery model: JSONB landing, in-database transform

Agreed at the schema working session: ingestion and scoring **deliver their
outputs as JSONB payloads** — the pipeline's artifact records, landed with
their keys and run identity — and the database side **transforms downstream**:
extracting typed columns, populating the physical tables, and feeding the named
views. This is the lowest-effort shape for the pipeline port (no per-table
typed writes to maintain) and keeps every physical choice on the database side
of the boundary. What the contract records about it:

- **The emit contract is the payload shape**, pinned by
  `schemas/pipeline-records.schema.json`. A JSONB landing zone accepts
  anything, so drift would otherwise surface as a broken transform or a
  silently-NULL dashboard column. The CI schema check is the primary
  drift gate.
- **Run identity is stamped at emit, or never.** `snapshot_id` /
  `snapshot_digest`, `release_id`, assessor identity — the transform cannot
  reconstruct these later; they arrive on every landed payload, and the
  landing zone dedupes on `release_id`.
- **The per-entity update disciplines bind the transform layer.** Whoever
  writes the physical tables applies them — merge-preserve for
  `curation_records` above all: a transform that treats curation payloads like
  the regenerable ones destroys human input on the next re-run.
- **Column type is `jsonb`, not `json`.** The transforms query these payloads;
  `jsonb` gives indexing and the extraction operators, `json` gives only byte
  fidelity. The digest rule below covers the one case where byte fidelity
  matters; if replay fidelity beyond that is wanted, land the verbatim artifact
  text beside the `jsonb` rather than switching the column type.
- **Digests are computed at emit, over artifact bytes, and treated as opaque
  values downstream.** Never recompute a digest from a stored `jsonb` payload —
  key order and formatting change on the way in, so it will not match. The
  artifact files and `publish_manifest.json` remain the byte-exact record.
- **"Promoted columns" reframed.** Where this document lists promoted filter
  columns on `score_records`, under this model they are the fields the DB
  transform must **extract and materialize** as real, indexable columns for the
  views; the emit supplies them inside the score payload.

### Freshness lineage

`poc3 publish` writes `publish_manifest.json` with artifact-keyed sha256
lineage — producer, input hashes, and versions for every artifact in the chain.
It answers the same "is this data current?" question as the status tables but
with more detail. The database should not grow a parallel freshness mechanism
that drifts from it; the run ledgers below (`ingestion_runs`, `scoring_runs`)
carry the same digests so the two stay reconcilable.

---

## Entity: `ingestion_runs` *(new)*

**Summary** One row per ingestion run for a state — the append-only ledger that
gives every ingestion snapshot an identity. This implements the PRD
requirement: prior ingestion snapshots are retained rather than replaced,
each keyed by its run id, so scores and reports can be associated with the exact
requirements they were computed against. The `snapshot_id` names the edition;
the `snapshot_digest` proves the content (both per ADR 0018's identity model,
computed at ingest here rather than read back at scoring time).

**Created at stage** Ingest step — one row created when the run starts, updated
to `completed` when both lenses are synced.

| Field | Description | Data Type |
|---|---|---|
| `ingestion_run_id` | Surrogate primary key | BIGSERIAL |
| `state` | State code, e.g. `AZ`, `WI`, `MN`, `TX`, `IN` | TEXT |
| `snapshot_id` | Snapshot identifier, `{artifact stem}@{digest[:16]}` — the id score and fact runs pin to. The PRD's `state_requirements_tag`. | TEXT |
| `snapshot_digest` | sha256 over the canonical form of the run's output artifact (volatile timestamps stripped) — proves which content the id names | TEXT |
| `edfi_version` | Ed-Fi version string from the artifact header | TEXT |
| `source_inputs` | Input list for the snapshot: source document filenames plus the swagger URLs used to build the spine manifest | JSONB |
| `source_element_count` | Source-lens element rows produced | INTEGER |
| `spine_element_count` | Spine-lens element rows produced | INTEGER |
| `status` | Run status, e.g. `running`, `completed`, `failed` | TEXT |
| `started_at` | Timestamp when the ingestion run started | TIMESTAMPTZ |
| `finished_at` | Timestamp when the ingestion run finished | TIMESTAMPTZ |
| `notes` | Free-form operator notes | TEXT |
| `imported_at` | Timestamp when this row was written to PostgreSQL | TIMESTAMPTZ |

**How are updates handled?** Append. A run row is never rewritten by a later
run. Retention depth for the element snapshots a run keys (how many prior runs
stay queryable) is **parameterized** — the actual requirement is being gathered
from the solutions team; the mechanism does not depend on the answer.

> **Ingestion retention — retained at the landing zone.** Prior element
> snapshots are retained in storage, keyed by `ingestion_run_id`: landing
> appends a new snapshot per run and never destroys a prior one, so history is
> queryable where scores can join it. This is what MC-6's own acceptance
> criteria already require — a documentation version is retrievable as
> content, not just a hash, and an update creates a new version, never an
> overwrite — so the retained artifact files are lineage evidence, not the
> system of record. How storage organizes the retained snapshots (tables,
> partitions, pruning) and the retention depth are MC-6's; depth stays
> parameterized pending the solutions-team requirement. The scoring pipeline
> is unaffected either way: it reads its own artifacts and stamps run
> identity at emit.

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
| `ingestion_run_id` | *(new)* Foreign key to the `ingestion_runs` row that produced this snapshot | BIGINT |
| `snapshot_id` | *(new)* Snapshot identifier, denormalized from the run record — every stored element names the requirements edition it reflects (the PRD's `state_requirements_tag`) | TEXT |
| `state` | State code, e.g. `AZ`, `WI`, `MN`, `TX`, `IN` | TEXT |
| `edfi_version` | Ed-Fi version string from the artifact header | TEXT |
| `domain` | Source Area: the state's own grouping vocabulary (XLSX sheet, Confluence page, MN collection, IN API resource, TX TEDS entity). Empty for swagger-backfill rows and spine-lens rows the source doc never mentioned. Not the Ed-Fi domain; see edfi_domain. | TEXT |
| `edfi_domain` | Cross-state Ed-Fi domain, spine-derived from the entity; multi-domain entities joined with "; ". NULL when the entity resolves to no spine domain. | TEXT |
| `entity` | Normalized entity name, e.g. `Section` — the best-matched Ed-Fi Data Standard entity (the PRD's `standard_entity`) | TEXT |
| `raw_entity` | Original entity string from the source document, e.g. `edfi.Section` or `az.SectionExtension` | TEXT |
| `match_confidence` | *(new)* Entity-to-standard-domain match confidence: `high`, `medium`, `low`, or `unmatched`. Unmatched entities are flagged for staff review, never silently dropped; staff may resolve by manual mapping or marking as a state-specific extension. | TEXT |
| `match_notes` | *(new)* Flags for extension entities, renamed entities, or descriptor overloads discovered during domain matching | TEXT |
| `element_name` | Field or property name | TEXT |
| `element_path` | *(new)* Full field path within the entity | TEXT |
| `data_type` | Canonical or source-verbatim data type | TEXT |
| `cardinality` | *(new)* `keys`, `required`, `optional`, or `optional_conditional` | TEXT |
| `definition_text` | State-authored definition text, or text backfilled from the Ed-Fi swagger | TEXT |
| `source` | Element origin: `core`, `extension`, `unknown`, or `filtered` | TEXT |
| `extension_name` | Extension identifier when `source = 'extension'` | TEXT |
| `in_scope` | *(new)* Scope classification: true when a vendor must supply (write or send) the element to meet the state's requirements; descriptors are in scope. False when the state populates it and vendors only read it, or vendors don't touch it. | BOOLEAN |
| `scope_confidence` | *(new)* Scope-classification confidence: `high`, `medium`, or `low` | TEXT |
| `scope_rationale` | *(new)* Brief rationale, required for every out-of-scope classification | TEXT |
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

**Does ingest erase and recreate all rows for a state?** Under retain-by-run,
each run inserts a fresh snapshot keyed by `ingestion_run_id`; rows from prior
runs are retained up to the retention depth, then pruned. "Current" reads go
through the `current_source_elements` view (latest completed run per state).

> Scope classification (`in_scope` / `scope_confidence` / `scope_rationale`)
> lives here because it is computed from the element record, before scoring;
> `score_records.in_scope` is the value the scoring run consumed, carried on the
> score row so a snapshot is self-describing. Staff corrections to scope
> classifications follow the curation path, not edits to this table.

---

## Entity: `spine_elements`

**Summary** One row per element record produced by walking the Ed-Fi swagger
spine catalog directly, enriched with any documentation the state's source has
for that element. This is the spine-lens counterpart of `source_elements`. Rows
in this table enumerate the full spine catalog for a state and mark elements as
`documented` or `documented = false` depending on whether the state's source doc
covers them. Spine elements also feed fact extraction.

**Created at stage** Ingest step — same as `source_elements`.

Fields are identical to `source_elements` (including the *(new)* columns:
`ingestion_run_id`, `snapshot_id`, `match_confidence`, `match_notes`,
`element_path`, `cardinality`, `in_scope`, `scope_confidence`,
`scope_rationale`), with these differences in meaning:

| Field | Spine-lens meaning | Data Type |
|---|---|---|
| `raw_entity` | Original entity string from the spine or source | TEXT |
| `definition_text` | Definition text from state source when available, otherwise from the swagger | TEXT |
| `edfi_standard_definition` | Ed-Fi standard definition from the swagger | TEXT |

**Does ingest erase and recreate all rows for a state?** Same retain-by-run
behavior as `source_elements`; "current" reads go through the
`current_spine_elements` view.

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
| `ingestion_run_id` | *(new)* The latest completed run this row reflects | BIGINT |
| `snapshot_id` | *(new)* Snapshot identifier of that run | TEXT |
| `edfi_version` | Ed-Fi version from the artifact | TEXT |
| `extracted_at` | Timestamp when the ingest adapter produced the artifact | TIMESTAMPTZ |
| `element_count` | Number of element rows in the artifact | INTEGER |
| `source_inputs` | Common input list for the snapshot. Source lens stores unique source document filenames; spine lens stores the swagger URLs used to build the spine manifest. | JSONB |
| `status` | Sync status; current code always writes `completed` | TEXT |
| `synced_at` | Timestamp when the PostgreSQL sync ran | TIMESTAMPTZ |

**Does ingest erase and recreate all rows for a state?** No. This table is
upserted — the existing row for `(state, lens)` is updated in place on every
sync. It always reflects the latest sync run.

> With `ingestion_runs` carrying the ledger, this entity is a convenience
> projection of "latest completed run per (state, lens)". Whether it stays a
> physical table or becomes a view over `ingestion_runs` is a physical-design
> call; the contract only requires that the fields above be readable at this
> grain.

---

## Entity: `gap_logs`

**Summary** One row per state per ingestion run summarizing the gap analysis
between the state's source document and the Ed-Fi spine catalog. The gap log
captures how well the source document covers the spine, which spine elements
are unmatched, and what the unflatten recovery produced. This is a diagnostic
artifact; it is computed and stored after the ingest step.

**Created at stage** Ingest step — `poc3 ingest <state>` (or `poc3 ingest gap
<state>` for a gap-only refresh).

| Field | Description | Data Type |
|---|---|---|
| `state` | State code (PK component) | TEXT |
| `ingestion_run_id` | *(new)* Run this gap analysis belongs to (PK component) | BIGINT |
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
| `alias_tier_histogram` | Optional: spine-lens documented matches per alias tier (`primary` vs the grammar's fuzzy tiers) — audits how much match rate rides on the fuzziest tiers | JSONB |
| `extra` | Free-form state-specific additions from `build_gap_log(..., extra=...)` | JSONB |
| `synced_at` | Timestamp when this row was written to PostgreSQL | TIMESTAMPTZ |

**Does ingest erase and recreate all rows for a state?** Retain-by-run, same as
the element tables: one row per state per run, retained with its run's snapshot.
A gap-only refresh replaces the row for its run.

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
| `ingestion_run_id` | *(new)* The ingestion snapshot this extraction ran against | BIGINT |
| `snapshot_id` | *(new)* Snapshot identifier of that ingestion run | TEXT |
| `contract_version` | *(new)* Envelope-shape version of the emitted records (see the version numbers) | TEXT |
| `fact_name` | Fact label, e.g. `business_rules_present`, `has_conditional_logic`, or the v30 extension-necessity pair `state_requirement_basis` / `core_can_express_requirement` (which replaced the retired `extension_is_necessary`). Emitted as `fact` in the artifact header. | TEXT |
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
flow, yes — it replaces the prior fact snapshot for the same state+lens after
ingest refreshes the underlying element rows. Facts are derived from the latest
element snapshot, so rerunning fact extraction after ingest recalculates facts
against the newer element rows. The run rows themselves are append-only — the
history of everything the model said is retained here and in the retained
observation runs.

---

## Entity: `fact_observations`

**Summary** One row per extracted fact observation — the primary storage for
every binary or enum fact the pipeline produces about a single element. Each row
belongs to a `fact_runs` parent (which carries the fact name, model, and prompt
version) and mirrors the artifact row the pipeline actually emits: the model's
raw value and the post-validation value are stored separately, with downgrade
provenance, so the deterministic evidence gate's decisions stay auditable.

**Created at stage** Phase-A fact extraction — rows stream into the JSONL
artifact as facts are produced; the landing zone receives them with their
parent run.

| Field | Description | Data Type |
|---|---|---|
| `fact_observation_id` | Surrogate primary key (landing-side) | BIGSERIAL |
| `fact_run_id` | Foreign key to the parent `fact_runs` row (landing-side; the artifact ties rows to their header) | BIGINT |
| `record_key` | Element key `STATE\|Entity\|element_name` — with `fact_run_id`, the unique key | TEXT |
| `entity` | Normalized entity name the fact was extracted for | TEXT |
| `element_name` | Element or property the fact is about | TEXT |
| `llm_value` | The value the model proposed, before validation | TEXT |
| `validated_value` | The value after the deterministic post-extraction gate — NULL when the gate vetoed an unsupported or low-confidence affirmative. Scoring consumes this, not `llm_value`. | TEXT |
| `spans` | Validated evidence spans (verbatim source excerpts) | JSONB |
| `confidence` | Confidence label from the extractor | TEXT |
| `downgrade_reason` | Why the gate downgraded, when it did (e.g. `hallucinated_span`, `spans_on_false_claim`) | TEXT |
| `any_invalid_spans` | Whether any proposed span failed validation | BOOLEAN |
| `model` | Row-level model override; normally NULL (model lives on the run header) | TEXT |
| `prompt_version` | Prompt version stamped on the row | TEXT |
| `created_at` | Timestamp when this row was landed (landing-side) | TIMESTAMPTZ |

**Does a new extraction erase prior observations?** For the normal operational
flow, yes — the prior fact snapshot for the same state+lens is replaced when
extraction is rerun against refreshed element rows. The default current-state
model is: latest ingest snapshot in the element tables, latest derived fact
snapshot here, with the append-only `fact_runs` ledger preserving run history.

**Analyst fact corrections never rewrite this table.** Corrections to
LLM-extracted facts (see `curation_records.facts`) are applied as an overlay at
score-aggregate time; `fact_observations` and the prompt cache stay the
immutable record of what the model said. A corrected fact's value in
`score_records` therefore intentionally diverges from this table —
`fact_provenance` marks it `human_corrected`, and that marker is the
reconciliation key. Any integrity check asserting that score facts match
observations must exempt human-corrected facts.

---

## Entity: `scoring_runs` *(new)*

**Summary** One row per score-aggregate run — the append-only run ledger for
scoring, mirroring the shape the fact layer already uses (`fact_runs` parent →
observation rows). This closes the audit requirement's remaining gap:
`scoring_plan_version` already gave history of scoring *logic*; this ledger
plus retained landed payloads give history of score *results* (how storage
keeps per-row history is MC-6's mechanism decision — see below). Each run pins the exact
inputs it scored (`snapshot_id` + `snapshot_digest`), the methodology that
scored them (`scoring_plan_version`), the envelope shape it emitted
(`contract_version`), its idempotency key (`release_id`), and who or what did
the scoring (`assessor_type` / `assessor_id`) — all six already emitted in the
sidecar header today (sidecar contract v2, ADR 0017/0018); this table is their
landing.

**Created at stage** Phase-C scoring step — one row per `poc3 score aggregate
--state <state> --lens <source|spine>` invocation (and one per legacy-backfill
load, with `assessor_type = 'human'`).

| Field | Description | Data Type |
|---|---|---|
| `scoring_run_id` | Surrogate primary key | BIGSERIAL |
| `state` | State code | TEXT |
| `lens` | `source` or `spine` | TEXT |
| `scored_at` | Timestamp when the scoring pipeline produced the result set | TIMESTAMPTZ |
| `status` | Run status, e.g. `complete` | TEXT |
| `scoring_plan_version` | Methodology version that produced this run's scores | TEXT |
| `contract_version` | Envelope-shape version of the emitted records (`SIDECAR_CONTRACT_VERSION`, currently `2`) | TEXT |
| `snapshot_id` | Ingestion snapshot this run scored — `{artifact stem}@{digest[:16]}`, already emitted in the sidecar header (ADR 0018). The PRD's `state_requirements_tag`. | TEXT |
| `snapshot_digest` | sha256 canonical-form digest of that snapshot — proves the content, not just the edition. Already emitted. | TEXT |
| `release_id` | Deterministic hash over the full run identity — the landing zone's idempotency key. Already emitted. | TEXT |
| `ingestion_run_id` | Foreign key to the ingestion run behind the tag | BIGINT |
| `assessor_type` | Open string (ADR 0018): `engine` today; `second-model`, `human`, `synthesis` are ordinary future values — a population-scale sweep over the snapshot | TEXT |
| `assessor_id` | Identifier of the assessor: engine+plan version, workbook batch id, model id, etc. | TEXT |
| `model` | Model identifier used for LLM fact extraction feeding this run | TEXT |
| `prompt_version` | Prompt version string feeding this run | TEXT |
| `data_completeness` | Default completeness flag for the run's rows: `pipeline_full`, `pipeline_machine_only`, or `legacy_import` | TEXT |
| `record_count` | Total records in the run | INTEGER |
| `scored_count` | Records scored | INTEGER |
| `skipped_count` | Records skipped | INTEGER |
| `in_scope_count` | In-scope rows used for NACHOS histogram and mean | INTEGER |
| `needs_review_count` | Rows flagged for reviewer attention | INTEGER |
| `header` | Full run header JSON (dimension stats, histograms, means) — per-run stats retained for history | JSONB |
| `notes` | Free-form operator notes | TEXT |
| `imported_at` | Timestamp when this row was written to PostgreSQL | TIMESTAMPTZ |

**How are updates handled?** Append. Runs are never updated or deleted within
the retention window. Retention depth (how far back run snapshots stay
queryable — previous RFP cycles at minimum) is **parameterized**; the actual
requirement is being gathered from the solutions team.

> **History mechanism — MC-6's decision.** The contract binds only the
> handoff guarantees: every landed record carries run identity, landing never
> destroys prior data (the zone dedupes on `release_id`, it does not replace),
> and history stays queryable through the `score_history` view. How storage
> keeps per-row history — append-only main table, separate history tables,
> partitions — is storage design, owned by MC-6 and going to the scheduled
> follow-up session. Whatever ships first must write run identity from run
> one; history cannot be captured retroactively.

---

## Entity: `score_records`

**Summary** One row per scored element — the **landed score payload**, written
by `poc3 score aggregate`. This is the central result record of the pipeline.
It holds the per-element NACHOS score, quality diagnostics, confidence, review
flags, and the full dimension breakdown for both the source lens and the spine
lens. Rows here are the input for analyst workbooks, reviewer comparisons, and
— through the transform and the named views — the dashboard read model. The
typed "current" table the transform maintains from these payloads is MC-6's
design; this section specifies what lands.

**Created at stage** Phase-C scoring step — `poc3 score aggregate --state
<state> --lens <source|spine>`. Aggregate consumes fact observations plus the
state's curation data: same-lens analyst fact corrections
(`curation_records.facts`) are overlaid onto the loaded fact pool before the
rule cascade runs, so rows here derive from facts and standing corrections
together — not from `fact_observations` alone.

| Field | Description | Data Type |
|---|---|---|
| `state` | State code (PK component) | TEXT |
| `lens` | `source` or `spine` (PK component) | TEXT |
| `record_key` | Unique element key in the form `STATE\|Entity\|element_name` (PK component) | TEXT |
| `scoring_run_id` | *(new)* The run that produced this current row — joins to `scoring_runs` for scored_at, plan version, snapshot tag, assessor | BIGINT |
| `scored_at` | *(new)* Timestamp of that run, denormalized for direct reads | TIMESTAMPTZ |
| `entity` | Normalized entity name | TEXT |
| `element_name` | Field or property name | TEXT |
| `quality_mean_diagnostic` | Internal arithmetic mean of the quality dimension scores (not surfaced on workbooks) | DOUBLE PRECISION |
| `complexity_score` | Business logic complexity tier: 0–3 (the NACHOS score) | INTEGER |
| `tier_name` | *(proposed payload addition — nachos-ai-poc-3#318; no such field exists in POC-3 today)* Name of the rule-cascade tier that matched — display labels and thresholds for it live in `score_labels`, keyed by `scoring_plan_version`. Until emitted, derivable from `complexity_score` + `score_labels`. | TEXT |
| `confidence_composite` | Minimum confidence across all dimensions: `high`, `medium`, or `low` | TEXT |
| `adjusted_nachos_score` | Final NACHOS score after extension and multi-entity adjustments, capped at 4.5 | DOUBLE PRECISION |
| `in_scope` | Whether this element is in scope for NACHOS methodology scoring (the value the run consumed; classification originates on the element record) | BOOLEAN |
| `documentation_style` | *(new — payload field; typed-column materialization is MC-6's call)* `prescriptive`, `conceptual`, `cross_reference`, `regulatory`, or `unspecified` — the extracted fact behind the `documentation_style_tier` dimension. (The PRD calls this business_logic_complexity and its first value "Conditional"; the pipeline's fact name and tokens are used here.) | TEXT |
| `adjustment_drivers` | *(proposed payload addition — nachos-ai-poc-3#318)* Array of the pipeline's canonical adjustment tokens: `unnecessary_ext`, `necessary_ext`, `multi_entity`, `fidelity_divergent_explained`, `fidelity_divergent_unclear`. Today these are byte-pinned substrings of `nachos_justification`; promoting them means emitting a structured array beside it. | JSONB |
| `extension_necessity` | *(proposed payload addition — nachos-ai-poc-3#318; adjudicable)* Resolved extension-necessity determination: `necessary`, `unnecessary`, or `unresolved`; NULL for core elements. Since scoring plan v30 (POC-3 PR #284) this is **not an extracted fact** — it resolves from two independent enum facts via a pure truth table (`rules.py resolve_extension_necessity_values`): `state_requirement_basis` ∈ { `law_or_regulation`, `state_reporting_mandate`, `state_program_requirement` (the positive bases), `none`, `unresolved` } and `core_can_express_requirement` ∈ { `yes`, `no`, `unresolved` }. A positive state basis OR core fit `no` ⇒ `necessary`; basis `none` AND core fit `yes` ⇒ `unnecessary`; every other combination ⇒ `unresolved` (review-routed, never guessed). The truth-table path taken is recorded in the `dimensions` payload as `extension_necessity_resolution` ∈ { `state_requirement_and_core_gap`, `state_requirement`, `core_gap`, `core_supported_without_state_requirement`, `unresolved` }. The retired `extension_is_necessary` fact must not reappear in any DDL or emit. | TEXT |
| `documentation_gap` | *(new — payload field; typed-column materialization is MC-6's call)* Whether the documentation-gap dimension fired for this row | BOOLEAN |
| `data_completeness` | *(new)* `pipeline_full`, `pipeline_machine_only`, or `legacy_import` — see the data-completeness flags section | TEXT |
| `nachos_justification` | Human-readable rule label and adjustment breakdown string | TEXT |
| `discovery_lens` | Provenance: `source` for source-doc rows, `spine_anchored` for gap-recovered rows | TEXT |
| `documentation_source` | Provenance label: `source_doc`, `swagger`, or `swagger_leaf` | TEXT |
| `dimensions` | Per-dimension score objects (value, rule_matched, inputs_used, confidence). The full firing-rule path for a row is the per-dimension `rule_matched` entries plus `nachos_justification`; it stays inspectable without re-running a model. | JSONB |
| `fact_provenance` | Per-fact audit trail (value, confidence, downgraded, downgrade_reason, spans). When an analyst fact correction replaced the extracted value at aggregate time, the entry additionally carries `provenance = 'human_corrected'` with confidence forced to `high`, downgrade flags cleared, and spans dropped; the key is emitted only when set, so uncorrected facts are byte-identical to before. Absent `provenance` means model-extracted. NULL on `legacy_import` rows. | JSONB |
| `review` | Review block (needs_review, reasons, route) | JSONB |

**Does scoring erase and recreate prior results?** At the handoff, no: each
aggregate run lands a complete new result set under its own `release_id`;
prior landed runs are retained per the history guarantees. How the transform
maintains the typed "current" table from successive runs (replace, filter to
latest, or otherwise) is MC-6's call. Source-lens and spine-lens results are
independent — aggregating one lens does not affect the other. Each run also
re-reads the curation data, so standing fact corrections re-apply to every new
run. A correction recorded after the last aggregate run is `pending
re-aggregate` until the next run — a status derivable by comparing
`curation_records.facts` against the current run's `fact_provenance` (states:
`applied`, `pending re-aggregate`, `record gone`).

> **Column materialization — MC-6's decision.** Which of these payload fields
> the transform extracts into typed, indexed columns for the reporting views —
> including the `edfi_domain` / core-vs-extension question (denormalize in the
> transform vs. join in the views) — is decided on MC-6 against the locked
> dashboard requirements. The contract's obligation is upstream of that:
> the fields above are guaranteed present in the landed payload (the JSON
> Schema enforces it), and filter fields never live *inside* an evidence blob
> (`dimensions`, `fact_provenance`, `review` stay JSONB payloads). Fields
> marked as proposed payload additions are the emit work tracked in
> nachos-ai-poc-3#318.

---

## Entity: `curation_records`

**Summary** Human curation persisted per state, keyed by the same record key
used in `score_records` (`STATE|Entity|element_name`). Three kinds of human
input live here, none derivable from source documents or swagger reprocessing:
(1) the workbook analyst-input band captured by `poc3 review ingest` — score
overrides on both axes, reviewed flags, analyst comments, and
commitment-tracker fields; (2) an optional per-record `adjudication` block —
team consensus, written by `poc3 review adjudicate`; (3) an optional per-record
`facts` block — analyst corrections to LLM-extracted facts, written by
`poc3 review correct-fact` and applied as a scoring input on the next aggregate
run. Every value carries author and timestamp metadata.

**Created at stage** Review step — three writers: `poc3 review ingest`
(workbook band, from returned review workbooks), `poc3 review adjudicate`
(adjudication block), and `poc3 review correct-fact` (fact corrections). The
latter two are CLI-direct and involve no workbook round-trip.

| Field | Description | Data Type |
|---|---|---|
| `state` | State code. Sidecar file is `data/curation/{state}.json` (lowercase filename); record keys use uppercase code. | TEXT |
| `version` | Sidecar schema version (currently `5`: v2 added the `adjudication` block, v3 the `facts` block, v4 the `confirmation` block, v5 `release_id_at_decision` on every decision stamp). Additive-convergent: older files stay readable — the newer blocks are simply absent — and converge to the current version on their next write; no bulk rewrite. | INTEGER |
| `updated_at` | Most recent write touching the state's sidecar (file-level in today's sidecar; per-record recency is in each value's `ingested_at`, an adjudication's `decided_at`, or a fact correction's `corrected_at`) | TIMESTAMPTZ |
| `record_key` | Element key in the form `STATE\|Entity\|element_name`, same key `score_records` uses. Lens-agnostic: one row per element covers both lenses. | TEXT |
| `entity` | Normalized entity name (derived from key) | TEXT |
| `element_name` | Field or property name (derived from key) | TEXT |
| `reviewed` | Workbook `Reviewed?` (stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens }`) | JSONB |
| `analyst_adjusted_override` | Workbook `Analyst Adjusted Score (override)` — the analyst's contested value for the headline adjusted score. Numeric; never replaces the engine score. Disagreement with the engine's adjusted score routes the row to review (one OVERRIDE queue row per contested axis; a fresh adjudication suppresses this axis's row). Stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens, release_id_at_decision }` — the release stamp is curation v5, on the two override-axis captures only. | JSONB |
| `analyst_base_override` | Workbook `Analyst Base Score (override)` — the analyst's contested value for the rule-cascade base tier, so the headline score and the tier can be contested independently. Same numeric handling, per-axis review routing, and v5 release stamp as the adjusted override, gated on its own captured lens. Stored as `{ value, author, ingested_at, source_workbook, workbook_generated, lens, release_id_at_decision }`. | JSONB |
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
| `adjudication` | Team-consensus block, written by `poc3 review adjudicate`. Emitted shape (curation v5): `{ status: "adjudicated", axis, value, lens, agreed_by, decided_at, rationale, engine_score_at_decision, plan_version_at_decision, release_id_at_decision }`. `agreed_by` records who agreed — adjudication is a team determination (the PRD's "participants"). `release_id_at_decision` pins which exact release was contested — additive precision, never a staleness input. *(Proposed change:)* `axis` covers `adjusted` today; the PRD adds `extension_necessity`. The engine score is never mutated; the `engine_score_at_decision` + `plan_version_at_decision` stamps make staleness computable at read time — if the engine's value moves past ±0.01, the plan version changes, or the record is no longer scored, the consensus stops rendering and the row re-enters the review queue as RE-ADJUDICATE. A fresh adjudication renders as `Effective Score (adjudicated)` and suppresses the matching-axis override disagreement row. | JSONB |
| `confirmation` | Analyst agreement with the engine (curation v4, `Reviewed? = Agree` or CLI): `{ status: "confirmed", lens, confirmed_by, confirmed_at, source_workbook, workbook_generated, engine_score_at_confirmation, plan_version_at_confirmation, release_id_at_decision }`. A fresh confirmation retires the row's review-queue entry; staleness follows the same engine-value + plan-version model as adjudication. | JSONB |
| `facts` | Analyst corrections to LLM-extracted facts, keyed by fact name (e.g. `has_conditional_logic`, or the v30 extension-necessity pair `state_requirement_basis` / `core_can_express_requirement` — each independently correctable; the resolved `extension_necessity` value is never corrected directly, it recomputes from the corrected facts through the truth table on the next aggregate run), written by `poc3 review correct-fact`. Each entry shaped `{ value, lens, author, corrected_at, rationale, prior_value, prior_provenance, plan_version_at_correction, release_id_at_decision }` (the release stamp is curation v5, uniform across all decision types). LLM facts only — deterministic facts are refused (a wrong deterministic value is a code bug, not curation) — and values are validated against the extraction schema before write. Unlike adjudication stamps, `prior_*` and the plan stamp are provenance, not staleness gates: the newest correction always applies on the next `score aggregate` run, where the fact's sidecar provenance becomes `human_corrected`. `prior_provenance` is `llm`, `llm_downgraded:{reason}`, or `human_corrected`. | JSONB |
| `history` | Per-record change history. Band-column entries are shaped `{ column, value, author, replaced_at, source_workbook }`; replaced adjudications and fact corrections append `{ column, value, replaced_at }` where `column` is `adjudication` or `fact:{fact_name}` and `value` preserves the entire prior block (author metadata lives inside the block). *(Changed:)* band-column entries keep the 50-entry cap; **replaced adjudication and fact-correction blocks are exempt from the cap** — the PRD requires a complete history of scoring judgments per element, so prior decision blocks are never evicted. | JSONB |

**How are updates handled?** This entity is merge-preserve, not snapshot-replace
and not status-upsert. Review ingest merges per curated column using newest-wins
semantics with bounded history. Blank workbook cells do not clear stored values.
The adjudication and fact-correction writers follow the same newest-wins
pattern — the replaced block moves whole into `history`, which for these blocks
is uncapped, so the effect is the PRD's append model: each adjudication adds a
record; none is ever destroyed. The blank-cell rule applies to the workbook band
only, since the CLI writers always carry explicit values. Re-ingest and re-score
flows must leave this curation data untouched — and re-score flows must re-read
it, because fact corrections are a scoring input.

**Why this entity has a different durability posture** Unlike ingest, fact, and
score outputs, these rows are human-authored and cannot be regenerated by
rerunning pipeline stages. They therefore require backup and retention controls
at least as strict as committed source artifacts. The `facts` block goes
further: it is a scoring input — `poc3 score aggregate` overlays same-lens
corrections onto the fact pool before the rule cascade — so losing these rows
would change published scores on the next aggregate run, not just drop
annotations.

> **Adjudication axes — asserted: `adjusted` and `extension_necessity`; the
> base tier is deliberately not adjudicable.** Three things agree on this. The
> pipeline's own design: adjudication is adjusted-only by construction — the
> base tier is pure rule-cascade output, so team consensus that a tier is
> wrong is by definition a rule problem, and it routes to the version-bump
> path via the override-clustering diagnostic. The PRD's routing section says
> the same (a rule is wrong → methodology change + plan-version bump). And
> analysts already have a per-row voice on the base tier through
> `analyst_base_override`, which routes disagreement to review. The PRD's
> adjudication field list ("any or all of: NACHOS score, Adjusted NACHOS
> score, extension necessity") contradicts its own routing section; that is a
> PRD wording correction, tracked on the PRD follow-ups list, not a contract
> question. Forward-compat stays cheap either way: readers skip unknown
> `axis` values, so a future base axis would be data, not a schema change.
>
> Note the routing interplay with the v30 two-fact necessity model: an
> extension-necessity disagreement has *two* sanctioned fixes. If the evidence
> is wrong (a missed state mandate, a hallucinated core candidate), correct
> `state_requirement_basis` or `core_can_express_requirement` in the `facts`
> block and let the truth table recompute. Adjudication of the
> `extension_necessity` axis is for the residual case — the team overrides the
> resolved determination after state conversations, evidence unchanged.

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
| `scoring_run_id` | *(new)* The run this row reflects — joins to `scoring_runs` | BIGINT |
| `edfi_version` | Ed-Fi version recorded in the score sidecar | TEXT |
| `scored_at` | Timestamp when the scoring pipeline produced the artifact | TIMESTAMPTZ |
| `record_count` | Total number of records in the sidecar | INTEGER |
| `scored_count` | Number of records that were scored | INTEGER |
| `skipped_count` | Number of records skipped during scoring | INTEGER |
| `mean_quality_score` | Arithmetic mean of per-record quality scores for documented-authored rows | DOUBLE PRECISION |
| `needs_review_count` | Number of rows flagged for reviewer attention | INTEGER |
| `scoring_plan_version` | Scoring plan version string, e.g. `30`. Methodology-semantics changes bump it — rules, prompts' meaning, adjustment magnitudes (v28 added the `human_corrected` provenance capability; v30 retired the conflated `extension_is_necessary` fact in favor of `state_requirement_basis` + `core_can_express_requirement`). Envelope-shape changes bump `contract_version` instead (ADR 0017), and individual fact corrections bump neither — they are per-row data annotations. | TEXT |
| `model` | Model identifier used for LLM fact extraction | TEXT |
| `prompt_version` | Prompt version string, e.g. `phase-a.v1` | TEXT |
| `in_scope_count` | Number of in-scope rows used for NACHOS histogram and mean | INTEGER |
| `documentation_gap_count` | Count of rows where the documentation gap dimension fired | INTEGER |
| `header` | Full sidecar header JSON excluding the `scores` array. This includes `dimension_stats`, `nachos_score_histogram`, `adjusted_nachos_score_histogram`, `mean_nachos_score`, and `mean_adjusted_nachos_score`. | JSONB |
| `synced_at` | Timestamp when PostgreSQL sync ran | TIMESTAMPTZ |

**Does scoring erase and recreate all rows for a state?** No. This table is
upserted. The existing `(state, lens)` row is updated in place on every
aggregate run. It always reflects the latest run's statistics.

> With `scoring_runs` carrying per-run stats in its `header`, this entity — like
> `ingestion_status` — is a convenience projection of "latest run per (state,
> lens)". Physical table vs view over `scoring_runs` is a physical-design call;
> the contract only requires the fields above be readable at this grain.

---

## Entity: `score_labels` *(new)*

**Summary** Tier names, band labels, and display thresholds stored as data,
keyed by `scoring_plan_version` — so a methodology change ships its own labels
and thresholds, and no dashboard or report ever hard-codes "3+ is red". One row
per label per plan version.

**Created at stage** Written when a scoring plan version is introduced;
immutable for that version thereafter.

| Field | Description | Data Type |
|---|---|---|
| `scoring_plan_version` | Plan version these labels belong to (PK component) | TEXT |
| `label_type` | What is being labeled: `nachos_tier`, `adjusted_band`, `complexity_band`, or `threshold` (PK component) | TEXT |
| `value_key` | The value or tier being labeled, e.g. `3`, or a range key (PK component) | TEXT |
| `display_label` | Human-readable label, e.g. `High complexity` | TEXT |
| `description` | Longer explanation for tooltips / report legends | TEXT |
| `min_value` | Inclusive lower bound for band-type labels | DOUBLE PRECISION |
| `max_value` | Inclusive upper bound for band-type labels | DOUBLE PRECISION |
| `sort_order` | Display ordering | INTEGER |

**How are updates handled?** Append per plan version. Labels for a shipped plan
version are never edited — a labeling change is a plan-version bump, same as
any other methodology change.

---

## Publication — principle in the contract, mechanism in MC-6

Completing a run never changes what dashboards show: a run is *published* by an
explicit promotion, and rollback is moving the pointer back. That principle is
contract-level — consumers depend on it, and nothing may be designed against
it. The pointer mechanism itself (a small append-only table the
`current_scores` view resolves through, or equivalent) is storage design,
owned by MC-6; it stays additive as long as the principle holds and history is
retained.

---

## Named views — the read-model contract

Dashboards, PDF export, and the analytical engine bind to **named views**,
never to physical tables. The contract fixes the view *names* and each view's
purpose — that is the consumer seam. The exposed-field rosters are MC-6's
read-model deliverable, settled against the locked dashboard requirements and
drawing on the payload guarantees above; view internals (join vs. denormalize,
indexing) are physical design, changeable without touching consumers or the
emit.

| View | Purpose |
|---|---|
| `current_scores` | The published current score per `(state, lens, record_key)`, with effective values (engine vs. adjudicated) computed at the score ↔ curation join — never written into score rows. Resolves through the publication pointer once one exists. |
| `state_summary` | Per-`(state, lens)` aggregates — the dashboard landing grain, sourced from the run headers. |
| `current_source_elements` / `current_spine_elements` | The "current elements" read path — each state's latest completed ingestion run. |
| `score_history` | Prior runs' results joined to `scoring_runs` (run identity, assessor, digests) — feeds the UI's snapshot selector and cross-run comparisons. |

Access control grants are scoped to views, so reporting never needs access
to physical tables.

---

## Update-semantics summary

| Entity | Discipline | Grain / key |
|---|---|---|
| `ingestion_runs` *(new)* | append | `ingestion_run_id` |
| `source_elements` | retain-by-run | (`ingestion_run_id`, state, element) |
| `spine_elements` | retain-by-run | (`ingestion_run_id`, state, element) |
| `ingestion_status` | status-upsert | (state, lens) |
| `gap_logs` | retain-by-run | (state, `ingestion_run_id`) |
| `fact_runs` | append | `fact_run_id` |
| `fact_observations` | replace-with-run | (`fact_run_id`, `record_key`) |
| `scoring_runs` *(new)* | append | `scoring_run_id` |
| `score_records` | landed per run; current-table maintenance is the transform's (MC-6) | (state, lens, `record_key`) |
| `curation_records` | **merge-preserve** | (state, `record_key`) |
| `score_status` | status-upsert | (state, lens) |
| `score_labels` *(new)* | append per plan version | (`scoring_plan_version`, `label_type`, `value_key`) |

---

## The CI contract test

The companion `schemas/pipeline-records.schema.json` (JSON Schema, draft 2020-12)
defines every record shape above under `$defs`. POC-3 already commits its own
contract artifact for the score sidecar — `docs/contracts/assessment-release.schema.json`,
generated from `score/release_contract.py`, with an envelope/payload split —
and states that the Metadata Catalog CI validates emits against it. The two
must not drift: for score records this file defers to the release contract
(same envelope roster), and adds what it does not cover — element records,
fact artifacts, curation sidecars, and gap logs. The contract test validates a
sample of pipeline-emitted records against it on every build, and the
transform/DDL review checks the schema file against what the database extracts.
A field added to the emit without a schema update — or a schema update without
a doc update — fails the build. Under the JSONB-landing delivery model the
landing zone accepts any payload, so this check is the only place drift fails
a build rather than surfacing as a broken transform. Doc, schema, and transforms change together, through this
file's change flow (MC-17), or not at all.
