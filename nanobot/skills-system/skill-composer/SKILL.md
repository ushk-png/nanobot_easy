---
name: skill-composer
description: >
  Create or update nanobot skills only when the user explicitly asks to make,
  draft, improve, or modify a skill. Orchestrates duplicate checks, design,
  security, utility, trigger differentiation, draft generation, and routing tests.
always: true
metadata:
  nanobot:
    id: system-skill-composer
    version: 1.0.0
    status: system
    category: system.composer
    risk_level: low
    requires_exec: false
    triggers:
      - make this a skill
      - create a skill
      - turn this into a skill
      - update this skill
      - draft a nanobot skill
---

# Skill Composer

## When To Use

- The user explicitly asks to create, draft, package, modify, or improve a skill.
- The user asks to turn a repeated workflow into a reusable skill.

## When Not To Use

- The user only asks you to perform a task once.
- The user did not ask for skill creation or skill modification.

## Procedure

1. Search existing skills first with `skill_search`.
2. Read supporting system skills as needed:
   - `skill-duplicate-check`
   - `skill-trigger-differentiation`
   - `skill-design-review`
   - `skill-security-review`
   - `skill-utility-review`
   - `skill-draft-generator`
   - `skill-test-generator`
3. Decide whether a new skill is justified. If an existing skill should be updated instead,
   explain why and edit only after the user confirms that intent.
4. For a new skill, create a draft under `skills/{skill-name}/SKILL.md`.
5. Draft frontmatter must include:
   - `name`
   - `description`
   - `metadata.nanobot.id`
   - `metadata.nanobot.version`
   - `metadata.nanobot.status: draft`
   - `metadata.nanobot.category`
   - `metadata.nanobot.risk_level`
   - `metadata.nanobot.requires_exec`
   - relation fields when relevant
6. Generate a routing test file with ten cases: five that should select the new skill and
   five neighbor cases that should not.
7. Run `nanobot skill reindex` and `nanobot skill test-routing` when practical.
8. Tell the user the draft path, then call `skill_request_approval` with the draft name and ask
   them to confirm registration in your own words in the same reply. Only their next plain yes/no
   message approves or cancels it. They can also run `nanobot skill approve <name>` in a terminal
   or use the WebUI directly.

## Lifecycle Rules

- Draft skills are not visible to `skill_search`.
- Candidate/verified promotion is a human action — via `skill_request_approval` plus the user's
  yes/no reply, the CLI, or the WebUI — never something the agent performs on its own initiative.
- Minor changes to triggers, description, or failure guidance require trigger differentiation
  review and a version bump.
- Major changes to Method, required tools, or risk require re-review and reapproval.
- Do not modify system skills from runtime.

## Output

Report:
- Draft path.
- Why the skill is needed.
- Neighbor skills checked.
- Security and utility concerns.
- Routing test path and result if run.
- Exact approval command.
