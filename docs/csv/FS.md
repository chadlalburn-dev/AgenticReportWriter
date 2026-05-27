# Functional Specification (FS)

**System:** Report Generator Agent
**Document version:** 0.1.0 (draft)
**Status:** DRAFT — requires QA / regulatory review
**Pairs with:** [URS.md](URS.md)

This FS describes **how** the system meets each requirement listed in
the URS. Each FS item has an ID (`FS-XXX`) that matches the
corresponding URS item.

## Architecture overview

Seven Cloud Run services described in the architecture plan:

1. `api-gateway` — IAP-fronted ingress; auth + RBAC
2. `ingestion-service` — connectors for local file, GCS, S3, SharePoint,
   Veeva, Medidata, LabVantage
3. `parsing-service` — Document AI + python-docx + openpyxl + pypdf
4. `template-service` — ReportTemplate CRUD; four authoring adapters
5. `generation-orchestrator` — plan→fill→critique loop
6. `citation-service` — evidence ledger
7. `document-renderer` — Google Docs API + Drive export

Cross-cutting services:
- `audit/` — immutable audit ledger (SQLite/Firestore) + hash chain
  + Merkle anchoring
- `data_integration/` — named-query registry + SQL safety gate
- `api_integration/` — connector registry + ApiCallGate
- `policy/` — compliance mode matrix (placeholder for follow-on work)

## Functional items

### Template authoring (URS-001 to URS-003)

- **FS-001** The `template-service` exposes four authoring entry
  points via `TemplateBuilder`:
  - `from_docx(path)` — walks a Word doc's heading hierarchy
    (Heading 1..6 styles) and produces a draft template
    (see `DocxAdapter`)
  - `from_library(template_id)` — loads `templates/library/<id>.json`;
    optional clone with new id (see `LibraryAdapter`)
  - `from_samples(directory)` — derives a template from common
    sections across multiple completed reports (see
    `SampleReportsAdapter`)
  - `from_scratch(spec, client)` — LLM-proposed outline from a
    scoping spec (see `FromScratchAdapter`)
- **FS-002** ReportTemplates are versioned (`version` field) and
  carry a `status` field. Once `status == APPROVED`, no in-place
  changes are permitted (enforced by the `template-service` and the
  Validated-mode policy).
- **FS-003** Each `TemplateSection` carries:
  - `generation: GenerationPolicy` (mode, prompt, length bounds)
  - `data_bindings: list[DataBinding]` (file_set, file_ref, named_query,
    sql_query, computed_metric, free_text_input, api_call)
  - `citation_policy: CitationPolicy` (required, granularity,
    min_citations_per_paragraph)
  - `validation_rules: list[ValidationRule]`

### Ingestion (URS-010 to URS-012)

- **FS-010** `services/ingestion_service/connectors/base.py` defines
  the `Connector` protocol. `local_file.py` implements the local
  filesystem connector; GCS / S3 / SharePoint / Veeva / Medidata /
  LabVantage connectors will be added under the same protocol.
- **FS-011** `services/parsing_service/` parses PDF (pypdf), DOCX
  (python-docx), XLSX (openpyxl) into `ParsedChunk` objects, each
  carrying a typed locator (`PdfLocator`, `DocxLocator`,
  `XlsxLocator`).
- **FS-012** `CanonicalDocument.content_hash` is the SHA-256 of the
  document bytes; ingestion is idempotent on identical bytes.

### Generation (URS-020 to URS-023)

- **FS-020** `ReportGenerator.generate()` runs three phases:
  1. `ReportPlanner.plan()` — Opus tier; produces an outline per
     section_id
  2. `SectionFiller.fill()` — Sonnet tier per section; RAG over the
     resolved chunks + queries + APIs
  3. `SectionCritic.critique()` — Opus tier; combines local
     validation (numeric-claim-cited, length bounds) with LLM review
- **FS-021** Each filler call's prompt includes the chunks AND query
  tables AND API tables, each labeled with a `[citation_id=XXX]`
  marker. The LLM is required to use those citation_ids in its
  output.
- **FS-022** `SectionFiller` validates that every citation_id the LLM
  returned is in the union of `chunk_to_citation` + `table_to_citation`
  values. Unknown IDs raise `StructuredOutputError` and the section
  fails (triggering a regenerate).
- **FS-023** Tables from `named_query`/`sql_query`/`api_call`
  bindings are pulled deterministically and rendered into the prompt
  as markdown tables; the LLM narrates around them but cannot
  re-derive their numerical content.

### Data integration safety (URS-030 to URS-032)

- **FS-030** Named queries live as YAML files under
  `templates/library/queries/` (or per-program directories). Each
  query carries an `ApprovalRecord` (`approved_by`, `approved_at`,
  change log).
- **FS-031** `SqlLinter` (sqlglot-based) rejects: multi-statement,
  Create/Drop/Alter/Truncate/Insert/Update/Delete/Merge/Pragma top-
  level, SELECT INTO. LLM-drafted SQL goes through lint → dry-run →
  approval callback. The default callback is `deny_all` (fail
  closed).
- **FS-032** `ApiCallGate` enforces per-connector allow-listed
  operations; unknown connectors raise `UNKNOWN_CONNECTOR`,
  disallowed operations raise `OPERATION_NOT_ALLOWED`.

### Audit trail (URS-040 to URS-043)

- **FS-040** `AuditEvent` (`services/audit/schema.py`) captures all
  required fields. `AuditAction` enum lists every observable
  transition.
- **FS-041** `AuditStore` implementations (`InMemoryAuditStore`,
  `SqliteAuditStore`, `FirestoreAuditStore`) compute and stamp
  `prev_event_hash` + `this_event_hash` on every `append()`.
  `verify_chain()` walks a sequence and raises
  `HashChainViolation` on the first mismatch.
- **FS-042** `Anchorer.anchor(period)` walks the chain, verifies it,
  computes a Merkle root over the per-event chain hashes, and signs
  it with a `RootSigner`. `LocalRsaSigner` (test/dev) uses
  cryptography library RSA-3072 PSS; `KmsRootSigner` uses Cloud KMS
  `asymmetric_sign`.
- **FS-043** `AuditStore.query(AuditQuery(...))` supports filters by
  `project_id`, `tenant_id`, `actor_id`, `action`, `since`, `until`,
  `limit`.

### Compliance modes (URS-050 to URS-052)

- **FS-050** `AuditEvent.mode` is `Literal["rd","gxp","part11"]`.
  `compliance_mode` is set at `ReportGenerator.generate()` time
  and stamped on every event.
- **FS-051** Mode immutability is enforced by:
  - `AuditEvent` itself being frozen (Pydantic `frozen=True`)
  - Future: `policy/mode_matrix.py` rejecting any mode change after
    `GENERATION_REQUESTED` is recorded
- **FS-052** Validated mode requirements implemented as follows:
  - Pinned model version → `VertexConfig.models` dict has explicit
    version pins (e.g., `claude-opus-4-7@20260115`)
  - temperature ≤ 0.1 → `LlmRequest.temperature` defaults to 0.0;
    Validated-mode enforcement will be added in `policy/`
  - LLM-call logging → `AuditingLlmClient` wraps every client call
  - Step-up re-auth → handled at the UI layer (not in this code
    base); the `SIGNATURE_APPLIED` event captures
    `actor_auth_method`
  - Signed Merkle anchor → `Anchorer` + `KmsRootSigner`

### Review + sign-off (URS-060 to URS-062)

- **FS-060** `services/document_renderer/gdocs.py` creates a Google
  Doc via the Docs API on `render_gdoc()`.
- **FS-061** Native Google Docs comments/suggestions provide the
  review surface. (No custom commenting layer required.)
- **FS-062** Signature flow stub: `SIGNATURE_APPLIED` audit event
  captures `actor_auth_method` + `reason`. UI layer to be added in a
  follow-up release.

### Export (URS-070 to URS-071)

- **FS-070** `GoogleDocsRenderer.export_docx()` and `export_pdf()`
  call the Drive API to export the Doc.
- **FS-071** Exports emit `EXPORT_PERFORMED` audit events via the
  `AuditSink` (UI layer; not yet wired in the CLI demo).

### Privacy (URS-080 to URS-081)

- **FS-080** PHI/PII de-identification will be implemented in
  `services/parsing_service/redactor.py` using Cloud DLP + custom
  rules. Encrypted mapping table stored in Firestore.
  **Status: not yet implemented in code; design captured in
  architecture plan.**
- **FS-081** No `ANTHROPIC_API_KEY` usage anywhere in the codebase
  (verifiable: `grep -ri ANTHROPIC_API_KEY services/ shared/` returns
  zero hits other than comments forbidding it). All authentication
  is via Application Default Credentials.

### Non-functional (URS-090 to URS-093)

- **FS-090** Each service has its own Dockerfile + Cloud Run
  deployment (Terraform to be added under `infra/terraform/`).
- **FS-091** Cost envelope is monitored via the
  `analytics_ops` BigQuery dataset (token usage, latency, run
  counts) — see architecture plan §6.
- **FS-092** Performance target met when stub-LLM run completes in
  <10 seconds for the synthetic 13-document corpus; real Claude
  latency depends on Vertex AI SLAs.
- **FS-093** Validated-mode audit retention is enforced by GCS Bucket
  Lock with a 10-year retention policy on the `ib-audit-{env}`
  bucket.

## Cross-references

- Architecture: `docs/architecture-plan.md`
- URS: `docs/csv/URS.md`
- Traceability: `docs/csv/traceability_matrix.md`
- Code: `services/`, `shared/`, `tests/`
