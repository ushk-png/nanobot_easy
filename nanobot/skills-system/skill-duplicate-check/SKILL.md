---
name: skill-duplicate-check
description: Check whether a proposed skill duplicates or should update an existing skill.
metadata:
  nanobot:
    id: system-skill-duplicate-check
    version: 1.0.0
    status: system
    category: system.composer
    risk_level: low
    requires_exec: false
---

# Skill Duplicate Check

## Method

1. Search for existing skills with the proposed trigger phrases and category.
2. Inspect close matches.
3. Classify the proposal:
   - new: clear new routing boundary
   - update: best handled by modifying an existing skill
   - duplicate: no meaningful distinction
4. If related skills remain, require frontmatter relations such as `conflicts_with`,
   `supersedes`, or `fallback_to`.

Return the checked skills and classification.
