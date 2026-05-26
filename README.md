# Report Generator Agent

AI-powered research report generation for clinical/pharma/regulatory teams. Ingests source data from heterogeneous systems (DBs, files, cloud storage, LIMS/EDC), populates a structured template, and produces a draft report as a Google Doc for collaborative review — with Word and PDF as final delivery formats. Every fact is cited back to its source.

**Pilot target:** Investigator's Brochure (ICH E6), one team, ~3 months.

## Architecture

See [docs/architecture-plan.md](docs/architecture-plan.md) for the full architecture, compliance model, and rollout plan.

Key pillars:
- **Hosting:** GCP — Vertex AI + Cloud Run + GCS + Firestore + Vertex Vector Search
- **LLM:** Claude (Sonnet 4.6 fill, Opus 4.7 plan/critique) on Vertex AI Model Garden
- **Working doc:** Google Docs; delivery: Word + PDF
- **Compliance modes:** selectable per report type — R&D / GxP-aware / Validated (Part 11)
- **Citations:** every fact cited to source (PDF page, DB row, query)

## Repository layout

```
Report-Generator-Agent/
├── docs/                          # architecture plan, design notes
├── shared/                        # cross-service Python packages
│   └── schemas/                   # CanonicalDocument, Citation, Template
├── services/                      # 7 Cloud Run services
│   ├── api-gateway/
│   ├── ingestion-service/
│   ├── parsing-service/
│   ├── template-service/
│   ├── generation-orchestrator/   # plan -> fill -> critique loop
│   ├── citation-service/
│   └── document-renderer/
├── templates/library/             # shipped JSON templates (ICH E6 IB, etc.)
├── samples/                       # synthetic test corpus
├── infra/terraform/               # GCP infrastructure-as-code (added later)
└── tests/
```

## Status

**Phase 1 — Local PoC.** Build and validate the generation loop on a local machine using sample data before any GCP work begins. Stakeholder approvals (GCP project, Workspace DWD, Vertex BAA) tracked in [docs/architecture-plan.md](docs/architecture-plan.md).

## Development

Requires Python 3.11+.

```powershell
# Create virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install (editable)
pip install -e ".[dev]"

# Run tests
pytest
```
