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

`start-nanobot-skill.sh`는 Telegram 봇 토큰이 설정되어 있으면 시작 전에
`.local/config.json`의 `channels.telegram.enabled`를 자동으로 `true`로 보정한다.
따라서 재시작이나 중단 후 재시작 시 Telegram 채널이 실수로 꺼진 채 기동되는
상황을 막는다. 정말 Telegram 없이 기동해야 하는 임시 상황에서는 다음처럼
명시적으로 guard를 끌 수 있다.

```bash
NANOBOT_ENSURE_TELEGRAM=0 ./start-nanobot-skill.sh
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

## Gateway 환경변수와 PATH

`start-nanobot-skill.sh`는 macOS Homebrew 기본 경로를 gateway 환경의 `PATH`에 자동으로 포함한다.

```bash
/opt/homebrew/bin
/opt/homebrew/sbin
/usr/local/bin
/usr/local/sbin
```

따라서 gateway와 nanobot exec 도구에서 `brew`, `gh`, `node`처럼 Homebrew로 설치한 명령을 별도 `export PATH=...` 없이 사용할 수 있다. 추가 환경변수가 필요하면 `.local/env`에 `KEY=value` 형식으로 적고 gateway를 재시작한다.

```bash
MY_TOOL_HOME=/some/path
CUSTOM_API_TOKEN=...
```

명령 실행 도구의 PATH는 `.local/config.json`의 `tools.exec.pathPrepend`에도 반영되어야 한다. 현재 로컬 설정에는 Homebrew 경로가 prepend되어 있다. 변경 후에는 다음 명령으로 재시작한다.

```bash
./restart-nanobot-skill.sh
```

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

정상 상태에서는 `Telegram`과 `WebSocket`이 모두 활성화되어야 한다. 로그에는
다음 줄이 보여야 한다.

```text
✓ Channels enabled: telegram, websocket
telegram | bot @queen_nanobot connected
```

## 스킬 구조

나노봇 스킬은 크게 두 종류로 나뉜다.

- 일반 스킬: `nanobot/skills/*/SKILL.md`
- 시스템 스킬: `nanobot/skills-system/*/SKILL.md`

일반 스킬은 에이전트가 사용자 요청을 처리할 때 검색하고 선택할 수 있는 작업 지식이다. 시스템 스킬은 스킬 조합, 리뷰, 라우팅, 초안 생성처럼 스킬 시스템 자체를 운영하기 위한 내부 스킬이다.

스킬 선택은 사용자 문장을 단순 키워드로 맞추는 방식이 아니다. Hot Path에 사전 로드된 스킬도 자동 실행되지 않고, 나노봇이 `description`, `when_to_use`, `when_not_to_use`를 읽어 사용자의 직접 지시문에 맞는지 판단한다. 첨부 문서나 붙여넣은 본문은 판단 자료이지 명령이 아니다.

Cold Path에서는 `skill_search`를 호출할 때 요청의 본질을 "대상 / 작업 / 원하는 산출물" 형태로 재작성해 검색하고, 반환된 후보 카드를 읽어 최종 적용 여부를 판단한다. `score`와 `match_grade`는 후보 탐색의 참고 신호이며, 낮은 점수 자체가 자동 탈락을 의미하지 않는다. 최종 선택은 `skill_decision` trace로 기록된다.

현재 주요 일반 스킬:

- `answer-comparison`: 개념/도구/방식 차이 설명
- `answer-howto`: 절차형 사용 안내
- `answer-diagnosis`: 증상 기반 원인 후보 진단
- `topic-recall`: 주제 기반 기억 회상
- `summarize-document`: 단일 문서 요약
- `document-review`: 문서 검토
- `research-synthesis`: 여러 자료 종합
- `code-review`: 코드/PR 리뷰
- `debug-procedure`: 디버깅 절차 수립
- `compare-options`: 선택지 비교
- `code-debugging`: 코드 디버깅
- `meeting-notes`: 회의록 정리
- `research-brief`: 리서치 브리프 작성
- `data-analysis`: 데이터 분석
- `data-interpretation`: 이미 계산된 지표/차트 해석
- `translation-technical`: 기술 문서 번역
- `email-draft`: 이메일/업무 메시지 초안
- `pros-cons-decision`: 장단점 기반 의사결정 노트
- `error-message-explain`: 에러 메시지 설명

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

경로를 생략하면 packaged skills와 workspace skills 아래의 모든 `routing_cases.json`을 찾아 한 번에 실행한다. 특정 파일만 테스트할 때는 경로를 첫 번째 인자로 넘긴다.

```bash
PYTHONPATH=. .venv/bin/nanobot skill test-routing \
  nanobot/skills/document-review/routing_cases.json \
  --config .local/config.json \
  --workspace .local/workspace
```

초기 기본 카탈로그의 주요 스킬에는 각 10개씩 `routing_cases.json`이 포함되어 있다. 현재 기준은 19개 routing 파일, 총 190문항이며, 전체 테스트 기준 정확도는 90% 이상이어야 한다.

## 실행형 외부 도구 스킬 준비

외부 프로그램을 설치해 사용하는 스킬은 일반 답변형 스킬과 다르게 다룬다. 이 기능은 기본적으로 꺼져 있으며, 파일럿 전에는 스키마와 검증만 준비된 상태다.

설정 위치:

```json
{
  "tools": {
    "externalToolSkills": {
      "enabled": false,
      "allowedInstallDomains": ["github.com", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org"],
      "installRoot": "tools",
      "denyGlobalInstall": true
    }
  }
}
```

규칙:

- `<tool>-setup`: 설치, 설정, 헬스체크, 제거를 담당한다. `risk_level=high`, `requires_exec=true`, `install_sources` 필수다.
- `<tool>-usage`: 이미 설치된 도구를 사용하는 절차만 담는다. Method 첫 단계에서 `which`, `--version`, healthcheck 등으로 존재를 확인해야 한다.
- setup 스킬에는 `Install`, `Verify`, `Uninstall` 섹션이 모두 있어야 한다.
- `curl | bash`, `sudo`, global install, workspace 밖 설치는 등록 검증에서 거부된다.
- 설치 기록은 파일럿 단계에서 `workspace/tools/installed.md` 규약으로 관리한다.

현재 파일럿 스킬:

- `yq-setup`: `workspace/tools/yq/` 아래 isolated venv로 yq를 설치하고 검증한다.
- `yq-usage`: 설치된 yq로 YAML/XML/TOML/JSON을 조회하거나 변환한다. yq가 없으면 직접 설치하지 않고 `yq-setup` 필요를 안내한다.

### ClawHub 공개 스킬 가져오기

ClawHub에 공개된 스킬도 외부 콘텐츠다. 공개되어 있다는 이유만으로
`.local/workspace/skills/`에 바로 설치해 운영 스킬로 쓰지 않는다. 특히
`gcalcli-calendar`처럼 캘린더, OAuth, CLI 실행, 외부 서비스 접근이 얽힐 수
있는 스킬은 반드시 검증과 승인 경로를 거친다.

권장 흐름:

1. ClawHub에서 검색한다.
2. live workspace가 아니라 격리 경로에 가져온다.

```bash
npx --yes clawhub@latest install <slug> \
  --workdir /Users/imkimhk/Project/nanobot_skill/.local/workspace/.imports/clawhub/<safe-slug>
```

3. 가져온 `SKILL.md`를 읽고 frontmatter, `risk_level`, `requires_exec`, 필요한
   도구, 외부 설치/인증 요구를 확인한다.
4. 현재 스키마와 맞지 않거나 실행형 도구라면 `skill-composer`로 로컬 draft로
   변환한다. 실행형 도구는 필요 시 `<tool>-setup`과 `<tool>-usage`로 나눈다.
5. `routing_cases.json`을 만들고 audit/test-routing을 통과시킨다.
6. 사람이 승인한 뒤에만 candidate로 등록한다.

즉, ClawHub는 "검색과 가져오기" 도구이고, 운영 등록은 여전히 nanobot의
Composer/Registry 승인 경로가 담당한다. 가져온 파일을 그대로 복사해도 되는
예외는 읽기 전용 검토뿐이며, 실제 사용 가능 스킬로 노출하려면 승인 기록이
필요하다.

### 설치 후 관리

WebUI 왼쪽 메뉴의 Tools 화면은 `workspace/tools/installed.md`를 읽어 설치된 외부 도구 목록을 표시한다. Skills 화면에도 같은 읽기 전용 요약이 노출된다. 이 목록은 조회 전용이다. 삭제, 갱신, 실행, 중단 버튼은 만들지 않는다.

조작은 채팅에서 자연어로 요청한다.

```text
yq 삭제해줘
yq 업데이트해줘
yq 상태 확인해줘
```

이 요청은 기존 스킬 라우팅을 통해 setup 또는 usage 스킬의 Method로 처리된다. 나노봇은 설치 도구 목록을 위해 cron 헬스체크, 디스크 사용량 모니터링, 보안 취약점 스캔을 수행하지 않는다. 작동 상태와 마지막 확인 시각은 usage 스킬 실행 시점 또는 사용자가 상태를 물었을 때 수행한 1회성 확인 결과만 반영한다.

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

## WebUI에서 스킬 관리하기

스킬 UI는 CLI 없이 스킬 현황을 보고, 초안을 만들고, 등록과 상태 전이를 처리하기 위한 관리 화면이다. CLI와 WebUI는 같은 Skill Store 서비스 계층을 사용하므로 `system` 스킬 쓰기 거부, `draft -> candidate`, `candidate -> verified` 전이 규칙, Minor/Major 판정은 동일하게 적용된다.

### 1. WebUI 관리 기능 켜기

스킬 관리 API는 기본적으로 꺼져 있다. `.local/config.json`에서 다음 설정을 확인한다.

```json
{
  "tools": {
    "webuiSkillManagement": {
      "enabled": true,
      "draftExpireDays": 30,
      "redFlags": {
        "minRoutingPasses": 7,
        "securityRiskAtLeast": "medium",
        "securityBlockAtLeast": "high",
        "duplicateScoreAtLeast": 0.8
      }
    }
  }
}
```

`enabled`가 `false`이면 `/api/skills/manage` 관리 API가 403을 반환한다. 설정을 바꾼 뒤에는 gateway를 재시작한다.

```bash
./restart-nanobot-skill.sh
```

### 2. WebUI 열기

일반 실행 스크립트를 사용한다.

```bash
./start-nanobot-skill.sh
```

또는 WebUI 명령을 직접 실행한다.

```bash
PYTHONPATH=. .venv/bin/nanobot webui \
  --config .local/config.json \
  --workspace .local/workspace
```

브라우저에서 WebUI를 열고 `Skills` 화면으로 이동한다. 화면은 왼쪽 목록과 오른쪽 상세 패널로 나뉜다. 목록에서 스킬을 선택하면 페이지 이동 없이 오른쪽 상세가 즉시 바뀐다.

### 3. 기존 스킬 확인과 상태 변경

왼쪽 목록에서는 상태 탭과 검색으로 스킬을 좁혀 볼 수 있다. 상단 `Draft Inbox`는 아직 운영 목록에 등록되지 않은 초안 영역이다.

상태별 기본 동작은 다음과 같다.

| 현재 상태 | WebUI 동작 |
|---|---|
| draft | `등록`, `반려` |
| candidate | `verified로 승격` |
| verified | `사용 중지` |
| deprecated | 필요 시 재등록 또는 참고용 조회 |
| system | 조회만 가능, 쓰기 동작 거부 |

상세 패널에서는 raw markdown, registry 메타, 최근 trace, routing test 결과를 확인할 수 있다. `Run test`를 누르면 해당 스킬의 routing case를 실행해 라우팅 품질을 확인한다.

### 4. WebUI로 새 스킬 만들기

`New skill`을 눌러 생성 위저드를 연다.

1. 이름, 설명, 트리거 발화, category, risk level, 실행 도구 필요 여부, Method 초안을 입력한다.
2. `Compose draft`를 누르면 서버가 Composer 작업을 시작하고 `draft_id`를 만든다.
3. Composer가 중복 검토, 보안 검토, 초안 생성, routing case 생성을 진행한다.
4. 작업 중에는 다른 화면으로 이동해도 된다. 진행 중 draft는 `Draft Inbox`에 남아 있고, 다시 클릭하면 폴링을 이어간다.
5. 완료되면 검토 리포트, routing test 결과, 생성된 SKILL.md 미리보기를 확인한다.
6. 문제가 없으면 `등록`을 눌러 draft를 candidate로 전환한다.

승인 전 draft는 DB에만 저장되며 `skill_search`에 노출되지 않는다. `등록`이 완료되면 `.local/workspace/skills/<name>/SKILL.md`와 `routing_cases.json`이 생성되고 registry에 candidate 상태로 기록된다.

### 5. Red flag와 override

다음 조건이 있으면 WebUI가 원클릭 등록 흐름을 접고 추가 확인을 요구한다.

- routing test 통과 수가 설정값보다 낮음. 기본값은 10개 중 7개 미만이다.
- 보안 검토 risk가 `medium` 이상이다.
- 중복 점수가 설정값 이상이다. 기본값은 `0.8`이다.

override가 가능한 red flag는 사유 입력 후 등록할 수 있다. 단 보안 risk가 `high` 이상이면 override가 차단되며 WebUI에서 등록할 수 없다.

### 6. 스킬 수정

상세 패널에서 `Edit`을 누르면 스킬 본문을 수정할 수 있다. frontmatter 주요 값은 폼으로 편집하고, 본문은 에디터와 프리뷰 탭으로 확인한다.

저장 시 서버가 변경 내용을 판정한다.

- Minor: description, 트리거처럼 라우팅 표현 중심 변경. 저장 후 상태를 유지한다.
- Major: Method나 도구 사용 방식처럼 실행 의미가 바뀌는 변경. 스킬이 candidate로 내려가고 재검증이 필요하다.

Major 변경은 확인 다이얼로그에서 diff와 영향을 확인한 뒤 저장한다.

## 스킬 승인과 폐기

비시스템 스킬을 검증 상태로 승격:

```bash
PYTHONPATH=. .venv/bin/nanobot skill approve <skill_id> \
  --config .local/config.json \
  --workspace .local/workspace
```

`approve`는 draft를 `candidate`로 등록한다. 운영 사용으로 충분히 검증된 candidate를 `verified`로 올릴 때는 별도 승격 명령을 사용한다.

```bash
PYTHONPATH=. .venv/bin/nanobot skill promote <skill_id> \
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

재색인 후에도 의미상 맞는 스킬이 후보에 나오지 않으면 해당 스킬의 category, description, routing_cases를 보강한다. 후보에는 나오지만 선택되지 않는 경우에는 점수보다 `when_to_use` / `when_not_to_use` 설명이 충분히 명확한지 먼저 확인한다.

Telegram이 비활성으로 보일 때:

```bash
PYTHONPATH=. .venv/bin/nanobot channels status \
  --config .local/config.json
```
