---
name: document-review
description: Review a document for clarity, correctness, risks, omissions, and actionable improvements.
metadata:
  nanobot:
    id: builtin-document-review
    version: 1.0.0
    status: verified
    category: document.review
    risk_level: low
    requires_exec: false
    triggers:
      - review this document
      - find gaps in this proposal
      - critique this draft
      - check this spec for risks
---

# Document Review

## Method

1. Identify the document goal, audience, and decision it supports.
2. Review for factual gaps, unclear claims, contradictions, missing assumptions, and risks.
3. Separate must-fix issues from suggestions.
4. Return concise findings first, then optional rewrite suggestions.
