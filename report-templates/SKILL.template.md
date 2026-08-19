---
# Copy this per report type. Set name/description/template, then it's a skill.
name: <report-type-slug>          # e.g. candidate-selection-dossier
description: >
  Generate a <Report Title> for a given compound, pulling live data from
  BigQuery and context from Confluence, with every value cited to its source.
  Trigger when the user asks to "generate / draft a <report title> for
  <compound>", or invokes /<report-type-slug>.
template: report-templates/<report-type-slug>.md
---

# <Report Title> — report skill

Runs the `<report-type-slug>` report template end to end. The template is the
source of truth for inputs, data sources, and section structure; this skill is
the fixed procedure for executing it.

## 1. Load the template
Read `{{template}}`. Parse the front-matter (`inputs`, `sources`, `citation`,
`output`) and the `##` section outline with its `Instruction` / `Sources` /
`Table` directives.

## 2. Resolve inputs
Fill each `inputs` entry from the user's request. Ask **only** for required
inputs that are genuinely missing — don't re-ask for anything already given.

## 3. Pull every source
- **bigquery** — run the `query_id` (a pre-approved named query) via the
  `QueryExecutor`, or inline `sql` through the SQL safety gate (lint → dry-run →
  approval). Keep the result table **and** the `query_id` + row reference for
  citations. Never paraphrase numbers out of a table.
- **confluence** — fetch page(s) by `cql` search or `page_id` via the Confluence
  connector. Keep the page id + title for citations.
- **file** — pull + parse via the ingestion connectors (page/section metadata
  retained for citations).
- If a **required** source returns nothing, note the gap in the affected
  section rather than inventing content.

## 4. Draft section by section (in outline order)
For each `##` section:
- Use its `Instruction` as the prompt and draw **only** on its listed `Sources`.
- If it has a `Table:` directive, insert that BigQuery result **verbatim** as a
  table; write the narrative around it.
- Every quantitative claim (values, counts, dates, study ids, parameters)
  carries a citation to its source — a `query_id` + row for BigQuery, a page for
  Confluence, a page/section for files.

## 5. Validate before finalising
- Each citation-required section has cited claims (per the template's
  `granularity` / `min_per_paragraph`).
- No fabricated citations — every citation must map to a source actually pulled.
- Rendered tables match their source rows exactly.

## 6. Render + log
- Render to the template's `output.formats` (docx / html / confluence_page).
- Write a provenance log: sources pulled, query ids + params, Confluence pages,
  and the full citation list.

## Guardrails
- **Deterministic data is authoritative.** BigQuery tables are inserted as-is;
  the model narrates, it never re-derives, rounds, or "tidies" numbers.
- **Confluence is evidence, not fact** — quote and cite it; don't restate it as
  established fact without attribution.
- **Discovery research** — no GxP / CSV / change-control framing.
- Produce a **draft for human review**; the scientist owns the final document.
