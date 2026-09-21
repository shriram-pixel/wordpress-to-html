<#
.SYNOPSIS
    One-time setup for wp-static-converter on Windows.

.DESCRIPTION
    Creates a virtual environment, installs the Python dependencies, downloads
    the Chromium build Playwright drives, and optionally provisions a private,
    portable copy of PHP and MariaDB.

    Nothing is installed system-wide. No services are registered, nothing is
    added to PATH, and no existing PHP, MySQL or WordPress installation on this
    machine is touched or modified. Everything lives under this folder.

.PARAMETER SkipRuntimes
    Do not download PHP and MariaDB now. They are then fetched automatically the
    first time a conversion runs, or you can point the tool at an existing
    install with WPSC_PHP_BINARY / WPSC_MYSQLD_BINARY in .env.

.PARAMETER Force
    Recreate the virtual environment from scratch.

.EXAMPLE
    .\setup.ps1
.EXAMPLE
    .\setup.ps1 -SkipRuntimes
#>

[CmdletBinding()]
param(
    [switch]$SkipRuntimes,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$script:StepNumber = 0

function Write-Step {
    param([string]$Message)
    $script:StepNumber++
    Write-Host ''
    Write-Host "[$script:StepNumber] $Message" -ForegroundColor Cyan
}

function Write-Ok   { param([string]$m) Write-Host "    OK  $m" -ForegroundColor Green }
function Write-Warn { param([string]$m) Write-Host "    !   $m" -ForegroundColor Yellow }
function Write-Fail { param([string]$m) Write-Host "    X   $m" -ForegroundColor Red }

Write-Host ''
Write-Host '  WordPress -> Static HTML converter' -ForegroundColor White
Write-Host '  Setup' -ForegroundColor DarkGray
Write-Host '  ---------------------------------------------------------------'

# ---------------------------------------------------------------- Python ----
Write-Step 'Checking Python'

$python = $null
foreach ($candidate in @('py -3.13', 'py -3.12', 'py -3.11', 'py -3', 'python')) {
    $parts = $candidate -split ' ', 2
    $exe = $parts[0]
    $argument = if ($parts.Count -gt 1) { $parts[1] } else { $null }

    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }

    try {
        $version = if ($argument) { & $exe $argument --version 2>&1 } else { & $exe --version 2>&1 }
    } catch { continue }

    if ($version -match 'Python (\d+)\.(\d+)\.(\d+)') {
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        if ($major -eq 3 -and $minor -ge 11) {
            $python = $candidate
            Write-Ok "$version  ($candidate)"
            break
        }
    }
}

if (-not $python) {
    Write-Fail 'Python 3.11 or newer was not found.'
    Write-Host ''
    Write-Host '    Install it from https://www.python.org/downloads/'
    Write-Host '    Tick "Add python.exe to PATH" in the installer, then run this script again.'
    exit 1
}

# ------------------------------------------------------------------ venv ----
Write-Step 'Preparing the virtual environment'

if ($Force -and (Test-Path '.venv')) {
    Write-Warn 'removing the existing .venv (-Force)'
    Remove-Item -Recurse -Force '.venv'
}

if (-not (Test-Path '.venv')) {
    $parts = $python -split ' ', 2
    if ($parts.Count -gt 1) { & $parts[0] $parts[1] -m venv .venv } else { & $parts[0] -m venv .venv }
    if ($LASTEXITCODE -ne 0) { Write-Fail 'could not create the virtual environment'; exit 1 }
    Write-Ok 'created .venv'
} else {
    Write-Ok '.venv already exists'
}

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) { Write-Fail "missing $venvPython"; exit 1 }

# ------------------------------------------------------------ dependencies --
Write-Step 'Installing Python dependencies'

& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Fail 'dependency installation failed'
    Write-Host '    If a package tried to build from source, install the Microsoft C++ Build Tools,'
    Write-Host '    or use a Python version that has prebuilt wheels (3.12 is a safe choice).'
    exit 1
}
Write-Ok 'dependencies installed'

# -------------------------------------------------------------- Chromium ----
Write-Step 'Installing the Chromium build used for rendering'

& $venvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Warn 'Chromium could not be installed automatically.'
    Write-Host '    Run this later:  .venv\Scripts\python.exe -m playwright install chromium'
} else {
    Write-Ok 'Chromium ready'
}

# ------------------------------------------------------------------ .env ----
Write-Step 'Configuration file'

if (-not (Test-Path '.env')) {
    if (Test-Path '.env.example') {
        Copy-Item '.env.example' '.env'
        Write-Ok 'created .env from .env.example'
    }
} else {
    Write-Ok '.env already exists; leaving it alone'
}

# -------------------------------------------------------------- runtimes ----
if ($SkipRuntimes) {
    Write-Step 'Skipping PHP and MariaDB (-SkipRuntimes)'
    Write-Warn 'they will be downloaded automatically on the first conversion'
} else {
    Write-Step 'Providing PHP and MariaDB'
    Write-Host '    Rendering a WordPress site means running it, which needs PHP and a'
    Write-Host '    database. Any existing install (XAMPP, Laragon, WAMP, PATH) is detected'
    Write-Host '    and used. Otherwise a private, portable copy is downloaded into'
    Write-Host '    .\runtime (about 130 MB, once).'
    Write-Host ''

    & $venvPython -m scripts.provision_runtimes
    if ($LASTEXITCODE -ne 0) {
        Write-Warn 'the runtimes could not be provisioned now.'
        Write-Host '    The application will retry on the first conversion, and its'
        Write-Host '    dependency panel explains what is missing.'
    }
}

# ----------------------------------------------------------- diagnostics ----
Write-Step 'Checking everything'

& $venvPython scripts\doctor.py
$doctorResult = $LASTEXITCODE

Write-Host ''
Write-Host '  ---------------------------------------------------------------'
if ($doctorResult -eq 0) {
    Write-Host '  Setup complete.' -ForegroundColor Green
} else {
    Write-Host '  Setup finished with warnings; see above.' -ForegroundColor Yellow
}
Write-Host ''
Write-Host '  Start the application with:' -ForegroundColor White
Write-Host '      .\run.ps1' -ForegroundColor Cyan
Write-Host ''
Write-Host '  Then open http://127.0.0.1:8000 and choose your .wpress file.'
Write-Host ''
