param(
    [string]$OutputRoot = (Join-Path $PSScriptRoot "dist")
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$distribution = Join-Path $OutputRoot "HanziLab"

$serverConfigPath = Join-Path $PSScriptRoot "dictionary-server.json"
if (-not (Test-Path -LiteralPath $serverConfigPath)) {
    throw "Release configuration is missing: dictionary-server.json"
}
$serverConfig = Get-Content -LiteralPath $serverConfigPath -Raw | ConvertFrom-Json
if (-not $serverConfig.url -or -not $serverConfig.token) {
    throw "Release configuration must include the dictionary server URL and token"
}
if ($serverConfig.ca_file -and -not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $serverConfig.ca_file))) {
    throw "Release CA certificate is missing: $($serverConfig.ca_file)"
}

python -m PyInstaller --noconfirm --clean --distpath $OutputRoot HanziLab.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name HanziLabUpdater `
    --icon (Join-Path $PSScriptRoot "assets\icons\updater.ico") `
    --distpath (Join-Path $PSScriptRoot "build\updater-dist") `
    --workpath (Join-Path $PSScriptRoot "build\updater-build") `
    --specpath (Join-Path $PSScriptRoot "build") `
    (Join-Path $PSScriptRoot "updater.py")
if ($LASTEXITCODE -ne 0) {
    throw "Updater build failed with exit code $LASTEXITCODE"
}
$updater = Join-Path $PSScriptRoot "build\updater-dist\HanziLabUpdater.exe"
if (-not (Test-Path -LiteralPath $updater)) {
    throw "Build completed without HanziLabUpdater.exe"
}
Copy-Item -LiteralPath $updater -Destination (Join-Path $distribution "_internal\HanziLabUpdater.exe") -Force

$executable = Join-Path $distribution "HanziLab.exe"
if (-not (Test-Path -LiteralPath $executable)) {
    throw "Build completed without HanziLab.exe"
}

$foreignIcu = Get-ChildItem -LiteralPath (Join-Path $distribution "_internal") -File |
    Where-Object { $_.Name -eq "icuuc.dll" -or $_.Name -like "icudt*.dll" }
if ($foreignIcu) {
    throw "Build contains an incompatible ICU runtime: $($foreignIcu.Name -join ', ')"
}

$dataDirectory = Join-Path $distribution "data"
foreach ($configFile in @("dictionary-server.json", "dictionary-server-ca.pem")) {
    $source = Join-Path $PSScriptRoot $configFile
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $distribution $configFile) -Force
    }
}
New-Item -ItemType Directory -Path $dataDirectory -Force | Out-Null

$smokeTest = Start-Process -FilePath $executable -ArgumentList "--smoke-test" -Wait -PassThru -WindowStyle Hidden
if ($smokeTest.ExitCode -ne 0) {
    throw "HanziLab smoke test failed with exit code $($smokeTest.ExitCode)"
}

Write-Host "Ready: $executable"
