# Data connections, tables and visualisations — plan

Grounded in what the repo already does, so the work is additive rather than a
rebuild. Findings from reading the code first, before writing any:

| Asked for | State found | Work needed |
| --- | --- | --- |
| Real parsed data in tables in the document | **Already renders.** `run_titanium.html` emits `<table class="ti-table">` from `s.tables` with real columns and rows. | Nothing in the renderer. The tox sections were empty because the *queries* were missing — see below. |
| Visualisations | **Absent entirely.** No chart code, no spec, no directive. | The largest genuine gap. |
| Configure data connections on existing and new templates | `report-templates/*.md` front-matter already declares `sources:` and the editor already has per-source rows with four kinds. | More kinds; expose in editor; per-compound defaults. |
| Evidence in more than one folder | A run carries one `evidence_folder`. | Multiple roots. |
| Build connectors in-app (BigQuery, Oracle, PPT in OneDrive) | `ApiConnector` protocol, `ApiConnectorRegistry`, `ApiCallGate`, `bigquery_executor.py` all exist. Connectors are registered **in code**, not configurable in-app. | Config surface + new kinds. |
| Connectors on/off per section, or per compound/target | `> Sources: a, b` per section already works. | Per-compound/target defaults. |
| Expose the MD guidance for building a section | `> Instruction:` in the .md already becomes `GenerationPolicy.prompt_template`. | Surface it in the editor for editing. |
| Deterministic structure + a paired visualisation of fixed type | `> Table:` exists but is advisory only. | Make it authoritative; add `> Visual:`. |

## Root cause of the empty report

`nonclinical_safety_summary.md` references four BigQuery-kind sources. Only one
resolves:

| Template asks for | Registry has | |
| --- | --- | --- |
| `pivotal_tox_summary_v2` | `pivotal_toxicology_summary_v2` | name mismatch |
| `exposure_margin_v1` | `exposure_summary_v2` | different query, and a margin is not a summary |
| `safety_pharmacology_v1` | — | missing, and no table backs it |
| `genotoxicity_summary_v1` | — | missing, and no table backs it |

`samples/synthetic_compound/edc.sqlite` holds `pivotal_tox` (4 rows, NOAEL and
target organs), `nonclinical_pk` (6), `exposure_summary` (7), `clinical_pk_ss`
(5), `ae_events_by_soc` (8). Nothing for safety pharmacology or genotoxicity.

So "no tables in the document" was never a rendering problem. Four of six
sections correctly reported that no data was retrieved.

## Slices, each confirmable in the app

1. **Tables carry real data.** Fix the id mismatches, author the genuinely
   missing queries, add the two missing synthetic tables. Make `> Table:`
   authoritative rather than advisory.
2. **Visualisations.** A `> Visual:` directive → a `VisualSpec` on the section →
   a server-rendered inline **SVG**. No CDN, no build step, works with
   JavaScript off, same spec always yields the same design. Chart values come
   from the resolved query result, never from the model.
3. **Editor exposes the guidance.** The section Instruction becomes an editable
   markdown field; Table and Visual are pickable per section; everything
   round-trips to the .md.
4. **Connectors.** New kinds — Oracle, SharePoint/OneDrive PPT — plus an in-app
   config surface where each declares what it needs and reports honestly when
   it cannot reach it. Per-compound/target default source sets, overridable per
   section.
5. **Evidence in several places.** A run takes more than one root.

## What cannot be verified here, and will say so in the app

BigQuery, Oracle and OneDrive all need credentials and network paths this
machine does not have: GSK's VPC-SC perimeter blocks self-service, and this repo
never uses API keys. Those connectors get built to the same protocol, with
configuration and a connection test that reports *unreachable from here* rather
than pretending. The SQLite executor stays the one that actually runs, and the
app names which is which — the same rule the engine chip already follows.
