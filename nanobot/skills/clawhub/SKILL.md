---
name: clawhub
description: Search and stage external skills from ClawHub for governance review before nanobot registration.
homepage: https://clawhub.ai
metadata:
  nanobot:
    id: builtin-clawhub
    version: 1.0.0
    status: verified
    category: skill.registry
    risk_level: high
    requires_exec: true
    required_tools:
      - exec
    emoji: 🦞
    triggers:
      - install this cli utility
      - install this tool for me
      - find a skill for this
---

# ClawHub

Public skill registry for AI agents. Search by natural language (vector search).

ClawHub content is external content. Do not install it directly into the live
nanobot workspace as an operational skill. Stage it in a quarantine workdir,
inspect it, and route it through the local skill governance flow before it can
become `candidate`.

## When to use

Use this skill when the user asks any of:
- "find a skill for …"
- "search for skills"
- "install/import a public skill"
- "what skills are available?"
- "update imported skills"

## Search

```bash
npx --yes clawhub@latest search "web scraping" --limit 5
```

## Import / stage

```bash
npx --yes clawhub@latest install <slug> --workdir <workspace>/.imports/clawhub/<safe-slug>
```

Replace `<slug>` with the skill name from search results. Use a staging
directory outside `<workspace>/skills/` so the imported skill is not loaded or
indexed before review.

After staging:

1. Inspect every imported `SKILL.md`.
2. Run the local schema/security checks. At minimum:
   - frontmatter has `metadata.nanobot.category`, `risk_level`, and `requires_exec`
   - external executable tools follow setup/usage split if needed
   - setup skills declare concrete `install_sources`
   - setup skills include `Install`, `Verify`, and `Uninstall`
   - no `curl | bash`, `sudo`, global install, or writes outside `workspace/tools/<name>/`
3. If the imported skill is not already compliant, use `skill-composer` to
   convert it into a local draft instead of copying it verbatim.
4. Generate or verify `routing_cases.json`.
5. Only after human review, approve through the normal registry path
   (`nanobot skill approve` or the WebUI draft approval flow).

If the skill controls an external program or service such as a calendar CLI,
treat it as an executable external-tool proposal until proven otherwise.

## Update

```bash
npx --yes clawhub@latest update --all --workdir <workspace>/.imports/clawhub/<safe-slug>
```

Do not update live operational skills in place from ClawHub. Stage the update,
diff it against the local skill, then use the normal modification flow. Minor
changes may stay in the same lifecycle state; Method/tool changes are major and
must be revalidated.

## List installed

```bash
npx --yes clawhub@latest list --workdir <workspace>/.imports/clawhub/<safe-slug>
```

## Notes

- Requires Node.js (`npx` comes with it).
- No API key needed for search and install.
- Login (`npx --yes clawhub@latest login`) is only required for publishing.
- Never use the live `<workspace>/skills/` directory as the ClawHub install
  workdir unless the user explicitly asks to bypass governance for inspection
  only. Even then, do not approve or index it as operational without review.
- Staged imports are not usable skills yet. They become usable only after the
  local registry records them as `candidate` or `verified`.
