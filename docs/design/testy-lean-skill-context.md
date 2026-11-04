# Testy: compact agent documents and on-demand skills

The agent's identity is in SOUL.md and its short operating policy in AGENTS.md.
The obsolete testy-agent skill is archived outside runtime skill discovery;
its speech defaults move to testy-voice, and its already duplicated no-audio
preference remains in USER.md. Japanese-only bundle boundaries move from AGENTS.md
into testy-japanese-recall. The existing new-first behavior and no-audio user
preference replace contradictory old review-first/combined-audio wording.
No learning data, schedule, tool implementation, user profile, or other agent changes.

Testy profile:

```json
{"skills": [], "preloadAlwaysSkills": false,
 "onDemandSkills": ["testy-japanese-recall", "testy-voice"]}
```

`ContextBuilder` uses compact name/description/path cards for these skills and
formerly automatic system skills. Their bodies are read only when relevant.
The existing registry search covers eligible skills; direct discovery cards also
cover draft/system entries that registry search does not return. No registry
status is promoted, and neither reading a skill nor its card grants permission.
Explicitly supplied skill selections still work. Existing defaults remain unchanged
for other agents (`preloadAlwaysSkills=true`, no extra on-demand skills).

Operational profile sources live under `docs/runtime-profiles/testy-lean/`.
Preserve all replaced documents, the original config/launcher/start script, and a
consistent SkillStore backup before applying. Reindex only Testy's derived index.
Pin and restart only Testy, and record both code and profile hashes. The ordinary
conversation path, model settings, configured tool set and authorization boundaries
stay unchanged.

Verification covers actual prompt composition, explicit/on-demand reads, real
registry search, draft discovery without promotion, config-to-loop propagation,
and the default behavior of other agents. Existing loader tests that assume no
system directory are compared against the original candidate by exact failing IDs.
Measure provider-reported input tokens before/after using the same user question,
and record call count and final-answer correctness separately. This does not claim
universal token reduction: requested specialist work may need further model turns.
