# User Requirements Specification (URS)

**System:** Report Generator Agent
**Document version:** 0.1.0 (draft)
**Status:** DRAFT — requires QA / regulatory review
**Authored from:** codebase state at commit `147aeae`

## 1. Purpose

The Report Generator Agent is an AI-assisted authoring system for
clinical/pharma research reports (Investigator's Brochures, Clinical
Study Reports, regulatory submissions, preclinical reports). It
ingests source data from heterogeneous systems, populates a structured
template with the data, and produces a draft for medical writer review.

This URS captures user-level requirements; the FS describes how they
are met; the traceability matrix links each to a test.

## 2. Intended users

| Role | Use |
|---|---|
| Medical writer | Reviews and finalizes generated drafts; authors templates |
| Regulatory affairs | Reviews submission-ready outputs |
| QA reviewer | Audits the audit chain + anchors; confirms compliance posture |
| Compliance admin | Configures compliance modes; manages template change control |
| Pilot team lead | Selects compounds + source corpora for runs |

## 3. Functional requirements

Each requirement has a unique ID (`URS-XXX`). The traceability matrix
links each to its corresponding FS item, code module, and test.

### Template authoring

- **URS-001** The system shall accept a template either (a) imported
  from an existing Word .docx file, (b) loaded from a shipped library
  of industry-standard templates (e.g., ICH E6, ICH E3, CONSORT), (c)
  derived from a set of completed reports, or (d) proposed by an LLM
  from a scoping spec.
- **URS-002** Approved templates shall be immutable. Modifications
  shall produce a new version under change control.
- **URS-003** Each template section shall declare its citation policy
  (required/optional, granularity), data bindings, and validation
  rules.

### Ingestion

- **URS-010** The system shall ingest documents from local file
  systems, GCS, S3, and SharePoint (via connector pattern).
- **URS-011** The system shall extract page/heading/cell-range
  metadata from PDF, DOCX, and XLSX documents so citations can deep-
  link to the source location.
- **URS-012** Source documents shall be deduplicated by content hash;
  re-ingest of identical bytes shall not produce duplicate records.

### Generation

- **URS-020** The system shall generate report sections via a
  three-phase loop: plan (outline) → fill (per-section prose) →
  critique (QA review).
- **URS-021** Every numeric or otherwise quantitative claim in a
  generated section shall carry at least one citation linking to its
  source.
- **URS-022** Citations that reference IDs not present in the supplied
  source pool (fabrications) shall be rejected before the section is
  accepted.
- **URS-023** Tables shall be populated from deterministic data
  sources (named queries or APIs); the LLM shall not re-derive
  numerical table contents.

### Data integration safety

- **URS-030** SQL queries against production data shall be limited to
  pre-approved named queries OR LLM-drafted SQL that has passed a
  linter + dry-run + human approval gate.
- **URS-031** Forbidden SQL patterns (DDL, multi-statement, DML write
  operations, unbounded DELETE/UPDATE) shall be rejected before
  reaching any database.
- **URS-032** API calls to external services shall be limited to a
  per-connector allow-list of operations.

### Audit trail

- **URS-040** The system shall record an immutable audit event for
  every:
  template creation/approval/retirement; source ingestion; generation
  request; plan/fill/critique phase; LLM call; citation creation;
  section edit; reviewer comment; signature; mode lock; export.
- **URS-041** Audit events shall be linked in a per-project hash chain
  such that any tampering breaks the chain at the first mutated
  event.
- **URS-042** In Validated mode, daily Merkle roots over the audit
  chain shall be signed by a Cloud KMS HSM-backed asymmetric key.
- **URS-043** Audit events shall be queryable by project, actor,
  action, and time window.

### Compliance modes

- **URS-050** The system shall support three compliance modes (R&D,
  GxP-aware, Validated) selectable per report-project.
- **URS-051** Mode shall be set at project creation and shall be
  immutable thereafter.
- **URS-052** Validated mode shall require: pinned model version;
  temperature ≤ 0.1; full LLM-call logging; step-up re-authentication
  at signature; signed Merkle anchor; signed exports.

### Review + sign-off

- **URS-060** Generated drafts shall flow into a Google Doc for
  collaborative reviewer markup before export.
- **URS-061** Reviewers shall be able to comment, suggest edits, and
  approve sections.
- **URS-062** In Validated mode, signature shall require step-up re-
  authentication (Part 11 §11.200(a)(1)) and a controlled-vocabulary
  reason for signing.

### Export

- **URS-070** Approved reports shall be exportable as .docx and PDF.
- **URS-071** Exports shall record an EXPORT_PERFORMED audit event
  linked to the source ReportInstance.

### Privacy

- **URS-080** PHI/PII in source documents shall be de-identified
  before being sent to the LLM. A mapping table shall allow
  authorized roles to re-identify post-generation.
- **URS-081** ANTHROPIC_API_KEY-style static credentials shall NOT be
  used. Authentication shall be via Application Default Credentials
  (workload identity in Cloud Run; gcloud ADC locally).

## 4. Non-functional requirements

- **URS-090** The system shall run as a set of containerized services
  on GCP Cloud Run.
- **URS-091** Cost per IB run at pilot scale (5–10 IB drafts/month, 50–
  200 source docs per IB) shall not exceed $8K/month at list pricing.
- **URS-092** End-to-end generation of one IB shall complete within
  15 minutes for a typical 50-document source corpus.
- **URS-093** The audit ledger shall retain events for 10 years in
  Validated mode (Bucket Lock + retention policy).

## 5. Out of scope (initial release)

- Multi-tenant SaaS (one GCP project, multiple teams via RBAC for now)
- Direct integration with external e-signature vendors (DocuSign etc.)
- Translation / localization of generated text
- Generation of full Investigator's Brochure final-form deliverables
  (the system produces drafts; human review and finalization are
  required)

## 6. Approval

| Role | Name | Signature | Date |
|---|---|---|---|
| Business owner | _TBD_ | | |
| QA representative | _TBD_ | | |
| Compliance | _TBD_ | | |
| Technical owner | Chad Alburn | | |
