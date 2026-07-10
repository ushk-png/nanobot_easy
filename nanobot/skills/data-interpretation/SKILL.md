---
name: data-interpretation
description: >
  Interpret already-computed metrics, charts, tables, or experiment results and
  explain what they mean. Triggers: "interpret these numbers", "what does this
  chart mean", "explain these metrics", "read this table".
metadata:
  nanobot:
    id: builtin-data-interpretation
    version: 1.0.0
    status: verified
    category: data.interpretation
    risk_level: low
    requires_exec: false
    conflicts_with:
      - data-analysis
    triggers:
      - interpret these numbers
      - what does this chart mean
      - explain these metrics
      - read this table
      - what should I conclude from this data
---

# Data Interpretation

## When To Use

- The user provides metrics, chart values, or a table and asks what they mean.
- The task is explanation and implication, not computation.

## When Not To Use

- Use `data-analysis` when raw files must be loaded, cleaned, or calculated.
- Use `research-synthesis` when the input is multiple textual sources.

## Method

1. Identify the metric or table dimensions.
2. Explain the main pattern before details.
3. Call out uncertainty, sample size, missing baseline, and possible confounders.
4. Separate observation from recommended action.
5. Suggest one follow-up analysis if the conclusion is uncertain.

## Failure Rules

- If units, denominators, or time ranges are missing, state the assumption and
  ask for clarification only if it changes the conclusion.
