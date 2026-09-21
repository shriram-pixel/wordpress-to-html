<#
.SYNOPSIS
    Start the wp-static-converter web interface.

.DESCRIPTION
    Launches the local web application and opens it in your browser.
    Everything runs on this machine; nothing is uploaded anywhere.

.PARAMETER Port
    Port to listen on. Default 8000.

.PARAMETER NoBrowser
    Do not open a browser window.

.PARAMETER Doctor
    Print the dependency report and exit without starting the server.

.EXAMPLE
    .\run.ps1
.EXAMPLE
    .\run.ps1 -Port 8080 -NoBrowser
#>

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$NoBrowser,
    [switch]$Doctor
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    Write-Host ''
    Write-Host '  The virtual environment is missing.' -ForegroundColor Red
    Write-Host '  Run the one-time setup first:' -ForegroundColor White
    Write-Host '      .\setup.ps1' -ForegroundColor Cyan
    Write-Host ''
    exit 1
}

if ($Doctor) {
    & $venvPython scripts\doctor.py
    exit $LASTEXITCODE
}

$env:WPSC_PORT = $Port
# Keep log output readable when a site title or filename is not ASCII.
$env:PYTHONIOENCODING = 'utf-8'

$url = "http://127.0.0.1:$Port"

Write-Host ''
Write-Host '  WordPress -> Static HTML converter' -ForegroundColor White
Write-Host "  $url" -ForegroundColor Cyan
Write-Host '  Press Ctrl+C to stop.' -ForegroundColor DarkGray
Write-Host ''

if (-not $NoBrowser) {
    # Give the server a moment to bind before the browser asks for the page.
    Start-Job -ScriptBlock {
        param($target)
        Start-Sleep -Seconds 3
        Start-Process $target
    } -ArgumentList $url | Out-Null
}

& $venvPython app.py
