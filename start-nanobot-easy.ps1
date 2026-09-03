param()

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DefaultInstallDir = if ($env:NANOBOT_EASY_HOME) { $env:NANOBOT_EASY_HOME } else { Join-Path $env:USERPROFILE "nanobot-easy" }
$ConfigPath = if ($env:NANOBOT_CONFIG) { $env:NANOBOT_CONFIG } else { Join-Path $ScriptDir ".local\config.json" }
$WorkspacePath = if ($env:NANOBOT_WORKSPACE) { $env:NANOBOT_WORKSPACE } else { Join-Path $ScriptDir ".local\workspace" }
$VenvPython = if ($env:NANOBOT_VENV_PYTHON) { $env:NANOBOT_VENV_PYTHON } else { Join-Path $ScriptDir ".venv\Scripts\python.exe" }
$EnvFile = if ($env:NANOBOT_ENV_FILE) { $env:NANOBOT_ENV_FILE } else { Join-Path $ScriptDir ".local\env" }
$InstallPs1 = Join-Path $ScriptDir "scripts\install-nanobot-easy.ps1"

# The actual launcher logic (config bootstrap, WebUI freshness rebuild,
# staleness-based restart, port-conflict detection, gateway start, browser
# open) lives in `nanobot up` (nanobot/cli/up.py) so it's shared with
# Linux/macOS and unit-testable. This script only does what has to happen
# before a working Python environment exists.

function Write-ErrInfo {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Red
}

function Import-DotEnvFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $idx = $trimmed.IndexOf("=")
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $value = $trimmed.Substring($idx + 1).Trim().Trim('"')
        Set-Item -Path "Env:$key" -Value $value
    }
}

if ($ScriptDir -ne $DefaultInstallDir) {
    Write-Host "Notice: recommended nanobot-easy checkout path is $DefaultInstallDir"
    Write-Host "This run will use the current checkout: $ScriptDir"
}

New-Item -ItemType Directory -Force -Path $WorkspacePath *> $null

if (-not (Test-Path $VenvPython)) {
    Write-Host "nanobot venv python not found: $VenvPython"
    Write-Host "Running repo-local installer first..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $InstallPs1
    if ($LASTEXITCODE -ne 0) { throw "installer failed" }
}

if (-not (Test-Path $VenvPython)) {
    Write-ErrInfo "nanobot venv python still not found after install: $VenvPython"
    exit 1
}

if ($env:NANOBOT_ENSURE_TELEGRAM -ne "0" -and (Test-Path $ConfigPath)) {
    $telegramGuard = @'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
channels = data.setdefault("channels", {})
telegram = channels.setdefault("telegram", {})
token = str(telegram.get("token") or "").strip()
if not token:
    print("telegram channel guard: token is empty; Telegram will not connect", file=sys.stderr)
elif telegram.get("enabled") is not True:
    telegram["enabled"] = True
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("telegram channel guard: enabled channels.telegram because a bot token is configured")
else:
    print("telegram channel guard: enabled")
'@
    $telegramGuardFile = [System.IO.Path]::GetTempFileName() + ".py"
    Set-Content -Path $telegramGuardFile -Value $telegramGuard -Encoding UTF8
    try {
        & $VenvPython $telegramGuardFile $ConfigPath
    } finally {
        Remove-Item $telegramGuardFile -ErrorAction SilentlyContinue
    }
}

Set-Location $ScriptDir
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$ScriptDir;$env:PYTHONPATH" } else { $ScriptDir }

if (Test-Path $EnvFile) {
    Import-DotEnvFile $EnvFile
}

& $VenvPython -m nanobot up --config $ConfigPath --workspace $WorkspacePath
exit $LASTEXITCODE
