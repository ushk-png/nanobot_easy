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
   - Before any tool call, write a one-line wave checkpoint in your working notes:
     `Wave N includes: ... / Deferred because depends on prior output: ...`.
     Only include subtasks whose dependencies are already satisfied. If a subtask
     needs the output of the current wave, it MUST be deferred to a later wave.
   - Search missing specialized skills with one batched `skill_search` call. Rewrite
     each subtask query to its underlying intent instead of copying surface wording.
     Set `wave_no` on every query. Do not include future dependent-wave subtasks in
     this batch.
   - Select one method per subtask by reading the returned capability cards
     (description, when_to_use, when_not_to_use, risk, exec needs, relations).
     Treat scores and match grades as retrieval hints, not final authority. If no
     candidate card fits, mark ordinary reasoning. Record each selected or rejected
     method with `skill_decision` and the same `wave_no`.
   - Execution location: keep the ledger and trace rules strict, but delegate
     selectively. Low-risk no-exec answer work may run in the main agent when it is
     small enough to keep context manageable. Use `spawn` or `delegate` when a
     subtask requires exec, needs isolation, consumes large context, benefits from
     parallelism, or needs a specialized profile.
   - Use `spawn` for independent work in the same wave when parallelism materially
     helps or the items are large. If you keep small independent subtasks in the
     main agent, still create one ledger row per item and record one `skill_decision`
     per item with the same `wave_no`.
   - Use `delegate` for serial dependent work when the returned result is needed
     before continuing and the task is not appropriate for direct main execution.
   - Make every delegated or spawned task self-contained and include `expected_output`.
   - Pass prior wave outputs in `context` for dependent `delegate` calls. A dependent
     task without prior wave output in context is a procedure error; stop and build
     the context package before delegating or continuing.
   - If an execution subtask fails, record it as Failed in `tasks.md`. Do not mark
     failed execution as completed merely because the error was analyzed. Skip only
     dependent downstream tasks, unless the user's requested downstream task is
     explicitly to review the failure itself.
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
