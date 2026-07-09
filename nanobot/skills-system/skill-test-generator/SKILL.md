---
name: skill-test-generator
description: Generate routing test cases for a skill draft: positive triggers and neighbor negative cases.
metadata:
  nanobot:
    id: system-skill-test-generator
    version: 1.0.0
    status: system
    category: system.composer
    risk_level: low
    requires_exec: false
---

# Skill Test Generator

Create a routing test file with ten cases:

- Five positive cases expected to route to the draft skill.
- Five neighbor or false-positive cases expected to route elsewhere.

Use this YAML format:

```yaml
cases:
  - query: example user phrase
    expected: skill-name
```

After approval, run:

```bash
nanobot skill reindex
nanobot skill test-routing path/to/routing.yaml
```
