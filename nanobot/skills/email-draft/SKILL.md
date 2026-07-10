---
name: email-draft
description: >
  Draft or rewrite emails and short professional messages with clear purpose,
  audience, tone, and next action. Triggers: "draft an email", "write a reply",
  "polish this message", "make this more professional".
metadata:
  nanobot:
    id: builtin-email-draft
    version: 1.0.0
    status: verified
    category: writing.email
    risk_level: low
    requires_exec: false
    triggers:
      - draft an email
      - write a reply
      - polish this message
      - make this more professional
      - email response
---

# Email Draft

## When To Use

- The user wants a new email, reply, announcement, or short professional
  message.
- The user asks to adjust tone while preserving intent.

## When Not To Use

- Use `translation-technical` when the main task is translation.
- Use `meeting-notes` when the input is meeting notes and the user wants actions.

## Method

1. Identify recipient, purpose, tone, and desired action.
2. Preserve facts and commitments; do not invent dates, promises, or authority.
3. Make the subject line explicit when useful.
4. Keep the draft concise unless the user asks for a formal long version.
5. Offer alternatives only when tone is uncertain.

## Failure Rules

- If recipient or desired outcome is missing and materially affects tone, ask one
  question before drafting.
