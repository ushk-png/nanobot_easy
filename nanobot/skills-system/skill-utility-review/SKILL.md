---
name: skill-utility-review
description: Decide whether a proposed skill is useful enough to keep: repeated workflow, quality improvement, routing value, and maintenance cost.
metadata:
  nanobot:
    id: system-skill-utility-review
    version: 1.0.0
    status: system
    category: system.composer
    risk_level: low
    requires_exec: false
---

# Skill Utility Review

Approve utility only when at least one is true:

- The workflow is repeated or likely to recur.
- The skill materially changes answer quality or safety.
- The skill preserves domain procedure that the base model would not reliably infer.
- The skill provides a tool workflow with fragile steps.

Reject or merge when:

- It is a one-off task.
- The skill is just domain facts without procedure.
- It overlaps heavily with an existing skill and has no clear routing boundary.

Return: keep, merge, or reject, with one paragraph of reasoning.
