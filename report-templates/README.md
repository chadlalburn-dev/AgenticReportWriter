# Report templates → report skills

A **report template** is a single Markdown document (this folder holds one
per report type) that fully defines how to produce that report: what inputs
it needs, which data sources it pulls from (BigQuery, Confluence, files),
and what each section should contain. Each template is designed to be
driven by a thin **report skill** (see `SKILL.template.md`).

```
report-templates/
├── README.md                          ← this file (the format)
├── SKILL.template.md                  ← scaffold: how a skill runs a template
├── candidate_selection_dossier.md     ← worked example (BigQuery + Confluence)
└── compound_profile_onepager.md       ← worked example (lighter)
```

## How a template becomes a running report

1. A user invokes the report skill (e.g. *"generate a candidate selection
   dossier for GVR-12345"*).
2. The skill loads the named template, resolves the **inputs** (e.g.
   `compound_id`).
3. For each **source**, it pulls the data:
   - `bigquery` → runs a pre-approved named query (or inline SQL through the
     safety gate) via the `QueryExecutor`. Returns tables, verbatim.
   - `confluence` → fetches page(s) by CQL search or page id via the
     Confluence connector. Returns narrative context.
   - `file` → pulls documents by path/tag via the ingestion connectors.
4. For each **section**, it drafts prose citing the pulled data. Deterministic
   tables (BigQuery results) are inserted verbatim — the model narrates around
   them and never re-derives numbers. Every quantitative claim carries a
   citation to its source (query id + row, or Confluence page).
5. It renders the output (docx / html / Confluence page) and writes a
   provenance log.

## Template format

Front-matter (YAML) carries the machine-readable contract; the body carries
the human-readable section outline + per-section guidance.

```yaml
---
report_type: candidate_selection_dossier   # stable slug (also the skill name)
title: Candidate Selection Dossier
version: 0.1.0
description: One-line summary of the report's purpose.
owner: preclinical-project-team

inputs:                        # parameters the user supplies at run time
  - id: compound_id
    prompt: Compound / programme identifier
    required: true

sources:                       # where data comes from
  - id: potency
    type: bigquery
    dataset: <bq_dataset>      # e.g. preclinical_assays
    query_id: assay_potency_summary_v1   # a pre-approved named query (preferred)
    params: { compound_id: "{{inputs.compound_id}}" }

  - id: target_rationale
    type: confluence
    space: PSS
    cql: 'title ~ "{{inputs.compound_id}} target rationale"'   # search…
    # page_id: "123456"                                        # …or a specific page

  - id: prior_reports
    type: file
    filter_tags: [nonclinical, prior_report]

citation:
  required: true
  granularity: claim           # claim | paragraph | section
  min_per_paragraph: 1

output:
  formats: [docx, html]        # docx | html | confluence_page
---
```

### Body: section outline

Each `##` heading is a section. Under it, three optional directives:

```markdown
## 2. Target and rationale

> Instruction: Summarise the target, disease rationale, and why this compound
> was progressed. 2–3 paragraphs. Formal, factual.
> Sources: target_rationale, prior_reports
> Table: (none)

## 4. Safety / toxicology

> Instruction: Summarise pivotal toxicology — species, durations, NOAELs,
> target organs, exposure margins. Cite each value to its source study.
> Sources: pivotal_tox
> Table: pivotal_tox        # insert this BigQuery result verbatim as a table
```

- **Instruction** — what the section should say (becomes the model's prompt).
- **Sources** — which `source.id`s feed this section (data scoping + citations).
- **Table** — a `bigquery` source id to render verbatim as a table (optional).

## Source types

| type | pulls from | how (existing engine piece) | returns |
|---|---|---|---|
| `bigquery` | BigQuery datasets | `QueryExecutor` + named-query registry / SQL safety gate | tables (verbatim, cited) |
| `confluence` | Confluence spaces/pages | Confluence `ApiConnector` behind `ApiCallGate` | narrative context (cited to page) |
| `file` | uploaded/stored docs | ingestion connectors + parsers | chunks (cited to page/section) |

Extending to a new source (SharePoint, an internal API, a LIMS) = add one
`ApiConnector` — no template-format change.

## Authoring rules of thumb

- **Prefer named queries** over inline SQL — they're pre-approved, versioned,
  and skip the per-run approval gate.
- **Keep numbers in `Table:` sources**, not in prose the model writes — that's
  what guarantees the report can't fabricate a value.
- **One template = one report type = one skill.** Reuse sources across
  templates rather than duplicating queries.
