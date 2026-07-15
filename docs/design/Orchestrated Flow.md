# POC-3 Orchestrated Flow — Commands and Table Hydration

Purpose: one operator-facing runbook covering both the **full pipeline** and the
**lite (score-only) path** — showing all pipeline stages, the `poc3.exe` command
path, and which PostgreSQL tables are hydrated at each stage.

**Two paths at a glance:**

| | Full pipeline | Lite path |
|---|---|---|
| Command | `poc3.exe publish --yes` | `poc3.exe publish --lite --yes` |
| LLM stages | source lens + spine lens | source lens only (~half the cost) |
| Reports | all reports | recommendations, source review queue, analyst workbooks |
| Use when | you need API-model-lens analysis, coverage, divergence, reviewer comparison | you need the scalar NACHOS score and analyst workbooks only |

The lite path is a _path through_ the full pipeline, not a fork — lite scores
are byte-identical to full-pipeline scores.

---

## Part 1 — Full Pipeline

### Single Command

Use `publish` as the default run path because it executes ordered stages and
records cross-stage lineage in one place.

```powershell
.\.venv\Scripts\poc3.exe publish --yes
```

Useful variants:

```powershell
# Plan only (no writes)
.\.venv\Scripts\poc3.exe publish --dry-run

# Resume from a failed/changed stage
.\.venv\Scripts\poc3.exe publish --from score-run-all-source --yes

# Reports refresh only
.\.venv\Scripts\poc3.exe publish --reports-only --yes
```

### End-to-End Stage Flow

```mermaid
graph TD
    A[spine fetch optional] --> B[spine build]
    B --> C[ingest state loaders]
    C --> D[ingest sub-stage: swagger backfill]
    D --> E[ingest sub-stage: gap surface]
    E --> F1[score run all source]
    E --> F2[score run all spine]
    F1 --> G1[score aggregate source]
    F2 --> G2[score aggregate spine]
    G1 --> H1[score gap extract optional]
    H1 --> H2[score aggregate gap]
    G1 --> I1[report coverage]
    G2 --> I1
    H2 --> I2[report analyst]
    I1 --> I2
    I2 --> I3[report recommendations]
    I2 --> I4[report review digest]
    I4 --> I5[report reviewer comparison]
    I2 --> I6[report divergence]
    I2 --> I7[report scoring]
```

### Stage Commands (Manual Equivalents)

These are the manual equivalents for the same stage families that `publish`
orchestrates.

```powershell
# Spine
.\.venv\Scripts\poc3.exe spine fetch --state all
.\.venv\Scripts\poc3.exe spine build --state all

# Ingest
.\.venv\Scripts\poc3.exe ingest az
.\.venv\Scripts\poc3.exe ingest wi
.\.venv\Scripts\poc3.exe ingest mn
.\.venv\Scripts\poc3.exe ingest tx
.\.venv\Scripts\poc3.exe ingest in
.\.venv\Scripts\poc3.exe ingest swagger-backfill --state all
.\.venv\Scripts\poc3.exe ingest gap --state all

# Facts and scoring
.\.venv\Scripts\poc3.exe score run-all --state all --lens source --facts all --yes --cost-cap 80
.\.venv\Scripts\poc3.exe score run-all --state all --lens spine  --facts all --yes --cost-cap 80
.\.venv\Scripts\poc3.exe score aggregate --state all --lens source
.\.venv\Scripts\poc3.exe score aggregate --state all --lens spine

# Optional gap scoring path
.\.venv\Scripts\poc3.exe score gap-extract --state all --with-llm --prompt-version step3.full.v1
.\.venv\Scripts\poc3.exe score aggregate-gap --state all

# Reports
.\.venv\Scripts\poc3.exe report coverage --lens source
.\.venv\Scripts\poc3.exe report coverage --lens spine
.\.venv\Scripts\poc3.exe report analyst --all
.\.venv\Scripts\poc3.exe report recommendations --state all
.\.venv\Scripts\poc3.exe report review-digest --lens source
.\.venv\Scripts\poc3.exe report review-digest --lens spine
.\.venv\Scripts\poc3.exe report reviewer-comparison
.\.venv\Scripts\poc3.exe report divergence
.\.venv\Scripts\poc3.exe report scoring --lens spine
```

### Table Hydration Matrix

Reference source: `docs/Database-entity-details.md`.

| Stage family | Primary command path | Hydrated table(s) | Write pattern |
|---|---|---|---|
| Spine + Ingest | `poc3.exe publish` (stages: `spine-build`, `ingest`, `swagger-backfill`, `gap-surface`) | `source_elements`, `spine_elements`, `ingestion_status`, `gap_logs` | `source_elements` / `spine_elements` snapshot-replace per state; `ingestion_status` upsert per `(state,lens)`; `gap_logs` replace per state |
| Phase-A fact extraction | `poc3.exe publish` (stages: `score-run-all-source`, `score-run-all-spine`) | `fact_runs`, `fact_observations` | New run + observation snapshot for current state/lens/fact set |
| Phase-C scoring aggregate | `poc3.exe publish` (stages: `score-aggregate-source`, `score-aggregate-spine`) | `score_records`, `score_status` | `score_records` snapshot-replace per `(state,lens)`; `score_status` upsert per `(state,lens)` |
| Reporting surfaces | `poc3.exe publish` (report stages) | No additional DB hydration required | Reads hydrated scoring/ingest artifacts and emits report artifacts |

### Lineage and Freshness Guidance

1. Use `poc3 publish` as the single orchestrated run, since it executes stages
   and writes lineage together.
2. If you already ran ingest/scoring manually, run `publish` afterward to
   generate or refresh canonical lineage for that sequence.
3. `ingestion_status` and `score_status` can exist earlier, but they are stage
   summaries; canonical cross-stage freshness comes from the publish manifest.

### Quick Operator Checklist

- Run `poc3.exe publish --dry-run` to verify planned stages.
- Run `poc3.exe publish --yes` for the canonical lineage-bearing execution.
- If a stage fails, resume with `poc3.exe publish --from <stage> --yes`.
- Treat the publish manifest as the authoritative cross-stage freshness signal.

---

## Part 2 — Lite Path (Score-Only)

The shortest operator path from a state's documentation to the Adjusted NACHOS
Score in the analyst workbooks, with PostgreSQL tables hydrated at each stage.
`--lite` is a profile of the same `publish` orchestrator — ordered stages,
skip-if-fresh, cross-stage lineage in the manifest, the LLM confirmation gate
and `--cost-cap` all work the same.

What the lite profile cuts relative to the full flow: `extract-spine` +
`aggregate-spine` (the second API-model-lens scoring pass), `report-coverage`,
`report-review-digest`, `report-reviewer-comparison`, `report-divergence`, and
`report-scoring`.

### Single Command

```powershell
.\.venv\Scripts\poc3.exe publish --lite --yes
```

Useful variants:

```powershell
# Plan only (no writes) — shows exactly which stages lite runs vs cuts
.\.venv\Scripts\poc3.exe publish --lite --dry-run

# Resume from a failed/changed stage (lite stage names only)
.\.venv\Scripts\poc3.exe publish --lite --from extract-source --yes

# Reports refresh only — resolves to report-recommendations, not report-coverage
.\.venv\Scripts\poc3.exe publish --lite --reports-only --yes

# Refetch swagger first (TX needs the local TSDS stack)
.\.venv\Scripts\poc3.exe publish --lite --refresh-spine --yes
```

> **Flags that do NOT compose with `--lite`:** `--with-gap-llm` and
> `--with-spine-workbooks` are full-edition flags and will error under `--lite`.

### End-to-End Stage Flow (Lite)

```mermaid
graph TD
    A["<b>spine-fetch</b> · optional<br/>pull swagger from the state sandboxes"] --> B["<b>spine-build</b><br/>build the canonical Ed-Fi model graph"]
    B --> C["<b>ingest</b><br/>parse each state's docs into element records"]
    C --> D["<b>swagger-backfill</b><br/>add API-published entities the docs skip"]
    D --> E["<b>gap-surface</b><br/>list the model slots the docs never mention"]
    E --> F["<b>extract-source</b><br/>LLM fact extraction · state-doc lens<br/>(the ONLY paid stage; warm cache ~$0)"]
    E --> H["<b>aggregate-gap</b><br/>score the gap rows · deterministic, $0"]
    F --> G["<b>aggregate-source</b><br/>rule cascade → scored records<br/>(where the scalar is born)"]
    G --> S(("<b>scored<br/>records</b>"))
    H --> S
    S --> I1["<b>report-recommendations</b><br/>one remediation per below-target score"]
    S --> I2["<b>report-review-queue</b><br/>rows needing human eyes · source lens"]
    I1 --> I3["<b>report-analyst</b><br/>the deliverable Excel workbooks"]
    I2 --> I3
```

`gap-surface` and `aggregate-gap` survive the cut because they are free and
deterministic, and the analyst workbook's **Documentation Gaps** sheet reads the
gap records — cutting them would let that sheet go silently stale after a
re-ingest.

### Stage Commands (Manual Equivalents)

Prefer `publish --lite` for real runs; these are for partial reruns and
understanding.

```powershell
# Spine — fetch swagger, then build the canonical model graph.
# Per-state only (no --state all). TX fetch needs the local TSDS Docker stack.
# Writes: spine JSON under data/spine/ -> hydrates spine reference data.
.\.venv\Scripts\poc3.exe spine fetch --state AZ
.\.venv\Scripts\poc3.exe spine build --state AZ

# Ingest — parse each state's published docs into normalized element records.
# One pass writes BOTH lens artifacts; lite only ever scores the source one.
# Writes: {state}_elements_source.json + {state}_elements_spine.json
#   -> source_elements / spine_elements.
.\.venv\Scripts\poc3.exe ingest az
.\.venv\Scripts\poc3.exe ingest wi
.\.venv\Scripts\poc3.exe ingest mn
.\.venv\Scripts\poc3.exe ingest tx
.\.venv\Scripts\poc3.exe ingest in

# Swagger backfill — append rows for entities the API publishes but the source
# doc never mentions (documented=False; audit surface, not authored documentation).
.\.venv\Scripts\poc3.exe ingest swagger-backfill --state all

# Gap surface + deterministic gap scoring ($0) — feeds the workbook's
# Documentation Gaps sheet.
# Writes: gap artifacts -> gap_logs; gap score sidecars -> score_records.
.\.venv\Scripts\poc3.exe ingest gap --state all
.\.venv\Scripts\poc3.exe score aggregate-gap --state all

# Fact extraction — the ONLY stage that spends money (prompt-cached; warm
# reruns ~$0). SOURCE LENS ONLY in lite — halves LLM spend vs a cold full run.
# Writes: per-fact artifacts -> fact_runs / fact_observations.
.\.venv\Scripts\poc3.exe score run-all --state all --lens source --facts all --yes --cost-cap 80

# Aggregate — deterministic rule cascade over the facts; one scored record
# per element. Where the scalar is born.
# Writes: {state}_scores_source.json sidecars -> score_records / score_status.
.\.venv\Scripts\poc3.exe score aggregate --state all --lens source

# Reports — recommendations + review-queue BEFORE analyst (the workbook joins
# both). Pass --lens source explicitly to review-queue (its CLI default is spine).
.\.venv\Scripts\poc3.exe report recommendations --state AZ
.\.venv\Scripts\poc3.exe report review-queue --lens source
.\.venv\Scripts\poc3.exe report analyst --all
```

### On-Demand Commands (outside the lite chain)

```powershell
# Audit workbook — every element with each dimension's firing rule, consumed
# facts, and confidence. Where a contested score gets settled.
.\.venv\Scripts\poc3.exe report audit --state WI

# Analyst curation round-trip — read the green analyst-input band (incl. score
# overrides) back from a workbook; edits survive every regeneration.
# Hydrates: curation_records.
.\.venv\Scripts\poc3.exe review ingest data\out\wi_analyst.xlsx

# Human-vs-AI comparison — works against lite outputs, but requires the
# hand-placed reviewer workbooks under docs/human-scored-files/ (not in git).
.\.venv\Scripts\poc3.exe report review-digest --lens source
```

### Table Hydration Matrix (Lite)

Reference source: `docs/Database-entity-details.md`. Identical write patterns to
the full edition; the lite differences are which rows exist.

| Stage family | Primary command path | Hydrated table(s) | Lite difference |
|---|---|---|---|
| Spine + Ingest | `publish --lite` (stages: `spine-build`, `ingest`, `swagger-backfill`, `gap-surface`) | `source_elements`, `spine_elements`, `ingestion_status`, `gap_logs` | none — ingest hydrates both element tables in one pass |
| Phase-A fact extraction | `publish --lite` (stage: `extract-source`) | `fact_runs`, `fact_observations` | source-lens runs only; no spine-lens fact rows |
| Phase-C scoring aggregate | `publish --lite` (stages: `aggregate-source`, `aggregate-gap`) | `score_records`, `score_status` | `(state, source)` + gap records only; no `(state, spine)` rows |
| Reporting surfaces | `publish --lite` (report stages) | none (reads hydrated artifacts, emits report artifacts) | recommendations, source-lens review queue, analyst workbooks only |
| Analyst curation (on demand) | `review ingest <workbook>` | `curation_records` | none |

### Quick Operator Checklist (Lite)

- `poc3.exe publish --lite --dry-run` — verify the plan: score-only stages show
  `would_run`/`skipped_fresh`, everything else `skipped_disabled`.
- `poc3.exe publish --lite --yes` — the canonical lineage-bearing lite run. Only
  `extract-source` spends money; warm reruns are ~$0.
- Stage fails → resume with `poc3.exe publish --lite --from <stage> --yes` (lite
  stage names only).
- The publish manifest is the authoritative freshness signal — lite and full
  runs share it, so switching between them re-runs exactly what drifted.
- On manual runs, the one ordering rule that still applies in lite:
  recommendations + review-queue **before** analyst. Pass `--lens source` to
  `report review-queue` (its CLI default is spine).
- After analysts hand a workbook back, run `review ingest` — same as the full
  edition.
