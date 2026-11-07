# Genie v4.2 implementation and verification

Date: 2026-10-22. Baseline checkout: `7f90cca1`.
Specification: user attachment `v4.2_최종보완지시문_반례시험_통과기준.md`.
This report distinguishes isolated verification from production activation.

## Scope and activation

The ordinary AgentLoop/AgentRunner remains the execution engine. The feature is
off by default and is enabled only by `agents.defaults.stepwise.lightweight`.
It is independent of the older `stepwise.enabled` procedure engine.

The local Genie config already has lightweight=true and stepwise.enabled=false.
Parsed local main, Heart, Testy, Rees, and Japanese coach configs have
lightweight=false. No local agent config was changed for this implementation.
No main or companion gateway is to be restarted for this change.

## Actual control and evidence contracts

- `task_note.py`: a turn-local `update_task` tool records the current request ID,
  intent, goal, selected targets and bounded ranges, source, analysis status,
  explanation, and references to successful write tool-call IDs. These use the
  existing provider tool-call channel, not response-text regular expressions or
  an assumed provider-specific structured-response API.
- Missing or old control data remains unconfirmed. Read-only discovery is allowed;
  mutation needs current control data. Current explicit no-write instructions
  remain effective even if the model supplies a contradictory interpretation.
- Follow-ups are checked before tool execution and between tool batches. Old tool
  calls receive superseded results without running. The current request ID changes;
  old control updates no longer apply. Discovery does not choose the first match.
  Semantic target selection remains the model's responsibility and may require a
  question. Code checks acquisition against every selected target.
- The built-in file reader attaches structured line/page ranges and a file version
  to the actual result, before display truncation. No inference is made from a
  missing range argument or a word such as "whole" in source content. Unknown tool
  contracts remain unknown. Reads offloaded/truncated in model context do not
  establish full acquisition; smaller reads of the original are required.
- Coverage can accumulate over compatible ranges from the same version. Valid
  cached local reads require a matching content hash; files above 1 MB are not
  hashed and are not automatically reused across requests. mtime/size alone do
  not prove content identity. Acquiring content and analyzing it are distinct;
  an actual tool operation invalidates the previous analysis-complete declaration.
- A final answer and final `update_task` can share a model response. No always-on
  classification/review model call is added. Simple requests bypass the task path.
  Complex Genie channel replies are sent after checking, instead of streaming
  unchecked completion text. Existing tool progress remains available.
- All completion defects are returned together. One correction process may use
  several reads/checks within the existing iteration budget. Unresolved work is
  reported incomplete, never promoted to success to reduce calls.
- Successful, denied, and unknown writes are distinct. A successful write cannot
  be replayed to fix reporting. An unknown `write_file` can be reconciled by a
  subsequent real read whose content hash matches the intended content. Unknown
  edits without a verifiable final-content contract remain unresolved rather than
  automatically retried.
- Child operations are cancelled and awaited with a bounded grace period. An
  operation that ignores cancellation stays isolated: late results go to its old
  run audit and never complete the new request. Genie filesystem searches use a
  separate killable process with the same filters/pagination and a parent-validated
  read root; no process-global enable flag is used.
- Execution status and delivery status are separate session fields. The channel's
  existing retries resend the response only. Final send failures are persisted.
  There is no new user-facing resend command or claim of exactly-once delivery
  when the transport's send outcome is unknown.

## Requirement-to-evidence map

The named tests live in `tests/agent/test_task_note.py` unless noted otherwise.
Model responses are scripted/failure-injected. Local filesystem operations and
search subprocesses are real; outbound transport is simulated, not Telegram.

| Requirement | Baseline/review observation | Verification after change |
| --- | --- | --- |
| I-01 / T01 | Current checkout had no control layer; archived version read old response markers | Old markers do not confirm intent; stale IDs rejected; pending follow-up prevents write; follow-up between read and write supersedes the prepared write |
| T-01 / T02-T03 | Archived implementation could accept one of two target reads | Every target required; wrong/missing target rejected; ambiguous request stays incomplete; selected target succeeds; no-source declaration cannot bypass a named file |
| C-01 / T04-T05 | Archived partial content containing "whole" passed; real whole read failed after detail shortening | Unknown contract fails closed; full text and requested line/page ranges succeed; wrong ranges fail; accumulated ranges succeed; model-context offload stays incomplete |
| L-01 / T06 | Archived shield left child work alive after parent cancellation | Cooperative child settles; noncooperative late return is old-run audit only; actual search process is killed on cancellation |
| D-01 / T07 | Final delivery failure only logged | Real save + injected send failure + resend preserves one write; execution=completed and delivery=failed/sent are independent |
| R-01 / T08-T09 | Archived unit checks did not cover complete recovery | Pre-write denial is blocked; real save then timeout reconciles by real read; all defects returned together; missing result reference fixed without another write |
| F-01 / T10-T11 | Archived code accepted reference fences instead of required file | Supplied body works; reference fence does not bypass file; valid cached read works; changed cached content rejected; computation can interrupt/resume analysis; exhausted budget cannot claim success |
| M-01 / T12 | Old assistant marker could be reused for changed request | Current no-write instruction wins; old update ID invalid; existing memory retrieval conditions/store isolation are retained, not rebuilt |

The connected channel-loop test is
`tests/agent/test_loop_runner_integration.py::test_genie_checked_response_reaches_outbound_with_delivery_identity`.
It exercises the normal loop with a real file reader and a scripted model, ending
in a checked outbound response with its own delivery identity.

## Verification and remaining operational work

Focused regression tests cover the runner, progress, file/search tools, channel
manager, injections, stop queues, and opt-in context-selection compatibility.
The final focused run passed 250 tests in 11.48 seconds, with zero failures or
skips. Its machine-readable result is
`.local/verification/genie-v42/pytest.xml` (local, gitignored). Ruff and
`git diff --check` also passed. The counterexample file contains 35 tests; the
normal channel-loop integration test is separate.
The prior review found eight pre-existing cancellation-test setup failures caused
by a MagicMock workspace reaching SQLite initialization. Those unrelated fixtures
were not rewritten as part of this change; a full-suite green result is not claimed.

Per-task session metrics record elapsed time, model calls, tool calls and the
runner's aggregate token usage, including recovery calls. Missing monetary cost is
null, not zero. Provider-internal retry counts/cost breakdowns are not exposed by
this layer. No live-model before/after performance result, Telegram acceptance
result, or general reasoning improvement is claimed from scripted tests.

Production activation requires a dedicated Genie restart after checking ongoing
work. The restart command is `.local/restart-genie-gateway.sh`; the new startup log
includes `Lightweight task evidence enabled for workspace ...workspace-genie`.
Disable `agents.defaults.stepwise.lightweight` in the Genie config and restart
Genie to return to the legacy path; do not enable the old stepwise engine as a
substitute. This does not undo external changes already performed.

At report creation, no gateway restart or live Telegram trial has been performed.
User acceptance remains manual; no quality evaluation dataset was introduced.

## Task progress follow-up

Removed the fixed notice formerly emitted for each tool response. Active task
updates can supply `planned_steps`: up to three short public future-tense steps,
with the first two shown as a task-specific starting plan. Actual tool attempts
then generate file/range, search, edit, execution-purpose or test-specific notices.
These are emitted after the current mutation gate, not for superseded work.

Notices are coalesced per request: at least 25 seconds between different phases,
60 seconds between notices within one phase, and no repeat of the same notice.
Internal bookkeeping and completed updates do not emit plans. A follow-up resets
the old plan. No model call or background timer is added for public progress;
unchecked final-response text remains private until completion checking.

`tests/agent/test_task_progress.py` verifies plans, timing with an injected clock,
read-range notices, internal-tool suppression, follow-up resets, command privacy,
and a real-file runner flow that emits one plan across repeated reads without
adding model calls. This feature still follows the Genie-only lightweight flag.

## Missing result report incident (2026-10-23)

Production evidence: Genie ended the 06:55:37 diagnosis at 06:56:55 with
`status=completed`, `analysis_complete=true`, and the public text
"필요한 구간은 모두 확인됐습니다. 원인 판정을 닫겠습니다."
After the user's continuation at 07:01:22 it ended at 07:01:39 with another
closing notice. In both cases `update_task.explanation` contained a diagnostic
claim, but that claim was not delivered as a user-facing report. There was no
timeout or iteration-budget stop in these turns; the stored stop state was
completed. The completion check verified acquisition and the model's analysis
flag, but had no contract for the actual answer. The common execution-promise
guard also did not recognize these diagnosis/closing phrases. It was not broadened
for main or companion agents.

Three regression cases failed before this correction: missing public report
incorrectly passed; the observed closing notice ended the runner; and an actual
report in structured control data was not used as the delivered answer.

Correction: completed Genie tasks require `final_answer`, separate from internal
status/explanation. The same existing tool-call response can contain that answer,
so successful turns do not require an always-on reviewer call or duplicate prose.
The checked answer passes the existing public-content hook, then replaces the
status sentence in the final response and persisted transcript. Empty answers
and promise/readiness-only answers do not satisfy the report requirement. This
is a deliverable-presence check, not a claim of general semantic correctness.

When only reporting is missing, a bounded reporting recovery uses the existing
evidence and cannot execute state-changing tools. It is available even after
an acquisition correction, within the existing iteration budget. Follow-ups
invalidate the old interpretation as before. The last-iteration finalization path
also uses the actual report; it cannot silently return a closing notice instead.

Tests cover both quoted public replies, cached-evidence continuation without
rereading, reporting after coverage recovery, mutation blocking during report
assembly, exhausted budgets, same-call report delivery, and the existing content
filter. Existing non-Genie behavior and simple-question bypass remain intact.

A separate live-model check used the configured `openai-codex/gpt-6.1-sol` and an
isolated supplied-body version of this incident (no writes, no Telegram message,
no production session mutation). It completed with an actual Korean report of
cause, evidence and uncertainty, using two model calls: 2,655 input tokens and
923 output tokens (3,578 total), 24,656 ms; monetary cost was unavailable.
This confirms tool-schema/report delivery with the actual provider, not the
correctness of the unrelated Testy repetition diagnosis or a full Telegram trial.

Final focused regression result for this correction: 270 passed in 12.05 seconds;
Ruff and `git diff --check` passed. JUnit output remains at
`.local/verification/genie-v42/pytest.xml`.
Only Genie was restarted at 2026-10-23 07:13:29 KST (PID 45866). Its health endpoint
returned status=ok and Telegram reconnected at 07:13:30. Main and companion gateway
PIDs were unchanged. No test message was sent to the user's Telegram conversation;
the next actual Telegram reply remains a user acceptance check.
