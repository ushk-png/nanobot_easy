---
name: code-debugging
description: Diagnose and fix code failures, failing tests, stack traces, regressions, or broken behavior.
metadata:
  nanobot:
    id: builtin-code-debugging
    version: 1.0.1
    status: verified
    category: coding.debug
    risk_level: medium
    requires_exec: true
    required_tools:
      - read_file
      - grep
      - exec
    conflicts_with:
      - code-review
      - debug-procedure
      - code-modify
    triggers:
      - find the regression
      - run the failing command
      - repair the implementation
      - fix this bug
      - tests are failing
      - debug this error
      - stack trace
      - regression
---

# Code Debugging

## When To Use

- The user asks to diagnose and fix a concrete code failure, failing test,
  stack trace, regression, or broken behavior.
- The task requires inspection, command execution, or file edits to repair a
  failure.

## When Not To Use

- Use `code-review` when the user wants review findings without edits.
- Use `debug-procedure` when the user wants a debugging plan only.
- Use `code-modify` for planned feature implementation, ordinary refactoring,
  or direct code changes not centered on a reproduced failure.

## Method

1. Read applicable repository instructions such as `AGENTS.md`,
   `CONTRIBUTING.md`, README, or test/build configuration when present.
2. Reproduce or inspect the failure before editing when feasible.
3. Trace the smallest responsible code path.
4. Make the narrowest change that fixes the behavior.
5. Add or update focused tests when risk justifies it and the project already
   has a suitable test pattern.
6. Run the closest relevant verification first, then broaden only if needed.
7. Report anything that could not be run.

## Failure Rules

- If the failure cannot be reproduced, do not guess a patch; report the observed
  state and ask for the missing log, command, or input that would narrow it.
- If verification fails, separate failures caused by the change from pre-existing
  or unrelated failures.
- Do not fix unrelated bugs or broken tests; mention them as residual risk.
- If a patch fails because the file changed, re-read the relevant range and retry
  once with a smaller edit.
