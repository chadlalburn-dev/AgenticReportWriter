---
report_type: candidate_selection_dossier
title: Candidate Selection Dossier
version: 0.1.0
description: >
  Decision-support dossier presented at candidate selection / nomination —
  integrates pharmacology, DMPK, and safety data with target rationale and
  developability into a benefit/risk recommendation.
owner: preclinical-project-team

tags:
  domain: pre_clinical
  discipline: [pharmacology, dmpk, nonclinical_safety, developability]
  compliance: non_gxp
  document_class: internal_decision
  modality: [small_molecule]

inputs:
  - id: compound_id
    prompt: Compound / programme identifier (e.g. GVR-12345)
    required: true
  - id: target_name
    prompt: Molecular target (e.g. Kinase Z)
    required: true

sources:
  # --- BigQuery (deterministic data, pulled verbatim) ---
  - id: potency
    type: bigquery
    dataset: preclinical_assays        # map to your real dataset
    query_id: assay_potency_selectivity_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: dmpk
    type: bigquery
    dataset: dmpk
    query_id: dmpk_summary_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: pivotal_tox
    type: bigquery
    dataset: nonclinical_safety
    query_id: pivotal_tox_summary_v2
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: developability
    type: bigquery
    dataset: cmc
    query_id: developability_metrics_v1
    params: { compound_id: "{{inputs.compound_id}}" }

  # --- Confluence (narrative context, cited to page) ---
  - id: target_rationale
    type: confluence
    space: PSS
    cql: 'title ~ "{{inputs.target_name}} target rationale"'
  - id: project_background
    type: confluence
    space: PSS
    cql: 'label = "{{inputs.compound_id}}" and label = "background"'
  - id: prior_decisions
    type: confluence
    space: PSS
    cql: 'label = "{{inputs.compound_id}}" and label = "governance"'

  # --- Files (prior reports) ---
  - id: prior_reports
    type: file
    filter_tags: [nonclinical, prior_report]

citation:
  required: true
  granularity: claim
  min_per_paragraph: 1

output:
  formats: [docx, html]
---

# Candidate Selection Dossier — {{inputs.compound_id}}

## 1. Executive summary

> Instruction: One-page synthesis — what the compound is, the target and
> indication, the headline pharmacology / DMPK / safety findings, and the
> recommendation. Written last, from the sections below. Every quantitative
> claim carries a citation.
> Sources: potency, dmpk, pivotal_tox, target_rationale
> Table: (none)

## 2. Target and rationale

> Instruction: Summarise the molecular target, disease rationale, and why this
> compound was progressed against it. 2–3 paragraphs, formal and factual.
> Sources: target_rationale, project_background, prior_reports
> Table: (none)

## 3. Pharmacology

> Instruction: Summarise primary pharmacology — potency and selectivity against
> the target and key off-targets, and efficacy in disease models. Report IC50 /
> selectivity values from the potency source; cite each.
> Sources: potency, prior_reports
> Table: potency

## 4. DMPK / ADME

> Instruction: Summarise absorption, distribution, metabolism, excretion and
> key PK parameters across species. Report Cmax, AUC, t½, CL, F from the dmpk
> source verbatim; narrate around the table.
> Sources: dmpk
> Table: dmpk

## 5. Safety / toxicology

> Instruction: Summarise pivotal toxicology — species, study durations, NOAELs,
> target organs of toxicity, reversibility, and exposure margins vs the
> projected efficacious exposure. Cite each NOAEL/finding to its source study.
> Sources: pivotal_tox
> Table: pivotal_tox

## 6. Developability / CMC

> Instruction: Summarise developability — solubility, permeability, stability,
> and any formulation/synthesis flags that affect progression.
> Sources: developability
> Table: developability

## 7. Risks and mitigations

> Instruction: Enumerate the material risks (safety, DMPK, developability,
> target) and the proposed mitigation / de-risking experiments. Reference any
> risks already logged in prior governance decisions.
> Sources: prior_decisions, pivotal_tox, dmpk, developability
> Table: (none)

## 8. Recommendation

> Instruction: State the benefit/risk conclusion and the recommendation
> (progress / progress-with-conditions / hold), with the conditions or
> outstanding questions. Must be defensible from the data in sections 3–7.
> Sources: (synthesises the above)
> Table: (none)
