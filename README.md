# nanobot-easy

UNDER CONSTRUCTION!!! 

**믿고 맡길 수 있는 개인용 AI 에이전트.**

에이전트에게 일을 맡길 때 늘 걸리는 세 가지가 있습니다. *제대로 알아들었을까, 엉뚱한 짓을 하진 않을까, 지난번에 말한 걸 기억할까.* nanobot-easy은 이 세 가지를 **감이 아니라 확인 가능한 형태로** 해결하려는 프로젝트입니다.

[nanobot](https://github.com/HKUDS/nanobot)(홍콩대 HKUDS)에서 분기해 독립적으로 발전시키고 있습니다. 원본이 "가볍고 다재다능한 에이전트"를 지향한다면, 이쪽은 **좁고 깊게 — 정확하고, 안전하고, 초보자도 쓸 수 있게**를 지향합니다.

---

## 바로 설치하고 실행하기

파일 하나만 받아서 실행하면 나머지는 알아서 됩니다: 필요한 프로그램을 찾아 설치하고, 저장소를 받고, `.venv`를 만들고, WebUI를 빌드하고, 게이트웨이를 띄우고, 브라우저를 엽니다. 브라우저에 뜨는 첫 화면에서 바로 LLM을 연결할 수 있습니다.

### macOS

`bootstrap.command` 파일을 받아서 Finder에서 더블클릭하세요. (저장소를 이미 받았다면 저장소 안의 `bootstrap.command`를 더블클릭해도 됩니다.)

### Windows

`bootstrap.bat` 파일을 받아서 탐색기에서 더블클릭하세요.

### Linux

`bootstrap.sh`를 받아서 더블클릭하세요. 배포판/파일관리자에 따라 더블클릭이 안 통하면 터미널에서 실행하세요.

```bash
./bootstrap.sh
```

세 플랫폼 모두 Python 3.11 이상, Git, Node.js/npm(또는 Bun)이 없으면 최대한 자동으로 설치를 시도하고, 안 되면 정확히 무엇을 설치해야 하는지 알려줍니다. 이미 설치되어 있고 게이트웨이가 떠 있는 상태에서 다시 실행하면 재설치 없이 브라우저만 다시 엽니다.

> 원본 `nanobot-ai`와 같은 환경에 동시 설치하지 마세요. 이 저장소는 repo-local `.venv`를 사용해 분리 실행하는 것을 기본으로 합니다.

### 터미널에서 한 줄로

더블클릭 파일을 받기 번거로운 분들을 위한 대안입니다. 동작은 위와 동일합니다.

```bash
curl -fsSL https://raw.githubusercontent.com/ushk-png/nanobot_easy/main/bootstrap.sh | bash    # Linux/macOS
```

```powershell
irm https://raw.githubusercontent.com/ushk-png/nanobot_easy/main/bootstrap.ps1 | iex           # Windows
```

### 직접 하나씩 실행하고 싶다면

저장소를 직접 클론하고 설치·실행 스크립트를 따로 부르고 싶은 분들을 위한 방법입니다.

**Linux / macOS**

```bash
git clone https://github.com/ushk-png/nanobot_easy.git "$HOME/nanobot-easy" && cd "$HOME/nanobot-easy" && ./scripts/install-nanobot-easy.sh
./start-nanobot-easy.sh
```

Ubuntu에서 Python venv 패키지가 아예 없거나 `sudo`를 사용할 수 없는 환경이면 먼저 아래를 실행한 뒤 다시 설치하세요.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl
```

macOS에 Python 3.11 이상이나 Node.js/npm이 없다면 Homebrew 기준으로 `brew install python node`를 실행한 뒤 다시 실행하세요.

**Windows**

```powershell
git clone https://github.com/ushk-png/nanobot_easy.git "$env:USERPROFILE\nanobot-easy"; cd "$env:USERPROFILE\nanobot-easy"; .\scripts\install.bat
.\start-nanobot.bat
```

Windows 설치 파일은 `.venv`가 없으면 생성하고, WebUI 번들이 없거나 오래됐으면 Node.js/npm 또는 Bun으로 (재)빌드하며, `.local\config.json`이 없으면 기본 설정만 만들고 첫 설정은 브라우저(WebUI)에서 진행합니다. Python이 없으면 python.org에서 Python 3.11 이상을 설치하고, 설치 화면에서 **Add python.exe to PATH**를 켠 뒤 다시 실행하세요. Node.js/npm이 없으면 nodejs.org에서 LTS 버전을 설치한 뒤 다시 실행하세요.

---

## 이런 분에게 맞습니다

- 에이전트에 나만의 작업 방식을 계속 가르치고 싶은 분
- AI가 파일을 지우거나 메일을 보내는 게 불안해서 자동화를 못 맡기고 있던 분
- 대화 때마다 배경 설명을 다시 하는 게 지겨운 분
- 개발자가 아닌 가족·학생에게 로컬 AI를 쥐여주고 싶은 분
- WebUI에서 연결, 내장 Agent Tools, 에이전트 관리, 자동화를 한곳에서 다루고 싶은 분

## 이런 분에게는 원본이 낫습니다

- 카카오톡·디스코드 등 다양한 채널 연동이 주 목적인 경우
- 최신 모델·기능을 가장 빨리 받아보고 싶은 경우
- 터미널 TUI 중심으로 쓰는 경우

---

## 무엇이 좋아지나

### 시키는 대로 알아듣습니다 — 그리고 그걸 증명할 수 있습니다

에이전트에 작업 방식(스킬)을 하나둘 추가하다 보면 반드시 겪는 일이 있습니다. **새 스킬을 넣었더니 멀쩡하던 게 엉뚱하게 반응하기 시작하는 것.** 스킬이 서로 트리거를 뺏기 때문인데, 보통은 눈치채지도 못한 채 품질이 무너집니다.

nanobot-easy에서는 스킬마다 "이런 요청은 이 스킬이 잡아야 한다"는 판정 기준이 함께 저장됩니다. 그래서 스킬을 추가할 때마다 **기존 스킬이 여전히 제대로 잡히는지 자동으로 회귀 검증**됩니다. 어떤 스킬이 얼마나 자주 불리는지, 성공률은 얼마인지도 명령 한 줄로 확인할 수 있습니다.

### 스킬을 직접 쓰지 않아도 됩니다

보통 에이전트에 새 작업 방식을 가르치려면 설정 파일을 손으로 씁니다. 잘 쓰기도 어렵고, 잘못 쓰면 앞의 문제가 터집니다.

여기서는 **필요한 걸 말로 설명하면 초안이 만들어지고**, 그 초안을 다른 스킬들이 검토합니다. 설계가 타당한지, 위험한 권한을 요구하진 않는지, 기존 것과 겹치진 않는지, 트리거가 충돌하진 않는지를 자동으로 걸러냅니다. 사람은 마지막에 채팅창에서 승인만 하면 됩니다.

### 엉뚱한 짓을 하지 않습니다

자동화를 못 맡기는 진짜 이유는 성능이 아니라 불안입니다. 이 프로젝트는 거기에 실제 장치를 걸었습니다.

- 파일 삭제, 메일 발송처럼 되돌릴 수 없는 작업은 의도가 명시되지 않으면 실행 자체가 막힙니다.
- 외부에서 가져온 스킬은 승인 전까지 동작하지 않고, 허용한 출처가 아니면 설치되지 않습니다.
- 에이전트가 다른 에이전트를 부르는 깊이에 상한이 있어, 위임이 무한히 번지지 않습니다.

### 대화가 쌓일수록 똑똑해집니다

지난주에 말한 프로젝트 이름, 팀원 호칭, 내가 쓰는 약어를 매번 다시 설명하지 않아도 됩니다. 대화를 사건 단위로 저장하고 필요할 때 꺼내 씁니다. 민감한 내용은 저장 단계에서 걸러지고, 기록은 전부 내 컴퓨터에 남습니다.

### 개발자가 아니어도 켜서 씁니다

파일 하나(macOS `bootstrap.command`, Windows `bootstrap.bat`, Linux `bootstrap.sh`)를 받아서 실행하면 설치부터 브라우저 온보딩까지 한 번에 끝납니다. 학습 모드를 켜면 코치 역할로 동작하면서 복습 큐를 관리해 주고, 위험한 도구는 처음부터 잠깁니다.

---

## 이런 일을 맡길 수 있습니다

- 반복 업무 표준화 — 회의록, 보고서, 리뷰 형식을 스킬로 고정
- 의사결정 보조 — 선택지 비교, 장단점 정리, 진단형 질문 응답
- 개발 보조 — 코드 리뷰, 디버깅 절차 안내, 에러 메시지 해설
- 조사 업무 — 출처가 붙은 리서치 브리프
- 학습 관리 — 간격 반복 복습, 개념 인출 훈련

---

## 원본과의 관계

원본 nanobot에서 2026년 7월 분기한 뒤 독립적으로 운영합니다. 기능을 그대로 따라가지 않습니다. 넓은 채널 연동이나 최신 기능이 필요하다면 [원본](https://github.com/HKUDS/nanobot)이 더 나은 선택입니다.

처음 쓰신다면 [한국어 스킬 사용 가이드](./docs/nanobot-easy-usage-ko.md)부터 보시길 권합니다. 브라우저 화면은 [WebUI 안내](./docs/webui.md), 설정은 [configuration.md](./docs/configuration.md), 학습자용 배포는 [학생 모드 안내](./docs/student-mode-distribution-ko.md)를 참고하세요.

## 기여

새 스킬은 초안 생성 → 자동 검토 → 판정 기준 첨부 → 승인 순서로 받습니다. 자세한 절차는 [기여 가이드](./docs/design/skill-writing-guide.md)에 있습니다.

## 라이선스

MIT. [nanobot](https://github.com/HKUDS/nanobot) (Copyright © 2025-present Xubin Ren and the nanobot contributors)에서 파생했으며 원 저작권 표기를 유지합니다. 본 프로젝트는 HKUDS와 무관하게 운영됩니다.
