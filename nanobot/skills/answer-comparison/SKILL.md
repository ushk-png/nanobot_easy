---
name: answer-comparison
description: >
  Explain differences between concepts, tools, approaches, or terms in a concise
  comparison. Triggers: "what is the difference between", "compare these terms",
  "A vs B meaning", "how are these different", "which concept applies here",
  "compare X and Y conceptually", "compare API styles", "A or B which is better",
  "A랑 B 중에 뭐가 나아", "A와 B 중 무엇을 써야 해".
metadata:
  nanobot:
    id: builtin-answer-comparison
    version: 1.0.0
    status: verified
    category: answer.compare
    risk_level: low
    requires_exec: false
    conflicts_with:
      - answer-howto
      - answer-diagnosis
      - compare-options
      - pros-cons-decision
    triggers:
      - compare two apis
      - what is the difference between
      - compare these terms
      - A vs B meaning
      - how are these different
      - which concept applies here
      - compare conceptually
      - compare API styles
      - 중에 뭘 써야
      - 중에 뭐가 나아
      - 무엇을 써야
      - 차이 비교
      - A와 B 비교
      - A랑 B 중
---

# Answer Comparison

## When To Use

- The user asks how two or more ideas, technologies, patterns, policies, or
  terms differ.
- The user needs a conceptual distinction before deciding what to do.
- The user asks which of two technologies, tools, APIs, databases, or concepts is
  better in a general way, without concrete project constraints beyond the two
  named items.
- The output should explain tradeoffs, not perform an operational task.
- The request says "conceptually", "meaning", "how are these different", or
  asks to compare styles/patterns.

## When Not To Use

- Use `compare-options` when the user wants a decision recommendation among
  concrete alternatives with explicit constraints such as budget, timeline,
  team fit, risk tolerance, or ranked requirements.
- Use `pros-cons-decision` when the user asks for a practical decision note,
  pros/cons list, recommendation, assumptions, and risks rather than a general
  conceptual comparison.
- Use `answer-howto` when the user asks for steps to accomplish something.
- Use `answer-diagnosis` when the user presents symptoms and asks what is wrong.

## Method

1. Name the comparison axis before listing differences.
2. Use a compact table when there are three or more dimensions.
3. State where the boundary is blurry or context-dependent.
4. End with a short "use X when / use Y when" summary if the user is choosing.

## Failure Rules

- If an item is ambiguous, state the likely interpretation and ask for the
  domain only if the distinction would change materially.
