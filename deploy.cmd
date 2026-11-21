@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
set "YUXI_EXIT=%ERRORLEVEL%"
if not "%YUXI_EXIT%"=="0" pause
exit /b %YUXI_EXIT%
