# Onyx LLM PoC access — filled submission + closeout plan

**Process:** GSK Work Instruction **VQD-WI-063019** — "LLM Access
Enablement within Onyx for Proof of Concept Purposes" (effective
13 Mar 2026). This is the sanctioned path to obtain Claude access for
a time-limited PoC (up to 90 days), tied to a specific GCP project and
an approved model.
**System:** Report Generator Agent (`chadlalburn-dev/AgenticReportWriter`)
**Prepared:** 2026-05-27
**Status:** DRAFT for submission — fields marked ⚠️ must be confirmed
by the requester before submitting.

## Scope confirmation (per WI §2)

- **In scope:** we need *new* Claude provisioning on Vertex AI → this
  WI applies (§2.1). We are **not** in the §2.2 out-of-scope bucket
  (which is for PoCs using models already accessible).
- **Data lane (WI page 4 table): "Test or mock data" → no additional
  data approval required**, permitted in Dev/Test/ENG. The pilot runs
  on synthetic data only (fictional compound XYZ-001), so we take the
  lightest data path. **Commitment: no real GSK/proprietary/PHI data
  enters the PoC.** Introducing any such data later would require the
  Data Lifecycle Management within Onyx SOP + ENG environment.

## Access Form fields (WI §5.2)

| # | Field | Value |
|---|---|---|
| 1 | Request title | `Claude access for Report Generator Agent PoC` |
| 2 | Business objective | Evaluate whether Claude on Vertex AI can generate draft Investigator's Brochures from source study documents and structured data, with every fact cited to its source, for medical-writer review. Success = a fully-cited, reviewable IB draft produced end-to-end from a synthetic compound corpus. |
| 3 | Target dates | ⚠️ Start = provisioning date; End = Start + 90 days (max). Enter actual dates. |
| 4 | Business owner | ⚠️ Name / team / email (you or your PoC sponsor). |
| 5 | Technical owner | Chad Alburn / ⚠️[team] / chad.l.alburn@gsk.com |
| 6 | Requested model | ⚠️ Claude, selected from the "Models Available" dropdown. **Note:** the architecture uses two tiers — Claude Sonnet (section fill) + Claude Opus (plan/critique). If the dropdown allows only one selection, request the most capable Claude available and we configure both tiers to it; if it allows multiple, request both. |
| 7 | GCP project id | ⚠️ **Exact** id of the project behind the Onyx ML Workspace (see Sequencing). Example format from the WI: `aipl-oligo-dev`. Do **not** guess — verify from the provisioned workspace. |
| 8 | Usage pattern | `batch` — each IB generation run is a batch of ~25–40 model calls. (Early dev is interactive; batch is the representative steady pattern.) |
| 9 | Expected scale | ~300 requests/day during active testing; peak concurrency ~10. (Basis: ~30 model calls per IB run × a handful of IBs/day.) |

## Sequencing / dependencies

1. **Provision the Onyx ML Workspace first** (self-service catalog:
   Cloud Run + GCS + BigQuery). That produces the **GCP project id**
   that Field 7 of this form requires.
2. **Then submit this LLM Enablement form**, pointing at that project
   id and requesting Claude.
3. The enablement team provisions LLM access + "any required IAM
   bindings, service accounts, or secrets configured per platform
   standard" (WI §5.4) — which likely reduces the separately-requested
   admin-role footprint for the model path.
4. Technical Owner runs a minimal validation test (one request) to
   confirm access, then confirms PoC start (WI §5.4).

## Operating expectations during the PoC (WI §5.5) — our commitments

- Usage stays within stated objective, scale, and timeline.
- Any material change (different model, higher scale, new project id,
  extended timeline) triggers a re-review — we will resubmit rather
  than silently exceed scope.
- Synthetic/mock data only for the duration of the PoC.
- Issues/incidents/policy concerns reported via the standard
  escalation path.

## PoC closeout & access revocation plan (WI §5.7 — required)

A closeout plan is an Appendix-1 checklist item; having it ready
strengthens the submission.

**Trigger:** on or before the PoC end date (Field 3 end date), or
immediately if the PoC is discontinued/rejected.

1. **Record outcome** (Business Owner): success criteria met / not met,
   with the end-to-end IB draft + audit artifacts as evidence.
2. **Stop the PoC** (Technical Owner): halt all scheduled/interactive
   generation runs; confirm no further model calls are issued.
3. **Request access revocation** (Technical Owner): notify the GenAI
   Platform team to disable the enabled Claude/Vertex access tied to
   the PoC (or verify automated revocation fired).
4. **Tear down PoC-only credentials/resources**: disable or remove the
   service accounts, secrets, and integrations created solely for the
   PoC. (Our design uses no static keys — workload identity / ADC — so
   there are no downloadable credentials to revoke, only SA bindings.)
5. **Archive the temporary PoC project** so it is clearly separated
   from any ongoing/production work (WI §5.6).
6. **Do not roll into production on PoC access.** If the PoC succeeds
   and needs to continue, start the standard PDLC production pathway;
   production access/controls/approvals are handled there before any
   go-live. Migration target is the Validated-mode production project
   (see `permissions_request_gsk_rd_ai_code_tool_exe.md`).
7. **Retain records** (WI §6.0): completed Access Form, approvals, and
   closeout evidence per the applicable retention policy.

## Appendix 1 checklist (WI pages 7–8) — pre-filled

- [x] Request title completed
- [x] Business objective provided (1–2 sentences)
- [ ] ⚠️ Target dates within PoC window (≤ 90 days)
- [ ] ⚠️ Business Owner identified (name, team, email)
- [x] Technical Owner identified (Chad Alburn, chad.l.alburn@gsk.com — ⚠️ add team)
- [ ] ⚠️ Requested model selected from approved list (Claude)
- [ ] ⚠️ GCP project id provided and verified (from Onyx workspace)
- [x] Usage pattern selected (batch)
- [x] Expected scale provided (~300 req/day, peak concurrency ~10)
- [x] Closeout/revocation plan exists (this document, section above)

## How this changes the overall access strategy

Three clean sequential steps, replacing the uphill admin-roles ask for
the model path:

1. **Onyx ML Workspace** (self-service) → Cloud Run + GCS + BigQuery
   + a GCP project id
2. **This LLM Enablement form** (VQD-WI-063019) → Claude in that
   project, 90-day PoC window
3. **Build/run** the agent on synthetic data within the window

The broader `gsk-rd-*` admin-role requests (see the other permission
docs) are now scoped mainly to what the self-service catalog + this WI
do **not** cover, and should be trimmed accordingly
(see the reconciliation follow-up).

## References

- WI: VQD-WI-063019 (Veeva QualityDocs — current version governs)
- Workspace request draft: `docs/onyx_workspace_request.md` *(if saved)*
- Production-target permissions: `docs/permissions_request_gsk_rd_ai_code_tool_exe.md`
- MVP permissions: `docs/permissions_request_mvp_pcs_sys_eng.md`
- Architecture: `docs/architecture-plan.md`
