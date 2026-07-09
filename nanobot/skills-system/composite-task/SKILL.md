---
name: composite-task
description: >
  Orchestrate requests that combine two or more distinct deliverables, methods,
  or skill domains. Triggers include "summarize and review", "analyze then fix",
  "do each file separately", "give both A and B", and dependent multi-step work.
always: true
metadata:
  nanobot:
    id: system-composite-task
    version: 1.0.0
    status: system
    category: system.orchestration
    risk_level: low
    requires_exec: false
    triggers:
      - summarize and review
      - analyze then fix
      - compare and draft
      - each document separately
      - produce multiple deliverables
---

# Composite Task Orchestration

## When To Use

- The user asks for multiple distinct deliverables.
- The request joins different methods or skill domains.
- One requested output is an input to another requested output.
- Several independent items should each receive the same treatment.

## When Not To Use

- A single skill clearly covers the whole request.
- The task was delegated to you as a subtask.
- The user only needs a simple direct answer.

## Method

1. Decompose the request into at most five one-level subtasks.
2. Mark dependencies between subtasks. Independent subtasks can run in the same wave.
3. Create or update `tasks.md` with task, status, and wave number.
4. For each wave:
   - Search missing specialized skills with one batched `skill_search` call.
   - Select one method per subtask. If all matches are weak, mark ordinary reasoning.
   - Run low-risk no-exec work yourself.
   - Use `spawn` for independent long or parallel delegated work.
   - Use `delegate` for serial dependent work that must return before continuing.
   - Make every delegated task self-contained and include `expected_output`.
   - Pass prior wave outputs in `context` for dependent `delegate` calls.
5. After each wave, update the ledger and refine later subtasks if prior output changes them.
   You may add new subtasks only once in the whole run.
6. Integrate results in the structure requested by the user. Label each sub-result with
   its source skill or profile when useful.
7. Verify that every original request was answered. Explicitly list failures or skipped
   dependent subtasks.

## Failure Rules

- If a subtask fails, skip only dependent downstream subtasks; continue independent work.
- Retry a failed delegated subtask at most once with a more concrete task/context package.
- If decomposition produces one subtask, stop using this skill and follow the single-skill path.
- If the subtask boundary is ambiguous, ask the user which deliverables they want.

## Bad Example

Question: "Summarize this document and review the business case."

Bad execution: blend a summary method and a review method into one improvised answer.

Correct execution: summarize first, pass that summary as context to the review step, then integrate.
