param([switch]$SkipCompile, [switch]$NativeOnly, [string]$Python = '', [string]$Node = '')
$ErrorActionPreference = 'Stop'
$kbRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$kbWork = Join-Path $kbRoot 'work/protected'
$kbPython = if ($Python) { $Python } else { Join-Path $kbRoot 'work/protected-tools/venv311/Scripts/python.exe' }
$kbNode = if ($Node) { $Node } else { (Get-Command node -ErrorAction Stop).Source }
$env:NUITKA_CACHE_DIR = Join-Path $kbRoot 'work/protected-tools/cache'
$env:NUITKA_UPDATE_CHECK = 'never'
$env:TEMP = Join-Path $kbRoot 'work/protected-tools/temp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
$env:PYTHONPATH = Join-Path $kbWork 'source/scripts'
Set-Location -LiteralPath $kbRoot
& $kbPython (Join-Path $PSScriptRoot 'prepare.py')
if ($LASTEXITCODE -ne 0) { throw 'Protected source staging failed' }
& $kbNode (Join-Path $PSScriptRoot 'obfuscate.cjs')
if ($LASTEXITCODE -ne 0) { throw 'Frontend protection failed' }
& $kbNode (Join-Path $kbWork 'frontend.test.cjs')
if ($LASTEXITCODE -ne 0) { throw 'Protected frontend regression failed' }
if (-not $SkipCompile) {
    Set-Location -LiteralPath (Join-Path $kbWork 'source')
    $kbNuitka = @('-m','nuitka','protected_core.py','--mode=module','--msvc=latest','--jobs=4','--lto=no',
        '--no-pyi-file','--no-pyi-stubs',
        '--assume-yes-for-downloads','--update-check=never','--include-package=web',
        '--include-module=kb','--include-module=workspace',
        ('--output-dir=' + (Join-Path $kbWork 'build')),
        ('--report=' + (Join-Path $kbWork 'compilation-report.xml')))
    & $kbPython @kbNuitka
    if ($LASTEXITCODE -ne 0) { throw 'Native compilation failed' }
}
Set-Location -LiteralPath $kbRoot
if ($NativeOnly) { Write-Host 'Native module compilation finished.'; exit 0 }
& $kbPython -m PyInstaller --clean --noconfirm --distpath (Join-Path $kbWork 'dist') --workpath (Join-Path $kbWork 'pyinstaller') (Join-Path $PSScriptRoot 'protected.spec')
if ($LASTEXITCODE -ne 0) { throw 'Protected distribution assembly failed' }
$kbExe = Join-Path $kbWork 'dist/YanZhi/YanZhi.exe'
$kbReport = Join-Path $kbWork 'self-test.json'
$kbTest = Start-Process -FilePath $kbExe -ArgumentList @('--self-test', ('"' + $kbReport + '"')) -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput (Join-Path $kbWork 'self-test-stdout.log') -RedirectStandardError (Join-Path $kbWork 'self-test-stderr.log')
if ($kbTest.ExitCode -ne 0) { throw 'Protected binary self-test failed' }
$kbResult = Get-Content -LiteralPath $kbReport -Raw | ConvertFrom-Json
if (-not $kbResult.ok -or -not $kbResult.native_protection) { throw 'Protected native code verification failed' }
Write-Host 'Protected runtime and native-code self-test passed.'
