# Metadata Catalog — Ingestion & Scoring PRD

> **Status:** Draft · **Last updated:** 2026-7-6 · **Owner:** Ed-Fi Alliance Staff

---

## 1. Product Overview

### 1.1 Background

The Data Standard Metadata Collection and Usage initiative collects, stores, and analyzes metadata about Ed-Fi Data Standard implementations — specifically the API specifications and supplemental data collection rules that state education agencies (SEAs) publish for vendors. Three high-value workflows sit at the center of the initiative: a **scoring engine** that measures the complexity of each SEA's data collection requirements, a process that involves conversations with the SEAs to align their specifications to the Ed-Fi Data Standard (reduction in complexity), and a **cluster analysis** that surfaces cross-state patterns.

This Ingestion and Scoring PRD covers the following jobs from the overall Metadata Catalog project:

* JTBD 1 (Scoring Engine)
* JTBD 3 (Standardization of Data Collection),
* JTBD 4 (Storage of API Specifications),
* JTBD 6 (Enrichment of API Specifications)
* and produces the output that will be written into the database --JTBD 7(Storage Engine).

This document covers the two pipelines that make those workflows possible:

* the **Ingestion Pipeline**, which converts raw SEA artifacts (Swagger/OAS files and supplemental business documentation) into structured, element-level records.
* and the **Scoring Engine** , which assigns NACHOS complexity scores to those records.

The overall Metadata Catalog PRD has an overview of all the project jobs.  See [Metadata Catalog PRD](./PRD.md) for more details.

### 1.2 Problem Statement

Ed-Fi staff have no scalable method to:

* Measure the complexity of a state's data collection requirements.
* Compare complexity across states.
* Identify which specific elements drive implementation burden.
* Prioritize standardization conversations with the highest-leverage states.

Manual scoring is slow, inconsistent across reviewers, and does not scale to the number of states and elements in scope.

### 1.3 Solutions to the Problem

The ingestion process, and an LLM-assisted pipeline that produces auditable, human-overridable scores produce the output for dashboards and other analytics, and the list of elements that require evaluation to reduce complexity.

The solution to the problem consists of a series of automated processes that accomplish these functions:

* (1) pull the most recent Data Standard model configuration for a specific state (handled in the ingestion pipeline);
  
* (2) to define the Business logic imposed to vendors for each element, and apply the two NACHOS scores based on the logic (handled in the scoring pipeline);
  
* (3) to store these outputs in a storage database;
  
* (4) to produce a list of elements that should be reviewed with the states, in order to reduce their complexity; and

* (5) to support a manual override of some scores based on state conversations.

**Story: At the end of the ingestion and scoring processes, the storage database will be updated with the state-specific Ed-Fi data model and derived results from the ingestion and scoring pipelines,  so that an analytical engine can read the data to run reports, produce dashboards and run cluster analyses.  The analytical engine will also lists the elements with some complexity scores, so that the Ed-Fi solutions team can discuss alternatives with state agencies to reduce such complexity.**

The database will be updated with all the ingestion and scoring information, including

* Updated swagger for any state, indicating the elements that are sent to SIS, Assessment or any other vendor for their processes, including extended entities, and/or extended elements within core entities.
* Updated business logic, which indicates what processes have to be run for a vendor to send that element.  A data element can trigger multiple business logic.
* NACHOS and Adjusted NACHOS Scores for each element, rationale for the scores, along with the applicable recommendations.
* Updated business logic and scores from manual review and rationale for the updates.

### 1.4 Term Definitions

| Term | Definition |
|---|---|
| **State-specific swagger** | in this document, it refers to all the elements that are required by that state for state reporting, including extensions, and if these extensions are necessary or not along with rationale to why they are necessary |
| **Extension** | Any data element, including a whole entity, not listed in the Ed-Fi Data Standard in a specific version. For example, if the state uses Ed-Fi DS 4.0, then an extension refers to any element or entity not listed in the Ed-FI DS v4.0 |
| **Source File** | Resulting artifact from the ingestion pipeline - stage 1, is the state's own published documentation that is written for vendors, with one record per element (entity.element) with the state's verbatim text plus a provenance point. |
| **Spine File** | Resulting artifact from the ingestion pipeline - stage 2, is the Ed-Fi Swagger/API specification that contains the structure (entities, elements, foreign-key chains, descriptors, extension surfaces) that the state-specific documentation is joined to |
| **Lens** | A lens is one of two parallel, never-averaged views of a state's data:  Source lens — starts from what the state actually wrote; one row per source-document entry; answers "how clear is the state's authored prose?"; Spine lens — starts from the full canonical Ed-Fi surface; one row per spine (entity, element) pair; answers "how much of Ed-Fi does the state document at all?" |
| **Gap Log File** | Resulting artifact from the ingestion pipeline, which indicates the spine elements NOT mentioned in the state's source documentation |
| **In Scope attribute** | Attribute that indicates if the data element needs to be populated by vendors (in-scope = true).  If the element is populated by the state and only read by the vendor, then in-scope = false.  Descriptors are in-scope |
|**Adjudication** | Is the official determination made by an authorized authority or expert panel after reviewing evidence, data, or documentation.  In this case, the authority is the Ed-Fi solutions architecture team, who is reviewing an element and making a decision to officially change and extension determination or complexity score |

### 1.5 Target Users for this PRD

| User | Role |
|---|---|
| **Ed-Fi Alliance Staff** | Primary operators of both pipelines; consumers of score reports and standardization analytics |
| **Developers and Analytical teams** | Creators of the system and users of the analytical engine|

Excluded from this PRD are: SIS, Assessment or other Vendors, and SEA Staff.

### 1.6 Design Principles

**Auditability over automation.** Every score is traceable to an evidence record. A score without a traceable rationale is not acceptable.

**Human override is a feature, not an exception.** Staff can override any machine-assigned score. Overrides and their rationale are persisted alongside the original evidence record.

**No student data, no PII.** The system is scoped exclusively to specification metadata. No student data or personally identifiable information flows through any component at any time.

---

## 2. Functional Requirements

### 2.1 JTBD 3/4/6 — Ingestion Pipeline

**Story:** As Ed-Fi Alliance Staff, I want to load a state's Swagger/OAS specification and supplemental business documentation into a structured catalog with one row per attribute and the state's specifications, so that I have clean, element-level records with business requirements and standard domain mapping ready for scoring and analysis.

#### 2.1.1 Source Input Collection and Business logic Extraction

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

The system SHALL accept a second input form — separate from the Swagger input — requesting the path(s) to the state's supplemental **business documentation** (e.g., data dictionaries, collection guides, data submission manuals, annotated spreadsheets).

Note: The POC  ingestion used two  input 1 the Swagger spec, input 2 the business-documentation sources.  The two streams are separate commands (spine fetch / spine build for the swagger; the per-state ingest commands for the documentation), both run at the ingestion stage, and the ingested element record carries the state's verbatim business text with provenance (business_rules_text, element_specific_rules, definition_text, source_document, source_page_or_section).

The system SHALL run process that operates at the **element level**, associating documented business rules to the specific API field or descriptor they govern. The extraction SHALL capture:

* The element name (field path within the entity)
* The extracted business logic text (verbatim quoted span from the source document)
* The source document and page/section reference (cited span)
* A confidence score for the extraction

#### 2.1.2 Entity Matching to Standard Domain

**Story:** As a user, I want each element from the state Swagger matched to its Ed-Fi standard domain so that the ingestion output identifies the standard domain for every entity and surfaces any elements that could not be matched.

After parsing the Swagger/OAS document(s), the system SHALL match each API entity (resource, descriptor, association) to its corresponding **Ed-Fi Data Standard domain definition** using a standard domain registry.

**Job Story** Each entity needs to be matched to a key domain based on a list  provided, in this way, analysis across domains and states can be standardized.  For extended entities, the process needs to map these to the closest domain, using the common names in their titles. For example: coursetranscript_ext is very similar to coursetranscript, and therefore allocated to the same domain.  

The process should try to match the extended entities to domain as much as possible.  The POC yields entity matching

**Job Story** Unmatched entities SHALL be flagged for staff review rather than silently dropped. Staff MAY resolve an unmatched entity by manually mapping it or marking it as a state-specific extension.

**Job Story** The history of the state business rules or business requirements needs to be preserved, either with a version or tag, so that the scores and reports can be associated with the tag.

The output of the ingestion process SHALL contain one row per attribute with the following columns:

| Column | Description |
|---|---|
| `entity_name` | Entity name as it appears in the state's Swagger |
| `standard_entity` | Best-matched entity name from the Ed-Fi Data Standard |
| `domain` | Ed-Fi domain (e.g., Student, Enrollment, Assessment) |
| `match_confidence` | High / Medium / Low / Unmatched |
| `match_notes` | Flags for extension entities, renamed entities, or descriptor overloads |
| `element_name` | Element name from the state definition |
| `element_type` | Element type from the Ed-Fi Data Standard |
| `element_cardinality` | Keys, optional, required, or optional conditional |
| `element_specific_rules` | State requirements for that specific element |
| `definition_text`| Element definition by the state |
| `source_document` | which file the element came from (example, the AZ matrix workbook, a WI Confluence page, etc.) |
| `source_page_or_section` | where the source document file is citing that element |
| `state_requirements_tag` | timestamp of when the state business rules were updated |

### 2.2 JTBD 3 — Scoring Engine

**Story:** As Ed-Fi Alliance Staff, I want to run a scoring engine that assigns a complexity score to each SEA data collection requirement, so that I can help the SEA align their data collection with the Ed-Fi Data Standard.

The scoring applies a rubric to assign a NACHOS value from 0 to 3, and and Adjusted NACHOS value from 0 to 4.5. The current rubric is listed on a Rubrics document.  This rubric was validated with the community.

#### 2.2.1 Business Logic

The scoring runs extraction over the records produced from the Swagger and Business Requirement ingestion. Scoring never re-reads the raw documents; it extracts facts from the ingested records.  

The system SHALL accept a second input form — separate from the Swagger input — requesting the path(s) to the state's supplemental **business documentation** (e.g., data dictionaries, collection guides, data submission manuals, annotated spreadsheets).

#### 2.2.2 Scope Classification

**Story:** As a user, I want each element classified as in-scope or out-of-scope based on the scope definition below, so that scoring and analysis focus on the elements that carry real implementation burden for vendors.

The system SHALL run a scope classification for each element in the Swagger using the following definitions:

* **In-scope:** An element is in-scope if it is accessible by the vendor (SIS, assessment, or other vendor), and the vendor **can** perform a PUT, POST, or DELETE on it. These are elements where the state sets the requirements and vendors must conform.

* **Out-of-scope:** An element is out-of-scope if it is populated by the state and can only be retrieved by vendors via a GET. These are read-only state-owned elements that vendors consume but do not write.

The classification process SHALL emit a confidence level (High / Medium / Low) and a brief rationale for each out-of-scope classification. Staff MAY override any classification.

#### 2.2.3 Data Storage

**Story:** As a user, I want all ingested element data, business logic, and scope classifications automatically stored in the database so that the records are available for scoring and analysis.

The system SHALL write AT A MINIMUM the following data to the storage database for each element:

| Field | Description |
|---|---|
| `run_id` | Unique identifier for the ingestion run |
| `state_id` | State identifier |
| `entity_name` | Entity name from the state's Swagger |
| `element_name` | Name of the element as defined by the state |
| `data_type` | WHether it is a descriptor, reference, string |
| `cardinality` | WHether the element is an identity, required or optional |
| `source` | WHether it is core or extension |
| `element_path` | Full field path within the entity |
| `standard_entity` | Matched Ed-Fi Data Standard entity |
| `domain` | Ed-Fi domain |
| `element_specific_rules` | Business requirements for that specific element |
| `definition_text`| Element definition by the state |
| `source_document` | Name or path to the source document for the business rules |
| `source_page_or_section` | Title(s), section names or path(s) to the specific page(s) or section(s) with the business rule for that element |
| `state_requirements_tag` | timestamp of when the state business rules were updated |
| `cited_span` | A verbatim excerpt from the state's source documentation that supports a specific extracted fact or business-rule claim |
| `in_scope` | Boolean scope flag |
| `scope_confidence` | High / Medium / Low |
| `created_at` | Timestamp of record creation |

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

Every scored element shall emit an **evidence record** to be stored in the database. See [Database-entity-details](./design/Database-entity-details.md)

#### 3. Rule Cascade and Score Overrides

**Story:** As a user, I want to see all the rules and cascade elements that are triggered for each row, along with the associated NACHOS score, Adjusted NACHOS score, and the name of the tier matched, so that I can understand exactly how each score was derived.

Ed-Fi Staff SHALL be able to inspect the full `firing_rule_path` and `tier_name` for any scored element to understand exactly which rules fired and which tier was matched.

---

### Score Disagreements

After the scoring is completed and stored in the database, the Ed-Fi Staff will review the scores and filter a list of elements with complexity.  This job is not included in this PRD (JBTD 14 Human Review).  

The Ed-Fi staff may make some corrections the scores due to a series of cases:

* Adjudication: The Ed-Fi team may determine a different score after discussions with state agencies. For example, an extension may be deemed necessary due to an undocumented business requirement, or an element may be considered less complex because the complexity is calculated by the state and only the resulting value is provided to vendors.  The team's decision is recorded as an adjudication — who agreed, when, the rationale, and the engine score and plan version at decision time —. The adjudicated results are displayed in the "Adjudicated" fields alongside the original engine-calculated scores or extension assessment.
  
* A fact is wrong (an LLM extraction error): correct the fact, and the unchanged rule cascade recomputes both scores, audit-trailed. This is the sanctioned intervention point (fact-level curation — the one path still planned in the POC). It's also where the PRD's "Adjusted must stay consistent with base" instinct is honored, with the engine as the only writer of scores.
  
* A rule is wrong.  This refers to the clustering analysis that the model determines.  That's evidence about the methodology, not about rows. The fix is a rule/prompt change plus a scoring-plan version bump, so the whole corpus benefits. The POC's Score Card carries a clustering diagnostic that aggregates override disagreements by adjustment type, direction, and delta, split by contested axis, so this signal surfaces without anyone hunting for it.

 This process is run in the HUMAN Review -JBTD 14.  It is listed in this PRD to clarify any possible implications to the Scoring Process:

**Story:** As a user, I want to record my judgment on a score with a rationale, see it preserved through every re-run, and have disagreements routed to the right fix — a fact correction, a methodology change, or a team adjudication — so that scores reflect expert review without losing the machine record.

 **Job Story:** The system SHALL NOT modify engine scores in response. The USER MAY NOT OVERRIDE the NACHOS or ADJUSTED NACHOS Score or extension necessity.  The system SHALL route any override-vs-engine disagreement for review, naming which score is contested.  

The system SHALL preserve a complete history of scoring changes per element, including the original machine-assigned score, each override or addition applied, the rationale provided, the staff member who made the change, and the resulting new values.

The engine score is immutable and always visible; human judgment is recorded as a distinct, provenance-carrying layer.

The Human Review Process JBTD 14 SHALL write adjudicated scores and evidence records to the storage database, extending each element record with the following fields:

| Field | Clarification |
|---|---|
| `scoring_run_id` | Unique identifier for the scoring run |
| `nachos_score` | System resulting NACHOS |
| `necessary_extension` | From the ingestion process |
| `adjusted_nachos` | System resulting Adjusted NACHOS score |
| `adjudicated_nachos_score` | Adjudicated NACHOS score |
| `adjudicated_adjusted_nachos` | Adjudicated Adjusted NACHOS score |
| `adjudicated_necessary_extension` | Adjudication value of the necessary extension |
| `adjudication_rationale` | Staff-provided rationale for override any or all the values adjudicated (null if not overridden) |
| `adjudication_by` | Staff member who applied the override (null if not overridden) |
| `adjudication_status` | is the decision by consensus or with some reservations or clarification |
| `adjudication_time` | Timestamp of the adjudication being introduced |

## 3. Non-Functional Requirements

### 3.1 Implementation Requirements

| ID | Category | Requirement | Implementation |
|---|---|---|---|
| NFR-DATA-1 | Data Integrity | All ingestion and scoring runs SHALL be transactional — no partial writes | Database transaction wrapping each run |
| NFR-DATA-2 | Provenance | Every ingestion record SHALL carry a `run_id`, `state_id`, and `created_at` timestamp | Applied at write time |
| NFR-DATA-3 | Provenance | Every score SHALL carry a full evidence record persisted alongside the scalar | `evidence_record` JSON column on score table |
| NFR-SEC-1 | Security | No student data or PII SHALL flow through any pipeline component | Input validation at ingestion form; static analysis gate in CI |

## 4. Architecture

For technology Stack, Pipeline Overview, Data Model and Environment Variables, please refer to the [Database-entity-details](./design/Database-entity-details.md).

---

## 5. Out of Scope

The following are explicitly out of scope for this PRD:

* Scoring Quality Gates calculations
* Student data of any kind
* Direct SEA portal access or automated fetching from SEA systems
* Vendor-facing score exposure (internal Ed-Fi staff use only in this phase)
* Natural language query interface (separate capability in the broader initiative)
* Cluster analysis across states (separate capability)
* Human Review processes (JTBD 14)
  