# Context selection: obligations without word rules

Scope: only the selective-context path (`agents.defaults.context_selection.enabled`).
`premature_final`, `answer_delivery`, `task_note` and the legacy path are unchanged.
Prohibitive rules (`instruction_policy` denied_actions, deploy/restart/delete/send prohibitions)
stay as restrictions: they can only forbid, never create an obligation.

## Stage 1 (patch 8): validator-error class, direct decoupled from request_type

Problem (golf e2e): a bare word in the request ("인터넷 검색은 필요 없습니다") made a correct
direct answer "incomplete", and `request_type=modify` alone made a correct direct design
`unsafe_direct`, which was sent to a repair call that timed out.

* `ERROR_CLASSES` / `error_class()`: every validation code is `format`, `omission`,
  `plan_conflict`, `data` or `validator`. Only model mistakes are repaired. `data` is restored
  in code (existing). `validator` codes are never shown to the model.
* `direct_blockers()` (structural only): unresolved references / prior context missing, media,
  required need not loaded, tool capability, `unresolved`, `task_relation=resume`, no answer.
  `request_type` is no longer a criterion. A resume word alone is no longer a criterion.
* An imperative action phrase (`_ACTION_REQUEST`, unchanged) with an otherwise valid direct design
  is `direct_action_word` (validator class): no repair call; the design is rerouted to `execute`
  with `answer=None` (fail safe: work is executed with tools, nothing is claimed complete).
* `answer_only()` no longer uses `request_type` or the bare-word `_ACTION`; it keeps the
  imperative `_ACTION_REQUEST` as an interim floor until obligations are typed (stage 2).
* The repair message lists only model-class codes.

## Stage 2 (patch 9): typed obligations, code floors, lookup gate

* `IntentDesign.obligations` (optional, default `None` for stored proposals and old models):
  `target` answer|prior_answer|external_resource, `lookup` required|forbidden|unneeded|conditional,
  `external_change` none|required, `source_span`, `keep_constraints`, `change_constraints`.
  Schema +~254 estimated tokens, rules +~58 (design input ~6k).
* `trusted_obligations()`: non-default lookup/external_change must cite a `source_span` supported
  by the request (`constraint_text_supported`). Untrusted obligations do not relax anything: the
  stage-1 rules apply. No repair call for that (`obligations_untrusted` counter).
* Direct blockers added: external_change required, lookup required, external_resource target,
  and the code floor `named_resource_unloaded` (a file named in the request is not in any loaded
  material/prior source; applies with or without obligations).
* The imperative-phrase heuristic is waived only for an anchored `prior_answer` edit whose earlier
  answer is loaded ("답변에서 수정"); everything else is still rerouted to execute.
* `answer_only()` with trusted obligations: no external change, lookup not required, target not
  external, no file/tool need, no state-changing observation, a named file was read.
* Restriction: `lookup=forbidden` blocks `web_search`/`web_fetch` in the selective tool registry
  (trusted or not; forbidding never needs proof). `lookup_conflict` if the design asks for them.
* Unsupported keep/change constraints are dropped in code (no repair call).

## Stage 3 (patch 10): result-kind completion

* `CompletionSpec.result_kind` adds `answer_content`, `computed`, `sourced`.
* `completion_kind()`: a typed spec decides; then trusted obligations (external_change required ->
  `mutation_result`, lookup required -> `sourced`, else `answer_content`); the legacy test-word
  rule remains only for untyped designs without a non-semantic spec (stored proposals).
* Answer kinds need no tool report. `computed` is checked in code against the delivered answer
  (`computed_mismatch`): declared `values` sum to `total`, each label's value appears on a line
  naming it (`12분 30초` = 12.5), the total appears, `excluded` labels get no non-zero number.
  A failed check makes the verdict incomplete and names the check (no repair call).
* `sourced` needs a successful read/lookup observation.
* The `request_type in {modify, execute}` mutation rule is replaced by
  `obligations.external_change == "required"` for typed designs; untyped designs keep it.

## Stage 4 (patch 11): correction record

`ContextSelector.reconcile_verdicts()` runs at the start of each selective turn (errors are
counted, never block the turn). For an earlier reply stored as the verdict wrapper
(`요청을 완료하지 못했습니다 / 종료 상태: completed / 남은 조건: <only that design's checks> /
부분 응답(미검증): ...`) it re-checks the stored design with the current rules (answer-only, no
tool executions in the task, computed checks against the stored answer). If it now passes:

* a `SYSTEM_EVENT` with metadata `kind=assistant_correction` is appended, `parent_event_id` = the
  original assistant event, metadata: original/corrected status, reason, checks, bare answer;
  event id `correction:<original id>` (idempotent);
* a new task-state version removes those checks from `unfinished`, appends a
  `verdict_correction` entry to `completed`, and sets DONE when nothing is left;
* the designer payload shows the original event with the bare answer and
  `verdict=corrected_complete` + `correction_event_id`.

The original event, earlier task versions, and the session file are never edited or deleted.
Timeouts/blockers, other stop reasons, tool executions or failing computed checks: no correction.
Dry run on a copy of the box e2e DB: golf1/2/3/golfdiag turn 1 each got one correction
(IN_PROGRESS, 3-4 unfinished -> DONE); timeout turns were left alone.

## Stage 5 (patch 12): contrast suite

`tests/agent/test_context_contrast.py` pairs requests whose words overlap but whose obligations are
opposite (검색해 줘 / 하지 마, 필요 없어 / 필요하면 검색해, 답변에서 수정 / 파일 수정, 테스트 방법 /
실행, quoted '검색 실패', 드라이버 제외 유지 / 추가, 계속 설명해줘, paraphrases, combinations,
instruction change, deploy). Variants: `model` (typed design), `untyped` (no obligations),
`untyped_direct` / `mistyped_direct` (wrong direct answers for work requests), `wrong_numbers`
(computed answer that does not add up). The executor is simulated with no tool calls.
`python -m tests.agent.test_context_contrast` prints false-block / false-complete rates.

Offline result (same table, same simulated designs; a repair returns the same design):

| tree | false block (26 complete) | false complete (33 work) | repair calls |
|---|---|---|---|
| patch 7 (b4b628e) | 26 | 0 | 39 |
| patch 12 | 2 (untyped imperative, conservative) | 0 | 14 |

Floors added while building the suite (structural, not per question):
* an imperative phrase must be accounted for by the typed obligations (prior-answer target or a
  `source_span` over it); otherwise default obligations cannot make the request answer-only;
* answer-kind checks never complete a turn whose floor is unmet (named file / file-tool need /
  external target / required lookup / unaccounted imperative with no successful observation);
* a direct, `new` answer with a loaded earlier answer is an answer continuation: the bare
  "계속" reference to unfinished work does not create `no_resumable_task` / `context_unresolved`.

## Stage 6 (patch 14): relation-level obligations and the mutation gate

- `nanobot/agent/context_relations.py`: each request is parsed into relations
  `(action, target, necessity, condition, source_span)`. Quoted text is blanked, clauses are split at
  punctuation and connectives (`말고/않고/대신/-고/-며`), a stem counts only as a predicate (request or
  connective ending; English only in imperative position). Verb *classes* (lookup, inspect, edit,
  write, run, send, delete, deploy) replace single phrasings, so 구글링해서 / 웹에서 찾아보고 /
  네이버에 쳐보고 / look it up are one lookup relation. Negation makes it `forbidden`, "필요 없" makes it
  `optional`, 필요하면 / 필요 시 / 모르면 / if needed make it `conditional`.
- The model may also send `obligations.relations`. A model relation counts only when its span is in the
  request and its polarity matches the clause it cites (`relation_polarity_conflict` is a model error
  and is repaired). When the two sides disagree, `forbidden` wins.
- Binding in code: `prior_answer` binds to the newest full assistant source (the real response event
  id). Deictic external targets (아까 만든 문서, 그 설정) bind to exactly one recent artifact
  (`recent_artifacts`: tool paths and files named in dialogue, filtered by noun kind). Otherwise the
  target is unresolved, which means clarification, never permission.
- Validator reroute (S1): a parsed relation that needs work (required lookup / inspection / external
  change, or an unresolved target) turns a direct design into `relation_requires_work` (validator
  class: no repair call, route goes to execute). The executor receives `reinterpretation`,
  `relations`, `unresolved_conditions` and `mutation_rule` in the execution context.
- Mutation gate (`EvidenceToolRegistry.prepare_call` → `SelectedContext.mutation_gate`): no
  non-read-only tool runs while any target (unresolved relation target, unresolved reference,
  design.unresolved), evidence, or permission condition is open. Permission conditions are:
  `external_change_forbidden`, `answer_edit_only`, or `no_requested_external_change` after a
  validator reinterpretation.
- `answer_only` is decided by relations for both typed and untyped designs: no required work, no
  unresolved target, no unaccounted imperative, no file/tool need, every required need
  acquired/validated, and no mutation. Remaining reasons are typed:
  `requirement_unmet:*`, `information_missing:*`, `target_unresolved:*`.

## Stage 7: corrections reach every next-LLM input (selective only)

Corrections stay separate records (`correction:<orig>`, never editing the original). `nanobot/agent/context_corrections.py`
applies the latest valid state wherever an earlier answer is shown to a model:

| Path | Where | Behaviour |
|---|---|---|
| recent conversation (design + executor `recent_context`) | `minimal_input` → `overlay_records` | content = corrected answer, `verdict=corrected_complete`; scoped id lookup, not limited to the 80-event tail |
| DB search / acquired evidence (`selected.evidence`) | end of `select` | same overlay; the redundant correction record is dropped from evidence |
| memory tools (`search_memory`, `read_memory_events`) | `tools/memory.py` | original text verbatim, then a "Corrections (latest valid state …)" section |
| task status | `reconcile_verdicts` (stage 4) | DONE + `verdict_correction` entry |
| session replay (legacy fallback) and session summaries | `loop.py` → `annotate_session`; `Session.get_history`, `MemoryStore._format_messages` | `content` kept; a `correction` field is preferred when present |

Without correction events (legacy mode never writes them) every path is unchanged. The Consolidator is not run after
selective turns (loop.py `_finalize`), but the token/idle/system paths can still summarise a selective session; those
read the annotated messages.
