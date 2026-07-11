---
name: topic-recall
description: >
  Resume or reconstruct an earlier conversation topic. Use when the user says
  "continue what we were doing", "go back to the earlier coding topic", "what was
  that file/function again", or asks to restore context from a previous topic.
metadata:
  nanobot:
    id: builtin-topic-recall
    version: 1.0.0
    status: verified
    category: memory.recall
    risk_level: low
    requires_exec: false
    triggers:
      - continue what we were doing
      - go back to the previous topic
      - resume the earlier coding task
      - what was that file again
      - restore context from earlier
---

# Topic Recall

## When To Use

- The user asks to resume a previous topic after intervening conversation.
- The user references an earlier file, function, decision, or task state.
- The current prompt depends on context that may have fallen out of the live window.

## When Not To Use

- The user asks a new standalone question.
- The referenced topic is already fully present in the current conversation.

## Method

1. Identify candidate topic phrases from the user request. Use explicit identifiers
   first: file paths, filenames, function names, config keys, dates, IDs, or named tasks.
   If two or more plausible topics match, or no concrete identifier/recent single topic
   makes the target clear, ask the user which topic. If exactly one topic is clearly
   implied, continue and state the topic you are restoring at the start of the answer.
2. Check `memory/topics/` for a matching topic snapshot.
   - Start with `grep` in `memory/topics` using likely keywords.
   - Read the best matching topic file if one exists.
3. If no topic snapshot exists or it is insufficient, search `memory/history.jsonl`.
   The history log is the preferred lightweight fallback because consolidation preserves
   topic-separated summaries and concrete identifiers. If it contains enough details
   to restore decisions, open items, next steps, and related paths, do not read raw
   session logs.
4. If history is missing, ambiguous, or lacks required identifiers, search session logs
   under `sessions/` for the topic phrase, file path, function, or decision.
5. If the relevant session log is too large to inspect directly, delegate reconstruction
   to a low-risk profile. The delegated task must include the topic phrase, session file
   path, and expected output.
6. Present a short restored-state summary: decisions, open items, next steps, related paths.
7. Continue the user's requested work from that restored state.

## Failure Rules

- If no matching topic can be found, say what you searched and ask the user for a more
  specific topic, file, or date.
- If multiple plausible topics match, do not choose one silently. Present the short
  candidate list and ask which topic to restore.
- Do not invent missing decisions.
