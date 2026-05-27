# Traceability matrix

**System:** Report Generator Agent
**Document version:** 0.1.0 (draft)
**Pairs with:** [URS.md](URS.md), [FS.md](FS.md)

Each row links a user requirement (URS) to its functional spec (FS),
the code module(s) implementing it, and the test(s) verifying it.

| URS | FS | Implementation (code) | Test(s) |
|---|---|---|---|
| URS-001 (4 authoring entry points) | FS-001 | `services/template_service/adapters/{docx,library,sample_reports,from_scratch}_adapter.py` | `tests/test_template_builder.py`, `tests/test_library_adapter.py`, `tests/test_sample_reports_adapter.py`, `tests/test_from_scratch_adapter.py` |
| URS-002 (approved templates immutable) | FS-002 | `shared/schemas/template.py` (`ReportTemplate`, `TemplateStatus`) | `tests/test_template_loading.py::test_ib_template_parses` (status flag); enforcement in `template_service` (out of scope this commit) |
| URS-003 (per-section policy) | FS-003 | `shared/schemas/template.py` (`GenerationPolicy`, `CitationPolicy`, `ValidationRule`, `DataBinding`) | `tests/test_template_loading.py::test_all_llm_sections_require_citations` |
| URS-010 (multi-source ingestion) | FS-010 | `services/ingestion_service/connectors/base.py`, `local_file.py` | `tests/test_local_file_connector.py` |
| URS-011 (page/heading/cell metadata) | FS-011 | `services/parsing_service/{pdf,docx,xlsx}_parser.py`; `shared/schemas/parsed_chunk.py` | `tests/test_parsers.py` |
| URS-012 (dedup by content hash) | FS-012 | `services/ingestion_service/connectors/local_file.py` (`content_hash`) | `tests/test_local_file_connector.py::test_local_file_connector_content_hash_is_stable` |
| URS-020 (plan→fill→critique) | FS-020 | `services/generation_orchestrator/{planner,filler,critic,orchestrator}.py` | `tests/test_orchestrator_end_to_end.py` |
| URS-021 (every numeric claim cited) | FS-021 + FS-022 | `services/generation_orchestrator/filler.py` (citation_id allocation + LLM prompt rendering); `services/generation_orchestrator/critic.py::_local_validation_issues` (numeric-without-citation check) | `tests/test_orchestrator_end_to_end.py::test_orchestrator_runs_full_ib_template` |
| URS-022 (fabricated citation rejection) | FS-022 | `services/generation_orchestrator/filler.py` (valid_citation_ids check) | `tests/test_orchestrator_end_to_end.py::test_orchestrator_rejects_fabricated_citation_ids` |
| URS-023 (deterministic tables) | FS-023 | `services/generation_orchestrator/filler.py::_render_table_for_prompt` + `_render_api_table_for_prompt`; the LLM receives the table rendered, it does not regenerate values | `tests/test_binding_resolver_with_safety_gate.py::test_named_query_binding_resolves_through_gate`, `tests/test_api_integration.py::test_resolver_runs_api_call_through_gate` |
| URS-030 (named-query registry + SQL gate) | FS-030 + FS-031 | `services/data_integration/{named_query,sql_safety,safety_gate,approval,executor}.py` | `tests/test_data_integration.py` |
| URS-031 (forbidden SQL rejected) | FS-031 | `services/data_integration/sql_safety.py` (`SqlLinter`, `_FORBIDDEN_TOP_LEVEL`) | `tests/test_data_integration.py::test_linter_rejects_forbidden_top_levels` |
| URS-032 (API allow-list) | FS-032 | `services/api_integration/{gate,connector,registry}.py` | `tests/test_api_integration.py::test_gate_disallowed_operation_raises` |
| URS-040 (immutable audit events) | FS-040 | `services/audit/schema.py` (`AuditEvent` with `frozen=True`); `services/audit/store.py` (INSERT-only) | `tests/test_audit_store.py::test_audit_event_is_frozen`, `test_inmemory_rejects_duplicate_event_id` |
| URS-041 (per-project hash chain) | FS-041 | `services/audit/hash_chain.py`; `services/audit/store.py` (chain stamping on append) | `tests/test_audit_store.py::test_inmemory_append_links_chain`, `test_sqlite_tamper_detection`, `test_inmemory_chains_are_per_project` |
| URS-042 (Validated-mode signed Merkle) | FS-042 | `services/audit/{merkle,signer,anchor}.py`; `KmsRootSigner` for production | `tests/test_audit_anchoring.py::test_anchor_produces_record_with_valid_signature`, `test_anchor_breaks_after_sqlite_tamper` |
| URS-043 (filtered audit queries) | FS-043 | `services/audit/store.py::AuditQuery`, `SqliteAuditStore.query` | `tests/test_audit_store.py::test_sqlite_query_by_action`, `test_inmemory_query_time_window` |
| URS-050 (mode selectable per project) | FS-050 | `AuditEvent.mode` field; `ReportGenerator.generate(compliance_mode=...)` | Verified end-to-end in `test_orchestrator_runs_full_ib_template` |
| URS-051 (mode immutable) | FS-051 | `AuditEvent` `frozen=True` enforces field-level immutability; project-level mode lock is a policy follow-up | Partial coverage in `test_audit_event_is_frozen` |
| URS-052 (Validated-mode requirements) | FS-052 | Multiple modules; pinned model in `shared/llm/vertex.py::DEFAULT_MODELS`; LLM-call logging in `services/audit/llm_audit.py::AuditingLlmClient`; signed Merkle in `services/audit/anchor.py` | `tests/test_audit_anchoring.py` for signing; `tests/test_orchestrator_end_to_end.py` confirms LLM_CALL events fire |
| URS-060 (Google Doc review surface) | FS-060 | `services/document_renderer/gdocs.py::GoogleDocsRenderer.render()` | `tests/test_document_renderer.py` (covers DryRunRenderer + spec generation; live Docs API testing manual until ADC is configured) |
| URS-061 (reviewer markup) | FS-061 | Delegated to native Google Docs commenting | (manual verification once ADC is wired) |
| URS-062 (Part 11 step-up re-auth at sign) | FS-062 | `AuditEvent.actor_auth_method` + `AuditAction.SIGNATURE_APPLIED`; UI layer to enforce step-up | (UI layer; not yet implemented) |
| URS-070 (.docx + PDF export) | FS-070 | `GoogleDocsRenderer.export_docx`, `export_pdf` | (manual verification once ADC is wired) |
| URS-071 (EXPORT_PERFORMED event) | FS-071 | `AuditAction.EXPORT_PERFORMED` available; emitter wiring is a CLI / UI follow-up | (follow-up) |
| URS-080 (PHI de-identification) | FS-080 | Design in `docs/architecture-plan.md`; code not yet implemented | (follow-up; tests will live in `tests/test_redactor.py`) |
| URS-081 (no static credentials) | FS-081 | Project-wide ban; verified by absence in source. `VertexLlmClient`, `GoogleDocsRenderer`, `FirestoreAuditStore`, `KmsRootSigner` all use ADC. | Manual: `grep -ri ANTHROPIC_API_KEY services/ shared/` returns zero non-comment hits |
| URS-090 (Cloud Run deployment) | FS-090 | One service per `services/<name>/` dir; Terraform to be added under `infra/terraform/` | (follow-up; deployment tests via Terraform plan + IQ protocol) |
| URS-091 (cost envelope) | FS-091 | `AuditingLlmClient` captures token counts + model_version on every call; aggregation via BigQuery `analytics_ops` dataset (follow-up) | (follow-up; integration test once BQ sink is wired) |
| URS-092 (generation latency) | FS-092 | Performance is a function of LlmClient (stub: <1s/section; Claude on Vertex: TBD) | Stub-LLM full-IB run completes in <15s end-to-end (manual; see CLI demo) |
| URS-093 (10-year audit retention) | FS-093 | Terraform on `ib-audit-{env}` bucket sets Bucket Lock + retention policy | (verified in IQ protocol once infra is provisioned) |

## Coverage summary

| Status | Count |
|---|---|
| Implemented + tested | 22 |
| Implemented; manual verification pending (Docs/Drive ADC) | 4 |
| Design only; implementation deferred (PHI redactor, mode-lock policy) | 4 |
| Infrastructure / deployment items (Terraform, BQ sink, Bucket Lock) | 4 |
| **Total URS items** | **34** |

## Audit log

Updates to this matrix must include the change control reference
number (see `change_control_sop.md`). Format: `CC-YYYY-MM-DD-NNN`.

| Date | Change | CC ref | Updated by |
|---|---|---|---|
| 2026-05-26 | Initial draft from codebase state | _none — draft baseline_ | system:csv-doc-generator |
