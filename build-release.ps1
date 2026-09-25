$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

python -m PyInstaller --noconfirm --clean HanziLab.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$executable = Join-Path $PSScriptRoot "dist\HanziLab\HanziLab.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Build completed without HanziLab.exe"
}

$foreignIcu = Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot "dist\HanziLab\_internal") -File |
    Where-Object { $_.Name -eq "icuuc.dll" -or $_.Name -like "icudt*.dll" }
if ($foreignIcu) {
    throw "Build contains an incompatible ICU runtime: $($foreignIcu.Name -join ', ')"
}

$dataDirectory = Join-Path $PSScriptRoot "dist\HanziLab\data"
foreach ($configFile in @("dictionary-server.json", "dictionary-server-ca.pem")) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $configFile) -Destination (Join-Path $PSScriptRoot "dist\HanziLab\$configFile") -Force
}
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null

$smokeTest = Start-Process -FilePath $executable -ArgumentList "--smoke-test" -Wait -PassThru -WindowStyle Hidden
if ($smokeTest.ExitCode -ne 0) {
    throw "HanziLab smoke test failed with exit code $($smokeTest.ExitCode)"
}

Write-Host "Ready: $executable"
