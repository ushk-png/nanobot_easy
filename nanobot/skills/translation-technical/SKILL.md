---
name: translation-technical
description: >
  Translate technical text while preserving terms, identifiers, code, commands,
  paths, and product names. Triggers: "translate this technical document",
  "translate keeping code terms", "Korean technical translation", "English
  version of this spec".
metadata:
  nanobot:
    id: builtin-translation-technical
    version: 1.0.0
    status: verified
    category: writing.translation
    risk_level: low
    requires_exec: false
    triggers:
      - translate this technical document
      - translate keeping code terms
      - Korean technical translation
      - English version of this spec
      - translate this API documentation
---

# Technical Translation

## When To Use

- The user asks to translate technical documentation, specs, API text, release
  notes, or engineering communication.
- Preserving identifiers and domain terms matters.

## When Not To Use

- Use `email-draft` when the user wants a new email composed, not translated.
- Use `summarize-document` when the user wants a shorter version rather than a
  translation.
- Use `document-review` when the user asks to review, critique, or check the
  clarity of an already translated spec or document.

## Method

1. Preserve code blocks, commands, file paths, API names, variable names, and
   product names exactly unless the user asks otherwise.
2. Translate prose naturally for the target audience.
3. Keep headings and list structure unless reformatting is requested.
4. Add a short terminology note only for ambiguous terms.

## Failure Rules

- If the target language is not specified, infer it from the request when clear;
  otherwise ask.
