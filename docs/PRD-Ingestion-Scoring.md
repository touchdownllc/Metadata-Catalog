# Metadata Catalog — Ingestion & Scoring PRD

> **Status:** Draft · **Last updated:** 2026-7-6 · **Owner:** Ed-Fi Alliance Staff

---

## 1. Product Overview

### 1.1 Background

The Data Standard Metadata Collection and Usage initiative collects, stores, and analyzes metadata about Ed-Fi Data Standard implementations — specifically the API specifications and supplemental data collection rules that state education agencies (SEAs) publish for vendors. Two high-value workflows sit at the center of the initiative: a **scoring engine** that measures the complexity of each SEA's data collection requirements, and a **cluster analysis** that surfaces cross-state patterns.

This PRD covers phase 2 of automated ingestion and NACHOS scoring.

This document covers the two pipelines that make those workflows possible:

* the **Ingestion Pipeline** (JTBD 1), which converts raw SEA artifacts (Swagger/OAS files and supplemental business documentation) into structured, element-level records.
* and the **Scoring Engine** (JTBD 3), which assigns NACHOS complexity scores to those records.

Access the following PRD for an overview of the overall Metadata Project.  **Link To be added**

### 1.2 Problem Statement

Ed-Fi staff have no scalable method to:

* Measure the complexity of a state's data collection requirements.
* Compare complexity across states.
* Identify which specific elements drive implementation burden.
* Prioritize standardization conversations with the highest-leverage states.

Manual scoring is slow, inconsistent across reviewers, and does not scale to the number of states and elements in scope. The ingestion process, and an LLM-assisted pipeline that produces auditable, human-overridable scores produce the output for dashboards and other analytics, and the list of elements that require evaluation to reduce complexity.

### 1.3 Solutions to the Problem

To solve the problems associated to the manual scoring, there will be automated processes to automatically: (1) pull a state Data Standard model configuration (handled in the ingestion pipeline);  (2) to define the Business logic imposed to vendors for each element, and apply the two NACHOS scores based on the logic (handled in the scoring pipeline); (3) to store these outputs in a storage database; (4) to produce a list of elements with some complexity that should be reviewed with the states, and (5) to support a manual override of some scores by adding new business requirements as these are learned from state conversations and updating the database with the new scores; and (6) to support further analytical work using the newly updated database.

**Story: At the end of the ingestion and scoring processes, the storage database will be updated with a state-specific Ed-Fi data model, with all the business requirements and logic for all elements that are passed to the vendors, their NACHOS and Adjusted NACHOS scores and rationale for the scoring and recommendations as a minimum,  so that  Ed-Fi members can run reports on the data, analyze common added complexity elements.  At the end of the scoring processes there will be a list artefact that lists the elements with some complexity scores, so that the Ed-Fi architecture team can discuss solutions with state agencies to reduce their complexity.**

The database will be updated with all the scoring information, including

* Updated swagger for any state, indicating the elements that are sent to SIS, Assessment or any other vendor for their processes, including extended entities, and/or extended elements within core entities.
* Updated business logic, which indicates what processes have to be run for a vendor to send that element.  A data element can trigger multiple business logic.
* NACHOS and Adjusted NACHOS Scores for each element, rationale for the scores, along with recommendation if applicable.
* Updated business logic and scores from manual review.

### 1.4 Term Definitions

| Term | Definition |
|---|---|
| **State-specific swagger** | in this document, it refers to all the elements that are required by that state for state reporting, including extensions, and if these extensions are necessary or not along with rationale to why they are necessary|
| **Extension** | Any data element, including entities, not listed in the Ed-Fi Data Standard in a specific version. Thus if the state uses Ed-Fi DS 4.0, then extensions refer to any element or entity not listed in the Ed-FI DS v4.0|
| **Source File** |Resulting file from the ingestion pipeline, which contains any data element of the data model listed by a state|
| **Spine File** |Resulting file from the ingestion pipeline, which contains the business logic identified for any element by a particular state |
| **In Scope file** |File that contains the data elements, including the entities, for a state-specific data model AND that are updated by vendors.  The in-scope file excludes values updated by the state agency, which are only pull by vendors|

### 1.5 Target Users for this PRD

| User | Role |
|---|---|
| **Ed-Fi Alliance Staff** | Primary operators of both pipelines; consumers of score reports and standardization analytics |

Excluded from this PRD are: SIS, Assessment or other Vendors, as SEA Staff.  They will be interested in read only capacity to some of the data. Vendors and SEA Staff may be given read access to some of the data in the future. SEA Staff may be given access to write a commitment form, and submit corrections to the scoring and business requirements.

### 1.6 Design Principles

**Auditability over automation.** Every score is traceable to an evidence record. A score without a traceable rationale is not acceptable.

**Human override is a feature, not an exception.** Staff can override any machine-assigned score. Overrides and their rationale are persisted alongside the original evidence record.

**No student data, no PII.** The system is scoped exclusively to specification metadata. No student data or personally identifiable information flows through any component at any time.

---

## 2. Functional Requirements

### 2.1 JTBD 1 — Ingestion Pipeline - IDENTIFIED AS "JTBD 1: Scoring" in Initial PRD

**Story:** As Ed-Fi Alliance Staff, I want to load a state's Swagger/OAS specification and supplemental business documentation into a structured catalog with one row per attribute and the state's specifications, so that I have clean, element-level records with business requirements and standard domain mapping ready for scoring and analysis.

#### 2.1.1 Source Input Collection

**Story:** As a user, I want to view the list of sources used to read business logic and be able to edit those sources — change a path, add more files, or remove an entry — so that I can manage the inputs driving the ingestion without restarting the process from scratch.

The system SHALL display the current list of configured source paths and URLs before and after an ingestion run, and SHALL allow a user to add, edit, or remove any entry from that list.

The system SHALL support the following source types for supplemental business documentation:

| Source Type | Description |
|---|---|
| **Confluence page** | A URL pointing to a Confluence page |
| **GitHub + Excel matrices** | A GitHub repository path containing Excel-based data matrices |
| **TWEDS** | Versioned XML artifacts or REST API access to the Texas Web-Enabled Data Standards |
| **PDF** | PDF documents such as data dictionaries or submission manuals |
| **Excel** | `.xlsx` files or worksheets, including those with multiple tabs or internal links to other resources |
| **HTML / URL** | A publicly accessible webpage |

The system SHALL validate that each provided file or URL is resolvable before proceeding. If a source fails validation, the system SHALL report the failure and allow the user to correct or remove the invalid entry without re-submitting valid sources.

#### 2.1.2 Entity Matching to Standard Domain

**Story:** As a user, I want each element from the state Swagger matched to its Ed-Fi standard domain so that the ingestion output identifies the standard domain for every entity and surfaces any elements that could not be matched.

After parsing the Swagger/OAS document(s), the system SHALL match each API entity (resource, descriptor, association) to its corresponding **Ed-Fi Data Standard domain definition** using a standard domain registry.

**Job Story** Each entity needs to be matched to a key domain based on the list of entities per domain provided, in this way, analysis across domains and states can be standardized.  For extended entities without a direct domain map, the process needs to map to the closest domain, based on the common names in the entity title, for example: coursetranscript_ext is very similar to coursetranscript, and therefore allocated to the same domain.  If the entity's name is completely different than an ed-fi entity, then the domain listed will be: state_specific.

The process should try to match the extended entities to domain as much as possible.  

**Job Story** Unmatched entities SHALL be flagged for staff review rather than silently dropped. Staff MAY resolve an unmatched entity by manually mapping it or marking it as a state-specific extension.

The output of the ingestion process SHALL contain one row per attribute with the following columns:

| Column | Description |
|---|---|
| `entity_name` | Entity name as it appears in the state's Swagger |
| `standard_entity` | Best-match entity name from the Ed-Fi Data Standard - CAN WE GET THIS?|
| `domain` | Ed-Fi domain (e.g., Student, Enrollment, Assessment) |
| `match_confidence` | High / Medium / Low / Unmatched |
| `match_notes` | Flags for extension entities, renamed entities, or descriptor overloads |
| `element_name` | Element name from the state definition |
| `element_type` | Element type from the Ed-Fi Data Standard |
| `element_cardinality` | Keys, optional, required, or optional conditional |

### 2.2 JTBD 3 — Scoring Engine

**Story:** As Ed-Fi Alliance Staff, I want to run a scoring engine that assigns a complexity score to each SEA data collection requirement, so that I can help the SEA align their data collection with the Ed-Fi Data Standard.

#### 2.2.1 Business Logic Extraction

The system SHALL accept a second input form — separate from the Swagger input — requesting the path(s) to the state's supplemental **business documentation** (e.g., data dictionaries, collection guides, data submission manuals, annotated spreadsheets).

The system SHALL run process that operates at the **element level**, associating documented business rules to the specific API field or descriptor they govern. The extraction SHALL capture:

* The element name (field path within the entity)
* The extracted business logic text (verbatim quoted span from the source document)
* The source document and page/section reference (cited span)
* A confidence score for the extraction

#### 2.2.2 Scope Classification

**Story:** As a user, I want each element classified as in-scope or out-of-scope based on who populates it and what write operations are permitted, so that scoring and analysis focus on the elements that carry real implementation burden for vendors.

The system SHALL run a scope classification for each element in the Swagger using the following definitions:

* **In-scope:** An element is in-scope if it is accessible by the vendor (SIS, assessment, or other vendor), and the vendor **can** perform a PUT, POST, or DELETE on it. These are elements where the state sets the requirements and vendors must conform.

* **Out-of-scope:** An element is out-of-scope if it is populated by the state and can only be retrieved by vendors via a GET. These are read-only state-owned elements that vendors consume but do not write.

The classification process SHALL emit a confidence level (High / Medium / Low) and a brief rationale for each out-of-scope classification. Staff MAY override any classification.

#### 2.2.3 Data Storage

**Story:** As a user, I want all ingested element data, business logic, and scope classifications automatically stored in the database so that the records are available for scoring and analysis.

The system SHALL write the following data to the storage database for each element upon completion of an ingestion run:

| Field | Description |
|---|---|
| `run_id` | Unique identifier for the ingestion run |
| `state_id` | State identifier |
| `entity_name` | Entity name from the state's Swagger |
| `element_name` | Name of the element as defined by the state |
| `data_type` | WHether it is a descriptor, reference, string.. |
| `cardinality` | WHether the element is an identity, required or optional |
| `source` | WHether it is core or extension |
| `element_path` | Full field path within the entity |
| `standard_entity` | Matched Ed-Fi Data Standard entity |
| `domain` | Ed-Fi domain |
| `business_requirements` | Extracted business rule text (null if none) |
| `cited_span` | Source document reference for the business logic |
| `in_scope` | Boolean scope flag |
| `scope_confidence` | High / Medium / Low |
| `created_at` | Timestamp of record creation |

TBD in discussion with Chris, Suganya:  As a user I want to have a clear understanding of the similarity of an extended element to the ed-Fi data standard, so that I can further inquiry with the state agency the reasoning for having an extension instead of the Ed-Fi core element
---

#### 2.2.4 Scoring LLM Execution

**Story:** As a user, I want the scoring engine to extract facts from the business logic using an LLM, evaluate deterministic facts computed from the record structure, and assign scores using a rules cascade, so that each element has a well-supported, auditable complexity score.

**Acceptance Criteria:** DRAFT
The resulting NACHOS scores and Adjusted NACHOS score for each record have all the rationale criteria that trigger the scoring, so that a user can link the actual scores to the rationale, and then link the rationale to the swagger and elements that trigger that rationale.

**NACHOS Score Context (peer signal).** Every scored row SHALL carry key dimension results from the scoring process, beside the scalar and the previous elements listed. These elements are most likely produced by the LLM or deterministic questions.  These are:

* Documentation_completeness (definition/rules/types/descriptors present or not, deterministic from the Swagger/API model, user override),
* business_logic_complexity (Conditional | Conceptual | Cross-reference | Regulatory | Unspecified),
* All the Drivers for adjustment (necessary extension | Unnecessary extension | cross-entity logic)
* Extension_justification  (why is necessary, or standalone)
* Semantic_fidelity (TO BE CONFIRMED: narrowed/broadened/divergent?)
* Definition quality (present, >= 20 chars, implementable)
* Element name alignment (matches Ed-Fi's name)

The system SHALL run a Scoring LLM against each in-scope element record produced by the Ingestion Pipeline. For each element the LLM SHALL extract and emit an **evidence record** containing, at minimum:

| Field | Description |
|---|---|
| `element_path` | Full field path within the entity |
| `conditional_logic` | Boolean — does the row describe conditional or branching logic? |
| `cross_entity_logic` | Boolean — does the row involve lookups or constraints across entity boundaries? |
| `aggregation` | Boolean — does the row require computed aggregates or derived values? |
| `concatenation` | Boolean — does the row require string construction from multiple fields? |
| `semantic_divergence` | Boolean — does the state narrow, broaden, or redefine the canonical Ed-Fi meaning? |
| `documentation_style` | `Prescriptive` \| `Conceptual` \| `Cross-reference` \| `Regulatory` \| `Unspecified` |
| `extension_necessity` | Boolean — does the element require a non-standard extension or descriptor override? |
| `cited_spans` | List of verbatim excerpts from source documentation that support the above flags |
| `nachos_score` | Base NACHOS scalar (integer) |
| `adjusted_nachos` | NACHOS score after semantic-fidelity adjustment (integer) |
| `adjustment_rationale` | Plain-text explanation of any adjustment applied |
| `candidate_recommendations` | List of suggested simplification or standardization actions |
| `firing_rule_path` | The ordered list of rules from the rule cascade that produced this score |
| `tier_name` | Name of the NACHOS tier matched by the rule cascade |
| `confidence` | Overall confidence in the score assignment (High / Medium / Low) |
| `review_routing` | `Auto-accept` \| `Route-for-review` |

> [!NOTE]
> **NACHOS Score Context (peer signal).** Every scored row SHALL carry, alongside the scalar score:
>
> * **Implementation Shape** — structural complexity, deterministic from the Swagger/API model
> * **Documentation Style** — one of: Prescriptive, Conceptual, Cross-reference, Regulatory, Unspecified
> * **Documentation Gap** — flag for complex structures the documentation under-explains

> [!NOTE]
> **Semantic-fidelity adjustment.** When a state narrows, broadens, or changes the canonical Ed-Fi meaning of an element, an adjustment SHALL be applied to the base NACHOS score. Exact adjustment magnitudes are subject to Alliance methodology review and SHALL be configurable without a code deployment.

#### 3. Rule Cascade and Score Overrides

**Story:** As a user, I want to see all the rules and cascade elements that are triggered for each row, along with the associated NACHOS score, Adjusted NACHOS score, and the name of the tier matched, so that I can understand exactly how each score was derived.

**Story:** As a user, I want to adjust scoring rules for specific fields with a rationale, or add new logic specific to a state, and trigger rescoring — with the full history of scoring changes preserved — so that I can refine scores based on domain expertise without losing the prior scoring record.

The scoring logic SHALL implement the rule cascade defined in the **POC Recommendation document (page 11)**. The rule cascade determines which combination of extracted flags maps to which NACHOS tier and drives the `firing_rule_path` and `tier_name` fields in the evidence record.

Staff SHALL be able to inspect the full `firing_rule_path` and `tier_name` for any scored element to understand exactly which rules fired and which tier was matched.

#### 3.1 Logic alignment, rescoring and manual score overwrite

* **Story** as an Ed-Fi ALliance Staff, I must be able to add a new business rule for a particular element or an entity, and re-run the LLM scoring, showing the old and new NACHOS and adjusted NACHOS scores, along with the rules that it triggers, and decide if the new scores are acceptable.
*
* **Story** Ed-Fi Alliance Staff MUST be able to override the assigned score based on human judgment, with the new scores and rationale recorded against the row's evidence record.

**Story:** As a user, I want to pull all scores, evidence records, rule overrides, and the full scoring history stored in the database so that any score is inspectable without re-running the model and all changes are traceable over time.

The system SHALL preserve a complete history of scoring changes per element, including the original machine-assigned score, each override or addition applied, the rationale provided, the staff member who made the change, and the resulting rescored values.

#### 3.2 Score and Override Storage

The system SHALL write scores and evidence records to the storage database upon completion of a scoring run, extending each element record with the following fields:

| Field | Description |
|---|---|
| `scoring_run_id` | Unique identifier for the scoring run |
| `nachos_score` | Base NACHOS scalar |
| `adjusted_nachos` | Adjusted NACHOS scalar |
| `override_nachos_score` | Staff-assigned score (null if not overridden) |
| `override_rationale` | Staff-provided rationale for override (null if not overridden) |
| `override_by` | Staff member who applied the override (null if not overridden) |
| `scored_at` | Timestamp of score assignment |
| `override_adjusted_nachos_score` | Calculated over override_nachos_score (null if not overridden) |

TBD with Chris, Suganya:  Should we label the nachos_score as OLD, and then update new nachos-score with the overwritten value.
The evidence record SHALL be stored in full so that any score is inspectable without re-running the model.

The system SHALL maintain a scoring history table that records each override or state-specific rule addition, preserving the prior score, the change applied, the rationale, and the re-scored result.

---

### 4. JTBD 3 — Score Evaluation Analytics _(TBD)_

Analysis of aggregate score data across states and across time is scoped for a future phase. Requirements will be defined separately.

---

## 3. Non-Functional Requirements

### 3.1 Implemented Requirements

| ID | Category | Requirement | Implementation |
|---|---|---|---|
| NFR-DATA-1 | Data Integrity | All ingestion and scoring runs SHALL be transactional — no partial writes | Database transaction wrapping each run |
| NFR-DATA-2 | Provenance | Every ingestion record SHALL carry a `run_id`, `state_id`, and `created_at` timestamp | Applied at write time |
| NFR-DATA-3 | Provenance | Every score SHALL carry a full evidence record persisted alongside the scalar | `evidence_record` JSON column on score table |
| NFR-SEC-1 | Security | No student data or PII SHALL flow through any pipeline component | Input validation at ingestion form; static analysis gate in CI |
| NFR-ACC-1 | Accessibility | All views and forms SHALL meet WCAG 2.1 AA | Verified with automated accessibility checker in CI |

### 3.2 Quality Gates (Scoring Engine)

| ID | Metric | Target | Notes |
|---|---|---|---|
| QG-1 | Score-tier alignment | >= 80% | Measured against the evaluation set seeded by Ed-Fi's existing manual scores, treated as human-scored observations, not ground truth |
| QG-2 | F1 (overall) | >= 0.7 | Calibration signal against evaluation set |
| QG-3 | F1 (simple / score-zero rows) | >= 0.9 | High precision on simple rows is critical — false positives here create unnecessary review burden |
| QG-4 | Cohen's Kappa | >= 0.6 | Inter-rater agreement between engine and human reviewer |
| QG-5 | Disagreement routing | 100% of tier disagreements routed for review | Disagreements are treated as calibration signal, not auto-resolved |

---

## 4. Architecture

### 4.1 Technology Stack

> [!NOTE: TO BE VALIDATED]
| Component | Technology | Notes |
|---|---|---|
| Ingestion LLM | Claude (Anthropic) | Configurable model; defaults to claude-sonnet-4-6 |
| Scoring LLM | Claude (Anthropic) | Same model pool; scoring prompt is distinct from ingestion prompt |
| Storage | PostgreSQL | Element catalog, evidence records, score tables |
| Job Queue | To be determined | Async execution of ingestion and scoring runs |
| API layer | To be determined | REST endpoints for form submissions, run status, report export |
| Frontend | To be determined | Input forms, progress views, report tables, scored element forms |

### 4.2 Pipeline Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  INGESTION PIPELINE (JTBD 1)                                         │
│                                                                      │
│  [User: view / edit source list]                                     │
│         │                                                            │
│         ▼                                                            │
│  Parse & Validate Swagger  ──► Entity Match to Ed-Fi Domain Registry │
│         │                                                            │
│  [User: Business doc paths/URLs — Confluence, GitHub/Excel,          │
│         TWEDS, PDF, Excel, HTML]                                     │
│         │                                                            │
│         ▼                                                            │
│  Business Logic Extraction LLM  (element-level, verbatim +          │
│  provenance pointer)                                                 │
│         │                                                            │
│         ▼                                                            │
│  Scope Classification  (in-scope: state-populated, vendor cannot     │
│  PUT/POST/DELETE; out-of-scope: state-populated, GET only)          │
│         │                                                            │
│         ▼                                                            │
│  Write to Storage DB  (elements + business logic + scope flags)     │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  SCORING ENGINE (JTBD 3)                                             │
│                                                                      │
│  Read in-scope elements from Storage DB                             │
│         │                                                            │
│         ▼                                                            │
│  Scoring LLM  ──► LLM-extracted facts + deterministic facts         │
│         │         computed from record structure                     │
│         ▼                                                            │
│  Rule Cascade  ──► NACHOS Score + Adjusted NACHOS + Tier Name       │
│         │                                                            │
│         ▼                                                            │
│  [User: inspect firing_rule_path + tier_name per row;               │
│   adjust rules or add state-specific logic; trigger rescore]        │
│         │                                                            │
│         ▼                                                            │
│  Write scores + evidence records + override history to Storage DB   │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.3 Data Model (Key Tables)

| Table | Primary Key | Key Columns |
|---|---|---|
| `ingestion_runs` | `run_id` | `state_id`, `created_at`, `approved_by`, `status` |
| `elements` | `element_id` | `run_id`, `state_id`, `entity_name`, `element_path`, `standard_entity`, `domain`, `in_scope`, `business_logic`, `cited_span` |
| `scoring_runs` | `scoring_run_id` | `run_id` (FK → ingestion_runs), `created_at`, `status` |
| `scores` | `score_id` | `element_id` (FK → elements), `scoring_run_id`, `nachos_score`, `adjusted_nachos`, `tier_name`, `evidence_record` (JSON), `override_score`, `override_by` |
| `scoring_history` | `history_id` | `score_id` (FK → scores), `prior_score`, `change_type`, `rationale`, `changed_by`, `rescored_at` |

---

## 5. Out of Scope

The following are explicitly out of scope for this PRD:

* JTBD 3 (Score Evaluation Analytics) — requirements TBD in a subsequent document
* Student data of any kind
* Real-time or webhook-based specification monitoring (batch ingestion only)
* Direct SEA portal access or automated fetching from SEA systems
* Vendor-facing score exposure (internal Ed-Fi staff use only in this phase)
* Natural language query interface (separate capability in the broader initiative)
* Cluster analysis across states (separate capability)

---

## 6. Backlog

Active issues are tracked in the project issue tracker. The following items are known open questions as of this draft:

| ID | Area | Question | Status |
|---|---|---|---|
| TBD | Scoring | Exact NACHOS adjustment magnitudes for semantic divergence cases | Pending Alliance methodology review |
| TBD | Ingestion | Handling of multi-version Swagger files for the same state | Open |
| TBD | Scoring | Evaluation set composition and reviewer assignment | Ed-Fi SHALL define before acceptance testing |
| TBD | Architecture | Job queue technology selection | Open |
| TBD | Reporting | Format and delivery mechanism for cross-state distribution reports | Open |

---

## 7. Environment Variable Reference

### 7.1 Ingestion Pipeline

| Variable | Description | Default |
|---|---|---|
| `INGESTION_LLM_MODEL` | Claude model ID for business logic extraction | `claude-sonnet-4-6` |
| `INGESTION_LLM_MAX_TOKENS` | Max tokens per extraction call | `4096` |
| `INGESTION_MAX_SOURCES` | Maximum files/URLs per ingestion run | `10` |
| `INGESTION_SUPPORTED_DOC_FORMATS` | Comma-separated list of accepted doc extensions | `pdf,docx,xlsx,txt,html` |
| `DB_CONNECTION_STRING` | PostgreSQL connection string | _(required)_ |

### 7.2 Scoring Engine

| Variable | Description | Default |
|---|---|---|
| `SCORING_LLM_MODEL` | Claude model ID for scoring | `claude-sonnet-4-6` |
| `SCORING_LLM_MAX_TOKENS` | Max tokens per scoring call | `4096` |
| `SCORING_BATCH_SIZE` | Elements per LLM batch call | `25` |
| `NACHOS_ADJUSTMENT_CONFIG` | Path to YAML file defining semantic-fidelity adjustment magnitudes | `config/nachos-adjustments.yaml` |
| `RULE_CASCADE_CONFIG` | Path to YAML file defining the rule cascade | `config/rule-cascade.yaml` |
| `QG_TIER_ALIGNMENT_THRESHOLD` | Score-tier alignment quality gate threshold | `0.80` |
| `QG_F1_OVERALL_THRESHOLD` | Overall F1 quality gate threshold | `0.70` |
| `QG_F1_SIMPLE_THRESHOLD` | F1 quality gate threshold for score-zero rows | `0.90` |
| `QG_KAPPA_THRESHOLD` | Cohen's Kappa quality gate threshold | `0.60` |
