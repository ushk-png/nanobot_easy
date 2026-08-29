# nanobot-easy 설치 및 사용 안내서

이 문서는 GitHub의 `ushk-png/nanobot-easy` 저장소를 처음 내려받은 사용자가 Linux, macOS, Windows에서 설치하고 실행하는 방법을 정리합니다.

## 빠른 시작

### Linux / Ubuntu

```bash
git clone https://github.com/ushk-png/nanobot-easy.git && cd nanobot-easy && ./install-nanobot-easy.sh
```

실행:

```bash
./start-nanobot-easy.sh
```

Ubuntu/Debian에서 `python3 -m venv`가 동작하지 않으면 설치 스크립트가 `python3-venv`와 `python3-pip` 설치를 시도합니다. WebUI 번들 빌드를 위해 Node.js/npm 또는 Bun도 필요합니다. `sudo`가 없거나 자동 설치가 막힌 환경에서는 먼저 아래를 실행하세요.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl nodejs npm
```

### macOS

```bash
git clone https://github.com/ushk-png/nanobot-easy.git && cd nanobot-easy && ./install-nanobot-easy.sh
```

실행:

```bash
./start-nanobot-easy.sh
```

Python 3.11 이상이나 Node.js/npm이 없다면 Homebrew 기준으로 `brew install python node`를 실행한 뒤 다시 실행하세요. Finder에서 더블클릭으로 실행하려면 `install-nanobot-easy.command`와 `start-nanobot-easy.command`를 사용하세요.

### Windows

PowerShell에서:

```powershell
git clone https://github.com/ushk-png/nanobot-easy.git; cd nanobot-easy; .\install.bat
```

실행:

```powershell
.\start-nanobot.bat
```

Python이 없다면 python.org에서 Python 3.11 이상을 설치하고, 설치 화면에서 **Add python.exe to PATH**를 선택한 뒤 PowerShell을 새로 열어 다시 실행하세요. Node.js/npm이 없다면 nodejs.org에서 LTS 버전을 설치하세요.

## 설치 스크립트가 하는 일

Linux/macOS의 `install-nanobot-easy.sh`와 Windows의 `install-nanobot-easy.ps1`은 같은 흐름을 수행합니다.

1. Python 3.11 이상을 찾습니다.
2. 저장소 내부에 `.venv`를 만들거나 기존 `.venv`를 재사용합니다.
3. venv에 pip를 준비하고 이 저장소를 editable mode로 설치합니다.
4. editable install에서는 Python 빌드 훅이 WebUI 번들을 만들지 않으므로, `bun` 또는 `npm`으로 `nanobot/web/dist`를 직접 빌드합니다.
5. `.local/config.json`이 없으면 첫 설정 wizard를 실행합니다.
6. `.local/workspace`를 준비합니다.

기본 설치 extras는 `telegram,documents`입니다. 필요하면 환경변수로 바꿀 수 있습니다.

```bash
NANOBOT_SKILL_EXTRAS=telegram,documents,pdf ./install-nanobot-easy.sh
```

Windows PowerShell:

```powershell
$env:NANOBOT_SKILL_EXTRAS="telegram,documents,pdf"
.\install.bat
```

## 실행 스크립트가 하는 일

Linux/macOS:

```bash
./start-nanobot-easy.sh
```

Windows:

```powershell
.\start-nanobot.bat
```

실행 스크립트는 `.venv`가 없거나 WebUI 번들이 없으면 설치 스크립트를 먼저 실행하고, `.local/config.json`이 없으면 설정 wizard를 실행합니다. 따라서 fresh clone 이후에도 실행 스크립트 하나로 설치 누락을 복구할 수 있습니다.

Linux/macOS의 `start-nanobot-easy.sh`는 gateway를 background process로 띄우고 다음 정보를 출력합니다.

- 설정 파일: `.local/config.json`
- workspace: `.local/workspace`
- health endpoint: 기본 `http://127.0.0.1:18790/health`
- WebUI: 기본 `http://127.0.0.1:8765/`
- 로그: `.local/logs/nanobot-easy-gateway.log`

Windows의 `start-nanobot.bat`은 repo-local 설정으로 `nanobot webui --background`를 실행합니다.

## 설정 파일과 개인정보

GitHub 저장소에는 개인 설정과 토큰이 포함되지 않습니다. 처음 실행할 때 wizard가 아래 경로를 만듭니다.

- 설정 파일: `.local/config.json`
- workspace: `.local/workspace`

API key, Telegram bot token, allow list 같은 개인정보/인증정보는 각 사용자가 직접 입력해야 합니다. 필요하면 `.local/env`에 환경변수를 저장하고 실행 전에 불러오도록 구성할 수 있습니다. `.local/`은 gitignore 대상입니다.

## 수동 진단 명령

설치 확인:

```bash
PYTHONPATH=. .venv/bin/nanobot --version
```

상태 확인:

```bash
PYTHONPATH=. .venv/bin/nanobot status \
  --config .local/config.json \
  --workspace .local/workspace
```

모델 응답 확인:

```bash
PYTHONPATH=. .venv/bin/nanobot agent \
  --config .local/config.json \
  --workspace .local/workspace \
  -m "짧게 자기소개해줘"
```

채널 상태 확인:

```bash
PYTHONPATH=. .venv/bin/nanobot channels status \
  --config .local/config.json
```

Windows에서는 `.venv\Scripts\python.exe -m nanobot ...` 형태로 실행하면 됩니다.

## 정지와 재시작

Linux/macOS:

```bash
./stop-nanobot-easy.sh
./restart-nanobot-easy.sh
```

로그 확인:

```bash
tail -f .local/logs/nanobot-easy-gateway.log
```

## 스킬 구조

나노봇 스킬은 크게 두 종류로 나뉩니다.

- 일반 스킬: `nanobot/skills/*/SKILL.md`
- 시스템 스킬: `nanobot/skills-system/*/SKILL.md`

일반 스킬은 에이전트가 사용자 요청을 처리할 때 검색하고 선택할 수 있는 작업 지식입니다. 시스템 스킬은 스킬 조합, 리뷰, 라우팅, 초안 생성처럼 스킬 시스템 자체를 운영하기 위한 내부 스킬입니다.

스킬을 추가하거나 수정한 뒤에는 workspace의 SQLite 스킬 레지스트리를 다시 색인합니다.

```bash
PYTHONPATH=. .venv/bin/nanobot skill reindex \
  --config .local/config.json \
  --workspace .local/workspace
```

목록 확인:

```bash
PYTHONPATH=. .venv/bin/nanobot skill list \
  --config .local/config.json \
  --workspace .local/workspace
```
