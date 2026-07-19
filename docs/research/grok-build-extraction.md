# Grok Build 방법론 추출 요약

목적: xai-org/grok-build에서 코딩 에이전트의 작업 원칙만 추출한다. 원문 프롬프트 문장을 복사하지 않고, nanobot 스킬에 옮길 수 있는 원칙 중심으로 정리한다.

## 조사 범위

- 조사 저장소: https://github.com/xai-org/grok-build
- 임시 클론 경로: `/tmp/grok-build-distill-20260717`
- 제외: Rust 코드 이식, Grok Build 실행/연동, 모델 전용 튜닝 문구 복제
- 참고한 내부 기준:
  - `docs/design/skill-framework-implementation-v3.3---14b5c557-d23d-4fec-80ed-09b60e10a786.md`
  - `docs/design/skill-writing-guide---61318583-ff6e-4de5-aaf3-02d2afa05c5b.md`

## 추출 자산

### 1. 시스템 프롬프트 / 에이전트 지시문

원문 위치:
- `crates/codegen/xai-grok-agent/templates/prompt.md`
- `crates/codegen/xai-grok-agent/templates/apply_patch_prompt.md`

요지:
- 사용자의 요청을 끝까지 해결하되 추측하지 않는다.
- 코드 수정은 기존 코드 스타일과 구조를 존중하며 최소 변경으로 수행한다.
- 원인에 가까운 수정을 선호하고 표면적 임시방편은 피한다.
- 관련 없는 버그나 테스트 실패는 고치지 않고 보고만 한다.
- AGENTS.md 같은 프로젝트 규칙을 우선 확인하고 적용한다.
- 복잡하거나 모호한 작업은 계획을 세우고 진행 상황을 갱신한다.
- 검증은 변경 지점에 가까운 테스트부터 시작해 점차 넓힌다.
- 최종 응답은 변경 내용, 검증 결과, 남은 위험을 간결하게 보고한다.

이 규칙이 존재하는 이유:
- 코딩 에이전트가 과도하게 범위를 넓히거나 불필요한 리팩터링을 하지 않도록 제한한다.
- 사용자의 기존 작업물과 저장소 관례를 보호한다.
- 수정 성공 여부를 말이 아니라 빌드/테스트로 확인하게 한다.

제외 표시:
- Grok Build 또는 특정 모델 정체성, 시스템 프롬프트 비공개 지시, UI 전용 응답 형식은 nanobot 스킬로 옮기지 않는다.

### 2. Permission 모드 / allow-deny / 안전 규칙

원문 위치:
- `crates/codegen/xai-grok-pager/docs/user-guide/22-permissions-and-safety.md`
- `crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md`
- `crates/codegen/xai-grok-pager/docs/user-guide/18-sandbox.md`

요지:
- 권한은 allow보다 deny가 우선한다.
- 읽기 전용 작업과 쓰기/실행 작업을 분리한다.
- 자동 승인 모드에서도 위험 명령과 외부 공유 상태 변경은 별도 주의가 필요하다.
- headless 자동화에서는 도구 허용/차단 목록과 최대 턴 수를 좁게 잡는다.
- 샌드박스는 읽기/쓰기 범위와 네트워크 접근을 제한하는 안전망이다.
- 비밀 파일, 환경 파일, 키 파일은 읽기/쓰기 모두 차단 대상으로 본다.

이 규칙이 존재하는 이유:
- 자동화된 코딩 에이전트가 파일 삭제, 원격 push, 비밀 유출, 외부 상태 변경을 일으키지 않게 한다.
- 실패 시 피해 반경을 작업 디렉토리와 승인된 명령으로 제한한다.

nanobot 적용 원칙:
- 스킬은 위험 명령을 직접 허용하지 않는다.
- `rm`, `sudo`, 강제 reset/push, broad delete, 비밀 출력은 명시 금지한다.
- 검증 명령은 프로젝트에 이미 존재하는 빌드/테스트 명령만 우선 사용한다.

### 3. AGENTS.md / 프로젝트 규칙 처리

원문 위치:
- `crates/codegen/xai-grok-agent/templates/apply_patch_prompt.md`
- `crates/codegen/xai-grok-pager/docs/user-guide/12-project-rules.md`

요지:
- 저장소와 하위 디렉토리의 지시 파일을 프로젝트 규칙으로 본다.
- 더 깊은 디렉토리의 규칙이 더 구체적이므로 우선한다.
- 수정 대상 파일이 속한 범위의 규칙을 따라야 한다.
- 규칙 파일은 코딩 스타일, 테스트 방법, 아키텍처 경계, PR/커밋 관례를 담을 수 있다.
- 규칙은 짧고 구체적일수록 잘 지켜진다.

이 규칙이 존재하는 이유:
- 같은 저장소 안에서도 하위 패키지별 관례가 다를 수 있다.
- 에이전트가 전역 취향으로 코드를 바꾸지 않고, 해당 코드베이스의 로컬 규칙을 따르게 한다.

nanobot 적용 원칙:
- 코드 수정 스킬의 1단계는 `AGENTS.md`, `CONTRIBUTING.md`, README, package/build 설정 확인이다.
- 하위 디렉토리 규칙이 있으면 해당 범위 파일 수정에 우선 적용한다.

### 4. 실패·재시도·검증·롤백 관련 지시

원문 위치:
- `crates/codegen/xai-grok-agent/templates/apply_patch_prompt.md`
- `crates/codegen/xai-grok-pager/docs/user-guide/19-plan-mode.md`
- `crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md`

요지:
- 파일 수정 후에는 관련 빌드/테스트/포맷 검증을 고려한다.
- 검증은 변경 코드에 가장 가까운 단위부터 시작한다.
- 실패하면 관련 없는 문제까지 고치지 말고, 자신의 변경과 관련된 오류인지 구분한다.
- 포맷/테스트 수정은 제한된 횟수만 반복하고, 계속 실패하면 사용자에게 상태를 보고한다.
- 모호하거나 고영향 작업은 계획 승인 후 구현한다.
- headless 작업은 최대 턴 제한에 걸릴 수 있으므로 큰 작업은 작은 단위로 쪼갠다.

이 규칙이 존재하는 이유:
- 검증 실패 시 에이전트가 무한 수정하거나 범위를 넓히는 것을 막는다.
- 사용자가 원하는 변경과 별개인 기존 실패를 건드리지 않게 한다.
- 자동화 세션의 턴/권한 제한을 고려해 작업을 완료 가능한 단위로 나누게 한다.

nanobot 적용 원칙:
- 코드 수정은 `읽기 → 최소 diff → 검증 → 보고` 흐름으로 고정한다.
- 실패 재시도는 제한하고, 실패 원인/실행한 명령/남은 리스크를 보고한다.
- 대규모 변경은 먼저 계획을 내고 승인받는다.

## 추출된 핵심 원칙 목록

1. 대상 저장소 규칙을 먼저 읽는다.
2. 변경 전 주변 코드를 충분히 읽는다.
3. 요구 범위를 벗어난 수정은 하지 않는다.
4. 가능한 최소 diff로 수정한다.
5. 루트 원인을 고치되 과설계하지 않는다.
6. 비밀, 삭제, 원격 push, 공유 상태 변경은 명시 승인 없이는 금지한다.
7. 검증은 변경 지점에 가까운 명령부터 실행한다.
8. 실패하면 관련 오류와 기존 오류를 구분한다.
9. 반복 수정은 제한하고 실패 상태를 투명하게 보고한다.
10. 최종 보고는 변경 파일, 검증 결과, 미검증 항목만 간결히 포함한다.
