$ErrorActionPreference = 'Stop'
$kbRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$kbWork = Join-Path $kbRoot 'work/protected'
$kbPython = Join-Path $kbRoot 'work/protected-tools/venv311/Scripts/python.exe'
$kbInner = Join-Path $kbWork 'inner-setup'
$kbDist = Join-Path $kbRoot 'dist/protected'
$kbApp = Join-Path $kbWork 'dist/YanZhi'
$kbIscc = Join-Path $env:LOCALAPPDATA 'Programs/Inno Setup 6/ISCC.exe'
$kbCsc = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$env:TEMP = Join-Path $kbRoot 'work/protected-tools/temp'
$env:TMP = $env:TEMP
$kbNativeTestFile = Join-Path $kbWork 'native-tests-result.json'
if (-not (Test-Path -LiteralPath $kbNativeTestFile)) { throw 'Native regression result is missing; this build cannot be released' }
$kbNativeTests = Get-Content -LiteralPath $kbNativeTestFile -Raw | ConvertFrom-Json
if (-not $kbNativeTests.ok -or $kbNativeTests.status -ne 'finished' -or $kbNativeTests.tests -lt 271) { throw 'Native regression must finish successfully before creating a release installer' }
$kbNativeFiles = @(Get-ChildItem -LiteralPath (Join-Path $kbApp '_internal') -Filter 'protected_core*.pyd')
if ($kbNativeFiles.Count -ne 1) { throw 'Expected one tested native module' }
if ((Get-FileHash -LiteralPath $kbNativeFiles[0].FullName -Algorithm SHA256).Hash -ne $kbNativeTests.native_sha256) { throw 'Native module changed after regression' }
if ((Get-FileHash -LiteralPath (Join-Path $kbApp 'YanZhi.exe') -Algorithm SHA256).Hash -ne $kbNativeTests.exe_sha256) { throw 'Executable changed after regression' }
& $kbPython (Join-Path $PSScriptRoot 'audit.py')
if ($LASTEXITCODE -ne 0) { throw 'Protected distribution audit failed' }
New-Item -ItemType Directory -Force -Path $kbInner,$kbDist | Out-Null
& $kbIscc ('/DBuildRoot=' + $kbApp) ('/DOutputRoot=' + $kbInner) ('/DProjectRoot=' + $kbRoot) (Join-Path $kbWork 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Protected installer compilation failed' }
$kbName = 'YanZhi-Setup-0.1.5-Protected-x64.exe'
$kbPayload = Join-Path $kbInner $kbName
$kbHashFile = Join-Path $kbInner 'inner.sha256'
(Get-FileHash -LiteralPath $kbPayload -Algorithm SHA256).Hash | Set-Content -LiteralPath $kbHashFile -Encoding ascii
$kbSetup = Join-Path $kbDist $kbName
& $kbCsc /nologo /target:winexe /platform:x64 /optimize+ /reference:System.Windows.Forms.dll `
    ('/win32manifest:' + (Join-Path $kbRoot 'packaging/setup.manifest')) `
    ('/resource:' + $kbPayload + ',YanZhi.Setup.Payload') `
    ('/resource:' + $kbHashFile + ',YanZhi.Setup.SHA256') `
    ('/out:' + $kbSetup) (Join-Path $kbRoot 'packaging/SetupBootstrap.cs')
if ($LASTEXITCODE -ne 0) { throw 'Protected bootstrap compilation failed' }
Copy-Item -LiteralPath (Join-Path $kbWork 'README.txt') -Destination (Join-Path $kbDist 'README.txt')
(Get-FileHash -LiteralPath $kbSetup -Algorithm SHA256).Hash + '  ' + $kbName |
    Set-Content -LiteralPath (Join-Path $kbDist 'SHA256SUMS.txt') -Encoding ascii
Write-Host ('Protected installer: ' + $kbSetup)
