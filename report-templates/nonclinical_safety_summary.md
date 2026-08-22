---
report_type: nonclinical_safety_summary
title: Nonclinical Safety / Toxicology Summary
version: 0.1.0
description: >
  Integrated nonclinical safety summary — pivotal toxicology across species and
  durations, safety pharmacology, genotoxicity, and exposure margins vs the
  projected efficacious exposure.
owner: nonclinical-safety

tags:
  domain: pre_clinical
  discipline: [nonclinical_safety]
  compliance: non_gxp
  document_class: technical_summary
  modality: [small_molecule]

inputs:
  - id: compound_id
    prompt: Compound / programme identifier
    required: true

sources:
  - id: pivotal_tox
    type: bigquery
    dataset: nonclinical_safety
    query_id: pivotal_toxicology_summary_v2
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: safety_pharm
    type: bigquery
    dataset: nonclinical_safety
    query_id: safety_pharmacology_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: genetox
    type: bigquery
    dataset: nonclinical_safety
    query_id: genotoxicity_summary_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: exposure_margins
    type: bigquery
    dataset: nonclinical_safety
    query_id: exposure_margin_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: study_context
    type: confluence
    space: PSS
    cql: 'label = "{{inputs.compound_id}}" and label = "toxicology"'
  - id: prior_reports
    type: file
    filter_tags: [nonclinical, toxicology]

citation:
  required: true
  granularity: claim
  min_per_paragraph: 1

output:
  formats: [docx, html]
---

# Nonclinical Safety Summary — {{inputs.compound_id}}

## 1. Overview and testing strategy

> Instruction: Summarise the nonclinical safety package and testing strategy —
> which species, durations, and study types were run and why. 1–2 paragraphs.
> Sources: study_context, prior_reports
> Table: (none)

## 2. Repeat-dose toxicology

> Instruction: Summarise pivotal repeat-dose toxicology by species and duration.
> Report NOAELs, target organs of toxicity, and reversibility. Cite each value
> to its source study. Insert the pivotal table verbatim.
> Sources: pivotal_tox
> Table: pivotal_tox

## 3. Safety pharmacology

> Instruction: Summarise cardiovascular (incl. hERG / QTc), respiratory, and CNS
> safety-pharmacology findings and their margins.
> Sources: safety_pharm
> Table: safety_pharm

## 4. Genotoxicity

> Instruction: Summarise the genotoxicity battery (Ames, chromosomal aberration/
> micronucleus) and conclusions.
> Sources: genetox
> Table: genetox

## 5. Exposure margins

> Instruction: Present exposure margins between the NOAEL exposures and the
> projected efficacious human exposure. Insert the margins table verbatim and
> interpret the therapeutic window.
> Sources: exposure_margins, pivotal_tox
> Table: exposure_margins
> Visual: margin binding=exposure_margins x=duration_text y=exposure_margin_x unit=x threshold=10 title="Exposure margin at NOAEL vs projected human AUC"

## 6. Safety conclusions and watch items

> Instruction: State the integrated safety conclusion, the principal risks /
> watch items, and any recommended de-risking studies. Defensible from
> sections 2–5.
> Sources: study_context
> Table: (none)
