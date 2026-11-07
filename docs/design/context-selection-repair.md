# 컨텍스트 선택 감사 결함 수정 기록

운영 활성화·재시작·유료 호출 없이 코드와 모의/임시 파일 검증만 수행한다.
기존 감사의 기대 조건은 유지하고 알려진 실패 표시(xfail)를 제거해 회귀 검사로 전환한다.

## 반례별 원인과 공통 수정 경로

| 반례 | 요구사항/잘못된 판정 | 기존 경로 | 공통 수정 함수 | 검증할 결과 |
|---|---|---|---|---|
| 1 요청문을 승인 자료로 인정 | 필수 자료/의미 판단 분리 위반 | select의 acquired 판정 | select + need_states/match_need | 요청문만으로 n1 해소 불가 |
| 2 다른 파일로 변경 허용 | 대상·버전·범위 연결 없음 | resolver/runner observations | context_evidence.match_need + authorize | 다른 파일/부분/구버전은 변경 차단 |
| 3 읽기 성공을 테스트 통과로 저장 | 결과 종류/변경 버전 검증 없음 | completion 도구 | validate_completion | 실제 test_result/exit code/버전 필요 |
| 4 실행 전 취소 무시 | 별도 task note에 종속 | runner 도구 경계 | TaskControl.run_mutation/authorize | 최신 지시 버전으로 변경 차단 |
| 5 취소한 A가 미완료로 남음 | 사용자 변경 투영 없음 | select/finish | 사용자 근거 기반 대체 투영/TaskControl | A 대체 기록, B 목표/조건 반영 |
| 6 무관한 과거 사용자 기록 편입 | stale이면 전체 사용자 재편입 | select bootstrap | 작업별 source 연결 | 같은 작업 원문만 복원 |
| 7 외부 요약 필드가 원문 삭제 | 자료와 제어 메타데이터 혼합 | prepare_selected_loop | 출처 있는 내부 요약 계약 | 외부 JSON 원문 유지 |
| 8 반복 한도 후 완료 주장 | 마지막 경로에 검사 없음 | max_iterations finalizer | SelectedContext.verdict | 미완료/남은 조건으로 종료 |
| 9 읽기 전용과 변경 목표 충돌 | 일부 동사만 의미 검사 | validate_meaning | instruction_policy/goal_conflict/authorize | 금지된 목표·실행 모두 거부 |
| 10 stale 제약을 건너뛴 direct | 미복원 출처 검증 없음 | minimal_input/direct | 작업 제약 출처 복원/직접 경로 검사 | 누락 출처 있으면 direct 금지 |
| 11 취소 시 호출 기록 누락 | 완료 후에만 계측 저장 | provider/finish | ContextTrace.begin/end 저널 | 전송 전 생성, 취소 후에도 영속 |

상세 구현·검증 결과는 작업 완료 시 아래에 기록한다.

## 수정 전후 결과

수정 전 결과는 [원래 감사](context-selection-audit.md)의 `--runxfail` 실행에서 11개 모두 실패였다.
원래 11개 테스트의 assert와 기대값을 유지하고 모듈 전체의 xfail 표시만 제거했다.

| 번호 | 수정 전 | 수정 후 회귀 결과 |
|---|---|---|
| 1 | 요청문이 승인 문서를 대신함 | 필수 원본은 미확보로 유지; 현재 요청은 승인 근거가 아님 |
| 2 | 무관한 파일 읽기로 변경 실행 1회 | 대상 불일치 거부; 변경 실행 0회 |
| 3 | 파일 읽기로 테스트 passed 저장 | 실제 테스트 실행·성공·변경 버전 근거 없으면 거부 |
| 4 | 대기 중 취소 뒤 변경 실행 1회 | 별도 task note 없이도 실행 직전 취소 확인, 실행 0회 |
| 5 | 취소한 A가 목표/미완료로 남음 | 사용자 원문에 근거해 A 대체 기록, B 목표와 완료 조건 반영 |
| 6 | 무관한 과거 사용자 발화를 제약으로 편입 | 연결한 작업의 출처만 복원; 무관한 기록 편입 없음 |
| 7 | 외부 context_summary 때문에 필수 원문 삭제 | 외부 필드는 일반 자료로 유지; 필수 원문 보존 |
| 8 | 필수 근거 없이 반복 한도 뒤 완료 응답 | 공통 검증으로 incomplete와 남은 조건 반환 |
| 9 | 읽기 전용 지시와 수정 목표 충돌 허용 | 설계 의미 검사와 실제 변경 경계에서 거부 |
| 10 | stale 제약 미복원 상태로 직접 응답 | 미복원 출처가 있으면 direct 금지 |
| 11 | 취소된 모델 시도 기록 유실 | 전송 전 시도 영속화; 취소 후에도 횟수/미확인 사용량 보존 |

## 공통 계약과 코드 위치

- `nanobot/agent/context_control.py`: `instruction_policy`, `TaskControl.refresh/authorize/run_mutation`.
  작업별 지시 버전, 실제 호출별 실행 원장, 최신 원문 확인, CAS 갱신을 공유한다.
  실행 경계는 기존 단일 게이트웨이 이벤트 루프/세션 직렬화에 더해 변경 시작을 직렬화하고,
  마지막 확인부터 실제 도구 dispatch까지 await 없이 진행한다. 준비 후 추가 대기 지시도 다시 확인한다.
  복수 독립 게이트웨이 프로세스가 동일 작업을 동시에 실행하는 분산 실행 잠금까지 검증한 것은 아니다.
- `nanobot/agent/context_evidence.py`: `observation/match_need/current/validate_completion`.
  도구 생산자가 붙인 메타데이터만 수용한다. 작업/요구/호출/원본 이벤트, 실제 대상, SHA-256,
  읽은 범위, 성공 여부와 결과 종류를 연결한다. 같은 버전의 부분 읽기만 합칠 수 있다.
  명시한 JSON 기대값은 코드로 확인하고, 자연어 의미는 별도의 출처 연결된 실행 모델 판단으로 남긴다.
- `nanobot/agent/context_selection.py`: `SelectedContext.verdict/completion_supported`,
  `ResolveContextNeedTool`, `RecordContextCompletionTool`, `prepare_selected_loop`.
  자료 확보와 의미 평가를 구분하고, 정상/직접/오류/반복 한도 종료에 같은 완료 검증을 적용한다.
  완료 검증 때 파일이 바뀐 경우 이전 읽기/테스트 근거를 다시 무효화한다.
  반복 한도·오류·미해결 필수 자료는 성공이 아니며 실제 완료 기록과 남은 조건을 표시한다.
- `nanobot/agent/runner.py`, `loop.py`: 선택 입력부터 실제 도구 dispatch, 새 사용자 지시 주입,
  최종 반환까지 위 계약을 연결한다. 선택 경로의 미검증 응답 텍스트 스트리밍은 보류하여
  검증 전에 성공 문장이 사용자에게 노출되지 않게 한다. 도구 진행 알림은 유지한다.
- `nanobot/agent/tools/filesystem.py`, `memory.py`, `shell.py`: 실제 읽기/원문 복원/명령 결과의
  신뢰 가능한 생산자 속성을 제공한다. 파일 읽기 중 내용 교체도 감지한다.
  exec는 실제 테스트 명령·종료 코드·실행 테스트 수·검사 전후 자료 버전을 기록한다.
  도움말/수집 전용/명령 제안/echo는 테스트 통과 근거가 아니다.
- `nanobot/agent/context_trace.py`, `providers/base.py`, `fallback_provider.py`:
  실제 제공자 시도마다 전송 전에 저널을 만들고 응답/실패/취소/타임아웃 상태를 갱신한다.
  fallback 래퍼의 중복 계수 없이 실제 하위 시도를 기록한다.

## 취소·복원·저장

- 실행 전 취소: 새 변경 차단. 실행 중 취소: `request_cancel` 또는 명시적 cancellation 지원이 있는
  도구에 중단 요청. 지원하지 않는 도구는 이미 시작한 실행 결과를 기다려 사실대로 기록한다.
- 실행 완료 후 취소: 실제 성공/실패와 취소 도착 시점을 함께 보존한다. 완료된 변경을 취소된 것처럼
  보고하지 않으며 자동 undo는 수행하지 않는다. 외부 runner 중단 뒤 늦은 결과도 원장에 추가한다.
- 취소 상태는 `instruction_status=CANCELLED` 및 취소 원장으로 보존한다.
  일반 “계속”으로 재개하지 않으며 명시적인 재개 지시는 새로운 지시 버전으로 다룬다.
  불완전한 기존 투영은 일부 호환 경로에서 `status=BLOCKED`로 표현될 수 있어, 취소 판단은 별도 필드를 우선한다.
- 기존 `tasks/task_context_versions`의 JSON 투영에 지시/실행/요구 근거/완료 평가/취소 항목을 추가한다.
  원본 JSONL/이벤트는 삭제·수정하지 않는다. 도구 결과 원본은 안정적인 이벤트 ID로 중복 저장을 막는다.
- 추가 SQLite 테이블 `context_model_attempts`: `attempt_id` 기본키와 `(request_id, call_sequence)` 유일성.
  요청·작업·단계·시도·재시도 연결과 상태만 기록하고 프롬프트/응답 원문/인증정보는 계측에 기록하지 않는다.
  사용량을 모르면 `provider_usage=None`, 전체 합계는 null, 확인된 부분만 별도 합계로 제공한다.
- 프로세스 소실 시 전송 전 생성한 `in_flight` 행은 남는다. `recover_abandoned()`는 이전 워커가
  종료됐다고 확인한 요청에 한해 `result_unknown`으로 바꾸는 명시적 복구 경로다.
  실행 중인 다른 프로세스의 시도를 임의로 실패/0토큰 처리하지 않는다.

## 로컬 검증

유료 호출 없이 모든 설계/실행 모델 응답은 모의 응답을 사용했다. 실제 임시 파일·SQLite·세션 JSONL과
실제 읽기/쓰기/재읽기/임시 pytest 명령 실행을 통해 입력 조립 → 선택 → 실행 → 검증 → 저장을 통과했다.
전체 저장소 테스트를 실행한 것은 아니며 변경된 경로의 집중 회귀 검사를 실행했다.

- 기존 컨텍스트 선택 54개, 원래 감사 11개, 인접/통합 회귀 42개를 함께 검사한다.
- 인접 검사는 대상/범위/버전/JSON 기대값, 새 지시 충돌, 취소 전·중·후와 늦은 결과,
  숨은 하위 작업 결과의 권한 오인, 취소 후 명시적 재개와 일반 계속,
  비슷한 서로 다른 작업, 중복 이벤트/호출, 실제 테스트 명령과 소스 변경,
  스트리밍 노출 방지, 응답 전후 취소 및 fallback 시도 원장을 포함한다.
- 기존 테스트의 기대값은 변경하지 않았다. 정상 경로 모의 읽기는 실제 임시 파일의 생산자 메타데이터를
  반환하도록 보강했고, 내부 요약 정상 사례는 실제 원본 이벤트/해시를 등록하도록 보강했다.
  외부 JSON 필드만으로 요약을 신뢰하는 반례는 그대로 유지한다.

최종 검사 수와 명령은 아래 최종 결과 항목에 기록한다.

## 미확인 사항과 운영 전 비교

이번 단계는 **재현된 결함 수정과 로컬 검증 완료**다. 실제 모델의 설계 정확도, 자연어 의미 판단,
답변 품질·속도·작업 완료율 보존까지 입증한 것은 아니다. 임의 자연어의 승인/거부를 코드가 완전히
판별한다고 주장하지 않는다. 불확실한 새 지시는 재조정 상태로 유지하고 변경을 차단한다.
지원하지 않는 도구의 중단/원격 효과 확인은 해당 도구의 계약에 의존한다.

메인은 설정 생략에 따른 기본 false, TESTY는 명시적 false를 유지했다. 이번 작업은 설정 변경,
서비스 활성화·재시작·유료 모델 호출을 하지 않았다. 기존 경로 복귀 설정은
`agents.defaults.contextSelection.enabled=false`다.

운영 전 별도로 승인한 제한 비교에서 동일 모델·도구·권한·자료 스냅샷과 새 세션을 사용한다.
기존 경로/선택 경로에 동일 요청을 넣고 지시·참조·필수 근거·변경 결과·완료율을 우선 평가한다.
토큰은 설계 호출/재시도/추가 조회/실행 전체를 합산하고, 미확인 사용량이 있는 요청은 완전한 합계로
비교하지 않는다. 지연 시간은 요청 수신부터 마지막 완료/불완료 응답까지 측정한다.
TESTY에서 설명·연속 수정·과거 작업 재개·중간 취소 사례를 소수로 먼저 비교하고,
실제 품질과 완료율이 나빠지거나 근거 보존이 깨지면 비활성 상태를 유지한다.

## 최종 결과

- 컨텍스트 선택: 기존 **54 + 감사 11 + 추가 42 = 107개 통과**.
- 아래 관련 집중 회귀 전체: **626 passed in 17.40s**.
- 변경한 Python 파일 전체 `ruff check` 통과, `git diff --check` 통과.
- pytest 종료 후 기존 SWIG 바인딩의 `swigvarlink` DeprecationWarning 1건이 표시됐다. 테스트 실패는 없다.
- 전체 저장소 테스트·운영 실행·실제 유료 모델 비교는 수행하지 않았다.

```bash
.venv/bin/python -m pytest \
  tests/agent/test_context_selection.py \
  tests/agent/test_context_selection_requirements_audit.py \
  tests/agent/test_context_selection_repair.py \
  tests/agent/test_answer_delivery.py \
  tests/agent/test_runner_premature_final.py \
  tests/agent/test_runner_core.py \
  tests/agent/test_runner_injections.py \
  tests/agent/test_runner_goal_continue.py \
  tests/agent/test_runner_progress_deltas.py \
  tests/agent/test_runner_errors.py \
  tests/agent/test_runner_fallback.py \
  tests/agent/test_loop_runner_integration.py \
  tests/agent/test_loop_save_turn.py \
  tests/agent/test_repetition_guard.py \
  tests/agent/test_task_note.py tests/agent/test_task_progress.py \
  tests/test_memory_event_store.py tests/test_memory_importer.py \
  tests/agent/test_memory_tools.py \
  tests/tools/test_filesystem_tools.py tests/tools/test_search_tools.py \
  tests/channels/test_channel_manager_reasoning.py \
  tests/channels/test_channel_manager_delta_coalescing.py \
  tests/config tests/providers/test_provider_retry.py \
  tests/providers/test_provider_retry_after_hints.py \
  tests/providers/test_xai_grok_provider.py -q --tb=short
```

주요 함수 위치: `context_control.py:98`(최신 지시 확인), `:173`(변경 허가 검사),
`:208`(취소와 실행 경계), `context_evidence.py:109`(필수 자료 계약), `:173`(완료 근거),
`context_selection.py:347`(공통 종료 검증), `:389`(완료 조건 재검증),
`:1054`(요약 선택), `context_trace.py:48`(전송 전 시도 저장), `:74`(응답 후 갱신),
`runner.py:1125`(검증 전 응답 델타 차단). 위치는 이 작업 완료 시 소스 기준이다.
