param(
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv")) {
    throw "Main .venv was not found. Create it and install requirements before building."
}

& ".\.venv\Scripts\python.exe" -m compileall . -q
& ".\.venv\Scripts\python.exe" -m pytest
& ".\.venv\Scripts\pyinstaller.exe" --noconfirm --clean PythonRPARecorder.spec

if (-not $SkipInstaller) {
    & "$PSScriptRoot\build_installer.ps1"
}

Write-Host "Built dist\PythonRPARecorder\PythonRPARecorder.exe"
