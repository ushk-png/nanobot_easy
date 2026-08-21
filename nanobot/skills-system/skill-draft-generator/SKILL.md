---
name: skill-draft-generator
description: Generate SKILL.md drafts that follow the nanobot metadata schema and writing guide.
metadata:
  nanobot:
    id: system-skill-draft-generator
    version: 1.0.0
    status: system
    category: system.composer
    risk_level: low
    requires_exec: false
---

# Skill Draft Generator

Generate a `SKILL.md` with:

1. YAML frontmatter using `metadata.nanobot`.
2. `metadata.nanobot.status: draft`.
3. A description with realistic trigger phrases.
4. When To Use and When Not To Use.
5. Method with ordered steps.
6. Output format when useful.
7. Failure rules.
8. Short prohibitions only when they prevent common mistakes.

For executable external tools, generate two skills instead of one:

- `<tool>-setup`: one-time install/config/healthcheck. It must declare
  `risk_level: high`, `requires_exec: true`, concrete
  `metadata.nanobot.install_sources`, and sections `Install`, `Verify`, and
  `Uninstall`.
- `<tool>-usage`: command patterns and error handling for an already-installed
  tool. The first Method step must check installation with `which`,
  `--version`, or a healthcheck; if missing, instruct the user that
  `<tool>-setup` is required. Add `fallback_to: [<tool>-setup]`.

Use ASCII unless the skill is explicitly for non-English user phrasing.
Do not mark a draft as candidate, verified, or system.
