---
name: answer-howto
description: >
  Give practical step-by-step instructions for a task the user can perform.
  Triggers: "how do I", "walk me through", "what are the steps", "setup steps",
  "how should I configure".
metadata:
  nanobot:
    id: builtin-answer-howto
    version: 1.0.0
    status: verified
    category: answer.howto
    risk_level: low
    requires_exec: false
    conflicts_with:
      - answer-comparison
      - answer-diagnosis
    triggers:
      - how do I
      - walk me through
      - what are the steps
      - setup steps
      - how should I configure
---

# Answer How-To

## When To Use

- The user asks for instructions, setup steps, configuration flow, or a practical
  procedure.
- The answer can be given safely without executing commands or changing files.

## When Not To Use

- Use `answer-comparison` for conceptual differences.
- Use `answer-diagnosis` when the user asks why something is failing.
- Use an execution-capable coding or setup skill when the user asks the agent to
  make changes directly.

## Method

1. State prerequisites first.
2. Provide ordered steps in the shortest sequence that works.
3. Include verification checks after meaningful steps.
4. Call out irreversible, costly, or security-sensitive steps before the user
   reaches them.
5. End with the next action the user should take.

## Failure Rules

- If the environment or target platform is unknown and it changes the steps,
  ask one concise clarifying question.
