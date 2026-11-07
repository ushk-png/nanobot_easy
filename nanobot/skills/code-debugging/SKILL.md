---
name: code-debugging
description: Diagnose and fix code failures, failing tests, stack traces, regressions, or broken behavior.
metadata:
  nanobot:
    id: builtin-code-debugging
    version: 1.0.2
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
2. Define the failing behavior and a check that would demonstrate the fix.
   Reproduce or inspect the failure before editing when feasible.
3. Trace the smallest responsible code path. Distinguish observed evidence from
   hypotheses; resolve uncertainty from code and docs before asking the user.
4. Make the narrowest change that fixes the behavior.
5. Add or update focused tests when risk justifies it and the project already
   has a suitable test pattern.
6. Run the closest relevant verification first, then broaden only if needed.
7. Inspect the diff for unrelated changes and report actual verification results.

## Done Criteria

- The requested failure has been addressed and the relevant check has run, or
  the result is explicitly marked partial or blocked with the missing evidence.
- Report the original failure, the change, and observed verification results.
  A check not run or a failure not reproduced must not be reported as passing.
- The diff contains only changes needed for the fix and its verification.

## Failure Rules

- If the failure cannot be reproduced, inspect available logs, tests, and code
  before asking for missing evidence. Do not guess a patch without evidence of
  the cause; report the observed state and the specific remaining blocker.
- State material assumptions and proceed with small, reversible implementation
  choices. Ask when the answer would materially change the required behavior,
  public API, data handling, or authorization.
- If verification fails, separate failures caused by the change from pre-existing
  or unrelated failures.
- Do not fix unrelated bugs or broken tests; mention them as residual risk.
- If a patch fails because the file changed, re-read the relevant range and retry
  once with a smaller edit.
