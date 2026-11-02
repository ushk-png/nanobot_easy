# 첫 설계 호출 실패: 수신·추출·검증·반환 결함 수정

2026-10-27. 운영 설정 변경·서비스 재시작·유료 모델 호출 없이 소스 수정과 로컬 검증을 수행했다.
대체 실행 경로, 검증 우회, 추가 재시도, 타임아웃 확대를 이번 수정에 추가하지 않았다.
기존 작업 디렉터리의 미커밋 변경을 보존했다.

## 두 운영 실패의 분리

| 구분 | 실제 남은 기록 | 확인된 중단 위치 | 미확인 |
|---|---|---|---|
| 이전: 19:27 요청 | 첫 호출 10.617초, 입력 16,229/출력 1,233토큰 수신 후 design_format_error. 교정 호출 9.360초 뒤 합산 20초 제한 | 첫 결과가 제공자 함수에서는 반환됐으나 설계 형식 검사 실패. 다음 교정은 반환 전 취소 | 첫 원본 결과·추출값·finish_reason·당시 wire 스키마/옵션 원문은 저장되지 않아 정확한 오류 필드/발생 주체 확정 불가 |
| 이번: 19:50 요청 | 첫 호출 1회 39.999초 후 timed_out. 교정/형식 오류 기록 없음. 사용량 null | 첫 제공자 호출 대기 중 합산 40초 제한으로 취소. 검증/실행 단계 진입 전 | 인증·응답 생성·첫 이벤트·완료·연결 종료의 당시 시점이 없어 어느 구간이 40초를 사용했는지 확정 불가 |

해당 요청 ID 끝부분은 각각 `1793010431132428000`, `1793011828128974000`이다.
이후에도 첫 설계 단일 호출의 약 40초 시간 초과 기록이 존재한다.
시간 초과 자체는 모델의 설계 능력 부족이나 JSON 생성 실패를 입증하지 않는다.
과거 20→40초 변경은 이전 작업에서 이루어진 것이며, 이번에는 유지했다.

## 인과관계를 분리한 결함과 수정

| 실패 현상 | 가설·확인/배제 증거 | 수정 | 수정 전 → 후 | 운영 장애와의 관계 |
|---|---|---|---|---|
| 완료 이벤트 뒤에도 미반환 | 기존 루프는 response.completed에서 상태만 저장하고 EOF를 기다림. 연결 유지 모의 실험으로 재현 | 터미널 스냅샷 추출 직후 반환. SDK와 raw SSE가 같은 상태 기계를 사용 | completed+연결 유지: timeout → 정상 반환 | 실제 운영 스트림 이벤트 기록이 없어 직접 원인 여부 미확인 |
| 정리 때문에 완료 응답이 시간 초과 | 파서만 고쳐도 HTTP async-with 종료가 결과 반환 전에 실행됨. 예전 HTTP 함수에 고친 파서를 넣어도 느린 정리에서 timeout 재현 | 연결/클라이언트 정리를 별도 소유 태스크로 분리. 각 정리에 독립적인 5초 제한. 실제 모델/설계 제한은 변경 없음 | 정리 대기: timeout → 정상 결과 즉시 반환; 정리 실패는 별도 상태 | 실제 운영 정리 시간 미기록으로 직접 원인 여부 미확인 |
| 완료 스냅샷만 받은 경우 결과 누락 | 기존 파서는 completed.response.output의 텍스트/함수 호출을 추출하지 않음 | 비어 있지 않은 최종 output을 권위 있는 결과로 사용 | 도구 0개 → 정상 도구 1개 | 이전 형식 오류를 만들 수 있는 수신 결함. 당시 원본 부재로 인과 미확인 |
| 부분 인자를 최종 인자로 오인 | 기존 우선순위가 부분 버퍼→output_item.done.arguments. 완성 인자가 있어도 부분 JSON을 반환하는 모의 반례 재현 | 최종 항목 우선. 전체 최종 스냅샷 우선. 같은 호출 ID의 반복 done은 하나로 유지 | '{"value":' → {'value': 2}; 중복 호출 제거 | 이전 모델 응답이 잘못됐다고 단정할 수 없음을 보여줌. 당시 발생 여부 미확인 |
| EOF를 성공으로 오인 | 터미널 이벤트가 없는 연결 종료도 stop으로 반환하던 반례 재현 | 터미널 없는 EOF는 오류. incomplete는 length로 전달하고 설계에서 거부. 부분 도구 버퍼도 완료로 승격하지 않음 | EOF+부분 결과 stop → 오류 | 불완전 응답을 검증 우회로 통과시키지 않음 |
| 설계 역할과 실행 역할 충돌 | 첫 시스템에 직접 텍스트 답변, 실행 도구 호출, 주제 메모 파일 쓰기, 매체 전달 지시가 들어감. 유일한 설계 제출 함수와 상충 | 설계 전용 identity. 단계별 권한 문구는 공통 스니펫 공유. 작업/사용자 bootstrap 원문 전부 유지 | 실행 전용 지시 존재 → 없음; 권한·원문 유지 검사 통과 | 불필요/상충 입력은 확인. 실제 지연 또는 형식 오류의 원인이었다는 증거는 없음 |

수정 전 재현은 HEAD의 파서 및 HTTP 함수 소스를 **메모리에서만** 실행하고, 동일 합성 스트림과
모의 HTTP 객체를 사용했다. 체크아웃 교체·운영 실행·원격 요청은 없었다.

## 터미널 상태·취소·정리 계약

- `response.completed`: 최종 output이 있으면 이를 추출하고 즉시 반환한다. delta와 최종 output을
  덧붙여 중복 생성하지 않는다. 일부 서버가 최종 output을 생략한 경우, 이미 완료된 항목만 사용한다.
- `response.incomplete`: length 결과로 종료한다. 설계 결과 JSON이 문법상 유효해도 성공으로 채택하지 않는다.
- failed/cancelled/error: 실패로 종료한다. 터미널 이벤트가 없는 EOF도 실패다.
- 완료 전 취소: CancelledError를 전달하고 부분 결과를 채택하지 않는다.
- 호출 결과가 반환된 뒤 취소: 이미 확보한 결과/사용량은 남긴다. 정리 태스크만 기다리던 이유로
  완료된 호출을 실패로 바꾸지 않는다. 사용자 취소의 실제 도구 실행 차단은 기존 지시 버전 가드를 유지한다.
- 정리 오류·시간 초과: 결과와 별도로 기록하고 다음 자원의 정리도 시도한다. 각 자원당 5초 제한이며
  이 제한은 설계 호출의 생성/수신 제한을 늘리는 설정이 아니다. 프로세스가 종료되면 정리가 중단될 수 있다.
  그 경우 cleanup_started만 남을 수 있으며 정리 성공으로 꾸미지 않는다.
- raw xAI/Codex HTTP와 SDK 스트림 소유 경로를 함께 보완했다. 스트림에 대한 추가 생성 요청은 없다.

## 같은 호출 ID로 남기는 단조 시계 계측

`context_model_attempts`의 기존 attempt_id에 `milestones`를 추가했다. 각 시점은 요청 시작 기준
`monotonic_ms`이며 UTC 벽시계와 별개다. 재시도는 별도의 attempt_id로 연결한다.
입력 조립 시점은 실제 전송 전 생성하는 동일 시도에 붙이고, 늦은 정리 시점도 원래 시도에 갱신한다.

| 구간 | 기록 |
|---|---|
| 입력 조립 | input_assembly_started / input_assembly_finished |
| 설계 한도 | design_deadline_started / design_deadline_expired |
| 제공자 진입과 인증 | provider_call_started, authentication_started/finished |
| 실제 전송 | request_dispatch: HTTP 전송 시작 직전 |
| 연결/헤더 | connection_established/tls_established: HTTPcore 연결 추적; response_headers: 응답 헤더 확보. 재사용 연결은 새 연결 이벤트가 없을 수 있음 |
| 수신 | first_event: 최초 해석된 SSE/SDK 이벤트; first_output: 최초 내용/도구 결과 |
| 종료 | terminal_event: 완료·실패·불완전·취소 종류와 상태; 터미널 없는 경우 stream_eof |
| 추출 | result_extracted: 크기·호출 수·내용 해시 |
| 검증 | schema_validation_started/finished, semantic_validation_finished |
| 함수 반환 | provider_call_returned, design_call_returned |
| 취소 | cancellation_received: 제공자 await에 취소가 전달된 시각 |
| 정리 | cleanup_started, resource_cleanup_started/finished, cleanup_finished |
| 내부 재시도 | retry_wait_started/finished, authentication_retry_started/finished |

텍스트·인증정보 대신 이벤트 종류, 상태, 바이트 수, 개수, ID, 해시, 허용한 설정만 저장한다.
원시 응답이나 오류의 임의 키/값은 새 계측에 저장하지 않는다. 기존 파서 경고도 원문 대신 길이만 남긴다.
필수 전송 전 저널 기록은 유지하고, 부수적인 단계 기록 실패가 완료 결과를 훼손하지 않도록 분리했다.

현재 xAI는 HTTPX raw SSE를 사용하며 OpenAI SDK 내부 재시도는 관여하지 않는다.
설계 40초는 **입력 조립 뒤**, 인증·요청·수신·추출·검증 및 기존 교정 호출을 합친 전체 제한이다.
HTTP 기본 I/O idle 제한은 90초이며 전체 제한보다 먼저 끝나는 보장은 없다. 과거 프로세스의 환경 오버라이드 실효값은 기록되지 않았으므로 기본값과 구분한다.
기존 제공자 transient 재시도 대기는 1/2/4초, xAI 401 인증 갱신 재전송은 기존 1회다.
이번 수정은 이 횟수나 정책을 늘리지 않는다. 동기 OAuth 갱신 스레드는 await 취소만으로 물리적으로
중단되지 않을 수 있으며, 과거 이 구간의 소요 시간은 미확인이다.

## 입력 약 15,114토큰의 구성

문제 요청의 당시 계측:

| 당시 구성 | 추정 토큰 | 해석 |
|---|---:|---|
| 첫 messages 전체 | 13,927 | 시스템과 최소 payload의 합 |
| 설계 시스템 전체 | 5,382 | 역할 지침 + 공통 identity + 작업 공간 bootstrap |
| 사용자 요청 | 177 | 요청 원문 |
| 최근 대화/출처 | 2,596 | 제한된 최근 원문과 참조 복원. 전체 세션 재생 아님 |
| 작업 상태 | 2,006 | 목표/제약 후보/결정/미완료 상태. 장기 기억 전문 아님 |
| 기능 이름·짧은 설명 | 3,874 | 도구와 스킬 목록. 전체 실행 도구 JSON 정의 아님 |
| 자료 단서 | 16 | 알려진 자료명 등 |
| 출력 스키마 자체 | 1,155 | 실제 도구 정의는 설계 제출 함수 하나 |
| 전송 직전 messages+도구 | 15,114 | 도구/메시지 포장 포함 추정치. 제공자 실측 아님 |

위 하위 수치는 당시 정리 전 payload를 독립 추정한 값으로 서로 중첩하며 합산하면 안 된다.
messages 합과 전송 직전 합의 차이 약 1,187은 설계 함수 정의(스키마+함수 포장)에 해당한다.
이전 19:27 호출에서 확인된 제공자 입력 16,229토큰을 이번 호출의 실측으로 재사용하지 않는다.

불필요한 구성으로 확인한 것은 **공통 identity 안의 실행 전용 지시**와 **중복된 표현**이다.
동일 현재 작업 공간 파일을 정적으로 비교하면 공통 프롬프트가 4,775 → 2,797토큰으로 줄었다.
사용자 bootstrap, 공유 단계별 권한, 신뢰 경계는 보존했다. 고유 제약이나 필요한 대화를 절단하지 않았다.
현재 요청을 출처 원문에서도 반복하던 표현은 content_ref=request로 연결하고 실제 원문과 이벤트 ID는 유지한다.

새 계측은 최종 wire payload의 사용자 원문/최근 대화/상태/기능/참조 구성별 추정치,
역할 지침·공통 규칙·출력 스키마·교정 메시지의 추정치와 wire 전체 크기를 분리한다.
전체 도구 정의 수와 실제 schema hash도 기록한다. 추정값의 독립 측정 특성을 표시하고,
구성별 합을 제공자 실측처럼 취급하지 않는다. 실제 제공자 토크나이저의 정확한 분해는 미확인이다.

## 이전 형식 오류의 확인 범위

이벤트 DB에 `submit_context_design`의 원본 도구 호출/결과 기록은 0건이었다. 설계 호출은 실행
도구 루프와 달리 원본을 세션에 넣지 않았고 당시 계측은 사용량과 포괄적인 오류 코드만 저장했다.
따라서 첫 운영 응답을 되살려 검증했다고 보고할 수 없다.

| 확인 항목 | 결론 |
|---|---|
| 구조화 출력 설정 전달 누락 | 코드상 함수 parameters를 전달한다. 당시 auto였고 server strict JSON 모드를 강제한 경로는 아님. 당시 wire 원문 부재로 실제 수신 설정까지 소급 입증 불가 |
| 스키마/검증기 불일치 | 현재는 동일 IntentDesign.model_json_schema를 전송·검증한다. 새 모의 통합 검사에서 조립/실제 wire schema hash 동일. 당시 schema snapshot 부재로 소급 확정 불가 |
| 스트림 중복/누락 | 최종 스냅샷 누락·부분 버퍼 우선·중복 done 결함을 로컬 재현 및 수정. 과거 발생 여부 미확인 |
| 부분 출력 파싱 | 최종 스냅샷만 있는 경우와 잘린 버퍼 반례 재현. 이제 터미널/완료 항목 없으면 성공 불가 |
| 출력 제한 | 당시 출력 1,233토큰이나 finish_reason이 남지 않아 단정 불가. 이제 length 응답은 교정으로 성공 처리하지 않음 |
| 교정 설정 문제 | 당시 첫 오류 뒤 교정이 남은 제한으로 취소. 실제 원본을 소급 확인할 수 없음. 새 계측은 각 시도의 설정/검증 상태/반환 시점을 연결 |

검증기의 필수 필드·타입·열거값·의미 검사는 완화하지 않았다. 모델이 틀린 것과 수신 코드가
훼손한 것을 분리해야 하며, 현재 증거로 당시 모델의 형식 준수 능력을 단정하지 않는다.

## 실제 적용 설정과 최소 후속 실험

읽기 전용으로 확인한 TESTY 설정은 `xai-grok/grok-4.7` / xai_grok, 설계 reasoning low,
max_output_tokens 3000, wire stream=true, 유일한 submit_context_design 함수, tool_choice=required다.
extra_body 오버라이드가 없다. 실행 모델의 무거운 generation 설정 대신 설계 값이 명시적으로
전달되는 것을 모의 실제 제공자 경로에서 확인했다. 과거 첫 사례는 auto였고 이후 required로
바뀌었으며, 이번에는 모델·추론·출력 제한·tool_choice·타임아웃을 변경하지 않았다.

로컬로 증명할 수 없는 것은 실제 제공자의 생성/대기 지연과 실제 요청의 시간 내 완료율이다.
추가 승인을 받은 제한 실험은 다음 순서가 적절하다.

1. 비식별 고정 요청, 동일 문맥 스냅샷, 동일 모델/low/3000/required/현재 제한을 고정한다.
   새 코드로 단일 호출의 단계 시간을 확보한다. 자동 재전송은 하지 않는다.
2. 응답이 도착하면 동일 SSE를 메모리에서 수정 전/후 파서에 재생한다. 출력 해시·완료 시점과
   함수 반환 시간을 비교한다. 별도 유료 호출 없이 수신/추출 요인만 비교할 수 있다.
3. 입력 역할 분리 효과를 평가하려면 동일 조건에서 시스템 프롬프트만 전/후로 바꾼 한 쌍을
   비교한다. 추론 수준·출력 제한·기한을 동시에 바꾸지 않는다. 두 호출만으로 평균 성능을
   입증하지 않으며, 불안정하면 추가 표본 승인 없이 반복하지 않는다.
4. 헤더/첫 이벤트 전 지연이면 인증·연결 구간, 첫 출력~완료가 길면 생성 구간,
   완료~추출/반환이 길면 수신 코드 구간으로 구분한다. 미완료 호출의 사용량은 미확인으로 둔다.

## 로컬 검증 결과

최종 검사 결과와 변경 파일 목록은 아래에 기록한다. 운영 장애의 직접 원인은 여전히 미확인인
부분이 있으며, 이번 완료 범위는 **원인이 재현된 코드 결함 수정과 설계 결과 수신·추출·검증·반환의
로컬 정상 동작 확인**이다. 실모델 품질/속도/완료율까지 검증 완료한 것이 아니다.

### 최종 결과와 변경 파일

- 관련 집중 회귀 **801 passed in 19.27s**. 기존 선택 54개와 감사 반례 11개 포함.
- 새 설계 복구/단계 통합 검사 13개와 스트림 수명주기 검사 18개가 포함됐다.
- Ruff 및 `git diff --check` 통과. 전체 저장소 테스트를 실행한 것은 아니다.
- pytest 종료 시 기존 SWIG `swigvarlink` DeprecationWarning 1건. 테스트 실패는 없다.
- 기존 네트워크 옵션 검사 2개의 빈 HTTP 응답 fixture를 정상 completed SSE로 보강했다.
  프록시/idle 설정의 기존 assert는 유지했고, 빈 EOF 거부는 새 독립 회귀 검사로 추가했다.
- 동일 작업 공간 파일의 정적 입력 비교이며 실모델 토큰·지연·품질 개선을 입증한 결과가 아니다.
- 메인/TESTY 설정 해시가 마지막 재시작 때와 동일하며 TESTY PID 파일은 81276이다.
  이번 작업에서 서비스 활성화·재시작·실패 요청 재전송·유료 모델 호출을 하지 않았다.

| 파일/함수 | 변경 목적 |
|---|---|
| `nanobot/providers/openai_responses/parsing.py`, `_consume_response_events` | 터미널 즉시 반환, 최종 결과 우선 추출, 부분/중복 보존 오류 수정, EOF 실패, SDK/raw 공통화 |
| `nanobot/providers/call_observation.py` | 호출별 내용 없는 시점 관찰, TCP/TLS 추적, 독립 자원 정리와 결과 분리 |
| `nanobot/providers/xai_grok_provider.py`, `_request_xai` | 인증/실제 wire 옵션/스키마/전송/헤더 관찰, 반환을 막던 정리 분리 |
| `nanobot/providers/openai_codex_provider.py`, `_request_codex` | 같은 HTTP 정리 문제의 공통 경계 수정 |
| `nanobot/providers/openai_compat_provider.py` | SDK wrapper가 가린 원본 스트림 close를 정리 경로에 연결 |
| `nanobot/providers/base.py` | 실제 시도별 관찰 ID 연결, 취소/반환/기존 내부 재시도 대기 시점 기록 |
| `nanobot/agent/context_trace.py` | 단조 시계 milestones, 개별 시도 구성 추정치, finish_reason/응답 크기 기록 |
| `nanobot/agent/context_selection.py` | 조립/검증/반환 시점, 최종 입력 구성 추정치, 불완전 출력 거부, 중복 요청 참조 |
| `nanobot/agent/context.py`, `templates/agent/design_identity.md` | 설계 역할에 불필요하고 상충하는 실행 프롬프트 분리 |
| `templates/agent/_snippets/permission_gates.md`, `identity.md` | 설계/실행이 같은 단계별 권한 원문을 공유하도록 이동. 실행 identity의 정책 내용 유지 |
| `tests/providers/test_response_stream_lifecycle.py` | 터미널/추출/정리/취소 인접 사례 |
| `tests/agent/test_context_design_recovery.py` | 실제 입력 조립→xAI 모의 HTTP/SSE→엄격한 검증→반환→시도 영속화 통합 검사 |
| `tests/providers/test_openai_codex_provider.py` | 기존 네트워크 옵션 검사의 정상 프로토콜 fixture 보강 |

재현 가능한 집중 명령:

```bash
.venv/bin/python -m pytest \
  tests/agent/test_context_design_recovery.py \
  tests/agent/test_context_selection.py \
  tests/agent/test_context_selection_requirements_audit.py \
  tests/agent/test_context_selection_repair.py \
  tests/providers/test_response_stream_lifecycle.py \
  tests/providers/test_openai_responses.py \
  tests/providers/test_xai_grok_provider.py \
  tests/providers/test_openai_codex_provider.py -q --tb=short
```

801개 검사는 위 범위에 기존 runner/loop/task note/answer delivery/memory/filesystem/search/
채널/config/provider retry와 Azure/Responses circuit breaker 회귀를 함께 실행한 결과다.
실제 생성 지연을 단정하거나, 타임아웃 확대/우회 실행을 기능 완료의 근거로 사용하지 않았다.
