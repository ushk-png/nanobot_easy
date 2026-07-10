---
name: answer-diagnosis
description: >
  Diagnose likely causes from symptoms, errors, logs, or observed behavior
  without executing commands. Triggers: "why is this happening", "what caused
  this", "diagnose this", "what does this error mean", "possible root causes".
metadata:
  nanobot:
    id: builtin-answer-diagnosis
    version: 1.0.0
    status: verified
    category: answer.diagnosis
    risk_level: low
    requires_exec: false
    conflicts_with:
      - answer-comparison
      - answer-howto
      - error-message-explain
    triggers:
      - why is this happening
      - what caused this
      - diagnose this
      - what does this error mean
      - possible root causes
---

# Answer Diagnosis

## When To Use

- The user provides symptoms, an error, or unexpected behavior and asks for
  likely causes.
- The user wants reasoning and next checks, not direct code execution.

## When Not To Use

- Use `error-message-explain` when the user only wants the meaning of an error.
- Use `code-debugging` when the user asks the agent to inspect or change files.
- Use `answer-howto` when the user asks for setup instructions.

## Method

1. Restate the observed symptom in one sentence.
2. List likely causes ordered by probability and impact.
3. For each cause, give one cheap confirming check.
4. Separate evidence from inference.
5. Recommend the next diagnostic step.

## Failure Rules

- If there is not enough evidence, give a short differential diagnosis and ask
  for the one missing artifact that would narrow it most.
