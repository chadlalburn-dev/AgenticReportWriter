# Change Control SOP

**System:** Report Generator Agent
**Document version:** 0.1.0 (draft)
**Effective when:** Validated-mode operation is in effect for at least one
  project; otherwise advisory.

This SOP governs how changes are made to:
- Approved templates (`templates/library/*.json` with
  `status: approved`)
- Code under `services/` and `shared/`
- Infrastructure under `infra/terraform/`
- The CSV documents in `docs/csv/`

## 1. Purpose

To ensure that every change to a Validated-mode component is:
- Documented before it is made
- Reviewed by the right roles
- Tested against the regression suite
- Reflected in the URS / FS / traceability matrix
- Captured in the audit trail

## 2. Scope

This SOP applies to **any change** that touches a file referenced by
the [traceability matrix](traceability_matrix.md) when at least one
project is operating in Validated mode.

Changes during the R&D / GxP-aware phases of pilot work do not
require this SOP (but are still tracked via git history).

## 3. Roles

| Role | Responsibility |
|---|---|
| Change requester | Files the change request; defines impact |
| Technical reviewer | Reviews implementation + tests |
| QA reviewer | Reviews documentation updates + regression evidence |
| Approver | Final sign-off; only persons listed in the URS approval table |
| Implementer | Lands the change in the codebase |

## 4. Change classes

| Class | Description | Approver depth | Regression scope |
|---|---|---|---|
| **A — Major** | Changes to approved templates, audit schema, hash-chain logic, signature flow, Vertex client | Approver + QA + Technical | Full pytest suite + IQ touched components |
| **B — Minor** | New connector implementations, new named queries, new file-set tags | QA + Technical | Affected service test files |
| **C — Documentation only** | Updates to architecture plan, URS, FS, this SOP, runbooks | Technical reviewer only | Markdown lint + link check |

A change that adds a brand-new feature spanning multiple modules
defaults to Class A unless the QA reviewer explicitly downgrades it
in the impact analysis.

## 5. Workflow

1. **File the change request** — create an issue or change record
   with:
   - Description
   - Class (A / B / C)
   - URS items affected (see traceability matrix)
   - FS items affected
   - Regression suite coverage plan (which tests run, any new tests)
   - Estimated rollback procedure

2. **Pre-implementation review**
   - Technical reviewer confirms the change does what it claims and
     has acceptable test coverage
   - QA reviewer confirms documentation updates are scoped correctly
   - Approver gives go-ahead before any code is written for Class A
     changes; review-after-implementation is acceptable for B and C

3. **Implement**
   - Branch from `main`; one branch per change request
   - Commit messages reference the change request ID (e.g.,
     `CC-2026-05-27-001: ...`)
   - All commits are signed-off (`git commit --signoff`)
   - Add or update tests; the full suite must pass before merge

4. **Update documentation in the same PR**
   - For Class A: URS / FS / traceability matrix as appropriate
   - For Class B: traceability matrix row for the new component
   - For Class C: the document itself + the audit log row at the
     bottom of the affected doc

5. **Post-implementation audit event**
   - A `TEMPLATE_APPROVED` / `MODE_LOCKED` / similar audit event is
     emitted via the AuditSink with:
     - `actor_id` = approver
     - `reason` = change control reference (e.g., `CC-2026-05-27-001`)
     - `target_type` / `target_id` = the artifact touched

6. **Validation evidence**
   - For Class A changes affecting `services/audit/`, the regression
     suite under `tests/test_audit_*.py` must run green and the
     results archived
   - For Class A changes affecting `services/generation_orchestrator/`,
     the end-to-end test (`tests/test_orchestrator_end_to_end.py`)
     must run green AND a manual end-to-end demo run (`python
     samples/run_synthetic_ib.py --anchor`) must produce a valid
     anchor

## 6. Template lifecycle

Approved templates (`status: approved`) are immutable. To change one:

1. Clone the approved template under a new version (e.g.,
   `ich_e6_ib_v0.2.0`) via `LibraryAdapter.load(template_id,
   options=LibraryAdapterOptions(clone_as_template_id=...))`
2. Edit the clone (status: draft)
3. Submit for QA review; on approval, mark `status: approved`
4. Retire the previous version: set the old template's status to
   `deprecated` (this is allowed because the old version's
   `template_id+version` key is preserved unchanged in the audit
   trail)

ReportInstances persist their `template_id + template_version` so a
mid-flight migration to a new template version does NOT silently
re-run prior instances — they remain associated with the version they
were generated against.

## 7. Emergency changes

In an incident (production outage, regulatory finding, data
integrity issue):

1. The on-call engineer may bypass the pre-implementation review for
   the minimum change required to restore service or data integrity
2. A retroactive change request must be filed within 24 hours
3. QA / approver review must occur within 5 business days
4. The audit log captures the emergency-change actor + reason via a
   `MODE_LOCKED` or `EXPORT_PERFORMED` event with
   `reason="emergency:<incident-id>"`

## 8. Periodic review

Every 12 months (or after any incident), the QA team reviews:
- All Class A changes in the prior period
- Deviation log entries
- Audit chain spot-checks (run `samples/audit_dashboard.py
  --project-id <pilot> --anchor <latest>`)
- Cost / latency / failure-rate metrics against URS-091 / URS-092

The review produces a periodic-review report that becomes part of
the next year's CSV evidence package.

## 9. Approval of this SOP

| Role | Name | Signature | Date |
|---|---|---|---|
| QA representative | _TBD_ | | |
| Compliance | _TBD_ | | |
| Technical owner | Chad Alburn | | |

## 10. Revision history

| Version | Date | Change | CC ref |
|---|---|---|---|
| 0.1.0 | 2026-05-26 | Initial draft | (baseline) |
