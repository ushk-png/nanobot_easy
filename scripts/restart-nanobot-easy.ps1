param()

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$ConfigPath = if ($env:NANOBOT_CONFIG) { $env:NANOBOT_CONFIG } else { Join-Path $RepoRoot ".local\config.json" }
$WorkspacePath = if ($env:NANOBOT_WORKSPACE) { $env:NANOBOT_WORKSPACE } else { Join-Path $RepoRoot ".local\workspace" }
$VenvPython = if ($env:NANOBOT_VENV_PYTHON) { $env:NANOBOT_VENV_PYTHON } else { Join-Path $RepoRoot ".venv\Scripts\python.exe" }

# The self-restart safety check (detached restart when called from inside the
# gateway's own process tree) lives in `nanobot restart` (nanobot/cli/up.py).

if (-not (Test-Path $VenvPython)) {
    Write-Host "nanobot venv python not found: $VenvPython" -ForegroundColor Red
    exit 1
}

Set-Location $RepoRoot
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$RepoRoot;$env:PYTHONPATH" } else { $RepoRoot }

& $VenvPython -m nanobot restart --config $ConfigPath --workspace $WorkspacePath
exit $LASTEXITCODE
