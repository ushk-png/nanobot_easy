# Testy conversation-first review

Scope: candidate 570a4aa8, AgentLoop/AgentRunner, ContextSelector, allocation
reducer, memory guidance, tool registry, permission/workspace boundaries, and
Testy's active workspace/persona instructions. This is a review of the response
and execution flow, not a claim to audit every subsystem or prove model accuracy.

## Decision

Use the existing ordinary AgentLoop for Testy by setting only
`agents.defaults.contextSelection.enabled` to `false`. Do not replace it with a
new router, mandatory model classifier, or a legacy fallback after a failed
design. Ordinary requests begin in the ordinary path. Preserve model settings,
timeouts, iteration limits, configured tool permissions, session history, and
experimental task records. Do not deploy to other agents or the main checkout.

The ordinary model sees recent conversation and available tools. It can answer,
ask a question, or use a relevant tool. Missing historical facts are retrieved
with scoped memory tools. Meaning and intent are model responsibilities.

## Findings and changes

| Area | Finding | Action |
| --- | --- | --- |
| Mandatory design | Every user turn enters design when enabled; failure blocks an otherwise simple answer | Use the existing ordinary path for Testy |
| Numeric contract | A text-triggered reducer mutates requirements; negated changes and multiword names are misread | No automatic entry for conversational answers; retain experimental implementation and historical evidence |
| Persona procedure | Required routing logging, fixed modes, 5-3-1 reasoning, and one-target gate add work to ordinary replies | Apply the Testy-only adaptive response policy; keep persona and specialized study contracts |
| Memory | Keywords can cause retrieval even with a visible referent | Current conversation first; retrieve missing relevant facts; raw reads for unavailable exact quotations |
| Skill routing | Mandatory telemetry and contradictory structured-output search rules add tool turns | Optional telemetry; specialize only when the method helps |
| Topic changes | A new topic requires a file write before answering | Handoff only when requested or part of authorized durable work |
| Tool actions | Tool schemas, intent metadata, actual result/error handling, filesystem/network controls remain useful | Keep code and configuration unchanged |
| Recovery and completion | Bounded empty-answer/promise recovery, cancellation and iteration limits are distinct from numeric task proof | Retain existing ordinary runner behavior; no extra automatic retries or timeout increases |

## Application and boundaries

The profile text at `docs/runtime-profiles/testy-conversation-policy.md` replaces
Testy's workspace response-mode/intent-gate section. Its persona declaration no
longer requires a `skill_decision` call. Shared intent-gate and persona skill
files are not edited; the Testy policy explicitly supersedes their older
procedure requirements without relaxing approvals or security limits.

`timeAllocationContractEnabled` can remain true but is dormant while context
selection is disabled. Runtime metadata must report both settings and the actual
ordinary path. No conversational answer may be advertised as contract-verified.
The strict task ledger is preserved, not migrated to a second ledger or marked
DONE. Old strict tasks do not automatically resume or grant tool permissions.
If a strict contract run is needed later, it requires an explicit mode decision;
this change does not implement per-message automatic strict-mode selection.

The ordinary path retains existing tool safeguards, not every additional
ContextSelector task/evidence gate. In particular, model intent and general
claims are not semantically proved by code. Permission instructions remain in
the prompt; existing runtime constraints stay as configured. Testy's current
`restrictToWorkspace=false` is preserved, not represented as a sandbox.

## Verification

Synthetic model responses exercise the real AgentLoop, recent conversation,
scoped memory search, temporary-file write/readback, missing intent metadata,
and a runtime explicitly configured with workspace restriction. They verify
control flow and persistence, not natural-language understanding. Scoped
existing tests cover context rendering, memory, runner safety/recovery,
tool validation, and the retained explicit allocation contract.

Actual-model checks must report observed answers and tool usage separately;
they do not inherit the strict contract's DONE or numeric proof claims. Preserve
failures instead of revising expected values. Keep activation backups and hashes
for the Testy config, workspace instructions, launcher, and start script so the
previous runtime/profile can be restored together.
