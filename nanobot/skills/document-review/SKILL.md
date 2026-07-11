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
      - review translated spec for clarity
---

# Document Review

## When Not To Use

- Use `code-review` for code files, diffs, pull requests, patches, or
  implementation-review comments.
- Use this skill rather than `translation-technical` when the user asks to
  review, critique, or check clarity of an already translated document.

## Method

1. Identify the document goal, audience, and decision it supports.
2. Review for factual gaps, unclear claims, contradictions, missing assumptions, and risks.
3. Separate must-fix issues from suggestions.
4. Return concise findings first, then optional rewrite suggestions.
