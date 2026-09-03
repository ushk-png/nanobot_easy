@echo off
rem Windows Explorer double-click entry point.
rem
rem Runs the sibling bootstrap.ps1 when this file sits inside a checkout. When
rem it was downloaded on its own, it fetches bootstrap.ps1 instead, so
rem receiving this single file is enough to install nanobot-easy.
setlocal

set "SCRIPT_DIR=%~dp0"
set "BOOTSTRAP_PS1=%SCRIPT_DIR%bootstrap.ps1"
set "BOOTSTRAP_URL=https://raw.githubusercontent.com/ushk-png/nanobot_easy/main/bootstrap.ps1"

if exist "%BOOTSTRAP_PS1%" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%BOOTSTRAP_PS1%" %*
) else (
  echo bootstrap.ps1 was not found next to this file; downloading it...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm '%BOOTSTRAP_URL%' | iex"
)
set "EXITCODE=%ERRORLEVEL%"

echo.
echo Setup finished. You can close this window, or press any key to exit.
pause >nul
exit /b %EXITCODE%
