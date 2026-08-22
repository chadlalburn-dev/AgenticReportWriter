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

1. ~~**Tables carry real data.**~~ **Done.** Ids repointed,
   `safety_pharmacology_v1` / `genotoxicity_summary_v1` / `exposure_margin_v1`
   authored, three tables seeded. All six bindings of
   `nonclinical_safety_summary` resolve and are cited; five of six sections pass
   their checks, against two of six before. `> Table:` is still advisory — the
   filler renders every resolved binding, which is the behaviour we want, so the
   directive is documentation rather than a switch. Left as is.
2. ~~**Visualisations.**~~ **Done for `bar` and `margin`.** `> Visual:` parses
   to a `VisualSpec`, rendered as deterministic inline SVG from
   `LedgerRow.typed_rows`. `line` and `scatter` are declared in `VisualKind` and
   raise "not implemented yet" — next tick.
3. ~~**Editor exposes the guidance.**~~ **Done.** The Instruction textarea was
   already there; a Figure field sits beside it and the writer round-trips
   `> Visual:` (it silently dropped it at first, which the round-trip test
   caught — saving an unedited template would have deleted the figure).
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


## Still open, in order

4. **Connectors.** Kinds today: `bigquery`, `confluence`, `file`, `api`. Needed:
   Oracle, and SharePoint/OneDrive PPT. Plus an in-app config surface, and
   per-compound/target default source sets.
5. **Evidence in several places.** A run still takes one `evidence_folder`.
6. **`line` and `scatter`** chart kinds.
7. **The rest of the query registry.** Sixteen `query_id` references across the
   template library resolve to nothing, and nothing tells you until a run
   half-fails. Two things: author the queries, and make an unresolvable
   reference visible in the editor at authoring time. The second matters more —
   you cannot configure data connections safely if a broken one is invisible.
   Remaining: `assay_potency_selectivity_v1`, `dmpk_summary_v1`,
   `developability_metrics_v1`, `compound_identity_v1`, `headline_potency_v1`,
   `headline_pk_v1`, `headline_noael_v1`, `invitro_adme_v1`,
   `metabolite_profile_v1`, `human_pk_projection_v1`, `physchem_formulation_v1`,
   `target_screening_summary_v1`, `assay_potency_summary_v1`.

## Test-infrastructure follow-up

`tests/test_no_internal_vocabulary.py` mines the real run store in `var/` for a
run with a failed critique, and asserts loudly when it finds none. Fixing
`must_cite_every_number` removed the reason most sections were failing, so the
suite broke because the app improved. The scan window is gone, which fixes it
today; the coupling is not. The fixture should build a run with a deliberately
unsatisfiable citation policy rather than depending on development data. Several
other page tests read the same store and share the fragility.
