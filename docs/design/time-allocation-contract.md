# 좁은 시간 배분 경로의 유효 요구·필수 검사·출력 계약

작성일: 2026-10-30. 상태: **설계 계약 확정, 구현·시험 실행·운영 적용 전**.
이번 산출물은 이 문서 한 개다. 아래의 함수 변경, 저장 형식, 인수 시험은 후속 구현의
규범이며 현재 제품에 구현됐다는 설명이나 시험 통과 보고가 아니다.

## 1. 근거와 결정

근거는 [구조 진단 보고서](../../../nanobot_skill_patches/handoff-review/system-diagnosis-0533ce1/REPORT.md)
§2~4, §7, §9와 그 보고서의 두 대조 반례다. 구조 진단을 다시 수행하거나 반례를 재실행하지 않았다.
코드 대응은 보고서가 분석한 검증 사본
`/Users/imkimhk/Project/nanobot_skill_patches/handoff-review/repo`,
HEAD `0533ce110777e4a4046747ba13e0bd498b8e2cac`의 함수·저장 구조를 읽어 확인했다.
현재 문서를 저장한 `nanobot_skill` 작업 폴더는 별도 운영 소스이며 그 후보와 동등하다고 보지 않는다.
원본 t68 트리 부재 등 기존 보고서의 증거 한계도 그대로 유지한다.

**권고안은 하나다. 작업별 유효 요구를 변경분으로 갱신하고, 그 스냅샷에서 필수 검사를
도출한 뒤, 검증한 구조화 결과로 수치 목록과 수치 설명을 생성한다.**
모델의 이월 인용은 요구의 존재 여부를 결정하지 않는다. 자유 답변의 숫자 재추출은 이
경로의 완료 판정에서 제거한다. 기존 검사를 모두 통과한 뒤 새 검사까지 붙이는 방식이 아니다.

범위는 한 작업 안의 **평면·순차 시간 배분 한 묶음**이다. 총 시간, 항목별 고정/조정 가능 시간,
제외 항목, 합계 일치/상한/잔여 배분, 같은 시간 배분, 최소 시간과 명시적인 변경·해제를 지원한다.
항목별 목적·방법 설명, 제외 조건 등 함께 요청된 필수 결과도 유지한다.
중첩 일정·동시 실행·복수 자원·시간대 일정은 자동 완료 지원 밖이다. 이런 요구를 발견하면
삭제하거나 평면화하지 않고 해당 요구를 미해결로 보존한다. 일반 답변·조회·외부 변경 경로는 보존한다.

## 2. 책임과 데이터 흐름

| 책임 | 담당 및 입력 | 산출물·경계 |
|---|---|---|
| 원문과 범위 보존 | 기존 `AgentLoop._persist_user_message_early`, `ConversationEventStore`, `MemoryScope` | 불변 사용자 event ID·sequence·원문. assistant 제안·검색 자료는 사용자 지시가 아님 |
| 작업 연결 | 기존 reference 검증과 질문 바인딩 | 검증된 task ID. 최근 작업이라는 이유만으로 조건을 합치지 않음 |
| 의미 제안 | 기존 설계/실행 모델의 동일 호출 | 출처를 가진 조건 변경분·결과 후보. 확정 요구/검사 통과를 쓸 권한은 없음 |
| 유효 요구 확정 | 좁은 모듈 `nanobot/agent/time_allocation.py`의 reducer(후속 신설), `ContextSelector.select`에서 호출 | 직전 스냅샷 + 해당 작업의 미반영 사용자 이벤트 + 검증된 변경분 → 새 스냅샷 또는 미해결. 원문 해석의 단일 경계 |
| 검사 도출·계산·출력 | 같은 좁은 모듈 | 스냅샷 → 필수 검사 → 결과 검증 → 결정적 출력. 범용 selector 안에 시간 정규식 추가 금지 |
| 권한·외부 증거 | 기존 `TaskControl`, `EvidenceToolRegistry`, needs/observations | 기존 권한·출처·실행 증거 책임 유지. 수치 통과로 조회/변경 의무를 대신하지 않음 |
| 완료·저장·보고 | `SelectedContext.verdict`, `ContextSelector.finish`, loop 저장/전달 경계 | 아래의 동일한 완료 묶음을 소비. 이 단계에서 요구/답변 숫자 재해석 금지 |

```text
사용자 이벤트 → 작업 연결 → 기존 스냅샷 + 변경분 → 유효 요구 R(task, version)
                                                        ↓
                                 미해결/충돌 ← 정합성 검사 + 필수 검사 집합
                                      ↓                 ↓
                              바인딩된 질문        결과 후보 A
                                      ↓                 ↓
                              사용자 선택 → R'    검사(R, A)
                                                        ↓
                                          같은 A로 목록·설명 렌더링
                                                        ↓
                                  버전 재확인 → 저장 → 같은 완료 묶음 보고
```

`direct`와 실행기 경로는 동일한 최종화 함수를 사용한다. `clarify`도 동일한 질문 저장 규칙을
사용한다. 모델의 route 표시는 제안이다. 요구 충돌·정보 부족이면 질문/미완료, 결과 오류면
결과 보정, 모든 의무 충족이면 완료라는 코드 결정이 우선한다. 별도 상시 모델 호출은 추가하지 않는다.

## 3. 저장 계약: 기존 장부에 한 개의 확정 투영

사용자 권위의 원천은 raw `USER_MESSAGE`다. 실행 단계가 읽는 확정본은
`TaskContextStore`의 기존 `task_context_versions.payload` 안에 추가할
**`effective_requirements` 한 개**다. 별도 DB/범용 요구 엔진/두 번째 작업 장부는 만들지 않는다.
원문 장부와 의미 투영을 분리하며, 모델 제안은 기존 `proposals`에 남긴다.

### 3.1 최소 데이터 의미

아래는 저장 계약이다. 기존 이름을 재사용하되 필요한 중첩 필드는 명시적으로 추가한다.
‘최상위 필드 하나’가 중첩 필드나 결과 스키마 변경까지 없다는 뜻은 아니다.

| 의미 | 저장 위치·형식 | 결정 |
|---|---|---|
| 작업·요구 버전 | 기존 `task_id`, `instruction_version` | 이 두 값이 요구 식별자. 저장 CAS `version`, 이벤트 `sequence`와 구별. 별도 requirement_version을 만들지 않음 |
| 확정 스냅샷 | `effective_requirements = {instruction_version, requirements, unresolved}` | payload의 task ID·scope/session 상속. requirements는 현재 유효한 행만 포함 |
| 요구 행 | `{id, kind, target, expected_values, sources}` | 기존 CompletionSpec의 target/expected_values 형태 재사용. sources는 아래 원문 참조 목록. id는 모델 check 문구와 독립인 안정 식별자. kind는 아래의 닫힌 종류 |
| 항목 식별·원문 | `kind=item`, `target=item_id`, `expected_values={label, mode, value}` | mode=`fixed/adjustable/excluded`. fixed에는 값 필수. adjustable의 value는 사용자가 명시한 시작/참고값이 있을 때만 보존하며 정확 일치 의무는 아님. 그 외 null. 모델 제안값은 이곳에 넣지 않음 |
| 총량 | `kind=total`, `expected_values={value, unit:"minute"}` | 총량 요구 ID는 변경 후에도 유지. 단위 변환 전 원문 근거 보존 |
| 관계 | `kind=relation`, `expected_values={kind, total_id, item_ids, value}` | kind=`composition/upper_bound/remainder/equal/min_each`. value는 min_each의 최소 시간, 그 외 null. total_id는 총량 의존 시 필수, equal/min_each는 null 가능. 관계 자체의 사용자 근거 필수 |
| 비수치 필수 결과 | `kind=answer_content`, `target=item_id 또는 answer`, `expected_values={text}` | 목적·방법·언어·필수 포함/제외 등. 기존 required_results/obligations와 연결. 수치 경로 편의를 위해 삭제 금지 |
| 미해결 | `effective_requirements.unresolved[] = {id, code, affects, sources, question_id}` | affects는 해당 요구 ID들. question_id는 질문 전 null 가능. 확정 행과 별개로 유지하며 필수 공백이면 완료 차단 |
| 변경 이력 | 기존 `supersessions[]`에 `requirement_id`, `operation`, `event_id`, `instruction_version`, 이전/새 값·근거 | operation=`replace/release`. 유지된 행은 복제 이력 없이 이전 버전과 같은 ID·근거로 보존 |
| 질문·선택 | 기존 `quantity_questions[]`, `decisions[]` 확장 | 질문 ID, affects, 선택별 변경분, 요청/응답 event ID, task ID, instruction_version, 해결한 사용자 event ID |
| 결과·검증·최종 출력 | 기존 `work_records[]` 안 `allocation` | 아래 §6의 완료 묶음. 기존 request/event/version 사용, 검증 요약은 completed/calculation_report가 참조 |

`requirements[].id`는 런타임이 task 안에서 발급하고 변경 후에도 유지한다. item_id는 최초
항목 요구 ID를 재사용한다. 표시 이름·배열 순서로 식별하지 않는다. 같은 이름의 항목이나 지시 대상이
모호하면 연결을 확정하지 않는다. sources는 `{event_id, source_span}`의 목록이며
source_span은 해당 이벤트의 정확한 발췌다. 평행한 ID/문자열 배열이나 원문 전체 복사로 대체하지 않는다.
최초 정의와 후속 유지/변경 근거를 혼동하지 않는다. 이벤트 전체를 이월하여 다른 절의 취소된 조건을
다시 활성화해서는 안 된다. 작업에 연결된 원문 전체는 누락 탐지용으로 읽되, 유효성은 요구 행으로 판단한다.

완료 가능한 스냅샷에는 총량 하나, 제외하지 않은 항목 하나 이상, 주 관계
composition/upper_bound/remainder 중 하나가 있어야 한다. remainder의 item_ids는 잔여를 받을
항목 집합이고 나머지 활성 항목은 선배분 값이 확정되어야 한다. remainder는 총량에서 선배분
합계를 뺀 값을 그 집합에 전부 배분하므로 최종 잔여는 0이다. 잔여를 남겨도 되는 요청은
upper_bound로 표현한다. equal/min_each는 추가 관계다.
`총 50분을 세 항목에 배분`은 그 원문을 근거로 composition으로 확정할 수 있지만, 단순 숫자
나열만으로 이 관계를 만들지는 않는다. 관계/필수 항목이 빠진 빈 검사 집합의 전칭 통과는 금지한다.

금액 등 다른 단위는 이 계약에서 취급하지 않는다. 시간은 내부적으로 정확한 유리수로 계산하고
저장 시 정규화한 문자열(`"20"`, `"12.5"`, 필요한 경우 `"20/3"`)을 사용한다.
float 허용오차로 고정값 위반을 통과시키지 않는다. 입력 분/초는 정확히 분으로 환산한다.
사용자가 반올림·정수 단위를 요구하면 그 요구도 행으로 보존하고, 동시에 충족할 수 없는 경우
조용히 반올림하지 않는다. 유한 소수는 소수로, 순환값은 정확한 분수로 출력한다.
반올림·정수 단위 요구는 최소 스키마에서 answer_content에 원문 그대로 보존한다. 이 계약의
자동 수치 검사에는 그 의미를 확정할 규칙이 없으므로 필수 요구이면 미해결도 함께 기록한다.
단위 변환과 출력 표기만으로 해당 요구를 충족했다고 간주하지 않는다.

### 3.2 최초 생성과 기존 상태 이행

최초 요청에서 원문의 항목·수치·관계·비수치 의무를 요구 행 또는 미해결 항목으로 전부 분류한다.
명시된 수치 절이 추출에서 빠졌으면 빈 집합을 유효 스냅샷으로 저장하지 않는다.
모델의 `quantity_relations`, `kept_conditions`, `completion_checks`가 비어 있어도 원문과
기존 스냅샷에서 요구를 확보한다. 좁은 문법으로 확정 가능한 항목은 코드가 복구하고,
원문은 있는데 해석이 지원되지 않으면 `interpretation_unsupported`를 남긴다.

레거시 작업은 해당 task에 검증되어 연결된 `user_constraints`·변경/질문 이력만 이벤트 순서대로
한 번 재생해 스냅샷을 만든다. 모델이 이번에 인용한 일부 원문으로 초기화하지 않는다.
출처·작업 연결·변경 의미가 충분하지 않으면 coverage=partial 및 미해결로 남긴다.
모든 세션 원문이나 다른 작업의 스냅샷을 합치지 않는다. 레거시 DONE은 새 계약의 완료 증거가 아니다.

## 4. 조건 생성·유지·대체·해제 규칙

### 4.1 변경분 수용

1. 먼저 scope/session/task 및 원문 event/span의 존재·가시성·사용자 권위를 검증한다.
   질문 응답이면 §4.3의 바인딩을 먼저 검사한다. 모델의 task ID 선택만으로 연결하지 않는다.
2. 직전 확정 행을 전부 복사하고, **검증된 명시적 변경분만** 적용한다. 모델이 이월 인용이나
   checks를 빠뜨린 것은 no-op이다. ‘같은 조건’, ‘항목 시간 유지’는 기존 행과 ID를 보존한다.
3. `총 50분으로 변경`은 total 행만 대체한다. `퍼팅은 25분으로`는 그 항목 fixed 값만 대체한다.
   다른 고정값·관계·제외·설명 요구는 유지한다. 후속 발화라는 이유로 모든 조건을 교체하지 않는다.
4. `퍼팅 고정을 풀어 배분해`는 fixed 조건을 해제하고 해당 item을 adjustable로 만든다.
   `퍼팅을 제외`는 item을 excluded로 바꾸고 합계/분배 관계의 활성 참여 집합에서 제거한다.
   연결된 equal/min_each 관계의 참여 집합도 함께 갱신하고 변경 근거를 기록한다.
   관계 자체가 더는 의미 없으면 그 관계를 release한다. 별도의 명시적 요구와 충돌하면 미해결로 둔다.
5. `전체 조건을 취소하고 새 계획`처럼 범위가 명시된 대체만 그 범위의 행을 해제한다.
   취소/대체된 옛 값은 이력에 남고 현재 검사 대상에서 빠진다. 나중에 재개해도 자동 부활시키지 않는다.
6. 모델의 제안·예시·지난 assistant의 배분은 사용자 fixed 요구가 아니다. 사용자가 그 계획을
   명시적으로 채택하면 인용한 응답 ID와 새 사용자 근거를 연결하고 채택 범위만 승격한다.
7. 새 사용자 요구 이벤트를 해당 작업에 수용하면 기존 `instruction_version`을 한 번 증가시키고
   이전 검증을 무효화한다. 중복 event ID 재처리는 증가시키지 않는다. 같은 이벤트의 미해결 해석을
   나중에 확정하여 유효 행이 달라져도 버전을 증가시킨다. CAS 저장·보고만으로는 증가시키지 않는다.
   불필요한 버전 재사용 최적화보다 보수적인 재검사를 선택한다.

### 4.2 불명확한 해석

**출처 일치만으로 의미 확정은 아니다.** source_span에 숫자가 있다고 그 숫자가 총량인지,
고정값인지, 상한인지 확정해서는 안 된다. 좁은 시간 문법과 문장 범위에서 역할·단위·대상·관계까지
입증되거나, 버전이 일치하는 질문 선택으로 확정되어야 행을 변경한다.

- `총 45분 이내`는 upper_bound, `합계 45분이 되도록`은 composition이다.
  `남은 시간을 A와 B에 똑같이`는 remainder와 equal을 생성한다. 단순한 총량 변경은 기존 관계를 유지한다.
- 모호한 `그건 줄여`는 기존 행을 삭제/변경하지 않고 affects 후보를 가진 미해결로 남긴다.
  필수 영향을 확정하지 못한 동안 DONE 불가다. 사용자에게 대상만 확인한다.
- 명백한 원문을 모델만 잘못 읽었으면 사용자 충돌로 보고하지 않는다. 코드의 좁은 해석으로
  복구하거나 기존 제한 내 모델 보정을 사용한다. 그래도 실패하면 `interpretation_unsupported`로
  미완료 보고한다. 사용자가 이미 준 수치를 무의미하게 재질문하지 않는다.
- 근거 없는 모델 후보는 `proposals`에 거부 사유를 남기고 후보에서 폐기한다. 불명확하다는 이유로
  기존 확정 요구를 삭제할 수 없다. 실제 사용자 요구 후보는 명시적인 해제/대체 또는 해당 요구가
  없었다는 원문 대조가 입증될 때만 미해결 목록에서 제거한다. 그 근거도 이력에 남긴다.
- 원문 자체가 부족하면 `user_info_missing`, 표현은 충분하나 기계가 못 읽으면
  `interpretation_unsupported`, 원문이 유실/가림 처리됐으면 `source_unavailable`로 구분한다.
  어느 경우도 not_applicable이나 검증 성공으로 변환하지 않는다.

### 4.3 충돌 질문과 사용자 선택

정상 모델 clarify와 코드 생성 질문 모두 `quantity_questions`에 동일하게 기록한다.
질문은 `(task_id, instruction_version, question_id, response_id, affects)` 및 선택별 **정확한
변경분**을 갖는다. proof는 확정 요구 ID와 계산 값에 연결한다. 원문 질문과 실제 전달 문구도 보존한다.

예: 총 45분·고정 20/15/15의 composition은 충돌이다. 질문에는 합 50분과 총 45분,
‘총량을 50분으로 변경하고 항목 유지’와 ‘항목 조정을 위한 고정 조건 변경’의 차이를 표시한다.
후자는 구체적인 조정 대상/방법이 없으면 자동으로 모든 고정을 풀지 않고 다음 미해결로 남긴다.

사용자가 첫 선택을 하면 total 행만 50으로 대체한다. 20/15/15는 같은 ID·원문 근거로 유지한다.
`50분으로` 같은 자유 응답도 유일한 유효 질문의 선택과 일치할 때만 같은 변경분으로 수용한다.
질문 이후 버전이 바뀌었거나 다른 작업의 질문이면 이전 선택 번호를 적용하지 않는다.
현재 요구에 맞게 질문을 다시 바인딩해야 한다. 여러 질문 중 대상이 모호하면 질문 식별을 요청한다.
선택 처리와 버전 증가를 하나의 CAS 갱신으로 저장하며 같은 선택 이벤트의 중복 적용을 금지한다.

## 5. 필수 검사 계약

필수 집합은 `checks = derive(effective_requirements)`다. 모델의 checks/specs는 보조 제안이며
필수 검사 생성·삭제 권한이 없다. 제안 검사는 같은 확정 요구에 연결해 보강할 수 있지만,
원문에 없는 모델 목표를 필수 사용자 의무로 추가할 수 없다.

검사 ID는 `(requirement_id, check_kind)`로 결정한다. 기존 `check_bindings`와
`completion_reports`에 task ID·instruction_version·requirement_ids·result_id·기대값·관측값·상태를
연결한다. 문자열 check 이름이나 ‘정답 값이 어딘가 등장’으로 통과시키지 않는다.

| 필수 검사 | 도출 근거 | 통과 조건 |
|---|---|---|
| 요구 완전성·출처 | 모든 활성 요구와 원문, unresolved | 필수 원문 누락/미분류 절 없음, 같은 task, 확정 의미, 필수 미해결 없음 |
| 요구 정합성 | total·fixed·관계·최소/단위 | 요구끼리 동시에 만족 가능. 합 50 고정과 composition 45는 conflict; adjustable을 임의 fixed로 계산하지 않음 |
| 결과 형태·항목 | item 요구 전체 | 활성 항목이 ID별 정확히 한 번 존재, 중복/미지 항목 없음, 제외 항목 배분 없음, 유효한 비음수 값·단위 |
| 고정값·총량 | fixed·total 행 | 결과 값이 요구와 정확히 일치. 합계가 같아도 항목 값 변경은 오류 |
| 합계·상한·잔여 | relation 행 | composition: 합=총량·잔여=0. upper_bound: 합≤총량·잔여=총량−합. remainder: 대상 합=총량−선배분 합, 최종 합=총량·잔여=0 |
| 균등·최소 | equal/min_each 행 | 지정 참여 집합의 값이 같고 각 최소 충족. fixed와 양립 불가하면 요구 충돌 |
| 필수 내용·표현 | answer_content 및 기존 obligations | 요청한 각 설명/제외/형식 요구의 산출물과 근거 존재, 필수 수치 설명은 결과 참조로 생성 |
| 최신성·외부 의무 | task/version/result 및 기존 needs/observations | 현재 버전의 동일 결과 검사. 조회·변경이 필요한 경우 그 기존 증거 검사도 충족 |

변경의 영향 범위는 요구 ID → 관련 관계 → 결과 → 출력 → DONE 순으로 추적한다.
최소 구현에서는 **시간 배분 검사 전체를 버전 변경 시 무효화하고 재계산**한다. 작고 결정적인
계산에서 부분 캐시를 유지할 이득보다 잘못된 재사용 위험이 크다. 다른 작업의 결과는 건드리지 않는다.
같은 버전에서도 결과 내용이 바뀌면 새 result_id를 발급하고 결과 의존 검사를 전부 다시 한다.
시간 조건과 무관한 외부 실행 사실은 보존하되 현재 요구에 대한 충족 판정은 별도 재연결해야 한다.

| 판정 | 의미 | 처리 경로·DONE |
|---|---|---|
| `requirement_conflict` | 확정된 사용자 요구끼리 양립 불가 | 관련 요구만 연결한 질문. 임의 조정 금지, DONE 불가 |
| `result_error` | 요구는 가능하나 결과 값/내용이 위반 | 사용자 요구 유지, 결과 후보 보정. 예산 소진이면 해당 오류와 미완료 보고 |
| `insufficient_info` | 필수 정보/출처/해석이 미확정 | 세부 code로 사용자 질문·원문 복구·해석 보정 구별. DONE 불가 |
| `not_applicable` | 실제로 해당 검사 대상이 없는 일반 작업/해제된 조건 | reason과 대상 ID 기록. 시간 요구 추출 실패나 모델 누락에 사용 금지 |
| `pass` | 해당 검사가 현재 버전·결과에서 충족 | 다른 필수 검사까지 통과해야 DONE |
| `environment_error` / stale | 완료 저장 전 저장·모델 전송·실행 장애 또는 버전 불일치 | 수치 오류/사용자 충돌로 보고하지 않음. 완료 확정 불가. committed 이후 채널 전달 실패는 §6.3의 전달 상태로 분리 |

일반 요청에 시간 배분이 없으면 이 경로만 not_applicable이며 기존 일반 완료 기준이 작동한다.
지원 밖의 시간 요구는 not_applicable로 숨기지 않고 명시적 미지원/미해결로 남긴다.

## 6. 결과·출력·완료 계약

### 6.1 구조화 결과와 가장 작은 생성 경로

결과 후보는 기존 설계 제출의 선택 필드 `allocation_result`로 받고, 실행기 보정도 같은 스키마를 쓴다.
이 필드가 없어도 요구에서 값이 유일하게 계산되는 경우(모든 항목 고정, 명시된 잔여 균등 배분)는
코드가 후보를 계산할 수 있다. 선택의 여지가 있는 adjustable 값은 모델이 제안한다.
**결과 후보로 확정 요구를 역으로 수정하지 않는다.** 자유 answer에서 결과를 추출하는 fallback은 두지 않는다.

```json
{
  "items": [
    {"item_id": "putting", "value": "20"},
    {"item_id": "approach", "value": "15"},
    {"item_id": "iron", "value": "15"}
  ],
  "total": "50",
  "remaining": "0"
}
```

예시 ID는 설명용이며 실제로는 §3에서 발급한 ID를 쓴다. total은 사용자 총 시간/한도이며,
배분 합계는 items에서 계산한다. remaining은 total−합계로 코드가 계산한다.
모델이 보내는 total/remaining도 검산하며 다르면 result_error다. 자동 생성 후보에는 코드가 채운다.
단위는 요구 스냅샷의 minute를 상속한다. 원본 후보와 수용 결과를 구분하여 조용한 보정은 하지 않는다.

실제 출력은 검증된 같은 객체에서 다음처럼 만든다. 항목 순서는 최초 사용자 순서를 보존한다.

```text
퍼팅: 20분
어프로치: 15분
아이언: 15분
총 시간: 50분 · 배분 합계: 50분 · 잔여: 0분
합계는 20+15+15=50분입니다.
```

마지막 식을 포함한 숫자는 renderer가 참조한 값으로만 생성한다. 이 문장 뒤에서 숫자를 다시
뽑아 총량을 판단하지 않는다. 직접 답변·실행기·최종 한도 정리·사용자 보고 모두 같은 출력을 쓴다.
미검증 모델 수치 답변을 streaming으로 먼저 내보내지 않고 수치 최종 블록은 검증 후 전달한다.

### 6.2 설명 보존과 충돌 방지

자유 문장은 목적·방법·주의점 등 **배분량을 새로 주장하지 않는 내용**에 한해 유지할 수 있다.
숫자만 금지하면 ‘퍼팅은 이십오 분’, ‘두 배’ 같은 충돌이 남으므로 배분량·합계·비율·시간 범위에
대한 모든 주장은 구조화된 참조/템플릿으로 표현한다. 예: item 값, 총량, 잔여, 균등 관계,
검증된 식을 가리키는 토큰을 renderer가 치환한다. 항목명은 요구의 label을 사용한다.

최소 구현은 배분 관련 설명을 검증된 템플릿으로 제한하고, 자유 설명은 항목별 정성적 방법으로
분리한다. 자유 설명의 수치/관계 주장은 참조로 옮기기 전까지 출력 수용하지 않는다. 단순 숫자
정규식 탐지를 의미 검증의 대용으로 사용하지 않는다. 경계가 불명확하거나 지원하지 않는 설명은
미검증 설명으로 식별하며 전체 요청을 DONE으로 올리지 않는다. 정성적 방법의 품질은 기존
의미 검증 의무로 남으며 수치 검사 통과가 그 품질을 보증하지 않는다.

사용자가 반복 횟수·거리·구간별 상세 일정 같은 추가 숫자를 필수로 요구하면 그 요구를
answer_content/미해결에 보존하고 별도 검증 가능한 산출물로 연결해야 한다. 지원하지 못하면
충족한 배분과 미완료 부분을 구별해 보고한다. 목록만 출력하고 필수 설명을 버리는 방식으로
DONE을 만들지 않는다. 다른 작업의 일반 자유문 생성 규칙은 변경하지 않는다.

### 6.3 동일한 완료 묶음

`work_records[].allocation`에 다음을 저장한다. task ID·request_id·event_id·instruction_version은
기존 부모 필드를 상속한다. 이 결합을 이하 ‘완료 묶음’이라 한다.

```text
allocation = {
  result_id,                      # 런타임 발급, 내용 바뀌면 재발급
  result,                         # 위 items/total/remaining, 불변
  checks,                         # 필수 집합 및 요구 ID별 기대/관측/판정
  output,                         # 실제 내보낼 최종 본문, renderer 버전 포함
  response_id,                    # 실제 assistant event 연결
  status                          # prepared / committed / delivered / delivery_unknown
}
```

요구 스냅샷은 기존 버전 테이블에서 동일 task·instruction_version으로 복원할 수 있어야 한다.
같은 instruction_version 아래 요구 내용이 다른 저장은 금지한다. `completed`와
`calculation_report`는 이 묶음의 result_id와 검사 요약을 참조한다. 독립 계산 결과를 따로 만들지 않는다.
수치 `verified`와 전체 완료, 저장 성공, 채널 전달 성공은 구별한다.

DONE 조건은 아래의 논리곱이다.

1. 현재 사용자 이벤트까지 해당 작업의 요구에 반영됐고, 이후 미조정 이벤트가 없다.
2. 유효 요구 스냅샷이 현재 instruction_version이며 필수 미해결·충돌이 없고 작업 전체가 취소되지 않았다.
   개별 조건의 명시적 해제는 갱신된 요구의 일부이며 그 자체로 완료를 막지 않는다.
3. 활성 요구에서 도출한 모든 필수 검사와 기존 외부 의무 검사가 같은 버전·결과에서 pass다.
4. 필수 비수치 결과도 충족됐고 최종 본문이 같은 결과에서 생성되어 저장됐다.
5. 최신 버전 재확인과 CAS 저장이 성공했다. route=direct나 답변 존재만으로 우회할 수 없다.

저장은 기존 저장소를 유지하는 다음 순서를 권고한다. prepared 묶음 저장 → 같은 output의
assistant event 저장 → 해당 response_id를 연결한 CAS로 committed/DONE 저장 → 사용자 전달.
각 단계는 request_id/result_id로 멱등 처리한다. 하나의 거대한 신규 트랜잭션 계층은 만들지 않는다.
중간 장애에서는 prepared나 미완료를 유지하고 복구 시 동일 event/묶음만 연결한다.
CAS 실패를 로그만 남기고 완료 응답으로 보내지 않는다. 최신 요구를 다시 읽고 미완료로 보고한다.
사용자용 history/API도 committed 전 본문을 ‘완료’로 표시하지 않도록 이 경계에 참여해야 한다.

DONE은 요청 결과의 검증·저장 완료다. 채널 전달은 delivered/delivery_unknown으로 별도 기록하고,
전달 실패를 사용자에게 전달 성공이라고 주장하지 않는다. 재전송은 저장된 같은 본문을 쓰며
외부 채널에서 exactly-once 전달까지 보장한다고 주장하지 않는다.
allocation.status의 delivered/delivery_unknown은 committed 이후에만 가능하며 검증·저장 완료를
계승한다. 현재 버전이 그대로라면 전달 실패만으로 검증된 DONE을 취소하거나 재계산하지 않는다.
게시 직전 새 이벤트를 확인하고 게시 시작을 직렬화한다. 이후 도착한 새 요청은 새 버전의 작업이다.

설계 실패/시간 초과가 요구 반영보다 먼저 발생해도 현재 request_id/event_id는 미완료로 남긴다.
연결이 확정된 변경 요청은 즉시 RECONCILE로 표시한다. 작업 연결이 불확실하면 어느 과거 작업도
임의 변경하지 않되 현재 요청의 상태는 미해결이다. 이전 DONE은 역사적 사실로 보존하고
**현재 요청의 완료 근거로 사용하지 않는다.** 상태 조회·사용자 보고는 task.status 하나가 아니라
현재 request/event/version과 committed 묶음의 일치를 요구한다.

## 7. 기존 코드 대응표와 대체 책임

모든 함수는 §1의 검증 후보 기준이다. 아래 변경은 향후 좁은 분기 안에서만 수행한다.

| 분류 | 기존 함수·구조 | 계약상의 처리 |
|---|---|---|
| 그대로 유지 | `_persist_user_message_early`, RawEvent, MemoryScope, raw reader | 원문 권위·scope·event ID·순서·가림 경계 유지 |
| 그대로 유지 | `TaskContextStore`의 버전 테이블·CAS·원문 `user_constraints`, `proposals` | 테이블과 CAS 알고리즘, 원문/모델 제안 분리 재사용. payload 확장과 호출 시점 변경은 공통 기준 적용 항목에 해당 |
| 그대로 유지 | `select_design_output`, `parse_design_arguments`, `split_partial_relation` | 공식 함수 인자 하나만 채택. 별도 텍스트 JSON 병합 금지. 부분 모델 관계는 후보로 보존 |
| 그대로 유지 | `TaskControl.authorize/run_mutation`, `EvidenceToolRegistry`, `context_evidence`의 외부 증거 | 권한·출처·변경 전 조회·실행 사실 검사 유지 |
| 공통 기준 사용으로 변경 | `minimal_input`, `build_prior_context`, `restore_design_sources`, `ContextSelector.select` | 해당 task 스냅샷·미반영 이벤트·필요 원문 제공. 모델 인용이 아닌 reducer가 확정 요구를 저장 |
| 공통 기준 사용으로 변경 | `verify_carried_conditions`, `effective_relations`, `carried_status`, `superseded_value` | 출처 검증은 수용 경계에서 재사용. 새 경로의 유효 조건/관계 조회는 스냅샷 조회로 대체 |
| 공통 기준 사용으로 변경 | `required_results`, `user_requirements`, `context_plan.requirement_ids/requirement_status` | 활성 요구 ID로 매핑. kind:target만으로 여러 시간 조건을 합치지 않음 |
| 공통 기준 사용으로 변경 | `relation_checks`, `spec_value_violations`, `calc_spec_missing`, `supplement_calculation`, `supplement_requirements` | 시간 조건 판정은 단일 derived checks 결과를 소비. 모델 보충도 같은 원문/변경분 수용 경계 통과 |
| 공통 기준 사용으로 변경 | `RecordContextCompletionTool`, `check_bindings`, `check_stale`, `note_instruction_read` | 모델 자기 보고로 필수 계산 통과 불가. 요구 ID·버전·result_id에 묶인 코드 판정 사용 |
| 공통 기준 사용으로 변경 | `recover_quantity_conflict`, `quantity_question_errors`, `relation_question`, `settle_unresolved` | 모델/코드 질문 모두 동일한 question binding과 선택 변경분 사용 |
| 공통 기준 사용으로 변경 | `TaskControl.ingest_event/refresh`, `ReviseContextRequirementsTool` | task 연결 뒤 reducer로 갱신. 모델 revision만으로 fixed/요구 해제 불가. 다른 task 이벤트 혼입 차단 |
| 공통 기준 사용으로 변경 | `SelectedContext.verdict/completion_supported`, `ContextSelector.finish`, `work_record`, `calculation_report`, `restore_task_work` | 하나의 완료 묶음 생성/소비. finish의 direct_done/answer_done 별도 DONE 승격은 좁은 경로에서 대체 |
| 공통 기준 사용으로 변경 | `AgentLoop._state_run/_state_save/_save_turn`, runner의 최종화/`context_plan.fix_route` | route 공통 수치 최종화, 같은 후보 스키마로 보정, CAS·버전 확인 뒤 저장/보고. 예산·재시도 상한은 유지 |
| 새 경로에서는 사용하지 않음 | `user_requirement_texts()`의 current + `_carried`만으로 수치 원문 집합 구성 | 스냅샷의 활성 요구가 대체. 모델 이월 인용 완전성 의존 제거 |
| 새 경로에서는 사용하지 않음 | `merge_current_groups`, `requirement_text_groups`, `_recover_partial_relations`, `_structured_group`를 매 단계 다시 실행 | 수용 경계에서만 좁은 해석 재사용 가능. 수용 후에는 다시 요구를 만들거나 merge하지 않음 |
| 새 경로에서는 사용하지 않음 | `direct_relation_errors`의 별도 현재 요청 수치 판정 | direct/실행기 공통 요구·결과 검사로 대체. 일반 의미/권한 검사는 유지 |
| 새 경로에서는 사용하지 않음 | `_labelled_values`, `_allocated_numbers`, `computed_mismatch`, `relation_answer_finding`, `relation_answer_findings/errors/unverified` | 수치 자유문 재추출 책임 제거. 구조화 result 검사 + 결정적 renderer가 대체 |
| 새 경로에서는 사용하지 않음 | `answer_check_failures` 중 시간 숫자 재파싱, checks 문자열만으로 completion floor 결정 | 수치 필수 집합은 active 요구에서 도출. 비수치 내용/외부 의무 검사는 유지 |
| 후속 과제 | `reconcile_verdicts`의 과거 wrapper 재판정 | 과거 답변을 새 계약 DONE으로 소급 승격하지 않음. 별도 이행 검토 |
| 후속 과제 | 중첩/병렬 일정, 범용 설명 의미 검증 확대, 다른 단위·다른 작업 유형 | 이번 좁은 경로를 이유로 일반 답변·조회·변경 경로 개편 금지 |

이전 수치 검사와 새 수치 검사는 한 요청에서 동시에 권위를 갖지 않는다. 좁은 경로 진입 시
위 ‘사용하지 않음’ 수치 분기를 건너뛰고 공통 결과를 반환한다. 일반 경로는 기존 분기를 유지한다.
구현 검토에서는 새 검사 추가 수보다 **없어진 중복 판정 호출과 단일 소비 지점**을 확인한다.

## 8. 독립 인수 기준

이 절은 실행할 시험의 명세이며 이번에 시험 코드 작성·pytest·재생·모델 호출은 수행하지 않는다.
기대값은 사용자 이벤트 시퀀스에서 사람이 사전에 작성하고 제품 함수나 product findings에서
만들지 않는다. 각 fixture는 scope/task 연결, 버전 변화, 활성/해제 요구 ID, 허용 결과, 질문과
선택의 효과, 기대 완료 상태를 고정한다. 런타임 ID는 최초 생성 때 fixture 기호에 바인딩한다.

### 8.1 필수 대조와 다중 턴 사례

| ID | 입력·교란 | 독립 기대값과 합격 조건 |
|---|---|---|
| A01 필수 정상 | 총 45, 고정 20/15/15 충돌 질문 → ‘총 50분, 항목 유지’. 정상 후보 20/15/15, 설명 `20+15+15=50분` | 같은 task, 버전 증가, total=50·fixed 보존·잔여=0. 실제 목록/식·저장 result 일치, 현재 DONE. 오차단이면 실패 |
| A02 필수 누락 | A01 후속에서 모델의 kept 인용·quantity_relations·completion_checks 전부 생략, 후보 25/10/15 | 기존 fixed 20/15/15가 그대로 검사 대상. 합=50이어도 result_error, 잘못된 목록/완료 보고 차단. not_applicable/DONE이면 실패 |
| A03 정상 누락 | A02와 동일한 제안 누락, 후보만 20/15/15 | 스냅샷에서 검사 생성, 정상 완료. 모델 인용 누락 자체를 이유로 거부하면 실패 |
| A04 명시 변경 | A01에서 ‘퍼팅 25, 어프로치 10으로 바꾸고 아이언 유지’, 총 50 | 변경된 두 fixed만 25/10, 아이언 15 보존. 옛 20/15 강제 금지. 새 버전 DONE |
| A05 유지·조정 | 총 60, 퍼팅 fixed 20, 드라이버 제외, 남은 시간을 어프로치/아이언 균등 → 총 45만 변경 | 20/12.5/12.5, 잔여0·제외 유지. 모델이 20/20/20 제안하면 이전값 오류 |
| A06 해제 | total 50, fixed 20/15/15에서 ‘퍼팅과 어프로치 고정만 풀어’ | 두 item만 adjustable, 아이언 fixed15. 합50인 25/10/15 허용. 옛 fixed 검사 없음, release 이력 있음 |
| A07 작업 분리 | 동일 세션 B에 총90·퍼팅30 생성 후 A의 응답을 지정해 재개 | A/B 각각 원래 ID·요구 보존. B의30을 A에 반영하거나 A 조건을 B에 강제하면 실패 |
| A08 선택 격리 | 총45 충돌 질문에서 total50 선택; 이후 버전 변경 뒤 옛 질문 선택 번호 재입력 | 최초 선택은 total만 변경. 오래된/다른 task 질문의 선택은 적용하지 않음. unrelated 요구 불변 |
| A09 버전·저장 | DONE v1 뒤 v2 변경, 모델 설계 실패/최종화 중 새 이벤트/CAS 충돌/저장 중단 | v1은 이력만 유지. 현재 요청 DONE·완료 보고 금지. committed 전 실패 복구에서 중복 결과/질문 없음 |
| A10 출력 일치 | 올바른 표 뒤 ‘최종 퍼팅25·어프로치10’ 설명 또는 정답·오답을 같은 줄에 함께 제안 | 두 값 중 첫 정답만 골라 pass 금지. 원시 수치 설명은 수용하지 않고 검증된 참조로 교정; 모순이 전달되면 실패 |
| A11 관계·정보 분리 | 합50 fixed와 composition45 / upper_bound60 / 총량 누락 / 모델 partial | 각각 conflict / 정상잔여10 / user_info_missing / 복구 또는 interpretation_unsupported. 네 경우를 하나의 error/NA로 합치면 실패 |
| A12 설명·제외 | ‘각 항목 목적·방법도 설명, 드라이버 제외’ 후 정상 수치만 제안 | 수치만으로 전체 DONE 불가. 필수 설명 충족 및 제외 조건 유지 후 완료 |
| A13 표현·단위 | 20/15/15의 목록·표·괄호식·등식, 분/초 동치, 12.5분=12분30초 | 동일 의미의 구조화 값과 renderer 출력은 같은 통과. 식 위치/공백/문구만으로 거부하면 실패 |
| A14 일반 경로 | 시간 배분 없는 설명·조회·파일 변경의 기존 fixture | 기존 권한·증거·완료 경로 유지. 새 시간 계약 의무나 질문 발생 금지 |
| A15 미지원·미해석 | 중첩/병렬 필수 요구, 모호한 대상, 찾을 수 없는 과거 원문 | 필수 요구 삭제·평면화·NA 완료 금지. 정확한 미해결 유형과 다음 필요한 정보 보고 |
| A16 잔여 배분 | 총60, 퍼팅 fixed20, 나머지는 어프로치/아이언에 균등 배분. 후보20/15/15 및20/20/20 | 전자는 잔여10이므로 result_error, 후자는 잔여0으로 완료. 같은20/15/15도 총60 이내라는 upper_bound 요청에서는 허용 |
| A17 전달 장애 | 정상 결과를 committed/DONE 저장한 뒤 채널 전송 실패, 같은 요청 재전송 | delivery_unknown이며 전달 성공 보고 금지. 현재 버전이 같으면 DONE 근거와 result_id/output 유지. 새 요구가 들어오면 이전 결과 재전송을 현재 요청의 완료로 표시하지 않음 |

각 사례는 `직접 답변 / 실행기 결과 / 한도 종료` 중 가능한 경로에 적용한다.
질문 사례는 `정상 모델 clarify / 코드 생성 질문` 양쪽과 저장 후 재개를 포함한다.
A01·A02는 다른 시험 성공 수로 대체할 수 없는 필수 인수 기준이다.
정상 요청 오차단과 잘못된 고정값 통과는 모두 실패이며 서로 상쇄하지 않는다.

### 8.2 관측·채점 절차

1. fixture의 원문·기대 활성 요구/상태를 실행 전에 고정한다. 모델 출력은 raw 공식 함수 인자로
   제공하며 필드 생략·partial·중복 별도 텍스트를 포함한다. harness 기본값으로 빈 필드를 채워
   실제 출력의 누락을 숨기지 않는다. 이전 실제 기록의 의미·수치는 보존한다.
2. **실제 사용자에게 전달된 본문**을 캡처한다. 제품 내부 answer나 renderer 입력만 검사하지 않는다.
   표/목록의 최종 항목 값·총량·잔여·제외·요청한 설명을 독립 기대값과 비교한다.
   반복/상충하는 값, 완료 wrapper, 질문 내용도 평가한다. 모순 중 첫 값만 채택하지 않는다.
3. 저장소를 다시 읽어 활성 요구·원문 근거·supersessions·질문/선택·result·checks·response_id를
   각각 fixture와 비교한다. DB와 출력이 함께 같은 오답인 경우에도 실패해야 한다.
4. 출력 관측에는 별도로 작성한 제한된 표/목록 reader와 고정 golden 출력을 쓴다.
   제품 parser/renderer/derive_checks를 oracle에 import하지 않는다. 자유 설명 의미는 사전
   기대 명제와 대조하고 자동 판독 불가 시 `unverified`/수동 검토로 남긴다. 통과로 세지 않는다.
5. `user_success`, `requirement_projection_correct`, `result_correct`, `decision_correct`,
   `storage_output_consistent`를 별도 기록한다. 제품 verified는 비교할 관측값일 뿐 정답이 아니다.
   정상 fixture의 거부는 decision_correct=false, 잘못된 후보의 정당한 차단만 true다.
6. 내용 오류, 정상 오차단, 미실행, oracle 미판독, 모델/환경 오류를 분리해 집계한다.
   통과한 pytest 개수·skip·내부 pass만으로 운영 승인이나 의미 정확도를 주장하지 않는다.

### 8.3 기존 채점 공백 수정 범위

| 기존 위치·공백(진단 §7) | 후속 수정 기준 |
|---|---|
| `tests/agent/test_followup_sequences.assert_values`: product relation/findings 재사용 | 상태 전달 회귀 가치는 보존하되 인수 oracle에서 제외. fixture → 실제 출력과 저장 요구/result 직접 대조 추가 |
| `scripts/followup_eval.grade`: user 실패 + product non-pass이면 internal_correct | 사전 기대의 정상/충돌/오류 분류와 판정을 대조. 정상 답변 오차단은 내부 정답 처리 금지 |
| `scripts/independent_eval.golf`: 같은 줄의 첫 정답값 선택 가능 | 최종 출력 전체의 상충 주장 검출, 다중 값은 실패/미판독. 정답 목록+오답 최종 주장 fixture 고정 |
| `followup_allowed`: 내부 conflict 라벨에 의존했던 후속 차단 | 실제 질문·선택지·task/version 바인딩으로 다음 턴 가능성을 판단. 판정 정확성은 별도 채점 |
| `t51_harness.design()`의 완전한 기본 형식 편향 | raw submit fixture와 기존 실제 provider 응답 기록을 보존해 누락/partial 대조. 완전한 mocks만으로 승인 금지 |
| 누락 t56 원본 등 skip | 원본 재생 미검증 표시 유지. 새 synthetic 대조와 구분. 누락 기록을 복원했다고 주장하지 않음 |

과거 응답을 새 structured schema로 변환하는 replay는 의미 보존 어댑터 시험으로 표시한다.
새 모델이 해당 스키마를 준수했다는 증거가 아니다. 기존 실제 응답의 정상 식은 renderer의
golden과 의미 보존 대조에 반드시 남긴다. 추가 실모델 전송은 별도 승인 범위다.

## 9. 최소 구현 범위와 되돌릴 수 있는 적용 경계

후속 구현은 다음 한 묶음으로 제한한다. 지금 실행하는 단계 목록이 아니다.

1. 좁은 시간 모듈에 스냅샷 reducer, 필수 검사 도출, 정확 계산, 결과 검증, renderer를 둔다.
   selector/runner에는 경계 호출만 둔다. 기존 원문·버전·질문·작업 기록을 확장한다.
2. direct/실행기 공통 최종화 및 §7의 중복 수치 판정 교체, 변경 뒤 무효화와 질문 선택 바인딩을
   함께 구현한다. 저장·보고 정합성이 빠진 ‘검사만 추가’ 패치는 이 계약의 구현으로 인정하지 않는다.
3. 기존 시험 채점 수정과 §8의 독립 인수 시험을 구현한다. 오프라인 실행은 별도 승인 후에만 한다.
   수치 의미를 단순화하거나 A01/A02 기대값을 약화해 통과시키지 않는다.

적용 시 기존 Config 스키마에 명시적인 `contextSelection.timeAllocationContractEnabled`
(기본 false) 한 개를 추가하는 방안을 권고한다. 이번에는 추가/설정하지 않는다.
기능 진입은 모델 checks 유무가 아니라 **기존 시간 스냅샷 또는 원문의 시간 배분 요청**으로
결정한다. 미지원 시간 요구는 지원 밖 미해결로 처리한다. 검사 실패 후 레거시 수치 완료 경로로
fallback하여 DONE이 되는 우회는 금지한다.

향후 적용은 검증 사본과 격리 DB부터 시작하며 후보 기준 commit과 스키마 계약을 고정한다.
운영 소스와 후보의 차이·누락 blob 문제를 해소하기 전 후보를 그대로 운영 패치라고 부르지 않는다.
기존 일반 경로 보존과 A01/A02를 포함한 인수 통과가 코드 적용 검토의 전제다.
실제 호출·배포는 각각 별도 승인 사항이다.

되돌리기는 플래그를 끄고 새 요청의 좁은 경로 진입을 중단하는 방식이다. 원문·기존 payload·
새 스냅샷·결과 이력은 삭제하지 않는다. 진행 중 작업은 경계에서 멈춰 미완료로 남긴다.
플래그를 끈 뒤 새 계약의 prepared/이전 DONE을 레거시 현재 요청의 완료 근거로 승격하지 않는다.
재활성화하면 최신 원문·버전부터 재조정한다. 검증 전 이중 판정을 운영에서 병행하는 shadow
검사층은 최소 구현 범위에 넣지 않는다.

## 10. 남은 결정 사항과 권고

이번 계약에서 책임 분리, 저장 위치, ID·버전, 유지/대체/해제, 관계 의미, 질문 선택, 필수 검사,
정확 수치 표현, 결과 출력, 완료 묶음 및 독립 인수 기준은 위 권고안으로 결정했다.
모델 변경·시간 제한 연장·범용 라우터 재설계는 해결책에 포함하지 않는다.

남는 결정은 적용 준비 사항이다.

- **구현 기준 트리:** 검증 후보 `0533ce1`을 설계 대응 기준으로 삼되, 운영 패치 기준은 후보와
  운영 소스의 차이를 확인한 뒤 별도로 고정한다. 원본 t68과 동등하다고 가정하지 않는 것을 권고한다.
- **지원 밖 설명/일정 확대:** 우선 이 계약의 평면 배분과 정성 설명 범위를 유지한다.
  임의 수치 서술·중첩 일정을 자동 검증할 수 있을 때 별도 계약으로 확대한다. 필수 요구 누락은 불허한다.
- **실제 호출·운영 시점:** 독립 인수 결과를 검토한 뒤 결정한다. 현재 문서만으로 적용 승인이나
  모델의 새 스키마 준수를 추정하지 않는다.

주요 절충은 수치 표현 자유도를 일부 줄이고 버전 변경마다 작은 계산을 다시 하는 대신,
사용자 고정 조건의 누락과 정상 등식의 오차단을 동일한 데이터 계약으로 막는 것이다.
일반 답변의 자유도·조회·변경 권한 경계는 이 절충의 대상이 아니다.

**이 문서 작성으로 이번 작업을 종료한다. 후속 구현·시험 실행·모델 전송·운영 적용은
승인 전까지 실행하지 않는다.**
