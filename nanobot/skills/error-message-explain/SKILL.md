---
name: error-message-explain
description: >
  Explain an error message, stack trace excerpt, status code, or command failure
  in plain language with likely meaning and next checks. Triggers: "what does
  this error mean", "explain this stack trace", "what is ENOENT", "HTTP 403
  meaning".
metadata:
  nanobot:
    id: builtin-error-message-explain
    version: 1.0.0
    status: verified
    category: answer.error_explain
    risk_level: low
    requires_exec: false
    conflicts_with:
      - answer-diagnosis
      - code-debugging
    triggers:
      - what does this error mean
      - explain this stack trace
      - what is ENOENT
      - HTTP 403 meaning
      - explain this command failure
---

# Error Message Explain

## When To Use

- The user asks what an error message means.
- The user wants a plain-language explanation and likely next checks.

## When Not To Use

- Use `answer-diagnosis` when the user asks for root-cause analysis across
  symptoms.
- Use `code-debugging` when the user asks the agent to inspect or fix code.
- Use `answer-comparison` when the user asks about the meaning or tradeoffs of
  two non-error concepts, tools, or styles.

## Method

1. Translate the error into plain language.
2. Identify the component that produced it if visible.
3. Explain the most likely causes.
4. Give two or three low-cost next checks.
5. Avoid pretending to know the exact cause without evidence.

## Failure Rules

- If the error text is incomplete, explain the visible part and ask for the full
  command, stack trace, or surrounding log line.
