---
name: pros-cons-decision
description: >
  Build a concise pros/cons decision note for a practical choice, including
  recommendation, assumptions, and risks. Triggers: "pros and cons", "decision
  note", "help me decide", "recommend between these options".
metadata:
  nanobot:
    id: builtin-pros-cons-decision
    version: 1.0.0
    status: verified
    category: decision.pros_cons
    risk_level: low
    requires_exec: false
    conflicts_with:
      - compare-options
      - answer-comparison
    triggers:
      - pros and cons
      - decision note
      - help me decide
      - recommend between these options
      - should we choose
---

# Pros/Cons Decision

## When To Use

- The user wants a decision note with pros, cons, recommendation, and risks.
- The alternatives are concrete enough to evaluate.

## When Not To Use

- Use `answer-comparison` for conceptual difference explanations.
- Use `compare-options` for a criteria-heavy comparison matrix.

## Method

1. State the decision and alternatives.
2. List pros and cons for each option using the user's constraints.
3. Add assumptions and risks.
4. Recommend one option when evidence is sufficient.
5. Identify the trigger that would change the recommendation.

## Failure Rules

- If the decision criteria are missing, use common criteria and mark them as
  assumptions.
