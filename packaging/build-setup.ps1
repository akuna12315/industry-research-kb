param([string]$Iscc = '', [string]$AppRoot = '')
$ErrorActionPreference = 'Stop'
$kbRoot = Split-Path -Parent $PSScriptRoot
if (-not $AppRoot) { $AppRoot = Join-Path $kbRoot 'work/packaging/dist/YanZhi' }
if (-not $Iscc) { $Iscc = Join-Path $env:LOCALAPPDATA 'Programs/Inno Setup 6/ISCC.exe' }
$kbCsc = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (-not (Test-Path -LiteralPath $kbCsc)) { throw '.NET Framework compiler is unavailable' }
$kbInner = Join-Path $kbRoot 'work/packaging/inner-setup'
$kbDist = Join-Path $kbRoot 'dist'
New-Item -ItemType Directory -Force -Path $kbInner,$kbDist | Out-Null
& $Iscc ('/DBuildRoot=' + $AppRoot) ('/DOutputRoot=' + $kbInner) ('/DProjectRoot=' + $kbRoot) (Join-Path $PSScriptRoot 'installer.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inner installer build failed' }
$kbInnerExe = Join-Path $kbInner 'YanZhi-Setup-0.1.5-x64.exe'
$kbHashFile = Join-Path $kbInner 'inner.sha256'
(Get-FileHash -LiteralPath $kbInnerExe -Algorithm SHA256).Hash | Set-Content -LiteralPath $kbHashFile -Encoding ascii
$kbSetup = Join-Path $kbDist 'YanZhi-Setup-0.1.5-x64.exe'
& $kbCsc /nologo /target:winexe /platform:x64 /optimize+ /reference:System.Windows.Forms.dll `
    ('/win32manifest:' + (Join-Path $PSScriptRoot 'setup.manifest')) `
    ('/resource:' + $kbInnerExe + ',YanZhi.Setup.Payload') `
    ('/resource:' + $kbHashFile + ',YanZhi.Setup.SHA256') `
    ('/out:' + $kbSetup) (Join-Path $PSScriptRoot 'SetupBootstrap.cs')
if ($LASTEXITCODE -ne 0) { throw 'Setup bootstrap build failed' }
(Get-FileHash -LiteralPath $kbSetup -Algorithm SHA256).Hash + '  YanZhi-Setup-0.1.5-x64.exe' |
    Set-Content -LiteralPath (Join-Path $kbDist 'SHA256SUMS.txt') -Encoding ascii
Write-Host ('Built: ' + $kbSetup)
