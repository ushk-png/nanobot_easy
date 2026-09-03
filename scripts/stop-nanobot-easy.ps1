param()

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$ConfigPath = if ($env:NANOBOT_CONFIG) { $env:NANOBOT_CONFIG } else { Join-Path $RepoRoot ".local\config.json" }
$WorkspacePath = if ($env:NANOBOT_WORKSPACE) { $env:NANOBOT_WORKSPACE } else { Join-Path $RepoRoot ".local\workspace" }
$VenvPython = if ($env:NANOBOT_VENV_PYTHON) { $env:NANOBOT_VENV_PYTHON } else { Join-Path $RepoRoot ".venv\Scripts\python.exe" }

# The actual stop logic (tracked-process termination, ancestor-process
# self-stop guard, port-based fallback when the state file is missing/stale)
# lives in `nanobot down` (nanobot/cli/up.py) so it's shared with
# Linux/macOS and unit-testable.

if (-not (Test-Path $VenvPython)) {
    Write-Host "nanobot venv python not found: $VenvPython"
    Write-Host "nanobot-easy gateway is not running (nothing was ever installed here)."
    exit 0
}

Set-Location $RepoRoot
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$RepoRoot;$env:PYTHONPATH" } else { $RepoRoot }

& $VenvPython -m nanobot down --config $ConfigPath --workspace $WorkspacePath
exit $LASTEXITCODE
