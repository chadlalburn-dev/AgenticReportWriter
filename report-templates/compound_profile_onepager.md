---
report_type: compound_profile_onepager
title: Compound Profile (one-pager)
version: 0.1.0
description: >
  A single-page snapshot of a compound — identity, headline pharmacology, PK,
  safety, and current status — for fast internal sharing and reviews.
owner: preclinical-project-team

inputs:
  - id: compound_id
    prompt: Compound / programme identifier
    required: true

sources:
  - id: identity
    type: bigquery
    dataset: compound_registry
    query_id: compound_identity_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: key_pharm
    type: bigquery
    dataset: preclinical_assays
    query_id: headline_potency_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: key_pk
    type: bigquery
    dataset: dmpk
    query_id: headline_pk_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: key_safety
    type: bigquery
    dataset: nonclinical_safety
    query_id: headline_noael_v1
    params: { compound_id: "{{inputs.compound_id}}" }
  - id: status_note
    type: confluence
    space: PSS
    cql: 'label = "{{inputs.compound_id}}" and label = "status"'

citation:
  required: true
  granularity: claim
  min_per_paragraph: 1

output:
  formats: [html, confluence_page]
---

# Compound Profile — {{inputs.compound_id}}

## Identity

> Instruction: One short paragraph — chemical class, target, proposed
> indication, current stage.
> Sources: identity, status_note
> Table: identity

## Key pharmacology

> Instruction: 2–3 sentences on potency/selectivity; headline numbers only.
> Sources: key_pharm
> Table: key_pharm

## Key PK

> Instruction: 2–3 sentences on the headline PK profile (species, F, t½).
> Sources: key_pk
> Table: key_pk

## Key safety

> Instruction: 2–3 sentences — highest-tier NOAEL(s), target organs, margin.
> Sources: key_safety
> Table: key_safety

## Status

> Instruction: One paragraph on current status, next milestone, and any open
> risks — drawn from the status note.
> Sources: status_note
> Table: (none)
