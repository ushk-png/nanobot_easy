---
name: skill-design-review
description: Review whether a skill draft has a clear purpose, concrete procedure, output format, examples, failure rules, and concise scope.
metadata:
  nanobot:
    id: system-skill-design-review
    version: 1.0.0
    status: system
    category: system.composer
    risk_level: low
    requires_exec: false
---

# Skill Design Review

Check the draft for:

1. Description has 3-7 realistic trigger phrases.
2. When to use and when not to use are clear.
3. Method is procedural, not a list of adjectives.
4. Output format is explicit when the workflow has a predictable deliverable.
5. Failure rules say what to do when inputs, tools, or evidence are missing.
6. Scope is narrow enough that neighbor skills can be distinguished.

Return must-fix issues first. If none, say the design is acceptable.
