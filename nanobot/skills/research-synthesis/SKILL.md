---
name: research-synthesis
description: >
  Synthesize multiple provided or sourced materials into themes, conclusions,
  open questions, and decision-ready takeaways. Triggers: "synthesize these
  sources", "combine these findings", "what do these reports say together",
  "make a research synthesis".
metadata:
  nanobot:
    id: builtin-research-synthesis
    version: 1.0.0
    status: verified
    category: research.synthesis
    risk_level: low
    requires_exec: false
    required_tools:
      - web
    conflicts_with:
      - research-brief
      - summarize-document
    triggers:
      - synthesize these sources
      - combine these findings
      - what do these reports say together
      - make a research synthesis
      - summarize the evidence across sources
---

# Research Synthesis

## When To Use

- The user has multiple sources, notes, search results, or reports that need to
  be combined.
- The desired output is cross-source conclusions rather than a single-source
  summary.

## When Not To Use

- Use `research-brief` when the user asks to find current external information.
- Use `summarize-document` when there is one document and no cross-source
  synthesis.

## Method

1. Identify the source set and the question being synthesized.
2. Group findings into themes.
3. Mark consensus, disagreement, and evidence gaps.
4. Distinguish source claims from your inference.
5. End with concise takeaways and open questions.

## Failure Rules

- If sources are missing or too thin, state the limitation and avoid overstating
  confidence.
