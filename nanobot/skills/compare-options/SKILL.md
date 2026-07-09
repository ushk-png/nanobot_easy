---
name: compare-options
description: Compare alternatives and recommend a choice using criteria, tradeoffs, and constraints.
metadata:
  nanobot:
    id: builtin-compare-options
    version: 1.0.0
    status: verified
    category: decision.compare
    risk_level: low
    requires_exec: false
    triggers:
      - compare these options
      - A vs B
      - which should I choose
      - pros and cons
---

# Compare Options

## Method

1. Extract the alternatives and the user's constraints.
2. Pick comparison criteria that matter for this situation.
3. Compare tradeoffs without pretending uncertain facts are certain.
4. Recommend one option when enough information exists; otherwise ask for the missing criterion.
