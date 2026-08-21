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

### Which engine writes the prose

The generation pipeline — retrieval, citation enforcement, the safety gate, the
audit chain — is the same whichever model is behind it. Only the sentences
change. Three engines implement `LlmClient`:

| Engine | When it is used | What you get |
| --- | --- | --- |
| `ClaudeCliLlmClient` | The Claude Code CLI is installed **and signed in** | Real Claude prose |
| `StubLlmClient` | Fallback, and every test | Deterministic placeholder text |
| `VertexLlmClient` | Once GCP/Vertex access lands | Real Claude prose, sanctioned path |

`REPORTGEN_ENGINE` picks one: `auto` (default — prefer the CLI, fall back to
the stub), `cli` (fail loudly if it cannot run), or `stub`.

The app never hides which one produced a draft. The engine is named in the
header on every page, in the run-setup screen before you commit to a run, and
in the non-dismissible notice above the draft itself. If placeholder prose
could be mistaken for a model's words, the product's provenance claim is void,
so this disclosure is covered by tests rather than left to convention.

**To turn on real generation**, sign the CLI in once — the app cannot do this
for you, because the login is an interactive browser flow:

```bash
claude
```

then `/login` at the prompt. The app picks it up within a minute; no restart.
Until then it correctly falls back to the stub and says so. `find_claude_binary`
looks at `REPORTGEN_CLAUDE_BIN`, then `PATH`, then the Windows install
directory, so set that variable if the CLI lives somewhere unusual.

Two behaviours of the CLI are worth knowing, because both cost real debugging
and are pinned by tests in `tests/test_claude_cli_engine.py`:

- **An unauthenticated `claude -p` exits 0.** It prints "Not logged in · Please
  run /login" and returns success, so a client that trusts the exit code
  reports success on total failure. The output is inspected instead.
- **stdin must be closed explicitly**, or the CLI waits ~3s for piped input and
  writes a warning into the captured output.

#### Data-governance boundary

The local CLI routes prompt content to Anthropic through its own session, **not**
through GSK's sanctioned Onyx LLM path. For the synthetic `XYZ-001` corpus that
is fine — the data is fictional. Pointing it at real GSK preclinical data is a
decision for a human, so `ClaudeCliConfig.allow_real_data` defaults to `False`
and the client refuses to run when a caller flags the corpus as real. That
check is a tripwire against accident, not a DLP control, and says so in its own
docstring.

#### Where the engine is disclosed

Two different questions, deliberately kept apart:

- **What would the app use now?** The header chip on every page. It appears on
  pages that have no run at all.
- **What drafted the report in front of me?** The notice above the draft, the
  Runs list marker, and the first line of every markdown export — all read the
  run's own recorded `model_version`, never the ambient engine.

Confusing the two is a provenance bug, not a cosmetic one. The draft page
originally rendered the ambient answer, so a report drafted by Claude read
"PLACEHOLDER text from an offline stub" — and once the CLI is signed in the same
code would have labelled every *existing* stub-drafted report as the model's own
words. That second direction is what the split guards against: invented prose
sitting behind a real provenance claim.

Where the engine is unrecorded (runs written before the field existed), the app
claims the stub. Under-claiming costs a reader nothing; over-claiming voids the
only thing this product asserts. `tests/test_engine_provenance.py` pins both
directions.
