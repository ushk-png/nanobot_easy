---
name: summarize-document
description: >
  Summarize one document, article, transcript, or pasted text into concise
  sections, key points, and notable details. Triggers: "summarize this document",
  "short summary of this article", "extract key points", "make this shorter".
metadata:
  nanobot:
    id: builtin-summarize-document
    version: 1.0.0
    status: verified
    category: document.summary
    risk_level: low
    requires_exec: false
    conflicts_with:
      - summarize
      - meeting-notes
      - document-review
      - research-synthesis
    triggers:
      - summarize this report
      - summarize this memo
      - summarize this article
      - summarize this pdf
      - summarize this transcript
      - summarize this document
      - short summary of this article
      - extract key points
      - make this shorter
      - summarize the text below
---

# Summarize Document

## When To Use

- The user wants a concise summary of one supplied document or text.
- The user asks for key points, outline, or executive summary.

## When Not To Use

- Use `meeting-notes` for decisions, owners, and action items from a meeting.
- Use `document-review` for critique and risks.
- Use `research-synthesis` for combining multiple sources.
- Use `summarize` when the user asks for summarize.sh, URL or YouTube
  extraction, or CLI-based processing of an external file.

## Method

1. Identify the document type and intended audience if obvious.
2. Preserve core claims, decisions, entities, dates, numbers, and caveats.
3. Use headings that match the user's requested format.
4. Keep the summary shorter than the source unless the user asks for detail.
5. Note important omissions or unclear source sections.

## Failure Rules

- If the document is not provided, ask the user to paste it or provide a path.
