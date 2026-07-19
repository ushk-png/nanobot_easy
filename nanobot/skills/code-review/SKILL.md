---
name: code-review
description: >
  Review code changes or snippets for correctness, regressions, maintainability,
  security, and missing tests without editing files. Triggers: "review this code",
  "review this diff", "find issues in this PR", "code review comments".
metadata:
  nanobot:
    id: builtin-code-review
    version: 1.0.1
    status: verified
    category: coding.review
    risk_level: low
    requires_exec: false
    conflicts_with:
      - code-debugging
      - debug-procedure
      - code-modify
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
- The user explicitly wants review only, not direct file edits.

## When Not To Use

- Reviewing a spec, proposal, config, guide, or other non-code document —
  use `document-review`.
- Use `code-modify` when the user asks the agent to implement, edit, refactor,
  or change files directly.
- Use `code-debugging` when the user asks the agent to reproduce, diagnose, and
  fix a failing behavior or test.
- Use `debug-procedure` when the user wants a debugging plan rather than review
  findings.

## Method

1. Check visible project rules, test conventions, or coding standards when they
   are provided with the code or diff.
2. Lead with findings ordered by severity.
3. Reference files, functions, or snippets when available.
4. Prioritize behavioral bugs, security issues, regressions, and missing tests.
5. Prefer minimal-diff recommendations; keep style-only feedback secondary.
6. If no issues are found, say so and name residual risks or test gaps.

## Failure Rules

- If there is no code or diff to review, ask for the relevant snippet or path.
- If test conventions are unclear, report test coverage risk without inventing a
  project-specific test command.
