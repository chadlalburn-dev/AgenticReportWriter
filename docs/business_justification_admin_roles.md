# Business justification — GCP admin roles (DPO review)

**Requestor:** Chad Alburn (chad.l.alburn@gsk.com)
**System:** Report Generator Agent — AI-assisted clinical/regulatory
report generation (Investigator's Brochure pilot)
**Projects:** `gsk-rd-pcs-sys-eng` (MVP) / `gsk-rd-ai-code-tool-exe`
(production target)
**Date:** 2026-05-27

## 1. Summary (for the DPO team)

I am requesting a set of GCP roles — several at admin level — for a
**single named developer** (me) to stand up a **pilot** system that
drafts clinical research reports. The admin roles fall into two
groups: **setup/provisioning roles that are needed only during
initial build-out and can be time-boxed/removed afterward**, and
**resource roles that can be scoped to this system's own resources**.

Three points that bound the data-protection risk:

1. **No personal data at pilot stage.** The pilot processes only
   **synthetic data** (a fictional compound, "XYZ-001") and
   de-identified inputs. No real patient/subject PHI is processed
   during the pilot. Real-data processing is a later, separately
   governed phase.
2. **Privacy-by-design is already built into the system** (Section 3):
   de-identification before any LLM call, no static credentials,
   customer-managed encryption keys, and an immutable audit trail.
3. **These are not standing production grants for a team** — they are
   provisioning roles for one developer on a pilot, and the
   highest-privilege ones can be removed once setup is complete.

## 2. Business context

GSK medical writers and regulatory teams spend significant time
manually assembling research reports (Investigator's Brochures,
Clinical Study Reports) by pulling data from multiple systems into
approved templates. This is slow, repetitive, and error-prone.

The Report Generator Agent automates the **draft** generation step:
it ingests source data, populates a structured template, and produces
a draft for human review — with every fact cited back to its source.
The business value is reduced cycle time and improved traceability;
the human medical writer remains accountable for the final document.

To build and operate this on GCP, the developer must be able to
provision the underlying services (Cloud Run, Vertex AI, Firestore,
storage, encryption keys) — which is what these roles enable.

## 3. Data-protection posture (why this is lower-risk than it looks)

The system was designed with the following controls, which are
already implemented in code (172 automated tests passing):

| Control | How it protects data |
|---|---|
| **De-identify before LLM** | PHI/PII is stripped via Cloud DLP before any data is sent to the language model. The LLM never sees identifiable data. (Pilot uses synthetic data regardless.) |
| **No static credentials** | The system uses no API keys or downloadable service-account key files. Authentication is via Application Default Credentials / workload identity. This eliminates a major credential-leakage risk. The `workloadIdentityPoolAdmin` role is requested specifically to enable this safer pattern. |
| **Customer-managed encryption (CMEK)** | All stored data is encrypted with keys we control in Cloud KMS. The `cloudkms.admin` role is requested to create these keys — i.e. the admin request exists *because* we are adding encryption, not bypassing it. |
| **Immutable audit trail** | Every action (data access, generation, export) is recorded in a hash-chained, tamper-evident audit ledger; in validated mode, daily snapshots are cryptographically signed. |
| **Human-in-the-loop** | The system produces drafts only; a qualified human reviews and approves before any output is finalized. |

## 4. Per-role business justification

Roles are grouped by purpose. "Admin?" flags which are admin-level.
"Least-privilege mitigation" shows how each can be scoped or removed.

### 4a. Setup / provisioning roles — time-boxed, removable after build-out

| Role | Admin? | Why it is needed | Least-privilege mitigation |
|---|---|---|---|
| `roles/serviceusage.serviceUsageAdmin` | Yes | Enable the GCP APIs the system uses (Vertex AI, Cloud Run, Firestore, KMS). No lower role can enable APIs. | **Removable after initial setup** — once APIs are enabled, this is no longer needed. |
| `roles/iam.serviceAccountAdmin` | Yes | Create the per-service service accounts the application runs as (one identity per service, least-privilege between services). | **Removable after setup.** Needed only while creating the SAs. |
| `roles/iam.workloadIdentityPoolAdmin` | Yes | Configure keyless CI/CD authentication (GitHub Actions → GCP) so we never use downloadable keys. **This role exists to *improve* the security posture.** | **Removable after setup.** One-time federation config. |
| `roles/resourcemanager.projectIamAdmin` | Yes (most sensitive) | Bind the least-privilege roles to the service accounts created above. | **Can be declined entirely** — if DPO prefers, the platform team runs the role-bindings from a script I provide, and I never hold this role. Otherwise **removable after setup**. |

### 4b. Resource roles — scoped to this system's own resources

| Role | Admin? | Why it is needed | Least-privilege mitigation |
|---|---|---|---|
| `roles/storage.admin` | Yes | Create + configure the storage buckets (with CMEK + retention policy) the system uses. | **Scopable to this system's buckets only** (`ib-*` naming prefix), rather than project-wide. |
| `roles/secretmanager.admin` | Yes | Create secret containers for future data-source credentials. | At pilot stage **no real secrets are stored** (synthetic data). Can be reduced to create+access on this system's secrets only. |
| `roles/cloudkms.admin` | Yes | Create the encryption keyring + keys used for CMEK and audit-trail signing. | **Scopable to a single keyring** for this system. This role enables encryption; it does not grant data access. |
| `roles/datastore.owner` | Yes | Create the Firestore database + collections for audit + metadata. | **Reducible to `datastore.user`** once the database is provisioned. |

### 4c. Non-admin role (listed for completeness)

| Role | Admin? | Why it is needed |
|---|---|---|
| `roles/aiplatform.user` | **No** — standard user role | Call the Vertex AI model to generate report drafts. This is a "use the service" role, not an admin role. |
| `roles/run.developer` | **No** — developer role | Deploy the application services to Cloud Run. |
| `roles/iam.serviceAccountUser` | **No** | Run services *as* the service accounts (standard deploy-time requirement). |
| `roles/artifactregistry.writer` | **No** | Publish the application's container images. |

## 5. Mitigations and commitments

To bound the data-protection exposure of the admin roles, I commit to:

1. **Time-boxing the setup roles.** The four roles in §4a
   (`serviceUsageAdmin`, `serviceAccountAdmin`,
   `workloadIdentityPoolAdmin`, `projectIamAdmin`) are needed only
   during initial provisioning (estimated 2–4 weeks). I am happy for
   them to be granted with an expiry, or removed by the platform team
   once setup is signed off.
2. **Declining `projectIamAdmin` if preferred.** This is the most
   sensitive role. If DPO would rather I not hold it, the platform
   team can execute the service-account role-bindings from a script I
   provide, and I will never hold project-IAM-admin.
3. **Scoping resource roles** to this system's own resources where
   the platform supports it (bucket-prefix scoping for storage,
   single keyring for KMS).
4. **Synthetic data only during the pilot.** No real patient or
   subject data is processed until a separate data-processing
   approval is in place.
5. **Full auditability.** Every action taken with these roles is
   recorded in GCP Cloud Audit Logs and is available for DPO/QA
   review at any time.

## 6. What I am asking the DPO team to approve

The role set in Section 4 for `chad.l.alburn@gsk.com` on the pilot
project, on the understanding that:
- the setup roles (§4a) may be time-boxed/removed after provisioning,
- `projectIamAdmin` may be declined in favor of platform-team-run
  bindings,
- the pilot processes synthetic/de-identified data only.

I am happy to walk the DPO team through the architecture or the
privacy controls in Section 3 if that helps the review.

## 7. References

- System architecture: `docs/architecture-plan.md`
- Privacy / compliance design (URS/FS): `docs/csv/`
- Permissions detail: `docs/permissions_request_mvp_pcs_sys_eng.md`
  and `docs/permissions_request_gsk_rd_ai_code_tool_exe.md`
