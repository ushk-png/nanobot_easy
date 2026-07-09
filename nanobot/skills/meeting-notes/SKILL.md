---
name: meeting-notes
description: Turn meeting transcripts or rough notes into decisions, action items, owners, dates, and follow-ups.
metadata:
  nanobot:
    id: builtin-meeting-notes
    version: 1.0.0
    status: verified
    category: document.notes
    risk_level: low
    requires_exec: false
    triggers:
      - summarize these meeting notes
      - extract action items
      - meeting transcript
      - decisions and owners
---

# Meeting Notes

## Method

1. Extract decisions, action items, owners, due dates, and unresolved questions.
2. Preserve exact names, project identifiers, and dates.
3. If owners or dates are missing, mark them as unspecified.
4. Keep the output scannable and avoid adding commentary not present in the notes.
