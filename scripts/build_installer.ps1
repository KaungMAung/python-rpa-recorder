$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path "dist\PythonRPARecorder\PythonRPARecorder.exe")) {
    throw "dist\PythonRPARecorder\PythonRPARecorder.exe not found. Run scripts\build.ps1 -SkipInstaller first."
}

$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            $iscc = Get-Item $candidate
            break
        }
    }
}
if (-not $iscc) {
    throw "Inno Setup compiler was not found. Install it with: winget install --id JRSoftware.InnoSetup -e"
}
$isccPath = if ($iscc.Source) { $iscc.Source } else { $iscc.FullName }

New-Item -ItemType Directory -Force -Path "installer_output" | Out-Null
& $isccPath "installer\PythonRPARecorder.iss"

Write-Host "Installer created: installer_output\PythonRPARecorderSetup.exe"
