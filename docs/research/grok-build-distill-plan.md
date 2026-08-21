# Grok Build 방법론 배분 계획

이 문서는 `docs/research/grok-build-extraction.md`의 원칙을 nanobot 스킬에 어떻게 반영할지 정리한다.

중요: 이 단계에서는 스킬 파일을 수정하지 않는다. 아래 계획 승인 후에만 작업 3으로 진행한다.

## 배분표

### A. 기존 스킬 보강: Minor 패치

#### code-review

대상 파일:
- `nanobot/skills/code-review/SKILL.md`

추가 후보:
- Method에 “리뷰 전 프로젝트 규칙/테스트 관례가 보이면 그 기준으로 판단” 추가.
- Method에 “수정 제안은 최소 diff 관점으로 제시하고, 관련 없는 스타일 지적은 후순위” 강화.
- Failure Rules에 “코드나 diff 없이 리뷰 요청이면 대상 경로/스니펫 요청”은 유지하되, “테스트 관례가 확인 안 되면 테스트 공백을 residual risk로 보고” 추가.
- When Not To Use에 `code-modify` 역방향 지목 추가: “사용자가 직접 수정/구현을 요청하면 code-modify 사용”.

이유:
- code-review는 수정하지 않는 스킬이므로 Grok Build의 구현 절차 전체가 아니라, 리뷰 기준과 경계만 보강한다.

#### code-debugging

대상 파일:
- `nanobot/skills/code-debugging/SKILL.md`

추가 후보:
- Method 1단계 앞 또는 뒤에 “AGENTS.md/CONTRIBUTING.md 등 저장소 규칙 확인” 추가.
- “실패 재현 또는 로그 확인 → 가장 작은 책임 경로 추적 → 최소 수정 → 가까운 검증부터 실행” 흐름 강화.
- Failure Rules 추가:
  - 재현 불가 시 추측 수정 금지, 필요한 명령/로그를 요청하거나 관찰 결과만 보고.
  - 검증 실패가 기존 실패인지 자신의 변경 때문인지 구분.
  - 관련 없는 실패는 고치지 않고 보고.
- When Not To Use에 `code-modify` 추가: “명확한 기능 추가/리팩터링/일반 코드 수정은 code-modify 사용”.

이유:
- code-debugging은 실패·버그 중심이다. 일반 코드 변경과 분리해야 라우팅이 명확해진다.

#### debug-procedure

대상 파일:
- `nanobot/skills/debug-procedure/SKILL.md`

추가 후보:
- Method에 “저장소 규칙과 기존 테스트 명령을 확인할 것을 계획에 포함” 추가.
- Method에 “가장 싼 관찰 → 좁은 재현 → 가설별 확인 → 수정 전 중단 조건” 강화.
- When Not To Use에 `code-modify` 추가: “직접 수정/리팩터링/기능 구현을 요청하면 code-modify 사용”.

이유:
- debug-procedure는 실행하지 않는 계획 스킬이므로 직접 수정 원칙은 넣지 않는다.

### B. 신규 스킬 1개: code-modify

신규 파일:
- `workspace/skills/code-modify/SKILL.md`

목적:
- 모든 일반 코드 수정/구현/리팩터링 요청에 적용하는 안전한 코드 변경 절차.
- Grok Build 실행 도구가 아니라, Grok Build에서 추출한 코딩 운영 원칙을 nanobot 방식으로 재작성한 스킬.

예상 frontmatter:
- name: `code-modify`
- status: `draft`
- category: `coding.modify`
- risk_level: `high`
- requires_exec: `true`
- conflicts_with:
  - `code-review`
  - `code-debugging`
  - `debug-procedure`
- author/reference 메타데이터:
  - `distilled from xai-org/grok-build (Apache 2.0)`

트리거 발화 후보:
- “이 코드 수정해줘”
- “기능 구현해줘”
- “리팩터링해줘”
- “파일 만들어줘”
- “빌드 오류 고쳐서 반영해줘”
- “이 요구사항대로 코드 바꿔줘”

When Not To Use 경계:
- 리뷰만 요청: `code-review`
- 원인 진단/버그 재현 중심: `code-debugging`
- 수정 없이 계획만 요청: `debug-procedure`
- 일반 설명/사용법: `answer-howto`

Method 초안:
1. 대상 저장소의 `AGENTS.md`, `CONTRIBUTING.md`, README, 빌드/테스트 설정이 있으면 먼저 읽고 따른다.
2. 변경 대상 주변 코드를 읽고 기존 패턴을 파악한다.
3. 작업 범위가 모호하거나 대규모이면 짧은 계획을 제시하고 승인받는다.
4. 최소 diff로 수정한다. 관련 없는 파일/버그/스타일은 건드리지 않는다.
5. 필요하면 기존 테스트 위치에 맞춰 작은 테스트를 추가/수정한다.
6. 변경 지점에 가장 가까운 빌드/테스트부터 실행한다.
7. 실패 시 원인을 구분해 최대 제한 횟수만 재시도하고, 계속 실패하면 상태를 보고한다.
8. 최종 보고는 변경 파일, 검증 명령, 결과, 미검증 항목만 간결히 작성한다.

Failure Rules 초안:
- 대상 경로가 불명확하면 먼저 확인 질문.
- 빌드/테스트 명령이 없으면 임의 도구를 추가하지 않고 미검증으로 보고.
- 검증 실패가 기존 문제로 보이면 수정 범위를 넓히지 않고 보고.
- 패치 충돌/파일 불일치 시 파일을 다시 읽고 작은 패치로 1회 재시도.
- 권한/환경 문제로 실행 불가하면 실행한 명령과 오류를 보고.

금지 후보:
- 명시 승인 없는 `rm`, `sudo`, `git reset --hard`, force push, broad delete.
- 비밀키/토큰 출력.
- 사용자가 요청하지 않은 대규모 리팩터링.
- 테스트/포매터 도구 신규 도입.
- 관련 없는 실패 수정.

출력 형식 후보:
- 변경: 파일 1~N개 요약
- 검증: 실행 명령과 결과
- 남은 항목: 미검증/주의점

### C. 채택 안 함

- Grok Build 정체성/모델명/시스템 프롬프트 비공개 문구
  - 이유: nanobot 스킬에 필요 없고 모델 전용이다.
- Grok Build TUI preamble 스타일 세부 문장
  - 이유: 사용자 선호와 nanobot 메시징 정책이 우선이다.
- Grok Build permission/sandbox 구현 자체
  - 이유: 하네스 기능이며 스킬 파일로 구현할 대상이 아니다. 원칙만 반영한다.
- Rust 내부 구조, CLI 세션/PTY/대시보드 코드
  - 이유: 이번 작업은 코드 이식이 아니다.
- Grok Build 설치/실행/릴레이 연동
  - 이유: 사용자가 명시적으로 “도구 연결이 아니다”라고 제한했다.
- 원문 프롬프트 문장 단위 복사
  - 이유: 목적은 문구 복제가 아니라 원칙 증류다.

## 작업 3 승인 후 예정 작업

1. 기존 `grok-build-usage` 스킬 삭제 또는 제거 처리.
2. `code-review`, `code-debugging`, `debug-procedure` 보강.
3. `workspace/skills/code-modify/SKILL.md` draft 생성.
4. `code-modify` routing cases 10개 작성.
5. 이웃 스킬 routing cases에 경계 문항 추가.
6. `nanobot skill audit` 및 routing 테스트 실행.
7. A/B 결과 파일 `docs/research/grok-distill-ab.md` 작성.

## 승인 요청

작업 3으로 진행하려면 이 배분 계획을 승인해야 한다.

승인 문구 예:
- “작업 3 진행해”
- “배분 계획 승인, 스킬 반영해”

승인 전까지 기존 스킬 삭제, 기존 스킬 수정, 신규 draft 생성, routing/audit/AB는 실행하지 않는다.
