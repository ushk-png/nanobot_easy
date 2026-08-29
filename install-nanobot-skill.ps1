param(
    [switch]$DryRun,
    [switch]$SkipWizard
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = if ($env:NANOBOT_SKILL_VENV) { $env:NANOBOT_SKILL_VENV } else { Join-Path $ScriptDir ".venv" }
$ConfigPath = if ($env:NANOBOT_CONFIG) { $env:NANOBOT_CONFIG } else { Join-Path $ScriptDir ".local\config.json" }
$WorkspacePath = if ($env:NANOBOT_WORKSPACE) { $env:NANOBOT_WORKSPACE } else { Join-Path $ScriptDir ".local\workspace" }
$WebuiDir = Join-Path $ScriptDir "webui"
$WebuiDist = Join-Path $ScriptDir "nanobot\web\dist"
$Extras = if ($env:NANOBOT_SKILL_EXTRAS) { $env:NANOBOT_SKILL_EXTRAS } else { "telegram,documents" }

function Write-Info {
    param([string]$Message)
    Write-Host $Message
}

function Fail {
    param([string]$Message)
    throw "Error: $Message"
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

function Find-Python {
    if ($env:PYTHON) {
        if ((Get-Command $env:PYTHON -ErrorAction SilentlyContinue) -and (Test-Python311 $env:PYTHON)) {
            return $env:PYTHON
        }
        Fail "PYTHON=$env:PYTHON was not found or is older than Python 3.11."
    }

    foreach ($Candidate in @("python", "py", "python3")) {
        if ((Get-Command $Candidate -ErrorAction SilentlyContinue) -and (Test-Python311 $Candidate)) {
            return $Candidate
        }
    }

    Fail "Python 3.11 or newer was not found. Install Python from https://www.python.org/downloads/ and enable 'Add python.exe to PATH', then rerun install.bat."
}

function New-NanobotVenv {
    param([string]$Python)
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (Test-Path $VenvPython) {
        Write-Info "Using existing virtual environment: $VenvDir"
        return $VenvPython
    }

    Write-Info "Creating virtual environment: $VenvDir"
    $Parent = Split-Path -Parent $VenvDir
    if ($Parent) {
        New-Item -ItemType Directory -Force -Path $Parent *> $null
    }

    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        Fail "Could not create a virtual environment. Reinstall Python with the venv module and pip enabled, then rerun install.bat."
    }
    return $VenvPython
}

function Find-WebuiRunner {
    foreach ($Candidate in @("bun", "npm")) {
        if (Get-Command $Candidate -ErrorAction SilentlyContinue) {
            return $Candidate
        }
    }
    return $null
}

function Ensure-WebuiDist {
    $IndexHtml = Join-Path $WebuiDist "index.html"
    if ((Test-Path $IndexHtml) -and $env:NANOBOT_FORCE_WEBUI_BUILD -ne "1") {
        Write-Info "Using existing WebUI build: $WebuiDist"
        return
    }

    if (-not (Test-Path (Join-Path $WebuiDir "package.json"))) {
        Fail "webui\package.json was not found; cannot build WebUI bundle."
    }

    $Runner = Find-WebuiRunner
    if (-not $Runner) {
        Fail "WebUI build requires Bun or Node.js/npm because editable Python installs do not run the packaged WebUI build hook. Install Node.js from https://nodejs.org/ or Bun from https://bun.sh/docs/installation, then rerun install.bat."
    }

    Write-Info "Building WebUI bundle with $Runner..."
    Push-Location $WebuiDir
    try {
        if ($Runner -eq "bun") {
            & bun install
            if ($LASTEXITCODE -ne 0) { Fail "bun install failed." }
            & bun run build
            if ($LASTEXITCODE -ne 0) { Fail "bun run build failed." }
        } elseif (Test-Path (Join-Path $WebuiDir "package-lock.json")) {
            & npm ci
            if ($LASTEXITCODE -ne 0) { Fail "npm ci failed." }
            & npm run build
            if ($LASTEXITCODE -ne 0) { Fail "npm run build failed." }
        } else {
            & npm install
            if ($LASTEXITCODE -ne 0) { Fail "npm install failed." }
            & npm run build
            if ($LASTEXITCODE -ne 0) { Fail "npm run build failed." }
        }
    } finally {
        Pop-Location
    }

    if (-not (Test-Path $IndexHtml)) {
        Fail "WebUI build finished but $IndexHtml is missing."
    }
    Write-Info "WebUI build ready: $WebuiDist"
}

function Invoke-OnboardIfNeeded {
    param([string]$VenvPython)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ConfigPath) *> $null
    New-Item -ItemType Directory -Force -Path $WorkspacePath *> $null

    if ($SkipWizard -or $env:NANOBOT_SKIP_WIZARD -eq "1") {
        Write-Info "Skipping setup wizard because --SkipWizard or NANOBOT_SKIP_WIZARD=1 was used."
        return
    }

    if (Test-Path $ConfigPath) {
        Write-Info "Config already exists: $ConfigPath"
        return
    }

    Write-Info "No config found. Starting first-run setup wizard..."
    $env:PYTHONPATH = if ($env:PYTHONPATH) { "$ScriptDir;$env:PYTHONPATH" } else { $ScriptDir }
    & $VenvPython -m nanobot onboard --config $ConfigPath --workspace $WorkspacePath --wizard
    if ($LASTEXITCODE -ne 0) {
        Fail "Setup wizard did not complete. Rerun install.bat or run .venv\Scripts\python.exe -m nanobot onboard --config .local\config.json --workspace .local\workspace --wizard"
    }
}

Set-Location $ScriptDir
if (-not (Test-Path (Join-Path $ScriptDir "pyproject.toml"))) {
    Fail "run this script from the nanobot_skill repository checkout"
}

$Python = Find-Python
$Version = & $Python --version
Write-Info "Using Python: $Version"

if ($DryRun) {
    Write-Info "Dry run: would create or reuse venv: $VenvDir"
    Write-Info "Dry run: would install: pip install -e .[$Extras]"
    Write-Info "Dry run: would build WebUI dist with bun or npm if nanobot\web\dist\index.html is missing"
    Write-Info "Dry run: would create config with: nanobot onboard --config $ConfigPath --workspace $WorkspacePath --wizard"
    Write-Info "Dry run: would run with: start-nanobot.bat"
    exit 0
}

$VenvPython = New-NanobotVenv $Python
& $VenvPython -m ensurepip --upgrade *> $null
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Fail "pip upgrade failed." }
& $VenvPython -m pip install -e ".[${Extras}]"
if ($LASTEXITCODE -ne 0) { Fail "nanobot_skill editable install failed." }

Write-Info "Installed nanobot_skill:"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$ScriptDir;$env:PYTHONPATH" } else { $ScriptDir }
& $VenvPython -m nanobot --version
if ($LASTEXITCODE -ne 0) { Fail "nanobot command could not be started after installation." }

Ensure-WebuiDist
Invoke-OnboardIfNeeded $VenvPython

Write-Info "Installation complete."
Write-Info "Run nanobot_skill with: start-nanobot.bat"
