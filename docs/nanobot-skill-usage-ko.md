# nanobot_skill 사용 안내서

이 문서는 `/Users/imkimhk/Project/nanobot_skill` 프로젝트에서 나노봇 스킬 기능을 실행하고 관리하는 방법을 정리한다.

## 기본 실행

프로젝트 루트로 이동한다.

```bash
cd /Users/imkimhk/Project/nanobot_skill
```

자동 실행 스크립트를 사용한다.

```bash
./start-nanobot-skill.sh
```

정지와 재시작은 다음 명령을 사용한다.

```bash
./stop-nanobot-skill.sh
./restart-nanobot-skill.sh
```

로그는 다음 파일에 기록된다.

```bash
.local/logs/nanobot-skill-gateway.log
```

실시간 로그 확인:

```bash
tail -f .local/logs/nanobot-skill-gateway.log
```

## 현재 실행 기준

이 프로젝트의 실행 기준은 다음과 같다.

- Python venv: `.venv`
- 실행 명령: `.venv/bin/nanobot`
- 설정 파일: `.local/config.json`
- workspace: `.local/workspace`
- gateway PID: `.local/run/nanobot-skill-gateway.pid`
- gateway log: `.local/logs/nanobot-skill-gateway.log`

OAuth provider는 OpenAI Codex로 설정되어 있으며, 모델은 `openai-codex/gpt-5.5`를 사용한다.

## 직접 테스트

Gateway를 띄우기 전에 LLM 연결만 확인하려면 다음 명령을 사용한다.

```bash
PYTHONPATH=. .venv/bin/nanobot agent \
  --config .local/config.json \
  --workspace .local/workspace \
  -m "짧게 자기소개해줘"
```

채널 상태는 다음 명령으로 확인한다.

```bash
PYTHONPATH=. .venv/bin/nanobot channels status \
  --config .local/config.json
```

## 스킬 구조

나노봇 스킬은 크게 두 종류로 나뉜다.

- 일반 스킬: `nanobot/skills/*/SKILL.md`
- 시스템 스킬: `nanobot/skills-system/*/SKILL.md`

일반 스킬은 에이전트가 사용자 요청을 처리할 때 검색하고 선택할 수 있는 작업 지식이다. 시스템 스킬은 스킬 조합, 리뷰, 라우팅, 초안 생성처럼 스킬 시스템 자체를 운영하기 위한 내부 스킬이다.

현재 주요 일반 스킬:

- `topic-recall`: 주제 기반 기억 회상
- `document-review`: 문서 검토
- `compare-options`: 선택지 비교
- `code-debugging`: 코드 디버깅
- `meeting-notes`: 회의록 정리
- `research-brief`: 리서치 브리프 작성
- `data-analysis`: 데이터 분석

현재 주요 시스템 스킬:

- `composite-task`: 복합 작업 분해와 위임
- `skill-composer`: 새 스킬 생성 흐름 진입점
- `skill-design-review`: 스킬 설계 검토
- `skill-security-review`: 보안 검토
- `skill-utility-review`: 유용성 검토
- `skill-duplicate-check`: 중복 스킬 확인
- `skill-trigger-differentiation`: 트리거 문구 구분
- `skill-draft-generator`: 스킬 초안 생성
- `skill-test-generator`: 스킬 테스트 생성

## 스킬 레지스트리 갱신

스킬을 추가하거나 수정한 뒤에는 workspace의 SQLite 스킬 레지스트리를 다시 색인한다.

```bash
PYTHONPATH=. .venv/bin/nanobot skill reindex \
  --config .local/config.json \
  --workspace .local/workspace
```

등록된 스킬 목록:

```bash
PYTHONPATH=. .venv/bin/nanobot skill list \
  --config .local/config.json \
  --workspace .local/workspace
```

통계 확인:

```bash
PYTHONPATH=. .venv/bin/nanobot skill stats \
  --config .local/config.json \
  --workspace .local/workspace
```

라우팅 테스트:

```bash
PYTHONPATH=. .venv/bin/nanobot skill test-routing \
  --config .local/config.json \
  --workspace .local/workspace
```

## 스킬 사용 방식

일반 대화에서 사용자는 스킬 이름을 직접 말하지 않아도 된다. 나노봇은 요청 내용에 맞춰 `skill_search` 도구로 관련 스킬을 찾고, 필요한 경우 해당 스킬 지침을 읽어 작업한다.

예시:

```text
이 회의 내용을 결정사항, 액션아이템, 리스크로 정리해줘.
```

이 요청은 `meeting-notes` 계열 스킬로 라우팅될 수 있다.

```text
두 가지 배포 전략의 장단점을 비교하고 추천해줘.
```

이 요청은 `compare-options` 계열 스킬로 라우팅될 수 있다.

```text
이 에러 로그를 보고 원인을 좁혀줘.
```

이 요청은 `code-debugging` 계열 스킬로 라우팅될 수 있다.

명시적으로 스킬을 지정할 수도 있다.

```text
document-review 스킬을 사용해서 이 문서를 검토해줘.
```

## 복합 작업과 서브에이전트

복잡한 요청은 `composite-task` 시스템 스킬을 통해 여러 하위 작업으로 나뉠 수 있다. 구현된 구조는 프로필 기반 서브에이전트를 지원한다.

대표 프로필:

- `researcher`: 조사와 정보 수집
- `reviewer`: 검토와 품질 확인
- `coder`: 코드 분석과 수정
- `writer`: 문서화와 요약

서브에이전트는 허용된 도구와 depth 제한을 가진다. 무한 위임을 막기 위해 `max_subagent_depth`가 적용된다.

## 새 스킬 작성 흐름

새 스킬을 만들 때는 바로 운영 스킬로 넣기보다 composer 흐름을 따른다.

권장 요청:

```text
새 스킬을 만들고 싶어. 반복되는 고객 문의를 분류하고 답변 초안을 만드는 스킬이 필요해.
```

나노봇은 `skill-composer` 시스템 스킬을 통해 다음을 점검한다.

- 기존 스킬과 중복되는지
- 트리거 조건이 충분히 명확한지
- 보안상 위험한 지침이 없는지
- 실제로 재사용 가치가 있는지
- 테스트 케이스가 필요한지

초안 스킬은 검토 전까지 바로 verified 상태가 되지 않는다.

## 스킬 승인과 폐기

비시스템 스킬을 검증 상태로 승격:

```bash
PYTHONPATH=. .venv/bin/nanobot skill approve <skill_id> \
  --config .local/config.json \
  --workspace .local/workspace
```

비시스템 스킬을 deprecated 상태로 변경:

```bash
PYTHONPATH=. .venv/bin/nanobot skill deprecate <skill_id> \
  --config .local/config.json \
  --workspace .local/workspace
```

시스템 스킬은 CLI로 승인 또는 폐기할 수 없도록 보호된다.

## 운영 리포트

자주 쓰이는 스킬을 확인:

```bash
PYTHONPATH=. .venv/bin/nanobot skill hot-path-report \
  --config .local/config.json \
  --workspace .local/workspace
```

수정 또는 폐기가 필요한 스킬 확인:

```bash
PYTHONPATH=. .venv/bin/nanobot skill lifecycle-report \
  --config .local/config.json \
  --workspace .local/workspace
```

이 리포트는 스킬 사용 횟수, 성공/실패 카운터, 라우팅 기록을 바탕으로 한다.

## Telegram에서 사용하기

Gateway가 실행 중이면 Telegram 메시지가 나노봇으로 전달된다.

```bash
./start-nanobot-skill.sh
```

Telegram에서 평소처럼 메시지를 보내면 된다. 스킬은 요청 문맥에 따라 자동 검색된다.

예시:

```text
이 내용을 회의록으로 정리해줘.
```

```text
이 코드 에러의 원인을 찾아줘.
```

```text
이 주제에 대해 짧은 리서치 브리프를 만들어줘.
```

## 문제 해결

Gateway가 시작되지 않으면 로그를 확인한다.

```bash
tail -n 120 .local/logs/nanobot-skill-gateway.log
```

프로세스가 꼬였을 때:

```bash
./stop-nanobot-skill.sh
./start-nanobot-skill.sh
```

OAuth가 만료되었거나 인증 오류가 날 때:

```bash
PYTHONPATH=. .venv/bin/nanobot provider login openai-codex \
  --set-main \
  --model openai-codex/gpt-5.5 \
  --config .local/config.json
```

스킬이 검색되지 않을 때:

```bash
PYTHONPATH=. .venv/bin/nanobot skill reindex \
  --config .local/config.json \
  --workspace .local/workspace
```

Telegram이 비활성으로 보일 때:

```bash
PYTHONPATH=. .venv/bin/nanobot channels status \
  --config .local/config.json
```
