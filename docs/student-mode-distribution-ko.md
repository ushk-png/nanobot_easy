# 학생 친화 배포판 설계 메모

이 포크는 원본 nanobot을 얇게 감싼 버전이 아니라, 초보자와 학생이 5분 안에
로컬 WebUI에서 자기 봇의 응답을 볼 수 있게 하는 별도 배포판이다.

## 업스트림 기준

- 기준 upstream 저장소: `https://github.com/HKUDS/nanobot`
- 현재 포크 기준점: `335bb65e835b6d4320b54794116c03bb7e4e2193`
- 기준점 날짜: 2026-07-11
- 라이선스: MIT
- 원저작자 표기: `Copyright (c) 2025-present Xubin Ren and the nanobot contributors`

원본 변경을 자동 병합하는 것을 목표로 하지 않는다. 보안 패치, provider/API
호환성, dependency 업데이트, 치명적 버그 수정만 선별 반영한다.

독자적으로 유지할 영역:

- 설치/온보딩
- 학생 모드/일반 모드
- WebUI 초보자 화면
- `safe_mode`
- 학습 로그/복습 큐
- 반복 학습 선생님 구조

## 설치 모드

설치 모드는 기능 구현을 바꾸는 것이 아니라 기본 설정을 바꾼다.

- 학생 모드: 담임 선생님이 메인 에이전트가 되고, 반복 학습 선생님이 복습 큐를 담당한다.
- 일반 모드: 기존 nanobot이 메인이고, 학습 요청 때 담임 선생님을 호출한다.

선생님 이름은 코드에 하드코딩하지 않고 `studentMode` 설정값으로 둔다.

```json
{
  "studentMode": {
    "mode": "student",
    "coachName": "담임 선생님",
    "reviewTeacherName": "엘르 선생님"
  }
}
```

Quick Start에서 학생 모드를 선택하면 다음 설정을 함께 저장한다.

- `studentMode.mode = "student"`
- `tools.safeMode = true`
- 메인 hot-path skill에 `highschool-study` 추가
- `review-teacher` subagent profile 추가

일반 모드에서는 기본 메인은 유지하고, `study-coach`와 `review-teacher`
subagent profile을 사용할 수 있게 둔다.

## 연결 방식

설치 모드와 연결 방식을 분리한다.

- 설치 모드: 학생 모드 / 일반 모드
- 연결 방식: 빠른 연결 / 수동 설정

OpenAI 연결의 목표 UX는 OAuth/ChatGPT 로그인이다. 단, 공식 지원 범위가
제한될 수 있으므로 API 키 입력은 폴백으로 유지한다. 오픈소스 앱에는 client
secret을 포함하지 않고, OAuth는 Authorization Code + PKCE 기반 공개
클라이언트 흐름을 원칙으로 한다.

## Windows 1차 설치

Windows는 1차 배포 대상이다. PowerShell 실행 정책 때문에 `install.ps1`만
제공하지 않고 `install.bat`으로 감싼다.

```text
install.bat
-> powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```

1차 배포 구성:

- `install.bat`
- `scripts/install.ps1`
- `start-nanobot.bat`
- `README.md`

SmartScreen 경고 대처는 README에 스크린샷 중심으로 안내한다.

## Safe Mode

`safe_mode`는 UI 숨김이 아니라 서버 측 Tool Policy다. CLI는 기존 기능을
유지하고, WebUI 학생/초보자 surface에서는 `tools.safeMode=true`를 강제하는
방향으로 둔다.

초기 차단 대상:

- `exec`
- `write_stdin`
- `list_exec_sessions`
- `apply_patch`
- `write_file`
- `edit_file`
- `run_cli_app`
- `mcp_*`

반복 학습 데이터는 범용 `write_file`을 열지 않고 `student_learning` 전용
도구로만 기록한다.

## 학습 데이터

학습 로그와 복습 큐는 세션 메모리에만 의존하지 않고 구조화된 로컬 파일에
저장한다.

- `study_log.jsonl`
- `review_queue.jsonl`

README에는 다음 원칙을 전면에 둔다.

```text
학습 로그와 복습 큐는 기본적으로 내 컴퓨터에만 저장됩니다.
학생의 질문, 오답, 학습 기록을 별도 서버에 업로드하지 않습니다.
```

단, 모델 답변 생성을 위해 대화 내용 일부는 사용자가 연결한 AI provider로
전송될 수 있음을 함께 안내한다.

## 복습 큐

개념마다 cron job을 만들지 않는다. 반복 학습 선생님 쪽 daily cron 1개가
오늘 만기된 항목만 조회한다.

중복 판단 기준:

```text
subject + concept
```

`date`는 중복 판단 키가 아니라 이력 필드다. 같은 개념이 다시 들어오면 새
항목을 만들지 않고 기존 항목의 `review_history`에 누적한다.

## 언어 설정

설치 중 언어 질문을 추가하지 않는다. WebUI는 저장된 언어 설정이 없으면
브라우저 언어를 감지하고, `ko` 계열이면 한국어를 사용한다. 사용자는 설정에서
나중에 변경할 수 있다.

## 이번 단계 제외

- Discord 웹훅 게시
- 자동 게시
- 게시 대기 UI
- OCR 자료 인덱싱
- 사용액/토큰 상한
- macOS 더블클릭 설치
