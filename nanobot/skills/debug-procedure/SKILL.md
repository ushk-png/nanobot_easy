---
name: debug-procedure
description: >
  Create a safe debugging plan for a failure, flaky behavior, or investigation
  without directly modifying code. Triggers: "debugging plan", "how should I
  debug", "investigation steps", "triage this failure", "narrow down this bug".
metadata:
  nanobot:
    id: builtin-debug-procedure
    version: 1.0.1
    status: verified
    category: coding.debug_plan
    risk_level: low
    requires_exec: false
    conflicts_with:
      - code-debugging
      - code-review
      - code-modify
      - answer-diagnosis
    triggers:
      - give me a debugging plan
      - debugging plan
      - how should I debug
      - investigation steps
      - triage this failure
      - narrow down this bug
---

# Debug Procedure

## When To Use

- The user wants a plan to debug a software failure.
- The user has symptoms but has not asked the agent to edit or execute code.

## When Not To Use

- Use `code-debugging` when direct repo inspection, command execution, or file
  edits are requested to fix a failure.
- Use `code-modify` when the user asks for direct implementation, refactoring, or
  code changes rather than a debugging plan.
- Use `answer-diagnosis` for non-code or general operational diagnosis.

## Method

1. State the failure signal and the suspected boundary.
2. Include a check for repository instructions and existing test/build commands
   before any proposed edits.
3. Define the cheapest reproduction or observation step.
4. Split the search into hypotheses with one confirming check each.
5. Recommend instrumentation only after cheaper checks.
6. End with stop conditions: what result proves the cause or changes direction.

## Failure Rules

- If logs, error text, or repro steps are missing, ask for the one artifact that
  would most reduce uncertainty.
- If the user asks to proceed from planning into code changes, switch to
  `code-debugging` for failure repair or `code-modify` for general changes.
