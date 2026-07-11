---
name: compare-options
description: >
  Compare concrete alternatives and recommend a choice using explicit criteria,
  tradeoffs, and constraints. Use for constrained selection, not for general
  two-technology difference questions.
metadata:
  nanobot:
    id: builtin-compare-options
    version: 1.0.0
    status: verified
    category: decision.compare
    risk_level: low
    requires_exec: false
    conflicts_with:
      - answer-comparison
      - pros-cons-decision
    triggers:
      - compare these options
      - A vs B recommendation
      - which should I choose
      - pros and cons
---

# Compare Options

## When To Use

- The user asks for a choice, recommendation, ranking, or fit assessment
  between concrete alternatives.
- The request includes constraints such as cost, risk, speed, team fit, or
  project context.
- The user provides three or more options, or asks to choose under explicit
  constraints such as budget, audience, timeline, workload, compliance, or
  family/team fit.

## When Not To Use

- Use `answer-comparison` when the user asks for a conceptual distinction,
  meaning, styles/patterns comparison, or "how are these different" without
  asking which option to choose.
- Use `answer-comparison` when the user asks a general two-item technology/tool
  question like "PostgreSQL랑 MySQL 중 뭐가 나아?" or "A와 B 중 무엇을 써야 해?"
  without concrete constraints.
- Use `pros-cons-decision` when the user explicitly asks for a pros/cons
  decision note.

## Method

1. Extract the alternatives and the user's constraints.
2. Pick comparison criteria that matter for this situation.
3. Compare tradeoffs without pretending uncertain facts are certain.
4. Recommend one option when enough information exists; otherwise ask for the missing criterion.
