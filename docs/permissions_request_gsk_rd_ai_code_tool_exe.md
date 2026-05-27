# GCP permissions request — `gsk-rd-ai-code-tool-exe` (PRODUCTION TARGET)

> **Status: deferred (Path B).** The MVP is being built on
> `gsk-rd-pcs-sys-eng` first to avoid the VPC-SC perimeter overhead on
> this project. This document is the eventual production-target
> request — re-issue once the MVP is validated and VPC-SC ingress is
> sorted. The active MVP request is in
> [permissions_request_mvp_pcs_sys_eng.md](permissions_request_mvp_pcs_sys_eng.md).

**Requestor:** Chad Alburn (chad.l.alburn@gsk.com)
**System:** Report Generator Agent (AI-assisted clinical/regulatory
report generation; architecture in
[docs/architecture-plan.md](architecture-plan.md))
**Repo:** `chadlalburn-dev/AgenticReportWriter`
**Target GCP project:** `gsk-rd-ai-code-tool-exe`
**Date:** 2026-05-27

> Companion to the GSK "Claude Code: Set up GitHub and GCP project
> integration" Project. That Project covered the CI/CD-side
> integration (Workload Identity Federation for GitHub Actions →
> GCP, Artifact Registry, Cloud Build trigger). This request covers
> the **runtime + operations surface** the agent needs to actually
> generate reports inside the project.

## 1. Business context (one paragraph)

This system generates draft research reports (Investigator's
Brochures, Clinical Study Reports, regulatory submissions) from
heterogeneous source data. Pilot scope is Investigator's Brochure on
one team. The compliance posture is selectable per project (R&D /
GxP-aware / 21 CFR Part 11 Validated); the pilot will operate
R&D + GxP-aware first, with Validated-mode wiring already in place
but not formally CSV'd until a separate workstream.

## 2. APIs to enable in `gsk-rd-ai-code-tool-exe`

| API | Why |
|---|---|
| `aiplatform.googleapis.com` | Vertex AI — Claude via Model Garden (Sonnet 4.6 fill, Opus 4.7 plan/critique). Embeddings + Vector Search. |
| `run.googleapis.com` | Cloud Run — the 7 services that compose the agent |
| `storage.googleapis.com` | GCS — landing zone for source docs, generated artifacts, audit ledger (Bucket Lock for Validated mode) |
| `firestore.googleapis.com` | Operational state: templates, report runs, citations, audit chain |
| `cloudkms.googleapis.com` | CMEK on all GCS / Firestore / BigQuery; HSM-backed key for Validated-mode Merkle root signing |
| `secretmanager.googleapis.com` | Connector credentials (Veeva, Medidata, LabVantage, SharePoint, S3) |
| `iap.googleapis.com` | Identity-Aware Proxy in front of `api-gateway` |
| `cloudbuild.googleapis.com` | CI/CD container builds (may overlap with existing GitHub Project) |
| `artifactregistry.googleapis.com` | Container image storage for each Cloud Run service |
| `logging.googleapis.com` | Cloud Audit Logs → GCS audit sink |
| `bigquery.googleapis.com` | `analytics_ops` dataset for run telemetry; `analytics_source` for read-only federated access to clinical warehouses |
| `documentai.googleapis.com` | PDF/DOCX layout extraction for source ingestion |
| `dlp.googleapis.com` | PHI/PII redaction before any data reaches the LLM |
| `workflows.googleapis.com` | Durable orchestration of multi-section generation runs |
| `eventarc.googleapis.com` | Pub/Sub-driven trigger for parsing on GCS object finalize |
| `pubsub.googleapis.com` | Inter-service async messaging |
| `vpcaccess.googleapis.com` | Serverless VPC Access — Cloud Run → internal resources |

## 3. IAM roles needed on `gsk-rd-ai-code-tool-exe`

### 3.1 Developer / operator (Chad Alburn)

Primary roles I need on this project, in priority order:

| Role | Purpose |
|---|---|
| `roles/aiplatform.user` | Invoke Vertex AI Claude endpoints for dev / smoke testing |
| `roles/run.developer` | Deploy + update Cloud Run services |
| `roles/storage.admin` (scoped to `ib-*` buckets if possible) | Create + manage project buckets |
| `roles/datastore.owner` *(or equivalent Firestore admin)* | Provision the Firestore database + collections |
| `roles/cloudkms.admin` (scoped to the project's keyring) | Create the CMEK keyring + per-data-class keys |
| `roles/secretmanager.admin` | Manage per-connector secret instances |
| `roles/iam.serviceAccountAdmin` | Create + manage the per-service SAs (one per Cloud Run service) |
| `roles/iam.workloadIdentityPoolAdmin` | Configure Workload Identity Federation (likely already in place from the GitHub Project) |
| `roles/logging.admin` | Configure log sinks (audit log → GCS, BQ) |
| `roles/bigquery.admin` (scoped to the `analytics_ops` dataset) | Create + maintain telemetry dataset |
| `roles/documentai.editor` | Configure Document AI processors |
| `roles/iap.tunnelResourceAccessor` *(and admin during setup)* | Configure IAP-fronted ingress |

If a `roles/owner`-scoped grant on this project is acceptable per GSK
policy, that subsumes the list above. Otherwise the granular roles
above are the minimum I'd need to stand up the system end-to-end.

### 3.2 Service accounts to create

| Service account | Purpose | Roles on this project |
|---|---|---|
| `api-gateway-sa@` | Gateway service identity | `roles/iam.serviceAccountTokenCreator` to invoke downstream Cloud Run services |
| `ingestion-sa@` | Connector runtime | `roles/secretmanager.secretAccessor` on connector secrets; `roles/storage.objectCreator` on landing bucket; `roles/pubsub.publisher` |
| `parsing-sa@` | Parsing + DLP | `roles/storage.objectViewer` on landing; `roles/storage.objectCreator` on normalized; `roles/documentai.apiUser`; `roles/dlp.user` |
| `template-sa@` | Template CRUD | `roles/datastore.user` |
| `orchestrator-sa@` | Generation runs | `roles/aiplatform.user` (Vertex AI Claude); `roles/datastore.user`; `roles/workflows.invoker` |
| `citation-sa@` | Evidence ledger | `roles/datastore.user`; `roles/storage.objectCreator` on audit bucket |
| `renderer-sa@` | Google Docs/Drive output | **Domain-wide delegation** scoped to `drive.file` + `documents` Workspace scopes (requires Workspace super-admin sign-off — see §6) |
| `audit-sa@` | Audit signing | `roles/cloudkms.signer` on the signing key only; `roles/storage.objectAdmin` on audit bucket (with Bucket Lock so admin still can't break retention) |

All service accounts are created **without** JSON keys. Workload
identity binds them to the Cloud Run service at deploy time. The
project's IAM policy must forbid `iam.serviceAccountKeys.create` on
these SAs.

## 4. Networking + security baselines

| Item | Need |
|---|---|
| VPC Service Controls perimeter | Around GCS, Firestore, BigQuery, Vertex AI, Secret Manager. Ingress allowed from IAP only; egress denied except to Google APIs and (optionally) approved external connectors |
| CMEK | All GCS buckets, Firestore database, BigQuery datasets, Secret Manager secrets. Keys live in this project's keyring under Cloud KMS. HSM protection level for the audit signing key (Validated mode) |
| Private Service Connect | For Vertex AI endpoints (LLM + Vector Search) so traffic doesn't traverse the public internet |
| Cloud Audit Logs | Admin Activity + Data Access enabled; log sink to `ib-audit-{env}` GCS bucket (Bucket Lock + 10-year retention for Validated mode) |
| Bucket Lock | `ib-audit-{env}` bucket — retention policy locked at creation time |

## 5. External system credentials (Secret Manager only)

The agent integrates with external systems via connectors. No
JSON-key files in the repo; credentials live in Secret Manager and
are accessed by the relevant per-service SA:

- Veeva Vault (API token)
- Medidata Rave (OAuth client credentials)
- LabVantage (API key + base URL)
- SharePoint (Graph API client credentials)
- AWS S3 (federated identity, no static keys)

Each secret rotates every 90 days; the policy is enforced via
Secret Manager rotation schedule + a Cloud Function rotator.

## 6. External approvals + cross-team dependencies

These are not GCP IAM grants but they block the work:

1. **Google Workspace super-admin sign-off on domain-wide delegation**
   for `renderer-sa@`, scoped to `drive.file` + `documents`. Without
   this, the agent cannot create or update Google Docs in users'
   Drives.
2. **Vertex AI Claude access via internal channel.** Per prior
   guidance (Douglas Scheesley, Gene's group), GSK uses corporate
   Vertex AI with Kong fronting Claude. Need:
   - Confirmation that the Claude endpoint is reachable from
     `gsk-rd-ai-code-tool-exe` via the corporate Kong gateway
   - The Kong `base_url` to pass to `anthropic[vertex]` SDK
   - Confirmation that the GSK BAA covers the specific Claude
     endpoint we'll use
3. **GAMP 5 / Part 11 supplier assessment** for Vertex AI Claude
   (regulatory follow-up; not blocking R&D / GxP-aware modes but
   required before formal Validated-mode operation).
4. **Privacy / legal sign-off** that the de-identify-before-LLM
   architecture (FS-080 in the CSV docs) satisfies GSK's PHI handling
   policy. The system never sends raw PHI to the LLM regardless, but
   policy alignment is needed.

## 7. What I'd plan to spin up first (sequencing)

To de-risk the early work I'd request the grants in this order:

1. Day 1: `roles/run.developer`, `roles/aiplatform.user`,
   `roles/storage.admin`, `roles/iam.serviceAccountAdmin`,
   `roles/secretmanager.admin`
2. Day 1-3: Stand up the api-gateway + orchestrator Cloud Run
   services with the stub LLM client. Verify Vertex AI Claude
   reachability through the Kong endpoint.
3. Week 2: `roles/cloudkms.admin` + KMS keyring + Bucket Lock on
   audit bucket; Firestore database.
4. Week 3: `roles/documentai.editor` + DLP for the ingestion path.
5. Week 4+: VPC-SC perimeter + Private Service Connect (security
   team-coordinated).

## 8. Reference materials

- **Architecture:** [docs/architecture-plan.md](architecture-plan.md)
- **CSV documentation:** [docs/csv/README.md](csv/README.md) (URS, FS,
  traceability matrix already drafted)
- **Code:** `services/`, `shared/`, `tests/` — 172 tests passing as
  of commit `39d2e4b`
- **End-to-end demo:** `python samples/run_synthetic_ib.py --anchor`
  runs the full pipeline on a synthetic compound corpus + emits a
  signed audit anchor (uses local RSA key today; swaps to KmsRootSigner
  once `roles/cloudkms.admin` is granted)

## 9. Contact

- Requestor: Chad Alburn (chad.l.alburn@gsk.com)
- Vertex AI corporate channel: Douglas Scheesley (Gene's group)
- Architectural questions: see this repo's
  [README.md](../README.md) + the plan above
