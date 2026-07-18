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
if ($LASTEXITCODE -ne 0) { throw "Python compilation checks failed." }

& ".\.venv\Scripts\python.exe" -m pytest
if ($LASTEXITCODE -ne 0) { throw "Tests failed; packaging was not started." }

& ".\.venv\Scripts\pyinstaller.exe" --noconfirm --clean PythonRPARecorder.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller packaging failed." }

if (-not $SkipInstaller) {
    & "$PSScriptRoot\build_installer.ps1"
}

Write-Host "Built dist\PythonRPARecorder\PythonRPARecorder.exe"
