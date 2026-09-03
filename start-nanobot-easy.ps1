param()

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DefaultInstallDir = if ($env:NANOBOT_EASY_HOME) { $env:NANOBOT_EASY_HOME } else { Join-Path $env:USERPROFILE "nanobot-easy" }
$ConfigPath = if ($env:NANOBOT_CONFIG) { $env:NANOBOT_CONFIG } else { Join-Path $ScriptDir ".local\config.json" }
$WorkspacePath = if ($env:NANOBOT_WORKSPACE) { $env:NANOBOT_WORKSPACE } else { Join-Path $ScriptDir ".local\workspace" }
$VenvPython = if ($env:NANOBOT_VENV_PYTHON) { $env:NANOBOT_VENV_PYTHON } else { Join-Path $ScriptDir ".venv\Scripts\python.exe" }
$RuntimeDir = if ($env:NANOBOT_RUNTIME_DIR) { $env:NANOBOT_RUNTIME_DIR } else { Join-Path $ScriptDir ".local\run" }
$LogDir = if ($env:NANOBOT_LOG_DIR) { $env:NANOBOT_LOG_DIR } else { Join-Path $ScriptDir ".local\logs" }
$PidFile = if ($env:NANOBOT_PID_FILE) { $env:NANOBOT_PID_FILE } else { Join-Path $RuntimeDir "nanobot-easy-gateway.pid" }
$LogFile = if ($env:NANOBOT_LOG_FILE) { $env:NANOBOT_LOG_FILE } else { Join-Path $LogDir "nanobot-easy-gateway.log" }
$EnvFile = if ($env:NANOBOT_ENV_FILE) { $env:NANOBOT_ENV_FILE } else { Join-Path $ScriptDir ".local\env" }
$WebuiSrcDir = Join-Path $ScriptDir "webui"
$WebuiDistIndex = Join-Path $ScriptDir "nanobot\web\dist\index.html"
$InstallPs1 = Join-Path $ScriptDir "install-nanobot-easy.ps1"

function Write-Info {
    param([string]$Message)
    Write-Host $Message
}

function Write-ErrInfo {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Red
}

if ($ScriptDir -ne $DefaultInstallDir) {
    Write-Host "Notice: recommended nanobot-easy checkout path is $DefaultInstallDir"
    Write-Host "This run will use the current checkout: $ScriptDir"
}

# Same staleness check as install-nanobot-easy.ps1's Ensure-WebuiDist: a
# `git pull` that only updates webui/src would otherwise keep serving a WebUI
# bundle built from the old source, since index.html already exists.
function Test-WebuiDistFresh {
    param([string]$IndexHtml)
    $IndexTime = (Get-Item $IndexHtml).LastWriteTimeUtc
    $PackageJson = Join-Path $WebuiSrcDir "package.json"
    if ((Test-Path $PackageJson) -and (Get-Item $PackageJson).LastWriteTimeUtc -gt $IndexTime) {
        return $false
    }
    $SrcDir = Join-Path $WebuiSrcDir "src"
    if (Test-Path $SrcDir) {
        $Newer = Get-ChildItem -Path $SrcDir -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTimeUtc -gt $IndexTime } |
            Select-Object -First 1
        if ($Newer) {
            return $false
        }
    }
    return $true
}

function Test-PortInUse {
    param([string]$HostName, [int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        $connected = $async.AsyncWaitHandle.WaitOne(500)
        if ($connected -and $client.Connected) {
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-PidOnPort {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($conn) { return $conn.OwningProcess }
    } catch {
        # NetTCPIP module unavailable; caller handles the empty result.
    }
    return $null
}

function Open-BrowserIfAvailable {
    param([string]$Url)
    if ($env:NANOBOT_OPEN_BROWSER -eq "0") { return }
    try { Start-Process $Url | Out-Null } catch {}
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

New-Item -ItemType Directory -Force -Path $RuntimeDir *> $null
New-Item -ItemType Directory -Force -Path $LogDir *> $null
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

if ((-not (Test-Path $WebuiDistIndex)) -or (-not (Test-WebuiDistFresh $WebuiDistIndex))) {
    if (Test-Path $WebuiDistIndex) {
        Write-Host "WebUI source has changed since the last build: $WebuiDistIndex is stale"
    } else {
        Write-Host "WebUI bundle not found: $WebuiDistIndex"
    }
    Write-Host "Running installer to (re)build the WebUI bundle..."
    $env:NANOBOT_FORCE_WEBUI_BUILD = "1"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $InstallPs1 -SkipWizard
    $installExit = $LASTEXITCODE
    Remove-Item Env:\NANOBOT_FORCE_WEBUI_BUILD -ErrorAction SilentlyContinue
    if ($installExit -ne 0) { throw "installer failed" }
}

if (-not (Test-Path $WebuiDistIndex)) {
    Write-ErrInfo "WebUI bundle still not found after install: $WebuiDistIndex"
    exit 1
}

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$ScriptDir;$env:PYTHONPATH" } else { $ScriptDir }

if (-not (Test-Path $ConfigPath)) {
    Write-Host "config not found: $ConfigPath"
    Write-Host "Creating a default config -- finish setup in the browser (WebUI) once nanobot-easy starts."
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ConfigPath) *> $null
    & $VenvPython -m nanobot onboard --config $ConfigPath --workspace $WorkspacePath
    if ($LASTEXITCODE -ne 0) { throw "nanobot onboard failed" }
}

if (-not (Test-Path $ConfigPath)) {
    Write-ErrInfo "config still not found after setup: $ConfigPath"
    exit 1
}

if ($env:NANOBOT_ENSURE_TELEGRAM -ne "0") {
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

if (Test-Path $EnvFile) {
    Import-DotEnvFile $EnvFile
}

# Keep the Vite WebUI dev server pointed at the gateway's WebSocket HTTP API.
# The health endpoint uses gateway.port, but /webui/bootstrap is served by
# channels.websocket.port, so a stale default here causes "bootstrap failed".
$readPortsScript = @'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
gateway = data.get("gateway") or {}
websocket = (data.get("channels") or {}).get("websocket") or {}
print(gateway.get("host") or "127.0.0.1")
print(int(gateway.get("port") or 18790))
print(websocket.get("host") or "127.0.0.1")
print(int(websocket.get("port") or 8765))
'@
$readPortsFile = [System.IO.Path]::GetTempFileName() + ".py"
Set-Content -Path $readPortsFile -Value $readPortsScript -Encoding UTF8
try {
    $portLines = & $VenvPython $readPortsFile $ConfigPath
} finally {
    Remove-Item $readPortsFile -ErrorAction SilentlyContinue
}
$GatewayHost = $portLines[0]
$GatewayPort = [int]$portLines[1]
$WebuiHost = $portLines[2]
$WebuiPort = [int]$portLines[3]

$writeEnvScript = @'
import json
import sys
from pathlib import Path

config_path = Path(sys.argv[1])
env_path = Path(sys.argv[2])
data = json.loads(config_path.read_text(encoding="utf-8"))
websocket = (data.get("channels") or {}).get("websocket") or {}
host = websocket.get("host") or "127.0.0.1"
port = int(websocket.get("port") or 8765)
env_path.parent.mkdir(parents=True, exist_ok=True)
env_path.write_text(f"NANOBOT_API_URL=http://{host}:{port}\n", encoding="utf-8")
print(f"webui env: NANOBOT_API_URL=http://{host}:{port}")
'@
$writeEnvFile = [System.IO.Path]::GetTempFileName() + ".py"
Set-Content -Path $writeEnvFile -Value $writeEnvScript -Encoding UTF8
try {
    & $VenvPython $writeEnvFile $ConfigPath (Join-Path $WebuiSrcDir ".env.local")
} finally {
    Remove-Item $writeEnvFile -ErrorAction SilentlyContinue
}

if (Test-Path $PidFile) {
    $existingPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    $proc = $null
    if ($existingPid) {
        $proc = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
    }
    if ($proc) {
        # A gateway is already running, but if the code has changed since *that*
        # process was launched, it's still running the old Python/WebUI it had
        # in memory at startup -- no amount of rebuilding on disk fixes that
        # without an actual restart. Auto-restart in that case instead of
        # silently leaving the user talking to stale code.
        $pidFileTime = (Get-Item $PidFile).LastWriteTimeUtc
        $staleRunning = $false
        $nanobotDir = Join-Path $ScriptDir "nanobot"
        if (Test-Path $nanobotDir) {
            $newerPy = Get-ChildItem -Path $nanobotDir -Recurse -File -Filter *.py -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTimeUtc -gt $pidFileTime } | Select-Object -First 1
            if ($newerPy) { $staleRunning = $true }
        }
        $srcDir = Join-Path $WebuiSrcDir "src"
        if (-not $staleRunning -and (Test-Path $srcDir)) {
            $newerSrc = Get-ChildItem -Path $srcDir -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTimeUtc -gt $pidFileTime } | Select-Object -First 1
            if ($newerSrc) { $staleRunning = $true }
        }

        if ($staleRunning) {
            Write-Host "nanobot-easy gateway is running (pid=$existingPid) but the code has changed since it started."
            Write-Host "restarting so the update actually takes effect..."
            $stopPs1 = Join-Path $ScriptDir "stop-nanobot-easy.ps1"
            if (Test-Path $stopPs1) {
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stopPs1
            }
        } else {
            Write-Host "nanobot-easy gateway is already running: pid=$existingPid"
            Write-Host "log: $LogFile"
            Write-Host "webui: http://${WebuiHost}:${WebuiPort}/"
            Open-BrowserIfAvailable "http://${WebuiHost}:${WebuiPort}/"
            exit 0
        }
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

# The PID file above says nothing is running, but something may still be
# bound to our ports (e.g. a previous run that was force-killed or left a
# detached child behind). Starting anyway would just crash with a confusing
# "address already in use" error, so check first and fail clearly.
if ((Test-PortInUse $GatewayHost $GatewayPort) -or (Test-PortInUse $WebuiHost $WebuiPort)) {
    Write-ErrInfo "nanobot-easy gateway ports look busy, but no tracked process is running:"
    Write-ErrInfo "  ${GatewayHost}:${GatewayPort} (health) / ${WebuiHost}:${WebuiPort} (websocket)"
    $blockingPid = Get-PidOnPort $GatewayPort
    if (-not $blockingPid) { $blockingPid = Get-PidOnPort $WebuiPort }
    if ($blockingPid) {
        $procName = (Get-Process -Id $blockingPid -ErrorAction SilentlyContinue).ProcessName
        Write-ErrInfo "  likely culprit: pid=$blockingPid ($procName)"
        Write-ErrInfo "  stop it with: Stop-Process -Id $blockingPid"
    } else {
        Write-ErrInfo "  could not identify the process automatically."
        Write-ErrInfo "  find it with: Get-NetTCPConnection -LocalPort $GatewayPort,$WebuiPort"
    }
    Write-ErrInfo "  or set NANOBOT_CONFIG to use different ports in .local\config.json (gateway.port / channels.websocket.port)."
    exit 1
}

$launchScript = @'
import os
import subprocess
import sys
from pathlib import Path

venv_python, config, workspace, log_file, cwd = sys.argv[1:]
Path(log_file).parent.mkdir(parents=True, exist_ok=True)
env = os.environ.copy()
log = open(log_file, "ab", buffering=0)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
proc = subprocess.Popen(
    [venv_python, "-m", "nanobot", "gateway", "--config", config, "--workspace", workspace],
    cwd=cwd,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=subprocess.STDOUT,
    env=env,
    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
    close_fds=True,
)
print(proc.pid)
'@
$launchFile = [System.IO.Path]::GetTempFileName() + ".py"
Set-Content -Path $launchFile -Value $launchScript -Encoding UTF8
try {
    $newPid = & $VenvPython $launchFile $VenvPython $ConfigPath $WorkspacePath $LogFile $ScriptDir
} finally {
    Remove-Item $launchFile -ErrorAction SilentlyContinue
}
$newPid = [int]($newPid | Select-Object -Last 1)
Set-Content -Path $PidFile -Value $newPid -Encoding ASCII

Start-Sleep -Seconds 2
$proc = Get-Process -Id $newPid -ErrorAction SilentlyContinue
if ($proc) {
    $healthUrl = "http://${GatewayHost}:${GatewayPort}/health"
    $webuiUrl = "http://${WebuiHost}:${WebuiPort}/"
    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-Process -Id $newPid -ErrorAction SilentlyContinue)) {
            Write-ErrInfo "nanobot-easy gateway exited during startup. Recent log:"
            if (Test-Path $LogFile) { Get-Content $LogFile -Tail 80 | ForEach-Object { Write-ErrInfo $_ } }
            Remove-Item $PidFile -ErrorAction SilentlyContinue
            exit 1
        }
        try {
            $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 1
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                $ready = $true
                break
            }
        } catch {
            # not ready yet
        }
        Start-Sleep -Milliseconds 500
    }
    if ($ready) {
        Write-Host "nanobot-easy gateway started: pid=$newPid"
        Write-Host "config: $ConfigPath"
        Write-Host "workspace: $WorkspacePath"
        Write-Host "health: $healthUrl"
        Write-Host "webui: $webuiUrl"
        Write-Host "log: $LogFile"
        Open-BrowserIfAvailable $webuiUrl
        exit 0
    }
    Write-Host "nanobot-easy gateway started but health check did not become ready yet: pid=$newPid"
    Write-Host "config: $ConfigPath"
    Write-Host "workspace: $WorkspacePath"
    Write-Host "webui: $webuiUrl"
    Write-Host "log: $LogFile"
} else {
    Write-ErrInfo "nanobot-easy gateway failed to start. Recent log:"
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 80 | ForEach-Object { Write-ErrInfo $_ } }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    exit 1
}
