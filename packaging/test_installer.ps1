param([string]$Installer = '', [switch]$PayloadOnly, [switch]$IsolatedTemp)
$ErrorActionPreference = 'Stop'
$kbRoot = Split-Path -Parent $PSScriptRoot
if (-not $Installer) { $Installer = Join-Path $kbRoot 'dist/YanZhi-Setup-0.1.5-x64.exe' }
if (-not $PayloadOnly) {
    $kbIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $kbPrincipal = [System.Security.Principal.WindowsPrincipal]::new($kbIdentity)
    if (-not $kbPrincipal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'The 0.1.5 installer requires administrator approval. Run installation acceptance from an elevated test console; do not silently bypass UAC.'
    }
}
$kbUninstallKey = 'HKCU:/Software/Microsoft/Windows/CurrentVersion/Uninstall/{45D16911-D834-470D-8506-E44FA715D76E}_is1'
$kbMachineUninstallKey = 'HKLM:/Software/Microsoft/Windows/CurrentVersion/Uninstall/{45D16911-D834-470D-8506-E44FA715D76E}_is1'
if ((Test-Path -LiteralPath $kbUninstallKey) -or (Test-Path -LiteralPath $kbMachineUninstallKey)) { throw 'YanZhi is already installed. Refusing to replace that installation for testing.' }
$kbTestRoot = Join-Path $kbRoot ('work/packaging/install-test-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
$kbApp = Join-Path $kbTestRoot '安装程序'
$kbData = Join-Path $kbTestRoot '保留资料'
$kbReport = Join-Path $kbTestRoot 'installed-self-test.json'
New-Item -ItemType Directory -Path $kbTestRoot,$kbData | Out-Null
Set-Content -LiteralPath (Join-Path $kbData 'keep.txt') -Value 'user-data-must-survive-upgrade-and-uninstall' -Encoding ascii
$kbBefore = (Get-FileHash -LiteralPath (Join-Path $kbData 'keep.txt')).Hash
$kbInstallArgs = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/LANG=chinesesimp',
    ('/DIR="' + $kbApp + '"'), '/GROUP="YanZhi Packaging Test"', '/TASKS=""')
$kbInstalled = $false
$kbPrevTemp = $env:TEMP
$kbPrevTmp = $env:TMP
$kbTemp = Join-Path $kbTestRoot 'setup-temp'
New-Item -ItemType Directory -Path $kbTemp | Out-Null
if ($IsolatedTemp) {
    $env:TEMP = $kbTemp
    $env:TMP = $kbTemp
}
try {
    $kbProcess = Start-Process -FilePath $Installer -ArgumentList ($kbInstallArgs + @('/LOG="' + (Join-Path $kbTestRoot 'install.log') + '"')) -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput (Join-Path $kbTestRoot 'install-stdout.log') -RedirectStandardError (Join-Path $kbTestRoot 'install-stderr.log')
    if ($kbProcess.ExitCode -ne 0) { throw 'Silent install failed' }
    $kbInstalled = $true
    $kbExe = Join-Path $kbApp 'YanZhi.exe'
    if (-not (Test-Path -LiteralPath $kbExe)) { throw 'Installed executable missing' }
    $kbRegistered = (Test-Path -LiteralPath $kbUninstallKey) -or (Test-Path -LiteralPath $kbMachineUninstallKey)
    $kbShortcut = Join-Path $env:APPDATA 'Microsoft/Windows/Start Menu/Programs/研知/研知 · 行业知识库.lnk'
    $kbHasShortcut = Test-Path -LiteralPath $kbShortcut
    if (-not $kbHasShortcut) {
        $kbCommonShortcut = Join-Path ([Environment]::GetFolderPath('CommonStartMenu')) 'Programs/研知/研知 · 行业知识库.lnk'
        $kbHasShortcut = Test-Path -LiteralPath $kbCommonShortcut
    }
    if (-not $kbRegistered -and -not (Test-Path -LiteralPath (Join-Path $kbApp 'uninstall.cmd'))) { throw 'Local uninstall entry missing' }
    $kbSavedPath = $env:PATH
    $kbSavedPythonPath = $env:PYTHONPATH
    $kbSavedPythonHome = $env:PYTHONHOME
    try {
        $env:PATH = (Join-Path $env:WINDIR 'System32') + ';' + $env:WINDIR
        Remove-Item Env:PYTHONPATH,Env:PYTHONHOME -ErrorAction SilentlyContinue
        $kbProcess = Start-Process -FilePath $kbExe -ArgumentList @('--self-test', ('"' + $kbReport + '"')) -WorkingDirectory $kbTestRoot -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $kbTestRoot 'app-stdout.log') -RedirectStandardError (Join-Path $kbTestRoot 'app-stderr.log')
        if ($kbProcess.ExitCode -ne 0) { throw 'Installed executable self-test failed' }
    } finally {
        $env:PATH = $kbSavedPath
        if ($null -eq $kbSavedPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $kbSavedPythonPath }
        if ($null -eq $kbSavedPythonHome) { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue } else { $env:PYTHONHOME = $kbSavedPythonHome }
    }
    $kbResult = Get-Content -LiteralPath $kbReport -Raw -Encoding utf8 | ConvertFrom-Json
    if (-not $kbResult.ok -or -not $kbResult.frozen) { throw 'Installed self-test result is invalid' }
    $kbProcess = Start-Process -FilePath $Installer -ArgumentList ($kbInstallArgs + @('/LOG="' + (Join-Path $kbTestRoot 'upgrade.log') + '"')) -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput (Join-Path $kbTestRoot 'upgrade-stdout.log') -RedirectStandardError (Join-Path $kbTestRoot 'upgrade-stderr.log')
    if ($kbProcess.ExitCode -ne 0) { throw 'Upgrade reinstall failed' }
    if ((Get-FileHash -LiteralPath (Join-Path $kbData 'keep.txt')).Hash -ne $kbBefore) { throw 'Upgrade changed data' }
} finally {
    if ($kbInstalled) {
        $kbUninstaller = [System.IO.Path]::GetFullPath((Join-Path $kbApp 'unins000.exe'))
        $kbExpectedApp = [System.IO.Path]::GetFullPath($kbApp) + [System.IO.Path]::DirectorySeparatorChar
        if (-not $kbUninstaller.StartsWith($kbExpectedApp, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Uninstall path escaped test application directory' }
        if (-not (Test-Path -LiteralPath $kbUninstaller)) { throw 'Test uninstaller missing' }
        & (Join-Path $kbApp 'uninstall.cmd') /VERYSILENT /SUPPRESSMSGBOXES /NORESTART ('/LOG=' + (Join-Path $kbTestRoot 'uninstall.log'))
        if ($LASTEXITCODE -ne 0) { throw 'Uninstall launcher failed' }
        $kbUninstallDeadline = (Get-Date).AddSeconds(30)
        $kbUninstallDone = $false
        while ((Get-Date) -lt $kbUninstallDeadline) {
            $kbUninstallLog = Join-Path $kbTestRoot 'uninstall.log'
            if ((Test-Path -LiteralPath $kbUninstallLog) -and -not (Test-Path -LiteralPath $kbExe)) {
                $kbUninstallDone = [bool](Select-String -LiteralPath $kbUninstallLog -SimpleMatch 'Uninstallation process succeeded.' -Quiet)
                if ($kbUninstallDone) { break }
            }
            Start-Sleep -Milliseconds 200
        }
        if (-not $kbUninstallDone) { throw 'Uninstall did not complete successfully' }
    }
}
$env:TEMP = $kbPrevTemp
$env:TMP = $kbPrevTmp
if (Test-Path -LiteralPath (Join-Path $kbApp 'YanZhi.exe')) { throw 'Executable remains after uninstall' }
if ((Get-FileHash -LiteralPath (Join-Path $kbData 'keep.txt')).Hash -ne $kbBefore) { throw 'Uninstall changed data' }
if ((Test-Path -LiteralPath $kbUninstallKey) -or (Test-Path -LiteralPath $kbMachineUninstallKey)) { throw 'Uninstall registration remains' }
@{ ok=$true; payload_only=[bool]$PayloadOnly; standard_registry_install=$kbRegistered; registry_fallback=(-not $kbRegistered); install=$true; chinese_path=$true; start_menu_shortcut=$kbHasShortcut; no_python_in_path=$true;
   frozen_self_test=$true; reinstall=$true; uninstall=$true; external_data_preserved=$true;
   report=$kbReport } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $kbRoot 'work/packaging/installer-test-015.json') -Encoding utf8
Get-Content -LiteralPath (Join-Path $kbRoot 'work/packaging/installer-test-015.json')
