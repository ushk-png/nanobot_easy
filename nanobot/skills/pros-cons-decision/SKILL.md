---
name: pros-cons-decision
description: >
  Build a concise pros/cons decision note for a practical choice, including
  recommendation, assumptions, and risks. Triggers: "pros and cons", "decision
  note", "help me decide", "recommend between these options", "장단점",
  "찬반", "할지 말지 고민".
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
      - 장단점
      - 장단점을
      - 장단점 정리
      - 장단점을 정리
      - 장단점 목록
      - 장단점 목록으로
      - 찬반
      - 할지 말지 고민
      - 도입의 장단점
      - 도입의 장단점을
      - 재택근무 도입의 장단점을 정리해줘
      - 이직할지 말지 고민인데 장단점 목록으로 정리해줘
---

# Pros/Cons Decision

## When To Use

- The user wants a decision note with pros, cons, recommendation, and risks.
- The alternatives are concrete enough to evaluate.
- The user asks in Korean for "장단점", "찬반", or whether to do something
  ("할지 말지") and wants a practical decision-oriented list.

## When Not To Use

- Use `answer-comparison` for conceptual difference explanations.
- Use `answer-comparison` for general two-item technology/tool comparisons such
  as "A랑 B 중 뭐가 나아?" when the user has not asked for a pros/cons decision
  note.
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
