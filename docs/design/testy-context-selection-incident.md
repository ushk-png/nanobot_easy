# TESTY 컨텍스트 선택 시험 점검 — 2026-10-26

## 운영 관측

TESTY에만 `contextSelection.enabled=true`, `failureMode=block`을 적용했다.
메인은 기존 경로를 유지했다. 사용자 비교 시험 첫 세트의 두 요청에서 지연과
미완료가 보고됐다. 원본 대화, DB 및 실행 체크포인트는 보존했다.

- 첫 요청: 17:28:33 수신, 17:31:05 최종 응답. 요청 계측 151,921ms.
- 제공자 보고 총 토큰 합계 134,939. 설계 2회, 실행 4회. 캐시 사용을 포함한
  호출별 입출력 합계이며, 청구 금액이나 기존 경로 대비 절감률을 뜻하지 않는다.
- 설계 두 호출은 각각 약 44초, 82초. 첫 설계는 `unsafe_direct`로 거부됐다.
- 첫 입력 추정 15,688토큰. 규칙 약 6,566, 기능 목록 약 5,673토큰 등으로
  목표 최소 입력을 넘었다. 필수 규칙을 삭제해 목표 수치를 맞추지는 않았다.
- 두 번째 요청: 17:33:34 수신. 반복 검색과 원문 조회 후 체크포인트의
  `resolve_context_need` 3건이 잘못된 호출 ID 때문에 실패했다. 이전 요청의 ID를
  현재 요청의 조회 근거로 제출했다. 저장된 최종 답변은 없었다.
- 둘째 요청 전체 토큰/시간 계측은 중단 전에 저장되지 않아 확정할 수 없다.
  첫 요청의 계측을 둘째 요청 수치로 재사용하지 않는다.

17:37경 선택 기능을 false로 되돌리고 TESTY만 재시작했다. 상태 확인 200,
메인 PID 유지. 실패한 요청을 자동 재전송하지 않았다. 기존 원문, 진행 중 요청
표시와 체크포인트를 삭제하지 않았다.

## 확인한 결함과 코드 수정

1. 직접 답변 검증이 비교 요청과 Q 번호 자체를 거부했다. 비교를 허용하고,
   Q 참조는 실제 질문/답변 원문 후보가 있는지 확인한다. 원문이 없는 참조,
   실제 작업 요청, 필수 자료 누락은 계속 직접 답변으로 통과시키지 않는다.
2. 설계 복구 입력에 거부된 제안 자체가 없었다. 제한된 재시도에 이전 제안과
   검증 코드를 제공하고, 오류 회피용 가짜 검색 요구를 추가하지 않도록 했다.
3. 이미 알려진 현재 작업/세션 원문 ID도 검색을 반복했다. ID로 원문을 모두
   확보하면 중복 검색을 생략한다. 관련 과거 기록 검색은 ID가 단서일 수 있으므로
   검색을 유지한다. 필수 자료 실패 게이트는 유지한다.
4. 설명 답변도 실제 도구 호출 기반 완료 기록을 강제했다. 대화 원문에 기반한
   설명/비교는 답변 전달과 실제 작업 증명을 구별한다. 파일·조사·실행 요청,
   누락 근거 및 실제 변경은 기존 증명 경계를 유지한다. 이전 작업의 미완료 항목을
   설명 답변 하나로 모두 완료 처리하지 않는다. 답변 전달은 의미적 정확성 검증으로
   계측하지 않는다.
5. 실행 모델이 실제 runtime 호출 ID를 추측했다. 제공자에 보내는 복사 입력에
   관측된 ID·도구 이름·성공 여부를 명시하고, 잘못된 ID 오류에도 실제 사용 가능한
   조회 ID를 반환한다. 원본 도구 호출·결과 내용과 연결은 변경하지 않는다.
6. 예전 내부 교정문이 사용자 원문/결정으로 승격됐다. hidden 표시와 정확히 일치하는
   알려진 레거시 내부 교정문을 선택 입력/작업 상태 해석에서 제외한다. 원본은 보존한다.
7. 새 작업 초기 상태가 무관한 세션 사용자 메시지를 모두 작업 제약으로 복사했다.
   새 작업 장부는 현재 원문으로 시작한다. 최근 문맥·과거 명시적 제약 후보의 입력
   보존 경로와 이어가기/재개 시 원문 확장은 유지한다.
8. 설계 계측이 실행 종료 때만 저장됐다. 설계 단계 종료/취소 때도 현재까지의 계측을
   저장해, 후속 실행 중단이 이미 사용한 설계 토큰을 지우지 않도록 했다.
9. 기능 이름은 유지하며 설계용 짧은 설명을 220자에서 120자로 줄였다.
   공통 실행 규칙·권한 제한은 유지했다. 실제 최소 입력이 2천~4천 토큰에 들어온다는
   보장은 없으며 이 부분은 추가 관측/개선이 필요하다.
10. 직접 답변의 명백한 본문 예고만 있는 응답도 기존 판정기로 제한된 형식 복구를
    수행한다. 길이만으로 단문을 거부하지 않는다. 상세 답변 요구를 설계 프롬프트에
    명시했지만 실제 모델의 상세성 개선은 미검증이다.

## 로컬 검증과 남은 한계

- 비식별 Q1~Q3 비교 및 Q2,, Q3 후속 요청을 전체 AgentLoop의 모의 모델로 재현.
  각각 설계 1회로 완료하며 원문과 초기 조건을 유지하고 전체 이력 재부착은 금지했다.
- 추가로 원문 없는 참조 거부, 필수 근거 실패, 가짜 실행 완료 거부, 주제 전환,
  미완료 작업 보존, 정확한 ID 노출/원본 도구 연결 보존, 내부 교정문 비승격,
  설계 계측 조기 저장 등을 검증했다.
- 관련 Python 회귀 테스트 515개 통과. 변경 Python 파일 Ruff 및 diff 공백 검사 통과.
- 실제 유료 모델 비교·질문 재전송은 수행하지 않았다. 속도·답변 품질·완료율·토큰
  절감률 개선을 이 모의 검증으로 입증하지 않는다. 제공자가 변경된 입력에 어떻게
  반응하는지와 큰 규칙/기능 목록 비용은 재활성화 전 확인해야 한다.

위 점검 종료 시점에는 TESTY와 메인의 선택 기능이 모두 꺼져 있었다. 다음 시험은 비식별 두 요청만
제한적으로 실행하고 설계+실행+재시도 총 토큰, 응답 시간, 실제 내용과 완료 여부를
함께 비교해야 한다. 기능을 다시 켜기 전 별도 운영 판단이 필요하다.


## 사용자 요청에 따른 재시험 적용 — 2026-10-26 17:51 KST

사용자의 명시적 재적용·재시작 요청으로 TESTY만 다시 활성화했다.
`enabled=true`, `failureMode=block`; 기존 설정을 로컬에 백업한 후 TESTY PID
78520 → 78935로 교체했다. 상태 확인 200, 에이전트 루프 시작, 텔레그램 연결,
해당 기동 구간 ERROR/Traceback 0건을 확인했다. 메인은 선택 기능 false 및
기존 PID를 유지했다. 적용 직전 컨텍스트 선택 회귀 48개와 변경 파일 Ruff 검사 통과.

비교 질문을 자동 전송하거나 유료 모델 재시험을 실행하지 않았다. 사용자가 대화로
재시험할 수 있는 상태이며, 실제 속도·품질 개선은 여전히 미검증이다.

## 감사 결함 수정 후 사용자 승인 적용 — 2026-10-26 19:18 KST

사용자의 “테스티에 적용해서 재시작” 요청으로 TESTY만 선택 기능을 활성화했다.
`enabled=true`, `failureMode=block`, 설계 시간 기본 20초/추론 low를 설정 로더로 확인했다.
설정은 `.local/config.testy.json.before-audit-repair-enable-20261026-191715.bak`에 백업했다.
감사 수정의 로컬 검증 결과는 `context-selection-repair.md`에 기록돼 있다.

기존 TESTY PID 79033을 종료했다. 기존 시작 스크립트로 생성한 PID 80826은
기동 후 유지되지 않아, 동일 환경 파일/가상환경/설정으로 독립 세션에서 다시 시작했다.
최종 PID 80850의 생존, `/health` HTTP 200, 에이전트 루프 시작, 텔레그램 연결을 재확인했다.
해당 최종 기동 구간 ERROR/Traceback은 없었다. MCP 연결 취소 경고가 있어 다음 메시지에서
재연결하도록 대기 중이며, MCP 실제 사용은 아직 확인하지 않았다.

메인은 실제 PID 49369 유지, 설정 해시 동일, 선택 기능 false를 확인했다.
메인 PID 파일은 오래된 PID를 가리키므로 실제 프로세스로 확인했으며 PID 파일을 수정하지 않았다.
비교 질문 자동 전송이나 유료 모델 테스트는 하지 않았다. 실제 응답 속도와 품질은 미검증이다.

## 설계 응답 교정 중 시간 초과 — 2026-10-26 19:32 KST

사용자가 전달한 `design_timeout` 응답의 요청 계측을 읽기 전용으로 확인했다.
첫 설계 시도는 10.617초 후 응답을 수신했으나 `design_format_error`로 거부됐다.
제공자 사용량은 입력 16,229 / 출력 1,233 / 합계 17,462토큰이었다.
교정 시도는 9.360초 후 전체 20초 제한으로 취소됐다. 두 번째 사용량은 미확인이다.
요청 전체 약 20.182초였으며 실행 단계로 진입하지 않았다.

최소 입력 추정은 14,618토큰(규칙 5,382 / 상태 2,006 / 최근 원문 2,555 / 기능 3,874 등)으로
여전히 소프트 목표보다 컸다. 첫 응답의 원문은 계측에 저장하지 않으므로 정확한 오류 필드는
소급 확정할 수 없다. 제공자가 늦어서 아무 응답도 하지 않은 경우와 구분해야 한다.

수정 사항:
- 설계 도구 선택을 `auto`에서 `required`로 변경한다. 유일하게 노출하는 것은
  변경 권한이 없는 `submit_context_design` 함수다. 제공자/모델의 실제 준수까지 보장하지 않는다.
- 형식 오류일 때 교정 입력에 실제 거부된 제안과 필드 경로/오류 종류를 전달한다.
  이전의 `Invalid design`과 포괄 코드만으로 재생성하던 경로를 보완했다.
  계측에는 값/원문/임의의 필드명이 남지 않도록 경로를 제한한다.
- 완전한 JSON 코드 펜스만 결정적으로 해제한다. 필수 필드/타입/열거값과 의미 검사는 유지한다.
- 현재 입력의 원문에 이미 포함된 동일 제약문과 중복 메타데이터만 제거한다.
  고유 제약·원문·출처·세션 구분·권한 규칙은 보존한다. 목표 토큰 크기 달성을 주장하지 않는다.
- 설계 자체가 없는 실패에 실행 완료 근거 부족을 덧붙이지 않도록 종료 사유를 정리했다.

신규 로컬 검사 6개(잘못된 형식과 정상 형식, 구체적 교정 입력/계측 비식별,
중복 제거 시 원문 보존, 실제 xAI 제공자의 모의 전송 경로 등) 포함 집중 회귀
**632 passed in 17.41s**, 변경 파일 Ruff와 diff 검사를 통과했다. 네트워크 모델 호출은 없었다.

로컬 검증 후 TESTY에 반영하고 독립 세션으로 재시작했다(PID 80850 → 81092).
TESTY만 `designTimeoutSeconds=40`으로 설정했다. 이는 첫 설계+교정 합산 상한이며
재시도를 추가하거나 도구 결과마다 설계 호출을 반복하는 변경이 아니다.
기본 설정/메인은 20초와 선택 기능 false를 유지했고, 메인 설정 해시와 PID 유지도 확인했다.
설정 백업: `.local/config.testy.json.before-designer-recovery-20261026-193220.bak`.

TESTY 상태 확인 HTTP 200, 에이전트 루프 시작, 텔레그램 연결, 시작 오류 0건을 확인했다.
MCP 연결 취소 경고는 계속 있으며 다음 메시지에서 재시도한다. 실제 MCP 사용은 미검증이다.
실패 요청을 자동 재전송하거나 기록을 삭제하지 않았다. 실모델 형식 준수·속도·완료율 개선은
아직 확인되지 않았으며 동일 원래 요청을 새 메시지로 시험해 평가해야 한다.

## 스트림 수신·추출 수정 사용자 승인 적용 — 2026-10-27 06:55 KST

사용자의 적용·재시작 요청으로 로컬 검증된 스트림 수정 코드를 TESTY에 반영했다.
동일 저장소/가상환경 경로를 확인하고 PID 81276을 정상 종료한 뒤 PID 86705로 시작했다.
강제 종료는 필요하지 않았다. 상태 확인 HTTP 200, 에이전트 루프 시작, 텔레그램 연결,
시작 ERROR/Traceback 0건을 확인했다. MCP 연결 취소 경고는 남아 다음 메시지에서 재시도한다.
메인 PID 49369와 메인/TESTY 설정 해시를 유지했다. 선택 기능 true/failureMode block,
설계 제한 40초 등 기존 설정은 변경하지 않았다. 유료 모델 호출이나 실패 요청 재전송은 하지 않았다.
수정 범위/검증/운영 인과관계의 미확인 사항은 `context-design-stream-repair.md`를 참조한다.

### 2026-10-27: source dependency repair after the 07:33 failure

The 07:33 design completed and passed schema validation at 25.36s, then failed
semantic validation with `unloaded_task_constraints`. Its correction consumed the
remaining 14.7s of the shared 40s deadline. The 07:30 request is distinct: output
arrived at 21.56s but no terminal event was observed before cancellation. Provider
generation versus transport delay remains unknown for that request.

Added `ContextSelector.restore_design_sources` at the pre-inference boundary.
The minimal candidate view remains sparse; before sending it, the selector reads
all missing original event dependencies of the current task through the scoped
raw reader. Only available, unredacted user originals in the same session are
added, once, in sequence order. No extra model call, timeout increase, fallback,
or validator relaxation was added. Loading originals after authoring an answer
would not establish that the answer respected them, so restoration occurs before
inference. Missing originals still fail the existing semantic guard.

Existing task ledgers are not rewritten: ambiguous legacy entries remain source
candidates, rather than being deleted through another lexical heuristic. The
normal state update already incorporates the current user event only into the
selected task. This repair does not prove all legacy task associations correct.

Local verification: 140 tests passed across context selection, all 11 audit
counterexamples (unchanged), adjacent repairs, design recovery and stream
lifecycle. New regression covers an original beyond the recent 80-event window,
without lexical prohibition keywords, reaching the first mock model call;
continuation succeeds in one call; restoration is idempotent. Unavailable-source
validation remains rejecting. Ruff and diff whitespace checks passed.

No runtime configuration change, restart, or paid model call in this repair.
The separate no-terminal-before-deadline incident is not claimed fixed.

### Per-event diagnostics after the 07:44 timeout

Added paired `stream_receive_wait_started/finished` and
`stream_event_received/processed` milestones to the shared Responses consumer.
Every event has a local sequence number and the existing durable attempt ID.
Event receipt and processing completion use monotonic timestamps. Processing
includes callbacks and extraction; cancellation/error in either receive or
processing is distinguished. Terminal return records processing completion and
never starts a spurious next-event wait. Item types distinguish reasoning,
message and function_call. Unknown event/item labels are normalized, not copied.
No payload, arguments, reasoning text, provider item IDs, or credentials are added.

The receive interval includes HTTP/SDK waiting and event decoding; these records
alone cannot prove provider generation latency versus intermediate transport
buffering. Event processing duration includes diagnostic persistence overhead.
No timeout/retry policy, validation requirement, or success criterion changed.
Tests simulate a blocked callback, a held-open stream, terminal return, and
SQLite persistence with the same attempt ID. Existing terminal/error lifecycle
coverage remains enabled. Deployment/restart and paid reproduction are not part
of this instrumentation change.

### Fix: streaming diagnostics no longer rewrite growing snapshots

Confirmed instrumentation regression on the 07:51 attempt: 1,374 events produced
5,523 milestones. `mark()` synchronously rewrote both aggregate and attempt JSON
on every milestone. The received sequence was reasoning, 1,250 text deltas, then
function arguments; cancellation was delivered only after 46s. This new overhead
does not retrospectively explain incidents before per-event instrumentation.

`ContextTrace.mark()` now queues stream milestones without SQLite operations.
A background asyncio task sends batches of at most 256 immutable events to a
worker thread, with 50ms batching intervals. The additive `context_model_events`
table is keyed by attempt and ordinal; duplicate appends are ignored. The worker
only appends events, never overwrites lifecycle state. Existing start/end/timeout
snapshots remain durable and include the full in-memory event sequence. Restart
recovery merges the append journal with the snapshot. A failed background write
retains queued data and logs a content-free warning; a later lifecycle snapshot
still records those events. Abrupt process exit can lose not-yet-flushed event
details; the write-ahead attempt remains in-flight and recovers as result_unknown,
never zero usage or false success.

Offline replay of the same 5,498 stream milestones in temporary databases:
old synchronous persistence 29.259s; new stream path 0.0076s, background flush
included 1.223s. These are diagnostic overhead measurements, not provider latency
or real-model completion comparisons. Tests cover large streams without aggregate
rewrites, restart merge, duplicate append, slow storage/cancellation, late writes
not overwriting timed_out, and failed append preserving terminal snapshots.
No timeout, retry policy, validator or model settings changed. No service restart
or paid model call performed in this fix.
