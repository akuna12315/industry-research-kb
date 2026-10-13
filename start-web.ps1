$ErrorActionPreference = 'Stop'
$kbRoot = $PSScriptRoot
$kbPython = Join-Path $kbRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $kbPython)) {
    python -m venv (Join-Path $kbRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建环境失败' }
}
$kbRequirements = Join-Path $kbRoot 'web/requirements.txt'
$kbStampPath = Join-Path $kbRoot 'data/web-requirements.sha256'
$kbHash = (Get-FileHash -LiteralPath $kbRequirements -Algorithm SHA256).Hash
if (-not (Test-Path -LiteralPath $kbStampPath) -or (Get-Content -LiteralPath $kbStampPath -Raw).Trim() -ne $kbHash) {
    & $kbPython -m pip install --cache-dir (Join-Path $kbRoot 'work/pip-cache') -r $kbRequirements
    if ($LASTEXITCODE -ne 0) { throw '安装依赖失败' }
    New-Item -ItemType Directory -Force -Path (Join-Path $kbRoot 'data') | Out-Null
    Set-Content -LiteralPath $kbStampPath -Value $kbHash
}
& $kbPython (Join-Path $kbRoot 'scripts/init_workspace.py')
if ($LASTEXITCODE -ne 0) { throw '初始化配置失败' }
Set-Location -LiteralPath $kbRoot
Write-Host 'Industry Atlas: http://127.0.0.1:8765'
Write-Host '按 Ctrl+C 停止；厂商API密钥可通过 KB_MODEL_API_KEY 环境变量加载。'
& $kbPython -m web.server
