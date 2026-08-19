---
report_type: ib_nonclinical_sections
title: Investigator's Brochure — nonclinical sections
version: 0.1.0
description: >
  The nonclinical content of an Investigator's Brochure (ICH E6 sections 2–3):
  physicochemical/formulation properties and the nonclinical pharmacology, PK,
  and toxicology summaries. Bridges the preclinical data into the IB shell.
owner: nonclinical / medical-writing

tags:
  domain: pre_clinical
  discipline: [pharmacology, dmpk, nonclinical_safety]
  compliance: non_gxp
  document_class: regulatory_component
  modality: [small_molecule]

inputs:
  - id: compound_id
    prompt: Compound / programme identifier
    required: true
  - id: product_name
    prompt: Product / compound display name
    required: true

sources:
  - id: cmc
    type: bigquery
    dataset: cmc
    query_id: physchem_formulation_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: nonclinical_pk
    type: bigquery
    dataset: dmpk
    query_id: nonclinical_pk_summary_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: pivotal_tox
    type: bigquery
    dataset: nonclinical_safety
    query_id: pivotal_tox_summary_v2
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: pharmacology_docs
    type: file
    filter_tags: [nonclinical, pharmacology]
  - id: target_rationale
    type: confluence
    space: PSS
    cql: 'title ~ "{{inputs.product_name}} target rationale"'

citation:
  required: true
  granularity: claim
  min_per_paragraph: 1

output:
  formats: [docx, html]
---

# {{inputs.product_name}} — IB nonclinical sections

## 2. Physical, chemical and pharmaceutical properties and formulation

> Instruction: Describe chemical structure/class, key physicochemical
> properties, formulation, and storage. Report constants from the CMC source
> verbatim.
> Sources: cmc
> Table: cmc

## 3.1 Nonclinical pharmacology

> Instruction: Summarise primary pharmacodynamics (mechanism, potency,
> selectivity, efficacy models), secondary pharmacodynamics, and safety
> pharmacology. Draw on the pharmacology documents and target rationale.
> Sources: pharmacology_docs, target_rationale
> Table: (none)

## 3.2 Pharmacokinetics and product metabolism in animals

> Instruction: Summarise ADME and PK parameters across species (Cmax, AUC, t½,
> CL, Vd, F). Insert the nonclinical PK table verbatim.
> Sources: nonclinical_pk
> Table: nonclinical_pk

## 3.3 Toxicology

> Instruction: Summarise single- and repeat-dose toxicology, genotoxicity, and
> reproductive/other toxicity as available. Report NOAELs, target organs, and
> reversibility; insert the pivotal toxicology table verbatim.
> Sources: pivotal_tox
> Table: pivotal_tox
