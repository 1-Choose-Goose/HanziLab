$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

python -m PyInstaller --noconfirm --clean HanziLab.spec

$executable = Join-Path $PSScriptRoot "dist\HanziLab\HanziLab.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Build completed without HanziLab.exe"
}

Write-Host "Ready: $executable"
