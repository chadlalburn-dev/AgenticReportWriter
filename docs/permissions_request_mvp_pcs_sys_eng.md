# GCP permissions request — MVP on `gsk-rd-pcs-sys-eng`

**Requestor:** Chad Alburn (chad.l.alburn@gsk.com)
**System:** Report Generator Agent — Investigator's Brochure MVP
**Repo:** `chadlalburn-dev/AgenticReportWriter` (172 tests passing,
end-to-end stub-LLM demo working today)
**Target GCP project:** `gsk-rd-pcs-sys-eng`
**Status of sibling project:** `gsk-rd-ai-code-tool-exe` is the
eventual production target, deferred because of VPC Service Controls
perimeter overhead (see prior diagnostic; perimeter violations
blocked listing GCS buckets, Cloud Run services, and Artifact
Registry repos there from Cloud Shell).
**Date:** 2026-05-27

## TL;DR — what I'm asking for

A minimum set of grants on `gsk-rd-pcs-sys-eng` to take the agent from
its current local-PoC state to a small Cloud Run deployment that
calls real Vertex AI Claude on a pilot Investigator's Brochure. Three
categories of ask:

1. **APIs to enable** (or confirm enabled): `aiplatform`, `run`,
   `firestore`, `storage`, `iam`, `iamcredentials`,
   `secretmanager`, `cloudkms`
2. **IAM roles for me** on this project so I can stand the services
   up myself rather than going back through the architect for every
   provisioning step
3. **External:** Vertex AI Claude on Model Garden enabled in
   `us-east5` (or whichever region the GSK Kong endpoint fronts),
   plus confirmation of the Kong base URL

The full production architecture (VPC-SC, KMS HSM, Bucket Lock,
Document AI, DLP, IAP, BigQuery analytics) is deferred to Path B on
`gsk-rd-ai-code-tool-exe`.

## Step 0 — diagnostic (run this in Cloud Shell first)

Paste into Cloud Shell at https://shell.cloud.google.com so we share
a baseline before requesting grants:

```bash
PROJECT_ID="gsk-rd-pcs-sys-eng"
echo "=== project number ==="
gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2>&1
echo ""
echo "=== APIs already enabled (the MVP-relevant ones) ==="
gcloud services list --enabled --project="$PROJECT_ID" \
  --filter='config.name:(aiplatform.googleapis.com OR run.googleapis.com OR firestore.googleapis.com OR storage.googleapis.com OR iam.googleapis.com OR iamcredentials.googleapis.com OR secretmanager.googleapis.com OR cloudkms.googleapis.com OR artifactregistry.googleapis.com OR cloudbuild.googleapis.com)' \
  --format='value(config.name)' 2>&1
echo ""
echo "=== can I list resources here? ==="
echo "-- Cloud Run services --"
gcloud run services list --project="$PROJECT_ID" --format='value(name,region)' 2>&1 | head -10
echo "-- service accounts --"
gcloud iam service-accounts list --project="$PROJECT_ID" --format='value(email)' 2>&1 | head -10
echo "-- GCS buckets --"
gcloud storage buckets list --project="$PROJECT_ID" --format='value(name)' 2>&1 | head -10
echo "-- Firestore databases --"
gcloud firestore databases list --project="$PROJECT_ID" --format='value(name)' 2>&1 | head -5
echo ""
echo "=== Vertex AI reachability ==="
gcloud ai models list --project="$PROJECT_ID" --region=us-east5 --format='value(displayName)' 2>&1 | head -5
gcloud ai models list --project="$PROJECT_ID" --region=us-central1 --format='value(displayName)' 2>&1 | head -5
```

The four things the output tells us:

| Section | What it determines |
|---|---|
| APIs enabled | Which of the 8 we still need to ask the architect to enable |
| Can list Cloud Run / SAs / GCS / Firestore | Whether I have any read access at all on this project today |
| Vertex AI reachability | Whether Claude on Model Garden works without further grants (vs. needing the Kong/internal-channel routing the architect mentioned) |
| Any `VPC_SERVICE_CONTROLS` violations in errors | Whether this project is inside a perimeter too — if not, MVP is significantly simpler than the sibling |

## 1. APIs to enable on `gsk-rd-pcs-sys-eng`

The MVP needs these eight. Some may already be on — Step 0 confirms.

| API | Why needed for MVP |
|---|---|
| `aiplatform.googleapis.com` | Vertex AI Claude (Sonnet 4.6 + Opus 4.7 via Model Garden) — the LLM heart |
| `run.googleapis.com` | Cloud Run — where the agent services run |
| `firestore.googleapis.com` | Audit ledger + report metadata (SqliteAuditStore swaps to FirestoreAuditStore — code already exists) |
| `storage.googleapis.com` | Source-doc landing zone + generated artifacts |
| `iam.googleapis.com` + `iamcredentials.googleapis.com` | Service account creation + token-based auth |
| `secretmanager.googleapis.com` | Connector credentials when we add real source-system connectors |
| `cloudkms.googleapis.com` | CMEK on GCS buckets (MVP); HSM-signed Merkle roots deferred to Path B |
| `artifactregistry.googleapis.com` | Container image hosting (only if we don't use Cloud Run source-deploys) |

`documentai`, `dlp`, `bigquery`, `vpcaccess`, `iap` are **not** needed
for MVP.

## 2. IAM roles I need on `gsk-rd-pcs-sys-eng`

Ranked by what unblocks the most work. If a `roles/owner` grant is
acceptable per GSK policy on a non-production pilot project, that
subsumes everything below.

| Role | What it unblocks |
|---|---|
| `roles/aiplatform.user` | Call Vertex AI Claude from local dev + Cloud Run (most critical — without this, no LLM calls work) |
| `roles/run.developer` | `gcloud run deploy` of the agent services |
| `roles/iam.serviceAccountAdmin` | Create per-service SAs (one per Cloud Run service) |
| `roles/iam.serviceAccountUser` | Let me act as the SAs I create |
| `roles/datastore.owner` *(or Firestore admin equivalent)* | Provision the Firestore database + collections |
| `roles/storage.admin` (scoped to `ib-*` buckets if possible) | Create + manage project buckets |
| `roles/secretmanager.admin` | Manage per-connector secret instances |
| `roles/cloudkms.admin` (scoped to a project keyring) | Create the CMEK keyring + per-data-class keys |

What I am **not** asking for on this project:

- VPC Service Controls administration — out of scope for Path A
- IAP admin — deferred (Cloud Run services will be `--no-allow-unauthenticated` and access goes through `gcloud run services proxy` for the MVP)
- Org policy edits — none needed if the project's defaults aren't too restrictive
- Workload Identity Federation pool admin — deferred; local dev uses `gcloud auth application-default login`, Cloud Run uses its own runtime SA

## 3. Service accounts I'll create (no JSON keys)

All bound via workload identity at deploy time. The architect doesn't
need to create these — I'll do it once `roles/iam.serviceAccountAdmin`
lands. Listing them here so the request is transparent:

| Service account | Used by | Project-scoped roles |
|---|---|---|
| `orchestrator-sa@…` | Generation orchestrator Cloud Run service | `roles/aiplatform.user`, `roles/datastore.user` |
| `audit-sa@…` | Audit emitter | `roles/datastore.user`, `roles/storage.objectAdmin` (audit bucket only) |
| `ingestion-sa@…` | Ingestion service | `roles/storage.objectAdmin` (landing bucket), `roles/secretmanager.secretAccessor` |
| `renderer-sa@…` | Document renderer (Google Docs API) | No project-IAM grants; uses Workspace domain-wide delegation (Path B concern) |

For the MVP I'll start with **one** SA (`agent-sa@…`) that does all
of the above and split it apart once we know what actually works.

## 4. Vertex AI Claude — external dependency

This is the one item that depends on cross-team work, not just IAM
grants on the project:

- Confirm Claude Sonnet 4.6 + Opus 4.7 are accessible via Vertex AI
  Model Garden in `us-east5` (or wherever GSK's Kong routes to)
- Confirm the corporate Kong base URL I should configure on the
  `AnthropicVertex` SDK client — the code already accepts a
  `base_url` parameter
- Confirm the GSK Vertex BAA covers the Claude endpoint for the
  data classes this MVP will touch (synthetic data only at pilot
  start; no real PHI until Path B + DLP redactor)

**Contact named in our prior architect chat:** Douglas Scheesley
(Gene's group). If a different group owns Vertex Claude
provisioning, please redirect.

## 5. Questions for the architect (open from prior diagnostic)

These came out of the `gsk-rd-ai-code-tool-exe` diagnostic and
generally still apply:

1. **Is `gsk-rd-pcs-sys-eng` inside a VPC-SC perimeter?** Step 0
   output will show this via any `VPC_SERVICE_CONTROLS` violation
   messages. If yes, see questions 2-3; if no, the MVP plan stands.
2. *(Only if perimeter exists)* What's the standard ingress pattern
   for developers needing to deploy/admin services here? Will I need
   a VM inside the perimeter, an ingress policy with my user, or
   should I just use Cloud Build inside the perimeter for all deploys?
3. *(Only if perimeter exists)* What's the standard egress pattern
   for Cloud Run services to call external SaaS (GitHub for source,
   ChEMBL/bioRxiv MCP endpoints later)?
4. **Is Cloud SQL allowed in this project?** The MVP uses Firestore
   so this is a Path B concern, but worth confirming early since
   the sibling project had BigQuery enabled (suggesting data-plane
   services are generally OK).
5. **For Path B migration:** What's the typical timeline to get a
   user added to a VPC-SC ingress policy on
   `gsk-rd-ai-code-tool-exe`? That dictates when I can move the
   workload over.

## 6. What I'll demonstrate once these land

The current local PoC runs end-to-end against the synthetic XYZ-001
corpus, producing 8 citations from 3 data sources (ingested docs,
named SQL queries against a local EDC, ChEMBL/ClinicalTrials-style
APIs). With the grants above I'll:

1. **Day 1-2:** `gcloud auth application-default login`; run the
   existing demo with `--vertex-project gsk-rd-pcs-sys-eng --vertex-region us-east5`,
   replacing the stub LLM with real Claude. Verify a real Claude run
   produces a populated IB instance with valid citations.
2. **Day 3-5:** Deploy the generation-orchestrator service to Cloud
   Run; verify it can call Vertex AI from inside the project; swap
   the SqliteAuditStore for FirestoreAuditStore (code already
   exists).
3. **Week 2:** Pilot run with the medical-writer reviewer on one
   real compound's source corpus (de-identified — Path A scope).
4. **Pilot exit:** decision point on Path B migration to
   `gsk-rd-ai-code-tool-exe` for Validated-mode operation.

## 7. Migration to Path B (`gsk-rd-ai-code-tool-exe`)

Once the MVP is validated on `gsk-rd-pcs-sys-eng`, the workload moves
to `gsk-rd-ai-code-tool-exe` for Validated-mode production. That
migration is captured in
[permissions_request_gsk_rd_ai_code_tool_exe.md](permissions_request_gsk_rd_ai_code_tool_exe.md).
No additional architecture work required — the code already supports
VPC-SC-aware deployment, KMS-backed signing (`KmsRootSigner`),
Firestore audit (`FirestoreAuditStore`), and Workspace domain-wide
delegation for Docs rendering. The migration is a deployment exercise,
not a re-architecture.

## 8. Contact

- Requestor: Chad Alburn (chad.l.alburn@gsk.com)
- Vertex AI corporate channel: Douglas Scheesley (Gene's group, per
  prior architect conversation)
- Repo + architecture: this repository's `docs/architecture-plan.md`
- CSV documentation: `docs/csv/` (URS / FS / traceability already
  drafted)
