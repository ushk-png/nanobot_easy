# 컨텍스트 선택 요구사항 감사 — 2026-10-26

> 아래는 수정 전 감사 기록이다. 이후 코드 수정과 동일 반례의 회귀 결과는
> [감사 결함 수정 기록](context-selection-repair.md)에 기록했다. 기존 기대 조건은 유지했다.

## 판정과 범위

**현재 구현은 원래 요구사항을 충족하지 못한다. 운영 전환 불가.**
스키마·검색·상태 저장·선택 입력·복구 도구의 골격은 있으나, 사용자 제약과 필수 근거,
취소 지시, 완료 판정의 보존에서 반례가 재현됐다. 타임아웃 단축이나 추론 강도 변경은
이 문제를 해결하거나 작업 완료율을 입증하지 않는다.

대상은 HEAD `89ff0f2c`(선택 기능 최초 커밋 `7f90cca1`)와 작업 디렉터리에 남아 있는
후속 미커밋 수정 전부다. 최근 대기 정책 변경은 아직 실제 모델로 검증하지 않았다.
이번 감사에서는 운영 코드를 추가 수정하지 않았고, 감사 테스트와 이 보고서만 추가했다.
기존 미커밋 변경은 보존했다. 서비스 재시작·설정 변경·유료 LLM 호출을 하지 않았다.
점검 시 메인과 TESTY 설정 모두 선택 기능 false였다.

## 재현 결과

`tests/agent/test_context_selection_requirements_audit.py`는 임시 파일/DB와 모의 모델만 쓴다.
기존 테스트의 성공 경로를 반복하는 대신 요구사항의 실패 경계를 검사한다.

```bash
.venv/bin/python -m pytest tests/agent/test_context_selection_requirements_audit.py --runxfail -q --tb=short
# 요구사항의 기대 조건을 그대로 검사: 11 failed (0.75초)

.venv/bin/python -m pytest tests/agent/test_context_selection.py tests/agent/test_context_selection_requirements_audit.py -q
# 기존 컨텍스트 선택 사례 54 passed, 알려진 감사 결함 11 xfailed (3.95초)
```

11건은 실제 미충족 조건이다. `xfail(strict=True)`는 알려진 결함을 눈에 보이게 보존하는
표시이며, 통과나 승인으로 계산하지 않는다. `--runxfail`로 항상 실제 실패를 재현할 수 있다.
수정 후에는 해당 xfail을 제거하고 통과해야 한다. 감사 테스트 Ruff 검사 통과.
이전 보고의 수백 개 회귀 테스트 통과는 아래 의미적 경계를 검증한 증거가 아니었다.

## 중요 발견

### F1 — 필수 자료의 존재와 확보 기준 충족을 혼동한다 (P1, 요구사항 4·5)

관련 코드: `context_selection.py:529`, `:575`, `:675`, `runner.py:684`.

- `information_needs.acceptance`는 스키마/프롬프트에만 존재하고 실제 확보 판정에 사용되지 않는다.
- 기억 자료는 어떤 이벤트라도 복원되거나 source ID가 이미 입력에 있으면 missing에서 제외된다.
  승인 문서가 필요한데 그 문서를 요구한 사용자 요청 자체를 자료로 지정해도 통과했다.
- 파일 자료 해소는 성공한 도구가 read_file인지만 확인한다. 경로·버전·범위·내용은 연결하지 않는다.
  실제 임시 파일 `unrelated.txt`를 읽은 다음 settings.json 확인 완료라고 제출하자
  모의 변경 도구가 1회 실행됐다. 운영 파일을 바꾼 시험은 아니다.

반례: `test_required_memory_fact_cannot_be_satisfied_by_request_text_alone`,
`test_reading_unrelated_file_cannot_unlock_required_file_change`.

### F2 — 새 취소 지시를 변경 직전에 확인하는 보호가 별도 기능에 종속된다 (P1, 2·8)

관련 코드: `runner.py:648`, `:1477`, `:303`.

도구 실행 전/배치 전 사용자 지시 확인은 `spec.task_state is not None`일 때만 작동한다.
이는 별도 lightweight task note 설정이며 contextSelection 활성화만으로 생성되지 않는다.
선택 기능 단독 상태에서 before_execute_tools 시점에 취소 지시가 대기열에 도착해도
모의 변경 도구를 1회 실행한 뒤 지시를 읽었다. 완료 기록만 지워도 이미 수행된 변경은 막지 못한다.

반례: `test_queued_cancellation_is_checked_before_tools_without_task_note`.

### F3 — 도구 성공을 완료 조건의 증명으로 과대 해석하며, 마지막 응답 경로가 검사를 우회한다 (P1, 6·7·9)

관련 코드: `context_selection.py:240`, `:709`, `runner.py:473`, `:989`, `:1323`.

- 완료 도구는 check 문자열이 계획에 있고 지정 호출이 성공했는지만 본다. 해당 결과가
  그 조건을 뒷받침하는지는 검사하지 않는다. README 읽기 성공으로 “통합 테스트 실행·통과”를
  passed로 저장하는 반례가 통과했다.
- 수정·실행 분류도 관측 목록 중 아무 변경 도구 하나의 성공이면 되며 목표/결과 연결이 없다.
- 반복 한도 소진 후 도구 없는 최종 정리 호출에는 selected_context의 필수 근거·완료 검사가 없다.
  필수 근거 n1이 unresolved이고 읽기만 했는데 “설정을 변경하고 검증을 완료했습니다”가 그대로
  최종 텍스트로 반환됐다. 내부 stop_reason이 max_iterations라는 사실로 사용자에게 표시된
  미검증 완료 주장이 정정되지는 않는다.
- 후속 수정의 answer_only 예외는 답변 전달 여부와 의미적 정확성 검증을 구분했지만,
  체크리스트 충족의 검증을 대체하지는 못한다.

반례: `test_successful_unrelated_read_cannot_prove_tests_passed`,
`test_iteration_finalizer_must_not_claim_completion_with_missing_evidence`.

### F4 — 유효한 제약 복원과 의미 검증이 일부 표현의 정규식에 의존한다 (P1, 3·5)

관련 코드: `context_selection.py:145`, `:155`, `:165`, `:288`, `:309`.

- 명시적 금지와 목표의 충돌 검사는 배포·재시작·삭제·발송의 일부 표현만 검사한다.
  “읽기만 하세요. 절대로 수정하지 마세요”에 “파일을 수정한다”는 설계를 제출해도 오류가 없었다.
- 상태의 유효 제약 후보는 특정 단어가 있는 줄만 뽑는다. “출력 언어는 한국어이며 예산은
  0원” 같은 긍정형 제약이 최근 구간 밖에 있으면 원문은 설계 입력에 복원되지 않는다.
- task_state=stale, 미복원 제약 출처 ID가 남아 있어도 “같은 조건으로”라는 후속 요청의
  직접 답변 설계가 통과했다. missing/stale 표시만 붙이는 것으로 보존이 보장되지 않는다.

반례: `test_explicit_no_edit_constraint_rejects_edit_goal_in_semantic_validation`,
`test_direct_followup_must_not_skip_unloaded_positive_task_constraints`.

### F5 — 임의 문서의 context_summary를 신뢰해 필요한 원문을 제거한다 (P1, 5·7)

관련 코드: `context_selection.py:827`.

오래된 도구 결과가 JSON이며 facts/status/error/verification 키를 가진 context_summary가
있으면 이를 믿고 나머지 본문을 제거한다. 신뢰된 도구 생산자가 제공한 요약 계약인지,
파일/검색 원문에 우연히 있던 필드인지 구별하지 않는다. 현재 판단에 필요한 자료인지도
최근 결과 2개라는 위치 기준 외에는 확인하지 않는다.

일반 read_file 결과인 문서의 details에만 중요한 DB 읽기 전용 조건을 두었더니,
문서 내부의 불완전한 context_summary 때문에 해당 조건이 모델 입력에서 사라졌다.
원본 저장 데이터는 남지만 이번 판단에 필요한 정보는 제외됐다.

반례: `test_untrusted_document_summary_cannot_remove_required_original_fact`.

### F6 — 작업 변경·취소가 현재 목표와 미완료 항목에 반영되지 않는다 (P1/P2, 6)

관련 코드: `task_context.py:93`, `context_selection.py:498`, `:506`, `:738`.

목표/최초 완료 조건은 생성 시 저장하고 이후 설계는 proposals에 추가한다. “그 방식 제외”도
candidate 관계만 남기며, revise 도구는 기존 요구·조건을 추가만 하고 대체/취소하지 못한다.
A 취소 후 B만 설명하라는 사용자 원문과 task_relation=change를 입력하고 B 답변을 끝내도
A 목표와 Deliver A 미완료가 남았다. 확정되지 않은 해석을 억지로 확정하면 안 되지만,
명확한 변경도 처리하지 않는 상태는 요구한 작업 상태 관리의 완성이 아니다.

반례: `test_user_cancellation_supersedes_old_goal_and_unfinished_item`.

### F7 — 최소 문맥과 누적 입력 관리가 불완전하다 (P2, 3·5·7)

관련 코드: `context_selection.py:264`, `:494`, `:513`, `:539`, `:801`, `context.py:140`.

- 모든 상태를 매번 coverage=partial로 저장하므로 정상 처리 이후에도 stale로 판단된다.
  이어가기에서는 모든 세션 사용자 원문을 작업 제약으로 다시 편입한다. 무관한 긴 예전 대화가
  현재 작업의 user_constraints로 들어가는 반례를 재현했다.
- 현재 작업/세션/관련 과거의 조회 범위를 스키마로 표현하지만 MemorySearcher 호출은 같은
  owner/workspace/agent 범위로 수행한다. 현재 작업 우선 검색 후 점진 확장과는 다르며,
  검색 후 정렬만으로 이미 후보에서 탈락한 현재 작업 자료를 복구할 수 없다.
- 첫 입력에서 임의의 필수 원문을 버리면 안 된다. 그러나 필수성 판정 대신 전체 사용자
  키워드 줄, 누적 proposals/decisions 등을 재전달하는 구조도 최소 문맥 구현으로 볼 수 없다.
- 일반 비구조화된 긴 도구 결과는 정리하지 않고 실제 모델 한도까지 예산만 확장한다.
  구조화된 요약 생산자는 현재 도구들에서 확인되지 않았으며, F5의 임의 문서 필드만 수용한다.
- 2천~4천/1만~1만6천은 합격 절대 기준이 아니다. 문제는 수치 초과 자체가 아니라 필요한
  정보와 무관한 이력을 구별하는 근거 및 유효한 요약/복구 계약이 부족하다는 것이다.

반례: `test_continue_does_not_import_all_unrelated_users_as_task_constraints`.

### F8 — 실행 중 취소된 요청의 누적 계측이 DB에 남지 않는다 (P2, 9)

관련 코드: `context_selection.py:435`, `:658`, `loop.py:1169`, `:2386`, `context_trace.py:68`.

최근 수정은 설계 종료/취소 시 계측을 저장하지만, 실행 중 취소 시에는 finish 저장 경로에
도달하지 않는다. 설계 후 실행 호출 1회를 취소하자 메모리 trace에는 2회, DB에는 설계 1회만
남았다. 저장된 설계 task_id도 null이었다. 전체 요청 비용/시도 횟수/시간을 신뢰할 수 없다.
측정값이 없는 경우 0토큰으로 사용하지 않아야 한다. 미커밋 수정에 usage_status 표시는
추가됐으나 누락된 호출을 저장하는 문제를 해결하지는 않는다.

반례: `test_cancelled_execution_persists_all_already_attempted_model_calls`.

## 원래 10개 항목별 대조

| 요구 항목 | 현재 확인 | 판정 |
|---|---|---|
| 1. 기존 구현/범위 | 기존 get_history 경로 유지, 주제 필터 없음. 500k 문맥에서 2,000개 상한/추정 입력 예산 471,808. focus는 신뢰 상태가 아님. tasks는 이제 새 경로에서 사용 | 기준선 차이 확인 |
| 2. 설계→검증→조회→실행→갱신 | 흐름 연결과 제한된 설계 호출은 존재. 새 지시/완료 경계 F2·F3 미흡 | 부분 |
| 3. 최소 입력/참조/상태 출처 | 원문·최근 대화·기능 이름/짧은 설명·missing/stale·후보 참조 존재. F4·F7 결함 | 미충족 |
| 4. 구조화 출력 | 필수 스키마 및 형식/의미 검사 분리, 제한된 형식 복구 존재. 클라이언트 검증과 기능 호출을 사용 | 부분 |
| 5. 코드 검증/자료 수집 | 등록 기능/구조화 출처 ID/제약 인용 검증, MemorySearcher·원문 읽기 재사용. F1·F4·F7 결함 | 미충족 |
| 6. 작업 상태 저장 | tasks+비파괴 sidecar 버전, CAS, 원문 참조 존재. 제안과 사용자 원문 구분. 유효 결정/취소 갱신 F6 미완성 | 미충족 |
| 7. 실행 입력/도구 루프 | 전체 이력 재부착 없이 선택 입력, 추가 기능 발견·요구 추가 가능. F2·F3·F5·F7 결함 | 미충족 |
| 8. 원본/복구/동시성 | 원본 보존, 범위 복원/명시 legacy 복구, 일부 재시작·중복·버전 충돌 검사 존재. F2 및 취소 뒤 상태/계측 경계 미흡 | 부분 |
| 9. 계측/검증 | 단계별 추정/실측, 선택 수·사유·누적 토큰 및 모의 테스트 존재. F8, 의미적 경계와 테스트 범위 부족 | 미충족 |
| 10. 전환/보고 | 기본 false, 명시 복구 옵션/비교 절차 문서 존재. 기존 보고의 완료 판단은 과도했으며 이번 감사로 정정 | 보완 필요 |

추가 확인: 기존 MemorySearcher의 FTS5·엔티티 검색·주변 문맥 복원·이벤트 제외·결과 예산과
원본 이벤트/호출 연결은 존재한다. 새 경로는 모델의 search_memory 호출 없이 기본 조회를
직접 수행한다. conversation_focus는 새 조회 정책의 보조 입력으로 사용하지 않는다.

## API/SDK 구조화 출력 확인의 범위

로컬 설치: openai 2.44.0, anthropic 0.116.0, pydantic 2.13.4.
현재 코드는 submit_context_design 함수 스키마와 Pydantic strict/extra=forbid 검증을 사용한다.
XAI 경로는 Responses 변환을 거치며, convert_tools는 함수명/설명/parameters만 전달한다.
서버 측 strict 구조화 출력 강제나 공급자별 지원 협상은 구현·검증된 것으로 볼 수 없다.
클라이언트 검증/복구가 있다는 것과 API가 스키마 준수를 보장한다는 것은 구별해야 한다.
이번 점검은 설치된 SDK·제공자 어댑터 코드만 확인했으며 실제 API 호출을 하지 않았다.

## 우선 수정 순서와 통과 기준

1. 사용자 제약·취소 경계를 모든 변경 도구 직전에 적용한다. 선택 기능과 별도 lightweight
   기능의 결합을 전제로 하지 않는다. source와 최신 사용자 이벤트 버전을 함께 확인한다.
2. 정보 요구의 candidate/acquired/validated/unavailable 상태를 구분한다. 실제 읽은 대상,
   버전·범위·결과와 판정 기준을 연결하고, 질문 원문/다른 파일을 근거로 해소하지 못하게 한다.
   자유 형식 acceptance의 의미를 코드가 완벽히 증명한다고 주장하지 않고 불확실성을 남긴다.
3. 일반·기록용 도구·반복 한도·fallback 등 모든 최종 종료를 같은 완료 검사로 통과시킨다.
   도구 성공은 해당 작업 결과의 근거로만 사용하며, 답변 전달과 실제 실행 검증을 구분한다.
4. 사용자 원문에 근거한 제약·변경·취소 투영을 구현하고, 후보 관계를 확정 결정처럼 사용하지
   않되 명확한 사용자 변경은 새 버전과 대체 관계로 반영한다. 불확실하면 원문을 복원한다.
5. 도구 요약은 신뢰된 생산자 계약과 원문 복구 참조가 있는 경우에만 사용한다. 필요한 원문과
   오류·시도 기록을 보존한다. 그 뒤에 중복과 무관한 이력의 선택 비용을 최적화한다.
6. 취소/실패/재시작 경계에서 요청별 계측과 작업 상태를 영속화한다. 측정 누락은 비용 0이 아니다.
7. 위 11개 감사 반례를 실제 통과시키고 원래 14개 시나리오와 회귀 검사를 함께 수행한다.
   이후 별도 승인된 제한 비교에서 설계+실행+복구 전체 토큰·시간과 지시 준수·완료율을 평가한다.

타임아웃/추론 강도 변경만으로 재시험 합격을 선언하거나 운영에 다시 켜서는 안 된다.
이번 감사 결과는 원래 구조의 미완성 지점을 확인한 결과이며, 해당 결함들의 수정 완료 보고가 아니다.
