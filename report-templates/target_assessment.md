---
report_type: target_assessment
title: Target Assessment / Validation
version: 0.1.0
description: >
  Target assessment — biology and disease linkage, druggability, internal
  screening evidence, and the external competitive/clinical landscape — to
  support a target-validation or go/no-go decision.
owner: target-sciences

inputs:
  - id: target_name
    prompt: Molecular target (e.g. Kinase Z)
    required: true
  - id: indication_keyword
    prompt: Disease / indication keyword
    required: true

sources:
  # Internal narrative + screening evidence
  - id: target_rationale
    type: confluence
    space: PSS
    cql: 'title ~ "{{inputs.target_name}} target rationale"'
  - id: internal_screening
    type: bigquery
    dataset: preclinical_assays
    query_id: target_screening_summary_v1
    params: { target_name: "{{inputs.target_name}}" }
  # External evidence via the API connectors (ChEMBL, ClinicalTrials)
  - id: chembl_target
    type: api
    connector: mock_chembl
    endpoint: target_search
    params: { target_name: "{{inputs.target_name}}" }
  - id: chembl_mechanism
    type: api
    connector: mock_chembl
    endpoint: get_mechanism
    params: { target_chembl_id: "CHEMBL-MOCK-KINZ" }
  - id: competitor_trials
    type: api
    connector: mock_clinicaltrials
    endpoint: search_trials
    params: { condition: "{{inputs.indication_keyword}}" }

citation:
  required: true
  granularity: claim
  min_per_paragraph: 1

output:
  formats: [docx, html]
---

# Target Assessment — {{inputs.target_name}}

## 1. Target biology and disease rationale

> Instruction: Summarise the target's biology, its mechanistic link to the
> disease, and the therapeutic hypothesis. 2–3 paragraphs.
> Sources: target_rationale, chembl_target, chembl_mechanism
> Table: chembl_target

## 2. Druggability and mechanism

> Instruction: Assess druggability — modality fit, known chemical matter,
> mechanism of action of inhibitors/modulators. Cite external evidence.
> Sources: chembl_mechanism, chembl_target
> Table: (none)

## 3. Internal screening evidence

> Instruction: Summarise internal screening evidence for the target — hit rates,
> tractable series, key potencies. Insert the screening table verbatim.
> Sources: internal_screening
> Table: internal_screening

## 4. Competitive / clinical landscape

> Instruction: Summarise the external landscape — active and completed trials
> against this target/indication, sponsors, phases, and endpoints. Insert the
> trials table verbatim and note the competitive position.
> Sources: competitor_trials
> Table: competitor_trials

## 5. Assessment and recommendation

> Instruction: State the target-validation conclusion — strength of evidence,
> key uncertainties, and a go / conditional-go / no-go recommendation with the
> experiments that would resolve the main uncertainties.
> Sources: target_rationale
> Table: (none)
