---
name: data-analysis
description: Inspect tabular or structured data, compute summaries, find anomalies, or explain trends.
metadata:
  nanobot:
    id: builtin-data-analysis
    version: 1.0.0
    status: verified
    category: data.analysis
    risk_level: medium
    requires_exec: true
    required_tools:
      - read_file
      - exec
    triggers:
      - inspect this spreadsheet
      - calculate totals
      - analyze this csv
      - summarize this dataset
      - find anomalies
      - calculate metrics
---

# Data Analysis

## Method

1. Inspect schema, row counts, missing values, and obvious data quality issues.
2. Compute only the metrics needed for the user's question.
3. State assumptions about filters, units, and date ranges.
4. Prefer reproducible calculations and mention the verification command or script.
