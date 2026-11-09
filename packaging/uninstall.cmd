@echo off
setlocal
set "TEMP=%~dp0..\.yanzhi-installer-temp"
if not exist "%TEMP%" mkdir "%TEMP%"
if not exist "%TEMP%" exit /b 1
set "TMP=%TEMP%"
rem Hand off before the uninstaller removes this launcher file.
start "" "%~dp0unins000.exe" %*
exit /b 0
