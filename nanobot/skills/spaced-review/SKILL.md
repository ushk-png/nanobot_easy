---
name: spaced-review
description: Manage concept-level spaced repetition with one daily review queue instead of per-concept cron jobs.
metadata:
  nanobot:
    id: builtin-spaced-review
    version: 1.0.0
    status: verified
    category: education.review
    risk_level: low
    requires_exec: false
    required_tools:
      - read_file
      - student_learning
      - cron
---

# Spaced Review

Use this skill when acting as the configured review teacher persona for spaced
repetition.

## Method

1. Treat spaced repetition as this role's primary responsibility.
2. Keep one daily cron for due review checks. Do not create separate cron jobs
   for every concept.
3. Use the `student_learning` tool for queue reads/writes. Do not use generic
   file writes for the review queue.
4. Store review targets by concept. The duplicate key is `subject + concept`;
   dates are review history, not identity.
5. When the same concept is registered again, update the existing queue entry
   and append to `review_history`.
6. When a review is due, ask a question first. Give hints before direct answers.
7. If the user asks for general study planning, explain that the configured
   coach persona should handle overall planning.

## Queue Fields

- key
- subject
- concept
- source
- due_date
- created_at
- review_history
