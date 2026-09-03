# One-file installer for nanobot-easy (Windows).
#
# Intended usage:
#   irm https://raw.githubusercontent.com/ushk-png/nanobot_easy/main/bootstrap.ps1 | iex
# or double-click bootstrap.bat next to this file (existing checkout), or run
# this script directly with PowerShell.
#
# It installs missing prerequisites (best-effort via winget), clones or
# updates the nanobot-easy repository, then hands off to the existing
# install-nanobot-easy.ps1 / start-nanobot-easy.ps1 scripts. It does not
# reimplement any of their logic.

$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:NANOBOT_EASY_REPO_URL) { $env:NANOBOT_EASY_REPO_URL } else { "https://github.com/ushk-png/nanobot_easy.git" }
$DefaultTargetDir = if ($env:NANOBOT_EASY_HOME) { $env:NANOBOT_EASY_HOME } else { Join-Path $env:USERPROFILE "nanobot-easy" }

function Write-Info { param([string]$Message) Write-Host $Message }
function Write-ErrInfo { param([string]$Message) Write-Host $Message -ForegroundColor Red }

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-Python311 {
    param([string]$Command)
    try {
        & $Command -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-Python311 {
    foreach ($candidate in @("python", "py", "python3")) {
        if ((Test-Command $candidate) -and (Test-Python311 $candidate)) {
            return $candidate
        }
    }
    return $null
}

function Update-PathFromRegistry {
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Install-WingetPackage {
    param([string]$Id)
    if (-not (Test-Command "winget")) { return $false }
    Write-Info "Installing $Id with winget..."
    winget install --id $Id -e --source winget --accept-package-agreements --accept-source-agreements
    return $LASTEXITCODE -eq 0
}

function Ensure-Dependencies {
    $needGit = -not (Test-Command "git")
    $needPython = -not (Find-Python311)
    $needNode = -not ((Test-Command "bun") -or (Test-Command "npm"))

    if (-not ($needGit -or $needPython -or $needNode)) {
        Write-Info "Prerequisites found: git, Python 3.11+, Node.js/npm (or Bun)."
        return $true
    }

    Write-Info "Some prerequisites are missing; attempting to install them with winget..."
    if (-not (Test-Command "winget")) {
        Write-ErrInfo "winget was not found. Install prerequisites manually, then rerun this script:"
        Write-ErrInfo "  git:    https://git-scm.com/download/win"
        Write-ErrInfo "  python: https://www.python.org/downloads/  (enable 'Add python.exe to PATH')"
        Write-ErrInfo "  node:   https://nodejs.org/  (LTS)"
        return $false
    }

    if ($needGit) { Install-WingetPackage "Git.Git" | Out-Null }
    if ($needPython) { Install-WingetPackage "Python.Python.3.12" | Out-Null }
    if ($needNode) { Install-WingetPackage "OpenJS.NodeJS.LTS" | Out-Null }

    Update-PathFromRegistry

    $needGit = -not (Test-Command "git")
    $needPython = -not (Find-Python311)
    $needNode = -not ((Test-Command "bun") -or (Test-Command "npm"))

    if ($needGit -or $needPython -or $needNode) {
        Write-ErrInfo "Some prerequisites could not be installed automatically. Install them manually, then rerun this script (a new terminal window may be required for PATH changes to apply):"
        if ($needGit) { Write-ErrInfo "  git:    winget install --id Git.Git -e   OR   https://git-scm.com/download/win" }
        if ($needPython) { Write-ErrInfo "  python: winget install --id Python.Python.3.12 -e   OR   https://www.python.org/downloads/ (enable 'Add python.exe to PATH')" }
        if ($needNode) { Write-ErrInfo "  node:   winget install --id OpenJS.NodeJS.LTS -e   OR   https://nodejs.org/ (LTS)" }
        return $false
    }

    return $true
}

function Resolve-TargetDir {
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml"))) {
        $content = Get-Content (Join-Path $PSScriptRoot "pyproject.toml") -Raw -ErrorAction SilentlyContinue
        if ($content -match "nanobot") {
            return $PSScriptRoot
        }
    }
    return $DefaultTargetDir
}

# These run native commands (git, powershell.exe) whose stdout lands in the
# success stream. Returning $true/$false from them would make the caller see
# an array of output lines plus the boolean -- always truthy -- so a failure
# would be silently ignored. They signal failure by throwing instead.
function Invoke-CloneOrUpdate {
    param([string]$TargetDir)
    if (Test-Path (Join-Path $TargetDir ".git")) {
        Write-Info "Existing checkout found at $TargetDir; updating..."
        Push-Location $TargetDir
        try {
            git pull --ff-only
            if ($LASTEXITCODE -ne 0) {
                Write-ErrInfo "git pull --ff-only failed (local changes or diverged history?). Continuing with the checkout as-is."
            }
        } finally {
            Pop-Location
        }
        return
    }
    if (Test-Path $TargetDir) {
        throw "$TargetDir already exists and is not a git checkout. Move it aside or set NANOBOT_EASY_HOME to another path."
    }
    Write-Info "Cloning nanobot-easy into $TargetDir..."
    git clone $RepoUrl $TargetDir
    if ($LASTEXITCODE -ne 0) {
        throw "git clone failed."
    }
}

function Invoke-ChildScript {
    param([string]$Path)
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Path
    if ($LASTEXITCODE -ne 0) {
        throw "$(Split-Path -Leaf $Path) failed."
    }
}

function Test-GatewayRunning {
    param([string]$TargetDir)
    $pidFile = Join-Path $TargetDir ".local\run\nanobot-easy-gateway.pid"
    if (-not (Test-Path $pidFile)) { return $false }
    $trackedPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $trackedPid) { return $false }
    return [bool](Get-Process -Id ([int]$trackedPid) -ErrorAction SilentlyContinue)
}

if (-not (Ensure-Dependencies)) { exit 1 }

try {
    $TargetDir = Resolve-TargetDir
    Invoke-CloneOrUpdate $TargetDir
    Set-Location $TargetDir

    $InstallPs1 = Join-Path $TargetDir "install-nanobot-easy.ps1"
    $StartPs1 = Join-Path $TargetDir "start-nanobot-easy.ps1"

    if (-not (Test-Path $InstallPs1)) { throw "install-nanobot-easy.ps1 not found in $TargetDir" }
    if (-not (Test-Path $StartPs1)) { throw "start-nanobot-easy.ps1 not found in $TargetDir" }

    # Already up? Then this is a "just open the UI again" run: skip the
    # install pass entirely. start-nanobot-easy.ps1 still rebuilds the WebUI
    # and restarts by itself if the code turns out to be stale.
    if (Test-GatewayRunning $TargetDir) {
        Write-Info "nanobot-easy is already running; skipping install and reopening the browser."
    } else {
        Invoke-ChildScript $InstallPs1
    }
    Invoke-ChildScript $StartPs1
} catch {
    Write-ErrInfo "Error: $($_.Exception.Message)"
    exit 1
}
