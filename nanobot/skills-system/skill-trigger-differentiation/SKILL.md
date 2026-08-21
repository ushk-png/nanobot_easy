---
name: skill-trigger-differentiation
description: Improve skill descriptions and when_not_to_use rules so routing separates neighbor skills cleanly.
metadata:
  nanobot:
    id: system-skill-trigger-differentiation
    version: 1.0.0
    status: system
    category: system.composer
    risk_level: low
    requires_exec: false
---

# Skill Trigger Differentiation

## Method

1. Write 3-7 realistic user trigger phrases in the description.
2. Add common synonyms and short phrases users actually type.
3. Add `when_not_to_use` for neighbor skills and known false positives.
4. Name the neighbor skill when a boundary is known.
5. Keep the description concise enough for search.

Return an improved description and any relation fields needed.
