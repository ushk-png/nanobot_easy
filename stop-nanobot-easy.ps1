param()

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = if ($env:NANOBOT_RUNTIME_DIR) { $env:NANOBOT_RUNTIME_DIR } else { Join-Path $ScriptDir ".local\run" }
$LogDir = if ($env:NANOBOT_LOG_DIR) { $env:NANOBOT_LOG_DIR } else { Join-Path $ScriptDir ".local\logs" }
$PidFile = if ($env:NANOBOT_PID_FILE) { $env:NANOBOT_PID_FILE } else { Join-Path $RuntimeDir "nanobot-easy-gateway.pid" }
$LogFile = if ($env:NANOBOT_LOG_FILE) { $env:NANOBOT_LOG_FILE } else { Join-Path $LogDir "nanobot-easy-gateway.log" }
$ConfigPath = if ($env:NANOBOT_CONFIG) { $env:NANOBOT_CONFIG } else { Join-Path $ScriptDir ".local\config.json" }
$VenvPython = if ($env:NANOBOT_VENV_PYTHON) { $env:NANOBOT_VENV_PYTHON } else { Join-Path $ScriptDir ".venv\Scripts\python.exe" }

function Test-PortInUse {
    param([string]$HostName, [int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        $connected = $async.AsyncWaitHandle.WaitOne(500)
        return ($connected -and $client.Connected)
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

function Get-PortsFromConfig {
    if (-not (Test-Path $ConfigPath) -or -not (Test-Path $VenvPython)) { return @() }
    $script = @'
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(0)
gateway = data.get("gateway") or {}
websocket = (data.get("channels") or {}).get("websocket") or {}
print(gateway.get("host") or "127.0.0.1")
print(int(gateway.get("port") or 18790))
print(websocket.get("host") or "127.0.0.1")
print(int(websocket.get("port") or 8765))
'@
    $tmpFile = [System.IO.Path]::GetTempFileName() + ".py"
    Set-Content -Path $tmpFile -Value $script -Encoding UTF8
    try {
        $lines = & $VenvPython $tmpFile $ConfigPath
    } finally {
        Remove-Item $tmpFile -ErrorAction SilentlyContinue
    }
    if (-not $lines -or $lines.Count -lt 4) { return @() }
    return @(
        @{ HostName = $lines[0]; Port = [int]$lines[1] },
        @{ HostName = $lines[2]; Port = [int]$lines[3] }
    )
}

function Stop-ByPortFallback {
    $found = $false
    foreach ($target in (Get-PortsFromConfig)) {
        if (Test-PortInUse $target.HostName $target.Port) {
            $found = $true
            $blockingPid = Get-PidOnPort $target.Port
            if ($blockingPid) {
                $procName = (Get-Process -Id $blockingPid -ErrorAction SilentlyContinue).ProcessName
                Write-Host "found an untracked process on $($target.HostName):$($target.Port): pid=$blockingPid ($procName)"
                Write-Host "stopping it: pid=$blockingPid"
                try { Stop-Process -Id $blockingPid -Force -ErrorAction Stop } catch {}
            } else {
                Write-Host "something is listening on $($target.HostName):$($target.Port) but its pid could not be determined."
                Write-Host "find it manually with: Get-NetTCPConnection -LocalPort $($target.Port)"
            }
        }
    }
    return $found
}

function Test-IsAncestorPid {
    param([int]$Needle)
    $currentPid = $PID
    while ($currentPid -and $currentPid -ne 0) {
        if ($currentPid -eq $Needle) { return $true }
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $currentPid" -ErrorAction SilentlyContinue
        if (-not $proc -or -not $proc.ParentProcessId) { break }
        $currentPid = $proc.ParentProcessId
    }
    return $false
}

if (-not (Test-Path $PidFile)) {
    Write-Host "nanobot-easy gateway is not running: pid file not found"
    if (Stop-ByPortFallback) { exit 0 }
    Write-Host "nothing found on the configured ports either."
    exit 0
}

$existingPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
if (-not $existingPid) {
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Host "nanobot-easy gateway is not running: empty pid file removed"
    if (Stop-ByPortFallback) { exit 0 }
    Write-Host "nothing found on the configured ports either."
    exit 0
}
$existingPid = [int]$existingPid

$proc = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
if (-not $proc) {
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Host "nanobot-easy gateway is not running: stale pid file removed"
    if (Stop-ByPortFallback) { exit 0 }
    Write-Host "nothing found on the configured ports either."
    exit 0
}

if ($env:NANOBOT_ALLOW_SELF_STOP -ne "1" -and (Test-IsAncestorPid $existingPid)) {
    Write-Host "refusing to stop nanobot-easy gateway from inside its own process tree" -ForegroundColor Red
    Write-Host "set NANOBOT_ALLOW_SELF_STOP=1 if you really intend to stop it from here" -ForegroundColor Red
    exit 2
}

Write-Host "stopping nanobot-easy gateway: pid=$existingPid"
try { Stop-Process -Id $existingPid -ErrorAction Stop } catch {}

$stopped = $false
for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        $stopped = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

if (-not $stopped) {
    Write-Host "gateway did not stop after Stop-Process; sending Force kill: pid=$existingPid" -ForegroundColor Red
    try { Stop-Process -Id $existingPid -Force -ErrorAction Stop } catch {}
}

Remove-Item $PidFile -ErrorAction SilentlyContinue
Write-Host "nanobot-easy gateway stopped"
Write-Host "log: $LogFile"
