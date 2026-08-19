---
report_type: dmpk_adme_summary
title: DMPK / ADME Summary
version: 0.1.0
description: >
  Cross-species DMPK/ADME summary — absorption, distribution, metabolism,
  excretion, in vitro ADME, PK parameters, and the human PK / dose projection.
owner: dmpk

tags:
  domain: dmpk
  compliance: non_gxp
  document_class: technical_summary
  modality: [small_molecule]

inputs:
  - id: compound_id
    prompt: Compound / programme identifier
    required: true

sources:
  - id: invitro_adme
    type: bigquery
    dataset: dmpk
    query_id: invitro_adme_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: pk_params
    type: bigquery
    dataset: dmpk
    query_id: nonclinical_pk_summary_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: metabolism
    type: bigquery
    dataset: dmpk
    query_id: metabolite_profile_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: human_projection
    type: bigquery
    dataset: dmpk
    query_id: human_pk_projection_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: method_notes
    type: confluence
    space: PSS
    cql: 'label = "{{inputs.compound_id}}" and label = "dmpk"'

citation:
  required: true
  granularity: claim
  min_per_paragraph: 1

output:
  formats: [docx, html]
---

# DMPK / ADME Summary — {{inputs.compound_id}}

## 1. In vitro ADME

> Instruction: Summarise in vitro ADME — solubility, permeability (e.g. Caco-2),
> plasma protein binding, metabolic stability, and CYP inhibition/induction.
> Report values from the source verbatim.
> Sources: invitro_adme
> Table: invitro_adme

## 2. Pharmacokinetics (in vivo)

> Instruction: Summarise cross-species PK — Cmax, Tmax, AUC, t½, clearance,
> volume of distribution, and oral bioavailability. Note dose-proportionality.
> Sources: pk_params
> Table: pk_params

## 3. Metabolism

> Instruction: Summarise metabolic pathways, major circulating metabolites, and
> any metabolites of concern / cross-species coverage.
> Sources: metabolism, method_notes
> Table: metabolism

## 4. Human PK and dose projection

> Instruction: Summarise the projected human PK and the predicted efficacious
> dose/regimen and its assumptions. Insert the projection table verbatim.
> Sources: human_projection
> Table: human_projection

## 5. DMPK conclusions

> Instruction: State the integrated DMPK conclusion — developability from a PK
> standpoint, and any liabilities (clearance, metabolite, DDI). Defensible from
> sections 1–4.
> Sources: method_notes
> Table: (none)
