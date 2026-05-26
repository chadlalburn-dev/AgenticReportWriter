# Research Report Generation Agent — GCP Architecture Plan

## Context

Clinical/pharma/regulatory teams (medical writers, regulatory affairs, scientists) spend significant time hand-assembling research reports — Investigator's Brochures, Clinical Study Reports, regulatory submissions, preclinical study reports — by pulling data from EDC/LIMS systems, prior reports, study databases, and internal documents into approved templates. This is repetitive, error-prone, and slow.

This plan designs a GCP-hosted AI agent that ingests source data from heterogeneous systems, populates a structured template (or proposes one if none exists), and produces a draft report as a Google Doc for collaborative review, with Word and PDF as final delivery formats. Every fact in the generated report is cited back to its source. The system supports three selectable compliance postures so the same platform can produce both internal R&D drafts and (eventually) Part 11-validated regulated outputs.

**Pilot target:** 1 report type (Investigator's Brochure), 1 team, ~3 months — to prove end-to-end value before expanding to CSR/regulatory submissions.

---

## Confirmed Scope

| Dimension | Decision |
|---|---|
| Domain | Clinical/pharma (CSR, IB, protocols), regulatory submissions, preclinical |
| MVP report type | **Investigator's Brochure (ICH E6)** |
| Data sources | Structured DBs (BQ/CloudSQL/Postgres), unstructured files (PDF/DOCX/XLSX), cloud storage (GCS/S3/SharePoint), APIs/LIMS/EDC (Veeva, Medidata Rave, LabVantage) |
| Working doc | **Google Docs** (collaborative review with comments/suggestions) |
| Final delivery | Word `.docx` + PDF |
| GCP stack | Vertex AI + Cloud Run + GCS + Firestore (+ Cloud Workflows, Pub/Sub, Document AI, DLP, KMS, IAP) |
| LLM | Claude (Sonnet 4.6 / Opus 4.7) on Vertex AI Model Garden |
| Tenancy | Single org, multiple teams with RBAC |
| Auth | Google Workspace SSO via IAP |
| Template authoring | Hybrid JSON schema, rendered to Docs/Word/PDF |
| Compliance modes | Selectable per report type: R&D / GxP-aware / Validated. **Pilot ships R&D + GxP-aware operational; Validated implemented but unvalidated (CSV deferred).** |
| Citations | Every fact cited to source (PDF page, DB row, query, system of record) |
| Review workflow | Template approval (once) → agent draft → human review in Google Docs → export |
| Sign-off | Configurable per compliance level (in-app for R&D/GxP; full Part 11 e-sig in Validated) |
| PHI handling | **De-identify before LLM via Cloud DLP + custom rules; encrypted mapping table for re-identification by authorized roles** |
| SQL safety | LLM can draft SQL; gated by linter + dry-run + per-run human approval before execution |
| Vector store | Vertex AI Vector Search; retrieval interface designed to be swappable to AlloyDB pgvector later |

---

## System Architecture

### Cloud Run service decomposition (7 services, MVP)

1. **api-gateway** — single ingress behind IAP; auth + RBAC enforcement; thin fan-out.
2. **ingestion-service** — connector framework (BQ, CloudSQL, GCS, S3, SharePoint, Veeva, Medidata, LabVantage). Writes raw to GCS landing, emits Pub/Sub events.
3. **parsing-service** — Pub/Sub-driven. Document AI for complex PDFs; python-docx/openpyxl for native DOCX/XLSX. Structure-aware chunking + Vertex embeddings.
4. **template-service** — CRUD on JSON-schema templates. Versioning, approval gating, library of industry-standard templates (ICH E6 IB, ICH E3 CSR, CONSORT).
5. **generation-orchestrator** — driven by Cloud Workflows for durable section-by-section orchestration. Plan → Fill → Critique loop calling Claude on Vertex.
6. **citation-service** — owns the evidence ledger. Every claim → source span. Standalone because it's the audit cornerstone and gets heavy under Part 11 mode.
7. **document-renderer** — JSON + LLM outputs → Google Docs via Docs API `batchUpdate`. Drive export to `.docx`/PDF.

**Why split this way:** generation, citation, and rendering have different scaling shapes (LLM-bound, write-heavy, CPU-bound) and different compliance blast radii. Citation is isolated so Part 11 hashing/signing never couples to LLM iteration speed.

### Data stores

- **Firestore (Native, regional, CMEK):** operational state — `templates`, `report_runs`, `report_runs/{id}/sections`, `report_runs/{id}/citations`, `audit_events` (append-only), `teams`, `users`, `connectors` (secrets by Secret Manager reference only).
- **GCS, bucket-per-purpose, CMEK on all:**
  - `ib-sources-raw-{env}` (landing, object versioning ON)
  - `ib-sources-normalized-{env}` (parsed chunks)
  - `ib-artifacts-{env}` (generated drafts, exports)
  - `ib-audit-{env}` (**Bucket Lock + retention policy**; 90d/7y/10y by mode)
  - `ib-tmp-{env}` (7-day lifecycle delete)
- **BigQuery:** `analytics_ops` dataset (system telemetry) and `analytics_source` dataset (read-only federated access to clinical warehouses).
- **Vertex AI Vector Search:** one index for chunked source documents. Citation metadata in every record (`source_uri`, `doc_version`, `page/sheet/cell`, `chunk_hash`).
- **Secret Manager:** per-connector credentials, CMEK, 90d rotation.
- *Deliberately NOT in MVP:* AlloyDB, Cloud SQL, Cloud Composer. Add only when justified.

### Identity & access

- **End-user auth:** Google Workspace SSO via IAP fronting api-gateway.
- **RBAC:** Cloud Identity groups → Firestore role records (`viewer`, `author`, `reviewer`, `approver`, `template-admin`, `compliance-admin`). Enforced at gateway and re-checked in each downstream service.
- **Google Docs/Drive access:** dedicated service account with **domain-wide delegation** scoped to `drive.file` + `documents`. (Requires Workspace super-admin sign-off — flagged as a pre-build blocker.)
- **External system credentials:** Secret Manager, one secret per connector instance per team.
- **Per-service identities:** one SA per Cloud Run service, workload identity, no key files.

### Networking & security baseline

- **VPC Service Controls** perimeter around GCS, Firestore, BigQuery, Vertex AI, Secret Manager.
- **CMEK everywhere** (KMS keys per data class: sources, artifacts, audit). HSM protection level for Validated tenants.
- **Private Service Connect** for Vertex AI endpoints (LLM + Vector Search).
- **Cloud Audit Logs** → log sink → `ib-audit-{env}` bucket (Bucket Lock).

---

## Generation Pipeline

### Template JSON schema (the centerpiece)

A template is a versioned JSON document describing report structure, generation instructions, and data contract. Two-level model: **Template** (reusable, approved, immutable once approved) → **ReportInstance** (populated, links to `template_id + version`).

Section node (recursive):
```
{
  section_id, title, level, children: [...],
  generation: {
    mode: "llm" | "deterministic" | "manual" | "hybrid",
    prompt_template, expected_length, style_directives,
    output_schema (prose | table | list)
  },
  data_bindings: [
    {type: "sql_query" | "file_set" | "file_ref" | "computed_metric" | "free_text_input" | "api_call", ...}
  ],
  citation_policy: {required, granularity, min_citations_per_paragraph},
  validation_rules: [{rule, severity}]
}
```

Ships with an **IB skeleton** matching ICH E6: Title Page → Confidentiality → Summary → 1. Introduction → 2. Physical/Chemical/Pharmaceutical Properties → 3. Nonclinical Studies (3.1 Pharmacology, 3.2 PK/Metabolism, 3.3 Toxicology) → 4. Effects in Humans (4.1 PK, 4.2 Safety/Efficacy, 4.3 Marketing Experience) → 5. Summary of Data and Guidance → Appendices.

### Template Builder (unified workflow, four entry adapters)

- **From existing Word `.docx`:** `python-docx` walks the document tree (Heading 1/2/3 → section hierarchy, tables, fields). LLM enriches each section with draft `prompt_template` and `data_bindings`. Document AI only for scanned/visually complex documents.
- **From sample reports:** LLM analyzes 3–5 completed reports to derive common structure and per-section conventions.
- **From industry standards:** shipped library (ICH E6 IB, ICH E3 CSR, CONSORT) the user selects and customizes.
- **From scratch:** agent asks scoping questions, proposes skeleton, iterates.

All four converge on a Template Builder UI: review proposed schema, edit prompts/bindings, attach reference docs, run a dry-run generation, submit for approval. **Approval is gated; only approved templates can generate delivery reports.**

### Ingestion & indexing

- **PHI/PII redaction (Cloud DLP + custom rules)** runs immediately after parsing and before embedding/LLM. Original-to-redacted mapping stored encrypted in Firestore, accessible only to authorized roles.
- **Unstructured documents:** structure-aware chunking preserving page/heading/sheet metadata for citations. Chunk size 300–600 tokens, 50-token overlap. Hash-based dedup; new doc with different hash → new `doc_version` (prior marked superseded).
- **Structured DBs:** two-tier — (a) **semantic catalog** of tables/columns as embeddings so the agent can discover relevant data; (b) **named query registry** of pre-approved parameterized queries the agent prefers. **Per user direction:** LLM may also draft novel SQL; it passes through a SQL linter + dry-run against a read-replica + human approval modal before execution against any production source.
- **LIMS/EDC connectors** implement a `Connector` interface (`authenticate`, `list_resources`, `fetch`, `normalize → CanonicalDocument`). Canonical document is the integration contract — every connector outputs the same shape with `source_system`, `source_id`, `effective_date`, `system_of_record_url`, `retrieval_timestamp`.

### Generation orchestration — three-phase loop

1. **Plan phase (Claude Opus 4.7, once per run):** outline what each section will assert; identify cross-references and shared facts. Output: structured plan JSON.
2. **Fill phase (Claude Sonnet 4.6, per section):** RAG retrieves top-k chunks from bound `file_set`s, executes bound `sql_query`s, calls bound APIs. Context = section prompt + plan excerpt + retrieved chunks (each tagged with `citation_id`) + structured data results. **Required structured JSON output:** `{paragraphs: [{text, claims: [{text, citation_ids: [...]}]}], tables: [...]}`.
3. **Critique phase (Claude Opus 4.7, per section):** self-review — every numeric claim cited, no hallucinated sources, length within bounds, style matches directives. Up to 3 regenerations; then escalate to human queue.

**Tables/figures/structured data are pulled deterministically, never LLM-generated.** AE listings, demographics, lab summaries come from SQL or named-query results and are rendered directly. LLM narrates around them. This is the single biggest hallucination-prevention lever.

### Citation tracking

Citation record:
```
citation_id (UUID), report_instance_id,
source_type ("pdf"|"docx"|"xlsx"|"sql"|"api"),
source_uri, source_version,
locator: {page?, paragraph?, sheet?, cell_range?, query_id?, row_filter?, api_endpoint?},
snippet, retrieved_at, retrieval_chunk_id
```

Inline format in LLM output is `[[cite:UUID]]`. Renderer transforms per output:
- **Google Docs:** numbered footnotes with live hyperlinks (Drive link for files, internal link to a "Sources" appendix for DB-derived citations).
- **Word/PDF:** native footnotes preserved through Drive export.

**Verification pass:** per-claim Claude check ("does this snippet support this claim?") + embedding-similarity sanity check. Failures route to a human review queue. Cached on `(claim_hash, citation_id)` to avoid double-paying on regeneration.

### Rendering

- **JSON + LLM outputs → Google Docs** via Docs API `batchUpdate` (InsertText, UpdateParagraphStyle for Heading levels, InsertTable, CreateFootnote, CreateNamedRange for each section).
- **Google Docs → Word `.docx`:** Drive API `files.export`.
- **Google Docs / Word → PDF:** Drive API export. Upgrade to server-side LibreOffice headless only if regulatory formatting demands it.

---

## Compliance Modes — Implementation Matrix

| Dimension | R&D | GxP-aware | Validated (Part 11) |
|---|---|---|---|
| Pilot status | **Operational** | **Operational** | **Implemented, not validated (CSV deferred)** |
| Audit events | Login, generation request, export | All R&D + edits, ingestion, comments, citations, template version | All GxP + signature events, re-auth, config changes, **every LLM call** (prompt/response/model/temp/seed/retrieval IDs) |
| Audit storage | Firestore, 90-day TTL | Firestore + GCS versioned, 7y | Firestore + GCS **Bucket Lock**, 10y, BigQuery sink, hash-chain w/ KMS-signed Merkle root |
| Identity at sign | Session token | Session + recorded attribution | **Step-up re-auth** (password + 2nd factor) — §11.200(a)(1) |
| Reason for signing | N/A | Optional | Required, controlled vocab |
| Sign-off | "Approve" button | Approve + comment + audit | Full Part 11 e-sig: re-auth + reason + KMS-signed binding to document hash |
| Workflow | Optional | Author → Reviewer | Author → Reviewer → QA → Approver, enforced |
| Export | Word, PDF watermarked "DRAFT — R&D" | Word, PDF watermarked "DRAFT — Not for submission" | Clean Word/PDF with signature page + audit appendix on demand |
| Template controls | User-modifiable | Versioned, reviewer-controlled | Validated artifact under change control |
| LLM determinism | Not required | Encouraged | **Required:** model version pinned, temp=0, seed fixed, retrieval snapshot ID stored |

**Mode is set at project creation and is immutable.** Mode change = new project + regeneration (preserves audit clarity).

**Validated mode notes:** the architecture supports it from day one (Bucket Lock, hash-chain, e-sig flow), but **operating** in Validated mode requires a completed CSV exercise (URS, FS, DS, IQ/OQ/PQ, SOPs, training records) and supplier assessment of Google Cloud / Anthropic. Pilot does not include CSV; it is a separate follow-on workstream.

---

## Critical Files (created at implementation start)

This is a greenfield project. The first files to create:

- `infra/terraform/main.tf` — root Terraform: GCP project, VPC-SC, KMS, IAP, Cloud Run services, GCS buckets w/ Bucket Lock, Vertex AI endpoints.
- `shared/schemas/canonical_document.py` — canonical document shape shared by ingestion, parsing, citation, generation.
- `shared/schemas/template_schema.json` — JSON Schema for templates (validated on save).
- `templates/library/ich_e6_ib.json` — shipped IB template skeleton.
- `services/ingestion-service/src/connectors/base.py` — `Connector` interface for Veeva/Medidata/LabVantage/SharePoint/S3/GCS implementations.
- `services/parsing-service/src/redactor.py` — Cloud DLP + custom-rule PHI redaction, with mapping-table emission.
- `services/template-service/src/builder.py` — four-adapter Template Builder (Word, samples, library, scratch).
- `services/generation-orchestrator/src/workflow.yaml` — Cloud Workflows definition for the plan→fill→critique loop.
- `services/generation-orchestrator/src/section_generator.py` — per-section Claude orchestration (Sonnet for fill, Opus for plan/critique).
- `services/generation-orchestrator/src/sql_safety.py` — linter + dry-run + approval-modal flow for LLM-drafted SQL.
- `services/citation-service/src/schema.py` — citation record + evidence-ledger model.
- `services/citation-service/src/verifier.py` — per-claim verification pass (LLM + embedding-similarity).
- `services/document-renderer/src/gdocs_renderer.py` — Docs API `batchUpdate` + Drive export to `.docx`/PDF.
- `services/audit/src/hash_chain.py` — Merkle/hash-chain implementation with KMS signing (Validated mode).
- `services/policy/src/mode_matrix.py` — single source of truth for compliance-mode behavior (gates exports, templates, signing, retention).

---

## Verification Plan

**Pilot success criteria** (3-month exit):

1. **Template authoring works for all four entry points.** Demonstrate: import an existing Word IB template, generate from ICH E6 library, derive from 3 sample IBs, generate from scratch. All produce valid JSON schemas usable for generation.
2. **End-to-end IB generation on real data.** Pilot team selects one compound, ingests its source corpus (prior reports, nonclinical study data from EDC, PK results from LIMS). Agent produces a draft IB in Google Docs.
3. **Citation discipline.** ≥99% of factual claims (counts, dates, study IDs, results) carry a valid citation. Verification pass identifies any uncited or mismatched claims before reviewer hands off.
4. **Reviewer workflow.** Medical writer reviews the Google Doc, makes edits/comments, approves. System exports clean Word + PDF for delivery.
5. **Audit completeness (GxP-aware mode).** Replay any generation run from audit log: who, what, when, which template version, which sources, which LLM calls, all citations. End-to-end traceable.
6. **PHI redaction verified.** Synthetic-PHI test set passed through ingestion; confirm no PHI reaches Vertex AI; mapping table allows authorized re-identification.
7. **SQL safety gate verified.** LLM-drafted SQL is linted, dry-run, and blocks on human approval before execution against any production source.
8. **Cost envelope held.** Target ≤$8K/month at pilot scale (5–10 IB drafts, 50–200 sources each); monitor via `analytics_ops` BigQuery dataset.

**How to test end-to-end:**

- Stand up the GCP project via Terraform; deploy all 7 Cloud Run services.
- Load the ICH E6 IB template into the library.
- Run a synthetic-PHI test corpus (≈20 fake study documents + a small BQ dataset) through ingestion. Verify redaction, citation metadata, and vector index population.
- Trigger an IB generation run via api-gateway with the synthetic compound. Inspect the Google Doc draft; confirm structure matches the template, every claim carries a citation, tables render from deterministic data, and the audit log records every LLM call.
- Repeat with the pilot team's first real compound.
- Run OQ-style checklist against the compliance-mode matrix to confirm each mode's gating behavior (R&D blocks submission exports, GxP-aware watermarks drafts, Validated path requires step-up re-auth at sign).

---

## Pre-Build Blockers Requiring Stakeholder Action

These are decisions/approvals that block kickoff, not engineering questions:

1. **Workspace super-admin approval** for domain-wide delegation on the `docs-renderer-sa` service account, scoped to `drive.file` + `documents`. Without this, no Google Docs path.
2. **Google Vertex AI BAA confirmation** covering Claude endpoints in the target region — needed even with the de-identify-first design, as a defense-in-depth posture.
3. **GCP project provisioning** with org-level approval for VPC-SC perimeter, CMEK keyring, and Bucket Lock policy.
4. **Pilot team and compound selected**, with named medical-writer reviewer and identified source corpus (prior studies, LIMS/EDC access scope).
5. **Validated-mode CSV scope decision** for the follow-on workstream (which report types' templates become validated artifacts; budget for IQ/OQ/PQ and golden-set regression test suite).
