# Computer System Validation (CSV) documentation

This directory holds the regulatory documentation needed to operate the
report generator in **Validated mode** (21 CFR Part 11 compliant) as
described in the [architecture plan](../architecture-plan.md).

## Status

These are **pre-populated starting drafts**, not approved CSV records.
They were derived from the codebase (`services/`, `templates/`,
`shared/`) and from the architecture plan. The QA / regulatory team
must:

1. Review each document in detail
2. Customize tenant-specific fields (organization, sponsors, key
   personnel, environments)
3. Run the protocols and capture evidence
4. Submit for formal approval through your organization's change-
   control process

Drafts are **not** suitable for regulatory submission as-is.

## Scope of validation

| Aspect | Decision |
|---|---|
| GAMP 5 category | Category 5 (custom + AI/ML); LLM is Category 4 supplier component |
| Regulated compliance modes | R&D, GxP-aware, Validated |
| Validation scope (initial release) | Validated mode for Investigator's Brochure generation only |
| Supplier assessment | Google Cloud (Vertex AI, KMS, Firestore, GCS); Anthropic (via Vertex) |
| Regulatory frameworks | FDA 21 CFR Part 11; EU Annex 11; ICH E6(R2) GCP |

## Documents in this directory

| File | Purpose |
|---|---|
| [URS.md](URS.md) | User Requirements Specification — what the system must do, from the user's perspective |
| [FS.md](FS.md) | Functional Specification — how the system meets each URS requirement |
| [traceability_matrix.md](traceability_matrix.md) | URS → FS → code module → test linkage |
| [change_control_sop.md](change_control_sop.md) | SOP for changes to validated templates, code, and infrastructure |

## Documents not yet produced (follow-up)

These are part of the full CSV deliverable but not in this initial
draft set:

- **DS (Design Specification)** — implementation-level architecture for
  each FS item. The existing `docs/architecture-plan.md` already
  carries most of this content; it should be re-formatted into the
  standard DS template when QA engages.
- **IQ (Installation Qualification)** — test protocol verifying the
  system is installed correctly in each environment (dev, staging,
  prod). Covers Terraform-applied state, IAM bindings, KMS keyrings,
  bucket retention locks, etc.
- **OQ (Operational Qualification)** — test protocol verifying each
  function operates per FS. Drawn from the existing pytest suite
  (`tests/`) which already covers most functional requirements;
  needs formatting + a deviation log.
- **PQ (Performance Qualification)** — test protocol verifying the
  system performs in production: end-to-end IB generation on real
  data, throughput, latency, recovery.

## How to use this directory

1. Open URS.md and confirm the documented requirements match what your
   org actually expects.
2. Walk the traceability matrix to confirm each URS line has both a
   functional design (FS) and a test (a row in `tests/`).
3. When changes are made to the code or templates, follow
   change_control_sop.md to update these documents in lockstep.

## Cross-references

- Architecture plan: [../architecture-plan.md](../architecture-plan.md)
- Audit trail design: see `services/audit/`
- Generation pipeline: see `services/generation_orchestrator/`
- Code review test suite: 161 pytest tests under `tests/`
