---
name: code-debugging
description: Diagnose and fix code failures, failing tests, stack traces, regressions, or broken behavior.
metadata:
  nanobot:
    id: builtin-code-debugging
    version: 1.0.0
    status: verified
    category: coding.debug
    risk_level: medium
    requires_exec: true
    required_tools:
      - read_file
      - grep
      - exec
    triggers:
      - fix this bug
      - tests are failing
      - debug this error
      - stack trace
      - regression
---

# Code Debugging

## Method

1. Reproduce or inspect the failure before editing when feasible.
2. Trace the smallest responsible code path.
3. Make the narrowest change that fixes the behavior.
4. Add or update focused tests when risk justifies it.
5. Run the relevant verification and report anything that could not be run.
