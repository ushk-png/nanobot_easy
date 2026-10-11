# 단계별 추론 지연·빈 답변 수정 보고 (2026-10-11)

브랜치: `fix/stepwise-latency-empty-answer` (push 안 함)
대상: 지니의 단계별 추론 엔진 `nanobot/reasoning/` (작업 시작 시 미커밋 상태)

## 요약

### 0단계 결과 — 구현 위치

지니의 구현은 `nanobot/workflow/`(WorkflowService·executor)를 **쓰지 않는 별도 엔진**이다.
지시서의 `workflow/executor.py :: _run_llm`(data 누적) 가정은 이 경로에 해당하지 않았다.

| 항목 | 위치 |
|---|---|
| 진입(라우팅) | `nanobot/agent/loop.py :: AgentLoop._state_run` — `reasoning_effort == "none"`이고 짧은 직접 요청이 아니면 추론 절차로 보냄 |
| 절차 구성 호출 | `nanobot/reasoning/bridge.py :: _propose_dynamic_procedure` → `ProviderStageLLM.complete("procedure", …)` |
| 절차 구조 검사(코드) | `nanobot/reasoning/contract.py :: normalize_procedure`, `validate_normalized_procedure` (`dynamic.py :: prepare_dynamic_procedure`) |
| 목적 적합성 검토 | **없었음** (이번에 `bridge.py :: _review_procedure` 추가) |
| 단계 실행 루프 | `nanobot/reasoning/engine.py :: ReasoningEngine.run` |
| 단계별 LLM 호출 | `bridge.py :: ProviderStageLLM.complete/_complete` → `provider.chat_with_retry(reasoning_effort="none")` |
| 답변 필드 채움 | `engine.py :: _apply` (`role == "answer"`일 때만 `case.answer = payload["answer"]`) |
| "답변을 만들지 못했습니다" | `bridge.py :: run_reasoning_procedure` (실행 후 `answer`가 비면 `stopped`로 바꾸고 문구 설정) |
| 실행 기록 | `<workspace>/memory/dynamic-reasoning.jsonl` (`bridge.py :: _record_execution`, 동적 절차만 기록하던 것을 전체로 확대) + gateway 로그 |

기준선: 지니의 미커밋 변경 전체를 브랜치 첫 커밋 `f080286b`(변경 없음 스냅샷)으로 남기고 그 위에서 수정했다.
이 스냅샷에는 추론 엔진과 무관한 지니 변경(`shell.py`, `exec_session.py`, `cli/up.py`, 문서)도 함께 들어 있다.

### 빈 답변 원인: (d) 그 밖의 원인 — 근거

답변 단계는 있었고(`select`, `justify`의 role = answer, outputs에 `answer`), 출력 필드 이름도 맞았다. (a)·(b)·(c)가 아니다.

1. **단계 호출이 단계 계약을 받지 못했다.** `engine.py :: _payload`는 모든 단계에 같은 내용(요청 + 빈 case 필드 + 단계 id)만 보냈다.
   단계의 `question`, `outputs`, 앞 단계 결과가 빠져 있었다. 수정 전 재현 실행에서 모든 단계 payload가 1,383~1,389바이트,
   입력 토큰이 모두 3,278개로 같았다.
2. **모델은 받은 payload 형태를 그대로 되돌려 보냈다.** 재현 실행의 단계 출력 키는 `answer, conclusion, constraints, criteria, facts, goal, judgments, request, stage`였다.
   단계 프롬프트가 "answer가 있는 단계에서만 최종 답을 쓰고 그 외에는 비운다"고 했지만, 모델은 자기가 답변 단계인지 알 수 없었다.
   그래서 답변 단계(`pick`)도 `answer`를 비워 보냈다.
3. **빈 답변이 검사를 통과했다.** `checks.py :: answer_omissions`는 절차에 conclusion 단계가 없으면 빈 답변에도 결함 없음(`[]`)을 돌려줬다.
   그래서 모든 단계가 `passed`, 엔진 상태가 `completed`가 됐다. 그 뒤 bridge가 `stopped`와 일반 문구로 덮어썼다.
4. 추가로 role이 `work`인 단계의 출력은 어디에도 저장되지 않아, 뒤 단계가 앞 단계 결과를 받을 길이 없었다.

근거의 한계: 08:01·08:27 실행의 절차 정의 원문과 단계 출력 원문은 기록되지 않았다(성공한 동적 절차는 trace를 남기지 않았음).
그래서 위 판정은 **같은 요청을 수정 전 코드로 다시 실행한 결과**(아래 "수정 전 재현")에 근거한다. 같은 증상이었다: 5단계 모두 통과, 답변 0자, 442초.

### 고친 것

| 단계 | 파일 / 함수 | 내용 |
|---|---|---|
| 1 | `providers/openai_responses/parsing.py :: _usage_from_response_obj` | `output_tokens_details.reasoning_tokens`, `input_tokens_details.cached_tokens`, `num_server_side_tools_used` 파싱 (실제 xAI 응답에서 확인한 필드 이름) |
| 1 | `reasoning/bridge.py :: ProviderStageLLM.complete`, `_finish_calls`, `_call_totals`, `_record_execution` | 호출별 `run_id, call_kind, step_id, duration_ms, model, reasoning_effort, reasoning_request(실제 보낸 값), 입력/출력/추론 토큰, payload_bytes` 기록, 실행 합계, 호출당 구조화 로그 한 줄 |
| 1 | `providers/xai_grok_provider.py :: reasoning_request_options` | 요청 본문에 실제로 실리는 `reasoning` 값을 계측에 노출 |
| 3 | `reasoning/contract.py :: _link_defects` | `answer` 출력을 선언한 단계가 없으면 실행 전 거부. role 라벨만 있는 경우도 거부 |
| 3 | `reasoning/engine.py :: run, _payload, _apply, _retry_same_step` | 단계 payload에 `question`·`expected_outputs`를 넣음. 단계별 출력 저장. 빈 답변이면 답변 단계만 1회 재요청(`repair_defects`). 끝까지 비면 `failed_empty_answer` |
| 3 | `reasoning/checks.py :: answer_omissions` | conclusion 단계 유무와 관계없이 빈 답변은 결함 |
| 3 | `reasoning/bridge.py :: _empty_answer_status`, `_procedure_outline` | 사용자 문구에 비어 있는 답변 단계 id를 표시. 실행한 절차 정의를 trace에 저장. `waiting_info` 질문을 덮어쓰지 않음 |
| 4 | `reasoning/bridge.py :: _review_procedure`, `review_defects`, `_REVIEW_PROMPT` | 생성과 분리된 목적 적합성 검토 호출. 입력은 요청·확인된 목표·절차 정의뿐. 검토자는 요청 항목→단계 id, 답변 단계 id만 답하고 통과 여부는 코드가 판정 |
| 5 | `providers/xai_grok_provider.py :: reasoning_disable_supported` | grok-4.5/4.6/4.7은 `False`(공식 문서 근거), 그 외는 `None`(미확인) |
| 5 | `reasoning/bridge.py :: _reasoning_mode` | 실행 기록에 `reasoning_mode`(요청 값, 지원 여부, `unsupported: …`) 저장 + 경고 로그 |
| 5 | `workflow/llm_adapter.py :: call_json` | 설정된 `reasoning_effort`를 provider에 전달 |
| 6 | `reasoning/engine.py :: _case_fields`, `_ROLE_FIELDS` | 단계 payload = 요청·목표·제약 + 역할이 읽는 case 필드 + 선언한 inputs의 출력/요구조건만 |
| 6 | `reasoning/contract.py` | 단계가 여러 개인데 답변 단계에 단계 입력이 없으면 거부(`answer_inputs_missing`). 요구조건을 `R1: 내용` 형태로 보존 |
| 6 | `reasoning/engine.py :: _apply` | 형식이 틀린 typed 출력(예: 동적 단계의 `judgments`)이 턴 전체 예외가 아니라 단계 결함이 되도록 함 |
| 7 | `reasoning/bridge.py :: _PROCEDURE_PROMPT` | inputs에는 단계 id/`request`만 넣을 것, 답변 단계 question에 요청 항목을 빠짐없이 쓸 것, repair_defects 처리법 |

### 설계서와 다르게 구현한 부분과 사유

- **설계서 원문을 이 기기에서 찾지 못했다.** 절 번호와 요구사항은 지시서에 인용된 내용을 기준으로 했다.
- **6단계의 "payload 크기 감소"는 동적 절차에서는 일어나지 않았다.** 수정 전 동적 단계 payload가 작았던 것은 필요한 값이 빠져 있었기 때문이다.
  수정 후에는 질문·출력 계약·앞 단계 결과가 들어가 평균 1,385 → 1,695바이트로 늘었다.
  승인 절차(S1~S7)에서는 역할별로 필요한 필드만 보내 줄어든다(테스트로 확인했고, 실측은 안 함).
  입력 토큰의 대부분(호출당 약 2,850개)은 payload가 아니라 고정 오버헤드다(아래 "미확인·남은 문제" 참고).
- **동일 단계 재요청**: `failure_returns_to`가 없는 단계와 "답변 비어 있음" 결함은 그 단계만 다시 요청한다. 단계 호출 예산(`max_calls=2`)과 `max_revisions`를 함께 소비한다.
  답변만 빈 경우 S5로 되돌리면 호출이 3배로 늘기 때문이다.
- **검토 통과 판정은 코드가 한다.** 검토자에게 통과/불통과 필드를 주지 않았다. 매핑되지 않은 요청 항목, 답변 단계 부재, 검토자가 적은 결함 중 하나라도 있으면 불통과다.
- **5단계 "설정 화면 표시"는 하지 않았다.** 실행 기록(`reasoning_mode`)과 로그 경고로만 표시한다.

## 재실행 결과

같은 요청(실행 기록의 08:27 건, 요청 길이 390자)을 지니 설정(`config.genie.json`, grok-4.7, effort none, temperature 0.1, maxTokens 8192)으로 `run_reasoning_procedure`에 직접 넣어 실행했다.
gateway·텔레그램 경로는 거치지 않았다. 원문은 출력하지 않았다.

### 수정 후 최종 재실행 (커밋 `40a5461c`)

| call_kind | step_id | duration_ms | 입력 토큰 | 출력 토큰 | 추론 토큰 | payload 바이트 |
|---|---|---:|---:|---:|---:|---:|
| compose | – | 81,995 | 3,389 | 7,890 | 7,658 | 1,804 |
| review | – | 54,272 | 3,602 | 4,300 | 4,156 | 2,452 |
| step | S1 | 20,042 | 3,327 | 1,683 | 1,660 | 1,545 |
| step | S2 | 18,467 | 3,358 | 1,649 | 1,600 | 1,641 |
| step | S3 | 15,349 | 3,383 | 1,309 | 1,283 | 1,719 |
| answer | S4 | 23,960 | 3,434 | 2,303 | 2,192 | 1,876 |

- 총 6회 / 214초(LLM 호출 합 214,085ms) / 입력 20,493 · 출력 19,134(추론 18,549) / payload 11,037바이트
- 수정 전 126초·284초(08:01·08:27)와 비교: 이번 실행은 214초. 단계 호출 4회 합은 77.8초(호출당 15~24초)였다.
  시간의 64%(136초)는 절차 구성+검토다. 수정 전 기록에는 호출별 시간이 없어 같은 기준으로 비교할 수 없다.
- 상태 `completed`, **최종 답변 채워짐**(153자). 앞부분: "확인된 필수 조건 네 가지를 모두 충족한 제품은 C뿐이므로 C를 선정한다. A는 감사 로그가 없어 필수 조건 하나를 충족하지 못해 제외한다. B는 도입비 5,200만 원으로 5,000만 원 한도를 초과해 제외한다. D는 …"

### 수정 전 재현 (커밋 `b7410de7`, 계측만 추가)

| call_kind | step_id | duration_ms | 입력 토큰 | 출력 토큰 | 추론 토큰 | payload 바이트 |
|---|---|---:|---:|---:|---:|---:|
| compose | – | 50,738 | 3,261 | 5,500 | 5,137 | 1,359 |
| compose | – | 47,479 | 3,960 | 3,866 | 3,527 | 3,894 |
| step | net | 28,888 | 3,278 | 2,980 | 2,683 | 1,383 |
| step | audit | 57,125 | 3,278 | 4,311 | 3,869 | 1,387 |
| step | volume | 85,549 | 3,278 | 6,562 | 6,209 | 1,389 |
| step | cost | 85,027 | 3,278 | 7,983 | 7,441 | 1,385 |
| answer | pick | 87,207 | 3,278 | 9,146 | 8,484 | 1,385 |

- 총 7회 / 442초 / 입력 23,611 · 출력 40,348(추론 37,350) / 12,182바이트. 5단계 모두 `passed`, 답변 0자.

### 중간 재실행 (커밋 `5116edab`, 6단계까지 적용)

- 총 5회(compose 3, review 2) / 276초. 단계는 실행되지 않았다.
- 첫 절차는 inputs에 제품·데이터 이름을 넣어 구조 검사에서 거부됐다.
- 다음 두 절차는 검토에서 "다른 제품을 뺀 이유를 쓰는 단계 없음" 등으로 거부됐다. 수정 한도를 넘겨 실행 없이 중단했다.
  사용자 문구: "요청의 일부를 처리하거나 최종 답을 쓰는 단계가 없는 절차라 실행하지 않았습니다."
- 그래서 구성 프롬프트를 고치고(7단계 커밋) 다시 실행했다. 결과는 위의 최종 재실행이다.

## 추론 설정 판정

**안 꺼짐.**

- 측정: effort `none`일 때 요청 본문의 `reasoning` 필드는 없다(`reasoning_request: null`). 그런데 모든 호출에서 추론 토큰이 1,283~8,484개 나왔고, 출력 토큰의 90~97%였다.
- 아주 작은 요청으로 따로 확인했을 때: effort none(생략)은 추론 302토큰·5.2초, effort `low`는 69토큰·2.2초였다.
- 공식 문서(docs.x.ai Reasoning 가이드): grok-4.7/4.6/4.5는 `reasoning.effort`로 `low|medium|high`(4.6·4.7은 `xhigh`도)를 받는다.
  "Reasoning cannot be disabled"이고, 지정하지 않으면 `high`가 기본값이다. `none` 값은 문서에 없다.
  즉 현재 설정 `none`은 실제로는 **가장 높은 기본 추론(high)** 으로 돈다.
- 조치: 추측으로 요청 필드를 만들지 않았다. 실행 기록에 `reasoning_mode.status = "unsupported: this model has no non-reasoning mode"`를 남기고 경고 로그를 낸다. 모델·effort 설정은 바꾸지 않았다.

## 테스트

```
$ .venv/bin/python -m pytest tests/reasoning tests/workflow tests/providers/test_xai_grok_provider.py tests/providers/test_openai_responses.py -q -p no:cacheprovider
198 passed in 0.75s
```

- `tests/reasoning`: 기준선 39개 → 62개. 새 파일은 `test_stepwise_instrumentation.py`, `test_empty_answer.py`, `test_fitness_review.py`, `test_reasoning_mode.py`, `test_step_inputs.py`다.
- 기존 테스트 두 곳을 바꿨다. 둘 다 약화가 아니다.
  - `test_dynamic_procedure.py`의 가짜 LLM에 review 응답을 추가했다. 새로 생긴 필수 호출에 답하게 한 것이고, 기존 단언은 그대로다.
  - 빈 답변 테스트의 기대 상태를 `stopped` → `failed_empty_answer`로 바꾸고, 문구 단언을 더 구체적으로 했다.

전체 스위트(브랜치 vs 기준선 커밋 `f080286b`, 같은 venv):

```
branch  : 93 failed, 4945 passed, 16 skipped
baseline: 94 failed, 4921 passed, 16 skipped
```

- 브랜치의 실패 93건은 모두 기준선에서도 실패한다.
  feishu_streaming 35, test_restart_command 17, test_skills_loader 11, feishu_reaction 10, task_cancel 7, 기타 13건이다.
  agent 쪽 실패는 `skill_store._ensure_schema`에서 난다. 기준선에만 있는 1건(`test_websocket_http_routes`)은 병렬 실행 때 흔들린 것으로 보인다(미확인).
- 전체 스위트가 저장소 루트에 `memory/`, `<MagicMock …>/` 디렉터리를 만든다(기존 테스트의 누수). 실행 후 지웠다.

```
$ .venv/bin/ruff check nanobot/reasoning nanobot/workflow/llm_adapter.py nanobot/providers/xai_grok_provider.py nanobot/providers/openai_responses tests/reasoning
All checks passed!
$ .venv/bin/ruff check nanobot/
Found 19 errors.
```

- `ruff check nanobot/`의 19건은 `origin/main`과 항목이 완전히 같다(diff 없음). 이 브랜치가 만든 것은 없다.
  관련 없는 파일(`workflow/*`, `memory/*`, `agent/tools/*`, `agent/loop.py` import 순서)이고, `N818` 예외 이름 변경은 API 변경이라 손대지 않았다. **완료 조건의 "ruff 통과"는 충족하지 못했다.**

## 사용자 결정이 필요한 항목

1. **단계 호출의 추론 강도** (지연의 주원인)
   - (a) 현 상태 유지 — `none` = 실제로는 `high`. 이번 성공 실행 기준 약 214초.
   - (b) **권장: 추론 절차 호출만 `low`로 지정.** 작은 요청 실측에서 추론 토큰 약 1/4, 시간 약 절반이었다. 전체 절차로는 아직 측정하지 않았다.
     `bridge.py :: STAGE_REASONING_EFFORT` 한 곳에서 바꿀 수 있다. 다만 사용자 설정 `none`과 의미가 달라지므로 승인 후 진행한다.
   - (c) 단계 호출 전용으로 비추론 모델을 지정. 구독 프록시에서 쓸 수 있는 비추론 모델은 확인하지 못했다(미확인). 모델 교체이므로 따로 결정이 필요하다.
2. **절차 재사용** — 시간의 64%가 구성+검토다. 같은 유형 요청은 검토를 통과한 절차를 재사용하면 이 두 호출을 건너뛸 수 있다(이번 범위 아님).
3. **검토 엄격도와 수정 한도** — 중간 재실행에서 검토가 매번 다른 누락을 지적해 한도(`MAX_DYNAMIC_RECOVERY=2`, 최대 3회 구성)를 넘겼다.
   한도를 늘리면 성공률이 오르지만 호출당 45~80초가 더 든다. 현행 유지를 권장한다.
4. **기준선 스냅샷 커밋** — 지니의 무관한 변경(`shell.py`, `exec_session.py`, `cli/up.py`, 문서)이 `f080286b`에 함께 들어갔다. PR 전에 분리할지 정해야 한다.
5. **gateway 재시작** — 실행 중인 지니 gateway(PID 17156)는 수정 전 코드로 떠 있다. 브랜치 코드를 쓰려면 재시작해야 하는데, 서비스가 잠깐 중단되므로 하지 않았다.

## 미확인·남은 문제

- 08:01·08:27 실행의 절차 정의·단계 출력 원문은 남아 있지 않다. 원인 판정은 같은 요청의 수정 전 재현에 근거한다. 이제는 `procedure`와 호출별 `output_keys`가 기록된다.
- 성공한 실행은 1회뿐이다(중간 재실행 1회는 검토에서 중단). 성공률과 시간 편차는 측정하지 않았다.
- 텔레그램 → gateway → `AgentLoop._state_run` 전체 경로로는 실행하지 않았다. `_state_run`은 단위 테스트로만 확인했다.
- **호출당 고정 입력 약 2,850토큰**: 4바이트짜리 요청도 입력 2,847토큰이었다. 단계 호출은 `tools=None`인데도 provider가 hosted `x_search` 도구를 붙이고 `tool_choice: "auto"`를 보내는 것이 원인으로 보이지만, 프록시 측 지시문일 수도 있다(미확인).
  측정한 실행에서 `num_server_side_tools_used`는 모두 0이었다.
- **버그(미수정)**: `hosted_search=False`이고 도구가 없으면 xAI가 `tool_choice was set … but no tools were specified`로 HTTP 400을 돌려준다(실측). 안전 모드에서 추론 절차 호출이 전부 실패할 수 있다. `xai_grok_provider.py :: _call_xai`에서 도구가 없으면 `tools`/`tool_choice`를 빼야 한다.
- 승인 절차(S1~S7)의 payload 감소는 테스트로만 확인했고 실측하지 않았다.
- 설정 화면에 "비추론 모드 미지원"을 표시하는 작업은 하지 않았다(로그·실행 기록만).
- 단계 프롬프트의 "20자 안팎" 짧은 질문 규칙과 검토의 항목 단위 매핑이 충돌한다. 답변 단계만 예외로 두었는데, 다른 유형의 요청에서도 맞는지는 확인하지 않았다.

## 후속 후보 (이번 범위 아님)

- 같은 유형 요청의 절차 재사용(검토 통과 절차 등록·재사용 정책 포함)
- 서로 의존하지 않는 단계의 병렬 실행(`_execution_order`의 같은 층 단계)
- 진행 상황 스트리밍(단계 시작 알림, 예상 남은 시간)
- `x_search` 자동 첨부·`tool_choice` 400 문제 정리와 고정 입력 오버헤드 축소
