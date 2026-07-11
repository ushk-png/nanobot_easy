---
name: code-review
description: >
  Review code changes or snippets for correctness, regressions, maintainability,
  security, and missing tests without editing files. Triggers: "review this code",
  "review this diff", "find issues in this PR", "code review comments".
metadata:
  nanobot:
    id: builtin-code-review
    version: 1.0.0
    status: verified
    category: coding.review
    risk_level: low
    requires_exec: false
    conflicts_with:
      - code-debugging
      - debug-procedure
    triggers:
      - review this code
      - review this diff
      - find issues in this PR
      - code review comments
      - check this patch
---

# Code Review

## When To Use

- The user asks for review findings on code, a diff, a pull request, or a patch.
- The user wants risks and defects identified before changes are made.

## When Not To Use

- Use `code-debugging` when the user asks the agent to reproduce, edit, or fix.
- Use `debug-procedure` when the user wants a debugging plan rather than review
  findings.
- Use `document-review` for prose documents, proposals, specs, or policy drafts
  that are not code, diffs, or pull requests.

## Method

1. Lead with findings ordered by severity.
2. Reference files, functions, or snippets when available.
3. Prioritize behavioral bugs, security issues, regressions, and missing tests.
4. Keep style-only feedback secondary.
5. If no issues are found, say so and name residual risks or test gaps.

## Failure Rules

- If there is no code or diff to review, ask for the relevant snippet or path.
