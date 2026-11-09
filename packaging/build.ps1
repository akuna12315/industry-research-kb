param(
    [string]$Python = '',
    [string]$Iscc = '',
    [switch]$SkipDependencies
)
$ErrorActionPreference = 'Stop'
$kbRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $kbRoot
if (-not $Python) {
    $Python = Join-Path $kbRoot 'work/packaging-tools/venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $Python)) {
        python -m venv (Join-Path $kbRoot 'work/packaging-tools/venv')
        if ($LASTEXITCODE -ne 0) { throw '创建打包环境失败' }
    }
}
if (-not $SkipDependencies) {
    & $Python -m pip install --cache-dir work/pip-cache -r packaging/requirements-build.lock
    if ($LASTEXITCODE -ne 0) { throw '安装打包依赖失败' }
}
if (-not $Iscc) {
    $kbCandidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs/Inno Setup 6/ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6/ISCC.exe')
    )
    $Iscc = $kbCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw '需要安装 Inno Setup 6，或使用 -Iscc 指定 ISCC.exe。' }
New-Item -ItemType Directory -Force -Path work/packaging,dist | Out-Null
& $Python packaging/prepare_notices.py
if ($LASTEXITCODE -ne 0) { throw '第三方声明生成失败' }
& $Python -m PyInstaller --clean --noconfirm --distpath work/packaging/dist --workpath work/packaging/build packaging/yanzhi.spec
if ($LASTEXITCODE -ne 0) { throw 'EXE构建失败' }
$kbExe = Join-Path $kbRoot 'work/packaging/dist/YanZhi/YanZhi.exe'
$kbReport = Join-Path $kbRoot 'work/packaging/frozen-self-test.json'
$kbTest = Start-Process -FilePath $kbExe -ArgumentList @('--self-test', ('"' + $kbReport + '"')) -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput work/packaging/frozen-stdout.log -RedirectStandardError work/packaging/frozen-stderr.log
if ($kbTest.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $kbReport)) { throw '打包EXE自检失败' }
$kbResult = Get-Content -LiteralPath $kbReport -Raw | ConvertFrom-Json
if (-not $kbResult.ok -or -not $kbResult.frozen) { throw '冻结程序自检失败，请查看报告' }
& $Python packaging/audit_bundle.py
if ($LASTEXITCODE -ne 0) { throw '安装包资料边界检查失败' }
& (Join-Path $PSScriptRoot 'build-setup.ps1') -Iscc $Iscc
& $Python packaging/check_setup_privileges.py
if ($LASTEXITCODE -ne 0) { throw 'Installer privilege manifest verification failed' }
Copy-Item -LiteralPath packaging/README.txt -Destination dist/使用说明.txt
Get-FileHash -LiteralPath dist/YanZhi-Setup-0.1.5-x64.exe -Algorithm SHA256 |
    ForEach-Object { $_.Hash + '  YanZhi-Setup-0.1.5-x64.exe' } |
    Set-Content -LiteralPath dist/SHA256SUMS.txt -Encoding ascii
Write-Host '构建完成：dist/YanZhi-Setup-0.1.5-x64.exe'
