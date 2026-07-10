---
name: answer-comparison
description: >
  Explain differences between concepts, tools, approaches, or terms in a concise
  comparison. Triggers: "what is the difference between", "compare these terms",
  "A vs B meaning", "how are these different", "which concept applies here".
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
    triggers:
      - what is the difference between
      - compare these terms
      - A vs B meaning
      - how are these different
      - which concept applies here
---

# Answer Comparison

## When To Use

- The user asks how two or more ideas, technologies, patterns, policies, or
  terms differ.
- The user needs a conceptual distinction before deciding what to do.
- The output should explain tradeoffs, not perform an operational task.

## When Not To Use

- Use `compare-options` when the user wants a decision recommendation between
  concrete alternatives.
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
